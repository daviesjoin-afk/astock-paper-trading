# -*- coding: utf-8 -*-
"""???????????????????

????????????????????????????????????
??????????????????????????????300?????
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing

import data_fetcher as dfc
import strategy_registry as SR
import strategy_selection_provenance as SP
import strategy_selection_resolver as SRES

DB_PATH = os.path.join(dfc.CACHE_DIR, "selection_tracking.db")
BENCHMARK_CODE = "BENCH_000300"
BENCHMARK_NAME = "CSI 300"
HORIZONS = (1, 3, 5, 10, 20)
MAX_TRACKING_DAYS = 20
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
#: Immutable strategy versions live in the ledger registry, not in this research
#: DB. Explicit override first (tests / offline replay must set it — never guess a
#: path from "the newest .sqlite3").
REGISTRY_DB_PATH: str | None = None
# PR-8：``holding_days`` 记录的是"已成功记录的收盘观测次数"，不是经交易所日历
# 认证的交易日数。字段名保留（兼容旧 API / 旧库），但语义必须显式声明。
HOLDING_DAY_SEMANTICS = "recorded_close_observation_count"


def _today():
    return dt.datetime.now(CHINA_TZ).date()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _connect()
    try:
        # 重建父表前必须先关外键：``selection_picks.run_id`` 有 ON DELETE
        # CASCADE，重建途中删除父表会连带销毁全部 picks / observations
        # （前向验证样本不可再生）。PRAGMA 必须在任何 DML 之前生效。
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            _runs_ddl() + """;
            CREATE TABLE IF NOT EXISTS selection_picks (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES selection_runs(id) ON DELETE CASCADE,
                rank_no INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                entry_price REAL,
                score REAL,
                super_net REAL,
                decision_tier TEXT,
                decision_action TEXT,
                snapshot_json TEXT NOT NULL,
                UNIQUE(run_id, code)
            );
            CREATE TABLE IF NOT EXISTS selection_observations (
                id INTEGER PRIMARY KEY,
                pick_id INTEGER NOT NULL REFERENCES selection_picks(id) ON DELETE CASCADE,
                observed_date TEXT NOT NULL,
                price REAL NOT NULL,
                benchmark_price REAL,
                holding_days INTEGER NOT NULL,
                return_pct REAL,
                benchmark_return_pct REAL,
                excess_return_pct REAL,
                UNIQUE(pick_id, observed_date)
            );
            CREATE INDEX IF NOT EXISTS idx_selection_runs_strategy_date
                ON selection_runs(strategy, run_date DESC);
            CREATE INDEX IF NOT EXISTS idx_selection_observations_pick_date
                ON selection_observations(pick_id, observed_date DESC);
            """
        )
        _migrate_runs(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_provenance_guards(conn)
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_selection_runs_key
                ON selection_runs(run_date, strategy, provenance_key);
            """
        )
    finally:
        conn.close()


#: 升级前的 run 表结构，按**列名**拷贝（绝不按位置），列错位在整行拷贝里是灾难。
LEGACY_RUN_COLUMNS = (
    "id", "run_date", "generated_at", "strategy", "strategy_name", "data_asof_date",
    "benchmark_entry_price", "universe_size", "candidate_count", "selected_count",
    "executable_count", "source", "result_json",
)

#: 历史行（升级前只留下模型族 id）的身份前缀。
LEGACY_KEY_PREFIX = "legacy"


def _table_exists(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn, table) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _unique_index_columns(conn, table) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if not row[2]:
            continue
        found.add(tuple(str(info[2]) for info in
                        conn.execute(f"PRAGMA index_info({row[1]})")))
    return found


def _runs_ddl(table: str = "selection_runs") -> str:
    """Canonical run-table DDL: the fresh and rebuilt tables share one definition.

    Keeping a single definition is what makes the rebuild order safe - see
    ``_migrate_runs``.
    """
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY,
            run_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            strategy TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            data_asof_date TEXT,
            benchmark_entry_price REAL,
            universe_size INTEGER,
            candidate_count INTEGER,
            selected_count INTEGER,
            executable_count INTEGER,
            source TEXT NOT NULL,
            result_json TEXT NOT NULL,
            -- R23 run 级 provenance：strategy_* 在本系列诚实地保持 NULL
            -- （``strategy`` 是模型族 id，注册表里不存在），状态为
            -- ``not_applicable``；asof/scope 则必须固定下来。
            provenance_status TEXT,
            provenance_key TEXT,
            asof_day TEXT,
            scope TEXT,
            cycle_id INTEGER,
            strategy_id TEXT,
            strategy_version INTEGER,
            strategy_checksum TEXT,
            UNIQUE(run_date, strategy, provenance_key)
        )
    """


def _absorb_leftover_runs(conn, legacy: str, staged: str) -> None:
    """Recover from an interrupted rebuild instead of deleting its only copy.

    Two leftovers exist in the wild, and both used to be destroyed on sight:

    * ``selection_runs_legacy`` - an *interrupted pre-fix* migration renamed the
      parent and then died. If the parent is gone the rename is simply undone;
      if the parent survived, the leftover rows are folded back in (by ``id``)
      *before* the leftover is dropped, so recovery can never discard the only
      surviving copy of the research history.
    * ``selection_runs_new`` - the staged table of this implementation. When the
      parent is gone, the staged table **is** the history and is promoted back.

    Fold/promote are idempotent, so a crash in the middle of recovery is itself
    recoverable.
    """
    if _table_exists(conn, staged) and not _table_exists(conn, "selection_runs"):
        conn.execute(f"ALTER TABLE {staged} RENAME TO selection_runs")
    if not _table_exists(conn, legacy):
        return
    if not _table_exists(conn, "selection_runs"):
        conn.execute(f"ALTER TABLE {legacy} RENAME TO selection_runs")
        return
    parent_columns = _columns(conn, "selection_runs")
    present = [name for name in LEGACY_RUN_COLUMNS
               if name in _columns(conn, legacy) and name in parent_columns]
    if present:
        columns_sql = ", ".join(present)
        # A rebuilt parent already carries provenance columns: stamp the folded
        # rows as legacy-unproven so no row is ever left without a declared
        # state. A pre-R23 parent has no such columns, and the normal migration
        # stamps them a moment later - so only select what exists.
        if "provenance_key" in parent_columns:
            conn.execute(
                f"""INSERT OR IGNORE INTO selection_runs({columns_sql},
                        provenance_status, provenance_key, asof_day, scope, cycle_id,
                        strategy_id, strategy_version, strategy_checksum)
                    SELECT {columns_sql}, '{SP.STATUS_LEGACY_UNPROVEN}',
                           '{LEGACY_KEY_PREFIX}|' || run_date || '|' || strategy,
                           data_asof_date, '{SP.SCOPE_RESEARCH}', NULL, NULL, NULL, NULL
                    FROM {legacy}"""
            )
        else:
            conn.execute(
                f"INSERT OR IGNORE INTO selection_runs({columns_sql})"
                f" SELECT {columns_sql} FROM {legacy}"
            )
    conn.execute(f"DROP TABLE {legacy}")


def _migrate_runs(conn) -> None:
    """Rebuild a pre-R23 run table into the provenance-keyed shape (idempotent).

    迁移策略（规格 §9 / §8）：只**新增** NULL provenance 列并标
    ``provenance_status='legacy_unproven'``；唯一契约从 ``(run_date, strategy)``
    改为 ``(run_date, strategy, provenance_key)`` —— 约束本身是错的，只 ADD
    COLUMN 会让「同日同策略、不同 as-of」继续互相覆盖，所以必须重建。
    **绝不**用今天的 Registry / generation 时间回填历史 provenance。

    崩溃安全 = 两步合一：

    1. **绝不重命名被引用的父表**。``selection_picks.run_id`` 有
       ``REFERENCES selection_runs(id) ON DELETE CASCADE``，而 SQLite 在
       ``ALTER TABLE ... RENAME TO`` 时会把子表的 FK 目标一起改写：曾经
       ``RENAME selection_runs TO selection_runs_legacy`` → 删掉 legacy → 子表
       schema 就指向了一张不存在的表，此后任何 pick 插入都是
       ``no such table: main.selection_runs_legacy``。现在按 SQLite 官方顺序
       「建新表 → 拷贝 → 删旧表 → 改名」，父表名字在重建期间**始终可用**。
    2. **任何遗留副本都先回收再删**（``_absorb_leftover_runs``）：中断在
       「改名之后、拷贝之前」时，legacy 表是历史的唯一副本，绝不能无条件删。
    """
    legacy = "selection_runs_legacy"
    staged = "selection_runs_new"
    _absorb_leftover_runs(conn, legacy, staged)
    columns = _columns(conn, "selection_runs")
    unique = _unique_index_columns(conn, "selection_runs")
    if "provenance_key" in columns and ("run_date", "strategy", "provenance_key") in unique:
        return
    if not _table_exists(conn, "selection_runs"):
        conn.executescript(_runs_ddl())
        return
    present = [name for name in LEGACY_RUN_COLUMNS if name in columns]
    select_legacy = ", ".join(present)
    conn.executescript(_runs_ddl(staged))
    conn.execute(
        f"""INSERT OR IGNORE INTO {staged}({select_legacy},
                provenance_status, provenance_key, asof_day, scope, cycle_id,
                strategy_id, strategy_version, strategy_checksum)
            SELECT {select_legacy}, '{SP.STATUS_LEGACY_UNPROVEN}',
                   '{LEGACY_KEY_PREFIX}|' || run_date || '|' || strategy,
                   data_asof_date, '{SP.SCOPE_RESEARCH}', NULL, NULL, NULL, NULL
            FROM selection_runs"""
    )
    conn.execute("DROP TABLE selection_runs")
    conn.execute(f"ALTER TABLE {staged} RENAME TO selection_runs")


def _ensure_provenance_guards(conn) -> None:
    """Provenance 是 write-time、immutable 的事实（DDL 层兜底，规格 §9/§16）。

    * INSERT —— 拒绝未声明状态、拒绝「声称 verified 却没有 version/checksum」、
      拒绝 cycle scope 缺 cycle_id、拒绝 research scope 携带 cycle_id；
    * UPDATE —— 一经写入不得更改（``NULL -> 值`` 同样被阻止），否则任何 repair
      脚本都能把 legacy ``unknown`` 洗白成 ``verified``。
    """
    guards = {
        "trg_selection_runs_provenance_insert": f"""
            CREATE TRIGGER trg_selection_runs_provenance_insert
            BEFORE INSERT ON selection_runs
            WHEN (NEW.provenance_status IS NOT NULL
                  AND NEW.provenance_status NOT IN
                      ('{SP.STATUS_VERIFIED}','{SP.STATUS_UNKNOWN}',
                       '{SP.STATUS_LEGACY_UNPROVEN}','{SP.STATUS_NOT_APPLICABLE}'))
              OR (NEW.provenance_status = '{SP.STATUS_VERIFIED}' AND (
                     NEW.strategy_id IS NULL OR NEW.strategy_version IS NULL
                  OR NEW.strategy_checksum IS NULL OR NEW.asof_day IS NULL
                  OR NEW.scope IS NULL))
              OR (NEW.scope = '{SP.SCOPE_CYCLE}' AND NEW.cycle_id IS NULL)
              OR (NEW.scope = '{SP.SCOPE_RESEARCH}' AND NEW.cycle_id IS NOT NULL)
            BEGIN SELECT RAISE(ABORT, 'invalid selection run provenance'); END
        """,
        "trg_selection_runs_provenance_immutable": """
            CREATE TRIGGER trg_selection_runs_provenance_immutable
            BEFORE UPDATE OF provenance_status,provenance_key,asof_day,scope,cycle_id,
                             strategy_id,strategy_version,strategy_checksum
            ON selection_runs
            WHEN NEW.provenance_status IS NOT OLD.provenance_status
              OR NEW.provenance_key IS NOT OLD.provenance_key
              OR NEW.asof_day IS NOT OLD.asof_day
              OR NEW.scope IS NOT OLD.scope
              OR NEW.cycle_id IS NOT OLD.cycle_id
              OR NEW.strategy_id IS NOT OLD.strategy_id
              OR NEW.strategy_version IS NOT OLD.strategy_version
              OR NEW.strategy_checksum IS NOT OLD.strategy_checksum
            BEGIN SELECT RAISE(ABORT, 'selection run provenance is immutable'); END
        """,
    }
    for name, ddl in guards.items():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(ddl)


def _number(value):
    return float(value) if isinstance(value, (int, float)) else None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _latest_signal_date(picks):
    dates = []
    for pick in picks:
        frame = dfc.load_shared_kline(pick.get("code"))
        if frame is not None and not frame.empty:
            dates.append(frame.index[-1].date().isoformat())
    return min(dates) if dates else None


def _benchmark_price_on_or_before(day):
    frame = dfc.load_shared_kline(BENCHMARK_CODE)
    if frame is None or frame.empty:
        return None
    rows = frame.loc[frame.index.date <= day]
    if rows.empty:
        return None
    value = rows.iloc[-1].get("close")
    return _number(value)


def refresh_benchmark():
    """Incrementally refresh the sole benchmark bar; never scans the whole universe."""
    cached = dfc.load_shared_kline(BENCHMARK_CODE)
    beg = "20230101"
    if cached is not None and not cached.empty:
        beg = cached.index[-1].date().strftime("%Y%m%d")
    try:
        latest = dfc.fetch_kline(None, beg=beg, secid="1.000300")
    except Exception:
        return False
    if latest is None or latest.empty:
        return False
    if cached is not None and not cached.empty:
        import pandas as pd
        latest = pd.concat([cached[cached.index < latest.index[0]], latest])
    dfc.save_kline(BENCHMARK_CODE, latest)
    dfc.flush_kline_manifest()
    return True


def record_run(result, run_date=None, source="scheduled", *, asof_day=None):
    """Persist one immutable candidate snapshot per strategy and trading date.

    Provenance（R23）：

    * run 的**身份**是 ``(run_date, strategy, provenance_key)``。同一个
      provenance key 的重试是幂等覆盖；不同的 as-of / 不同 scope 是**另一份
      证据**，绝不互相覆盖；
    * 本系列的 ``strategy`` 是**模型族 id**（``three_day`` / ``five_day`` /
      ``ten_day`` …），注册表里不存在这些 id，所以策略版本轴诚实地记为
      ``provenance_status='not_applicable'``、``strategy_*`` 保持 NULL ——
      **绝不**为了字段齐整而编造一个 strategy_id；
    * ``asof_day`` 由 caller 显式传入，或从结果里唯一可证明的因子日解析；无法唯一
      确定时 ``provenance_status='unknown'``，**不**退回 today / 最新行情日；
    * ``scope`` 永远是 ``research``、``cycle_id`` 永远是 NULL —— 研究 run 不属于
      任何 paper cycle，本模块从不解析 active cycle。

    历史 ``run_date`` 的不可变性在**写入之前**判定：过去的快照不接受任何改写
    （对它的 upsert 会删除既有 picks，FK ``ON DELETE CASCADE`` 进而销毁全部
    observations，``holding_days`` 等前向验证指标将被永久破坏）。
    """
    ensure_schema()
    strategy = str(result.get("strategy") or "")
    if not strategy:
        raise ValueError("missing strategy id")
    day = run_date or _today().isoformat()
    picks = list(result.get("picks") or [])
    data_asof = _latest_signal_date(picks)
    bench_entry = _benchmark_price_on_or_before(dt.date.fromisoformat(day))
    generated_at = dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds")
    safe_result = _json_safe(result)
    provenance = _run_provenance(result, strategy, asof_day=asof_day,
                                 data_asof=data_asof)
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM selection_runs WHERE run_date=? AND strategy=?",
            (day, strategy),
        ).fetchone()
        if existing is not None and str(day) < _today().isoformat():
            # 历史日期的快照不可变 —— 必须在任何写之前收敛，否则就是
            # "先覆盖、再声明跳过"（R23 之前正是这个顺序：upsert 已经改了行，
            # 才发现日期是过去，于是返回 skipped，而证据已经变了）。
            return {
                "status": "skipped", "reason": "historical run_date is immutable",
                "run_date": day, "strategy": strategy, "run_id": existing["id"],
                "provenance_status": provenance["provenance_status"],
                "provenance_key": provenance["provenance_key"],
            }
        conn.execute(
            """
            INSERT INTO selection_runs(
                run_date, generated_at, strategy, strategy_name, data_asof_date,
                benchmark_entry_price, universe_size, candidate_count, selected_count,
                executable_count, source, result_json,
                provenance_status, provenance_key, asof_day, scope, cycle_id,
                strategy_id, strategy_version, strategy_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_date, strategy, provenance_key) DO UPDATE SET
                generated_at=excluded.generated_at,
                strategy_name=excluded.strategy_name,
                data_asof_date=excluded.data_asof_date,
                benchmark_entry_price=excluded.benchmark_entry_price,
                universe_size=excluded.universe_size,
                candidate_count=excluded.candidate_count,
                selected_count=excluded.selected_count,
                executable_count=excluded.executable_count,
                source=excluded.source,
                result_json=excluded.result_json
            """,
            (
                day, generated_at, strategy, str(result.get("strategy_name") or strategy),
                data_asof, bench_entry, result.get("universe_size"),
                result.get("candidate_count"), len(picks), result.get("executable_count"),
                source, json.dumps(safe_result, ensure_ascii=False, separators=(",", ":")),
                provenance["provenance_status"], provenance["provenance_key"],
                provenance["asof_day"], SP.SCOPE_RESEARCH, None,
                provenance["strategy_id"], provenance["strategy_version"],
                provenance["strategy_checksum"],
            ),
        )
        run = conn.execute(
            "SELECT id FROM selection_runs WHERE run_date=? AND strategy=? AND "
            "provenance_key IS ?", (day, strategy, provenance["provenance_key"]),
        ).fetchone()
        conn.execute("DELETE FROM selection_picks WHERE run_id=?", (run["id"],))
        for rank, pick in enumerate(picks, 1):
            decision = pick.get("buy_decision") or {}
            conn.execute(
                """
                INSERT INTO selection_picks(
                    run_id, rank_no, code, name, industry, entry_price, score,
                    super_net, decision_tier, decision_action, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"], rank, str(pick.get("code") or ""), pick.get("name"),
                    pick.get("industry"), _number(pick.get("price")), _number(pick.get("score")),
                    _number(pick.get("super_net")), decision.get("tier"), decision.get("action"),
                    json.dumps(_json_safe(pick), ensure_ascii=False, separators=(",", ":")),
                ),
            )
    return {"date": day, "strategy": strategy, "saved": len(picks), "data_asof_date": data_asof}


def _run_provenance(result, strategy, *, asof_day=None, data_asof=None) -> dict:
    """Pin one research run's as-of and record the honest strategy-axis verdict.

    ``strategy`` here is a **model family**, not a registered strategy id, so the
    version axis is ``not_applicable`` and ``strategy_*`` stays NULL. The
    question "which immutable version produced this?" therefore has a truthful
    answer: "this run is not strategy-versioned" — rather than a fabricated id.

    If a *registered* strategy id is ever passed (a future caller wiring family B
    to the registry), the version is resolved once through the shared resolver as
    a research pin — never by re-implementing version lookup here.
    """
    declared = SP.declared_asof_candidates(result)
    if not any(label == "data_quality.complete_cutoff" for label, _ in declared) and data_asof:
        # ``data_asof_date`` 是本模块自己推导的因子截止日：它同样是「决策所基于
        # 的因子快照」的候选，且与 pick 上的逐条日期互相印证。
        declared = list(declared) + [("data_asof", data_asof)]
    try:
        asof = SP.resolve_asof_day(asof_day, declared)
    except SP.AsOfUnprovable as exc:
        return {
            "provenance_status": SP.STATUS_UNKNOWN,
            "provenance_key": SP.run_provenance_key(
                scope=SP.SCOPE_RESEARCH, subject=str(strategy), asof_day=None,
            ),
            "asof_day": None,
            "strategy_id": None,
            "strategy_version": None,
            "strategy_checksum": None,
            "provenance_detail": str(exc),
        }
    registered = _registered_strategy_ids()
    if str(strategy) in registered:
        with closing(_registry_conn()) as registry:
            reading = SRES.research_provenance(registry, str(strategy), asof_day=asof)
        provenance = reading.provenance
        return {
            "provenance_status": reading.status,
            "provenance_key": SP.run_provenance_key(
                scope=SP.SCOPE_RESEARCH, subject=str(strategy), asof_day=asof,
                strategy_version=(provenance.strategy_version if provenance else None),
                strategy_checksum=(provenance.strategy_checksum if provenance else None),
            ),
            "asof_day": asof,
            "strategy_id": str(strategy),
            "strategy_version": provenance.strategy_version if provenance else None,
            "strategy_checksum": provenance.strategy_checksum if provenance else None,
            "provenance_detail": reading.detail,
        }
    return {
        "provenance_status": SP.STATUS_NOT_APPLICABLE,
        "provenance_key": SP.run_provenance_key(
            scope=SP.SCOPE_RESEARCH, subject=str(strategy), asof_day=asof,
            strategy_version=None, strategy_checksum=None,
        ),
        "asof_day": asof,
        "strategy_id": None,
        "strategy_version": None,
        "strategy_checksum": None,
        "provenance_detail": (
            f"{strategy!r} is a model family, not a registered strategy id"
        ),
    }


def _registered_strategy_ids() -> frozenset[str]:
    """Current registry ids — used *only* to decide applicability, never to fill."""
    try:
        return frozenset(SR.active_ids(db_path=_registry_db_path())) | frozenset(
            spec.id for spec in SR.list_definitions(db_path=_registry_db_path()))
    except Exception:
        return frozenset()


def _registry_db_path() -> str:
    """Immutable versions live in the ledger registry, not the research DB."""
    if REGISTRY_DB_PATH:
        return str(REGISTRY_DB_PATH)
    module = sys.modules.get("paper_trading")
    path = getattr(module, "DB_PATH", None) if module is not None else None
    if path:
        return str(path)
    import data_paths
    return data_paths.data_path("paper_trading.sqlite3")


def _registry_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_registry_db_path()}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _authority_snapshot_rows():
    """经 Market Data Authority 取全市场事实。

    这是一个**收盘后的 tracking job**（非浏览器只读路径），原有实现就是
    ``fetch_market_snapshot()``（缓存未命中时联网），因此这里保留显式刷新
    语义以维持行为 parity；只是把取数、完整性校验与新鲜度判定统一交给
    authority，而不是自己直连 provider。
    """
    try:
        import market_data_service as MDSvc
        return MDSvc.refresh_rows()
    except Exception:
        return []


def _latest_snapshot_prices():
    # R24：只读路径经 Market Data Authority（此前直接调 fetch_market_snapshot，
    # 在缓存过期时会同步穿透 provider，并绕过 authority 的新鲜度判定）。
    rows = _authority_snapshot_rows()
    by_code = {}
    dates = []
    for row in rows:
        price = _number(row.get("price"))
        quote_at = row.get("quote_at") or ""
        quote_day = str(quote_at)[:10]
        if price is not None and price > 0 and len(quote_day) == 10:
            by_code[str(row.get("code") or "")] = price
            dates.append(quote_day)
    if not dates:
        return None, {}
    # A verified quote date prevents holiday/after-hours stale snapshots being
    # recorded as a fictitious trading day.
    return max(dates), by_code


def update_observations():
    """Append one end-of-day market observation for active candidate snapshots."""
    ensure_schema()
    today = _today().isoformat()
    quote_day, prices = _latest_snapshot_prices()
    if quote_day != today:
        return {"status": "skipped", "reason": "market quote is not a same-day close", "quote_date": quote_day}
    refresh_benchmark()
    benchmark = _benchmark_price_on_or_before(dt.date.fromisoformat(today))
    inserted = 0
    with _connect() as conn:
        active = conn.execute(
            """
            SELECT p.id, p.code, p.entry_price, r.run_date, r.benchmark_entry_price
            FROM selection_picks p JOIN selection_runs r ON r.id=p.run_id
            WHERE r.run_date < ? AND r.run_date >= ?
            """,
            (today, (dt.date.fromisoformat(today) - dt.timedelta(days=45)).isoformat()),
        ).fetchall()
        for row in active:
            price = prices.get(row["code"])
            entry = _number(row["entry_price"])
            if price is None or entry is None or entry <= 0:
                continue
            prior_days = conn.execute(
                "SELECT COUNT(*) AS c FROM selection_observations WHERE pick_id=?",
                (row["id"],),
            ).fetchone()["c"]
            if prior_days >= MAX_TRACKING_DAYS:
                continue
            ret = (price / entry - 1) * 100
            bench_entry = _number(row["benchmark_entry_price"])
            bench_ret = ((benchmark / bench_entry - 1) * 100) if benchmark and bench_entry else None
            excess = ret - bench_ret if bench_ret is not None else None
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO selection_observations(
                    pick_id, observed_date, price, benchmark_price, holding_days,
                    return_pct, benchmark_return_pct, excess_return_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], today, price, benchmark, prior_days + 1, ret, bench_ret, excess),
            )
            inserted += int(cur.rowcount or 0)
    return {"status": "ok", "date": today, "observed": inserted, "benchmark": benchmark}


def _assessment(samples, horizon):
    if len(samples) < 20:
        return {"state": "accumulating", "label": "Accumulating", "advice": f"Only {horizon}-day evidence is available; parameters remain unchanged."}
    avg_excess = sum(x["excess_return_pct"] for x in samples if x["excess_return_pct"] is not None)
    excess_n = sum(1 for x in samples if x["excess_return_pct"] is not None)
    avg_excess = avg_excess / excess_n if excess_n else None
    win_rate = sum(1 for x in samples if x["return_pct"] > 0) / len(samples) * 100
    if avg_excess is not None and avg_excess <= -2 and win_rate < 45:
        return {"state": "review", "label": "Review needed", "advice": "Sustained underperformance: review out-of-sample and sector splits before any change."}
    if avg_excess is not None and avg_excess > 0 and win_rate >= 50:
        return {"state": "validated", "label": "Keep observing", "advice": "Positive excess return needs evidence across more market regimes."}
    return {"state": "watch", "label": "Keep observing", "advice": "No stable edge yet; preserve rules and continue collecting evidence."}


def _metric_rows(conn, strategy):
    where = "" if not strategy else "WHERE r.strategy=?"
    args = () if not strategy else (strategy,)
    rows = conn.execute(
        f"""
        SELECT p.id AS pick_id, r.strategy, r.strategy_name, r.run_date, p.rank_no,
               p.code, p.name, p.industry, p.entry_price, p.decision_tier,
               o.observed_date, o.price, o.holding_days, o.return_pct,
               o.benchmark_return_pct, o.excess_return_pct
        FROM selection_picks p
        JOIN selection_runs r ON r.id=p.run_id
        LEFT JOIN selection_observations o ON o.pick_id=p.id
        {where}
        ORDER BY r.run_date DESC, p.rank_no, o.holding_days
        """,
        args,
    ).fetchall()
    grouped = defaultdict(list)
    pick_meta = {}
    for row in rows:
        item = dict(row)
        pick_meta[item["pick_id"]] = item
        if item["observed_date"]:
            grouped[item["pick_id"]].append(item)
    return pick_meta, grouped


def _duplicate_picks(conn, max_days=60):
    """按信号日聚合各参与策略的重复入选股票。"""
    rows = conn.execute(
        """
        SELECT p.id AS pick_id, r.run_date, r.strategy, r.strategy_name,
               p.rank_no, p.code, p.name, p.industry, p.entry_price,
               p.decision_tier
        FROM selection_picks p
        JOIN selection_runs r ON r.id=p.run_id
        WHERE r.run_date >= date('now', ?)
        ORDER BY r.run_date DESC, p.code, p.rank_no
        """,
        (f"-{max(1, int(max_days))} day",),
    ).fetchall()
    groups = defaultdict(list)
    for row in rows:
        item = dict(row)
        if item.get("code"):
            groups[(item["run_date"], item["code"])].append(item)
    # 读取最新跟踪点，避免重复项只有静态排名没有后续表现。
    latest = {}
    for row in conn.execute(
        """SELECT pick_id, observed_date, price, holding_days, return_pct, excess_return_pct
           FROM selection_observations ORDER BY pick_id, holding_days DESC"""
    ).fetchall():
        latest.setdefault(row["pick_id"], dict(row))
    duplicates = []
    for (run_date, code), items in groups.items():
        by_strategy = {}
        for item in items:
            by_strategy.setdefault(item["strategy"], item)
        if len(by_strategy) < 2:
            continue
        strategy_items = []
        returns = []
        excess = []
        for item in sorted(by_strategy.values(), key=lambda x: x["rank_no"]):
            point = latest.get(item["pick_id"])
            strategy_items.append({
                "strategy": item["strategy"],
                "strategy_name": item["strategy_name"],
                "rank": item["rank_no"],
                "entry_price": item["entry_price"],
                "decision_tier": item["decision_tier"],
            })
            if point and point.get("return_pct") is not None:
                returns.append(point["return_pct"])
            if point and point.get("excess_return_pct") is not None:
                excess.append(point["excess_return_pct"])
        first = items[0]
        duplicates.append({
            "run_date": run_date,
            "code": code,
            "name": first.get("name"),
            "industry": first.get("industry"),
            "strategy_count": len(strategy_items),
            "strategies": strategy_items,
            "latest_return_pct": sum(returns) / len(returns) if returns else None,
            "latest_excess_return_pct": sum(excess) / len(excess) if excess else None,
        })
    duplicates.sort(key=lambda x: (x["run_date"], -x["strategy_count"], x["code"]), reverse=True)
    return duplicates


def dashboard(strategy="", limit=30):
    """Return compact, auditable forward-performance evidence for the UI."""
    ensure_schema()
    with _connect() as conn:
        run_where = "" if not strategy else "WHERE strategy=?"
        run_args = () if not strategy else (strategy,)
        runs = [dict(row) for row in conn.execute(
            f"""SELECT run_date, generated_at, strategy, strategy_name, data_asof_date,
                       selected_count, candidate_count, executable_count, source
                FROM selection_runs {run_where}
                ORDER BY run_date DESC, strategy LIMIT ?""",
            (*run_args, max(1, min(int(limit), 180))),
        ).fetchall()]
        pick_meta, grouped = _metric_rows(conn, strategy)
        duplicate_picks = _duplicate_picks(conn)
    strategy_ids = sorted({item["strategy"] for item in pick_meta.values()} | {r["strategy"] for r in runs})
    strategies = []
    for sid in strategy_ids:
        own_ids = [pid for pid, item in pick_meta.items() if item["strategy"] == sid]
        horizons = []
        for horizon in HORIZONS:
            samples = []
            for pid in own_ids:
                point = next((x for x in grouped.get(pid, []) if x["holding_days"] >= horizon), None)
                if point:
                    samples.append(point)
            avg_return = sum(x["return_pct"] for x in samples) / len(samples) if samples else None
            excess_values = [x["excess_return_pct"] for x in samples if x["excess_return_pct"] is not None]
            avg_excess = sum(excess_values) / len(excess_values) if excess_values else None
            win_rate = sum(1 for x in samples if x["return_pct"] > 0) / len(samples) * 100 if samples else None
            assessment = _assessment(samples, horizon)
            horizons.append({
                "horizon": horizon, "sample_count": len(samples),
                "avg_return_pct": avg_return, "avg_excess_pct": avg_excess,
                "win_rate_pct": win_rate, "assessment": assessment,
            })
        latest_meta = next((item for item in pick_meta.values() if item["strategy"] == sid), {})
        primary = next((x for x in horizons if x["horizon"] == 10), horizons[-1])
        strategies.append({
            "strategy": sid, "strategy_name": latest_meta.get("strategy_name") or sid,
            "pick_count": len(own_ids), "metrics": horizons,
            "assessment": primary["assessment"],
        })
    latest_picks = []
    for pid, meta in pick_meta.items():
        points = grouped.get(pid, [])
        latest = points[-1] if points else None
        latest_picks.append({
            "run_date": meta["run_date"], "strategy": meta["strategy"],
            "strategy_name": meta["strategy_name"], "rank": meta["rank_no"],
            "code": meta["code"], "name": meta["name"], "industry": meta["industry"],
            "entry_price": meta["entry_price"], "decision_tier": meta["decision_tier"],
            "observed_date": latest.get("observed_date") if latest else None,
            "holding_days": latest.get("holding_days") if latest else 0,
            "price": latest.get("price") if latest else None,
            "return_pct": latest.get("return_pct") if latest else None,
            "excess_return_pct": latest.get("excess_return_pct") if latest else None,
        })
    latest_picks.sort(key=lambda x: (x["run_date"], -x["rank"]), reverse=True)
    manifest = dfc.get_kline_manifest()
    dates = [str(item.get("last_date"))[:10] for item in manifest.values() if item.get("last_date")]
    # 15:05 前的当日线可能仍是半根K线；展示给验证页的“完整日”必须回退。
    cutoff = dt.datetime.now(CHINA_TZ).date()
    if dt.datetime.now(CHINA_TZ).time() < dt.time(15, 5):
        cutoff -= dt.timedelta(days=1)
    complete_dates = [value for value in dates if value <= cutoff.isoformat()]
    return {
        "generated_at": dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
        "tracking_days": MAX_TRACKING_DAYS,
        "benchmark": BENCHMARK_NAME,
        # PR-8：观测语义显式对齐，避免把观测次数读成"交易日"。
        "holding_day_semantics": HOLDING_DAY_SEMANTICS,
        "kline_source": "与模拟盘共享：data_cache/klines（前复权日线）",
        "kline_source_version": getattr(dfc, "SHARED_KLINE_SOURCE_VERSION", "unknown"),
        "kline_manifest_updated_at": (max((str(item.get("updated_at")) for item in manifest.values() if item.get("updated_at")), default=None)),
        "kline_latest_complete_date": max(complete_dates) if complete_dates else None,
        "runs": runs,
        "strategies": strategies,
        "latest_picks": latest_picks[:100],
        "duplicate_picks": duplicate_picks[:300],
        "note": "Returns start at the saved signal close and only verified post-close quotes are recorded. Rules are never auto-mutated from a small sample.",
    }
