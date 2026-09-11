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

### 三种运行模式

系统只有三种 token 配置状态，对应三种模式。**不存在任何关闭边界的开关**：

| token 状态 | 模式 | 写请求行为 |
| --- | --- | --- |
| UNSET（不存在 / trim 后为空） | `local-only` | 环回客户端 + **本地 `Host`** 才允许；其余 **403** |
| VALID（trim 后 >= 24 字符且字符合法） | `authenticated` | 任何客户端（含 localhost）都必须带 `Authorization: Bearer` |
| INVALID（存在但太短 / 含非法字符） | `misconfigured` | 全部 **503**（绝不降级为 UNSET 或免鉴权） |

#### Local-only mode

未配置 token 时的默认模式，保留"本机零配置可用"：

- 写请求需要**同时**满足两个条件：客户端来自环回，且 `Host` 本身指向本机。
- 远端写请求一律 `403 remote operator mutations are disabled`。
- `Host` 不是本地地址时一律 `403 local-only operator mutations require a loopback host`。
- 这不是配置错误，因此**不返回 503**——它是刻意的本地兼容模式。
- 浏览器来源防护（`Origin` / `Sec-Fetch-Site`）在写方法上始终生效，所以环回
  客户端也不能被跨站页面冒用（localhost CSRF / DNS rebinding）。
- 该模式依赖环回 + 来源防护，**不应通过额外网络转发对外暴露**。

##### `Host` 必须是本地地址（DNS rebinding）

仅检查"客户端 IP 是环回"**不足以**挡住 DNS rebinding。攻击者可以让页面运行在
`http://attacker.example.com`，随后把该域名重绑到 `127.0.0.1`；此时：

- 浏览器发出的 `Origin` 与 `Host` 都是 `attacker.example.com`，两者一致；
- `Sec-Fetch-Site` 表现为 `same-origin`；
- TCP 层客户端确实是环回地址。

也就是说"环回 + `Origin == Host`"这三个条件会被同时满足，而请求其实来自
攻击者页面。因此在 local-only 模式下，`Host` 必须**本身就是本地地址**：

- 接受：exact `localhost`（大小写不敏感），以及环回 IP 字面量
  （`127.0.0.0/8`、`::1`，可带端口，IPv6 可带方括号）。
- 拒绝：**一切域名**，包括"当前解析到 `127.0.0.1`"的域名。
- 判定是**纯字面量比较，绝不查 DNS**——任何"解析后看是不是环回"的实现都会
  重新打开这个漏洞。

> 使用自定义主机名（例如内网域名 `astock.internal`）时必须配置合法 token，
> 让边界进入 `authenticated` 模式；local-only 模式不接受任何域名。

#### Authenticated mode

配置了合法 token 后的模式：

- 任何客户端的写请求都必须携带 `Authorization: Bearer <token>`。
- **localhost 没有豁免**——`VALID + 127.0.0.1 + 无凭据` 同样返回 `401`。
- 凭据比较使用 `hmac.compare_digest`（常量时间）。

#### Misconfigured mode

配置了存在但非法的 token（例如长度不足）：

- 所有写请求 `503 operator authentication is misconfigured`。
- 不 fallback 到 UNSET、不 fallback 到 local-only、不 fallback 到免鉴权。
- 响应体不含 token、token 前缀、实际长度或 env 路径。

### 配置

```bash
# 生成一个强随机 token（>= 24 字符）
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 写入 .env（已被 gitignore），不要提交
ASTOCK_OPERATOR_TOKEN=<生成的随机串>
```

- 当前状态随时可见于只读接口 `GET /api/operator-status`，它只返回
  `mode` / `token_configured` / `writes_protected`——**不返回 token 长度、前缀或值**。
- 启动日志只打印模式名（`operator auth mode: local-only` /
  `operator auth mode: authenticated` / `operator auth misconfigured`）。

### 客户端携带方式

唯一支持的凭据形式是标准请求头，Bearer 方案：

```
Authorization: Bearer <token>
```

`Bearer` scheme 大小写不敏感；但 `Basic` / `Token` / 缺 scheme / 缺凭据 /
凭据内含空白一律拒绝。凭据**只走这一个请求头**，绝不进 URL / query / body /
cookie（避免落入访问日志、浏览器历史与 `Referer`）。

浏览器端在 **`sessionStorage`**（key `astock.operatorToken.v1`）中保存，
由 `frontend/src/core/api.js` **仅对写方法**自动附加。语义是"本标签页本次会话"：
同一 tab 刷新保留，关闭 tab 即消失。它不是身份系统，只是一道本机/内网边界；
不要在共享浏览器上解锁。只读请求（GET/HEAD/OPTIONS）**绝不**携带凭据。

### 浏览器来源防护

对所有写方法：

- 请求带 `Origin` 时执行同源校验（比较 scheme / host / port，默认端口规范化）；
  跨源 → `403`。
- `Origin: null` → `403`（不解释成"没有 Origin"）。
- `Sec-Fetch-Site: cross-site` → `403`，**即使 Bearer 正确**。
- `Origin` 缺失（CLI / curl 常见）→ 继续按 token 状态与 IP 策略判断。

### 客户端 IP 判定

环回判定**只**使用 `request.client.host`，再用
`ipaddress.ip_address(...).is_loopback` 判定。应用层**不解析**
`X-Forwarded-For` / `X-Real-IP` / `Forwarded`——手工信任转发头会让远端客户端
伪装成本机。代理信任由 Uvicorn 的
`--proxy-headers --forwarded-allow-ips=127.0.0.1` 承担，应用层不建立第二套 parser。

### 反向代理必须保留 `Host` 的端口

浏览器访问 `http://server.example.com:8600` 时发送的 `Origin` 里**带端口**——
端口是 origin 的一部分。若反向代理把 `Host` 规范化成不带端口的形式，后端只能按
协议默认端口推断（http→80），于是 `8600 != 80`，**合法的同源写请求会被判
`cross_origin` → 403**。

因此 `deploy/astock-codex.nginx.conf` 用 `$http_host`（原样保留 `host:port`）
而不是 `$host`（会丢掉非默认端口）：

```nginx
map $http_host $astock_upstream_host {
    ""      $host;          # HTTP/1.0 客户端不发 Host 时的兜底
    default $http_host;
}

location / {
    proxy_pass http://127.0.0.1:18600;
    proxy_set_header Host $astock_upstream_host;
}
```

回归证据分两层，缺一不可：

- **静态**：`backend/test_operator_boundary.py::NginxProxyConfigTests` 锁住配置
  文件里不得出现 `proxy_set_header Host $host;`，且 map 变量必须被真正使用
  （只定义不使用等于死配置）。
- **行为**：`ReverseProxyIntegrationTests`（真实 TCP 反代 + 真实 uvicorn）与浏览器端
  `frontend/e2e/specs/reverse-proxy.spec.js`（真实 Chromium + 真实反代）双向验证——
  保留端口的形态下同源写请求成功，丢端口的形态下**复现 403**。
  后者是必要的：直连 Uvicorn 的 E2E 里 `Host` 天然带端口，**永远测不出**这个缺陷。

> **不要把 local-only 模式放在反向代理之后对外提供写服务。**
> 反代到后端的 TCP 连接来自 `127.0.0.1`，因此**所有**经反代而来的远端客户端在应用层
> 都表现为"环回客户端"。此时 local-only 模式只剩两道防线：来源防护与 `Host` 本地性。
> 若反代把 `Host` 固定改写成 `localhost`（而不是原样透传），这两道防线会一起失效，
> 远端就能写。对外提供写服务请配置 token（authenticated 模式），并让反代**原样透传**
> `Host`。

### Compose Credential Source

**server compose 的 `ASTOCK_OPERATOR_TOKEN` 唯一来源是 `env_file`。**

```yaml
env_file:
  - ${ASTOCK_ENV_FILE:-./.env.example}
environment:
  ASTOCK_SCHEDULE_FILE: /app/deploy/astock-codex.cron
  # 不得在此声明 ASTOCK_OPERATOR_TOKEN
```

原因：compose 的 `environment:` 优先级高于 `env_file:`。若写成
`ASTOCK_OPERATOR_TOKEN: "${ASTOCK_OPERATOR_TOKEN:-}"`，宿主未导出的空串会覆盖
`ASTOCK_ENV_FILE` 里已正确写入的 token，把服务悄悄降级成 UNSET（local-only）。
`backend/test_operator_boundary.py::ComposeSecurityTests` 对此有回归测试。

### 已知边界（本 PR 不做）

- 不引入 OAuth / 账户系统 / JWT / RBAC / session 框架。
- 不做 TLS 终止、不新增反向代理；这些属于运维层。
- 来源 IP 信任不由应用层判断——应用不解析 `X-Forwarded-For` 做授权，
  网络来源应由 nginx / 安全组 / ACL 层约束。

### 残余风险

1. 这是**单操作员共享密钥**边界，不是 RBAC，无法区分多个操作者。
2. **TLS 由部署层负责**：Bearer token 不应经公网明文 HTTP 传输。
3. 只读 GET 默认仍公开，看板数据对可达网络可见。
4. `sessionStorage` 不能抵抗同源 XSS——同源脚本仍可读取凭据。
5. 操作员边界不替代既有的风险门禁与人工确认（`confirmed=true` 仍独立生效）。
6. local-only 模式依赖环回 + 来源防护 + `Host` 本地性，不应通过额外网络转发暴露；
   使用自定义主机名必须配置 token 进入 authenticated 模式。
7. 反向代理配置错误（如用 `$host` 丢掉非默认端口）会让合法同源写请求 403——
   这是可用性问题，不是鉴权绕过；但它会诱使运维"关掉边界"，故必须按上节配置。


## 写接口清单

纸盘和 adaptive API 含有启动、暂停、下单、撤单、运行研究、写反馈和应用风控等状态变更接口。
当前 HTTP 控制面共有 **128 条路由，其中 61 条为写方法**（`POST`/`PUT`/`PATCH`/`DELETE`）。
它们全部由统一中间件覆盖（按方法判定，不按前缀），验证要点：

1. 本机/容器模式下宿主端口仅绑定环回地址（`127.0.0.1`）。
2. 写接口要求 operator 凭据；读接口不要求，读写分离。
3. 未授权请求不会因为携带 `confirmed=true` 而成功——`confirmed` 只是产品确认步骤。
4. 所有状态变更都能在审计记录中定位操作者、时间、版本和原因。

回归测试见 `backend/test_operator_boundary.py`，路由清单从 `main.app.openapi()`
生成（不维护人工白名单），覆盖三态矩阵、Origin 矩阵、Bearer 解析、query 凭据无效、
转发头伪造、GET 凭据不泄漏、domain handler 不被触达，以及 compose 优先级契约。

## 报告问题

不要在公开 Issue 中提交密码、Token、Cookie、数据库文件或包含密钥的日志。请使用仓库的 [GitHub 私密漏洞报告](https://github.com/daviesjoin-afk/astock-paper-trading/security/advisories/new)，提供复现步骤、受影响版本、影响范围和脱敏证据。该渠道只有仓库维护者可见；若页面不可用，请不要改用公开 Issue 披露敏感细节。

维护者会尽量在 7 天内确认收到报告。修复前请给维护者合理处理时间；确认修复和发布时间后再公开技术细节。
