"""Pure entry decisions over explicitly supplied policy and market facts."""


def prepare_entry_slice(*, payload, account, qty, asof_day, continuation, existing_plan, lot_size, plan_builder):
    """Calculate one slice without changing the signal, account or ledger."""
    try:
        slice_count = int(payload.get("entry_slice_policy") or account.get("entry_slices") or 1)
    except (TypeError, ValueError):
        slice_count = 1
    if continuation and existing_plan:
        slice_count = max(slice_count, len(existing_plan))
    slice_count = max(1, min(slice_count, 5))
    if slice_count <= 1:
        return qty, None, {}, None
    state = dict(payload.get("entry_slices") or {})
    if not state.get("plan"):
        state = {"plan": plan_builder(int(qty or 0), slice_count, lot_size=lot_size),
                 "filled": 0, "day": str(asof_day)[:10], "slices": slice_count}
    plan = [int(item) for item in (state.get("plan") or [])]
    filled = max(0, int(state.get("filled") or 0))
    sizing = {}
    if plan and filled < len(plan):
        target = plan[filled]
        clamped = (min(int(qty or 0), target) // lot_size) * lot_size
        sizing.update(entry_slice_target_qty=target, entry_slice_index=filled + 1, entry_slice_total=len(plan))
        if clamped > 0:
            qty = clamped
    return qty, state, sizing, {"plan": plan, "filled": filled, "slices": slice_count}


def chase_entry_gate(pick, quote, market, entry_model, q, execution_quote, *, policy, num, limit_pct):
    """Controlled chase gate for TQ and the sector-hot acceleration lane."""
    _num = num
    pct = _num(quote.get("pct"), -999)
    result = {"allowed": False, "risk_scale": 1.0, "reason": None, "mode": "normal"}
    # 追高通道由执行策略声明（chase_lane），不再按账户 ID 分支：
    # momentum=短线接力，sector_hot=板块热点加速，其余一律不追高。
    if policy.chase_lane not in ("momentum", "sector_hot"):
        result["reason"] = policy.chase_rejection
        return result
    sector_hot = (
        policy.chase_lane == "sector_hot"
        and str(pick.get("candidate_status") or "") in {
            "ths_hot_lane", "concept_expansion_lane", "sector_surge_lane", "hot_leader_watch",
        }
        and pct >= 7.0
    )
    if policy.chase_lane != "momentum" and not sector_hot:
        result["reason"] = policy.chase_rejection
        return result
    result["required"] = bool(sector_hot)
    if sector_hot:
        heat = pick.get("sector_heat") if isinstance(pick.get("sector_heat"), dict) else {}
        rank = int(_num(heat.get("rank"), 999))
        heat_pct = _num(heat.get("pct"), -999.0)
        overheat = (entry_model.get("overheat_guard") or {}) if isinstance(entry_model, dict) else {}
        if market.get("light") != "green":
            result["reason"] = "热点加速试仓仅在市场绿灯开放"
            return result
        if execution_quote.get("status") != "cross_source_checked":
            result["reason"] = "热点加速试仓必须通过双源实时行情核验"
            return result
        if pct > limit_pct - 0.15:
            result["reason"] = "热点加速已接近涨停，不模拟封板排队成交"
            return result
        if q.get("tier") != "Q1":
            result["reason"] = f"热点加速后市质量 {q.get('tier')}，未达到 Q1"
            return result
        score = _num(entry_model.get("score"))
        if not entry_model.get("passed") or score < 0.80:
            result["reason"] = f"热点加速确认分 {score:.2f} 不足 0.80"
            return result
        if rank > 5 or heat_pct < 2.5:
            result["reason"] = f"热点加速要求板块前5且涨幅≥2.5%，当前第 {rank}、{heat_pct:+.2f}%"
            return result
        main_pct = _num(quote.get("main_pct"), -999)
        vol_ratio = _num(quote.get("vol_ratio"))
        if main_pct < 3.0 or vol_ratio < 2.0:
            result["reason"] = "热点加速所需主力净流入≥3%且量比≥2.0未同时满足"
            return result
        if str(overheat.get("level") or "unknown") not in {"normal", "caution"}:
            result["reason"] = "个股已处于连续加速末端，热点策略不追末板"
            return result
        result.update({
            # This is deliberately smaller than a normal sector position, but
            # not a dust order.  The former 25% scale was multiplied again by
            # the entry-model scale (and, before the sizing fix below, by the
            # market-yellow scale), which regularly rounded a valid hot name
            # down to a single lot.  Half of the independently available
            # sector budget keeps the first hot-leader expression meaningful
            # while preserving headroom for confirmation or rotation.
            "allowed": True, "risk_scale": 0.50, "mode": "板块热点加速试仓",
            "reason": "绿灯、前五热点、双源、Q1、主力与量能同步，允许未触板的50%试仓",
        })
        return result
    if market.get("light") not in ("green", "yellow"):
        result["reason"] = "大盘非绿/黄灯，禁止追买"
        return result
    if execution_quote.get("status") != "cross_source_checked":
        result["reason"] = "追买必须通过双源实时行情核验"
        return result
    # limit_up_threshold 是交易所涨停前的保守触发线（9.5/19.5/29.5），
    # 实时行情通常会显示到 10/20/30%，允许小幅上浮用于行情取整。
    # P3：动量确认模式从 +3.5% 起（与普通入场带上沿一致），消除
    # 3.5%~7% 之间"既不是低位启动、又不按追高管理"的规则模糊区。
    if pct < 3.5 or pct > limit_pct + 0.60:
        result["reason"] = f"涨幅 {pct:+.2f}% 未处于短线动量确认区间（+3.5% 至涨停附近）"
        return result
    # 已触及涨停价位的标的无法区分“封板”与“未封板”：本系统不模拟涨停板
    # 排队成交（ACCOUNT_SPECS 明确声明），因此贴近涨停幅度的报价一律不放行。
    if pct >= limit_pct - 0.15:
        result["reason"] = f"涨幅 {pct:+.2f}% 已触及涨停价位，不模拟封板排队成交"
        return result
    if q.get("tier") != "Q1":
        result["reason"] = f"后市质量 {q.get('tier')}，未达到 Q1"
        return result
    score = _num(entry_model.get("score"))
    if not entry_model.get("passed") or score < 0.78:
        result["reason"] = f"策略确认分 {score:.2f} 不足"
        return result
    main_pct = _num(quote.get("main_pct"), -999)
    vol_ratio = _num(quote.get("vol_ratio"))
    # 强势接力只在强资金、强量能和高质量信号同时成立时追买。
    if main_pct < 2.0 or vol_ratio < 1.5 or _num(pick.get("score")) < 0.20:
        result["reason"] = "接力追买所需资金、量能或候选质量不足"
        return result
    result.update({"allowed": True, "risk_scale": 0.35, "mode": "短线确认追高",
                   "reason": "短线日内做T：主力、量能、Q1 后市和候选质量确认通过"})
    return result
