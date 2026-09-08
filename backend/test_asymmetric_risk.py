# -*- coding: utf-8 -*-
"""非对称风险进化（asymmetric risk evolution）回归测试。"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asymmetric_risk as AR
import runtime_settings as RSET

CURRENT = {
    "sector_rotation": {
        "style": "sector", "max_positions": 3,
        "max_weight_pct": 32.0, "max_exposure_pct": 92.0,
    },
}
FULL_EVIDENCE = {"evidence_count": 25, "observation_days": 12}


def _proposed(key, new):
    override = dict(CURRENT["sector_rotation"])
    override[key] = new
    return {"sector_rotation": override}


class ClassifyTests(unittest.TestCase):
    def test_direction_classification(self):
        self.assertEqual("expand", AR.classify_risk_change("max_exposure_pct", 90, 92))
        self.assertEqual("tighten", AR.classify_risk_change("max_exposure_pct", 90, 85))
        self.assertEqual("expand", AR.classify_risk_change("max_positions", 3, 4))
        self.assertEqual("tighten", AR.classify_risk_change("max_positions", 3, 2))
        self.assertEqual("none", AR.classify_risk_change("max_weight_pct", 32, 32))
        self.assertEqual("none", AR.classify_risk_change("max_weight_pct", 32, "x"))


class AsymmetricGateTests(unittest.TestCase):
    def test_tightening_is_allowed_without_evidence(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 85), evidence_count=None)
        self.assertTrue(gate["allowed"])
        self.assertEqual(1, len(gate["tightenings"]))

    def test_expansion_without_evidence_is_rejected(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), evidence_count=None)
        self.assertFalse(gate["allowed"])
        self.assertIn("观察期", gate["violations"][0])

    def test_expansion_with_low_evidence_is_rejected(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=6, observation_days=30)
        self.assertFalse(gate["allowed"])
        self.assertIn("证据不足", gate["violations"][0])
        self.assertGreater(AR.EXPANSION_MIN_SAMPLES, AR.TIGHTEN_MIN_SAMPLES)

    def test_expansion_without_observation_window_is_rejected(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=25, observation_days=3)
        self.assertFalse(gate["allowed"])
        self.assertIn("观察期未满", gate["violations"][0])

    def test_full_expansion_requires_all_three_gates(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), **FULL_EVIDENCE)
        self.assertTrue(gate["allowed"])
        self.assertEqual(1, len(gate["expansions"]))

    def test_single_round_step_cap_blocks_big_jumps_even_with_evidence(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 99), **FULL_EVIDENCE)
        self.assertFalse(gate["allowed"])
        self.assertIn("单轮放大超过硬上限", gate["violations"][0])

    def test_position_cap_step_is_one_seat(self):
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_positions", 6), **FULL_EVIDENCE)
        self.assertFalse(gate["allowed"])
        self.assertIn("单轮放大超过硬上限", gate["violations"][0])
        ok = AR.validate_risk_updates(
            CURRENT, _proposed("max_positions", 4), **FULL_EVIDENCE)
        self.assertTrue(ok["allowed"])

    def test_all_direction_keys_are_capped(self):
        self.assertEqual(set(AR.MAX_SINGLE_ROUND_STEP), set(AR.RISK_DIRECTION_KEYS))


class RuntimeSettingsWiringTests(unittest.TestCase):
    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # ensure_schema 的迁移分支假定账本主表已存在（真实环境由 init_db 建表）。
        conn.execute(
            "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY AUTOINCREMENT, duration_days INTEGER, enabled_strategies TEXT)"
        )
        RSET.ensure_schema(conn)
        return conn

    def test_ui_cannot_expand_risk_without_evidence_context(self):
        conn = self._db()
        payload = {"strategy_overrides": _proposed("max_exposure_pct", 95)}
        with self.assertRaises(ValueError) as ctx:
            RSET.update(conn, payload, actor="human-ui")
        self.assertIn("观察期", str(ctx.exception))

    def test_ui_can_tighten_risk_immediately(self):
        conn = self._db()
        payload = {"strategy_overrides": _proposed("max_exposure_pct", 85)}
        result = RSET.update(conn, payload, actor="human-ui")
        self.assertEqual(
            85.0,
            result["strategy"]["strategy_overrides"]["sector_rotation"]["max_exposure_pct"],
        )

    def test_evolution_path_with_full_evidence_can_expand(self):
        conn = self._db()
        payload = {"strategy_overrides": _proposed("max_exposure_pct", 94)}
        result = RSET.update(
            conn, payload, actor="evolution",
            risk_evidence={"evidence_count": 25, "observation_days": 12},
        )
        self.assertEqual(
            94.0,
            result["strategy"]["strategy_overrides"]["sector_rotation"]["max_exposure_pct"],
        )

    def test_every_expansion_is_audited(self):
        conn = self._db()
        RSET.update(conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
                    actor="evolution",
                    risk_evidence={"evidence_count": 25, "observation_days": 12})
        rows = conn.execute(
            "SELECT key,old_value,new_value,updated_by FROM paper_runtime_settings_audit"
        ).fetchall()
        self.assertTrue(any(row["key"] == "strategy_overrides" for row in rows))


if __name__ == "__main__":
    unittest.main()
