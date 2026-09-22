# -*- coding: utf-8 -*-
"""给 paper-runtime 页面加载路径的关键 API 计时（真实 e2e server，离线夹具）。

目的：判断 E2E flaky 属于哪一类 —— 页面没渲染 / backend API 慢 / DB 锁等待 /
provider 误入读路径。只做只读测量，不改生产代码。

用法：
    python work/r23_round4_endpoint_timing.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend")
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
PORT = 8621

# paper-runtime 页面加载实际请求的三个只读来源（见 frontend/src/features/paper.js）。
ENDPOINTS = [
    ("/api/health", "webServer ready probe"),
    ("/api/paper/allocation-explain", "运行时参与/额度/阶段（卡片数据源）"),
    ("/api/strategies?include_archived=true", "注册表身份/不可变版本"),
    ("/api/paper/strategy-center", "内置策略风险边界摘要"),
]


def wait_ready(base, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def time_endpoint(base, path):
    url = base + path
    start = time.time()
    status, size, err = None, None, None
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            body = resp.read()
            status, size = resp.status, len(body)
    except urllib.error.HTTPError as exc:
        status, err = exc.code, f"HTTPError {exc.code}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return {"path": path, "elapsed": round(time.time() - start, 3),
            "status": status, "bytes": size, "error": err}


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="r23-timing-")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [PYTHON, os.path.join("e2e", "server.py"), "--port", str(PORT),
         "--data-dir", data_dir, "--proxy-port", "0", "--strip-proxy-port", "0"],
        cwd=FRONTEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    base = f"http://127.0.0.1:{PORT}"
    try:
        if not wait_ready(base):
            print("server 未就绪")
            return 1
        print(f"server ready on {PORT}\n")

        # 冷启动：进程刚起，第一次请求就是页面要付的代价。
        print("=== COLD（进程刚起，首次请求）===")
        cold = []
        for path, why in ENDPOINTS:
            row = time_endpoint(base, path)
            row["why"] = why
            cold.append(row)
            print(f'  {row["elapsed"]:7.3f}s  {row["status"]}  {path}  # {why}')

        # 第二次：缓存/预热之后。
        print("\n=== WARM（紧接着第二次）===")
        warm = []
        for path, why in ENDPOINTS:
            row = time_endpoint(base, path)
            row["why"] = why
            warm.append(row)
            print(f'  {row["elapsed"]:7.3f}s  {row["status"]}  {path}')

        out = {"port": PORT, "cold": cold, "warm": warm}
        target = os.path.join(tempfile.gettempdir(), "r23r3_endpoint_timing.json")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(out, handle, ensure_ascii=False, indent=2)
        print(f"\njson -> {target}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
