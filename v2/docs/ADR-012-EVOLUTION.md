# ADR 012：自进化治理（晋级与回退规则）

状态：提议（Draft）。本轮落地**决策层**：给定候选与基准的样本外/影子指标，按硬规则给出 `promoted` / `rejected` / `insufficient_evidence` / `reverted` / `kept` 及逐条理由。不做参数搜索、不写账本、不碰价格。

## 为什么先做决策层

自进化的风险不在“能不能生成候选”，而在“用什么证据晋级”。把规则做成可测试的纯函数，可以让后续的参数搜索、影子运行、自动晋级都受同一套约束，避免用单次回测的偶然收益换版本。

## 规则

`EvolutionPolicy` 默认值：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `min_out_of_sample_days` | 20 | 样本外最少交易日 |
| `min_shadow_days` | 10 | 影子运行最少交易日 |
| `min_trades` | 10 | 最少成交笔数 |
| `max_drawdown_bps` | 1500 | 候选回撤上限 |
| `promote_margin_bps` | 50 | 相对基准的收益门槛 |
| `rollback_drawdown_bps` | 2000 | 晋级后回撤回退阈值 |

**晋级**（`decide_promotion`）：

1. 样本不足（任一维度）→ `insufficient_evidence`，**不晋级**；
2. 候选回撤 > 上限 → `rejected`（风险恶化）；
3. 候选收益 < 基准收益 + 门槛 → `rejected`（优势不足）；
4. 全部满足 → `promoted`，理由写明收益/回撤与阈值。

**回退**（`decide_rollback`）：

1. 当前回撤 > 回退阈值 → `reverted`；
2. 当前收益 + 门槛 < 晋级时收益 → `reverted`（优势消失）；
3. 否则 `kept`。

## 边界

- 纯函数：不读价格、不读账户、不写账本；
- 指标必须由研究/影子流程提供；本层不判断指标来源是否可信（由 ADR 006/007 的审核价格保证）；
- 不实现参数搜索与自动执行晋级；这些是后续工作；
- 决策带 canonical `digest`，可复算。

## 验收（本轮）

1. 指标充足且优势达标 → `promoted`，理由完整；
2. 样本不足 → `insufficient_evidence`，即使收益极高；
3. 回撤超限 → `rejected`，理由含风险恶化；
4. 优势不足门槛 → `rejected`；
5. 晋级后回撤恶化/优势消失 → `reverted`，否则 `kept`；
6. 同一输入决策与摘要稳定。

## 候选生成（已实现）

`v2/research/generate_candidates.py`（stdlib-only）：

```bash
python research/generate_candidates.py   --base data/strategy-momentum-v1.json   --grid grid.json --out candidates.json --max-candidates 32
```

- `grid.json` 形如 `{"lookback":[2,3,5],"threshold_bps":[25,50]}`；
- 只允许已声明的参数（当前 `lookback` 1–250、`threshold_bps` 1–5000），非整数/越界/缺参直接拒绝；
- 笛卡尔积超过上限（默认 32）拒绝；与基准相同的组合被剔除；
- 候选身份 = 参数规范 JSON 的 SHA-256（与 Rust canonical digest 口径一致，已用真实工件 `parameters_digest` 校验）；
- 输出 manifest：`schema_version / base_strategy_version_id / base_parameters_digest / candidates[] / digest`；
- 不评估、不晋级、不写账本——只产出影子运行可消费的确定性候选清单。

## 影子决策 CLI（已实现）

```bash
python scripts/shadow_candidates.py --artifact cands/X.artifact.json   --output cands/X.output.json --cycle shadow-candidate-1   --base-cycle shadow-base-1 --performance-days 2026-10-30,2026-11-02   --decide --record-event --event-id evo-shadow-1   --decided-at 2026-11-06T15:00:00+08:00
```

- 使用**同一审核价格序列**为基准与候选取绩效（ADR 016），任一缺失即返回 `unavailable`，不产生决策；
- `--decide` 调用治理规则；`--record-event` 还需显式 `--event-id` 与 `--decided-at`（拒绝机器时间）；
- 指标口径：`out_of_sample_days = shadow_days = 序列天数`，`trades` 来自成交；
- 输出包含 `performance{base,candidate}`、`decision` 与 `event`。

## 事件与实验关联（已实现）

`POST /v2/evolution/events` 可带 `base_cycle` / `candidate_cycle`（均可选），事件因此能指向产生指标的实验；`GET` 列表与分页均返回这两个字段，进化视图显示“基准 … · 候选 …”。schema v14。

## 回退演练（已覆盖）

冒烟在合成指标上走完“晋级 → 风险恶化 → 回退”：

1. `POST /v2/evolution/events`（kind=promotion）→ `promoted`；
2. `POST /v2/evolution/events`（kind=rollback，当前回撤 2500 bps、收益 100 bps）→ `reverted`；
3. `GET /v2/evolution/events?status=reverted` → 恰好该事件。

说明：这验证的是**规则与事件链路**，不代表任何真实绩效。

## 未决问题

- 指标来源：需要由审核净值序列（ADR 007）统一生成，禁止手工填写；
- 滚动验证与多重比较校正：候选数量多时需要显著性控制；
- 事件查询：`GET /v2/evolution/events?limit=&candidate=&status=&base_cycle=&candidate_cycle=` 支持按候选 id、状态与关联实验精确过滤（在最近 200 条内过滤后再截断）；分页端点同样接受这两个实验参数；日期范围用 `from_day`/`to_day`（含端点，按 decided_at 的**中国日**判定，非法日期 422 `invalid_evolution_day`），在扫描窗口内服务端过滤；分页用 `GET /v2/evolution/events/page?limit=&before=&before_id=&after=`，游标为 `(decided_at, event_id)`（均不含），同一时刻多条事件不会漏读，最大扫描 2000 条。
- 晋级/回退事件已可持久化（`POST/GET /v2/evolution/events`，schema v13）：`event_id` 幂等、同 id 不同决策冲突、列表按时间倒序；仍需与影子运行流水线打通；
- 参数搜索空间与算力预算。
