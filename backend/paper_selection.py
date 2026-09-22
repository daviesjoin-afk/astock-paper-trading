# -*- coding: utf-8 -*-
"""「策略选股」：模拟盘 5 套策略的盘后自动选股与持久化。

设计要点（与需求一一对应）
--------------------------
1. **策略来源**：完全取自模拟盘账户注册表 ``strategy_registry``（5 套），
   与「策略模拟」页同源；每个策略带编号（策略1…策略5）与名称。
2. **评分选股**：复用既有选股因子链 ``main._select_uncached``（同一套
   覆盖率/行情新鲜度/买卖范围门禁），每个策略按其模型族评分取 Top N
   （默认 5，候选不足则少于 5 只并给出空状态）。
3. **分组归属**：结果按策略分组存储，run 的唯一键为
   ``(trade_date, strategy_id, provenance_key)``。同一只股票被多个策略同时
   选中时**分别归属各策略、不去重、不合并**。
4. **调度**：交易日盘后由 ``paper_selection_runner.py`` 触发（cron），
   重跑在**同一个 provenance key** 上覆盖（DELETE 后 INSERT，同一事务）；
   immutable version 不同则是**另一份证据**，绝不互相覆盖。

本模块只做研究选股，不生成订单、不改动模拟盘账本。因此它产出的 run 永远是
``scope='research'``、``cycle_id IS NULL`` —— 这表示「该 run 不属于任何 paper
cycle」，**不是**「不知道周期，所以猜一个」（规格 §C8）。本模块从不读取
active cycle、也从不从 ``paper_accounts.cycle_id`` 推断周期。

Provenance（R23）
-----------------
每个 run 在**创建的那一刻**通过 :mod:`strategy_selection_resolver` pin 一次
immutable ``(strategy_id, version, checksum)``，并经
:func:`strategy_selection_provenance.resolve_asof_day` 解析一次 ``asof_day``，
然后持久化。同一 run 的 picks 通过 ``run_id`` 引用 run，**不**各自重复解析版本
（规格 §24：避免 N picks → N Registry queries）。

历史读取只读 persisted stamp：:func:`latest` 不再用**当前** Registry 决定历史
可见性，也不再用当前名称解释历史行（规格 §15）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from contextlib import closing

import data_fetcher as dfc
import strategy_registry as SR
import strategy_selection_provenance as SP
import strategy_selection_resolver as SRES

DB_PATH = os.path.join(dfc.CACHE_DIR, "selection_tracking.db")
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_TOPN = 5
MAX_TOPN = 20

#: Immutable strategy versions live in the **ledger** database. 研究库
#: （``selection_tracking.db``）从不保存策略版本，因此 provenance 解析必须指向
#: 主账本 registry —— 这是本模块唯一读账本的地方，且只读。
#:
#: 显式配置优先（测试 / 离线回放必须自己指定，绝不猜"最近的那个 .sqlite3"）。
REGISTRY_DB_PATH: str | None = None


def _registry_db_path() -> str:
    """Resolve the ledger DB explicitly: override → paper_trading → data_paths."""
    if REGISTRY_DB_PATH:
        return str(REGISTRY_DB_PATH)
    module = sys.modules.get("paper_trading")
    path = getattr(module, "DB_PATH", None) if module is not None else None
    if path:
        return str(path)
    import data_paths
    return data_paths.data_path("paper_trading.sqlite3")


def _registry_conn() -> sqlite3.Connection:
    """Read-only connection to the immutable-version registry."""
    conn = sqlite3.connect(f"file:{_registry_db_path()}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

# 模拟盘账户 → 选股模型族（strategies.run_strategy 的策略 id）。
# reported_profit_breakout / main_force_top10 使用各自复合评分实现，
# 因此直接映射到同名策略 id。
#
# 注意方向：这是「注册表策略 → 模型族」，**不是**策略身份的反向映射。一个模型族
# 可以被多个策略（或完全没有策略）使用，所以绝不能用它反推 strategy_id —— 那会
# 造出不存在的 provenance。
STRATEGY_MODEL = {
    "tq_breakout": "one_to_two",
    "trend_pullback": "bottom_reversal",
    "sector_rotation": "sentiment_pioneer",
    "reported_profit_breakout": "reported_profit_breakout",
    "main_force_top10": "main_force_top10",
}

#: R23 之前就存在的列。迁移按**列名**拷贝，绝不按位置。
LEGACY_RUN_COLUMNS = (
    "id", "trade_date", "strategy_id", "strategy_no", "strategy_name", "model_id",
    "status", "message", "factor_date", "topn", "source", "created_at",
)

#: picks 表里 R23 之前就存在的列。
LEGACY_PICK_COLUMNS = (
    "id", "trade_date", "strategy_id", "rank_no", "code", "name", "industry",
    "price", "pct", "score", "super_net", "reasons", "news_status",
)

#: 历史行（升级前只留下 strategy_id）的身份前缀：可证明、可幂等，且**不含**任何
#: version / checksum 猜测。
LEGACY_KEY_PREFIX = "legacy"


def _today():
    return dt.datetime.now(CHINA_TZ).date()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _unique_index_columns(conn, table) -> set[tuple[str, ...]]:
    """Return every UNIQUE index's column tuple on ``table`` (read-only)."""
    found: set[tuple[str, ...]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if not row[2]:
            continue
        found.add(tuple(str(info[2]) for info in
                        conn.execute(f"PRAGMA index_info({row[1]})")))
    return found


def _table_exists(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _runs_ddl(table: str = "paper_selection_runs") -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_no INTEGER NOT NULL,
            strategy_name TEXT NOT NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL,               -- ok|empty|blocked|error
            message TEXT,
            factor_date TEXT,
            topn INTEGER NOT NULL,
            source TEXT NOT NULL,               -- scheduled|manual
            created_at TEXT NOT NULL,
            -- R23 run 级 provenance：不可变、可为 NULL（legacy 行）
            strategy_version INTEGER,
            strategy_checksum TEXT,
            asof_day TEXT,
            scope TEXT,
            cycle_id INTEGER,
            provenance_status TEXT,
            provenance_key TEXT,
            UNIQUE(trade_date, strategy_id, provenance_key)
        )
    """


def _picks_ddl(table: str = "paper_selection_picks") -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            trade_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            rank_no INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            industry TEXT,
            price REAL,
            pct REAL,
            score REAL,
            super_net REAL,
            reasons TEXT,
            news_status TEXT,
            UNIQUE(run_id, rank_no)
        )
    """


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the run/pick schema and migrate any pre-R23 shape in place.

    Migration policy（规格 §9 / §8）：

    * 只**新增** NULL provenance 列；历史行标记 ``legacy_unproven``，绝不用今天的
      Registry 回填 version / checksum / asof；
    * 唯一契约从 ``(trade_date, strategy_id)`` 改为
      ``(trade_date, strategy_id, provenance_key)`` —— 约束本身是错的，只 ADD COLUMN
      会让「同日同策略、不同 immutable version」继续互相覆盖，因此必须重建表；
    * picks 通过 ``run_id`` 归属 run，``(run_id, rank_no)`` 才是新唯一键。历史
      picks 的 ``run_id`` 由同 ``(trade_date, strategy_id)`` 的 legacy run 唯一确定：
      这是**可证明的结构性归属**（picks 只有一种可能的父 run），不是 provenance
      回填。
    """
    conn.executescript(_runs_ddl())
    conn.executescript(_picks_ddl())
    _migrate_runs(conn)
    _migrate_picks(conn)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_paper_selection_picks_day
            ON paper_selection_picks(trade_date, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_paper_selection_picks_run
            ON paper_selection_picks(run_id, rank_no);
        CREATE INDEX IF NOT EXISTS idx_paper_selection_runs_key
            ON paper_selection_runs(trade_date, strategy_id, provenance_key);
        """
    )
    _ensure_provenance_guards(conn)


def _ensure_provenance_guards(conn) -> None:
    """Make a run's provenance a write-time, immutable fact (DDL-level).

    Python-level discipline is not enough here: the whole point of R23 is that a
    later strategy edit can never reinterpret an earlier selection, so the
    database refuses it too (规格 §9 / §16).

    * INSERT —— 拒绝「声称 verified 但字段不全」、「cycle scope 没有 cycle_id」、
      「research scope 带了 cycle_id」、「未声明的 status」；
    * UPDATE —— 一经写入不得更改（``NULL -> 值`` 同样被阻止），否则任何 repair
      脚本都能把 legacy ``unknown`` 洗白成 ``verified``；
    * picks —— 必须归属一个 run（``run_id`` 非空），否则 pick 会再次变成"按
      (trade_date, strategy_id) 猜归属"。
    """
    guards = {
        "trg_paper_selection_runs_provenance_insert": f"""
            CREATE TRIGGER trg_paper_selection_runs_provenance_insert
            BEFORE INSERT ON paper_selection_runs
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
            BEGIN SELECT RAISE(ABORT, 'invalid selection provenance'); END
        """,
        "trg_paper_selection_runs_provenance_immutable": """
            CREATE TRIGGER trg_paper_selection_runs_provenance_immutable
            BEFORE UPDATE OF strategy_id,strategy_version,strategy_checksum,asof_day,
                             scope,cycle_id,provenance_status,provenance_key
            ON paper_selection_runs
            WHEN NEW.strategy_id IS NOT OLD.strategy_id
              OR NEW.strategy_version IS NOT OLD.strategy_version
              OR NEW.strategy_checksum IS NOT OLD.strategy_checksum
              OR NEW.asof_day IS NOT OLD.asof_day
              OR NEW.scope IS NOT OLD.scope
              OR NEW.cycle_id IS NOT OLD.cycle_id
              OR NEW.provenance_status IS NOT OLD.provenance_status
              OR NEW.provenance_key IS NOT OLD.provenance_key
            BEGIN SELECT RAISE(ABORT, 'selection provenance is immutable'); END
        """,
        "trg_paper_selection_picks_require_run": """
            CREATE TRIGGER trg_paper_selection_picks_require_run
            BEFORE INSERT ON paper_selection_picks
            WHEN NEW.run_id IS NULL
            BEGIN SELECT RAISE(ABORT, 'selection pick requires a run'); END
        """,
    }
    for name, ddl in guards.items():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(ddl)


def _migrate_runs(conn) -> None:
    """Rebuild a pre-R23 run table into the provenance-keyed shape (idempotent).

    Crash-safe: the pre-R23 table is renamed, not dropped, so an interrupted
    migration can be resumed — the copy runs again and only then is the legacy
    table dropped. ``executescript`` commits any pending transaction, so the
    steps are ordered so that no state is reachable in which the data is
    unreachable.
    """
    legacy = "paper_selection_runs_legacy"
    columns = _columns(conn, "paper_selection_runs")
    unique = _unique_index_columns(conn, "paper_selection_runs")
    keyed = ("trade_date", "strategy_id", "provenance_key") in unique
    if "provenance_key" in columns and keyed and not _table_exists(conn, legacy):
        return
    if not _table_exists(conn, legacy):
        conn.execute("ALTER TABLE paper_selection_runs RENAME TO " + legacy)
        conn.executescript(_runs_ddl())
    present = [name for name in LEGACY_RUN_COLUMNS
               if name in _columns(conn, legacy)]
    select_legacy = ", ".join(present)
    rows = conn.execute(f"SELECT {select_legacy} FROM {legacy}").fetchall()
    for row in rows:
        record = dict(row) if hasattr(row, "keys") else dict(zip(present, row, strict=False))
        key = f"{LEGACY_KEY_PREFIX}|{record.get('trade_date')}|{record.get('strategy_id')}"
        if conn.execute(
            "SELECT 1 FROM paper_selection_runs WHERE trade_date=? AND strategy_id=? "
            "AND provenance_key IS ?",
            (record.get("trade_date"), record.get("strategy_id"), key),
        ).fetchone():
            continue
        conn.execute(
            f"""INSERT INTO paper_selection_runs({select_legacy},
                    strategy_version, strategy_checksum, asof_day, scope, cycle_id,
                    provenance_status, provenance_key)
                VALUES({','.join('?' for _ in present)},?,?,?,?,?,?,?)""",
            tuple(record.get(name) for name in present)
            + (None, None, None, SP.SCOPE_RESEARCH, None,
               SP.STATUS_LEGACY_UNPROVEN, key),
        )
    conn.executescript(
        f"""
        INSERT INTO paper_selection_runs({select_legacy},
            strategy_version, strategy_checksum, asof_day, scope, cycle_id,
            provenance_status, provenance_key)
        SELECT {select_legacy}, NULL, NULL, NULL, '{SP.SCOPE_RESEARCH}', NULL,
               '{SP.STATUS_LEGACY_UNPROVEN}',
               '{LEGACY_KEY_PREFIX}|' || trade_date || '|' || strategy_id
        FROM {legacy} l
        WHERE NOT EXISTS (
            SELECT 1 FROM paper_selection_runs r
            WHERE r.trade_date = l.trade_date AND r.strategy_id = l.strategy_id
              AND r.provenance_key = '{LEGACY_KEY_PREFIX}|' || l.trade_date || '|' || l.strategy_id
        );
        DROP TABLE {legacy};
        """
    )


def _migrate_picks(conn) -> None:
    """Rebuild a pre-R23 pick table and attach legacy picks to their run."""
    legacy = "paper_selection_picks_legacy"
    columns = _columns(conn, "paper_selection_picks")
    unique = _unique_index_columns(conn, "paper_selection_picks")
    if not (("run_id", "rank_no") in unique and "run_id" in columns
            and not _table_exists(conn, legacy)):
        if not _table_exists(conn, legacy):
            conn.execute("ALTER TABLE paper_selection_picks RENAME TO " + legacy)
            conn.executescript(_picks_ddl())
        present = [name for name in LEGACY_PICK_COLUMNS
                   if name in _columns(conn, legacy)]
        select_legacy = ", ".join(present)
        conn.executescript(
            f"""
            INSERT INTO paper_selection_picks({select_legacy})
            SELECT {select_legacy} FROM {legacy} l
            WHERE NOT EXISTS (
                SELECT 1 FROM paper_selection_picks p
                WHERE p.trade_date = l.trade_date AND p.strategy_id = l.strategy_id
                  AND p.rank_no = l.rank_no AND p.run_id IS NULL
            );
            DROP TABLE {legacy};
            """
        )
    if not _table_exists(conn, "paper_selection_runs"):
        return
    # 结构性归属：legacy picks 只可能属于同 (trade_date, strategy_id) 的 legacy run。
    conn.execute(
        """UPDATE paper_selection_picks SET run_id = (
               SELECT r.id FROM paper_selection_runs r
               WHERE r.trade_date = paper_selection_picks.trade_date
                 AND r.strategy_id = paper_selection_picks.strategy_id
                 AND r.provenance_status = ?
           ) WHERE run_id IS NULL""",
        (SP.STATUS_LEGACY_UNPROVEN,),
    )


def catalog():
    """返回 5 套策略的编号/名称/模型族（顺序与注册表一致）。

    只用于「现在能跑哪些策略」的**展示与调度**，不是历史 authority。
    """
    labels = _registry_labels()
    items = []
    for index, spec_id in enumerate(_registry_active_ids(), start=1):
        items.append({
            "no": index,
            "label": f"策略{index}",
            "strategy_id": spec_id,
            "strategy_name": labels.get(spec_id, spec_id),
            "model_id": STRATEGY_MODEL.get(spec_id, spec_id),
        })
    return items


def _registry_labels() -> dict[str, str]:
    """Current display names — presentation only, never historical authority."""
    try:
        return SR.labels(db_path=_registry_db_path())
    except Exception:
        return {}


def _registry_active_ids() -> tuple[str, ...]:
    try:
        return tuple(SR.active_ids(db_path=_registry_db_path()))
    except Exception:
        return ()


def _run_one(model_id: str, topn: int):
    """调用既有选股流水线（含覆盖率与行情新鲜度门禁）。"""
    import main as M
    return M._select_uncached(strategy=model_id, topn=topn)


_DATE_KEYS = ("historical_factor_date", "trade_date", "reference_date", "complete_cutoff")


def _trade_date_of(result):
    """从选股返回里解析一个**展示用**的因子基准日。

    真实的 ``main._select_uncached`` 并不会在顶层放日期：它把
    ``historical_factor_date`` 写到每只 pick 上，把 ``reference_date`` /
    ``complete_cutoff`` 放在 ``data_quality`` 里。因此按
    顶层 → data_quality → pick 逐级回退，保证落库时不会丢数据来源。

    **这不是 provenance 的 as-of。** 权威 as-of 是 ``asof_day``，由
    :func:`strategy_selection_provenance.resolve_asof_day` 按「显式 → 唯一 →
    混合即拒绝」解析。本函数会接受 ``reference_date``（那是**目标**交易日，与因子
    as-of 是两个不同的事实），所以它只填 ``factor_date`` 这一展示列。
    """
    if not isinstance(result, dict):
        return ""
    sources = [result]
    quality = result.get("data_quality")
    if isinstance(quality, dict):
        sources.append(quality)
    sources.extend(p for p in (result.get("picks") or [])[:1] if isinstance(p, dict))
    for source in sources:
        for key in _DATE_KEYS:
            value = str(source.get(key) or "").strip()
            if value:
                return value[:10]
    return ""


def run_daily(strategies=None, topn: int = DEFAULT_TOPN, run_date: str | None = None,
              source: str = "scheduled", asof_day: str | None = None) -> dict:
    """为每套策略跑一次评分选股，并覆盖写入当天结果。

    返回 ``{trade_date, topn, strategies: [...]}``，每项含 status/picks；``status``
    ∈ ok / empty（候选不足）/ blocked（数据门禁未通过）/ error。

    Provenance 语义：

    * 每个策略在 run 创建时 pin **一次** immutable head，并解析一次 ``asof_day``；
      同一 run 的 picks 共享该 provenance（``run_id`` 引用）；
    * 重跑只覆盖**同一个 provenance key**；immutable version 变了就是另一份证据，
      旧证据保留；
    * as-of 无法唯一确定（缺候选日期 / 多日期冲突）时 run 记为
      ``provenance_status='unknown'``，**不**退回 today / 最新因子日 / min-max。
    """
    topn = max(1, min(int(topn), MAX_TOPN))
    trade_date = str(run_date or _today().isoformat())[:10]
    items = catalog()
    if strategies:
        wanted = {str(item) for item in strategies}
        items = [item for item in items
                 if item["strategy_id"] in wanted or str(item["no"]) in wanted]
    conn = _connect()
    try:
        ensure_schema(conn)
        summary = []
        for item in items:
            now = dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds")
            status, message, picks, factor_date = "error", "", [], ""
            result = None
            try:
                result = _run_one(item["model_id"], topn)
                if not isinstance(result, dict):
                    # 选股链路（main._select_uncached）在拒绝场景会返回
                    # FastAPI JSONResponse 而不是 dict；显式报错而不是让后续
                    # .get 抛出难以定位的 AttributeError。
                    raise TypeError(
                        f"选股链路返回类型异常: {type(result).__name__}")
                if result.get("need_init"):
                    status = "blocked"
                    message = str(result.get("message") or "选股数据尚未就绪")
                    factor_date = str(result.get("factor_date") or "")
                else:
                    raw = (result or {}).get("picks") or []
                    factor_date = _trade_date_of(result)
                    for index, pick in enumerate(raw[:topn], start=1):
                        news_check = pick.get("news_check") or {}
                        picks.append({
                            "rank_no": index,
                            "code": str(pick.get("code") or ""),
                            "name": pick.get("name"),
                            "industry": pick.get("industry"),
                            "price": pick.get("price"),
                            "pct": pick.get("pct"),
                            "score": pick.get("score"),
                            "super_net": pick.get("super_net"),
                            "reasons": pick.get("reasons") or [],
                            "news_status": news_check.get("status"),
                        })
                    status = "ok" if picks else "empty"
                    if not picks:
                        message = "当日无通过门禁的候选"
            except Exception as exc:
                status = "error"
                message = f"{type(exc).__name__}: {exc}"[:300]

            provenance = _run_provenance(conn, item["strategy_id"], result,
                                        explicit_asof=asof_day)

            # 覆盖语义：只覆盖**同一份证据**（同 provenance key），重跑不留副本；
            # 不同 immutable version / 不同 as-of 是另一份证据，绝不 DELETE。
            conn.execute("BEGIN")
            replaced = [int(row[0]) for row in conn.execute(
                "SELECT id FROM paper_selection_runs WHERE trade_date=? AND strategy_id=? "
                "AND provenance_key IS ?",
                (trade_date, item["strategy_id"], provenance["provenance_key"])).fetchall()]
            for run_id in replaced:
                conn.execute("DELETE FROM paper_selection_picks WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM paper_selection_runs WHERE id=?", (run_id,))
            conn.execute(
                """INSERT INTO paper_selection_runs(trade_date,strategy_id,strategy_no,
                   strategy_name,model_id,status,message,factor_date,topn,source,created_at,
                   strategy_version,strategy_checksum,asof_day,scope,cycle_id,
                   provenance_status,provenance_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trade_date, item["strategy_id"], item["no"], item["strategy_name"],
                 item["model_id"], status, message, factor_date, topn, source, now,
                 provenance["strategy_version"], provenance["strategy_checksum"],
                 provenance["asof_day"], provenance["scope"], provenance["cycle_id"],
                 provenance["provenance_status"], provenance["provenance_key"]),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # 其它 run 的 picks 由它们自己的 run_id 归属；这里只写本 run 的。
            conn.execute("DELETE FROM paper_selection_picks WHERE run_id=?", (run_id,))
            for pick in picks:
                conn.execute(
                    """INSERT INTO paper_selection_picks(run_id,trade_date,strategy_id,
                       rank_no,code,name,industry,price,pct,score,super_net,reasons,news_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, trade_date, item["strategy_id"], pick["rank_no"], pick["code"],
                     pick["name"], pick["industry"], pick["price"], pick["pct"],
                     pick["score"], pick["super_net"],
                     json.dumps(pick["reasons"], ensure_ascii=False), pick["news_status"]),
                )
            conn.commit()
            summary.append({**item, "status": status, "message": message,
                            "factor_date": factor_date, "picks": picks,
                            "provenance": provenance})
        return {"trade_date": trade_date, "topn": topn, "source": source,
                "strategies": summary}
    finally:
        conn.close()


def _run_provenance(conn, strategy_id: str, result, *, explicit_asof=None) -> dict:
    """Pin one run's provenance: resolve the immutable head once, the as-of once.

    The immutable version comes from
    :func:`strategy_selection_resolver.research_provenance` —— 研究 run **唯一**
    允许读"现在"的地方；as-of 来自
    :func:`strategy_selection_provenance.resolve_asof_day`，它拒绝缺失或冲突的
    输入而不是猜一个。
    """
    declared = SP.declared_asof_candidates(result)
    try:
        asof = SP.resolve_asof_day(explicit_asof, declared)
    except SP.AsOfUnprovable as exc:
        asof, asof_detail = None, str(exc)
    else:
        asof_detail = ""
    if asof is None:
        reading = SP.ProvenanceReading(None, SP.STATUS_UNKNOWN,
                                       asof_detail or "as-of unprovable", str(strategy_id))
    else:
        # provenance 只 pin 一次：用只读的 registry 连接，读完即关闭并持久化。
        with closing(_registry_conn()) as registry:
            reading = SRES.research_provenance(registry, strategy_id, asof_day=asof)
    if reading.is_authoritative:
        payload = reading.require().to_dict()
        payload["provenance_status"] = SP.STATUS_VERIFIED
        payload["provenance_detail"] = ""
        payload["provenance_key"] = SP.run_provenance_key(
            scope=payload["scope"], subject=payload["strategy_id"],
            asof_day=payload["asof_day"], strategy_version=payload["strategy_version"],
            strategy_checksum=payload["strategy_checksum"],
        )
        return payload
    return {
        "strategy_id": str(strategy_id),
        "strategy_version": None,
        "strategy_checksum": None,
        "asof_day": asof,
        "scope": SP.SCOPE_RESEARCH,
        "cycle_id": None,
        "provenance_status": SP.STATUS_UNKNOWN,
        "provenance_detail": reading.detail,
        "provenance_key": SP.run_provenance_key(
            scope=SP.SCOPE_RESEARCH, subject=str(strategy_id), asof_day=asof,
        ),
    }


def _latest_trade_date(conn):
    row = conn.execute(
        "SELECT trade_date FROM paper_selection_runs ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    return row["trade_date"] if row else None


def _run_projection(row, provenance: SP.ProvenanceReading, *, current_name) -> dict:
    """One run as a compatibility projection plus its provenance.

    ``strategy_name`` is the **historical** persisted name. The current registry
    name is exposed separately as ``current_strategy_name`` for display only — it
    is never the authority for a historical row（规格 §15）。
    """
    payload = {
        "run_id": row["id"],
        "strategy_id": row["strategy_id"],
        "strategy_no": row["strategy_no"],
        "strategy_name": row["strategy_name"],
        "current_strategy_name": current_name,
        "model_id": row["model_id"],
        "status": row["status"],
        "message": row["message"] or "",
        "factor_date": row["factor_date"] or "",
        "asof_day": row["asof_day"],
        "updated_at": row["created_at"] or None,
        "source": row["source"],
    }
    payload.update(provenance.to_dict())
    return payload


_EMPTY_PROVENANCE = {
    "run_id": None, "provenance_status": None, "provenance_detail": "",
    "strategy_version": None, "strategy_checksum": None, "asof_day": None,
    "scope": None, "cycle_id": None,
}


def latest(trade_date: str | None = None, strategy_id: str = "") -> dict:
    """读取某交易日（默认最近一次运行日）的分组选股结果。

    历史可见性来自**持久化行本身**，不再由当前 Registry 决定：策略被改名、暂停或
    归档之后，历史 run 仍然可读，且仍然带着它自己的 immutable version/checksum 与
    as-of（规格 §C1 / §C6 / §15）。
    """
    conn = _connect()
    try:
        ensure_schema(conn)
        day = str(trade_date or "").strip() or _latest_trade_date(conn)
        if not day:
            return {"found": False, "trade_date": None, "strategies": []}
        runs = [dict(row) for row in conn.execute(
            "SELECT * FROM paper_selection_runs WHERE trade_date=? ORDER BY strategy_no, id",
            (day,)).fetchall()]
        picks_rows = conn.execute(
            "SELECT * FROM paper_selection_picks WHERE trade_date=? "
            "ORDER BY strategy_id, rank_no", (day,)).fetchall()
    finally:
        conn.close()

    grouped: dict[int, list[dict]] = {}
    for row in picks_rows:
        grouped.setdefault(row["run_id"], []).append({
            "rank_no": row["rank_no"],
            "code": row["code"],
            "name": row["name"],
            "industry": row["industry"],
            "price": row["price"],
            "pct": row["pct"],
            "score": row["score"],
            "super_net": row["super_net"],
            "reasons": json.loads(row["reasons"] or "[]"),
            "news_status": row["news_status"],
        })

    labels = _registry_labels()
    by_strategy: dict[str, list[dict]] = {}
    for row in runs:
        by_strategy.setdefault(row["strategy_id"], []).append(row)

    strategies: list[dict] = []
    for spec_id, rows in by_strategy.items():
        if strategy_id and spec_id != strategy_id:
            continue
        projections = [(row, SRES.reading_from_run(row, default_scope=SP.SCOPE_RESEARCH))
                       for row in rows]
        authoritative = [(row, reading) for row, reading in projections
                         if reading.is_authoritative]
        primary_row, primary_reading = (authoritative[-1] if authoritative
                                        else projections[-1])
        entry = _run_projection(primary_row, primary_reading,
                                current_name=labels.get(spec_id))
        entry["label"] = f"策略{primary_row['strategy_no']}"
        entry["picks"] = grouped.get(primary_row["id"], [])
        # 同一天同一策略可能有多个 run（不同 immutable version / 不同 as-of）：
        # 全部保留，展示项取最权威、最新的那个（规格 §8）。
        entry["runs"] = [_run_projection(row, reading, current_name=labels.get(spec_id))
                         for row, reading in projections]
        strategies.append(entry)

    # 兼容：当前注册表里还没有当日 run 的策略仍以 not_run 出现（只影响展示）。
    seen = set(by_strategy)
    for item in catalog():
        if item["strategy_id"] in seen:
            continue
        if strategy_id and item["strategy_id"] != strategy_id:
            continue
        strategies.append({
            **item,
            "current_strategy_name": item["strategy_name"],
            "status": "not_run", "message": "", "factor_date": "",
            "updated_at": None, "source": None, "picks": [], "runs": [],
            **_EMPTY_PROVENANCE,
        })
    strategies.sort(key=lambda item: (item.get("strategy_no") or 0,
                                      item.get("run_id") or 0))
    return {"found": True, "trade_date": day, "strategies": strategies}
