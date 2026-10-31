"""Execute an explicitly authorized daily paper-only allocation policy.

This does not approve shadow evolution proposals. It writes explicit, versioned
weights under a separately authorized policy, on the caller's transaction.

v2（2026-10-25）：起点从"等权"改为"当前权重"。三策略名册的初始预算是
20% / 50% / 30%，等权锚点会把稳健的 50% 每天往下拽，且旧边界 15%–30% 让
50% 直接不可表示（`lower > upper` 会抛错，进而拖垮入场计划）。现在：

- 目标 = 当前权重 + (该策略收益 − 平均收益)，再投影到"每日最多移动
  2 个百分点"和 [5%, 60%] 的绝对边界（上限与 `adaptive_engine` 的
  `max_strategy_weight_pct` 默认值一致）；
- 边界或权重不合法时 `target_weights` 仍抛 `ValueError`（纯函数契约不变），
  但 `rebalance` 会把它转成 ``infeasible_current_weights`` 状态返回，
  绝不因为一个外来声明而炸掉入场计划；
- `POLICY_ID` 升到 v2：v1 的授权（内嵌旧规则）不再匹配，策略进入
  ``not_authorized`` 而不是按新数学静默执行，重新授权必须由人显式完成。
"""
import copy
import datetime as dt
import hashlib
import json
import math

import evolution_apply as E
from adaptive_common import json_text as _json
import paper_portfolio_read_model as P
import paper_repository as R

POLICY_ID = "authorized-daily-performance-v2"
POLICY_RULES = {"min_sell_samples": 3, "min_weight_pct": 5.0,
                "max_weight_pct": 60.0, "max_daily_step_pct": 2.0}


def target_weights(current, returns):
    """Project a return-sensitive target onto daily step and absolute limits.

    目标以**当前权重**为锚点（不是等权）：20/50/30 这类非等权基线在收益
    相同的一天不会被强行拉回等权，只有在收益差超过每日步长时才移动。
    """
    if set(current) != set(returns) or not current:
        raise ValueError("complete strategy set required")
    if any(not math.isfinite(float(v)) for v in (*current.values(), *returns.values())):
        raise ValueError("finite weights and returns required")
    if abs(sum(current.values()) - 100) > 0.01:
        raise ValueError("current weights must sum to 100")
    floor = float(POLICY_RULES["min_weight_pct"])
    ceiling = float(POLICY_RULES["max_weight_pct"])
    step = float(POLICY_RULES["max_daily_step_pct"])
    if any(v < floor - 1e-9 or v > ceiling + 1e-9 for v in current.values()):
        raise ValueError("current weights outside policy bounds")
    lower = {k: max(floor, v - step) for k, v in current.items()}
    upper = {k: min(ceiling, v + step) for k, v in current.items()}
    if any(lower[k] > upper[k] for k in current) or sum(lower.values()) > 100 or sum(upper.values()) < 100:
        raise ValueError("current weights incompatible with policy bounds")
    mean = sum(returns.values()) / len(returns)
    targets = {k: current[k] + max(-10.0, min(10.0, returns[k] - mean))
               for k in current}
    left, right = -100.0, 100.0
    for _ in range(60):
        shift = (left + right) / 2
        total = sum(max(lower[k], min(upper[k], targets[k] + shift)) for k in current)
        if total < 100:
            left = shift
        else:
            right = shift
    units = {k: int(math.floor(max(lower[k], min(upper[k], targets[k] + right)) * 100 + 1e-8))
             for k in current}
    for k in sorted(current, key=lambda k: (-(targets[k] + right - units[k] / 100), k)):
        if sum(units.values()) >= 10000:
            break
        if units[k] + 1 <= round(upper[k] * 100):
            units[k] += 1
    if sum(units.values()) != 10000:
        raise ValueError("unable to produce bounded exact weights")
    return {k: units[k] / 100 for k in sorted(units)}


def rebalance(conn, *, cycle_id, decision_at, account_ids=None):
    """Daily evaluation using only previous-day bounded verified execution.

    ``account_ids`` 是本周期**执行参与者**。给了就只管理这些账户：退出独立资金
    账户（retiring）的旧策略仍挂在同一周期账本上，它们的权重已经不再参与分配，
    却会因为"必须覆盖周期内全部账户"的旧口径而把三个正式策略的权重一起稀释。
    不给时保持旧行为（整周期），兼容既有调用与测试。
    """
    instant = dt.datetime.fromisoformat(str(decision_at))
    if instant.tzinfo is None:
        raise ValueError("aware decision timestamp required")
    instant = instant.astimezone(E.TZ)
    day = instant.date().isoformat()
    cycle = conn.execute("SELECT status FROM paper_cycles WHERE id=?", (cycle_id,)).fetchone()
    active = conn.execute("SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
                          " ORDER BY id DESC LIMIT 1").fetchone()
    if not cycle or cycle["status"] != "running" or not active or int(active["id"]) != int(cycle_id):
        return {"status": "inactive"}
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_accounts WHERE cycle_id=? ORDER BY id", (cycle_id,))]
    if account_ids is not None:
        scope = {str(item) for item in account_ids}
        rows = [row for row in rows if str(row["id"]) in scope]
        if not rows:
            return {"status": "no_participant_in_cycle"}
    if not rows or any(r["status"] != "running" for r in rows):
        return {"status": "inactive"}
    params = {r["id"]: E.load_account_params(r) for r in rows}
    policies = [p.get("adaptive_allocation_policy") for p in params.values()]
    policy = policies[0]
    if not isinstance(policy, dict) or any(p != policy for p in policies):
        return {"status": "not_authorized"}
    if (policy.get("id") != POLICY_ID or policy.get("status") != "active"
            or policy.get("approved_by") != "user_delegated_design"
            or any(policy.get(k) != v for k, v in POLICY_RULES.items())):
        return {"status": "not_authorized"}
    effective = dt.date.fromisoformat(policy["effective_date"])
    approved = dt.datetime.fromisoformat(policy["approved_at"])
    if approved.tzinfo is None or approved > instant or instant.date() < effective:
        return {"status": "not_effective"}
    allocations = {k: p.get("adaptive_allocation") or {} for k, p in params.items()}
    if all(a.get("policy_id") == POLICY_ID and a.get("evaluated_date") == day
           for a in allocations.values()):
        return {"status": "already_evaluated"}
    if any(a.get("status") != "active" for a in allocations.values()):
        return {"status": "allocation_unavailable"}
    context = P.PortfolioReadContext(int(cycle_id), instant.date() - dt.timedelta(days=1))
    returns, evidence = {}, {}
    for account in params:
        initial = P.cycle_initial(conn, context, account)
        pnl, status = P.realized_pnl(conn, context, account_id=account)
        fills, _, proof = P.sell_fills(conn, context, account)
        samples = {int(f["order_id"]) for f in fills}
        if (status != P.STATUS_VERIFIED or pnl is None or not initial or initial <= 0
                or not proof or len(samples) < policy["min_sell_samples"]):
            return {"status": "waiting_evidence", "account_id": account}
        returns[account] = 100 * pnl / initial
        evidence[account] = {"initial_cash": initial, "realized_pnl": pnl,
                             "sell_order_ids": sorted(samples), "return_pct": returns[account]}
    current = {k: float(a["weight_pct"]) for k, a in allocations.items()}
    try:
        weights = target_weights(current, returns)
    except ValueError as exc:
        # 外来声明（例如名册初始 20/50/30，或别的通道写入的历史值）不合法时
        # 只停止本策略，不让入场计划跟着失败。
        return {"status": "infeasible_current_weights", "reason": str(exc),
                "current_weights": current}
    fingerprint = hashlib.sha256(json.dumps(
        {"cycle_id": cycle_id, "asof_day": context.asof_day.isoformat(), "evidence": evidence},
        sort_keys=True, allow_nan=False).encode()).hexdigest()
    # AI 只解释既有分配结论；ADVISORY 下返回的 weights 保持原样。
    import ai_advice_service as ADV
    ADV.consume_safe(
        conn, kind="allocation", decision_id=f"allocation:{cycle_id}:{day}:{fingerprint}",
        strategy_id="portfolio", code=f"cycle:{cycle_id}", rule=None,
        exact_identity={"cycle_id": int(cycle_id), "policy_id": POLICY_ID},
        current=current, proposed=current, participants=tuple(params),
        min_pct=float(policy["min_weight_pct"]), max_pct=float(policy["max_weight_pct"]),
        cycle_id=cycle_id, now=instant.isoformat())
    for row in rows:
        account = row["id"]
        updated = copy.deepcopy(params[account])
        updated["adaptive_allocation_previous"] = allocations[account]
        updated["adaptive_allocation"] = {
            "weight_pct": weights[account], "status": "active", "effective_date": day,
            "applied_at": instant.isoformat(), "evaluated_date": day,
            "policy_id": POLICY_ID, "approved_by": policy["approved_by"],
            "source": "authorized_policy_execution", "evidence_asof": context.asof_day.isoformat(),
            "evidence_fingerprint": fingerprint,
        }
        E.write_account_params(conn, account, updated, updated_at=instant.isoformat())
        E.append_parameter_snapshot(conn, row, updated,
                                     reason=f"dynamic allocation policy={POLICY_ID}",
                                     created_at=instant.isoformat())
    R.audit(conn, None, "dynamic_allocation_evaluated", _json({
        "cycle_id": cycle_id, "policy_id": POLICY_ID, "weights_before": current,
        "weights_after": weights, "evidence": evidence, "fingerprint": fingerprint,
    }), instant.isoformat())
    return {"status": "evaluated", "weights_pct": weights, "evidence_fingerprint": fingerprint}
