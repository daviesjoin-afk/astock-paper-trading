# V2 验收对照表

把路线图的每个验收项落到**可复现的命令与证据**上。现状基线：Rust 158 / Python 研究 58 / 浏览器 134 / 冒烟全链路；CI 8 个作业（含 `verify-report`，上传验证报告与归档清单）。

## 一键证据

```bash
bash scripts/verify_all.sh --report /tmp/verify.json
```

输出各套件 `pass` 与耗时，`/tmp/verify.json` 为机器可读汇总；失败时仍写出且 `ok=false`，退出 1。`--only`/`--archive`/`--report` 组合可用，未知或空选择退出 2。

## 分阶段对照

| 路线图验收 | 验证方式 | 期望 | 现状 |
| --- | --- | --- | --- |
| 10 万元自动划分为 A/B/预留 | `cargo test paper_account_`；账户页「双槽」；`GET /v2/paper-accounts/{id}` | A/B 各 45,000，预留 10,000（整数分拆分） | ✅ |
| 两策略不重复占用现金、不互相卖仓 | 审计第 2 项 `capital_conservation`、第 3 项 `order_fill_link`；并发用例 | 并发同信号只成交一次；现金恒等式成立 | ✅ |
| 重启后账本一致 | `bash scripts/smoke.sh`（重启段重开同一 SQLite） | 决策/成交/摘要完全一致 | ✅ |
| 自动完成买卖/拒绝/暂停/继续/重放 | 冒烟会话段 + `advance_paper_session_to` 用例；账户推进按钮 | 同一输入同一结果，暂停后不推进 | ✅ |
| 估值/净值/回撤/费用 | `GET .../valuation`、`.../equity-series`、`.../performance`；缺数据返回 `unavailable` | 有审核价格才计算，否则明确拒绝 | ✅ 机制 |
| 数据缺失不伪造收益 | 所有估值路径 fail-closed；`price_set_coverage` 报缺口 | `no_reviewed_price_set` | ✅ |
| 资金调整可解释、可审计 | `GET .../allocation-proposal`（含理由/摘要/版本切换标注）、`.../rebalances`、审计 `rebalance_chain` | 每次划拨有前后权重、金额、摘要 | ✅ |
| 调仓不绕过 T+1/现金/风险底线 | 划拨只移现金；订单仍走 `decide` 与 `risk_violation`；测试覆盖 | 越过边界返回规则错误 | ✅ |
| 候选生成/样本外/影子/晋级/回退有证据 | `shadow_candidates.py --decide --record-event`；`/v2/evolution/events`；`signal-diff`；`performance/segments` | 事件带理由、关联实验、摘要 | ✅ |
| 禁止未来数据 | 证据契约校验（缺/过期/方向不符即拒）；冒烟含未来日期拒绝 | 未来或缺失证据一律拒绝 | ✅ |
| 不以单次收益晋级 | ADR-012 门槛（样本天数/影子天数/优势/回撤）+ 滚动窗口 | 未达门槛判 `rejected`/`insufficient_evidence` | ✅ |
| 运行状态/预算/成交/净值/进化可查 | 概览状态条、账户页、进化历史（过滤/分页/差异/绩效）、实验信息（价格集合/覆盖） | 全部只读展示，失败显示 `—` | ✅ |
| 安全启动/暂停/停止模拟 | 会话按钮（启动/暂停/继续/停止）+ 账户推进；`verify_all` | 状态机拒绝非法动作 | ✅ |
| 全部交易为模拟 | 代码无券商接口；`/v2/health` 返回 `paper_only: true` | 无实盘通道 | ✅ |
| **真实审核历史行情数据** | `v2/data/price-sets/` 目录 + `publish_price_set.sh` + 覆盖核对 | 目录中需有人工审核数据 | ⏳ **外部依赖** |

## 数据接入后的收尾步骤

1. `bash scripts/publish_price_set.sh --input prices.csv --set-id <id> --pack <pack> --coverage-start <d> --coverage-end <d> --symbols <syms> --trust reviewed`
2. `GET /v2/price-sets/coverage?...` 确认缺口为空；
3. 账户页确认估值由 `unavailable` 变为 `valued`，绩效行出现数字；
4. `python scripts/weekly_review.py --days-from-pack <pack> --symbol <sym>` 跑滚动复核；
5. 归档：`db_tool.py backup` + `events/export` + `verify_archive.py --dir`。

## 未覆盖/明确不做

- 实盘券商接入、自动下单；
- 年化/夏普等未定义指标；
- 守护进程式调度（由外部 cron/task 调用脚本）；
- 就地 schema 迁移（旧库按设计拒绝）。
