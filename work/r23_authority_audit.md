# R23 Authority Audit — Strategy / Selection Provenance

- 仓库：`daviesjoin-afk/astock-paper-trading`
- Base：`c872ae18574ecba5e1b788112fcd3c818af475d9`（= `origin/master`）
- 审计方式：**逐调用链读源码 + 逐表 `PRAGMA`/DDL 核对**，不按模块名推断。
- 结论一句话：**immutable strategy version 的权威只存在于 `paper_strategy_versions`；R23 之前，
  两条 selection 持久化 family 完全没有策略版本 provenance，`paper_signals` 没有 cycle 归属，
  且 `paper_selection.latest()` 在历史读取时会重新解析 current Registry。**

---

## 一、真实链路（追调用链，不按模块名假设）

```
strategy_definitions            ← identity / lifecycle（current state）
        ↓
paper_strategy_versions         ← immutable version authority（strategy_id, version, checksum, definition_json）
paper_strategy_version_heads    ← current head（"现在"）
paper_cycle_strategy_versions   ← cycle → immutable version（cycle pin authority）
        ↓
STRATEGY_REGISTRY.get_version / cycle_version_for_account / stamp_for_account   ← 唯一版本解析入口
        ↓
┌───────────────────────────────────────────────────────────────────────────────┐
│ 生产执行链（paper_trading.sqlite3，会写入订单/成交/现金）                      │
│                                                                               │
│  _candidate_rows(account, day, ...)      ← 候选生成（读 current spec + 因子）  │
│        ↓                                                                      │
│  generate_signals(day) / _bootstrap_signals_for_today(day)   ← ranking+approval │
│        ↓  _strategy_stamp(conn, account_id[, signal_id])                      │
│  paper_signals                            ← signal（有 S/V/C，**无 cycle**）   │
│        ↓  _buy_order(...) → _strategy_stamp(conn, account_id, signal_id)      │
│  paper_orders                             ← order（有 S/V/C + cycle_id）       │
│        ↓  execution_planner.commit_fill                                        │
│  paper_fills / paper_position_lots / cash / audit / risk_decisions             │
└───────────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────────────────┐
│ 研究选股链（selection_tracking.db，**不写账本**）                              │
│                                                                               │
│  A. paper_selection.run_daily → main._select_uncached（模型族）                │
│       → paper_selection_runs / paper_selection_picks                          │
│  B. selection_runner.run_daily → strategies.STRATEGIES（模型族）               │
│       → selection_runs / selection_picks / selection_observations             │
│                                                                               │
│  消费者：selection_tracking.dashboard、selection_alpha_report、factor_quality_shadow│
│  纯契约（无 I/O）：selection_labels、selection_tradability                      │
└───────────────────────────────────────────────────────────────────────────────┘

独立（既非 A 也非 B、也不产生 paper_signals）：
  strategy_plugins.persist_snapshot → strategy_trace 的 content-addressed replay blob
  （只被 strategies.py / synthetic_demo_strategy.py 使用；生产 candidate 路径不生成 trace id）
```

### 明确否定的「看起来完整」lineage

- **`paper_selection_*` 不产生 `paper_signals`**：`paper_selection.py` 的 DB 是
  `selection_tracking.db`（`DB_PATH = CACHE_DIR/selection_tracking.db`），模块 docstring 明确
  「只做研究选股，不生成订单、不改动模拟盘账本」。**不存在 selection → signal 的真实 lineage**，
  因此 R23 不新增 `selection_id → order` 的外键（规格 §5 C10 的禁止项）。
- **`selection_tracking.selection_runs.strategy` 是「模型族 id」（`three_day`/`five_day`/`ten_day`/
  `reported_profit_breakout`/`main_force_top10`，来自 `strategies.STRATEGIES`），不是注册表
  `strategy_id`**：`three_day`/`five_day`/`ten_day` 在 `strategy_definitions` 中**不存在**
  （实测：注册表只有 5 套内置 + 用户策略）。因此 family B 的「策略版本轴」是
  **not_applicable**，不得靠名字巧合把它映射成某个 strategy_id。
- **`paper_selection.STRATEGY_MODEL` 是「注册表策略 → 模型族」的映射**，方向是策略→模型族；
  反向映射不成立（一个模型族可被多个策略/无策略使用），不得用它反推 strategy_id。

---

## 二、逐层审计

| # | 层 | 当前 authority | 当前已存 provenance 字段 | 缺失 | 读 current head？ | latest/current fallback？ | 历史读取重解析 Registry？ | 生产上游？ | research-only？ |
|---|---|---|---|---|---|---|---|---|---|
| 1 | strategy identity/lifecycle | `strategy_definitions` | id/name/origin/lifecycle_status/current_version/current_checksum | — | current head **只代表现在** | — | 否（仅展示） | 是（permission） | 否 |
| 2 | immutable version | `paper_strategy_versions` | strategy_id/version/checksum/definition_json | — | 否 | 否 | 否 | 是 | 否 |
| 3 | cycle → version | `paper_cycle_strategy_versions` + `SR.cycle_stamp_for_account`/`cycle_version_for_account`（strict，无回退） | cycle_id/account_id/strategy_id/version/checksum/bound_at | — | 否 | **否（已是 strict）** | 否 | 是 | 否 |
| 4 | live 版本解析 | `SR.stamp_for_account` | 3 元组 | — | **会**（cycle pin 缺失 → legacy binding → 用户策略 current head） | **有**（legacy + current head） | 否 | 是 | 否 |
| 5 | candidate generation | `paper_trading._candidate_rows` | 无 strategy stamp（只读 current spec 做模型选择） | candidate 层无 immutable stamp | **是**（`_spec_for` 读 current spec） | 无 | 否 | 是 | 否 |
| 6 | signal 写入（收盘） | `paper_trading.generate_signals` | `paper_signals.strategy_id/strategy_version/strategy_checksum`（DB 触发器保证完整且不可改） | **cycle_id 缺失**；`asof_day` 只在 `signal_date`；每候选重复解析 | **会**（`_strategy_stamp(conn, account_id)` → `stamp_for_account`） | **有**（legacy binding / 用户 current head） | 否 | 是 | 否 |
| 7 | signal 写入（盘中） | `paper_trading._bootstrap_signals_for_today` | 同上 | 同上 | **会** | **有** | 否 | 是 | 否 |
| 8 | signal → order | `paper_trading._buy_order` / `_record_entry_frozen_waitlist` | 继承 signal stamp（`_strategy_stamp(..., signal_id)`）+ `cycle_id` | — | 仅当 signal stamp 不完整时 | 有（但被 signal 触发器堵住） | 否 | 是 | 否 |
| 9 | 直接 order（无 signal） | `_intraday_sell` | `_strategy_stamp(conn, account_id)` + `cycle_id` | — | **会**（同上） | **有** | 否 | 是 | 否 |
| 10 | order → fill/eval | `execution_planner.commit_fill` | 从 durable order row 继承 | — | 否（R20 已收敛） | 否 | 否 | 是 | 否 |
| 11 | **family A** `paper_selection_runs/picks` | `paper_selection.ensure_schema`（自有 owner，`selection_tracking.db`） | **只有 strategy_id / strategy_name / model_id / factor_date** | **version / checksum / asof_day / scope / cycle_id / provenance_status / 稳定 run identity** | **是（读取时）**：`latest()` 用 `catalog()` 的 current `SR.active_ids()`+`SR.labels()` 重建分组 | **有**：paused/archived 策略的历史结果会从 `latest()` 中消失，名称用**当前**注册表名 | **是** | 否（research-only） | **是** |
| 12 | **family B** `selection_runs/picks/observations` | `selection_tracking.ensure_schema`（自有 owner） | run_date / strategy(模型族) / data_asof_date / benchmark_entry_price / result_json；picks 有 `run_id` FK | **strategy_id / version / checksum / asof_day / scope / cycle_id / provenance_status** | 否 | **有（更严重）**：`record_run` 先 UPSERT 再判「历史 run_date 不可变」，历史行的 run 级内容会被**今天的**结果覆盖后返回 skipped | 否 | 否（research-only） | **是** |
| 13 | archive（信号/订单） | `paper_trading.cleanup_stale_data` | `paper_signals_archive` 与 live 表列序一致（`INSERT ... SELECT *`） | 归档同样缺 cycle_id | 否 | 否 | 否 | 是 | 否 |
| 14 | 前端/API 读取 | `main.py:1611`（`PS.latest`）、`main.py:1659`（`ST.dashboard`） | 只消费 compatibility projection | — | 通过 family A 间接 | 同上 | family A 会 | 否 | 是 |
| 15 | replay trace | `strategy_trace` + `strategy_plugins` | content-addressed snapshot（data_date/code_version） | — | 否 | 否 | 否 | **否**（生产 candidate 路径不生成 trace id） | 是 |

---

## 三、R23 判定（哪些是真缺口、哪些只是「看起来缺」）

### 真缺口（本 PR 修）

1. **G1 — family A 无版本 provenance + 历史读取重解析 Registry**
   `paper_selection_runs` 没有 version/checksum；`latest()` 每次都用**当前** `SR.active_ids()`/
   `SR.labels()` 重建输出。后果：策略改名 → 历史结果的名称变了；策略 paused/archived →
   历史 selection 直接从 API 视图里消失。这正是规格 §2 的
   `historical fact != current strategy registry state`。
2. **G2 — family A 同日覆盖销毁不同版本证据（C2）**
   `paper_selection_runs` 的 `UNIQUE(trade_date, strategy_id)` + `DELETE`→`INSERT`
   覆盖语义，使「同日同策略、不同 immutable version」的两份证据无法共存；picks 又以
   `(trade_date, strategy_id, rank_no)` 为唯一键，没有稳定 run identity（规格 §8）。
3. **G3 — family B 历史 as-of 被未来结果污染（C7）**
   `selection_tracking.record_run` 先做 `ON CONFLICT(run_date, strategy) DO UPDATE`
   （含 `result_json`/计数/`generated_at`），**之后**才判 `run_date < today` 并返回
   `skipped: historical run_date is immutable`。注释声称不可变，代码已经改完了。
4. **G4 — family B 无策略身份/版本轴**
   `strategy` 是模型族，注册表里没有 `three_day`/`five_day`/`ten_day`。诚实答案是
   **not_applicable**（不得编造 strategy_id），但 run 身份 / as-of / 状态语义必须与 family A 一致。
5. **G5 — `paper_signals` 没有 cycle 归属**
   规格 §2 第 4 问（用哪个 cycle）对 signal 不可回答：只能借 `paper_orders.cycle_id`，
   而被 blocked/pending/从未成交的信号根本没有 order。这是「signal 属于哪个 cycle」的
   权威缺失，也是 signal/order cycle 一致性无法验证的原因（C10）。
6. **G6 — signal 写入器在 cycle pin 缺失时会 fallback 到 legacy binding / 用户 current head**
   `_strategy_stamp` → `SR.stamp_for_account`：cycle pin → legacy binding → `origin='user'` 的
   **current head**。对「cycle 已明确」的路径，这就是规格 C4 禁止的 fallback。
7. **G7 — 同一 run 的 N 个 pick 重复解析 N 次版本**（规格 §24）
   `_strategy_stamp` 位于候选循环内部（`generate_signals` / `_bootstrap_signals_for_today`），
   每个候选各查一次注册表；provenance 应当 **run/account 解析一次、picks 共享**。

### 不是缺口（写 regression 锁住，不大改）

8. **paper_signals 的 stamp 完整性/不可变性**：DB 触发器 `trg_paper_signals_strategy_stamp_insert`
   拒绝 partial/伪造戳，`trg_paper_signals_strategy_stamp_immutable` 拒绝事后改写 →
   C5（checksum 错误）在账本层**已 fail closed**（不是靠 Python 静默纠正）。
9. **signal → order 继承**：`_buy_order` / `_record_entry_frozen_waitlist` 传 `signal_id`，
   在 signal stamp 完整时直接继承，不再查 current head → C10 的「order writer 重新 current-stamp」
   在**有 signal 的路径上不存在**（锁住即可）。
10. **cycle pin 严格性**：`SR.cycle_stamp_for_account` / `cycle_version_for_account` 已是
    strict（无 legacy/current 回退），R23 必须复用它、不得另写 SQL。
11. **archive 列序**：`paper_signals_archive` / `paper_orders_archive` 用 `INSERT ... SELECT *`，
   已有 `test_order_cycle_provenance.MIG_CYCLE_5` 的列序防线；R23 给 signals 加列必须沿用同一标准。
12. **research-only 模块**：`selection_labels` / `selection_tradability` 是纯契约（无 DB、无策略身份），
   `strategy_trace`/`strategy_plugins` 不参与生产 candidate 路径 → 都不需要 provenance 改动。

---

## 四、R23 的设计决定（对应规格条款）

| 规格条款 | 落地 |
|---|---|
| §6 纯 contract | 新增 `backend/strategy_selection_provenance.py`：frozen dataclass + 校验 + 规范身份 + 状态语义；**只依赖 stdlib**，不开 DB / 不读环境 / 不读时钟 |
| §7 复用 cycle resolver | 新增 `backend/strategy_selection_resolver.py`（薄 adapter），cycle 路径只用 `SR.cycle_version_for_account`（= `cycle_stamp_for_account` + 校验 checksum 的 `get_version`）；research 路径只在 run 创建时 pin 一次 current immutable head |
| §8 run-level provenance | family A：新增 `provenance_key` 身份列 + `UNIQUE(trade_date, strategy_id, provenance_key)`，picks 增加 `run_id` FK（`paper_selection_runs.id`）；family B：`UNIQUE(run_date, strategy, provenance_key)`，picks 保持 `run_id` FK。**不建第三套 shadow 表** |
| §8 同日重试 | 同一 `provenance_key` → 覆盖（幂等 retry）；不同 version/checksum → 不同 run，**不 DELETE 老证据** |
| §9 legacy | 迁移只加 NULL 列；历史行标 `legacy_unproven`；**绝不**用今天的 Registry 回填 |
| §10 signal/order | `paper_signals` 增 `cycle_id`（live + archive 同序追加）；signal 写入改为**显式 cycle + strict pin**，pin 缺失 → 该账户 fail closed（不写错戳）；order 路径保持继承 signal stamp |
| §12 as-of | 复用 `strategy_trace.data_date` 的语义（explicit → 唯一 → 混合拒绝 → 无法证明拒绝）；多个输入日期冲突 → fail closed，不 min/max |
| §13 evidence | 生产 candidate 路径没有 trace id → **不**新建 replay blob 系统；只存 `(strategy_id, version, checksum, asof, scope, cycle)` |
| §14 schema ownership | 主账本（`paper_signals`/archive）走 `paper_schema_migrations.py` + `db_migrate.py` v23；`selection_tracking.db` 的两套表继续由各自 `ensure_schema` owner 管理，**不合并两个 SQLite** |
| §15 compatibility | `strategy_name` 等仅作展示投影；历史权威只认 `strategy_id + version + checksum`；`latest()` 不再用 current Registry 决定历史可见性 |
| §16 维护性 | 不复制版本解析；stamp 一路传递；不做大 Manager；contract 零 I/O；依赖方向 `paper_selection/selection_tracking/paper_trading → strategy_selection_provenance / resolver → strategy_registry`，反向禁止 |
| §17 架构门禁 | 扩展 `test_paper_trading_architecture_guard.py`（Guard 14，AST 静态锁）+ 新建 `test_strategy_selection_provenance.py`（行为 + 契约） |
| §18 regression | SP-01..SP-18 全部落在 `test_strategy_selection_provenance.py` |
| §19 mutation | `work/r23_mutation_check.py`，M-SP1..M-SP15，每条指名唯一永久回归 |

## 五、明确不做的事（避免越界）

- 不改 lifecycle / DSL / risk profile / allocation / execution / API 契约。
- 不新增 order writer / fill writer / cash writer / risk path（R19–R22 不变式）。
- 不给 `paper_selection_*` 与 `selection_runs` 造 selection→signal 假 lineage。
- 不把两个 SQLite（`paper_trading.sqlite3` / `selection_tracking.db`）合并。
- 不为 provenance 在每个 pick 上复制 immutable definition JSON（只存 3 元组 + 引用）。
- 不实现 R24（Market Data Boundary）/ R25（Pure Decision + Deterministic Replay）。
