# PR-5 风险只读查询复用

基线 commit：`32f2a9951f8b3aa84894a1ca71322c72f73dc0b7`（PR301 squash 合并提交）。
本批只治理重复替补读取和风险动作执行记录核验，未修改策略、风险阈值、候选规则、行情政策、
数据库 schema、调度或成交权威；PR-4 的日线轮内复用保持。

## 替补事实：按账户读取，各持仓独立判断

`paper_replacement_evidence.load_replacement_candidates` 仍是 SQL owner。
`paper_risk_evidence.replacement_candidate_inputs` 统一账户、日期和原三类状态范围，公开声明在 `__all__`。
避免服务另写一份状态列表或读取规则。

风险预取阶段仍按原路径读取；writer（写事务）内重新核对周期、持仓后，按持仓账户读取原始行一次，
放在逐持仓复核之前。不会把预取期间读到的候选直接当成 writer 内仍有效的候选。
`eligible_replacement_candidates` 与 `best_replacement_candidate` 可消费这一同账户、同日、同状态范围的原始行，
每个持仓仍独立执行证券权限、持仓排除和纯候选排序，取得独立的选择结果。
复用的是行，不是资格结论或交易批准；空列表就是空列表，不触发隐藏的第二次查询。
旧调用不传原始行时仍走原 owner 查询。数据只在本次 writer 的复核阶段使用，不跨事务持久化。

日期等式 `intended_date == asof_day`、`signal_date <= asof_day`、合法隔夜候选、状态范围、
账户隔离、证券权限、排序和同分顺序均保持。
换仓真正买入时，原 signal 重新读取、资金/持仓/周期/运行资格及成交权威复核仍然执行。

## 执行动作记录：当前持仓、当前事务内一次读取

`execution_verification.verified_execution_markers` 为一个持仓合并读取本次所需的四种 marker：
硬止损首段、下跌预警首段、当前下跌级别、预警连续确认。
保持原 `POSITIVE_EXECUTION_PREDICATE`、sell/account/code/as-of 范围，
SQLite `json_extract` 与历史 reason `LIKE` 匹配方式不变，最早记录仍按订单 id 排序。
单项与批量共用同一个时间/价格证据格式化实现；部分成交仍属于已发生的正成交，未升格为完整成交。

返回值保留原三种形状：

| 结果 | 含义及消费方式 |
| --- | --- |
| `None` | 未查到符合原正成交谓词的记录，不签发“已确认零成交”事实 |
| 有内容字典 | 保留最早记录的创建/执行时间、成交价、距今分钟；不可解析字段仍为 None |
| 字段均未知的字典 | 无法核验，保守阻止重复动作；绝不压成 False 或当作未执行 |

批量语句遇到 SQLite 错误时，回原 `first_verified_positive_execution` 逐项读取。
例如某 marker 的早期命中在坏 JSON 之前，原 `LIMIT 1` 可以成功；不能因为批量读取遇到后面的坏行，
就把所有 marker 都改成未知或全都改成未发生。缺表、坏 JSON 等失败语义由原 owner 保持。

marker 批量读取保留在当前持仓的消费点，处于 writer 内；不缓存到后续持仓或下一事务。
前一个持仓可能已提交新的执行事实，不能拿早期快照推断后一个持仓的动作是否已发生。
因此本批将账户候选查询移出逐持仓循环，将四次 marker SQL 合为当前持仓的一次窄查询；
没有为“全部移到循环外”另建一份可能过期的 marker 状态。
新的接口只读，不写订单、流水或验证状态；`execution_planner` 的 revalidate/commit_fill 权威不变。

## 验证与查询计数

新增 `backend/test_risk_query_reuse.py` 的 10 项测试覆盖：
单批一次 SELECT、完整/部分成交、未知/不一致列、账户/证券/日期/方向隔离、最早顺序、
历史 LIKE、坏时间、坏 JSON 回退、缺表未知、事务中新增记录后重新读取、裸 SQLite 行、
标识符白名单、输入复用而非决策复用、空输入不重读、三持仓生产 SQL 计数，以及预取后候选过期的 writer 重验。
相关八模块 135 项通过；架构/交易权威三模块 118 项通过。Ruff、compileall 与 diff check 通过，未放宽预算。

复用现有 `scripts/pr4_risk_kline_probe.py`，仅扩展 SQL 计数、同账户三持仓候选场景及信号表捕获。
在基线与当前 checkout 根目录运行同一绝对脚本，固定时钟、固定源、独立临时 SQLite/CSV；
14 场景的风险返回、异常和 15 表全部内容一致，共 400 行、6,007 字段。
其中三持仓场景包含合法当日/隔夜候选和未来候选，逐持仓替补结果仍为相同证券与分数，信号状态也一致。
规范化结果 JSON SHA256：
`4240e9b63dd96b2b0657823bfbeec7bf7ff52c34c28447d3bfe49b64cac1f330`。

| 固定场景 | 预取阶段候选 SQL 前→后 | writer 候选 SQL 前→后 | marker SQL 前→后 |
| --- | ---: | ---: | ---: |
| 单账户单持仓、正常证据 | 1→1 | 1→1 | 4→1 |
| 同证券两个账户 | 2→2 | 2→2 | 8→2 |
| 同账户三个持仓与有效候选 | 1→1 | 3→1 | 12→3 |
| 旧报价，不进入执行动作核验 | 1→1 | 1→1 | 0→0 |

三持仓场景中，上述目标语句合计 16→5 次。计数来自 SQLite trace，描述入口 SQL 语句数，
不等于扫描行数、数据库文件 I/O 或生产耗时。异常回退会额外读取；批量语句可能读取比旧 LIMIT 1 更多的行，
没有据此宣称所有场景或整轮都加速。线上锁等待和 P50/P95 尚未验证。

复跑（freezegun 只作探针依赖，不加入应用依赖）：

```bash
PYTHONPATH=backend python -m unittest test_risk_query_reuse test_risk_application_service \
  test_risk_input_demand test_replacement_asof_provenance test_r26_convergence_regressions \
  test_r26_execution_authority_guard test_paper_risk_exit_production_path test_risk_round_kline
python /absolute/path/to/scripts/pr4_risk_kline_probe.py /temporary/path/result.json
python scripts/run_tests_parallel.py --shards 8 --timeout 900
```

本地完整后端 7,501 项全部通过，Python 3.14.5，8 分片 344.0 秒，无失败、错误或模块加载错误。
远端最终 HEAD 的 CI 实际日志同步记录在本 PR 描述。无迁移，回滚恢复原代码/镜像并保留合法账本，
不以旧备份覆盖成交。不部署，保持 Draft 等待审核。

MERGE: NOT MERGED（本 PR）

DEPLOY: NOT DEPLOYED
