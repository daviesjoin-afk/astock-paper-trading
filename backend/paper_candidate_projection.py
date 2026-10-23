"""Deterministic candidate projection over supplied factors and live facts.

No market access, ledger access, registry lookup or clock reads belong here.
"""
import pandas as pd

import paper_candidate_live_fields as PCLF
from paper_numeric import num
from paper_trading_rules import asset_type


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


def risk_flag_codes(universe_rows):
    """Codes the trading rules refuse regardless of strategy score or rank."""
    return {
        str(row.get("code")) for row in (universe_rows or [])
        if row.get("risk_flag")
        or "ST" in str(row.get("name") or "").upper()
        or "退" in str(row.get("name") or "")
    }


def filter_executable_candidates(picks, *, mode=None, excluded_codes=()):
    """Drop what the runtime cannot execute, keeping the original order.

    Only three runtime predicates belong here: the intraday-T asset
    restriction, the security-scope exclusion set and a non-positive price.
    Sizing, cash and slot decisions stay with execution -- a candidate is
    never deleted because one lot looks expensive.
    """
    excluded = set(excluded_codes or ())
    if mode == "intraday_t":
        picks = [pick for pick in picks if asset_type(pick.get("code"), pick.get("name")) == "stock_t1"]
    picks = [pick for pick in picks if pick["code"] not in excluded]
    return [pick for pick in picks if num(pick.get("price")) > 0]
