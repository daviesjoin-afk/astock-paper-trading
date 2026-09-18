#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量版本化数据库迁移器（schema_version 表 + 顺序迁移脚本）。

设计原则：
- 每个数据库维护一张 schema_version 表（db_name, version, applied_at, description）
- 迁移脚本按版本号顺序执行，已应用版本跳过
- 每个迁移在事务中执行，失败回滚
- 首次应用待迁移项前创建一致性 SQLite 备份，便于整库恢复
- 零第三方依赖，纯 sqlite3

用法：python db_migrate.py [paper_trading|adaptive_learning|all] [--dry-run] [--no-backup]
"""
import datetime as dt
import os
import sqlite3
import sys

import adaptive_selection_compat as selection_compat
import paper_schema_migrations as paper_schema
import strategy_registry

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import data_paths
CACHE_DIR = data_paths.data_dir()

DB_PATHS = {
    "paper_trading": os.path.join(CACHE_DIR, "paper_trading.sqlite3"),
    "adaptive_learning": os.path.join(CACHE_DIR, "adaptive_learning.sqlite3"),
}


def _ensure_strategy_versioning(conn):
    strategy_registry.ensure_schema(conn)
    return paper_schema.ensure_strategy_reference_columns(conn)


def _backfill_execution_verification(conn):
    """按**证据**回填执行验证结论（v11 建列之后的第二步，幂等）。

    只处理 ``execution_status IS NULL`` 的行，逐行由
    :func:`execution_verification.backfill_legacy_orders` 判定：

    * 有完整成交流水证据 → ``verified``（升级前由生产写路径落库、却因为没有
      盖章而从统计里消失的**真实成交**，一次算清）；
    * 没有证据或证据不足 → ``unknown``，**绝不**因为 ``status='filled'`` 升级。

    只把"账本自称"升级成"证据证明"是这一层唯一禁止的事；按证据判定本身就是
    闸门的定义，因此回填不是数据改写而是结论材料化。
    """
    import execution_verification as EV

    return EV.backfill_legacy_orders(conn)


def _ensure_tradability_archive(conn):
    """v13：历史可交易性事实资产表。

    纯新增（``CREATE TABLE IF NOT EXISTS``），既不新增既有表字段也不回填历史行，
    因此重复执行是无副作用的；表结构由
    :func:`tradability_archive.ensure_schema` 持有，迁移只负责把它挂进正式
    版本链，避免"运行时建表"的第二个真相来源。
    """
    import tradability_archive as TA

    return TA.ensure_schema(conn)


def _ensure_tradability_ingestion_runs(conn):
    """v14：历史可交易性摄取运行审计表。

    纯新增（``CREATE TABLE IF NOT EXISTS``），不触碰 archive 核心表语义；
    表结构由 :func:`tradability_ingestion.ensure_ingestion_schema` 持有。
    """
    import tradability_ingestion as TI

    return TI.ensure_ingestion_schema(conn)


def _ensure_tradability_observation_ledger(conn):
    """v15：可交易性**观察台账**（append-only）。

    记录"我们什么时候看到 / 尝试看到这些事实"，与 v13 的"市场事实是什么"严格分开。
    纯新增（``CREATE TABLE IF NOT EXISTS``），**不触碰** ``historical_tradability_archive``
    的任何行，也**不**给历史行伪造 ``first_seen_at``：升级前那些行真实的"系统首次观察到
    的时间"无法从 archive 反推（``created_at`` 只覆盖 evidence 行，且完全无法表达
    unknown / error 观察），因此一律诚实标为 ``legacy_observation_unknown``。
    """
    import tradability_observation_ledger as OL

    return OL.ensure_ledger_schema(conn)


def _ensure_tradability_shadow_table(conn):
    """v16：Shadow 比对结果表（可选持久化）。

    独立表，与 archive / ingestion audit / orders / fills / positions / 学习表完全隔离。
    唯一身份含 ``validation_as_of``，所以不同知识时点的验证各自成行。
    """
    import tradability_shadow as TS

    return TS.ensure_shadow_schema(conn)


def _ensure_tradability_archive_observation_links(conn):
    """v17：archive 事实行的观察来源链接表（append-only，纯新增）。

    记录"这条 archive 事实行是由哪次 ingestion run、连同哪个 observation event 一起
    写入的"。这是区分「真正 pre-ledger 历史行」与「ledger-era 行」的唯一可靠依据：
    fingerprint 相等只能证明内容相同（一条旧行后来被重新观察到同内容证据时指纹也会
    相同），而本表证明的是**同一个 transaction 里一起产生**。

    **绝不回填历史行**：升级前的 archive 行没有链接，那就是"原始观察时间不可知"，
    诚实答案是 ``legacy_observation_unknown``。表结构由
    :func:`tradability_observation_ledger.ensure_archive_link_schema` 持有。
    """
    import tradability_observation_ledger as OL

    return OL.ensure_archive_link_schema(conn)


def _ensure_order_cycle_provenance(conn):
    """v18：订单不可变周期归属（write-time fact，纯新增列 + guard，绝不回填）。

    只给 ``paper_orders`` / ``paper_orders_archive`` 加 ``cycle_id`` 并安装 guard；
    **不** UPDATE 任何既有行。升级前的订单属于哪个周期无法从当前状态反推，
    ``cycle_id IS NULL`` 就是诚实的 legacy provenance 状态 —— 任何 backfill 都是
    把"不知道"洗白成"知道"。表结构与 guard 由
    :func:`paper_schema_migrations.ensure_order_cycle_provenance` 持有。
    """
    return paper_schema.ensure_order_cycle_provenance(conn)


# 迁移注册表：db_name -> [(version, description, sql_or_callable), ...]
MIGRATIONS = {
    "paper_trading": [
        (1, "创建 schema_version 表", """
        CREATE TABLE IF NOT EXISTS schema_version(
            db_name TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            description TEXT
        );
        """),
        (2, "补齐订单、持仓和账户兼容字段", paper_schema.ensure_paper_columns),
        (3, "补齐运行时租约与 fencing 字段", paper_schema.ensure_runtime_lease_columns),
        (4, "补齐点火影子表与索引", paper_schema.ensure_ignition_shadow_table),
        (5, "创建动态策略定义与生命周期表", strategy_registry.ensure_schema),
        (6, "创建不可变策略版本与交易证据版本戳", _ensure_strategy_versioning),
        (7, "新增可执行策略 DSL 定义字段", strategy_registry.ensure_schema),
        (8, "新增订单重试血缘字段 retry_of_order_id", paper_schema.ensure_order_lineage_column),
        (9, "新增风险放大提案生命周期字段", paper_schema.ensure_proposal_lifecycle_columns),
        # PR-1.1：PR #107 修好了代码默认值（individual_mom5_min 2.0 -> 0.02），
        # 但历史自进化 overlay 已把旧的 percentage-point 值持久化进
        # paper_accounts.params，运行时会覆盖代码默认值。这里做一次性数据兼容
        # 迁移：只修 sentiment_pioneer 且恰为 2.0 的情形，同事务写 audit。
        # 幂等由 schema_version 保证；重复启动 migrated=0 且不产生重复 audit。
        (10, "迁移 legacy 选股动量 overlay 单位（2.0 -> 0.02）",
         selection_compat.migrate_legacy_selection_units),
        # PR-150 wiring：执行验证闸门三列。历史行保持 NULL（= 未验证），
        # 绝不因为 status='filled' 就自动升级为真实成交。
        (11, "新增执行验证闸门字段（execution_status/verified/evidence_source）",
         paper_schema.ensure_execution_verification_columns),
        # PR-150 wiring（补完）：把历史行的验证结论按**证据**一次性算清。没有这步，
        # 升级前由生产写路径落库的真实成交会永久停在 NULL，被闸门当成"没有证据"
        # 而从已实现盈亏 / NAV / 执行绩效里消失。
        (12, "按成交流水证据回填执行验证结论（幂等）", _backfill_execution_verification),
        # 历史可交易性事实资产层：历史日期上"这只票当时是否真的可交易"的证据。
        # 纯新增表，不改写任何既有行；Provider 不得直接建表，一律走本迁移。
        (13, "新增历史可交易性事实资产表（幂等）", _ensure_tradability_archive),
        # 历史可交易性证据摄取运行审计：记录每次 backfill 的 provider 集合、
        # 计数、冲突、unprovable 与指纹。纯新增，不触碰 archive 核心表。
        (14, "新增历史可交易性摄取运行审计表（幂等）", _ensure_tradability_ingestion_runs),
        # 可交易性观察台账：记录"我们什么时候看到/尝试看到这些事实"（含 provider
        # unknown / error）。纯新增；**绝不**给升级前的 archive 历史行回填 first_seen_at
        # ——那是时间旅行，真实观察时间无法从 archive 反推。
        (15, "新增可交易性观察台账（幂等，append-only）",
         _ensure_tradability_observation_ledger),
        # Shadow 比对结果（可选持久化，v2 身份含 validation_as_of）。与生产表完全隔离。
        (16, "新增可交易性 Shadow 比对结果表（幂等）", _ensure_tradability_shadow_table),
        # 行级 provenance 链接：archive 事实行 ↔ 产生它的 observation event。
        # 纯新增、append-only；**绝不**回填历史行——升级前的 archive 行没有这种链接，
        # 那就是"原始观察时间不可知"，编一条假链接才是真正的错误。只为未来 ingestion
        # 建立可靠 provenance（见 issue #161 §六/§七/§二十四）。
        (17, "新增 archive 事实行观察来源链接表（幂等，append-only）",
         _ensure_tradability_archive_observation_links),
        # 订单不可变周期归属：给 paper_orders / paper_orders_archive 同时加 cycle_id
        # 并安装 guard（新订单必须带真实 cycle、写入后不可更改）。**绝不回填历史行**：
        # 升级前的订单属于哪个周期无法从当前状态反推（paper_accounts.cycle_id 是可变
        # 重绑定，paused 周期 started_at 为 NULL），cycle_id IS NULL 就是诚实的
        # legacy provenance 状态。Migration 本身绝不允许"提高历史 coverage"。
        (18, "新增订单不可变周期归属字段 cycle_id（幂等，不回填）",
         _ensure_order_cycle_provenance),
    ],
    "adaptive_learning": [
        (1, "创建 schema_version 表", """
        CREATE TABLE IF NOT EXISTS schema_version(
            db_name TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            description TEXT
        );
        """),
    ],
}


def _current_version(conn, db_name):
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE db_name=?", (db_name,)
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _run_operation(conn, operation):
    if callable(operation):
        result = operation(conn)
        if result is False:
            raise RuntimeError("迁移操作未完成")
        return
    statement = []
    for char in operation:
        statement.append(char)
        if char == ";" and sqlite3.complete_statement("".join(statement)):
            sql = "".join(statement).strip()
            if sql:
                conn.execute(sql)
            statement = []
    sql = "".join(statement).strip()
    if sql and not sql.startswith("--"):
        conn.execute(sql)


def _backup_database(conn, path, current_version):
    """Create a consistent pre-migration SQLite snapshot using the backup API."""
    root, extension = os.path.splitext(path)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_path = f"{root}.pre-v{current_version}-{stamp}{extension or '.sqlite3'}"
    backup = sqlite3.connect(backup_path, timeout=30)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    return backup_path


def migrate(db_name, apply=True, path=None, backup=True):
    if db_name not in DB_PATHS:
        print(f"未知数据库: {db_name}")
        return
    path = path or DB_PATHS[db_name]
    if not os.path.exists(path):
        print(f"数据库不存在（跳过）: {path}")
        return
    conn = sqlite3.connect(path, timeout=30)
    try:
        current = _current_version(conn, db_name)
        pending = [m for m in MIGRATIONS.get(db_name, []) if m[0] > current]
        if not pending:
            print(f"[{db_name}] schema 已是最新 (v{current})")
            return
        if apply and backup:
            backup_path = _backup_database(conn, path, current)
            print(f"[{db_name}] ✓ 已创建迁移前备份: {backup_path}")
        for version, desc, sql in sorted(pending, key=lambda x: x[0]):
            if not apply:
                print(f"[{db_name}] 待应用 v{version}: {desc}")
                continue
            try:
                conn.execute("BEGIN")
                _run_operation(conn, sql)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version(db_name, version, applied_at, description) VALUES(?,?,datetime('now'),?)",
                    (db_name, version, desc),
                )
                conn.commit()
                print(f"[{db_name}] ✓ 已应用 v{version}: {desc}")
            except Exception as exc:
                conn.rollback()
                print(f"[{db_name}] ✗ v{version} 失败: {exc}（事务回滚）")
                raise
    finally:
        conn.close()


def main():
    targets = [arg for arg in sys.argv[1:] if arg not in {"--dry-run", "--no-backup"}] or ["all"]
    apply = "--dry-run" not in sys.argv
    backup = "--no-backup" not in sys.argv
    for target in targets:
        if target == "all":
            for db_name in DB_PATHS:
                migrate(db_name, apply=apply, backup=backup)
        else:
            migrate(target, apply=apply, backup=backup)


if __name__ == "__main__":
    main()
