"""Verify repaired boundaries without application imports, DB or network.

The original before-fix evidence stays in architecture_boundary_probe_result.json.
Full behavior coverage lives in backend/test_architecture_boundary_fixes.py.
"""

import ast
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    waits = []
    calls = []

    class Unavailable(Exception):
        def __init__(self, *, status_code, detail):
            self.status_code = status_code
            super().__init__(detail)

    class Event:
        def wait(self, timeout):
            waits.append(timeout)
            return False

    api_tree = ast.parse((ROOT / "backend" / "api_paper.py").read_text(encoding="utf-8"))
    wait_seconds = next(
        n.value.value
        for n in api_tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "READ_MODEL_WAIT_SECONDS" for t in n.targets)
    )
    function = next(n for n in api_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_cache_load")
    miss = object()
    flight = {"event": Event(), "result": miss, "error": None}
    namespace = {
        "_current_cache_generation": lambda: 1,
        "_cget": lambda *a, **k: None,
        "_cache_lock": threading.Lock(),
        "_cache_inflight": {"dashboard": flight},
        "_CACHE_MISS": miss,
        "HTTPException": Unavailable,
        "READ_MODEL_WAIT_SECONDS": wait_seconds,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "api_paper.py", "exec"), namespace)
    try:
        namespace["_cache_load"]("dashboard", lambda: calls.append(True))
    except Unavailable as error:
        assert error.status_code == 503
    else:
        raise AssertionError("Expected bounded wait to report unavailability")
    assert waits == [wait_seconds] and not calls
    assert namespace["_cache_inflight"]["dashboard"] is flight
    dashboard_tree = ast.parse((ROOT / "backend" / "dashboard_queries.py").read_text(encoding="utf-8"))
    metric_calls = [
        n
        for n in ast.walk(dashboard_tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_account_metrics"
    ]
    assert metric_calls and all(
        any(
            k.arg == "allow_network" and isinstance(k.value, ast.Constant) and k.value.value is False
            for k in call.keywords
        )
        for call in metric_calls
    )
    result = {
        "phase": "after_fix",
        "scope": "real cache function with simulated timeout; dashboard call contract AST; no app imports, DB, network or sleep",
        "waits": waits,
        "loader_calls": len(calls),
        "status": 503,
        "original_owner_preserved": True,
        "all_dashboard_metrics_forbid_network": True,
    }
    (ROOT / "work" / "architecture_boundary_fix_probe_result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
