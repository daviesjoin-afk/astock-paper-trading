"""证券范围逐行判断的成本测量（合成数据，仅比较两种写法本身）。

测的是 ``main.scanner`` 里那一步：给定扫描输出的因子表（约 5000 行 × 64 列，
混合 dtype），取出"是否属于可买入证券范围"的布尔掩码。

  A. ``df.apply(lambda row: _buy_scope(...)["allowed"], axis=1)``（旧写法）
  B. ``itertuples`` 逐行调用**同一个** ``_buy_scope``（新写法）

两段都调用同一份规则实现，因此差异只来自"逐行 Series 对象 vs 元组"。
"""
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

import numpy as np
import pandas as pd

import selection_service as SEL

ROWS = 5000
SCOPE_COLUMNS = ("code", "name", "risk_flag", "instrument_type", "security_type")
FACTOR_COLUMNS = (
    "name", "industry", "pe", "pb", "roe", "rev_yoy", "profit_yoy", "net_profit",
    "annual_net_profit", "annual_report_date", "annual_report_published_at",
    "report_published_at", "report_age_days", "profit_source", "mktcap", "float_cap",
    "report_date", "price", "pct", "amount", "turnover", "main_pct", "main_net",
    "mom5_raw", "mom20_raw", "mom60_raw", "vol_surge_raw", "rsi14_raw",
)


def _frame(rows=ROWS):
    rng = np.random.default_rng(7)
    # 覆盖真实分布：主板/创业板、科创板、北交所、ST、ETF、坏代码。
    prefixes = ["600", "601", "603", "000", "001", "002", "300", "301", "688", "830", "430"]
    codes = [f"{prefixes[i % len(prefixes)]}{i:03d}"[:6] for i in range(rows)]
    names = [("ST " if i % 97 == 0 else "") + f"标的{i}" for i in range(rows)]
    kinds = rng.choice(["stock", "etf", "unknown", None], size=rows, p=[0.9, 0.03, 0.04, 0.03])
    data = {
        "code": pd.Series(codes, dtype="object"),
        "name": pd.Series(names, dtype="object"),
        "risk_flag": pd.Series(rng.choice([0, 1, None], size=rows), dtype="object"),
        "instrument_type": pd.Series(kinds, dtype="object"),
        "security_type": pd.Series([None] * rows, dtype="object"),
    }
    for column in FACTOR_COLUMNS:
        if column in data:
            continue
        if column in {"industry", "profit_source"}:
            data[column] = pd.Series(rng.choice(["银行", "钢铁", "半导体"], size=rows), dtype="object")
        else:
            data[column] = pd.Series(rng.standard_normal(rows) * 10, dtype="float64")
    for column in ("three_up", "above_ma5_5d", "above_ma10_5d", "above_boll_mid",
                   "above_ma60", "above_all_ma", "weekly_oversold", "monthly_oversold"):
        data[column] = pd.Series(rng.random(rows) > 0.5, dtype="bool")
    # 真实因子表是 64 列（详见 strategies.build_factor_table）；补齐宽度，
    # 让测量反映"逐行 Series 需要复制多少列"。
    extra = 64 - len(data)
    for position in range(extra):
        data[f"aux_{position}"] = pd.Series(rng.standard_normal(rows), dtype="float64")
    return pd.DataFrame(data)


def _apply_mask(table):
    return table.apply(
        lambda row: SEL._buy_scope(
            row.get("code"), row.get("name"), row.get("risk_flag"),
            row.get("instrument_type") or row.get("security_type"),
        )["allowed"],
        axis=1,
    )


def _itertuples_mask(table):
    present = [column for column in SCOPE_COLUMNS if column in table.columns]
    index = {column: position for position, column in enumerate(present)}
    allowed = []
    for record in table[present].itertuples(index=False, name=None):
        instrument = record[index["instrument_type"]] if "instrument_type" in index else None
        allowed.append(SEL._buy_scope(
            record[index["code"]],
            record[index["name"]] if "name" in index else None,
            record[index["risk_flag"]] if "risk_flag" in index else None,
            instrument or (record[index["security_type"]] if "security_type" in index else None),
        )["allowed"])
    return pd.Series(allowed, index=table.index, dtype=bool)


def _time(fn, table, repeats=3):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        mask = fn(table)
        samples.append(round((time.perf_counter() - start) * 1000, 1))
    return mask, samples


table = _frame()
mask_a, samples_a = _time(_apply_mask, table)
mask_b, samples_b = _time(_itertuples_mask, table)
assert mask_a.equals(mask_b), "两种写法必须给出完全相同的掩码"
print(json.dumps({
    "rows": len(table),
    "columns": len(table.columns),
    "allowed_rows": int(mask_b.sum()),
    "apply_axis1_ms": samples_a,
    "itertuples_ms": samples_b,
    "apply_axis1_median_ms": statistics.median(samples_a),
    "itertuples_median_ms": statistics.median(samples_b),
    "speedup": round(statistics.median(samples_a) / statistics.median(samples_b), 2),
    "identical_mask": True,
    "note": "Synthetic table shaped like the scanner output; same _buy_scope rule in both variants.",
}, ensure_ascii=False, indent=2))
