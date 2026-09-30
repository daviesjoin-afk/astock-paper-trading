"""Append-only persistence for canonical ShadowComparisonReport evidence.

The repository appends one immutable report and reads one explicitly named
report back. It has no latest/list operation, and it never touches anything
except its own table.
"""
from __future__ import annotations

import json
import sqlite3

import shadow_comparison as SC
import shadow_runtime as SR


class ShadowComparisonRepositoryError(ValueError):
    pass


def _material(report: SC.ShadowComparisonReport) -> dict:
    """The exact material the report fingerprint covers.

    The projection carries the whole contract; the two identity keys are
    derived from it, exactly as the pure builder does.
    """
    material = report.projection()
    material.pop("report_id", None)
    material.pop("report_fingerprint", None)
    return material


def append_report(conn: sqlite3.Connection,
                  report: SC.ShadowComparisonReport) -> SC.ShadowComparisonReport:
    """Append one report idempotently; only touches ``shadow_comparison_reports``."""
    if not isinstance(report, SC.ShadowComparisonReport):
        raise TypeError("canonical comparison report evidence is required")
    material = _material(report)
    if (report.report_id != report.report_fingerprint
            or SR.fingerprint(material) != report.report_fingerprint):
        raise ShadowComparisonRepositoryError("comparison_report_fingerprint_mismatch")
    spec = report.comparison_spec
    active, challenger = report.active_strategy_stamp, report.challenger_strategy_stamp
    conn.execute(
        """INSERT OR IGNORE INTO shadow_comparison_reports
           (report_id,report_fingerprint,shadow_run_id,shadow_run_fingerprint,
            active_strategy_id,active_strategy_version,active_strategy_checksum,
            challenger_strategy_id,challenger_strategy_version,challenger_strategy_checksum,
            environment_fingerprint,session_date,decision_at,availability,
            coverage_ratio,evidence_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (report.report_id, report.report_fingerprint, report.shadow_run_id,
         report.shadow_run_fingerprint,
         active["strategy_id"], int(active["version"]), active["checksum"],
         challenger["strategy_id"], int(challenger["version"]), challenger["checksum"],
         spec["environment_fingerprint"], spec["session_date"], spec["decision_at"],
         report.availability, float(report.coverage["coverage_ratio"]),
         SR.canonical_json(material)),
    )
    row = conn.execute(
        "SELECT evidence_json FROM shadow_comparison_reports WHERE report_id=?",
        (report.report_id,),
    ).fetchone()
    if row is None or str(row[0]) != SR.canonical_json(material):
        raise ShadowComparisonRepositoryError("comparison_report_idempotency_conflict")
    return report


def get_report(conn: sqlite3.Connection,
               report_id: str) -> SC.ShadowComparisonReport | None:
    """Read only the exact report ID supplied by the caller."""
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise ShadowComparisonRepositoryError("explicit_comparison_report_id_required")
    row = conn.execute(
        "SELECT report_id,report_fingerprint,evidence_json"
        " FROM shadow_comparison_reports WHERE report_id=?",
        (report_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        material = json.loads(row[2])
    except (TypeError, ValueError) as exc:
        raise ShadowComparisonRepositoryError("comparison_report_evidence_invalid") from exc
    if SR.fingerprint(material) != str(row[1]) or str(row[0]) != str(row[1]):
        raise ShadowComparisonRepositoryError("comparison_report_fingerprint_mismatch")
    return SC.ShadowComparisonReport(
        report_id=str(row[0]),
        report_fingerprint=str(row[1]),
        schema_version=material["schema_version"],
        comparison_spec=material["comparison_spec"],
        active_evidence=material["active_evidence"],
        active_order_lifecycle=material["active_order_lifecycle"],
        shadow_run_id=material["shadow_run_id"],
        shadow_run_fingerprint=material["shadow_run_fingerprint"],
        environment_identity=material["environment_identity"],
        active_strategy_stamp=material["active_strategy_stamp"],
        challenger_strategy_stamp=material["challenger_strategy_stamp"],
        availability=material["availability"],
        coverage=material["coverage"],
        observations=tuple(material["observations"]),
        signal_delta=material["signal_delta"],
        decision_delta=material["decision_delta"],
        turnover=material["turnover"],
        execution=material["execution"],
        risk_rejection=material["risk_rejection"],
        performance=material["performance"],
        provenance=material["provenance"],
        blocking_reasons=tuple(material["blocking_reasons"]),
    )
