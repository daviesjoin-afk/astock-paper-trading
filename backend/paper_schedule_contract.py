# -*- coding: utf-8 -*-
"""Scheduler contract: the single source of truth for paper slot vocabulary.

Before this module the slot vocabulary lived in four places that could drift:
``paper_slot_service.SUPPORTED_SLOTS`` (what ``run_slot`` accepts),
``paper_runner``'s argparse ``choices`` (what cron/systemd may invoke),
``paper_trading.install_windows_schedule``'s ``definitions`` (the Windows task
table), and ``deploy/astock-codex.cron`` (the Linux table).  A slot added to
one list silently stayed absent from the others, and the only record of the
resulting platform gap was a comment in the cron file.

This module owns three things and nothing else:

1. the slot vocabulary, split by *how a slot is entered* — :data:`RUN_SLOTS` go
   through ``run_slot`` and are therefore validated by ``validate_slot``;
   :data:`RUNNER_ONLY_SLOTS` are runner branches that bypass ``run_slot``
   entirely and must never be accepted by it;
2. the hot-path set, which decides lease TTL and lock retry policy;
3. the schedule *table* (task name, clock, slot, days, retries), from which both
   the Windows task registration and the cron template are derived.

It deliberately does **not** own: any database access, any subprocess call, any
filesystem access, or the decision of *when* a slot runs on a given host.  The
table describes the intended schedule; each platform renders it in its own
native format.  Keeping the renderers separate is what lets the Linux cron file
stay auditable as plain text while Windows uses ``schtasks`` flags.

Zero project imports by design: the runners, the HTTP layer and the deployment
scripts all read from here, so importing anything back would recreate the
cycles this boundary exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Slots dispatched through ``paper_trading.run_slot``.  ``validate_slot``
#: accepts exactly this set, so adding a slot here without a matching branch in
#: ``_run_slot_impl`` is a code-level error, not a silent no-op.
RUN_SLOTS: tuple[str, ...] = (
    "auction",
    "open",
    "risk",
    "close",
    "weekly-review",
    "intraday",
)

#: Slots that exist only as a ``paper_runner`` branch.  ``fast-entry`` calls
#: ``monitor_fast_entries()`` directly and never reaches ``run_slot``, so it is
#: intentionally **absent** from :data:`RUN_SLOTS`: accepting it in
#: ``validate_slot`` would imply a dispatch path that does not exist.
#: ``auction-track`` is a scheduled observation-only branch with its own
#: bounded JSON artifacts; it never enters ledger preflight or run_slot.
RUNNER_ONLY_SLOTS: tuple[str, ...] = ("fast-entry", "auction-track")

#: Everything a runner may be asked to execute.
RUNNER_SLOTS: tuple[str, ...] = RUN_SLOTS + RUNNER_ONLY_SLOTS

#: Slots whose lease TTL and lock retry policy assume a short, latency-sensitive
#: pass.  Mirrors ``paper_trading.HOT_PATH_SLOTS``.
HOT_PATH_SLOTS: frozenset[str] = frozenset(
    {"auction", "open", "risk", "intraday", "fast-entry", "auction-track"}
)

#: Legacy validation message.  Preserved verbatim: callers and tests assert on
#: this exact string, and rewording it would be a user-visible change disguised
#: as a refactor.
SLOT_ERROR = "slot 必须是 auction、open、risk、close、weekly-review 或 intraday"

_WEEKDAY_DAYS = "MON,TUE,WED,THU,FRI"


def validate_slot(slot: str) -> str:
    """Return a run slot or raise the legacy ``ValueError``."""
    if slot not in RUN_SLOTS:
        raise ValueError(SLOT_ERROR)
    return slot


def is_hot_path(slot: str | None) -> bool:
    """Whether a slot belongs to the latency-sensitive trading hot path."""
    return bool(slot and str(slot).lower() in HOT_PATH_SLOTS)


@dataclass(frozen=True)
class ScheduleTask:
    """One scheduler entry, expressed independently of any platform.

    ``clock`` is ``HH:MM`` in the exchange's local timezone.  ``repeat`` is the
    ``schtasks``-style ``HH:MM`` duration, or ``None`` for a one-shot task.
    ``retry_clocks`` lists additional one-shot times at which the same slot is
    retried; ``run_slot`` is idempotent for a completed run, so a retry that
    arrives after success is a cheap no-op rather than a duplicate scan.
    """

    name: str
    clock: str
    slot: str
    days: str
    repeat: str | None = None
    retry_clocks: tuple[str, ...] = field(default_factory=tuple)

    def entries(self) -> tuple[tuple[str, str], ...]:
        """Every ``(task_name, clock)`` this row produces, in run order."""
        return ((self.name, self.clock),) + tuple(
            (self.name, clock) for clock in self.retry_clocks
        )


#: The canonical schedule table.  Order is the rendering order for both
#: platforms and is part of the contract: the Windows registration and the cron
#: file list tasks in the same sequence so a diff between them stays reviewable.
#:
#: ``risk`` is intentionally absent.  It is a valid ``run_slot`` slot and the
#: runner accepts it, but neither platform gives it a standalone entry: the
#: intraday scan performs the risk pass in the same minute.  That convention
#: used to exist only as a comment in the cron file; recording the absence here
#: makes it an explicit, testable property of the contract.
SCHEDULE_TASKS: tuple[ScheduleTask, ...] = (
    ScheduleTask("A股模拟盘-竞价跟踪", "09:15", "auction-track", _WEEKDAY_DAYS,
                 repeat="00:12"),
    ScheduleTask("A股模拟盘-竞价预选", "09:25", "auction", _WEEKDAY_DAYS,
                 retry_clocks=("09:27", "09:29")),
    ScheduleTask("A股模拟盘-日内监控-上午", "09:30", "intraday", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-日内监控-下午", "13:00", "intraday", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-开盘审批", "09:31", "open", _WEEKDAY_DAYS,
                 retry_clocks=("09:33", "09:35")),
    ScheduleTask("A股模拟盘-盘后评分", "15:05", "close", _WEEKDAY_DAYS,
                 retry_clocks=("15:10", "15:12")),
    ScheduleTask("A股模拟盘-周度复盘", "15:20", "weekly-review", "FRI"),
)

#: Task names probed by ``schedule_status``.  Derived from the table so the
#: probe list can never disagree with the registration list again.
SCHEDULE_TASK_NAMES: tuple[str, ...] = tuple(task.name for task in SCHEDULE_TASKS)

#: Slots covered by a standalone scheduled entry.  ``risk`` and ``fast-entry``
#: are absent by design — see the note on :data:`SCHEDULE_TASKS`.
SCHEDULED_SLOTS: frozenset[str] = frozenset(task.slot for task in SCHEDULE_TASKS)

#: Intraday scan windows, ``HH:MM`` inclusive on both ends.
#:
#: The end times are the last minute the deployed cron actually fires in, not the
#: wall-clock close of the session: the morning window's last tick is 11:24
#: (``0-25/3`` never reaches :25) and the afternoon's is 14:57 (``*/3`` on the
#: final whole hour).  Recording 11:25 / 14:55 because the prose says "至 11:25
#: 和 14:55" would make the declared window disagree with the schedule it claims
#: to describe, which is exactly the drift this contract exists to prevent.
INTRADAY_WINDOWS: tuple[tuple[str, str], ...] = (
    ("09:30", "11:24"),
    ("13:00", "14:57"),
)

#: Intraday scan cadence in minutes.  Both platforms repeat the intraday task at
#: this interval; the cron renderer turns it into ``*/N`` minute expressions.
INTRADAY_INTERVAL_MINUTES = 3

# 09:15、09:18、09:21、09:24 只观测；09:25 仍由原竞价预选负责。
AUCTION_TRACKING_CRON_MINUTES = "15-24/3"

#: Per-hour cron minute fields for the intraday scan, keyed by hour.
#:
#: These are recorded verbatim from ``deploy/astock-codex.cron`` rather than
#: derived, because they are **not** fully derivable from the window bounds and
#: the cadence: the 09:33 start (freeing 09:30/09:31 for the one-shot intraday
#: run and the ``open`` slot, which share ``paper-scan.lock``) and the 13:55 end
#: (a 13:57 tick would duplicate 14:00) are deliberate authorial choices with no
#: single rule that reproduces both.  Inventing such a rule here would replace
#: an auditable hand-written file with a guess that looks authoritative.
#:
#: ``INTRADAY_WINDOWS`` and ``INTRADAY_INTERVAL_MINUTES`` state the *intent*;
#: this table states the *rendering*.  A guard test asserts the two agree — every
#: tick falls inside a window, the cadence holds, and no window hour is missing —
#: so a change to the intent that is not reflected here fails loudly instead of
#: silently drifting.
INTRADAY_CRON_MINUTES: dict[int, str] = {
    9: "33-59/3",
    10: "*/3",
    11: "0-25/3",
    13: "0-55/3",
    14: "*/3",
}


def render_cron_lines() -> list[str]:
    """Cron field lines for every scheduled entry, in table order.

    Returns the five cron fields plus the command suffix (``run_paper_slot.sh
    <slot>``); locking, log redirection and absolute paths stay with the caller
    so the generated file remains reviewable as the deployment artifact it is.
    A repeating task emits its window lines once, not once per retry entry.
    """
    lines: list[str] = []
    emitted_intraday = False
    for task in SCHEDULE_TASKS:
        dow = "1-5" if task.days == _WEEKDAY_DAYS else "5"
        if task.slot == "auction-track":
            lines.append(f"{AUCTION_TRACKING_CRON_MINUTES} 9 * * {dow} run_paper_slot.sh auction-track")
            continue
        for _, clock in task.entries():
            if task.repeat:
                continue
            hour, minute = clock.split(":")
            lines.append(f"{minute} {hour} * * {dow} run_paper_slot.sh {task.slot}")
        if task.slot == "intraday" and not emitted_intraday:
            emitted_intraday = True
            for hour in sorted(INTRADAY_CRON_MINUTES):
                lines.append(
                    f"{INTRADAY_CRON_MINUTES[hour]} {hour:02d} * * {dow} "
                    f"run_paper_slot.sh intraday"
                )
    return lines
