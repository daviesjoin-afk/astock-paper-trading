# ADR 014：影子候选工件的实验边界

状态：提议（Draft）。本轮允许**影子候选**创建实验：`shadow-` 前缀、复用审核源码摘要、独立参数；账户与资金路径仍只接受审核工件。

## 问题

ADR 013 让候选工件与信号自洽，但实验接口只接受审核工件（id、内容、参数、输出全部相等）。候选因此无法进入影子运行，自进化停在“生成清单”。

## 决定

### 1. 允许的例外（仅实验）

`validate_experiment_spec` 对 `strategy_version_id` 以 `shadow-` 开头者：

1. 工件必须自洽（`verify_content`：参数规范文本与摘要一致）；
2. `source_digest` 必须等于**某个审核策略**的源码摘要（当前 momentum / reversion）；
3. `research_input_id` 必须可解析为审核输入，且摘要一致；
4. 满足则接受，**不要求**与审核工件整体相等，也不要求存在审核研究输出。

其它情况保持原规则：非 `shadow-` id 必须与审核工件逐字段相等，且输出摘要匹配。

### 2. 不改动的边界

- 纸面账户创建仍只解析审核策略 id（`create_paper_account_request` → `reviewed_strategy_artifact`）；影子工件不能拥有资金槽位。
- 影子实验不享受“审核研究输出”复现：`/replay` 只验证存储的决策与成交；研究复现仍只针对审核工件。
- 晋级仍由 ADR 012 的证据规则与事件记录决定；本轮不自动晋级。

### 3. 运行方式

```
generate_candidates.py → candidates.json
emit_candidate_artifacts.py → shadow-<candidate>.artifact.json / .output.json
scripts/shadow_candidates.py → 创建实验、导入报价证据、提交冻结信号、输出计数
```

冒烟验证：一个 `lookback=3` 候选产生 1 条信号（基准 2 条），实验 `runs_total=1`，成交/拒绝合计 1。

## 验收（本轮）

1. `shadow-` + 审核源码摘要 + 审核输入 → 实验创建成功；
2. `shadow-` + 未知源码摘要 → `unsupported_strategy_artifact`；
3. 非 `shadow-` 的漂移工件 → 冲突/规则错误（原行为）；
4. 影子工件无法用于纸面账户（账户创建路径不变）；
5. 冒烟端到端：候选 → 实验 → 信号 → 计数一致。

## 未决问题

- 影子参数语义校验（上界/正数）仍需研究侧保证；Rust 只校验摘要自洽。
- 是否需要为影子实验单独的研究输出登记（若未来要做影子复现）。
- 多源码版本并存时，`source_digest` 白名单需要随审核清单扩展。
- 自动晋级、指标抽取（依赖审核价格）仍未接线。
