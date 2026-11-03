"""P11-D routing/capacity and bounded shared-SQLite coordination contracts."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import paper_schedule_contract as schedule
import paper_storage as storage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pr11_capacity", ROOT / "scripts" / "pr11_capacity.py")
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class CandidateLaneSourceTests(unittest.TestCase):
    def _business_hash(self, node):
        node = copy.deepcopy(node)
        if node.name == "monitor_risk":
            # Only this explicit caller-clock injection is admitted. The
            # remaining AST is still compared with the unchanged C baseline.
            clocks = []
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or ast.unparse(call.func) != "PRSS.settle_state":
                    continue
                for keyword in list(call.keywords):
                    if keyword.arg == "current_time":
                        self.assertEqual("dt.datetime.now", ast.unparse(keyword.value))
                        clocks.append(keyword)
                        call.keywords.remove(keyword)
            self.assertEqual(1, len(clocks), "risk wait must receive exactly one explicit caller clock")
        if node.name == "monitor_intraday":
            # Normalize only the reviewed move of this exact probe block past
            # the risk gate. Its contents and every other statement stay frozen.
            probes = [n for n in node.body if isinstance(n, ast.Try) and any(
                isinstance(c, ast.Call) and ast.unparse(c.func) == "PTM.phase"
                and c.args and isinstance(c.args[0], ast.Constant)
                and c.args[0].value == "source_health" for c in ast.walk(n))]
            self.assertEqual(1, len(probes))
            risk_index = next(i for i, n in enumerate(node.body) if isinstance(n, ast.Assign)
                              and any(isinstance(t, ast.Name) and t.id == "risk_result" for t in n.targets))
            self.assertEqual(risk_index + 2, node.body.index(probes[0]))
            node.body.remove(probes[0])
            node.body.insert(risk_index, probes[0])
        return hashlib.sha256(ast.dump(node).encode()).hexdigest()

    def test_worker_routing_keeps_risk_and_recovery_apart_from_candidate_tasks(self):
        for slot in schedule.RUNNER_SLOTS:
            expected = "astock-paper-worker" if slot in {"risk", "execution-resume"} else "astock-candidate-worker"
            self.assertEqual(expected, schedule.worker_container_name(slot))
        self.assertEqual("paper-scan.lock", schedule.host_lock_name("intraday"))
        self.assertEqual("risk-scan.lock", schedule.host_lock_name("risk"))
        self.assertEqual("execution-scan.lock", schedule.host_lock_name("execution-resume"))

    def test_business_functions_match_the_merged_c_baseline(self):
        frozen = json.loads((ROOT / "backend/fixtures/candidate_lane_57e559b.json").read_text(encoding="utf-8"))
        self.assertEqual("57e559b7a8db3df114e223e4cfb8c096256b44c9", frozen["base"])
        tree = ast.parse((ROOT / "backend/paper_trading.py").read_text(encoding="utf-8"))
        nodes = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for name, record in frozen["functions"].items():
            actual = self._business_hash(nodes[name])
            self.assertEqual(record["sha256"], actual, name)

    def test_clock_exception_does_not_allow_business_or_clock_substitution(self):
        tree = ast.parse((ROOT / "backend/paper_trading.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "monitor_risk")
        wrong_clock = copy.deepcopy(node)
        for call in ast.walk(wrong_clock):
            if isinstance(call, ast.Call) and ast.unparse(call.func) == "PRSS.settle_state":
                next(k for k in call.keywords if k.arg == "current_time").value = ast.parse("dt.datetime.utcnow", mode="eval").body
        with self.assertRaises(AssertionError):
            self._business_hash(wrong_clock)
        changed_completion = copy.deepcopy(node)
        for call in ast.walk(changed_completion):
            if isinstance(call, ast.Call) and ast.unparse(call.func) == "PRSS.complete_scan":
                next(k for k in call.keywords if k.arg == "finished_at").value = ast.Constant("forged")
        self.assertNotEqual(self._business_hash(node), self._business_hash(changed_completion))
        scan = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "monitor_intraday")
        changed_probe = copy.deepcopy(scan)
        for call in ast.walk(changed_probe):
            if isinstance(call, ast.Call) and ast.unparse(call.func) == "dfc.check_data_source_health":
                next(k for k in call.keywords if k.arg == "force").value = ast.Constant(False)
        self.assertNotEqual(self._business_hash(scan), self._business_hash(changed_probe))

    @unittest.skipUnless((ROOT / "deploy/run_paper_slot.sh").exists(), "deployment source absent")
    def test_host_script_uses_the_worker_route_and_keeps_all_lock_names(self):
        source = (ROOT / "deploy/run_paper_slot.sh").read_text(encoding="utf-8")
        self.assertIn('risk|execution-resume) worker="astock-paper-worker"', source)
        self.assertIn('*) worker="astock-candidate-worker"', source)
        self.assertIn('"$worker" python backend/paper_runner.py --slot "$slot"', source)
        for lock in ("paper-scan.lock", "risk-scan.lock", "execution-scan.lock"):
            self.assertIn(lock, source)


@unittest.skipUnless(shutil.which("docker") and (ROOT / "docker-compose.server.yml").exists(),
                     "requires host compose CLI/profile; CI resource probe also validates it")
class WorkerCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = capacity.read_compose(ROOT / "docker-compose.server.yml")

    def _check(self, config=None, **overrides):
        facts = {"cpu_count": 2, "total_memory": 11 * 1024**3, "available_memory": 6 * 1024**3}
        facts.update(overrides)
        return capacity.evaluate(config or self.config, **facts)

    def test_real_profile_has_shared_data_but_distinct_worker_limits(self):
        report = self._check()
        self.assertTrue(report["ok"], report)
        services = self.config["services"]
        paper, candidate = services["astock-paper-worker"], services["astock-candidate-worker"]
        def cache_source(service):
            return next(v["source"] for v in service["volumes"] if v["target"] == "/app/data_cache")
        self.assertEqual(cache_source(paper), cache_source(candidate))
        self.assertNotEqual(paper["container_name"], candidate["container_name"])
        self.assertEqual(0.8, float(paper["cpus"]))
        self.assertEqual(0.6, float(candidate["cpus"]))
        self.assertEqual(1250 * capacity.MIB, report["limits"]["astock-candidate-worker"]["memory_bytes"])

    def test_low_cpu_total_memory_and_available_memory_are_rejected(self):
        for kwargs, error in (({"cpu_count": 1}, "insufficient_cpu_capacity"),
                              ({"total_memory": 4 * 1024**3}, "insufficient_total_memory"),
                              ({"available_memory": 100 * 1024**2}, "insufficient_available_memory")):
            with self.subTest(kwargs=kwargs):
                self.assertIn(error, self._check(**kwargs)["errors"])

    def test_quota_swap_priority_and_native_concurrency_cannot_drift(self):
        for field, value, error in (("cpus", 3, "insufficient_cpu_capacity"),
                                   ("memswap_limit", 2 * 1024**3, "candidate_swap_not_bounded_to_memory"),
                                   ("cpu_shares", 8192, "risk_cpu_priority_missing")):
            config = copy.deepcopy(self.config)
            config["services"]["astock-candidate-worker"][field] = value
            self.assertIn(error, self._check(config)["errors"])
        config = copy.deepcopy(self.config)
        config["services"]["astock-candidate-worker"]["environment"]["OPENBLAS_NUM_THREADS"] = "8"
        self.assertIn("native_thread_limit_missing:astock-candidate-worker", self._check(config)["errors"])

    def test_missing_or_nonfinite_resource_facts_are_not_accepted(self):
        for value in ("nan", "inf", 0, -1):
            config = copy.deepcopy(self.config)
            config["services"]["astock-candidate-worker"]["cpus"] = value
            with self.assertRaises(ValueError):
                self._check(config)


class CandidateSqliteCoordinationTests(unittest.TestCase):
    def test_acquisition_error_closes_the_unreturned_connection(self):
        for stage in ("pragma", "begin"):
            with self.subTest(stage=stage):
                conn = sqlite3.connect(":memory:")
                self.addCleanup(conn.close)
                wrapper = mock.Mock(wraps=conn)
                def execute(sql, *args, connection=conn):
                    if sql == "BEGIN IMMEDIATE":
                        raise sqlite3.OperationalError("database is locked")
                    return connection.execute(sql, *args)
                if stage == "begin":
                    wrapper.execute.side_effect = execute
                journal = (mock.patch.object(storage, "apply_journal_mode", side_effect=sqlite3.OperationalError("busy"))
                           if stage == "pragma" else mock.patch.object(storage, "apply_journal_mode"))
                with mock.patch.object(storage.sqlite3, "connect", return_value=wrapper), journal:
                    with self.assertRaises(sqlite3.OperationalError):
                        with storage.db(":memory:", immediate=True, hot_path=True):
                            self.fail("failed acquisition must never yield a connection")
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")

    def test_busy_retry_waits_outside_a_transaction_and_leaves_commit_to_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "shared.sqlite3")
            with storage.db(path) as conn:
                conn.execute("CREATE TABLE probe_events(event_key TEXT PRIMARY KEY)")
            owner = sqlite3.connect(path)
            contender = sqlite3.connect(path)
            self.addCleanup(owner.close)
            self.addCleanup(contender.close)
            try:
                owner.execute("BEGIN IMMEDIATE")
                contender.execute("PRAGMA busy_timeout=1")
                def release(_):
                    self.assertFalse(contender.in_transaction)
                    owner.rollback()
                with mock.patch.object(storage.time, "sleep", side_effect=release) as sleep:
                    storage.execute_with_retry(contender, "BEGIN IMMEDIATE", hot_path=True)
                sleep.assert_called_once()
                self.assertTrue(contender.in_transaction)
                contender.execute("INSERT INTO probe_events VALUES ('same-event')")
                contender.rollback()
                self.assertEqual(0, contender.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0])
                with storage.db(path, immediate=True, hot_path=True) as conn:
                    self.assertEqual(800, conn.execute("PRAGMA busy_timeout").fetchone()[0])
                    storage.execute_with_retry(conn, "INSERT OR IGNORE INTO probe_events VALUES ('same-event')")
                    storage.execute_with_retry(conn, "INSERT OR IGNORE INTO probe_events VALUES ('same-event')")
                self.assertEqual(1, contender.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0])
            finally:
                owner.close()
                contender.close()


if __name__ == "__main__":
    unittest.main()
