# -*- coding: utf-8 -*-
"""Canonical strategy promotion proposals and deterministic evidence policy.

Lifecycle legality lives in ``strategy_lifecycle``. This module only decides
whether exact immutable evidence is sufficient for one requested state edge.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import strategy_lifecycle as SL

POLICY_VERSION = "strategy-promotion-policy-v1"
PROPOSER_TYPES = frozenset({"human", "system", "ai"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: Provenance paths the shadow -> paper evidence relies on. The report must carry
#: its own owner provenance for each of them: a report whose provenance map is
#: absent or degraded is not admissible. This is a presence/ownership check over
#: the owner's own labels — never a threshold, a score, or a comparison of one
#: strategy against another.
REQUIRED_COMPARISON_PROVENANCE = (
    "active.order_lifecycle_columns",
    "active.signal_decision",
    "active.admission_decision",
    "active.execution_evidence",
    "active.runtime_context",
    "challenger.signal",
    "challenger.entry",
    "challenger.execution",
    "comparison.deltas",
    "comparison.coverage",
    "comparison.active_order_lifecycle",
)


class PromotionError(ValueError):
    """Stable rejection from the promotion policy or proposal ledger."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _freeze_json(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_json(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PromotionError("promotion_payload_invalid")


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class PromotionEvidenceBundle:
    r29_run_key: str | None = None
    r30_report_key: str | None = None
    #: Exact identity of the immutable ``ShadowComparisonReport`` that carries the
    #: shadow -> paper evidence. Exactly one identity, the one the owner already
    #: persists (``report_id == report_fingerprint``); no second comparison key is
    #: invented here.
    shadow_comparison_report_id: str | None = None
    future_paper_evidence_ref: str | None = None

    def __post_init__(self):
        for key in ("r29_run_key", "r30_report_key", "shadow_comparison_report_id"):
            value = getattr(self, key)
            if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
                raise PromotionError(f"{key}_invalid")
        value = self.future_paper_evidence_ref
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise PromotionError("future_paper_evidence_ref_invalid")

    @classmethod
    def from_mapping(cls, raw: Mapping | None):
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise PromotionError("promotion_evidence_bundle_invalid")
        allowed = {"r29_run_key", "r30_report_key", "shadow_comparison_report_id",
                   "future_paper_evidence_ref"}
        if set(raw) - allowed:
            raise PromotionError("promotion_evidence_bundle_unknown_field")
        return cls(**dict(raw))

    def projection(self):
        return {"r29_run_key": self.r29_run_key, "r30_report_key": self.r30_report_key,
                "shadow_comparison_report_id": self.shadow_comparison_report_id,
                "future_paper_evidence_ref": self.future_paper_evidence_ref}


@dataclass(frozen=True)
class PromotionProposal:
    proposal_fingerprint: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    from_state: str
    target_state: str
    evidence_bundle: PromotionEvidenceBundle
    proposer_type: str
    proposer_id: str
    rationale: str
    created_at: str
    policy_version: str = POLICY_VERSION

    def projection(self):
        return {"proposal_fingerprint": self.proposal_fingerprint,
                "strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
                "strategy_checksum": self.strategy_checksum, "from_state": self.from_state,
                "target_state": self.target_state,
                "evidence_bundle": self.evidence_bundle.projection(),
                "proposer_type": self.proposer_type, "proposer_id": self.proposer_id,
                "rationale": self.rationale, "created_at": self.created_at,
                "policy_version": self.policy_version}


@dataclass(frozen=True)
class PromotionDecision:
    decision_fingerprint: str
    policy_version: str
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    from_state: str
    target_state: str
    eligible: bool
    blocking_reasons: tuple[str, ...]
    required_evidence: tuple[str, ...]
    satisfied_evidence: tuple[str, ...]
    evidence_fingerprints: Mapping
    evidence_bundle: PromotionEvidenceBundle

    def projection(self):
        return {"decision_fingerprint": self.decision_fingerprint,
                "policy_version": self.policy_version, "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "strategy_checksum": self.strategy_checksum, "from_state": self.from_state,
                "target_state": self.target_state, "eligible": self.eligible,
                "blocking_reasons": list(self.blocking_reasons),
                "required_evidence": list(self.required_evidence),
                "satisfied_evidence": list(self.satisfied_evidence),
                "evidence_fingerprints": _thaw(self.evidence_fingerprints),
                "evidence_bundle": self.evidence_bundle.projection()}


PROMOTION_RULES = {
    ("draft", "candidate"): ("exact_strategy_version", "runtime_ready"),
    ("candidate", "research"): ("exact_strategy_version",),
    ("research", "validated"): ("r29_run_key",),
    ("research", "validation_failed"): ("deterministic_r29_failure_evidence",),
    ("validated", "shadow"): ("r29_run_key", "r30_report_key"),
    ("shadow", "paper"): ("shadow_comparison_report_id",),
    ("paper", "production_sim"): ("future_paper_evidence_ref",),
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS strategy_promotion_proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      proposal_fingerprint TEXT NOT NULL UNIQUE,
      strategy_id TEXT NOT NULL,
      strategy_version INTEGER NOT NULL,
      strategy_checksum TEXT NOT NULL,
      from_state TEXT NOT NULL,
      target_state TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      evidence_json TEXT NOT NULL,
      decision_json TEXT NOT NULL,
      rationale TEXT NOT NULL,
      proposer_type TEXT NOT NULL CHECK(proposer_type IN ('human','system','ai')),
      proposer_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      payload_fingerprint TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_strategy_proposals_identity
      ON strategy_promotion_proposals(strategy_id,strategy_version,id DESC);
    CREATE TRIGGER IF NOT EXISTS strategy_promotion_proposals_no_update
      BEFORE UPDATE ON strategy_promotion_proposals
      BEGIN SELECT RAISE(ABORT,'append-only strategy promotion proposals'); END;
    CREATE TRIGGER IF NOT EXISTS strategy_promotion_proposals_no_delete
      BEFORE DELETE ON strategy_promotion_proposals
      BEGIN SELECT RAISE(ABORT,'append-only strategy promotion proposals'); END;
    """)


def _verify_r29(evidence_conn, run_key: str, identity: Mapping) -> tuple[dict | None, str | None]:
    try:
        import experiment_validation_repository as EVR
        import experiment_validation_runner as R29
        import experiment_contract as EC
        repo = object.__new__(EVR.ExperimentValidationRepository)
        repo.conn = evidence_conn
        run = repo.get_run(run_key=run_key)
    except Exception:
        return None, "r29_evidence_unavailable"
    if run is None:
        return None, "r29_run_not_found"
    if (run.get("validation_status") != "ready"
            or run.get("strategy_id") != identity["strategy_id"]
            or run.get("strategy_version") != identity["strategy_version"]
            or run.get("strategy_checksum") != identity["strategy_checksum"]):
        return None, "r29_identity_or_status_mismatch"
    result = run.get("result")
    if not isinstance(result, Mapping) or result.get("status") != "completed":
        return None, "r29_result_not_completed"
    try:
        # R29 persists ExperimentResult.projection(), whose stable public
        # metric names are intentionally shorter than the Python constructor
        # fields. Reconstruct from that canonical projection when verifying
        # the owner row.
        metrics = result.get("metrics") or {}
        canonical_result = EC.ExperimentResult(
            experiment_fingerprint=run["experiment_fingerprint"], status="completed",
            total_return=metrics.get("return", metrics.get("total_return")),
            max_drawdown=metrics.get("drawdown", metrics.get("max_drawdown")),
            volatility=metrics.get("volatility"), turnover=metrics.get("turnover"),
            trade_count=metrics.get("trade_count"), total_cost=metrics.get("cost", metrics.get("total_cost")),
            exposure=metrics.get("exposure"), capacity_proxy=metrics.get("capacity_proxy"),
            data_coverage=metrics.get("data_coverage"), regime_breakdown=metrics.get("regime_breakdown"),
            result_fingerprint=result.get("result_fingerprint"))
        if result.get("result_fingerprint") != canonical_result.result_fingerprint:
            return None, "r29_result_fingerprint_mismatch"
        validation = run.get("validation_evidence")
        if (not isinstance(validation, Mapping) or validation.get("status") != "ready"
                or validation.get("experiment_fingerprint") != run.get("experiment_fingerprint")):
            return None, "r29_validation_evidence_invalid"
        owner = {"calendar_fingerprint": run.get("calendar_fingerprint"),
                 "universe_archive_fingerprint": run.get("universe_archive_fingerprint"),
                 "tradability_evidence_fingerprint": run.get("tradability_evidence_fingerprint"),
                 "market_archive_fingerprint": run.get("market_archive_fingerprint"),
                 "financial_archive_fingerprint": run.get("financial_archive_fingerprint"),
                 "dataset_fingerprint": run.get("dataset_fingerprint"),
                 "strategy_version": {"strategy_id": run["strategy_id"],
                                      "version": run["strategy_version"],
                                      "checksum": run["strategy_checksum"]},
                 "validation_evidence_fingerprint": _sha(validation)}
        expected = EVR.ExperimentValidationRepository.build_run_key(
            run["experiment_fingerprint"], owner, R29.RUNNER_VERSION)
        if (expected != run_key or run.get("run_key") != run_key
                or run.get("runner_version") != R29.RUNNER_VERSION):
            return None, "r29_run_identity_invalid"
    except Exception:
        return None, "r29_evidence_corrupt"
    return run, None


def _verify_r30(evidence_conn, report_key: str, run_key: str,
                identity: Mapping, result_fingerprint: str) -> tuple[dict | None, str | None]:
    try:
        import robustness_repository as RREP
        repo = object.__new__(RREP.RobustnessRepository)
        repo.conn = evidence_conn
        report = repo.get_report_by_key(report_key)
    except Exception:
        return None, "r30_evidence_unavailable"
    if report is None:
        return None, "r30_report_not_found"
    body = report.get("report")
    baseline = body.get("baseline_identity") if isinstance(body, Mapping) else None
    if (report.get("report_key") != report_key
            or report.get("baseline_run_key") != run_key
            or not isinstance(baseline, Mapping)
            or baseline.get("run_key") != run_key
            or baseline.get("result_fingerprint") != result_fingerprint
            or report.get("baseline_result_fingerprint") != result_fingerprint
            or baseline.get("strategy_id") != identity["strategy_id"]
            or baseline.get("strategy_version") != identity["strategy_version"]
            or baseline.get("strategy_checksum") != identity["strategy_checksum"]):
        return None, "r30_identity_mismatch"
    cases = body.get("cases") if isinstance(body, Mapping) else None
    if not isinstance(cases, list) or not cases:
        return None, "r30_cases_missing"
    failed = any(not isinstance(case, Mapping)
                 or not isinstance(case.get("result"), Mapping)
                 or case["result"].get("status") == "failed" for case in cases)
    unavailable = any(case["result"].get("status") == "unavailable" for case in cases
                      if isinstance(case, Mapping) and isinstance(case.get("result"), Mapping))
    if failed:
        return None, "r30_failed_case"
    if unavailable:
        return None, "r30_mandatory_case_unavailable"
    return report, None


def _verify_shadow_comparison(conn: sqlite3.Connection, report_id: str,
                              identity: Mapping):
    """Verify one exact ``ShadowComparisonReport`` as the shadow -> paper evidence.

    The comparison report is the single owner of Active/Challenger comparison
    facts, so this policy verifies *that* exact immutable record and nothing else:
    it never searches for a latest comparison, a current challenger, or a most
    recent shadow run. Every check below is a fact about the named report, and an
    unknown vocabulary fails closed instead of being read as success.
    """
    try:
        import shadow_comparison as SC
        import shadow_comparison_repository as SCR
        import shadow_runtime as SRW
    except Exception:
        return None, "shadow_comparison_evidence_unavailable"
    try:
        report = SCR.get_report(conn, report_id)
    except SCR.ShadowComparisonRepositoryError:
        return None, "shadow_comparison_corrupt"
    except sqlite3.OperationalError:
        # No comparison-evidence table in this environment: that is absence.
        return None, "shadow_comparison_report_not_found"
    except Exception:
        return None, "shadow_comparison_corrupt"
    if report is None:
        return None, "shadow_comparison_report_not_found"
    try:
        if (str(report.report_id) != str(report_id)
                or str(report.report_id) != str(report.report_fingerprint)):
            return None, "shadow_comparison_identity_mismatch"
        spec = report.comparison_spec if isinstance(report.comparison_spec, Mapping) else {}
        declared = {key: value for key, value in spec.items()
                    if key != "comparison_scope_identity"}
        if SRW.fingerprint(declared) != str(spec.get("comparison_scope_identity") or ""):
            return None, "shadow_comparison_identity_mismatch"
        stamp = (report.challenger_strategy_stamp
                 if isinstance(report.challenger_strategy_stamp, Mapping) else {})
        if (str(stamp.get("strategy_id") or "") != identity["strategy_id"]
                or int(stamp.get("version") or 0) != identity["strategy_version"]
                or str(stamp.get("checksum") or "") != identity["strategy_checksum"]):
            return None, "shadow_comparison_identity_mismatch"
        declared_challenger = spec.get("challenger")
        if (not isinstance(declared_challenger, Mapping)
                or str(declared_challenger.get("strategy_id") or "") != identity["strategy_id"]):
            return None, "shadow_comparison_identity_mismatch"
        environment = (report.environment_identity
                       if isinstance(report.environment_identity, Mapping) else {})
        shared = environment.get("shared_environment_fingerprints")
        if (str(environment.get("shared_environment_equality") or "") != "EQUAL"
                or not isinstance(shared, Mapping)
                or not shared.get("shadow") or not shared.get("active")
                or str(shared.get("active")) != str(shared.get("shadow"))):
            return None, "shadow_comparison_environment_mismatch"
        availability = str(report.availability or "")
        states = {SC.ComparisonAvailability.AVAILABLE.value,
                  SC.ComparisonAvailability.PARTIAL.value,
                  SC.ComparisonAvailability.UNAVAILABLE.value}
        if availability not in states:
            return None, "shadow_comparison_corrupt"
        if availability != SC.ComparisonAvailability.AVAILABLE.value:
            return None, ("shadow_comparison_partial"
                          if availability == SC.ComparisonAvailability.PARTIAL.value
                          else "shadow_comparison_unavailable")
        if tuple(report.blocking_reasons or ()):
            return None, "shadow_comparison_blocking_reasons"
        coverage = report.coverage if isinstance(report.coverage, Mapping) else {}
        if tuple(coverage.get("blocking_reasons") or ()):
            return None, "shadow_comparison_blocking_reasons"
        expected = coverage.get("expected_observations")
        if (isinstance(expected, bool) or not isinstance(expected, int) or expected < 1
                or coverage.get("available_observations") != expected):
            return None, "shadow_comparison_coverage_incomplete"
        provenance = report.provenance if isinstance(report.provenance, Mapping) else {}
        admissible = {SC.EvidenceProvenance.OWNER_ISSUED.value,
                      SC.EvidenceProvenance.CAPTURED_INPUT.value,
                      SC.EvidenceProvenance.DERIVED.value}
        for path in REQUIRED_COMPARISON_PROVENANCE:
            if str(provenance.get(path) or "") not in admissible:
                return None, "shadow_comparison_provenance_incomplete"
    except Exception:
        return None, "shadow_comparison_corrupt"
    return report, None


def evaluate(conn: sqlite3.Connection, *, strategy_id: str, strategy_version: int,
             strategy_checksum: str, from_state: str, target_state: str,
             evidence_bundle: PromotionEvidenceBundle | Mapping | None = None,
             evidence_conn: sqlite3.Connection | None = None) -> PromotionDecision:
    bundle = (evidence_bundle if isinstance(evidence_bundle, PromotionEvidenceBundle)
              else PromotionEvidenceBundle.from_mapping(evidence_bundle))
    identity = {"strategy_id": str(strategy_id), "strategy_version": int(strategy_version),
                "strategy_checksum": str(strategy_checksum)}
    required = list(PROMOTION_RULES.get((from_state, target_state), ()))
    reasons: list[str] = []
    satisfied: list[str] = []
    fingerprints: dict[str, str] = {}
    if target_state not in SL.TRANSITION_TABLE.get(from_state, ()):
        reasons.append("lifecycle_edge_not_legal")
    if from_state == "paused" and target_state in SL.RESUME_TRANSITION_TARGETS:
        reasons.append("explicit_resume_required")
    version = conn.execute("""SELECT v.checksum,h.current_version,h.current_checksum
        FROM paper_strategy_versions v JOIN paper_strategy_version_heads h USING(strategy_id)
        WHERE v.strategy_id=? AND v.version=?""",
        (identity["strategy_id"], identity["strategy_version"])).fetchone()
    if version is None:
        reasons.append("strategy_version_not_found")
    elif version[0] != identity["strategy_checksum"]:
        reasons.append("strategy_checksum_mismatch")
    elif int(version[1]) != identity["strategy_version"] or version[2] != identity["strategy_checksum"]:
        reasons.append("strategy_version_changed")
    else:
        satisfied.append("exact_strategy_version")
    current = SL.get_state(conn, identity["strategy_id"], identity["strategy_version"],
                           checksum=identity["strategy_checksum"])
    if current is None or current.get("state") != from_state:
        reasons.append("strategy_lifecycle_conflict")
    if (from_state, target_state) == ("draft", "candidate"):
        try:
            import strategy_registry as SR
            readiness = SR.runtime_readiness(conn, identity["strategy_id"])
            if readiness.get("runtime_ready"):
                satisfied.append("runtime_ready")
                fingerprints["runtime_readiness"] = _sha(readiness.get("checks") or {})
            else:
                reasons.append("strategy_runtime_not_ready")
        except Exception:
            reasons.append("strategy_runtime_readiness_unavailable")
    elif (from_state, target_state) == ("candidate", "research"):
        pass
    elif (from_state, target_state) == ("research", "validation_failed"):
        reasons.append("deterministic_validation_failure_owner_unavailable")
    elif target_state == "rejected":
        reasons.append("explicit_rejection_intent_required")
    elif (from_state, target_state) == ("research", "validated"):
        if not bundle.r29_run_key:
            reasons.append("exact_r29_run_key_required")
        elif evidence_conn is None:
            reasons.append("r29_evidence_unavailable")
        else:
            run, reason = _verify_r29(evidence_conn, bundle.r29_run_key, identity)
            if reason:
                reasons.append(reason)
            else:
                satisfied.append("r29_run_key")
                fingerprints["r29_result_fingerprint"] = run["result"]["result_fingerprint"]
                fingerprints["r29_validation_payload"] = run["payload_fingerprint"]
    elif (from_state, target_state) == ("validated", "shadow"):
        if not bundle.r29_run_key:
            reasons.append("exact_r29_run_key_required")
        elif evidence_conn is None:
            reasons.append("r29_evidence_unavailable")
        else:
            run, reason = _verify_r29(evidence_conn, bundle.r29_run_key, identity)
            if reason:
                reasons.append(reason)
            else:
                satisfied.append("r29_run_key")
                fingerprints["r29_result_fingerprint"] = run["result"]["result_fingerprint"]
                if not bundle.r30_report_key:
                    reasons.append("exact_r30_report_key_required")
                else:
                    report, reason = _verify_r30(evidence_conn, bundle.r30_report_key,
                                                 bundle.r29_run_key, identity,
                                                 run["result"]["result_fingerprint"])
                    if reason:
                        reasons.append(reason)
                    else:
                        satisfied.append("r30_report_key")
                        fingerprints["r30_report_fingerprint"] = report["report_fingerprint"]
    elif (from_state, target_state) == ("shadow", "paper"):
        # shadow -> paper consumes exactly one immutable ShadowComparisonReport.
        # The report is read from ``conn``: its append-only owner table lives in
        # this same paper database (created by the paper schema migrations), while
        # the R29/R30 evidence lives in the separate evidence connection.
        if not bundle.shadow_comparison_report_id:
            reasons.append("shadow_comparison_report_required")
        else:
            report, reason = _verify_shadow_comparison(
                conn, bundle.shadow_comparison_report_id, identity)
            if reason:
                reasons.append(reason)
            else:
                satisfied.append("shadow_comparison_report_id")
                fingerprints["shadow_comparison_report_fingerprint"] = str(
                    report.report_fingerprint)
                fingerprints["shadow_comparison_scope_identity"] = str(
                    (report.comparison_spec or {}).get("comparison_scope_identity") or "")
                fingerprints["shadow_run_fingerprint"] = str(report.shadow_run_fingerprint)
    elif (from_state, target_state) == ("paper", "production_sim"):
        # paper -> production_sim still has no evidence owner. Filling
        # ``future_paper_evidence_ref`` with comparison evidence would fabricate a
        # fact this policy cannot verify, so the edge stays blocked.
        reasons.append("paper_runtime_evidence_owner_unavailable")
    decision_material = {"policy_version": POLICY_VERSION, **identity,
                         "from_state": from_state, "target_state": target_state,
                         "evidence_bundle": bundle.projection(),
                         "evidence_fingerprints": fingerprints,
                         "eligible": not reasons, "blocking_reasons": sorted(set(reasons)),
                         "required_evidence": required,
                         "satisfied_evidence": sorted(set(satisfied))}
    return PromotionDecision(
        decision_fingerprint=_sha(decision_material), policy_version=POLICY_VERSION,
        strategy_id=identity["strategy_id"], strategy_version=identity["strategy_version"],
        strategy_checksum=identity["strategy_checksum"], from_state=from_state,
        target_state=target_state, eligible=not reasons,
        blocking_reasons=tuple(sorted(set(reasons))), required_evidence=tuple(required),
        satisfied_evidence=tuple(sorted(set(satisfied))),
        evidence_fingerprints=MappingProxyType(dict(fingerprints)), evidence_bundle=bundle)


def _proposal_payload(*, strategy_id, strategy_version, strategy_checksum, from_state,
                      target_state, evidence_bundle, proposer_type, proposer_id, rationale,
                      policy_version=POLICY_VERSION):
    return {"strategy_id": str(strategy_id), "strategy_version": int(strategy_version),
            "strategy_checksum": str(strategy_checksum), "from_state": str(from_state),
            "target_state": str(target_state), "evidence_bundle": evidence_bundle.projection(),
            "proposer_type": str(proposer_type), "proposer_id": str(proposer_id or ""),
            "rationale": str(rationale or ""), "policy_version": str(policy_version)}


def create_proposal(conn: sqlite3.Connection, *, strategy_id: str, strategy_version: int,
                    strategy_checksum: str, from_state: str, target_state: str,
                    evidence_bundle: PromotionEvidenceBundle | Mapping | None,
                    proposer_type: str, proposer_id: str, rationale: str = "",
                    evidence_conn: sqlite3.Connection | None = None,
                    created_at: str | None = None) -> dict:
    if proposer_type not in PROPOSER_TYPES or not str(proposer_id or "").strip():
        raise PromotionError("promotion_proposer_invalid")
    bundle = (evidence_bundle if isinstance(evidence_bundle, PromotionEvidenceBundle)
              else PromotionEvidenceBundle.from_mapping(evidence_bundle))
    payload = _proposal_payload(strategy_id=strategy_id, strategy_version=strategy_version,
        strategy_checksum=strategy_checksum, from_state=from_state, target_state=target_state,
        evidence_bundle=bundle, proposer_type=proposer_type, proposer_id=proposer_id,
        rationale=rationale)
    fingerprint_material = {key: payload[key] for key in (
        "strategy_id", "strategy_version", "strategy_checksum", "from_state", "target_state",
        "evidence_bundle", "proposer_type", "proposer_id", "policy_version")}
    proposal_fingerprint = _sha(fingerprint_material)
    decision = evaluate(conn, strategy_id=strategy_id, strategy_version=strategy_version,
        strategy_checksum=strategy_checksum, from_state=from_state, target_state=target_state,
        evidence_bundle=bundle, evidence_conn=evidence_conn)
    created_at = created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    # The creation time is audit metadata, not part of the proposal payload's
    # semantics. Retries with identical inputs stay idempotent at a later time.
    stored = {**payload, "proposal_fingerprint": proposal_fingerprint,
              "decision": decision.projection()}
    payload_fingerprint = _sha(stored)
    ensure_schema(conn)
    prior = conn.execute("SELECT payload_fingerprint FROM strategy_promotion_proposals WHERE proposal_fingerprint=?",
                         (proposal_fingerprint,)).fetchone()
    if prior is not None:
        if prior[0] != payload_fingerprint:
            raise PromotionError("promotion_proposal_fingerprint_conflict")
        return get_proposal(conn, proposal_fingerprint)
    try:
        conn.execute("""INSERT INTO strategy_promotion_proposals
            (proposal_fingerprint,strategy_id,strategy_version,strategy_checksum,from_state,target_state,
             policy_version,evidence_json,decision_json,rationale,proposer_type,proposer_id,created_at,
             payload_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (proposal_fingerprint, str(strategy_id), int(strategy_version), str(strategy_checksum),
             from_state, target_state, POLICY_VERSION, _canonical(bundle.projection()),
             _canonical(decision.projection()), str(rationale or ""), proposer_type,
             str(proposer_id), created_at, payload_fingerprint))
    except sqlite3.IntegrityError as exc:
        raise PromotionError("promotion_proposal_conflict") from exc
    return get_proposal(conn, proposal_fingerprint)


def get_proposal(conn: sqlite3.Connection, fingerprint: str) -> dict | None:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM strategy_promotion_proposals WHERE proposal_fingerprint=?",
                       (str(fingerprint),)).fetchone()
    if row is None:
        return None
    item = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(
        ("id", "proposal_fingerprint", "strategy_id", "strategy_version", "strategy_checksum",
         "from_state", "target_state", "policy_version", "evidence_json", "decision_json",
         "rationale", "proposer_type", "proposer_id", "created_at", "payload_fingerprint"),
        row, strict=True))
    try:
        evidence = json.loads(item.pop("evidence_json"))
        decision = json.loads(item.pop("decision_json"))
    except (TypeError, ValueError) as exc:
        raise PromotionError("corrupt_promotion_proposal") from exc
    item["evidence_bundle"] = evidence
    item["decision"] = decision
    payload = {key: item[key] for key in (
        "strategy_id", "strategy_version", "strategy_checksum", "from_state", "target_state",
        "proposer_type", "proposer_id", "rationale", "policy_version")}
    payload["evidence_bundle"] = evidence
    stored = {**payload, "proposal_fingerprint": item["proposal_fingerprint"],
              "decision": decision}
    if _sha(stored) != item["payload_fingerprint"]:
        raise PromotionError("corrupt_promotion_proposal")
    expected_fp = _sha({key: payload[key] for key in (
        "strategy_id", "strategy_version", "strategy_checksum", "from_state", "target_state",
        "evidence_bundle", "proposer_type", "proposer_id", "policy_version")})
    if expected_fp != item["proposal_fingerprint"]:
        raise PromotionError("corrupt_promotion_proposal")
    return item


def list_proposals(conn: sqlite3.Connection, strategy_id: str, *, version: int | None = None,
                   limit: int = 50) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise PromotionError("invalid_promotion_proposal_limit")
    ensure_schema(conn)
    sql = "SELECT proposal_fingerprint FROM strategy_promotion_proposals WHERE strategy_id=?"
    params: list[object] = [str(strategy_id)]
    if version is not None:
        sql += " AND strategy_version=?"; params.append(int(version))
    sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    return [item for row in conn.execute(sql, params).fetchall()
            if (item := get_proposal(conn, row[0])) is not None]


def apply_proposal(paper_conn: sqlite3.Connection, evidence_conn: sqlite3.Connection | None,
                   proposal: Mapping, *, actor_type: str, actor_id: str) -> dict:
    if actor_type == "ai":
        raise PromotionError("ai_cannot_apply_transition")
    if actor_type not in {"human", "system"}:
        raise PromotionError("lifecycle_actor_invalid")
    bundle = PromotionEvidenceBundle.from_mapping(proposal.get("evidence_bundle"))
    decision = evaluate(paper_conn, strategy_id=proposal["strategy_id"],
        strategy_version=int(proposal["strategy_version"]),
        strategy_checksum=proposal["strategy_checksum"], from_state=proposal["from_state"],
        target_state=proposal["target_state"], evidence_bundle=bundle,
        evidence_conn=evidence_conn)
    original = proposal.get("decision") or {}
    if (not decision.eligible or decision.decision_fingerprint != original.get("decision_fingerprint")):
        raise PromotionError("promotion_proposal_stale_or_blocked")
    result = SL.transition(paper_conn, strategy_id=proposal["strategy_id"],
        strategy_version=int(proposal["strategy_version"]),
        strategy_checksum=proposal["strategy_checksum"], expected_state=proposal["from_state"],
        target_state=proposal["target_state"], actor_type=actor_type, actor_id=actor_id,
        transition_kind="promotion", promotion_decision=decision.projection(),
        evidence={"proposal_fingerprint": proposal["proposal_fingerprint"],
                  "evidence_bundle": bundle.projection(),
                  "evidence_fingerprints": _thaw(decision.evidence_fingerprints)})
    return {"lifecycle": result, "decision": decision.projection(),
            "proposal_fingerprint": proposal["proposal_fingerprint"]}
