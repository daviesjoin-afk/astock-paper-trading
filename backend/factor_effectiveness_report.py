"""Read-only, conditional factor diagnostics from saved paper-signal snapshots.

No factor is rebuilt from today's data. Returns are research close-to-close
labels issued by selection_labels, not fills, account P&L or executable alpha.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

import data_paths
from paper_numeric import coerced_number as _coerced_number
import factor_calibration as FC
import selection_labels as SL

VERSION = "factor-effectiveness-v1"
TZ = dt.timezone(dt.timedelta(hours=8))
HORIZONS = (1, 5, 10)
MIN_PAIRS = 5
MIN_DATES = 20
FACTORS = tuple(FC.FACTOR_GROUPS)
BOUNDARY = (
    "仅评估已保存的模拟盘候选信号（含被拦截和未成交记录），不是全市场因子检验。"
    "收益为决策日之后首个交易日收盘至其后N个交易日收盘的原始价格收益；"
    "未扣费用、未验证可成交性或复权，不代表账户盈亏、因果贡献或调权重后的净值改善。"
    "逐日等权统计；持有窗口可能重叠，20个有效日期仅是展示门槛，不代表统计显著。"
    "保留的是账本当前保存的决策快照，不还原已被刷新覆盖的早期快照；财务披露时点未单独认证。"
    "交易日取本地沪深300日线日期，依赖该缓存的完整性。"
)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


# 数值读取合同的唯一 owner：宽松解析、严格有效（布尔/NaN/无穷一律未知）。
_number = _coerced_number


def _moment(value):
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.replace(tzinfo=TZ) if moment.tzinfo is None else moment.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def _object(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _load_prices(path, sources):
    if not path.is_file():
        sources[path.name] = {"status": "missing"}
        return {}
    try:
        raw = path.read_bytes()
        prices = {}
        for row in csv.DictReader(raw.decode("utf-8-sig").splitlines()):
            day = dt.date.fromisoformat(row["date"]).isoformat()
            if day in prices:
                raise ValueError("duplicate price date")
            prices[day] = row["close"]
        sources[path.name] = {"status": "loaded", "sha256": hashlib.sha256(raw).hexdigest()}
        return prices
    except (OSError, UnicodeError, KeyError, ValueError):
        sources[path.name] = {"status": "invalid"}
        return {}


def _read_samples(path, cutoff, asof):
    """Never create/migrate the ledger; read live and archive in one transaction."""
    rejected = Counter()
    seen = {}
    conflicts = set()
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "paper_signals" not in tables:
            raise sqlite3.DatabaseError("paper_signals table missing")
        for table in ("paper_signals", "paper_signals_archive"):
            if table not in tables:
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            fields = ("id", "account_id", "signal_date", "code", "created_at", "payload",
                      "strategy_id", "strategy_version", "strategy_checksum")
            selection = ",".join(name if name in columns else f"NULL AS {name}" for name in fields)
            for row in conn.execute(f"SELECT {selection} FROM {table} WHERE signal_date>=?", (cutoff,)):
                payload = _object(row["payload"])
                snapshot = _mapping(payload.get("decision_snapshot"))
                decision_at = snapshot.get("decision_at") if "decision_at" in snapshot else row["created_at"]
                moment = _moment(decision_at)
                if moment is None or len(str(decision_at or "")) <= 10:
                    rejected["invalid_decision_at"] += 1
                    continue
                if moment > asof:
                    rejected["future_decision"] += 1
                    continue
                if moment.date().isoformat() < cutoff:
                    rejected["outside_window"] += 1
                    continue
                if not payload:
                    rejected["invalid_payload_rows"] += 1
                pick = _mapping(payload.get("pick"))
                trace = _mapping(pick.get("candidate_trace"))
                frozen_factors = _mapping(snapshot.get("factors"))
                raw = _mapping(frozen_factors.get("raw"))
                factor_source = "decision_snapshot.factors.raw"
                if not raw:
                    raw = _mapping(pick.get("factor_snapshot"))
                    factor_source = "pick.factor_snapshot"
                if not raw:
                    raw = _mapping(trace.get("factor_snapshot"))
                    factor_source = "pick.candidate_trace.factor_snapshot"
                components = _mapping(frozen_factors.get("score_components")) or _mapping(pick.get("score_components"))
                weights = _mapping(components.get("weights"))
                clean_weights = {k: _number(v) for k, v in weights.items() if k in FACTORS}
                valid_weights = bool(clean_weights) and all(v is not None and v >= 0 for v in clean_weights.values())
                valid_weights = valid_weights and sum(clean_weights.values()) > 0
                identity = {"account_id": row["account_id"], "strategy_id": row["strategy_id"],
                            "strategy_version": row["strategy_version"], "strategy_checksum": row["strategy_checksum"],
                            "calibration_version": components.get("factor_calibration_version"),
                            "weights": clean_weights if valid_weights else {}}
                if components.get("factor_input_version"):
                    identity["factor_input_version"] = components["factor_input_version"]
                complete = (bool(identity["strategy_id"]) and isinstance(identity["strategy_version"], int)
                            and identity["strategy_version"] > 0 and bool(identity["strategy_checksum"])
                            and bool(identity["calibration_version"]) and valid_weights)
                factor_dates = [pick.get("historical_factor_date"), trace.get("data_date"),
                                _mapping(payload.get("factor")).get("factor_date"), snapshot.get("asof")]
                future_factor = any(str(day)[:10] > moment.date().isoformat() for day in factor_dates if day)
                if future_factor:
                    rejected["future_factor_snapshot"] += 1
                    continue
                if not str(row["code"] or "").isdigit() or len(str(row["code"])) != 6:
                    rejected["invalid_code"] += 1
                    continue
                sample = {"account_id": row["account_id"], "signal_date": row["signal_date"],
                          "code": row["code"], "decision_at": moment.isoformat(),
                          "decision_time_source": "decision_snapshot" if "decision_at" in snapshot else "created_at",
                          "factor_source": factor_source,
                          "group_id": _hash(identity), "identity": identity,
                          "identity_status": "complete" if complete else "incomplete",
                          "factors": {k: _number(raw.get(k)) for k in FACTORS},
                          "factor_state": {k: ("missing" if raw.get(k) is None else
                                               "observed" if _number(raw.get(k)) is not None else "invalid")
                                           for k in FACTORS},
                          "payload_sha256": hashlib.sha256(str(row["payload"]).encode()).hexdigest(),
                          "source_refs": [{"table": table, "id": row["id"]}]}
                key = (row["account_id"], row["signal_date"], row["code"])
                if key in seen:
                    previous = seen[key]
                    # Outcome/status changes do not create an independent observation.
                    comparable = ("decision_at", "group_id", "factors", "factor_state")
                    if any(sample[k] != previous[k] for k in comparable):
                        conflicts.add(key)
                    else:
                        previous["source_refs"].extend(sample["source_refs"])
                        rejected["duplicate_copy"] += 1
                else:
                    seen[key] = sample
        rejected["conflicting_samples"] = len(conflicts)
        samples = [v for k, v in sorted(seen.items()) if k not in conflicts]
        # One stock is one cross-sectional observation per day/configuration.
        daily = {}
        daily_conflicts = set()
        for sample in samples:
            key = (sample["group_id"], sample["decision_at"][:10], sample["code"])
            if key not in daily:
                daily[key] = sample
            elif any(sample[k] != daily[key][k] for k in ("factors", "factor_state")):
                daily_conflicts.add(key)
            else:
                daily[key]["source_refs"].extend(sample["source_refs"])
                rejected["duplicate_daily_observation"] += 1
        rejected["conflicting_daily_observations"] = len(daily_conflicts)
        return [v for k, v in sorted(daily.items()) if k not in daily_conflicts], dict(rejected)
    finally:
        conn.close()


def _factor_metrics(samples, factor, horizon, identity_status):
    by_day = defaultdict(list)
    states = Counter(sample["factor_state"][factor] for sample in samples)
    for sample in samples:
        label = sample["labels"][str(horizon)]
        value = sample["factors"][factor]
        if value is not None and label["label_status"] == SL.STATUS_VERIFIED:
            by_day[label["decision_trade_date"]].append((value, label["raw_forward_return"]))
    daily = []
    skipped = Counter()
    for day, pairs in sorted(by_day.items()):
        if identity_status != "complete":
            skipped["incomplete_identity_dates"] += 1
            continue
        if len(pairs) < MIN_PAIRS:
            skipped["too_few_pairs_dates"] += 1
            continue
        x, y = zip(*pairs, strict=True)
        if len(set(x)) < 2 or len(set(y)) < 2:
            skipped["constant_factor_dates" if len(set(x)) < 2 else "constant_return_dates"] += 1
            continue
        ic = FC.rank_ic(x, y, minimum_pairs=MIN_PAIRS)
        if ic is None:
            skipped["undefined_ic_dates"] += 1
            continue
        # Strictly separated tails: tied boundary values are never split by row order.
        ordered = sorted(x)
        low_cut = ordered[(len(x) - 1) // 3]
        high_cut = ordered[math.ceil(2 * (len(x) - 1) / 3)]
        low = [ret for value, ret in pairs if value <= low_cut] if low_cut < high_cut else []
        high = [ret for value, ret in pairs if value >= high_cut] if low_cut < high_cut else []
        daily.append({"date": day, "pairs": len(pairs), "rank_ic": round(ic, 8),
                      "low_return_pct": 100 * statistics.mean(low) if low else None,
                      "high_return_pct": 100 * statistics.mean(high) if high else None})
    ics = [row["rank_ic"] for row in daily]
    tails = [row for row in daily if row["low_return_pct"] is not None]
    return {"factor": factor, "horizon": horizon,
            "status": ("incomplete_identity" if identity_status != "complete" else
                       "descriptive" if len(ics) >= MIN_DATES else "insufficient_dates"),
            "candidate_rows": len(samples), "observed_rows": states["observed"],
            "missing_rows": states["missing"], "invalid_rows": states["invalid"],
            "factor_coverage_pct": round(100 * states["observed"] / max(len(samples), 1), 2),
            "paired_rows": sum(map(len, by_day.values())), "paired_dates": len(by_day),
            "valid_ic_dates": len(ics), "group_return_dates": len(tails),
            "mean_rank_ic": round(statistics.mean(ics), 6) if ics else None,
            "positive_ic_ratio": round(sum(v > 0 for v in ics) / len(ics), 4) if ics else None,
            "low_return_pct": round(statistics.mean(r["low_return_pct"] for r in tails), 6) if tails else None,
            "high_return_pct": round(statistics.mean(r["high_return_pct"] for r in tails), 6) if tails else None,
            "high_minus_low_pp": round(statistics.mean(r["high_return_pct"] - r["low_return_pct"] for r in tails), 6)
            if tails else None, "skipped": dict(skipped), "daily": daily}


def build_report(paper_db_path, *, kline_dir=None, asof=None, window_days=90, include_samples=False):
    """No network, schema changes or disk writes; all failure states are explicit."""
    if isinstance(asof, str) and len(asof) == 10:
        asof += "T23:59:59+08:00"
    evaluation = _moment(asof) if asof is not None else dt.datetime.now(TZ)
    if evaluation is None or window_days < 1:
        raise ValueError("invalid asof or window_days")
    cutoff = (evaluation.date() - dt.timedelta(days=window_days)).isoformat()
    root = Path(kline_dir) if kline_dir is not None else Path(paper_db_path).parent / "klines"
    report = {"version": VERSION, "asof": evaluation.isoformat(), "window_start": cutoff,
              "status": "unavailable", "boundary": BOUNDARY,
              "minimum_pairs_per_date": MIN_PAIRS, "minimum_ic_dates": MIN_DATES,
              "label_version": SL.label_version(SL.BASIS_RAW), "groups": [], "issues": []}
    try:
        samples, rejected = _read_samples(paper_db_path, cutoff, evaluation)
    except (OSError, sqlite3.Error):
        report["issues"].append("signal_ledger_unavailable")
        return report
    sources = {}
    benchmark = _load_prices(root / "BENCH_000300.csv", sources)
    sessions = SL.normalize_sessions(benchmark)
    if not sessions:
        report["issues"].append("market_session_calendar_unavailable")
    report["calendar"] = {"source": "BENCH_000300.csv", "first_date": sessions[0] if sessions else None,
                          "last_date": sessions[-1] if sessions else None, "sessions": len(sessions)}
    prices = {}
    labels = {}
    groups = defaultdict(list)
    for sample in samples:
        code = sample["code"]
        if code not in prices:
            prices[code] = _load_prices(root / f"{code}.csv", sources)
        sample["labels"] = {}
        for horizon in HORIZONS:
            key = (code, sample["decision_at"], horizon)
            if key not in labels:
                labels[key] = SL.selection_label(code=code, decision_at=sample["decision_at"],
                    horizon=horizon, sessions=sessions, prices=prices[code], asof=evaluation,
                    basis=SL.BASIS_RAW).as_dict()
            sample["labels"][str(horizon)] = labels[key]
        groups[sample["group_id"]].append(sample)
    for group_id, rows in sorted(groups.items()):
        first = rows[0]
        group = {"group_id": group_id, **first["identity"], "identity_status": first["identity_status"],
                 "candidate_rows": len(rows), "decision_dates": len({r["decision_at"][:10] for r in rows}),
                 "first_decision_at": min(r["decision_at"] for r in rows),
                 "last_decision_at": max(r["decision_at"] for r in rows),
                 "label_counts": {str(h): dict(Counter(r["labels"][str(h)]["label_status"] for r in rows))
                                  for h in HORIZONS},
                 "label_reasons": {str(h): dict(Counter(r["labels"][str(h)]["label_reason"] for r in rows))
                                   for h in HORIZONS}}
        factor_names = sorted(first["identity"]["weights"]) or list(FACTORS)
        group["factors"] = [_factor_metrics(rows, factor, horizon, first["identity_status"])
                            for factor in factor_names for horizon in HORIZONS]
        report["groups"].append(group)
    report["coverage"] = {"candidate_rows": len(samples),
                          "decision_dates": len({s["decision_at"][:10] for s in samples}),
                          "rows_with_factors": sum(any(v is not None for v in s["factors"].values()) for s in samples),
                          "rows_with_complete_identity": sum(s["identity_status"] == "complete" for s in samples),
                          "rejected": rejected}
    report["price_sources"] = sources
    report["sample_fingerprint"] = _hash(samples)
    report["status"] = "computed" if samples and sessions else "unavailable"
    report["report_hash"] = _hash(report)
    if include_samples:
        report["samples"] = samples
    return report


def evidence_summary(report):
    """Latest observed configuration per account; never pool incompatible versions."""
    latest = {}
    for group in report["groups"]:
        account = group["account_id"]
        latest[account] = max(group["last_decision_at"], latest.get(account, ""))
    groups = []
    for group in report["groups"]:
        if group["last_decision_at"] != latest[group["account_id"]]:
            continue
        compact = {k: group[k] for k in ("account_id", "strategy_id", "strategy_version", "strategy_checksum",
                   "group_id", "identity_status", "weights", "calibration_version", "candidate_rows",
                   "decision_dates", "last_decision_at", "label_counts")}
        if "factor_input_version" in group:
            compact["factor_input_version"] = group["factor_input_version"]
        fields = ("factor", "horizon", "status", "factor_coverage_pct", "missing_rows", "invalid_rows",
                  "paired_rows", "valid_ic_dates", "mean_rank_ic", "high_return_pct", "low_return_pct",
                  "high_minus_low_pp", "skipped")
        compact["factors"] = ([{k: item[k] for k in fields} for item in group["factors"]]
                              if group["identity_status"] == "complete" else [])
        groups.append(compact)
    return {"version": VERSION, "status": report["status"], "asof": report["asof"],
            "report_hash": report.get("report_hash"), "boundary": BOUNDARY,
            "minimum_ic_dates": MIN_DATES, "coverage": report.get("coverage", {}),
            "issues": report["issues"], "group_scope": "latest_timestamp_configurations_per_account",
            "omitted_historical_groups": len(report["groups"]) - len(groups), "groups": groups}


def render_markdown(report):
    coverage = report.get("coverage", {})
    states = {"descriptive": "仅描述性统计", "insufficient_dates": "有效日期不足20",
              "incomplete_identity": "身份信息不全"}
    lines = ["# 因子有效性报告", "", f"评估时点：{report['asof']}", "", BOUNDARY, "",
             "Rank IC 是因子排名与后续收益排名的相关程度。收益单位为百分比，组间差为百分点。",
             "样本不足时可展示已有统计，但不能据此宣称因子有效或调整参数。", "",
             f"去重后候选 {coverage.get('candidate_rows', 0)} 条，决策日期 {coverage.get('decision_dates', 0)} 个；"
             f"有可用因子 {coverage.get('rows_with_factors', 0)} 条，身份字段完整 {coverage.get('rows_with_complete_identity', 0)} 条。",
             "", "覆盖明细：`" + json.dumps(coverage, ensure_ascii=False) + "`", "",
             "问题：`" + json.dumps(report["issues"], ensure_ascii=False) + "`", ""]
    for group in report["groups"]:
        version = group["strategy_version"] if group["strategy_version"] is not None else "未记录"
        lines += [f"## {group['account_id']} / 策略版本 {version}", "",
                  f"配置分组：`{group['group_id']}`；候选 {group['candidate_rows']} 条；"
                  f"决策日期 {group['decision_dates']} 个。", "",
                  "保存权重：`" + json.dumps(group["weights"], ensure_ascii=False) + "`", "",
                  "收益窗口状态：`" + json.dumps(group["label_counts"], ensure_ascii=False) + "`", ""]
        if "factor_input_version" in group:
            lines += [f"评分输入版本：`{group['factor_input_version']}`", ""]
        if group["identity_status"] != "complete":
            lines += ["策略身份、因子计算版本或权重字段不全，本组不计算 IC 和分组收益。", ""]
            continue
        lines += [
                  "| 因子 | 天数 | 有效日期 | 配对数 | 覆盖率% | 平均IC | 高分组% | 低分组% | 高减低百分点 | 状态 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for item in group["factors"]:
            values = [item[k] for k in ("factor", "horizon", "valid_ic_dates", "paired_rows", "factor_coverage_pct",
                      "mean_rank_ic", "high_return_pct", "low_return_pct", "high_minus_low_pp", "status")]
            values[-1] = states[values[-1]]
            lines.append("| " + " | ".join("—" if v is None else str(v) for v in values) + " |")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", default=data_paths.data_path("paper_trading.sqlite3"))
    parser.add_argument("--kline-dir")
    parser.add_argument("--asof")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--output-dir", default=data_paths.report_dir())
    args = parser.parse_args()
    report = build_report(args.paper_db, kline_dir=args.kline_dir, asof=args.asof,
                          window_days=args.window_days, include_samples=True)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "factor_effectiveness_report.json").write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    (output / "factor_effectiveness_report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report_hash": report.get("report_hash"),
                      "coverage": report.get("coverage", {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
