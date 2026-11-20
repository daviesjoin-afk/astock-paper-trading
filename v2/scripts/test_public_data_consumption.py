"""Integration: real source adapters -> shared SDK -> bounded daily payloads.

All market/news rows below are synthetic. No external service or model runs.
"""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))
import llm_daily
from public_data import PublicDataClient
from source_http import SourceHttp

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
REPORT = {"day": "2026-11-14", "trades": [{"symbol": "600519.SH"}]}


class PublicDataConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.news_time = "2026-11-14 17:58:00"
        def opener(request, timeout):
            self.calls.append(request.full_url)
            if "/fflow/" in request.full_url:
                body = json.dumps({"data": {"klines": [
                    "2026-11-14 17:59:00,100.01,-10.02,20.03,30.04,70.05"]}}).encode()
            else:
                data = {"result": {"cmsArticleWebOld": [{
                    "title": "synthetic announcement", "content": "synthetic text",
                    "date": self.news_time, "mediaName": "fixture",
                    "url": "https://fixture.invalid/news"}]}}
                body = ("jQuery_news(" + json.dumps(data) + ")").encode()
            return io.BytesIO(body)
        self.client = PublicDataClient(SourceHttp(opener=opener, clock=lambda: NOW))

    def test_adapters_really_feed_both_tasks_with_provenance_and_correct_units(self):
        records, audit = llm_daily.public_data_tasks(REPORT, now=NOW, client=self.client)
        tasks = {record["task"]: record for record in records}
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(audit["calls"]), 2)
        self.assertEqual(tasks["flow_reading"]["payload"]["flows"]["super_large_net_inflow_cents"], 7005)
        self.assertEqual(tasks["flow_reading"]["payload"]["flows"]["retail_net_inflow_cents"], -1002)
        flow = tasks["flow_reading"]["payload"]["flows"]
        self.assertFalse(flow["semantics"]["is_raw_level2_tape"])
        self.assertTrue(flow["sources"])
        news = tasks["news_impact"]["payload"]["materials"]
        self.assertEqual(news[0]["title"], "synthetic announcement")
        self.assertTrue(news[0].get("ref"))
        self.assertEqual(audit["calls"][0]["params"], {"code": "600519"})
        self.assertEqual(audit["calls"][1]["params"], {"code": "600519", "page_size": 12})

    def test_source_timeout_skips_only_its_task_and_keeps_healthy_news(self):
        original = self.client
        class FailingFlow:
            def datasets(self):
                return original.datasets()
            def dataset(self, name, **params):
                if "flow" in name:
                    raise TimeoutError("synthetic timeout")
                return original.dataset(name, **params)
        with redirect_stderr(io.StringIO()):
            records, audit = llm_daily.public_data_tasks(REPORT, now=NOW, client=FailingFlow())
        tasks = {record["task"]: record for record in records}
        self.assertEqual(tasks["flow_reading"]["status"], "skipped")
        self.assertIn("payload", tasks["news_impact"])
        self.assertEqual(audit["calls"][0]["outcome"], "network_error")
        self.assertEqual(len(self.calls), 1)

    def test_past_report_in_auto_mode_never_fetches_current_snapshots(self):
        report = dict(REPORT, day="2026-11-13")
        records, audit = llm_daily.public_data_tasks(report, now=NOW, client=self.client)
        self.assertEqual(audit["strategy"], "off")
        self.assertEqual(self.calls, [])
        self.assertTrue(all(record["status"] == "skipped" for record in records))

    def test_news_after_explicit_cutoff_is_excluded_but_flow_kept(self):
        self.news_time = "2026-11-14 18:01:00"
        records, audit = llm_daily.public_data_tasks(REPORT, now=NOW, client=self.client,
                                                   as_of="2026-11-14T18:00:00+08:00")
        tasks = {record["task"]: record for record in records}
        self.assertIn("payload", tasks["flow_reading"])
        self.assertEqual(tasks["news_impact"]["status"], "skipped")
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
