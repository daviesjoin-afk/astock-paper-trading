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
        self.assertTrue(expected_modules, "生产模块集合不能是空的")
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
    与绝对导入指向同一批生产模块，必须一起计数。该计数保留为架构 review signal，
    不再单独阻断合法 owner 迁移；真正的 evidence 签发私有入口仍由
    ``test_ai_research_evidence_ownership_guard.py`` 的精确 owner/factory allowlist
    作为语义守卫。
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


class PackageAwareScanTests(unittest.TestCase):
    """生产扫描必须递归到子包，并用完整模块身份。

    改造前 `production_paths()` 只做 `root.glob("*.py")`：模块一旦迁进
    `backend/paper/`，就会从依赖图与所有指标里**静默消失**——不是被正确统计。
    这里用临时目录构造嵌套 package 做负向控制，证明现在的扫描能发现它们，
    且排除规则不会误伤生产模块。
    """

    def _make_tree(self, files: dict[str, str]) -> Path:
        import tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
        return root

    def test_nested_package_modules_are_discovered(self):
        root = self._make_tree({
            "top_level.py": "x = 1\n",
            "paper/__init__.py": "",
            "paper/risk_service.py": "y = 1\n",
            "paper/deep/exit_rules.py": "z = 1\n",
        })
        keys = {coupling_metrics.module_key(p, root)
                for p in coupling_metrics.production_paths(root)}
        self.assertIn("top_level", keys)
        self.assertIn("paper.risk_service", keys)
        self.assertIn("paper.deep.exit_rules", keys,
                      "递归扫描没有进入第二层子包")
        self.assertNotIn("paper.__init__", keys, "__init__ 不是生产模块")

    def test_excluded_dirs_do_not_hide_production_modules(self):
        root = self._make_tree({
            "paper/real.py": "x = 1\n",
            "tools/scanner.py": "y = 1\n",
            "test_support/helpers.py": "z = 1\n",
            "fixtures/sample.py": "w = 1\n",
            "test_thing.py": "t = 1\n",
        })
        keys = {coupling_metrics.module_key(p, root)
                for p in coupling_metrics.production_paths(root)}
        self.assertIn("paper.real", keys, "排除规则误伤了生产模块")
        for excluded in ("tools.scanner", "test_support.helpers",
                         "fixtures.sample", "test_thing"):
            self.assertNotIn(excluded, keys, f"{excluded} 不应被当作生产模块")

    def test_module_identity_uses_the_full_path_not_the_stem(self):
        """两个同名模块必须能区分，否则依赖图会把它们当成同一个节点。"""
        root = self._make_tree({
            "paper/common.py": "x = 1\n",
            "data/common.py": "y = 1\n",
        })
        keys = [coupling_metrics.module_key(p, root)
                for p in coupling_metrics.production_paths(root)]
        self.assertEqual(sorted(keys), ["data.common", "paper.common"])
        self.assertNotEqual(keys[0], keys[1], "同名模块的身份发生了碰撞")

    def test_relative_import_resolves_inside_a_package(self):
        root = self._make_tree({
            "paper/__init__.py": "",
            "paper/risk_service.py": "from .rules import decide\n",
            "paper/rules.py": "def decide(): pass\n",
        })
        modules = {coupling_metrics.module_key(p, root)
                   for p in coupling_metrics.production_paths(root)}
        tree = ast.parse((root / "paper/risk_service.py").read_text(encoding="utf-8"))
        edges = coupling_metrics._local_imports(
            tree, modules, current="paper.risk_service")
        self.assertEqual({"paper.rules"}, edges, "相对导入没有解析到子包内目标")

    def test_parent_relative_import_resolves(self):
        root = self._make_tree({
            "paper/__init__.py": "",
            "paper/common.py": "x = 1\n",
            "paper/deep/exit_rules.py": "from ..common import x\n",
        })
        modules = {coupling_metrics.module_key(p, root)
                   for p in coupling_metrics.production_paths(root)}
        tree = ast.parse((root / "paper/deep/exit_rules.py").read_text(encoding="utf-8"))
        edges = coupling_metrics._local_imports(
            tree, modules, current="paper.deep.exit_rules")
        self.assertEqual({"paper.common"}, edges, ".. 相对导入解析错误")

    def test_import_cycle_is_detected_across_package_boundaries(self):
        """负向控制：违规模块放进嵌套 package，环检测必须失败。"""
        root = self._make_tree({
            "paper/__init__.py": "",
            "paper/a.py": "import data.b\n",
            "data/__init__.py": "",
            "data/b.py": "from paper.a import thing\n",
        })
        modules = {coupling_metrics.module_key(p, root)
                   for p in coupling_metrics.production_paths(root)}
        edges = {}
        for path in coupling_metrics.production_paths(root):
            key = coupling_metrics.module_key(path, root)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            edges[key] = coupling_metrics._local_imports(tree, modules, current=key) - {key}
        cycles = coupling_metrics.strongly_connected(edges)
        self.assertTrue(cycles, "子包之间的导入环没有被发现")
        joined = {frozenset(cycle) for cycle in cycles}
        self.assertIn(frozenset({"paper.a", "data.b"}), joined, cycles)

    def test_foundation_reverse_dependency_in_subpackage_is_blocked(self):
        """基础层反向依赖在子包里同样要被拦截。"""
        root = self._make_tree({
            "paper/__init__.py": "",
            "paper/data_paths.py": "from paper.business import x\n",
            "paper/business.py": "x = 1\n",
        })
        violations = coupling_metrics._foundation_violations_cached(root)
        self.assertEqual((("paper.data_paths", "paper.business"),), violations,
                         "子包里的基础层反向依赖没有被拦截")


class SubpackageWriteScannerTests(unittest.TestCase):
    """唯一写入者检测必须覆盖子目录。

    负向控制：把一个「第二个写方」放进嵌套 package，扫描必须能看见它，
    从而让 owner 声明失败；否则模块一迁进子包，重复写入就会被静默放过。
    """

    def _make_tree(self, files: dict[str, str]) -> Path:
        import tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
        return root

    def test_second_writer_inside_a_subpackage_is_seen(self):
        from tools import ledger_write_inventory as INV
        root = self._make_tree({
            "paper/__init__.py": "",
            "paper/order_ledger.py": '''
                def insert(conn):
                    conn.execute("INSERT INTO paper_fills(order_id, qty) VALUES(?,?)", (1, 2))
            ''',
            "paper/rogue_writer.py": '''
                def sneak(conn):
                    conn.execute("INSERT INTO paper_fills(order_id, qty) VALUES(?,?)", (3, 4))
            ''',
        })
        found = {
            entry["module"] for entry in INV.scan(root)["entries"]
            if entry["role"] == "runtime" and "paper_fills" in entry["write_targets"]
        }
        self.assertEqual({"paper.order_ledger", "paper.rogue_writer"}, found,
                         "子包里的第二个写入方没有被发现，唯一写入者检测会漏报")

    def test_excluded_directories_do_not_contribute_writers(self):
        from tools import ledger_write_inventory as INV
        root = self._make_tree({
            "paper/__init__.py": "",
            "paper/order_ledger.py": '''
                def insert(conn):
                    conn.execute("INSERT INTO paper_fills(order_id, qty) VALUES(?,?)", (1, 2))
            ''',
            "tools/helper.py": '''
                def insert(conn):
                    conn.execute("INSERT INTO paper_fills(order_id, qty) VALUES(?,?)", (1, 2))
            ''',
        })
        found = {
            entry["module"] for entry in INV.scan(root)["entries"]
            if entry["role"] == "runtime" and "paper_fills" in entry["write_targets"]
        }
        self.assertEqual({"paper.order_ledger"}, found,
                         "tools/ 下的文件被误当成生产写入方")
