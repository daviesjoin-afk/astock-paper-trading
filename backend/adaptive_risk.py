# -*- coding: utf-8 -*-
"""Constrained risk-parameter evolution for the paper-trading accounts.

The module only changes a versioned ``adaptive_risk`` overlay.  Automatic
promotion is limited to conservative changes after evidence gates pass;
loosening any limit always requires an explicit human approval.  It never
creates orders and never connects to a broker.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import statistics
from collections import Counter
from adaptive_common import _loads, _json, _clamp  # C3: 收敛重复工具函数
from strategy_registry import labels as strategy_labels

ACCOUNT_NAMES = {
    account_id: strategy_labels().get(account_id, account_id)
    for account_id in ("tq_breakout", "trend_pullback", "sector_rotation", "main_force_top10")
}

BASE_RISK = {
    "tq_breakout": {
        "max_weight": 0.15, "max_exposure": 0.55, "max_industry": 0.30,
        "single_risk": 0.004, "daily_loss": 0.020, "drawdown": 0.070,
        "cooldown_days": 2, "min_cost_edge": 0.006,
    },
    "trend_pullback": {
        "max_weight": 0.20, "max_exposure": 0.75, "max_industry": 0.35,
        "single_risk": 0.006, "daily_loss": 0.025, "drawdown": 0.100,
        "cooldown_days": 3, "min_cost_edge": 0.004,
    },
    "sector_rotation": {
        "max_weight": 0.18, "max_exposure": 0.65, "max_industry": 0.45,
        "single_risk": 0.005, "daily_loss": 0.025, "drawdown": 0.085,
        "cooldown_days": 2, "min_cost_edge": 0.005,
    },
    # Active in the current two-strategy cycle.  Keep this overlay inside the
    # adaptive bounds; the execution profile remains the stricter authority.
    "main_force_top10": {
        "max_weight": 0.22, "max_exposure": 0.78, "max_industry": 0.45,
        "single_risk": 0.006, "daily_loss": 0.025, "drawdown": 0.100,
        "cooldown_days": 2, "min_cost_edge": 0.006,
    },
}

# The intraday guard is a separate risk family from the cost-based stop.  Keep
# its defaults in the evolution ledger so a learned version can be replayed
# against the same three-stage warning/partial/full policy that the paper
# executor used.  These values are deliberately conservative; a loosening
# proposal can never be auto-applied.
DOWNSIDE_BASE = {
    "tq_breakout": {
        "downside_warning_pct": -2.0, "downside_partial_pct": -3.0,
        "downside_full_pct": -5.0, "downside_relative_pct": -2.5,
        "downside_peak_retrace_pct": 3.5, "downside_partial_ratio": 0.35,
    },
    "trend_pullback": {
        "downside_warning_pct": -2.5, "downside_partial_pct": -3.5,
        "downside_full_pct": -5.5, "downside_relative_pct": -3.0,
        "downside_peak_retrace_pct": 4.5, "downside_partial_ratio": 0.30,
    },
    "sector_rotation": {
        "downside_warning_pct": -2.5, "downside_partial_pct": -3.5,
        "downside_full_pct": -5.5, "downside_relative_pct": -3.0,
        "downside_peak_retrace_pct": 4.0, "downside_partial_ratio": 0.30,
    },
    "main_force_top10": {
        "downside_warning_pct": -2.5, "downside_partial_pct": -3.5,
        "downside_full_pct": -5.0, "downside_relative_pct": -3.0,
        "downside_peak_retrace_pct": 4.5, "downside_partial_ratio": 0.30,
    },
}

DOWNSIDE_BOUNDS = {
    "downside_warning_pct": (-4.0, -1.0),
    "downside_partial_pct": (-7.0, -2.0),
    "downside_full_pct": (-10.0, -3.0),
    "downside_relative_pct": (-6.0, -1.0),
    "downside_peak_retrace_pct": (2.0, 8.0),
    "downside_partial_ratio": (0.20, 0.50),
}

# For negative percentage thresholds, a larger value (less negative) fires
# earlier and is tighter.  A lower retrace threshold and a larger partial
# ratio are also tighter.  This direction map keeps change classification and
# rollback semantics correct for the new policy family.
DOWNSIDE_TIGHTER_HIGHER = {
    "downside_warning_pct", "downside_partial_pct", "downside_full_pct",
    "downside_relative_pct", "downside_partial_ratio",
}
DOWNSIDE_TIGHTER_LOWER = {"downside_peak_retrace_pct"}

BOUNDS = {
    "max_weight": (0.08, 0.22),
    "max_exposure": (0.35, 0.78),
    "max_industry": (0.18, 0.45),
    "single_risk": (0.0020, 0.0060),
    "daily_loss": (0.010, 0.025),
    "drawdown": (0.040, 0.100),
    "cooldown_days": (2, 5),
    "min_cost_edge": (0.004, 0.012),
}

LOWER_IS_TIGHTER = {
    "max_weight", "max_exposure", "max_industry", "single_risk", "daily_loss", "drawdown"
}
HIGHER_IS_TIGHTER = {"cooldown_days", "min_cost_edge"}

REGIME_SCALE = {
    "risk_off": 0.80,
    "high_volatility": 0.86,
    "rotation": 0.93,
    "balanced": 0.95,
    "momentum": 1.00,
    "unclassified": 0.92,
}


def _num(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _next_weekday(value):
    day = dt.date.fromisoformat(str(value)[:10]) + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day.isoformat()


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adaptive_risk_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            account_id TEXT NOT NULL,
            regime TEXT NOT NULL,
            baseline_params TEXT NOT NULL,
            candidate_params TEXT NOT NULL,
            evidence TEXT NOT NULL,
            risk_reduction_pct REAL NOT NULL DEFAULT 0,
            change_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            application_mode TEXT NOT NULL,
            reason TEXT NOT NULL,
            previous_account_params TEXT,
            effective_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            applied_at TEXT,
            UNIQUE(run_date,account_id,regime)
        );
        CREATE TABLE IF NOT EXISTS adaptive_risk_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            account_id TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adaptive_order_risk_attribution(
            order_id INTEGER PRIMARY KEY,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            side TEXT NOT NULL,
            order_status TEXT NOT NULL,
            order_date TEXT NOT NULL,
            risk_version TEXT NOT NULL,
            candidate_id INTEGER,
            decision_id INTEGER,
            decision_linked INTEGER NOT NULL DEFAULT 0,
            payload_complete INTEGER NOT NULL DEFAULT 0,
            execution_integrity INTEGER NOT NULL DEFAULT 1,
            realized_pnl REAL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_order_risk_version
            ON adaptive_order_risk_attribution(account_id,risk_version,order_date);
        CREATE TABLE IF NOT EXISTS adaptive_risk_daily_outcomes(
            account_id TEXT NOT NULL,
            outcome_date TEXT NOT NULL,
            risk_version TEXT NOT NULL,
            candidate_id INTEGER,
            nav REAL NOT NULL,
            daily_return_pct REAL,
            drawdown_pct REAL NOT NULL,
            orders INTEGER NOT NULL DEFAULT 0,
            filled_orders INTEGER NOT NULL DEFAULT 0,
            rejected_orders INTEGER NOT NULL DEFAULT 0,
            deferred_orders INTEGER NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            stop_exits INTEGER NOT NULL DEFAULT 0,
            decision_link_pct REAL NOT NULL DEFAULT 0,
            execution_integrity_pct REAL NOT NULL DEFAULT 100,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_id,outcome_date)
        );
        CREATE TABLE IF NOT EXISTS adaptive_downside_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            event_date TEXT NOT NULL,
            action TEXT NOT NULL,
            level TEXT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 0,
            main_force_intent TEXT NOT NULL DEFAULT 'uncertain',
            confidence REAL,
            intraday_pct REAL,
            relative_to_market_pct REAL,
            peak_retrace_pct REAL,
            cost_return_pct REAL,
            sell_ratio REAL NOT NULL DEFAULT 0,
            order_status TEXT,
            realized_pnl REAL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id,code,event_date,action)
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_downside_events_account_date
            ON adaptive_downside_events(account_id,event_date,action);
        CREATE TABLE IF NOT EXISTS adaptive_risk_deployments(
            candidate_id INTEGER PRIMARY KEY,
            account_id TEXT NOT NULL,
            risk_version TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            status TEXT NOT NULL,
            baseline TEXT NOT NULL,
            observation_days INTEGER NOT NULL DEFAULT 0,
            post_metrics TEXT NOT NULL DEFAULT '{}',
            decision TEXT,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS adaptive_risk_outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            operation TEXT NOT NULL DEFAULT 'apply',
            version TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            applied_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_adaptive_risk_outbox_pending
            ON adaptive_risk_outbox(status, updated_at);
        """
    )


def _paper_connection(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _current_risk(account):
    account_id = account["id"]
    current = dict(BASE_RISK[account_id])
    current.update(DOWNSIDE_BASE.get(account_id, {}))
    account_params = _loads(account.get("params"), {})
    overlay = account_params.get("adaptive_risk") or {}
    meta = account_params.get("adaptive_risk_meta") or {}
    if meta.get("status") == "active":
        for key, bounds in BOUNDS.items():
            if key not in overlay:
                continue
            value = _clamp(_num(overlay[key], current[key]), *bounds)
            current[key] = int(round(value)) if key == "cooldown_days" else round(value, 6)
        for key, bounds in DOWNSIDE_BOUNDS.items():
            if key not in overlay:
                continue
            current[key] = round(_clamp(_num(overlay[key], current[key]), *bounds), 6)
    return current, account_params, meta


def _nav_metrics(paper, account_id):
    rows = paper.execute(
        "SELECT nav_date,nav FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account_id,)
    ).fetchall()
    values = [_num(row["nav"]) for row in rows if _num(row["nav"]) > 0]
    returns = [(values[index] / values[index - 1] - 1) for index in range(1, len(values))]
    peak = values[0] if values else 0.0
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            max_drawdown = min(max_drawdown, value / peak - 1)
    downside = [value for value in returns if value < 0]
    return {
        "nav_days": len(values),
        "daily_observations": len(returns),
        "realized_max_drawdown_pct": round(abs(max_drawdown) * 100, 3),
        "downside_day_ratio_pct": round(100 * len(downside) / len(returns), 1) if returns else None,
        "mean_daily_return_pct": round(statistics.mean(returns) * 100, 4) if returns else None,
        "daily_tail_loss_pct": round(abs(min(returns)) * 100, 3) if returns else None,
    }


def _sync_downside_events(adaptive, paper, now):
    """Import staged downside reviews into the evolution ledger.

    The paper executor owns detection and execution gates.  Evolution only
    consumes the resulting, versioned reviews/orders, so an AI suggestion can
    never manufacture a risk event or bypass a quote/T+1 check.
    """
    reviews = paper.execute(
        """SELECT account_id,code,review_date,action,detail,created_at
             FROM paper_position_reviews
            WHERE action IN ('downside_warning','downside_pending_quote',
                             'downside_partial','downside_full')"""
    ).fetchall()
    synced = 0
    for review in reviews:
        detail = _loads(review["detail"], {}) or {}
        guard = detail.get("downside_guard") or {}
        if not guard:
            continue
        intent = guard.get("main_force_intent") or {}
        level = str(guard.get("level") or "warning")
        action = str(review["action"] or "downside_warning")
        order_rows = paper.execute(
            """SELECT status,realized_pnl,risk_payload FROM paper_orders
                WHERE account_id=? AND code=? AND substr(created_at,1,10)=?
                ORDER BY id DESC""",
            (review["account_id"], review["code"], str(review["review_date"])[:10]),
        ).fetchall()
        order_status = None
        realized_pnl = None
        for order in order_rows:
            order_detail = _loads(order["risk_payload"], {}) or {}
            if order_detail.get("downside_guard"):
                order_status = order["status"]
                realized_pnl = _num(order["realized_pnl"], None)
                break
        payload = {
            "guard": guard,
            "review_created_at": review["created_at"],
            "order_status": order_status,
            "realized_pnl": realized_pnl,
        }
        adaptive.execute(
            """INSERT INTO adaptive_downside_events(
                   account_id,code,event_date,action,level,confirmed,
                   main_force_intent,confidence,intraday_pct,relative_to_market_pct,
                   peak_retrace_pct,cost_return_pct,sell_ratio,order_status,realized_pnl,
                   detail,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id,code,event_date,action) DO UPDATE SET
                   level=excluded.level,confirmed=excluded.confirmed,
                   main_force_intent=excluded.main_force_intent,confidence=excluded.confidence,
                   intraday_pct=excluded.intraday_pct,relative_to_market_pct=excluded.relative_to_market_pct,
                   peak_retrace_pct=excluded.peak_retrace_pct,cost_return_pct=excluded.cost_return_pct,
                   sell_ratio=excluded.sell_ratio,order_status=excluded.order_status,
                   realized_pnl=excluded.realized_pnl,detail=excluded.detail,updated_at=excluded.updated_at""",
            (review["account_id"], review["code"], str(review["review_date"])[:10], action,
             level, int(bool(guard.get("confirmed"))),
             str(intent.get("classification") or "uncertain"), _num(intent.get("confidence"), None),
             _num(guard.get("intraday_pct"), None), _num(guard.get("relative_to_market_pct"), None),
             _num(guard.get("peak_retrace_pct"), None), _num(guard.get("cost_return_pct"), None),
             _num(guard.get("sell_ratio"), 0.0), order_status, realized_pnl,
             _json(payload), str(review["created_at"] or now), now),
        )
        synced += 1
    return synced


def _evidence(adaptive, paper, account_id):
    nav = _nav_metrics(paper, account_id)
    reward_rows = adaptive.execute(
        "SELECT regime,raw_reward,excess_return_pct,drawdown_pct FROM adaptive_rewards WHERE account_id=?",
        (account_id,),
    ).fetchall()
    closed_trades = paper.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND side='sell' AND status='filled'",
        (account_id,),
    ).fetchone()[0]
    filled_orders = paper.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status='filled'",
        (account_id,),
    ).fetchone()[0]
    regimes = sorted({row["regime"] for row in reward_rows if row["regime"] != "unclassified"})
    rewards = [_num(row["raw_reward"]) for row in reward_rows]
    excess = [_num(row["excess_return_pct"]) for row in reward_rows]
    drawdowns = [abs(min(0.0, _num(row["drawdown_pct"]))) for row in reward_rows]
    attribution = adaptive.execute(
        """SELECT COUNT(*) total,SUM(decision_linked) linked,SUM(payload_complete) complete,
                  SUM(execution_integrity) integrity
             FROM adaptive_order_risk_attribution WHERE account_id=?""", (account_id,)
    ).fetchone()
    daily_rows = adaptive.execute(
        "SELECT * FROM adaptive_risk_daily_outcomes WHERE account_id=? ORDER BY outcome_date",
        (account_id,),
    ).fetchall()
    downside_rows = adaptive.execute(
        "SELECT * FROM adaptive_downside_events WHERE account_id=? ORDER BY event_date,id",
        (account_id,),
    ).fetchall()
    downside_levels = Counter(str(row["level"] or "warning") for row in downside_rows)
    downside_intents = Counter(str(row["main_force_intent"] or "uncertain") for row in downside_rows)
    confirmed_rows = [row for row in downside_rows if int(row["confirmed"] or 0)]
    filled_downside = [row for row in downside_rows if row["order_status"] == "filled"]
    downside_evidence = {
        "events": len(downside_rows),
        "confirmed_events": len(confirmed_rows),
        "warning_events": downside_levels.get("warning", 0),
        "partial_events": downside_levels.get("partial", 0),
        "full_events": downside_levels.get("full", 0),
        "filled_exits": len(filled_downside),
        "distribution_events": downside_intents.get("distribution", 0),
        "washout_events": downside_intents.get("washout", 0),
        "uncertain_events": downside_intents.get("uncertain", 0),
        "confirmed_distribution_rate_pct": round(
            100 * sum(row["main_force_intent"] == "distribution" for row in confirmed_rows)
            / max(len(confirmed_rows), 1), 1,
        ),
        "realized_pnl_after_guard": round(
            sum(_num(row["realized_pnl"], 0.0) for row in filled_downside), 2,
        ),
    }
    attributed_orders = int(attribution["total"] or 0) if attribution else 0
    return {
        **nav,
        "closed_trades": int(closed_trades),
        "filled_orders": int(filled_orders),
        "trade_events": max(int(closed_trades), int(filled_orders) // 2),
        "reward_samples": len(reward_rows),
        "regime_count": len(regimes),
        "regimes": regimes,
        "mean_reward": round(statistics.mean(rewards), 4) if rewards else None,
        "mean_excess_pct": round(statistics.mean(excess), 4) if excess else None,
        "reward_window_drawdown_pct": round(max(drawdowns), 3) if drawdowns else None,
        "attributed_orders": attributed_orders,
        "decision_link_pct": round(100 * _num(attribution["linked"]) / max(attributed_orders, 1), 1) if attribution else 0.0,
        "payload_complete_pct": round(100 * _num(attribution["complete"]) / max(attributed_orders, 1), 1) if attribution else 0.0,
        "execution_integrity_pct": round(100 * _num(attribution["integrity"]) / max(attributed_orders, 1), 1) if attribution else 0.0,
        "risk_outcome_days": len(daily_rows),
        "risk_rejected_orders": sum(int(row["rejected_orders"] or 0) for row in daily_rows),
        "risk_deferred_orders": sum(int(row["deferred_orders"] or 0) for row in daily_rows),
        "stop_exits": sum(int(row["stop_exits"] or 0) for row in daily_rows),
        "downside_guard": downside_evidence,
    }


def _version_context(paper, account_id, order_at):
    """Return the risk version that was active when this exact order was created.

    ``effective_date`` is retained for compact reporting, but it is not
    precise enough for same-day recalibration.  Without the timestamp a
    14:00 parameter change was incorrectly attributed to an order from 09:35.
    """
    order_at = str(order_at or "").replace("T", " ")
    order_date = order_at[:10]
    rows = paper.execute(
        """SELECT version,params,effective_date FROM paper_parameter_versions
             WHERE account_id=? AND effective_date<=? ORDER BY effective_date DESC,id DESC""",
        (account_id, order_date),
    ).fetchall()
    for row in rows:
        params = _loads(row["params"], {})
        meta = params.get("adaptive_risk_meta") or {}
        effective = str(meta.get("effective_date") or row["effective_date"] or "")[:10]
        effective_at = str(meta.get("effective_at") or f"{effective} 00:00:00").replace("T", " ")
        if meta.get("status") == "active" and effective <= order_date and effective_at <= order_at:
            return str(meta.get("version") or row["version"]), meta.get("candidate_id"), meta
    return f"base-{account_id}", None, {"status": "base"}


def _sync_order_attribution(adaptive, paper, now):
    order_columns = {str(row["name"]) for row in paper.execute("PRAGMA table_info(paper_orders)")}
    order_type_expr = "order_type" if "order_type" in order_columns else "'market' AS order_type"
    origin_expr = "origin" if "origin" in order_columns else "'strategy' AS origin"
    # Only the validity bit is needed here. Materialising every full decision
    # snapshot made the learning worker grow past its cgroup memory limit.
    orders = paper.execute(
        f"""SELECT id,account_id,code,side,status,created_at,filled_price,qty,
                   amount,executed_at,reason,realized_pnl,{order_type_expr},{origin_expr},
                   CASE WHEN json_valid(risk_payload)
                              AND json_type(risk_payload)='object'
                              AND length(trim(risk_payload))>2
                        THEN 1 ELSE 0 END AS payload_complete
              FROM paper_orders ORDER BY id"""
    )
    synced = 0
    for order in orders:
        order_date = str(order["created_at"] or "")[:10]
        version, candidate_id, meta = _version_context(paper, order["account_id"], order["created_at"])
        decision = paper.execute(
            """SELECT id,decision,created_at FROM paper_risk_decisions
                 WHERE account_id=? AND COALESCE(code,'')=COALESCE(?, '') AND side=?
                   AND ABS(strftime('%s',created_at)-strftime('%s',?))<=600
                 ORDER BY ABS(strftime('%s',created_at)-strftime('%s',?)),id DESC LIMIT 1""",
            (order["account_id"], order["code"], order["side"], order["created_at"], order["created_at"]),
        ).fetchone()
        payload_complete = int(order["payload_complete"] or 0)
        filled = order["status"] == "filled"
        integrity = int(
            (filled and _num(order["filled_price"]) > 0 and int(order["qty"] or 0) > 0 and _num(order["amount"]) > 0)
            or (not filled and order["filled_price"] is None and order["executed_at"] is None)
        )
        detail = {
            "decision": dict(decision) if decision else None,
            "risk_meta": meta,
            "order_reason": order["reason"],
            "order_type": order["order_type"],
            "origin": order["origin"],
        }
        adaptive.execute(
            """INSERT INTO adaptive_order_risk_attribution(
                   order_id,account_id,code,side,order_status,order_date,risk_version,candidate_id,
                   decision_id,decision_linked,payload_complete,execution_integrity,realized_pnl,
                   detail,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                   order_status=excluded.order_status,risk_version=excluded.risk_version,
                   candidate_id=excluded.candidate_id,decision_id=excluded.decision_id,
                   decision_linked=excluded.decision_linked,payload_complete=excluded.payload_complete,
                   execution_integrity=excluded.execution_integrity,realized_pnl=excluded.realized_pnl,
                   detail=excluded.detail,updated_at=excluded.updated_at""",
            (order["id"], order["account_id"], order["code"], order["side"], order["status"],
             order_date, version, candidate_id, decision["id"] if decision else None, int(bool(decision)),
             payload_complete, integrity, order["realized_pnl"], _json(detail), now, now),
        )
        synced += 1
    return synced


def _capture_daily_outcomes(adaptive, paper, now):
    captured = 0
    for account in paper.execute("SELECT id,initial_cash FROM paper_accounts ORDER BY id"):
        nav_rows = paper.execute(
            "SELECT nav_date,nav FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account["id"],)
        ).fetchall()
        peak = _num(account["initial_cash"])
        previous = None
        for nav_row in nav_rows:
            day, nav = str(nav_row["nav_date"]), _num(nav_row["nav"])
            peak = max(peak, nav)
            daily_return = (nav / previous - 1) * 100 if previous else None
            drawdown = (1 - nav / peak) * 100 if peak else 0.0
            # P3 审计修复（E1）：daily 行归属传当日结束时间——旧实现传裸日期，
            # 字符串比较 "T+0 00:00:00" <= "T+0" 为 False，导致当日生效版本的
            # 当日数据被错误归属到旧版本（订单级归因正确，两层口径矛盾）。
            version, candidate_id, _ = _version_context(paper, account["id"], f"{day} 23:59:59")
            order_rows = paper.execute(
                "SELECT status,side,reason,COALESCE(realized_pnl,0) pnl FROM paper_orders WHERE account_id=? AND substr(created_at,1,10)=?",
                (account["id"], day),
            ).fetchall()
            attribution = adaptive.execute(
                """SELECT COUNT(*) total,SUM(decision_linked) linked,SUM(execution_integrity) integrity
                     FROM adaptive_order_risk_attribution WHERE account_id=? AND order_date=?""",
                (account["id"], day),
            ).fetchone()
            total = int(attribution["total"] or 0) if attribution else 0
            detail = {"order_statuses": dict((status, sum(1 for row in order_rows if row["status"] == status)) for status in {row["status"] for row in order_rows})}
            adaptive.execute(
                """INSERT INTO adaptive_risk_daily_outcomes(
                       account_id,outcome_date,risk_version,candidate_id,nav,daily_return_pct,drawdown_pct,
                       orders,filled_orders,rejected_orders,deferred_orders,realized_pnl,stop_exits,
                       decision_link_pct,execution_integrity_pct,detail,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id,outcome_date) DO UPDATE SET
                       risk_version=excluded.risk_version,candidate_id=excluded.candidate_id,nav=excluded.nav,
                       daily_return_pct=excluded.daily_return_pct,drawdown_pct=excluded.drawdown_pct,
                       orders=excluded.orders,filled_orders=excluded.filled_orders,
                       rejected_orders=excluded.rejected_orders,deferred_orders=excluded.deferred_orders,
                       realized_pnl=excluded.realized_pnl,stop_exits=excluded.stop_exits,
                       decision_link_pct=excluded.decision_link_pct,
                       execution_integrity_pct=excluded.execution_integrity_pct,
                       detail=excluded.detail,updated_at=excluded.updated_at""",
                (account["id"], day, version, candidate_id, nav, daily_return, drawdown, len(order_rows),
                 sum(row["status"] == "filled" for row in order_rows),
                 sum(row["status"] == "risk_rejected" for row in order_rows),
                 sum(row["status"] == "deferred_capacity" for row in order_rows),
                 sum(_num(row["pnl"]) for row in order_rows),
                 sum(row["side"] == "sell" and "止损" in str(row["reason"] or "") for row in order_rows),
                 round(100 * _num(attribution["linked"]) / max(total, 1), 1) if attribution else 0.0,
                 round(100 * _num(attribution["integrity"]) / max(total, 1), 1) if attribution else 100.0,
                 _json(detail), now, now),
            )
            previous = nav
            captured += 1
    return captured


def _window_metrics(rows):
    rows = list(rows)
    returns = [_num(row["daily_return_pct"]) for row in rows if row["daily_return_pct"] is not None]
    return {
        "days": len(rows),
        "max_drawdown_pct": round(max([_num(row["drawdown_pct"]) for row in rows] or [0]), 3),
        "tail_daily_loss_pct": round(abs(min(returns)), 3) if returns else 0.0,
        "mean_daily_return_pct": round(statistics.mean(returns), 4) if returns else None,
        "orders": sum(int(row["orders"] or 0) for row in rows),
        "filled_orders": sum(int(row["filled_orders"] or 0) for row in rows),
        "rejected_orders": sum(int(row["rejected_orders"] or 0) for row in rows),
        "deferred_orders": sum(int(row["deferred_orders"] or 0) for row in rows),
        "realized_pnl": round(sum(_num(row["realized_pnl"]) for row in rows), 2),
        "stop_exits": sum(int(row["stop_exits"] or 0) for row in rows),
        "decision_link_pct": round(statistics.mean([_num(row["decision_link_pct"]) for row in rows]), 1) if rows else 0.0,
        "execution_integrity_pct": round(statistics.mean([_num(row["execution_integrity_pct"], 100) for row in rows]), 1) if rows else 100.0,
    }


def _deployment_baseline(conn, account_id, effective_date):
    rows = conn.execute(
        """SELECT * FROM adaptive_risk_daily_outcomes
             WHERE account_id=? AND outcome_date<? ORDER BY outcome_date DESC LIMIT 5""",
        (account_id, effective_date),
    ).fetchall()
    return _window_metrics(reversed(rows))


def _register_deployment(conn, candidate, version, effective_date, now):
    baseline = _deployment_baseline(conn, candidate["account_id"], effective_date)
    conn.execute(
        """UPDATE adaptive_risk_deployments SET status='superseded',decision='superseded',
                  reason='后续风控版本已生效',updated_at=?
             WHERE account_id=? AND candidate_id<>? AND status IN ('observing','review_required')""",
        (now, candidate["account_id"], candidate["id"]),
    )
    conn.execute(
        """INSERT INTO adaptive_risk_deployments(
               candidate_id,account_id,risk_version,effective_date,status,baseline,
               observation_days,post_metrics,decision,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(candidate_id) DO UPDATE SET
               risk_version=excluded.risk_version,effective_date=excluded.effective_date,
               status='observing',baseline=excluded.baseline,updated_at=excluded.updated_at""",
        (candidate["id"], candidate["account_id"], version, effective_date, "observing",
         _json(baseline), 0, _json({}), "observe", "等待至少5个模拟盘净值日完成部署后验证", now, now),
    )


def _monitor_deployments(conn, paper_db_path, now_fn):
    deployments = conn.execute(
        "SELECT * FROM adaptive_risk_deployments WHERE status IN ('observing','review_required') ORDER BY candidate_id"
    ).fetchall()
    if not deployments:
        return {"observing": 0, "validated": 0, "review_required": 0, "auto_rolled_back": []}
    paper = _paper_connection(paper_db_path)
    actions = []
    try:
        for deployment in deployments:
            account = paper.execute("SELECT params FROM paper_accounts WHERE id=?", (deployment["account_id"],)).fetchone()
            params = _loads(account["params"], {}) if account else {}
            meta = params.get("adaptive_risk_meta") or {}
            post_rows = conn.execute(
                """SELECT * FROM adaptive_risk_daily_outcomes
                     WHERE account_id=? AND outcome_date>=? ORDER BY outcome_date""",
                (deployment["account_id"], deployment["effective_date"]),
            ).fetchall()
            post = _window_metrics(post_rows)
            attr = conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN candidate_id=? THEN 1 ELSE 0 END) wired,
                          SUM(decision_linked) linked,SUM(execution_integrity) integrity
                     FROM adaptive_order_risk_attribution
                    WHERE account_id=? AND order_date>=?""",
                (deployment["candidate_id"], deployment["account_id"], deployment["effective_date"]),
            ).fetchone()
            total = int(attr["total"] or 0)
            post["version_wiring_pct"] = round(100 * _num(attr["wired"]) / max(total, 1), 1)
            post["order_decision_link_pct"] = round(100 * _num(attr["linked"]) / max(total, 1), 1)
            post["order_integrity_pct"] = round(100 * _num(attr["integrity"]) / max(total, 1), 1)
            post["attributed_orders"] = total
            status, decision, reason = "observing", "observe", "等待至少5个模拟盘净值日完成部署后验证"
            active_candidate = str(meta.get("candidate_id") or "") == str(deployment["candidate_id"])
            technical_failure = None
            if not account or not active_candidate:
                technical_failure = "账户当前生效版本与部署候选不一致"
            elif total >= 3 and post["version_wiring_pct"] < 99.9:
                technical_failure = f"订单风控版本接线覆盖仅 {post['version_wiring_pct']:.1f}%"
            elif total >= 3 and post["order_integrity_pct"] < 100:
                technical_failure = f"订单执行完整性仅 {post['order_integrity_pct']:.1f}%"
            baseline = _loads(deployment["baseline"], {})
            performance_warning = (
                post["days"] >= 5
                and (
                    post["max_drawdown_pct"] > _num(baseline.get("max_drawdown_pct")) + 2.0
                    or post["tail_daily_loss_pct"] > _num(baseline.get("tail_daily_loss_pct")) + 1.0
                )
            )
            if technical_failure:
                status, decision, reason = "rollback_required", "auto_rollback", technical_failure
                actions.append((deployment["account_id"], deployment["candidate_id"], technical_failure))
            elif performance_warning:
                status, decision = "review_required", "human_review"
                reason = "观察期风险结果较部署前明显恶化；避免把市场波动误判为参数因果，暂停后续进化并等待人工复核"
            elif post["days"] >= 5:
                status, decision, reason = "validated", "keep", "5个净值日观察完成，接线完整且未触发效果恶化门禁"
            elif deployment["status"] == "review_required":
                # P3 审计修复：观察日数不足时不得把人工复核中的部署降级
                # 回 observing——那会抹掉 review_required 的人工复核标记。
                status, decision = "review_required", "human_review"
                reason = "人工复核中；观察日数不足5日，维持复核状态不降级"
            conn.execute(
                """UPDATE adaptive_risk_deployments SET status=?,observation_days=?,post_metrics=?,
                       decision=?,reason=?,updated_at=?,reviewed_at=? WHERE candidate_id=?""",
                (status, post["days"], _json(post), decision, reason, now_fn(),
                 now_fn() if status in {"validated", "review_required", "rollback_required"} else None,
                 deployment["candidate_id"]),
            )
    finally:
        paper.close()
    rolled_back = []
    for account_id, candidate_id, reason in actions:
        try:
            rollback(conn, paper_db_path, account_id, now_fn, f"技术闭环自动回滚：{reason}")
            conn.execute(
                "UPDATE adaptive_risk_deployments SET status='rolled_back',decision='auto_rollback',updated_at=? WHERE candidate_id=?",
                (now_fn(), candidate_id),
            )
            rolled_back.append(candidate_id)
        except ValueError:
            pass
    states = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status,COUNT(*) n FROM adaptive_risk_deployments GROUP BY status"
    )}
    return {"observing": states.get("observing", 0), "validated": states.get("validated", 0),
            "review_required": states.get("review_required", 0), "auto_rolled_back": rolled_back}


def _propose(account_id, current, regime, evidence, evolution_tier="waiting"):
    scale = REGIME_SCALE.get(regime, REGIME_SCALE["unclassified"])
    if account_id == "sector_rotation" and regime == "rotation":
        scale = max(scale, 0.98)
    if account_id == "trend_pullback" and regime == "high_volatility":
        scale = min(scale, 0.83)
    mean_reward = evidence.get("mean_reward")
    if mean_reward is not None and mean_reward < 0:
        scale -= 0.05
    if _num(evidence.get("realized_max_drawdown_pct")) >= 5:
        scale -= 0.05
    # 3 日快速影子不落账户；5 日起才允许明显但仍受边界约束的保守收紧。
    minimum_scale = {"waiting": 0.96, "fast_shadow": 0.92, "micro": 0.88, "standard": 0.80, "mature": 0.70}.get(evolution_tier, 0.96)
    scale = _clamp(scale, minimum_scale, 1.00)
    # Start from the currently active version so an already tightened policy
    # is not silently reset to the static baseline on the next close.
    base = current
    proposed = {}
    for key in LOWER_IS_TIGHTER:
        value = base[key] * scale
        proposed[key] = round(_clamp(value, *BOUNDS[key]), 6)
    proposed["cooldown_days"] = int(round(_clamp(
        base["cooldown_days"] + (1 - scale) * 5, *BOUNDS["cooldown_days"]
    )))
    proposed["min_cost_edge"] = round(_clamp(
        base["min_cost_edge"] + (1 - scale) * 0.02, *BOUNDS["min_cost_edge"]
    ), 6)
    # New staged downside policy: only tighten automatically after at least
    # three observed events with a majority of confirmed distribution signals
    # and a negative reward window.  Loosening is intentionally not proposed
    # here; it remains a human-review decision after false-positive analysis.
    guard = evidence.get("downside_guard") or {}
    guard_events = int(guard.get("events") or 0)
    distribution_rate = _num(guard.get("confirmed_distribution_rate_pct"), 0.0)
    if guard_events >= 3 and distribution_rate >= 60 and _num(evidence.get("mean_reward"), 0.0) < 0:
        step = 0.25 if evolution_tier in {"standard", "mature"} else 0.15
        for key in ("downside_warning_pct", "downside_partial_pct", "downside_full_pct", "downside_relative_pct"):
            proposed[key] = round(_clamp(base[key] + step, *DOWNSIDE_BOUNDS[key]), 6)
        proposed["downside_peak_retrace_pct"] = round(_clamp(
            base["downside_peak_retrace_pct"] - step, *DOWNSIDE_BOUNDS["downside_peak_retrace_pct"]
        ), 6)
        proposed["downside_partial_ratio"] = round(_clamp(
            base["downside_partial_ratio"] + (0.02 if evolution_tier in {"standard", "mature"} else 0.01),
            *DOWNSIDE_BOUNDS["downside_partial_ratio"],
        ), 6)
    else:
        for key in DOWNSIDE_BOUNDS:
            proposed[key] = round(_clamp(base[key], *DOWNSIDE_BOUNDS[key]), 6)
    return proposed, round(scale, 3)


def _change_kind(current, candidate):
    changed = False
    loosening = False
    for key, value in candidate.items():
        old = current[key]
        if abs(_num(value) - _num(old)) <= 1e-9:
            continue
        changed = True
        if key in LOWER_IS_TIGHTER and value > old:
            loosening = True
        if key in HIGHER_IS_TIGHTER and value < old:
            loosening = True
        if key in DOWNSIDE_TIGHTER_HIGHER and value < old:
            loosening = True
        if key in DOWNSIDE_TIGHTER_LOWER and value > old:
            loosening = True
    if not changed:
        return "no_change"
    return "includes_loosening" if loosening else "conservative_tighten"


def _risk_reduction(current, candidate):
    changes = []
    for key in LOWER_IS_TIGHTER:
        if current[key] > 0:
            changes.append(max(0.0, 1 - candidate[key] / current[key]))
    for key in HIGHER_IS_TIGHTER:
        if candidate[key] > current[key]:
            changes.append(min(1.0, (candidate[key] - current[key]) / max(current[key], 1e-6)))
        else:
            changes.append(0.0)
    for key, bounds in DOWNSIDE_BOUNDS.items():
        span = max(bounds[1] - bounds[0], 1e-6)
        if key in DOWNSIDE_TIGHTER_HIGHER:
            changes.append(max(0.0, (candidate[key] - current[key]) / span))
        else:
            changes.append(max(0.0, (current[key] - candidate[key]) / span))
    return round(100 * statistics.mean(changes), 2) if changes else 0.0


def _tier_requirements(config, prefix):
    return {
        "nav_days": int(config.get(f"risk_{prefix}_nav_days", {"shadow": 1, "fast": 3, "standard": 5, "mature": 10}[prefix])),
        "trade_events": int(config.get(f"risk_{prefix}_trade_events", {"shadow": 1, "fast": 2, "standard": 4, "mature": 8}[prefix])),
        "reward_samples": int(config.get(f"risk_{prefix}_reward_samples", {"shadow": 2, "fast": 4, "standard": 6, "mature": 12}[prefix])),
        "regime_count": int(config.get(f"risk_{prefix}_regimes", {"shadow": 1, "fast": 1, "standard": 2, "mature": 2}[prefix])),
    }


def _gate_result(evidence, profile, config):
    tier_checks = {}
    achieved = "waiting"
    for prefix, tier_name in (("shadow", "fast_shadow"), ("fast", "micro"), ("standard", "standard"), ("mature", "mature")):
        requirements = _tier_requirements(config, prefix)
        checks = {
            key: {"current": int(evidence.get(key) or 0), "required": required,
                  "passed": int(evidence.get(key) or 0) >= required}
            for key, required in requirements.items()
        }
        checks["data_quality"] = {
            "current": profile.get("quality"), "required": "valid_close",
            "passed": profile.get("quality") == "valid_close",
        }
        order_required = {"shadow": 6, "fast": 10, "standard": 30, "mature": 60}[prefix]
        checks["attributed_orders"] = {
            "current": int(evidence.get("attributed_orders") or 0), "required": order_required,
            "passed": int(evidence.get("attributed_orders") or 0) >= order_required,
        }
        checks["decision_link_quality"] = {
            "current": _num(evidence.get("decision_link_pct")), "required": 90.0,
            "passed": _num(evidence.get("decision_link_pct")) >= 90.0,
        }
        checks["execution_integrity"] = {
            "current": _num(evidence.get("execution_integrity_pct")), "required": 100.0,
            "passed": _num(evidence.get("execution_integrity_pct")) >= 100.0,
        }
        passed = all(item["passed"] for item in checks.values())
        tier_checks[prefix] = {"passed": passed, "checks": checks, "requirements": requirements}
        if passed:
            achieved = tier_name
    # P3 审计修复（E3）：返回实际达成层级的检查项。旧实现固定返回
    # fast 层——waiting/shadow 候选展示看似更严的门槛，mature 候选展示
    # 看似更松的门槛，人工审批的核心审计材料层级错报。
    display_tier = {"fast_shadow": "shadow", "micro": "fast", "standard": "standard",
                    "mature": "mature"}.get(achieved, "shadow")
    return tier_checks[display_tier]["checks"], achieved, tier_checks


def _upsert_candidate(conn, payload, now):
    existing = conn.execute(
        "SELECT id,status FROM adaptive_risk_candidates WHERE run_date=? AND account_id=? AND regime=?",
        (payload["run_date"], payload["account_id"], payload["regime"]),
    ).fetchone()
    if existing and existing["status"] in {"applied", "rolled_back"}:
        return existing["id"]
    if existing:
        pending = conn.execute(
            "SELECT 1 FROM adaptive_risk_outbox WHERE candidate_id=? AND status IN ('pending','error') LIMIT 1",
            (int(existing["id"]),),
        ).fetchone()
        if pending:
            # The outbox contains the exact candidate payload that must be
            # replayed.  Do not replace it with a newer evaluation while the
            # cross-database operation is still recoverable.
            return existing["id"]
    conn.execute(
        """INSERT INTO adaptive_risk_candidates(
           run_date,account_id,regime,baseline_params,candidate_params,evidence,risk_reduction_pct,
           change_kind,status,application_mode,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_date,account_id,regime) DO UPDATE SET
             baseline_params=excluded.baseline_params,candidate_params=excluded.candidate_params,
             evidence=excluded.evidence,risk_reduction_pct=excluded.risk_reduction_pct,
             change_kind=excluded.change_kind,status=excluded.status,
             application_mode=excluded.application_mode,reason=excluded.reason,updated_at=excluded.updated_at""",
        (payload["run_date"], payload["account_id"], payload["regime"], _json(payload["baseline_params"]),
         _json(payload["candidate_params"]), _json(payload["evidence"]), payload["risk_reduction_pct"],
         payload["change_kind"], payload["status"], payload["application_mode"], payload["reason"], now, now),
    )
    return conn.execute(
        "SELECT id FROM adaptive_risk_candidates WHERE run_date=? AND account_id=? AND regime=?",
        (payload["run_date"], payload["account_id"], payload["regime"]),
    ).fetchone()["id"]


def _is_conservative(current, candidate):
    return _change_kind(current, candidate) in {"no_change", "conservative_tighten"}


def _queue_outbox(conn, item, version, effective_date, approved_by, now,
                  operation="apply", outbox_candidate_id=None, reason=None,
                  previous_account_params=None, require_conservative=True):
    """Persist the intent before writing the separate paper database.

    ``candidate_id`` is positive for apply and negative for rollback so one
    candidate can have at most one durable intent for each operation.  The
    conflict clause deliberately leaves the original payload untouched: a
    retry must replay the exact parameters that were approved, not whatever a
    later evaluation happens to produce.
    """
    payload = {
        "candidate_id": int(item["id"]),
        "effective_date": str(effective_date)[:10],
        "approved_by": approved_by,
        "reason": reason,
        "previous_account_params": previous_account_params,
        "require_conservative": bool(require_conservative),
    }
    outbox_id = int(outbox_candidate_id if outbox_candidate_id is not None else item["id"])
    conn.execute(
        """INSERT INTO adaptive_risk_outbox(
           candidate_id,account_id,operation,version,payload,status,attempts,created_at,updated_at)
           VALUES(?,?,?,?,?,'pending',0,?,?)
           ON CONFLICT(candidate_id) DO UPDATE SET updated_at=excluded.updated_at
        """,
        (outbox_id, item["account_id"], operation, version, _json(payload), now, now),
    )
    # Commit the intent before the paper DB transaction.  If the process dies
    # after this point, replay_pending_outbox() can reconcile both ledgers.
    conn.commit()


def _mark_outbox_error(conn, outbox_candidate_id, error, now):
    conn.execute(
        """UPDATE adaptive_risk_outbox
           SET status='error',attempts=attempts+1,last_error=?,updated_at=?
           WHERE candidate_id=?""",
        (str(error)[:500], now, int(outbox_candidate_id)),
    )
    conn.commit()


def _mark_outbox_applied(conn, outbox_candidate_id, now):
    conn.execute(
        """UPDATE adaptive_risk_outbox
           SET status='applied',last_error=NULL,updated_at=?,applied_at=?
           WHERE candidate_id=?""",
        (now, now, int(outbox_candidate_id)),
    )


def cancel_outbox(conn, candidate_id, now, reason="跨账本补偿取消"):
    """终止无法安全重放的应用/回滚意图。

    纸盘已经由外层补偿恢复时，原 pending/error outbox 若继续存在会在
    下一次进化循环再次写入。负键是回滚意图，所以正负键一起终止；已
    applied 的历史意图保持不变。
    """
    candidate_id = int(candidate_id)
    ids = (candidate_id, -candidate_id) if candidate_id else (0,)
    conn.execute(
        """UPDATE adaptive_risk_outbox
              SET status='cancelled',last_error=?,updated_at=?
            WHERE candidate_id IN (?,?) AND status IN ('pending','error')""",
        (str(reason or "跨账本补偿取消")[:500], now, ids[0], ids[1]),
    )
    conn.commit()


def _record_risk_event_once(conn, candidate_id, account_id, event, detail, now):
    """Make the adaptive-side event write safe to replay."""
    found = conn.execute(
        """SELECT 1 FROM adaptive_risk_events
           WHERE candidate_id=? AND account_id=? AND event=? LIMIT 1""",
        (int(candidate_id), account_id, event),
    ).fetchone()
    if not found:
        conn.execute(
            "INSERT INTO adaptive_risk_events(candidate_id,account_id,event,detail,created_at) VALUES(?,?,?,?,?)",
            (candidate_id, account_id, event, detail, now),
        )


def _finalize_apply(conn, candidate, candidate_id, version, effective_date,
                    approved_by, previous_params, now):
    """Finish the adaptive ledger side of an already durable paper apply.

    The paper and adaptive databases are committed separately.  A retry may
    therefore find the proposed parameters already present in the paper
    ledger (``no_change``) while the adaptive candidate is still eligible.
    Finalization must be replay-safe and restore every adaptive-side record
    without creating a second deployment or event.
    """
    conn.execute(
        """UPDATE adaptive_risk_candidates SET status='applied',application_mode=?,previous_account_params=?,
           effective_date=?,applied_at=?,updated_at=? WHERE id=?""",
        (approved_by, previous_params, effective_date, now, now, candidate_id),
    )
    _record_risk_event_once(
        conn, candidate_id, candidate["account_id"], "applied",
        _json({"approved_by": approved_by, "effective_date": effective_date, "effective_at": now}), now,
    )
    _register_deployment(conn, candidate, version, effective_date, now)
    _mark_outbox_applied(conn, candidate_id, now)


def apply_candidate(conn, paper_db_path, candidate_id, now_fn, approved_by="conservative-auto", require_conservative=True):
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM adaptive_risk_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("风控候选不存在")
    candidate = dict(row)
    if candidate["status"] == "applied":
        _mark_outbox_applied(conn, candidate_id, str(now_fn()))
        conn.commit()
        return candidate_id
    allowed = {"eligible_auto_tighten", "human_review_required"}
    if candidate["status"] not in allowed:
        raise ValueError("风控候选尚未通过样本与跨盘面门槛")
    proposed = _loads(candidate["candidate_params"], {})
    if not isinstance(proposed, dict):
        raise ValueError("风控候选参数格式无效")

    # Reuse the original intent when a previous attempt reached the paper DB
    # but died before the adaptive ledger was finalized.  This preserves the
    # original rollback point and version across retries.
    outbox_row = conn.execute(
        "SELECT version,payload FROM adaptive_risk_outbox WHERE candidate_id=? AND status IN ('pending','error')",
        (candidate_id,),
    ).fetchone()
    intent = _loads(outbox_row["payload"], {}) if outbox_row else {}

    paper = _paper_connection(paper_db_path)
    try:
        account_row = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (candidate["account_id"],)).fetchone()
        if not account_row:
            raise ValueError("模拟策略账户不存在")
        account = dict(account_row)
        current, account_params, _ = _current_risk(account)
        if require_conservative and not _is_conservative(current, proposed):
            raise ValueError("自动晋级只允许收紧风险，放宽参数必须人工审批")
        now = str(now_fn())
        effective_date = str(intent.get("effective_date") or now[:10])[:10]
        previous_params = intent.get("previous_account_params")
        if not isinstance(previous_params, str):
            previous_params = _json(account_params)
        approved_by = intent.get("approved_by") or approved_by
        version = str(outbox_row["version"]) if outbox_row else f"risk-evo-{candidate['run_date'].replace('-', '')}-{candidate_id}"
        # P3 审计修复（E4）：no_change 幂等返回——参数完全相同时不再写
        # 新版本/重置观察期（旧路径会制造冗余版本并把部署观察清零）。
        if _change_kind(current, proposed) == "no_change":
            _finalize_apply(
                conn, candidate, candidate_id, version, effective_date,
                approved_by, previous_params, now,
            )
            conn.commit()
            return candidate_id
        # 风控调参对模拟盘即时生效；仍保留精确生效时间、版本、观察期和自动回滚机制。
    finally:
        paper.close()

    # Queue the exact cross-database operation before touching paper DB.
    _queue_outbox(
        conn, candidate, version, effective_date, approved_by, now,
        previous_account_params=previous_params,
        require_conservative=require_conservative,
    )

    paper = _paper_connection(paper_db_path)
    try:
        paper.execute("BEGIN IMMEDIATE")
        account_row = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (candidate["account_id"],)).fetchone()
        if not account_row:
            raise ValueError("模拟策略账户不存在")
        account = dict(account_row)
        existing_version = paper.execute(
            "SELECT 1 FROM paper_parameter_versions WHERE account_id=? AND version=? LIMIT 1",
            (candidate["account_id"], version),
        ).fetchone()
        if not existing_version:
            account_params = _loads(account.get("params"), {})
            account_params["adaptive_risk"] = proposed
            account_params["adaptive_risk_meta"] = {
                "status": "active", "candidate_id": candidate_id, "version": version,
                "approved_by": approved_by, "effective_date": effective_date,
                "effective_at": now,
                "source_regime": candidate["regime"],
            }
            paper.execute(
                "UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
                (_json(account_params), version, now, candidate["account_id"]),
            )
            paper.execute(
                """INSERT INTO paper_parameter_versions(
                   cycle_id,account_id,version,style,params,reason,effective_date,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (account.get("cycle_id"), candidate["account_id"], version, account.get("style") or "adaptive-risk",
                 _json(account_params), f"自进化风控候选 {candidate_id}；{approved_by}", effective_date, now),
            )
            paper.execute(
                "INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
                (candidate["account_id"], "adaptive_risk_applied",
                 f"candidate={candidate_id}; version={version}; effective_at={now}; approved_by={approved_by}", now),
            )
        paper.commit()
    except Exception as exc:
        try:
            paper.rollback()
        finally:
            paper.close()
        _mark_outbox_error(conn, candidate_id, f"{type(exc).__name__}: {exc}", str(now_fn()))
        raise
    finally:
        try:
            paper.close()
        except Exception:
            pass

    now = str(now_fn())
    try:
        _finalize_apply(
            conn, candidate, candidate_id, version, effective_date,
            approved_by, previous_params, now,
        )
        conn.commit()
    except Exception:
        # The paper commit is already durable, so leave the committed outbox
        # intent pending and roll back only the adaptive-side partial writes.
        # A later replay will rebuild the candidate/event/deployment atomically.
        conn.rollback()
        raise
    return candidate_id


def rollback(conn, paper_db_path, account_id, now_fn, reason="人工回滚"):
    ensure_schema(conn)
    candidate = conn.execute(
        "SELECT * FROM adaptive_risk_candidates WHERE account_id=? AND status='applied' ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if not candidate:
        raise ValueError("该策略没有可回滚的自进化风控版本")
    previous = _loads(candidate["previous_account_params"], {})
    candidate = dict(candidate)
    rollback_id = -int(candidate["id"])
    pending = conn.execute(
        "SELECT version,payload FROM adaptive_risk_outbox WHERE candidate_id=? AND status IN ('pending','error')",
        (rollback_id,),
    ).fetchone()
    intent = _loads(pending["payload"], {}) if pending else {}
    now = str(now_fn())
    effective_date = str(intent.get("effective_date") or now[:10])[:10]
    reason = str(intent.get("reason") or reason)[:500]
    version = str(pending["version"]) if pending else f"risk-rollback-{account_id}-{candidate['id']}"

    # Capture account existence and enforce the existing fail-closed version
    # check before recording a rollback intent.
    paper = _paper_connection(paper_db_path)
    try:
        account = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            raise ValueError("模拟策略账户不存在")
        current_version = str(account["version"] or "")
        latest_applied_version = conn.execute(
            """SELECT d.risk_version FROM adaptive_risk_deployments d
                 JOIN adaptive_risk_candidates c ON c.id=d.candidate_id
                WHERE c.account_id=? AND c.status='applied'
                ORDER BY c.id DESC LIMIT 1""",
            (account_id,),
        ).fetchone()
        if (
            latest_applied_version
            and current_version.startswith("risk-")
            and str(latest_applied_version[0] or "") != current_version
            and not (pending and current_version == version)
        ):
            raise ValueError(
                f"账本当前版本 {current_version} 与最新已应用候选版本 {latest_applied_version[0]} 不一致，"
                "可能存在中断的应用操作；请人工核对 paper_parameter_versions 后再回滚"
            )
    finally:
        paper.close()

    _queue_outbox(
        conn, candidate, version, effective_date, "human-rollback", now,
        operation="rollback", outbox_candidate_id=rollback_id, reason=reason,
        previous_account_params=previous,
    )
    _finish_rollback(conn, paper_db_path, candidate, {
        "previous_account_params": previous,
        "version": version,
        "effective_date": effective_date,
        "reason": reason,
    }, now_fn)
    return candidate["id"]


def _finish_rollback(conn, paper_db_path, candidate, payload, now_fn):
    """Reconcile a durable rollback intent without duplicate ledger rows."""
    candidate = dict(candidate)
    candidate_id = int(candidate["id"])
    account_id = str(candidate["account_id"])
    outbox_id = -candidate_id
    if candidate["status"] == "rolled_back":
        _mark_outbox_applied(conn, outbox_id, str(now_fn()))
        conn.commit()
        return candidate_id
    previous = payload.get("previous_account_params") or {}
    if isinstance(previous, str):
        previous = _loads(previous, {})
    if not isinstance(previous, dict):
        raise ValueError("回滚版本缺少可恢复参数")
    now = str(now_fn())
    effective_date = str(payload.get("effective_date") or now[:10])[:10]
    version = str(payload.get("version") or f"risk-rollback-{account_id}-{candidate_id}")
    reason = str(payload.get("reason") or "人工回滚")[:500]
    previous_meta = dict(previous.get("adaptive_risk_meta") or {})
    if previous_meta:
        previous_meta.update({
            "status": "active", "version": version,
            "effective_date": effective_date, "effective_at": now,
            "approved_by": "human-rollback", "rollback_of": candidate_id,
        })
        previous["adaptive_risk_meta"] = previous_meta

    paper = _paper_connection(paper_db_path)
    try:
        paper.execute("BEGIN IMMEDIATE")
        account = paper.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            raise ValueError("模拟策略账户不存在")
        existing_version = paper.execute(
            "SELECT 1 FROM paper_parameter_versions WHERE account_id=? AND version=? LIMIT 1",
            (account_id, version),
        ).fetchone()
        if not existing_version:
            paper.execute(
                "UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
                (_json(previous), version, now, account_id),
            )
            paper.execute(
                """INSERT INTO paper_parameter_versions(
                   cycle_id,account_id,version,style,params,reason,effective_date,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (account["cycle_id"], account_id, version, account["style"] or "adaptive-risk",
                 _json(previous), reason, effective_date, now),
            )
            paper.execute(
                "INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
                (account_id, "adaptive_risk_rolled_back",
                 f"candidate={candidate_id}; effective_at={now}; reason={reason}", now),
            )
        paper.commit()
    except Exception as exc:
        try:
            paper.rollback()
        finally:
            paper.close()
        _mark_outbox_error(conn, outbox_id, f"{type(exc).__name__}: {exc}", str(now_fn()))
        raise
    finally:
        try:
            paper.close()
        except Exception:
            pass

    now = str(now_fn())
    try:
        conn.execute(
            "UPDATE adaptive_risk_candidates SET status='rolled_back',updated_at=? WHERE id=?",
            (now, candidate_id),
        )
        _record_risk_event_once(
            conn, candidate_id, account_id, "rolled_back", _json({"reason": reason}), now,
        )
        conn.execute(
            """UPDATE adaptive_risk_deployments SET status='rolled_back',decision='rollback',
                      reason=?,updated_at=?,reviewed_at=? WHERE candidate_id=?""",
            (reason, now, now, candidate_id),
        )
        _mark_outbox_applied(conn, outbox_id, now)
        conn.commit()
    except Exception:
        # Keep the durable rollback intent pending if adaptive-side finalizing
        # fails after the paper rollback commit.
        conn.rollback()
        raise
    return candidate_id


def replay_pending_outbox(conn, paper_db_path, now_fn, limit=20):
    """Replay risk parameter applies/rollbacks after a process interruption."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT candidate_id,operation,payload,attempts FROM adaptive_risk_outbox
           WHERE status IN ('pending','error') ORDER BY id LIMIT ?""",
        (max(1, min(int(limit), 100)),),
    ).fetchall()
    recovered = []
    for row in rows:
        outbox_id = int(row["candidate_id"])
        payload = _loads(row["payload"], {}) or {}
        try:
            candidate_id = abs(outbox_id)
            candidate_row = conn.execute(
                "SELECT * FROM adaptive_risk_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not candidate_row:
                raise ValueError("风控 outbox 对应候选不存在")
            if str(row["operation"]) == "rollback":
                _finish_rollback(conn, paper_db_path, dict(candidate_row), payload, now_fn)
            else:
                apply_candidate(
                    conn, paper_db_path, candidate_id, now_fn,
                    approved_by=payload.get("approved_by", "conservative-auto"),
                    require_conservative=bool(payload.get("require_conservative", True)),
                )
            recovered.append(outbox_id)
        except Exception as exc:
            # P3 审计修复（E7）：持久性错误重试上限——超过 5 次转 dead
            # 终态，不再每个学习周期空转刷屏；需人工介入。
            attempts = int(row["attempts"] or 0)
            marker = f"replay:{type(exc).__name__}: {exc}"
            if attempts >= 5:
                conn.execute(
                    """UPDATE adaptive_risk_outbox SET status='dead',attempts=attempts+1,
                           last_error=?,updated_at=? WHERE candidate_id=?""",
                    (f"dead-letter: {marker}", str(now_fn()), outbox_id),
                )
                conn.commit()
            else:
                _mark_outbox_error(conn, outbox_id, marker, str(now_fn()))
    return recovered


def evaluate(conn, profile, config, paper_db_path, now_fn):
    ensure_schema(conn)
    if not os.path.exists(paper_db_path):
        return {"status": "paper_db_missing", "candidates": 0, "applied": 0}
    recovered_ids = replay_pending_outbox(conn, paper_db_path, now_fn)
    paper = _paper_connection(paper_db_path)
    try:
        synced_orders = _sync_order_attribution(conn, paper, now_fn())
        captured_days = _capture_daily_outcomes(conn, paper, now_fn())
        synced_downside = _sync_downside_events(conn, paper, now_fn())
    finally:
        paper.close()
    deployment_result = _monitor_deployments(conn, paper_db_path, now_fn)
    deployment_locks = {
        row["account_id"] for row in conn.execute(
            "SELECT account_id FROM adaptive_risk_deployments WHERE status IN ('observing','review_required','rollback_required')"
        )
    }
    paper = _paper_connection(paper_db_path)
    created = 0
    auto_applied = []
    try:
        accounts = [dict(row) for row in paper.execute(
            "SELECT * FROM paper_accounts WHERE id IN ('tq_breakout','trend_pullback','sector_rotation') ORDER BY id"
        )]
        for account in accounts:
            account_id = account["id"]
            current, _, _ = _current_risk(account)
            evidence = _evidence(conn, paper, account_id)
            evidence["strategy_version"] = str(account.get("version") or "")
            evidence["profile_date"] = str(profile.get("profile_date") or "")[:10]
            gates, evolution_tier, tier_checks = _gate_result(evidence, profile, config)
            gate_key = {"fast_shadow": "shadow", "micro": "fast", "standard": "standard", "mature": "mature"}.get(evolution_tier, "shadow")
            evidence["gates"] = tier_checks[gate_key]["checks"]
            evidence["tier_checks"] = tier_checks
            evidence["evolution_tier"] = evolution_tier
            proposed, scale = _propose(account_id, current, profile["regime"], evidence, evolution_tier)
            evidence["regime_scale"] = scale
            kind = _change_kind(current, proposed)
            if account_id in deployment_locks and kind != "no_change":
                status, reason = "deployment_observing", "上一风控版本仍在5个净值日观察或人工复核期；禁止叠加新版本"
            elif kind == "no_change":
                status, reason = "no_change", "当前盘面与兑现证据不要求改变风控边界"
            elif evolution_tier in {"micro", "standard", "mature"} and kind == "conservative_tighten":
                status, reason = "eligible_auto_tighten", f"{evolution_tier} 级证据通过；按该级步长保守收紧，下一交易日生效"
            elif evolution_tier in {"micro", "standard", "mature"}:
                status, reason = "human_review_required", f"{evolution_tier} 级证据通过，但包含风险放宽，必须人工审批"
            elif evolution_tier == "fast_shadow":
                status, reason = "shadow_candidate", "1日快速影子候选已生成；继续观察至3日门槛后才允许小步收紧"
            elif evidence["nav_days"] >= 3 and evidence["reward_samples"] >= 3:
                status, reason = "shadow_candidate", "已进入影子验证，尚未达到自动晋级门槛"
            else:
                status, reason = "waiting_data", "净值、平仓、成熟奖励或跨盘面样本不足"
            payload = {
                "run_date": profile["profile_date"], "account_id": account_id,
                "regime": profile["regime"], "baseline_params": current,
                "candidate_params": proposed, "evidence": evidence,
                "risk_reduction_pct": _risk_reduction(current, proposed),
                "change_kind": kind, "status": status,
                "application_mode": f"{evolution_tier}-conservative-auto" if status == "eligible_auto_tighten" else "shadow",
                "reason": reason,
            }
            candidate_id = _upsert_candidate(conn, payload, now_fn())
            created += 1
            if status == "eligible_auto_tighten" and bool(config.get("risk_auto_apply_conservative", False)):
                apply_candidate(conn, paper_db_path, candidate_id, now_fn, require_conservative=True)
                auto_applied.append(candidate_id)
    finally:
        paper.close()
    return {"status": "completed", "candidates": created, "auto_applied": auto_applied,
            "orders_attributed": synced_orders, "daily_outcomes": captured_days,
            "downside_events": synced_downside,
            "deployments": deployment_result, "recovered_outbox": recovered_ids}


def overview(conn, config, paper_db_path):
    ensure_schema(conn)
    candidates = []
    for row in conn.execute(
        "SELECT * FROM adaptive_risk_candidates ORDER BY run_date DESC,id DESC LIMIT 12"
    ):
        item = dict(row)
        for key in ("baseline_params", "candidate_params", "evidence"):
            item[key] = _loads(item[key], {})
        item.pop("previous_account_params", None)
        item["account_name"] = ACCOUNT_NAMES.get(item["account_id"], item["account_id"])
        candidates.append(item)
    active = []
    if os.path.exists(paper_db_path):
        paper = _paper_connection(paper_db_path)
        try:
            for row in paper.execute(
                "SELECT id,name,version,params FROM paper_accounts WHERE id IN ('tq_breakout','trend_pullback','sector_rotation') ORDER BY id"
            ):
                params = _loads(row["params"], {})
                meta = params.get("adaptive_risk_meta") or {}
                if meta.get("status") == "active":
                    # 风控与选股各自保留独立版本；账户总 version 只是最后一次
                    # 参数写入的指针，不能作为某一子系统的版本事实来源。
                    version = str(meta.get("version") or row["version"] or "")
                    audit = paper.execute(
                        """SELECT effective_date,created_at FROM paper_parameter_versions
                           WHERE account_id=? AND version=? ORDER BY id DESC LIMIT 1""",
                        (row["id"], version),
                    ).fetchone()
                    active.append({
                        "account_id": row["id"], "account_name": row["name"], "version": version,
                        "account_version": row["version"], "params": params.get("adaptive_risk") or {},
                        "meta": meta,
                        "effective_date": (audit["effective_date"] if audit else meta.get("effective_date")),
                        "created_at": (audit["created_at"] if audit else None),
                    })
        finally:
            paper.close()
    attribution = conn.execute(
        """SELECT COUNT(*) total,SUM(decision_linked) linked,SUM(payload_complete) complete,
                  SUM(execution_integrity) integrity,COUNT(DISTINCT risk_version) versions
             FROM adaptive_order_risk_attribution"""
    ).fetchone()
    total_orders = int(attribution["total"] or 0)
    daily_outcomes = [dict(row) for row in conn.execute(
        "SELECT * FROM adaptive_risk_daily_outcomes ORDER BY outcome_date DESC,account_id LIMIT 18"
    )]
    deployments = []
    for row in conn.execute(
        "SELECT * FROM adaptive_risk_deployments ORDER BY candidate_id DESC LIMIT 12"
    ):
        item = dict(row)
        item["baseline"] = _loads(item["baseline"], {})
        item["post_metrics"] = _loads(item["post_metrics"], {})
        item["account_name"] = ACCOUNT_NAMES.get(item["account_id"], item["account_id"])
        deployments.append(item)
    closure = {
        "stage": "observing" if deployments else ("evidence_ready" if total_orders else "not_started"),
        "orders_attributed": total_orders,
        "risk_versions": int(attribution["versions"] or 0),
        "decision_link_pct": round(100 * _num(attribution["linked"]) / max(total_orders, 1), 1),
        "payload_complete_pct": round(100 * _num(attribution["complete"]) / max(total_orders, 1), 1),
        "execution_integrity_pct": round(100 * _num(attribution["integrity"]) / max(total_orders, 1), 1),
        "daily_outcomes": daily_outcomes,
        "deployments": deployments,
        "flow": [
            "订单前风控决策", "订单与生效版本归因", "成交/拒绝/延期结果回写",
            "每日风险结果账本", "生成有界候选", "次日生效观察", "验证/人工复核/技术回滚",
        ],
        "rollback_policy": "接线或执行完整性故障自动回滚；收益与回撤恶化只暂停进化并要求人工复核，避免错误归因。",
    }
    return {
        "mode": "受约束自动优化",
        "policy": "证据达标后仅允许自动收紧；任何风险放宽必须人工审批，所有版本均可回滚。",
        "auto_apply_conservative": bool(config.get("risk_auto_apply_conservative", False)),
        "requirements": _tier_requirements(config, "shadow"),
        "tiers": {
            "fast_shadow": {"label": "1日快速影子", "max_step": "仅观察，不改账户", "requirements": _tier_requirements(config, "shadow")},
            "micro": {"label": "3日小步调整", "max_step": "约12%保守收紧", "requirements": _tier_requirements(config, "fast")},
            "standard": {"label": "5日标准验证", "max_step": "约10%", "requirements": _tier_requirements(config, "standard")},
            "mature": {"label": "10日成熟进化", "max_step": "完整受限区间", "requirements": _tier_requirements(config, "mature")},
        },
        "candidates": candidates,
        "active_versions": active,
        "closure": closure,
        "downside_policy": {
            "engine": "主力意图 + 三段式下跌防线",
            "confirmation_scans": 2,
            "defaults": DOWNSIDE_BASE,
            "bounds": DOWNSIDE_BOUNDS,
            "auto_change_rule": "至少3个事件且确认出货占比≥60%、奖励窗口为负时，只允许保守收紧；放宽必须人工审批",
        },
        "deepseek_advisor": {
            "enabled": bool(config.get("llm_advisor_enabled", False)),
            "configured": bool(os.getenv("DEEPSEEK_API_KEY")),
            "provider": "DeepSeek",
            "mode": "research_only",
            "can": ["解释异常", "总结事件", "生成待验证假设", "辅助周报"],
            "cannot": ["直接下单", "绕过门禁", "直接改参数", "接触API密钥前端"],
        },
    }
