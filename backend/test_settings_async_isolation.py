# -*- coding: utf-8 -*-
"""``POST /api/settings/`` 不得在事件循环里执行同步数据库事务。

缺陷（B3）：设置路由是 ``async def``，却在函数体里直接调用 ``P.init_db()``、
``P._db(immediate=True)`` 并立即返回快照。这些是同步 SQLite 调用，还可能等待
写锁（``paper_storage`` 的普通锁等待可达 60 秒）。等待期间被阻塞的正是同一进程
用来调度**所有**请求的事件循环，于是一次"保存设置"会拖住并发心跳等无关请求。

本文件用可观测行为钉住修复：同步业务单元必须在线程里跑，事件循环必须继续推进。
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

import api_settings as SETTINGS


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeAdaptive:
    def __init__(self, sink=None):
        self.calls = []
        self._sink = sink

    def update_ai_settings(self, updates):
        self.calls.append(("ai_settings", updates))
        if self._sink is not None:
            self._sink.append(threading.current_thread())

    def update_ai_slot_fn(self, slot, **kwargs):
        self.calls.append(("slot", slot, kwargs))
        if self._sink is not None:
            self._sink.append(threading.current_thread())
        return {"slot": slot}

    def update_ai_review_settings_fn(self, **kwargs):
        self.calls.append(("review", kwargs))
        if self._sink is not None:
            self._sink.append(threading.current_thread())
        return dict(kwargs)


class SettingsOffLoopTests(unittest.TestCase):
    def setUp(self):
        self.threads = []
        self.adaptive = _FakeAdaptive(self.threads)
        self.sleep_seconds = 0.0
        self.runtime_updates = []
        self.audits = []

        @contextmanager
        def fake_db(immediate=False):
            self.threads.append(threading.current_thread())
            yield object()

        def fake_init_db():
            if self.sleep_seconds:
                import time
                time.sleep(self.sleep_seconds)
            self.threads.append(threading.current_thread())

        def fake_update(conn, updates, actor=None):
            self.runtime_updates.append((dict(updates), actor))

        patchers = [
            mock.patch.object(SETTINGS, "_get_adaptive", return_value=self.adaptive),
            mock.patch.object(SETTINGS.P, "init_db", side_effect=fake_init_db),
            mock.patch.object(SETTINGS.P, "_db", side_effect=fake_db),
            mock.patch.object(SETTINGS.P, "_audit",
                              side_effect=lambda *a, **k: self.audits.append(a)),
            mock.patch.object(SETTINGS.RSET, "update", side_effect=fake_update),
            mock.patch.object(SETTINGS, "_snapshot", return_value={"ok": True}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_with_heartbeat(self, coro_factory):
        """在事件循环里跑路由，同时数心跳次数。"""
        async def scenario():
            ticks = 0
            stop = False

            async def heartbeat():
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.01)

            task = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.05)
            baseline = ticks
            result = await coro_factory()
            during = ticks - baseline
            stop = True
            await task
            return result, during

        return asyncio.run(scenario())

    def test_settings_write_does_not_block_the_event_loop(self):
        self.sleep_seconds = 0.25  # 模拟一次真实的锁等待
        request = _FakeRequest({"simulation": {"default_starting_capital": 200000}})
        result, ticks = self._run_with_heartbeat(
            lambda: SETTINGS.update_settings(request, True)
        )
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(
            ticks, 5, f"保存设置期间事件循环被阻塞（仅 {ticks} 次心跳）",
        )
        self.assertEqual(len(self.runtime_updates), 1)
        self.assertEqual(self.runtime_updates[0][1], "human-ui")

    def test_database_work_runs_on_a_worker_thread(self):
        request = _FakeRequest({"risk": {"single_position_max_amount": 30000}})
        asyncio.run(SETTINGS.update_settings(request, True))
        self.assertTrue(self.threads)
        caller = threading.current_thread()
        self.assertTrue(
            all(thread is not caller for thread in self.threads),
            "SQLite 事务必须在工作线程里执行，不能占用事件循环线程",
        )

    def test_ai_payloads_are_applied_in_the_same_off_loop_unit(self):
        request = _FakeRequest({"ai": {"review_mode": "dual"}})
        asyncio.run(SETTINGS.update_settings(request, True))
        self.assertEqual(self.adaptive.calls, [("ai_settings", {"review_mode": "dual"})])

    def test_value_error_still_maps_to_422(self):
        from fastapi import HTTPException

        def boom(*_args, **_kwargs):
            raise ValueError("非法设置值")

        with mock.patch.object(SETTINGS.RSET, "update", side_effect=boom):
            request = _FakeRequest({"simulation": {"default_starting_capital": 1}})
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(SETTINGS.update_settings(request, True))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_unconfirmed_request_writes_nothing(self):
        from fastapi import HTTPException

        request = _FakeRequest({"simulation": {"default_starting_capital": 1}})
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(SETTINGS.update_settings(request, False))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.threads, [])
        self.assertEqual(self.runtime_updates, [])


class AiSlotOffLoopTests(unittest.TestCase):
    def test_ai_slot_write_runs_off_the_event_loop(self):
        seen = []
        adaptive = _FakeAdaptive(seen)
        with mock.patch.object(SETTINGS, "_get_adaptive", return_value=adaptive):
            asyncio.run(SETTINGS.update_ai_review_slot(
                "ai1", _FakeRequest({"enabled": True}), True,
            ))
        self.assertEqual([call[0] for call in adaptive.calls], ["slot"])
        self.assertTrue(seen)
        self.assertTrue(all(thread is not threading.current_thread() for thread in seen))

    def test_ai_review_mode_runs_off_the_event_loop(self):
        seen = []
        adaptive = _FakeAdaptive(seen)
        with mock.patch.object(SETTINGS, "_get_adaptive", return_value=adaptive):
            result = asyncio.run(SETTINGS.update_ai_review(
                _FakeRequest({"review_mode": "dual"}), True,
            ))
        self.assertEqual(result, {"review_mode": "dual", "single_reviewer_slot": None})
        self.assertTrue(seen)
        self.assertTrue(all(thread is not threading.current_thread() for thread in seen))


if __name__ == "__main__":
    unittest.main()
