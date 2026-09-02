# -*- coding: utf-8 -*-
"""持仓跟踪与卖出纪律模块
- 跟踪池持久化：data_cache/tracking.json
- 个股卖出规则（任一触发即提示）：
  1) 跟踪止损：自加入后最高价回撤 > trail_stop_pct（默认 10%）
  2) 硬止损：相对成本价亏损 > hard_stop_pct（默认 8%）
  3) 时间止损：持有 > max_hold_days（默认 20 个交易日）且收益 < 5%
  4) 因子反转：20日动量转负 且 当日主力净流入占比为负
- 组合级风控：
  * 回撤熔断：组合净值自峰值回撤 >5% 建议减半仓，>10% 建议清仓观望
  * 行业集中度：单行业持仓只数占比 >40% 提示分散
  * 波动率仓位建议：按 20 日波动率倒数归一化给出建议权重
诚实声明：所有信号为规则化提示，仅供研究参考，不构成投资建议。
"""
import os, json, threading
from datetime import datetime

import pandas as pd

import data_fetcher as dfc
import strategies as S

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACK_PATH = os.path.join(BASE, "data_cache", "tracking.json")
_LOCK = threading.Lock()

DEFAULT_RULES = {
    "trail_stop_pct": 10.0,   # 跟踪止损：峰值回撤%
    "hard_stop_pct": 8.0,     # 硬止损：成本亏损%
    "max_hold_days": 20,      # 时间止损：交易日
    "time_stop_min_ret": 5.0, # 时间止损豁免收益%
}


def _load():
    for path in (TRACK_PATH, TRACK_PATH + ".bak"):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "positions" in d:
                    d.setdefault("history", [])
                    d.setdefault("rules", dict(DEFAULT_RULES))
                    return d
            except Exception:
                continue
    return {"positions": [], "history": [], "rules": dict(DEFAULT_RULES)}


def _save(data):
    os.makedirs(os.path.dirname(TRACK_PATH), exist_ok=True)
    # 先备份现有非空数据，防止异常写入清空跟踪池
    try:
        if os.path.exists(TRACK_PATH):
            with open(TRACK_PATH, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("positions"):
                import shutil
                shutil.copyfile(TRACK_PATH, TRACK_PATH + ".bak")
    except Exception:
        pass
    tmp = TRACK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, TRACK_PATH)


def add_position(code, name=None, cost=None, strategy=None, note=None):
    """加入跟踪池。cost 缺省用最新价。"""
    with _LOCK:
        data = _load()
        if any(p["code"] == code for p in data["positions"]):
            return {"ok": False, "message": f"{code} 已在跟踪池中"}
        snap = dfc.fetch_realtime_for_codes([code])
        row = snap[0] if snap else {}
        price = row.get("price")
        entry = {
            "code": code,
            "name": name or row.get("name") or code,
            "industry": row.get("industry"),
            "cost": float(cost) if cost is not None else (float(price) if isinstance(price, (int, float)) else None),
            "added_at": datetime.now().strftime("%Y-%m-%d"),
            "peak_price": float(price) if isinstance(price, (int, float)) else None,
            "strategy": strategy,
            "note": note,
        }
        data["positions"].append(entry)
        _save(data)
        return {"ok": True, "position": entry}


def remove_position(code):
    snap = dfc.fetch_realtime_for_codes([code])
    exit_price = snap[0].get("price") if snap else None
    with _LOCK:
        data = _load()
        before = len(data["positions"])
        removed = next((p for p in data["positions"] if p["code"] == code), None)
        data["positions"] = [p for p in data["positions"] if p["code"] != code]
        archived = None
        if removed:
            cost = removed.get("cost")
            realized = (
                round((exit_price / cost - 1) * 100, 2)
                if isinstance(exit_price, (int, float)) and cost
                else None
            )
            archived = {
                **removed,
                "removed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "exit_price": exit_price,
                "realized_ret_pct": realized,
            }
            data.setdefault("history", []).append(archived)
            data["history"] = data["history"][-1000:]
        _save(data)
        return {"ok": len(data["positions"]) < before, "closed_trade": archived}


def list_positions():
    return _load()


def _trade_days_since(added_at, kline):
    """用K线索引数近似持有交易日数"""
    try:
        ts = pd.Timestamp(added_at)
        return int((kline.index > ts).sum())
    except Exception:
        return None


def check_positions(rules=None):
    """核心：对跟踪池逐只检查卖出规则 + 组合级风控。盘后/盘中均可调用。"""
    with _LOCK:
        data = _load()
    positions = data["positions"]
    r = dict(DEFAULT_RULES)
    r.update(data.get("rules") or {})
    if rules:
        r.update(rules)
    if not positions:
        return {"positions": [], "portfolio": None, "rules": r,
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "disclaimer": "规则化信号仅供研究参考，不构成投资建议。"}

    codes = [p["code"] for p in positions]
    snap = {s["code"]: s for s in dfc.fetch_realtime_for_codes(codes)}

    results, rets, inds = [], [], {}
    peak_dirty = False
    for p in positions:
        code = p["code"]
        s = snap.get(code, {})
        price = s.get("price") if isinstance(s.get("price"), (int, float)) else None
        cost = p.get("cost")
        # 更新峰值
        if price and (not p.get("peak_price") or price > p["peak_price"]):
            p["peak_price"] = price
            peak_dirty = True
        peak = p.get("peak_price") or price

        ret_pct = round((price / cost - 1) * 100, 2) if (price and cost) else None
        drawdown_from_peak = round((1 - price / peak) * 100, 2) if (price and peak) else None

        # K线用于时间止损与动量反转
        kline = dfc.load_cached_kline(code)
        hold_days = _trade_days_since(p["added_at"], kline) if kline is not None else None
        mom20 = None
        if kline is not None and len(kline) > 21:
            c = kline["close"]
            mom20 = round((c.iloc[-1] / c.iloc[-21] - 1) * 100, 2)

        signals = []
        if drawdown_from_peak is not None and drawdown_from_peak >= r["trail_stop_pct"]:
            signals.append({"type": "跟踪止损", "level": "sell",
                            "msg": f"自峰值回撤 {drawdown_from_peak}% ≥ {r['trail_stop_pct']}%"})
        if ret_pct is not None and ret_pct <= -r["hard_stop_pct"]:
            signals.append({"type": "硬止损", "level": "sell",
                            "msg": f"较成本亏损 {abs(ret_pct)}% ≥ {r['hard_stop_pct']}%"})
        if (hold_days is not None and hold_days > r["max_hold_days"]
                and (ret_pct is None or ret_pct < r["time_stop_min_ret"])):
            signals.append({"type": "时间止损", "level": "warn",
                            "msg": f"已持有 {hold_days} 个交易日且收益不足 {r['time_stop_min_ret']}%，考虑换仓"})
        main_pct = s.get("main_pct")
        if (mom20 is not None and mom20 < 0
                and isinstance(main_pct, (int, float)) and main_pct < 0):
            signals.append({"type": "因子反转", "level": "warn",
                            "msg": f"20日动量转负({mom20}%) 且主力净流入占比为负({main_pct}%)"})

        action = ("卖出提示" if any(x["level"] == "sell" for x in signals)
                  else ("关注" if signals else "持有"))
        vol20 = None
        if kline is not None and len(kline) > 21:
            vol20 = float(kline["close"].pct_change().tail(20).std() or 0) * 100

        results.append({
            **{k: p.get(k) for k in ["code", "name", "industry", "cost", "added_at", "strategy", "note"]},
            "strategy_name": (S.STRATEGIES.get(p.get("strategy"), {}).get("name") if p.get("strategy") else None) or p.get("strategy") or "-",
            "price": price, "pct_today": s.get("pct"), "ret_pct": ret_pct,
            "peak_price": peak, "drawdown_from_peak": drawdown_from_peak,
            "hold_days": hold_days, "mom20": mom20, "main_pct": main_pct,
            "vol20": round(vol20, 2) if vol20 else None,
            "signals": signals, "action": action,
        })
        if ret_pct is not None:
            rets.append(ret_pct)
        ind = p.get("industry") or s.get("industry") or "未知"
        inds[ind] = inds.get(ind, 0) + 1

    if peak_dirty:
        with _LOCK:
            d2 = _load()
            pm = {p["code"]: p for p in positions}
            for p in d2["positions"]:
                if p["code"] in pm and pm[p["code"]].get("peak_price"):
                    p["peak_price"] = pm[p["code"]]["peak_price"]
            _save(d2)

    # ---- 组合级风控 ----
    n = len(results)
    avg_ret = round(sum(rets) / len(rets), 2) if rets else None
    # 未记录持仓数量，不能用股价/成本冒充权重；采用明确的等权估计。
    dds = [x["drawdown_from_peak"] for x in results if x["drawdown_from_peak"] is not None]
    port_dd = round(sum(dds) / len(dds), 2) if dds else None
    breaker = "正常"
    if port_dd is not None:
        if port_dd >= 10:
            breaker = "熔断：建议清仓观望（组合等权峰值回撤估计≥10%）"
        elif port_dd >= 5:
            breaker = "警戒：建议减半仓（组合等权峰值回撤估计≥5%）"
    conc = []
    for ind, cnt in sorted(inds.items(), key=lambda x: -x[1]):
        share = cnt / n * 100
        if share > 40 and n >= 3:
            conc.append(f"{ind} 占 {cnt}/{n} ({share:.0f}%)，行业过度集中")
    # 波动率倒数仓位建议
    vols = {x["code"]: x["vol20"] for x in results if x.get("vol20")}
    weights = None
    if vols:
        inv = {c: 1.0 / max(v, 0.5) for c, v in vols.items()}
        ssum = sum(inv.values())
        weights = {c: round(w / ssum * 100, 1) for c, w in inv.items()}
    sell_cnt = sum(1 for x in results if x["action"] == "卖出提示")
    warn_cnt = sum(1 for x in results if x["action"] == "关注")

    return {
        "positions": results,
        "portfolio": {
            "count": n, "avg_ret_pct": avg_ret, "avg_peak_drawdown": port_dd,
            "breaker": breaker, "concentration_warnings": conc,
            "suggested_weights_pct": weights,
            "weighting_note": "未记录持仓数量，组合收益与回撤按单票等权估计",
            "sell_signals": sell_cnt, "warn_signals": warn_cnt,
        },
        "rules": r,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "disclaimer": "规则化信号仅供研究参考，不构成投资建议。",
    }


def strategy_feedback(strategy_id=None):
    """优先使用已归档交易；没有平仓历史时才使用当前持仓的临时表现。"""
    raw = _load()
    history = raw.get("history", [])
    if strategy_id:
        history = [row for row in history if (row.get("strategy") or "") == strategy_id]
    closed_returns = [
        row["realized_ret_pct"]
        for row in history
        if isinstance(row.get("realized_ret_pct"), (int, float))
    ]
    if closed_returns:
        wins = sum(value > 0 for value in closed_returns)
        return {
            "strategy": strategy_id or "全部",
            "source": "closed_trades",
            "count": len(closed_returns),
            "avg_ret_pct": round(sum(closed_returns) / len(closed_returns), 2),
            "win_rate_pct": round(wins / len(closed_returns) * 100, 1),
            "avg_hold_days": None,
            "sell_signals": 0,
            "warn_signals": 0,
            "advice": ["基于已移出跟踪池并归档的交易计算；样本较小时仅作辅助判断"],
        }
    try:
        d = check_positions()
    except Exception:
        return None
    pos = d.get("positions", [])
    if strategy_id:
        pos = [p for p in pos if (p.get("strategy") or "") == strategy_id]
    if not pos:
        return {"strategy": strategy_id, "count": 0, "note": "该策略暂无跟踪持仓，无实盘反馈"}
    rets = [p["ret_pct"] for p in pos if isinstance(p.get("ret_pct"), (int, float))]
    holds = [p["hold_days"] for p in pos if isinstance(p.get("hold_days"), (int, float))]
    sell_hits = sum(1 for p in pos if p.get("action") == "卖出提示")
    warn_hits = sum(1 for p in pos if p.get("action") == "关注")
    win = sum(1 for r in rets if r > 0)
    fb = {
        "strategy": strategy_id or "全部",
        "source": "open_positions_provisional",
        "count": len(pos),
        "avg_ret_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "win_rate_pct": round(win / len(rets) * 100, 1) if rets else None,
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
        "sell_signals": sell_hits, "warn_signals": warn_hits,
    }
    # 反馈判断：实盘跟踪表现差 → 建议优化时偏保守
    advice = []
    if fb["win_rate_pct"] is not None and fb["win_rate_pct"] < 40:
        advice.append("跟踪胜率<40%：建议优先选择 use_gate=True 且分散度更高（topn更大）的参数")
    if fb["avg_ret_pct"] is not None and fb["avg_ret_pct"] < 0:
        advice.append("跟踪平均收益为负：回测最优参数可能过拟合当前风格，建议采用更长调仓周期(rebalance=10或20)降低换手")
    if fb["avg_hold_days"] is not None and fb["avg_hold_days"] < 5 and sell_hits > 0:
        advice.append("持仓短期内频繁触发止损：候选 rebalance=5 的高频参数与实际风格不符，建议用10日档")
    if not advice:
        advice.append("当前持仓表现正常；这是未平仓样本，不应替代样本外回测")
    fb["advice"] = advice
    return fb


def update_rules(new_rules):
    with _LOCK:
        data = _load()
        rules = data.get("rules") or dict(DEFAULT_RULES)
        for k, v in (new_rules or {}).items():
            if k in DEFAULT_RULES and isinstance(v, (int, float)):
                rules[k] = v
        data["rules"] = rules
        _save(data)
        return rules
