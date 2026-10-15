"""炼料炉 Smelt —— 料 → 铁。

料仓里的料是别处攒好的判断（拍板、画像候选、提炼候选、kg 段），但还不是铁：
text 是 AI 写的概括、quote 常常太长或只有一句「A」、kind 只是出处。
炼料 = 一批料调一次 claude，每条料至多出一块铁；锚由程序逐字核，核不过就去会话里补原话，
补不出就坐「他认」座位。

设计正本：docs/INTAKE-SPEC.md 第 14 节。main.py import 本模块，本模块不 import main：
调用函数、铁目录、剑目录、状态文件都当参数传进来，免得循环引用。

跑法：
  python smelt.py segments <料 id>   # 找源会话、挑段，不调模型
  python smelt.py --selftest         # 临时目录 + 桩，不碰真实数据、不调 claude
"""
import argparse
import datetime
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import heroes
import intake

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SMELT_TEMPLATE = ROOT / "forge_prompts" / "smelt.md"

KINDS = ("判断", "经验", "流程")        # 同 main.KINDS / GRADES：炼出的铁和反思炉的铁同一堆
GRADES = ("上品", "中品", "下品")
SEAT = "他认"
# 用 fullmatch：intake.ITEM_ID 的 $ 会放过尾巴上的一个换行
MATERIAL_ID = re.compile(r"[a-z]+:[0-9a-f]{16}")
KIND_LABEL = {"decision": "拍板", "profile": "画像候选", "insight": "提炼候选", "kg": "kg 段"}
STATE_WHY = {"stale": "正本里找不到了：改过字，或者被收割走了",
             "unverified": "正本没读到，没核过"}

ANCHOR_MAX = 30
TEXT_MAX = 40            # 铁的一句话（第 14.3 节第 2 条）
ANCHOR_MIN = 6           # 第 14.3 节第 3 条：6 是 ④
SEGMENT_LIMIT = 5        # 第 14.4 节第 4 条：≤5 段是 ④
SEGMENT_MAX = 200
# 挑段的最低分（④）：锚只核逐字、不核相关，一两分的不相干段进了提示词，模型硬截一句也能过核
SEGMENT_MIN_SCORE = 3
CONTEXT_MAX = 80         # 每段带的前文最多这么多字，只给模型判话题，不许从里面截锚
SESSION_CAP = 80         # ts 那天开的会话最多读这么多场（④）
SMELT_TIMEOUT = 240


class SmeltError(Exception):
    """同 main.ForgeError 的形状（stage / message / detail），main 那边原样转成 ForgeError。"""

    def __init__(self, stage, message, detail=""):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.detail = (detail or "")[:600]


# ── 锚核验 ────────────────────────────────────────────────────────

# 点头只证明他点了头，不算原话锚（拍板账 10-15「补原话」那行）。
# 长的先删：先删「推荐」会把「按照你的推荐」删成「按照你的」，就认不出是点头了
NODS = sorted(("A B C D 甲 乙 丙 丁 可以 行 好 好的 对 对的 是 是的 嗯 嗯嗯 同意 我同意 按推荐 按照推荐 "
               "按你的推荐 按照你的推荐 按你推荐 按照你推荐 推荐 选 就这样 那就 我觉得 "
               "吧 啊 呀 哈 呢 了 ok OK 没问题 没意见 也行 都行 行吧 收到 好嘞 听你的 "
               "按你说的 按照你说的 你说的 就按 就照 按照 照着 来 就 这么办 这么定 这么做 定了 "
               "我们 咱们 继续 下一步").split(), key=len, reverse=True)
# 删完点头的词，剩下不到这么多字也算点头：「就按你的推荐来吧」「好的，我们就这么办」这类
# 句子里除了点头只剩虚字，词表列不全（10-15 Codex 复核实测放过了三句）
NOD_REST = 3
_QUOTES = "「」『』“”‘’\"'"
_ELLIPSIS = ("……", "…", "...")
_NOT_WORD = re.compile(r"[\W_]+")
_SPACE = re.compile(r"\s+")


def _trim(s):
    return (s or "").strip().strip(_QUOTES).strip() if isinstance(s, str) else ""


def _is_nod(bare):
    for f in NODS:
        bare = bare.replace(f, "")
    return len(bare) < NOD_REST


def check_anchor(anchor, sources):
    """sources = [(来源标记, 文本)] → (通过?, 原因, 命中的来源标记)。"""
    a = _trim(anchor)
    if not a:
        return False, "没给锚", None
    if any(e in a for e in _ELLIPSIS):
        return False, "锚里有省略号（拼接或省略过）", None
    if len(a) > ANCHOR_MAX:
        return False, "锚超过 {} 字".format(ANCHOR_MAX), None
    bare = _NOT_WORD.sub("", a)
    if len(bare) < ANCHOR_MIN:
        return False, "锚去掉标点和空白不足 {} 字".format(ANCHOR_MIN), None
    if _is_nod(bare):
        return False, "锚只是点头的话，点头不算原话", None
    for mark, text in sources:
        if text and a in text:
            return True, "逐字", mark
    # 他打字时随手加的空格、换行，模型截锚时常吞掉——去掉全部空白再比一次，字一个都不许差
    flat = _SPACE.sub("", a)
    for mark, text in sources:
        if text and flat in _SPACE.sub("", text):
            return True, "去掉空白后逐字", mark
    return False, "锚不在原话里（疑似编造或改了字）", None


def quote_usable(quote):
    """quote 能不能截出锚。60 多字的能用（模型从里面截一段），「A」不能用。"""
    bare = _NOT_WORD.sub("", _trim(quote))
    return len(bare) >= ANCHOR_MIN and not _is_nod(bare)


# ── 找源会话（第 14.4 节第 2 条） ──────────────────────────────────

_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
# 只翻会话那一层：claude 根下一层项目目录里的 *.jsonl（不进 <会话>/subagents/），codex 同 scrolls 的口径。
# 不调 scrolls：codex 的 scrolls 每份要读头 40 行认出身，按号找会话只用得着文件名
_PATTERNS = {"claude": "*/*.jsonl", "codex": "**/rollout-*.jsonl"}
_SID_IN_SRC = re.compile(r"会话\s*([0-9a-f][0-9a-f-]{7,35})")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ROLLOUT_DAY = re.compile(r"rollout-(\d{4}-\d{2}-\d{2})T")


def _rolls(cfg):
    """→ [(侠客, 路径, 会话 uuid)]。花名册同寻料页（heroes.enabled），根不在的跳过。"""
    out = []
    for h, root in heroes.enabled(cfg or {}):
        d = Path(root).expanduser()
        if not d.is_dir():
            continue
        pat = _PATTERNS.get(h.ID)
        try:
            files = sorted(d.glob(pat)) if pat else [Path(s["path"]) for s in h.scrolls(d)]
        except OSError:
            continue
        for f in files:
            m = _UUID.search(f.stem)
            if m:
                out.append((h, f, m.group(1)))
    return out


def _sid_hints(item):
    """extra 里带的会话号：画像的 sid（完整 uuid），拍板账「源」里的「会话 <号>」。"""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    hints = []
    sid = extra.get("sid")
    if isinstance(sid, str) and sid.strip():
        hints.append(sid.strip().lower())
    src = extra.get("源")
    if isinstance(src, str):
        for m in _SID_IN_SRC.finditer(src):
            n = m.group(1).strip("-")
            if not _DAY.fullmatch(n):       # 拍板账里有「会话 2026-10-09」：那是日子不是号
                hints.append(n)
    return list(dict.fromkeys(hints))


def _sid_match(hint, sid):
    """号的三种写法：8 位前缀、前 8 + 后 4 共 12 位、完整 uuid。"""
    h, u = hint.replace("-", ""), sid.replace("-", "")
    if len(h) < 8:
        return False
    if len(h) == 12:
        # 拍板账里 12 位的号全是前 8 + 后 4（8cfb7d9622d5 对 8cfb7d96-…-e8412b4a22d5）；
        # 再按「前 12 位」认，会把前 8 位撞上、尾号不对的别的会话一起拉进来
        return u.startswith(h[:8]) and u.endswith(h[-4:])
    return u.startswith(h)


def _opened_day(path, max_lines=50):
    """会话的开场日（本地时区）。codex 文件名里就是本地开场时刻；claude 取第一条带 timestamp 的记录。"""
    m = _ROLLOUT_DAY.match(path.name)
    if m:
        return m.group(1)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                if '"timestamp"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ts = ev.get("timestamp") if isinstance(ev, dict) else None
                if not isinstance(ts, str):
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if dt.tzinfo is not None:
                    dt = dt.astimezone()    # 留痕记的是 UTC，开场日按本机时区算
                return dt.date().isoformat()
    except OSError:
        pass
    return None


def _local_day(ts):
    """料的 ts → 本机日期。只给日期的照原样；带时区的换成本机时区（开场日也是按本机算的）。"""
    if not isinstance(ts, str):
        return ""
    if len(ts) > 10:
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone().date().isoformat()
        except ValueError:
            pass
    return ts[:10]


def find_sessions(item, cfg, cap=SESSION_CAP):
    """→ (用的哪一步, [(侠客, 路径)])。三步按顺序，前一步一场都没找着才走下一步（④）。"""
    rolls = _rolls(cfg)
    tried = []

    def said(step, got):
        return "；".join(tried + [step]), got

    hints = _sid_hints(item)
    if hints:
        got = [(h, p) for h, p, sid in rolls if any(_sid_match(n, sid) for n in hints)]
        if got:
            return said("① extra 会话号 {} → {} 场".format("、".join(hints), len(got)), got)
        tried.append("① extra 会话号 {} 在花名册的根里没找到".format("、".join(hints)))

    day = _local_day(item.get("ts"))
    if _DAY.fullmatch(day):
        start = datetime.datetime.strptime(day, "%Y-%m-%d").timestamp()
        cands = []
        for h, p, _sid in rolls:
            try:
                st = p.stat()
            except OSError:
                continue
            # 那天开的会话，最后一笔一定不早于那天零点：先按 mtime 筛掉大半，剩下的才读头几行
            if st.st_mtime >= start and _opened_day(p) == day:
                cands.append((st.st_size, h, p))
        # 超了留最大的：立判断的多是长谈，小卷宗多是一问一答的派活（④）
        cands.sort(key=lambda c: -c[0])
        got = [(h, p) for _size, h, p in cands[:cap]]
        if got:
            cut = "，共 {} 场只取 {} 场（截掉 {} 场）".format(len(cands), cap, len(cands) - cap) \
                if len(cands) > cap else ""
            return said("② ts 那天（{}）开场的会话 → {} 场{}".format(day, len(got), cut), got)
        tried.append("② ts 那天（{}）没有开场的会话".format(day))

    seen = [s["sid"] for s in (item.get("seen") or [])
            if isinstance(s, dict) and isinstance(s.get("sid"), str) and s["sid"].strip()]
    if seen:
        got = [(h, p) for h, p, sid in rolls if any(_sid_match(n, sid) for n in seen)]
        if got:
            return said("③ seen 里的 {} 场 → 找到 {} 场".format(len(seen), len(got)), got)
        tried.append("③ seen 里的 {} 场一场都没找到".format(len(seen)))
    return said("没找到源会话", [])


# ── 挑段（第 14.4 节第 4 条） ─────────────────────────────────────

_HAN = re.compile(r"[㐀-䶿一-鿿]+")
_PIECE = re.compile(r"[^。！？!?\n]+[。！？!?]*")


def _bigrams(s):
    out = set()
    for run in _HAN.findall(s if isinstance(s, str) else ""):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _pieces(text):
    for m in _PIECE.finditer(text):
        p = m.group(0).strip()
        while p:
            yield p[:SEGMENT_MAX]
            p = p[SEGMENT_MAX:].strip()


def _sid_of(path):
    m = _UUID.search(Path(path).stem)
    return m.group(1) if m else Path(path).stem


def segments(item, sessions, limit=SEGMENT_LIMIT):
    """他开口的话切成小段，按和料的 text（+ quote）的汉字二元组重叠数打分，取最高的 ≤limit 段。"""
    want = _bigrams(item.get("text")) | _bigrams(item.get("quote"))
    if not want:
        return []
    scored, seen = [], set()
    for h, p in sessions:
        sid = _sid_of(p)
        for line, text in h.user_turns(p):
            prev = ""
            for piece in _pieces(text):
                score = len(_bigrams(piece) & want)
                if piece not in seen and score >= SEGMENT_MIN_SCORE:
                    seen.add(piece)
                    # 前文给模型判「是不是同一件事」：一小句看不出话题，字面碰巧相像就会被截成锚
                    # （10-15 验收实测：「有没有必要存在」截自他问两个服务要不要合并的那场）
                    scored.append({"hero": h.ID, "sid": sid, "line": line,
                                   "text": piece, "score": score, "ctx": prev[-CONTEXT_MAX:]})
                prev = piece
    scored.sort(key=lambda s: -s["score"])      # 稳定排序：同分按会话、行号的先后
    return scored[:limit]


# ── 炼料（第 14.3、14.4 节） ──────────────────────────────────────

def _flat(s):
    return _SPACE.sub(" ", s or "").strip()


def _block(n, w):
    it = w["item"]
    lines = ["### 料 {}".format(n),
             "料 id：{}".format(it["id"]),
             "出处：{}（{}）".format(KIND_LABEL.get(it.get("kind"), it.get("kind")), it.get("kind")),
             "text（别的 AI 写的概括，不是原话）：{}".format(_flat(it.get("text")))]
    lines.append("原话：「{}」".format(it["quote"].strip()) if w["quote_ok"] else "原话：（没有能用的）")
    if w["segs"]:
        lines.append("补出的原话段：")
        lines += ["段{}{}：{}".format(i, "（前文：{}）".format(s["ctx"]) if s.get("ctx") else "", s["text"])
                  for i, s in enumerate(w["segs"], 1)]
    return "\n".join(lines)


def build_prompt(works):
    tpl = SMELT_TEMPLATE.read_text(encoding="utf-8")
    # 模板里有 JSON 花括号，用 replace 不用 format（同 main.build_reflect_prompt）。
    # count、seen 不给模型：撞回场数只排序，不进品级（第 14.5 节）
    return tpl.replace("{materials}", "\n\n".join(_block(i, w) for i, w in enumerate(works, 1)))


def parse_output(raw):
    """→ ({料 id: 候选}, [用不上的条目])。抠数组的手法同 main.parse_irons。"""
    text = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    if not text.startswith("["):
        a, b = text.find("["), text.rfind("]")
        if a < 0 or b < a:
            raise SmeltError("bad_output", "炼料炉没吐出 JSON 数组", raw)
        text = text[a:b + 1]
    try:
        items = json.loads(text)
    except Exception as e:
        raise SmeltError("bad_json", "炼料炉吐的不是合法 JSON：{}".format(e), raw)
    if not isinstance(items, list):
        raise SmeltError("bad_output", "炼料炉吐的不是数组", raw)
    got, junk = {}, []
    for it in items:
        mid = it.get("material") if isinstance(it, dict) else None
        if not isinstance(mid, str) or not isinstance(it.get("text"), str) or not it["text"].strip():
            junk.append({"reason": "缺 material 或 text", "raw": it})
        elif mid.strip() in got:
            junk.append({"reason": "同一条料吐了两块，只留第一块", "raw": it})
        else:
            got[mid.strip()] = it
    return got, junk


def _sources(w):
    out = []
    q = w["item"].get("quote")
    if isinstance(q, str) and q.strip():
        out.append(("quote", q))
    out += [({"hero": s["hero"], "sid": s["sid"], "line": s["line"]}, s["text"]) for s in w["segs"]]
    return out


def _used_ids(irons_dir):
    used = set()
    for f in Path(irons_dir).glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in (rec.get("irons") or []) if isinstance(rec, dict) else []:
            if isinstance(it, dict) and it.get("id"):
                used.add(it["id"])
    return used


def _shown(f):
    try:
        return f.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return f.as_posix()


_write_lock = threading.Lock()


def run(material_ids, *, cfg, call, irons_dir, now, log=None):
    """→ {irons, skipped, file}。cfg 是整份 config：intake 段给 intake.load，heroes / session_dir 给找会话。"""
    say = log or (lambda m: None)
    irons_dir = Path(irons_dir)
    cfg = cfg or {}
    # 走 intake.load 的 ttl 缓存，不另读文件；intake 段缺了就当一个根都没配，不去偷读 config.json
    pool = {it["id"]: it for it in intake.load(cfg=cfg.get("intake") or {})["items"]}

    skipped, works = [], {}
    for mid in dict.fromkeys(material_ids):
        it = pool.get(mid)
        if it is None:
            skipped.append({"id": mid, "why": "料仓里没有这条"})
        elif it.get("state") not in ("ok", "alone"):
            skipped.append({"id": mid, "why": STATE_WHY.get(it.get("state"),
                                                           "状态是 {}，不能炼".format(it.get("state")))})
        else:
            works[mid] = {"item": it, "quote_ok": quote_usable(it.get("quote")),
                          "segs": [], "step": None, "supplied": False}

    def supply(w):
        # 用到才补：quote 缺了、或核不过，才去会话里找。补出的原话只存在铁上，不回写正本（第 14.4 节第 5 条）
        step, sessions = find_sessions(w["item"], cfg)
        w["segs"] = segments(w["item"], sessions) if sessions else []
        w["step"], w["supplied"] = step, True
        say("补原话：{} — {}，挑出 {} 段".format(w["item"]["id"], step, len(w["segs"])))

    for w in works.values():
        if not w["quote_ok"]:
            supply(w)

    results, dropped, retry = {}, [], []
    if works:
        say("炼料炉开火：料 {} 条".format(len(works)))
        got, junk = parse_output(call(build_prompt(list(works.values())), timeout=SMELT_TIMEOUT))
        dropped += junk
        for mid, w in works.items():
            c = got.get(mid)
            if c is None:
                skipped.append({"id": mid, "why": "炉子没吐出这条"})
                continue
            ok, why, mark = check_anchor(c.get("anchor"), _sources(w))
            if ok:
                results[mid] = (c, mark)
                continue
            dropped.append({"material": mid, "reason": why, "raw": c})
            if not w["supplied"]:
                supply(w)
                if w["segs"]:
                    retry.append(mid)
                    continue
            results[mid] = (c, None)

    # 补了原话的那几条再烧一次，只重试这一轮（④）
    if retry:
        try:
            got, junk = parse_output(call(build_prompt([works[m] for m in retry]),
                                          timeout=SMELT_TIMEOUT))
        except Exception as e:
            # 头一轮已经出了铁，这一轮烧糊不该连累它们。这几条也不坐「他认」：
            # 他认说的是「原话补不出」，炉子烧糊是另一回事，进 skipped 让他再炼一次
            why = "补了原话后重炼没烧成（{}），这条没出铁，可再炼一次".format(getattr(e, "message", e))
            skipped += [{"id": m, "why": why} for m in retry]
            dropped.append({"reason": why, "materials": list(retry)})
            retry = []
            got, junk = {}, []
        dropped += junk
        for mid in retry:
            c = got.get(mid)
            if c is None:
                # 炉子这轮没交这条，原话其实补出来了（段在），不能当「补不出」去坐他认
                skipped.append({"id": mid, "why": "补了原话后重炼，炉子没吐出这条，可再炼一次"})
                continue
            ok, why, mark = check_anchor(c.get("anchor"), _sources(works[mid]))
            if not ok:
                dropped.append({"material": mid, "reason": "补原话后仍不过：" + why, "raw": c})
            results[mid] = (c, mark if ok else None)

    for mid in list(results):
        n = len(results[mid][0]["text"].strip())
        if n > TEXT_MAX:
            # 超长的一句话进了剑，判据就成了长段——同「料直接当铁」的毛病（第 14.3 节排除表）
            dropped.append({"material": mid, "reason": "一句话 {} 字，超 {} 字".format(n, TEXT_MAX),
                            "raw": results.pop(mid)[0]})
            skipped.append({"id": mid, "why": "炼出的一句话 {} 字，超 {} 字，可再炼一次".format(n, TEXT_MAX)})

    for mid, (c, mark) in results.items():
        if mark is None and works[mid]["step"]:
            dropped.append({"material": mid, "reason": "坐他认：{}，挑出 {} 段".format(
                works[mid]["step"], len(works[mid]["segs"]))})

    picked = now.strftime("%Y-%m-%d")
    with _write_lock:
        used = _used_ids(irons_dir)
        irons = []
        for mid, w in works.items():
            if mid not in results:
                continue
            c, mark = results[mid]
            prefix, n = mid.split(":", 1)[1][:8], 1
            while "{}-m{}".format(prefix, n) in used:
                n += 1
            iron = {"id": "{}-m{}".format(prefix, n), "text": c["text"].strip()}
            used.add(iron["id"])
            if mark is not None:
                iron["anchor"] = _trim(c.get("anchor"))
            iron["kind"] = c.get("kind") if c.get("kind") in KINDS else "经验"
            iron["grade"] = c.get("grade") if c.get("grade") in GRADES else "下品"
            it = w["item"]
            iron["from"] = {"material": mid, "ptr": it.get("ptr"),
                            "via": dict(it.get("via") or {}), "picked": picked}
            if mark is not None:
                iron["anchor_src"] = mark
            else:
                iron["seat"] = SEAT
            irons.append(iron)

        stamp = now.strftime("%Y%m%d-%H%M%S")
        f, k = irons_dir / "smelt-{}.json".format(stamp), 2
        while f.exists():           # 同一秒炼两回：别把上一回的铁盖掉
            f, k = irons_dir / "smelt-{}-{}.json".format(stamp, k), k + 1
        record = {"session_hash": f.stem, "source": "smelt", "path": "",
                  "created": now.strftime("%Y-%m-%d %H:%M"), "chars": 0,
                  "irons": irons, "dropped": dropped, "skipped": skipped}
        irons_dir.mkdir(parents=True, exist_ok=True)
        # 一块铁都没出也照落：勾了料、炉子说了什么，都得看得见
        f.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    say("炼料出铁 {} 块（他认 {} 块，跳过 {} 条）→ {}".format(
        len(irons), sum(1 for i in irons if i.get("seat") == SEAT), len(skipped), _shown(f)))
    return {"irons": irons, "skipped": skipped, "file": _shown(f)}


# ── 加工状态（第 14.6 节：能算的不存，只记「弃」） ──────────────────

UNSEEN, IN_FORGE, SWORDED, DISCARD = "未看", "已入炉", "已成剑", "弃"
_state_lock = threading.Lock()


def _read_state(state_file, log=None):
    f = Path(state_file)
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
        return data
    except Exception as e:
        if log:
            log("{} 读不动（{}），「弃」先当没记过".format(f.name, e))
        return {}


def forge_states(irons_dir, swords_dir, state_file, *, log=None):
    """→ {料 id: {state, irons, swords}}，只含有动静的料；没动静的用 forge_of 补「未看」。"""
    out = {}

    def slot(mid):
        return out.setdefault(mid, {"state": UNSEEN, "irons": [], "swords": [], "_discard": False})

    for f in sorted(Path(irons_dir).glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in (rec.get("irons") or []) if isinstance(rec, dict) else []:
            mid = (it.get("from") or {}).get("material") if isinstance(it, dict) else None
            if isinstance(mid, str) and it.get("id") not in slot(mid)["irons"]:
                slot(mid)["irons"].append(it.get("id"))
    sd = Path(swords_dir)
    for d in (sorted(sd.iterdir()) if sd.is_dir() else []):
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        for snap in (meta.get("irons") or []) if isinstance(meta, dict) else []:
            mid = (snap.get("from") or {}).get("material") if isinstance(snap, dict) else None
            if isinstance(mid, str) and meta.get("id") not in slot(mid)["swords"]:
                slot(mid)["swords"].append(meta.get("id"))
    for mid, rec in _read_state(state_file, log).items():
        if isinstance(rec, dict) and rec.get("state") == DISCARD:
            slot(mid)["_discard"] = True
    for s in out.values():
        # 算得出的事实压过手记的弃：已成剑 > 已入炉 > 弃 > 未看（④）
        s["state"] = (SWORDED if s["swords"] else IN_FORGE if s["irons"]
                      else DISCARD if s["_discard"] else UNSEEN)
        del s["_discard"]
    return out


def forge_of(states, mid):
    return states.get(mid) or {"state": UNSEEN, "irons": [], "swords": []}


def toggle_discard(mid, state_file, now, *, log=None):
    """弃 ↔ 撤销。→ "弃" 或 None（撤销就删那一键）。"""
    f = Path(state_file)
    with _state_lock:
        data = _read_state(f, log)
        if isinstance(data.get(mid), dict) and data[mid].get("state") == DISCARD:
            del data[mid]
            state = None
        else:
            at = now.isoformat(timespec="seconds") if hasattr(now, "isoformat") else str(now)
            data[mid] = {"state": DISCARD, "at": at}
            state = DISCARD
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)      # 先写临时件再换名：写到一半断掉，旧的「弃」还在
    return state


# ── CLI ──────────────────────────────────────────────────────────

def _config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print("[smelt] config.json 读不动（{}），按空配置跑".format(e), file=sys.stderr)
        return {}


def cli_segments(mid):
    cfg = _config()
    t0 = time.time()
    got = intake.load(cfg=cfg.get("intake") or {})
    it = next((x for x in got["items"] if x["id"] == mid), None)
    t1 = time.time()
    if it is None:
        print("料仓里没有这条：{}（读到 {} 条；problems：{}）".format(
            mid, len(got["items"]), got["problems"] or "无"))
        return 1
    q = it.get("quote")
    print("料：{}（{} · state={} · ts={}）".format(mid, it.get("kind"), it.get("state"), it.get("ts")))
    print("text：{}".format((it.get("text") or "")[:120]))
    print("quote：{}（{}）".format(repr(q[:60]) if isinstance(q, str) else "无",
                                  "能用" if quote_usable(q) else "不能用，要补"))
    step, sessions = find_sessions(it, cfg)
    t2 = time.time()
    segs = segments(it, sessions)
    t3 = time.time()
    print("用的哪一步：{}".format(step))
    print("找到 {} 场：".format(len(sessions)))
    for h, p in sessions[:12]:
        print("  {} {}".format(h.ID, p))
    if len(sessions) > 12:
        print("  …另 {} 场".format(len(sessions) - 12))
    print("挑出 {} 段：".format(len(segs)))
    for i, s in enumerate(segs, 1):
        print("  段{} 分{} {}·{}#L{}：{}".format(i, s["score"], s["hero"], s["sid"], s["line"], s["text"]))
    print("耗时：读料 {:.1f}s · 找会话 {:.1f}s · 挑段 {:.1f}s".format(t1 - t0, t2 - t1, t3 - t2))
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="炼料炉 · 料 → 铁（segments 只找段，不调模型）")
    ap.add_argument("cmd", nargs="?", choices=("segments",))
    ap.add_argument("material", nargs="?", help="料 id，形如 decision:<16 位十六进制>")
    ap.add_argument("--selftest", action="store_true", help="临时目录 + 桩自测，不碰真实数据、不调 claude")
    args = ap.parse_args(argv)
    if args.selftest:
        return 0 if selftest() else 1
    if args.cmd == "segments" and args.material:
        return cli_segments(args.material)
    ap.print_help()
    return 2


# ── 自测（临时目录 + 桩，不碰真实数据、不调 claude） ────────────────

def _jsonl(path, rows):
    """rows 里的 None 写成空行：行号要按物理行算，空行也占一行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(("" if r is None else json.dumps(r, ensure_ascii=False)) + "\n"
                            for r in rows), encoding="utf-8")
    return path


def _u(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _cx(role, *texts, kind="input_text"):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
                                                 "content": [{"type": kind, "text": t} for t in texts]}}


def selftest():
    import shutil
    import tempfile
    print("[smelt] selftest 开始 —— 临时目录 + 桩，不读真实数据、不调 claude")
    tmp = Path(tempfile.mkdtemp(prefix="smelt_selftest_"))
    ok = True

    def check(cond, name):
        nonlocal ok
        if not cond:
            ok = False
        print("  [{}] {}".format("PASS" if cond else "FAIL", name))

    try:
        # ── 10 演示守卫在前端（先跑：前端枝可能还没写完，FAIL 就如实 FAIL，不替它改 index.html）──
        page = ROOT / "index.html"
        html = page.read_text(encoding="utf-8") if page.exists() else ""
        check(bool(re.search(r"async function loadBin\(\)\s*\{\s*if \(state\.demo\) return;", html)),
              "10 前端：loadBin() 第一句是 if (state.demo) return;（演示模式不画料仓）")

        # ── 1 锚核验 ──
        quote = ("咱们可以按照传统开发的流程经验啥的。比如说需求分析一个chat专门做，"
                 "直接关联到设计方案。之后执行的话，看量来分chat，分subagent啥的")
        src = [("quote", quote)]
        r = check_anchor("需求分析一个chat专门做", src)
        check(r[0] and r[2] == "quote", "1 锚：逐字截自 quote 的一段 → 过，来源标 quote")
        check(check_anchor("「需求分析一个chat专门做」", src)[0], "1 锚：两头带「」照样认")
        check(not check_anchor("需求分析两个chat专门做", src)[0], "1 锚：改了一个字 → 拒")
        long_a = quote[:31]
        check(not check_anchor(long_a, src)[0] and "30" in check_anchor(long_a, src)[1],
              "1 锚：逐字但超过 30 字 → 拒")
        for e in ("需求分析……专门做", "需求分析…专门做", "需求分析...专门做"):
            check(not check_anchor(e, [("quote", e)])[0], "1 锚：含省略号 {} → 拒".format(e[4:-3]))
        check(not check_anchor("需求分析", src)[0], "1 锚：去掉标点不足 6 字 → 拒")
        nods = ["A", "可以", "按推荐", "可以按照你的推荐。我觉得。"]
        for n in nods:
            check(not check_anchor(n, [("quote", n)])[0], "1 锚：点头词「{}」→ 拒".format(n))
        for n in ("就按你的推荐来吧", "可以，按照你说的来", "好的，我们就这么办"):
            check(not check_anchor(n, [("quote", n)])[0] and not quote_usable(n),
                  "1 锚：点头句「{}」（除了点头只剩虚字）→ 拒，quote 也算不能用".format(n))
        check(check_anchor("可以先不做表达扫描", [("quote", "可以先不做表达扫描")])[0],
              "1 锚：带点头词但有实话的（可以先不做表达扫描）→ 过")
        check("点头" in check_anchor("可以按照你的推荐。我觉得。", [("quote", "可以按照你的推荐。我觉得。")])[1],
              "1 锚：「可以按照你的推荐。我觉得。」够 6 字，是点头规则拒的")
        r = check_anchor("需求分析一个chat专门做", [("quote", "比如说需求分析 一个 chat\n专门做")])
        check(r[0] and r[2] == "quote", "1 锚：只差空白 → 去掉空白后逐字，过")
        seg_mark = {"hero": "claude", "sid": "x", "line": 3}
        r = check_anchor("前置审查会把模型拉向更早停机", [("quote", "A"), (seg_mark, "门不是免费的，前置审查会把模型拉向更早停机")])
        check(r[0] and r[2] == seg_mark, "1 锚：截自补出的段 → 来源标 {hero,sid,line}")
        check(not quote_usable("A") and not quote_usable("可以") and not quote_usable(None),
              "1 quote_usable：「A」「可以」、缺 → 不能用")
        check(quote_usable(quote) and len(quote) > 60, "1 quote_usable：60 多字的 quote 能用")

        # ── 7 user_turns ──
        from heroes import claude as hc, codex as hx
        cl_root, cx_root = tmp / "claude", tmp / "codex"
        S_A = "57acd630-a358-4684-bc17-66059849922d"
        S_B = "f0b38e9d-ac45-4af4-89d2-e79ad46ced42"
        S_C = "8cfb7d96-bd68-4a81-a468-e8412b4a22d5"
        S_M = "11111111-2222-4333-8444-555555555555"
        S_X = "01a13cf6-51e9-73f2-b921-59ff92498a08"
        fa = _jsonl(cl_root / "d--fake-proj" / (S_A + ".jsonl"), [
            {"type": "queue-operation", "timestamp": "2026-09-16T04:00:00.000Z"},      # 1
            None,                                                                       # 2 空行
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Caveat: 元信息"}},
            _u("<system-reminder>提醒：别信\n这一段</system-reminder>\n门不是免费的，前置审查会把模型拉向更早停机"),
            {"type": "user", "toolUseResult": {"ok": 1},
             "message": {"role": "user", "content": [{"type": "tool_result", "content": "工具回执"}]}},
            {"type": "user", "message": {"role": "user", "content":
                "<command-name>/model</command-name>\n<command-message>model</command-message>"}},
            _u("外部材料\n别人的文章：门不是免费的，前置审查会把模型拉向更早停机"),
            _u("# 外部材料 · 公众号\n别人写的"),
            _u('<pasted_content id="1">粘进来的一大段：门不是免费的</pasted_content id="1">'),
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "AI 说的"}]}},
            {"type": "user", "message": {"role": "user", "content": "A"}},              # 11
            _u("UserPromptSubmit hook additional context: 已注入: x"),
            _u("<task-notification>子分身回来了</task-notification>"),
            _u("[Request interrupted by user]"),
            _u('外部資料："""别人的文章：门不是免费的"""\n\n按照这篇焚诀审查当前项目'),          # 15
            _u('豆包翻译："""我自己口述的一段"""'),                                         # 16
            _u('外部资料："""文章里带代码\ndef f():\n    """docstring"""\n文章完"""这个可以深挖'),  # 17
        ])
        turns = hc.user_turns(fa)
        check(turns[:2] == [(4, "门不是免费的，前置审查会把模型拉向更早停机"), (11, "A")],
              "7 claude user_turns：注入区删掉、元信息/工具回执/命令回显/头标块/粘贴区/hook 回执都不算，行号按物理行")
        check(turns[2:] == [(15, "按照这篇焚诀审查当前项目"), (16, '豆包翻译："""我自己口述的一段"""'),
                            (17, "这个可以深挖")],
              "7 claude user_turns：头标行就用三引号包住的只删包住的那段（收尾认最后一个三引号）、留他的话；"
              "不带头标的三引号不动")
        fm = _jsonl(cl_root / "d--fake-proj" / (S_M + ".jsonl"), [
            {"type": "queue-operation", "timestamp": "2026-09-16T05:00:00.000Z"},
            _u("<system-reminder>x</system-reminder>"),
            _u("# 你是锻造台的执行枝。照任务书干活"),
            _u("门不是免费的，前置审查会把模型拉向更早停机"),
        ])
        check(hc.user_turns(fm) == [], "7 claude user_turns：机器任务书开的场（# 你是…）整场 []")
        fq = _jsonl(tmp / "claude-question" / "d--fake-proj" / ("44444444-5555-4666-8777-888888888888.jsonl"), [
            _u("你是不是把需求和实现混在一起了？"),
            _u("需求分析必须在实现之前完成"),
        ])
        check([t for _l, t in hc.user_turns(fq)] == ["你是不是把需求和实现混在一起了？", "需求分析必须在实现之前完成"],
              "7 claude user_turns：他以「你是不是…？」开场不算机器任务书，整场照留")
        # 放在另一个根下：不掺进下面 find_sessions 按日子数场的用例
        ft = _jsonl(tmp / "claude-taskbook" / "d--fake-proj" / ("22222222-3333-4444-8555-666666666666.jsonl"), [
            {"type": "queue-operation", "timestamp": "2026-10-09T05:00:00.000Z"},
            _u("# 评审视角：表达扫描\n\n下面是一份 PPT 的全部页面文字。" + "5. **画成图的关系必须和事实对得上**。" * 12),
            _u("本场一件事：给述职 PPT 建表达形式知识库\n【对 ，就是看提炼完总结后的大段的文字】"),
            _u("# 一个问题\n这块咋整"),
            _u("对 ，就是看提炼完总结后的大段的文字，识别里面表达了什么样子的关系"),
        ])
        check(hc.user_turns(ft) == [(4, "# 一个问题\n这块咋整"),
                                    (5, "对 ，就是看提炼完总结后的大段的文字，识别里面表达了什么样子的关系")],
              "7 claude user_turns：AI 写的任务书（# 开头的长块）、开场白（本场一件事…）不算他说的；他打的短 # 照留")
        fx = _jsonl(cx_root / "2026" / "09" / "16" / ("rollout-2026-09-16T10-00-00-" + S_X + ".jsonl"), [
            {"type": "session_meta", "timestamp": "2026-09-16T01:00:00Z", "payload": {"id": S_X, "cwd": "D:/x"}},
            _cx("user", "# AGENTS.md instructions for D:/x\n规则", "<environment_context>x</environment_context>"),
            _cx("developer", "系统灌的"),
            _cx("user", "换班按事情双触发，开局是换班门票"),
            _cx("user", "录音对话\n[00:01] 甲：别人说的"),
            _cx("assistant", "AI 答的", kind="output_text"),
        ])
        check(hx.user_turns(fx) == [(4, "换班按事情双触发，开局是换班门票")],
              "7 codex user_turns：AGENTS.md / 尖括号注入 / developer / 头标块都不算")
        fxm = _jsonl(cx_root / "2026" / "09" / "17" / ("rollout-2026-09-17T10-00-00-" + S_M[:-1] + "6.jsonl"), [
            {"type": "session_meta", "payload": {"id": "x"}},
            _cx("user", "你是复核员。读下面的稿子"),
            _cx("user", "换班按事情双触发"),
        ])
        check(hx.user_turns(fxm) == [], "7 codex user_turns：机器任务书开的场整场 []")

        # ── 8 find_sessions：三种号写法 + ts 那天开场 + seen ──
        _jsonl(cl_root / "d--fake-proj" / (S_B + ".jsonl"), [
            {"type": "summary", "summary": "没有时间戳的头一行"},
            {"type": "queue-operation", "timestamp": "2026-09-20T04:00:00.000Z"},
            _u("复利是叠加在执行线之上的第二个机制"),
        ])
        _jsonl(cl_root / "d--fake-proj" / (S_C + ".jsonl"), [
            {"type": "queue-operation", "timestamp": "2026-09-24T04:00:00.000Z"},
            _u("开局先写一句闲话"),
            _u("换班按事情双触发，开局是换班门票"),
        ])
        # 子分身的卷宗不算：只翻项目目录那一层
        _jsonl(cl_root / "d--fake-proj" / S_A / "subagents" / ("agent-" + S_B + ".jsonl"), [
            {"type": "queue-operation", "timestamp": "2026-09-16T04:00:00.000Z"}, _u("子分身的话")])
        cfg = {"heroes": [{"id": "claude", "root": str(cl_root)}, {"id": "codex", "root": str(cx_root)}]}

        def ids_of(got):
            return sorted(_sid_of(p) for _h, p in got)

        step, got = find_sessions({"extra": {"源": "会话 57acd630 15:47:43（语义提炼补记）"}}, cfg)
        check(ids_of(got) == [S_A] and step.startswith("①"), "8 号写法：8 位前缀")
        step, got = find_sessions({"extra": {"源": "本场 + 会话 f0b38e9ded42"}}, cfg)
        check(ids_of(got) == [S_B] and step.startswith("①"), "8 号写法：前 8 位 + 后 4 位共 12 位")
        step, got = find_sessions({"extra": {"sid": S_C}}, cfg)
        check(ids_of(got) == [S_C] and step.startswith("①"), "8 号写法：画像 extra.sid 完整 uuid")
        step, got = find_sessions({"ts": "2026-09-16", "extra": {"源": "会话 2026-10-09 里定的"}}, cfg)
        check(ids_of(got) == sorted([S_A, S_M, S_X]) and step.startswith("②"),
              "8 ts 那天开场的会话（claude 首条 timestamp / codex 文件名日期），「会话 <日期>」不当号，子分身不算")
        step, got = find_sessions({"ts": "2026-09-20"}, cfg)
        check(ids_of(got) == [S_B], "8 ts：头一行没时间戳也往下找到第一条带 timestamp 的")
        step, got = find_sessions({"ts": "2026-09-16"}, cfg, cap=2)
        check(len(got) == 2 and "截掉 1 场" in step, "8 ts 那天的会话超过上限：只取上限场，结果里说截了几场")
        step, got = find_sessions({"ts": None, "seen": [{"sid": S_B, "date": "2026-09-20"},
                                                        {"sid": "deadbeef-0000-4000-8000-000000000000"}]}, cfg)
        check(ids_of(got) == [S_B] and step.startswith("③"), "8 ts 是 null：走 seen 那几场")
        step, got = find_sessions({"extra": {"源": "会话 abcdef12"}, "ts": "2026-09-24"}, cfg)
        check(ids_of(got) == [S_C] and "①" in step and "②" in step, "8 号在根里找不到：往下走 ts，结果里两步都看得见")
        step, got = find_sessions({"ts": "2001-01-01"}, cfg)
        check(got == [] and "没找到" in step, "8 三步都落空：[]，说没找到")
        # 前 12 位和 S_B 一样、尾号不同的另一场：按「前 12 位」读会被误认
        same_head = "f0b38e9d-ac45-4222-8222-222222225678"
        check(_sid_match("f0b38e9ded42", S_B) and not _sid_match("f0b38e9dac45", same_head)
              and not _sid_match("f0b38e9dac45", S_B),
              "8 12 位的号只按前 8 + 后 4 认，不按前 12 位认")
        utc_noon = datetime.datetime(2026, 9, 16, 12, 0, tzinfo=datetime.timezone.utc)
        check(_local_day(utc_noon.isoformat().replace("+00:00", "Z")) == utc_noon.astimezone().date().isoformat()
              and _local_day("2026-09-16") == "2026-09-16" and _local_day(None) == "",
              "8 带时区的 ts 换成本机日期再找开场日；纯日期照原样")

        segs = segments({"text": "认门不是免费的：前置审查会把模型拉向更早停机，只备料不改规则", "quote": "A"},
                        [(hc, fa)])
        check(segs and segs[0]["sid"] == S_A and segs[0]["line"] == 4 and segs[0]["hero"] == "claude"
              and segs[0]["text"] == "门不是免费的，前置审查会把模型拉向更早停机",
              "8 segments：按汉字二元组重叠挑段，带 hero / sid / line")
        check(all(s["score"] >= SEGMENT_MIN_SCORE for s in segs) and len(segs) <= SEGMENT_LIMIT,
              "8 segments：低于最低分的不要、≤5 段")
        fc = _jsonl(tmp / "claude-ctx" / "d--fake-proj" / ("33333333-4444-4555-8666-777777777777.jsonl"), [
            _u("关于字典和资源管理两个服务要不要合并。一个是他们本身有没有必要存在，存在的意义"),
        ])
        sc = segments({"text": "拿原点检验组件：他们本身有没有必要存在"}, [(hc, fc)])
        sc_work = {"item": {"id": "decision:" + "1" * 16, "kind": "decision", "text": "x"},
                   "quote_ok": False, "segs": sc}
        check(sc and sc[0]["ctx"] == "关于字典和资源管理两个服务要不要合并。"
              and "段1（前文：关于字典和资源管理两个服务要不要合并。）：一个是" in build_prompt([sc_work])
              and all(text != sc[0]["ctx"] for _m, text in _sources(sc_work)),
              "8 segments：段带同一句话里的前一小句当前文、进提示词；前文不进核锚的来源")
        check(segments({"text": "停机"}, [(hc, fa)]) == [],
              "8 segments：只撞上一两个二元组的不相干段不挑（最低分 {}）".format(SEGMENT_MIN_SCORE))

        # ── 6 run() 全流程（桩 call、桩 intake.load、临时铁目录、假会话根） ──
        def mat(name, state="ok", **kw):
            import hashlib
            mid = "decision:" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
            it = {"id": mid, "kind": "decision", "text": name, "ptr": "D:/fake/LEDGER.md#L{}".format(len(name)),
                  "ts": None, "state": state,
                  "via": {"canon": "fake/decisions", "derived": "fake/hits"}}
            it.update(kw)
            return it

        m_good = mat("按阶段换场：需求和设计一场，执行另开一场", quote=quote, ts="2026-10-05", count=9,
                     seen=[{"sid": S_B, "date": "2026-10-07", "title": "不该进提示词的标题"}])
        m_a = mat("认门不是免费的：前置审查会把模型拉向更早停机，只备料不改规则", quote="A", ts="2026-09-16",
                  extra={"源": "会话 57acd630"})
        m_none = mat("一条补不出原话的料", quote="A", ts="2001-01-01")
        m_retry = mat("换班按事情双触发：开局就是换班门票", quote="这一句是原话里能截的东西，够长也够用",
                      extra={"sid": S_C})
        m_omit = mat("炉子不吐的那条料", quote="另一句够长够用的原话，炉子偏不吐它")
        m_stale = mat("正本里没了的料", state="stale")
        m_unv = mat("正本没读到的料", state="unverified")
        missing = "decision:" + "0" * 16
        pool = [m_good, m_a, m_none, m_retry, m_omit, m_stale, m_unv]
        script = {
            m_good["id"]: lambda n: {"text": "同一件事换阶段就换场", "kind": "流程", "grade": "上品",
                                     "anchor": "需求分析一个chat专门做"},
            m_a["id"]: lambda n: {"text": "前置门会改停机点", "kind": "判断", "grade": "中品",
                                  "anchor": "前置审查会把模型拉向更早停机"},
            m_none["id"]: lambda n: {"text": "补不出原话也照样炼", "kind": "瞎写", "anchor": "A"},
            m_retry["id"]: lambda n: {"text": "换班按事情双触发", "kind": "流程", "grade": "中品",
                                      "anchor": "这句是编的不在原话里" if n == 1 else "换班按事情双触发"},
        }
        calls = []

        def stub_call(prompt, timeout=120):
            calls.append(prompt)
            out = [dict(script[m](len(calls)), material=m)
                   for m in re.findall(r"料 id：(\S+)", prompt) if m in script]
            return "好的，炼好了：\n```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"

        irons_dir = tmp / "irons"
        irons_dir.mkdir()
        orig_load = intake.load
        intake.load = lambda kind=None, min_count=None, cfg=None: {"items": [dict(i) for i in pool]}
        try:
            now1 = datetime.datetime(2026, 10, 15, 14, 30, 12)
            res = run([m_good["id"], m_a["id"], m_none["id"], m_retry["id"], m_omit["id"],
                       m_stale["id"], m_unv["id"], missing, m_good["id"]],
                      cfg=cfg, call=stub_call, irons_dir=irons_dir, now=now1)
            by = {i["from"]["material"]: i for i in res["irons"]}
            why = {s["id"]: s["why"] for s in res["skipped"]}
            g = by.get(m_good["id"]) or {}
            check(g.get("id") == m_good["id"].split(":")[1][:8] + "-m1" and g.get("anchor_src") == "quote"
                  and g.get("anchor") in quote,
                  "6 run：好 quote → 锚逐字在 quote 里，anchor_src=quote，id=<前 8 位>-m1")
            check(g.get("from") == {"material": m_good["id"], "ptr": m_good["ptr"], "via": m_good["via"],
                                    "picked": "2026-10-15"}, "6 run：from 带 material / ptr / via / picked")
            check(list(g) == ["id", "text", "anchor", "kind", "grade", "from", "anchor_src"],
                  "6 run：铁的字段顺序照合同")
            a = by.get(m_a["id"]) or {}
            check(a.get("anchor_src") == {"hero": "claude", "sid": S_A, "line": 4},
                  "6 run：quote「A」→ 去会话补出，anchor_src 指回那场那行")
            n = by.get(m_none["id"]) or {}
            check(n.get("seat") == SEAT and "anchor" not in n and "anchor_src" not in n,
                  "6 run：补不出 → 他认铁，没有 anchor / anchor_src 键")
            check(n.get("kind") == "经验" and n.get("grade") == "下品", "6 run：kind / grade 不在词表 → 经验 / 下品")
            rt = by.get(m_retry["id"]) or {}
            check(rt.get("anchor_src") == {"hero": "claude", "sid": S_C, "line": 3} and len(calls) == 2,
                  "6 run：quote 能用但锚核不过 → 补原话后重烧一轮（只一轮），锚指回补出的那行")
            check("料 id：" + m_retry["id"] in calls[1] and m_good["id"] not in calls[1],
                  "6 run：重烧那一轮只带核不过的那几条")
            check(why.get(m_omit["id"]) == "炉子没吐出这条", "6 run：模型压根没吐 → skipped")
            check(why.get(m_stale["id"]) == STATE_WHY["stale"] and why.get(m_unv["id"]) == STATE_WHY["unverified"],
                  "6 run：stale / unverified → skipped，文案照合同")
            check(why.get(missing) == "料仓里没有这条", "6 run：料仓里没有 → skipped")
            check(sum(1 for i in res["irons"] if i["from"]["material"] == m_good["id"]) == 1,
                  "6 run：同一请求里重复的 id 只炼一次")
            p1 = calls[0]
            check("门不是免费的，前置审查会把模型拉向更早停机" in p1 and "原话：（没有能用的）" in p1,
                  "6 run：quote 不能用的，头一轮之前就补好段进提示词")
            check("不该进提示词的标题" not in p1 and "count" not in p1, "6 run：count / seen 不进提示词")
            rec_f = irons_dir / "smelt-20261015-143012.json"
            rec = json.loads(rec_f.read_text(encoding="utf-8")) if rec_f.exists() else {}
            check(res["file"].endswith("smelt-20261015-143012.json") and rec.get("source") == "smelt"
                  and rec.get("session_hash") == "smelt-20261015-143012" and rec.get("irons") == res["irons"],
                  "6 run：落盘 smelt-<日期-时刻>.json，形状照反思炉")
            check(any(d.get("material") == m_retry["id"] for d in rec.get("dropped") or []),
                  "6 run：核不过的候选留在 dropped")
            res2 = run([m_good["id"]], cfg=cfg, call=stub_call, irons_dir=irons_dir, now=now1)
            check([i["id"] for i in res2["irons"]] == [m_good["id"].split(":")[1][:8] + "-m2"]
                  and res2["file"].endswith("smelt-20261015-143012-2.json"),
                  "6 run：同一条料炼第二次 → -m2；同一秒落盘不盖掉上一份")
            res3 = run([m_stale["id"]], cfg=cfg, call=stub_call, irons_dir=irons_dir,
                       now=datetime.datetime(2026, 10, 15, 15, 0, 0))
            check(res3["irons"] == [] and len(calls) == 3 and (irons_dir / "smelt-20261015-150000.json").exists(),
                  "6 run：一条都炼不了 → 不调模型，照样落盘留痕")

            def burnt_retry(prompt, timeout=120):
                if burnt_retry.n:
                    raise RuntimeError("超时")
                burnt_retry.n += 1
                return json.dumps([{"material": m_retry["id"], "text": "换班按事情双触发", "kind": "流程",
                                    "grade": "中品", "anchor": "这句是编的不在原话里"}], ensure_ascii=False)
            burnt_retry.n = 0
            res4 = run([m_retry["id"]], cfg=cfg, call=burnt_retry, irons_dir=irons_dir,
                       now=datetime.datetime(2026, 10, 15, 15, 5, 0))
            why4 = {s["id"]: s["why"] for s in res4["skipped"]}
            check(res4["irons"] == [] and "重炼没烧成" in why4.get(m_retry["id"], ""),
                  "6 run：补原话后那一轮烧糊 → 进 skipped 让他再炼，不坐他认")

            def silent_retry(prompt, timeout=120):
                silent_retry.n += 1
                if silent_retry.n > 1:
                    return "[]"
                return json.dumps([{"material": m_retry["id"], "text": "换班按事情双触发", "kind": "流程",
                                    "grade": "中品", "anchor": "这句是编的不在原话里"}], ensure_ascii=False)
            silent_retry.n = 0
            res5 = run([m_retry["id"]], cfg=cfg, call=silent_retry, irons_dir=irons_dir,
                       now=datetime.datetime(2026, 10, 15, 15, 6, 0))
            why5 = {s["id"]: s["why"] for s in res5["skipped"]}
            check(res5["irons"] == [] and "炉子没吐出这条" in why5.get(m_retry["id"], ""),
                  "6 run：补原话后那一轮没吐这条 → 进 skipped，不当成补不出去坐他认")

            def wordy(prompt, timeout=120):
                return json.dumps([{"material": m_good["id"], "text": "分析需求" * 15, "kind": "流程",
                                    "grade": "上品", "anchor": "需求分析一个chat专门做"}], ensure_ascii=False)
            res6 = run([m_good["id"]], cfg=cfg, call=wordy, irons_dir=irons_dir,
                       now=datetime.datetime(2026, 10, 15, 15, 7, 0))
            why6 = {s["id"]: s["why"] for s in res6["skipped"]}
            check(res6["irons"] == [] and "超 {} 字".format(TEXT_MAX) in why6.get(m_good["id"], ""),
                  "6 run：铁的一句话超 40 字 → 不出铁、进 skipped")
        finally:
            intake.load = orig_load

        # ── 4 加工状态推算 ──
        swords_dir, state_file = tmp / "swords", tmp / "intake-state.json"
        (swords_dir / "s1").mkdir(parents=True)
        Y, Z = "profile:" + "1" * 16, "kg:" + "2" * 16
        (swords_dir / "s1" / "meta.json").write_text(json.dumps({"id": "s1", "irons": [
            {"id": "y-m1", "from": {"material": Y}}, {"id": "g", "from": {"material": m_good["id"]}}]}),
            encoding="utf-8")
        logs = []
        st = forge_states(irons_dir, swords_dir, state_file, log=logs.append)
        check(forge_of(st, m_a["id"])["state"] == IN_FORGE and forge_of(st, m_a["id"])["irons"] == [a.get("id")],
              "4 状态：铁的 from.material=X → 已入炉，带铁 id")
        check(forge_of(st, Y) == {"state": SWORDED, "irons": [], "swords": ["s1"]}
              and forge_of(st, m_good["id"])["state"] == SWORDED,
              "4 状态：剑快照 from.material=Y → 已成剑（压过已入炉）")
        check(forge_of(st, Z)["state"] == UNSEEN, "4 状态：没动静 → 未看")
        now2 = datetime.datetime(2026, 10, 15, 16, 0, 0)
        check(toggle_discard(Z, state_file, now2) == DISCARD
              and forge_of(forge_states(irons_dir, swords_dir, state_file), Z)["state"] == DISCARD,
              "4 状态：点弃 → 弃，记进状态文件")
        toggle_discard(m_a["id"], state_file, now2)
        check(forge_of(forge_states(irons_dir, swords_dir, state_file), m_a["id"])["state"] == IN_FORGE,
              "4 状态：已入炉的料点了弃，仍显示已入炉（算得出的压过手记的）")
        check(toggle_discard(Z, state_file, now2) is None
              and Z not in json.loads(state_file.read_text(encoding="utf-8")), "4 状态：弃点两次 → 键删掉")
        toggle_discard(Z, state_file, now2)
        state_file.unlink()
        st = forge_states(irons_dir, swords_dir, state_file)
        check(forge_of(st, Z)["state"] == UNSEEN and forge_of(st, m_a["id"])["state"] == IN_FORGE
              and forge_of(st, Y)["state"] == SWORDED, "4 状态：删掉状态文件 → 弃没了，已入炉 / 已成剑照样算得出")
        state_file.write_text("{写坏了", encoding="utf-8")
        st = forge_states(irons_dir, swords_dir, state_file, log=logs.append)
        check(forge_of(st, Y)["state"] == SWORDED and any("读不动" in m for m in logs),
              "4 状态：状态文件写坏了 → 当空，log 里说一句")
        state_file.unlink()

        _selftest_main(tmp, check)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("[smelt] selftest {}".format("全绿" if ok else "有 FAIL"))
    return ok


FAKE_SWORD = """剑名：试锻剑
选铁：{picked}

=== SKILL.md ===
---
name: test-sword
description: 自测用的一把剑
triggers: 自测
---

# 自测

## 判据

- 照着干——（他认 · 2026-10-15）

## 步骤

1. 跑自测

## 反例

- 照抄而不是照着干
"""


def _selftest_main(tmp, check):
    """main.py 那头：座位闸、场景模式、经验值计数、接口。main 的目录和 call_claude 是模块全局，
    临时换掉、finally 换回——一个字节都不许写进真实 data/。"""
    import http.client
    from http.server import ThreadingHTTPServer
    import main

    saved = {k: getattr(main, k) for k in ("IRONS", "SWORDS", "PROFILE_FILE", "LOG_FILE",
                                           "INTAKE_STATE_FILE", "call_claude", "log", "DEMO_MODE")}
    saved_payload = intake.materials_payload
    mroot = tmp / "main"
    main.IRONS, main.SWORDS = mroot / "irons", mroot / "swords"
    main.PROFILE_FILE, main.LOG_FILE = mroot / "profile.json", mroot / "forge.log"
    main.INTAKE_STATE_FILE = mroot / "intake-state.json"
    main.IRONS.mkdir(parents=True)
    main.SWORDS.mkdir(parents=True)
    main.DEMO_MODE = False
    logs, prompts = [], []
    main.log = logs.append
    srv = None
    try:
        frm = {"material": "decision:" + "a" * 16, "ptr": "D:/Project/personal/toys/DECISION-LEDGER.md#L96",
               "via": {"canon": "c", "derived": "d"}, "picked": "2026-10-15"}
        t1 = {"id": "aaaaaaaa-m1", "text": "他认的一块", "kind": "判断", "grade": "中品",
              "from": frm, "seat": SEAT}
        t2 = dict(t1, id="bbbbbbbb-m1", text="他认的另一块")
        n1 = {"id": "cccccccc-m1", "text": "有锚的一块", "anchor": "需求分析一个chat专门做", "kind": "流程",
              "grade": "上品", "from": dict(frm, material="decision:" + "c" * 16), "anchor_src": "quote"}
        (main.IRONS / "smelt-20261015-143012.json").write_text(
            json.dumps({"source": "smelt", "irons": [t1, t2, n1]}, ensure_ascii=False), encoding="utf-8")
        r1 = {"id": "0f2d3f3d-i1", "text": "反思炉出的一块", "anchor": "动词是 how，不是 why", "kind": "判断",
              "grade": "上品"}
        (main.IRONS / "0f2d3f3dfdde.json").write_text(json.dumps({"irons": [r1]}, ensure_ascii=False),
                                                     encoding="utf-8")

        # ── 5 炼料不是反思场次 ──
        check(main.count_reflect_sessions() == 1, "5 count_reflect_sessions 不数 smelt-*.json")

        # ── 2 座位上限 ──
        def stub(prompt, timeout=120):
            prompts.append(prompt)
            return FAKE_SWORD.format(picked=", ".join(re.findall(r"- \[([^\]]+)\]", prompt)))

        main.call_claude = stub
        try:
            main.forge(iron_ids=[t1["id"], t2["id"]])
            stage = None
        except main.ForgeError as e:
            stage = e.stage
        check(stage == "seat_taken" and prompts == [] and main.STAGE_CODES.get("seat_taken") == 400,
              "2 点名锻剑选两块他认铁 → seat_taken（400），且没烧火")
        out = main.forge(iron_ids=[t1["id"], n1["id"]])
        meta = json.loads((main.SWORDS / out["sword"]["id"] / "meta.json").read_text(encoding="utf-8"))
        snaps = {s["id"]: s for s in meta.get("irons") or []}
        check(snaps.get(t1["id"], {}).get("seat") == SEAT
              and snaps.get(t1["id"], {}).get("from", {}).get("ptr") == frm["ptr"]
              and snaps.get(n1["id"], {}).get("from", {}).get("ptr") == frm["ptr"]
              and "seat" not in snaps.get(n1["id"], {}),
              "2 一块他认 + 一块普通 → 能锻；剑的铁快照带 from.ptr，他认那块带 seat")
        check("他认 · 2026-10-15（无原话锚：用它的判据以「——（他认 · 2026-10-15）」收尾）" in prompts[-1]
              and "原话：「需求分析一个chat专门做」" in prompts[-1], "2 iron_line：他认铁标出来，普通铁照旧")
        ign = main.SWORDS / out["sword"]["id"] / ".gitignore"
        check(ign.exists() and ign.read_text(encoding="utf-8").splitlines()[-1] == "*",
              "2 用了料仓铁的剑：剑目录里放只写 * 的 .gitignore，整把不进公开仓")
        out_r = main.forge(iron_ids=[r1["id"]])
        check(not (main.SWORDS / out_r["sword"]["id"] / ".gitignore").exists(),
              "2 只用反思炉的铁锻的剑：不放 .gitignore，照旧入库")
        scroll = mroot / "SKILL.md"
        scroll.write_text("---\nname: x\n---\n# 外来剑\n", encoding="utf-8")
        saved_cfg = main.read_config
        main.read_config = lambda: dict(main.DEFAULT_CONFIG, foreign_scrolls=[
            {"id": "x", "name": "外来剑", "path": str(scroll)}])
        try:
            n_before = len(prompts)
            main.reforge(str(scroll), [t1["id"], n1["id"]])
            stage = None
        except main.ForgeError as e:
            stage = e.stage
        finally:
            main.read_config = saved_cfg
        check(stage == "no_anchor" and len(prompts) == n_before,
              "2 重锻不收「他认」铁（重锻每处改动都要引原话锚），挡在调模型之前")

        # ── 3 场景模式不放他认铁 ──
        out = main.forge(scene="写日报")
        check(t1["id"] not in prompts[-1] and t2["id"] not in prompts[-1] and n1["id"] in prompts[-1],
              "3 场景模式：他认铁不进料堆（不在 prompt 里）")
        prompts.clear()

        def stub_greedy(prompt, timeout=120):
            prompts.append(prompt)
            return FAKE_SWORD.format(picked="{}, {}".format(n1["id"], t1["id"]))

        main.call_claude = stub_greedy
        out = main.forge(scene="写日报")
        check(out["sword"]["iron_ids"] == [n1["id"]], "3 场景模式：炉子报了他认铁 id 也不算进 used")

        # ── 9 接口：演示模式挡料仓，非演示模式校验请求体 ──
        srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=15)
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            c.request(method, path, body=raw,
                      headers={"Content-Type": "application/json"} if raw is not None else {})
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, json.loads(data.decode("utf-8"))

        good_id = "decision:" + "c" * 16
        main.DEMO_MODE = True
        s, b = req("GET", "/api/intake/materials")
        check(s == 200 and b.get("ok") is False and b.get("items") == [], "9 演示：materials 回 ok:false、items 空")
        s1, _ = req("POST", "/api/smelt", {"material_ids": [good_id]})
        s2, _ = req("POST", "/api/intake/discard", {"id": good_id})
        check(s1 == 403 and s2 == 403, "9 演示：POST /api/smelt、/api/intake/discard 回 403")
        _, demo_irons = req("GET", "/api/irons")
        _, demo_arm = req("GET", "/api/armory")
        main.DEMO_MODE = False
        _, live_irons = req("GET", "/api/irons")
        _, live_arm = req("GET", "/api/armory")

        def carried(swords):
            return [s["id"] for s in swords if any(i.get("from") for i in s.get("irons") or [])]
        check(any(i.get("from") for i in live_irons) and not any(i.get("from") for i in demo_irons),
              "9 演示：/api/irons 不吐出自料仓的铁（原话和正本路径不出门）")
        check(carried(live_arm) and not carried(demo_arm),
              "9 演示：/api/armory 不吐用了料仓铁的剑")
        bad = [({"material_ids": ["decision:XYZ"]}, "非法 id"),
               ({"material_ids": [good_id], "path": "C:/x"}, "多余键"),
               ({"material_ids": []}, "空数组"),
               ({"material_ids": good_id}, "不是数组"),
               ({"material_ids": [good_id + "\n"]}, "尾巴带换行"),
               ({"material_ids": ["decision:%016x" % i for i in range(21)]}, "超过 20 条"),
               ([good_id], "请求体不是对象")]
        for body, name in bad:
            s, b = req("POST", "/api/smelt", body)
            check(s == 400 and b.get("stage") == "bad_request", "9 非演示：/api/smelt {} → 400".format(name))
        s, b = req("POST", "/api/intake/discard", {"id": "bad"})
        check(s == 400 and b.get("stage") == "bad_request", "9 非演示：/api/intake/discard 非法 id → 400")
        s, b = req("POST", "/api/intake/discard", {"id": good_id})
        s2, b2 = req("POST", "/api/intake/discard", {"id": good_id})
        check(s == 200 and b == {"ok": True, "id": good_id, "state": "弃"}
              and s2 == 200 and b2.get("state") is None, "9 非演示：弃一次 state=弃，再点一次 state=null")
        intake.materials_payload = lambda cfg=None, kind=None, min_count=None: {
            "ok": True, "items": [{"id": good_id}, {"id": "kg:" + "9" * 16}], "notes": [], "problems": []}
        s, b = req("GET", "/api/intake/materials")
        fs = {i["id"]: i.get("forge") for i in b.get("items") or []}
        check(s == 200 and fs.get(good_id, {}).get("state") == SWORDED
              and fs.get("kg:" + "9" * 16) == {"state": UNSEEN, "irons": [], "swords": []},
              "9 非演示：materials 每条料带 forge 字段")
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        intake.materials_payload = saved_payload
        for k, v in saved.items():
            setattr(main, k, v)


if __name__ == "__main__":
    # 换成以模块名 smelt 导入的那一份再跑：main 也 import smelt，
    # 不这样的话自测里会有 __main__ 和 smelt 两份，桩换在一份上、main 调的却是另一份
    import smelt
    sys.exit(smelt.main())
