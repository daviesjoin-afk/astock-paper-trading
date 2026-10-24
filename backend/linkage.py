# -*- coding: utf-8 -*-
"""板块联动分析
1) 历史联动：股票池个股按行业分组 → 行业等权日收益序列（近 window 个交易日）→ 相关性矩阵
   → 输出高相关（联动）板块对，说明"A板块涨往往带动B板块"
2) 今日共振：叠加实时板块资金流，若历史高联动的板块对今日同向上涨/同获主力净流入，
   标记为"今日共振中"——联动信号更可信。**"今天"必须被证明**：证据过期/跨日时
   如实标为不可判定，而不是把最后一份已知快照当今天的行情。
诚实声明：相关性≠因果，联动关系会随市场风格切换而失效，仅供研究参考。
"""
import datetime as dt
import os
import time

import pandas as pd

import data_fetcher as dfc
import market_data_contract as MDC
import universe as U

CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
_CACHE = {"data": None, "ts": 0.0, "key": None}
CLOSE_TIME = dt.time(15, 0)


def _policy_for(now):
    """适用时间规则：收盘后谈当日结果用收盘口径，盘中用实时口径。"""
    return (MDC.CLOSE_SNAPSHOT_POLICY if now.time() >= CLOSE_TIME
            else MDC.LIVE_MARKET_POLICY)


def _evidence_clock_key(now):
    """证据判定的**输入指纹**（一次 os.stat，不读快照正文）。

    放进结果缓存键，使"快照被改写 / 换日 / 跨过收盘时刻"立刻失效，
    而不必为了判断缓存是否有效去读一遍几 MB 的行情快照。
    """
    try:
        stat = os.stat(dfc.MARKET_SNAPSHOT_FULL_CACHE_PATH)
        file_sig = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        file_sig = None
    return (file_sig, _policy_for(now).name, now.date().isoformat())


def _today_evidence(now, policy):
    """当日共振的证据状态：``(evidence, rows)``。

    历史相关性只要历史 K 线就能算，永远可以展示；但 ``co_move_today`` /
    ``resonance`` 是**当日**断言。``read_snapshot`` 刻意保留最后一份已知事实
    供展示，所以"返回行"不等于"今天有效" —— 必须过适用时间规则与业务日校验。
    """
    import market_data_service as MDSvc

    today = now.date().isoformat()
    reading = MDSvc.read_snapshot(policy=policy, now=now, asof_day=today)
    projection = reading.projection()
    rows = [dict(row) for row in reading.rows()] if reading.available else []
    # 行时间戳优先，整份快照的观测时点兜底（与 ``close_snapshot_rows`` 同口径）。
    fallback = projection["observed_at"]
    quote_days = {
        str((row.get("quote_at") or row.get("observed_at") or fallback) or "")[:10]
        for row in rows
    }
    quote_days.discard("")
    usable = bool(rows) and reading.freshness == MDC.FRESHNESS_FRESH and quote_days == {today}
    evidence = {
        "status": projection["status"],
        "freshness": projection["freshness"],
        "policy": projection["policy"],
        "observed_at": projection["observed_at"],
        "age_seconds": projection["age_seconds"],
        "reason": projection["reason"],
        "quote_date": max(quote_days) if quote_days else None,
        "business_date": today,
        "usable": usable,
        "evaluated_at": now.isoformat(timespec="seconds"),
    }
    return evidence, (rows if usable else [])


def _unavailable_evidence(now, reason):
    return {
        "status": "unavailable",
        "freshness": "unknown",
        "policy": _policy_for(now).name,
        "observed_at": None,
        "age_seconds": None,
        "reason": reason,
        "quote_date": None,
        "business_date": now.date().isoformat(),
        "usable": False,
        "evaluated_at": now.isoformat(timespec="seconds"),
    }


def _flow_map(rows):
    """按行业聚合当日涨跌与主力净流入（口径与个股 industry 字段一致）。"""
    agg = {}
    for row in rows:
        industry = row.get("industry")
        if not industry:
            continue
        item = agg.setdefault(industry, {"pcts": [], "main_net": 0.0})
        if isinstance(row.get("pct"), (int, float)):
            item["pcts"].append(row["pct"])
        if isinstance(row.get("amount"), (int, float)) and isinstance(row.get("main_pct"), (int, float)):
            item["main_net"] += row["amount"] * row["main_pct"] / 100.0
    flow = {}
    for industry, item in agg.items():
        if item["pcts"]:
            flow[industry] = {
                "pct": round(sum(item["pcts"]) / len(item["pcts"]), 2),
                "main_net": item["main_net"],
            }
    return flow


def sector_linkage(window=60, min_corr=0.70, min_members=3, top_pairs=30):
    now = dt.datetime.now(CHINA_TZ)
    key = (window, min_corr, min_members, _evidence_clock_key(now))
    if _CACHE["data"] and _CACHE["key"] == key and time.time() - _CACHE["ts"] < 300:
        return dict(_CACHE["data"])
    try:
        evidence, snap = _today_evidence(now, _policy_for(now))
    except Exception as exc:  # 证据读取失败 → 当日不可判定，但历史相关性仍可展示
        evidence, snap = _unavailable_evidence(now, f"{type(exc).__name__}"), []

    uni = U.load_universe()
    if not uni:
        return {"error": "股票池未构建，请先初始化数据"}
    # 行业成员分组
    ind_members = {}
    for u in uni:
        ind = u.get("industry")
        if ind:
            ind_members.setdefault(ind, []).append(u["code"])

    # 行业等权日收益序列
    series = {}
    for ind, codes in ind_members.items():
        if len(codes) < min_members:
            continue
        rets = []
        for c in codes:
            df = dfc.load_cached_kline(c)
            if df is None or len(df) < window + 5:
                continue
            rets.append(df["close"].pct_change().iloc[-(window + 1):])
        if len(rets) >= min_members:
            merged = pd.concat(rets, axis=1)
            series[ind] = merged.mean(axis=1)
    if len(series) < 2:
        return {"error": "可用行业数据不足，无法计算联动"}

    mat = pd.DataFrame(series).dropna(how="all")
    corr = mat.corr()

    # 今日共振判定：直接从全市场快照按行业(f100)聚合，与联动序列口径一致
    # （板块资金流接口只返回前100个细分板块，与个股行业字段口径不一致，故不采用）。
    # ``snap`` 只有在证据通过适用时间规则与业务日校验时才非空。
    flow_map = _flow_map(snap)

    pairs = []
    inds = list(corr.columns)
    for i in range(len(inds)):
        for j in range(i + 1, len(inds)):
            c = corr.iloc[i, j]
            if pd.isna(c) or c < min_corr:
                continue
            a, b = inds[i], inds[j]
            fa, fb = flow_map.get(a, {}), flow_map.get(b, {})
            pa, pb = fa.get("pct"), fb.get("pct")
            ma, mb = fa.get("main_net"), fb.get("main_net")
            if evidence["usable"]:
                co_move = (isinstance(pa, (int, float)) and isinstance(pb, (int, float))
                           and pa * pb > 0 and abs(pa) > 0.3 and abs(pb) > 0.3)
                co_fund = (isinstance(ma, (int, float)) and isinstance(mb, (int, float))
                           and ma > 0 and mb > 0)
                resonance = bool(co_move and co_fund)
            else:
                # 证据不足 → 明确"不可判定"（None），绝不输出 False 冒充"今日不联动"。
                co_move = co_fund = resonance = None
            pairs.append({
                "a": a, "b": b, "corr": round(float(c), 3),
                "a_pct": pa, "b_pct": pb,
                "a_main_yi": round(ma / 1e8, 2) if isinstance(ma, (int, float)) else None,
                "b_main_yi": round(mb / 1e8, 2) if isinstance(mb, (int, float)) else None,
                "co_move_today": None if co_move is None else bool(co_move),
                "co_fund_today": None if co_fund is None else bool(co_fund),
                "resonance": resonance,
            })
    pairs.sort(
        key=lambda x: (bool(x["resonance"]), bool(x["co_move_today"]), x["corr"]),
        reverse=True,
    )
    pairs = pairs[:top_pairs]

    resonating = [p for p in pairs if p["resonance"]]
    if evidence["usable"]:
        today_sentence = f"其中{len(resonating)}对今日正在共振（同涨且同获主力净流入）"
        resonating_count = len(resonating)
    else:
        reason = evidence["reason"] or evidence["status"]
        today_sentence = (
            f"今日行情证据不可用（{reason}；行情日 {evidence['quote_date'] or '未知'}，"
            f"业务日 {evidence['business_date']}），当日共振不可判定"
        )
        resonating_count = None
    out = {
        "window": window, "min_corr": min_corr,
        "industries_analyzed": len(series),
        "pairs": pairs,
        "resonating_count": resonating_count,
        "today_evidence": evidence,
        "summary": (f"分析{len(series)}个行业近{window}日收益相关性，发现{len(pairs)}对高联动板块（corr≥{min_corr}），"
                    f"{today_sentence}"),
        "note": "相关性不代表因果，风格切换时联动会失效；共振板块可作为选股时的板块确认信号，不构成投资建议。",
    }
    _CACHE.update({"data": out, "ts": time.time(), "key": key})
    return dict(out)
