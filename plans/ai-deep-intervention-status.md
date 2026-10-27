# AI 深度介入：实施状态（对应审核文档的 S0–S4）

- 状态日期：2026-10-26；基线 `d89fe4a`（审核针对的提交），本文记录其后的修复与推进。
- 本文只记录**已落地并有测试**的内容。设计意图仍以
  [整体方案](ai-deep-intervention-master-plan.md)、[基础方案](ai-shadow-and-performance-owner.md)
  和[审核文档](ai-deep-intervention-review-20261026.md)为准。
- 当前权限状态：**ADVISORY**。没有签发任何授权凭据，自动采用路径端到端不可达。

## S0 修正底座：7 项缺陷全部修复

| 缺陷 | 修复要点 | 回归用例 |
|---|---|---|
| R1 窗口盈亏混入窗口外收益 | 盈亏事实改为带成交日 `(fill_date, pnl)`；跨日分批成交无法拆日 → 返回不可用；活动周期用 `owner(窗口末) − owner(起点前一天)` | `test_strategy_performance.ArchiveReadTests.test_only_the_in_window_realized_pnl_is_reported` 等 6 项 |
| R2 缺报价把市值算成零 | 任意持仓缺当日报价即抛 `missing_position_quote`，拒绝发布该会话；运行侧转 `waiting_data` | `test_native_paired_replay.ValuationTests.*`、`test_experiment_increment...missing_position_quote...` |
| R3 回执可能只写一侧 | 两侧预检 + `SAVEPOINT` 写入；工作单元整体保存点；"已发布"要求两侧完整配对；半侧残留显式失败 | `test_agent_task_runner.WorkUnitAtomicityTests.*`、`test_experiment_increment...half_published...` |
| R4 执行期持有写锁 | 领取/启动/续约/发布各自短事务；计算在事务外；`TaskOutcome.publish` 只在短事务里跑 | `test_agent_task_runner.ConcurrencyTests.*` |
| R5 会话序号冒充任务步数 | `step` 记本任务工具步数；实验游标进 checkpoint/payload | `test_experiment_increment...twenty_session...` |
| R6 环境身份不含因子表 | 身份纳入 columns/index/逐行取值；`REPLAY_VERSION` 升 v2 | `test_native_paired_replay.EnvironmentTests.*` |
| R7 CLI NameError | `__main__` 移到文件末尾；CLI 用**子进程**回归 | `test_experiment_increment...subprocess`、`test_research_worker...subprocess` |

审核"附加观察"的租约边界也一并收紧：`_authorize` 检查 `lease_expires_at`，到期即拒写；
任务在计算期间被取消，发布前复核会拒绝（`test_agent_task_runner...cancelled_during_the_handler`）。

## S1 一个 AI 循环跑通

已落地：

- `ai_research_agent.py` —— 工具循环。白名单只读工具（绩效事实 / 实验进度 / 目标清单），
  决策契约只接受 `continue / revise_hypothesis / eliminate / wait_for_data`；
  权威字段（qty/shares/promoted/verified/capability_id…）出现即整条拒绝；
  非法输出允许修一次，再错整条拒绝；步数有上限且每步写检查点；供应商故障走可重试。
  生产模型来自既有槽位（`ai_review_service.get_slot_config` + `ai_provider_transport.call_json`）。
- `ai_recommendation_store.py` —— 方案 §6.1 最小意见对象，append-only（触发器拒绝改删），
  写入前校验禁用键与非有限数值。
- `research_worker.py` —— 生产注册/启动入口（`run` / `status`）：注册研究 handler、
  自愈补投下一会话、短事务推进、快照同时给出残留/失败/耗时/授权模式。

验收对应关系：

| 审核要求 | 现状 |
|---|---|
| 明确的生产注册/启动入口 | `research_worker run\|status`，子进程可跑 |
| 接现有模型提供商 | `AGENT.slot_provider()` 读既有槽位；未配置时返回受控拒绝而非伪造调用 |
| AI 读工具结果并选择继续/改假设/淘汰/等待 | 四种动作全部实现并有测试 |
| 无需手动调用 Python 函数即从入队走到结论 | `test_ai_research_agent.EndToEndTests...through_the_worker`（provider 为注入式假实现） |
| 重启后能续接 | `test_ai_research_agent...restart_resumes_from_the_committed_step` |

**未完成**：真实模型的一条端到端调用未在服务器上跑过（本机无凭据）。
本地只能证明"接得上、边界对、失败可控"，不能证明"某个模型会给出好判断"。

## S2 测量与提速

已落地：`research_metrics.py` 把耗时拆成四段——`queue_wait / provider_wait / compute /
evidence_wait`，给 p50/p95，并直接指出最长那段是什么、该不该动手（等市场的那一段
工程加速无效）。数据只来自既成的生命周期时间戳与阶段计时，不新增埋点。
`research_worker status` 已把它接进快照。

**未完成**：审核给的三个 p95 目标（事件→任务 ≤2 秒、快照→意见 ≤30 秒、小批实验 ≤15 分钟）
**尚未实测**。原因见下节"服务器环境核实"：真实模型调用目前不具备配置前提。

为避免再靠"试一次调用"猜测配置状态，已提供三条只读命令：

- `research_worker status` → `provider` 段报 `ready / key_set / model / base_host`（不含密钥）；
- `research_worker provider-check --slot ai1` → 发一次 32 token 的最小真实调用，
  返回 `stage=config|call`、`latency_ms`、`input/output_tokens`，非 OK 时退出码 2；
- `research_worker bench --runs N --sessions M` → **一次跑完三条目标**并给 p50/p95
  与 `met`（`true/false/null`）。实验段不需要模型，Agent 段只在 provider 就绪时测；
  没测到的段一律 `met=null` 并标 `provider_not_configured`，绝不因为"没报错"写成达标。

本地试跑（4 会话，无 provider）实测：`small_experiment_batch` p95 = **0.244 秒**
（目标 900 秒，`met=true`）；两条模型相关目标 `met=null`、原因 `provider_not_configured`。
**这只是工具本身跑通**，生产验收仍须在服务器上用真实数据与真实模型测。

## 服务器环境核实（2026-10-26 只读检查与运行时验证）

用只读中继连上 `[REMOTE_HOST]` 核对（未部署、未改配置）：

| 项目 | 实测 |
|---|---|
| 部署代码 | `/root/codex` 停在 `11bb144`，**本次工作尚未上线** |
| 容器 | `astock-codex` / `paper-worker` / `data-worker` / `task-worker` 均 Up（healthy） |
| 容器 Python | **3.14.9**（与本仓库 Dockerfile 一致；宿主机才是 3.9.25） |
| 容器根文件系统 | **只读**（`docker cp` 被拒），所以不能用拷贝方式就地验证 |
| 数据目录 | `/root/codex/data_cache` 挂到 `/app/data_cache`，`ASTOCK_DATA_DIR` 未设置 |
| 应用自读路径 | 容器内 `data_paths.data_dir()` = `/app/data_cache`（与挂载一致） |
| `ai_provider_slots` / `ai_saved_providers` / `ai_slot_provider_bindings` | **0 / 0 / 0 行** |
| `get_slot_config("ai1"/"ai2")` | `key_set=false`、`model=""`；主机也无 `AI_SLOT_*` 兜底 |
| 主机上全部 `paper_trading.sqlite3` | 已逐个查过（含 backup、docker volume），**没有一个配了 provider** |

**运行时验证（已做）**：把本分支新增/改动的 34 个 backend 文件打进 tar 送到服务器，
用 `docker run --rm -v /tmp/verify:/verify:ro astock-codex:latest`（同一生产镜像、
同一 Python 3.14.9）跑本分支的 14 个测试模块 →
**`Ran 306 tests ... OK`**。这证明新代码在生产运行时上能导入、能跑通；
它**不**等于已完成部署，也不等于做过真实模型调用。

## 仍缺的只有环境前提，不是代码

| 缺口 | 依赖什么 |
|---|---|
| 真实模型的一次端到端调用（S1 验收） | 在部署实例上保存一个 provider（当前 0 行） |
| 三个 p95 目标实测（S2 验收） | 同上 + 本分支需部署（当前停在 `11bb144`） |
| 真实环境里的凭据签发与自动采用 | 同上；且签发是运维动作，需人工发起 |

一条命令即可判定，不必再试错：

```
python -m research_worker provider-check --slot ai1     # 通了没有、多快
python -m research_worker bench --runs 5 --sessions 6   # 三条 p95 与是否达标
```

## S3 逐项接入模拟盘

已落地：

- `ai_advice_service.py` —— **入场建议**的消费与归因。原因分类合同
  `hard / soft_experimentable / unknown`，**unknown 按硬约束处理**；
  `prefer_entry` 不能把 rejected 改成 approved；过期/身份不符/越界/重复消费各有稳定原因；
  OFF 与 ADVISORY 下 `final_decision` 必须逐字等于 `rules_only_decision`。
- 同模块的**退出建议**归因：`hard_stop / system_drawdown / risk_exit / untradable …`
  视为硬退出，AI 说"再等等"一律被 `hard_exit_cannot_be_deferred` 拦下；
  `wait_soft_exit` 只在软退出下有意义；认不出的退出理由按硬处理。
- 同模块的**组合分配**：AI 只提目标比例，归一化复用既有
  `strategy_weight_scope.project_weights`（恰好 100%、每项在上下限内）；
  未知策略 / 负数 / 全零 / 上下限不可行都直接拒绝，不悄悄夹取。
- `strategy_health_service._performance_dimension` **已由 `strategy_performance` 产出**
  （原为 stub）：缺证据时给 `no_exact_pin` 这类**具体**原因，不再笼统地说"没有 owner"；
  **健康/退休阈值一个字未改**（方案 §2.5 要求那是独立变更）。
- **归因落库**（migration v40 `ai_decision_audits`）：每次决策写一条追加式解释行
  （`rules_only_decision / ai_opinion / final_decision / effect_reason /
  consumed_recommendation_id`）。同一 `(decision_id, audit_fingerprint)` 幂等，
  触发器禁止改删，**绝不回填历史**。读路径在旧库缺表时返回"尚未建立"，不抛栈。
- **复盘归因**：`research_worker attribution [--strategy-id]` 汇总审计行
  （按效果原因/模式计数、被消费意见数、最终动作被 AI 改变的决策数），并明确注明
  "本表只解释差异，AI 是否更好要看成对实验"——不拿计数冒充收益。

**未完成**：`promotion_science` 侧的晋级通道尚未消费这些审计行（晋级仍走原有的
成对实验证据链，这是刻意的：审计行不构成晋升凭据）。

## S4 范围内自动采用

已落地机制层：`ai_capability.py`、`ai_capability_store.py`，以及**签发链路**。

- 凭据字段按方案 §5.2 全部必填，缺一项即 `incomplete_capability_credential`。
- 采用器独立于生产者（`approver == producer` 直接拒绝）；有效期、撤销代号、
  策略/目标类型/字段范围、单次变更上限、日变更/成本/试次预算、写前基线复核（`stale_base`）、
  证据政策一致性，逐项校验并在拒绝时**不写任何东西**。
- **签发/撤销流程**：`issue` 要求运维身份；同 id 不同内容拒绝（不能悄悄改范围）；
  `revoke` 递增撤销代号并写 append-only 事件。预算按 `(capability_id, window_key)`
  持久化，重启与换候选都不清零。
- **HTTP 接口**（`/api/settings/ai-review/capabilities`）：
  `GET` 只读列表（不含任何密钥）、`POST` 签发、`POST /{id}/revoke` 撤销；
  写路由需 `confirmed=true` 并自动落入既有 operator 鉴权与 CSRF 门禁
  （`test_operator_boundary` 从 openapi 枚举写路由，136 项全绿）。
- **界面**：设置页新增"3. 自动采用授权（默认关闭）"区——列出现有凭据与撤销代号、
  提供全字段签发表单、写操作二次确认；没有一键开启，默认仍是 ADVISORY。
- **写入者已接线**（`ai_adoption_writer.py`）：真正落库的是既有的
  `adaptive_selection.apply_candidate`——候选身份、进化门槛、`paper_parameter_versions`
  版本追加、outbox 与审计全部沿用，本层只做"复核 + 委托 + 原样透传拒绝"。
  写前复核两颗钉子：① 实时参数基线与调用方声明的基线不一致 → `stale_base`，一个字节不写；
  ② 候选要写的值必须等于授权请求要写的值 → 否则 `candidate_does_not_match_authorized_change`。
- **端到端已证明**（`test_ai_adoption_writer`，6 项，跑在真实 `PT.init_db()` 库上）：
  有效凭据 + 范围内 + 候选一致 → `paper_accounts.params` 真的变了、`paper_parameter_versions`
  追加一条；`stale_base`、候选不一致、无凭据、超日额度四种情形下**参数一个字节都没变**。
- **既有门禁不被绕过**：写入者只允许"当前生效因子权重、单因子变化 ≤3pp"的自动应用。
  因此权重类目标可以在授权范围内连续自动落地（无需逐次点击，受日额度约束），
  而条件类覆盖（`entry_policy`）仍会被写入者拒绝并原样上报——那是既有规则，不是本层的取舍。

**未完成**：没有对外的"一键采用"HTTP 接口。采用入口目前是库内函数
（`ai_adoption_writer.adopt_candidate`）+ 已签发的凭据；是否把采用动作也暴露成 API
属于产品决定，不宜在研究链路尚未上线前先开这个口子。

## 验证记录

- 后端全量：`python -m unittest discover -s backend -p "test_*.py"` → **6932 通过**
  （skipped=10）。`ruff check backend` 全绿。
- 前端：`node --test tests/*.test.mjs` → **229 通过**；`node build.mjs` 幂等
  （连续两次构建输出一致），`frontend/dist` 已同步。
- 一次全量运行里 `test_strategy_roster` 的
  `test_daily_policy_can_be_reauthorised_for_v2_bounds` 曾单独失败一次；随后
  **四次复跑全绿**（单模块 2 次、组合 1 次、`-f` 全量 1 次），且该模块与新代码无调用关系，
  判定为未复现的偶发，未按"已修复"记账。
- 新增测试文件：`test_ai_research_agent`（21）、`test_ai_capability`（19）、
  `test_ai_capability_store`（13）、`test_ai_advice_service`（34）、
  `test_ai_adoption_writer`（6，真实 `PT.init_db()` 库）、`test_api_ai_capabilities`（9）、
  `test_research_metrics`（6）、`test_research_worker`（12）；前端
  `tests/ai-capability.test.mjs`（5）。

## 仍缺的只有环境前提，不是代码

| 缺口 | 依赖什么 |
|---|---|
| 真实模型的一次端到端调用（S1 验收） | 部署实例上要有可用 provider（当前 0 行配置） |
| 三个 p95 目标实测（S2 验收） | 同上 + 本分支需部署（当前停在 `11bb144`） |
| 真实环境里的凭据签发与自动采用 | 同上；且签发是运维动作，需人工发起 |

`research_worker provider-check --slot ai1` 与 `research_worker status` 的 `provider` 段
就是为了把这三件事变成"一条命令即可判定"，而不是继续靠试错。

## 一句话现状

研究链路（事件 → 任务 → 工具 → 模型判断 → 结构化结论 → 可续跑）已闭环；
窗口、估值、原子性、写锁、身份、健康维度接线、决策归因落库这些"结论可信度"的前置条件已修；
采用链路从"凭据校验 → 签发/撤销 → 唯一写入者落地 → 预算累计"全部打通并有端到端用例，
默认仍是 ADVISORY（没有凭据就不采用）。
**唯一无法在本地闭合的，是部署环境既没上线本分支、也没有配置任何模型 provider。**
