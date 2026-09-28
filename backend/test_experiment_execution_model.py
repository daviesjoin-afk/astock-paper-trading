"""Production-path regression tests for fail-closed R29 execution."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import experiment_contract as EC
import experiment_execution_model as EM
import test_experiment_pit_validation as FIXTURES
import tradability_archive as TA


CODE = "000001.SH"
SESSIONS = ("2026-01-05", "2026-01-06")


def _bar(session: str) -> dict:
    return {"code": CODE, "session": session, "open": 10.0, "high": 11.0,
            "low": 9.0, "close": 10.0, "volume": 1000.0, "amount": 10000.0}


def _evidence(*, suspended: bool = False, observed_at: str = "2026-01-06T09:00:00+08:00"):
    return TA.TradabilityEvidence(
        code=CODE, session_date=SESSIONS[1], is_listed=True,
        listing_date="2020-01-01", delisting_date=None, is_st=False,
        is_suspended=suspended, suspension_reason="suspended" if suspended else None,
        has_market_quote=not suspended, has_trade_volume=not suspended,
        is_price_limit_locked=False, price_limit_direction=None,
        source="test-owner", observed_at=observed_at,
        effective_at="2026-01-06T09:30:00+08:00",
    )


def _spec() -> EC.ExperimentSpec:
    initial = FIXTURES._spec()
    return replace(
        initial,
        parameter_set={**initial.parameter_set, "validation_portfolio": {
            "initial_cash": 100000, "max_positions": 1, "position_sizing": "equal_weight",
        }},
        execution_assumptions={
            "execution_profile_version": EM.PROFILE_VERSION,
            "fill_assumptions": {"signal": "session_close", "execution": "next_session_open"},
            "capacity_assumptions": {"participation_rate": 0.05},
            "t_plus_one_semantics": "sell_after_next_session",
            "price_limit_semantics": "historical_tradability_archive_v1",
            "partial_fill_semantics": "volume_participation_cap",
        },
    )


def _simulate(*, signal: bool, bars=None, evidence=None, repository=None):
    cutoff = 0 if signal else 100
    return EM.simulate(
        _spec(), ast={"op": "strategy", "rule": {
            "op": "gt", "left": {"op": "field", "name": "close"},
            "right": {"op": "const", "value": cutoff},
        }},
        sessions=SESSIONS,
        members_by_session={session: [{"code": CODE}] for session in SESSIONS},
        bars=[_bar(day) for day in SESSIONS] if bars is None else bars,
        tradability_repository=repository,
        tradability_evidence=evidence,
    )


class ExperimentExecutionModelTests(unittest.TestCase):
    def test_zero_and_one_trade_counts_are_integers_and_completed_results_validate(self):
        for signal, facts, expected in (
            (False, {}, 0),
            (True, {(CODE, SESSIONS[1]): _evidence()}, 1),
        ):
            metrics = _simulate(signal=signal, evidence=facts)
            self.assertIs(type(metrics["trade_count"]), int)
            self.assertEqual(expected, metrics["trade_count"])
            result = EC.ExperimentResult(
                experiment_fingerprint="a" * 64, status="completed",
                total_return=metrics["total_return"], max_drawdown=metrics["max_drawdown"],
                volatility=metrics["volatility"], turnover=metrics["turnover"],
                trade_count=metrics["trade_count"], total_cost=metrics["total_cost"],
                exposure=metrics["exposure"], capacity_proxy=metrics["capacity_proxy"],
                data_coverage=metrics["data_coverage"], regime_breakdown=metrics["regime_breakdown"],
            )
            self.assertEqual("completed", result.status)

    def test_missing_open_tradability_evidence_makes_execution_unavailable(self):
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable,
                                    "execution_tradability_unavailable"):
            _simulate(signal=True, evidence={})

    def test_close_time_evidence_cannot_replace_open_time_evidence(self):
        class CloseOnlyRepository:
            def __init__(self):
                self.requested = []

            def evidence_at(self, code, session, decision_time):
                self.requested.append(decision_time)
                if decision_time.endswith("15:00:00+08:00"):
                    return _evidence(observed_at="2026-01-06T15:00:00+08:00")
                return None

        repository = CloseOnlyRepository()
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable,
                                    "execution_tradability_unavailable"):
            _simulate(signal=True, repository=repository)
        self.assertEqual(["2026-01-06T09:30:00+08:00"], repository.requested)

    def test_missing_open_bar_with_proven_tradability_is_unavailable(self):
        with self.assertRaisesRegex(EM.ExperimentExecutionUnavailable,
                                    "execution_market_bar_unavailable"):
            _simulate(signal=True, bars=[_bar(SESSIONS[0])],
                      evidence={(CODE, SESSIONS[1]): _evidence()})

    def test_missing_bar_with_proven_suspension_is_a_known_non_fill(self):
        metrics = _simulate(signal=True, bars=[_bar(SESSIONS[0])],
                            evidence={(CODE, SESSIONS[1]): _evidence(suspended=True)})
        self.assertEqual(0, metrics["trade_count"])


if __name__ == "__main__":
    unittest.main()
