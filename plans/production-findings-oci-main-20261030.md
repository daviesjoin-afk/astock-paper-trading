# 生产发现记录（2026-10-30 oci-main）

本文记录 2026-10-30 在 OCI 生产实例 `oci-main` 上做三端同步时**实测**到的事实。
不是设想，是服务器上跑出来的数字。后续排查同类现象先看这里。

---

## 1. 接入通道：直连超时 ≠ 服务器不可达

| 观测 | 结果 |
| --- | --- |
| 本机 SYN `<SERVER_IP>:22/80/443` | 全部 timeout，ICMP 0 收；tracert 到 Oracle 边缘路由器后无响应 |
| Shodan InternetDB（第三方视角） | `{"ports":[22],"cpes":["cpe:/a:openbsd:openssh:10.2p1","cpe:/o:canonical:ubuntu_linux"]}` |
| 经海外 HTTP CONNECT 代理连 22 | `HTTP/1.1 200 Connection established` |
| 本机到其它 Oracle 端点（`objectstorage.us-sanjose-1` :443） | OPEN |

结论：**服务器与安全列表都正常，是国内出口到该 IP 的路径丢包**。同一现象此前被
误判成「实例已停 / 公网 IP 变更」。

处理方式：走本机 Clash Verge 混合端口（`127.0.0.1:7897`，mihomo 默认值）做
HTTP CONNECT 隧道，SSH 走隧道。`scripts/oracle_ctl.py` 已内置
`ORACLE_PROXY` / `ORACLE_KEY`。

口令细节：`Oracle1559.` 与 `Oracle1559` 是两个口令，**尾部的英文句点是口令的一部分**。
SSH banner `OpenSSH_9.6p1 Ubuntu-3ubuntu13.20`（24.04），认证方式
`publickey / password / keyboard-interactive` 三者都开放；密钥
`~/.ssh/id_ed25519_astock` 可直接登录 `ubuntu`。

## 2. 三端同步结果

| 端 | 标识 |
| --- | --- |
| 本地 | `72c550b`（master，与 origin/master 一致） |
| GitHub | `72c550b`（PR #293 squash，8/8 CI 绿） |
| 服务器 | `/opt/astock-codex` = `72c550b`，镜像 `c9c19b1d`，四容器 healthy，`/api/health` 200 |

部署走的是**服务器自身的 git checkout**（`/opt/astock-codex/.git` 存在），
不是 rsync 投放：

```bash
cd /opt/astock-codex && git fetch origin && git reset --hard origin/master
ASTOCK_ENV_FILE=./.env sudo -n -E bash deploy/deploy.sh
```

`deploy/deploy.sh` 一次完成：构建带 `ASTOCK_GIT_COMMIT` 的镜像 → `up -d` →
`db_migrate.py all`（schema 已是最新 v40 / v11）→ 同步 `/etc/cron.d/astock-codex` →
健康检查 → 写 `.deploy-source.json`。宿主 `.env` 含 `ASTOCK_OPERATOR_TOKEN`，
由 compose `env_file` 读入，**不能**被 rsync 覆盖。

`deploy/preflight.sh` 在服务器上：`PASS=26 WARN=1 FAIL=0`（WARN 是"活动账本 863 条订单"，属提示）。

## 3. 新代码的生产验证（10:57:52 一轮实测）

| 代码 | 旧行为 | 新行为 |
| --- | --- | --- |
| 601956 | 双源差异 1.653% → 核验未通过 → 只写"待确认" | `skew=48s relax=True cross_source_checked`，`basis=price_evidence` → `confirmed=True` |
| 603239 | 同上 | `skew=48s relax=True`，核验通过，`confirmed=True` |
| 603341 | 同上 | `skew=51s relax=True`，价差 0.109% 落在放宽容差内 |

两处修复在生产上同时命中：
1. **行情源**：真实采样时滞 48–51s 被采到 `time_skew_seconds`，两源价差 0.0–0.22%
   落在时滞放宽容差内 → 判为 `cross_source_checked`（旧口径会直接拒绝）。
2. **下跌确认**：`intent=uncertain`（资金三件套全缺：`主力净流入占比`/`超大单资金`/`量比`，
   `confidence=0.4`）但价格证据齐全 → `basis=price_evidence` → `confirmed=True`，
   二轮确认终于可达。

## 4. 遗留问题（本轮暴露，未修）

### 4.1 资金流字段结构性缺失 → 意图长期 uncertain

`data_source_health.eastmoney.status = "degraded", rows = 0`，
`snapshot_flow_coverage = 0.0`，快照口径是 `price_only_full_market_snapshot`。
即：**东财 clist 全市场接口在 OCI 上取不到数据**，资金流只能靠腾讯 price-only 快照，
于是 `super_net` / `main_pct` / `vol_ratio` 三件套恒缺 → `main_force_intent` 恒为
`uncertain`（confidence 0.4）。本次修复让"价格证据"通道接管，但资金证据仍然缺失，
`distribution`（疑似出货）这条更强的通道在这台机器上基本不会触发。

待办：查东财 clist 在美国机房被拒的具体原因（HTTP 状态/超时/`http_breaker_skip`
calls=45 errors=45），或给资金流加腾讯/新浪备用源。

### 4.2 扫描周期 18 分钟 >> cron 3 分钟 → 多数 slot 被跳过

实测 `run_key` 序列：`intraday:202610301003` → `:202610301021` → `:202610301039` → `:202610301051`，
即约 18 分钟一轮，而 cron 是每 3 分钟一次。跳过的轮次记
`status=in_progress, reason=上一轮全市场任务仍在执行，本轮不并发重入`。

耗时分布（单轮）：
`intraday_scan` 644–678s、`market_context` 369–383s、`position_quotes` 138–140s、
`http_get_attempt` 500s（152 次调用 / 27 次错误）、`candidate_bootstrap` 270–290s、
`risk_snapshot` 298–309s。

这不是"崩溃"，是**单轮太慢 + 3 分钟频率不匹配**：3 分钟防线的语义被稀释成
18 分钟一次，`downside_confirmed` 要求的"间隔 ≥3min-30s"虽然满足，但减仓实际延迟
可达 18 分钟。要么把单轮压到 3 分钟内，要么把 cron 频率对齐真实周期。

### 4.3 其它观测（未处理）

- `factor_columns_missing` 告警：`amount`/`turnover` 整列缺失（腾讯 price-only 快照
  不带这两个字段）。`marketdata_providers._tencent_volume_amount_turnover` 已能从腾讯
  报文补齐，但当前走的是快照路径，没走到解析路径。
- `execution_orders` 里 6 张委托长期 `pending_execution`，`reasons=["TRADABILITY_UNKNOWN"]`：
  成交性观测不可用导致委托挂起不成交（含 601956 1900 股、603239 500 股）。
- `market_context` 报 `实时沪深300 -0.72%`，`breadth` 取自 `2026-10-29`（隔日 breadth）。
