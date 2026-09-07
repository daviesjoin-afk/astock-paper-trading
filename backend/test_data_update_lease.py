"""Static checks for the API/worker data-update admission boundary.

This test intentionally parses ``main.py`` instead of importing the FastAPI
application: the lightweight source checkout used by CI does not install the
large market-data dependency set.  Runtime tests on the deployment image
exercise the same wrappers with the real ``resource_guard`` lease.

Converted from pytest-style top-level functions to unittest cases so
``unittest discover`` (and the CI ``--network none`` offline gate) actually
collects them (issue #29).
"""

import ast
import unittest
from pathlib import Path


MAIN = Path(__file__).with_name("main.py")


def _function(name):
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


class DataUpdateLeaseTests(unittest.TestCase):
    def test_incremental_wrappers_use_cross_process_lease(self):
        for name, locked_name in (
            ("_run_manual_incremental_update", "_run_manual_incremental_update_locked"),
            ("_run_factor_only_update", "_run_factor_only_update_locked"),
        ):
            with self.subTest(wrapper=name):
                source = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), _function(name))
                self.assertIsNotNone(source)
                tree = ast.parse(source)
                self.assertTrue(
                    any(
                        isinstance(node, ast.With)
                        and any(
                            isinstance(item.context_expr, ast.Call)
                            and isinstance(item.context_expr.func, ast.Name)
                            and item.context_expr.func.id == "heavy_job_lease"
                            for item in node.items
                        )
                        for node in ast.walk(tree)
                    )
                )
                self.assertIn(locked_name, source)
                self.assertIn("status", source)
                self.assertIn("failed", source)

    def test_installer_removes_conflicting_cron_profiles_before_install(self):
        script = MAIN.parents[1] / "deploy" / "install-centos9.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("/etc/cron.d/astock-codex", text)
        self.assertIn("/etc/cron.d/astock-quant", text)
        self.assertIn("rm -f --", text)
        self.assertIn('"${APP_DIR}/deploy/astock-quant.cron" /etc/cron.d/astock-quant', text)


if __name__ == "__main__":
    unittest.main()
