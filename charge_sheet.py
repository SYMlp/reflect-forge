"""回炉单生成器 Charge Sheet —— 炉子自己的攒料器。

反思锻造台把 session 里的精华（铁）锻成剑。但反思本身有入场成本：
要想起来开、要自己找料、要自己组织问题。这个生成器把「找料 + 归集」自动化——
它是一台**确定性归集器**（不是判断器），按两根轴把使用痕迹归成一份「回炉单」：

  · 组件轴：每个 route/组件被命中多少次、时间跨度、代表性开场样本
  · 事  轴：每场会话申报的「本场一件事」（session 契约标记），标明是哪位侠客的会话

产出「回炉单」（markdown 人读版 + json 机读版），供反思炉 / 反思会话消费。
「进料口」这个名字归扫描器 intake.py：回炉单读的是痕迹，不是成形的料（docs/INTAKE-SPEC.md 第 8 节）。

设计权威：
  · toys/self-analysis/2026-09-01-用磨分离锻造回路-深挖框题.md（§1 缺口 / §3 切片 1 / §3½ 递归分层）
  · toys/self-analysis/2026-08-31-开场意图识别机制设计稿.md（§10 复述契约）
  · docs/INTAKE-SPEC.md 第 8 节（并列不替换、改名、会话来源走侠客花名册）

硬边界（写死在设计里，不许破）：
  · **本文件里没有一次 LLM 调用**。含金量判断留给炉子和反思会话——
    判断给 LLM、归集给程序，这是设计原则。这台机器只数数、只归集、只排序。
  · 只读 hook 台账 + 花名册上各位侠客的卷宗（heroes.enabled，同寻料页），只写本仓 data/。
  · session 转写可能上到十几 MB——**逐行流式解析，绝不整文件载入**。

跑法：
  python charge_sheet.py            # 台账读 config.json 的 charge_sheet 段，会话走侠客花名册
  python charge_sheet.py --selftest # 临时目录造数据自测，不碰真实数据
  python charge_sheet.py --since 2026-09-01           # 只看该日期起（含）的裸奔率，切掉老账
  python charge_sheet.py --ledger <路径> --sessions <目录或glob> --out <目录>  # 临时覆盖（--sessions 调试用，给了就不走花名册）
"""
import argparse
import datetime
import glob as globmod
import json
import os
import re
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import heroes

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CONFIG_FILE = ROOT / "config.json"

# 台账 sid 截断到 16 字符（见 my-desk/pa 的 hook 写法）；session 文件名是完整 uuid，
# 前 16 位与台账 sid 对齐——两根轴就靠这 16 个字符缝合
SID_KEY_LEN = 16

# 每个组件挂几条代表性开场样本 / 开场 miss 区挂几条 / 中途 miss 挂几条 / 事轴每 sid 只取首条申报
COMPONENT_SAMPLES = 4
MISS_SAMPLES = 12
MIDSESSION_SAMPLES = 5
PROMPT_HEAD_MAX = 140

# miss 样本里这些前缀不是「开场」——是后台 re-invoke 与系统注入的噪声，
# 归到噪声计数、不进 miss 样本区。No silent caps：数量照实报，不静默吞
NOISE_PREFIXES = ("<task-notification>", "<system-reminder>", "<command-name>",
                  "<local-command", "<bash-", "Caveat:")

# config.json 里 charge_sheet 段缺省值。真实路径全部走配置，代码里只留形状缺省，
# 禁止把用户机器上的绝对路径硬编在源码里。
# 会话不在这一段：走整份 config 的 heroes（侠客花名册，同寻料页）——加一位侠客，回炉单一个字不用改
DEFAULT_CHARGE_CFG = {
    # hook 路由台账：每条 prompt 的命中/miss 落账（生产者读自己的上游，不算写死路径，INTAKE-SPEC 第 11 节）
    "ledger_path": "",
    # 回炉单落盘目录（相对本仓）
    "out_dir": "data/charge",
}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def read_charge_config():
    """→ (charge_sheet 段, 整份 config)。整份给 heroes.enabled 认花名册。
    读不动就当空的——由调用方决定报不报错。"""
    cfg, whole = dict(DEFAULT_CHARGE_CFG), {}
    if CONFIG_FILE.exists():
        try:
            whole = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(whole.get("charge_sheet") or {})
        except Exception as e:
            print("[charge] config.json 读不动（{}），charge_sheet 段先空着".format(e))
    return cfg, whole


# ── 组件轴：扫 hook 路由台账 ──────────────────────────────────────

def _clean_head(s):
    """prompt_head 里带换行与前后空白，压成单行、掐到 PROMPT_HEAD_MAX 字。"""
    one = " ".join((s or "").split())
    return one[:PROMPT_HEAD_MAX]


def _is_noise(head):
    h = (head or "").lstrip()
    return h.startswith(NOISE_PREFIXES)


def _parse_ts(ts):
    """把时间戳解析成**朴素**(无时区) datetime，解不动就返回 None，不炸。

    台账 ts 形如 2026-08-24T09:56:42.747683（本地朴素）；session timestamp 形如
    2026-08-23T01:01:33.664Z（UTC 带 Z）。为了让两侧能同尺子比较，这里统一丢掉
    时区信息、按朴素时间比。由此 session（UTC）与台账（本地）在跨零点 ~8h 内的
    边界场次可能落错一侧——窗口只到「天」的粒度，这点偏差可接受（见 docs 说明）。
    """
    s = (ts or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1]  # 去掉 UTC 标记，按朴素时间比（见上）
    try:
        dt = datetime.datetime.fromisoformat(s)
    except Exception:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _parse_since(s):
    """--since 参数解析成当天 00:00 的朴素 datetime。格式错就返回 None（由调用方报错）。"""
    try:
        return datetime.datetime.strptime((s or "").strip(), "%Y-%m-%d")
    except Exception:
        return None


def _session_time(fp):
    """会话的**窗口判定时刻**：优先取文件内首个可解析的消息 timestamp，整份取不到才退回文件 mtime。

    择一说明（写进 docs）：session jsonl 按 append 时序落行，首行即开场，
    所以「首个 timestamp = 开场时刻」。窗口语义是「设计上线后**开的**场」，
    按开场时刻归窗最贴切；mtime 是最后修改（≈ 收尾）时刻，只当兜底。
    逐行读、命中首个 timestamp 即停——十几 MB 也只付首行成本。
    """
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"timestamp"' not in line:  # 廉价闸门：无 timestamp 字样不解析
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                dt = _parse_ts(ev.get("timestamp"))
                if dt:
                    return dt
    except OSError:
        pass
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(fp))
    except OSError:
        return None


def scan_ledger(path, since=None):
    """逐行流式扫台账，按组件归集命中，并把 miss 分成「开场 / 中途」两类单收。

    开场判定（确定性 sessionize，2026-09-01 首炉修订）：台账按 append 时序落行，
    某 sid 在**全量台账**里的第一行即该会话的开场——first-seen 跨全文件计算，
    不受 since 窗口影响（窗前已开的场，窗内的行都算中途）。缺 sid 的行无法
    sessionize，保守按开场计——宁可多进样本区被人看到，不静默藏进中途。

    miss 语义由此分层：**开场 miss** = 该有设计到场却裸奔（重点矿）；
    **中途 miss** = 会话中途的 prompt 无路由命中（常态，仅供观察，不算裸奔）。
    这是归集口径不是含金量判断——开场/中途是台账里的机械事实。

    since 非 None（朴素 datetime）时，只统计 `ts >= since` 的行——无法解析 ts 的行
    因为定位不了时间、也就无法确认在窗内，一并剔除（但仍参与 first-seen 判定）。
    since 为 None 时归集范围不变。

    返回 dict：
      components: {route: {hits, first_ts, last_ts, files:set, samples:[head]}}
      opening_rows: int            # 窗内开场行总数（命中 + miss，噪声不算）
      opening_misses: [head]       # 开场裸奔样本
      opening_miss_rows: int
      midsession_misses: [head]    # 中途无命中样本（少量，仅供观察）
      midsession_miss_rows: int
      noise_miss: int              # 被判为噪声的 miss 条数（照实报，不静默）
      rows, miss_rows: int         # 总行数 / matched 为空的行数
      span:       (first_ts, last_ts)   # 整个台账的时间跨度
    """
    f = Path(path).expanduser()
    if not f.exists():
        raise FileNotFoundError("找不到 hook 路由台账：{}".format(f))

    components = {}
    opening_misses, midsession_misses = [], []
    opening_rows = opening_miss_rows = midsession_miss_rows = 0
    noise_miss = 0
    rows, miss_rows = 0, 0
    first_ts, last_ts = None, None
    seen_sids = set()

    with f.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            # 开场判定必须先于窗口过滤：first-seen 跨全量台账（窗前开的场，窗内行=中途）
            sid = ev.get("sid")
            if sid:
                is_opening = sid not in seen_sids
                seen_sids.add(sid)
            else:
                is_opening = True  # 缺 sid 无法 sessionize，保守按开场计（见 docstring）
            ts = _parse_ts(ev.get("ts"))
            if since is not None and (ts is None or ts < since):
                continue  # 窗外 / 无法定位时间 → 不计入本窗（含组件轴与 miss 区）
            rows += 1
            if ts:
                first_ts = ts if first_ts is None or ts < first_ts else first_ts
                last_ts = ts if last_ts is None or ts > last_ts else last_ts

            matched = ev.get("matched") or []
            head = _clean_head(ev.get("prompt_head"))
            files = ev.get("files") or []
            noise = _is_noise(head)
            if is_opening and not noise:
                opening_rows += 1

            if not matched:
                miss_rows += 1
                if noise:
                    noise_miss += 1
                elif is_opening:
                    opening_miss_rows += 1
                    if head and len(opening_misses) < MISS_SAMPLES:
                        opening_misses.append(head)
                else:
                    midsession_miss_rows += 1
                    if head and len(midsession_misses) < MIDSESSION_SAMPLES:
                        midsession_misses.append(head)
                continue

            for route in matched:
                c = components.setdefault(route, {
                    "hits": 0, "first_ts": None, "last_ts": None,
                    "files": set(), "samples": [],
                })
                c["hits"] += 1
                if ts:
                    c["first_ts"] = ts if c["first_ts"] is None or ts < c["first_ts"] else c["first_ts"]
                    c["last_ts"] = ts if c["last_ts"] is None or ts > c["last_ts"] else c["last_ts"]
                for fn in files:
                    c["files"].add(fn)
                # 代表性样本：去重、滤掉系统噪声（task-notification 这类后台 re-invoke
                # 也会命中 route，但它不是「开场」），只留前 COMPONENT_SAMPLES 条
                if (head and not _is_noise(head) and head not in c["samples"]
                        and len(c["samples"]) < COMPONENT_SAMPLES):
                    c["samples"].append(head)

    return {
        "components": components,
        "opening_rows": opening_rows,
        "opening_misses": opening_misses,
        "opening_miss_rows": opening_miss_rows,
        "midsession_misses": midsession_misses,
        "midsession_miss_rows": midsession_miss_rows,
        "noise_miss": noise_miss,
        "rows": rows,
        "miss_rows": miss_rows,
        "span": (first_ts, last_ts),
    }


# ── 事轴：流式扫花名册上各位侠客的卷宗，取「本场一件事：」申报 ──────

EVENT_MARKER = "本场一件事"

# 卷宗 → 台账 sid。Claude 的文件名就是会话 uuid；Codex 的是 rollout-<时刻>-<uuid>，uuid 在尾巴上。
# 台账两家都记 uuid 前 16 位（实测 Codex 的行形如 01a13d98-a610-75），所以都先取 uuid 再截
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# mtime 预筛放宽一天：开场时刻是丢了时区的 UTC（见 _parse_ts），本机在 UTC 以西时，
# 窗内的场 mtime 可能早于 since。宽一天就预筛不掉窗内的场，精判仍归 _session_time
_PREFILTER_SLACK = datetime.timedelta(days=1)


def _sid_of(fp):
    stem = Path(fp).stem
    m = _UUID.search(stem)
    return (m.group(0) if m else stem)[:SID_KEY_LEN]


def _resolve_session_files(session_glob):
    """调试口 --sessions：既可是目录（自动补 *.jsonl），也可是 glob 串。"""
    p = Path(session_glob).expanduser()
    if p.is_dir():
        return sorted(str(x) for x in p.glob("*.jsonl"))
    return sorted(globmod.glob(os.path.expanduser(session_glob)))


def _candidates(cfg, session_glob=""):
    """→ (点名, 卷宗)。点名 [{hero, name, root, found, sessions, declared}]，
    卷宗 [(点名那一格, 侠客模块, 路径, 出身, mtime)]。

    默认走花名册（heroes.enabled，同寻料页）：Claude 根下全部项目槽、Codex 根下全部 rollout，
    怎么翻归侠客自己（scrolls）。根不在的跳过，但点名里照实记 found=False——
    没装 Codex 的机器不该开不了单，也不该悄悄少一家。
    session_glob 给了是调试口：只扫它，每份按路径认是谁的（heroes.whose）。
    """
    roll, files = {}, []

    def slot(h, root, found):
        return roll.setdefault((h.ID, str(root)), {
            "hero": h.ID, "name": h.NAME, "root": str(root),
            "found": found, "sessions": 0, "declared": 0})

    if session_glob:
        for fp in _resolve_session_files(session_glob):
            h = heroes.whose(fp)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            files.append((slot(h, session_glob, True), h, fp, "", mt))
        return list(roll.values()), files

    for h, root in heroes.enabled(cfg or {}):
        d = Path(root).expanduser()
        if not d.is_dir():
            slot(h, root, False)
            continue
        try:
            got = h.scrolls(d)
        except OSError as e:
            print("[charge] {}的卷宗翻不动（{}），先跳过".format(h.NAME, e))
            slot(h, root, False)
            continue
        entry = slot(h, root, True)
        for s in got:
            files.append((entry, h, s["path"], s.get("project", ""), s["_mtime"]))
    return list(roll.values()), files


def _extract_declaration(text):
    """从一段 assistant 文本里找以「本场一件事：」开头的申报行，取冒号后的一句话。"""
    for raw in text.split("\n"):
        s = raw.strip().lstrip("#>*-").strip()
        if s.startswith(EVENT_MARKER):
            rest = s[len(EVENT_MARKER):].lstrip()
            # 兼容全角/半角冒号；没冒号也认（直接跟内容）
            rest = rest.lstrip("：:").strip()
            if rest:
                return rest[:200]
    return None


def scan_sessions(cfg=None, since=None, session_glob=""):
    """逐份流式扫卷宗，取「本场一件事」申报。会话从哪来见 _candidates。

    AI 说了什么、怎么从留痕里捡出来，各门派规矩不同，交给侠客的 ai_lines；这里只把 marker
    递给它当廉价闸门（原始行里没有 marker 的不解析），再从吐出来的话里找申报行。
    人那一侧的话 ai_lines 不吐：用户读设计文档、工具回显也会带出 marker，不算申报。

    since 非 None（朴素 datetime）时：先按 mtime 预筛（放宽一天，见 _PREFILTER_SLACK），
    再按 `_session_time(fp) >= since` 精判；窗外的整份不扫、不计数（total_files 也只算窗内）。
    since 为 None 时全扫。

    返回 dict：
      declared:   [{sid, hero, hero_name, project, decl, at, file}]   # 有申报的会话，每份取首条，按申报时刻排
      declared_count / undeclared_count / total_files: int
      by_hero:    [{hero, name, root, found, sessions, declared}]      # 各位侠客的点名，根不在的也列
    """
    roll, files = _candidates(cfg, session_glob)
    if since is not None:
        floor = (since - _PREFILTER_SLACK).timestamp()
        files = [f for f in files if f[4] >= floor]
        files = [f for f in files
                 if (t := _session_time(f[2])) is not None and t >= since]

    declared = {}   # (侠客, sid16) -> 申报
    for entry, h, fp, project, _mt in files:
        entry["sessions"] += 1
        key = (h.ID, _sid_of(fp))
        if key in declared:
            continue  # 同一位侠客同一个 sid 已从别的文件拿到申报（正常一 sid 一文件，防御性写法）
        found = None
        with closing(h.ai_lines(fp, EVENT_MARKER)) as said:
            for at, text in said:
                decl = _extract_declaration(text)
                if decl:
                    found = (at, decl)
                    break
        if found:
            entry["declared"] += 1
            declared[key] = {"sid": key[1], "hero": h.ID, "hero_name": h.NAME,
                             "project": project, "decl": found[1], "at": found[0] or "",
                             "file": Path(fp).name}

    out = sorted(declared.values(),
                 key=lambda d: _parse_ts(d["at"]) or datetime.datetime.max)
    return {
        "declared": out,
        "declared_count": len(out),
        "undeclared_count": max(0, len(files) - len(out)),
        "total_files": len(files),
        "by_hero": roll,
    }


# ── 组装回炉单 ────────────────────────────────────────────────────

def _fmt_ts(ts):
    return ts.strftime("%Y-%m-%d %H:%M") if ts else ""


def _span_days(a, b):
    return (b - a).days if (a and b) else 0


def build_charge_sheet(ledger, sessions, since=None):
    """把两根轴归集成一份结构化回炉单（纯数据，无任何判断结论）。

    since（朴素 datetime 或 None）只用于在头部标注生效窗口，不影响归集逻辑本身
    （过滤已在 scan_ledger / scan_sessions 里做完）。
    """
    comps = []
    for route, c in ledger["components"].items():
        comps.append({
            "component": route,
            "hits": c["hits"],
            "first_ts": _fmt_ts(c["first_ts"]),
            "last_ts": _fmt_ts(c["last_ts"]),
            "span_days": _span_days(c["first_ts"], c["last_ts"]),
            "files": sorted(c["files"]),
            "samples": c["samples"],
        })
    # 组件按命中次数降序；同次数按跨度降序——只是排序，不是价值判断
    comps.sort(key=lambda x: (x["hits"], x["span_days"]), reverse=True)

    rows = ledger["rows"]
    miss_rows = ledger["miss_rows"]
    opening_rows = ledger["opening_rows"]
    opening_miss_rows = ledger["opening_miss_rows"]
    # 密度只按开场口径算：中途 prompt 无路由是常态，混进分母只会虚高（2026-09-01 首炉修订）
    opening_miss_density = round(opening_miss_rows / opening_rows, 3) if opening_rows else 0.0

    # 候选淬火排序：确定性指标堆一起，给「建议看的先后」，不下「该不该淬火」的结论
    ranking = []
    for i, c in enumerate(comps, 1):
        ranking.append({
            "rank": i,
            "component": c["component"],
            "hits": c["hits"],
            "span_days": c["span_days"],
            # reason 是描述句，不是判断句——把数字念出来而已
            "reason": "命中 {} 次 · 跨度 {} 天 · 最近 {}".format(
                c["hits"], c["span_days"], c["last_ts"] or "—"),
        })

    span = ledger["span"]
    return {
        "generated_at": _now(),
        "window": {
            "since": since.strftime("%Y-%m-%d") if since else "",
            "ledger_from": _fmt_ts(span[0]),
            "ledger_to": _fmt_ts(span[1]),
            "ledger_rows": rows,
            "session_files": sessions["total_files"],
        },
        "by_component": comps,
        "misses": {
            "miss_rows": miss_rows,
            "opening_rows": opening_rows,
            "opening_miss_rows": opening_miss_rows,
            "opening_miss_density": opening_miss_density,
            "midsession_miss_rows": ledger["midsession_miss_rows"],
            "noise_dropped": ledger["noise_miss"],
            "samples": ledger["opening_misses"],
            "midsession_samples": ledger["midsession_misses"],
        },
        "events": {
            "declared": sessions["declared"],
            "declared_count": sessions["declared_count"],
            "undeclared_count": sessions["undeclared_count"],
            "total_sessions": sessions["total_files"],
            "by_hero": sessions["by_hero"],
        },
        "ranking": ranking,
        "_note": "确定性归集器产物：只数、只归、只排序。含金量判断留给反思炉与反思会话。",
    }


# ── 渲染人读版 ────────────────────────────────────────────────────

def render_md(sheet):
    w = sheet["window"]
    L = []
    L.append("# 回炉单 · {}".format(sheet["generated_at"]))
    L.append("")
    L.append("> 反思炉的进料清单。确定性归集，**不含任何含金量判断**——"
             "该淬火哪块、值不值得，看这单子的人（或炉子）自己定。")
    L.append(">")
    if w.get("since"):
        L.append("> **窗口：{} 起（--since）**——只统计该日期（含）之后的台账行与开场在该日期后的会话，"
                 "切掉窗前老账。".format(w["since"]))
        L.append(">")
    L.append("> 台账窗口：{} → {}（{} 行）｜session 转写：{} 份".format(
        w["ledger_from"] or "—", w["ledger_to"] or "—",
        w["ledger_rows"], w["session_files"]))
    L.append("")

    # 组件轴
    L.append("## 一、组件轴 —— 谁被用得最狠")
    L.append("")
    if sheet["by_component"]:
        L.append("| 组件 | 命中 | 跨度(天) | 最近命中 |")
        L.append("|:--|--:|--:|:--|")
        for c in sheet["by_component"]:
            L.append("| {} | {} | {} | {} |".format(
                c["component"], c["hits"], c["span_days"], c["last_ts"] or "—"))
        L.append("")
        for c in sheet["by_component"]:
            if not c["samples"]:
                continue
            L.append("**{}**（{} 次）代表性开场：".format(c["component"], c["hits"]))
            for s in c["samples"]:
                L.append("- {}".format(s))
            L.append("")
    else:
        L.append("_台账里没有任何命中记录。_")
        L.append("")

    # miss 区（开场/中途分层，2026-09-01 首炉修订）
    m = sheet["misses"]
    L.append("## 二、开场 miss 区 —— 该有设计到场却裸奔的开场")
    L.append("")
    L.append("窗内开场 **{}** 场：裸奔 **{}** 场 · 开场 miss 密度 **{}** · 已滤系统噪声 {} 条。".format(
        m["opening_rows"], m["opening_miss_rows"], m["opening_miss_density"], m["noise_dropped"]))
    L.append("")
    L.append("> 开场判定 = sid 在全量台账的首行。这些是回炉的重点矿："
             "开场没接到任何设计，可能是缺 route、也可能是卡的场景没覆盖。")
    L.append("")
    if m["samples"]:
        for s in m["samples"]:
            L.append("- {}".format(s))
    else:
        L.append("_没有采到裸奔开场（要么开场全命中，要么全是系统噪声）。_")
    L.append("")
    L.append("另有会话**中途** miss **{}** 条——中途 prompt 多数本就无需路由到场，"
             "仅供观察、不算裸奔（样本最多 {} 条）：".format(
                 m["midsession_miss_rows"], MIDSESSION_SAMPLES))
    L.append("")
    for s in m["midsession_samples"]:
        L.append("- {}".format(s))
    if not m["midsession_samples"]:
        L.append("_无中途 miss。_")
    L.append("")

    # 事轴
    e = sheet["events"]
    L.append("## 三、事轴 —— 本场一件事")
    L.append("")
    L.append("session 转写共 **{}** 份：有申报 **{}** 份，无申报 **{}** 份"
             "（契约 2026-09-01 上线，存量少是正常的）。".format(
                 e["total_sessions"], e["declared_count"], e["undeclared_count"]))
    L.append("")
    # 各位侠客点名：根不在的也列出来，少了哪家看得见，不静默
    for r in e["by_hero"]:
        if r["found"]:
            L.append("- {}：{} 份，有申报 {} 份（根 `{}`）".format(
                r["name"], r["sessions"], r["declared"], r["root"]))
        else:
            L.append("- {}：根不在这台机器上，跳过（`{}`）".format(r["name"], r["root"]))
    if e["by_hero"]:
        L.append("")
    if e["declared"]:
        L.append("| 侠客 | sid | 本场一件事 |")
        L.append("|:--|:--|:--|")
        for d in e["declared"]:
            # 申报里带半角竖线会把表格劈开，转义掉
            L.append("| {} | `{}` | {} |".format(
                d["hero_name"], d["sid"], d["decl"].replace("|", "\\|")))
    else:
        L.append("_还没有会话申报「本场一件事」。_")
    L.append("")

    # 候选淬火排序
    L.append("## 四、候选淬火排序（只排序，不下结论）")
    L.append("")
    L.append("> 按命中频次降序。频次高 = 用得狠 = 淬火收益面大；开场 miss 密度见第二节。"
             "**排序不等于结论**——最终看谁值得回炉，是反思会话的活。")
    L.append("")
    if sheet["ranking"]:
        for r in sheet["ranking"]:
            L.append("{}. **{}** —— {}".format(r["rank"], r["component"], r["reason"]))
    else:
        L.append("_无可排序的组件。_")
    L.append("")
    L.append("---")
    L.append("")
    L.append("_{}_".format(sheet["_note"]))
    L.append("")
    return "\n".join(L)


# ── 落盘 ──────────────────────────────────────────────────────────

def write_outputs(sheet, out_dir):
    d = Path(out_dir)
    if not d.is_absolute():
        d = ROOT / out_dir
    d.mkdir(parents=True, exist_ok=True)
    date = sheet["generated_at"].split(" ")[0]
    json_path = d / "charge-{}.json".format(date)
    md_path = d / "charge-{}.md".format(date)
    json_path.write_text(json.dumps(sheet, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(sheet), encoding="utf-8")
    # latest 指针：反思会话/炉子不必知道日期就能取到最新一单
    (d / "charge-latest.json").write_text(
        json.dumps(sheet, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "charge-latest.md").write_text(render_md(sheet), encoding="utf-8")
    return json_path, md_path


# ── 编排 ──────────────────────────────────────────────────────────

def generate(ledger_path, out_dir, since=None, cfg=None, session_glob=""):
    """cfg 是整份 config（认花名册用）；session_glob 是调试口，给了就不走花名册。"""
    ledger = scan_ledger(ledger_path, since=since)
    sessions = scan_sessions(cfg, since=since, session_glob=session_glob)
    sheet = build_charge_sheet(ledger, sessions, since=since)
    json_path, md_path = write_outputs(sheet, out_dir)
    return sheet, json_path, md_path


# ── 自测（临时目录造数据，不碰真实数据） ──────────────────────────

def selftest():
    print("[charge] selftest 开始 —— 全程临时目录，不读真实数据")
    tmp = Path(tempfile.mkdtemp(prefix="charge_selftest_"))
    ok = True

    def check(cond, name):
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print("  [{}] {}".format(mark, name))

    try:
        # 造台账：3 条命中 opening（同 sid 一部分）、2 条命中 deep-dig、
        # 2 条 miss（1 真开场 + 1 系统噪声）
        ledger = tmp / "ledger.jsonl"
        rows = [
            {"ts": "2026-09-01T09:00:00.0", "sid": "aaaa1111-bbbb-22",
             "project": "toys", "matched": ["opening"], "files": ["a.md"],
             "prompt_head": "帮我在 toys 弄个新想法"},
            {"ts": "2026-09-02T10:00:00.0", "sid": "aaaa1111-bbbb-22",
             "project": "toys", "matched": ["opening", "deep-dig"], "files": ["a.md", "b.md"],
             "prompt_head": "这段话拆一下深挖"},
            {"ts": "2026-09-03T11:00:00.0", "sid": "cccc3333-dddd-44",
             "project": "toys", "matched": ["opening"], "files": ["a.md"],
             "prompt_head": "开个新场景研究一下 X"},
            {"ts": "2026-09-04T12:00:00.0", "sid": "eeee5555-ffff-66",
             "project": "toys", "matched": ["deep-dig"], "files": ["b.md"],
             "prompt_head": "深挖这个决策"},
            {"ts": "2026-09-05T13:00:00.0", "sid": "eeee5555-ffff-66",
             "project": "toys", "matched": [], "files": [],
             "prompt_head": "随便问一句没被任何路由接住的话"},
            {"ts": "2026-09-05T15:00:00.0", "sid": "ffff7777-8888-99",
             "project": "toys", "matched": [], "files": [],
             "prompt_head": "第一句就裸奔的新开场"},
            {"ts": "2026-09-06T14:00:00.0", "sid": "9999aaaa-bbbb-cc",
             "project": "toys", "matched": [], "files": [],
             "prompt_head": "<task-notification>\n<task-id>xxx</task-id>"},
        ]
        ledger.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                          encoding="utf-8")

        # 造 session：一份带申报（assistant 文本首行）、一份用户 tool_result 里也有 marker
        # （不该被算成申报）、一份完全无 marker。放在一座 Claude 根的一个项目槽里，走花名册读
        cl1 = tmp / "claude1"
        sess_dir = cl1 / "d--Proj-toys"
        sess_dir.mkdir(parents=True)
        cfg1 = {"heroes": [{"id": "claude", "root": str(cl1)}]}

        def wl(fp, objs):
            fp.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n",
                          encoding="utf-8")

        # 文件名前 16 位 = 台账 sid，验证缝合
        wl(sess_dir / "aaaa1111-bbbb-2200-0000-000000000000.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "干个活"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "本场一件事：给 toys 加个想法登记入口\n\n开始动手"}]}},
        ])
        # 用户消息里出现 marker（读设计文档），不算申报
        wl(sess_dir / "cccc3333-dddd-4400-0000-000000000000.jsonl", [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "文档里写着 本场一件事 契约……"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "好的，我看看"}]}},
        ])
        # 完全无 marker
        wl(sess_dir / "eeee5555-ffff-6600-0000-000000000000.jsonl", [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "没有申报的一场"}]}},
        ])

        out = tmp / "out"
        sheet, jp, mp = generate(str(ledger), str(out), cfg=cfg1)

        # —— 断言 ——
        comp = {c["component"]: c for c in sheet["by_component"]}
        check(comp.get("opening", {}).get("hits") == 3, "opening 命中 3 次")
        check(comp.get("deep-dig", {}).get("hits") == 2, "deep-dig 命中 2 次")
        check(sheet["by_component"][0]["component"] == "opening", "排序：opening 居首（频次最高）")
        check(comp["opening"]["span_days"] == 2, "opening 跨度 2 天（09-01→09-03）")

        check(sheet["misses"]["miss_rows"] == 3, "miss 行 3 条（1 开场 + 1 中途 + 1 噪声）")
        check(sheet["misses"]["noise_dropped"] == 1, "系统噪声滤掉 1 条")
        check(sheet["misses"]["opening_rows"] == 4, "开场行 4 条（噪声开场不计）")
        check(sheet["misses"]["opening_miss_rows"] == 1, "开场 miss 仅 1 条")
        check(sheet["misses"]["opening_miss_density"] == 0.25, "开场 miss 密度 1/4")
        check(len(sheet["misses"]["samples"]) == 1
              and "裸奔的新开场" in sheet["misses"]["samples"][0], "开场 miss 样本正确")
        check(sheet["misses"]["midsession_miss_rows"] == 1
              and "没被任何路由" in sheet["misses"]["midsession_samples"][0],
              "中途 miss 单收（同 sid 第二行起算中途）")

        decl = {d["sid"]: d["decl"] for d in sheet["events"]["declared"]}
        check(sheet["events"]["declared_count"] == 1, "事轴：仅 1 份真申报")
        check("aaaa1111-bbbb-22" in decl, "申报按 sid 前 16 位缝合")
        check("想法登记入口" in decl.get("aaaa1111-bbbb-22", ""), "申报内容取冒号后一句")
        check("cccc3333-dddd-44" not in decl, "用户 tool_result 里的 marker 不算申报")
        check(sheet["events"]["total_sessions"] == 3, "共扫到 3 份 session")
        check(sheet["events"]["undeclared_count"] == 2, "无申报 2 份")

        check(jp.exists() and mp.exists(), "json / md 双版落盘")
        check((out / "charge-latest.md").exists(), "latest 指针落盘")
        check("回炉单" in mp.read_text(encoding="utf-8"), "md 含标题")
        # 不传 since 时头部不出现 --since 窗口行（逐字节兼容）
        check("--since" not in mp.read_text(encoding="utf-8"), "无 since 时头部不含窗口行")

        # —— 窗口过滤 --since 断言 ——
        # 台账：跨 08-29 → 09-02，外加一条无 ts 行。since=2026-08-31 应只留窗内。
        ledger2 = tmp / "ledger2.jsonl"
        rows2 = [
            {"ts": "2026-08-29T09:00:00.0", "sid": "old11111-aaaa-bb",
             "matched": ["opening"], "files": [],
             "prompt_head": "窗前老开场，应被切掉"},
            {"ts": "2026-08-30T09:00:00.0", "sid": "old22222-cccc-dd",
             "matched": [], "files": [],
             "prompt_head": "窗前 miss，应被切掉"},
            {"ts": "2026-09-01T09:00:00.0", "sid": "win33333-eeee-ff",
             "matched": ["opening"], "files": [],
             "prompt_head": "窗内开场 A"},
            {"ts": "2026-09-02T09:00:00.0", "sid": "win33333-eeee-ff",
             "matched": ["deep-dig"], "files": [],
             "prompt_head": "窗内 deep-dig"},
            {"ts": "2026-09-02T10:00:00.0", "sid": "old11111-aaaa-bb",
             "matched": [], "files": [],
             "prompt_head": "窗内 miss，但 sid 窗前已开场，应算中途"},
            {"ts": "2026-09-02T11:00:00.0", "sid": "win44444-9999-88",
             "matched": [], "files": [],
             "prompt_head": "窗内新开场裸奔"},
            {"sid": "nots5555-7777-66", "matched": ["opening"], "files": [],
             "prompt_head": "无 ts 行，since 下应被切掉"},
        ]
        ledger2.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows2) + "\n",
                           encoding="utf-8")

        # session：用顶层 timestamp 判窗；无 timestamp 的用 mtime 兜底
        cl2 = tmp / "claude2"
        sess2 = cl2 / "d--Proj-toys"
        sess2.mkdir(parents=True)
        cfg2 = {"heroes": [{"id": "claude", "root": str(cl2)}]}

        def wl2(name, objs, mtime=None):
            fp = sess2 / name
            fp.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n",
                          encoding="utf-8")
            if mtime is not None:
                os.utime(fp, (mtime, mtime))
            return fp

        DECL = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "本场一件事：窗内这件事"}]}}]
        NODECL = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "没申报的一场"}]}}]
        # sA：消息 timestamp 09-01（窗内）+ 有申报
        wl2("aaaawin1-0000-0000-0000-000000000000.jsonl",
            [{"type": "assistant", "timestamp": "2026-09-01T02:00:00.000Z",
              "message": {"role": "assistant", "content": [
                  {"type": "text", "text": "本场一件事：窗内这件事"}]}}])
        # sB：消息 timestamp 08-25（窗外）→ 整份剔除
        wl2("bbbbout1-0000-0000-0000-000000000000.jsonl",
            [{"type": "assistant", "timestamp": "2026-08-25T02:00:00.000Z",
              "message": {"role": "assistant", "content": [
                  {"type": "text", "text": "本场一件事：窗前老场"}]}}])
        # sC：无 timestamp，mtime 强设 08-20（窗外）→ 兜底剔除
        import time as _t
        t_out = _t.mktime(datetime.datetime(2026, 8, 20).timetuple())
        wl2("ccccout2-0000-0000-0000-000000000000.jsonl", NODECL, mtime=t_out)
        # sD：无 timestamp，mtime 强设 09-01（窗内）→ 兜底计入（无申报）
        t_in = _t.mktime(datetime.datetime(2026, 9, 1).timetuple())
        wl2("ddddwin2-0000-0000-0000-000000000000.jsonl", NODECL, mtime=t_in)

        since = datetime.datetime(2026, 8, 31)
        out2 = tmp / "out2"
        s2, _, _ = generate(str(ledger2), str(out2), since=since, cfg=cfg2)

        # 组件轴 + miss 区都按窗过滤
        comp2 = {c["component"]: c for c in s2["by_component"]}
        check(s2["window"]["ledger_rows"] == 4, "窗内台账行 4 条（切掉 2 窗前 + 1 无 ts）")
        check(comp2.get("opening", {}).get("hits") == 1, "窗内 opening 仅 1 次（窗前那条被切）")
        check(comp2.get("deep-dig", {}).get("hits") == 1, "窗内 deep-dig 1 次")
        check(s2["misses"]["opening_miss_rows"] == 1
              and "新开场裸奔" in s2["misses"]["samples"][0], "窗内开场 miss 仅 1 条")
        check(s2["misses"]["midsession_miss_rows"] == 1
              and "应算中途" in s2["misses"]["midsession_samples"][0],
              "first-seen 跨窗：窗前开的场，窗内 miss 算中途")
        check(s2["misses"]["opening_rows"] == 2, "窗内开场 2 场（开场A + 新开场裸奔）")
        check(s2["window"]["since"] == "2026-08-31", "window.since 记录生效日期")

        # 事轴：消息 ts 判窗 + mtime 兜底
        check(s2["events"]["total_sessions"] == 2, "窗内 session 2 份（sB 消息ts窗外/sC mtime窗外 均剔除）")
        check(s2["events"]["declared_count"] == 1, "窗内申报 1 份（sA）")
        check(s2["events"]["undeclared_count"] == 1, "窗内无申报 1 份（sD，mtime 兜底计入）")

        # md 头部出现窗口行
        check("--since" in (out2 / "charge-latest.md").read_text(encoding="utf-8"),
              "since 下 md 头部含窗口行")

        # 同数据不传 since：行为回到全量（不变）
        out3 = tmp / "out3"
        s3, _, _ = generate(str(ledger2), str(out3), cfg=cfg2)
        comp3 = {c["component"]: c for c in s3["by_component"]}
        check(s3["window"]["ledger_rows"] == 7, "不传 since：台账全量 7 行")
        check(comp3.get("opening", {}).get("hits") == 3, "不传 since：opening 全量 3 次")
        check(s3["misses"]["miss_rows"] == 3, "不传 since：miss 全量 3 条")
        check(s3["events"]["total_sessions"] == 4, "不传 since：session 全量 4 份")
        check(s3["window"]["since"] == "", "不传 since：window.since 为空")

        # —— 花名册多根：Claude 两个项目槽 + Codex 按日期分层 ——
        # 申报要两家都进事轴、侠客标对；--since 对两家都切；根不在跳过不炸
        from heroes import claude as hc, codex as hx

        def wjl(fp, objs, mtime=None):
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n",
                          encoding="utf-8")
            if mtime is not None:
                os.utime(fp, (mtime, mtime))
            return fp

        def c_ai(ts, text):
            return {"type": "assistant", "timestamp": ts,
                    "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

        cl3 = tmp / "claude3"
        wjl(cl3 / "d--Proj-alpha" / "1111aaaa-0000-4000-8000-000000000001.jsonl", [
            {"type": "user", "timestamp": "2026-09-02T01:00:00.000Z",
             "message": {"role": "user", "content": "本场一件事：他贴进来的，不算"}},
            c_ai("2026-09-02T01:00:05.000Z", "本场一件事：Claude 甲槽的事 | 带竖线\n开始")])
        wjl(cl3 / "d--Proj-beta" / "2222bbbb-0000-4000-8000-000000000002.jsonl", [
            c_ai("2026-09-03T01:00:00.000Z", "本场一件事：Claude 乙槽的事")])
        # 消息时刻在窗前（mtime 是今天）：预筛放过，精判剔除
        wjl(cl3 / "d--Proj-beta" / "3333cccc-0000-4000-8000-000000000003.jsonl", [
            c_ai("2026-08-20T01:00:00.000Z", "本场一件事：窗前的 Claude 场")])
        # mtime 也在窗前：预筛就剔除
        t_old = _t.mktime(datetime.datetime(2026, 8, 10).timetuple())
        wjl(cl3 / "d--Proj-beta" / "4444dddd-0000-4000-8000-000000000004.jsonl", [
            c_ai("2026-08-10T01:00:00.000Z", "本场一件事：更老的 Claude 场")], mtime=t_old)

        cx3 = tmp / "codex3"
        cx_sid = "01a0aaaa-1111-7222-8333-944445555666"
        cx_file = cx3 / "2026" / "09" / "04" / "rollout-2026-09-04T10-00-00-{}.jsonl".format(cx_sid)
        wjl(cx_file, [
            {"timestamp": "2026-09-04T02:00:00.000Z", "type": "session_meta",
             "payload": {"id": cx_sid, "timestamp": "2026-09-04T02:00:00.000Z", "cwd": "d:\\Proj\\gamma"}},
            {"timestamp": "2026-09-04T02:00:01.000Z", "type": "response_item", "payload": {
                "type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "## 0. 先申报「本场一件事」\n本场一件事：开场卡里的样例，不算"}]}},
            {"timestamp": "2026-09-04T02:00:02.000Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "本场一件事：他贴的旧对话，不算"}]}},
            # 同一句话 event_msg 先记了一遍：只认 response_item，否则会先认到这份
            {"timestamp": "2026-09-04T02:00:03.000Z", "type": "event_msg", "payload": {
                "type": "item_completed", "item": {"role": "assistant", "content": [
                    {"type": "Text", "text": "本场一件事：event_msg 里那份，不该先被认"}]}}},
            {"timestamp": "2026-09-04T02:00:03.000Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "本场一件事：Codex 这场的事\n阶段：执行"}]}},
        ])
        wjl(cx3 / "2026" / "09" / "05" / "rollout-2026-09-05T10-00-00-01a0cccc-2222-7333-8444-955556666777.jsonl", [
            {"timestamp": "2026-09-05T02:00:00.000Z", "type": "session_meta", "payload": {"cwd": "d:\\Proj\\gamma"}},
            {"timestamp": "2026-09-05T02:00:01.000Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "这场没申报"}]}},
        ])
        wjl(cx3 / "2026" / "08" / "20" / "rollout-2026-08-20T10-00-00-01a0bbbb-3333-7444-8555-966667777888.jsonl", [
            {"timestamp": "2026-08-20T02:00:00.000Z", "type": "session_meta", "payload": {"cwd": "d:\\Proj\\gamma"}},
            {"timestamp": "2026-08-20T02:00:01.000Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "本场一件事：窗前的 Codex 场"}]}},
        ])

        # 侠客的 ai_lines 只吐 AI 说的话
        cx_said = list(hx.ai_lines(cx_file))
        check(len(cx_said) == 1 and cx_said[0][1].startswith("本场一件事：Codex 这场的事")
              and cx_said[0][0] == "2026-09-04T02:00:03.000Z",
              "codex.ai_lines 只吐 assistant 的 output_text（developer / user / event_msg 都不吐），带时刻")
        cl_said = list(hc.ai_lines(sess_dir / "cccc3333-dddd-4400-0000-000000000000.jsonl"))
        check([t for _, t in cl_said] == ["好的，我看看"], "claude.ai_lines 只吐 assistant 的 text")
        check(list(hc.ai_lines(sess_dir / "cccc3333-dddd-4400-0000-000000000000.jsonl",
                               EVENT_MARKER)) == [], "ai_lines 的 needle：原始行没有它的不解析")
        check(list(hx.ai_lines(tmp / "nope.jsonl")) == [], "ai_lines 文件不在：空，不炸")

        cfg3 = {"heroes": [{"id": "claude", "root": str(cl3)}, {"id": "codex", "root": str(cx3)}]}
        s4, _, mp4 = generate(str(ledger2), str(tmp / "out4"), since=since, cfg=cfg3)
        ev4 = s4["events"]
        d4 = {d["decl"]: d for d in ev4["declared"]}
        hero4 = {r["hero"]: r for r in ev4["by_hero"]}
        check(ev4["declared_count"] == 3, "多根 + since：申报 3 份（Claude 两槽各 1 + Codex 1）")
        check({d["hero"] for d in ev4["declared"]} == {"claude", "codex"}, "两位侠客的申报都进事轴")
        cxd = d4.get("Codex 这场的事") or {}
        check(cxd.get("hero") == "codex" and cxd.get("hero_name") == "Codex",
              "Codex 申报标的是 Codex（取的是 response_item 那份，不是 event_msg / user / developer）")
        check(cxd.get("sid") == cx_sid[:SID_KEY_LEN], "Codex 的 sid 取文件名尾巴上的 uuid 前 16 位")
        check(cxd.get("project") == "d:/Proj/gamma", "Codex 申报带出身（session_meta 的 cwd）")
        check((d4.get("Claude 甲槽的事 | 带竖线") or {}).get("hero_name") == "Claude Code"
              and "Claude 乙槽的事" in d4, "Claude 两个项目槽的申报都进来，标 Claude Code")
        check("他贴进来的，不算" not in d4, "Claude 用户轮里的 marker 不算申报")
        check("窗前的 Claude 场" not in d4 and "更老的 Claude 场" not in d4,
              "since 切 Claude：消息时刻窗前的剔除（mtime 预筛 + 精判两条路）")
        check("窗前的 Codex 场" not in d4, "since 切 Codex：消息时刻窗前的剔除")
        check(hero4["claude"]["sessions"] == 2 and hero4["claude"]["declared"] == 2,
              "点名：Claude 窗内 2 份、有申报 2 份")
        check(hero4["codex"]["sessions"] == 2 and hero4["codex"]["declared"] == 1,
              "点名：Codex 窗内 2 份、有申报 1 份")
        check(ev4["total_sessions"] == 4 and ev4["undeclared_count"] == 1, "多根：窗内 4 份、无申报 1 份")
        md4 = mp4.read_text(encoding="utf-8")
        check("| 侠客 | sid | 本场一件事 |" in md4 and "| Codex | `{}` |".format(cx_sid[:16]) in md4
              and "| Claude Code |" in md4, "md 事轴表多一列「侠客」")
        check("带竖线" in md4 and "\\| 带竖线" in md4, "申报里的半角竖线转义，不劈表格")

        s5, _, _ = generate(str(ledger2), str(tmp / "out5"), cfg=cfg3)
        h5 = {r["hero"]: r for r in s5["events"]["by_hero"]}
        check(h5["claude"]["sessions"] == 4 and h5["claude"]["declared"] == 4
              and h5["codex"]["sessions"] == 3 and h5["codex"]["declared"] == 2,
              "不传 since：两家全量（Claude 4/4、Codex 3/2）")

        # 根不在：跳过、点名里照实记，不炸
        cfg6 = {"heroes": [{"id": "claude", "root": str(cl3)},
                           {"id": "codex", "root": str(tmp / "no-such-codex")}]}
        s6, _, mp6 = generate(str(ledger2), str(tmp / "out6"), since=since, cfg=cfg6)
        h6 = {r["hero"]: r for r in s6["events"]["by_hero"]}
        check(h6["codex"]["found"] is False and h6["codex"]["sessions"] == 0
              and h6["claude"]["declared"] == 2, "Codex 根不在：跳过，Claude 照常")
        check("根不在这台机器上" in mp6.read_text(encoding="utf-8"), "md 点名里写出根不在的那家")
        cfg7 = {"heroes": [{"id": "claude", "root": str(tmp / "nope1")},
                           {"id": "codex", "root": str(tmp / "nope2")}]}
        s7, _, _ = generate(str(ledger2), str(tmp / "out7"), cfg=cfg7)
        check(s7["events"]["total_sessions"] == 0
              and all(not r["found"] for r in s7["events"]["by_hero"]),
              "两家的根都不在：事轴空，回炉单照出")

        # 调试口 --sessions：只扫给的目录，按路径认侠客
        s8, _, _ = generate(str(ledger), str(tmp / "out8"), session_glob=str(sess_dir))
        check(s8["events"]["declared_count"] == 1 and s8["events"]["total_sessions"] == 3
              and s8["events"]["declared"][0]["hero"] == "claude", "--sessions 调试口照旧可用")

    finally:
        # 临时目录清干净，不留垃圾
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("[charge] selftest {}".format("全绿" if ok else "有 FAIL"))
    return ok


# ── 交付报告 ──────────────────────────────────────────────────────

def _print_report(sheet, jp, mp):
    e = sheet["events"]
    m = sheet["misses"]
    print("")
    print("=== 回炉单已出炉 ===")
    if sheet["window"].get("since"):
        print("窗口：{} 起（--since）".format(sheet["window"]["since"]))
    print("落盘：{}".format(jp))
    print("      {}".format(mp))
    print("组件轴：{} 个组件被命中".format(len(sheet["by_component"])))
    if sheet["ranking"]:
        top = sheet["ranking"][0]
        print("        最热：{}（{} 次）".format(top["component"], top["hits"]))
    print("miss 区：开场 {}/{} 裸奔 · 密度 {} · 中途 miss {} 条 · 滤噪声 {} 条".format(
        m["opening_miss_rows"], m["opening_rows"], m["opening_miss_density"],
        m["midsession_miss_rows"], m["noise_dropped"]))
    print("事  轴：{} 份 session，{} 份有「本场一件事」申报，{} 份无".format(
        e["total_sessions"], e["declared_count"], e["undeclared_count"]))
    for r in e["by_hero"]:
        if r["found"]:
            print("        {}：{} 份，有申报 {} 份".format(r["name"], r["sessions"], r["declared"]))
        else:
            print("        {}：根不在，跳过（{}）".format(r["name"], r["root"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="回炉单生成器 · 炉子自己的攒料器（确定性归集，无 LLM）")
    ap.add_argument("--selftest", action="store_true", help="临时目录造数据自测，不碰真实数据")
    ap.add_argument("--since", default="", metavar="YYYY-MM-DD",
                    help="只统计该日期（含）起的台账行与该日期后开场的会话，切掉窗前老账")
    ap.add_argument("--ledger", default="", help="覆盖 config 的 hook 路由台账路径")
    ap.add_argument("--sessions", default="",
                    help="调试用：只扫这个会话目录或 glob，不走侠客花名册")
    ap.add_argument("--out", default="", help="覆盖 config 的回炉单落盘目录")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1

    since = None
    if args.since:
        since = _parse_since(args.since)
        if since is None:
            print("[charge] --since 格式应为 YYYY-MM-DD（收到：{}）".format(args.since))
            return 2

    cfg, whole = read_charge_config()
    ledger_path = args.ledger or cfg["ledger_path"]
    out_dir = args.out or cfg["out_dir"] or "data/charge"

    if not ledger_path:
        print("[charge] 缺路径：请在 config.json 的 charge_sheet 段填 ledger_path，或用 --ledger 覆盖。")
        return 2

    print("[charge] 台账：{}".format(ledger_path))
    if args.sessions:
        print("[charge] 会话：{}（--sessions，不走花名册）".format(args.sessions))
    else:
        for h, root in heroes.enabled(whole):
            print("[charge] 会话：{} · {}".format(h.NAME, root))
    if since:
        print("[charge] 窗口：{} 起（--since）".format(args.since))
    sheet, jp, mp = generate(ledger_path, out_dir, since=since, cfg=whole,
                             session_glob=args.sessions)
    _print_report(sheet, jp, mp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
