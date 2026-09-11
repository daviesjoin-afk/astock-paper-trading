# -*- coding: utf-8 -*-
"""Application service for paper slot validation and preflight cleanup.

This module deliberately sits below the legacy ``paper_trading`` facade.  It
owns only orchestration that is safe to extract without moving execution,
position sizing, quote validation, T+1, price-limit, suspension, or risk logic.
"""
from __future__ import annotations

from collections.abc import Callable

import entry_lifecycle as ELC
import execution_dispatch as EPD


SUPPORTED_SLOTS = frozenset({
    "auction", "open", "risk", "close", "weekly-review", "intraday",
})
_SLOT_ERROR = "slot 必须是 auction、open、risk、close、weekly-review 或 intraday"


def validate_slot(slot: str) -> str:
    """Return a supported slot or preserve the legacy validation error."""
    if slot not in SUPPORTED_SLOTS:
        raise ValueError(_SLOT_ERROR)
    return slot


def _best_effort_audit(db_factory, audit: Callable, event: str, detail: str) -> None:
    try:
        with db_factory() as conn:
            audit(conn, "system", event, detail)
    except Exception:
        # Audit failure must never turn cleanup into an execution outage.
        pass


def run_preflight(*, db_factory, audit: Callable, asof_day) -> dict:
    """Run cleanup in the legacy order and keep failures fail-soft + audited.

    Lifecycle cleanup must run before dispatch cleanup.  Either cleanup may
    fail independently; the slot itself continues exactly as the legacy facade
    did, while a best-effort audit records the failure.
    """
    result = {
        "lifecycle": "ok",
        "dispatch": "ok",
        "asof_day": asof_day.isoformat() if hasattr(asof_day, "isoformat") else str(asof_day),
    }
    try:
        with db_factory() as conn:
            ELC.expire_stale_signals(conn, asof_day=asof_day)
            ELC.expire_stale_orders(conn)
    except Exception as exc:  # pragma: no cover - exercised through injected failures
        result["lifecycle"] = "error"
        _best_effort_audit(
            db_factory,
            audit,
            "entry_lifecycle_error",
            f"信号/委托清扫失败：{type(exc).__name__}: {exc}",
        )

    try:
        with db_factory() as conn:
            EPD.run_execution_dispatch(conn)
    except Exception as exc:  # pragma: no cover - exercised through injected failures
        result["dispatch"] = "error"
        _best_effort_audit(
            db_factory,
            audit,
            "execution_dispatch_error",
            f"执行器清扫失败：{type(exc).__name__}: {exc}",
        )
    return result
