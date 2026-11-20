#!/usr/bin/env python3
"""Ask an LLM for factor hypotheses and keep only the ones that survive parsing.

This is the boundary where a language model is allowed to influence research,
and the boundary is deliberately narrow:

* the model's output is **data** (a factor expression), never an instruction;
* every candidate is re-parsed into the Rust `Factor` grammar and rejected if
  it does not fit — no "close enough" repairs, no silent defaults;
* the model is recorded alongside each hypothesis, so a factor that came from
  a hallucination can be traced back to it.

Nothing here touches a ledger. The output is a JSON file of *proposals* that a
human (or the P4 governance pipeline) may choose to evaluate.

Usage:
    ASTOCK_LLM_KEY=... python llm_hypothesis.py \
        --base-url https://example.com/v1 --model deepseek-v4.1-flash \
        --theme "低波动可能比动量更可靠" --count 3 --out ../work/hypotheses.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# The exact grammar. Kept in one place so the prompt and the validator cannot
# drift apart: a model told about an operator that the parser rejects produces
# a proposal that is thrown away for no reason.
GRAMMAR = """因子表达式只允许以下形式之一，严格 JSON，不得发明算子：
{"close":{"offset":N}}          N 0..249，N 日前收盘
{"mean":{"window":N}}           收盘价均值，N 1..250
{"std_dev":{"window":N}}        收盘价标准差，N 1..250
{"max":{"window":N}}            区间最高收盘
{"min":{"window":N}}            区间最低收盘
{"momentum":{"window":N}}       今收相对 N 日前收盘的涨跌幅（基点），N 1..249
{"add":[A,B]} / {"sub":[A,B]} / {"div":[A,B]}   二元运算，div 返回基点
{"const":{"value":N}}
约束：std_dev/mean/max/min 只作用于收盘价序列本身，不能套在其它表达式上。
"""

ALLOWED_KEYS = {
    "close": {"offset"},
    "mean": {"window"},
    "std_dev": {"window"},
    "max": {"window"},
    "min": {"window"},
    "momentum": {"window"},
    "const": {"value"},
    "add": None,
    "sub": None,
    "div": None,
}

MAX_WINDOW = 250
MAX_DEPTH = 12


class InvalidFactor(Exception):
    """The model's expression does not fit the grammar. Never repaired."""


def validate(node, depth=0):
    """Mirror of the Rust `Factor::validate` rules.

    Duplicated on purpose: a proposal is rejected here, before a Rust call, so
    an unusable hypothesis never reaches the evaluator at all.
    """
    if depth > MAX_DEPTH:
        raise InvalidFactor(f"depth {depth} exceeds {MAX_DEPTH}")
    if not isinstance(node, dict) or len(node) != 1:
        raise InvalidFactor(f"expected a single-key object, got {node!r}")
    (operator, argument), = node.items()
    if operator not in ALLOWED_KEYS:
        raise InvalidFactor(f"unknown operator {operator!r}")
    fields = ALLOWED_KEYS[operator]
    if fields is None:
        if not isinstance(argument, list) or len(argument) != 2:
            raise InvalidFactor(f"{operator} takes a two-element list")
        for child in argument:
            validate(child, depth + 1)
        return
    if not isinstance(argument, dict):
        raise InvalidFactor(f"{operator} takes an object, got {argument!r}")
    extra = set(argument) - fields
    if extra:
        raise InvalidFactor(f"{operator} has unexpected fields {sorted(extra)}")
    if fields == {"offset"}:
        value = argument.get("offset")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < MAX_WINDOW:
            raise InvalidFactor(f"offset out of range: {value!r}")
    elif fields == {"window"}:
        value = argument.get("window")
        limit = MAX_WINDOW if operator == "mean" or operator in {"std_dev", "max", "min"} else MAX_WINDOW - 1
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= limit:
            raise InvalidFactor(f"{operator} window out of range: {value!r}")
    elif fields == {"value"}:
        value = argument.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidFactor(f"const must be an integer, got {value!r}")


def request_hypotheses(base_url, api_key, model, theme, count, timeout):
    prompt = (
        f"{GRAMMAR}\n"
        f"研究方向：{theme}\n"
        f"请提出 {count} 个互不相同的因子表达式。每个都要能用上面的语法精确表达；"
        "如果一个想法无法表达，就换一个能表达的。\n"
        '只输出 JSON：{"hypotheses":[{"expr":..., "rationale":"..."}]}'
    )
    body = {
        "model": model,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你是量化研究助手。只输出严格 JSON，不要 markdown 代码块，不要解释。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload["choices"][0]["message"].get("content", "")


def parse_and_keep(raw, model, theme):
    """Split the model's answer into accepted and rejected hypotheses.

    Both lists are reported. A run that silently drops half the output teaches
    nobody anything; the rejections are how you find out the prompt is wrong.
    """
    text = raw.strip()
    # Models sometimes wrap JSON in a fenced block despite instructions.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        return [], [{"reason": f"unparseable_json: {error}", "raw": raw[:500]}]
    items = payload.get("hypotheses")
    if not isinstance(items, list) or not items:
        return [], [{"reason": "no_hypotheses_field", "raw": raw[:500]}]

    accepted, rejected = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or "expr" not in item:
            rejected.append({"reason": "missing_expr", "index": index, "item": item})
            continue
        try:
            validate(item["expr"])
        except InvalidFactor as error:
            rejected.append(
                {
                    "reason": str(error),
                    "index": index,
                    "expr": item.get("expr"),
                }
            )
            continue
        accepted.append(
            {
                "expr": item["expr"],
                "rationale": item.get("rationale", ""),
                "proposed_by": f"llm:{model}",
                "theme": theme,
                # Explicitly not evidence: a proposal has no measured quality
                # until it has been evaluated against data.
                "status": "proposed",
            }
        )
    return accepted, rejected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ASTOCK_LLM_BASE", ""))
    parser.add_argument("--model", default=os.environ.get("ASTOCK_LLM_MODEL", "deepseek-v4.1-flash"))
    parser.add_argument("--theme", required=True)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    api_key = os.environ.get("ASTOCK_LLM_KEY")
    if not args.base_url or not api_key:
        print("ASTOCK_LLM_BASE / ASTOCK_LLM_KEY must be set", file=sys.stderr)
        return 2

    raw = request_hypotheses(
        args.base_url, api_key, args.model, args.theme, args.count, args.timeout
    )
    accepted, rejected = parse_and_keep(raw, args.model, args.theme)
    report = {
        "theme": args.theme,
        "model": args.model,
        "accepted": accepted,
        "rejected": rejected,
        "note": (
            "Proposals only. A hypothesis has no measured quality until it is "
            "evaluated; it is not evidence and it is not a signal."
        ),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(
        f"accepted {len(accepted)}, rejected {len(rejected)} -> {args.out}",
        file=sys.stderr,
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
