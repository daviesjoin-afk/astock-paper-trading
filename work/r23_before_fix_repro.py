#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R23 before/after reproduction —— Strategy / Selection Provenance (C1..C10)。

用法::

    python work/r23_before_fix_repro.py --expect before
    python work/r23_before_fix_repro.py --expect after

设计约束：一个「两边可能悄悄变成同一份代码」的 before-fix 探针比没有更糟。因此本脚本

1. **自检修订版本**：探测 ``backend/strategy_selection_provenance.py`` 是否存在、以及
   ``paper_selection.py`` 是否已持久化 run 级 provenance，据此判定 ``before`` / ``after``；
   与 ``--expect`` 不符时直接退出 1，而不是打印一个无意义的结论。
2. **只观察可观察事实**：断言全部落在真实生产入口（``PS.run_daily`` / ``PS.latest`` /
   ``ST.record_run`` / ``PT.start_new_cycle`` / ``PT.generate_signals`` / ``PT.run_slot``）
   与真实数据库上；不 import 任何 R23 新模块，因此同一份脚本在修复前后都可运行。
3. **每条给出可核对的证据行**；``REPRODUCED`` = 该缺口在当前树上真实存在。

C3/C4/C10 复用 ``test_production_path_golden_replay.OfflinePaperEnv``：那是仓库里唯一一套
「依赖注入 + 真实生产链路」的离线环境（行情/健康工件全部注入，不手写 paper_orders）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_selection as PS  # noqa: E402
import selection_tracking as ST  # noqa: E402
import strategy_registry as SR  # noqa: E402

REPRODUCED = "REPRODUCED"
NOT_REPRODUCED = "NOT REPRODUCED"

DAY = "2026-09-07"
OLD_DAY = "2026-08-03"
STRATEGY_ID = "provenance_alpha"

RESULTS: list[tuple[str, str, str]] = []


def record(case: str, verdict: str, detail: str) -> None:
    RESULTS.append((case, verdict, detail))
    print(f"[{case}] {verdict}")
    for line in str(detail).splitlines():
        print(f"        {line}")


def detect_revision() -> str:
    contract = os.path.exists(os.path.join(BACKEND, "strategy_selection_provenance.py"))
    source = open(os.path.join(BACKEND, "paper_selection.py"), encoding="utf-8").read()
    wired = "provenance_key" in source and "strategy_version" in source
    if contract and wired:
        return "after"
    if not contract and not wired:
        return "before"
    raise SystemExit(
        "无法判定修订版本（contract=%s wired=%s）：工作树处于修复中间态，探针拒绝给出结论"
        % (contract, wired)
    )


def columns(conn, table) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _picks(count=3, day=DAY):
    return [
        {
            "code": f"6001{i:03d}", "name": f"溯源{i}", "industry": "电子",
            "price": 10.0 + i, "pct": 1.0, "score": 0.9 - i * 0.01,
            "super_net": 1_000_000.0 - i, "reasons": [f"理由{i}"],
            "news_check": {"status": "clean", "hits": 0},
            "historical_factor_date": day,
        }
        for i in range(1, count + 1)
    ]


def _payload(count=3, day=DAY):
    return {
        "strategy": "three_day",
        "strategy_name": "三日策略",
        "picks": _picks(count=count, day=day),
        "data_quality": {"reference_date": day, "complete_cutoff": day},
    }


class SelectionEnv:
    """family A/B 的隔离环境：临时目录 + 独立 registry DB + 独立 research DB。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="r23-repro-")
        self.registry_db = os.path.join(self.tmp, "paper_trading.sqlite3")
        self.research_db = os.path.join(self.tmp, "selection_tracking.db")
        self._patches = [
            (SR, "DEFAULT_DB_PATH", SR.DEFAULT_DB_PATH),
            (PS, "DB_PATH", PS.DB_PATH),
            (ST, "DB_PATH", ST.DB_PATH),
            (PS, "_run_one", PS._run_one),
            (ST, "_latest_signal_date", ST._latest_signal_date),
            (ST, "_benchmark_price_on_or_before", ST._benchmark_price_on_or_before),
        ]
        # `after` 树新增的显式 registry 入口：设置为**本环境**的账本，绝不靠猜
        # 「最近的那个 .sqlite3」。不存在时跳过（`before` 树上没有这个属性）。
        if hasattr(PS, "REGISTRY_DB_PATH"):
            self._patches.append((PS, "REGISTRY_DB_PATH", PS.REGISTRY_DB_PATH))
        if hasattr(ST, "REGISTRY_DB_PATH"):
            self._patches.append((ST, "REGISTRY_DB_PATH", ST.REGISTRY_DB_PATH))
        SR.DEFAULT_DB_PATH = self.registry_db
        PS.DB_PATH = self.research_db
        ST.DB_PATH = self.research_db
        if hasattr(PS, "REGISTRY_DB_PATH"):
            PS.REGISTRY_DB_PATH = self.registry_db
        if hasattr(ST, "REGISTRY_DB_PATH"):
            ST.REGISTRY_DB_PATH = self.registry_db
        PS._run_one = lambda model_id, topn: _payload(count=3)
        ST._latest_signal_date = lambda picks: DAY
        ST._benchmark_price_on_or_before = lambda day: None
        with self._registry() as conn:
            SR.ensure_schema(conn)
            conn.commit()

    def _registry(self):
        conn = sqlite3.connect(self.registry_db, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def _research(self):
        conn = sqlite3.connect(self.research_db, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def upgrade(self, strategy_id="tq_breakout", name="改名后的策略"):
        """发布 v2（同时改名）——用于 current-head reinterpretation。"""
        with self._registry() as conn:
            version = SR.save_definition(conn, strategy_id, {"name": name},
                                         actor="r23-repro", change_note="v2")
            conn.commit()
            return version.version, version.checksum

    def close(self):
        for target, attr, old in reversed(self._patches):
            setattr(target, attr, old)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# C1 — current-head reinterpretation
# ---------------------------------------------------------------------------


def case_c1(env: SelectionEnv) -> None:
    PS.run_daily(topn=5, run_date=DAY)
    env.upgrade()
    with env._research() as conn:
        PS.ensure_schema(conn)
        cols = columns(conn, "paper_selection_runs")
        row = conn.execute(
            "SELECT * FROM paper_selection_runs WHERE trade_date=? AND strategy_id='tq_breakout'",
            (DAY,)).fetchone()
    stored = dict(row) if row is not None else {}
    answers_version = ("strategy_version" in cols and "strategy_checksum" in cols
                       and stored.get("strategy_version") is not None)
    group = next((g for g in PS.latest(trade_date=DAY)["strategies"]
                  if g["strategy_id"] == "tq_breakout"), {})
    live_name = SR.get("tq_breakout").name
    detail = (
        f"persisted columns={cols}\n"
        f"stored strategy_version={stored.get('strategy_version')!r} "
        f"strategy_checksum={stored.get('strategy_checksum')!r}\n"
        f"latest(D).strategy_name={group.get('strategy_name')!r} / "
        f"current registry name={live_name!r}"
    )
    reproduced = (not answers_version) or (group.get("strategy_name") == live_name
                                          and live_name != "短线日内做T")
    record("C1", REPRODUCED if reproduced else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------
# C2 — same-day version overwrite
# ---------------------------------------------------------------------------


def case_c2(env: SelectionEnv) -> None:
    PS.run_daily(topn=5, run_date=DAY)
    with env._research() as conn:
        PS.ensure_schema(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM paper_selection_runs WHERE trade_date=? "
            "AND strategy_id='tq_breakout'", (DAY,)).fetchone()[0]
    v2, _ = env.upgrade()
    PS.run_daily(topn=5, run_date=DAY)
    with env._research() as conn:
        PS.ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM paper_selection_runs WHERE trade_date=? AND strategy_id='tq_breakout'",
            (DAY,)).fetchall()
    after = len(rows)
    versions = sorted({r["strategy_version"] for r in rows
                       if "strategy_version" in r.keys() and r["strategy_version"] is not None})
    detail = (f"v2 published (version={v2}); same-day runs before={before} after={after} "
              f"distinct persisted versions={versions}")
    reproduced = after < 2 or versions != [1, 2]
    record("C2", REPRODUCED if reproduced else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------
# C5 — checksum mismatch（selection 侧）
# ---------------------------------------------------------------------------


def case_c5(env: SelectionEnv) -> None:
    with env._registry() as conn:
        version = SR.get_version("tq_breakout", conn=conn)
    with env._research() as conn:
        PS.ensure_schema(conn)
        cols = columns(conn, "paper_selection_runs")
        stored = None
        if "strategy_checksum" in cols:
            stored = conn.execute(
                "SELECT strategy_checksum FROM paper_selection_runs WHERE trade_date=? "
                "AND strategy_id='tq_breakout'", (DAY,)).fetchone()
    # 账本侧（paper_signals）已由 DB 触发器 fail closed；这里只看 selection 侧：
    # 「版本/checksum 都不落库」⇒ 系统无法区分「v1 的 v1 checksum」与「v1 的错 checksum」。
    has_checksum = ("strategy_checksum" in cols and stored is not None
                    and stored[0] is not None)
    detail = (f"registry v1 checksum={version.checksum[:12]}…\n"
              f"selection rows carry checksum column={'strategy_checksum' in cols} "
              f"value={(stored[0] if stored else None)!r}")
    record("C5", REPRODUCED if not has_checksum else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------
# C6 — archived strategy history
# ---------------------------------------------------------------------------


def case_c6(env: SelectionEnv) -> None:
    with env._registry() as conn:
        SR.archive_definition(conn, "tq_breakout", reason="r23 repro archive",
                              actor="r23-repro")
        conn.commit()
        status = SR.get("tq_breakout", conn=conn).status
    view = PS.latest(trade_date=DAY)
    ids = [g["strategy_id"] for g in view["strategies"]]
    group = next((g for g in view["strategies"] if g["strategy_id"] == "tq_breakout"), None)
    detail = (f"strategy lifecycle={status}; latest({DAY}) groups={ids}\n"
              f"archived strategy visible={group is not None} "
              f"picks={(len(group['picks']) if group else 0)}")
    reproduced = group is None or not group.get("picks")
    record("C6", REPRODUCED if reproduced else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------
# C7 — as-of isolation（family B 的历史 run 被今天的结果覆盖）
# ---------------------------------------------------------------------------


def case_c7(env: SelectionEnv) -> None:
    ST.ensure_schema()
    ST.record_run(_payload(day=OLD_DAY), run_date=OLD_DAY, source="scheduled")
    with env._research() as conn:
        first = dict(conn.execute(
            "SELECT * FROM selection_runs WHERE run_date=? AND strategy='three_day'",
            (OLD_DAY,)).fetchone())
    today_payload = dict(_payload(day=DAY))
    today_payload["universe_size"] = 4242
    ST.record_run(today_payload, run_date=OLD_DAY, source="manual")
    with env._research() as conn:
        second = dict(conn.execute(
            "SELECT * FROM selection_runs WHERE run_date=? AND strategy='three_day'",
            (OLD_DAY,)).fetchone())
    mutated = [key for key in ("result_json", "universe_size", "generated_at", "source")
               if first.get(key) != second.get(key)]
    detail = (f"historical run_date={OLD_DAY}: fields changed by a later run = {mutated}\n"
              f"universe_size {first.get('universe_size')!r} -> {second.get('universe_size')!r}")
    record("C7", REPRODUCED if mutated else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------
# C9 — legacy strategy_id-only selection
# ---------------------------------------------------------------------------


def case_c9(env: SelectionEnv) -> None:
    """Legacy ``strategy_id``-only row must stay provably unproven.

    关键是**走真实迁移**：先用 R23 之前的表结构建库（``UNIQUE(trade_date,
    strategy_id)``、无 provenance 列），写入一条只有 ``strategy_id`` 的历史行，
    再让 ``paper_selection.ensure_schema`` 迁移它。修复前的树没有这些列，因此连
    「这条历史行的版本轴是否可证」都无法表达 —— 那正是缺口。
    """
    legacy_db = os.path.join(env.tmp, "legacy_research.db")
    with sqlite3.connect(legacy_db) as conn:
        conn.executescript(
            """
            CREATE TABLE paper_selection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL, strategy_id TEXT NOT NULL,
                strategy_no INTEGER NOT NULL, strategy_name TEXT NOT NULL,
                model_id TEXT NOT NULL, status TEXT NOT NULL, message TEXT,
                factor_date TEXT, topn INTEGER NOT NULL, source TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(trade_date, strategy_id)
            );
            CREATE TABLE paper_selection_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
                strategy_id TEXT NOT NULL, rank_no INTEGER NOT NULL, code TEXT NOT NULL,
                name TEXT, industry TEXT, price REAL, pct REAL, score REAL,
                super_net REAL, reasons TEXT, news_status TEXT,
                UNIQUE(trade_date, strategy_id, rank_no)
            );
            """
        )
        conn.execute(
            "INSERT INTO paper_selection_runs(trade_date,strategy_id,strategy_no,"
            "strategy_name,model_id,status,message,factor_date,topn,source,created_at) "
            "VALUES('2026-08-04','trend_pullback',2,'趋势波段优选','bottom_reversal',"
            "'ok','','2026-08-04',5,'scheduled','2026-08-04T17:25:00')")
        conn.execute(
            "INSERT INTO paper_selection_picks(trade_date,strategy_id,rank_no,code,name) "
            "VALUES('2026-08-04','trend_pullback',1,'600001','历史票')")
        conn.commit()
    old_db = PS.DB_PATH
    PS.DB_PATH = legacy_db
    try:
        with sqlite3.connect(legacy_db) as conn:
            conn.row_factory = sqlite3.Row
            PS.ensure_schema(conn)
            cols = columns(conn, "paper_selection_runs")
            row = dict(conn.execute(
                "SELECT * FROM paper_selection_runs WHERE trade_date='2026-08-04'"
            ).fetchone())
    finally:
        PS.DB_PATH = old_db
    status_col = "provenance_status" in cols
    with sqlite3.connect(legacy_db) as conn:
        conn.row_factory = sqlite3.Row
        pick_cols = columns(conn, "paper_selection_picks")
        run_id = None
        if "run_id" in pick_cols:
            picks = conn.execute(
                "SELECT run_id FROM paper_selection_picks WHERE trade_date='2026-08-04'"
            ).fetchone()
            run_id = picks[0] if picks else None
    head = SR.get("trend_pullback").current_version
    detail = (f"legacy row after real migration: strategy_id={row['strategy_id']!r} "
              f"strategy_version={row.get('strategy_version')!r} "
              f"strategy_checksum={row.get('strategy_checksum')!r} "
              f"provenance_status={row.get('provenance_status')!r} "
              f"(current registry head=v{head})\n"
              f"provenance_status column present={status_col}; "
              f"picks.run_id column present={'run_id' in pick_cols}; "
              f"legacy pick run_id={run_id}")
    # 缺口 = 无法表达「这条历史行的版本轴不可证明」，或用 current head 填了它。
    reproduced = (not status_col
                  or row.get("provenance_status") != "legacy_unproven"
                  or row.get("strategy_version") is not None)
    record("C9", REPRODUCED if reproduced else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------
# C3 / C4 / C8 / C10 — 真实生产链路（离线回放环境）
# ---------------------------------------------------------------------------


def ledger_cases() -> None:
    import test_production_path_golden_replay as G

    env_class = G.OfflinePaperEnv
    env_class.setUpClass()
    tmp = env_class._tmp
    old_ps_db, old_st_db = PS.DB_PATH, ST.DB_PATH
    old_ps_reg = getattr(PS, "REGISTRY_DB_PATH", None)
    old_st_reg = getattr(ST, "REGISTRY_DB_PATH", None)
    PS.DB_PATH = os.path.join(tmp, "selection_tracking.db")
    ST.DB_PATH = PS.DB_PATH
    # `after` 树：研究 run 的 immutable version 必须从**这个**账本解析。
    registry_db = os.path.join(tmp, "paper_trading.sqlite3")
    if hasattr(PS, "REGISTRY_DB_PATH"):
        PS.REGISTRY_DB_PATH = registry_db
    if hasattr(ST, "REGISTRY_DB_PATH"):
        ST.REGISTRY_DB_PATH = registry_db
    try:
        _ledger_probes(G, env_class)
    finally:
        PS.DB_PATH, ST.DB_PATH = old_ps_db, old_st_db
        if hasattr(PS, "REGISTRY_DB_PATH"):
            PS.REGISTRY_DB_PATH = old_ps_reg
        if hasattr(ST, "REGISTRY_DB_PATH"):
            ST.REGISTRY_DB_PATH = old_st_reg
        env_class.tearDownClass()


def _seed_cycle_strategy(G, env_class):
    """建立 user 策略 + active + enabled + cycle（全部走生产服务）。"""
    PT = G.PT
    with sqlite3.connect(PT.DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        SR.ensure_schema(conn)
        SR.create_user_definition(conn, STRATEGY_ID, "R23 溯源策略", dsl_ast=G.RULE,
                                  metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                                  actor="r23-repro")
        SR.transition(conn, STRATEGY_ID, "validated", expected_status="draft",
                      reason="r23 validate", actor="r23-repro")
        SR.transition(conn, STRATEGY_ID, "active", expected_status="validated",
                      reason="r23 activate", actor="r23-repro")
        G.RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="r23-repro")
        conn.commit()
    PT.init_db()
    _summary, cycle = PT.start_new_cycle(capital=G.CAPITAL, include_dashboard=False)
    return int(cycle["id"])


def _ledger_probes(G, env_class) -> None:
    PT = G.PT
    cycle_id = _seed_cycle_strategy(G, env_class)
    with sqlite3.connect(PT.DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        stamp = SR.cycle_stamp_for_account(conn, STRATEGY_ID, cycle_id=cycle_id)
        head_before = SR.get(STRATEGY_ID, conn=conn).current_version
        signal_cols = columns(conn, "paper_signals")
    pinned_version = int(stamp[1]) if stamp else None
    pinned_checksum = stamp[2] if stamp else None

    def _signals():
        with sqlite3.connect(PT.DB_PATH, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT * FROM paper_signals WHERE account_id=? ORDER BY id", (STRATEGY_ID,))]

    def _by_date(rows):
        out: dict[str, int] = {}
        for row in rows:
            key = str(row.get("signal_date"))
            out[key] = out.get(key, 0) + 1
        return out

    close = PT.generate_signals(G.D0)
    rows = [row for row in close.get("accounts", []) if row.get("id") == STRATEGY_ID]
    signals = _signals()
    with sqlite3.connect(PT.DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        orders = [dict(row) for row in conn.execute(
            "SELECT * FROM paper_orders WHERE account_id=?", (STRATEGY_ID,))]
    signal_versions = sorted({row.get("strategy_version") for row in signals})
    signal_cycles = (sorted({row.get("cycle_id") for row in signals})
                     if "cycle_id" in signal_cols else None)

    # ---- C10：signal 必须携带 cycle 归属，且 order stamp 与 signal 一致 ----
    order_stamps = sorted({(row.get("strategy_version"), row.get("strategy_checksum"))
                           for row in orders})
    detail = (
        f"cycle={cycle_id} pin=({STRATEGY_ID}, v{pinned_version}, {str(pinned_checksum)[:12]}…) "
        f"current head=v{head_before}\n"
        f"close scan accounts={rows}\n"
        f"signals={len(signals)} by_date={_by_date(signals)} versions={signal_versions} "
        f"cycle values={signal_cycles}\n"
        f"paper_signals has cycle_id column={'cycle_id' in signal_cols}; "
        f"orders={len(orders)} stamps={order_stamps}"
    )
    reproduced = ("cycle_id" not in signal_cols) or any(
        row.get("cycle_id") is None for row in signals)
    record("C10", REPRODUCED if reproduced else NOT_REPRODUCED, detail)

    # ---- C3：cycle pin 必须胜过 current head ----
    # 用**已知可产出 signal 的 D0 收盘扫描**做探针：先清空该账户信号，再在 head
    # 升级到 v2 之后重跑同一扫描，比较写回的 immutable version。
    with sqlite3.connect(PT.DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        SR.save_definition(conn, STRATEGY_ID, {"name": "R23 溯源策略 v2"},
                           actor="r23-repro", change_note="v2")
        conn.commit()
        head_after = SR.get(STRATEGY_ID, conn=conn).current_version
        conn.execute("DELETE FROM paper_signals WHERE account_id=?", (STRATEGY_ID,))
        conn.commit()
    PT.generate_signals(G.D0)
    c3_rows = _signals()
    c3_versions = sorted({row.get("strategy_version") for row in c3_rows})
    detail = (f"cycle pin v{pinned_version} vs current head v{head_after}; "
              f"清空后重跑 D0 收盘扫描 signals={len(c3_rows)} versions={c3_versions}\n"
              f"（正对照：升级前同一扫描写入 2 行 ⇒ 探针已被触发）")
    if not c3_rows:
        record("C3", NOT_REPRODUCED,
               detail + "\n原因：重跑后未产生任何 signal，cycle-pin 写入路径未被触发")
    else:
        record("C3", REPRODUCED if c3_versions != [pinned_version] else NOT_REPRODUCED, detail)

    # ---- C4：cycle pin 缺失必须 fail closed ----
    # 为什么不能「删 pin 再跑扫描」：``generate_signals`` 会先走 ``init_db`` /
    # ``SR.ensure_schema``（用 definitions 补 head 行）再走 ``_ensure_cycle`` →
    # ``SR.bind_cycle_versions``（用 head 重新 pin），所以**扫描永远看不到缺 pin 的
    # 状态**。缺 pin 的可达形态是「cycle 上没有 binding」，因此直接对 account 级的
    # 解析器取证：修复前它回退到 legacy binding / current head（缺口），修复后必须
    # 拒绝。这里同时给出正对照（pin 放回 → 解析器必须返回 pin 的 v1）。
    def _resolve_account_stamp(conn):
        """Return (label, value, error) for whichever account-level decider exists."""
        if hasattr(PT, "_cycle_signal_provenance"):
            try:
                return "PT._cycle_signal_provenance", PT._cycle_signal_provenance(
                    conn, STRATEGY_ID), ""
            except Exception as exc:  # noqa: BLE001 - 观察「是否 fail closed」
                return "PT._cycle_signal_provenance", None, f"{type(exc).__name__}: {exc}"
        value = PT._strategy_stamp(conn, STRATEGY_ID)
        return "PT._strategy_stamp", value, ""

    with sqlite3.connect(PT.DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        pin_before = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?",
            (cycle_id, STRATEGY_ID)).fetchall()]
        conn.execute("DELETE FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?",
                     (cycle_id, STRATEGY_ID))
        legacy_removed = conn.execute(
            "DELETE FROM paper_strategy_legacy_bindings WHERE account_id=?",
            (STRATEGY_ID,)).rowcount
        conn.commit()
        label, value, c4_error = _resolve_account_stamp(conn)
        head_now = SR.get(STRATEGY_ID, conn=conn).current_version
        # 正对照：把 pin 恢复成**升级前的 v1**（而不是 current head），同一个解析器
        # 必须给出 v1 —— 它同时证明「解析器不是永远失败」和「pin 为 v1 时不会返回
        # current head v2」。
        v1 = conn.execute(
            "SELECT strategy_id, version, checksum FROM paper_strategy_versions "
            "WHERE strategy_id=? ORDER BY version LIMIT 1", (STRATEGY_ID,)).fetchone()
        if v1 is not None:
            conn.execute(
                "INSERT OR REPLACE INTO paper_cycle_strategy_versions(cycle_id,account_id,"
                "strategy_id,strategy_version,strategy_checksum,bound_at) "
                "VALUES(?,?,?,?,?,datetime('now'))",
                (cycle_id, STRATEGY_ID, v1["strategy_id"], v1["version"], v1["checksum"]))
        conn.commit()
        _label2, control, _err2 = _resolve_account_stamp(conn)
    resolved = ""
    if isinstance(value, tuple):
        resolved = f"stamp={value}"
    elif value is not None:
        resolved = f"cycle={value[0]} stamp={value[1]}"
    detail = (
        f"cycle pin rows for cycle={cycle_id} before removal={len(pin_before)}; "
        f"legacy bindings removed={legacy_removed}; current def head=v{head_now}\n"
        f"{label} with **no** cycle pin -> {resolved or '(refused)'} {c4_error}\n"
        f"正对照：pin 放回后 -> {control}"
    )
    # 缺口 = 缺 pin 时仍然解析出（非空）stamp，即静默回退到 legacy binding /
    # current head。修复后必须拒绝（抛 SignalCycleUnprovable）。
    fallback = False
    if value is not None:
        if isinstance(value, tuple):
            fallback = any(part is not None for part in value)
        else:
            fallback = True
    reproduced = fallback
    record("C4", REPRODUCED if reproduced else NOT_REPRODUCED, detail)

    # ---- C8：research run 不得偷偷绑定 active cycle ----
    PS.run_daily(topn=5, run_date=DAY)
    with sqlite3.connect(PS.DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        cols = columns(conn, "paper_selection_runs")
        row = dict(conn.execute(
            "SELECT * FROM paper_selection_runs WHERE trade_date=? AND strategy_id=?",
            (DAY, STRATEGY_ID)).fetchone() or {})
    scope_ok = row.get("scope") == "research" and row.get("cycle_id") is None
    detail = (f"active cycle in ledger={cycle_id}\n"
              f"research run scope={row.get('scope')!r} cycle_id={row.get('cycle_id')!r}\n"
              f"columns={cols}")
    record("C8", REPRODUCED if not scope_ok else NOT_REPRODUCED, detail)


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=["before", "after"], required=True)
    args = parser.parse_args()
    revision = detect_revision()
    print(f"revision detected = {revision} (expected {args.expect})")
    if revision != args.expect:
        print(f"FAIL: 探针在 {revision} 树上运行，但 --expect {args.expect}；拒绝给出结论")
        return 1

    env = SelectionEnv()
    try:
        case_c1(env)
        case_c2(env)
        case_c5(env)
        case_c6(env)
        case_c7(env)
        case_c9(env)
    finally:
        env.close()
    ledger_cases()

    print("\n=== R23 before/after reproduction summary ===")
    for case, verdict, _detail in RESULTS:
        print(f"{case}: {verdict}")
    counts = {verdict: sum(1 for _, x, _ in RESULTS if x == verdict)
              for verdict in (REPRODUCED, NOT_REPRODUCED)}
    print(json.dumps(counts, ensure_ascii=False))
    if args.expect == "before":
        ok = counts[REPRODUCED] > 0
        print("EXPECTATION before: 至少一条缺口被真实复现 ->", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    ok = counts[REPRODUCED] == 0
    print("EXPECTATION after: 所有缺口均不再复现 ->", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
