# -*- coding: utf-8 -*-
"""Environment-backed tuning values with one parsing policy.

Several runtime modules need a numeric knob (cache windows, request budgets,
retry limits).  Keeping one parser avoids the usual drift where one module
treats an empty string as zero, another raises, and a third silently accepts
NaN.  Invalid input always falls back to the documented default.
"""
from __future__ import annotations

import os


def env_float(name, default, *, minimum=None):
    """Return a finite float from ``name`` or ``default``.

    ``default`` is used for missing or unparsable values.  ``minimum`` clamps
    the result (for example 0 for "budget disabled" knobs).
    """
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        value = float(default)
    if value != value or value in (float("inf"), float("-inf")):
        value = float(default)
    if minimum is not None and value < float(minimum):
        value = float(minimum)
    return value


def env_int(name, default, *, minimum=None):
    """Integer variant of :func:`env_float` (truncates like ``int``)."""
    return int(env_float(name, default, minimum=minimum))
