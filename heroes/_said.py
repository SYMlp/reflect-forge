"""「他开口的话」里与门派无关的两条规矩，claude / codex 的 user_turns 共用。

不是侠客，不进花名册。规矩是 toys session-archaeology 的简版——公开仓不能 import 私仓。
"""
import re

# 粘进来的别人的话：他在开头写了头标，说明这块不是他说的。
# 「外部资料」「外部資料」「公众号文章」是实测他真写过的同义头标（真实会话里三种都有）
PASTED_HEADS = ("外部材料", "外部资料", "外部資料", "公众号文章",
                "讲座转写", "豆包起草", "豆包对抗", "机器回执", "录音对话")
# 机器任务书开的场（锻造台、审计器、子分身、handoff 派下来的活）：整场都不是他在说
# 只认角色剧本「你是锻造台的执行枝」「你是「反思锻造台」的锻剑炉」，不认他的问句：
# 「你是不是把需求和实现混了？」开场，整场他的话都会被当成机器任务书丢光（10-15 Codex 复核实测）
MACHINE_OPEN = re.compile(r"^(#+\s*)?你是(?!不|否|谁|怎|咋|什么|说|在|真|要|想|觉得|对|错|指|哪|故意)")


def machine_open(block):
    first = re.split(r"[\n。]", block, maxsplit=1)[0]
    return bool(MACHINE_OPEN.match(block)) and "？" not in first and "?" not in first


QUOTE3 = '"""'
# AI 写好、他粘进来的：派活任务书（实测「# 评审视角：表达扫描」一万多字，整场就这一句）、
# 上一场收尾起草的新场开场白（「本场一件事：…」，他的原话只是夹在【】里被引用）。
# 炼料拿它当原话来源，会把 AI 的句子截成锚（10-15 验收实测）。错杀他自己打的一句，这条料最多落到
# 「他认」、座位有上限；放过了，假锚进剑、没人兜——所以宁可多杀（④）。
# 只管 user_turns（原话锚）；to_text 喂反思炉要的是整场的活，派下来的任务书照留（见 codex.py 文首）
TASKBOOK = re.compile(r"^#{1,6}\s")
TASKBOOK_MIN = 200
OPENER = "本场一件事"


def ai_drafted(block):
    return block.startswith(OPENER) or (len(block) >= TASKBOOK_MIN and bool(TASKBOOK.match(block)))


def unpasted(block):
    """头标块 → 剩下他自己说的话。

    头标那一行就用三引号包住的，包住的是别人的话：删掉那段，留下他围着它说的
    （实测「外部資料：\"\"\"<文章>\"\"\" 按照这篇焚诀审查……」，后半句正是他的要求）。
    没有包裹就整块算粘进来的。不带头标的三引号不动：他的豆包翻译也常用三引号包。
    包裹的收尾认最后一个三引号：粘进来的文章自己会带三引号（实测一篇里的 Python docstring 有 14 个）。
    """
    first = block.split("\n", 1)[0].lstrip("#").strip().lstrip("【[").strip()
    if not first.startswith(PASTED_HEADS):
        return block
    a, b = block.find(QUOTE3), block.rfind(QUOTE3)
    if a < 0 or b <= a or "\n" in block[:a].strip():
        return ""
    return block[b + 3:].strip()
