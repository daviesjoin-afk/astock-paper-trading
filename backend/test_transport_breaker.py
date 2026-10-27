# -*- coding: utf-8 -*-
"""传输层按主机短路的语义。

背景：盘中扫描 95% 的时间是网络等待，其中相当一部分在等**已经挂掉的主机**。
生产实测单轮 `http_get_attempt` 58 次调用里 34 次失败、累计 44 秒墙钟而
CPU 只有 1.1 秒；这些等待串在 `risk_pass`(67s)、`position_quotes`(25s) 等
串行阶段里，把整轮扫描拖到 190 秒以上。

短路必须保守，以下每一条都有对应用例：

* 只在**传输层**失败（超时/连接错误）计数；HTTP 状态错误说明对端活着，应当清零。
* 未到阈值不得短路：偶发抖动仍允许下一次真的去请求。
* 短路抛 ``requests.ConnectionError``（与真实连接失败同类型），调用方原有的
  fail-closed 分支原样生效——短路不是"放行"，是"更快地判定不可用"。
* 一次成功即清零；冷却到期自动恢复。
* 按主机隔离：一个域挂了不该让别的域停止尝试。
"""
import unittest
from unittest import mock

import requests

import marketdata_transport as T

URL = "https://qt.gtimg.cn/q=sh600000"


def _http_error(status):
    """构造带 ``response.status_code`` 的 HTTPError。

    真实 ``raise_for_status()`` 一定带 response；短路要区分 4xx（主机可达）
    与 5xx（主机侧异常），所以测试也必须带上状态码，不能用裸 HTTPError。
    """
    response = mock.Mock()
    response.status_code = status
    error = requests.HTTPError(f"{status} error", response=response)
    return error


class TransportBreakerTests(unittest.TestCase):
    def setUp(self):
        T.reset_transport_breakers()

    def tearDown(self):
        T.reset_transport_breakers()

    def _fail(self, times):
        for _ in range(times):
            with self.assertRaises(requests.RequestException):
                with mock.patch.object(T, "_http_get_impl",
                                       side_effect=requests.Timeout("slow")):
                    T.http_get(URL, timeout=1, retries=0)

    def test_single_failure_does_not_open(self):
        self._fail(1)
        self.assertFalse(T.transport_breaker_snapshot()["qt.gtimg.cn"]["open"])

    def test_threshold_failures_open_the_host(self):
        self._fail(T._TRANSPORT_BREAKER_THRESHOLD)
        snapshot = T.transport_breaker_snapshot()["qt.gtimg.cn"]
        self.assertTrue(snapshot["open"])
        self.assertEqual(snapshot["fails"], T._TRANSPORT_BREAKER_THRESHOLD)

    def test_open_host_fails_fast_without_touching_the_transport(self):
        self._fail(T._TRANSPORT_BREAKER_THRESHOLD)
        with mock.patch.object(T, "_http_get_impl") as impl:
            with self.assertRaises(requests.ConnectionError) as raised:
                T.http_get(URL, timeout=5)
        impl.assert_not_called()
        self.assertIn("breaker open", str(raised.exception))

    def test_success_resets_consecutive_failures(self):
        self._fail(3)
        with mock.patch.object(T, "_http_get_impl", return_value="ok"):
            self.assertEqual(T.http_get(URL), "ok")
        snapshot = T.transport_breaker_snapshot()["qt.gtimg.cn"]
        self.assertEqual(snapshot["fails"], 0)
        self.assertFalse(snapshot["open"])

    def test_client_error_resets_and_never_opens(self):
        """4xx = 我们的请求有问题，主机可达 → 清零，永不短路。"""
        for _ in range(T._TRANSPORT_BREAKER_THRESHOLD + 2):
            with self.assertRaises(requests.HTTPError):
                with mock.patch.object(T, "_http_get_impl",
                                       side_effect=_http_error(404)):
                    T.http_get(URL, timeout=1, retries=0)
        self.assertIsNone(T._transport_breaker_remaining("qt.gtimg.cn"))

    def test_server_error_counts_as_host_failure(self):
        """5xx = 主机侧异常，必须计入失败。

        生产实测 push2 的 `trends2` 会快速返回 502，而同主机的 `details` /
        `ulist` 在读超时。若把 5xx 也算"可达"，两者交替出现会让短路永远不触发。
        """
        for _ in range(T._TRANSPORT_BREAKER_THRESHOLD):
            with self.assertRaises(requests.HTTPError):
                with mock.patch.object(T, "_http_get_impl",
                                       side_effect=_http_error(502)):
                    T.http_get(URL, timeout=1, retries=0)
        self.assertIsNotNone(T._transport_breaker_remaining("qt.gtimg.cn"))

    def test_interleaved_client_error_resets_the_consecutive_count(self):
        """穿插的 4xx 必须清零连续失败，否则会误短路整个主机。

        场景：超时、超时、404、超时、超时、404……如果 4xx 只是"不计数"，
        六次**非连续**超时仍会把短路打开，与"连续失败才触发"的文档矛盾，
        并且会让同一主机上所有正常端点被误伤 90 秒。
        """
        def boom(_url, **_kwargs):
            raise requests.Timeout("slow")

        def not_found(_url, **_kwargs):
            raise _http_error(404)

        sequence = [boom, boom, not_found] * 4
        for step in sequence:
            with mock.patch.object(T, "_http_get_impl", side_effect=step):
                with self.assertRaises(requests.RequestException):
                    T.http_get(URL, timeout=1, retries=0)
            self.assertIsNone(
                T._transport_breaker_remaining("qt.gtimg.cn"),
                "穿插 4xx 后不得累积成短路")

    def test_interleaved_server_error_still_opens(self):
        """生产实况：超时与 502 交替，短路的应是"主机在坏"，不能永不触发。"""
        def boom(_url, **_kwargs):
            raise requests.Timeout("slow")

        def bad_gateway(_url, **_kwargs):
            raise _http_error(502)

        sequence = [boom, bad_gateway] * T._TRANSPORT_BREAKER_THRESHOLD
        for step in sequence:
            with mock.patch.object(T, "_http_get_impl", side_effect=step):
                with self.assertRaises(requests.RequestException):
                    T.http_get(URL, timeout=1, retries=0)
        self.assertIsNotNone(T._transport_breaker_remaining("qt.gtimg.cn"))

    def test_hosts_are_isolated(self):
        self._fail(T._TRANSPORT_BREAKER_THRESHOLD)
        other = "https://sinajs.cn/list=sh600000"
        self.assertIsNone(T._transport_breaker_remaining(T._transport_host(other)))
        with mock.patch.object(T, "_http_get_impl", return_value="ok"):
            self.assertEqual(T.http_get(other), "ok")

    def test_cooldown_expiry_restores_retries(self):
        clock = [1000.0]
        with mock.patch.object(T.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in range(T._TRANSPORT_BREAKER_THRESHOLD):
                T._transport_breaker_note("qt.gtimg.cn", ok=False)
            self.assertIsNotNone(T._transport_breaker_remaining("qt.gtimg.cn"))
            clock[0] += T._TRANSPORT_BREAKER_COOLDOWN_SECONDS - 1
            self.assertIsNotNone(T._transport_breaker_remaining("qt.gtimg.cn"))
            clock[0] += 2
            self.assertIsNone(T._transport_breaker_remaining("qt.gtimg.cn"))

    def test_snapshot_is_a_copy(self):
        T._transport_breaker_note("qt.gtimg.cn", ok=False)
        snapshot = T.transport_breaker_snapshot()
        snapshot["qt.gtimg.cn"]["fails"] = 999
        self.assertEqual(
            T.transport_breaker_snapshot()["qt.gtimg.cn"]["fails"], 1)

    def test_reset_clears_every_host(self):
        T._transport_breaker_note("sinajs.cn", ok=False)
        T._transport_breaker_note("sinajs.cn", ok=False)
        T.reset_transport_breakers()
        self.assertEqual(T.transport_breaker_snapshot(), {})

    def test_host_parsing_is_stable(self):
        self.assertEqual(T._transport_host("https://QT.GTIMG.CN/q?x=1"), "qt.gtimg.cn")
        self.assertEqual(T._transport_host("not a url"), "unknown")
        # 同一主机的不同路径/参数必须归到同一个短路键，否则每个 URL 各占一格。
        self.assertEqual(T._transport_host("https://sinajs.cn/a"),
                         T._transport_host("https://sinajs.cn/b?c=1"))
        self.assertNotEqual(T._transport_host("https://sinajs.cn/a"),
                            T._transport_host("https://qt.gtimg.cn/a"))

    def test_success_path_still_returns_text(self):
        """回归：短路包装不能改变正常返回。"""
        with mock.patch.object(T, "_http_get_impl", return_value='{"ok":true}'):
            self.assertEqual(T.http_get(URL), '{"ok":true}')


if __name__ == "__main__":
    unittest.main()
