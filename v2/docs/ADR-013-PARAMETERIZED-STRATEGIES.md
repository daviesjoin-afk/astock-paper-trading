# ADR 013：参数化策略与候选工件

状态：提议（Draft）。本轮让策略**从工件读取参数**，并提供把候选清单变成自洽工件与信号的离线步骤——影子评估的前置条件。仍不评估盈亏、不晋级。

## 问题

候选生成器只产出参数清单；若策略源码把参数写死，所谓“候选”行为完全相同，影子评估毫无意义。

## 决定

### 1. 策略接口

`signals(entries, parameters=None)`：

- `parameters is None` → 使用模块内 `PARAMETERS`（审核默认值）；
- 否则使用传入的参数映射，必须包含 `lookback` 与 `threshold_bps`；
- 规则本身不变，仅参数来源变化。

`research_contract.build_output()` 从工件 `parameters_canonical` 解析参数并传入；工件仍是参数的唯一权威。

### 2. 摘要级联

修改策略源码会改变 `source_digest`，因此：

1. `run_research.py --strategy X --emit` 重新生成 `strategy-X-v1.json` 与 `research-output-X-v1.json`；
2. `--check` 必须通过（仓库字节 ↔ 审核工件逐字节一致）；
3. Rust 侧继续用内置源码字节校验工件 `source_digest`，无需改动。

本次重新生成后，两套策略的默认参数与信号数量不变（2 条 / 2 条），仅摘要变化。

### 3. 候选工件发射

`research/emit_candidate_artifacts.py`：

- 输入候选清单（`generate_candidates.py` 产物）；
- 复用审核源码（`source_digest` 不变），替换 `strategy_version_id` 与参数/摘要；
- 用 `research_contract` 校验工件与输出，写入 `<candidate>.artifact.json` / `<candidate>.output.json`；
- 输出的信号由**同一契约**生成，可直接作为影子实验的冻结研究输出。

不修改 `data/` 下的审核文件；候选工件只写入调用方指定目录。

## 边界

- Rust 仍只校验工件自洽（参数摘要、源码摘要、输入摘要），不解析参数语义；
- 参数合法性由研究侧网格（ADR-012）保证；
- 候选工件不是审核工件：晋级必须走 ADR-012 的证据规则与事件记录；
- 尚无 P&L 评估：影子指标仍需审核价格集合（ADR 006/007）。

## 验收（本轮）

1. 默认参数下 `--check` 通过，信号与摘要可复现；
2. 候选工件通过 `verify_artifact` / `verify_output`，`source_digest` 与基准一致；
3. 改变 `lookback` 可让候选信号与基准不同（测试用 lookback 10 → 0 条信号）；
4. 发射结果字节确定（重复运行一致）；
5. 未知基准策略、非法清单拒绝。

## 未决问题

- 影子实验如何引用候选工件（现有实验接口要求工件随 spec 提交，需确认审核边界）；
- 候选参数语义校验（正数、上界）是否也要在 Rust 侧复核；
- 滚动验证与样本外切分的具体实现；
- 多策略源码版本并存时的目录布局。
