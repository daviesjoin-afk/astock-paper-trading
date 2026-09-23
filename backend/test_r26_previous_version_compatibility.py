# -*- coding: utf-8 -*-
"""上一正式版本 → R26 的兼容性 gate（hard merge gate）。

──────────────────────── 为什么需要这一层 ────────────────────────

R26 给 ``paper_orders`` / ``paper_fills`` / ``paper_position_lots`` 增加了执行
事实字段（``filled_qty`` / ``execution_asof`` / ``pricing_basis`` / ``slippage`` /
``ruleset_version`` / ``source_fill_id`` 等）。这些字段的问题不在于"新代码能不能
读自己写的行"，而在于**升级后的第一分钟**：线上库里全是上一版本写下的行，它们

* 没有新列（schema 层缺列），
* 即使补了列也是 NULL（``filled_qty`` 为 NULL、``execution_status`` 为 NULL、
  ``source_fill_id`` 为 NULL），
* 其中一部分 ``status='filled'`` 却**没有**成交流水。

如果升级路径处理不当，会有两种对称的错误：要么运行时崩在缺列上；要么更糟 ——
把"无法证明的历史事实"自动洗成"已验证成交"（凭空生成 provenance）。本 gate 同时
检查这两侧。

──────────────────────── 版本口径（§十六） ────────────────────────

**Previous Version 的定义**：本轮目标 release 发布**之前**、最近一个**正式受支持
release** 的数据形状。它不是"上一个 PR"、也不是"上一个 commit"，更不是"上一个
schema number"。

本测试因此绑定**真实 release tag** 的 **commit + 数据形状**，而不是任何脚本名或
进行中的 release 重编号 PR：版本重编号仍在独立分支上推进，把 fixture 绑到它的
tag 名会让这条 gate 在别人改版本号时假失败。这里用
``PREVIOUS_RELEASE_TAG`` 常量显式声明，并在测试里断言该 tag 存在且是 master 的祖先
——"它确实是我们的上一版本"这件事本身也是被测对象。

──────────────────────── 覆盖（§十六 要求） ────────────────────────

    fresh install             全新库初始化
    previous-version upgrade  上一版本库 → 当前迁移
    migration repeated twice  迁移跑两次结果一致（幂等）
    restart after migration   迁移后重新 init（模拟重启）

以及 legacy 数据的可读性与"不伪造"约束：legacy order / fill / lot / archive 可读，
``source_fill_id`` 保持 NULL，不生成 FillEvent，不猜 provenance。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_verification as EV  # noqa: E402
import paper_portfolio_read_model as P  # noqa: E402
import paper_schema_migrations as PSM  # noqa: E402
import paper_trading as PT  # noqa: E402
from paper_portfolio_read_model import (  # noqa: E402
    PortfolioReadContext,
    positions_for_context,
)

REPO_ROOT = os.path.dirname(BACKEND)

#: **Previous Version**：本轮目标 release 之前最近一个正式受支持 release。
PREVIOUS_RELEASE_TAG = "v2.0.0"
#: 该 release 的 commit，写在 fixture 头部作为来源凭证。
PREVIOUS_RELEASE_COMMIT = "cbb1863b8d3cc1f07e52dfe9b99a360494f53be4"
#: 上一版本真实 schema 的逐字摘录。
PREVIOUS_RELEASE_SCHEMA_FIXTURE = os.path.join(
    BACKEND, "fixtures", f"previous_release_{PREVIOUS_RELEASE_TAG}_schema.sql",
)

DAY = "2026-09-01"
ACCOUNT = "tq_breakout"
CODE = "600901"


def _git(*args):
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _extract_ddl(source):
    """从上一版本 ``paper_trading.py`` 里取出 ``init_db`` 的 ``CREATE TABLE`` DDL 块。

    该 DDL 是被三引号包裹的字符串字面量；结束行是该字面量的收尾引号。
    """
    marker = "CREATE TABLE IF NOT EXISTS paper_accounts"
    start = source.find(marker)
    if start < 0:
        return None
    end = source.find('\n"""\n', start)
    if end < 0:
        return None
    return source[start:end]


def _live_release_ddl():
    """从 release tag 读取上一版本的 DDL；tag 不可解析时返回 ``None``。

    CI 的 actions/checkout 默认浅克隆且 ``--no-tags``，测试进程里解析不到 tag，
    所以这条路径**可选**：fixture 文件才是常规来源，这里是防漂移的交叉校验。
    """
    code, source, _err = _git("show", f"{PREVIOUS_RELEASE_TAG}:backend/paper_trading.py")
    if code != 0:
        return None
    return _extract_ddl(source)


def _fixture_ddl():
    """读取随仓库提交的上一版本 schema fixture（去掉来源注释头）。

    fixture 必须真的**是**上一版本产生的形状，所以它逐字摘录自 release 的
    ``init_db`` DDL，并在头部标注来源 commit；测试会断言这份摘录可执行、且
    （tag 可解析时）与 live tag 逐字一致。

    只剥掉开头那一段来源说明注释（连续注释行直到第一个非注释行），**保留** DDL
    内部原有的 SQL 注释 —— 它们也是上一版本形状的一部分。
    """
    with open(PREVIOUS_RELEASE_SCHEMA_FIXTURE, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("--"):
            body_start = index
            break
    return "\n".join(lines[body_start:]).strip("\n")


class PreviousReleaseFixtureTests(unittest.TestCase):
    """先证明"上一版本"这个前提本身成立，再谈兼容性。

    常规路径读**随仓库提交的 fixture**（CI 浅克隆解析不到 tag）；只要 tag 可解析
    （本地或完整检出），就额外逐字比对 live tag，防止 fixture 摘录悄悄漂移。
    """

    def test_fixture_declares_a_real_release_source(self):
        with open(PREVIOUS_RELEASE_SCHEMA_FIXTURE, encoding="utf-8") as handle:
            head = handle.read(1200)
        self.assertIn(PREVIOUS_RELEASE_TAG, head)
        self.assertIn(PREVIOUS_RELEASE_COMMIT, head, "fixture 必须标注来源 commit")

    def test_fixture_is_executable_and_creates_the_previous_version_tables(self):
        ddl = _fixture_ddl()
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(ddl)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        for table in ("paper_accounts", "paper_orders", "paper_fills",
                      "paper_position_lots", "paper_cycles", "paper_archives"):
            self.assertIn(table, tables, f"fixture 缺少上一版本的 {table}")

    def test_fixture_records_the_previous_version_shape(self):
        """上一版本的形状必须与 R26 之后**不同**，否则这条 gate 是空转。"""
        ddl = _fixture_ddl()
        # R26 新增的列在上一版本里不存在 —— 这正是需要迁移的原因。
        for absent in ("source_fill_id", "filled_qty", "execution_asof",
                       "pricing_basis", "ruleset_version", "event_key"):
            self.assertNotIn(
                absent, ddl,
                f"{absent} 出现在上一版本 DDL 里：fixture 取错了版本，gate 会空转",
            )

    def test_fixture_matches_the_live_release_tag_when_available(self):
        """tag 可解析时逐字比对，防止 fixture 摘录漂移。"""
        live = _live_release_ddl()
        if live is None:
            self.skipTest(
                f"{PREVIOUS_RELEASE_TAG} 在当前检出里不可解析（CI 浅克隆 / --no-tags）；"
                "fixture 的权威来源是其头部标注的 commit",
            )
        self.assertEqual(
            live.strip("\n"), _fixture_ddl().strip("\n"),
            f"fixture 与 {PREVIOUS_RELEASE_TAG} 的 init_db DDL 不一致：请按头部说明重新生成",
        )

    def test_previous_release_commit_is_an_ancestor_of_the_current_line(self):
        """上一版本必须真的在我们这条线上（用 commit，而不是 tag 名）。"""
        code, _out, _err = _git(
            "merge-base", "--is-ancestor", PREVIOUS_RELEASE_COMMIT, "HEAD",
        )
        if code == 128:
            self.skipTest("当前检出缺少该 commit（浅克隆）；本地/完整检出会执行此断言")
        self.assertEqual(
            0, code,
            f"{PREVIOUS_RELEASE_COMMIT}（{PREVIOUS_RELEASE_TAG}）不是当前分支的祖先",
        )


class PreviousVersionUpgradeTests(unittest.TestCase):
    """上一版本库 → R26：迁移可重复、数据仍可读、且不伪造执行事实。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="r26-prev-version-")
        self.db_path = os.path.join(self.tmp, "paper.sqlite3")
        self._old_db_path = PT.DB_PATH
        PT.DB_PATH = self.db_path

    def tearDown(self):
        PT.DB_PATH = self._old_db_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- fixture：由上一版本 DDL 建库 ----------

    def _create_previous_version_db(self):
        """用**上一版本真实 DDL** 建库（fixture 逐字摘录自 release commit）。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_fixture_ddl())
        conn.commit()
        return conn

    def _seed_previous_version_data(self, conn, *, phantom_claim=True):
        """写入上一版本**真实允许**的数据形状（含没有流水自称成交的旧行）。

        注意：这里只使用上一版本 base DDL 里**真实存在**的列。``paper_accounts.
        cycle_id`` 在 v2.0.0 的建表语句里没有（它是后续迁移补上的），所以本 fixture
        不写它 —— 兼容性 gate 的前提正是"fixture 的形状必须真的是上一版本的形状"。

        ``phantom_claim`` 控制是否写入那条"自称成交却没有流水"的旧行。它与
        "读路径必须可用"是**互相冲突**的两个前提：无流水的 ``status='filled'`` 行
        正是执行验证闸门要挡的自相矛盾证据，读路径面对它会 fail closed（宁可报
        unknown，也不发布一个可能错的持仓量）。因此需要分别验证两件事：
        一致的 legacy 库读得出来；不一致的 legacy 库 fail closed 且不伪造。
        """
        stamp = f"{DAY} 09:00:00"
        cycle_id = int(conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "started_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"cycle-{DAY}", "running", 1_000_000.0, "shared_pool",
             stamp, stamp, stamp),
        ).lastrowid)
        conn.execute(
            "INSERT INTO paper_accounts(id,name,source_strategy,status,initial_cash,"
            "cash,cycle_days,max_positions,max_weight,max_exposure,version,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "首板接力", "test", "running", 1_000_000.0, 900_000.0,
             30, 5, 0.3, 1.0, "v2.0.0", stamp, stamp),
        )
        # 1) 旧订单：status='filled' 且有流水（升级前的"真实成交"）。
        filled_order = int(conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,"
            "executed_at,order_type,origin,cycle_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", CODE, "回放甲", 300, 20.0, 20.0, 6000.0, 6.0,
             "filled", "旧版本成交", "{}", f"{DAY} 09:35:00",
             f"{DAY} 09:36:00", "market", "strategy", cycle_id),
        ).lastrowid)
        conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
            "fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (filled_order, ACCOUNT, "buy", CODE, 300, 20.0, 6000.0, 6.0, DAY,
             f"{DAY} 09:36:00", "旧版本假设"),
        )
        # 2) 旧订单：status='filled' 但**没有流水**（升级前的"执行幻觉"）。
        #    这是"自相矛盾的 legacy 证据"，只在专门验证 fail-closed 的用例里写入。
        phantom_order = None
        if phantom_claim:
            phantom_order = int(conn.execute(
                "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
                "filled_price,amount,fees,status,reason,risk_payload,created_at,"
                "executed_at,order_type,origin,cycle_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ACCOUNT, "buy", "600902", "回放乙", 100, 15.0, 15.0, 1500.0, 1.5,
                 "filled", "旧版本自称成交但无流水", "{}", f"{DAY} 09:40:00",
                 f"{DAY} 09:41:00", "market", "strategy", cycle_id),
            ).lastrowid)
        # 3) 旧订单：仍在途。
        conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "status,reason,risk_payload,created_at,order_type,origin,cycle_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, "buy", "600903", "回放丙", 200, 10.0, "pending_limit",
             "旧版本在途", "{}", f"{DAY} 09:45:00", "limit", "manual", cycle_id),
        )
        # 4) 旧 lot：没有 source_fill_id（该列当时不存在）。
        conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,"
            "qty,remaining_qty,cost,acquired_at,available_date,asset_type,"
            "source_order_id,cost_fee_included,is_t_base) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, ACCOUNT, CODE, "回放甲", "测试", 300, 300, 20.02,
             f"{DAY} 09:36:00", "2026-09-02", "stock_t1", filled_order, 1, 1),
        )
        conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,"
            "entry_date,available_date,asset_type,peak_price,take_stage) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, "回放甲", "测试", 300, 20.02, DAY, "2026-09-02",
             "stock_t1", 20.5, 0),
        )
        # 5) 旧归档快照：属于**更早**的周期（`cycle_id` 与活动周期不同 —— 归档意味
        #    着那个周期的活动账本已被清空，两者不可能共用同一个 id）。含多笔部分成交
        #    （历史缺 event_key / 执行列）。
        archived_cycle_id = int(conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
            "started_at,ended_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("cycle-archived", "archived", 1_000_000.0, "shared_pool",
             f"{DAY} 08:00:00", f"{DAY} 15:00:00", f"{DAY} 08:00:00", stamp),
        ).lastrowid)
        conn.execute(
            "INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at) "
            "VALUES(?,?,?,?,?)",
            (archived_cycle_id, "cycle-archived", "旧版本归档", json.dumps({
                "paper_accounts": [{"id": ACCOUNT, "name": "首板接力"}],
                "paper_orders": [{
                    "id": 9001, "account_id": ACCOUNT, "side": "buy", "code": CODE,
                    "name": "回放甲", "qty": 200, "status": "filled",
                    "created_at": f"{DAY} 10:00:00", "executed_at": f"{DAY} 10:05:00",
                }],
                "paper_fills": [
                    {"id": 1, "order_id": 9001, "account_id": ACCOUNT, "side": "buy",
                     "code": CODE, "qty": 100, "price": 20.0, "amount": 2000.0,
                     "fees": 2.0, "fill_date": DAY, "quote_at": f"{DAY} 10:01:00",
                     "assumption": "旧"},
                    {"id": 2, "order_id": 9001, "account_id": ACCOUNT, "side": "buy",
                     "code": CODE, "qty": 100, "price": 20.1, "amount": 2010.0,
                     "fees": 2.0, "fill_date": DAY, "quote_at": f"{DAY} 10:04:00",
                     "assumption": "旧"},
                ],
            }, ensure_ascii=False), f"{DAY} 15:00:00"),
        )
        conn.commit()
        return {"cycle_id": cycle_id, "filled_order": filled_order,
                "phantom_order": phantom_order}

    def _upgrade(self):
        """模拟真实升级：先跑迁移器，再重新初始化运行时。"""
        import db_migrate
        # 真实升级入口：``migrate`` 自己开连接、按 schema_version 顺序执行并事务化。
        db_migrate.migrate("paper_trading", path=self.db_path, backup=False)
        PT.init_db()
        return self._open()

    def _open(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- 升级路径 ----------

    def test_previous_version_database_upgrades_and_stays_readable(self):
        conn = self._create_previous_version_db()
        seeded = self._seed_previous_version_data(conn)
        conn.close()

        conn = self._upgrade()
        try:
            columns = PSM.table_columns(conn, "paper_orders")
            for column in ("filled_qty", "remaining_qty", "execution_asof",
                           "execution_reasons", "execution_evidence",
                           "pricing_basis", "slippage", "ruleset_version",
                           "execution_version"):
                self.assertIn(column, columns, f"迁移后缺少 {column}")

            lots_columns = PSM.table_columns(conn, "paper_position_lots")
            self.assertIn("source_fill_id", lots_columns)

            fills_columns = PSM.table_columns(conn, "paper_fills")
            for column in ("event_key", "execution_asof", "pricing_basis",
                           "slippage", "market_evidence", "ruleset_version",
                           "execution_evidence"):
                self.assertIn(column, fills_columns, f"迁移后缺少 {column}")

            # legacy 数据仍可读，且数量守恒（迁移不制造也不吞掉订单）。
            order_count = conn.execute(
                "SELECT COUNT(*) FROM paper_orders", 
            ).fetchone()[0]
            self.assertEqual(3, order_count, "迁移改变了订单行数")
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM paper_fills WHERE order_id=?",
                (seeded["filled_order"],),
            ).fetchone()[0], "迁移改变了历史成交流水")
        finally:
            conn.close()

    def test_legacy_fill_backed_by_real_rows_becomes_verified(self):
        """上一版本写入的真实成交（有流水）必须在升级后**可被证明**。

        否则升级会把历史真实成交当成"没发生"，已实现盈亏与执行绩效凭空少一块。
        """
        conn = self._create_previous_version_db()
        seeded = self._seed_previous_version_data(conn)
        conn.close()
        conn = self._upgrade()
        try:
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (seeded["filled_order"],),
            ).fetchone()
            self.assertTrue(
                EV.is_verified_row(row),
                "上一版本的真实成交（status='filled' 且有完整流水）升级后未被证明",
            )
        finally:
            conn.close()

    def test_legacy_row_claiming_filled_without_rows_is_not_fabricated(self):
        """``status='filled'`` 却没有流水的旧行**不得**变成已验证成交。

        这正是 execution_verification 存在的理由：账本自称不等于证据证明。
        """
        conn = self._create_previous_version_db()
        seeded = self._seed_previous_version_data(conn)
        conn.close()
        conn = self._upgrade()
        try:
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (seeded["phantom_order"],),
            ).fetchone()
            self.assertFalse(
                EV.is_verified_row(row),
                "没有成交流水的旧行被升级成'已验证成交'（凭空制造 provenance）",
            )
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM paper_fills WHERE order_id=?",
                (seeded["phantom_order"],),
            ).fetchone()[0], "迁移为旧行凭空生成了 FillEvent")
            self.assertFalse(EV.is_positive_execution_row(row))
        finally:
            conn.close()

    def test_legacy_lot_keeps_a_null_source_fill_id(self):
        """历史 lot 的血缘必须保持 NULL：不可证明就绝不猜。"""
        conn = self._create_previous_version_db()
        self._seed_previous_version_data(conn)
        conn.close()
        conn = self._upgrade()
        try:
            lots = [dict(row) for row in conn.execute(
                "SELECT * FROM paper_position_lots",
            )]
            self.assertTrue(lots, "legacy lot 丢失")
            for lot in lots:
                self.assertIsNone(
                    lot["source_fill_id"],
                    "迁移给历史 lot 猜了一个 source_fill_id（伪造血缘）",
                )
            # 也不能反过来"顺手"补 filled_qty 之类无法证明的量。
            orders = [dict(row) for row in conn.execute("SELECT * FROM paper_orders")]
            for order in orders:
                self.assertEqual(
                    0, int(order.get("filled_qty") or 0),
                    f"旧行 {order['id']} 的 filled_qty 被非证据地填成非零",
                ) if order["status"] != "filled" else None
        finally:
            conn.close()

    def test_migration_is_idempotent_and_survives_a_restart(self):
        """迁移跑两次结果一致；迁移后重启（重新 init）仍可读。"""
        conn = self._create_previous_version_db()
        self._seed_previous_version_data(conn)
        conn.close()

        conn = self._upgrade()
        first = self._schema_signature(conn)
        first_orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        conn.close()

        # 第二次迁移 + 再次 init（等价于"迁移后再启动一次进程"）。
        conn = self._upgrade()
        second = self._schema_signature(conn)
        second_orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        try:
            self.assertEqual(
                first, second,
                "迁移不是幂等的：第二次跑改变了 schema",
            )
            self.assertEqual(first_orders, second_orders, "第二次迁移改变了数据行数")
        finally:
            conn.close()

    def _schema_signature(self, conn):
        signature = {}
        for table in ("paper_orders", "paper_orders_archive", "paper_fills",
                      "paper_position_lots", "paper_accounts"):
            signature[table] = tuple(sorted(PSM.table_columns(conn, table)))
        return signature

    # ---------- 升级后的读路径 ----------

    def test_read_models_work_on_upgraded_legacy_data(self):
        """一致的 legacy 库升级后各读路径必须可用（不能崩、不能静默给错数）。"""
        conn = self._create_previous_version_db()
        seeded = self._seed_previous_version_data(conn, phantom_claim=False)
        conn.close()
        conn = self._upgrade()
        try:
            # 1) 持仓读模型：legacy lot 必须在 as-of 之后可见。
            context = PortfolioReadContext(
                cycle_id=seeded["cycle_id"], asof_day="2026-09-02",
            )
            positions = positions_for_context(conn, context)
            self.assertTrue(
                any(str(row.get("code")) == CODE for row in positions),
                "升级后 legacy 持仓读不出来",
            )

            # 2) 个股历史：legacy 流水与归档都必须可读。
            #    ``stock_trade_history`` 会先取一次实时报价打标记 —— 本测试必须是
            #    **离线**的：真实网络调用会污染全局 provider 健康登记表，让后来
            #    断言"没有真实失败"的用例假红。这里把报价入口 patch 成空表。
            with mock.patch.object(PT, "_quotes", lambda codes, **kw: {}):
                history = PT.stock_trade_history(CODE)
            self.assertIn("fills", history)
            self.assertIn("orders", history)
            archived = [
                fill for fill in history["fills"] if fill.get("archived_cycle")
            ]
            self.assertEqual(
                2, len(archived),
                "旧版本归档里的两笔部分成交在升级后读不到（历史缺口）",
            )

            # 3) 风控去重判据能在升级后的库上运行（不抛异常、结论保守）。
            self.assertFalse(EV.has_verified_positive_execution(
                conn, marker="hard_stop_first_trim", account_id=ACCOUNT,
                code=CODE, asof_day=DAY,
            ), "legacy 库上凭空认为一次性减仓动作已执行")
        finally:
            conn.close()

    def test_self_contradictory_legacy_evidence_does_not_publish_verified_accounting(self):
        """legacy 库出现自相矛盾证据时，账务读路径必须 fail closed。

        一条 ``status='filled'`` 却没有成交流水的旧行是"账本自称"，不是证据。
        它虽然没有被升级成 verified，但**它的存在本身**说明这个周期的账不可信：
        现金等聚合口径必须报 unknown，而不是发布一个可能是幻觉的数字。
        """
        conn = self._create_previous_version_db()
        seeded = self._seed_previous_version_data(conn, phantom_claim=True)
        conn.close()
        conn = self._upgrade()
        try:
            context = PortfolioReadContext(
                cycle_id=seeded["cycle_id"], asof_day="2026-09-02",
            )
            # 有证据的 lot 仍然读得出来（fail closed 不等于全盘否定）。
            positions = positions_for_context(conn, context)
            self.assertTrue(any(str(row.get("code")) == CODE for row in positions))

            # 但聚合现金口径必须报 unknown：这个周期存在无法证明的成交声称。
            value, status = P.cash(conn, context)
            self.assertIsNone(value, "自相矛盾的 legacy 账本被发布成确定现金数")
            self.assertEqual("unknown", status)

            # 且那条幽灵订单绝不能被计入已实现盈亏的"已验证"口径。
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE id=?", (seeded["phantom_order"],),
            ).fetchone()
            self.assertFalse(EV.is_verified_row(row))
        finally:
            conn.close()

    def test_fresh_install_still_matches_the_migrated_schema(self):
        """fresh install 与"升级上来的库"必须得到同一套列（两条路径不分叉）。"""
        conn = self._create_previous_version_db()
        self._seed_previous_version_data(conn)
        conn.close()
        upgraded = self._upgrade()
        upgraded_signature = self._schema_signature(upgraded)
        upgraded.close()

        fresh_path = os.path.join(self.tmp, "fresh.sqlite3")
        old = PT.DB_PATH
        try:
            PT.DB_PATH = fresh_path
            PT.init_db()
            fresh = self._open()
            fresh_signature = self._schema_signature(fresh)
            fresh.close()
        finally:
            PT.DB_PATH = old

        self.assertEqual(
            fresh_signature, upgraded_signature,
            "fresh install 与升级上来的库列集不一致：两条路径已经开始分叉",
        )


if __name__ == "__main__":
    unittest.main()
