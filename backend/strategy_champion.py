# -*- coding: utf-8 -*-
"""策略 Champion / Challenger 版本管理（PR：champion challenger strategy versions）。

每个策略维护一条 Champion（当前生效参数版本）与至多一个 Challenger（影子
候选版本）。Challenger 不直接改变运行行为，先进入 shadow 观察窗：

- **影子评估口径**：Challenger 窗口 = ``proposed_at → now``；Champion 对照窗
  = 紧邻的等长时间段。两段都在同一账本上重放，指标同口径可比。
- **五维比较**：return、max drawdown、turnover、execution（成交率）、
  concentration（按买入金额的 HHI）。
- **晋升门禁（fail-closed）**：必须"收益改善**且**风险四维全部不恶化"
  （在容差内）才允许 promotion——任何一维恶化即拒绝。
- **失败自动回滚**：evaluate 给出 reject 结论时 Challenger 自动置为
  ``rolled_back``；显式 rollback 也随时可丢弃 Challenger 回到 Champion。

指标全部从模拟账本（paper_orders / paper_positions）推导，断网可算。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

__all__ = [
    "STRATEGY_CHAMPION_VERSION",
    "ROLE_CHAMPION",
    "ROLE_CHALLENGER",
    "STATUS_SHADOW",
    "STATUS_PROMOTED",
    "STATUS_ROLLED_BACK",
    "PROMOTION_TOLERANCE",
    "collect_ledger_metrics",
    "compare_for_promotion",
    "ensure_schema",
    "evaluate_challenger",
    "open_challenger",
    "promote_challenger",
    "rollback_challenger",
]

STRATEGY_CHAMPION_VERSION = "strategy-champion-v1"
ROLE_CHAMPION = "champion"
ROLE_CHALLENGER = "challenger"
STATUS_SHADOW = "shadow"
STATUS_PROMOTED = "promoted"
STATUS_ROLLED_BACK = "rolled_back"

# 风险维度的容差：恶化在容差内视为"不恶化"（相对/绝对按维度定义）。
PROMOTION_TOLERANCE = {
    "max_drawdown_pct": 0.5,     # 允许多 0.5 个百分点的回撤
    "turnover_amount_ratio": 0.15,  # 换手最多高出 15%
    "execution_fill_rate": 0.05,    # 成交率最多低 5 个百分点
    "concentration_hhi": 0.05,      # 集中度 HHI 最多高 0.05
}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_champion_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            role TEXT NOT NULL,             -- 'champion' | 'challenger'
            params TEXT NOT NULL,           -- JSON: 策略参数版本
            base_version_id INTEGER,        -- challenger 的母本版本
            source TEXT NOT NULL,           -- 'evolution' | 'manual' | 'init'
            status TEXT NOT NULL,           -- 'shadow' | 'promoted' | 'rejected' | 'rolled_back'
            proposed_at TEXT NOT NULL,
            evaluated_at TEXT,
            metrics TEXT,                   -- JSON: 评估指标快照
            decision TEXT,                  -- JSON: 晋升判定详情
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_champion_recent
            ON strategy_champion_versions(strategy_id, role, id DESC);
        """
    )


def _rows_to_dicts(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def collect_ledger_metrics(conn, strategy_id: str, since: str, until: str) -> dict[str, Any]:
    """从模拟账本推导一个时间窗内的五维策略指标（断网可算）。"""
    window_orders = _rows_to_dicts(conn.execute(
        """SELECT side,status,code,COALESCE(amount,0) AS amount,
                  COALESCE(realized_pnl,0) AS realized_pnl,executed_at,created_at
             FROM paper_orders
            WHERE account_id=? AND status='filled'
              AND executed_at IS NOT NULL AND executed_at>=? AND executed_at<?
              AND COALESCE(amount,0)>0
            ORDER BY executed_at""",
        (str(strategy_id), str(since), str(until)),
    ).fetchall())
    # 成交率分母 = 窗口内所有买入尝试（含未成交/被拒），按 created_at 计。
    attempts = conn.execute(
        """SELECT COUNT(*) FROM paper_orders
            WHERE account_id=? AND side='buy' AND created_at>=? AND created_at<?""",
        (str(strategy_id), str(since), str(until)),
    ).fetchone()[0]
    filled = len(window_orders)
    buy_amount = sum(float(o["amount"]) for o in window_orders if o["side"] == "buy")
    realized = sum(float(o["realized_pnl"]) for o in window_orders if o["side"] == "sell")
    filled_buys = sum(1 for o in window_orders if o["side"] == "buy")
    # 日度已实现盈亏序列 → 最大回撤
    daily: dict[str, float] = {}
    for order in window_orders:
        if order["side"] == "sell":
            day = str(order["executed_at"])[:10]
            daily[day] = daily.get(day, 0.0) + float(order["realized_pnl"])
    cumulative, peak, max_drawdown = 0.0, 0.0, 0.0
    for day in sorted(daily):
        cumulative += daily[day]
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    denom = max(buy_amount, 1.0)
    per_code: dict[str, float] = {}
    for order in window_orders:
        if order["side"] == "buy":
            per_code[order["code"]] = per_code.get(order["code"], 0.0) + float(order["amount"])
    hhi = sum((value / denom) ** 2 for value in per_code.values()) if per_code else 0.0
    return {
        "window_start": str(since)[:10],
        "window_end": str(until)[:10],
        "return_pct": round(realized / denom * 100.0, 4),
        "max_drawdown_pct": round(max_drawdown / denom * 100.0, 4),
        "turnover_amount": round(buy_amount, 2),
        "execution_fill_rate": round(
            (filled_buys / attempts * 100.0) if attempts else 0.0, 2),
        "concentration_hhi": round(hhi, 4),
        "filled_orders": filled,
        "version": STRATEGY_CHAMPION_VERSION,
    }


def compare_for_promotion(
    champion_metrics: Mapping[str, Any],
    challenger_metrics: Mapping[str, Any],
    *,
    tolerance: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """晋升门禁：收益改善 **且** 风险四维全部不恶化（fail-closed）。"""
    tol = dict(PROMOTION_TOLERANCE if tolerance is None else tolerance)
    champion_return = float(champion_metrics.get("return_pct") or 0.0)
    challenger_return = float(challenger_metrics.get("return_pct") or 0.0)
    dimensions: list[dict[str, Any]] = []

    dimensions.append({
        "key": "return_pct", "label": "收益（必须改善）",
        "champion": champion_return, "challenger": challenger_return,
        "passed": challenger_return > champion_return,
        "reason": None if challenger_return > champion_return else (
            f"收益未改善：{challenger_return:.2f}% ≤ Champion {champion_return:.2f}%"),
    })
    checks = (
        ("max_drawdown_pct", "最大回撤",
         lambda c, x: x <= c + tol["max_drawdown_pct"],
         lambda c, x: f"回撤恶化 {x:.2f}% > {c:.2f}% + {tol['max_drawdown_pct']}"),
        ("turnover_amount_ratio", "换手",
         None, None),
        ("execution_fill_rate", "成交率",
         lambda c, x: x >= c - tol["execution_fill_rate"],
         lambda c, x: f"成交率下降 {x:.2f}% < {c:.2f}% - {tol['execution_fill_rate']}"),
        ("concentration_hhi", "集中度",
         lambda c, x: x <= c + tol["concentration_hhi"],
         lambda c, x: f"集中度恶化 {x:.3f} > {c:.3f} + {tol['concentration_hhi']}"),
    )
    for key, label, ok_fn, bad_fn in checks:
        champion_value = float(champion_metrics.get(key) or 0.0)
        challenger_value = float(challenger_metrics.get(key) or 0.0)
        if key == "turnover_amount_ratio":
            champion_turnover = float(champion_metrics.get("turnover_amount") or 0.0)
            challenger_turnover = float(challenger_metrics.get("turnover_amount") or 0.0)
            limit = max(champion_turnover, 1.0) * (1.0 + tol["turnover_amount_ratio"])
            passed = challenger_turnover <= limit
            reason = None if passed else (
                f"换手放大 {challenger_turnover:,.0f} > {champion_turnover:,.0f} × "
                f"{1 + tol['turnover_amount_ratio']:.2f}")
            dimensions.append({
                "key": key, "label": label, "champion": champion_turnover,
                "challenger": challenger_turnover, "passed": passed, "reason": reason,
            })
            continue
        passed = bool(ok_fn(champion_value, challenger_value))
        dimensions.append({
            "key": key, "label": label, "champion": champion_value,
            "challenger": challenger_value, "passed": passed,
            "reason": None if passed else bad_fn(champion_value, challenger_value),
        })
    promotable = all(item["passed"] for item in dimensions)
    return {
        "promotable": promotable,
        "dimensions": dimensions,
        "failed": [item["label"] for item in dimensions if not item["passed"]],
        "version": STRATEGY_CHAMPION_VERSION,
    }


def _latest(conn, strategy_id: str, role: str, statuses: tuple[str, ...] = ()):
    placeholders = ",".join("?" for _ in statuses) if statuses else "''"
    row = conn.execute(
        f"""SELECT * FROM strategy_champion_versions
             WHERE strategy_id=? AND role=? AND status IN ({placeholders})
             ORDER BY id DESC LIMIT 1""",
        (str(strategy_id), role, *statuses),
    ).fetchone()
    return dict(row) if row is not None else None


def open_challenger(
    conn,
    strategy_id: str,
    params: Mapping[str, Any],
    *,
    source: str = "evolution",
    evidence_count: int | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """为策略开启一个影子 Challenger（不改变运行行为）。

    参数先经策略进化画像钳制（bounded），再落库为 shadow 版本。
    """
    import evolution_profiles as EP

    ensure_schema(conn)
    resolved = EP.evolution_profile_for(strategy_id)
    clamped = EP.clamp_to_profile(strategy_id, dict(params or {}))
    moment = (now or dt.datetime.now()).isoformat(timespec="seconds")
    champion = _latest(conn, strategy_id, ROLE_CHAMPION,
                       (STATUS_PROMOTED, "active", "init"))
    cursor = conn.execute(
        """INSERT INTO strategy_champion_versions(
               strategy_id,role,params,base_version_id,source,status,proposed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (str(strategy_id), ROLE_CHALLENGER, _json_dumps(clamped),
         champion["id"] if champion else None, str(source), STATUS_SHADOW, moment, moment),
    )
    conn.commit()
    return {
        "challenger_id": int(cursor.lastrowid),
        "strategy_id": str(strategy_id),
        "status": STATUS_SHADOW,
        "params": clamped,
        "profile": resolved["label"],
        "version": STRATEGY_CHAMPION_VERSION,
    }


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    import json

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def evaluate_challenger(conn, strategy_id: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """评估最新的 shadow Challenger；失败自动回滚，成功等待显式晋升。

    时钟显式传入（生产为真实时间），评估窗与对照窗都相对该时钟计算。
    """
    moment = now or dt.datetime.now()
    challenger = _latest(conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if challenger is None:
        return {"evaluated": False, "reason": "该策略没有处于 shadow 的 Challenger"}
    proposed_at = str(challenger["proposed_at"])[:19].replace("T", " ")
    now_text = moment.isoformat(timespec="seconds")
    proposed_dt = dt.datetime.fromisoformat(proposed_at)
    window = moment - proposed_dt
    if window.total_seconds() < 3600:
        return {
            "evaluated": False, "reason": "影子观察窗不足 1 小时，暂不评估",
            "challenger_id": int(challenger["id"]),
        }
    champion_window_start = (proposed_dt - window).isoformat(timespec="seconds")
    challenger_metrics = collect_ledger_metrics(conn, strategy_id, proposed_at, now_text)
    champion_metrics = collect_ledger_metrics(
        conn, strategy_id, champion_window_start, proposed_at)
    decision = compare_for_promotion(champion_metrics, challenger_metrics)
    status = STATUS_PROMOTED if decision["promotable"] else STATUS_ROLLED_BACK
    conn.execute(
        """UPDATE strategy_champion_versions
              SET status=?,evaluated_at=?,metrics=?,decision=? WHERE id=?""",
        (status, now_text, _json_dumps(challenger_metrics), _json_dumps(decision),
         int(challenger["id"])),
    )
    conn.commit()
    return {
        "evaluated": True,
        "challenger_id": int(challenger["id"]),
        "decision": decision,
        "challenger_metrics": challenger_metrics,
        "champion_metrics": champion_metrics,
        "status": status,
        "version": STRATEGY_CHAMPION_VERSION,
    }


def promote_challenger(conn, strategy_id: str, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """把通过晋升门禁的 Challenger 转正为新 Champion。"""
    challenger = _latest(conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if challenger is None:
        return {"promoted": False, "reason": "没有可晋升的 shadow Challenger"}
    evaluation = evaluate_challenger(conn, strategy_id, now=now)
    if not evaluation.get("evaluated"):
        return {"promoted": False, "reason": evaluation.get("reason")}
    decision = evaluation["decision"]
    if not decision.get("promotable"):
        return {
            "promoted": False,
            "reason": "晋升门禁未通过：" + "；".join(decision.get("failed") or []),
            "decision": decision,
        }
    now = _now()
    conn.execute(
        "UPDATE strategy_champion_versions SET status='superseded' WHERE strategy_id=? AND role=?",
        (str(strategy_id), ROLE_CHAMPION),
    )
    conn.execute(
        "UPDATE strategy_champion_versions SET status=? WHERE id=?",
        (STATUS_PROMOTED, int(challenger["id"])),
    )
    conn.execute(
        """INSERT INTO strategy_champion_versions(
               strategy_id,role,params,base_version_id,source,status,proposed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (str(strategy_id), ROLE_CHAMPION, challenger["params"], int(challenger["id"]),
         "promotion", STATUS_PROMOTED, now, now),
    )
    conn.commit()
    return {
        "promoted": True,
        "strategy_id": str(strategy_id),
        "champion_version_id": int(conn.execute(
            "SELECT id FROM strategy_champion_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]),
        "params": _json_loads(challenger["params"]),
        "decision": decision,
        "version": STRATEGY_CHAMPION_VERSION,
    }


def rollback_challenger(conn, strategy_id: str, reason: str = "manual_rollback") -> dict[str, Any]:
    """丢弃 Challenger，策略保持 Champion 参数（自动回滚也走这里）。"""
    challenger = _latest(conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if challenger is None:
        return {"rolled_back": False, "reason": "没有可回滚的 shadow Challenger"}
    conn.execute(
        """UPDATE strategy_champion_versions SET status=?,decision=? WHERE id=?""",
        (STATUS_ROLLED_BACK,
         _json_dumps({"rolled_back": True, "reason": str(reason)[:200]}),
         int(challenger["id"])),
    )
    conn.commit()
    return {
        "rolled_back": True,
        "strategy_id": str(strategy_id),
        "challenger_id": int(challenger["id"]),
        "reason": str(reason)[:200],
        "version": STRATEGY_CHAMPION_VERSION,
    }
