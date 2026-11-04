# 主线整理记录（2026-11-04）

本文记录本轮历史 PR 清理与 master 主线统一的**实际执行结果**。
只涉及 PR 状态、分支依赖与主线同步；未修改模拟交易规则、策略参数、
仓位、风控阈值、数据库结构或生产账本，也未部署服务器。

## 1. 基线

| 项 | SHA |
| --- | --- |
| 本轮起始 master | `79d4390bf366dd9e19e210e350db751d07b89e2d` |
| 本轮最终 master | `0180c4ecc8d518fbc0f54fd202f6471ffcc5d5af`（含本记录自身的合并 #335） |

起始 master 上开放 4 个 PR（#179、#271、#333、#334），其中 #334 的 base
指向 #333 的分支，必须先处理依赖。

## 2. 关闭的 PR

### PR #179 — docs(release): rewrite v2.0.0 notes around practical outcomes

**结论：分支已过期，不合并。**

核实依据：

- `docs/RELEASE-v2.0.0.md` 在当前 master 上**不存在**；发布文档体系已演进为
  v0.x 版本线与 `docs/RELEASE-v0.20.0.md`。
- `CHANGELOG.md` 当前主线无 v2.0.0 条目（最新为 v0.20.0）。
- 全量 diff 仅涉及 `CHANGELOG.md` 与 `docs/RELEASE-v2.0.0.md`（+134/−397），
  无代码、配置、测试或数据变更。

按 not_planned 关闭，分支 `docs/v2-release-notes-outcomes` 保留。
**注：** GitHub 当前 API 对已关闭 PR 不返回 issue 级 `state_reason` 字段，
关闭原因以上述 PR 评论为准。

### PR #271 — feat(strategy): 三策略收敛 + 初始预算 20/50/30 + 自进化可调

**结论：已被主线取代，不合并、不提取子集。**

以 merge-base `4292700` 为基准取 PR 真实改动集（44 文件，+4714/−113），
逐文件比对**函数集合**而不是只看文件是否存在：

| 功能 | master 对应模块 | 结果 |
| --- | --- | --- |
| 三策略名册 | `backend/strategy_roster.py`（382 行） | 函数集合完全覆盖 |
| 20/50/30 预算 | 同上 `budget_share=0.20/0.50/0.30` | 一致 |
| 策略生命周期 | `backend/strategy_lifecycle.py`（455 行，17 函数） | 主线独立实现，PR 未含此模块 |
| 做T配对分析 | `backend/intraday_t_pair_report.py`（387 行） | PR 独有仅私有工具 `_number` |
| 主力确认 | `backend/main_force_confirmation.py`（110 行） | PR 独有仅 `_number` |
| 竞价影子研究 | `backend/auction_factor_shadow.py`（312 行） | master 的 `_day_cross_section` 多 `cutoff` 参数（PIT 更严格） |

其他差异：

- `scripts/three_strategy_switch.py`：master 18 函数（含 `write_budget_weights`、
  `authorize_daily_policy`、`verify_budget`），比 PR 分支版本更完整。
- `Dockerfile` 的 `COPY scripts`：master 第 102 行已存在，生产容器 `/app/scripts` 实测可见。
- `monday_preflight.py`、`strategy_portfolio_audit.py`：函数集合与 PR 完全一致。
- `test_strategy_roster.py`：master 30 个测试函数，PR 为 1 个。
- 命名差异（「激进做T/稳健波段/保守防守」→「短线日内做T/趋势波段优选/三日策略」）
  是后续产品定名，不是功能缺失。

按 not_planned 关闭，分支 `codex/three-strategy-redesign` 保留作为审计记录。

## 3. 合并的 PR

### PR #333 — perf(tests): 分片数按可用内存收敛并重新校准成本档案

| 项 | 值 |
| --- | --- |
| base | `master` |
| head（合并前最后确认） | `d14319b41409226c61f0c849cc7b6b5d0fcff488` |
| 实际 merge SHA | `25d8703eacc4c026c157a54641a0282b20521967` |
| 合并方式 | squash（`--match-head-commit` 校验 exact head） |

核实项：

- base 为 master，head 未变化，8/8 检查通过（含两次 Security Leak Scan）。
- 无审查意见、无未解决审查线程。
- `scripts/run_tests_parallel.py`：新增 `available_memory_mib()` /
  `memory_limited_shards()` / `_oom_hint()`，按可用内存下调分片数并把 OOM
  崩溃与断言失败区分开；CI `docker-smoke` 超时 15→20 分钟（有实测依据）。
- `scripts/test_costs.json`：`--calibrate` 重新生成的分片权重。
- **决策规则数学语义**：`backend/decision_rules.py` 将 `_safe` 的
  `pd.isna(v)` 改为 `math.isnan(v)`。用 21 个用例穷举比对（`0/±0/±1/1.5/
  NaN/±inf/True/False/None/str/list/dict/np.int64/np.float64/np.nan/np.inf/
  np.bool_`），新旧实现**逐例输出一致，0 处差异**，且不再在导入期拉入 pandas。

### PR #334 — perf(startup): 按入口延迟交易栈的 pandas 加载

| 项 | 值 |
| --- | --- |
| 原 base | `codex/regression-harness-memory` @ `d14319b4` |
| 新 base | `master` |
| rebase 前 head | `e957a05f7ce77316da212f6a21d55f408e5375a2` |
| rebase 后 head | `78032d84a394b8be872bb71f62f5464d257a9157` |
| 实际 merge SHA | `837b8fcde50b61174a370da272fc41283068115c` |
| 合并方式 | squash（`--match-head-commit` 校验 exact head） |

执行过程：

1. 先完成 #333 并重新获取 master（`25d8703e`）。
2. 确认 `codex/lazy-trading-stack` 工作区干净（仅本地未跟踪 `plans/*`）。
3. `git rebase --onto origin/master d14319b4 codex/lazy-trading-stack`，
   无冲突。
4. `git log --oneline origin/master..HEAD` 只剩 PR 自身 **3 个提交**：
   `perf(startup)` / `chore(evidence)` / `test(marketdata)`。
5. `git diff --stat origin/master...HEAD` 25 文件 +380/−104；
   `git diff --check` 无空白错误；**未**携带 #333 的测试分片、成本档案或
   决策规则修改。
6. `--force-with-lease` 推送。
7. PR base 改为 master。
8. 在**新 base 上重跑完整 CI**（未沿用旧 SHA 结果）：8/8 通过。
9. 回归覆盖：`backend/test_paper_lazy_imports.py` 7 项、
   `backend/test_marketdata_feeds.py`、后端全量四分发。

本地实测收益：`import paper_trading` 约 0.57 s，导入期 `pandas` 未加载
（`'pandas' in sys.modules` 为 False），兼容别名 `_snapshot_safe`、
`DECISION_SNAPSHOT_VERSION` 仍可访问。

## 4. 本轮 exact-head CI 结果

| PR | head | 结果 |
| --- | --- | --- |
| #333 | `d14319b4` | 8/8 通过 |
| #334 | `78032d84` | 8/8 通过 |

两者均包含 `tests`、`docker-smoke`、`browser-e2e (chromium)`、`frontend`、
`quality`、`syntax` 与两次 `security-leak-scan`。

## 5. 主线核验

```
git rev-parse HEAD            -> 0180c4ecc8d518fbc0f54fd202f6471ffcc5d5af
git rev-parse origin/master   -> 0180c4ecc8d518fbc0f54fd202f6471ffcc5d5af
```

- 工作区干净（仅本地未跟踪的 `plans/*.md`，不属于仓库发布内容）。
- `#333`、`#334` 各只出现一次，无重复合并。
- 无 `<<<<<<<` / `>>>>>>>` 冲突残留。
- `#179`、`#271` 均为 CLOSED 且未合并。
- 当前开放 PR 数：**0**。

## 6. 尚未清理的分支

远端仍有 **42** 个分支。其中 7 个已完全合入 master，但其是否仍被其他工作引用
尚未逐一确认，因此**本轮未删除任何分支**。

本轮相关分支状态：

| 分支 | 状态 |
| --- | --- |
| `codex/regression-harness-memory` | 合并 #333 时由 GitHub 自动删除 |
| `codex/lazy-trading-stack` | 合并 #334 时由 GitHub 自动删除 |
| `docs/v2-release-notes-outcomes` | 保留（#179 审计记录） |
| `codex/three-strategy-redesign` | 保留（#271 审计记录） |

## 7. 尚未确认的生产部署版本

生产服务器 `/opt/astock-codex` 当前 `.deploy-source.json`：

```
commit=79d4390bf366dd9e19e210e350db751d07b89e2d
image=a5df3316（镜像摘要前 8 位）
```

即生产落后本轮最终 master（`837b8fc`）两个提交：`#333`（测试分片与 CI 超时）
与 `#334`（启动期 pandas 惰性加载）。按本轮约束**未部署、未重启容器**，
因此生产版本与 master 的差异需要在下一阶段显式决策。

## 8. 本轮约束遵守情况

| 约束 | 情况 |
| --- | --- |
| 不修改交易规则/策略参数/仓位/风控阈值 | 未修改 |
| 不修改数据库结构与生产账本 | 未修改 |
| 不部署服务器、不重启容器 | 未执行 |
| 不删除未经核实的代码 | 未删除任何文件 |
| 合并前检查 exact-head SHA 与 CI | #333/#334 均用 `--match-head-commit` |
| 仅在 master 上合并 | 两个 PR base 均为 master |
