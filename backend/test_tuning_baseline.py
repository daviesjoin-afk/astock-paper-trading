# -*- coding: utf-8 -*-
"""AI 调参的权重基线。

没有 adaptive_selection overlay 时，运行时用的是模型默认权重。基线必须与
运行时一致：给模型空权重会让它无从提案，共识层也会因 base_weights 为空判
weight_format_invalid，AI 于是永远只能 hold。
"""
import os
import sqlite3
import tempfile
import unittest

import deepseek_advisor as D
import strategies as S


def _db(rows):
    path = tempfile.mktemp(suffix=".sqlite3")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_accounts(id TEXT, version TEXT, style TEXT, params TEXT)")
    for account_id, params in rows:
        conn.execute("INSERT INTO paper_accounts VALUES(?,?,?,?)",
                     (account_id, "v1", "", params))
    conn.commit()
    conn.close()
    return path


class TuningBaselineTests(unittest.TestCase):
    def _accounts(self, rows):
        path = _db(rows)
        try:
            return D._tuning_accounts(path)
        finally:
            os.unlink(path)

    def test_account_without_overlay_gets_the_model_defaults(self):
        accounts = self._accounts([("tq_breakout", "{}")])
        self.assertEqual(1, len(accounts))
        self.assertEqual(set(S.PAPER_WEIGHTS["one_to_two"]), set(accounts[0]["weights"]))
        self.assertAlmostEqual(1.0, sum(accounts[0]["weights"].values()), places=9)

    def test_existing_overlay_wins_over_the_defaults(self):
        params = '{"adaptive_selection": {"weights": {"flow": 0.5, "mom_short": 0.5}}}'
        accounts = self._accounts([("tq_breakout", params)])
        self.assertEqual({"flow": 0.5, "mom_short": 0.5}, accounts[0]["weights"])

    def test_hard_rule_accounts_are_excluded_from_tuning(self):
        accounts = self._accounts([
            ("reported_profit_breakout", "{}"),
            ("main_force_top10", "{}"),
            ("tq_breakout", "{}"),
        ])
        self.assertEqual(["tq_breakout"], [a["account_id"] for a in accounts])

    def test_every_returned_account_has_a_non_empty_baseline(self):
        accounts = self._accounts([
            ("tq_breakout", "{}"),
            ("trend_pullback", "{}"),
            ("sector_rotation", "{}"),
        ])
        self.assertEqual(3, len(accounts))
        for account in accounts:
            self.assertTrue(account["weights"], account["account_id"])


if __name__ == "__main__":
    unittest.main()
