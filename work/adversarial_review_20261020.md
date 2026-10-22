# 2026-10-20 对抗性审查与优化记录（最近改动 / 三端同步 / 前端细节 / 自进化）

## 0. 范围与方法

- **基线**：`master @ 4d4cb7b`（#255 合并）+ 工作区未提交的 `paper_job_run_facts` 拆分。
- **三端**：后端 `backend/`、前端 `frontend/src/`（构建产物 `frontend/dist/` 是提交物）、部署 `deploy/`。
- **方法**：先验证最近改动（边界/测试/ruff），再做契约盘点（OpenAPI ↔ 前端调用 ↔ 部署脚本），
  最后对前端细节与自进化链路做**可证伪**的对抗性审查：每条结论都要求最小复现或行为测试，
  修复必须配回归测试；无法本地验证的项显式标注（见 §6）。
- **并行**：5 个 teammate 分片（自进化后端 / 进化中心前端 / 纸盘前端 / 三端同步 / inline-handler 收口），
  Lead 负责 core 前端、调度契约、迁移器、文档与最终验证。

## 1. 最近改动检查

| 项 | 结论 | 证据 |
| --- | --- | --- |
| 未提交的 `paper_job_run_facts` 拆分（自 `paper_trading.py` 搬出） | **正确**：新模块只依赖 stdlib；`paper_trading` 保留 re-export facade 且返回同一类型对象；`deepseek_research` 改直连新模块 | `test_runtime_incident_owner_facts`（INC-23c 新增）50 项通过；`test_ai_research_runtime_adapter` 通过；ruff/compileall 通过 |
| 文档未同步 | 已补：`ARCHITECTURE.md` owner matrix（`paper_job_run_facts`）+ INC-23c 说明；`docs/REPOSITORY_LAYOUT.md` 模块地图（`paper_job_run_facts` / `paper_schedule_contract`）；`docs/TEST_MATRIX.md` 新增门禁表 | 本文件 §4 |
| #255 的三端影响 | HTTP/凭据/调度词表/build id 均已对齐（见 §2）；发现 1 处真实平台漂移 D1（已修） | `work/three_end_sync_20261020.md` |

## 2. 三端同步结论

已对齐（证据见 `work/three_end_sync_20261020.md`）：

- **HTTP**：进程内 OpenAPI 153 条路径 vs 前端 96 个唯一 `(method,path)` 调用点 → 缺失 0、方法错配 0。
- **凭据**：`operator_auth.py` 的 `WRITE_METHODS/READ_METHODS` 与 `core/api.js` 的 `MUTATION_METHODS/READ_METHODS` 逐字一致。
- **调度契约**：`paper_slot_service` / `paper_runner` / `schedule_status` / Windows 安装表 / cron 全部派生自
  `paper_schedule_contract.py`；`test_paper_schedule_contract` 16 项（含 cron↔渲染语义比对）通过。
- **构建身份**：`backend/build_info.py` = `app.js` = `boot.js` = `index.html` = `20261012-runtime-followup`。
- **部署**：compose 服务名、9 个 `ASTOCK_*` 环境变量、Dockerfile/requirements/healthcheck 路由均对得上。

漂移与处理：

| 编号 | 严重度 | 漂移 | 处理 |
| --- | --- | --- | --- |
| D1 | 中 | 契约给 auction/open/close 声明了 `retry_clocks`，cron 已渲染，但 `install_windows_schedule` 只取 `task.clock` → Windows 端没有失败兜底；旧守卫只查任务名抓不到 | **已修**：retry 渲染成独立任务名（`/TN` 是主键，同名 `/F` 会覆盖主任务）；新增 `WindowsRegistrationFidelityGuard` 2 项（捕获 schtasks 参数断言 retry/重复窗口） |
| D2 | 高（仅 native profile） | `install-centos9.sh` 引用 `deploy/astock-quant.{service,nginx.conf,cron}`，三者**从未进过 git**（`git log --all -- deploy/astock-quant.*` 为空）；旧脚本在装包、建 venv、改属主之后才失败，留下半装状态 | **已修（fail-fast）**：在 `dnf install/useradd/rsync` 之前预检并 `exit 3`；`NativeProfileArtifactTests` 3 项锁住"声明缺失 == 实际缺失"；`deploy/README.md` 记录状态与时间表冲突（native 5 分钟 vs 契约 3 分钟） |
| D3 | 中 | `frontend/dist` 相对 HEAD 脏 | 已统一重建（构建幂等，SHA256 复跑不变） |
| D4 | 中低 | `schedule_status` 已由 `/api/paper/overview` 下发，但前端 `paper.js` 赋值后零引用；Linux 端"已安装"只代表 cron 文件存在（G3 仍开放） | **OPEN**：需要产品决定是否在 UI 展示（直接展示"已安装"会夸大事实） |
| D5 | 低 | `deploy/cron/astock-fast-entry`、`astock-codex-swap.service` 无任何安装脚本引用（孤儿） | **OPEN**（不阻塞） |
| D6 | 低 | `docs/TEST_MATRIX.md` 无 INC 系列条目 | 已补 2026-10-20 门禁表 |

## 3. 对抗性审查发现与修复

### 3.1 自进化 / 学习链路（后端）

| 编号 | 严重度 | 问题 | 证据 | 修复 / 测试 |
| --- | --- | --- | --- | --- |
| E1 | **high** | 崩溃窗口（阶段全落盘、终态 `UPDATE` 未提交）→ 下次 `run_loop` 把**全部 5 阶段重跑**（mutate 重复产候选），`done_stages` 膨胀到 10 条，完成代被伪装 `interrupted`，耗尽 `MAX_GEN_ATTEMPTS` 后永久 `failed`，每日幂等守卫失效 | 直接调内部函数复现：`resumed=1, completed=0, interrupted=1`，`stage calls on resume: [observe,evaluate,mutate,validate,apply]` | `evolution_loop.py`：done_stages 按 `STAGES` 归一化去重；无显式 resume 时只补跑未完成阶段（全完成 → 只补终态）；`_find_pending` 对已全落盘的代不吃重试预算。`test_evolution_loop_crash_recovery`（4 项） |
| E2 | **med-high** | `pre_apply_gate` 只看每日收盘才写的 AB 快照 → 当日第二次覆盖直接放行（观察期门禁 fail-open），效果无法归因 | 修复前 `gate ALLOWED a second overlay on day-0` | `evolution_validation.py`：新增 `_active_overlay_deployed_date()`，同时读账户 params 中生效覆盖的 `applied_at/effective_date`，不足 5 个净值日即 raise。`test_evolution_validation_gate`（5 项） |
| E3 | med | 续跑时 `improvement` 拿本代自己比自己（恒 0）；纯补写路径把 `evolution_generation.intelligence_score` 覆盖成 NULL | 旧 `prev` 查询 `ORDER BY generation DESC LIMIT 1` 取到自身 | 加 `WHERE generation<?`；`ordered` 为空时保留既有快照并回读。同 E1 测试文件 |
| E4 | info | 激活门禁 / reward attribution 复核：全仓只有 `evolution_activation.py` 写 `evolution_active_params`；activate 有指纹+重跑确定性+stale/rowcount CAS+同事务副作用；reward 幂等且冲突 fail-closed | grep + 现有测试 | 无缺陷 |
| E5 | low | `evolution_activation` 对 `10**400` 级整数抛 `OverflowError` 而非 violation（拒绝型，不放行） | 代码审阅 | **OPEN**（建议先做 int 位宽判定） |

### 3.2 前端：进化中心 / AI 研究 / 执行核验

| 编号 | 严重度 | 问题 | 修复 / 测试 |
| --- | --- | --- | --- |
| F1 | **high** | R30 robustness 两个入口守卫写成 `/^\\d+$/`（匹配字面 `\d+`），任何数字 run id 都进不来 → 两个按钮永久静默失效、零请求 | 改正则 + 非法/空 id 明确提示；`adaptive-robustness` 3 项（HEAD 源码上确认失败） |
| F2 | **high** | 时间线空态把后端 `message` 裸拼进 `innerHTML` | `adaptiveEsc`；行为测试 |
| F3 | med | 切页后异步渲染写已卸载 DOM（TypeError）；概览重试 `setTimeout` 无句柄/无上限/并发叠加 | 宿主守卫 + clearTimeout + 20 次上限 + 文案转义；`adaptive-ui-safety` 7 项 |
| F4 | **med（行为）** | 人工核验用原生 prompt/confirm/alert，且**取消驳回原因仍会提交驳回**；无防重复提交 | 全走 `ui/dialog.js`；取消→零请求；`_executionVerifyInFlight` 只提交一次；5 项测试 |
| F5 | med | 缺失字段渲染成假值：`0ms`、`NaN`、`-%`、`success` 缺失显示"失败" | `adaptiveRatioPercent` / 三态 success / `adaptiveValue` 兜底 |
| F6 | **high** | inline handler 的 JS 字符串参数用 `adaptiveEsc`（HTML 实体先解码再当 JS 解析，`&#39;` 可闭合字符串）→ 注入面 9 处 | 新增 `adaptiveJsArg`（见 §3.4 L3），9 处改预计算字面量 |
| F7 | med | `renderAdaptive` 约 30 处后端字符串裸拼 innerHTML；进 class 的标识符可属性逃逸 | 统一 `adaptiveEsc`；新增 `adaptiveCssToken` 白名单（`[A-Za-z0-9_-]`）；`decision_id` 改 `encodeURIComponent`；`adaptive-render-escaping` 7 项 |

### 3.3 前端：纸盘 / 风控 / 选股

| 编号 | 严重度 | 问题 | 修复 / 测试 |
| --- | --- | --- | --- |
| P1 | **high** | `selection.js` 存储型 XSS：股票名/行业/入选理由/买入摘要/舆情原因/门控说明/信号消息/卖出摘要/免责声明裸拼 innerHTML | 全部 `adaptiveEsc`；`buy_decision/sell_decision` 缺失不再 TypeError；`selection-render-safety` 4 项 |
| P2 | med | `renderTrack` 用 `!==null` 判缺失 → 渲染 `-%`/`--%`/`undefined日`，并把"无数据"染成亏损色；`p.breaker` 缺失 TypeError | `==null → —`；防御性默认值；同 P1 测试 |
| P3 | **high** | `paper.js` 同类 XSS 面更大（L809-957，含 reason/assumption/report/disclaimer 等自由文本与外部股票名） | 新增唯一出口 `paperText`/`paperNum`，替换 94+22 处；`d=d||{}`、`plan.quote` 等缺失防护；`paper-dashboard-safety` 3 项 |
| P4 | med | 个股档案"今日盈亏"：多账户 `today_pnl` 求和却配 `positions[0]` 的百分比；部分账户缺行情仍当完整总额 | 单账户完整才显示百分比；多账户显示"N 个账户合计"；缺行情明示；`paper-stock-history-accounting` 5 项 |
| P5 | low | `risk.js` 缺值显示 `-%`；审计失败时页码丢失；下一页禁用态用请求前快照 | `pctFmt/countFmt`；保留"第 N 页"；改用当前展示快照；`risk-missing-values` 2 项 |
| P6 | med | `selection.js` 2 处原生 confirm + 5 处原生 alert（违反 dialog.js 唯一对话框层） | `confirmDialog` / `toast`，错误原样展示后端 detail |

### 3.4 Lead 侧（core / 调度 / 迁移器 / 部署 / 文档）

| 编号 | 严重度 | 问题 | 修复 / 测试 |
| --- | --- | --- | --- |
| L1 | **high** | `core/format.js` 的 `sigTag`/`sellTag` 把后端字段裸拼 innerHTML；`sellTag` 对缺失 `auction_matrix` 抛 TypeError（整行渲染崩溃）；`paperStatusTag` 未知状态未转义 | 转义 + null 安全；`format-escaping` 5 项（HEAD 源码上证明旧行为：TypeError + 注入） |
| L2 | **high** | `adaptiveJsArg` 原实现是 `encodeURIComponent`（不编码 `'`，无法用于 inline handler） | 重写为双引号属性内安全的 JS 字面量（`\u0027` 等）；eval 还原 + 不可逃逸测试 |
| L3 | med | `boot.js` 4 个人工批准/回滚入口用原生 `confirm/alert`（阻塞 JS 线程、无法样式化、与 dialog 层冲突） | 改 `confirmDialog`/`toast`，错误原样展示 detail |
| L4 | **high（Windows）** | `db_migrate.py` 用 `✓/✗` 打印迁移日志：GBK 控制台上 `UnicodeEncodeError` 会（a）在 `commit` 之后把成功报成失败，（b）在 `except` 里替换掉真实异常。CI 跑 UTF-8 Linux 抓不到 | `_console()` 编码降级；`test_db_migrate` 2 项（模拟 GBK stdout，断言迁移完成 + 真实异常不被掩盖）。**这也是全量测试在 Windows 上 19 个 error 的唯一根因** |
| L5 | **high** | `signal-audit-table.js`（不在任何 teammate 写范围）同类 XSS：股票名/代码/账户名/模型名/时间戳裸拼；另有历史编码损坏的分隔符 `" ? "`；`riskText(zhRiskText(...))` 双重转义 | 全字段转义、`" ? "` → `" · "`、去掉二次转义；`signal-audit-escaping` 4 项 |
| L6 | med | `settings.js` AI 槽位 id 裸拼进 `data-testid`/`id`/`for`/`option value`（属性注入面） | `adaptiveEsc`；`aiSlotFeedback` 选择器去除破坏字符；API 路径 `encodeURIComponent`；`settings-slot-escaping` 2 项 |
| L7 | med | `playwright.config.js` `workers: isCI ? 1 : undefined` 与注释"每个 spec 文件串行"相反：本地按 CPU 并行 → 共享同一服务/账本互相污染，实测 22 红，串行 38/38 绿 | 本地默认 1 worker（可用 `ASTOCK_E2E_WORKERS` 显式覆盖） |
| L8 | 文档 | 新模块/新门禁未进文档；前端 README 依赖表过期 | 已更新 `ARCHITECTURE.md`、`docs/REPOSITORY_LAYOUT.md`、`docs/TEST_MATRIX.md`、`frontend/src/README.md`（含 inline handler 参数规则） |

## 4. 验证证据（本机 Windows，Python 3.14，Node 26）

| 检查 | 结果 |
| --- | --- |
| 后端全量 `unittest discover -s backend`（`PYTHONIOENCODING=utf-8`） | **6049 项，OK（skipped=9），1200s**；基线同环境曾因 GBK 编码 19 errors（L4 修复后消失） |
| 前端单测 `npm run test:unit` | **235 项全绿**（基线 183，新增 52） |
| 浏览器 E2E `npx playwright test --workers=1` | **38/38 通过**（CI 等价串行；修复 L7 前本地并行跑出 22 红，基线 HEAD 也 18 红 → 本地并行不是有效门禁） |
| `ruff check backend` / `compileall` / `git diff --check` | 全部通过 |
| `frontend/dist` 构建 | 幂等（重跑 SHA256 不变），build id 与后端一致 |
| 三端契约测试 | `test_paper_schedule_contract` 18 项、`test_frontend_module_contract` 11 项、`test_build_identity` 7 项全绿 |

## 5. 剩余风险 / OPEN

> 2026-10-20 晚更新：下列第 1–4 项已在 §8 处理完毕（native 退役、schedule_status 如实展示、
> mutate 幂等 + 数值边界、孤儿部署文件）；第 5–6 项保留为后续工作。

1. **native-centos9 profile 仍不可安装**：三份 `astock-quant.*` 制品不存在；当前只是 fail-fast。恢复需先决定时间表（native 5 分钟 vs 契约 3 分钟）并补齐 service/nginx/cron。
2. `schedule_status` 前端未展示（D4）；Linux "已安装" 仍只代表 cron 文件存在。
3. `evolution_activation` 超大整数 `OverflowError`（E5）；`mutate` 的"候选已落库但阶段未记 done"仍可能重跑（SE 侧 cooldown 部分抑制，建议用 `candidate_params_id` short-circuit）。
4. 孤儿部署文件（D5）。
5. `settings.js` 仍有 `SETTINGS_STRATEGY_NAMES` 等前端常量表；策略元数据的最终来源仍是后端 registry（本轮未扩大范围）。
6. 本机全量后端测试需 `PYTHONIOENCODING=utf-8`（CI 天然 UTF-8）；迁移器已做降级，但其他模块若再引入 emoji 仍可能复现同类问题。

## 6. 无法本地验证（需 CI/服务器）

- Linux cron 实际触发与 flock 行为、docker build / compose 起服 / HEALTHCHECK、Windows `schtasks` 真实注册、真实行情 provider（会发外网）。
- OpenAPI 为进程内导出，不是 HTTP 抓取；`paper_schedule_contract` 的 cron 比对是语义展开而非真实 crontab 加载。

## 7. 复现命令

```powershell
# 后端（Windows 必须显式 UTF-8，否则 GBK 控制台编码错误）
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONDONTWRITEBYTECODE="1"
.\.venv\Scripts\python.exe -m unittest discover -s backend -p "test_*.py"

# 前端
cd frontend; npm run test:unit; npm run build
npx playwright test --project=chromium --workers=1

# 质量
.\.venv\Scripts\python.exe -m ruff check backend
```

## 8. 第二轮：§5 的 OPEN 五项全部处理（2026-10-20 晚）

### 8.1 服务器形态判定（先定事实，再决定 native 去留）

仓库内可复核的证据一致指向 **Docker profile**，不是 native：

- `docs/AUTONOMOUS_PAPER_SYSTEM_AUDIT_2026-10-15.md` 的服务器取证结论是"**四个运行容器**的
  构建版本一致"；`docker-compose.server.yml` 恰好定义四个服务（`astock-codex` /
  `astock-data-worker` / `astock-task-worker` / `astock-paper-worker`）。
- `deploy/backup.sh` 的路径探测只有 `/opt/astock-codex` 与 `/root/codex`，从不包含
  `/opt/astock-quant`——若现网跑 native，备份会一直失败。
- `deploy/astock-quant.{service,nginx.conf,cron}` 从未进入 git（`git log --all` 为空）。

→ 结论：native-centos9 **退役**，不再"补齐制品"。

### 8.2 五项修复

| 项 | 变更 | 回归测试 |
| --- | --- | --- |
| 5.1 native 退役 | `deploy/install-centos9.sh` 改为退役说明 + `exit 3`；`deploy/README.md` 重写为 Docker-only（含备份/恢复/健康检查/swap/告警）；`README.md` / `README_EN.md` / `CONTRIBUTING.md` / `docs/REPOSITORY_LAYOUT.md` 删除"native 受支持/3.11 例外"；新增 `docs/archive/NATIVE_CENTOS9_PROFILE_RETIREMENT_2026-10-20.md`（证据 + 恢复路径） | `test_repository_hygiene.NativeProfileRetirementTests`（3 项）、`test_data_update_lease.test_native_installer_is_retired` |
| 5.2 调度状态如实展示 | `schedule_status()` 默认探测 `/etc/cron.d/astock-codex`（原来探测 native 的 astock-quant → Docker 生产上 installed 永远 False），新增 `probe_mode`（`schtasks` / `file_exists`）与 `schedule_file`；`install_platform_schedule` 文案改指 `deploy/sync-cron.sh`；前端新增 `paperScheduleStatusLine()` 渲染到纸盘看板，明说"文件存在不等于 cron 已触发" | `test_paper_schedule_contract.ScheduleStatusProbeTests`（4 项）、`frontend/tests/paper-schedule-status.test.mjs`（4 项） |
| 5.3a 超大整数 | `evolution_activation._finite_number()`：`10**400` 这类 int 变成 violation（"必须是有限数值"），不再抛 `OverflowError` | `test_evolution_activation_bounds`（2 项） |
| 5.3b mutate 幂等 | `evolution_params` 新增 `loop_generation`（部分唯一索引，一代最多一条候选）；`SE.evolve/auto_evolve_if_needed` 传递代；`ProductionBackend.mutate` 先按代复用既有候选、候选一落库立刻回写 state 检查点、IntegrityError 时回读复用 | `test_evolution_loop_crash_recovery.MutateCrashWindowIdempotencyTests`（3 项：崩溃后不重复产候选 / 数据库唯一性 / evolve 记录代） |
| 5.4 孤儿部署文件 | 删除 `deploy/cron/astock-fast-entry`（`fast-entry` 是 `RUNNER_ONLY_SLOTS`，契约明确不调度，且无人安装）；`astock-codex-swap.service` 与 `health-alert.sh` 在 `deploy/README.md` 文档化（后者还修了探测端口 18600→8600 的生产误报）；新增 `DeployArtifactReferenceTests` 防止再生孤儿 | `test_repository_hygiene.DeployArtifactReferenceTests` |

### 8.3 第二轮验证

| 检查 | 结果 |
| --- | --- |
| 目标后端批次（evolution/learning/schedule/hygiene/lease/frontend-contract/build-id/migrate/runtime-facts/architecture） | **562 项 OK（skipped=1）** |
| 前端单测 `npm run test:unit` | **239 项全绿** |
| 浏览器 E2E `--workers=1` | **38/38 通过** |
| 后端全量 | `work/full_tests_utf8_20261020_final.log`：**6059 项 OK（skipped=9）** |
| ruff / compileall / `git diff --check` | 全部通过 |

### 8.4 测试隔离教训（第一次全量 3 errors 的根因）

`ScheduleStatusProbeTests` 第一版为了绕开 `test_operator_boundary` 的模块级桩
（它把 `paper_trading.schedule_status` 换成静态实现，因为 Windows 沙箱禁用
`schtasks`），用 `importlib` 以**独立模块名重新 import 整个 `paper_trading`**。
这触发了模块级 composition 副作用，污染了后续两个模块：

- `test_paper_slot_occupancy.test_production_consumers_parity`：
  `manual_orders._dependencies()` 被重新绑定到第二份模块的函数，与 patch 目标不再是同一对象；
- `test_signal_pipeline.test_SIG10b`：dashboard 读模型依赖被第二份模块覆盖，overview 投影为空。

**修复**：把探测逻辑抽成纯函数 `paper_trading._schedule_probe(names, *, os_name,
schedule_file, run, isfile)`（依赖全部可注入），`schedule_status` 只是它的薄封装；
测试直接打纯函数，不重新 import、不 patch 模块级全局状态。这也让探测逻辑本身
可被独立推理——副作用（subprocess / 文件系统）全部由调用方注入。
