# -*- coding: utf-8 -*-
"""Read-only quality audit for the persisted selection factor snapshot.

This module deliberately does not participate in scoring, screening, risk
checks, or order generation.  It profiles the factor *snapshot* at its
declared grain (normally one row per security) and the selection-tracking
ledger separately, so a missing or suspicious field can never silently become
a trading decision.  All results are JSON-safe and suitable for attaching to
the adaptive overview or an audit endpoint.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import math
import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence


QUALITY_VERSION = "factor-quality-shadow-v1"
DEFAULT_FACTOR_COLUMNS = (
    "mom5", "mom20", "mom60", "rev5", "vol20", "vol_surge", "flow_proxy",
    "rsi14", "macd_dif", "ma5", "ma10", "ma20", "ma60", "price",
    "turnover", "volume_ratio", "main_pct", "super_net", "super_pct",
    "score", "raw_score", "fundamental_score", "sentiment_score",
)
DEFAULT_REQUIRED_COLUMNS = (
    "code", "last_date", "three_up", "boll_mid_breakout", "above_ma5_5d",
    "above_ma10_5d", "above_boll_mid", "above_ma60", "above_all_ma",
    "weekly_oversold", "monthly_oversold",
)
DATE_COLUMNS = ("asof_date", "last_date", "data_asof_date", "signal_date", "run_date")
META_COLUMNS = {
    "code", "name", "industry", "sector", "industry_name", "board", "market",
    "exchange", "security_type", "last_date", "asof_date", "data_asof_date",
    "signal_date", "run_date", "quote_at", "source", "source_name",
}


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        if "T" in text or " " in text:
            return dt.datetime.fromisoformat(text).date()
        return dt.date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _normalise_rows(rows: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result = []
    for row in rows or []:
        if isinstance(row, Mapping):
            result.append({str(key): value for key, value in row.items()})
    return result


def load_factor_rows(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a CSV without guessing or filling missing fields."""
    metadata = {"path": path, "exists": os.path.exists(path), "columns": []}
    if not metadata["exists"]:
        return [], metadata
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            metadata["columns"] = list(reader.fieldnames or [])
            rows = _normalise_rows(reader)
        metadata["size_bytes"] = os.path.getsize(path)
        return rows, metadata
    except (OSError, UnicodeError, csv.Error) as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        return [], metadata


def load_tracking_records(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load candidate picks at their stored grain, if the ledger is present."""
    metadata = {"path": path, "exists": os.path.exists(path), "table": "selection_picks"}
    if not metadata["exists"]:
        return [], metadata
    try:
        with sqlite3.connect(path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT r.run_date, r.strategy, r.strategy_name, p.code,
                          p.rank_no, p.score, p.industry
                     FROM selection_picks p
                     JOIN selection_runs r ON r.id = p.run_id
                    ORDER BY r.run_date DESC, r.strategy, p.rank_no"""
            ).fetchall()
        records = [dict(row) for row in rows]
        metadata["rows"] = len(records)
        return records, metadata
    except (OSError, sqlite3.Error) as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        return [], metadata


def _columns(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        result.update(str(key) for key in row)
    return result


def _missingness(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> dict[str, dict[str, Any]]:
    total = len(rows)
    output = {}
    for column in columns:
        missing = 0
        for row in rows:
            value = row.get(column)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing += 1
        output[column] = {
            "missing": missing,
            "total": total,
            "missing_rate_pct": round(100 * missing / total, 3) if total else None,
            "present_rate_pct": round(100 * (total - missing) / total, 3) if total else None,
        }
    return output


def _infer_numeric_columns(rows: Sequence[Mapping[str, Any]], columns: set[str]) -> list[str]:
    preferred = [name for name in DEFAULT_FACTOR_COLUMNS if name in columns]
    inferred = []
    for column in sorted(columns):
        if column in META_COLUMNS or column in preferred:
            continue
        values = [_safe_float(row.get(column)) for row in rows]
        if sum(value is not None for value in values) >= max(3, min(20, len(rows))):
            inferred.append(column)
    return preferred + inferred


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 8)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[low], 8)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 8)


def _distribution(rows: Sequence[Mapping[str, Any]], column: str) -> dict[str, Any]:
    values = [_safe_float(row.get(column)) for row in rows]
    values = [value for value in values if value is not None]
    q01, q05, q25, q50, q75, q95, q99 = (
        _quantile(values, p) for p in (0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99)
    )
    iqr = (q75 - q25) if q75 is not None and q25 is not None else None
    low_fence = q25 - 1.5 * iqr if iqr is not None else None
    high_fence = q75 + 1.5 * iqr if iqr is not None else None
    outliers = sum(
        1 for value in values
        if low_fence is not None and high_fence is not None
        and (value < low_fence or value > high_fence)
    )
    return {
        "column": column,
        "valid": len(values),
        "missing": len(rows) - len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "quantiles": {"p01": q01, "p05": q05, "p25": q25, "p50": q50,
                      "p75": q75, "p95": q95, "p99": q99},
        "iqr": round(iqr, 8) if iqr is not None else None,
        "robust_outlier_count": outliers,
        "robust_outlier_rate_pct": round(100 * outliers / len(values), 3) if values else None,
    }


def _correlation(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> dict[str, Any]:
    # A compact pairwise Pearson matrix; pairwise deletion is explicit in the
    # output so missing factors are not silently imputed.
    selected = list(columns[:48])
    matrix = {column: {} for column in selected}
    sample_counts = {}
    high_pairs = []
    for left in selected:
        for right in selected:
            pairs = [
                (_safe_float(row.get(left)), _safe_float(row.get(right)))
                for row in rows
            ]
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            key = f"{left}::{right}"
            sample_counts[key] = len(pairs)
            if len(pairs) < 3:
                value = None
            else:
                left_values, right_values = zip(*pairs, strict=True)
                left_mean, right_mean = statistics.mean(left_values), statistics.mean(right_values)
                numerator = sum((a - left_mean) * (b - right_mean) for a, b in pairs)
                left_var = sum((a - left_mean) ** 2 for a in left_values)
                right_var = sum((b - right_mean) ** 2 for b in right_values)
                denominator = math.sqrt(left_var * right_var)
                value = round(numerator / denominator, 6) if denominator else None
            matrix[left][right] = value
            if left < right and value is not None and abs(value) >= 0.90:
                high_pairs.append({"left": left, "right": right, "correlation": value,
                                   "samples": len(pairs)})
    return {
        "columns": selected,
        "matrix": matrix,
        "pair_sample_counts": sample_counts,
        "high_correlation_pairs": high_pairs,
        "note": "两两删除缺失值；少于3个共同样本不计算相关性。",
    }


def _industry_concentration(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    field = next((name for name in ("industry", "sector", "industry_name")
                  if any(str(row.get(name) or "").strip() for row in rows)), None)
    if field is None:
        return {"status": "unavailable", "field": None,
                "reason": "行业字段缺失或全部为空，不伪造行业归属。"}
    counts = Counter(str(row.get(field)).strip() for row in rows if str(row.get(field) or "").strip())
    total = sum(counts.values())
    shares = {key: count / total for key, count in counts.items()} if total else {}
    top = counts.most_common(10)
    return {
        "status": "available" if total else "unavailable", "field": field,
        "covered_rows": total, "unique_industries": len(counts),
        "top_industries": [
            {"industry": key, "rows": count, "share_pct": round(100 * shares[key], 3)}
            for key, count in top
        ],
        "top1_share_pct": round(100 * (top[0][1] / total), 3) if top and total else None,
        "top5_share_pct": round(100 * sum(count for _, count in top[:5]) / total, 3) if total else None,
        "hhi": round(sum(share * share for share in shares.values()), 6) if shares else None,
        "note": "按因子快照行计数；未将行业缺失行分配到任何行业。",
    }


def _asof_quality(rows: Sequence[Mapping[str, Any]], asof: Any = None) -> dict[str, Any]:
    field = next((name for name in DATE_COLUMNS if any(_safe_date(row.get(name)) for row in rows)), None)
    if field is None:
        return {"status": "unknown", "field": None,
                "reason": "未找到可解析的 asof/last_date 字段。"}
    parsed = [_safe_date(row.get(field)) for row in rows]
    valid = [value for value in parsed if value is not None]
    comparison = _safe_date(asof) or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    future_rows = sum(1 for value in valid if value > comparison)
    invalid_rows = len(rows) - len(valid)
    return {
        "status": "ok" if valid and not future_rows and not invalid_rows else "warning",
        "field": field, "comparison_asof": comparison.isoformat(),
        "valid_rows": len(valid), "invalid_rows": invalid_rows,
        "min": min(valid).isoformat() if valid else None,
        "max": max(valid).isoformat() if valid else None,
        "future_rows": future_rows,
        "future_rate_pct": round(100 * future_rows / len(rows), 3) if rows else None,
    }


def _strategy_duplicates(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    records = _normalise_rows(records)
    if not records:
        return {"status": "unavailable", "rows": 0,
                "reason": "没有 selection_tracking 记录，无法判断五策略重复候选。"}
    by_day_code: dict[tuple[str, str], set[str]] = defaultdict(set)
    strategy_counts = Counter()
    for row in records:
        code = str(row.get("code") or "").strip()
        strategy = str(row.get("strategy") or row.get("strategy_name") or "").strip()
        day = str(row.get("run_date") or row.get("signal_date") or "").strip()
        if not code or not strategy:
            continue
        by_day_code[(day, code)].add(strategy)
        strategy_counts[strategy] += 1
    repeated = [
        {"date": day, "code": code, "strategies": sorted(strategies), "count": len(strategies)}
        for (day, code), strategies in sorted(by_day_code.items()) if len(strategies) > 1
    ]
    pair_counts = Counter()
    for item in repeated:
        for left, right in itertools.combinations(item["strategies"], 2):
            pair_counts[f"{left} × {right}"] += 1
    return {
        "status": "available", "rows": len(records),
        "strategy_rows": dict(strategy_counts), "unique_day_code": len(by_day_code),
        "repeated_day_code_count": len(repeated),
        "repeated_rate_pct": round(100 * len(repeated) / len(by_day_code), 3) if by_day_code else None,
        "pair_counts": dict(pair_counts), "examples": repeated[:50],
        "grain": "同一选股日、同一股票代码跨策略重复候选",
    }


def audit_factor_rows(
    rows: Iterable[Mapping[str, Any]] | None,
    *,
    asof: Any = None,
    source: Mapping[str, Any] | None = None,
    strategy_records: Iterable[Mapping[str, Any]] | None = None,
    required_columns: Sequence[str] = DEFAULT_REQUIRED_COLUMNS,
) -> dict[str, Any]:
    """Return an auditable, read-only factor quality report."""
    rows = _normalise_rows(rows)
    columns = _columns(rows)
    missing_columns = [column for column in required_columns if column not in columns]
    numeric_columns = _infer_numeric_columns(rows, columns)
    duplicate_codes = Counter(str(row.get("code") or "").strip() for row in rows)
    duplicate_codes.pop("", None)
    duplicate_keys = {code: count for code, count in duplicate_codes.items() if count > 1}
    exact_duplicates = len(rows) - len({json.dumps(_json_safe(row), ensure_ascii=False, sort_keys=True)
                                        for row in rows})
    missingness = _missingness(rows, required_columns)
    distributions = [_distribution(rows, column) for column in numeric_columns]
    correlation = _correlation(rows, numeric_columns)
    industry = _industry_concentration(rows)
    temporal = _asof_quality(rows, asof=asof)
    strategy = _strategy_duplicates(strategy_records or [])

    findings = []
    if not rows:
        findings.append({"id": "empty_snapshot", "severity": "critical",
                         "message": "因子快照为空，不能用于任何筛选或回测。"})
    if missing_columns:
        findings.append({"id": "missing_required_columns", "severity": "high",
                         "message": "缺少必需因子/元数据列，不能把缺列当作全量覆盖。",
                         "columns": missing_columns})
    if duplicate_keys:
        affected = sum(duplicate_keys.values())
        rate = 100 * affected / len(rows) if rows else None
        findings.append({"id": "duplicate_code", "severity": "high" if (rate or 0) > 1 else "medium",
                         "message": "同一股票出现多行，需确认是否混入多日期或多来源粒度。",
                         "duplicate_code_count": len(duplicate_keys), "affected_rows": affected,
                         "affected_rate_pct": round(rate, 3) if rate is not None else None})
    if temporal.get("future_rows", 0):
        findings.append({"id": "future_asof", "severity": "critical",
                         "message": "发现晚于比较截止日的因子记录，存在前视风险。",
                         "future_rows": temporal["future_rows"], "field": temporal.get("field")})
    if temporal.get("invalid_rows", 0):
        findings.append({"id": "invalid_asof", "severity": "high",
                         "message": "部分因子记录的截止日期无法解析，时间口径不可验证。",
                         "invalid_rows": temporal["invalid_rows"]})
    high_missing = [column for column, item in missingness.items()
                    if item["missing_rate_pct"] is not None and item["missing_rate_pct"] > 20]
    if high_missing and not missing_columns:
        findings.append({"id": "factor_missingness", "severity": "high",
                         "message": "必需列缺失率超过20%，评分横截面可能失真。",
                         "columns": high_missing})
    if correlation["high_correlation_pairs"]:
        findings.append({"id": "factor_collinearity", "severity": "medium",
                         "message": "存在高度相关因子，权重相加可能重复计算同一信息。",
                         "pairs": correlation["high_correlation_pairs"][:20]})
    if industry.get("status") == "available" and (industry.get("top1_share_pct") or 0) >= 50:
        findings.append({"id": "industry_concentration", "severity": "medium",
                         "message": "单一行业占比过高，仅作影子暴露提醒，不改变候选。",
                         "top1_share_pct": industry.get("top1_share_pct"),
                         "hhi": industry.get("hhi")})
    if strategy.get("status") == "available" and strategy.get("repeated_day_code_count", 0):
        findings.append({"id": "strategy_overlap", "severity": "low",
                         "message": "五策略存在重复候选，供组合层观察，不改变策略分数。",
                         "repeated_day_code_count": strategy.get("repeated_day_code_count")})

    severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    highest = max((severity_order[item["severity"]] for item in findings), default=0)
    status = "blocked" if highest >= 4 else "warning" if highest >= 2 else "ok"
    return _json_safe({
        "version": QUALITY_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "read_only": True,
        "trading_impact": "none",
        "dataset": {
            "grain": "预期每个代码、一个最新完整交易日的因子快照；实际粒度由重复代码和日期检查确认。",
            "rows": len(rows), "columns": sorted(columns), "column_count": len(columns),
            "unique_codes": len({str(row.get("code") or "").strip() for row in rows if str(row.get("code") or "").strip()}),
            "empty_code_rows": sum(1 for row in rows if not str(row.get("code") or "").strip()),
            "duplicate_code_count": len(duplicate_keys), "exact_duplicate_rows": exact_duplicates,
            "source": dict(source or {}),
        },
        "coverage": {
            "required_columns": list(required_columns), "missing_columns": missing_columns,
            "missingness": missingness,
        },
        "numeric_factors": {
            "columns": numeric_columns, "distributions": distributions,
            "correlation": correlation,
        },
        "temporal": temporal,
        "industry_concentration": industry,
        "strategy_overlap": strategy,
        "findings": findings,
        "rules": [
            "只读影子审计，不修改候选、分数、交易或风控参数。",
            "缺行业字段不分配行业；缺日期不宣称点时有效。",
            "两两相关使用共同有效样本，缺失值不作零值填充。",
        ],
    })


def report_from_cache(cache_dir: str, *, tracking_db_path: str | None = None,
                      asof: Any = None) -> dict[str, Any]:
    factor_path = os.path.join(cache_dir, "selection_factors.csv")
    rows, source = load_factor_rows(factor_path)
    tracking_path = tracking_db_path or os.path.join(cache_dir, "selection_tracking.db")
    records, tracking_source = load_tracking_records(tracking_path)
    source = dict(source)
    source["tracking"] = tracking_source
    return audit_factor_rows(rows, asof=asof, source=source, strategy_records=records)


def self_test() -> dict[str, Any]:
    base = {"code": "000001", "last_date": "2026-08-11", "three_up": 1,
            "boll_mid_breakout": 1, "above_ma5_5d": 1, "above_ma10_5d": 1,
            "above_boll_mid": 1, "above_ma60": 1, "above_all_ma": 1,
            "weekly_oversold": 0, "monthly_oversold": 0, "mom5": 0.1,
            "mom20": 0.2, "flow_proxy": 0.3, "industry": "银行"}
    empty = audit_factor_rows([])
    assert empty["status"] == "blocked" and empty["dataset"]["rows"] == 0
    missing = audit_factor_rows([{"code": "000001"}])
    assert "last_date" in missing["coverage"]["missing_columns"]
    duplicate = audit_factor_rows([base, dict(base, mom5=0.2)])
    assert duplicate["dataset"]["duplicate_code_count"] == 1
    future = audit_factor_rows([dict(base, last_date="2099-01-01")], asof="2026-08-11")
    assert future["temporal"]["future_rows"] == 1 and future["status"] == "blocked"
    correlated = audit_factor_rows([dict(base, code=f"00000{i}", mom20=i, flow_proxy=i * 2)
                                    for i in range(1, 8)])
    assert correlated["numeric_factors"]["correlation"]["high_correlation_pairs"]
    concentrated = audit_factor_rows(
        [dict(base, code=f"00000{i}", industry="银行" if i < 7 else "医药") for i in range(1, 8)],
        strategy_records=[
            {"run_date": "2026-08-11", "strategy": "三日策略", "code": "000001"},
            {"run_date": "2026-08-11", "strategy": "五日策略", "code": "000001"},
        ],
    )
    assert concentrated["industry_concentration"]["top1_share_pct"] > 50
    assert concentrated["strategy_overlap"]["repeated_day_code_count"] == 1
    return {"status": "ok", "cases": ["empty", "missing_columns", "duplicate_code",
                                        "future_asof", "high_correlation", "industry_overlap"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="read-only selection factor quality audit")
    parser.add_argument("--cache-dir", default="data_cache")
    parser.add_argument("--tracking-db", default=None)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    result = self_test() if args.self_test else report_from_cache(
        args.cache_dir, tracking_db_path=args.tracking_db, asof=args.asof
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
