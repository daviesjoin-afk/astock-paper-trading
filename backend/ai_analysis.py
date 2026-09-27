# -*- coding: utf-8 -*-
"""Time-window orchestration over the canonical typed research lifecycle.

``adaptive_ai_analysis_runs`` remains an operational timeline projection.  The
research conclusion, its evidence references, provider execution, and durable
research authority live only in ``ai_research_runs``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
LEASE_SECONDS = 20 * 60
WINDOWS = {
    "premarket", "auction", "open-confirm", "morning", "noon", "afternoon",
    "risk-review", "close-risk", "close", "adversarial", "manual",
}
SCOPES = {"all", "market", "sector", "holdings"}


def _now():
    """Operational timestamps only; never an evidence as-of source."""
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _analysis_business_key(asof_day, window, scope, targets):
    normalized_targets = tuple(sorted(
        (str(account_id).strip(), int(cycle_id)) for account_id, cycle_id in targets
    ))
    target_identity = (
        "none" if not normalized_targets else
        hashlib.sha256(_json(normalized_targets).encode("utf-8")).hexdigest()
    )
    return f"ai:{asof_day}:{window}:{scope}:targets:{target_identity}", target_identity


def ensure_schema(conn):
    """Create the operational API timeline; canonical research remains in its own ledger."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adaptive_ai_analysis_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_key TEXT NOT NULL UNIQUE,
            trade_date TEXT NOT NULL,
            analysis_window TEXT NOT NULL,
            scope TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            provider TEXT,
            secondary_provider TEXT,
            model TEXT,
            evidence_hash TEXT NOT NULL,
            source_asof TEXT,
            coverage REAL,
            quote_age_seconds REAL,
            deterministic_status TEXT NOT NULL,
            result TEXT,
            secondary_result TEXT,
            error_code TEXT,
            retries INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_analysis_day
            ON adaptive_ai_analysis_runs(trade_date, id DESC);
        """
    )


def _row_dict(cursor, row):
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {item[0]: row[index] for index, item in enumerate(cursor.description or ())}


def _market_event(context):
    """Read a typed R24 fact at the caller-declared time, with no refresh."""
    import ai_research_contract as ARC
    import market_data_contract as MDC
    import market_data_service as MDS

    reading, _metadata = MDS.read_snapshot_with_meta(
        MDC.LIVE_MARKET_POLICY,
        now=context.market_now,
        asof_day=context.asof_day,
    )
    if reading.availability != MDC.AVAILABILITY_AVAILABLE or reading.snapshot is None:
        return reading, None
    ref = ARC.evidence_ref_from_market_reading(reading)
    if ref.as_of > context.asof_day:
        raise ValueError("market owner returned evidence later than the declared as-of day")
    rows = [dict(row) for row in reading.rows() if isinstance(row, dict)]
    event = ARC.InformationEvent(
        as_of=context.asof_day,
        source="market_data_service.read_snapshot_with_meta",
        evidence_ref=ref,
        payload={"rows": rows, "kind": reading.snapshot.kind,
                 "observed_at": reading.snapshot.observed_at},
    )
    return reading, event


def _portfolio_events(paper_db_path, context):
    """Use portfolio/execution owners only when account and cycle are explicit."""
    if not context.targets:
        return ()
    import ai_research_contract as ARC
    import ai_research_portfolio_adapter as PFA
    import paper_portfolio_read_model as PPRM
    from deepseek_research import _execution_leg

    conn = sqlite3.connect(f"file:{paper_db_path}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    events = []
    try:
        for account_id, cycle_id in context.targets:
            portfolio_context = PPRM.PortfolioReadContext(cycle_id, context.asof_day)
            for projection in PPRM.accounting_fact_projections(
                conn, portfolio_context, account_id=account_id,
            ):
                ref = PFA.evidence_ref_from_portfolio_projection(projection)
                raw = projection.projection()
                payload = {
                    key: value for key, value in raw.items()
                    if key not in {"verification", "verification_method", "fact_verification_status",
                                   "authority", "is_authoritative", "is_verified", "outcome"}
                }
                events.append(ARC.InformationEvent(
                    as_of=context.asof_day,
                    source="portfolio_owner.accounting_fact_projections",
                    evidence_ref=ref,
                    payload=payload,
                ))
            execution, _trades, _fees, _complete = _execution_leg(
                conn, account_id, cycle_id, context,
            )
            events.extend(execution)
    finally:
        conn.close()
    return tuple(events)


def deterministic_snapshot(paper_db_path, snapshot_paths, window="manual", scope="all", *, context=None):
    """Presentation projection of R24 facts; no independent freshness/coverage verdict."""
    if context is None:
        raise ValueError("research_asof_context_required")
    reading, event = _market_event(context)
    return {
        "trade_date": context.asof_day,
        "window": str(window),
        "scope": str(scope),
        "snapshot_source": "market_data_authority" if event else None,
        "source_asof": None if reading.snapshot is None else reading.snapshot.as_of,
        "quote_age_seconds": reading.age_seconds,
        "rows": None if reading.snapshot is None else len(reading.snapshot.rows),
        "coverage_pct": None,
        "data_quality": reading.status,
        "market_event": None if event is None else event.projection(),
        "paper": {"availability": "available" if context.targets else "unavailable",
                  "reason": None if context.targets else "portfolio_context_required"},
    }


def _decode_result(raw):
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _canonical_report(connect_factory, run_id):
    import ai_research_repository as repository
    from deepseek_advisor import research_report_view_from_row

    with connect_factory() as conn:
        row = repository.get_run(conn, int(run_id))
    return None if row is None else research_report_view_from_row(row)


def run_analysis(connect_factory, paper_db_path, snapshot_paths, provider_module=None,
                 config=None, trigger="manual-ui", window="manual", scope="all", *, context=None):
    """Orchestrate a canonical research run and save only its operational reference."""
    window = str(window or "manual")[:30]
    scope = str(scope or "all")[:30]
    if window not in WINDOWS:
        raise ValueError("unsupported_analysis_window")
    if scope not in SCOPES:
        raise ValueError("unsupported_analysis_scope")
    if context is None:
        raise ValueError("research_asof_context_required")
    if scope == "holdings" and not context.targets:
        raise ValueError("portfolio_context_required_for_holdings_scope")

    trade_date = context.asof_day
    business_key, target_identity = _analysis_business_key(
        trade_date, window, scope, context.targets,
    )
    with connect_factory() as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            "SELECT * FROM adaptive_ai_analysis_runs WHERE business_key=?", (business_key,),
        )
        existing = cursor.fetchone()
        retries = 0
        if existing:
            row = _row_dict(cursor, existing)
            stored = _decode_result(row.get("result"))
            if row.get("status") == "completed" and stored and stored.get("canonical_run_id"):
                report = _canonical_report(connect_factory, stored["canonical_run_id"])
                if report is not None:
                    return {"status": "idempotent", "run": row, "result": report}
            retries = int(row.get("retries") or 0) + 1
            conn.execute(
                "UPDATE adaptive_ai_analysis_runs SET status='superseded',"
                "error_code='legacy_projection_only',result=NULL,secondary_result=NULL,"
                "updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        created = _now()
        conn.execute(
            "INSERT INTO adaptive_ai_analysis_runs(business_key,trade_date,analysis_window,scope,"
            "trigger,status,deterministic_status,evidence_hash,retries,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(business_key) DO UPDATE SET "
            "trigger=excluded.trigger,status=excluded.status,deterministic_status=excluded.deterministic_status,"
            "evidence_hash=excluded.evidence_hash,retries=excluded.retries,created_at=excluded.created_at,"
            "updated_at=excluded.updated_at,result=NULL,secondary_result=NULL,error_code=NULL",
            (business_key, trade_date, window, scope, str(trigger)[:80], "running", "pending",
             "pending", retries, created, created),
        )

    import ai_research_service as service
    import deepseek_advisor as advisor
    events = []
    try:
        reading, market_event = _market_event(context)
        if market_event is None:
            raise ValueError("market_evidence_unavailable")
        events.append(market_event)
        events.extend(_portfolio_events(paper_db_path, context))
        provider_config = advisor._research_provider_config(connect_factory)
        result = service.run_research_run(
            connect_factory,
            purpose="ai_analysis",
            trigger=str(trigger or "manual-ui")[:120],
            hypothesis_id=f"ai_analysis:{trade_date}:{window}:{scope}:targets:{target_identity}",
            as_of=trade_date,
            subject=f"{window}:{scope}",
            question=("对所给 typed owner evidence 进行时间窗研究；只给研究假设，"
                      "明确暴露证据缺口，不得补造数据或产生交易权限。"),
            events=events,
            provider_config=provider_config,
        )
        report = advisor.research_report_view(result)
        reference = {
            "canonical_run_id": result.run_id,
            "authority": "canonical_research_ledger",
            "presentation_status": report["verdict"],
        }
        run_status = "completed"
        error_code = None
        provider = result.provider_slot
        model = result.provider_model
    except advisor.ResearchReadinessError as exc:
        report = None
        reference = None
        run_status = "blocked"
        error_code = f"research_{exc.reason}"[:80]
        provider = model = None
    except Exception as exc:  # noqa: BLE001 - failure is operational metadata only.
        report = None
        reference = None
        run_status = "failed"
        error_code = type(exc).__name__[:80]
        provider = model = None

    market_ref = events[0].evidence_ref if events else None
    with connect_factory() as conn:
        ensure_schema(conn)
        conn.execute(
            "UPDATE adaptive_ai_analysis_runs SET status=?,provider=?,model=?,"
            "evidence_hash=?,source_asof=?,coverage=NULL,quote_age_seconds=?,"
            "deterministic_status=?,result=?,secondary_result=NULL,error_code=?,"
            "finished_at=?,updated_at=? WHERE business_key=?",
            (run_status, provider, model,
             evidence_hash([event.evidence_ref.projection() for event in events]),
             None if market_ref is None else market_ref.as_of,
             None if not events else reading.age_seconds,
             "unavailable" if market_ref is None else reading.status,
             _json(reference) if reference is not None else None, error_code,
             _now(), _now(), business_key),
        )
    return {
        "status": run_status,
        "window": window,
        "scope": scope,
        "result": report,
        "secondary_result": None,
        "canonical_run_id": None if reference is None else reference["canonical_run_id"],
        "evidence_hash": evidence_hash([event.evidence_ref.projection() for event in events]),
        "error_code": error_code,
    }


def timeline(connect_factory, limit=40, trade_date=None):
    """Read operational rows and expose only canonical run references, never conclusions."""
    day = str(trade_date or dt.datetime.now(TZ).date().isoformat())[:10]

    with connect_factory() as conn:
        ensure_schema(conn)
        cursor = conn.execute(
            "SELECT * FROM adaptive_ai_analysis_runs WHERE trade_date=? ORDER BY id DESC LIMIT ?",
            (day, max(1, min(int(limit), 200))),
        )
        rows = []
        for raw in cursor:
            item = _row_dict(cursor, raw)
            reference = _decode_result(item.get("result"))
            run_id = reference.get("canonical_run_id") if reference else None
            if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
                run_id = None
            item["canonical_run_id"] = run_id
            if run_id is None and item.get("result"):
                # Legacy conclusions remain marked as unavailable; never promote or expose them.
                item["source"] = "legacy_compatibility_history"
            item["result"] = None
            item["secondary_result"] = None
            rows.append(item)
    return {"status": "ok", "trade_date": day, "runs": rows, "windows": rows}
