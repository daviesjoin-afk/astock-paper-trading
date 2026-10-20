"""Compare round snapshots with the real offline candidate pipeline."""
import copy
from unittest import mock

import paper_candidate_inputs as PCI
import paper_trading as PT
import test_provenance_inflight_change as Replay


class CandidateSnapshotReplayTests(Replay._RolloverFixture):
    def test_real_candidates_keep_order_scores_and_reasons_with_fewer_source_reads(self):
        self.assertEqual(PT._rebuild_selection_factor_cache(Replay.D_DAY)["status"], "ok")
        with self._conn() as conn:
            account = dict(conn.execute("SELECT * FROM paper_accounts WHERE id=?", (Replay.STRATEGY_ID,)).fetchone())
        finance = {"data": {"600901": {"net_profit": 100., "profit_yoy": 20.}}}
        with mock.patch.object(PT.dfc, "fetch_finance_latest", return_value=finance) as source, \
                mock.patch.object(PT.U, "load_universe", wraps=PT.U.load_universe) as universe:
            expected_rows, expected_meta = PT._candidate_rows(account, Replay.D_DAY, {"light": "green"})
            self.assertTrue(expected_rows, "positive control must produce actual candidates")
            source.reset_mock()
            universe.reset_mock()
            shared = PCI.CandidateScanInputs(PT.F.live_snapshot_asof())
            for _ in range(2):
                rows, meta = PT._candidate_rows(account, Replay.D_DAY, {"light": "green"}, shared_inputs=shared)
                self.assertEqual(rows, expected_rows)
                # This diagnostic measures wall-clock age, not selection facts.
                expected = copy.deepcopy(expected_meta)
                expected["factor_freshness"].pop("built_age_seconds", None)
                meta["factor_freshness"].pop("built_age_seconds", None)
                self.assertEqual(meta, expected)
            source.assert_called_once()
            universe.assert_called_once()

    def test_blocked_freshness_keeps_reason_and_never_reads_finance(self):
        self.assertEqual(PT._rebuild_selection_factor_cache(Replay.D_DAY)["status"], "ok")
        account = {"id": Replay.STRATEGY_ID}
        with mock.patch.object(PT, "_selection_factor_freshness", return_value={
                "passed": False, "reason": "fixture incomplete factors"}), \
                mock.patch.object(PT.dfc, "fetch_finance_latest") as source:
            expected = PT._candidate_rows(account, Replay.D_DAY, {"light": "green"})
            shared = PCI.CandidateScanInputs(PT.F.live_snapshot_asof())
            self.assertEqual(PT._candidate_rows(account, Replay.D_DAY, {"light": "green"}, shared_inputs=shared), expected)
            source.assert_not_called()
