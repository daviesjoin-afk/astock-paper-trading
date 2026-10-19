# -*- coding: utf-8 -*-
"""Numeric coercion for ledger arithmetic.

One function, on purpose.  The codebase carried two live ``_num`` contracts:
``paper_trading._num`` rejects anything that is not already an ``int``/``float``
(so ``"3.5"`` becomes the default) and treats ``NaN`` as absent, while
``paper_position_read_model._num`` coerces strings and lets ``NaN`` through.

They are not duplicates -- they are different answers to "what does an
unreadable price mean".  On a lot-cost path the strict one is correct: a price
that cannot be read must become a safe default, never a number.  This module
holds that strict contract so the ledger owners can share it without importing
the trading monolith, and the looser one stays where a lenient parse is
genuinely intended.

Stdlib only.  No project imports, so nothing here can close an import cycle.
"""
from __future__ import annotations


def num(value, default=0.0):
    """Coerce ``value`` to float, rejecting non-numeric and NaN input.

    ``bool`` is intentionally accepted (it is an ``int`` subclass) to match the
    historical behaviour of the callers this replaced.
    """
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    return default
