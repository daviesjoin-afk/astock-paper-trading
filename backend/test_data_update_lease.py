"""Static checks for the API/worker data-update admission boundary.

This test intentionally parses ``main.py`` instead of importing the FastAPI
application: the lightweight source checkout used by CI does not install the
large market-data dependency set.  Runtime tests on the deployment image
exercise the same wrappers with the real ``resource_guard`` lease.
"""

import ast
from pathlib import Path


MAIN = Path(__file__).with_name("main.py")


def _function(name):
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_incremental_wrappers_use_cross_process_lease():
    for name, locked_name in (
        ("_run_manual_incremental_update", "_run_manual_incremental_update_locked"),
        ("_run_factor_only_update", "_run_factor_only_update_locked"),
    ):
        source = ast.get_source_segment(MAIN.read_text(encoding="utf-8"), _function(name))
        assert source is not None
        tree = ast.parse(source)
        assert any(
            isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "heavy_job_lease"
                for item in node.items
            )
            for node in ast.walk(tree)
        )
        assert locked_name in source
        assert "status" in source and "failed" in source


def test_installer_removes_conflicting_cron_profiles_before_install():
    script = MAIN.parents[1] / "deploy" / "install-centos9.sh"
    text = script.read_text(encoding="utf-8")
    assert "/etc/cron.d/astock-codex" in text
    assert "/etc/cron.d/astock-quant" in text
    assert "rm -f --" in text
    assert '"${APP_DIR}/deploy/astock-quant.cron" /etc/cron.d/astock-quant' in text

