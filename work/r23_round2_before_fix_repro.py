# -*- coding: utf-8 -*-
"""R23 round 2 —— BEFORE 复现探针（仅本地证据，不提交）。

把 authority 临时改回**修复前**的写法，再跑本轮的 production regression：

* Blocker A：``signal_write_context`` 改成旧行为 —— 忽略调用方捕获的 cycle，
  直接 ``SELECT cycle_id FROM paper_accounts``（这正是 PR #181 修复前的
  ``signal_cycle_provenance``）。
* Blocker B：``_pin_research_version`` 移到 ``_run_one`` 之后（等于
  ``_run_provenance`` 自己读 current head）。

不修改任何生产文件：用模块级 monkeypatch 在同一进程内制造旧行为。
"""
from __future__ import annotations

import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(BACKEND) == "work":
    BACKEND = os.path.join(os.path.dirname(BACKEND), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import paper_selection as PS
import paper_trading as PT
import strategy_registry as SR
import strategy_selection_provenance as SP
import strategy_selection_resolver as SRES

REPORT = []


def _legacy_signal_write_context(conn, account_id, *, cycle_id, account_cycle_id,
                                 asof_day=""):
    """修复前 authority：重新从 paper_accounts 解析周期（忽略调用方的捕获）。"""
    row = conn.execute(
        "SELECT cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
    ).fetchone()
    derived = SP.canonical_cycle_id(row[0] if row is not None else None)
    if derived is None:
        raise SRES.SignalCycleUnprovable(account_id, None, "account has no durable cycle")
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=derived)
    if stamp is None:
        raise SRES.SignalCycleUnprovable(
            account_id, derived, "cycle has no pinned immutable strategy version")
    return SRES.SignalWriteContext(
        account_id=account_id, cycle_id=derived, strategy_id=stamp[0],
        strategy_version=stamp[1], strategy_checksum=stamp[2], asof_day=asof_day)


def _legacy_research_provenance(conn, strategy_id, *, asof_day, scope=None, pin=None):
    """修复前行为：**忽略**计算前的 pin，自己读 current head。

    这正是 HEAD 里 ``research_provenance`` 的写法 —— provenance 解析时才问
    「现在是哪一版」，于是计算期间发布的版本被记成产出该结果的那一版。
    """
    del pin
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return SRES._unproven("no strategy id supplied")
    if SP.canonical_day(asof_day) is None:
        return SRES._unproven("missing or invalid as-of day", strategy_id)
    head = SR.get_version(strategy_id, conn=conn)
    if head is None:
        return SRES._unproven(f"strategy {strategy_id} has no immutable version", strategy_id)
    provenance = SP.StrategySelectionProvenance(
        strategy_id=head.strategy_id, strategy_version=head.version,
        strategy_checksum=head.checksum, asof_day=asof_day,
        scope=scope or SP.SCOPE_RESEARCH,
    )
    return SP.ProvenanceReading(provenance, SP.STATUS_VERIFIED, "", strategy_id)


def install_legacy_authority():
    SRES.signal_write_context = _legacy_signal_write_context
    PT.SRES.signal_write_context = _legacy_signal_write_context
    SRES.research_provenance = _legacy_research_provenance
    PS.SRES.research_provenance = _legacy_research_provenance


def main():
    install_legacy_authority()
    import test_provenance_inflight_change as T

    suite = unittest.TestSuite()
    for name in (
        "test_RV01_close_signal_rollover_does_not_restamp_old_candidates",
        "test_RV03_bootstrap_signal_rollover_aborts_the_stale_account_batch",
        "test_RV07_inflight_version_publication_does_not_change_the_run_stamp",
    ):
        cls = ("SignalCycleRolloverTests" if name.startswith("test_RV01")
               else "BootstrapCycleRolloverTests" if name.startswith("test_RV03")
               else "ResearchVersionInflightTests")
        suite.addTest(getattr(T, cls)(name))

    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)
    print("\n=== BEFORE 复现汇总 ===")
    print(f"run={result.testsRun} failures={len(result.failures)} "
          f"errors={len(result.errors)}")
    for case, detail in result.failures + result.errors:
        first = [line for line in detail.strip().splitlines() if line.strip()][-1]
        print(f"  RED {case.id().split('.')[-1]}: {first[:200]}")
    expected_red = 3
    if result.testsRun == expected_red and len(result.failures) + len(result.errors) == expected_red:
        print("VERDICT: BEFORE reproduced — 3/3 RED")
        return 0
    print("VERDICT: BEFORE NOT reproduced（探针本身有问题，不能当证据）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
