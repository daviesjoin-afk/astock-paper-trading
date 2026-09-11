#!/usr/bin/env python3
"""PR-56：Playwright E2E 用的本地 FastAPI 启动器（真实 HTTP，无外部网络）。

职责：
  1. 建一个**独立临时数据目录**（ASTOCK_DATA_DIR），绝不写仓库 data_cache/
     或任何生产库；每个进程一份，互不干扰。
  2. 用确定性离线夹具启动真实 FastAPI 应用（与生产同一份 backend 代码）。
  3. 打印端口，供 Playwright webServer 使用。

外部依赖（行情源 / LLM / 新闻）在测试里不联网：ASTOCK_DEMO=1 提供合成宇宙与
本地账本；未覆盖的路由若需要网络，测试不应调用它们。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO_ROOT, "backend")

# 本文件以脚本方式运行（``python e2e/server.py``），脚本所在目录即 e2e/，
# 因此可以直接 import 同目录的反代夹具。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reverse_proxy import ReverseProxy  # noqa: E402

# PR-2：E2E 用的 operator token。刻意写成明显的合成值（全小写 + 连字符），
# 不对应任何真实环境；仅供本地/CI 的临时实例使用。
OPERATOR_TOKEN = "zz-e2e-operator-placeholder-value"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8611)
    parser.add_argument("--data-dir", default=None,
                        help="临时数据目录（缺省自动创建并 DROP_AFTER 时删除）")
    parser.add_argument("--keep-data-dir", action="store_true",
                        help="结束后保留临时数据目录（排障用）")
    # PR-2 复审 Blocker 1：正式反代形态回归。直连 Uvicorn 时 Host 天然带端口，
    # 测不出 nginx ``Host $host`` 丢端口的缺陷；因此这里额外起两个真实反代。
    parser.add_argument("--proxy-port", type=int, default=8612,
                        help="保留 host:port 的反代端口（修复后的 nginx 形态）；0=禁用")
    parser.add_argument("--strip-proxy-port", type=int, default=8613,
                        help="丢弃端口的反代端口（修复前的 nginx 形态，用于自证回归有效）；0=禁用")
    args = parser.parse_args()

    data_dir = args.data_dir or tempfile.mkdtemp(prefix="astock-e2e-")
    os.makedirs(data_dir, exist_ok=True)

    # 环境必须在导入 backend 之前设置：路径在模块导入时解析
    os.environ["ASTOCK_DATA_DIR"] = data_dir
    os.environ["ASTOCK_DEMO"] = "1"            # 合成宇宙 + 本地账本，离线可跑
    os.environ["ASTOCK_DEMO_FORCE"] = "1"
    os.environ["ASTOCK_ENABLE_FALLBACK_THREADS"] = "0"
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ["LLM_ADVISOR_ENABLED"] = "0"
    # PR-2：操作员边界。E2E 驱动的是真实应用，因此必须像真实运维那样配置
    # operator token——绝不能为了过测试而关掉鉴权（那会让测试失去意义）。
    # 前端通过 **sessionStorage** 里的 astock.operatorToken.v1 读取，并把它作为
    # 标准 authorization 头（Bearer 方案）发出；E2E 用真实的"解锁"交互写入
    # （见 e2e/specs/operator-unlock.spec.js），不在配置层预注入。
    os.environ["ASTOCK_OPERATOR_TOKEN"] = OPERATOR_TOKEN
    sys.path.insert(0, BACKEND)

    import uvicorn  # noqa: E402
    import main  # noqa: E402,F401  (真实应用装配)

    # PR-2 复审 Blocker 1：在应用之前先把反代起好（uvicorn.run 会阻塞主线程）。
    # 反代只做 Host 转发语义的复刻，转发目标就是本进程即将监听的 127.0.0.1:port。
    proxies = []
    for port, mode, label in (
        (args.proxy_port, "preserve", "reverse proxy (host:port preserved)"),
        (args.strip_proxy_port, "strip_port", "reverse proxy (port stripped / buggy)"),
    ):
        if not port:
            continue
        proxies.append(
            ReverseProxy("127.0.0.1", args.port, host_mode=mode,
                         listen_port=port, name=f"proxy-{mode}").start()
        )
        print(f"[e2e-server] {label} on http://127.0.0.1:{port}", flush=True)

    print(f"[e2e-server] data_dir={data_dir}", flush=True)
    print(f"[e2e-server] listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        uvicorn.run(main.app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        for proxy in proxies:
            proxy.stop()
        if not args.keep_data_dir:
            shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
