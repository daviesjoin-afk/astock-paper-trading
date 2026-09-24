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

## 评审分层与 OpenCodeReview（观察模式）

评审分四层，OpenCodeReview（OCR）只占第三层，而且只是**补充性的观察层**：

```text
Layer 1 deterministic  ruff / compileall / unittest / security scan / docker-smoke
Layer 2 behavioral     targeted regression / mutation / full backend suite
Layer 3 semantic       OpenCodeReview：跨文件语义、owner/authority、PIT/provenance
Layer 4 human          authority / provenance / architecture / roadmap
```

OCR **不替代** unittest、mutation、ruff、security scan、exact-head CI 或人工审核；**「OCR 没发现问题」不等于正确性已被证明**。它只做 read / analyze / comment / summarize，**不自动修代码、不自动改 PR、不自动合并**，第一阶段也不自动 resolve review thread。

- **双重固定**：action 固定到 full commit SHA（`bccbc15f785269400735d5255540c231e6c02b6d`，# v1.12.9），CLI 固定到 `ocr_version: "1.12.9"`。升级必须走单独的、经过评审的 dependency bump，不允许 `@main` 或 `latest` 漂移。
- **安全模型**：触发方式是 `pull_request_target`（使用 base 分支上的 workflow 定义，secrets 可用）；wrapper **不 checkout PR head、不执行 PR 代码**，权限只有 `contents: read` 与 `pull-requests: write`。密钥只从仓库配置注入，仓库内不提交任何 token 或 endpoint。
- **项目规则**在 `.opencodereview/rule.json`：backend 的 owner / authority / PIT / provenance 不变量、contract 测试与 mutation harness 的非空真性、GitHub workflow 的 runtime / security 约束。`backend/test_*.py` 与 `work/*mutation*.py` 被**有意**强制纳入评审 —— 本仓库的 contract 测试、architecture guard 与 mutation 校验本身就是 correctness surface。
- **它不是 required check**：OCR provider / auth / action 自己坏掉时会看到 job RED（因此不写 `continue-on-error`），但不会阻塞 merge。「不阻塞」由 branch protection 不把它列为 required context 实现，而不是靠吞掉失败。先观察 5–10 个真实 PR 的真阳性 / 假阳性 / 噪声 / token 用量，再决定是否升级为门禁。
- **凭据由人工配置**：仓库 Settings → Secrets and variables → Actions（`OCR_LLM_URL`、`OCR_LLM_AUTH_TOKEN`、`OCR_LLM_MODEL`、`OCR_LLM_USE_ANTHROPIC`）。注意 v1.12.9 把**空值**当作 Anthropic 协议，所以 `OCR_LLM_USE_ANTHROPIC` 必须显式写 `false`（OpenAI-compatible）或 `true`（Anthropic），不要留空；也不要按模型名猜协议。
- **bootstrap 限制**：`pull_request_target` 的 workflow 由 base 分支定义触发，因此新增该 workflow 的那个 PR 本身不会被自己评审 —— 这不是失败，首次真实运行发生在它合并之后的正常 PR 上。

## Pull Request 清单

- 说明改动目的、影响范围和验证命令。
- 标明测试通过、跳过或受环境限制的部分。
- 检查 diff 中没有本机路径、服务器地址、账号密码、token 或 API key。
- 文档不得把模拟结果描述为收益保证或投资建议。
