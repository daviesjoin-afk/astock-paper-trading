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
        # PR-33：闸门内部自动登记，观察时钟自此起算；重复登记不会产生第二条。
        self.assertEqual(1, len(gate["registered"]))
        registered = AR.ensure_proposals_registered(
            conn, CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE)
        self.assertEqual(0, len(registered))  # 已有 pending，不重复登记
        rows = conn.execute("SELECT * FROM risk_expansion_proposals").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("pending", rows[0]["status"])

    def test_window_from_persisted_proposal_gates_the_expansion(self):
        conn = _db()
        _backdate_proposal(conn, days_ago=3)
        early = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE, conn=conn, challenger_win=True)
        self.assertFalse(early["allowed"])
        self.assertIn("观察期未满", early["violations"][0])
        _backdate_proposal(conn, days_ago=12)
        mature = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE, conn=conn, challenger_win=True)
        self.assertTrue(mature["allowed"])
        self.assertEqual(1, len(mature["expansions"]))

    def test_expansion_requires_challenger_win(self):
        """PR-33：证据 + 观察期都满足，但没有 Challenger 胜出 → 仍然拒绝。"""
        conn = _db()
        _backdate_proposal(conn, days_ago=12)
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94),
            evidence_count=FULL_EVIDENCE, conn=conn)
        self.assertFalse(gate["allowed"])
        self.assertIn("Challenger 胜出", gate["violations"][0])

    def test_caller_asserted_days_are_never_trusted(self):
        # 没有 conn / 没有落库提案时，观察期一律不满足（自报天数无效）。
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), evidence_count=FULL_EVIDENCE,
            challenger_win=True)
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
            evidence_count=FULL_EVIDENCE, conn=conn, challenger_win=True)
        self.assertTrue(ok["allowed"])

    def test_all_direction_keys_are_capped(self):
        self.assertEqual(set(AR.MAX_SINGLE_ROUND_STEP), set(AR.RISK_DIRECTION_BY_KEY))
        self.assertTrue(set(AR.RISK_DIRECTION_KEYS) <= set(AR.RISK_DIRECTION_BY_KEY))


class UnifiedRiskDirectionTests(unittest.TestCase):
    """PR-33：risk_per_trade / stop loosen / holding extension 统一方向建模。"""

    def test_stop_loosen_is_an_expansion(self):
        # 负域止损：-0.05 → -0.08 是放宽（风险放大）；-0.05 → -0.03 是收紧。
        self.assertEqual("expand", AR.classify_risk_change("hard_stop", -0.05, -0.08))
        self.assertEqual("tighten", AR.classify_risk_change("hard_stop", -0.05, -0.03))

    def test_unknown_keys_fail_closed_as_risk_increasing(self):
        # 未登记方向的参数一律按"数值变大 = 放大"处理，宁可拦错也不放过。
        self.assertEqual("expand", AR.classify_risk_change("ma_period", 5, 20))

    def test_risk_per_trade_and_holding_extension_directions(self):
        self.assertEqual("expand", AR.classify_risk_change("risk_per_trade", 0.22, 0.25))
        self.assertEqual("tighten", AR.classify_risk_change("risk_per_trade", 0.22, 0.20))
        self.assertEqual("expand", AR.classify_risk_change("holding_days", 5, 8))
        self.assertEqual("tighten", AR.classify_risk_change("holding_days", 5, 3))

    def test_declared_direction_overrides_the_key_table(self):
        self.assertEqual(
            "tighten",
            AR.classify_risk_change("risk_per_trade", 0.22, 0.25, "lower_is_riskier"),
        )

    def _evaluate(self, conn, key, old, new, *, evidence, challenger_win, backdate=None):
        if backdate is not None:
            _backdate_proposal(conn, days_ago=backdate, key=key, new_value=float(new))
        return AR.evaluate_risk_adjustments(
            conn, "sector_rotation", [{"key": key, "old": old, "new": new}],
            evidence_count=evidence, challenger_win=challenger_win,
        )

    def test_stop_loosen_needs_the_full_gate(self):
        conn = _db()
        gate = self._evaluate(conn, "hard_stop", -0.05, -0.06, evidence=None,
                              challenger_win=False)
        self.assertFalse(gate["allowed"])
        self.assertIn("证据样本数", gate["violations"][0])
        # 单轮放宽超过 1pp 一律拒绝（哪怕证据/观察期都满足）。
        wide = self._evaluate(conn, "hard_stop", -0.05, -0.09, evidence=25,
                              challenger_win=True, backdate=12)
        self.assertFalse(wide["allowed"])
        self.assertIn("单轮放大超过硬上限", wide["violations"][0])
        # 观察期 + Challenger 都满足才放行。
        gate = self._evaluate(conn, "hard_stop", -0.05, -0.06, evidence=25,
                              challenger_win=True, backdate=12)
        self.assertTrue(gate["allowed"], gate["violations"])

    def test_holding_extension_step_cap_is_two_days(self):
        conn = _db()
        gate = self._evaluate(conn, "holding_days", 5, 10, evidence=25,
                              challenger_win=True, backdate=12)
        self.assertFalse(gate["allowed"])
        self.assertIn("单轮放大超过硬上限", gate["violations"][0])

    def test_risk_per_trade_step_cap_is_one_point(self):
        conn = _db()
        gate = self._evaluate(conn, "risk_per_trade", 0.22, 0.30, evidence=25,
                              challenger_win=True, backdate=12)
        self.assertFalse(gate["allowed"])
        self.assertIn("单轮放大超过硬上限", gate["violations"][0])


class ProposalLifecycleTests(unittest.TestCase):
    """PR-33：提案 pending → promoted / rejected / superseded。"""

    def _register(self, conn, key="max_exposure_pct", new_value=94.0):
        return AR.register_proposal(conn, "sector_rotation", key, 92.0, new_value,
                                    evidence_count=FULL_EVIDENCE, actor="test")

    def test_register_then_promote(self):
        conn = _db()
        proposal = self._register(conn)
        self.assertEqual("pending", proposal["status"])
        self.assertEqual([p["id"] for p in AR.pending_proposals(conn)], [proposal["id"]])
        _backdate_proposal(conn, days_ago=12)
        AR.promote_proposal(conn, "sector_rotation", "max_exposure_pct", 94.0, actor="ui")
        rows = conn.execute(
            "SELECT status,resolved_by FROM risk_expansion_proposals WHERE id=?",
            (proposal["id"],)).fetchone()
        self.assertEqual("promoted", rows["status"])
        self.assertEqual("ui", rows["resolved_by"])
        self.assertEqual([], AR.pending_proposals(conn))

    def test_newer_proposal_supersedes_the_previous_pending(self):
        conn = _db()
        first = self._register(conn, new_value=94.0)
        second = self._register(conn, new_value=95.0)
        statuses = dict(conn.execute(
            "SELECT id,status FROM risk_expansion_proposals").fetchall())
        self.assertEqual("superseded", statuses[first["id"]])
        self.assertEqual("pending", statuses[second["id"]])
        self.assertEqual(1, len(AR.pending_proposals(conn)))

    def test_rejected_proposal_never_becomes_effective(self):
        conn = _db()
        proposal = self._register(conn)
        self.assertTrue(AR.reject_proposal(conn, proposal["id"], actor="human-ui",
                                           note="manual rejection"))
        # 被拒绝的提案不在 pending 中 → 观察期作废，放大必须重新登记并重新观察。
        gate = AR.validate_risk_updates(
            CURRENT, _proposed("max_exposure_pct", 94), evidence_count=FULL_EVIDENCE,
            conn=conn, challenger_win=True)
        self.assertFalse(gate["allowed"])
        self.assertIn("尚未登记", gate["violations"][0])
        status = conn.execute(
            "SELECT status FROM risk_expansion_proposals WHERE id=?",
            (proposal["id"],)).fetchone()
        self.assertEqual("rejected", status["status"])

    def test_lifecycle_columns_survive_legacy_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE risk_expansion_proposals(
                   id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL,
                   key TEXT NOT NULL, old_value REAL, new_value REAL NOT NULL,
                   evidence_count INTEGER, proposed_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'pending')""")
        AR.ensure_proposal_lifecycle_columns(conn)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(risk_expansion_proposals)").fetchall()}
        self.assertTrue({"resolved_at", "resolved_by", "note"} <= columns)


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
                evidence_count=FULL_EVIDENCE, challenger_win=True)
        self.assertIn("尚未登记", str(ctx.exception))
        # 观察期满 + Challenger 胜出后重试同一入口即可生效。
        _backdate_proposal(conn, days_ago=12)
        result = RSET.apply_evolution_risk_update(
            conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
            evidence_count=FULL_EVIDENCE, challenger_win=True)
        self.assertEqual(
            94.0,
            result["strategy"]["strategy_overrides"]["sector_rotation"]["max_exposure_pct"],
        )
        # 生效后对应提案进入 promoted，不会被重复利用。
        rows = conn.execute(
            "SELECT status FROM risk_expansion_proposals").fetchall()
        self.assertEqual(["promoted"], [row["status"] for row in rows])

    def test_evolution_cannot_expand_without_challenger_win(self):
        conn = _db()
        _backdate_proposal(conn, days_ago=12)
        with self.assertRaises(ValueError) as ctx:
            RSET.apply_evolution_risk_update(
                conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
                evidence_count=FULL_EVIDENCE)
        self.assertIn("Challenger 胜出", str(ctx.exception))

    def test_every_expansion_is_audited(self):
        conn = _db()
        _backdate_proposal(conn, days_ago=12)
        RSET.apply_evolution_risk_update(
            conn, {"strategy_overrides": _proposed("max_exposure_pct", 94)},
            evidence_count=FULL_EVIDENCE, challenger_win=True)
        rows = conn.execute(
            "SELECT key,old_value,new_value,updated_by FROM paper_runtime_settings_audit"
        ).fetchall()
        self.assertTrue(any(row["key"] == "strategy_overrides" for row in rows))


if __name__ == "__main__":
    unittest.main()
