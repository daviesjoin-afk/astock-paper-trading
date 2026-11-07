"""Vertical research reproducibility demo against the local Rust service.

Fixed strategy definition + fixed research input -> research signals that must be
reproducible, and trading facts that only the Rust kernel decides.

    python scripts/research_reproduction_demo.py            # first run
    python scripts/research_reproduction_demo.py --verify-only   # after a restart

All prices and capacities come from the reviewed offline fixture. They are
synthetic test inputs, not market data and not a performance claim.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "scripts"))
import run_research  # noqa: E402
from experiment_demo import call  # noqa: E402

CYCLE_A = "research-a"
CYCLE_B = "research-b"
CYCLE_DRIFT = "research-drift"
PACK = "cn-offline-fixture-pack-1"


def reviewed_strategy() -> dict:
    return json.loads((ROOT / "data" / "strategy-momentum-v1.json").read_text(encoding="utf8"))


def frozen_manifest() -> dict:
    return json.loads((ROOT / "data" / "research-input-offline-v1.json").read_text(encoding="utf8"))


def frozen_output() -> dict:
    return json.loads((ROOT / "data" / "research-output-momentum-v1.json").read_text(encoding="utf8"))


def entry_for(manifest: dict, quote_id: str) -> dict:
    return next(entry for entry in manifest["entries"] if entry["quote"]["quote_id"] == quote_id)


def evidence(manifest: dict, signal: dict) -> dict:
    entry = entry_for(manifest, signal["quote_id"])
    level = entry[signal["side"]]
    return {
        "market_data_pack_id": PACK,
        "schema_version": 2,
        "available_at": entry["available_at"],
        "quote": entry["quote"],
        "execution": {
            "schema_version": 2,
            "quote_ref": {"market_data_pack_id": PACK, "source": entry["quote"]["source"],
                          "quote_id": entry["quote"]["quote_id"]},
            "order_side": signal["side"],
            "price_cents": level["price_cents"],
            "available_quantity": level["available_quantity"],
            "observed_at": entry["quote"]["observed_at"],
        },
    }


def request(cycle: str, signal: dict) -> dict:
    return {
        "schema_version": 4,
        "cycle_id": cycle,
        "strategy_version_id": reviewed_strategy()["strategy_version_id"],
        "signal_id": signal["signal_id"],
        "decision_at": signal["decision_at"],
        "signal": {"symbol": signal["symbol"], "side": signal["side"]},
        "quote_ref": {"market_data_pack_id": signal["market_data_pack_id"],
                      "source": signal["source"], "quote_id": signal["quote_id"]},
    }


def create(cycle: str, cash: int = 10_000_000) -> dict:
    return call("experiments", {
        "schema_version": 1, "cycle_id": cycle, "initial_cash_cents": cash,
        "strategy": reviewed_strategy(), "market_data_pack_id": PACK,
        "execution_policy_version": "v2-paper-policy-6",
    })


def run_cycle(cycle: str) -> list:
    manifest = frozen_manifest()
    runs = []
    for signal in frozen_output()["signals"]:
        call("quotes", evidence(manifest, signal))
        runs.append(call("simulations", request(cycle, signal)))
    return runs


def fills(cycle: str) -> list:
    rows = call("simulations?cycle_id=" + cycle)
    return [row["outcome"]["fill"] for row in reversed(rows) if row["outcome"].get("fill")]


def reproduction(cycle: str, expected: int = 200) -> dict:
    return call("experiments/" + cycle + "/research-reproduction", expected=expected)


def verify() -> dict:
    """Restart-safe checks: no writes, only re-verification of frozen facts."""
    report = {}
    for cycle in (CYCLE_A, CYCLE_B):
        replay = call("replay?cycle_id=" + cycle)
        reproduced = reproduction(cycle)
        assert replay["ok"] and replay["fills_verified"] == 2, replay
        assert reproduced["ok"] and reproduced["signals_verified"] == 2, reproduced
        assert reproduced["research_input_id"] == "offline-research-input-v1"
        assert call("experiments/" + cycle)["status"] == "completed"
        report[cycle] = {"replay": replay, "research_reproduction": reproduced,
                         "fills": fills(cycle)}
    assert report[CYCLE_A]["research_reproduction"]["research_output_digest"] == \
        report[CYCLE_B]["research_reproduction"]["research_output_digest"]
    assert report[CYCLE_A]["research_reproduction"]["parameters_digest"] == \
        report[CYCLE_B]["research_reproduction"]["parameters_digest"]
    assert report[CYCLE_A]["fills"] == report[CYCLE_B]["fills"]
    # The drift experiment remains an honest non-reproduction across restarts.
    assert reproduction(CYCLE_DRIFT, expected=500)
    return report


def main() -> None:
    if "--verify-only" in sys.argv:
        print(json.dumps(verify(), ensure_ascii=False, indent=2))
        print("Research reproducibility after restart: identical signals, digests and fills")
        return

    # 1. The Python side must regenerate the reviewed research output byte for byte.
    printed = run_research.check("momentum")
    assert printed["signals"] == 2, printed

    # 2. Two independent experiments realise the same frozen signals. Identical
    # starting capital keeps the comparison about research identity, not sizing;
    # capital isolation itself is covered by scripts/experiment_demo.py.
    create(CYCLE_A)
    create(CYCLE_B)
    create(CYCLE_DRIFT)
    runs_a = run_cycle(CYCLE_A)
    runs_b = run_cycle(CYCLE_B)
    assert all(run["outcome"]["status"] == "filled" for run in runs_a), runs_a
    assert all(run["outcome"]["status"] == "filled" for run in runs_b), runs_b

    # Python never decided quantity, price or cash: those come back from Rust.
    for run in runs_a:
        fill = run["outcome"]["fill"]
        assert fill["quantity"] == 700 and fill["price_cents"] in (1295, 1245), fill
        assert run["outcome"]["cash_after_cents"] > 0
    assert fills(CYCLE_A) == fills(CYCLE_B)

    # 3. Research reproduction is a separate proof from replay.
    reproduced_a = reproduction(CYCLE_A)
    reproduced_b = reproduction(CYCLE_B)
    assert reproduced_a["research_output_digest"] == reproduced_b["research_output_digest"]
    assert reproduced_a["source_digest"] == reviewed_strategy()["source_digest"]
    assert reproduced_a["parameters_digest"] == reviewed_strategy()["parameters_digest"]
    assert call("replay?cycle_id=" + CYCLE_A)["fills_verified"] == 2

    # 4. A decision outside the frozen research output replays but is not reproducible.
    manifest = frozen_manifest()
    drift_signal = {
        "signal_id": "post-hoc-signal",
        "decision_at": manifest["entries"][-1]["decision_at"],
        "symbol": manifest["entries"][-1]["quote"]["symbol"],
        "side": "buy",
        "market_data_pack_id": PACK,
        "source": manifest["entries"][-1]["quote"]["source"],
        "quote_id": manifest["entries"][-1]["quote"]["quote_id"],
    }
    call("quotes", evidence(manifest, drift_signal))
    call("simulations", request(CYCLE_DRIFT, drift_signal))
    assert call("replay?cycle_id=" + CYCLE_DRIFT)["ok"]
    reproduction(CYCLE_DRIFT, expected=500)

    # 5. Completing A freezes it without touching B.
    before_b = call("portfolio?cycle_id=" + CYCLE_B)
    call("experiments/" + CYCLE_A + "/complete", {})
    call("experiments/" + CYCLE_B + "/complete", {})
    assert call("portfolio?cycle_id=" + CYCLE_B) == before_b
    verify()
    print("Frozen strategy artifact + frozen research input reproduced two independent experiments")
    print("Extra non-frozen decision replays but fails research reproduction")
    print("Python produced signals only; Rust owned eligibility, price, capacity, quantity and cash")


if __name__ == "__main__":
    main()
