# -*- coding: utf-8 -*-
"""候选数值边界的回归：畸形数值必须是 violation，而不是异常。

真实故障：``_validate_params_payload`` 用裸 ``float(value)`` 做边界比较。
Python 对 ``10**400`` 这类 int 会抛 ``OverflowError``，于是"一条畸形候选"
不是被明确拒绝，而是让整轮自进化以异常收场（调用方看到的是崩溃，不是 violations）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import evolution_activation as EA
import self_evolution as SE


class MalformedNumberValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        SE.ensure_schema(self.conn)
        EA.ensure_schema(self.conn)
        self.params = dict(SE.get_current_params(self.conn)["params"])

    def _candidate_with(self, **overrides):
        params = dict(self.params)
        params.update(overrides)
        created = EA.create_candidate(
            self.conn, params, source="evolve", reason="bounds test", validate=False)
        return created["params_id"]

    def test_huge_integer_is_a_violation_not_an_exception(self):
        params_id = self._candidate_with(consensus_weight_ratio=10 ** 400)
        outcome = EA.validate_candidate(self.conn, params_id)  # 修复前抛 OverflowError
        self.assertFalse(outcome["valid"])
        self.assertTrue(
            any("必须是有限数值" in item for item in outcome["violations"]),
            outcome["violations"],
        )

    def test_normal_out_of_range_value_still_reports_the_bound(self):
        params_id = self._candidate_with(consensus_weight_ratio=99.0)
        outcome = EA.validate_candidate(self.conn, params_id)
        self.assertFalse(outcome["valid"])
        self.assertTrue(
            any("超出全局边界" in item for item in outcome["violations"]),
            outcome["violations"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
