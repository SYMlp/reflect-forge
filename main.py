"""反思锻造台 Reflect Forge —— 后端本体。

打工人的经验不随下班蒸发，而是长成他自己带得走的资产。

铁 = session 里真正有价值的精华（判断 / 经验 / 流程）
剑 = 锻出来的 SKILL.md
火 = token

设计权威：docs/DESIGN.md（术语表 §1 / API 契约 §4 / 等级规则 §5）
跑法：python main.py  → http://localhost:7712
"""
import datetime
import json
import mimetypes
import os
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


# ── config ───────────────────────────────────────────────────────

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
            if path in ("/", "/index.html"):
                f = ROOT / "index.html"
                if not f.exists():
                    # 视觉线还没落地时不给白屏，给一句能看懂的话
                    return self._json({"note": "锻造台已开炉，index.html 还在视觉线手里"})
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
            self._read_body()
            return self._json({"error": "unknown route: " + path}, 404)
        except ForgeError as e:
            log("烧糊了[{}]：{}".format(e.stage, e.message))
            return self._err(e, 400)
        except Exception as e:
            log("意外炸炉：{}: {}".format(type(e).__name__, e))
            return self._err(e)


if __name__ == "__main__":
    log("反思锻造台开炉 · 锻造师「{}」".format(read_config()["forge_master"]))
    log("→ http://localhost:{}".format(PORT))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
