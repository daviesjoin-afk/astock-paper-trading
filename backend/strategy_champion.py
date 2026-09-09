# -*- coding: utf-8 -*-
r"""策略 Champion / Challenger 版本管理（PR：champion challenger strategy versions）。

每个策略维护 Champion（当前运行参数）与至多一个 Challenger（影子候选）。

诚实语义（重要）：Challenger 开启时其参数**真正写入运行参数仓**
（`self_evolution.adjust_strategy_params`，由 dual_ai_tuner / evolution_apply
消费），也就是说影子窗内的订单就是 Challenger 参数实际产生的——这是纯模拟
盘，"影子"意味着失败会自动回滚，而不是假装参数没生效：

- **开启**：候选参数先过策略进化画像的**完整校验**（可调清单/锁定/步长/
  单调/最小证据量，fail-closed），通过后写入参数仓并登记 shadow 行
  （含 Champion 参数快照，供回滚）。同一策略已有 shadow Challenger 时拒绝
  重复开启。
- **评估**：影子窗（proposed_at → now，Challenger 参数实际运行）对照等长的
  前段（Champion 参数运行），五维指标从模拟账本推导。**失败自动回滚**：
  恢复 Champion 参数并标记 rolled_back；通过则置 ready，等待显式晋升
  （查看/轮询页面绝不触发晋升）。
- **晋升**：仅接受 ready 状态；参数已在运行，补记 Champion 版本行。
- **回滚**：恢复 Champion 参数快照。

指标口径：收益分母 = 窗口内买入金额 + **窗口期初持仓市值**（已含隔窗持仓
的成本基础）；成交率分子分母用同一订单队列（窗口内创建或成交的买单）。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Mapping

__all__ = [
    "STRATEGY_CHAMPION_VERSION",
    "ROLE_CHAMPION",
    "ROLE_CHALLENGER",
    "STATUS_SHADOW",
    "STATUS_READY",
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

STRATEGY_CHAMPION_VERSION = "strategy-champion-v2"
ROLE_CHAMPION = "champion"
ROLE_CHALLENGER = "challenger"
STATUS_SHADOW = "shadow"
STATUS_READY = "ready"
STATUS_PROMOTED = "promoted"
STATUS_ROLLED_BACK = "rolled_back"

# 风险维度的容差：恶化在容差内视为"不恶化"（相对/绝对按维度定义）。
PROMOTION_TOLERANCE = {
    "max_drawdown_pct": 0.5,        # 允许多 0.5 个百分点的回撤
    "turnover_amount_ratio": 0.15,  # 换手最多高出 15%
    "execution_fill_rate": 5.0,     # 成交率最多低 5 个百分点（0-100 口径）
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
            params TEXT NOT NULL,           -- JSON: 该版本的策略参数
            champion_params TEXT,           -- JSON: 开启时的 Champion 参数快照（回滚用）
            base_version_id INTEGER,        -- 母本（参数仓版本 id）
            shadow_version_id INTEGER,      -- Challenger 在参数仓中的版本 id
            source TEXT NOT NULL,           -- 'evolution' | 'manual' | 'promotion'
            status TEXT NOT NULL,           -- shadow | ready | promoted | rolled_back | superseded
            evidence_count INTEGER,
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


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def collect_ledger_metrics(conn, strategy_id: str, since: str, until: str) -> dict[str, Any]:
    """从模拟账本推导一个时间窗内的五维策略指标（断网可算）。"""
    window_orders = [dict(row) for row in conn.execute(
        """SELECT side,status,code,COALESCE(amount,0) AS amount,
                  COALESCE(realized_pnl,0) AS realized_pnl,executed_at,created_at
             FROM paper_orders
            WHERE account_id=? AND status='filled'
              AND executed_at IS NOT NULL AND executed_at>=? AND executed_at<?
              AND COALESCE(amount,0)>0
            ORDER BY executed_at""",
        (str(strategy_id), str(since), str(until)),
    ).fetchall()]
    # 成交率：分子分母同一队列 —— 窗口内创建 **或** 成交的买单。
    attempts = conn.execute(
        """SELECT COUNT(*) FROM paper_orders
            WHERE account_id=? AND side='buy'
              AND ((created_at>=? AND created_at<?)
                   OR (executed_at IS NOT NULL AND executed_at>=? AND executed_at<?))""",
        (str(strategy_id), str(since), str(until), str(since), str(until)),
    ).fetchone()[0]
    filled_buys = sum(1 for o in window_orders if o["side"] == "buy")
    buy_amount = sum(float(o["amount"]) for o in window_orders if o["side"] == "buy")
    realized = sum(float(o["realized_pnl"]) for o in window_orders if o["side"] == "sell")
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
    # 收益分母 = 窗口内买入 + **窗口期初已持仓的市值**（隔窗持仓的成本基础），
    # 否则"窗口前买入、窗口内卖出"的盈亏会除以接近 0 的分母。
    carried = conn.execute(
        """SELECT COALESCE(SUM(qty*cost),0) FROM paper_positions
            WHERE account_id=? AND entry_date<?""",
        (str(strategy_id), str(since)[:10]),
    ).fetchone()[0]
    carried_value = float(carried or 0.0)
    denom = max(buy_amount + carried_value, 1.0)
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
        "carried_value": round(carried_value, 2),
        "execution_fill_rate": round(
            (filled_buys / attempts * 100.0) if attempts else 0.0, 2),
        "concentration_hhi": round(hhi, 4),
        "filled_orders": len(window_orders),
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
        ("execution_fill_rate", "成交率",
         lambda c, x: x >= c - tol["execution_fill_rate"],
         lambda c, x: f"成交率下降 {x:.2f}% < {c:.2f}% - {tol['execution_fill_rate']}pp"),
        ("concentration_hhi", "集中度",
         lambda c, x: x <= c + tol["concentration_hhi"],
         lambda c, x: f"集中度恶化 {x:.3f} > {c:.3f} + {tol['concentration_hhi']}"),
    )
    for key, label, ok_fn, bad_fn in checks:
        champion_value = float(champion_metrics.get(key) or 0.0)
        challenger_value = float(challenger_metrics.get(key) or 0.0)
        passed = bool(ok_fn(champion_value, challenger_value))
        dimensions.append({
            "key": key, "label": label, "champion": champion_value,
            "challenger": challenger_value, "passed": passed,
            "reason": None if passed else bad_fn(champion_value, challenger_value),
        })
    champion_turnover = float(champion_metrics.get("turnover_amount") or 0.0)
    challenger_turnover = float(challenger_metrics.get("turnover_amount") or 0.0)
    limit = max(champion_turnover, 1.0) * (1.0 + tol["turnover_amount_ratio"])
    turnover_passed = challenger_turnover <= limit
    dimensions.append({
        "key": "turnover_amount_ratio", "label": "换手",
        "champion": champion_turnover, "challenger": challenger_turnover,
        "passed": turnover_passed,
        "reason": None if turnover_passed else (
            f"换手放大 {challenger_turnover:,.0f} > {champion_turnover:,.0f} × "
            f"{1 + tol['turnover_amount_ratio']:.2f}"),
    })
    promotable = all(item["passed"] for item in dimensions)
    return {
        "promotable": promotable,
        "dimensions": dimensions,
        "failed": [item["label"] for item in dimensions if not item["passed"]],
        "version": STRATEGY_CHAMPION_VERSION,
    }


def _latest(paper_conn, strategy_id: str, role: str, statuses: tuple[str, ...]):
    placeholders = ",".join("?" for _ in statuses)
    row = paper_conn.execute(
        f"""SELECT * FROM strategy_champion_versions
             WHERE strategy_id=? AND role=? AND status IN ({placeholders})
             ORDER BY id DESC LIMIT 1""",
        (str(strategy_id), role, *statuses),
    ).fetchone()
    return dict(row) if row is not None else None


def open_challenger(
    paper_conn,
    evo_conn,
    strategy_id: str,
    params: Mapping[str, Any],
    *,
    source: str = "evolution",
    evidence_count: int | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """开启影子 Challenger：完整画像校验 → 参数真正写入运行仓 → 登记 shadow。

    同一策略已有 shadow Challenger 时拒绝（先评估/回滚再开新的）。
    """
    import evolution_profiles as EP
    import self_evolution as SE

    ensure_schema(paper_conn)
    if _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,)) is not None:
        return {
            "opened": False,
            "reason": "该策略已有 shadow Challenger，请先评估或回滚",
        }
    current = SE.get_strategy_params(evo_conn, strategy_id)
    check = EP.validate_strategy_adjustment(
        strategy_id, current["params"], dict(params or {}),
        evidence_count=evidence_count,
    )
    if not check["allowed"]:
        return {
            "opened": False, "violations": check["violations"],
            "reason": "候选参数未通过策略进化画像校验（fail-closed）",
        }
    applied = SE.adjust_strategy_params(
        evo_conn, strategy_id, check["adjusted"],
        reason=f"challenger_shadow:{source}", source="challenger_shadow",
        evidence_count=evidence_count,
    )
    if not applied.get("adjusted"):
        return {
            "opened": False,
            "reason": "参数仓未接受候选参数：" + str(applied.get("reason")
                                              or applied.get("violations")),
        }
    moment = (now or dt.datetime.now()).isoformat(timespec="seconds")
    cursor = paper_conn.execute(
        """INSERT INTO strategy_champion_versions(
               strategy_id,role,params,champion_params,base_version_id,
               shadow_version_id,source,status,evidence_count,proposed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (str(strategy_id), ROLE_CHALLENGER, _json_dumps(check["adjusted"]),
         _json_dumps(current["params"]), current["id"],
         applied.get("new_params_id"), str(source), STATUS_SHADOW,
         evidence_count, moment, moment),
    )
    paper_conn.commit()
    return {
        "opened": True,
        "challenger_id": int(cursor.lastrowid),
        "strategy_id": str(strategy_id),
        "status": STATUS_SHADOW,
        "params": check["adjusted"],
        "shadow_version_id": applied.get("new_params_id"),
        "version": STRATEGY_CHAMPION_VERSION,
    }


def _restore_champion_params(evo_conn, strategy_id: str, champion_params: Mapping[str, Any],
                             reason: str) -> dict[str, Any]:
    """把参数仓恢复为 Champion 快照（只调整有差异的可调键）。"""
    import self_evolution as SE

    current = SE.get_strategy_params(evo_conn, strategy_id)
    diffs = {
        key: value for key, value in dict(champion_params or {}).items()
        if key in current["params"] and current["params"].get(key) != value
    }
    if not diffs:
        return {"restored": True, "changed_keys": []}
    applied = SE.adjust_strategy_params(
        evo_conn, strategy_id, diffs,
        reason=reason[:120], source="challenger_rollback",
        evidence_count=999999,  # 回滚是安全方向，不受最小证据量限制
    )
    return {"restored": bool(applied.get("adjusted")), "changed_keys": applied.get("changed_keys")}


def evaluate_challenger(
    paper_conn,
    evo_conn,
    strategy_id: str,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """评估 shadow Challenger；失败自动回滚，通过置 ready 等待显式晋升。

    查看/轮询视图调用本函数也绝不触发晋升——晋升只走显式 promote。
    """
    challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_SHADOW,))
    if challenger is None:
        ready = _latest(paper_conn, strategy_id, ROLE_CHALLENGER, (STATUS_READY,))
        if ready is not None:
            return {
                "evaluated": False, "status": STATUS_READY,
                "reason": "已有通过门禁、待显式晋升的 Challenger",
                "challenger_id": int(ready["id"]),
            }
        return {"evaluated": False, "reason": "该策略没有处于 shadow 的 Challenger"}
    moment = now or dt.datetime.now()
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
    challenger_metrics = collect_ledger_metrics(paper_conn, strategy_id, proposed_at, now_text)
    champion_metrics = collect_ledger_metrics(
        paper_conn, strategy_id, champion_window_start, proposed_at)
    decision = compare_for_promotion(champion_metrics, challenger_metrics)
    if decision["promotable"]:
        status = STATUS_READY  # 晋升由显式 promote 完成；这里绝不改状态为 promoted。
    else:
        status = STATUS_ROLLED_BACK
        restore = _restore_champion_params(
            evo_conn, strategy_id,
            _json_loads(challenger.get("champion_params")),
            f"challenger 失败自动回滚：{'；'.join(decision.get('failed') or [])}",
        )
        decision["rollback"] = restore
    paper_conn.execute(
        """UPDATE strategy_champion_versions
              SET status=?,evaluated_at=?,metrics=?,decision=? WHERE id=?""",
        (status, now_text, _json_dumps(challenger_metrics), _json_dumps(decision),
         int(challenger["id"])),
    )
    paper_conn.commit()
    return {
        "evaluated": True,
        "challenger_id": int(challenger["id"]),
        "decision": decision,
        "challenger_metrics": challenger_metrics,
        "champion_metrics": champion_metrics,
        "status": status,
        "version": STRATEGY_CHAMPION_VERSION,
    }


def promote_challenger(paper_conn, evo_conn, strategy_id: str,
                       *, now: dt.datetime | None = None) -> dict[str, Any]:
    """把通过门禁（ready）的 Challenger 转正为新 Champion。"""
    challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER,
                         (STATUS_SHADOW, STATUS_READY))
    if challenger is None:
        return {"promoted": False, "reason": "没有可晋升的 Challenger"}
    if challenger["status"] == STATUS_SHADOW:
        evaluation = evaluate_challenger(paper_conn, evo_conn, strategy_id, now=now)
        if not evaluation.get("evaluated"):
            return {"promoted": False, "reason": evaluation.get("reason")}
        challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER,
                             (STATUS_SHADOW, STATUS_READY))
        if challenger is None or challenger["status"] != STATUS_READY:
            return {
                "promoted": False,
                "reason": "晋升门禁未通过，Challenger 已自动回滚",
                "decision": evaluation.get("decision"),
            }
    now_text = (now or dt.datetime.now()).isoformat(timespec="seconds")
    paper_conn.execute(
        "UPDATE strategy_champion_versions SET status='superseded' WHERE strategy_id=? AND role=?",
        (str(strategy_id), ROLE_CHAMPION),
    )
    paper_conn.execute(
        "UPDATE strategy_champion_versions SET status=? WHERE id=?",
        (STATUS_PROMOTED, int(challenger["id"])),
    )
    paper_conn.execute(
        """INSERT INTO strategy_champion_versions(
               strategy_id,role,params,base_version_id,source,status,proposed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (str(strategy_id), ROLE_CHAMPION, challenger["params"],
         int(challenger["id"]), "promotion", STATUS_PROMOTED, now_text, now_text),
    )
    paper_conn.commit()
    return {
        "promoted": True,
        "strategy_id": str(strategy_id),
        "challenger_id": int(challenger["id"]),
        "params": _json_loads(challenger["params"]),
        "version": STRATEGY_CHAMPION_VERSION,
    }


def rollback_challenger(paper_conn, evo_conn, strategy_id: str,
                        reason: str = "manual_rollback") -> dict[str, Any]:
    """丢弃 Challenger 并把参数仓恢复为 Champion 快照。"""
    challenger = _latest(paper_conn, strategy_id, ROLE_CHALLENGER,
                         (STATUS_SHADOW, STATUS_READY))
    if challenger is None:
        return {"rolled_back": False, "reason": "没有可回滚的 Challenger"}
    restore = _restore_champion_params(
        evo_conn, strategy_id, _json_loads(challenger.get("champion_params")),
        f"challenger 回滚：{reason}")
    paper_conn.execute(
        """UPDATE strategy_champion_versions SET status=?,decision=? WHERE id=?""",
        (STATUS_ROLLED_BACK,
         _json_dumps({"rolled_back": True, "reason": str(reason)[:200],
                      "restore": restore}),
         int(challenger["id"])),
    )
    paper_conn.commit()
    return {
        "rolled_back": True,
        "strategy_id": str(strategy_id),
        "challenger_id": int(challenger["id"]),
        "restore": restore,
        "reason": str(reason)[:200],
        "version": STRATEGY_CHAMPION_VERSION,
    }
