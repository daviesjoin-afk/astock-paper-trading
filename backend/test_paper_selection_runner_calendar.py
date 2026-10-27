"""Calendar checks must not load the HTTP application to skip a closed day."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PaperSelectionRunnerCalendarTests(unittest.TestCase):
    def test_closed_day_uses_universe_without_importing_http_application(self):
        program = r'''
import builtins
import datetime as dt
import sys
from unittest.mock import patch

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "main" or name == "fastapi" or name.startswith("fastapi."):
        raise AssertionError("calendar check imported HTTP application: " + name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import paper_selection_runner as runner
import universe
day = dt.date(2026, 10, 25)
with patch.object(universe, "is_trade_day", return_value=False) as calendar, \
     patch.object(runner.PS, "_today", return_value=day), \
     patch.object(runner.PS, "run_daily", side_effect=AssertionError("closed day ran selection")), \
     patch.object(sys, "argv", ["paper_selection_runner.py", "--slot", "daily"]):
    assert runner.main() == 0
    calendar.assert_called_once_with(day)
assert "main" not in sys.modules
assert "fastapi" not in sys.modules
'''
        with tempfile.TemporaryDirectory(prefix="selection-calendar-") as data_dir:
            env = os.environ.copy()
            env["ASTOCK_DATA_DIR"] = data_dir
            completed = subprocess.run(
                [sys.executable, "-c", program],
                cwd=Path(__file__).parent,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('"non_trading_day"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
