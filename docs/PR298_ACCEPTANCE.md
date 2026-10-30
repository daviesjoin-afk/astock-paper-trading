# PR298 固定输入验收记录

基线：3c6d1a665d4561b073843294a6d51bf775f1c55f。实现提交：549d49c。验收只增加测试和探针，不修改生产逻辑。

## 行为对照

同一 Windows / Python 3.14.5 / 项目虚拟环境；两份独立 checkout；使用同一份修复后的探针，时钟参数为 `freeze_time("2026-10-30 09:00:00", tz_offset=8)`，不随真实日期推进；复用现有离线真实账本夹具，不访问在线行情。验收工具依赖 freezegun 1.5.5 安装在临时工具目录，没有加入应用依赖。

冻结时钟先于生产模块和回放测试模块的导入，确保导入时计算的 `D0` 等日期也固定（本次 `D0=2026-11-02`）。仅 pandas 的原生日期扩展在冻结前预加载，避免 Windows / Python 3.14 下导入崩溃。修改版另用独立进程重复运行，并在外层时钟设为 `2031-03-17 01:00:00` 的进程中再运行；三次完整 JSON 与基线完全一致。冻结时钟下 unittest 显示的 0 秒不作为性能数据。

每边 10 个场景均通过。比较风险返回结果及 12 张表所有列，包含订单、成交、账户、持仓、lot、资金预占、NAV、审计、cycle、风险状态、风险决策与风险扫描生命周期。17 个检查点共 4,775 个账本字段逐值一致；不删除时间戳、ID、价格、数量、资金或审计字段。JSON 序列化仅用于跨进程传递。

新增的 `paper_risk_scan_runs` 使用 `SELECT *` 全列读取：`id`、`cycle_id`、`asof_date`、`scan_minute`、`status`、`attempt`、`started_at`、`finished_at`、`error`、`detail`，不以 `paper_audit` 代替。各检查点累计包含该表 13 行、130 个字段值。空仓路径核对认领 running → completed → 同分钟重复入口不再执行服务且记录不变；失败重试核对 failed / attempt=1 / 原始错误 → running / attempt=2 / 清空结束时间和错误 → completed / attempt=2。所有中间状态同样纳入基线对照。

检查点包括：周期切换拒绝；完整风险卖出；部分风险卖出；风险审计恰好一次；恢复 300 股→同事件重复零新增成交→新事件再成交 700 股；完整交易回放；真实恢复入口 fencing 失败前后账本完全相同。

规范化 JSON（`json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")`）SHA256：`b1251b6aeb06585b21b59ce830202ac33a70867dac4c3eee21e07ef159261b4a`。此结果取代旧版 11 表 / 3,285 字段证据及其 SHA256。

注意：上述业务测试复用真实风险/执行函数；盘中编排另由顺序、非空结果透传、空结果复用、异常中断测试验证。它不能证明所有市场输入均等价。

复跑方式：在基线 `3c6d1a6` 与当前 PR 的两份 checkout 根目录，分别运行同一份修改后的 `scripts/pr298_ledger_probe.py <输出文件>`（用绝对脚本路径和同一 Python 环境），每次退出码都必须为 0。给临时验收工具目录安装 `freezegun==1.5.5`，通过 `PYTHONPATH` 引入，不修改应用依赖；导入项目模块前给每个进程设置独立的 `ASTOCK_DATA_DIR`。探针仅写临时测试账本。然后用以下命令比较输出（文件名按实际路径替换）：

```bash
python - <<'PY'
import hashlib
import json
from pathlib import Path

baseline = json.loads(Path("baseline.json").read_text(encoding="utf-8"))
current = json.loads(Path("current.json").read_text(encoding="utf-8"))
assert baseline == current
fields = sum(len(row) for cp in current.values() for rows in cp["ledger"].values() for row in rows)
print("checkpoints:", len(current), "ledger fields:", fields)
print(hashlib.sha256(json.dumps(current, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest())
PY
```

## 隔离编排性能对照

两边各 20 次；相同响应和延迟：健康检查 2ms、全市场响应 30ms、风险 10ms、恢复 5ms；全市场响应均为空（degraded / scan_ready=False），无数据缓存，数据库以隔离替身代替。运行实际 monitor_intraday，测量真实墙钟，不冻结性能时钟。

2026-10-30 本轮复跑结果如下；P50 为中位数，P95 按最近秩法取第 19 个有序样本。样本量较小，结论为暂定（provisional）。

| 指标 | 基线 P50 / P95 / max ms | 修改后 P50 / P95 / max ms |
|---|---:|---:|
| 风险启动延迟 | 68.758 / 75.949 / 76.592 | 3.282 / 4.028 / 5.390 |
| 风险服务替身耗时 | 10.216 / 10.600 / 10.841 | 10.439 / 10.605 / 10.645 |
| 本轮隔离扫描耗时 | 85.529 / 92.928 / 93.106 | 84.483 / 87.427 / 87.550 |

每次调用均为 health=2、market snapshot=2、risk=1、resume=1；两边所有返回结果一致。调用数指服务边界次数，不等于内部 HTTP 请求数。固定延迟下主要变化是风险启动更早，本轮总耗时的小幅波动不能证明稳定提速。锁等待没有被测量（数据库替身，无锁竞争）；调度跳过次数没有被测量（直接调用，没有调度器）。

复跑：两边分别运行相同 `scripts/pr298_timing_probe.py <输出文件>`。P50/P95/max 属于小样本隔离编排结果，不能外推到真实全市场扫描、真实 provider、锁等待或 skipped slots。真实 provider 请求、调度跳过和争锁性能仍未验证；本 PR 未部署，不以本数据宣称线上提速。

## 真实性能验收边界

本轮核对现有计时实现与隔离探针的测量范围，没有增加生产埋点或优化功能。当前工作区未配置生产连接主机，PR-1 也未部署，因此没有真实环境修改版样本；历史生产发现不与本次离线结果拼成 before/after。

| 指标 | 本轮可证明的内容 | 仍需独立验收的内容 |
|---|---|---|
| provider 请求数 | 服务边界调用数不变；未调用真实 provider | 同数据源、降级状态、交易日和缓存条件下的实际 attempts / errors |
| SQLite 锁等待 | 固定输入真实账本等价；计时探针的数据库为替身 | 生产 writer 争锁、事务时间；`db_acquire` 含连接等开销，不能直接当纯锁等待 |
| 调度跳过 | 直接调用编排，未经过 cron 或任务锁 | 自然 tick、runner started、skipped/in_progress、队列年龄和风险启动间隔 |
| 全市场实际耗时 | 仅固定 30ms 的空响应 | 真实市场与候选处理的端到端 P50 / P95 / max，冷/热缓存分开 |

现有 `host_paper_lock_wait_seconds` 只度量主机 `paper-scan.lock`，不包含外层 cron 锁或 Docker 启动，也不是 SQLite 锁等待。阶段计时包含子阶段，不能直接求和作为整轮耗时。未测指标保持“未验证”，不填 0；后续在可比自然样本具备后，才补真实性能结论。PR-1 的范围仍为风险优先级与委托恢复顺序，不启动 PR-2。

## 验收脚本静态检查

CI quality 作业现在执行 `ruff check backend scripts/pr298_ledger_probe.py scripts/pr298_timing_probe.py`，使用现有 Ruff 0.16.6 与项目规则，不扩大到其它历史脚本。该命令以及 `python -m compileall -q backend scripts/pr298_ledger_probe.py scripts/pr298_timing_probe.py`、`git diff --check` 本地均通过。

2026-10-30 本地全量后端回归：Python 3.14.5，`python scripts/run_tests_parallel.py --shards 4`，7,448 项全部通过，403.2 秒，无失败、错误或模块加载错误；测试进程的 PATH 前置已安装的 Git Bash。新增的 2 个生命周期场景位于独立验收探针中，计入上文每侧 10 个场景，不重复计入后端用例总数。

## 异常语义

monitor_risk 或恢复入口抛出 RuntimeError / paper lease lost 时，monitor_intraday 原样抛出并停止后续处理。风险失败时不执行恢复；恢复入口失败时不执行全市场刷新。异常没有改成成功、空列表或重试完成。

恢复前置确实改变失败的发生位置：基线已刷新市场才失败，现在先失败而没有候选扫描。这符合本实现的 fail-closed 顺序，但会牺牲失败轮的候选可用性，需要人工审核。恢复函数内部已有的逐订单异常回滚、可重试标记仍由原函数处理；本 PR 未改变它们。

## 回滚

合并后如需回滚，整体 revert 本 PR 的提交（含实现和验收提交）。回滚前无需数据库迁移；本 PR 未修改 schema 或运行配置。当前未合并、未部署。
