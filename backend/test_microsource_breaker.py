# -*- coding: utf-8 -*-
"""微结构数据源短路（breaker）语义。

背景：``_micro_get`` 对「3 主机 × 4 秒」共享一个尝试预算，单次最坏要等 12 秒。
盘中扫描会对十余只候选各发 tick / trend，当上游集体不可用时，一轮扫描有近百秒
纯粹在等已挂掉的源超时（生产实测 microstructure.ticks 12 次 108 秒）。这段证据
是 shadow-grade、fail-open，只进软分，不值得拖垮整轮扫描。

这些用例把"短路"钉成可验证行为，而不是靠生产日志观察：

* 未到阈值不短路（偶发失败仍允许下一次真的去请求）；
* 到阈值后立即失败、**不再消耗任何网络预算**；
* 一次成功就把连续失败清零；
* 冷却到期后自动恢复重试；
* 快照只读，不会被调用方改坏内部状态。
"""
import unittest
from unittest import mock

import alt_data as AD


class MicroSourceBreakerTests(unittest.TestCase):
    def setUp(self):
        AD.reset_micro_breakers()

    def tearDown(self):
        AD.reset_micro_breakers()

    def test_source_key_is_stable_per_endpoint(self):
        self.assertEqual(AD._micro_source_key(AD.TICK_DETAIL_URL), "eastmoney_tick")
        self.assertEqual(AD._micro_source_key(AD.MINUTE_TREND_URL), "eastmoney_trend")
        self.assertEqual(AD._micro_source_key(AD.TENCENT_QUOTE_URL + "sh600000"),
                         "tencent_depth")
        self.assertEqual(AD._micro_source_key("https://example.invalid/x"),
                         "eastmoney_other")

    def test_single_failure_does_not_open_the_breaker(self):
        with mock.patch.object(AD, "_micro_fetch", side_effect=TimeoutError("slow")):
            with self.assertRaises(TimeoutError):
                AD._micro_get(AD.TICK_DETAIL_URL, {})
        self.assertFalse(AD.micro_breaker_snapshot()["eastmoney_tick"]["open"])
        # 阈值未到：下一次仍然真的去请求（这里用成功替身，证明没有被短路）。
        with mock.patch.object(AD, "_micro_fetch", return_value={"ok": True}) as fetch:
            self.assertEqual(AD._micro_get(AD.TICK_DETAIL_URL, {}), {"ok": True})
        self.assertEqual(fetch.call_count, 1)

    def test_threshold_failures_open_the_breaker(self):
        with mock.patch.object(AD, "_micro_fetch", side_effect=TimeoutError("slow")):
            for _ in range(AD.MICRO_BREAKER_THRESHOLD):
                with self.assertRaises(TimeoutError):
                    AD._micro_get(AD.TICK_DETAIL_URL, {})
        snapshot = AD.micro_breaker_snapshot()["eastmoney_tick"]
        self.assertTrue(snapshot["open"])
        self.assertEqual(snapshot["fails"], AD.MICRO_BREAKER_THRESHOLD)

    def test_open_breaker_fails_fast_without_touching_the_transport(self):
        AD._micro_breaker_note("eastmoney_tick", False)
        AD._micro_breaker_note("eastmoney_tick", False)
        with mock.patch.object(AD, "_micro_fetch") as fetch, \
                mock.patch.object(AD.requests, "get") as get, \
                mock.patch.object(AD.subprocess, "run") as curl:
            with self.assertRaises(RuntimeError) as raised:
                AD._micro_get(AD.TICK_DETAIL_URL, {})
        self.assertIn("breaker open", str(raised.exception))
        fetch.assert_not_called()
        get.assert_not_called()
        curl.assert_not_called()

    def test_success_resets_consecutive_failures(self):
        AD._micro_breaker_note("eastmoney_trend", False)
        AD._micro_breaker_note("eastmoney_trend", True)
        snapshot = AD.micro_breaker_snapshot()["eastmoney_trend"]
        self.assertEqual(snapshot["fails"], 0)
        self.assertFalse(snapshot["open"])

    def test_cooldown_expiry_restores_retries(self):
        clock = [1000.0]
        with mock.patch.object(AD.time, "monotonic", side_effect=lambda: clock[0]):
            AD._micro_breaker_note("eastmoney_tick", False)
            AD._micro_breaker_note("eastmoney_tick", False)
            self.assertIsNotNone(AD._micro_breaker_remaining("eastmoney_tick"))
            clock[0] += AD.MICRO_BREAKER_COOLDOWN_SECONDS - 1
            self.assertIsNotNone(AD._micro_breaker_remaining("eastmoney_tick"))
            clock[0] += 2
            self.assertIsNone(AD._micro_breaker_remaining("eastmoney_tick"))

    def test_breakers_are_isolated_per_source(self):
        AD._micro_breaker_note("eastmoney_tick", False)
        AD._micro_breaker_note("eastmoney_tick", False)
        self.assertIsNotNone(AD._micro_breaker_remaining("eastmoney_tick"))
        # 另一个源不受影响：一个端点挂了不该让别的端点也停止尝试。
        self.assertIsNone(AD._micro_breaker_remaining("eastmoney_trend"))

    def test_snapshot_is_a_copy_not_internal_state(self):
        AD._micro_breaker_note("eastmoney_tick", False)
        snapshot = AD.micro_breaker_snapshot()
        snapshot["eastmoney_tick"]["fails"] = 999
        self.assertEqual(AD.micro_breaker_snapshot()["eastmoney_tick"]["fails"], 1)

    def test_reset_clears_every_source(self):
        AD._micro_breaker_note("eastmoney_tick", False)
        AD._micro_breaker_note("eastmoney_trend", False)
        AD.reset_micro_breakers()
        self.assertEqual(AD.micro_breaker_snapshot(), {})


if __name__ == "__main__":
    unittest.main()
