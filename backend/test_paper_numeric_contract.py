# -*- coding: utf-8 -*-
"""The strict numeric contract, and the dead helpers that must stay dead.

``paper_numeric`` holds one function because the codebase carried two live
``_num`` contracts that answer "what does an unreadable price mean"
differently:

* ``paper_trading._num`` / ``paper_numeric.num`` -- strict.  Anything that is
  not already an ``int``/``float`` becomes the default, and ``NaN`` counts as
  absent.  A lot cost must never be computed from a price that could not be
  read.
* ``paper_position_read_model._num`` -- lenient.  Coerces ``"3.5"`` to 3.5 and
  lets ``NaN`` through.

They are not duplicates, so they are not merged.  These tests pin the strict
one directly, because "the two implementations differ" does not prove the one
on the ledger path kept its strictness -- a mutation that swaps the strict
contract for the lenient one still leaves the two bodies different.

The second half guards deletions.  Two same-named JSON helpers were removed
because nothing referenced them; without a guard they are exactly the kind of
copy-paste that grows back.
"""
import unittest
from pathlib import Path

import paper_numeric as PNum  # noqa: E402

BACKEND = Path(__file__).resolve().parent


class StrictNumericContractGuard(unittest.TestCase):
    """``paper_numeric.num`` must keep rejecting unreadable values."""

    def test_rejects_string_numerals(self):
        self.assertEqual(PNum.num("3.5"), 0.0)
        self.assertEqual(PNum.num("3.5", 9.0), 9.0)
        self.assertEqual(PNum.num("abc"), 0.0)
        self.assertEqual(PNum.num(""), 0.0)

    def test_rejects_none_and_nan(self):
        self.assertEqual(PNum.num(None), 0.0)
        self.assertEqual(PNum.num(float("nan")), 0.0)
        self.assertEqual(PNum.num(float("inf")), float("inf"))

    def test_accepts_real_numbers(self):
        self.assertEqual(PNum.num(3.5), 3.5)
        self.assertEqual(PNum.num(7), 7.0)
        self.assertEqual(PNum.num(7, 1.0), 7.0)
        self.assertEqual(PNum.num(0), 0.0)

    def test_paper_trading_alias_uses_the_same_contract(self):
        import paper_trading as PT

        self.assertIs(
            PT._num, PNum.num,
            "paper_trading._num must be the same function object as "
            "paper_numeric.num, not a second implementation that happens to "
            "behave the same today",
        )
        for value in ("3.5", None, float("nan"), 3.5, 7, "abc"):
            self.assertEqual(
                PT._num(value), PNum.num(value),
                f"paper_trading._num({value!r}) must follow paper_numeric",
            )

    def test_lenient_contract_stays_separate(self):
        """The lenient ``_num`` must not be substituted onto ledger paths."""
        import paper_position_read_model as PPRM

        self.assertEqual(PPRM._num("3.5"), 3.5)
        self.assertNotEqual(
            PPRM._num("3.5"), PNum.num("3.5"),
            "if these now agree, the reason for keeping two contracts is gone "
            "and the module note must be revisited",
        )


class NumericVariantContractGuard(unittest.TestCase):
    """每一个仍在用的 ``_num`` 变体，把它的口径钉死。

    ``_num`` 在仓库里曾有多份副本。逐字相同的已经收拢成对 ``paper_numeric.num``
    的引用（下面用 ``is`` 断言，避免"今天行为一样、明天分叉"）；语义确实不同的
    保留在原处，但必须在此固定，否则一次顺手"统一"就会静默改掉读价口径。
    """

    def test_strict_variants_share_one_function_object(self):
        import paper_numeric as PNum
        import paper_risk_service as PRS

        self.assertIs(
            PRS._num, PNum.num,
            "paper_risk_service._num 必须是 paper_numeric.num 本身，"
            "而不是一份行为恰好相同的副本",
        )

    def test_leaf_engine_keeps_an_identical_copy_on_purpose(self):
        """``paper_risk_decision`` 必须零项目依赖，所以它的副本是**刻意**的。

        guard6a 要求该模块不能 import 任何项目内模块（纯决策引擎，不能卷入环），
        因此它自带一份 ``_num``。这里锁住"行为与 paper_numeric 完全一致"——
        哪天有人只改一边，这条会红。
        """
        import paper_numeric as PNum
        import paper_risk_decision as PRD

        for value in (None, float("nan"), float("inf"), 3.5, 7, 0, -1.5, "3.5",
                      "abc", "", True, False):
            self.assertEqual(
                PRD._num(value), PNum.num(value),
                f"paper_risk_decision._num({value!r}) 与 paper_numeric.num 分叉了",
            )

    def test_lot_ledger_is_strict_and_never_raises(self):
        import paper_lot_ledger as PLOT

        self.assertEqual(PLOT._num(float("nan")), 0.0)
        self.assertEqual(PLOT._num(float("nan"), 5.0), 5.0)
        self.assertEqual(PLOT._num("3.5"), 0.0, "记账路径不接受字符串数字")
        self.assertEqual(PLOT._num(None), 0.0)
        self.assertEqual(PLOT._num(3.5), 3.5)
        self.assertEqual(PLOT._num(7), 7.0)
        # 超出 float 范围的整数：回落默认值，而不是抛 OverflowError
        self.assertEqual(PLOT._num(10 ** 400), 0.0)

    def test_risk_evidence_treats_nan_as_absent(self):
        import paper_risk_evidence as PREv

        self.assertEqual(PREv._num(float("nan")), 0.0)
        self.assertEqual(PREv._num("3.5"), 0.0)
        self.assertEqual(PREv._num(3.5), 3.5)

    def test_replacement_decision_rejects_bool(self):
        import paper_replacement_decision as PRep

        self.assertEqual(PRep._num(True), 0.0, "bool 不是价格")
        self.assertEqual(PRep._num(True, 9.0), 9.0)
        self.assertEqual(PRep._num(3.5), 3.5)

    def test_read_side_variants_stay_lenient(self):
        import paper_position_read_model as PPRM
        import rebalance_scanner as RBS

        for module in (PPRM, RBS):
            self.assertEqual(module._num("3.5"), 3.5, f"{module.__name__} 应宽松解析")
            self.assertEqual(module._num(None, 7.0), 7.0)
            self.assertEqual(module._num("abc", 7.0), 7.0)


class RemovedHelperGuard(unittest.TestCase):
    """The unreferenced JSON helpers must not grow back."""

    REMOVED = {
        "rebalance_scanner.py": "_json",
        "retention_maintenance.py": "_loads",
    }

    def test_removed_helpers_are_absent(self):
        for filename, symbol in self.REMOVED.items():
            source = (BACKEND / filename).read_text(encoding="utf-8")
            self.assertNotIn(
                f"def {symbol}(", source,
                f"{filename} re-declares {symbol}; it had no callers -- if one is "
                "genuinely needed, import the shared helper instead of copying it",
            )

    def test_paper_lot_ledger_does_not_copy_the_json_helpers(self):
        """The lot owner must not carry unreferenced copies for other modules.

        It consumes ``paper_numeric`` and the shared ``_date``/``_now`` it
        already had; JSON encoding is not its job.
        """
        source = (BACKEND / "paper_lot_ledger.py").read_text(encoding="utf-8")
        for symbol in ("def _json(", "def _loads("):
            self.assertNotIn(symbol, source)


if __name__ == "__main__":
    unittest.main()
