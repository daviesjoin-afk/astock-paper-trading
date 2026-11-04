# P1-04 自进化编排审计

日期：2026-11-04  
审计基线：`dad42e5a8924f5465c86dbf00efa20115305c597`  
范围：`adaptive_runner` / `adaptive_engine` / `ai_review_service` / `self_evolution` / `evolution_loop` / `evolution_loop_runner` / Cron 与 host launcher。

## 1. 结论

**保留两个业务阶段，统一 orchestration control plane；不合并算法、不删除研究能力。**

当前结构并不是两套相同的“自进化算法”：

- `adaptive_engine.run_learning_cycle` 负责生产盘后证据与研究产物。
- `ai_review_service` 负责双 AI / 单 AI 审核，产生结构化审核结果。
- `self_evolution` 负责 reviewer 语义证据、真实 reward 归因、参数候选计算与生命周期。
- `evolution_loop` 负责把既有证据送入 observe/evaluate/mutate/validate/apply 的可恢复代际流程。
- `evolution_activation` 负责显式候选激活、CAS 与激活历史。

把它们合成一个“统一进化函数”会混淆研究、证据、候选和生效权限；这是错误的合并边界。

## 2. 当前真实数据流

```text
adaptive_runner (cron / retry)
  └─ adaptive_engine.run_learning_cycle
       ├─ market profile / alpha / dataset / reward
       ├─ reward attribution
       ├─ bandit / risk / selection evaluation
       └─ ai_review_service.run_ai_review
            ├─ dual_ai_tuning_runs (audit)
            └─ self_evolution.track_run
                 └─ evolution_tracking (learning evidence)

evolution_loop_runner (cron / retry)
  └─ heavy_job_lease
       └─ evolution_loop.run_loop
            └─ ProductionBackend
                 ├─ observe   -> self_evolution.get_performance_metrics
                 ├─ evaluate  -> intelligence score
                 ├─ mutate    -> self_evolution.auto_evolve_if_needed
                 ├─ validate  -> evolution_activation.validate_candidate
                 └─ apply     -> record pending activation only

operator / explicit approval
  └─ evolution_activation.activate_params_candidate
       └─ CAS active pointer + history + audit
```

关键边界：

- AI consensus 不是参数生效证据。
- `evolution_tracking.applied` 只有真实 apply 后才能置位；有提案不等于已应用。
- `ProductionBackend.apply` 不激活参数，返回 `applied_to_runtime=False` / `pending_activation=True`。
- 只有显式激活入口能推进 active 参数指针。

## 3. 调度与资源事实

`deploy/astock-codex.cron` 当前窗口：

| 任务 | 时间 |
|---|---|
| adaptive close | 15:35 |
| adaptive close retry | 15:55 / 16:15 |
| evolution loop | 16:45 / 17:05 / 17:35 |

`adaptive_runner` 和 `evolution_loop_runner` 都使用 `resource_guard.heavy_job_lease`。该 lease 使用全局重型任务锁，不按 job name 建立独立锁。因此两个任务是**串行准入**，不是并行重型 lane。竞争失败应当返回 deferred，由后续 Cron 窗口续跑。

Host launcher `deploy/run_evolution_loop.sh` 另有自己的 `flock -w 180`，用于防止多个 evolution-loop 进程同时运行。它不替代容器内的全局资源准入。

## 4. 本轮发现与修复

### 4.1 tracking persistence failure 被吞掉

旧行为：

- `ai_review_service._write_audit` 写入 `dual_ai_tuning_runs`。
- 随后调用 `self_evolution.track_run`。
- tracking 写入异常被 `except Exception: pass` 吞掉。
- 审核主结果继续返回，adaptive learning cycle 也可能标记 completed。
- 下游 evolution loop 无法区分“没有新样本”与“本次 tracking 写入失败”。

本轮修复：

- 审核结果增加 `evolution_tracking.status`、`tracking_id`、`error_type`。
- 状态透过 `dual_ai_tuner` 兼容 adapter 返回。
- `adaptive_runs.detail.dual_ai_tuning` 持久化该状态。
- tracking 仍是 best-effort；追踪失败不把成功的 AI 审核伪装成失败，也不阻断其他学习阶段。

限制：本轮只增加可见性，没有自动重建缺失 tracking。不能直接用事后当前参数版本补写旧 tracking，否则会伪造审核当时的 `evolution_params_id`。后续如要 replay，必须先有不可变的参数版本快照或事务性 outbox。

### 4.2 resource lease 之前发生数据库写入

旧行为：

```text
_connect
  -> ensure_loop_schema
  -> self_evolution.ensure_schema
  -> self_evolution.init_params
heavy_job_lease
  -> run_loop
```

被资源门禁拒绝时，数据库 schema 和默认参数初始化已经执行。

本轮修复：

- `--daily` 先通过只读 SQLite 连接探测是否已完成。
- 未完成才申请全局 heavy lease。
- 获得 lease 后才执行 `_connect`、schema 初始化和参数初始化。
- 资源许可被拒绝时，不创建、不初始化学习数据库。
- 获得 lease 后再次检查 daily completion，关闭只读探测与 lease 之间的竞态。

### 4.3 相同待激活候选重复生成

旧行为：

- `evolution_loop` 每个交易日可触发一次。
- `self_evolution.should_evolve` 的 cooldown 是 1 小时。
- 如果 active 参数未变化、最近 20 条 tracking 仍满足相同阈值，次日可能再次产生相同参数。
- `evolution_activation.create_candidate` 本身只插入候选；全局 `self_evolution.evolve` 之前没有复用等价待激活候选。

本轮修复：

- 用 `find_equivalent_candidate` 按 scope + active base + 参数内容查找已有候选。
- 找到等价候选时不再插入第二条。
- `ProductionBackend.mutate` 将它识别为 reused candidate，并继续校验/记录待激活身份。
- active 指针不变，绝不把候选写成已生效。

## 5. 尚未决定的设计问题

### 5.1 tracking 样本是否有时间有效期

`self_evolution.get_performance_metrics(window=20)` 使用按 id 排序的最近 20 条 tracking，不限制创建时间。它可能是刻意设计的滚动样本窗口；也可能使长期没有新审核时，旧样本继续触发候选。

不能凭感觉加 7 天/30 天门槛。需要先决定：

- 样本按条数还是按交易日构成窗口；
- 无新数据时是否允许旧证据继续使用；
- 冷启动与样本不足的状态如何表达；
- reward attribution 的成熟等待期如何与 evidence expiry 协调。

### 5.2 time-budget 不是单代超时

`run_evolution_loop.sh` 当前只传 `--daily`，没有传 `--time-budget`。CLI 默认 generations=1。并且 `run_loop` 的 time-budget 主要在代际边界检查，不会中断正在执行的单代 stage。

因此不能把当前 time-budget 描述为可靠的单轮墙钟上限。若需要真正的 stage timeout，应设计可恢复 checkpoint + 有界 stage lease，而不是只加一个 CLI 数字。

### 5.3 apply 命名

`apply` 是持久 stage key，兼容历史状态数据。本阶段实际是确认/记录待激活候选，不推进 runtime active pointer。保留数据库 stage key；可调整 UI 文案和文档，明确显示“待人工激活”。

## 6. 推荐目标架构

不合并算法，统一运行管理：

1. 一个 orchestration control plane 管理任务定义、依赖、触发、资源预算、lease、retry、超时和运行状态。
2. 两个独立 capability：`market_learning`（生产证据）与 `candidate_evolution`（消费证据并生成候选）。
3. 持久化父子关联：`adaptive_run_id → ai_review_run_id → evolution_tracking_id → generation_id → candidate_id`。
4. 每个阶段有独立 status 与 error；上游非关键阶段失败不污染其他阶段，但依赖它的下游阶段必须明确 blocked / waiting。
5. 候选生成采用 active base + params digest + evidence identity 幂等；激活仍由独立人工授权边界负责。
6. Dashboard 展示“证据生产、证据追踪、代际候选、人工激活”四种不同状态，不用一个 completed 标签覆盖整条链路。

## 7. 验收边界

本轮代码修改不更改交易规则、参数边界、AI 结论、Cron 时间、生产数据库 schema 或 active 参数指针。回归必须证明：

- tracking 写入失败可见，但审核主流程仍可完成；
- tracking 成功时返回真实持久化 ID；
- single-review 不冒充 evolution tracking；
- 资源许可拒绝时不执行 schema / 参数初始化；
- daily completion 的只读探测不执行 DDL；
- 相同 active base 下重复运行不会重复堆叠等价候选；
- 待激活候选仍不推进 active 参数指针。
