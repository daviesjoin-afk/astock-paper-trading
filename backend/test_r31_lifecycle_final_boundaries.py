# -*- coding: utf-8 -*-
"""Permanent regressions for R31 upgrade, safety, resume, and purge edges."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_lifecycle as SL
import strategy_registry as SR
from user_strategy_participation import user_known_ids, user_participant_ids


DSL = {"op": "gt", "left": {"op": "field", "name": "close"},
       "right": {"op": "indicator", "name": "ma", "window": 20}}


class R31LifecycleFinalBoundaries(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        SR.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cycle_schema(self):
        self.conn.execute("CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY, status TEXT, capital REAL)")
        self.conn.execute("INSERT INTO paper_cycles VALUES(1,'running',100000)")

    def _paper_user(self, strategy_id="r31_resume"):
        spec = SR.create_user_definition(self.conn, strategy_id, "R31 resume", dsl_ast=DSL)
        version = SR.get_version(strategy_id, conn=self.conn)
        # Model a pre-R31 active legacy definition so the owner imports PAPER.
        self.conn.execute("DROP TRIGGER strategy_lifecycle_events_no_delete")
        self.conn.execute("DELETE FROM strategy_lifecycle_events WHERE strategy_id=?", (strategy_id,))
        self.conn.execute("DELETE FROM strategy_lifecycle_state WHERE strategy_id=?", (strategy_id,))
        self.conn.execute("UPDATE strategy_definitions SET lifecycle_status='active' WHERE id=?",
                          (strategy_id,))
        SL.ensure_schema(self.conn)
        return spec, version

    def test_upgrade_imports_each_live_exact_pin_without_copying_new_head_draft(self):
        spec, version1 = self._paper_user("r31_upgrade_pin")
        self._cycle_schema()
        SR.bind_cycle_versions(self.conn, 1, [spec.id])
        version2 = SR.save_definition(self.conn, spec.id, {"description": "new draft head"})
        self.assertEqual("draft", SL.get_state(self.conn, spec.id, version2.version,
                                               checksum=version2.checksum)["state"])

        # Simulate an upgrade database where the head was already bootstrapped
        # by R31 but the older live pin had no owner row yet.
        self.conn.execute("DROP TRIGGER strategy_lifecycle_events_no_delete")
        self.conn.execute("DELETE FROM strategy_lifecycle_events WHERE strategy_id=? AND strategy_version=?",
                          (spec.id, version1.version))
        self.conn.execute("DELETE FROM strategy_lifecycle_state WHERE strategy_id=? AND strategy_version=?",
                          (spec.id, version1.version))

        SL.ensure_schema(self.conn)
        old_state = SL.get_state(self.conn, spec.id, version1.version, checksum=version1.checksum)
        self.assertEqual("paper", old_state["state"])
        self.assertEqual("draft", SL.get_state(self.conn, spec.id, version2.version,
                                                checksum=version2.checksum)["state"])
        events_before = len(SL.history(self.conn, spec.id, version1.version))
        SL.ensure_schema(self.conn)
        self.assertEqual(events_before, len(SL.history(self.conn, spec.id, version1.version)))
        event = SL.history(self.conn, spec.id, version1.version)[0]
        self.assertEqual("live_cycle_pin_import", event["transition_kind"])
        self.assertEqual("paper", event["to_state"])
        self.assertEqual(["1"], json.loads(event["evidence_json"])["live_cycle_ids"])

    def test_safety_transition_accepts_exact_live_old_pin_only(self):
        self._cycle_schema()
        old = SR.get_version("tq_breakout", conn=self.conn)
        SR.bind_cycle_versions(self.conn, 1, ["tq_breakout"])
        new = SR.save_definition(self.conn, "tq_breakout", {"description": "new head"})

        state = SL.transition(self.conn, strategy_id="tq_breakout",
            strategy_version=old.version, strategy_checksum=old.checksum,
            expected_state="paper", target_state="paused", actor_type="human",
            actor_id="r31-test", transition_kind="safety", reason_code="incident",
            reason_text="Pause the version still pinned by the running cycle.")
        self.assertEqual("paused", state["state"])
        self.assertEqual("draft", SL.get_state(self.conn, "tq_breakout", new.version,
                                                checksum=new.checksum)["state"])

        newer = SR.save_definition(self.conn, "tq_breakout", {"description": "another head"})
        with self.assertRaisesRegex(SL.LifecycleError, "strategy_version_changed"):
            SL.transition(self.conn, strategy_id="tq_breakout",
                strategy_version=new.version, strategy_checksum=new.checksum,
                expected_state="draft", target_state="quarantined", actor_type="human",
                actor_id="r31-test", transition_kind="safety", reason_code="incident",
                reason_text="This historical version is not pinned to a live cycle.")
        self.assertEqual("draft", SL.get_state(self.conn, "tq_breakout", newer.version,
                                                checksum=newer.checksum)["state"])

    def test_compat_cycle_schema_without_status_cannot_claim_live_pin(self):
        old = SR.get_version("tq_breakout", conn=self.conn)
        self.conn.execute("CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY)")
        self.conn.execute("INSERT INTO paper_cycles(id) VALUES(9)")
        self.conn.execute("""INSERT INTO paper_cycle_strategy_versions
            (cycle_id,account_id,strategy_id,strategy_version,strategy_checksum,bound_at)
            VALUES(9,'tq_breakout','tq_breakout',?,?, '2026-09-28T00:00:00Z')""",
            (old.version, old.checksum))
        new = SR.save_definition(self.conn, "tq_breakout", {"description": "new head"})

        # Both bootstrap and safety checks must fail closed when the legacy
        # cycle table cannot prove whether a cycle is still live.
        SL.ensure_schema(self.conn)
        self.assertFalse(SL.is_live_cycle_pinned(
            self.conn, "tq_breakout", old.version, old.checksum))
        with self.assertRaisesRegex(SL.LifecycleError, "strategy_version_changed"):
            SL.transition(self.conn, strategy_id="tq_breakout",
                strategy_version=old.version, strategy_checksum=old.checksum,
                expected_state="paper", target_state="paused", actor_type="human",
                actor_id="r31-test", transition_kind="safety", reason_code="incident",
                reason_text="Cannot prove a live binding from the compatibility schema.")
        self.assertEqual("draft", SL.get_state(self.conn, "tq_breakout", new.version,
                                                checksum=new.checksum)["state"])

    def test_pause_remove_resume_restores_participation_without_changing_cycle_or_lots(self):
        spec, version = self._paper_user()
        self._cycle_schema()
        SR.bind_cycle_versions(self.conn, 1, [spec.id])
        self.conn.execute("""CREATE TABLE paper_position_lots(
            id INTEGER PRIMARY KEY, cycle_id INTEGER, account_id TEXT, code TEXT,
            qty INTEGER, remaining_qty INTEGER, cost REAL)""")
        self.conn.execute("INSERT INTO paper_position_lots VALUES(1,1,?, '600000.SH',100,100,10)",
                          (spec.id,))

        before_cycle = tuple(self.conn.execute("SELECT id,status,capital FROM paper_cycles").fetchone())
        before_pin = tuple(self.conn.execute("""SELECT cycle_id,account_id,strategy_id,
            strategy_version,strategy_checksum,bound_at FROM paper_cycle_strategy_versions""").fetchone())
        before_lot = tuple(self.conn.execute("SELECT * FROM paper_position_lots").fetchone())
        self.assertIn(spec.id, user_participant_ids(self.conn))
        self.assertIn(spec.id, user_known_ids(self.conn))

        SL.transition(self.conn, strategy_id=spec.id, strategy_version=version.version,
            strategy_checksum=version.checksum, expected_state="paper", target_state="paused",
            actor_type="human", actor_id="r31-test", transition_kind="safety",
            reason_code="operator_pause", reason_text="Temporarily stop new execution.")
        self.assertNotIn(spec.id, user_participant_ids(self.conn))
        self.assertIn(spec.id, user_known_ids(self.conn))

        with self.assertRaisesRegex(SL.LifecycleError, "explicit_resume_required"):
            SL.transition(self.conn, strategy_id=spec.id, strategy_version=version.version,
                strategy_checksum=version.checksum, expected_state="paused", target_state="paper",
                actor_type="system", actor_id="automatic-promotion", transition_kind="promotion")
        with self.assertRaisesRegex(SL.LifecycleError, "ai_cannot_apply_transition"):
            SL.transition(self.conn, strategy_id=spec.id, strategy_version=version.version,
                strategy_checksum=version.checksum, expected_state="paused", target_state="paper",
                actor_type="ai", actor_id="assistant", transition_kind="resume",
                reason_code="resume", reason_text="AI cannot restore execution.")
        resumed = SL.transition(self.conn, strategy_id=spec.id, strategy_version=version.version,
            strategy_checksum=version.checksum, expected_state="paused", target_state="paper",
            actor_type="human", actor_id="r31-test", transition_kind="resume",
            reason_code="operator_resume", reason_text="Operator reviewed and restored execution.")
        self.assertEqual("paper", resumed["state"])
        self.assertIn(spec.id, user_participant_ids(self.conn))
        self.assertEqual(before_cycle, tuple(self.conn.execute(
            "SELECT id,status,capital FROM paper_cycles").fetchone()))
        self.assertEqual(before_pin, tuple(self.conn.execute("""SELECT cycle_id,account_id,strategy_id,
            strategy_version,strategy_checksum,bound_at FROM paper_cycle_strategy_versions""").fetchone()))
        self.assertEqual(before_lot, tuple(self.conn.execute("SELECT * FROM paper_position_lots").fetchone()))
        resume_event = SL.history(self.conn, spec.id, version.version)[-1]
        self.assertEqual("resume", resume_event["transition_kind"])
        self.assertEqual("operator_resume", resume_event["reason_code"])

        prod_spec, prod_version = self._paper_user("r31_prod_resume")
        promotion = {"eligible": True, "strategy_id": prod_spec.id,
            "strategy_version": prod_version.version, "strategy_checksum": prod_version.checksum,
            "from_state": "paper", "target_state": "production_sim",
            "decision_fingerprint": "a" * 64, "policy_version": "r31-test-policy"}
        SL.transition(self.conn, strategy_id=prod_spec.id,
            strategy_version=prod_version.version, strategy_checksum=prod_version.checksum,
            expected_state="paper", target_state="production_sim", actor_type="human",
            actor_id="r31-test", transition_kind="promotion", promotion_decision=promotion)
        SL.transition(self.conn, strategy_id=prod_spec.id,
            strategy_version=prod_version.version, strategy_checksum=prod_version.checksum,
            expected_state="production_sim", target_state="paused", actor_type="human",
            actor_id="r31-test", transition_kind="safety", reason_code="operator_pause",
            reason_text="Pause production simulation.")
        restored = SL.transition(self.conn, strategy_id=prod_spec.id,
            strategy_version=prod_version.version, strategy_checksum=prod_version.checksum,
            expected_state="paused", target_state="production_sim", actor_type="human",
            actor_id="r31-test", transition_kind="resume", reason_code="operator_resume",
            reason_text="Restore the previous simulation mode.")
        self.assertEqual("production_sim", restored["state"])


if __name__ == "__main__":
    unittest.main()
