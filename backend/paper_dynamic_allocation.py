"""Execute an explicitly authorized daily paper-only allocation policy.

This does not approve shadow evolution proposals. It writes explicit, versioned
weights under a separately authorized policy, on the caller's transaction.
"""
import copy
import datetime as dt
import hashlib
import json
import math

import evolution_apply as E
import paper_portfolio_read_model as P
import paper_repository as R

POLICY_ID = "authorized-daily-performance-v1"
POLICY_RULES = {"min_sell_samples": 3, "min_weight_pct": 15.0,
                "max_weight_pct": 30.0, "max_daily_step_pct": 2.0}


def target_weights(current, returns):
    """Project a return-sensitive target onto daily step and absolute limits."""
    if set(current) != set(returns) or not current:
        raise ValueError("complete strategy set required")
    if any(not math.isfinite(float(v)) for v in (*current.values(), *returns.values())):
        raise ValueError("finite weights and returns required")
    if abs(sum(current.values()) - 100) > 0.01:
        raise ValueError("current weights must sum to 100")
    lower = {k: max(15.0, v - 2.0) for k, v in current.items()}
    upper = {k: min(30.0, v + 2.0) for k, v in current.items()}
    if any(lower[k] > upper[k] for k in current) or sum(lower.values()) > 100 or sum(upper.values()) < 100:
        raise ValueError("current weights incompatible with policy bounds")
    mean = sum(returns.values()) / len(returns)
    targets = {k: 100 / len(current) + max(-10.0, min(10.0, returns[k] - mean))
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


def rebalance(conn, *, cycle_id, decision_at):
    """Daily evaluation using only previous-day bounded verified execution."""
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
    if not rows or any(r["status"] != "running" for r in rows):
        return {"status": "inactive"}
    params = {r["id"]: E._account_params(r) for r in rows}
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
        initial = P._cycle_initial(conn, context, account)
        pnl, status = P.realized_pnl(conn, context, account_id=account)
        fills, _, proof = P._sell_fills(conn, context, account)
        samples = {int(f["order_id"]) for f in fills}
        if (status != P.STATUS_VERIFIED or pnl is None or not initial or initial <= 0
                or not proof or len(samples) < policy["min_sell_samples"]):
            return {"status": "waiting_evidence", "account_id": account}
        returns[account] = 100 * pnl / initial
        evidence[account] = {"initial_cash": initial, "realized_pnl": pnl,
                             "sell_order_ids": sorted(samples), "return_pct": returns[account]}
    current = {k: float(a["weight_pct"]) for k, a in allocations.items()}
    weights = target_weights(current, returns)
    fingerprint = hashlib.sha256(json.dumps(
        {"cycle_id": cycle_id, "asof_day": context.asof_day.isoformat(), "evidence": evidence},
        sort_keys=True, allow_nan=False).encode()).hexdigest()
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
        E._write_account_params(conn, account, updated, updated_at=instant.isoformat())
        E._append_parameter_snapshot(conn, row, updated,
                                     reason=f"dynamic allocation policy={POLICY_ID}",
                                     created_at=instant.isoformat())
    R.audit(conn, None, "dynamic_allocation_evaluated", E._json({
        "cycle_id": cycle_id, "policy_id": POLICY_ID, "weights_before": current,
        "weights_after": weights, "evidence": evidence, "fingerprint": fingerprint,
    }), instant.isoformat())
    return {"status": "evaluated", "weights_pct": weights, "evidence_fingerprint": fingerprint}
