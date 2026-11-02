"""用具体违例验证迁移后的源码守卫确实会拒绝错误代码。"""
import unittest
from unittest import mock

import test_execution_profiles as profiles
import test_paper_trading_architecture_guard as guards


class GuardNegativeTests(unittest.TestCase):
    def test_limit_defer_guard_rejects_missing_owner_call(self):
        source = guards._source("paper_trading.py")
        broken = source.replace("SIG.defer_for_capacity(", "SIG.reject_signal(")
        self.assertNotEqual(source, broken)
        case = profiles.BuyOrderWiringTests()
        with mock.patch("builtins.open", mock.mock_open(read_data=broken)):
            with self.assertRaises((AssertionError, ValueError)):
                case.test_limit_deferred_signal_stays_retry_eligible()

    def test_provenance_guard_rejects_wrong_plan_identity(self):
        case = guards.EntryCapitalPlanningIsBounded()
        original = case._flat

        def broken(name, filename="paper_trading.py"):
            source = original(name, filename)
            if name == "_buy_order":
                source = source.replace('"allocation_plan_id":exact_plan["plan_id"]',
                                        '"allocation_plan_id":None')
            return source

        with mock.patch.object(case, "_flat", side_effect=broken):
            with self.assertRaisesRegex(AssertionError, "allocation plan id"):
                case.test_guard10g_production_buy_callers_pass_cycle_and_asof()

    def test_cycle_conflict_guard_rejects_missing_retry(self):
        case = guards.EntryCapitalPlanningIsBounded()
        source = case._flat("strategy_fill_failure", "manual_orders.py")
        broken = source.replace("_defer_signal", "_reject_signal")
        self.assertNotEqual(source, broken)
        with mock.patch.object(case, "_flat", return_value=broken):
            with self.assertRaises(AssertionError):
                case.test_guard10k_mismatch_never_releases_a_foreign_reservation()
