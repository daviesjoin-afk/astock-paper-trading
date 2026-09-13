# -*- coding: utf-8 -*-
"""Materialize missing user strategy ledger identities."""


def provision_user_accounts(conn, participant_ids, *, spec_for, now_fn, audit_fn):
    """Create missing paused, zero-funded user strategy ledger rows."""
    for account_id in participant_ids:
        exists = conn.execute("SELECT 1 FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if exists:
            continue
        spec = spec_for(account_id)
        now = now_fn()
        conn.execute(
            """INSERT INTO paper_accounts
            (id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,max_weight,max_exposure,
             risk_profile,version,benchmark_start,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, spec["name"], spec["source_strategy"], "paused", 0.0, 0.0,
             spec["cycle_days"], spec["max_positions"], spec["max_weight"], spec["max_exposure"],
             spec["risk_profile"], spec["strategy_version"], None, now, now),
        )
        audit_fn(conn, account_id, "user_strategy_account_provisioned",
                 f"用户策略 {spec['name']} 已开户（paused，等待周期分配资金；DSL v{spec['dsl_version']}）")
