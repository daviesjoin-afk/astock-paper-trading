# -*- coding: utf-8 -*-
"""PR-11 B：风险快通道作为**独立 lane** 的回归。

这里的断言只覆盖 lane 边界本身——运行身份（三分钟窗口）、运行租约隔离、
重复跳转的幂等，以及观测到"这一分钟的风控是风险 lane 跑的"。风险算法、成交
权威与保护性减仓的逐字段等价性由 ``test_paper_risk_exit_production_path`` 的
RISK-SCAN-* 用例负责，不在这里复制一份。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

import paper_dispatch_metrics as PDM
import paper_trading as PT
from test_paper_risk_exit_production_path import PaperRiskExitProductionPathTestCase

REAL_DATETIME = dt.datetime


def _fixed_clock(moment):
    """把 ``PT.dt.datetime.now()`` 固定到某一分钟（跨模块共享同一 datetime 对象）。"""

    class FixedDateTime(REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.replace(tzinfo=tz)

    return mock.patch.object(PT.dt, "datetime", FixedDateTime)


class RiskWindowKeyTests(unittest.TestCase):
    def test_window_key_buckets_to_the_three_minute_grid(self):
        # 取整规则与 _intraday_business_key 相同：minute // 3 * 3，因此 14:48/14:51
        # 是两个相邻的调度窗口，14:50 属于 14:48 那个窗口。
        self.assertEqual("risk:202609101448",
                         PT._risk_business_key(REAL_DATETIME(2026, 9, 10, 14, 48, 5)))
        self.assertEqual("risk:202609101448",
                         PT._risk_business_key(REAL_DATETIME(2026, 9, 10, 14, 50, 59)))
        self.assertEqual("risk:202609101451",
                         PT._risk_business_key(REAL_DATETIME(2026, 9, 10, 14, 51, 0)))

    def test_risk_and_intraday_never_share_a_run_key(self):
        moment = REAL_DATETIME(2026, 9, 10, 14, 48)
        self.assertEqual("intraday:202609101448", PT._slot_run_key("intraday", moment))
        self.assertEqual("risk:202609101448", PT._slot_run_key("risk", moment))
        self.assertIsNone(PT._slot_run_key("close", moment))
        self.assertIsNone(PT._slot_run_key("open", moment))

    def test_run_slot_takes_the_window_key_and_the_lane_lease_from_one_place(self):
        with open(PT.__file__, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("run_key = _slot_run_key(slot, run_asof)", text)
        self.assertIn('"paper-runner-risk" if slot == "risk" else "paper-runner-global"', text)


class RiskLaneRuntimeTests(PaperRiskExitProductionPathTestCase):
    """真实 SQLite + 真实 run_slot：租约、窗口幂等与观测归属。"""

    def _run(self, slot="risk", moment=None):
        buffer = io.StringIO()
        context = _fixed_clock(moment) if moment else contextlib.nullcontext()
        with context, contextlib.redirect_stderr(buffer):
            result = PT.run_slot(slot, asof_date=self.day, force=True)
        events = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        return result, events

    def _lease_rows(self):
        with PT._db() as conn:
            return {row["lock_key"]: dict(row) for row in conn.execute(
                "SELECT lock_key, owner_key, slot, fencing_token FROM paper_runtime_locks")}

    def test_risk_lane_does_not_wait_for_the_global_runtime_lease(self):
        """intraday 正持有全局租约时，风险 lane 仍必须能跑（不是 in_progress）。"""
        with PT._db(immediate=True) as conn:
            acquired, owner, _ = PT._claim_runtime_lease(
                conn, "paper-runner-global", "intraday-owner", "intraday", ttl_seconds=300)
        self.assertTrue(acquired)

        result, _events = self._run(moment=REAL_DATETIME(2026, 9, 10, 14, 48))

        self.assertNotEqual("in_progress", result.get("status"), result)
        leases = self._lease_rows()
        self.assertIn("paper-runner-global", leases, "风险 lane 抢走了全局租约")
        self.assertTrue(leases["paper-runner-global"]["owner_key"].startswith("intraday-owner:"))
        self.assertEqual("intraday", leases["paper-runner-global"]["slot"])
        self.assertNotIn("paper-runner-risk", leases, "风险 lane 完成后必须释放自己的租约")

    def test_second_tick_in_the_same_window_is_already_done(self):
        moment = REAL_DATETIME(2026, 9, 10, 14, 48)
        first, _ = self._run(moment=moment)
        self.assertNotEqual("already_done", first.get("status"), first)
        second, _ = self._run(moment=moment)
        self.assertEqual("already_done", second.get("status"))
        self.assertEqual("risk", second.get("slot"))

    def test_next_window_runs_again(self):
        first, _ = self._run(moment=REAL_DATETIME(2026, 9, 10, 14, 48))
        self.assertNotEqual("already_done", first.get("status"), first)
        later, _ = self._run(moment=REAL_DATETIME(2026, 9, 10, 14, 51))
        self.assertNotEqual("already_done", later.get("status"),
                            "下一个三分钟窗口被上一窗口吞掉：风险节拍会停在第一跳")
        with PT._db() as conn:
            keys = [row["run_key"] for row in conn.execute(
                "SELECT run_key FROM paper_job_runs WHERE slot='risk' ORDER BY run_key")]
        self.assertEqual(["risk:202609101448", "risk:202609101451"], keys)

    def test_risk_lane_reports_its_own_completed_scan_marker(self):
        _result, events = self._run(moment=REAL_DATETIME(2026, 9, 10, 14, 48))
        self.assertEqual(1, len(events), events)
        event = events[0]
        self.assertEqual("risk", event["timing"]["slot"])
        self.assertEqual(["completed"], [s["status"] for s in event["timing"]["risk_scans"]])
        summary = PDM.summarize(events)
        self.assertEqual("risk_slot", summary["last_successful_risk_scan"]["source"])
        self.assertEqual({"risk": {"completed": 1}}, summary["risk_scans"]["by_slot"])

    def test_intraday_fallback_in_the_same_minute_is_already_scanned(self):
        """风险 lane 先跑完，同分钟 intraday 的 monitor_risk 只能 already_scanned。"""
        self._run(moment=REAL_DATETIME(2026, 9, 10, 14, 48))
        buffer = io.StringIO()
        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 48)), contextlib.redirect_stderr(buffer):
            with PT.PTM.capture("intraday"):
                fallback = PT.monitor_risk(self.day)
                PT.PTM.outcome(PT._slot_completion_status(fallback))
        self.assertEqual("already_scanned", fallback.get("status"))
        self.assertEqual([], fallback.get("orders"))
        event = json.loads(buffer.getvalue().splitlines()[0])
        self.assertEqual(["already_scanned"],
                         [s["status"] for s in event["timing"]["risk_scans"]])
        summary = PDM.summarize([event])
        self.assertIsNone(summary["last_successful_risk_scan"]["at"],
                          "intraday 兜底没有真扫描，不能记成一次成功风控")


if __name__ == "__main__":
    unittest.main()
