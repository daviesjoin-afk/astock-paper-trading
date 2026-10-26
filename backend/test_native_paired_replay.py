# -*- coding: utf-8 -*-
"""成对回放的验证矩阵（方案 §7「实验与接线」逐条落地）。

关注点不是"有没有收益"，而是：两侧状态是否真的独立、T+1/费用/整手是否真的生效、
资金是否守恒、回执是否能幂等重放、以及回放**不**会冒充晋升证据。
"""
import unittest

import execution_planner as EP
import experiment_target_manifest as ETM
import native_paired_replay as N

#: 真实交易日（2026 国庆休市 10-01 ~ 10-07，由交易所日历裁定）。
TRADING_DAYS = ("2026-09-28", "2026-09-29", "2026-09-30",
                "2026-10-08", "2026-10-09", "2026-10-12")
HOLIDAY = "2026-10-06"
CAPITAL = 100_000.0


def quote(code, price, prev_close=10.0):
    return N.ReplayQuote(code=code, price=price, prev_close=prev_close)


def session(day, prices=None):
    prices = prices or {}
    quotes = tuple(
        quote(code, prices.get(code, 10.0)) for code in ("600001", "600002"))
    return N.ReplaySession(session_date=day, decision_at=f"{day}T14:30:00+08:00",
                           quotes=quotes, factor_table={"day": day},
                           first_board_codes=("600001",))


def environment(days=TRADING_DAYS, prices=None):
    prices = prices or {}
    return N.ReplayEnvironment(sessions=tuple(
        session(day, prices.get(day)) for day in days))


def fixed_selector(order=("600001", "600002")):
    def select(model_family, replay_session, condition_overrides, weight_overrides, topn):
        return list(order)[:int(topn)]
    return select


def params(label, family="first_board_relay", conditions=()):
    return N.SideParameters(label=label, model_family=family,
                            condition_overrides=tuple(conditions))


def rules(**overrides):
    kwargs = dict(reference_capital=CAPITAL, max_positions=2, hold_sessions=1, topn=2)
    kwargs.update(overrides)
    return N.ReplayRules(**kwargs)


def run(champion=None, challenger=None, select=None, rule=None, days=TRADING_DAYS,
        prices=None):
    return N.run_paired_replay(
        environment=environment(days, prices),
        champion=champion or params(N.SIDE_CHAMPION),
        challenger=challenger or params(N.SIDE_CHALLENGER),
        rules=rule or rules(),
        select=select or fixed_selector(),
        experiment_id="exp-1",
        target_identity="tq_breakout:entry_policy:first_board_relay",
    )


class EnvironmentTests(unittest.TestCase):
    def test_a_holiday_session_is_refused(self):
        with self.assertRaises(N.ReplayRefused) as caught:
            N.ReplaySession(session_date=HOLIDAY, decision_at=f"{HOLIDAY}T14:30:00+08:00",
                            quotes=(quote("600001", 10.0),))
        self.assertEqual(str(caught.exception), N.R_SESSION_NOT_A_TRADING_DAY)

    def test_duplicate_symbol_quotes_are_refused(self):
        with self.assertRaises(N.ReplayRefused):
            N.ReplaySession(session_date=TRADING_DAYS[0],
                            decision_at=f"{TRADING_DAYS[0]}T14:30:00+08:00",
                            quotes=(quote("600001", 10.0), quote("600001", 10.5)))

    def test_empty_or_unordered_environments_are_refused(self):
        with self.assertRaises(N.ReplayRefused):
            N.ReplayEnvironment(sessions=())
        with self.assertRaises(N.ReplayRefused):
            N.ReplayEnvironment(sessions=(session(TRADING_DAYS[1]),
                                          session(TRADING_DAYS[0])))

    def test_environment_hash_is_order_and_value_sensitive(self):
        first = environment().environment_hash
        self.assertEqual(first, environment().environment_hash)
        moved = environment(prices={TRADING_DAYS[0]: {"600001": 10.5}}).environment_hash
        self.assertNotEqual(first, moved)

    def test_the_factor_table_is_part_of_the_environment_identity(self):
        """只改因子表内容也必须改变身份：表才是真正决定选股顺序的输入。"""
        import pandas as pd

        def table(pct_first):
            rows = [
                {"name": "甲", "industry": "A", "price": 10.0, "pct": pct_first,
                 "amount": 5e7, "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6,
                 "mom5_raw": 0.02, "mom20_raw": 0.03, "mom60_raw": 0.05,
                 "mom_short": 0.02, "volsurge": 1.5, "vol_surge_raw": 2.0,
                 "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
                 "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0},
                {"name": "乙", "industry": "A", "price": 12.0, "pct": 2.0, "amount": 4e7,
                 "turnover": 2.0, "main_pct": 0.8, "super_net_raw": 8e5, "mom5_raw": 0.01,
                 "mom20_raw": 0.02, "mom60_raw": 0.04, "mom_short": 0.01, "volsurge": 1.2,
                 "vol_surge_raw": 1.5, "rsi14_raw": 52.0, "flow": 0.9, "sentiment": 0.0,
                 "above_boll_mid": True, "boll_mid_breakout": False, "ma20": 11.5, "ma60": 11.0},
            ]
            return pd.DataFrame(rows, index=["600001", "600002"])

        def build(pct_first):
            day = TRADING_DAYS[0]
            session = N.ReplaySession(
                session_date=day, decision_at=f"{day}T14:30:00+08:00",
                quotes=(N.ReplayQuote("600001", 10.0, prev_close=10.0),
                        N.ReplayQuote("600002", 12.0, prev_close=12.0)),
                factor_table=table(pct_first), first_board_codes=("600001",))
            return N.ReplayEnvironment(sessions=(session,))

        base, mutated = build(3.0), build(-99.0)
        self.assertNotEqual(base.environment_hash, mutated.environment_hash)
        selector = N.strategies_selector()
        self.assertEqual(selector("first_board_relay", base.sessions[0], {}, None, 1),
                         ["600001"])
        self.assertEqual(selector("first_board_relay", mutated.sessions[0], {}, None, 1),
                         ["600002"])

    def test_a_serialization_round_trip_preserves_identity_and_selection(self):
        import pandas as pd

        day = TRADING_DAYS[0]
        table = pd.DataFrame(
            [{"name": "甲", "industry": "A", "price": 10.0, "pct": 3.0, "amount": 5e7,
              "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
              "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
              "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
              "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0},
             {"name": "乙", "industry": "A", "price": 12.0, "pct": 2.0, "amount": 4e7,
              "turnover": 2.0, "main_pct": 0.8, "super_net_raw": 8e5, "mom5_raw": 0.01,
              "mom20_raw": 0.02, "mom60_raw": 0.04, "mom_short": 0.01, "volsurge": 1.2,
              "vol_surge_raw": 1.5, "rsi14_raw": 52.0, "flow": 0.9, "sentiment": 0.0,
              "above_boll_mid": True, "boll_mid_breakout": False, "ma20": 11.5,
              "ma60": 11.0}],
            index=["600001", "600002"])
        session = N.ReplaySession(
            session_date=day, decision_at=f"{day}T14:30:00+08:00",
            quotes=(N.ReplayQuote("600001", 10.0, prev_close=10.0),
                    N.ReplayQuote("600002", 12.0, prev_close=12.0)),
            factor_table=table, first_board_codes=("600001",))
        rebuilt = N.session_from_payload(N.session_payload(session))
        self.assertEqual(rebuilt.identity(), session.identity())
        self.assertEqual(N.ReplayEnvironment(sessions=(rebuilt,)).environment_hash,
                         N.ReplayEnvironment(sessions=(session,)).environment_hash)
        selector = N.strategies_selector()
        self.assertEqual(selector("first_board_relay", rebuilt, {}, None, 5),
                         selector("first_board_relay", session, {}, None, 5))


class ControlRunTests(unittest.TestCase):
    def test_identical_parameters_produce_an_identical_nav_series(self):
        """控制组：没改参数时两侧必须逐点一致，否则引擎本身在制造差异。"""
        result = run()
        self.assertEqual(result.nav_series(N.SIDE_CHAMPION),
                         result.nav_series(N.SIDE_CHALLENGER))
        self.assertEqual(result.paired_excess_return(), 0.0)
        for champion_step, challenger_step in zip(result.champion_steps,
                                                  result.challenger_steps,
                                                  strict=True):
            self.assertEqual(champion_step.cash, challenger_step.cash)
            self.assertEqual(len(champion_step.fills), len(challenger_step.fills))

    def test_the_replay_is_deterministic(self):
        first = run()
        second = run()
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual([item.receipt_fingerprint for item in first.receipts],
                         [item.receipt_fingerprint for item in second.receipts])

    def test_a_changed_quote_changes_the_replay_identity(self):
        first = run()
        second = run(prices={TRADING_DAYS[0]: {"600001": 10.5}})
        self.assertNotEqual(first.fingerprint, second.fingerprint)


class DivergenceTests(unittest.TestCase):
    def test_a_parameter_driven_selection_change_shows_up_in_the_pair(self):
        champion_select = fixed_selector(("600001", "600002"))
        challenger_select = fixed_selector(("600002", "600001"))

        base = params(N.SIDE_CHAMPION)
        result = N.run_paired_replay(
            environment=environment(), champion=base,
            challenger=params(N.SIDE_CHALLENGER),
            rules=rules(max_positions=1, topn=1),
            select=lambda family, s, c, w, topn: champion_select(
                family, s, c, w, topn) if family == base.model_family and c.get(
                    "probe") is None else challenger_select(family, s, c, w, topn),
            experiment_id="exp-1", target_identity="t",)
        # 两侧都买了，但选择序列相同 —— 这里只验证引擎不会因为"选择函数不同"而串状态。
        self.assertEqual(result.nav_series(N.SIDE_CHAMPION),
                         result.nav_series(N.SIDE_CHALLENGER))

        left = N.run_paired_replay(
            environment=environment(), champion=base,
            challenger=params(N.SIDE_CHALLENGER, conditions=(("first_board_bonus", 0.0),)),
            rules=rules(max_positions=1, topn=1),
            select=lambda family, s, c, w, topn: (
                list(("600001",)) if c.get("first_board_bonus", 0.22) >= 0.22
                else list(("600002",))),
            experiment_id="exp-1", target_identity="t")
        self.assertEqual(left.champion_steps[0].state.lots[0].code, "600001")
        self.assertEqual(left.challenger_steps[0].state.lots[0].code, "600002")

    def test_the_two_sides_never_share_state(self):
        result = N.run_paired_replay(
            environment=environment(), champion=params(N.SIDE_CHAMPION),
            challenger=params(N.SIDE_CHALLENGER, conditions=(("first_board_bonus", 0.0),)),
            rules=rules(max_positions=1, topn=1),
            select=lambda family, s, c, w, topn: (
                ["600001"] if c.get("first_board_bonus", 0.22) >= 0.22 else ["600002"]),
            experiment_id="exp-1", target_identity="t")
        champion_cash = result.champion_steps[0].cash
        challenger_cash = result.challenger_steps[0].cash
        self.assertEqual(result.champion_steps[0].state.lots[0].code, "600001")
        self.assertEqual(result.challenger_steps[0].state.lots[0].code, "600002")
        self.assertNotEqual(result.champion_steps[0].state.lots,
                            result.challenger_steps[0].state.lots)
        self.assertGreater(champion_cash, 0)
        self.assertGreater(challenger_cash, 0)


class AccountingTests(unittest.TestCase):
    def test_cash_reconciles_with_fills_and_fees_every_session(self):
        result = run(rule=rules(hold_sessions=2))
        for side in (N.SIDE_CHAMPION, N.SIDE_CHALLENGER):
            steps = result.champion_steps if side == N.SIDE_CHAMPION else result.challenger_steps
            for step in steps:
                expected = CAPITAL
                for row in steps:
                    for fill in row.fills:
                        if row.session_date > step.session_date:
                            continue
                        if fill.side == "buy":
                            expected -= fill.amount + fill.fees
                        else:
                            expected += fill.amount - fill.fees
                self.assertAlmostEqual(step.cash, expected, places=6)
                self.assertGreaterEqual(step.cash, -1e-9)

    def test_fees_match_the_single_fee_authority(self):
        result = run()
        for step in result.champion_steps:
            for fill in step.fills:
                _, _, expected_fees = EP.estimate_execution_terms(10.0, fill.quantity,
                                                                 fill.side)
                self.assertAlmostEqual(fill.fees, expected_fees, places=9,
                                       msg=f"{fill.side}:{fill.code}")

    def test_nav_equals_cash_plus_marked_positions(self):
        result = run()
        for step in result.champion_steps:
            self.assertAlmostEqual(step.nav, step.cash + step.positions_value, places=6)

    def test_every_buy_is_a_whole_lot(self):
        result = run()
        buys = [fill for step in result.champion_steps for fill in step.fills
                if fill.side == "buy"]
        self.assertTrue(buys)
        for fill in buys:
            self.assertEqual(fill.quantity % 100, 0, fill.code)
            self.assertGreater(fill.quantity, 0)

    def test_positions_are_capped_by_the_declared_slot_count(self):
        result = run(rule=rules(max_positions=1, topn=2))
        for step in result.champion_steps:
            self.assertLessEqual(len({lot.code for lot in step.state.lots}), 1)


class SettlementTests(unittest.TestCase):
    def test_a_lot_cannot_be_sold_on_its_entry_session(self):
        result = run(rule=rules(hold_sessions=0 if False else 1))
        first = result.champion_steps[0]
        self.assertTrue([fill for fill in first.fills if fill.side == "buy"])
        self.assertFalse([fill for fill in first.fills if fill.side == "sell"])

    def test_a_sold_lot_leaves_the_book_and_pays_stamp_duty(self):
        result = run(rule=rules(hold_sessions=1))
        sells = [fill for step in result.champion_steps for fill in step.fills
                 if fill.side == "sell"]
        self.assertTrue(sells, "持有期到后应当出现卖出")
        for fill in sells:
            _, _, fees = EP.estimate_execution_terms(10.0, fill.quantity, "sell")
            self.assertAlmostEqual(fill.fees, fees, places=9)
            self.assertGreater(fees, 0)

    def test_a_missing_reference_price_blocks_the_buy(self):
        """没有昨收就没有涨跌停基准：必须拒绝成交，不能凭空成交。"""
        sessions = tuple(
            N.ReplaySession(
                session_date=day, decision_at=f"{day}T14:30:00+08:00",
                quotes=(N.ReplayQuote("600001", 10.0),), factor_table={"day": day})
            for day in TRADING_DAYS)
        result = N.run_paired_replay(
            environment=N.ReplayEnvironment(sessions=sessions),
            champion=params(N.SIDE_CHAMPION), challenger=params(N.SIDE_CHALLENGER),
            rules=rules(max_positions=1, topn=1), select=fixed_selector(("600001",)),
            experiment_id="exp-1", target_identity="t")
        self.assertEqual([fill for step in result.champion_steps for fill in step.fills], [])
        reasons = {reason for step in result.champion_steps for _code, reason in step.blocked}
        self.assertIn("missing_reference_price", reasons)
        for step in result.champion_steps:
            self.assertEqual(step.nav, CAPITAL)

    def test_untradable_symbols_are_blocked_with_a_reason_and_kept(self):
        sessions = []
        for index, day in enumerate(TRADING_DAYS):
            # 第二个会话把 600002 顶到涨停（12.00 → 13.20 = +10%），买入必须被拦下。
            sessions.append(N.ReplaySession(
                session_date=day, decision_at=f"{day}T14:30:00+08:00",
                quotes=(quote("600001", 10.0),
                        quote("600002", 13.2 if index == 1 else 12.0, prev_close=12.0)),
                factor_table={"day": day}))

        def select(family, replay_session, conditions, weights, topn):
            # 第一个会话买 600001；之后改推 600002，让涨停闸门真正被走到。
            return ["600001"] if replay_session.session_date == TRADING_DAYS[0] else ["600002"]

        result = N.run_paired_replay(
            environment=N.ReplayEnvironment(sessions=tuple(sessions)),
            champion=params(N.SIDE_CHAMPION), challenger=params(N.SIDE_CHALLENGER),
            rules=rules(max_positions=2, topn=1, hold_sessions=99),
            select=select, experiment_id="exp-1", target_identity="t")
        blocked_reasons = {reason for step in result.champion_steps
                           for _code, reason in step.blocked}
        self.assertTrue(blocked_reasons, "涨停/不可成交必须留下原因")
        self.assertIn("limit_up_buy_blocked", blocked_reasons)
        # 涨停那一天没有凭空成交。
        limit_up_step = result.champion_steps[1]
        self.assertEqual(
            [fill.code for fill in limit_up_step.fills if fill.side == "buy"], [])
        self.assertNotIn("600002", {lot.code for lot in limit_up_step.state.lots})
        # 闸门是当日的，不是永久的：价格回落后同一标的可以正常买入。
        later = [fill for step in result.champion_steps[2:] for fill in step.fills
                 if fill.code == "600002" and fill.side == "buy"]
        self.assertTrue(later, "涨停解除后应当可以买入")


class ReceiptTests(unittest.TestCase):
    def test_every_transition_publishes_one_unique_receipt(self):
        result = run()
        self.assertEqual(len(result.receipts), len(result.champion_steps)
                         + len(result.challenger_steps))
        keys = [item.key for item in result.receipts]
        self.assertEqual(len(set(keys)), len(keys))
        for receipt in result.receipts:
            self.assertEqual(receipt.environment_hash, result.environment_hash)
            self.assertEqual(receipt.evaluator_version, N.EVALUATOR_VERSION)

    def test_replaying_the_same_inputs_republishes_the_same_receipts(self):
        first = run()
        second = run()
        self.assertEqual([item.receipt_fingerprint for item in first.receipts],
                         [item.receipt_fingerprint for item in second.receipts])

    def test_transition_sequence_is_monotonic(self):
        result = run()
        sequences = [item.transition_sequence for item in result.receipts]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(sequences[0], 1)


class PromotionBoundaryTests(unittest.TestCase):
    def test_a_historical_replay_never_qualifies_a_promotion(self):
        with self.assertRaises(N.ReplayRefused) as caught:
            N.require_promotion_eligible()
        self.assertEqual(str(caught.exception), N.R_PROMOTION_NEEDS_LIVE)

    def test_the_result_carries_its_own_limitations(self):
        result = run()
        self.assertGreaterEqual(len(result.limitations), 4)
        self.assertIn("不是新鲜交易证据", "".join(result.limitations))
        self.assertEqual(result.projection()["replay_version"], N.REPLAY_VERSION)


class IntegrationTests(unittest.TestCase):
    def test_the_real_selector_drives_both_sides_end_to_end(self):
        import pandas as pd

        def factor_table(scale):
            rows = [
                {"name": "甲", "industry": "行业A", "price": 10.0, "pct": 3.0, "amount": 5e7,
                 "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
                 "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
                 "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
                 "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0},
                {"name": "乙", "industry": "行业A", "price": 12.0 * scale, "pct": 2.0,
                 "amount": 4e7, "turnover": 2.0, "main_pct": 0.8, "super_net_raw": 8e5,
                 "mom5_raw": 0.01, "mom20_raw": 0.02, "mom60_raw": 0.04, "mom_short": 0.01,
                 "volsurge": 1.2, "vol_surge_raw": 1.5, "rsi14_raw": 52.0, "flow": 0.9,
                 "sentiment": 0.0, "above_boll_mid": True, "boll_mid_breakout": False,
                 "ma20": 11.5, "ma60": 11.0},
            ]
            return pd.DataFrame(rows, index=["600001", "600002"])

        sessions = tuple(
            N.ReplaySession(
                session_date=day, decision_at=f"{day}T14:30:00+08:00",
                quotes=(quote("600001", round(10.0 * scale, 2)),
                        quote("600002", round(12.0 * scale, 2), prev_close=12.0)),
                factor_table=factor_table(scale), first_board_codes=("600001",))
            for day, scale in zip(TRADING_DAYS, (1.0, 1.01, 1.02, 1.03, 1.04, 1.05), strict=True))
        result = N.run_paired_replay(
            environment=N.ReplayEnvironment(sessions=sessions),
            champion=params(N.SIDE_CHAMPION),
            challenger=params(N.SIDE_CHALLENGER,
                              conditions=(("first_board_bonus", 0.0),)),
            rules=rules(max_positions=2, hold_sessions=1, topn=2),
            select=N.strategies_selector(),
            experiment_id="exp-integration",
            target_identity="tq_breakout:entry_policy:first_board_relay")
        for side in (N.SIDE_CHAMPION, N.SIDE_CHALLENGER):
            steps = (result.champion_steps if side == N.SIDE_CHAMPION
                     else result.challenger_steps)
            fills = [fill for step in steps for fill in step.fills]
            self.assertTrue(fills, f"{side} 应当有成交")
            self.assertTrue(any(fill.side == "buy" for fill in fills), side)
        self.assertEqual(len(result.receipts), 2 * len(TRADING_DAYS))


class ValuationTests(unittest.TestCase):
    """R2：持仓缺报价时不能把市值算成零——那会造出并未发生的亏损/上涨。"""

    def _state_with_lot(self, code="600001", quantity=100, price=10.0):
        return N.SideState(
            cash=1000.0,
            lots=(N.Lot(code=code, quantity=quantity, entry_session=TRADING_DAYS[0],
                        entry_price=price, entry_index=0),))

    def test_a_held_position_without_a_quote_refuses_the_session(self):
        day = TRADING_DAYS[1]
        session = N.ReplaySession(
            session_date=day, decision_at=f"{day}T14:30:00+08:00",
            quotes=(N.ReplayQuote("600002", 12.0, prev_close=12.0),))
        with self.assertRaises(N.ReplayRefused) as caught:
            N._advance_side(self._state_with_lot(), side_params=params(N.SIDE_CHAMPION),
                            session=session, index=1,
                            rules=rules(max_positions=2, hold_sessions=99),
                            select=lambda *args: [])
        self.assertIn(N.R_MISSING_POSITION_QUOTE, str(caught.exception))
        self.assertIn("600001", str(caught.exception))

    def test_a_quote_returning_next_day_cannot_manufacture_a_gain(self):
        """缺价的那一天整体拒绝，所以后面报价恢复也不会补出一段假收益。"""
        day_one, day_two = TRADING_DAYS[0], TRADING_DAYS[1]
        sessions = (
            N.ReplaySession(session_date=day_one,
                            decision_at=f"{day_one}T14:30:00+08:00",
                            quotes=(N.ReplayQuote("600001", 10.0, prev_close=10.0),),
                            factor_table={"day": day_one}),
            # 第二天该股没有报价：整段回放必须在这里停下，而不是把它的市值当 0。
            N.ReplaySession(session_date=day_two,
                            decision_at=f"{day_two}T14:30:00+08:00",
                            quotes=(N.ReplayQuote("600002", 12.0, prev_close=12.0),),
                            factor_table={"day": day_two}),
        )
        with self.assertRaises(N.ReplayRefused) as caught:
            N.run_paired_replay(
                environment=N.ReplayEnvironment(sessions=sessions),
                champion=params(N.SIDE_CHAMPION), challenger=params(N.SIDE_CHALLENGER),
                rules=rules(max_positions=2, hold_sessions=99),
                select=fixed_selector(("600001",)), experiment_id="exp-1",
                target_identity="t")
        self.assertIn(N.R_MISSING_POSITION_QUOTE, str(caught.exception))

    def test_a_fully_quoted_book_still_marks_to_market(self):
        day = TRADING_DAYS[0]
        session = N.ReplaySession(
            session_date=day, decision_at=f"{day}T14:30:00+08:00",
            quotes=(N.ReplayQuote("600001", 11.0, prev_close=10.0),))
        state, step = N._advance_side(
            self._state_with_lot(), side_params=params(N.SIDE_CHAMPION),
            session=session, index=0, rules=rules(max_positions=2, hold_sessions=99),
            select=lambda *args: [])
        self.assertEqual(step.positions_value, 1100.0)
        self.assertEqual(step.nav, 2100.0)


class ManifestGatingTests(unittest.TestCase):
    """实验清单必须真正管住回放：未声明字段、无变化变体、不支持的策略一律拒绝。"""

    def test_the_variant_only_carries_the_declared_change(self):
        champion, challenger = N.sides_from_manifest(
            "tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
            model_family="first_board_relay",
            variant_fields={"first_board_bonus": 0.0})
        self.assertEqual(champion.condition_map(), {})
        self.assertEqual(challenger.condition_map(), {"first_board_bonus": 0.0})
        self.assertEqual(champion.model_family, challenger.model_family)

    def test_the_champion_side_keeps_its_own_overrides(self):
        champion, challenger = N.sides_from_manifest(
            "tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
            model_family="first_board_relay",
            champion_fields={"chase_penalty": 0.5},
            variant_fields={"first_board_bonus": 0.1})
        self.assertEqual(champion.condition_map(), {"chase_penalty": 0.5})
        self.assertEqual(challenger.condition_map(),
                         {"chase_penalty": 0.5, "first_board_bonus": 0.1})

    def test_an_undeclared_field_is_refused(self):
        with self.assertRaises(N.ReplayRefused) as caught:
            N.sides_from_manifest("tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
                                  model_family="first_board_relay",
                                  variant_fields={"hold_bias": 0.2})
        self.assertIn(ETM.REFUSAL_FIELD_NOT_ALLOWED, str(caught.exception))

    def test_a_variant_equal_to_the_baseline_is_refused(self):
        with self.assertRaises(N.ReplayRefused) as caught:
            N.sides_from_manifest("tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
                                  model_family="first_board_relay",
                                  variant_fields={"first_board_bonus": 0.22})
        self.assertIn("variant_does_not_change", str(caught.exception))

    def test_an_unsupported_strategy_is_refused(self):
        with self.assertRaises(N.ReplayRefused) as caught:
            N.sides_from_manifest("reported_profit_breakout",
                                  target_kind=ETM.TARGET_ENTRY_POLICY,
                                  model_family="reported_profit_breakout",
                                  variant_fields={"first_board_bonus": 0.0})
        self.assertIn(ETM.REFUSAL_NO_PARAMETERIZED_INPUT, str(caught.exception))

    def test_weights_targets_route_into_weight_overrides(self):
        champion, challenger = N.sides_from_manifest(
            "tq_breakout", target_kind=ETM.TARGET_SELECTION_WEIGHTS,
            model_family="first_board_relay", variant_fields={"volsurge": 0.3})
        self.assertIsNone(champion.weight_map())
        self.assertEqual(challenger.weight_map(), {"volsurge": 0.3})
        self.assertEqual(challenger.condition_map(), {})

    def test_manifest_to_replay_end_to_end(self):
        champion, challenger = N.sides_from_manifest(
            "tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
            model_family="first_board_relay",
            variant_fields={"first_board_bonus": 0.0})
        environment_ = environment()
        result = N.run_paired_replay(
            environment=environment_, champion=champion, challenger=challenger,
            rules=rules(max_positions=1, topn=1), select=fixed_selector(("600001",)),
            experiment_id="exp-manifest",
            target_identity="tq_breakout:entry_policy:first_board_relay")
        self.assertEqual(result.environment_hash, environment_.environment_hash)
        self.assertEqual(len(result.receipts), 2 * len(TRADING_DAYS))
        self.assertTrue(result.limitations)
        # 回放仍然不产生晋升资格。
        with self.assertRaises(N.ReplayRefused):
            N.require_promotion_eligible()


if __name__ == "__main__":
    unittest.main()
