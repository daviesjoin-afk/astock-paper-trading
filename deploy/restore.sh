#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${1:-}" == --confirm-restore ]] || { echo '用法: restore.sh --confirm-restore <backup-dir>' >&2; exit 2; }
BACKUP_DIR=$(realpath -e "${2:?缺少备份目录}")
if [[ -z "${APP_DIR:-}" ]]; then
  if [[ -d /opt/astock-codex/data_cache ]]; then APP_DIR=/opt/astock-codex; else APP_DIR=/root/codex; fi
fi
APP_DIR=$(realpath -e "$APP_DIR")
[[ "$APP_DIR" != / && -d "$APP_DIR/data_cache" ]] || exit 2
cd "$APP_DIR"
mkdir -p .locks
exec 9>.locks/restore.lock
flock -n 9 || exit 2

# Validate and stage before stopping services or touching live databases.
[[ -s "$BACKUP_DIR/SHA256SUMS" ]] || exit 2
(cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS)
PRE=$(mktemp -d "$APP_DIR/backups-restore-XXXXXXXX")
mkdir "$PRE/staged" "$PRE/original"
FILES=()
for name in paper_trading.sqlite3 adaptive_learning.sqlite3 paper_research.sqlite3 selection_tracking.db; do
  if [[ -f "$BACKUP_DIR/$name.gz" ]]; then
    gzip -dc "$BACKUP_DIR/$name.gz" > "$PRE/staged/$name"
  elif [[ -f "$BACKUP_DIR/$name" ]]; then
    cp "$BACKUP_DIR/$name" "$PRE/staged/$name"
  else
    continue
  fi
  [[ "$(sqlite3 "$PRE/staged/$name" 'PRAGMA integrity_check;')" == ok ]] || exit 2
  FILES+=("$name")
done
[[ ${#FILES[@]} -gt 0 ]] || { echo '没有可恢复数据库' >&2; exit 2; }
COMPOSE=(docker compose -f docker-compose.server.yml)
running=$("${COMPOSE[@]}" ps --status running --services)
[[ -n "$running" ]] || { echo '没有运行中的服务，请使用离线恢复流程' >&2; exit 2; }
mapfile -t RUNNING <<< "$running"
STOPPED=0
MODIFIED=0
recover() {
  rc=$?
  trap - EXIT
  if (( rc != 0 && MODIFIED )); then
    "${COMPOSE[@]}" stop "${RUNNING[@]}" || { echo "停服务失败，保留 $PRE 供人工恢复" >&2; exit "$rc"; }
    for name in "${FILES[@]}"; do
      for suffix in '' -wal -shm; do
        if [[ -e "$PRE/original/$name$suffix" ]]; then
          cp -p "$PRE/original/$name$suffix" "data_cache/$name$suffix" || exit "$rc"
        elif [[ -e "data_cache/$name$suffix" ]]; then
          mv "data_cache/$name$suffix" "$PRE/staged/failed-$name$suffix" || exit "$rc"
        fi
      done
    done
  fi
  if (( STOPPED )); then "${COMPOSE[@]}" start "${RUNNING[@]}" || exit 1; fi
  exit "$rc"
}
trap recover EXIT
STOPPED=1
# Include every running worker; stopped containers reject cron docker-exec.
"${COMPOSE[@]}" stop "${RUNNING[@]}"
for name in "${FILES[@]}"; do
  for suffix in '' -wal -shm; do
    [[ ! -e "data_cache/$name$suffix" ]] || cp -p "data_cache/$name$suffix" "$PRE/original/$name$suffix"
  done
done
MODIFIED=1
for name in "${FILES[@]}"; do
  for suffix in -wal -shm; do
    [[ ! -e "data_cache/$name$suffix" ]] || mv "data_cache/$name$suffix" "$PRE/original/retired-$name$suffix"
  done
  cp "$PRE/staged/$name" "data_cache/$name"
  if [[ -f "$PRE/original/$name" ]]; then
    chown --reference="$PRE/original/$name" "data_cache/$name"
    chmod --reference="$PRE/original/$name" "data_cache/$name"
  else
    chown --reference=data_cache "data_cache/$name"
    chmod 660 "data_cache/$name"
  fi
done
"${COMPOSE[@]}" start "${RUNNING[@]}"
for attempt in {1..30}; do
  if curl -fsS --max-time 5 http://127.0.0.1:18600/api/health >/dev/null; then
    trap - EXIT
    echo "恢复完成，回滚点: $PRE"
    exit 0
  fi
  sleep 2
done
echo '健康检查失败，回滚数据库' >&2
exit 1
