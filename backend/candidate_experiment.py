"""Pure candidate replay authority. No storage, discovery, clock or provider access.

candidate-replay-v1: factor gates entry only; explicit exit owns exit;
absent exit falls back to inverse entry. Changing these rules requires v2.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Mapping, Any

import experiment_contract as EC
import point_in_time as PIT
import strategy_candidate as SC
import strategy_dsl_evaluator as EVAL

CANDIDATE_REPLAY_CONTRACT_VERSION = "candidate-replay-v1"


class CandidateUniverseUnavailable(ValueError):
    """A declared candidate universe cannot be applied to this historical owner."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CandidateReplayDefinition:
    # Only a verified candidate is accepted. There is no caller-supplied AST field.
    candidate: SC.StrategyCandidate
    replay_contract_version: str = CANDIDATE_REPLAY_CONTRACT_VERSION
    #: Robustness-only derived entry override. ``None`` keeps the canonical candidate
    #: replay identity byte-for-byte; a derived override never changes the candidate
    #: or the candidate's canonical replay fingerprint.
    entry_ast_override: Mapping | None = field(default=None, compare=False)

    def __post_init__(self):
        if self.replay_contract_version != CANDIDATE_REPLAY_CONTRACT_VERSION:
            raise ValueError("unsupported_candidate_replay_contract")
        if not isinstance(self.candidate, SC.StrategyCandidate):
            raise ValueError("canonical_candidate_required")
        candidate = SC.candidate_from_projection(self.candidate.projection())
        if not SC.verify_candidate_fingerprint(candidate):
            raise ValueError("candidate_identity_mismatch")
        EC.StrategyIdentity(candidate.parent_strategy_id, candidate.parent_strategy_version,
                            candidate.parent_strategy_checksum)
        object.__setattr__(self, "candidate", candidate)

    def projection(self):
        """Canonical replay projection. A robustness-derived entry override is NOT
        part of the candidate replay identity: it is recorded separately in the
        scenario evidence as ``derived_entry_fingerprint`` / ``scenario_replay_fingerprint``.
        """
        candidate = self.candidate.projection()
        return {"candidate_id": candidate["candidate_id"],
                "candidate_schema_version": candidate["candidate_schema_version"],
                "entry_ast": candidate["entry_spec"], "factor_ast": candidate["factor_spec"],
                "exit_ast": candidate["exit_spec"], "universe_spec": candidate["universe_spec"],
                "constraints": candidate["constraints"],
                "replay_contract_version": self.replay_contract_version}

    @property
    def replay_fingerprint(self):
        return EC._digest(self.projection())

    @property
    def subject(self):
        c = self.candidate
        return EC.CandidateExperimentSubject(
            c.candidate_id, c.candidate_fingerprint, c.candidate_schema_version,
            EC.StrategyIdentity(c.parent_strategy_id, c.parent_strategy_version, c.parent_strategy_checksum),
            self.replay_contract_version, self.replay_fingerprint)

    @property
    def effective_entry_ast(self) -> Mapping:
        """The entry AST actually evaluated: the canonical one unless derived."""
        return self.entry_ast_override if self.entry_ast_override is not None else self.candidate.entry_spec

    @property
    def derived_entry_fingerprint(self) -> str:
        return EC._digest(self.effective_entry_ast)

    @property
    def scenario_replay_fingerprint(self) -> str:
        """Fingerprint of the exact replay a robustness scenario actually runs."""
        return EC._digest({"candidate_replay_fingerprint": self.replay_fingerprint,
                           "entry_ast": self.effective_entry_ast})

    def derive_entry_ast(self, entry_ast: Mapping) -> "CandidateReplayDefinition":
        """Return a derived replay that keeps the same candidate and factor/exit."""
        return replace(self, entry_ast_override=_freeze(entry_ast))

    def signals(self, snapshot: Mapping[str, Any]) -> tuple[bool, bool]:
        p = self.projection()
        entry = EVAL.evaluate(self.effective_entry_ast, snapshot)
        factor = p["factor_ast"] is None or EVAL.evaluate(p["factor_ast"], snapshot)
        exit_signal = (EVAL.evaluate(p["exit_ast"], snapshot)
                       if p["exit_ast"] is not None else not entry)
        return bool(entry and factor), bool(exit_signal)


def compile_candidate_replay(candidate: SC.StrategyCandidate) -> CandidateReplayDefinition:
    return CandidateReplayDefinition(candidate)


def filter_candidate_members(replay: CandidateReplayDefinition, members: Mapping, universe_identity: str):
    """Filter owner-proven historical members; never manufacture membership."""
    scope = replay.candidate.universe_spec
    pinned = scope.get("asof_universe_identity")
    if pinned is not None and pinned != universe_identity:
        raise CandidateUniverseUnavailable("candidate_universe_identity_mismatch")
    if scope["scope_kind"] == "a_share_boards":
        raise CandidateUniverseUnavailable("candidate_universe_board_scope_not_supported")
    if scope["scope_kind"] == "a_share_all":
        return {session: list(rows) for session, rows in members.items()}
    if scope["scope_kind"] != "explicit_symbols":
        raise CandidateUniverseUnavailable("candidate_universe_scope_not_supported")
    symbols = set(scope["symbols"])
    # Archive codes may include exchange suffixes; canonical candidate symbols are six digits.
    return {session: [row for row in rows if str(row["code"]).split(".")[0] in symbols]
            for session, rows in members.items()}


def validate_candidate_asof(candidate, end_date, cutoff):
    instant = PIT.parse_asof(cutoff)
    if (instant is None or end_date > candidate.asof
            or instant.astimezone(PIT.china_tz()).date().isoformat() > candidate.asof):
        raise ValueError("candidate_asof_leakage")


def build_candidate_experiment_spec(candidate, replay, experiment_plan, tradability_fingerprint):
    canonical = compile_candidate_replay(candidate)
    if not isinstance(replay, CandidateReplayDefinition) or replay.subject != canonical.subject:
        raise ValueError("candidate_identity_mismatch")
    validate_candidate_asof(candidate, experiment_plan.end_date, experiment_plan.asof_policy["cutoff"])
    environment = experiment_plan.experiment_environment()
    environment["parameter_set"] = {**environment["parameter_set"],
                                    "experiment_plan_fingerprint": experiment_plan.fingerprint}
    return EC.CandidateExperimentSpec(
        subject=canonical.subject, tradability_fingerprint=tradability_fingerprint,
        **environment)
