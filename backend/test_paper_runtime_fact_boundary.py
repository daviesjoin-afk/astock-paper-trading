"""Research reads paper runtime facts without importing trading orchestration."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PaperRuntimeFactBoundaryTests(unittest.TestCase):
    def test_adapter_keeps_exact_projection_type_without_loading_trading_engine(self):
        script = '''
import sqlite3
import sys
import ai_research_runtime_adapter as adapter
import paper_runtime_facts as facts
assert "paper_trading" not in sys.modules
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE paper_job_runs (run_key TEXT, slot TEXT, market_date TEXT, status TEXT, detail TEXT, started_at TEXT, finished_at TEXT, owner_key TEXT, fencing_token INTEGER)")
conn.execute("INSERT INTO paper_job_runs VALUES (?,?,?,?,?,?,?,?,?)", ("run-1", "risk", "2026-01-01", "running", None, "2026-01-01 10:00:00", None, "owner", 1))
fact = facts.paper_job_run_fact(conn, "run-1", as_of="2026-01-01")
assert type(fact) is facts.PaperJobRunFactProjection
assert adapter.evidence_ref_from_runtime_projection(fact).as_of == "2026-01-01"
assert "paper_trading" not in sys.modules
import paper_trading
assert paper_trading.PaperJobRunFactProjection is facts.PaperJobRunFactProjection
assert paper_trading.paper_job_run_fact is facts.paper_job_run_fact
conn.execute("UPDATE paper_job_runs SET status='unknown'")
assert facts.paper_job_run_fact(conn, "run-1", as_of="2026-01-01") is None
'''
        with tempfile.TemporaryDirectory(prefix="paper-fact-boundary-") as data_dir:
            env = os.environ.copy()
            env["ASTOCK_DATA_DIR"] = data_dir
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=Path(__file__).parent,
                env=env, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
