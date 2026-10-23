"""The extracted task reader must work without importing trading orchestration."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

BACKEND = Path(__file__).resolve().parent


class StandaloneRuntimeFactsTests(unittest.TestCase):
    def test_incident_collector_does_not_load_trading_or_adaptive_orchestration(self):
        script = textwrap.dedent('''
            import datetime as dt
            import importlib.abc
            import sys
            from unittest import mock

            attempted = []
            class RejectOrchestration(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname in {"paper_trading", "adaptive_engine"}:
                        attempted.append(fullname)
                        raise AssertionError("unexpected orchestration import: " + fullname)

            sys.meta_path.insert(0, RejectOrchestration())
            import paper_runtime_facts as paper
            import adaptive_runtime_facts as adaptive
            import ai_research_runtime_adapter
            import deepseek_research as research

            context = research.ResearchAsOfContext(
                asof_day="2026-09-20",
                market_now=dt.datetime.fromisoformat("2026-09-20T15:40:00+08:00"),
                targets=(),
            )
            connection = mock.Mock()
            fact = paper.PaperJobRunFactProjection(
                version=paper.PAPER_JOB_RUN_FACT_CONTRACT_VERSION,
                record_kind=paper.PAPER_JOB_RUN_RECORD_KIND,
                run_key="intraday:202609201530", slot="intraday", market_date="2026-09-20",
                runtime_status="failed", detail_canonical='{}',
                started_at="2026-09-20 15:30:00", finished_at="2026-09-20 15:35:00",
                owner_key="test-worker", fencing_token=1,
                availability_kind=paper.PAPER_JOB_RUN_AVAILABILITY_TERMINAL,
                availability_day="2026-09-20", fact_verification_status=paper.PAPER_JOB_RUN_FACT_RECORDED,
            )
            with mock.patch.object(paper, "paper_job_run_facts", return_value=(fact,)) as paper_read, \\
                 mock.patch.object(adaptive, "adaptive_run_facts", return_value=()) as adaptive_read, \\
                 mock.patch.object(research, "_paper", return_value=connection):
                events = research._collect_typed_events("incident_triage", connection, "unused", context)
                assert len(events) == 1
                assert events[0].evidence_ref.source_id == paper.PAPER_JOB_RUN_RECORD_KIND + "|" + fact.revision_identity
                assert events[0].evidence_ref.as_of == fact.availability_day
                paper_read.assert_called_once_with(connection, as_of="2026-09-20", limit=12)
                adaptive_read.assert_called_once_with(connection, as_of="2026-09-20", limit=12)
            connection.close.assert_called_once_with()
            assert not attempted, attempted
            assert "paper_trading" not in sys.modules
            assert "adaptive_engine" not in sys.modules
        ''')
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script], cwd=BACKEND,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_old_entry_exports_the_same_contract_and_readers(self):
        import paper_runtime_facts as facts
        import paper_trading as legacy

        for name in (
            "PAPER_JOB_RUN_FACT_CONTRACT_VERSION", "PAPER_JOB_RUN_RECORD_KIND",
            "PAPER_JOB_RUN_LIFECYCLE_STATUSES", "PAPER_JOB_RUN_LEGACY_READONLY_STATUSES",
            "PAPER_JOB_RUN_TERMINAL_STATUSES", "PAPER_JOB_RUN_IN_PROGRESS_STATUSES",
            "PAPER_JOB_RUN_FACT_RECORDED", "PAPER_JOB_RUN_FACT_OWNER_UNPROVEN",
            "PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES", "PAPER_JOB_RUN_AVAILABILITY_TERMINAL",
            "PAPER_JOB_RUN_AVAILABILITY_IN_PROGRESS", "PAPER_JOB_RUN_AVAILABILITY_KINDS",
            "PaperJobRuntimeFactError", "PaperJobRunFactProjection", "paper_job_run_fact", "paper_job_run_facts",
        ):
            with self.subTest(export=name):
                self.assertIs(getattr(facts, name), getattr(legacy, name))

    def test_reader_imports_only_the_required_standard_library_modules(self):
        tree = ast.parse((BACKEND / "paper_runtime_facts.py").read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertEqual(
            {"__future__", "dataclasses", "datetime", "hashlib", "json", "typing", "zoneinfo"}, imports,
        )

    def test_reader_queries_are_read_only_and_have_no_implicit_clock(self):
        tree = ast.parse((BACKEND / "paper_runtime_facts.py").read_text(encoding="utf-8"))
        queries = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            self.assertNotIn(name, {"connect", "commit", "rollback", "open", "now", "today", "utcnow"})
            if name in {"execute", "executemany", "executescript"}:
                self.assertEqual("execute", name)
                self.assertIsInstance(node.args[0], ast.Constant)
                query = node.args[0].value
                self.assertTrue(query.lstrip().upper().startswith("SELECT "), query)
                queries.append(query)
        self.assertTrue(queries, "read-only guard did not inspect any query")


if __name__ == "__main__":
    unittest.main()
