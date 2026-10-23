# -*- coding: utf-8 -*-
"""普跌后恢复入场的证据判定（纯决策：无 IO、无时钟、无项目 import）。

存在理由
--------
2026-10-23 的诊断确认了两件互相对立的事：一是"午后不接受首次触发"确实挡住了
恐慌后的恢复买入（晋西车轴 13:07 模型 0.756 高于门槛 0.580 仍被拒），二是
直接删掉时段限制等于允许午后无条件追涨。所以这里做的不是"放开午后"，而是
**给恢复入场加一组可检验的前置条件**，并且只对"上午有依据"的候选生效：

    上午有过真实触发/确认（状态机保留的证据）
    + 大盘本身已恢复（当日指数参照，且该参照必须是当日 fresh 值）
    + 个股的弱势能被大盘解释（相对强弱未破下限）
    + 没有个股层面的独立坏消息
    + 主力资金不是持续流出
    + 价格已从跌后低点收复 reclaim_pct
    ⇒ 允许一次小仓位首笔入场；后续加仓仍需新证据

任何一条无法证明，一律 `allowed=False` 并给出分类与缺失项——这个模块的职责
是**拒绝**，不是找理由放行。仓位大小、追高上限、Q级、席位、资金与硬止损仍由
既有链路独立决定，本模块不碰。

输入全部是决策时点已知事实：
    morning   —— ``entry_timing.recovery_watch_evidence`` 的输出
    quote     —— 当轮行情（price/pct/main_pct/quote_at）
    market    —— 当轮市场状态（live_index_pct 与 index_reference）
    negative_news —— 该标的当轮是否命中负面公告/舆情
    policy    —— ``strategy_policies.recovery_entry_policy``
"""
from __future__ import annotations

CLASSIFICATION_MARKET_RECOVERY = "market_selloff_recovery"
CLASSIFICATION_NO_EVIDENCE = "no_morning_evidence"
CLASSIFICATION_MARKET_UNPROVEN = "market_reference_unproven"
CLASSIFICATION_MARKET_NOT_RECOVERED = "market_not_recovered"
CLASSIFICATION_STOCK_WEAKNESS = "stock_specific_weakness"
CLASSIFICATION_NEGATIVE_EVENT = "negative_event"
CLASSIFICATION_OUTFLOW = "persistent_outflow"
CLASSIFICATION_UNPROVEN_FLOW = "unproven_flow"
CLASSIFICATION_FALSE_REBOUND = "false_rebound"
CLASSIFICATION_DISABLED = "recovery_entry_disabled"

MODEL = "recovery_entry_evidence_v1"


def _num(value, default=None):
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def assess(*, morning, quote, market, negative_news, policy):
    """返回恢复入场的判定与全部证据；``allowed`` 只在全部条件成立时为 True。"""
    policy = dict(policy or {})
    quote = dict(quote or {})
    market = dict(market or {})
    morning = dict(morning or {})
    evidence = []
    missing = []
    inputs = {
        "price": _num(quote.get("price")),
        "pct": _num(quote.get("pct")),
        "main_pct": _num(quote.get("main_pct"), _num(quote.get("main_net_pct"))),
        "index_pct": _num(market.get("live_index_pct")),
        "index_status": ((market.get("index_reference") or {}) or {}).get("status"),
        "index_time": market.get("live_index_time"),
        "negative_news": bool(negative_news),
        "trigger_price": _num((morning.get("morning_evidence") or {}).get("trigger_price")),
        "first_trigger_at": (morning.get("morning_evidence") or {}).get("first_trigger_at"),
        "breakdown_at": morning.get("breakdown_at"),
        "low_since_breakdown": _num(morning.get("low_since_breakdown")),
    }

    def verdict(classification, reason, *, allowed=False):
        return {
            "model": MODEL,
            "allowed": bool(allowed),
            "classification": classification,
            "reason": reason,
            "evidence": evidence,
            "missing": missing,
            "inputs": inputs,
        }

    if not policy:
        return verdict(CLASSIFICATION_DISABLED, "该策略未启用跌后恢复入场")

    inner = morning.get("morning_evidence") or {}
    if not morning or not inner.get("trigger_price") or not inner.get("first_trigger_at"):
        return verdict(CLASSIFICATION_NO_EVIDENCE,
                       "缺少上午的触发/确认证据，按全新候选处理，不允许午后入场")
    evidence.append(
        f"上午 {inner.get('first_trigger_at')} 触发于 {_num(inner.get('trigger_price')):.2f}"
        + (f"，{inner.get('confirmed_at')} 已确认" if inner.get("confirmed_at") else "")
        + (f"；失效原因：{morning.get('breakdown_at')} {morning.get('breakdown_reason')}"
           if morning.get("breakdown_at") else "")
    )

    # 1) 指数参照必须是当日 fresh 值——否则"市场恢复"无从证明（与第一批的
    #    相对强弱口径同一条约束：别日指数不得代理当日行情）。
    index_pct = inputs["index_pct"]
    if inputs["index_status"] != "same_session" or index_pct is None:
        missing.append("当日沪深300指数")
        return verdict(CLASSIFICATION_MARKET_UNPROVEN,
                       "当日沪深300指数参照不可用，无法证明是市场层面的回撤")
    floor = _num(policy.get("market_recovery_floor_pct"), 0.0)
    if index_pct < floor:
        return verdict(CLASSIFICATION_MARKET_NOT_RECOVERED,
                       f"指数仍在 {index_pct:+.2f}%（恢复下限 {floor:+.2f}%），市场未恢复")
    evidence.append(f"指数已回到 {index_pct:+.2f}%（下限 {floor:+.2f}%）")

    # 2) 个股的弱势要能被大盘解释。
    pct = inputs["pct"]
    if pct is None:
        missing.append("当日涨跌幅")
        return verdict(CLASSIFICATION_STOCK_WEAKNESS, "缺少当日涨跌幅，无法判断相对强弱")
    relative = pct - index_pct
    rel_floor = _num(policy.get("relative_floor_pct"), 0.0)
    if relative < rel_floor:
        return verdict(CLASSIFICATION_STOCK_WEAKNESS,
                       f"相对沪深300 弱 {relative:+.2f}%（下限 {rel_floor:+.2f}%），"
                       "按个股独立走弱处理，不恢复入场")
    evidence.append(f"相对沪深300 {relative:+.2f}%（下限 {rel_floor:+.2f}%）")

    # 3) 个股独立坏消息：公告/舆情风险不接受"跌后恢复"豁免。
    if negative_news:
        return verdict(CLASSIFICATION_NEGATIVE_EVENT,
                       "命中负面公告/舆情，个股事件不得用市场恢复解释")

    # 4) 持续资金流出：缺证据按未证明处理（fail-closed，清单里列出缺失项）。
    main_pct = inputs["main_pct"]
    if main_pct is None:
        missing.append("主力净流入占比")
        return verdict(CLASSIFICATION_UNPROVEN_FLOW,
                       "缺少主力资金证据，无法排除持续流出")
    outflow = _num(policy.get("outflow_main_pct"), 0.0)
    if main_pct <= outflow:
        return verdict(CLASSIFICATION_OUTFLOW,
                       f"主力净流入 {main_pct:+.2f}% 低于 {outflow:+.2f}%，按持续流出处理")
    evidence.append(f"主力净流入 {main_pct:+.2f}%（下限 {outflow:+.2f}%）")

    # 5) 收复跌后低点：用跌后最低价作参照，避免"从低点反弹一点点"就算恢复。
    price = inputs["price"]
    low = inputs["low_since_breakdown"]
    reclaim = _num(policy.get("reclaim_pct"))
    if price is None or not low or reclaim is None:
        missing.append("价格/跌后低点")
        return verdict(CLASSIFICATION_FALSE_REBOUND, "缺少价格或跌后低点，无法确认收复")
    low = min(low, price)
    need = low * (1.0 + reclaim)
    if price < need:
        return verdict(CLASSIFICATION_FALSE_REBOUND,
                       f"现价 {price:.2f} 未收复跌后低点 {low:.2f} 的 "
                       f"{reclaim * 100:.1f}%（需 {need:.2f}），按假反弹处理")
    evidence.append(f"现价 {price:.2f} 已收复跌后低点 {low:.2f} +{reclaim * 100:.1f}%")

    return verdict(CLASSIFICATION_MARKET_RECOVERY,
                   "普跌后恢复：上午有依据、市场已恢复、个股弱势可由大盘解释，"
                   "允许一次小仓位首笔入场",
                   allowed=True)
