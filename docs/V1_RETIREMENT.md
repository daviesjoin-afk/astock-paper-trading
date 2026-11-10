# V1 退役记录

状态：**已合并**。退役变更由 [PR #357](https://github.com/daviesjoin-afk/astock-paper-trading/pull/357) 以 Merge Commit 合入 `master`，本文件随后补记合并 SHA、未合并分支的独立归档与收尾结果。

## 1. 基线

| 项目 | 值 |
| --- | --- |
| 退役合并提交 | `b553aba027b0a049b8c5a83b407a7c6a0667d6fb`（#357 Merge Commit，父提交 `96a79ff` + `43268e1`） |
| 当前 master | `b553aba027b0a049b8c5a83b407a7c6a0667d6fb` |
| 退役前 master | `96a79ff64111109d5c258a965cc0dd8048f88426`（#356 合并后、仍含完整 V1） |
| V1 主归档标签 | `v1-retirement-baseline-2026-11-07`（annotated tag 对象 `82d50ea08110edaa6d24ed9f2cd1e8d19919e4f1`）→ 提交 `96a79ff64111109d5c258a965cc0dd8048f88426` |
| 远端可读 | 是：`git ls-remote --tags origin` 同时返回 tag 对象与 `^{}` 提交 |
| 合并后 CI | `rust-kernel`、`research-contract`、`web-ui`、`integration-smoke`、`security-leak-scan` 全部 success |

主归档标签**只保存退役前 master 的 V1 状态**，不包含任何未合并分支的独有提交；未合并分支由第 2 节的独立标签保存。

## 2. 未合并 V1 分支的独立归档

这两个分支从未合入 master，主归档标签覆盖不到它们；已按各自 HEAD 建立不可覆盖的 annotated tag，并在标签对象中写明"V1 已退役、不要合回"。

| 分支 | 分支 HEAD | 归档标签 | tag 对象 | 与 master 关系 |
| --- | --- | --- | --- | --- |
| `codex/p1-audit-fixes`（原 PR #345） | `e2e891f9d359b0e6aa5cc2b3be03c5a89f3a70cc` | `v1-archive-p1-audit-fixes-2026-11-07` | `961b53d17506448112302a07f6a7ec39641cf136` | diverged，ahead 2 / behind 52 |
| `codex/server-runtime-audit-fixes`（无打开 PR） | `ef7b0edf3a21b36018d742c8b2f1c42c3225cadd` | `v1-archive-server-runtime-audit-fixes-2026-11-07` | `5d4c58c38235b814b2f5cf81ac0d30cefb4a60fe` | diverged，ahead 3 / behind 694 |

两个标签的对象与 peeled commit 已通过 GitHub API 交叉核对（tag → commit 一致），分支 HEAD 提交在远端仍可访问，独有提交可独立恢复：

```bash
git fetch --tags origin
git worktree add /tmp/v1-pr345 v1-archive-p1-audit-fixes-2026-11-07
git worktree add /tmp/v1-runtime v1-archive-server-runtime-audit-fixes-2026-11-07
```

归档验证完成后才删除两个远端分支；删除分支不影响标签与提交对象。

## 3. 删除清单（#357）

删除前的计数取自主归档标签（`git ls-tree -r`）。

| 路径 | 删除文件 | 行数（约） | 内容 |
| --- | --- | --- | --- |
| `backend/` | 778（756 个 `.py`） | 358,565 | V1 Python 业务、测试、夹具与数据 |
| `frontend/` | 96（91 个源文件） | 15,732 | V1 前端源码、测试与构建产物 |
| `deploy/` | 24 | 2,004 | systemd、cron、nginx、备份/恢复、安装与运维脚本 |
| `plans/` | 28 | 9,090 | 旧架构与轮次开发计划 |
| `docs/`（根目录） | 66 | 14,880 | V1 开发/运行/审计文档与截图 |
| `scripts/` | 25（保留 `scripts/security/` 4 个） | 6,548 | 旧运行、调度、审计与性能探针 |
| 根目录入口 | 13 | — | `Dockerfile`、`.dockerignore`、`docker-compose.yml`、`docker-compose.server.yml`、`.env.example`、`.python-version`、`pyproject.toml`、`requirements.txt`、`requirements.lock`、`start.sh`、`start.bat`、`start.ps1`、`.gitleaks.toml` |

合计 **1031 个已跟踪文件删除**；#357 合并差异为 1042 files changed, 300 insertions(+), 414634 deletions(-)。`CHANGELOG.md` 未删除，改写为 V1 冻结说明 + V2 变更入口。

不复制 V1 源码到 `archive/`：原始代码只在 Git 历史与上述归档标签中，避免形成第二份可被误引用的实现。

## 4. 保留的安全设施与仓库底座

- `scripts/security/`：`scan-sensitive-data.py`、`scan-before-push.{sh,ps1}`、`README.md`
- `.security-allowlist`：逐值放行记录（含 V1 历史的公开提交 id、公开摘要、公开部署路径、本轮的归档 tag 对象 id）
- `.github/workflows/security-leak-scan.yml`：工作树 + 全部可达历史扫描，push 与 pull_request 均触发
- `.github/workflows/v2.yml`：V2 全套检查（已去掉 `paths:` 过滤）
- `LICENSE`、`.gitattributes`、`.gitignore`、`.github/ISSUE_TEMPLATE/`、`.github/pull_request_template.md`、`opencode.json`
- `v2/`：63 个已跟踪文件（代码、数据、测试、锁文件、ADR 与文档）

## 5. 线上部署状态

**已确认（仓库事实，取自归档标签）**

- 两套互斥 profile：
  - `native-centos9`：`/opt/astock-quant`，systemd 单元 `astock-quant`，`/etc/cron.d/astock-quant`，5 分钟调度，Uvicorn 单 worker，端口 8600。
  - `docker-compose-server`：`/opt/astock-codex`，容器 `astock`、`astock-data-worker`、`astock-task-worker`、`astock-paper-worker`、`astock-candidate-worker`，`/etc/cron.d/astock-codex`，3 分钟调度。
- nginx 反代 `deploy/astock-codex.nginx.conf`；备份/恢复 `deploy/backup.sh`、`deploy/restore.sh`；磁盘守护 `deploy/disk-guard.sh`；健康检查与告警 `deploy/healthcheck.sh`、`deploy/health-alert.sh`。

**未确认（本执行环境无法访问）**

- 上述哪一套（或两套）仍在运行、容器是否健康、cron 是否仍被触发、数据库是否仍写入、是否有其它机器在拉取/部署。
- 本次执行环境没有服务器地址、凭据或网络通道；**未连接服务器、未停止任何服务、未删除服务器数据、未清理 Docker volume、未改网络配置**。

**仓库合并 ≠ 线上退役**：线上状态以运维只读核查结果为准，见第 9 节。

## 6. 私有数据库与备份

- V1 运行时数据库只在服务器数据目录（`/opt/astock-quant` 或 `/opt/astock-codex` 下），**未触碰**。
- 仓库从未跟踪数据库/日志/备份（`.gitignore` 与安全扫描共同保证），删除工作树不影响任何运行数据。
- 本地未跟踪目录 `data_cache/`（约 7.5 MB）与 `work/`（约 2.8 MB）保留未删：属于运行数据与历史审计产物，不是本次退役目标。
- `deploy/backup.sh` 随 `deploy/` 从分支移除；服务器上既有备份目录与备份文件保持原样。

## 7. 旧 PR、旧分支与残留引用

### 已处置

| 引用 | 处置 |
| --- | --- |
| PR #345 `codex/p1-audit-fixes` | 已关闭，关闭说明含归档标签与恢复命令 |
| 分支 `codex/p1-audit-fixes` | 归档标签核验后已删除 |
| 分支 `codex/server-runtime-audit-fixes` | 归档标签核验后已删除 |

### 仍存在的 V1 时代分支（未授权删除，需决定）

| 分支 | 与 master | 触及顶层目录 |
| --- | --- | --- |
| `codex/earnings-disclosure-production` | diverged, ahead 11 / behind 249 | backend, deploy, docs, frontend, requirements |
| `codex/execution-verification-consumption` | diverged, ahead 2 / behind 640 | backend, work |
| `codex/large-file-split` | diverged, ahead 4 / behind 249 | backend, deploy, docs, frontend, plans |
| `codex/paper-decision-audit-phase2` | diverged, ahead 1 / behind 733 | tools |
| `codex/r26-simulation-execution-fidelity-20260923` | diverged, ahead 1 / behind 495 | backend, docs, frontend |
| `codex/r36c-selection-iteration-policy` | diverged, ahead 1 / behind 250 | backend, docs, plans |
| `codex/scan-perf-bench` | diverged, ahead 1 / behind 110 | backend |
| `codex/tencent-market-fallback` | diverged, ahead 22 / behind 250 | backend, deploy, docs, frontend |
| `codex/v2-pit-state` | diverged, ahead 4 / behind 38 | `.github`, `v2`（V2 分支，不是 V1） |

这些分支未删除。若要与两个已归档分支同样处理，先按 HEAD 建归档标签、核验后再删；直接向 master 提 PR 会因文件不存在和必需检查（V2 五项）而无法合并。

### 残留引用扫描（当前工作树）

`git grep -n -I -E "backend\.main|from backend|import backend|frontend/src|uvicorn|fastapi|requirements\.(txt|lock)|docker-compose|ASTOCK_SCHEDULE_FILE|paper_trading"`：

- 仅命中本文件（描述被删内容的记录本身）与 `scripts/security/scan-sensitive-data.py` 的 `SAFE_HOSTS` 公开文档域名常量。
- `git grep -- v2` 对 `backend|frontend|deploy|plans` 为空：V2 不导入、不启动、不调用 V1。
- 新克隆只依赖 Rust 1.99.1（`v2/rust-toolchain.toml` + `Cargo.lock`）、Python 标准库、Node（`v2/web/package-lock.json`）。

## 8. CI 与分支保护

- `.github/workflows/ci.yml` 已删除（`tests`、`syntax`、`frontend`、`quality`、`docker-smoke`、`browser-e2e` 全部依赖 V1 代码或 V1 Dockerfile）。
- `.github/workflows/v2.yml` 已去掉 `paths:` 过滤：任何 PR 与 master push 都运行。
- 保留检查：`rust-kernel`、`research-contract`、`web-ui`、`integration-smoke`、`security-leak-scan`（真实执行，无占位任务）。
- `master` 分支保护已从 V1 的 `syntax`/`quality`/`docker-smoke`/`tests` 更新为上述 5 个 V2 context（`strict: true`、`enforce_admins: true`）。

## 9. 未完成的运维退役事项（需人工执行）

1. 确认线上实际运行的是哪一套 profile（systemd 或 compose），不要在同一台机器同时停两套。
2. 停止调度：`systemctl disable --now astock-quant` 或 `docker compose -f docker-compose.server.yml down`（先确认），并移除 `/etc/cron.d/astock-quant` 或 `/etc/cron.d/astock-codex`。
3. 备份并归档 V1 数据库、备份目录与部署元数据；确认保留期限后再决定是否清理。
4. 撤销 8600 端口与 nginx 反代的对外暴露；清理证书、DNS 记录与监控/告警规则。
5. 检查是否还有其它机器或容器在跑 V1 调度（`systemctl list-units | grep astock`、`docker ps`、`ls /etc/cron.d`）。
6. 处置第 7 节列出的 8 个 V1 时代分支（先归档后删除）。
7. 确认不需要把 V1 的线上历史数据导入 V2：本轮明确**不做迁移**，V2 只使用离线合成数据包。

## 10. 恢复方法

```bash
git fetch --tags origin
git worktree add /tmp/v1-restore v1-retirement-baseline-2026-11-07        # 退役前 master（含完整 V1）
git worktree add /tmp/v1-pr345 v1-archive-p1-audit-fixes-2026-11-07       # 未合并审计修复
git worktree add /tmp/v1-runtime v1-archive-server-runtime-audit-fixes-2026-11-07
# 或按路径取文件
git show v1-retirement-baseline-2026-11-07:backend/main.py | head
```

## 11. 本轮未做的事

- 不部署、不停止线上服务、不删除服务器数据或 Docker volume、不改网络配置。
- 不迁移、不兼容、不重写 V1 业务逻辑，不建立 V1 兼容层。
- 不改写 Git 历史（V1 代码在历史里保持原样，安全扫描继续覆盖它）。
- 不新增微服务、消息队列、ORM、插件框架，不同时开发策略比较、收益排行、AI 自进化、行情供应商或实盘交易。
