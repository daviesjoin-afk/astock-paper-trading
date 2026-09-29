"""Application boundary for exact-version, isolated Shadow execution."""
from __future__ import annotations

import sqlite3

import execution_planner as EP
import shadow_run_repository as SRR
import shadow_runtime as SR


def run_shadow(conn: sqlite3.Connection, *, spec: SR.ShadowRunSpec,
               environment: SR.FrozenShadowEnvironment,
               candidates: tuple[SR.ShadowCandidate, ...]) -> SR.ShadowRunEvidence:
    """Resolve exact R31/Registry owners, evaluate, and append one ShadowRun.

    Reads only the explicitly requested immutable version, lifecycle state, and
    optional previous run ID. There is no head/latest/provider fallback.

    The current entry ``ExecutionPolicy`` is resolved exactly once, here, and
    frozen before the pure runtime runs. Shadow evaluation itself therefore has
    no remaining dependency on the owner's current policy state, and the frozen
    fingerprint is recorded in the run evidence.
    """
    version, lifecycle_state = SR.resolve_exact_shadow_strategy(conn, spec.challenger)
    execution_policy = EP.execution_policy_snapshot(spec.challenger.strategy_id)
    previous_run = None
    if spec.previous_shadow_run_id is not None:
        previous_run = SRR.get_run(conn, spec.previous_shadow_run_id)
        if previous_run is None:
            raise SR.ShadowRuntimeError(
                "UNAVAILABLE", "explicit_previous_shadow_run_required",
            )
    evidence = SR.evaluate_shadow(
        spec=spec, environment=environment, strategy_version=version,
        lifecycle_state=lifecycle_state, execution_policy=execution_policy,
        candidates=candidates, previous_run=previous_run,
    )
    return SRR.append_run(conn, evidence)

