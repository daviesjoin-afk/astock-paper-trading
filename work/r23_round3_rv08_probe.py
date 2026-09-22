# -*- coding: utf-8 -*-
"""诊断：deferred 事务下，竞争者到底卡在哪。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(os.path.join(ROOT, "backend"))

import paper_trading as PT
import strategy_selection_resolver as SRES
import test_provenance_inflight_change as T

T.SignalCommitFencingTests.setUpClass()
case = T.SignalCommitFencingTests(
    "test_RV08_rollover_cannot_land_between_validation_and_first_insert")
case.setUp()

orig = SRES.signal_write_context
comp = {}
calls = []


def hook(*a, **k):
    calls.append(1)
    ctx = orig(*a, **k)
    if not comp:
        import time
        t0 = time.time()
        comp.update(case.try_competing_rollover())
        comp["elapsed"] = round(time.time() - t0, 3)
    return ctx


SRES.signal_write_context = hook
PT.SRES.signal_write_context = hook
try:
    PT.generate_signals(T.D_DAY)
except Exception as exc:
    print("generate_signals raised:", type(exc).__name__, exc)
finally:
    SRES.signal_write_context = orig
    PT.SRES.signal_write_context = orig

print("hook calls:", len(calls))
print("competitor:", comp)
print("account cycle now:", case.account_cycle())
print("cycle_a:", case.cycle_a)
print("rows:", [(r["code"], r["cycle_id"], r["strategy_version"]) for r in case.signal_rows()])
print("audit:", case.audit_events()[-6:])
