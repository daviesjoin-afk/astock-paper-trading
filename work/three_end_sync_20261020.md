# 三端契约同步核验报告（backend/ · frontend/src/ · deploy/）

- 审计日期：2026-10-20（21:38–22:05 CST）
- 基线：`4d4cb7b`（#255 大合并） + 工作区未提交改动（`paper_job_run_facts` 拆分、前端 XSS 硬化、docs 更新）
- 方式：只读审计；全部结论由命令/脚本输出支撑。**未修改** backend/**、frontend/src/**、frontend/dist/**、deploy/**。
- 重要前提：审计期间**其他 teammate 正在并发改工作区**（`frontend/src/*`、`frontend/dist/app.js`、`ARCHITECTURE.md`、`docs/REPOSITORY_LAYOUT.md`、`backend/evolution_*.py` 等在审计窗口内被修改）。本报告结论对应上表时间点的快照；凡受并发影响处均已标注。
- 审计脚本/产物：`work/_three_end_api_check.py`、`work/_openapi.json`（`main.app.openapi()` 进程内导出，153 条路径）、`work/_api_check_out.txt`。

---

## 0. 结论摘要

| 契约 | 状态 |
| --- | --- |
| HTTP API（前端调用 ↔ 后端路由/方法） | ✅ 干净：98 个唯一 (method, path) 调用点全部命中；0 缺失、0 方法错配 |
| 凭据方法白名单 | ✅ 逐字对齐 |
| 构建标识 build id | ✅ 三处一致（后端 / src / index.html / dist） |
| 调度契约（PSch ↔ cron ↔ Windows ↔ runner ↔ slot_service） | ⚠️ 主词表已单一来源，但 **Windows 渲染器丢失 retry 时段** |
| 部署脚本 | ❌ `install-centos9.sh` 引用 3 个仓库中不存在的文件（native profile 必失败）；另有 1 个孤儿 cron 文件 |
| 构建产物一致性 | ⚠️ dist 与当前 src 同步，但 dist 相对 HEAD 是脏的（前端改动未提交，CI 的 `git diff --exit-code -- frontend/dist` 会红） |
| `paper_job_run_facts` 拆分 | ✅ 代码/测试/部署安全；docs 正被另一 teammate 补齐 |

历史审计（`work/three_end_schedule_contract_audit.json`）的 G1/G2/G3：
- **G1（slot 词表无单一来源）已修复**：#255 引入 `backend/paper_schedule_contract.py`，四端全部派生自它（证据见 §3）。
- **G2（时间表无共同来源）基本修复**：`SCHEDULE_TASKS` + `render_cron_lines()` + `CronRenderingFidelityGuard` 已建立；**残留缺口 D1**（Windows 侧丢 retry）。
- **G3（schedule_status 在 Linux 只查文件存在）仍开放**，且前端根本没渲染该字段（**D4**）。

---

## 1. HTTP API 契约（已对齐）

### 方法
1. 进程内导出 OpenAPI（不启服务）：`cd backend; ..\.venv\Scripts\python.exe -c "import main,json;json.dump(main.app.openapi(),open('../work/_openapi.json','w',encoding='utf-8'),ensure_ascii=False)"` → `153 paths`。
2. 扫描 `frontend/src/**/*.js` 的 `api/apiGet/apiPost/apiPostJson/apiJson(...)` 首个参数，做括号感知解析 + 动态段归一（`'a'+expr+'b'` → `a{}b`，剥离 query），再对 OpenAPI 模板（`{param}` → `[^/]+`）做方法+路径匹配：`python work/_three_end_api_check.py`。

### 输出摘要
```
frontend api call sites (unique method+path): 96
matched: 91  unmatched: 5
```
5 条 "unmatched" 逐条人工核对，**全部是 query 拼接**而非路径漂移，属脚本归一盲区：
- `frontend/src/features/paper.js:711,738`：`'/api/paper/overview' + (…'?activity=1'…)` → `GET /api/paper/overview` ✅
- `frontend/src/features/strategies.js:924,939`：`'/api/strategies/'+id+'/challenger|candidates' + '?…'` → 对应 GET 路由 ✅
- `frontend/src/features/execution.js:138`：`'/api/paper/execution-dispatch/verify'+query` → `POST /api/paper/execution-dispatch/verify` ✅
另有 2 处变量路径（`frontend/src/features/adaptive.js:1007,1020` 的 `apiPost(query)`，query 为 `/api/adaptive/advisor/run` 与 `/api/adaptive/advisor/suite`）——两条路由均存在 ✅。

**结论：前端调用但后端不存在的 0 条；方法不匹配 0 条。**

### #255 新增能力的消费判断
| 能力 | HTTP 出口 | 前端消费 | 判定 |
| --- | --- | --- | --- |
| market data status | `/api/paper/overview` → `dashboard_queries.py:596` `"market_data": MDSvc.read_projection()` | `paper.js:373 paperMarketDataHtml()`，`paper.js:489,792` 渲染 | ✅ 已接 |
| risk audit 游标/过滤（#255 重写） | `/api/paper/risk-audit`（`api_paper.py` 新签名 `page_size/account_id/level/date/cursor`） | `risk.js:26-29` 消费 `pagination.next_cursor`；`risk.js:79-80` 消费 `pagination.total/has_next`；`risk.js:66` `scan_status` | ✅ 已接 |
| selection 上下文就绪状态 | `/api/select`（`main.py:1330` `data_status`）、`/api/data-validity`（`source_health`）、`/api/health`（`data_source_health`） | `selection.js:285 source_health`、`selection.js:337 context_pending`、`selection.js` `data_status`/`need_init` | ✅ 已接 |
| schedule_status | `/api/paper/overview` → `dashboard_queries.py:111-118,607` `"schedule": schedule` | **未消费**（`paper.js:933` 赋值后无任何引用；`paper.js:78` 只用 start 响应的 `schedule.ok`） | ⚠️ 见 **D4** |
| candidate_selection（`candidate_selection*.py`） | 无 HTTP 出口（grep `api_*.py|main.py` 0 命中；importers 仅 `candidate_selection_repository/service`、`experiment_search_*`、测试） | — | ✅ 合理未消费（无路由可消费） |
| disclosure cache（`disclosure_cache.py`） | 无 HTTP 出口（importers 仅 `disclosure_cache_migrate`、`disclosure_timeline`、测试） | — | ✅ 合理未消费（无路由可消费） |
| paper runtime facts（`paper_job_run_facts.py`） | 无 HTTP 出口；消费方是 `deepseek_research.py:585`（incident triage） | — | ✅ 合理未消费（研究侧 owner 读侧，不是 UI 能力） |

> 说明：`backend/paper_selection_runner.py` 等 runner 是 candidate_selection 的消费方（见 `backend/test_r36c_candidate_selection.py`），不是前端漏接。

---

## 2. 凭据白名单（已对齐）

```
backend/operator_auth.py:76  WRITE_METHODS    = frozenset({"POST","PUT","PATCH","DELETE"})
backend/operator_auth.py:77  READ_METHODS     = frozenset({"GET","HEAD","OPTIONS"})
frontend/src/core/api.js:55  READ_METHODS     = ['GET','HEAD','OPTIONS']
frontend/src/core/api.js:64  MUTATION_METHODS = ['POST','PUT','PATCH','DELETE']
```
逐字一致（集合 vs 数组，元素与顺序一致）。行为侧守卫：`frontend/tests/operator-credential.test.mjs` + `api-request-contract.test.mjs`（见 §5 测试结果）。

---

## 3. 调度契约

### 已对齐（G1 修复证据）
`cd backend; python -c "import paper_schedule_contract as PSch, paper_slot_service as PSS; ..."` 实测：
```
RUN_SLOTS      ('auction','open','risk','close','weekly-review','intraday')
RUNNER_ONLY    ('fast-entry',)
RUNNER_SLOTS   ('auction','open','risk','close','weekly-review','intraday','fast-entry')
HOT            ['auction','fast-entry','intraday','open','risk']
slot_service==RUN_SLOTS  True
SCHEDULED      ['auction','close','intraday','open','weekly-review']
validate_slot('fast-entry') -> ValueError（正确拒绝 runner-only slot）
PT.HOT_PATH_SLOTS==PSch  True
```
派生关系全部成立：
- `backend/paper_slot_service.py:18` `SUPPORTED_SLOTS = frozenset(PSch.RUN_SLOTS)`
- `backend/paper_runner.py:32,35` `choices=list(RUNNER_SLOTS)`
- `backend/paper_trading.py:1463` `HOT_PATH_SLOTS = PSch.HOT_PATH_SLOTS`
- `backend/paper_trading.py:15102-15106` `schedule_status` 探测 `PSch.SCHEDULE_TASK_NAMES`
- `backend/paper_trading.py:15159-15162` `install_windows_schedule` 从 `PSch.SCHEDULE_TASKS` 生成 definitions
- `deploy/astock-codex.cron` ↔ `PSch.render_cron_lines()`：`CronRenderingFidelityGuard` 展开成 (slot, dow)→触发时刻集合做语义比较，`backend/test_paper_schedule_contract.py` 16/16 通过。
- cron 的非 PSch runner（合理分工，且参数/取值全部有效）：8 个模块文件均存在，cron 里用到的 `--slot/--trigger/--session/--workers/--max-seconds/--topn/--daily` 均在对应 argparse 中定义；取值均落在 choices 内（`news_runner` premarket/noon/after-close；`adaptive_runner` midday/midday-advisor/scheduled-*；`selection_runner`/`paper_selection_runner` daily；`history_recovery_runner --workers 2 --max-seconds 780`；`evolution_loop_runner --daily`）。
- `weekly-review` 走 `docker exec astock-task-worker` 而非 `run_paper_slot.sh` 是**有意且被测试钉住**的（`test_paper_schedule_contract.py:198-215`），不算漂移。

### 漂移
**D1（中）Windows 任务注册丢失 retry 时段，与 Linux cron 不一致**
- 契约：`backend/paper_schedule_contract.py:117-128` 的 `SCHEDULE_TASKS` 为 auction/open/close 声明了 `retry_clocks`（09:27/09:29、09:33/09:35、15:10/15:12），`entries()` 明确产出这些 `(name, clock)`。
- cron：`deploy/astock-codex.cron:22-23,49-50,53-54` 如实渲染了 6 条 retry。
- Windows：`backend/paper_trading.py:15159-15167` 只取 `task.clock`（`definitions = [(task.name, task.clock, task.slot, task.days, task.repeat) …]`，`/ST clock` 单次注册），**完全忽略 `task.retry_clocks`**。Windows 主机的竞价/开盘/收盘失败兜底因此不存在。
- 守卫盲区：`test_paper_schedule_contract.py:146-153` 只断言"用了 `PSch.SCHEDULE_TASKS`、没有硬编码任务名"，未断言 retry 被渲染 → 测试全绿但行为已漂移。
- 建议（需 Lead 决策，backend 只读）：`install_windows_schedule` 改为遍历 `task.entries()`，retry 用独立任务名（如 `f"{name}-retry-{HHMM}"`）注册；或把"Windows 不注册 retry"写进契约并加断言。二选一都必须落测试。

**D5（低）`deploy/cron/astock-fast-entry` 是孤儿文件**
- `deploy/cron/astock-fast-entry:6` 定义了每 30 秒一次的 fast-entry 调度，但仓库内**没有任何安装脚本/文档引用 `deploy/cron/`**（`grep -r 'deploy/cron' deploy/` 0 命中），`sync-cron.sh` 只装 `deploy/astock-codex.cron`，`install-centos9.sh` 只装 `astock-quant.cron`。
- 结合 `fast-entry` 是 `RUNNER_ONLY_SLOTS`（不进 PSch 调度表）——该文件要么是废弃产物，要么是缺失的安装步骤。建议：删除或补安装入口，并说明 fast-entry 的调度归属。

**G3 残留（低，见 D4）**：`backend/paper_trading.py:15113-15123` Linux 分支仍然只 `os.path.isfile(schedule_file)` 就把 6 个任务名全部标记 found；"已安装" ≠ "真的在跑"。

---

## 4. 部署脚本

### 已对齐
- docker 服务名：`docker-compose.server.yml` 含 `astock`(container `astock-codex`)、`astock-data-worker`、`astock-task-worker`、`astock-paper-worker`；cron 的 `docker exec astock-task-worker|astock-data-worker` 与 `deploy/run_paper_slot.sh:31` 的 `docker exec … astock-paper-worker` 全部命中。
- 环境变量：`ASTOCK_SCHEDULE_FILE`(paper_trading.py:15115)、`ASTOCK_CACHE_DIR`(adaptive_learning_dispatch.py:19, resource_guard.py:26, strategy_trace.py:61)、`ASTOCK_WORKER_HIGH_WATER_PCT`(resource_guard.py:30)、`ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS`(paper_timing.py:96)、`ASTOCK_ENABLE_FALLBACK_THREADS`(main.py:520,524)、`ASTOCK_DEMO`/`ASTOCK_DEMO_FORCE`(demo_seed.py, main.py:513)、`ASTOCK_OPERATOR_TOKEN`(operator_auth.py:69) 均在后端有真实读取点。
- `Dockerfile`：`python:3.14-slim-bookworm`，`COPY requirements.txt requirements.lock`（`requirements.lock` 存在），`COPY frontend ./frontend`（dist 随源码进镜像），`CMD uvicorn backend.main:app --port 8600`，HEALTHCHECK 命中 `/api/health` 与 `/api/adaptive/ai/settings`（两路由均在 OpenAPI 中）。
- `deploy/deploy.sh`：`docker compose -f docker-compose.server.yml`、`db_migrate.py all`、`sync-cron.sh`、`docker inspect … astock-codex` 均与仓库现状一致。

### 漂移
**D2（高，仅影响 native-centos9 profile）`install-centos9.sh` 引用 3 个不存在的部署文件**
- `deploy/install-centos9.sh:107` → `"${APP_DIR}/deploy/astock-quant.service"`（不存在）
- `deploy/install-centos9.sh:114` → `"${APP_DIR}/deploy/astock-quant.nginx.conf"`（不存在）
- `deploy/install-centos9.sh:127` → `"${APP_DIR}/deploy/astock-quant.cron"`（不存在）
- 证据：`git ls-files deploy` 无任何 `astock-quant.*`（只有 `astock-codex.cron` / `astock-codex.nginx.conf` / `astock-codex-swap.service`）；`Test-Path deploy/astock-quant.service` = False；仓库内 `*quant*` 命名文件搜索无部署产物。脚本 `set -euo pipefail`，会在第 107 行直接失败。
- 该 profile 是**文档化的受支持 profile**（`deploy/README.md`、`README.md:8`、`CONTRIBUTING.md:18` 均声明 `native-centos9`），因此不是死代码。
- 守卫盲区：`backend/test_data_update_lease.py:53-58` 只断言脚本文本里出现 `"${APP_DIR}/deploy/astock-quant.cron"`，**不检查被引用文件是否存在** → CI 无法发现。
- 建议（需 Lead 决策；涉及新增部署文件，超出本次最小修复范围）：补齐 `astock-quant.service/.nginx.conf/.cron`（注意 native profile 文档写的是 5 分钟节奏，与 PSch 的 3 分钟不同，需要先决定是否让 native cron 也派生自 PSch），或退役该 profile 并删除脚本引用；同时把守卫改为"引用文件必须存在"。
- 另：`deploy/astock-codex-swap.service` 存在但全仓库无引用（`grep astock-codex-swap` 0 命中）——低优先级孤儿，可与 D5 一并清理。

---

## 5. 构建一致性

### 已对齐
- build id：`backend/build_info.py:25` `APP_BUILD_ID="20261012-runtime-followup"`；`frontend/src/app.js:3`、`frontend/src/boot.js:12`、`frontend/index.html:9,240`（`?v=20261012-runtime-followup`）一致。
- `cd backend; python -m unittest test_build_identity -v` → **7 tests OK**（含 `test_dist_bundle_carries_build_id`、`test_index_html_cache_bust_matches_build`、`/api/version` 断言）。
- dist 与当前 src 同步：`cd frontend; npm run build` 前后 `git diff --stat -- frontend/dist` 均为 `app.js | 4 ++--`（2 增 2 删），即重建是幂等的，dist 没有落后于当前 src。

### 漂移
**D3（中）dist 相对 HEAD 是脏的，CI `git diff --exit-code -- frontend/dist` 会红**
- 现状：`git status --short` → ` M frontend/dist/app.js`；原因不是 #255，而是**另一 teammate 正在做前端 XSS 硬化**（`frontend/src/features/{adaptive,execution,selection}.js`、`boot.js`、`core/format.js` 未提交，`frontend/dist/app.js` 已于 21:41:53 被重建）。
- 风险：前端改动与 dist 若分开提交（或只提交 src），CI 的 `.github/workflows/ci.yml:227-230` 会失败；即便侥幸合入，部署镜像里的 `/app.js` 也与 src 不一致。
- 建议：**前端 src 的 owner 在提交时同 commit 带上 `frontend/dist`**；不要由其他人代建/代提交，避免和进行中的编辑互相覆盖。

### 测试结果（当前工作区，快照时间 22:00 左右）
- 前端：`cd frontend; npm run test:unit` → **205 tests / 205 pass / 0 fail**（含 `api-request-contract`、`operator-credential`、新增的 `adaptive-ui-safety`/`format-escaping`/`selection-render-safety`）。
  - 注：21:48 曾出现一次 `execution-display.test.mjs` 失败（`document.createElement is not a function`），发生在 `frontend/src/features/execution.js` 已改、对应测试尚未落盘的并发窗口内；21:52 复跑已全绿。属并发编辑瞬时态，不是 #255 回归。
- 后端调度契约：`cd backend; python -m unittest test_paper_schedule_contract` → **16 tests OK**。
- 构建标识：`test_build_identity` → **7 tests OK**。

---

## 6. `paper_job_run_facts` 拆分（工作区未提交）

- 新模块 `backend/paper_job_run_facts.py`：只读 owner 读侧，含 `PaperJobRunFactProjection` / `paper_job_run_fact(s)` / 状态闭集。
- 依赖方向正确：`paper_trading.py:15218` 从新模块 import 并 re-export facade（`15257-15268`）；`deepseek_research.py:585` 直接 import 新模块；**无部署脚本引用旧位置**（`deploy/` 内无 `paper_job_run_facts` 命中）。
- 测试：`backend/test_runtime_incident_owner_facts.py` 新增 `test_INC_23c_...`（import 边界、只读性、facade 同一性），并同步改了 writer/reader 扫描目标。
- 文档（审计窗口内正被另一 teammate 补齐，已确认落盘）：
  - `ARCHITECTURE.md:2850`（owner matrix 行 `paper_job_run_facts PaperJobRunFactProjection`）、`ARCHITECTURE.md:2954-2955`（`INC-01 ~ INC-23c` + 读侧搬移说明）
  - `docs/REPOSITORY_LAYOUT.md:180`（模拟盘域模块清单加入 `paper_job_run_facts`）
- **D6（低）`docs/TEST_MATRIX.md` 仍无 INC 系列条目**：`grep 'INC-|runtime_incident' docs/TEST_MATRIX.md` 0 命中（整个 INC 注册表只在 `ARCHITECTURE.md:2952-2955`）。INC-23c 的"注册"位置已在 ARCHITECTURE.md，但 TEST_MATRIX 作为测试矩阵没有指向 `test_runtime_incident_owner_facts.py` 的行。建议补一行（或写明 INC 注册表在 ARCHITECTURE.md），避免下一个人只查 TEST_MATRIX 时误判无覆盖。

---

## 7. 未验证项（明确未验证 + 原因）

1. **Linux cron 实际触发、`flock` 行为、`/opt/astock-codex` 路径替换**：本机为 Windows，无 cron/systemd/docker 运行时；只做了静态比对（cron 文件 ↔ `render_cron_lines()` 由测试做语义比较）。
2. **Docker 镜像构建 / compose 起服 / HEALTHCHECK**：未执行 `docker build`、`docker compose up`（环境无 docker；且 `deploy/deploy.sh` 会改真实部署状态）。Dockerfile 与 requirements 只做了静态一致性核对。
3. **Windows `schtasks` 真实注册**：未运行 `install_windows_schedule()`（会改本机计划任务）。D1 的结论来自源码静态分析（definitions 只取 `task.clock`）。
4. **浏览器 E2E / Playwright**：未运行 `frontend/e2e`（CI 的 `browser-e2e` 作业才有浏览器），API 契约用 Node/Node 原生 test + OpenAPI 静态匹配替代。
5. **真实行情/数据源健康**：`/api/health`、`/api/data-validity` 的真实 provider 行为未触发（离线环境，且会发起外网请求）。
6. **OpenAPI 是进程内导出**（`main.app.openapi()`），不是从运行中的 HTTP 服务抓取 `/openapi.json`；两者等价（同一 FastAPI 实例），但未经过真实网络栈/中间件。

---

## 8. 需 Lead 决策

| # | 事项 | 归属 | 建议 |
| --- | --- | --- | --- |
| 1 | D1 Windows 注册丢 retry | backend | 改 `install_windows_schedule` 用 `task.entries()` 注册 retry，或把该差异写进契约+测试 |
| 2 | D2 `install-centos9.sh` 引用不存在的 3 个文件 | deploy（可改）/ 或退役 profile | 先定 native profile 去留；若保留需补文件并让 native cron 与 PSch 对齐（当前文档写 5 分钟，PSch 是 3 分钟） |
| 3 | D3 dist 与 src 必须同 commit | frontend owner | 提交前端改动时一并提交 `frontend/dist/app.js`，合并前跑 `npm run build && git diff --exit-code -- frontend/dist` |
| 4 | D4 schedule_status 已下发但前端不渲染 | frontend | 要么在模拟盘页渲染 `installed/runtime_status/runtime_failed_count/note`（顺带关闭 G3 的"界面无法区分"），要么从 payload 移除该死字段 |
| 5 | D5/D6 孤儿 cron + TEST_MATRIX 缺 INC 行 | deploy/docs | 低优先级清理 |

---

## 附：证据命令清单

```powershell
# API 契约
cd backend; ..\.venv\Scripts\python.exe -c "import main,json;json.dump(main.app.openapi(),open('../work/_openapi.json','w',encoding='utf-8'),ensure_ascii=False)"
.\.venv\Scripts\python.exe work\_three_end_api_check.py        # 96 调用点 / 91 精确匹配 / 5 query 拼接

# 凭据白名单
Select-String backend/operator_auth.py -Pattern 'WRITE_METHODS|READ_METHODS'
Select-String frontend/src/core/api.js -Pattern 'MUTATION_METHODS|READ_METHODS'

# 调度契约
cd backend; ..\.venv\Scripts\python.exe -m unittest test_paper_schedule_contract -v   # 16 OK
..\.venv\Scripts\python.exe -c "import paper_schedule_contract as PSch,paper_slot_service as PSS; ..."

# 构建
cd backend; ..\.venv\Scripts\python.exe -m unittest test_build_identity -v            # 7 OK
cd frontend; npm run build; git -C .. diff --stat -- frontend/dist                     # 重建幂等
cd frontend; npm run test:unit                                                        # 205 pass

# 部署
git ls-files deploy                        # 无 astock-quant.*
Get-ChildItem deploy -Filter 'astock-quant*'   # 空
Select-String deploy/install-centos9.sh -Pattern 'astock-quant\.'
Select-String backend/test_data_update_lease.py -Pattern 'astock-quant'   # 仅文本存在性
```
