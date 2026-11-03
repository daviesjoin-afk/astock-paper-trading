# 行情源可达性与盘中扫描耗时（2026-11-03 现场取证）

本文只记录**当次实测**：服务器 `<SERVER_HOST>`（香港，Ubuntu，2 vCPU / 11 GiB，
Docker profile `astock-codex`）在 2026-11-03 13:09—13:30 的可观测事实。
没有任何收益推断；不能把下面的时间当成"改完之后一定快多少"的承诺。

## 1. 当日调度结果（旧版本 714b434，09:57—13:10）

来源：`/opt/astock-codex/reports/scheduler.log` 的 `paper_slot_exit` 事件，
日期 2026-11-03。

| slot | 成功 | 失败 |
| --- | --- | --- |
| intraday | 0 | 37 |
| risk | 17 | 20 |
| execution-resume | 22 | 15 |

`intraday` 当天 **0 成功 / 37 失败**，失败原因字段 `risk_scan_state=running` +
`error=slot_work_incomplete`：本轮 `monitor_risk` 只等 20 秒（旧
`DEFAULT_SETTLE_TIMEOUT_SECONDS=20`），而同窗口风控实际需要约 80 秒，于是每个 tick
都在风控没跑完时放弃，候选扫描与委托恢复从未真正开始。

## 2. 单轮风控的时间构成（13:03 与 13:06 两个窗口，同一口径）

`paper_slot_timing.timing.stages`，单位秒：

| 阶段 | 13:03 risk | 13:06 risk | 说明 |
| --- | --- | --- | --- |
| engine_import | 3.7 | 3.7 | 纯 Python 导入 |
| http_get_attempt | 79.5（7 次调用 / 5 次错误） | 79.7（7 / 5） | **几乎全部墙钟** |
| quote.primary | 18.0 | 18.0 | 东财 ulist |
| quote.flow | 42.0 | 42.0 | 资金流补全（逐票） |
| quote.tradability | 18.1 | 18.1 | 停牌/可交易性 |
| quote.independent | 0.7 | 0.7 | 腾讯核验源 |
| position_quotes（外层） | 79.7 | 79.8 | 含上面 4 项 |
| risk_pass（外层） | 81.0 | 81.0 | CPU 仅 0.72 s |

同期 `intraday` 的 `source_health` 43.2 秒（5 次调用 / 3 次错误），且它排在风控门禁
**之前**，风控没跑完也照样先烧掉这 43 秒。

## 3. 出口可达性（同一台服务器，13:20 实测）

`curl -s -o /dev/null -m 8 -w 'code=%{http_code} connect=%{time_connect} total=%{time_total}'`

| 主机 | 结果 | 备注 |
| --- | --- | --- |
| push2.eastmoney.com | code=000，25 s 超时（connect 未建立） | DNS 解析成功，TCP 不建立 |
| 82.push2.eastmoney.com | code=000，8 s 超时 | |
| push2delay.eastmoney.com | code=000，8 s 超时 | |
| push2his.eastmoney.com | code=000，8 s 超时 | **K 线主机** |
| 92.push2his.eastmoney.com | code=000，8 s 超时 | |
| np-listapi.eastmoney.com | code=000，8 s 超时 | |
| emappdata.eastmoney.com | code=000，8 s 超时 | |
| datacenter-web.eastmoney.com | 200，0.48 s | 可用 |
| data.eastmoney.com | 200，2.25 s | 可用 |
| qt.gtimg.cn（腾讯报价） | 200，0.67 s（3 只票批量） | 可用 |
| web.ifzq.gtimg.cn（腾讯 K 线） | 200，0.86 s | 可用 |
| hq.sinajs.cn（新浪报价） | 200，0.50 s | 可用 |
| money.finance.sina.com.cn（新浪 K 线） | code=000，8 s 超时 | **新浪 K 线也不可达** |

结论（事实层）：承载**报价（ulist/clist）与资金流**的东财 push2 家族在这台机器的
出口上 TCP 不通；承载 **K 线的 push2his 家族**同样不通；新浪的 K 线域名也不通。
可用的只剩腾讯（qt.gtimg.cn / web.ifzq.gtimg.cn）、新浪报价（hq.sinajs.cn）和
东财的 datacenter-web/data 两个域名。

这与"K 线新鲜覆盖率约 38.7%、全市场快照超过 30 分钟"是同一件事的两面：
主源整段不可达时，代码会依次付出超时预算再落到腾讯兜底，落地失败的标的就没有新鲜数据。

## 4. 已部署与未部署的修复

- `d30dc4e`（已并入 master、已在本轮部署）：故障主机遵守冷却、跨进程降级用绝对
  到期时间、风控等待 20 s→120 s 且有窗口上限、intraday 改到风控门禁之后再探活、
  成功风控时间不再固定偏向 risk slot。
- 未做（下一阶段）：把"哪个主机整段不可达"的事实跨进程持久化（目前 clist 的
  主机冷却 `_clist_host_health` 只在进程内，每个 tick 都是从零学一遍）；以及让
  报价/资金流在 push2 不可达时**先用腾讯**而不是先付超时。

## 5. 验收口径（部署后按此复测）

同一份 `scheduler.log`、同一批字段：

1. `intraday` 的成功数 / 失败数，失败原因是否仍为 `risk_running`；
2. `risk_pass` 与 `position_quotes` 的 p50/max；
3. `http_get_attempt` 的调用次数与错误次数（错误次数下降说明短路生效）；
4. `source_health` 是否只在风控完成后出现；
5. `runtime_commit` 是否已变为部署 commit（防止用旧样本冒充新结果）。

不允许用"请求级变快"换算成"整轮扫描变快"或收益改善。

## 6. 部署后复测（2026-11-03 13:52—14:40，同一份 scheduler.log）

部署身份：`.deploy-source.json` → `commit=2844b8f7…`、`image=f18be56a…`、
`deployed_at=13:52:42+08:00`、`health=healthy`、`api_health=200`；
容器 `ASTOCK_GIT_COMMIT=2844b8f7…`。窗口内 18 个 attempt 全部同一 commit。

| 指标 | 基线（714b434 / 09:34—13:10） | 部署后（2844b8f / 13:52—14:40） |
| --- | --- | --- |
| intraday 成功 | 0 / 34（714b434 之后） | 10 completed / 13 start（+1 failed、1 进行中、3 个 exit≠0） |
| risk 成功 | 17 / 34 | 14 / 14 |
| execution-resume 成功 | 20 / 34 | 14 / 14 |
| 实际完成风控扫描 | 34 次，间隔 p50 180 s | 14 次，无跨入口重复 completed |
| quote.flow | p50 42.0 s | p50 0 s / max 3.0 s |
| quote.tradability | p50 18.1 s | p50 0.13 s / max 0.62 s |
| position_quotes | p50 79.7 s | p50 6.6 s / max 21.5 s |
| quote.primary | p50 18.0 s | p50 4.5 s / max 15.5 s |
| open_execution | 54.6 s（单样本） | p50 6.6 s / max 16.5 s |
| execution.market_snapshot | 224 s（单样本，部署当日） | p50 6.0 s / max 15.4 s |
| intraday_scan | 530—630 s（部署后、修 transport 前） | p50 134.8 s / max 297.4 s |

部署后仍有 3 次 intraday `exit≠0`、1 次 failed：这些样本的 `intraday_scan` 落在
297 秒附近，说明单轮仍会跨过 3 分钟网格（p50 134.8 s 尚可，尾部仍超窗）。
这与"仍然每轮都失败"是两件事，不能混写。

本轮为这些数字做的改动（按提交顺序）：

1. `d30dc4e`（Codex，本轮部署）：风控等待 20 s→120 s 且有窗口上限、故障主机遵守
   冷却、跨进程降级用绝对到期时间、intraday 先风控后探活。
2. `cace215`（部署脚本）：运行期三个目录归 app(10001)，迁移前等 API 就绪、
   迁移失败即失败、五个容器必须 running 才写身份文件。13:21 那次部署把
   `data_cache` chown 成 ubuntu，API 立刻 `readonly database` 崩溃重启，是靠人工
   重启才恢复的 —— 这条修复的来源就是这次事故。
3. `4b445e4`（传输层）：降级档主机的廉价探测失败后把"继续跳过"窗口从 90 秒延长到
   300 秒，探测预算 6 s→2.5 s。依据：`transport_breaker.json` 里 push2 家族
   `trips` 已达 216、`fails` 218，但每个 90 秒冷却到期后仍要重新付一次探测。

仍未做（下一阶段）：`intraday_scan` 尾部 297 秒超窗。已定位的可优化项（均带行号，
见 `docs/MARKETDATA_FLOW_LATENCY_2026-11-03.md` 与 teammate 简报）：
`execute_open` 的第二份 `check_data_source_health(force=True)` 与 intraday 主体重复
（约 43 s）；全市场刷新的 240 秒磁盘 TTL 与 530—630 秒的轮次时长不匹配；
`candidate_bootstrap` 的 p50 71.8 秒属因子表/评分，不在取数层。

