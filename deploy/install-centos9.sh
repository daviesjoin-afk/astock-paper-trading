#!/usr/bin/env bash
# native-centos9 原生部署 profile 已退役（2026-10-20）。本脚本不再安装任何东西。
#
# 为什么退役（证据可复核）：
#   1. 生产服务器运行 4 个容器（docs/AUTONOMOUS_PAPER_SYSTEM_AUDIT_2026-10-15.md
#      的取证结论；与 docker-compose.server.yml 的 astock-codex / astock-data-worker /
#      astock-task-worker / astock-paper-worker 一致），即 Docker profile；
#   2. 本 profile 依赖的 deploy/astock-quant.{service,nginx.conf,cron} 从未进入 git
#      （`git log --all -- deploy/astock-quant.*` 为空），无法按仓库版本复现；
#      deploy/backup.sh 的路径探测也从不包含 /opt/astock-quant；
#   3. 它使用 Python 3.11（canonical 3.14 的唯一 legacy 例外）与 5 分钟时间表，
#      与 backend/paper_schedule_contract.py 的 3 分钟契约冲突。
#
# 历史与恢复路径：docs/archive/NATIVE_CENTOS9_PROFILE_RETIREMENT_2026-10-20.md
set -euo pipefail

cat >&2 <<'EOF'
native-centos9 原生部署 profile 已退役（2026-10-20），本脚本不再安装任何东西。

请使用 Docker profile：
  docker compose -f docker-compose.server.yml up -d --build
  sudo bash deploy/sync-cron.sh

若你确实有一台仍在运行 native profile 的主机，请先做只读盘点：
  systemctl cat astock-quant
  cat /etc/cron.d/astock-quant
  ls -l /opt/astock-quant/deploy/
然后按 docs/archive/NATIVE_CENTOS9_PROFILE_RETIREMENT_2026-10-20.md 的"恢复路径"处理。
EOF
exit 3
