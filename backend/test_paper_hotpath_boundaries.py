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


if __name__ == "__main__":
    unittest.main()
