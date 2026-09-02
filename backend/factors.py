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
    asof: 截止日期（回测用，防前视偏差——只用 asof 及之前的数据）"""
    # #7: 预加载 manifest 用于检测不复权数据
    try:
        _manifest = dfc.get_kline_manifest()
    except Exception:
        _manifest = {}
    rows = []
    for code, df in klines.items():
        if df is None or df.empty:
            continue
        d = df if asof is None else df[df.index <= asof]
        if len(d) < 65:
            continue
        c = d["close"]
        try:
            mom20 = c.iloc[-1] / c.iloc[-21] - 1
            mom60 = c.iloc[-1] / c.iloc[-61] - 1
            rev5 = c.iloc[-1] / c.iloc[-6] - 1
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
            ema12 = c.iloc[-26:].ewm(span=12, adjust=False).mean().iloc[-1]
            ema26 = c.iloc[-26:].ewm(span=26, adjust=False).mean().iloc[-1]
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
            row_data = {"code": code, "mom5": rev5, "mom20": mom20, "mom60": mom60, "rev5": rev5,
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
            # #7: 新浪不复权数据的技术指标偏差衰减
            meta = _manifest.get(code) or {}
            if str(meta.get("source") or "").lower() == "sina" and str(meta.get("adjustment") or "").lower() == "none":
                row_data["adjustment_warning"] = True
                for _fcol in ("mom5", "mom20", "mom60", "rev5"):
                    if isinstance(row_data.get(_fcol), (int, float)):
                        row_data[_fcol] = row_data[_fcol] * 0.7
            else:
                row_data["adjustment_warning"] = False
            rows.append(row_data)
        except Exception:
            continue
    return pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame()

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

    ``asof`` 是可选的历史回放截止日；不传时保持现有实时策略口径。
    当前财务接口仅返回报告期（``report_date``），没有披露时间，因此
    未知披露时间的实时值标记为 ``shadow``，历史回放会隐藏该值。
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

        def _value(key, visible):
            return f.get(key) if visible else np.nan

        pe = s.get("pe")
        pb = s.get("pb")
        rows.append({
            "code": code, "name": s.get("name"), "industry": s.get("industry"),
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
            "mktcap": s.get("mktcap"), "float_cap": s.get("float_cap"),
            "main_net": s.get("main_net"), "super_net": s.get("super_net"),
            "turnover": s.get("turnover"), "pct_today": s.get("pct"),
        })
    return pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame()

def compute_sentiment_factors(universe_codes):
    """情绪因子：人气榜排名 + 排名飙升。来源：东方财富股票人气榜（实时）"""
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
            # 判断首板：昨日涨到涨停 且 前日未涨��
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
