# -*- coding: utf-8 -*-
import ast
from pathlib import Path
import unittest


class AdaptiveDependencyBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = Path(__file__).with_name("adaptive_engine.py")
        cls.tree = ast.parse(cls.source_path.read_text(encoding="utf-8"))

    def test_adaptive_engine_has_no_direct_order_module_import(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertNotIn("paper_trading", imported)
        self.assertNotIn("api_paper", imported)

    def test_adaptive_engine_has_no_order_submission_calls(self):
        forbidden = {
            "submit_order", "submit_manual_order", "cancel_manual_order",
            "execute_open", "_execute_manual_plan", "_commit_strategy_buy",
        }
        calls = {
            node.func.id for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(forbidden.isdisjoint(calls), sorted(forbidden & calls))


if __name__ == "__main__":
    unittest.main()
