# -*- coding: utf-8 -*-
"""Small R26 semantic mutation matrix. Each mutant is a real faulty rule change.

The script serially edits production source, runs one permanent regression anchor,
then restores the exact original bytes and verifies the file hash. It deliberately
has no generic mutation engine.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
PLANNER = "backend/execution_planner.py"
PRELUDE = "\n_REPLAY_PROBE = [0]\n"
EXECUTION_TEST = "test_execution_planner.SimulationExecutionContractTests"


def _execution_test(name: str) -> str:
    return f"{EXECUTION_TEST}.{name}"


MUTATIONS = [
    {
        "id": "M1", "file": PLANNER,
        "changes": [
            (
                """    if side == "sell":
        if context.sellable_quantity is None:
            reasons.append(ExecutionReason.TRADABILITY_UNKNOWN.value)
        elif context.sellable_quantity <= 0:
            reasons.append(ExecutionReason.T1_NOT_SELLABLE.value)""",
                """    if False:  # mutant: skip T+1 sellability guard
        reasons.append(ExecutionReason.T1_NOT_SELLABLE.value)""",
            ),
            (
                """    if side == "sell":
        capacity = min(capacity, max(0, int(context.sellable_quantity or 0)))""",
                """    if side == "sell":
        capacity = min(capacity, remaining)""",
            ),
        ],
        "test": _execution_test("test_t1_and_locked_limit_facts_block_the_relevant_side"),
        "desc": "T+1 不可卖份额被忽略",
    },
    {
        "id": "M2", "file": PLANNER,
        "changes": [(
            "if reading.freshness != MDC.FRESHNESS_FRESH:",
            "if False and reading.freshness != MDC.FRESHNESS_FRESH:",
        )],
        "test": _execution_test("test_stale_market_evidence_blocks_fill"),
        "desc": "过期行情被当作新鲜行情",
    },
    {
        "id": "M3", "file": PLANNER,
        "changes": [("capacity = min(remaining, liquidity_qty)", "capacity = remaining")],
        "test": _execution_test("test_liquidity_caps_fill_and_preserves_remaining_quantity"),
        "desc": "跳过流动性上限，委托量全部成交",
    },
    {
        "id": "M4", "file": PLANNER,
        "changes": [("remaining_quantity=remaining - fill_quantity,", "remaining_quantity=0,")],
        "test": _execution_test("test_liquidity_caps_fill_and_preserves_remaining_quantity"),
        "desc": "部分成交后把剩余数量错误清零",
    },
    {
        "id": "M5", "file": PLANNER,
        "changes": [
            ('"cancelled", "rejected", "expired", "filled", "superseded",',
             '"rejected", "expired", "filled", "superseded",'),
        ],
        "test": _execution_test("test_invalid_lot_and_cancelled_order_cannot_fill"),
        "desc": "撤销委托仍可继续成交",
    },
    {
        "id": "M6", "file": PLANNER,
        # R26 收敛后，行情映射已收敛到 R24 契约；"历史执行不得回退到较新行情日"
        # 的锚点因此落在 planner 传下去的 asof_day 上。
        "changes": [(
            "    day = MDC.canonical_day(asof_day)\n"
            "    snapshot = MDC.symbol_quote_snapshot(quote, asof_day=day)",
            "    day = MDC.canonical_day(quote.get(\"quote_at\"))\n"
            "    snapshot = MDC.symbol_quote_snapshot(quote, asof_day=day)",
        )],
        "test": _execution_test("test_historical_execution_rejects_a_quote_from_a_later_session"),
        "desc": "历史执行忽略请求日期，改用较新的行情日期",
    },
    {
        "id": "M7", "file": PLANNER,
        # R26 收敛后 event key 有了唯一命名 owner；把"重放"错误编码成不同事件
        # 就等于让同一份证据再次成交。
        #
        # 变异必须**确定性**：早先用 ``dt.datetime.now()``，但粗粒度时钟上连续两次
        # ``now()`` 可能完全相等（本机实测连调三次同值），于是这个变异体时灵时不灵，
        # 报 SURVIVED 时其实什么都没测到。同一个语义缺陷改用单调计数器表达，
        # 连续两次 key 推导必然不同，任何正确的不变式测试都必须稳定抓到它。
        "changes": [
            (
                "def _fill_event_key(*, order_id, quote_at, ruleset_version):",
                "def _fill_event_key(*, order_id, quote_at, ruleset_version):\n"
                "    _REPLAY_PROBE[0] += 1  # mutant: replay becomes a new event",
            ),
            (
                '        f"{int(order_id)}|{quote_at}|{ruleset_version}".encode("utf-8")',
                '        f"{int(order_id)}|{quote_at}|{ruleset_version}|'
                '{_REPLAY_PROBE[0]}".encode("utf-8")',
            ),
        ],
        "prelude": PRELUDE,
        "test": "test_r26_convergence_regressions."
                "IdempotencyTests.test_same_quote_observation_cannot_fill_twice",
        "desc": "相同行情重放被错误编码成新成交事件",
    },
    {
        "id": "M8-slippage", "file": PLANNER,
        "changes": [
            ("fill_price = round(estimated_fill_price(reference, side), 2)",
             "fill_price = round(reference, 2)"),
        ],
        "test": _execution_test("test_verified_liquid_quote_produces_deterministic_full_fill_and_fees"),
        "desc": "成交价漏计固定滑点",
    },
    {
        "id": "M8-fees", "file": PLANNER,
        "changes": [
            (
                "fees = round(estimate_execution_fees(amount, side), 2)",
                "fees = 0.0",
            ),
        ],
        "test": _execution_test("test_verified_liquid_quote_produces_deterministic_full_fill_and_fees"),
        "desc": "成交事件漏计手续费",
    },
    {
        "id": "M9", "file": PLANNER,
        "changes": [
            (
                """        if not side_allowed:
            block = getattr(
                tradability,
                "buy_block_reason" if side == "buy" else "sell_block_reason",
                None,
            )
            block_code = getattr(block, "value", str(block or "unknown_state"))
            if block_code == "suspended":
                reasons.append(ExecutionReason.SUSPENDED.value)
            elif block_code in {"buy_limit_locked", "sell_limit_locked"}:
                reasons.append(ExecutionReason.PRICE_LIMIT_LOCKED.value)
            else:
                reasons.append(ExecutionReason.TRADABILITY_UNKNOWN.value)""",
                """        if not side_allowed:
            block = getattr(
                tradability,
                "buy_block_reason" if side == "buy" else "sell_block_reason",
                None,
            )
            block_code = getattr(block, "value", str(block or "unknown_state"))
            if block_code == "suspended":
                reasons.append(ExecutionReason.SUSPENDED.value)
            elif block_code in {"buy_limit_locked", "sell_limit_locked"}:
                pass  # mutant: locked limit is executable
            else:
                reasons.append(ExecutionReason.TRADABILITY_UNKNOWN.value)""",
            ),
        ],
        "test": _execution_test("test_t1_and_locked_limit_facts_block_the_relevant_side"),
        "desc": "涨跌停封板仍允许模拟成交",
    },
    # ── R26 收敛轮（本次修复的四个已确认缺陷 + 血缘/守卫） ──────────────
    {
        "id": "M10-partial-signal", "file": "backend/paper_trading.py",
        "changes": [(
            '        "partially_filled": "partially_filled",',
            '        "partially_filled": "pending",',
        )],
        "test": "test_r26_convergence_regressions."
                "PartialSignalReconciliationTests"
                ".test_partial_buy_fill_keeps_signal_partially_filled_across_reconciliation",
        "desc": "对账把部分的 signal 状态降级回 pending",
    },
    {
        "id": "M11-risk-partial-dedup", "file": "backend/execution_verification.py",
        "changes": [
            (
                '    "((COALESCE(execution_verified, 0) = 1 AND execution_status = \'verified\')"\n'
                '    " OR (execution_verified = 0 AND execution_status = \'partial\'))"',
                '    "((COALESCE(execution_verified, 0) = 1 AND execution_status = \'verified\'))"',
            ),
            (
                '    if status == EXECUTION_STATUS_PARTIAL:\n'
                '        flag = _row_field(row, "execution_verified")\n'
                '        # 部分成交的 flag 必须是精确的 0（SQLite 存成整数 0）。\n'
                '        return flag is not None and not isinstance(flag, (str, bytes)) and flag == 0\n',
                '',
            ),
        ],
        "test": "test_r26_convergence_regressions."
                "RiskPartialDedupTests.test_partial_trim_counts_as_executed_but_full_fill_also_does",
        "desc": "部分成交不参与一次性减仓去重（会重复减仓）",
    },
    {
        "id": "M12-archive-collapse", "file": "backend/paper_trading.py",
        "changes": [(
            'archived_fills.setdefault(archived_fill.get("order_id"), []).append(archived_fill)',
            'archived_fills.setdefault(archived_fill.get("order_id"), [archived_fill])',
        )],
        "test": "test_r26_convergence_regressions."
                "ArchiveMultiFillTests.test_archived_history_keeps_every_partial_fill",
        "desc": "归档把同一订单的多笔成交 collapse 成一笔",
    },
    {
        "id": "M13-cumulative-liquidity", "file": PLANNER,
        "changes": [(
            "    already_consumed = max(0, int(context.same_day_consumed_quantity or 0))",
            "    already_consumed = 0",
        )],
        "test": "test_r26_convergence_regressions."
                "CumulativeLiquidityConsumptionTests"
                ".test_second_event_cannot_re_consume_the_same_cumulative_volume",
        "desc": "同一份累计成交量被重复消费（参与率成倍放大）",
    },
    {
        "id": "M14-manual-remaining", "file": PLANNER,
        "changes": [(
            '        row.get("remaining_qty") if row.get("remaining_qty") is not None\n'
            '        else max(0, int(row.get("qty") or 0) - int(row.get("filled_qty") or 0)),',
            '        max(0, int(row.get("qty") or 0)),',
        )],
        "test": "test_r26_convergence_regressions."
                "ManualRemainingRevalidationTests"
                ".test_revalidate_feeds_remaining_quantity_and_keeps_desired_qty",
        "desc": "手动委托复核使用原始委托量而不是剩余量",
    },
    {
        "id": "M15-execution-signal-dependency", "file": PLANNER,
        "changes": [(
            "    snapshot = MDC.symbol_quote_snapshot(quote, asof_day=day)",
            "    import signal_service as SIG\n"
            "    SIG.signal_evidence(quote or {}, asof_day=day or \"\")\n"
            "    snapshot = MDC.symbol_quote_snapshot(quote, asof_day=day)",
        )],
        "test": "test_r26_execution_authority_guard."
                "ExecutionConsumesOnlyTheMarketDataBoundary"
                ".test_guard15a_execution_planner_does_not_import_signal_service",
        "desc": "执行权威重新依赖 signal 层（架构守卫必须拦住）",
    },
    {
        "id": "M16-session-closing-auction", "file": PLANNER,
        "changes": [(
            '    if current < dt.time(14, 57):\n        return "continuous_afternoon"',
            '    if current < dt.time(15, 0):\n        return "continuous_afternoon"',
        )],
        "test": "test_r26_convergence_regressions."
                "SessionAndTimezoneTests.test_closing_auction_is_not_a_continuous_phase",
        "desc": "收盘集合竞价被当成连续竞价成交",
    },
]

BROKEN_RE = re.compile(
    r"SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError|"
    r"NameError|UnboundLocalError|_FailedTest|AttributeError: module|"
    r"test .* not found|No module named",
    re.IGNORECASE,
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_test(target: str, seq: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = os.path.join(PYCACHE_ROOT, f"run{seq:03d}")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "unittest", target], cwd=BACKEND,
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated mutation ids")
    parser.add_argument("--non-vacuity", action="store_true",
                        help="run each regression anchor once before mutation")
    args = parser.parse_args()
    selected = MUTATIONS
    if args.only:
        wanted = set(args.only.split(","))
        selected = [item for item in MUTATIONS if item["id"] in wanted]
        if {item["id"] for item in selected} != wanted:
            print(f"unknown mutation id(s): {sorted(wanted - {x['id'] for x in selected})}")
            return 2

    originals: dict[str, bytes] = {}
    paths = {item["file"] for item in selected}
    for rel in paths:
        with open(os.path.join(ROOT, rel), "rb") as handle:
            originals[rel] = handle.read()
    hashes = {rel: sha256(raw) for rel, raw in originals.items()}
    global PYCACHE_ROOT
    PYCACHE_ROOT = tempfile.mkdtemp(prefix="r26_mutation_pycache_")
    caught = []
    survived = []
    seq = 0
    try:
        if args.non_vacuity:
            for item in selected:
                seq += 1
                result = run_test(item["test"], seq)
                if result.returncode:
                    print(f"BASELINE FAIL {item['id']} ({item['test']})")
                    print((result.stdout + result.stderr)[-5000:])
                    return 1
                print(f"BASELINE PASS {item['id']} {item['test']}")

        for item in selected:
            rel = item["file"]
            original_text = originals[rel].decode("utf-8")
            mutant_text = original_text
            for old, new in item["changes"]:
                count = mutant_text.count(old)
                if count != 1:
                    print(f"ANCHOR ERROR {item['id']}: expected 1 anchor, found {count}")
                    return 2
                mutant_text = mutant_text.replace(old, new, 1)
            if item.get("prelude"):
                mutant_text += item["prelude"]
            with open(os.path.join(ROOT, rel), "wb") as handle:
                handle.write(mutant_text.encode("utf-8"))
            try:
                seq += 1
                result = run_test(item["test"], seq)
            finally:
                with open(os.path.join(ROOT, rel), "wb") as handle:
                    handle.write(originals[rel])
                with open(os.path.join(ROOT, rel), "rb") as handle:
                    restored = handle.read()
                if sha256(restored) != hashes[rel]:
                    raise RuntimeError(f"restore hash mismatch: {rel}")

            output = result.stdout + result.stderr
            if result.returncode and not BROKEN_RE.search(output):
                caught.append(item["id"])
                print(f"CAUGHT {item['id']}: {item['desc']}")
            elif result.returncode == 0:
                survived.append(item["id"])
                print(f"SURVIVED {item['id']}: {item['desc']}")
            else:
                print(f"FAKE {item['id']}: harness/import failure")
                print(output[-5000:])
                return 2
    finally:
        for rel, raw in originals.items():
            with open(os.path.join(ROOT, rel), "wb") as handle:
                handle.write(raw)
            with open(os.path.join(ROOT, rel), "rb") as handle:
                if sha256(handle.read()) != hashes[rel]:
                    raise RuntimeError(f"final restore hash mismatch: {rel}")

    print(f"RESULT caught={len(caught)} survived={len(survived)} total={len(selected)}")
    return 0 if not survived and len(caught) == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
