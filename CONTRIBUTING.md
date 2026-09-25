# 贡献指南

感谢参与本项目。提交代码前请先阅读 `README.md`、`docs/RUNBOOK.md` 和 `LICENSE`。

## 开发流程

1. 从 `master` 创建短分支，说明要解决的问题。
2. 使用 **Python 3.14** 安装 `requirements.lock`；`requirements.txt` 仅用于维护允许的版本范围。
3. 运行 `python -m unittest discover -s backend -p "test_*.py" -v`。
4. 运行 `ruff check backend` 和 `pip-audit --requirement requirements.lock --strict`；修改依赖后用仓库约定的 uv 命令重新生成锁文件。
5. 对策略、撮合或风控改动补充回归测试，并在 PR 中说明数据假设和风险边界。
6. 不提交 `data_cache/`、`reports/`、`.env`、日志、运行时数据库或任何凭据。

## Python 运行时

**Python 3.14 是本项目的 canonical development / CI / container runtime。** `Dockerfile` 的基础镜像、`.python-version`、Ruff `target-version`、依赖锁的编译目标与 GitHub Actions 的全部 Python 作业都以 3.14 为唯一版本。项目**不维护 per-PR 的多 Python 兼容矩阵**：不为每个 PR 重复在多个 Python minor 上跑同一套测试。

**唯一例外是 `native-centos9` 原生部署 profile**：CentOS Stream 9 的默认仓库不提供 3.14，该 profile 仍使用 Python 3.11（见 `deploy/install-centos9.sh` 与 `deploy/README.md`）。这是明确保留的 legacy 部署例外 —— 既不是第二个 canonical runtime baseline，也不是 PR 兼容 lane；它的升级或退役将在独立的、经过评审的变更中处理。该例外不构成把 Python 3.11 重新引入开发环境、GitHub CI、Docker 镜像或测试矩阵的许可。

因此本地与 agent 的默认验证流程是：

```text
python --version        → Python 3.14.x
targeted tests          → Python 3.14
full backend suite      → Python 3.14，最终只跑一次
compileall / ruff       → Python 3.14
GitHub backend 单测 CI  → Python 3.14（单一 lane）
```

提交 PR 时不要写成"分别在 3.11 / 3.12 / 3.14 运行"。确认旧解释器上是否还能跑属于一次性的人工兼容性检查，不构成 CI 承诺。`docker-smoke` 与 backend 单测是两个不同性质的 gate（前者验证生产镜像 + 无网络打包，后者验证宿主 runner + 锁文件），都要保留。

## 交易安全边界

本项目是模拟盘研究工具，不连接券商，不触碰真实资金。不要将 API key、服务器配置、账户信息、未脱敏运行截图或真实持仓数据提交到仓库。涉及买入、卖出、风控门禁的改动必须保持异常行情默认拒绝的 fail-closed 行为。

## 测试分层

测试分三层，约定如下（对应 issue #29）：

1. **离线必测**（默认）：`python -m unittest discover -s backend -p "test_*.py"` 必须在**完全无网络**的环境下通过。CI 在 `--network none` 的容器里执行同一套测试作为强制门禁——新增测试一律默认离线，数据用合成 fixture（参考 `backend/test_demo_seed.py` 的临时目录隔离写法）。
2. **联网可选**：确需真实行情的验证逻辑写成可跳过用例，统一用 `@unittest.skipUnless(os.getenv("ASTOCK_NET_TESTS") == "1", "network-optional test")` 标记；CI 永远跳过，本地按需 `ASTOCK_NET_TESTS=1` 运行。不要在默认测试里直接发起外部请求。
3. **人工确认**：依赖真实账户、服务器或密钥的验证脚本不进单测，放入 `deploy/` 或文档化操作步骤，并在 PR 中注明"未自动化"。

判断标准：如果一个测试在没有网络时会变红、变慢或产生随机结果，它属于第 2/3 层，必须显式标记。

## 评审分层

项目 PR 使用三层验证：

1. **deterministic**
   - `compileall`
   - `ruff`
   - `unittest`
   - security scan
   - `docker-smoke`
   - exact-head GitHub CI

2. **behavioral**
   - changed-path targeted regressions
   - contract tests
   - architecture guards
   - semantic mutation
   - production 逻辑变化完成后，最终 full backend suite

3. **human review**
   - owner / authority
   - provenance
   - PIT / look-ahead
   - architecture boundaries
   - maintainability
   - roadmap capability preservation

**外部 AI semantic-review workflow 不属于 merge gate。** 正确性必须由 deterministic evidence + 人工架构审核证明：只有可复现的 CI 结果、contract / architecture guard、mutation 证据，以及人工对 authority、provenance、PIT 与架构边界的判断，才能作为 merge 依据。

## Pull Request 清单

- 说明改动目的、影响范围和验证命令。
- 标明测试通过、跳过或受环境限制的部分。
- 检查 diff 中没有本机路径、服务器地址、账号密码、token 或 API key。
- 文档不得把模拟结果描述为收益保证或投资建议。
