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
exec flock "${lock_options[@]}" "$lock_dir/paper-scan.lock" \
  docker exec astock-paper-worker python backend/paper_runner.py --slot "$slot"
