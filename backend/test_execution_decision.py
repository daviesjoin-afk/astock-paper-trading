# -*- coding: utf-8 -*-
"""R26 execution authority rules: deterministic and provider-independent."""
from __future__ import annotations

import unittest
from unittest import mock

import execution_planner as EP
import paper_trading_rules as PTR


def _quote(**overrides):
    result = {
        "code": "600000", "name": "测试标的", "price": 10.0, "prev_close": 10.0,
        "pct": 0.5, "amount": 5_000_000.0, "volume": 500_000,
        "quote_source": "live", "quote_validation": "cross_source_checked",
        "quote_at": "2026-09-08T10:00:00", "tradable": True,
    }
    result.update(overrides)
    return result


def _context(*, qty=100, side="buy", quote=None, as_of="2026-09-08T10:00:00",
             sellable_qty=None, same_day_filled_qty=0, order_type="market", limit_price=None):
    return EP.build_execution_context(
        account_id="test", cycle_id=3, code="600000", side=side, desired_qty=qty,
        as_of=as_of, market_evidence=quote or _quote(), order_type=order_type,
        limit_price=limit_price, sellable_qty=sellable_qty,
        same_day_filled_qty=same_day_filled_qty,
    )


class ExecutionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.calendar = mock.patch.object(PTR, "is_trade_weekday", return_value=True)
        self.calendar.start()
        self.quote_gate = mock.patch.object(
            EP, "quote_gate", return_value={"fresh": True, "status": "cross_source_checked"},
        )
        self.quote_gate.start()

    def tearDown(self):
        self.quote_gate.stop()
        self.calendar.stop()

    def test_partial_fill_is_capped_by_explicit_participation_policy(self):
        decision = EP.decide_simulated_execution(_context(qty=10_000))

        self.assertTrue(decision.executable_now)
        self.assertEqual("partially_filled", decision.status)
        self.assertEqual(10_000, decision.desired_qty)
        self.assertEqual(5_000, decision.filled_qty)
        self.assertEqual(5_000, decision.remaining_qty)
        self.assertEqual(0.01, decision.liquidity["participation_rate"])
        self.assertEqual("reference_plus_slippage", decision.pricing_basis)
        self.assertAlmostEqual(decision.filled_qty * decision.fill_price, decision.amount, places=2)

    def test_repeated_snapshot_does_not_reuse_consumed_market_capacity(self):
        decision = EP.decide_simulated_execution(
            _context(qty=10_000, same_day_filled_qty=5_000),
        )

        self.assertFalse(decision.executable_now)
        self.assertIn("no_executable_liquidity", decision.reason_codes)

    def test_session_and_market_facts_block_execution(self):
        scenarios = (
            (_context(quote=_quote(quote_at="2026-09-08T12:15:00"),
                      as_of="2026-09-08T12:15:00"), "outside_continuous_session"),
            (_context(quote=_quote(suspended=True), as_of="2026-09-08T10:00:00"), "not_tradable"),
            (_context(quote=_quote(amount=0.0), as_of="2026-09-08T10:00:00"), "liquidity_unknown"),
            (_context(quote=_quote(limit_up_locked=True, pct=10.0, price=11.0),
                      as_of="2026-09-08T10:00:00"), "locked_price_limit"),
            (_context(quote=_quote(), as_of=""), "missing_execution_as_of"),
        )
        for context, expected in scenarios:
            with self.subTest(reason=expected):
                decision = EP.decide_simulated_execution(context)
                self.assertFalse(decision.executable_now)
                self.assertIn(expected, decision.reason_codes)

    def test_sell_requires_proven_t_plus_one_sellable_quantity(self):
        decision = EP.decide_simulated_execution(
            _context(side="sell", qty=100, sellable_qty=0),
        )

        self.assertFalse(decision.executable_now)
        self.assertIn("t_plus_one_locked", decision.reason_codes)

    def test_daily_price_band_caps_slippage_and_rejects_out_of_band_price(self):
        buy = EP.decide_simulated_execution(_context())
        locked_buy = EP.decide_simulated_execution(_context(
            quote=_quote(price=11.0, pct=10.0, ask_available_qty=0),
        ))
        bad_quote = EP.decide_simulated_execution(_context(
            quote=_quote(price=11.02, pct=10.2),
        ))

        self.assertEqual(10.01, buy.fill_price)
        self.assertIn("locked_price_limit", locked_buy.reason_codes)
        self.assertIn("price_outside_daily_limit", bad_quote.reason_codes)

    def test_full_sell_can_liquidate_odd_lot_but_partial_sell_cannot(self):
        full = EP.decide_simulated_execution(_context(
            side="sell", qty=150, sellable_qty=150,
        ))
        partial = EP.decide_simulated_execution(_context(
            side="sell", qty=150, sellable_qty=200,
        ))

        self.assertEqual(150, full.filled_qty)
        self.assertEqual(100, partial.filled_qty)

    def test_untrusted_quote_and_invalid_buy_lot_fail_closed(self):
        with mock.patch.object(EP, "quote_gate", return_value={"fresh": False, "reason": "not verified"}):
            untrusted = EP.decide_simulated_execution(_context())
        invalid_lot = EP.decide_simulated_execution(_context(qty=150))

        self.assertIn("market_untrusted", untrusted.reason_codes)
        self.assertIn("invalid_lot_size", invalid_lot.reason_codes)

    def test_context_is_frozen_and_market_snapshot_is_copied(self):
        source = _quote()
        context = _context(quote=source)
        source["price"] = 99.0

        self.assertEqual(10.0, context.market_evidence["price"])
        with self.assertRaises((AttributeError, TypeError)):
            context.desired_qty = 999  # type: ignore[misc]

    def test_execution_terms_are_the_only_fill_price_and_fee_formula(self):
        buy = PTR.simulated_execution_terms(10.0, "buy", 100)
        sell = PTR.simulated_execution_terms(10.0, "sell", 100)

        self.assertEqual(10.01, buy["fill_price"])
        self.assertEqual(9.99, sell["fill_price"])
        self.assertAlmostEqual(buy["amount"] * PTR.COMMISSION, buy["fees"], places=2)
        self.assertAlmostEqual(
            sell["amount"] * (PTR.COMMISSION + PTR.STAMP_SELL), sell["fees"], places=2,
        )


if __name__ == "__main__":
    unittest.main()
