from __future__ import annotations

from pathlib import Path
import re


PATH = Path("backend/strategies.py")
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    text = text.replace(old, new, 1)


def regex_once(pattern: str, replacement: str, label: str) -> None:
    global text
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")


replace_once(
    "import factors as F\n",
    "import factors as F\nimport factor_calibration as FC\n",
    "calibration import",
)
replace_once(
    'def zneg(series):\n    return F.zscore(pd.Series(series, dtype="float64") * -1)\n',
    'def zneg(series):\n    return F.zscore(pd.Series(series, dtype="float64") * -1, fill_missing=False)\n',
    "missing-preserving zneg",
)
replace_once(
    "    momentum_score = (rank01(mom5.clip(-20, 20)) * 0.60\n"
    "                      + rank01(mom20.clip(-40, 60)) * 0.40)\n",
    "    # mom5/mom20 are fractions. Keep winsorisation in the same unit.\n"
    "    momentum_score = (rank01(mom5.clip(-0.20, 0.20)) * 0.60\n"
    "                      + rank01(mom20.clip(-0.40, 0.60)) * 0.40)\n",
    "fraction clips",
)

new_build = '''def build_factor_table(
    price_f: pd.DataFrame,
    fund_f: pd.DataFrame,
    sentiment: dict = None,
    realtime_flow: dict = None,
    realtime_super_flow: dict = None,
):
    """Merge factor sources while keeping alpha and evidence quality separate."""
    idx = price_f.index.intersection(fund_f.index)
    table = pd.DataFrame(index=idx)
    price = price_f.loc[idx]
    fund = fund_f.loc[idx]

    for column in (
        "name", "industry", "pe", "pb", "roe", "rev_yoy", "profit_yoy",
        "net_profit", "annual_net_profit", "annual_report_date",
        "annual_report_published_at", "report_published_at", "report_age_days",
        "profit_source", "mktcap", "float_cap", "report_date",
    ):
        table[column] = fund[column] if column in fund else np.nan
    table["price"] = price["price"]
    table["pct"] = fund["pct_today"] if "pct_today" in fund else np.nan
    table["amount"] = price["amount"] if "amount" in price else np.nan
    table["turnover"] = price["turnover"] if "turnover" in price else np.nan
    table["mom5_raw"] = price["mom5"]
    table["mom20_raw"] = price["mom20"]
    table["mom60_raw"] = price["mom60"]
    table["vol_surge_raw"] = price["vol_surge"]
    table["rsi14_raw"] = price["rsi14"]
    for column in TECHNICAL_COLUMNS:
        table[column] = price[column] if column in price else False

    missing_columns = [
        name for name in ("pct", "amount", "turnover")
        if name not in table.columns or table[name].notna().sum() == 0
    ]
    if missing_columns:
        import json as _json
        import sys as _sys
        print(_json.dumps({
            "alarm": "factor_columns_missing",
            "columns": missing_columns,
            "note": "行情快照关键字段整列缺失；缺失证据保持 unknown 并由 evidence-quality 门禁处理",
        }, ensure_ascii=False), flush=True)
        _sys.stdout.flush()
    table.attrs["factor_warnings"] = missing_columns

    # Missing factor observations stay missing. Composite alpha renormalises
    # over observed components instead of silently replacing unknown with zero.
    pe_z = F.zscore(pd.to_numeric(fund["pe"], errors="coerce") * -1, fill_missing=False)
    pb_z = F.zscore(pd.to_numeric(fund["pb"], errors="coerce") * -1, fill_missing=False)
    roe_z = F.zscore(fund["roe"], fill_missing=False)
    profit_z = F.zscore(fund["profit_yoy"], fill_missing=False)
    revenue_z = F.zscore(fund["rev_yoy"], fill_missing=False)
    mom5_z = F.zscore(price["mom5"], fill_missing=False)
    mom20_z = F.zscore(price["mom20"], fill_missing=False)
    mom60_z = F.zscore(price["mom60"], fill_missing=False)

    table["value"] = FC.weighted_available(
        {"pe": pe_z, "pb": pb_z}, {"pe": 0.5, "pb": 0.5}, index=idx
    )
    table["quality"] = FC.weighted_available(
        {"roe": roe_z, "profit": profit_z, "revenue": revenue_z},
        {"roe": 0.5, "profit": 0.25, "revenue": 0.25},
        index=idx,
    )
    # Short and medium momentum are intentionally disjoint factor families.
    table["mom_short"] = mom5_z
    table["mom"] = FC.weighted_available(
        {"mom20": mom20_z, "mom60": mom60_z},
        {"mom20": 0.6, "mom60": 0.4},
        index=idx,
    )
    table["volsurge"] = F.zscore(price["vol_surge"], fill_missing=False)
    table["rsi"] = F.zscore(
        pd.to_numeric(price["rsi14"], errors="coerce") * -1,
        fill_missing=False,
    )

    price_quality = pd.to_numeric(
        price.get("price_evidence_quality", pd.Series(FC.FULL_QUALITY, index=idx)),
        errors="coerce",
    ).reindex(idx).clip(0.0, 1.0).fillna(0.0)
    table["adjustment_warning"] = price.get(
        "adjustment_warning", pd.Series(False, index=idx)
    ).reindex(idx).fillna(False).astype(bool)
    for factor in ("mom_short", "mom", "volsurge", "rsi"):
        table[f"{factor}_evidence_quality"] = price_quality.where(
            table[factor].notna(), 0.0
        )
    table["value_evidence_quality"] = table["value"].notna().astype(float)
    table["quality_evidence_quality"] = table["quality"].notna().astype(float)

    proxy_flow = F.zscore(price["flow_proxy"], fill_missing=False)
    if realtime_flow:
        flow_series = pd.Series({code: realtime_flow.get(code, np.nan) for code in idx})
        live_flow = F.zscore(flow_series, fill_missing=False)
        table["flow"] = live_flow.combine_first(proxy_flow)
        table["flow_evidence_quality"] = pd.Series(
            np.where(
                live_flow.notna(),
                FC.FULL_QUALITY,
                np.where(proxy_flow.notna(), FC.PROXY_QUALITY, 0.0),
            ),
            index=idx,
            dtype="float64",
        )
        coverage = int(live_flow.notna().sum())
        table["flow_source"] = (
            f"实时主力净流入占比({coverage}/{len(idx)})，缺失项用量价代理（质量单独记录）"
        )
    else:
        table["flow"] = proxy_flow
        table["flow_evidence_quality"] = pd.Series(
            np.where(proxy_flow.notna(), FC.PROXY_QUALITY, 0.0),
            index=idx,
            dtype="float64",
        )
        table["flow_source"] = "量价资金代理（质量单独记录）"

    fallback_super = (
        pd.to_numeric(fund["super_net"], errors="coerce").reindex(idx)
        if "super_net" in fund
        else pd.Series(np.nan, index=idx, dtype="float64")
    )
    if realtime_super_flow:
        live_super = pd.to_numeric(
            pd.Series({code: realtime_super_flow.get(code, np.nan) for code in idx}),
            errors="coerce",
        ).reindex(idx)
        table["super_net_raw"] = live_super.combine_first(fallback_super)
        table["super_net_live"] = live_super.notna()
        table["super_net_source"] = np.where(
            live_super.notna(), "实时超大单净流入", "历史资金字段（仅上下文）"
        )
    else:
        table["super_net_raw"] = fallback_super
        table["super_net_live"] = False
        table["super_net_source"] = "历史资金字段（仅上下文）"

    if sentiment:
        table["hot_rank"] = pd.Series({
            code: (sentiment.get(code) or {}).get("hot_rank") for code in idx
        })
        sentiment_raw = pd.Series({
            code: (sentiment.get(code) or {}).get("sentiment", np.nan) for code in idx
        })
        table["sentiment"] = F.zscore(sentiment_raw, fill_missing=False)
        table["sentiment_evidence_quality"] = table["sentiment"].notna().astype(float)
        table["sentiment_source"] = np.where(
            table["sentiment"].notna(), "实时人气榜", "unavailable"
        )
    else:
        # Momentum/volume are not allowed to masquerade as sentiment.
        table["hot_rank"] = np.nan
        table["sentiment"] = np.nan
        table["sentiment_evidence_quality"] = 0.0
        table["sentiment_source"] = "unavailable"

    table.attrs["factor_calibration_version"] = FC.CALIBRATION_VERSION
    return table


def _bool_column'''
regex_once(
    r"def build_factor_table\(.*?\n\ndef _bool_column",
    new_build,
    "factor table function",
)

old_score = '''    score = pd.Series(0.0, index=table.index)
    for factor, weight in weights.items():
        # 行情快照来自 JSON/CSV 混合缓存时，数值列偶尔会以字符串形式
        # 进入 DataFrame。统一转数值，避免 5 分钟监控因字符串与数字比较
        # 异常退出并把本轮状态误显示成“异常”。
        values = pd.to_numeric(table[factor], errors="coerce").fillna(0.0)
        score += values * weight
    # Keep the unmodified factor score for research.  The execution model still
    # uses ``score`` below exactly as before; these extra columns are evidence
    # for the shadow-validation ledger and do not change ranking or orders.
    base_score = score.copy()
    def numeric_column(name):
        source = table[name] if name in table.columns else pd.Series(0.0, index=table.index)
        return pd.to_numeric(source, errors="coerce").fillna(0.0)
'''
new_score = '''    required_factors = ("sentiment",) if strategy_id == "sentiment_pioneer" else ()
    calibrated = FC.score_factors(
        table,
        weights,
        required_factors=required_factors,
    )
    score = calibrated.alpha_score.copy()
    base_score = score.copy()
    evidence_quality = calibrated.evidence_quality
    evidence_observed_weight = calibrated.observed_weight
    evidence_ok = (
        base_score.notna()
        & calibrated.required_ok
        & evidence_quality.ge(FC.MIN_RUNTIME_EVIDENCE_QUALITY)
    )

    def numeric_column(name):
        source = table[name] if name in table.columns else pd.Series(np.nan, index=table.index)
        return pd.to_numeric(source, errors="coerce")
'''
replace_once(old_score, new_score, "paper score")
replace_once(
    '    star_sector_bonus = numeric_column("star_sector_bonus")\n    score += star_sector_bonus\n',
    '    star_sector_bonus = numeric_column("star_sector_bonus").fillna(0.0)\n    score += star_sector_bonus\n',
    "optional star context",
)
replace_once(
    '    ranked = table.copy()\n    ranked["score_base"] = base_score\n',
    '    # Evidence quality is an audit/gate dimension, never an alpha multiplier.\n'
    '    score = score.mask(~evidence_ok, -999.0)\n\n'
    '    ranked = table.copy()\n'
    '    ranked["score_base"] = base_score\n'
    '    ranked["score_evidence_quality"] = evidence_quality\n'
    '    ranked["score_observed_weight"] = evidence_observed_weight\n'
    '    ranked["score_required_ok"] = calibrated.required_ok\n',
    "final evidence gate",
)
replace_once(
    '                "sector_heat_score", "sector_early_rotation_score",\n',
    '                "sector_heat_score", "sector_early_rotation_score",\n'
    '                "mom_short_evidence_quality", "mom_evidence_quality",\n'
    '                "flow_evidence_quality", "volsurge_evidence_quality",\n'
    '                "sentiment_evidence_quality", "value_evidence_quality",\n'
    '                "quality_evidence_quality", "rsi_evidence_quality",\n',
    "factor quality snapshot",
)
replace_once(
    '                    "version": "paper-score-evidence-v1",\n',
    '                    "version": "paper-score-evidence-v2",\n'
    '                    "factor_calibration_version": FC.CALIBRATION_VERSION,\n'
    '                    "evidence_quality": _number(row.get("score_evidence_quality"), 6),\n'
    '                    "observed_weight": _number(row.get("score_observed_weight"), 6),\n'
    '                    "required_factors_ok": bool(row.get("score_required_ok", False)),\n',
    "score audit version",
)
replace_once(
    '        "conditions_used": conditions,\n',
    '        "conditions_used": conditions,\n'
    '        "factor_calibration": {\n'
    '            "version": FC.CALIBRATION_VERSION,\n'
    '            "minimum_evidence_quality": FC.MIN_RUNTIME_EVIDENCE_QUALITY,\n'
    '            "required_factors": list(required_factors),\n'
    '            "eligible_evidence_rows": int(evidence_ok.sum()),\n'
    '        },\n',
    "result calibration metadata",
)

PATH.write_text(text, encoding="utf-8")
