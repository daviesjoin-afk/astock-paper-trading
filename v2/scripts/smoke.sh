#!/usr/bin/env bash
set -euo pipefail
export ASTOCK_V2_DB="${RUNNER_TEMP:-/tmp}/astock-v2-http-smoke-${GITHUB_RUN_ID:-local}.sqlite3"
# End-to-end price-set pipeline: CSV -> reviewed-artifact tool -> directory loader.
PRICE_DIR="${RUNNER_TEMP:-/tmp}/astock-price-sets-${GITHUB_RUN_ID:-local}"
rm -rf "$PRICE_DIR"
mkdir -p "$PRICE_DIR"
cat > "$PRICE_DIR/prices.csv" <<'CSV'
symbol,day,price,source,observed_at
600000.SH,2026-10-30,12.34,offline-fixture,2026-10-30T15:00:00+08:00
600000.SH,2026-11-02,12.19,offline-fixture,2026-11-02T15:00:00+08:00
CSV
python research/make_price_set.py   --input "$PRICE_DIR/prices.csv" --out "$PRICE_DIR/smoke-synthetic.json"   --set-id smoke-synthetic --pack cn-offline-fixture-pack-1 --trust synthetic   --coverage-start 2026-10-30 --coverage-end 2026-11-02   --symbols 600000.SH --days 2026-10-30,2026-11-02 > /dev/null
export ASTOCK_PRICE_SETS_DIR="$PRICE_DIR"
rm -f "$ASTOCK_V2_DB" "$ASTOCK_V2_DB-wal" "$ASTOCK_V2_DB-shm"
cargo run --locked --quiet >"${RUNNER_TEMP:-/tmp}/astock-v2-api.log" 2>&1 &
server_pid=$!
cleanup() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  rm -f "$ASTOCK_V2_DB" "$ASTOCK_V2_DB-wal" "$ASTOCK_V2_DB-shm"
  rm -rf "$PRICE_DIR"
}
trap cleanup EXIT
wait_ready() {
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
}
wait_ready

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
    if payload is None and path.split("?")[0] in ("portfolio", "simulations", "replay"):
        path += ("&" if "?" in path else "?") + "cycle_id=offline-fixture-cycle"
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
listed = call("experiments")
assert [row["cycle_id"] for row in listed["experiments"]] == [], listed
reviewed_strategy = json.loads(Path("data/strategy-momentum-v1.json").read_text(encoding="utf-8"))
call("experiments", {
    "schema_version": 1, "cycle_id": proposal["cycle_id"], "initial_cash_cents": 10_000_000,
    "strategy": reviewed_strategy,
    "market_data_pack_id": "cn-offline-fixture-pack-1", "execution_policy_version": "v2-paper-policy-6",
})
listed = call("experiments")
assert [row["cycle_id"] for row in listed["experiments"]] == [proposal["cycle_id"]], listed
assert listed["experiments"][0]["status"] == "active"
assert listed["experiments"][0]["initial_cash_cents"] == 10_000_000
assert listed["experiments"][0]["strategy_version_id"] == "research-momentum-v1"
quote = json.loads(Path("fixtures/quote.json").read_text())

calendar_version = "sse-szse-scheduled-2026-v1"
session_version = "cn-a-share-continuous-only-v1"
instrument_version = "cn-instruments-offline-v1"

def bind_execution(incoming, side="buy"):
    level = incoming.get("execution")
    if level is not None:
        level["quote_ref"] = {"market_data_pack_id": incoming["market_data_pack_id"], **{key: incoming["quote"][key] for key in ("source", "quote_id")}}
        level["order_side"] = side
        level["observed_at"] = incoming["quote"]["observed_at"]

def market_case(day, time, label, reason, side="buy", symbol="600000.SH"):
    req = copy.deepcopy(proposal)
    req["signal_id"] = "calendar-" + label
    req["signal"]["side"] = side
    req["signal"]["symbol"] = symbol
    req["decision_at"] = day + "T" + time + "+08:00"
    instant = dt.datetime.fromisoformat(req["decision_at"])
    incoming = copy.deepcopy(quote)
    incoming["quote"]["symbol"] = symbol
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
    assert first["instrument_version"] == instrument_version, first
    assert first["evidence"] == saved
    retry = call("simulations", req)
    assert retry["duplicate"] and retry["id"] == first["id"], retry
    assert retry["outcome"] == first["outcome"] and retry["evidence"] == first["evidence"]
    return first

# Keep append-only decision times monotonic: this holiday precedes the worker fixture.
market_case("2026-10-01", "10:00:00", "holiday", "holiday_market_closed")
market_case("2026-10-26", "10:00:00", "identity-before-coverage", "instrument_date_not_covered")
for symbol, label, reason in [
    ("688001.SH", "star", "unsupported_instrument_board"),
    ("513150.SH", "etf", "unsupported_security_type"),
    ("999999.SH", "identity-missing", "instrument_identity_missing"),
]:
    # Independent, explicit fixture identities; enough price/capacity cannot
    # turn an unsupported instrument into an ordinary main-board stock.
    first = market_case("2026-10-30", "09:59:58", label, reason, symbol=symbol)
    assert first["outcome"]["fill"] is None
    assert first["outcome"]["cash_after_cents"] == 10_000_000
    assert (first["evidence"]["instrument"] is None) == (symbol == "999999.SH")

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
prefix_runs = 8 + len(execution_cases)

output = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "v2-proposal.json"
for _ in range(2):
    subprocess.run([sys.executable, "research/worker.py", "--output", str(output), "--send"], check=True)
registered = call("quotes", quote)  # Duplicate ingress.
assert registered["trust_level"] == "offline_fixture", registered
assert registered["provider_published_at"] is None, registered
assert registered["schema_version"] == 5, registered
assert registered["instrument"]["symbol"] == "600000.SH"
assert registered["instrument"]["exchange"] == "sse"
assert registered["instrument"]["board"] == "main_board"
assert registered["instrument"]["security_type"] == "ordinary_a_share"
assert registered["instrument"]["evidence_version"] == instrument_version
assert registered["instrument"]["quantity_rule_version"] == "sse-main-board-whole-lot-2026-v1"
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

for field in ("calendar_version", "session_policy_version", "calendar", "sessions", "instrument", "instrument_version", "board", "quantity_rule_version"):
    injected = copy.deepcopy(proposal)
    injected[field] = "override"
    call("simulations", injected, 422)
    injected = copy.deepcopy(quote)
    injected[field] = "forged"
    call("quotes", injected, 422)
for run in runs:
    assert run["calendar_version"] == calendar_version
    assert run["session_policy_version"] == session_version
    assert run["instrument_version"] == instrument_version

historical_buy = call("portfolio?asof_day=2026-10-30")
assert historical_buy["positions"][0]["quantity"] == 700, historical_buy
assert historical_buy["positions"][0]["sellable_quantity"] == 0
assert historical_buy["runs_total"] == prefix_runs
before_buy = call("portfolio?asof_day=2026-10-29")
assert not before_buy["positions"] and not before_buy["lots"], before_buy
assert before_buy["cash_cents"] == before_buy["initial_cash_cents"]
assert before_buy["runs_total"] == 2  # Holiday and pre-coverage rejections only.
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
    ("2026-11-06", "10:00:00", "identity-after-coverage", "instrument_date_not_covered", "buy"),
    ("2027-01-04", "10:00:00", "unknown-year", "unknown_trading_date", "buy"),
]
for args in cases:
    market_case(*args)
for _ in range(2):
    assert call("portfolio?asof_day=2026-10-30") == historical_buy
    assert call("portfolio?asof_day=2026-10-29") == before_buy
    sold_then_bought = call("portfolio?asof_day=2026-11-02")
    assert sold_then_bought["runs_total"] == prefix_runs + len(cases) - 2, sold_then_bought
    assert sold_then_bought["positions"][0]["sellable_quantity"] == 0
for _ in range(2):
    report = call("replay")
    assert report["ok"] and (report["runs_verified"], report["fills_verified"]) == (prefix_runs + len(cases), 3), report
assert call("portfolio")["runs_total"] == prefix_runs + len(cases)

# Paper account: one user-funded account, two frozen slots, exact split.
paper_spec = {
    "schema_version": 1, "account_id": "smoke-account", "initial_cash_cents": 10_000_000,
    "reserve_bps": 1000, "created_at": "2026-11-08T09:00:00+08:00",
    "slots": [
        {"slot": "A", "strategy_version_id": "research-momentum-v1",
         "market_data_pack_id": "cn-offline-fixture-pack-1",
         "execution_policy_version": "v2-paper-policy-6", "weight_bps": 4500},
        {"slot": "B", "strategy_version_id": "research-reversion-v1",
         "market_data_pack_id": "cn-offline-fixture-pack-1",
         "execution_policy_version": "v2-paper-policy-6", "weight_bps": 4500},
    ],
}
paper = call("paper-accounts", paper_spec)
assert paper["initial_cash_cents"] == 10_000_000
assert paper["reserve_cents"] == 1_000_000
assert [slot["budget_cents"] for slot in paper["slots"]] == [4_500_000, 4_500_000]
assert [slot["cash_cents"] for slot in paper["slots"]] == [4_500_000, 4_500_000]
assert paper["cash_cents"] == paper["initial_cash_cents"]
assert paper["reserve_cents"] + sum(slot["cash_cents"] for slot in paper["slots"]) == paper["initial_cash_cents"]
assert all(slot["positions"] == [] for slot in paper["slots"])
assert call("paper-accounts/smoke-account") == paper  # read must not drift
assert call("paper-accounts", paper_spec) == paper  # identical spec is idempotent
conflicting = copy.deepcopy(paper_spec)
conflicting["initial_cash_cents"] = 20_000_000
call("paper-accounts", conflicting, expected=409)
call("paper-accounts/does-not-exist", expected=404)
# The directory now carries one published artifact; synthetic sets are listed
# but never selected for a user-facing valuation.
catalogue = call("price-sets")
assert len(catalogue) == 1, catalogue
assert catalogue[0]["price_set_id"] == "smoke-synthetic", catalogue
assert catalogue[0]["trust"] == "synthetic", catalogue
# Evolution governance: evidence-based promotion, no lucky single backtest.
evolution = call("evolution/decisions", {
    "kind": "promotion",
    "incumbent": {"candidate_id": "base", "out_of_sample_days": 30, "shadow_days": 15,
                  "trades": 40, "return_bps": 500, "drawdown_bps": 800},
    "candidate": {"candidate_id": "cand", "out_of_sample_days": 25, "shadow_days": 12,
                  "trades": 25, "return_bps": 700, "drawdown_bps": 900},
})
assert evolution["status"] == "promoted", evolution
# The account ledger proves its own invariants and links fills to outcomes.
audit = call("paper-accounts/smoke-account/audit")
assert audit["ok"] is True, audit
# A rebalance moves cash and updates weights without touching fills.
rebalance = call("paper-accounts/smoke-account/rebalances", {
    "targets": [3500, 5500],
    "proposal_digest": "a" * 64,
    "applied_at": "2026-11-03T09:00:00+08:00",
    "note": "smoke rebalance",
})
assert rebalance["weights_before"] == [4500, 4500], rebalance
assert rebalance["weights_after"] == [3500, 5500], rebalance
assert rebalance["from_slot"] == "A" and rebalance["to_slot"] == "B", rebalance
account = call("paper-accounts/smoke-account")
assert account["slots"][0]["weight_bps"] == 3500, account["slots"][0]
assert account["slots"][0]["budget_cents"] == 3_500_000, account["slots"][0]
assert account["slots"][0]["cash_cents"] == 3_500_000, account["slots"][0]
audit = call("paper-accounts/smoke-account/audit")
assert audit["ok"] is True, audit
assert [check["name"] for check in audit["checks"]] == [
    "budget_split", "fills_replay", "capital_conservation",
    "order_fill_link", "fill_matches_outcome", "session_link", "session_signals",
    "rebalance_chain",
]
# No reviewed price set is bundled: the equity series refuses instead of guessing.
# Allocation proposals need reviewed prices; synthetic data never drives them.
allocation = call("paper-accounts/smoke-account/allocation-proposal?days=2026-10-30,2026-11-02")
assert allocation["status"] == "unavailable" and allocation["reason"] == "no_reviewed_price_set", allocation
series = call("paper-accounts/smoke-account/equity-series?days=2026-10-30")
assert series["status"] == "unavailable" and series["reason"] == "no_reviewed_price_set", series
call("paper-accounts/smoke-account/equity-series?days=", expected=422)

# Slot-owned order path: evidence, budget and conservation.
paper_order = {
    "schema_version": 1, "account_id": "smoke-account", "slot": "A",
    "signal_id": "paper-smoke-buy",
    "decision_at": "2026-10-30T10:00:00+08:00",
    "signal": {"symbol": "600000.SH", "side": "buy"},
    "quote_ref": proposal["quote_ref"],
}
order = call("paper-accounts/smoke-account/orders", paper_order)
assert order["outcome"]["status"] == "filled", order
fill = order["outcome"]["fill"]
spent = fill["quantity"] * fill["price_cents"] + fill["fees_cents"]
paper = call("paper-accounts/smoke-account")
slot_a, slot_b = paper["slots"]
assert slot_a["cash_cents"] == slot_a["budget_cents"] - spent, (slot_a, spent)
assert slot_b["cash_cents"] == slot_b["budget_cents"]
assert paper["reserve_cents"] + slot_a["cash_cents"] + slot_b["cash_cents"] == 10_000_000 - spent
assert slot_a["positions"] == [{"symbol": "600000.SH", "quantity": fill["quantity"], "sellable_quantity": None}]
assert call("paper-accounts/smoke-account/orders", paper_order)["duplicate"] is True
orders = call("paper-accounts/smoke-account/orders")
assert len(orders) == 1 and orders[0]["id"] == order["id"], orders
assert orders[0]["slot"] == "A" and orders[0]["outcome"] == order["outcome"]

# Deterministic session clock: explicit start, two frozen signals, pause gates.
session_spec = {
    "schema_version": 1, "session_id": "smoke-session", "account_id": "smoke-account",
    "start_at": "2026-10-30T00:00:00+08:00",
    "signals": [
        {"slot": "A", "signal_id": "session-buy", "decision_at": "2026-10-30T10:00:00+08:00",
         "signal": {"symbol": "600000.SH", "side": "buy"}, "quote_ref": proposal["quote_ref"]},
        {"slot": "A", "signal_id": "session-sell", "decision_at": "2026-11-02T10:00:00+08:00",
         "signal": {"symbol": "600000.SH", "side": "sell"}, "quote_ref": proposal["quote_ref"]},
    ],
}
session = call("paper-sessions", session_spec)
assert session["status"] == "created" and session["cursor"] == 0 and session["total"] == 2, session
assert call("paper-sessions/smoke-session/start", {})["status"] == "running"
first_step = call("paper-sessions/smoke-session/step", {})
assert first_step["session"]["cursor"] == 1, first_step
assert first_step["session"]["clock_at"] == "2026-10-30T10:00:00+08:00", first_step
assert first_step["order"]["outcome"]["status"] in ("filled", "rejected"), first_step
assert call("paper-sessions/smoke-session/pause", {})["status"] == "paused"
call("paper-sessions/smoke-session/step", {}, expected=409)
assert call("paper-sessions/smoke-session/resume", {})["status"] == "running"
last_step = call("paper-sessions/smoke-session/step", {})
assert last_step["session"]["status"] == "completed" and last_step["session"]["cursor"] == 2, last_step
assert call("paper-sessions/smoke-session")["status"] == "completed"
import sqlite3
with sqlite3.connect("file:" + os.environ["ASTOCK_V2_DB"] + "?mode=ro", uri=True) as connection:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
    snapshot = json.loads(connection.execute("SELECT json_extract(pack_json,'$.instruments') FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-1'").fetchone()[0])
    assert snapshot["version"] == instrument_version
    assert snapshot["trust_level"] == "offline_fixture"
    assert connection.execute("SELECT count(*) FROM v2_fills").fetchone()[0] == 3
    for request_json, evidence_json, version in connection.execute("SELECT request_json,evidence_json,instrument_version FROM v2_simulation_runs"):
        assert version == instrument_version
        evidence = json.loads(evidence_json)
        if evidence is not None and evidence["instrument"] is not None:
            assert evidence["instrument"] in snapshot["instruments"]
        assert "instrument" not in json.loads(request_json)
print("HTTP/SQLite instrument identity/coverage/unsupported types/immutable snapshot/replay passed")
print("HTTP execution evidence/price/capacity/missing/expiry/conflicts/retry/replay passed")
print("HTTP calendar/session/auction/unknown-date/retry/replay passed")
print("HTTP historical cash/holdings/counts/no-future-leak/repeated-reads passed")
print("HTTP evidence ingress/conflicts/bypasses/rejections/retries/replay passed")
PY
python3 scripts/experiment_demo.py
python3 scripts/market_pack_demo.py
python3 scripts/research_reproduction_demo.py
# Reopen the exact SQLite file in a new Rust process, then verify both frozen ledgers.
kill "$server_pid"
wait "$server_pid" 2>/dev/null || true
cargo run --locked --quiet >"${RUNNER_TEMP:-/tmp}/astock-v2-api.log" 2>&1 &
server_pid=$!
wait_ready
python3 scripts/experiment_demo.py --verify-only
python3 scripts/market_pack_demo.py --verify-only
python3 scripts/research_reproduction_demo.py --verify-only
echo "V2 HTTP/Python/Rust/SQLite research-reproduction smoke passed"
