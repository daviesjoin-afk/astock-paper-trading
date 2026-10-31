# -*- coding: utf-8 -*-
"""PR-7：候选公共事实层收敛（新闻代码并集 + 快照复用 + 指纹短路）。

三层事实各自只有一个 owner：

* ``paper_candidate_scan.collect_evidence`` —— 一轮扫描只按**代码并集**抓一次
  新闻/公告，再按账户切片（每条记录独立副本）；
* ``paper_candidate_scan.NewsSnapshot`` —— 判定"同一轮里能不能复用这份事实"：
  抓取成功 + 覆盖所需代码 + 未超过 TTL，三者缺一即重新抓取；
* ``paper_candidate_inputs.CandidateScanInputs.fundamentals`` —— 内容指纹先用
  相等短路，再做 JSON 序列化；key 语义不变（相等内容必然同 key）。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import paper_candidate_inputs as PCI  # noqa: E402
import paper_candidate_scan as Scan  # noqa: E402
import paper_trading as PT  # noqa: E402


def _paying_news(calls):
    def news(names):
        calls.append(dict(names))
        return [
            {"code": code, "name": name, "tone": -1, "time": "20261030093000"}
            for code, name in names.items()
        ]
    return news


class UnionNewsTests(unittest.TestCase):
    """一轮只请求一次，且各账户拿到的只能是自己的代码。"""

    def _ports(self, *, accounts_news=None):
        calls = []
        names_by_account = {
            "a": [{"code": "600001", "name": "甲"}, {"code": "600002", "name": "乙"}],
            "b": [{"code": "600002", "name": "乙"}, {"code": "600003", "name": "丙"}],
        }

        def cooled(conn, account_id, rows, day):
            return {"candidates": names_by_account[account_id]}

        ports = Scan.EvidencePorts(
            open_readonly=lambda: _NullContext(),
            cooled_candidates=cooled,
            news=accounts_news or _paying_news(calls),
            sector_flow=lambda key: [],
            quotes=lambda codes: {code: {"price": 10.0} for code in codes},
        )
        candidates = {account_id: ([], {}) for account_id in names_by_account}
        return ports, candidates, calls

    def test_news_is_fetched_once_for_the_union_of_accounts(self):
        ports, candidates, calls = self._ports()
        collected = Scan.collect_evidence([{"id": "a"}, {"id": "b"}], candidates,
                                          dt.date(2026, 10, 30), ports=ports)
        self.assertEqual(len(calls), 1, "两个账户只允许一次新闻请求")
        self.assertEqual(set(calls[0]), {"600001", "600002", "600003"})
        self.assertEqual(
            {row["code"] for row in collected.evidence_by_account["a"]["news"]},
            {"600001", "600002"},
        )
        self.assertEqual(
            {row["code"] for row in collected.evidence_by_account["b"]["news"]},
            {"600002", "600003"},
        )
        self.assertTrue(collected.news_snapshot.ok)
        self.assertEqual(collected.news_snapshot.source, "provider")

    def test_news_rows_are_isolated_between_accounts(self):
        ports, candidates, _ = self._ports()
        collected = Scan.collect_evidence([{"id": "a"}, {"id": "b"}], candidates,
                                          dt.date(2026, 10, 30), ports=ports)
        shared_code = "600002"
        first = [row for row in collected.evidence_by_account["a"]["news"]
                 if row["code"] == shared_code][0]
        first["tone"] = 99
        second = [row for row in collected.evidence_by_account["b"]["news"]
                  if row["code"] == shared_code][0]
        self.assertEqual(second["tone"], -1, "账户之间不得共享可变新闻行")
        self.assertEqual(collected.news_snapshot.rows[0]["tone"], -1,
                         "快照本体也不得被消费者改写")

    def test_blocked_account_does_not_contribute_codes(self):
        ports, candidates, calls = self._ports()
        candidates["b"] = ([], {"blocked": True, "reason": "候选失败"})
        collected = Scan.collect_evidence([{"id": "a"}, {"id": "b"}], candidates,
                                          dt.date(2026, 10, 30), ports=ports)
        self.assertEqual(set(calls[0]), {"600001", "600002"})
        self.assertNotIn("b", collected.evidence_by_account)

    def test_news_failure_is_not_reported_as_no_news(self):
        def boom(names):
            raise TimeoutError("news provider down")
        ports, candidates, _ = self._ports(accounts_news=boom)
        collected = Scan.collect_evidence([{"id": "a"}, {"id": "b"}], candidates,
                                          dt.date(2026, 10, 30), ports=ports)
        self.assertFalse(collected.news_snapshot.ok)
        self.assertEqual(collected.news_snapshot.source, "failed")
        self.assertFalse(collected.news_snapshot.reusable_for(["600001"]),
                         "失败快照绝不能被视为可复用的空结果")
        for evidence in collected.evidence_by_account.values():
            self.assertEqual(evidence["news"], [])

    def test_uncoded_rows_are_a_defensive_case_not_a_production_path(self):
        """生产源恒写 code（快讯与公告两条路径，公告还按 universe 过滤）。

        这里用假 provider 造一条无代码记录，钉住"万一出现"时的分发语义：它会被
        分发给每个消费者（按代码取值的门禁自行过滤），绝不静默丢失。
        """
        def news(names):
            return [{"code": "600001", "tone": -1}, {"tone": 0, "summary": "全市场条目"}]

        ports = Scan.EvidencePorts(
            open_readonly=lambda: _NullContext(),
            cooled_candidates=lambda conn, account_id, rows, day: {
                "candidates": [{"code": "600001", "name": "甲"}]},
            news=news,
            sector_flow=lambda key: [],
            quotes=lambda codes: {},
        )
        collected = Scan.collect_evidence([{"id": "a"}, {"id": "b"}],
                                          {"a": ([], {}), "b": ([], {})},
                                          dt.date(2026, 10, 30), ports=ports)
        for account_id in ("a", "b"):
            rows = collected.evidence_by_account[account_id]["news"]
            self.assertIn("全市场条目", [row.get("summary") for row in rows],
                          "无代码记录必须对所有消费者可见，不能静默丢弃")

    def test_no_codes_means_no_provider_call(self):
        calls = []
        ports = Scan.EvidencePorts(
            open_readonly=lambda: _NullContext(),
            cooled_candidates=lambda conn, account_id, rows, day: {"candidates": []},
            news=_paying_news(calls),
            sector_flow=lambda key: [],
            quotes=lambda codes: {},
        )
        collected = Scan.collect_evidence([{"id": "a"}], {"a": ([], {})},
                                          dt.date(2026, 10, 30), ports=ports)
        self.assertEqual(calls, [])
        self.assertTrue(collected.news_snapshot.ok)
        self.assertEqual(collected.news_snapshot.source, "empty")


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class NewsSnapshotReuseTests(unittest.TestCase):
    """复用三条件：成功 + 覆盖 + 未超时。"""

    def _snapshot(self, **overrides):
        values = dict(rows=({"code": "600001"},), codes=frozenset({"600001", "600002"}),
                      observed_at=dt.datetime(2026, 10, 30, 9, 30, 0), ok=True,
                      source="provider")
        values.update(overrides)
        return Scan.NewsSnapshot(**values)

    def test_reusable_when_successful_covering_and_fresh(self):
        snapshot = self._snapshot()
        now = dt.datetime(2026, 10, 30, 9, 31, 0)
        self.assertTrue(snapshot.reusable_for(["600001"], now=now))
        self.assertTrue(snapshot.reusable_for(["600001", "600002"], now=now))

    def test_not_reusable_when_codes_are_not_covered(self):
        snapshot = self._snapshot()
        now = dt.datetime(2026, 10, 30, 9, 31, 0)
        self.assertFalse(snapshot.reusable_for(["600001", "600009"], now=now))

    def test_not_reusable_when_stale(self):
        snapshot = self._snapshot()
        stale_after = dt.timedelta(seconds=Scan.NEWS_SNAPSHOT_MAX_AGE_SECONDS + 1)
        self.assertFalse(snapshot.reusable_for(["600001"],
                                               now=snapshot.observed_at + stale_after))

    def test_not_reusable_after_a_failed_fetch(self):
        snapshot = self._snapshot(ok=False, source="failed")
        self.assertFalse(snapshot.reusable_for(["600001"],
                                               now=dt.datetime(2026, 10, 30, 9, 31, 0)))


class WaitlistReuseTests(unittest.TestCase):
    """执行阶段的等待池新闻：能复用就复用，条件不满足照旧请求。"""

    def _snapshot(self, *, ok=True, codes=("600001", "600002"), age=10):
        observed = dt.datetime.now() - dt.timedelta(seconds=age)
        return Scan.NewsSnapshot(
            rows=tuple({"code": code, "tone": -1} for code in codes),
            codes=frozenset(codes), observed_at=observed, ok=ok, source="provider",
        )

    def test_reuses_a_fresh_covering_snapshot_without_another_request(self):
        with mock.patch.object(PT, "_news_for") as fetch:
            rows = PT._waitlist_news({"600001": "甲"}, self._snapshot())
        fetch.assert_not_called()
        self.assertEqual([row["code"] for row in rows], ["600001"])

    def test_refetches_when_the_snapshot_does_not_cover_the_codes(self):
        with mock.patch.object(PT, "_news_for", return_value=[{"code": "600009"}]) as fetch:
            rows = PT._waitlist_news({"600009": "壬"}, self._snapshot())
        fetch.assert_called_once()
        self.assertEqual([row["code"] for row in rows], ["600009"])

    def test_refetches_after_a_failed_candidate_fetch(self):
        with mock.patch.object(PT, "_news_for", return_value=[{"code": "600001"}]) as fetch:
            PT._waitlist_news({"600001": "甲"}, self._snapshot(ok=False))
        fetch.assert_called_once()

    def test_refetches_when_stale(self):
        stale = self._snapshot(age=Scan.NEWS_SNAPSHOT_MAX_AGE_SECONDS + 5)
        with mock.patch.object(PT, "_news_for", return_value=[]) as fetch:
            PT._waitlist_news({"600001": "甲"}, stale)
        fetch.assert_called_once()

    def test_without_a_snapshot_behaviour_is_unchanged(self):
        with mock.patch.object(PT, "_news_for", return_value=[{"code": "600001"}]) as fetch:
            PT._waitlist_news({"600001": "甲"}, None)
        fetch.assert_called_once()

    def test_empty_waitlist_never_calls_the_provider(self):
        with mock.patch.object(PT, "_news_for") as fetch:
            self.assertEqual(PT._waitlist_news({}, self._snapshot()), [])
        fetch.assert_not_called()


class FundamentalsFingerprintTests(unittest.TestCase):
    """指纹仍是**逐调用内容哈希**：测得 10–13ms/900KiB，但短路不安全。

    曾经尝试"与上一次输入相等就复用 key"（相等比较约 0.6ms，比序列化快 20 倍），
    但调用方可以**原地修改**传进来的 universe/finance（``_candidate_rows`` 就会
    在 live overlay 阶段这么做），持有引用再比较会把"已变的内容"误判成"没变"，
    直接返回过期因子表。要安全地短路必须深拷贝整份快照，成本与序列化相当。
    因此本 PR 保留内容哈希，并用测试钉住"原地修改必须失效"。
    """

    def _inputs(self):
        return PCI.CandidateScanInputs("2026-10-30T10:00:00+08:00")

    def _data(self):
        universe = [{"code": f"{600000 + i:06d}", "name": str(i)} for i in range(50)]
        finance = {"data": {f"{600000 + i:06d}": {"pe": 10.0 + i, "pb": 1.0}
                            for i in range(50)}, "asof": "2026-10-29"}
        return universe, finance

    def test_identical_content_computes_once_and_returns_isolated_copies(self):
        inputs = self._inputs()
        universe, finance = self._data()
        with mock.patch.object(PCI.F, "compute_fundamental_factors",
                               wraps=PCI.F.compute_fundamental_factors) as compute:
            first = inputs.fundamentals(universe, finance, asof=dt.date(2026, 10, 30))
            second = inputs.fundamentals(list(universe), dict(finance),
                                         asof=dt.date(2026, 10, 30))
        self.assertEqual(compute.call_count, 1, "内容相同只计算一次")
        self.assertTrue(first.equals(second))
        second.iloc[0, second.columns.get_loc("name")] = "被改写"
        self.assertNotEqual(first.iloc[0]["name"], "被改写", "不得把缓存表直接交给调用方")

    def test_in_place_mutation_always_invalidates_the_cached_table(self):
        """调用方原地改输入时，内容哈希必须重新计算（这就是拒绝短路的理由）。"""
        inputs = self._inputs()
        universe, finance = self._data()
        with mock.patch.object(PCI.F, "compute_fundamental_factors",
                               wraps=PCI.F.compute_fundamental_factors) as compute:
            inputs.fundamentals(universe, finance, asof=dt.date(2026, 10, 30))
            universe[0]["pe"] = 99.0
            inputs.fundamentals(universe, finance, asof=dt.date(2026, 10, 30))
            finance["data"]["600000"]["pe"] = 77.0
            inputs.fundamentals(universe, finance, asof=dt.date(2026, 10, 30))
        self.assertEqual(compute.call_count, 3)

    def test_different_asof_never_shares_a_table(self):
        inputs = self._inputs()
        universe, finance = self._data()
        strict = inputs.fundamentals(universe, finance, asof=dt.date(2026, 10, 30))
        live = inputs.fundamentals(universe, finance, asof=None)
        self.assertFalse(strict.equals(live), "PIT 截止日不同必须是不同结果")


if __name__ == "__main__":
    unittest.main()
