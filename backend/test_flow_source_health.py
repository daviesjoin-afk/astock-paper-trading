# -*- coding: utf-8 -*-
"""资金源不可达必须**诚实可见**：主机级不可达 ≠ 符号级缺字段。

现网现场（OCI oci-main，2026-10-30 实测）：东方财富所有行情入口在该机房的
TCP 443 全部不可达（push2/push2delay/push2his/82.push2/… 解析到 120.76.x、
119.3.x、117.184.x、43.144.x、180.163.x 等多段，全部 FAIL）。腾讯
qt.gtimg.cn（0.6s）与新浪 hq.sinajs.cn（1.2s）正常 —— 但两家的个股报文里
**没有**资金流字段。

后果链：全市场快照取不到 → 走 ``price_only_full_market_snapshot`` 回退 →
快照没有 f62/f66/f72/f78/f84/f184 → ``_fetch_all_flow_map`` 恒空 →
``super_net`` / ``main_pct`` / ``vol_ratio`` 恒缺 → ``main_force_intent``
恒 ``uncertain``。

旧实现把这一切呈现成"缺几个字段"（rows 0 / degraded / 同一批 missing），
运维看不出这是**机房级**不可达，只能一次次换域名试 —— 而换域名救不回来。

这些用例把两种"没数据"钉成**可区分**的行为：

* 所有 clist 候选主机都不可达 → 健康度记 ``host_unreachable``，带已试主机
  列表，reason 可辨识；
* 单只票缺字段（主机可达）→ 仍是符号级 ``symbol_missing`` / degraded；
* **绝不**用委比/成交量伪造 ``super_net`` / ``main_net`` / ``main_pct``；
* 审计文案要能区分"这只票没资金数据"与"今天全市场都没资金数据"。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import data_fetcher as dfc
import marketdata_providers as MP
import paper_risk_decision as PRD

TZ = dt.timezone(dt.timedelta(hours=8))


def _connection_error():
    """模拟 TCP 层连接失败（机房出口被掐时的真实异常类型）。"""
    import requests
    return requests.exceptions.ConnectionError("connection refused")


def _http_error():
    """模拟"主机可达但本次请求被拒"——这是**接口级**失败，不是不可达。"""
    import requests
    return requests.exceptions.HTTPError("404 url format error")


class HostReachabilityClassificationTests(unittest.TestCase):
    """传输层失败才等于"主机不可达"；HTTP 错误说明主机可达。"""

    def test_transport_failures_are_host_unreachable(self):
        self.assertEqual(MP.classify_host_failure(_connection_error()),
                         MP.HOST_UNREACHABLE)
        import requests
        self.assertEqual(MP.classify_host_failure(requests.exceptions.Timeout()),
                         MP.HOST_UNREACHABLE)

    def test_http_errors_mean_the_host_is_reachable(self):
        self.assertEqual(MP.classify_host_failure(_http_error()), MP.HOST_REACHABLE)

    def test_summary_marks_segment_unreachable_only_when_every_host_fails(self):
        all_down = MP.clist_host_summary({
            "push2.eastmoney.com": MP.HOST_UNREACHABLE,
            "82.push2.eastmoney.com": MP.HOST_UNREACHABLE,
        })
        self.assertTrue(all_down["host_unreachable"])
        self.assertEqual(len(all_down["hosts_tried"]), 2)

        one_alive = MP.clist_host_summary({
            "push2.eastmoney.com": MP.HOST_UNREACHABLE,
            "82.push2.eastmoney.com": MP.HOST_EMPTY_RESPONSE,
        })
        self.assertFalse(one_alive["host_unreachable"],
                         "有主机应答过就不能说整段不可达")
        self.assertEqual(one_alive["reachable_hosts"], ["82.push2.eastmoney.com"])


class ClistHostReachabilityTests(unittest.TestCase):
    """``_fetch_clist`` 必须把主机级可达性事实带出来。"""

    def test_all_hosts_unreachable_is_reported_as_such(self):
        def fake_get_json(_url, _params, **_kwargs):
            raise _connection_error()

        with mock.patch.object(dfc, "_get_json", fake_get_json), \
                mock.patch.object(dfc, "CLIST_HOSTS", ["a.eastmoney.com", "b.eastmoney.com"]):
            meta = dfc._fetch_clist("f12", pages=1, pz=10, return_meta=True)
        reach = meta["host_reachability"]
        self.assertTrue(reach["host_unreachable"])
        self.assertEqual(sorted(reach["hosts_tried"]), ["a.eastmoney.com", "b.eastmoney.com"])
        self.assertEqual(meta["rows"], [])

    def test_a_reachable_host_means_the_segment_is_not_unreachable(self):
        """主机可达但没数据 = 数据级缺失，绝不能升级成"整段不可达"。"""
        def fake_get_json(_url, _params, **_kwargs):
            raise _http_error()

        with mock.patch.object(dfc, "_get_json", fake_get_json), \
                mock.patch.object(dfc, "CLIST_HOSTS", ["a.eastmoney.com"]):
            meta = dfc._fetch_clist("f12", pages=1, pz=10, return_meta=True)
        self.assertFalse(meta["host_reachability"]["host_unreachable"])

    def test_success_path_also_reports_reachability(self):
        def fake_get_json(_url, params, **_kwargs):
            return {"data": {"total": 1, "diff": [{"f12": "000001"}]}}

        with mock.patch.object(dfc, "_get_json", fake_get_json), \
                mock.patch.object(dfc, "CLIST_HOSTS", ["a.eastmoney.com"]):
            meta = dfc._fetch_clist("f12", pages=1, pz=10, return_meta=True)
        reach = meta["host_reachability"]
        self.assertFalse(reach["host_unreachable"])
        self.assertEqual(reach["host_outcomes"]["a.eastmoney.com"], MP.HOST_OK)


class FlowFetchStateTests(unittest.TestCase):
    """``_flow_fetch_state`` 必须区分主机级不可达与符号级缺失。"""

    def setUp(self):
        self._saved = dict(dfc._flow_fetch_state)

    def tearDown(self):
        dfc._flow_fetch_state = dict(self._saved)

    def _meta(self, rows, *, unreachable):
        outcomes = {
            host: (MP.HOST_UNREACHABLE if unreachable else MP.HOST_EMPTY_RESPONSE)
            for host in dfc.CLIST_HOSTS
        }
        return {
            "rows": rows, "total": len(rows), "pages_expected": 1,
            "pages_ok": 1, "failed_pages": [], "complete": True,
            "host_reachability": MP.clist_host_summary(outcomes),
        }

    def test_unreachable_hosts_are_recorded_with_the_tried_host_list(self):
        with mock.patch.object(dfc, "_fetch_clist",
                               return_value=self._meta([], unreachable=True)):
            self.assertEqual(dfc._fetch_all_flow_map(), {})
        state = dfc.get_flow_fetch_state()
        self.assertEqual(state["flow_source_status"], MP.FLOW_SOURCE_HOST_UNREACHABLE)
        self.assertTrue(state["host_unreachable"])
        self.assertEqual(sorted(state["hosts_tried"]), sorted(dfc.CLIST_HOSTS))
        self.assertIn("不可达", state["reason"])
        self.assertIn("整段", state["reason"])

    def test_reachable_hosts_with_missing_rows_stay_symbol_level(self):
        with mock.patch.object(dfc, "_fetch_clist",
                               return_value=self._meta([], unreachable=False)):
            dfc._fetch_all_flow_map()
        state = dfc.get_flow_fetch_state()
        self.assertEqual(state["flow_source_status"], MP.FLOW_SOURCE_SYMBOL_MISSING)
        self.assertFalse(state["host_unreachable"])

    def test_flow_fields_are_never_fabricated(self):
        """主机不可达时**不得**造出 super_net / main_net / main_pct。"""
        with mock.patch.object(dfc, "_fetch_clist",
                               return_value=self._meta([], unreachable=True)):
            out = dfc._fetch_all_flow_map()
        self.assertEqual(out, {})
        state = dfc.get_flow_fetch_state()
        self.assertEqual(state["rows"], 0)

    def test_symbol_level_status_uses_the_same_vocabulary(self):
        self.assertEqual(MP.flow_source_status({}), MP.FLOW_SOURCE_SYMBOL_MISSING)
        self.assertEqual(
            MP.flow_source_status({}, host_unreachable=True),
            MP.FLOW_SOURCE_HOST_UNREACHABLE)
        self.assertEqual(
            MP.flow_source_status({"main_net": 1.0}), MP.FLOW_SOURCE_OK)


class SourceHealthVisibilityTests(unittest.TestCase):
    """``data_source_health`` 必须一眼看出"东财整段不可达 / 腾讯新浪可用"。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="astock-flow-health-")
        self.addCleanup(self._cleanup)
        patcher = mock.patch.object(dfc, "DATA_SOURCE_HEALTH_PATH",
                                    os.path.join(self._dir, "health.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            try:
                os.unlink(os.path.join(self._dir, name))
            except OSError:
                pass
        os.rmdir(self._dir)

    def _health(self, *, eastmoney_unreachable):
        outcomes = {
            host: (MP.HOST_UNREACHABLE if eastmoney_unreachable
                   else MP.HOST_EMPTY_RESPONSE)
            for host in dfc.CLIST_HOSTS
        }
        meta = {
            "rows": [], "total": 0, "pages_expected": 0, "pages_ok": 0,
            "failed_pages": [1], "complete": False,
            "host_reachability": MP.clist_host_summary(outcomes),
        }
        patches = [
            mock.patch.object(dfc, "load_source_health", return_value={}),
            mock.patch.object(dfc, "_save_source_health"),
            mock.patch.object(dfc, "reset_data_source"),
            mock.patch.object(dfc.time, "sleep"),
            mock.patch.object(dfc, "http_get", return_value=""),
            mock.patch.object(dfc, "fetch_independent_realtime_for_codes",
                              return_value=[{"code": "300214", "price": 10.0,
                                             "pct": 1.0, "source": "tencent_public_quote",
                                             "quote_at": dt.datetime.now(TZ).isoformat()}]),
            mock.patch.object(dfc, "_fetch_clist", return_value=meta),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return dfc.check_data_source_health(force=True)

    def test_unreachable_segment_is_visible_in_the_health_payload(self):
        payload = self._health(eastmoney_unreachable=True)
        self.assertEqual(payload["eastmoney"]["status"], "host_unreachable")
        self.assertTrue(payload["eastmoney"]["host_unreachable"])
        self.assertTrue(payload["eastmoney"]["hosts_tried"])
        self.assertIn("不可达", payload["eastmoney"]["reason"])
        # 资金源状态独立成段：行情源可用不代表资金源可用。
        self.assertIn("flow_source", payload)
        self.assertTrue(payload["flow_source"]["host_unreachable"])
        # 探测已经证实整段不可达时，资金源状态就必须如实是 host_unreachable
        # —— 不等资金流抓取跑过（那一路可能还没跑，状态仍是 unknown）。
        self.assertEqual(payload["flow_source"]["status"],
                         MP.FLOW_SOURCE_HOST_UNREACHABLE)
        self.assertTrue(payload["flow_source"]["hosts_tried"])
        self.assertIn("整段不可达", payload["flow_source"]["summary"])
        self.assertIn("腾讯", payload["flow_source"]["summary"])
        # 关键：不能因为腾讯/新浪可用就把整段不可达说成"正常"。
        self.assertTrue(payload["healthy"])

    def test_reachable_hosts_with_no_rows_are_not_reported_as_unreachable(self):
        payload = self._health(eastmoney_unreachable=False)
        self.assertNotEqual(payload["eastmoney"]["status"], "host_unreachable")
        self.assertFalse(payload["eastmoney"]["host_unreachable"])

    def test_health_payload_is_json_serialisable(self):
        payload = self._health(eastmoney_unreachable=True)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)


class IntentAuditTextTests(unittest.TestCase):
    """审计文案要能区分"这只票没资金数据"与"今天全市场都没资金数据"。"""

    POSITION = {"code": "600000", "qty": 100, "cost": 10.0, "account_id": "trend_pullback"}

    def _intent(self, *, flow_source_status, main_pct=None, super_net=None):
        quote = {
            "price": 9.0, "pct": -3.0, "high": 9.5, "low": 9.1,
            "main_pct": main_pct, "super_net": super_net, "vol_ratio": None,
            "quote_at": "2026-10-30T10:00:00+08:00",
        }
        if flow_source_status is not None:
            quote["flow_source_status"] = flow_source_status
        return PRD.main_force_intent(self.POSITION, quote, market={"live_index_pct": 0.0})

    def test_host_unreachable_is_reported_as_a_market_wide_gap(self):
        intent = self._intent(flow_source_status=MP.FLOW_SOURCE_HOST_UNREACHABLE)
        self.assertEqual(intent["flow_missing_scope"], "host_unreachable")
        self.assertEqual(intent["flow_source_status"], MP.FLOW_SOURCE_HOST_UNREACHABLE)
        self.assertIn("超大单资金", intent["flow_evidence_missing"])

    def test_symbol_missing_is_reported_as_a_per_symbol_gap(self):
        intent = self._intent(flow_source_status=MP.FLOW_SOURCE_SYMBOL_MISSING)
        self.assertEqual(intent["flow_missing_scope"], "symbol_missing")
        self.assertNotEqual(intent["flow_missing_scope"], "host_unreachable")

    def test_missing_fields_stay_identical_between_the_two_scopes(self):
        """两种现场的缺口**字段**相同，但**成因范围**不同 —— 这正是要能区分的点。"""
        host = self._intent(flow_source_status=MP.FLOW_SOURCE_HOST_UNREACHABLE)
        symbol = self._intent(flow_source_status=MP.FLOW_SOURCE_SYMBOL_MISSING)
        self.assertEqual(host["flow_evidence_missing"], symbol["flow_evidence_missing"])
        self.assertNotEqual(host["flow_missing_scope"], symbol["flow_missing_scope"])

    def test_unknown_scope_when_the_line_carries_no_status(self):
        intent = self._intent(flow_source_status=None)
        self.assertEqual(intent["flow_missing_scope"], "unknown")

    def test_flow_fields_are_never_invented_from_price_fields(self):
        """委比/成交量之类的字段不得被拿来冒充资金字段。"""
        intent = self._intent(flow_source_status=MP.FLOW_SOURCE_HOST_UNREACHABLE)
        self.assertIsNone(intent.get("super_net"))
        self.assertIsNone(intent.get("main_pct"))


if __name__ == "__main__":
    unittest.main()
