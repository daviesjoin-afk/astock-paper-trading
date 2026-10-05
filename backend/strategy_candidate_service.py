# -*- coding: utf-8 -*-
"""Application boundary for strategy candidate generation（R35-A）.

这一层只做四件事，而且顺序是固定的：

1. **exact read**：按调用方给出的 exact ``strategy_version`` + ``strategy_checksum``
   读 registry 的 immutable version。没有 head 兜底、没有 ``MAX(version)``、
   没有 "latest"；checksum 不符由 registry 自己 fail closed。
2. **pin**：把读到的定义冻结成 :class:`ParentStrategyPin`。pin 之后，父策略将来
   升级到 v2 也不会改变这个候选绑定的 v1。
3. **generate**：把 pin 与其它显式事实交给纯生成域
   （``strategy_generator.generate_parameter_variants``）。
4. **append**：把候选幂等追加进台账，并把本次提案的来源证据单独追加。

它**不**写 lifecycle、**不**产生 promotion 结论、**不**下单、**不**碰正式账本，
也没有 apply/execute 路径：生成出来的东西永远是 ``StrategyCandidate``。
"""
from __future__ import annotations

import sqlite3

import strategy_candidate as SC
import strategy_candidate_repository as SCRepo
import strategy_generator as SG
import strategy_registry as SR


class StrategyCandidateUnavailable(ValueError):
    """The requested exact parent version or candidate evidence is unavailable."""


def pin_parent_strategy(conn: sqlite3.Connection, *, strategy_id: str,
                        strategy_version: int, strategy_checksum: str,
                        asof: str, research_provenance=None, universe_spec=None,
                        intended_market_regime=None, constraints=None) -> SG.ParentStrategyPin:
    """Read one **exact** immutable strategy version and freeze it as a pin.

    ``strategy_registry.get_version`` raises when the exact version exists but its
    checksum differs, and returns ``None`` when the version does not exist. Both
    mean the same thing here: we cannot establish the parent identity, so we fail
    closed instead of falling back to the current head.
    """
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise StrategyCandidateUnavailable("explicit_parent_strategy_id_required")
    if isinstance(strategy_version, bool) or not isinstance(strategy_version, int) \
            or strategy_version < 1:
        raise StrategyCandidateUnavailable("explicit_parent_strategy_version_required")
    if not isinstance(strategy_checksum, str) or not SC._SHA256.fullmatch(strategy_checksum):
        raise StrategyCandidateUnavailable("explicit_parent_strategy_checksum_required")
    try:
        record = SR.get_version(str(strategy_id).strip(), int(strategy_version),
                                checksum=str(strategy_checksum), conn=conn)
    except (ValueError, sqlite3.Error) as exc:
        raise StrategyCandidateUnavailable("exact_parent_strategy_version_unavailable") from exc
    if record is None:
        raise StrategyCandidateUnavailable("exact_parent_strategy_version_unavailable")
    definition = dict(record.definition)
    dsl_ast = definition.get("dsl_ast")
    if dsl_ast is None:
        # 没有声明式 DSL 的父策略（native built-in）不能作为受约束生成的基础：
        # 生成域只接受 bounded DSL，绝不为 native 实现发明第二套表达式语言。
        raise StrategyCandidateUnavailable("parent_strategy_has_no_declarative_dsl")
    metadata = dict(definition.get("metadata") or {})
    return SG.ParentStrategyPin(
        strategy_id=str(record.strategy_id), strategy_version=int(record.version),
        strategy_checksum=str(record.checksum), dsl_ast=dsl_ast, asof=asof,
        research_provenance=(research_provenance
                             if research_provenance is not None
                             else metadata.get("research_provenance")),
        universe_spec=(universe_spec if universe_spec is not None
                       else metadata.get("universe_spec")),
        intended_market_regime=(intended_market_regime
                                if intended_market_regime is not None
                                else metadata.get("intended_market_regime")),
        # 调用方没给约束时**继承父策略那一版自己的约束**，而不是变成空集：
        # 空集不是"无约束"，而是"把父策略的仓位/敞口/权重上限悄悄丢掉"，
        # 那会让下游实验读到一份并非父策略语义的候选。
        constraints=(constraints if constraints is not None
                     else metadata.get("constraints")),
        exit_spec=metadata.get("exit_spec"),
        factor_spec=metadata.get("factor_spec"),
    )


def generate_and_record_candidates(
    conn: sqlite3.Connection, *, strategy_id: str, strategy_version: int,
    strategy_checksum: str, asof: str, parameter_adjustments,
    universe_spec, intended_market_regime: str,
    evidence_count: int | None = None, hypothesis_id: str | None = None,
    research_provenance=None, random_seed: int | None = None,
    model_identity=None, constraints=None, created_at: str | None = None,
) -> dict:
    """Generate constrained candidates from one exact pinned parent and record them.

    The caller owns the SQLite connection and transaction. Returns a projection of
    what was produced plus the proposal identity, and nothing about strategy
    quality: this service has no opinion on whether a candidate is any good.
    """
    pin = pin_parent_strategy(
        conn, strategy_id=strategy_id, strategy_version=strategy_version,
        strategy_checksum=strategy_checksum, asof=asof,
        research_provenance=research_provenance, universe_spec=universe_spec,
        intended_market_regime=intended_market_regime, constraints=constraints)
    if pin.universe_spec is None:
        raise StrategyCandidateUnavailable("explicit_universe_spec_required")
    if pin.intended_market_regime is None:
        raise StrategyCandidateUnavailable("explicit_intended_market_regime_required")
    generator_input = SG.GeneratorInput(
        parent_pin=pin, parameter_adjustments=parameter_adjustments,
        universe_spec=pin.universe_spec, intended_market_regime=pin.intended_market_regime,
        asof=asof, evidence_count=evidence_count, hypothesis_id=hypothesis_id,
        research_provenance=pin.research_provenance, random_seed=random_seed,
        model_identity=model_identity, constraints=pin.constraints)
    candidates = SG.generate_parameter_variants(generator_input)
    recorded = []
    for candidate in candidates:
        SCRepo.append_candidate(conn, candidate, created_at=created_at)
        SCRepo.record_proposal(conn, candidate,
                               input_fingerprint=generator_input.input_fingerprint,
                               created_at=created_at)
        recorded.append(candidate.projection())
    return {
        "authority": "strategy_candidate_generation",
        "generator_type": generator_input.generator_type,
        "generator_version": generator_input.generator_version,
        "generator_contract_version": SG.GENERATOR_CONTRACT_VERSION,
        "input_fingerprint": generator_input.input_fingerprint,
        "parent_strategy_pin": pin.identity,
        "asof": pin.asof,
        "candidate_count": len(recorded),
        "candidate_ids": [item["candidate_id"] for item in recorded],
        "candidates": recorded,
    }


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict:
    """Read exactly one candidate by id; never "the latest one"."""
    try:
        candidate = SCRepo.get_candidate(conn, str(candidate_id or ""))
    except (SCRepo.StrategyCandidateRepositoryError, sqlite3.Error) as exc:
        raise StrategyCandidateUnavailable("candidate_evidence_unavailable") from exc
    if candidate is None:
        raise StrategyCandidateUnavailable("strategy_candidate_not_found")
    try:
        proposals = SCRepo.list_proposals(conn, candidate.candidate_id)
    except (SCRepo.StrategyCandidateRepositoryError, sqlite3.Error) as exc:
        raise StrategyCandidateUnavailable("candidate_evidence_unavailable") from exc
    projection = candidate.projection()
    return {
        "authority": "read_only_exact_candidate_projection",
        "candidate": projection,
        # 持久化元数据（created_at）不属于 candidate identity，因此单独作为台账
        # 事实发布：前端要显示"什么时候记的"时读它，而不是读指纹材料。
        "persistence": SCRepo.get_candidate_persistence(conn, candidate.candidate_id),
        # 只读事实：candidate 台账里没有评估结论，因此状态就是 CANDIDATE 本身。
        "status": "CANDIDATE",
        "parent_strategy_pin": candidate.identity(),
        "proposals": list(proposals),
        # 前端**不得**自行判断候选是否优秀 / 能否晋级 / 是否允许进 Shadow；
        # 这些结论以后由 backend contract 返回，R35-A 明确不发布它们。
        "evaluation": None,
        "promotion": None,
    }


def list_candidates_for_parent(conn: sqlite3.Connection, *, strategy_id: str,
                               strategy_version: int,
                               strategy_checksum: str) -> dict:
    """List candidates pinned to one exact parent version (append order)."""
    rows = conn.execute(
        """SELECT candidate_id FROM strategy_candidates
            WHERE parent_strategy_id=? AND parent_strategy_version=?
              AND parent_strategy_checksum=?
            ORDER BY created_at,candidate_id""",
        (str(strategy_id), int(strategy_version), str(strategy_checksum)),
    ).fetchall()
    items = []
    for row in rows:
        candidate_id = str(row[0])
        read = get_candidate(conn, candidate_id)
        items.append({"candidate": read["candidate"], "persistence": read["persistence"]})
    return {
        "authority": "read_only_exact_parent_candidates",
        "parent_strategy_pin": {"strategy_id": str(strategy_id),
                                "strategy_version": int(strategy_version),
                                "strategy_checksum": str(strategy_checksum)},
        "items": items,
    }


def _with_paper_connection(work, *, immediate: bool = False):
    """Connection ownership for the HTTP entry points only.

    The core functions above stay connection-explicit so they can be tested and
    composed; only these public entry points open a connection (same pattern as
    the other evidence owners in this repository).
    """
    import paper_trading as PT
    PT.init_db()
    with PT._db(immediate=immediate) as conn:
        return work(conn)


def capture_strategy_candidates(strategy_id: str, **kwargs) -> dict:
    """Generate candidates from one exact pinned parent and append them."""
    def _work(conn):
        return generate_and_record_candidates(conn, strategy_id=strategy_id, **kwargs)
    return _with_paper_connection(_work, immediate=True)


def read_strategy_candidate(candidate_id: str) -> dict:
    """Read exactly one candidate by id; never "the latest one"."""
    def _work(conn):
        return get_candidate(conn, candidate_id)
    return _with_paper_connection(_work)


def read_parent_candidates(strategy_id: str, *, strategy_version: int,
                           strategy_checksum: str) -> dict:
    def _work(conn):
        return list_candidates_for_parent(
            conn, strategy_id=strategy_id, strategy_version=strategy_version,
            strategy_checksum=strategy_checksum)
    return _with_paper_connection(_work)


__all__ = ["StrategyCandidateUnavailable", "capture_strategy_candidates",
           "generate_and_record_candidates", "get_candidate",
           "list_candidates_for_parent", "pin_parent_strategy",
           "read_parent_candidates", "read_strategy_candidate"]
