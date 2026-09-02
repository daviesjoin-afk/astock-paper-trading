# -*- coding: utf-8 -*-
"""Read-only paper execution-quality audit."""
from __future__ import annotations
import sqlite3
from collections import defaultdict

EXECUTION_QUALITY_VERSION = "execution-quality-shadow-v1"

def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def audit(paper_db_path, limit=5000):
    result = {
        "version": EXECUTION_QUALITY_VERSION, "mode": "shadow_only",
        "trading_impact": "none", "status": "unavailable",
        "orders": 0, "filled_orders": 0, "rejected_orders": 0,
        "by_strategy": [], "notes": [
            "成交质量仅用于复盘，不改委托价格或交易规则。",
            "模拟成交使用既定滑点假设，不能替代券商真实成交质量。",
        ],
    }
    try:
        conn = sqlite3.connect(paper_db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            """SELECT a.source_strategy AS account_id,o.side,o.qty,o.planned_price,
                      o.filled_price,o.amount,o.fees,o.status,o.reason,o.executed_at
               FROM paper_orders o LEFT JOIN paper_accounts a ON a.id=o.account_id
               ORDER BY o.id DESC LIMIT ?""", (int(limit),)
        )]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        try: conn.close()
        except Exception: pass

    buckets = defaultdict(lambda: {"orders":0,"filled":0,"rejected":0,"fees":0.0,"amount":0.0,"slippage_bps":[],"missing_plan":0})
    for row in rows:
        key = row.get("account_id") or "unknown"
        bucket = buckets[key]; bucket["orders"] += 1
        status = str(row.get("status") or "")
        if status in {"risk_rejected","rejected","cancelled","expired"}:
            bucket["rejected"] += 1
        if status != "filled":
            continue
        bucket["filled"] += 1
        amount = max(_num(row.get("amount")), 0.0)
        fees = max(_num(row.get("fees")), 0.0)
        bucket["amount"] += amount; bucket["fees"] += fees
        planned, filled = _num(row.get("planned_price")), _num(row.get("filled_price"))
        if planned <= 0 or filled <= 0:
            bucket["missing_plan"] += 1
            continue
        raw_bps = (filled / planned - 1.0) * 10000.0
        # Positive is adverse for both buy and sell.
        adverse_bps = raw_bps if str(row.get("side")) == "buy" else -raw_bps
        bucket["slippage_bps"].append(adverse_bps)

    summaries=[]
    for account_id, b in sorted(buckets.items()):
        filled=b["filled"]; orders=b["orders"]; values=sorted(b["slippage_bps"])
        median=values[len(values)//2] if values else None
        p95=values[min(len(values)-1, int(len(values)*.95))] if values else None
        summaries.append({
            "account_id":account_id, "orders":orders, "filled_orders":filled,
            "rejected_orders":b["rejected"],
            "fill_rate_pct":round(100*filled/max(orders,1),2),
            "rejection_rate_pct":round(100*b["rejected"]/max(orders,1),2),
            "fees":round(b["fees"],2),
            "fee_bps":round(10000*b["fees"]/b["amount"],2) if b["amount"] else None,
            "adverse_slippage_median_bps":round(median,2) if median is not None else None,
            "adverse_slippage_p95_bps":round(p95,2) if p95 is not None else None,
            "orders_without_planned_price":b["missing_plan"],
        })
    result.update({
        "status":"ready", "orders":len(rows),
        "filled_orders":sum(x["filled_orders"] for x in summaries),
        "rejected_orders":sum(x["rejected_orders"] for x in summaries),
        "by_strategy":summaries,
    })
    return result

if __name__ == "__main__":
    import tempfile, os
    fd,path=tempfile.mkstemp(); os.close(fd)
    c=sqlite3.connect(path)
    c.executescript("""CREATE TABLE paper_accounts(id INTEGER,source_strategy TEXT);
    CREATE TABLE paper_orders(id INTEGER,account_id INTEGER,side TEXT,qty INTEGER,planned_price REAL,filled_price REAL,amount REAL,fees REAL,status TEXT,reason TEXT,executed_at TEXT);
    INSERT INTO paper_accounts VALUES(1,'tq_breakout');
    INSERT INTO paper_orders VALUES(1,1,'buy',100,10,10.01,1001,5,'filled','', '');
    INSERT INTO paper_orders VALUES(2,1,'buy',100,10,0,0,0,'risk_rejected','', '');"""); c.commit(); c.close()
    x=audit(path); assert x["status"]=="ready" and x["filled_orders"]==1 and x["rejected_orders"]==1
    os.unlink(path); print("execution_quality_shadow self-check passed")
