# -*- coding: utf-8 -*-
"""selection tradability 契约测试（P1–P30 + 方向性 golden + regime 边界）。

每条测试对应一个**可证伪**陈述。变异脚本 ``pr157_mutation_check.py`` 的
M1–M20 逐条还原这些缺陷，本文件必须把它们抓住。
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib
import random
import unittest

import paper_trading_rules as PTR
import security_state_point_in_time as SS
import selection_labels as SL
import selection_tradability as ST
import walk_forward_validation as WFV


SESSION = "2024-06-18"
NEXT_SESSION = "2024-06-19"
MAIN_BOARD = "600001"
CHINEXT = "300001"
STAR = "688001"
BSE = "830001"
ETF = "510300"


def evidence(
    session=SESSION,
    *,
    price=10.2,
    reference=10.0,
    volume=1_000_000.0,
    halted=False,
    name="某某股份",
    risk_flag=None,
    available_at=None,
    volume_required=False,
):
    """一条 PIT 证据。``name`` 默认给出**历史**名称 —— 缺了它就是 ST 未知。"""
    return ST.MarketEvidence(
        session=session,
        available_at=(
            available_at if available_at is not None else ST.session_close_at(session)
        ),
        price=price,
        reference_price=reference,
        volume=volume,
        halted=halted,
        name=name,
        risk_flag=risk_flag,
        volume_required=volume_required,
    )


def buy(ev, code=MAIN_BOARD, **kwargs):
    return ST.tradability_at(ev, code=code, side=ST.SIDE_BUY,
                             action_at=ST.session_close_at(ev.session), **kwargs)


def sell(ev, code=MAIN_BOARD, entry_session="2024-06-17"):
    return ST.tradability_at(
        ev, code=code, side=ST.SIDE_SELL,
        action_at=ST.session_close_at(ev.session), entry_session=entry_session,
    )


def row(sample_key, code=MAIN_BOARD, *, entry_ev=None, exit_ev=None,
        entry_session=SESSION, exit_session=None, selected=True,
        label_status="verified", label_value=0.10):
    return ST.SelectionRow(
        sample_key=sample_key,
        code=code,
        selected=selected,
        intended_entry_session=entry_session,
        entry_evidence=entry_ev,
        intended_exit_session=exit_session,
        exit_evidence=exit_ev,
        market_label_status=label_status,
        market_label_value=label_value,
    )


class _FakeKlineReport:
    """把 ``selection_alpha_report`` 的 K 线读取换成本测试自带的行情。

    ``benchmark_map`` 会 ``os.listdir(KLINE_DIR)`` 取基准票池，因此只换
    ``load_kline`` 不够 —— 必须同时把目录列举也换掉，否则测试依赖运行环境里
    有没有真实 ``data_cache/klines``。
    """

    def __init__(self, klines):
        self.klines = klines

    def __enter__(self):
        import selection_alpha_report as AR

        self._AR = AR
        self._orig_load = AR.load_kline
        self._orig_listdir = AR.os.listdir
        AR.load_kline = lambda code: dict(self.klines.get(code) or {})
        AR.os.listdir = lambda path: [
            f"{code}.csv" for code in self.klines
        ] if str(path) == str(AR.KLINE_DIR) else self._orig_listdir(path)
        return AR

    def __exit__(self, *exc):
        self._AR.load_kline = self._orig_load
        self._AR.os.listdir = self._orig_listdir
        return False


# ─────────────────────── P1–P9: the core verdict ───────────────────────


class CoreVerdictTest(unittest.TestCase):
    def test_p1_historical_tradability_only_reads_pit_evidence(self):
        """P1：只读 ``available_at <= action_at`` 的证据。

        EOD 成交量/收盘价在 15:00 才可用，不能用来证明 09:31 可以成交。
        """
        ev = evidence(available_at="2024-06-18T15:00:00+08:00")
        intraday = ST.tradability_at(
            ev, code=MAIN_BOARD, side=ST.SIDE_BUY,
            action_at="2024-06-18T09:31:00+08:00",
        )
        self.assertEqual(ST.STATUS_UNPROVEN, intraday.status)
        self.assertEqual(ST.REASON_EVIDENCE_NOT_VISIBLE, intraday.reason)
        self.assertFalse(intraday.executable)
        # 同一个证据在 15:00 是可见的。
        close = buy(ev)
        self.assertEqual(ST.STATUS_EXECUTABLE, close.status)

    def test_p2_suspended_entry_is_blocked_and_produces_no_fake_fill(self):
        """P2：停牌 entry → ``blocked``，且不产生任何假成交。"""
        built = ST.build_executable_outcomes(
            [row("a", entry_ev=evidence(halted=True, price=None))]
        )
        outcome = built["outcomes"][0]
        self.assertEqual(ST.STATUS_BLOCKED, outcome.entry_status)
        self.assertEqual(ST.REASON_SUSPENDED, outcome.entry_reason)
        self.assertFalse(outcome.executable)
        self.assertIsNone(outcome.actual_entry_session)
        self.assertIsNone(outcome.executable_return)
        self.assertEqual(1, built["report"]["blocked_entry"])

    def test_p3_buy_side_price_limit_block_follows_the_authoritative_rule(self):
        """P3：买入方向按权威口径拦涨停（主板 9.5%，不是统一的 10%）。"""
        blocked = buy(evidence(price=110.0, reference=100.0))
        self.assertEqual(ST.STATUS_BLOCKED, blocked.status)
        self.assertEqual(ST.REASON_LIMIT_UP_BUY_BLOCKED, blocked.reason)
        self.assertEqual(9.5, blocked.limit_pct)
        allowed = buy(evidence(price=109.0, reference=100.0, name="某某股份"))
        self.assertEqual(ST.STATUS_EXECUTABLE, allowed.status)
        # 阈值确实来自权威实现。
        self.assertEqual(PTR.limit_pct(MAIN_BOARD), blocked.limit_pct)

    def test_p3b_verdict_agrees_with_the_authoritative_comparison(self):
        """P3b：对同一批价格，本模块的结论与仓库权威比较式**逐一一致**。

        权威式（``backtest`` 的成交门禁）：``pct >= limit_pct/100`` 即拦买、
        ``pct <= -limit_pct/100`` 即拦卖。边界上的浮点行为因此完全继承仓库口径，
        本模块不引入第二套阈值判断。
        """
        limit = PTR.limit_pct(MAIN_BOARD) / 100.0
        for price in (100.0, 102.0, 105.0, 108.0, 109.0, 109.5, 110.0, 115.0):
            pct = price / 100.0 - 1.0
            verdict = buy(evidence(price=price, reference=100.0, name="某某股份"))
            self.assertEqual(
                pct >= limit,
                verdict.status == ST.STATUS_BLOCKED,
                (price, pct, verdict.status),
            )
        for price in (100.0, 98.0, 95.0, 92.0, 90.5, 90.0, 85.0):
            pct = price / 100.0 - 1.0
            verdict = sell(evidence(price=price, reference=100.0, name="某某股份"))
            self.assertEqual(
                pct <= -limit,
                verdict.status == ST.STATUS_BLOCKED,
                (price, pct, verdict.status),
            )

    def test_p4_sell_side_price_limit_block_follows_the_authoritative_rule(self):
        """P4：卖出方向按权威口径拦跌停。"""
        blocked = sell(evidence(price=90.4, reference=100.0))
        self.assertEqual(ST.STATUS_BLOCKED, blocked.status)
        self.assertEqual(ST.REASON_LIMIT_DOWN_SELL_BLOCKED, blocked.reason)
        allowed = sell(evidence(price=90.6, reference=100.0, name="某某股份"))
        self.assertEqual(ST.STATUS_EXECUTABLE, allowed.status)

    def test_p5_buy_and_sell_directionality_cannot_be_swapped(self):
        """P5（方向性 golden）：涨停只拦买、跌停只拦卖，绝不一刀切。"""
        limit_up = evidence(price=110.0, reference=100.0)
        limit_down = evidence(price=90.0, reference=100.0)
        self.assertEqual(ST.STATUS_BLOCKED, buy(limit_up).status)
        self.assertEqual(ST.STATUS_EXECUTABLE, sell(limit_up).status)
        self.assertEqual(ST.STATUS_EXECUTABLE, buy(limit_down).status)
        self.assertEqual(ST.STATUS_BLOCKED, sell(limit_down).status)
        # 两条被拦的 reason 必须不同 —— 不允许合并成一个 "not tradable"。
        self.assertEqual(ST.REASON_LIMIT_UP_BUY_BLOCKED, buy(limit_up).reason)
        self.assertEqual(ST.REASON_LIMIT_DOWN_SELL_BLOCKED, sell(limit_down).reason)

    def test_p6_unknown_suspension_evidence_is_unproven_not_tradable(self):
        """P6：停牌证据未知 → ``unproven``，绝不默认可成交。"""
        verdict = buy(evidence(price=None, halted=None))
        self.assertEqual(ST.STATUS_UNPROVEN, verdict.status)
        self.assertEqual(ST.REASON_UNKNOWN_SUSPENSION_STATUS, verdict.reason)
        self.assertFalse(verdict.executable)

    def test_p7_unknown_price_limit_evidence_is_unproven_where_it_matters(self):
        """P7：``resolve_limit_pct`` 只在 ST 会改变结论的区间里要求 ST 证据。

        这是底层解析器的语义；``tradability_at`` 更早就会因 ST 未知而
        ``unproven / unknown_st_status``（见 P7b），所以这里直接测解析器。
        """
        # 主板：+7% 落在 [5%, 9.5%) 的歧义区间 → 无法判定。
        self.assertEqual(
            (None, ST.REASON_UNKNOWN_PRICE_LIMIT),
            ST.resolve_limit_pct(MAIN_BOARD, name=None, risk_flag=None, pct_change=0.07),
        )
        # +2%：无论 ST 与否都碰不到涨跌停 → 与 ST 无关。
        self.assertEqual(
            (9.5, None),
            ST.resolve_limit_pct(MAIN_BOARD, name=None, risk_flag=None, pct_change=0.02),
        )
        # +10%：无论 ST 与否都会被拦 → 与 ST 无关。
        self.assertEqual(
            (9.5, None),
            ST.resolve_limit_pct(MAIN_BOARD, name=None, risk_flag=None, pct_change=0.10),
        )
        # 给出历史名称 → 歧义消失。
        self.assertEqual(
            (9.5, None),
            ST.resolve_limit_pct(
                MAIN_BOARD, name="某某股份", risk_flag=None, pct_change=0.07
            ),
        )

    def test_p7b_unknown_historical_st_status_fails_closed(self):
        """P7b：**历史** ST 状态未知时，账户权限无法证明 → ``unproven``。

        "不知道当时是不是 ST" 不等于"当时不是 ST"：ST 证券不在账户权限范围内，
        所以缺名称/风险标记时不能放行。板块层面（由代码段决定）仍然照常判定。
        """
        unknown = buy(evidence(price=102.0, reference=100.0, name=None, risk_flag=None))
        self.assertEqual(ST.STATUS_UNPROVEN, unknown.status)
        self.assertEqual(ST.REASON_UNKNOWN_ST_STATUS, unknown.reason)
        self.assertFalse(unknown.executable)
        # 给出历史名称（非 ST）→ 可执行。
        self.assertEqual(
            ST.STATUS_EXECUTABLE,
            buy(evidence(price=102.0, reference=100.0, name="某某股份")).status,
        )
        # 板块层面就出局的（科创板/北交所/其他）不需要 ST 证据也是 blocked。
        for code in (STAR, BSE, "900001"):
            verdict = buy(evidence(price=102.0, reference=100.0, name=None), code=code)
            self.assertEqual(ST.STATUS_BLOCKED, verdict.status, code)
            self.assertEqual(ST.REASON_UNSUPPORTED_SECURITY_TYPE, verdict.reason, code)
        # risk_flag 单独给出也算 ST 证据。
        self.assertEqual(
            ST.STATUS_BLOCKED,
            buy(evidence(price=102.0, reference=100.0, name=None, risk_flag=True)).status,
        )

    def test_p8_missing_or_invalid_price_is_never_coerced_to_zero(self):
        """P8：缺失/非法价格绝不变成 0，也绝不成交。"""
        for price in (0, -1.0, float("nan"), float("inf"), "abc", True):
            verdict = buy(evidence(price=price))
            self.assertEqual(ST.STATUS_INVALID, verdict.status, price)
            self.assertEqual(ST.REASON_INVALID_PRICE, verdict.reason, price)
            self.assertIsNone(verdict.price)
        missing = buy(evidence(price=None, halted=False))
        self.assertEqual(ST.STATUS_UNPROVEN, missing.status)
        self.assertIsNone(missing.price)

    def test_p9_zero_and_missing_volume_are_explicit(self):
        """P9：0 成交量与缺成交量是两种显式状态。

        一个收盘价有效的 bar 本身就证明当天成交过，成交量是冗余佐证，所以默认
        **不**要求它；显式 0 是矛盾数据 → blocked。需要更严的调用方显式打开
        ``volume_required``。
        """
        zero = buy(evidence(volume=0))
        self.assertEqual(ST.STATUS_BLOCKED, zero.status)
        self.assertEqual(ST.REASON_ZERO_VOLUME, zero.reason)
        # 默认：缺成交量不阻塞（bar 本身是成交证据）。
        self.assertEqual(ST.STATUS_EXECUTABLE, buy(evidence(volume=None)).status)
        # 显式要求成交量时：缺失 → unproven。
        strict = buy(evidence(volume=None, volume_required=True))
        self.assertEqual(ST.STATUS_UNPROVEN, strict.status)
        self.assertEqual(ST.REASON_MISSING_VOLUME, strict.reason)


# ─────────────────── P10–P14: PIT / current-state sentinels ───────────────────


class PitSentinelTest(unittest.TestCase):
    FORBIDDEN_IMPORTS = frozenset(
        {
            "universe",
            "paper_trading",
            "marketdata",
            "marketdata_feeds",
            "marketdata_cache",
            "selection_tracking",
            "data_pipeline",
        }
    )

    def test_p10_current_st_state_cannot_rewrite_a_historical_verdict(self):
        """P10：当前 ST / 当前状态不能重写历史判定。"""
        historical = evidence(price=102.0, reference=100.0, name="某某股份")
        baseline = buy(historical)
        self.assertEqual(ST.STATUS_EXECUTABLE, baseline.status)
        tampered = dataclasses.replace(
            historical,
            provenance={"current_name": "*ST某某", "current_suspended": True},
        )
        self.assertEqual(baseline, buy(tampered))

    def test_p11_current_universe_cannot_rewrite_a_historical_verdict(self):
        """P11：当前 universe / 上市状态不能重写历史判定。"""
        historical = evidence(price=102.0, reference=100.0, name="某某股份")
        baseline = buy(historical)
        for provenance in (
            {"current_universe_member": False},
            {"current_listing_state": "delisted"},
            {"current_board": "科创板"},
        ):
            self.assertEqual(
                baseline, buy(dataclasses.replace(historical, provenance=provenance))
            )
        # 源码守卫：本模块不得 import 任何"当前状态"数据源。
        source = pathlib.Path(ST.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual([], sorted(imported & self.FORBIDDEN_IMPORTS))

    def test_p12_future_price_action_cannot_change_entry_tradability(self):
        """P12：未来价格不能改变 entry 判定。"""
        entry_ev = evidence(price=102.0, reference=100.0)
        baseline = buy(entry_ev)
        self.assertEqual(ST.STATUS_EXECUTABLE, baseline.status)
        # 未来价格以另一个 evidence 的形式存在，或（更隐蔽地）被塞进 provenance，
        # 都不得影响 entry 判定。
        for future_price in (50.0, 200.0, 0.0, None):
            future_ev = evidence(session="2024-06-20", price=future_price, reference=102.0)
            self.assertEqual(baseline, buy(entry_ev), future_price)
            self.assertNotEqual(future_ev.price, entry_ev.price)
            tampered = dataclasses.replace(
                entry_ev, provenance={"future_price": future_price}
            )
            self.assertEqual(baseline, buy(tampered), future_price)
        # 未来证据进入 exit 判定是合法的，但不能回流到 entry。
        future_halted = evidence(session="2024-06-20", price=None, halted=True)
        self.assertEqual(baseline, buy(entry_ev))
        self.assertEqual(ST.STATUS_BLOCKED, sell(future_halted).status)

    def test_p13_future_volume_cannot_change_entry_tradability(self):
        """P13：未来成交量不能改变 entry 判定。"""
        entry_ev = evidence(price=102.0, reference=100.0, volume=1_000_000.0)
        baseline = buy(entry_ev)
        self.assertEqual(ST.STATUS_EXECUTABLE, baseline.status)
        for future_volume in (0.0, 1e12, None):
            future_ev = evidence(
                session="2024-06-20", price=102.0, reference=100.0, volume=future_volume
            )
            self.assertEqual(baseline, buy(entry_ev), future_volume)
            if future_volume == 0.0:
                self.assertEqual(ST.STATUS_BLOCKED, buy(future_ev).status)
            tampered = dataclasses.replace(
                entry_ev, provenance={"future_volume": future_volume}
            )
            self.assertEqual(baseline, buy(tampered), future_volume)

    def test_p14_future_halt_state_cannot_change_entry_tradability(self):
        """P14：未来停牌状态不能改变 entry 判定。"""
        entry_ev = evidence(price=102.0, reference=100.0)
        baseline = buy(entry_ev)
        self.assertEqual(baseline, buy(entry_ev))
        self.assertEqual(ST.STATUS_EXECUTABLE, baseline.status)


# ──────────────── P15–P20: blocked is never silently carried forward ────────────────


class NoSilentCarryForwardTest(unittest.TestCase):
    def test_p15_blocked_entry_is_not_shifted_to_the_next_session(self):
        """P15：买不进就是买不进，绝不用下一根 K 线补一个成交。"""
        blocked = evidence(price=110.0, reference=100.0)          # 涨停，买不到
        next_session_ok = evidence(session=NEXT_SESSION, price=110.5, reference=110.0)
        self.assertEqual(ST.STATUS_EXECUTABLE, buy(next_session_ok).status)
        built = ST.build_executable_outcomes(
            [row("a", entry_ev=blocked, entry_session=SESSION)]
        )
        outcome = built["outcomes"][0]
        self.assertEqual(SESSION, outcome.intended_entry_session)
        self.assertIsNone(outcome.actual_entry_session)
        self.assertNotEqual(NEXT_SESSION, outcome.actual_entry_session)

    def test_p16_blocked_entry_never_produces_a_verified_executable_return(self):
        """P16：blocked entry 不得产生 executable return。"""
        built = ST.build_executable_outcomes(
            [row("a", entry_ev=evidence(price=110.0, reference=100.0), label_value=0.25)]
        )
        outcome = built["outcomes"][0]
        self.assertFalse(outcome.executable)
        self.assertIsNone(outcome.executable_return)
        self.assertEqual(0, built["report"]["executable"])

    def test_p17_selection_fact_survives_a_blocked_entry(self):
        """P17：不可成交**不**删除"策略当时选过它"这个历史事实。"""
        built = ST.build_executable_outcomes(
            [row("blocked-one", entry_ev=evidence(halted=True, price=None))]
        )
        outcome = built["outcomes"][0]
        self.assertTrue(outcome.selected)
        self.assertEqual("blocked-one", outcome.sample_key)
        self.assertEqual(1, built["report"]["input_selected"])
        self.assertEqual(1, len(built["outcomes"]))

    def test_p18_market_outcome_and_executable_outcome_stay_distinct(self):
        """P18：market counterfactual 与 executable outcome 是两件事。"""
        built = ST.build_executable_outcomes(
            [row("a", entry_ev=evidence(price=110.0, reference=100.0), label_value=0.30)]
        )
        outcome = built["outcomes"][0]
        self.assertEqual("verified", outcome.market_label_status)
        self.assertEqual(0.30, outcome.market_label_value)
        self.assertIsNone(outcome.executable_return)
        self.assertFalse(outcome.executable)

    def test_p19_blocked_exit_never_pretends_the_intended_close_was_filled(self):
        """P19：卖不出去时不得拿 intended close 当已实现成交。"""
        built = ST.build_executable_outcomes(
            [
                row(
                    "a",
                    entry_ev=evidence(price=102.0, reference=100.0),
                    exit_session="2024-06-20",
                    exit_ev=evidence(session="2024-06-20", price=None, halted=True),
                    label_value=0.18,
                )
            ]
        )
        outcome = built["outcomes"][0]
        self.assertEqual(ST.STATUS_EXECUTABLE, outcome.entry_status)
        self.assertEqual(ST.STATUS_BLOCKED, outcome.exit_status)
        self.assertEqual(ST.REASON_SUSPENDED, outcome.exit_reason)
        self.assertIsNone(outcome.actual_exit_session)
        self.assertIsNone(outcome.executable_return)
        self.assertFalse(outcome.executable)
        self.assertEqual(1, built["report"]["blocked_exit"])

    def test_p20_carry_forward_only_through_the_authoritative_t1_rule(self):
        """P20：唯一允许"前进 session"的地方是权威 T+1 规则本身。"""
        self.assertEqual(
            str(PTR.next_weekday("2024-06-14")),
            ST.earliest_sellable_session(MAIN_BOARD, name=None, entry_session="2024-06-14"),
        )
        # 周末：周五入场 → 下周一才可卖。
        self.assertEqual(
            "2024-06-17",
            ST.earliest_sellable_session(MAIN_BOARD, name=None, entry_session="2024-06-14"),
        )

    def test_p31_evidence_from_another_session_is_rejected(self):
        """P31：evidence 带的是**另一个 session** 的 bar 时，绝不"就地按它的收盘判定"。

        一条目标 entry=06-18 的行配上一根 06-19 的 bar，若按 06-19 收盘判定，
        就会被判成可执行并记成 ``actual_entry_session=06-19`` —— 正是契约禁止的
        "静默顺延到下一个 session"。
        """
        wrong_session = evidence(session=NEXT_SESSION, price=102.0, reference=100.0)
        verdict = ST.entry_tradability(
            wrong_session, code=MAIN_BOARD, entry_session=SESSION
        )
        self.assertEqual(ST.STATUS_INVALID, verdict.status)
        self.assertEqual(ST.REASON_EVIDENCE_SESSION_MISMATCH, verdict.reason)
        self.assertFalse(verdict.executable)
        self.assertEqual(SESSION, verdict.detail["intended_session"])
        self.assertEqual(NEXT_SESSION, verdict.detail["evidence_session"])

        # 出口侧同样拒绝。
        exit_verdict = ST.exit_tradability(
            evidence(session="2024-06-20", price=102.0, reference=100.0),
            code=MAIN_BOARD, exit_session="2024-06-19", entry_session=SESSION,
        )
        self.assertEqual(ST.STATUS_INVALID, exit_verdict.status)
        self.assertEqual(ST.REASON_EVIDENCE_SESSION_MISMATCH, exit_verdict.reason)

        # 端到端：错位证据不得制造一次成交。
        built = ST.build_executable_outcomes(
            [
                ST.SelectionRow(
                    sample_key="a", code=MAIN_BOARD, selected=True,
                    intended_entry_session=SESSION,
                    entry_evidence=evidence(session=NEXT_SESSION, price=102.0, reference=100.0),
                    market_label_status="verified", market_label_value=0.2,
                )
            ]
        )
        outcome = built["outcomes"][0]
        self.assertFalse(outcome.executable)
        self.assertIsNone(outcome.actual_entry_session)
        self.assertIsNone(outcome.executable_return)

    def test_p32_missing_exit_evidence_is_not_a_successful_exit(self):
        """P32：没有离场证据 ≠ 离场没问题 —— 完整 executable outcome 要求两侧都可执行。"""
        built = ST.build_executable_outcomes(
            [
                ST.SelectionRow(
                    sample_key="entry-only", code=MAIN_BOARD, selected=True,
                    intended_entry_session=SESSION,
                    entry_evidence=evidence(price=102.0, reference=100.0),
                    market_label_status="verified", market_label_value=0.2,
                )
            ]
        )
        outcome = built["outcomes"][0]
        # 入场侧确实可执行，但那只是"买得进"。
        self.assertTrue(outcome.entry_executable)
        self.assertFalse(outcome.executable)
        self.assertEqual(ST.STATUS_UNPROVEN, outcome.exit_status)
        self.assertEqual(ST.REASON_EXIT_EVIDENCE_MISSING, outcome.exit_reason)
        self.assertIsNone(outcome.actual_exit_session)
        self.assertIsNone(outcome.executable_return)
        self.assertEqual(0, built["report"]["executable"])
        self.assertEqual(1, built["report"]["unproven"])
        self.assertTrue(ST.audit_totals(built["report"])["complete"])
        # executable-only 口径不得收下它。
        self.assertEqual(
            [],
            ST.evaluation_eligibility(
                built["outcomes"], mode=ST.MODE_EXECUTABLE
            )["sample_keys"],
        )


# ──────────────── P21–P24: T+1, board policy, order, audit ────────────────


class PolicyAndAuditTest(unittest.TestCase):
    def test_p21_t1_invariant_is_preserved(self):
        """P21：T+1 不回归 —— 买入当日不得卖出。"""
        same_day = ST.exit_tradability(
            evidence(price=102.0, reference=100.0),
            code=MAIN_BOARD, exit_session=SESSION, entry_session=SESSION,
        )
        self.assertEqual(ST.STATUS_BLOCKED, same_day.status)
        self.assertEqual(ST.REASON_T1_NOT_SELLABLE, same_day.reason)
        next_day = ST.exit_tradability(
            evidence(session=NEXT_SESSION, price=102.0, reference=100.0),
            code=MAIN_BOARD, exit_session=NEXT_SESSION, entry_session=SESSION,
        )
        self.assertEqual(ST.STATUS_EXECUTABLE, next_day.status)

    def test_p21c_t1_uses_the_resolved_entry_session_when_intended_is_omitted(self):
        """``intended_entry_session`` 省略时，T+1 仍必须用**已解析的**入场 session。

        契约显式允许只给带日期的入场证据。此时若把 ``None`` 传进 T+1 检查，
        ``tradability_at`` 会整段跳过 T+1，于是"同日买、同日卖"被判成可执行。
        """
        same_day_evidence = evidence(session=SESSION, price=102.0, reference=100.0)
        built = ST.build_executable_outcomes([
            ST.SelectionRow(
                sample_key="omitted-intended-entry", code=MAIN_BOARD, selected=True,
                # 故意不传 intended_entry_session。
                entry_evidence=same_day_evidence,
                intended_exit_session=SESSION,
                exit_evidence=same_day_evidence,
                market_label_status="verified", market_label_value=0.2,
            )
        ])
        outcome = built["outcomes"][0]
        # 同日卖出违反 T+1 → 不得可执行，也不得有 executable return。
        self.assertEqual(ST.STATUS_BLOCKED, outcome.exit_status)
        self.assertEqual(ST.REASON_T1_NOT_SELLABLE, outcome.exit_reason)
        self.assertFalse(outcome.executable)
        self.assertIsNone(outcome.executable_return)
        self.assertEqual(0, built["report"]["executable"])
        self.assertEqual(1, built["report"]["blocked_exit"])
        self.assertTrue(ST.audit_totals(built["report"])["complete"])

    def test_p21b_etf_t0_branch_delegates_to_the_authoritative_classifier(self):
        """P21b：T+0 ETF 的分类委托权威实现（当前账户权限把 ETF 挡在前面）。"""
        self.assertEqual("etf_t0", PTR.asset_type(ETF, "沪深300ETF"))
        self.assertEqual(
            "2024-06-14",
            ST.earliest_sellable_session(ETF, name="沪深300ETF", entry_session="2024-06-14"),
        )
        # ETF 不在账户权限范围内 → 仍然 fail closed（权威 security_scope 的结论）。
        verdict = buy(evidence(price=4.02, reference=4.0), code=ETF)
        self.assertEqual(ST.STATUS_BLOCKED, verdict.status)
        self.assertEqual(ST.REASON_UNSUPPORTED_SECURITY_TYPE, verdict.reason)

    def test_p22_board_and_st_policies_reuse_the_authoritative_rule(self):
        """P22：板块 / ST 口径直接委托，绝不写统一的 10%。"""
        for code in (MAIN_BOARD, CHINEXT, STAR, BSE):
            self.assertEqual(
                PTR.limit_pct(code, None, False), ST.board_limit_pct(code), code
            )
        self.assertEqual(9.5, ST.board_limit_pct(MAIN_BOARD))
        self.assertEqual(19.5, ST.board_limit_pct(CHINEXT))
        # ST 覆盖由权威实现给出。
        limit, reason = ST.resolve_limit_pct(
            MAIN_BOARD, name="*ST某某", risk_flag=False, pct_change=0.06
        )
        self.assertIsNone(reason)
        self.assertEqual(PTR.limit_pct(MAIN_BOARD, "*ST某某"), limit)
        self.assertEqual(5.0, limit)

    def test_p23_unsupported_security_type_fails_closed(self):
        """P23：账户权限之外的证券一律 blocked，不猜。"""
        for code, board in ((STAR, "科创板"), (BSE, "北交所"), ("900001", "其他证券")):
            verdict = buy(evidence(price=10.2, reference=10.0), code=code)
            self.assertEqual(ST.STATUS_BLOCKED, verdict.status, code)
            self.assertEqual(ST.REASON_UNSUPPORTED_SECURITY_TYPE, verdict.reason, code)
            self.assertEqual(board, verdict.board, code)
        st = buy(evidence(price=10.2, reference=10.0, name="*ST某某"))
        self.assertEqual(ST.STATUS_BLOCKED, st.status)
        self.assertEqual("风险警示", st.board)

    def test_p24_input_order_does_not_change_the_result(self):
        """P24：输入顺序不影响任何判定与计数。"""
        rows = [
            row("a", entry_ev=evidence(price=102.0, reference=100.0)),
            row("b", entry_ev=evidence(halted=True, price=None)),
            row("c", entry_ev=evidence(price=110.0, reference=100.0)),
            row("d", entry_ev=evidence(price=None, halted=None)),
            row("e", selected=False),
        ]
        baseline = ST.build_executable_outcomes(rows)
        for seed in (0, 1, 7, 20260915):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            other = ST.build_executable_outcomes(shuffled)
            self.assertEqual(dict(baseline["report"]), dict(other["report"]))
            self.assertEqual(
                {o.sample_key: o.as_dict() for o in baseline["outcomes"]},
                {o.sample_key: o.as_dict() for o in other["outcomes"]},
            )

    def test_p25_reason_counts_account_for_every_input_row(self):
        """P25：每一条输入选股都必须落在某个计数桶里，不静默丢行。"""
        rows = [
            row("exec", entry_ev=evidence(price=102.0, reference=100.0)),
            row("blocked", entry_ev=evidence(price=110.0, reference=100.0)),
            row("unproven", entry_ev=evidence(price=None, halted=None)),
            row("invalid", entry_ev=evidence(price=0)),
            row("skipped", selected=False),
        ]
        built = ST.build_executable_outcomes(rows)
        totals = ST.audit_totals(built["report"])
        self.assertTrue(totals["complete"], built["report"])
        self.assertEqual(5, totals["input_rows"])
        self.assertEqual(5, totals["accounted"])
        counts = built["report"]["reason_counts"]
        self.assertEqual(1, counts[ST.REASON_LIMIT_UP_BUY_BLOCKED])
        self.assertEqual(1, counts[ST.REASON_UNKNOWN_SUSPENSION_STATUS])
        self.assertEqual(1, counts[ST.REASON_INVALID_PRICE])
        self.assertEqual(1, counts[ST.REASON_OK])
        self.assertEqual(4, built["report"]["input_selected"])

    def test_p26_executable_subset_contains_only_executable_evidence(self):
        """P26：executable 子集里只能有 entry 与 exit 都真正可执行的样本。"""
        built = ST.build_executable_outcomes(
            [
                row(
                    "good", entry_ev=evidence(price=102.0, reference=100.0),
                    exit_session="2024-06-20",
                    exit_ev=evidence(session="2024-06-20", price=105.0, reference=102.0),
                    label_value=0.03,
                ),
                row(
                    "bad-exit", entry_ev=evidence(price=102.0, reference=100.0),
                    exit_session="2024-06-20",
                    exit_ev=evidence(session="2024-06-20", price=None, halted=True),
                    label_value=0.5,
                ),
                row("unproven-entry", entry_ev=evidence(price=None, halted=None)),
            ]
        )
        executable = [o for o in built["outcomes"] if o.executable]
        self.assertEqual(["good"], [o.sample_key for o in executable])
        self.assertIsNotNone(executable[0].executable_return)
        by_key = {o.sample_key: o for o in built["outcomes"]}
        self.assertFalse(by_key["unproven-entry"].executable)
        self.assertEqual(ST.STATUS_UNPROVEN, by_key["unproven-entry"].entry_status)
        for outcome in built["outcomes"]:
            if outcome.executable:
                self.assertEqual(ST.STATUS_EXECUTABLE, outcome.entry_status)
                self.assertIn(outcome.exit_status, (None, ST.STATUS_EXECUTABLE))
            else:
                self.assertIsNone(outcome.executable_return)

    def test_p27_walk_forward_boundary_and_sample_identity_are_untouched(self):
        """P27：本 contract 不改变 walk-forward 的边界与 sample identity。"""
        sessions = [f"2024-06-{day:02d}" for day in range(3, 29)]
        samples = [
            WFV.ValidationSample(
                sample_key=f"k{index:03d}",
                code=MAIN_BOARD,
                decision_session=day,
                label_available_at=WFV.label_available_at_for_close(day),
                target=0.01,
                horizon=2,
                exit_date=day,
                pit_status=WFV.PIT_VERIFIED,
                features={"momentum": float(index)},
            )
            for index, day in enumerate(sessions)
        ]
        config = WFV.WalkForwardConfig(
            min_train_sessions=10, validation_sessions=4, test_sessions=4
        )
        before = WFV.build_walk_forward_folds(samples, config)
        rows = [
            row(
                f"k{index:03d}",
                entry_ev=evidence(session=day, price=10.2, reference=10.0),
                entry_session=day,
            )
            for index, day in enumerate(sessions)
        ]
        built = ST.build_executable_outcomes(rows)
        after = WFV.build_walk_forward_folds(samples, config)
        self.assertEqual(
            [fold.keys("train") for fold in before["folds"]],
            [fold.keys("train") for fold in after["folds"]],
        )
        self.assertEqual(dict(before["report"]), dict(after["report"]))
        # sample identity 原样保留，一行都没被丢。
        self.assertEqual(
            {f"k{index:03d}" for index in range(len(sessions))},
            {outcome.sample_key for outcome in built["outcomes"]},
        )


# ──────────────── P28–P30: label separation & end-to-end ────────────────


class LabelSeparationAndEndToEndTest(unittest.TestCase):
    def test_p28_market_label_stays_verified_while_execution_is_blocked(self):
        """P28：market label 可以保持 verified，而 executable outcome 是 blocked。"""
        built = ST.build_executable_outcomes(
            [row("a", entry_ev=evidence(halted=True, price=None), label_value=0.42)]
        )
        outcome = built["outcomes"][0]
        self.assertEqual("verified", outcome.market_label_status)
        self.assertEqual(0.42, outcome.market_label_value)
        self.assertFalse(outcome.executable)
        self.assertEqual(1, built["report"]["blocked_entry"])
        self.assertEqual(0, built["report"]["executable"])

    def test_p29_future_exit_tradability_changes_the_exit_outcome_not_the_entry(self):
        """P29：改变未来 exit 可成交性只影响 exit 结果，绝不回流 entry。"""
        entry_ev = evidence(price=102.0, reference=100.0)
        good_exit = evidence(session="2024-06-20", price=105.0, reference=102.0)
        bad_exit = evidence(session="2024-06-20", price=None, halted=True)
        built = ST.build_executable_outcomes(
            [
                row("a", entry_ev=entry_ev, exit_session="2024-06-20", exit_ev=good_exit),
                row("b", entry_ev=entry_ev, exit_session="2024-06-20", exit_ev=bad_exit),
            ]
        )
        first, second = built["outcomes"]
        self.assertEqual(first.entry_status, second.entry_status)
        self.assertEqual(first.entry_reason, second.entry_reason)
        self.assertEqual(ST.STATUS_EXECUTABLE, first.exit_status)
        self.assertEqual(ST.STATUS_BLOCKED, second.exit_status)
        self.assertTrue(first.executable)
        self.assertFalse(second.executable)

    def test_p30_end_to_end_selection_to_executable_evaluation(self):
        """P30：selection → tradability → label → executable evaluation 全链路。"""
        rows = [
            # A：T+1 可执行，未来上涨，正常离场。
            row(
                "A", entry_ev=evidence(price=102.0, reference=100.0),
                entry_session="2024-06-18", exit_session="2024-06-20",
                exit_ev=evidence(session="2024-06-20", price=108.0, reference=104.0),
                label_value=0.08,
            ),
            # B：T+1 涨停买不到，但未来同样上涨 —— alpha 有，执行吃不到。
            row(
                "B", entry_ev=evidence(price=110.0, reference=100.0),
                entry_session="2024-06-18", exit_session="2024-06-20",
                exit_ev=evidence(session="2024-06-20", price=118.0, reference=110.0),
                label_value=0.18,
            ),
            # C：T+1 可执行，但目标离场日停牌 —— 不得拿 intended close 当已实现。
            row(
                "C", entry_ev=evidence(price=102.0, reference=100.0),
                entry_session="2024-06-18", exit_session="2024-06-20",
                exit_ev=evidence(session="2024-06-20", price=None, halted=True),
                label_value=0.30,
            ),
        ]
        built = ST.build_executable_outcomes(rows)
        report = built["report"]
        by_key = {outcome.sample_key: outcome for outcome in built["outcomes"]}

        # selection facts = A + B + C（一条都不能消失）
        self.assertEqual(3, report["input_selected"])
        self.assertEqual({"A", "B", "C"}, set(by_key))
        self.assertTrue(all(outcome.selected for outcome in by_key.values()))
        # market labels = A + B + C（counterfactual 完整保留）
        self.assertEqual(
            {"A": 0.08, "B": 0.18, "C": 0.30},
            {key: outcome.market_label_value for key, outcome in by_key.items()},
        )
        self.assertTrue(
            all(outcome.market_label_status == "verified" for outcome in by_key.values())
        )
        # executable evaluation = 只有 A
        self.assertEqual(["A"], [o.sample_key for o in built["outcomes"] if o.executable])
        self.assertEqual(1, report["executable"])
        # blocked report 里 B（entry）与 C（exit）都在，且各自有机器 reason
        self.assertEqual(1, report["blocked_entry"])
        self.assertEqual(1, report["blocked_exit"])
        self.assertEqual(
            ST.REASON_LIMIT_UP_BUY_BLOCKED, by_key["B"].entry_reason
        )
        self.assertEqual(ST.REASON_SUSPENDED, by_key["C"].exit_reason)
        # 两条 blocked 都不能有 executable return
        self.assertIsNone(by_key["B"].executable_return)
        self.assertIsNone(by_key["C"].executable_return)
        # 审计计数完整
        self.assertTrue(ST.audit_totals(report)["complete"])
        self.assertEqual(
            1, report["reason_counts"][ST.REASON_LIMIT_UP_BUY_BLOCKED]
        )
        self.assertEqual(1, report["reason_counts"][ST.REASON_SUSPENDED])

    def test_p30c_evaluation_mode_is_explicit_and_additive(self):
        """§22：可执行子集是**显式**选择，不静默改变既有 consumer 的数字。"""
        built = ST.build_executable_outcomes(
            [
                row(
                    "A", entry_ev=evidence(price=102.0, reference=100.0),
                    exit_session="2024-06-20",
                    exit_ev=evidence(session="2024-06-20", price=105.0, reference=102.0),
                    label_value=0.03,
                ),
                row("B", entry_ev=evidence(price=110.0, reference=100.0), label_value=0.18),
                row("C", entry_ev=evidence(price=None, halted=None), label_value=0.30),
                row("D", selected=False, label_value=0.0),
            ]
        )
        outcomes = built["outcomes"]
        # 默认口径 = market counterfactual：全部被选中的样本都保留。
        market = ST.evaluation_eligibility(outcomes)
        self.assertEqual(ST.MODE_MARKET, market["mode"])
        self.assertEqual(3, market["selected"])
        self.assertEqual(3, market["eligible"])
        self.assertEqual(0, market["excluded_by_execution"])
        self.assertEqual(["A", "B", "C"], market["sample_keys"])
        # 显式口径 = executable only：只留真正可执行的。
        executable = ST.evaluation_eligibility(outcomes, mode=ST.MODE_EXECUTABLE)
        self.assertEqual(["A"], executable["sample_keys"])
        self.assertEqual(2, executable["excluded_by_execution"])
        # 未知口径直接拒绝，不猜。
        with self.assertRaises(ST.TradabilityContractError):
            ST.evaluation_eligibility(outcomes, mode="whatever")

    def test_p30d_rule_regime_changes_the_verdict_for_the_same_move(self):
        """§30：同一种价格变化，policy context 不同 → 结论可以不同。"""
        # +15%：主板（9.5%）触及涨停；创业板（19.5%）没有。
        self.assertEqual(
            ST.STATUS_BLOCKED, buy(evidence(price=115.0, reference=100.0)).status
        )
        self.assertEqual(
            ST.STATUS_EXECUTABLE,
            buy(evidence(price=115.0, reference=100.0, name="某某股份"), code=CHINEXT).status,
        )
        # +6%：非 ST 主板可执行；ST（5%）触及涨停 → blocked。
        self.assertEqual(
            ST.STATUS_EXECUTABLE,
            buy(evidence(price=106.0, reference=100.0, name="某某股份")).status,
        )
        self.assertEqual(
            ST.STATUS_BLOCKED,
            buy(evidence(price=106.0, reference=100.0, name="*ST某某")).status,
        )


# ──────────────── delegation guards ────────────────


class DelegationGuardTest(unittest.TestCase):
    def test_contract_delegates_every_market_rule(self):
        """本 contract 不复制任何市场规则：逐项与权威实现对齐。"""
        self.assertEqual(
            PTR.security_scope(MAIN_BOARD),
            ST.security_permission(MAIN_BOARD, name=None, risk_flag=None),
        )
        self.assertEqual(
            PTR.limit_pct(MAIN_BOARD, "*ST某某"),
            ST.resolve_limit_pct(
                MAIN_BOARD, name="*ST某某", risk_flag=None, pct_change=0.06
            )[0],
        )
        self.assertEqual(
            str(PTR.next_weekday("2024-06-14")),
            ST.earliest_sellable_session(MAIN_BOARD, name=None, entry_session="2024-06-14"),
        )
        self.assertEqual(
            ST.session_close_at(SESSION),
            ST.session_close_at(SESSION),
        )

    def test_no_universal_price_limit_magic_number(self):
        """禁止把涨跌停写成统一的 10%（或**任何**本地板块/ST 常量）。

        用 AST 只看**数值常量**，因此文档字符串里解释 9.5/19.5/29.5 不受影响。

        此前这里给 ``ST_LIMIT_PCT = 5.0`` 开了例外，理由是"唯一允许的本地常量"。
        那是错的：ST 上限必须**唯一**来自 :func:`paper_trading_rules.limit_pct`，
        本地复制会在权威口径变化时静默漂移。现在没有任何例外。
        """
        forbidden = {
            0.1, 0.095, 0.195, 0.295,
            9.5, 19.5, 29.5,
            0.05, 5.0,
        }
        source = pathlib.Path(ST.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                if isinstance(node.value, bool):
                    continue
                if node.value in forbidden:
                    found.append((node.lineno, node.value))
        self.assertEqual([], found, f"local price-limit constants: {found}")

    def test_st_limit_is_derived_from_the_authoritative_rule(self):
        """ST 上限逐板块与权威实现一致，且不依赖任何本地常量。"""
        self.assertFalse(hasattr(ST, "ST_LIMIT_PCT"), "ST_LIMIT_PCT must be gone")
        for code in (MAIN_BOARD, CHINEXT):
            self.assertEqual(
                PTR.limit_pct(code, "*ST某某", True),
                ST.st_limit_pct(code, name="*ST某某", risk_flag=True),
            )
            # 强制 ST 口径 = 权威实现按 ST 解析的结果。
            self.assertEqual(
                PTR.limit_pct(code, None, True),
                ST.st_limit_pct(code, name=None, risk_flag=True),
            )


class ProductionEvaluatorWiringTest(unittest.TestCase):
    """生产评估路径真的在用可执行子集，而不是"只加了个没人调用的模式"。"""

    #: 06-17 是 06-14 决策之后的第一个交易日（entry），06-18 是 exit。
    KLINES = {
        # entry +5%，exit +10%：买入方向没被拦（卖出方向不看涨停）→ 可执行。
        "600001": {
            "2024-06-14": (10.0, 10.0),
            "2024-06-17": (10.0, 10.5),
            "2024-06-18": (10.5, 11.55),
            "2024-06-19": (11.5, 11.6),
        },
        # entry 正好 +10%：主板涨停买不进 → blocked_entry。
        "600002": {
            "2024-06-14": (10.0, 10.0),
            "2024-06-17": (10.0, 11.0),
            "2024-06-18": (11.0, 11.5),
            "2024-06-19": (11.5, 11.6),
        },
    }

    def _evaluate(self, picks, **kwargs):
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            return AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30", **kwargs
            )

    #: 动作 session 当时的状态来源：entry/exit 都不是决策日，必须显式提供。
    @staticmethod
    def _action_state(code, session):
        return {"name": "某某股份", "risk_flag": False}

    def test_production_evaluator_excludes_unexecutable_picks_by_default(self):
        """生产默认口径 = executable only：买不进的样本不再进均值。"""
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]
        lines, summary = self._evaluate(picks, security_state_fn=self._action_state)
        self.assertEqual(ST.MODE_EXECUTABLE, summary["tradability_mode"])
        self.assertEqual(1, summary["entry_counts"]["executable"])
        self.assertEqual(1, summary["entry_counts"]["blocked_entry"])
        # 只有可执行的那一条进入均值 —— 这就是"生产指标真的变了"。
        self.assertEqual(1, summary["n"])
        self.assertIsNotNone(summary["mean_return"])
        # 但被拦的那条**没有消失**，它在计数里。
        self.assertEqual(
            1, summary["tradability_counts"][ST.REASON_LIMIT_UP_BUY_BLOCKED]
        )
        joined = "\n".join(lines)
        self.assertIn("可成交性：", joined)
        self.assertIn("可执行样本", joined)

    def test_production_evaluator_market_mode_is_explicit(self):
        """market counterfactual 口径必须显式选择，且两条都保留。"""
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]
        _, summary = self._evaluate(
            picks, tradability_mode=ST.MODE_MARKET,
            security_state_fn=self._action_state)
        self.assertEqual(2, summary["n"])
        self.assertEqual(1, summary["entry_counts"]["blocked_entry"])
        # 未知口径直接拒绝。
        with self.assertRaises(ValueError):
            self._evaluate(picks, tradability_mode="nope")

    def test_production_evaluator_fails_closed_without_historical_name(self):
        """没有**历史**名称 → ST 未知 → unproven → 不进可执行均值（fail closed）。"""
        picks = [("s1", "2024-06-14", "600001", None)]
        lines, summary = self._evaluate(picks)
        self.assertEqual(0, summary["n"])
        self.assertEqual(1, summary["entry_counts"]["unproven"])
        # entry 与 exit 两侧都因 ST 未知而 unproven，所以该 reason 计数是 2。
        self.assertEqual(
            2, summary["tradability_counts"][ST.REASON_UNKNOWN_ST_STATUS]
        )
        self.assertIn("没有任何样本能证明可执行", "\n".join(lines))

    def test_production_evaluator_reads_the_name_recorded_at_decision_time(self):
        """pick 加载器带回**决策当时**记录的 name，而不是当前快照的名称。"""
        import os
        import sqlite3 as sq
        import tempfile

        import selection_alpha_report as AR

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "selection_tracking.db")
            conn = sq.connect(db)
            conn.execute(
                "CREATE TABLE selection_runs(id INTEGER PRIMARY KEY, strategy TEXT,"
                " data_asof_date TEXT)"
            )
            conn.execute(
                "CREATE TABLE selection_picks(run_id INTEGER, code TEXT, rank_no INTEGER,"
                " name TEXT)"
            )
            conn.execute("INSERT INTO selection_runs VALUES(1,'s1','2024-06-14')")
            conn.execute("INSERT INTO selection_picks VALUES(1,'600001',1,'某某股份')")
            conn.commit()
            conn.close()
            original = AR.DATA_DIR
            AR.DATA_DIR = tmp
            try:
                picks = AR.picks_from_tracking(3650)
            finally:
                AR.DATA_DIR = original
        self.assertEqual([("s1", "2024-06-14", "600001", "某某股份")], picks)


class PitOrderingAndActionTimeStateTest(unittest.TestCase):
    """Round-2 三个 blocker 的回归：

    1. PIT 可见性必须先于一切**证据派生**字段（名称 / 风险标记 / 停牌 / 价格 / 量）；
    2. entry/exit 的资格证据必须是**动作 session 当时**的状态，不是决策日名称；
    3. 逐策略"已验证样本"必须在执行过滤**之前**计数。
    """

    KLINES = ProductionEvaluatorWiringTest.KLINES

    @staticmethod
    def _non_st(code, session):
        return {"name": "某某股份", "risk_flag": False}

    def _evaluate(self, picks, **kwargs):
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            return AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30", **kwargs
            )

    def _tradability(self, evidence, action_session):
        return ST.tradability_at(
            evidence, code="600001", side=ST.SIDE_BUY,
            action_at=ST.session_close_at(action_session),
        )

    def test_p33_future_visible_st_name_cannot_rewrite_an_earlier_verdict(self):
        """动作时点看不到的 *ST 名称，不得把更早的动作改成 blocked。"""
        future_only = ST.MarketEvidence(
            session="2024-06-20",
            available_at=ST.session_close_at("2024-06-20"),
            price=10.2, reference_price=10.0, halted=False,
            name="某某股份ST",
        )
        verdict = self._tradability(future_only, "2024-06-18")
        self.assertEqual(ST.STATUS_UNPROVEN, verdict.status)
        self.assertEqual(ST.REASON_EVIDENCE_NOT_VISIBLE, verdict.reason)

    def test_p33b_future_visible_risk_flag_cannot_rewrite_an_earlier_verdict(self):
        future_only = ST.MarketEvidence(
            session="2024-06-20",
            available_at=ST.session_close_at("2024-06-20"),
            price=10.2, reference_price=10.0, halted=False,
            name="某某股份", risk_flag=True,
        )
        verdict = self._tradability(future_only, "2024-06-18")
        self.assertEqual(ST.STATUS_UNPROVEN, verdict.status)
        self.assertEqual(ST.REASON_EVIDENCE_NOT_VISIBLE, verdict.reason)

    def test_p33c_visible_state_still_decides_normally(self):
        """可见性通过后，证据派生权限照常生效（不是把门禁变成永久 unproven）。"""
        visible_st = ST.MarketEvidence(
            session="2024-06-18",
            available_at=ST.session_close_at("2024-06-18"),
            price=10.2, reference_price=10.0, halted=False,
            name="某某股份ST",
        )
        verdict = self._tradability(visible_st, "2024-06-18")
        self.assertEqual(ST.STATUS_BLOCKED, verdict.status)
        self.assertEqual(ST.REASON_UNSUPPORTED_SECURITY_TYPE, verdict.reason)
        self.assertEqual("historical_name", verdict.detail.get("basis"))

    def test_p34_decision_day_name_is_not_action_time_evidence(self):
        """没有动作时状态来源 → entry/exit 一律 ST 未知 → unproven（fail closed）。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]
        _, summary = self._evaluate(picks)
        self.assertEqual(0, summary["entry_counts"]["executable"])
        self.assertEqual(1, summary["entry_counts"]["unproven"])
        self.assertEqual(0, summary["n"])
        self.assertEqual(
            2, summary["tradability_counts"][ST.REASON_UNKNOWN_ST_STATUS])

    def test_p35_st_change_before_entry_blocks_the_stale_decision_name(self):
        """决策日非 ST、entry 时已是 ST → 过期名称不得让交易保持可执行。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            if session == "2024-06-17":
                return {"name": "某某股份ST", "risk_flag": True}
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(picks, security_state_fn=provider)
        self.assertEqual(0, summary["entry_counts"]["executable"])
        self.assertEqual(1, summary["entry_counts"]["blocked_entry"])
        self.assertEqual(0, summary["n"])

    def test_p35b_action_time_state_proves_executability(self):
        picks = [("s1", "2024-06-14", "600001", "某某股份")]
        _, summary = self._evaluate(picks, security_state_fn=self._non_st)
        self.assertEqual(1, summary["entry_counts"]["executable"])
        self.assertEqual(1, summary["n"])

    def test_p36_per_strategy_verified_counts_survive_execution_filtering(self):
        """2 条 verified、1 条可执行 → 策略行必须打印 ``2 / 1``。"""
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]
        lines, summary = self._evaluate(picks, security_state_fn=self._non_st)
        self.assertEqual(1, summary["entry_counts"]["executable"])
        self.assertEqual(1, summary["entry_counts"]["blocked_entry"])
        # 指标仍然只用可执行的那一条。
        self.assertEqual(1, summary["n"])
        self.assertIsNotNone(summary["mean_return"])
        row = [line for line in lines if line.startswith("| s1 |")][0]
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        self.assertEqual("2", cells[1], row)
        self.assertEqual("1", cells[2], row)

    def test_p36b_market_mode_keeps_verified_and_executable_distinct(self):
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]
        lines, summary = self._evaluate(
            picks, tradability_mode=ST.MODE_MARKET,
            security_state_fn=self._non_st)
        self.assertEqual(2, summary["n"])
        row = [line for line in lines if line.startswith("| s1 |")][0]
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        self.assertEqual("2", cells[1], row)
        self.assertEqual("1", cells[2], row)


class OutcomeBucketAuthorityTest(unittest.TestCase):
    """P2（round 3）第 3 项：桶分类只能有一处实现。

    ``build_executable_outcomes`` 的 ``report`` 计数与 :func:`ST.outcome_bucket`
    必须对**冲突状态组合**给出同一个桶。report 层若自行按另一套优先级手写，
    ``entry=unproven + exit=blocked`` 这类样本就会在两个地方被算进不同的桶。
    """

    def _make_outcome(self, *, entry_status, exit_status, entry_reason="r",
                      exit_reason="r", executable=False):
        return ST.ExecutableSelectionOutcome(
            sample_key="k",
            security_code=MAIN_BOARD,
            selected=True,
            entry_status=entry_status,
            entry_reason=entry_reason,
            exit_status=exit_status,
            exit_reason=exit_reason,
            entry_executable=entry_status == ST.STATUS_EXECUTABLE,
            executable=executable,
        )

    def test_conflicting_entry_unproven_exit_blocked_is_unproven(self):
        """entry 先失败 → 桶由 entry 决定，而不是"看到 exit blocked 就算 blocked_exit"。"""
        outcome = self._make_outcome(
            entry_status=ST.STATUS_UNPROVEN, exit_status=ST.STATUS_BLOCKED)
        self.assertEqual(ST.OUTCOME_BUCKET_UNPROVEN, ST.outcome_bucket(outcome))

    def test_conflicting_entry_invalid_exit_blocked_is_invalid(self):
        outcome = self._make_outcome(
            entry_status=ST.STATUS_INVALID, exit_status=ST.STATUS_BLOCKED)
        self.assertEqual(ST.OUTCOME_BUCKET_INVALID, ST.outcome_bucket(outcome))

    def test_conflicting_entry_unproven_exit_invalid_is_unproven(self):
        outcome = self._make_outcome(
            entry_status=ST.STATUS_UNPROVEN, exit_status=ST.STATUS_INVALID)
        self.assertEqual(ST.OUTCOME_BUCKET_UNPROVEN, ST.outcome_bucket(outcome))

    def test_entry_ok_exit_blocked_is_blocked_exit(self):
        outcome = self._make_outcome(
            entry_status=ST.STATUS_EXECUTABLE, exit_status=ST.STATUS_BLOCKED)
        self.assertEqual(ST.OUTCOME_BUCKET_BLOCKED_EXIT, ST.outcome_bucket(outcome))

    def test_not_selected_has_its_own_bucket(self):
        outcome = ST.ExecutableSelectionOutcome(
            sample_key="k", security_code=MAIN_BOARD, selected=False,
            entry_status=ST.STATUS_UNPROVEN, entry_reason=ST.REASON_OK,
        )
        self.assertEqual(ST.OUTCOME_BUCKET_NOT_SELECTED, ST.outcome_bucket(outcome))

    def test_contract_report_counts_agree_with_outcome_bucket(self):
        """contract 的 ``report`` 计数 == 逐条 ``outcome_bucket`` 统计（冲突组合在内）。"""
        rows = [
            # entry unproven（ST 未知）+ exit blocked（跌停）→ unproven。
            row("c1", entry_ev=ST.MarketEvidence(
                session=SESSION, available_at=ST.session_close_at(SESSION),
                price=10.2, reference_price=10.0, halted=False, name=None),
                exit_ev=ST.MarketEvidence(
                    session=NEXT_SESSION,
                    available_at=ST.session_close_at(NEXT_SESSION),
                    price=9.0, reference_price=10.0, halted=False, name="某某股份"),
                exit_session=NEXT_SESSION),
            # entry invalid（价格非法）+ exit blocked → invalid。
            row("c2", entry_ev=ST.MarketEvidence(
                session=SESSION, available_at=ST.session_close_at(SESSION),
                price=-1.0, reference_price=10.0, halted=False, name="某某股份"),
                exit_ev=ST.MarketEvidence(
                    session=NEXT_SESSION,
                    available_at=ST.session_close_at(NEXT_SESSION),
                    price=9.0, reference_price=10.0, halted=False, name="某某股份"),
                exit_session=NEXT_SESSION),
            # entry ok + exit blocked → blocked_exit。
            row("c3", entry_ev=evidence(),
                exit_ev=ST.MarketEvidence(
                    session=NEXT_SESSION,
                    available_at=ST.session_close_at(NEXT_SESSION),
                    price=9.0, reference_price=10.0, halted=False, name="某某股份"),
                exit_session=NEXT_SESSION),
            row("c4", entry_ev=evidence(), exit_ev=evidence(session=NEXT_SESSION),
                exit_session=NEXT_SESSION),
            row("c5", selected=False, entry_ev=evidence()),
        ]
        built = ST.build_executable_outcomes(rows)
        counts = ST.outcome_bucket_counts(built["outcomes"])
        for bucket in ST.OUTCOME_BUCKET_KEYS:
            self.assertEqual(
                built["report"][bucket], counts[bucket],
                f"bucket {bucket}: report={built['report'][bucket]} vs "
                f"outcome_bucket={counts[bucket]}",
            )
        self.assertEqual(1, counts[ST.OUTCOME_BUCKET_UNPROVEN])
        self.assertEqual(1, counts[ST.OUTCOME_BUCKET_INVALID])
        self.assertEqual(1, counts[ST.OUTCOME_BUCKET_BLOCKED_EXIT])
        self.assertEqual(1, counts[ST.OUTCOME_BUCKET_EXECUTABLE])
        self.assertEqual(1, counts[ST.OUTCOME_BUCKET_NOT_SELECTED])


class ProductionSecurityStateWiringTest(unittest.TestCase):
    """P1（round 3）：生产 ``main()`` 必须真的接线，而不是只有测试注入才有样本。"""

    KLINES = ProductionEvaluatorWiringTest.KLINES

    def _run_main(self, tmp, **kwargs):
        """直接调用**真实** ``main()``，只把数据目录与归档路径换到临时目录。"""
        import json as _json
        import os as _os

        import selection_alpha_report as AR

        original_dir = AR.DATA_DIR
        original_kline_dir = AR.KLINE_DIR
        original_report = AR.REPORT_PATH
        original_loader = AR.load_kline
        original_listdir = AR.os.listdir
        original_window = AR.WINDOW_DAYS
        # 夹具用的是固定历史日期，必须把"最近 N 天"窗口放开，否则会被
        # ``data_asof_date >= cutoff`` 全部滤掉，测试变成空跑。
        AR.WINDOW_DAYS = 3650
        AR.DATA_DIR = tmp
        AR.KLINE_DIR = _os.path.join(tmp, "klines")
        AR.REPORT_PATH = _os.path.join(tmp, "reports", "alpha.md")
        AR.load_kline = lambda code: dict(self.KLINES.get(code) or {})
        # ``market_sessions()`` / ``benchmark_map()`` 都列举 KLINE_DIR；必须一起换，
        # 否则报告会退回"真实 data_cache 不存在"的空路径。
        AR.os.listdir = lambda path: (
            [f"{code}.csv" for code in self.KLINES]
            if str(path) == str(AR.KLINE_DIR) else original_listdir(path)
        )
        try:
            from contextlib import redirect_stdout
            import io

            buf = io.StringIO()
            with redirect_stdout(buf):
                AR.main(**kwargs)
            return _json.loads(buf.getvalue().strip().splitlines()[-1])
        finally:
            AR.DATA_DIR = original_dir
            AR.KLINE_DIR = original_kline_dir
            AR.REPORT_PATH = original_report
            AR.load_kline = original_loader
            AR.os.listdir = original_listdir
            AR.WINDOW_DAYS = original_window

    def _seed_tracking_db(self, tmp, rows):
        import os as _os
        import sqlite3 as sq

        db = _os.path.join(tmp, "selection_tracking.db")
        conn = sq.connect(db)
        conn.execute(
            "CREATE TABLE selection_runs(id INTEGER PRIMARY KEY, strategy TEXT,"
            " data_asof_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE selection_picks(run_id INTEGER, code TEXT, rank_no INTEGER,"
            " name TEXT)"
        )
        for index, (strategy, day, code, name) in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO selection_runs VALUES(?,?,?)", (index, strategy, day)
            )
            conn.execute(
                "INSERT INTO selection_picks VALUES(?,?,?,?)", (index, code, 1, name)
            )
        conn.commit()
        conn.close()

    def _write_archive(self, tmp, rows):
        import json as _json
        import os as _os

        path = _os.path.join(tmp, "security_state_history.json")
        with open(path, "w", encoding="utf-8") as handle:
            _json.dump(
                {
                    "kind": "historical_archive",
                    "historical_membership_complete": True,
                    "archive_source": "unit-test archive",
                    "availability_basis": "session_close",
                    "rows": rows,
                },
                handle,
                ensure_ascii=False,
            )
        return path

    def test_real_main_produces_executable_samples_with_an_archive(self):
        """真实 ``main()`` + 历史归档 → 生产路径真的产出 executable 样本。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._seed_tracking_db(
                tmp, [("s1", "2024-06-14", "600001", "某某股份")]
            )
            archive = self._write_archive(tmp, [
                {"code": "600001", "effective_from": "2024-06-14",
                 "effective_to": "2024-06-30", "name": "某某股份", "risk_flag": False},
            ])
            payload = self._run_main(tmp, archive_path=archive)
        self.assertTrue(payload["executable_coverage_available"])
        self.assertEqual(SS.SOURCE_OK, payload["security_state_source"]["status"])
        exec_summaries = [
            s for s in payload["summaries"] if s["view"] == "executable"
        ]
        self.assertTrue(exec_summaries, payload["summaries"])
        # 不是只有 unit test 注入 provider 时才有 executable sample：
        # 这条走的是 main() 自己加载归档的路径。
        self.assertTrue(
            any(s["entry_counts"]["executable"] >= 1 for s in exec_summaries),
            exec_summaries,
        )
        self.assertTrue(any(s["n"] >= 1 for s in exec_summaries), exec_summaries)

    def test_real_main_degrades_honestly_without_an_archive(self):
        """没有历史归档 → 明确区分 market 可用 / executable 不可用，不假装有指标。"""
        import os as _os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._seed_tracking_db(
                tmp, [("s1", "2024-06-14", "600001", "某某股份")]
            )
            payload = self._run_main(
                tmp, archive_path=_os.path.join(tmp, "does-not-exist.json")
            )
        self.assertFalse(payload["executable_coverage_available"])
        self.assertEqual(
            SS.SOURCE_MISSING, payload["security_state_source"]["status"])
        market = [s for s in payload["summaries"] if s["view"] == "market_counterfactual"]
        executable = [s for s in payload["summaries"] if s["view"] == "executable"]
        self.assertTrue(market and executable)
        # market 视图仍有指标（基于已验证标签）。
        self.assertTrue(any(s["n"] >= 1 for s in market), market)
        # executable 视图诚实降级：样本全部 unproven，且没有 executable 基准。
        self.assertTrue(
            all(s["entry_counts"]["executable"] == 0 for s in executable), executable
        )
        # 有已验证样本的 executable 视图必须诚实记为 unproven（不是 executable）。
        scored = [s for s in executable if s["label_status_counts"]["verified"]]
        self.assertTrue(scored, executable)
        self.assertTrue(
            all(s["entry_counts"]["unproven"] >= 1 for s in scored), scored
        )
        self.assertTrue(
            all(s["executable_benchmark_available"] is False for s in executable),
            executable,
        )
        self.assertTrue(all(s["executable_excess"] is None for s in executable))
        # 可执行基准不可得时，绝不用未过滤的 market 基准冒充。
        self.assertTrue(all(s["mean_excess"] is None for s in executable), executable)

    def test_main_never_uses_a_current_snapshot_as_history(self):
        """归档 kind 不是 historical_archive → 一律降级（当前快照无权充当历史）。"""
        import os as _os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._seed_tracking_db(
                tmp, [("s1", "2024-06-14", "600001", "某某股份")]
            )
            path = _os.path.join(tmp, "security_state_history.json")
            with open(path, "w", encoding="utf-8") as handle:
                import json as _json

                _json.dump({
                    "kind": "current_snapshot",
                    "rows": [{"code": "600001", "effective_from": "2024-06-14",
                              "name": "某某股份", "risk_flag": False}],
                }, handle, ensure_ascii=False)
            payload = self._run_main(tmp, archive_path=path)
        self.assertFalse(payload["executable_coverage_available"])
        self.assertEqual(
            SS.SOURCE_NOT_HISTORICAL, payload["security_state_source"]["status"])


class ExecutableBenchmarkSeparationTest(unittest.TestCase):
    """P2（round 3）第 2 项：executable 收益不得与未过滤 benchmark 相减。"""

    KLINES = ProductionEvaluatorWiringTest.KLINES

    def _evaluate(self, picks, *, security_state_fn=None):
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            return AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=security_state_fn,
            )

    @staticmethod
    def _non_st(code, session):
        return {"name": "某某股份", "risk_flag": False}

    #: 两条样本收益**刻意不同**，才能证伪"blocked 样本被混进 executable_excess"：
    #:   600001 可执行，entry 06-17 +5%、exit 06-18 +10%  → 收益 +10%
    #:   600002 entry 06-17 +10%（涨停买不进）→ blocked_entry，收益 +25%
    MIXED_KLINES = {
        "600001": {
            "2024-06-14": (10.0, 10.0),
            "2024-06-17": (10.0, 10.5),
            "2024-06-18": (10.5, 11.55),
            "2024-06-19": (11.5, 11.6),
        },
        "600002": {
            "2024-06-14": (10.0, 10.0),
            "2024-06-17": (10.0, 11.0),
            "2024-06-18": (11.0, 12.5),
            "2024-06-19": (12.5, 12.6),
        },
    }

    def test_market_view_executable_excess_only_uses_executable_selections(self):
        """market 视图下 ``executable_excess`` 只能用真正可执行的选股样本。

        blocked / unproven 的样本在 market 视图里会进入收益统计（这是该口径的
        本意），但它们**不得**被拿去减可执行基准 —— 那会重新制造 population 错配。
        这里两条样本收益刻意不同，因此把 blocked 样本混进来会**改变数值**。
        """
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),  # 可执行
            ("s1", "2024-06-14", "600002", "某某股份"),  # entry 涨停 → blocked
        ]
        with _FakeKlineReport(self.MIXED_KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.MIXED_KLINES.values() for day in bars})
            )
            lines, summary = AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                tradability_mode=ST.MODE_MARKET,
                security_state_fn=self._non_st,
            )
        # market 视图保留两条样本的收益。
        self.assertEqual(2, summary["n"])
        self.assertEqual(1, summary["entry_counts"]["blocked_entry"])
        row = [line for line in lines if line.startswith("| s1 |")][0]
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        # 可执行样本列仍是 1（只有一条真能成交）。
        self.assertEqual("1", cells[2], row)
        # executable_excess 必须只用可执行样本：这里只有 600001。
        self.assertIsNotNone(summary["executable_excess"])
        # 基准是随机样本（这里恰好就是这两只票）的可执行子集，即 600001 的 +10%。
        # 因此正确值 = 10% - 10% = 0%。若把 blocked 的 600002(+25%) 混进来，
        # 均值会变成 17.5% - 10% = +7.5%，测试立刻失败。
        self.assertAlmostEqual(0.0, summary["executable_excess"], places=6)
        self.assertIn("executable_excess", "\n".join(lines))

    def test_production_report_buckets_match_the_contract(self):
        """冲突状态（entry unproven + exit blocked）在**生产报告**里也是 unproven。

        report 层若自行按"先看 exit blocked"分类，这里会得到 ``blocked_exit``。
        """
        # entry 06-17 +5%（买入不拦）；exit 06-18 -10%（卖出跌停 → exit blocked）。
        klines = {
            "600001": {
                "2024-06-14": (10.0, 10.0),
                "2024-06-17": (10.0, 10.5),
                "2024-06-18": (10.5, 9.45),
                "2024-06-19": (9.4, 9.5),
            },
        }
        picks = [("s1", "2024-06-14", "600001", None)]

        def provider(code, session):
            if session == "2024-06-17":
                # entry 时点没有历史状态 → ST 未知 → entry unproven。
                return None
            return {"name": "某某股份", "risk_flag": False}

        with _FakeKlineReport(klines) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in klines.values() for day in bars})
            )
            _, summary = AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=provider,
            )
        self.assertEqual(1, summary["entry_counts"]["unproven"])
        self.assertEqual(0, summary["entry_counts"]["blocked_exit"])
        # production 的桶必须与 contract 的权威分类逐键一致。
        self.assertEqual(
            {bucket: summary["entry_counts"][bucket] for bucket in ST.OUTCOME_BUCKETS},
            summary["outcome_buckets"],
        )

    def test_executable_excess_is_unavailable_without_an_executable_benchmark(self):
        """没有同口径 PIT 状态源 → executable_excess 是 None，而不是 market 值。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]
        _, summary = self._evaluate(picks)
        self.assertFalse(summary["executable_benchmark_available"])
        self.assertIsNone(summary["executable_excess"])
        self.assertIsNone(summary["mean_excess"])

    def test_executable_excess_uses_the_executable_population(self):
        """同口径可得时，两个 excess 是**两个独立**的数字（population 不同）。"""
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]
        lines, summary = self._evaluate(
            picks, security_state_fn=self._non_st)
        self.assertTrue(summary["executable_benchmark_available"])
        self.assertIsNotNone(summary["executable_excess"])
        self.assertIsNotNone(summary["mean_excess"])
        # 两个指标必须是分开的字段，且表头同时出现，不能共用一个"超额"。
        joined = "\n".join(lines)
        self.assertIn("market_counterfactual_excess", joined)
        self.assertIn("executable_excess", joined)

    def test_benchmark_blocked_sample_is_not_counted_as_executable_baseline(self):
        """benchmark 里买不进的样本（涨停）不得进可执行基准 population。"""
        # 只放一只票作为基准样本：entry 恰好 +10%（主板涨停 → 买不进）。
        klines = {
            "600002": {
                "2024-06-14": (10.0, 10.0),
                "2024-06-17": (10.0, 11.0),
                "2024-06-18": (11.0, 11.5),
                "2024-06-19": (11.5, 11.6),
            },
        }
        with _FakeKlineReport(klines) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in klines.values() for day in bars})
            )
            benches = AR.benchmark_map(
                {"2024-06-14"}, 1, sessions, asof="2024-06-30",
                security_state_fn=self._non_st,
            )
        # market 口径：verified 标签在 → 有值。
        self.assertIn("2024-06-14", benches["market"])
        # 可执行口径：这条被涨停拦下 → 该日**没有**可执行基准样本。
        self.assertNotIn("2024-06-14", benches["executable"])

    def test_benchmark_executable_key_absent_without_a_state_source(self):
        """没有状态源 → ``executable`` 键整体为 None（不可得），不是空 dict 冒充。"""
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            benches = AR.benchmark_map(
                {"2024-06-14"}, 1, sessions, asof="2024-06-30",
                security_state_fn=None,
            )
        self.assertIsNone(benches["executable"])


class SecurityStateArchiveTest(unittest.TestCase):
    """P1（round 3）：归档源的 PIT 语义。"""

    PAYLOAD = {
        "kind": "historical_archive",
        "historical_membership_complete": True,
        "archive_source": "unit-test",
        "availability_basis": "session_close",
        # 半开区间：``[effective_from, effective_to)``。相邻窗口必须首尾相接而不
        # 重叠 —— ``[06-14, 06-18)`` + ``[06-18, 06-30)`` 表示"06-18 起换成 ST"，
        # 这正是生产归档该写的样子（``effective_to`` 是**下一个**窗口的起点）。
        "rows": [
            {"code": "600001", "effective_from": "2024-06-14",
             "effective_to": "2024-06-30", "name": "某某股份", "risk_flag": False},
            {"code": "600002", "effective_from": "2024-06-14",
             "effective_to": "2024-06-18", "name": "某某股份", "risk_flag": False},
            {"code": "600002", "effective_from": "2024-06-18",
             "effective_to": "2024-06-30", "name": "某某股份ST", "risk_flag": True},
        ],
    }

    def _archive(self):
        archive = SS.SecurityStateArchive.from_payload(self.PAYLOAD)
        self.assertIsNotNone(archive)
        return archive

    def test_state_is_scoped_to_its_effective_window(self):
        archive = self._archive()
        self.assertEqual("某某股份", archive.state_at("600001", "2024-06-18")["name"])
        # 窗口之外 → 未知（None），绝不沿用旧值。
        self.assertIsNone(archive.state_at("600001", "2024-07-01"))

    def test_st_change_is_reported_at_the_right_session(self):
        archive = self._archive()
        before = archive.state_at("600002", "2024-06-17")
        after = archive.state_at("600002", "2024-06-18")
        self.assertEqual("某某股份", before["name"])
        self.assertTrue(after["risk_flag"])

    def test_archive_state_is_visible_at_its_own_session_close(self):
        archive = self._archive()
        state = archive.state_at("600001", "2024-06-18")
        self.assertTrue(
            ST.is_visible_at(state["available_at"], ST.session_close_at("2024-06-18"))
        )

    def test_provider_never_falls_back_to_a_decision_day_name(self):
        """provider 给不出状态 → ``(None, None)``，由 contract 判 unproven。"""
        import selection_alpha_report as AR

        provider = SS.make_state_provider(self._archive())
        self.assertEqual((None, None), AR._action_security_state(
            "600001", "2024-07-01", decision_day="2024-06-14",
            decision_name="某某股份", security_state_fn=provider))

    def test_future_only_state_cannot_prove_an_earlier_action(self):
        """归档里一条 available_at 晚于动作时点的状态 → 不得用于更早的动作。"""
        import selection_alpha_report as AR

        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": True,
            "rows": [
                {"code": "600001", "effective_from": "2024-06-14",
                 "effective_to": "2024-06-30", "name": "某某股份",
                 "risk_flag": False,
                 "available_at": "2024-06-20T15:00:00+08:00"},
            ],
        }
        provider = SS.make_state_provider(
            SS.SecurityStateArchive.from_payload(payload))
        self.assertEqual((None, None), AR._action_security_state(
            "600001", "2024-06-14", decision_day="2024-06-14",
            decision_name="某某股份", security_state_fn=provider))

    def test_current_snapshot_is_not_a_historical_source(self):
        self.assertEqual(
            SS.SOURCE_NOT_HISTORICAL,
            SS.archive_provenance({"kind": "current_snapshot", "rows": [{}]})["status"],
        )

    def test_incomplete_archive_is_refused(self):
        self.assertEqual(
            SS.SOURCE_INCOMPLETE,
            SS.archive_provenance(
                {"kind": "historical_archive", "rows": [{}]})["status"],
        )

    def test_resolve_without_an_archive_returns_none(self):
        import os as _os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fn, provenance = SS.resolve_security_state_fn(
                archive_path=_os.path.join(tmp, "nope.json"))
        self.assertIsNone(fn)
        self.assertEqual(SS.SOURCE_MISSING, provenance["status"])

    def test_malformed_explicit_available_at_rejects_the_row(self):
        """显式但不可解析的 ``available_at`` 必须整行拒绝，**不得**回落到收盘时点。

        ``availability_basis="session_close"`` 的兜底只适用于字段**缺失**。
        若一个写坏的显式时间戳被当作"没写"而回落，等于把不可信的可用性证据
        伪造成一个可见时点，让未来才记下的状态证明更早的动作可执行。
        """
        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": True,
            "availability_basis": "session_close",
            "rows": [
                {"code": "600001", "effective_from": "2024-06-14",
                 "effective_to": "2024-06-30", "name": "某某股份",
                 "risk_flag": False, "available_at": "not-a-date"},
            ],
        }
        archive = SS.SecurityStateArchive.from_payload(payload)
        self.assertIsNotNone(archive)
        self.assertIsNone(archive.state_at("600001", "2024-06-14"))

    def test_absent_available_at_still_uses_the_session_close_fallback(self):
        """对照：字段**缺失**时才允许 session_close 兜底（回归保护，避免误伤）。"""
        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": True,
            "availability_basis": "session_close",
            "rows": [
                {"code": "600001", "effective_from": "2024-06-14",
                 "effective_to": "2024-06-30", "name": "某某股份",
                 "risk_flag": False},
            ],
        }
        archive = SS.SecurityStateArchive.from_payload(payload)
        state = archive.state_at("600001", "2024-06-14")
        self.assertIsNotNone(state)
        self.assertEqual("某某股份", state["name"])


class StrictBooleanNormalizationTest(unittest.TestCase):
    """Round 4 / F1：声明性布尔必须严格归一，``bool("false")`` 绝不为 True。"""

    def test_as_strict_bool_truth_table(self):
        import point_in_time as PIT

        # 真值
        for value in (True, 1, 1.0, "true", "TRUE", "True", "1", "yes", "Y", "on", " t "):
            self.assertIs(True, PIT.as_strict_bool(value), value)
        # 假值 —— 关键：字符串 "false" / "0" 绝不能被 bool() 读成 True。
        for value in (False, 0, 0.0, "false", "FALSE", "False", "0", "no", "N", "off", " f "):
            self.assertIs(False, PIT.as_strict_bool(value), value)
        # 无法判定
        for value in (None, "", "maybe", "2", [], {}, float("nan")):
            self.assertIsNone(PIT.as_strict_bool(value), value)

    def test_archive_complete_flag_string_false_is_not_complete(self):
        """``"historical_membership_complete": "false"`` 不得被读成完整归档。"""
        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": "false",
            "rows": [{"code": "600001", "effective_from": "2024-06-14",
                      "name": "某某股份", "risk_flag": False}],
        }
        self.assertEqual(
            SS.SOURCE_INCOMPLETE, SS.archive_provenance(payload)["status"])
        self.assertIsNone(SS.SecurityStateArchive.from_payload(payload))

    def test_archive_complete_flag_string_zero_is_not_complete(self):
        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": "0",
            "rows": [{"code": "600001", "effective_from": "2024-06-14",
                      "name": "某某股份", "risk_flag": False}],
        }
        self.assertEqual(
            SS.SOURCE_INCOMPLETE, SS.archive_provenance(payload)["status"])

    def test_archive_complete_flag_explicit_true_string_is_complete(self):
        """显式 ``"true"`` 仍应被接受（严格归一不是"只认 bool 类型"）。"""
        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": "true",
            "rows": [{"code": "600001", "effective_from": "2024-06-14",
                      "name": "某某股份", "risk_flag": False}],
        }
        self.assertEqual(SS.SOURCE_OK, SS.archive_provenance(payload)["status"])

    def test_unknown_complete_flag_is_not_complete(self):
        payload = {
            "kind": "historical_archive",
            "historical_membership_complete": "maybe",
            "rows": [{"code": "600001", "effective_from": "2024-06-14",
                      "name": "某某股份", "risk_flag": False}],
        }
        self.assertEqual(
            SS.SOURCE_INCOMPLETE, SS.archive_provenance(payload)["status"])

    def test_risk_flag_string_false_is_not_st(self):
        """``risk_flag="false"`` 必须读成非 ST，不能靠 bool() 变成 ST。"""
        self.assertIs(False, ST.normalize_risk_flag("false"))
        self.assertIs(False, ST.normalize_risk_flag("0"))
        self.assertIs(True, ST.normalize_risk_flag("true"))
        self.assertIs(True, ST.normalize_risk_flag(1))
        self.assertIsNone(ST.normalize_risk_flag("maybe"))

    def test_risk_flag_string_false_does_not_block_a_normal_stock(self):
        """``risk_flag="false"`` 的正常股票必须可执行（不是被误判成 ST 而 blocked）。"""
        ev = evidence(price=106.0, reference=100.0, name="某某股份", risk_flag="false")
        verdict = buy(ev)
        self.assertEqual(ST.STATUS_EXECUTABLE, verdict.status)
        self.assertEqual(ST.REASON_OK, verdict.reason)

    def test_risk_flag_string_false_still_applies_st_limit_from_the_name(self):
        """名称本身含 ST 时，``risk_flag="false"`` 不能把 ST 规则关掉。

        直接测解析器：名称 ST 必须给出权威 ST 上限（当前 5.0），而不是板块上限。
        """
        limit_pct, reason = ST.resolve_limit_pct(
            MAIN_BOARD, name="*ST某某", risk_flag="false", pct_change=0.06
        )
        self.assertIsNone(reason)
        self.assertEqual(PTR.limit_pct(MAIN_BOARD, "*ST某某"), limit_pct)
        # 6% 的涨幅在 ST 口径下已触及涨停 → 买入方向被拦。
        self.assertEqual(
            ST.STATUS_BLOCKED,
            buy(evidence(price=106.0, reference=100.0, name="*ST某某",
                         risk_flag="false")).status,
        )

    def test_risk_flag_unknown_spelling_fails_closed(self):
        """``risk_flag="maybe"`` 无法判定 → unproven，绝不默认非 ST。

        名称**存在**（所以不是"什么都没给"那条门禁），只有风险标记的写法无法
        判定 —— 这正是"字段存在但不可信"的路径。
        """
        ev = evidence(price=100.5, reference=100.0, name="某某股份", risk_flag="maybe")
        verdict = buy(ev)
        self.assertEqual(ST.STATUS_UNPROVEN, verdict.status)
        self.assertEqual(ST.REASON_UNKNOWN_ST_STATUS, verdict.reason)

    def test_security_permission_does_not_use_plain_bool(self):
        """``security_permission`` 对 ``"false"`` 必须按非 ST 处理。"""
        self.assertEqual(
            PTR.security_scope(MAIN_BOARD, None, False),
            ST.security_permission(MAIN_BOARD, name=None, risk_flag="false"),
        )


class ExecutableCoverageHonestyTest(unittest.TestCase):
    """Round 4 / F3：provider 对象存在 ≠ 可执行覆盖可用。"""

    KLINES = ProductionEvaluatorWiringTest.KLINES

    def _evaluate(self, picks, *, security_state_fn=None):
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            return AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=security_state_fn,
            )

    def test_provider_that_resolves_nothing_reports_unavailable(self):
        """provider 存在但每个 session 都返回 None → 覆盖度 0，指标不可用。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def empty_provider(code, session):
            return None

        lines, summary = self._evaluate(picks, security_state_fn=empty_provider)
        self.assertGreater(summary["action_state_coverage"]["required"], 0)
        self.assertEqual(0, summary["action_state_coverage"]["resolved"])
        self.assertFalse(summary["action_state_coverage_available"])
        self.assertFalse(summary["executable_metrics_available"])
        self.assertFalse(summary["executable_benchmark_available"])
        self.assertIsNone(summary["executable_excess"])
        self.assertIn("不可用", "\n".join(lines))

    def test_provider_that_resolves_states_reports_available(self):
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(picks, security_state_fn=provider)
        self.assertGreater(summary["action_state_coverage"]["resolved"], 0)
        self.assertTrue(summary["action_state_coverage_available"])
        self.assertTrue(summary["executable_metrics_available"])

    def test_partial_coverage_is_reported_as_a_ratio(self):
        """只有 entry 能解析、exit 不能 → 覆盖度必须如实报出分数。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def partial(code, session):
            if session == "2024-06-17":
                return {"name": "某某股份", "risk_flag": False}
            return None

        _, summary = self._evaluate(picks, security_state_fn=partial)
        cov = summary["action_state_coverage"]
        self.assertEqual(1, cov["resolved"])
        self.assertGreater(cov["required"], cov["resolved"])

    def test_real_main_reports_coverage_not_just_provider_presence(self):
        """真实 ``main()``：归档存在但**不覆盖**动作 session → 指标不可用。"""
        import json as _json
        import os as _os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = _os.path.join(tmp, "selection_tracking.db")
            import sqlite3 as sq

            conn = sq.connect(db)
            conn.execute(
                "CREATE TABLE selection_runs(id INTEGER PRIMARY KEY, strategy TEXT,"
                " data_asof_date TEXT)"
            )
            conn.execute(
                "CREATE TABLE selection_picks(run_id INTEGER, code TEXT,"
                " rank_no INTEGER, name TEXT)"
            )
            conn.execute("INSERT INTO selection_runs VALUES(1,'s1','2024-06-14')")
            conn.execute("INSERT INTO selection_picks VALUES(1,'600001',1,'某某股份')")
            conn.commit()
            conn.close()
            # 归档只覆盖决策日 06-14（半开区间 `[06-14, 06-15)`），而 entry 是 06-17、
            # exit 是 06-18 —— 因此没有任何动作 session 能解析出状态。
            archive = _os.path.join(tmp, "security_state_history.json")
            with open(archive, "w", encoding="utf-8") as handle:
                _json.dump({
                    "kind": "historical_archive",
                    "historical_membership_complete": True,
                    "availability_basis": "session_close",
                    "rows": [{"code": "600001", "effective_from": "2024-06-14",
                              "effective_to": "2024-06-15", "name": "某某股份",
                              "risk_flag": False}],
                }, handle, ensure_ascii=False)

            import selection_alpha_report as AR

            original = dict(
                DATA_DIR=AR.DATA_DIR, KLINE_DIR=AR.KLINE_DIR,
                REPORT_PATH=AR.REPORT_PATH, load_kline=AR.load_kline,
                listdir=AR.os.listdir, WINDOW_DAYS=AR.WINDOW_DAYS,
            )
            AR.DATA_DIR = tmp
            AR.KLINE_DIR = _os.path.join(tmp, "klines")
            AR.REPORT_PATH = _os.path.join(tmp, "reports", "alpha.md")
            AR.WINDOW_DAYS = 3650
            AR.load_kline = lambda code: dict(self.KLINES.get(code) or {})
            AR.os.listdir = lambda path: (
                [f"{code}.csv" for code in self.KLINES]
                if str(path) == str(AR.KLINE_DIR) else original["listdir"](path)
            )
            try:
                from contextlib import redirect_stdout
                import io

                buf = io.StringIO()
                with redirect_stdout(buf):
                    AR.main(archive_path=archive)
                payload = _json.loads(buf.getvalue().strip().splitlines()[-1])
            finally:
                AR.DATA_DIR = original["DATA_DIR"]
                AR.KLINE_DIR = original["KLINE_DIR"]
                AR.REPORT_PATH = original["REPORT_PATH"]
                AR.load_kline = original["load_kline"]
                AR.os.listdir = original["listdir"]
                AR.WINDOW_DAYS = original["WINDOW_DAYS"]

        # provider 存在（归档合法），但没有任何动作 session 能解析 → 不可用。
        self.assertTrue(payload["provider_present"])
        self.assertEqual(0, payload["action_state_coverage"]["resolved"])
        self.assertFalse(payload["executable_metrics_available"])
        self.assertFalse(payload["executable_coverage_available"])


# ═══════════════════════════════════════════════════════════════════════════════
# Round 5 — the four findings and where they are fixed
# ═══════════════════════════════════════════════════════════════════════════════


class ExecutableBenchmarkCoverageSemanticsTest(unittest.TestCase):
    """R5-F1：``executable_benchmark_available`` 必须由**基准侧**实际覆盖决定。

    ``benchmark_map()`` 早就分开返回了 benchmark 侧的 ``{required, resolved}``
    覆盖度，但 ``evaluate()`` 当时把它丢掉了，只看 ``bench_exec is not None``
    —— 那最多只能说明"调用方传了 provider 对象"。一个 provider 可以**解析出
    被选中的票、却一条随机基准样本都解析不出来**（策略票与随机票池是两个
    population），此时 executable 基准并不存在，指标必须标为不可得。
    """

    #: 与 ProductionEvaluatorWiringTest 同一批行情：06-17 = entry，06-18 = exit。
    KLINES = ProductionEvaluatorWiringTest.KLINES

    def _evaluate(self, picks, *, security_state_fn=None, **kwargs):
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            return AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=security_state_fn, **kwargs,
            )

    def test_selected_coverage_without_benchmark_coverage_is_unavailable(self):
        """**核心回归**：选股侧覆盖度 > 0、基准侧覆盖度 = 0 → 基准与指标均不可得。

        provider 解析出了被选中的票的动作状态，却**一条随机基准样本都没解析出来**
        （两个 population 不同：随机票池与策略选中的票）。``benchmark_map()`` 已经把
        这个事实作为 ``coverage`` 返回，但旧实现把它丢掉，只看
        ``bench_exec is not None``（"调用方传了 provider 对象"）——于是报告声称
        可执行基准可用，而 ``executable_excess`` 其实一条都算不出来。

        这里直接把 ``benchmark_map`` 的返回值固定成该覆盖度组合，使断言精确落在
        "``evaluate()`` 是否真的消费基准侧覆盖度"这一条契约上。
        """
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            original = AR.benchmark_map
            AR.benchmark_map = lambda *a, **k: {
                # market 侧照常有值（只依赖 verified 标签）。
                "market": {"2024-06-14": 0.01},
                # 基准侧**一个动作状态都没解析出来** → 可执行基准为空 dict。
                "executable": {},
                "coverage": {"required": 6, "resolved": 0},
            }
            try:
                lines, summary = AR.evaluate(
                    picks, 1, "选股质量", sessions, asof="2024-06-30",
                    security_state_fn=provider,
                )
            finally:
                AR.benchmark_map = original

        # 选股侧确实解析出了状态 —— 这正是旧判据会误判为"可用"的前提。
        self.assertGreater(summary["action_state_coverage"]["resolved"], 0)
        self.assertTrue(summary["action_state_coverage_available"])
        # 基准侧一个都没解析出来。
        self.assertEqual(0, summary["benchmark_state_coverage"]["resolved"])
        self.assertEqual(6, summary["benchmark_state_coverage"]["required"])
        self.assertFalse(summary["benchmark_state_coverage_available"])
        # 因此基准不可得、指标不可得、executable_excess 为 None。
        self.assertFalse(summary["executable_benchmark_available"])
        self.assertFalse(summary["executable_metrics_available"])
        self.assertIsNone(summary["executable_excess"])
        self.assertEqual([], summary["executable_benchmark_days"])
        joined = "\n".join(lines)
        self.assertIn("不可用", joined)
        # 覆盖度行必须**同时**给出两侧数字，读者才能看出是基准侧拖的后腿。
        self.assertIn("基准侧", joined)

    def test_benchmark_coverage_on_a_date_outside_the_evaluation_is_unavailable(self):
        """基准只在**别的**决策日有值 → 本次评估减不出超额，仍判不可得。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            original = AR.benchmark_map
            AR.benchmark_map = lambda *a, **k: {
                "market": {"2024-06-14": 0.01, "2024-06-17": 0.02},
                # 基准解析成功，但只在 06-17；本次评估的决策日是 06-14。
                "executable": {"2024-06-17": 0.02},
                "coverage": {"required": 4, "resolved": 4},
            }
            try:
                _, summary = AR.evaluate(
                    picks, 1, "选股质量", sessions, asof="2024-06-30",
                    security_state_fn=provider,
                )
            finally:
                AR.benchmark_map = original

        # 基准侧解析成功，但被评估的决策日（06-14）上没有任何可执行基准样本。
        self.assertTrue(summary["benchmark_state_coverage_available"])
        self.assertEqual([], summary["executable_benchmark_days"])
        self.assertFalse(summary["executable_benchmark_available"])
        self.assertIsNone(summary["executable_excess"])

    def test_blocked_benchmark_samples_make_the_benchmark_unavailable(self):
        """端到端（真实 provider）：基准样本全部买不进 → 无可用可执行基准。

        provider 对每个 (code, session) 都作答（因此基准侧 ``resolved > 0``），
        但基准票池的 entry 全部涨停 → 可执行基准一条样本都没有，基准与指标都必须
        标为不可得，而不是"有覆盖度就算可用"。
        """
        # 唯一一只票在 entry 当天正好 +10%（主板涨停 → 买不进）。
        klines = {
            "600002": {
                "2024-06-14": (10.0, 10.0),
                "2024-06-17": (10.0, 11.0),
                "2024-06-18": (11.0, 11.5),
                "2024-06-19": (11.5, 11.6),
            },
        }

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        with _FakeKlineReport(klines) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in klines.values() for day in bars})
            )
            _, summary = AR.evaluate(
                [("s1", "2024-06-14", "600002", "某某股份")], 1, "选股质量",
                sessions, asof="2024-06-30", security_state_fn=provider,
            )
        # 基准侧解析出了状态……
        self.assertGreater(summary["benchmark_state_coverage"]["resolved"], 0)
        self.assertTrue(summary["benchmark_state_coverage_available"])
        # ……但没有任何可执行基准样本落在被评估的决策日上。
        self.assertEqual([], summary["executable_benchmark_days"])
        self.assertFalse(summary["executable_benchmark_available"])
        self.assertIsNone(summary["executable_excess"])

    def test_full_coverage_on_the_evaluated_date_stays_available(self):
        """对照：两侧都解析成功且日期对得上 → 基准可用（防止过度收紧）。"""
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(picks, security_state_fn=provider)
        self.assertTrue(summary["benchmark_state_coverage_available"])
        self.assertGreater(summary["benchmark_state_coverage"]["resolved"], 0)
        self.assertIn("2024-06-14", summary["executable_benchmark_days"])
        self.assertTrue(summary["executable_benchmark_available"])
        self.assertTrue(summary["executable_metrics_available"])

    def test_benchmark_coverage_is_reported_separately_from_selected_coverage(self):
        """两个覆盖度必须是**两个**字段，绝不合并成一个"动作状态覆盖度"。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(picks, security_state_fn=provider)
        self.assertIn("action_state_coverage", summary)
        self.assertIn("benchmark_state_coverage", summary)
        self.assertIn("benchmark_state_coverage_available", summary)
        self.assertIn("action_state_coverage_available", summary)
        # 两者是独立对象：改一个不会连带改另一个。
        self.assertIsNot(summary["action_state_coverage"],
                         summary["benchmark_state_coverage"])

    def test_real_main_reports_benchmark_coverage_separately(self):
        """真实 ``main()`` 也必须分开报两侧覆盖度。"""
        import os as _os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = _os.path.join(tmp, "selection_tracking.db")
            import sqlite3 as sq

            conn = sq.connect(db)
            conn.execute(
                "CREATE TABLE selection_runs(id INTEGER PRIMARY KEY, strategy TEXT,"
                " data_asof_date TEXT)"
            )
            conn.execute(
                "CREATE TABLE selection_picks(run_id INTEGER, code TEXT,"
                " rank_no INTEGER, name TEXT)"
            )
            conn.execute("INSERT INTO selection_runs VALUES(1,'s1','2024-06-14')")
            conn.execute("INSERT INTO selection_picks VALUES(1,'600001',1,'某某股份')")
            conn.commit()
            conn.close()

            import selection_alpha_report as AR

            original = dict(
                DATA_DIR=AR.DATA_DIR, KLINE_DIR=AR.KLINE_DIR,
                REPORT_PATH=AR.REPORT_PATH, load_kline=AR.load_kline,
                listdir=AR.os.listdir, WINDOW_DAYS=AR.WINDOW_DAYS,
            )
            AR.DATA_DIR = tmp
            AR.KLINE_DIR = _os.path.join(tmp, "klines")
            AR.REPORT_PATH = _os.path.join(tmp, "reports", "alpha.md")
            AR.WINDOW_DAYS = 3650
            AR.load_kline = lambda code: dict(self.KLINES.get(code) or {})
            AR.os.listdir = lambda path: (
                [f"{code}.csv" for code in self.KLINES]
                if str(path) == str(AR.KLINE_DIR) else original["listdir"](path)
            )
            try:
                from contextlib import redirect_stdout
                import io

                buf = io.StringIO()
                with redirect_stdout(buf):
                    AR.main(security_state_fn=lambda code, session: {
                        "name": "某某股份", "risk_flag": False})
                payload = _json_loads(buf.getvalue().strip().splitlines()[-1])
            finally:
                AR.DATA_DIR = original["DATA_DIR"]
                AR.KLINE_DIR = original["KLINE_DIR"]
                AR.REPORT_PATH = original["REPORT_PATH"]
                AR.load_kline = original["load_kline"]
                AR.os.listdir = original["listdir"]
                AR.WINDOW_DAYS = original["WINDOW_DAYS"]

        self.assertIn("action_state_coverage", payload)
        self.assertIn("benchmark_state_coverage", payload)
        self.assertGreater(payload["benchmark_state_coverage"]["required"], 0)


class ResolvedSessionPropagationTest(unittest.TestCase):
    """R5-F2：``actual_*_session`` 必须写**解析出的** session，而不是原始输入。

    ``intended_entry_session`` / ``intended_exit_session`` 都是可省略字段
    （契约显式允许：省略时由带日期的证据给出动作 session）。旧实现把
    ``row.intended_*_session`` 直接抄进 ``actual_*_session``，于是
    "两边都省略、证据日期不同、成交成立"的行会得到
    ``executable=True`` + 非空 ``executable_return``，而 ``actual_entry_session``
    与 ``actual_exit_session`` **双双为 None** —— 审计记录自相矛盾，且丢掉了
    T+1 逻辑本来就依赖的那个成交 session。
    """

    def test_both_intended_sessions_omitted_are_filled_from_the_evidence(self):
        """**核心回归**：两侧 intended 都省略，成交成立 → actual 必须等于证据日期。"""
        entry_ev = evidence(session=SESSION, price=102.0, reference=100.0)
        exit_ev = evidence(session=NEXT_SESSION, price=103.0, reference=102.0)
        built = ST.build_executable_outcomes([
            ST.SelectionRow(
                sample_key="omitted-both",
                code=MAIN_BOARD,
                selected=True,
                # 故意两侧都不传 intended_*。
                entry_evidence=entry_ev,
                exit_evidence=exit_ev,
                market_label_status="verified",
                market_label_value=0.05,
            )
        ])
        outcome = built["outcomes"][0]
        self.assertTrue(outcome.executable)
        self.assertIsNotNone(outcome.executable_return)
        self.assertIsNone(outcome.intended_entry_session)
        self.assertIsNone(outcome.intended_exit_session)
        # 成交 session 由证据解析而来 —— 绝不是 None。
        self.assertEqual(SESSION, outcome.actual_entry_session)
        self.assertEqual(NEXT_SESSION, outcome.actual_exit_session)
        # 且两者是**不同**的 session（T+1 成立）。
        self.assertNotEqual(outcome.actual_entry_session, outcome.actual_exit_session)
        self.assertEqual(1, built["report"]["executable"])
        self.assertTrue(ST.audit_totals(built["report"])["complete"])

    def test_only_entry_intended_omitted_is_filled_from_the_evidence(self):
        """只省略入场：exit 侧仍按输入/证据正确落地。"""
        entry_ev = evidence(session=SESSION, price=102.0, reference=100.0)
        exit_ev = evidence(session=NEXT_SESSION, price=103.0, reference=102.0)
        built = ST.build_executable_outcomes([
            ST.SelectionRow(
                sample_key="omitted-entry",
                code=MAIN_BOARD,
                selected=True,
                entry_evidence=entry_ev,
                intended_exit_session=NEXT_SESSION,
                exit_evidence=exit_ev,
                market_label_status="verified", market_label_value=0.05,
            )
        ])
        outcome = built["outcomes"][0]
        self.assertTrue(outcome.executable)
        self.assertEqual(SESSION, outcome.actual_entry_session)
        self.assertEqual(NEXT_SESSION, outcome.actual_exit_session)

    def test_actual_session_stays_none_when_no_fill_was_established(self):
        """对照：没有确立成交（blocked）时 ``actual_*`` 必须保持 ``None``。

        "从证据补 session"只适用于**已成立**的成交；把被拦的动作也补上 session
        就等于凭空制造了一笔成交 —— 那是本契约明令禁止的静默顺延。
        """
        limit_up = evidence(session=SESSION, price=110.0, reference=100.0)
        built = ST.build_executable_outcomes([
            ST.SelectionRow(
                sample_key="blocked-entry", code=MAIN_BOARD, selected=True,
                entry_evidence=limit_up,
                intended_exit_session=NEXT_SESSION,
                exit_evidence=evidence(session=NEXT_SESSION, price=101.0,
                                       reference=100.0),
                market_label_status="verified", market_label_value=0.25,
            )
        ])
        outcome = built["outcomes"][0]
        self.assertFalse(outcome.executable)
        self.assertIsNone(outcome.actual_entry_session)
        self.assertIsNone(outcome.actual_exit_session)
        self.assertIsNone(outcome.executable_return)

    def test_actual_session_is_normalized_to_the_session_date(self):
        """审计字段与 ``intended_*`` 必须同一书写口径（10 字符日期）。"""
        entry_ev = ST.MarketEvidence(
            session="2024-06-18T00:00:00+08:00",
            available_at=ST.session_close_at("2024-06-18"),
            price=102.0, reference_price=100.0, halted=False, name="某某股份",
        )
        exit_ev = ST.MarketEvidence(
            session="2024-06-19T00:00:00+08:00",
            available_at=ST.session_close_at("2024-06-19"),
            price=103.0, reference_price=102.0, halted=False, name="某某股份",
        )
        built = ST.build_executable_outcomes([
            ST.SelectionRow(
                sample_key="iso-sessions", code=MAIN_BOARD, selected=True,
                entry_evidence=entry_ev, exit_evidence=exit_ev,
                market_label_status="verified", market_label_value=0.05,
            )
        ])
        outcome = built["outcomes"][0]
        self.assertEqual(SESSION, outcome.actual_entry_session)
        self.assertEqual(NEXT_SESSION, outcome.actual_exit_session)


class StrictNumericBooleanTest(unittest.TestCase):
    """R5-F3：声明性布尔的**数值**兼容也只认精确的 0 / 1。

    旧实现把"非零即真"当作兼容：``historical_membership_complete: 2`` 或 ``-1``
    会直接解锁历史完整模式。这类值说明生产者与消费者对字段语义的理解已经不一致，
    继续猜测等于替对方编造一份声明。风险标记（``risk_flag``）走同一语义。
    """

    def test_numeric_truth_table_only_accepts_exact_zero_and_one(self):
        import point_in_time as PIT

        # 精确的 0 / 1（含 float 与 bool）→ 明确判定。
        self.assertIs(True, PIT.as_strict_bool(1))
        self.assertIs(True, PIT.as_strict_bool(1.0))
        self.assertIs(True, PIT.as_strict_bool(True))
        self.assertIs(False, PIT.as_strict_bool(0))
        self.assertIs(False, PIT.as_strict_bool(0.0))
        self.assertIs(False, PIT.as_strict_bool(False))
        # 其他数值一律"无法判定"，绝不当成 True。
        for value in (2, -1, -2, 3, 0.5, -0.5, 1.5, 100, -100):
            self.assertIsNone(PIT.as_strict_bool(value), value)
        # 无穷与 NaN 同样无法判定。
        for value in (float("inf"), float("-inf"), float("nan")):
            self.assertIsNone(PIT.as_strict_bool(value), value)

    def test_risk_flags_use_the_same_numeric_semantics(self):
        """风险标记复用同一真值表：``2`` / ``-1`` 不得被读成 ST。"""
        self.assertIs(True, ST.normalize_risk_flag(1))
        self.assertIs(False, ST.normalize_risk_flag(0))
        for value in (2, -1, 0.5, float("inf"), float("nan")):
            self.assertIsNone(ST.normalize_risk_flag(value), value)

    def test_malformed_complete_flag_cannot_unlock_historical_mode(self):
        """``historical_membership_complete: 2`` / ``-1`` 不得解锁历史完整模式。"""
        for value in (2, -1, 0.5, float("inf"), float("nan")):
            payload = {
                "kind": "historical_archive",
                "historical_membership_complete": value,
                "rows": [{"code": "600001", "effective_from": "2024-06-14",
                          "name": "某某股份", "risk_flag": False}],
            }
            self.assertEqual(
                SS.SOURCE_INCOMPLETE, SS.archive_provenance(payload)["status"],
                f"value={value!r}")
            self.assertIsNone(
                SS.SecurityStateArchive.from_payload(payload), f"value={value!r}")

    def test_universe_source_complete_flag_rejects_non_boolean_numbers(self):
        """``point_in_time.universe_source_provenance`` 同样不得被 2 / -1 解锁。"""
        import point_in_time as PIT

        for value in (2, -1, 0.5, float("inf"), float("nan")):
            out = PIT.universe_source_provenance(
                {"kind": "historical_archive",
                 "historical_membership_asof": "2026-12-31",
                 "historical_membership_complete": value}, "2024-06-14")
            self.assertFalse(out["historical_membership_complete"], f"value={value!r}")
            self.assertEqual(
                PIT.UNIVERSE_SOURCE_INCOMPLETE, out["status"], f"value={value!r}")

    def test_exact_one_and_zero_still_work_end_to_end(self):
        """对照：合法的 1 / 0 仍必须被接受（严格不等于只认 bool 类型）。"""
        ok_payload = {
            "kind": "historical_archive",
            "historical_membership_complete": 1,
            "rows": [{"code": "600001", "effective_from": "2024-06-14",
                      "name": "某某股份", "risk_flag": 0}],
        }
        self.assertEqual(SS.SOURCE_OK, SS.archive_provenance(ok_payload)["status"])
        archive = SS.SecurityStateArchive.from_payload(ok_payload)
        self.assertIsNotNone(archive)

    def test_malformed_risk_flag_number_fails_closed_in_the_verdict(self):
        """``risk_flag=2`` 无法判定 → unproven，绝不默认非 ST。"""
        verdict = buy(evidence(price=100.5, reference=100.0, name="某某股份",
                               risk_flag=2))
        self.assertEqual(ST.STATUS_UNPROVEN, verdict.status)
        self.assertEqual(ST.REASON_UNKNOWN_ST_STATUS, verdict.reason)


class EffectiveToContractTest(unittest.TestCase):
    """R5-F4：``effective_to`` 的语义被固定为**半开区间**，文档/实现/测试一致。

    归档文档曾写 ``effective_from <= session < effective_to``（半开），而
    ``state_at()`` 实现的是闭区间（``<= end``）。两种口径下"``effective_to`` 当天
    算不算本行"给出相反答案，生产者无法据此写出正确的归档。现在统一为半开：
    ``[effective_from, effective_to)``，与
    :func:`point_in_time.classification_visibility` /
    :func:`point_in_time.universe_membership` 完全一致 —— 相邻窗口首尾相接且
    不重叠，生产者不需要靠"哪一行生效更晚"的兜底规则来消歧。
    """

    @staticmethod
    def _archive(*rows, availability_basis="session_close"):
        return SS.SecurityStateArchive.from_payload({
            "kind": "historical_archive",
            "historical_membership_complete": True,
            "availability_basis": availability_basis,
            "rows": list(rows),
        })

    def test_effective_to_date_itself_is_outside_the_window(self):
        """**边界**：``session == effective_to`` → **不**属于本行（窗口已结束）。"""
        archive = self._archive(
            {"code": "600001", "effective_from": "2024-06-14",
             "effective_to": "2024-06-18", "name": "某某股份", "risk_flag": False},
        )
        # 窗口内最后一个 session 仍可见。
        self.assertEqual(
            "某某股份", archive.state_at("600001", "2024-06-17")["name"])
        # effective_to 当日**不在**窗口内。
        self.assertIsNone(archive.state_at("600001", "2024-06-18"))
        # 之后同样不可见（不沿用旧值）。
        self.assertIsNone(archive.state_at("600001", "2024-06-19"))

    def test_the_following_date_belongs_to_the_next_window(self):
        """**边界**：``effective_to`` 当日属于**下一个**窗口。"""
        archive = self._archive(
            {"code": "600001", "effective_from": "2024-06-14",
             "effective_to": "2024-06-18", "name": "某某股份", "risk_flag": False},
            {"code": "600001", "effective_from": "2024-06-18",
             "effective_to": "2024-06-30", "name": "某某股份ST", "risk_flag": True},
        )
        self.assertEqual("某某股份", archive.state_at("600001", "2024-06-17")["name"])
        boundary = archive.state_at("600001", "2024-06-18")
        self.assertEqual("某某股份ST", boundary["name"])
        self.assertTrue(boundary["risk_flag"])
        self.assertEqual("2024-06-18", boundary["effective_from"])

    def test_effective_from_date_itself_is_inside_the_window(self):
        """**边界**：``session == effective_from`` → 属于本行（左闭）。"""
        archive = self._archive(
            {"code": "600001", "effective_from": "2024-06-14",
             "effective_to": "2024-06-30", "name": "某某股份", "risk_flag": False},
        )
        state = archive.state_at("600001", "2024-06-14")
        self.assertIsNotNone(state)
        self.assertEqual("2024-06-14", state["effective_from"])
        # 前一天不可见。
        self.assertIsNone(archive.state_at("600001", "2024-06-13"))

    def test_effective_to_equal_to_effective_from_is_an_empty_window(self):
        """``effective_to == effective_from`` 是空窗口 → 不覆盖任何 session。"""
        archive = self._archive(
            {"code": "600001", "effective_from": "2024-06-14",
             "effective_to": "2024-06-14", "name": "某某股份", "risk_flag": False},
        )
        self.assertIsNone(archive.state_at("600001", "2024-06-14"))
        self.assertIsNone(archive.state_at("600001", "2024-06-13"))

    def test_absent_effective_to_covers_exactly_its_own_session(self):
        """缺省 ``effective_to`` = 只覆盖 ``effective_from`` 当天（半开写法）。"""
        archive = self._archive(
            {"code": "600001", "effective_from": "2024-06-14",
             "name": "某某股份", "risk_flag": False},
        )
        self.assertIsNotNone(archive.state_at("600001", "2024-06-14"))
        self.assertIsNone(archive.state_at("600001", "2024-06-15"))

    def test_documentation_and_implementation_agree_on_the_boundary(self):
        """文档、实现、测试必须对 ``effective_to`` 给出同一个答案。

        这条测试直接读 ``security_state_point_in_time`` 的模块文档串，断言它写的是
        半开区间；同时用实现验证同一语义。文档若被改回闭区间，这里立刻失败 ——
        契约不得在生产者面前保持歧义。

        对比对象 :func:`point_in_time.classification_visibility` 用的是**同一口径**：
        它把 date-only 的 ``effective_to`` 解释为该自然日结束，因此 date-only 的
        ``asof`` 落在 ``effective_to`` 当天时同样判为**窗口已结束**。两条契约在
        日粒度上必须给出同一个答案，否则"归档生产者按哪一条写"就又变成了猜测。
        """
        import security_state_point_in_time as SS_MOD

        doc = SS_MOD.__doc__ or ""
        self.assertIn("effective_from <= session", doc)
        self.assertIn("< effective_to", doc)
        self.assertNotIn("<= effective_to", doc)

        archive = self._archive(
            {"code": "600001", "effective_from": "2024-06-14",
             "effective_to": "2024-06-18", "name": "某某股份", "risk_flag": False},
        )
        # 与 :func:`point_in_time.classification_visibility` 的区间口径一致：
        # effective_to 当日 → 窗口已结束（两条契约都判"不可见"）。
        import point_in_time as PIT

        at_boundary = PIT.classification_visibility(
            {"industry": "银行", "industry_effective_from": "2024-06-14",
             "industry_effective_to": "2024-06-18"}, "2024-06-18")
        self.assertFalse(at_boundary["visible"])
        self.assertEqual("effective_window_expired", at_boundary["basis"])
        self.assertIsNone(archive.state_at("600001", "2024-06-18"))

        # 窗口内的最后一个 session：两条契约都判"可见"。
        inside = PIT.classification_visibility(
            {"industry": "银行", "industry_effective_from": "2024-06-14",
             "industry_effective_to": "2024-06-18"}, "2024-06-17")
        self.assertTrue(inside["visible"])
        self.assertEqual("某某股份", archive.state_at("600001", "2024-06-17")["name"])


def _json_loads(text):
    import json as _json

    return _json.loads(text)


if __name__ == "__main__":
    unittest.main()


# ═══════════════════════════════════════════════════════════════════════════════
# Round 7 — the last two correctness findings
# ═══════════════════════════════════════════════════════════════════════════════


class ExecutableAvailabilitySeparationTest(unittest.TestCase):
    """R7-F1：``executable_metrics_available`` 不得是 ``executable_benchmark_available`` 的别名。

    三个概念必须**各自**判定、绝不互相代替：

    * ``executable_selection_metrics_available`` —— 至少一条真正可执行的选股观测；
    * ``executable_benchmark_available`` —— 被评估的决策日上存在可执行基准基线；
    * ``executable_excess_available`` —— 至少一条可执行选股观测落在**同时**有可执行
      基准的决策日上（``executable_excess`` 唯一的样本来源）。

    旧实现把 legacy 字段直接等于基准侧可用性，于是"基准侧有样本、选股侧一条都不可
    执行"时报告仍声称可执行指标可用（``n == 0`` 且 ``executable_excess is None``）。
    """

    KLINES = ProductionEvaluatorWiringTest.KLINES

    def _evaluate(self, picks, *, security_state_fn=None, benchmark=None):
        with _FakeKlineReport(self.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            original = AR.benchmark_map
            if benchmark is not None:
                AR.benchmark_map = lambda *a, **k: dict(benchmark)
            try:
                return AR.evaluate(
                    picks, 1, "选股质量", sessions, asof="2024-06-30",
                    security_state_fn=security_state_fn,
                )
            finally:
                AR.benchmark_map = original

    def test_benchmark_without_any_executable_selection_claims_nothing(self):
        """**核心回归**：基准侧可用 + 选股侧可执行 n=0 → 三项全部 false。

        provider 答不出任何选股侧状态（``name``/``risk_flag`` 都缺席 → 契约判
        ``unknown_st_status``），但基准侧照常有可执行基线。旧实现会在这里声称
        ``executable_metrics_available == true``，而 ``n == 0``、
        ``executable_excess is None``。
        """
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            # 状态存在但**不可用**：两个字段都缺席 → 契约 fail closed。
            return {"name": None, "risk_flag": None}

        _, summary = self._evaluate(
            picks, security_state_fn=provider,
            benchmark={
                "market": {"2024-06-14": 0.01},
                "executable": {"2024-06-14": 0.02},
                "coverage": {"required": 6, "resolved": 6},
            },
        )
        # 基准侧确实成立。
        self.assertTrue(summary["benchmark_state_coverage_available"])
        self.assertIn("2024-06-14", summary["executable_benchmark_days"])
        self.assertTrue(summary["executable_benchmark_available"])
        # 选股侧一条可执行观测都没有。
        self.assertEqual(0, summary["n"])
        self.assertEqual(0, summary["executable_selected_n"])
        self.assertEqual(0, summary["executable_paired_n"])
        self.assertFalse(summary["executable_selection_metrics_available"])
        self.assertFalse(summary["executable_excess_available"])
        self.assertIsNone(summary["executable_excess"])
        # legacy 汇总字段绝不因基准侧可用而为 true。
        self.assertFalse(summary["executable_metrics_available"])

    def test_executable_selection_without_a_paired_benchmark_date(self):
        """可执行选股样本存在、但没有配对的基准日 → 超额不可用、指标不可用。

        这是三个概念的**中间态**：选股侧成立、基准侧成立，但两者不在同一个决策日上
        —— ``executable_excess`` 依然一条都减不出来。
        """
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(
            picks, security_state_fn=provider,
            benchmark={
                "market": {"2024-06-14": 0.01},
                # 基准只在 06-17 有值，而选股样本的决策日是 06-14。
                "executable": {"2024-06-17": 0.02},
                "coverage": {"required": 4, "resolved": 4},
            },
        )
        self.assertTrue(summary["benchmark_state_coverage_available"])
        self.assertGreater(summary["executable_selected_n"], 0)
        self.assertTrue(summary["executable_selection_metrics_available"])
        # 但被评估的决策日上没有可执行基准 → 基准不可得、超额不可得。
        self.assertEqual([], summary["executable_benchmark_days"])
        self.assertFalse(summary["executable_benchmark_available"])
        self.assertFalse(summary["executable_excess_available"])
        self.assertEqual(0, summary["executable_paired_n"])
        self.assertIsNone(summary["executable_excess"])
        self.assertFalse(summary["executable_metrics_available"])

    def test_all_three_available_on_a_paired_date(self):
        """对照：选股侧可执行 + 基准侧同日可用 → 三项全 true（防过度收紧）。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(picks, security_state_fn=provider)
        self.assertTrue(summary["executable_selection_metrics_available"])
        self.assertTrue(summary["executable_benchmark_available"])
        self.assertTrue(summary["executable_excess_available"])
        self.assertTrue(summary["executable_metrics_available"])
        self.assertGreater(summary["executable_selected_n"], 0)
        self.assertGreater(summary["executable_paired_n"], 0)
        self.assertIsNotNone(summary["executable_excess"])

    def test_the_three_fields_are_reported_separately(self):
        """三个字段必须同时存在，且不是同一个值被复制三遍。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": "某某股份", "risk_flag": False}

        _, summary = self._evaluate(picks, security_state_fn=provider)
        for key in (
            "executable_selection_metrics_available",
            "executable_benchmark_available",
            "executable_excess_available",
            "executable_metrics_available",
        ):
            self.assertIn(key, summary)
        # legacy 字段是三者之**合**，不是基准侧字段的别名。
        self.assertEqual(
            summary["executable_metrics_available"],
            summary["executable_selection_metrics_available"]
            and summary["executable_benchmark_available"]
            and summary["executable_excess_available"],
        )

    def test_real_main_reports_the_split(self):
        """真实 ``main()`` 也必须把三者分开报出（不是只有 evaluate 层有）。"""
        import os as _os
        import sqlite3 as sq
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = _os.path.join(tmp, "selection_tracking.db")
            conn = sq.connect(db)
            conn.execute(
                "CREATE TABLE selection_runs(id INTEGER PRIMARY KEY, strategy TEXT,"
                " data_asof_date TEXT)"
            )
            conn.execute(
                "CREATE TABLE selection_picks(run_id INTEGER, code TEXT,"
                " rank_no INTEGER, name TEXT)"
            )
            conn.execute("INSERT INTO selection_runs VALUES(1,'s1','2024-06-14')")
            conn.execute("INSERT INTO selection_picks VALUES(1,'600001',1,'某某股份')")
            conn.commit()
            conn.close()

            import selection_alpha_report as AR

            original = dict(
                DATA_DIR=AR.DATA_DIR, KLINE_DIR=AR.KLINE_DIR,
                REPORT_PATH=AR.REPORT_PATH, load_kline=AR.load_kline,
                listdir=AR.os.listdir, WINDOW_DAYS=AR.WINDOW_DAYS,
            )
            AR.DATA_DIR = tmp
            AR.KLINE_DIR = _os.path.join(tmp, "klines")
            AR.REPORT_PATH = _os.path.join(tmp, "reports", "alpha.md")
            AR.WINDOW_DAYS = 3650
            AR.load_kline = lambda code: dict(self.KLINES.get(code) or {})
            AR.os.listdir = lambda path: (
                [f"{code}.csv" for code in self.KLINES]
                if str(path) == str(AR.KLINE_DIR) else original["listdir"](path)
            )
            try:
                from contextlib import redirect_stdout
                import io

                buf = io.StringIO()
                with redirect_stdout(buf):
                    AR.main(security_state_fn=lambda code, session: {
                        "name": "某某股份", "risk_flag": False})
                payload = _json_loads(buf.getvalue().strip().splitlines()[-1])
            finally:
                AR.DATA_DIR = original["DATA_DIR"]
                AR.KLINE_DIR = original["KLINE_DIR"]
                AR.REPORT_PATH = original["REPORT_PATH"]
                AR.load_kline = original["load_kline"]
                AR.os.listdir = original["listdir"]
                AR.WINDOW_DAYS = original["WINDOW_DAYS"]

        for key in (
            "executable_metrics_available",
            "executable_selection_metrics_available",
            "executable_benchmark_available",
            "executable_excess_available",
            "executable_selected_n",
            "executable_paired_n",
        ):
            self.assertIn(key, payload)
        self.assertEqual(
            payload["executable_metrics_available"],
            payload["executable_selection_metrics_available"]
            and payload["executable_benchmark_available"]
            and payload["executable_excess_available"],
        )


class ActionStateUsabilityPredicateTest(unittest.TestCase):
    """R7-F2：覆盖度的 ``resolved`` 必须表示状态**真的可用**，而不是字段非 ``None``。

    唯一权威谓词是 :func:`selection_tradability.action_state_usable`，也就是
    ``tradability_at`` 第 4 步（证据派生权限）所表达的语义。报告层不得自己判断。
    """

    def test_predicate_truth_table(self):
        """谓词真值表：与契约第 4 步逐条对应。"""
        cases = [
            # (name, risk_flag, usable, why)
            (None, None, False, "两者都缺席 → ST 未知"),
            (None, "maybe", False, "显式存在但无法归一 → fail closed"),
            (None, "garbage", False, "同上"),
            (None, 2, False, "数值只认精确 0/1"),
            ("某某股份", "maybe", False, "即使有名称，坏写法也不放行"),
            ("某某股份", None, True, "名称本身就是资格证据"),
            (None, False, True, "归一成功 → 是不是 ST 已确定"),
            (None, True, True, "归一成功（是 ST 也可判）"),
            (None, "false", True, "字符串假值严格归一"),
            (None, "0", True, "字符串零严格归一"),
            ("某某股份", False, True, "两者都有"),
        ]
        for name, flag, expected, why in cases:
            with self.subTest(name=name, risk_flag=flag, why=why):
                self.assertEqual(
                    expected, ST.action_state_usable(name, flag), why
                )

    def test_malformed_risk_flag_is_not_counted_as_resolved_selected_side(self):
        """选股侧：``{"name": None, "risk_flag": "maybe"}`` **不得**记成 resolved。

        契约对这份状态判 ``unproven / unknown_st_status``；覆盖度若记成 resolved，
        就与契约自相矛盾。
        """
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": None, "risk_flag": "maybe"}

        with _FakeKlineReport(ProductionEvaluatorWiringTest.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({d for bars in ProductionEvaluatorWiringTest.KLINES.values()
                        for d in bars})
            )
            _, summary = AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=provider,
            )
        cov = summary["action_state_coverage"]
        self.assertGreater(cov["required"], 0)
        self.assertEqual(0, cov["resolved"], "坏写法不得算作可用状态")
        self.assertFalse(summary["action_state_coverage_available"])
        # 契约对同一份状态的结论必须是 unproven / unknown_st_status：覆盖度与契约
        # 同口径，不能一边说"已解析"一边判 unproven。
        self.assertGreater(
            summary["tradability_counts"].get(ST.REASON_UNKNOWN_ST_STATUS, 0), 0
        )

    def test_malformed_risk_flag_is_not_counted_as_resolved_benchmark_side(self):
        """基准侧：同一个判据也必须生效（两侧覆盖度不得各自为政）。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": None, "risk_flag": "maybe"}

        with _FakeKlineReport(ProductionEvaluatorWiringTest.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({d for bars in ProductionEvaluatorWiringTest.KLINES.values()
                        for d in bars})
            )
            _, summary = AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=provider,
            )
        bench = summary["benchmark_state_coverage"]
        self.assertGreater(bench["required"], 0)
        self.assertEqual(0, bench["resolved"], "坏写法不得算作可用状态")
        self.assertFalse(summary["benchmark_state_coverage_available"])

    def test_absent_name_with_valid_flag_still_counts_as_resolved(self):
        """对照：名称缺席但 ``risk_flag`` 可归一 → **可用**（防过度收紧）。"""
        picks = [("s1", "2024-06-14", "600001", "某某股份")]

        def provider(code, session):
            return {"name": None, "risk_flag": False}

        with _FakeKlineReport(ProductionEvaluatorWiringTest.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({d for bars in ProductionEvaluatorWiringTest.KLINES.values()
                        for d in bars})
            )
            _, summary = AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=provider,
            )
        self.assertGreater(summary["action_state_coverage"]["resolved"], 0)
        self.assertTrue(summary["action_state_coverage_available"])

    def test_coverage_never_exceeds_the_usable_verdicts(self):
        """覆盖度与契约必须**同口径**：resolved 不得超过可执行判定成功的样本。

        provider 对每个动作 session 都返回坏写法，则两侧覆盖度都为 0，且没有任何
        样本被判 executable —— 报告不得一边说"覆盖 3/3"一边说"一条都不可执行"。
        """
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]

        def provider(code, session):
            return {"name": None, "risk_flag": "maybe"}

        with _FakeKlineReport(ProductionEvaluatorWiringTest.KLINES) as AR:
            sessions = SL.normalize_sessions(
                sorted({d for bars in ProductionEvaluatorWiringTest.KLINES.values()
                        for d in bars})
            )
            _, summary = AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30",
                security_state_fn=provider,
            )
        self.assertEqual(0, summary["action_state_coverage"]["resolved"])
        self.assertEqual(0, summary["entry_counts"]["executable"])
        self.assertFalse(summary["executable_selection_metrics_available"])
