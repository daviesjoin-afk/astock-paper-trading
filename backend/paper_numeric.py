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

import math


def num(value, default=0.0):
    """Coerce ``value`` to float, rejecting non-numeric and NaN input.

    ``bool`` is intentionally accepted (it is an ``int`` subclass) to match the
    historical behaviour of the callers this replaced.
    """
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    return default


def coerced_number(value):
    """Coerce ``value`` to a finite float, or ``None`` when it is not a number.

    This is the *lenient-parse, strict-validity* contract:

    - numeric strings are accepted (``"3.5"`` → ``3.5``), because the report and
      shadow-lane readers consume values that arrive from CSV/JSON payloads;
    - ``bool`` is rejected — ``True`` is not a measurement, and silently turning
      it into ``1.0`` would turn a missing flag into a number;
    - ``NaN`` and infinities are rejected — they are "unknown", not "zero";
    - anything that cannot be parsed becomes ``None`` (unknown), never an
      exception and never a default of ``0``.

    It is deliberately **not** :func:`num`: that one refuses string input
    because a price that cannot be read must become a safe default on a
    lot-cost path. Two contracts, two answers, one owner each.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def bounded_num(value, default=None):
    """Parse a float and reject magnitudes at or above ``1e15``.

    A separate contract from :func:`coerced_number`: some lanes (AI review,
    evolution loops) treat an absurdly large magnitude as "the source produced
    garbage" and fall back to ``default``, while still accepting ordinary
    infinities is *not* wanted either — the magnitude check is the only
    rejection. Callers that need ``NaN``/infinity rejected must not use this.
    """
    try:
        number = float(value)
        return number if abs(number) < 1e15 else default
    except (TypeError, ValueError):
        return default


def finite_num(value, default=None):
    """Parse a float, falling back to ``default`` for NaN/infinity/garbage."""
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def nullable_num(value, default=0.0):
    """Parse a float; ``None`` becomes ``default``; other garbage becomes ``default``.

    Unlike :func:`finite_num` this does **not** reject infinities — the callers
    that use it treat an infinite value as a present (if extreme) number.
    """
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def strict_finite(value):
    """Return a finite float for a *measured* value, or ``None``.

    Only genuine numbers and well-formed numeric strings are accepted:
    - ``None`` and ``bool`` are unknown, never ``1.0``/``0.0``;
    - the literal strings ``nan``/``none``/``null``/``-``/``inf``/``-inf``/
      ``+inf`` are unknown even though ``float()`` would happily parse them;
    - anything else (lists, dicts, arbitrary objects) is unknown.

    Missing data must never be coerced to zero on a learning/evaluation path.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {
                "nan", "none", "null", "-", "inf", "-inf", "+inf"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) else None
