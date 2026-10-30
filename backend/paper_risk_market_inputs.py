"""Risk evidence acquisition separated from transaction and decision owners."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import paper_timing as PTM


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
    position_codes = sorted({str(item["code"]) for item in positions})
    codes = sorted(set(position_codes) | set(candidate_codes or ()))
    names = {item["code"]: item.get("name") or item["code"] for item in positions}

    def _trajectories():
        try:
            return ports.trajectories(position_codes, asof_day=day)
        except Exception as exc:
            return {code: {"status": "source_unavailable", "code": code,
                           "error": type(exc).__name__, "score_applied": False}
                    for code in position_codes}

    # 新闻与资金流轨迹只依赖代码集合，和报价互不依赖。旧实现把它们串在报价
    # 之后依次等待；这里让两条支路并行，报价仍留在调用线程执行——轮内报价
    # 复用是 thread-local，搬进工作线程会把复用池丢在线程里。
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="risk-input") as pool:
        news_future = pool.submit(PTM.bind_worker(ports.news), names)
        trajectory_future = pool.submit(PTM.bind_worker(_trajectories))
        quote_map = ports.quotes(codes, asof_date=day)
        news = news_future.result()
        flow_trajectory_map = trajectory_future.result()
    news_meta = dict(ports.news_meta())
    return {"market_context": market_context, "quote_map": quote_map, "news": news,
            "news_meta": news_meta, "flow_trajectory_map": flow_trajectory_map}
