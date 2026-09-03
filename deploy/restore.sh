#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--confirm-restore" ]]; then
  echo "用法: $0 --confirm-restore <backup-dir>" >&2
  exit 2
fi
BACKUP_DIR="${2:-}"
APP_DIR="${APP_DIR:-/root/codex}"
[[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]] || { echo "备份目录不存在" >&2; exit 2; }
[[ -f "$BACKUP_DIR/SHA256SUMS" ]] || { echo "缺少 SHA256SUMS" >&2; exit 2; }

cd "$APP_DIR"
docker compose -f docker-compose.server.yml ps >/dev/null
PRE="/root/codex/backups/pre-restore-$(date +%Y%m%d-%H%M%S)"
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
rsync -a --delete "$BACKUP_DIR/data_cache/" data_cache/
[[ -d "$BACKUP_DIR/reports" ]] && rsync -a --delete "$BACKUP_DIR/reports/" reports/ || true
docker compose -f docker-compose.server.yml up -d --build --no-deps astock astock-task-worker astock-data-worker
curl -fsS --max-time 20 http://127.0.0.1:18600/api/health >/dev/null
trap - EXIT
echo "恢复完成，回滚点: $PRE"
