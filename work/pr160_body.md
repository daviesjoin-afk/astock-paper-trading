## Scope

本 PR 关闭 PR #157 合并时 **defer 的三个 P2**，并建立 **Shadow Tradability Validation**
（生产判断 vs Historical Tradability Archive 判断的只读比对）。

```
Historical Tradability Archive
        ↓
Evidence Ingestion
        ↓
Coverage / provenance / PIT
        ↓
Shadow Tradability Validation      ← 本 PR
        ↓
未来才可能讨论 execution authority
```

**本 PR 不做**：不改订单路径、不改 can_buy/can_sell、不改策略评分 / 因子 / 收益计算 /
持仓 / 成交 / portfolio risk / AI / evolution / learning labels，不自动调参，不部署。

---

## Base / Head

| 项 | 值 |
| --- | --- |
| base | `master` @ `1dc4b053f125fe0d38a42c9244737ae9433e9a19` |
| head | `codex/tradability-shadow-validation` @ `c65f40d3583821131e0e130d4900fc656e9d9cc7` |

开始前已 `git fetch` 确认 master 未漂移（`origin/master` == 上述 base SHA，即 #157 的
merge commit）。

---

## Phase A — 三个 deferred P2 如何关闭

先复现，再修。复现脚本 `work/pr160_repro_p2.py` 在 **base commit 的代码**上跑，输出：

```
[P2-1] divergent replay outcome      : ACCEPTED fingerprint=False
[P2-1] archive rows before/after     : 1 -> 2
[P2-1] audit fingerprint before/after: 3c667b65… -> 3c667b65…      ← audit 仍描述旧 run
[P2-2] codes=","                -> resolved 5000 codes  <-- FALLBACK TO WHOLE MARKET
[P2-3] reversed range  -> resolved 0 sessions <-- EMPTY RANGE ACCEPTED
[P2-3] zero-session write run        -> status=completed archive=0 audit_rows=1
```

修复后同一脚本输出：

```
[P2-1] divergent replay outcome      : REJECTED IngestionError
[P2-1] archive rows before/after     : 1 -> 1
[P2-2] codes=","                -> REJECTED CodeScopeError
[P2-3] reversed range  -> REJECTED SessionScopeError
[P2-3] zero-session write run        -> REJECTED IngestionError archive=0 audit_rows=0
```

### P2-1 同 run_id 的 divergent replay 必须 fail closed

`run_id` 是**审计身份**，`run_fingerprint` 是**内容身份**，两者不是一回事。

```
same run_id + same run_fingerprint  => 幂等重放，允许
same run_id + different fingerprint => IngestionError
                                       → 整个事务 rollback
                                       → archive 不变、audit 不变
```

`IngestionService.ingest` 重构为**两阶段**：先算完 coverage / status / run_fingerprint，
**然后**校验 replay identity，**最后**才写 archive 与 audit。

```python
if write:
    self._assert_replay_identity(run_id, run_fingerprint)   # ← 校验
    for evidence in normalized_evidence:                     # ← 才落事实
        if self._repo.save(evidence): persisted.append(evidence)
    self._persist_run(...)                                   # ← 才落审计
```

关键点：**所有 archive 写入都排在校验之后**，所以校验不过时 archive 行数与 audit 行
都不会变——这不依赖调用方是否记得 rollback（`run_backfill` 的 rollback 仍然保留，作为
异常路径的第二道保险）。既有 run 没有记录 `run_fingerprint` 时也 fail closed：无法证明
是同一次重放就不接受。

特别审计（默认 provider 每次生成新的 `retrieved_at`）：`test_new_retrieved_at_cannot_impersonate_an_old_run`
证明新观察时点不能冒充旧 run_id 的重放。

回归测试 R1–R7 + 3 条补充，全部断言"失败路径 archive/audit 逐行不变"（不只是行数）：

| 用例 | 断言 |
| --- | --- |
| R1 | same run_id + same fingerprint → 成功、无重复事实、audit 行不变 |
| R2 | changed code scope → 拒绝，DB 快照逐项不变 |
| R3 | changed sessions → 拒绝，DB 快照逐项不变 |
| R4 | changed evidence → 拒绝，DB 快照逐项不变 |
| R5 | changed provider_version → 拒绝，DB 快照逐项不变 |
| R6 | divergent replay → archive 行数不变 |
| R7 | divergent replay → audit 行不变 |
| 补充 | 校验发生在持久化之前（事实内容逐行不变）；不同 run_id + 相同内容不是冲突 |

### P2-2 显式空 `--codes` 禁止 fallback 全市场

严格区分 `codes is None` 与"显式给了但解析为空 / 含非法代码"：

```python
if codes is None:              # 未传 → 允许默认 universe
    selected = sorted(load_listing_records().keys())
else:
    selected = normalize_codes(codes)   # 显式 → 解析失败即 CodeScopeError
```

- 空输入 / 只有分隔符 → `CodeScopeError`；
- **任一个** token 非法 → `CodeScopeError`（静默丢弃会让操作员以为整批都处理了）；
- code 归一化**复用**既有实现：`disclosure_timeline.normalize_code`（前缀 / 补零）+
  `marketdata_feeds` 的"必须 6 位数字"判据，不另造第三套 code 规则。

`--write` 下这曾经把 `--codes ,` 放大成全市场写入。

| 用例 | 断言 |
| --- | --- |
| CODE1 | 未传 `--codes` → 默认 universe（`--limit` 仍生效） |
| CODE2 | `--codes ","` → 拒绝 |
| CODE3 | 纯空白 / `", , ,"` → 拒绝 |
| CODE4 | 全非法（`abc` / `12345` / `1234567` / `600000abc` / `00000X`）→ 拒绝；合法+非法混合 → 拒绝 |
| CODE5 | 显式合法 subset → 恰好该 subset（含 `SH/SZ` 前缀归一与去重保序） |
| CODE6 | 非法/空 codes + `--write` → archive 与 audit 都不变 |

### P2-3 空 trading-session range 必须失败

倒序 / 只有周末 / 只有休市日 / 非法日期 → `SessionScopeError`。交易日判定继续复用
`selection_labels.sessions_between` → `universe.is_trade_day`，**没有**第二套交易日历。
显式 `--session` 保留单 session 语义，但仍要求"合法日期且是交易日"。

编排层同样拒绝空 scope（`IngestionError`），所以即使绕过 scope 解析层直接调用
`run_backfill`，零 session 也不会落一行把自己记成 `completed` 的审计。

| 用例 | 断言 |
| --- | --- |
| SESSION1 | `from > to` → 拒绝 |
| SESSION2 | 只有周末 → 拒绝 |
| SESSION3 | 只有法定休市日 → 拒绝 |
| SESSION4 | 非法日期（`2026-13-99` / `not-a-date` / `20260914` / 空 / `2026-09`）→ 拒绝；非法 `--session`、休市 `--session` 同样拒绝 |
| SESSION5 | 正常范围 → 只返回权威交易日；单 session → 单元素 |
| SESSION6 | 零 session + `--write` → archive 与 audit 都不变；绕过解析层直接调编排层同样被拒 |

### Transaction atomicity

三项共享同一条原则：**validate operator scope、validate replay identity，都发生在任何
不可逆持久化之前**。

- operator scope 在**打开数据库之前**解析（CLI），非法输入连 `sqlite3.connect` 都不会发生；
- replay identity 在**任何 archive 写入之前**校验；
- 错误路径：archive 不变、audit 不变、不 commit。

回归专门验证"失败的 run 让 DB 逻辑状态逐项不变"，快照包含：
表清单、索引清单、`PRAGMA user_version`、archive 行数、audit 行数、**archive 逐行内容**、
**audit 逐行内容**。

---

## Phase A Mutation Matrix

在既有 ingestion 矩阵上新增 8 条（M-R1..R4 / M-CODE1..2 / M-SESSION1..2），并把因本 PR
改动而失效的两个 anchor（M-F1 / M-S1）在同一 commit 内重新指向新源码形态。

| 变异 | 结果 |
| --- | --- |
| M-R1 允许 same run_id + divergent fingerprint | CAUGHT |
| M-R2 divergent replay 写 archive 但不更新 audit | CAUGHT |
| M-R3 先写 archive/audit 再检查 run_id conflict | CAUGHT |
| M-R4 既有 run 无 fingerprint 时放行 | CAUGHT |
| M-CODE1 explicit empty codes fallback whole universe | CAUGHT |
| M-CODE2 all-invalid codes 被静默丢弃 | CAUGHT |
| M-SESSION1 empty session range accepted | CAUGHT |
| M-SESSION2 zero-session write creates audit row | CAUGHT |

**矩阵总结果：47/47 CAUGHT，0 survived**（含既有 TTI1–TTI12 / M-D1 / M-F1..F3 / M-S1 /
M-DR1 / M-C1..C5）。`S0` 哨兵（只改注释）按预期 UNDETECTED。
每条变异都做 byte-for-byte 还原并核对 sha256：40 次 restore 全部 `bytes_match=True
sha256_match=True`，0 次失败。无 equivalent mutation。

---

## Phase B — Shadow Tradability Validation

### 架构与权威边界

```
production decision  ──┐
                       ├─→  ShadowComparator  ─→  ShadowComparison  ─→  summary / audit
archive decision     ──┘
```

- **生产侧**必须复用生产路径真正在用的实现：`selection_tradability.entry_tradability` /
  `exit_tradability`（它们内部走 `tradability_at`）。Shadow **不复制**任何生产规则。
- **归档侧**必须复用 `tradability_archive.tradability_at`。Shadow 不读
  `historical_tradability_archive` 的原始 SQL，也**不知道** ST / 停牌 / 涨跌停方向 /
  quote / volume 该怎么判——那些规则已经有 authority。
- 新增独立模块 `backend/tradability_shadow.py`；对象命名为 `ShadowComparison` /
  `ShadowComparisonSummary`，**刻意不叫** `TradabilityDecision` / `OrderDecision` /
  `ExecutionDecision`（它是观察，不是 authority）。
- Shadow 不 import 任何下单 / 持仓 / 成交 / 学习模块，也不暴露
  `allow_order` / `block_order` / `override` / `effective_can_buy` / `effective_can_sell`。

### Comparison identity

```
(code, session, decision_at, side, comparison_contract_version)
```

同一身份 deterministic、idempotent：相同内容 → no-op（返回 `"identical"`）；**冲突内容 →
`ShadowConflictError`**（fail closed，禁止 last-write-wins，原记录不被覆盖）。

### Comparison taxonomy

```
agree_allow
agree_block
production_allow_archive_block
production_block_archive_allow
archive_unknown
archive_missing
archive_unprovable
production_unknown
comparison_invalid
```

`comparable` = 前四项；`not_comparable` = 后五项。同时保留 `production_reason` /
`archive_reason`，供后续按原因归类（ST / 停牌 / quote / volume / 涨跌停锁定 / 上市状态）。

### Comparable / not-comparable 计数契约

归档当前的证据缺口**不被算成 disagreement**：

- `archive_unknown`：有可见证据，但该方向依赖的事实当时未知（如 ST 未知）；
- `archive_unprovable`：事实存在，但在 `decision_at` 当时不可知（PIT 不成立）；
- `archive_missing`：从未摄取过这条 `(code, session)`；
- `production_unknown`：生产侧自己判 `unproven`；
- `comparison_invalid`：身份非法或生产判 `invalid`。

只有 `production verdict 有效 AND archive verdict 历史可证明` 才进入 agree/disagree 分母。

Summary 输出 `requested` / `comparable` / `not_comparable` / `agree` / `disagree` /
四类 agree-disagree 细分 / 五类不可比细分，以及 `by_side` / `by_production_reason` /
`by_archive_reason` / `by_session` / `by_status`。

指标：

```
comparison_rate   = comparable / requested
agreement_rate    = agree / comparable
disagreement_rate = disagree / comparable
```

`comparable == 0` 时三个比率里后两个是 **`None`（not_available）**，不是 0%、不是 100%。

### PIT 保证

- `future evidence cannot alter earlier comparison`：之后才观察到的修订版不得改写更早的比对；
- `future observed_at` / `future effective_at` 一律不可见；
- `later revision` 只在**新的 `decision_at`** 之后才可见；
- 比对身份包含 `decision_at`，同一天不同决策时点是不同身份。

### BUY / SELL 方向性

涨停锁定 → 买入被拦、卖出不被这个原因拦；跌停锁定 → 相反；锁定但方向未知 → 两侧都拦。
语义与 `selection_tradability` / `tradability_archive` 的既有 golden 完全一致，
Shadow 不复制市场规则。

### Reason-level golden matrix

15 个用例逐类验证（listed / not listed / delisted / ST / non-ST / unknown ST /
suspended / unknown suspension / quote / unknown quote / volume / unknown volume /
limit-up lock / limit-down lock / unknown lock），每类都断言：归档 verdict、比对分类、
comparable 与否、summary 计数。带非空洞性断言（覆盖的分类数 ≥ 3）。

### Shadow 不污染 production

`backend/test_tradability_shadow_architecture_guard.py` 用 AST 静态扫描钉死：

1. **公开 API 面等值断言**（不是黑名单）：模块级公开可调用名必须**恰好**等于登记集合，
   `ShadowComparator` / `ShadowComparison` 的公开方法同理。新增任何公开入口都必须显式
   登记——登记本身就是一次需要理由的架构决定。（最初写成黑名单，非空洞性验证显示
   "新增一个叫 `permit_trade` 的入口"能存活，因此改成等值断言。）
2. Shadow 不得 import 下单 / 持仓 / 成交 / 学习 / 策略模块，不得调用
   `submit_order` / `cancel_order` / `modify_order` / … ；
3. Shadow 不得直接读原始可交易性字段（`is_suspended` / `is_price_limit_locked` /
   `has_market_quote` / `has_trade_volume` / `price_limit_direction`），未知原因必须来自
   `TA.TradabilityReason` 而不是本地字符串常量；
4. 执行 / 学习链路不得 import `tradability_shadow`，也不得内嵌 `ShadowComparator`；
5. 操作员 CLI 不得出现 `--apply` / `--enforce` / `--switch-authority` / `--take-over`，
   且不得包含任何写库语句。

### On/Off golden sentinel（长期 regression）

`test_tradability_shadow.ShadowOnOffDoesNotChangeProduction`：同一输入下 shadow 关闭与
开启，`orders` / `fills` / `positions` / `cash` / `production_tradability` / `selection`
逐项相等；Shadow 唯一新增的是 comparison / audit 输出。

### Optional persistence

默认**不持久化**（本 PR 的消费方是只读 CLI / 报告，无消费方的表不提前建）。需要留存时
显式调用 `save_comparisons`，写入独立表 `tradability_shadow_comparisons`，**不复用**
`historical_tradability_archive` / `tradability_ingestion_runs` / `orders` / `fills` /
`positions` / selection labels / learning tables。唯一身份是
`(code, session, decision_at, side, contract_version)`。

---

## Phase B Mutation Matrix

| 变异 | 结果 |
| --- | --- |
| M-SH1 archive unknown 被算 disagreement | CAUGHT |
| M-SH2 archive unprovable 被算 comparable | CAUGHT |
| M-SH3 agreement_rate 用 requested 当分母 | CAUGHT |
| M-SH4 future observed evidence 参与历史 comparison | CAUGHT |
| M-SH5 BUY/SELL 涨跌停方向反转 | CAUGHT |
| M-SH6 shadow 暴露 authority 入口 | CAUGHT |
| M-SH7 shadow 汇总把全部结论算成一致 | CAUGHT |
| M-SH8 同 identity 允许 conflicting overwrite | CAUGHT |

M-SH4 / M-SH5 的 anchor 落在 `tradability_archive._visible_at` 与 `_buy_reason`：
它们是**归档侧**的 PIT 与方向性 authority，Shadow 只是消费它。改坏那里 Shadow 就会
比对出一个错误的归档结论，因此这两条变异必须由 Shadow 的 golden 抓住——这正是
"Shadow 不得自己重写市场规则"的证明：它没有自己的规则可改。

---

## Operator / CLI

只读工具 `work/tradability_shadow_validation.py`，支持
`--session` / `--from` / `--to` / `--codes` / `--limit` / `--side` / `--json`。

- **复用** #157 已建立的 code resolution 与 trading-session resolution（`tradability_backfill`
  的 `resolve_codes` / `resolve_sessions`），不复制；
- 因此同样继承：显式空 `--codes` → 错误、零交易日范围 → 错误、权威交易日历；
- 默认 read-only；**没有** `--apply` / `--enforce` / `--switch-authority`。

端到端冒烟 `work/pr160_cli_smoke.py`（真实 SQLite + 真实生产判定 + 真实 K 线缓存）：

```
requested: 10   comparable: 5   not_comparable: 5
agree: 2   disagree: 3
agree_allow: 1   agree_block: 1
production_allow_archive_block: 1   production_block_archive_allow: 2
archive_unknown: 1   archive_unprovable: 2   archive_missing: 2
production_unknown: 0   comparison_invalid: 0
comparison_rate: 0.5   agreement_rate: 0.4   disagreement_rate: 0.6
```

七类状态全部被真实走到；非法 scope 四种输入全部 `exit=2` 且**未打开数据库**；
CLI 运行前后数据库文件字节完全一致（只读证明）。

---

## Review round 1 — 3 P1 + 3 P2 全部修复

第一轮 review 提出 6 条发现，逐条**先复现再修**，全部已修复并 resolve：

| 级别 | 发现 | 修复 |
| --- | --- | --- |
| P1 | `audit_conn=None` 时 replay identity 无处可查 | `write=True` 要求持久审计存储；缺审计表同样 fail closed（dry-run 不受影响） |
| P1 | error→unknown 翻转不改变指纹 | 指纹覆盖 provider 结果分布（evidence/unknown/error/skipped/status）——凡进审计的差异都是内容身份 |
| P1 | Shadow CLI 为卖出伪造同日 `entry_session` | 不声明 `entry_session`（本工具比的是市场层面可交易性，T+1 属于持仓层面），消除假分歧 |
| P2 | 用"后来是否摄取过"决定归档分类 | 分类只用 `decision_at` 当时可见证据；missing/unprovable 由调用方显式声明，未来探测已删除 |
| P2 | 生产 verdict 的 side 与比对 side 不一致仍接受 | `side` 不匹配 → `comparison_invalid` |
| P2 | `work/` 缺失时整个类被 skip | skip 只作用于真正读 CLI 文件的用例，护栏自身的非空洞性检测在任何环境都跑 |

变异矩阵随之扩到 **44** 条（新增 M-R5/R6、M-SH9/10/11）。其中两条**修正而非保留**：
M-R6 原本 `IMPORT-FAILED`（无法 import 的注入不算 kill），M-SH9 原本是 inert 变异
（注入后行为不变）；改成真正注入缺陷后 44/44 CAUGHT、0 survived。

---

## Review round 2 — 1 P1 + 2 P2 全部修复

| 级别 | 发现 | 修复 |
| --- | --- | --- |
| P1 | unprovable / conflict 明细不进内容身份 | 指纹覆盖 unprovable pair 列表与 conflict 明细（field/providers/values）——凡进审计行的差异都是内容身份 |
| P2 | CLI 仍从当前归档推导 `ingested_later` | CLI 不再声明（`ingested_later=False`）；`archive_unprovable` 需要真正的摄取台账，列为 follow-up |
| P2 | 审计连接可以与归档连接不同 | 构造时即拒绝不一致的连接；生产本来就传同一个连接 |

顺带两个**由变异矩阵而非 review 发现**的质量修正：

- `M-R8`（conflicts 不进指纹）最初 **SURVIVED**——provider 集合变化会同时改 normalized
  evidence，间接用例因此"因为错误的原因通过"。新增
  `FingerprintCoversEveryAuditVisibleDifference`，直接驱动 `_run_fingerprint` 并断言每个
  审计可见输入都会改变指纹（含两个**不同冲突值集合**必须哈希不同）。
- 变异矩阵现在运行时持有锁文件，revert 脚本在锁存在时**拒绝启动**。曾经并发运行导致
  revert 脚本把变异体当成"原始内容"记下并"还原"回去，留下永久损坏的源码；现在这种
  情况是一次明确拒绝，而不是一次静默损坏。

---

## Known evidence gaps

归档当前仍**不能**证明（Shadow 把它们如实归入 not_comparable，而不是分歧）：

- 历史上市 / 退市权威源不足 —— `load_listing_records` 只能给"今天仍在册"的弱事实，
  `is_listed` 常为 unknown；
- 停牌历史源不完整（`SuspensionHistoryProvider` 依赖调用方注入的完整归档）；
- 没有 Level2 封单 / 排盘证据，涨跌停只能判"触及"，不能判"必然成交"；
- 部分 historical bars 的 `observed_at` 只能取 `retrieved_at`，因此判 unprovable；
- 真实 retry 后的实际成交 session 无法用日线重放（生产 TTL 是盘中 90/240 分钟）。

这些缺口在 summary 里以 `archive_unknown` / `archive_unprovable` / `archive_missing`
显式计数，**不进** agreement/disagreement 分母。

### Follow-up（本 PR 不做，已记录）

1. **摄取台账**：`archive_unprovable` 现在只能由调用方显式声明。要诚实地自动判定
   "这条 pair 有证据、只是晚于 decision_at 才被观察到"，需要一个记录该 pair **首次被
   观察到**时间的摄取台账——那是当前状态无法重建的事实。在它存在之前，CLI 一律报
   `archive_missing`（两者都是 not_comparable，不影响一致率分母）。
2. **市场级 vs 持仓级的 T+1**：Shadow CLI 比的是市场层面可交易性，因此不声明
   `entry_session`。若将来要覆盖持仓层面的 T+1 分歧，需要接入真实持仓入场时点，而不是
   在 CLI 里编一个。

---

## Tests

### Focused suites

```
backend/test_tradability_shadow.py                        51 tests
backend/test_tradability_shadow_architecture_guard.py      22 tests
backend/test_tradability_backfill.py      （+36：R1–R7 / CODE1–6 / SESSION1–6 等）
```

```
$ python -m unittest test_tradability_backfill test_tradability_ingestion \
      test_tradability_archive test_tradability_shadow \
      test_tradability_shadow_architecture_guard
Ran 399 tests in 1.951s

OK
```

### 非空洞性验证（revert-then-run）

`work/pr160_verify_new_tests.py` 把每处修复**就地回退**，跑对应模块，断言 rc != 0，
再按 sha256 还原：

```
[CAUGHT] R1 divergent replay rejected
[CAUGHT] R2 empty code scope rejected
[CAUGHT] R3 explicit empty codes rejected (fallback removed)
[CAUGHT] R4 zero-session range rejected
[CAUGHT] R5 archive writes happen after replay validation
[CAUGHT] S1 archive gaps not counted as disagreement
[CAUGHT] S2 agreement_rate uses requested as denominator
[CAUGHT] S3 future evidence may enter a past comparison
[CAUGHT] S4 BUY/SELL limit direction inverted
[CAUGHT] S5 conflicting comparison content overwrites (last-write-wins)
[CAUGHT] S6 shadow exposes an authority entry point
[CAUGHT] S7 comparison identity drops decision_at

OK: 12 reverts all caught; all files restored byte-for-byte
```

（S6 第一次是 SURVIVED，因此把公开 API 黑名单改成了等值断言。）

### Full backend

```
$ python -m unittest discover -s backend -p "test_*.py"
Ran 2969 tests in 192.397s

OK (skipped=5)
```

- base（master @ 1dc4b05）计数：**2847**
- 本 PR head 计数：**2969**（+122）
- 数字来自与 CI 相同的 runner（`unittest discover`），不是 pytest。

```
$ python -m ruff check backend
All checks passed!
$ python -m compileall -q backend
（无输出 = 成功）
```

`ruff check work/` 在 base 上就有 3 个 B905（两个在既有 `work/tradability_mutation_check.py`，
一个在本 PR 新增的矩阵文件里，属同类既有风格），不在 CI 的检查范围内（CI 只跑
`ruff check backend`）；已在 base 与 head 上分别确认数量一致，非本 PR 引入。

---

## Frontend impact audit

搜索 `frontend/`：

```
$ grep -rn "tradability" frontend/src/ frontend/index.html
（无结果）
$ grep -rln "tradability_archive|tradability_ingestion|tradability_shadow|tradability_backfill" backend/api_*.py backend/main.py
（无结果）
```

前端现有的 "shadow" 字样全部属于既有的**自适应 / 风控影子调参**体系
（`adaptive.js` 的 `shadow_candidate`、`portfolio-shadow-panel`、
`execution_quality_shadow.py` 等），与可交易性事实层无关；现有 diagnostics 页面
（`p-selection-evaluation` → `/api/selection-evaluation`）消费的是收盘跟踪账本，也不消费
本层。

**本 PR 没有新增 backend API**（Shadow 的消费入口是只读 CLI，不是 HTTP），因此：

> 已检查前端消费链路，本 PR 无需前端修改。

### 前端门槛（未改前端，仍需证明无 regression）

```
$ node build.mjs
$ git diff --exit-code -- frontend/dist      → 无 diff（dist 保持新鲜）
$ npm run test:unit
# tests 111  # pass 111  # fail 0
$ npx playwright test --project=chromium --workers=1
32 passed (2.2m)
```

---

## Sensitive scan

```
$ python scripts/security/scan-sensitive-data.py --repo . --scope worktree
kinds  : none
values : 0
```

（本 PR 新增的 `work/pr160_*.py` 最初在 docstring 里写了本机 venv 的绝对路径，被
`LOCAL_PATH` 抓到 1 项；已改成占位符后重扫为 0。）

---

## Exact-head CI

见下方评论（push 后按**最终 head SHA** 读取，不沿用任何此前 head 的结果）。

---

## 明确声明

```
Historical Tradability Archive remains zero-authority.
Shadow validation does not alter orders, fills, positions,
selection decisions, or learning behavior.
```
