"""Deterministic candidate projection over supplied factors and live facts.

No market access, ledger access, registry lookup or clock reads belong here.
"""
import pandas as pd

import paper_candidate_live_fields as PCLF


def overlay_live_fields(table, live_map, factor_date):
    """Overlay only usable supplied live values; preserve historical factors."""
    live_field_map = {
        "price": "price", "pct": "pct", "amount": "amount",
        "turnover": "turnover", "main_pct": "main_pct",
        "super_net_raw": "super_net", "main_net": "main_net",
    }
    PCLF.overlay(table, live_map, live_field_map)
    table["quote_at"] = pd.Series(
        {code: (live_map.get(str(code)) or {}).get("quote_at") for code in table.index}
    )
    table["quote_source"] = pd.Series(
        {code: (live_map.get(str(code)) or {}).get("source") or "live_snapshot" for code in table.index}
    )
    table["flow_source"] = pd.Series(
        {code: (live_map.get(str(code)) or {}).get("flow_source") or "eastmoney_snapshot" for code in table.index}
    )
    table["flow_quote_at"] = pd.Series(
        {code: (live_map.get(str(code)) or {}).get("flow_quote_at") or (live_map.get(str(code)) or {}).get("quote_at")
         for code in table.index}
    )
    table["historical_factor_date"] = factor_date
    return table


def select_candidate_lanes(picks, *, reserved_limit=6, total_limit=30):
    """Keep original lane ordering while reserving space for early-hot facts."""
    reserved_statuses = {"hot_leader_watch", "concept_expansion_lane", "sector_surge_lane", "ths_hot_lane"}
    hot_watch = [pick for pick in picks if pick.get("candidate_status") in reserved_statuses][:reserved_limit]
    regular = [pick for pick in picks if pick.get("candidate_status") not in reserved_statuses]
    return regular[:max(0, total_limit - len(hot_watch))] + hot_watch
