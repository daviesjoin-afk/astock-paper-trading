#!/usr/bin/env python3
"""Build model input and transport suggestions for the paper account."""
from __future__ import annotations

from collections import Counter
import io
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.request

import live_quote_feed as feed

RECENT_TRADES = 8
# Ranked names get the full anti-trap detail; the whole tradable market rides
# along as a compact integer table. Measured on the live model (1M window):
# 5,210 symbols -> 294 KB / 147k prompt tokens / 5-7 s, and a repeat round hits
# the prompt cache, so seeing the whole market costs far less than being blind
# to it.
DIGEST_CANDIDATES = 40
MARKET_TABLE_LIMIT = 6_000
# How many held positions to describe by symbol (with live P&L).
DIGEST_POSITIONS = 12

LIVE_SYSTEM_PROMPT = """你审查 A 股模拟盘本轮风险，输出建议，由 Rust 决定能否成交。只使用输入中可核对的数据。缺失字段表示未知。quote_time_estimated=true 表示行情时间为估计。禁止编造新闻、资金流、成交量或涨跌停状态。

先判断市场广度和振幅，再看槽位、持仓与候选。change_distribution 的涨跌平家数只覆盖能比较现价与昨收的标的，sample_count 是收到的报价总数，*_bps 的 100 等于 1%。稳健看趋势持续性和回撤风险，激进看动量与趋势是否一致。输出建议作用于股票代码，不能限定到某个槽位。

- 排名只是相对强弱。弱市允许整轮不点名买入。有现金、排名靠前或贴近涨停都不能单独成为买入依据。buy 会请求内核对未持有的槽位发出明确买入信号。点名前检查趋势、日内位置、振幅、日内均价线偏离（vwap_distance_bps）、诱多风险分（bull_trap_risk）、近期诱多错题本（recent_bull_trap_lessons）、已持仓槽位和可用预算；distance_to_limit_up_bps 按昨收计算。若接近或到达涨停、破日内均价线（vwap_distance_bps <= -120）、冲高留长上影（upper_wick_bps >= 150）、bull_trap_risk >= 30、关键证据缺失，或只剩涨幅可支持，放弃点名。已有持仓的代码也可能在另一槽触发开仓。
- 检查持仓时看本轮是否有有效价格、风险是否恶化。浮亏本身不构成卖出证据。同日账本显示可卖量为 0 时，不重复给卖出建议。用 account_as_of_round_id 的日期识别账本交易日。若账本属于上一交易日，开盘 T+1 由内核滚动，可卖量以本轮内核为准。sell 只对已持仓代码提出，并陈述依据。
- blocked_symbols 限制开仓和加仓。只填有明确禁买依据的代码。同一代码只出现一次，不同时列入禁买和买入建议。证据冲突或不足时，调整值填 0，可使用空列表。风险方向明确时优先使用一个全局调整，避免重复施压。所有数字发出前检查范围。

仅输出 JSON 对象，不能有 Markdown。顶层字段固定，不要输出 slot_recommendations、suggested_orders 或 risk_flags。
- "confidence" 为 0 至 100 的整数。"bias" 为 -20 至 20 的整数，负值更谨慎。"entry_delta" 为 -15 至 15 的整数，正值提高开仓门槛（更难买）、负值降低门槛（更容易买）。看空或要求收紧时必须给正值，输出前自检：reason 说防守，entry_delta 就必须是正数。"weight_tilt_bps" 为 -1000 至 1000 的整数，表示动量相对形态的偏移。
- northbound 是沪股通/深股通当日累计净买入（亿元）及相对首个有效值的 delta。它是外资当日的真实资金方向。**你必须在 market_state.evidence 里写出这两个数值**（例如"沪股通 +379.8 亿 / 深股通 -9.3 亿"），并说明它支持还是不支持本轮的开仓松紧。它只决定整体仓位倾向，不是个股买入依据；缺失时写"未知"，绝不用成交额替代。
- market_heat 是人气榜与概念标签（前 30）。用来看"市场这轮在炒什么、我的持仓和候选属于哪个题材"：若某候选恰在人气榜且概念与当前热点一致，可作为加分；若候选概念已连续退潮或热度排名大幅下滑，不要追。热度不等于价值，不能单独作为买入依据；不要在 reason 里罗列整张榜。
- news 是本轮抓到的、标题或摘要里出现你持仓或候选代码/名称的财经快讯与个股新闻。**每轮必须先读 news，并在 reason 里点明它对本轮的影响**（无影响也要说"新闻无实质影响"）。
  · 涉及立案、处罚、虚假记载、退市风险、业绩暴雷、大额减持等重大负面时：该代码一律不得买入，并列入 blocked_symbols；若已持仓且可卖，应提出卖出，说明依据来自哪条新闻。
  · 涉及中标、重组、业绩预增、大额增持等实质利好时，只可作为加分项，仍必须通过趋势、均价线、诱多风险的检验才能点名买入。
  · 只用新闻里写明的信息判断方向，不要臆造数字；标题与摘要冲突或过于笼统时按"不确定"处理，不要为了凑理由把它说成利好或利空。
  · news 为空表示本轮没有关联新闻，不等于"没有事件"，不要据此推断。
- own_track_record 是你自己的历史成绩单，按市场状态分组：blocked_symbols.mean_bps 是你禁买后那些股票的平均涨跌（正数说明你拦错了，错过了涨幅）；named_buys.mean_bps 是你点名买入后的平均涨跌（负数说明你点错了）。cases 太少的状态不会出现，不要外推。每轮务必先读它：若某状态下你拦错的平均幅度明显为正，本轮该状态就要明显收紧禁买标准（只拦硬证据，如涨停封板、破均价线、长上影），而不是凭排名靠前就拦；若点名买入长期为负，就不要点名，让内核按自身信号执行。
- 全市场表里凡触发禁买条件的代码都要写进 "blocked_symbols"（最多 40 个），不要只挑几个。"suggestions" 最多 8 条，每条用 {"symbol":"输入中的完整代码","side":"buy|sell|hold","score_delta":0,"weight_bps":0} 的结构。score_delta 限 ±20，weight_bps 限 ±2000。
- "reason" 用一句中文写建议和主要依据。"market_state" 用 {"evidence":"可核对的数值或代码","counterpoint":"主要反证或缺失的数据"}，两项各一句。"""


def json_line(event: str, **fields: Any) -> str:
    return json.dumps({"event": event, **fields}, ensure_ascii=False,
                      allow_nan=False, default=str)


class Log:
    """Structured JSON lines to stdout and optionally an append-only file."""

    def __init__(self, path: str | Path | None = Path(__file__).resolve().parent.parent / "work" / "live" / "intraday-loop.log",
                 *, stdout: Any = None):
        self.path = Path(path) if path is not None else None
        self.stdout = stdout or sys.stdout

    def __enter__(self) -> "Log":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def emit(self, event: str, **fields: Any) -> None:
        line = json_line(event, **fields)
        print(line, file=self.stdout, flush=True)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError as error:
                print(json_line("log_write_failed", error=str(error)),
                      file=self.stdout, flush=True)


# ---- Market digest and raw LLM transport ----------------------------------

def _pct_bps(numerator: float, denominator: float) -> int | None:
    """Integer basis points, or None when the denominator cannot support it."""
    if denominator <= 0:
        return None
    return int(round(numerator / denominator * 10_000))


def ranked_candidates(quotes: Sequence[Mapping[str, Any]], *,
                      limit: int = DIGEST_CANDIDATES) -> list[dict[str, Any]]:
    """The strongest few candidates by a transparent, self-contained score.

    This is *not* the kernel's decision score — Rust owns that and this module
    must not duplicate it. It is a ranking a human (or a model) can read: the
    names whose momentum, position in range and volume stand out. The kernel
    still decides everything; the AI only gets names to reason about instead of
    being blind to the market.

    Missing facts stay missing: a candidate without a previous close or a
    high/low range is skipped rather than scored on a substituted zero.
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for quote in quotes:
        if not isinstance(quote, Mapping) or quote.get("tradable") is False:
            continue
        symbol = quote.get("symbol")
        price = quote.get("price_cents")
        prev = quote.get("prev_close_cents")
        if not isinstance(symbol, str) or not symbol:
            continue
        if not isinstance(price, (int, float)) or not isinstance(prev, (int, float)):
            continue
        if price <= 0 or prev <= 0:
            continue
        momentum = _pct_bps(float(price) - float(prev), float(prev))
        if momentum is None:
            continue

        high, low = quote.get("high_cents"), quote.get("low_cents")
        position = None
        amplitude = None
        if (isinstance(high, (int, float)) and isinstance(low, (int, float))
                and high > low):
            position = _pct_bps(float(price) - float(low), float(high) - float(low))
            amplitude = _pct_bps(float(high) - float(low), float(prev))

        closes = quote.get("trailing_closes") or ()
        trend = None
        trend_3d = None
        dist_20d_high = None
        if isinstance(closes, (list, tuple)) and len(closes) >= 5:
            first, last = closes[-5], closes[-1]
            if isinstance(first, (int, float)) and isinstance(last, (int, float)):
                if first > 0 and last > 0:
                    trend = _pct_bps(float(last) - float(first), float(first))
            if len(closes) >= 3:
                c3 = closes[-3]
                if isinstance(c3, (int, float)) and c3 > 0:
                    trend_3d = _pct_bps(float(price) - float(c3), float(c3))
            high_20d = max((float(c) for c in closes[-20:] if isinstance(c, (int, float)) and c > 0), default=0.0)
            if high_20d > 0:
                dist_20d_high = _pct_bps(float(price) - high_20d, high_20d)

        open_c = quote.get("open_cents")
        open_mom = None
        upper_wick = None
        if isinstance(open_c, (int, float)) and open_c > 0:
            open_mom = _pct_bps(float(price) - float(open_c), float(open_c))
            if isinstance(high, (int, float)) and high > 0:
                body_top = max(float(price), float(open_c))
                if high > body_top:
                    upper_wick = _pct_bps(float(high) - body_top, float(open_c))

        vol = quote.get("volume")
        amt = quote.get("amount_cents")
        vwap_dist = None
        if isinstance(vol, (int, float)) and isinstance(amt, (int, float)) and vol > 0 and amt > 0:
            # amount_cents is money, volume is shares, price is cents: the
            # cents cancel, so turnover price = amount / volume in one unit.
            # Scaling either side by 100 produced a fake -9,999 bps gap.
            vwap = float(amt) / float(vol)
            if vwap > 0 and 0.2 <= vwap / float(price) <= 5.0:
                vwap_dist = _pct_bps(float(price) - vwap, vwap)

        bull_trap_risk = 0
        trap_flags: list[str] = []
        if vwap_dist is not None and vwap_dist <= -120:
            bull_trap_risk += 35
            trap_flags.append("破日内均价线(VWAP)")
        if upper_wick is not None and upper_wick >= 150:
            bull_trap_risk += 30
            trap_flags.append("长上影冲高回落")
        if momentum >= 300 and open_mom is not None and open_mom <= -50:
            bull_trap_risk += 25
            trap_flags.append("假阳真阴(高开低走)")
        if trend_3d is not None and trend_3d >= 1800 and (position is not None and position < 7500):
            bull_trap_risk += 25
            trap_flags.append("3日连涨高位派发")
        bull_trap_risk = min(100, bull_trap_risk)

        # Transparent blend: momentum dominates, intraday position confirms,
        # trend adds persistence. Components that are absent contribute zero
        # (a missing fact is not a zero fact).
        score = (
            (momentum or 0) / 25.0
            + ((position - 5_000) / 250.0 if position is not None else 0.0)
            + ((trend or 0) / 100.0)
        )
        entry: dict[str, Any] = {
            "symbol": symbol,
            "momentum_bps": momentum,
            "score": int(round(score)),
        }
        name = quote.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()
        if position is not None:
            entry["position_in_range_bps"] = position
        if amplitude is not None:
            entry["amplitude_bps"] = amplitude
        if trend is not None:
            entry["trend_5d_bps"] = trend
        if trend_3d is not None:
            entry["cum_gain_3d_bps"] = trend_3d
        if dist_20d_high is not None:
            entry["dist_20d_high_bps"] = dist_20d_high
        if vwap_dist is not None:
            entry["vwap_distance_bps"] = vwap_dist
        if upper_wick is not None:
            entry["upper_wick_bps"] = upper_wick
        # Always state the risk score, including a real zero: the prompt asks
        # the model to reject >= 30, and an absent field reads as "unknown" and
        # silently disables that rule. Flags stay conditional (they describe
        # which signals fired).
        entry["bull_trap_risk"] = bull_trap_risk
        if trap_flags:
            entry["bull_trap_flags"] = trap_flags
        limit_up = quote.get("limit_up_cents")
        if type(limit_up) is int and limit_up > 0:
            entry["distance_to_limit_up_bps"] = _pct_bps(limit_up - price, prev)
        scored.append((score, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored[:limit]]


def held_positions(account: Any, quotes: Sequence[Mapping[str, Any]], *,
                   limit: int = DIGEST_POSITIONS) -> list[dict[str, Any]]:
    """Mark holdings using only usable prices from this round's snapshot."""
    out: list[dict[str, Any]] = []
    if not isinstance(account, Mapping):
        return out
    marks = {quote["symbol"]: quote["price_cents"] for quote in quotes
             if isinstance(quote, Mapping)
             and isinstance(quote.get("symbol"), str)
             and type(quote.get("price_cents")) is int and quote["price_cents"] > 0
             and quote.get("tradable") is not False}
    for item in account.get("positions") or []:
        if not isinstance(item, Mapping):
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        quantity = item.get("quantity")
        cost = item.get("avg_cost_cents")
        last = marks.get(symbol)
        entry: dict[str, Any] = {
            "symbol": symbol,
            "slot": item.get("slot"),
            "quantity": quantity,
            "avg_cost_cents": cost,
            "sellable_quantity": item.get("sellable_quantity"),
        }
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()
        if last is not None:
            entry["last_price_cents"] = last
            entry["mark_source"] = "current_snapshot"
            if isinstance(cost, (int, float)) and cost > 0:
                entry["pnl_bps"] = _pct_bps(float(last) - float(cost), float(cost))
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def compute_market_sentiment(quotes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Real-time cross-sectional market sentiment, plus the surge-fade share.

    ``surge_fade_rate_bps`` is the share of names that reached +6% intraday and
    then gave back at least 2% from that high. It is a *price* statistic, not a
    limit-up board statistic: A-share 炸板率 counts only names that actually
    touched their limit-up price and then opened, which this measure does not
    know and cannot know from a quote snapshot. The field is named for what it
    measures so no reader can mistake one for the other, and it is one input to
    the temperature, never a verdict about 主力 intent.
    """
    total = up = down = flat = strong = weak = limit_up = limit_down = high_surge = surge_fade = 0
    for q in quotes:
        if not isinstance(q, Mapping) or q.get("tradable") is False:
            continue
        price = q.get("price_cents")
        prev = q.get("prev_close_cents")
        if not isinstance(price, (int, float)) or not isinstance(prev, (int, float)) or price <= 0 or prev <= 0:
            continue
        total += 1
        chg_bps = int(round((float(price) - float(prev)) / float(prev) * 10_000))
        if chg_bps > 0:
            up += 1
        elif chg_bps < 0:
            down += 1
        else:
            flat += 1
        if chg_bps >= 500:
            strong += 1
        elif chg_bps <= -500:
            weak += 1
        lu = q.get("limit_up_cents")
        ld = q.get("limit_down_cents")
        # The 9.5% fallback is only for a row with no published limit. Applied
        # to a name whose real band is 5% (ST) or 30% (北交所) it invents a
        # limit-up: on 2026-11-23 it reported 237 against 142 at the ceiling.
        if (isinstance(lu, int) and lu > 0 and price >= lu) or (
                not (isinstance(lu, int) and lu > 0) and chg_bps >= 950):
            limit_up += 1
        if (isinstance(ld, int) and ld > 0 and price <= ld) or (
                not (isinstance(ld, int) and ld > 0) and chg_bps <= -950):
            limit_down += 1
        high = q.get("high_cents")
        if isinstance(high, (int, float)) and high > prev:
            high_bps = int(round((float(high) - float(prev)) / float(prev) * 10_000))
            if high_bps >= 600:
                high_surge += 1
                pullback_bps = int(round((float(high) - float(price)) / max(float(high), 1.0) * 10_000))
                if pullback_bps >= 200:
                    surge_fade += 1
    if total == 0:
        return {"score": 50, "label": "暂无数据", "up_ratio_bps": 0,
                "surge_fade_rate_bps": 0, "surge_fade_count": 0,
                "surge_fade_basis": "冲高≥6%且较日内高点回落≥2%的占比，不是炸板率"}
    up_ratio_bps = int(round(up * 10_000 / total))
    surge_fade_rate_bps = int(round(surge_fade * 10_000 / high_surge)) if high_surge > 0 else 0
    breadth_term = max(-20, min(20, (up_ratio_bps - 5000) // 200))
    tail_term = max(-15, min(15, (strong - weak) * 180 // total))
    limit_term = max(-10, min(10, (limit_up - limit_down) * 250 // total))
    fade_penalty = max(0, min(15, surge_fade_rate_bps // 500))
    score = max(0, min(100, 50 + breadth_term + tail_term + limit_term - fade_penalty))
    label = ("情绪冰点" if score < 25 else "情绪偏冷" if score < 42
             else "情绪中性" if score < 60 else "情绪偏暖" if score < 80 else "情绪亢奋")
    return {
        "score": score,
        "label": label,
        "up_ratio_bps": up_ratio_bps,
        "strong_count": strong,
        "weak_count": weak,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "surge_fade_count": surge_fade,
        "surge_fade_rate_bps": surge_fade_rate_bps,
        # Spelled out in the payload as well as the prompt: this is the field
        # most likely to be read as 炸板率, and reading it that way is wrong.
        "surge_fade_basis": ("冲高≥6%且较日内高点回落≥2%的只数占全部冲高≥6%只数的比例；"
                             "不等于炸板率（炸板率只统计触板后开板），不能单独据以判定主力意图"),
    }


def build_slot_candidates(quotes: Sequence[Mapping[str, Any]], *, limit: int = 30) -> dict[str, list[dict[str, Any]]]:
    """Separate, anti-bull-trap candidate pools for steady and aggressive slots."""
    steady_pool: list[tuple[float, dict[str, Any]]] = []
    aggr_pool: list[tuple[float, dict[str, Any]]] = []
    for item in ranked_candidates(quotes, limit=400):
        mom = item.get("momentum_bps") or 0
        pos = item.get("position_in_range_bps")
        amp = item.get("amplitude_bps")
        dist_lu = item.get("distance_to_limit_up_bps")
        trend = item.get("trend_5d_bps") or 0
        trap_risk = item.get("bull_trap_risk") or 0
        vwap_dist = item.get("vwap_distance_bps")
        if dist_lu is not None and dist_lu < 180:
            continue
        if pos is not None and pos < 6000:
            continue
        if vwap_dist is not None and vwap_dist < -100:
            continue
        if 30 <= mom <= 450 and (amp is None or amp <= 500) and trend >= 0 and trap_risk <= 25:
            steady_pool.append((trend * 0.5 + mom * 0.3 - trap_risk * 2.0, item))
        if 150 <= mom <= 650 and (pos is None or pos >= 7000) and (dist_lu is None or dist_lu >= 220) and trap_risk <= 35:
            aggr_pool.append((mom * 0.6 + trend * 0.4 - trap_risk * 2.0, item))
    steady_pool.sort(key=lambda x: x[0], reverse=True)
    aggr_pool.sort(key=lambda x: x[0], reverse=True)
    return {
        "steady": [row for _, row in steady_pool[:limit]],
        "aggressive": [row for _, row in aggr_pool[:limit]],
    }


def extract_bull_trap_lessons(
    held: Sequence[Mapping[str, Any]],
    trades: Any,
    quotes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Distill recent losing positions and stop-loss fills into concrete negative examples for AI."""
    lessons: list[dict[str, Any]] = []
    quote_map = {
        q["symbol"]: q
        for q in quotes
        if isinstance(q, Mapping) and isinstance(q.get("symbol"), str)
    }
    seen: set[str] = set()
    for pos in held:
        sym = pos.get("symbol")
        pnl = pos.get("pnl_bps")
        if not isinstance(sym, str) or not isinstance(pnl, (int, float)) or pnl > -300:
            continue
        seen.add(sym)
        q = quote_map.get(sym, {})
        lessons.append({
            "symbol": sym,
            "name": pos.get("name") or q.get("name"),
            "slot": pos.get("slot"),
            "type": "当前持仓被套",
            "pnl_bps": int(pnl),
            "lesson": f"持仓浮亏 {pnl / 100:.2f}%，切勿在弱势或跌破均价线时盲目补仓摊平，优先去弱留强",
        })
        if len(lessons) >= 5:
            return lessons
    if isinstance(trades, list):
        for tr in trades[:40]:
            if not isinstance(tr, Mapping):
                continue
            sym = tr.get("symbol")
            side = tr.get("side")
            reason = str(tr.get("reason") or "")
            if not isinstance(sym, str) or sym in seen:
                continue
            if side == "sell" and ("stop" in reason or "exit" in reason):
                seen.add(sym)
                lessons.append({
                    "symbol": sym,
                    "name": tr.get("name"),
                    "slot": tr.get("slot"),
                    "type": "近期止损教训",
                    "round_id": tr.get("round_id"),
                    "lesson": f"曾因 {reason} 触发止损出局，说明早盘/高位追涨极易遭遇T+1次日低开闷杀，本轮严禁对同类冲高回落股追高",
                })
                if len(lessons) >= 5:
                    break
    return lessons


MARKET_TABLE_COLUMNS = (
    "symbol", "momentum_bps", "position_in_range_bps", "amplitude_bps",
    "vwap_distance_bps", "upper_wick_bps", "bull_trap_risk", "distance_to_limit_up_bps",
)


def market_table(quotes: Sequence[Mapping[str, Any]], *,
                 limit: int = MARKET_TABLE_LIMIT) -> dict[str, Any]:
    """Every tradable symbol as one compact integer row, strongest first.

    Coding the columns once keeps a full-market view inside a fraction of the
    window instead of repeating keys 5,210 times. Null means unknown; a symbol
    missing a previous close is absent rather than scored on a fake zero, so the
    model cannot mistake an unreported price for a flat one.
    """
    rows = [[item.get(column) for column in MARKET_TABLE_COLUMNS]
            for item in ranked_candidates(quotes, limit=limit)]
    return {
        "cols": list(MARKET_TABLE_COLUMNS),
        "rows": rows,
        "note": "全市场可交易标的，按动量降序；null 表示该字段未知。",
    }


def market_digest(snapshot: Mapping[str, Any], *, account: Any,
                  trades: Any) -> dict[str, Any]:
    """A state summary the observer can actually reason about.

    It carries market breadth, the two slot books, recent fills, the ranked
    candidate names and the held positions with live P&L. It still carries no
    policy: no threshold, no order, no position size — those stay in Rust.
    """
    quotes = snapshot.get("quotes") or []
    up = down = flat = 0
    amplitudes: list[float] = []
    for quote in quotes:
        if not isinstance(quote, Mapping) or quote.get("tradable") is False:
            continue
        price = quote.get("price_cents")
        prev = quote.get("prev_close_cents")
        if isinstance(price, (int, float)) and isinstance(prev, (int, float)) and prev > 0:
            if price > prev:
                up += 1
            elif price < prev:
                down += 1
            else:
                flat += 1
        high, low = quote.get("high_cents"), quote.get("low_cents")
        if (isinstance(high, (int, float)) and isinstance(low, (int, float))
                and prev and prev > 0):
            amplitudes.append((float(high) - float(low)) / float(prev))
    slots: list[dict[str, Any]] = []
    if isinstance(account, Mapping):
        for item in account.get("slots") or []:
            if isinstance(item, Mapping):
                slots.append({
                    "slot": item.get("slot"),
                    "cash_cents": item.get("cash_cents"),
                    "equity_cents": item.get("equity_cents"),
                    "positions": item.get("positions"),
                })
    recent: list[dict[str, Any]] = []
    if isinstance(trades, list):
        # The kernel returns the newest fills first.
        for item in trades[:RECENT_TRADES]:
            if isinstance(item, Mapping):
                recent.append({
                    "round_id": item.get("round_id"),
                    "slot": item.get("slot"),
                    "symbol": item.get("symbol"),
                    "side": item.get("side"),
                    "quantity": item.get("quantity"),
                    "price_cents": item.get("price_cents"),
                    "reason": item.get("reason"),
                })
    held = held_positions(account, quotes)
    return {
        "quote_time": snapshot.get("quote_time"),
        "quote_time_estimated": bool(snapshot.get("quote_time_estimated")),
        "market_table": market_table(quotes),
        "sample_count": len(quotes),
        "change_distribution": {"up": up, "down": down, "flat": flat},
        "average_amplitude_bps": (round(sum(amplitudes) * 10_000 / len(amplitudes))
                                  if amplitudes else None),
        "account_as_of_round_id": account.get("as_of_round_id") if isinstance(account, Mapping) else None,
        "slots": slots,
        "held_positions": held,
        "ranked_candidates": ranked_candidates(quotes),
        "slot_candidates": build_slot_candidates(quotes),
        "market_sentiment": compute_market_sentiment(quotes),
        "recent_bull_trap_lessons": extract_bull_trap_lessons(held, trades, quotes),
        "recent_trades": recent,
    }


def _parse_model_json(content: Any) -> Any:
    """Parse the model's answer as a JSON object.

    A completion can arrive wrapped in a ```json fence, or truncated by the
    server's token cap mid-object. Both are recoverable without inventing
    values: strip the fence, then close any braces/brackets the truncation left
    open. Anything that still does not parse raises, and the round runs with
    neutral advice — the same fail-closed path as a transport error.
    """
    if not isinstance(content, str):
        raise ValueError("model content is not text")
    text = content.strip()
    if text.startswith("```"):
        # ```json\n{...}\n```  ->  {...}
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = _close_truncated_json(text)
        if repaired is None:
            raise
        return json.loads(repaired)


def _close_truncated_json(text: str) -> str | None:
    """Close a truncated JSON object by dropping the incomplete tail.

    Only structural repair: the last complete key/value pair is kept and the
    open containers are closed. No field is added, guessed or defaulted here;
    Rust still decides every bound and a missing field stays missing.
    """
    depth_square = depth_curly = 0
    in_string = escaped = False
    last_safe = None
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth_curly += 1
        elif char == "}":
            depth_curly -= 1
            if depth_curly == 0 and depth_square == 0:
                return text[: index + 1]
        elif char == "[":
            depth_square += 1
        elif char == "]":
            depth_square -= 1
        elif char == "," and depth_square == 0:
            # Only a comma closes a complete key/value pair at this depth; a
            # colon would leave a bare key behind and produce invalid JSON.
            last_safe = index
    if depth_curly <= 0:
        return None
    if last_safe is None:
        return None
    head = text[: last_safe].rstrip().rstrip(",")
    # Recount the open containers in the kept prefix before closing them.
    square = curly = 0
    in_str = esc = False
    for char in head:
        if in_str:
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
    return head + "]" * max(square, 0) + "}" * max(curly, 0)


def _provider_tokens(payload: Any) -> dict[str, int | None]:
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    usage = usage if isinstance(usage, Mapping) else {}
    completion = usage.get("completion_tokens_details")
    completion = completion if isinstance(completion, Mapping) else {}
    prompt = usage.get("prompt_tokens_details")
    prompt = prompt if isinstance(prompt, Mapping) else {}

    def number(*values: Any) -> int | None:
        return next((value for value in values if type(value) is int and value >= 0), None)

    return {
        "prompt_tokens": number(usage.get("prompt_tokens"), usage.get("input_tokens")),
        "completion_tokens": number(usage.get("completion_tokens"), usage.get("output_tokens")),
        "total_tokens": number(usage.get("total_tokens")),
        "reasoning_tokens": number(completion.get("reasoning_tokens"),
                                   usage.get("completion_thinking_tokens"),
                                   usage.get("reasoning_tokens")),
        "cached_prompt_tokens": number(prompt.get("cached_tokens"),
                                       usage.get("prompt_cache_hit_tokens"),
                                       usage.get("cache_read_input_tokens"),
                                       usage.get("cached_tokens")),
    }


# Keys that hold the whole market table and the account books. They change only
# when a position or a quote changes, and putting them AFTER the volatile block
# would defeat the prompt cache, so they lead.
# Only facts that are byte-identical across rounds. The slot books carry
# cash/equity, which moves with every fill, so they stay volatile.
STABLE_DIGEST_KEYS = ("resident_memory", "market_table", "own_track_record",
                      "recent_bull_trap_lessons", "account_as_of_round_id")


def attach_track_record(digest: dict[str, Any], card: Mapping[str, Any] | None) -> dict[str, Any]:
    """Give the observer its own measured track record.

    Without this the model reasons from first principles every round and cannot
    notice that, say, its blocks in `mixed` sessions gave up 144 bps on average
    while its named buys lost 122. The numbers come from the committed ledger,
    scored at the same session's close; a regime below the case floor is omitted
    rather than shown as a confident-looking average of a handful of names.
    """
    if card:
        digest["own_track_record"] = card
    return digest


def attach_northbound(digest: dict[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    """Add the published northbound net-buy series for this morning."""
    if block:
        digest["northbound"] = block
    return digest


def attach_hot(digest: dict[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    """Add the market heat list and its concept tags, when the source returned it."""
    if block and block.get("items"):
        digest["market_heat"] = block
    return digest


def attach_news(digest: dict[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    """Add the symbol-linked headlines, when any were found for this round."""
    if block and block.get("items"):
        digest["news"] = block
    return digest


def attach_resident_memory(digest: dict[str, Any], block: str | None) -> dict[str, Any]:
    """Inject the small always-on memory tier, bounded by the writer.

    This is the resident layer of the three-tier design: a few dozen lines that
    ride in every round, unlike ``own_track_record`` which is the searchable
    evidence and unlike the trial ledger which is only read when deciding a
    change.
    """
    if block:
        digest["resident_memory"] = block
    return digest


def digest_messages(system_prompt: str, digest: Mapping[str, Any]) -> list[dict[str, str]]:
    """Order the payload so the cached prefix is as long as possible.

    A provider caches the longest common prefix of the prompt. Serialising the
    dict in its natural order put the minute-by-minute fields first, so every
    round diverged in the opening kilobytes and the measured hit rate was 0.2%.
    Stable structure leads; the volatile tail follows.
    """
    stable = {key: digest[key] for key in STABLE_DIGEST_KEYS if key in digest}
    volatile = {key: value for key, value in digest.items() if key not in stable}
    # ``sort_keys`` would alphabetise the wrapper and put "round" ahead of
    # "stable", shortening the cached prefix by the whole market table. Sort
    # inside each block instead and keep the two-block order explicit.
    payload = ('{"stable":' + json.dumps(dict(sorted(stable.items())), ensure_ascii=False)
               + ',"round":' + json.dumps(dict(sorted(volatile.items())), ensure_ascii=False)
               + '}')
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": payload},
    ]


def raw_advice(digest: Mapping[str, Any], *, base_url: str, api_key: str,
               model: str, timeout: float = 30.0,
               opener: Callable[..., Any] = urllib.request.urlopen,
               log: Log | None = None, round_id: str | None = None,
               monotonic: Callable[[], float] = time.monotonic,
               system_prompt: str = LIVE_SYSTEM_PROMPT) -> dict[str, Any] | None:
    """Transport raw suggestions to Rust, retrying a confirmed empty answer once."""
    if not base_url or not api_key:
        if log:
            log.emit("ai_unavailable", round_id=round_id,
                     reason="missing_llm_credentials", attempts=0)
        return None
    started = monotonic()
    retry_limit = min(timeout, 15.0)
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 1_200,
        "response_format": {"type": "json_object"},
        # Measured on the live model: with reasoning enabled the 2,000-token
        # budget was spent entirely on hidden reasoning and the reply was empty
        # every round, so the scheduler paid for a retry each time. Asking for
        # the JSON directly answers in ~3s for ~4.9k tokens instead.
        "thinking": {"type": "disabled"},
    }
    reason = "empty_completion"
    attempts = 0
    for attempt in (1, 2):
        request_timeout = timeout
        if attempt == 2:
            elapsed = monotonic() - started
            remaining = timeout + retry_limit - elapsed
            if remaining < 1.0:
                reason = "reasoning_retry_budget_exhausted"
                break
            request_timeout = min(retry_limit, remaining)
            # The first call already asked for direct JSON; the fallback is for
            # a provider that rejects that switch, so allow reasoning room.
            body.pop("thinking", None)
            body["max_tokens"] = 8_000
            if log:
                log.emit("ai_retry", round_id=round_id, reason=reason,
                         elapsed_ms=round(elapsed * 1_000))
        attempts = attempt
        request_at = monotonic()
        usage = _provider_tokens(None)
        status = "request_error"
        try:
            if attempt == 1:
                body["messages"] = digest_messages(system_prompt, digest)
            request = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                method="POST")
            with opener(request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            usage = _provider_tokens(payload)
            status = "invalid_response"
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if isinstance(content, str) and content.strip():
                parsed = _parse_model_json(content)
                if not isinstance(parsed, dict):
                    raise ValueError("model answer is not a JSON object")
                status = "completed"
                if log:
                    log.emit("ai_completed", round_id=round_id, attempts=attempts,
                             elapsed_ms=round((monotonic() - started) * 1_000))
                return {"model": model, "raw_advice": parsed}
            finish = choice.get("finish_reason")
            status = "empty_completion"
            reasoning = usage["reasoning_tokens"]
            safe_finish = finish if finish in ("length", "stop", "content_filter", None) else "other"
            reason = f"empty_completion:finish_reason={safe_finish}:reasoning_tokens={reasoning}"
            if (attempt == 1 and model.casefold().startswith("deepseek-")
                    and finish == "length" and reasoning):
                continue
        except urllib.error.HTTPError as error:
            reason = f"HTTPError: {error.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                KeyError, IndexError, TypeError, AttributeError) as error:
            reason = type(error).__name__
        finally:
            if log:
                log.emit("ai_usage", round_id=round_id, attempt=attempt, model=model,
                         status=status, elapsed_ms=round((monotonic() - request_at) * 1_000),
                         **usage)
        break
    if log:
        log.emit("ai_unavailable", round_id=round_id, reason=reason,
                 attempts=attempts, elapsed_ms=round((monotonic() - started) * 1_000))
    return None


def main() -> None:
    # Bound the whole exchange, even if a peer keeps a socket alive by
    # trickling data before each urllib socket timeout.
    watchdog = threading.Timer(55, os._exit, args=(124,))
    watchdog.daemon = True
    watchdog.start()
    try:
        request = json.load(sys.stdin.buffer)
        telemetry = io.StringIO()
        digest = market_digest(request["snapshot"], account=request["account"],
                               trades=request["trades"])
        advice = raw_advice(digest, base_url=os.getenv("ASTOCK_LLM_BASE", feed.DEFAULT_LLM_BASE),
                            api_key=os.getenv("ASTOCK_LLM_KEY", ""),
                            model=os.getenv("ASTOCK_LLM_MODEL", feed.DEFAULT_LLM_MODEL),
                            log=Log(None, stdout=telemetry), round_id=request["round_id"])
        print(json.dumps({"advice": advice, "events": [json.loads(line)
                        for line in telemetry.getvalue().splitlines()]}, allow_nan=False))
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    main()
