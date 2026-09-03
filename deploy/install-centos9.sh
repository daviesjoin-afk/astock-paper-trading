#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 运行：sudo bash deploy/install-centos9.sh" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/astock-quant"
APP_USER="astock"
APP_GROUP="astock"
PASSWORD_FILE="/etc/nginx/.astock-quant.htpasswd"

# This script is the explicit native-CentOS profile.  Docker deployments must
# use docker-compose.server.yml and astock-codex.cron; refusing an ambiguous
# profile prevents installing the legacy scheduler by accident.
if [[ "${ASTOCK_DEPLOY_PROFILE:-native}" != "native" ]]; then
  echo "本脚本仅支持 native CentOS 部署；Docker 请使用 docker-compose.server.yml" >&2
  exit 2
fi

# This script is deliberately the native CentOS profile.  The Docker profile
# has a different image, data directory, worker split and 3-minute scheduler;
# it is deployed from /root/codex and must never be mixed with this service.
# Keep the historical default (native) for existing installations, but make
# an accidental Docker invocation fail before packages or files are changed.
DEPLOY_PROFILE="${ASTOCK_DEPLOY_PROFILE:-native-centos9}"
case "${DEPLOY_PROFILE}" in
  native|native-centos9)
    DEPLOY_PROFILE="native-centos9"
    ;;
  docker|docker-compose|docker-compose-server)
    echo "拒绝：ASTOCK_DEPLOY_PROFILE=${DEPLOY_PROFILE} 属于 Docker profile。" >&2
    echo "请使用 Docker 部署流程；本脚本只安装 native-centos9 profile。" >&2
    exit 2
    ;;
  *)
    echo "未知 ASTOCK_DEPLOY_PROFILE=${DEPLOY_PROFILE}；可选值：native-centos9。" >&2
    exit 2
    ;;
esac

# A Docker cron file left behind on a host is still an active deployment
# choice, even when its containers are currently stopped.  Do not silently
# remove it: an explicit migration acknowledgement is required.  Running
# Docker containers are always a hard stop because the two applications can
# otherwise share the same data/workflow under different service managers.
DOCKER_CRON_PRESENT=0
for docker_cron in /etc/cron.d/astock-codex /etc/cron.d/astock-codex.cron; do
  if [[ -f "${docker_cron}" ]]; then
    DOCKER_CRON_PRESENT=1
    break
  fi
done
if [[ "${DOCKER_CRON_PRESENT}" == "1" && "${ASTOCK_MIGRATE_DOCKER_TO_NATIVE:-0}" != "1" ]]; then
  echo "拒绝：检测到 Docker 调度文件（/etc/cron.d/astock-codex）。" >&2
  echo "若确认切换到 native-centos9，请先停止 Docker profile，并设置 ASTOCK_MIGRATE_DOCKER_TO_NATIVE=1 重试。" >&2
  exit 2
fi
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' \
      | grep -Eq '^(astock-codex|astock-task-worker|astock-data-worker)$'; then
    echo "拒绝：检测到运行中的 Docker profile 容器。请先停止 Docker profile，再安装 native-centos9。" >&2
    exit 2
  fi
fi

dnf install -y python3.11 python3.11-pip nginx httpd-tools rsync cronie curl openssl

if ! getent group "${APP_GROUP}" >/dev/null; then
  groupadd --system "${APP_GROUP}"
fi
if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${APP_GROUP}" --home-dir /var/lib/astock \
    --create-home --shell /sbin/nologin "${APP_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
if [[ "${SOURCE_DIR}" != "${APP_DIR}" ]]; then
  rsync -a --exclude-from="${SOURCE_DIR}/deploy/rsync-exclude.txt" \
    "${SOURCE_DIR}/" "${APP_DIR}/"
fi

python3.11 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip wheel
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 \
  "${APP_DIR}/data_cache" "${APP_DIR}/reports" /var/lib/astock/locks
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}/data_cache" "${APP_DIR}/reports"
chown -R root:"${APP_GROUP}" "${APP_DIR}/backend" "${APP_DIR}/deploy"
chmod -R g=rX,o= "${APP_DIR}/backend" "${APP_DIR}/deploy"
chown -R root:nginx "${APP_DIR}/frontend"
chmod -R u=rwX,g=rX,o= "${APP_DIR}/frontend"

install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/astock-quant.service" /etc/systemd/system/astock-quant.service
install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/astock-quant.nginx.conf" /etc/nginx/conf.d/astock-quant.conf
# This installer is the native CentOS profile.  After the preflight above has
# made a Docker-to-native migration explicit, remove both profile names before
# installing exactly one scheduler.  The Docker profile is installed by its
# own deployment flow from astock-codex.cron.
for old_cron in \
  /etc/cron.d/astock-codex \
  /etc/cron.d/astock-codex.cron \
  /etc/cron.d/astock-quant \
  /etc/cron.d/astock-quant.cron; do
  rm -f -- "${old_cron}"
done
install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/astock-quant.cron" /etc/cron.d/astock-quant
touch /var/log/astock-quant-scheduler.log
chown "${APP_USER}:${APP_GROUP}" /var/log/astock-quant-scheduler.log

GENERATED_PASSWORD=0
if [[ -n "${ASTOCK_ADMIN_PASSWORD:-}" ]]; then
  printf '%s\n' "${ASTOCK_ADMIN_PASSWORD}" \
    | htpasswd -i -c "${PASSWORD_FILE}" "${ASTOCK_ADMIN_USER:-admin}"
elif [[ ! -f "${PASSWORD_FILE}" ]]; then
  ASTOCK_ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
  GENERATED_PASSWORD=1
  printf '%s\n' "${ASTOCK_ADMIN_PASSWORD}" \
    | htpasswd -i -c "${PASSWORD_FILE}" "${ASTOCK_ADMIN_USER:-admin}"
fi
chmod 0640 "${PASSWORD_FILE}"
chown root:nginx "${PASSWORD_FILE}"

timedatectl set-timezone Asia/Shanghai
nginx -t
systemctl daemon-reload
systemctl enable --now crond
systemctl enable --now astock-quant
systemctl enable --now nginx

if [[ "${OPEN_FIREWALL:-0}" == "1" ]] && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=http
  firewall-cmd --reload
fi

for _ in {1..30}; do
  if curl --fail --silent --max-time 3 \
    http://127.0.0.1:8600/api/health >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8600/api/health
printf '\n'
systemctl --no-pager --full status astock-quant | sed -n '1,12p'

echo "部署完成。登录用户：${ASTOCK_ADMIN_USER:-admin}"
echo "部署 profile：${DEPLOY_PROFILE}；调度文件：/etc/cron.d/astock-quant（native 5 分钟）"
if [[ "${GENERATED_PASSWORD}" == "1" ]]; then
  echo "一次性生成的登录密码：${ASTOCK_ADMIN_PASSWORD}"
  echo "请立即保存；脚本不会把明文密码写入磁盘。"
fi
echo "云安全组应只允许你的固定出口 IP 访问 TCP 80。"
