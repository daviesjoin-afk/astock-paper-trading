"""Selection application must be usable without the FastAPI/HTTP composition root."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SelectionApplicationBoundaryTests(unittest.TestCase):
    def test_cli_and_selection_application_do_not_import_http_main(self):
        program = r'''
import builtins
import sys

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "main" or name == "fastapi" or name.startswith("fastapi."):
        raise AssertionError("selection application imported HTTP main: " + name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import selection_application
import selection_runner
import paper_selection
assert "main" not in sys.modules
assert "fastapi" not in sys.modules
'''
        with tempfile.TemporaryDirectory(prefix="selection-app-") as data_dir:
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


if __name__ == "__main__":
    unittest.main()
