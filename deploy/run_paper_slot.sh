#!/usr/bin/env bash
# Serialize every paper runner invocation across the host.  Fast-entry and
# intraday scans each import the full strategy stack; running them together in
# one Docker cgroup caused real OOM kills and a stalled audit trail.
set -euo pipefail

slot="${1:?slot is required}"
lock_dir="/opt/astock-codex/.locks"
mkdir -p "$lock_dir"

# Fast passes must not queue ahead of the three-minute risk/market scan.
# The next 30-second pass can retry; full scans retain their bounded wait.
lock_options=(-w 20)
if [[ "$slot" == "fast-entry" ]]; then
  lock_options=(-n)
fi
# Keep the same lock for the complete Docker invocation. SECONDS is Bash's
# elapsed timer (one-second resolution); do not confuse this inner lock with
# the outer cron slot lock or Docker/Python startup time.
exec 9>"$lock_dir/paper-scan.lock"
SECONDS=0
if flock "${lock_options[@]}" 9; then
  host_wait="$SECONDS"
else
  result=$?
  printf '{"event":"paper_host_lock","status":"not_acquired","wait_seconds":%s,"exit_code":%s}\n' \
    "$SECONDS" "$result" >&2
  exit "$result"
fi
exec docker exec --env "ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS=$host_wait" \
  astock-paper-worker python backend/paper_runner.py --slot "$slot"
