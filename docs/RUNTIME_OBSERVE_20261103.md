# 运行时观测报告：scheduler.log（2026-11-03）

- 观测对象：Oracle 服务器 `/opt/astock-codex/reports/scheduler.log`（JSON 事件流）
- 观测工具：`scripts/runtime_observe.py`（本次新增，只用标准库 + 既有 paramiko/oracle_ctl）
- 观测时刻：2026-11-03 13:15:09 +08:00（最后一次全量拉取）
- 基线窗口：2026-11-03 09:34:00 → 13:10:00（+08:00），runtime_commit `714b434`
- 部署后窗口：**待部署后补测**（远端仍跑 `714b434`，本机已提交但未部署的 HEAD 是 `d996ccb`）

本报告只记录**日志里能读到的事实**与其口径。它**不做收益推断，也不下"性能已改善"的
结论** —— 只有一份版本的单窗口样本，没有对照组。第 7 节列了明确不做的推断。

---

## 1. 工具：`scripts/runtime_observe.py`

```bash
# 默认：远端最近 20000 行（当前文件只有 9264 行 → 等于全量），人读摘要打到 stdout
.venv\Scripts\python.exe scripts/runtime_observe.py --lines 2000

# 基线窗口 + 结构化落盘
.venv\Scripts\python.exe scripts/runtime_observe.py --lines 20000 ^
    --label baseline-714b434 ^
    --since 2026-11-03T09:34 --until 2026-11-03T13:10 ^
    --json  <输出>.json --markdown <输出>.md

# 离线复算：先 --keep-raw 拉一份原始日志，之后完全本地跑
.venv\Scripts\python.exe scripts/runtime_observe.py --source local --keep-raw raw.log ...
```

拉取实现（两条路都用仓库既有的 `scripts/oracle_ctl.py` 连接参数，含 `.env.oracle`
里的 HTTP CONNECT 代理）：

| 方式 | 命令 | 实测耗时（2000 行 / 11.4 MB） | 说明 |
| --- | --- | --- | --- |
| `--pull exec`（默认） | `oracle_ctl.py exec -- 'tail -n N LOG'` | **≈ 21 s** | 远端一次性 tail，经 `sudo -E bash -lc`，不受文件属主限制 |
| `--pull sftp` | paramiko SFTP 按块从文件尾倒读 | ≈ 206 s | 二进制安全、不做编码转换；要求登录用户对该文件有读权限（曾实测一次 `PermissionError`，此时脚本自动回退 exec） |
| `--source local` | 直接读本机文件 | 秒级 | 用于复算/留证；`--keep-raw` 就是为它准备原始件 |

原始字节一律先落本地临时文件再读回解析（`--keep-raw` 可保留），所以"拉取"与"解析"
可以分开复核。

### 坏输入容错（生产日志实测有三类）

1. **GBK / UTF-8 混排**：中文 `reason` 显示为乱码。脚本只按 **JSON 字段名/字段值** 归类，
   不匹配中文文本（这是本次报告能成立的前提）。
2. **GBK 尾字节撞上反斜杠**：GBK 双字节字符尾字节可为 `0x5C`，UTF-8 解码后形成 `\x`
   这类非法 JSON 转义 → 首次解析失败，脚本在有界窗口内做一次转义修复后重试并计数
   `repairs`。
3. **截断行 / 非 JSON 行**：逐条计入 `unparsed`，不抛异常、不中断聚合。

解析器用 `json.JSONDecoder.raw_decode` 在整段文本上流式推进，因此**既兼容一行一个
紧凑 JSON，也兼容早期 `jq` 美化过的多行缩进 JSON**（全量文件 9264 行 → 6898 条记录，
差的 2366 行主要是多行对象的续行与 traceback 等纯文本行）。

---

## 2. 样本与窗口

| 项 | 值 |
| --- | --- |
| 拉取字节 | 48,371,823（全量文件） |
| 文件行数 | 9,264 |
| 解析出的 JSON 记录 | 6,898 |
| `unparsed`（非 JSON/截断） | 8（见 §6，全部是 traceback 里的 Python 源码回声） |
| `repairs`（GBK 反斜杠修复） | 0（本文件未触发；用构造样本验证过，见 §6） |
| 全文件首/末**带时间戳**记录时刻 | 2026-10-21T09:25:03+08:00 / 2026-11-03T13:15:09+08:00 |
| **窗口内记录** | **409** |
| **窗口内尝试数**（`timing.attempt_id` 去重） | **104** |

`--since/--until` 的口径：记录时刻取 `timing.started_at` → 顶层 `started_at` → 顶层
`at`（后者只用于主机 shell 发的三个事件）。**注意边界**：`paper_slot_exit` 用自己的
`at` 打戳，所以 13:09 启动、13:10:26 退出的那次 intraday，其 exit 事件落在窗口外
（`intraday` start=34 / exit=33 就是这个原因，不是丢事件）。

### runtime_commit 分布（字段：`timing.runtime_commit`，来自容器 `ASTOCK_GIT_COMMIT`）

| runtime_commit | 尝试数 | 首次出现 | 末次出现 |
| --- | --- | --- | --- |
| `714b4349cfa2bfc7714cde0188fef440df92fb07` | 102 | 2026-11-03T09:57:02+08:00 | 2026-11-03T13:09:05+08:00 |
| `3c6d1a665d4561b073843294a6d51bf775f1c55f` | 2 | 2026-11-03T09:35:02+08:00 | 2026-11-03T09:36:01+08:00 |

版本切换点（`started_at` 序上 commit 发生变化）：

- **2026-11-03T09:57:02+08:00：`3c6d1a6` → `714b434`（slot=intraday）**

远端 `.deploy-source.json`：`commit=714b434`、`base_commit=3c6d1a6`、
`deployed_at=2026-11-03T01:56:06Z`（= 09:56:06 +08:00），与日志切换点一致（09:36 之后、
09:57 之前换容器）。因此本报告窗口里 **09:35–09:36 的 2 个尝试属于旧版本
`3c6d1a6`，其余 102 个属于 `714b434`**，两者不能混在一起算比例。

---

## 3. 口径与原始事件字段名（每个指标一行）

| 指标 | 口径 | 原始字段 | 样本量 |
| --- | --- | --- | --- |
| slot 归属 | `paper_slot_timing` / runner 结果行取 **`timing.slot`**；`paper_host_lock`/`paper_slot_start`/`paper_slot_exit` 取**顶层 `slot`**，两种形状互不回落 | `timing.slot`、`slot` | 104 attempts / 201 主机事件 |
| 一次尝试 | 一个 `timing.attempt_id`。同一 attempt 在日志里落两条（`scope="finished"` 的 `paper_slot_timing` + `scope="runner_result"` 的结果行），按 id 合并、不重复计数 | `timing.attempt_id`、`timing.scope` | 104 |
| 主机锁 | `paper_host_lock` 的 `status`：`not_acquired`（没抢到，本轮没进 runner）/ `yielded_to_full_scan`（fast-entry 主动让路）；等待秒数取 `wait_seconds`（有 start 事件时以它为准） | `event`、`status`、`wait_seconds`、`slot` | 窗口内 0 条 |
| **实际完成的风控扫描** | `timing.risk_scans[]` 中 `status=="completed"` 的身份 `(cycle_id, asof_date, scan_minute)` 去重计数。外层 slot `completed` **不等于**又扫了一次 | `timing.risk_scans[].status`、`.cycle_id`、`.asof_date`、`.scan_minute` | 标记 136 条 → 34 个身份 |
| 跨入口重复扫描身份 | 同一身份出现在**不同 slot** 的 `risk_scans`；`completed` 同时出现在两个 slot 属契约异常，单独统计 | 同上 + 记录所属 `timing.slot` | 34 个身份 × 3 slot |
| 风控开始间隔 | 各 `risk_scans[].started_at` 排序后，**不同 scan_minute 相邻两条**的差值；只把"同日且间隔 ≤30 min"的对计入节拍，跨午休/跨日单列 | `timing.risk_scans[].started_at` | 32 对（+1 对被排除） |
| slot 成功/失败 | attempt 级：`status ∈ {completed, ok}` → completed；`{already_done, skipped, up_to_date, already_scanned}` → idle；`{failed, error, blocked}` → failed；另有 `in_progress` / unknown | `paper_slot_timing.status` | 104 |
| 失败归类 | **只读 JSON 字段**：`error`、`retry_reason`、`risk_scan_state`（顶层或嵌套 `risk.risk_scan_state`/`risk.status`）、`status`。判序：`paper lease lost*` → `lease_lost`；state ∈ {running,in_progress,claimed} 或 retry 同值 → `risk_running`；retry=`risk_in_progress` → `risk_in_progress`；error=`slot_work_incomplete` → `slot_work_incomplete`；error 含 `timeout` → `timeout`；其余 `other:<error>` | 同上一行 | 65 次 failed/in_progress |
| 阶段秒数 | `timing.stages[name].seconds` 是该 attempt 内**该阶段累计**秒数（**含子阶段，不能相加得总耗时**）；`calls`/`errors` 为累计计数 | `timing.stages.*.seconds/.calls/.errors` | 见 §4.5 |
| risk_snapshot_refresh | runner 结果行**顶层** `risk_snapshot_refresh` 的字符串值 | `risk_snapshot_refresh` | 18 条（按 attempt 去重） |

---

## 4. 基线窗口（714b434，09:34–13:10）：数字

### 4.1 各 slot 事件计数

| slot | `paper_slot_start` | `paper_slot_exit` | exit≠0 | `paper_slot_timing` | timing 状态分布 |
| --- | --- | --- | --- | --- | --- |
| `risk` | 34 | 33 | 17 | 34 | completed 17 / failed 17 |
| `intraday` | 34 | 33 | 33 | 35 | completed 1 / failed 34 |
| `execution-resume` | 34 | 33 | 13 | 34 | completed 20 / failed 14 |
| `open` | 0 | 0 | 0 | 1 | already_done 1 |

`paper_host_lock`：窗口内 **0 条**（`not_acquired` 0、`yielded_to_full_scan` 0），
即三个 lane 都没被主机锁挡下；`intraday` 的 `wait_seconds` p50=0、**max=8 s**。

三种 lane 在窗口内各 34–35 次尝试：节拍是**每 3 分钟一次**（09:57–11:24 的 30 拍 +
13:00–13:09 的 4 拍），三条 lane 对齐到同一批 `scan_minute` 上互相竞争。

### 4.2 实际完成的风控扫描：34 次，节拍 180 s

- `risk_scans` 标记 **136** 条：`completed` **34**、`risk_running` **99**、`already_scanned` **3**。
- `completed` 的**不同身份 34 个**（`cycle_id=15`、`asof_date=2026-11-03`，即当日只有
  一个调度周期），身份字段缺失的标记 0 条。
- 按 slot 归属：

| slot | completed | already_scanned | risk_running |
| --- | --- | --- | --- |
| `risk` | 17 | 0 | 17 |
| `execution-resume` | 17 | 3 | 48 |
| `intraday` | **0** | 0 | 34 |

- 完成的分钟（`scan_minute`）是完整的 3 分钟网格，**没有缺拍**：
  `09:57 → 11:24` 共 30 个（步长严格 180 s），午休后 `13:00 → 13:09` 共 4 个。
- **风控开始间隔**（`risk_scans[].started_at`，相邻不同分钟）：32 对，
  **p50 = 180 s，p95 = 181 s，min = 179 s，max = 181 s**；被排除的跨时段对 1 个：
  `11:24:06 → 13:00:06`（5760 s，午休）。

### 4.3 跨入口重复扫描身份：34/34 都落 3 个 slot，但 `completed` 从未重复

- 34 个身份全部在 `{intraday, risk, execution-resume}` 三个 slot 里出现过。
- 每个身份的 `completed` **恰好由单一 lane 持有**（`risk` 17、`execution-resume` 17、
  `intraday` 0），**`completed_in_multiple_slots` = 0** —— 耐久认领的互斥在这段日志里
  没有被破坏。
- 换句话说：**同一分钟确实是三个入口同时在抢，只有一个真跑完，其余记 `risk_running`
  或 `already_scanned`。** 这是本窗口最集中的失败来源，见 §4.4。

### 4.4 intraday / execution-resume 的成功与失败

| slot | attempts | 成功（completed） | 失败 | 失败归类（JSON 字段） | elapsed p50 / max |
| --- | --- | --- | --- | --- | --- |
| `intraday` | 35 | **1**（2.9%） | 34 | `risk_running` **34** | 66.165 s / 622.847 s |
| `execution-resume` | 34 | 20（58.8%） | 14 | `risk_running` 14 | 84.363 s / 125.961 s |
| `risk` | 34 | 17（50.0%） | 17 | `risk_running` 17 | 54.284 s / 127.729 s |

要点（都是字段可对的原话，不是推断）：

- `intraday` 的 34 次失败**全部**是 `error="slot_work_incomplete"` +
  `risk_scan_state="running"`（嵌套 `risk.risk_scan_state` 同值），归类 `risk_running`；
  **没有** `timeout`、`lease_lost`、`blocked` 归类出现。
- `intraday` 唯一成功的 1 次是 **09:36:01 的旧版本 `3c6d1a6`**（elapsed 622.8 s，正是
  上表 max 的来源）。**换到 `714b434` 之后，`intraday` 34 次尝试 0 成功**。
- `intraday` 的 elapsed p50 66.2 s 与失败原因吻合：`risk_pass` 只有 20.3 s，之后判定
  "本轮风控已由别的入口执行"提前收工。
- `execution-resume` 与 `risk` 各自也会输给另一方（各 14 / 17 次 `risk_running`），
  即这两个 lane 之间存在互相抢同一分钟的失败，不独是 `intraday` 的问题。

### 4.5 阶段秒数分布（attempt 内累计值）

| 阶段 | n | p50 | max | calls 合计 | errors 合计 | 有错误的 attempt |
| --- | --- | --- | --- | --- | --- | --- |
| `risk_pass` | 103 | 59.843 s | 137.124 s | 137 | 0 | 0 |
| `http_get_attempt` | 69 | 79.447 s | 438.680 s | 502 | **294** | 69 |
| `source_health` | 35 | 43.231 s | 44.540 s | 35 | 0 | 0 |
| `position_quotes` | 35 | 79.741 s | 80.819 s | 35 | 0 | 0 |
| `quote.flow` | 35 | 42.046 s | 42.367 s | 35 | 0 | 0 |
| `quote.primary` | 35 | 18.024 s | 18.195 s | 35 | 0 | 0 |
| `quote.tradability` | 35 | 18.130 s | 19.253 s | 35 | 0 | 0 |
| `quote.same_source_fallback` | 35 | 1.159 s | 1.601 s | 35 | 0 | 0 |
| `quote.independent` | 35 | 0.697 s | 0.888 s | 35 | 0 | 0 |
| `quote.local_metadata` | 35 | 0.227 s | 0.238 s | 35 | 0 | 0 |
| `quote.snapshot_read` | 35 | 0.093 s | 0.116 s | 35 | 0 | 0 |

`risk_pass` 按 lane 拆开（同一字段、不同样本子集）：

| lane | n | p50 | max |
| --- | --- | --- | --- |
| `risk` | 34 | 50.108 s | 123.756 s |
| `execution-resume` | 34 | 70.232 s | 122.001 s |
| `intraday` | 35 | 20.251 s | 137.124 s |

`http_get_attempt` 按 lane 拆开：

| lane | n | p50 | max | calls | errors |
| --- | --- | --- | --- | --- | --- |
| `risk` | 17 | 79.727 s | 81.080 s | 122 | 85 |
| `execution-resume` | 17 | 79.711 s | 81.111 s | 123 | 85 |
| `intraday` | 35 | 43.225 s | 438.680 s | 257 | 124 |

其它值得定位的阶段（按 p50 取前几名，`intraday_scan` 是 `intraday` 的外层聚合阶段）：

| 阶段 | n | p50 | max |
| --- | --- | --- | --- |
| `risk_snapshot` | 1 | 135.168 s | 135.168 s |
| `candidate_bootstrap` | 1 | 103.274 s | 103.274 s |
| `open_execution` | 1 | 97.439 s | 97.439 s |
| `intraday_scan` | 35 | 63.502 s | 485.762 s |
| `execution_risk_retry_backoff` | 17 | 20.000 s | 20.000 s |
| `engine_import` | 104 | 3.708 s | 5.078 s |

`http_get_attempt` 是本窗口唯一有非零 `errors` 的阶段：**502 次调用 / 294 次失败
（58.6%）**，且 69 个带该阶段的 attempt **全部**至少有一次错误。阶段 `seconds` 是
累计值（含重试），因此 "79 s 的 p50 + 5/7 次失败" 是同一件事的一面，不能当网络请求
成功率用。

### 4.6 runtime_commit 与 `risk_snapshot_refresh`

- commit 分布见 §2。窗口内**只有一次**版本切换（09:57:02 起 `714b434`），最后一个
  样本 13:09:05 仍是 `714b434`。
- `risk_snapshot_refresh`（按 attempt 去重 18 条）：
  `{"already_running": 13, "completed": 1, "scheduled": 4}`。
  按 slot：`risk` → `scheduled` 4 / `already_running` 13；`intraday` → `completed` 1
  （就是 09:36 那次旧版本成功尝试）。窗口内没有 `failed`。

---

## 5. 部署后窗口：待部署后补测

事实（截至 2026-11-03 13:21 +08:00）：

| 检查项 | 结果 |
| --- | --- |
| 远端 `.deploy-source.json` | `commit=714b434`、`base_commit=3c6d1a6`、`deployed_at=2026-11-03T01:56:06Z`、`health=healthy`、`api_health=200` |
| 容器 `ASTOCK_GIT_COMMIT` | `714b4349cfa2bfc7714cde0188fef440df92fb07` |
| 容器 | `astock-codex` `Up 3 hours (healthy)`；另 4 个 worker（data/paper/task/candidate）均 `Up 3 hours` |
| 本机 git HEAD | `d996ccb`（领先 3 个提交：`d30dc4e`、`3c28b95`、`d996ccb`） |
| 日志里最晚样本 | 13:12:02（`714b434`）；扩展到 13:00–13:20 复算：15 attempts、`transitions: []`，全部 `714b434` |

结论：**新版（`d996ccb`）尚未部署，因此"部署后窗口"现在不存在，写"待部署后补测"。**
脚本本身已就绪，补测只需要两条命令：

```bash
# 1) 部署后（等到容器 ASTOCK_GIT_COMMIT 变化）跑同一窗口口径
.venv\Scripts\python.exe scripts/runtime_observe.py --lines 20000 ^
    --label post-<new-commit> ^
    --since <部署时刻> --until <观测时刻> ^
    --json post.json --markdown post.md

# 2) 判据（看两个地方，不靠肉眼找时间）
#    JSON: runtime_commit.transitions 非空 且 to == 新 commit
#    JSON: runtime_commit.by_attempt 里出现新 commit 的尝试数 > 0
```

复算基线用的是同一份脚本、同一套口径，指标口径表在 §3，可直接与上面对比。

---

## 6. 脚本验证记录（真实输出）

### 6.1 远端拉取 + 2000 行（`--pull sftp` 的一次完整运行）

```
来源 | remote / sftp
主机 / 远端文件 | <SERVER_HOST> /opt/astock-codex/reports/scheduler.log
拉取字节 | 11,418,066
观测到的首/末记录时刻 | 2026-10-23T10:51:01+08:00 / 2026-11-03T13:18:06+08:00
窗口内记录 / 总记录 | 1994 / 1994
解析失败行（unparsed） | 0
转义修复行（repairs） | 0
尝试数（attempt_id 去重） | 538
real 3m26.356s
```

同一批 2000 行用默认 `--pull exec` 的两次跑：`11,511,379` 字节 / `536` attempts /
`real 0m21.277s`，以及 `11,384,535` 字节 / `537` attempts / `real 0m17.274s`。
三跑的数字在小数位上不同，是**同一份日志在长**：末条记录依次是 13:13:27 → 13:18:06
→ 13:21:03。对同一份原始文件做 `--source local` 复算才是逐位可复现的。

### 6.2 全量拉取（20000 行 ≥ 文件 9264 行）

```
拉取字节 | 48,371,823
窗口内记录 / 总记录 | 409 / 6898
解析失败行（unparsed） | 8
转义修复行（repairs） | 0
尝试数（attempt_id 去重） | 104
real 0m36.5s
```

那 **8 条 `unparsed` 全部是真·非 JSON 文本**（不是解析器漏读），实测内容为：
traceback 里回显的 f-string 片段 `{"degraded", "stale", "future_excluded"} for value in
quality_values) else (`、`'{reason}…{_dumps(factor_refresh.get('refresh_gate') or {})}"`、
以及 `{\n  File "/app/backend/paper_trading.py", line 2010, in _audit`。脚本把它们跳过
并计数，聚合照常完成。

### 6.3 构造坏输入（本地，验证不崩）

同一文件里放入：紧凑 JSON、`jq` 美化多行 JSON、`error`/`retry_reason`/嵌套 `risk` 都
齐全的结果行、**GBK 双字节 + 尾字节 `0x5C`** 造出的非法转义行、一条被截断的
`paper_slot_timing`、一行 Python traceback 垃圾：

```
解析失败行（unparsed） | 1        ← 截断行
转义修复行（repairs）  | 1        ← GBK 反斜杠行，修复后成功解析
attempt_id 去重尝试数  | 1
exit=0
```

`sftp` 权限不足那条分支也实测触发过：`PermissionError: [Errno 13] Permission denied`
→ 自动回退 `exec` 并继续，报告里 `来源` 会写成 `remote / exec`。

---

## 7. 明确不做的推断与已知盲区

不做（本报告没有足够证据）：

1. **不判断"性能已改善/变差"**：只有一个版本（`714b434`）的一个窗口，没有对照组。
   部署后窗口为空，任何对比都缺另一半。
2. **不判断策略收益**（任务也不需要）：收益与这些事件无映射关系。
3. **不把 `intraday` 34/34 失败直接归因于"风控节拍太快"**：字段只能说"同一分钟的
   风控已被另一入口认领"，谁是因谁是果需要另做实验（例如只放单一 lane 的对照窗口）。
4. **不把 `http_get_attempt` 的 `errors` 当网络失败率**：它是阶段内累计计数，一个
   阶段调用可能含多次重试；且 `seconds` 含重试等待。

已知盲区（口径本身决定）：

- 外层 cron 的 `flock -n` 拒绝**不写日志**；fast-entry 日志另有去向。所以"尝试数"
  只是日志里可观测的尝试，不是 cron 触发数。
- `paper_slot_start` / `paper_slot_exit` / `paper_host_lock` 三个事件由
  `deploy/run_paper_slot.sh` 发出，**只在该脚本上线后才有**；更早的样本只有
  `paper_slot_timing`，因此早期窗口的 start/exit 计数天然为 0。
- 窗口边界按记录时刻裁剪，`exit` 用自身 `at` 打戳，跨边界的 start/exit 会差 1 条。
- `risk_scans` 的身份依赖 `cycle_id`/`asof_date`/`scan_minute` 三字段齐全；缺失时
  `identity_incomplete` 计数（本窗口为 0）。
- `unparsed` 行数只统计"以 `{` 开头但解析失败"的片段；纯文本行（traceback 正文、
  cron 输出）不计入，也不影响聚合。

---

## 8. 复现清单

```bash
cd <PROJECT_ROOT>

# 1) 基线窗口（远端拉全量 + 窗口裁剪 + 结构化落盘）
.venv\Scripts\python.exe scripts/runtime_observe.py --lines 20000 ^
  --label baseline-714b434 --since 2026-11-03T09:34 --until 2026-11-03T13:10 ^
  --keep-raw %TEMP%\scheduler_raw.log ^
  --json %TEMP%\baseline.json --markdown %TEMP%\baseline.md

# 2) 完全离线复算（不再连服务器）
.venv\Scripts\python.exe scripts/runtime_observe.py --source local ^
  --log %TEMP%\scheduler_raw.log --lines 20000 ^
  --since 2026-11-03T09:34 --until 2026-11-03T13:10

# 3) 只问"有没有换版本"（最近 5 小时的窄窗口，最快）
.venv\Scripts\python.exe scripts/runtime_observe.py --lines 2000 ^
  --since 2026-11-03T13:00 --until 2026-11-03T23:59 --json %TEMP%\deploy_check.json
```

`--json` 的关键字段速查：`parse.unparsed/repairs`、`window.records_in_window`、
`runtime_commit.by_attempt/transitions/latest_commit`、`slots.*`、
`risk_scans.completed_unique_identities`、`risk_scan_intervals.seconds`、
`slot_outcomes.*.failure_reasons`、`stages.named|quote|other`、
`risk_snapshot_refresh`。
