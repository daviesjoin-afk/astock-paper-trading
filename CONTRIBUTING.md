# 贡献指南

感谢参与本项目。提交代码前请先阅读 `README.md`、`docs/RUNBOOK.md` 和 `LICENSE`。

## 开发流程

1. 从 `master` 创建短分支，说明要解决的问题。
2. 在 Python 3.11 或 3.12 环境安装 `requirements.lock`；`requirements.txt` 仅用于维护允许的版本范围。
3. 运行 `python -m unittest discover -s backend -p "test_*.py" -v`。
4. 运行 `ruff check backend` 和 `pip-audit --requirement requirements.lock --strict`；修改依赖后用仓库约定的 uv 命令重新生成锁文件。
5. 对策略、撮合或风控改动补充回归测试，并在 PR 中说明数据假设和风险边界。
6. 不提交 `data_cache/`、`reports/`、`.env`、日志、运行时数据库或任何凭据。

## 交易安全边界

本项目是模拟盘研究工具，不连接券商，不触碰真实资金。不要将 API key、服务器配置、账户信息、未脱敏运行截图或真实持仓数据提交到仓库。涉及买入、卖出、风控门禁的改动必须保持异常行情默认拒绝的 fail-closed 行为。

## Pull Request 清单

- 说明改动目的、影响范围和验证命令。
- 标明测试通过、跳过或受环境限制的部分。
- 检查 diff 中没有本机路径、服务器地址、账号密码、token 或 API key。
- 文档不得把模拟结果描述为收益保证或投资建议。
