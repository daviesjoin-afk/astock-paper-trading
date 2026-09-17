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
# 当前周期 id（与生产只读面板同一条件）；lot 必须落在该周期里才可被观察到。
cycle_row = conn.execute(
    "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused') "
    "ORDER BY id DESC LIMIT 1"
).fetchone()
cycle_id = int(cycle_row[0])
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
    "cost_fee_included,is_t_base) VALUES(?,?,'600903','逐日新材',NULL,1200,1200,"
    "10.0,'2026-09-17 10:00:00','2026-09-18','stock_t1',99001,1,1)",
    (cycle_id, strategy_id),
)
conn.commit()
conn.close()
print("seeded copy:", DB, "account:", strategy_id, "cycle:", cycle_id)

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


def run_expect_error(extra):
    """仓位层的 operator 契约：显式非法值必须 exit 2，绝不 fail-open。"""
    proc = subprocess.run(
        [sys.executable, "work/tradability_shadow_validation.py",
         "--session", "2026-09-17", "--codes", "600903", "--side", "sell",
         "--position-aware", *extra],
        cwd=ROOT, env=ENV, capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


failures = []
for label, extra in (
    ("missing --account-id", []),
    ("invalid --validation-as-of",
     ["--account-id", strategy_id, "--validation-as-of", "not-a-timestamp"]),
    ("non-positive --sell-quantity",
     ["--account-id", strategy_id, "--sell-quantity", "0"]),
):
    code, text = run_expect_error(extra)
    ok = code == 2
    print(f"operator error [{label}]: exit={code} ({'ok' if ok else 'NOT REJECTED'}) :: {text}")
    if not ok:
        failures.append(f"operator error 未生效: {label} (exit={code})")


off_path = os.path.join(SCRATCH, "off.json")
on_path = os.path.join(SCRATCH, "on.json")
print("off exit:", run(["--json"], off_path))
print("on  exit:", run(["--position-aware", "--account-id", strategy_id,
                       "--sell-quantity", "1200", "--json"], on_path))

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
    if pa.get("error"):
        failures.append(f"position-aware 路径报错: {pa['error']}")
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
                  "cycle=" + str(item.get("cycle_id")),
                  "held=" + str(item["held_quantity"]),
                  "sellable=" + str(item["sellable_quantity"]),
                  "locked=" + str(item["t1_locked_quantity"]),
                  "basis=" + str(item.get("quantity_basis")),
                  "reason=" + str(item["production_reason"]))
        # ── v2 承重断言 ──
        # 该样本：09-17 买入（available_date=09-18），决策 session 也是 09-17
        # → 同日卖出被 T+1 锁住。**决策时点的持仓数量必须是 1200**（而不是
        # 今天的可变余额），且这 1200 全部是 t1_locked。
        if not summary["position_comparable"]:
            failures.append("预期该样本可比，但 position_comparable=0")
        if summary["t1_locked_quantity"] != 1200:
            failures.append(
                "决策时点历史数量口径错误: t1_locked_quantity="
                f"{summary['t1_locked_quantity']} != 1200")
        if summary["proven_sellable_quantity"] != 0:
            failures.append(
                "同日买入不得被判成可卖: proven_sellable_quantity="
                f"{summary['proven_sellable_quantity']} != 0")
        for item in pa["comparisons"]:
            if item["side"] != "sell":
                continue
            if item["held_quantity"] != 1200:
                failures.append(
                    f"决策时点持仓数量错误: held={item['held_quantity']} != 1200")
            if item.get("quantity_basis") != "historical_replay_fifo_at_decision":
                failures.append(
                    f"quantity_basis 不是历史重放口径: {item.get('quantity_basis')}")
            if item.get("cycle_id") != cycle_id:
                failures.append(
                    f"观察未绑定目标 cycle: {item.get('cycle_id')} != {cycle_id}")

print("\n%s" % ("PASS" if not failures else "FAIL: %s" % failures))
raise SystemExit(1 if failures else 0)