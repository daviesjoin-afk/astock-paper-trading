"""Two reviewed offline packs: same capital/strategy/external IDs, different prices.
All prices and capacities are synthetic. Run against the local Rust service.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
from experiment_demo import call, view
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from worker import build_fixture_proposal

ROOT = Path(__file__).resolve().parents[1]
A, B = "pack-experiment-A", "pack-experiment-B"
PACK_A, PACK_B = "cn-offline-fixture-pack-1", "cn-offline-fixture-pack-2"

def proposal(cycle):
    return build_fixture_proposal(cycle, PACK_A if cycle == A else PACK_B)

def create(cycle, pack):
    return call("experiments", {
        "schema_version": 1, "cycle_id": cycle, "initial_cash_cents": 10_000_000,
        "strategy": {"strategy_version_id": "research-fixture-v1", "research_input_id": "offline-fixture-input-v1"},
        "market_data_pack_id": pack, "execution_policy_version": "v2-paper-policy-6",
    })

def incoming(cycle, price=1250):
    data = json.loads((ROOT / "fixtures/quote.json").read_text(encoding="utf8"))
    data["market_data_pack_id"] = proposal(cycle)["quote_ref"]["market_data_pack_id"]
    data["execution"]["quote_ref"] = proposal(cycle)["quote_ref"]
    data["quote"]["price_cents"] = price
    data["execution"]["price_cents"] = price
    return data

def sell(day, label):
    req = proposal(B)
    req["signal"]["side"] = "sell"
    req["signal_id"] = label
    req["quote_ref"]["quote_id"] = label
    req["decision_at"] = day + "T10:00:00+08:00"
    data = incoming(B, 2000)
    data["quote"]["quote_id"] = label
    data["quote"]["observed_at"] = day + "T09:59:30+08:00"
    data["available_at"] = day + "T09:59:31+08:00"
    data["execution"]["quote_ref"] = req["quote_ref"]
    data["execution"]["order_side"] = "sell"
    data["execution"]["observed_at"] = data["quote"]["observed_at"]
    call("quotes", data)
    return call("simulations", req)

def verify():
    report = {}
    for cycle, pack, count, fills, quantity in [(A, PACK_A, 1, 1, 700), (B, PACK_B, 3, 2, 400)]:
        exp = call("experiments/" + cycle)
        assert exp["spec"]["market_data_pack_id"] == pack and exp["status"] == "completed"
        assert exp["spec"]["initial_cash_cents"] == 10_000_000
        manifest = call("market-data-packs/" + pack)["manifest"]
        assert manifest["pack_id"] == pack and manifest["trust_level"] == "offline_fixture"
        assert len(manifest["content_summary"]) == 3
        old = view(cycle, "2026-10-30")
        assert old["positions"][0]["quantity"] == quantity
        assert old["positions"][0]["sellable_quantity"] == 0
        latest = view(cycle)
        assert latest["runs_total"] == count
        assert latest["cash_cents"] == (9_124_500 if cycle == A else 9_998_600)
        if cycle == A:
            assert latest["positions"][0]["quantity"] == 700
        else:
            assert not latest["positions"]
        replay = call("replay?cycle_id=" + cycle)
        assert replay["ok"] and (replay["runs_verified"], replay["fills_verified"]) == (count, fills)
        assert replay["cash_cents"] == latest["cash_cents"]
        retry = call("simulations", proposal(cycle))
        assert retry["duplicate"] and retry["evidence"]["market_data_pack_id"] == pack
        assert retry["outcome"]["fill"]["price_cents"] == (1250 if cycle == A else 2000)
        report[cycle] = {"pack_id": pack, "latest": latest, "historical": old, "replay": replay}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Two reviewed packs: same capital/strategy/external ID, scoped prices/history/replay passed")

def main():
    if "--verify-only" in sys.argv:
        verify()
        return
    create(A, PACK_A)
    a_data = incoming(A)
    # Same original reference price, same execution level as the reviewed fixture.
    a_data["quote"]["price_cents"] = json.loads((ROOT / "fixtures/quote.json").read_text(encoding="utf8"))["quote"]["price_cents"]
    call("quotes", a_data)
    filled = call("simulations", proposal(A))
    assert filled["outcome"]["fill"]["quantity"] == 700
    call("experiments/" + A + "/complete", {})
    old = view(A, "2026-10-30")
    frozen = call("replay?cycle_id=" + A)
    # Only after completing A do we select/register B and add B's differently priced evidence.
    create(B, PACK_B)
    b_data = incoming(B, 2000)
    stored = call("quotes", b_data)
    assert stored["quote"]["quote_id"] == filled["evidence"]["quote"]["quote_id"]
    assert stored["quote"]["source"] == filled["evidence"]["quote"]["source"]
    assert call("simulations", proposal(B))["outcome"]["fill"]["quantity"] == 400
    assert call("quotes", b_data) == stored
    bad = copy.deepcopy(b_data)
    bad["execution"]["price_cents"] += 1
    call("quotes", bad, 409)
    wrong = proposal(B)
    wrong["quote_ref"]["market_data_pack_id"] = PACK_A
    call("simulations", wrong, 409)
    assert sell("2026-10-30", "pack-demo-t1")["outcome"]["reason"] == "no_t1_sellable_lot"
    assert sell("2026-11-02", "pack-demo-next-day")["outcome"]["status"] == "filled"
    assert view(A, "2026-10-30") == old
    assert call("replay?cycle_id=" + A) == frozen
    call("experiments/" + B + "/complete", {})
    # No upload/registration route is offered.
    call("market-data-packs/" + PACK_B, {"manifest": {}}, 405)
    verify()

if __name__ == "__main__":
    main()

