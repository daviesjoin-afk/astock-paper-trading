# -*- coding: utf-8 -*-
"""Shared low-level helpers for the adaptive / self-evolution modules.

C3 consolidation target: the four helpers below were copy-pasted into ~8
modules with byte-identical bodies (`_now` / `_json` / `_loads` / `_clamp`).
They are pure and have no per-module semantic drift, so unifying them is safe.

Intentionally NOT here:
- `_num`: carries per-module default drift (adaptive_engine uses ``default=None``
  while adaptive_selection/risk rely on ``default=0.0`` at ~100 call sites such as
  ``max(1e-9, _num(value))``; dual_ai_tuner/self_evolution/evolution_loop cap at
  1e15). Forcing one signature would silently change behaviour -> left module-local.
- `_connect`: opens different databases (adaptive_learning vs evolution loop) with
  different schema init. Not a real duplicate -> left module-local.

Keep this module a pure leaf: only stdlib imports, no adaptive_* imports.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from zoneinfo import ZoneInfo

try:
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - only if tzdata is missing
    TZ = None


def _now() -> str:
    if TZ is not None:
        return dt.datetime.now(TZ).isoformat(timespec="seconds")
    return dt.datetime.now().isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return default if default is not None else {}


def _clamp(value, low, high):
    return max(low, min(high, value))
