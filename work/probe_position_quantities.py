# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow —— production-copy 探针（只读）。

用法::

    python work/probe_position_quantities.py <db_copy_path>

**只读**：只对传入的 DB **副本** 执行 SELECT，不写任何表、不改任何行。

统计口径（规格 §9 逐字要求）：

* active cycle id
* per-account open lots
* same code across multiple accounts
* partially consumed lots
* fully consumed lots
* historical quantity reconstructable / unprovable counts

**关键区分**（规格逐字禁止把二者混为一谈）::

    current open lots coverage      ← 只说明"今天有多少未平仓 lot"
    historical position coverage    ← 只有真的完成 decision-time reconstruction 才算

本探针**不**用前者冒充后者：`reconstructable` 一栏才是"历史数量可重建"的覆盖度，
它由**逐组重放**（生产 FIFO 语义 + 已验证卖出成交）判定，重放与账本对不上即
`unprovable`。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import tradability_position_evidence as PE  # noqa: E402


def _rows(conn, sql, args=()):
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    except sqlite3.OperationalError as exc:
        print(f"  (skipped: {exc})")
        return []


def main(argv):
    if len(argv) < 2:
        print("用法: python work/probe_position_quantities.py <db_copy_path>")
        return 2
    path = argv[1]
    if not Path(path).exists():
        print(f"DB 副本不存在: {path}")
        return 2
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print("position quantity probe (READ-ONLY)")
    print("=" * 72)

    # ── 1. active cycle ──
    cycles = _rows(conn, "SELECT id,cycle_key,status FROM paper_cycles "
                         "WHERE status IN ('draft','running','paused') ORDER BY id DESC")
    active = cycles[0]["id"] if cycles else None
    print(f"active cycle id: {active}  (candidates={[c['id'] for c in cycles]})")

    # ── 2. lot 总量与消耗分类（全周期 + 当前周期）──
    for label, clause, args in (
        ("all cycles", "1=1", ()),
        ("active cycle", "cycle_id=?", (active,)),
    ):
        if active is None and clause != "1=1":
            continue
        total = _rows(conn, f"SELECT COUNT(*) AS n FROM paper_position_lots WHERE {clause}", args)
        open_lots = _rows(conn, f"SELECT COUNT(*) AS n FROM paper_position_lots "
                                f"WHERE {clause} AND remaining_qty>0", args)
        partial = _rows(conn, f"SELECT COUNT(*) AS n FROM paper_position_lots "
                              f"WHERE {clause} AND remaining_qty>0 AND remaining_qty<qty", args)
        fully = _rows(conn, f"SELECT COUNT(*) AS n FROM paper_position_lots "
                            f"WHERE {clause} AND remaining_qty<=0", args)
        print(f"\n[{label}] lots total={total[0]['n']} open={open_lots[0]['n']} "
              f"partially_consumed={partial[0]['n']} fully_consumed={fully[0]['n']}")

    # ── 3. per-account open lots（当前周期）──
    print("\nper-account open lots (active cycle):")
    per_account = _rows(
        conn,
        "SELECT account_id, COUNT(*) AS lots, SUM(remaining_qty) AS open_qty "
        "FROM paper_position_lots WHERE cycle_id=? AND remaining_qty>0 "
        "GROUP BY account_id ORDER BY lots DESC", (active,))
    for row in per_account:
        print(f"  {row['account_id']}: lots={row['lots']} open_qty={row['open_qty']}")
    if not per_account:
        print("  (none)")

    # ── 4. same code across multiple accounts（当前周期）──
    print("\nsame code across multiple accounts (active cycle):")
    shared = _rows(
        conn,
        "SELECT code, COUNT(DISTINCT account_id) AS accounts, "
        "COUNT(*) AS lots, SUM(remaining_qty) AS open_qty "
        "FROM paper_position_lots WHERE cycle_id=? AND remaining_qty>0 "
        "GROUP BY code HAVING COUNT(DISTINCT account_id)>1 ORDER BY accounts DESC",
        (active,))
    for row in shared:
        print(f"  {row['code']}: accounts={row['accounts']} lots={row['lots']} "
              f"open_qty={row['open_qty']}")
    if not shared:
        print("  (none) —— 该库当前不存在同 code 跨账户持仓")
    print(f"  => shared-code groups: {len(shared)}")

    # ── 5. multi-lot (account, code) groups（当前周期）──
    groups = _rows(
        conn,
        "SELECT account_id, code, COUNT(*) AS lots FROM paper_position_lots "
        "WHERE cycle_id=? GROUP BY account_id, code HAVING COUNT(*)>1 "
        "ORDER BY lots DESC", (active,))
    print(f"\nmulti-lot (account,code) groups (active cycle): {len(groups)}")
    for row in groups[:10]:
        print(f"  {row['account_id']}/{row['code']}: lots={row['lots']}")

    # ── 6. 历史数量可重建 / 不可重建（**逐组重放**）──
    print("\nhistorical quantity reconstruction (active cycle):")
    adapter = PE.PositionEvidenceAdapter(conn, evidence_provider=None)
    scopes = _rows(
        conn,
        "SELECT DISTINCT account_id, code FROM paper_position_lots "
        "WHERE cycle_id=? ORDER BY account_id, code", (active,))
    reconstructable = unprovable = 0
    reasons: dict = {}
    for scope in scopes:
        lots = adapter.load_lots(scope["code"], cycle_id=active,
                                 account_id=scope["account_id"])
        events = adapter._sell_events(scope["account_id"], scope["code"])
        replay = adapter._replay(lots, events, decision_session="2099-01-01",
                                 decision_at=None)
        if replay["consistent"]:
            reconstructable += 1
        else:
            unprovable += 1
            for item in replay["diagnostics"]:
                reasons[item] = reasons.get(item, 0) + 1
    print(f"  (cycle,account,code) groups total: {len(scopes)}")
    print(f"  reconstructable: {reconstructable}")
    print(f"  unprovable: {unprovable}")
    if reasons:
        print(f"  unprovable reasons: {reasons}")

    # ── 7. 卖出证据与验证覆盖 ──
    sells = _rows(conn, "SELECT COUNT(*) AS n FROM paper_fills WHERE side='sell'")
    verified_sells = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id "
        "WHERE f.side='sell' AND COALESCE(o.execution_verified,0)=1 "
        "AND o.execution_status='verified'")
    buys = _rows(conn, "SELECT COUNT(*) AS n FROM paper_fills WHERE side='buy'")
    verified_buys = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id "
        "WHERE f.side='buy' AND COALESCE(o.execution_verified,0)=1 "
        "AND o.execution_status='verified'")
    print(f"\nevidence: buy fills={buys[0]['n']} (verified={verified_buys[0]['n']}), "
          f"sell fills={sells[0]['n']} (verified={verified_sells[0]['n']})")

    # ── 8. ETF lot 覆盖 ──
    etf = _rows(conn, "SELECT COUNT(*) AS n FROM paper_position_lots WHERE asset_type='etf_t0'")
    print(f"etf_t0 lots: {etf[0]['n']}")

    print("\n注意：current open lots coverage **不是** historical position coverage；")
    print("      后者只由上面的 reconstructable 一栏给出。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
