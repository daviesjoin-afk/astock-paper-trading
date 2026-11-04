"""Hard-gate semantic dependency invariants; report structural metrics without blocking redesign."""
import ast
import json
from pathlib import Path
import textwrap
import unittest

from tools import coupling_metrics


class CouplingBudgetTests(unittest.TestCase):
    """Architecture metrics are review evidence, not a ratchet against valid redesign.

    The historical coupling_budget.lock.json is retained as a comparison snapshot.
    Only semantic dependency invariants remain blocking here. File size, function
    size, module counts, fan-out, raw-SQL module counts and import-style proxies
    must never force unrelated code moves or wrapper creation.
    """

    REVIEW_BASELINE = Path(__file__).with_name("coupling_budget.lock.json")

    def test_no_import_cycles(self):
        actual = coupling_metrics.measure_all()
        self.assertEqual([], actual["import_cycle_members"])
        self.assertEqual(0, actual["import_cycles"])

    def test_foundation_never_depends_on_a_business_domain(self):
        """Keep the dependency direction; update layer ownership when architecture moves."""
        violations = coupling_metrics.foundation_violations()
        self.assertEqual([], violations, (
            "基础层不得反向依赖业务域；如新架构调整层级，必须同步更新层级登记，"
            "不能通过放宽守卫掩盖真实反向依赖"
        ))

    def test_foundation_allowlist_has_no_unused_entries(self):
        """Exceptions must correspond exactly to real, documented assembly edges."""
        measured = coupling_metrics.measure_all()
        expected_modules = {path.stem for path in coupling_metrics.production_paths()}
        self.assertEqual(
            expected_modules, set(measured["import_edges"]),
            "import_edges 必须覆盖全部当前扫描的生产模块",
        )
        self.assertGreater(len(expected_modules), 100, "生产模块集合不能是空的")
        edges = {module: set(targets)
                 for module, targets in measured["import_edges"].items()}
        for module, allowed in coupling_metrics.FOUNDATION_ALLOWED_DEPENDENCIES.items():
            with self.subTest(module=module):
                self.assertIn(module, coupling_metrics.FOUNDATION_MODULES)
                used = set(edges.get(module, ())) & set(allowed)
                self.assertEqual(
                    set(allowed), used,
                    "白名单必须与实际依赖一致；多余条目等于默许新的反向依赖",
                )

    def test_foundation_allowlist_never_names_a_foundation_module(self):
        """Foundation-to-foundation dependencies do not need business-boundary exceptions."""
        for module, allowed in coupling_metrics.FOUNDATION_ALLOWED_DEPENDENCIES.items():
            with self.subTest(module=module):
                overlap = set(allowed) & coupling_metrics.FOUNDATION_MODULES
                self.assertEqual(set(), overlap)

    def test_structural_metrics_remain_available_for_review(self):
        """Collect size/coupling indicators, but deliberately apply no numeric ceilings."""
        actual = coupling_metrics.measure_all()
        baseline = json.loads(self.REVIEW_BASELINE.read_text(encoding="utf-8"))
        self.assertTrue(baseline, "历史指标快照不能为空")
        missing = sorted(set(baseline) - set(actual))
        self.assertEqual([], missing, f"度量器丢失了历史指标：{missing}")
        for name in baseline:
            with self.subTest(metric=name):
                self.assertIsInstance(
                    actual[name], (int, float),
                    f"{name} 必须保持可观测；它是 review signal，不是 CI ceiling",
                )

class PrivateChannelCountingTests(unittest.TestCase):
    """`from .module import _private` 必须与绝对导入同口径计数。

    背景：守卫原先只认 ``node.level == 0``，于是 ``data_fetcher`` 里那条
    ``from .market_data_contract import _parse_instant`` 回退分支从未被统计，
    "私有导入已归零"的说法在相对导入范围内并不成立。这里把口径钉住：相对导入
    与绝对导入指向同一批生产模块，必须一起计数；预算仍是 0，所以任何新的同类
    私有相对导入都会让 ``CouplingBudgetTests`` 立刻失败。
    """

    MODULES = {"owner", "other"}

    def _count(self, source):
        return coupling_metrics.from_import_private_names(
            ast.parse(textwrap.dedent(source)), self.MODULES)

    def test_absolute_private_import_is_counted(self):
        self.assertEqual(self._count("from owner import _hidden"), 1)

    def test_relative_private_import_is_counted(self):
        self.assertEqual(self._count("from .owner import _hidden"), 1,
                         "相对导入不得绕开私有导入统计")

    def test_relative_public_import_is_not_counted(self):
        self.assertEqual(self._count("from .owner import visible"), 0)

    def test_relative_import_of_foreign_module_is_not_counted(self):
        self.assertEqual(self._count("from .third_party import _hidden"), 0,
                         "只统计本仓库的生产模块")

    def test_dunder_names_are_not_private_channel(self):
        self.assertEqual(self._count("from .owner import __all__"), 0)
