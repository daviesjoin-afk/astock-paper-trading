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
    # 前端通过 localStorage 里的 operatorToken 读取并放进请求头（见
    # playwright.config.js 的 addInitScript）。
    os.environ["ASTOCK_OPERATOR_TOKEN"] = OPERATOR_TOKEN
    sys.path.insert(0, BACKEND)

    import uvicorn  # noqa: E402
    import main  # noqa: E402,F401  (真实应用装配)

    print(f"[e2e-server] data_dir={data_dir}", flush=True)
    print(f"[e2e-server] listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        uvicorn.run(main.app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if not args.keep_data_dir:
            shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
