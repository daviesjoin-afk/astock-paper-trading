# -*- coding: utf-8 -*-
"""Application service for paper slot validation and preflight cleanup.

This module deliberately sits below the legacy ``paper_trading`` facade.  It
owns only orchestration that is safe to extract without moving execution,
position sizing, quote validation, T+1, price-limit, suspension, or risk logic.
"""
from __future__ import annotations

from collections.abc import Callable

import entry_lifecycle as ELC
import paper_capital_reservations as PCR
import execution_dispatch as EPD


import paper_schedule_contract as PSch
import paper_risk_scan_state as PRSS

SUPPORTED_SLOTS = frozenset(PSch.RUN_SLOTS)


def run_execution_resume(day, *, monitor_risk: Callable, resume_orders: Callable) -> dict:
    """Resume approved orders after the same-window risk pass settles.

    No candidate refresh or approval happens here. The execution function
    fetches fresh quotes outside the writer transaction and owns all fills.
    """
    risk = monitor_risk(day)
    if not PRSS.result_is_settled(risk):
        return PRSS.blocked_round_result(
            slot="execution-resume", date=day.isoformat(), risk=risk,
            reason="风控扫描未完成：本轮不恢复已批准委托",
            extra={"execution_orders": []},
        )
    return {"status": "ok", "slot": "execution-resume", "date": day.isoformat(),
            "risk": risk, "execution_orders": resume_orders(day)}


def validate_slot(slot: str) -> str:
    """Return a supported slot or preserve the legacy validation error."""
    return PSch.validate_slot(slot)


def _best_effort_audit(db_factory, audit: Callable, event: str, detail: str) -> None:
    try:
        with db_factory() as conn:
            audit(conn, "system", event, detail)
    except Exception:
        # Audit failure must never turn cleanup into an execution outage.
        pass


def run_preflight(*, db_factory, audit: Callable, resolve_asof_day: Callable,
                  expire_order: Callable, expire_signal: Callable,
                  expire_stale_signal: Callable, supersede_order: Callable,
                  signals) -> dict:
    """Run cleanup in the legacy order and keep failures fail-soft + audited.

    ``resolve_asof_day`` is invoked *inside* the lifecycle try/DB context.  That
    deliberately preserves the legacy exceptional path: a bad date is audited
    as a lifecycle failure, dispatch cleanup still runs, and the facade later
    resolves the date again for the actual slot run (where the error propagates
    exactly as before).
    """
    result = {"lifecycle": "ok", "dispatch": "ok", "asof_day": None}
    try:
        with db_factory() as conn:
            asof_day = resolve_asof_day()
            result["asof_day"] = (
                asof_day.isoformat() if hasattr(asof_day, "isoformat") else str(asof_day)
            )
            ELC.expire_stale_signals(
                conn, release_reservation=PCR.release_order_reservation, asof_day=asof_day,
                expire_signal=expire_stale_signal, supersede_order=supersede_order)
            ELC.expire_stale_orders(
                conn, release_reservation=PCR.release_order_reservation,
                expire_order=expire_order, expire_signal=expire_signal)
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
            EPD.run_execution_dispatch(
                conn, release_reservation=PCR.release_order_reservation,
                signals=signals)
    except Exception as exc:  # pragma: no cover - exercised through injected failures
        result["dispatch"] = "error"
        _best_effort_audit(
            db_factory,
            audit,
            "execution_dispatch_error",
            f"执行器清扫失败：{type(exc).__name__}: {exc}",
        )
    return result
