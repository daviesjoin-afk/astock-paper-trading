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
from zoneinfo import ZoneInfo

try:
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - only if tzdata is missing
    TZ = None


def now_stamp() -> str:
    """本地时区的秒级 ISO 时间戳（adaptive/evolution 的写入时间口径）。"""
    if TZ is not None:
        return dt.datetime.now(TZ).isoformat(timespec="seconds")
    return dt.datetime.now().isoformat(timespec="seconds")


def json_text(value) -> str:
    """紧凑、非 ASCII 安全、拒绝 NaN 的 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def json_loads(value, default=None):
    """宽松读取：空值/坏 JSON 返回 ``default``（默认 ``{}``）。"""
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return default if default is not None else {}


def clamp_number(value, low, high):
    return max(low, min(high, value))


# ── 兼容转发（PR-9：先给 owner 立公开契约，旧私有名保留为薄别名） ─────────────
# 迁移前 ~12 个模块直接 ``from adaptive_common import _now, _json, ...``，
# 那是一条**私有跨模块依赖**：owner 改内部名就会打断所有消费者。现在公开名是
# 唯一定义，私有名只是别名；未迁移的调用方仍然可用，但不再构成私有耦合。
_now = now_stamp
_json = json_text
_loads = json_loads
_clamp = clamp_number
