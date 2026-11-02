# -*- coding: utf-8 -*-
"""PR-11 调度观测的回归：只验证聚合口径与读取路径，不触碰调度行为。

本文件刻意**不**用自造的扁平事件构造器。``paper_timing.capture`` 的真实输出把
slot 放在 ``timing.slot`` 里，旧的构造器把它放在顶层，恰好掩盖了"读不到 slot"
的缺陷（#324 复审第一条阻断）。因此这里：真实格式用 ``capture`` 现生成，历史格式
单独断言它**必须**被记成 ``unknown`` 而不是被当成正常事件。
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paper_dispatch_metrics as PDM
import paper_timing as timing

REPO_ROOT = Path(__file__).resolve().parents[1]


def _timing_event(slot, started_at, *, status="completed", wait=None, stages=None, risk_status="completed"):
    """生产形状：slot 在 ``timing`` 里，与 ``paper_timing.capture`` 一致。"""
    timing_block = {"started_at": started_at, "stages": stages if stages is not None else {"risk_pass": {"errors": 0}}, "slot": slot,
                    "risk_scans": [{"status": risk_status}] if risk_status else []}
    if wait is not None:
        timing_block["host_paper_lock_wait_seconds"] = wait
    return json.dumps({"event": "paper_slot_timing", "status": status,
                       "timing": timing_block}, ensure_ascii=False)


def _start(slot, *, tick="t1", wait=0):
    return json.dumps({"event": "paper_slot_start", "slot": slot, "tick_id": tick,
                       "wait_seconds": wait, "at": "2026-11-02T09:33:00+08:00"})


def _exit(slot, *, tick="t1", code=0):
    return json.dumps({"event": "paper_slot_exit", "slot": slot, "tick_id": tick,
                       "exit_code": code, "at": "2026-11-02T09:33:12+08:00"})


def _lock(status, *, slot=None, wait=None):
    payload = {"event": "paper_host_lock", "status": status}
    if slot is not None:
        payload["slot"] = slot
    if wait is not None:
        payload["wait_seconds"] = wait
    return json.dumps(payload)


def _summarize(*lines, **kwargs):
    return PDM.summarize(list(PDM.iter_events(lines)), **kwargs)


class RealEventShapeTests(unittest.TestCase):
    """真实 ``paper_timing.capture`` 输出必须是唯一被采信的格式。"""

    def test_capture_output_carries_slot_only_inside_timing(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), timing.capture("intraday"):
            with timing.phase("risk_pass"):
                pass
            timing.risk_scan_outcome("completed")
            timing.outcome("completed")
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(1, len(lines))
        event = json.loads(lines[0])
        self.assertEqual("paper_slot_timing", event["event"])
        self.assertNotIn("slot", event, "顶层没有 slot：从顶层读会把任务记成 unknown")
        self.assertEqual("intraday", event["timing"]["slot"])

    def test_capture_output_is_aggregated_under_its_real_slot(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), timing.capture("intraday"):
            with timing.phase("risk_pass"):
                pass
            timing.risk_scan_outcome("completed")
            timing.outcome("completed")
        summary = PDM.summarize_lines(buffer.getvalue().splitlines())
        self.assertIn("intraday", summary["runs"])
        self.assertNotIn(PDM.UNKNOWN_SLOT, summary["runs"])
        self.assertEqual(1, summary["runs"]["intraday"]["end_events"])
        self.assertEqual(1, summary["runs"]["intraday"]["completed"])
        self.assertEqual("intraday_risk_phase", summary["last_successful_risk_scan"]["source"])
        self.assertEqual(0, summary["ticks"]["unparsed"])

    def test_review_sample_event_is_read_from_timing_slot(self):
        """#324 复审给出的生产样本，逐字照抄。"""
        sample = json.dumps({
            "event": "paper_slot_timing",
            "status": "completed",
            "timing": {
                "slot": "intraday",
                "started_at": "2026-11-02T09:33:00+08:00",
                "stages": {"risk_pass": {"calls": 1, "errors": 0}},
            },
        })
        summary = _summarize(sample)
        self.assertEqual(["intraday"], list(summary["runs"]))
        self.assertIsNone(summary["last_successful_risk_scan"]["at"],
                          "旧日志只有阶段耗时，不能证明实际扫描")

    def test_flat_top_level_slot_is_not_accepted_as_evidence(self):
        """历史/自造格式（slot 在顶层）不得被当成正常事件。"""
        summary = _summarize(
            '{"event": "paper_slot_timing", "status": "completed", "slot": "risk",'
            ' "timing": {"started_at": "2026-11-02T09:33:00+08:00"}}')
        self.assertIn(PDM.UNKNOWN_SLOT, summary["runs"])
        self.assertNotIn("risk", summary["runs"])
        self.assertEqual(1, summary["ticks"]["unparsed"])
        self.assertIsNone(summary["last_successful_risk_scan"]["at"])


class ParsingRobustnessTests(unittest.TestCase):
    def test_stage_errors_accept_only_nonnegative_finite_integers(self):
        for raw in (float("inf"), -float("inf"), float("nan"), -1, -0.5, 0.5,
                    True, False, None, "", "inf", "nan", "0.5", [], {}):
            with self.subTest(raw=raw):
                self.assertIsNone(PDM._stage_errors({"errors": raw}))
                event = json.loads(_timing_event(
                    "intraday", "2026-11-02T09:33:00+08:00",
                    stages={"risk_pass": {"errors": raw}}))
                summary = PDM.summarize([event])
                self.assertEqual(1, summary["ticks"]["end_events"])
                self.assertIsNone(summary["last_successful_risk_scan"]["at"])
                json.dumps(summary, allow_nan=False)
        self.assertIsNone(PDM._stage_errors({}))
        for raw in (0, 0.0, "0", 1, "2"):
            self.assertEqual(int(raw), PDM._stage_errors({"errors": raw}))

    def test_bad_lines_are_skipped_without_raising(self):
        lines = [
            "",
            "   ",
            "cron started /opt/astock-codex/deploy/run_paper_slot.sh",
            "{not json}",
            "[1,2,3]",
            '{"event": 42}',
            '{"event": "paper_slot_timing", "status": "completed", "timing": "oops"}',
            '{"event": "paper_slot_timing", "status": "completed", "timing": {"slot": 7}}',
            '{"event": "paper_host_lock", "status": "not_acquired", "wait_seconds": "x"}',
        ]
        summary = PDM.summarize(list(PDM.iter_events(lines)))
        self.assertEqual(2, summary["ticks"]["end_events"])
        self.assertEqual(2, summary["ticks"]["unparsed"])
        self.assertEqual(0, summary["queue_age_seconds"]["samples"])

    def test_malformed_timing_fields_degrade_without_raising(self):
        events = [
            '{"event": "paper_slot_timing", "status": "completed", "timing":'
            ' {"slot": "intraday", "started_at": "not-a-time",'
            '  "stages": {"risk_pass": {"calls": 1, "errors": "many"}}}}',
            '{"event": "paper_slot_timing", "status": "completed", "timing":'
            ' {"slot": "intraday", "started_at": "2026-11-02T09:36:00+08:00",'
            '  "stages": "broken"}}',
            '{"event": "paper_slot_start", "slot": "intraday", "wait_seconds": -3}',
        ]
        summary = _summarize(*events)
        # errors 不是数字时按"未知"处理：不能冒充零错误而记为成功风控。
        self.assertIsNone(summary["last_successful_risk_scan"]["at"])
        self.assertEqual(2, summary["runs"]["intraday"]["end_events"])
        self.assertEqual(0, summary["queue_age_seconds"]["samples"])
        self.assertEqual(0, summary["start_to_start_seconds"].get("intraday", {}).get("samples", 0))

    def test_mixed_timezone_offsets_drop_the_pair_instead_of_raising(self):
        """naive 与 aware 的 ``started_at`` 相减会抛 TypeError：只能丢掉这一对。"""
        summary = _summarize(
            _timing_event("risk", "2026-11-02T09:30:00+08:00"),
            _timing_event("risk", "2026-11-02T09:33:00"),
            _timing_event("risk", "2026-11-02T09:36:00+08:00"),
        )
        self.assertEqual({}, summary["start_to_start_seconds"])
        self.assertEqual(3, summary["ticks"]["end_events"])

    def test_negative_wait_is_not_counted(self):
        self.assertEqual(0, _summarize(_lock(PDM.NOT_ACQUIRED, slot="intraday", wait=-5))
                         ["queue_age_seconds"]["samples"])

    def test_non_finite_wait_never_reaches_the_dashboard_payload(self):
        """``1e999`` 会解析成 inf；输出必须是标准 JSON，否则前端整块解析失败。"""
        summary = _summarize(
            _lock(PDM.NOT_ACQUIRED, slot="intraday", wait=1e999),
            _start("intraday", tick="a", wait=1e999),
        )
        self.assertEqual(0, summary["queue_age_seconds"]["samples"])
        self.assertEqual(summary, json.loads(json.dumps(summary, allow_nan=False)))


class TickAccountingTests(unittest.TestCase):
    def test_missing_container_is_only_a_start_attempt(self):
        summary = _summarize(_start("risk"), _exit("risk", code=1))
        self.assertEqual(1, summary["ticks"]["start_attempts"])
        self.assertEqual("host_start_attempt_events", summary["ticks"]["source"])
        self.assertNotIn("started", summary["ticks"])
        self.assertEqual(0, summary["ticks"]["end_events"])
        self.assertIsNone(summary["last_successful_risk_scan"]["at"])

    def test_start_and_end_events_pair_by_tick_id(self):
        summary = _summarize(
            _start("intraday", tick="a", wait=4),
            _timing_event("intraday", "2026-11-02T09:33:00+08:00"),
            _exit("intraday", tick="a"),
        )
        ticks = summary["ticks"]
        self.assertEqual(1, ticks["start_attempts"])
        self.assertEqual(1, ticks["end_events"])
        self.assertEqual(1, ticks["completed"])
        self.assertEqual(0, ticks["unfinished"])
        self.assertEqual(0, ticks["exited_without_result"])
        self.assertEqual(PDM.SOURCE_START_EVENTS, ticks["source"])
        self.assertEqual(1, summary["runs"]["intraday"]["start_attempts"])

    def test_started_without_end_event_is_unfinished_not_completed(self):
        summary = _summarize(_start("intraday", tick="a"))
        self.assertEqual(1, summary["ticks"]["start_attempts"])
        self.assertEqual(0, summary["ticks"]["end_events"])
        self.assertEqual(1, summary["ticks"]["unfinished"])
        self.assertEqual(0, summary["ticks"]["completed"])

    def test_started_then_exited_without_result_is_its_own_category(self):
        summary = _summarize(_start("risk", tick="a"), _exit("risk", tick="a", code=137))
        self.assertEqual(1, summary["ticks"]["unfinished"] + summary["ticks"]["exited_without_result"])
        self.assertEqual(1, summary["ticks"]["exited_without_result"])
        self.assertEqual(0, summary["ticks"]["unfinished"])
        self.assertEqual(0, summary["ticks"]["completed"])

    def test_legacy_log_without_start_events_says_so(self):
        summary = _summarize(_timing_event("intraday", "2026-11-02T09:33:00+08:00"))
        self.assertEqual(PDM.SOURCE_END_EVENTS, summary["ticks"]["source"])
        self.assertEqual(0, summary["ticks"]["start_attempts"])
        self.assertEqual(1, summary["ticks"]["end_events"])

    def test_real_capture_tick_id_pairs_with_the_host_start_event(self):
        """主机注入 ``ASTOCK_SCHEDULER_TICK_ID`` 后，真实结束事件能与启动事件配对。"""
        with mock.patch.dict(os.environ, {"ASTOCK_SCHEDULER_TICK_ID": "risk-1793604230-1149"}):
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer), timing.capture("risk"):
                with timing.phase("risk_pass"):
                    pass
                timing.risk_scan_outcome("completed")
                timing.outcome("completed")
        lines = [_start("risk", tick="risk-1793604230-1149")] + buffer.getvalue().splitlines()
        summary = PDM.summarize_lines(lines)
        self.assertEqual(1, summary["ticks"]["start_attempts"])
        self.assertEqual(0, summary["ticks"]["unfinished"])
        self.assertEqual(0, summary["ticks"]["exited_without_result"])
        self.assertEqual(1, summary["ticks"]["completed"])
        self.assertEqual("risk_slot", summary["last_successful_risk_scan"]["source"])

    def test_skips_are_attributed_to_their_slot(self):
        summary = _summarize(
            _lock(PDM.NOT_ACQUIRED, slot="intraday", wait=61),
            _lock(PDM.NOT_ACQUIRED, slot="intraday", wait=61),
            _lock(PDM.YIELDED, slot="fast-entry"),
            _start("intraday", tick="a", wait=0),
        )
        self.assertEqual(2, summary["skips"][PDM.NOT_ACQUIRED])
        self.assertEqual(1, summary["skips"][PDM.YIELDED])
        self.assertEqual(3, summary["ticks"]["skipped"])
        self.assertEqual(2, summary["skips_by_slot"]["intraday"][PDM.NOT_ACQUIRED])
        self.assertEqual(1, summary["skips_by_slot"]["fast-entry"][PDM.YIELDED])
        self.assertEqual(2, summary["runs"]["intraday"]["skipped"])
        self.assertEqual(1, summary["runs"]["fast-entry"]["skipped"])

    def test_queue_age_counts_each_attempt_once(self):
        summary = _summarize(
            _lock(PDM.NOT_ACQUIRED, slot="intraday", wait=60),
            _start("intraday", tick="a", wait=5),
            _timing_event("intraday", "2026-11-02T09:33:00+08:00", wait=5),
        )
        queue = summary["queue_age_seconds"]
        self.assertEqual(2, queue["samples"], "同一轮的主机锁等待不能计两次")
        self.assertEqual(60.0, queue["max"])

    def test_queue_age_falls_back_to_end_events_for_legacy_logs(self):
        queue = _summarize(
            _timing_event("intraday", "2026-11-02T09:33:00+08:00", wait=0),
            _timing_event("intraday", "2026-11-02T09:36:00+08:00", wait=12),
        )["queue_age_seconds"]
        self.assertEqual(2, queue["samples"])
        self.assertEqual(12.0, queue["max"])

    def test_queue_age_mixes_paired_starts_and_orphan_end_events(self):
        """有启动事件的轮次用进入等待，没有启动事件的轮次（旧日志/直连 docker exec）
        用结束事件里的等待：同一个窗口两种轮次都要能计，且不重复。"""
        queue = _summarize(
            _start("intraday", tick="a", wait=5),
            _timing_event("intraday", "2026-11-02T09:33:00+08:00", wait=5),
            '{"event": "paper_slot_timing", "status": "completed", "timing":'
            ' {"slot": "weekly-review", "started_at": "2026-11-02T15:20:00+08:00",'
            '  "host_paper_lock_wait_seconds": 9}}',
        )["queue_age_seconds"]
        self.assertEqual(2, queue["samples"])
        self.assertEqual(9.0, queue["max"])


class StatusSemanticsTests(unittest.TestCase):
    def test_completed_slot_without_actual_scan_cannot_advance_risk_success(self):
        for slot in ("risk", "intraday"):
            for marker in (None, "already_scanned", "failed"):
                with self.subTest(slot=slot, marker=marker):
                    last = _summarize(
                        _timing_event(slot, "2026-11-02T09:33:00+08:00"),
                        _timing_event(slot, "2026-11-02T09:36:00+08:00", risk_status=marker),
                    )["last_successful_risk_scan"]
                    self.assertEqual("2026-11-02T09:33:00+08:00", last["at"])

    def test_end_event_statuses_are_split_into_four_buckets(self):
        summary = _summarize(
            _timing_event("intraday", "2026-11-02T09:30:00+08:00", status="completed"),
            _timing_event("intraday", "2026-11-02T09:33:00+08:00", status="skipped"),
            _timing_event("intraday", "2026-11-02T09:36:00+08:00", status="already_done"),
            _timing_event("intraday", "2026-11-02T09:39:00+08:00", status="failed"),
            _timing_event("intraday", "2026-11-02T09:42:00+08:00", status="weird"),
        )
        ticks = summary["ticks"]
        self.assertEqual(1, ticks["completed"])
        self.assertEqual(2, ticks["idle"])
        self.assertEqual(1, ticks["failed"])
        self.assertEqual(1, ticks["unknown_status"])
        runs = summary["runs"]["intraday"]
        self.assertEqual(
            {"start_attempts": 0, "end_events": 5, "skipped": 0,
             "completed": 1, "idle": 2, "failed": 1, "unknown": 1},
            runs,
        )

    def test_skipped_risk_slot_is_not_a_successful_risk_scan(self):
        for status in ("skipped", "already_done", "up_to_date", "failed", "blocked"):
            with self.subTest(status=status):
                summary = _summarize(
                    _timing_event("risk", "2026-11-02T09:33:00+08:00", status=status))
                self.assertIsNone(summary["last_successful_risk_scan"]["at"])

    def test_completed_risk_slot_wins_and_reports_status(self):
        last = _summarize(_timing_event("risk", "2026-11-02T09:33:00+08:00"))["last_successful_risk_scan"]
        self.assertEqual("risk_slot", last["source"])
        self.assertEqual("completed", last["status"])
        self.assertEqual("risk", last["slot"])
        self.assertTrue(last["at"].startswith("2026-11-02T09:33"))

    def test_intraday_risk_phase_requires_completed_with_zero_errors(self):
        stages = {"risk.alt_evidence": {"seconds": 1.0, "errors": 0}}
        last = _summarize(_timing_event("intraday", "2026-11-02T09:36:00+08:00", stages=stages))
        self.assertEqual("intraday_risk_phase", last["last_successful_risk_scan"]["source"])

        idle = _summarize(_timing_event("intraday", "2026-11-02T09:36:00+08:00",
                                        status="skipped", stages=stages))
        self.assertIsNone(idle["last_successful_risk_scan"]["at"])

        failed = _summarize(_timing_event("intraday", "2026-11-02T09:36:00+08:00",
                                          status="failed", stages=stages))
        self.assertIsNone(failed["last_successful_risk_scan"]["at"])

        errors = _summarize(_timing_event(
            "intraday", "2026-11-02T09:36:00+08:00",
            stages={"risk.alt_evidence": {"seconds": 1.0, "errors": 1}}))
        self.assertIsNone(errors["last_successful_risk_scan"]["at"])

    def test_intraday_without_risk_stage_is_not_a_risk_scan(self):
        last = _summarize(_timing_event(
            "intraday", "2026-11-02T09:36:00+08:00",
            stages={"candidate.strategy": {"seconds": 1.0, "errors": 0}}))
        self.assertIsNone(last["last_successful_risk_scan"]["at"])

    def test_display_snapshot_enqueue_is_not_risk_evidence(self):
        """``risk_snapshot_enqueue`` 是展示入队（异常被吞），不能当风控证据。"""
        only_enqueue = _summarize(_timing_event(
            "intraday", "2026-11-02T09:36:00+08:00",
            stages={"risk_snapshot_enqueue": {"seconds": 0.1, "errors": 0}}))
        self.assertIsNone(only_enqueue["last_successful_risk_scan"]["at"])

        enqueue_failed = _summarize(_timing_event(
            "intraday", "2026-11-02T09:36:00+08:00",
            stages={"risk_pass": {"calls": 1, "errors": 0},
                    "risk_snapshot_enqueue": {"seconds": 0.1, "errors": 1}}))
        self.assertEqual("intraday_risk_phase",
                         enqueue_failed["last_successful_risk_scan"]["source"])

        risk_failed = _summarize(_timing_event(
            "intraday", "2026-11-02T09:36:00+08:00",
            stages={"risk_pass": {"calls": 1, "errors": 1},
                    "risk_snapshot_enqueue": {"seconds": 0.1, "errors": 0}}))
        self.assertIsNone(risk_failed["last_successful_risk_scan"]["at"])

    def test_later_failed_risk_event_keeps_the_last_real_success(self):
        last = _summarize(
            _timing_event("risk", "2026-11-02T09:33:00+08:00"),
            _timing_event("risk", "2026-11-02T09:36:00+08:00", status="failed"),
        )["last_successful_risk_scan"]
        self.assertEqual("risk_slot", last["source"])
        self.assertTrue(last["at"].startswith("2026-11-02T09:33"))


class IntervalTests(unittest.TestCase):
    def test_start_to_start_uses_started_at_and_counts_breaches(self):
        risk = _summarize(
            _timing_event("risk", "2026-11-02T09:30:00+08:00"),
            _timing_event("risk", "2026-11-02T09:33:00+08:00"),
            _timing_event("risk", "2026-11-02T09:40:00+08:00"),
        )["start_to_start_seconds"]["risk"]
        self.assertEqual(2, risk["samples"])
        self.assertEqual(180.0, risk["p50_seconds"])
        self.assertEqual(420.0, risk["max_seconds"])
        self.assertEqual(1, risk["breaches"])
        self.assertEqual(180.0, risk["target_seconds"])

    def test_intervals_are_tracked_per_slot(self):
        summary = _summarize(
            _timing_event("risk", "2026-11-02T09:30:00+08:00"),
            _timing_event("intraday", "2026-11-02T09:31:00+08:00"),
            _timing_event("risk", "2026-11-02T09:33:00+08:00"),
        )["start_to_start_seconds"]
        self.assertEqual(1, summary["risk"]["samples"])
        self.assertNotIn("intraday", summary)

    def test_custom_target_changes_breach_count(self):
        summary = _summarize(
            _timing_event("risk", "2026-11-02T09:30:00+08:00"),
            _timing_event("risk", "2026-11-02T09:34:00+08:00"),
            target_interval_seconds=300,
        )
        self.assertEqual(0, summary["start_to_start_seconds"]["risk"]["breaches"])


class PathResolutionTests(unittest.TestCase):
    def test_default_path_follows_the_report_dir(self):
        path = PDM.scheduler_log_path("/app/reports", {})
        self.assertEqual(os.path.join("/app/reports", "scheduler.log"), path)

    def test_override_wins_and_blank_override_is_ignored(self):
        self.assertEqual("/tmp/custom.log",
                         PDM.scheduler_log_path("/app/reports", {"ASTOCK_SCHEDULER_LOG": " /tmp/custom.log "}))
        self.assertEqual(os.path.join("/app/reports", "scheduler.log"),
                         PDM.scheduler_log_path("/app/reports", {"ASTOCK_SCHEDULER_LOG": "   "}))

    def test_default_path_is_repo_relative_so_the_api_container_can_read_it(self):
        """宿主写 ``reports/``，容器把它挂在 ``/app/reports``：默认值必须同源。"""
        with mock.patch.dict(os.environ, {"ASTOCK_DATA_DIR": "", "ASTOCK_REPORT_DIR": "",
                                          "ASTOCK_SCHEDULER_LOG": ""}):
            import data_paths
            path = PDM.scheduler_log_path(data_paths.report_dir())
        self.assertEqual(os.path.join("reports", "scheduler.log"),
                         os.path.relpath(path, REPO_ROOT))

    def test_api_image_mounts_the_same_report_dir(self):
        compose_path = REPO_ROOT / "docker-compose.server.yml"
        dockerfile_path = REPO_ROOT / "Dockerfile"
        if not (compose_path.exists() and dockerfile_path.exists()):
            # docker-smoke job 在镜像内跑回归（cwd=/app），仓库根文件不在那里；
            # 布局断言只在源码树里有意义。
            self.skipTest("no Dockerfile / compose in this checkout root")
        compose = compose_path.read_text(encoding="utf-8")
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        self.assertIn("./reports:/app/reports", compose)
        self.assertIn("WORKDIR /app", dockerfile)


class FileTests(unittest.TestCase):
    def test_nonfinite_stage_errors_do_not_break_file_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scheduler.log"
            good = _timing_event("intraday", "2026-11-02T09:33:00+08:00")
            for raw in ("1e999", "-1e999", "NaN", "-1", "0.5", "null", "true"):
                with self.subTest(raw=raw):
                    bad = _timing_event("intraday", "2026-11-02T09:36:00+08:00")
                    bad = bad.replace('"errors": 0', '"errors": ' + raw)
                    path.write_text(good + "\n" + bad + "\n", encoding="utf-8")
                    result = PDM.summarize_file(path)
                    self.assertTrue(result["available"])
                    self.assertEqual(2, result["ticks"]["end_events"])
                    self.assertEqual("2026-11-02T09:33:00+08:00",
                                     result["last_successful_risk_scan"]["at"])
                    json.dumps(result, allow_nan=False)

    def test_missing_file_reports_the_reason_instead_of_raising(self):
        result = PDM.summarize_file(Path(tempfile.gettempdir()) / "no-such-scheduler.log")
        self.assertFalse(result["available"])
        self.assertEqual("missing", result["error"])

    def test_unreadable_file_reports_the_reason_instead_of_raising(self):
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            result = PDM.summarize_file("/app/reports/scheduler.log")
        self.assertFalse(result["available"])
        self.assertEqual("unreadable", result["error"])

    def test_unexpected_aggregation_failure_degrades_the_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scheduler.log"
            path.write_text(_timing_event("risk", "2026-11-02T09:33:00+08:00") + "\n",
                            encoding="utf-8")
            with mock.patch.object(PDM, "summarize_lines", side_effect=RuntimeError("boom")):
                result = PDM.summarize_file(path)
        self.assertFalse(result["available"])
        self.assertEqual("parse_error", result["error"])

    def test_file_summary_keeps_the_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scheduler.log"
            path.write_text("\n".join([
                _timing_event("risk", "2026-11-02T09:30:00+08:00"),
                _timing_event("risk", "2026-11-02T09:33:00+08:00"),
            ]) + "\n", encoding="utf-8")
            result = PDM.summarize_file(path)
        self.assertTrue(result["available"])
        self.assertEqual(2, result["lines_scanned"])
        self.assertEqual(1, result["start_to_start_seconds"]["risk"]["samples"])


class DispatchStatusTests(unittest.TestCase):
    """``paper_trading.dispatch_status`` 必须能读到容器内的报告目录副本。"""

    def _trading(self):
        import paper_trading as paper
        return paper

    def test_reads_the_report_dir_copy_and_reports_missing(self):
        paper = self._trading()
        with tempfile.TemporaryDirectory() as folder:
            reports = Path(folder) / "reports"
            reports.mkdir()
            log = reports / "scheduler.log"
            log.write_text(_timing_event("risk", "2026-11-02T09:33:00+08:00") + "\n",
                           encoding="utf-8")
            env = {"ASTOCK_DATA_DIR": folder, "ASTOCK_REPORT_DIR": "", "ASTOCK_SCHEDULER_LOG": ""}
            with mock.patch.dict(os.environ, env):
                available = paper.dispatch_status()
                self.assertTrue(available["available"], available)
                self.assertEqual(1, available["ticks"]["end_events"])
                log.unlink()
                missing = paper.dispatch_status()
        self.assertFalse(missing["available"])
        self.assertEqual("missing", missing["error"])

    def test_env_override_points_at_another_file(self):
        paper = self._trading()
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "elsewhere.log"
            log.write_text(_timing_event("risk", "2026-11-02T09:33:00+08:00") + "\n",
                           encoding="utf-8")
            with mock.patch.dict(os.environ, {"ASTOCK_SCHEDULER_LOG": str(log)}):
                result = paper.dispatch_status()
        self.assertTrue(result["available"], result)
        self.assertEqual(str(log), result["path"])

    def test_session_windows_come_from_the_schedule_contract(self):
        """dispatch_status 必须把交易窗口交给聚合器，午休不算节拍违规。"""
        paper = self._trading()
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "scheduler.log"
            log.write_text("\n".join([
                _timing_event("risk", "2026-11-02T11:24:00+08:00"),
                _timing_event("risk", "2026-11-02T13:00:00+08:00"),
            ]) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ASTOCK_SCHEDULER_LOG": str(log)}):
                result = paper.dispatch_status()
        risk = result["start_to_start_seconds"]["risk"]
        self.assertEqual(0, risk["samples"])
        self.assertEqual(1, risk["excluded_session_gaps"])
        self.assertEqual(0, risk["breaches"])


class RiskScanLaneTests(unittest.TestCase):
    """PR-11 B：完成标记要能区分"风险 lane 跑的"和"intraday 兜底跑的"。"""

    def test_risk_scan_markers_are_split_by_status_and_lane(self):
        summary = _summarize(
            _timing_event("risk", "2026-11-02T09:33:00+08:00", risk_status="completed"),
            _timing_event("intraday", "2026-11-02T09:33:00+08:00", risk_status="already_scanned"),
            _timing_event("intraday", "2026-11-02T09:36:00+08:00", risk_status="completed"),
            _timing_event("risk", "2026-11-02T09:39:00+08:00", risk_status="failed"),
        )
        scans = summary["risk_scans"]
        self.assertEqual(4, scans["total"])
        self.assertEqual({"already_scanned": 1, "completed": 2, "failed": 1}, scans["by_status"])
        self.assertEqual({"completed": 1, "failed": 1}, scans["by_slot"]["risk"])
        self.assertEqual({"already_scanned": 1, "completed": 1}, scans["by_slot"]["intraday"])

    def test_already_scanned_is_idle_and_never_a_successful_risk_scan(self):
        summary = _summarize(
            _timing_event("risk", "2026-11-02T09:33:00+08:00", status="already_scanned",
                          risk_status="already_scanned"))
        self.assertEqual(1, summary["ticks"]["idle"])
        self.assertEqual(0, summary["ticks"]["completed"])
        self.assertIsNone(summary["last_successful_risk_scan"]["at"])
        self.assertEqual({"already_scanned": 1}, summary["risk_scans"]["by_status"])

    def test_risk_scan_without_stage_evidence_is_not_a_success(self):
        """只有完成标记、没有风险阶段耗时：证据不完整，不冒充成功风控。"""
        summary = _summarize(
            _timing_event("risk", "2026-11-02T09:33:00+08:00", stages={},
                          risk_status="completed"))
        self.assertIsNone(summary["last_successful_risk_scan"]["at"])

    def test_malformed_scan_markers_do_not_raise(self):
        summary = _summarize(
            '{"event": "paper_slot_timing", "status": "completed", "timing":'
            ' {"slot": "risk", "started_at": "2026-11-02T09:33:00+08:00",'
            '  "risk_scans": "broken", "stages": {"risk_pass": {"errors": 0}}}}',
            '{"event": "paper_slot_timing", "status": "completed", "timing":'
            ' {"slot": "risk", "started_at": "2026-11-02T09:36:00+08:00",'
            '  "risk_scans": [42, {"status": ""}], "stages": {"risk_pass": {"errors": 0}}}}',
        )
        self.assertEqual(0, summary["risk_scans"]["total"])


class SessionWindowTests(unittest.TestCase):
    WINDOWS = (("09:30", "11:24"), ("13:00", "14:57"))

    def test_lunch_gap_is_excluded_from_the_cadence(self):
        risk = _summarize(
            _timing_event("risk", "2026-11-02T11:24:00+08:00"),
            _timing_event("risk", "2026-11-02T13:00:00+08:00"),
            session_windows=self.WINDOWS,
        )["start_to_start_seconds"]["risk"]
        self.assertEqual(0, risk["samples"])
        self.assertEqual(1, risk["excluded_session_gaps"])
        self.assertEqual(0, risk["breaches"])

    def test_in_session_gaps_still_count_breaches(self):
        risk = _summarize(
            _timing_event("risk", "2026-11-02T09:33:00+08:00"),
            _timing_event("risk", "2026-11-02T09:40:00+08:00"),
            session_windows=self.WINDOWS,
        )["start_to_start_seconds"]["risk"]
        self.assertEqual(1, risk["samples"])
        self.assertEqual(1, risk["breaches"])
        self.assertEqual(0, risk["excluded_session_gaps"])

    def test_overnight_gap_is_excluded_even_though_the_clocks_match(self):
        """T 日 14:55 → T+1 09:30 落在同一 HH:MM 窗口，但不是同一交易日。

        只比较时刻会把跨夜间隔算进节拍样本：次日第一跳的 breach 凭空出现，
        节拍验收数字被每天的收尾/开局污染。
        """
        risk = _summarize(
            _timing_event("risk", "2026-11-02T14:55:00+08:00"),
            _timing_event("risk", "2026-11-03T09:30:00+08:00"),
            session_windows=self.WINDOWS,
        )["start_to_start_seconds"]["risk"]
        self.assertEqual(0, risk["samples"])
        self.assertEqual(1, risk["excluded_session_gaps"])
        self.assertEqual(0, risk["breaches"])

    def test_same_day_same_window_gap_still_counts(self):
        risk = _summarize(
            _timing_event("risk", "2026-11-02T14:48:00+08:00"),
            _timing_event("risk", "2026-11-02T14:54:00+08:00"),
            session_windows=self.WINDOWS,
        )["start_to_start_seconds"]["risk"]
        self.assertEqual(1, risk["samples"])
        self.assertEqual(0, risk["excluded_session_gaps"])

    def test_without_windows_every_gap_counts(self):
        risk = _summarize(
            _timing_event("risk", "2026-11-02T11:24:00+08:00"),
            _timing_event("risk", "2026-11-02T13:00:00+08:00"),
        )["start_to_start_seconds"]["risk"]
        self.assertEqual(1, risk["samples"])
        self.assertEqual(1, risk["breaches"])
        self.assertEqual(0, risk["excluded_session_gaps"])


class RunnerExitCodeTests(unittest.TestCase):
    def test_already_scanned_is_a_successful_idempotent_skip(self):
        import paper_runner as runner
        self.assertEqual(0, runner._result_exit_code({"status": "already_scanned"}))
        self.assertEqual(1, runner._result_exit_code({"status": "blocked"}))
        self.assertEqual(1, runner._result_exit_code({"status": "failed"}))


if __name__ == "__main__":
    unittest.main()
