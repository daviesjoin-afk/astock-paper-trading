"""同名数值 helper 的输入矩阵：只合并**行为等价**的实现。

审计第 6 项：源码里 ``_num`` / ``_number`` / ``_finite`` 有几十份，语义并不
相同。本文件把它们的差异写成可执行的输入矩阵，防止"看起来一样"的合并把
未知价格变成零，或让历史上被拒绝的输入重新被接受。

矩阵覆盖：空值、布尔、数字字符串、NaN、无穷大、异常对象。
"""
import math
import unittest

import paper_numeric


class _Exploding:
    def __float__(self):
        raise RuntimeError("boom")


#: 宽松解析 + 严格有效（布尔/NaN/无穷 → 未知）。
LENIENT_STRICT = {
    "None": (None, None),
    "bool_true": (True, None),
    "bool_false": (False, None),
    "int": (3, 3.0),
    "float": (3.5, 3.5),
    "negative": (-1.0, -1.0),
    "numeric_string": ("3.5", 3.5),
    "garbage_string": ("abc", None),
    "empty_string": ("", None),
    "nan": (float("nan"), None),
    "inf": (float("inf"), None),
    "neg_inf": (float("-inf"), None),
    "list": ([], None),
    "dict": ({}, None),
}

#: 严格数值：只接受真正的 int/float；布尔按历史行为接受，字符串拒绝。
STRICT_NUM = {
    "None": (None, 0.0),
    "bool_true": (True, 1.0),
    "bool_false": (False, 0.0),
    "int": (3, 3.0),
    "float": (3.5, 3.5),
    "negative": (-1.0, -1.0),
    "numeric_string": ("3.5", 0.0),
    "garbage_string": ("abc", 0.0),
    "empty_string": ("", 0.0),
    "nan": (float("nan"), 0.0),
    "inf": (float("inf"), float("inf")),
    "neg_inf": (float("-inf"), float("-inf")),
    "list": ([], 0.0),
    "dict": ({}, 0.0),
}


class NumericContractMatrixTests(unittest.TestCase):
    def test_coerced_number_matrix(self):
        for name, (value, expected) in LENIENT_STRICT.items():
            with self.subTest(case=name):
                self.assertEqual(paper_numeric.coerced_number(value), expected)

    def test_num_matrix(self):
        for name, (value, expected) in STRICT_NUM.items():
            with self.subTest(case=name):
                self.assertEqual(paper_numeric.num(value), expected)

    def test_exploding_object_behaviour_matches_the_original(self):
        # 宽松合同只捕获 (TypeError, ValueError, OverflowError)。一个在
        # ``__float__`` 里抛别的异常的���象会**向上冒泡** —— 这是被替换实现
        # 的既有行为，必须保持一致，否则调用方的异常处理会改变。
        with self.assertRaises(RuntimeError):
            paper_numeric.coerced_number(_Exploding())
        # 严格合同只接受真正的 int/float，因此根本不尝试转换。
        self.assertEqual(paper_numeric.num(_Exploding()), 0.0)

    def test_the_two_contracts_disagree_on_strings(self):
        """两个合同必须保持不同；合并它们就是审计第 6 项要防的事故。"""
        self.assertEqual(paper_numeric.coerced_number("3.5"), 3.5)
        self.assertEqual(paper_numeric.num("3.5"), 0.0)

    def test_unknown_price_never_becomes_zero_in_the_lenient_contract(self):
        for value in (None, "abc", float("nan"), float("inf"), True):
            with self.subTest(value=value):
                self.assertIsNone(paper_numeric.coerced_number(value))


class SharedNumberOwnerTests(unittest.TestCase):
    """合并后的实现必须与被替换的原始实现逐项一致。"""

    def _original(self, value):
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None

    def test_shared_owner_matches_the_original_implementation(self):
        for name, (value, _expected) in LENIENT_STRICT.items():
            with self.subTest(case=name):
                self.assertEqual(paper_numeric.coerced_number(value),
                                 self._original(value))
        with self.assertRaises(RuntimeError):
            paper_numeric.coerced_number(_Exploding())
        with self.assertRaises(RuntimeError):
            self._original(_Exploding())

    def test_all_merged_modules_use_the_shared_owner(self):
        import auction_factor_shadow
        import auction_tracking
        import factor_effectiveness_report
        import intraday_t_pair_report
        import main_force_confirmation

        modules = (
            auction_factor_shadow, auction_tracking, factor_effectiveness_report,
            intraday_t_pair_report, main_force_confirmation,
        )
        for module in modules:
            with self.subTest(module=module.__name__):
                self.assertIs(module._number, paper_numeric.coerced_number)
                self.assertIsNone(module._number(True))
                self.assertIsNone(module._number(float("nan")))
                self.assertEqual(module._number("3.5"), 3.5)
                self.assertIsNone(module._number("abc"))


class NumericContractSeparationTests(unittest.TestCase):
    """不同合同必须保持不同，否则合并就改变了风控/学习口径。"""

    def test_bounded_num_rejects_huge_magnitudes_only(self):
        self.assertEqual(paper_numeric.bounded_num("1e14"), 1e14)
        self.assertIsNone(paper_numeric.bounded_num("1e15"))
        self.assertIsNone(paper_numeric.bounded_num("1e300"))
        # 注意：bound 合同**不**拒绝 inf（abs(inf) < 1e15 为假 → default）。
        self.assertIsNone(paper_numeric.bounded_num(float("inf")))
        self.assertIsNone(paper_numeric.bounded_num(float("nan")))
        self.assertEqual(paper_numeric.bounded_num("abc", 7.0), 7.0)

    def test_finite_num_rejects_nan_and_infinity(self):
        self.assertIsNone(paper_numeric.finite_num(float("nan")))
        self.assertIsNone(paper_numeric.finite_num(float("inf")))
        self.assertEqual(paper_numeric.finite_num("3.5"), 3.5)
        self.assertEqual(paper_numeric.finite_num("abc", 1.0), 1.0)
        self.assertEqual(paper_numeric.finite_num(True), 1.0)

    def test_nullable_num_keeps_infinity_and_replaces_none(self):
        self.assertEqual(paper_numeric.nullable_num(None, 2.0), 2.0)
        self.assertEqual(paper_numeric.nullable_num("3.5"), 3.5)
        self.assertEqual(paper_numeric.nullable_num(float("inf")), float("inf"))
        self.assertEqual(paper_numeric.nullable_num("abc", 4.0), 4.0)

    def test_strict_finite_rejects_literal_unknown_strings(self):
        for text in ("nan", "NaN", "none", "null", "-", "inf", "-inf", "+inf"):
            with self.subTest(text=text):
                self.assertIsNone(paper_numeric.strict_finite(text))
        self.assertIsNone(paper_numeric.strict_finite(True))
        self.assertIsNone(paper_numeric.strict_finite(None))
        self.assertIsNone(paper_numeric.strict_finite([]))
        self.assertEqual(paper_numeric.strict_finite(" 3.5 "), 3.5)
        self.assertEqual(paper_numeric.strict_finite(3), 3.0)

    def test_finite_num_and_strict_finite_disagree_on_bool(self):
        self.assertEqual(paper_numeric.finite_num(True), 1.0)
        self.assertIsNone(paper_numeric.strict_finite(True))

    def test_merged_modules_keep_their_own_contract(self):
        import adaptive_engine
        import adaptive_risk
        import adaptive_selection
        import ai_review_service
        import evolution_loop
        import learning_dataset
        import learning_evaluation
        import paper_position_read_model
        import paper_position_review
        import self_evolution
        import trade_attribution

        bounded = (ai_review_service, evolution_loop, self_evolution)
        for module in bounded:
            with self.subTest(module=module.__name__):
                self.assertEqual(module._num("1e15"), None)
                self.assertEqual(module._num("1e14"), 1e14)

        finite = (adaptive_engine, trade_attribution, adaptive_risk,
                  adaptive_selection)
        for module in finite:
            with self.subTest(module=module.__name__):
                # 这几个的默认值不同（None vs 0.0），但"不可读 → 默认值"
                # 的语义一致。
                self.assertIsNone(module._num(float("nan"), None))
                self.assertIsNone(module._num(float("inf"), None))
                self.assertEqual(module._num("3.5"), 3.5)

        nullable = (paper_position_read_model, paper_position_review)
        for module in nullable:
            with self.subTest(module=module.__name__):
                # 这两个是**纯域**模块（有"零项目导入"守卫），所以它们保留
                # 自己的实现而不是依赖 paper_numeric。行为必须一致。
                self.assertEqual(module._num(None, 2.0), 2.0)
                self.assertEqual(module._num(float("inf")), float("inf"))
                self.assertEqual(module._num("abc", 4.0), 4.0)

        strict = (learning_dataset, learning_evaluation)
        for module in strict:
            with self.subTest(module=module.__name__):
                self.assertIsNone(module._finite(True))
                self.assertIsNone(module._finite("nan"))
                self.assertEqual(module._finite("3.5"), 3.5)


if __name__ == "__main__":
    unittest.main()
