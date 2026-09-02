# -*- coding: utf-8 -*-
"""板块联动分析
1) 历史联动：股票池个股按行业分组 → 行业等权日收益序列（近 window 个交易日）→ 相关性矩阵
   → 输出高相关（联动）板块对，说明"A板块涨往往带动B板块"
2) 今日共振：叠加实时板块资金流，若历史高联动的板块对今日同向上涨/同获主力净流入，
   标记为"今日共振中"——联动信号更可信
诚实声明：相关性≠因果，联动关系会随市场风格切换而失效，仅供研究参考。
"""
import time
import pandas as pd

import data_fetcher as dfc
import universe as U

_CACHE = {"data": None, "ts": 0.0, "key": None}


def sector_linkage(window=60, min_corr=0.70, min_members=3, top_pairs=30):
    key = f"{window}_{min_corr}_{min_members}"
    if _CACHE["data"] and _CACHE["key"] == key and time.time() - _CACHE["ts"] < 300:
        return _CACHE["data"]

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
    # （板块资金流接口只返回前100个细分板块，与个股行业字段口径不一致，故不采用）
    flow_map = {}
    try:
        snap = dfc.fetch_market_snapshot()
        agg = {}
        for s in snap:
            ind = s.get("industry")
            if not ind:
                continue
            a = agg.setdefault(ind, {"pcts": [], "main_net": 0.0})
            if isinstance(s.get("pct"), (int, float)):
                a["pcts"].append(s["pct"])
            if isinstance(s.get("amount"), (int, float)) and isinstance(s.get("main_pct"), (int, float)):
                a["main_net"] += s["amount"] * s["main_pct"] / 100.0
        for ind, a in agg.items():
            if a["pcts"]:
                flow_map[ind] = {"pct": round(sum(a["pcts"]) / len(a["pcts"]), 2),
                                 "main_net": a["main_net"]}
    except Exception:
        pass

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
            co_move = (isinstance(pa, (int, float)) and isinstance(pb, (int, float))
                       and pa * pb > 0 and abs(pa) > 0.3 and abs(pb) > 0.3)
            co_fund = (isinstance(ma, (int, float)) and isinstance(mb, (int, float))
                       and ma > 0 and mb > 0)
            pairs.append({
                "a": a, "b": b, "corr": round(float(c), 3),
                "a_pct": pa, "b_pct": pb,
                "a_main_yi": round(ma / 1e8, 2) if isinstance(ma, (int, float)) else None,
                "b_main_yi": round(mb / 1e8, 2) if isinstance(mb, (int, float)) else None,
                "co_move_today": bool(co_move),
                "co_fund_today": bool(co_fund),
                "resonance": bool(co_move and co_fund),
            })
    pairs.sort(key=lambda x: (x["resonance"], x["co_move_today"], x["corr"]), reverse=True)
    pairs = pairs[:top_pairs]

    resonating = [p for p in pairs if p["resonance"]]
    out = {
        "window": window, "min_corr": min_corr,
        "industries_analyzed": len(series),
        "pairs": pairs,
        "resonating_count": len(resonating),
        "summary": (f"分析{len(series)}个行业近{window}日收益相关性，发现{len(pairs)}对高联动板块（corr≥{min_corr}），"
                    f"其中{len(resonating)}对今日正在共振（同涨且同获主力净流入）"),
        "note": "相关性不代表因果，风格切换时联动会失效；共振板块可作为选股时的板块确认信号，不构成投资建议。",
    }
    _CACHE.update({"data": out, "ts": time.time(), "key": key})
    return out
