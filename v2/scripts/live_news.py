#!/usr/bin/env python3
"""Market news for the intraday observer, built on the source layer we already ship.

The upstream ``astock_public`` skill (simonlin1212/a-stock-data) already exposes
working news endpoints; this module only calls them and does the one thing they
do not: attach a news item to the symbols it actually mentions, so the observer
sees "600000.SH: <headline>" instead of a wall of unrelated market chatter.

Nothing is invented. A headline is kept only when a held or candidate symbol's
name, or its code, appears in the title or summary; the source time is preserved
verbatim and no sentiment is inferred from keywords -- that judgement belongs to
the model, which can read the text.

A record only becomes a usable fact when both of those hold *and* its timestamp
can be read and does not lie after the moment of the fetch. Two failure modes
are real with these wires: 东财 returns rows whose ``time`` is unparseable, and
some rows carry a timestamp later than the fetch itself. Neither can describe
the round being decided, so both are kept as labelled rejections instead of
facts.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Iterable, Mapping, Sequence

# A wire item older than this no longer describes the round being decided.
FRESH_MINUTES = 90
# Upstream ``hsgt_realtime`` column names. The contract is hgt = 沪股通
# (Shanghai) and sgt = 深股通 (Shenzhen); naming them here keeps the direction
# from being re-derived (and once re-derived backwards) at each call site.
HGT_KEY = "hgt_yi"
SGT_KEY = "sgt_yi"

# A source stamp is minute-resolution and the local clock can differ from the
# publisher's by seconds; anything beyond this is a real future timestamp, not
# clock noise.
FUTURE_TOLERANCE = timedelta(minutes=2)
# Bounds the prompt: the observer reasons over a digest, not a news feed.
MAX_ITEMS = 24
MAX_TITLE_CHARS = 120
# How many labelled rejections to show, so "we dropped something" stays
# auditable without shipping the whole discarded feed.
MAX_REJECTED = 5
SHANGHAI = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            stamp = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return stamp.replace(tzinfo=SHANGHAI)
    return None


def _clip(text: Any, limit: int = MAX_TITLE_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _safe_call(client: Any, name: str, kwargs: Mapping[str, Any]) -> list[Any]:
    try:
        return client.call(name, **kwargs) or []
    except Exception:  # noqa: BLE001 - one wire down must not blind the other
        return []


def fetch_headlines(client: Any, *, page_size: int = 50) -> list[dict[str, Any]]:
    """Both wires, newest first. A failing endpoint is skipped, not fatal.

    Two independent providers are worth having because either can go dark; the
    upstream notes already record 财联社's old API being retired once. When both
    fail the caller gets an empty list and says so, rather than a stale cache
    presented as current news.
    """
    wires = (("cls_telegraph", {"page_size": page_size}),
             ("eastmoney_global_news", {"page_size": page_size}))
    # The two wires are independent, so they are fetched together: sequentially
    # they cost their sum, and this runs inside a three-minute round budget.
    with ThreadPoolExecutor(max_workers=len(wires)) as pool:
        fetched = list(pool.map(lambda wire: _safe_call(client, *wire), wires))
    items: list[dict[str, Any]] = []
    for (name, _kwargs), rows in zip(wires, fetched):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            title = _clip(row.get("title"))
            if not title:
                continue
            items.append({
                "title": title,
                "summary": _clip(row.get("summary") or row.get("content")),
                "time": row.get("time"),
                "wire": name,
            })
    items.sort(key=lambda item: _parse_time(item.get("time")) or datetime.min.replace(tzinfo=SHANGHAI),
               reverse=True)
    return items


def _symbol_names(snapshot: Mapping[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for quote in snapshot.get("quotes") or []:
        if not isinstance(quote, Mapping):
            continue
        symbol, name = quote.get("symbol"), quote.get("name")
        if isinstance(symbol, str) and symbol and isinstance(name, str):
            clean = name.strip()
            # Single-character names and placeholders match too much text to be
            # evidence; the code still links those symbols.
            if len(clean) >= 2 and clean not in ("-", "None"):
                names[symbol] = clean
    return names


def _code_of(symbol: str) -> str:
    """The six-digit code without the exchange suffix."""
    return symbol.split(".", 1)[0]


def _mentions(text: str, symbol: str, name: str | None) -> bool:
    """Whether a headline text really points at this symbol.

    A name needs two characters: a one-character name ("涨") matches half the
    market. The code or the full symbol always counts.
    """
    if not isinstance(text, str) or not text:
        return False
    code = _code_of(symbol)
    if code and code in text:
        return True
    if symbol and symbol.upper() in text.upper():
        return True
    return bool(isinstance(name, str) and len(name.strip()) >= 2
                and name.strip() in text)


def _text_of(item: Mapping[str, Any]) -> str:
    return f"{item.get('title') or ''} {item.get('summary') or ''}"


def link_symbols(items: Sequence[Mapping[str, Any]], names: Mapping[str, str],
                 *, watch: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Attach each headline to the symbols it names, and drop the rest.

    A hit needs the full symbol, its code, or a name of at least two characters.
    Substring matching on a one-character name would tag half the market.
    """
    wanted = set(watch)
    candidates = list(dict.fromkeys(list(names) + sorted(wanted)))
    linked: list[dict[str, Any]] = []
    for item in items:
        text = _text_of(item)
        hits = [symbol for symbol in candidates
                if _mentions(text, symbol, names.get(symbol))]
        if wanted:
            hits = [symbol for symbol in hits if symbol in wanted]
        if not hits:
            continue
        linked.append({**item, "symbols": sorted(set(hits))[:6]})
    return linked


def fetch_symbol_news(client: Any, symbol: str, *, name: str | None = None,
                      page_size: int = 10) -> list[dict[str, Any]]:
    """News published about one symbol, straight from the per-stock endpoint.

    Market wires carry mostly macro chatter, so a held name can go days without
    appearing in them. Asking the per-symbol endpoint is the source's own way to
    answer "what happened to this stock", and it costs one request.

    The endpoint is a keyword search on the code, so 东财 returns weakly related
    articles: a piece that merely mentions a peer, or a market wrap-up tagged
    with the code. Querying the symbol is therefore not evidence that the text
    is about it. A row is bound to the symbol only when the title or the summary
    itself names the stock (its name, its code or its full symbol); everything
    else comes back marked ``unverified`` with an empty ``symbols`` list, so a
    caller can drop it instead of handing the model a false attribution.
    """
    try:
        rows = client.call("eastmoney_stock_news", symbol, page_size=page_size) or []
    except Exception:  # noqa: BLE001 - one symbol failing must not drop the rest
        return []
    out = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        title = _clip(row.get("title"))
        if not title:
            continue
        item = {"title": title, "summary": _clip(row.get("content")),
                "time": row.get("time"), "wire": "eastmoney_stock_news"}
        if _mentions(_text_of(item), symbol, name):
            out.append({**item, "symbols": [symbol],
                        "verification": "title_or_summary_names_symbol"})
        else:
            out.append({**item, "symbols": [],
                        "verification": "unverified_no_symbol_mention"})
    return out


def _time_verdict(item: Mapping[str, Any], current: datetime,
                  max_age: timedelta) -> str:
    """``fresh``, or the label explaining why the record cannot be a fact.

    A stamp that cannot be parsed, or that lies after the fetch, cannot describe
    the round being decided; keeping either as evidence would let a mislabelled
    upstream row steer a decision.
    """
    stamp = _parse_time(item.get("time"))
    if stamp is None:
        return "unknown_time"
    if stamp - current > FUTURE_TOLERANCE:
        return "future_time"
    if (current - stamp) > max_age:
        return "stale"
    return "fresh"


def digest_news(client: Any, snapshot: Mapping[str, Any], *,
                watch: Iterable[str] = (), now: datetime | None = None,
                page_size: int = 50, per_symbol: int = 8,
                symbol_limit: int = 6,
                per_symbol_watch: Iterable[str] | None = None) -> dict[str, Any]:
    """A bounded, fresh, symbol-linked news block for one round.

    ``watch`` is the full matching scope (holdings plus ranked candidates);
    ``per_symbol_watch`` limits the per-symbol requests to the names worth one
    (holdings), because each request costs latency inside a round budget.
    """
    current = now or _now()
    names = _symbol_names(snapshot)
    watch_list = list(dict.fromkeys(watch))
    query_list = (list(dict.fromkeys(per_symbol_watch))
                  if per_symbol_watch is not None else watch_list)[:symbol_limit]
    raw = fetch_headlines(client, page_size=page_size)
    fresh: list[dict[str, Any]] = []
    dropped = Counter()
    rejected: list[dict[str, Any]] = []

    def reject(item: Mapping[str, Any], reason: str) -> None:
        dropped[reason] += 1
        if len(rejected) < MAX_REJECTED:
            rejected.append({"title": item.get("title"), "time": item.get("time"),
                             "wire": item.get("wire"), "reason": reason})

    for item in raw:
        verdict = _time_verdict(item, current, timedelta(minutes=FRESH_MINUTES))
        if verdict != "fresh":
            reject(item, verdict)
            continue
        fresh.append(item)
    linked = link_symbols(fresh, names, watch=watch_list)
    # Per-symbol lookup, newest first, for the names we actually hold.
    seen_titles = {item["title"] for item in linked}
    extra: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(query_list)))) as pool:
        per_symbol_rows = list(pool.map(
            lambda symbol: fetch_symbol_news(client, symbol,
                                             name=names.get(symbol),
                                             page_size=per_symbol),
            query_list))
    for _symbol, rows in zip(query_list, per_symbol_rows):
        for item in rows:
            verdict = _time_verdict(item, current,
                                    timedelta(minutes=FRESH_MINUTES * 8))
            if verdict != "fresh":
                reject(item, verdict)  # wider window: a stock's own news is rarer
                continue
            if not item.get("symbols"):
                reject(item, "unverified_symbol_match")
                continue
            if item["title"] in seen_titles:
                continue
            seen_titles.add(item["title"])
            extra.append(item)
    linked = sorted(linked + extra,
                    key=lambda i: _parse_time(i.get("time")) or datetime.min.replace(tzinfo=SHANGHAI),
                    reverse=True)
    return {
        "as_of": current.isoformat(),
        "fresh_within_minutes": FRESH_MINUTES,
        "future_tolerance_minutes": int(FUTURE_TOLERANCE.total_seconds() // 60),
        "items": linked[:MAX_ITEMS],
        "counts": {"fetched": len(raw), "fresh": len(fresh),
                   "linked": len(linked), "dropped_stale": dropped["stale"],
                   "dropped_unknown_time": dropped["unknown_time"],
                   "dropped_future_time": dropped["future_time"],
                   "dropped_unverified": dropped["unverified_symbol_match"],
                   "per_symbol": len(extra),
                   "watch": len(watch_list), "per_symbol_queries": len(query_list)},
        "rejected": rejected,
        "note": ("只收录标题或摘要中出现持仓/候选代码或名称、且时间可解析且不晚于抓取时刻的新闻；"
                 "未知时间/未来时间/弱相关个股新闻不计入 items。未做情绪打分，由你判断。"),
    }


def main() -> int:
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", default="", help="comma-separated symbols")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))
    from astock_public import PublicClient
    from source_http import SourceHttp

    symbols = [s.strip() for s in args.watch.split(",") if s.strip()]
    snapshot = {"quotes": []}
    if symbols:
        try:
            rows = PublicClient(http=SourceHttp()).call("tencent_quote", symbols) or {}
            snapshot = {"quotes": [
                {"symbol": code.upper() if "." in code else code,
                 "name": row.get("name")}
                for code, row in rows.items()]}
        except Exception:  # noqa: BLE001 - names are an enhancement
            snapshot = {"quotes": []}
    block = digest_news(PublicClient(http=SourceHttp()), snapshot, watch=symbols)
    print(json.dumps(block, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def fetch_hot_and_sectors(client: Any, *, limit: int = 30) -> dict[str, Any]:
    """Market-wide heat and concept tags from the source layer's own endpoints.

    The observer has prices but no notion of what the market is talking about:
    which names are attracting attention, and which theme a symbol belongs to.
    Both endpoints already ship with the upstream layer, so this only normalises
    them. A missing endpoint yields an empty block rather than a guess.
    """
    hot: list[dict[str, Any]] = []
    try:
        rows = client.call("ths_hot_list") or []
    except Exception:  # noqa: BLE001 - heat is additive
        rows = []
    for row in rows[:limit]:
        if not isinstance(row, Mapping):
            continue
        code = row.get("code")
        if not isinstance(code, str) or not code:
            continue
        try:
            heat = int(float(row.get("heat")))
        except (TypeError, ValueError):
            heat = None
        hot.append({
            "symbol": _full_symbol(code),
            "name": _clip(row.get("name"), 20),
            "heat": heat,
            "rank_change": row.get("rank_chg"),
            "concepts": [str(c) for c in (row.get("concepts") or [])][:4],
        })
    return {"items": hot, "note": "热度榜按人气排序；concepts 为该股所属概念标签，来自同花顺热榜。"}


def _full_symbol(code: str) -> str:
    """Attach the exchange suffix the kernel uses; unknown prefixes pass through."""
    text = str(code).strip().upper()
    if not text or "." in text:
        return text
    if text.startswith(("60", "68", "51", "58", "11")):
        return text + ".SH"
    if text.startswith(("00", "30", "15", "16", "12")):
        return text + ".SZ"
    if text.startswith(("8", "4", "92")):
        return text + ".BJ"
    return text


def attach_hot(digest: dict[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    if block and block.get("items"):
        digest["market_heat"] = block
    return digest


def fetch_northbound(client: Any) -> dict[str, Any] | None:
    """Intraday northbound flows, the one capital-flow fact the source really has.

    Order-flow by size bucket is not available (the project already refuses to
    substitute volume for it), but the沪股通/深股通 series is published minute by
    minute. It answers "is foreign money buying this morning" without inventing
    anything. Values are in 亿元 as published; the first and last sample are kept
    so the block shows a direction, not one bare number.

    The upstream field mapping (``astock_public._upstream/SKILL.md``, ``hsgt_realtime``)
    is ``hgt`` = 沪股通 (Shanghai) and ``sgt`` = 深股通 (Shenzhen). Getting that
    backwards silently swaps the two markets' flows, so this function maps by
    that contract, states the mapping it used, and reports per-leg ``as_of``
    because the two series stop printing at different times near the close.
    """
    try:
        frame = client.call("hsgt_realtime")
    except Exception:  # noqa: BLE001 - a missing flow series is not fatal
        return None
    try:
        rows = [{str(k): v for k, v in rec.items()} for rec in frame.to_dict("records")]
    except Exception:  # noqa: BLE001 - a non-frame return means parse failure
        return None
    if not rows:
        return None

    def num(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if out == out else None  # reject NaN

    # Upstream contract: hgt = 沪股通 (Shanghai), sgt = 深股通 (Shenzhen).
    # Each leg can go NaN independently near the close (observed: 深股通/sgt is
    # NaN from 14:58 while 沪股通/hgt keeps printing), so take the last *valid*
    # value per leg rather than assuming the final row is usable for both.
    def series(key: str) -> list[float]:
        return [v for v in (num(row.get(key)) for row in rows) if v is not None]

    def last_valid(key: str) -> float | None:
        values = series(key)
        return values[-1] if values else None

    def as_of(key: str) -> str | None:
        for row in reversed(rows):
            if num(row.get(key)) is not None:
                stamp = row.get("time")
                return None if stamp is None else str(stamp)
        return None

    sh_now, sz_now = last_valid(HGT_KEY), last_valid(SGT_KEY)
    if sh_now is None and sz_now is None:
        return None
    sh_series, sz_series = series(HGT_KEY), series(SGT_KEY)
    sh_open = sh_series[0] if sh_series else None
    sz_open = sz_series[0] if sz_series else None
    sh_as_of, sz_as_of = as_of(HGT_KEY), as_of(SGT_KEY)
    return {
        "as_of": sh_as_of or sz_as_of,
        # One as-of per leg: 沪股通 and 深股通 stop printing at different times,
        # and a single shared stamp would claim the wrong moment for one of them.
        "as_of_by_leg": {"shanghai": sh_as_of, "shenzhen": sz_as_of},
        "source_fields": {"shanghai": HGT_KEY, "shenzhen": SGT_KEY,
                          "contract": "astock_public.hsgt_realtime: hgt=沪股通, sgt=深股通"},
        "shanghai_net_yi": sh_now,
        "shenzhen_net_yi": sz_now,
        "shanghai_delta_yi": None if sh_now is None or sh_open is None else round(sh_now - sh_open, 2),
        "shenzhen_delta_yi": None if sz_now is None or sz_open is None else round(sz_now - sz_open, 2),
        "note": "hgt_yi=沪股通、sgt_yi=深股通当日累计净买入（亿元）；delta 为相对当日首个有效值的增量。",
    }


def attach_northbound(digest: dict[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    if block:
        digest["northbound"] = block
    return digest
