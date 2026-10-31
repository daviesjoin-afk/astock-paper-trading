# -*- coding: utf-8 -*-
"""新开仓价格门禁与开盘追高时段余量（owner）。

这里只做**纯规则判定**：给定账户、候选与报价，判断这笔当日新开仓是否
允许，以及不允许的原因。它不读数据库、不取行情、不下单。

判定依据全部来自声明层（账户 spec）与传入的报价/候选事实，所以：

- 规则版本化在一处，不再散落在引擎各处；
- 门禁可以被独立回放与测试；
- 短线日内做T、主力承接、财报质量突破各���的专属追高通道在这里统一
  表达，而不是各写一份。
"""
from __future__ import annotations

import datetime as dt

import paper_account_specs as ACS
import strategy_policies as SPOL

from paper_numeric import num as _num
from paper_trading_rules import limit_pct as _limit_pct

NEW_STRATEGY_ID = SPOL.NEW_STRATEGY_ID
MAIN_FORCE_STRATEGY_ID = SPOL.MAIN_FORCE_STRATEGY_ID

#: 板块轮动的热点加速候选有专属追高通道（见 ``_new_entry_price_gate``）。
AGGRESSIVE_SECTOR_LANE_STATUSES = frozenset({
    "ths_hot_lane", "concept_expansion_lane", "sector_surge_lane", "hot_leader_watch",
})

#: 不使用 DSL 声明时，各策略的默认新开仓涨幅上限。
DEFAULT_ENTRY_PCT_CEILINGS = {
    "sector_rotation": 7.0,
    "trend_pullback": 6.0,
}


def _spec_for(account_id):
    """内置账户的声明 spec（独立副本）。"""
    spec = ACS.builtin_spec(account_id)
    return spec if spec is not None else ACS.fallback_spec()


def new_entry_price_gate(account_id, pick, quote=None, *, spec=None,
                          selection_mode=None):
    """判定一笔当日新开仓是否允许，返回 ``(allowed, reason)``。

    ``spec`` 是该账户的声明 spec（引擎解析后传入）。默认取内置声明，
    这样本模块可以独立测试；用户 DSL 策略必须由调用方传入真实 spec，
    否则会用错的上限。
    """
    spec = spec if spec is not None else _spec_for(account_id)
    if selection_mode is None:
        selection_mode = spec.get("selection_mode")
    return _new_entry_price_gate(account_id, pick, quote, spec=spec,
                                 selection_mode=selection_mode)


def _open_runup_bonus(now=None):
    """开盘追高上限的时段余量：早盘宽、随时间收紧、午后小幅容忍。

    开盘价只是集合竞价单点打印，且热点主题的日一动量集中在开盘
    半小时；固定 2% 上限会把板块异动车道要抓的进场窗口全部堵死
    （2026-08-25 09:34 600737 较开盘 +5.77% 被拒的教训）。余量：
    09:30-09:45 +2.5pp、09:45-10:30 +1.0pp、13:00-14:30 +0.5pp、其余 0。

    午后那一档是 2026-10-27 补的：14:00 前后的二次启动与早盘脉冲不是同一
    种风险，原来午后余量恒为 0，等于让"午后确认恢复"只能精确落在开盘价
    上方几个 bp。只放松 0.5pp，且所有策略原有的动量禁区/确认轮次不变。
    """
    now = now if isinstance(now, dt.datetime) else dt.datetime.now()
    t = now.time()
    if dt.time(9, 30) <= t <= dt.time(9, 45):
        return 0.025
    if dt.time(9, 45) < t <= dt.time(10, 30):
        return 0.010
    if dt.time(13, 0) <= t <= dt.time(14, 30):
        return 0.005
    return 0.0
def _new_entry_price_gate(account_id, pick, quote=None, *, spec=None,
                          selection_mode=None):
    """Keep same-day new entries away from late acceleration and limit-up queues.

    This gate applies only to a *new* strategy-position.  Existing positions
    still use their dedicated scale-in / intraday-T models; another strategy
    may independently hold the same code.

    2026-08-24 新增盘中追高闸门：现价较今日开盘的溢价超过
    ``max_open_runup_pct`` 时拒绝开仓。此前只校验涨幅 vs 昨收，
    候选在早盘被 deferred 后，等席位释放时按已拉升 2%+ 的现价成交
    （"便宜的不买贵的买"），追高买入极易买在日内顶部。
    """
    spec = spec if spec is not None else _spec_for(account_id)
    if selection_mode is None:
        selection_mode = spec.get("selection_mode")
    quote = quote or {}
    pct = _num(quote.get("pct"), _num(pick.get("pct"), None))
    # This is only a *routing* exception.  The later chase gate still requires
    # green market, top-sector confirmation, Q1, strong money flow/volume and
    # a passing cross-source quote before it may execute.  Do not apply this
    # to ordinary sector candidates or any other strategy.
    aggressive_sector_candidate = (
        account_id == "sector_rotation"
        and str(pick.get("candidate_status") or "") in AGGRESSIVE_SECTOR_LANE_STATUSES
        and pct is not None and pct >= 7.0
    )
    # 盘中追高上限：现价 / 今日开盘 - 1。开盘价缺失时跳过（无法度量盘中拉升）。
    price = _num(quote.get("price"), 0.0)
    open_price = _num(quote.get("open_price"), 0.0)
    max_runup = (spec or {}).get("max_open_runup_pct")
    # The fast strategy's acceleration zone is handled by the strict chase
    # gate below.  Let it reach that gate instead of being rejected first by
    # the generic open-price runup rule.
    tq_momentum_candidate = account_id == "tq_breakout" and pct is not None and pct >= 3.5
    # 主力点火候选（2026-08-31 复核 P1）：+3.5%~+7.5% 不再被开盘追高上限
    # 直接否决——通用追高帽与主力 +8.8% 执行区间互相矛盾，当日大量强股
    # 因此反复被拒。改走 ignition_entry 八项确认（入场评估层强制校验）。
    mf_ignition_candidate = (
        account_id == MAIN_FORCE_STRATEGY_ID
        and pct is not None and 3.5 <= pct <= 7.5
    )
    if (
        price > 0 and open_price > 0 and max_runup is not None
        and not aggressive_sector_candidate
        and not tq_momentum_candidate and not mf_ignition_candidate
    ):
        effective_cap = max_runup + _open_runup_bonus()
        runup = price / open_price - 1
        if runup > effective_cap:
            return False, (
                f"现价较开盘 {runup*100:+.2f}% 已超盘中追高上限 "
                f"{effective_cap*100:.1f}%（含时段余量），转观察等待回踩，不追日内高点"
            )
    if pct is None:
        return True, None
    entry_name = (spec or {}).get("entry_model_name") or account_id
    limit_pct = _limit_pct(pick.get("code"))
    # 短线日内做T拥有唯一的追高通道，后续仍必须通过 _chase_entry_gate 的
    # 双源行情、Q1、资金、量能和策略确认分校验；本函数不能提前把它筛掉。
    if account_id == "tq_breakout":
        if pct > limit_pct + 0.60:
            return False, f"涨幅 {pct:+.2f}% 已超出涨停附近有效报价区间，停止追高"
        if pct >= limit_pct - 0.15:
            return False, f"涨幅 {pct:+.2f}% 已触及涨停价位，不模拟封板排队成交"
        return True, None

    # 财报质量突破不追高：涨停附近或突破末端一律观察；其余高位按独立
    # 入场上限过滤。它不能借用短线接力的追高通道。
    if account_id == MAIN_FORCE_STRATEGY_ID:
        limit_buffer = 1.0 if limit_pct <= 10.0 else 2.0
        if pct >= limit_pct - limit_buffer:
            return False, f"涨幅 {pct:+.2f}% 已接近涨停，{entry_name}不模拟封板排队"
        ignition_high = 7.5
        if pct > ignition_high:
            return False, (
                f"涨幅 {pct:+.2f}% 超出主力承接/点火区间（≤+{ignition_high}%），转入观察"
            )
        if pct > 3.5:
            # 点火分支路由：此处不做实质判定，只打标；八项点火条件
            # （分钟量能、VWAP、低点抬高、资金增量/持续率、微观盘口）
            # 在入场评估层强制校验，30 秒快速通道同样携带该证据。
            if isinstance(pick, dict):
                pick["ignition_lane"] = True
        return True, None

    if account_id == NEW_STRATEGY_ID:
        limit_buffer = 1.0 if limit_pct <= 10.0 else 2.0
        if pct >= limit_pct - limit_buffer:
            return False, f"涨幅 {pct:+.2f}% 已接近涨停，{entry_name}不追高，转入回踩观察"
        ceiling = _num((spec or {}).get("entry_pct_high"), 6.5)
        if pct > ceiling:
            return False, f"涨幅 {pct:+.2f}% 超出{entry_name}突破执行上限，转入观察"
        return True, None

    # 板块轮动的热点加速候选由专属追高门禁接管；普通板块候选仍不追高。
    if aggressive_sector_candidate:
        if pct >= limit_pct - 0.15:
            return False, f"涨幅 {pct:+.2f}% 已触及涨停价位，不模拟封板排队成交"
        return True, None

    # 趋势与板块策略不追高：涨停附近一律观察；其余高位也按策略上限过滤。
    limit_buffer = 1.0 if limit_pct <= 10.0 else 2.0
    if pct >= limit_pct - limit_buffer:
        return False, f"涨幅 {pct:+.2f}% 已接近涨停，{entry_name}不追高，转入次日观察"
    ceiling = DEFAULT_ENTRY_PCT_CEILINGS.get(account_id, 7.0)
    if selection_mode == "dsl":
        ceiling = _num((spec or {}).get("entry_pct_high"), 7.0)
    if pct > ceiling:
        return False, f"涨幅 {pct:+.2f}% 超出{entry_name}当日新开仓区间，转入观察"
    return True, None


# ── 公开契约（PR-9） ───────────────────────────────────────────────────────
open_runup_bonus = _open_runup_bonus
