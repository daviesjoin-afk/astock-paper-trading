"""PR305 stage-order replay against frozen results from master e622955.

Record the same matrix with the original checkout's *entire* backend:
PR8_STAGE_BACKEND=<base>/backend python backend/test_entry_sizing_stage_order.py --record <json>
Ordinary unittest execution uses the current backend and the committed oracle.
No production function is copied, rewritten, or bypassed in this replay.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.environ.get("PR8_STAGE_BACKEND", str(Path(__file__).resolve().parent)))

import paper_trading as PT  # noqa: E402
from test_strategy_buy_commit_convergence import (  # noqa: E402
    ACCOUNT, CODE, DAY, DAY_PREV, MARKET, _BuyCase, _quote,
)

ORACLE = Path(__file__).with_name("fixtures") / "entry_sizing_stage_order.json"
# The frozen master oracle describes a closed dispatch window (no current or
# next batch). This is an independent clock from PT._now and fixture decision_at.
DISPATCH_AT = dt.datetime.combine(DAY, dt.time(15, 30))
SCENARIOS = (
    "single", "new_slices", "continuation", "zero_slice_target",
    "trial_first_cap", "dispatch_block_price_expired", "slice_builder_error",
    "malformed_plan", "malformed_plan_and_profile_error", "profile_error",
    "price_wait_error", "dispatch_error", "exceptional_error",
    "allocation_early_return", "deployment_setting_error", "recovery_input_error",
)


class StageFailure(RuntimeError):
    pass


def _buy_frame():
    frame = inspect.currentframe()
    try:
        while frame is not None:
            if frame.f_code.co_name == "_buy_order":
                return frame.f_locals
            frame = frame.f_back
        return {}
    finally:
        del frame


def _audit(local):
    risk = local.get("risk") or {}
    sizing = local.get("sizing")
    stage_fields = {
        "target_amount", "entry_tranche_scale_pct", "entry_tranche_qty",
        "main_force_first_tranche", "entry_slice_target_qty", "entry_slice_index",
        "entry_slice_total", "lifecycle_deployment_clamped_qty", "lifecycle_deployment_amount",
        "capital_deployment", "execution_limit_price", "minimum_order_amount", "qty_final",
    }
    return copy.deepcopy({
        "qty": local.get("qty"), "reasons": local.get("reasons"),
        "local_sizing": ({key: value for key, value in sizing.items() if key in stage_fields}
                         if sizing is not None else None),
        "risk_sizing_attached": "sizing" in risk,
        "risk_sizing_same_object": "sizing" in risk and risk["sizing"] is sizing,
        "risk_fields": sorted(set(risk) & {
            "sizing", "recovery_probe", "exceptional_opportunity", "entry_price_wait",
            "entry_opportunity", "execution_profile", "execution_dispatch", "entry_slices",
            "capital_deployment", "symbol_aggregate_check", "theme_aggregate_check"}),
        "slice_state": local.get("slice_state"),
        "entry_slices": risk.get("entry_slices"),
        "execution_dispatch": risk.get("execution_dispatch"),
        "price_wait": risk.get("entry_price_wait"),
    })


def replay_case(name, *, dispatch_at=DISPATCH_AT):
    harness = _BuyCase()
    with mock.patch.object(PT, "_now", return_value=f"{DAY} 10:00:00"), \
            mock.patch.object(PT.EPD, "_now", return_value=dispatch_at):
        harness.setUp()
        try:
            harness.quotes[CODE] = _quote(CODE)
            payload = {"pick": {"code": CODE, "score": .8, "price": 10.},
                       "decision": {"entry_model": {"score": 90.}}}
            if name != "single":
                payload["entry_slice_policy"] = 3
            if name in {"continuation", "zero_slice_target"}:
                payload["entry_slices"] = {
                    "plan": [100, 200, 300] if name == "continuation" else [0, 200, 300],
                    "filled": 1 if name == "continuation" else 0,
                    "day": str(DAY), "slices": 3,
                }
            if name.startswith("malformed_plan"):
                payload["entry_slices"] = {"plan": ["bad"], "filled": 0}
            if name == "recovery_input_error":
                payload["recovery_watch"] = ["bad"]
            signal_id = harness.add_signal(signal_date=DAY_PREV.isoformat(), payload=payload)
            events = []
            observations = {}

            def observed(label, function):
                def call(*args, **kwargs):
                    events.append(label)
                    if label in {"profile", "price_wait", "dispatch", "slice", "exceptional"}:
                        observations[label] = _audit(_buy_frame())
                    if (name in {"profile_error", "malformed_plan_and_profile_error"}
                            and label == "profile") or name == label + "_error":
                        raise StageFailure(label)
                    if name == "slice_builder_error" and label == "plan_builder":
                        raise StageFailure("plan_builder")
                    return function(*args, **kwargs)
                return call

            base_owner = PT.PSZ
            base_name = "_risk_quantity" if hasattr(base_owner, "_risk_quantity") else "price_aware_qty"
            slice_owner = PT.PSZ if hasattr(PT.PSZ, "prepare_entry_slice") else PT.PED
            original_plan = harness._entry_plan_fixture

            def fixed_plan(*args, **kwargs):
                kwargs["decision_at"] = f"{DAY}T10:00:00+08:00"
                return original_plan(*args, **kwargs)

            patches = [
                mock.patch.object(PT, "_build_portfolio_entry_plan", side_effect=fixed_plan),
                mock.patch.object(base_owner, base_name,
                                  side_effect=observed("base", getattr(base_owner, base_name))),
                mock.patch.object(PT, "_execution_profile_for_account",
                                  side_effect=observed("profile", PT._execution_profile_for_account)),
                mock.patch.object(PT, "_entry_price_wait",
                                  side_effect=observed("price_wait", PT._entry_price_wait)),
                mock.patch.object(PT.EPD, "plan_execution_dispatch",
                                  side_effect=observed("dispatch", PT.EPD.plan_execution_dispatch)),
                mock.patch.object(slice_owner, "prepare_entry_slice",
                                  side_effect=observed("slice", slice_owner.prepare_entry_slice)),
                mock.patch.object(PT.ELC, "entry_slice_plan",
                                  side_effect=observed("plan_builder", PT.ELC.entry_slice_plan)),
                mock.patch.object(PT, "_exceptional_opportunity",
                                  side_effect=observed("exceptional", PT._exceptional_opportunity)),
            ]
            if name == "exceptional_error":
                patches.append(mock.patch.object(PT, "_entry_execution_scale", return_value=0.))
            if name == "trial_first_cap":
                # Explicit model/policy inputs; every eligibility gate still runs.
                original = PT._strategy_entry_assessment
                patches.append(mock.patch.object(PT, "_strategy_entry_assessment",
                    side_effect=lambda *a, **kw: {**original(*a, **kw), "entry_tranche_scale": .5}))
                from dataclasses import replace
                policy = PT.EP.policy_for(ACCOUNT)
                patches.append(mock.patch.object(PT.EP, "policy_for", return_value=replace(
                    policy, first_tranche_nav_pct=.04)))
            if name == "dispatch_block_price_expired":
                dispatch = PT.EPD.plan_execution_dispatch
                patches.extend([
                    mock.patch.object(PT, "_entry_price_wait", side_effect=observed(
                        "price_wait", lambda *a, **kw: {"anchor_price": 9., "started_at": f"{DAY} 09:00:00"})),
                    mock.patch.object(PT, "_price_wait_outcome", return_value={
                        "status": "failed", "waited_minutes": 60., "started_at": f"{DAY} 09:00:00"}),
                    mock.patch.object(PT.EPD, "plan_execution_dispatch", side_effect=observed(
                        "dispatch", lambda *a, **kw: {**dispatch(*a, **kw), "blocked": True,
                                                     "blocked_reason": "stage dispatch veto"})),
                ])
            if name == "allocation_early_return":
                def blocked(*args, **kwargs):
                    value = fixed_plan(*args, **kwargs)
                    value["candidate_blocked"] = True
                    value["plan"]["blocking_reasons"] = ["stage allocation veto"]
                    return value

                patches.append(mock.patch.object(PT, "_build_portfolio_entry_plan", side_effect=blocked))
            if name == "deployment_setting_error":
                get_setting = PT.RSET.get

                def fail_aggregate(conn, key, *args, **kwargs):
                    if key == "theme_aggregate_cap_pct":
                        raise StageFailure("theme_setting")
                    return get_setting(conn, key, *args, **kwargs)

                patches.append(mock.patch.object(PT.RSET, "get", side_effect=fail_aggregate))
                def small_deployment(*args, **kwargs):
                    value = fixed_plan(*args, **kwargs)
                    # The settings fault precedes a positive, small deployment cap.
                    value["plan"]["capital_plan"]["allowance_by_strategy"][ACCOUNT] = 5000.
                    return value

                patches.extend([
                    mock.patch.object(PT, "_build_portfolio_entry_plan", side_effect=small_deployment),
                    mock.patch.object(PT, "_entry_execution_scale", return_value=100.),
                ])
            error = None
            exception_audit = None
            result = None
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                with PT._db() as conn:
                    signal = dict(conn.execute("SELECT * FROM paper_signals WHERE id=?",
                                               (signal_id,)).fetchone())
                try:
                    with PT._db(immediate=True) as conn:
                        result = PT._buy_order(conn, {"id": ACCOUNT}, signal,
                            dict(harness.quotes[CODE]), dict(MARKET), [], DAY,
                            all_quotes=dict(harness.quotes))
                except Exception as exc:
                    error = {"type": type(exc).__name__, "message": str(exc)}
                    trace = exc.__traceback__
                    while trace is not None:
                        if trace.tb_frame.f_code.co_name == "_buy_order":
                            exception_audit = _audit(trace.tb_frame.f_locals)
                        trace = trace.tb_next
            with PT._db() as conn:
                orders = [dict(row) for row in conn.execute(
                    "SELECT qty,filled_qty,status,reason,risk_payload FROM paper_orders WHERE signal_id=?",
                    (signal_id,))]
                for order in orders:
                    risk = PT._loads(order.pop("risk_payload"), {})
                    order["sizing"] = _audit({"sizing": risk.get("sizing")})["local_sizing"]
                    order["entry_slices"] = risk.get("entry_slices")
                persisted = dict(conn.execute("SELECT status,reason,payload FROM paper_signals WHERE id=?",
                                               (signal_id,)).fetchone())
                persisted["payload"] = PT._loads(persisted["payload"], {})
                ledger = {
                    "orders": orders, "signal": persisted, "cash": PT._shared_cash(conn),
                    "fills": [dict(row) for row in conn.execute(
                        "SELECT qty,price,amount,fees FROM paper_fills ORDER BY id")],
                    "reservation_count": conn.execute("SELECT COUNT(*) FROM paper_capital_reservations").fetchone()[0],
                    "risk_count": conn.execute("SELECT COUNT(*) FROM paper_risk_decisions").fetchone()[0],
                    "audit_count": conn.execute("SELECT COUNT(*) FROM paper_audit").fetchone()[0],
                }
            # Stage observations only retain the fields relevant to that boundary.
            return {"events": events, "observations": observations, "error": error,
                    "exception_audit": exception_audit, "result": result, "ledger": ledger}
        finally:
            harness.tearDown()


class EntrySizingStageOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        oracle = json.loads(ORACLE.read_text(encoding="utf-8"))
        if oracle["source_sha"] != "e622955deb702ae65e101ce260a0e3bea2e0a2f0":
            raise AssertionError("stage oracle must be recorded from the original master")
        cls.baseline = oracle["cases"]

    def test_frozen_stage_order_exception_audit_and_ledger(self):
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    actual = replay_case(name)
                for dimension in ("error", "events", "observations", "exception_audit", "result", "ledger"):
                    with self.subTest(dimension=dimension):
                        self.assertEqual(actual[dimension], self.baseline[name][dimension])

    def test_staged_owner_reuses_base_and_preserves_audit_reference(self):
        from test_entry_quantity import BASE
        owner = PT.PSZ
        with mock.patch.object(owner, "_risk_quantity", wraps=owner._risk_quantity) as base:
            quantity = owner.entry_quantity(**BASE)
            audit = quantity.sizing
            quantity = owner.entry_quantity(quantity, fill_price=10., num=PT._num,
                                            entry_tranche_scale=.6)
            quantity = owner.entry_quantity(quantity, fill_price=10., num=PT._num,
                                            first_tranche_cap_amount=5000.)
            quantity = owner.entry_quantity(quantity, num=PT._num, slice_request=owner.EntrySliceRequest(
                {}, {"entry_slices": 2}, str(DAY), False, None, PT.ELC.entry_slice_plan))
            state, detail = quantity.slice_state, quantity.slice_detail
            quantity = owner.entry_quantity(quantity, fill_price=10., num=PT._num,
                                            deployment_max_qty=100, deployment_amount=1000.)
        base.assert_called_once()
        self.assertIs(quantity.sizing, audit)
        self.assertIs(quantity.slice_state, state)
        self.assertIs(quantity.slice_detail, detail)
        self.assertEqual(quantity.qty, 100)
        self.assertEqual(audit["entry_tranche_qty"], 500)
        self.assertEqual(audit["entry_slice_target_qty"], 200)
        self.assertEqual(audit["lifecycle_deployment_clamped_qty"], 100)

    def test_dispatch_clock_preserves_window_evidence_and_trading_contract(self):
        # A future window must remain observable, including at an exception.
        # Clock control must not normalize/drop that field just to fit oracle.
        for name in ("single", "slice_builder_error", "deployment_setting_error"):
            for hour, minute, has_next in ((12, 17, True), (15, 30, False)):
                with self.subTest(scenario=name, hour=hour, minute=minute):
                    moment = dt.datetime.combine(DAY, dt.time(hour, minute))
                    with mock.patch.object(PT.EPD, "_now", side_effect=AssertionError(
                            "ambient dispatch clock leaked")) as ambient, \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        actual = replay_case(name, dispatch_at=moment)
                    ambient.assert_not_called()
                    dispatch = actual["observations"]["slice"]["execution_dispatch"]
                    self.assertEqual(dispatch["next_window"] is not None, has_next)
                    if has_next:
                        self.assertTrue(dispatch["next_window"]["starts_at"].startswith(str(DAY)))
                    if actual["exception_audit"] is not None:
                        self.assertEqual(actual["exception_audit"]["execution_dispatch"], dispatch)
                    for dimension in ("events", "error", "result", "ledger"):
                        self.assertEqual(actual[dimension], self.baseline[name][dimension])


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--record":
    source_sha = subprocess.check_output([
        "git", "-C", str(Path(PT.__file__).resolve().parents[1]), "rev-parse", "HEAD",
    ], text=True).strip()
    if Path(sys.argv[2]).resolve() == ORACLE.resolve() and source_sha != "e622955deb702ae65e101ce260a0e3bea2e0a2f0":
        raise SystemExit("refusing to replace the master oracle with another revision")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cases = {name: replay_case(name) for name in SCENARIOS}
    Path(sys.argv[2]).write_text(json.dumps({"source_sha": source_sha, "cases": cases},
                                          ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {len(cases)} stage-order cases")
elif __name__ == "__main__":
    unittest.main()
