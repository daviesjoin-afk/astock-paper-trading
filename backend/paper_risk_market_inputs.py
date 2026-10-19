"""Risk evidence acquisition separated from transaction and decision owners."""
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RiskMarketInputs:
    market_context: Callable[..., Any]
    quotes: Callable[..., Any]
    news: Callable[..., Any]
    news_meta: Callable[..., Any]
    trajectories: Callable[..., Any]


def load(*, day, positions, candidate_codes, ports: RiskMarketInputs):
    market_context = ports.market_context(day)
    if not positions:
        return {"market_context": market_context, "quote_map": {}, "news": [],
                "news_meta": dict(ports.news_meta()), "flow_trajectory_map": {}}
    codes = sorted({str(item["code"]) for item in positions} | set(candidate_codes or ()))
    quote_map = ports.quotes(codes, asof_date=day)
    news = ports.news({item["code"]: item.get("name") or item["code"] for item in positions})
    news_meta = dict(ports.news_meta())
    try:
        flow_trajectory_map = ports.trajectories(codes, asof_day=day)
    except Exception:
        flow_trajectory_map = {}
    return {"market_context": market_context, "quote_map": quote_map, "news": news,
            "news_meta": news_meta, "flow_trajectory_map": flow_trajectory_map}
