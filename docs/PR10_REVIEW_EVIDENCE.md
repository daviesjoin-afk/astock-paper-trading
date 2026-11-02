# PR-10 复审修复证据（2026-11-02）

状态：交付完成、验收未通过。现有 A—E 五包保留 Draft，不合并、不部署。表引用分散度下降尚未满足，不移交 PR-11。

## 同口径逐表对照

基线为冻结提交 `52eb50d390a05f88b451cf31f12bfb1b3df59391`（A 开始前，生产源码与 master `8e3c22d` 一致）。前后都使用当前同一个扫描器。三列均只统计 `backend/*.py` 的 runtime 角色，排除 test/seed/migration/fixture；写方按真实写目标，读方按 SQL FROM/JOIN（DELETE 首表不算读，但子查询保留）。全部引用为表名整词文本引用，包含注释/文档字符串，不能等同真实 SQL 读者。

| 核心表 | 写入模块 前→后 | 读取模块 前→后 | 全部引用模块 前→后 |
| --- | --- | --- | --- |
| `paper_orders` | 6→1 | 33→35 | 45→46 |
| `paper_signals` | 4→1 | 13→13 | 24→23 |
| `paper_accounts` | 9→1 | 26→26 | 40→41 |
| `paper_cycles` | 2→1 | 12→12 | 19→19 |
| `paper_fills` | 1→1 | 16→16 | 18→18 |
| `paper_positions` | 1→1 | 4→4 | 16→16 |
| `paper_position_lots` | 1→1 | 11→11 | 15→15 |
| `paper_capital_reservations` | 1→1 | 3→3 | 10→10 |
| `paper_risk_decisions` | 1→1 | 6→6 | 12→12 |
| `paper_audit` | 1→1 | 5→5 | 9→9 |

写方收口得到验证，但全部引用总数 208→209，没有达到原定下降目标。原预算 `paper_table_fanout_max` 采用另一口径（包含维护模块、表集合也不同），不能将该指标 51→50 当成相对 A 之前的下降证据。

## import 跨域边

同一 `production_paths/domain_of` 分类与 foundation 排除规则；629→636。新增七条已审核设计成本，删除 0 条。该数独立于逐表引用分散度。

| 新增来源 | 目标 |
| --- | --- |
| `adaptive_engine` | `paper_account_config` |
| `adaptive_risk` | `paper_account_config` |
| `adaptive_selection` | `paper_account_config` |
| `adaptive_selection_compat` | `paper_account_config` |
| `evolution_apply` | `paper_account_config` |
| `execution_dispatch` | `paper_order_ledger` |
| `execution_verification` | `paper_order_ledger` |

`HEAD_BUDGET=618` 是历史基值。`FEATURE_ALLOWANCE` 从 A 前 11 到 C 后 18，提高了有效上限；这是已审核例外，不是预算未放宽。本次修复不再扩大该额度、不改域分类。完整新增/删除集合及前后模块名单见 `backend/ledger_fanout_evidence.json`。

## 冻结清单与计数对账

| 口径 | 数量 |
| --- | --- |
| 原冻结 module::function 组 | 57 |
| 原冻结按目标表展开的稳定操作 ID | 68（A 18 / B 22 / C 20 / D 8） |
| 旧 E 树 writer 函数组数 | 71 |
| 修复后 writer 函数组数 | 70（A 19 / B 24 / C 12 / D 15；按当前接口登记的包分类） |
| 原→新 SQL 语句模板数 | 118→75 |

SQL 模板数按源码中的完整静态/动态 SQL 表达式去掉拼接内部重复片段，不是运行时执行次数或业务操作数。原始跨表函数按表展开，多条 SQL 可归同一操作，多个原操作也可复用新接口。旧 §18.1 的 19+24+16+7=66 没有对应冻结清单，应撤回；不能由 66、71 的差额推定漏了五项。

完整原始元数据保存在 `backend/ledger_frozen_baseline.json`；68 个原始 ID 一对一映射在 `backend/ledger_migration_map.json`。映射保留原模块/函数/目标表/事务，列出新接口、源码引用/依赖绑定位置、关联行为测试与迁移状态；70 个当前 writer 接口均被映射覆盖。静态映射不单独证明每条调用方所有分支均已动态重演；测试覆盖的层次以具体用例为准。

| 原始稳定操作 ID | 新 owner::接口 | 行为回归模块 |
| --- | --- | --- |
| `PR10:adaptive_engine::_restore_paper_account:paper_accounts` | `paper_account_ledger::write_params` | `test_paper_account_ledger`<br>`test_paper_repository` |
| `PR10:adaptive_risk::_finish_rollback:paper_accounts` | `paper_account_ledger::write_params` | `test_paper_account_ledger` |
| `PR10:adaptive_risk::apply_candidate:paper_accounts` | `paper_account_ledger::write_params` | `test_paper_account_ledger` |
| `PR10:adaptive_selection::_finish_rollback:paper_accounts` | `paper_account_ledger::write_params` | `test_paper_account_ledger`<br>`test_ai_adoption_writer` |
| `PR10:adaptive_selection::apply_candidate:paper_accounts` | `paper_account_ledger::write_params` | `test_paper_account_ledger`<br>`test_ai_adoption_writer` |
| `PR10:adaptive_selection_compat::migrate_legacy_selection_units:paper_accounts` | `paper_account_ledger::write_params` | `test_legacy_factor_unit_migration` |
| `PR10:evolution_apply::write_account_params:paper_accounts` | `paper_account_ledger::write_params` | `test_evolution_apply` |
| `PR10:execution_dispatch::_retire_for_retry:paper_orders` | `paper_order_ledger::supersede_order` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::_retire_for_retry:paper_signals` | `signal_service::reopen_for_retry`<br>`signal_service::expire_after_order_retry` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::_terminate:paper_orders` | `paper_order_ledger::terminate_order` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::_terminate:paper_signals` | `signal_service::terminate_for_dispatch` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::_write_signal_marker:paper_signals` | `signal_service::write_payload` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::expire_order:paper_orders` | `paper_order_ledger::expire_order` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::retire_duplicate_waitlist:paper_orders` | `paper_order_ledger::retire_duplicate_waitlist_orders` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::retire_duplicate_waitlist:paper_signals` | `signal_service::retire_waitlist_signals` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::retire_gated_orders:paper_orders` | `paper_order_ledger::retire_gated_order` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::supersede_duplicate_execution_retry:paper_orders` | `paper_order_ledger::supersede_duplicate_execution_retry` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::supersede_for_expired_signal:paper_orders` | `paper_order_ledger::supersede_for_expired_signal` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_dispatch::supersede_for_terminal_signal:paper_orders` | `paper_order_ledger::supersede_for_terminal_signal` | `test_execution_dispatch`<br>`test_entry_lifecycle`<br>`test_waitlist_dedupe_write` |
| `PR10:execution_planner::commit_fill:paper_orders` | `paper_order_ledger::record_execution_block`<br>`paper_order_ledger::record_fill_result`<br>`paper_order_ledger::accumulate_realized_pnl` | `test_execution_planner`<br>`test_deferred_fill_cycle_binding` |
| `PR10:execution_verification::backfill_legacy_orders:paper_orders` | `paper_order_ledger::stamp_execution_rows` | `test_execution_verification` |
| `PR10:execution_verification::stamp_order:paper_orders` | `paper_order_ledger::stamp_execution` | `test_execution_verification` |
| `PR10:execution_verification::stamp_terminal_zero_orders:paper_orders` | `paper_order_ledger::stamp_execution_rows` | `test_execution_verification` |
| `PR10:manual_orders::_commit_strategy_buy:paper_orders` | `paper_order_ledger::insert_order`<br>`paper_order_ledger::update_order_outcome` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::_terminalize_cycle_stale_order:paper_orders` | `paper_order_ledger::terminalize_cycle_drift_order` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::cancel_manual_order:paper_orders` | `paper_order_ledger::cancel_order` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::commit_strategy_entry_fill:paper_signals` | `signal_service::mark_filled`<br>`signal_service::record_signal_outcome`<br>`signal_service::defer_for_capacity` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::process_pending_manual_orders:paper_orders` | `paper_order_ledger::update_order_outcome` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::strategy_fill_failure:paper_orders` | `paper_order_ledger::update_order_outcome` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::strategy_fill_failure:paper_signals` | `signal_service::defer_for_capacity`<br>`signal_service::reopen_pending` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:manual_orders::submit_manual_order:paper_orders` | `paper_order_ledger::insert_order`<br>`paper_order_ledger::update_order_outcome` | `test_deferred_fill_cycle_binding`<br>`test_order_cycle_provenance`<br>`test_runtime_data_fixes` |
| `PR10:paper_cycle_service::archive_cycle:paper_cycles` | `paper_cycle_service::archive_cycle` | `test_paper_cycle_service`<br>`test_cycle_archive_writes` |
| `PR10:paper_risk_writes::<module>:paper_orders` | `paper_order_ledger::insert_order` | `test_paper_risk_writes`<br>`test_paper_risk_exit_production_path` |
| `PR10:paper_shared_cash::credit_shared_cash:paper_accounts` | `paper_account_ledger::apply_cash_delta` | `test_paper_shared_cash` |
| `PR10:paper_shared_cash::debit_shared_cash:paper_accounts` | `paper_account_ledger::debit_cash` | `test_paper_shared_cash` |
| `PR10:paper_trading::_bootstrap_signals_for_today:paper_signals` | `signal_service::supersede_blocked_for_day`<br>`signal_service::supersede_pending_for_day` | `test_strategy_selection_provenance`<br>`test_runtime_data_fixes` |
| `PR10:paper_trading::_buy_order:paper_orders` | `paper_order_ledger::insert_order`<br>`paper_order_ledger::supersede_older_intents` | `test_deferred_fill_cycle_binding`<br>`test_replacement_asof_provenance`<br>`test_signal_supersede_set` |
| `PR10:paper_trading::_buy_order:paper_signals` | `signal_service::reject_signal`<br>`signal_service::update_signal_feedback`<br>`signal_service::expire_within`<br>`signal_service::mark_shadow`<br>`signal_service::defer_for_capacity`<br>`signal_service::supersede_signals` | `test_deferred_fill_cycle_binding`<br>`test_replacement_asof_provenance`<br>`test_signal_supersede_set` |
| `PR10:paper_trading::_cleanup_stale_data:paper_orders` | `paper_order_ledger::archive_delete_orders`<br>`paper_order_ledger::archive_supersede_orders` | `test_cycle_archive_writes`<br>`test_paper_timing` |
| `PR10:paper_trading::_cleanup_stale_data:paper_signals` | `signal_service::archive_delete_signals`<br>`signal_service::archive_expire_signals`<br>`signal_service::archive_supersede_signals` | `test_cycle_archive_writes`<br>`test_paper_timing` |
| `PR10:paper_trading::_enforce_order_intent:paper_signals` | `signal_service::reject_signal` | `test_order_intent_contract` |
| `PR10:paper_trading::_ensure_accounts:paper_accounts` | `paper_account_ledger::ensure_profile`<br>`paper_account_ledger::set_version`<br>`paper_account_ledger::insert_account` | `test_paper_user_account_provisioning`<br>`test_paper_account_specs` |
| `PR10:paper_trading::_ensure_cycle:paper_accounts` | `paper_account_ledger::detach_cycle`<br>`paper_account_ledger::attach_to_cycle`<br>`paper_account_ledger::set_cycle_profile`<br>`paper_account_ledger::activate_for_running_cycle`<br>`paper_account_ledger::set_style`<br>`paper_account_ledger::set_daily_start_nav`<br>`paper_account_ledger::initialize_user_cycle` | `test_paper_user_cycle_attachment`<br>`test_ledger_bootstrap_lifecycle`<br>`test_paper_cycle_ownership` |
| `PR10:paper_trading::_ensure_cycle:paper_cycles` | `paper_cycle_service::insert_cycle`<br>`paper_cycle_service::set_capital` | `test_paper_user_cycle_attachment`<br>`test_ledger_bootstrap_lifecycle`<br>`test_paper_cycle_ownership` |
| `PR10:paper_trading::_intraday_sell:paper_orders` | `paper_order_ledger::insert_order`<br>`paper_order_ledger::update_risk_payload` | `test_paper_risk_exit_production_path`<br>`test_runtime_data_fixes` |
| `PR10:paper_trading::_rebuild_realized_pnl:paper_orders` | `paper_order_ledger::record_realized_pnl` | `test_execution_verification` |
| `PR10:paper_trading::_reconcile_shared_cash:paper_accounts` | `paper_account_ledger::apply_cash_delta` | `test_shared_pool_late_join`<br>`test_paper_shared_cash` |
| `PR10:paper_trading::_record_entry_frozen_waitlist:paper_orders` | `paper_order_ledger::insert_order` | `test_package_a_callers` |
| `PR10:paper_trading::_shared_risk_state:paper_accounts` | `paper_account_ledger::set_cooldown` | `test_cycle_funds`<br>`test_paper_account_ledger` |
| `PR10:paper_trading::_supersede_signal_execution_retries:paper_orders` | `paper_order_ledger::supersede_previous_execution_retry` | `test_package_a_callers` |
| `PR10:paper_trading::_weekly_review:paper_accounts` | `paper_account_ledger::write_params` | `test_paper_account_ledger`<br>`test_evolution_apply` |
| `PR10:paper_trading::configure_capital:paper_accounts` | `paper_account_ledger::set_cycle_capital` | `test_cycle_ledger_ownership`<br>`test_paper_cycle_capital` |
| `PR10:paper_trading::configure_capital:paper_cycles` | `paper_cycle_service::set_capital` | `test_cycle_ledger_ownership`<br>`test_paper_cycle_capital` |
| `PR10:paper_trading::create_cycle:paper_accounts` | `paper_account_ledger::detach_cycle`<br>`paper_account_ledger::apply_cycle_membership` | `test_paper_user_cycle_attachment`<br>`test_cycle_ledger_ownership`<br>`test_paper_account_ledger` |
| `PR10:paper_trading::create_cycle:paper_cycles` | `paper_cycle_service::insert_cycle` | `test_paper_user_cycle_attachment`<br>`test_cycle_ledger_ownership`<br>`test_paper_account_ledger` |
| `PR10:paper_trading::execute_open:paper_signals` | `signal_service::expire_signal`<br>`signal_service::write_payload` | `test_runtime_data_fixes`<br>`test_entry_lifecycle` |
| `PR10:paper_trading::monitor_fast_entries:paper_signals` | `signal_service::defer_for_capacity`<br>`signal_service::write_payload` | `test_runtime_data_fixes`<br>`test_execution_dispatch` |
| `PR10:paper_trading::process_pending_execution_orders:paper_orders` | `paper_order_ledger::advance_execution_state` | `test_deferred_fill_cycle_binding`<br>`test_execution_dispatch` |
| `PR10:paper_trading::process_pending_execution_orders:paper_signals` | `signal_service::advance_signal_feedback` | `test_deferred_fill_cycle_binding`<br>`test_execution_dispatch` |
| `PR10:paper_trading::set_account_style:paper_accounts` | `paper_account_ledger::set_style` | `test_paper_account_ledger` |
| `PR10:paper_trading::set_accounts_status:paper_accounts` | `paper_account_ledger::set_status` | `test_paper_cycle_service`<br>`test_cycle_ledger_ownership` |
| `PR10:paper_trading::set_accounts_status:paper_cycles` | `paper_cycle_service::set_status` | `test_paper_cycle_service`<br>`test_cycle_ledger_ownership` |
| `PR10:paper_user_account_provisioning::provision_user_accounts:paper_accounts` | `paper_account_ledger::insert_account` | `test_paper_user_account_provisioning` |
| `PR10:paper_user_cycle_attachment::reconcile_user_cycle_accounts:paper_accounts` | `paper_account_ledger::detach_cycle`<br>`paper_account_ledger::initialize_user_cycle` | `test_paper_user_cycle_attachment` |
| `PR10:signal_service::conflict_statement:paper_signals` | `signal_service::conflict_statement` | `test_signal_order_reconcile_write`<br>`test_entry_lifecycle`<br>`test_strategy_selection_provenance` |
| `PR10:signal_service::expire_for_expired_order:paper_signals` | `signal_service::expire_for_expired_order` | `test_signal_order_reconcile_write`<br>`test_entry_lifecycle`<br>`test_strategy_selection_provenance` |
| `PR10:signal_service::expire_stale_signal:paper_signals` | `signal_service::expire_stale_signal` | `test_signal_order_reconcile_write`<br>`test_entry_lifecycle`<br>`test_strategy_selection_provenance` |
| `PR10:signal_service::reconcile_signal_status:paper_signals` | `signal_service::reconcile_signal_status` | `test_signal_order_reconcile_write`<br>`test_entry_lifecycle`<br>`test_strategy_selection_provenance` |

## 验证与重现

新增边界回归覆盖：低绑定上限的大历史集合、重复 ID、终态保护、ABORT 单语句原子性、调用方回滚；参数省略/显式 NULL、真实恢复入口坏快照；账户跨字段成员重置失败原子性；五处改写守卫各有会被拒绝的违例样本。

读取契约同步实际读者；不会把只执行 DELETE 的持久化 owner 伪造为 SELECT 读者。没有独立权威读入口的表保留明确说明。

```powershell
python scripts/pr10_acceptance_evidence.py --check
python backend/tools/ledger_write_inventory.py
python scripts/run_tests_parallel.py --shards 4
```

首度重建冻结快照需要完整历史：`python scripts/pr10_acceptance_evidence.py --freeze 52eb50d`。日常验证读取已提交快照，浅克隆无需获取旧历史。测试使用隔离临时目录，不接触生产账本。

本轮本机 Python 3.14.3 全量运行 410 模块、7,753 项，371.2 秒。3 个分片全通过；另一分片仅部署脚本测试出现 9 个子断言失败，原因是 PATH 选中 Windows 的 Bash 入口。仅为复验把 Git Bash 加到该测试进程 PATH，部署守卫模块 5 项全部通过；没有修改部署脚本或测试判定。不能将第一次全量记作零失败。

A 相关 32 项、独立写入归属 21 项；C 扩展 122 项；D 周期/归档/预算 67 项，以及新增原子性 3 项均通过。最终 E 证据复验与远端 CI 状态以本次提交后的检查为准。Ruff 全 backend 与证据脚本通过。

未做服务器运行时验证；没有生产收益结论。
