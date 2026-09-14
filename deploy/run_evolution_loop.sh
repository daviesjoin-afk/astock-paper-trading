#!/usr/bin/env bash
# Serialize evolution loop runner across the host with bounded wait.
# Prevents concurrency conflicts while allowing transient locks to clear.
# Daily idempotency and trade-day guards are enforced by evolution_loop_runner.py --daily.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

LOCK_DIR="${ROOT_DIR}/.locks"
if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
  LOCK_DIR="/tmp/.astock_locks"
  mkdir -p "$LOCK_DIR"
fi

LOCK_FILE="${LOCK_DIR}/evolution-loop.lock"

# flock -w 180 allows up to 3 minutes for any concurrent/lingering task to release lock.
exec flock -w 180 "$LOCK_FILE" \
  docker exec astock-task-worker python backend/evolution_loop_runner.py --daily "$@"
