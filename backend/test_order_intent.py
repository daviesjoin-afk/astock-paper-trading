# -*- coding: utf-8 -*-
"""OrderIntent 契约与五策略兼容 adapter 的回归测试（PR-05）。"""
import copy
import datetime as dt
import unittest

from order_intent import (
    ORDER_INTENT_FIELDS,
    OrderIntent,
    OrderIntentContractError,
    intent_to_legacy_fields,
    order_intent_from_payload,
    order_intent_from_signal,
    reject_qty_claims,
)

NOW = dt.datetime(2026, 9, 8, 10, 30, 0)

# 五套模拟盘策略的代表性 pick（执行器当前读取的字段子集）。
FIVE_STRATEGY_SIGNALS = {
    "tq_breakout": {
        "code": "002241", "name": "歌尔股份", "price": 21.5, "score": 0.82,
        "industry": "消费电子", "entry_model": "强势日内候选实时确认",
        "stop_loss": 20.1, "candidate_status": "normal",
    },
    "trend_pullback": {
        "code": "600519", "name": "贵州茅台", "price": 1520.0, "score": 0.64,
        "industry": "白酒", "entry_model": "趋势回踩结构确认",
        "ma20": 1508.0, "ma60": 1490.0,
    },
    "sector_rotation": {
        "code": "300750", "name": "宁德时代", "price": 188.0, "score": 0.71,
        "industry": "电池", "entry_model": "热点板块相对强度",
        "sector_rank": 1,
    },
    "reported_profit_breakout": {
        "code": "000651", "name": "格力电器", "price": 42.3, "score": 0.68,
        "industry": "家电", "entry_model": "已披露财报质量与突破确认",
        "stop_loss": 40.9,
    },
    "main_force_top10": {
        "code": "601899", "name": "紫金矿业", "price": 17.8, "score": 0.77,
        "industry": "有色金属", "entry_model": "主力持续性与微观成交确认",
        "main_pct": 12.4, "amount": 2450000000.0,
    },
}


class OrderIntentContractTests(unittest.TestCase):
    def test_strategy_payloads_cannot_carry_quantity_fields(self):
        for payload in (
            {"code": "002241", "qty": 100},
            {"code": "002241", "detail": {"shares": 300}},
            {"code": "002241", "sizing": {"amount": 5000.0}},
            ["harmless", {"order_amount": 1.0}],
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(OrderIntentContractError):
                    reject_qty_claims(payload)

    def test_intent_structurally_has_no_quantity_field(self):
        self.assertNotIn("qty", ORDER_INTENT_FIELDS)
        self.assertNotIn("amount", ORDER_INTENT_FIELDS)
        fields = set(OrderIntent.__dataclass_fields__)
        self.assertFalse(fields & {"qty", "quantity", "shares", "amount", "sizing"})

    def test_invalid_intents_are_rejected(self):
        base = {
            "symbol": "002241", "side": "buy", "strength": 0.5,
            "urgency": "immediate", "data_asof": "2026-09-08",
            "expires_at": "2026-09-08T15:05:00", "stop_reference": "price",
            "reason": "test",
        }
        with self.assertRaises(OrderIntentContractError):
            OrderIntent(**{**base, "side": "hold"})
        with self.assertRaises(OrderIntentContractError):
            OrderIntent(**{**base, "urgency": "whenever"})
        with self.assertRaises(OrderIntentContractError):
            OrderIntent(**{**base, "symbol": ""})
        with self.assertRaises(OrderIntentContractError):
            OrderIntent(**{**base, "data_asof": "2026-09-09"})
        with self.assertRaises(OrderIntentContractError):
            OrderIntent(**{**base, "context": {"qty": 100}})
        with self.assertRaises(OrderIntentContractError):
            OrderIntent(**{**base, "strength": 1.8})

    def test_payload_round_trip_preserves_the_intent(self):
        intent = OrderIntent(
            symbol="002241", side="buy", strength=0.82, urgency="immediate",
            data_asof="2026-09-08", expires_at="2026-09-08T15:05:00",
            stop_reference="price", reason="突破确认", strategy_id="tq_breakout",
            context={"score": 0.82, "industry": "消费电子"},
        )

        restored = order_intent_from_payload(intent.to_payload())

        self.assertEqual(intent, restored)
        self.assertEqual("order-intent-v1", intent.to_payload()["contract_version"])

    def test_payload_with_top_level_qty_is_rejected_not_silently_dropped(self):
        stored = {
            "symbol": "002241", "side": "buy", "strength": 0.82,
            "urgency": "immediate", "data_asof": "2026-09-08",
            "expires_at": "2026-09-08T15:05:00", "stop_reference": "price",
            "reason": "test", "qty": 500,
        }
        with self.assertRaises(OrderIntentContractError):
            order_intent_from_payload(stored)


class FiveStrategyAdapterTests(unittest.TestCase):
    def test_every_paper_strategy_signal_adapts_to_a_valid_intent(self):
        for strategy_id, signal in FIVE_STRATEGY_SIGNALS.items():
            with self.subTest(strategy_id=strategy_id):
                intent = order_intent_from_signal(strategy_id, signal, now=NOW)

                self.assertEqual(signal["code"], intent.symbol)
                self.assertEqual("buy", intent.side)
                self.assertEqual(strategy_id, intent.strategy_id)
                self.assertEqual("2026-09-08", intent.data_asof)
                self.assertTrue(0.0 <= intent.strength <= 1.0)
                self.assertEqual(signal["score"], intent.context["score"])
                self.assertTrue(intent.reason)

    def test_adapter_profiles_match_strategy_horizons(self):
        expected_urgency = {
            "tq_breakout": "immediate",
            "trend_pullback": "next_session",
            "sector_rotation": "same_session",
            "reported_profit_breakout": "next_session",
            "main_force_top10": "same_session",
        }
        expected_expiry_prefix = {
            "tq_breakout": "2026-09-08T15:05",
            "trend_pullback": "2026-09-09T09:35",
            "sector_rotation": "2026-09-08T15:05",
            "reported_profit_breakout": "2026-09-09T09:35",
            "main_force_top10": "2026-09-08T15:05",
        }
        expected_stop = {
            "tq_breakout": "price",
            "trend_pullback": "technical",
            "sector_rotation": "technical",
            "reported_profit_breakout": "price",
            "main_force_top10": "price",
        }
        for strategy_id, signal in FIVE_STRATEGY_SIGNALS.items():
            intent = order_intent_from_signal(strategy_id, signal, now=NOW)
            self.assertEqual(expected_urgency[strategy_id], intent.urgency, strategy_id)
            self.assertEqual(expected_expiry_prefix[strategy_id], intent.expires_at[:16], strategy_id)
            self.assertEqual(expected_stop[strategy_id], intent.stop_reference, strategy_id)

    def test_adapter_round_trip_is_equivalent_for_executor_read_fields(self):
        # 等价性承诺：旧 signal -> 意图 -> 旧字段视图，执行器读取的键值不变。
        executor_read_keys = (
            "code", "name", "price", "score", "industry", "entry_model",
            "stop_loss", "candidate_status", "sector_rank", "main_pct",
            "ma20", "ma60", "amount",
        )
        for strategy_id, signal in FIVE_STRATEGY_SIGNALS.items():
            with self.subTest(strategy_id=strategy_id):
                original = copy.deepcopy(signal)
                intent = order_intent_from_signal(strategy_id, signal, now=NOW)
                legacy = intent_to_legacy_fields(intent)

                for key in executor_read_keys:
                    if key in original:
                        self.assertEqual(original[key], legacy.get(key), key)
                # 适配不得改变输入（纯函数）。
                self.assertEqual(original, signal)

    def test_strength_is_clamped_and_defaults_are_conservative(self):
        hot = order_intent_from_signal(
            "tq_breakout", {"code": "002241", "score": 3.7}, now=NOW
        )
        self.assertEqual(1.0, hot.strength)

        unknown = order_intent_from_signal(
            "unknown_strategy", {"code": "600000"}, now=NOW
        )
        self.assertEqual("conservative", unknown.urgency)
        self.assertEqual("none", unknown.stop_reference)
        self.assertEqual(0.5, unknown.strength)
        self.assertEqual("2026-09-09T09:35", unknown.expires_at[:16])

    def test_signal_carrying_qty_is_a_contract_violation(self):
        with self.assertRaises(OrderIntentContractError):
            order_intent_from_signal(
                "tq_breakout", {"code": "002241", "qty": 500}, now=NOW
            )

    def test_market_turnover_alias_is_not_an_order_sizing_claim(self):
        # main_force_top10 的 ``amount`` 是龙虎榜市场成交额，不是下单金额；
        # 适配必须放行并以 market_turnover 入 context，还原时改回 amount。
        intent = order_intent_from_signal(
            "main_force_top10", FIVE_STRATEGY_SIGNALS["main_force_top10"], now=NOW
        )
        self.assertNotIn("amount", intent.context)
        self.assertEqual(2450000000.0, intent.context["market_turnover"])

        legacy = intent_to_legacy_fields(intent)
        self.assertNotIn("market_turnover", legacy)
        self.assertEqual(2450000000.0, legacy["amount"])

    def test_adapter_reason_survives_legacy_projection(self):
        signal = {"code": "002241", "reason": "20 日新高放量突破"}
        intent = order_intent_from_signal("tq_breakout", signal, now=NOW)
        self.assertEqual("20 日新高放量突破", intent.reason)
        self.assertEqual("20 日新高放量突破", intent_to_legacy_fields(intent)["reason"])

    def test_close_scan_intended_session_anchors_expiry(self):
        # 收盘扫描在 2026-09-08 盘后生成、面向 09-09 的信号：
        # 有效期必须锚定意图交易日，而不是入库即过期的 09-08T15:05。
        for strategy_id, expected_expiry in (
            ("tq_breakout", "2026-09-09T15:05"),
            ("sector_rotation", "2026-09-09T15:05"),
            ("main_force_top10", "2026-09-09T15:05"),
            ("trend_pullback", "2026-09-09T09:35"),
            ("reported_profit_breakout", "2026-09-09T09:35"),
        ):
            with self.subTest(strategy_id=strategy_id):
                intent = order_intent_from_signal(
                    strategy_id, FIVE_STRATEGY_SIGNALS[strategy_id], now=NOW,
                    intended_session=dt.date(2026, 9, 9),
                )
                self.assertEqual(expected_expiry, intent.expires_at[:16], strategy_id)

    def test_next_session_expiry_skips_weekends(self):
        # 2026-09-11 是周五：next_session 有效期必须是下周一，而不是周六。
        friday = dt.datetime(2026, 9, 11, 10, 30, 0)
        intent = order_intent_from_signal(
            "trend_pullback", FIVE_STRATEGY_SIGNALS["trend_pullback"], now=friday,
        )
        self.assertEqual("2026-09-14T09:35", intent.expires_at[:16])

    def test_adapter_is_deterministic(self):
        signal = FIVE_STRATEGY_SIGNALS["sector_rotation"]
        first = order_intent_from_signal("sector_rotation", signal, now=NOW)
        second = order_intent_from_signal("sector_rotation", signal, now=NOW)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
