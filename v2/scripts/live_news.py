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
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Iterable, Mapping, Sequence

# A wire item older than this no longer describes the round being decided.
FRESH_MINUTES = 90
# Bounds the prompt: the observer reasons over a digest, not a news feed.
MAX_ITEMS = 24
MAX_TITLE_CHARS = 120
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


def link_symbols(items: Sequence[Mapping[str, Any]], names: Mapping[str, str],
                 *, watch: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Attach each headline to the symbols it names, and drop the rest.

    A hit needs the full symbol or a name of at least two characters. Substring
    matching on a one-character name would tag half the market.
    """
    wanted = set(watch)
    # A watched symbol only links by its full code. Accepting a bare name match
    # for a watched symbol would re-open the one-character hole this filter
    # exists to close: "涨" appears in half of the market's headlines.
    by_code = {symbol: symbol for symbol in wanted}
    linked: list[dict[str, Any]] = []
    for item in items:
        text = f"{item.get('title') or ''} {item.get('summary') or ''}"
        hits = [symbol for symbol, name in names.items()
                if len(name) >= 2 and name in text]
        hits += [symbol for symbol in by_code if symbol in text]
        if wanted:
            hits = [symbol for symbol in hits if symbol in wanted]
        if not hits:
            continue
        linked.append({**item, "symbols": sorted(set(hits))[:6]})
    return linked


def fetch_symbol_news(client: Any, symbol: str, *, page_size: int = 10) -> list[dict[str, Any]]:
    """News published about one symbol, straight from the per-stock endpoint.

    Market wires carry mostly macro chatter, so a held name can go days without
    appearing in them. Asking the per-symbol endpoint is the source's own way to
    answer "what happened to this stock", and it costs one request.
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
        out.append({"title": title, "summary": _clip(row.get("content")),
                    "time": row.get("time"), "wire": "eastmoney_stock_news",
                    "symbols": [symbol]})
    return out


def digest_news(client: Any, snapshot: Mapping[str, Any], *,
                watch: Iterable[str] = (), now: datetime | None = None,
                page_size: int = 50, per_symbol: int = 8,
                symbol_limit: int = 6) -> dict[str, Any]:
    """A bounded, fresh, symbol-linked news block for one round."""
    current = now or _now()
    names = _symbol_names(snapshot)
    raw = fetch_headlines(client, page_size=page_size)
    fresh: list[dict[str, Any]] = []
    stale = 0
    for item in raw:
        stamp = _parse_time(item.get("time"))
        if stamp is not None and (current - stamp) > timedelta(minutes=FRESH_MINUTES):
            stale += 1
            continue
        fresh.append(item)
    linked = link_symbols(fresh, names, watch=watch)
    # Per-symbol lookup, newest first, for the names we actually hold or ranked.
    seen_titles = {item["title"] for item in linked}
    extra: list[dict[str, Any]] = []
    targets = list(dict.fromkeys(watch))[:symbol_limit]
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(targets)))) as pool:
        per_symbol_rows = list(pool.map(
            lambda symbol: fetch_symbol_news(client, symbol, page_size=per_symbol), targets))
    for symbol, rows in zip(targets, per_symbol_rows):
        for item in rows:
            stamp = _parse_time(item.get("time"))
            if stamp is not None and (current - stamp) > timedelta(minutes=FRESH_MINUTES * 8):
                continue  # allow a wider window: a stock's own news is rarer
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
        "items": linked[:MAX_ITEMS],
        "counts": {"fetched": len(raw), "fresh": len(fresh),
                   "linked": len(linked), "dropped_stale": stale,
                   "per_symbol": len(extra)},
        "note": "只收录标题或摘要中出现持仓/候选代码或名称的新闻；未做情绪打分，由你判断。",
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

    # Each leg can go NaN independently near the close (observed: sgt is NaN
    # from 14:58 while hgt keeps printing), so take the last *valid* value per
    # leg rather than assuming the final row is usable for both.
    def series(key: str) -> list[float]:
        return [v for v in (num(row.get(key)) for row in rows) if v is not None]

    def last_valid(key: str) -> float | None:
        values = series(key)
        return values[-1] if values else None

    def as_of(key: str) -> str | None:
        for row in reversed(rows):
            if num(row.get(key)) is not None:
                return str(row.get("time"))
        return None

    sh_now, sz_now = last_valid("sgt_yi"), last_valid("hgt_yi")
    if sh_now is None and sz_now is None:
        return None
    sh_open = series("sgt_yi")[0] if series("sgt_yi") else None
    sz_open = series("hgt_yi")[0] if series("hgt_yi") else None
    return {
        "as_of": as_of("hgt_yi") or as_of("sgt_yi"),
        # sgt_yi carries 沪股通's cumulative net buy; hgt_yi carries 深股通's.
        "shanghai_net_yi": sh_now,
        "shenzhen_net_yi": sz_now,
        "shanghai_delta_yi": None if sh_now is None or sh_open is None else round(sh_now - sh_open, 2),
        "shenzhen_delta_yi": None if sz_now is None or sz_open is None else round(sz_now - sz_open, 2),
        "note": "sgt_yi=沪股通、hgt_yi=深股通当日累计净买入（亿元）；delta 为相对当日首个有效值的增量。",
    }


def attach_northbound(digest: dict[str, Any], block: Mapping[str, Any] | None) -> dict[str, Any]:
    if block:
        digest["northbound"] = block
    return digest
