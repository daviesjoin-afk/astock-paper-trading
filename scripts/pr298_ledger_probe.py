"""PR298 固定时钟账本对照；在待比较 checkout 根目录运行，参数为输出 JSON。
仅临时测试账本；需 freezegun（验收工具依赖，不加入应用依赖）。"""

import json, sys, unittest, sqlite3
from freezegun import freeze_time

from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "backend"))
import paper_trading as PT
import test_production_path_golden_replay as G
import test_risk_application_service as R
from unittest import mock

clock = freeze_time("2026-10-30 09:00:00", tz_offset=8)
clock.start()
out = {}
tables = [
    "paper_orders",
    "paper_fills",
    "paper_accounts",
    "paper_positions",
    "paper_position_lots",
    "paper_capital_reservations",
    "paper_nav",
    "paper_audit",
    "paper_cycles",
    "paper_position_risk_state",
    "paper_risk_decisions",
    "paper_risk_log",
]


def capture(name):
    with sqlite3.connect(PT.DB_PATH) as c:
        names = {x[0] for x in c.execute("select name from sqlite_master where type='table'")}
        out.setdefault(name, {})["ledger"] = {
            t: [dict(zip([x[0] for x in cur.description], row, strict=True)) for row in cur]
            for t in tables
            if t in names
            for cur in [c.execute("select * from " + t + " order by rowid")]
        }


class Risk(R.RiskServiceContractTests):
    def run_risk(self):
        result = super().run_risk()
        out.setdefault(self._testMethodName, {}).setdefault("risk_results", []).append(result)
        return result

    def tearDown(self):
        capture(self._testMethodName)


class Golden(G.ProductionInvariantTests):
    def test_resume_fencing(self):
        original = PT._assert_active_lease

        def fence(conn, phase="ledger mutation"):
            if phase == "resume pending execution order":
                capture("fence_before")
                raise RuntimeError("paper lease lost: acceptance")
            return original(conn, phase)

        with mock.patch.object(PT, "_assert_active_lease", side_effect=fence):
            with self.assertRaisesRegex(RuntimeError, "paper lease lost:"):
                self.test_r26_partial_fill_retry_is_idempotent_and_finishes_same_order()
        capture("fence_after")
        self.assertEqual(out["fence_before"], out["fence_after"])

    def tearDown(self):
        capture(self._testMethodName)


s = unittest.TestSuite()
for n in [
    "test_rsvc5_external_io_cycle_rollover_fails_closed",
    "test_rsvc7_risk_sell_commits_through_execution_planner_once",
    "test_rsvc8_partial_risk_sell_advances_take_stage",
    "test_rsvc9_full_risk_sell_closes_episode_from_authoritative_lots",
    "test_rsvc13_risk_log_and_audit_counts_stay_exactly_once",
]:
    s.addTest(Risk(n))
for n in [
    "test_r26_partial_fill_retry_is_idempotent_and_finishes_same_order",
    "test_full_replay_digest_is_deterministic",
    "test_resume_fencing",
]:
    s.addTest(Golden(n))
r = unittest.TextTestRunner().run(s)
Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf8")
sys.exit(not r.wasSuccessful())
