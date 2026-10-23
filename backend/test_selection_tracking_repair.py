# -*- coding: utf-8 -*-
"""用原始报价证据修正盘中污染观测的回归（B2 后续）。

B2 修复只阻止**新的**盘中写入。已经落库的行需要可靠证据才能修正，本文件锁定
三条底线：

1. 证据必须是**收盘窗口内**的原始报价：盘中归档、隔日归档一律拒绝；
2. 只改被证据证明有误的行，没有证据的行原样保留；
3. 默认 dry-run，执行时用旧值比对，避免覆盖期间发生的其它写入。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

import selection_tracking as ST
import selection_tracking_repair as REPAIR

CHINA_TZ = ST.CHINA_TZ
DAY = "2026-10-23"
AFTER_CLOSE = f"{DAY}T17:15:00+08:00"


def _rows(price_by_code, quote_at=f"{DAY} 15:00:00"):
    return [
        {"code": code, "name": code, "price": price, "quote_at": quote_at}
        for code, price in price_by_code.items()
    ]


def _snapshot(price_by_code, saved_at=AFTER_CLOSE, quote_at=f"{DAY} 15:00:00"):
    rows = _rows(price_by_code, quote_at)
    return {"complete": True, "rows": rows, "saved_at": saved_at, "expected_rows": len(rows)}


class RepairCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        os.unlink(self.db_path)
        self.addCleanup(lambda: [os.unlink(path) for path in (
            self.db_path, self.db_path + "-wal", self.db_path + "-shm")
            if os.path.exists(path)])
        patches = [
            mock.patch.object(ST, "DB_PATH", self.db_path),
            # 不联网、不读真实 K 线：基准价固定，便于断言重算结果。
            mock.patch.object(REPAIR, "_benchmark_price", return_value=4000.0),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        ST.ensure_schema()
        self.pick_ids = self._seed()

    def _seed(self):
        """一条昨日入选 10 元的候选，外加两条已写入的观测行。"""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO selection_runs(
                    run_date, generated_at, strategy, strategy_name, data_asof_date,
                    benchmark_entry_price, universe_size, candidate_count,
                    selected_count, executable_count, source, result_json,
                    provenance_status, provenance_key, asof_day, scope, cycle_id,
                    strategy_id, strategy_version, strategy_checksum
                ) VALUES ('2026-10-22', '2026-10-22T15:05:00+08:00', 'three_day', '3 day',
                          '2026-10-22', 4000.0, 5000, 10, 3, 3, 'test', '{}',
                          'not_applicable', 'k|2026-10-22', '2026-10-22', 'research',
                          NULL, NULL, NULL, NULL)
                """,
            )
            run_id = int(cursor.lastrowid)
            pick_ids = {}
            for rank, code in enumerate(("600000", "000001", "300750"), start=1):
                pick = conn.execute(
                    """
                    INSERT INTO selection_picks(
                        run_id, rank_no, code, name, industry, entry_price,
                        score, super_net, decision_tier, decision_action, snapshot_json
                    ) VALUES (?, ?, ?, ?, 'bank', 10.0, 1.0, 0.0, 'A', 'buy', '{}')
                    """,
                    (run_id, rank, code, code),
                )
                pick_ids[code] = int(pick.lastrowid)
            for code, price in (("600000", 11.0), ("000001", 10.5), ("300750", 9.5)):
                conn.execute(
                    """
                    INSERT INTO selection_observations(
                        pick_id, observed_date, price, benchmark_price, holding_days,
                        return_pct, benchmark_return_pct, excess_return_pct
                    ) VALUES (?, ?, ?, 4000.0, 1, ?, 0.0, ?)
                    """,
                    (pick_ids[code], DAY, price, (price / 10.0 - 1) * 100,
                     (price / 10.0 - 1) * 100),
                )
        return pick_ids

    def _observation(self, code):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute(
                """
                SELECT o.price, o.return_pct, o.excess_return_pct, o.holding_days
                FROM selection_observations o JOIN selection_picks p ON p.id = o.pick_id
                WHERE p.code = ?
                """,
                (code,),
            ).fetchone())


class EvidenceValidationTests(RepairCase):
    def test_intraday_archive_is_rejected(self):
        with self.assertRaises(REPAIR.RepairError) as ctx:
            REPAIR.build_plan(
                _snapshot({"600000": 10.0}, saved_at=f"{DAY}T11:00:00+08:00"),
                db_path=self.db_path,
            )
        self.assertIn("15:00", str(ctx.exception))

    def test_previous_session_archive_only_proves_its_own_day(self):
        plan = REPAIR.build_plan(
            _snapshot({"600000": 10.0}, saved_at="2026-10-22T17:15:00+08:00",
                      quote_at="2026-10-22 15:00:00"),
            db_path=self.db_path,
        )
        self.assertEqual(plan["evidence"]["business_date"], "2026-10-22")
        self.assertEqual(plan["updates"], [], "隔日证据不能改写今天的观测")
        self.assertEqual(plan["no_evidence"], 3)

    def test_mislabeled_evidence_is_rejected(self):
        """归档时点是今天，但行里的报价来自昨天 → 收盘窗口内没有可用事实。"""
        with self.assertRaises(REPAIR.RepairError):
            REPAIR.build_plan(
                _snapshot({"600000": 10.0}, saved_at=AFTER_CLOSE,
                          quote_at="2026-10-22 15:00:00"),
                db_path=self.db_path,
            )

    def test_evidence_without_a_timestamp_is_rejected(self):
        with self.assertRaises(REPAIR.RepairError):
            REPAIR.build_plan(_rows({"600000": 10.0}), db_path=self.db_path)

    def test_bare_row_list_works_with_an_explicit_asof(self):
        plan = REPAIR.build_plan(
            _rows({"600000": 10.0}), db_path=self.db_path, asof=AFTER_CLOSE,
        )
        self.assertEqual(plan["evidence"]["business_date"], DAY)
        self.assertEqual(plan["evidence"]["verified_quotes"], 1)

    def test_unrecognised_evidence_fails_closed(self):
        with self.assertRaises(REPAIR.RepairError):
            REPAIR.build_plan("not-json", db_path=self.db_path)


class PlanTests(RepairCase):
    def test_plan_only_touches_rows_with_a_proven_close(self):
        plan = REPAIR.build_plan(_snapshot({"600000": 10.0}), db_path=self.db_path)
        self.assertEqual(plan["scanned_rows"], 3)
        self.assertEqual(len(plan["updates"]), 1, "只有证据覆盖的 600000 才可修正")
        self.assertEqual(plan["no_evidence"], 2)
        update = plan["updates"][0]
        self.assertEqual(update["old_price"], 11.0)
        self.assertEqual(update["new_price"], 10.0)
        self.assertAlmostEqual(update["new_return_pct"], 0.0, places=6)
        self.assertEqual(self._observation("600000")["price"], 11.0, "dry-run 不得写库")

    def test_row_already_matching_the_verified_close_is_unchanged(self):
        plan = REPAIR.build_plan(_snapshot({"000001": 10.5}), db_path=self.db_path)
        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["unchanged"], 1)

    def test_rows_from_another_business_day_are_not_rewritten(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE selection_observations SET observed_date='2026-10-22'")
        plan = REPAIR.build_plan(_snapshot({"600000": 10.0}), db_path=self.db_path)
        self.assertEqual(plan["updates"], [], "隔日证据不得改写当日观测")
        self.assertEqual(plan["no_evidence"], 3)

    def test_since_filter_limits_the_scan(self):
        plan = REPAIR.build_plan(
            _snapshot({"600000": 10.0}), db_path=self.db_path, since="2026-10-24",
        )
        self.assertEqual(plan["scanned_rows"], 0)


class ApplyTests(RepairCase):
    def test_apply_rewrites_price_and_derived_returns(self):
        plan = REPAIR.build_plan(
            _snapshot({"600000": 10.0, "000001": 10.0, "300750": 10.0}),
            db_path=self.db_path,
        )
        self.assertEqual(len(plan["updates"]), 3)
        REPAIR.apply_plan(plan)
        self.assertTrue(plan["applied"])
        self.assertEqual(plan["applied_rows"], 3)
        fixed = self._observation("600000")
        self.assertEqual(fixed["price"], 10.0)
        self.assertAlmostEqual(fixed["return_pct"], 0.0, places=6)
        self.assertAlmostEqual(fixed["excess_return_pct"], 0.0, places=6)
        self.assertEqual(fixed["holding_days"], 1, "holding_days 语义不变")

    def test_apply_is_idempotent(self):
        plan = REPAIR.build_plan(_snapshot({"600000": 10.0}), db_path=self.db_path)
        REPAIR.apply_plan(plan)
        again = REPAIR.build_plan(_snapshot({"600000": 10.0}), db_path=self.db_path)
        self.assertEqual(again["updates"], [], "第二次不得再产生修改")
        self.assertEqual(again["unchanged"], 1)

    def test_apply_skips_rows_changed_after_planning(self):
        plan = REPAIR.build_plan(_snapshot({"600000": 10.0}), db_path=self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE selection_observations SET price=12.0 WHERE pick_id=?",
                         (self.pick_ids["600000"],))
        result = REPAIR.apply_plan(plan)
        self.assertEqual(result["applied_rows"], 0, "旧值不匹配时不得覆盖")
        self.assertIn("note", result)
        self.assertEqual(self._observation("600000")["price"], 12.0)

    def test_cli_dry_run_reports_without_writing(self):
        evidence_path = os.path.join(os.path.dirname(self.db_path), "evidence.json")
        with open(evidence_path, "w", encoding="utf-8") as handle:
            json.dump(_snapshot({"600000": 10.0}), handle)
        self.addCleanup(os.unlink, evidence_path)
        code = REPAIR.main(["--evidence", evidence_path, "--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertEqual(self._observation("600000")["price"], 11.0)

    def test_cli_rejects_intraday_evidence_with_exit_code_2(self):
        evidence_path = os.path.join(os.path.dirname(self.db_path), "intraday.json")
        with open(evidence_path, "w", encoding="utf-8") as handle:
            json.dump(_snapshot({"600000": 10.0}, saved_at=f"{DAY}T10:30:00+08:00"), handle)
        self.addCleanup(os.unlink, evidence_path)
        code = REPAIR.main(["--evidence", evidence_path, "--db", self.db_path, "--apply"])
        self.assertEqual(code, 2)
        self.assertEqual(self._observation("600000")["price"], 11.0)


if __name__ == "__main__":
    unittest.main()
