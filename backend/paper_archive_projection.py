# -*- coding: utf-8 -*-
"""Immutable paper archive snapshot 到活动列表的纯投影。"""
from __future__ import annotations

import json
import sqlite3


def project_order_rows(archives, loads):
    """从归档快照提取只读订单行，并隔离损坏的历史快照。"""
    archived_rows = []
    archived_seen = set()
    for archive in archives:
        try:
            snapshot = loads(archive.get("snapshot"), {}) or {}
            archived_names = {
                row.get("id"): row.get("name")
                for row in (snapshot.get("paper_accounts") or [])
            }
            fills_by_order = {}
            for fill in snapshot.get("paper_fills") or []:
                try:
                    fill_order_id = int(fill.get("order_id"))
                except (TypeError, ValueError):
                    continue
                item_fill = dict(fill)
                raw_evidence = item_fill.pop("execution_evidence", None)
                try:
                    item_fill["market_evidence"] = (json.loads(raw_evidence) or {}).get("market_evidence") if raw_evidence else None
                except (TypeError, ValueError, AttributeError):
                    item_fill["market_evidence"] = None
                fills_by_order.setdefault(fill_order_id, []).append(item_fill)
            for archived in snapshot.get("paper_orders") or []:
                item = dict(archived)
                key = (
                    str(archive.get("cycle_key") or ""),
                    str(item.get("id") or ""),
                    str(item.get("created_at") or ""),
                )
                if key in archived_seen:
                    continue
                archived_seen.add(key)
                raw_payload = item.pop("risk_payload", None)
                payload = raw_payload if isinstance(raw_payload, dict) else {}
                execution = payload.get("execution") if isinstance(payload, dict) else {}
                execution = execution if isinstance(execution, dict) else {}
                item["account_name"] = archived_names.get(item.get("account_id"), item.get("account_id"))
                item["archived_cycle"] = archive.get("cycle_key")
                item["read_only"] = True
                events = fills_by_order.get(int(item.get("id") or 0), [])
                item["desired_qty"] = max(0, int(item.get("qty") or 0))
                item["filled_qty"] = sum(max(0, int(fill.get("qty") or 0)) for fill in events)
                item["remaining_qty"] = max(0, item["desired_qty"] - item["filled_qty"])
                item["execution_asof"] = execution.get("as_of") or next(
                    (fill.get("execution_asof") for fill in events if fill.get("execution_asof")), None,
                )
                item["market_data_state"] = execution.get("market_state")
                item["market_data_validation"] = (execution.get("market_evidence") or {}).get("quote_validation")
                item["market_data_asof"] = (execution.get("market_evidence") or {}).get("quote_at")
                item["market_evidence"] = execution.get("market_evidence") or next(
                    (fill.get("market_evidence") for fill in events if fill.get("market_evidence")), None,
                )
                item["execution_reason_codes"] = execution.get("reason_codes") or []
                item["blocking_reason"] = item.get("reason") if item["remaining_qty"] else None
                item["slippage_amount"] = sum(float(fill.get("slippage_amount") or 0) for fill in events)
                item["fill_events"] = events
                item["allowed_actions"] = []
                archived_rows.append(item)
        except (TypeError, ValueError, sqlite3.Error):
            # One damaged legacy archive must not blank the current ledger.
            continue
    return archived_rows
