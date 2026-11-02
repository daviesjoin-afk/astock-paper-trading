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
import paper_risk_scan_state as PRSS
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
        self.assertIn('"risk": "paper-runner-risk"', text)


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


class RiskScanWindowIdentityTests(unittest.TestCase):
    """扫描身份必须与三分钟窗口对齐，否则同一窗口会跑两次真实风控。"""

    #: 身份取整归 durable 扫描状态 owner（``paper_risk_scan_state``），
    #: ``paper_trading`` 只负责调用它——两边共用一个实现才不会再漂移。

    def test_scan_window_minute_matches_the_risk_business_key(self):
        # 09:34 与 09:35 属于同一个三分钟窗口（09:33 起算），身份必须相同。
        for moment in (REAL_DATETIME(2026, 9, 10, 9, 33, 5),
                       REAL_DATETIME(2026, 9, 10, 9, 34, 59),
                       REAL_DATETIME(2026, 9, 10, 9, 35, 0)):
            with self.subTest(moment=moment):
                self.assertEqual("2026-09-10 09:33", PRSS.window_minute(moment))
                self.assertEqual("risk:202609100933", PT._risk_business_key(moment))

    def test_scan_window_minute_rolls_to_the_next_window(self):
        self.assertEqual("2026-09-10 09:36",
                         PRSS.window_minute(REAL_DATETIME(2026, 9, 10, 9, 36, 1)))
        self.assertEqual("2026-09-10 14:57",
                         PRSS.window_minute(REAL_DATETIME(2026, 9, 10, 14, 59, 30)))


class RiskRunningGateTests(PaperRiskExitProductionPathTestCase):
    """P0：``running`` 不等于完成，intraday 不得在风控未完成时进入执行与候选。"""

    def test_intraday_gate_blocks_execution_and_candidates_while_risk_runs(self):
        resume = mock.Mock(return_value=[])
        refresh = mock.Mock(return_value=[])
        opening = mock.Mock(return_value={"orders": [], "execution_orders": []})
        running = {"status": "risk_running", "deferred": True,
                   "risk_scan_state": "running", "orders": []}
        with mock.patch.object(PT, "init_db"),                 mock.patch.object(PT, "_cleanup_stale_data"),                 mock.patch.object(PT.dfc, "check_data_source_health", return_value={}),                 mock.patch.object(PT, "monitor_risk", return_value=running),                 mock.patch.object(PT, "process_pending_execution_orders", resume),                 mock.patch.object(PT.MDSvc, "refresh_rows", refresh),                 mock.patch.object(PT, "execute_open", opening):
            result = PT.monitor_intraday(self.day, force=True)
        self.assertEqual("risk_in_progress", result["status"])
        self.assertTrue(result["deferred"])
        self.assertEqual([], result["execution_orders"])
        resume.assert_not_called()
        refresh.assert_not_called()
        opening.assert_not_called()
        # 外层 scheduler 必须看到非成功状态，才会在同一窗口内重试。
        self.assertEqual("failed", PT._slot_completion_status(result))

    def test_barrier_risk_still_running_returns_risk_running_and_does_not_complete(self):
        """可控 barrier：risk lane 认领后阻塞在执行中，intraday 兜底必须 fail-closed。"""
        import threading

        started = threading.Event()
        release = threading.Event()

        def blocking_impl(asof_date=None, *, cycle_id):
            started.set()
            if not release.wait(30):
                raise AssertionError("测试未释放 risk lane 阻塞")
            # 认领与完成事务仍由 monitor_risk 自己做；这里只模拟"执行阶段耗时"，
            # 避免在并发窗口里再跑一次真实风控写事务（那会把 SQLite 锁带进断言）。
            return {"slot": "risk", "date": str(asof_date), "orders": [], "manual_orders": []}

        lane = {}

        def run_lane():
            lane.update(PT.monitor_risk(self.day) or {})

        # 只打一次补丁、只由主线程打：两个线程各自 patch ``datetime.datetime``
        # 会把"恢复顺序"变成竞态，测试结束后全局 datetime 可能留在替身上。
        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 48)),                 mock.patch.object(PT, "_monitor_risk_impl", blocking_impl):
            thread = threading.Thread(target=run_lane, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(30), "risk lane 未能认领扫描")
                with mock.patch.object(PT, "RISK_RUNNING_WAIT_SECONDS", 0.0),                         mock.patch.object(PT, "RISK_RUNNING_POLL_SECONDS", 0.01):
                    fallback = PT.monitor_risk(self.day)
                self.assertEqual("risk_running", fallback["status"])
                self.assertTrue(fallback["deferred"])
                self.assertEqual("running", fallback["risk_scan_state"])
                self.assertEqual("failed", PT._slot_completion_status(fallback))
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive(), "risk lane 线程未结束")
        self.assertNotIn("status", lane, "阻塞解除后 risk lane 应正常完成（无状态即成功）")

    def test_barrier_peer_completion_turns_the_wait_into_already_scanned(self):
        """同一 barrier 下，对端在等待窗口内完成 ⇒ 本入口得到 already_scanned。"""
        import threading

        started = threading.Event()
        release = threading.Event()

        def blocking_impl(asof_date=None, *, cycle_id):
            started.set()
            release.wait(30)
            return {"slot": "risk", "date": str(asof_date), "orders": [], "manual_orders": []}

        def run_lane():
            PT.monitor_risk(self.day)

        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 48)),                 mock.patch.object(PT, "_monitor_risk_impl", blocking_impl):
            thread = threading.Thread(target=run_lane, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(30), "risk lane 未能认领扫描")
                threading.Timer(0.2, release.set).start()
                with mock.patch.object(PT, "RISK_RUNNING_WAIT_SECONDS", 30.0),                         mock.patch.object(PT, "RISK_RUNNING_POLL_SECONDS", 0.02):
                    fallback = PT.monitor_risk(self.day)
                self.assertEqual("already_scanned", fallback["status"])
                self.assertEqual("completed", PT._slot_completion_status(fallback))
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())


    def test_barrier_cycle_rollover_during_the_wait_is_fail_closed(self):
        """等待期间周期翻转：不能拿旧身份的结果当"本轮风控已完成"。"""
        import threading

        started = threading.Event()
        release = threading.Event()

        def blocking_impl(asof_date=None, *, cycle_id):
            started.set()
            release.wait(30)
            return {"slot": "risk", "date": str(asof_date), "orders": [], "manual_orders": []}

        def run_lane():
            PT.monitor_risk(self.day)

        def rollover(conn, *, cycle_id):
            raise PRSS.RiskScanCycleChanged("active cycle 已切换")

        with _fixed_clock(REAL_DATETIME(2026, 9, 10, 14, 48)),                 mock.patch.object(PT, "_monitor_risk_impl", blocking_impl):
            thread = threading.Thread(target=run_lane, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(30), "risk lane 未能认领扫描")
                threading.Timer(0.2, release.set).start()
                with mock.patch.object(PT, "RISK_RUNNING_WAIT_SECONDS", 30.0),                         mock.patch.object(PT, "RISK_RUNNING_POLL_SECONDS", 0.02),                         mock.patch.object(PRSS, "assert_cycle_active", rollover):
                    fallback = PT.monitor_risk(self.day)
                self.assertNotEqual("already_scanned", fallback.get("status"))
                self.assertEqual("risk_unavailable", fallback["status"])
                self.assertTrue(fallback["deferred"])
                self.assertEqual("failed", PT._slot_completion_status(fallback))
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())


class ScheduleStatusRiskLaneTests(PaperRiskExitProductionPathTestCase):
    """独立风险任务的失败必须进入运行状态汇总。"""

    def test_failed_risk_run_surfaces_in_schedule_status(self):
        today = dt.date.today().isoformat()
        with PT._db(immediate=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO paper_job_runs"
                "(run_key,slot,market_date,status,detail,started_at,finished_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (f"risk:{today.replace('-', '')}0933", "risk", today, "failed",
                 "{}", f"{today} 09:33:00", f"{today} 09:33:05"),
            )
        status = PT.schedule_status()
        self.assertTrue(any(row["run_key"].startswith("risk:")
                            for row in status["latest_risk_runs"]))
        self.assertEqual("异常", status["runtime_status"])
        self.assertGreaterEqual(status["runtime_failed_count"], 1)


class OpenSlotRiskGateTests(PaperRiskExitProductionPathTestCase):
    """P0：开盘审批与盘中扫描共用同一条"风控完成"约束。"""

    def test_execute_open_blocks_resume_and_candidates_on_unsettled_risk(self):
        running = {"status": "risk_running", "deferred": True, "risk_scan_state": "running"}
        resume = mock.Mock(return_value=[])
        manual = mock.Mock(return_value=[])
        events = mock.Mock(return_value={"status": "skipped"})
        with mock.patch.object(PT, "init_db"),                 mock.patch.object(PT, "process_pending_execution_orders", resume),                 mock.patch.object(PT, "process_pending_manual_orders", manual),                 mock.patch.object(PT, "monitor_opening_events", events),                 mock.patch.object(PT.MDSvc, "refresh_rows") as refresh:
            result = PT.execute_open(self.day, risk_result=running)
        self.assertEqual("risk_in_progress", result["status"])
        self.assertTrue(result["deferred"])
        self.assertEqual([], result["execution_orders"])
        resume.assert_not_called()
        manual.assert_not_called()
        events.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual("failed", PT._slot_completion_status(result))

    def test_execute_open_blocks_when_its_own_risk_pass_is_unsettled(self):
        resume = mock.Mock(return_value=[])
        with mock.patch.object(PT, "init_db"),                 mock.patch.object(PT, "monitor_risk",
                                  return_value={"status": "risk_running", "deferred": True}),                 mock.patch.object(PT, "process_pending_execution_orders", resume),                 mock.patch.object(PT.MDSvc, "refresh_rows") as refresh:
            result = PT.execute_open(self.day)
        self.assertEqual("risk_in_progress", result["status"])
        resume.assert_not_called()
        refresh.assert_not_called()

    def test_execute_open_still_runs_when_the_risk_pass_settled(self):
        """成功路径不能被新门禁误伤：风险服务不写 status 也算已完成。"""
        resume = mock.Mock(return_value=[])
        with mock.patch.object(PT, "init_db"),                 mock.patch.object(PT, "monitor_risk",
                                  return_value={"orders": [], "manual_orders": []}),                 mock.patch.object(PT, "process_pending_execution_orders", resume),                 mock.patch.object(PT.dfc, "check_data_source_health", return_value={}),                 mock.patch.object(PT, "_entry_freeze_status", return_value={}),                 mock.patch.object(PT.MDSvc, "refresh_rows", return_value=[]):
            result = PT.execute_open(self.day)
        resume.assert_not_called()
        self.assertNotEqual("risk_in_progress", result.get("status"))


class SettleConfigTests(unittest.TestCase):
    """P1：等待配置必须收敛成有限值，nan/inf 不能让截止时间永远不成立。"""

    def test_illegal_values_fall_back_to_the_default(self):
        for raw in ("nan", "inf", "-inf", "-1", "1e9", "", None, "abc"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    20.0, PRSS.bounded_seconds(raw, default=20.0, minimum=0.0, maximum=300.0))

    def test_legal_values_are_kept(self):
        self.assertEqual(0.0, PRSS.bounded_seconds("0", default=20.0, minimum=0.0, maximum=300.0))
        self.assertEqual(2.5, PRSS.bounded_seconds("2.5", default=20.0, minimum=0.0, maximum=300.0))

    def test_await_settled_sanitises_its_own_arguments(self):
        calls = {"n": 0}
        now = {"t": 0.0}

        def read_scan():
            calls["n"] += 1
            return {"status": "running"}

        def clock():
            now["t"] += 1.0
            return now["t"]

        # nan/inf 会被收敛成默认的 20s / 0.25s；用假时钟证明它**有**截止，
        # 不靠真实睡眠（否则这个用例本身就要跑 20 秒）。
        settled = PRSS.await_settled(read_scan, timeout=float("nan"), poll=float("inf"),
                                     clock=clock, sleep=lambda _s: None)
        self.assertEqual("running", settled["state"])
        self.assertLess(calls["n"], 30, "nan/inf 必须被收敛，否则循环不会终止")
        self.assertGreaterEqual(calls["n"], 5, "收敛后仍应按默认值轮询多轮")


if __name__ == "__main__":
    unittest.main()
