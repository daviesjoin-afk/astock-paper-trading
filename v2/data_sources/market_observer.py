"""Ask a model to look at the market, and hand its answer to the kernel.

Division of labour
------------------
This module does four things and nothing else:

1. Assemble a compact market state digest.
2. Call the model.
3. Parse the answer into fields, without judging whether they are acceptable.
4. POST it to the kernel, which decides what to do with it.

**It deliberately contains no bounds, no confidence floor, no symbol whitelist
and no bias check.** Those rules live in exactly one place: the Rust
`domain::ai_policy` module, which re-clamps everything it receives and writes
down what it reduced. Duplicating them here would create a second source of
truth for the safety rules, and two copies of a limit drift apart silently --
the copy that is not enforced becomes a false assurance that the input was
already checked.

The practical consequence: if this file is wrong, or a hostile answer reaches
it, the worst outcome is a rejected or reduced advice record in the kernel.
It cannot widen a limit, because it does not know what the limits are.

What is still validated here are *shape* problems, not *policy* problems:
whether the answer is JSON, whether fields are the right type, whether the
response envelope exists. A malformed answer produces no submission at all,
which is the same outcome as the kernel ignoring it, but it saves the round
trip and records a clearer reason.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

# The kernel's endpoint for advice. Set by the caller; there is no default so a
# misconfigured run cannot post advice to an unintended host.
ADVICE_PATH = "/v2/ai/advice"


@dataclass
class MarketObservation:
    """The state digest handed to the model.

    Explicit fields rather than a free-form dict so the prompt and the parser
    cannot drift apart: adding a field here makes it appear in the payload, and
    the prompt is updated in the same edit.
    """

    day: str
    regime: str
    regime_separation_bps: int | None
    regime_breadth_bps: int | None
    slot: str
    slot_return_bps: int | None
    slot_drawdown_bps: int | None
    slot_positions: int
    candidates: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    recent_trades: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


SYSTEM_PROMPT = """你是 A 股模拟盘的盘前观察员。你的职责是读市场，不是下指令。

你会收到当日状态摘要：市场状态、某个仓位近况、候选股票（含评分与日内形态）、以及可能有的新闻。

要回答的问题是：**今天这个仓位的策略假设还成立吗？**

- 趋势仓（steady）：当前趋势更像业绩/资金驱动，还是情绪拉升？
- 突破仓（aggressive）：候选里的突破，是真突破还是无量假突破？

要求：
1. 只依据给到的数据。数据里没有的（成交量、龙虎榜、财报）不要假设。
2. 不确定就说不确定，confidence 给低分。
3. 你的建议会被内核按固定上限裁剪，并在报告里记录裁剪过程。不需要你猜上限是多少。
4. reason 写清楚依据了哪几个具体数据点，不要写空话。

只输出 JSON：
{
  "bias": "bullish|neutral|bearish|unclear",
  "entry_delta": 整数,
  "weight_delta_bps": 整数,
  "position_delta": 整数,
  "blocked_symbols": ["代码", ...],
  "confidence": 0-100 整数,
  "reason": "依据具体数据点的说明"
}"""


def build_user_prompt(observation: MarketObservation) -> str:
    """Render the digest as compact text.

    A fixed-width candidate table rather than nested JSON: the model reads the
    list more reliably, and the list is the part that matters most.
    """
    lines = [
        f"日期：{observation.day}",
        f"市场状态：{observation.regime}"
        + (f"（分离度 {observation.regime_separation_bps} bps）"
           if observation.regime_separation_bps is not None else "")
        + (f"（上涨占比 {observation.regime_breadth_bps} bps）"
           if observation.regime_breadth_bps is not None else ""),
        "",
        f"仓位：{observation.slot}",
        (f"  累计收益：{observation.slot_return_bps} bps"
         if observation.slot_return_bps is not None else "  累计收益：无数据"),
        (f"  最大回撤：{observation.slot_drawdown_bps} bps"
         if observation.slot_drawdown_bps is not None else "  最大回撤：无数据"),
        f"  当前持仓数：{observation.slot_positions}",
        "",
    ]
    if observation.candidates:
        lines.append("候选（按评分降序，最多 12 个）：")
        lines.append("代码 | 评分 | 理由 | klow(下影线bps) | klen(振幅bps) | kmid(实体bps)")
        for item in observation.candidates[:12]:
            lines.append(
                "{symbol} | {score} | {reason} | {klow} | {klen} | {kmid}".format(
                    symbol=item.get("symbol", "?"),
                    score=item.get("score", "?"),
                    reason=item.get("reason", "?"),
                    klow=item.get("klow_bps", "-"),
                    klen=item.get("klen_bps", "-"),
                    kmid=item.get("kmid_bps", "-"),
                )
            )
        lines.append("")
    else:
        lines.extend(["候选：今天没有达到门槛的标的", ""])

    if observation.news:
        lines.append("相关新闻（最多 5 条）：")
        for item in observation.news[:5]:
            lines.append(f"- [{item.get('symbol', '?')}] {item.get('title', '')}"
                         f"（{item.get('published_at', '时间未知')}）")
        lines.append("")
    else:
        lines.extend(["相关新闻：本次没有取到", ""])

    if observation.recent_trades:
        lines.append("最近成交（最多 5 笔）：")
        for item in observation.recent_trades[:5]:
            lines.append(
                f"- {item.get('trade_day', '?')} {item.get('side', '?')} "
                f"{item.get('symbol', '?')} x{item.get('quantity', '?')} "
                f"@{item.get('price_cents', '?')} 分（{item.get('reason', '')}）"
            )
        lines.append("")

    lines.append("请按系统提示输出 JSON。")
    return "\n".join(lines)


def parse_fields(raw: str) -> dict[str, Any] | None:
    """Extract the answer's fields, with no policy judgement.

    Returns `None` when the text is not a JSON object. Types are coerced where
    that is unambiguous and left alone where it is not: a non-numeric
    `entry_delta` becomes absent rather than zero, because "the model did not
    say" and "the model said no change" are different claims that the kernel
    records differently.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    def as_int(key: str) -> int | None:
        value = payload.get(key)
        if isinstance(value, bool):
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    blocked = payload.get("blocked_symbols")
    if not isinstance(blocked, list):
        blocked = []
    return {
        "bias": str(payload.get("bias", "unclear")),
        "entry_delta": as_int("entry_delta"),
        "weight_delta_bps": as_int("weight_delta_bps"),
        "position_delta": as_int("position_delta"),
        "blocked_symbols": [str(item) for item in blocked],
        "confidence": as_int("confidence"),
        "reason": str(payload.get("reason", ""))[:800],
    }


def ask_model(observation: MarketObservation, *, base_url: str, api_key: str,
              model: str, timeout: float = 120.0) -> dict[str, Any] | None:
    """Call the model and return parsed fields, or None on any failure.

    Failures are returned, not raised: an unreachable model must leave the
    trading day untouched, so the caller needs a value it can handle rather
    than an exception it might not expect.
    """
    if not base_url or not api_key:
        return None
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(observation)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, IndexError, TypeError):
        return None
    return parse_fields(content)


def submit(advice: dict[str, Any], *, api: str, timeout: float = 30.0) -> bool:
    """POST the advice to the kernel, which clamps and records it.

    Returns whether the kernel accepted it. A rejection is not an error here:
    the kernel is the authority on what is acceptable, and the caller should
    carry on with the deterministic policy either way.
    """
    body = json.dumps(advice, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api.rstrip("/") + ADVICE_PATH, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def config_from_env() -> tuple[str, str, str]:
    return (
        os.environ.get("ASTOCK_LLM_BASE", ""),
        os.environ.get("ASTOCK_LLM_KEY", ""),
        os.environ.get("ASTOCK_LLM_MODEL", "deepseek-v4.1-flash"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", required=True,
                        help="path to the state digest JSON (from the kernel)")
    parser.add_argument("--api", default=os.environ.get("ASTOCK_API", ""),
                        help="kernel base URL, advice is POSTed here")
    parser.add_argument("--base-url", default=os.environ.get("ASTOCK_LLM_BASE", ""))
    parser.add_argument("--api-key", default=os.environ.get("ASTOCK_LLM_KEY", ""))
    parser.add_argument("--model", default=os.environ.get("ASTOCK_LLM_MODEL",
                                                        "deepseek-v4.1-flash"))
    args = parser.parse_args(argv)

    payload = json.loads(open(args.observation, encoding="utf-8").read())
    observation = MarketObservation(**payload)
    fields = ask_model(observation, base_url=args.base_url,
                       api_key=args.api_key, model=args.model)
    if fields is None:
        print(json.dumps({"status": "no_advice", "reason": "model_unavailable"},
                         ensure_ascii=False))
        return 0
    if args.api:
        accepted = submit(fields, api=args.api)
        print(json.dumps({"status": "submitted" if accepted else "rejected",
                          "advice": fields}, ensure_ascii=False))
    else:
        print(json.dumps({"status": "not_submitted", "reason": "no_api_url",
                          "advice": fields}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
