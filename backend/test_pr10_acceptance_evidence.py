"""原始冻结 ID 不随迁移消失；映射、接口、调用位置与同口径证据可反查。"""
import ast
import importlib.util
import json
from pathlib import Path
import unittest

import tools.ledger_write_inventory as INV

BACKEND = Path(__file__).resolve().parent
FROZEN = json.loads((BACKEND / "ledger_frozen_baseline.json").read_text(encoding="utf-8"))
MAPPING = json.loads((BACKEND / "ledger_migration_map.json").read_text(encoding="utf-8"))


class FrozenMigrationTests(unittest.TestCase):
    def test_account_projection_reuses_existing_fill_read_owner(self):
        evidence = json.loads((BACKEND / "ledger_fanout_evidence.json").read_text(encoding="utf-8"))
        before = evidence["before"]["tables"]
        after = evidence["after"]["tables"]
        self.assertEqual({"paper_account_metrics"},
                         set(before["paper_fills"]["readers"]) - set(after["paper_fills"]["readers"]))
        self.assertEqual(set(),
                         set(after["paper_fills"]["readers"]) - set(before["paper_fills"]["readers"]))
        self.assertEqual(before["paper_fills"]["writers"], after["paper_fills"]["writers"])
        self.assertNotIn("paper_account_metrics", INV.scan_readers()["paper_fills"])
        self.assertLess(sum(len(s["references"]) for s in after.values()),
                        sum(len(s["references"]) for s in before.values()))

    def test_original_ids_are_covered_once_and_keep_original_metadata(self):
        self.assertEqual("52eb50d390a05f88b451cf31f12bfb1b3df59391", FROZEN["ref"])
        self.assertEqual(57, FROZEN["writer_function_groups"])
        self.assertEqual(68, FROZEN["table_operations"])
        original = {op["id"]: op for op in FROZEN["operations"]}
        migrated = {op["id"]: op for op in MAPPING["operations"]}
        self.assertEqual(len(migrated), len(MAPPING["operations"]))
        self.assertEqual(set(original), set(migrated))
        self.assertEqual(FROZEN["ref"], MAPPING["baseline_ref"])
        for key, spec in migrated.items():
            for field in ("source", "table", "package", "transaction"):
                self.assertEqual(original[key][field], spec[field], key)
            self.assertEqual("migrated", spec["status"])
            self.assertTrue(spec["interfaces"], key)
            self.assertTrue(spec["exception_boundary"], key)
            self.assertTrue(spec["behavior_tests"], key)
            for module in spec["behavior_tests"]:
                tree = ast.parse((BACKEND / f"{module}.py").read_text(encoding="utf-8"))
                self.assertTrue(any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
                                    for n in ast.walk(tree)), module)

    def test_interfaces_cover_current_writers_and_references_still_exist(self):
        grouped = INV.group_targets(INV.scan())
        self.assertEqual(set(grouped), set(MAPPING["interfaces"]))
        self.assertEqual(len(grouped), MAPPING["current_writer_function_groups"])
        for key, spec in MAPPING["interfaces"].items():
            source = (BACKEND / f"{spec['owner']}.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            self.assertIn(spec["function"], {n.name for n in tree.body if isinstance(n, ast.FunctionDef)})
            self.assertTrue(spec["call_sites"], key)
            for site in spec["call_sites"]:
                path = BACKEND / f"{site['module']}.py"
                lines = path.read_text(encoding="utf-8").splitlines()
                # 位置指向直接调用、导出或依赖容器绑定，未把静态引用说成行为证明。
                window = "\n".join(lines[site["line"] - 1:site["line"] + 5])
                self.assertIn(spec["function"], window, (key, site))
        for operation in MAPPING["operations"]:
            for key in operation["interfaces"]:
                self.assertIn(operation["table"], grouped[key], operation["id"])

    def test_fanout_evidence_matches_current_source(self):
        path = BACKEND.parent / "scripts" / "pr10_acceptance_evidence.py"
        spec = importlib.util.spec_from_file_location("pr10_evidence", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected = json.loads(module.EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(expected, module.evidence())
        self.assertEqual(629, len(expected["before"]["cross_domain_edges"]))
        self.assertEqual(636, len(expected["after"]["cross_domain_edges"]))
