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
import selection_tradability as ST
import security_state_point_in_time as SS

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


def benchmark_map(dates, horizon, sessions, *, asof, security_state_fn=None):
    """同日随机 300 只股票的**同期**前向收益均值 —— 两个口径**分开**返回。

    此前 benchmark 只要求 label ``verified``，而 selected 侧在
    ``MODE_EXECUTABLE`` 下还要过 tradability filter。两个 population 不同却相减
    成一个叫"超额"的数字，等于拿"能成交的策略样本"去比"全部随机样本"。

    因此这里返回两个独立的 map：

    * ``market``：只要求 label ``verified``（市场反事实口径）。这是原行为。
    * ``executable``：与 selected 侧**同口径**走 PIT 可成交性契约（entry/exit
      两侧都 ``executable`` 才计入）。**若没有可信的 PIT 历史状态源
      （``security_state_fn is None``），该键整体缺席** —— 报告必须写
      "可执行基准不可得"，而不是把未过滤的均值冒充成可执行基准。
    * ``coverage``：**基准侧**的动作状态覆盖度 ``{required, resolved}``。调用方
      必须用它判断基准是否真的成立：``executable`` 是空 dict（例如 provider 对
      随机基准样本全部返回 ``None``）与"没有 provider"（``None``）是**两种不同**
      的不可得，两者都不得被当成"基准可用"。随机基准票池与策略选中的票是两个
      population，选股侧的覆盖度不能替基准侧作证。

    逐股仍走同一个标签契约：``pending`` / ``unavailable`` 绝不填 0 混进基准。
    """
    empty = {"market": {}, "executable": None, "coverage": {"required": 0, "resolved": 0}}
    try:
        codes = [f[:-4] for f in os.listdir(KLINE_DIR) if f.endswith(".csv")]
    except OSError:
        return dict(empty)
    if not codes:
        return dict(empty)
    random.seed(20260828)
    sample = random.sample(codes, min(BENCH_N, len(codes)))
    per_date = {}
    exec_rows = []
    coverage = {"required": 0, "resolved": 0}
    for code in sample:
        kline = load_kline(code)
        for day in dates:
            result = label_for(code, day, horizon, sessions, asof=asof, kline=kline)
            if not result.verified:
                continue
            per_date.setdefault(day, []).append(result.raw_forward_return)
            if security_state_fn is None:
                continue
            entry_name, entry_flag = _resolve_action_state(
                code, result.entry_date, decision_day=day, decision_name=None,
                security_state_fn=security_state_fn, coverage=coverage,
            )
            exit_name, exit_flag = _resolve_action_state(
                code, result.exit_date, decision_day=day, decision_name=None,
                security_state_fn=security_state_fn, coverage=coverage,
            )
            exec_rows.append(
                ST.SelectionRow(
                    sample_key=f"bench|{code}|{day}",
                    code=code,
                    selected=True,
                    intended_entry_session=result.entry_date,
                    entry_evidence=_tradability_evidence(
                        code, entry_name, entry_flag, result.entry_date,
                        result.entry_price, kline),
                    intended_exit_session=result.exit_date,
                    exit_evidence=_tradability_evidence(
                        code, exit_name, exit_flag, result.exit_date,
                        result.exit_price, kline),
                    market_label_status=result.label_status,
                    market_label_value=result.raw_forward_return,
                )
            )
    market = {day: statistics.mean(v) for day, v in per_date.items() if v}
    if security_state_fn is None:
        # 没有可信历史状态源 → 可执行基准**不可得**（None 而非未过滤均值）。
        return {"market": market, "executable": None, "coverage": coverage}
    built = ST.build_executable_outcomes(exec_rows)
    per_date_exec = {}
    for outcome in built["outcomes"]:
        if not outcome.executable:
            continue
        day = outcome.sample_key.rsplit("|", 1)[-1]
        per_date_exec.setdefault(day, []).append(outcome.executable_return)
    executable = {day: statistics.mean(v) for day, v in per_date_exec.items() if v}
    # ``coverage`` 让调用方区分"provider 存在"与"基准样本真的解析出了动作状态"：
    # provider 对每个 session 都返回 None 时，这里 resolved == 0。
    return {"market": market, "executable": executable, "coverage": coverage}


def _previous_close(kline, session):
    """``session`` 之前最近一个交易日的收盘价（涨跌停的参考价）。

    只读**该 session 之前**的 bar，绝不使用当日或之后的成交信息。
    """
    if not kline or session is None:
        return None
    prior = [day for day in sorted(kline) if day < str(session)[:10]]
    if not prior:
        return None
    try:
        return float(kline[prior[-1]][1])
    except (IndexError, TypeError, ValueError):
        return None


def _action_security_state(code, session, *, decision_day, decision_name,
                           security_state_fn):
    """动作 session **当时**的证券名称 / 风险标记（PIT）。

    ``selection_picks.name`` / ``paper_signals.name`` 只证明**决策日**的名称；
    而 entry 是"决策日之后第一个交易日"、exit 还在更后面。ST / 退市状态可以在
    决策与动作之间发生变化，所以**过期名称不得被当成动作时的资格证据**。

    取值顺序：

    1. 调用方给了 ``security_state_fn`` → 用该 session 的历史状态；
    2. 否则只有"动作 session == 决策日"时才允许复用决策当时的名称；
    3. 其余情况一律 ``(None, None)`` —— 由 contract 判 ``unproven``（fail closed），
       **绝不**用决策日的名称把之后的动作"证明"成可执行。
    """
    if session is None:
        return None, None
    action_session = str(session)[:10]
    if security_state_fn is not None:
        state = security_state_fn(str(code or ""), action_session)
        if state is None:
            return None, None
        if not isinstance(state, dict):
            return state, None
        # provider 若声明了该状态自身的可用时点，就必须**在动作时点可见**才准用：
        # 一条"之后才记下的"名称不能证明更早的动作可执行（PIT 硬门禁）。
        if state.get("available_at") is not None:
            visible = ST.is_visible_at(
                state.get("available_at"), ST.session_close_at(action_session)
            )
            if not visible:
                return None, None
        return state.get("name"), state.get("risk_flag")
    if decision_day is not None and action_session == str(decision_day)[:10]:
        return decision_name, None
    return None, None


def _tradability_evidence(code, name, risk_flag, session, price, kline):
    """把一条 #147 标签 + 日线 + **动作 session 当时**的证券状态适配成 tradability 证据。

    ``halted=False`` 有依据：标签是 ``verified``，意味着 entry/exit 价格已经过了
    ``selection_labels.price_point``（它把 ``halted`` / ``missing`` / ``invalid``
    严格分开）—— 我们没有在这里重新判断停牌，而是消费 #147 已经建立的证据。

    ``name`` / ``risk_flag`` 必须来自 :func:`_action_security_state`，即**该动作
    session 当时**的状态；用决策日的名称或当前快照的名称都是 PIT 违规。
    """
    if session is None:
        return None
    return ST.MarketEvidence(
        session=str(session)[:10],
        available_at=ST.session_close_at(str(session)[:10]),
        price=price,
        reference_price=_previous_close(kline, session),
        halted=False,
        name=name,
        risk_flag=risk_flag,
    )


def _resolve_action_state(code, session, *, decision_day, decision_name,
                          security_state_fn, coverage):
    """调用 :func:`_action_security_state` 并记录**动作状态覆盖度**。

    ``coverage`` 会累计：

    * ``required`` —— 需要动作时状态的 (code, session) 次数；
    * ``resolved`` —— 真的拿到可用 name / risk_flag 的次数。

    "provider 对象存在"与"动作状态真的解析出来了"是两件事：一个 provider 可能
    对每个 session 都返回 ``None``，此时 executable 覆盖度是 0，报告不得声称
    可执行指标可用。
    """
    name, flag = _action_security_state(
        code, session, decision_day=decision_day, decision_name=decision_name,
        security_state_fn=security_state_fn,
    )
    if session is not None:
        coverage["required"] += 1
        if name is not None or flag is not None:
            coverage["resolved"] += 1
    return name, flag


def evaluate(picks, horizon, label, sessions, *, asof,
             tradability_mode=ST.MODE_EXECUTABLE, security_state_fn=None,
             state_source_status=None):
    """picks: list of (strategy, decision_day, code[, name]). Returns (lines, summary).

    ``tradability_mode``
        生产选股质量默认用 ``executable_only``（§22）：只有 entry 与 exit 都真正
        可执行的样本才进入均值/胜率/超额。被拦或证据不足的样本**不会消失** ——
        它们进入 ``tradability_counts`` 与报告里的"可执行"一栏，所以"策略选了 100
        条、其中 80 条真能成交"是可读的。需要 market counterfactual 口径的调用方
        显式传 ``ST.MODE_MARKET``。

    ``security_state_fn(code, session) -> {"name":..., "risk_flag":...} | None``
        **动作 session 当时**的证券状态来源（PIT）。entry/exit 与决策日不是同一天，
        决策当时记录的名称不能证明那时的 ST 资格；没有这个来源时，除"动作 session
        恰为决策日"外一律按 ST 未知 fail closed（``unproven``）。
    """
    if tradability_mode not in ST.EVALUATION_MODES:
        raise ValueError(f"unknown tradability_mode: {tradability_mode!r}")
    by_strategy = {}
    kline_cache = {}
    dates = {pick[1] for pick in picks}
    benches = benchmark_map(
        dates, horizon, sessions, asof=asof, security_state_fn=security_state_fn
    )
    bench = benches.get("market") or {}
    bench_exec = benches.get("executable")
    # 基准侧的**动作状态覆盖度**（由 ``benchmark_map`` 累计，作用域是随机基准样本）。
    # 它必须与选股侧的覆盖度**分开**统计：两侧的 population 不同（随机票池 vs
    # 策略选出的票），一侧解析成功完全不能证明另一侧也解析成功。
    bench_coverage = dict(benches.get("coverage") or {"required": 0, "resolved": 0})
    # 选股侧的**动作状态覆盖度**：区分"provider 对象存在"与"动作状态真的解析出来"。
    coverage = {"required": 0, "resolved": 0}
    status_counts = {status: 0 for status in SL.LABEL_STATUSES}
    verified = []
    for pick in picks:
        strategy, day, code = pick[0], pick[1], pick[2]
        name = pick[3] if len(pick) > 3 else None
        kline = kline_cache.setdefault(code, load_kline(code))
        result = label_for(code, day, horizon, sessions, asof=asof, kline=kline)
        status_counts[result.label_status] += 1
        if not result.verified:
            # pending（未走完）/ unavailable（缺证据）明确计数，
            # 既不进均值也不当负例。
            continue
        verified.append((strategy, day, code, name, result, kline))

    # 可成交性判定走唯一权威实现，不在报告层复制任何规则。
    rows = []
    for strategy, day, code, name, result, kline in verified:
        entry_name, entry_flag = _resolve_action_state(
            code, result.entry_date, decision_day=day, decision_name=name,
            security_state_fn=security_state_fn, coverage=coverage,
        )
        exit_name, exit_flag = _resolve_action_state(
            code, result.exit_date, decision_day=day, decision_name=name,
            security_state_fn=security_state_fn, coverage=coverage,
        )
        rows.append(
            ST.SelectionRow(
                sample_key=f"{strategy}|{day}|{code}",
                code=code,
                selected=True,
                intended_entry_session=result.entry_date,
                entry_evidence=_tradability_evidence(
                    code, entry_name, entry_flag, result.entry_date,
                    result.entry_price, kline),
                intended_exit_session=result.exit_date,
                exit_evidence=_tradability_evidence(
                    code, exit_name, exit_flag, result.exit_date,
                    result.exit_price, kline),
                market_label_status=result.label_status,
                market_label_value=result.raw_forward_return,
            )
        )
    built = ST.build_executable_outcomes(rows)
    outcomes = {outcome.sample_key: outcome for outcome in built["outcomes"]}
    tradability_counts = dict(built["report"]["reason_counts"])
    # 桶分类**只消费** contract 的权威实现，report 层不再复制优先级。
    entry_counts = {
        bucket: built["report"][bucket] for bucket in ST.OUTCOME_BUCKETS
    }

    # ── 可执行覆盖度：provider 存在 ≠ 指标可用 ──
    # executable 基准**可得**必须同时满足三个条件，缺一即"不可得"：
    #   1. 有可信状态源（``bench_exec is not None``，即调用方给了 provider）；
    #   2. **基准侧**真的解析出了动作状态（``bench_coverage["resolved"] > 0``）。
    #      随机基准票池可能一条都解析不出来 —— 例如 provider 只覆盖策略选中的票，
    #      或者归档恰好不覆盖随机样本。此时"选股侧解析成功"完全不能替代它，
    #      两个 population 必须各自证明自己的覆盖；
    #   3. 至少在**一个被评估的决策日**上真的存在可执行基准样本。基准可能在
    #      别的日期有值，而本次评估的日期上一个都没有 —— 那样 executable_excess
    #      一条也减不出来，报告不得声称基准可用。
    # 选股侧覆盖度单独记录，只用于说明"哪些样本具备可执行判定所需的证据"，
    # **不再**作为基准可用性的替代证据。
    state_coverage_available = coverage["resolved"] > 0
    sample_days = {item[1] for item in verified}
    bench_exec_days = sorted(set(bench_exec or {}) & sample_days)
    benchmark_state_coverage_available = bench_coverage.get("resolved", 0) > 0
    executable_benchmark_available = bool(
        bench_exec is not None
        and benchmark_state_coverage_available
        and bench_exec_days
    )
    executable_metrics_available = executable_benchmark_available

    for strategy, day, code, name, result, kline in verified:
        outcome = outcomes[f"{strategy}|{day}|{code}"]
        # 逐策略计数必须在**执行过滤之前**完成：否则"策略选了 2 条、1 条买不进"
        # 会在策略行里显示成 1/1，把被拦的那条藏掉，而全局计数却仍保留它。
        item = by_strategy.setdefault(
            strategy,
            {
                "r": [], "market_excess": [], "executable_excess": [],
                "executable": 0, "verified": 0,
            },
        )
        item["verified"] += 1
        if tradability_mode == ST.MODE_EXECUTABLE and not outcome.executable:
            continue
        item["r"].append(result.raw_forward_return)
        item["executable"] += 1 if outcome.executable else 0
        # 两个 excess **分开**累积，绝不混为一个数字。
        if day in bench:
            item["market_excess"].append(result.raw_forward_return - bench[day])
        # executable_excess **只**用真正可执行的选股样本，且必须与同口径的
        # 可执行基准相减。在 market 视图下 blocked / unproven 的样本会走到这里，
        # 若把它们也减去可执行基准，就重新制造了本 PR 要消除的 population 错配。
        if (
            outcome.executable
            and executable_benchmark_available
            and day in (bench_exec or {})
        ):
            item["executable_excess"].append(
                result.raw_forward_return - bench_exec[day]
            )

    bench_available_text = "可得" if executable_benchmark_available else "不可得"
    lines = [
        f"\n### {label}（T+{horizon} 交易日，entry = 决策后首个交易日收盘；"
        f"标签口径 {LABEL_VERSION}；可成交口径 {tradability_mode}）\n",
        "| 策略 | 已验证样本 | 可执行样本 | 均值收益 | 胜率 | "
        "market_counterfactual_excess | executable_excess | 基准 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    all_r, all_x = [], []
    all_exec_x = []
    for strategy in sorted(by_strategy):
        item = by_strategy[strategy]
        # 该策略可能有 verified 样本、但在当前口径下一条都不 eligible（例如全部被
        # 执行过滤拦下）。这时必须照实打印"已验证 N / 可执行 0"，指标列写 "—"，
        # 而不是让 statistics.mean 在空序列上抛错、也不是把整行藏掉。
        if item["r"]:
            mean_r = statistics.mean(item["r"])
            win = sum(value > 0 for value in item["r"]) / len(item["r"])
            mean_r_text = f"{mean_r * 100:+.2f}%"
            win_text = f"{win * 100:.0f}%"
        else:
            mean_r_text = "—"
            win_text = "—"
        mean_market_x = (
            statistics.mean(item["market_excess"]) if item["market_excess"] else None
        )
        mean_exec_x = (
            statistics.mean(item["executable_excess"])
            if item["executable_excess"] else None
        )
        bench_mean = statistics.mean(bench.values()) if bench else None
        lines.append(
            f"| {strategy} | {item['verified']} | {item['executable']} | "
            f"{mean_r_text} | {win_text} | "
            f"{(mean_market_x * 100) if mean_market_x is not None else float('nan'):+.2f}% | "
            f"{(mean_exec_x * 100) if mean_exec_x is not None else float('nan'):+.2f}% | "
            f"{(bench_mean * 100) if bench_mean is not None else float('nan'):+.2f}% |"
        )
        all_r.extend(item["r"])
        all_x.extend(item["market_excess"])
        all_exec_x.extend(item["executable_excess"])
    lines.append(
        "样本状态：" + "，".join(
            f"{status} {status_counts[status]}" for status in SL.LABEL_STATUSES
        )
        + "（非 verified 的样本不计入任何均值或胜率）"
    )
    lines.append(
        "可成交性："
        + "，".join(f"{name} {count}" for name, count in entry_counts.items())
        + f"（口径 {tradability_mode}；不可执行的样本保留在计数里，不静默丢弃）"
    )
    lines.append(
        "基准口径：market_counterfactual_excess 用『仅 verified』的随机样本均值；"
        "executable_excess 用**同口径可执行**随机样本均值"
        f"（当前：{bench_available_text}）。"
        "两者 population 不同，**不得**相互替代或合并。"
    )
    lines.append(
        "动作状态覆盖度："
        f"选股侧 {coverage['resolved']}/{coverage['required']} 个动作 session 解析出 "
        "PIT 历史状态；"
        f"基准侧 {bench_coverage.get('resolved', 0)}/{bench_coverage.get('required', 0)} 个。"
        "两侧 population 不同，**必须分开统计**：provider 对象存在不等于可执行指标可用，"
        "选股侧解析成功也不等于随机基准侧解析成功。"
    )
    if not executable_benchmark_available:
        lines.append(
            "⚠️ 可执行指标/基准**不可用**：缺少同口径 PIT 历史证券状态源，"
            "或该源在**选股侧或基准侧**未能解析出任何动作 session 状态，"
            "或本次评估的决策日上不存在任何可执行基准样本。"
            "executable_excess 一栏为 —，绝不拿未做 tradability 过滤的 market 基准冒充。"
        )
    if entry_counts["unproven"] and not entry_counts["executable"]:
        lines.append(
            "⚠️ 没有任何样本能证明可执行：历史 ST 名称/涨跌停参考价等证据不足时，"
            "本 contract 一律 fail closed 判为 unproven，而不是默认可成交。"
        )
    summary = {
        "label": label,
        "horizon": horizon,
        "label_version": LABEL_VERSION,
        "tradability_mode": tradability_mode,
        "tradability_counts": tradability_counts,
        "entry_counts": entry_counts,
        "outcome_buckets": dict(entry_counts),
        "n": len(all_r),
        "mean_return": round(statistics.mean(all_r) * 100, 2) if all_r else None,
        # market counterfactual excess（原字段名保留给既有消费者）。
        "mean_excess": round(statistics.mean(all_x) * 100, 2) if all_x else None,
        "market_counterfactual_excess": (
            round(statistics.mean(all_x) * 100, 2) if all_x else None
        ),
        # 可执行 excess：基准不可得时是 None（unavailable），不是估算值。
        "executable_excess": (
            round(statistics.mean(all_exec_x) * 100, 2) if all_exec_x else None
        ),
        # 覆盖度证据：provider 存在 ≠ 指标可用，因此把**两侧**的"要求/解析"
        # 都报出来，调用方可以据此区分"选股侧没证据"与"基准侧没证据"。
        "action_state_coverage": {
            "required": coverage["required"],
            "resolved": coverage["resolved"],
        },
        "benchmark_state_coverage": {
            "required": bench_coverage.get("required", 0),
            "resolved": bench_coverage.get("resolved", 0),
        },
        "action_state_coverage_available": state_coverage_available,
        "benchmark_state_coverage_available": benchmark_state_coverage_available,
        # 实际用于 executable_excess 的可执行基准决策日（可能为空 → 基准不可得）。
        "executable_benchmark_days": bench_exec_days,
        "executable_metrics_available": executable_metrics_available,
        "executable_benchmark_available": executable_benchmark_available,
        "label_status_counts": status_counts,
    }
    if state_source_status is not None:
        summary["security_state_source"] = dict(state_source_status)
    return lines, summary


def _table_columns(conn, table):
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def picks_from_tracking(days):
    db = os.path.join(DATA_DIR, "selection_tracking.db")
    if not os.path.exists(db):
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # ``selection_picks.name`` 是**决策当时**记录的名称 —— 可成交性判定需要它
        # 来判断历史 ST 状态；拿当前快照的名称重写历史是 PIT 违规。
        has_name = "name" in _table_columns(conn, "selection_picks")
        columns = "p.code, p.rank_no" + (", p.name" if has_name else "")
        rows = conn.execute(
            f"""SELECT r.strategy, r.data_asof_date, {columns}
                 FROM selection_picks p JOIN selection_runs r ON r.id = p.run_id
                 WHERE r.data_asof_date >= ? AND r.data_asof_date IS NOT NULL""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    picks = []
    for row in rows:
        name = str(row[4]) if has_name and row[4] is not None else None
        picks.append((str(row[0]), str(row[1])[:10], str(row[2]), name))
    return picks


def picks_from_signals(days):
    """模拟盘已成交信号 → picks，决策日取 ``signal_date``。

    ``paper_signals`` 把**决策**记在 ``signal_date``（收盘后形成信号），把
    **执行**记在 ``intended_date``（下一个交易日）。契约的 entry 已经是
    "决策日之后第一个交易日"，因此这里必须传 ``signal_date``：传
    ``intended_date`` 会把它当成一个收盘后决策，entry 再往后多推一个交易日，
    于是周一决策、周二成交的样本会被报成周三收盘入场，整条收益与 horizon 全错位。
    """
    db = os.path.join(DATA_DIR, "paper_trading.sqlite3")
    if not os.path.exists(db):
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        has_name = "name" in _table_columns(conn, "paper_signals")
        columns = "account_id, signal_date, code" + (", name" if has_name else "")
        rows = conn.execute(
            f"""SELECT {columns} FROM paper_signals
                WHERE status='filled' AND signal_date IS NOT NULL AND signal_date >= ?""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    picks = []
    for row in rows:
        name = str(row[3]) if has_name and row[3] is not None else None
        picks.append((f"filled:{row[0]}", str(row[1])[:10], str(row[2]), name))
    return picks


def production_security_state_fn(*, archive_path=None):
    """生产路径的 action-time 状态源。

    返回 ``(security_state_fn | None, provenance)``。**只**承认显式声明为历史
    归档的 PIT 状态源（:mod:`security_state_point_in_time`）；归档不存在时返回
    ``(None, provenance)``，调用方据此诚实降级 —— 绝不用当前快照或决策日名称
    补位（那正是 PIT 违规）。
    """
    return SS.resolve_security_state_fn(archive_path=archive_path)


def main(security_state_fn=None, *, archive_path=None):
    asof = datetime.date.today().isoformat()
    sessions = market_sessions()
    tracking = picks_from_tracking(WINDOW_DAYS)
    filled = picks_from_signals(WINDOW_DAYS)
    state_fn, state_status = production_security_state_fn(archive_path=archive_path)
    if security_state_fn is not None:
        state_fn = security_state_fn
        state_status = {
            "status": SS.SOURCE_OK,
            "kind": "injected_provider",
            "source": "explicit",
            "rows": None,
        }
    provider_present = state_fn is not None
    summaries = []

    def _sections(picks, horizon, label, view):
        """同一个样本集分别产出 market counterfactual 与 executable 两套指标。

        ``MODE_MARKET`` 只依赖已验证标签，**永远可得**；
        ``MODE_EXECUTABLE`` 依赖同口径 PIT 历史状态源，不可得时它诚实地什么都不给。
        """
        if view == "market_counterfactual":
            lines, summary = evaluate(
                picks, horizon, label, sessions, asof=asof,
                tradability_mode=ST.MODE_MARKET,
                security_state_fn=state_fn, state_source_status=state_status,
            )
        else:
            lines, summary = evaluate(
                picks, horizon, label, sessions, asof=asof,
                tradability_mode=ST.MODE_EXECUTABLE,
                security_state_fn=state_fn, state_source_status=state_status,
            )
        summary["view"] = view
        summaries.append(summary)
        return lines

    # 先算完全部分节，再据此写"口径边界"：executable 是否真的可用由**实际
    # 覆盖度**决定（每个 summary 的 executable_metrics_available），而不是由
    # "有没有 provider 对象"决定。
    report_head = [
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
        "",
        "## 口径边界（诚实声明）",
        "",
        "- market counterfactual（市场反事实）指标：**可用**，基于已验证标签。",
    ]
    report = list(report_head)

    if tracking:
        for view in ("market_counterfactual", "executable"):
            report.append(f"\n#### 每日 topN 候选 · {view}")
            report += _sections(tracking, 1, "每日 topN 候选", view)
            report += _sections(tracking, 3, "每日 topN 候选", view)
    else:
        report.append("\nselection_tracking.db 无样本（selection_picks 为空或库缺失）。")
    if filled:
        for view in ("market_counterfactual", "executable"):
            report.append(f"\n#### 模拟盘实际成交信号 · {view}")
            report += _sections(filled, 1, "模拟盘实际成交信号", view)
    else:
        report.append("\npaper_signals 无 filled 样本。")

    # ── 口径边界：用**实际覆盖度**判定，而不是 provider 是否存在 ──
    # 选股侧与基准侧**分开**汇总：executable 指标可用要求 executable 视图自己
    # 判定为可用（该判定已包含"基准侧解析出状态"与"被评估日期上存在可执行基准
    # 样本"两个条件）。
    exec_summaries = [s for s in summaries if s.get("view") == "executable"]
    required = sum(s.get("action_state_coverage", {}).get("required", 0)
                   for s in exec_summaries)
    resolved = sum(s.get("action_state_coverage", {}).get("resolved", 0)
                   for s in exec_summaries)
    bench_required = sum(s.get("benchmark_state_coverage", {}).get("required", 0)
                         for s in exec_summaries)
    bench_resolved = sum(s.get("benchmark_state_coverage", {}).get("resolved", 0)
                         for s in exec_summaries)
    # executable 指标可用 ⟺ 至少一个 executable 视图真的算出了同口径基准。
    # 这里**不**再用"选股侧 resolved > 0"作为基准可用性的替代证据：随机基准票池
    # 可能一条都解析不出来，那时基准与指标都不可得。
    executable_metrics_available = any(
        s.get("executable_metrics_available") for s in exec_summaries
    )
    if executable_metrics_available:
        boundary = (
            f"- executable 指标：**可用**。PIT 历史证券状态源已接入，"
            f"选股侧动作状态覆盖度 {resolved}/{required}，"
            f"基准侧 {bench_resolved}/{bench_required}。"
        )
    elif provider_present:
        boundary = (
            f"- executable 指标：**不可用**。虽然提供了状态源对象，但同口径的可执行"
            f"基准并不成立（选股侧覆盖度 {resolved}/{required}，"
            f"基准侧 {bench_resolved}/{bench_required}）——可能是随机基准样本一条都"
            "没有解析出动作状态，或本次评估的决策日上不存在任何可执行基准样本。"
            "因此本报告**不**声称已得到 executable 指标；executable_excess 一栏为 —。"
        )
    else:
        boundary = (
            "- executable 指标：**不可用**。**没有**可信的 PIT 历史证券状态源"
            "（可回答 ``security_state_at(code, session)`` 的归档）。因此 entry/exit "
            "时点的 ST 资格无法证明，可执行样本一律 fail closed 判为 ``unproven``；"
            "本报告**不**声称已得到 executable 指标，也**不**用当前快照或决策日名称补齐。"
        )
    boundary_lines = [
        boundary,
        f"- 状态源：`{state_status.get('status')}`"
        + (f"（source={state_status.get('source')}）" if state_status.get("source") else ""),
        f"- 动作状态覆盖度：选股侧 {resolved}/{required}，基准侧 {bench_resolved}/{bench_required}"
        "（两侧 population 不同；provider 对象存在**不等于**可执行指标可用）。",
    ]
    report = report_head + boundary_lines + report[len(report_head):]

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    print(json.dumps({
        "report": REPORT_PATH,
        "security_state_source": state_status,
        "provider_present": provider_present,
        "executable_metrics_available": executable_metrics_available,
        "action_state_coverage": {"required": required, "resolved": resolved},
        "benchmark_state_coverage": {
            "required": bench_required, "resolved": bench_resolved,
        },
        # 兼容字段：含义已收紧为"实际覆盖可用"，不再是"provider 存在"。
        "executable_coverage_available": executable_metrics_available,
        "summaries": summaries,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
