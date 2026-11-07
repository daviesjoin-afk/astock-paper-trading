# V1 退役记录

状态：本文件随 `codex/retire-legacy-v1` 分支提交，Draft PR 待审。合并后本仓库只维护 AStock V2（`v2/`）。

## 1. 归档基线（可恢复）

| 项目 | 值 |
| --- | --- |
| 标签 | `v1-retirement-baseline-2026-11-07`（annotated tag 对象 `82d50ea08110edaa6d24ed9f2cd1e8d19919e4f1`） |
| 指向提交 | `96a79ff64111109d5c258a965cc0dd8048f88426`（#356 合并后的 master，仍含完整 V1） |
| 远端 | 已推送：`git ls-remote --tags origin` 同时返回 tag 对象与 `^{}` 提交，说明远端可读取 |
| 是否覆盖旧标签 | 否。现有标签 `v0.2.0…v2.0.0` 均未改动；新标签名不含既有前缀 |

恢复（只读，不影响 master）：

```bash
git fetch --tags origin
git worktree add /tmp/v1-restore v1-retirement-baseline-2026-11-07
# 或按路径取文件
git show v1-retirement-baseline-2026-11-07:backend/main.py | head
```

## 2. 删除清单（当前工作树）

删除前的计数取自归档标签（`git ls-tree -r`）。

| 路径 | 删除文件 | 行数（约） | 内容 |
| --- | --- | --- | --- |
| `backend/` | 778（其中 756 个 `.py`） | 358,565 | V1 Python 业务、测试、夹具与数据 |
| `frontend/` | 96（91 个源文件） | 15,732 | V1 前端源码、测试与构建产物 |
| `deploy/` | 24 | 2,004 | systemd、cron、nginx、备份/恢复、安装与运维脚本 |
| `plans/` | 28 | 9,090 | 旧架构与轮次开发计划 |
| `docs/`（根目录） | 66 | 14,880 | V1 开发/运行/审计文档与截图 |
| `scripts/` | 25（保留 `scripts/security/` 的 4 个文件） | 6,548 | 旧运行、调度、审计与性能探针 |
| 根目录入口 | 13 | — | `Dockerfile`、`.dockerignore`、`docker-compose.yml`、`docker-compose.server.yml`、`.env.example`、`.python-version`、`pyproject.toml`、`requirements.txt`、`requirements.lock`、`start.sh`、`start.bat`、`start.ps1`、`.gitleaks.toml` |

合计：`git status --short | grep -c '^D '` = **1031** 个已跟踪文件被删除。
`CHANGELOG.md` 未删除，改写为 V1 冻结说明 + V2 变更入口。

不复制 V1 源码到 `archive/`：原始代码只在 Git 历史与归档标签中，避免形成第二份可被误引用的实现。

## 3. 保留的安全设施与仓库底座

- `scripts/security/`：`scan-sensitive-data.py`、`scan-before-push.{sh,ps1}`、`README.md`
- `.security-allowlist`：逐值放行记录（含 V1 历史的公开提交 id、公开摘要、V1 公开部署路径）
- `.github/workflows/security-leak-scan.yml`：工作树 + 全部可达历史扫描，push 与 pull_request 均触发
- `.github/workflows/v2.yml`：V2 全套检查
- `LICENSE`、`.gitattributes`、`.gitignore`、`.github/ISSUE_TEMPLATE/`、`.github/pull_request_template.md`、`opencode.json`
- `v2/`：63 个已跟踪文件（代码、数据、测试、锁文件、ADR 与文档）

`scripts/` 目录本身没有整体删除；安全扫描器与其 pre-push 包装被保留，路径仍是 `scripts/security/scan-sensitive-data.py`，与工作流引用一致。

## 4. 线上部署状态

**仓库内可确认的事实**（来自归档标签的 `deploy/README.md`、`docker-compose.server.yml`、cron 与 systemd 文件）：

- 两套互斥 profile：
  - `native-centos9`：`/opt/astock-quant`，systemd 单元 `astock-quant`，`/etc/cron.d/astock-quant`，5 分钟调度，Uvicorn 单 worker，端口 8600。
  - `docker-compose-server`：`/opt/astock-codex`，容器 `astock`、`astock-data-worker`、`astock-task-worker`、`astock-paper-worker`、`astock-candidate-worker`，`/etc/cron.d/astock-codex`，3 分钟调度。
- nginx 反代配置在 `deploy/astock-codex.nginx.conf`；备份/恢复脚本为 `deploy/backup.sh`、`deploy/restore.sh`；磁盘守护 `deploy/disk-guard.sh`；健康检查与告警 `deploy/healthcheck.sh`、`deploy/health-alert.sh`。

**未确认的事实**：本次执行环境没有任何服务器凭据或网络通道，因此**无法确认**上述哪一套（或两套）仍在运行、容器是否健康、cron 是否仍被触发、数据库是否有新写入。本轮**没有**连接服务器、没有停止任何服务、没有删除服务器数据、没有清理 Docker volume。

需要运维在合并后自行核对（见第 7 节）。

## 5. 私有数据库与备份

- V1 运行时数据库只在服务器数据目录（`/opt/astock-quant` 或 `/opt/astock-codex` 下），**未触碰**。
- 仓库从未跟踪数据库/日志/备份（`.gitignore` 与安全扫描共同保证），因此删除工作树不影响任何运行数据。
- 本地未跟踪目录 `data_cache/`（约 7.5 MB）与 `work/`（约 2.8 MB）保留未删：它们属于运行数据与历史审计产物，不是本次退役目标。
- `deploy/backup.sh` 随 `deploy/` 从分支移除；服务器上既有的备份目录与备份文件保持原样。

## 6. 旧分支与未关闭 PR

| 引用 | 类型 | 处置建议 |
| --- | --- | --- |
| #345 `codex/p1-audit-fixes`（`fix(audit): 拒绝调用方事务被隐式提交、证据按语义比较`） | 打开的非 Draft PR，改动 `backend/` | 退役后无法合并（目标文件已删除）；建议关闭并注明"V1 退役，见 `v1-retirement-baseline-2026-11-07`" |
| 远端分支 `codex/p1-audit-fixes` | 分支 | 关闭 PR 后删除分支；历史由归档标签保留 |
| 远端分支 `codex/server-runtime-audit-fixes` | 无对应打开 PR 的分支 | 建议删除，避免后续误合入 |

仓库内没有其他打开的非 V2 PR。

## 7. 未完成的运维退役事项（需人工执行）

1. 确认线上实际运行的是哪一套 profile（systemd 或 compose），不要在同一台机器同时停两套。
2. 停止调度：`systemctl disable --now astock-quant` 或 `docker compose -f docker-compose.server.yml down`（先确认），并移除 `/etc/cron.d/astock-quant` 或 `/etc/cron.d/astock-codex`。
3. 备份并归档 V1 数据库、备份目录与部署元数据；确认保留期限后再决定是否清理。
4. 撤销 8600 端口与 nginx 反代的对外暴露；清理证书、DNS 记录与监控/告警规则。
5. 检查是否还有其它机器或容器在跑 V1 调度（`systemctl list-units | grep astock`、`docker ps`、`ls /etc/cron.d`）。
6. 关闭 #345 与两个 V1 分支。
7. 确认不需要把 V1 的线上历史数据导入 V2：本轮明确**不做迁移**，V2 只使用离线合成数据包。

## 8. CI 与分支保护

- 删除 `.github/workflows/ci.yml`（V1 的 `tests`、`syntax`、`frontend`、`quality`、`docker-smoke`、`browser-e2e` 全部依赖 `backend/`、`frontend/` 或 V1 Dockerfile）。
- `.github/workflows/v2.yml` 去掉 `paths:` 过滤：任何 PR 与 master push 都运行，避免"改动不在 `v2/**` 就跳过内核测试"。
- 保留的检查：`rust-kernel`（Rust 单元测试 + release 构建 + 账本基准）、`research-contract`（Python 研究契约与重做测试）、`web-ui`（TypeScript 构建）、`integration-smoke`（真实 HTTP + SQLite + 研究重做 + 重启）、`security-leak-scan`（工作树 + 全历史）。
- 分支保护 `master` 原本要求 `syntax`、`quality`、`docker-smoke`、`tests` 四个 V1 context。删除 CI 后这些 context 不再产生，必须改为真正执行的 V2 context：`rust-kernel`、`research-contract`、`web-ui`、`integration-smoke`、`security-leak-scan`。不使用空白成功任务冒充旧检查。

## 9. 本轮未做的事

- 不部署、不停止线上服务、不删除服务器数据或 Docker volume。
- 不迁移、不兼容、不重写 V1 业务逻辑，不建立 V1 兼容层。
- 不改写 Git 历史（V1 代码在历史里保持原样，安全扫描继续覆盖它）。
- 不新增微服务、消息队列、ORM、插件框架，不同时开发策略比较、收益排行、AI 自进化、行情供应商或实盘交易。
