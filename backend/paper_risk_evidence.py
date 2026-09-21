# -*- coding: utf-8 -*-
"""Risk evidence and deterministic decision adapters for one claimed cycle.

This module owns the risk-only evidence/read-model adapters that used to live
inside :mod:`paper_trading`.  It never imports ``paper_trading`` and receives
all runtime/infrastructure dependencies through :class:`RiskEvidenceDeps`.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pandas as pd

import paper_position_review as PReview
import paper_position_review_evidence as PREV
import paper_replacement_decision as PRep
import paper_replacement_evidence as PREPL
import paper_risk_decision as PRD
import strategy_policies as SPOL
from paper_trading_rules import limit_pct as _limit_pct, security_scope as _security_scope

__all__ = [
    "RiskEvidenceDeps",
    "best_replacement_candidate",
    "intraday_downside_guard",
    "downside_confirmed",
    "position_quality_score",
    "over_capacity_exit_candidates",
    "permission_scope_exit_candidates",
    "save_position_review",
    "sell_plan",
]


@dataclass(frozen=True)
class RiskEvidenceDeps:
    """Dependencies that remain owned by the paper-trading application layer."""

    lot_size: int
    max_sells_per_run: int
    blocked_retry_minutes: int
    small_pct: float
    permission_scope_exit_max_per_strategy_day: int
    permission_scope_exit_ratio: float
    protective_exit_classes: frozenset[str]
    entry_retry_signal_statuses: tuple[str, ...]
    entry_frozen_waitlist_status: str
    intraday_interval_minutes: int
    holding_quality_weights: Mapping[str, Mapping[str, float]]
    strategy_risk_behaviors: Mapping[str, Mapping[str, Any]]
    main_force_strategy_id: str
    review_policy: Any
    risk_version: str
    alt_data: Any | None
    load_kline: Callable[..., Any]


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date(value=None):
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError):
        return default if default is not None else {}


def _num(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) and pd.notna(value) else default


def _score100(value, default=50.0):
    if value is None:
        return default
    value = _num(value, default / 100.0)
    if 0.0 <= value <= 1.5:
        value *= 100.0
    return max(0.0, min(100.0, value))


def _negative_hits(news, code):
    return [
        row for row in (news or [])
        if row.get("code") == code and row.get("tone", 0) < 0
    ]


def _hold_days(position, asof_day, load_kline):
    frame = load_kline(position["code"], asof_day, inclusive=False)
    if frame is not None and not frame.empty:
        try:
            return int((frame.index.date > _date(position["entry_date"])).sum())
        except Exception:
            pass
    return max((_date(asof_day) - _date(position["entry_date"])).days, 0)


def _replacement_min_hold_days(account_id):
    return int(SPOL.position_review_min_hold_days(account_id))


def _volatility_shadow(code, asof_day, price, load_kline):
    result = {"version": "volatility-shadow-v1", "status": "unknown", "atr20_pct": None,
              "daily_std_pct": None, "samples": 0, "asof_date": _date(asof_day).isoformat()}
    try:
        frame = load_kline(str(code), _date(asof_day), inclusive=False)
        if frame is None or len(frame) < 15:
            result["reason"] = "历史K线不足15根"
            return result
        closes, trs, prev = [], [], None
        for _, row in frame.iterrows():
            close = _num(row.get("close"), None)
            high = _num(row.get("high"), close)
            low = _num(row.get("low"), close)
            if not close or close <= 0:
                continue
            closes.append(close)
            if prev:
                trs.append(max(high - low, abs(high - prev), abs(low - prev)))
            prev = close
        if len(closes) < 15:
            result["reason"] = "有效收盘价不足15根"
            return result
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        recent_tr = trs[-20:]
        recent_ret = returns[-20:]
        mean_ret = sum(recent_ret) / len(recent_ret) if recent_ret else 0.0
        variance = sum((value - mean_ret) ** 2 for value in recent_ret) / max(len(recent_ret) - 1, 1)
        last_close = _num(price, closes[-1]) or closes[-1]
        result.update({
            "status": "ok", "samples": len(closes),
            "atr20_pct": round(sum(recent_tr) / len(recent_tr) / last_close * 100, 3) if recent_tr else None,
            "daily_std_pct": round((variance ** 0.5) * 100, 3),
            "last_close": round(last_close, 4),
        })
    except Exception as exc:
        result["reason"] = f"影子计算失败：{type(exc).__name__}"
    return result


def best_replacement_candidate(conn, account_id, day, held_codes, *, deps: RiskEvidenceDeps):
    """Find the strongest **same-day** pending candidate that is not already held.

    R18（as-of hard contract）：今天能触发今天卖出的替补必须**属于今天**。候选读取
    经 :mod:`paper_replacement_evidence` 固定在 ``intended_date == day``（**等式**，
    不是 ``today..next_weekday`` 的 range）与 ``signal_date <= day``（历史 as-of 不得
    读到未来证据）；否则明天的候选会先制造今天的卖出，而真实 BUY 又因
    ``signal_freshness`` 要求 ``intended_date == asof_day`` 被拒（§4-§7）。

    合法 overnight 计划（``signal_date = D-1``、``intended_date = D``）仍然可用。
    归档 signal 不参与：它是历史 opening 证据，不是 executable candidate。
    """
    statuses = ("pending", "deferred_capacity", deps.entry_frozen_waitlist_status)
    rows = PREPL.load_replacement_candidates(
        conn, account_id=account_id, asof_day=_date(day).isoformat(), statuses=statuses,
    )
    # security scope 仍由 adapter 处理（纯域模块不 import 项目 API）。
    allowed = []
    for row in rows:
        pick = (_loads(row.get("payload"), {}) or {}).get("pick") or {}
        code = str(row.get("code") or "")
        if code and _security_scope(code, row.get("name") or pick.get("name"),
                                    pick.get("risk_flag"))["allowed"]:
            allowed.append(row)
    # 候选质量是稳定复合分；排序在纯域模块里（held 排除也在那里再兜一次）。
    return PRep.choose_best_candidate(allowed, held_codes=held_codes)


def intraday_downside_guard(position, quote, market=None, news=None, policy_override=None,
                            flow_trajectory=None, *, asof_day, deps: RiskEvidenceDeps):
    """Combine intraday weakness and main-force intent into a staged guard.

    ``warning`` is informational/freeze-add territory.  ``partial`` and
    ``full`` are candidates for a sell only after the caller confirms the
    signal on a subsequent scan.  Washout evidence suppresses a sell unless
    a severe loss/negative-event condition is also present.

    ``asof_day`` 必填：峰值口径与主力意图都不得回退到机器当前日期，
    R15 的缺陷正是在这里把 as-of 漏给了峰值 helper。
    """
    account_id = str(position.get("account_id") or "")
    policy = dict(SPOL.intraday_downside_policy(account_id))
    if policy_override:
        for key in policy:
            candidate_key = f"downside_{key}"
            if candidate_key in policy_override and policy_override[candidate_key] is not None:
                policy[key] = policy_override[candidate_key]
    if not policy:
        return {"level": "none", "sell_ratio": 0.0, "reason": "无对应策略下跌防线"}
    quote = quote or {}
    pct = _num(quote.get("pct"), None)
    price = _num(quote.get("price"), None)
    cost = _num(position.get("cost"), None)
    ret_pct = (price / cost - 1) * 100 if price and cost else None
    market_pct = _num((market or {}).get("live_index_pct"), None)
    relative = pct - market_pct if pct is not None and market_pct is not None else None
    peak = PRD.position_peak(position, quote, price, asof_day=asof_day)
    peak_retrace = (1 - price / peak) * 100 if price and peak else None
    peak_return = (peak / cost - 1) * 100 if peak and cost else None
    giveback = peak_return - ret_pct if peak_return is not None and ret_pct is not None else None
    intent = PRD.main_force_intent(position, quote, market=market, news=news)
    distribution = intent.get("classification") == "distribution" and intent.get("confidence", 0) >= 0.58
    washout = intent.get("classification") == "washout" and intent.get("confidence", 0) >= 0.58
    negative_news = bool(_negative_hits(news or [], str(position.get("code") or "")))
    warning = bool(
        (pct is not None and pct <= policy["warning_pct"])
        or (relative is not None and relative <= policy["relative_pct"])
        or (peak_retrace is not None and peak_retrace >= policy["peak_retrace_pct"])
    )
    # Intraday-T must not dump inventory during a low-open/high-go recovery.
    # A quote that has reclaimed the morning low by >=1.5% with a non-negative
    # tape is treated as a washout/recovery unless independent distribution
    # evidence is present.  Hard stops remain handled by _sell_plan.
    day_low = _num(quote.get("low"), None)
    low_rebound = ((price / day_low - 1.0) * 100) if price and day_low and day_low > 0 else None
    recovery_hold = bool(
        account_id == "tq_breakout"
        and low_rebound is not None and low_rebound >= 1.5
        and pct is not None and pct >= -1.0
        and not distribution and not negative_news
    )
    if recovery_hold:
        warning = False
    flow_trajectory = dict(flow_trajectory or {})
    main_force_distribution = bool(
        position.get("account_id") == deps.main_force_strategy_id
        and distribution and flow_trajectory.get("status") == "ok"
        and _num(flow_trajectory.get("main_delta_5m"), 0.0) < 0
        and _num(flow_trajectory.get("positive_persistence_10m"), 1.0) < 0.50
    )
    # Distribution needs both price weakness and an independent confirmation;
    # a large fall with positive/neutral flow is treated as possible washout.
    partial = bool(
        distribution
        and pct is not None and pct <= policy["partial_pct"]
        and (ret_pct is None or ret_pct <= 0 or (relative is not None and relative <= policy["relative_pct"]))
    )
    if main_force_distribution:
        partial = True
    severe = bool(
        distribution
        and pct is not None and pct <= policy["full_pct"]
        and (ret_pct is None or ret_pct <= 0)
        and peak_retrace is not None
        and peak_retrace >= policy["peak_retrace_pct"]
    )
    if main_force_distribution:
        severe = True
    # A position can lose its entire accumulated edge before the daily loss
    # threshold is reached.  Treat that as a separate, auditable risk path:
    # washout evidence may suppress the ordinary intent-based sell, but it
    # must not suppress protection of a meaningful peak-to-current giveback.
    giveback_partial = bool(
        giveback is not None
        and peak_return is not None
        and peak_return >= policy.get("giveback_min_peak_return_pct", 4.0)
        and giveback >= policy.get("giveback_partial_pct", 6.0)
        and (ret_pct is None or ret_pct <= 1.0)
        and (pct is None or pct <= 0.0)
    )
    giveback_severe = bool(
        giveback_partial
        and giveback >= policy.get("giveback_full_pct", 10.0)
        and (ret_pct is None or ret_pct <= -3.0)
    )
    if washout and not negative_news:
        # 洗盘只是在风险尚可承受时延迟减仓，不能覆盖各策略自己的
        # 亏损保护线。短线T容忍区最窄，趋势波段最宽，板块轮动居中。
        washout_override = _num(
            (deps.strategy_risk_behaviors.get(account_id) or {}).get(
                "washout_loss_override_pct"
            ),
            -4.0,
        )
        loss_beyond_override = (
            ret_pct is not None and ret_pct <= washout_override
        ) or (
            relative is not None and relative <= policy["relative_pct"] - 1.0
        )
        if not loss_beyond_override:
            partial = False
            severe = False
    warning_trim_ratio = _num(policy.get("warning_trim_ratio"), 0.0)
    if giveback_severe or severe:
        level, sell_ratio = "full", 1.0
    elif giveback_partial or partial:
        level, sell_ratio = "partial", policy["partial_ratio"]
    elif warning:
        level, sell_ratio = "warning", warning_trim_ratio
    else:
        level, sell_ratio = "none", 0.0
    reasons = []
    if main_force_distribution:
        reasons.append("超强主力出货共振：意图分类为疑似出货，5分钟资金转负且10分钟持续率低于50%")
    if pct is not None and pct <= policy["warning_pct"]:
        reasons.append(f"当日跌幅 {pct:+.2f}%")
    if relative is not None and relative <= policy["relative_pct"]:
        reasons.append(f"相对沪深300弱 {relative:+.2f}%")
    if peak_retrace is not None and peak_retrace >= policy["peak_retrace_pct"]:
        reasons.append(f"盘中高点回撤 {peak_retrace:.2f}%")
    if giveback_partial:
        reasons.append(
            f"收益回吐保护：峰值收益 {peak_return:.2f}%、已回吐 {giveback:.2f}%"
        )
    reasons.append(intent.get("label") or "主力意图不确定")
    return {
        "level": level,
        "sell_ratio": sell_ratio,
        "reason": "；".join(reasons) if reasons else "未达到下跌预警条件",
        "policy": policy,
        "main_force_intent": intent,
        "intraday_pct": round(pct, 2) if pct is not None else None,
        "cost_return_pct": round(ret_pct, 2) if ret_pct is not None else None,
        "relative_to_market_pct": round(relative, 2) if relative is not None else None,
        "peak_retrace_pct": round(peak_retrace, 2) if peak_retrace is not None else None,
        "peak_return_pct": round(peak_return, 2) if peak_return is not None else None,
        "giveback_pct": round(giveback, 2) if giveback is not None else None,
        "giveback_protection": bool(giveback_partial or giveback_severe),
        "strategy_risk_behavior": deps.strategy_risk_behaviors.get(account_id, {}),
        "negative_news": negative_news,
        "flow_trajectory": flow_trajectory,
        "main_force_distribution": main_force_distribution,
        "recovery_hold": recovery_hold,
        "recovery_hold_reason": (
            f"低开高走：较日内低点反弹 {low_rebound:.2f}%，暂缓日内预警减仓"
            if recovery_hold else None
        ),
        # 日内T的预警首段减仓是一次性的轻仓保护；趋势/板块仍维持
        # 两次确认后才执行的 partial/full 机制。
        "requires_confirmation": level in {"partial", "full"},
        "model": "intraday_downside_guard_v1",
        "asof": quote.get("quote_at"),
    }


def downside_confirmed(conn, account_id, code, asof_day, guard, *, deps: RiskEvidenceDeps):
    """Require a distinct prior five-minute scan with the same adverse intent."""
    # 2026-09-03 二次确认减仓：warning 级也允许走两次确认。首段减仓
    # 一天只有一次，之后价格长期停在 warning 区间（跌不深但持续阴跌、
    # 主力意图反复读出疑似出货）时，两次确认是仅存的保护通道，
    # 不能再被级别门槛直接挡掉。
    if not guard or guard.get("level") not in {"partial", "full", "warning"}:
        return False
    rows = conn.execute(
        """SELECT decision,payload,created_at FROM paper_risk_decisions
           WHERE account_id=? AND code=? AND side='sell'
             AND substr(created_at,1,10)=?
             AND decision IN ('downside_warning','downside_partial_pending','downside_full_pending')
           ORDER BY id DESC LIMIT 2""",
        (account_id, code, _date(asof_day).isoformat()),
    ).fetchall()
    if not rows:
        return False
    latest_row = rows[0]
    latest = _loads(latest_row["payload"], {})
    prior_guard = latest.get("downside_guard") or {}
    prior_class = ((prior_guard.get("main_force_intent") or {}).get("classification"))
    current_class = ((guard.get("main_force_intent") or {}).get("classification"))
    prior_level = prior_guard.get("level")
    current_time = str(guard.get("asof") or "")
    prior_time = str(prior_guard.get("asof") or latest_row["created_at"] or "")
    try:
        current_at = dt.datetime.fromisoformat(current_time.replace("Z", "+00:00"))
        prior_at = dt.datetime.fromisoformat(prior_time.replace("Z", "+00:00"))
        if bool(current_at.tzinfo) != bool(prior_at.tzinfo):
            return False
        if (current_at - prior_at).total_seconds() < max(60, deps.intraday_interval_minutes * 60 - 30):
            return False
    except (TypeError, ValueError, OverflowError):
        # A missing timestamp must fail closed; two concurrent scheduler calls
        # must never be mistaken for two independent confirmations.
        return False
    # Giveback protection is intent-independent by design (see the audit note
    # above): a position that gave back a large accumulated edge while the
    # main-force classifier returned "uncertain" (e.g. missing flow fields)
    # used to make this confirmation unreachable, so the protective exit could
    # never fire until the hard stop.  Two consecutive scans both carrying the
    # giveback flag are still required — the spacing check above already
    # guarantees they are distinct observations.
    giveback_today = bool(
        conn.execute(
            ("SELECT 1 FROM paper_risk_decisions "
             "WHERE account_id=? AND code=? AND side='sell' "
             "AND substr(created_at,1,10)=? "
             "AND decision IN ('downside_warning','downside_partial_pending','downside_full_pending') "
             "AND json_extract(payload,'$.downside_guard.giveback_protection') "
             "    IN (1,'1','true','True') "
             "LIMIT 1"),
            (account_id, code, _date(asof_day).isoformat()),
        ).fetchone()
    )
    # 2026-09-03 sticky fix: giveback protection is a persistent intraday
    # condition.  Once it has fired in ANY earlier scan today it stays valid
    # for the rest of the day, so a minor bounce flipping the live flag back
    # to 0 can no longer make the protective confirmation unreachable (the
    # position used to drift all the way to the hard stop instead).
    giveback_confirmed = (
        bool(guard.get("giveback_protection")) or giveback_today
    ) and (
        bool(prior_guard.get("giveback_protection")) or giveback_today
    )
    confirmed_intent = (
        prior_class == current_class == "distribution" or giveback_confirmed
    )
    return prior_level in {"warning", "partial", "full"} and confirmed_intent


def position_quality_score(conn, position, quote, asof_day, *, cycle_id, news=None, replacement=None, nav=None, market=None,
                           flow_trajectory=None, deps: RiskEvidenceDeps):
    """Score an existing holding for concentration decisions (0..100).

    The score is intentionally independent from the entry gate: it combines
    the position's actual return, live momentum/flow, completed-kline trend,
    the original model score and verified negative-event pressure.  A high
    score may remain as a one-lot core/observation holding; a low score is
    eligible for rotation only after T+1 and quote gates pass.

    ``cycle_id`` 是 **keyword-only 且必填**（R17）：入场 signal 的 provenance
    必须钉在**已认领的**周期上，实现层不得再问一次"现在 active 的是谁"。

    评分算术本身已抽到 :mod:`paper_position_review`；本函数只做证据收集与
    orchestration（行情 / K 线 / provenance / 新闻 / 替代数据 / 权重 / 替补）。
    """
    code = str(position.get("code") or "")
    account_id = position.get("account_id")
    price = _num(quote.get("price"), _num(position.get("cost")))
    cost = _num(position.get("cost"))
    ret_pct = (price / cost - 1) * 100 if price > 0 and cost > 0 else 0.0
    pct = _num(quote.get("pct"))
    main_pct = _num(quote.get("main_pct"), _num(quote.get("main_net_pct")))
    vol_ratio = _num(quote.get("vol_ratio"), 1.0)
    turnover = _num(quote.get("turnover"), 0.0)
    momentum = max(0.0, min(100.0, 50.0 + pct * 4.0 + (vol_ratio - 1.0) * 12.0))
    flow = max(0.0, min(100.0, 50.0 + main_pct * 4.0))
    return_score = max(0.0, min(100.0, 50.0 + ret_pct * 3.0))

    trend = 50.0
    trend_detail = "趋势数据不足"
    frame = deps.load_kline(code, asof_day, inclusive=False)
    if frame is not None and not frame.empty and "close" in frame.columns:
        close_series = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if not close_series.empty:
            last_close = float(close_series.iloc[-1])
            ma20 = float(close_series.tail(20).mean()) if len(close_series) >= 20 else None
            ma60 = float(close_series.tail(60).mean()) if len(close_series) >= 60 else None
            checks = []
            if ma20:
                checks.append(last_close >= ma20)
            if ma60:
                checks.append(last_close >= ma60)
            if ma20 and ma60:
                checks.append(ma20 >= ma60)
            trend = 50.0 + sum(20.0 if item else -20.0 for item in checks)
            trend = max(0.0, min(100.0, trend))
            trend_detail = f"收盘/MA20/MA60结构 {sum(checks)}/{len(checks)}"

    # R17：入场模型分只能来自**当前 episode 的 provenance** ——
    # risk_state.opened_order_id → verified cycle-owned BUY order → 精确 signal_id。
    # 绝不回落到「account+code 的最近一条 signal」（那既没有 episode 归属，
    # 也没有 asof 上界，会让后来的无关 signal 或未来 signal 改变自动换仓判定）。
    provenance = PREV.resolve_entry_signal(
        conn,
        cycle_id=cycle_id,
        account_id=account_id,
        code=code,
        opened_order_id=position.get("episode_opened_order_id"),
        asof_day=asof_day,
    )
    model_score = 50.0
    model_score_source = "unknown"
    if provenance["status"] == "verified":
        signal = provenance["signal"]
        payload = _loads(signal.get("payload"), {})
        entry = (payload.get("decision") or {}).get("entry_model") or {}
        model_score = max(
            _score100(signal["t_score"], 0.0),
            _score100(entry.get("score"), 0.0),
            _score100(signal["rank_score"], 0.0),
        )
        model_score_source = "episode_provenance"
    negative = _negative_hits(news or [], code)
    main_force_intent = PRD.main_force_intent(position, quote, market=market, news=news)
    news_penalty = min(24.0, len(negative) * 12.0)
    # 限售解禁预警（P0）：未来30天大额解禁（≥3%流通）重扣，60天≥5%中扣。
    # 解禁是确定性的供给冲击，等价格反应再退出就晚了；数据源失败时扣0。
    lockup_penalty, lockup_desc = 0.0, None
    if deps.alt_data is not None:
        try:
            lockup_penalty, lockup_desc = deps.alt_data.lockup_penalty(code, asof_day=asof_day)
        except Exception:
            lockup_penalty, lockup_desc = 0.0, None
    # 龙虎榜信号（P1）：近7天上榜净买 +6（抢筹确认），净卖 -6（出货警示）。
    lhb_bonus, lhb_desc = 0.0, None
    if deps.alt_data is not None:
        try:
            lhb_bonus, lhb_desc = deps.alt_data.lhb_position_signal(code, asof_day=asof_day)
        except Exception:
            lhb_bonus, lhb_desc = 0.0, None
    # 筹码/杠杆信号（P2）：两融 ±4、大宗 -5/+3、股东户数 ±5。
    # 各自独立小幅度，合计最坏 -14，与 lockup/lhb 共同构成替代数据
    # 评分层；任一数据源失败该项为 0，不影响其余。
    margin_bonus, margin_desc, block_bonus, block_desc, holder_bonus, holder_desc = 0.0, None, 0.0, None, 0.0, None
    if deps.alt_data is not None:
        try:
            margin_bonus, margin_desc = deps.alt_data.margin_signal(code, asof_day=asof_day)
        except Exception:
            margin_bonus, margin_desc = 0.0, None
        try:
            block_bonus, block_desc = deps.alt_data.block_trade_signal(code, asof_day=asof_day)
        except Exception:
            block_bonus, block_desc = 0.0, None
        try:
            holder_bonus, holder_desc = deps.alt_data.holder_signal(code, asof_day=asof_day)
        except Exception:
            holder_bonus, holder_desc = 0.0, None
    alt_bonus = lhb_bonus + margin_bonus + block_bonus + holder_bonus
    alt_shadow_delta = alt_bonus - lockup_penalty
    weights = deps.holding_quality_weights.get(account_id, deps.holding_quality_weights["trend_pullback"])
    hold_days = _hold_days(position, asof_day, deps.load_kline)
    # 评分算术与 grade 边界在 paper_position_review（纯域模块），公式逐字等价。
    scored = PReview.score_quality(
        model_score=model_score, trend_score=trend, flow_score=flow,
        momentum_score=momentum, return_score=return_score,
        news_penalty=news_penalty, weights=weights, hold_days=hold_days,
    )
    score = scored["score"]
    grade = scored["grade"]
    trend_for_score = scored["trend_for_score"]
    review_phase = scored["review_phase"]
    market_value = max(0.0, _num(position.get("qty")) * max(price, 0.0))
    nav = max(_num(nav), 0.0)
    position_pct = market_value / nav * 100 if nav else 0.0
    small = position_pct < deps.small_pct * 100
    one_lot = int(_num(position.get("qty"))) <= deps.lot_size
    replacement_score = _num((replacement or {}).get("score"), None)
    replacement_edge = replacement_score - score if replacement_score is not None else None
    reasons = [
        f"模型 {model_score:.1f}", f"趋势 {trend_for_score:.1f}" + ("（新仓中性）" if hold_days < 1 else ""),
        f"资金 {flow:.1f}", f"动量 {momentum:.1f}",
        f"收益 {return_score:.1f}",
    ]
    if negative:
        reasons.append(f"负面事件 {len(negative)} 条")
    if lockup_penalty:
        reasons.append(f"影子·限售解禁 -{lockup_penalty:.0f}（{lockup_desc}）")
    if lhb_desc:
        reasons.append(f"影子·龙虎榜 {'+' if lhb_bonus >= 0 else ''}{lhb_bonus:.0f}（{lhb_desc}）")
    for _bonus, _desc, _label in (
        (margin_bonus, margin_desc, "两融"),
        (block_bonus, block_desc, "大宗"),
        (holder_bonus, holder_desc, "股东户数"),
    ):
        if _desc:
            reasons.append(f"影子·{_label} {'+' if _bonus >= 0 else ''}{_bonus:.0f}（{_desc}）")
    if one_lot:
        reasons.append("当前仅一手")
    if small:
        reasons.append(f"仓位仅 {position_pct:.2f}%")
    reasons.append(
        f"主力意图 {main_force_intent['label']}"
        f"（置信度 {main_force_intent['confidence']*100:.0f}%）"
    )
    flow_trajectory = dict(flow_trajectory or {})
    if flow_trajectory.get("status") == "ok":
        flow_direction = flow_trajectory.get("direction")
        divergence = "none"
        if pct > 0 and flow_direction == "outflow":
            divergence = "price_up_flow_out"
        elif pct < 0 and flow_direction == "inflow":
            divergence = "price_down_flow_in"
        flow_trajectory["price_flow_divergence"] = divergence
        labels = {"inflow": "流入", "outflow": "流出", "flat": "平稳"}
        reasons.append(
            f"影子·分钟资金 {labels.get(flow_direction, '未知')}，"
            f"5分钟主力变化 {float(flow_trajectory.get('main_delta_5m') or 0)/10000:.1f}万，"
            f"持续性 {float(flow_trajectory.get('positive_persistence_10m') or 0)*100:.0f}%"
        )
    return {
        "code": code, "account_id": account_id, "score": score, "grade": grade,
        "market_value": round(market_value, 2), "position_pct": round(position_pct, 2),
        "ret_pct": round(ret_pct, 2), "hold_days": hold_days,
        "small_position": bool(small), "one_lot": bool(one_lot),
        "review_phase": review_phase, "weights": weights,
        "model_score": round(model_score, 2), "trend_score": round(trend_for_score, 2),
        "trend_raw_score": round(trend, 2),
        # R17 provenance：解释"这个 model_score 是怎么来的"，让 50 分不是黑箱。
        "model_score_source": model_score_source,
        "episode_opened_order_id": position.get("episode_opened_order_id"),
        "entry_signal_id": provenance.get("signal_id"),
        "entry_signal_date": provenance.get("signal_date"),
        "entry_signal_provenance_status": provenance["status"],
        "entry_signal_provenance_reason": provenance.get("reason"),
        # decide_action 是纯函数，最短观察期由 adapter 注入（账户配置不进口域层）。
        "min_hold_days": _replacement_min_hold_days(account_id),
        "flow_score": round(flow, 2), "momentum_score": round(momentum, 2),
        "return_score": round(return_score, 2), "turnover": round(turnover, 2),
        "news_penalty": round(news_penalty, 2),
        "lockup_penalty": round(lockup_penalty, 2),
        "lockup_detail": lockup_desc,
        "lhb_bonus": round(lhb_bonus, 2),
        "lhb_detail": lhb_desc,
        "margin_bonus": round(margin_bonus, 2), "margin_detail": margin_desc,
        "block_bonus": round(block_bonus, 2), "block_detail": block_desc,
        "holder_bonus": round(holder_bonus, 2), "holder_detail": holder_desc,
        "alt_shadow_delta": round(alt_shadow_delta, 2),
        "alt_score_applied": False,
        "fund_flow_trajectory": flow_trajectory,
        "fund_flow_trajectory_applied": False,
        "trend_detail": trend_detail,
        "replacement": replacement, "replacement_score": replacement_score,
        "replacement_edge": round(replacement_edge, 2) if replacement_edge is not None else None,
        "main_force_intent": main_force_intent,
        "reasons": reasons,
    }


def over_capacity_exit_candidates(conn, positions, reviews, account_map, asof_day,
                                  *, count_budget, account_specs, deps: RiskEvidenceDeps):
    """Pick the weakest *sellable* positions when a strategy exceeds its cap.

    The cap controls the number of distinct stocks, not the number of lots.
    New purchases are blocked immediately; existing excess holdings are then
    reduced over ordinary risk passes, preserving T+1 and live-quote gates.
    """
    selected = {}
    by_account = {}
    for position in positions:
        if int(_num(position.get("qty"))) >= deps.lot_size:
            by_account.setdefault(position["account_id"], []).append(position)
    for account_id, items in by_account.items():
        fallback = (account_specs.get(account_id) or {}).get("max_positions", 5)
        limit = max(1, int(count_budget["limits"].get(account_id, fallback)))
        excess = max(0, len(items) - limit)
        if not excess:
            continue
        eligible = [
            item for item in items
            if int(_num(item.get("available_qty"))) >= deps.lot_size
        ]
        eligible.sort(key=lambda item: (
            _num((reviews.get((account_id, item["code"])) or {}).get("score"), 100.0),
            _num((reviews.get((account_id, item["code"])) or {}).get("market_value"), 0.0),
        ))
        for rank, item in enumerate(eligible[:excess], start=1):
            review = reviews.get((account_id, item["code"])) or {}
            selected[(account_id, item["code"])] = (
                f"策略持仓数 {len(items)}/{limit}（总上限 {count_budget['pool_limit']}），压缩超额持仓；"
                f"按质量评分排序第 {rank} 个（{_num(review.get('score')):.1f} 分）"
            )
    return selected


def permission_scope_exit_candidates(conn, positions, reviews, quote_map, asof_day,
                                     *, deps: RiskEvidenceDeps):
    """Choose at most one restricted holding per strategy and trading day.

    These are legacy positions which the user cannot trade on the configured
    account (STAR/BSE/ST).  They must never be reinforced.  Exits are gradual,
    auditable and still obey T+1, fresh-quote and limit-down execution gates.
    """
    selected = {}
    by_account = {}
    for position in positions:
        quote = (quote_map or {}).get(position.get("code"), {})
        scope = _security_scope(
            position.get("code"), quote.get("name") or position.get("name"),
            quote.get("risk_flag"),
        )
        if scope["allowed"] or int(_num(position.get("qty"))) < deps.lot_size:
            continue
        item = dict(position)
        item["security_scope"] = scope
        by_account.setdefault(position["account_id"], []).append(item)
    priority = {"风险警示": 0, "北交所": 1, "科创板": 2}
    for account_id, items in by_account.items():
        completed_today = conn.execute(
            """SELECT COUNT(*) FROM paper_audit
               WHERE account_id=? AND event='permission_scope_exit'
                 AND substr(created_at,1,10)=?""",
            (account_id, _date(asof_day).isoformat()),
        ).fetchone()[0]
        remaining = max(0, deps.permission_scope_exit_max_per_strategy_day - int(completed_today or 0))
        if not remaining:
            continue
        items.sort(key=lambda item: (
            0 if int(_num(item.get("available_qty"))) >= deps.lot_size else 1,
            priority.get(item["security_scope"].get("board"), 9),
            _num((reviews.get((account_id, item["code"])) or {}).get("score"), 100.0),
        ))
        for item in items[:remaining]:
            scope = item["security_scope"]
            selected[(account_id, item["code"])] = (
                f"{scope['reason']}；不在可交易权限范围，按每策略每日最多 "
                f"{deps.permission_scope_exit_max_per_strategy_day} 只逐步退出并释放席位"
            )
    return selected


def save_position_review(conn, cycle_id, review, action, reason):
    # R17：review_date 必须**显式**来自调用方。此前是
    # ``_date(review.get("review_date") or dt.date.today())`` —— 一个 wall-clock
    # 回退：任何遗漏 review_date 的路径都会把历史 as-of 复核日期偷偷写成"机器今天"，
    # 而 monitor_risk 一直显式设置 review["review_date"] = day。缺失即 fail fast，
    # 绝不猜日期（determinism 修复，属 R17 范围）。
    review_date = review.get("review_date")
    if review_date is None or str(review_date).strip() == "":
        raise ValueError("_save_position_review 需要显式 review_date（不允许 wall-clock 回退）")
    replacement = review.get("replacement") or {}
    conn.execute(
        """INSERT INTO paper_position_reviews(
           cycle_id,account_id,code,review_date,score,grade,action,market_value,
           position_pct,replacement_code,replacement_score,reasons,detail,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cycle_id,account_id,code,review_date) DO UPDATE SET
             score=excluded.score,grade=excluded.grade,action=excluded.action,
             market_value=excluded.market_value,position_pct=excluded.position_pct,
             replacement_code=excluded.replacement_code,replacement_score=excluded.replacement_score,
             reasons=excluded.reasons,detail=excluded.detail,created_at=excluded.created_at""",
        (
            cycle_id, review["account_id"], review["code"], _date(review_date).isoformat(),
            review["score"], review["grade"], action, review["market_value"], review["position_pct"],
            replacement.get("code"), review.get("replacement_score"),
            "；".join(review.get("reasons") or []),
            _json({**review, "action": action, "action_reason": reason}), _now(),
        ),
    )


def sell_plan(position, quote, asof_day, news, hard_stop_touched_today=False, spec_override=None,
              *, base_spec, deps: RiskEvidenceDeps):
    """卖出决策的 orchestration adapter：解析输入 → 交给纯 engine → 组装旧 schema。

    真正的硬止损/移动止损/最长持有/阶梯止盈/严重度仲裁都在
    ``paper_risk_decision.evaluate_sell``（零 DB / 零 wall clock / 零策略依赖）。
    这里只做本模块才有能力做的事：解析策略 spec、算 hold_days、解析涨跌停与
    首段减仓比例、补 volatility shadow 诊断，并保持对调用方稳定的 4 元组返回。
    """
    # PR-30：spec_override 允许调用方传入"account_specs × 编译画像"的生效参数
    # （hard_stop/trail/hold_max 取更紧）；未传时保持原有行为。
    spec = dict(base_spec)
    if spec_override:
        spec.update(spec_override)
    days = _hold_days(position, asof_day, deps.load_kline)
    price = _num(quote.get("price"), 0)
    decision = PRD.evaluate_sell(
        position, quote, asof_day=asof_day, spec=spec, hold_days=days, news=news,
        hard_stop_touched_today=hard_stop_touched_today,
        limit_pct=_limit_pct(position["code"]),
        # 首段减仓比例同样来自策略 policy：engine 不解析账户归属。
        hard_stop_first_trim_ratio=SPOL.intraday_downside_policy(
            position["account_id"]
        ).get("partial_ratio", 0.35),
        risk_version=deps.risk_version,
    )
    if decision["status"] == "no_quote":
        return 0.0, "缺少有效报价", decision["next_stage"], {
            "strategy_id": position["account_id"],
            "risk_profile": spec.get("risk_profile"),
            "strategy_version": decision["strategy_version"],
            "hold_days": days,
        }
    exit_profile = {
        "strategy_id": position["account_id"],
        "risk_profile": spec.get("risk_profile"),
        "strategy_version": decision["strategy_version"],
        "hold_min": spec.get("hold_min"),
        "hold_max": spec.get("hold_max"),
        "hard_stop": spec.get("hard_stop"),
        "trail_after": spec.get("trail_after"),
        "trail_stop": spec.get("trail_stop"),
        "take_profit": spec.get("take_profit"),
        "hard_stop_unchanged": True,
    }
    exit_class = decision["exit_class"]
    return decision["sell_ratio"], decision["reason"], decision["next_stage"], {
        "strategy_id": position["account_id"],
        "risk_profile": spec.get("risk_profile"),
        "strategy_version": decision["strategy_version"],
        "exit_profile": exit_profile,
        "ret_pct": round(decision["ret"]*100, 2),
        "drawdown_pct": round((decision["drawdown"] or 0)*100, 2),
        "hold_days": days,
        "main_force_intent": decision["main_force_intent"],
        "shadow_news_warning_count": decision["shadow_news_warning_count"],
        "shadow_news_notice": (
            "快讯关键词仅作影子提示，不自动卖出"
            if decision["shadow_news_warning_count"] else None
        ),
        "exit_class": exit_class,
        "exit_reason_code": decision["exit_reason_code"],
        "exit_marker": decision["exit_marker"],
        "protective_exit": exit_class in deps.protective_exit_classes,
        "volatility_shadow": _volatility_shadow(position["code"], asof_day, price, deps.load_kline),
    }
