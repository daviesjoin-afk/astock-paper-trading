# -*- coding: utf-8 -*-
"""Attach and detach already-provisioned user strategy ledger rows."""


def _row(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description or ()]
    return dict(zip(columns, row, strict=True))


def reconcile_user_cycle_accounts(
    conn,
    cycle,
    enabled_ids,
    user_account_ids,
    *,
    builtin_active_ids,
    spec_for,
    available_capital_fn,
    benchmark_fn,
    num_fn,
    date_fn,
    now_fn,
    audit_fn,
):
    """Reconcile user ledger rows against a caller-resolved cycle target."""
    enabled = set(enabled_ids)
    builtin_ids = set(builtin_active_ids)
    cycle_id = cycle["id"]
    for account_id in user_account_ids:
        if account_id in builtin_ids:
            continue
        row = _row(conn, "SELECT * FROM paper_accounts WHERE id=?", (account_id,))
        if row is None:
            continue
        user_spec = spec_for(account_id)
        if account_id not in enabled and row["cycle_id"] == cycle_id:
            conn.execute(
                "UPDATE paper_accounts SET cycle_id=NULL,status='paused',initial_cash=0,cash=0,updated_at=? WHERE id=?",
                (now_fn(), account_id),
            )
            continue
        if row["cycle_id"] is None and account_id in enabled:
            capital = num_fn(cycle["capital"], 100000.0)
            account_capital = (
                capital / max(len(enabled_ids), 1)
                if str(cycle["cycle_key"] or "").startswith("legacy-")
                else available_capital_fn(account_id)
            )
            benchmark = benchmark_fn()
            conn.execute(
                "UPDATE paper_accounts SET cycle_id=?,mode=?,style=?,status=?,initial_cash=?,cash=?,benchmark_start=?,daily_start_nav=?,daily_nav_date=?,risk_profile=?,version=?,max_positions=?,max_weight=?,max_exposure=?,updated_at=? WHERE id=?",
                (cycle_id, user_spec["mode"], user_spec["default_style"], cycle["status"],
                 account_capital, account_capital, benchmark, account_capital, date_fn().isoformat(),
                 user_spec["risk_profile"], user_spec["strategy_version"], user_spec["max_positions"],
                 user_spec["max_weight"], user_spec["max_exposure"], now_fn(), account_id),
            )
            conn.execute("DELETE FROM paper_nav WHERE account_id=?", (account_id,))
            conn.execute(
                "INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
                (account_id, date_fn().isoformat(), account_capital, 0.0, account_capital, benchmark, now_fn()),
            )
            conn.execute(
                "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (cycle_id, account_id, user_spec["strategy_version"], user_spec["default_style"], "{}",
                 "用户策略接入周期", date_fn().isoformat(), now_fn()),
            )
            audit_fn(
                conn,
                account_id,
                "user_strategy_cycle_attached",
                f"用户策略 {user_spec['name']} 接入周期（{user_spec['lifecycle_stage']} 档）",
            )


def detach_cycle(conn, account_id, *, now):
    """账户退出当前周期：清空 cycle_id 并把账户置为暂停、资金归零。"""
    conn.execute(
        "UPDATE paper_accounts SET cycle_id=NULL,status='paused',"
        "initial_cash=0,cash=0,updated_at=? WHERE id=?",
        (now, account_id),
    )


def set_status(conn, *, status, now, clause, clause_params=()):
    """账户启停：``clause`` 由调用方给出（活动周期过滤），本入口只落库状态。"""
    if not clause:
        raise ValueError("set_status 需要显式过滤条件")
    return conn.execute(
        f"UPDATE paper_accounts SET status=?, updated_at=? WHERE {clause}",
        (status, now, *clause_params),
    ).rowcount


def set_cycle_capital(conn, account_id, *, cycle_id, capital, now):
    """周期资金配置：在同一周期下把初始资金/现金/基准净值设为同一份额。

    ``WHERE cycle_id=? AND id=?`` 保留原保护 —— 账户已经切到别的周期时不写。
    """
    return conn.execute(
        "UPDATE paper_accounts SET initial_cash=?,cash=?,daily_start_nav=?,updated_at=?"
        " WHERE cycle_id=? AND id=?",
        (capital, capital, capital, now, cycle_id, account_id),
    ).rowcount
