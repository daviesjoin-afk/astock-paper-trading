#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--confirm-restore" ]]; then
  echo "用法: $0 --confirm-restore <backup-dir>" >&2
  exit 2
fi
BACKUP_DIR="${2:-}"
if [[ -n "${APP_DIR:-}" ]]; then
  APP_DIR="$APP_DIR"
elif [[ -d /opt/astock-codex/data_cache ]]; then
  APP_DIR=/opt/astock-codex
elif [[ -d /root/codex/data_cache ]]; then
  APP_DIR=/root/codex
else
  echo "restore failed: 未找到项目目录（尝试过 APP_DIR、/opt/astock-codex、/root/codex）" >&2
  exit 1
fi
[[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]] || { echo "备份目录不存在" >&2; exit 2; }
[[ -f "$BACKUP_DIR/SHA256SUMS" ]] || { echo "缺少 SHA256SUMS" >&2; exit 2; }

cd "$APP_DIR"
docker compose -f docker-compose.server.yml ps >/dev/null
PRE="$APP_DIR/backups/pre-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PRE"
cp -a data_cache "$PRE/data_cache"
cp -a reports "$PRE/reports" 2>/dev/null || true
cp -a docker-compose.server.yml Dockerfile .dockerignore "$PRE/"

rollback() {
  rc=$?
  [[ "$rc" -eq 0 ]] && return
  echo "恢复或健康检查失败，自动回滚到 $PRE" >&2
  rm -rf data_cache
  cp -a "$PRE/data_cache" data_cache
  rm -rf reports
  [[ -d "$PRE/reports" ]] && cp -a "$PRE/reports" reports
  docker compose -f docker-compose.server.yml up -d --no-build --no-deps astock astock-task-worker astock-data-worker >/dev/null || true
  curl -fsS --max-time 10 http://127.0.0.1:18600/api/health >/dev/null || true
  exit "$rc"
}
trap rollback EXIT

(cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS)
# backup.sh 将 sqlite 与 data_cache 附属清单直接放在备份目录根部（而非
# data_cache/ 子目录），这里按相同布局恢复，逐文件落入 data_cache。
mkdir -p data_cache
for f in "$BACKUP_DIR"/*.sqlite3 "$BACKUP_DIR"/*.db "$BACKUP_DIR"/kline_manifest.json "$BACKUP_DIR"/universe.json; do
  [[ -f "$f" ]] && cp -f "$f" data_cache/
done
[[ -d "$BACKUP_DIR/reports" ]] && rsync -a --delete "$BACKUP_DIR/reports/" reports/ || true
docker compose -f docker-compose.server.yml up -d --build --no-deps astock astock-task-worker astock-data-worker
curl -fsS --max-time 20 http://127.0.0.1:18600/api/health >/dev/null
trap - EXIT
echo "恢复完成，回滚点: $PRE"
