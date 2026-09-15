# -*- coding: utf-8 -*-
"""选股 alpha 周报：验证"选出来的票是否真的赚钱"。

对 selection_tracking.db 的每日 topN 候选（selection_picks）与模拟盘
实际成交（paper_signals, status='filled'）做 K 线前向回放：

  r1 = 决策后首个交易日收盘 -> 之后第 1 个交易日收盘
  r3 = 决策后首个交易日收盘 -> 之后第 3 个交易日收盘

并与同日随机 300 只股票基准对比，输出超额收益。

修复 6（2026-08-28）：此前选股质量从未被系统性度量，本次 -0.7%~-1.7% 的
次日负 alpha 靠亏钱才发现。

本模块现在**不再自己实现标签**：所有前向收益一律由
:mod:`selection_labels` 的唯一权威契约生成。此前这里用「信号日收盘价比
次日收盘价」计算，等于把收盘后才形成的信号回填成当天收盘成交 —— 一个
现实中不可执行的 entry。修正后的 entry 是决策日**之后**第一个交易日。

输出的每个样本都带 ``label_status``：``pending``（窗口未走完）与
``unavailable``（缺证据）**不计入**任何均值或胜率，只被计数上报。
绝不再有「未到期当成负例」「缺失当成 0%」。

只读操作；输出 reports/selection_alpha_report.md 并打印一行 JSON 摘要。
"""
import csv
import datetime
import json
import os
import random
import sqlite3
import statistics

import selection_labels as SL

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data_cache")
KLINE_DIR = os.path.join(DATA_DIR, "klines")
REPORT_PATH = os.path.join(BASE, "reports", "selection_alpha_report.md")
WINDOW_DAYS = 30
BENCH_N = 300

#: 与 ``selection_tracking.BENCHMARK_CODE`` 保持同一个指数标的（本地常量，
#: 避免为了一个字符串把数据抓取模块拉进离线报告）。
INDEX_CODE = "BENCH_000300"

#: 选股快照在当日收盘后才形成 → 决策时点取当日收盘后，entry 落到下一个交易日。
DECISION_AFTER_CLOSE_SUFFIX = "T16:00:00+08:00"

#: 权威标签口径。仓库没有可信的 PIT 指数基准序列，因此 v1 以**原始收益**
#: 为 authoritative；报告里"vs 同日随机抽样均值"只是展示层对照，不是标签字段。
LABEL_VERSION = SL.label_version(SL.BASIS_RAW)


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


def market_sessions():
    """权威市场交易日序列：优先指数 K 线的日期，否则退回全部 K 线日期并集。"""
    index_kline = load_kline(INDEX_CODE)
    if index_kline:
        return SL.normalize_sessions(index_kline)
    days = set()
    try:
        names = [name for name in os.listdir(KLINE_DIR) if name.endswith(".csv")]
    except OSError:
        return []
    for name in names:
        days.update(load_kline(name[:-4]))
    return SL.normalize_sessions(days)


def legacy_forward_returns(kline, day, horizon):
    """**已废弃**：signal-day close → close N sessions later。

    保留只为审计对照 —— 它把决策日收盘价当作 entry，对收盘后形成的信号是
    不可执行的。新代码一律走 :func:`selection_labels.selection_label`。
    """
    if day not in kline:
        return None
    dates = sorted(d for d in kline if d >= day)
    if len(dates) <= horizon:
        return None
    base = kline[day][1]
    return kline[dates[horizon]][1] / base - 1


def _prices_of(kline):
    return {day: value[1] for day, value in (kline or {}).items()}


def label_for(code, day, horizon, sessions, *, asof, kline=None):
    """走唯一权威契约生成一条标签（不在这里重算任何收益）。"""
    return SL.selection_label(
        code=code,
        decision_at=f"{day}{DECISION_AFTER_CLOSE_SUFFIX}",
        horizon=horizon,
        sessions=sessions,
        prices=_prices_of(kline if kline is not None else load_kline(code)),
        asof=asof,
        basis=SL.BASIS_RAW,
    )


def benchmark_map(dates, horizon, sessions, *, asof):
    """同日随机 300 只股票的**同期**前向收益均值（报告层对照口径）。

    逐股走同一个标签契约：只有 ``verified`` 的样本参与均值，
    ``pending`` / ``unavailable`` 绝不填 0 混进基准。
    """
    try:
        codes = [f[:-4] for f in os.listdir(KLINE_DIR) if f.endswith(".csv")]
    except OSError:
        return {}
    if not codes:
        return {}
    random.seed(20260828)
    sample = random.sample(codes, min(BENCH_N, len(codes)))
    per_date = {}
    for code in sample:
        kline = load_kline(code)
        for day in dates:
            result = label_for(code, day, horizon, sessions, asof=asof, kline=kline)
            if result.verified:
                per_date.setdefault(day, []).append(result.raw_forward_return)
    return {day: statistics.mean(v) for day, v in per_date.items() if v}


def evaluate(picks, horizon, label, sessions, *, asof):
    """picks: list of (strategy, decision_day, code). Returns (lines, summary)."""
    by_strategy = {}
    kline_cache = {}
    dates = {day for _, day, _ in picks}
    bench = benchmark_map(dates, horizon, sessions, asof=asof)
    status_counts = {status: 0 for status in SL.LABEL_STATUSES}
    for strategy, day, code in picks:
        kline = kline_cache.setdefault(code, load_kline(code))
        result = label_for(code, day, horizon, sessions, asof=asof, kline=kline)
        status_counts[result.label_status] += 1
        if not result.verified:
            # pending（未走完）/ unavailable（缺证据）明确计数，
            # 既不进均值也不当负例。
            continue
        item = by_strategy.setdefault(strategy, {"r": [], "excess": []})
        item["r"].append(result.raw_forward_return)
        if day in bench:
            item["excess"].append(result.raw_forward_return - bench[day])
    lines = [
        f"\n### {label}（T+{horizon} 交易日，entry = 决策后首个交易日收盘；"
        f"标签口径 {LABEL_VERSION}）\n",
        "| 策略 | 已验证样本 | 均值收益 | 胜率 | 超额(vs同日抽样均值) | 基准 |",
        "|---|---|---|---|---|---|",
    ]
    all_r, all_x = [], []
    for strategy in sorted(by_strategy):
        item = by_strategy[strategy]
        mean_r = statistics.mean(item["r"])
        win = sum(value > 0 for value in item["r"]) / len(item["r"])
        mean_x = statistics.mean(item["excess"]) if item["excess"] else None
        bench_mean = statistics.mean(bench.values()) if bench else None
        lines.append(
            f"| {strategy} | {len(item['r'])} | {mean_r * 100:+.2f}% | {win * 100:.0f}% | "
            f"{(mean_x * 100) if mean_x is not None else float('nan'):+.2f}% | "
            f"{(bench_mean * 100) if bench_mean is not None else float('nan'):+.2f}% |"
        )
        all_r.extend(item["r"])
        all_x.extend(item["excess"])
    lines.append(
        "样本状态：" + "，".join(
            f"{status} {status_counts[status]}" for status in SL.LABEL_STATUSES
        )
        + "（非 verified 的样本不计入任何均值或胜率）"
    )
    summary = {
        "label": label,
        "horizon": horizon,
        "label_version": LABEL_VERSION,
        "n": len(all_r),
        "mean_return": round(statistics.mean(all_r) * 100, 2) if all_r else None,
        "mean_excess": round(statistics.mean(all_x) * 100, 2) if all_x else None,
        "label_status_counts": status_counts,
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
    asof = datetime.date.today().isoformat()
    sessions = market_sessions()
    tracking = picks_from_tracking(WINDOW_DAYS)
    filled = picks_from_signals(WINDOW_DAYS)
    report = [
        "# 选股 Alpha 周报",
        "",
        f"生成时间：{datetime.datetime.now().isoformat(timespec='seconds')}；"
        f"窗口：最近 {WINDOW_DAYS} 天；评估时点：{asof}；"
        f"基准：同日随机 {BENCH_N} 只股票均值（仅 verified 样本）。",
        "",
        f"标签口径：`{LABEL_VERSION}`，由 backend/selection_labels.py 唯一权威生成；"
        f"市场交易日序列 {len(sessions)} 个。"
        "entry = 决策日之后第一个交易日收盘（收盘后形成的信号不可回填当日成交）。",
        "",
        "判读：超额为负 = 选股弱于随机买入，排序因子在损耗净值；连续两周为负需回滚或重检排序。",
    ]
    summaries = []
    if tracking:
        lines, s1 = evaluate(tracking, 1, "每日 topN 候选", sessions, asof=asof)
        report += lines
        summaries.append(s1)
        lines, s3 = evaluate(tracking, 3, "每日 topN 候选", sessions, asof=asof)
        report += lines
        summaries.append(s3)
    else:
        report.append("\nselection_tracking.db 无样本（selection_picks 为空或库缺失）。")
    if filled:
        lines, s2 = evaluate(filled, 1, "模拟盘实际成交信号", sessions, asof=asof)
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
