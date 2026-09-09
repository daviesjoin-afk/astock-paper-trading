# -*- coding: utf-8 -*-
r"""True shadow Champion / Challenger control plane.

The Champion alone drives the formal paper account. Opening a Challenger
creates immutable candidate parameters and isolated shadow ledgers; it never
writes the formal active parameter head. Promotion is the only head switch.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Mapping

__all__ = [
    "STRATEGY_CHAMPION_VERSION", "ROLE_CHAMPION", "ROLE_CHALLENGER",
    "STATUS_SHADOW", "STATUS_READY", "STATUS_PROMOTED", "STATUS_ROLLED_BACK",
    "PROMOTION_TOLERANCE", "active_runtime_checksum", "collect_ledger_metrics",
    "collect_shadow_ledger_metrics", "compare_for_promotion", "ensure_schema",
    "evaluate_challenger", "open_challenger", "promote_challenger",
    "rollback_challenger", "run_shadow_context", "run_shadow_counterfactual",
]

STRATEGY_CHAMPION_VERSION = "strategy-champion-v3"
ROLE_CHAMPION = "champion"
ROLE_CHALLENGER = "challenger"
STATUS_SHADOW = "shadow"
STATUS_READY = "ready"
STATUS_PROMOTED = "promoted"
STATUS_ROLLED_BACK = "rolled_back"
PROMOTION_TOLERANCE = {
    "max_drawdown_pct": 0.5,
    "turnover_amount_ratio": 0.15,
    "execution_fill_rate": 5.0,
    "concentration_hhi": 0.05,
}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _checksum(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def ensure_schema(conn) -> None:
    """Create idempotent metadata and shadow-only ledger tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_champion_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL, role TEXT NOT NULL, params TEXT NOT NULL,
            champion_params TEXT, base_version_id INTEGER, shadow_version_id INTEGER,
            source TEXT NOT NULL, status TEXT NOT NULL, evidence_count INTEGER,
            proposed_at TEXT NOT NULL, evaluated_at TEXT, metrics TEXT, decision TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_champion_recent
            ON strategy_champion_versions(strategy_id, role, id DESC);

        CREATE TABLE IF NOT EXISTS strategy_shadow_parameter_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL,
            params TEXT NOT NULL, checksum TEXT NOT NULL, base_active_version_id INTEGER,
            base_active_checksum TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_shadow_params_recent
            ON strategy_shadow_parameter_versions(strategy_id, id DESC);
        CREATE TRIGGER IF NOT EXISTS strategy_shadow_params_immutable_update
            BEFORE UPDATE ON strategy_shadow_parameter_versions
            BEGIN SELECT RAISE(ABORT, 'shadow parameter versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS strategy_shadow_params_immutable_delete
            BEFORE DELETE ON strategy_shadow_parameter_versions
            BEGIN SELECT RAISE(ABORT, 'shadow parameter versions are immutable'); END;

        CREATE TABLE IF NOT EXISTS strategy_shadow_contexts(
            challenger_id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL,
            champion_params TEXT NOT NULL, champion_checksum TEXT NOT NULL,
            challenger_parameter_version_id INTEGER NOT NULL,
            challenger_checksum TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, challenger_id INTEGER NOT NULL,
            role TEXT NOT NULL, snapshot_checksum TEXT NOT NULL, strategy_id TEXT NOT NULL,
            signal_key TEXT, code TEXT, side TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, challenger_id INTEGER NOT NULL,
            role TEXT NOT NULL, snapshot_checksum TEXT NOT NULL, strategy_id TEXT NOT NULL,
            signal_key TEXT, code TEXT, side TEXT, qty REAL NOT NULL DEFAULT 0,
            planned_price REAL, amount REAL NOT NULL DEFAULT 0, status TEXT,
            payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_fills(
            id INTEGER PRIMARY KEY AUTOINCREMENT, challenger_id INTEGER NOT NULL,
            role TEXT NOT NULL, snapshot_checksum TEXT NOT NULL, strategy_id TEXT NOT NULL,
            code TEXT, side TEXT, qty REAL NOT NULL DEFAULT 0, price REAL,
            amount REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0,
            payload TEXT NOT NULL, filled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_nav(
            id INTEGER PRIMARY KEY AUTOINCREMENT, challenger_id INTEGER NOT NULL,
            role TEXT NOT NULL, snapshot_checksum TEXT NOT NULL, nav_date TEXT,
            cash REAL NOT NULL DEFAULT 0, market_value REAL NOT NULL DEFAULT 0,
            nav REAL NOT NULL DEFAULT 0, payload TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(challenger_id, role, snapshot_checksum)
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_counterfactual_window
            ON shadow_nav(challenger_id, role, created_at, snapshot_checksum);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_champion_versions)")}
    if "base_checksum" not in columns:
        conn.execute("ALTER TABLE strategy_champion_versions ADD COLUMN base_checksum TEXT")
    if "shadow_param_version_id" not in columns:
        conn.execute("ALTER TABLE strategy_champion_versions ADD COLUMN shadow_param_version_id INTEGER")


def active_runtime_checksum(evo_conn, strategy_id: str) -> dict[str, Any]:
    """Canonical checksum of the formal active parameter head."""
    import self_evolution as SE

    current = SE.get_strategy_params(evo_conn, strategy_id)
    return {"strategy_id": str(strategy_id), "version_id": current.get("id"),
            "checksum": _checksum(current["params"]), "params": dict(current["params"])}


def _latest(conn, strategy_id: str, role: str, statuses: tuple[str, ...]):
    placeholders = ",".join("?" for _ in statuses)
    row = conn.execute(
        f"SELECT * FROM strategy_champion_versions WHERE strategy_id=? AND role=? "
        f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (str(strategy_id), role, *statuses),
    ).fetchone()
    return dict(row) if row is not None else None


def _metric_result(*, realized, denom, buy_amount, attempts, filled_buys, per_code, navs, since, until):
    peak = max_drawdown = 0.0
    for nav in navs:
        peak = max(peak, nav)
        max_drawdown = max(max_drawdown, peak - nav)
    hhi = sum((value / max(buy_amount, 1.0)) ** 2 for value in per_code.values()) if per_code else 0.0
    return {
        "window_start": str(since)[:19], "window_end": str(until)[:19],
        "return_pct": round(float(realized) / max(float(denom), 1.0) * 100.0, 4),
        "max_drawdown_pct": round(max_drawdown / max(float(denom), 1.0) * 100.0, 4),
        "turnover_amount": round(float(buy_amount), 2),
        "execution_fill_rate": round(float(filled_buys) / max(int(attempts), 1) * 100.0, 2) if attempts else 0.0,
        "concentration_hhi": round(hhi, 4), "filled_orders": int(filled_buys),
        "version": STRATEGY_CHAMPION_VERSION,
    }


def collect_ledger_metrics(conn, strategy_id: str, since: str, until: str) -> dict[str, Any]:
    """Legacy formal-ledger helper retained for callers outside promotion."""
    orders = [dict(row) for row in conn.execute(
        """SELECT side,status,code,COALESCE(amount,0) amount,COALESCE(realized_pnl,0) realized_pnl,
                  executed_at,created_at FROM paper_orders WHERE account_id=? AND status='filled'
               AND executed_at IS NOT NULL AND executed_at>=? AND executed_at<? AND COALESCE(amount,0)>0""",
        (str(strategy_id), str(since), str(until)),
    ).fetchall()]
    attempts = conn.execute(
        """SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND side='buy'
              AND ((created_at>=? AND created_at<?) OR (executed_at>=? AND executed_at<?))""",
        (str(strategy_id), str(since), str(until), str(since), str(until)),
    ).fetchone()[0]
    buys = [item for item in orders if item["side"] == "buy"]
    buy_amount = sum(float(item["amount"] or 0) for item in buys)
    per_code: dict[str, float] = {}
    for item in buys:
        per_code[item["code"]] = per_code.get(item["code"], 0.0) + float(item["amount"] or 0)
    daily: dict[str, float] = {}
    for item in orders:
        if item["side"] == "sell":
            day = str(item["executed_at"])[:10]
            daily[day] = daily.get(day, 0.0) + float(item["realized_pnl"] or 0)
    cumulative = peak = max_drawdown = 0.0
    for day in sorted(daily):
        cumulative += daily[day]
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    carried = conn.execute(
        "SELECT COALESCE(SUM(qty*cost),0) FROM paper_positions WHERE account_id=? AND entry_date<?",
        (str(strategy_id), str(since)[:10]),
    ).fetchone()[0]
    carried_value = float(carried or 0)
    result = _metric_result(realized=sum(float(item["realized_pnl"] or 0) for item in orders if item["side"] == "sell"),
                            denom=max(buy_amount + carried_value, 1.0), buy_amount=buy_amount, attempts=attempts,
                            filled_buys=len(buys), per_code=per_code, navs=[], since=since, until=until)
    result["max_drawdown_pct"] = round(max_drawdown / max(buy_amount + carried_value, 1.0) * 100.0, 4)
    result["carried_value"] = round(carried_value, 2)
    return result


def collect_shadow_ledger_metrics(conn, challenger_id: int, role: str, since: str, until: str) -> dict[str, Any]:
    """Derive one side's metrics from only this Challenger's shadow ledger."""
    orders = [dict(row) for row in conn.execute(
        "SELECT code,side,amount FROM shadow_orders WHERE challenger_id=? AND role=? AND created_at>=? AND created_at<?",
        (int(challenger_id), role, str(since), str(until)),
    ).fetchall()]
    fills = [dict(row) for row in conn.execute(
        "SELECT code,side,amount,realized_pnl FROM shadow_fills WHERE challenger_id=? AND role=? AND filled_at>=? AND filled_at<?",
        (int(challenger_id), role, str(since), str(until)),
    ).fetchall()]
    navs = [float(row[0] or 0) for row in conn.execute(
        "SELECT nav FROM shadow_nav WHERE challenger_id=? AND role=? AND created_at>=? AND created_at<? ORDER BY created_at,id",
        (int(challenger_id), role, str(since), str(until)),
    ).fetchall()]
    buy_orders = [item for item in orders if item["side"] == "buy"]
    buy_fills = [item for item in fills if item["side"] == "buy"]
    buy_amount = sum(float(item["amount"] or 0) for item in buy_fills)
    per_code: dict[str, float] = {}
    for item in buy_fills:
        per_code[item["code"]] = per_code.get(item["code"], 0.0) + float(item["amount"] or 0)
    return _metric_result(realized=sum(float(item["realized_pnl"] or 0) for item in fills if item["side"] == "sell"),
                          denom=max(navs[0] if navs else 0, buy_amount, 1.0), buy_amount=buy_amount,
                          attempts=len(buy_orders), filled_buys=len(buy_fills), per_code=per_code,
                          navs=navs, since=since, until=until)


def compare_for_promotion(champion_metrics: Mapping[str, Any], challenger_metrics: Mapping[str, Any], *, tolerance=None) -> dict[str, Any]:
    tol = dict(PROMOTION_TOLERANCE if tolerance is None else tolerance)
    rows = []
    champion_return, challenger_return = float(champion_metrics.get("return_pct") or 0), float(challenger_metrics.get("return_pct") or 0)
    rows.append({"key": "return_pct", "label": "收益（必须改善）", "champion": champion_return, "challenger": challenger_return,
                 "passed": challenger_return > champion_return, "reason": None if challenger_return > champion_return else "收益未改善"})
    for key, label, rule in (
        ("max_drawdown_pct", "最大回撤", lambda c, x: x <= c + tol["max_drawdown_pct"]),
        ("execution_fill_rate", "成交率", lambda c, x: x >= c - tol["execution_fill_rate"]),
        ("concentration_hhi", "集中度", lambda c, x: x <= c + tol["concentration_hhi"]),
    ):
        c, x = float(champion_metrics.get(key) or 0), float(challenger_metrics.get(key) or 0)
        rows.append({"key": key, "label": label, "champion": c, "challenger": x, "passed": bool(rule(c, x)),
                     "reason": None if rule(c, x) else f"{label}恶化"})
    c, x = float(champion_metrics.get("turnover_amount") or 0), float(challenger_metrics.get("turnover_amount") or 0)
    turnover_ok = x <= max(c, 1.0) * (1 + tol["turnover_amount_ratio"])
    rows.append({"key": "turnover_amount_ratio", "label": "换手", "champion": c, "challenger": x,
                 "passed": turnover_ok, "reason": None if turnover_ok else "换手恶化"})
    return {"promotable": all(row["passed"] for row in rows), "dimensions": rows,
            "failed": [row["label"] for row in rows if not row["passed"]], "version": STRATEGY_CHAMPION_VERSION}


def open_challenger(paper_conn, evo_conn, strategy_id: str, params: Mapping[str, Any], *, source="evolution", evidence_count=None, now=None) -> dict[str, Any]:
    """Create immutable candidate parameters without changing formal runtime."""
    import evolution_profiles as EP

    ensure_schema(paper_conn)
    if _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,)) is not None:
        return {"opened": False, "reason": "该策略已有 shadow Challenger，请先评估或回滚"}
    before = active_runtime_checksum(evo_conn, strategy_id)
    check = EP.validate_strategy_adjustment(strategy_id, before["params"], dict(params or {}), evidence_count=evidence_count)
    if not check["allowed"]:
        return {"opened": False, "violations": check["violations"], "reason": "候选参数未通过策略进化画像校验（fail-closed）"}
    candidate = EP.clamp_to_profile(strategy_id, {**before["params"], **check["adjusted"]})
    if _checksum(candidate) == before["checksum"]:
        return {"opened": False, "reason": "候选参数与当前 Champion 完全相同"}
    moment = (now or dt.datetime.now()).isoformat(timespec="seconds")
    shadow_param_id = paper_conn.execute(
        """INSERT INTO strategy_shadow_parameter_versions(strategy_id,params,checksum,base_active_version_id,base_active_checksum,created_at)
           VALUES(?,?,?,?,?,?)""",
        (str(strategy_id), _json_dumps(candidate), _checksum(candidate), before["version_id"], before["checksum"], moment),
    ).lastrowid
    challenger_id = paper_conn.execute(
        """INSERT INTO strategy_champion_versions(strategy_id,role,params,champion_params,base_version_id,shadow_version_id,
               shadow_param_version_id,base_checksum,source,status,evidence_count,proposed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(strategy_id), ROLE_CHALLENGER, _json_dumps(candidate), _json_dumps(before["params"]), before["version_id"],
         shadow_param_id, shadow_param_id, before["checksum"], str(source), STATUS_SHADOW, evidence_count, moment, moment),
    ).lastrowid
    paper_conn.execute(
        """INSERT INTO strategy_shadow_contexts(challenger_id,strategy_id,champion_params,champion_checksum,
               challenger_parameter_version_id,challenger_checksum,created_at) VALUES(?,?,?,?,?,?,?)""",
        (challenger_id, str(strategy_id), _json_dumps(before["params"]), before["checksum"], shadow_param_id, _checksum(candidate), moment),
    )
    paper_conn.commit()
    after = active_runtime_checksum(evo_conn, strategy_id)
    if after["checksum"] != before["checksum"]:
        raise RuntimeError("opening a Challenger changed the active runtime head")
    return {"opened": True, "challenger_id": int(challenger_id), "strategy_id": str(strategy_id), "status": STATUS_SHADOW,
            "params": candidate, "shadow_param_version_id": int(shadow_param_id),
            "active_runtime_checksum_before": before["checksum"], "active_runtime_checksum_after": after["checksum"],
            "version": STRATEGY_CHAMPION_VERSION}


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in (value or []) if isinstance(item, Mapping)]


def run_shadow_context(paper_conn, evo_conn, strategy_id: str, market_snapshot: Mapping[str, Any], runner, *, observed_at: dt.datetime | None = None) -> dict[str, Any]:
    """Run a deterministic internal strategy runner twice against one snapshot.

    ``runner(role, params, snapshot)`` is deliberately an internal callable,
    rather than a web payload: untrusted clients must not be able to fabricate
    shadow performance and promote parameters.  The snapshot is JSON-cloned
    per side so neither invocation can mutate the other's input.
    """
    ensure_schema(paper_conn)
    active = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if active is None:
        return {"recorded": False, "reason": "没有处于 shadow 的 Challenger"}
    context = paper_conn.execute(
        "SELECT champion_params,champion_checksum,challenger_parameter_version_id FROM strategy_shadow_contexts WHERE challenger_id=?",
        (active["id"],),
    ).fetchone()
    if context is None:
        return {"recorded": False, "reason": "Challenger 缺少 immutable shadow context"}
    formal = active_runtime_checksum(evo_conn, strategy_id)
    if formal["checksum"] != context["champion_checksum"]:
        return {"recorded": False, "reason": "Champion 参数头已变化，影子上下文过期"}
    candidate = paper_conn.execute(
        "SELECT params FROM strategy_shadow_parameter_versions WHERE id=?",
        (context["challenger_parameter_version_id"],),
    ).fetchone()
    if candidate is None:
        return {"recorded": False, "reason": "immutable Challenger 参数版本缺失"}
    champion_params = _json_loads(context["champion_params"])
    challenger_params = _json_loads(candidate["params"])
    champion_output = runner(ROLE_CHAMPION, champion_params, _json_loads(_json_dumps(market_snapshot)))
    challenger_output = runner(ROLE_CHALLENGER, challenger_params, _json_loads(_json_dumps(market_snapshot)))
    return run_shadow_counterfactual(
        paper_conn, strategy_id, market_snapshot, champion_output, challenger_output, observed_at=observed_at,
    )


def run_shadow_counterfactual(paper_conn, strategy_id: str, market_snapshot: Mapping[str, Any], champion: Mapping[str, Any], challenger: Mapping[str, Any], *, observed_at: dt.datetime | None = None) -> dict[str, Any]:
    """Write both outputs for exactly one market snapshot to isolated ledgers."""
    ensure_schema(paper_conn)
    active = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if active is None:
        return {"recorded": False, "reason": "没有处于 shadow 的 Challenger"}
    if not isinstance(market_snapshot, Mapping) or not market_snapshot:
        raise ValueError("market_snapshot 必须是非空映射")
    if not isinstance(champion, Mapping) or not isinstance(challenger, Mapping):
        raise ValueError("Champion 与 Challenger 输出必须是映射")
    moment, snapshot_checksum = (observed_at or dt.datetime.now()).isoformat(timespec="seconds"), _checksum(market_snapshot)
    counts = {}
    for role, output in ((ROLE_CHAMPION, champion), (ROLE_CHALLENGER, challenger)):
        signals, orders, fills = _mapping_rows(output.get("signals")), _mapping_rows(output.get("orders")), _mapping_rows(output.get("fills"))
        for signal in signals:
            paper_conn.execute("INSERT INTO shadow_signals(challenger_id,role,snapshot_checksum,strategy_id,signal_key,code,side,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                               (active["id"], role, snapshot_checksum, str(strategy_id), str(signal.get("signal_key") or ""), signal.get("code"), signal.get("side"), _json_dumps(dict(signal)), moment))
        for order in orders:
            paper_conn.execute("""INSERT INTO shadow_orders(challenger_id,role,snapshot_checksum,strategy_id,signal_key,code,side,qty,planned_price,amount,status,payload,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (active["id"], role, snapshot_checksum, str(strategy_id), str(order.get("signal_key") or ""), order.get("code"), order.get("side"), float(order.get("qty") or 0), order.get("planned_price"), float(order.get("amount") or 0), order.get("status"), _json_dumps(dict(order)), moment))
        for fill in fills:
            paper_conn.execute("""INSERT INTO shadow_fills(challenger_id,role,snapshot_checksum,strategy_id,code,side,qty,price,amount,realized_pnl,payload,filled_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (active["id"], role, snapshot_checksum, str(strategy_id), fill.get("code"), fill.get("side"), float(fill.get("qty") or 0), fill.get("price"), float(fill.get("amount") or 0), float(fill.get("realized_pnl") or 0), _json_dumps(dict(fill)), moment))
        nav = dict(output.get("nav") or {})
        paper_conn.execute("""INSERT INTO shadow_nav(challenger_id,role,snapshot_checksum,nav_date,cash,market_value,nav,payload,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?)""",
                           (active["id"], role, snapshot_checksum, nav.get("nav_date") or moment[:10], float(nav.get("cash") or 0), float(nav.get("market_value") or 0), float(nav.get("nav") or 0), _json_dumps(nav), moment))
        counts[role] = {"signals": len(signals), "orders": len(orders), "fills": len(fills), "nav": 1}
    paper_conn.commit()
    return {"recorded": True, "challenger_id": int(active["id"]), "snapshot_checksum": snapshot_checksum, "counts": counts, "version": STRATEGY_CHAMPION_VERSION}


def _counterfactual_snapshots(conn, challenger_id: int, role: str, since: str, until: str) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT snapshot_checksum FROM shadow_nav WHERE challenger_id=? AND role=? AND created_at>=? AND created_at<?", (int(challenger_id), role, str(since), str(until))).fetchall()}


def evaluate_challenger(paper_conn, evo_conn, strategy_id: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Compare only same-period rows carrying identical market snapshot checksums."""
    challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if challenger is None:
        ready = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_READY,))
        return ({"evaluated": False, "status": STATUS_READY, "challenger_id": int(ready["id"]), "reason": "已有待晋升 Challenger"} if ready else {"evaluated": False, "reason": "该策略没有处于 shadow 的 Challenger"})
    moment = now or dt.datetime.now()
    proposed = dt.datetime.fromisoformat(str(challenger["proposed_at"])[:19])
    if (moment - proposed).total_seconds() < 3600:
        return {"evaluated": False, "reason": "影子观察窗不足 1 小时，暂不评估", "challenger_id": int(challenger["id"])}
    since, until = proposed.isoformat(timespec="seconds"), moment.isoformat(timespec="seconds")
    champion_snapshots = _counterfactual_snapshots(paper_conn, challenger["id"], ROLE_CHAMPION, since, until)
    challenger_snapshots = _counterfactual_snapshots(paper_conn, challenger["id"], ROLE_CHALLENGER, since, until)
    if not champion_snapshots or champion_snapshots != challenger_snapshots:
        return {"evaluated": False, "challenger_id": int(challenger["id"]), "reason": "缺少同期间同快照的 Champion/Challenger counterfactual 账本"}
    champion_metrics = collect_shadow_ledger_metrics(paper_conn, challenger["id"], ROLE_CHAMPION, since, until)
    challenger_metrics = collect_shadow_ledger_metrics(paper_conn, challenger["id"], ROLE_CHALLENGER, since, until)
    decision = compare_for_promotion(champion_metrics, challenger_metrics)
    decision["counterfactual_snapshot_checksums"] = sorted(champion_snapshots)
    status = STATUS_READY if decision["promotable"] else STATUS_ROLLED_BACK
    paper_conn.execute("UPDATE strategy_champion_versions SET status=?,evaluated_at=?,metrics=?,decision=? WHERE id=?",
                       (status, until, _json_dumps({"champion": champion_metrics, "challenger": challenger_metrics}), _json_dumps(decision), challenger["id"]))
    paper_conn.commit()
    return {"evaluated": True, "challenger_id": int(challenger["id"]), "decision": decision, "champion_metrics": champion_metrics, "challenger_metrics": challenger_metrics, "status": status, "version": STRATEGY_CHAMPION_VERSION}


def promote_challenger(paper_conn, evo_conn, strategy_id: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Promotion alone writes one new active parameter version."""
    import self_evolution as SE

    challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW, STATUS_READY))
    if challenger is None:
        return {"promoted": False, "reason": "没有可晋升的 Challenger"}
    if challenger["status"] == STATUS_SHADOW:
        evaluation = evaluate_challenger(paper_conn, evo_conn, strategy_id, now=now)
        if not evaluation.get("evaluated"):
            return {"promoted": False, "reason": evaluation.get("reason")}
        challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_READY,))
        if challenger is None:
            return {"promoted": False, "reason": "晋升门禁未通过，Challenger 已结束"}
    active = active_runtime_checksum(evo_conn, strategy_id)
    if active["checksum"] != challenger.get("base_checksum"):
        return {"promoted": False, "reason": "Champion 参数头已变化，拒绝过期 Challenger 晋升"}
    candidate = _json_loads(challenger["params"])
    diffs = {key: value for key, value in candidate.items() if active["params"].get(key) != value}
    activated = SE.adjust_strategy_params(evo_conn, strategy_id, diffs, reason="challenger_promotion", source="challenger_promotion", evidence_count=challenger.get("evidence_count"))
    if not activated.get("adjusted"):
        return {"promoted": False, "reason": "无法原子切换 active 参数头：" + str(activated.get("reason") or activated.get("violations"))}
    after = active_runtime_checksum(evo_conn, strategy_id)
    if after["checksum"] != _checksum(candidate):
        raise RuntimeError("promotion did not produce the Challenger parameter head")
    now_text = (now or dt.datetime.now()).isoformat(timespec="seconds")
    paper_conn.execute("UPDATE strategy_champion_versions SET status='superseded' WHERE strategy_id=? AND role=? AND status=?", (str(strategy_id), ROLE_CHAMPION, STATUS_PROMOTED))
    paper_conn.execute("UPDATE strategy_champion_versions SET status=? WHERE id=?", (STATUS_PROMOTED, challenger["id"]))
    paper_conn.execute("""INSERT INTO strategy_champion_versions(strategy_id,role,params,base_version_id,base_checksum,source,status,proposed_at,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (str(strategy_id), ROLE_CHAMPION, _json_dumps(candidate), after["version_id"], after["checksum"], "promotion", STATUS_PROMOTED, now_text, now_text))
    paper_conn.commit()
    return {"promoted": True, "strategy_id": str(strategy_id), "challenger_id": int(challenger["id"]), "params": candidate, "active_parameter_version_id": after["version_id"], "active_runtime_checksum": after["checksum"], "version": STRATEGY_CHAMPION_VERSION}


def rollback_challenger(paper_conn, evo_conn, strategy_id: str, reason: str = "manual_rollback") -> dict[str, Any]:
    """Discard shadow metadata without restoring or touching formal runtime."""
    challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW, STATUS_READY))
    if challenger is None:
        return {"rolled_back": False, "reason": "没有可回滚的 Challenger"}
    paper_conn.execute("UPDATE strategy_champion_versions SET status=?,decision=? WHERE id=?", (STATUS_ROLLED_BACK, _json_dumps({"rolled_back": True, "reason": str(reason)[:200], "formal_runtime_unchanged": True}), challenger["id"]))
    paper_conn.commit()
    return {"rolled_back": True, "strategy_id": str(strategy_id), "challenger_id": int(challenger["id"]), "reason": str(reason)[:200], "version": STRATEGY_CHAMPION_VERSION}
