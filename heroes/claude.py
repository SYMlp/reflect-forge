"""侠客·Claude Code —— 这炉子出生时认的第一位。

卷宗归档在 `~/.claude/projects/<项目slug>/<会话uuid>.jsonl`，
项目出身压在目录名里（有损，得照磁盘贪心还原）。
"""
import datetime
import json
import re
from pathlib import Path

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
