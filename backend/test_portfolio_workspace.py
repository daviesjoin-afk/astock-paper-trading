from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import portfolio_allocation_policy as PAP
import portfolio_allocation_repository as PAPRepo
import portfolio_runtime as PR
import portfolio_runtime_repository as PRRepo
import portfolio_workspace_service as PWS


def _workspace_fixtures():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE portfolio_runtime_snapshots(
            snapshot_id TEXT PRIMARY KEY,snapshot_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE portfolio_allocation_plans(
            plan_id TEXT PRIMARY KEY,plan_fingerprint TEXT NOT NULL,
            plan_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY,cycle_id INTEGER,account_id TEXT,code TEXT,
            side TEXT,qty INTEGER,status TEXT,allocation_intent_kind TEXT,
            portfolio_snapshot_id TEXT,allocation_plan_id TEXT,
            allocation_plan_fingerprint TEXT,allocation_policy_version TEXT,
            strategy_id TEXT,strategy_version INTEGER,strategy_checksum TEXT);
        """)
    dimensions = tuple(PR.PortfolioDimension(
        name=name, status=PR.UNAVAILABLE, facts={}, provenance="UNAVAILABLE",
        blocking_reasons=(f"{name}_unavailable",)) for name in (
            "capital", "strategy_exposure", "concentration", "turnover",
            "risk_consumption", "signal_conflicts", "capacity", "correlation"))
    snapshot = PR.build_portfolio_runtime_snapshot(
        cycle_id=7, asof_day="2026-10-04",
        decision_at="2026-10-04T09:30:00+08:00",
        cycle_identity={"cycle_id": 7, "cycle_key": "workspace-test"},
        strategy_pins=[{"account_id": "account-a", "strategy_id": "strategy-a",
                        "strategy_version": 1, "strategy_checksum": "a" * 64}],
        economic_owner_ids=["account-a"], execution_participant_ids=["account-a"],
        risk_exit_participant_ids=["account-a"],
        source_identities={"cycle_owner": "test"},
        market_evidence_identity=None,
        dimensions=dimensions)
    PRRepo.append_snapshot(conn, snapshot)
    plan = PAP.build_portfolio_allocation_plan(
        snapshot=snapshot,
        declarations=[PAP.StrategyResourceDeclaration(
            account_id="account-a", strategy_id="strategy-a", strategy_version=1,
            strategy_checksum="a" * 64, max_positions=6, min_positions=1)],
        weights={"account-a": 1.0},
        intents=[PAP.ResourceIntent(
            intent_id="workspace-entry-1", account_id="account-a",
            intent_kind="NEW_ENTRY", symbol="600000")],
        hard_pool_cap=15, strategy_max_positions=6, strategy_min_positions=1,
        protected_slot_floor=1, account_order={}, shared_pool_max_exposure=0.82,
        strategy_pool_floor_ratio=0.60)
    PAPRepo.append_plan(conn, plan)
    return conn, snapshot, plan


class PortfolioWorkspaceServiceTests(unittest.TestCase):
    def test_c8_existing_exposure_sell_path_does_not_require_an_allocation_plan(self):
        source = (Path(PWS.__file__).with_name("manual_orders.py")
                  .read_text(encoding="utf-8"))
        start = source.index("def _manual_order_plan(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]
        buy_branch = body.index('if side == "buy":')
        sell_branch = body.index('else:\n        position_limit = 0', buy_branch)
        self.assertIn("_build_portfolio_entry_plan(", body[buy_branch:sell_branch])
        self.assertNotIn("_build_portfolio_entry_plan(", body[sell_branch:])

    def test_workspace_reads_exact_named_cycle_snapshot_plan_and_orders(self):
        conn, snapshot, plan = _workspace_fixtures()
        conn.execute(
            """INSERT INTO paper_orders VALUES(
                5,7,'account-a','600000','buy',100,'filled','NEW_ENTRY',?,?,?,?,?,1,?)""",
            (snapshot.snapshot_id, plan.plan_id, plan.plan_fingerprint,
             plan.allocation_policy_version, "strategy-a", "a" * 64))
        result = PWS.get_portfolio_workspace(
            conn, cycle_id=7, plan_id=plan.plan_id)
        self.assertEqual("read_only_exact_plan_projection", result["authority"])
        self.assertEqual(snapshot.snapshot_id,
                         result["portfolio_snapshot"]["snapshot_id"])
        self.assertEqual(plan.plan_id, result["allocation_plan"]["plan_id"])
        self.assertEqual(5, result["order_provenance"]["orders"][0]["order_id"])
        self.assertEqual("AVAILABLE", result["order_provenance"]["status"])
        self.assertIsNone(result["risk_decision"])

        later_plan = PAP.build_portfolio_allocation_plan(
            snapshot=snapshot,
            declarations=[PAP.StrategyResourceDeclaration(
                account_id="account-a", strategy_id="strategy-a", strategy_version=1,
                strategy_checksum="a" * 64, max_positions=6, min_positions=1)],
            weights={"account-a": 0.75},
            intents=[PAP.ResourceIntent(
                intent_id="workspace-entry-later", account_id="account-a",
                intent_kind="NEW_ENTRY", symbol="600000")],
            hard_pool_cap=15, strategy_max_positions=6, strategy_min_positions=1,
            protected_slot_floor=1, account_order={}, shared_pool_max_exposure=0.82,
            strategy_pool_floor_ratio=0.60)
        PAPRepo.append_plan(conn, later_plan)
        historical = PWS.get_portfolio_workspace(
            conn, cycle_id=7, plan_id=plan.plan_id)
        self.assertEqual(plan.plan_id, historical["allocation_plan"]["plan_id"])
        self.assertEqual(plan.plan_id,
                         historical["order_provenance"]["orders"][0]["allocation_plan_id"])

    def test_workspace_requires_matching_cycle_and_never_looks_up_latest(self):
        conn, _snapshot, plan = _workspace_fixtures()
        source = open(PWS.__file__, encoding="utf-8").read()
        self.assertIn('PAPRepo.get_plan(conn, str(plan_id or ""))', source)
        for forbidden in ("get_latest_plan(", "get_current_plan(", "ORDER BY created_at DESC"):
            self.assertNotIn(forbidden, source)
        manual = open(PWS.__file__.replace("portfolio_workspace_service.py",
                                           "manual_orders.py"), encoding="utf-8").read()
        self.assertNotIn("get_latest_plan(", manual)
        self.assertIn('allocation_provenance["portfolio_snapshot_id"]', manual)
        with self.assertRaisesRegex(PWS.PortfolioWorkspaceUnavailable,
                                    "portfolio_workspace_cycle_mismatch"):
            PWS.get_portfolio_workspace(conn, cycle_id=8, plan_id=plan.plan_id)

    def test_workspace_marks_corrupt_order_provenance_unavailable(self):
        conn, snapshot, plan = _workspace_fixtures()
        conn.execute(
            """INSERT INTO paper_orders VALUES(
                9,7,'account-a','600000','buy',100,'filled','NEW_ENTRY',?,?,?,?,?,1,?)""",
            (snapshot.snapshot_id, plan.plan_id, "0" * 64,
             plan.allocation_policy_version, "strategy-a", "a" * 64))
        result = PWS.get_portfolio_workspace(conn, cycle_id=7, plan_id=plan.plan_id)
        self.assertEqual("UNAVAILABLE", result["order_provenance"]["status"])
        self.assertEqual("order_allocation_provenance_mismatch",
                         result["order_provenance"]["unknown_orders"][0]["reason"])

    def test_workspace_checks_order_strategy_pin_and_intent_against_exact_plan(self):
        conn, snapshot, plan = _workspace_fixtures()
        conn.execute(
            """INSERT INTO paper_orders VALUES(
                12,7,'account-a','600000','buy',100,'filled','NEW_ENTRY',?,?,?,?,?,1,?)""",
            (snapshot.snapshot_id, plan.plan_id, plan.plan_fingerprint,
             plan.allocation_policy_version, "strategy-a", "a" * 64))
        result = PWS.get_portfolio_workspace(conn, cycle_id=7, plan_id=plan.plan_id)
        self.assertEqual("AVAILABLE", result["order_provenance"]["status"])

        conn.execute("UPDATE paper_orders SET strategy_checksum=? WHERE id=12", ("b" * 64,))
        pin_mismatch = PWS.get_portfolio_workspace(
            conn, cycle_id=7, plan_id=plan.plan_id)
        self.assertEqual("UNAVAILABLE", pin_mismatch["order_provenance"]["status"])

        conn.execute("UPDATE paper_orders SET strategy_checksum=?,allocation_intent_kind=? WHERE id=12",
                     ("a" * 64, "ADD_POSITION"))
        intent_mismatch = PWS.get_portfolio_workspace(
            conn, cycle_id=7, plan_id=plan.plan_id)
        self.assertEqual("UNAVAILABLE", intent_mismatch["order_provenance"]["status"])

    def test_c29_workspace_ui_renders_backend_facts_without_allocation_math(self):
        repo = Path(PWS.__file__).resolve().parents[1]
        source = (repo / "frontend" / "src" / "features" / "paper.js").read_text(
            encoding="utf-8")
        start = source.index("export async function loadPortfolioWorkspace()")
        end = source.index("export function paperOverviewSignature", start)
        body = source[start:end]
        self.assertIn("/api/portfolio/workspace?cycle_id=", body)
        self.assertIn("workspaceJson({facts:x.facts", body)
        for forbidden in ("Math.", ".reduce(", ".sort(", "get_latest_plan(",
                          "get_current_plan("):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
