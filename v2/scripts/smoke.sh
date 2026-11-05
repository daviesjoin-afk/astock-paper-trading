#!/usr/bin/env bash
set -euo pipefail
export ASTOCK_V2_DB="${RUNNER_TEMP:-/tmp}/astock-v2-http-smoke-${GITHUB_RUN_ID:-local}.sqlite3"
rm -f "$ASTOCK_V2_DB" "$ASTOCK_V2_DB-wal" "$ASTOCK_V2_DB-shm"
cargo run --quiet >"${RUNNER_TEMP:-/tmp}/astock-v2-api.log" 2>&1 &
server_pid=$!
cleanup() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  rm -f "$ASTOCK_V2_DB" "$ASTOCK_V2_DB-wal" "$ASTOCK_V2_DB-shm"
}
trap cleanup EXIT
ready=0
for _ in $(seq 1 90); do
  if curl --fail --silent http://127.0.0.1:8765/v2/health > /dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat "${RUNNER_TEMP:-/tmp}/astock-v2-api.log"
    exit 1
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  cat "${RUNNER_TEMP:-/tmp}/astock-v2-api.log"
  exit 1
fi

python3 research/worker.py --output "${RUNNER_TEMP:-/tmp}/v2-proposal.json" --send
python3 research/worker.py --output "${RUNNER_TEMP:-/tmp}/v2-proposal.json" --send
curl --fail --silent http://127.0.0.1:8765/v2/portfolio | python3 -c '
import json,sys
v=json.load(sys.stdin)
assert v["runs_total"] == 1, v
assert len(v["positions"]) == 1, v
assert v["cash_cents"] < v["initial_cash_cents"], v
'
curl --fail --silent http://127.0.0.1:8765/v2/replay | python3 -c '
import json,sys
r=json.load(sys.stdin)
assert r["ok"] is True, r
assert (r["runs_verified"],r["fills_verified"]) == (1,1), r
'
echo "V2 HTTP/Python/Rust/SQLite end-to-end smoke passed"
