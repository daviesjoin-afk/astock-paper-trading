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
# How many ranked candidates the AI observer gets to see. Enough for it to
# reason about specific names, small enough to stay a summary, not a payload.
DIGEST_CANDIDATES = 15
# How many held positions to describe by symbol (with live P&L).
DIGEST_POSITIONS = 12

LIVE_SYSTEM_PROMPT = """你审查 A 股模拟盘本轮风险，输出建议，由 Rust 决定能否成交。只使用输入中可核对的数据。缺失字段表示未知。quote_time_estimated=true 表示行情时间为估计。禁止编造新闻、资金流、成交量或涨跌停状态。

先判断市场广度和振幅，再看槽位、持仓与候选。change_distribution 的涨跌平家数只覆盖能比较现价与昨收的标的，sample_count 是收到的报价总数，*_bps 的 100 等于 1%。稳健看趋势持续性和回撤风险，激进看动量与趋势是否一致。输出建议作用于股票代码，不能限定到某个槽位。

- 排名只是相对强弱。弱市允许整轮不点名买入。有现金、排名靠前或贴近涨停都不能单独成为买入依据。buy 会请求内核对未持有的槽位发出明确买入信号。点名前检查趋势、日内位置、振幅、已持仓槽位和可用预算；distance_to_limit_up_bps 按昨收计算。若接近或到达涨停、关键证据缺失，或只剩涨幅可支持，放弃点名。已有持仓的代码也可能在另一槽触发开仓。
- 检查持仓时看本轮是否有有效价格、风险是否恶化。浮亏本身不构成卖出证据。同日账本显示可卖量为 0 时，不重复给卖出建议。用 account_as_of_round_id 的日期识别账本交易日。若账本属于上一交易日，开盘 T+1 由内核滚动，可卖量以本轮内核为准。sell 只对已持仓代码提出，并陈述依据。
- blocked_symbols 限制开仓和加仓。只填有明确禁买依据的代码。同一代码只出现一次，不同时列入禁买和买入建议。证据冲突或不足时，调整值填 0，可使用空列表。风险方向明确时优先使用一个全局调整，避免重复施压。所有数字发出前检查范围。

仅输出 JSON 对象，不能有 Markdown。顶层字段固定，不要输出 slot_recommendations、suggested_orders 或 risk_flags。
- "confidence" 为 0 至 100 的整数。"bias" 为 -20 至 20 的整数，负值更谨慎。"entry_delta" 为 -15 至 15 的整数，正值提高开仓门槛。"weight_tilt_bps" 为 -1000 至 1000 的整数，表示动量相对形态的偏移。
- "blocked_symbols" 最多 5 个代码。"suggestions" 最多 8 条，每条用 {"symbol":"输入中的完整代码","side":"buy|sell|hold","score_delta":0,"weight_bps":0} 的结构。score_delta 限 ±20，weight_bps 限 ±2000。
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
        if isinstance(closes, (list, tuple)) and len(closes) >= 5:
            first, last = closes[-5], closes[-1]
            if isinstance(first, (int, float)) and isinstance(last, (int, float)):
                if first > 0 and last > 0:
                    trend = _pct_bps(float(last) - float(first), float(first))

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
    return {
        "quote_time": snapshot.get("quote_time"),
        "quote_time_estimated": bool(snapshot.get("quote_time_estimated")),
        "sample_count": len(quotes),
        "change_distribution": {"up": up, "down": down, "flat": flat},
        "average_amplitude_bps": (round(sum(amplitudes) * 10_000 / len(amplitudes))
                                  if amplitudes else None),
        "account_as_of_round_id": account.get("as_of_round_id") if isinstance(account, Mapping) else None,
        "slots": slots,
        "held_positions": held_positions(account, quotes),
        "ranked_candidates": ranked_candidates(quotes),
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
        "max_tokens": 2_000,
        "response_format": {"type": "json_object"},
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
            body["thinking"] = {"type": "disabled"}
            if log:
                log.emit("ai_retry", round_id=round_id, reason=reason,
                         elapsed_ms=round(elapsed * 1_000))
        attempts = attempt
        request_at = monotonic()
        usage = _provider_tokens(None)
        status = "request_error"
        try:
            if attempt == 1:
                body["messages"] = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(
                        digest, ensure_ascii=False, allow_nan=False)},
                ]
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
