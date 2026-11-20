#!/usr/bin/env python3
"""Run one LLM task end to end: render, call, validate, degrade honestly.

The executor is deliberately boring. It does not retry with a reworded prompt,
does not "fix" malformed output, and does not fall back to a plausible answer.
A failed job returns a structured failure with a code, because the pipeline
needs to know the difference between "no data", "the model refused", and "the
model produced something unusable".

Usage:
    ASTOCK_LLM_KEY=... python llm_runner.py \
        --task pnl_review --input payload.json --out result.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import llm_tasks


# Failure codes. Shared with llm_tasks.TaskError so a caller branches on one
# vocabulary rather than mixing strings and exceptions.
CODES = (
    "backend_not_configured",
    "timeout",
    "network_error",
    "http_error",
    "empty_output",
    "output_truncated",
    "invalid_json",
    "schema_validation_failed",
    "data_unavailable",
    "unknown_task",
)


class Failure(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def strip_code_fence(text: str) -> str:
    """Remove a markdown fence if the model added one despite instructions."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("```")
    if len(body) < 2:
        return stripped
    inner = body[1]
    if inner.startswith("json"):
        inner = inner[4:]
    return inner.strip()


def call_model(base_url: str, api_key: str, model: str, system: str, user: str,
               temperature: float, max_tokens: int, timeout: float) -> str:
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode("utf-8", "replace")
        raise Failure("http_error", f"{error.code} {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise Failure("network_error", str(error)) from error
    choices = payload.get("choices") or []
    if not choices:
        raise Failure("empty_output", "no choices returned")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    finish = choice.get("finish_reason")
    # Reasoning models spend the output budget on hidden tokens before they
    # write anything. A `length` finish with empty content means the budget was
    # consumed by thinking, which is a configuration problem, not a model
    # refusal — naming it that way is what makes it fixable.
    if finish == "length":
        reasoning = (payload.get("usage") or {}).get("completion_thinking_tokens")             or (payload.get("usage") or {}).get("completion_tokens_details", {}).get("reasoning_tokens")
        raise Failure(
            "output_truncated",
            f"finish_reason=length, reasoning_tokens={reasoning}, content_chars={len(content)}",
        )
    if not content.strip():
        reasoning = (payload.get("usage") or {}).get("completion_thinking_tokens")
        raise Failure(
            "empty_output",
            f"finish_reason={finish}, reasoning_tokens={reasoning}, "
            f"reasoning_field={bool(message.get('reasoning_content'))}",
        )
    return content


def run(task_name: str, payload: dict, *, base_url: str, api_key: str, model: str,
        timeout: float = 120.0, log=None) -> dict:
    """Execute one job. Always returns a record; never raises for a job failure."""
    started = time.time()
    record = {
        "task": task_name,
        "model": model,
        "status": "ok",
    }
    try:
        task = llm_tasks.get(task_name)
        system, user = llm_tasks.render(task, payload)
        record["prompt"] = {"system": system, "user": user}
        raw = call_model(base_url, api_key, model, system, user,
                         task.temperature, task.max_output_tokens, timeout)
        record["raw"] = raw[:4000]
        text = strip_code_fence(raw)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise Failure("invalid_json", str(error)) from error
        validated = task.validator(parsed)
        record["result"] = validated
    except llm_tasks.TaskError as error:
        record["status"] = "failed"
        record["code"] = error.code
        record["detail"] = error.detail
    except Failure as error:
        record["status"] = "failed"
        record["code"] = error.code
        record["detail"] = error.detail
    except Exception as error:  # a bug here must not take the pipeline down
        record["status"] = "failed"
        record["code"] = "unknown_error"
        record["detail"] = f"{type(error).__name__}: {error}"
    record["ms"] = int((time.time() - started) * 1000)
    if log:
        log(record)
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--input", required=True, type=argparse.FileType("r", encoding="utf-8"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--base-url", default=os.environ.get("ASTOCK_LLM_BASE", ""))
    parser.add_argument("--model", default=os.environ.get("ASTOCK_LLM_MODEL", "deepseek-v4.1-flash"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--list", action="store_true", help="print available tasks and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, task in llm_tasks.TASKS.items():
            print(f"{name:22} {task.title:16} inputs={','.join(task.required_inputs)}")
        return 0

    api_key = os.environ.get("ASTOCK_LLM_KEY")
    if not args.base_url or not api_key:
        print(json.dumps({"status": "failed", "code": "backend_not_configured"}))
        return 2

    payload = json.load(args.input)
    record = run(args.task, payload, base_url=args.base_url, api_key=api_key,
                 model=args.model, timeout=args.timeout)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    # A failed job is a normal outcome, not a crash: exit 0 so a batch run can
    # continue and report the whole picture.
    print(json.dumps({"task": args.task, "status": record["status"],
                      "code": record.get("code"), "ms": record["ms"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
