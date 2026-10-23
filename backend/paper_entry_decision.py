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


#: 一手金额不足一手时，可等待预算释放的绑定约束（其余属结构性不可满足）。
WAITABLE_BINDING_CONSTRAINTS = ("cash", "weight", "exposure", "industry")


def assess_order_economics(*, reasons, qty, fill_price, price, pending_cash,
                           shared_cash, minimum_order_amount, lot_size,
                           economics_policy, min_cost_edge, sizing,
                           fee_estimator, sell_price_estimator, num):
    """把最终数量换算成金额与费用，并判定金额/成本侧能否下单。

    ``reasons`` 与 ``sizing`` 只读：本函数返回它自己新增的拒绝原因（硬拒绝与
    软约束分开）以及需要写回的 sizing 字段。软约束进替补队列、硬拒绝终态
    拒绝的口径由调用方按返回的分组决定；这里不读时钟、不访问市场、不写库。
    """
    _num = num
    amount = qty * fill_price
    fees = fee_estimator(amount, "buy")
    updates = {
        "one_lot_amount": round(lot_size * fill_price, 2),
        "one_lot_fee": round(fee_estimator(lot_size * fill_price, "buy"), 2),
        "cash_available": round(shared_cash, 2),
        "qty_final": int(qty),
    }
    rejected = []
    soft = []
    if qty < lot_size and not reasons:
        limits = sizing.get("constraint_shares") or {}
        binding = [name for name, value in limits.items() if _num(value) < lot_size]
        if not binding:
            binding = list(sizing.get("binding_constraints") or [])
        labels = {
            "risk": "单笔止损预算", "weight": "单票仓位上限",
            "exposure": "策略总仓位上限", "industry": "行业仓位上限",
            "cash": "可用现金",
        }
        root = labels.get(binding[0], "风险约束") if binding else "风险约束"
        root_limit = _num(limits.get(binding[0]), 0) if binding else 0
        # binding 为 risk 时是结构性不可满足：高价股 × 紧止损使 by_risk 永远
        # 不足一手，转等待池只会每天空转占位到 24h 归档。预算侧约束才可等待。
        if set(binding) == {"risk"}:
            rejected.append(
                f"单笔止损预算结构性不足：一手约 ¥{lot_size * fill_price:,.2f}，"
                f"风险预算最多 {root_limit:.0f} 股，转终态拒绝"
            )
        else:
            rejected.append(
                f"一手约 ¥{lot_size * fill_price:,.2f}，模型可买 {int(qty)} 股；金额约束仅影响下单规模，"
                f"当前转入等待池（参考限制：{root}，最多 {root_limit:.0f} 股）"
            )
            soft.append(rejected[-1])
    # 尘埃单：金额低于当前周期单席可表达金额且其余闸门全过时，若放行就会用
    # 小仓占用稀缺席位；归为软拦截，预算释放后按全尺寸复核。
    if qty >= lot_size and amount < minimum_order_amount and not reasons:
        dust_reason = (
            f"低于动态最小建仓金额 ¥{minimum_order_amount:,.0f}（当前 ¥{amount:,.0f}）；"
            "席位稀缺时不建尘埃仓，候选保留在等待池，预算释放后按全尺寸复核"
        )
        rejected.append(dust_reason)
        soft.append(dust_reason)
    cash_after_pending = shared_cash - pending_cash
    # 现金为零是真实安全否决；余额只是小于本单则属软性不足，仍可重试——
    # 2026-08 审计发现这条曾被硬分类，导致只差排队的候选被永久拒绝。
    if cash_after_pending <= 1e-6:
        rejected.append(
            f"共享资金池暂无可用现金（已有待成交买单预占 ¥{pending_cash:,.2f}）"
            if pending_cash > 0 else "共享资金池暂无可用现金"
        )
    elif amount + fees > cash_after_pending + 1e-6:
        cash_reason = (
            f"共享资金池可用现金不足（已有待成交买单预占 ¥{pending_cash:,.2f}）"
            if pending_cash > 0 else "共享资金池可用现金不足"
        )
        rejected.append(cash_reason)
        soft.append(cash_reason)
    if amount and fees / amount > 0.003:
        cost_reason = "单笔最低佣金占比过高，暂不下单，候选保留在等待池复核"
        rejected.append(cost_reason)
        soft.append(cost_reason)
    if economics_policy and amount > 0:
        min_effective_amount = _num(economics_policy.get("min_effective_order_amount"), 0.0)
        expected_edge = max(
            _num(economics_policy.get("min_expected_edge_pct"), 0.0),
            _num(min_cost_edge, 0.0),
        )
        # 用实际买入佣金 + 预估卖出佣金/印花税 + 双向滑点做保守成本门槛；
        # 不把未实现收益当现金，只判断该委托是否值得发生。
        expected_profit = amount * expected_edge
        sell_fees = fee_estimator(amount, "sell")
        slippage_cost = qty * (
            abs(fill_price - price)
            + abs(sell_price_estimator(price, "sell") - price)
        )
        estimated_round_trip_cost = fees + sell_fees + slippage_cost
        updates["minimum_effective_order_amount"] = min_effective_amount
        updates["expected_edge_pct"] = round(expected_edge * 100, 2)
        updates["expected_profit_amount"] = round(expected_profit, 2)
        updates["estimated_round_trip_cost"] = round(estimated_round_trip_cost, 2)
        if min_effective_amount and amount < min_effective_amount:
            min_amount_reason = (
                f"短线有效委托至少 ¥{min_effective_amount:,.0f}，当前 ¥{amount:,.0f}；"
                "金额约束仅用于委托 sizing，候选保留在等待池复核"
            )
            rejected.append(min_amount_reason)
            soft.append(min_amount_reason)
        elif expected_profit < estimated_round_trip_cost * 1.25:
            edge_reason = (
                f"短线预期毛收益 ¥{expected_profit:,.2f} 未覆盖估算往返成本 "
                f"¥{estimated_round_trip_cost:,.2f}，候选保留在等待池复核"
            )
            rejected.append(edge_reason)
            soft.append(edge_reason)
    return {"amount": amount, "fees": fees, "sizing": updates,
            "reasons": rejected, "soft_reasons": soft}
