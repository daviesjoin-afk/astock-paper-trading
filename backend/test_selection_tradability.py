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
        """禁止把涨跌停写成统一的 10%（或任何板块常量）。

        用 AST 只看**数值常量**，因此文档字符串里解释 9.5/19.5/29.5 不受影响。
        """
        forbidden = {
            0.1, 0.095, 0.195, 0.295,
            9.5, 19.5, 29.5,
            0.05, 5.0,  # ST 上限必须来自权威实现，不能是本地常量以外的猜值
        }
        # ST_LIMIT_PCT 是唯一允许的本地常量，且必须与权威口径一致。
        self.assertEqual(PTR.limit_pct(MAIN_BOARD, "*ST某某"), ST.ST_LIMIT_PCT)
        source = pathlib.Path(ST.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                if isinstance(node.value, bool):
                    continue
                if node.value in forbidden and node.value != ST.ST_LIMIT_PCT:
                    found.append((node.lineno, node.value))
        self.assertEqual([], found, f"local price-limit constants: {found}")


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
        import selection_alpha_report as AR

        original = AR.load_kline
        AR.load_kline = lambda code: dict(self.KLINES.get(code) or {})
        try:
            sessions = SL.normalize_sessions(
                sorted({day for bars in self.KLINES.values() for day in bars})
            )
            return AR.evaluate(
                picks, 1, "选股质量", sessions, asof="2024-06-30", **kwargs
            )
        finally:
            AR.load_kline = original

    def test_production_evaluator_excludes_unexecutable_picks_by_default(self):
        """生产默认口径 = executable only：买不进的样本不再进均值。"""
        picks = [
            ("s1", "2024-06-14", "600001", "某某股份"),
            ("s1", "2024-06-14", "600002", "某某股份"),
        ]
        lines, summary = self._evaluate(picks)
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
        _, summary = self._evaluate(picks, tradability_mode=ST.MODE_MARKET)
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


if __name__ == "__main__":
    unittest.main()
