# 源码效率与缺陷审查（2026-10-23）

基线：`codex/large-file-split`，`2983b15`。本轮确认 **6 类功能缺陷、1 项有测量证据的性能问题**。仅新增审查文档、探针和结果，未修改业务源码。探针曾误写工作树缓存，处置及未能确认恢复的部分见文末。

## 结论与处理顺序

P1 表示应优先处理的数据正确性或隔离问题；P2 表示有条件触发的可用性、展示正确性或性能问题。

| 编号 | 优先级 | 问题 | 主要位置 |
|---|---|---|---|
| B1 | P1 | 独立数据目录配置未覆盖行情缓存及选股跟踪库 | `backend/data_fetcher.py:33`、`backend/selection_tracking.py:22` |
| B2 | P1 | 收盘跟踪误用实时规则：正常收盘被跳过，盘中报价反而能入库 | `backend/selection_tracking.py:580`、`:609`、`:643` |
| B3 | P2 | 保存设置在异步请求中同步等待数据库，阻塞同一服务进程处理其他请求 | `backend/api_settings.py:175` |
| B4 | P2 | 前端超时只覆盖响应头，正文读取可无限等待 | `frontend/src/core/api.js:275`、`:288` |
| B5 | P2 | 只读验证页面每次重建数据库触发器，与后台写入争锁 | `backend/selection_tracking.py:759`、`:310` |
| B6 | P2 | 板块联动把过期快照标为“今日共振” | `backend/linkage.py:62` |
| E1 | P2 | 扫描接口每次重读全部历史 CSV、重算相同因子 | `backend/main.py:1051` |

### B1：数据目录隔离不完整

`data_paths.py` 明确以 `ASTOCK_DATA_DIR` 指定独立运行目录；账本和部分财务文件已使用该入口，但 `data_fetcher.CACHE_DIR` 仍固定为源码根目录下的 `data_cache`。K 线、行情快照、清单，以及依赖它的 `selection_tracking.DB_PATH` 都会落入旧目录。

**触发与影响：** 在测试、离线验证或另一个实例中设置独立目录后，仍可能读写原工作树缓存；不同目录中的账本与行情也可能被混用。本轮探针实际触发了该问题。

**证据：** 新进程配置临时目录后，`effective_market_cache` 仍指向工作树的 `data_cache`，`isolated=false`。现已保存的探针在导入消费者前显式重绑定缓存路径，并断言跟踪库位于临时目录。

**最小修复方向：** 复用 `data_paths.data_dir()/data_path()`，核对所有派生常量的初始化顺序；增加新进程级验证，断言指定独立目录后源码目录没有产生运行数据。只修一个数据库路径不足以解决整条路径分裂。

### B2：收盘跟踪的时间规则用反了

`_authority_snapshot_rows()` 调用 `refresh_rows()`，默认采用报价距当前时间最多 240 秒的实时规则。定时任务在 15:45、16:15 等收盘后时刻运行（`deploy/astock-codex.cron:60`）；正常 15:00 报价已经超窗。

同一模块的 `update_observations()` 只检查报价日期等于当天，没有要求已经收盘。页面按钮通过 `POST /api/selection-evaluation/refresh` 直接进入它，因此盘中刷新可以写入所谓收盘收益。唯一键配合 `INSERT OR IGNORE` 又使当天后续记录无法纠正它。

**复现：**

- 固定时间 15:45，提供 4,000 条当天 15:00 报价：跟踪调用返回 0 条，任务跳过；使用项目已有的 `CLOSE_SNAPSHOT_POLICY` 返回 4,000 条。
- 昨日入选价 10 元，当天 10:30 报价 11 元：写入一条 +10% 记录；随后给出真正收盘价 10 元，新增数为 0，库中仍保留 11 元。
- 第二步的收盘纠正探针直接提供有效收盘行，用来单独验证重复记录不能覆盖；即使先修复了取数规则，也不能自动修复已污染的历史。

**最小修复方向：** 跟踪入口明确传入收盘规则及目标交易日，收盘前禁止记录；保留逐行时间窗口验证。已有盘中样本的修复需要可靠原始报价证据，不能简单把所有重复记录改成无条件覆盖。

### B3：异步路由内执行同步数据库事务

`update_settings()` 在 `async def` 中直接调用 `P.init_db()`、`P._db(immediate=True)` 和返回快照。这些调用会同步执行 SQLite，并可能等待数据库写锁。异步事件循环就是同一服务进程调度请求的线程；这里等待时，其他请求也无法正常推进。`paper_storage.py:57` 配置的普通数据库锁等待可达 60 秒。

**复现：** 临时数据库由另一连接持有写锁，调用真实设置路由和真实存储事务入口；只把数据库等待缩短为 250 毫秒，并跳过初始化。原本 20 毫秒应执行的并发心跳约 344 毫秒才执行。说明阻塞影响超出了保存设置这一请求。

**最小修复方向：** 将设置请求解析成结构化参数后使用普通同步路由，或把完整同步业务单元交给线程执行；数据库连接应在线程内创建和关闭。`api_paper.py:429` 的旧预览路由也存在同类写法，但当前策略编辑页面使用的是另一个同步预览接口，不能把旧路由问题误说成当前预览按钮必现。

### B4：请求正文没有超时保护

`fetch()` 在响应头到达后就可以返回。`_sendOnce()` 随即清除计时器，而稍后 `_settle()` 才等待 `response.json()`。服务端或代理只要先返回头部、随后正文停滞，请求就不再受设定超时约束，依赖请求完成来释放的界面加载状态也会一直保留。

**复现：** 本机 HTTP 服务立即发送头部、300 毫秒后发送正文。调用 `api(..., {method:'POST', timeout:100})`，最终留存的复跑结果为约 326 毫秒后成功返回，没有超时。

**最小修复方向：** 每次尝试的计时器覆盖响应正文读取，完成或失败后统一清理；重试前取消不再使用的响应体。读取正文的中止异常必须传播，不能被当前 JSON 解析兜底转换成成功的 `{}`；写请求仍不可自动重放。

### B5：读页面重复执行数据库结构修改

`dashboard()` 每次先执行 `ensure_schema()`，后者无条件删除、重建两个触发器。这使 `GET /api/selection-evaluation` 需要数据库写锁，并反复使数据库结构版本变化。触发器本身是有用的完整性约束，问题在于每次读取都重建。

**复现：** 对已初始化的数据库调用一次 dashboard，`PRAGMA schema_version` 从 20 变为 24。在另一连接持有写事务时，普通 `SELECT` 可以读取同一库，dashboard 却报 `database is locked`。探针把等待缩短到 50 毫秒；生产连接默认是 20 秒。

**最小修复方向：** 将结构升级留给显式初始化/迁移入口，仅在版本变化时调整触发器；页面使用只读查询。并补齐连接关闭：`with sqlite3.Connection` 只负责提交或回滚，不负责关闭连接，当前 `with _connect()` 不能代替 `closing()`。

### B6：过期数据仍触发“今日”标记

`linkage.sector_linkage()` 使用 `read_snapshot()` 返回的行，却没有检查新鲜度、报价日期及降级状态。该读取接口刻意保留最后一份已知事实供展示，所以“返回行”不等于“今天有效”。后续直接计算 `co_move_today`、`co_fund_today`、`resonance`，并缓存五分钟。

**复现：** 当前日期 10 月 23 日，快照是 10 月 22 日 15:00，契约判定明确为 `stale`；两个行业仍被输出为 `co_move_today=true`、`co_fund_today=true`、`resonance=true`。

**最小修复方向：** 历史相关性仍可展示；当日共振必须通过适用时间规则及业务日期验证，证据不足时明确标为不可判定。不要通过在只读页面强制联网来掩盖数据过期。

### E1：重复扫描重复做历史计算

`main.scanner()` 每次遍历所有股票调用 `load_shared_kline()`，底层实际读取 CSV、解析日期、去重排序，再计算整张价格因子表。用户只是修改一个过滤条件，也会重做不变的历史部分。当前主选股流程已经有 `selection_service._load_selection_base()` 的版本签名缓存，可作为复用方向。

**实测：** 250 只合成股票，每只 260 根日线，连续两次调用真实 scanner。共读取 500 次 CSV、执行 2 次完整因子计算，两次因子表完全相同；耗时分别约 3.33 秒和 3.67 秒。报价服务被替换为空结果，测量在覆盖率检查处结束，因此这些时间仅说明该前置重复工作的成本，不是生产全流程延迟，也不外推全市场耗时。

**替换方向：** 按完整交易日、历史清单版本和因子算法版本复用已验证的因子表，在结果上应用查询过滤；实时行情仍独立检查覆盖率与新鲜度。不要简单缓存整个带实时行情的响应，也不要为缩短耗时删掉历史数据有效性检查。

## 其他可替换写法：已确认结构，尚未测量收益

| 位置 | 现有成本 | 最小替换建议 | 修复状态 |
|---|---|---|---|
| `selection_tracking.py:629` | 每个候选单独查询观测数量，形成 N+1 查询，即先查一批数据，再为每条追加查询 | 在同一事务中批量统计所需候选的观测数；保留每日唯一约束 | 已改（一次分组统计） |
| `selection_tracking.py:714` | 为得到每个候选的最新点，读取并排序所有历史观测，再逐条丢弃旧记录 | 限定本次候选集合，在数据库中选取每个候选的最新记录，明确并列时的排序规则 | 已改（SQL 内限定候选窗口 + `holding_days → observed_date → id` 并列规则） |
| `main.py:1110` | `DataFrame.apply(axis=1)` 为逐行证券范围判断创建行对象 | 若测量证实是瓶颈，可用只含所需字段的 `itertuples`；仍调用同一证券范围规则，避免复制规则 | 已测量并改（46.1ms → 9.5ms，见 E2） |

这些不计入已复现的缺陷数量。三项的修复与测量记录见"修复记录"一节；E2 的收益是单步 ~37ms，不等同于端到端加速。

## 覆盖范围与验证

全库清单及模式扫描覆盖基线中 **764 个代码文件**：436 个源码/辅助文件、326 个测试或夹具、2 个生成/第三方文件。其中源码分类包括后端 262、前端 36、辅助工作脚本 124、部署 9、其他启动/脚本 5。

所有 Python 文件已解析语法；遍历函数规模、循环中的 I/O/查询、并发入口、缓存与时间判断等模式。对行情边界、选股服务与跟踪、请求生命周期、数据库事务、板块联动及交易/研究热点做了调用链复核。**这是全库自动扫描加重点人工审查，不表示每个分支都已执行或逐行穷尽证明。**

- `python -m ruff check backend`：通过。
- 后端相关现有测试：146 项通过，覆盖行情边界、选股来源、架构边界、选股服务和仓位计算；有既有资源未关闭警告。
- 前端现有单元测试：239 项通过。
- 部署/启动 Shell 脚本语法检查及 `git diff --check`：通过。
- 隔离探针复现了上列缺陷；没有连接真实行情服务，没有运行部署或交易操作。
- 已排除旧报告中修好的 dashboard 联网、API 缓存递归等待、风险任务释放锁次序等问题；没有把已缓存的股票池/清单误报为每次磁盘重读。

## 可重复执行的探针

在仓库根目录运行（依赖项目已有 Python/Node 环境）：

```bash
python -X utf8 scripts/source-audit-20261023/backend-probe.py
python -X utf8 scripts/source-audit-20261023/performance-probe.py
python -X utf8 scripts/source-audit-20261023/scope-probe.py
node scripts/source-audit-20261023/frontend-probe.mjs
```

脚本使用临时 SQLite 和合成行情，显式重绑定已发现不遵守环境变量的路径。结果保存在同目录的 `*-result.json`；基线源码库存清单位于 `.codex/source-audit/inventory.json`。这些是本轮诊断脚本，不是新加入的业务测试体系。`scope-probe.py` 是修复阶段新增的微基准（证券范围掩码两种写法），与上面三个缺陷复现探针分开计数。

## 本轮探针误写缓存的说明

最初依据 `data_paths` 的目录隔离说明设置了临时目录，但没有在首次写入前核验消费者的实际路径。B1 导致探针写入了当前工作树缓存。这是审查执行上的疏漏。

- 250 个 CSV 经创建时间和完整内容核对，全部是本轮新生成文件，已删除。
- 当前可确认的测试跟踪记录已删除；清理前已保存 SQLite 备份至 `.codex/source-audit/cache-recovery/selection_tracking.before-probe-cleanup.db`。
- `selection_tracking.db` 在审查前就存在，且没有事前备份，**不能确认原内容和结构已经完全恢复，也不能排除原有同日同策略记录被覆盖**。清理前备份只保留事故后的状态，不应误称原始恢复点。
- 模拟盘主账本文件的修改时间仍为当日 08:49，早于探针运行时间；本轮未通过探针对它执行交易写入。
- 修订后的探针已在显式临时路径下重新执行，主要缺陷结论均再次成立。

因此，后续处理应先修 B1，再修 B2；在继续使用当前工作树的选股跟踪缓存前，应核对其来源或从可信备份恢复。

## 修复记录（同日，`codex/large-file-split`）

上列 6 类缺陷与 1 项实测性能问题已全部修复。文件位置均以本次基线为准；每项都配了会失败的回归测试，探针在修复后重跑并保存了新的结果文件。

| 编号 | 修复方式 | 主要位置 | 回归 |
|---|---|---|---|
| B1 | 缓存根改由 `data_paths` 解析，并统一 11 处运行时路径 | `data_fetcher.py:32`、`alt_data.py`、`selection_alpha_report.py`、`trade_attribution.py`、`adaptive_learning_dispatch.py`、`resource_guard.py`、`strategy_trace.py`、`risk_center.py`、`tracker.py`、`strategy_registry.py`、`security_state_point_in_time.py` | `backend/test_data_dir_isolation.py`（新进程，3 项） |
| B2 | 跟踪取数显式走 `CLOSE_SNAPSHOT_POLICY` + 目标交易日，收盘前拒绝写入，逐行校验报价业务日 | `selection_tracking.py:600` | `backend/test_selection_tracking_close_rules.py`（10 项） |
| B3 | 同步业务单元（含连接创建/关闭）整块搬进线程池；旧预览路由改回同步路由 | `api_settings.py:151`、`api_paper.py:428` | `backend/test_settings_async_isolation.py`（7 项） |
| B4 | 每次尝试的计时器覆盖正文读取；读取异常向上传播，不再被 JSON 解析兜底吞成 `{}` | `frontend/src/core/api.js:262` | `frontend/tests/api-request-contract.test.mjs` +6 项 |
| B5 | 读页面用 `mode=ro` 连接；结构升级只留在显式 `ensure_schema`，触发器仅在定义变化时重建；连接改用 `closing()` | `selection_tracking.py:51`、`:285`、`:820` | 同上（B2 文件内 4 项读路径测试） |
| B6 | 当日共振过适用时间规则（收盘/实时口径）+ 业务日校验；证据不足输出 `null` 而非 `false` | `linkage.py:23` | `backend/test_linkage_freshness.py`（6 项） |
| E1 | 历史因子按"完整交易日 + 清单版本 + 算法版本"复用，实时行情仍每次校验 | `main.py:1042` | `backend/test_scanner_history_reuse.py`（4 项） |
| E2 | 证券范围掩码换 `itertuples`（实测 46.1ms → 9.5ms），规则不复制 | `main.py:1113` | `backend/test_scanner_scope_mask.py`（7 项） |
| B2b | 新增离线修正工具：只按收盘政策验证过的原始报价改写污染行 | `selection_tracking_repair.py`（新） | `backend/test_selection_tracking_repair.py`（15 项） |

### 关键实现取舍

- **B1**：`data_fetcher.CACHE_DIR` 是整条路径分裂的根：`selection_tracking.DB_PATH`、`selection_service` 的因子/结果缓存、`universe.UNIVERSE_PATH` 都从它派生。因此修的是根而不是逐个数据库。`ASTOCK_CACHE_DIR`（`strategy_trace` / `resource_guard` / `adaptive_learning_dispatch`）保留原有优先级，只把兜底从源码目录改成 `data_dir()`。新测试用**新进程**断言所有路径落在指定目录内，并断言源码 `data_cache/` 没有新增文件——旧的 `dfc.CACHE_DIR = temp` 重绑定不再需要。
- **B2**：不再给 `refresh_rows()` 传默认实时规则，而是显式传收盘政策与 `asof_day`，因此 15:45 / 16:15 / 16:45 / 17:15 四个 cron 时刻都能拿到当日 15:00 报价。`INSERT OR IGNORE` 保持不变：已被盘中写入污染的历史行需要原始报价证据才能纠正，不能靠"重复即覆盖"处理。
- **B5**：`dashboard()` 不再调用 `ensure_schema()`，库未初始化时如实返回空证据（响应结构不变）。触发器仍是完整性约束，只是不再每次读取都 DROP + CREATE。
- **B6**：15:00 之后用收盘口径、盘中用实时口径；历史相关性照旧可展示，只把当日断言降级为不可判定，并且没有为了"看起来有数据"而在只读路径上联网。

### 同时处理的低风险项

审查报告"其他可替换写法"表中的两项已在同一次改动中完成（都与 B2 同函数）：`update_observations` 的 N+1 观测计数改成一次分组统计；`_duplicate_picks` 的最新跟踪点改为在 SQL 内限定本次候选集合并显式声明并列规则（`holding_days → observed_date → id`）。第三项（`main.scanner` 的逐行证券范围判断）先补测量再替换，见下节。

### E2：证券范围逐行判断（先测量，后替换）

`main.scanner` 用 `DataFrame.apply(axis=1)` 为每一行判断可买入范围。新增微基准 `scripts/source-audit-20261023/scope-probe.py`，合成 5000 行 × 64 列（与真实因子表同宽）、覆盖主板/创业板/科创板/北交所/ST/ETF/坏代码，两段都调用**同一份** `selection_service._buy_scope`：

| 写法 | 三次耗时 (ms) | 中位数 |
|---|---|---|
| `apply(axis=1)` | 46.1 / 45.7 / 46.2 | 46.1 |
| `itertuples` | 10.6 / 9.5 / 9.1 | 9.5 |

掩码逐行一致（`identical_mask=true`），约 **4.85×**。已换成 `main._permitted_scope_mask()`，规则仍只在 `_buy_scope` 一处。诚实结论：这一步省的是 **~37ms/次扫描**，不是主要成本（E1 省的是秒级），改动理由是"同等语义下更便宜且无副作用"，不是"解决了性能瓶颈"。

回归：`backend/test_scanner_scope_mask.py`（7 项）把新实现与旧 `apply` 写法逐行比对，覆盖缺列、NaN、float 化 `risk_flag`、受限板块，并锁定"没有 `code` 列时全部拒绝"。

### B2b：用原始报价证据修正已污染的观测

B2 只阻止新的盘中写入。已落库的行需要证据才能修正，因此新增离线工具 `backend/selection_tracking_repair.py`（不在任何请求路径上）：

- 证据来源是 authority 自己持久化的全市场快照 payload（或等价的 `{code, price, quote_at}` 行列表）；
- 判定复用 `market_data_contract.close_snapshot_rows`：证据时点必须 ≥ 15:00 且落在该交易日收盘窗口内，否则直接失败（`RepairError`），不猜；
- 只改**被证据证明有误**的行；证据缺失、属于别的交易日、或价格已一致的行原样保留并分类计数；
- 默认 dry-run，`--apply` 才写库，且 `UPDATE ... WHERE price = <旧值>` 比对，避免覆盖期间发生的其它写入；
- `holding_days` 语义不变（它记录的是"已记录收盘观测次数"）。

实测：`python backend/selection_tracking_repair.py --evidence data_cache/market_snapshot_full.json`（工作树真实 artifact，`saved_at=2026-10-23 15:15`）验证出 10 条收盘报价、扫描 0 行 —— 当前工作树跟踪库里 `selection_runs` / `selection_picks` / `selection_observations` 都是 0 行，本地没有可修数据；工具在有数据的库上按 `backend/test_selection_tracking_repair.py`（15 项）验证：11.0 → 10.0、收益重算为 0.0、无证据行不动、二次执行幂等、盘中归档与错标证据一律拒绝。

### 修复后复跑证据

- `python -X utf8 scripts/source-audit-20261023/backend-probe.py`
  - `data_directory`: `isolated=true`，K 线目录由 import 直接建在隔离目录内（不再需要手工重绑定）。
  - `close_policy`: 收盘政策取回 4000 行（原先跟踪调用 0 行）。
  - `intraday_as_close`: 盘中刷新 `skipped / market has not closed yet`；收盘刷新写入收盘价 10.0，不再是盘中 11.0。
  - `dashboard_schema`: `schema_version` 8 → 8；另一连接持有写事务时 dashboard 不再报 `database is locked`（2ms）。
  - `settings_async_blocking`: 并发心跳 32ms（原先 344ms）。
  - `async_blocking`: 旧预览路由 `route_is_coroutine_function=false`，心跳 30ms。
- `python -X utf8 scripts/source-audit-20261023/performance-probe.py`：3 次扫描（无条件 / `pe_max` / `pb_max`）共 **250 次 CSV 读取、1 次因子计算**（原先 2 次扫描就是 500 次读取、2 次计算）。
- `node scripts/source-audit-20261023/frontend-probe.mjs`：100ms 超时在 **116ms** 处 `AbortError`（原先 326ms 后成功返回）。
- `stale_linkage`：10 月 22 日快照在 10 月 23 日不再产出 `resonance=true`，三个当日字段均为 `null`。
- 回归：`python -m ruff check backend` 通过；后端主回归 **6092 项通过 / 9 项跳过 / 0 失败**（含 4281 项子测试，修复期间全量跑过一次）；追加 E2/B2b 后，涉及改动面的定向回归 **434 项 + 37 项新测试** 全部通过；前端 **245 项通过**；`frontend/dist` 已重建。
- `python -X utf8 scripts/source-audit-20261023/scope-probe.py`：5000 行 × 64 列掩码，`apply(axis=1)` 46.1ms → `itertuples` 9.5ms，掩码逐行一致。

### 仍然存在的限制

- 修正历史污染需要**原始报价证据**：工具已就绪（`selection_tracking_repair.py`），但没有归档的行仍然改不了 —— 这是刻意的，宁可留下可疑行也不编造收盘价。当前工作树跟踪库为空（0 行观测），本地没有可修数据。
- 当前工作树 `selection_tracking.db` 在审查期间被探针写过且没有事前备份，来源仍未经确认；继续使用前应按第 124–128 行的说明核对或从可信备份恢复。
- E1 的缓存按清单/目录签名失效：历史回补正在写盘的窗口内缓存会反复失效并重算，这是有意的保守选择（宁可重算，也不让写了一半的历史进入因子表）。
- E2 的收益是单步 ~37ms，不是端到端加速；如果后续把证券范围判断也搬进因子缓存（按 `universe` 版本），这一步可以直接省掉。
