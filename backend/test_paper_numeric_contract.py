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
