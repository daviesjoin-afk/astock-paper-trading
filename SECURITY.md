# Security boundary

## 禁止提交的内容（P0）

以下内容一律不得进入仓库、提交信息、Issue、PR、CI 日志或截图：

- 私人邮箱（提交身份必须使用 `*@users.noreply.github.com`）
- 公网 IP、真实服务器地址、SSH 登录信息与私钥
- 密码、Token、API Key、Cookie、Session 凭据
- 真实账户号 / Broker 账户 ID
- 数据库、导出数据、日志、备份文件
- 本机绝对路径（`C:\Users\<name>`）与服务器部署绝对路径
- 未脱敏的界面截图（可能含账户号、邮箱、路径、IP）

文档与示例一律使用：`user@example.com`、`example.com`、`localhost`、`127.0.0.1`、
`192.0.2.x`、`198.51.100.x`、`203.0.113.x`、`<PROJECT_ROOT>`、`<USER_HOME>`、`<SERVER_HOST>`。

## 自动门禁

`scripts/security/scan-sensitive-data.py` 同时扫描当前工作树与全部可达历史；
CI job `security-leak-scan` 在 push 与 pull_request 上强制运行，命中即失败。
本地可在推送前手动执行：

```bash
python scripts/security/scan-sensitive-data.py --repo . --scope all
```

确需放行的公开内容（厂商域名、示例路径等）写入 `.security-allowlist` 并注明理由；
真实邮箱、公网 IP、凭据、私钥与真实账户号**永远不得**加入白名单。


## 支持版本

仅维护最新 GitHub Release 对应的 `master`。旧标签用于复现和审计，不再单独接收安全补丁。

## 运行边界

- 这是模拟交易系统，不支持真实券商下单；不要把任何券商凭据放入仓库、日志、Issue 或测试数据。
- 本地启动脚本默认使用 `localhost`/`127.0.0.1`。
- 本地 Compose 与服务器 Compose 均将宿主端口绑定到环回地址（`127.0.0.1:8600` /
  `127.0.0.1:18600`），容器内部 Uvicorn 仍监听 `0.0.0.0`——三层边界共同工作：
  **容器内 0.0.0.0（供 docker port publishing / healthcheck / nginx 反代）+ 宿主环回发布 +
  应用层 operator 鉴权**。如需公网访问，必须由运维层显式配置网络 ACL、TLS 与身份认证。
- `confirmed=true` 只用于浏览器二次确认，**不能当作鉴权**。

## 操作员边界（PR-2）

写接口（`POST`/`PUT`/`PATCH`/`DELETE`）由统一模块 `backend/operator_auth.py`
和 `main.py` 的全局中间件保护，与路由前缀无关，因此不存在"某个前缀漏配"的旁路。
`GET`/`HEAD`/`OPTIONS` 为只读控制面，不需要凭据（读写分离）。

### 配置

```bash
# 生成一个强随机 token（>= 16 字符）
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 写入 .env（已被 gitignore），不要提交
ASTOCK_OPERATOR_TOKEN=<生成的随机串>
```

- **未配置 token 时写接口按 fail-closed 拒绝（HTTP 503）**，只读看板不受影响。
  这不会静默降级成"无鉴权"。
- 弱 token（< 16 字符或含非法字符）**不构成边界**，写请求同样被拒绝。
- 仅在**纯本机离线演示**时，可用 `ASTOCK_OPERATOR_AUTH_REQUIRED=0` 显式关闭写接口鉴权；
  不要在服务器或任何他人可达的环境设置它。
- 当前状态随时可见于只读接口 `GET /api/operator-status`（**不回显 token 本身**）。

### 客户端携带方式

凭据**只走请求头**，绝不进 URL/query（避免落入访问日志、浏览器历史与 `Referer`）：

```
X-Operator-Token: <token>
Authorization: Bearer <token>
```

浏览器端在 `localStorage['operatorToken']` 中保存一次，由 `frontend/src/core/api.js`
自动附加到写请求。它不是身份系统，只是一道本机/内网边界；不要在共享浏览器上保存。

### 已知边界（本 PR 不做）

- 不引入 OAuth / 账户系统 / JWT / RBAC / session 框架。
- 不做 TLS 终止、不新增反向代理；这些属于运维层。
- 来源 IP 信任不由应用层判断——应用不解析 `X-Forwarded-For` 做授权，
  网络来源应由 nginx / 安全组 / ACL 层约束。


## 写接口清单

纸盘和 adaptive API 含有启动、暂停、下单、撤单、运行研究、写反馈和应用风控等状态变更接口。
当前 HTTP 控制面共有 **129 条路由，其中 61 条为写方法**（`POST`/`PUT`/`PATCH`/`DELETE`）。
它们全部由统一中间件覆盖（按方法判定，不按前缀），验证要点：

1. 本机/容器模式下宿主端口仅绑定环回地址（`127.0.0.1`）。
2. 写接口要求 operator 凭据；读接口不要求，读写分离。
3. 未授权请求不会因为携带 `confirmed=true` 而成功——`confirmed` 只是产品确认步骤。
4. 所有状态变更都能在审计记录中定位操作者、时间、版本和原因。

回归测试见 `backend/test_operator_boundary.py`（覆盖各前缀族的写路由 + 配置 fail-closed +
读写分离 + 不依赖 cookie 的 CSRF 免疫）。

## 报告问题

不要在公开 Issue 中提交密码、Token、Cookie、数据库文件或包含密钥的日志。请使用仓库的 [GitHub 私密漏洞报告](https://github.com/daviesjoin-afk/astock-paper-trading/security/advisories/new)，提供复现步骤、受影响版本、影响范围和脱敏证据。该渠道只有仓库维护者可见；若页面不可用，请不要改用公开 Issue 披露敏感细节。

维护者会尽量在 7 天内确认收到报告。修复前请给维护者合理处理时间；确认修复和发布时间后再公开技术细节。
