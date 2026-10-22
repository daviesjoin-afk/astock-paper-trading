# 服务器部署说明（Docker profile）

本仓库的服务器部署只有**一个受支持的 profile**：`docker-compose-server`。
`native-centos9` 原生 profile 已于 **2026-10-20 退役**，`install-centos9.sh` 只保留
fail-fast 说明（`exit 3`），不再安装任何东西。退役证据与恢复路径见
[`docs/archive/NATIVE_CENTOS9_PROFILE_RETIREMENT_2026-10-20.md`](../docs/archive/NATIVE_CENTOS9_PROFILE_RETIREMENT_2026-10-20.md)。

| Profile | 服务/数据目录 | 调度文件 | 日内频率 | 入口 |
| --- | --- | --- | --- | --- |
| `docker-compose-server`（唯一受支持） | `/opt/astock-codex`，容器 `astock-codex` / `astock-data-worker` / `astock-task-worker` / `astock-paper-worker` | `/etc/cron.d/astock-codex` | 3 分钟 | `docker-compose.server.yml` + `deploy/sync-cron.sh` |
| `native-centos9`（**已退役**） | ~~`/opt/astock-quant`，systemd `astock-quant`~~ | ~~`/etc/cron.d/astock-quant`~~ | ~~5 分钟~~ | ~~`install-centos9.sh`~~（现在只 fail-fast） |

## 架构

- Nginx（宿主机或容器侧）对外提供 Basic Auth、限流、压缩与静态首页；
- Uvicorn 只监听回环/容器网络，不直接暴露公网；
- 容器由 compose 管理：API、数据 worker、任务 worker、paper worker 各自独立 cgroup；
- cron 按上海时区执行模拟盘日内监控、开盘、风控、收盘、周复盘与自进化闭环；
- 数值计算线程固定为 1，避免小机器上 BLAS 线程争抢。

## 首次上线 / 更新

```bash
# 1) 上传源码到服务器（保留 .git 可选；构建身份用 ASTOCK_GIT_COMMIT 显式传入）
rsync -av --exclude-from=deploy/rsync-exclude.txt ./ root@服务器IP:/opt/astock-codex/

# 2) 构建并启动
cd /opt/astock-codex
docker compose -f docker-compose.server.yml up -d --build

# 3) 同步 cron（模板里的路径跟随实际部署根目录；备份不会留在 /etc/cron.d）
sudo bash deploy/sync-cron.sh
```

更新时重复第 2、3 步即可；`deploy/deploy.sh` 把「备份 → 构建 → 重启 → schema 迁移 →
cron 同步 → 健康检查」串成一条流程。备份/恢复/健康检查脚本：

```bash
sudo bash deploy/backup.sh --keep 7     # SQLite 在线备份 + 压缩 + 清单校验
sudo bash deploy/restore.sh <backup>    # 还原前完整性检查，失败恢复
sudo bash deploy/healthcheck.sh         # curl 127.0.0.1:8600/api/health
```

## 调度契约（三端单一事实来源）

`backend/paper_schedule_contract.py` 是 slot 词表、热路径、任务表与 cron 渲染的唯一来源：

- `deploy/astock-codex.cron` 是契约的渲染结果，`deploy/sync-cron.sh` 负责安装；
- 盘后自进化闭环由 `deploy/run_evolution_loop.sh` 串行化（内置 `flock -w 180`，
  cron 在 16:45 / 17:05 / 17:35 三个窗口重试；runner 自带交易日与"当天已完成"守卫）；
- Windows 本地模式由 `paper_trading.install_windows_schedule` 从同一张表注册
  （含 `retry_clocks` 的独立重试任务）；
- 改调度**只改契约模块**，然后同步 cron 文件；`backend/test_paper_schedule_contract.py`
  会把两边展开成具体触发时刻做语义比对，并断言 Windows 侧渲染了同样的重试。

`/api/paper/overview` 下发的 `schedule_status` 同时给出 `probe_mode`：
Windows 是逐任务真实探测（`schtasks`），Linux 只能证明调度文件存在（`file_exists`）。
界面按这个口径措辞，不把"文件存在"说成"已安装运行"。

## 2GB 内存机器的运行原则

- 不增加 Uvicorn worker；选股重计算由应用锁合并。
- 若确认没有 swap，可额外配置 1–2GB swap 作为 OOM 保险；swap 不是性能优化。
- 仓库提供可选的 swap 单元 [`astock-codex-swap.service`](astock-codex-swap.service)：
  它 `swapon /opt/astock-codex/.swapfile`（需先自行 `fallocate` 创建该文件），
  在 `docker.service` 之前生效。启用方式：

  ```bash
  sudo install -m 0644 deploy/astock-codex-swap.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now astock-codex-swap
  ```

- 首次导入全市场历史数据应在非交易时段进行，避免与模拟盘监控争抢资源。
- `MemoryHigh=900M` / `MemoryMax=1200M` 的容器内存上限见 `docker-compose.server.yml`。

## 验证

```bash
docker compose -f docker-compose.server.yml ps
curl -fsS http://127.0.0.1:8600/api/health
sudo ls -l /etc/cron.d/astock-codex
sudo tail -n 100 /opt/astock-codex/reports/scheduler.log
```

可选健康告警（磁盘 / 内存 / 容器健康 / API / 数据库膨胀 / scheduler 错误）：

```bash
sudo bash deploy/health-alert.sh      # 0=健康，1=有告警；告警写入 reports/health-alert.log
# 探测地址可用 ASTOCK_HEALTH_URL 覆盖（默认 http://127.0.0.1:8600/api/health）
```

可以把它挂到 cron，例如每 10 分钟一次：`*/10 * * * * root /opt/astock-codex/deploy/health-alert.sh`。

## 安全边界

- 服务器模式必须配置 `ASTOCK_OPERATOR_TOKEN`（>= 8 字符）；未配置时只有本机环回 +
  本地 Host 可写。反向代理必须保留 `host:port`（nginx 用 `$http_host`）。
- 云安全组只放行你自己的固定出口 IP 访问对外端口；没有 TLS 时 Basic Auth 凭据不会加密。
