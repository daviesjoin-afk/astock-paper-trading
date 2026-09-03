# -*- coding: utf-8 -*-
"""面向 A 股日线策略的事件驱动回测内核。

原则：
1. 信号只使用信号日收盘及以前的数据，统一在下一交易日开盘成交。
2. 按板块处理涨跌停，涨停买入和跌停卖出均按“未成交”处理。
3. 显式计入佣金、卖出印花税和开盘滑点。
4. 每个策略在回测中复现自己的候选过滤，避免“实盘一套、回测另一套”。

局限：
- 当前基础库覆盖当前上市股票，但尚无完整退市历史成分，仍存在幸存者偏差。
- 免费数据没有历史财务披露时点，默认禁用基本面回填，避免把最新财务数据泄漏到过去。
- 日线无法还原涨跌停板内的排队、开板和部分成交。
"""
import os

import numpy as np
import pandas as pd

import data_fetcher as dfc
import strategies as S

# Keep backtests aligned with the paper-trading fee model (0.01% commission,
# no minimum commission).
COMMISSION = 0.0001
MIN_COMMISSION = 0.0
STAMP_SELL = 0.0005
SLIPPAGE = 0.001

_matrix_cache = {}
_daily_score_cache = {}
_gate_series_cache = {}


def _pct_change(obj):
    """兼容 pandas 2/3，禁止默认前向填充造成停牌日伪收益。"""
    return obj.pct_change(fill_method=None)


def load_matrices():
    """加载缓存 K 线为日期×代码矩阵。"""
    if "close" in _matrix_cache:
        return _matrix_cache
    fields = {"close": {}, "open": {}, "high": {}, "low": {}, "amount": {}, "volume": {}}
    for filename in os.listdir(dfc.KLINE_DIR):
        if not filename.endswith(".csv") or filename.startswith("BENCH"):
            continue
        code = filename[:-4]
        frame = dfc.load_cached_kline(code)
        if frame is None or len(frame) < 120:
            continue
        for field in fields:
            if field in frame:
                fields[field][code] = pd.to_numeric(frame[field], errors="coerce")

    close = pd.DataFrame(fields["close"]).sort_index()
    _matrix_cache["close"] = close
    for field in ("open", "high", "low", "amount", "volume"):
        _matrix_cache[field] = pd.DataFrame(fields[field]).reindex(
            index=close.index, columns=close.columns
        )
    bench = dfc.load_cached_kline("BENCH_000300")
    _matrix_cache["bench"] = (
        pd.to_numeric(bench["close"], errors="coerce").sort_index()
        if bench is not None and "close" in bench
        else None
    )
    return _matrix_cache


def clear_matrix_cache():
    _matrix_cache.clear()
    _daily_score_cache.clear()
    _gate_series_cache.clear()


def _rsi_matrix(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    loss = (-delta.clip(upper=0)).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.mask((loss == 0) & (gain > 0), 100).mask((gain == 0) & (loss > 0), 0)


def precompute_factors():
    """一次计算全部只依赖历史量价的时序因子。"""
    matrices = load_matrices()
    close, amount = matrices["close"], matrices["amount"]
    ret = _pct_change(close)
    ema12 = close.ewm(span=12, adjust=False, min_periods=26).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    factors = {
        "ret1": ret,
        "mom5": close / close.shift(5) - 1,
        "mom20": close / close.shift(20) - 1,
        "mom60": close / close.shift(60) - 1,
        "vol20": ret.rolling(20, min_periods=20).std() * np.sqrt(252),
        "volsurge": (
            amount.rolling(5, min_periods=5).mean()
            / amount.rolling(60, min_periods=60).mean()
        ),
        "flowproxy": (
            (np.sign(ret) * amount).rolling(5, min_periods=5).sum()
            / (amount.rolling(60, min_periods=60).mean() * 5)
        ),
        "rsi14": _rsi_matrix(close),
        "macd_dif": ema12 - ema26,
        "amount20": amount.rolling(20, min_periods=10).mean(),
    }
    factors["mom_short"] = (
        factors["mom5"] * 0.5 + factors["mom20"] * 0.3 + factors["mom60"] * 0.2
    )
    return matrices, factors


def _xz(row):
    row = pd.to_numeric(row, errors="coerce")
    valid = row.dropna()
    if len(valid) < 5 or not np.isfinite(valid.std()) or valid.std() == 0:
        return pd.Series(0.0, index=row.index)
    return ((row - valid.mean()) / valid.std()).clip(-3, 3).fillna(0.0)


def _limit_ratio(code):
    code = str(code)
    if code.startswith(("8", "4", "92")):
        return 0.295
    if code.startswith(("30", "68")):
        return 0.195
    return 0.095


def _limit_series(columns):
    return pd.Series({_code: _limit_ratio(_code) for _code in columns}, dtype="float64")


def build_gate_series(dates):
    """按日生成海外风险门控；每个日期只读取当日以前的数据。"""
    cache_key = (
        str(dates[0]) if len(dates) else None,
        str(dates[-1]) if len(dates) else None,
        len(dates),
    )
    if cache_key in _gate_series_cache:
        return _gate_series_cache[cache_key]
    history = dfc.fetch_overseas_history()
    lights = pd.Series("green", index=dates, dtype="object")

    def _close(key):
        return history[key]["df"]["close"] if key in history else None

    dj, nd, hs, usd = (_close(k) for k in ("DJIA", "NDX", "HSI", "USDIDX"))
    for date in dates:
        score, us_moves = 0.0, []
        for series in (dj, nd):
            if series is None:
                continue
            known = series[series.index <= date]
            if len(known) >= 6:
                us_moves.append(float(known.iloc[-1] / known.iloc[-6] - 1) * 100)
        if us_moves:
            average = sum(us_moves) / len(us_moves)
            score += 2 if average < -3 else (1 if average < -1.5 else 0)
        if hs is not None:
            known = hs[hs.index <= date]
            if len(known) >= 6 and float(known.iloc[-1] / known.iloc[-6] - 1) * 100 < -3:
                score += 1
        if usd is not None:
            known = usd[usd.index <= date]
            if len(known) >= 21 and float(known.iloc[-1] / known.iloc[-21] - 1) * 100 > 2:
                score += 1
        lights.loc[date] = "red" if score >= 3 else ("yellow" if score >= 1.5 else "green")
    _gate_series_cache[cache_key] = lights
    return lights


GATE_POS = {"green": 1.0, "yellow": 0.6, "red": 0.3}


def _fundamental_frame(fund_static, columns):
    """最新快照仅在显式传入时启用；默认返回全空，杜绝隐式前视。"""
    frame = pd.DataFrame(fund_static or {}).T.reindex(columns)
    for name in ("pe", "pb", "roe", "profit_yoy", "rev_yoy", "float_cap"):
        if name not in frame:
            frame[name] = np.nan
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    return frame


def _score_on_date(strategy_id, date, matrices, factors, fundamentals, use_fundamentals):
    """计算单个信号日的横截面分数，并应用策略专属候选规则。"""
    close = matrices["close"]
    px = close.loc[date]
    columns = close.columns
    zf = {
        "mom_short": (
            _xz(factors["mom5"].loc[date]) * 0.5
            + _xz(factors["mom20"].loc[date]) * 0.3
            + _xz(factors["mom60"].loc[date]) * 0.2
        ),
        "volsurge": _xz(factors["volsurge"].loc[date]),
        "flow": _xz(factors["flowproxy"].loc[date]),
        "sentiment": _xz(
            factors["volsurge"].loc[date] * 0.5 + factors["mom20"].loc[date]
        ),
        "rsi": _xz(factors["rsi14"].loc[date] - 50) * -1,
    }
    if use_fundamentals:
        last_close = close.ffill().iloc[-1]
        pe_at_date = fundamentals["pe"] * px / last_close
        pb_at_date = fundamentals["pb"] * px / last_close
        zf["value"] = (
            _xz(pe_at_date.where(pe_at_date > 0)) * -1
            + _xz(pb_at_date.where(pb_at_date > 0)) * -1
        ) / 2
        zf["quality"] = (
            _xz(fundamentals["roe"])
            + _xz(fundamentals["profit_yoy"]) * 0.5
            + _xz(fundamentals["rev_yoy"]) * 0.5
        ) / 2

    # 只对当前模式可获得的因子重新归一化权重，避免缺失因子稀释分数。
    available_weights = {
        name: weight for name, weight in S.WEIGHTS[strategy_id].items() if name in zf
    }
    weight_sum = sum(available_weights.values()) or 1.0
    score = pd.Series(0.0, index=columns)
    for name, weight in available_weights.items():
        score += zf[name] * (weight / weight_sum)

    ret1 = factors["ret1"].loc[date]
    mom20 = factors["mom20"].loc[date]
    volsurge = factors["volsurge"].loc[date]

    if strategy_id == "one_to_two":
        location = close.index.get_loc(date)
        if location < 1:
            return score.iloc[0:0]
        previous_date = close.index[location - 1]
        previous_ret = factors["ret1"].loc[previous_date]
        limits = _limit_series(columns)
        # 信号日是首板日，下一交易日开盘参与；前一日接近涨停则视为连板并排除。
        eligible = (ret1 >= limits) & (previous_ret < limits * 0.85)
        score = score.where(eligible)
    elif strategy_id == "bottom_reversal":
        # 与实时策略一致：缩量或仍在快速下跌会降权，资金转正会加分。
        score = score - (volsurge < 1.0).astype(float) * 0.30
        score = score + (zf["flow"] > 0.5).astype(float) * 0.20
        score = score - (mom20 < -0.05).astype(float) * 0.20
    elif strategy_id == "sentiment_pioneer":
        # 历史人气榜不可得；用量价情绪代理，并在返回结果中明确标注。
        score = score - (zf["sentiment"] < -0.5).astype(float) * 0.20

    liquid = (
        px.notna()
        & factors["amount20"].loc[date].gt(0)
    )
    return score.where(liquid).dropna().sort_values(ascending=False)


def _execution_price(side, open_price, low_price, high_price):
    if side == "buy":
        slipped = open_price * (1 + SLIPPAGE)
        return min(slipped, high_price) if np.isfinite(high_price) else slipped
    slipped = open_price * (1 - SLIPPAGE)
    return max(slipped, low_price) if np.isfinite(low_price) else slipped


def run_backtest(
    strategy_id,
    topn=10,
    rebalance=10,
    use_gate=True,
    start=None,
    end=None,
    fund_static=None,
    prepared=None,
    use_latest_fundamentals=False,
):
    """运行回测。

    ``use_latest_fundamentals`` 默认关闭。只有显式传入历史近似数据并接受前视偏差时才开启。
    """
    if strategy_id not in S.STRATEGIES:
        return {"error": f"未知策略: {strategy_id}"}
    if topn < 1 or topn > 100 or rebalance < 1 or rebalance > 120:
        return {"error": "topn 必须为 1-100，rebalance 必须为 1-120"}

    matrices, factors = prepared or precompute_factors()
    close, open_ = matrices["close"], matrices["open"]
    valuation_close = close.ffill()
    dates = close.index
    if start:
        dates = dates[dates >= pd.Timestamp(start)]
    if end:
        dates = dates[dates <= pd.Timestamp(end)]
    if start is None:
        dates = dates[60:]
    if len(dates) < 40:
        return {"error": "数据不足，请先初始化至少 100 个交易日的历史数据"}

    fundamentals = _fundamental_frame(fund_static, close.columns)
    fundamentals_enabled = bool(use_latest_fundamentals and fund_static)
    gate_series = build_gate_series(dates) if use_gate else None
    rebalance_dates = set(dates[::rebalance])
    limits = _limit_series(close.columns)

    cash = 1.0
    positions = {}
    equity_rows = []
    gate_log = []
    holdings_history = []
    trade_log = []
    rejected = {"limit_up": 0, "limit_down": 0, "suspended": 0}
    turnover_value = 0.0
    total_cost = 0.0

    for index, date in enumerate(dates):
        prices = valuation_close.loc[date]
        position_value = sum(
            shares * prices.get(code, np.nan)
            for code, shares in positions.items()
            if np.isfinite(prices.get(code, np.nan))
        )
        equity_rows.append({"date": date, "equity": cash + position_value})
        if date not in rebalance_dates or index + 1 >= len(dates):
            continue

        score_key = (id(close), strategy_id, pd.Timestamp(date))
        scores = None if fundamentals_enabled else _daily_score_cache.get(score_key)
        if scores is None:
            scores = _score_on_date(
                strategy_id,
                date,
                matrices,
                factors,
                fundamentals,
                fundamentals_enabled,
            )
            if not fundamentals_enabled:
                _daily_score_cache[score_key] = scores
        targets = list(scores.head(topn).index)
        light = gate_series.loc[date] if use_gate else "green"
        position_scale = GATE_POS.get(light, 1.0)
        if use_gate:
            gate_log.append({"date": str(date.date()), "light": light})

        next_date = dates[index + 1]
        next_open = open_.loc[next_date]
        next_low = matrices["low"].loc[next_date]
        next_high = matrices["high"].loc[next_date]
        prior_close = close.loc[date]
        execution_marks = next_open.where(next_open.notna(), valuation_close.loc[date])
        execution_equity = cash + sum(
            shares * execution_marks.get(code, np.nan)
            for code, shares in positions.items()
            if np.isfinite(execution_marks.get(code, np.nan))
        )
        desired_each = position_scale * execution_equity / len(targets) if targets else 0.0

        # 先卖出移除标的和目标仓位的超配部分。
        for code in list(positions):
            op = next_open.get(code, np.nan)
            prev = prior_close.get(code, np.nan)
            if not np.isfinite(op) or not np.isfinite(prev) or prev <= 0:
                rejected["suspended"] += 1
                continue
            current_value = positions[code] * op
            desired_value = desired_each if code in targets else 0.0
            if current_value <= desired_value * 1.001:
                continue
            if op / prev - 1 <= -limits[code]:
                rejected["limit_down"] += 1
                continue
            sell_value_at_open = current_value - desired_value
            shares = min(positions[code], sell_value_at_open / op)
            price = _execution_price("sell", op, next_low.get(code, np.nan), next_high.get(code, np.nan))
            gross = shares * price
            # 佣金按万一免五模拟，与 paper_trading 保持一致。
            # 小额交易的净收益，使优化器偏向过度换手的参数。
            fees = max(MIN_COMMISSION, gross * COMMISSION) + gross * STAMP_SELL
            positions[code] -= shares
            if positions[code] <= 1e-12:
                del positions[code]
            cash += gross - fees
            turnover_value += gross
            total_cost += fees + shares * max(op - price, 0)
            trade_log.append(
                {
                    "signal_date": str(date.date()),
                    "execution_date": str(next_date.date()),
                    "code": code,
                    "side": "sell",
                    "price": round(float(price), 4),
                    "value": round(float(gross), 6),
                }
            )

        # 再按等权目标补仓；现金不足时按剩余缺口比例缩放。
        needs = []
        for code in targets:
            op = next_open.get(code, np.nan)
            prev = prior_close.get(code, np.nan)
            if not np.isfinite(op) or not np.isfinite(prev) or op <= 0 or prev <= 0:
                rejected["suspended"] += 1
                continue
            if op / prev - 1 >= limits[code]:
                rejected["limit_up"] += 1
                continue
            current_value = positions.get(code, 0.0) * op
            deficit = max(desired_each - current_value, 0.0)
            if deficit > execution_equity * 0.001:
                needs.append((code, op, deficit))
        required_cash = sum(
            deficit * (1 + COMMISSION) + MIN_COMMISSION for _, _, deficit in needs
        )
        scale = min(1.0, cash / required_cash) if required_cash > 0 else 0.0
        for code, op, deficit in needs:
            budget = deficit * scale
            price = _execution_price("buy", op, next_low.get(code, np.nan), next_high.get(code, np.nan))
            shares = max(budget - MIN_COMMISSION, 0.0) / (price * (1 + COMMISSION))
            gross = shares * price
            fees = max(MIN_COMMISSION, gross * COMMISSION)
            if gross + fees > cash or shares <= 0:
                continue
            positions[code] = positions.get(code, 0.0) + shares
            cash -= gross + fees
            turnover_value += gross
            total_cost += fees + shares * max(price - op, 0)
            trade_log.append(
                {
                    "signal_date": str(date.date()),
                    "execution_date": str(next_date.date()),
                    "code": code,
                    "side": "buy",
                    "price": round(float(price), 4),
                    "value": round(float(gross), 6),
                }
            )
        holdings_history.append(
            {
                "signal_date": str(date.date()),
                "execution_date": str(next_date.date()),
                "target": targets,
                "gate": light,
                "position_scale": position_scale,
            }
        )

    equity = pd.DataFrame(equity_rows).set_index("date")["equity"]
    metrics = compute_metrics(equity)
    benchmark_curve = None
    benchmark = matrices["bench"]
    if benchmark is not None:
        aligned = benchmark.reindex(equity.index).ffill().bfill()
        if len(aligned) and np.isfinite(aligned.iloc[0]) and aligned.iloc[0] > 0:
            aligned = aligned / aligned.iloc[0]
            benchmark_curve = [round(float(value), 4) for value in aligned]
            metrics["benchmark_return"] = round(float(aligned.iloc[-1] - 1) * 100, 2)
            metrics["excess_return"] = round(
                metrics["total_return"] - metrics["benchmark_return"], 2
            )

    return {
        "strategy": strategy_id,
        "strategy_name": S.STRATEGIES[strategy_id]["name"],
        "params": {
            "topn": topn,
            "rebalance": rebalance,
            "use_gate": use_gate,
            "fundamentals_mode": (
                "latest_snapshot_approx" if fundamentals_enabled else "disabled_no_pit_data"
            ),
            "start": str(equity.index[0].date()),
            "end": str(equity.index[-1].date()),
        },
        "metrics": metrics,
        "dates": [str(date.date()) for date in equity.index],
        "equity": [round(float(value), 4) for value in equity],
        "benchmark": benchmark_curve,
        "gate_log": gate_log[-60:] if use_gate else [],
        "recent_holdings": holdings_history[-3:],
        "trades": len(trade_log),
        "recent_trades": trade_log[-100:],
        "execution": {
            "rejected_orders": rejected,
            "turnover_multiple": round(turnover_value, 3),
            "estimated_cost_pct_of_initial": round(total_cost * 100, 3),
            "commission": COMMISSION,
            "stamp_duty_sell": STAMP_SELL,
            "slippage": SLIPPAGE,
        },
        "data_quality": {
            "signal_timing": "信号日收盘后生成，下一交易日开盘成交",
            "strategy_filter_reproduced": True,
            "point_in_time_fundamentals": fundamentals_enabled is False,
            "survivorship_bias": "current_listed_universe_without_delisted_history",
            "sentiment_proxy": strategy_id == "sentiment_pioneer",
        },
        "disclaimer": (
            "回测默认不使用无法按历史披露时点还原的财务因子；基础库覆盖当前沪深北上市股票，"
            "但缺少完整退市历史成分，仍有幸存者偏差。已计入佣金、卖出印花税和开盘滑点，"
            "日线无法模拟排队与部分成交。"
            "历史表现不代表未来收益，不构成投资建议。"
        ),
    }


def compute_metrics(curve):
    curve = pd.Series(curve, dtype="float64").dropna()
    if len(curve) < 2 or curve.iloc[0] <= 0:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "annual_volatility": 0.0,
            "daily_win_rate": 0.0,
        }
    returns = _pct_change(curve).dropna()
    total = float(curve.iloc[-1] / curve.iloc[0] - 1)
    calendar_days = max((curve.index[-1] - curve.index[0]).days, 1)
    annual = (1 + total) ** (365.25 / calendar_days) - 1 if total > -1 else -1.0
    drawdown = curve / curve.cummax() - 1
    max_drawdown = float(drawdown.min())
    volatility = float(returns.std() * np.sqrt(252)) if len(returns) else 0.0
    sharpe = (
        float(returns.mean() / returns.std() * np.sqrt(252))
        if returns.std() > 0
        else 0.0
    )
    downside = returns[returns < 0]
    sortino = (
        float(returns.mean() / downside.std() * np.sqrt(252))
        if len(downside) > 1 and downside.std() > 0
        else 0.0
    )
    calmar = annual / abs(max_drawdown) if max_drawdown < 0 else 0.0
    active = returns[returns != 0]
    win_rate = float((active > 0).mean()) if len(active) else 0.0
    return {
        "total_return": round(total * 100, 2),
        "annual_return": round(annual * 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "annual_volatility": round(volatility * 100, 2),
        "daily_win_rate": round(win_rate * 100, 1),
        "active_days": int(len(active)),
    }
