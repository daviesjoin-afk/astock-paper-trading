# -*- coding: utf-8 -*-
"""因子引擎：从K线/快照/财务/舆情计算标准化因子分（Z-score，行业内可选）"""
import numpy as np
import pandas as pd
try:
    import data_fetcher as dfc
except ImportError:  # Allow ``backend.factors`` package-style test imports.
    from . import data_fetcher as dfc
try:
    from financial_point_in_time import financial_visibility
except ImportError:  # Allow ``backend.factors`` package-style test imports.
    from .financial_point_in_time import financial_visibility
try:
    import factor_calibration as FC
except ImportError:  # Allow ``backend.factors`` package-style test imports.
    from . import factor_calibration as FC
try:
    import point_in_time as PIT
except ImportError:  # Allow ``backend.factors`` package-style test imports.
    from . import point_in_time as PIT


def _bar_availability(index):
    """把 K 线索引映射成"该 bar 何时真正可用"的 tz-aware 索引。

    日线 bar 只有日期（生产解析器产出 naive midnight）→ 当日**收盘 15:00**
    （Asia/Shanghai）；索引带显式时刻（分钟 bar、或已带收盘时刻）→ 原样信任。
    无法解释的索引 → NaT，strict 模式下被丢弃（fail closed，绝不猜）。
    """
    stamps = pd.DatetimeIndex(index)
    if stamps.tz is not None:
        naive = stamps.tz_convert(PIT.china_tz()).tz_localize(None)
    else:
        naive = stamps
    date_only = naive == naive.normalize()
    closed = naive.normalize() + pd.Timedelta(
        hours=PIT.CN_MARKET_CLOSE.hour, minutes=PIT.CN_MARKET_CLOSE.minute)
    combined = naive.where(~date_only, closed)
    return combined.tz_localize(
        PIT.china_tz(), ambiguous="NaT", nonexistent="NaT")


def _adjustment_pit_safe(meta, asof_moment):
    """该票的价格序列在 ``asof`` 时点是否**可证明** PIT-safe。

    * 不复权（``adjustment == "none"``）：原始 OHLCV 不会被公司行为回溯重算 → safe。
    * 前复权（``qfq``）：qfq 是按"抓取当时已知的全部公司行为"整段重算的，只有在
      能证明这份数据在 ``asof`` 之前就已经写成时才可认为当时能拿到同样的调整，
      即 manifest 的 ``updated_at <= asof``；否则**无法证明** → 标记 unsafe。
    * 其它/未知 → unsafe。

    本 PR 不实现公司行为数据库：证明不了就标记，绝不把"无法证明"当成"可信"。
    """
    adjustment = str(meta.get("adjustment") or "").strip().lower()
    if adjustment == "none":
        return True
    if adjustment != "qfq":
        return False
    if asof_moment is None:
        return False
    updated = PIT.parse_available_at(meta.get("updated_at"))
    if updated is None:
        return False
    return updated <= asof_moment


def zscore(series, fill_missing=True):
    """横截面标准分；可选择保留缺失值以便调用方使用可信代理回填。"""
    s = pd.Series(series, dtype="float64")
    valid = s.dropna()
    if len(valid) < 5 or valid.std() == 0:
        result = pd.Series(0.0, index=s.index)
        return result if fill_missing else result.where(s.notna())
    z = (s - valid.mean()) / valid.std()
    z = z.clip(-3, 3)
    return z.fillna(0.0) if fill_missing else z

def compute_price_factors(klines: dict, asof=None):
    """基于历史K线的因子：动量/反转/波动/量能。klines: {code: df}

    ``asof`` 是决策时点，两种模式必须严格分层：

    * ``asof is None`` → **live compatibility mode**，行为与改造前逐值一致。
    * ``asof is not None`` → **strict PIT historical mode**：只有"可用时点 <= asof"
      的 bar 参与计算。日线 bar 在**当日收盘 15:00（Asia/Shanghai）**才可用，因此
      ``asof = 当日 10:00`` 看不到当日那根完整 OHLCV；``asof`` 之后的 bar 一律不进入
      任何因子。索引无法解释、或 ``asof`` 无法解析时直接跳过该票，而不是猜。
    """
    strict = PIT.asof_is_strict(asof)
    asof_moment = PIT.parse_asof(asof) if strict else None
    # #7: 预加载 manifest 用于检测不复权数据
    try:
        _manifest = dfc.get_kline_manifest()
    except Exception:
        _manifest = {}
    bars_dropped_future = 0
    bars_unreadable_index = 0
    adjustments_unproven = 0
    rows = []
    for code, df in klines.items():
        if df is None or df.empty:
            continue
        if strict:
            # 无法解析的 asof 不是历史回放的合法依据 → fail closed。
            if asof_moment is None:
                continue
            available = _bar_availability(df.index)
            keep = available.notna() & (available <= asof_moment)
            bars_dropped_future += int((available.notna() & ~keep).sum())
            bars_unreadable_index += int(available.isna().sum())
            d = df.loc[keep]
        else:
            d = df
        if len(d) < 65:
            continue
        c = d["close"]
        try:
            mom20 = c.iloc[-1] / c.iloc[-21] - 1
            mom60 = c.iloc[-1] / c.iloc[-61] - 1
            # 2026-08-28 语义修正：rev5 原与 mom5 完全重复（同为 5 日涨幅），
            # 现改为"距 5 日高点回撤"深度因子（<=0，越接近 0 回踩越浅），
            # 供趋势/轮动策略度量回踩质量；mom5 保持 5 日动量原语义。
            mom5 = c.iloc[-1] / c.iloc[-6] - 1
            rev5 = c.iloc[-1] / c.iloc[-6:].max() - 1
            ret = c.pct_change().iloc[-21:]
            vol20 = ret.std() * np.sqrt(252)
            amt5 = d["amount"].iloc[-5:].mean()
            amt60 = d["amount"].iloc[-60:].mean()
            vol_surge = amt5 / amt60 if amt60 > 0 else np.nan
            # 资金代理因子：近5日 (涨跌方向×成交额) 净流向 / 近60日日均成交额
            sign = np.sign(c.pct_change().iloc[-5:])
            flow_proxy = (sign * d["amount"].iloc[-5:]).sum() / (amt60 * 5) if amt60 > 0 else np.nan
            # RSI(14) 最新值（Wilder 平滑，至少使用完整历史初始化）
            delta = c.diff()
            gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
            if loss.iloc[-1] == 0 and gain.iloc[-1] > 0:
                rsi14 = 100.0
            elif gain.iloc[-1] == 0 and loss.iloc[-1] > 0:
                rsi14 = 0.0
            else:
                rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else np.nan
                rsi14 = round(float(100 - 100 / (1 + rs)), 1) if np.isfinite(rs) else 50.0
            # MACD 信号：DIF 与 DEA 差值
            # P3 精读修复：EMA 是递归序列，必须全程展开再取末值。旧实现
            # 截断到最后 26 根再算——ema12 需约 3×span=36 点收敛、ema26 需
            # 约 78 点，截断后 DIF 系统性偏移且截面内不可比。
            ema12 = c.ewm(span=12, adjust=False).mean().iloc[-1]
            ema26 = c.ewm(span=26, adjust=False).mean().iloc[-1]
            macd_dif = float(ema12 - ema26)
            ma5 = c.rolling(5).mean()
            ma10 = c.rolling(10).mean()
            ma20 = c.rolling(20).mean()
            ma60 = c.rolling(60).mean()
            three_up = bool((c.diff().iloc[-3:] > 0).all())
            boll_mid_breakout = bool(
                c.iloc[-1] > ma20.iloc[-1]
                and c.iloc[-2] <= ma20.iloc[-2]
            )
            above_ma5_5d = bool((c.iloc[-5:] > ma5.iloc[-5:]).all())
            above_ma10_5d = bool((c.iloc[-5:] > ma10.iloc[-5:]).all())
            above_all_ma = bool(
                c.iloc[-1] > max(ma5.iloc[-1], ma10.iloc[-1], ma20.iloc[-1], ma60.iloc[-1])
            )
            weekly = c.resample("W-FRI").last().dropna()
            monthly = c.resample("ME").last().dropna()
            weekly_oversold = bool(
                len(weekly) >= 8
                and weekly.iloc[-1] <= weekly.iloc[-8:].max() * 0.92
            )
            monthly_oversold = bool(
                len(monthly) >= 6
                and monthly.iloc[-1] <= monthly.iloc[-6:].max() * 0.88
            )
            row_data = {"code": code, "mom5": mom5, "mom20": mom20, "mom60": mom60, "rev5": rev5,
                         "vol20": vol20, "vol_surge": vol_surge, "flow_proxy": flow_proxy,
                         "rsi14": rsi14, "macd_dif": macd_dif,
                         "price": float(c.iloc[-1]), "last_date": str(d.index[-1].date()),
                         "three_up": three_up,
                         "boll_mid_breakout": boll_mid_breakout,
                         "above_ma5_5d": above_ma5_5d,
                         "above_ma10_5d": above_ma10_5d,
                         "above_boll_mid": bool(c.iloc[-1] > ma20.iloc[-1]),
                         "above_ma60": bool(c.iloc[-1] > ma60.iloc[-1]),
                         "above_all_ma": above_all_ma,
                         "weekly_oversold": weekly_oversold,
                         "monthly_oversold": monthly_oversold,
                         "ma5": float(ma5.iloc[-1]), "ma10": float(ma10.iloc[-1]),
                         "ma20": float(ma20.iloc[-1]), "ma60": float(ma60.iloc[-1])}
            # 数据源可信度只能进入 evidence-quality；绝不能改变动量/反转
            # 的经济含义。旧逻辑对新浪不复权源直接乘 0.7，会让同一价格
            # 序列仅因来源不同就得到不同 alpha，且无法区分“信号弱”与
            # “证据质量低”。
            meta = _manifest.get(code) or {}
            unadjusted = (
                str(meta.get("source") or "").lower() == "sina"
                and str(meta.get("adjustment") or "").lower() == "none"
            )
            row_data["adjustment_warning"] = bool(unadjusted)
            row_data["price_evidence_quality"] = (
                FC.UNADJUSTED_PRICE_QUALITY if unadjusted else FC.FULL_QUALITY
            )
            # PIT provenance：区分"bar 已按 decision_asof 截断"与
            # "复权口径可证明在 asof 当时成立"。两者都不成立时不得声称 PIT-safe。
            adjustment_safe = _adjustment_pit_safe(meta, asof_moment)
            row_data["bar_pit_safe"] = bool(strict)
            row_data["adjustment_pit_safe"] = bool(adjustment_safe)
            row_data["price_pit_safe"] = bool(strict and adjustment_safe)
            if strict and not adjustment_safe:
                adjustments_unproven += 1
            rows.append(row_data)
        except Exception:
            continue
    frame = pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame()
    frame.attrs["pit"] = PIT.pit_flags(
        mode="strict" if strict else "live",
        decision_asof=PIT.parse_asof(asof).isoformat(timespec="seconds") if asof_moment else None,
        price_pit_safe=bool(strict and rows and adjustments_unproven == 0),
        bars_dropped_future=bars_dropped_future,
        bars_unreadable_index=bars_unreadable_index,
        codes_without_proven_adjustment=adjustments_unproven,
    )
    return frame

def _first_finance_value(record, keys):
    """Return the first explicit non-empty field without inventing metadata."""
    for key in keys:
        value = record.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return None


def _has_finance_value(record, keys):
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str) and (not value.strip() or value.strip().lower() in {"nan", "nat", "none", "null", "-"}):
            continue
        if isinstance(value, float) and np.isnan(value):
            continue
        return True
    return False


def compute_fundamental_factors(snapshot, finance, asof=None):
    """价值/质量因子，并附带保守的财务报告点时元数据。

    ``asof is None`` → **live compatibility mode**：保持现有实时策略口径。
    ``asof is not None`` → **strict PIT historical mode**，两类数据分别收紧：

    * **财务数值**（roe/rev_yoy/profit_yoy/net_profit/annual_net_profit）继续由
      ``financial_point_in_time.financial_visibility`` 判定：报告期**永不**充当
      披露时间，缺披露时间戳 → 不可见。
    * **snapshot 动态字段与行业分类**（pe/pb/mktcap/float_cap/main_net/super_net/
      turnover/pct_today/industry）改为按可用时点判定：快照行必须有可信
      ``observed_at``（或 ``quote_at``/``quote_ts``/``available_at``）且
      ``<= asof`` 才可见；行业还必须满足分类生效区间或"该行本身在 asof 前被观测到"。

    历史模式下不可见即**缺失**：动态数值 → ``NaN``，分类 → ``None``。
    绝不回退到"今天的数据"，也绝不用降低权重的方式假装处理。
    """
    finance_payload = finance if isinstance(finance, dict) else {}
    fin = finance_payload.get("data", {})
    if not isinstance(fin, dict):
        fin = {}
    if asof is None:
        # A future caller may attach the replay cutoff to the finance payload;
        # existing callers leave it absent and remain in live compatibility mode.
        asof = finance_payload.get("asof_date")
    rows = []
    snapshot_hidden = 0
    industry_hidden = 0
    for s in snapshot or []:
        if not isinstance(s, dict) or not s.get("code"):
            continue
        code = s["code"]
        f = fin.get(code, {})
        if not isinstance(f, dict):
            f = {}

        row_asof = asof if asof is not None else f.get("asof_date")
        if row_asof is None:
            row_asof = s.get("asof_date")

        # snapshot 动态字段 + 行业分类的可用性判定（strict 模式下未知 = 不可见）
        snapshot_at_raw, _snapshot_at_iso = PIT.snapshot_available_at(s)
        snapshot_verdict = PIT.is_visible_at(snapshot_at_raw, row_asof)
        classification_verdict = PIT.classification_visibility(s, row_asof)
        snapshot_visible = bool(snapshot_verdict["visible"])
        if PIT.asof_is_strict(row_asof) and not snapshot_visible:
            snapshot_hidden += 1
        if PIT.asof_is_strict(row_asof) and not classification_verdict["visible"]:
            industry_hidden += 1
        latest_meta = financial_visibility(f, row_asof)
        annual_record = {
            "annual_report_period": _first_finance_value(f, ("annual_report_period", "annual_report_date")),
            "annual_report_published_at": _first_finance_value(
                f,
                (
                    "annual_report_published_at",
                    "annual_published_at",
                    "annual_publish_date",
                    "annual_published_date",
                    "annual_notice_date",
                ),
            ),
            "annual_net_profit": f.get("annual_net_profit"),
        }
        annual_meta = financial_visibility(
            annual_record,
            row_asof,
            period_keys=("annual_report_period",),
            published_keys=("annual_report_published_at",),
            value_keys=("annual_net_profit",),
        )
        latest_visible = bool(latest_meta["visible"])
        annual_visible = bool(annual_meta["visible"])
        latest_has_value = _has_finance_value(f, ("roe", "rev_yoy", "profit_yoy", "net_profit", "eps", "bps"))
        annual_has_value = _has_finance_value(f, ("annual_net_profit",))
        # The source describes the profit value available to the strategy.  In
        # live mode a missing publication timestamp remains shadow; in strict
        # history mode hidden values never fall through as if they were visible.
        if latest_visible and latest_has_value:
            profit_source = latest_meta["profit_source"]
        elif annual_visible and annual_has_value:
            profit_source = annual_meta["profit_source"]
        else:
            profit_source = latest_meta["profit_source"]
            if profit_source == "unknown" and annual_meta["profit_source"] != "unknown":
                profit_source = annual_meta["profit_source"]

        def _value(key, visible, _factor=f):
            return _factor.get(key) if visible else np.nan

        pe = s.get("pe") if snapshot_visible else None
        pb = s.get("pb") if snapshot_visible else None

        def _dynamic(key, _visible=snapshot_visible, _row=s):
            """snapshot 动态数值：可见才透传，strict 不可见统一缺失为 NaN。"""
            return _row.get(key) if _visible else np.nan

        rows.append({
            "code": code, "name": s.get("name"),
            "industry": classification_verdict["value"],
            "pe": pe if isinstance(pe, (int, float)) and pe > 0 else np.nan,
            "pb": pb if isinstance(pb, (int, float)) and pb > 0 else np.nan,
            "roe": _value("roe", latest_visible), "rev_yoy": _value("rev_yoy", latest_visible),
            "profit_yoy": _value("profit_yoy", latest_visible),
            "report_date": latest_meta["report_period"],
            "report_period": latest_meta["report_period"],
            "report_published_at": latest_meta["report_published_at"],
            "asof_date": latest_meta["asof_date"],
            "report_age_days": latest_meta["report_age_days"],
            "profit_source": profit_source,
            "net_profit": _value("net_profit", latest_visible),
            "annual_net_profit": _value("annual_net_profit", annual_visible),
            "annual_report_date": annual_meta["report_period"],
            "annual_report_period": annual_meta["report_period"],
            "annual_report_published_at": annual_meta["report_published_at"],
            "annual_report_age_days": annual_meta["report_age_days"],
            "mktcap": _dynamic("mktcap"), "float_cap": _dynamic("float_cap"),
            "main_net": _dynamic("main_net"), "super_net": _dynamic("super_net"),
            "turnover": _dynamic("turnover"), "pct_today": _dynamic("pct"),
            # 按数据类分层的可审计判定（机器只读 reason，不解析自然语言）
            "snapshot_pit_reason": snapshot_verdict["reason"],
            "snapshot_observed_at": snapshot_verdict["available_at"],
            "classification_pit_reason": classification_verdict["reason"],
            "classification_basis": classification_verdict["basis"],
        })
    frame = pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame()
    strict = PIT.asof_is_strict(asof)
    total = len(rows)
    frame.attrs["pit"] = PIT.pit_flags(
        mode="strict" if strict else "live",
        decision_asof=PIT.parse_asof(asof).isoformat(timespec="seconds") if strict and PIT.parse_asof(asof) else None,
        snapshot_pit_safe=bool(strict and total and snapshot_hidden == 0),
        classification_pit_safe=bool(strict and total and industry_hidden == 0),
        financial_pit_safe=bool(strict and total),
        rows_with_hidden_snapshot=snapshot_hidden,
        rows_with_hidden_industry=industry_hidden,
    )
    return frame

def compute_sentiment_factors(universe_codes, asof=None):
    """情绪因子：人气榜排名 + 排名飙升。来源：东方财富股票人气榜（实时）

    ``asof is None`` → **live compatibility mode**，行为不变。
    ``asof is not None`` → **strict PIT historical mode**：人气榜只有实时接口、
    没有任何历史归档或 ``observed_at``，因此历史时点**必然**不可用。此时
    直接返回空 dict，并且**绝不发起实时网络调用**——回放 2024 年时去调今天
    的热榜 API 是最典型的前视泄漏。
    """
    if PIT.asof_is_strict(asof):
        return {}
    hot = dfc.fetch_hot_rank(topn=100)
    scores = {}
    for h in hot:
        code = h["code"]
        if code not in universe_codes:
            continue
        rank_score = (101 - h["rank"]) / 100.0
        surge = min(max(h.get("rank_chg") or 0, 0), 200) / 200.0
        scores[code] = {"hot_rank": h["rank"], "rank_chg": h.get("rank_chg"), 
                        "sentiment": 0.6 * rank_score + 0.4 * surge}
    return scores


def pit_provenance(price=None, fund=None, sentiment=None, asof=None):
    """按数据类汇总 PIT 证据，回答"这次结果的每类数据是否 PIT-safe？"。

    刻意**不用**单个模糊的 ``data_ok=True``：价格截断、复权口径、快照观测时点、
    行业分类生效区间、情绪源归档是五件独立的事，任何一件不成立都不能靠另一件掩盖。
    ``sentiment`` 为历史模式下返回的空 dict 时，``sentiment_pit_safe`` 记为 True
    （"正确地判定为不可用"本身就是 PIT-safe），而不是把它伪装成"有数据且安全"。
    """
    price_flags = dict(getattr(price, "attrs", {}).get("pit") or {})
    fund_flags = dict(getattr(fund, "attrs", {}).get("pit") or {})
    strict = PIT.asof_is_strict(asof)
    out = PIT.pit_flags(
        mode="strict" if strict else "live",
        decision_asof=PIT.parse_asof(asof).isoformat(timespec="seconds")
        if strict and PIT.parse_asof(asof) else None,
    )
    for flag in ("price_pit_safe", "snapshot_pit_safe",
                 "classification_pit_safe", "financial_pit_safe"):
        out[flag] = bool((price_flags if flag == "price_pit_safe" else fund_flags).get(flag))
    out["sentiment_pit_safe"] = bool(sentiment is not None and not sentiment) if strict else False
    out["price"] = price_flags
    out["fundamental"] = fund_flags
    return out

def overseas_risk_gate(history=None):
    """海外风险门控：绿/黄/红。合成规则（可回测）：
    - 道指+纳指近5日累计跌幅均值 < -3% → +2分; < -1.5% → +1分
    - 恒指近5日跌幅 < -3% → +1分
    - 美元指数近20日涨幅 > 2%（人民币贬值压力）→ +1分
    分数 >=3 红灯, >=1.5 黄灯, 否则绿灯"""
    hist = history or dfc.fetch_overseas_history()
    detail, score = [], 0.0
    def _pct(df, n):
        c = df["df"]["close"] if isinstance(df, dict) else df["close"]
        if len(c) < n + 1:
            return None
        return float(c.iloc[-1] / c.iloc[-n-1] - 1) * 100
    us = []
    for k in ["DJIA", "NDX"]:
        if k in hist:
            p = _pct(hist[k], 5)
            if p is not None:
                us.append(p)
                detail.append({"name": hist[k]["name"], "window": "5日", "pct": round(p, 2)})
    if us:
        avg = sum(us) / len(us)
        if avg < -3: score += 2
        elif avg < -1.5: score += 1
    if "HSI" in hist:
        p = _pct(hist["HSI"], 5)
        if p is not None:
            detail.append({"name": "恒生指数", "window": "5日", "pct": round(p, 2)})
            if p < -3: score += 1
    if "USDIDX" in hist:
        p = _pct(hist["USDIDX"], 20)
        if p is not None:
            detail.append({"name": "美元指数", "window": "20日", "pct": round(p, 2)})
            if p > 2: score += 1
    light = "red" if score >= 3 else ("yellow" if score >= 1.5 else "green")
    advice = {"green": "外围环境平稳，策略正常仓位运行",
              "yellow": "外围波动加大，建议仓位降至6成以内，谨慎追高",
              "red": "外围风险显著，建议仓位降至3成以内或观望，等待企稳"}[light]
    return {"light": light, "score": score, "detail": detail, "advice": advice}

def news_keyword_scan(universe_names: dict, include_announcements=False):
    """扫描快讯；风控页可额外接入可追溯的公司公告，交易层默认保持原口径。"""
    news = dfc.fetch_fast_news(50)
    NEG = ["立案", "调查", "违规", "处罚", "减持", "质押", "亏损", "下滑", "退市", "诉讼", "冻结", "爆雷", "商誉减值"]
    POS = ["中标", "回购", "增持", "预增", "涨价", "签约", "获批", "突破", "创新高", "扩产", "分红"]
    hits = []
    for n in news:
        text = n.get("summary") or ""
        for code, name in universe_names.items():
            short = name.replace("A", "").replace(" ", "")
            if len(short) >= 2 and short in text:
                neg = [w for w in NEG if w in text]
                pos = [w for w in POS if w in text]
                tone = -1 if neg else (1 if pos else 0)
                hits.append({"code": code, "name": name, "tone": tone,
                              "keywords": neg + pos, "summary": text[:120],
                              "time": n.get("time"), "source": n.get("source"),
                              "verified": False, "event_type": "快讯关键词"})
    if include_announcements:
        for item in dfc.fetch_company_announcements(universe_names.keys()):
            code = str(item.get("code") or "")
            if code not in universe_names:
                continue
            text = str(item.get("summary") or "")
            neg = [word for word in NEG if word in text]
            pos = [word for word in POS if word in text]
            hits.append({
                "code": code,
                "name": universe_names.get(code) or item.get("name") or code,
                "tone": -1 if neg else (1 if pos else 0),
                "keywords": neg + pos,
                "summary": text[:160],
                "time": item.get("time"),
                "source": item.get("source"),
                "category": item.get("category"),
                "article_id": item.get("article_id"),
                "verified": True,
                "event_type": "公司公告",
            })
    # 同一抓取窗口的同标的同标题只保留一次，避免公告与快讯重复展示。
    deduped, seen = [], set()
    for item in hits:
        key = (item.get("code"), item.get("summary"), item.get("time"))
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped

# ===== 一进二首板判定 =====
def limit_up_threshold(code: str) -> float:
    """按交易板块返回保守涨停识别阈值（已给价格取整留出余量）。"""
    s = str(code)
    if s.startswith(("8", "4", "92")):
        return 0.295
    if s.startswith(("30", "68")):
        return 0.195
    return 0.095

def find_first_board_candidates(klines: dict, today_date=None):
    """从股票池中筛选昨日首板候选。
    klines: {code: DataFrame}（来自 load_cached_kline）
    判定规则：昨日涨≥涨停阈值 且 前日<涨停阈值（非连板）= 首板
    返回 {code: {name, yd_pct, limit_type, db_pct, open_today}}
    """
    import pandas as pd
    candidates = {}
    for code, df in klines.items():
        if df is None or len(df) < 3:
            continue
        try:
            c = df["close"]
            o = df["open"]
            lim = limit_up_threshold(code)
            # 首板判定使用比 limit_up_threshold 更严格的“贴近真实涨停”线。
            # 前复权序列的日收益与真实涨幅一致（除权日除外），真实涨停收盘
            # 的收益距名义涨停只差交易所价格取整（低价股最大约 0.3pp）；
            # 9.5% 宽松线会把 +9.5%~+9.9% 的未封板强势股误判为首板。
            if lim > 0.25:
                board_lim = 0.297
            elif lim > 0.15:
                board_lim = 0.198
            else:
                board_lim = 0.098
            # K线最新日期可能是今天（盘中），需要判断
            # 取最后两根：如果最新>昨天，则昨天=c[-2]/c[-3]，否则昨天=c[-1]/c[-2]
            reference = pd.Timestamp(today_date).date() if today_date is not None else pd.Timestamp.now().date()
            # 缓存索引加载时已经规范为 DatetimeIndex；这里只需要最后一个日期。
            # 对全市场逐票重复转换整段 3 年索引会产生数百万次无意义解析。
            last_date = pd.Timestamp(df.index[-1]).date()
            # 若缓存已有参考日 K 线，参考日前一根才是“昨日”；否则最新一根就是昨日。
            yesterday_pos = -2 if last_date >= reference else -1
            previous_pos = yesterday_pos - 1
            before_previous_pos = yesterday_pos - 2
            if len(c) < abs(before_previous_pos):
                continue
            yd_ret = float(c.iloc[yesterday_pos] / c.iloc[previous_pos] - 1)
            db_ret = float(c.iloc[previous_pos] / c.iloc[before_previous_pos] - 1)
            if last_date >= reference:
                open_today = float(o.iloc[-1])   # 今日开盘
                close_today = float(c.iloc[-1])  # 今日收盘（可能盘后）
            else:
                open_today = None
                close_today = None
            # 判断首板：昨日涨到涨停 且 前日未涨停
            if yd_ret < board_lim:
                continue
            if db_ret is not None and db_ret >= board_lim:
                continue  # 连板，不是首板
            limit_type = "20cm" if lim > 0.15 else "10cm"
            cand = {
                "code": code,
                "yd_pct": round(yd_ret * 100, 2),
                "db_pct": round(db_ret * 100, 2) if db_ret is not None else None,
                "limit_type": limit_type,
            }
            if open_today is not None:
                cand["open_today"] = round(open_today, 2)
                cand["close_today"] = round(close_today, 2) if close_today is not None else None
            candidates[code] = cand
        except Exception:
            continue
    return candidates