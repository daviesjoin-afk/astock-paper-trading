"""The preparation owner has no writer; each invocation owns its reuse scope."""
import datetime as dt
import unittest
from unittest import mock

import paper_candidate_scan as Scan


class PreparationTests(unittest.TestCase):
    def test_shared_inputs_are_round_local_and_blocked_strategy_does_not_abort_others(self):
        events, scopes = [], []
        def candidates(account, factor_day, market, **kwargs):
            events.append("rows:" + account["id"])
            scopes.append(kwargs["shared_inputs"])
            if account["id"] == "broken":
                raise ValueError("fixture")
            return [{"code": "600001", "score": 10}], {"factor_date": str(factor_day)}
        def micro(codes, **kwargs):
            events.append("micro")
            self.assertEqual(codes, ["600001"])
            return {"600001": {"status": "partial", "score_applied": False}}
        def evidence(accounts, rows, day):
            events.append("evidence")
            self.assertTrue(rows["broken"][1]["blocked"])
            return {"a": {"quotes": {}}}
        ports = Scan.PreparationPorts(candidates, lambda rows, account, **kw: rows, micro, evidence)
        args = dict(factor_day=dt.date(2026, 10, 19), day=dt.date(2026, 10, 20),
                    market={}, sector_rows=[], live_universe=[], snapshot_asof="2026-10-20T10:00:00+08:00",
                    ports=ports)
        accounts = [{"id": "a"}, {"id": "broken"}, {"id": "b"}]
        first = Scan.prepare(accounts, **args)
        self.assertEqual(events, ["rows:a", "rows:broken", "rows:b", "micro", "evidence"])
        self.assertTrue(all(scope is scopes[0] for scope in scopes))
        self.assertEqual(first.rows_by_account["a"][0][0]["score"], 10)
        self.assertFalse(first.microstructures["600001"]["score_applied"])
        Scan.prepare(accounts, **args)
        self.assertIsNot(scopes[0], scopes[-1])

    def test_source_failure_preserves_candidates_and_evidence_failure_is_not_hidden(self):
        ports = Scan.PreparationPorts(
            lambda *args, **kwargs: ([{"code": "600001"}], {}),
            lambda rows, *args, **kwargs: rows,
            mock.Mock(side_effect=TimeoutError), lambda *args: {"a": {}},
        )
        args = dict(factor_day="2026-10-19", day="2026-10-20", market={}, sector_rows=[],
                    live_universe=[], snapshot_asof="fixed", ports=ports)
        result = Scan.prepare([{"id": "a"}], **args)
        self.assertEqual(result.microstructures, {})
        self.assertEqual(result.rows_by_account["a"][0], [{"code": "600001"}])
        self.assertEqual(result.evidence_by_account, {"a": {}})
        failing = Scan.PreparationPorts(ports.candidate_rows, ports.prioritize, ports.microstructures,
                                       mock.Mock(side_effect=RuntimeError("evidence unavailable")))
        with self.assertRaisesRegex(RuntimeError, "evidence unavailable"):
            Scan.prepare([{"id": "a"}], **{**args, "ports": failing})
