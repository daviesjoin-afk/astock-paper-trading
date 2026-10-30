# PR-3 风险输入按消费者收窄

基线为 PR299 合并提交 `6d1f9df857a0175690201a33cdc21ff8dd9baa15`。
按《模拟盘升级路线图》的 PR-3 实施；经用户确认，保留原新闻决策输入，候选新闻缺口单独列出。

## 取数归属与范围

`paper_risk_evidence.eligible_replacement_candidates` 复用现有
`paper_replacement_evidence.load_replacement_candidates`，统一预取与决策的候选资格。
范围为有持仓的账户、`intended_date == day`、`signal_date <= day`、原三类可替补状态、
非该账户已持有且证券权限允许的候选。合法隔夜计划继续保留；候选评分、排序和同分次序不变。
没有新增周期过滤条件，也不以当前运行账户集合排除历史持仓的风险处理。

`paper_risk_market_inputs` 负责采集：

| 事实 | 本次请求范围 | 失败语义 |
| --- | --- | --- |
| 报价 | 持仓与有效替补候选并集，去重一次取数 | 沿用原报价缺失、新鲜度、核验及成交门禁 |
| 资金流轨迹 | 去重持仓代码，不再请求候选专用轨迹 | 批量异常按持仓返回 `source_unavailable`、异常类型、`score_applied=False`；逐票失败沿用原状态 |
| 新闻 | 原持仓代码与名称 | 原 `news_meta` 的 error/stale/observed_at 不变 |
| 当日指数和持仓替代证据 | 原有路径 | 原有未知/失败语义 |

不新增缓存。事实只属于当前风险调用，源时间不重写；下一次调用重新经过现有 provider 的缓存与失败恢复规则。
资金流与新闻继续并行，报价仍留在调用线程，保持现有轮内报价复用。
决策写事务内仍重新读取候选、持仓、周期与运行资格；不缓存替补选择或买卖结论。
若采集后候选发生变化，缺少报价继续走原拒绝路径，不在 writer 内补网络。

## 新闻缺口及范围调整

原风险路径只为持仓取新闻。非持仓替补通常没有新闻输入；直接补新闻并接入换仓买入判断，
可能让负面事件拦截原本会执行的换仓，无法承诺交易结果不变。
用户已明确选择保留原结果，因此本次不扩大新闻输入。
候选新闻补齐需要另行批准行为变化，并验证负面事件、公告核验及源失败的买入结果。
这项缺口没有被标为已完成。PR-7 多策略公共事实复用也不在本次范围。

## 可复跑验收

`backend/test_risk_input_demand.py` 使用临时 SQLite 与固定数据源替身。
资格对照独立执行原 SQL、权限筛选与纯替补选择，比较完整候选行及胜出结果；
覆盖当日/隔夜、未来/过期、三类状态、多账户、已持有、证券权限、同分次序、writer 重验。
真实 `monitor_risk` 样例包含 1 个持仓及 6 个 pending 代码，验证请求参数：

| 口径 | 基线 | 本次 |
| --- | ---: | ---: |
| 报价批次所含唯一代码 | 6 | 2 |
| 资金流轨迹批次所含唯一代码 | 6 | 1 |
| 报价批量入口次数 | 1 | 1 |
| 轨迹批量入口次数 | 1 | 1 |
| 新闻范围 | 持仓 | 持仓 |

这些是离线请求范围和入口计数，不是生产 HTTP 次数。轨迹批次内部按证券逐个调用
`alt_data.fund_flow_trajectory`，因此不需要的逐票任务被移除；底层缓存、重试和熔断仍影响实际 HTTP 数。
空仓不为候选取数；资金流异常明确标记未知且后续调用可恢复，新闻失败元数据继续保留。

复跑：

```bash
PYTHONPATH=backend python -m unittest test_risk_input_demand test_import_laziness \
  test_replacement_asof_provenance test_risk_application_service \
  test_paper_risk_exit_production_path test_risk_alt_evidence_prefetch \
  test_paper_risk_scan_state test_paper_risk_decision test_open_execution_risk_reuse
python scripts/run_tests_parallel.py --shards 8 --timeout 900
```

固定时钟账本对照复用 `scripts/pr298_ledger_probe.py`，分别在基线与当前 checkout 执行，
各用独立临时 `ASTOCK_DATA_DIR`，验收工具依赖 freezegun 不进入应用依赖。
10 个场景、17 个检查点的风险返回与 12 表全部字段一致，规范化 JSON SHA256：
`b1251b6aeb06585b21b59ce830202ac33a70867dac4c3eee21e07ef159261b4a`。
涵盖空仓、失败重试、周期切换、完整/部分卖出、重复执行与 fencing（阻止失去运行资格的进程提交成交）。
相关回归 164 项通过；完整后端与架构最终结果见 PR 描述。

## 限制与回滚

不更改风险阈值、策略评分、资金约束、T+1、执行权威、手动订单路径、schema、cron 或部署。
资格读取从全库代码扫描变为按持仓账户读取，SQL 次数可能增加；按账户重复计算优化属于 PR-5。
未测生产 HTTP、风险间隔、SQLite writer 等待与整轮 P50/P95，不发布线上提速结论。
本地性能数不能替代同一版本、同一来源状态下的自然交易时段样本。
无数据库迁移；回滚使用原镜像/代码，保留合法交易账本，不用旧备份覆盖成交。

MERGE: NOT MERGED（本 PR）

DEPLOY: NOT DEPLOYED
