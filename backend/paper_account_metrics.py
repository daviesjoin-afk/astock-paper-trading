"""Account performance read projection.

External facts and clocks enter through ``AccountMetricDependencies``.  This
module never imports the trading facade and does not add database queries.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

import paper_account_specs as ACS
from paper_numeric import num as _num
from paper_common import load_json as _loads


@dataclasses.dataclass(frozen=True, slots=True)
class AccountMetricDependencies:
    account_specs: dict[str, Any]
    today: Callable[[], str]
    account_metric_inputs: Callable[..., dict]
    account_reference_capital: Callable[..., float]
    market_session: Callable[..., dict]
    position_rows: Callable[..., list]
    quotes: Callable[..., dict]
    risk_profile: Callable[..., dict]
    today_position_performance: Callable[..., tuple]
    today_sell_performance: Callable[..., dict]


def bind_dependencies(factory):
    global _dependencies
    _dependencies = factory


def _ports():
    if _dependencies is None:
        raise RuntimeError("paper_account_metrics dependencies are not bound")
    return _dependencies()


_dependencies = None


def account_metrics(conn, account, quotes=None, positions=None, metric_cache=None, allow_network=True):
    # Orphan accounts (removed from ACCOUNT_SPECS but still present in the
    # ledger) must not crash the whole dashboard.
    spec = _ports().account_specs.get(account["id"]) or {"entry_model_name": str(account.get("id") or "unknown")}
    profile = _ports().risk_profile(account, conn=conn)
    if metric_cache is None:
        nav_row = conn.execute(
            "SELECT * FROM paper_nav WHERE account_id=? ORDER BY nav_date DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
    else:
        nav_row = (metric_cache.get("latest_nav") or {}).get(account["id"])
    positions = positions if positions is not None else _ports().position_rows(conn, account["id"])
    if quotes is None:
        quotes = _ports().quotes([p["code"] for p in positions]) if positions else {}
    missing_valuation_codes = [
        str(p.get("code")) for p in positions
        if not isinstance((quotes.get(p["code"]) or {}).get("price"), (int, float))
    ]
    valuation_status = (
        "cost_fallback" if missing_valuation_codes
        else str((nav_row["quote_status"] if nav_row and "quote_status" in nav_row.keys() else None) or "verified")
    )
    position_value = sum(
        _num((quotes.get(p["code"]) or {}).get("price"), _num(p["cost"])) * _num(p["qty"])
        for p in positions
    )
    today = _ports().today()
    if metric_cache is None:
        # 直查分支与缓存分支必须产出**同一批**已验证成交：两处各写一份 SQL 会让
        # trade_count / realized_pnl / 胜率随调用方是否传 cache 而漂移（同一账户
        # 同一时刻出现两个执行绩效）。统一走仓储的已验证卖出投影。
        sells = list(
            (
                _ports().account_metric_inputs(conn, [account["id"]], today).get("sells") or {}
            ).get(account["id"], [])
        )
    else:
        sells = list((metric_cache.get("sells") or {}).get(account["id"], []))
    # A strategy may have sold its last position today.  In that case the
    # current-position quote list is empty, but today's realized attribution
    # still needs the sold symbols' live quotes (for the previous-close
    # baseline).  Fetch them explicitly instead of treating an empty holding
    # list as "no daily P&L".
    sell_codes = {
        str(order.get("code") or "") for order in sells
        if order.get("code") and str(order.get("executed_at") or order.get("created_at") or "")[:10] == today
    }
    missing_sell_codes = sorted(code for code in sell_codes if code not in quotes)
    if missing_sell_codes and allow_network:
        quotes = {**quotes, **_ports().quotes(missing_sell_codes)}
    if metric_cache is None:
        buy_count = int(conn.execute(
            "SELECT COUNT(*) FROM paper_fills WHERE account_id=? AND side='buy'",
            (account["id"],),
        ).fetchone()[0])
        rejected = int(conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status!='filled'",
            (account["id"],),
        ).fetchone()[0])
    else:
        buy_count = int((metric_cache.get("buy_count") or {}).get(account["id"], 0))
        rejected = int((metric_cache.get("rejected") or {}).get(account["id"], 0))
    unrealized = sum((_num((quotes.get(p["code"]) or {}).get("price"), _num(p["cost"])) - _num(p["cost"])) * _num(p["qty"]) for p in positions)
    position_cost_value = sum(_num(p.get("cost")) * _num(p.get("qty")) for p in positions)
    holding_return_pct = (
        unrealized / position_cost_value * 100
        if position_cost_value > 0 else None
    )
    # In shared-pool mode an account's cash balance is merely the ledger
    # bucket used to attribute orders; it is not an independent 100,000 yuan
    # account.  Therefore NAV - initial_cash is not strategy P&L and can show
    # artificial losses of tens of thousands when another strategy consumed
    # the shared cash.  Attribute only actual completed sell P&L plus the
    # current mark-to-market P&L of this strategy's open lots.
    realized = sum(_num(order.get("realized_pnl")) for order in sells)
    strategy_pnl = realized + unrealized
    ledger_initial_cash = max(0.0, _num(account.get("initial_cash"), 0.0))
    # A sleeve introduced after a shared-pool cycle begins can have no private
    # ledger allocation without minting cash into the pool.  Its optional
    # reference capital is presentation/risk context only; orders still use
    # the real pooled cash through _shared_account_exposure.
    initial_cash = _ports().account_reference_capital(account)
    # In a shared pool, ``cash`` is only a settlement allocation bucket.  It
    # must never be used as a strategy NAV: another model spending shared cash
    # would otherwise manufacture a large loss here.  Persisted account NAV,
    # drawdown, excess return and the headline card therefore all share this
    # synthetic but fully attributable definition.
    nav = initial_cash + strategy_pnl
    if metric_cache is None:
        navs = [
            _num(row[0]) for row in conn.execute(
                "SELECT nav FROM paper_nav WHERE account_id=? ORDER BY nav_date",
                (account["id"],),
            ).fetchall()
        ]
    else:
        navs = [_num(value) for value in (metric_cache.get("navs") or {}).get(account["id"], [])]
    if ledger_initial_cash <= 0 < initial_cash:
        # Existing zero NAV snapshots belong to the late-join bookkeeping
        # state, not a real loss.  Rebase only this sleeve's synthetic series;
        # the persisted shared-pool NAV is deliberately untouched.
        navs = [initial_cash if abs(value) < 0.01 else value for value in navs]
    if not navs or abs(navs[-1] - nav) > 0.01:
        navs.append(nav)
    else:
        navs[-1] = nav
    peak = navs[0] if navs else nav
    dd = 0.0
    for value in navs:
        peak = max(peak, value)
        dd = min(dd, value / peak - 1 if peak else 0)
    wins = [order for order in sells if _num(order.get("realized_pnl")) > 0]
    losses = [order for order in sells if _num(order.get("realized_pnl")) < 0]
    win_rate = len(wins) / len(sells) * 100 if sells else None
    gain = sum(_num(order.get("realized_pnl")) for order in wins)
    loss = abs(sum(_num(order.get("realized_pnl")) for order in losses))
    benchmark = None
    if nav_row and nav_row["benchmark"] and account.get("benchmark_start"):
        benchmark = nav_row["benchmark"] / account["benchmark_start"] - 1
    market_session = _ports().market_session()
    if metric_cache is None:
        previous_nav_row = conn.execute(
            "SELECT nav,nav_date FROM paper_nav WHERE account_id=? AND nav_date<? ORDER BY nav_date DESC LIMIT 1",
            (account["id"], today),
        ).fetchone()
    else:
        previous_nav_row = (metric_cache.get("previous_nav") or {}).get(account["id"])
    # Today's strategy attribution is also independent of the shared cash
    # bucket: open-position mark-to-market change plus today's completed sell
    # P&L.  This prevents a cash transfer from appearing as +/- 10,000 yuan.
    today_position_pnl = 0.0
    today_position_base = 0.0
    today_has_quote = False
    for position in positions:
        quote = quotes.get(position["code"]) or {}
        price = _num(quote.get("price"), _num(position.get("cost")))
        pnl, _pct, baseline = _ports().today_position_performance(position, price, quote, today)
        if pnl is not None:
            today_has_quote = True
            today_position_pnl += _num(pnl)
            today_position_base += _num(baseline)
    today_sells = [
        order for order in sells
        if str(order.get("executed_at") or order.get("created_at") or "")[:10] == today
    ]
    today_sell = _ports().today_sell_performance(today_sells, quotes, today)
    today_sell_pnl = today_sell["pnl"]
    today_sell_available = today_sell["covered"] == today_sell["total"]
    # Daily P&L is available when either an open holding was marked or there
    # was a completed sell today.  Previously ``today_has_quote`` was required
    # unconditionally, so a strategy that fully exited showed an em dash even
    # though its realized loss/profit was known.
    today_activity = today_has_quote or today_sell["total"] > 0
    today_pnl = (
        (today_position_pnl if today_has_quote else 0.0) + today_sell_pnl
        if market_session["today_pnl_available"] and today_activity and today_sell_available
        else None
    )
    today_baseline_nav = (
        (today_position_base if today_has_quote else 0.0) + today_sell["baseline"]
        if today_pnl is not None else None
    )
    today_return_pct = today_pnl / today_baseline_nav * 100 if today_pnl is not None and today_baseline_nav > 0 else None
    daily_loss_pct = max(0.0, -(today_return_pct or 0.0))
    return {
        "id": account["id"], "name": account["name"], "status": account["status"], "cycle_days": account["cycle_days"],
        "mode": account.get("mode"), "style": account.get("style"),
        "style_name": ACS.style_name(account.get("style"), account.get("style")),
        "risk_profile": account.get("risk_profile"),
        "risk_profile_name": profile.get("name", account.get("risk_profile")),
        "entry_model_name": spec["entry_model_name"],
        "risk_limits": {
            "max_weight_pct": round(profile["max_weight"] * 100, 1),
            "max_exposure_pct": round(profile["max_exposure"] * 100, 1),
            "max_industry_pct": round(profile["max_industry"] * 100, 1),
            "single_risk_pct": round(profile["single_risk"] * 100, 2),
            "daily_loss_pct": round(profile["daily_loss"] * 100, 1),
            "drawdown_pct": round(profile["drawdown"] * 100, 1),
        },
        "version": account["version"], "params": _loads(account.get("params"), {}),
        "initial_cash": round(initial_cash, 2), "cash": account["cash"],
        "ledger_initial_cash": round(ledger_initial_cash, 2),
        "capital_allocation_mode": "shared_pool_reference" if ledger_initial_cash <= 0 < initial_cash else "shared_pool_ledger",
        "shared_pool_reference_capital": round(initial_cash, 2) if ledger_initial_cash <= 0 < initial_cash else None,
        "cash_allocation_bucket": round(_num(account["cash"]), 2),
        "strategy_synthetic_nav": round(nav, 2),
        "nav": round(nav, 2), "return_pct": round(strategy_pnl / initial_cash * 100, 2) if initial_cash > 0 else None,
        "valuation_status": valuation_status,
        "valuation_missing_codes": missing_valuation_codes,
        "benchmark_return_pct": round(benchmark * 100, 2) if benchmark is not None else None,
        "excess_return_pct": round(((nav / initial_cash - 1) - benchmark) * 100, 2) if benchmark is not None and initial_cash > 0 else None,
        "max_drawdown_pct": round(dd * 100, 2), "trade_count": buy_count + len(sells),
        "risk_blocks": rejected,
        "position_value": round(position_value, 2),
        "position_cost_value": round(position_cost_value, 2),
        "holding_return_pct": round(holding_return_pct, 2) if holding_return_pct is not None else None,
        "fund_utilization_pct": round(position_value / max(nav, 1) * 100, 1),
        "deployment_limit_pct": round(profile["max_exposure"] * 100, 1),
        "deployment_remaining": round(max(nav * profile["max_exposure"] - position_value, 0.0), 2),
        "realized_pnl": round(realized, 2), "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(strategy_pnl, 2), "today_pnl": round(today_pnl, 2) if today_pnl is not None else None,
        "today_return_pct": round(today_return_pct, 2) if today_return_pct is not None else None,
        "today_pnl_available": today_pnl is not None,
        "today_pnl_status": (
            "已清仓，含当日已实现盈亏"
            if today_pnl is not None and not positions and today_sell["total"] > 0
            else market_session["label"] if today_pnl is None else ""
        ),
        "market_session": market_session["code"],
        "today_baseline_nav": round(today_baseline_nav, 2) if today_baseline_nav is not None else None,
        "today_baseline_date": today if today_pnl is not None else (previous_nav_row["nav_date"] if previous_nav_row else None),
        "today_open_position_pnl": round(today_position_pnl, 2) if today_has_quote else None,
        "today_sell_pnl": today_sell_pnl if today_sell["covered"] == today_sell["total"] else None,
        "today_sell_quote_coverage": f"{today_sell['covered']}/{today_sell['total']}",
        "today_sell_missing_codes": today_sell["missing_codes"],
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "profit_loss_ratio": round(gain / loss, 2) if loss else (None if not gain else None),
        "daily_loss_pct": round(daily_loss_pct, 2),
    }
