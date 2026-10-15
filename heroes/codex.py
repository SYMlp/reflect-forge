"""侠客·Codex —— 第二位入册的。

卷宗归档在 `~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl`，
按日子堆，不按项目堆。所以出身不在路径里——在卷宗头一条 `session_meta` 的 `cwd` 里，
直接写着，不用像 Claude 那样照磁盘贪心还原。

两处门派差异，是这个模块存在的全部理由：

1. 留痕外面裹了一层 `payload`：`{"type":"response_item","payload":{"type":"message",...}}`
2. 话分两种块：人说的是 `input_text`，AI 答的是 `output_text`（Claude 两边都叫 `text`）

还有一条不写下来就会踩的：`role: user` 里混着一大堆不是人打的东西。实测 25 份卷宗的
user 留痕，只有一半出自真人，另一半是灌进去的：`<environment_context>` / `<recommended_plugins>`
/ `<in-app-browser-context>` 这类 XML 式注入，外加整份 `# AGENTS.md instructions for <项目>` 规则。
这些喂进炉子只会出假铁——炉子会把项目规约当成锻造师的判断给你锻出来。

但**不能见 `#` 就滤**：`# 细选批 · 批 8 派工单` / `# 任务：子 agent 可见性的呈现层改造`
这些同样是 `#` 开头，却正是派下来的活，是这炉料里最该留的部分。所以只认死两类：
尖括号开头的整段，和 AGENTS.md 规则注入。宁可漏滤一点噪声，不可错滤一句真话。

（`role: developer` 整条都是系统灌的 skills 清单与记忆索引，不必细分，一律不出声。）
"""
import datetime
import json
from pathlib import Path

from ._said import MACHINE_OPEN, ai_drafted, unpasted

ID = "codex"
NAME = "Codex"
DEFAULT_ROOT = "~/.codex/sessions/"

# 灌进去的、不是人打的。只认死这两类，见 模块 docstring 的「不能见 # 就滤」
_INJECT_HEADS = ("<", "# AGENTS.md instructions")


def owns(path):
    p = str(path).replace("\\", "/")
    return "/.codex/sessions/" in p or Path(p).name.startswith("rollout-")


def _meta(path, max_lines=40):
    """卷宗头几行里捞 session_meta。捞不着就算了，出身写「(不详)」也比崩了强。"""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") == "session_meta":
                    return r.get("payload") or {}
    except OSError:
        pass
    return {}


def _project_of(meta):
    cwd = meta.get("cwd") or ""
    if not cwd:
        return "(不详)"
    return cwd.replace("\\", "/").rstrip("/")


def scrolls(root):
    """矿场按日子分层：<root>/YYYY/MM/DD/rollout-*.jsonl。一路翻到底收齐。

    子 agent 的卷宗也在这堆里（session_meta.thread_source == "subagent"），
    照收不误——记录侧不设「值不值得记」的门，判断是锻造师的事，不是矿灯的事。
    """
    out = []
    for f in sorted(Path(root).rglob("rollout-*.jsonl")):
        try:
            st = f.stat()
        except OSError:
            continue
        meta = _meta(f)
        project = _project_of(meta)
        agent = meta.get("agent_nickname")
        out.append({
            "file": f.name,
            "path": str(f),
            "project": project + ("（{}）".format(agent) if agent else ""),
            "hero": ID,
            "date": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
            "size_kb": round(st.st_size / 1024, 1),
            "_mtime": st.st_mtime,
        })
    return out


def _speech(ev):
    """一条留痕 → [(谁, 说了什么)]。工具调用、推理、token 账一律不出声。"""
    if ev.get("type") != "response_item":
        return []
    p = ev.get("payload") or {}
    if p.get("type") != "message":
        return []
    role = p.get("role")
    if role == "developer":          # 灌进去的系统指令，不是人说的话
        return []
    said = []
    for b in (p.get("content") or []):
        if not isinstance(b, dict) or b.get("type") not in ("input_text", "output_text"):
            continue
        t = (b.get("text") or "").strip()
        if not t or t.startswith(_INJECT_HEADS):
            continue
        said.append((role, t))
    return said


def to_text(path, limit=24000):
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        for role, text in _speech(ev):
            out.append("[{}] {}".format(role, text[:1200]))
    return "\n".join(out)


def head(path, n=200, cap=400):
    out = []
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        lines = [line for i, line in enumerate(fh) if i < n]
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        for role, text in _speech(ev):
            out.append("[{}] {}".format(role, text[:cap]))
    return "\n".join(out)


# ── 补原话：只留他开口的话 ───────────────────────────────────────
# 读法同 _speech（role=user 的 input_text，跳过 _INJECT_HEADS），另加三条与门派无关的（见 _said）：
# 头标标明是粘进来的别人的话；机器任务书开的场（handoff 派下来的活）整场不算他说的；AI 写的任务书、开场白不算他说的
def user_turns(path):
    out = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            for no, line in enumerate(fh, 1):
                if '"response_item"' not in line or '"user"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                p = ev.get("payload") or {}
                if ev.get("type") != "response_item" or p.get("type") != "message" \
                        or p.get("role") != "user":
                    continue
                for b in (p.get("content") or []):
                    if not isinstance(b, dict) or b.get("type") != "input_text":
                        continue
                    t = (b.get("text") or "").strip()
                    if not t or t.startswith(_INJECT_HEADS):
                        continue
                    t = unpasted(t)
                    if not t:
                        continue
                    if not out and MACHINE_OPEN.match(t):
                        return []
                    if ai_drafted(t):
                        continue
                    out.append((no, t))
    except OSError:
        return []
    return out
