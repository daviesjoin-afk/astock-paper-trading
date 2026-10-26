# -*- coding: utf-8 -*-
"""竞价特征作为**候选因子**的影子评估（只读，不参与交易、不改权重）。

背景：09:15–09:25 的竞价数据从 2026-10-26 起按日归档（``auction_tracking``），
但"竞价情况值不值得进选股流程"不能用当天的一两个观察拍板。本模块只回答两件事：

1. 已归档的竞价日里，竞价特征与**开盘后的收益**有没有稳定的横截面关系
   （Rank IC、符号一致性、高分组减低分组）；
2. 按事先写死的门槛，够不够格被"接纳"为正式因子。

门槛是**先写死再看数据**的，避免事后挑一个好看的窗口：
至少 ``MIN_DAYS`` 个有效交易日、至少一个目标上 ``|平均 IC| >= MIN_MEAN_IC``
且符号一致率 ``>= MIN_SIGN_CONSISTENCY``。达不到就输出 ``insufficient_days``
或 ``not_admitted`` 并列出还差什么——不填零、不插值、不声称有效。

本模块不做的事：不写账本、不写权重、不改候选、不联网；`enabled` 永远是 False，
真正的接入点在 ``factors.py`` + ``factor_calibration.FACTOR_GROUPS``，需要人工改配置。
"""
from __future__ import annotations

import csv
import datetime as dt
import math
from pathlib import Path

import auction_tracking as tracking
import data_fetcher as dfc

VERSION = "auction-factor-shadow-v1"
MIN_DAYS = 20
MIN_MEAN_IC = 0.03
MIN_SIGN_CONSISTENCY = 0.60
MAX_CODES_PER_DAY = 400

FEATURES = {
    "auction_pct": "竞价涨跌幅（%）",
    "amount_ratio": "竞价成交额 / 前一日成交额",
    "main_pct": "竞价主力净额 / 竞价成交额（%）",
    "vol_ratio": "竞价量比",
    "turnover": "竞价换手率（%）",
}
TARGETS = {
    "open_to_close": "当日开盘到收盘（%）",
    "close_to_next_close": "当日收盘到次日收盘（%）",
}
BOUNDARY = (
    "这是**研究证据**：只用已归档的竞价快照与日线缓存，不参与下单、不改权重、不改候选。"
    "样本来自本机归档，跨日不足时结论无效；竞价金额只覆盖观测到的个股，不是全市场竞价额。"
)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def load_kline_days(code):
    """读取该股日线（date→{open, close, amount}）；缓存缺失返回空表，不联网。"""
    path = Path(dfc.kline_cache_path(str(code)))
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return {}
    reader = csv.DictReader(raw)
    if not reader.fieldnames:
        return {}
    fields = {name.strip().lower(): name for name in reader.fieldnames}
    date_key = fields.get("date") or fields.get("trade_date") or fields.get("日期")
    if date_key is None:
        return {}
    bars = {}
    for row in reader:
        day = _date(row.get(date_key))
        if day is None:
            continue
        close = _number(row.get(fields.get("close", "")))
        open_price = _number(row.get(fields.get("open", "")))
        amount = _number(row.get(fields.get("amount", "")))
        if close is None or close <= 0:
            continue
        bars[day] = {"open": open_price, "close": close, "amount": amount}
    return bars


def _rank(values):
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[order[position]] = average
        index = end + 1
    return ranks


def spearman(pairs):
    """秩相关；样本 < 3 或某一侧方差为 0 时返回 None（不硬凑一个数）。"""
    usable = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(usable) < 3:
        return None
    xs = _rank([x for x, _ in usable])
    ys = _rank([y for _, y in usable])
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _features(row):
    amount = _number(row.get("amount"))
    main_net = _number(row.get("main_net"))
    return {
        "auction_pct": _number(row.get("pct")),
        "amount": amount,
        "main_pct": (main_net / amount * 100.0
                     if main_net is not None and amount and amount > 0 else None),
        "vol_ratio": _number(row.get("vol_ratio")),
        "turnover": _number(row.get("turnover")),
    }


def _day_cross_section(day, rows, bars_by_code, cutoff=None):
    """一天的横截面；目标收益只能使用截至 ``cutoff`` 已收盘的日线。"""
    samples = []
    # 先按金额取流动性最好的样本：归档行本身没有排序，直接截断会变成"按代码顺序"抽样。
    ordered = sorted(rows, key=lambda item: -(_number(item.get("amount")) or 0.0))
    for row in ordered[:MAX_CODES_PER_DAY]:
        code = str(row.get("code") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        bars = bars_by_code(code)
        if not bars:
            continue
        bar = bars.get(day)
        if bar is None:
            continue
        features = _features(row)
        if features["amount"] is None:
            continue
        previous = [key for key in sorted(bars) if key < day]
        prev_amount = bars[previous[-1]]["amount"] if previous else None
        features["amount_ratio"] = (
            features["amount"] / prev_amount if prev_amount and prev_amount > 0 else None)
        open_price = bar.get("open")
        close_cutoff = cutoff.date() if cutoff is not None and (
            cutoff.hour, cutoff.minute) >= (15, 0) else None
        target = {}
        if cutoff is None or day < cutoff.date() or day == close_cutoff:
            if open_price and open_price > 0 and bar.get("close"):
                target["open_to_close"] = (bar["close"] / open_price - 1.0) * 100.0
        following = [key for key in sorted(bars)
                     if key > day and (cutoff is None
                                      or key < cutoff.date() or key == close_cutoff)]
        if following:
            next_close = bars[following[0]]["close"]
            if next_close and bar.get("close"):
                target["close_to_next_close"] = (next_close / bar["close"] - 1.0) * 100.0
        if not target:
            continue
        samples.append({"code": code, "features": features, "targets": target})
    return samples


def _summarise_feature(daily_ics):
    values = [value for value in daily_ics if value is not None]
    if not values:
        return {"days": 0, "mean_ic": None, "sign_consistency": None}
    positive = sum(1 for value in values if value > 0)
    negative = len(values) - positive
    dominant = max(positive, negative) / len(values)
    return {
        "days": len(values),
        "mean_ic": round(sum(values) / len(values), 4),
        "sign_consistency": round(dominant, 3),
        "positive_days": positive,
        "negative_days": negative,
    }


def build_report(asof=None, *, lookback_days=tracking.ARCHIVE_KEEP_DAYS):
    """按已归档的竞价日评估竞价特征；数据不足时明确说不足，不猜。"""
    now = asof or dt.datetime.now(tracking.CN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tracking.CN)
    now = now.astimezone(tracking.CN)
    # 归档取数和收益可见性共用同一截止时间；收盘前不能消费当日收盘结果。
    days = [path for path in tracking.archived_days(lookback_days)
            if (day := _date(path.stem)) is not None
            and (day < now.date() or (day == now.date() and now.time() >= dt.time(15, 0)))]
    usable_days, missing = [], []
    kline_cache = {}

    def bars_for(code):
        if code not in kline_cache:
            kline_cache[code] = load_kline_days(code)
        return kline_cache[code]

    per_day = {}
    for path in days:
        day = _date(path.stem)
        payload = tracking.load_archived_day(path.stem)
        rows = payload.get("rows") or []
        if day is None or not rows:
            missing.append(path.stem)
            continue
        samples = _day_cross_section(day, rows, bars_for, cutoff=now)
        if len(samples) < 5:
            missing.append(path.stem)
            continue
        usable_days.append(day.isoformat())
        per_day[day] = samples

    stats = {target: {feature: [] for feature in FEATURES} for target in TARGETS}
    for day, samples in per_day.items():
        for target in TARGETS:
            for feature in FEATURES:
                pairs = [(sample["features"].get(feature), sample["targets"].get(target))
                         for sample in samples]
                ic = spearman(pairs)
                if ic is not None:
                    stats[target][feature].append(ic)

    summary = {
        target: {feature: _summarise_feature(values)
                 for feature, values in feature_stats.items()}
        for target, feature_stats in stats.items()
    }
    report = {
        "version": VERSION,
        "as_of": now.isoformat(),
        "status": "ok" if usable_days else "insufficient_days",
        "boundary": BOUNDARY,
        "enabled": False,
        "archived_days": len(days),
        "usable_days": usable_days,
        "usable_day_count": len(usable_days),
        "skipped_days": missing[:20],
        "features": dict(FEATURES),
        "targets": dict(TARGETS),
        "stats": summary,
        "thresholds": {
            "min_days": MIN_DAYS,
            "min_abs_mean_ic": MIN_MEAN_IC,
            "min_sign_consistency": MIN_SIGN_CONSISTENCY,
        },
        "note": "竞价数据从 2026-10-26 起按日归档；天数不足时任何 IC 都只是样本内噪声。",
    }
    report["admission"] = admit(report)
    return report


def admit(report):
    """按先写死的门槛判断能否进入选股流程；默认永远是"未接纳"。"""
    days = int(report.get("usable_day_count") or 0)
    reasons = []
    candidates = []
    if days < MIN_DAYS:
        reasons.append(f"有效交易日 {days} < {MIN_DAYS}")
    for target, features in (report.get("stats") or {}).items():
        for feature, item in features.items():
            mean_ic = item.get("mean_ic")
            consistency = item.get("sign_consistency")
            signal_days = int(item.get("days") or 0)
            if signal_days < MIN_DAYS:
                if mean_ic is not None and consistency is not None:
                    reasons.append(
                        f"{target}/{feature} 有效交易日 {signal_days} < {MIN_DAYS}")
                continue
            if (mean_ic is not None and consistency is not None
                    and abs(mean_ic) >= MIN_MEAN_IC and consistency >= MIN_SIGN_CONSISTENCY):
                candidates.append({"target": target, "feature": feature,
                                   "days": signal_days,
                                   "mean_ic": mean_ic,
                                   "sign_consistency": consistency})
    if days >= MIN_DAYS and not candidates:
        reasons.append(
            f"没有任何特征同时满足 |平均 IC| >= {MIN_MEAN_IC} 且符号一致率 >= {MIN_SIGN_CONSISTENCY}")
    return {
        "admitted": False,
        "status": "not_admitted",
        "reasons": reasons or ["需要人工复核并显式开启（本模块不自动改选股）"],
        "qualifying": candidates,
        "next_step": "阈值达标后仍需人工在 factors.py / factor_calibration 接入并做样本外对照。",
    }


def evidence_summary(report):
    """给 AI 研究入口的有界载荷：不带逐日明细。"""
    return {key: value for key, value in (report or {}).items() if key != "per_day"}


__all__ = ["VERSION", "MIN_DAYS", "MIN_MEAN_IC", "MIN_SIGN_CONSISTENCY", "FEATURES",
           "TARGETS", "BOUNDARY", "spearman", "build_report", "admit",
           "evidence_summary", "load_kline_days"]
