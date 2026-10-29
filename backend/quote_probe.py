# -*- coding: utf-8 -*-
"""只读批量行情查询与耗时对比，不写交易记录或改变成交门禁。

python backend/quote_probe.py --codes 600887 002980 --quotes
python backend/quote_probe.py --codes 600887 --sources tencent sina eastmoney --repeat 3
"""
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import replace

import data_fetcher as dfc
import marketdata_feeds as feeds


def parse_codes(values):
    codes = list(dict.fromkeys(values))
    if not codes or len(codes) > 200 or any(not re.fullmatch(r"[0-9]{6}", c) for c in codes):
        raise ValueError("请提供1至200个六位股票代码")
    return codes


def make_feed(source, timeout):
    factories = {
        "tencent": dfc.tencent_realtime_feed,
        "sina": dfc.sina_realtime_feed,
        "eastmoney": dfc.eastmoney_realtime_feed,
    }
    options = dict(
        attempts=1, health=feeds.FeedHealthRegistry(),
        reliability=feeds.FeedReliabilityPolicy(timeout_seconds=timeout),
        reset_data_source=lambda *_args, **_kwargs: None,
    )
    if source == "eastmoney":
        options["hosts"] = ("push2.eastmoney.com",)
    return replace(factories[source](), **options)


def probe(codes, source, timeout=4.0, include_quotes=False):
    codes = parse_codes(codes)
    started = time.perf_counter()
    error = None
    try:
        rows = make_feed(source, timeout).fetch_realtime(codes)
    except Exception as exc:
        rows = []
        error = type(exc).__name__
    by_code = {str(row.get("code")): row for row in rows if str(row.get("code")) in codes}
    result = {
        "source": source, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "requested": len(codes), "returned": len(by_code),
        "missing_codes": [code for code in codes if code not in by_code],
        "quote_times": sorted({str(row.get("quote_at")) for row in by_code.values() if row.get("quote_at")}),
        "status": "complete" if len(by_code) == len(codes) else "partial" if by_code else "unavailable",
    }
    if error:
        result["error"] = error
    if include_quotes:
        result["quotes"] = [by_code[code] for code in codes if code in by_code]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--sources", nargs="+", choices=("tencent", "sina", "eastmoney"), default=["tencent", "sina"])
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--timeout", type=float, default=4.0, help="单次网络请求超时秒数，范围0.5至10")
    parser.add_argument("--quotes", action="store_true")
    args = parser.parse_args(argv)
    try:
        codes = parse_codes(args.codes)
        if not 0.5 <= args.timeout <= 10:
            raise ValueError("单次请求超时必须在0.5至10秒之间")
    except ValueError as exc:
        parser.error(str(exc))
    results = [dict(round=iteration + 1, **probe(codes, source, args.timeout, args.quotes))
               for iteration in range(args.repeat) for source in dict.fromkeys(args.sources)]
    print(json.dumps({"purpose": "read_only_quote_query", "execution_verified": False,
                      "note": "返回源时间和缺失代码；报价完整不代表新鲜或允许成交。", "results": results}, ensure_ascii=False))
    return 0 if any(row["returned"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
