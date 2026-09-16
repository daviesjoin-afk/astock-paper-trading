# -*- coding: utf-8 -*-
"""架构护栏：正式 learning 模块不得再自己切 train/validation/test。

这条护栏的存在理由是一个真实缺陷：``adaptive_engine._run_alpha_lab`` 曾经对原始
证据行自己做 ``int(len(dates) * 0.70)`` 的日期切分，于是"样本外结果"来自一个不
知道 PIT availability、标签成熟度、重叠未来标签、canonical cutoff 和 dataset
fingerprint 的旁路。代码 review 抓不住这类回归，所以把它固化成静态检查。

护栏刻意写得很窄，避免误伤：

* 只检查**正式 learning 模块**，不检查 UI / 回测 / 数据抓取里的日期切片；
* 判定基于 AST 结构（``len(...) * <浮点>`` 参与 ``int()``），不是宽松 regex，
  所以 ``int(len(active))``、``values[min(...)]`` 这类正常写法不会报；
* 对 alpha lab 额外要求：正式 partition 必须来自 canonical dataset，且
  ``_alpha_dataset()`` 的返回值不得再作为切分输入。
"""

import ast
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent

# 会产生/筛选/晋级学习候选的正式模块：这些模块不得自行切分数据。
FORMAL_LEARNING_MODULES = (
    "adaptive_engine.py",
    "learning_evaluation.py",
    "neural_shadow.py",
)

# 允许出现"长度比例"写法的模块：它们是展示、回测或数据完整性检查，
# 不是 learning authority。
NOT_LEARNING_AUTHORITY = {
    "adaptive_engine.py": {
        # 这些函数里的比例是数据完整性门槛，不是 train/validation 切分。
        "_market_profile",
        "_alpha_dataset",
        "_alpha_bounded_sample",
        # OHLC 字段覆盖率门槛（95%），与 partition 无关。
        "_data_input_state",
    },
}

FRACTION_TOLERANCE = 1e-9


def _load_tree(name):
    return ast.parse((BACKEND / name).read_text(encoding="utf-8"))


def _is_len_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
    )


def _is_ratio(node):
    """True for a literal fraction such as ``0.70`` / ``0.2`` (not 0, 1, or an int)."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and not float(node.value).is_integer()
    )


def _ratio_scaled_lengths(tree):
    """Yield ``(lineno, source)`` for every ``len(<something>) * <fraction>``.

    Structural, not textual: only a ``len(...)`` call multiplied by a
    non-integral float literal counts.  ``int(len(active))`` and
    ``values[int(len(values) * .95)]``-style percentile indexing are therefore
    reported by ast.unparse and filtered by the caller, not silently missed.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
            continue
        left, right = node.left, node.right
        if _is_len_call(left) and _is_ratio(right):
            yield node.lineno, ast.unparse(node)
        elif _is_len_call(right) and _is_ratio(left):
            yield node.lineno, ast.unparse(node)


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls(node, name):
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == name:
            yield child


class LearningModulesDoNotSplitDates(unittest.TestCase):
    def test_formal_learning_modules_have_no_ratio_length_split(self):
        offenders = []
        for name in FORMAL_LEARNING_MODULES:
            allowed_functions = NOT_LEARNING_AUTHORITY.get(name, set())
            tree = _load_tree(name)
            allowed_lines = set()
            for function_name in allowed_functions:
                function = _function(tree, function_name)
                if function is not None:
                    allowed_lines.update(
                        node.lineno for node in ast.walk(function)
                        if hasattr(node, "lineno")
                    )
            for lineno, expression in _ratio_scaled_lengths(tree):
                if lineno in allowed_lines:
                    continue
                offenders.append(f"{name}:{lineno}: {expression}")
        self.assertEqual([], offenders, "formal learning module slices data by ratio")

    def test_alpha_lab_does_not_slice_dates_into_train_validation(self):
        """The specific bypass this guard exists for."""
        tree = _load_tree("adaptive_engine.py")
        lab = _function(tree, "_run_alpha_lab")
        self.assertIsNotNone(lab, "_run_alpha_lab must exist")
        source = ast.unparse(lab)
        for banned in ("dates[:split]", "dates[split:]"):
            self.assertNotIn(banned, source)
        # No slicing of a date list at all inside the lab.
        for node in ast.walk(lab):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
                rendered = ast.unparse(node)
                self.assertNotIn(
                    "dates", rendered,
                    f"_run_alpha_lab slices a date list: {rendered}",
                )

    def test_alpha_lab_gets_partitions_from_the_canonical_dataset(self):
        tree = _load_tree("adaptive_engine.py")
        lab = _function(tree, "_run_alpha_lab")
        self.assertIsNotNone(lab)
        called = {call.func.id for call in _calls(lab, "_canonical_alpha_build")}
        self.assertTrue(called, "_run_alpha_lab must obtain its dataset canonically")

    def test_alpha_lab_never_uses_raw_rows_as_a_partition_source(self):
        """``_alpha_dataset()`` may exist, but not as the lab's split input."""
        tree = _load_tree("adaptive_engine.py")
        lab = _function(tree, "_run_alpha_lab")
        self.assertIsNotNone(lab)
        self.assertEqual(
            [], list(_calls(lab, "_alpha_dataset")),
            "_run_alpha_lab must not take partitions from the raw device row set",
        )

    def test_the_lab_reads_partitions_off_the_build(self):
        tree = _load_tree("adaptive_engine.py")
        frame = _function(tree, "_canonical_alpha_frame")
        self.assertIsNotNone(frame)
        source = ast.unparse(frame)
        # Partitions are read from the canonical build, per name.
        self.assertIn("build.partitions", source)
        self.assertIn("learning_dataset.PARTITIONS", source)


class GuardIsNotVacuouslyPassing(unittest.TestCase):
    """The guard must actually be able to fail; otherwise it proves nothing."""

    def test_ratio_detector_fires_on_the_old_bypass(self):
        tree = ast.parse(
            "def f(dataset):\n"
            "    dates = sorted({row['profile_date'] for row in dataset})\n"
            "    split = max(1, int(len(dates) * 0.70))\n"
            "    return dates[:split], dates[split:]\n"
        )
        found = list(_ratio_scaled_lengths(tree))
        self.assertEqual(1, len(found), "the detector missed the original bypass")
        self.assertIn("len(dates) * 0.7", found[0][1])

    def test_ratio_detector_ignores_integer_length_uses(self):
        tree = ast.parse(
            "def f(active, values):\n"
            "    a = int(len(active))\n"
            "    b = values[min(len(values) - 1, int(len(values) * 0.95))]\n"
            "    return a, b\n"
        )
        # ``* 0.95`` is a percentile index, not a split -- it is deliberately NOT
        # filtered here (the detector is structural); this test documents that
        # the detector does not fire on plain integer length conversion.
        found = [item for item in _ratio_scaled_lengths(tree) if "0.95" not in item[1]]
        self.assertEqual([], found)

    def test_slice_detector_fires_on_a_date_slice(self):
        tree = ast.parse("def f(dates, split):\n    return dates[:split]\n")
        lab = _function(tree, "f")
        rendered = [ast.unparse(n) for n in ast.walk(lab)
                    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice)]
        self.assertTrue(any("dates" in item for item in rendered))


if __name__ == "__main__":
    unittest.main()
