"""Fixed offline two-experiment demo against the running local Rust service.
Synthetic prices/capacity; this is not authenticated market data.
Run from v2/: python scripts/experiment_demo.py [--verify-only]
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
from worker import build_fixture_proposal

def call(path, payload=None, expected=200):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("http://127.0.0.1:8765/v2/" + path, data,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            assert response.status == expected
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read()
        assert error.code == expected, (path, error.code, body)
        return body

def view(cycle, day=None):
    return call("portfolio?cycle_id=" + cycle + ("" if day is None else "&asof_day=" + day))

def proposal(cycle):
    return build_fixture_proposal(cycle)

def create(cycle, cash):
    return call("experiments", {
        "schema_version": 1, "cycle_id": cycle, "initial_cash_cents": cash,
        "strategy": {"strategy_version_id": "research-fixture-v1",
                     "research_input_id": "offline-fixture-input-v1"},
        "market_data_pack_id": "cn-offline-fixture-pack-1",
        "execution_policy_version": "v2-paper-policy-6",
    })

def sell(cycle, day, label):
    req = proposal(cycle)
    req["signal_id"] = label
    req["signal"]["side"] = "sell"
    req["decision_at"] = day + "T10:00:00+08:00"
    req["quote_ref"]["quote_id"] = label + "-quote"
    incoming = json.loads((ROOT / "fixtures/quote.json").read_text())
    incoming["quote"]["quote_id"] = req["quote_ref"]["quote_id"]
    incoming["quote"]["observed_at"] = day + "T09:59:30+08:00"
    incoming["available_at"] = day + "T09:59:31+08:00"
    incoming["execution"]["quote_ref"] = req["quote_ref"]
    incoming["execution"]["order_side"] = "sell"
    incoming["execution"]["observed_at"] = incoming["quote"]["observed_at"]
    call("quotes", incoming)
    return call("simulations", req)

def verify():
    reports = {}
    for cycle, seed, count, quantity in [("experiment-A", 10_000_000, 2, 700),
                                          ("experiment-B", 5_000_000, 3, 300)]:
        current = view(cycle)
        old = view(cycle, "2026-10-30")
        before = view(cycle, "2026-10-29")
        assert current["cycle_id"] == cycle and current["initial_cash_cents"] == seed
        assert current["runs_total"] == count and not current["positions"]
        assert before["cash_cents"] == seed and before["runs_total"] == 0
        assert old["positions"][0]["quantity"] == quantity
        assert old["positions"][0]["sellable_quantity"] == 0
        assert call("experiments/" + cycle)["status"] == "completed"
        assert call("simulations", proposal(cycle))["duplicate"]
        replay = call("replay?cycle_id=" + cycle)
        assert replay["ok"] and replay["cycle_id"] == cycle
        assert replay["runs_verified"] == count and replay["fills_verified"] == 2
        assert replay["cash_cents"] == current["cash_cents"]
        rows = call("simulations?cycle_id=" + cycle)
        assert len(rows) == count and all(r["request"]["cycle_id"] == cycle for r in rows)
        reports[cycle] = {"portfolio": current, "historical": old, "replay": replay}
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    print("Independent experiments: cash/holdings/T+1/counts/history/idempotency/replay passed")

def main():
    if "--verify-only" in sys.argv:
        verify()
        return
    for path in ["portfolio", "simulations", "replay"]:
        call(path, expected=400)  # No global fallback.
    create("experiment-A", 10_000_000)
    create("experiment-B", 5_000_000)
    incoming = json.loads((ROOT / "fixtures/quote.json").read_text())
    saved = call("quotes", incoming)
    assert saved["trust_level"] == "offline_fixture"
    first = call("simulations", proposal("experiment-A"))
    assert first["outcome"]["status"] == "filled"
    historical_a = view("experiment-A", "2026-10-30")
    assert view("experiment-B")["runs_total"] == 0
    assert view("experiment-B")["cash_cents"] == 5_000_000
    assert sell("experiment-A", "2026-11-02", "demo-next-day")["outcome"]["status"] == "filled"
    final_a = view("experiment-A")
    second = call("simulations", proposal("experiment-B"))
    assert second["outcome"]["status"] == "filled"
    assert first["evidence"] == second["evidence"] == saved
    assert sell("experiment-B", "2026-10-30", "demo-t1")["outcome"]["reason"] == "no_t1_sellable_lot"
    assert sell("experiment-B", "2026-11-02", "demo-next-day")["outcome"]["status"] == "filled"
    assert view("experiment-A") == final_a
    assert view("experiment-A", "2026-10-30") == historical_a
    changed = copy.deepcopy(incoming)
    changed["execution"]["price_cents"] += 1
    call("quotes", changed, 409)
    for cycle in ["experiment-A", "experiment-B"]:
        call("experiments/" + cycle + "/complete", {})
        changed = proposal(cycle)
        changed["signal_id"] = "after-completion"
        call("simulations", changed, 409)
    verify()

if __name__ == "__main__":
    main()

