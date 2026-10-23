# -*- coding: utf-8 -*-
"""收盘任务连续失败的两类原因与重试边界（2026-10-23 诊断修复）。

生产事实（只读调查）
--------------------
``paper_jobs`` 里 slot='close' 的记录连续失败，两类错误交替出现：

* 10-19 / 10-21 「任务进程中断或超时，被回收」——进程被 SIGKILL/OOM；
* 10-20 / 10-22 「完整日线覆盖不足，保留上一有效因子版本」——日线/因子没补齐。

两个可证明的机制性缺陷：

1. **僵尸租约把当天的重试全部挡成 already_done。** close 的租约 TTL 是 2400 秒，
   续期间隔是 TTL/4=10 分钟，而"进程是否还活着"只看 ``expires_at``：进程被杀
   后 expires_at 仍留在未来，15:10/15:12 两次兜底全部被跳过（``paper_runner``
   退出码 0）。当天的失败要等下一个碰库的进程在 ``init_db`` 里补记。
2. **重试窗口不够。** ``universe.refresh_history`` 是 420 秒 bounded + 持久化
   游标增量续传，它的设计前提是"下一次 15:25/15:40/16:00 继续"，但收盘任务当时
   只有 15:05/15:10/15:12 三个触发点，且自动重试预算是全局 2 次——后三个窗口
   既不存在，存在了也会因为预算耗尽而 already_done。

钉死的契约：

    CJ-01  心跳续期间隔 ≤ 120 秒，且不短于 60 秒（长任务几分钟内即可暴露死亡）。
    CJ-02  判定放弃的三种证据：租约到期 / 心跳丢失 3 次续期 / legacy 行超出 TTL；
           心跳新鲜的活任务绝不被抢。
    CJ-03  15:05 被杀的进程在 15:12 的兜底窗口可被判定放弃（当天能重跑）。
    CJ-04  回收是**合并** detail 而不是覆盖：retry_count / history_refresh /
           factor_refresh 保留，并写入 failure_kind 与 elapsed_seconds。
    CJ-05  调度契约给出 close 的 5 个兜底窗口，渲染与部署一致（见
           ``test_paper_schedule_contract``），且重试预算覆盖这些窗口。
    CJ-06  覆盖不足仍然 fail-closed：多给窗口不等于放行不完整历史数据。

纯 fixture：内存/临时 sqlite，不联网、不读写真实账本。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.modules.setdefault("requests", mock.MagicMock())
import paper_schedule_contract as PSch  # noqa: E402
import paper_trading as PT  # noqa: E402

CLOSE_TTL = 2400


class LeaseHeartbeatTests(unittest.TestCase):
    """CJ-01 / CJ-02 —— 心跳节奏与放弃判定。"""

    def test_cj01_long_slot_heartbeat_never_exceeds_two_minutes(self):
        for ttl in (300, 600, 2400):
            interval = PT._lease_renew_interval(ttl)
            self.assertGreaterEqual(interval, 60.0, f"ttl={ttl} 续期过于频繁")
            self.assertLessEqual(interval, 120.0, f"ttl={ttl} 死亡暴露太慢")

    def test_cj01_intraday_interval_is_tightened_as_before(self):
        self.assertEqual(75.0, PT._lease_renew_interval(300))
        self.assertEqual(120.0, PT._lease_renew_interval(CLOSE_TTL))

    def test_cj02_expired_lease_is_abandoned(self):
        now = dt.datetime(2026, 10, 23, 15, 10, 0)
        self.assertTrue(PT._lease_abandoned(
            "2026-10-23 15:09:00", "2026-10-23 15:09:30", "2026-10-23 15:05:00",
            lease_ttl_seconds=CLOSE_TTL, now=now,
        ))

    def test_cj02_live_lease_with_fresh_heartbeat_is_never_stolen(self):
        now = dt.datetime(2026, 10, 23, 15, 40, 0)
        self.assertFalse(PT._lease_abandoned(
            "2026-10-23 16:02:00", "2026-10-23 15:39:00", "2026-10-23 15:05:00",
            lease_ttl_seconds=CLOSE_TTL, now=now,
        ))

    def test_cj02_dead_process_with_live_lease_is_detected_by_heartbeat(self):
        """租约还在未来，但心跳停了 3 个续期间隔——进程已被杀。"""
        now = dt.datetime(2026, 10, 23, 15, 40, 0)
        self.assertTrue(PT._lease_abandoned(
            "2026-10-23 15:45:00", "2026-10-23 15:05:00", "2026-10-23 15:05:00",
            lease_ttl_seconds=CLOSE_TTL, now=now,
        ))

    def test_cj02_legacy_rows_without_lease_metadata_use_started_at(self):
        now = dt.datetime(2026, 10, 23, 16, 30, 0)
        self.assertTrue(PT._lease_abandoned(
            None, None, "2026-10-23 15:05:00",
            lease_ttl_seconds=CLOSE_TTL, now=now,
        ))
        self.assertFalse(PT._lease_abandoned(
            None, None, "2026-10-23 16:25:00",
            lease_ttl_seconds=CLOSE_TTL, now=now,
        ))

    def test_cj02_unparsable_timestamps_fail_closed(self):
        self.assertTrue(PT._lease_abandoned(
            "not-a-time", "also-not", None, lease_ttl_seconds=CLOSE_TTL,
        ))


class SameDayRetryWindowTests(unittest.TestCase):
    """CJ-03 —— 当天最后一次兜底触发必须能接住被杀的任务。"""

    KILLED_AT = dt.datetime(2026, 10, 23, 15, 5, 0)

    def _killed_job(self):
        """15:05 起跑、随后被 SIGKILL 的 close 行：心跳停在 started_at。"""
        started = self.KILLED_AT.strftime("%Y-%m-%d %H:%M:%S")
        expires = (self.KILLED_AT + dt.timedelta(seconds=CLOSE_TTL)).strftime(
            "%Y-%m-%d %H:%M:%S")
        return {"started_at": started, "heartbeat_at": started, "expires_at": expires}

    def test_cj03_last_retry_window_reclaims_the_killed_run(self):
        job = self._killed_job()
        retry_at = dt.datetime(2026, 10, 23, 15, 12, 0)   # 契约里的最后一个即时兜底
        self.assertTrue(PT._lease_abandoned(
            job["expires_at"], job["heartbeat_at"], job["started_at"],
            lease_ttl_seconds=CLOSE_TTL, now=retry_at,
        ), "被杀的 15:05 收盘任务在 15:12 仍被当成在运行，当天不会重跑")

    def test_cj03_a_healthy_long_run_is_not_reclaimed(self):
        started = self.KILLED_AT
        job = {
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat_at": (started + dt.timedelta(minutes=7)).strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": (started + dt.timedelta(seconds=CLOSE_TTL)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.assertFalse(PT._lease_abandoned(
            job["expires_at"], job["heartbeat_at"], job["started_at"],
            lease_ttl_seconds=CLOSE_TTL, now=started + dt.timedelta(minutes=8),
        ))

    def test_cj05_retry_budget_covers_every_contracted_window(self):
        close = next(t for t in PSch.SCHEDULE_TASKS if t.slot == "close")
        self.assertEqual(
            ("15:10", "15:12", "15:25", "15:40", "16:00"), close.retry_clocks,
        )
        self.assertGreater(
            PT.SLOT_MAX_AUTO_RETRIES["close"], len(close.retry_clocks),
            "预算必须让每个契约兜底窗口都能真正执行一次",
        )
        self.assertEqual(2, PT.DEFAULT_MAX_AUTO_RETRIES,
                         "其它 slot 的失败不会因为等数据自愈，预算保持 2 次")


class RecoveryDetailTests(unittest.TestCase):
    """CJ-04 —— 回收合并 detail，并记录失败分类与耗时。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patch = mock.patch.object(PT, "DB_PATH", self.path)
        self._patch.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        self.tmp.cleanup()

    def _running_close_job(self, *, started_minutes_ago):
        started = (dt.datetime.now() - dt.timedelta(minutes=started_minutes_ago)).strftime(
            "%Y-%m-%d %H:%M:%S")
        expired = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        detail = {
            "retry_count": 1,
            "history_refresh": {"status": "partial", "updated": 812},
            "factor_refresh": {"status": "blocked", "coverage_pct": 71.5},
        }
        self.conn.execute(
            """INSERT INTO paper_jobs(slot,market_date,status,detail,started_at,
                                     heartbeat_at,expires_at)
               VALUES('close',?, 'running', ?, ?, ?, ?)""",
            ("2026-10-23", json.dumps(detail), started, started, expired),
        )
        self.conn.commit()

    def test_cj04_recovery_preserves_prior_detail_and_records_duration(self):
        self._running_close_job(started_minutes_ago=70)
        with self.conn:
            PT._recover_stale_runtime_state(self.conn, boot_recovery=False)
        row = self.conn.execute(
            "SELECT status,detail,started_at,finished_at FROM paper_jobs "
            "WHERE slot='close'"
        ).fetchone()
        self.assertEqual("failed", row["status"])
        detail = json.loads(row["detail"])
        self.assertEqual(1, detail["retry_count"], "retry_count 被回收覆盖，重试预算失真")
        self.assertEqual({"status": "partial", "updated": 812}, detail["history_refresh"])
        self.assertEqual(71.5, detail["factor_refresh"]["coverage_pct"])
        self.assertEqual("interrupted", detail["failure_kind"])
        self.assertIn("任务进程中断或超时", detail["error"])
        self.assertGreaterEqual(detail["elapsed_seconds"], 69 * 60)
        self.assertTrue(row["finished_at"], "回收必须写入 finished_at，否则耗时不可算")

    def test_cj04_live_run_is_not_recovered(self):
        started = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        future = (dt.datetime.now() + dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO paper_jobs(slot,market_date,status,detail,started_at,
                                     heartbeat_at,expires_at)
               VALUES('close','2026-10-23','running','{}',?,?,?)""",
            (started, started, future),
        )
        self.conn.commit()
        with self.conn:
            PT._recover_stale_runtime_state(self.conn, boot_recovery=False)
        status = self.conn.execute(
            "SELECT status FROM paper_jobs WHERE slot='close'").fetchone()[0]
        self.assertEqual("running", status, "活进程的 running 认领被回收了")


class CoverageStillFailsClosedTests(unittest.TestCase):
    """CJ-06 —— 多给窗口不放行不完整历史数据。"""

    def test_cj06_blocked_close_is_still_failed(self):
        detail = {
            "status": "blocked",
            "reason": "完整日线覆盖不足，保留上一有效因子版本",
            "refresh_gate": {"passed": False, "coverage": 0.71},
        }
        self.assertEqual("failed", PT._slot_completion_status(detail))

    def test_cj06_retry_budget_cannot_turn_a_blocked_run_into_success(self):
        # 预算只决定"能不能再试一次"，完成状态永远由同一套门禁决定。
        detail = {"status": "blocked", "reason": "完整日线覆盖不足"}
        for _ in range(PT.SLOT_MAX_AUTO_RETRIES["close"]):
            self.assertEqual("failed", PT._slot_completion_status(detail))


if __name__ == "__main__":
    unittest.main()
