#!/usr/bin/env bash
# Serialize every paper runner invocation across the host.  Fast-entry and
# intraday scans each import the full strategy stack; running them together in
# one Docker cgroup caused real OOM kills and a stalled audit trail.
set -euo pipefail

slot="${1:?slot is required}"
lock_dir="/root/codex/.locks"
mkdir -p "$lock_dir"

# A short bounded wait allows the 30-second fast pass to finish before a
# three-minute scan starts, without allowing either job to pile up forever.
exec flock -w 20 "$lock_dir/paper-scan.lock" \
  docker exec astock-paper-worker python backend/paper_runner.py --slot "$slot"
