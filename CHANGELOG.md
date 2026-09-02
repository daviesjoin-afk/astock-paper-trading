# 维护记录

## 2026-09-02

### 新增

- 增加 Docker 镜像运行支持，容器以非 root 用户启动 API 和 Web 看板。
- 增加 `docker-compose.yml`，持久化模拟盘缓存、SQLite 数据、日志和日报。
- 增加 Docker 健康检查与运行说明。
- 增加 MIT 开源许可证、GitHub Actions 测试工作流、贡献指南和公开 Demo 状态说明。

### 脱敏检查

- 本次提交仅包含源码、Docker 配置和文档。
- 未包含本机绝对路径、服务器地址/账号/密码、运行时数据库、日志、API key 或其他凭据。
- `DEEPSEEK_API_KEY` 仅以 `.env.example` 注释占位符形式保留。

### 验证边界

- 当前维护环境未安装 Docker，未能执行实际镜像构建和容器启动验证。
- 当前 Python 环境缺少项目依赖，完整单元测试未能作为通过依据。
- GitHub Actions 配置将在远程仓库首次 push 后由 GitHub 执行；本地维护环境无法代替该验证。

### 本地克隆验证

- 从 GitHub `master` 全新克隆后，按 README 安装依赖并启动服务。
- 修正前端风险审计请求与回归测试的条数不一致（`limit=80` → `limit=160`）。
- 修正后后端 123 个测试全部通过；首页和 `/api/health` 均返回 HTTP 200。
- 验证使用 Python 3.14；Docker 镜像仍需在安装 Docker 的环境中单独构建验证。
