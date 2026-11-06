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

def bind_execution(incoming, side="buy"):
    level = incoming.get("execution")
    if level is not None:
        level["quote_ref"] = {key: incoming["quote"][key] for key in ("source", "quote_id")}
        level["order_side"] = side
        level["observed_at"] = incoming["quote"]["observed_at"]

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
    bind_execution(incoming, side)
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

# Empty portfolio: each negative case must reject for execution evidence, not
# merely because an earlier purchase already opened the position.
execution_cases = [
    ("missing", "execution_evidence_missing"),
    ("direction", "execution_side_mismatch"),
    ("zero", "insufficient_execution_liquidity"),
    ("short", "insufficient_execution_liquidity"),
    ("stale", "stale_execution_evidence"),
    ("future", "quote_not_yet_available"),
]
for label, reason in execution_cases:
    req = copy.deepcopy(proposal)
    req["signal_id"] = "execution-" + label
    req["decision_at"] = "2026-10-30T09:59:59+08:00"
    incoming = copy.deepcopy(quote)
    incoming["quote"]["quote_id"] = req["quote_ref"]["quote_id"] = "execution-quote-" + label
    bind_execution(incoming)
    if label == "missing":
        incoming.pop("execution")
    elif label == "direction":
        incoming["execution"]["order_side"] = "sell"
    elif label in ("zero", "short"):
        incoming["execution"]["available_quantity"] = 0 if label == "zero" else 699
    elif label == "stale":
        incoming["execution"]["observed_at"] = "2026-10-30T09:56:58.999+08:00"
    else:
        incoming["available_at"] = incoming["execution"]["observed_at"] = "2026-10-30T10:00:00+08:00"
    stored = call("quotes", incoming)
    first = call("simulations", req)
    assert first["outcome"]["reason"] == reason and first["outcome"]["fill"] is None, first
    assert first["outcome"]["cash_after_cents"] == 10_000_000
    retry = call("simulations", req)
    assert retry["duplicate"] and retry["outcome"] == first["outcome"]
    assert retry["evidence"] == stored
    if label == "missing":
        assert stored["execution"] is None
        enriched = copy.deepcopy(quote)
        enriched["quote"]["quote_id"] = req["quote_ref"]["quote_id"]
        bind_execution(enriched)
        call("quotes", enriched, 409)  # Cannot retrofit evidence into old identity.
prefix_runs = 4 + len(execution_cases)

output = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "v2-proposal.json"
for _ in range(2):
    subprocess.run([sys.executable, "research/worker.py", "--output", str(output), "--send"], check=True)
registered = call("quotes", quote)  # Duplicate ingress.
assert registered["trust_level"] == "offline_fixture", registered
assert registered["provider_published_at"] is None, registered
assert registered["schema_version"] == 3, registered
assert registered["quote"] == quote["quote"]
assert registered["available_at"] == quote["available_at"]
assert registered["execution"] == quote["execution"]
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
for field in ("schema_version", "quote_ref", "order_side", "price_cents", "available_quantity", "observed_at"):
    changed = copy.deepcopy(quote)
    changed["execution"].pop(field)
    call("quotes", changed, 422)
for field, value in (("price_cents", 1251), ("available_quantity", 701), ("order_side", "sell"),
                     ("observed_at", "2026-10-30T09:59:29+08:00")):
    changed = copy.deepcopy(quote)
    changed["execution"][field] = value
    call("quotes", changed, 409)
for field, value in (("price_cents", 0), ("available_quantity", -1), ("received_at", "forged"),
                     ("trust_level", "authenticated_provider")):
    changed = copy.deepcopy(quote)
    changed["execution"][field] = value
    call("quotes", changed, 422)
for field in ("execution", "price_cents", "available_quantity"):
    injected = copy.deepcopy(proposal)
    injected[field] = quote["execution"]
    call("simulations", injected, 422)
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
bind_execution(later)
call("quotes", later)
retry = call("simulations", missing)
assert retry["duplicate"] and retry["evidence"] is None, retry
assert retry["outcome"] == first["outcome"] and retry["id"] == first["id"]

late = copy.deepcopy(proposal)
late["signal_id"] = "late-evidence"
late["quote_ref"]["quote_id"] = "late-quote"
later["quote"]["quote_id"] = "late-quote"
later["available_at"] = "2026-10-30T10:00:00.001+08:00"
bind_execution(later)
call("quotes", later)
first = call("simulations", late)
assert first["outcome"]["reason"] == "quote_not_yet_available", first
assert call("simulations", late)["duplicate"]
changed = copy.deepcopy(proposal)
changed["quote_ref"]["quote_id"] = "late-quote"
call("simulations", changed, 409)

view = call("portfolio")
assert view["runs_total"] == prefix_runs and len(view["positions"]) == 1, view
assert view["cash_cents"] < view["initial_cash_cents"], view
for _ in range(2):
    report = call("replay")
    assert report["ok"] and (report["runs_verified"], report["fills_verified"]) == (prefix_runs, 1), report
runs = call("simulations")
filled = next(run for run in runs if run["outcome"]["status"] == "filled")
assert filled["evidence"] == registered
assert filled["outcome"]["fill"]["price_cents"] == 1250 != quote["quote"]["price_cents"]
assert filled["outcome"]["fill"]["quantity"] == 700

for field in ("calendar_version", "session_policy_version", "calendar", "sessions"):
    injected = copy.deepcopy(proposal)
    injected[field] = "override"
    call("simulations", injected, 422)
for run in runs:
    assert run["calendar_version"] == calendar_version
    assert run["session_policy_version"] == session_version

historical_buy = call("portfolio?asof_day=2026-10-30")
assert historical_buy["positions"][0]["quantity"] == 700, historical_buy
assert historical_buy["positions"][0]["sellable_quantity"] == 0
assert historical_buy["runs_total"] == prefix_runs
before_buy = call("portfolio?asof_day=2026-10-29")
assert not before_buy["positions"] and not before_buy["lots"], before_buy
assert before_buy["cash_cents"] == before_buy["initial_cash_cents"]
assert before_buy["runs_total"] == 1  # The October 1 holiday rejection only.
call("portfolio?asof_day=2026-1-1", expected=422)

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
    assert call("portfolio?asof_day=2026-10-30") == historical_buy
    assert call("portfolio?asof_day=2026-10-29") == before_buy
    sold_then_bought = call("portfolio?asof_day=2026-11-02")
    assert sold_then_bought["runs_total"] == prefix_runs + len(cases) - 1, sold_then_bought
    assert sold_then_bought["positions"][0]["sellable_quantity"] == 0
for _ in range(2):
    report = call("replay")
    assert report["ok"] and (report["runs_verified"], report["fills_verified"]) == (prefix_runs + len(cases), 3), report
assert call("portfolio")["runs_total"] == prefix_runs + len(cases)
print("HTTP execution evidence/price/capacity/missing/expiry/conflicts/retry/replay passed")
print("HTTP calendar/session/auction/unknown-date/retry/replay passed")
print("HTTP historical cash/holdings/counts/no-future-leak/repeated-reads passed")
print("HTTP evidence ingress/conflicts/bypasses/rejections/retries/replay passed")
PY
echo "V2 HTTP/Python/Rust/SQLite end-to-end smoke passed"
