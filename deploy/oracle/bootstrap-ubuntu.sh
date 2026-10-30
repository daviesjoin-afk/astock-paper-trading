#!/usr/bin/env bash
# Oracle Cloud（OCI）Ubuntu 实例：一次性初始化 + 模拟盘 docker-compose-server profile 上线。
#
# 设计原则
#   1. 幂等：重复执行只补差异，不重复写文件、不重启已经健康的容器。
#   2. 分阶段：base（系统底座）/ docker（容器运行时）/ app（项目上线），可单独跑。
#   3. 不碰防火墙之外的安全边界：不关 SELinux、不改 sshd 认证方式、不开 8600 公网。
#
# 用法（服务器上，root 或 sudo）：
#   sudo -E bash bootstrap-ubuntu.sh --stage all --commit <40位SHA>
#   sudo -E bash bootstrap-ubuntu.sh --stage base
#   sudo -E bash bootstrap-ubuntu.sh --stage app --commit <40位SHA> --skip-build
#
# 环境变量（都有默认值）：
#   APP_DIR=/opt/astock-codex      项目安装目录
#   SRC_DIR=/tmp/astock-src        上传源码的暂存目录（git archive 解包）
#   TZ_NAME=Asia/Shanghai          系统时区（A股调度必须上海时区）
#   SWAP_GB=2                      交换文件大小（0 = 不建）
#   SWAP_MIN_MEM_GB=8              物理内存低于该值时建 swap
#   HTTP_ALLOW=                    允许访问 80/443 的来源；留空表示不动 80/443
#                                  （Docker profile 的看板只绑 127.0.0.1:18600，
#                                    对外靠宿主 nginx；没有域名/白名单前不要开公网）
#   SSH_ALLOW=                     允许远程登录的来源；留空表示保持镜像原有的全网规则
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/astock-codex}"
SRC_DIR="${SRC_DIR:-/tmp/astock-src}"
TZ_NAME="${TZ_NAME:-Asia/Shanghai}"
SWAP_GB="${SWAP_GB:-2}"
SWAP_MIN_MEM_GB="${SWAP_MIN_MEM_GB:-8}"
HTTP_ALLOW="${HTTP_ALLOW:-}"
SSH_ALLOW="${SSH_ALLOW:-}"
STAGE="all"
COMMIT=""
SKIP_BUILD=0
FORCE=0
MIN_APP_MEM_GB=3.2

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage) STAGE="${2:?}"; shift 2 ;;
    --commit) COMMIT="${2:?}"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

[[ "$(id -u)" == "0" ]] || die "请用 root 执行：sudo -E bash $0 ..."
[[ "$STAGE" =~ ^(base|docker|app|all)$ ]] || die "--stage 只能是 base|docker|app|all"

mem_gb() { awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo; }
disk_free_gb() { df -Pk / | awk 'NR==2 {printf "%d", $4/1024/1024}'; }

# ───────────────────────────────────────────────────────────── base ──
stage_base() {
  log "base：APT 索引与基础包"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg lsb-release apt-transport-https \
    jq rsync unzip tar cron logrotate htop tmux vim-tiny \
    iptables iptables-persistent netfilter-persistent \
    chrony locales tzdata openssh-client \
    python3 python3-venv git
  # 安全补丁：只升级已装包，不引入新内核之外的交互（Oracle 镜像自带 unattended-upgrades）
  apt-get -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" upgrade

  log "base：时区 $TZ_NAME + NTP"
  timedatectl set-timezone "$TZ_NAME" 2>/dev/null || ln -sf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
  systemctl enable --now chrony 2>/dev/null || systemctl enable --now systemd-timesyncd 2>/dev/null || true

  log "base：locale（C.UTF-8，避免中文日志乱码）"
  sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen 2>/dev/null || true
  sed -i 's/^# *zh_CN.UTF-8 UTF-8/zh_CN.UTF-8 UTF-8/' /etc/locale.gen 2>/dev/null || true
  locale-gen >/dev/null 2>&1 || true
  cat > /etc/default/locale <<'EOF'
LANG=C.UTF-8
LC_ALL=C.UTF-8
EOF

  if [[ "$SWAP_GB" != "0" ]] && ! swapon --show=NAME --noheadings | grep -q .; then
    if awk -v m="$SWAP_MIN_MEM_GB" '/MemTotal/ {exit !($2/1024/1024 < m)}' /proc/meminfo; then
      log "base：创建 ${SWAP_GB}G swap"
      fallocate -l "${SWAP_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB*1024)) status=none
      chmod 600 /swapfile
      mkswap /swapfile >/dev/null
      swapon /swapfile
      grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
      cat > /etc/sysctl.d/99-astock.conf <<'EOF'
vm.swappiness=20
vm.vfs_cache_pressure=50
vm.max_map_count=262144
net.core.somaxconn=1024
fs.file-max=200000
EOF
      sysctl --system >/dev/null
    else
      log "base：物理内存充足，跳过 swap"
    fi
  fi

  log "base：放行必要端口（OCI 镜像默认 iptables 末尾 REJECT）"
  open_port() {
    local proto="$1" dport="$2" src="$3"
    if iptables -C INPUT -p "$proto" -s "$src" --dport "$dport" -j ACCEPT 2>/dev/null; then
      return 0
    fi
    iptables -I INPUT 1 -p "$proto" -s "$src" --dport "$dport" -j ACCEPT
    log "  + $proto/$dport from $src"
  }
  open_port tcp 22 "${SSH_ALLOW:-0.0.0.0/0}"
  if [[ -n "$HTTP_ALLOW" ]]; then
    open_port tcp 80 "$HTTP_ALLOW"
    open_port tcp 443 "$HTTP_ALLOW"
  else
    log "  跳过 80/443（HTTP_ALLOW 未设置）：看板只走 127.0.0.1:18600，对外另有 nginx"
  fi
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1 || warn "iptables 规则保存失败（重启后可能丢失）"
  fi
  systemctl enable cron >/dev/null 2>&1 || true
  systemctl start cron || true

  log "base：完成（mem=$(mem_gb)G disk_free=$(disk_free_gb)G tz=$(timedatectl show -p Timezone --value 2>/dev/null || echo $TZ_NAME)）"
}

# ─────────────────────────────────────────────────────────── docker ──
stage_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "docker：已安装 $(docker --version)，跳过"
  else
    log "docker：安装官方仓库"
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
      curl -fsSL --retry 5 --retry-all-errors \
        https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    fi
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    # shellcheck disable=SC2154
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi

  log "docker：daemon 配置（日志轮转 + live-restore）"
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" },
  "live-restore": true
}
EOF
  systemctl enable docker >/dev/null 2>&1 || true
  systemctl restart docker
  usermod -aG docker ubuntu 2>/dev/null || true
  log "docker：$(docker --version) / $(docker compose version --short 2>/dev/null || echo 'compose 未知')"
}

# ────────────────────────────────────────────────────────────── app ──
stage_app() {
  mem=$(mem_gb)
  if awk -v m="$MIN_APP_MEM_GB" -v c="$mem" 'BEGIN {exit !(c < m)}'; then
    if [[ "$FORCE" != "1" ]]; then
      cat >&2 <<EOF
[FAIL] 物理内存 ${mem}G 低于模拟盘 profile 的下限 ${MIN_APP_MEM_GB}G。
       docker-compose.server.yml 四个容器上限合计约 3.5G（API 950m + data 650m
       + task 720m + paper 1250m），1G 免费 micro 实例会在盘中反复 OOM。
       换 A1.Flex（4 OCPU / 24G）或至少 E2 2C/4G 后重跑；确要强上：--force。
EOF
      exit 3
    fi
    warn "内存 ${mem}G 低于下限，按 --force 继续（盘中 OOM 风险自担）"
  fi
  [[ "$(disk_free_gb)" -ge 8 ]] || die "根分区可用不足 8G（当前 $(disk_free_gb)G），镜像构建会失败"

  [[ -d "$SRC_DIR" ]] || die "源码暂存目录不存在：$SRC_DIR（先在本地执行 oracle_ctl.py push）"
  [[ -f "$SRC_DIR/docker-compose.server.yml" ]] || die "$SRC_DIR 不像本项目源码（缺 docker-compose.server.yml）"
  command -v docker >/dev/null || die "docker 未安装，先跑 --stage docker"
  docker compose version >/dev/null 2>&1 || die "docker compose 插件缺失"

  if [[ -z "$COMMIT" ]]; then
    COMMIT="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || true)"
  fi
  [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "缺少合法 build commit（--commit 或 git rev-parse），拒绝构建不可追溯镜像"

  log "app：同步源码到 $APP_DIR（保留 data_cache/reports）"
  mkdir -p "$APP_DIR"
  # --delete 会删掉目标端多出来的文件：源码包里没有 .env（被 gitignore、也不在
  # git archive 里），所以必须显式排除宿主自己的持久配置，否则每次部署都会把
  # 线上 token/参数换成本机生成的默认值。
  rsync -a --delete \
    --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' \
    --exclude 'data_cache/' --exclude 'reports/' --exclude 'logs/' \
    --exclude 'frontend/node_modules/' --exclude '.pytest_cache/' \
    --exclude '.env' --exclude '.env.*' --exclude '.deploy-source.json' \
    "$SRC_DIR"/ "$APP_DIR"/
  mkdir -p "$APP_DIR"/{data_cache,reports,.locks}
  chown -R ubuntu:ubuntu "$APP_DIR"

  if [[ ! -s "$APP_DIR/.env" ]]; then
    log "app：生成 .env（操作员 token + env_file 指向）"
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    {
      echo ""
      echo "# --- 由 deploy/oracle/bootstrap-ubuntu.sh 生成，$(date -Iseconds) ---"
      echo "ASTOCK_ENV_FILE=./.env"
      echo "ASTOCK_OPERATOR_TOKEN=${token}"
      echo "PAPER_IGNITION_BUY=1"
      echo "ASTOCK_ENABLE_FALLBACK_THREADS=0"
    } >> "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    chown ubuntu:ubuntu "$APP_DIR/.env"
    log "app：token 已写入 $APP_DIR/.env（备份这个文件，写接口需要它）"
  else
    log "app：复用已有 .env"
  fi

  cd "$APP_DIR"
  export ASTOCK_ENV_FILE=./.env
  export ASTOCK_GIT_COMMIT="$COMMIT"
  COMPOSE=(docker compose -f docker-compose.server.yml)

  if [[ "$SKIP_BUILD" != "1" ]]; then
    log "app：构建镜像（首次约 8-20 分钟，构建 sqlite amalgamation）"
    "${COMPOSE[@]}" build --build-arg "ASTOCK_GIT_COMMIT=$COMMIT"
  fi
  log "app：启动容器"
  "${COMPOSE[@]}" up -d
  sleep 10

  log "app：schema 迁移"
  docker exec astock-codex python /app/backend/db_migrate.py all || warn "迁移返回非零（首次建库可忽略，逐条看日志）"

  log "app：安装 cron（路径随 $APP_DIR，不写死 /opt）"
  ASTOCK_DEPLOY_ROOT="$APP_DIR" bash deploy/sync-cron.sh || warn "cron 同步失败，盘中不会自动扫描"

  log "app：健康检查"
  ok=0
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18600/api/health || echo 000)"
    [[ "$code" == "200" ]] && { ok=1; break; }
    sleep 3
  done
  [[ "$ok" == "1" ]] || { docker logs astock-codex --tail 40 2>&1 || true; die "API 健康检查未通过"; }

  {
    echo "commit=$COMMIT"
    echo "image=$(docker inspect astock-codex --format '{{.Image}}' 2>/dev/null | sed 's/^sha256://')"
    echo "deployed_at=$(date -Iseconds)"
    echo "source=oracle-bootstrap"
    echo "health=healthy"
    echo "api_health=200"
  } > "$APP_DIR/.deploy-source.json"

  log "app：完成。容器："
  docker ps --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}'
  log "本机自测： curl -s http://127.0.0.1:18600/api/health"
}

case "$STAGE" in
  base)   stage_base ;;
  docker) stage_docker ;;
  app)    stage_app ;;
  all)    stage_base; stage_docker; stage_app ;;
esac
log "stage=$STAGE 结束"
