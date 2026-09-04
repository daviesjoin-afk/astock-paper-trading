# -*- coding: utf-8 -*-
"""选股 alpha 周报：验证"选出来的票是否真的赚钱"。

对 selection_tracking.db 的每日 topN 候选（selection_picks）与模拟盘
实际成交（paper_signals, status='filled'）做 K 线前向回放：

  r1 = 信号日收盘 -> 次日收盘
  r3 = 信号日收盘 -> 第 3 日收盘

并与同日随机 300 只股票基准对比，输出超额收益。修复 6（2026-08-28）：
此前选股质量从未被系统性度量，本次 -0.7%~-1.7% 的次日负 alpha 靠亏钱
才发现；本报告每周五收盘后自动生成，负 alpha 应在报告里直接可见。

只读操作；输出 reports/selection_alpha_report.md 并打印一行 JSON 摘要。
"""
import csv
import datetime
import json
import os
import random
import sqlite3
import statistics

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data_cache")
KLINE_DIR = os.path.join(DATA_DIR, "klines")
REPORT_PATH = os.path.join(BASE, "reports", "selection_alpha_report.md")
WINDOW_DAYS = 30
BENCH_N = 300


def load_kline(code):
    path = os.path.join(KLINE_DIR, f"{code}.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    out[row["date"]] = (float(row["open"]), float(row["close"]))
                except (KeyError, ValueError):
                    continue
    except OSError:
        return {}
    return out


def forward_returns(kline, day, horizon):
    """signal-day close -> close N trading days later."""
    if day not in kline:
        return None
    dates = sorted(d for d in kline if d >= day)
    if len(dates) <= horizon:
        return None
    base = kline[day][1]
    return kline[dates[horizon]][1] / base - 1


def benchmark_map(dates, horizon):
    """mean forward return of a random stock sample on each date."""
    try:
        codes = [f[:-4] for f in os.listdir(KLINE_DIR) if f.endswith(".csv")]
    except OSError:
        return {}
    random.seed(20260828)
    sample = random.sample(codes, min(BENCH_N, len(codes)))
    per_date = {}
    for code in sample:
        kline = load_kline(code)
        for day in dates:
            value = forward_returns(kline, day, horizon)
            if value is not None:
                per_date.setdefault(day, []).append(value)
    return {day: statistics.mean(v) for day, v in per_date.items() if v}


def evaluate(picks, horizon, label):
    """picks: list of (strategy, day, code). Returns summary dict."""
    by_strategy = {}
    kline_cache = {}
    dates = {day for _, day, _ in picks}
    bench = benchmark_map(dates, horizon)
    for strategy, day, code in picks:
        kline = kline_cache.setdefault(code, load_kline(code))
        value = forward_returns(kline, day, horizon)
        if value is None:
            continue
        item = by_strategy.setdefault(strategy, {"r": [], "excess": []})
        item["r"].append(value)
        if day in bench:
            item["excess"].append(value - bench[day])
    lines = [f"\n### {label}（T+{horizon} 收盘对收盘）\n",
             "| 策略 | 样本 | 均值收益 | 胜率 | 超额(vs随机基准) | 基准 |",
             "|---|---|---|---|---|---|"]
    all_r, all_x = [], []
    for strategy in sorted(by_strategy):
        item = by_strategy[strategy]
        mean_r = statistics.mean(item["r"])
        win = sum(v > 0 for v in item["r"]) / len(item["r"])
        mean_x = statistics.mean(item["excess"]) if item["excess"] else None
        bench_mean = statistics.mean(bench.values()) if bench else None
        lines.append(
            f"| {strategy} | {len(item['r'])} | {mean_r * 100:+.2f}% | {win * 100:.0f}% | "
            f"{(mean_x * 100) if mean_x is not None else float('nan'):+.2f}% | "
            f"{(bench_mean * 100) if bench_mean is not None else float('nan'):+.2f}% |"
        )
        all_r.extend(item["r"])
        all_x.extend(item["excess"])
    summary = {
        "label": label,
        "horizon": horizon,
        "n": len(all_r),
        "mean_return": round(statistics.mean(all_r) * 100, 2) if all_r else None,
        "mean_excess": round(statistics.mean(all_x) * 100, 2) if all_x else None,
    }
    return lines, summary


def picks_from_tracking(days):
    db = os.path.join(DATA_DIR, "selection_tracking.db")
    if not os.path.exists(db):
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT r.strategy, r.data_asof_date, p.code, p.rank_no
                 FROM selection_picks p JOIN selection_runs r ON r.id = p.run_id
                 WHERE r.data_asof_date >= ? AND r.data_asof_date IS NOT NULL""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [(str(r[0]), str(r[1])[:10], str(r[2])) for r in rows]


def picks_from_signals(days):
    db = os.path.join(DATA_DIR, "paper_trading.sqlite3")
    if not os.path.exists(db):
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT account_id, intended_date, code FROM paper_signals
                WHERE status='filled' AND intended_date >= ?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [(f"filled:{r[0]}", str(r[1])[:10], str(r[2])) for r in rows]


def main():
    tracking = picks_from_tracking(WINDOW_DAYS)
    filled = picks_from_signals(WINDOW_DAYS)
    report = [
        "# 选股 Alpha 周报",
        "",
        f"生成时间：{datetime.datetime.now().isoformat(timespec='seconds')}；"
        f"窗口：最近 {WINDOW_DAYS} 天；基准：同日随机 {BENCH_N} 只股票均值。",
        "",
        "判读：超额为负 = 选股弱于随机买入，排序因子在损耗净值；连续两周为负需回滚或重检排序。",
    ]
    summaries = []
    if tracking:
        lines, s1 = evaluate(tracking, 1, "每日 topN 候选")
        report += lines
        summaries.append(s1)
        lines, s3 = evaluate(tracking, 3, "每日 topN 候选")
        report += lines
        summaries.append(s3)
    else:
        report.append("\nselection_tracking.db 无样本（selection_picks 为空或库缺失）。")
    if filled:
        lines, s2 = evaluate(filled, 1, "模拟盘实际成交信号")
        report += lines
        summaries.append(s2)
    else:
        report.append("\npaper_signals 无 filled 样本。")
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    print(json.dumps({"report": REPORT_PATH, "summaries": summaries}, ensure_ascii=False))


if __name__ == "__main__":
    main()
