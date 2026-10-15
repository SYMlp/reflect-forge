"""侠客 —— 执剑的人，各有各的门派。

锻造台只认「铁」，不认铁是谁打回来的。可炉子要自己去旧账里寻料，
就绕不开一件事：每个侠客留下的卷宗，格式和归档规矩都不一样。

    Claude Code   ~/.claude/projects/<项目slug>/<会话uuid>.jsonl
    Codex         ~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl

这个包把「门派差异」全关在各自的模块里，对外只露五件事：

    scrolls(root)      寻料 —— 翻出这位侠客的全部卷宗，每份标出身与日子
    to_text(path,lim)  出铁 —— 卷宗 → 可入炉的纯文本（只留人说了什么、AI 答了什么）
    head(path, n)      验矿 —— 只掀开头几页，替锻造师省一簇火
    user_turns(path)   补原话 —— 只留「他开口的话」[(jsonl 行号, 文本)]，炼料时从里面找原话锚
    ai_lines(path,needle)  回炉单 —— 逐条吐「AI 说的话」(时刻, 文本)，流式读；回炉单从里面找「本场一件事：」申报

外加一个 owns(path)：前端把路径原样传回来入炉时，靠它认出该请哪位侠客。

加一位新侠客 = 加一个模块 + 在 ROSTER 里挂个名。炉子一个字不用改。
"""
from . import claude, codex

# 花名册。顺序即寻料时的呈现顺序；config.json 的 heroes 可以只启用其中几位。
ROSTER = (claude, codex)


def by_id(hid):
    for h in ROSTER:
        if h.ID == hid:
            return h
    return None


def enabled(cfg):
    """→ [(侠客模块, 卷宗根目录)]。

    config.json 的 `heroes` 说了算；没写就是花名册上全体到齐、各用各的默认根。
    老配置里的 `session_dir` 仍然管用——它是 Claude 的根，因为这炉子出生时只认它一位。
    """
    out = []
    conf = cfg.get("heroes")
    if not conf:
        conf = [{"id": h.ID} for h in ROSTER]
    for item in conf:
        if isinstance(item, str):
            item = {"id": item}
        h = by_id(item.get("id"))
        if not h or item.get("off"):
            continue
        root = item.get("root")
        if not root and h.ID == "claude":
            root = cfg.get("session_dir")
        out.append((h, root or h.DEFAULT_ROOT))
    return out


def whose(path):
    """卷宗 → 哪位侠客留的。认不出就交给花名册头一位（老卷宗都是 Claude 的）。"""
    for h in ROSTER:
        if h.owns(path):
            return h
    return ROSTER[0]
