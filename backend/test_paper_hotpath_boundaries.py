# -*- coding: utf-8 -*-
"""Paper trading hot-path module boundary and lazy import tests.

Ensures that fast-path / intraday modules (paper_trading, paper_runner)
do not eagerly import heavy post-market or research dependencies (paper_research, self_evolution),
preventing memory bloat and lock contention during high-frequency execution.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


class PaperHotpathBoundaryTests(unittest.TestCase):
    def _run_subproc(self, code: str):
        env = dict(os.environ)
        current_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{BACKEND}{os.pathsep}{current_pp}" if current_pp else BACKEND
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_paper_trading_import_does_not_load_heavy_modules(self):
        """Importing paper_trading must not eagerly load paper_research or self_evolution."""
        code = (
            "import sys\n"
            "import paper_trading\n"
            "loaded = set(sys.modules.keys())\n"
            "assert 'paper_research' not in loaded, f'paper_research was eagerly loaded'\n"
            "assert 'self_evolution' not in loaded, f'self_evolution was eagerly loaded'\n"
            "print('OK')\n"
        )
        res = self._run_subproc(code)
        self.assertEqual(res.returncode, 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_paper_runner_import_does_not_load_heavy_modules(self):
        """Importing paper_runner must not eagerly load paper_research or self_evolution."""
        code = (
            "import sys\n"
            "import paper_runner\n"
            "loaded = set(sys.modules.keys())\n"
            "assert 'paper_research' not in loaded, f'paper_research was eagerly loaded'\n"
            "assert 'self_evolution' not in loaded, f'self_evolution was eagerly loaded'\n"
            "print('OK')\n"
        )
        res = self._run_subproc(code)
        self.assertEqual(res.returncode, 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_lazy_loaded_modules_accessible_on_demand(self):
        """Modules should be loaded seamlessly when lazy getters are called."""
        import paper_trading

        pr = paper_trading._get_pr()
        self.assertIsNotNone(pr)
        self.assertTrue(hasattr(pr, "dashboard") or hasattr(pr, "ensure_schema"))

        se = paper_trading._get_se()
        self.assertIsNotNone(se)
        self.assertTrue(hasattr(se, "EVOLUTION_VERSION") or hasattr(se, "BOUNDS"))

    def test_hot_path_slots_definition_and_classification(self):
        """HOT_PATH_SLOTS must contain the SLA-critical trading slots."""
        import paper_trading as PT

        expected_hot = {"auction", "open", "risk", "intraday", "fast-entry"}
        self.assertEqual(PT.HOT_PATH_SLOTS, expected_hot)

        for slot in expected_hot:
            self.assertTrue(PT._is_hot_path_slot(slot), f"Slot {slot} must be classified as hot path")
            self.assertTrue(PT._is_hot_path_slot(slot.upper()), f"Slot {slot.upper()} must be case-insensitive")

        cold_slots = ["close", "sync-kline", "rebuild-factors", "daily-report", "history-recovery", None, ""]
        for slot in cold_slots:
            self.assertFalse(PT._is_hot_path_slot(slot), f"Slot {slot} must not be classified as hot path")

    def test_run_slot_wires_hot_path_profile_to_storage_db(self):
        """run_slot must pass hot_path=True to _db on hot slots and False on cold slots."""
        from unittest.mock import patch
        import paper_trading as PT

        captured_calls = []
        real_db = PT._db

        def capturing_db(*args, **kwargs):
            captured_calls.append(kwargs.get("hot_path", False))
            return real_db(*args, **kwargs)

        with patch.object(PT, "_is_trade_weekday", return_value=True), \
             patch.object(PT, "_db", side_effect=capturing_db), \
             patch.object(PT, "_assert_active_lease", return_value=None), \
             patch.object(PT, "_claim_runtime_lease", return_value=(False, "other", "exp")):

            # Run hot slot: "intraday"
            captured_calls.clear()
            PT.run_slot("intraday", force=True)
            self.assertIn(True, captured_calls, "Hot slot 'intraday' must wire hot_path=True into _db")

            # Run cold slot: "close"
            captured_calls.clear()
            PT.run_slot("close", force=True)
            self.assertTrue(all(hp is False for hp in captured_calls), "Cold slot 'close' must keep hot_path=False")


if __name__ == "__main__":
    unittest.main()
