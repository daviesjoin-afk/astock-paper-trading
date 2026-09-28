"""Trailing-only, derived market regime labels for R30 reports."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def classify_sessions(bars: Sequence[Mapping[str, Any]], sessions: Sequence[str],
                      policy: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Classify each session using only its own and earlier benchmark closes."""
    benchmark = policy.get("benchmark_symbol")
    by_session = {str(row.get("session")): row for row in bars
                  if row.get("code") == benchmark and row.get("session") in sessions}
    output = {}
    trend_window = policy["trend_window_sessions"]
    volatility_window = policy["volatility_window_sessions"]
    for index, session in enumerate(sessions):
        trend = volatility = "unknown"
        trend_rows = [by_session.get(day) for day in sessions[max(0, index - trend_window + 1):index + 1]]
        vol_rows = [by_session.get(day) for day in sessions[max(0, index - volatility_window + 1):index + 1]]
        if len(trend_rows) == trend_window and all(row is not None for row in trend_rows):
            closes = [row.get("close") for row in trend_rows]
            if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(float(value)) and value > 0 for value in closes):
                change = float(closes[-1]) / float(closes[0]) - 1
                trend = ("bull" if change >= policy["bull_threshold"] else
                         "bear" if change <= -policy["bear_threshold"] else "sideways")
        if len(vol_rows) == volatility_window and all(row is not None for row in vol_rows):
            closes = [row.get("close") for row in vol_rows]
            if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(float(value)) and value > 0 for value in closes):
                returns = [float(closes[pos]) / float(closes[pos - 1]) - 1
                           for pos in range(1, len(closes))]
                mean = sum(returns) / len(returns)
                realized_vol = math.sqrt(sum((value - mean) ** 2 for value in returns) / len(returns))
                volatility = ("high" if realized_vol >= policy["high_vol_threshold"] else
                              "low" if realized_vol <= policy["low_vol_threshold"] else "middle")
        output[session] = {"trend_regime": trend, "volatility_regime": volatility}
    return output


def summarize_trace(trace: Sequence[Mapping[str, Any]],
                    regimes: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """Aggregate actual per-session trace rows independently by regime axis."""
    result: dict[str, Any] = {"trend": {}, "volatility": {}}
    for axis, key in (("trend", "trend_regime"), ("volatility", "volatility_regime")):
        labels = sorted({regimes.get(str(row.get("session")), {}).get(key, "unknown")
                         for row in trace})
        for label in labels:
            rows = [row for row in trace if regimes.get(str(row.get("session")), {}).get(key) == label]
            returns = [row["daily_return"] for row in rows if row.get("daily_return") is not None]
            equity = 1.0
            peak = 1.0
            drawdown = 0.0
            for value in returns:
                equity *= 1 + value
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1)
            mean = sum(returns) / len(returns) if returns else None
            vol = (math.sqrt(sum((value - mean) ** 2 for value in returns) / len(returns))
                   if returns and mean is not None else None)
            result[axis][label] = {
                "sessions": len(rows), "return": equity - 1 if returns else None,
                "drawdown": drawdown if returns else None, "volatility": vol,
                "turnover": sum(float(row.get("turnover_delta") or 0) for row in rows),
                "trade_count": sum(int(row.get("trade_count_delta") or 0) for row in rows),
                "cost": sum(float(row.get("cost_delta") or 0) for row in rows),
                "exposure": (sum(float(row.get("exposure") or 0) for row in rows) / len(rows)
                             if rows else None),
            }
    return result
