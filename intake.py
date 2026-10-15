"""进料扫描器 Intake —— 炉子的进料口。

别的组件攒下的现成判断（料），由产出方在自己的 ASSET.yml 里写 `materials:` 声明。
这台扫描器只读声明就能建委派链（扫元数据 ≠ 建对象），用到时才去跑读数口，
再按「逐字段先问正本、正本没给才用派生」合并。

约定正本：docs/INTAKE-SPEC.md（api `intake/v0`）。要点：
  · 只跑声明里写的命令：组件目录里的 .py，用炉子自己的 python 跑，不走 shell
  · 炉子不存第二份事实：读数只在进程内存里按 ttl 缓存，data/ 里不落料
  · 丢条目、截断、口没读成，一律进 problems，不许静默
  · 纯标准库；PyYAML 只在 scan() 里懒加载，没装就把进料整个关掉，炉子其余功能照常

跑法：
  python intake.py registry                              # 登记表 + 委派链，不跑任何读数
  python intake.py materials [--kind K] [--min-count N]  # 跑读数、合并后的料
  python intake.py --selftest                            # 临时目录造假卡自测，不碰真实数据
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
API = "intake/v0"

KINDS = ("decision", "profile", "insight", "kg")     # kind 词表 v0（规范第 5.1 节）
TIERS = ("canon", "derived")
# 公开仓默认一个根都不扫：roots 就是信任边界，加一个根等于信它目录里的脚本
DEFAULTS = {"roots": [], "candidate_min": 3, "timeout": 20}
DEFAULT_TTL = 300
TEXT_MAX = 240
SCAN_DEPTH = 2

PORT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COMP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ITEM_ID = re.compile(r"^([a-z]+):[0-9a-f]{16}$")
HAS_MATERIALS = re.compile(r"^materials\s*:", re.M)
FIELDS = ("id", "kind", "text", "ptr", "ts", "quote", "count", "seen", "extra")
OPTIONAL_TYPES = {"quote": str, "count": int, "seen": list, "extra": dict}
PUBLIC_KEYS = ("component", "id", "title", "kind", "tier", "path")

_cache = {}                 # (口全名, read, dir) -> (时刻, 读数)
_locks = {}
_locks_guard = threading.Lock()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def settings(section=None):
    """config.json 的 intake 段 + 缺省。main.py 传它 read_config() 的那一段；
    传 None 才自己去读文件（CLI 用）。"""
    if section is None:
        section = {}
        if CONFIG_FILE.exists():
            try:
                section = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("intake") or {}
            except Exception as e:
                print("[intake] config.json 读不动（{}），按缺省跑".format(e), file=sys.stderr)
    st = dict(DEFAULTS)
    st.update(section or {})
    roots = st.get("roots") or []
    st["roots"] = [roots] if isinstance(roots, str) else list(roots)
    t = st.get("timeout")
    st["timeout"] = t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 \
        else DEFAULTS["timeout"]
    return st


def _yaml():
    import yaml   # 懒加载：没装 PyYAML 时 import intake 本身不能失败
    return yaml


# ── 扫卡：只读声明，不跑读数 ────────────────────────────────────

def _card_dirs(root):
    """根目录本身 + 下面两层子目录。跳过 .、_ 开头的目录和 node_modules。"""
    out = [root]

    def walk(d, depth):
        if depth > SCAN_DEPTH:
            return
        try:
            kids = sorted((e for e in os.scandir(d) if e.is_dir()), key=lambda e: e.name)
        except OSError:
            return
        for e in kids:
            if e.name.startswith((".", "_")) or e.name == "node_modules":
                continue
            out.append(Path(e.path))
            walk(e.path, depth + 1)

    walk(root, 1)
    return out


def _check_port(m, cdir, comp, ids, problems):
    """校验一条 materials 声明。坏的进 problems、返回 None，同卡别的口照常。"""
    if not isinstance(m, dict):
        problems.append("{}: materials 里有一项不是对象".format(comp))
        return None
    pid = m.get("id")
    where = "{}/{}".format(comp, pid if isinstance(pid, str) and pid else "?")

    def bad(msg):
        problems.append("{}: {}，这个口不进登记表".format(where, msg))

    if not isinstance(pid, str) or not PORT_ID.match(pid):
        return bad("口 id 不合规（小写字母、数字、短横）")
    if pid in ids:
        return bad("口 id 在组件内重复")
    ids.add(pid)

    tier = m.get("tier")
    if tier not in TIERS:
        return bad("tier 只认 canon / derived，收到 {!r}".format(tier))
    kind = m.get("kind")
    if tier == "canon":
        if not isinstance(kind, str):
            return bad("正本口只许写一个 kind（一个字符串）")
        kinds = [kind]
    else:
        kinds = [kind] if isinstance(kind, str) else kind
        if not (isinstance(kinds, list) and kinds and all(isinstance(k, str) for k in kinds)):
            return bad("kind 要么是一个字符串，要么是一组字符串")
        kinds = list(dict.fromkeys(kinds))
        if isinstance(kind, list):
            kind = kinds
    off = [k for k in kinds if k not in KINDS]
    if off:
        return bad("kind {} 不在 v0 词表 {}".format("、".join(off), "/".join(KINDS)))

    read = m.get("read")
    if not (isinstance(read, list) and read and all(isinstance(x, str) and x for x in read)):
        return bad("read 必须是字符串数组 [脚本.py, 参数...]")
    base = cdir.resolve()
    script = (base / read[0]).resolve()
    # 只许组件目录里的 .py：不许 ../ 跑出去，不许 cmd / sh 这类壳子
    if script.suffix.lower() != ".py" or base not in script.parents:
        return bad("read 的脚本必须是组件目录里的 .py：{}".format(read[0]))
    if not script.is_file():
        return bad("read 的脚本不存在：{}".format(read[0]))

    ttl = m.get("ttl")
    ttl = DEFAULT_TTL if ttl is None else ttl
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        return bad("ttl 要是非负整数（秒）")
    title = m.get("title")
    return {"component": comp, "id": pid,
            "title": str(title) if title not in (None, "") else pid,
            "kind": kind, "tier": tier, "path": "{}/{}".format(comp, pid),
            "read": [str(script), *read[1:]], "ttl": ttl, "dir": str(base), "kinds": kinds,
            "card": (base / "ASSET.yml").as_posix()}


def scan(roots=None):
    """扫卡，只读声明。→ {enabled, sources, problems}。

    sources 里每个口带 read / dir / kinds 供内部跑读数用；接口往外吐时只给 PUBLIC_KEYS。
    """
    if roots is None:
        roots = settings()["roots"]
    try:
        yaml = _yaml()
    except ImportError:
        return {"enabled": False, "sources": [],
                "problems": ["没装 PyYAML，进料关闭（炉子其余功能照常）"]}

    sources, problems, comps, seen_cards = [], [], {}, set()
    if not roots:
        problems.append("intake.roots 为空：一个根目录都没配，不扫任何料仓")
    for r in roots:
        root = Path(str(r)).expanduser()
        if not root.is_dir():
            problems.append("根目录不在：{}".format(root))
            continue
        for d in _card_dirs(root):
            card = d / "ASSET.yml"
            if not card.is_file():
                continue
            key = str(card.resolve())
            if key in seen_cards:       # 两个根有重叠时，同一张卡只认一次
                continue
            seen_cards.add(key)
            try:
                text = card.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                problems.append("{}: 读不动（{}）".format(card, e))
                continue
            if not HAS_MATERIALS.search(text):
                continue                # 不供料的卡连解析都省了
            try:
                doc = yaml.safe_load(text)
            except Exception as e:
                problems.append("{}: YAML 解析不动（{}）".format(card, str(e).splitlines()[0][:160]))
                continue
            if not isinstance(doc, dict) or "materials" not in doc:
                continue
            cid = doc.get("id")
            comp = cid.strip() if isinstance(cid, str) and cid.strip() else d.name
            if not COMP_ID.match(comp):
                problems.append("{}: 组件名 {!r} 不合规，这张卡的料不进登记表".format(card, comp))
                continue
            if comp in comps:
                problems.append("{}: 组件名 {} 和 {} 撞了，这张卡的料不进登记表".format(
                    card, comp, comps[comp]))
                continue
            comps[comp] = card
            mats = doc.get("materials")
            if not isinstance(mats, list):
                problems.append("{}: materials 必须是列表".format(comp))
                continue
            ids = set()
            for m in mats:
                src = _check_port(m, d, comp, ids, problems)
                if src:
                    sources.append(src)
    return {"enabled": True, "sources": sources, "problems": problems}


def public(src):
    """对外的 path 给声明所在的卡（修声明要去的地方）；口全名 <组件>/<口> 只在内部和 chains、via 里用。"""
    out = {k: src[k] for k in PUBLIC_KEYS}
    out["path"] = src["card"]
    return out


# ── 建链：只凭声明 ──────────────────────────────────────────────

def _slots(reg, problems=None):
    """→ {kind: {"canon": 口, "derived": 口}}。v0 每格只坐一个口：先扫到的进链，多出来的报 problems。"""
    slots = {}
    for s in reg.get("sources") or []:
        for k in s["kinds"]:
            slot = slots.setdefault(k, {"canon": None, "derived": None})
            if slot[s["tier"]] is None:
                slot[s["tier"]] = s
            elif problems is not None:
                name = "正本" if s["tier"] == "canon" else "派生"
                problems.append("kind={} 已有{}口 {}，{} 多出来了，不进链（v0 每个 kind 至多一个{}口）".format(
                    k, name, slot[s["tier"]]["path"], s["path"], name))
    return {k: slots[k] for k in KINDS if k in slots}


def chains(reg, problems=None):
    """按规范第 6 节给每个 kind 排委派链，正本在前。只凭声明，不跑任何读数。
    多出来的口记进传入的 problems 列表（不传就不记）。"""
    return {k: [s["path"] for s in (slot["canon"], slot["derived"]) if s]
            for k, slot in _slots(reg, problems).items()}


# ── 跑读数 ──────────────────────────────────────────────────────

def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _run(src, timeout):
    env = dict(os.environ)
    # 红线：读数口用不着模型，厂商 key 一律不往下传
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    # Windows 管道默认按 GBK 编码，产出方吐中文不钉死就乱码；两头都钉成 UTF-8
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, *src["read"]], cwd=src["dir"], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError("退出码 {}：{}".format(
            proc.returncode, (proc.stderr or "").strip()[-300:] or "（stderr 是空的）"))
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise ValueError("stdout 不是一份 JSON：{}".format((proc.stdout or "").strip()[:80]))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("stdout 不是带 items 列表的 JSON 对象")
    return data


def _read(src, timeout):
    """→ (读数, 是否走缓存, 错误)。只缓存成功的读数：失败了下次还得再试，不能把坏结果捂 ttl 秒。"""
    key = (src["path"], tuple(src["read"]), src["dir"])
    with _lock(key):
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < src["ttl"]:
            return hit[1], True, None
        try:
            data = _run(src, timeout)
        except subprocess.TimeoutExpired:
            return None, False, "超过 {} 秒没跑完".format(timeout)
        except Exception as e:
            return None, False, str(e)[:400]
        _cache[key] = (time.time(), data)
        return data, False, None


def _check_items(src, raw):
    """逐条校验最小形状（规范第 5 节）。不合格的丢掉，按原因归总进 problems——丢了多少看得见。"""
    good, seen, foreign = [], set(), []
    tally = {}                  # 说法 -> [件数, 例]
    unknown = set()

    def note(msg, ex):
        t = tally.setdefault(msg, [0, ex])
        t[0] += 1

    for n, it in enumerate(raw, 1):
        if not isinstance(it, dict):
            note("丢了：不是对象", "#{}".format(n))
            continue
        iid, k = it.get("id"), it.get("kind")
        ex = iid if isinstance(iid, str) and iid else "#{}".format(n)
        m = ITEM_ID.match(iid) if isinstance(iid, str) else None
        if not m:
            note("丢了：id 不是 <kind>:<16 位小写十六进制>", ex)
            continue
        if k not in KINDS:
            note("丢了：kind 不在 v0 词表", ex)
            continue
        if m.group(1) != k:
            note("丢了：id 前缀和 kind 对不上", ex)
            continue
        if k not in src["kinds"]:
            foreign.append((k, iid))
            continue
        seen.add(iid)
        miss = [f for f in ("text", "ptr") if not isinstance(it.get(f), str) or not it[f].strip()]
        if miss:
            note("丢了：{} 缺或为空".format("、".join(miss)), ex)
            continue
        if "ts" not in it:
            note("丢了：缺 ts 键（值可以是 null，键不能没有）", ex)
            continue
        if it["ts"] is not None and not isinstance(it["ts"], str):
            note("丢了：ts 不是字符串也不是 null", ex)
            continue
        if any(g["id"] == iid for g in good):
            note("丢了：同一个口里 id 重复，留第一条", ex)
            continue
        item = {f: it[f] for f in FIELDS if f in it}
        unknown.update(f for f in it if f not in FIELDS)
        for f, t in OPTIONAL_TYPES.items():
            v = item.get(f)
            if v is not None and (not isinstance(v, t) or (t is int and isinstance(v, bool))):
                item.pop(f)
                note("{} 类型不对，这个字段丢掉、条目照留".format(f), ex)
        if len(item["text"]) > TEXT_MAX:
            note("text 超 {} 字，照留不截".format(TEXT_MAX), ex)
        good.append(item)

    probs = ["{}: {}（{} 条，例：{}）".format(src["path"], msg, c, ex) for msg, (c, ex) in tally.items()]
    if unknown:
        probs.append("{}: 条目带了约定外的字段 {}，炉子不认（附注请放进 extra）".format(
            src["path"], "、".join(sorted(unknown))))
    return {"good": good, "ids": {g["id"] for g in good}, "seen": seen,
            "foreign": foreign, "problems": probs}


def _merge(ci, di):
    """逐字段先问正本：正本给了（键在、值不是 null）用正本的，没给才用派生的。"""
    out = {}
    for f in FIELDS:
        if ci is not None and ci.get(f) is not None:
            out[f] = ci[f]
        elif di is not None and f in di:
            out[f] = di[f]
        elif ci is not None and f in ci:
            out[f] = ci[f]
    return out


def load(kind=None, min_count=None, cfg=None):
    """只跑链上的口：正本先、派生后，一个口只跑一次；按 id 合并，判四种状态。

    → {enabled, asof, items, notes, problems}。cfg 是 intake 段（不传就读 config.json）。
    """
    st = settings(cfg)
    reg = scan(st["roots"])
    problems = list(reg["problems"])
    out = {"enabled": reg["enabled"], "asof": _now(), "items": [], "notes": [], "problems": problems}
    if kind is not None and kind not in KINDS:
        problems.append("kind={} 不在 v0 词表 {}".format(kind, "/".join(KINDS)))
        return out
    if min_count is not None and (isinstance(min_count, bool) or not isinstance(min_count, int)
                                  or min_count < 0):
        problems.append("min_count 要是非负整数，收到 {!r}".format(min_count))
        return out

    slots = _slots(reg, problems)
    if kind is not None and kind not in slots and reg["enabled"]:
        problems.append("没有哪个口声明了 kind={}".format(kind))
    wanted = [kind] if kind else list(slots)

    order = []
    for tier in TIERS:
        for k in wanted:
            s = (slots.get(k) or {}).get(tier)
            if s and s not in order:
                order.append(s)

    checked = {}                # 口全名 -> _check_items 结果；没读成的口不在里面
    for s in order:
        data, cached, err = _read(s, st["timeout"])
        if err:
            problems.append("{}: 没读成——{}".format(s["path"], err))
            continue
        # note 是这份读数的限定语（截了多少、口径差在哪），规范要求炉子必须显示
        out["notes"].append({"via": s["path"], "note": data.get("note"),
                             "asof": data.get("asof"), "cached": cached})
        checked[s["path"]] = c = _check_items(s, data["items"])
        problems.extend(c["problems"])

    # 口吐出它没声明的 kind：一律丢。撞上那个 kind 正本口里的同 id，就是一份料有了两个正本
    for s in order:
        c = checked.get(s["path"])
        if not c or not c["foreign"]:
            continue
        by_kind = {}
        for k, iid in c["foreign"]:
            by_kind.setdefault(k, []).append(iid)
        for k, ids in by_kind.items():
            msg = "{}: 吐出它没声明的 kind={} 条目 {} 条，已丢（例：{}）".format(
                s["path"], k, len(ids), ids[0])
            canon = (slots.get(k) or {}).get("canon")
            other = checked.get(canon["path"]) if canon and canon is not s else None
            clash = [i for i in ids if other and i in other["seen"]]
            if clash:
                msg += "；其中 {} 条的 id 也在 kind={} 的正本口 {} 里——一份料只能有一个正本，" \
                       "是 {} 的声明错了（例：{}）".format(len(clash), k, canon["path"], s["path"], clash[0])
            problems.append(msg)

    for k in wanted:
        slot = slots.get(k)
        if not slot:
            continue
        c, d = slot["canon"], slot["derived"]
        cc = checked.get(c["path"]) if c else None
        dc = checked.get(d["path"]) if d else None
        c_items = {it["id"]: it for it in cc["good"] if it["kind"] == k} if cc else {}
        d_items = {it["id"]: it for it in dc["good"] if it["kind"] == k} if dc else {}
        for iid in list(c_items) + [i for i in d_items if i not in c_items]:
            ci, di = c_items.get(iid), d_items.get(iid)
            item = _merge(ci, di)
            if c is None:
                state = "alone"
            elif cc is None or (ci is None and iid in cc["seen"]):
                # 正本口没读成，或正本里这条形状坏了被丢：都是「没核过」，不许判 stale
                state = "unverified"
            elif ci is not None:
                state = "ok"
            else:
                state = "stale"
            item["state"] = state
            item["via"] = {"canon": c["path"] if ci else None, "derived": d["path"] if di else None}
            if min_count is not None and (item.get("count") or 0) < min_count:
                continue
            out["items"].append(item)
    return out


# ── 接口形状（main.py 和 CLI 共用） ─────────────────────────────

def registry_payload(cfg=None):
    st = settings(cfg)
    reg = scan(st["roots"])
    problems = list(reg["problems"])
    ch = chains(reg, problems)
    return {"ok": reg["enabled"], "api": API, "sources": [public(s) for s in reg["sources"]],
            "chains": ch, "problems": problems}


def materials_payload(cfg=None, kind=None, min_count=None):
    st = settings(cfg)
    got = load(kind, min_count, cfg=st)
    return {"ok": got["enabled"], "api": API, "asof": got["asof"],
            "kind": kind, "min_count": min_count,
            # 只回显，不当 min_count 的缺省：留给料仓页面标「候选」
            "candidate_min": st["candidate_min"],
            "items": got["items"], "notes": got["notes"], "problems": got["problems"]}


# ── 自测（临时目录造假卡，不碰真实数据） ────────────────────────

# 假读数口：照 fixtures.json 里这个口的剧本吐数、报错、超时或吐非 JSON；每跑一次记一行 runs.log。
# 故意不 reconfigure stdout、json 不转义中文——管道编码没钉死，这里的中文就会乱
FAKE_SCRIPT = '''import json, os, sys, time, pathlib
here = pathlib.Path(__file__).parent
port = sys.argv[1]
with open(here / "runs.log", "a", encoding="utf-8") as f:
    f.write(port + "\\n")
spec = json.loads((here / "fixtures.json").read_text(encoding="utf-8"))[port]
if spec.get("sleep"):
    time.sleep(spec["sleep"])
if spec.get("exit"):
    sys.stderr.write("故意报错\\n")
    sys.exit(spec["exit"])
if "raw" in spec:
    print(spec["raw"])
    sys.exit(0)
out = spec["out"]
if spec.get("echo_encoding"):
    out["note"] = "PYTHONIOENCODING={}；stdout={}".format(os.environ.get("PYTHONIOENCODING"), sys.stdout.encoding)
print(json.dumps(out, ensure_ascii=False))
'''


def selftest():
    print("[intake] selftest 开始 —— 全程临时目录，不读真实数据")
    tmp = Path(tempfile.mkdtemp(prefix="intake_selftest_"))
    ok = True

    def check(cond, name):
        nonlocal ok
        if not cond:
            ok = False
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))

    def hid(kind, s):
        return "{}:{}".format(kind, hashlib.sha1(s.encode("utf-8")).hexdigest()[:16])

    def item(kind, s, text, **kw):
        it = {"id": hid(kind, s), "kind": kind, "text": text,
              "ptr": "D:/fake/{}.md#L1".format(s), "ts": None}
        it.update(kw)
        return it

    def comp(root, rel, card, fixtures=None):
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "ASSET.yml").write_text(card, encoding="utf-8")
        if fixtures is not None:
            (d / "mat.py").write_text(FAKE_SCRIPT, encoding="utf-8")
            (d / "fixtures.json").write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
        return d

    def runs(d):
        f = d / "runs.log"
        return f.read_text(encoding="utf-8").split() if f.exists() else []

    def has(problems, *words):
        return any(all(w in p for w in words) for p in problems)

    try:
        _yaml()
    except ImportError:
        check(False, "PyYAML 在场（其余用例要读卡，没装就只能测降级）")
        shutil.rmtree(tmp, ignore_errors=True)
        print("[intake] selftest 有 FAIL")
        return False

    try:
        # ── A 主场：四种状态 / 字段级合并 / 坏声明 / 扫描口径 / 缓存 / note 透传 / 中文 ──
        ra = tmp / "rootA"
        D1, D2, D3, D5 = (hid("decision", s) for s in ("d1", "d2", "d3", "d5"))
        P1, I1, I2, K1 = hid("profile", "p1"), hid("insight", "i1"), hid("insight", "i2"), hid("kg", "k1")
        canon_d1 = item("decision", "d1", "正本的话：先问正本再问派生", quote="他的原话",
                        ts="2026-10-01", count=None, extra={"mark": "他过目"})
        ledger = comp(ra, "ledger-dir", """id: ledger
materials:
  - id: decisions
    title: 拍板账
    kind: decision
    tier: canon
    read: [mat.py, decisions]
  - id: profile
    title: 画像候选
    kind: profile
    tier: canon
    read: [mat.py, profile]
""", {
            "decisions": {"out": {"note": "正本口：全量，没截", "asof": "2026-10-15T10:00:00", "items": [
                canon_d1,
                item("decision", "d2", "只在正本里的一条", ts="2026-10-02"),
                {"id": D5, "kind": "decision", "text": "正本这条缺 ptr", "ts": None},
            ]}},
            "profile": {"exit": 1},
        })
        hits = comp(ra, "nest/hits", """materials:
  - id: judgment-hits
    title: 被撞回的旧判断
    kind: [decision, profile, insight]
    tier: derived
    read: [mat.py, hits]
    ttl: 900
""", {
            "hits": {"out": {"note": "派生口：画像非 member 行丢了 2 条", "asof": "2026-10-15T09:00:00", "items": [
                item("decision", "d1", "派生的话", ts="2026-09-30", count=5,
                     seen=[{"sid": "s1", "date": "2026-10-03"}, {"sid": "s2", "date": "2026-10-04"}],
                     extra={"same_day": 1}),
                item("decision", "d3", "正本里已经没有这条了", count=3),
                item("decision", "d5", "派生里好好的", count=2),
                item("profile", "p1", "画像候选一条", count=4),
                item("insight", "i1", "提炼候选一条", count=1),
                item("insight", "i2", "长" * 300, count=0),
                {"id": "decision:XYZ", "kind": "decision", "text": "id 坏了", "ptr": "x", "ts": None},
                {"id": hid("insight", "i3"), "kind": "insight", "text": "缺 ptr", "ts": None},
                {"id": hid("insight", "i4"), "kind": "insight", "text": "缺 ts 键", "ptr": "x"},
            ]}},
        })
        comp(ra, ".", """id: rootcomp
materials:
  - id: kg
    kind: kg
    tier: canon
    read: [mat.py, kg]
""", {"kg": {"echo_encoding": True, "out": {"items": [item("kg", "k1", "根目录本身的卡也扫到")]}}})
        comp(ra, "badread", """id: badread
materials:
  - id: escape
    kind: decision
    tier: canon
    read: [../ledger-dir/mat.py, decisions]
  - id: weird
    kind: weird
    tier: derived
    read: [mat.py, x]
  - id: listcanon
    kind: [decision, profile]
    tier: canon
    read: [mat.py, x]
  - id: Bad_ID
    kind: decision
    tier: canon
    read: [mat.py, x]
  - id: shell
    kind: insight
    tier: derived
    read: [cmd.exe, /c, echo]
""", {})
        hidden_card = """id: {}
materials:
  - id: x
    kind: kg
    tier: derived
    read: [mat.py, x]
"""
        comp(ra, ".hidden", hidden_card.format("hidden"), {})
        comp(ra, "_private", hidden_card.format("private"), {})
        comp(ra, "node_modules/pkg", hidden_card.format("npm"), {})
        comp(ra, "a/b/c", hidden_card.format("deep3"), {})
        comp(ra, "nomat", "id: nomat\nidentity: 不供料的卡\n")

        reg = scan([str(ra)])
        names = {s["path"] for s in reg["sources"]}
        check(names == {"ledger/decisions", "ledger/profile", "hits/judgment-hits", "rootcomp/kg"},
              "扫描口径：根目录本身 + 两层；卡里有 id 用 id、没有用目录名")
        check(not any(s["component"] in ("hidden", "private", "npm", "deep3") for s in reg["sources"]),
              "跳过 . / _ 开头、node_modules、第三层")
        check(has(reg["problems"], "badread/escape", "组件目录里的 .py"), "坏声明：read 跑出组件目录 → problems")
        check(has(reg["problems"], "badread/weird", "词表"), "坏声明：kind 不在词表 → problems")
        check(has(reg["problems"], "badread/listcanon", "正本口只许写一个"), "坏声明：正本口写一组 kind → problems")
        check(has(reg["problems"], "Bad_ID", "口 id 不合规"), "坏声明：口 id 大写/下划线 → problems")
        check(has(reg["problems"], "badread/shell", ".py"), "坏声明：read 指壳子不是 .py → problems")
        check(not any("nomat" in p for p in reg["problems"]), "不带 materials 的卡不进 problems")
        ch = chains(reg)
        check(ch == {"decision": ["ledger/decisions", "hits/judgment-hits"],
                     "profile": ["ledger/profile", "hits/judgment-hits"],
                     "insight": ["hits/judgment-hits"], "kg": ["rootcomp/kg"]},
              "建链只凭声明：正本在前、派生在后")
        check(runs(ledger) == [] and runs(hits) == [], "scan / chains 不跑任何读数")

        cfg_a = {"roots": [str(ra)], "timeout": 10}
        got = load(cfg=cfg_a)
        by = {it["id"]: it for it in got["items"]}
        d1 = by.get(D1) or {}
        check(d1.get("state") == "ok" and d1.get("via") == {"canon": "ledger/decisions",
                                                             "derived": "hits/judgment-hits"},
              "ok：正本里有，via 两头都在")
        check(d1.get("text") == canon_d1["text"], "合并：text 正本赢（中文经管道不乱码）")
        check(d1.get("ts") == "2026-10-01" and d1.get("quote") == "他的原话"
              and d1.get("extra") == {"mark": "他过目"}, "合并：ts / quote / extra 正本赢")
        check(d1.get("count") == 5 and len(d1.get("seen") or []) == 2,
              "合并：正本 count 是 null，派生的 count / seen 补上")
        check((by.get(D2) or {}).get("state") == "ok" and by[D2]["via"]["derived"] is None
              and "count" not in by[D2], "ok：只在正本里，via.derived 为 null")
        check((by.get(D3) or {}).get("state") == "stale" and by[D3]["via"]["canon"] is None,
              "stale：正本读成了但没有这个 id")
        check((by.get(D5) or {}).get("state") == "unverified",
              "正本里这条形状坏了被丢：判 unverified 不判 stale")
        check((by.get(P1) or {}).get("state") == "unverified", "unverified：正本口报错，派生的料一律没核过")
        check(not any(it["state"] == "stale" for it in got["items"] if it["kind"] == "profile"),
              "正本口坏了，不许把派生的料判 stale")
        check((by.get(I1) or {}).get("state") == "alone", "alone：kind 没声明正本口")
        check((by.get(K1) or {}).get("state") == "ok", "根目录本身的卡读得到")
        check(len((by.get(I2) or {}).get("text", "")) == 300 and has(got["problems"], "超 240 字"),
              "text 超 240 字：照留，problems 里看得见")
        check("decision:XYZ" not in by and has(got["problems"], "hits/judgment-hits", "id 不是"),
              "id 格式坏：丢掉 + problems")
        check(hid("insight", "i3") not in by and hid("insight", "i4") not in by
              and has(got["problems"], "ptr 缺或为空") and has(got["problems"], "缺 ts 键"),
              "缺 ptr / 缺 ts 键：丢掉 + problems")
        check(has(got["problems"], "ledger/profile", "没读成", "退出码 1"), "正本口报错进 problems")
        notes = {n["via"]: n for n in got["notes"]}
        check(notes.get("ledger/decisions", {}).get("note") == "正本口：全量，没截"
              and notes.get("hits/judgment-hits", {}).get("asof") == "2026-10-15T09:00:00",
              "note / asof 透传")
        # 本机系统代码页若已是 UTF-8，光比中文测不出漏钉编码；直接看读数口收到的环境和 stdout 编码
        check((notes.get("rootcomp/kg") or {}).get("note") == "PYTHONIOENCODING=utf-8；stdout=utf-8",
              "读数口子进程的 stdout 钉成 UTF-8（PYTHONIOENCODING）")
        check(runs(hits) == ["hits"], "一个派生口服务三个 kind，只跑一次")
        load(cfg=cfg_a)
        check(runs(hits) == ["hits"] and runs(ledger).count("decisions") == 1, "ttl 内第二次走缓存")
        check(runs(ledger).count("profile") == 2, "失败的读数不缓存，下次再试")
        mc = load(min_count=3, cfg=cfg_a)
        check({it["id"] for it in mc["items"]} == {D1, D3, P1}, "min_count=3：没有 count 的当 0")
        ins = load(kind="insight", cfg=cfg_a)
        check({it["kind"] for it in ins["items"]} == {"insight"} and len(ins["items"]) == 2,
              "kind 过滤")
        check(has(load(kind="bogus", cfg=cfg_a)["problems"], "词表"), "kind 不在词表 → problems")

        # ── B 三条 v0 限制 ──
        rb = tmp / "rootB"
        X, P = hid("decision", "x"), hid("profile", "p")
        c1 = comp(rb, "c1", """id: c1
materials:
  - id: decisions
    kind: decision
    tier: canon
    read: [mat.py, decisions]
  - id: profile
    kind: profile
    tier: canon
    read: [mat.py, profile]
  - id: hits
    kind: [decision]
    tier: derived
    read: [mat.py, hits]
""", {
            "decisions": {"out": {"items": [item("decision", "x", "一份料")]}},
            "profile": {"out": {"items": [item("profile", "p", "画像"),
                                          item("decision", "x", "同一份料又从画像口冒出来")]}},
            "hits": {"out": {"items": [item("decision", "x", "派生", count=2)]}},
        })
        c2 = comp(rb, "c2", """id: c2
materials:
  - id: decisions
    kind: decision
    tier: canon
    read: [mat.py, decisions]
  - id: hits
    kind: decision
    tier: derived
    read: [mat.py, hits]
""", {"decisions": {"out": {"items": []}}, "hits": {"out": {"items": []}}})
        regb = scan([str(rb)])
        pb = []
        chb = chains(regb, pb)
        check(chb.get("decision") == ["c1/decisions", "c1/hits"] and chb.get("profile") == ["c1/profile"],
              "限制：每 kind 一正本一派生，先扫到的进链")
        check(has(pb, "c2/decisions", "不进链"), "限制：同 kind 两个正本口 → 多的进 problems")
        check(has(pb, "c2/hits", "不进链"), "限制：同 kind 两个派生口 → 多的进 problems")
        gb = load(cfg={"roots": [str(rb)], "timeout": 10})
        check(has(gb["problems"], "c1/profile", "kind=decision", "c1/decisions", "只能有一个正本"),
              "限制：正本口吐声明外 kind 撞同 id → problems 点名两个口")
        check([it["id"] for it in gb["items"] if it["kind"] == "profile"] == [P],
              "声明外 kind 的条目丢掉")
        xs = [it for it in gb["items"] if it["id"] == X]
        check(len(xs) == 1 and xs[0]["state"] == "ok" and xs[0]["count"] == 2, "同 id 只出一条，正常合并")
        check(runs(c2) == [], "不进链的口一次都不跑")
        check(sorted(runs(c1)) == ["decisions", "hits", "profile"], "进链的口各跑一次")

        # ── C 正本口超时 / 吐非 JSON → unverified ──
        rc = tmp / "rootC"
        comp(rc, "t", """id: t
materials:
  - id: decisions
    kind: decision
    tier: canon
    read: [mat.py, slow]
  - id: profile
    kind: profile
    tier: canon
    read: [mat.py, garbage]
  - id: hits
    kind: [decision, profile]
    tier: derived
    read: [mat.py, hits]
""", {"slow": {"sleep": 4, "out": {"items": []}},
      "garbage": {"raw": "这不是 JSON"},
      "hits": {"out": {"items": [item("decision", "y", "超时那边的派生", count=3),
                                 item("profile", "q", "非 JSON 那边的派生", count=1)]}}})
        gc = load(cfg={"roots": [str(rc)], "timeout": 1})
        check(len(gc["items"]) == 2 and {it["state"] for it in gc["items"]} == {"unverified"},
              "unverified：正本口超时 / 吐非 JSON，派生的料不判 stale")
        check(has(gc["problems"], "t/decisions", "超过 1 秒"), "超时进 problems")
        check(has(gc["problems"], "t/profile", "不是一份 JSON"), "非 JSON 进 problems")

        # ── D 接口形状 / 空 roots / 没装 PyYAML ──
        rp = registry_payload({"roots": [str(ra)]})
        check(rp["ok"] and rp["api"] == API and rp["chains"] == ch
              and all(set(s) == set(PUBLIC_KEYS) for s in rp["sources"]),
              "registry 接口：ok / api / chains，sources 不暴露 read / dir")
        mp = materials_payload({"roots": [str(ra)], "candidate_min": 3}, kind="decision")
        check(mp["ok"] and mp["candidate_min"] == 3 and mp["notes"] and mp["asof"],
              "materials 接口：回显 candidate_min，带 notes / asof")
        er = registry_payload({"roots": []})
        check(er["ok"] and er["sources"] == [] and has(er["problems"], "roots 为空"),
              "roots 为空：不扫，problems 说一句")
        orig = globals()["_yaml"]

        def no_yaml():
            raise ImportError("模拟没装 PyYAML")

        globals()["_yaml"] = no_yaml
        try:
            r0 = scan([str(ra)])
            m0 = materials_payload({"roots": [str(ra)]})
            check(not r0["enabled"] and r0["sources"] == [] and has(r0["problems"], "没装 PyYAML"),
                  "没装 PyYAML：进料关闭，problems 说一句")
            check(m0["ok"] is False and m0["items"] == [], "没装 PyYAML：materials 不炸、空料")
        finally:
            globals()["_yaml"] = orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _cache.clear()

    print("[intake] selftest {}".format("全绿" if ok else "有 FAIL"))
    return ok


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="进料扫描器 · 炉子的进料口（只读声明，用到才跑读数）")
    ap.add_argument("cmd", nargs="?", choices=("registry", "materials"))
    ap.add_argument("--kind", default=None, help="只要这一种料：" + " / ".join(KINDS))
    ap.add_argument("--min-count", type=int, default=None, help="只留 count >= N 的（没有 count 的当 0）")
    ap.add_argument("--selftest", action="store_true", help="临时目录造假卡自测，不碰真实数据")
    args = ap.parse_args(argv)
    if args.selftest:
        return 0 if selftest() else 1
    if args.cmd == "registry":
        out = registry_payload()
    elif args.cmd == "materials":
        out = materials_payload(kind=args.kind, min_count=args.min_count)
    else:
        ap.print_help()
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
