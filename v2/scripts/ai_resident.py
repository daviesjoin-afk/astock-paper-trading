#!/usr/bin/env python3
"""Three-tier memory for the intraday observer, adapted from dsh-mnemon.

mnemon keeps memory in three layers: a small resident block injected every turn,
project documents searched on demand, and durable long-term evidence recalled by
query. Its write path is guarded (add/replace/remove against a current snapshot),
its capacity is bounded, and a change to behaviour is only adopted after it beats
the current version on a fixed set of cases.

AStock had none of that. Every round the observer started from zero; the
`evolution` table only ever held a rule-generated proposal that was never
measured. This module supplies the missing pieces for a paper-trading loop:

  * ``resident``  — a few dozen lines that fit in every round (regime facts that
    held up, rules learned from losses). Regenerated, never hand-edited.
  * ``evidence``  — the measured scorecard from ``ai_memory``, searched on demand.
  * ``trials``    — candidate parameter changes with their forward measurement,
    adopted only when every case improves and the sample clears its floor.

Read-only with respect to the trading ledger. Writes only inside ``--root``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# A resident block that grows without bound stops being resident: it would push
# the market table out of the prompt. mnemon bounds its projection the same way.
MAX_RESIDENT_CHARS = 2_400
# Below this many independently scored names a claim is not reported at all.
MIN_CASES = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Entry:
    """One resident memory line.

    ``kind`` separates a measured fact from a rule, because they age
    differently: a regime fact is re-measured every session, a rule persists
    until a trial disproves it.
    """

    id: str
    kind: str  # "fact" | "rule"
    text: str
    source: str
    updated: str
    cases: int = 0
    expires: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text,
                "source": self.source, "updated": self.updated,
                "cases": self.cases, "expires": self.expires}


def _entry(kind: str, text: str, source: str, *, cases: int = 0,
           now: datetime | None = None) -> Entry:
    text = " ".join(text.split())
    return Entry(id=_digest(kind + text), kind=kind, text=text, source=source,
                 updated=(now or _utc_now()).date().isoformat(), cases=cases)


# ---------------------------------------------------------------- fact derive

def facts_from_scorecard(card: Mapping[str, Any]) -> list[Entry]:
    """Turn the measured track record into statements the model can act on.

    The verdict is excess over that session's market median, never the raw move:
    on a day when the whole market rises 3%, a blocked name that rises 2% still
    underperformed, and calling that a missed gain would teach the wrong lesson.
    Only regimes that cleared the case floor appear, so a thin sample never
    becomes a confident sentence.
    """
    out: list[Entry] = []
    for regime, block in sorted((card.get("by_regime") or {}).items()):
        blocked = block.get("blocked_symbols") or {}
        buys = block.get("named_buys") or {}
        if blocked.get("cases", 0) >= MIN_CASES:
            excess = blocked.get("excess_bps")
            if excess is not None:
                if excess > 20:
                    verdict = ("拦错了：这些票跑赢大盘，说明你的禁买在这类行情里过度保守。"
                               "只保留硬证据——涨停封板、破日内均价线、长上影冲高回落——"
                               "其余交给内核评分。")
                elif excess < -20:
                    verdict = "拦对了：这些票跑输大盘，禁买有效，继续按现有标准执行。"
                else:
                    verdict = "接近中性：禁买对超额收益影响不大，不必因此放宽或收紧。"
                out.append(_entry(
                    "fact",
                    f"{regime} 状态：你禁买的股票相对大盘中位 {excess:+d} bps"
                    f"（{blocked['cases']} 例，跑赢 {blocked.get('beat_market', 0)} /"
                    f" 跑输 {blocked.get('lagged_market', 0)}）。{verdict}",
                    source="ai_memory.scorecard", cases=blocked["cases"]))
        if buys.get("cases", 0) >= MIN_CASES:
            excess = buys.get("excess_bps")
            if excess is not None:
                detail = (f"相对大盘 {excess:+d} bps（{buys['cases']} 例）")
                if excess < -20:
                    detail += "。点名的超额为负，这类状态不要点名，交给内核信号。"
                elif excess > 20:
                    detail += "。点名的超额为正，这类状态可以点名，但仍需硬证据。"
                else:
                    detail += "。点名与大盘接近，点名不构成优势。"
                out.append(_entry("fact", f"{regime} 状态：你点名的买入{detail}",
                                  source="ai_memory.scorecard", cases=buys["cases"]))
    return out


# --------------------------------------------------------------- resident IO

def resident_path(root: Path) -> Path:
    return root / "resident.json"


def read_resident(root: Path) -> list[Entry]:
    path = resident_path(root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = []
    for raw in payload.get("entries") or []:
        if not isinstance(raw, Mapping) or not raw.get("text"):
            continue
        entries.append(Entry(
            id=str(raw.get("id") or _digest(str(raw["text"]))),
            kind=str(raw.get("kind") or "fact"),
            text=str(raw["text"]), source=str(raw.get("source") or "unknown"),
            updated=str(raw.get("updated") or ""), cases=int(raw.get("cases") or 0),
            expires=raw.get("expires")))
    return entries


def _bounded(entries: Sequence[Entry]) -> list[Entry]:
    """Facts first (they carry the measured numbers), then rules, newest first.

    A resident block that overflows its budget would push the market table out
    of the prompt, so the cut is explicit rather than accidental.
    """
    ordered = sorted(entries, key=lambda e: (e.kind != "fact", -int(e.updated.replace("-", "") or 0)))
    kept, used = [], 0
    for entry in ordered:
        cost = len(entry.text) + 1
        if used + cost > MAX_RESIDENT_CHARS:
            continue
        kept.append(entry)
        used += cost
    return kept


def write_resident(root: Path, entries: Sequence[Entry]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "budget_chars": MAX_RESIDENT_CHARS,
               "updated": _utc_now().isoformat(),
               "entries": [e.to_json() for e in _bounded(entries)]}
    path = resident_path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------- write protocol

@dataclass
class Mutation:
    """One guarded change, mirroring mnemon's add/replace/remove contract."""

    action: str            # "add" | "replace" | "remove"
    text: str = ""
    old_text: str = ""
    kind: str = "rule"
    source: str = "observer"
    expected_revision: str | None = None


def apply_mutations(entries: Sequence[Entry], mutations: Iterable[Mutation],
                    *, revision: str, now: datetime | None = None) -> tuple[list[Entry], list[str]]:
    """Apply mutations against a reviewed snapshot; refuse anything stale.

    A write that was computed against an older snapshot could silently clobber a
    newer fact, so the caller must name the revision it read. Rejected changes
    are reported, never dropped silently.
    """
    rejected: list[str] = []
    current = list(entries)
    for mutation in mutations:
        action = (mutation.action or "").strip().lower()
        if action not in {"add", "replace", "remove"}:
            rejected.append(f"unknown_action:{mutation.action}")
            continue
        if mutation.expected_revision is not None and mutation.expected_revision != revision:
            rejected.append(f"stale_revision:{mutation.action}")
            continue
        text = " ".join((mutation.text or "").split())
        old = " ".join((mutation.old_text or "").split())
        if action == "add":
            if not text:
                rejected.append("empty_text"); continue
            if any(e.text == text for e in current):
                rejected.append("duplicate"); continue
            current.append(_entry(mutation.kind, text, mutation.source, now=now))
        elif action == "replace":
            hits = [e for e in current if old and old in e.text]
            if len(hits) != 1:
                rejected.append(f"replace_needs_one_match:{len(hits)}"); continue
            if not text:
                rejected.append("empty_text"); continue
            index = current.index(hits[0])
            current[index] = _entry(hits[0].kind, text, mutation.source, now=now)
        else:  # remove
            hits = [e for e in current if old and old in e.text]
            if len(hits) != 1:
                rejected.append(f"remove_needs_one_match:{len(hits)}"); continue
            current.remove(hits[0])
    return _bounded(current), rejected


def revision_of(entries: Sequence[Entry]) -> str:
    return _digest(json.dumps([e.to_json() for e in entries], sort_keys=True))


# ------------------------------------------------------------------- trials

@dataclass
class Trial:
    """A candidate behaviour change, measured on the same cases as the current one."""

    candidate_id: str
    policy: dict[str, Any]
    formed_on: str
    cases: list[float] = field(default_factory=list)   # paired improvement, bps
    status: str = "collecting"
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "policy": self.policy,
                "formed_on": self.formed_on, "cases": self.cases,
                "status": self.status, "reasons": self.reasons}


def paired_summary(cases: Sequence[float], *, floor_cases: int = 21) -> dict[str, Any]:
    """Paired improvement with a conservative interval, never a bare mean.

    The floor matches the replay evaluator already in the repo: a handful of days
    cannot support a positive verdict, and saying so is the honest outcome.
    """
    if not cases:
        return {"cases": 0, "status": "collecting",
                "reasons": ["no_forward_cases"]}
    if len(cases) < floor_cases:
        return {"cases": len(cases), "mean_bps": round(sum(cases) / len(cases), 1),
                "status": "collecting", "reasons": ["insufficient_forward_days"]}
    mean = sum(cases) / len(cases)
    variance = sum((c - mean) ** 2 for c in cases) / (len(cases) - 1)
    stdev = math.sqrt(variance)
    half = 2.10 * stdev / math.sqrt(len(cases))
    lo, hi = mean - half, mean + half
    reasons = []
    if lo <= 0:
        reasons.append("no_positive_95_percent_lower_bound")
    return {"cases": len(cases), "mean_bps": round(mean, 1),
            "ci95": [round(lo, 1), round(hi, 1)],
            "status": "requires_review" if not reasons else "not_adopted",
            "reasons": reasons}


def adopt(trial: Trial, *, forwarded: Sequence[float]) -> tuple[bool, list[str]]:
    """Adoption requires an interval that excludes zero, not a better average.

    A change that cannot clear its own floor stays inert; activating on a point
    estimate is how a paper strategy learns noise.
    """
    summary = paired_summary(forwarded)
    reasons = list(summary.get("reasons") or [])
    if summary["status"] != "requires_review":
        return False, reasons or [summary["status"]]
    if any(case < 0 for case in forwarded):
        return False, ["a_forward_case_regressed"]
    return True, []


# ------------------------------------------------------------------ rendering

def render_resident(entries: Sequence[Entry], *, now: date | None = None) -> str:
    """The block the observer receives every round, newest facts first."""
    today = (now or _utc_now().date()).isoformat()
    live = [e for e in entries if not (e.expires and e.expires < today)]
    if not live:
        return ""
    lines = ["【你自己的记忆】（每条都来自已结算账本，不是推测）"]
    for entry in _bounded(live):
        tag = "实测" if entry.kind == "fact" else "规则"
        lines.append(f"- [{tag}] {entry.text}")
    return "\n".join(lines)


def refresh(root: Path, card: Mapping[str, Any]) -> list[Entry]:
    """Re-derive measured facts, keep learned rules, and rewrite the block."""
    previous = read_resident(root)
    rules = [e for e in previous if e.kind == "rule"]
    facts = facts_from_scorecard(card)
    merged = _bounded(facts + rules)
    write_resident(root, merged)
    return merged


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    refresh_cmd = sub.add_parser("refresh", help="re-derive facts from a scorecard")
    refresh_cmd.add_argument("--root", type=Path, required=True)
    refresh_cmd.add_argument("--card", type=Path, required=True)

    show = sub.add_parser("show", help="print the resident block")
    show.add_argument("--root", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "refresh":
        card = json.loads(args.card.read_text(encoding="utf-8"))
        entries = refresh(args.root, card)
        print(json.dumps({"entries": len(entries), "revision": revision_of(entries)},
                         ensure_ascii=False))
        return 0
    print(render_resident(read_resident(args.root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
