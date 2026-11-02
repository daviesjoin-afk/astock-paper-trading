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
    "execution-resume",
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
    {"auction", "open", "risk", "execution-resume", "intraday", "fast-entry", "auction-track"}
)

#: Legacy validation message.  Preserved verbatim: callers and tests assert on
#: this exact string, and rewording it would be a user-visible change disguised
#: as a refactor.
SLOT_ERROR = "slot 必须是 auction、open、risk、execution-resume、close、weekly-review 或 intraday"

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

    ``close`` carries five retries because its failures are not all alike: an
    interrupted process needs one early retry (15:10/15:12), while "完整日线覆盖
    不足" is a *data-not-ready* verdict that only a later window can clear.
    ``universe.refresh_history`` is bounded to 420 seconds and resumes from a
    persisted cursor, so the 15:25/15:40/16:00 entries are the incremental
    continuation those later windows exist for.  Each attempt still runs the same
    fail-closed coverage gate — a late window never marks partial history as
    success.
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
#: ``risk`` used to be absent by design: the intraday scan performed the risk
#: pass in the same minute.  PR-11 B moves it onto its own cron entry, its own
#: host lock and its own runtime lease, because a risk pass that waits behind a
#: full-market candidate scan cannot hold a three-minute cadence.  The intraday
#: scan keeps its ``monitor_risk()`` call as a fallback; the durable scan claim
#: (:mod:`paper_risk_scan_state`) still guarantees at most one risk pass per
#: ``(cycle_id, asof_date, scan_minute)`` no matter which entry runs first.
SCHEDULE_TASKS: tuple[ScheduleTask, ...] = (
    ScheduleTask("A股模拟盘-竞价跟踪", "09:15", "auction-track", _WEEKDAY_DAYS,
                 repeat="00:12"),
    ScheduleTask("A股模拟盘-竞价预选", "09:25", "auction", _WEEKDAY_DAYS,
                 retry_clocks=("09:27", "09:29")),
    ScheduleTask("A股模拟盘-日内监控-上午", "09:30", "intraday", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-风险快通道-上午", "09:33", "risk", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-日内监控-下午", "13:00", "intraday", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-风险快通道-下午", "13:00", "risk", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-委托恢复-上午", "09:33", "execution-resume", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-委托恢复-下午", "13:00", "execution-resume", _WEEKDAY_DAYS,
                 repeat="01:55"),
    ScheduleTask("A股模拟盘-开盘审批", "09:31", "open", _WEEKDAY_DAYS,
                 retry_clocks=("09:33", "09:35")),
    ScheduleTask("A股模拟盘-盘后评分", "15:05", "close", _WEEKDAY_DAYS,
                 retry_clocks=("15:10", "15:12", "15:25", "15:40", "16:00")),
    ScheduleTask("A股模拟盘-周度复盘", "15:20", "weekly-review", "FRI"),
)

#: Task names probed by ``schedule_status``.  Derived from the table so the
#: probe list can never disagree with the registration list again.
SCHEDULE_TASK_NAMES: tuple[str, ...] = tuple(task.name for task in SCHEDULE_TASKS)

#: Slots covered by a standalone scheduled entry.  ``fast-entry`` stays absent by
#: design: it is a runner-only branch with its own cron file and its own log.
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

#: 风险快通道的分钟字段：与 intraday 同一三分钟网格、同一交易窗口。
#:
#: 同网格是刻意的：两个入口在同分钟进入时，durable scan claim
#: （``paper_risk_scan_state``）保证只有一方真正执行，另一方回 ``already_scanned``。
#: 如果错开一分钟，同一窗口内会出现两次风险扫描（两个不同 ``scan_minute``），
#: 既没有收益，又会让"这一分钟最多一次风控"的验收失效。
RISK_CRON_MINUTES: dict[int, str] = dict(INTRADAY_CRON_MINUTES)

#: 会重复触发的 slot → 分钟字段表。渲染顺序仍由 :data:`SCHEDULE_TASKS` 决定。
REPEAT_CRON_MINUTES: dict[str, dict[int, str]] = {
    "intraday": INTRADAY_CRON_MINUTES,
    "risk": RISK_CRON_MINUTES,
    "execution-resume": INTRADAY_CRON_MINUTES,
}

#: 宿主串行锁（``deploy/run_paper_slot.sh`` 里 ``flock`` 的那把文件）按 slot 分配。
#:
#: 默认仍是共享的 ``paper-scan.lock``：竞价、开盘审批、日内监控、快通道继续互相
#: 串行，PR-11 不删除这把锁。risk 与 execution-resume 各有独立锁；候选扫描
#: 不通过共享锁挡住它们。共享 cgroup 资源隔离留 P11-D。
HOST_LOCK_BY_SLOT: dict[str, str] = {
    "risk": "risk-scan.lock", "execution-resume": "execution-scan.lock",
}

#: 没有独立锁的 slot 共用的宿主锁文件名。
DEFAULT_HOST_LOCK = "paper-scan.lock"


def worker_container_name(slot: str | None) -> str:
    """Deployment lane only: preserve slot identities and trading dispatch."""
    return ("astock-paper-worker" if slot in {"risk", "execution-resume"}
            else "astock-candidate-worker")


def host_lock_name(slot: str | None) -> str:
    """该 slot 在宿主机上使用哪把串行锁（纯查表，不碰文件系统）。"""
    return HOST_LOCK_BY_SLOT.get(str(slot or "").strip().lower(), DEFAULT_HOST_LOCK)


def joins_full_scan_intent(slot: str | None) -> bool:
    """该 slot 是否参与"全扫描优先"的意图标记协议。

    意图标记只保护共用 ``paper-scan.lock`` 的 lane：``fast-entry`` 看到全扫描
    留下的标记就主动让路。风险 lane 有自己的锁，既不该留标记，也不该因为别人
    留了标记而让路。
    """
    return host_lock_name(slot) == DEFAULT_HOST_LOCK


def window_run_key(slot, moment):
    """Align repeating lane identities to the same three-minute grid."""
    minute = moment.minute // INTRADAY_INTERVAL_MINUTES * INTRADAY_INTERVAL_MINUTES
    return f"{slot}:{moment.replace(minute=minute, second=0, microsecond=0):%Y%m%d%H%M}"


def render_cron_lines() -> list[str]:
    """Cron field lines for every scheduled entry, in table order.

    Returns the five cron fields plus the command suffix (``run_paper_slot.sh
    <slot>``); locking, log redirection and absolute paths stay with the caller
    so the generated file remains reviewable as the deployment artifact it is.
    A repeating task emits its window lines once, not once per retry entry.
    """
    lines: list[str] = []
    emitted_repeats: set[str] = set()
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
        minutes_by_hour = REPEAT_CRON_MINUTES.get(task.slot)
        if task.repeat and minutes_by_hour and task.slot not in emitted_repeats:
            emitted_repeats.add(task.slot)
            for hour in sorted(minutes_by_hour):
                lines.append(
                    f"{minutes_by_hour[hour]} {hour:02d} * * {dow} "
                    f"run_paper_slot.sh {task.slot}"
                )
    return lines
