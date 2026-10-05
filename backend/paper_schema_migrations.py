# -*- coding: utf-8 -*-
"""Paper ledger schema compatibility migrations.

所有增量 schema 变更集中在这里，并保持幂等（重复执行不会改变结果）。
调用方负责事务边界；本模块不导入交易引擎，也不执行行情或订单逻辑。
"""
from __future__ import annotations

import sqlite3


def table_columns(conn, table):
    """返回表的列名；表不存在时返回空集合。"""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def ensure_columns(conn, table, definitions):
    """按定义补齐缺失列，返回实际新增的列名。"""
    columns = table_columns(conn, table)
    added = []
    for column, definition in definitions.items():
        if column not in columns and columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(column)
    return tuple(added)


def ensure_paper_columns(conn):
    """补齐旧 paper ledger 的订单、lot、持仓和账户字段。"""
    changes = {}
    changes["paper_orders"] = ensure_columns(
        conn,
        "paper_orders",
        {
            "realized_pnl": "REAL",
            "order_type": "TEXT NOT NULL DEFAULT 'market'",
            "origin": "TEXT NOT NULL DEFAULT 'strategy'",
            "expires_at": "TEXT",
            "cancelled_at": "TEXT",
            "retry_of_order_id": "INTEGER",
            "filled_qty": "INTEGER NOT NULL DEFAULT 0",
            "remaining_qty": "INTEGER",
            "execution_asof": "TEXT",
            "execution_reasons": "TEXT NOT NULL DEFAULT '[]'",
            "execution_evidence": "TEXT NOT NULL DEFAULT '{}'",
            "pricing_basis": "TEXT",
            "slippage": "REAL NOT NULL DEFAULT 0",
            "ruleset_version": "TEXT",
            "execution_version": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    # 归档表列集必须与活跃表一致（retention 用 SELECT * 整行拷贝）。
    changes["paper_orders_archive"] = ensure_columns(
        conn,
        "paper_orders_archive",
        {
            "retry_of_order_id": "INTEGER",
            "filled_qty": "INTEGER NOT NULL DEFAULT 0",
            "remaining_qty": "INTEGER",
            "execution_asof": "TEXT",
            "execution_reasons": "TEXT NOT NULL DEFAULT '[]'",
            "execution_evidence": "TEXT NOT NULL DEFAULT '{}'",
            "pricing_basis": "TEXT",
            "slippage": "REAL NOT NULL DEFAULT 0",
            "ruleset_version": "TEXT",
            "execution_version": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    changes["paper_fills"] = ensure_columns(
        conn,
        "paper_fills",
        {
            "event_key": "TEXT",
            "execution_asof": "TEXT",
            "pricing_basis": "TEXT",
            "slippage": "REAL NOT NULL DEFAULT 0",
            "market_evidence": "TEXT NOT NULL DEFAULT '{}'",
            "ruleset_version": "TEXT",
            "execution_evidence": "TEXT NOT NULL DEFAULT '{}'",
        },
    )
    if table_columns(conn, "paper_fills"):
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_fills_event_key "
            "ON paper_fills(event_key) WHERE event_key IS NOT NULL"
        )
    # Preserve only fill quantities supported by existing immutable fill rows.
    # An old order status by itself is never upgraded into fill evidence.
    if table_columns(conn, "paper_orders") and table_columns(conn, "paper_fills"):
        conn.execute(
            """UPDATE paper_orders
                  SET filled_qty=COALESCE((SELECT SUM(f.qty) FROM paper_fills f
                                            WHERE f.order_id=paper_orders.id),0),
                      remaining_qty=MAX(0,qty-COALESCE((SELECT SUM(f.qty) FROM paper_fills f
                                                         WHERE f.order_id=paper_orders.id),0))
                WHERE remaining_qty IS NULL"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_paper_orders_remaining_on_insert
               AFTER INSERT ON paper_orders
               WHEN NEW.remaining_qty IS NULL
               BEGIN
                   UPDATE paper_orders
                      SET remaining_qty=MAX(0,NEW.qty-COALESCE(NEW.filled_qty,0))
                    WHERE id=NEW.id;
               END"""
        )
    changes["paper_position_lots"] = ensure_columns(
        conn,
        "paper_position_lots",
        {
            "cost_fee_included": "INTEGER NOT NULL DEFAULT 0",
            # R26：lot 的**逐笔成交血缘**。``source_order_id`` 只说明"来自哪张委托"，
            # 一笔委托可以有多个 FillEvent（部分成交）时无法证明某个 lot 到底由哪
            # 一笔成交产生。历史旧 lot 保持 NULL —— 无法从任何当前状态反推它来自
            # 哪笔流水，**绝不**按时间/价格猜一个 fill_id。
            "source_fill_id": "INTEGER",
        },
    )
    changes["paper_positions"] = ensure_columns(
        conn,
        "paper_positions",
        {"asset_type": "TEXT NOT NULL DEFAULT 'stock_t1'"},
    )
    changes["paper_accounts"] = ensure_columns(
        conn,
        "paper_accounts",
        {
            "cycle_id": "INTEGER",
            "mode": "TEXT NOT NULL DEFAULT 'swing'",
            "style": "TEXT NOT NULL DEFAULT 'pullback'",
            "risk_profile": "TEXT NOT NULL DEFAULT 'aggressive'",
            "params": "TEXT NOT NULL DEFAULT '{}'",
            "daily_start_nav": "REAL",
            "daily_nav_date": "TEXT",
            "cooldown_until": "TEXT",
        },
    )
    return changes


def ensure_order_lineage_column(conn):
    """PR-29：订单重试血缘列 retry_of_order_id（活跃表 + 归档表，幂等）。"""
    definitions = {"retry_of_order_id": "INTEGER"}
    changes = {}
    for table in ("paper_orders", "paper_orders_archive"):
        changes[table] = ensure_columns(conn, table, definitions)
    return changes


def ensure_order_cycle_provenance(conn):
    """v18：订单的**不可变周期归属**（write-time fact，绝不回填历史）。

    给 ``paper_orders`` 与 ``paper_orders_archive`` 同时、同位置加上 ``cycle_id``：
    retention 仍用 ``INSERT OR IGNORE INTO paper_orders_archive SELECT * FROM
    paper_orders`` 整行拷贝，所以两张表的列数与顺序必须严格一致，否则整行拷贝会错位。

    本函数只做两件事：``ALTER TABLE ... ADD COLUMN`` 与 guard 安装。**绝不**给历史行
    回填 ``cycle_id`` —— 升级前的订单属于哪个周期无法从任何**当前**状态反推
    （``paper_accounts.cycle_id`` 是可变重绑定，``paper_cycles.started_at`` 对 paused
    周期为 NULL），``cycle_id IS NULL`` 正是诚实的 legacy provenance 状态。

    Guard 语义：

    * ``paper_orders`` BEFORE INSERT —— 新的 account-scoped 订单必须带 ``cycle_id``，
      且必须指向真实存在的 ``paper_cycles.id``；trigger 不回扫旧行，因此历史 NULL
      行不受影响。**不**拿"当前 active cycle"再比较一次，否则会把 write-time fact
      和后来的 current state 混在一起。
    * ``paper_orders`` / ``paper_orders_archive`` BEFORE UPDATE OF cycle_id —— 一经
      写入不得更改，``NULL -> 8`` 同样被阻止；否则以后任何 repair 脚本都能把
      "不知道"洗白成"知道"。
    * ``paper_orders_archive`` **不装** INSERT guard —— 升级前的 deferred / waitlist /
      retry 行之后仍可能经 ``SELECT *`` 进入归档表，legacy NULL 必须允许归档。
    """
    definitions = {"cycle_id": "INTEGER"}
    changes = {}
    for table in ("paper_orders", "paper_orders_archive"):
        changes[table] = ensure_columns(conn, table, definitions)
    _ensure_order_cycle_provenance_guards(conn)
    return changes


def _ensure_order_cycle_provenance_guards(conn):
    """Reject new orders without a durable cycle, and freeze it once written.

    Historical NULL rows are intentionally left untouched: a trigger never
    re-scans existing rows, so upgrading a multi-gigabyte ledger rewrites
    nothing. The archive table only receives the immutability guard, because
    retention still copies legacy NULL-cycle rows into it.
    """
    has_cycles = bool(table_columns(conn, "paper_cycles"))
    if has_cycles and "cycle_id" in table_columns(conn, "paper_orders"):
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_paper_orders_cycle_provenance_insert
                BEFORE INSERT ON paper_orders
                WHEN NEW.account_id IS NOT NULL AND (
                    NEW.cycle_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM paper_cycles c WHERE c.id=NEW.cycle_id
                    )
                )
                BEGIN SELECT RAISE(ABORT, 'invalid order cycle provenance'); END"""
        )
    for table in ("paper_orders", "paper_orders_archive"):
        if "cycle_id" not in table_columns(conn, table):
            continue
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_cycle_provenance_immutable
                BEFORE UPDATE OF cycle_id ON {table}
                WHEN NEW.cycle_id IS NOT OLD.cycle_id
                BEGIN SELECT RAISE(ABORT, 'order cycle provenance is immutable'); END"""
        )


# ─── 调仓状态表的周期归属（v19） ─────────────────────────────────────────────
#
# 不变量::
#
#     Every rebalance fact belongs to exactly one paper cycle.
#
# 背景：``rebalance_scanner`` 的三张状态表（scan / plan / cooldown）是在
# Round-11 才被搬进 paper ledger 的。数据库归属修对了，但**周期归属**没有：
# 三张表都没有 ``cycle_id``，于是
#
#   * ``rebalance_scans`` 的唯一键是 ``(scan_date, account_id, code)`` ——
#     这是一个**跨周期错误约束**：同一天从 cycle 8 翻到 cycle 9 时，
#     cycle 9 的扫描会 ``INSERT OR REPLACE`` 掉 cycle 8 的同一行；
#   * ``rebalance_cooldown`` 的主键是 ``(code, account_id)`` ——
#     旧周期的冷却会天然压住新周期；
#   * ``rebalance_plans`` 没有周期身份，只能靠"当前 active cycle"在验证时
#     重新猜归属。
#
# 本函数只做两件事：把三张表的 schema 变成周期可分区，并安装 guard。
# **绝不**给历史行回填 ``cycle_id``：升级前的调仓行属于哪个周期无法从任何
# **当前**状态反推（``paper_accounts.cycle_id`` 是可变重绑定，``MAX(paper_cycles.id)``
# 与"当时 active"是两回事，日期更是与周期无函数关系），
# ``cycle_id IS NULL`` 正是诚实的 legacy 状态 —— 所有 operational 查询都按
# ``cycle_id=?`` 过滤，NULL 行因此天然不可见、不可验证、不可执行。

#: 调仓状态表（三张都必须带 ``cycle_id``）。
REBALANCE_STATE_TABLES = ("rebalance_scans", "rebalance_plans", "rebalance_cooldown")

#: 规范列序（新建与重建共用同一份，避免"历史库与新库形状不同"）。
_REBALANCE_SCANS_COLUMNS = (
    "id", "cycle_id", "scan_date", "account_id", "code", "name",
    "current_qty", "cost", "current_price", "unrealized_pnl_pct", "hold_days",
    "quality_score", "prev_quality_score", "quality_change",
    "fund_flow_trend", "consecutive_outflow_days",
    "action", "action_reason", "planned_sell_ratio",
    "scan_version", "created_at",
)

_REBALANCE_PLANS_COLUMNS = (
    "id", "cycle_id", "plan_date", "execute_date", "account_id", "code", "name",
    "action", "sell_qty", "sell_ratio", "sell_reason",
    "replacement_code", "replacement_name", "replacement_score",
    "status", "open_price", "open_pct", "open_volume_ratio", "open_fund_flow",
    "open_verified", "open_verify_reason",
    "executed_at", "executed_price", "executed_qty", "realized_pnl",
    "plan_version", "created_at", "updated_at",
)

_REBALANCE_COOLDOWN_COLUMNS = (
    "cycle_id", "code", "account_id", "sold_date", "cooldown_until",
)

#: ``rebalance_scans`` 的最终唯一契约 —— 必须含 ``cycle_id``。
REBALANCE_SCANS_UNIQUE = ("cycle_id", "scan_date", "account_id", "code")

#: ``rebalance_cooldown`` 的最终主键 —— 必须含 ``cycle_id``。
REBALANCE_COOLDOWN_PK = ("cycle_id", "code", "account_id")


def rebalance_scans_ddl(table="rebalance_scans"):
    """``rebalance_scans`` 的规范 DDL（唯一键含 ``cycle_id``）。"""
    return f"""
        CREATE TABLE {table}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            scan_date TEXT NOT NULL,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            -- 持仓状态
            current_qty INTEGER,
            cost REAL,
            current_price REAL,
            unrealized_pnl_pct REAL,
            hold_days INTEGER,
            -- 质量评估
            quality_score REAL,
            prev_quality_score REAL,
            quality_change REAL,
            fund_flow_trend TEXT,
            consecutive_outflow_days INTEGER,
            -- 决策
            action TEXT NOT NULL,
            action_reason TEXT,
            planned_sell_ratio REAL DEFAULT 0,
            -- 元数据
            scan_version TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(cycle_id, scan_date, account_id, code)
        )
    """


def rebalance_plans_ddl(table="rebalance_plans"):
    """``rebalance_plans`` 的规范 DDL（``cycle_id`` 是 creation-time fact）。"""
    return f"""
        CREATE TABLE {table}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            plan_date TEXT NOT NULL,
            execute_date TEXT,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            action TEXT NOT NULL,
            -- 卖出计划
            sell_qty INTEGER,
            sell_ratio REAL,
            sell_reason TEXT,
            -- 替补计划
            replacement_code TEXT,
            replacement_name TEXT,
            replacement_score REAL,
            -- 状态
            status TEXT NOT NULL DEFAULT 'planned',
            -- 开盘验证
            open_price REAL,
            open_pct REAL,
            open_volume_ratio REAL,
            open_fund_flow REAL,
            open_verified BOOLEAN DEFAULT 0,
            open_verify_reason TEXT,
            -- 执行结果
            executed_at TEXT,
            executed_price REAL,
            executed_qty INTEGER,
            realized_pnl REAL,
            -- 元数据
            plan_version TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """


def rebalance_cooldown_ddl(table="rebalance_cooldown"):
    """``rebalance_cooldown`` 的规范 DDL（主键含 ``cycle_id``）。

    该表当前**没有任何读写调用点**（见 PR body 的 dead-table 审计），但 schema
    必须避免它将来被接回时把旧周期冷却套在新周期上 —— 不能留下 latent
    cross-cycle state。
    """
    return f"""
        CREATE TABLE {table}(
            cycle_id INTEGER,
            code TEXT NOT NULL,
            account_id TEXT NOT NULL,
            sold_date TEXT NOT NULL,
            cooldown_until TEXT NOT NULL,
            PRIMARY KEY(cycle_id, code, account_id)
        )
    """


def _unique_index_columns(conn, table):
    """返回该表上第一个 UNIQUE **约束**的列序列（无则 ``None``）。

    只看 ``origin='u'``（``UNIQUE(...)`` 表约束产生的隐式索引），不看
    ``CREATE INDEX``（``origin='c'``）—— 后者可以随便增删，不是契约。
    """
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        if len(row) < 4 or int(row[2]) != 1 or str(row[3]) != "u":
            continue
        try:
            columns = [r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})").fetchall()]
        except sqlite3.Error:  # pragma: no cover - 竞态
            continue
        return tuple(columns)
    return None


def _primary_key_columns(conn, table):
    """返回该表的主键列序列（按 ``pk`` 序号；无主键则 ``()``）。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return ()
    keyed = sorted((int(r[5]), r[1]) for r in rows if int(r[5]) > 0)
    return tuple(name for _index, name in keyed)


def _rebuild_table(conn, table, ddl_factory, canonical_columns):
    """安全、幂等的 table rebuild。

    规则：**只有当约束本身要变时才重建**（``rebalance_scans`` 的 UNIQUE、
    ``rebalance_cooldown`` 的 PK）；纯加列走 ``ALTER TABLE ADD COLUMN``。

    重建在调用方的事务内完成：建新表 → 显式列名整行搬运 → DROP 旧表 → RENAME。
    搬运**逐列点名**（绝不 ``SELECT *``），旧表缺的列写 ``NULL`` —— 对
    ``cycle_id`` 而言这正是"legacy 归属不可证明"的诚实取值。
    """
    old_columns = table_columns(conn, table)
    if not old_columns:
        conn.execute(ddl_factory(table))
        return "created"
    staged = f"{table}__r12_rebuild"
    conn.execute(f"DROP TABLE IF EXISTS {staged}")
    conn.execute(ddl_factory(staged))
    target = ", ".join(f'"{column}"' for column in canonical_columns)
    source = ", ".join(
        f'"{column}"' if column in old_columns else "NULL"
        for column in canonical_columns
    )
    conn.execute(
        f'INSERT INTO "{staged}" ({target}) SELECT {source} FROM "{table}"'
    )
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{staged}" RENAME TO "{table}"')
    return "rebuilt"


def _ensure_rebalance_cycle_guards(conn):
    """新行必须带真实周期，且周期归属一经写入不可更改。

    为什么用 trigger 而不是 ``NOT NULL`` 列约束：``NOT NULL`` 是**逐行**约束，
    它无法同时表达"新行必须非 NULL"与"历史行保持 NULL"—— 声明了 ``NOT NULL``
    就再也插不进 legacy 行，升级必须回填，而回填就是猜归属。v18 的订单周期归属
    已经确立了同一模式（列可空 + BEFORE INSERT guard + 不可变 guard），这里沿用。

    历史 NULL 行不受影响：trigger 不回扫既有行，因此升级一个多 GB 账本不会重写
    任何数据。``paper_cycles`` 不存在（极简 schema / 测试库）时只校验非 NULL ——
    读不到周期事实就不假装能校验它。
    """
    has_cycles = bool(table_columns(conn, "paper_cycles"))
    for table in REBALANCE_STATE_TABLES:
        if "cycle_id" not in table_columns(conn, table):
            continue
        when = "NEW.cycle_id IS NULL"
        if has_cycles:
            when += (" OR NOT EXISTS (SELECT 1 FROM paper_cycles c"
                     " WHERE c.id=NEW.cycle_id)")
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_cycle_required_insert
                BEFORE INSERT ON {table}
                WHEN {when}
                BEGIN SELECT RAISE(ABORT, 'rebalance state requires a cycle'); END"""
        )
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_cycle_immutable
                BEFORE UPDATE OF cycle_id ON {table}
                WHEN NEW.cycle_id IS NOT OLD.cycle_id
                BEGIN SELECT RAISE(ABORT, 'rebalance cycle ownership is immutable'); END"""
        )


def ensure_rebalance_state_cycle_ownership(conn):
    """v19：调仓状态（scan / plan / cooldown）的**周期归属**（幂等，不回填）。

    三张表都必须带 ``cycle_id``，且唯一契约必须把周期算进去：

    * ``rebalance_scans``：``UNIQUE(scan_date, account_id, code)``
      → ``UNIQUE(cycle_id, scan_date, account_id, code)``（**必须重建**，不能只
      ``ADD COLUMN`` 再把旧 UNIQUE 留着 —— 那样同日跨周期仍会互相 replace）；
    * ``rebalance_plans``：纯新增列（无约束变更，最省）；
    * ``rebalance_cooldown``：``PRIMARY KEY(code, account_id)``
      → ``PRIMARY KEY(cycle_id, code, account_id)``（**必须重建**）。

    旧行保留、其它字段逐字保留、``cycle_id`` 一律 ``NULL``（不猜归属）。
    """
    changes = {}

    if _unique_index_columns(conn, "rebalance_scans") != REBALANCE_SCANS_UNIQUE:
        changes["rebalance_scans"] = _rebuild_table(
            conn, "rebalance_scans", rebalance_scans_ddl, _REBALANCE_SCANS_COLUMNS,
        )
    else:
        changes["rebalance_scans"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rebalance_scans_date"
        " ON rebalance_scans(scan_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rebalance_scans_cycle_date"
        " ON rebalance_scans(cycle_id, scan_date DESC)"
    )

    if "cycle_id" not in table_columns(conn, "rebalance_plans"):
        if not table_columns(conn, "rebalance_plans"):
            conn.execute(rebalance_plans_ddl("rebalance_plans"))
            changes["rebalance_plans"] = "created"
        else:
            conn.execute('ALTER TABLE "rebalance_plans" ADD COLUMN cycle_id INTEGER')
            changes["rebalance_plans"] = "altered"
    else:
        changes["rebalance_plans"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rebalance_plans_date"
        " ON rebalance_plans(plan_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rebalance_plans_status"
        " ON rebalance_plans(status, plan_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rebalance_plans_cycle_status"
        " ON rebalance_plans(cycle_id, status, plan_date DESC)"
    )

    if _primary_key_columns(conn, "rebalance_cooldown") != REBALANCE_COOLDOWN_PK:
        changes["rebalance_cooldown"] = _rebuild_table(
            conn, "rebalance_cooldown", rebalance_cooldown_ddl,
            _REBALANCE_COOLDOWN_COLUMNS,
        )
    else:
        changes["rebalance_cooldown"] = "ok"

    _ensure_rebalance_cycle_guards(conn)
    return changes


# ─── 周期归属的持仓运行时风险状态（v20） ─────────────────────────────────────
#
# 不变量::
#
#     paper_position_lots        = position quantity / ownership authority
#     paper_position_risk_state  = runtime peak / staged-take-profit authority
#     paper_positions            = compatibility projection only
#
# 背景：``peak_price``（移动止损的峰值）与 ``take_stage``（阶梯止盈已消费档位）
# 不是展示元数据 —— ``paper_trading._sell_plan`` 用前者算 drawdown 并触发
# ``trailing_stop``、用后者驱动阶梯止盈的状态机，它们是 execution-adjacent 的
# 运行时风险状态。但它们一直寄居在 ``paper_positions`` 投影里，而该表
# **没有 cycle_id、没有 position episode 身份**（``PRIMARY KEY(account_id, code)``），
# 于是旧周期 / 旧 episode 的 peak 与 stage 会直接泄漏进新周期，真实改变卖出决策。
#
# **绝不回填**：升级前的 ``paper_positions.peak_price`` / ``take_stage`` 属于哪个
# 周期、哪个 position episode 无法从任何当前状态反推 —— 和 #169 禁止
# "stale mirror -> current lot" 是同一种错误。没有风险状态行时，语义是
# fail-safe（读模型给成本锚 peak 与"未知"stage），绝不用投影元数据冒充权威。
#
#: 规范列序（与 DDL 共用一份事实）。
POSITION_RISK_STATE_COLUMNS = (
    "cycle_id", "account_id", "code", "peak_price", "take_stage",
    "opened_order_id", "initialized_at", "updated_at",
)


def position_risk_state_ddl(table="paper_position_risk_state"):
    """``paper_position_risk_state`` 的规范 DDL（主键含 ``cycle_id``）。

    ``cycle_id`` 直接 ``NOT NULL``：本表由 v20 全新创建、**禁止回填**，所以
    不存在"历史行需要保持 NULL"的问题 —— 不需要 v18/v19 的"可空列 + trigger"
    折衷，可以直接用列约束表达"每行都必须属于一个周期"。cycle 是否真实存在的
    校验仍由 trigger 承担（``paper_cycles`` 不存在的极简库里退化为仅 NOT NULL）。
    """
    return f"""
        CREATE TABLE {table}(
            cycle_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            peak_price REAL NOT NULL,
            take_stage INTEGER NOT NULL DEFAULT 0,
            opened_order_id INTEGER,
            initialized_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(cycle_id, account_id, code)
        )
    """


def _ensure_position_risk_state_guards(conn):
    """新行必须指向真实周期，且周期归属一经写入不可更改。

    周期身份是 **creation-time ownership fact**：风险状态行由哪次建仓产生，
    就永远属于哪个周期。``BEFORE UPDATE OF cycle_id`` 让任何 repair 脚本都
    无法把行"改挂"到另一个周期 —— 那等于事后重写归属历史。
    """
    if not table_columns(conn, "paper_position_risk_state"):
        return
    has_cycles = bool(table_columns(conn, "paper_cycles"))
    when = "NEW.cycle_id IS NULL"
    if has_cycles:
        when += (" OR NOT EXISTS (SELECT 1 FROM paper_cycles c"
                 " WHERE c.id=NEW.cycle_id)")
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_paper_position_risk_state_cycle_required_insert
            BEFORE INSERT ON paper_position_risk_state
            WHEN {when}
            BEGIN SELECT RAISE(ABORT, 'position risk state requires a cycle'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_paper_position_risk_state_cycle_immutable
            BEFORE UPDATE OF cycle_id ON paper_position_risk_state
            WHEN NEW.cycle_id IS NOT OLD.cycle_id
            BEGIN SELECT RAISE(ABORT, 'position risk state cycle ownership is immutable'); END"""
    )


def ensure_position_risk_state(conn):
    """v20：cycle-owned 的持仓运行时风险状态表（幂等，**绝不回填**）。

    只建表 + 安装 guard。**不执行任何** ``INSERT ... SELECT ... FROM
    paper_positions``：升级前投影里的 peak/take_stage 没有 cycle / episode
    归属，把它们搬进本表就是把"不知道"洗白成"当前周期的已知状态"。
    首个状态行只能由 verified BUY 的 episode 生命周期创建。
    """
    changes = {}
    if not table_columns(conn, "paper_position_risk_state"):
        conn.execute(position_risk_state_ddl("paper_position_risk_state"))
        changes["paper_position_risk_state"] = "created"
    else:
        changes["paper_position_risk_state"] = "ok"
    _ensure_position_risk_state_guards(conn)
    return changes


# ─── 周期归属的风险扫描运行状态（v21） ──────────────────────────────────────
#
# 不变量::
#
#     paper_risk_scan_runs = risk scan run lifecycle authority
#     paper_audit          = human-readable audit trail only
#
# 背景：分钟级风险扫描的幂等性此前由 ``paper_audit`` 里一条
# ``event='risk_scan_state'`` 的 JSON 标记决定，而那条标记的 key **只有机器分钟**，
# 没有 cycle 身份、也没有 asof 日期。于是：
#
#   * 同一分钟内翻周期时，新周期会读到旧周期的 completed 标记而被
#     ``already_scanned`` 吞掉 —— 新周期持仓一次风控都没跑；
#   * 同一分钟跑不同 asof 的 replay 会互相抑制；
#   * wrapper 与 impl 各自取一次分钟，跨分钟边界会留下永远没有 failed 转换的
#     orphan running 标记。
#
# **绝不回填**：升级前的 audit 标记没有 durable cycle 归属，无法从当前状态
# 反推（``paper_accounts.cycle_id`` 是可变重绑定、``MAX(paper_cycles.id)``
# 不是"当时 active"、日期与周期无函数关系）。历史归属未知就保持未知，留在
# 旧 audit 里；本表只从空表开始承载**未来**的运行事实。

#: 规范列序（与 DDL 共用一份事实）。
RISK_SCAN_RUN_COLUMNS = (
    "id", "cycle_id", "asof_date", "scan_minute", "status", "attempt",
    "started_at", "finished_at", "error", "detail",
)

#: 合法状态（与 ``paper_risk_scan_state.SCAN_STATUSES`` 共用一份事实）。
RISK_SCAN_RUN_STATUSES = ("running", "completed", "failed")


def risk_scan_run_ddl(table="paper_risk_scan_runs"):
    """``paper_risk_scan_runs`` 的规范 DDL。

    身份 ``UNIQUE(cycle_id, asof_date, scan_minute)`` 三者缺一不可：

    * 只做 ``UNIQUE(cycle_id, scan_minute)`` 会让同一分钟的不同 asof replay
      互相抑制；
    * 只做 ``UNIQUE(asof_date, scan_minute)`` 会让同分钟翻周期互相抑制。

    ``cycle_id`` 直接 ``NOT NULL``：本表由 v21 全新创建、**禁止回填**，
    因此可以直接用列约束表达"每行都必须属于一个周期"，不需要 v18/v19 的
    "可空列 + trigger"折衷。cycle 是否真实存在由 trigger 校验。
    """
    return f"""
        CREATE TABLE {table}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            asof_date TEXT NOT NULL,
            scan_minute TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT,
            detail TEXT NOT NULL DEFAULT '{{}}',
            UNIQUE(cycle_id, asof_date, scan_minute),
            CHECK(status IN ('running','completed','failed'))
        )
    """


def _ensure_risk_scan_run_guards(conn):
    """新行必须指向真实周期，且身份一经写入不可更改。

    身份是 **claim-time fact**：这次扫描认领的是哪个周期的哪一天哪一分钟，
    就永远是那个身份。``BEFORE UPDATE OF cycle_id, asof_date, scan_minute``
    让任何 repair 脚本都无法把一次已经跑过的扫描"改挂"到另一个周期/另一天/
    另一分钟 —— 那等于事后重写执行归属历史，正是 R16 要消灭的 provenance
    fabrication。
    """
    if not table_columns(conn, "paper_risk_scan_runs"):
        return
    has_cycles = bool(table_columns(conn, "paper_cycles"))
    when = "NEW.cycle_id IS NULL"
    if has_cycles:
        when += (" OR NOT EXISTS (SELECT 1 FROM paper_cycles c"
                 " WHERE c.id=NEW.cycle_id)")
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_paper_risk_scan_runs_cycle_required_insert
            BEFORE INSERT ON paper_risk_scan_runs
            WHEN {when}
            BEGIN SELECT RAISE(ABORT, 'invalid risk scan cycle provenance'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_paper_risk_scan_runs_identity_immutable
            BEFORE UPDATE OF cycle_id, asof_date, scan_minute ON paper_risk_scan_runs
            WHEN NEW.cycle_id IS NOT OLD.cycle_id
              OR NEW.asof_date IS NOT OLD.asof_date
              OR NEW.scan_minute IS NOT OLD.scan_minute
            BEGIN SELECT RAISE(ABORT, 'risk scan identity is immutable'); END"""
    )


def ensure_risk_scan_run_state(conn):
    """v21：cycle-owned 的风险扫描运行状态表（幂等，**绝不回填**）。

    只建表 + 安装 guard。**不执行任何** ``INSERT ... SELECT ... FROM
    paper_audit``：升级前那条 ``event='risk_scan_state'`` 的标记没有 cycle
    归属，把它的 created_at / 当前 active cycle / MAX(cycle_id) 拿来推算
    "当时属于哪个周期"就是把"不知道"洗白成"知道"。历史扫描归属未知就保持
    未知；首个运行行只能由 ``paper_risk_scan_state.claim_scan`` 创建。
    """
    changes = {}
    if not table_columns(conn, "paper_risk_scan_runs"):
        conn.execute(risk_scan_run_ddl("paper_risk_scan_runs"))
        changes["paper_risk_scan_runs"] = "created"
    else:
        changes["paper_risk_scan_runs"] = "ok"
    _ensure_risk_scan_run_guards(conn)
    return changes


def ensure_strategy_reference_columns(conn):
    """Append immutable strategy-version stamps to execution evidence tables.

    Live and archive tables intentionally receive the columns in the same
    order because retention still uses ``INSERT ... SELECT *``.
    Historical rows remain NULL and resolve through the small immutable legacy
    binding table; this avoids rewriting a multi-gigabyte ledger.
    """
    definitions = {
        "strategy_id": "TEXT",
        "strategy_version": "INTEGER",
        "strategy_checksum": "TEXT",
    }
    changes = {}
    for table in (
        "paper_signals", "paper_signals_archive",
        "paper_orders", "paper_orders_archive",
        "paper_risk_decisions", "paper_audit",
    ):
        changes[table] = ensure_columns(conn, table, definitions)
    _ensure_strategy_reference_guards(conn)
    return changes


#: Table-aware explicit-unknown exceptions for strategy provenance.  A missing
#: entry means all-NULL provenance is rejected for that table.
STRATEGY_STAMP_UNKNOWN_ALLOWANCE = {
    "paper_orders": (
        "NEW.side='sell' AND NEW.cycle_id IS NOT NULL "
        "AND NEW.status IN ('pending_execution','unfilled_limit_down')"
    ),
    "paper_risk_decisions": "NEW.side='sell'",
    "paper_audit": (
        "NEW.event IN ("
        "'sell_filled',"
        "'protective_exit_recovery_watch',"
        "'quality_rotation',"
        "'concentration_rotation',"
        "'permission_scope_exit'"
        ")"
    ),
}


def _ensure_strategy_reference_guards(conn):
    """Reject partial, forged, or later-mutated evidence stamps.

    Historical NULL rows are intentionally left untouched. A complete stamp
    must reference an exact immutable strategy version. An all-NULL stamp is
    the explicit "unknown provenance" state only where the table-aware
    allowance below says a causal protective-SELL path needs it; partial and
    forged stamps are rejected everywhere. Insert triggers are refreshed so an
    already-initialized database receives the current guard definition.
    """
    for table in (
        "paper_signals", "paper_orders", "paper_risk_decisions", "paper_audit",
    ):
        if not {"account_id", "strategy_id", "strategy_version", "strategy_checksum"}.issubset(
            table_columns(conn, table)
        ):
            continue
        trigger = f"trg_{table}_strategy_stamp_insert"
        allow_unknown = STRATEGY_STAMP_UNKNOWN_ALLOWANCE.get(table, "0")
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(
            f"""CREATE TRIGGER {trigger}
                BEFORE INSERT ON {table}
                WHEN NEW.account_id IS NOT NULL
                 AND NOT (
                    NEW.strategy_id IS NULL
                    AND NEW.strategy_version IS NULL
                    AND NEW.strategy_checksum IS NULL
                    AND ({allow_unknown})
                 )
                 AND (
                    NEW.strategy_id IS NULL OR NEW.strategy_version IS NULL
                    OR NEW.strategy_checksum IS NULL
                    OR NEW.strategy_id <> NEW.account_id
                    OR NOT EXISTS (
                        SELECT 1 FROM paper_strategy_versions v
                        WHERE v.strategy_id=NEW.strategy_id
                          AND v.version=NEW.strategy_version
                          AND v.checksum=NEW.strategy_checksum
                    )
                )
                BEGIN SELECT RAISE(ABORT, 'invalid strategy version stamp'); END"""
        )
    for table in (
        "paper_signals", "paper_signals_archive", "paper_orders",
        "paper_orders_archive", "paper_risk_decisions", "paper_audit",
    ):
        if not {"strategy_id", "strategy_version", "strategy_checksum"}.issubset(
            table_columns(conn, table)
        ):
            continue
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_strategy_stamp_immutable
                BEFORE UPDATE OF strategy_id,strategy_version,strategy_checksum ON {table}
                WHEN NEW.strategy_id IS NOT OLD.strategy_id
                  OR NEW.strategy_version IS NOT OLD.strategy_version
                  OR NEW.strategy_checksum IS NOT OLD.strategy_checksum
                BEGIN SELECT RAISE(ABORT, 'strategy version stamp is immutable'); END"""
        )


def ensure_signal_cycle_provenance(conn):
    """v23：信号的**不可变周期归属**（write-time fact，绝不回填历史）。

    给 ``paper_signals`` 与 ``paper_signals_archive`` 同时、同位置加上 ``cycle_id``：
    retention 仍用 ``INSERT OR IGNORE INTO paper_signals_archive SELECT * FROM
    paper_signals`` 整行拷贝，所以两张表的列数与顺序必须严格一致，否则整行拷贝会错位。

    为什么 signal 需要自己的周期归属：``paper_orders.cycle_id`` 只覆盖「已经下单」
    的 signal。被 blocked / pending / 从未成交的 signal 根本没有 order，于是
    「这条 signal 属于哪个 cycle」无处可查 —— 只能靠 ``paper_accounts.cycle_id``
    这一**可变**重绑定去猜，而它在新建周期时会被改写。

    本函数只做两件事：``ALTER TABLE ... ADD COLUMN`` 与 guard 安装。**绝不**给历史行
    回填 ``cycle_id`` —— 升级前的 signal 属于哪个周期无法从任何**当前**状态反推，
    ``cycle_id IS NULL`` 正是诚实的 legacy provenance 状态。

    Guard 语义：

    * ``paper_signals`` BEFORE INSERT —— account-scoped signal 必须带真实存在的
      ``cycle_id``，**且**该 account 当前确实属于这个 cycle。两条都是 write-time
      fact：第一条保证周期不是幽灵，第二条保证「A 的候选」不会被写成「B 的
      signal」。这是防 rollover 穿透的**第二层**防线 —— 写入器已经在
      ``BEGIN IMMEDIATE`` 里做过同样的校验并会整批 stale，DB 这一层保证即使
      调用方漏了、或将来新增了写入点，也无法落库一条 cycle 归属已经过期的行。
      注意：只约束**新写**的行；既有历史行（含 NULL）不受影响，不做回填。
    * 两张表 BEFORE UPDATE OF cycle_id —— 一经写入不得更改，``NULL -> 8`` 同样被阻止。
      **rollover 后旧 signal 的历史行保持原 cycle**，绝不会被 UPDATE 迁移。
    * ``paper_signals_archive`` **不装** INSERT guard —— 历史 NULL 行仍会经
      ``SELECT *`` 进入归档表，legacy NULL 必须允许归档。
    """
    definitions = {"cycle_id": "INTEGER"}
    changes = {}
    for table in ("paper_signals", "paper_signals_archive"):
        changes[table] = ensure_columns(conn, table, definitions)
    _ensure_signal_cycle_provenance_guards(conn)
    return changes


def _ensure_signal_cycle_provenance_guards(conn):
    """Reject new signals without a durable cycle, and freeze it once written."""
    has_cycles = bool(table_columns(conn, "paper_cycles"))
    # account-cycle 一致性检查需要 ``paper_accounts`` 存在。极简 schema 下只装
    # 前半段（cycle 必须存在），不会引用一张不存在的表。
    has_accounts = bool(table_columns(conn, "paper_accounts"))
    if has_cycles and "cycle_id" in table_columns(conn, "paper_signals"):
        account_clause = (
            """                    OR NOT EXISTS (
                        SELECT 1 FROM paper_accounts a
                        WHERE a.id = NEW.account_id
                          AND a.cycle_id = NEW.cycle_id
                    )
"""
            if has_accounts else ""
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_paper_signals_cycle_provenance_insert")
        conn.execute(
            """CREATE TRIGGER trg_paper_signals_cycle_provenance_insert
                BEFORE INSERT ON paper_signals
                WHEN NEW.account_id IS NOT NULL AND (
                    NEW.cycle_id IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM paper_cycles c WHERE c.id=NEW.cycle_id
                    )
""" + account_clause + """                )
                BEGIN SELECT RAISE(ABORT, 'invalid signal cycle provenance'); END"""
        )
    for table in ("paper_signals", "paper_signals_archive"):
        if "cycle_id" not in table_columns(conn, table):
            continue
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_cycle_provenance_immutable")
        conn.execute(
            f"""CREATE TRIGGER trg_{table}_cycle_provenance_immutable
                BEFORE UPDATE OF cycle_id ON {table}
                WHEN NEW.cycle_id IS NOT OLD.cycle_id
                BEGIN SELECT RAISE(ABORT, 'signal cycle provenance is immutable'); END"""
        )


def ensure_runtime_lease_columns(conn):
    """补齐调度租约/fencing 字段，并规范旧时间分隔符。"""
    migrations = {
        "paper_jobs": {
            "owner_key": "TEXT",
            "heartbeat_at": "TEXT",
            "expires_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_job_runs": {
            "owner_key": "TEXT",
            "heartbeat_at": "TEXT",
            "expires_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_runtime_locks": {
            "heartbeat_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_nav": {"quote_status": "TEXT NOT NULL DEFAULT 'verified'"},
    }
    changes = {}
    for table, definitions in migrations.items():
        changes[table] = ensure_columns(conn, table, definitions)
    # Older runner builds used an ISO ``T`` separator while the rest of the
    # ledger used a space. Normalize once so expiry comparisons stay correct.
    for table in ("paper_jobs", "paper_job_runs", "paper_runtime_locks"):
        columns = table_columns(conn, table)
        for column in ("started_at", "acquired_at", "heartbeat_at", "expires_at"):
            if column not in columns:
                continue
            conn.execute(
                f"UPDATE {table} SET {column}=replace({column},'T',' ') "
                f"WHERE {column} IS NOT NULL AND instr({column},'T')>0"
            )
    return changes


def ensure_ignition_shadow_table(conn):
    """补齐点火影子表及其索引；影子表缺失不应阻断主交易链路。"""
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_ignition_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                bucket TEXT NOT NULL,
                code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                price REAL,
                pct REAL,
                runup REAL,
                old_rule_passed INTEGER NOT NULL DEFAULT 0,
                old_rule_reason TEXT,
                ignition_passed INTEGER NOT NULL DEFAULT 0,
                ignition_reasons TEXT,
                price_30m REAL,
                at_30m TEXT,
                price_60m REAL,
                at_60m TEXT,
                resolved INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_ignition_shadow_unique
                ON paper_ignition_shadow(day, bucket, code)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_paper_ignition_shadow_recent
                ON paper_ignition_shadow(day, resolved)"""
        )
        return True
    except Exception:
        return False


def ensure_execution_verification_columns(conn):
    """PR-150 wiring：执行验证闸门的三列（活跃表 + 归档表，幂等）。

    只**新增**列，绝不覆盖或改写既有列：``status`` / ``realized_pnl`` 等保持
    原样，闸门是叠加的消费层结论。

    历史行保持 NULL —— NULL 在 :data:`execution_verification.VERIFIED_PREDICATE`
    里被 ``COALESCE`` 取成 0，因此旧行**自动**被排除在真实成交统计之外，
    而不是被默认升级成成交。
    """
    definitions = {
        "execution_status": "TEXT",
        "execution_verified": "INTEGER",
        "execution_evidence_source": "TEXT",
    }
    changes = {}
    for table in ("paper_orders", "paper_orders_archive"):
        changes[table] = ensure_columns(conn, table, definitions)
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_orders_execution_verified"
            " ON paper_orders(execution_verified, execution_status)"
        )
    except sqlite3.Error:  # pragma: no cover - 索引失败不应阻断主链路
        pass
    return changes


def ensure_proposal_lifecycle_columns(conn):
    """PR-33：风险放大提案的生命周期列（resolved_at/resolved_by/note，幂等）。"""
    import asymmetric_risk as AR

    try:
        AR.ensure_proposals_table(conn)
        AR.ensure_proposal_lifecycle_columns(conn)
        return True
    except Exception:
        return False


# ─── 隔离式 ShadowRun 追加证据（v24 / R32-C） ────────────────────────────────
#
# 不变量::
#
#     shadow_runs = Challenger 运行的唯一追加式证据 owner
#     paper_*     = 正式账本；Shadow 路径绝不写入其中任何一张表
#
# 这里刻意**不**建立 shadow_accounts / shadow_orders / shadow_fills /
# shadow_positions / shadow_lots / shadow_cash 六套与正式账本平行的 authority。
# 每一份 ShadowRun 保存该次运行的完整输入与输出 evidence，状态转移是纯函数；
# 下一条 run 只以显式 ``previous_shadow_run_id`` 引用上一条的 ``after_state``，
# 不查询"最新 run"。
#
# 身份：``run_id`` 是对 canonical evidence 的 SHA-256，因此
# ``run_id == run_fingerprint``（CHECK 强制）；``PRIMARY KEY(run_id)`` +
# ``UNIQUE(run_fingerprint)`` 让重复追加成为幂等 no-op，而不是第二条逻辑重复行。
# ``created_at`` 只是写入时刻，永不参与任何决策。


def shadow_run_ddl(table="shadow_runs"):
    """``shadow_runs`` 的规范 DDL（单一事实来源，migration 与 ``init_db`` 共用）。"""
    return f"""
        CREATE TABLE IF NOT EXISTS {table}(
            run_id TEXT PRIMARY KEY CHECK(length(run_id)=64),
            run_fingerprint TEXT NOT NULL UNIQUE CHECK(length(run_fingerprint)=64),
            challenger_strategy_id TEXT NOT NULL,
            challenger_strategy_version INTEGER NOT NULL CHECK(challenger_strategy_version>0),
            challenger_strategy_checksum TEXT NOT NULL CHECK(length(challenger_strategy_checksum)=64),
            active_strategy_id TEXT NOT NULL,
            active_strategy_version INTEGER NOT NULL CHECK(active_strategy_version>0),
            active_strategy_checksum TEXT NOT NULL CHECK(length(active_strategy_checksum)=64),
            environment_fingerprint TEXT NOT NULL CHECK(length(environment_fingerprint)=64),
            session_date TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            reference_capital REAL NOT NULL CHECK(reference_capital>0),
            previous_shadow_run_id TEXT REFERENCES {table}(run_id),
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK(run_id=run_fingerprint)
        )
    """


def ensure_shadow_runs_table(conn):
    """v24：建 ShadowRun 追加证据表 + 链索引 + append-only guard（幂等、不回填）。

    既有账本走 ``init_db`` 快路径时也必须执行本函数，否则升级后的线上库永远
    没有 ``shadow_runs``，Shadow 运行只能 fail closed。本函数只创建自己的表，
    不 INSERT、不 UPDATE 任何正式账本。
    """
    changes = {}
    if not table_columns(conn, "shadow_runs"):
        conn.execute(shadow_run_ddl("shadow_runs"))
        changes["shadow_runs"] = "created"
    else:
        changes["shadow_runs"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_runs_chain"
        " ON shadow_runs(challenger_strategy_id,challenger_strategy_version,run_id)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS shadow_runs_no_update
           BEFORE UPDATE ON shadow_runs
           BEGIN SELECT RAISE(ABORT,'shadow runs are append-only'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS shadow_runs_no_delete
           BEFORE DELETE ON shadow_runs
           BEGIN SELECT RAISE(ABORT,'shadow runs are append-only'); END"""
    )
    return changes


# ─── Active/Challenger 比对证据（v25 / R32-D） ────────────────────────────────
#
# 不变量::
#
#     shadow_comparison_reports = Active/Challenger 比对事实的唯一追加式 owner
#     shadow_runs               = Challenger 运行证据 owner；比对不回填其字段
#     paper_*                   = 正式账本；比对路径绝不写入其中任何一张表
#
# 刻意只建**一张**报告表：不建立 comparison_signal / comparison_execution /
# comparison_risk / comparison_performance 这类平行 authority 表。signal /
# decision / execution / risk / turnover / performance 都是同一份 canonical
# report envelope 的字段，派生展示必须从该 envelope 重建。
#
# 身份：``report_id`` 是对 canonical report material 的 SHA-256，因此
# ``report_id == report_fingerprint``（CHECK 强制）；``PRIMARY KEY(report_id)`` +
# ``UNIQUE(report_fingerprint)`` 让重复追加成为幂等 no-op，而不是第二条逻辑重复行。
# ``created_at`` 只是写入时刻，永不参与任何决策。
#
# 对 ``shadow_runs`` 刻意**不建**外键：比对只引用 exact run identity，且两侧都是
# append-only 证据；外键会在 ``PRAGMA foreign_keys`` 关闭的路径上退化成装饰，
# 真正的绑定由 service 按显式 run id 读取 + builder 的 identity 校验承担。


def shadow_comparison_report_ddl(table="shadow_comparison_reports"):
    """``shadow_comparison_reports`` 的规范 DDL（migration 与 ``init_db`` 共用）。"""
    return f"""
        CREATE TABLE IF NOT EXISTS {table}(
            report_id TEXT PRIMARY KEY CHECK(length(report_id)=64),
            report_fingerprint TEXT NOT NULL UNIQUE CHECK(length(report_fingerprint)=64),
            shadow_run_id TEXT NOT NULL CHECK(length(shadow_run_id)=64),
            shadow_run_fingerprint TEXT NOT NULL CHECK(length(shadow_run_fingerprint)=64),
            active_strategy_id TEXT NOT NULL,
            active_strategy_version INTEGER NOT NULL CHECK(active_strategy_version>0),
            active_strategy_checksum TEXT NOT NULL CHECK(length(active_strategy_checksum)=64),
            challenger_strategy_id TEXT NOT NULL,
            challenger_strategy_version INTEGER NOT NULL CHECK(challenger_strategy_version>0),
            challenger_strategy_checksum TEXT NOT NULL CHECK(length(challenger_strategy_checksum)=64),
            environment_fingerprint TEXT NOT NULL CHECK(length(environment_fingerprint)=64),
            session_date TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            availability TEXT NOT NULL CHECK(availability IN ('AVAILABLE','PARTIAL','UNAVAILABLE')),
            coverage_ratio REAL NOT NULL CHECK(coverage_ratio>=0 AND coverage_ratio<=1),
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK(report_id=report_fingerprint)
        )
    """


def ensure_shadow_comparison_reports(conn):
    """v25：建比对报告追加表 + 索引 + append-only guard（幂等、不回填）。

    既有账本走 ``init_db`` 快路径时也必须执行本函数，否则升级后的线上库永远没有
    ``shadow_comparison_reports``，比对只能 fail closed。本函数只创建自己的表，
    不 INSERT、不 UPDATE 任何正式账本，也不触碰 ``shadow_runs``。
    """
    changes = {}
    if not table_columns(conn, "shadow_comparison_reports"):
        conn.execute(shadow_comparison_report_ddl("shadow_comparison_reports"))
        changes["shadow_comparison_reports"] = "created"
    else:
        changes["shadow_comparison_reports"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_comparison_reports_run"
        " ON shadow_comparison_reports(shadow_run_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_comparison_reports_scope"
        " ON shadow_comparison_reports(environment_fingerprint,session_date,decision_at)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS shadow_comparison_reports_no_update
           BEFORE UPDATE ON shadow_comparison_reports
           BEGIN SELECT RAISE(ABORT,'shadow comparison reports are append-only'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS shadow_comparison_reports_no_delete
           BEFORE DELETE ON shadow_comparison_reports
           BEGIN SELECT RAISE(ABORT,'shadow comparison reports are append-only'); END"""
    )
    return changes


# ─── 风险决策的精确订单关联（v26 / R32-E1） ───────────────────────────────────
#
# 不变量::
#
#     paper_risk_decisions.order_id = 「这条风险决策说的是哪张订单」的精确事实
#
# 背景：``paper_risk_decisions`` 一直没有 order 引用，于是"某张订单的风控决策
# 是什么"只能靠 ``(account_id, code, side)`` 加时间序去猜最近一条 —— 这正是
# R32-E1 明令禁止的 provenance fabrication（表本身无唯一约束，每轮扫描都追加，
# "最近一条"既不是同一张订单，也不一定属于同一次尝试）。
#
# 本函数只做两件事：加一列 + 加索引。**绝不回填**：升级前那行属于哪张订单无法
# 从任何当前状态反推（``paper_orders`` 的 retry 链、status 变化、归档都可能已经
# 改写可见性），``order_id IS NULL`` 就是诚实的 legacy 归属状态。只有**写入当刻
# 手里确实有 order_id** 的调用点才盖这一列（当前是 Execution Authority 对已存在
# 订单的决策），其余写入点保持 NULL。


def ensure_risk_decision_order_linkage(conn):
    """v26：风险决策的精确 order 关联列（幂等，**绝不回填**）。"""
    changes = {"paper_risk_decisions": ensure_columns(
        conn, "paper_risk_decisions", {"order_id": "INTEGER"})}
    if "order_id" in table_columns(conn, "paper_risk_decisions"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_risk_decisions_order"
            " ON paper_risk_decisions(order_id) WHERE order_id IS NOT NULL"
        )
    return changes


#: Legal ``authority`` values for a risk-decision row's write-time provenance.
#: The vocabulary is a property of the table shape, so it lives with the schema
#: owner: every writer must state one of these, and no reader may ever infer an
#: authority from a table name, a decision string, a reason, a status, or the
#: mere presence of an order id.
RISK_DECISION_AUTHORITIES = (
    "EXECUTION", "RISK", "ENTRY", "ALLOCATION", "TIMING", "INTRADAY", "AUDIT",
)
RISK_DECISION_PROVENANCE_SCHEMA_VERSION = "risk-decision-provenance-v1"


def risk_decision_provenance(authority, decision_kind):
    """The single canonical shape of a risk decision's write-time provenance."""
    if authority not in RISK_DECISION_AUTHORITIES:
        raise ValueError(f"unknown risk decision authority: {authority!r}")
    return {
        "schema_version": RISK_DECISION_PROVENANCE_SCHEMA_VERSION,
        "authority": authority,
        "decision_kind": decision_kind,
    }


# ─── 策略健康快照（v27 / R33-A） ─────────────────────────────────────────────
#
# 不变量::
#
#     strategy_health_snapshots = 「某个 exact strategy version 在某个显式
#     observation window 下拥有哪些健康事实」的唯一追加式 owner
#
# R33-A 只建**事实层**：健康快照既不改 lifecycle，也不写正式账本，也不发布
# health_score / rank / tier 这类结论。快照一旦写入不可改（append-only trigger）；
# 历史证据不会因为「今天数据变全了」而愈合 —— 需要新事实就追加**新快照**。
#
# DDL 单一事实来源在本模块：migration 与 init_db 快路径调用同一个函数，
# 否则升级后的线上库永远没有这张表（健康采集只能 fail closed）。

STRATEGY_HEALTH_SNAPSHOT_COLUMNS = (
    "snapshot_id", "snapshot_fingerprint", "health_contract_version",
    "strategy_id", "strategy_version", "strategy_checksum",
    "observation_start", "observation_end", "window_identity",
    "lifecycle_state", "coverage_ratio", "evidence_json", "created_at",
)


def strategy_health_snapshot_ddl(table="strategy_health_snapshots"):
    """``strategy_health_snapshots`` 的规范 DDL（migration 与 ``init_db`` 共用）。"""
    return f"""
    CREATE TABLE IF NOT EXISTS {table}(
        snapshot_id TEXT PRIMARY KEY,
        snapshot_fingerprint TEXT NOT NULL,
        health_contract_version TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        strategy_version INTEGER NOT NULL,
        strategy_checksum TEXT NOT NULL,
        observation_start TEXT NOT NULL,
        observation_end TEXT NOT NULL,
        window_identity TEXT NOT NULL,
        lifecycle_state TEXT,
        coverage_ratio REAL NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(snapshot_id = snapshot_fingerprint)
    )
    """


def ensure_strategy_health_snapshots(conn):
    """v27：建策略健康快照追加表 + 索引 + append-only guard（幂等、不回填）。

    只创建自己的表；不 INSERT/UPDATE 任何正式账本，也不触碰 lifecycle 的任何表。
    """
    changes = {}
    if not table_columns(conn, "strategy_health_snapshots"):
        conn.execute(strategy_health_snapshot_ddl("strategy_health_snapshots"))
        changes["strategy_health_snapshots"] = "created"
    else:
        changes["strategy_health_snapshots"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_health_snapshots_version"
        " ON strategy_health_snapshots(strategy_id,strategy_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_health_snapshots_window"
        " ON strategy_health_snapshots(observation_start,observation_end)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS strategy_health_snapshots_no_update
           BEFORE UPDATE ON strategy_health_snapshots
           BEGIN SELECT RAISE(ABORT,'strategy health snapshots are append-only'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS strategy_health_snapshots_no_delete
           BEFORE DELETE ON strategy_health_snapshots
           BEGIN SELECT RAISE(ABORT,'strategy health snapshots are append-only'); END"""
    )
    return changes


def ensure_portfolio_runtime_snapshots(conn):
    """v30: append-only exact-cycle portfolio runtime fact snapshots."""
    changes = {}
    if not table_columns(conn, "portfolio_runtime_snapshots"):
        conn.execute("""CREATE TABLE portfolio_runtime_snapshots(
            snapshot_id TEXT PRIMARY KEY,
            snapshot_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(snapshot_id = snapshot_fingerprint)
        )""")
        changes["portfolio_runtime_snapshots"] = "created"
    else:
        changes["portfolio_runtime_snapshots"] = "ok"
    conn.execute("""CREATE TRIGGER IF NOT EXISTS portfolio_runtime_snapshots_no_update
        BEFORE UPDATE ON portfolio_runtime_snapshots
        BEGIN SELECT RAISE(ABORT,'portfolio runtime snapshots are append-only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS portfolio_runtime_snapshots_no_delete
        BEFORE DELETE ON portfolio_runtime_snapshots
        BEGIN SELECT RAISE(ABORT,'portfolio runtime snapshots are append-only'); END""")
    return changes


def ensure_portfolio_allocation_plans(conn):
    """v31: append-only R34-B multi-strategy allocation plans.

    不变量：``portfolio_allocation_plans`` 是「R34-B policy 为某个精确快照
    算出了哪份资源计划」的唯一追加式 owner。

    计划是**证据**，不是状态：表里只有不可变的、带指纹的单行记录。
    ``CHECK(plan_id = plan_fingerprint)`` 加上两个 append-only trigger，让
    ``latest`` / ``current`` / 覆盖写这类路径在 schema 层面就不可能存在。
    本函数只建表，不做任何回填，也不写正式账本。
    """
    changes = {}
    if not table_columns(conn, "portfolio_allocation_plans"):
        conn.execute("""CREATE TABLE portfolio_allocation_plans(
            plan_id TEXT PRIMARY KEY,
            plan_fingerprint TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(plan_id = plan_fingerprint)
        )""")
        changes["portfolio_allocation_plans"] = "created"
    else:
        changes["portfolio_allocation_plans"] = "ok"
    conn.execute("""CREATE TRIGGER IF NOT EXISTS portfolio_allocation_plans_no_update
        BEFORE UPDATE ON portfolio_allocation_plans
        BEGIN SELECT RAISE(ABORT,'portfolio allocation plans are append-only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS portfolio_allocation_plans_no_delete
        BEFORE DELETE ON portfolio_allocation_plans
        BEGIN SELECT RAISE(ABORT,'portfolio allocation plans are append-only'); END""")
    return changes


def ensure_order_allocation_provenance(conn):
    """Add nullable R34-C intent/plan evidence without guessing old orders.

    The formal ``db_migrate`` version owns this schema change. NULL remains the
    explicit unknown state for historical and not-yet-migrated orders.
    Active and archive columns are appended in the same order because retention
    archives rows with ``INSERT ... SELECT *``.
    """
    definitions = {
        "allocation_intent_kind": "TEXT",
        "portfolio_snapshot_id": "TEXT",
        "allocation_plan_id": "TEXT",
        "allocation_plan_fingerprint": "TEXT",
        "allocation_policy_version": "TEXT",
    }
    return {
        table: ensure_columns(conn, table, definitions)
        for table in ("paper_orders", "paper_orders_archive")
    }


# ─── 策略退休决策（v28 / R33-B） ─────────────────────────────────────────────
#
# 不变量::
#
#     strategy_retirement_decisions = 「某份健康快照给出了哪条 policy 建议」的
#     唯一追加式 owner
#
# R33-B 只产出**建议**：决策表是证据，不是状态。因此刻意没有
# current_retirement_state / latest_decision 这类列或表 —— 否则读路径会开始
# 把「最近一次建议」当成「当前退休状态」，而那正是 R33-B 明令禁止的诱导。
# 同 id 不同内容必须冲突，不允许 overwrite；需要新结论就追加新决策。
# 决策不写 lifecycle：本表不引用 strategy_lifecycle_* 任何列。


STRATEGY_RETIREMENT_DECISION_COLUMNS = (
    "decision_id", "decision_fingerprint", "snapshot_id", "snapshot_fingerprint",
    "strategy_id", "strategy_version", "decision_type", "policy_version",
    "evidence_json", "created_at",
)


def strategy_retirement_decision_ddl(table="strategy_retirement_decisions"):
    """``strategy_retirement_decisions`` 的规范 DDL（migration 与 ``init_db`` 共用）。"""
    return f"""
    CREATE TABLE IF NOT EXISTS {table}(
        decision_id TEXT PRIMARY KEY,
        decision_fingerprint TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        snapshot_fingerprint TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        strategy_version INTEGER NOT NULL,
        decision_type TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(decision_id = decision_fingerprint)
    )
    """


def ensure_strategy_retirement_decisions(conn):
    """v28：建策略退休决策追加表 + 索引 + append-only guard（幂等、不回填）。

    只创建自己的表；不 INSERT/UPDATE 任何正式账本，也不触碰 lifecycle 的任何表。
    """
    changes = {}
    if not table_columns(conn, "strategy_retirement_decisions"):
        conn.execute(strategy_retirement_decision_ddl("strategy_retirement_decisions"))
        changes["strategy_retirement_decisions"] = "created"
    else:
        changes["strategy_retirement_decisions"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_retirement_decisions_snapshot"
        " ON strategy_retirement_decisions(snapshot_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_retirement_decisions_version"
        " ON strategy_retirement_decisions(strategy_id,strategy_version,policy_version)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS strategy_retirement_decisions_no_update
           BEFORE UPDATE ON strategy_retirement_decisions
           BEGIN SELECT RAISE(ABORT,'strategy retirement decisions are append-only'); END"""
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS strategy_retirement_decisions_no_delete
           BEFORE DELETE ON strategy_retirement_decisions
           BEGIN SELECT RAISE(ABORT,'strategy retirement decisions are append-only'); END"""
    )
    return changes


# ─── 策略退休人工工作流（v29 / R33-C）──────────────────────────────────────
# Proposal 与 approval 都是不可变证据。运行态从确切 proposal、approval 以及
# lifecycle event/current exact state 推导；这里不建 current/latest 状态表。
STRATEGY_RETIREMENT_PROPOSAL_COLUMNS = (
    "proposal_id", "proposal_fingerprint", "decision_id", "decision_fingerprint",
    "snapshot_id", "snapshot_fingerprint", "strategy_id", "strategy_version",
    "strategy_checksum", "current_state", "target_state", "reason",
    "approval_status", "created_at", "evidence_json",
)

STRATEGY_RETIREMENT_APPROVAL_COLUMNS = (
    "approval_id", "approval_fingerprint", "proposal_id", "proposal_fingerprint",
    "operator_identity", "approval_action", "approved_at", "reason", "evidence_json",
)


def ensure_strategy_retirement_workflow(conn):
    """v29: create append-only proposal/approval evidence, without data backfill."""
    changes = {}
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_retirement_proposals(
            proposal_id TEXT PRIMARY KEY,
            proposal_fingerprint TEXT NOT NULL UNIQUE,
            decision_id TEXT NOT NULL,
            decision_fingerprint TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            snapshot_fingerprint TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version INTEGER NOT NULL CHECK(strategy_version > 0),
            strategy_checksum TEXT NOT NULL,
            current_state TEXT NOT NULL,
            target_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            approval_status TEXT NOT NULL CHECK(approval_status='PENDING_APPROVAL'),
            created_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            CHECK(proposal_id=proposal_fingerprint),
            CHECK(decision_id=decision_fingerprint)
        )
    """)
    changes["strategy_retirement_proposals"] = "created"
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_retirement_approvals(
            approval_id TEXT PRIMARY KEY,
            approval_fingerprint TEXT NOT NULL UNIQUE,
            proposal_id TEXT NOT NULL UNIQUE,
            proposal_fingerprint TEXT NOT NULL,
            operator_identity TEXT NOT NULL CHECK(length(trim(operator_identity)) > 0),
            approval_action TEXT NOT NULL CHECK(approval_action IN ('APPROVE','REJECT')),
            approved_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            CHECK(approval_id=approval_fingerprint)
        )
    """)
    changes["strategy_retirement_approvals"] = "created"
    for table in ("strategy_retirement_proposals", "strategy_retirement_approvals"):
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT,'{table} are append-only'); END"""
        )
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'{table} are append-only'); END"""
        )
    return changes


# ─── 策略候选台账（v33 / R35-A） ─────────────────────────────────────────────
#
# 不变量::
#
#     strategy_candidates = 「哪个 generator 在哪个显式 as-of 下，从哪个 **pinned
#     parent** 提出了哪一份 canonical strategy specification」的唯一追加式 owner
#
# 三条硬约束，都在 schema 层而不只是代码层：
#
# 1. ``CHECK(candidate_id = candidate_fingerprint)`` —— candidate 身份**就是**
#    canonical 指纹。没有"名字/描述/时间相近"这种第二套去重权威，也不可能出现
#    "语义相同但 ID 不同"的两个候选。
# 2. append-only trigger —— 已有候选不得 UPDATE/DELETE 变成另一个候选。改动任何
#    语义事实产生的是**新的候选行**（新的 candidate_id / fingerprint）。
# 3. 表里**没有**评估事实列（Sharpe / 收益 / 回撤 / 胜率 / promotion 结果）。
#    那些属于 R36 / R31 的事实，由各自的 owner 产生；一旦混进候选身份，同一个
#    候选就会因为"跑过一次"而变成另一个候选。
#
# 不变量之外还刻意不建 latest/current 视图或列：读路径只能按显式 candidate ID 取。

STRATEGY_CANDIDATE_COLUMNS = (
    "candidate_id", "candidate_fingerprint", "candidate_contract_version",
    "candidate_schema_version", "parent_strategy_id", "parent_strategy_version",
    "parent_strategy_checksum", "generator_type", "generator_version",
    "generator_contract_version", "hypothesis_id", "asof", "random_seed",
    "candidate_json", "created_at",
)

STRATEGY_CANDIDATE_PROPOSAL_COLUMNS = (
    "proposal_id", "candidate_id", "input_fingerprint", "proposal_json", "created_at",
)


def strategy_candidate_ddl(table="strategy_candidates"):
    """``strategy_candidates`` 的规范 DDL（migration 与 ``init_db`` 共用）。"""
    return f"""
    CREATE TABLE IF NOT EXISTS {table}(
        candidate_id TEXT PRIMARY KEY,
        candidate_fingerprint TEXT NOT NULL,
        candidate_contract_version TEXT NOT NULL,
        candidate_schema_version TEXT NOT NULL,
        parent_strategy_id TEXT,
        parent_strategy_version INTEGER,
        parent_strategy_checksum TEXT,
        generator_type TEXT NOT NULL,
        generator_version TEXT NOT NULL,
        generator_contract_version TEXT NOT NULL,
        hypothesis_id TEXT,
        asof TEXT NOT NULL,
        random_seed INTEGER,
        candidate_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(candidate_id = candidate_fingerprint),
        CHECK(length(candidate_id)=64),
        -- parent pin 三件套必须同时给出或同时缺席：半个 pin 无法证明绑的是哪一版。
        CHECK(
            (parent_strategy_id IS NULL AND parent_strategy_version IS NULL
             AND parent_strategy_checksum IS NULL)
            OR (parent_strategy_id IS NOT NULL AND parent_strategy_version > 0
                AND parent_strategy_checksum IS NOT NULL
                AND length(parent_strategy_checksum)=64)
        )
    )
    """


def strategy_candidate_proposal_ddl(table="strategy_candidate_proposals"):
    """``strategy_candidate_proposals`` 的规范 DDL（去重的证据侧，append-only）。"""
    return f"""
    CREATE TABLE IF NOT EXISTS {table}(
        proposal_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        proposal_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(candidate_id) REFERENCES strategy_candidates(candidate_id)
    )
    """


def ensure_strategy_candidates(conn):
    """v33: create the append-only strategy candidate ledger (idempotent, no backfill).

    只创建自己的两张表；不 INSERT/UPDATE 任何正式账本，不写 lifecycle，不写订单，
    也不发布任何评估结论。升级前的历史里没有候选概念，因此这里**没有回填** ——
    从既有策略反推"当时提出过哪些候选"是凭空捏造 provenance。
    """
    changes = {}
    if not table_columns(conn, "strategy_candidates"):
        conn.execute(strategy_candidate_ddl("strategy_candidates"))
        changes["strategy_candidates"] = "created"
    else:
        changes["strategy_candidates"] = "ok"
    if not table_columns(conn, "strategy_candidate_proposals"):
        conn.execute(strategy_candidate_proposal_ddl("strategy_candidate_proposals"))
        changes["strategy_candidate_proposals"] = "created"
    else:
        changes["strategy_candidate_proposals"] = "ok"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_candidates_parent"
        " ON strategy_candidates(parent_strategy_id,parent_strategy_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_candidates_generator"
        " ON strategy_candidates(generator_type,generator_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_candidate_proposals_candidate"
        " ON strategy_candidate_proposals(candidate_id,created_at)"
    )
    for table in ("strategy_candidates", "strategy_candidate_proposals"):
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT,'{table} are append-only'); END"""
        )
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'{table} are append-only'); END"""
        )
    return changes
