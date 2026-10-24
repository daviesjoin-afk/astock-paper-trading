# 源码效率与缺陷审查

## 目标与完成标准

基于 `codex/large-file-split @ 2983b15` 审查受版本管理的源码。全库做结构和风险模式扫描，对线上调用路径、取数/缓存、交易与研究、前端异步和部署脚本深入核对。交付有具体位置、触发条件、影响、复现证据和修复建议的清单；性能建议区分实测与待测。

审查阶段不改业务实现；修复阶段按报告给出的最小修复方向逐项落地，并为每一项补一条会失败的回归测试。

## 范围

- `backend/`：生产 Python、测试和数据库脚本。
- `frontend/src/`：手写前端与请求/渲染逻辑；测试验证已有约束。
- `deploy/`、`work/` 等：部署与辅助脚本。
- 构建产物、第三方代码及历史归档单独计数，不当作手写生产源码。

## 阶段

- [x] 完成全库清单、语法/质量检查和热点风险扫描。
- [x] 核对高风险调用链，排除已有防护和不在运行路径的误报。
- [x] 用隔离样本复现缺陷、测量可替换低效写法，复跑相关现有测试。
- [x] 汇总按优先级排列的报告及覆盖范围，确认业务源码未变。
- [x] 修复 B1 数据目录隔离（先修根路径，再修消费者）。
- [x] 修复 B2 收盘跟踪规则，并阻止盘中写入收盘收益。
- [x] 修复 B5 只读页面的结构写入、连接未关闭。
- [x] 修复 B3 异步路由内的同步数据库事务。
- [x] 修复 B4 前端超时未覆盖正文读取。
- [x] 修复 B6 过期快照冒充"今日共振"。
- [x] 修复 E1 扫描接口重复历史计算。
- [x] E2 先测量再替换证券范围逐行判断（46.1ms → 9.5ms，规则不复制）。
- [x] B2b 提供基于原始报价证据的污染行修正工具（dry-run 默认，按旧值比对写入）。
- [x] 完结：探针复跑、ruff、后端全量、前端全量、dist 重建。

## 证据

- 审查阶段：原始清单、探针和日志保存在 `.codex/source-audit/`；结论以当时源码及实际复现为准。
- 修复阶段：三个探针在修复后重跑，结果覆盖写入 `scripts/source-audit-20261023/*-result.json`；回归测试为
  `backend/test_data_dir_isolation.py`、`backend/test_selection_tracking_close_rules.py`、
  `backend/test_settings_async_isolation.py`、`backend/test_linkage_freshness.py`、
  `backend/test_scanner_history_reuse.py`，以及 `frontend/tests/api-request-contract.test.mjs` 新增 6 项。

## 交付

- 报告（含修复记录）：`docs/SOURCE_EFFICIENCY_BUG_AUDIT_2026-10-23.md`。
- 可重跑探针及结果：`scripts/source-audit-20261023/`。
- 审查结论：6 类功能缺陷、1 项实测性能问题；修复前后端 146 项、前端 239 项现有测试通过。
- 修复结果：6 类缺陷 + E1/E2 两项性能问题全部关闭；`ruff` 通过，后端 6092 项 / 前端 245 项通过，
  `frontend/dist` 已重建，探针对应指标逐项复现为已修复。
- E2：新增微基准 `scripts/source-audit-20261023/scope-probe.py`（46.1ms → 9.5ms，掩码逐行一致），
  回归 `backend/test_scanner_scope_mask.py`。
- B2b：历史污染修正工具 `backend/selection_tracking_repair.py`（默认 dry-run、按旧值比对写入、
  只接受收盘政策验证过的原始报价），回归 `backend/test_selection_tracking_repair.py`；
  当前工作树跟踪库为空，本地没有可修数据。
- 限制：没有归档证据的历史行仍然无法修正（刻意不猜）；工作树 `selection_tracking.db`
  在审查期被探针写过且无事前备份，来源仍待核对。
