# -*- coding: utf-8 -*-
"""非对称风险进化（asymmetric risk evolution）回归测试。"""
from __future__ import annotations

import datetime as dt
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
FULL_EVIDENCE = 25


def _proposed(key, new):
    override = dict(CURRENT["sector_rotation"])
    override[key] = new
    return {"sector_rotation": override}


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # ensure_schema 的迁移分支假定账本主表已存在（真实环境由 init_db 建表）。
    conn.execute(
        "CREATE TABLE paper_cycles(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " duration_days INTEGER, enabled_strategies TEXT)"
    )
    RSET.ensure_schema(conn)
    return conn


def _backdate_proposal(conn, days_ago, key="max_exposure_pct",
                       new_value=94.0, strategy_id="sector_rotation"):
    AR.ensure_proposals_table(conn)
    proposed_at = (dt.datetime.now() - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")
    existing = conn.execute(
        """SELECT id FROM risk_expansion_proposals
            WHERE strategy_id=? AND key=? AND new_value=? AND status='pending'""",
        (strategy_id, key, new_value),
    ).fetchone()
    if existing:
        conn.execute("UPDATE risk_expansion_proposals SET proposed_at=? WHERE id=?",
                     (proposed_at, existing["id"]))
    else:
        conn.execute(
            """INSERT INTO risk_expansion_proposals(
                   strategy_id,key,old_value,new_value,evidence_count,proposed_at,status)
               VALUES(?,?,?,?,?,?, 'pending')""",
            (strategy_id, key, 92.0, new_value, FULL_EVIDENCE, proposed_at),
        )
    conn.commit()


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
        conn = _db()
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 85), evidence_count=None, conn=conn)
        self.assertTrue(gate["allowed"])
        self.assertEqual(1, len(gate["tightenings"]))

    def test_expansion_without_evidence_is_rejected(self):
        conn = _db()
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), evidence_count=None, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertIn("证据样本数", gate["violations"][0])

    def test_expansion_with_low_evidence_is_rejected(self):
        conn = _db()
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), evidence_count=6, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertIn("证据不足", gate["violations"][0])
        self.assertGreater(AR.EXPANSION_MIN_SAMPLES, AR.TIGHTEN_MIN_SAMPLES)

    def test_expansion_without_persisted_proposal_is_rejected_and_registered(self):
        conn = _db()
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertIn("尚未登记", gate["violations"][0])
        # 登记动作由显式 API 完成（runtime_settings.update 在拒绝前调用）。
        registered = AR.ensure_proposals_registered(
            conn, CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE)
        self.assertEqual(1, len(registered))
        rows = conn.execute("SELECT * FROM risk_expansion_proposals").fetchall()
        self.assertEqual(1, len(rows))  # 观察时钟自此起算

    def test_window_from_persisted_proposal_gates_the_expansion(self):
        conn = _db()
        _backdate_proposal(conn, days_ago=3)
        early = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertFalse(early["allowed"])
        self.assertIn("观察期未满", early["violations"][0])
        _backdate_proposal(conn, days_ago=12)
        mature = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertTrue(mature["allowed"])
        self.assertEqual(1, len(mature["expansions"]))

    def test_caller_asserted_days_are_never_trusted(self):
        # 没有 conn / 没有落库提案时，观察期一律不满足（自报天数无效）。
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), evidence_count=FULL_EVIDENCE)
        self.assertFalse(gate["allowed"])

    def test_single_round_step_cap_blocks_big_jumps_even_with_evidence(self):
        conn = _db()
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 99),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertIn("单轮放大超过硬上限", gate["violations"][0])

    def test_position_cap_step_is_one_seat(self):
        conn = _db()
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_positions", 6),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertIn("单轮放大超过硬上限", gate["violations"][0])
        _backdate_proposal(conn, days_ago=12, key="max_positions", new_value=4.0)
        ok = AR.validate_risk_updates(
            CURRENT, _proposed("max_positions", 4),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertTrue(ok["allowed"])

    def test_all_direction_keys_are_capped(self):
        self.assertEqual(set(AR.MAX_SINGLE_ROUND_STEP), set(AR.RISK_DIRECTION_KEYS))


class RuntimeSettingsWiringTests(unittest.TestCase):
    def test_ui_cannot_expand_risk_without_evidence_context(self):
        conn = _db()
        payload = {"strategy_overrides": _proposed("max_exposure_pct", 95)}
        with self.assertRaises(ValueError) as ctx:
            RSET.update(conn, payload, actor="human-ui")
        self.assertIn("证据样本数", str(ctx.exception))

    def test_ui_can_tighten_risk_immediately(self):
        conn = _db()
        payload = {"strategy_overrides": _proposed("max_exposure_pct", 85)}
        result = RSET.update(conn, payload, actor="human-ui")
        self.assertEqual(
            85.0,
            result["strategy"]["strategy_overrides"]["sector_rotation"]["max_exposure_pct"],
        )

    def test_evolution_path_is_the_only_production_expansion_route(self):
        conn = _db()
        # 第一次调用：证据达标但无提案 → 自动登记、拒绝（观察时钟启动）。
        with self.assertRaises(ValueError) as ctx:
            RSET.apply_evolution_risk_update(
                conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
                evidence_count=FULL_EVIDENCE)
        self.assertIn("尚未登记", str(ctx.exception))
        # 观察期满后重试同一入口即可生效。
        _backdate_proposal(conn, days_ago=12)
        result = RSET.apply_evolution_risk_update(
            conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
            evidence_count=FULL_EVIDENCE)
        self.assertEqual(
            94.0,
            result["strategy"]["strategy_overrides"]["sector_rotation"]["max_exposure_pct"],
        )

    def test_every_expansion_is_audited(self):
        conn = _db()
        _backdate_proposal(conn, days_ago=12)
        RSET.apply_evolution_risk_update(
            conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
            evidence_count=FULL_EVIDENCE)
        rows = conn.execute(
            "SELECT key,old_value,new_value,updated_by FROM paper_runtime_settings_audit"
        ).fetchall()
        self.assertTrue(any(row["key"] == "strategy_overrides" for row in rows))


if __name__ == "__main__":
    unittest.main()
