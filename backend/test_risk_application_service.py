# -*- coding: utf-8 -*-
"""R21 risk application service regression (RSVC-1 ~ RSVC-14)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import execution_planner as EP  # noqa: E402
import paper_account_specs as ACS  # noqa: E402
import paper_position_risk_state as PPRS  # noqa: E402
import paper_risk_evidence as PREv  # noqa: E402
import paper_risk_scan_state as PRSS  # noqa: E402
import paper_risk_service as PRSVC  # noqa: E402
import paper_trading as PT  # noqa: E402
import strategy_registry as SR  # noqa: E402
import strategy_risk_enforcement as SRE  # noqa: E402
import strategy_runtime as SRT  # noqa: E402
import test_position_risk_state as PRS  # noqa: E402
import user_strategy_participation as USP  # noqa: E402


ACCOUNT = "tq_breakout"


class _RiskServiceCase(PRS._ProductionRiskScanCase):
    ACCOUNT = ACCOUNT

    def setUp(self):
        super().setUp()
        self.code_b = self._inner.code_b

    def clear_scan_state(self):
        with PT._db(immediate=True) as conn:
            conn.execute("DELETE FROM paper_risk_scan_runs")
            conn.execute("DELETE FROM paper_audit WHERE event LIKE 'risk_scan%'")

    def run_risk(self):
        return PT.monitor_risk(self.day)

    def set_quote(self, code=None, *, price=10.2, pct=1.0, high=10.3, low=10.1):
        self._inner._set_fresh_exit_quote(
            code or self.code, price=price, pct=pct, high=high, low=low,
        )

    def set_non_sell_quote(self, code=None):
        self.set_quote(code, price=10.2, pct=1.0, high=10.3, low=10.1)

    def set_sell_quote(self, code=None):
        self.set_quote(code, price=9.0, pct=-8.0, high=9.2, low=8.9)

    def add_lot_for(self, code, qty, cost):
        self._inner._insert_lot(self.ACCOUNT, code, qty, cost)
        PPRS.initialize_episode(
            self.conn, cycle_id=self.cycle, account_id=self.ACCOUNT,
            code=code, peak_price=cost,
        )
        self.conn.commit()

    def set_running(self):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "UPDATE paper_accounts SET status='running', cycle_id=? WHERE id=?",
                (self.cycle, self.ACCOUNT),
            )

    def set_future_adaptive_risk(self, *, warning_pct=-4.0, max_exposure=0.30):
        self.set_running()
        effective = (self.day + dt.timedelta(days=1)).isoformat()
        with PT._db(immediate=True) as conn:
            row = conn.execute(
                "SELECT params FROM paper_accounts WHERE id=?", (self.ACCOUNT,)
            ).fetchone()
            params = PT._loads(row["params"] if row is not None else None, {}) or {}
            params["adaptive_risk"] = {
                "max_exposure": max_exposure,
                "downside_warning_pct": warning_pct,
            }
            params["adaptive_risk_meta"] = {
                "status": "active", "effective_date": effective,
                "version": "rsvc", "candidate_id": "rsvc",
            }
            conn.execute(
                "UPDATE paper_accounts SET params=? WHERE id=?",
                (PT._json(params), self.ACCOUNT),
            )

    def advance_head_after_pin(self):
        import strategy_registry as SR
        with PT._db(immediate=True) as conn:
            SR.bind_cycle_versions(conn, self.cycle, (self.ACCOUNT,))
            current = SR.get_version(self.ACCOUNT, conn=conn)
            SR.save_definition(
                conn, self.ACCOUNT,
                {"metadata": {"style": "trend", "hold": 2, "daily": True, "positions": 2}},
                expected_version=current.version, actor="rsvc",
                change_note="rsvc head advance", risk_evidence=20,
            )


    USER = "r21_alpha"
    USER_PINNED_RULE = {
        "op": "gt", "left": {"op": "field", "name": "close"},
        "right": {"op": "indicator", "name": "ma", "window": 20},
    }
    USER_PINNED_CONFIG = {
        "style": "trend", "hold": 8, "positions": 3, "daily": True, "close": True,
    }
    USER_HEAD_RULE = {
        "op": "gt", "left": {"op": "field", "name": "close"},
        "right": {"op": "const", "value": 1},
    }
    USER_HEAD_CONFIG = {
        "style": "quality", "daily": True, "close": True, "hold": 20,
        "positions": 8, "stop": True, "atr": True,
    }

    def seed_user_strategy_v1(self):
        with PT._db(immediate=True) as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, self.USER, "R21 alpha", dsl_ast=dict(self.USER_PINNED_RULE),
                metadata=dict(self.USER_PINNED_CONFIG), actor="rsvc",
            )
            conn.execute(
                "INSERT OR IGNORE INTO paper_accounts(id,name,source_strategy,status,"
                "initial_cash,cash,cycle_days,max_positions,max_weight,max_exposure,"
                "version,created_at,updated_at,cycle_id,risk_profile) "
                "VALUES(?,?,'strategy_dsl','running',0,0,8,3,0.32,0.9,'v0',?,?,?, 'trend')",
                (self.USER, self.USER, f"{self.day.isoformat()} 00:00:00",
                 f"{self.day.isoformat()} 00:00:00", int(self.cycle)),
            )
            SR.bind_cycle_versions(conn, self.cycle, [self.USER])
        with PT._db() as conn:
            return SR.cycle_version_for_account(conn, self.USER, cycle_id=self.cycle)

    def advance_user_head(self, pinned_version):
        with PT._db(immediate=True) as conn:
            SR.save_definition(
                conn, self.USER,
                {"dsl_ast": dict(self.USER_HEAD_RULE),
                 "metadata": dict(self.USER_HEAD_CONFIG)},
                expected_version=pinned_version.version, actor="rsvc",
                change_note="rsvc user head advance",
            )

    def add_lot_for_account(self, account_id, code, qty, cost):
        self._inner._insert_lot(account_id, code, qty, cost)
        PPRS.initialize_episode(
            self.conn, cycle_id=self.cycle, account_id=account_id,
            code=code, peak_price=cost,
        )
        self.conn.commit()

    def delete_user_cycle_binding(self):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "DELETE FROM paper_cycle_strategy_versions WHERE cycle_id=? AND account_id=?",
                (self.cycle, self.USER),
            )
            try:
                conn.execute(
                    "DELETE FROM paper_strategy_legacy_bindings WHERE account_id=?",
                    (self.USER,),
                )
            except Exception:
                pass

    def delete_user_legacy_binding(self):
        with PT._db(immediate=True) as conn:
            conn.execute(
                "DELETE FROM paper_strategy_legacy_bindings WHERE account_id=?",
                (self.USER,),
            )

    def review_detail(self, code=None):
        row = self.conn.execute(
            "SELECT detail FROM paper_position_reviews"
            " WHERE cycle_id=? AND account_id=? AND code=?"
            " ORDER BY id DESC LIMIT 1",
            (self.cycle, self.ACCOUNT, code or self.code),
        ).fetchone()
        return json.loads(row["detail"]) if row is not None else {}


class RiskServiceContractTests(_RiskServiceCase):
    def _fresh_case(self):
        case = type(self)("test_rsvc1_risk_run_context_requires_explicit_identity")
        case.setUp()
        return case

    def test_rsvc1_risk_run_context_requires_explicit_identity(self):
        with self.assertRaises(ValueError):
            PRSVC.RiskRunContext(cycle_id=None, asof_day=self.day)
        with self.assertRaises(ValueError):
            PRSVC.RiskRunContext(cycle_id=self.cycle, asof_day=None)
        context = PRSVC.RiskRunContext(cycle_id=self.cycle, asof_day=self.day.isoformat())
        self.assertEqual(context.cycle_id, self.cycle)
        self.assertEqual(context.asof_day, self.day)

    def test_rsvc2_historical_capacity_uses_bounded_budget(self):
        self.add_lot(100, 10.0)
        self.set_future_adaptive_risk()
        self.advance_head_after_pin()
        self.set_non_sell_quote()
        calls = []
        original = PT._dynamic_position_limits

        def spy(conn, *args, **kwargs):
            calls.append(kwargs)
            return original(conn, *args, **kwargs)

        with mock.patch.object(PT, "_dynamic_position_limits", side_effect=spy):
            self.run_risk()
        self.assertTrue(calls)
        for kwargs in calls:
            self.assertEqual(kwargs.get("cycle_id"), self.cycle)
            self.assertEqual(kwargs.get("asof_day"), self.day)
        with PT._db() as conn:
            bounded = original(conn, cycle_id=self.cycle, asof_day=self.day)
        detail = self.review_detail()
        self.assertEqual(
            detail.get("dynamic_position_limit"),
            bounded["limits"].get(self.ACCOUNT),
        )

    def test_rsvc3_historical_downside_policy_is_cycle_asof_bound(self):
        self.add_lot(100, 10.0)
        self.set_future_adaptive_risk()
        self.set_non_sell_quote()
        captured = {}
        original = PREv.intraday_downside_guard
        profile_calls = []
        original_profile = PT._risk_profile

        def guard_spy(position, quote, **kwargs):
            captured.update(kwargs)
            return original(position, quote, **kwargs)

        def profile_spy(account, *args, **kwargs):
            profile_calls.append(kwargs)
            return original_profile(account, *args, **kwargs)

        with mock.patch.object(PREv, "intraday_downside_guard", side_effect=guard_spy), \
             mock.patch.object(PT, "_risk_profile", side_effect=profile_spy):
            self.run_risk()
        self.assertTrue(profile_calls)
        for kwargs in profile_calls:
            self.assertEqual(kwargs.get("asof_day"), self.day)
            self.assertEqual(kwargs.get("cycle_id"), self.cycle)
            self.assertIsNotNone(kwargs.get("conn"))
        override = captured.get("policy_override") or {}
        self.assertEqual(override.get("downside_warning_pct"), -2.0)

    def test_rsvc4_sell_spec_uses_cycle_pin_not_current_head(self):
        self.add_lot(100, 10.0)
        self.advance_head_after_pin()
        self.set_non_sell_quote()
        calls = []
        original = SRE.effective_spec_for_cycle

        def spy(conn, account_id, base_spec, **kwargs):
            calls.append(kwargs)
            return original(conn, account_id, base_spec, **kwargs)

        with mock.patch.object(SRE, "effective_spec_for_cycle", side_effect=spy), \
             mock.patch.object(SRE, "effective_spec") as unbounded:
            self.run_risk()
        self.assertTrue(calls)
        self.assertEqual(calls[0].get("cycle_id"), self.cycle)
        unbounded.assert_not_called()

    def test_rsvc5_external_io_cycle_rollover_fails_closed(self):
        self.add_lot(100, 10.0)
        self.set_sell_quote()
        before_orders = self.conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE side='sell'"
        ).fetchone()[0]
        before_reviews = self.conn.execute(
            "SELECT COUNT(*) FROM paper_position_reviews"
        ).fetchone()[0]

        def rollover(codes, asof_date=None):
            with PT._db(immediate=True) as conn:
                conn.execute(
                    "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,"
                    "created_at,updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
                    ("rsvc-rollover", "running", 100000.0, "balanced",
                     "2026-09-20 09:30:00", "2026-09-20 09:30:00", "2026-09-20 09:30:00"),
                )
                return {code: self._inner.quotes_map[code] for code in codes
                        if code in self._inner.quotes_map}

        with mock.patch.object(PT, "_quotes", side_effect=rollover):
            with self.assertRaises(PRSS.RiskScanCycleChanged):
                self.run_risk()
        after_orders = self.conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE side='sell'"
        ).fetchone()[0]
        after_reviews = self.conn.execute(
            "SELECT COUNT(*) FROM paper_position_reviews"
        ).fetchone()[0]
        self.assertEqual(after_orders, before_orders)
        self.assertEqual(after_reviews, before_reviews)

    def test_rsvc6_empty_branch_still_fences_syncs_and_processes_manual(self):
        with mock.patch.object(PRSS, "assert_cycle_active", wraps=PRSS.assert_cycle_active) as fence, \
             mock.patch.object(PT, "_record_nav", wraps=PT._record_nav) as nav, \
             mock.patch.object(
                 PT, "process_pending_manual_orders",
                 return_value=[{"status": "ok"}],
             ) as pending:
            result = self.run_risk()
        self.assertEqual(result["slot"], "risk")
        self.assertEqual(result["orders"], [])
        self.assertTrue(fence.called)
        self.assertTrue(nav.called)
        pending.assert_called_once()

    def test_rsvc7_risk_sell_commits_through_execution_planner_once(self):
        self.add_lot(100, 10.0)
        self.set_sell_quote()
        with mock.patch.object(EP, "commit_fill", wraps=EP.commit_fill) as commit:
            result = self.run_risk()
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(
            [item for item in result["orders"] if item["status"] == "filled"].__len__(),
            1,
        )

    def test_rsvc8_partial_risk_sell_advances_take_stage(self):
        self.add_lot(200, 10.0)
        self.set_quote(price=10.9, pct=9.0, high=11.0, low=10.8)
        result = self.run_risk()
        filled = [item for item in result["orders"] if item["status"] == "filled"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(int(filled[0]["qty"]), 100)
        self.assertEqual(int(self.state_row()["take_stage"]), 1)

    def test_rsvc9_full_risk_sell_closes_episode_from_authoritative_lots(self):
        self.add_lot(100, 10.0)
        self.set_sell_quote()
        self.run_risk()
        self.assertEqual(self.remaining_lots(), 0)
        self.assertIsNone(self.state_row())

    def test_rsvc10_quality_capacity_permission_order_is_stable(self):
        self.add_lot_for(self.code_b, 100, 10.0)
        self.add_lot(100, 10.0)
        self.set_sell_quote(self.code)
        self.set_sell_quote(self.code_b)
        reviews = {
            self.code: {
                "account_id": self.ACCOUNT, "code": self.code, "score": 90.0,
                "grade": "A", "market_value": 1000.0, "position_pct": 10.0,
                "reasons": [], "replacement": None, "review_date": self.day,
            },
            self.code_b: {
                "account_id": self.ACCOUNT, "code": self.code_b, "score": 10.0,
                "grade": "D", "market_value": 1000.0, "position_pct": 10.0,
                "reasons": [], "replacement": None, "review_date": self.day,
            },
        }

        def score(conn, position, quote, asof_day, **kwargs):
            return dict(reviews[position["code"]])

        with mock.patch.object(PREv, "position_quality_score", side_effect=score), \
             mock.patch.object(
                 PREv, "over_capacity_exit_candidates",
                 return_value={(self.ACCOUNT, self.code_b): "capacity weak"},
             ), \
             mock.patch.object(PREv, "permission_scope_exit_candidates", return_value={}), \
             mock.patch.object(PREv, "best_replacement_candidate", return_value={}):
            self.run_risk()
        codes = [
            row["code"] for row in self.conn.execute(
                "SELECT code FROM paper_orders WHERE side='sell'"
                " AND status='filled' ORDER BY id"
            ).fetchall()
        ]
        self.assertEqual(codes[:2], [self.code_b, self.code], codes)

    def test_rsvc11_replacement_candidate_is_intended_asof_and_signal_bounded(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "CREATE TABLE paper_signals(id INTEGER PRIMARY KEY,account_id TEXT,"
                "signal_date TEXT,intended_date TEXT,code TEXT,name TEXT,rank_score REAL,"
                "t_score REAL,payload TEXT,status TEXT,created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_signals VALUES(1,?,?,?,?,?,?,?,?,?,?)",
                (self.ACCOUNT, self.day.isoformat(), self.day.isoformat(),
                 "600001", "valid", 0.5, 0.5, "{}", "pending", "2026-09-10 09:00:00"),
            )
            conn.execute(
                "INSERT INTO paper_signals VALUES(2,?,?,?,?,?,?,?,?,?,?)",
                (self.ACCOUNT, (self.day + dt.timedelta(days=1)).isoformat(),
                 self.day.isoformat(), "600002", "future-signal", 0.99, 0.99,
                 "{}", "pending", "2026-09-11 09:00:00"),
            )
            conn.execute(
                "INSERT INTO paper_signals VALUES(3,?,?,?,?,?,?,?,?,?,?)",
                (self.ACCOUNT, self.day.isoformat(),
                 (self.day + dt.timedelta(days=1)).isoformat(),
                 "600003", "future-intent", 0.99, 0.99,
                 "{}", "pending", "2026-09-10 09:00:00"),
            )
            best = PT._best_replacement_candidate(conn, self.ACCOUNT, self.day, set())
            self.assertIsNotNone(best)
            self.assertEqual(best["code"], "600001")
            self.assertEqual(best["intended_date"], self.day.isoformat())
        finally:
            conn.close()

    def test_rsvc12_rotation_buy_delegates_to_existing_buy_adapter(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("CREATE TABLE paper_signals(id INTEGER PRIMARY KEY,status TEXT,name TEXT)")
            conn.execute("INSERT INTO paper_signals VALUES(1,'pending','候选')")
            calls = []

            def rotate(*args, **kwargs):
                calls.append((args, kwargs))
                return {"filled": True, "status": "filled"}

            ports = types.SimpleNamespace(rotation_buy=rotate)
            deps = types.SimpleNamespace(entry_frozen_waitlist_status="entry_frozen_waitlist")
            result = PRSVC._rotation_buy_candidate(
                conn, {"id": self.ACCOUNT}, {"signal_id": 1, "code": "600001"},
                {"price": 10.0}, {}, [], self.day,
                all_quotes={}, ports=ports, deps=deps,
            )
            self.assertTrue(calls)
            self.assertTrue(result["rotation"])
        finally:
            conn.close()

    def test_rsvc13_risk_log_and_audit_counts_stay_exactly_once(self):
        self.add_lot(100, 10.0)
        self.set_sell_quote()
        self.run_risk()
        risk_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_risk_decisions"
            " WHERE account_id=? AND code=? AND side='sell' AND decision='filled'",
            (self.ACCOUNT, self.code),
        ).fetchone()[0]
        audit_count = self.conn.execute(
            "SELECT COUNT(*) FROM paper_audit WHERE account_id=? AND event='sell_filled'",
            (self.ACCOUNT,),
        ).fetchone()[0]
        self.assertEqual(risk_count, 1)
        self.assertEqual(audit_count, 1)

    def test_rsvc14_pending_manual_runs_on_empty_and_nonempty_paths(self):
        with mock.patch.object(
            PT, "process_pending_manual_orders", return_value=[{"status": "empty"}],
        ) as pending:
            empty = self.run_risk()
        self.assertEqual(empty["manual_orders"], [{"status": "empty"}])
        pending.assert_called_once()

        self.add_lot(100, 10.0)
        self.set_non_sell_quote()
        self.clear_scan_state()
        with mock.patch.object(
            PT, "process_pending_manual_orders", return_value=[{"status": "nonempty"}],
        ) as pending:
            nonempty = PT.monitor_risk(self.day)
        self.assertEqual(nonempty["manual_orders"], [{"status": "nonempty"}])
        pending.assert_called_once()

    def test_rsvc15_user_sell_base_policy_uses_cycle_pinned_version(self):
        pinned = self.seed_user_strategy_v1()
        with PT._db() as conn:
            pinned_spec = PRSVC._spec_for(self.USER, conn, cycle_id=self.cycle)
        self.advance_user_head(pinned)
        with PT._db() as conn:
            head_context = SRT.get_context(conn, self.USER)
            head_spec = USP.user_spec_for(head_context, risk_profiles=ACS.RISK_PROFILES)
            service_spec = PRSVC._spec_for(self.USER, conn, cycle_id=self.cycle)
        self.assertNotEqual(pinned_spec, head_spec)
        self.assertNotEqual(pinned_spec["hold_max"], head_spec["hold_max"])
        self.assertEqual(service_spec, pinned_spec)
        self.assertEqual(service_spec["strategy_version"], f"v{pinned.version}")

        position = {
            "account_id": self.USER, "code": self.code, "qty": 100,
            "available_qty": 100, "cost": 10.0, "peak_price": 10.0,
            "entry_date": (self.day - dt.timedelta(days=6)).isoformat(),
            "take_stage": 0,
        }
        quote = {
            "price": 10.0, "pct": 0.0, "high": 10.0, "low": 10.0,
            "quote_at": f"{self.day.isoformat()} 10:00:00",
        }
        with mock.patch.object(PT, "_completed_kline", return_value=None):
            deps = PT._risk_service_ports().evidence
        pinned_ratio, _, _, _ = PREv.sell_plan(
            position, quote, self.day, [], base_spec=pinned_spec, deps=deps,
        )
        head_ratio, _, _, _ = PREv.sell_plan(
            position, quote, self.day, [], base_spec=head_spec, deps=deps,
        )
        self.assertEqual(pinned_ratio, 0.0)
        self.assertGreater(head_ratio, 0.0)

    def test_rsvc16_risk_facts_use_cycle_pinned_strategy_provenance(self):
        pinned = self.seed_user_strategy_v1()
        self.advance_user_head(pinned)
        self.add_lot_for_account(self.USER, self.code, 100, 10.0)
        self.set_quote(self.code, price=9.0, pct=-10.0, high=9.2, low=8.9)
        self.run_risk()
        order = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER, self.code),
        ).fetchone()
        decision = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER, self.code),
        ).fetchone()
        self.assertIsNotNone(order)
        self.assertIsNotNone(decision)
        self.assertEqual(order["status"], "unfilled_limit_down")
        for row in (order, decision):
            self.assertEqual(row["strategy_id"], pinned.strategy_id)
            self.assertEqual(int(row["strategy_version"]), pinned.version)
            self.assertEqual(row["strategy_checksum"], pinned.checksum)

        # Missing pin: explicit cycle exists, but no cycle/legacy binding.
        # The audit metadata stays unknown; it must not adopt current head.
        self.delete_user_cycle_binding()
        self.add_lot_for_account(self.USER, self.code_b, 100, 10.0)
        self.set_quote(self.code_b, price=9.0, pct=-10.0, high=9.2, low=8.9)
        self.clear_scan_state()
        self.run_risk()
        missing_order = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER, self.code_b),
        ).fetchone()
        missing_decision = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER, self.code_b),
        ).fetchone()
        self.assertIsNotNone(missing_order)
        self.assertIsNotNone(missing_decision)
        self.assertEqual(missing_order["status"], "unfilled_limit_down")
        for row in (missing_order, missing_decision):
            self.assertIsNone(row["strategy_id"])
            self.assertIsNone(row["strategy_version"])
            self.assertIsNone(row["strategy_checksum"])


    def test_rsvc17_filled_sell_inherits_durable_order_provenance(self):
        pinned = self.seed_user_strategy_v1()
        self.advance_user_head(pinned)
        self.add_lot_for_account(self.USER, self.code, 100, 10.0)
        self.set_quote(self.code, price=9.0, pct=-8.0, high=9.2, low=8.9)
        self.run_risk()
        order = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER, self.code),
        ).fetchone()
        decision = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND code=? AND side='sell'"
            " AND decision='filled' ORDER BY id DESC LIMIT 1",
            (self.USER, self.code),
        ).fetchone()
        audit = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='sell_filled'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER,),
        ).fetchone()
        self.assertIsNotNone(order)
        self.assertIsNotNone(decision)
        self.assertIsNotNone(audit)
        self.assertEqual(order["status"], "filled")
        for row in (order, decision, audit):
            self.assertEqual(row["strategy_id"], pinned.strategy_id)
            self.assertEqual(int(row["strategy_version"]), pinned.version)
            self.assertEqual(row["strategy_checksum"], pinned.checksum)
        recovery = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='protective_exit_recovery_watch'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER,),
        ).fetchone()
        if recovery is not None:
            self.assertEqual(recovery["strategy_id"], pinned.strategy_id)
            self.assertEqual(int(recovery["strategy_version"]), pinned.version)
            self.assertEqual(recovery["strategy_checksum"], pinned.checksum)

        # Missing cycle pin: the protective SELL still executes, but all
        # downstream provenance remains explicitly unknown.
        self.delete_user_cycle_binding()
        self.add_lot_for_account(self.USER, self.code_b, 100, 10.0)
        self.set_quote(self.code_b, price=9.0, pct=-8.0, high=9.2, low=8.9)
        self.clear_scan_state()
        self.run_risk()
        missing_order = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum,status"
            " FROM paper_orders WHERE account_id=? AND code=? AND side='sell'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER, self.code_b),
        ).fetchone()
        missing_decision = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_risk_decisions WHERE account_id=? AND code=? AND side='sell'"
            " AND decision='filled' ORDER BY id DESC LIMIT 1",
            (self.USER, self.code_b),
        ).fetchone()
        missing_audit = self.conn.execute(
            "SELECT strategy_id,strategy_version,strategy_checksum"
            " FROM paper_audit WHERE account_id=? AND event='sell_filled'"
            " ORDER BY id DESC LIMIT 1",
            (self.USER,),
        ).fetchone()
        self.assertIsNotNone(missing_order)
        self.assertIsNotNone(missing_decision)
        self.assertIsNotNone(missing_audit)
        self.assertEqual(missing_order["status"], "filled")
        for row in (missing_order, missing_decision, missing_audit):
            self.assertIsNone(row["strategy_id"])
            self.assertIsNone(row["strategy_version"])
            self.assertIsNone(row["strategy_checksum"])



    def test_rsvc18_post_fill_rotation_audits_inherit_sell_provenance(self):
        def stamp(row):
            if row is None:
                return (None, None, None)
            return (
                row["strategy_id"], row["strategy_version"], row["strategy_checksum"],
            )

        def pin_v1_then_v2(case):
            pinned = case.seed_user_strategy_v1()
            case.advance_user_head(pinned)
            head = SR.get_version(case.USER, conn=case.conn)
            self.assertNotEqual(pinned.version, head.version)
            self.assertNotEqual(pinned.checksum, head.checksum)
            # Keep the cycle pin but remove the legacy resolver fallback, so
            # current head is the only value an unpassed audit can adopt.
            case.delete_user_legacy_binding()
            return pinned, head

        def sell_order(case):
            return case.conn.execute(
                "SELECT strategy_id,strategy_version,strategy_checksum,status"
                " FROM paper_orders WHERE account_id=? AND side='sell'"
                " ORDER BY id DESC LIMIT 1",
                (case.USER,),
            ).fetchone()

        def filled_decision(case):
            return case.conn.execute(
                "SELECT strategy_id,strategy_version,strategy_checksum"
                " FROM paper_risk_decisions WHERE account_id=? AND side='sell'"
                " AND decision='filled' ORDER BY id DESC LIMIT 1",
                (case.USER,),
            ).fetchone()

        def audit(case, event):
            return case.conn.execute(
                "SELECT strategy_id,strategy_version,strategy_checksum"
                " FROM paper_audit WHERE account_id=? AND event=?"
                " ORDER BY id DESC LIMIT 1",
                (case.USER, event),
            ).fetchone()

        def add_capacity_positions(case):
            for code in ("600001", "600002", "600003", "600004", "600005", "600006"):
                case.add_lot_for_account(case.USER, code, 100, 10.0)
                case.set_quote(code, price=10.0, pct=0.5, high=10.2, low=9.8)

        def assert_filled_concentration(case, result):
            self.assertTrue(
                any(
                    item.get("status") == "filled" and item.get("concentration_rotation")
                    for item in result.get("orders", [])
                ),
                f"no filled concentration SELL: {result.get('orders')}",
            )

        with self.subTest("capacity exit pinned v1 / current v2"):
            case = self._fresh_case()
            try:
                pinned, _head = pin_v1_then_v2(case)
                add_capacity_positions(case)
                result = case.run_risk()
                assert_filled_concentration(case, result)
                expected = (pinned.strategy_id, pinned.version, pinned.checksum)
                for row in (
                    sell_order(case),
                    filled_decision(case),
                    audit(case, "sell_filled"),
                    audit(case, "concentration_rotation"),
                ):
                    self.assertEqual(stamp(row), expected)
            finally:
                case.doCleanups()

        with self.subTest("capacity exit missing cycle pin"):
            case = self._fresh_case()
            try:
                _pinned, _head = pin_v1_then_v2(case)
                case.delete_user_cycle_binding()
                add_capacity_positions(case)
                result = case.run_risk()
                assert_filled_concentration(case, result)
                for row in (
                    sell_order(case),
                    filled_decision(case),
                    audit(case, "sell_filled"),
                    audit(case, "concentration_rotation"),
                ):
                    self.assertEqual(stamp(row), (None, None, None))
            finally:
                case.doCleanups()

        with self.subTest("quality rotation consolidation_exit"):
            case = self._fresh_case()
            try:
                pinned, _head = pin_v1_then_v2(case)
                case.add_lot_for_account(case.USER, case.code, 100, 10.0)
                case.set_quote(case.code, price=10.0, pct=0.5, high=10.2, low=9.8)
                original = PREv.position_quality_score

                def low_quality(*args, **kwargs):
                    review = original(*args, **kwargs)
                    review["score"] = 0.0
                    review["grade"] = "淘汰"
                    review["hold_days"] = 2
                    review["min_hold_days"] = 2
                    return review

                with mock.patch.object(
                    PREv, "position_quality_score", side_effect=low_quality,
                ):
                    result = case.run_risk()
                assert_filled_concentration(case, result)
                expected = (pinned.strategy_id, pinned.version, pinned.checksum)
                for row in (
                    sell_order(case),
                    filled_decision(case),
                    audit(case, "sell_filled"),
                    audit(case, "quality_rotation"),
                    audit(case, "concentration_rotation"),
                ):
                    self.assertEqual(stamp(row), expected)
            finally:
                case.doCleanups()

        with self.subTest("permission scope exit"):
            case = self._fresh_case()
            try:
                pinned, _head = pin_v1_then_v2(case)
                code = "688001"
                case.add_lot_for_account(case.USER, code, 300, 10.0)
                case.set_quote(code, price=10.0, pct=0.5, high=10.2, low=9.8)
                result = case.run_risk()
                assert_filled_concentration(case, result)
                expected = (pinned.strategy_id, pinned.version, pinned.checksum)
                for row in (
                    sell_order(case),
                    filled_decision(case),
                    audit(case, "sell_filled"),
                    audit(case, "permission_scope_exit"),
                    audit(case, "concentration_rotation"),
                ):
                    self.assertEqual(stamp(row), expected)
                self.assertIsNotNone(audit(case, "permission_scope_exit"))
            finally:
                case.doCleanups()



if __name__ == "__main__":
    unittest.main()
