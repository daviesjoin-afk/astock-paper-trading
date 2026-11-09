# ADR 005：模拟总账户与双策略资金槽位

状态：已实现。账户契约、资金划分、只读模型、槽位订单/成交、确定性会话、批量/账户级推进均已落地；估值见 ADR-006/007，动态分配见 ADR-008/009，自进化见 ADR-012–018，策略切换见 ADR-018。

## 问题

ADR 001 让每个实验拥有独立资金与账本，实验是**研究身份**。最终产品需要一个用户只输入一次本金、由两套策略共享的**模拟总账户**：

- 用户只设置本金、启用两套策略、设定风险边界；
- 账户把本金划分为两个策略预算与一笔不参与交易的准备金；
- 两套策略各自持仓、各自成交，但都不能越过账户边界；
- 重启、回放、审计都必须给出完全一致的事实。

不能用 `cycle_id` 冒充总账户：实验继续承担研究身份，账户与槽位需要自己的所有权。

## 决定

### 1. 所有权

| 实体 | 所有者 | 说明 |
| --- | --- | --- |
| `PaperAccount` | `v2_paper_accounts`（Store 唯一写入者） | 用户本金、创建时刻、账户身份 |
| `PaperSlot` | 账户 spec 内冻结的两条记录 | 策略工件、数据包、执行规则、权重、预算 |
| 实验 `cycle_id` | 独立表 | 研究身份，不参与账户账本 |
| 准备金 | 账户只读派生 | 不交易、不分配、不参与策略预算 |

不新增服务、不新增数据库写入者；账户与槽位是同一条 SQLite 事实链上的两类对象。

### 2. 资金划分（纯函数）

`PaperAccountSpec::validate()` 计算 `PaperCapitalSplit`：

- 权重以**账户整体的 basis points** 表达，`sum(slot.weight_bps) + reserve_bps = 10_000`；
- 每个槽位预算 `floor(initial_cash_cents × weight_bps / 10_000)`；
- 除法余数全部进入准备金：`reserve = initial − Σ budgets`，因此
  `reserve + Σ slot_budget = initial` 恒成立，且准备金 ≥ 0；
- 权重或准备金非法、槽位不是恰好 A/B、两个槽位复用同一策略、初始资金 ≤ 0、`created_at` 不是显式 RFC3339 → 统一拒绝。

`created_at` 由调用方给出：domain 不读机器时钟，账户创建时刻也是显式事实。

### 3. 守恒与隔离（后续 PR 必须继续满足）

- 槽位现金只能来自自己的预算；成交只能扣自己的现金、只能卖自己的持仓。
- 槽位之间不能互相转账、不能共享持仓、不能透支。
- 准备金永不参与撮合，任何成交都不得使 `slot.cash_cents < 0`。
- 账户现金 = 准备金 + 两槽位现金；任何时刻都不得出现"额外现金"。
- 同一账户 id 重复提交相同 spec 幂等；不同 spec 冲突，绝不静默覆盖。

本轮只实现"预算 + 初始现金 + 空持仓"的只读模型；成交路径在下一轮加入时，必须补充
"比较前后逐字段守恒"和"重启一致"的测试。

### 4. 明确不做

- 不做动态资金分配（第四阶段）、不做自进化（第五阶段）；
- 不把账户结果写回实验，也不让实验直接消费账户资金；
- 不引入 ORM、消息队列、第二个写入者或独立账户服务；
- 不在本轮加前端页面（只读界面在 API 稳定后单独 PR）。

## 验收（本轮）

1. 100,000.00 元按 45% / 45% / 10% 划分 → A 45,000.00、B 45,000.00、准备金 10,000.00；
2. 不能整除的余数进入准备金，`reserve + Σ budgets` 始终等于初始资金；
3. 非法 spec（权重和、重复槽位、重复策略、零本金、非法时间、非法准备金）全部拒绝；
4. 相同 spec 幂等、不同 spec 冲突；
5. 同一 SQLite 文件重新打开后账户视图逐字段相同；
6. 账户 HTTP 端点只读/幂等，不产生交易事实。

### 6. 订单路径（已实现）

`PaperOrderRequest { schema_version, account_id, slot, signal_id, decision_at, signal, quote_ref }`

- 策略身份、预算、现金和持仓由**槽位**拥有；请求只表达意图与依赖的行情证据。
- 决策复用 `domain::decide` 与实验完全相同的规则（会话、日历、身份、T+1、现金、容量），不复制判断逻辑。
- 每个槽位内 append-only：新决策时间不得早于该槽位上一笔；跨时区按同一 instant 比较。
- 幂等键 `(account_id, slot, strategy_version_id, signal_id)`：同请求返回 `duplicate=true`，同键不同载荷 → `Conflict`。
- 证据缺失或过期不报错，按规则产出 `rejected`（`quote_evidence_missing` 等），账本不变。
- 成交按槽位重建 `PortfolioState`（从该槽位的 fill 顺序回放），任何时刻不得透支槽位现金。
- 现金守恒：`reserve + Σ slot.cash = initial − Σ(买入含费) + Σ(卖出扣费)`；准备金不参与任何撮合。

### 7. 确定性会话（已实现）

`PaperSessionSpec { schema_version, session_id, account_id, start_at, signals[] }`

- 信号列表是**冻结输入**：每条包含槽位、signal id、决策时刻、方向/标的与报价证据引用；必须按时间非降序、且不早于 `start_at`。
- 会话状态机：`created → running → paused → running → … → stopped`，`running` 且游标耗尽时自动 `completed`；非法转移返回 `invalid_paper_session_transition`。
- `step` 一次只做一件事：取游标处的信号，提交**恰好一笔**槽位订单，然后把 `clock_at` 推进到该决策时刻。暂停/停止时 `step` 返回 `paper_session_not_running`，不落账。
- 会话时钟是显式字符串，不读机器时间；同一信号列表 + 同一账户初始状态在任意重放中产生同一账本。
- 订单信号身份加会话前缀（`session_id:signal_id`），因此会话订单与手工订单可区分，重试仍是幂等。
- 崩溃恢复：订单先落账、会话游标后更新；重试命中幂等键后再推进游标，不会重复成交。

### 8. 客户端创建账户（已实现）

`PaperAccountRequest { schema_version, account_id, initial_cash_cents, reserve_bps, created_at, slots[{slot, strategy_version_id, market_data_pack_id, execution_policy_version, weight_bps}] }`

- 浏览器只提交**已审核策略的 id**，服务端用内置 reviewed artifact 解析内容身份与摘要；客户端永远不携带 digest，也无法提交自定义策略。
- `GET /v2/strategies` 返回可选策略、默认数据包、执行规则与准备金比例，作为表单的唯一事实来源。
- 校验仍在 domain：权重和、A/B 槽位、重复策略、本金、时间、数据包/规则兼容。
- 同 id 重复提交同内容幂等，不同内容冲突。

### 9. 从实验导入会话（已实现）

`POST /v2/paper-sessions/from-experiment { session_id, account_id, cycle_id, slot, start_at? }`

- 只读读取实验已存的决策（含被拒绝的决策），按 id 升序转成会话信号；不重新撮合、不改写实验。
- 槽位策略、数据包、执行规则必须与该实验一致，否则分别返回 `paper_slot_strategy_mismatch` / `paper_session_pack_mismatch` / `paper_session_policy_mismatch`。
- `start_at` 缺省时取第一条决策所在**中国日**的 00:00（显式派生，不读机器时间）。
- 空实验拒绝（`paper_session_requires_signals`）；不存在的实验返回 404。
- 导入后的会话与手工会话同构：启动、暂停、继续、运行到结束、重放规则完全一致。

### 10. 按日推进与估值占位（已实现）

- `POST /v2/paper-sessions/{id}/advance { day }`：只执行**中国日 ≤ day** 的信号；两个决策之间的日期不产生任何动作；非法日期返回 `invalid_advance_day`。未来信号永远不会被提前执行（无 look-ahead）。
- `PaperAccountView.valuation` 固定输出 `{status:"unavailable", reason:"no_reviewed_price_set"}`：在 ADR-006 定义审核价格集合之前，账户页不计算市值、净值、收益或回撤，只展示现金与持仓数量，并显式提示不可估值。

### 11. 账户风险边界（已实现）

`PaperRiskLimits { max_symbol_weight_bps, max_order_notional_bps, min_slot_cash_bps }`（默认 25% / 50% / 10%，均为槽位预算的 basis points）：

- **成本口径、无需价格**：单笔金额 = 数量 × 成交价；单标的成本 = (现有数量 + 本次数量) × 成交价；现金下限 = 成交后槽位现金。
- 任一越界 → 该笔决策记为 `rejected`（`risk_order_notional_exceeded` / `risk_symbol_concentration_exceeded` / `risk_cash_floor_exceeded`），**不产生部分成交、不动账本**。
- 卖出不受买入侧金额/集中度约束（后续如需卖出侧限制，单独建模）。
- 数值区间 0–10000 bps；非法值拒绝创建。
- 账户视图输出 `limits`，控制台在槽位卡上明示当前边界。

### 12. 账本自检（已实现）

`GET /v2/paper-accounts/{id}/audit` 返回只读审计：

| 检查 | 含义 |
| --- | --- |
| `budget_split` | 槽位预算等于 spec 划分结果 |
| `fills_replay` | 槽位状态可由成交顺序重建 |
| `capital_conservation` | 初始本金 + 净现金流 = 准备金 + 两槽现金 |
| `order_fill_link` | 已成交订单数 = 成交条数 |
| `fill_matches_outcome` | 每条成交与其订单 `outcome.fill` 完全一致（可发现篡改） |
| `session_link` | 会话 `last_order_id` 必须属于该账户的订单 |
| `session_signals` | 会话订单必须与冻结信号前缀逐条一致（signal id、决策时刻、报价引用），且条数等于 cursor |
| `rebalance_chain` | 划拨的权重链连续、每次划拨保持准备金、现金只在不同槽位间移动，且最后权重等于当前权重 |

整体 `ok` 与 canonical `digest` 一并返回；控制台在账户页展示「账本自检：通过/失败 + 摘要」。审计只读，不修复、不写回。

## 未决问题

- 自动运行时钟（第二阶段）与账户的关系：账户是否需要"当前模拟日期"这一显式状态？倾向放入独立的 run session，而不是写进账户 spec。
- 动态分配的输入（净值/风险）来自第三阶段估值体系，必须先有可信价格集合。
- 槽位是否允许中途替换策略：倾向不允许，替换等于新建账户或新 session。
- ~~自动运行时钟~~ 已决定：独立 session 持有显式时钟与冻结信号列表（见 §7）；批量 `run` 与前端控制留在下一 PR。
- 账户级风险上限：单策略敞口、单标的集中度、现金预留的下限，需要在动态分配 PR 中显式建模并进审计。
