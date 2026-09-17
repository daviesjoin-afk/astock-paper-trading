# -*- coding: utf-8 -*-
"""CLI 端到端（§24/§25/§26）：在**已迁移的数据库副本**上跑 OFF / ON 两条路径。

- 只操作副本；真实库不动。
- 断言：市场层面结果 OFF/ON 逐字节相同；仓位层只新增观察产物。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
SCRATCH = os.path.join(tempfile.gettempdir(), "astock_cli_e2e")
os.makedirs(SCRATCH, exist_ok=True)
DB = os.path.join(SCRATCH, "paper_trading.sqlite3")

import db_migrate  # noqa: E402

shutil.copy(os.path.join(ROOT, "data_cache", "paper_trading.sqlite3"), DB)
db_migrate.migrate("paper_trading", apply=True, path=DB, backup=False)

conn = sqlite3.connect(DB, timeout=30)
# 用真实账户的合法策略戳（paper_orders 有写入期触发器校验策略版本）
stamp = conn.execute(
    "SELECT strategy_id, strategy_version, strategy_checksum FROM paper_orders "
    "WHERE execution_status='verified' LIMIT 1"
).fetchone()
strategy_id, version, checksum = stamp
conn.execute(
    "INSERT OR REPLACE INTO historical_tradability_archive(code,session_date,"
    "effective_at,observed_at,is_listed,is_st,is_suspended,is_price_limit_locked,"
    "price_limit_direction,has_market_quote,has_trade_volume,source,listing_date,"
    "delisting_date,suspension_reason,created_at) "
    "VALUES('600903','2026-09-17','2026-09-17T15:05:00','2026-09-17T15:05:00',"
    "1,0,0,0,NULL,1,1,'probe','2000-01-01',NULL,NULL,'2026-09-17T15:05:00')"
)
conn.execute(
    "INSERT INTO paper_orders(id,account_id,side,code,name,qty,planned_price,"
    "filled_price,amount,fees,status,risk_payload,created_at,executed_at,order_type,"
    "origin,strategy_id,strategy_version,strategy_checksum,execution_status,"
    "execution_verified,execution_evidence_source) "
    "VALUES(99001,?, 'buy','600903','逐日新材',1200,10.0,10.0,12000.0,0.0,'filled','',"
    "'2026-09-17 09:30:00','2026-09-17 10:00:00','market','strategy',?,?,?,"
    "'verified',1,'paper_orders+paper_fills')",
    (strategy_id, strategy_id, version, checksum),
)
conn.execute(
    "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
    "fill_date,quote_at,assumption) VALUES(99001,?,'buy','600903',1200,10.0,"
    "12000.0,0.0,'2026-09-17',NULL,'close')",
    (strategy_id,),
)
conn.execute(
    "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
    "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
    "cost_fee_included,is_t_base) VALUES(136,?,'600903','逐日新材',NULL,1200,1200,"
    "10.0,'2026-09-17 10:00:00','2026-09-18','stock_t1',99001,1,1)",
    (strategy_id,),
)
conn.commit()
conn.close()
print("seeded copy:", DB, "account:", strategy_id)

ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ASTOCK_DATA_DIR": SCRATCH}


AS_OF = "2026-10-09T16:00:00+08:00"          # 固定知识时点：否则两次运行的 as-of 不同


def run(extra, out):
    with open(out, "w", encoding="utf-8") as handle:
        proc = subprocess.run(
            [sys.executable, "work/tradability_shadow_validation.py",
             "--session", "2026-09-17", "--codes", "600903", "--side", "sell",
             "--validation-as-of", AS_OF, *extra],
            cwd=ROOT, env=ENV, stdout=handle, stderr=subprocess.STDOUT,
        )
    return proc.returncode


off_path = os.path.join(SCRATCH, "off.json")
on_path = os.path.join(SCRATCH, "on.json")
print("off exit:", run(["--json"], off_path))
print("on  exit:", run(["--position-aware", "--account-id", strategy_id,
                       "--sell-quantity", "1200", "--json"], on_path))

failures = []
with open(off_path, encoding="utf-8") as fh:
    off_raw = fh.read()
with open(on_path, encoding="utf-8") as fh:
    on_raw = fh.read()
if not off_raw.strip().startswith("{"):
    failures.append("OFF 路径没有产出 JSON（看下面输出）")
    print(off_raw[-1500:])
if not on_raw.strip().startswith("{"):
    failures.append("ON 路径没有产出 JSON")
    print(on_raw[-1500:])

if not failures:
    off = json.loads(off_raw)
    on = json.loads(on_raw)
    same_summary = off["summary"] == on["summary"]
    same_comparisons = off["comparisons"] == on["comparisons"]
    print("市场层面 summary 逐字段相同:", same_summary)
    print("市场层面 comparisons 逐字段相同:", same_comparisons)
    if not same_summary or not same_comparisons:
        failures.append("仓位层改写了市场层面结果")
    pa = on.get("position_aware") or {}
    print("position_aware error:", pa.get("error"))
    summary = pa.get("summary")
    if summary:
        for key in ("requested", "sell_comparisons", "position_comparable",
                    "position_not_comparable", "position_evidence_proven",
                    "t1_pass", "t1_blocked", "requested_sell_quantity",
                    "proven_sellable_quantity", "t1_locked_quantity",
                    "unknown_quantity", "position_comparison_rate"):
            print(" ", key, "=", summary[key])
        for item in pa["comparisons"]:
            print("  cmp:", item["code"], item["side"],
                  "market=" + str(item["market_status"]),
                  "pos=" + str(item["position_status"]),
                  "ev=" + str(item["position_evidence_status"]),
                  "held=" + str(item["held_quantity"]),
                  "sellable=" + str(item["sellable_quantity"]),
                  "locked=" + str(item["t1_locked_quantity"]),
                  "reason=" + str(item["production_reason"]))

print("\n%s" % ("PASS" if not failures else "FAIL: %s" % failures))
raise SystemExit(1 if failures else 0)
