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
                mock.patch.object(PT.U, "load_universe", wraps=PT.U.load_universe) as universe, \
                mock.patch.object(PT.F, "compute_fundamental_factors", wraps=PT.F.compute_fundamental_factors) as compute:
            expected_rows, expected_meta = PT._candidate_rows(account, Replay.D_DAY, {"light": "green"})
            self.assertTrue(expected_rows, "positive control must produce actual candidates")
            source.reset_mock()
            universe.reset_mock()
            compute.reset_mock()
            shared = PCI.CandidateScanInputs(PT.F.live_snapshot_asof())
            for _ in range(3):
                rows, meta = PT._candidate_rows(account, Replay.D_DAY, {"light": "green"}, shared_inputs=shared)
                self.assertEqual(rows, expected_rows)
                # This diagnostic measures wall-clock age, not selection facts.
                expected = copy.deepcopy(expected_meta)
                expected["factor_freshness"].pop("built_age_seconds", None)
                meta["factor_freshness"].pop("built_age_seconds", None)
                self.assertEqual(meta, expected)
            source.assert_called_once()
            universe.assert_called_once()
            compute.assert_called_once()

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

    def test_research_backfill_reuses_sources_and_computation_within_one_run(self):
        self.assertEqual(PT._rebuild_selection_factor_cache(Replay.D_DAY)["status"], "ok")
        with self._conn() as conn:
            account = dict(conn.execute("SELECT * FROM paper_accounts WHERE id=?", (Replay.STRATEGY_ID,)).fetchone())
        base = PT.U.load_universe()
        live = [{**row, "quote_at": Replay.D_DAY.isoformat() + "T15:00:00+08:00"} for row in base]
        finance = {"data": {"600901": {"net_profit": 100., "profit_yoy": 20.}}}
        researcher = mock.Mock()
        researcher.record_shadow_run.return_value = {"status": "saved"}
        with mock.patch.object(PT, "init_db"), \
                mock.patch.object(PT.MDSvc, "refresh_rows", return_value=live), \
                mock.patch.object(PT, "_market_state", return_value={"light": "green"}), \
                mock.patch.object(PT, "_get_pr", return_value=researcher), \
                mock.patch.object(PT, "_active_account_rows", return_value=[dict(account) for _ in range(3)]), \
                mock.patch.object(PT.dfc, "fetch_finance_latest", return_value=finance) as source, \
                mock.patch.object(PT.U, "load_universe", wraps=PT.U.load_universe) as universe, \
                mock.patch.object(PT.F, "compute_fundamental_factors", wraps=PT.F.compute_fundamental_factors) as compute, \
                mock.patch.object(PT.F, "live_snapshot_asof", return_value=Replay.D_DAY.isoformat() + "T15:05:00+08:00"):
            result = PT.backfill_research_shadow(Replay.D_DAY)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["accounts"]), 3)
        self.assertTrue(all(row.get("candidates", 0) > 0 for row in result["accounts"]), result)
        source.assert_called_once()
        universe.assert_called_once()
        compute.assert_called_once()
        self.assertEqual(researcher.record_shadow_run.call_count, 3)

    def test_close_generation_passes_a_round_scoped_snapshot(self):
        with mock.patch.object(PT, "_candidate_rows", wraps=PT._candidate_rows) as candidate:
            result = PT.generate_signals(Replay.D_DAY)
        self.assertTrue(result["accounts"], result)
        self.assertGreater(candidate.call_count, 0)
        snapshots = [call.kwargs["shared_inputs"] for call in candidate.call_args_list]
        self.assertTrue(all(isinstance(snapshot, PCI.CandidateScanInputs) for snapshot in snapshots))
        self.assertTrue(all(snapshot is snapshots[0] for snapshot in snapshots))
