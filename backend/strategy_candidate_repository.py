# -*- coding: utf-8 -*-
"""Append-only persistence for immutable ``StrategyCandidate`` records（R35-A）.

The candidate is **evidence, not state**: this repository offers
``append_candidate()`` and an exact ``get_candidate(candidate_id)`` only. There is
deliberately no ``get_latest_candidate``, no ``get_current_candidate``, no
overwrite and no delete — the ledger is append-oriented, so "把旧候选改成新策略"
在接口层就不存在。

Same ``candidate_id`` with different content is a **conflict**, never a silent
overwrite. Schema creation belongs to the formal migration owner
(``paper_schema_migrations`` / ``db_migrate``), never to this repository.

Dedup authority: a repeated proposal of the same canonical candidate is
idempotent on ``candidate_id``. The original proposal's source evidence is
preserved — re-proposals are recorded in ``strategy_candidate_proposals`` rather
than replacing the candidate row, so去重不会丢掉"谁、什么时候、为什么又提了一次"。
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import sqlite3

import strategy_candidate as SC


class StrategyCandidateRepositoryError(ValueError):
    pass


#: 同一进程内的提案事件序号。提案是**事件**，不是内容的函数：同一秒（甚至同一
#: 微秒）内对同一候选、同一输入提出两次，是两条独立的历史记录，必须各自有身份。
#: 只按内容 + 秒级时间戳取 id 会让第二条被 ``INSERT OR IGNORE`` 静默吞掉，
#: 从而违反"append-only 台账记录每一次提案"的契约。
_PROPOSAL_SEQUENCE = itertools.count(1)


def _proposal_event_identity(created_at: str | None) -> tuple[str, str]:
    """Return ``(iso_timestamp_with_microseconds, process_unique_sequence)``."""
    if created_at is None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    else:
        stamp = str(created_at)
    return stamp, str(next(_PROPOSAL_SEQUENCE))


def _payload(candidate: SC.StrategyCandidate) -> str:
    return json.dumps(candidate.projection(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _proposal_payload(*, generator_type: str, generator_version: str, asof: str,
                      hypothesis_id: str | None, research_provenance,
                      input_fingerprint: str, random_seed: int | None,
                      model_identity) -> str:
    material = {
        "generator_type": generator_type,
        "generator_version": generator_version,
        "asof": asof,
        "hypothesis_id": hypothesis_id,
        "research_provenance": dict(research_provenance or {}),
        "input_fingerprint": input_fingerprint,
        "random_seed": random_seed,
        "model_identity": dict(model_identity or {}),
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def append_candidate(conn: sqlite3.Connection, candidate: SC.StrategyCandidate, *,
                     created_at: str | None = None) -> SC.StrategyCandidate:
    """Append one candidate idempotently; only touches the candidate tables.

    ``created_at`` is persistence metadata and is deliberately **not** part of the
    canonical fingerprint: two identical specifications are the same candidate no
    matter when each was proposed.
    """
    if not isinstance(candidate, SC.StrategyCandidate):
        raise TypeError("canonical strategy candidate is required")
    if not SC.verify_candidate_fingerprint(candidate):
        raise StrategyCandidateRepositoryError("candidate_fingerprint_mismatch")
    payload = _payload(candidate)
    conn.execute(
        """INSERT OR IGNORE INTO strategy_candidates
           (candidate_id,candidate_fingerprint,candidate_contract_version,
            candidate_schema_version,parent_strategy_id,parent_strategy_version,
            parent_strategy_checksum,generator_type,generator_version,
            generator_contract_version,hypothesis_id,asof,random_seed,
            candidate_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (candidate.candidate_id, candidate.candidate_fingerprint,
         SC.CANDIDATE_CONTRACT_VERSION, candidate.candidate_schema_version,
         candidate.parent_strategy_id, candidate.parent_strategy_version,
         candidate.parent_strategy_checksum, candidate.generator_type,
         candidate.generator_version, candidate.generator_contract_version,
         candidate.hypothesis_id, candidate.asof, candidate.random_seed, payload,
         created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()),
    )
    row = conn.execute(
        "SELECT candidate_json,candidate_fingerprint FROM strategy_candidates"
        " WHERE candidate_id=?", (candidate.candidate_id,)).fetchone()
    if (row is None or str(row[0]) != payload
            or str(row[1]) != candidate.candidate_fingerprint):
        # Same identity, different content is a conflict, never a silent overwrite.
        raise StrategyCandidateRepositoryError("candidate_idempotency_conflict")
    return candidate


def record_proposal(conn: sqlite3.Connection, candidate: SC.StrategyCandidate, *,
                    input_fingerprint: str, created_at: str | None = None) -> str:
    """Record one proposal event for an already-persisted candidate.

    This is how dedup keeps its evidence: the candidate row is written once, but
    every proposal (generator, as-of, hypothesis, model, input fingerprint) is
    appended. Returns the proposal id.
    """
    if not isinstance(candidate, SC.StrategyCandidate):
        raise TypeError("canonical strategy candidate is required")
    if not isinstance(input_fingerprint, str) or not SC._SHA256.fullmatch(input_fingerprint):
        raise StrategyCandidateRepositoryError("proposal_input_fingerprint_invalid")
    payload = _proposal_payload(
        generator_type=candidate.generator_type, generator_version=candidate.generator_version,
        asof=candidate.asof, hypothesis_id=candidate.hypothesis_id,
        research_provenance=candidate.research_provenance,
        input_fingerprint=input_fingerprint, random_seed=candidate.random_seed,
        model_identity=candidate.model_identity)
    stamp, sequence = _proposal_event_identity(created_at)
    proposal_id = SC._sha({"candidate_id": candidate.candidate_id,
                           "proposal": json.loads(payload), "created_at": stamp,
                           "event_sequence": sequence})
    conn.execute(
        """INSERT OR IGNORE INTO strategy_candidate_proposals
           (proposal_id,candidate_id,input_fingerprint,proposal_json,created_at)
           VALUES(?,?,?,?,?)""",
        (proposal_id, candidate.candidate_id, input_fingerprint, payload, stamp),
    )
    row = conn.execute(
        "SELECT candidate_id,proposal_json FROM strategy_candidate_proposals"
        " WHERE proposal_id=?", (proposal_id,)).fetchone()
    if row is None or str(row[0]) != candidate.candidate_id or str(row[1]) != payload:
        raise StrategyCandidateRepositoryError("candidate_proposal_idempotency_conflict")
    return proposal_id


def get_candidate(conn: sqlite3.Connection,
                  candidate_id: str) -> SC.StrategyCandidate | None:
    """Read only the exact candidate named by its fingerprint ID.

    There is no latest/current lookup: the caller must already know which
    candidate it is asking about. A stored row that no longer re-derives its own
    fingerprint is corruption and fails closed rather than becoming "a different
    candidate".
    """
    if not isinstance(candidate_id, str) or len(candidate_id) != 64:
        raise StrategyCandidateRepositoryError("explicit_candidate_id_required")
    row = conn.execute(
        "SELECT candidate_id,candidate_fingerprint,candidate_json"
        " FROM strategy_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    if row is None:
        return None
    try:
        candidate = SC.candidate_from_projection(json.loads(str(row[2])))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise StrategyCandidateRepositoryError("candidate_evidence_invalid") from exc
    if (candidate.candidate_id != str(row[0])
            or candidate.candidate_fingerprint != str(row[1])
            or not SC.verify_candidate_fingerprint(candidate)):
        raise StrategyCandidateRepositoryError("candidate_fingerprint_mismatch")
    return candidate


def get_candidate_persistence(conn: sqlite3.Connection, candidate_id: str) -> dict:
    """Read the persistence metadata of one exact candidate.

    ``created_at`` is deliberately **not** part of the canonical candidate
    fingerprint (two identical specifications are the same candidate no matter
    when each was proposed), so it cannot travel inside ``StrategyCandidate``.
    It is still a real, displayable fact about the ledger row, and this is where
    it is read — from the persistence owner, not from the contract.
    """
    if not isinstance(candidate_id, str) or len(candidate_id) != 64:
        raise StrategyCandidateRepositoryError("explicit_candidate_id_required")
    row = conn.execute(
        "SELECT created_at FROM strategy_candidates WHERE candidate_id=?",
        (candidate_id,)).fetchone()
    if row is None:
        return {}
    return {"created_at": str(row[0])}


def list_proposals(conn: sqlite3.Connection, candidate_id: str) -> tuple[dict, ...]:
    """Read the proposal history of one exact candidate (oldest first)."""
    if not isinstance(candidate_id, str) or len(candidate_id) != 64:
        raise StrategyCandidateRepositoryError("explicit_candidate_id_required")
    rows = conn.execute(
        "SELECT proposal_id,candidate_id,input_fingerprint,proposal_json,created_at"
        " FROM strategy_candidate_proposals WHERE candidate_id=? ORDER BY created_at,proposal_id",
        (candidate_id,)).fetchall()
    history = []
    for row in rows:
        try:
            material = json.loads(str(row[3]))
        except ValueError as exc:
            raise StrategyCandidateRepositoryError("candidate_proposal_evidence_invalid") from exc
        history.append({"proposal_id": str(row[0]), "candidate_id": str(row[1]),
                        "input_fingerprint": str(row[2]),
                        "created_at": str(row[4]), **material})
    return tuple(history)


__all__ = ["StrategyCandidateRepositoryError", "append_candidate", "get_candidate",
           "get_candidate_persistence", "list_proposals", "record_proposal"]
