#!/usr/bin/env bash
# Keep the data-worker's shared resource admission; never take a trading lock.
set -euo pipefail
lock_dir="${ASTOCK_LOCK_DIR:-/opt/astock-codex/.locks}"
mkdir -p "$lock_dir"
exec 9>"$lock_dir/resource-heavy.lock"
if ! flock -n 9; then
  printf '{"event":"dashboard_refresh","lane":"dashboard-refresh","status":"skipped","reason":"resource_busy","at":"%s"}
' "$(date -Iseconds)"
  exit 0
fi
docker exec astock-data-worker python backend/paper_snapshot_worker.py
