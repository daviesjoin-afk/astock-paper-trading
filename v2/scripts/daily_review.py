#!/usr/bin/env python3
"""Attach an LLM review to an already-produced daily report.

The deterministic pipeline settles the day and writes the numbers. This script
runs *after* that and adds a second opinion — it never produces a number of its
own and never changes the ledger.

Two properties matter more than the review's content:

* **Optional by construction.** No API key means no review; the daily report
  stands on its own and the script says `skipped`. A trading day must never
  depend on a third-party service being reachable.
* **Failure is reported, not hidden.** A refused or malformed review is
  appended as a failure note, so a reader can tell "the model had nothing to
  say" apart from "the model said nothing was wrong".

Usage:
    python scripts/daily_review.py --api http://127.0.0.1:18765 \
        --day 2026-11-10 --report-dir ../work/reports \
        --base-url https://example.com/v1 --model deepseek-v4.1-flash
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))
import llm_runner  # noqa: E402


def fetch(base: str, path: str, timeout: float = 30.0):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as response:
        return json.load(response)


def build_signals_payload(report: dict) -> list[dict]:
    """Turn a day's fills into per-symbol review payloads.

    Only settled fills are reviewed. There is no "what should we buy" task
    here: the rules already decided, and a second opinion on a position that
    does not exist would be a suggestion, not a review.
    """
    payloads = []
    trades = report.get("trades") or []
    for trade in trades:
        symbol = trade.get("symbol")
        if not symbol:
            continue
        payloads.append(
            {
                "symbol": symbol,
                "signal": {
                    "side": trade.get("side"),
                    "quantity": trade.get("quantity"),
                    "price_cents": trade.get("price_cents"),
                    "reason": trade.get("reason"),
                },
                # The evidence the rule recorded at decision time. When it is
                # absent (older rows), say so instead of substituting a
                # reconstruction that would describe a different decision.
                "basis": trade.get("basis")
                or {"note": "该笔成交未记录决策依据（旧账本行）"} ,
                "context": {
                    "day": report.get("day"),
                    "equity_cents": (report.get("account") or {}).get("equity_cents"),
                    "blocked_today": report.get("blocked"),
                    "slot_returns": report.get("slots"),
                },
            }
        )
    return payloads


def build_pnl_payload(report: dict) -> dict | None:
    """One attribution payload for the day, or None when there is nothing to attribute."""
    account = report.get("account") or {}
    total = account.get("day_return_bps")
    if total is None:
        return None
    fills = report.get("fills") or {}
    slots = report.get("slots") or []
    return {
        "period": report.get("day"),
        "total_bps": total,
        "activity": {
            "filled_orders": fills.get("count", 0),
            "buys": fills.get("buys", 0),
            "sells": fills.get("sells", 0),
            "fee_cents": fills.get("fees_cents", 0),
            "blocked": report.get("blocked", 0),
        },
        "breakdown": {
            "steady_return_bps": slots[0].get("return_bps") if slots else None,
            "aggressive_return_bps": slots[1].get("return_bps") if len(slots) > 1 else None,
            "max_drawdown_bps": account.get("max_drawdown_bps"),
        },
    }


def append_review(report_path: Path, block: str) -> None:
    """Append to the day's text report.

    Appended, never rewritten: the deterministic part of the report must stay
    exactly as the kernel produced it, so the two voices remain separable.
    """
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    separator = "\n\n" if existing and not existing.endswith("\n") else "\n"
    report_path.write_text(existing + separator + block + "\n", encoding="utf-8")


def render_block(records: list[dict]) -> str:
    lines = ["── AI 复核（仅供参考，不改变账本）──"]
    if not records:
        lines.append("本次没有需要复核的成交。")
        return "\n".join(lines)
    for record in records:
        label = record.get("label", record.get("task"))
        if record.get("status") != "ok":
            lines.append(f"[{label}] 复核失败：{record.get('code')} {record.get('detail','')}".rstrip())
            continue
        result = record.get("result") or {}
        if "verdict" in result:
            verdict = {"agree": "同意", "object": "有异议", "insufficient_evidence": "证据不足"}.get(
                result["verdict"], result["verdict"]
            )
            lines.append(f"[{label}] {verdict}")
            for reason in result.get("reasons", [])[:5]:
                lines.append(f"  · {reason}")
            for risk in result.get("risks", [])[:3]:
                lines.append(f"  ⚠ {risk}")
        elif "attribution" in result:
            lines.append(f"[{label}] 归因（合计 {result.get('total_bps')} bps）")
            for item in result.get("attribution", []):
                lines.append(f"  · {item.get('source')} {item.get('bps')} bps：{item.get('evidence','')}")
            if result.get("notes"):
                lines.append(f"  总结：{result['notes']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:18765")
    parser.add_argument("--day", required=True)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--base-url", default=os.environ.get("ASTOCK_LLM_BASE", ""))
    parser.add_argument("--model", default=os.environ.get("ASTOCK_LLM_MODEL", "deepseek-v4.1-flash"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-fills", type=int, default=5,
                        help="review at most this many fills to bound cost")
    args = parser.parse_args(argv)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"{args.day}.txt"

    api_key = os.environ.get("ASTOCK_LLM_KEY")
    if not args.base_url or not api_key:
        # Not an error: the deterministic report is the product, this is an extra.
        append_review(report_path, "── AI 复核：未配置，已跳过 ──")
        print(json.dumps({"status": "skipped", "reason": "no_llm_configuration"}))
        return 0

    try:
        report = fetch(args.api, f"/v2/account/daily-report?day={args.day}")
    except (urllib.error.URLError, OSError) as error:
        append_review(report_path, f"── AI 复核：无法读取日报（{error}）──")
        print(json.dumps({"status": "failed", "code": "report_unavailable"}))
        return 0

    records: list[dict] = []
    for payload in build_signals_payload(report)[: args.max_fills]:
        record = llm_runner.run(
            "signal_review", payload,
            base_url=args.base_url, api_key=api_key, model=args.model, timeout=args.timeout,
        )
        record["label"] = f"{payload['symbol']} {payload['signal']['side']}"
        records.append(record)

    pnl_payload = build_pnl_payload(report)
    if pnl_payload is not None:
        record = llm_runner.run(
            "pnl_review", pnl_payload,
            base_url=args.base_url, api_key=api_key, model=args.model, timeout=args.timeout,
        )
        record["label"] = "当日归因"
        records.append(record)

    append_review(report_path, render_block(records))

    # The raw records go next to the report so a rejected review can be
    # inspected later instead of vanishing into a one-line summary.
    raw_path = args.report_dir / f"{args.day}.review.json"
    raw_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for item in records if item.get("status") == "ok")
    print(json.dumps({"status": "ok", "reviewed": len(records), "succeeded": ok,
                      "report": str(report_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
