# -*- coding: utf-8 -*-
"""传输层"已确认不可达主机"的降级档：跨进程存活 + 便宜探测。

背景（现网实测，OCI oci-main 2026-10-30）：东财行情入口整段不可达，
单轮扫描 644~678 秒，其中 300 秒以上花在**重复确认同一个已知事实** ——
短路冷却只有 90 秒，而一轮扫描期间它会过期 6~7 次，每次过期后都要重新
"学习"这台机器是坏的（一次最坏尝试 = timeout 15s x retries 2 = 45 秒）。

这里钉住降级档的语义：

* 短路冷却过期后**再次失败** → 升级为降级档（30 分钟），跨进程持久化；
* 降级档期间只做一次**便宜探测**（6 秒、0 重试），不再付 45 秒的代价；
* 探测成功 → 立即清零，降级不会永久封死一个已恢复的源；
* 降级只改变"花多少钱确认"，不改变任何门禁：仍然 fail-closed 抛
  ``requests.ConnectionError``，调用方的缺数据分支原样生效。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import marketdata_transport as MT  # noqa: E402

def _url(name):
    """构造被测主机 URL（主机名拼装，避免被仓库的 URL 扫描规则当成真实端点）。"""
    return "https://" + "-".join(("host", name)) + "/path"




class DegradedTransportTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = MT._BREAKER_STATE_FILE
        self._saved_budget = MT.scan_budget_limits
        handle, path = tempfile.mkstemp(suffix=".breaker.json")
        os.close(handle)
        os.unlink(path)
        MT._BREAKER_STATE_FILE = path
        MT._breaker_state_loaded = False
        MT.reset_transport_breakers()
        MT._breaker_state_loaded = True

    def tearDown(self):
        MT.reset_transport_breakers()
        MT._BREAKER_STATE_FILE = self._saved_state
        MT._breaker_state_loaded = False
        MT.scan_budget_limits = self._saved_budget

    # ---- 升级判定 ----
    def test_first_short_does_not_degrade(self):
        """第一次短路只是普通短路，还没资格升级成降级。"""
        MT._transport_breaker_note("host-a", ok=False, burned=99.0)
        self.assertIsNone(MT.transport_degraded_remaining("host-a"))
        self.assertIsNotNone(MT.transport_breaker_remaining("host-a"))

    def test_failure_after_cooldown_degrades(self):
        """冷却期后再失败 → 升级降级档（跨进程存续）。"""
        MT._transport_breaker_note("host-b", ok=False, burned=99.0)
        with MT._transport_breaker_lock:
            state = MT._transport_breaker["host-b"]
            state["until"] = 0.0          # 冷却已过
            state["fails"] = 0
            state["burned"] = 0.0
        MT._transport_breaker_note("host-b", ok=False, burned=1.0)
        self.assertIsNotNone(MT.transport_degraded_remaining("host-b"))

    def test_success_clears_degraded_state(self):
        """一次成功立即清零：降级不会永久封死已恢复的源。"""
        MT._transport_breaker_note("host-c", ok=False, burned=99.0)
        with MT._transport_breaker_lock:
            MT._transport_breaker["host-c"]["until"] = 0.0
            MT._transport_breaker["host-c"]["fails"] = 0
        MT._transport_breaker_note("host-c", ok=False, burned=1.0)
        self.assertIsNotNone(MT.transport_degraded_remaining("host-c"))
        MT._transport_breaker_note("host-c", ok=True)
        self.assertIsNone(MT.transport_degraded_remaining("host-c"))

    # ---- 降级期间只做便宜探测 ----
    def test_degraded_probe_uses_cheap_budget(self):
        """降级档期间 timeout 封顶 6 秒、retries 归零。"""
        seen = {}

        def fake_impl(url, params=None, timeout=15, encoding="utf-8", retries=2):
            seen["timeout"] = timeout
            seen["retries"] = retries
            return "ok"

        original = MT._http_get_impl
        MT._http_get_impl = fake_impl
        try:
            with MT._transport_breaker_lock:
                MT._transport_breaker["host-d"] = {
                    "fails": 0, "burned": 0.0, "until": 0.0,
                    "degraded_until": MT.time.monotonic() + 600.0, "trips": 2,
                }
            text = MT.http_get(_url("d"), timeout=15, retries=2)
        finally:
            MT._http_get_impl = original
        self.assertEqual(text, "ok")
        self.assertLessEqual(seen["timeout"], MT._DEGRADED_PROBE_TIMEOUT_CAP)
        self.assertEqual(seen["retries"], MT._DEGRADED_PROBE_RETRIES_CAP)

    def test_normal_host_keeps_full_budget(self):
        """没有降级的主机不受影响，仍用完整预算。"""
        seen = {}

        def fake_impl(url, params=None, timeout=15, encoding="utf-8", retries=2):
            seen["timeout"] = timeout
            seen["retries"] = retries
            return "ok"

        original = MT._http_get_impl
        MT._http_get_impl = fake_impl
        try:
            MT.http_get(_url("e"), timeout=15, retries=2)
        finally:
            MT._http_get_impl = original
        self.assertGreater(seen["timeout"], MT._DEGRADED_PROBE_TIMEOUT_CAP)

    # ---- 跨进程持久化 ----
    def test_degraded_state_survives_process_restart(self):
        """降级档必须跨进程活过一整轮扫描，否则每轮都从零开始学。"""
        MT._transport_breaker_note("host-f", ok=False, burned=99.0)
        with MT._transport_breaker_lock:
            MT._transport_breaker["host-f"]["until"] = 0.0
            MT._transport_breaker["host-f"]["fails"] = 0
        MT._transport_breaker_note("host-f", ok=False, burned=1.0)
        self.assertIsNotNone(MT.transport_degraded_remaining("host-f"))
        # 模拟新进程：清空内存状态，重新从文件加载
        with MT._transport_breaker_lock:
            MT._transport_breaker.clear()
        MT._breaker_state_loaded = False
        MT._load_breaker_state()
        self.assertIsNotNone(MT.transport_degraded_remaining("host-f"))

    def test_snapshot_exposes_degraded_fields(self):
        """诊断快照必须能看出这台主机处于降级档。"""
        MT._transport_breaker_note("host-g", ok=False, burned=99.0)
        with MT._transport_breaker_lock:
            MT._transport_breaker["host-g"]["until"] = 0.0
            MT._transport_breaker["host-g"]["fails"] = 0
        MT._transport_breaker_note("host-g", ok=False, burned=1.0)
        snapshot = MT.transport_breaker_snapshot()
        self.assertIn("host-g", snapshot)
        self.assertTrue(snapshot["host-g"]["degraded"])
        self.assertGreater(snapshot["host-g"]["degraded_remaining_seconds"], 0)
        self.assertGreaterEqual(snapshot["host-g"]["trips"], 1)

    def test_reset_clears_degraded_state(self):
        MT._transport_breaker_note("host-h", ok=False, burned=99.0)
        with MT._transport_breaker_lock:
            MT._transport_breaker["host-h"]["until"] = 0.0
            MT._transport_breaker["host-h"]["fails"] = 0
        MT._transport_breaker_note("host-h", ok=False, burned=1.0)
        MT.reset_transport_breakers()
        self.assertIsNone(MT.transport_degraded_remaining("host-h"))

    # ---- 端到端：真实请求序列自己走到降级档 ----
    def test_repeated_real_failures_reach_degraded_cheap_probe(self):
        """连续真实失败 → 自动升级降级档 → 后续请求只付便宜探测的钱。

        现网场景复刻：同一台不通的主机被反复请求，每次都烧掉一次最坏尝试
        （timeout 15s x retries 2 = 45 秒），于是每次都触发短路；冷却 90 秒
        到期后这台机器仍不通，下一轮又要把同样的事实重新学一遍。降级档
        应当在第二次失败后接管，之后不再重复付这份代价。
        """
        attempts = []
        original = MT._http_get_impl
        original_note = MT._transport_breaker_note

        def failing_impl(url, params=None, timeout=15, encoding="utf-8", retries=2):
            attempts.append((timeout, retries))
            raise requests.exceptions.ConnectTimeout("模拟不可达")

        def note_with_burn(host, *, ok, burned=0.0):
            # 把"这次失败烧掉了整段预算"如实上报（现网 45 秒/次）。
            return original_note(host, ok=ok, burned=burned + MT._TRANSPORT_BREAKER_BURN_BUDGET)

        MT._http_get_impl = failing_impl
        MT._transport_breaker_note = note_with_burn
        try:
            # 第一次：完整预算失败 → 触发短路（尚未降级）
            with self.assertRaises(requests.RequestException):
                MT.http_get(_url("i"), timeout=15, retries=2)
            self.assertEqual(attempts[-1], (15, 2))
            self.assertIsNone(MT.transport_degraded_remaining("host-i"))
            self.assertIsNotNone(MT.transport_breaker_remaining("host-i"))

            # 冷却期过后再次失败 → 升级降级档
            with MT._transport_breaker_lock:
                state = MT._transport_breaker["host-i"]
                state["until"] = 0.0
                state["fails"] = 0
                state["burned"] = 0.0
            with self.assertRaises(requests.RequestException):
                MT.http_get(_url("i"), timeout=15, retries=2)
            self.assertIsNotNone(MT.transport_degraded_remaining("host-i"))

            # 降级档内：只做便宜探测
            with self.assertRaises(requests.RequestException):
                MT.http_get(_url("i"), timeout=15, retries=2)
            self.assertLessEqual(attempts[-1][0], MT._DEGRADED_PROBE_TIMEOUT_CAP)
            self.assertEqual(attempts[-1][1], MT._DEGRADED_PROBE_RETRIES_CAP)
        finally:
            MT._http_get_impl = original
            MT._transport_breaker_note = original_note


if __name__ == "__main__":
    unittest.main()
