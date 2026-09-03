# CentOS Stream 9 部署说明

本项目有两个互斥的部署 profile。先选定 profile，再使用对应的服务、数据目录
和调度文件；不要在同一台机器上同时运行两套调度器。

| Profile | 服务/数据目录 | 调度文件 | 日内频率 | 入口 |
| --- | --- | --- | --- | --- |
| `native-centos9` | `/opt/astock-quant`，systemd `astock-quant` | `/etc/cron.d/astock-quant` | 5 分钟 | 本文的 `install-centos9.sh` |
| `docker-compose-server` | `/root/codex`，`astock-codex`/worker 容器 | `/etc/cron.d/astock-codex` | 3 分钟 | `docker-compose.server.yml` + `astock-codex.cron` |

`install-centos9.sh` 只安装 `native-centos9`。它不会把原生 profile 变成 Docker
profile，也不会改变 8600 端口的访问或鉴权方式。Docker profile 的 3 分钟调度必须
沿用 `deploy/astock-codex.cron`，不要用本文脚本代替。

目标规格：2 核 CPU、2GB 内存、50GB 系统盘。该文档只适用于 native CentOS profile；
Docker 生产 profile 请使用 `docker-compose.server.yml` 与 `astock-codex.cron`，不要混装两套 cron。
该规格适合单人使用，但应保持
一个 Uvicorn worker；Pandas 数据与进程内缓存会在多 worker 间重复占用内存。

## 架构

- Nginx 对外监听 80，提供 Basic Auth、限流、压缩和静态首页。
- Uvicorn 仅监听 `127.0.0.1:8600`，不直接暴露到公网。
- systemd 负责开机启动、异常重启与内存上限。
- cron 按上海时区执行模拟盘日内监控、开盘、风控、收盘和周复盘。
- 数值计算线程固定为 1，避免 2 核机器上 BLAS 线程争抢。

## Profile 检查与迁移

原生安装脚本默认使用 `native-centos9`，也可以显式指定：

```bash
ASTOCK_DEPLOY_PROFILE=native-centos9 \
  sudo -E bash deploy/install-centos9.sh
```

脚本会在安装包和覆盖文件前检查 Docker profile。若发现运行中的
`astock-codex`、`astock-task-worker` 或 `astock-data-worker`，会直接停止并要求先
停止 Docker 服务；若发现 `/etc/cron.d/astock-codex`，也会要求显式确认迁移，不会
静默删除 Docker 调度。

确认从 Docker 迁移到原生 profile 时，先停止 Docker 服务并确认数据备份，再执行：

```bash
cd /root/codex
docker compose -f docker-compose.server.yml down
ASTOCK_DEPLOY_PROFILE=native-centos9 \
ASTOCK_MIGRATE_DOCKER_TO_NATIVE=1 \
  sudo -E bash deploy/install-centos9.sh
```

迁移完成后应只存在 `/etc/cron.d/astock-quant`。反向迁移时不要运行
`install-centos9.sh`，应使用 Docker compose 部署流程，并只安装
`/etc/cron.d/astock-codex`。可用下面的检查确认没有双调度：

```bash
sudo ls -l /etc/cron.d/astock-codex /etc/cron.d/astock-quant 2>/dev/null || true
docker ps --format '{{.Names}}'
```

## 首次上线

先将项目上传到服务器临时目录：

```bash
rsync -av --exclude-from=deploy/rsync-exclude.txt ./ root@服务器IP:/tmp/astock-quant/
```

在服务器执行：

```bash
cd /tmp/astock-quant
ASTOCK_ADMIN_USER=admin ASTOCK_ADMIN_PASSWORD='替换为强密码' \
  sudo -E bash deploy/install-centos9.sh
```

默认不会修改防火墙。请先在云安全组把 TCP 80 的来源限制为你自己的固定出口
IP，再执行：

```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --reload
```

不要将 8600 端口加入云安全组或 firewalld。没有域名时，HTTP Basic Auth 的登录
信息不会被 TLS 加密，因此必须使用来源 IP 白名单；准备域名后应升级为 HTTPS。

## 验证

```bash
sudo bash /opt/astock-quant/deploy/healthcheck.sh
sudo systemctl status astock-quant nginx crond
sudo journalctl -u astock-quant -n 100 --no-pager
sudo tail -n 100 /var/log/astock-quant-scheduler.log
curl -u admin:'你的密码' -I http://服务器IP/
```

持续观察资源：

```bash
systemd-cgtop
sudo journalctl -u astock-quant -f
```

## 2GB 内存机器的运行原则

- 不增加 Uvicorn worker；当前同步接口由线程池承载，选股重计算会被应用锁合并。
- 先使用现有 2GB 内存。若确认没有 swap，可额外配置 1–2GB swap 作为 OOM
  保险，但 swap 不是性能优化，不能代替内存监控。
- 首次导入全市场历史数据应在非交易时段进行，避免和模拟盘监控争抢资源。
- `MemoryHigh=900M` 会先触发回收压力，`MemoryMax=1200M` 防止单服务拖垮整机。

## 更新

上传新版本后重新运行安装脚本即可更新代码和依赖；脚本不会删除现有
`data_cache`、模拟盘数据库或报告目录。更新前仍建议备份：

```bash
sudo tar -C /opt/astock-quant -czf /root/astock-data-backup.tgz data_cache reports
```
