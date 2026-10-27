"""Bound shadow-source retries without changing successful parsed evidence."""
import contextlib
import datetime as dt
import io
import json
from types import SimpleNamespace
import unittest
from unittest import mock

import alt_data as AD
import paper_timing as PTM


class MicrostructureBudgetTests(unittest.TestCase):
    def setUp(self):
        with AD._cache_lock:
            AD._mem_cache.clear()
        # 源级短路是模块级状态：不清空会让"连续失败到阈值"泄漏到同进程的后续
        # 用例（后面的成功用例会拿到 breaker open 而不是真的去请求）。
        AD.reset_micro_breakers()

    def tearDown(self):
        AD.reset_micro_breakers()

    def test_slow_failed_requests_do_not_start_another_transport_budget(self):
        clock = [0.0]
        def fail(*args, **kwargs):
            clock[0] += kwargs["timeout"]
            raise TimeoutError("private-endpoint")
        with mock.patch.object(AD.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(AD.requests, "get", side_effect=fail) as get, \
                mock.patch.object(AD.subprocess, "run") as curl:
            with self.assertRaises(RuntimeError):
                AD._micro_get(AD.TICK_DETAIL_URL, {})
        # 断言"整个函数只有一个共享预算"这件事本身：主机轮换必须共享同一份
        # 额度，而不是每个主机各起一份。预算绝对值随单次 timeout 调整（2026-10-27
        # 由 4s 降到 2s），所以这里从函数默认值推导，不写死 12。
        per_attempt = AD._micro_get.__defaults__[0]
        self.assertEqual(get.call_count, 3)
        self.assertEqual(clock[0], 3 * per_attempt)
        self.assertLessEqual(clock[0], 6.0, "单次微结构请求的等待预算不得超过 6 秒")
        curl.assert_not_called()

    def test_quick_request_failures_still_allow_curl_recovery(self):
        payload = {"details": ["09:31,10,100,1,2"]}
        with mock.patch.object(AD.requests, "get", side_effect=TimeoutError), \
                mock.patch.object(AD.subprocess, "run", return_value=SimpleNamespace(
                    stdout=json.dumps({"data": payload}))) as curl:
            self.assertEqual(AD._micro_get(AD.TICK_DETAIL_URL, {}), payload)
        self.assertEqual(curl.call_count, 1)

    def test_remaining_budget_is_passed_to_curl_and_no_later_attempt_starts(self):
        """共享 deadline 的不变量：剩余额度传给 curl，耗尽后不再起新尝试。

        显式传 ``timeout=4``（3 主机 → 12 秒预算）把场景固定在"三个请求各花
        3 秒、剩 3 秒交给 curl"。这样默认预算日后调整（2026-10-27 已由 4s
        降到 2s）不会让这条不变量测试变成在测默认值。
        """
        clock = [0.0]
        def fail_request(*args, **kwargs):
            clock[0] += 3
            raise TimeoutError
        def fail_curl(args, **kwargs):
            self.assertEqual(float(args[args.index("--max-time") + 1]), 3)
            clock[0] += 3
            raise TimeoutError
        with mock.patch.object(AD.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(AD.requests, "get", side_effect=fail_request), \
                mock.patch.object(AD.subprocess, "run", side_effect=fail_curl) as curl:
            with self.assertRaises(RuntimeError):
                AD._micro_get(AD.TICK_DETAIL_URL, {}, timeout=4)
        self.assertEqual(curl.call_count, 1)
        self.assertEqual(clock[0], 12)

    def test_partial_results_and_worker_timings_preserve_unknowns(self):
        def source(url, params, **kwargs):
            if url == AD.TICK_DETAIL_URL:
                raise TimeoutError("private-marker")
            return {"trends": ["09:31,10.02,10.00,1,1"]}
        output = io.StringIO()
        # Patch the HTTP boundary so the real depth phase and parser still run.
        fields = ["0"] * 31
        fields[10], fields[20] = "100", "50"
        response = mock.Mock(text='v="' + "~".join(fields) + '";')
        with mock.patch.object(AD, "_micro_get", side_effect=source), \
                mock.patch.object(AD.requests, "get", return_value=response), \
                contextlib.redirect_stderr(output), PTM.capture("intraday"):
            result = AD.market_microstructures(["000001", "000001"], asof_day=dt.date.today())
            stages = PTM.snapshot("test")["stages"]
        row = result["000001"]
        self.assertEqual(row["status"], "partial")
        self.assertIsNone(row["active_buy_sell_imbalance"])
        self.assertAlmostEqual(row["depth_imbalance"], 1 / 3, places=4)
        self.assertFalse(row["score_applied"])
        for name in ("depth", "ticks", "trend"):
            self.assertEqual(stages["microstructure." + name]["calls"], 1)
        self.assertEqual(stages["microstructure.ticks"]["errors"], 1)
        self.assertNotIn("private-marker", output.getvalue())
