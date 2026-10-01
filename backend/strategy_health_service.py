# -*- coding: utf-8 -*-
"""Application boundary for exact strategy health capture（R33-A）.

这一层只做三件事：

1. **exact read**：按调用方给出的 exact ``strategy_version`` + ``strategy_checksum``
   读 immutable version，按**显式** observation window 读 owner 证据；
2. 把每个维度交给它的 owner（registry 的 runtime readiness、lifecycle、execution
   的核验结论列、risk 的 write-time provenance、comparison 的 exact report）；
3. 交给纯 builder :func:`strategy_health.build_strategy_health` 组装并 append。

它**不**写 lifecycle、不写正式账本、不产生任何由证据到结论的映射，
也**不**做 latest / current 查找：窗口与身份都必须由调用方显式给出。
"""
from __future__ import annotations

import json

import execution_verification as EV
import paper_schema_migrations as PSM
import paper_trading as PT
import shadow_comparison_repository as SCR
import strategy_health as SH
import strategy_health_repository as SHRepo
import strategy_lifecycle as SL
import strategy_registry as SR

#: 窗口解释：``[observation_start, observation_end)`` 的**业务日**边界，
#: 落在行自己的 ``created_at``（owner 列）上。仓库既有的日粒度查询同样用
#: ``substr(created_at,1,10)``（见 ``execution_verification``），这里保持同一口径。
_WINDOW_CLAUSE = "substr(created_at,1,10) >= ? AND substr(created_at,1,10) < ?"

#: 行上没有 owner 盖章的核验结论时，如实记这一档（不是 unknown，也不是未验证）。
NOT_STAMPED = "not_stamped"


def _order_rows(conn, identity, window):
    params = (*identity, window.observation_start, window.observation_end)
    return conn.execute(
        "SELECT id,cycle_id,status,qty,filled_qty,execution_status,execution_verified,"
        "execution_evidence_source,created_at FROM paper_orders"
        " WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=?"
        f" AND {_WINDOW_CLAUSE} ORDER BY id",
        params,
    ).fetchall()


def _verified_order_count(conn, identity, window) -> int:
    # 唯一谓词必须来自 owner（``execution_verification.VERIFIED_PREDICATE``）。
    return int(conn.execute(
        "SELECT COUNT(*) FROM paper_orders"
        " WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=?"
        f" AND {_WINDOW_CLAUSE} AND " + EV.VERIFIED_PREDICATE,
        (*identity, window.observation_start, window.observation_end),
    ).fetchone()[0])


def _fill_counts(conn, identity, window) -> tuple[int, int]:
    """Fills reachable only through their own order (the owner linkage)."""
    rows = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT f.order_id) FROM paper_fills f"
        " JOIN paper_orders o ON o.id=f.order_id"
        " WHERE o.strategy_id=? AND o.strategy_version=? AND o.strategy_checksum=?"
        f" AND {_WINDOW_CLAUSE.replace('created_at', 'o.created_at')}",
        (*identity, window.observation_start, window.observation_end),
    ).fetchone()
    return int(rows[0] or 0), int(rows[1] or 0)


def _runtime_dimension(conn, identity, version_row):
    readiness = SR.runtime_readiness(conn, identity[0], version=identity[1],
                                     checksum=identity[2])
    checks = dict(readiness.get("checks") or {})
    status = (SH.STATUS_AVAILABLE if readiness.get("version") is not None
              else SH.STATUS_UNAVAILABLE)
    facts = {
        "runtime_ready": bool(readiness.get("runtime_ready")),
        "checks": checks,
        "compile_errors": list(readiness.get("errors") or []),
        "exact_version": {"strategy_id": readiness.get("strategy_id"),
                          "version": readiness.get("version"),
                          "checksum": readiness.get("checksum")},
    }
    if status == SH.STATUS_UNAVAILABLE:
        return SH.unavailable_dimension(SH.DIMENSION_RUNTIME_INTEGRITY,
                                        SH.REASON_EXACT_VERSION_NOT_PERSISTED, facts=facts)
    return SH.HealthDimension(
        name=SH.DIMENSION_RUNTIME_INTEGRITY, status=status, facts=facts,
        provenance=SH.PROVENANCE_OWNER_ISSUED,
        source_identity=f"strategy_registry.runtime_readiness:{identity[0]}@{identity[1]}",
        source_fingerprint=SH.fingerprint(checks),
    )


def _lifecycle_dimension(conn, identity):
    state = SL.get_state(conn, identity[0], identity[1], checksum=identity[2])
    events = SL.history(conn, identity[0], identity[1])
    if state is None:
        return SH.unavailable_dimension(
            SH.DIMENSION_LIFECYCLE_INTEGRITY, SH.REASON_LIFECYCLE_STATE_UNKNOWN,
            facts={"lifecycle_events": len(events)})
    current = str(state.get("state"))
    facts = {
        "lifecycle_state": current,
        "formal_cycle_allowed": bool(SL.allows_formal_cycle(current)),
        "lifecycle_events": len(events),
        "last_event_fingerprint": (events[-1].get("event_fingerprint") if events else None),
        "last_transition": ({"from_state": events[-1].get("from_state"),
                             "to_state": events[-1].get("to_state"),
                             "transition_kind": events[-1].get("transition_kind"),
                             "actor_type": events[-1].get("actor_type")} if events else None),
        "promotion_decision_fingerprint": (events[-1].get("promotion_decision_fingerprint")
                                           if events else None),
    }
    return SH.HealthDimension(
        name=SH.DIMENSION_LIFECYCLE_INTEGRITY, status=SH.STATUS_AVAILABLE, facts=facts,
        provenance=SH.PROVENANCE_OWNER_ISSUED,
        source_identity=f"strategy_lifecycle.state:{identity[0]}@{identity[1]}",
        source_fingerprint=SH.fingerprint(facts),
    )


def _execution_dimension(conn, identity, window):
    rows = _order_rows(conn, identity, window)
    status_counts: dict[str, int] = {}
    evidence_sources: dict[str, int] = {}
    for row in rows:
        state = row[5] if row[5] is not None else NOT_STAMPED
        status_counts[str(state)] = status_counts.get(str(state), 0) + 1
        source = row[7] if row[7] is not None else NOT_STAMPED
        evidence_sources[str(source)] = evidence_sources.get(str(source), 0) + 1
    fills, filled_orders = _fill_counts(conn, identity, window)
    facts = {
        "orders_in_window": len(rows),
        "verified_orders": _verified_order_count(conn, identity, window),
        "fill_rows": fills,
        "orders_with_fill_rows": filled_orders,
        # 分档用 owner 自己的词表（execution_verification.EXECUTION_STATUSES /
        # EVIDENCE_SOURCES）；未盖章的行单独记 not_stamped，不折算成 unknown。
        "owner_execution_status": status_counts,
        "owner_evidence_source": evidence_sources,
        "owner_status_vocabulary": list(EV.EXECUTION_STATUSES),
        "owner_evidence_source_vocabulary": list(EV.EVIDENCE_SOURCES),
    }
    return SH.HealthDimension(
        name=SH.DIMENSION_EXECUTION_EVIDENCE, status=SH.STATUS_AVAILABLE, facts=facts,
        provenance=SH.PROVENANCE_OWNER_ISSUED,
        source_identity="paper_orders+paper_fills:" + "|".join(
            [identity[0], str(identity[1]), identity[2], window.observation_start,
             window.observation_end]),
        source_fingerprint=SH.fingerprint(facts),
    )


def _risk_dimension(conn, identity, window):
    rows = conn.execute(
        "SELECT id,decision,order_id,payload,created_at FROM paper_risk_decisions"
        " WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=?"
        f" AND {_WINDOW_CLAUSE} ORDER BY id",
        (*identity, window.observation_start, window.observation_end),
    ).fetchall()
    by_authority: dict[str, int] = {authority: 0 for authority in PSM.RISK_DECISION_AUTHORITIES}
    undeclared = 0
    unrecognized = 0
    linked = 0
    for row in rows:
        if row[2] is not None:
            linked += 1
        try:
            payload = json.loads(str(row[3] or "{}"))
        except ValueError:
            payload = {}
        declared = payload.get("decision_provenance") if isinstance(payload, dict) else None
        authority = declared.get("authority") if isinstance(declared, dict) else None
        if authority is None:
            undeclared += 1
        elif authority in by_authority:
            by_authority[str(authority)] += 1
        else:
            unrecognized += 1
    facts = {
        "risk_decision_rows": len(rows),
        "order_linked_rows": linked,
        # 只有 authority == "RISK" 的行才是 Risk Authority 自己的结论；ENTRY /
        # EXECUTION / ALLOCATION / TIMING 不得被统计成风险事件。
        "owner_issued_risk_rows": by_authority["RISK"],
        "rows_by_declared_authority": by_authority,
        "rows_without_declared_authority": undeclared,
        "rows_with_unrecognized_authority": unrecognized,
        "authority_vocabulary": list(PSM.RISK_DECISION_AUTHORITIES),
        "risk_events_are_not_inferable_from_absence": True,
    }
    return SH.HealthDimension(
        name=SH.DIMENSION_RISK_EVIDENCE, status=SH.STATUS_AVAILABLE, facts=facts,
        provenance=SH.PROVENANCE_OWNER_ISSUED,
        source_identity="paper_risk_decisions:" + "|".join(
            [identity[0], str(identity[1]), identity[2], window.observation_start,
             window.observation_end]),
        source_fingerprint=SH.fingerprint(facts),
    )


def _activity_dimension(conn, identity, window):
    pinned = int(conn.execute(
        "SELECT COUNT(DISTINCT cycle_id) FROM paper_cycle_strategy_versions"
        " WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=?",
        identity,
    ).fetchone()[0])
    orders = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT cycle_id) FROM paper_orders"
        " WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=?"
        f" AND {_WINDOW_CLAUSE} AND cycle_id IS NOT NULL",
        (*identity, window.observation_start, window.observation_end),
    ).fetchone()
    fills, _filled_orders = _fill_counts(conn, identity, window)
    facts = {
        "pinned_cycles": pinned,
        "observed_cycles": int(orders[1] or 0),
        "orders_in_window": int(orders[0] or 0),
        "fill_rows": fills,
        # paper_signals 没有 strategy stamp，而 cycle pin 是 (cycle_id, account_id)：
        # 无法把一条 signal 归属到 exact version，因此如实记不可用。
        "signals": None,
        "zero_activity_is_not_unhealthy": True,
    }
    return SH.HealthDimension(
        name=SH.DIMENSION_ACTIVITY_COVERAGE, status=SH.STATUS_PARTIAL, facts=facts,
        provenance=SH.PROVENANCE_OWNER_ISSUED,
        source_identity="paper_cycle_strategy_versions+paper_orders:" + "|".join(
            [identity[0], str(identity[1]), identity[2]]),
        source_fingerprint=SH.fingerprint(facts),
        blocking_reasons=(SH.REASON_SIGNAL_ATTRIBUTION_UNAVAILABLE,),
    )


def _performance_dimension():
    # 仓库没有「exact strategy version 历史绩效（NAV/return/drawdown/PnL）」的 owner：
    # paper_performance 是日内持仓 P&L，paper_portfolio_read_model 明确不发布
    # nav/market_value/return。缺 owner 就只能 UNAVAILABLE，绝不拿日内数据升级成
    # 策略历史绩效权威。
    return SH.unavailable_dimension(
        SH.DIMENSION_PERFORMANCE, SH.REASON_STRATEGY_PERFORMANCE_OWNER_UNAVAILABLE,
        facts={"strategy_version_scoped_owner": None})


def _comparable_dimension(conn, identity, comparison_report_id):
    if not comparison_report_id:
        return SH.not_applicable_dimension(SH.DIMENSION_COMPARABLE_EVIDENCE,
                                           SH.REASON_COMPARISON_REPORT_NOT_SPECIFIED)
    report = SCR.get_report(conn, comparison_report_id)
    if report is None:
        raise SH.HealthEvidenceError("exact_comparison_report_unavailable")
    stamp = dict(report.challenger_strategy_stamp or {})
    if (str(stamp.get("strategy_id") or "") != identity[0]
            or int(stamp.get("version") or 0) != identity[1]
            or str(stamp.get("checksum") or "") != identity[2]):
        raise SH.HealthEvidenceError("comparison_report_identity_mismatch")
    facts = {
        "report_id": str(report.report_id),
        "availability": str(report.availability),
        "coverage": dict(report.coverage or {}),
        "blocking_reasons": [str(item) for item in report.blocking_reasons],
        "provenance": dict(report.provenance or {}),
    }
    return SH.HealthDimension(
        name=SH.DIMENSION_COMPARABLE_EVIDENCE, status=SH.STATUS_AVAILABLE, facts=facts,
        provenance=SH.PROVENANCE_OWNER_ISSUED,
        source_identity=f"shadow_comparison.report:{report.report_id}",
        source_fingerprint=str(report.report_fingerprint),
    )


def _capture(conn, *, strategy_id, strategy_version, strategy_checksum,
             observation_start, observation_end, comparison_report_id, lifecycle_state):
    window = SH.HealthObservationWindow(observation_start=observation_start,
                                        observation_end=observation_end)
    identity = (str(strategy_id), int(strategy_version), str(strategy_checksum))
    try:
        version_row = SR.get_version(identity[0], identity[1], checksum=identity[2], conn=conn)
    except ValueError as exc:
        # registry 对「exact version 存在但 checksum 不符」是 raise，不是返回 None：
        # 两种情况下我们都无法建立这个 exact version 的事实，因此统一 fail closed。
        raise SH.HealthEvidenceError("exact_strategy_version_not_persisted") from exc
    if version_row is None:
        # exact version 必须存在：否则「这个 version 的健康事实」无从谈起。
        raise SH.HealthEvidenceError("exact_strategy_version_not_persisted")
    dimensions = (
        _runtime_dimension(conn, identity, version_row),
        _lifecycle_dimension(conn, identity),
        _execution_dimension(conn, identity, window),
        _risk_dimension(conn, identity, window),
        _activity_dimension(conn, identity, window),
        _performance_dimension(),
        _comparable_dimension(conn, identity, comparison_report_id),
    )
    return SH.build_strategy_health(
        strategy_id=identity[0], strategy_version=identity[1], strategy_checksum=identity[2],
        observation_window=window, lifecycle_state=lifecycle_state, dimensions=dimensions,
        source_identities={
            "exact_strategy_version": f"{identity[0]}@{identity[1]}#{identity[2]}",
            "observation_window": window.identity,
            "comparison_report_id": comparison_report_id or None,
        },
    )


def _with_paper_connection(work, *, immediate: bool = False):
    PT.init_db()
    with PT._db(immediate=immediate) as conn:
        return work(conn)


def capture_strategy_health(strategy_id: str, *, strategy_version: int,
                            strategy_checksum: str, observation_start: str,
                            observation_end: str,
                            comparison_report_id: str | None = None) -> dict:
    """Capture one exact health snapshot and append it. Evidence only."""
    def _work(conn):
        state = SL.get_state(conn, str(strategy_id), int(strategy_version),
                             checksum=str(strategy_checksum))
        snapshot = _capture(
            conn, strategy_id=strategy_id, strategy_version=strategy_version,
            strategy_checksum=strategy_checksum, observation_start=observation_start,
            observation_end=observation_end, comparison_report_id=comparison_report_id,
            lifecycle_state=(state or {}).get("state"))
        appended = SHRepo.append_snapshot(conn, snapshot)
        return appended.projection()
    return _with_paper_connection(_work, immediate=True)


def get_health_snapshot(strategy_id: str, snapshot_id: str) -> dict:
    """Read exactly one snapshot by id; never "the latest one"."""
    def _work(conn):
        snapshot = SHRepo.get_snapshot(conn, snapshot_id)
        if snapshot is None:
            raise SH.HealthEvidenceError("health_snapshot_not_found")
        if snapshot.strategy_id != str(strategy_id):
            # 页面身份与快照身份必须一致，否则就是把 A 的证据显示成 B 的。
            raise SH.HealthEvidenceError("health_snapshot_strategy_mismatch")
        return snapshot.projection()
    return _with_paper_connection(_work)


__all__ = ["capture_strategy_health", "get_health_snapshot", "NOT_STAMPED"]
