# Oracle Cloud（OCI）Ubuntu 实例：接入 + 模拟盘上线手册

本目录只解决一件事：**把一台全新的 OCI Ubuntu 实例变成能长期跑 A 股模拟盘的宿主机**，
并且让这台机器上的部署过程可复现、可回滚。

Docker profile（`docker-compose.server.yml`）是这台机器的目标形态；
`deploy/README.md` 里的 `native-centos9` 是另一台 CentOS 机器的互斥 profile，两者不要混装。

---

## 1. 首次接入的判定顺序

第一次连不上时按下面的顺序判定，**不要先怀疑密码**：SSH 密码错误一定会先有 banner、
再回 `Permission denied`；而「TCP 22 直接超时」说明报文在到达 sshd 之前就被丢了。

| 现象 | 判定 | 处理 |
| --- | --- | --- |
| `ping` 全丢 + `22/80/443` 全部 timeout | 报文在 OCI 边缘被 DROP（安全列表/NSG）或实例未运行 | 见 §2 |
| 第三方探针能连 22、只有本机超时 | 本地出口到该 IP 的路径被丢包（运营商/GFW 侧），服务器本身正常 | 走本机代理隧道，见 §1.1 |
| `22` 可连，但认证失败 `Permission denied` | 到达 sshd；Oracle 镜像默认 `PasswordAuthentication no` | 先走控制台，见 §2.4 |
| `22` 可连，登录后被 `Connection closed` | 用户不存在 / shell 被禁 / 磁盘满 | 控制台串口登录查看 |
| 能登录但外网访问 80/8600 不通 | 实例内 iptables 末尾 `REJECT`（OCI 镜像默认） | `base` 阶段会补规则 |

### 1.1 直连超时但服务器是好的：走本机代理隧道

国内出口到某些海外 IP 的 22 端口会被静默丢弃（现象与「实例已停」完全一样）。
判定方法：先用**第三方节点**探同一端口，再看本机结果。

```bash
# 第三方视角（Shodan InternetDB，无需 key）：返回 ports 数组即代表该口对公网开放
curl -s https://internetdb.shodan.io/<SERVER_IP>
# 另一路第三方探针：check-host.net 的 TCP 探针（页面为 JS 渲染，读报告页）
```

第三方能连、本机 timeout → 服务器侧无问题，是本地出口路径问题。此时用本机
Clash/Clash Verge 的混合端口做 HTTP CONNECT 隧道（**不要用网上的随机开放代理**：
它们会明文转发 SSH 会话，且随时失效）：

```ini
# 仓库根 .env.oracle（.gitignore 的 .env.* 已忽略）
ORACLE_HOST=<实例公网 IP>
ORACLE_USER=ubuntu
ORACLE_PASSWORD=<口令>
ORACLE_PROXY=127.0.0.1:7897      # Clash Verge 混合端口（mihomo 默认 7897）
ORACLE_KEY=<本机家目录>/.ssh/id_ed25519_astock   # 已有部署私钥时优先用密钥
```

`oracle_ctl.py` 会先试密钥、再试口令；两者都不通时明确报 `Authentication failed`。
注意口令里的**英文句点**是口令的一部分（`Oracle1559.` 与 `Oracle1559` 是两个口令）。


快速判定（本机执行）：

```bash
python scripts/oracle_ctl.py probe          # 端口 + banner + 系统体检
python scripts/oracle_ctl.py watch --interval 30 --timeout 3600   # 等放行，通了自动退出
```

绕过本机网络的自查：用第三方节点探同一端口（例如 check-host.net 的 TCP 探针）。
第三方也超时 → 问题在服务器侧；第三方能连、只有你超时 → 问题在本机出口或被针对性封禁。

## 2. 需要控制台做的事（只有这三件）

### 2.1 实例是否在运行

`Compute → Instances → <实例>`：状态必须是 **Running**。
Oracle 的 Always Free 实例长期低负载会被回收（stopped），停止超过保留期可能被删除；
实例一旦重建，**公网 IP 通常会变**，旧 IP 只剩黑洞路由 —— 这正是「ping 不通、端口全 timeout」
最常见的原因。

### 2.2 公网 IP 是否还挂在这台实例上

`Instance details → Attached VNICs → Primary VNIC → Public IPv4 address`。
如果你手上的 IP 和这里不一致，以控制台为准。

### 2.3 安全列表 / NSG 是否放行 22

`Networking → VCN → Security Lists → Default Security List → Add Ingress Rule`

- Source Type: `CIDR`，Source: 你的固定出口 IP + `/32`（不确定就先 `0.0.0.0/0`，打通后再收窄）
- IP Protocol: `TCP`，Destination Port Range: `22`

若实例的 VNIC 挂了 **NSG**，NSG 的规则同样要放行（NSG 与安全列表是"与"关系）。
**不要**放行 `8600`：Docker profile 的看板只绑 `127.0.0.1:18600`，对外只经宿主 nginx。

### 2.4 只认密钥的镜像怎么办

Oracle 的 Ubuntu 镜像默认 `PasswordAuthentication no`。两条路：

1. 控制台 `Console connection`（串口）进系统，编辑 `/etc/ssh/sshd_config.d/99-astock.conf`
   加 `PasswordAuthentication yes` 后 `systemctl reload ssh`；
2. 或者干脆把本机公钥塞进去：控制台无法直接改 `authorized_keys` 时，用 cloud-init /
   实例元数据重跑，或者串口登录后执行本脚本的 `key` 子命令（登录后）：

```bash
python scripts/oracle_ctl.py key      # 追加本机 ~/.ssh/id_ed25519.pub
```

拿到访问权后**先装密钥、再关密码登录**：

```bash
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
```

## 3. 规格门槛（先算内存，再装东西）

`docker-compose.server.yml` 的四个容器上限合计约 **3.5G**：

| 容器 | mem_limit |
| --- | --- |
| `astock-codex`（API/看板） | 950m |
| `astock-data-worker` | 650m |
| `astock-task-worker` | 720m |
| `astock-paper-worker` | 1250m |

- **1 OCPU / 1G 的 `VM.Standard.E2.1.Micro`** 跑不了：盘中扫描会反复 OOM，账本写入被打断。
- 目标规格：`VM.Standard.A1.Flex` **2–4 OCPU / 8–24G**（Always Free 额度内即可），
  或 `VM.Standard.E2.1.Micro` 之外的任意 2C4G 及以上。
- 磁盘：引导卷 50G 起步（镜像构建 + `data_cache` + `reports` 增长）。
- 架构：A1 是 **aarch64**。本项目镜像基于 `python:3.14-slim-bookworm`（多架构）与
  `gcc` 现场编译 sqlite amalgamation，**aarch64 可构建**，但首次构建时间明显长于 x86。

`bootstrap-ubuntu.sh --stage app` 在内存 < 3.2G 时会直接 `exit 3`，确要强上才加 `--force`。

### 3.1 已有的生产实例：`oci-main`

现网主机就是 OCI Ubuntu 24.04 / aarch64 / 2 OCPU / 11G 内存 / 96G 系统盘，
`/opt/astock-codex` 是 **git checkout**（有 `.git`），而不是 rsync 投放的目录。
更新现网要走 git 路径，**不要**用 `bootstrap-ubuntu.sh --stage app` 的 rsync 覆盖：

```bash
# 本机
cd /d/Codex/astock-paper-trading && git push origin master   # 或先合 PR
# 服务器（经 oracle_ctl.py exec）
cd /opt/astock-codex && git fetch origin && git reset --hard origin/master
ASTOCK_ENV_FILE=./.env sudo -n -E bash deploy/deploy.sh
```

`deploy/deploy.sh` 会：构建带 `ASTOCK_GIT_COMMIT=<完整 SHA>` 的镜像 → `up -d` →
`db_migrate.py all` → 同步 `/etc/cron.d/astock-codex` → 健康检查 → 写
`.deploy-source.json`。宿主 `.env`（含 `ASTOCK_OPERATOR_TOKEN`）由 compose 的
`env_file` 读入，**不要**用 `rsync --delete` 覆盖它。

现网还跑着别的容器（`napcat` / `astrbot` / `vaultwarden` / nginx），改防火墙或
`docker system prune` 前先确认它们不被波及。


## 4. 一键上线流程（本机 → 服务器）

连接信息放仓库根的 `.env.oracle`（`.gitignore` 的 `.env.*` 已忽略，永不入库）：

```ini
ORACLE_HOST=<实例公网 IP>
ORACLE_PORT=22
ORACLE_USER=ubuntu
ORACLE_PASSWORD=<控制台/邮箱里拿到的初始口令，装完密钥后建议清空>
```

```bash
# 0) 确认可达（不可达先回 §2）
python scripts/oracle_ctl.py probe

# 1) 系统底座：时区/swap/locale/iptables/基础包
python scripts/oracle_ctl.py bootstrap --stage base

# 2) 容器运行时：docker + compose 插件 + 日志轮转
python scripts/oracle_ctl.py bootstrap --stage docker

# 3) 上传源码（git archive HEAD，不带 .git/缓存/数据）
python scripts/oracle_ctl.py push

# 4) 构建镜像 + 起容器 + schema 迁移 + 装 cron + 健康检查
python scripts/oracle_ctl.py bootstrap --stage app

# 5) 复检
python scripts/oracle_ctl.py health
python scripts/oracle_ctl.py logs --lines 80
```

后续每次更新代码，只跑第 3、4 步即可（`app` 阶段用 `rsync --delete` 同步，保留
`data_cache/`、`reports/`；`.env` 不会被覆盖）。

## 5. 三个阶段的副作用清单

`base`

- `apt upgrade`（安全补丁）；安装 `iptables-persistent`、`chrony`、`rsync`、`jq`、`git` 等
- 时区 → `Asia/Shanghai`（A 股调度按上海时区，服务器在 San Jose 也必须改）
- `locale` → `C.UTF-8`（中文日志不乱码）
- 物理内存 < 8G 时建 `SWAP_GB`（默认 2G）交换文件 + `vm.swappiness=20`
- 在 `INPUT` 链**最前面**插入 ACCEPT：`22`（始终）、`80/443`（仅当 `HTTP_ALLOW` 非空），
  然后 `netfilter-persistent save`（OCI 镜像默认规则末尾是 `REJECT all`，不插就全被吃掉）

`docker`

- 官方 apt 源 + `docker-ce`/`buildx`/`compose-plugin`
- `/etc/docker/daemon.json`：`json-file` 日志 `20m × 3` + `live-restore`
- `ubuntu` 加入 `docker` 组（重新登录生效）

`app`

- `rsync -a --delete` 源码到 `/opt/astock-codex`，保留 `data_cache`/`reports`
- 首次生成 `/opt/astock-codex/.env`：`ASTOCK_ENV_FILE=./.env` +
  `ASTOCK_OPERATOR_TOKEN=<32 字节随机>`（**写接口必带这个 Bearer token，请备份**）
- `docker compose build --build-arg ASTOCK_GIT_COMMIT=<SHA>` → `up -d` →
  `db_migrate.py all` → `deploy/sync-cron.sh`（路径随实际目录，不写死 `/opt`）→ 健康检查
- 成功后写 `/opt/astock-codex/.deploy-source.json`（本次跑的提交与镜像身份）

## 6. 上线后的日常

```bash
# 看板只在服务器本机或 SSH 隧道内可达：先建隧道，再开 localhost:18600
ssh -L 18600:localhost:18600 "$ORACLE_USER@$ORACLE_HOST"
sudo bash /opt/astock-codex/deploy/healthcheck.sh
sudo bash /opt/astock-codex/deploy/preflight.sh
docker ps --format '{{.Names}}\t{{.Status}}'
tail -f /opt/astock-codex/reports/scheduler.log
```

- 调度：`/etc/cron.d/astock-codex`（每 3 分钟盘中扫描）与
  `/etc/cron.d/astock-three-strategy-start`（周期启动器），时区由系统决定。
- 数据备份：`deploy/backup.sh` 由 cron 每日收盘后跑；OCI 侧建议再给引导卷开
  块存储备份策略（`Block Volume → Backup Policies`）。
- 磁盘水位：`deploy/disk-guard.sh`；OCI 引导卷默认 50G，增长超过 70% 时扩容
  （`Block Volume → Edit → Resize`，然后在系统内 `growpart` + `resize2fs`）。

## 7. 安全边界（别让步）

1. `18600` 只绑环回，永不进安全列表；对外访问走宿主 nginx（`deploy/astock-codex.nginx.conf`）
   或 SSH 隧道。
2. 写接口必须有 `ASTOCK_OPERATOR_TOKEN`；`local-only` 模式只适用于纯本机。
3. SSH 用密钥 + 收窄来源 IP；`fail2ban`（可选）：
   `apt-get install -y fail2ban && printf '[sshd]\nenabled=true\n' > /etc/fail2ban/jail.d/sshd.local`。
4. Oracle 免费实例的出站 25 端口被封（发不了邮件），告警走 webhook 而不是 SMTP。
5. `.env` 权限 600，只 root/ubuntu 可读；不要 commit，不要贴群。

## 8. 排障速查

| 症状 | 先看 |
| --- | --- |
| 22 timeout | §2.1/§2.3；实例是否 Running、IP 是否变更 |
| 登录后立刻断开 | `df -h` 磁盘满 / `journalctl -u ssh` |
| `docker compose build` 在 sqlite 阶段卡住 | 构建期要下 `sqlite.org` 源码包，OCI 到该站较慢，Dockerfile 已内置 6 次重试 |
| 容器起来但 `/api/health` 000 | `docker logs astock-codex --tail 50`；端口是否被别的进程占用 |
| 盘中不扫描 | `ls -l /etc/cron.d/astock-codex`、`systemctl status cron`、`tail reports/scheduler.log` |
| 看板 403（写操作） | 请求头没带 Bearer 凭据（见 §7 第 2 条），或 Origin 与 Host 不同源 |
| 内存 OOM（`dmesg -T | grep -i oom`） | 规格不够，见 §3；或临时停 `astock-paper-worker` |
