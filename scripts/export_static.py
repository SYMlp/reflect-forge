# 静态快照导出：给 GitHub Pages 产 site/ 目录（P0.5 公网站脱机化）。
#
# 做什么：
#   1. 临时起一台 FORGE_DEMO=1 的专用实例（随机高位空闲端口，抓完即关，不打扰 7712/7713）
#   2. 抓全部 GET 接口存成 site/api/<name>.json（页面实际调用的 6 个，含契约外的 prospect_banner）
#   3. 复制 index.html 进 site/ 并注入 window.STATIC_MODE=true（源文件不动，开关只活在导出产物里）
#   4. 复制 static/ 整目录、写 CNAME 与 .nojekyll
#
# 兜底：专用实例起不来时，退到正在跑的 7713 只读 demo 实例抓数。
# 跑法：python scripts/export_static.py

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
# 页面 js 实际发起的全部 GET。prospect_banner 不在契约 §4 里但前端真的在调（横幅单开的一路）
GET_ENDPOINTS = ["profile", "armory", "irons", "prospect", "prospect_banner", "manifest"]
CNAME_DOMAIN = "forge.lsrabbit.space"
FALLBACK_PORT = 7713
STATIC_FLAG = '<script>window.STATIC_MODE = true;</script>'
# 注锚：charset+viewport 那一行的尾巴，index.html 第一行必带（DESIGN §3 规矩），够稳
INJECT_ANCHOR = '<meta name="viewport" content="width=device-width, initial-scale=1">'

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print("[export] {}".format(msg))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fetch_json(port, name):
    url = "http://127.0.0.1:{}/api/{}".format(port, name)
    with urllib.request.urlopen(url, timeout=10) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def wait_ready(port, timeout_s=15):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            fetch_json(port, "profile")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def start_demo_instance():
    port = free_port()
    env = dict(os.environ)
    env["FORGE_DEMO"] = "1"
    env["FORGE_PORT"] = str(port)
    # 红线：CLI 走订阅登录态，不透传厂商 API key（虽然本脚本只打 GET，不烧火）
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py")],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if wait_ready(port):
        log("专用 demo 实例已起：端口 {}（pid {}）".format(port, proc.pid))
        return proc, port
    proc.terminate()
    return None, None


def main():
    proc, port = start_demo_instance()
    if port is None:
        log("专用实例没起来，退到 7713 只读实例兜底抓数")
        port = FALLBACK_PORT
        if not wait_ready(port, timeout_s=5):
            log("7713 也不通，导出失败")
            return 1

    try:
        snapshots = {}
        for name in GET_ENDPOINTS:
            snapshots[name] = fetch_json(port, name)
            log("抓到 /api/{}".format(name))

        # 快照必须来自演示模式实例：profile.demo=true 是前端 POST 拦截的第一道闸，
        # 缺了它快照站的开炉按钮会真发请求。normal 实例的数据不许出站。
        if not snapshots["profile"].get("demo"):
            log("profile.demo 不是 true——抓到的不是演示模式实例，拒绝导出")
            return 1

        if os.path.isdir(SITE):
            shutil.rmtree(SITE)
        api_dir = os.path.join(SITE, "api")
        os.makedirs(api_dir)
        for name, payload in snapshots.items():
            with open(os.path.join(api_dir, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)

        with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
            html = f.read()
        if INJECT_ANCHOR not in html:
            log("index.html 里找不到注锚（viewport meta），拒绝导出")
            return 1
        html = html.replace(INJECT_ANCHOR, INJECT_ANCHOR + STATIC_FLAG, 1)
        with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8", newline="\n") as f:
            f.write(html)

        shutil.copytree(os.path.join(ROOT, "static"), os.path.join(SITE, "static"))
        with open(os.path.join(SITE, "CNAME"), "w", encoding="ascii", newline="\n") as f:
            f.write(CNAME_DOMAIN + "\n")
        # Pages 默认过一遍 Jekyll，关掉：快照站是纯静态成品，不需要也不该被再加工
        with open(os.path.join(SITE, ".nojekyll"), "w") as f:
            pass

        log("site/ 导出完成：index.html + api/{} 份 + static/ + CNAME + .nojekyll".format(len(snapshots)))
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            log("专用实例已关")


if __name__ == "__main__":
    sys.exit(main())
