"""反思锻造台 Reflect Forge —— 后端本体。

打工人的经验不随下班蒸发，而是长成他自己带得走的资产。

铁 = session 里真正有价值的精华（判断 / 经验 / 流程）
剑 = 锻出来的 SKILL.md
火 = token

设计权威：docs/DESIGN.md（术语表 §1 / API 契约 §4 / 等级规则 §5）
跑法：python main.py  → http://localhost:7712
"""
import datetime
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
PROMPTS = ROOT / "forge_prompts"
DATA = ROOT / "data"
IRONS = DATA / "irons"
SWORDS = DATA / "swords"
PROFILE_FILE = DATA / "profile.json"
CONFIG_FILE = ROOT / "config.json"
LOG_FILE = ROOT / "forge.log"
PORT = 7712  # 7711 归前身 component-forge，两台炉子可同时开

for d in (DATA, IRONS, SWORDS):
    d.mkdir(parents=True, exist_ok=True)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def log(msg):
    """炉火留痕。现场 demo 出问题时，这份日志是唯一能回看的东西。"""
    line = "[{}] {}".format(_now(), msg)
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── config / profile ──────────────────────────────────────────────

DEFAULT_CONFIG = {
    "forge_master": "锻造师",
    "works_slain_base": 0,
    "session_dir": "~/.claude/projects/",
    "prospect_banner": "",
    "featured_scroll": {},
}


def read_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log("config.json 读不动（{}），先用默认值跑".format(e))
    return cfg


def read_profile():
    """profile.json 只存**算不出来的那部分**：名字，和 forge 累加的斩活增量。

    四维经验值一律现算（数数据目录），不落盘——落盘就会和真实产物对不上，
    而这个产品的整个立论就是「经验值来自真实锻造记录」。
    """
    prof = {"name": read_config()["forge_master"], "works_slain_delta": 0}
    if PROFILE_FILE.exists():
        try:
            prof.update(json.loads(PROFILE_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log("profile.json 读不动（{}），按初始档案跑".format(e))
    return prof


def write_profile(prof):
    PROFILE_FILE.write_text(json.dumps(prof, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def bump_works_slain(n=1):
    """斩活 +1。锻剑（/api/forge）完成时调它——一次锻造 = 一件活被斩透了。"""
    prof = read_profile()
    prof["works_slain_delta"] = int(prof.get("works_slain_delta") or 0) + n
    write_profile(prof)
    return prof


# ── 等级引擎（DESIGN §5，四维全达标才晋升） ──────────────────────────

# 门槛表照抄 DESIGN §5。注意第三维：设计稿写的是「转正剑」（把数），
# 契约 exp 里给前端的是 temper_rate（转正率）——门槛按把数判，经验条按率画，
# 两个读数都在 next_level_req 里给全，前端不用自己换算。
LEVELS = [
    {"level": "见习锻造师", "title": "初见炉火",
     "req": {"reflect_sessions": 0, "swords_forged": 0, "swords_promoted": 0, "works_slain": 0}},
    {"level": "初级锻造师", "title": "铁屑初扬",
     "req": {"reflect_sessions": 3, "swords_forged": 1, "swords_promoted": 0, "works_slain": 3}},
    {"level": "中级锻造师", "title": "百炼始成",
     "req": {"reflect_sessions": 10, "swords_forged": 3, "swords_promoted": 1, "works_slain": 10}},
    {"level": "高级锻造师", "title": "剑气渐盈",
     "req": {"reflect_sessions": 30, "swords_forged": 8, "swords_promoted": 4, "works_slain": 40}},
    {"level": "超级锻造师", "title": "炉火纯青",
     "req": {"reflect_sessions": 80, "swords_forged": 20, "swords_promoted": 12, "works_slain": 150}},
    {"level": "锻造之神", "title": "开炉即神兵",
     "req": {"reflect_sessions": 200, "swords_forged": 50, "swords_promoted": 30, "works_slain": 500}},
]


def count_reflect_sessions():
    """一个 json 文件 = 一场反思（文件名是 session hash，重复喂同一段不重复计数）。"""
    return len(list(IRONS.glob("*.json")))


def list_sword_metas():
    out = []
    for d in (sorted(SWORDS.iterdir()) if SWORDS.exists() else []):
        f = d / "meta.json"
        if f.exists():
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
    return out


def compute_exp():
    """经验值不手填，全部从落盘产物现算——数字造不了假，这是这个产品的立论。"""
    swords = list_sword_metas()
    forged = len(swords)
    promoted = len([s for s in swords if s.get("status") == "forged"])
    cfg = read_config()
    prof = read_profile()
    return {
        "reflect_sessions": count_reflect_sessions(),
        "swords_forged": forged,
        # 一把剑都没锻时转正率是 0 不是 1——没开炉不能算满分
        "temper_rate": round(promoted / forged, 2) if forged else 0.0,
        "works_slain": int(cfg["works_slain_base"]) + int(prof.get("works_slain_delta") or 0),
        # 门槛判定用的把数（契约 exp 四字段之外的内部读数）
        "swords_promoted": promoted,
    }


def level_of(exp):
    """四维全部达标才算这一级。从高往低扫，第一个全达标的就是当前等级。"""
    for i in range(len(LEVELS) - 1, -1, -1):
        req = LEVELS[i]["req"]
        if all(exp.get(k, 0) >= v for k, v in req.items()):
            return i
    return 0


def build_profile():
    exp = compute_exp()
    idx = level_of(exp)
    cur = LEVELS[idx]
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    out = {
        "name": read_profile()["name"],
        "title": cur["title"],
        "level": cur["level"],
        "exp": {
            "reflect_sessions": exp["reflect_sessions"],
            "swords_forged": exp["swords_forged"],
            "temper_rate": exp["temper_rate"],
            "works_slain": exp["works_slain"],
        },
        "next_level_req": None,
    }
    if nxt:
        req = nxt["req"]
        out["next_level_req"] = {
            "level": nxt["level"],
            "title": nxt["title"],
            **req,
            # 每维单独给达标位：四维全绿才跳级，缺哪一维前端一眼看得见
            "met": {k: exp.get(k, 0) >= v for k, v in req.items()},
            "current": {k: exp.get(k, 0) for k in req},
        }
    else:
        out["next_level_req"] = {"level": None, "note": "已至锻造之神，无上可攀"}
    return out


# ── claude CLI（红线：只走 CLI 订阅登录态） ─────────────────────────

def find_claude():
    """Windows 上 which 可能给到 claude.ps1，subprocess 跑不了，换同目录 claude.cmd。"""
    p = shutil.which("claude")
    if p and p.lower().endswith(".ps1"):
        cmd = Path(p).with_suffix(".cmd")
        if cmd.exists():
            return str(cmd)
    return p


def claude_env():
    env = dict(os.environ)
    # 红线：只走 CLI 自己的 OAuth 订阅登录态，绝不用厂商 API key
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env["DESK_NO_PACK"] = "1"
    return env


class ForgeError(Exception):
    """带 stage 的结构化错误——前端有错误态，要认得出是哪一步烧糊的。"""

    def __init__(self, stage, message, detail=""):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.detail = detail[:600]


def call_claude(prompt, timeout=120):
    """prompt 走 stdin，不进命令行——中文转义 + 8191 长度限制两个坑一起躲开。"""
    claude = find_claude()
    if not claude:
        raise ForgeError("cli_missing", "找不到 claude CLI，先确认 claude 在 PATH 里")
    try:
        proc = subprocess.run(
            [claude, "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=claude_env(),
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        raise ForgeError("timeout", "炉火烧了 {} 秒还没出铁，这次先撤".format(timeout))
    if proc.returncode != 0:
        raise ForgeError("cli_error", "claude CLI 返回非零",
                         (proc.stderr or proc.stdout or "").strip())
    out = (proc.stdout or "").strip()
    if not out:
        raise ForgeError("empty", "claude CLI 什么都没吐出来")
    return out


# ── 反思炉 ────────────────────────────────────────────────────────

REFLECT_TEMPLATE = PROMPTS / "reflect.md"
KINDS = ("判断", "经验", "流程")
GRADES = ("上品", "中品", "下品")


def jsonl_to_text(path, limit=24000):
    """.jsonl 会话留痕 → 可喂给炉子的纯文本。只留人说了什么、AI 答了什么。"""
    f = Path(path).expanduser()
    if not f.exists():
        raise ForgeError("no_file", "找不到这份会话记录：{}".format(f))
    if f.suffix.lower() != ".jsonl":
        return f.read_text(encoding="utf-8", errors="replace")[:limit]
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        msg = ev.get("message") or {}
        role = msg.get("role") or ev.get("type")
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            out.append("[{}] {}".format(role, content.strip()[:1200]))
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text" \
                        and blk.get("text", "").strip():
                    out.append("[{}] {}".format(role, blk["text"].strip()[:1200]))
    text = "\n".join(out)
    if len(text) > limit:
        # 掐头留尾：开场定了要干什么，收尾见得到结果，中间的过程可以省
        text = text[:limit // 2] + "\n…（中间略）…\n" + text[-limit // 2:]
    return text


def armory_digest():
    """给提炼 prompt 用的兵器架清单——要判断有没有引用已有的剑，得先让它知道有哪些剑。"""
    lines = []
    for m in list_sword_metas():
        lines.append("- {}（{}，{}）".format(
            m.get("name") or m.get("id"), m.get("version", "v0.1"),
            "已转正" if m.get("status") == "forged" else "草稿"))
    return "\n".join(lines) or "（兵器架还空着，本次不会有引用剑的铁）"


def scrolls_digest():
    s = read_config().get("featured_scroll") or {}
    if not s.get("name"):
        return "（秘籍阁尚未开启）"
    return "- {}：{}".format(s["name"], s.get("desc", ""))


def build_reflect_prompt(session_text):
    tpl = REFLECT_TEMPLATE.read_text(encoding="utf-8")
    # 模板里有大段 JSON 花括号，用 replace 不用 format——省掉转义地狱
    return (tpl.replace("{armory}", armory_digest())
               .replace("{scrolls}", scrolls_digest())
               .replace("{session}", session_text))


def parse_irons(raw, session_text, session_hash):
    """CLI 爱包 ```json，也爱在数组前后加一句寒暄。先把数组抠出来，再逐条验。"""
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    if not text.startswith("["):
        a, b = text.find("["), text.rfind("]")
        if a < 0 or b < a:
            raise ForgeError("bad_output", "炉子没吐出 JSON 数组", raw)
        text = text[a:b + 1]
    try:
        items = json.loads(text)
    except Exception as e:
        raise ForgeError("bad_json", "炉子吐的不是合法 JSON：{}".format(e), raw)
    if not isinstance(items, list):
        raise ForgeError("bad_output", "炉子吐的不是数组", raw)

    irons, dropped = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        anchor = (it.get("anchor") or "").strip()
        text_ = (it.get("text") or "").strip()
        if not text_ or not anchor:
            dropped.append({"reason": "缺 text 或 anchor", "raw": it})
            continue
        # 锚必须逐字出现在原文里。这是提炼质量唯一的硬闸门：
        # 模型最爱干的事就是把自己的总结当成人家的原话，这一行就是拦它的。
        if anchor not in session_text:
            dropped.append({"reason": "锚不在原文里（疑似编造）", "raw": it})
            continue
        iron = {
            "id": "{}-i{}".format(session_hash[:8], len(irons) + 1),
            "text": text_,
            "anchor": anchor[:60],
            "kind": it.get("kind") if it.get("kind") in KINDS else "经验",
            "grade": it.get("grade") if it.get("grade") in GRADES else "下品",
        }
        cite = it.get("cite")
        # cite 宁缺勿滥：结构不对就整个丢掉，不猜、不补——假引用比没引用糟
        if isinstance(cite, dict) and (cite.get("name") or "").strip():
            iron["cite"] = {
                "name": cite["name"].strip(),
                "kind": cite.get("kind") if cite.get("kind") in ("剑", "秘籍") else "秘籍",
            }
        irons.append(iron)
    return irons, dropped


def reflect(source, content="", path=""):
    if source == "file":
        session_text = jsonl_to_text(path)
    else:
        session_text = (content or "").strip()
    if len(session_text) < 20:
        raise ForgeError("too_short", "这段记录太短了，炉子烧不出铁")

    session_hash = hashlib.sha1(session_text.encode("utf-8")).hexdigest()[:12]
    log("反思炉开火：hash={} 长度={} 字".format(session_hash, len(session_text)))
    raw = call_claude(build_reflect_prompt(session_text), timeout=120)
    irons, dropped = parse_irons(raw, session_text, session_hash)

    record = {
        "session_hash": session_hash,
        "source": source,
        "path": str(path) if source == "file" else "",
        "created": _now(),
        "chars": len(session_text),
        "irons": irons,
        # 被闸掉的候选也留痕：将来要调提炼质量，得看得见炉子都吐了什么渣
        "dropped": dropped,
    }
    (IRONS / (session_hash + ".json")).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    log("出铁 {} 块（闸掉 {} 块）→ data/irons/{}.json".format(
        len(irons), len(dropped), session_hash))
    return {"irons": irons, "session_hash": session_hash, "dropped": len(dropped)}


# ── HTTP ─────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ReflectForge/0.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        # ensure_ascii=False：中文直出，前端拿到就能渲染，不用再解 \uXXXX
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, e, code=500):
        if isinstance(e, ForgeError):
            return self._json({"error": e.message, "stage": e.stage, "detail": e.detail}, code)
        return self._json({"error": "{}: {}".format(type(e).__name__, e),
                           "stage": "server"}, 500)

    def _file(self, f):
        if not f.exists() or not f.is_file():
            return self._json({"error": "没有这份东西：" + self.path}, 404)
        raw = f.read_bytes()
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            raise ForgeError("bad_request", "请求体不是合法 JSON：{}".format(e))

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/profile":
                return self._json(build_profile())

            if path in ("/", "/index.html"):
                f = ROOT / "index.html"
                if not f.exists():
                    # 视觉线还没落地时不给白屏，给一句能看懂的话
                    return self._json({"note": "锻造台已开炉，index.html 还在视觉线手里",
                                       "api": ["/api/profile", "/api/reflect"]})
                return self._file(f)

            if path.startswith("/static/"):
                f = (STATIC / path[len("/static/"):]).resolve()
                # 只放行 static/ 里的东西，别把 data/ 和 config.json 一起端出去
                if not str(f).startswith(str(STATIC.resolve())):
                    return self._json({"error": "越界了"}, 403)
                return self._file(f)

            return self._json({"error": "unknown route: " + path}, 404)
        except Exception as e:
            return self._err(e)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._read_body()

            if path == "/api/reflect":
                source = data.get("source") or "paste"
                if source not in ("paste", "file"):
                    raise ForgeError("bad_request", "source 只认 paste 或 file")
                return self._json(reflect(source, data.get("content") or "",
                                          data.get("path") or ""))

            return self._json({"error": "unknown route: " + path}, 404)
        except ForgeError as e:
            log("烧糊了[{}]：{}".format(e.stage, e.message))
            # 4xx = 你给的料不对，5xx = 炉子自己出问题。前端要按这个分错误态
            code = 400 if e.stage in ("bad_request", "too_short", "no_file") else 502
            return self._err(e, code)
        except Exception as e:
            log("意外炸炉：{}: {}".format(type(e).__name__, e))
            return self._err(e)


if __name__ == "__main__":
    p = build_profile()
    log("反思锻造台开炉 · 锻造师「{}」· {}（{}）· 铁 {} 场 / 剑 {} 把".format(
        p["name"], p["level"], p["title"],
        p["exp"]["reflect_sessions"], p["exp"]["swords_forged"]))
    log("→ http://localhost:{}".format(PORT))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
