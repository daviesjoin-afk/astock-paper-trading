# -*- coding: utf-8 -*-
"""Scientific evidence gate for Champion / Challenger promotion.

The gate deliberately uses only post-proposal shadow NAV observations where the
Champion and Challenger were evaluated on the same immutable market snapshot.
It never reads formal trading results and never tunes candidate parameters.

The last part of the chronological sample is treated as a holdout.  Promotion
requires a positive paired excess return, a conservative lower confidence bound
above zero on the full paired sample, and continued positive behavior in the
holdout.  This is an engineering promotion gate, not a claim of academic proof;
serial correlation and multiple-testing correction remain explicit residual
risks for later work.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any, Mapping

PROMOTION_SCIENCE_VERSION = "promotion-science-v1"
MIN_PAIRED_SNAPSHOTS = 12
MIN_PAIRED_INTERVALS = 10
MIN_DISTINCT_DAYS = 5
HOLDOUT_FRACTION = 0.40
MIN_HOLDOUT_INTERVALS = 4
CONFIDENCE_Z = 1.96
MIN_HOLDOUT_POSITIVE_RATIO = 0.55


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _paired_rows(conn, challenger_id: int, since: str, until: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT c.snapshot_checksum, c.created_at, c.nav_date,
                  c.nav AS champion_nav, x.nav AS challenger_nav,
                  x.created_at AS challenger_created_at, x.nav_date AS challenger_nav_date
             FROM shadow_nav c
             JOIN shadow_nav x
               ON x.challenger_id=c.challenger_id
              AND x.snapshot_checksum=c.snapshot_checksum
              AND x.role='challenger'
            WHERE c.challenger_id=? AND c.role='champion'
              AND c.created_at>=? AND c.created_at<?
            ORDER BY c.created_at, c.id""",
        (int(challenger_id), str(since), str(until)),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if str(item["created_at"]) != str(item["challenger_created_at"]):
            raise ValueError("Champion/Challenger shadow NAV timestamps differ for one snapshot")
        champion_nav = float(item["champion_nav"] or 0)
        challenger_nav = float(item["challenger_nav"] or 0)
        if champion_nav <= 0 or challenger_nav <= 0:
            raise ValueError("shadow NAV must be positive for scientific promotion evidence")
        result.append({
            "snapshot_checksum": str(item["snapshot_checksum"]),
            "created_at": str(item["created_at"]),
            "nav_date": str(item["nav_date"] or item["created_at"])[:10],
            "champion_nav": champion_nav,
            "challenger_nav": challenger_nav,
        })
    return result


def _sample_stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    if not values:
        return {
            "n": 0, "mean": 0.0, "stdev": None, "stderr": None,
            "lower_95": None, "positive_ratio": 0.0,
        }
    mean = statistics.fmean(values)
    if n >= 2:
        stdev = statistics.stdev(values)
        stderr = stdev / math.sqrt(n)
        lower = mean - CONFIDENCE_Z * stderr
    else:
        stdev = None
        stderr = None
        lower = None
    return {
        "n": n,
        "mean": mean,
        "mean_bps": mean * 10000.0,
        "stdev": stdev,
        "stderr": stderr,
        "lower_95": lower,
        "lower_95_bps": None if lower is None else lower * 10000.0,
        "positive_ratio": sum(1 for value in values if value > 0) / n,
    }


def _paired_excess_returns(rows: list[Mapping[str, Any]]) -> list[float]:
    values: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        champion_return = float(current["champion_nav"]) / float(previous["champion_nav"]) - 1.0
        challenger_return = float(current["challenger_nav"]) / float(previous["challenger_nav"]) - 1.0
        values.append(challenger_return - champion_return)
    return values


def evaluate_promotion_evidence(
    conn,
    challenger_id: int,
    since: str,
    until: str,
) -> dict[str, Any]:
    """Evaluate paired post-proposal shadow evidence for one Challenger.

    ``evaluable=False`` means more evidence is required and the Challenger should
    remain in shadow.  ``evaluable=True, promotable=False`` means the configured
    sample requirement has been reached but the candidate failed the scientific
    promotion gate.
    """
    try:
        rows = _paired_rows(conn, challenger_id, since, until)
    except (ValueError, TypeError) as exc:
        return {
            "version": PROMOTION_SCIENCE_VERSION,
            "evaluable": False,
            "promotable": False,
            "reason": str(exc),
            "failed": ["paired_shadow_integrity"],
            "window_start": str(since),
            "window_end": str(until),
        }

    snapshot_count = len(rows)
    intervals = _paired_excess_returns(rows)
    distinct_days = len({row["nav_date"] for row in rows})
    evidence_checksum = _checksum(rows)
    base = {
        "version": PROMOTION_SCIENCE_VERSION,
        "method": "paired-excess-return-normal-ci+chronological-holdout",
        "oos": True,
        "oos_source": "post_proposal_shadow",
        "window_start": str(since),
        "window_end": str(until),
        "snapshot_count": snapshot_count,
        "paired_interval_count": len(intervals),
        "distinct_days": distinct_days,
        "evidence_checksum": evidence_checksum,
        "thresholds": {
            "min_paired_snapshots": MIN_PAIRED_SNAPSHOTS,
            "min_paired_intervals": MIN_PAIRED_INTERVALS,
            "min_distinct_days": MIN_DISTINCT_DAYS,
            "holdout_fraction": HOLDOUT_FRACTION,
            "min_holdout_intervals": MIN_HOLDOUT_INTERVALS,
            "confidence_z": CONFIDENCE_Z,
            "min_holdout_positive_ratio": MIN_HOLDOUT_POSITIVE_RATIO,
        },
    }

    shortages: list[str] = []
    if snapshot_count < MIN_PAIRED_SNAPSHOTS:
        shortages.append(f"paired snapshots {snapshot_count} < {MIN_PAIRED_SNAPSHOTS}")
    if len(intervals) < MIN_PAIRED_INTERVALS:
        shortages.append(f"paired intervals {len(intervals)} < {MIN_PAIRED_INTERVALS}")
    if distinct_days < MIN_DISTINCT_DAYS:
        shortages.append(f"distinct days {distinct_days} < {MIN_DISTINCT_DAYS}")
    if shortages:
        return {
            **base,
            "evaluable": False,
            "promotable": False,
            "reason": "科学晋升证据不足：" + "; ".join(shortages),
            "failed": ["sample_size"],
        }

    holdout_n = max(MIN_HOLDOUT_INTERVALS, int(math.ceil(len(intervals) * HOLDOUT_FRACTION)))
    if holdout_n >= len(intervals):
        holdout_n = max(1, len(intervals) // 2)
    development = intervals[:-holdout_n]
    holdout = intervals[-holdout_n:]
    full_stats = _sample_stats(intervals)
    development_stats = _sample_stats(development)
    holdout_stats = _sample_stats(holdout)

    checks = [
        {
            "key": "full_mean_positive",
            "passed": full_stats["mean"] > 0,
            "reason": "配对超额收益均值未为正",
        },
        {
            "key": "full_lower_95_positive",
            "passed": full_stats["lower_95"] is not None and full_stats["lower_95"] > 0,
            "reason": "配对超额收益 95% 置信下界未高于 0",
        },
        {
            "key": "development_mean_positive",
            "passed": development_stats["mean"] > 0,
            "reason": "前段样本未体现正超额收益",
        },
        {
            "key": "holdout_mean_positive",
            "passed": holdout_stats["mean"] > 0,
            "reason": "后段 holdout 超额收益均值未为正",
        },
        {
            "key": "holdout_positive_ratio",
            "passed": holdout_stats["positive_ratio"] >= MIN_HOLDOUT_POSITIVE_RATIO,
            "reason": "后段 holdout 正超额比例不足",
        },
    ]
    failed = [item["key"] for item in checks if not item["passed"]]
    promotable = not failed
    return {
        **base,
        "evaluable": True,
        "promotable": promotable,
        "reason": None if promotable else "科学晋升门禁未通过",
        "failed": failed,
        "checks": checks,
        "full": full_stats,
        "development": development_stats,
        "holdout": holdout_stats,
        "holdout_interval_count": len(holdout),
    }


def verify_promotion_evidence(conn, challenger_id: int, stored: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute the stored scientific decision before promotion.

    This blocks legacy READY rows that have no scientific evidence and detects
    mutation of the shadow NAV evidence after evaluation.
    """
    if not isinstance(stored, Mapping) or stored.get("version") != PROMOTION_SCIENCE_VERSION:
        return {"valid": False, "reason": "缺少当前版本的科学晋升证据"}
    since = stored.get("window_start")
    until = stored.get("window_end")
    if not since or not until:
        return {"valid": False, "reason": "科学晋升证据缺少固定评估窗口"}
    current = evaluate_promotion_evidence(conn, challenger_id, str(since), str(until))
    if not current.get("evaluable") or not current.get("promotable"):
        return {"valid": False, "reason": current.get("reason") or "科学晋升门禁当前不通过", "current": current}
    if current.get("evidence_checksum") != stored.get("evidence_checksum"):
        return {"valid": False, "reason": "科学晋升证据在评估后发生变化", "current": current}
    if not stored.get("promotable"):
        return {"valid": False, "reason": "已存科学晋升决策不是 promotable", "current": current}
    return {"valid": True, "current": current}
