# PR-7 候选公共事实层收敛（新闻代码并集 / 快照复用 / 指纹成本决策）

基线：`master` @ `a95cff9`（PR #303 合并后）。路线图：`模拟盘升级路线图.docx` §七 PR-7。
MERGE: NOT MERGED · DEPLOY: NOT DEPLOYED

## 1. 修改前的调用图

```
_bootstrap_signals_for_today
└── PCScan.prepare
    └── PCScan.collect_evidence
        ├── for account in accounts:          ← 每个策略各一次
        │     cooled = cooled_candidates(...)
        │     news  = ports.news(names)       ← N 次 provider 调用（代码高度重叠）
        └── quotes(代码并集)                  ← 已经是并集

execute_open
└── execution.waitlist_news
      prefetch_news = _news_for(prefetch_names)   ← 同一轮里的第 N+1 次
```

两个账户的候选代码通常高度重叠（同一份 universe + 同一份因子表），逐账户请求等于把
同一份新闻/公告重复抓 N 次；等待池随后又为另一批代码再抓一次。

## 2. 修改后的结构

```
collect_evidence
├── 逐账户: cooldowns → names
├── 代码并集 → _collect_news_snapshot(ports, union_names)     ← 一轮一次
├── 逐账户: snapshot.rows_for(names)                          ← 按代码切片，深拷贝
└── quotes(代码并集) + sector_flow（不变）

prepare → PreparedCandidates(news_snapshot=…)
_bootstrap_signals_for_today → result["news_snapshot"]
monitor_intraday → execute_open(..., news_snapshot=…)
execute_open → _waitlist_news(names, snapshot)
```

## 3. 事实 owner 契约（§9.1 要求逐项）

| 项 | 值 |
| --- | --- |
| owner | `paper_candidate_scan.NewsSnapshot`（不是各策略，也不是 `paper_trading`） |
| key | 本轮全部账户冷却后候选代码的**并集** + 各自的名字映射 |
| 生命周期 | 一次 `prepare` 到同轮 `execute_open` 结束；不跨 slot、不跨进程 |
| source timestamp | `observed_at` = **抓取时刻**；每条记录的 `time` 仍逐行保留，时效判断继续按记录时间做 |
| TTL | `NEWS_SNAPSHOT_MAX_AGE_SECONDS = 180 s`（与最小调度周期同量级） |
| 失效 | 抓取失败（`ok=False`）、未覆盖请求的代码、或超过 TTL → 调用方必须重新抓取 |
| 失败语义 | 失败 **不是**"没有负面新闻"：`ok=False` 的快照永不被复用，等待池会重新请求 |
| 隔离 | 每次切片 `deepcopy`；一个策略改写自己的行不会影响其他策略或快照本体 |
| 数据源契约 | `factors.news_keyword_scan` 的快讯与公告两条路径都**必然**写 `code`，公告还先按本次请求的 `universe_names` 过滤（无 code 的公告项直接跳过）→ 生产源不会产出无代码记录 |

## 4. 指纹（`CandidateScanInputs.fundamentals`）成本决策

现状：key = `sha256(json.dumps((universe, finance, asof, snapshot_asof), sort_keys=True))`。

实测（本机，5,000 只 universe + 5,000 条 finance，payload ≈ 900 KiB）：

| 操作 | 耗时 |
| --- | --- |
| `json.dumps(... sort_keys=True)` 生成指纹 | **10.4–12.9 ms/次** |
| 两个等值输入的深比较 `==` | 0.6 ms |

曾尝试"与上一次输入相等就复用 key"，被**现有回归测试证伪**：`_candidate_rows` 会**原地修改**
传进来的 `universe`/`finance`（live overlay 阶段），持有引用再比较会把"已变的内容"误判成
"没变"，直接返回过期因子表。要安全短路必须深拷贝整份快照（≈ 序列化成本），收益归零。

**结论：保留逐调用内容哈希**，不做跨策略短路。代价是每轮每策略约 10 ms CPU（3 个内置策略
≈ 31 ms，占该 slot 候选阶段 <1%）。`test_candidate_fact_layer.FundamentalsFingerprintTests`
把"原地修改必须失效"和"内容相同只算一次"都钉住，避免以后有人用不安全的短路"优化"回来。

## 5. 行为等价

- 策略候选顺序、评分、PIT、时效、阻断理由：由 `_candidate_rows` / 因子层/冷却/审批链决定，
  本 PR 未改其输入（universe、finance、disclosures、fundamentals 的取用顺序与内容不变）。
- 风险门槛、成交判定、资金、cycle/lease/fencing：未触碰。
- 新闻**内容**对每个账户仍只包含它自己的代码；差异只在于"同一份 provider 结果被请求一次"。

## 6. 测试

`backend/test_candidate_fact_layer.py`（18 项）：

| 组 | 覆盖 |
| --- | --- |
| 并集 | 两账户重叠代码 → 仅 1 次请求；并集内容正确；各账户只得到自己的代码 |
| 隔离 | 改写 A 的新闻行不影响 B，也不污染快照本体 |
| 失败 | provider 抛错 → `ok=False` / `source="failed"`，绝不被复用；各账户 news 为空 |
| 空集 | 无代码不发请求，`source="empty"`、`ok=True` |
| 防御分支 | 若将来出现无代码记录，它会分发给所有消费者（按代码取值的门禁自行过滤），不静默丢失；当前生产源不会触发 |
| 复用条件 | 成功 + 覆盖 + 未超时；未覆盖/失败/超时三种都拒绝复用 |
| 等待池 | 可复用时不请求；不可复用时照旧 `_news_for`；无快照时行为不变；空等待池不请求 |
| 指纹 | 内容相同只算一次且返回隔离副本；原地修改必失效；不同 asof 不共享 |

契约更新（有意为之）：`test_candidate_shared_inputs` 的新闻调用次数从 `["news","news","quotes"]`
改为 `["news","quotes"]`；`test_paper_timing` 的 `execute_open` 断言增加 `news_snapshot=None`。

## 7. 验证

见 PR 描述与本文件的最终一轮结果（L0–L4 + 全量耗时）。

## 7.1 本轮结果（HEAD 见 PR）

- L0：`ruff check backend` 全绿；`compileall` 通过；`git diff --check` 干净。
- L1：`test_candidate_fact_layer` **18 项**；`test_candidate_shared_inputs` / `test_paper_candidate_scan` /
  `test_candidate_snapshot_replay` / `test_paper_scan_baseline` 全绿。
- L2：`test_paper_timing`、`test_open_execution_risk_reuse`、`test_execution_evidence`、
  `test_execution_verification_probe`、`test_scanner_history_reuse`、`test_scanner_scope_mask`、
  `test_paper_candidate_live_fields`、`test_candidate_factor_wiring`、`test_candidate_column_reads`、
  `test_manual_order_*` 全绿。
- L3：`test_paper_trading_architecture_guard`、`test_architecture_boundary_audit`、
  `test_schema_ownership` 全绿；耦合预算未放宽（`max_file_lines` 14574 → **14604**，其余不变）。
- L4：`python scripts/run_tests_parallel.py --shards 8 --timeout 900` →
  **7563 项全绿**，墙钟 277.6 s，最慢分片 239.9 s。

## 7.2 调用次数对照（结构性，不是墙钟）

| 调用点 | master | PR-7 |
| --- | --- | --- |
| `collect_evidence` 的 `ports.news` | 每账户 1 次（3 内置策略 = 3 次/轮） | **1 次/轮**（代码并集） |
| `execute_open` 的 `_news_for` | 1 次/轮 | **0 次/轮**（覆盖 + 新鲜时复用），否则仍 1 次 |
| `fundamentals` 内容指纹 | 每策略 1 次（≈10 ms/900 KiB） | 不变（见 §4 决策） |

## 7.3 已知限制

- 等待池复用受 `NEWS_SNAPSHOT_MAX_AGE_SECONDS = 180 s` 约束；超时即重新抓取，不做"近似新"。
- 新闻 provider 若在并集请求中部分失败（返回不全），本层无法区分"真的没有"与"没返回"，
  与改造前一致（同一次调用内同样是整份结果）；单请求异常则整份标记失败并禁止复用。
- 指纹仍逐调用序列化；跨策略短路的收益被"输入可原地修改"的安全性抵消（§4）。
