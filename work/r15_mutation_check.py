# -*- coding: utf-8 -*-
"""R15 负向变异矩阵（M-RD1 ~ M-RD12）。

每条变异都对应一条 R15 / 架构契约，必须让**对应**契约测试变红——否则门禁是空的。

流程（每条变异，严格逐字节备份/还原）::

    1. 读取目标文件字节 + sha256；
    2. 断言变异锚点存在（not found = 立即失败，绝不静默跳过）；
    3. 注入变异（bytes 级替换，只替换第一处）；
    4. 用仓库真实测试套件跑对应契约测试（``python -m unittest``）；
    5. 断言 RED：退出码非 0，且输出中目标测试以 FAIL:/ERROR: 出现
       （non-vacuity —— 失败必须来自对应契约，而不是 import/收集错误）；
    6. finally 逐字节还原，sha256 必须与原文件一致。

变异矩阵::

    M-RD1   bought_today 忽略 entry_date          -> RD-01 同日新仓识别
    M-RD2   asof_day=None 回退机器当天            -> RD-02 显式 as-of（wall-clock 泄漏）
    M-RD3   同日新仓峰值吸收买入前 high           -> RD-03 同日峰值口径
    M-RD4   隔夜仓峰值丢掉日内 high               -> RD-04 隔夜口径
    M-RD5   退出严重度仲裁失效（后者覆盖）        -> RD-13 严重度序
    M-RD6   硬止损首触直接全清                    -> RD-10 首段减仓
    M-RD7   未知 take_stage 被当成已知            -> RD-14 档位不可证明不得卖
    M-RD8   limit_pct 缺失被静默兜底              -> RD-07 调用方必须交齐执行参数
    M-RD9   移动止损不看回撤                      -> RD-12 峰值回撤判定
    M-RD10  无报价被当成可决策                    -> RD-08 no_quote 短路
    M-RD11  paper_trading 重新长出 _position_peak -> 架构 Guard 2b（纯 helper 回流）
    M-RD12  _sell_plan 不再把 asof 传给引擎       -> 架构 Guard 6e（R15 缺陷重现口）

用法（仓库根目录）::

    .venv/Scripts/python.exe work/r15_mutation_check.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
TEST_MODULE = "test_paper_risk_decision"
GUARD_MODULE = "test_paper_trading_architecture_guard"
DECISION_FILE = "backend/paper_risk_decision.py"
PAPER_TRADING_FILE = "backend/paper_trading.py"


def b(text: str) -> bytes:
    return text.encode("utf-8")


MUTATIONS = [
    {
        "id": "M-RD1",
        "file": DECISION_FILE,
        "old": '    return (\n'
               '        qty > 0 and today_qty >= qty\n'
               '        and str(position.get("entry_date") or "") == asof\n'
               '    )\n',
        "new": '    return qty > 0 and today_qty >= qty  # mutation: 丢掉 entry_date 判定\n',
        "test": f"{TEST_MODULE}.BoughtTodayTests.test_rd01_bought_today_semantics",
        "desc": "bought_today 忽略 entry_date（隔夜仓被误判成同日新仓）",
    },
    {
        "id": "M-RD2",
        "file": DECISION_FILE,
        "old": '    if asof_day is None:\n'
               '        raise ValueError(\n'
               '            "risk decision 需要显式 asof_day：不允许回退到机器当前日期"\n'
               '        )\n',
        "new": '    if asof_day is None:\n'
               '        asof_day = dt.date.today()  # mutation: wall-clock 回退\n',
        "test": f"{TEST_MODULE}.ExplicitAsofTests.test_rd02_missing_asof_fails_fast",
        "desc": "asof_day=None 静默回退到机器当天（R15 缺陷的根源）",
    },
    {
        "id": "M-RD3",
        "file": DECISION_FILE,
        "old": '    if bought_today(position, asof_day=asof_day):\n'
               '        return max(authoritative, price or 0.0)\n',
        "new": '    if bought_today(position, asof_day=asof_day):\n'
               '        return max(authoritative, _num(quote.get("high"), 0.0), price or 0.0)\n',
        "test": f"{TEST_MODULE}.PositionPeakTests."
                "test_rd03_same_day_new_position_ignores_pre_entry_high",
        "desc": "同日新仓峰值吸收买入前的日内 high",
    },
    {
        "id": "M-RD4",
        "file": DECISION_FILE,
        "old": '    return max(authoritative, _num(quote.get("high"), 0.0), price or 0.0)\n',
        "new": '    return max(authoritative, price or 0.0)  # mutation: 丢掉日内 high\n',
        "test": f"{TEST_MODULE}.PositionPeakTests."
                "test_rd04_overnight_position_absorbs_intraday_high",
        "desc": "隔夜仓峰值丢掉日内 high（回撤被低估）",
    },
    {
        "id": "M-RD5",
        "file": DECISION_FILE,
        "old": '        if EXIT_SEVERITY[new_class] > EXIT_SEVERITY.get(exit_class, 0):\n',
        "new": '        if True:  # mutation: 严重度仲裁失效，后者覆盖前者\n',
        "test": f"{TEST_MODULE}.ExitSeverityTests.test_rd13_hard_stop_outranks_max_hold",
        "desc": "退出严重度仲裁失效（硬止损被较弱类别覆盖）",
    },
    {
        "id": "M-RD6",
        "file": DECISION_FILE,
        "old": '        if crash_tape or hard_stop_touched_today:\n',
        "new": '        if True:  # mutation: 首触硬止损直接全清\n',
        "test": f"{TEST_MODULE}.HardStopTests.test_rd10_first_touch_trims_before_clearing",
        "desc": "硬止损首触直接清仓（单针探底被卖在最低点）",
    },
    {
        "id": "M-RD7",
        "file": DECISION_FILE,
        "old": '    raw_stage = position.get("take_stage")\n'
               '    stage_known = raw_stage is not None\n'
               '    next_stage = int(raw_stage) if stage_known else 0\n',
        "new": '    raw_stage = position.get("take_stage")\n'
               '    stage_known = True  # mutation: 未知档位被当成已知\n'
               '    next_stage = int(raw_stage or 0)\n',
        "test": f"{TEST_MODULE}.StagedTakeProfitTests."
                "test_rd14_unknown_stage_skips_and_known_stage_consumes_levels",
        "desc": "未知 take_stage 被当成 stage 0（档位不可证明却照样卖）",
    },
    {
        "id": "M-RD8",
        "file": DECISION_FILE,
        "old": '    if limit_pct is None:\n'
               '        raise ValueError("evaluate_sell 需要显式 limit_pct（由调用方解析）")\n',
        "new": '    limit_pct = 0.0 if limit_pct is None else limit_pct  # mutation: 静默兜底\n',
        "test": f"{TEST_MODULE}.EvaluateSellContractTests."
                "test_rd07_limit_pct_must_be_resolved_by_caller",
        "desc": "limit_pct 缺失被静默兜底（engine 自行猜涨跌停）",
    },
    {
        "id": "M-RD9",
        "file": DECISION_FILE,
        "old": '    if ret >= spec["trail_after"] and drawdown is not None '
               'and drawdown >= spec["trail_stop"]:\n',
        "new": '    if ret >= spec["trail_after"]:  # mutation: 不看回撤\n',
        "test": f"{TEST_MODULE}.TrailingStopTests.test_rd12_trailing_stop_fires_on_peak_drawdown",
        "desc": "移动止盈只要浮盈就清仓（回撤条件丢失）",
    },
    {
        "id": "M-RD10",
        "file": DECISION_FILE,
        "old": '            "status": "no_quote",\n',
        "new": '            "status": "decided",  # mutation: 无报价也当可决策\n',
        "test": f"{TEST_MODULE}.EvaluateSellContractTests.test_rd08_missing_quote_is_a_no_op",
        "desc": "无有效报价被当成可决策（缺少报价仍然卖出）",
    },
    {
        "id": "M-RD11",
        "file": PAPER_TRADING_FILE,
        "old": 'def _sell_plan(position, quote, asof_day, news, hard_stop_touched_today=False, '
               'spec_override=None):\n',
        "new": 'def _position_peak(position, quote, price, asof_day=None):\n'
               '    # mutation: 纯 helper 回流到 god module\n'
               '    return _num(quote.get("high"), 0.0)\n'
               '\n'
               '\n'
               'def _sell_plan(position, quote, asof_day, news, hard_stop_touched_today=False, '
               'spec_override=None):\n',
        "test": f"{GUARD_MODULE}.RiskStateCrudStaysInItsOwner."
                "test_guard2b_forbidden_forwarding_wrappers_do_not_come_back",
        "desc": "架构：_position_peak 回流 paper_trading（纯决策再被 god module 吸收）",
    },
    {
        "id": "M-RD12",
        "file": PAPER_TRADING_FILE,
        "old": '        position, quote, asof_day=asof_day, spec=spec, hold_days=days, news=news,\n',
        "new": '        position, quote, spec=spec, hold_days=days, news=news,\n',
        "test": f"{GUARD_MODULE}.RiskDecisionModuleIsDeterministic."
                "test_guard6e_sell_plan_delegates_to_the_pure_engine",
        "desc": "架构：_sell_plan 不再把显式 as-of 传给决策引擎",
    },
]


def run_test(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unittest", target],
        cwd=BACKEND, capture_output=True, text=True, timeout=300,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    print(f"repo root: {ROOT}")
    for module in (TEST_MODULE, GUARD_MODULE):
        base = run_test(module)
        if base.returncode != 0:
            print(f"BASELINE FAILED —— {module} 必须全绿，终止。")
            print(base.stdout[-2000:], base.stderr[-2000:])
            return 2
    print(f"BASELINE: {TEST_MODULE} + {GUARD_MODULE} all green\n")

    results = []
    for mut in MUTATIONS:
        path = os.path.join(ROOT, mut["file"])
        with open(path, "rb") as fh:
            original = fh.read()
        original_hash = sha256(original)
        try:
            candidates = []
            for style in ("\n", "\r\n"):
                if style == "\r\n" and b"\r\n" not in original:
                    continue
                old_b = b(mut["old"].replace("\n", style))
                if old_b in original:
                    candidates.append((old_b, b(mut["new"].replace("\n", style))))
            if not candidates:
                raise AssertionError(f"变异锚点未找到: {mut['id']} in {mut['file']}")
            old_b, new_b = candidates[0]
            injected = original.replace(old_b, new_b, 1)
            if injected == original:
                raise AssertionError(f"变异未产生任何字节变化: {mut['id']}")
            with open(path, "wb") as fh:
                fh.write(injected)

            proc = run_test(mut["test"])
            output = (proc.stdout or "") + (proc.stderr or "")
            red = proc.returncode != 0
            method = mut["test"].split(".")[-1]
            marker = f"FAIL: {method}" in output or f"ERROR: {method}" in output
            status = "RED" if (red and marker) else "SUSPECT"
            results.append((mut["id"], mut["desc"], status, mut["test"]))
            print(f"[{mut['id']}] {mut['desc']}")
            print(f"    -> {status} (rc={proc.returncode}, contract_failure={marker})")
        finally:
            with open(path, "wb") as fh:
                fh.write(original)
            with open(path, "rb") as fh:
                restored = fh.read()
            assert sha256(restored) == original_hash, f"还原后 sha256 不一致: {mut['file']}"

    print("\n===== R15 mutation summary =====")
    failed = [r for r in results if r[2] != "RED"]
    for mid, desc, status, _test in results:
        print(f"  {mid:<8} {status:<8} {desc}")
    if failed:
        print(f"\nRESULT: {len(failed)} mutation(s) NOT confirmed -> DO NOT TRUST GATE")
        return 1
    print(f"\nRESULT: {len(results)}/{len(results)} mutations RED, "
          "all files restored byte-identical")
    return 0


if __name__ == "__main__":
    del mock  # 本脚本不做进程内桩；保留 import 供将来的 dry-run 扩展
    raise SystemExit(main())
