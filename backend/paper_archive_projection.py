# -*- coding: utf-8 -*-
"""Immutable paper archive snapshot 到活动列表的纯投影。"""
from __future__ import annotations

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
                item.pop("risk_payload", None)
                item["account_name"] = archived_names.get(item.get("account_id"), item.get("account_id"))
                item["archived_cycle"] = archive.get("cycle_key")
                item["read_only"] = True
                archived_rows.append(item)
        except (TypeError, ValueError, sqlite3.Error):
            # One damaged legacy archive must not blank the current ledger.
            continue
    return archived_rows
