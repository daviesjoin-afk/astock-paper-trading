"""PR-8: frozen equivalence, negative boundaries and single-owner enforcement."""
from __future__ import annotations

import ast
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import entry_lifecycle as ELC
import paper_entry_decision as PED
import paper_sizing as PSZ
from paper_numeric import num

ROOT = Path(__file__).resolve().parents[1]
FROZEN = Path(__file__).with_name("fixtures") / "entry_quantity_frozen.json"
BASE = dict(nav=100000., cash=50000., position_value=0., industry_value=0.,
            code_value=0., fill_price=10., hard_stop=-.05,
            profile={"single_risk": .01, "max_weight": .1,
                     "max_exposure": .65, "max_industry": .3}, num=num)


class EntryQuantityTests(unittest.TestCase):
    def test_frozen_automatic_constraint_matrix_all_audit_fields(self):
        for sample in json.loads(FROZEN.read_text(encoding="utf-8"))["unit"]:
            with self.subTest(sample=sample["name"]):
                result = PSZ.entry_quantity(**sample["inputs"], num=num)
                self.assertEqual(result.qty, sample["qty"])
                self.assertEqual(result.sizing, sample["sizing"])

    def test_frozen_manual_requested_quantity_matrix(self):
        for request, expected, reasons in (
            (None, 1000, ()), (0, 1000, ()), (-100, 1000, ()),
            (100, 100, ()), (100.9, 100, ()),
            (99, 99, ("委托数量必须为 100 股的正整数倍",)),
            (1001, 1001, ("委托数量超过模型上限 1000 股", "委托数量必须为 100 股的正整数倍")),
            (1100, 1100, ("委托数量超过模型上限 1000 股",)),
        ):
            with self.subTest(request=request):
                result = PSZ.entry_quantity(**BASE, requested_qty=request)
                self.assertEqual((result.qty, result.recommended_qty, result.reasons),
                                 (expected, 1000, reasons))

    def test_automatic_manual_same_recommendation_and_constraints(self):
        for cash in (0., 999.99, 1000., 1000.01, 1999.99, 50000.):
            for price in (9.99, 10., 10.01, 1000.):
                inputs = {**BASE, "cash": cash, "fill_price": price}
                automatic = PSZ.entry_quantity(**inputs)
                manual = PSZ.entry_quantity(**inputs, requested_qty=0)
                self.assertEqual(automatic.qty, manual.recommended_qty)
                self.assertEqual(automatic.sizing, manual.sizing)
                self.assertEqual(automatic.qty % 100, 0)

    def test_no_cash_or_capacity_oversizing(self):
        for changes, binding in (
            ({"cash": 0.}, "cash"),
            ({"pool_cap_amount": 1000., "pending_pool_amount": 1000.}, "exposure"),
            ({"strategy_cap_amount": 1000., "pending_strategy_amount": 1000.}, "exposure"),
            ({"industry_value": 30000.}, "industry"),
            ({"code_value": 10000.}, "weight"),
        ):
            with self.subTest(changes=changes):
                result = PSZ.entry_quantity(**{**BASE, **changes}, requested_qty=100)
                self.assertEqual(result.recommended_qty, 0)
                self.assertIn(binding, result.sizing["binding_constraints"])
                self.assertEqual(result.reasons, ("委托数量超过模型上限 0 股",))

    def test_invalid_prices_do_not_produce_quantity(self):
        for price in (0, -1, float("nan"), float("inf"), -float("inf"), None, "bad"):
            with self.subTest(price=price):
                result = PSZ.entry_quantity(**{**BASE, "fill_price": price}, requested_qty=100)
                self.assertEqual(result.recommended_qty, 0)
                self.assertEqual(result.sizing["reason"], "无有效价格")
                self.assertEqual(result.reasons, ("委托数量超过模型上限 0 股",))

    def test_invalid_infinite_request_is_rejected(self):
        for request in (float("inf"), -float("inf")):
            result = PSZ.entry_quantity(**BASE, requested_qty=request)
            self.assertEqual(result.qty, 0)
            self.assertEqual(result.reasons, ("委托数量必须为 100 股的正整数倍",))

    def test_legacy_numeric_coercion_is_preserved(self):
        for request in ("100", "bad", float("nan")):
            result = PSZ.entry_quantity(**BASE, requested_qty=request)
            self.assertEqual((result.qty, result.reasons), (1000, ()))

    def test_price_change_recalculates_same_requested_order(self):
        before = PSZ.entry_quantity(**BASE, requested_qty=1000)
        after = PSZ.entry_quantity(**{**BASE, "fill_price": 10.01}, requested_qty=1000)
        self.assertEqual(before.reasons, ())
        self.assertEqual((after.qty, after.recommended_qty), (1000, 900))
        self.assertEqual(after.reasons, ("委托数量超过模型上限 900 股",))

    def test_tranche_floor_and_first_cap_keep_legacy_audit(self):
        result = PSZ.entry_quantity(**BASE, entry_tranche_scale=.51,
                                    first_tranche_cap_amount=3999.99)
        self.assertEqual(result.qty, 300)
        self.assertEqual(result.sizing["entry_tranche_qty"], 300)
        self.assertEqual(result.sizing["target_amount"], 3000.)
        self.assertEqual(result.sizing["main_force_first_tranche"]["qty_before"], 500)

    def test_slice_before_deployment_and_audit_stage(self):
        request = PSZ.EntrySliceRequest({}, {"entry_slices": 3}, "2026-09-10",
                                        False, None, ELC.entry_slice_plan)
        result = PSZ.entry_quantity(**BASE, entry_tranche_scale=.6,
                                    slice_request=request, deployment_max_qty=100,
                                    deployment_amount=1000.)
        self.assertEqual(result.pre_slice_qty, 600)
        self.assertEqual(result.slice_state["plan"], [200, 200, 200])
        self.assertEqual(result.sizing["entry_slice_target_qty"], 200)
        self.assertEqual(result.qty, 100)
        self.assertEqual(result.sizing["target_amount"], 6000.)
        self.assertEqual(result.sizing["lifecycle_deployment_clamped_qty"], 100)

    def test_existing_slice_plan_is_not_mutated_or_rebuilt(self):
        payload = {"entry_slice_policy": 2, "entry_slices": {
            "plan": [100, 200, 300], "filled": 1, "slices": 2}}
        original = json.dumps(payload, sort_keys=True)
        builder = mock.Mock(side_effect=AssertionError("must not rebuild"))
        result = PSZ.entry_quantity(**BASE, slice_request=PSZ.EntrySliceRequest(
            payload, {}, "2026-09-10", True, [100, 200, 300], builder))
        self.assertEqual(result.qty, 200)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)
        builder.assert_not_called()

    def test_rebuy_and_add_caps_use_same_owner(self):
        self.assertEqual(PSZ.entry_quantity(**BASE, quantity_cap=300).qty, 300)
        self.assertEqual(PSZ.entry_quantity(**BASE, tranche_amount=3999.99).qty, 300)
        self.assertEqual(PSZ.entry_quantity(**BASE, tranche_quantity=399.99).qty, 300)

    def test_minimum_order_policy_is_not_an_extra_sizing_constraint(self):
        result = PSZ.entry_quantity(**BASE)
        threshold = PSZ.dynamic_minimum_order_amount(100000., 15)
        self.assertEqual(threshold, 3200.)
        self.assertNotIn("minimum_order_amount", result.sizing["constraint_shares"])

    def test_legacy_entry_forwards_once_without_second_calculation(self):
        with mock.patch.object(PSZ, "entry_quantity", wraps=PSZ.entry_quantity) as entry, \
                mock.patch.object(PSZ, "_risk_quantity", wraps=PSZ._risk_quantity) as risk:
            qty, detail = PSZ.price_aware_qty(**BASE)
        entry.assert_called_once_with(**BASE)
        risk.assert_called_once()
        self.assertEqual((qty, detail), (1000, PSZ.entry_quantity(**BASE).sizing))

    def test_legacy_slice_forwards_without_its_own_arithmetic(self):
        self.assertIs(PED.prepare_entry_slice, PSZ.prepare_entry_slice)
        self.assertEqual(PED.prepare_entry_slice(
            payload={}, account={}, qty=100, asof_day="2026-09-10", continuation=False,
            existing_plan=None, lot_size=100, plan_builder=ELC.entry_slice_plan),
            (100, None, {}, None))


class EntryQuantityOwnershipTests(unittest.TestCase):
    def test_sizing_owner_is_stdlib_only_and_cannot_write_or_fetch(self):
        tree = ast.parse((ROOT / "backend/paper_sizing.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(item.name in sys.stdlib_module_names for item in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, sys.stdlib_module_names)
            if isinstance(node, ast.Call):
                self.assertNotIn(ast.unparse(node.func).split(".")[-1], {
                    "execute", "commit", "rollback", "connect", "open", "read_csv", "urlopen"})

    def test_both_entry_paths_call_public_owner_and_never_change_returned_quantity(self):
        for filename, function in (("paper_trading.py", "_buy_order"),
                                   ("manual_orders.py", "_manual_order_plan")):
            tree = ast.parse((ROOT / "backend" / filename).read_text(encoding="utf-8"))
            body = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == function)
            calls = [node for node in ast.walk(body) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == "PSZ.entry_quantity"]
            self.assertEqual(len(calls), 1, filename)
            if function == "_buy_order":
                assignments = [node for node in ast.walk(body) if isinstance(node, ast.Assign)
                               and any(isinstance(item, ast.Name) and item.id == "qty"
                                       for target in node.targets for item in ast.walk(target))]
                self.assertEqual(len(assignments), 1, "automatic qty must only unpack owner result")
                self.assertEqual(ast.unparse(assignments[0].value), "(quantity.qty, quantity.sizing)")
            else:
                self.assertFalse(any(isinstance(node, ast.Name) and node.id == "_price_aware_qty"
                                     for node in ast.walk(body)))
                self.assertIn("plan['qty'] = quantity.qty", ast.unparse(body))

    def test_intraday_paths_only_supply_quantity_caps(self):
        source = (ROOT / "backend/paper_trading.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name in ("_intraday_buyback", "_swing_scale_in"):
            body = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == name)
            assignments = [node for node in ast.walk(body) if isinstance(node, ast.Assign)
                           and any(isinstance(item, ast.Name) and item.id == "qty"
                                   for target in node.targets for item in ast.walk(target))]
            self.assertEqual(len(assignments), 1, name)
            self.assertEqual(ast.unparse(assignments[0].value.func), "_price_aware_qty")

    def test_frozen_real_automatic_manual_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts/pr8_sizing_probe.py"),
                "--output", str(Path(tmp) / "actual.json"), "--compare", str(FROZEN),
            ], cwd=ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class EntrySizingExecutionAuthorityTests(unittest.TestCase):
    def _fixture(self):
        from test_execution_planner import SimulationExecutionContractTests
        return SimulationExecutionContractTests()

    def test_planner_can_reduce_sizing_proposal_to_evidenced_fill(self):
        import execution_planner as EP
        fixture = self._fixture()
        proposal = PSZ.entry_quantity(**BASE)
        _, context = fixture._facts(liquidity=30000)
        decision = EP.evaluate_simulated_execution(fixture._intent(qty=proposal.qty), context)
        self.assertEqual(proposal.qty, 1000)
        self.assertEqual((decision.fill_quantity, decision.remaining_quantity), (300, 700))
        self.assertEqual(decision.status, "partially_filled")

    def test_t1_still_prevents_sale_of_sized_position(self):
        import execution_planner as EP
        fixture = self._fixture()
        proposal = PSZ.entry_quantity(**BASE)
        _, context = fixture._facts(sellable=0)
        decision = EP.evaluate_simulated_execution(
            fixture._intent(side="sell", qty=proposal.qty), context)
        self.assertEqual(decision.fill_quantity, 0)
        self.assertIn(EP.ExecutionReason.T1_NOT_SELLABLE.value, decision.reasons)

    def test_position_sellable_quantity_remains_upper_bound(self):
        import execution_planner as EP
        fixture = self._fixture()
        _, context = fixture._facts(sellable=100, liquidity=200000)
        decision = EP.evaluate_simulated_execution(fixture._intent(side="sell", qty=1000), context)
        self.assertLessEqual(decision.fill_quantity, 100)
        self.assertGreaterEqual(decision.remaining_quantity, 900)


from test_strategy_buy_commit_convergence import (  # noqa: E402
    _BuyCase, CODE, DAY_PREV, _quote,
)
import paper_trading as PT  # noqa: E402


class EntryQuantityGateBoundaries(_BuyCase):
    def _buy(self):
        self.quotes[CODE] = _quote(CODE)
        signal = self.add_signal(signal_date=DAY_PREV.isoformat())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.run_buy(signal_id=signal)

    def _minimum_boundary(self, delta, blocked):
        # Use the execution-price owner's full precision at the exact boundary.
        amount = 19900 * PT.EP.estimated_fill_price(10., "buy")
        with mock.patch.object(PT, "_dynamic_minimum_order_amount",
                               return_value=(amount + delta, {})):
            result, order = self._buy()
        self.assertEqual(bool(result.get("filled")), not blocked, result)
        if blocked:
            self.assertIn("低于动态最小建仓金额", order["reason"])

    def test_minimum_amount_below_boundary(self):
        self._minimum_boundary(-.01, False)

    def test_minimum_amount_exact_boundary(self):
        self._minimum_boundary(0., False)

    def test_minimum_amount_above_boundary(self):
        self._minimum_boundary(.01, True)

    def test_capacity_exhaustion_still_blocks_after_sizing(self):
        original = self._entry_plan_fixture

        def no_slots(*args, **kwargs):
            value = original(*args, **kwargs)
            value["plan"]["slot_plan"]["limits"][kwargs["account_id"]] = 0
            return value

        with mock.patch.object(PT, "_build_portfolio_entry_plan", side_effect=no_slots):
            result, order = self._buy()
        self.assertFalse(result.get("filled"), result)
        self.assertIn("策略持仓及待成交席位已达 exact plan 上限 0/0", order["reason"])

    def test_existing_reservation_is_not_spent_by_new_buy(self):
        import paper_capital_reservations as PCR
        with PT._db(immediate=True) as conn:
            ok, reason = PT._reserve_shared_capital(conn, "other-pending", "tq_breakout", CODE,
                                                  999000., 5.)
            self.assertTrue(ok, reason)
        result, order = self._buy()
        self.assertFalse(result.get("filled"), result)
        self.assertIn("预占", order["reason"])
        with PT._db() as conn:
            _, pending = PCR.pending_buy_reservations(conn, num_fn=num)
            self.assertEqual(pending, 999005.)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
