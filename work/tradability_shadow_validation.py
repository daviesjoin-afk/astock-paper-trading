# -*- coding: utf-8 -*-
"""Shadow Tradability Validation —— **只读**操作员工具。

用法::

    # 单 session，指定代码
    python work/tradability_shadow_validation.py --session 2025-06-10 --codes 000001,600000

    # 日期范围（按 A 股交易日历过滤）
    python work/tradability_shadow_validation.py --from 2025-06-01 --to 2025-06-30 --limit 50

    # 固定知识时点（"我们站在哪一天的知识上做这次验证"）
    python work/tradability_shadow_validation.py --session 2025-06-10 \
        --validation-as-of 2026-09-17

``--validation-as-of`` 缺省 = 当前知识时点（看全部已有观察）。显式给出时只使用
``recorded_at <= validation_as_of`` 的观察事件：晚于该时点才被摄取的事实对这次验证
不可见，因此同一条历史决策在不同知识时点的验证是两个不同的快照（身份里含它）。

本工具**只**做 argument parsing / 格式化 / exit code，全部比对逻辑在
:mod:`backend.tradability_shadow`，scope 解析在 :mod:`backend.tradability_backfill`
（因此与回填 CLI 共享同一套 operator scope 契约：显式空 ``--codes`` 报错、零交易日
范围报错、权威交易日历）。

**零 authority**：本工具没有 ``--apply`` / ``--enforce`` / ``--switch-authority``，
不会改变任何订单、成交、持仓、选股或学习行为。它只输出观察结果。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import data_paths  # noqa: E402
import security_state_point_in_time as SS  # noqa: E402
import selection_tradability as ST  # noqa: E402
import tradability_archive as TA  # noqa: E402
import tradability_backfill as TB  # noqa: E402
import tradability_observation_ledger as OL  # noqa: E402
import tradability_shadow as TS  # noqa: E402


def _kline(code):
    """共享 K 线缓存 → ``{date: {close, volume}}``（与回填同一 reader）。"""
    return TB.build_kline_reader()(code)


def _previous_close(kline_map, session):
    """``session`` 之前最近一个交易日的收盘价（涨跌停参考价）。

    只读**该 session 之前**的 bar，绝不使用当日或之后的成交信息。
    """
    if not kline_map or session is None:
        return None
    prior = [day for day in sorted(kline_map) if day < str(session)[:10]]
    if not prior:
        return None
    close = kline_map[prior[-1]].get("close")
    return None if close is None else float(close)


def _production_evidence(code, session, kline_map, state_fn):
    """按**生产口径**构造该动作 session 的证据（与选股报告同一套适配）。

    ``halted`` 与名称 / 风险标记的口径：有历史状态源时用**该 session 当时**的状态；
    没有时留 ``None`` → 契约判 ``unproven / unknown_st_status``（fail closed），
    **绝不**用当前快照的名称把历史动作"证明"成可执行。
    """
    close = kline_map.get(session, {}).get("close") if kline_map else None
    name = risk_flag = None
    if state_fn is not None:
        state = state_fn(str(code or ""), str(session)[:10])
        if isinstance(state, dict):
            name, risk_flag = state.get("name"), state.get("risk_flag")
        elif state is not None:
            name = state
    return ST.MarketEvidence(
        session=str(session)[:10],
        available_at=ST.session_close_at(str(session)[:10]),
        price=close,
        reference_price=_previous_close(kline_map, session),
        halted=False if close is not None else None,
        name=name,
        risk_flag=risk_flag,
    )


def _production_verdict(code, session, side, kline_map, state_fn):
    """**真实生产判定**：直接调用生产路径在用的 ``entry_/exit_tradability``。

    本工具不复制任何生产规则——它把生产函数拿来跑，拿回它的 verdict。

    **卖出方向不传 ``entry_session``**：这个 CLI 手上没有真实的持仓入场 session，
    而生产契约里 ``entry_session`` 一旦给出就会做 T+1 校验。伪造 ``entry_session =
    session``（"当天买入当天卖"）会让普通 T+1 证券被判 ``t1_not_sellable``，于是默认
    运行的卖出半边会报出一批**假的** ``production_block_archive_allow`` 分歧。
    本 CLI 比较的是**市场层面的可交易性**，T+1 属于持仓层面，因此这里诚实地不声明
    入场时点（不传 = 不做 T+1 判定），而不是编一个出来。
    """
    evidence = _production_evidence(code, session, kline_map, state_fn)
    if side == ST.SIDE_BUY:
        return ST.entry_tradability(evidence, code=code, entry_session=session)
    return ST.exit_tradability(evidence, code=code, exit_session=session)


def _pair_has_archive_row(conn, code, session):
    """归档里是否存在该 ``(code, session)`` 的**任何**行。

    这**只**用于一个诊断标签：升级前就已存在的历史数据（archive 有行、台账无任何事件）
    其真实 ``first_seen_at`` 无法反推，只能诚实标 ``legacy_observation_unknown``。
    它问的是"这条 pair 有没有事实行"，**不是**"它当时可不可见"——后者由 archive 的
    ``tradability_at`` 在 ``decision_at`` 下回答。
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM historical_tradability_archive "
            "WHERE code=? AND session_date=? LIMIT 1",
            (str(code), str(session)[:10]),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Shadow Tradability Validation（只读；不改变任何交易行为）"
    )
    parser.add_argument("--session", help="单个 session（YYYY-MM-DD）")
    parser.add_argument("--from", dest="from_date", help="日期范围起点（YYYY-MM-DD）")
    parser.add_argument("--to", dest="to_date", help="日期范围终点（YYYY-MM-DD）")
    parser.add_argument("--codes", help="逗号分隔的代码列表；缺省用当前 universe")
    parser.add_argument("--limit", type=int, default=None, help="只比对前 N 只（调试）")
    parser.add_argument("--side", choices=list(ST.SIDES), default=None, help="只比对某一方向")
    parser.add_argument(
        "--validation-as-of",
        dest="validation_as_of",
        default=None,
        help="知识时点（YYYY-MM-DD 或完整时间戳）；缺省=当前知识时点",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    start, end = args.from_date, args.to_date
    if args.session and (start or end):
        print("--session 与 --from/--to 互斥")
        return 2
    if not args.session and not start:
        print("需要 --session 或 --from")
        return 2

    # 与回填 CLI **共享**同一套 operator scope 契约（显式空 --codes / 零交易日范围
    # 一律 operator error），绝不在这里再犯一遍。
    try:
        sessions = TB.resolve_sessions(start, end, session=args.session)
        codes = TB.resolve_codes(args.codes, limit=args.limit)
    except TB.ScopeError as exc:
        print(f"scope 错误: {exc}")
        return 2
    if not codes:
        print("没有可比对的代码（universe 为空）")
        return 2

    db_path = data_paths.data_path("paper_trading.sqlite3")
    if not os.path.exists(db_path):
        print(f"数据库不存在（跳过）: {db_path}")
        return 2

    state_fn, state_status = SS.resolve_security_state_fn()
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        repo = TA.TradabilityArchiveRepository(conn)
        # 台账提供"我们什么时候第一次看到这条 pair"这一**不可变历史序列**。它只用于
        # 分类 archive_missing / archive_unprovable 与诊断，绝不参与 archive verdict。
        ledger = OL.ObservationLedgerRepository(conn)
        comparator = TS.ShadowComparator(repo, ledger=ledger)
        sides = [args.side] if args.side else list(ST.SIDES)
        items = []
        kline_cache: dict = {}
        for session in sessions:
            for code in codes:
                if code not in kline_cache:
                    kline_cache[code] = _kline(code)
                kline_map = kline_cache[code]
                for side in sides:
                    verdict = _production_verdict(
                        code, session, side, kline_map, state_fn
                    )
                    items.append(
                        {
                            "production_verdict": verdict,
                            "code": code,
                            "session": session,
                            "side": side,
                            "decision_at": ST.session_close_at(session),
                            # 知识时点：显式给出时只使用 ``recorded_at <= as_of`` 的
                            # 观察事件。它同时进入比对身份，因此 2026-09-17 与 2026-10-01
                            # 的验证各自成行，今天新摄取一条观察不会让昨天的比对冲突。
                            "validation_as_of": args.validation_as_of,
                        }
                    )
        comparisons = comparator.compare_many(items)
        summary = comparator.summarize(comparisons)
    finally:
        conn.close()

    if args.json:
        print(
            json.dumps(
                {
                    "summary": summary.to_dict(),
                    "comparisons": [c.to_dict() for c in comparisons],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("=== shadow tradability validation (READ-ONLY / observation only) ===")
    print(f"session_state_source: {json.dumps(state_status, ensure_ascii=False)}")
    print(f"codes: {len(codes)}  sessions: {len(sessions)}  sides: {len(sides)}")
    data = summary.to_dict()
    for key in (
        "requested", "comparable", "not_comparable",
        "agree", "disagree",
        "agree_allow", "agree_block",
        "production_allow_archive_block", "production_block_archive_allow",
        "archive_unknown", "archive_unprovable", "archive_missing",
        "production_unknown", "comparison_invalid",
        "comparison_rate", "agreement_rate", "disagreement_rate",
    ):
        print(f"{key}: {data[key]}")
    print(f"validation_as_of: {args.validation_as_of or '(current)'}")
    by_diagnostic: dict = {}
    by_provider_outcome: dict = {}
    late_observed = 0
    for comparison in comparisons:
        if comparison.archive_diagnostic:
            key = comparison.archive_diagnostic
            by_diagnostic[key] = by_diagnostic.get(key, 0) + 1
        for outcome, count in (comparison.provider_outcomes or {}).items():
            by_provider_outcome[outcome] = by_provider_outcome.get(outcome, 0) + count
        if comparison.status == TS.ShadowStatus.ARCHIVE_UNPROVABLE.value:
            late_observed += 1
    print("archive_diagnostic:", json.dumps(by_diagnostic, ensure_ascii=False))
    print("provider_outcomes:", json.dumps(by_provider_outcome, ensure_ascii=False))
    print(f"late_observed (archive_unprovable): {late_observed}")
    for comparison in comparisons[:5]:
        print(
            "  sample: "
            f"{comparison.code} {comparison.session} {comparison.side} "
            f"status={comparison.status} "
            f"first_observed_at={comparison.first_observed_at} "
            f"market_provable_at_decision={comparison.market_provable_at_decision} "
            f"system_possessed_at_decision={comparison.system_possessed_at_decision}"
        )
    print("by_side:", json.dumps(data["by_side"], ensure_ascii=False))
    print("by_production_reason:", json.dumps(data["by_production_reason"], ensure_ascii=False))
    print("by_archive_reason:", json.dumps(data["by_archive_reason"], ensure_ascii=False))
    print("archive 缺口不计入 disagreement：见 archive_unknown / archive_unprovable / archive_missing。")
    print("Shadow 只是观察，零 execution authority。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
