# native-centos9 原生部署 profile 退役记录（2026-10-20）

## 结论

`native-centos9` 原生部署 profile **退役**。本仓库的服务器部署只有 `docker-compose-server`
一个受支持 profile。`deploy/install-centos9.sh` 保留为 fail-fast 说明（`exit 3`），不再安装任何东西。

## 决策证据（可复核）

1. **生产实际形态是 Docker**：`docs/AUTONOMOUS_PAPER_SYSTEM_AUDIT_2026-10-15.md` 的服务器取证
   结论是"四个运行容器的构建版本一致"；`docker-compose.server.yml` 恰好定义四个服务
   （`astock-codex` / `astock-data-worker` / `astock-task-worker` / `astock-paper-worker`）。
2. **native 制品从未进入仓库**：`git log --all -- deploy/astock-quant.service`、
   `deploy/astock-quant.nginx.conf`、`deploy/astock-quant.cron` 均为空；安装脚本却引用它们，
   旧行为会在装完包、建完 venv、改完属主之后才失败，留下半装状态。
3. **运维脚本也不认识 native 路径**：`deploy/backup.sh` 的路径探测只有
   `/opt/astock-codex` 与 `/root/codex`，从不包含 `/opt/astock-quant`。
4. **运行时与时间表都与当前契约冲突**：该 profile 固定 Python 3.11（canonical 3.14 的
   legacy 例外），并用 5 分钟日内频率；`backend/paper_schedule_contract.py` 与
   `deploy/astock-codex.cron` 是 3 分钟 + 固定 retry。恢复它就意味着三端调度契约重新漂移。

## 本次变更

- `deploy/install-centos9.sh`：改为退役说明 + `exit 3`（在改动系统之前退出）。
- `deploy/README.md`：只描述 Docker profile；补充可选 swap 单元说明。
- `README.md` / `CONTRIBUTING.md` / `docs/REPOSITORY_LAYOUT.md`：删除"native 是 Python 3.11
  例外/受支持"的表述。
- `deploy/cron/astock-fast-entry`：删除（孤儿文件：`fast-entry` 是 `RUNNER_ONLY_SLOTS`，
  契约明确不调度它，也没有任何安装脚本引用该 cron）。
- `backend/test_repository_hygiene.py`：用 `NativeProfileRetirementTests` 取代
  `NativeProfileArtifactTests`，锁住"安装器必须退役、不得再出现 native 安装步骤"。
- `backend/test_data_update_lease.py`：删除针对旧安装器的 cron 清理断言，改为退役守卫。

## 恢复路径（仅当确实存在现网 native 主机）

不要重写制品。先只读盘点现网：

```bash
systemctl cat astock-quant
cat /etc/nginx/conf.d/astock-quant.conf
cat /etc/cron.d/astock-quant
ls -l /opt/astock-quant/deploy/
```

以现网实际文件为**唯一来源**回填仓库，并先决定时间表：

- 方案 A（推荐）：把现网迁移到 Docker profile（数据目录做一次备份/恢复演练），
  然后删除 native 主机上的 systemd/cron；
- 方案 B：保留 native，但必须把它的 5 分钟表改成与 `paper_schedule_contract` 一致的
  3 分钟 + retry，并让安装器从契约渲染 cron，而不是手写。

无论哪种方案，恢复时都要把 `NativeProfileRetirementTests` 改成对应的支持性守卫，
不允许出现"文档说支持、仓库里却没有制品"的中间态。
