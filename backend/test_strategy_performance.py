# -*- coding: utf-8 -*-
"""绩效事实 owner 的验证矩阵（方案 §7「绩效读取」逐条落地）。

这个文件的重点是**不许造假**：缺证据必须返回 None 加稳定 reason，不能出现
0、False、"数据库里刚好几行"冒充覆盖率，也不能把现金流当利润。
"""
import json
import os
import sqlite3
import tempfile
import unittest

import paper_cycle_service as PCS
import strategy_performance as SP

CHECKSUM = "a" * 64
OTHER_CHECKSUM = "b" * 64
STRATEGY = "trend_pullback"
WINDOW = ("2026-10-01", "2026-10-31")


def request(**overrides):
    kwargs = dict(strategy_id=STRATEGY, strategy_version=3, strategy_checksum=CHECKSUM,
                  observation_start=WINDOW[0], observation_end=WINDOW[1])
    kwargs.update(overrides)
    return SP.PerformanceRequest(**kwargs)


def nav(day, value, status="verified"):
    return SP.NavPoint(nav_date=day, nav=value, quote_status=status)


def fill(day, side, amount, fees=0.0, code="600000"):
    return SP.FillRecord(fill_date=day, side=side, amount=amount, fees=fees, code=code)


def slice_(**overrides):
    kwargs = dict(cycle_id=1, cycle_key="c1", cycle_status="archived",
                  source=SP.SOURCE_ARCHIVE, started_at="2026-10-01 09:31:00",
                  ended_at="2026-10-20 16:45:00", capital=100000.0,
                  nav_points=(nav("2026-10-02", 101000.0), nav("2026-10-19", 103000.0)))
    kwargs.update(overrides)
    return SP.CycleSlice(**kwargs)


class PureRequestTests(unittest.TestCase):
    def test_checksum_and_window_are_validated(self):
        with self.assertRaises(SP.PerformanceEvidenceError):
            request(strategy_checksum="not-a-checksum")
        with self.assertRaises(SP.PerformanceEvidenceError):
            request(observation_start="2026-10-31", observation_end="2026-10-01")
        with self.assertRaises(SP.PerformanceEvidenceError):
            request(requested_metrics=("made_up_metric",))

    def test_unavailable_metric_cannot_smuggle_a_value(self):
        with self.assertRaises(SP.PerformanceEvidenceError):
            SP.MetricFact(name=SP.METRIC_FEES, status=SP.STATUS_UNAVAILABLE, value=0.0)
        with self.assertRaises(SP.PerformanceEvidenceError):
            SP.MetricFact(name=SP.METRIC_FEES, status=SP.STATUS_AVAILABLE, value=None)

    def test_archive_format_constant_matches_the_archive_owner(self):
        self.assertEqual(SP._ARCHIVE_FORMAT, PCS.ARCHIVE_FORMAT)


class NoPinTests(unittest.TestCase):
    def test_no_exact_pin_never_falls_back_to_a_head_or_latest(self):
        fact = SP.build_performance_fact(request(), slices=[])
        self.assertIn(SP.R_NO_EXACT_PIN, fact.blocking_reasons)
        for name, metric in fact.metrics.items():
            self.assertEqual(metric.status, SP.STATUS_UNAVAILABLE, name)
            self.assertIsNone(metric.value, name)
        self.assertEqual(fact.coverage.cycles_included, 0)
        self.assertEqual(fact.status, SP.STATUS_UNAVAILABLE)


class CashFlowVersusProfitTests(unittest.TestCase):
    def test_open_buy_is_not_a_realized_loss(self):
        only_buys = slice_(fills=(fill("2026-10-05", "buy", 50000.0, 15.0),))
        fact = SP.build_performance_fact(request(), slices=[only_buys])
        realized = fact.metric(SP.METRIC_REALIZED_PNL)
        self.assertEqual(realized.value, 0.0)
        self.assertEqual(realized.status, SP.STATUS_AVAILABLE)
        cash = fact.metric(SP.METRIC_CASH_FLOW)
        self.assertEqual(cash.value["buy_outflow"], 50015.0)
        self.assertEqual(cash.value["sell_inflow"], 0.0)
        self.assertEqual(cash.value["net_external_flow"], 0.0)

    def test_sell_without_cost_evidence_is_unavailable_not_zero(self):
        sells_only = slice_(fills=(fill("2026-10-06", "sell", 48000.0, 12.0),))
        fact = SP.build_performance_fact(request(), slices=[sells_only])
        realized = fact.metric(SP.METRIC_REALIZED_PNL)
        self.assertEqual(realized.status, SP.STATUS_UNAVAILABLE)
        self.assertIsNone(realized.value)
        self.assertIn(SP.R_INCOMPLETE_COST_BASIS, realized.reasons)

    def test_owner_issued_realized_pnl_is_reused_when_available(self):
        usage = slice_(source=SP.SOURCE_ACTIVE, owner_realized_pnl=(1234.5, SP.STATUS_AVAILABLE),
                       fills=())
        fact = SP.build_performance_fact(request(), slices=[usage])
        self.assertEqual(fact.metric(SP.METRIC_REALIZED_PNL).value, 1234.5)
        self.assertEqual(fact.metric(SP.METRIC_REALIZED_PNL).status, SP.STATUS_AVAILABLE)

    def test_archived_sell_pnl_is_used_only_when_verifiably_present(self):
        archived = slice_(archived_sell_pnl=(("2026-10-05", 500.0),
                                             ("2026-10-19", -200.0)))
        fact = SP.build_performance_fact(request(), slices=[archived])
        self.assertEqual(fact.metric(SP.METRIC_REALIZED_PNL).value, 300.0)

    def test_a_sell_outside_the_window_never_enters_the_windowed_pnl(self):
        """R1 复现：窗口 [10-15, 10-21) 只应计入 10-19 的 100 元，不是 600 元。"""
        archived = slice_(archived_sell_pnl=(("2026-10-05", 500.0),
                                             ("2026-10-19", 100.0)))
        fact = SP.build_performance_fact(
            request(observation_start="2026-10-15", observation_end="2026-10-21"),
            slices=[archived])
        self.assertEqual(fact.metric(SP.METRIC_REALIZED_PNL).value, 100.0)

    def test_a_window_after_the_last_sale_reports_zero_not_the_cycle_total(self):
        archived = slice_(archived_sell_pnl=(("2026-10-05", 500.0),))
        fact = SP.build_performance_fact(
            request(observation_start="2026-10-20", observation_end="2026-10-25"),
            slices=[archived])
        self.assertEqual(fact.metric(SP.METRIC_REALIZED_PNL).value, 0.0)

    def test_a_window_before_any_sale_reports_zero(self):
        archived = slice_(archived_sell_pnl=(("2026-10-19", 100.0),))
        fact = SP.build_performance_fact(
            request(observation_start="2026-10-01", observation_end="2026-10-10"),
            slices=[archived])
        self.assertEqual(fact.metric(SP.METRIC_REALIZED_PNL).value, 0.0)

    def test_an_undated_pnl_fact_is_refused_at_construction(self):
        """盈亏事实必须带成交日期：不能靠订单日期猜，也不能无日期累加。"""
        with self.assertRaises(SP.PerformanceEvidenceError):
            slice_(archived_sell_pnl=(500.0,))


class OwnerWindowTests(unittest.TestCase):
    """活动周期路径：owner 给的是累计值，窗口内必须做减法。"""

    def _cycle(self, **overrides):
        cycle = {"id": 1, "cycle_key": "c1", "status": "active",
                 "capital": 100000.0, "started_at": "2026-10-01 09:31:00",
                 "ended_at": None}
        cycle.update(overrides)
        return cycle

    def test_the_window_value_is_the_difference_of_two_cumulative_reads(self):
        import paper_portfolio_read_model as PRM
        from unittest import mock

        cumulative = {"2026-10-20": 600.0, "2026-10-14": 500.0}

        def fake(conn, context, account_id=None):
            return cumulative.get(context.asof_day.isoformat()), "verified"

        with mock.patch.object(PRM, "realized_pnl", side_effect=fake):
            value, status = SP._owner_realized_pnl(
                None, 1, STRATEGY, self._cycle(),
                request(observation_start="2026-10-15", observation_end="2026-10-21"))
        self.assertEqual(value, 100.0)
        self.assertEqual(status, SP.STATUS_AVAILABLE)

    def test_a_cycle_starting_inside_the_window_needs_no_subtraction(self):
        import paper_portfolio_read_model as PRM
        from unittest import mock

        with mock.patch.object(PRM, "realized_pnl",
                               side_effect=lambda conn, context, account_id=None: (
                                   250.0, "verified")):
            value, status = SP._owner_realized_pnl(
                None, 1, STRATEGY, self._cycle(started_at="2026-10-17 09:31:00"),
                request(observation_start="2026-10-15", observation_end="2026-10-21"))
        self.assertEqual(value, 250.0)
        self.assertEqual(status, SP.STATUS_AVAILABLE)

    def test_an_unverifiable_read_on_either_side_is_unavailable(self):
        import paper_portfolio_read_model as PRM
        from unittest import mock

        def fake(conn, context, account_id=None):
            if context.asof_day.isoformat() == "2026-10-14":
                return None, "unknown"
            return 600.0, "verified"

        with mock.patch.object(PRM, "realized_pnl", side_effect=fake):
            value, status = SP._owner_realized_pnl(
                None, 1, STRATEGY, self._cycle(),
                request(observation_start="2026-10-15", observation_end="2026-10-21"))
        self.assertIsNone(value)
        self.assertEqual(status, SP.STATUS_UNAVAILABLE)


class NavSeriesTests(unittest.TestCase):
    def test_two_flat_valuations_give_zero_return_and_zero_drawdown(self):
        flat = slice_(nav_points=(nav("2026-10-02", 100000.0), nav("2026-10-09", 100000.0)))
        fact = SP.build_performance_fact(request(), slices=[flat])
        metrics = fact.metric(SP.METRIC_CYCLE_RETURN)
        self.assertEqual(metrics.value["per_cycle"]["c1"], 0.0)
        self.assertEqual(fact.metric(SP.METRIC_MAX_DRAWDOWN).value["per_cycle"]["c1"], 0.0)

    def test_a_single_valuation_is_not_enough_for_drawdown(self):
        single = slice_(nav_points=(nav("2026-10-09", 98000.0),))
        fact = SP.build_performance_fact(request(), slices=[single])
        self.assertEqual(fact.metric(SP.METRIC_CYCLE_RETURN).value["per_cycle"]["c1"],
                         round(98000.0 / 100000.0 - 1.0, 8))
        # 期初资本是第一个估值点，所以回撤算得出来；但一个 NAV 观测看不到区间路径。
        self.assertAlmostEqual(fact.metric(SP.METRIC_MAX_DRAWDOWN).value["per_cycle"]["c1"],
                               0.02, places=8)
        self.assertEqual(fact.metric(SP.METRIC_MAX_DRAWDOWN).status, SP.STATUS_PARTIAL)
        self.assertIn(SP.R_PARTIAL_NAV_COVERAGE, fact.metric(SP.METRIC_MAX_DRAWDOWN).reasons)

    def test_drawdown_uses_the_opening_capital_as_the_first_point(self):
        dip = slice_(nav_points=(nav("2026-10-02", 90000.0), nav("2026-10-09", 95000.0)))
        fact = SP.build_performance_fact(request(), slices=[dip])
        self.assertAlmostEqual(fact.metric(SP.METRIC_MAX_DRAWDOWN).value["per_cycle"]["c1"],
                               0.1, places=8)

    def test_missing_capital_blocks_the_return_instead_of_guessing(self):
        no_capital = slice_(capital=None)
        fact = SP.build_performance_fact(request(), slices=[no_capital])
        metric = fact.metric(SP.METRIC_CYCLE_RETURN)
        self.assertEqual(metric.status, SP.STATUS_UNAVAILABLE)
        self.assertIn(SP.R_MISSING_OPENING_VALUATION, fact.blocking_reasons)

    def test_no_valuation_inside_the_window_blocks_the_return(self):
        closed_early = slice_(nav_points=())
        fact = SP.build_performance_fact(request(), slices=[closed_early])
        self.assertIn(SP.R_MISSING_CLOSING_VALUATION, fact.blocking_reasons)

    def test_degraded_quote_keeps_the_number_but_lowers_the_status(self):
        degraded = slice_(nav_points=(nav("2026-10-02", 101000.0),
                                      nav("2026-10-09", 103000.0, status="cost_fallback")))
        fact = SP.build_performance_fact(request(), slices=[degraded])
        self.assertEqual(fact.metric(SP.METRIC_CYCLE_RETURN).status, SP.STATUS_PARTIAL)
        self.assertIsNotNone(fact.metric(SP.METRIC_CYCLE_RETURN).value)
        self.assertEqual(fact.coverage.degraded_nav_days, 1)
        self.assertIn(SP.R_DEGRADED_QUOTE_QUALITY, fact.blocking_reasons)

    def test_unknown_quote_status_is_treated_as_degraded_not_verified(self):
        odd = slice_(nav_points=(nav("2026-10-09", 103000.0, status="something_new"),))
        fact = SP.build_performance_fact(request(), slices=[odd])
        self.assertEqual(fact.coverage.degraded_nav_days, 1)


class CrossCycleTests(unittest.TestCase):
    def test_same_day_rollover_makes_that_valuation_ambiguous(self):
        first = slice_(cycle_id=1, cycle_key="c1", started_at="2026-10-01 09:31:00",
                       ended_at="2026-10-09 16:45:00",
                       nav_points=(nav("2026-10-02", 101000.0), nav("2026-10-09", 102000.0)))
        second = slice_(cycle_id=2, cycle_key="c2", started_at="2026-10-09 16:46:00",
                        ended_at=None, cycle_status="active", source=SP.SOURCE_ACTIVE,
                        nav_points=(nav("2026-10-09", 102000.0), nav("2026-10-16", 104000.0)))
        fact = SP.build_performance_fact(request(), slices=[first, second])
        self.assertGreaterEqual(fact.coverage.ambiguous_nav_days, 1)
        self.assertIn(SP.R_AMBIGUOUS_CYCLE_NAV, fact.blocking_reasons)
        self.assertEqual(fact.metric(SP.METRIC_CYCLE_RETURN).status, SP.STATUS_PARTIAL)

    def test_returns_are_reported_per_cycle_and_never_compounded(self):
        first = slice_(cycle_id=1, cycle_key="c1")
        second = slice_(cycle_id=2, cycle_key="c2", started_at="2026-10-21 09:31:00",
                        ended_at=None, cycle_status="active", source=SP.SOURCE_ACTIVE,
                        nav_points=(nav("2026-10-22", 110000.0), nav("2026-10-23", 111000.0)))
        fact = SP.build_performance_fact(request(), slices=[first, second])
        per_cycle = fact.metric(SP.METRIC_CYCLE_RETURN).value["per_cycle"]
        self.assertEqual(sorted(per_cycle), ["c1", "c2"])
        self.assertAlmostEqual(per_cycle["c1"], 0.03, places=8)
        self.assertAlmostEqual(per_cycle["c2"], 0.11, places=8)

    def test_max_drawdown_is_per_cycle_never_the_max_across_cycles(self):
        shallow = slice_(cycle_id=1, cycle_key="c1",
                         nav_points=(nav("2026-10-02", 99000.0), nav("2026-10-03", 99500.0)))
        deep = slice_(cycle_id=2, cycle_key="c2", started_at="2026-10-10 09:31:00",
                      ended_at="2026-10-20 16:45:00",
                      nav_points=(nav("2026-10-11", 80000.0), nav("2026-10-12", 81000.0)))
        fact = SP.build_performance_fact(request(), slices=[shallow, deep])
        drawdowns = fact.metric(SP.METRIC_MAX_DRAWDOWN).value["per_cycle"]
        self.assertEqual(sorted(drawdowns), ["c1", "c2"])
        self.assertAlmostEqual(drawdowns["c2"], 0.2, places=8)
        self.assertIsInstance(drawdowns, dict, "跨周期回撤不能压成一个标量")

    def test_time_weighted_return_refuses_to_link_cycles_without_a_rule(self):
        fact = SP.build_performance_fact(request(), slices=[slice_()])
        metric = fact.metric(SP.METRIC_TIME_WEIGHTED_RETURN)
        self.assertEqual(metric.status, SP.STATUS_UNAVAILABLE)
        self.assertIn(SP.R_CYCLE_LINKING_NOT_DEFINED, metric.reasons)
        self.assertIsNone(metric.value)

    def test_unknown_capital_flow_is_flagged_and_return_is_lowered(self):
        drifted = slice_(capital=100000.0, reasons=(SP.R_UNKNOWN_CAPITAL_FLOW,))
        fact = SP.build_performance_fact(request(), slices=[drifted])
        self.assertIn(SP.R_UNKNOWN_CAPITAL_FLOW, fact.blocking_reasons)


class CoverageTests(unittest.TestCase):
    def test_no_calendar_means_no_denominator(self):
        fact = SP.build_performance_fact(request(), slices=[slice_()], expected_nav_days=None)
        self.assertIsNone(fact.coverage.expected_nav_days)
        self.assertIsNone(fact.coverage.missing_nav_days)
        self.assertIn(SP.R_SESSION_CALENDAR_UNAVAILABLE, fact.coverage.reasons)

    def test_injected_calendar_produces_an_honest_missing_count(self):
        fact = SP.build_performance_fact(request(), slices=[slice_()], expected_nav_days=12)
        self.assertEqual(fact.coverage.expected_nav_days, 12)
        self.assertEqual(fact.coverage.observed_nav_days, 2)
        self.assertEqual(fact.coverage.missing_nav_days, 10)

    def test_observed_above_expected_is_a_contract_violation(self):
        with self.assertRaises(SP.PerformanceEvidenceError):
            SP.Coverage(expected_nav_days=1, observed_nav_days=2, missing_nav_days=0,
                        excluded_fills=0, included_fills=0, ambiguous_nav_days=0,
                        degraded_nav_days=0, cycles_included=1, cycles_excluded=0)

    def test_fills_outside_the_window_are_excluded_from_every_metric(self):
        wide = slice_(fills=(fill("2026-09-30", "buy", 1000.0),
                             fill("2026-10-05", "buy", 2000.0)))
        fact = SP.build_performance_fact(request(), slices=[wide])
        self.assertEqual(fact.metric(SP.METRIC_TRADE_COUNT).value, 1)
        self.assertEqual(fact.metric(SP.METRIC_TURNOVER_AMOUNT).value, 2000.0)


class ParameterBoundaryTests(unittest.TestCase):
    def test_two_parameter_versions_on_one_day_make_the_boundary_unobservable(self):
        changed = slice_(parameter_versions=(
            SP.ParameterVersion(effective_date="2026-10-05", version="v3"),
            SP.ParameterVersion(effective_date="2026-10-05", version="v4"),
        ))
        fact = SP.build_performance_fact(request(), slices=[changed])
        self.assertIn(SP.R_PARAMETER_BOUNDARY_UNOBSERVABLE, fact.blocking_reasons)
        for name in (SP.METRIC_CYCLE_RETURN, SP.METRIC_MAX_DRAWDOWN,
                     SP.METRIC_TIME_WEIGHTED_RETURN):
            self.assertEqual(fact.metric(name).status, SP.STATUS_UNAVAILABLE, name)
        # 交易事实仍然可用：参数边界不可观测不影响"成交了几笔"。
        self.assertEqual(fact.metric(SP.METRIC_TRADE_COUNT).status, SP.STATUS_AVAILABLE)

    def test_a_requested_parameter_version_must_actually_appear(self):
        single = slice_(parameter_versions=(
            SP.ParameterVersion(effective_date="2026-10-05", version="v3"),))
        fact = SP.build_performance_fact(
            request(parameter_version_id="v9"), slices=[single])
        self.assertIn(SP.R_PARAMETER_BOUNDARY_UNOBSERVABLE, fact.blocking_reasons)


class FingerprintTests(unittest.TestCase):
    def test_same_inputs_produce_the_same_fingerprint(self):
        first = SP.build_performance_fact(request(), slices=[slice_()])
        second = SP.build_performance_fact(request(), slices=[slice_()])
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_a_corrected_source_changes_the_fingerprint(self):
        original = SP.build_performance_fact(request(), slices=[slice_()])
        corrected = SP.build_performance_fact(request(), slices=[
            slice_(nav_points=(nav("2026-10-02", 101000.0), nav("2026-10-19", 103500.0)))])
        self.assertNotEqual(original.fingerprint, corrected.fingerprint)

    def test_the_method_version_participates_in_the_fingerprint(self):
        fact = SP.build_performance_fact(request(), slices=[slice_()])
        material = fact.fingerprint_material()
        self.assertEqual(SP.fingerprint(material), fact.fingerprint)
        material["method_version"] = "something-else"
        self.assertNotEqual(SP.fingerprint(material), fact.fingerprint)


# --------------------------------------------------------------------------- #
# 读取层：真库、只读、不造数
# --------------------------------------------------------------------------- #

ARCHIVE_FORMAT = PCS.ARCHIVE_FORMAT


def _archive_snapshot(account_id=STRATEGY, *, cycle_id=1, nav_rows=None, orders=None,
                      fills=None, capital=100000.0, parameters=None, fmt=ARCHIVE_FORMAT):
    snapshot = {
        "_archive_format": fmt,
        "paper_accounts": [{"id": account_id, "initial_cash": capital}],
        "paper_orders": orders if orders is not None else [],
        "paper_fills": fills if fills is not None else [],
        "paper_nav": nav_rows if nav_rows is not None else [
            {"account_id": account_id, "nav_date": "2026-10-02", "nav": 101000.0,
             "quote_status": "verified"},
            {"account_id": account_id, "nav_date": "2026-10-19", "nav": 103000.0,
             "quote_status": "verified"},
        ],
        "paper_parameter_versions": parameters if parameters is not None else [
            {"account_id": account_id, "cycle_id": cycle_id, "effective_date": "2026-10-01",
             "version": "v3"},
        ],
    }
    return snapshot


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._folder.name, "paper_trading.sqlite3")
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE paper_cycles (id INTEGER PRIMARY KEY, cycle_key TEXT, status TEXT,
                capital REAL, started_at TEXT, ended_at TEXT);
            CREATE TABLE paper_cycle_strategy_versions (cycle_id INTEGER, account_id TEXT,
                strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
                bound_at TEXT);
            CREATE TABLE paper_archives (id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER, cycle_key TEXT, reason TEXT, snapshot TEXT,
                created_at TEXT);
            CREATE TABLE paper_nav (account_id TEXT, nav_date TEXT, nav REAL,
                quote_status TEXT, UNIQUE(account_id, nav_date));
            CREATE TABLE paper_orders (id INTEGER PRIMARY KEY, account_id TEXT, side TEXT,
                strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
                cycle_id INTEGER, realized_pnl REAL, execution_verified INTEGER);
            CREATE TABLE paper_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
                account_id TEXT, side TEXT, code TEXT, qty INTEGER, price REAL,
                amount REAL, fees REAL, fill_date TEXT);
            CREATE TABLE paper_accounts (id TEXT PRIMARY KEY, initial_cash REAL,
                cycle_id INTEGER);
            CREATE TABLE paper_parameter_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER, account_id TEXT, version TEXT, effective_date TEXT);
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self._folder.cleanup()

    def write(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def add_cycle(self, cycle_id, *, status="archived", capital=100000.0,
                  started_at="2026-10-01 09:31:00", ended_at="2026-10-20 16:45:00"):
        self.write("INSERT INTO paper_cycles(id,cycle_key,status,capital,started_at,ended_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (cycle_id, f"2026-C{cycle_id}", status, capital, started_at, ended_at))
        self.bind(cycle_id, checksum=CHECKSUM)

    def bind(self, cycle_id, *, checksum=CHECKSUM, version=3):
        self.write("INSERT INTO paper_cycle_strategy_versions VALUES(?,?,?,?,?,?)",
                   (cycle_id, STRATEGY, STRATEGY, version, checksum, "2026-10-01 09:31:00"))

    def add_archive(self, cycle_id, snapshot):
        self.write("INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at)"
                   " VALUES(?,?,?,?,?)",
                   (cycle_id, f"2026-C{cycle_id}", "rollover",
                    json.dumps(snapshot, ensure_ascii=False), "2026-10-20 16:45:00"))

    def snapshot_of_everything(self):
        conn = sqlite3.connect(self.path)
        try:
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("paper_cycles", "paper_cycle_strategy_versions",
                                  "paper_archives", "paper_nav", "paper_orders",
                                  "paper_fills", "paper_accounts",
                                  "paper_parameter_versions")}
        finally:
            conn.close()


class HealthDimensionWiringTests(LedgerFixture):
    """绩效维度由本 owner 产出（方案 §2.5 的第一步：只读证据，不改阈值）。"""

    def _dimension(self, conn, start="2026-10-01", end="2026-10-31"):
        import strategy_health as SH
        import strategy_health_service as SHS

        window = SH.HealthObservationWindow(observation_start=start, observation_end=end)
        return SHS._performance_dimension(conn, (STRATEGY, 3, CHECKSUM), window)

    def test_a_pinned_cycle_produces_a_populated_dimension(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot())
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(fact["status"], (SP.STATUS_AVAILABLE, SP.STATUS_PARTIAL))
        conn = sqlite3.connect(self.path)
        try:
            dimension = self._dimension(conn)
        finally:
            conn.close()
        self.assertIn(dimension.status, ("AVAILABLE", "PARTIAL"))
        self.assertEqual(dimension.provenance, "OWNER_ISSUED")
        self.assertEqual(dimension.facts["strategy_version_scoped_owner"],
                         SP.OWNER_VERSION)
        self.assertIn("cycle_return", dimension.facts["metrics"])
        self.assertIsNotNone(dimension.facts["fact_fingerprint"])

    def test_without_a_pin_the_reason_is_specific_not_generic(self):
        # 有周期，但没有绑定到这个精确版本：owner 必须说"没有 pin"，而不是泛泛地说没 owner。
        self.write("INSERT INTO paper_cycles(id,cycle_key,status,capital,started_at,ended_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (1, "2026-C1", "archived", 100000.0, "2026-10-01 09:31:00",
                    "2026-10-20 16:45:00"))
        conn = sqlite3.connect(self.path)
        try:
            dimension = self._dimension(conn)
        finally:
            conn.close()
        self.assertEqual(dimension.status, "UNAVAILABLE")
        self.assertEqual(dimension.provenance, "UNAVAILABLE")
        self.assertIn("no_exact_pin", dimension.blocking_reasons)


class ArchiveReadTests(LedgerFixture):
    def test_archived_cycle_is_read_from_the_archive_by_exact_cycle_id(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot())
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(fact["status"] in (SP.STATUS_AVAILABLE, SP.STATUS_PARTIAL), True)
        self.assertEqual(fact["metrics"][SP.METRIC_CYCLE_RETURN]["value"]["per_cycle"]["2026-C1"],
                         round(103000.0 / 100000.0 - 1.0, 8))
        self.assertEqual(fact["cycle_slices"][0]["source"], SP.SOURCE_ARCHIVE)
        self.assertIsNotNone(fact["cycle_slices"][0]["archive_fingerprint"])

    def test_reading_writes_nothing_to_the_ledger(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot())
        before = self.snapshot_of_everything()
        SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(self.snapshot_of_everything(), before)

    def test_missing_archive_is_reported_not_filled_with_zeros(self):
        self.add_cycle(1)
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(SP.R_ARCHIVE_MISSING, fact["blocking_reasons"])
        self.assertIsNone(fact["metrics"][SP.METRIC_CYCLE_RETURN]["value"])

    def test_corrupt_archive_is_reported(self):
        self.add_cycle(1)
        self.write("INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at)"
                   " VALUES(?,?,?,?,?)", (1, "2026-C1", "rollover", "{not json",
                                          "2026-10-20 16:45:00"))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(SP.R_ARCHIVE_CORRUPT, fact["blocking_reasons"])

    def test_unsupported_archive_format_is_refused(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(fmt="compact-ledger-v0"))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(SP.R_ARCHIVE_FORMAT_UNSUPPORTED, fact["blocking_reasons"])

    def test_duplicate_archives_are_flagged_and_counted_once(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot())
        self.add_archive(1, _archive_snapshot(nav_rows=[
            {"account_id": STRATEGY, "nav_date": "2026-10-02", "nav": 101000.0,
             "quote_status": "verified"},
            {"account_id": STRATEGY, "nav_date": "2026-10-19", "nav": 104000.0,
             "quote_status": "verified"}]))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(SP.R_ARCHIVE_DUPLICATE, fact["blocking_reasons"])
        self.assertEqual(fact["coverage"]["cycles_included"], 1)
        self.assertEqual(
            fact["metrics"][SP.METRIC_CYCLE_RETURN]["value"]["per_cycle"]["2026-C1"],
            round(104000.0 / 100000.0 - 1.0, 8))

    def test_a_fill_without_a_strategy_stamp_is_excluded(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(
            orders=[{"id": 7, "account_id": STRATEGY, "side": "buy", "cycle_id": 1,
                     "strategy_id": None, "strategy_version": None,
                     "strategy_checksum": None}],
            fills=[{"account_id": STRATEGY, "order_id": 7, "side": "buy",
                    "code": "600000", "amount": 1000.0, "fees": 5.0,
                    "fill_date": "2026-10-05"}]))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(fact["coverage"]["excluded_fills"], 1)
        self.assertEqual(fact["coverage"]["included_fills"], 0)
        self.assertIn(SP.R_IDENTITY_STAMP_MISSING, fact["blocking_reasons"])

    def test_a_fill_with_a_foreign_stamp_is_excluded(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(
            orders=[{"id": 8, "account_id": STRATEGY, "side": "buy", "cycle_id": 1,
                     "strategy_id": STRATEGY, "strategy_version": 3,
                     "strategy_checksum": OTHER_CHECKSUM}],
            fills=[{"account_id": STRATEGY, "order_id": 8, "side": "buy",
                    "code": "600000", "amount": 1000.0, "fees": 5.0,
                    "fill_date": "2026-10-05"}]))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(fact["coverage"]["excluded_fills"], 1)
        self.assertIn(SP.R_IDENTITY_STAMP_MISMATCH, fact["blocking_reasons"])

    def test_an_unverified_sell_is_not_counted_as_realized_profit(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(orders=[
            {"id": 9, "account_id": STRATEGY, "side": "sell", "cycle_id": 1,
             "strategy_id": STRATEGY, "strategy_version": 3,
             "strategy_checksum": CHECKSUM, "realized_pnl": 5000.0,
             "execution_verified": 0}]))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(fact["metrics"][SP.METRIC_REALIZED_PNL]["status"],
                         SP.STATUS_UNAVAILABLE)
        self.assertIsNone(fact["metrics"][SP.METRIC_REALIZED_PNL]["value"])

    def test_cycle_capital_disagreeing_with_the_account_is_flagged(self):
        self.add_cycle(1, capital=50000.0)
        self.add_archive(1, _archive_snapshot(capital=100000.0))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(SP.R_UNKNOWN_CAPITAL_FLOW, fact["blocking_reasons"])

    def _sell(self, order_id, code, fill_date, pnl, *, verified=1):
        return (
            {"id": order_id, "account_id": STRATEGY, "side": "sell", "cycle_id": 1,
             "strategy_id": STRATEGY, "strategy_version": 3,
             "strategy_checksum": CHECKSUM, "realized_pnl": pnl,
             "execution_verified": verified},
            {"account_id": STRATEGY, "order_id": order_id, "side": "sell", "code": code,
             "amount": 10000.0, "fees": 5.0, "fill_date": fill_date},
        )

    def test_only_the_in_window_realized_pnl_is_reported(self):
        """R1 复现：10-05 赚 500、10-19 赚 100，窗口 [10-15, 10-21) 应为 100。"""
        early, early_fill = self._sell(21, "600001", "2026-10-05", 500.0)
        late, late_fill = self._sell(22, "600002", "2026-10-19", 100.0)
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(orders=[early, late],
                                             fills=[early_fill, late_fill]))
        fact = SP.collect_performance_fact(
            request(observation_start="2026-10-15", observation_end="2026-10-21"),
            db_path=self.path)
        self.assertEqual(fact["metrics"][SP.METRIC_REALIZED_PNL]["value"], 100.0)
        self.assertEqual(fact["metrics"][SP.METRIC_REALIZED_PNL]["status"],
                         SP.STATUS_AVAILABLE)

    def test_a_cross_day_partial_fill_cannot_be_split_by_date(self):
        order, _fill = self._sell(23, "600001", "2026-10-05", 500.0)
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(orders=[order], fills=[
            {"account_id": STRATEGY, "order_id": 23, "side": "sell", "code": "600001",
             "amount": 5000.0, "fees": 2.0, "fill_date": "2026-10-05"},
            {"account_id": STRATEGY, "order_id": 23, "side": "sell", "code": "600001",
             "amount": 5000.0, "fees": 2.0, "fill_date": "2026-10-19"},
        ]))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        metric = fact["metrics"][SP.METRIC_REALIZED_PNL]
        self.assertEqual(metric["status"], SP.STATUS_UNAVAILABLE)
        self.assertIsNone(metric["value"])
        self.assertIn(SP.R_INCOMPLETE_COST_BASIS, fact["blocking_reasons"])

    def test_a_sell_without_any_fill_row_cannot_be_dated(self):
        order, _fill = self._sell(24, "600001", "2026-10-05", 500.0)
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot(orders=[order], fills=[]))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(fact["metrics"][SP.METRIC_REALIZED_PNL]["status"],
                         SP.STATUS_UNAVAILABLE)


class PinSelectionTests(LedgerFixture):
    def test_only_cycles_bound_to_the_exact_version_are_read(self):
        self.add_cycle(1)
        self.add_archive(1, _archive_snapshot())
        self.write("INSERT INTO paper_cycles(id,cycle_key,status,capital,started_at,ended_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (2, "2026-C2", "archived", 100000.0, "2026-10-01 09:31:00",
                    "2026-10-20 16:45:00"))
        self.bind(2, checksum=OTHER_CHECKSUM)
        self.add_archive(2, _archive_snapshot())
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(len(fact["cycle_slices"]), 1)
        self.assertEqual(fact["cycle_slices"][0]["cycle_id"], 1)

    def test_a_missing_cycle_row_surfaces_instead_of_disappearing(self):
        self.write("INSERT INTO paper_cycle_strategy_versions VALUES(?,?,?,?,?,?)",
                   (99, STRATEGY, STRATEGY, 3, CHECKSUM, "2026-10-01 09:31:00"))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertIn(SP.R_CYCLE_ROW_MISSING, fact["blocking_reasons"])

    def test_the_ledger_that_does_not_exist_is_an_environment_error(self):
        with self.assertRaises(SP.LedgerUnavailable):
            SP.collect_performance_fact(request(),
                                        db_path=os.path.join(self._folder.name, "nope.db"))


class ActiveCycleTests(LedgerFixture):
    def test_active_cycle_reads_live_tables_and_never_invents_realized_pnl(self):
        self.add_cycle(1, status="active", ended_at=None)
        self.write("INSERT INTO paper_accounts(id,initial_cash,cycle_id) VALUES(?,?,?)",
                   (STRATEGY, 100000.0, 1))
        self.write("INSERT INTO paper_nav(account_id,nav_date,nav,quote_status)"
                   " VALUES(?,?,?,?)", (STRATEGY, "2026-10-02", 101500.0, "verified"))
        self.write("INSERT INTO paper_nav(account_id,nav_date,nav,quote_status)"
                   " VALUES(?,?,?,?)", (STRATEGY, "2026-10-09", 102500.0, "verified"))
        self.write("INSERT INTO paper_orders(id,account_id,side,strategy_id,strategy_version,"
                   "strategy_checksum,cycle_id,realized_pnl,execution_verified)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (11, STRATEGY, "buy", STRATEGY, 3, CHECKSUM, 1, None, 1))
        self.write("INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,"
                   "fees,fill_date) VALUES(?,?,?,?,?,?,?,?,?)",
                   (11, STRATEGY, "buy", "600000", 100, 10.0, 1000.0, 5.0, "2026-10-05"))
        fact = SP.collect_performance_fact(request(), db_path=self.path)
        self.assertEqual(fact["cycle_slices"][0]["source"], SP.SOURCE_ACTIVE)
        self.assertEqual(fact["coverage"]["included_fills"], 1)
        self.assertEqual(fact["metrics"][SP.METRIC_TRADE_COUNT]["value"], 1)
        self.assertEqual(fact["metrics"][SP.METRIC_TURNOVER_AMOUNT]["value"], 1000.0)
        self.assertEqual(fact["metrics"][SP.METRIC_FEES]["value"], 5.0)
        self.assertEqual(fact["metrics"][SP.METRIC_CYCLE_RETURN]["value"]["per_cycle"]
                         ["2026-C1"], round(102500.0 / 100000.0 - 1.0, 8))


if __name__ == "__main__":
    unittest.main()
