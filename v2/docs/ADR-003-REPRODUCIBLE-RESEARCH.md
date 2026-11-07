# ADR 003：可核验策略工件与冻结研究输入

状态：提议（Draft PR）。基线为 #355 合并提交 `5c8029d44c86415395ec4d614909610d38f4aa91`，master 完整检查成功。

## 三种操作必须区分

| 操作 | 现有能力 | 本轮新增 |
| --- | --- | --- |
| 历史回放 replay | `Store::verify_replay` 用已固定输入重新执行**已存储**决策，逐字段比对请求、证据、成交和资金 | 不改语义 |
| 重做 redo | 无 | 用**已固定的策略定义**和**已固定的研究输入**重新生成研究信号，并与审核过的研究输出逐字段比对 |
| 策略比较 compare | 无，且本轮不做 | 不做。比较需要统计口径、样本外区间和多重检验控制，本轮只交付"同一策略可重做" |

回放成功不能证明研究信号可重新生成：回放只重算"已经写进库里的决策"，它的输入就是那些决策本身。把回放测试当成重做测试是错误等价。

## 修复前的三个真实缺口（先写失败测试）

1. **策略版本只有身份，没有内容。** `StrategyArtifact { strategy_version_id, research_input_id }` 只有两个字符串。修改研究参数、研究源码或算法版本，只要沿用同一个 `strategy_version_id`，Store 就无法发现；它比较的是"声明是否相同"，不是"内容是否相同"。
2. **同一数据包可因追加证据的时点与集合不同而产出不同的新决策，且没有任何事实记录该实验使用了哪一组输入。** 证据是不可变的，但"某个实验的研究输入是哪一组证据"从未被登记。后来的证据可以合法进入同一个实验，使同一实验在两次运行时拥有不同的输入集合；没有内容身份就无法说明两次运行的差异来自策略还是输入。
3. **`verify_replay` 只能校验已存储决策。** 它不知道研究代码、参数和研究输出是什么，因此无法回答"研究信号能否重新生成"。

失败测试固定在 `v2/research/test_research_reproducibility.py` 与 `v2/src/store/tests.rs` 的 `research_*` 用例中；修复前它们因缺少契约字段/方法而失败，修复后通过。基线回放测试保持原有断言，不冒充重做测试。

## 决定

### 1. 策略工件 v2（内容身份）

`StrategyArtifact` 升为 `schema_version = 2`，记录：

| 字段 | 含义 |
| --- | --- |
| `strategy_version_id` | 已审核策略身份（不变） |
| `research_input_id` | 已审核研究输入身份 |
| `algorithm_version` | 固定算法契约版本，本轮为 `research-signal-v1` |
| `source_digest` | 研究算法源码（归一化换行后）的 SHA-256 |
| `parameters_canonical` | 规范化参数 JSON 文本（键排序、紧凑分隔符） |
| `parameters_digest` | 上述文本的 SHA-256 |
| `research_input_digest` | 冻结研究输入清单内容摘要 |
| `random_seed` | 仅随机算法使用；本轮两个策略都是确定性算法，显式为 `null` |
| `runtime_version` | 运行环境契约，本轮为 `python-3.13-stdlib` |

同一 `strategy_version_id` 与不同内容必须冲突。Rust 从仓库内字节重新计算三个摘要后与声明比对，不信任调用者自报：`source_digest` 对 `research/strategies/*.py` 的实际源码计算，`parameters_digest` 对 `parameters_canonical` 计算，`research_input_digest` 对审核过的输入清单计算。SHA-256 由 `domain/digest.rs` 自带实现（FIPS 180-4，含 NIST 向量测试），不新增 crate 依赖。Python 侧只用标准库 `hashlib`。

审核目录沿用数据包的做法：`src/store/mod.rs` 内的显式匹配把 id 映射到 `data/strategy-*.json` + 源码文件，未知 id 明确拒绝。新增策略需要新的数据记录和审核定义，不提供上传或动态加载。

### 2. 冻结研究输入（不可变清单）

`data/research-input-*.json` 是研究输入的唯一权威表示，记录有序条目：

- `sequence`、`decision_at`（研究时钟）、`available_at` 与 `quote`（含 `observed_at`）。
- 每个条目的双边离线流动性 `buy` / `sell`（价格与容量），使同一份"市场快照"可以支撑任一方向的信号；执行侧按实际方向取用其中一侧。
- `content_digest` 覆盖 `{schema_version, research_input_id, market_data_pack_id, entries}`，不含描述性文本。

条目顺序、时点和内容都在摘要内，因此"追加一条""换一条""调换顺序"都会改变内容身份。Store 在创建实验时以事务登记清单，同 id 异内容冲突，重启时逐条与审核定义比对，被篡改即整库拒绝。

**为什么新增一张表（不可替代的业务用途）**：研究输入是独立于策略、独立于实验的事实所有权。两个实验（同一策略的重做、不同策略的比较）必须能引用*同一*输入身份，而实验行内嵌会让"同一输入"变成"同一个实验内的输入"，无法跨实验核对，也无法在实验被删除/完成后继续解析。`v2_research_inputs(research_input_id, manifest_json)` 只做这一件事，不改任何交易表结构。

### 3. 重做校验

`Store::verify_research_reproduction(cycle_id)`：

1. 实验工件必须逐字段等于审核定义（含三个摘要）。
2. 审核过的研究输出 `data/research-output-<strategy>-v1.json` 的 `algorithm_digest` / `parameters_digest` / `research_input_digest` 必须等于工件声明，且 `content_digest` 自洽。
3. 该实验已存储的决策序列必须与冻结研究输出**逐字段一致**：数量、顺序、`signal_id`、`decision_at`、`symbol`、`side`、`quote_ref`。多一条、少一条、换一条都失败。
4. 每条决策固定的证据必须与冻结输入条目一致（`quote`、`available_at`、所用方向的流动性）；缺失证据仍按缺失核验，不会被后来到达的证据替代。

研究报告只描述研究信号与输入身份。它不分派成交：数量、价格、费用、资金和 T+1 仍由 Rust `decide` 独立判断，`verify_replay` 独立核验。

研究输出文件由 Python 从"审核过的策略源码 + 审核过的输入"生成；`python -m unittest discover -s v2/research` 在 CI 内重新生成并与仓库字节比较，任何漂移即失败。Rust 不执行 Python，只把已存储决策与审核文件比对——这条链路是"Python 生成、CI 固定、Rust 核验"。

### 4. 事实划分

| 事实 | 所有权 |
| --- | --- |
| 市场价格规则（日历/时段/证券/数量规则） | 数据包清单，不可变 |
| 逐条行情证据 | 不可信离线导入 + Store 记录首次接收时间；同一身份不可覆盖 |
| 声明可见时点 | 证据 `available_at` 与冻结输入条目；研究声明，不是供应商事实 |
| 系统实际接收时间 | 仅 Store 写入 `received_at`，永不参与研究身份或回放判定 |
| 研究输入（用哪一组证据、以什么顺序和时点） | `v2_research_inputs` + `data/research-input-*.json` 内容摘要 |
| 研究信号 | Python 生成，审核文件固定，Rust 只读比对 |
| 成交事实 | Rust 独立判断，仅 Store 写入 |

系统实际接收时间与历史声明可见时间永远分列两处：前者是运行事实，后者是研究声明。二者相等只是巧合，不构成约束。

### 5. 版本与兼容

- `SimulationRequest` 仍为 4；`QuoteImport` 2；`QuoteEvidence` 5；`ExecutionLiquidity` 2；`ExecutionPolicy` 仍为 `v2-paper-policy-6`。
- `ExperimentSpec.schema_version` 仍为 1，形状不变；变化在内嵌 `StrategyArtifact` 的契约版本。
- SQLite 一次升为 schema 8：新增 `v2_research_inputs`。schema 7 文件在写入前明确拒绝，不自动迁移、不推断。
- 旧 `StrategyArtifact`（schema 1，两字段）被拒绝：它没有内容身份，接受它就等于继续允许缺口 1。

## 验收

- 修复前失败：审核工件无法反序列化（缺内容字段）；未审核策略/未审核研究输入被接受；不存在重做校验。
- 修复后通过：同 id 异参数、异源码、异输入分别冲突；两个原创策略在同一输入上生成不同但各自可复现的信号；同一策略在两次独立实验中得到逐字段相同的成交；篡改冻结输入、追加非冻结决策、替换证据均被拒绝且不写库；进程重启后仍可复核。
- 合成样例明确标识：`600000.SH`、价格与容量都是离线测试输入，不声称历史收益或策略优势。

## 不做

不实现策略自动晋级、研究排名、真实数据供应商、实盘、完整订单生命周期、通用插件框架、ORM、任务队列、微服务或 V1 兼容层。不新增 crate、Python 依赖或第二个服务。V1 代码不动。
