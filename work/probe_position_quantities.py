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


#: 规格 §21 要求的 unprovable 归因分类。**按优先级**匹配：一个组只归一类
#: （具体原因优先于状态名，状态名优先于通用结论），否则总数会被重复计数。
#: 每条内部元组是 ``(优先级, 诊断名, 类别)``，数字越小越具体。
_REASON_PRIORITY = (
    # 归属本身无法证明：竞争周期边界缺失 / 显式身份冲突
    (10, "competing_cycle_unprovable", "cycle_attribution_unprovable"),
    (11, "sell_fill_cycle_identity_conflict", "cycle_attribution_unprovable"),
    # 多个可证明窗口都能覆盖该卖出
    (20, "sell_fill_cycle_ambiguous", "cycle_attribution_ambiguous"),
    # 请求周期自身窗口/起点不可证明（没有更具体的原因时才算它）
    (30, "sell_fill_cycle_unprovable", "cycle_attribution_unprovable"),
    # 重放终态与账本对不上 ⇒ 成交证据不完整
    (40, "replay_does_not_match_ledger", "replay_ledger_mismatch"),
    (41, "sell_fill_exceeds_available_lots", "replay_ledger_mismatch"),
    (42, "unverified_sell_fill_excluded", "replay_ledger_mismatch"),
    # 成交身份冲突（别的账户 / 别的股票）
    (50, "sell_fill_identity_mismatch", "identity_mismatch"),
    (51, "source_order_identity_mismatch", "identity_mismatch"),
    (52, "fill_identity_mismatch", "identity_mismatch"),
    # 事件时间无法确定先后
    (60, "sell_fill_session_invalid", "unknown_event_ordering"),
    (61, "same_session_sell_time_unknown", "unknown_event_ordering"),
    (62, "same_session_sell_not_orderable", "unknown_event_ordering"),
    (63, "future_lot_consumption_required", "unknown_event_ordering"),
)

#: 分类的**固定输出顺序**（缺席也打印 0，便于跨轮对比）。
_REASON_CATEGORIES = (
    "cycle_attribution_unprovable",
    "cycle_attribution_ambiguous",
    "replay_ledger_mismatch",
    "identity_mismatch",
    "unknown_event_ordering",
    "other",
)


def _categorize_diagnostics(diagnostics) -> str:
    """把一个 unprovable 组归到**唯一**一类（按最具体的原因）。"""
    present = set(diagnostics or ())
    for _, name, category in sorted(_REASON_PRIORITY):
        if name in present:
            return category
    return "other"


def _rows(conn, sql, args=()):
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    except sqlite3.OperationalError as exc:
        print(f"  (skipped: {exc})")
        return []


def _count(rows) -> int:
    """COUNT 查询的取值；空结果（表缺失等）一律 0，绝不让探针崩在半路。"""
    if not rows:
        return 0
    value = rows[0].get("n")
    return 0 if value is None else int(value)


def _scalar(conn, sql, args=()):
    rows = _rows(conn, sql, args)
    if not rows:
        return None
    return next(iter(rows[0].values()), None)


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
    for row in _rows(conn, "SELECT id,status,started_at,ended_at,created_at "
                           "FROM paper_cycles ORDER BY id"):
        print(f"  cycle {row['id']}: status={row['status']} "
              f"start={row['started_at']} end={row['ended_at']} created={row['created_at']}")

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
    # 走**公开只读诊断入口**：与真实 PositionEvidenceAdapter 完全同一条
    # cycle-scoped 重放路径（同一 _sell_events / _eligible_lots / _replay），
    # 不再由探针自己调私有方法、也不再用 2099 哨兵伪造决策时点。
    reconstructable = unprovable = 0
    reasons: dict = {}
    by_category = {name: 0 for name in _REASON_CATEGORIES}
    attribution: dict = {}
    for scope in scopes:
        report = adapter.replay_diagnostics(
            scope["code"], cycle_id=active, account_id=scope["account_id"])
        if report["consistent"]:
            reconstructable += 1
        else:
            unprovable += 1
            for item in report["diagnostics"]:
                reasons[item] = reasons.get(item, 0) + 1
            # 每个**组**只归一类（规格 §21），否则同一组的多个诊断会被重复计数。
            by_category[_categorize_diagnostics(report["diagnostics"])] += 1
        # 周期归属分布（proven / mismatch / ambiguous / unprovable）——由公开入口
        # 一并返回，探针不再自己调私有方法。
        for key, value in (report.get("attribution") or {}).items():
            attribution[key] = attribution.get(key, 0) + value
    print(f"  (cycle,account,code) groups total: {len(scopes)}")
    print(f"  reconstructable: {reconstructable}")
    print(f"  unprovable: {unprovable}")
    if reasons:
        print(f"  unprovable reasons (raw): {reasons}")
    # 按**原因**拆解（规格 §21）：只报一个总数无法说明真正缺的是哪种证据。
    # 一个组只归一类，因此各类之和 == unprovable 组数。
    print("  unprovable breakdown by reason (每组归一类):")
    for name in _REASON_CATEGORIES:
        print(f"    {name}: {by_category[name]}")
    print(f"  SELL cycle attribution: {attribution}")
    print("  ^ 以上数字由本轮 exact head 的 **cycle-scoped replay** 重新计算得出"
          "（公开入口 replay_diagnostics），非复用上一轮读数。")

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
    print(f"\nevidence: buy fills={_count(buys)} (verified={_count(verified_buys)}), "
          f"sell fills={_count(sells)} (verified={_count(verified_sells)})")

    # ── 8. ETF lot 覆盖 ──
    etf = _rows(conn, "SELECT COUNT(*) AS n FROM paper_position_lots WHERE asset_type='etf_t0'")
    print(f"T+0 ETF samples (asset_type='etf_t0'): {_count(etf)}")

    print("\n注意：current open lots coverage **不是** historical position coverage；")
    print("      后者只由上面的 reconstructable 一栏给出。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
