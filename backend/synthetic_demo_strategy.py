# -*- coding: utf-8 -*-
"""Deterministic, offline StrategyPlugin example built entirely from synthetic data.

This module is intentionally isolated from the real paper-trading database, market
feeds and account state.  It creates a strategy definition in an in-memory SQLite
database, temporarily registers a real ``StrategyPlugin``, runs factor -> candidate
-> entry/risk/exit contracts, then unregisters the plugin before returning.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import pandas as pd

import strategy_plugins as PLUGINS
import strategy_registry as SR
import strategy_runtime as SRT

DEMO_STRATEGY_ID = "synthetic_demo"
DEMO_SELECTOR_ID = "synthetic_demo_selector"
DEMO_DATA_DATE = "2026-01-15"
DEMO_FACTOR_INPUTS = ("momentum", "quality", "liquidity")


def synthetic_factor_table() -> pd.DataFrame:
    """Return a fixed, non-market factor table used by the example and CI."""
    table = pd.DataFrame(
        [
            {"code": "SYNTH_A", "momentum": 0.90, "quality": 0.80, "liquidity": 0.70},
            {"code": "SYNTH_B", "momentum": 0.75, "quality": 0.90, "liquidity": 0.60},
            {"code": "SYNTH_C", "momentum": 0.40, "quality": 0.95, "liquidity": 0.95},
            {"code": "SYNTH_D", "momentum": 0.70, "quality": 0.50, "liquidity": 0.80},
        ]
    )
    table.attrs["data_date"] = DEMO_DATA_DATE
    return table


def _candidate_runner(table: pd.DataFrame, *, topn: int = 2, **_kwargs) -> dict[str, Any]:
    """Small deterministic selector over declared synthetic factors only."""
    required = {"code", *DEMO_FACTOR_INPUTS}
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"synthetic demo factor table missing column: {missing[0]}")
    rows: list[dict[str, Any]] = []
    for raw in table.loc[:, ["code", *DEMO_FACTOR_INPUTS]].to_dict("records"):
        momentum = float(raw["momentum"])
        quality = float(raw["quality"])
        liquidity = float(raw["liquidity"])
        if momentum < 0.60 or quality < 0.55 or liquidity < 0.50:
            continue
        score = 0.50 * momentum + 0.30 * quality + 0.20 * liquidity
        rows.append(
            {
                "code": str(raw["code"]),
                "score": round(score, 6),
                "factors": {
                    "momentum": momentum,
                    "quality": quality,
                    "liquidity": liquidity,
                },
            }
        )
    rows.sort(key=lambda item: (-float(item["score"]), str(item["code"])))
    picks = rows[: max(int(topn), 0)]
    return {"strategy": DEMO_SELECTOR_ID, "count": len(picks), "picks": picks}


def build_demo_plugin() -> PLUGINS.StrategyPlugin:
    """Build the example plugin without registering it globally."""
    return PLUGINS.StrategyPlugin(
        strategy_id=DEMO_STRATEGY_ID,
        selector_id=DEMO_SELECTOR_ID,
        factor_inputs=DEMO_FACTOR_INPUTS,
        candidate_runner=_candidate_runner,
        # Use one audited policy family that exercises opening-event,
        # entry-economics, downside and recovery contracts.  The demo never
        # opens a paper account or submits an order.
        entry_policy_key="tq_breakout",
        exit_policy_key="tq_breakout",
    )


def _demo_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    SR.ensure_schema(conn)
    SR.create_user_definition(
        conn,
        DEMO_STRATEGY_ID,
        "Synthetic Offline Demo",
        implementation_key=DEMO_SELECTOR_ID,
        description="Deterministic synthetic StrategyPlugin example; never used for live trading.",
        metadata={"demo": True, "data_source": "synthetic"},
        actor="synthetic_demo",
    )
    return conn


def run_demo() -> dict[str, Any]:
    """Exercise factor -> candidate -> entry/risk/exit through StrategyPlugin.

    The returned object contains only fixed synthetic inputs and compiled strategy
    contract data.  It intentionally contains no process environment, filesystem
    path, real account state, credential material or live market data.
    """
    plugin = build_demo_plugin()
    conn = _demo_connection()
    registered_here = False
    SRT.clear_cache()
    try:
        if DEMO_STRATEGY_ID in PLUGINS.plugin_ids():
            raise RuntimeError("synthetic demo plugin is already registered")
        PLUGINS.register_plugin(plugin)
        registered_here = True
        table = synthetic_factor_table()
        candidates = PLUGINS.select_candidates(DEMO_STRATEGY_ID, table, topn=2)
        registered = PLUGINS.get_plugin(DEMO_STRATEGY_ID)
        manifest = registered.manifest(conn=conn)
        entry = registered.entry_contract(conn)
        exit_contract = registered.exit_contract(conn)
        return {
            "demo": True,
            "data_source": "synthetic",
            "data_date": DEMO_DATA_DATE,
            "manifest": manifest,
            "factors": table.to_dict("records"),
            "candidates": candidates,
            "entry": {
                "opening_event": entry["opening_event"],
                "entry_economics": entry["entry_economics"],
            },
            "risk": entry["risk_profile"],
            "exit": {
                "intraday_downside": exit_contract["intraday_downside"],
                "recovery": exit_contract["recovery"],
                "position_review_min_hold_days": exit_contract["position_review_min_hold_days"],
                "risk_reject_cooldown_minutes": exit_contract["risk_reject_cooldown_minutes"],
            },
        }
    finally:
        if registered_here:
            PLUGINS.unregister_plugin(DEMO_STRATEGY_ID)
        SRT.clear_cache()
        conn.close()


def main() -> int:
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
