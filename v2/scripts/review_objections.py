#!/usr/bin/env python3
"""Turn repeated AI objections into evidence a human can act on.

The daily review already records what the model said. That record died in a
file. This reads a window of review records and answers a narrower, more
useful question: **has the model objected to the same thing repeatedly?**

Repeated objection is not proof. It is a signal worth reading, and it must not
be able to move a candidate on its own — so this never writes candidate state
and never produces a pass. What it produces is:

* a per-objection tally with the days it appeared,
* a `repeated` flag once the same objection shows up on enough distinct days,
* an explicit refusal when there is not enough history to say anything.

The distinction that matters: three rejections on one day is one bad day, not
a pattern. Repetition is counted over distinct review days.

Usage:
    python scripts/review_objections.py --reviews ../work/reports \
        --days 20 --threshold 3 --out ../work/objections.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import re
import sys

# Objections arrive as prose. These are the classes worth counting, matched by
# keyword because the model is not asked to emit a code — forcing a code would
# make it choose one of ours rather than say what it saw.
CATEGORIES = {
    "evidence_missing": ("证据不足", "缺少依据", "无法验证", "no evidence", "insufficient evidence"),
    "position_mismatch": ("仓位", "不到目标", "目标权重", "half", "underweight", "仓位不足"),
    "stop_loss_concern": ("止损", "stop loss", "risk of loss"),
    "signal_weak": ("信号弱", "score 低", "阈值", "weak signal"),
    "concentration": ("集中", "concentration", "单一持仓"),
    "data_quality": ("数据缺", "停牌", "missing", "data gap", "缺口"),
}


def classify(text: str) -> list[str]:
    """Which objection classes a piece of prose belongs to.

    A sentence can carry more than one; nothing here forces a single label.
    An unclassifiable objection still counts as an objection — see `other`.
    """
    lowered = text.lower()
    hits = [name for name, keys in CATEGORIES.items()
            if any(key.lower() in lowered for key in keys)]
    return hits or ["other"]


def read_reviews(directory: Path, limit: int) -> list[dict]:
    """Review records, newest last, at most `limit` days."""
    files = sorted(directory.glob("*.review.json"))
    out = []
    for path in files[-limit:]:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        day = path.name.replace(".review.json", "")
        out.append({"day": day, "records": records})
    return out


def objection_text(record: dict) -> str:
    """The model's objection, from whichever field the task populated."""
    for key in ("concerns", "objection", "reason", "summary", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            joined = " ".join(str(item) for item in value if str(item).strip())
            if joined:
                return joined
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviews", required=True, type=Path)
    parser.add_argument("--days", type=int, default=20,
                        help="how many recent review days to consider")
    parser.add_argument("--threshold", type=int, default=3,
                        help="distinct days before an objection counts as repeated")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.reviews.is_dir():
        raise SystemExit(f"review directory not found: {args.reviews}")

    reviews = read_reviews(args.reviews, args.days)
    tally: dict[str, set[str]] = defaultdict(set)
    per_day: dict[str, dict[str, int]] = {}
    rejected_labels: dict[str, set[str]] = defaultdict(set)

    for entry in reviews:
        day = entry["day"]
        counts: dict[str, int] = defaultdict(int)
        for record in entry["records"]:
            status = record.get("status")
            if status != "ok":
                # A failed call is not an objection. Counting it as one would
                # manufacture a pattern out of an outage.
                continue
            parsed = record.get("parsed") or record.get("result") or {}
            verdict = str(parsed.get("verdict") or parsed.get("decision") or "")
            text = objection_text(parsed) or objection_text(record)
            # Only a non-approval is an objection.
            approving = verdict in ("同意", "approve", "approved", "agree")
            if approving:
                continue
            for category in classify(text or verdict):
                tally[category].add(day)
                counts[category] += 1
                label = record.get("label")
                if label:
                    rejected_labels[category].add(str(label))
        if counts:
            per_day[day] = dict(counts)

    repeated = {
        category: {
            "distinct_days": len(days),
            "days": sorted(days),
            "subjects": sorted(rejected_labels.get(category, ())),
            "repeated": len(days) >= args.threshold,
        }
        for category, days in tally.items()
    }

    report = {
        "review_days_considered": len(reviews),
        "review_days_with_objections": len(per_day),
        "threshold": args.threshold,
        "repeated": {k: v for k, v in repeated.items() if v["repeated"]},
        "all": repeated,
        "per_day": per_day,
        "verdict": (
            "insufficient_history" if len(reviews) < args.threshold
            else ("repeated_objections" if any(v["repeated"] for v in repeated.values())
                  else "no_repeated_objection")
        ),
        "note": (
            "Repetition is counted over distinct days, not messages: three "
            "objections in one review are one day. Nothing here changes a "
            "candidate's state — this is input for a human decision."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in
                      ("review_days_considered", "review_days_with_objections",
                       "threshold", "verdict")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
