import tempfile
import unittest
from pathlib import Path

from tools import coupling_metrics as metrics


class CouplingMetricTests(unittest.TestCase):
    def measure(self, sources):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in sources.items():
                (root / f"{name}.py").write_text(source, encoding="utf-8")
            return metrics.measure_all(root)

    def test_counts_cycles_deferred_imports_and_table_owners(self):
        result = self.measure({
            "paper_a": "import paper_b\nfrom paper_c import value\nSQL = 'SELECT * FROM paper_orders'\n",
            "paper_b": "import paper_a\ndef f():\n    import paper_c\n",
            "paper_c": "value = 1\n",
        })
        self.assertEqual(1, result["import_cycles"])
        self.assertEqual([("paper_a", "paper_b")], result["import_cycle_members"])
        self.assertEqual(1, result["deferred_imports"])
        self.assertEqual(1, result["paper_table_fanout"]["paper_orders"])

    def test_duplicate_helpers_and_cross_module_private_access(self):
        result = self.measure({
            "paper_a": "import paper_b\ndef _num(v, default=0):\n    return paper_b._private(v)\n",
            "paper_b": "def _num(v, default=None):\n    return default\n",
        })
        self.assertEqual(1, result["duplicate_helpers"])
        self.assertEqual(1, result["private_cross_access"])
        self.assertEqual({"_num": 2}, result["duplicate_helper_names"])

    def test_cross_domain_edges_only_count_real_domain_boundaries(self):
        result = self.measure({
            "paper_a": "import paper_b\nimport ai_agent\n",
            "paper_b": "VALUE = 1\n",
            "ai_agent": "import paper_b\n",
        })
        # paper_a → paper_b is intra-domain; paper_a/ai_agent and ai_agent/paper_b cross.
        self.assertEqual(2, result["cross_domain_edges"])

    def test_self_import_is_not_a_cycle(self):
        result = self.measure({"paper_a": "import paper_a\n"})
        self.assertEqual(0, result["import_cycles"])

    def test_fixture_discovery_excludes_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper_a.py").touch()
            (root / "test_paper_a.py").touch()
            self.assertEqual(("paper_a.py",), tuple(p.name for p in metrics.production_paths(root)))


if __name__ == "__main__":
    unittest.main()
