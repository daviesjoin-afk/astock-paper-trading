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

curl --fail --silent -H 'Content-Type: application/json' \
  --data-binary @fixtures/quote.json http://127.0.0.1:8765/v2/quotes > /dev/null
python3 research/worker.py --output "${RUNNER_TEMP:-/tmp}/v2-proposal.json" --send
python3 research/worker.py --output "${RUNNER_TEMP:-/tmp}/v2-proposal.json" --send
python3 - <<'PY'
import copy
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path("research").resolve()))
from worker import build_fixture_proposal

def call(path, payload=None, expected=200):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request("http://127.0.0.1:8765/v2/" + path, body,
                                     {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == expected
            return json.load(response)
    except urllib.error.HTTPError as error:
        assert error.code == expected, (error.code, error.read())
        return error.read()

proposal = build_fixture_proposal()
quote = json.loads(Path("fixtures/quote.json").read_text())
registered = call("quotes", quote)  # Duplicate ingress.
assert registered["trust_level"] == "offline_fixture", registered
assert registered["provider_published_at"] is None, registered
assert registered["received_at"], registered
for field, value in (("price_cents", 1), ("tradable", False)):
    changed = copy.deepcopy(quote)
    changed["quote"][field] = value
    call("quotes", changed, 409)
for field, value in (("source", "claimed-live-provider"), ("price_cents", 0)):
    changed = copy.deepcopy(quote)
    changed["quote"][field] = value
    call("quotes", changed, 422)
for field in ("quantity", "shares", "amount"):
    injected = copy.deepcopy(proposal)
    injected["signal"][field] = 100
    call("simulations", injected, 422)
injected = copy.deepcopy(proposal)
injected["quote"] = quote["quote"]
call("simulations", injected, 422)
injected = copy.deepcopy(proposal)
injected["quote_ref"]["price_cents"] = 1
call("simulations", injected, 422)

missing = copy.deepcopy(proposal)
missing["signal_id"] = "missing-evidence"
missing["quote_ref"]["quote_id"] = "missing-quote"
first = call("simulations", missing)
assert first["outcome"]["reason"] == "quote_evidence_missing", first
assert first["evidence"] is None and not first["duplicate"], first
later = copy.deepcopy(quote)
later["quote"]["quote_id"] = "missing-quote"
call("quotes", later)
retry = call("simulations", missing)
assert retry["duplicate"] and retry["evidence"] is None, retry
assert retry["outcome"] == first["outcome"] and retry["id"] == first["id"]

late = copy.deepcopy(proposal)
late["signal_id"] = "late-evidence"
late["quote_ref"]["quote_id"] = "late-quote"
later["quote"]["quote_id"] = "late-quote"
later["available_at"] = "2026-10-30T10:00:00.001+08:00"
call("quotes", later)
first = call("simulations", late)
assert first["outcome"]["reason"] == "quote_not_yet_available", first
assert call("simulations", late)["duplicate"]
changed = copy.deepcopy(proposal)
changed["quote_ref"]["quote_id"] = "late-quote"
call("simulations", changed, 409)

view = call("portfolio")
assert view["runs_total"] == 3 and len(view["positions"]) == 1, view
assert view["cash_cents"] < view["initial_cash_cents"], view
for _ in range(2):
    report = call("replay")
    assert report["ok"] and (report["runs_verified"], report["fills_verified"]) == (3, 1), report
runs = call("simulations")
filled = next(run for run in runs if run["outcome"]["status"] == "filled")
assert filled["evidence"] == quote
print("HTTP evidence ingress/conflicts/bypasses/rejections/retries/replay passed")
PY
echo "V2 HTTP/Python/Rust/SQLite end-to-end smoke passed"
