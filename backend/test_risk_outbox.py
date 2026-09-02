# -*- coding: utf-8 -*-
"""Offline idempotence/recovery checks for adaptive risk parameter commits."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import adaptive_risk as risk  # noqa: E402


class RiskOutboxTests(unittest.TestCase):
    def setUp(self):
        self.adaptive = sqlite3.connect(":memory:")
        self.adaptive.row_factory = sqlite3.Row
        risk.ensure_schema(self.adaptive)

        fd, self.paper_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.paper = sqlite3.connect(self.paper_path)
        self.paper.row_factory = sqlite3.Row
        self.paper.executescript(
            """
            CREATE TABLE paper_accounts(
                id TEXT PRIMARY KEY,
                params TEXT NOT NULL,
                version TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                cycle_id TEXT,
                style TEXT
            );
            CREATE TABLE paper_parameter_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT,
                account_id TEXT NOT NULL,
                version TEXT NOT NULL,
                style TEXT,
                params TEXT NOT NULL,
                reason TEXT,
                effective_date TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE paper_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.paper.execute(
            "INSERT INTO paper_accounts(id,params,version,updated_at,cycle_id,style) VALUES(?,?,?,?,?,?)",
            ("tq_breakout", "{}", "paper-risk-v1", "2026-08-25T09:00:00", "cycle-1", "tq"),
        )
        self.paper.commit()
        self.paper.close()

        baseline = dict(risk.BASE_RISK["tq_breakout"])
        baseline.update(risk.DOWNSIDE_BASE["tq_breakout"])
        candidate = dict(baseline)
        candidate["max_weight"] = 0.14
        self.adaptive.execute(
            """INSERT INTO adaptive_risk_candidates(
               run_date,account_id,regime,baseline_params,candidate_params,evidence,
               risk_reduction_pct,change_kind,status,application_mode,reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2026-08-25", "tq_breakout", "balanced",
                json.dumps(baseline), json.dumps(candidate), json.dumps({}),
                1.0, "conservative_tighten", "eligible_auto_tighten", "test", "test", 
                "2026-08-25T09:00:00", "2026-08-25T09:00:00",
            ),
        )
        self.adaptive.commit()
        self.candidate_id = self.adaptive.execute(
            "SELECT id FROM adaptive_risk_candidates"
        ).fetchone()[0]

    def tearDown(self):
        self.adaptive.close()
        try:
            os.remove(self.paper_path)
        except FileNotFoundError:
            pass

    @staticmethod
    def now():
        return "2026-08-25T10:00:00"

    def _paper_counts(self):
        paper = sqlite3.connect(self.paper_path)
        counts = {
            "versions": paper.execute("SELECT COUNT(*) FROM paper_parameter_versions").fetchone()[0],
            "audits": paper.execute("SELECT COUNT(*) FROM paper_audit").fetchone()[0],
        }
        paper.close()
        return counts

    def test_apply_replays_after_paper_commit_and_is_idempotent(self):
        # Simulate a process dying after the paper transaction but before the
        # adaptive candidate/event/outbox-finalization transaction.
        with mock.patch.object(risk, "_record_risk_event_once", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                risk.apply_candidate(self.adaptive, self.paper_path, self.candidate_id, self.now)

        pending = self.adaptive.execute(
            "SELECT status,attempts FROM adaptive_risk_outbox WHERE candidate_id=?", (self.candidate_id,)
        ).fetchone()
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self._paper_counts(), {"versions": 1, "audits": 1})

        recovered = risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now)
        self.assertEqual(recovered, [self.candidate_id])
        self.assertEqual(
            self.adaptive.execute("SELECT status FROM adaptive_risk_candidates WHERE id=?", (self.candidate_id,)).fetchone()[0],
            "applied",
        )
        self.assertEqual(
            self.adaptive.execute("SELECT status FROM adaptive_risk_outbox WHERE candidate_id=?", (self.candidate_id,)).fetchone()[0],
            "applied",
        )
        self.assertEqual(self._paper_counts(), {"versions": 1, "audits": 1})
        self.assertEqual(
            self.adaptive.execute(
                "SELECT COUNT(*) FROM adaptive_risk_events WHERE candidate_id=? AND event='applied'",
                (self.candidate_id,),
            ).fetchone()[0],
            1,
        )

        # A repeated replay/application cannot create another version, audit,
        # event, or deployment row.
        self.assertEqual(risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now), [])
        risk.apply_candidate(self.adaptive, self.paper_path, self.candidate_id, self.now)
        self.assertEqual(self._paper_counts(), {"versions": 1, "audits": 1})
        self.assertEqual(
            self.adaptive.execute("SELECT COUNT(*) FROM adaptive_risk_events WHERE candidate_id=?", (self.candidate_id,)).fetchone()[0],
            1,
        )

    def test_replay_finalizes_when_paper_version_already_exists(self):
        """A durable paper commit must still finalize the adaptive ledger.

        This models the narrow crash window where the paper transaction and
        its parameter-version/audit rows committed, while the adaptive-side
        candidate/event/deployment transaction had not finished yet.  The
        current paper parameters therefore equal the proposal (``no_change``
        from the replay's point of view), but the pending outbox must not be
        treated as complete until all adaptive records are reconciled.
        """
        candidate = dict(self.adaptive.execute(
            "SELECT * FROM adaptive_risk_candidates WHERE id=?", (self.candidate_id,)
        ).fetchone())
        proposed = json.loads(candidate["candidate_params"])
        version = f"risk-evo-{candidate['run_date'].replace('-', '')}-{self.candidate_id}"
        effective_date = "2026-08-25"
        now = self.now()

        paper = sqlite3.connect(self.paper_path)
        paper.row_factory = sqlite3.Row
        account = dict(paper.execute(
            "SELECT * FROM paper_accounts WHERE id=?", (candidate["account_id"],)
        ).fetchone())
        params = json.loads(account["params"] or "{}")
        params["adaptive_risk"] = proposed
        params["adaptive_risk_meta"] = {
            "status": "active", "candidate_id": self.candidate_id,
            "version": version, "approved_by": "conservative-auto",
            "effective_date": effective_date, "effective_at": now,
            "source_regime": candidate["regime"],
        }
        paper.execute(
            "UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?",
            (json.dumps(params), version, now, candidate["account_id"]),
        )
        paper.execute(
            """INSERT INTO paper_parameter_versions(
               cycle_id,account_id,version,style,params,reason,effective_date,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (account["cycle_id"], candidate["account_id"], version, account["style"],
             json.dumps(params), "模拟已提交但 adaptive 收口中断", effective_date, now),
        )
        paper.execute(
            "INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
            (candidate["account_id"], "adaptive_risk_applied",
             f"candidate={self.candidate_id}; version={version}; effective_at={now}", now),
        )
        paper.commit()
        paper.close()

        risk._queue_outbox(
            self.adaptive, candidate, version, effective_date,
            "conservative-auto", now,
            previous_account_params=account["params"],
            require_conservative=True,
        )

        self.assertEqual(
            self.adaptive.execute(
                "SELECT status FROM adaptive_risk_outbox WHERE candidate_id=?",
                (self.candidate_id,),
            ).fetchone()[0],
            "pending",
        )
        self.assertEqual(
            risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now),
            [self.candidate_id],
        )
        self.assertEqual(
            self.adaptive.execute(
                "SELECT status FROM adaptive_risk_candidates WHERE id=?",
                (self.candidate_id,),
            ).fetchone()[0],
            "applied",
        )
        self.assertEqual(
            self.adaptive.execute(
                "SELECT COUNT(*) FROM adaptive_risk_events WHERE candidate_id=? AND event='applied'",
                (self.candidate_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.adaptive.execute(
                "SELECT COUNT(*) FROM adaptive_risk_deployments WHERE candidate_id=?",
                (self.candidate_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self._paper_counts(), {"versions": 1, "audits": 1})
        self.assertEqual(
            risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now),
            [],
        )
        self.assertEqual(self._paper_counts(), {"versions": 1, "audits": 1})

    def test_rollback_replays_after_paper_commit_and_is_idempotent(self):
        risk.apply_candidate(self.adaptive, self.paper_path, self.candidate_id, self.now)
        with mock.patch.object(risk, "_record_risk_event_once", side_effect=RuntimeError("simulated rollback crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated rollback crash"):
                risk.rollback(self.adaptive, self.paper_path, "tq_breakout", self.now, "test rollback")

        rollback_id = -self.candidate_id
        pending = self.adaptive.execute(
            "SELECT status,operation FROM adaptive_risk_outbox WHERE candidate_id=?", (rollback_id,)
        ).fetchone()
        self.assertEqual((pending["status"], pending["operation"]), ("pending", "rollback"))
        self.assertEqual(self._paper_counts(), {"versions": 2, "audits": 2})

        recovered = risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now)
        self.assertEqual(recovered, [rollback_id])
        self.assertEqual(
            self.adaptive.execute("SELECT status FROM adaptive_risk_candidates WHERE id=?", (self.candidate_id,)).fetchone()[0],
            "rolled_back",
        )
        self.assertEqual(
            self.adaptive.execute("SELECT status FROM adaptive_risk_outbox WHERE candidate_id=?", (rollback_id,)).fetchone()[0],
            "applied",
        )
        self.assertEqual(self._paper_counts(), {"versions": 2, "audits": 2})
        self.assertEqual(
            self.adaptive.execute(
                "SELECT COUNT(*) FROM adaptive_risk_events WHERE candidate_id=? AND event='rolled_back'",
                (self.candidate_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now), [])

    def test_human_loosening_flag_survives_replay(self):
        """An explicit human apply remains replayable without auto-widening."""
        baseline = dict(risk.BASE_RISK["tq_breakout"])
        baseline.update(risk.DOWNSIDE_BASE["tq_breakout"])
        proposed = dict(baseline)
        proposed["max_weight"] = 0.16  # loosening: only human apply may use it
        self.adaptive.execute(
            """INSERT INTO adaptive_risk_candidates(
               run_date,account_id,regime,baseline_params,candidate_params,evidence,
               risk_reduction_pct,change_kind,status,application_mode,reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "2026-08-26", "tq_breakout", "balanced",
                json.dumps(baseline), json.dumps(proposed), json.dumps({}),
                0.0, "includes_loosening", "human_review_required", "human", "test",
                "2026-08-26T09:00:00", "2026-08-26T09:00:00",
            ),
        )
        self.adaptive.commit()
        candidate_id = self.adaptive.execute(
            "SELECT id FROM adaptive_risk_candidates WHERE run_date='2026-08-26'"
        ).fetchone()[0]
        with mock.patch.object(risk, "_record_risk_event_once", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                risk.apply_candidate(
                    self.adaptive, self.paper_path, candidate_id, self.now,
                    approved_by="human-ui", require_conservative=False,
                )
        payload = json.loads(self.adaptive.execute(
            "SELECT payload FROM adaptive_risk_outbox WHERE candidate_id=?", (candidate_id,)
        ).fetchone()[0])
        self.assertFalse(payload["require_conservative"])
        self.assertEqual(risk.replay_pending_outbox(self.adaptive, self.paper_path, self.now), [candidate_id])
        self.assertEqual(
            self.adaptive.execute("SELECT status FROM adaptive_risk_candidates WHERE id=?", (candidate_id,)).fetchone()[0],
            "applied",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
