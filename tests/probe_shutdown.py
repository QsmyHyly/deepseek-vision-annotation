"""关闭服务接口的验收探针：POST /api/shutdown 真的能停掉进程吗？非本机来源真的被拒吗？

为什么不能用 TestClient 验：它的分支恰好是"没有 Server 句柄 -> 503"那一条，
而真正要验的是**有**句柄时进程会不会真的退掉、端口会不会真的释放。
这两件事只有真起一个进程才测得出来。

跑法（不花 API，走 LLM_PROVIDER=mock；占 8799 端口，跑完自己收干净）：
    python tests/probe_shutdown.py

它属于 tests/ui/ 那一类「要真东西才测得出来」的自检，不是离线单测，
所以不叫 test_*.py（不会被 pytest 收走）。见 AGENTS.md §4.11。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = Path(__file__).resolve().parent.parent.parent / "qsmy-deepseek-locator" / "src"
PORT = 8799


def env_for(host: str) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(LIB),
        "PYTHONIOENCODING": "utf-8",
        "LLM_PROVIDER": "mock",       # 不花 API
        "DEEPSEEK_API_KEY": "",
        "WEB_HOST": host,
        "WEB_PORT": str(PORT),
    })
    return env


def post(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_status(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def wait_ready(host_ip: str, seconds: float = 45.0) -> bool:
    url = "http://%s:%d/api/health" % (host_ip, PORT)
    deadline = time.time() + seconds
    while time.time() < deadline:
        if get_status(url) == 200:
            return True
        time.sleep(0.4)
    return False


def port_free() -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + str(detail)) if detail else ""))


# ---------------- 第 1 幕：绑定 127.0.0.1，本机关机必须成功 ----------------
print("[1] WEB_HOST=127.0.0.1：本机可以关掉它")
assert port_free(), "端口 %d 已经被占，先停掉它再跑本探针" % PORT
p = subprocess.Popen([sys.executable, "main.py", "web"], cwd=str(ROOT),
                     env=env_for("127.0.0.1"), stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
try:
    check("服务起来了", wait_ready("127.0.0.1"))
    code, body = post("http://127.0.0.1:%d/api/shutdown" % PORT)
    check("POST /api/shutdown -> 200", code == 200, body[:120])
    check("响应体说明正在关闭", json.loads(body).get("ok") is True, body[:120])
    try:
        p.wait(timeout=20)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    check("进程真的退出了", exited, "returncode=%s" % p.returncode)
    check("端口已经释放", port_free())
    check("健康检查已经不通", get_status("http://127.0.0.1:%d/api/health" % PORT) is None)
finally:
    if p.poll() is None:
        p.kill()
        p.wait(timeout=10)

# ---------------- 第 2 幕：绑定 0.0.0.0，经局域网 IP 必须被拒 ----------------
print("")
print("[2] WEB_HOST=0.0.0.0：从局域网 IP 打过来必须 403")
ip = lan_ip()
print("      本机局域网 IP = %s" % ip)
assert port_free(), "端口没释放干净"
p2 = subprocess.Popen([sys.executable, "main.py", "web"], cwd=str(ROOT),
                      env=env_for("0.0.0.0"), stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
try:
    check("服务起来了（0.0.0.0）", wait_ready("127.0.0.1"))
    code, body = post("http://%s:%d/api/shutdown" % (ip, PORT))
    check("非本机来源 -> 403", code == 403, body[:160])
    check("被拒之后服务还活着", get_status("http://127.0.0.1:%d/api/health" % PORT) == 200)
    code2, _ = post("http://127.0.0.1:%d/api/shutdown" % PORT)
    check("本机来源仍然可以关", code2 == 200)
finally:
    if p2.poll() is None:
        p2.kill()
        p2.wait(timeout=10)

print("")
bad = [n for n, ok, _ in results if not ok]
print("=" * 62)
print("%d / %d 项通过" % (len(results) - len(bad), len(results)))
if bad:
    print("失败：" + "; ".join(bad))
    sys.exit(1)
print("全部通过")