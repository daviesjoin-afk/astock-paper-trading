#!/usr/bin/env bash
set -euo pipefail
export ASTOCK_V2_DB="${RUNNER_TEMP:-/tmp}/astock-v2-http-smoke-${GITHUB_RUN_ID:-local}.sqlite3"
rm -f "$ASTOCK_V2_DB" "$ASTOCK_V2_DB-wal" "$ASTOCK_V2_DB-shm"
cargo run --locked --quiet >"${RUNNER_TEMP:-/tmp}/astock-v2-api.log" 2>&1 &
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
python3 - <<'PY'
import copy
import datetime as dt
import json
import os
import subprocess
import tempfile
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

calendar_version = "sse-szse-scheduled-2026-v1"
session_version = "cn-a-share-continuous-only-v1"

def market_case(day, time, label, reason, side="buy"):
    req = copy.deepcopy(proposal)
    req["signal_id"] = "calendar-" + label
    req["signal"]["side"] = side
    req["decision_at"] = day + "T" + time + "+08:00"
    instant = dt.datetime.fromisoformat(req["decision_at"])
    incoming = copy.deepcopy(quote)
    incoming["quote"]["quote_id"] = "calendar-quote-" + label
    incoming["quote"]["observed_at"] = (instant - dt.timedelta(seconds=30)).isoformat()
    incoming["available_at"] = (instant - dt.timedelta(seconds=29)).isoformat()
    req["quote_ref"]["quote_id"] = incoming["quote"]["quote_id"]
    saved = call("quotes", incoming)
    first = call("simulations", req)
    assert first["outcome"]["reason"] == reason, first
    assert first["outcome"]["status"] == ("filled" if reason == "filled" else "rejected"), first
    assert first["calendar_version"] == calendar_version, first
    assert first["session_policy_version"] == session_version, first
    assert first["evidence"] == saved
    retry = call("simulations", req)
    assert retry["duplicate"] and retry["id"] == first["id"], retry
    assert retry["outcome"] == first["outcome"] and retry["evidence"] == first["evidence"]
    return first

# Keep append-only decision times monotonic: this holiday precedes the worker fixture.
market_case("2026-10-01", "10:00:00", "holiday", "holiday_market_closed")
output = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "v2-proposal.json"
for _ in range(2):
    subprocess.run([sys.executable, "research/worker.py", "--output", str(output), "--send"], check=True)
registered = call("quotes", quote)  # Duplicate ingress.
assert registered["trust_level"] == "offline_fixture", registered
assert registered["provider_published_at"] is None, registered
assert registered["schema_version"] == 2, registered
assert registered["quote"] == quote["quote"]
assert registered["available_at"] == quote["available_at"]
received = dt.datetime.fromisoformat(registered["received_at"])
assert received.utcoffset() == dt.timedelta(hours=8)
assert dt.timedelta(0) <= dt.datetime.now(dt.timezone.utc) - received < dt.timedelta(minutes=2)
assert call("quotes", quote) == registered  # First receipt never refreshes.
for field, value in (
    ("received_at", "2000-01-01T00:00:00+08:00"),
    ("trust_level", "authenticated_provider"),
    ("provider_published_at", "2026-10-30T09:59:30+08:00"),
    ("provider_request_id", "invented-proof"),
    ("provider_response_id", "invented-proof"),
):
    injected = copy.deepcopy(quote)
    injected[field] = value
    call("quotes", injected, 422)
call("quotes", registered, 422)  # A durable record is not an import command.
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
assert view["runs_total"] == 4 and len(view["positions"]) == 1, view
assert view["cash_cents"] < view["initial_cash_cents"], view
for _ in range(2):
    report = call("replay")
    assert report["ok"] and (report["runs_verified"], report["fills_verified"]) == (4, 1), report
runs = call("simulations")
filled = next(run for run in runs if run["outcome"]["status"] == "filled")
assert filled["evidence"] == registered

for field in ("calendar_version", "session_policy_version", "calendar", "sessions"):
    injected = copy.deepcopy(proposal)
    injected[field] = "override"
    call("simulations", injected, 422)
for run in runs:
    assert run["calendar_version"] == calendar_version
    assert run["session_policy_version"] == session_version

cases = [
    ("2026-10-31", "10:00:00", "saturday", "weekend_market_closed", "buy"),
    ("2026-11-01", "10:00:00", "sunday", "weekend_market_closed", "buy"),
    ("2026-11-02", "09:14:59.999", "before-auction", "outside_trading_session", "buy"),
    ("2026-11-02", "09:15:00", "open-auction-start", "opening_auction_not_supported", "buy"),
    ("2026-11-02", "09:25:00", "open-auction-end", "opening_auction_not_supported", "buy"),
    ("2026-11-02", "09:25:00.001", "preopen-gap", "outside_trading_session", "buy"),
    ("2026-11-02", "09:29:59.999", "before-open", "outside_trading_session", "buy"),
    ("2026-11-02", "09:30:00", "open-t1-sale", "filled", "sell"),
    ("2026-11-02", "11:30:00", "morning-end-buy", "filled", "buy"),
    ("2026-11-02", "11:30:00.001", "lunch-millisecond", "midday_break", "buy"),
    ("2026-11-02", "11:30:59", "lunch-second", "midday_break", "buy"),
    ("2026-11-02", "12:59:59.999", "before-afternoon", "midday_break", "buy"),
    ("2026-11-02", "13:00:00", "afternoon-open", "position_already_open", "buy"),
    ("2026-11-02", "14:56:59.999", "continuous-end", "position_already_open", "buy"),
    ("2026-11-02", "14:57:00", "closing-start", "closing_auction_not_supported", "buy"),
    ("2026-11-02", "15:00:00", "closing-end", "closing_auction_not_supported", "buy"),
    ("2026-11-02", "15:00:00.001", "after-close-ms", "outside_trading_session", "buy"),
    ("2026-11-02", "15:00:59", "after-close-sec", "outside_trading_session", "buy"),
    ("2026-11-02", "15:05:00", "postmarket", "outside_trading_session", "buy"),
    ("2027-01-04", "10:00:00", "unknown-year", "unknown_trading_date", "buy"),
]
for args in cases:
    market_case(*args)
for _ in range(2):
    report = call("replay")
    assert report["ok"] and (report["runs_verified"], report["fills_verified"]) == (4 + len(cases), 3), report
assert call("portfolio")["runs_total"] == 4 + len(cases)
print("HTTP calendar/session/auction/unknown-date/retry/replay passed")
print("HTTP evidence ingress/conflicts/bypasses/rejections/retries/replay passed")
PY
echo "V2 HTTP/Python/Rust/SQLite end-to-end smoke passed"
