"""侠客·Claude Code —— 这炉子出生时认的第一位。

卷宗归档在 `~/.claude/projects/<项目slug>/<会话uuid>.jsonl`，
项目出身压在目录名里（有损，得照磁盘贪心还原）。
"""
import datetime
import json
import re
from pathlib import Path

from ._said import ai_drafted, machine_open, unpasted

ID = "claude"
NAME = "Claude Code"
DEFAULT_ROOT = "~/.claude/projects/"


def owns(path):
    p = str(path).replace("\\", "/")
    return "/.claude/projects/" in p


def prettify_slug(slug):
    r"""矿脉出身。目录名是把真实路径的分隔符压成 '-' 得来的
    （D:\Project\personal\toys → d--Project-personal-toys）。这个压缩有损——
    项目名自带的连字符也变成了 '-'，光看字符串还不回去（reflect-forge 会被劈成两截）。
    所以照着磁盘贪心还原：每一层取「能对上真实目录的最长名字」。还原不上就退回粗切分，
    出身写歪一点也比没有强。"""
    m = re.match(r"^([a-zA-Z])--(.+)$", slug)
    if not m:
        return slug.strip("-").replace("-", "/") or slug
    parts = m.group(2).split("-")
    cur, out, i = Path(m.group(1).upper() + ":\\"), [], 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            cand = "-".join(parts[i:j])
            if (cur / cand).is_dir():
                out.append(cand)
                cur, i = cur / cand, j
                break
        else:
            out.extend(parts[i:])
            break
    return "/".join(out)


def scrolls(root):
    """矿场是整座江湖：根下一层是各项目的 slug 目录，jsonl 躺在里面。"""
    out = []
    for sub in Path(root).iterdir():
        if not sub.is_dir():
            continue
        project = prettify_slug(sub.name)
        for f in sub.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({
                "file": f.name,
                "path": str(f),
                "project": project,
                "hero": ID,
                "date": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                "size_kb": round(st.st_size / 1024, 1),
                "_mtime": st.st_mtime,
            })
    return out


def _speech(ev):
    """一条留痕 → [(谁, 说了什么)]。不是话的（工具、元信息）一律不出声。"""
    msg = ev.get("message") or {}
    role = msg.get("role") or ev.get("type")
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return [(role, content.strip())]
    if isinstance(content, list):
        return [(role, b["text"].strip()) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
                and b.get("text", "").strip()]
    return []


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


# ── 补原话：只留他开口的话 ───────────────────────────────────────
# role=user 的留痕里一大半不是他打的字：宿主塞的提醒、命令回显、IDE 选区、粘进来的长文。
# 原话锚要能指回「他当时那句话」，这些混进来，模型就会从提醒里截一句当他的原话。
# 规矩是 toys session-archaeology/tagger.py 的 user_turns 简版——公开仓不能 import 私仓

# 成对的注入标签区：整段删掉，删完剩下的才是他打的字（他常在提醒后面接着说话）。
# 收尾标签也会带属性（实测 `</pasted_content id="0625">`），收尾一样放过 [^>]*
_INJECTED = re.compile(
    r"<(system-reminder|pasted_content|command-[\w-]+|local-command-[\w-]+|ide_[\w-]+"
    r"|user-prompt-submit-hook|bash-[\w-]+)\b[^>]*>.*?</\1\b[^>]*>", re.S)
# hook 往 user 轮里写的回执，带着这些字样的整块都不是人话
_HOOK_ECHO = ("UserPromptSubmit hook", "additional context", "已注入:", "命中: [")


def _said_blocks(ev):
    if ev.get("type") != "user" or ev.get("isMeta") or "toolUseResult" in ev:
        return []
    c = (ev.get("message") or {}).get("content")
    if isinstance(c, str):
        return [c]
    return [b.get("text") or "" for b in (c or [])
            if isinstance(b, dict) and b.get("type") == "text"]


def user_turns(path):
    out = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            for no, line in enumerate(fh, 1):
                if '"user"' not in line:     # 先粗筛，大卷宗里大半是 AI 和工具的留痕
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                for b in _said_blocks(ev):
                    b = _INJECTED.sub("", b).strip()
                    if (not b or b.startswith(("<", "[Request interrupted"))
                            or any(k in b for k in _HOOK_ECHO)):
                        continue
                    b = unpasted(b)
                    if not b:
                        continue
                    if not out and machine_open(b):
                        return []
                    if ai_drafted(b):
                        continue
                    out.append((no, b))
    except OSError:
        return []
    return out


# ── 回炉单：只留 AI 说的话 ───────────────────────────────────────
# 事轴要从 AI 的回复里找「本场一件事：」申报。人那一侧不算：他贴设计文档、工具回显都会带出这几个字
def ai_lines(path, needle=""):
    """→ 逐条吐 (时刻, AI 说的一段话)。时刻是留痕上原样的 timestamp 串，没有就是 None。

    逐行流式读，十几 MB 的卷宗也不整份载入。needle 给了，原始行里没有它的直接跳过、不解析：
    一份卷宗里含申报字样的往往只有几行。
    行里有 \\u 转义的照样解析：中文写成 \\uXXXX 的留痕，原始行里找不到中文字样（10-15 Codex 复核实测）。"""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if needle and needle not in line and "\\u" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                for role, text in _speech(ev):
                    if role == "assistant":
                        yield ev.get("timestamp"), text
    except OSError:
        return


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
            out.append(line.strip()[:300])
            continue
        for role, text in _speech(ev):
            out.append("[{}] {}".format(role, text[:cap]))
    return "\n".join(out)
