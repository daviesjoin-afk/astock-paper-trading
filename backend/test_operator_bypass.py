# -*- coding: utf-8 -*-
"""操作员凭据可关闭开关的状态机与设置校验。

默认必须保持"要求凭据"；只有操作员显式关闭才旁路。
"""
import unittest

import operator_auth as O
import runtime_settings as R


class BypassStateTests(unittest.TestCase):
    def tearDown(self):
        # 任何用例都不许把旁路状态泄漏给其它测试。
        O.set_bypass(False)

    def test_default_is_credential_required(self):
        O.set_bypass(False)
        self.assertFalse(O.bypass_disabled())

    def test_set_bypass_toggles_both_ways(self):
        O.set_bypass(True)
        self.assertTrue(O.bypass_disabled())
        O.set_bypass(False)
        self.assertFalse(O.bypass_disabled())

    def test_set_bypass_coerces_truthiness(self):
        O.set_bypass(1)
        self.assertTrue(O.bypass_disabled())
        O.set_bypass(0)
        self.assertFalse(O.bypass_disabled())

    def test_read_requests_stay_allowed_regardless_of_bypass(self):
        O.set_bypass(True)
        self.assertTrue(O.evaluate_request("GET", {}).allowed)
        O.set_bypass(False)
        self.assertTrue(O.evaluate_request("GET", {}).allowed)


class SecuritySettingTests(unittest.TestCase):
    def test_default_requires_credentials(self):
        self.assertIs(True, R.DEFAULTS["operator_auth_required"])

    def test_value_is_validated_as_boolean(self):
        self.assertFalse(R.validate({"operator_auth_required": False})["operator_auth_required"])
        self.assertTrue(R.validate({"operator_auth_required": "on"})["operator_auth_required"])

    def test_group_is_exposed_for_the_settings_snapshot(self):
        self.assertIn("security", R.SETTING_GROUPS)
        self.assertEqual(("operator_auth_required",), R.SETTING_GROUPS["security"])


if __name__ == "__main__":
    unittest.main()
