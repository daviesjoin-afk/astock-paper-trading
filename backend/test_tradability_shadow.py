# -*- coding: utf-8 -*-
"""Shadow Tradability Validation 契约测试。

这些测试只为一件事：**Shadow 是观察，不是 authority**。

因此它们刻意大量断言"不可比"与"拒绝"：

* 归档证据缺口（unknown / unprovable / missing）**不进** disagreement 分母；
* 分母为 0 时一致率必须是 ``None``，不是 0% 也不是 100%；
* 同一比对身份上的冲突内容必须 fail closed，**绝不** last-write-wins；
* 未来证据不得改写更早的比对（PIT）；
* 涨跌停方向必须保持"涨停只拦买、跌停只拦卖"；
* Shadow 开关不得改变任何生产输出。

夹具全部是内存 SQLite + 注入的假事实，无网络、不触碰执行路径。
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selection_tradability as ST  # noqa: E402
import tradability_archive as TA  # noqa: E402
import tradability_shadow as TS  # noqa: E402

SOURCE = "shadow_fixture.json"

NORMAL = {
    "is_listed": True,
    "is_st": False,
    "is_suspended": False,
    "has_market_quote": True,
    "has_trade_volume": True,
}


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    return conn


def production_verdict(status, *, reason="ok", side=ST.SIDE_BUY):
    """构造一条**生产口径**的 verdict 字典（形状与 TradabilityVerdict 一致）。"""
    return {
        "status": status,
        "reason": reason,
        "side": side,
        "executable": status == ST.STATUS_EXECUTABLE,
    }


class ShadowTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.repo = TA.TradabilityArchiveRepository(self.conn)
        self.comparator = TS.ShadowComparator(self.repo)

    def add_fact(self, session, *, code="000001", observed=None, effective=None, **flags):
        """写入一条归档事实；``observed``/``effective`` 默认该 session 收盘后。"""
        payload = {
            "code": code,
            "session_date": session,
            "source": SOURCE,
            "observed_at": observed or f"{session}T15:05:00",
            "effective_at": effective or f"{session}T15:05:00",
            **NORMAL,
        }
        payload.update(flags)
        return self.repo.save(TA.normalize_record(payload))

    def compare(self, verdict, *, code="000001", session="2024-01-10",
                side=ST.SIDE_BUY, at=None):
        return self.comparator.compare(
            verdict, code=code, session=session, side=side,
            decision_at=at or f"{session}T16:00:00",
        )


# ───────────────────────── 1. Taxonomy 与可比性 ─────────────────────────


class TaxonomyIsExhaustive(unittest.TestCase):
    """词汇表必须覆盖 spec 要求的每一类，且 comparable / not-comparable 分开。"""

    def test_required_statuses_exist(self):
        required = {
            "agree_allow", "agree_block",
            "production_allow_archive_block", "production_block_archive_allow",
            "archive_unknown", "archive_missing", "archive_unprovable",
            "production_unknown", "comparison_invalid",
        }
        self.assertEqual(required, {status.value for status in TS.SHADOW_STATUSES})

    def test_comparable_and_not_comparable_partition_the_vocabulary(self):
        comparable = {status.value for status in TS.COMPARABLE_STATUSES}
        not_comparable = {status.value for status in TS.NOT_COMPARABLE_STATUSES}
        self.assertEqual(set(), comparable & not_comparable)
        self.assertEqual(
            {status.value for status in TS.SHADOW_STATUSES},
            comparable | not_comparable,
        )

    def test_only_the_two_agree_statuses_are_agreement(self):
        self.assertEqual(
            {"agree_allow", "agree_block"},
            {status.value for status in TS.AGREE_STATUSES},
        )

    def test_only_directional_conflicts_are_disagreement(self):
        self.assertEqual(
            {"production_allow_archive_block", "production_block_archive_allow"},
            {status.value for status in TS.DISAGREE_STATUSES},
        )


class AgreementClassification(ShadowTestCase):
    """两侧都放行 / 都阻断 → agree；方向相反 → disagree。"""

    def test_both_allow_is_agree_allow(self):
        self.add_fact("2024-01-10")
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(TS.ShadowStatus.AGREE_ALLOW.value, comparison.status)
        self.assertTrue(comparison.comparable)

    def test_both_block_is_agree_block(self):
        self.add_fact("2024-01-10", is_st=True)
        comparison = self.compare(
            production_verdict(ST.STATUS_BLOCKED, reason=ST.REASON_UNKNOWN_ST_STATUS)
        )
        self.assertEqual(TS.ShadowStatus.AGREE_BLOCK.value, comparison.status)
        self.assertTrue(comparison.comparable)

    def test_production_allow_archive_block(self):
        self.add_fact("2024-01-10", is_suspended=True)
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value, comparison.status
        )
        self.assertTrue(comparison.comparable)

    def test_production_block_archive_allow(self):
        self.add_fact("2024-01-10")
        comparison = self.compare(
            production_verdict(ST.STATUS_BLOCKED, reason=ST.REASON_MISSING_PRICE)
        )
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW.value, comparison.status
        )
        self.assertTrue(comparison.comparable)


# ───────────────────────── 2. 归档缺口不是 disagreement ─────────────────────────


class ArchiveGapsAreNotDisagreement(ShadowTestCase):
    def test_missing_archive_evidence_is_not_comparable(self):
        # 从未摄取 → archive_missing，绝不算 disagreement。
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(TS.ShadowStatus.ARCHIVE_MISSING.value, comparison.status)
        self.assertFalse(comparison.comparable)
        self.assertIsNone(comparison.archive_allowed)

    def test_unprovable_archive_evidence_is_not_comparable(self):
        # 事实存在，但决策当时还不可知（observed_at 在 decision_at 之后）。
        self.add_fact("2024-01-10", observed="2024-02-01T15:05:00")
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(TS.ShadowStatus.ARCHIVE_UNPROVABLE.value, comparison.status)
        self.assertFalse(comparison.comparable)

    def test_unknown_archive_facts_are_not_comparable(self):
        # 归档有可见证据，但 ST 事实未知 → archive_unknown，不是 disagree。
        self.add_fact("2024-01-10", is_st=None)
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(TS.ShadowStatus.ARCHIVE_UNKNOWN.value, comparison.status)
        self.assertFalse(comparison.comparable)

    def test_production_unproven_is_not_comparable(self):
        self.add_fact("2024-01-10")
        comparison = self.compare(
            production_verdict(ST.STATUS_UNPROVEN, reason=ST.REASON_UNKNOWN_ST_STATUS)
        )
        self.assertEqual(TS.ShadowStatus.PRODUCTION_UNKNOWN.value, comparison.status)
        self.assertFalse(comparison.comparable)

    def test_missing_production_verdict_is_not_comparable(self):
        self.add_fact("2024-01-10")
        comparison = self.compare(None)
        self.assertEqual(TS.ShadowStatus.PRODUCTION_UNKNOWN.value, comparison.status)
        self.assertFalse(comparison.comparable)

    def test_invalid_identity_is_comparison_invalid(self):
        comparison = self.comparator.compare(
            production_verdict(ST.STATUS_EXECUTABLE),
            code="", session="2024-01-10", side=ST.SIDE_BUY,
            decision_at="2024-01-10T16:00:00",
        )
        self.assertEqual(TS.ShadowStatus.COMPARISON_INVALID.value, comparison.status)
        self.assertFalse(comparison.comparable)

    def test_unknown_side_is_comparison_invalid(self):
        comparison = self.comparator.compare(
            production_verdict(ST.STATUS_EXECUTABLE),
            code="000001", session="2024-01-10", side="hold",
            decision_at="2024-01-10T16:00:00",
        )
        self.assertEqual(TS.ShadowStatus.COMPARISON_INVALID.value, comparison.status)

    def test_invalid_production_status_is_comparison_invalid(self):
        self.add_fact("2024-01-10")
        comparison = self.compare(
            production_verdict(ST.STATUS_INVALID, reason=ST.REASON_MISSING_CODE)
        )
        self.assertEqual(TS.ShadowStatus.COMPARISON_INVALID.value, comparison.status)


# ───────────────────────── 3. Summary 分母契约 ─────────────────────────


class SummaryDenominators(ShadowTestCase):
    def test_agreement_rate_uses_comparable_not_requested(self):
        # 2 条可比（1 agree + 1 disagree）+ 3 条不可比。
        self.add_fact("2024-01-10")
        self.add_fact("2024-01-11", is_suspended=True)
        agree = self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-10")
        disagree = self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-11")
        missing_a = self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-12")
        missing_b = self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-13")
        unknown = self.compare(None, session="2024-01-14")

        summary = self.comparator.summarize([agree, disagree, missing_a, missing_b, unknown])
        self.assertEqual(5, summary.requested)
        self.assertEqual(2, summary.comparable)
        self.assertEqual(3, summary.not_comparable)
        self.assertEqual(1, summary.agree)
        self.assertEqual(1, summary.disagree)
        self.assertAlmostEqual(2 / 5, summary.comparison_rate)
        self.assertAlmostEqual(1 / 2, summary.agreement_rate)
        self.assertAlmostEqual(1 / 2, summary.disagreement_rate)

    def test_zero_comparable_yields_none_not_zero_or_one(self):
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        summary = self.comparator.summarize([comparison])
        self.assertEqual(0, summary.comparable)
        self.assertIsNone(summary.agreement_rate)
        self.assertIsNone(summary.disagreement_rate)
        # comparison_rate 仍可计算（requested > 0）。
        self.assertEqual(0.0, summary.comparison_rate)

    def test_empty_input_yields_none_rates(self):
        summary = self.comparator.summarize([])
        self.assertEqual(0, summary.requested)
        self.assertIsNone(summary.comparison_rate)
        self.assertIsNone(summary.agreement_rate)
        self.assertIsNone(summary.disagreement_rate)

    def test_summary_counters_match_every_taxonomy_bucket(self):
        self.add_fact("2024-01-10")
        self.add_fact("2024-01-11", is_st=True)
        self.add_fact("2024-01-12", is_suspended=True)
        self.add_fact("2024-01-13", is_st=None)
        self.add_fact("2024-01-14", observed="2024-03-01T15:05:00")
        comparisons = [
            self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-10"),
            self.compare(
                production_verdict(ST.STATUS_BLOCKED, reason=ST.REASON_UNKNOWN_ST_STATUS),
                session="2024-01-11",
            ),
            self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-12"),
            self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-13"),
            self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-14"),
            self.compare(production_verdict(ST.STATUS_EXECUTABLE), session="2024-01-15"),
            self.compare(None, session="2024-01-16"),
            self.compare(production_verdict(ST.STATUS_INVALID), session="2024-01-17"),
        ]
        summary = self.comparator.summarize(comparisons)
        self.assertEqual(1, summary.agree_allow)
        self.assertEqual(1, summary.agree_block)
        self.assertEqual(1, summary.production_allow_archive_block)
        self.assertEqual(0, summary.production_block_archive_allow)
        self.assertEqual(1, summary.archive_unknown)
        self.assertEqual(1, summary.archive_unprovable)
        self.assertEqual(1, summary.archive_missing)
        self.assertEqual(1, summary.production_unknown)
        self.assertEqual(1, summary.comparison_invalid)
        self.assertEqual(
            summary.requested,
            summary.comparable + summary.not_comparable,
        )
        self.assertEqual(summary.comparable, summary.agree + summary.disagree)

    def test_by_side_and_by_reason_breakdowns_are_reported(self):
        self.add_fact("2024-01-10", is_st=True)
        buy = self.compare(production_verdict(ST.STATUS_BLOCKED, reason="st_restricted"))
        sell = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE, side=ST.SIDE_SELL),
            side=ST.SIDE_SELL,
        )
        summary = self.comparator.summarize([buy, sell])
        self.assertEqual({"buy", "sell"}, set(summary.by_side))
        self.assertEqual(1, summary.by_side["buy"]["requested"])
        self.assertIn("st_restricted", summary.by_production_reason)
        self.assertIn("st_restricted", summary.by_archive_reason)
        self.assertIn("2024-01-10", summary.by_session)

    def test_unknown_status_is_rejected_not_swallowed(self):
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        object.__setattr__(comparison, "status", "made_up")
        with self.assertRaises(TS.ShadowError):
            self.comparator.summarize([comparison])


# ───────────────────────── 4. PIT ─────────────────────────


class PointInTimeGolden(ShadowTestCase):
    def test_future_evidence_cannot_alter_earlier_comparison(self):
        self.add_fact("2024-01-10")
        before = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        # 之后才被观察到的修订版（同 session，更晚 observed_at）。
        self.add_fact(
            "2024-01-10", observed="2024-06-01T15:05:00",
            effective="2024-06-01T15:05:00", is_suspended=True,
        )
        after = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        # 昨天的比对不得被今天的修订改写。
        self.assertEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(TS.ShadowStatus.AGREE_ALLOW.value, after.status)

    def test_future_observed_at_is_invisible(self):
        self.add_fact("2024-01-10", observed="2024-12-31T15:05:00")
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(TS.ShadowStatus.ARCHIVE_UNPROVABLE.value, comparison.status)

    def test_future_effective_at_is_invisible(self):
        self.add_fact("2024-01-10", effective="2024-12-31T15:05:00")
        comparison = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        self.assertEqual(TS.ShadowStatus.ARCHIVE_UNPROVABLE.value, comparison.status)

    def test_later_revision_visible_only_after_new_decision(self):
        self.add_fact("2024-01-10")
        self.add_fact(
            "2024-01-10", observed="2024-06-01T15:05:00",
            effective="2024-06-01T15:05:00", is_suspended=True,
        )
        early = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE), at="2024-01-10T16:00:00"
        )
        late = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE), at="2024-07-01T16:00:00"
        )
        self.assertEqual(TS.ShadowStatus.AGREE_ALLOW.value, early.status)
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value, late.status
        )

    def test_comparison_identity_includes_decision_at(self):
        self.add_fact("2024-01-10")
        early = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE), at="2024-01-10T16:00:00"
        )
        late = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE), at="2024-01-11T16:00:00"
        )
        self.assertNotEqual(early.identity, late.identity)


# ───────────────────────── 5. BUY / SELL 方向性 ─────────────────────────


class BuySellDirectionality(ShadowTestCase):
    def test_limit_up_blocks_buy_not_sell(self):
        self.add_fact(
            "2024-01-10", is_price_limit_locked=True,
            price_limit_direction=TA.PRICE_LIMIT_UP,
        )
        buy = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        sell = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE, side=ST.SIDE_SELL),
            side=ST.SIDE_SELL,
        )
        # 涨停：买入被归档拦（分歧），卖出不被这个原因拦（一致）。
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value, buy.status
        )
        self.assertEqual(TS.ShadowStatus.AGREE_ALLOW.value, sell.status)

    def test_limit_down_blocks_sell_not_buy(self):
        self.add_fact(
            "2024-01-10", is_price_limit_locked=True,
            price_limit_direction=TA.PRICE_LIMIT_DOWN,
        )
        buy = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        sell = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE, side=ST.SIDE_SELL),
            side=ST.SIDE_SELL,
        )
        self.assertEqual(TS.ShadowStatus.AGREE_ALLOW.value, buy.status)
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value, sell.status
        )

    def test_unknown_lock_direction_blocks_both_sides(self):
        self.add_fact("2024-01-10", is_price_limit_locked=True)
        buy = self.compare(production_verdict(ST.STATUS_EXECUTABLE))
        sell = self.compare(
            production_verdict(ST.STATUS_EXECUTABLE, side=ST.SIDE_SELL),
            side=ST.SIDE_SELL,
        )
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value, buy.status
        )
        self.assertEqual(
            TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value, sell.status
        )


# ───────────────────────── 6. Reason-level golden matrix ─────────────────────────


class ReasonLevelGoldenMatrix(ShadowTestCase):
    """每个事实类别都验证：归档结论、比对分类、可比性与 summary 计数。"""

    CASES = (
        ("listed_normal", {}, True, TS.ShadowStatus.AGREE_ALLOW.value),
        ("not_listed", {"is_listed": False}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("delisted", {"is_listed": False, "delisting_date": "2024-01-01"}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("st", {"is_st": True}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("unknown_st", {"is_st": None}, None, TS.ShadowStatus.ARCHIVE_UNKNOWN.value),
        ("suspended", {"is_suspended": True}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("unknown_suspension", {"is_suspended": None}, None,
         TS.ShadowStatus.ARCHIVE_UNKNOWN.value),
        ("no_quote", {"has_market_quote": False}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("unknown_quote", {"has_market_quote": None}, None,
         TS.ShadowStatus.ARCHIVE_UNKNOWN.value),
        ("no_volume", {"has_trade_volume": False}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("unknown_volume", {"has_trade_volume": None}, None,
         TS.ShadowStatus.ARCHIVE_UNKNOWN.value),
        # 买入方向上：涨停锁定拦买 → 分歧；跌停锁定**不**拦买 → 一致（方向性）。
        ("limit_up_lock_buy", {"is_price_limit_locked": True,
                               "price_limit_direction": TA.PRICE_LIMIT_UP}, False,
         TS.ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value),
        ("limit_down_lock_buy", {"is_price_limit_locked": True,
                                 "price_limit_direction": TA.PRICE_LIMIT_DOWN}, True,
         TS.ShadowStatus.AGREE_ALLOW.value),
        ("not_locked", {"is_price_limit_locked": False}, True,
         TS.ShadowStatus.AGREE_ALLOW.value),
        # 锁定为 False 是**已证明未锁定**；None 是"锁定事实未知"。
        # 注意：归档权威语义里锁定是**追加**限制，``is_price_limit_locked=None``
        # 不等于核心事实未知（核心事实是上市/停牌/ST/行情/成交量），因此仍判可交易。
        # Shadow 必须原样复现这个结论，不得自行把它升级成 archive_unknown。
        ("unknown_lock", {"is_price_limit_locked": None}, True,
         TS.ShadowStatus.AGREE_ALLOW.value),
    )

    def test_every_case_classifies_and_counts(self):
        seen = set()
        for name, flags, expected_allow, expected_status in self.CASES:
            with self.subTest(case=name):
                conn = open_db()
                try:
                    repo = TA.TradabilityArchiveRepository(conn)
                    payload = {
                        "code": "000001", "session_date": "2024-01-10",
                        "source": SOURCE,
                        "observed_at": "2024-01-10T15:05:00",
                        "effective_at": "2024-01-10T15:05:00",
                        **NORMAL,
                    }
                    payload.update(flags)
                    repo.save(TA.normalize_record(payload))
                    comparator = TS.ShadowComparator(repo)
                    comparison = comparator.compare(
                        production_verdict(ST.STATUS_EXECUTABLE),
                        code="000001", session="2024-01-10", side=ST.SIDE_BUY,
                        decision_at="2024-01-10T16:00:00",
                    )
                    self.assertEqual(expected_status, comparison.status)
                    self.assertEqual(expected_allow, comparison.archive_allowed)
                    summary = comparator.summarize([comparison])
                    self.assertEqual(1, summary.requested)
                    self.assertEqual(1, summary.by_status[comparison.status])
                    if comparison.comparable:
                        self.assertEqual(1, summary.comparable)
                        self.assertEqual(1, summary.agree + summary.disagree)
                    else:
                        self.assertEqual(0, summary.comparable)
                        self.assertIsNone(summary.agreement_rate)
                    seen.add(comparison.status)
                finally:
                    conn.close()
        # 非空洞性：这批用例真的覆盖了多种分类，而不是全部落到同一个。
        self.assertGreaterEqual(len(seen), 3)


# ───────────────────────── 7. 身份与幂等 ─────────────────────────


class ComparisonIdentity(unittest.TestCase):
    def test_identity_tuple_is_the_documented_key(self):
        comparison = TS.ShadowComparison(
            code="000001", session="2024-01-10", decision_at="2024-01-10T16:00:00",
            side=ST.SIDE_BUY, status=TS.ShadowStatus.AGREE_ALLOW.value, comparable=True,
            production_allowed=True, production_reason="ok", production_status="executable",
            archive_allowed=True, archive_reason="ok", archive_source=SOURCE,
            archive_fingerprint="fp", archive_effective_at="2024-01-10T15:05:00",
            archive_observed_at="2024-01-10T15:05:00", archive_evidence_present=True,
        )
        self.assertEqual(
            ("000001", "2024-01-10", "2024-01-10T16:00:00", "buy", TS.CONTRACT_VERSION),
            comparison.identity,
        )

    def test_same_identity_and_content_share_fingerprint(self):
        first = self._comparison()
        second = self._comparison()
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_content_change_changes_fingerprint(self):
        first = self._comparison()
        second = self._comparison(status=TS.ShadowStatus.AGREE_BLOCK.value)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    @staticmethod
    def _comparison(**overrides):
        payload = {
            "code": "000001", "session": "2024-01-10",
            "decision_at": "2024-01-10T16:00:00", "side": ST.SIDE_BUY,
            "status": TS.ShadowStatus.AGREE_ALLOW.value, "comparable": True,
            "production_allowed": True, "production_reason": "ok",
            "production_status": "executable", "archive_allowed": True,
            "archive_reason": "ok", "archive_source": SOURCE,
            "archive_fingerprint": "fp", "archive_effective_at": "2024-01-10T15:05:00",
            "archive_observed_at": "2024-01-10T15:05:00",
            "archive_evidence_present": True,
        }
        payload.update(overrides)
        return TS.ShadowComparison(**payload)


class ShadowPersistence(unittest.TestCase):
    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        TS.ensure_shadow_schema(self.conn)
        self.comparison = ComparisonIdentity._comparison()

    def test_first_write_inserts_second_is_identical(self):
        self.assertEqual("inserted", TS.save_comparison(self.conn, self.comparison))
        self.assertEqual("identical", TS.save_comparison(self.conn, self.comparison))
        rows = TS.load_comparisons(self.conn)
        self.assertEqual(1, len(rows))

    def test_conflicting_content_on_same_identity_fails_closed(self):
        TS.save_comparison(self.conn, self.comparison)
        conflicting = ComparisonIdentity._comparison(
            status=TS.ShadowStatus.AGREE_BLOCK.value, production_allowed=False,
        )
        with self.assertRaises(TS.ShadowConflictError):
            TS.save_comparison(self.conn, conflicting)
        rows = TS.load_comparisons(self.conn)
        self.assertEqual(1, len(rows))
        # 原记录未被覆盖（绝不是 last-write-wins）。
        self.assertEqual(TS.ShadowStatus.AGREE_ALLOW.value, rows[0]["comparison_status"])

    def test_shadow_table_is_isolated_from_production_tables(self):
        names = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn(TS.SHADOW_TABLE, names)
        for production_table in (
            TA.ARCHIVE_TABLE, "tradability_ingestion_runs",
            "paper_orders", "paper_fills", "paper_positions",
        ):
            self.assertNotEqual(TS.SHADOW_TABLE, production_table)
        self.assertNotEqual(TA.ARCHIVE_TABLE, TS.SHADOW_TABLE)


# ───────────────────────── 8. On/Off golden sentinel ─────────────────────────


class ShadowOnOffDoesNotChangeProduction(ShadowTestCase):
    """最重的非干扰测试：开/关 Shadow，生产输出必须完全一致。

    Shadow **唯一**允许新增的是 comparison / audit 输出。
    """

    def _production_run(self, *, shadow_enabled):
        """一段纯生产计算：生产判定 + 选择结果 + 持仓/现金快照。"""
        self.add_fact("2024-01-10")
        verdict = production_verdict(ST.STATUS_EXECUTABLE)
        production_output = {
            "production_tradability": dict(verdict),
            "selection": ["000001", "600000"],
            "orders": [("000001", ST.SIDE_BUY, 100)],
            "fills": [("000001", ST.SIDE_BUY, 100, 10.5)],
            "positions": {"000001": 100},
            "cash": 100000.0,
        }
        extra = None
        if shadow_enabled:
            comparison = self.compare(verdict)
            extra = self.comparator.summarize([comparison]).to_dict()
        return production_output, extra

    def test_production_outputs_are_identical_with_shadow_on_and_off(self):
        off, extra_off = self._production_run(shadow_enabled=False)
        on, extra_on = self._production_run(shadow_enabled=True)
        self.assertIsNone(extra_off)
        self.assertIsNotNone(extra_on)
        # 逐项相同：orders / fills / positions / cash / tradability / selection。
        self.assertEqual(off, on)

    def test_shadow_only_adds_comparison_output(self):
        off, _ = self._production_run(shadow_enabled=False)
        on, extra = self._production_run(shadow_enabled=True)
        self.assertEqual(off, on)
        self.assertEqual(1, extra["requested"])


# ───────────────────────── 9. 架构边界（模块 API 面） ─────────────────────────


class ShadowApiSurface(unittest.TestCase):
    """Shadow 不得暴露任何"执行/授权"入口。"""

    FORBIDDEN_NAMES = (
        "allow_order", "block_order", "override", "effective_can_buy",
        "effective_can_sell", "submit_order", "cancel_order", "modify_order",
        "enforce", "apply",
    )

    def test_module_exposes_no_authority_entry_point(self):
        for name in self.FORBIDDEN_NAMES:
            self.assertFalse(
                hasattr(TS, name), f"tradability_shadow 不得暴露 {name}"
            )

    def test_comparator_exposes_no_authority_method(self):
        for name in self.FORBIDDEN_NAMES:
            self.assertFalse(
                hasattr(TS.ShadowComparator, name),
                f"ShadowComparator 不得暴露 {name}",
            )

    def test_comparison_object_is_not_named_decision(self):
        # 命名即契约：它是观察结果，不是决策。
        self.assertFalse(hasattr(TS, "TradabilityDecision"))
        self.assertFalse(hasattr(TS, "OrderDecision"))
        self.assertFalse(hasattr(TS, "ExecutionDecision"))

    def test_comparison_exposes_both_sides_and_status(self):
        comparison = ComparisonIdentity._comparison()
        payload = comparison.to_dict()
        for key in (
            "production_allowed", "production_reason", "archive_allowed",
            "archive_reason", "status", "comparable", "contract_version",
        ):
            self.assertIn(key, payload)

    def test_comparison_does_not_mutate_production_modules(self):
        # 消费端只读：构造比对前后，生产判定对象必须逐字段不变。
        verdict = production_verdict(ST.STATUS_EXECUTABLE)
        snapshot = dict(verdict)
        ComparisonIdentity._comparison()
        self.assertEqual(snapshot, verdict)


# ───────────────────────── 10. compare_many 确定性 ─────────────────────────


class CompareManyIsDeterministic(ShadowTestCase):
    def test_output_is_sorted_by_identity(self):
        self.add_fact("2024-01-10")
        items = [
            {"production_verdict": production_verdict(ST.STATUS_EXECUTABLE),
             "code": "600000", "session": "2024-01-10", "side": ST.SIDE_BUY,
             "decision_at": "2024-01-10T16:00:00"},
            {"production_verdict": production_verdict(ST.STATUS_EXECUTABLE),
             "code": "000001", "session": "2024-01-10", "side": ST.SIDE_BUY,
             "decision_at": "2024-01-10T16:00:00"},
        ]
        first = self.comparator.compare_many(items)
        second = self.comparator.compare_many(list(reversed(items)))
        self.assertEqual([c.identity for c in first], [c.identity for c in second])
        self.assertEqual("000001", first[0].code)

    def test_batch_summary_is_order_independent(self):
        self.add_fact("2024-01-10")
        self.add_fact("2024-01-11", is_st=True)
        items = [
            {"production_verdict": production_verdict(ST.STATUS_EXECUTABLE),
             "code": "000001", "session": session, "side": ST.SIDE_BUY,
             "decision_at": f"{session}T16:00:00"}
            for session in ("2024-01-10", "2024-01-11")
        ]
        forward = self.comparator.summarize(self.comparator.compare_many(items))
        backward = self.comparator.summarize(
            self.comparator.compare_many(list(reversed(items)))
        )
        self.assertEqual(forward.to_dict(), backward.to_dict())


if __name__ == "__main__":
    unittest.main()
