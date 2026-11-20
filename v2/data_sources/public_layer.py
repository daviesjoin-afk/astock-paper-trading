"""Compatibility bulk-ingest helpers for the common public data layer.

The complete upstream catalog and query API live behind
``public_data.PublicDataClient.datasets()`` / ``dataset(name, **params)``.
They include current snapshots, recent-window data, history and calculators.
This module retains existing bars/valuation/industry bulk CLI entry points.

Source dates describe when a fact applied; fetched_at describes when it was
retrieved. Neither an effective date nor an old financial reporting period
proves that a later downloaded revision was available to a past decision.

Adapted from simonlin1212/a-stock-data (Apache-2.0). See astock_public for
vendored source, provenance, complete capabilities and reproducible updates.

Usage:
    python v2/data_sources/public_layer.py valuation --symbols symbols.txt \\
        --start 2026-09-01 --end 2026-11-11 --out valuation.sqlite3
    python v2/data_sources/public_layer.py industry --out industry.sqlite3
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import io
import json
from pathlib import Path
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import urllib.request

SCHEMA = """
CREATE TABLE IF NOT EXISTS valuation_day(
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    close_cents INTEGER,
    pe_ttm_x10000 INTEGER,
    pb_mrq_x10000 INTEGER,
    ps_ttm_x10000 INTEGER,
    pcf_ncf_ttm_x10000 INTEGER,
    turnover_bps INTEGER,
    halted INTEGER NOT NULL DEFAULT 0,
    is_st INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    PRIMARY KEY(symbol, day)
);
CREATE INDEX IF NOT EXISTS valuation_day_idx ON valuation_day(day);
CREATE TABLE IF NOT EXISTS bar(
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    open_cents INTEGER NOT NULL,
    high_cents INTEGER NOT NULL,
    low_cents INTEGER NOT NULL,
    close_cents INTEGER NOT NULL,
    prev_close_cents INTEGER NOT NULL,
    volume_shares INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    adjust TEXT NOT NULL DEFAULT 'none',
    source TEXT NOT NULL,
    tradable INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(symbol, day)
);
CREATE INDEX IF NOT EXISTS bar_day ON bar(day);
CREATE TABLE IF NOT EXISTS industry_history(
    symbol TEXT NOT NULL,
    start_day TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    l1_code TEXT,
    l2_code TEXT,
    source TEXT NOT NULL,
    PRIMARY KEY(symbol, start_day, industry_code)
);
CREATE INDEX IF NOT EXISTS industry_history_idx ON industry_history(symbol, start_day);
"""


# ---------------------------------------------------------------- market bars

def tdx_daily(day: str, *, timeout: float = 90.0) -> list[dict]:
    """One trading day, whole market, from the Tongdaxin after-close package.

    This is the primary bar source and it is not close: one ~2.8 MB request
    yields every SH/SZ/BJ name's OHLCV plus turnover. Measured 2026-11-14:
    5,578 rows in 44-62 seconds.

    Delegates to `ingest_market`, which already owns the 150-byte code table and
    512-byte quote block layout, rather than re-parsing the binary here.
    """
    try:
        from . import ingest_market
        from .public_data import day_id
    except ImportError:
        import ingest_market
        from public_data import day_id

    day = day_id(day)
    rows = ingest_market.parse_package(
        ingest_market.fetch_package(day, timeout=timeout), day.replace("-", ""))
    return [{
        "symbol": row.symbol,
        "day": day,
        "name": getattr(row, "name", "") or "",
        "open_cents": row.open_cents,
        "high_cents": row.high_cents,
        "low_cents": row.low_cents,
        "close_cents": row.close_cents,
        "prev_close_cents": row.prev_close_cents,
        "volume_shares": row.volume_shares,
        "amount_cents": row.amount_cents,
        "adjust": "none",
        "tradable": row.tradable,
        "source": "tdx",
    } for row in rows]


def tencent_daily(symbol: str, days: int = 120, *, timeout: float = 10.0,
                  http=None) -> list[dict]:
    """Recent raw bars, using the existing parser and shared transport.

    Tencent daily volume is shares for STAR and lots for other boards. Missing
    previous close/turnover are unknown, not zero; these query rows alone are
    insufficient for the complete bar ingest contract.
    """
    import urllib.parse
    try:
        from .a_stock_data import normalize_symbol, parse_tencent_payload, price_to_cents
        from .source_http import SourceHttp, RequestIdentity
    except ImportError:
        from a_stock_data import normalize_symbol, parse_tencent_payload, price_to_cents
        from source_http import SourceHttp, RequestIdentity

    if type(days) is not int or not 1 <= days <= 640:
        raise ValueError("days must be an integer in 1..640")
    symbol, market, code = normalize_symbol(symbol)
    query = urllib.parse.urlencode({"param": f"{market}{code},day,,,{days},"})
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + query
    transport = http or SourceHttp(timeout=timeout)

    def validate(raw):
        payload = json.loads(raw.decode("utf-8"))
        items = ((payload.get("data") or {}).get(market + code) or {}).get("day")
        if not isinstance(items, list) or not items:
            raise ValueError("missing unadjusted Tencent day series")
        # Reuse the strict existing identity, OHLC, decimal and STAR-unit rules.
        dates = [str(row[0]) for row in items if isinstance(row, list) and row]
        if len(dates) != len(items):
            raise ValueError("malformed Tencent day series")
        return parse_tencent_payload(payload, symbol, min(dates), max(dates), url, "")

    snap = transport.request(
        RequestIdentity("tencent-daily-recent", url, symbol=symbol,
                        date=f"recent:{days}", adjust="none"),
        headers={"User-Agent": "Mozilla/5.0 (compatible; astock-v2)"}, validate=validate)
    batch = validate(snap.raw)
    return [{
        "symbol": symbol, "day": day, "name": "",
        "open_cents": price_to_cents(bar.open),
        "high_cents": price_to_cents(bar.high),
        "low_cents": price_to_cents(bar.low),
        "close_cents": price_to_cents(bar.close),
        "prev_close_cents": None, "amount_cents": None,
        "volume_shares": bar.volume_shares, "tradable": None,
        "adjust": "none", "source": "tencent",
        "fetched_at": snap.fetched_at, "sha256": snap.sha256,
        "cache_hit": snap.cache_hit,
    } for day, bar in sorted(batch.bars.items())]


def fetch_bars(day: str, *, timeout: float = 90.0,
               symbols: list[str] | None = None) -> dict:
    """Prefer the whole-market TDX package; fall back for explicit symbols.

    Query fallback rows preserve unknown turnover/previous close. They cannot
    silently become complete, executable ledger bars in ``store_bars``.
    """
    try:
        from .public_data import day_id, symbol_id
    except ImportError:
        from public_data import day_id, symbol_id
    day = day_id(day)
    selected = sorted(set(symbol_id(s) for s in symbols)) if symbols is not None else None
    if selected == []:
        raise ValueError("symbols must not be empty")
    try:
        rows = tdx_daily(day, timeout=timeout)
        if selected is not None:
            rows = [row for row in rows if row["symbol"] in selected]
        if not rows or (selected is not None and {row["symbol"] for row in rows} != set(selected)):
            raise ValueError("TDX requested rows missing")
        return {"day": day, "source": "tdx", "rows": rows, "degraded": False,
                "status": "ok"}
    except Exception as primary_error:
        error = f"{type(primary_error).__name__}: {primary_error}"[:200]
    rows, failures = [], []
    for symbol in selected or []:
        try:
            matched = [row for row in tencent_daily(symbol, timeout=min(timeout, 30.0))
                       if row["day"] == day]
            if not matched:
                raise ValueError("requested day not returned by recent-window source")
            rows.extend(matched)
        except Exception as exc:
            failures.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:200]})
    return {"day": day, "source": "tencent" if rows else None, "rows": rows,
            "status": ("ok" if rows and not failures else "partial" if rows else "error"),
            "degraded": True, "primary_error": error, "failures": failures,
            "note": "Fallback requires explicit symbols; absent amount/previous-close/"
                    "halt state remain unknown and cannot be written as complete bars."}


# ---------------------------------------------------------------- baostock

def baostock_symbol(symbol: str) -> str:
    """Canonical `600519.SH` -> `sh.600519`.

    BaoStock refuses Beijing codes outright (`10004011`), so that is caught here
    before a session is opened rather than surfacing as an empty frame.
    """
    try:
        from .public_data import symbol_id
    except ImportError:
        from public_data import symbol_id
    symbol = symbol_id(symbol)
    code, market = symbol.split(".")
    if market == "SH":
        return f"sh.{code}"
    if market == "SZ":
        return f"sz.{code}"
    raise ValueError(
        f"baostock 不支持 {symbol}：北交所(4/8/92)号段会被服务端拒绝。"
        "北交所估值请用当日快照源，不能进历史回测。"
    )


@contextmanager
def _bs_session():
    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {login.error_code} {login.error_msg}")
    try:
        yield bs
    finally:
        bs.logout()


def _rs_rows(result) -> list[list[str]]:
    """ResultData -> rows. An error code raises; an empty frame is never a guess."""
    if result.error_code != "0":
        raise RuntimeError(f"baostock 查询失败: {result.error_code} {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return rows


def _as_x10000(value: str) -> int | None:
    """Decimal text -> scaled integer, or None when the source says nothing.

    None and 0 are different claims: a zero PE is meaningless, an absent PE means
    the field was blank for that day (loss-making quarters do report blanks).
    """
    if value in ("", None, "-") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return None
        return int((number * 10_000).to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError, ValueError):
        return None


def fetch_valuation(symbol: str, start: str, end: str) -> list[dict]:
    """Daily valuation for one symbol. Rows are stamped with the session date."""
    bs_code = baostock_symbol(symbol)  # validate before opening a session
    fields = "date,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn,tradestatus,isST"
    with _bs_session() as bs:
        result = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start, end_date=end, frequency="d", adjustflag="3",
        )
        rows = _rs_rows(result)
    out = []
    for row in rows:
        if len(row) != 9:
            raise ValueError("baostock valuation row shape changed")
        day, close, pe, pb, ps, pcf, turn, status, is_st = row
        if not day or status not in ("0", "1") or is_st not in ("0", "1"):
            raise ValueError("baostock date/trading/ST state unknown; not assumed normal")
        out.append({
            "symbol": symbol,
            "day": day,
            "close_cents": int(round(float(close) * 100)) if close else None,
            "pe_ttm_x10000": _as_x10000(pe),
            "pb_mrq_x10000": _as_x10000(pb),
            "ps_ttm_x10000": _as_x10000(ps),
            "pcf_ncf_ttm_x10000": _as_x10000(pcf),
            "turnover_bps": int(round(float(turn) * 100)) if turn else None,
            "halted": 1 if status == "0" else 0,
            "is_st": 1 if is_st == "1" else 0,
            "source": "baostock",
        })
    return out


# ---------------------------------------------------------------- shenwan

SW_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
SW_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fetch_industry_history() -> list[dict]:
    """Shenwan classification history: one row per symbol per reclassification.

    `start_date` is the day the classification took effect, which is what makes
    an as-of lookup honest. Shenwan publishes codes only, no Chinese names -- the
    name table is a separate unreleased document, so no name is invented here.
    """
    import pandas as pd

    request = urllib.request.Request(SW_URL, headers={"User-Agent": SW_UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    frame = pd.read_excel(io.BytesIO(payload))
    frame = frame.rename(columns={"股票代码": "code", "计入日期": "start_date",
                                  "行业代码": "industry_code"})
    missing = {"code", "start_date", "industry_code"} - set(frame.columns)
    if missing:
        raise RuntimeError(f"申万表结构变了，缺列 {sorted(missing)}；实际列={list(frame.columns)}")
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["industry_code"] = frame["industry_code"].astype(str).str.zfill(6)
    frame["start_date"] = pd.to_datetime(frame["start_date"], errors="coerce")
    frame = frame.dropna(subset=["start_date"])
    out = []
    for _, row in frame.iterrows():
        code = row["code"]
        industry = row["industry_code"]
        # Shenwan prefixes a market digit on the six-digit code; the canonical
        # suffix comes from the board prefix, never from the industry code.
        if code[:3] in ("600", "601", "603", "605", "688", "689"):
            symbol = f"{code}.SH"
        elif code[:3] in ("000", "001", "002", "003", "300", "301"):
            symbol = f"{code}.SZ"
        elif code[:2] in ("43", "83", "87", "88", "92"):
            symbol = f"{code}.BJ"
        else:
            continue
        out.append({
            "symbol": symbol,
            "start_day": row["start_date"].strftime("%Y-%m-%d"),
            "industry_code": industry,
            "l1_code": industry[:2] + "0000",
            "l2_code": industry[:4] + "00",
            "source": "swsresearch",
        })
    return out


def industry_as_of(conn: sqlite3.Connection, symbol: str, day: str) -> dict | None:
    """The classification in force on `day`, i.e. the latest change at or before it."""
    row = conn.execute(
        "SELECT industry_code, l1_code, l2_code, start_day FROM industry_history "
        "WHERE symbol = ? AND start_day <= ? ORDER BY start_day DESC LIMIT 1",
        (symbol, day),
    ).fetchone()
    if not row:
        return None
    return {"symbol": symbol, "as_of": day, "industry_code": row[0],
            "l1_code": row[1], "l2_code": row[2], "since": row[3]}


# ---------------------------------------------------------------- storage

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(valuation_day)")}
    if "pcf_ncf_ttm_x10000" not in columns:
        conn.execute("ALTER TABLE valuation_day ADD COLUMN pcf_ncf_ttm_x10000 INTEGER")
    conn.commit()


def store_valuation(conn: sqlite3.Connection, rows: list[dict]) -> int:
    written = 0
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO valuation_day"
            "(symbol, day, close_cents, pe_ttm_x10000, pb_mrq_x10000, ps_ttm_x10000,"
            " pcf_ncf_ttm_x10000, turnover_bps, halted, is_st, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row["symbol"], row["day"], row["close_cents"], row["pe_ttm_x10000"],
             row["pb_mrq_x10000"], row["ps_ttm_x10000"], row.get("pcf_ncf_ttm_x10000"),
             row["turnover_bps"], row["halted"], row["is_st"], row["source"]),
        )
        written += 1
    return written


def store_bars(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Write complete raw bars, preserving halts; preflight before any writes."""
    fields = ("open_cents", "high_cents", "low_cents", "close_cents",
              "prev_close_cents", "volume_shares", "amount_cents")
    for row in rows:
        if row.get("adjust", "none") != "none":
            raise ValueError("adjusted prices cannot be stored as raw bars")
        for field in fields:
            value = row.get(field)
            if type(value) is not int or value < 0 or (field == "close_cents" and value == 0):
                raise ValueError(f"complete integer raw bar required: {field}")
        tradable = row.get("tradable", 1 if row.get("source") == "tdx" else None)
        if not isinstance(tradable, (bool, int)) or tradable not in (0, 1):
            raise ValueError("explicit known tradable state required")
    conn.executemany(
        "INSERT INTO bar"
        "(symbol, day, name, open_cents, high_cents, low_cents, close_cents,"
        " prev_close_cents, volume_shares, amount_cents, adjust, source, tradable)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,'none',?,?) "
        "ON CONFLICT(symbol,day) DO UPDATE SET "
        "name=excluded.name, open_cents=excluded.open_cents, high_cents=excluded.high_cents, "
        "low_cents=excluded.low_cents, close_cents=excluded.close_cents, "
        "prev_close_cents=excluded.prev_close_cents, volume_shares=excluded.volume_shares, "
        "amount_cents=excluded.amount_cents, adjust=excluded.adjust, "
        "source=excluded.source, tradable=excluded.tradable",
        [(row["symbol"], row["day"], row.get("name", ""), row["open_cents"],
          row["high_cents"], row["low_cents"], row["close_cents"],
          row["prev_close_cents"], row["volume_shares"], row["amount_cents"],
          row["source"], int(row.get("tradable", 1))) for row in rows],
    )
    return len(rows)


def store_industry(conn: sqlite3.Connection, rows: list[dict]) -> int:
    written = 0
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO industry_history"
            "(symbol, start_day, industry_code, l1_code, l2_code, source)"
            " VALUES(?,?,?,?,?,?)",
            (row["symbol"], row["start_day"], row["industry_code"],
             row["l1_code"], row["l2_code"], row["source"]),
        )
        written += 1
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    valuation = sub.add_parser("valuation", help="daily PE/PB/PS/turnover/ST/halt")
    valuation.add_argument("--symbols", required=True, type=Path,
                           help="one canonical symbol per line")
    valuation.add_argument("--start", required=True)
    valuation.add_argument("--end", required=True)
    valuation.add_argument("--out", required=True, type=Path)
    valuation.add_argument("--limit", type=int, default=0)

    bars = sub.add_parser("bars", help="whole-market daily bars from the tdx package")
    bars.add_argument("--days", required=True,
                      help="comma separated YYYY-MM-DD, or START:END")
    bars.add_argument("--out", required=True, type=Path)

    industry = sub.add_parser("industry", help="shenwan classification history")
    industry.add_argument("--out", required=True, type=Path)

    args = parser.parse_args(argv)

    if args.command == "bars":
        spec = args.days
        if ":" in spec:
            import datetime
            start, _, end = spec.partition(":")
            day = datetime.date.fromisoformat(start)
            stop = datetime.date.fromisoformat(end)
            wanted = []
            while day <= stop:
                if day.weekday() < 5:      # weekends are never trading days
                    wanted.append(day.isoformat())
                day += datetime.timedelta(days=1)
        else:
            wanted = [part.strip() for part in spec.split(",") if part.strip()]

        args.out.parent.mkdir(parents=True, exist_ok=True)
        failures = []
        with closing(sqlite3.connect(args.out)) as conn:
            ensure_schema(conn)
            for index, day in enumerate(wanted, 1):
                try:
                    result = fetch_bars(day)
                except Exception as error:  # noqa: BLE001
                    failures.append({"day": day, "error": str(error)[:160]})
                    continue
                if result["degraded"]:
                    failures.append({"day": day, "error": result.get("primary_error", "")})
                    continue
                store_bars(conn, result["rows"])
                conn.commit()
                print(f"  [{index}/{len(wanted)}] {day} {result['source']} "
                      f"{len(result['rows'])} rows", flush=True)
            total = conn.execute("SELECT COUNT(*) FROM bar").fetchone()[0]
            days = conn.execute("SELECT COUNT(DISTINCT day) FROM bar").fetchone()[0]
        print(json.dumps({"status": "partial" if failures else "ok",
                          "rows": total, "days": days, "failed": len(failures),
                          "failures": failures}, ensure_ascii=False))
        return 3 if failures else 0

    if args.command == "industry":
        rows = fetch_industry_history()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(args.out)) as conn:
            ensure_schema(conn)
            written = store_industry(conn, rows)
            conn.commit()
            symbols = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM industry_history").fetchone()[0]
        print(json.dumps({"rows": written, "symbols": symbols}, ensure_ascii=False))
        return 0

    symbols = [line.strip() for line in args.symbols.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")]
    if args.limit:
        symbols = symbols[:args.limit]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    with closing(sqlite3.connect(args.out)) as conn:
        ensure_schema(conn)
        for index, symbol in enumerate(symbols, 1):
            try:
                rows = fetch_valuation(symbol, args.start, args.end)
            except Exception as error:
                # Continue other symbols; the final result and exit code remain partial.
                failures.append({"symbol": symbol, "error": str(error)[:160]})
                continue
            if not rows:
                failures.append({"symbol": symbol, "error": "valuation_rows_empty"})
                continue
            store_valuation(conn, rows)
            conn.commit()
            if index % 25 == 0 or index == len(symbols):
                total = conn.execute("SELECT COUNT(*) FROM valuation_day").fetchone()[0]
                print(f"  [{index}/{len(symbols)}] {symbol} rows={len(rows)} total={total}",
                      flush=True)
        total = conn.execute("SELECT COUNT(*) FROM valuation_day").fetchone()[0]
        symbols_ok = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM valuation_day").fetchone()[0]
    print(json.dumps({"status": "partial" if failures else "ok",
                      "rows": total, "symbols": symbols_ok,
                      "failed": len(failures), "failures": failures}, ensure_ascii=False))
    return 3 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
