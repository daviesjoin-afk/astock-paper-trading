# -*- coding: utf-8 -*-
"""账户开户业务编排；账户写入统一交给 paper_account_ledger。"""
from paper_account_ledger import ensure_profile, insert_account, set_version

__all__ = ["ensure_profile", "insert_account", "set_version", "provision_user_accounts"]


def provision_user_accounts(conn, participant_ids, *, spec_for, now_fn, audit_fn):
    """Create missing paused, zero-funded user strategy ledger rows."""
    for account_id in participant_ids:
        exists = conn.execute("SELECT 1 FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if exists:
            continue
        spec = spec_for(account_id)
        now = now_fn()
        insert_account(conn, fields={
            "id": account_id, "name": spec["name"],
            "source_strategy": spec["source_strategy"], "status": "paused",
            "initial_cash": 0.0, "cash": 0.0, "cycle_days": spec["cycle_days"],
            "max_positions": spec["max_positions"], "max_weight": spec["max_weight"],
            "max_exposure": spec["max_exposure"], "risk_profile": spec["risk_profile"],
            "version": spec["strategy_version"], "benchmark_start": None,
            "created_at": now, "updated_at": now,
        })
        audit_fn(conn, account_id, "user_strategy_account_provisioned",
                 f"用户策略 {spec['name']} 已开户（paused，等待周期分配资金；DSL v{spec['dsl_version']}）")
