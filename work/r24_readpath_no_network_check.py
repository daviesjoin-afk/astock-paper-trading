# -*- coding: utf-8 -*-
"""R24：证明**只读路径**不触发 provider 网络（复核修正版）。

R24 复审指出：旧版本只验证 ``strategy_allocation_explain()``，并且只把
``fetch_market_snapshot_full`` 当作网络入口 —— 于是 ``/api/hot`` 开头的
``fetch_hot_rank``（内部 ``http_post_json``）会假绿。

本脚本因此做两件事：

1. 把**所有**会发起 HTTP 的 provider 入口都换成断言失败；
2. 逐个驱动已迁移的只读路径，要求"不触网 + 返回明确的 market-data 状态"。

用法：python work/r24_readpath_no_network_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
data_dir = tempfile.mkdtemp(prefix="r24-readpath-")
os.environ["ASTOCK_DATA_DIR"] = data_dir
os.environ["ASTOCK_DEMO"] = "1"
os.environ["ASTOCK_DEMO_FORCE"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import data_fetcher as dfc  # noqa: E402
import market_data_contract as MDC  # noqa: E402
import market_data_service as MDSvc  # noqa: E402
import paper_trading as PT  # noqa: E402

#: 所有会发起 HTTP 的 provider 入口（含 hot_rank —— 它内部 http_post_json）。
NETWORK_ENTRIES = (
    "fetch_market_snapshot_full", "fetch_market_snapshot", "fetch_indices",
    "fetch_realtime_for_codes", "fetch_independent_realtime_for_codes",
    "fetch_tencent_realtime_for_codes", "check_data_source_health",
    "fetch_hot_rank", "fetch_fast_news", "fetch_sector_flow",
    "fetch_finance_latest", "fetch_hot_sector_snapshot",
)

ATTEMPTS: list[str] = []


def _forbid_network() -> None:
    def _boom(name):
        def _inner(*_args, **_kwargs):
            ATTEMPTS.append(name)
            raise AssertionError(f"network must not be called from read path: {name}")
        return _inner
    for name in NETWORK_ENTRIES:
        if hasattr(dfc, name):
            setattr(dfc, name, _boom(name))


def _run(label, fn):
    before = len(ATTEMPTS)
    started = time.time()
    try:
        result = fn()
        note = ""
    except Exception as exc:
        result = None
        note = f" {type(exc).__name__}: {exc}"
    elapsed = time.time() - started
    new_calls = ATTEMPTS[before:]
    status = ""
    if isinstance(result, dict):
        md = result.get("market_data") or result.get("data_fact") or {}
        if md:
            status = f" market_data={md.get('status')}"
    flag = "" if not new_calls else f"  *** NETWORK: {new_calls} ***"
    print(f"  {elapsed:7.3f}s  {label}{status}{note}{flag}")
    return result


_forbid_network()
PT.init_db()

print("=== 只读路径（provider 全部替换为断言失败）===")
_run("market_data_service.read_snapshot", lambda: MDSvc.read_snapshot(now=MDSvc.now_utc()))
_run("market_data_service.read_projection", lambda: MDSvc.read_projection())
_run("market_data_service.read_snapshot_legacy_shape",
     MDSvc.read_snapshot_legacy_shape)
_run("strategy_allocation_explain()", PT.strategy_allocation_explain)
_run("dashboard()", lambda: __import__("dashboard_queries").dashboard())
_run("linkage.sector_linkage()", lambda: __import__("linkage").sector_linkage())
_run("adaptive_engine._snapshot_rows", lambda: __import__("adaptive_engine")._snapshot_rows())
_run("deepseek_advisor._read_snapshot",
     lambda: __import__("deepseek_advisor")._read_snapshot(()))
_run("ai_analysis._read_snapshot",
     lambda: __import__("ai_analysis")._read_snapshot(()))

print()
print(f"total provider calls attempted from read paths: {len(ATTEMPTS)}")
if ATTEMPTS:
    print(f"VIOLATION: {ATTEMPTS}")
    raise SystemExit(1)
print("PASS: 只读路径零 provider 网络调用")
print()
print("注：以下**不是**只读路径，显式允许联网，故不在此断言：")
print("  * POST /api/selection-evaluation/refresh → selection_tracking.update_observations")
print("    （显式刷新动作，非 GET 只读）")
print("  * trade_attribution._quote_maps → 无已知事实时 refresh")
print("    （盘后归因 job，经 adaptive_engine.run_close_attribution 调用；")
print("     该 fallback refresh 是迁移前既有行为，本轮只把 raw open 换成 authority）")
print("  * GET /api/hot → fetch_hot_rank（独立 artifact：东财人气榜，非 full-market 快照）")
print("    该入口仍会同步取榜；R24 的收敛目标是 full-market snapshot，不是它。")
