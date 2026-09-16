# -*- coding: utf-8 -*-
"""历史可交易性事实资产层契约测试。

这些测试存在只为一件事：**证明"股票在数据库里存在"不等于"当时能交易"**。

因此它们刻意大量断言**拒绝**与**未知**：没有证据 → 阻断；证据不足 → 阻断；
只有**当时可见**的证据算数。一个把"不知道"当成"允许"的归档层，比没有归档层更糟，
因为它会让历史回测看起来有证据支撑。

夹具全部是内存 SQLite，无网络、无 provider 调用、不触碰任何执行路径。
"""

import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tradability_archive as TA  # noqa: E402

SOURCE = "historical_tradability_fixture.json"


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    return conn


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = open_db()
        self.addCleanup(self.conn.close)
        self.repo = TA.TradabilityArchiveRepository(self.conn)

    # 基准事实：一只正常可交易的已上市普通股。任何用例都可以用关键字覆盖，
    # 需要"未知"的用例请**显式**传 None —— 未知是一项要主动声明的输入，
    # 不是夹具的默认值。
    NORMAL = {
        "is_listed": True,
        "is_st": False,
        "is_suspended": False,
        "has_market_quote": True,
        "has_trade_volume": True,
    }

    def add(self, session, *, code="000001", observed=None, effective=None, **flags):
        """写入一条事实。``observed``/``effective`` 默认取该 session 收盘后。"""
        payload = {
            "code": code,
            "session_date": session,
            "source": SOURCE,
            "observed_at": observed or f"{session}T15:05:00",
            "effective_at": effective or f"{session}T15:05:00",
            **self.NORMAL,
        }
        payload.update(flags)
        return self.repo.save(TA.normalize_record(payload))

    def decide(self, session, *, code="000001", at=None):
        return TA.tradability_at(
            code, session,
            decision_time=at or f"{session}T16:00:00",
            repository=self.repo,
        )


# ───────────────────────────── PIT 语义 ─────────────────────────────


class PointInTimeIsEnforced(ArchiveTestCase):
    def test_future_observation_does_not_explain_the_past(self):
        """2025-03 的查询不得看到 2025-06 才知道的 ST。

        关键在 effective_at 必须**早于**决策时间：这条 ST 声称 2025-02-01 就已生效，
        所以"当时是否已经观察到"是唯一能挡住它的闸门。若把 effective_at 设到决策
        时间之后，两条闸门同时生效，测试就退化成一个抓不住 observed_at 缺陷的用例。
        """
        self.add("2025-01-10")
        # 这条 ST 证据直到 2025-06-10 才被观察到，但它声称 2025-02-01 就已生效。
        self.add("2025-01-10", is_st=True,
                 observed="2025-06-10T15:05:00", effective="2025-02-01T00:00:00")

        march = self.decide("2025-01-10", at="2025-03-01T09:00:00")
        self.assertEqual(TA.TradabilityReason.OK, march.buy_block_reason)
        self.assertTrue(march.can_buy)
        self.assertEqual(SOURCE, march.source)

        july = self.decide("2025-01-10", at="2025-07-01T09:00:00")
        self.assertEqual(TA.TradabilityReason.ST_RESTRICTED, july.buy_block_reason)
        self.assertFalse(july.can_buy)

    def test_later_observation_of_a_normal_state_does_not_retro_clear_st(self):
        """反向污染同样禁止：今天看到"已摘帽"，不能改写当时是 ST 的事实。"""
        self.add("2025-06-10", is_st=True)
        self.add("2025-06-10", is_st=False, observed="2025-09-01T15:05:00",
                 effective="2025-08-01T00:00:00")

        back = self.decide("2025-06-10", at="2025-06-11T09:00:00")
        self.assertEqual(TA.TradabilityReason.ST_RESTRICTED, back.buy_block_reason)

        later = self.decide("2025-06-10", at="2025-09-02T09:00:00")
        self.assertEqual(TA.TradabilityReason.OK, later.buy_block_reason)

    def test_evidence_effective_in_the_future_is_not_yet_visible(self):
        """已观察到、但生效时点还在未来 → 当时不算生效。"""
        self.add("2025-03-10", is_st=False,
                 observed="2025-03-10T15:05:00", effective="2025-12-01T00:00:00")
        # 该 session 只有这一条，且它当时尚未生效 → 没有可见记录。
        decision = self.decide("2025-03-10", at="2025-03-11T09:00:00")
        self.assertFalse(decision.evidence_present)
        self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, decision.buy_block_reason)

        after = self.decide("2025-03-10", at="2025-12-02T09:00:00")
        self.assertTrue(after.evidence_present)
        self.assertEqual(TA.TradabilityReason.OK, after.buy_block_reason)

    def test_unparseable_decision_time_fails_closed(self):
        self.add("2025-03-10")
        for bad in (None, "", "not-a-time"):
            decision = TA.tradability_at(
                "000001", "2025-03-10", decision_time=bad, repository=self.repo
            )
            self.assertFalse(decision.can_buy, bad)
            self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, decision.buy_block_reason)

    def test_latest_effective_evidence_wins_within_one_session(self):
        """同一 session 可有多条状态；当时取**最新生效**的那条。"""
        self.add("2025-05-05", is_suspended=True,
                 observed="2025-05-05T09:35:00", effective="2025-05-05T09:30:00")
        self.add("2025-05-05", is_suspended=False,
                 observed="2025-05-05T14:35:00", effective="2025-05-05T14:30:00")

        morning = self.decide("2025-05-05", at="2025-05-05T10:00:00")
        self.assertEqual(TA.TradabilityReason.SUSPENDED, morning.buy_block_reason)

        afternoon = self.decide("2025-05-05", at="2025-05-05T15:00:00")
        self.assertEqual(TA.TradabilityReason.OK, afternoon.buy_block_reason)


# ───────────────────────────── 停牌 ─────────────────────────────


class SuspensionIsRespected(ArchiveTestCase):
    def test_suspended_inside_the_window_and_tradable_after(self):
        for day in ("2025-05-01", "2025-05-05", "2025-05-09"):
            self.add(day, is_suspended=True)
        for day in ("2025-05-20",):
            self.add(day, is_suspended=False)

        suspended = self.decide("2025-05-05")
        self.assertTrue(suspended.evidence_present)
        self.assertEqual(TA.TradabilityReason.SUSPENDED, suspended.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.SUSPENDED, suspended.sell_block_reason)
        self.assertFalse(suspended.can_buy)
        self.assertFalse(suspended.can_sell)

        resumed = self.decide("2025-05-20")
        self.assertEqual(TA.TradabilityReason.OK, resumed.buy_block_reason)
        self.assertTrue(resumed.can_buy)
        self.assertTrue(resumed.can_sell)

    def test_suspension_reason_is_carried_into_the_decision(self):
        self.add("2025-05-05", is_suspended=True, suspension_reason="重大资产重组")
        decision = self.decide("2025-05-05")
        self.assertEqual("重大资产重组", decision.suspension_reason)
        self.assertEqual("重大资产重组", decision.to_dict()["suspension_reason"])

    def test_unknown_suspension_is_not_treated_as_tradable(self):
        """缺失 ≠ 未停牌。"""
        self.add("2025-05-05", is_suspended=None)
        decision = self.decide("2025-05-05")
        self.assertFalse(decision.can_buy)
        self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, decision.buy_block_reason)


# ───────────────────────────── 上市 / 退市 ─────────────────────────────


class ListingLifecycleIsRespected(ArchiveTestCase):
    def test_before_listing_is_not_listed(self):
        self.add("2025-06-01", is_listed=None, listing_date="2025-06-10")
        decision = self.decide("2025-06-01")
        self.assertEqual(TA.TradabilityReason.NOT_LISTED, decision.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.NOT_LISTED, decision.sell_block_reason)

    def test_on_and_after_listing_is_tradable(self):
        self.add("2025-06-11", is_listed=None, listing_date="2025-06-10")
        decision = self.decide("2025-06-11")
        self.assertEqual(TA.TradabilityReason.OK, decision.buy_block_reason)
        self.assertTrue(decision.can_buy)

    def test_listing_date_boundary_is_inclusive(self):
        """上市当日算上市（``session >= listing_date``）。"""
        self.add("2025-06-10", is_listed=None, listing_date="2025-06-10")
        self.assertEqual(TA.TradabilityReason.OK, self.decide("2025-06-10").buy_block_reason)
        # 前一天则否
        self.add("2025-06-09", is_listed=None, listing_date="2025-06-10")
        self.assertEqual(
            TA.TradabilityReason.NOT_LISTED, self.decide("2025-06-09").buy_block_reason
        )

    def test_delisted_date_is_no_longer_listed(self):
        self.add("2025-09-20", is_listed=None, listing_date="2015-01-01",
                 delisting_date="2025-09-15", has_market_quote=False,
                 has_trade_volume=False)
        decision = self.decide("2025-09-20")
        self.assertEqual(TA.TradabilityReason.DELISTED, decision.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.DELISTED, decision.sell_block_reason)

    def test_delisting_boundary_is_exclusive(self):
        """退市当日已不在上市区间内（``[listing_date, delisting_date)``）。"""
        self.add("2025-09-15", is_listed=None, listing_date="2015-01-01",
                 delisting_date="2025-09-15", has_market_quote=False,
                 has_trade_volume=False)
        self.assertEqual(TA.TradabilityReason.DELISTED, self.decide("2025-09-15").buy_block_reason)

    def test_unknown_listing_state_does_not_default_to_tradable(self):
        """两个日期都不知道，又没有显式 is_listed → 未知，不是"默认上市"。"""
        self.add("2025-03-10", is_listed=None)
        decision = self.decide("2025-03-10")
        self.assertFalse(decision.can_buy)
        self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, decision.buy_block_reason)


# ───────────────────────────── 行情 / 成交 ─────────────────────────────


class QuoteAndVolumeExistence(ArchiveTestCase):
    def test_quote_missing_blocks_buy_and_sell(self):
        self.add("2025-03-10", is_listed=True, is_st=False, is_suspended=False,
                 has_market_quote=False, has_trade_volume=False)
        decision = self.decide("2025-03-10")
        self.assertEqual(TA.TradabilityReason.NO_QUOTE, decision.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.NO_QUOTE, decision.sell_block_reason)

    def test_zero_volume_blocks_buy_but_not_sell(self):
        """没有成交不再支持"能买"；卖出并不要求当日有成交量。"""
        self.add("2025-03-10", is_listed=True, is_st=False, is_suspended=False,
                 has_market_quote=True, has_trade_volume=False)
        decision = self.decide("2025-03-10")
        self.assertEqual(TA.TradabilityReason.NO_VOLUME, decision.buy_block_reason)
        self.assertTrue(decision.can_sell)

    def test_existence_alone_is_not_tradability(self):
        """核心断言：数据库里有这只票，不代表当时能交易。"""
        self.add("2025-03-10", is_listed=True, has_market_quote=False,
                 has_trade_volume=False)
        decision = self.decide("2025-03-10")
        self.assertFalse(decision.can_buy)
        self.assertFalse(decision.can_sell)


# ───────────────────────────── 涨跌停 ─────────────────────────────


class PriceLimitLock(ArchiveTestCase):
    def test_limit_up_blocks_buy_but_leaves_sell_executable(self):
        """涨停只拦买 —— 卖出仍然可成交（仓库权威口径 test_p5）。"""
        self.add("2025-03-10", is_listed=True, is_st=False, is_suspended=False,
                 has_market_quote=True, has_trade_volume=True,
                 is_price_limit_locked=True, price_limit_direction="up")
        decision = self.decide("2025-03-10")
        self.assertFalse(decision.can_buy)
        self.assertEqual(TA.TradabilityReason.BUY_LIMIT_LOCKED, decision.buy_block_reason)
        self.assertTrue(decision.can_sell)
        self.assertEqual(TA.TradabilityReason.OK, decision.sell_block_reason)

    def test_limit_down_blocks_sell_but_leaves_buy_executable(self):
        """跌停只拦卖 —— 买入仍然可成交。"""
        self.add("2025-03-10", is_listed=True, is_st=False, is_suspended=False,
                 has_market_quote=True, has_trade_volume=True,
                 is_price_limit_locked=True, price_limit_direction="down")
        decision = self.decide("2025-03-10")
        self.assertTrue(decision.can_buy)
        self.assertEqual(TA.TradabilityReason.OK, decision.buy_block_reason)
        self.assertFalse(decision.can_sell)
        self.assertEqual(TA.TradabilityReason.SELL_LIMIT_LOCKED, decision.sell_block_reason)

    def test_directionality_cannot_be_swapped(self):
        """golden：涨停与跌停的拦截侧必须相反，绝不一刀切。"""
        self.add("2025-03-10", is_price_limit_locked=True, price_limit_direction="up")
        up = self.decide("2025-03-10")
        self.add("2025-03-11", is_price_limit_locked=True, price_limit_direction="down")
        down = self.decide("2025-03-11")

        self.assertFalse(up.can_buy)
        self.assertTrue(up.can_sell)
        self.assertTrue(down.can_buy)
        self.assertFalse(down.can_sell)
        # 两侧被拦的 reason 必须不同——不允许合并成一个 "not tradable"。
        self.assertNotEqual(up.buy_block_reason, down.sell_block_reason)

    def test_locked_with_unknown_direction_blocks_both_sides(self):
        """锁定事实已证、方向未知 → fail closed，两侧都拦且原因各自方向性。"""
        self.add("2025-03-10", is_price_limit_locked=True, price_limit_direction=None)
        decision = self.decide("2025-03-10")
        self.assertFalse(decision.can_buy)
        self.assertFalse(decision.can_sell)
        self.assertEqual(TA.TradabilityReason.BUY_LIMIT_LOCKED, decision.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.SELL_LIMIT_LOCKED, decision.sell_block_reason)

    def test_direction_aliases_are_normalized(self):
        """上游的各种方向写法收敛到两个常量；无法识别的保持未知。"""
        for raw, expected in (("UP", "up"), ("limit_up", "up"), ("涨停", "up"),
                              ("Down", "down"), ("limit-down", None),
                              ("跌停", "down"), ("", None), (None, None)):
            self.assertEqual(expected, TA._direction(raw), raw)

    def test_not_locked_is_normal(self):
        self.add("2025-03-10", is_listed=True, is_st=False, is_suspended=False,
                 has_market_quote=True, has_trade_volume=True,
                 is_price_limit_locked=False)
        decision = self.decide("2025-03-10")
        self.assertTrue(decision.can_buy)
        self.assertEqual(TA.TradabilityReason.OK, decision.buy_block_reason)


# ───────────────────────────── 未知状态 ─────────────────────────────


class UnknownFailsClosed(ArchiveTestCase):
    def test_no_record_at_all_is_unknown_and_blocks_both_ways(self):
        decision = self.decide("2025-03-10")
        self.assertFalse(decision.can_buy)
        self.assertFalse(decision.can_sell)
        self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, decision.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, decision.sell_block_reason)
        self.assertFalse(decision.evidence_present)
        self.assertIsNone(decision.fingerprint)

    def test_unknown_reason_is_never_ok(self):
        decision = self.decide("2025-03-10")
        self.assertNotEqual(TA.TradabilityReason.OK, decision.buy_block_reason)
        self.assertNotEqual(TA.TradabilityReason.OK, decision.sell_block_reason)

    def test_unknown_decision_still_carries_audit_fields(self):
        payload = self.decide("2025-03-10").to_dict()
        for key in ("source", "effective_at", "observed_at", "fingerprint",
                    "evidence_present", "contract_version"):
            self.assertIn(key, payload)
        self.assertIsNone(payload["source"])
        self.assertEqual(TA.CONTRACT_VERSION, payload["contract_version"])

    def test_malformed_lookup_arguments_are_unknown_not_permitted(self):
        self.add("2025-03-10")
        for code, session in (("", "2025-03-10"), ("000001", ""), (None, None)):
            decision = TA.tradability_at(
                code, session, decision_time="2025-03-11T09:00:00", repository=self.repo
            )
            self.assertFalse(decision.can_buy, (code, session))
            self.assertEqual(
                TA.TradabilityReason.UNKNOWN_STATE, decision.buy_block_reason
            )


# ───────────────────────────── 审计链 ─────────────────────────────


class AuditTrail(ArchiveTestCase):
    def test_every_decision_carries_source_time_and_fingerprint(self):
        self.add("2025-03-10")
        decision = self.decide("2025-03-10")
        self.assertEqual(SOURCE, decision.source)
        self.assertTrue(decision.effective_at)
        self.assertTrue(decision.observed_at)
        self.assertEqual(64, len(decision.fingerprint or ""))

    def test_fingerprint_is_content_addressed_and_stable(self):
        self.add("2025-03-10")
        first = self.decide("2025-03-10").fingerprint
        again = self.decide("2025-03-10").fingerprint
        self.assertEqual(first, again)
        self.repo.clear_cache()
        self.assertEqual(first, self.decide("2025-03-10").fingerprint)

    def test_different_facts_have_different_fingerprints(self):
        self.add("2025-03-10")
        plain = self.decide("2025-03-10").fingerprint
        self.add("2025-03-11", is_st=True)
        flagged = self.decide("2025-03-11").fingerprint
        self.assertNotEqual(plain, flagged)

    def test_reason_enum_is_exhaustive(self):
        """所有阻断原因必须来自枚举，禁止散落字符串。"""
        expected = {
            "OK", "NOT_LISTED", "DELISTED", "ST_RESTRICTED", "SUSPENDED",
            "NO_QUOTE", "NO_VOLUME", "BUY_LIMIT_LOCKED", "SELL_LIMIT_LOCKED",
            "UNKNOWN_STATE",
        }
        self.assertEqual(expected, {member.name for member in TA.TradabilityReason})
        # 每个决策的原因都必须是枚举成员（而不是裸字符串）
        self.add("2025-03-10")
        decision = self.decide("2025-03-10")
        self.assertIsInstance(decision.buy_block_reason, TA.TradabilityReason)
        self.assertIsInstance(decision.sell_block_reason, TA.TradabilityReason)


# ───────────────────────────── 证据层与判断层分离 ─────────────────────────────


class EvidenceAndDecisionAreSeparateLayers(ArchiveTestCase):
    def test_evidence_has_no_tradability_judgement_fields(self):
        """历史事实不随未来规则变化：事实层不得含 can_buy / can_sell。"""
        fields = set(TA.TradabilityEvidence.__dataclass_fields__)
        self.assertNotIn("can_buy", fields)
        self.assertNotIn("can_sell", fields)
        self.assertNotIn("buy_block_reason", fields)

    def test_decision_layer_holds_the_judgement(self):
        fields = set(TA.TradabilityDecision.__dataclass_fields__)
        for key in ("can_buy", "can_sell", "buy_block_reason", "sell_block_reason"):
            self.assertIn(key, fields)

    def test_st_history_comes_from_evidence_not_from_the_name(self):
        """ST 必须来自带 effective_at 的证据，而不是当前名称推断。"""
        self.add("2025-01-10", is_st=False)
        self.add("2025-06-10", is_st=True)
        self.add("2025-09-10", is_st=False)

        self.assertEqual(TA.TradabilityReason.OK, self.decide("2025-01-10").buy_block_reason)
        self.assertEqual(
            TA.TradabilityReason.ST_RESTRICTED, self.decide("2025-06-10").buy_block_reason
        )
        self.assertEqual(TA.TradabilityReason.OK, self.decide("2025-09-10").buy_block_reason)

    def test_st_blocks_sell_too(self):
        self.add("2025-06-10", is_st=True, has_market_quote=True, has_trade_volume=True)
        decision = self.decide("2025-06-10")
        self.assertEqual(TA.TradabilityReason.ST_RESTRICTED, decision.buy_block_reason)
        self.assertTrue(decision.can_sell)


# ───────────────────────────── 规范化与来源 ─────────────────────────────


class NormalizationIsStrict(unittest.TestCase):
    BASE = {
        "code": "000001",
        "session_date": "2025-03-10",
        "is_listed": True,
        "is_st": False,
        "is_suspended": False,
        "has_market_quote": True,
        "has_trade_volume": True,
        "source": SOURCE,
        "observed_at": "2025-03-10T15:05:00",
        "effective_at": "2025-03-10T15:05:00",
    }

    def normalize(self, **overrides):
        payload = dict(self.BASE)
        payload.update(overrides)
        return TA.normalize_record(payload)

    def test_missing_source_is_refused(self):
        with self.assertRaises(TA.TradabilityArchiveError):
            self.normalize(source=None)

    def test_missing_observed_at_is_refused(self):
        with self.assertRaises(TA.TradabilityArchiveError):
            self.normalize(observed_at=None)

    def test_missing_effective_at_is_refused(self):
        with self.assertRaises(TA.TradabilityArchiveError):
            self.normalize(effective_at=None)

    def test_missing_code_is_refused(self):
        with self.assertRaises(TA.TradabilityArchiveError):
            self.normalize(code=None)

    def test_invalid_session_is_refused(self):
        with self.assertRaises(TA.TradabilityArchiveError):
            self.normalize(session_date="2025-13-45")

    def test_invalid_timestamp_is_refused(self):
        with self.assertRaises(TA.TradabilityArchiveError):
            self.normalize(observed_at="not-a-time")

    def test_strict_boolean_rejects_non_zero_one_numerics(self):
        """``2`` / ``-1`` / ``0.5`` 不是布尔，必须是未知而不是"真"。"""
        for bad in (2, -1, 0.5, float("inf"), float("nan")):
            evidence = self.normalize(is_st=bad)
            self.assertIsNone(evidence.is_st, bad)

    def test_strict_boolean_accepts_exact_zero_and_one(self):
        self.assertIs(True, self.normalize(is_st=1).is_st)
        self.assertIs(False, self.normalize(is_st=0).is_st)
        self.assertIs(True, self.normalize(is_st=True).is_st)

    def test_unknown_booleans_are_none_not_false(self):
        evidence = TA.normalize_record({
            "code": "000001", "session_date": "2025-03-10", "source": SOURCE,
            "observed_at": "2025-03-10T15:05:00", "effective_at": "2025-03-10T15:05:00",
        })
        self.assertIsNone(evidence.is_st)
        self.assertIsNone(evidence.is_suspended)
        self.assertIsNone(evidence.has_market_quote)
        self.assertIsNone(evidence.has_trade_volume)
        self.assertIsNone(evidence.is_listed)


# ───────────────────────────── 存储语义 ─────────────────────────────


class RepositorySemantics(ArchiveTestCase):
    def test_duplicate_write_is_a_no_op_first_fact_wins(self):
        self.assertTrue(self.add("2025-03-10", is_st=False))
        self.assertFalse(self.add("2025-03-10", is_st=True))
        self.assertEqual(1, self.repo.count())
        self.assertEqual(
            TA.TradabilityReason.OK, self.decide("2025-03-10").buy_block_reason
        )

    def test_later_observed_correction_to_the_same_effective_state_is_kept(self):
        """同一生效时点的上游修正必须保留，并在其后被采用（双时态）。

        若身份不含 observed_at，这条修正会撞唯一键被静默丢弃，修正之后的决策会
        继续使用过期的第一版事实。
        """
        self.add("2025-06-10", is_st=False, observed="2025-06-10T15:05:00",
                 effective="2025-06-10T09:30:00")
        # 同一天、同一生效时点，但更晚才观察到的更正：其实是 ST。
        self.assertTrue(self.add("2025-06-10", is_st=True,
                                 observed="2025-06-12T15:05:00",
                                 effective="2025-06-10T09:30:00"))
        self.assertEqual(2, self.repo.count(), "修正必须落库，而不是被唯一键吞掉")

        # 修正被观察到之前 → 当时只知道第一版。
        before = self.decide("2025-06-10", at="2025-06-11T09:00:00")
        self.assertEqual(TA.TradabilityReason.OK, before.buy_block_reason)

        # 修正被观察到之后 → 采用最新已知的版本。
        after = self.decide("2025-06-10", at="2025-06-13T09:00:00")
        self.assertEqual(TA.TradabilityReason.ST_RESTRICTED, after.buy_block_reason)

    def test_same_observation_replayed_is_still_a_no_op(self):
        """同一观测时点的重复写入仍是 no-op —— 身份只放宽到观测维度。"""
        self.add("2025-06-10", is_st=False, observed="2025-06-10T15:05:00",
                 effective="2025-06-10T09:30:00")
        self.assertFalse(self.add("2025-06-10", is_st=False,
                                  observed="2025-06-10T15:05:00",
                                  effective="2025-06-10T09:30:00"))
        self.assertEqual(1, self.repo.count())

    def test_distinct_effective_instants_coexist_on_one_session(self):
        self.assertTrue(self.add("2025-05-05", is_suspended=True,
                                 effective="2025-05-05T09:30:00"))
        self.assertTrue(self.add("2025-05-05", is_suspended=False,
                                 effective="2025-05-05T14:30:00"))
        self.assertEqual(2, self.repo.count())

    def test_default_tuple_rows_are_read_correctly(self):
        """默认 ``sqlite3.connect()``（无 row_factory）必须照常工作。

        ``SELECT *`` 在默认连接上返回 tuple，按列名取值会抛 TypeError；旧实现把它
        吞成 None，于是每一列都未知、所有已落库行被 ``_visible_at`` 拒绝，归档
        永久返回 UNKNOWN_STATE。这种失效看起来像"没有数据"，而不是像 bug。
        """
        conn = sqlite3.connect(":memory:")  # 故意不设 row_factory
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        repo = TA.TradabilityArchiveRepository(conn)
        repo.save(TA.normalize_record({
            "code": "000001", "session_date": "2025-06-10", "source": SOURCE,
            "observed_at": "2025-06-10T15:05:00", "effective_at": "2025-06-10T09:30:00",
            "is_listed": True, "is_st": False, "is_suspended": False,
            "has_market_quote": True, "has_trade_volume": True,
        }))
        decision = TA.tradability_at(
            "000001", "2025-06-10",
            decision_time="2025-06-11T09:00:00", repository=repo,
        )
        self.assertTrue(decision.evidence_present, "默认 tuple 连接下事实必须被读到")
        self.assertEqual(TA.TradabilityReason.OK, decision.buy_block_reason)
        self.assertEqual(SOURCE, decision.source)

    def test_positional_row_mapping_follows_the_declared_column_order(self):
        """位置取值必须与建表列序一致 —— 用一整行 tuple 直接验证。"""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        conn.execute(
            f"INSERT INTO {TA.ARCHIVE_TABLE}("
            + ", ".join(TA.ARCHIVE_COLUMNS) + ") VALUES("
            + ", ".join("?" for _ in TA.ARCHIVE_COLUMNS) + ")",
            (
                1, "000001", "2025-06-10", "2025-06-10T09:30:00", "2025-06-10T15:05:00",
                1, 0, 0, 0, "up", 1, 1,
                SOURCE, None, None, None, "2025-06-10T15:06:00",
            ),
        )
        row = conn.execute(
            f"SELECT {TA._COLUMN_LIST} FROM {TA.ARCHIVE_TABLE}"
        ).fetchone()
        self.assertIsInstance(row, tuple)
        evidence = TA._row_to_evidence(row)
        self.assertEqual("000001", evidence.code)
        self.assertEqual("2025-06-10", evidence.session_date)
        self.assertIs(True, evidence.is_listed)
        self.assertIs(False, evidence.is_st)
        self.assertEqual("up", evidence.price_limit_direction)
        self.assertIs(True, evidence.has_trade_volume)
        self.assertEqual(SOURCE, evidence.source)

    def test_subsecond_observations_stay_distinct(self):
        """同一秒内的两次观测必须是两个身份，否则后到的修正被静默丢弃。"""
        self.assertNotEqual(
            TA._canonical_instant("2025-06-10T15:00:00.100"),
            TA._canonical_instant("2025-06-10T15:00:00.900"),
        )
        self.assertNotEqual(
            TA._canonical_instant("2025-06-10T15:00:00.100000"),
            TA._canonical_instant("2025-06-10T15:00:00.900000"),
        )

        self.add("2025-06-10", is_st=False, observed="2025-06-10T15:00:00.100",
                 effective="2025-06-10T09:30:00")
        self.assertTrue(self.add("2025-06-10", is_st=True,
                                 observed="2025-06-10T15:00:00.900",
                                 effective="2025-06-10T09:30:00"))
        self.assertEqual(2, self.repo.count())

    def test_truncation_does_not_make_evidence_visible_early(self):
        """截断到整秒会把证据的可见时点提前，必须保留原始精度。"""
        self.add("2025-06-10", is_st=True, observed="2025-06-10T15:00:00.900",
                 effective="2025-06-10T09:30:00")
        # 决策时点落在同一秒内但**早于**观测时刻 → 当时还没观察到。
        early = self.decide("2025-06-10", at="2025-06-10T15:00:00.100")
        self.assertFalse(early.evidence_present)
        self.assertEqual(TA.TradabilityReason.UNKNOWN_STATE, early.buy_block_reason)
        # 观测时刻之后 → 可见。
        later = self.decide("2025-06-10", at="2025-06-10T15:00:01")
        self.assertTrue(later.evidence_present)

    def test_external_write_invalidates_another_instances_cache(self):
        """另一条连接写入后，本实例的缓存必须失效，而不是无限期返回旧事实。

        摄取进程写、服务进程读是自然分工；只在自己 save() 时清缓存的话，读侧的
        缓存对别人的写入毫不知情，后到的修正在读侧等于不存在。
        """
        import tempfile

        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = handle.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        def connect():
            conn = sqlite3.connect(path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            TA.ensure_schema(conn)
            self.addCleanup(conn.close)
            return conn

        reader = TA.TradabilityArchiveRepository(connect())
        reader.save(TA.normalize_record({
            "code": "000001", "session_date": "2025-06-10", "source": SOURCE,
            "observed_at": "2025-06-10T15:05:00", "effective_at": "2025-06-10T09:30:00",
            "is_listed": True, "is_st": False, "is_suspended": False,
            "has_market_quote": True, "has_trade_volume": True,
        }))

        # 先把该时点的结论灌进 reader 的缓存。
        first = TA.tradability_at(
            "000001", "2025-06-10",
            decision_time="2025-06-13T09:00:00", repository=reader,
        )
        self.assertEqual(TA.TradabilityReason.OK, first.buy_block_reason)

        # 另一条连接写入修正。
        writer = TA.TradabilityArchiveRepository(connect())
        writer.save(TA.normalize_record({
            "code": "000001", "session_date": "2025-06-10", "source": SOURCE,
            "observed_at": "2025-06-12T15:05:00", "effective_at": "2025-06-10T09:30:00",
            "is_listed": True, "is_st": True, "is_suspended": False,
            "has_market_quote": True, "has_trade_volume": True,
        }))

        again = TA.tradability_at(
            "000001", "2025-06-10",
            decision_time="2025-06-13T09:00:00", repository=reader,
        )
        self.assertEqual(
            TA.TradabilityReason.ST_RESTRICTED, again.buy_block_reason,
            "外部写入后缓存必须失效",
        )

    def test_decision_time_is_part_of_the_cache_identity(self):
        """缓存键必须含 decision_time，否则会把某时点的结论错给另一时点。"""
        self.add("2025-03-10", is_st=False)
        self.add("2025-03-10", is_st=True, observed="2025-06-10T15:05:00",
                 effective="2025-05-01T00:00:00")
        # 先查"早"，再查"晚"——若缓存键只有 (code, date)，第二次会命中错结论。
        early = self.decide("2025-03-10", at="2025-03-11T09:00:00")
        late = self.decide("2025-03-10", at="2025-07-01T09:00:00")
        self.assertEqual(TA.TradabilityReason.OK, early.buy_block_reason)
        self.assertEqual(TA.TradabilityReason.ST_RESTRICTED, late.buy_block_reason)

    def test_write_invalidates_the_cache(self):
        """新写入的事实必须立刻可见——缓存不得回放旧结论。

        注意查询时点：停牌在 13:00 生效，而该 session 末尾（15:05）又写入了一条
        已复牌的事实，所以**收盘后**查询看到的是复牌后的正常状态（这是正确的
        PIT 语义——当时最新的生效事实）。要观察这条停牌，决策时点必须落在
        停牌生效之后、复牌之前。
        """
        self.add("2025-03-10")
        self.assertEqual(TA.TradabilityReason.OK, self.decide("2025-03-10").buy_block_reason)

        self.add("2025-03-10", is_suspended=True,
                 observed="2025-03-10T14:00:00", effective="2025-03-10T13:00:00")
        during = self.decide("2025-03-10", at="2025-03-10T14:00:00")
        self.assertEqual(TA.TradabilityReason.SUSPENDED, during.buy_block_reason)

        # 收盘后查询看到的是后来生效的复牌事实，而不是被缓存住的盘中结论。
        after = self.decide("2025-03-10", at="2025-03-10T16:00:00")
        self.assertEqual(TA.TradabilityReason.OK, after.buy_block_reason)

    def test_persisted_rows_survive_a_reread(self):
        self.add("2025-03-10", is_st=True)
        fresh = TA.TradabilityArchiveRepository(self.conn)
        decision = TA.tradability_at(
            "000001", "2025-03-10", decision_time="2025-03-11T09:00:00",
            repository=fresh,
        )
        self.assertEqual(TA.TradabilityReason.ST_RESTRICTED, decision.buy_block_reason)

    def test_archive_never_writes_on_read(self):
        self.add("2025-03-10")
        before = self.repo.count()
        for _ in range(5):
            self.decide("2025-03-10")
            self.decide("2025-99-99")
        self.assertEqual(before, self.repo.count())


# ───────────────────────────── Migration ─────────────────────────────


class MigrationCreatesTheArchive(unittest.TestCase):
    def test_migration_013_is_registered_and_additive(self):
        import db_migrate as M

        versions = [item[0] for item in M.MIGRATIONS["paper_trading"]]
        self.assertIn(13, versions)
        self.assertEqual(len(versions), len(set(versions)), "迁移版本号必须唯一")
        self.assertEqual(sorted(versions), versions, "迁移必须按版本号递增注册")

    def test_ensure_schema_is_idempotent_and_creates_the_unique_constraint(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        TA.ensure_schema(conn)
        TA.ensure_schema(conn)
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TA.ARCHIVE_TABLE})")}
        self.assertEqual(
            {"id", "code", "session_date", "effective_at", "observed_at", "is_listed",
             "is_st", "is_suspended", "is_price_limit_locked", "price_limit_direction",
             "has_market_quote",
             "has_trade_volume", "source", "listing_date", "delisting_date",
             "suspension_reason", "created_at"},
            columns,
        )
        indexes = [row[0] for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='index' AND tbl_name={TA.ARCHIVE_TABLE!r}"
        )]
        self.assertTrue(
            any("autoindex" in name for name in indexes),
            "必须存在 (code, session_date, effective_at) 唯一约束的索引",
        )

    def test_schema_module_owns_the_ddl_not_the_migration(self):
        """迁移必须复用归档模块的建表函数，避免第二个真相来源。"""
        import ast
        source = Path(TA.__file__).with_name("db_migrate.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_names = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.Import) for alias in node.names
        }
        self.assertIn("tradability_archive", module_names)
        self.assertIn("CREATE TABLE", Path(TA.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
