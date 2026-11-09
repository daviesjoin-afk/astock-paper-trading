# V2 现状（截至 2026-11-08，master `c339a45`）

一页看清“系统现在能做什么、证据在哪、还缺什么”。

## 一句话

纯模拟、确定性账本、双策略资金池、受约束自进化，全部机制与证据已就位；唯一未闭合的是**真实审核行情数据**（外部依赖），缺数据时全链路明确拒绝而非估算。

## 验证基线

| 项 | 当前 |
| --- | --- |
| Rust 测试 | 158 |
| Python 研究测试 | 58 |
| Playwright | 134（24 skipped） |
| 端到端冒烟 | 全链路通过（含脚本自身行为断言） |
| CI | 8 个作业全绿（含 `verify-report`，上传验证报告 + 归档清单） |
| Schema | v15（旧库按设计拒绝，需重建） |
| 一键验证 | `bash scripts/verify_all.sh [--fast] [--only LIST] [--archive DIR] [--report F]` |

## 能力清单（每项都有测试或审计项）

### 账户与交易
- 单账户 + A/B 双槽 + 准备金；权重 bps、整数分拆分、守恒。
- 确定性会话（created/running/paused/stopped/completed），单步/按日/账户级推进；非法动作拒绝。
- 订单：证据、T+1、整手、预算、集中度、现金底线；拒绝原因显式存储。
- 推进预览（只读）：`GET /v2/paper-accounts/{id}/advance/preview?day=`。

### 审计（9 项）
`budget_split` · `fills_replay` · `capital_conservation` · `order_fill_link` · `fill_matches_outcome` · `session_link` · `session_signals` · `rebalance_chain` · `switch_chain`
其中 fill 与 switch_chain 有篡改测试证明非恒真。

### 数据
- `reviewed` 价格集合注册表（`data/price-sets/*.json`，`ASTOCK_PRICE_SETS_DIR` 可覆盖）。
- 生成/安装：`make_price_set.py` + `publish_price_set.sh`（防覆盖、dry-run、退出码）。
- 覆盖日与缺口：`/v2/price-sets/days`、`/v2/price-sets/coverage`。
- 输入模板：`data/price-set-examples/`（标注 synthetic）。
- 全流程文档：`docs/DATA_ONBOARDING.md`。

### 估值 / 绩效
- 单点估值、净值序列、回撤、收益/费用/笔数；任何缺口 → `unavailable`，不补零。
- 账户页与实验绩效面板同口径（ADR-016）。

### 资金
- 提案：边界、准备金、步长、理由、摘要；窗口内版本切换 → 理由追加并重算摘要（界面有徽标）。
- 执行：二次确认（划拨金额 + 划拨后预算），后端仍硬校验。
- 敞口（成本口径）、划拨链审计。

### 自进化
- 候选生成（有界网格 + 规范摘要）→ 影子实验 → 信号差异 → 绩效/滚动窗口 → 治理决策 → 事件（关联实验、过滤、导出、离线复核）。
- 策略版本切换：追加链、幂等、时间单调、前置校验（会话/订单时序）、视图与审计、控制台表单。
- 拒绝优于猜测：证据不足一律 `insufficient_evidence`。

### 运维
- `/v2/health`：schema、账户/会话/事件计数、价格集合信任拆分。
- 备份/恢复/校验：`db_tool`（在线 API、拒绝覆盖、损坏即失败）。
- 归档：`verify_archive`（单对 / 整目录 / 侧车 / 合并 manifest）。
- 调度：`daily_advance.sh`（dry-run 调预览）、`weekly_review.py`（三种窗口来源）。
- 一键验证 + JSON 报告；CI 工件留存。

## 唯一未闭合项

**真实审核行情数据。** 现状：

- 目录中没有 reviewed 集合，只有测试用 synthetic；
- 因此估值/绩效/资金提案在真实运行中均返回 `unavailable`；
- 落地页会给出模板与发布脚本指引，并提供「去核对覆盖」入口；
- 接入步骤见 `docs/DATA_ONBOARDING.md`（CSV → 发布 → 覆盖核对 → 验证 → 归档）。

这不是代码缺口：机制、校验、失败语义、文档与测试都已就绪，缺的是合规数据本身。

## 立刻可做的验证

```bash
cd v2
bash scripts/verify_all.sh --report /tmp/verify.json    # 全量
python scripts/verify_archive.py --dir /backup          # 归档（若有）
curl -fsS http://127.0.0.1:8765/v2/health                # 运行中实例
```

## 建议的下一步（按价值排序）

1. 提供合规样例 CSV（或指定数据源），走 `DATA_ONBOARDING` 全链路演练；
2. 归档 manifest 只读展示（需新增读接口）；
3. 定期 ADR/实现一致性复核（已有两轮）；
4. 控制台细节：概览与账户页状态信息进一步整合。
