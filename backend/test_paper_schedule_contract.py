# -*- coding: utf-8 -*-
"""Scheduler contract guards: one vocabulary across backend, runner and deploy.

The slot vocabulary and the schedule table used to live in four unlinked places
(``paper_slot_service.SUPPORTED_SLOTS``, ``paper_runner``'s argparse choices,
``paper_trading.install_windows_schedule``'s definitions, and
``deploy/astock-codex.cron``).  Nothing stopped a slot from being added to one
and forgotten in the others, and the only record of the resulting platform gap
was a comment in the cron file.

``backend/paper_schedule_contract.py`` is now the single source of truth.  These
tests pin the three properties that make it trustworthy:

1. **Vocabulary consistency** — every consumer derives its list from the
   contract, so they cannot disagree.
2. **Rendering fidelity** — the cron fields the contract renders are *semantically
   identical* to the hand-written ``deploy/astock-codex.cron``.  Equality is
   checked by expanding both into concrete ``(hour, minute)`` trigger sets, not
   by string comparison: ``33-59/3`` and an equivalent enumerated list are the
   same schedule.
3. **Intent/rendering agreement** — every rendered intraday tick falls inside a
   declared window, respects the declared cadence, and no window hour is left
   unscanned.  This is what catches a minute-field edit that no longer matches
   the windows it claims to cover.

The intraday minute fields are recorded verbatim rather than derived; see the
comment on ``INTRADAY_CRON_MINUTES`` for why deriving them would be a guess.
Guard 3 is therefore the substitute for a derivation rule.
"""
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
CRON_FILE = ROOT / "deploy" / "astock-codex.cron"

import paper_schedule_contract as PSch  # noqa: E402


def _parse_field(field: str, low: int, high: int) -> set[int]:
    """Expand one cron field into the set of values it matches."""
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, raw_step = part.split("/")
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start, end = (int(bound) for bound in part.split("-"))
        else:
            start = end = int(part)
        values.update(value for value in range(start, end + 1) if (value - start) % step == 0)
    return values


def _triggers(minute_field: str, hour_field: str, dow: str) -> set[tuple[int, int]]:
    """Concrete ``(hour, minute)`` firing times for one cron line."""
    hours = _parse_field(hour_field, 0, 23)
    minutes = _parse_field(minute_field, 0, 59)
    assert dow, "day-of-week is part of the identity being compared"
    return {(hour, minute) for hour in hours for minute in minutes}


def _collect(lines: list[str], pattern: str) -> dict[tuple[str, str], set[tuple[int, int]]]:
    """Group cron lines by ``(command argument, day-of-week)``."""
    collected: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for line in lines:
        match = re.search(pattern, line)
        if not match:
            continue
        fields = line.split()
        key = (match.group(1), fields[4])
        collected.setdefault(key, set()).update(_triggers(fields[0], fields[1], fields[4]))
    return collected


class SlotVocabularyGuard(unittest.TestCase):
    """Every consumer derives its list from the contract."""

    def test_run_slots_and_runner_only_slots_are_disjoint(self):
        self.assertEqual(
            set(PSch.RUN_SLOTS) & set(PSch.RUNNER_ONLY_SLOTS),
            set(),
            "a slot cannot be both a run_slot target and a runner-only branch",
        )

    def test_runner_slots_is_the_union(self):
        self.assertEqual(
            set(PSch.RUNNER_SLOTS),
            set(PSch.RUN_SLOTS) | set(PSch.RUNNER_ONLY_SLOTS),
        )

    def test_slot_service_delegates_to_the_contract(self):
        import paper_slot_service as PSS

        self.assertEqual(set(PSS.SUPPORTED_SLOTS), set(PSch.RUN_SLOTS))
        for slot in PSch.RUN_SLOTS:
            self.assertEqual(PSS.validate_slot(slot), slot)

    def test_validate_slot_rejects_runner_only_slots(self):
        for slot in PSch.RUNNER_ONLY_SLOTS:
            with self.assertRaises(ValueError) as caught:
                PSch.validate_slot(slot)
            self.assertEqual(str(caught.exception), PSch.SLOT_ERROR)

    def test_paper_trading_hot_path_matches_the_contract(self):
        import paper_trading as P

        self.assertEqual(set(P.HOT_PATH_SLOTS), set(PSch.HOT_PATH_SLOTS))
        for slot in PSch.RUNNER_SLOTS:
            self.assertEqual(
                P._is_hot_path_slot(slot),
                PSch.is_hot_path(slot),
                f"hot-path classification drifted for {slot}",
            )

    def test_runner_uses_the_contract_choices_and_hot_path(self):
        source = (BACKEND / "paper_runner.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        choices_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and any(kw.arg == "choices" for kw in node.keywords)
        ]
        self.assertEqual(len(choices_calls), 1, "runner must declare exactly one slot choice list")
        choices_node = next(kw for kw in choices_calls[0].keywords if kw.arg == "choices")
        self.assertIsInstance(choices_node.value, ast.Call)
        self.assertEqual(choices_node.value.func.id, "list")
        self.assertEqual(choices_node.value.args[0].id, "RUNNER_SLOTS")
        self.assertIn("is_hot_path(", source, "runner must not re-declare the hot-path set")

    def test_schedule_status_probes_the_contract_task_names(self):
        source = (BACKEND / "paper_trading.py").read_text(encoding="utf-8")
        self.assertIn(
            "names = list(PSch.SCHEDULE_TASK_NAMES)",
            source,
            "schedule_status must probe the contract's task names, not a private list",
        )

    def test_windows_install_uses_the_contract_table(self):
        source = (BACKEND / "paper_trading.py").read_text(encoding="utf-8")
        self.assertIn("PSch.SCHEDULE_TASKS", source)
        for literal in ('"A股模拟盘-竞价预选"', '"A股模拟盘-盘后评分"'):
            self.assertNotIn(
                literal, source,
                "task names must come from the contract, not be re-declared",
            )

    def test_scheduled_slots_are_a_subset_of_run_slots(self):
        self.assertTrue(
            set(PSch.SCHEDULED_SLOTS) <= set(PSch.RUN_SLOTS),
            "a scheduled entry must name a slot run_slot can actually execute",
        )

    def test_risk_and_fast_entry_are_deliberately_unscheduled(self):
        """The two unscheduled slots are a recorded decision, not an oversight.

        ``risk`` runs inside the intraday scan, and ``fast-entry`` is a
        runner-only branch.  Both used to be absent from the platform tables with
        only a cron comment as evidence; the absence is now asserted.
        """
        self.assertIn("risk", PSch.RUN_SLOTS)
        self.assertIn("fast-entry", PSch.RUNNER_ONLY_SLOTS)
        self.assertNotIn("risk", PSch.SCHEDULED_SLOTS)
        self.assertNotIn("fast-entry", PSch.SCHEDULED_SLOTS)


class CronRenderingFidelityGuard(unittest.TestCase):
    """The contract's cron rendering is semantically the deployed cron file."""

    @classmethod
    def setUpClass(cls):
        cls.cron_lines = [
            line for line in CRON_FILE.read_text(encoding="utf-8").splitlines()
            if "run_paper_slot.sh" in line and not line.strip().startswith("#")
        ]
        cls.assertTrue(cls.cron_lines, "cron file must contain paper runner invocations")

    def test_rendered_triggers_equal_deployed_triggers(self):
        deployed = _collect(self.cron_lines, r"run_paper_slot\.sh (\S+)")
        rendered = _collect(PSch.render_cron_lines(), r"run_paper_slot\.sh (\S+)")
        self.assertEqual(
            set(deployed) - {("weekly-review", "5")}, set(rendered) - {("weekly-review", "5")},
            "the set of (slot, day-of-week) schedules differs",
        )
        for key in sorted(set(deployed) & set(rendered)):
            self.assertEqual(
                deployed[key], rendered[key],
                f"schedule for {key} differs between cron file and contract",
            )

    def test_weekly_review_is_invoked_directly_by_the_cron_file(self):
        """weekly-review bypasses the wrapper script, and the contract records it.

        ``deploy/astock-codex.cron`` calls ``paper_runner.py --slot
        weekly-review`` through ``docker exec`` instead of ``run_paper_slot.sh``,
        because the review runs in the task worker rather than the paper worker.
        The contract still lists it — it is a real scheduled slot — but the
        rendering comparison above excludes it, and this test keeps the reason
        visible so a future edit does not silently move it onto the wrapper.
        """
        cron_text = CRON_FILE.read_text(encoding="utf-8")
        direct = [
            line for line in cron_text.splitlines()
            if "paper_runner.py --slot weekly-review" in line and not line.strip().startswith("#")
        ]
        self.assertTrue(direct, "weekly-review must remain a direct runner invocation")
        wrapper = [line for line in self.cron_lines if "weekly-review" in line]
        self.assertEqual(wrapper, [], "weekly-review must not go through run_paper_slot.sh")
        self.assertIn("weekly-review", PSch.SCHEDULED_SLOTS)


class IntradayIntentGuard(unittest.TestCase):
    """Rendered minute fields still agree with the declared windows and cadence."""

    def test_every_tick_falls_inside_a_declared_window(self):
        windows = [
            (
                (int(start.split(":")[0]), int(start.split(":")[1])),
                (int(end.split(":")[0]), int(end.split(":")[1])),
            )
            for start, end in PSch.INTRADAY_WINDOWS
        ]
        for hour, minute_field in PSch.INTRADAY_CRON_MINUTES.items():
            for minute in _parse_field(minute_field, 0, 59):
                self.assertTrue(
                    any(
                        (start_h, start_m) <= (hour, minute) <= (end_h, end_m)
                        for (start_h, start_m), (end_h, end_m) in windows
                    ),
                    f"tick {hour:02d}:{minute:02d} is outside every declared window",
                )

    def test_cadence_holds_within_each_hour(self):
        step = PSch.INTRADAY_INTERVAL_MINUTES
        for hour, minute_field in PSch.INTRADAY_CRON_MINUTES.items():
            minutes = sorted(_parse_field(minute_field, 0, 59))
            gaps = {b - a for a, b in zip(minutes, minutes[1:], strict=False)}
            self.assertEqual(
                gaps, {step},
                f"hour {hour} minute field {minute_field!r} is not a {step}-minute cadence",
            )

    def test_no_window_hour_is_left_unscanned(self):
        declared = set(PSch.INTRADAY_CRON_MINUTES)
        for start, end in PSch.INTRADAY_WINDOWS:
            start_hour = int(start.split(":")[0])
            end_hour = int(end.split(":")[0])
            for hour in range(start_hour, end_hour + 1):
                self.assertIn(
                    hour, declared,
                    f"hour {hour} is inside an intraday window but has no cron field",
                )

    def test_declared_window_hours_are_exactly_the_rendered_hours(self):
        expected = set()
        for start, end in PSch.INTRADAY_WINDOWS:
            expected.update(range(int(start.split(":")[0]), int(end.split(":")[0]) + 1))
        self.assertEqual(set(PSch.INTRADAY_CRON_MINUTES), expected)


class WindowsRegistrationFidelityGuard(unittest.TestCase):
    """Windows 注册器渲染契约的方式必须和 cron 渲染器一致（三端同步）。

    契约给 ``auction`` / ``open`` / ``close`` 声明了 ``retry_clocks``，cron 侧由
    ``render_cron_lines()`` 逐条渲染。Windows 侧此前只取 ``task.clock``，于是
    "失败兜底"只存在于 Linux 端 —— 平台间行为不一致，而当时的守卫只断言任务名，
    抓不到这个缺口。这个测试直接捕获 ``schtasks`` 调用参数，断言每条 retry 都被
    注册成**独立任务名**（``/TN`` 是主键，同名 + ``/F`` 会覆盖主任务）。
    """

    def _capture_creates(self):
        import paper_trading as PT

        captured = []

        def fake_run(args, **_kwargs):
            captured.append(list(args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(PT.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(PT.os, "name", "nt"), \
             mock.patch.object(PT, "schedule_status",
                               return_value={"installed": True, "tasks": []}):
            result = PT.install_windows_schedule()
        self.assertTrue(result["ok"], result.get("errors"))
        return [args for args in captured if "/Create" in args]

    def test_every_retry_clock_is_registered_as_a_distinct_task(self):
        creates = self._capture_creates()
        names = [args[args.index("/TN") + 1] for args in creates]
        clocks = [args[args.index("/ST") + 1] for args in creates]
        self.assertEqual(
            len(names), len(set(names)),
            "schtasks 任务名必须唯一：同名 + /F 会覆盖主任务而不是追加一条重试",
        )
        for task in PSch.SCHEDULE_TASKS:
            for clock in task.retry_clocks:
                self.assertIn(
                    clock, clocks,
                    f"{task.name} 的重试 {clock} 没有注册到 Windows —— "
                    "cron 渲染器会渲染它，两端会因此行为不一致",
                )
        # 主任务仍然全部注册，retry 只是追加而不是替换。
        self.assertEqual(set(PSch.SCHEDULE_TASK_NAMES) - set(names), set())

    def test_repeat_window_is_only_applied_to_the_primary_intraday_task(self):
        creates = self._capture_creates()
        by_name = {args[args.index("/TN") + 1]: args for args in creates}
        for task in PSch.SCHEDULE_TASKS:
            primary = by_name[task.name]
            self.assertEqual("/RI" in primary, bool(task.repeat),
                             f"{task.name} 的重复窗口标记与契约不一致")
            for index, _clock in enumerate(task.retry_clocks, start=1):
                retry = by_name[f"{task.name}-重试{index}"]
                self.assertNotIn("/RI", retry, "一次性重试任务不能带重复窗口")
                self.assertNotIn("/DU", retry, "一次性重试任务不能带重复窗口")


class ScheduleStatusProbeTests(unittest.TestCase):
    """schedule_status 的探测口径必须可证明，且 Docker profile 下探测正确的文件。

    历史缺陷：Linux 端默认探测 ``/etc/cron.d/astock-quant``（native profile），
    而生产是 Docker profile（``deploy/sync-cron.sh`` 安装 ``/etc/cron.d/astock-codex``），
    于是生产上 ``installed`` 永远 False，提示还指向已退役的 install-centos9.sh。

    测试直接打纯逻辑 ``_schedule_probe``（依赖全部注入），不 patch 模块级
    ``schedule_status``——全量套件里 ``test_operator_boundary`` 会在 import 时把它
    换成静态桩（Windows 沙箱禁用 schtasks），依赖全局状态会让测试与导入顺序耦合。
    """

    @staticmethod
    def _probe(*, os_name, schedule_file, file_exists):
        import paper_trading as PT
        return PT._schedule_probe(
            list(PSch.SCHEDULE_TASK_NAMES),
            os_name=os_name,
            schedule_file=schedule_file,
            run=lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout="", stderr=""),
            isfile=lambda path: file_exists,
        )

    def test_linux_probes_the_docker_profile_cron_file(self):
        found, probe_mode, used = self._probe(
            os_name="posix", schedule_file="/etc/cron.d/astock-codex", file_exists=True)
        self.assertEqual(probe_mode, "file_exists")
        self.assertEqual(used, "/etc/cron.d/astock-codex")
        self.assertEqual(found, list(PSch.SCHEDULE_TASK_NAMES))

    def test_linux_missing_file_is_reported_as_not_installed(self):
        found, probe_mode, _used = self._probe(
            os_name="posix", schedule_file="/etc/cron.d/astock-codex", file_exists=False)
        self.assertEqual(probe_mode, "file_exists")
        self.assertEqual(found, [])

    def test_windows_probe_mode_is_schtasks(self):
        found, probe_mode, _used = self._probe(
            os_name="nt", schedule_file="ignored", file_exists=False)
        self.assertEqual(probe_mode, "schtasks")
        self.assertEqual(found, list(PSch.SCHEDULE_TASK_NAMES))

    def test_linux_install_message_points_at_the_docker_profile(self):
        import paper_trading as PT

        with mock.patch.object(PT.os, "name", "posix"), \
             mock.patch.object(PT, "schedule_status", return_value={
                 "installed": False, "schedule_file": "/etc/cron.d/astock-codex"}):
            result = PT.install_platform_schedule()
        self.assertFalse(result["ok"])
        self.assertIn("deploy/sync-cron.sh", result["message"])
        self.assertNotIn("install-centos9.sh", result["message"])


if __name__ == "__main__":
    unittest.main()
