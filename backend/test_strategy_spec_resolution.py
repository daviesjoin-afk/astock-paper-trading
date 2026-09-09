# -*- coding: utf-8 -*-
"""PR-39：清除剩余不安全的 ``ACCOUNT_SPECS[...]`` 直接下标。

背景：五套内置策略的常量表 ``ACCOUNT_SPECS`` 里没有用户自建策略的账户，
任何 ``ACCOUNT_SPECS[account_id]`` / ``ACCOUNT_SPECS[account['id']]`` 在
用户策略上都会 **KeyError**。PR-35/37 已经把大多数生产路径改走
``_spec_for()``，但历史数据 stale 判定分支还留着一处直接下标——用户策略
一旦落到"历史 K 线滞后"分支，整轮收盘扫描会直接抛异常而不是给出可读
的 blocked 原因。

本测试锁住两件事：
1. 源码门禁：用户可达路径不允许再出现 ``ACCOUNT_SPECS[`` 直接下标
   （仅内置集合的构造与文档字符串豁免）；
2. 用户 DSL 策略的 stale-factor E2E：factor lag 超限必须返回 blocked +
   可读 reason，不得抛 KeyError，不得产生可执行 signal 或任何 order。
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import unittest

import paper_trading as PT
import runtime_settings as RSET
import strategy_registry as SR
import strategy_runtime as SRT
import test_production_path_golden_replay as G

BACKEND = os.path.dirname(os.path.abspath(__file__))
STRATEGY_ID = "stale_factor_gamma"

_SUBSCRIPT = re.compile(r"ACCOUNT_SPECS\s*\[")
# 豁免：只在内置 id 集合上求值的构造式（不可能命中用户策略）。
_ALLOWED_PATTERNS = (
    re.compile(r"ACTIVE_ACCOUNT_SPECS\s*=\s*\{.*ACCOUNT_SPECS\[.*for account_id in ACTIVE_ACCOUNT_IDS"),
)


def _iter_backend_sources():
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        yield name, os.path.join(BACKEND, name)


def _is_docstring_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("#") or stripped.startswith(("``", "``ACCOUNT_SPECS"))


class AccountSpecSubscriptGuardTests(unittest.TestCase):
    """源码门禁：用户可达路径禁止直接下标 ACCOUNT_SPECS。"""

    def test_no_unsafe_account_specs_subscript(self):
        offenders = []
        for name, path in _iter_backend_sources():
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, start=1):
                    if "ACCOUNT_SPECS[" not in line:
                        continue
                    if _is_docstring_line(line):
                        continue
                    if any(pattern.search(line) for pattern in _ALLOWED_PATTERNS):
                        continue
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
        self.assertEqual([], offenders, "用户策略可达路径不允许直接下标 ACCOUNT_SPECS；请改用 _spec_for()")

    def test_spec_for_never_raises_for_unknown_user_strategy(self):
        """_spec_for 是唯一解析口：未知用户策略也必须给出可用的保守 spec。"""
        spec = PT._spec_for("definitely-not-a-known-account")
        self.assertIn("max_factor_lag", spec)
        self.assertIn("hard_stop", spec)
        self.assertIsInstance(spec["max_factor_lag"], int)


class StaleFactorUserStrategyTests(G.OfflinePaperEnv, unittest.TestCase):
    """用户 DSL 策略 + factor lag 超限 → blocked + 可读 reason，不抛异常。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        G.QUOTE_PRICES.clear()
        G.QUOTE_SCENARIOS.clear()

    def setUp(self):
        self._db_index = getattr(self.__class__, "_db_seq", 0)
        self.__class__._db_seq = self._db_index + 1
        PT.DB_PATH = os.path.join(self._tmp, f"paper_trading_{self._db_index}.sqlite3")
        SRT.clear_cache()
        PT.init_db()
        self._original_usable = PT._strategy_reference_is_usable
        self.addCleanup(setattr, PT, "_strategy_reference_is_usable", self._original_usable)

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _activate(self, strategy_id):
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, strategy_id, "stale factor 策略", dsl_ast=G.RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="stale-factor-test",
            )
            SR.transition(conn, strategy_id, "validated", expected_status="draft",
                          reason="validate", actor="stale-factor-test")
            SR.transition(conn, strategy_id, "active", expected_status="validated",
                          reason="activate", actor="stale-factor-test")

    def _make_history_stale(self, lag=9):
        """让历史 K 线时判定为滞后（模拟因子/日线停留在很久以前）。"""
        PT._strategy_reference_is_usable = lambda *args, **kwargs: (False, lag)

    def test_stale_factor_blocks_user_strategy_with_readable_reason(self):
        self._activate(STRATEGY_ID)
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="stale-factor-test")
        PT.init_db()
        _, cycle = PT.start_new_cycle(capital=G.CAPITAL, include_dashboard=False)
        self.assertEqual(tuple(cycle["enabled_strategies"]), (STRATEGY_ID,))

        self._make_history_stale(lag=9)
        # 不得抛 KeyError：整轮收盘扫描必须正常返回。
        close_result = PT.generate_signals(G.D0)
        self.assertNotEqual(close_result.get("status"), "failed", close_result)

        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status,reason FROM paper_signals WHERE account_id=?", (STRATEGY_ID,)
            ).fetchall()
            orders = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE account_id=?", (STRATEGY_ID,)
            ).fetchone()[0]
            expected_lag = str(PT._spec_for(STRATEGY_ID, conn=conn).get("max_factor_lag"))
        self.assertTrue(rows, "必须留下 blocked 记录（而不是静默丢弃或抛异常）")
        # 没有任何可执行信号。
        self.assertEqual([], [row["status"] for row in rows if row["status"] != "blocked"])
        # reason 可读且带策略自身的上限（用户策略 max_factor_lag 来自 spec）。
        reasons = [str(row["reason"] or "") for row in rows]
        self.assertTrue(any("历史数据滞后" in reason for reason in reasons), reasons)
        self.assertTrue(any("超过本策略上限" in reason for reason in reasons), reasons)
        self.assertTrue(
            any(("超过本策略上限 " + expected_lag) in reason for reason in reasons),
            f"reason 必须给出本策略的 max_factor_lag={expected_lag}：{reasons}",
        )
        # 不得产生任何委托。
        self.assertEqual(0, orders, "stale factor 不得产生委托")

    def test_stale_factor_open_slot_creates_no_order(self):
        self._activate(STRATEGY_ID)
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="stale-factor-test")
        PT.init_db()
        PT.start_new_cycle(capital=G.CAPITAL, include_dashboard=False)
        self._make_history_stale(lag=9)
        PT.generate_signals(G.D0)
        opened = PT.run_slot("open", G.D1, force=True)
        self.assertNotEqual(opened.get("status"), "failed", opened)
        with self._conn() as conn:
            orders = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE account_id=?", (STRATEGY_ID,)
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM paper_signals WHERE account_id=? AND status='pending'",
                (STRATEGY_ID,),
            ).fetchone()[0]
        self.assertEqual(0, orders)
        self.assertEqual(0, pending)

    def test_fresh_history_still_approves_user_strategy(self):
        """对照：滞后判定恢复后，同一策略仍能正常出信号（门禁没有误伤）。"""
        self._activate(STRATEGY_ID)
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="stale-factor-test")
        PT.init_db()
        PT.start_new_cycle(capital=G.CAPITAL, include_dashboard=False)
        close_result = PT.generate_signals(G.D0)
        rows = [row for row in close_result["accounts"] if row["id"] == STRATEGY_ID]
        self.assertTrue(rows and rows[0]["created"] > 0, close_result)


if __name__ == "__main__":
    unittest.main()
