# -*- coding: utf-8 -*-
"""HTTP boundary for the auditable self-evolution subsystem."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import datetime as dt
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

import adaptive_engine as adaptive
import adaptive_learning_dispatch as learning_dispatch
import paper_position_read_model as PPRM
import paper_storage as PST
import self_evolution as SE


router = APIRouter(prefix="/api/adaptive", tags=["adaptive-learning"])


def _canonical_build_revision() -> str:
    """Return the repository's single injected build identity."""
    return os.environ.get("ASTOCK_GIT_COMMIT", "")


class ExperimentValidationRequest(BaseModel):
    """Exact identities required to run one offline canonical validation."""
    spec: dict
    market_archive_fingerprint: str = Field(min_length=64, max_length=64)
    universe_archive_fingerprint: str = Field(min_length=64, max_length=64)
    financial_archive_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    calendar_fingerprint: str = Field(min_length=64, max_length=64)
    benchmark_symbol: str = Field(min_length=1, max_length=24)
    walk_forward: dict


class RobustnessRequest(BaseModel):
    """Exact R29 baseline spec, R30 plan, and pinned owner identities."""
    spec: dict
    plan: dict
    owner_identities: dict
    benchmark_symbol: str = Field(min_length=1, max_length=24)
    walk_forward: dict | None = None


@contextmanager
def _experiment_validation_connection(*, query_only: bool):
    """Open only the adaptive DB and the additive validation ledger schema."""
    db_path = adaptive.DB_PATH
    if query_only:
        if not os.path.exists(db_path):
            raise FileNotFoundError("validation ledger unavailable")
        uri = "file:" + os.path.abspath(db_path).replace(os.sep, "/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if query_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            import experiment_validation_repository as repository
            repository.ensure_schema(conn)
            conn.commit()
        yield conn
    finally:
        conn.close()


def _validation_run_projection(row):
    return {key: row.get(key) for key in (
        "id", "run_key", "experiment_fingerprint", "strategy_id", "strategy_version",
        "strategy_checksum", "calendar_fingerprint", "universe_archive_fingerprint",
        "financial_archive_fingerprint", "tradability_evidence_fingerprint",
        "market_archive_fingerprint", "dataset_fingerprint",
        "validation_status", "validation_evidence", "result", "folds", "runner_version",
        "runner_code_revision", "created_at")}


def _robustness_owner_identities(request, baseline):
    expected = {key: baseline.get(key) for key in (
        "calendar_fingerprint", "universe_archive_fingerprint",
        "financial_archive_fingerprint", "tradability_evidence_fingerprint",
        "market_archive_fingerprint", "dataset_fingerprint", "strategy_id",
        "strategy_version", "strategy_checksum")}
    if request.owner_identities != expected:
        raise ValueError("baseline_owner_identity_mismatch")
    return expected


@contextmanager
def _paper_rebalance_db():
    """调仓引擎的读写连接：**必须是 paper ledger**，不是 adaptive DB。

    调仓扫描在同一连接上既读 paper 账本事实（``paper_accounts`` / 当前持仓 lot /
    ``paper_orders`` / ``paper_signals``），又写自己的状态表
    （``rebalance_scans`` / ``rebalance_plans`` / ``rebalance_cooldown``）。
    因此它的事务模型天然是 **paper-ledger coupled**：这些状态一旦与账本事实分开，
    「扫到的持仓」与「据此写下的计划」就不再来自同一个快照。

    旧实现用 ``adaptive._connect()``（= ``adaptive_learning.sqlite3``）。那张库既没有
    ``paper_accounts`` 也没有 ``paper_position_lots``，于是 endpoint 在错误的数据库上
    找 paper 事实 —— 读不到就报 ``no such table``；更糟的是它会在错误库里建出
    rebalance 状态，让 status/plans 与 scan 各说各话（split-brain）。

    刻意**不用** ``PPRM.connect_readonly`` / ``paper_ledger_reader``：它们带
    ``mode=ro`` + ``PRAGMA query_only=ON``，而调仓扫描必须写上述三张状态表。
    也刻意**不** ``ATTACH`` 两个库：跨库事务会引入锁与部分提交语义，而 scanner
    本来就依赖 paper ledger，最小正确模型是「调仓状态与 paper 账本同库」。

    连接归属（Round-11）与周期归属（Round-12）是两件事，必须同时成立：
    调仓状态既属于 paper ledger，也属于**某一个** paper cycle。
    """
    with PST.db(adaptive.PAPER_DB_PATH) as conn:
        yield conn

# ─── 轻量内存缓存 ───
# One cache implementation is enough for the read-only status endpoints.
# Keeping timestamp and value maps separate preserves the existing O(1) access
# path without maintaining a dead second namespace.
_cache = {}
_cache_ts = {}
MAX_CACHE_ENTRIES = 64

def _cache_get(key, ttl=30):
    import time as _t
    if key in _cache and _t.time() - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    return None

def _cache_set(key, value):
    import time as _t
    if key not in _cache and len(_cache) >= MAX_CACHE_ENTRIES:
        oldest_key = min(_cache, key=lambda item: _cache_ts.get(item, 0))
        _cache.pop(oldest_key, None)
        _cache_ts.pop(oldest_key, None)
    _cache[key] = value
    _cache_ts[key] = _t.time()

def _cache_clear(prefix=None):
    if prefix is None:
        _cache.clear()
        _cache_ts.clear()
    else:
        for k in list(_cache.keys()):
            if k.startswith(prefix):
                del _cache[k]
                del _cache_ts[k]


def _quote_metadata(rows):
    """Return auditable freshness/source/coverage metadata for a quote pull."""
    rows = rows if isinstance(rows, list) else []
    codes = {str(row.get("code") or "") for row in rows
             if isinstance(row, dict) and str(row.get("code") or "").strip()}
    quote_times = [str(row.get("quote_at") or row.get("quote_ts") or "")
                   for row in rows if isinstance(row, dict)
                   and (row.get("quote_at") or row.get("quote_ts"))]
    sources = sorted({str(row.get("source") or row.get("source_name") or "")
                      for row in rows if isinstance(row, dict)
                      and (row.get("source") or row.get("source_name"))})
    # The snapshot endpoint does not expose a trusted universe denominator;
    # report measured coverage explicitly instead of inventing a percentage.
    return {
        "quote_asof": max(quote_times) if quote_times else None,
        "source": sources[0] if len(sources) == 1 else ("mixed" if sources else "unknown"),
        "sources": sources,
        "coverage": {"rows": len(rows), "unique_codes": len(codes), "denominator": None},
    }


def _fetch_rebalance_quotes():
    """Fetch quotes before opening the SQLite connection used by a scan."""
    from data_fetcher import fetch_market_snapshot
    rows = fetch_market_snapshot(pages=20, allow_disk_fallback=False)
    metadata = _quote_metadata(rows)
    if metadata["source"] == "unknown" and rows:
        # The adapter currently normalizes rows without a per-row provider
        # field.  Keep the source explicit at the boundary rather than making
        # downstream audit readers infer it from a missing value.
        metadata["source"] = "fetch_market_snapshot"
    if not rows:
        raise HTTPException(status_code=503, detail={
            "status": "quote_unavailable", "quote_meta": metadata,
        })
    return {str(row.get("code")): row for row in rows if isinstance(row, dict) and row.get("code")}, metadata

_MANUAL_ACTOR = "human-ui"
_OVERVIEW_TTL_SECONDS = 60.0
_OVERVIEW_SNAPSHOT_PATH = os.path.join(
    getattr(adaptive, "CACHE_DIR", os.path.join(os.path.dirname(__file__), "data_cache")),
    "adaptive_overview_ui_snapshot.json",
)


def _is_complete_overview(data):
    """Only serve a persisted view if it can render every adaptive section."""
    return isinstance(data, dict) and all(
        isinstance(data.get(key), dict)
        for key in ("engine", "risk_optimizer", "selection_optimizer", "deepseek_advisor", "neural_control")
    )


def _load_overview_snapshot():
    try:
        with open(_OVERVIEW_SNAPSHOT_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if _is_complete_overview(data) else None
    except (OSError, ValueError, TypeError):
        return None


def _store_overview_snapshot(data):
    if not _is_complete_overview(data):
        return
    directory = os.path.dirname(_OVERVIEW_SNAPSHOT_PATH)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="adaptive-overview-", suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, _OVERVIEW_SNAPSHOT_PATH)
    except OSError:
        try:
            if "temporary" in locals() and os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


_persisted_overview = _load_overview_snapshot()
_overview_cache = {"data": _persisted_overview, "ts": 0.0, "running": False, "error": None}
_overview_lock = threading.Lock()


def _schedule_overview_refresh() -> None:
    """Build the costly read model off the request path."""
    with _overview_lock:
        if _overview_cache["running"]:
            return
        _overview_cache.update(running=True, error=None)

    def worker() -> None:
        try:
            # The engine keeps its own coherent short cache.  Forcing a full
            # SQLite/attribution rebuild here defeated the non-blocking API
            # cache and made ordinary tab switches contend with research work.
            data = adaptive.overview(force=False)
            _store_overview_snapshot(data)
            with _overview_lock:
                _overview_cache.update(data=data, ts=time.time(), running=False)
        except Exception as exc:
            with _overview_lock:
                _overview_cache.update(running=False, error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=worker, name="adaptive-overview-refresh", daemon=True).start()


def _require_confirmation(confirmed: bool, action: str):
    """Keep browser-originated state changes explicitly human-confirmed.

    There is intentionally no shared secret in this single-operator paper
    system.  The API boundary still must not let an accidental click or a
    stale UI request mutate the learning ledger without the same confirmation
    affordance used for apply and rollback.
    """
    if not confirmed:
        raise HTTPException(status_code=409, detail=f"请先在页面确认{action}")


@router.get("/overview")
def overview():
    now = time.time()
    with _overview_lock:
        data = _overview_cache["data"]
        fresh = data is not None and now - _overview_cache["ts"] < _OVERVIEW_TTL_SECONDS
        error = _overview_cache["error"]
    if fresh:
        return data
    _schedule_overview_refresh()
    if data is not None:
        # A stale coherent snapshot is much more useful than a blocked page.
        return {**data, "snapshot_stale": True, "refreshing": True}
    return {
        "refreshing": True,
        "snapshot_stale": True,
        "message": "正在后台汇总自进化证据，页面不会阻塞。",
        "refresh_error": error,
    }


@router.get("/ai/settings")
def ai_settings():
    """Return safe AI controls, explanations, and provider capabilities."""
    return adaptive.ai_settings_schema()


@router.post("/ai/settings")
def update_ai_settings(
    llm_advisor_enabled: bool | None = Query(None),
    llm_provider: str | None = Query(None),
    llm_realtime_tuning_enabled: bool | None = Query(None),
    llm_realtime_auto_apply: bool | None = Query(None),
    llm_realtime_require_cross_source: bool | None = Query(None),
    llm_realtime_min_interval_minutes: int | None = Query(None, ge=5, le=240),
    llm_realtime_min_valid_rows: int | None = Query(None, ge=100, le=10000),
    llm_realtime_mode: str | None = Query(None),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "保存AI调参设置")
    updates = {k: v for k, v in {
        "llm_advisor_enabled": llm_advisor_enabled,
        "llm_provider": llm_provider,
        "llm_realtime_tuning_enabled": llm_realtime_tuning_enabled,
        "llm_realtime_auto_apply": llm_realtime_auto_apply,
        "llm_realtime_require_cross_source": llm_realtime_require_cross_source,
        "llm_realtime_min_interval_minutes": llm_realtime_min_interval_minutes,
        "llm_realtime_min_valid_rows": llm_realtime_min_valid_rows,
        "llm_realtime_mode": llm_realtime_mode,
    }.items() if v is not None}
    try:
        return {"settings": adaptive.update_ai_settings(updates)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/ai/overview")
def ai_overview():
    """Fast AI tab payload; full evidence aggregation always runs in background."""
    with _overview_lock:
        data = _overview_cache.get("data")
        refreshing = bool(_overview_cache.get("running"))
    if data is None:
        _schedule_overview_refresh()
        data, refreshing = {}, True
    advisor = data.get("deepseek_advisor") or {}
    tuning = advisor.get("realtime_tuning") or {}
    candidates = (data.get("selection_optimizer") or {}).get("candidates") or []
    ai_candidates = [item for item in candidates if item.get("tier") == "ai_realtime" or "AI" in str(item.get("reason") or "").upper()]
    return {"settings": adaptive.ai_settings(), "parameters": adaptive.AI_SETTINGS_META,
            "providers": __import__("deepseek_advisor").provider_catalog(), "advisor": advisor,
            "realtime_tuning": tuning, "latest_runs": [tuning.get("latest")] if tuning.get("latest") else [],
            "candidates": ai_candidates[:30], "snapshot_stale": not bool(data), "refreshing": refreshing}


@router.get("/ai/timeline")
def ai_timeline(
    limit: int = Query(40, ge=1, le=100),
    trade_date: str | None = Query(None, max_length=10),
):
    """Expose the persisted AI-analysis timeline used by the adaptive page."""
    try:
        return adaptive.ai_analysis_timeline(limit=limit, trade_date=trade_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取AI分析时间线失败：{type(exc).__name__}") from exc


@contextmanager
def _canonical_research_connection():
    """Open the adaptive DB for canonical research reads without engine side effects.

    The general adaptive connection initializes unrelated learning tables and state. A
    history GET must not run that initialization, providers, or orchestration. The only
    permitted setup is the idempotent canonical ledger schema, followed by SQLite's
    query-only mode for the actual repository read.
    """
    db_path = adaptive.DB_PATH
    db_directory = os.path.dirname(os.path.abspath(db_path))
    if db_directory:
        os.makedirs(db_directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        import ai_research_repository as repository

        repository.ensure_schema(conn)
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


@router.get("/research/runs")
def canonical_research_runs(
    limit: int = Query(50, ge=1, le=200),
    purpose: str | None = Query(None, max_length=80),
    as_of: str | None = Query(None, max_length=200),
    subject: str | None = Query(None, max_length=200),
):
    """Return persisted canonical research artifacts in append order, never current truth."""
    import ai_research_repository as repository

    try:
        with _canonical_research_connection() as conn:
            runs = repository.recent_runs(
                conn, limit=limit, purpose=purpose, as_of=as_of, subject=subject,
            )
    except repository.ResearchPersistenceError as exc:
        raise HTTPException(status_code=500, detail=exc.reason) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid_research_filter") from exc
    return {"status": "ok", "runs": runs}


@router.get("/research/runs/{run_id}")
def canonical_research_run(run_id: int = Path(..., gt=0)):
    """Return one validated persisted research artifact; missing rows are 404."""
    import ai_research_repository as repository

    try:
        with _canonical_research_connection() as conn:
            run = repository.get_run(conn, run_id)
    except repository.ResearchPersistenceError as exc:
        raise HTTPException(status_code=500, detail=exc.reason) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="research_run_not_found")
    return {"status": "ok", "run": run}


@router.post("/experiments/validate")
def validate_canonical_experiment(request: ExperimentValidationRequest):
    """Run an exact-identity validation offline; never refresh or select inputs."""
    try:
        import experiment_contract as EC
        import experiment_execution_model  # noqa: F401
        import financial_feature_evidence as FFE
        import experiment_validation_repository as EVR
        import historical_financial_archive as HFA
        import experiment_validation_runner as runner
        import historical_market_archive as HMA
        import historical_session_calendar as HSC
        import historical_universe_archive as HUA
        import learning_dataset as LD
        import strategy_registry as SR
        import tradability_archive as TA
        import walk_forward_validation as WFV

        raw = dict(request.spec)
        strategy = raw.get("strategy")
        if not isinstance(strategy, dict):
            raise ValueError("strategy_identity_required")
        raw["strategy"] = EC.StrategyIdentity(**strategy)
        raw["start_date"] = raw.pop("start_date", raw.get("date_range", {}).get("start"))
        raw["end_date"] = raw.pop("end_date", raw.get("date_range", {}).get("end"))
        raw.pop("date_range", None)
        spec = EC.ExperimentSpec(**raw)
        if (spec.market_data_fingerprint != request.market_archive_fingerprint
                or spec.universe_fingerprint != request.universe_archive_fingerprint
                or spec.parameter_set.get("validation_calendar_fingerprint") != request.calendar_fingerprint
                or spec.parameter_set.get("financial_archive_fingerprint") != request.financial_archive_fingerprint):
            raise ValueError("owner_identity_mismatch")
        config = WFV.WalkForwardConfig(**request.walk_forward)
        with _experiment_validation_connection(query_only=False) as conn:
            market_repo = HMA.HistoricalMarketArchiveRepository(conn)
            financial_archive_repo = HFA.HistoricalFinancialArchiveRepository(conn)
            financial_feature_repo = FFE.FinancialFeatureEvidenceRepository(conn, financial_archive_repo)
            universe_repo = HUA.HistoricalUniverseArchiveRepository(conn)
            tradability_repo = TA.TradabilityArchiveRepository(conn)
            ledger = EVR.ExperimentValidationRepository(conn)
            calendar = HSC.issue_from_market_archive(
                market_repo, archive_fingerprint=request.market_archive_fingerprint,
                benchmark_symbol=request.benchmark_symbol, start=spec.start_date, end=spec.end_date,
            )
            if calendar.calendar_fingerprint != request.calendar_fingerprint:
                raise ValueError("calendar_identity_mismatch")
            LD.ensure_schema(conn)
            manifest = LD.read_manifest(conn, spec.dataset_fingerprint)
            if manifest is None:
                raise ValueError("dataset_identity_unavailable")
            dataset = LD.build_dataset(
                conn, cutoff=manifest["cutoff"], split_spec=manifest["split_spec"],
                feature_names=manifest["feature_names"],
                horizon_semantics=manifest["horizon_semantics"],
                contract_version=manifest["contract_version"],
                financial_feature_repository=financial_feature_repo
                if request.financial_archive_fingerprint else None,
                financial_archive_fingerprint=request.financial_archive_fingerprint,
                persist=False,
            )
            if dataset.fingerprint != spec.dataset_fingerprint:
                raise ValueError("dataset_identity_mismatch")
            samples = [sample for partition in LD.PARTITIONS
                       for sample in dataset.partitions.get(partition, ())]
            strategy_version = SR.get_version(
                spec.strategy.strategy_id, spec.strategy.version,
                checksum=spec.strategy.checksum,
            )
            # The app process injects its build identity. The request cannot claim it.
            build_revision = _canonical_build_revision()
            output = runner.run_validation(
                spec, runner_code_revision=build_revision,
                strategy_version=strategy_version, dataset_manifest=manifest,
                samples=samples, walk_forward_config=config,
                session_calendar=calendar, market_archive_repository=market_repo,
                market_archive_fingerprint=request.market_archive_fingerprint,
                universe_archive_repository=universe_repo,
                universe_archive_fingerprint=request.universe_archive_fingerprint,
                tradability_repository=tradability_repo,
                financial_feature_repository=financial_feature_repo,
                financial_archive_fingerprint=request.financial_archive_fingerprint,
                validation_repository=ledger,
                created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            )
            if output.get("run"):
                output["run"] = _validation_run_projection(output["run"])
            return output
    except (TypeError, ValueError, KeyError) as exc:
        reason = str(exc) if str(exc).isidentifier() else "validation_request_invalid"
        raise HTTPException(status_code=422, detail=reason) from exc
    except Exception as exc:
        # Do not expose SQL, filesystem details, paths, or raw exception text.
        raise HTTPException(status_code=503, detail="validation_inputs_unavailable") from exc


@router.get("/experiments/runs")
def canonical_experiment_runs(
    limit: int = Query(50, ge=1, le=200),
    experiment_fingerprint: str | None = Query(None, min_length=64, max_length=64),
    strategy_id: str | None = Query(None, max_length=80),
):
    import experiment_validation_repository as repository

    try:
        with _experiment_validation_connection(query_only=True) as conn:
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='experiment_validation_runs'").fetchone()
            if not table:
                runs = []
            else:
                repo = object.__new__(repository.ExperimentValidationRepository)
                repo.conn = conn
                runs = repo.recent_runs(limit=limit, experiment_fingerprint=experiment_fingerprint,
                                        strategy_id=strategy_id)
    except repository.ExperimentValidationPersistenceError as exc:
        raise HTTPException(status_code=503, detail=exc.args[0]) from exc
    except FileNotFoundError:
        return {"status": "ok", "runs": []}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="validation_history_unavailable") from exc
    return {"status": "ok", "runs": [_validation_run_projection(item) for item in runs]}


@router.get("/experiments/runs/{run_id}")
def canonical_experiment_run(run_id: str = Path(..., min_length=1, max_length=64)):
    import experiment_validation_repository as repository

    try:
        with _experiment_validation_connection(query_only=True) as conn:
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='experiment_validation_runs'").fetchone()
            if not table:
                run = None
                repo = None
            else:
                repo = object.__new__(repository.ExperimentValidationRepository)
                repo.conn = conn
            if repo is None:
                pass
            elif run_id.isdecimal():
                run = repo.get_run(run_id=int(run_id))
            elif len(run_id) == 64:
                run = repo.get_run(run_key=run_id)
            else:
                raise HTTPException(status_code=422, detail="exact_validation_run_identity_required")
    except repository.ExperimentValidationPersistenceError as exc:
        raise HTTPException(status_code=503, detail=exc.args[0]) from exc
    except FileNotFoundError:
        run = None
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="validation_history_unavailable") from exc
    if run is None:
        raise HTTPException(status_code=404, detail="validation_run_not_found")
    return {"status": "ok", "run": _validation_run_projection(run)}


@router.post("/experiments/runs/{run_id}/robustness")
def create_canonical_robustness_report(request: RobustnessRequest,
                                       run_id: int = Path(..., gt=0)):
    """Run a bounded offline adversarial report against one canonical R29 run."""
    try:
        import experiment_contract as EC
        import experiment_execution_model  # noqa: F401
        import experiment_pit_validation as PV
        import financial_feature_evidence as FFE
        import experiment_validation_repository as EVR
        import historical_financial_archive as HFA
        import historical_market_archive as HMA
        import historical_session_calendar as HSC
        import historical_universe_archive as HUA
        import learning_dataset as LD
        import robustness_contract as RC
        import robustness_repository as RREP
        import robustness_runner as RRUN
        import strategy_dsl_schema as DSL
        import strategy_registry as SR
        import tradability_archive as TA
        import walk_forward_validation as WFV

        raw = dict(request.spec)
        strategy = raw.get("strategy")
        if not isinstance(strategy, dict):
            raise ValueError("strategy_identity_required")
        raw["strategy"] = EC.StrategyIdentity(**strategy)
        date_range = raw.get("date_range") or {}
        raw["start_date"] = raw.pop("start_date", date_range.get("start"))
        raw["end_date"] = raw.pop("end_date", date_range.get("end"))
        raw.pop("date_range", None)
        spec = EC.ExperimentSpec(**raw)
        plan = RC.RobustnessPlan(**request.plan)
        with _experiment_validation_connection(query_only=False) as conn:
            market_repo = HMA.HistoricalMarketArchiveRepository(conn)
            financial_archive_repo = HFA.HistoricalFinancialArchiveRepository(conn)
            financial_feature_repo = FFE.FinancialFeatureEvidenceRepository(conn, financial_archive_repo)
            universe_repo = HUA.HistoricalUniverseArchiveRepository(conn)
            tradability_repo = TA.TradabilityArchiveRepository(conn)
            validation_repo = EVR.ExperimentValidationRepository(conn)
            baseline = validation_repo.get_run(run_id=run_id)
            if baseline is None:
                raise HTTPException(status_code=404, detail="validation_run_not_found")
            expected_owners = _robustness_owner_identities(request, baseline)
            if (spec.market_data_fingerprint != expected_owners["market_archive_fingerprint"]
                    or spec.universe_fingerprint != expected_owners["universe_archive_fingerprint"]
                    or spec.dataset_fingerprint != expected_owners["dataset_fingerprint"]
                    or spec.parameter_set.get("validation_calendar_fingerprint") != expected_owners["calendar_fingerprint"]
                    or spec.parameter_set.get("financial_archive_fingerprint") != expected_owners["financial_archive_fingerprint"]
                    or request.benchmark_symbol != plan.regime_policy["benchmark_symbol"]):
                raise ValueError("baseline_owner_identity_mismatch")
            calendar = HSC.issue_from_market_archive(
                market_repo, archive_fingerprint=spec.market_data_fingerprint,
                benchmark_symbol=request.benchmark_symbol,
                start=spec.start_date, end=spec.end_date)
            if calendar.calendar_fingerprint != expected_owners["calendar_fingerprint"]:
                raise ValueError("baseline_calendar_identity_mismatch")
            manifest = LD.read_manifest(conn, spec.dataset_fingerprint)
            if manifest is None:
                raise ValueError("baseline_dataset_unavailable")
            dataset = LD.build_dataset(
                conn, cutoff=manifest["cutoff"], split_spec=manifest["split_spec"],
                feature_names=manifest["feature_names"],
                horizon_semantics=manifest["horizon_semantics"],
                contract_version=manifest["contract_version"],
                financial_feature_repository=financial_feature_repo
                if expected_owners["financial_archive_fingerprint"] else None,
                financial_archive_fingerprint=expected_owners["financial_archive_fingerprint"],
                persist=False)
            if dataset.fingerprint != spec.dataset_fingerprint:
                raise ValueError("baseline_dataset_identity_mismatch")
            samples = [sample for partition in LD.PARTITIONS
                       for sample in dataset.partitions.get(partition, ())]
            strategy_version = SR.get_version(spec.strategy.strategy_id, spec.strategy.version,
                                              checksum=spec.strategy.checksum)
            ast = DSL.normalize(strategy_version.definition.get("dsl_ast"))
            dependencies = PV.strategy_dsl_dependencies(ast)
            financial_by_pair = {}
            if dependencies["financial_fields"]:
                for sample in samples:
                    sample_key = getattr(sample, "sample_key", None)
                    code = getattr(sample, "code", None)
                    session = (getattr(sample, "feature_asof", None)
                               or getattr(sample, "decision_session", None))
                    if not sample_key or not code or not session:
                        continue
                    for field_name in dependencies["financial_fields"]:
                        item = financial_feature_repo.resolve_for_dataset_sample(
                            dataset_fingerprint=spec.dataset_fingerprint,
                            sample_key=str(sample_key), feature_name=field_name,
                            financial_archive_fingerprint=expected_owners["financial_archive_fingerprint"],
                        ).projection()
                        if item.get("verification") != "proven":
                            continue
                        pair = (str(code), str(session))
                        entry = financial_by_pair.setdefault(pair, {"decision_at": item.get("decision_at")})
                        if entry["decision_at"] != item.get("decision_at"):
                            raise ValueError("financial_feature_decision_mismatch")
                        if field_name in entry and entry[field_name] != item.get("feature_value"):
                            raise ValueError("financial_feature_value_mismatch")
                        entry[field_name] = item.get("feature_value")
            config = WFV.WalkForwardConfig(**request.walk_forward) if request.walk_forward else None
            extended_calendar = None
            date_scenarios = [scenario for scenario in plan.scenarios()
                              if scenario["category"] in {"start_date", "end_date"}]
            if date_scenarios:
                calendar_manifest = market_repo.get_manifest(spec.market_data_fingerprint)
                benchmark_calendar = (calendar_manifest.benchmark_calendars.get(request.benchmark_symbol)
                                      if calendar_manifest is not None else None)
                if not isinstance(benchmark_calendar, dict):
                    raise ValueError("historical_benchmark_calendar_unavailable")
                owner_sessions = list(benchmark_calendar.get("sessions") or ())
                if (spec.start_date not in owner_sessions or spec.end_date not in owner_sessions
                        or owner_sessions != sorted(set(owner_sessions))):
                    raise ValueError("historical_benchmark_calendar_unavailable")
                base_start, base_end = owner_sessions.index(spec.start_date), owner_sessions.index(spec.end_date)
                ranges = []
                for scenario in date_scenarios:
                    params = scenario["parameters"]
                    start_index, end_index = base_start, base_end
                    if scenario["category"] == "start_date":
                        start_index += params["shift_sessions"]
                    else:
                        end_index += params["shift_sessions"]
                    if 0 <= start_index <= end_index < len(owner_sessions):
                        selected = owner_sessions[start_index:end_index + 1]
                        if selected:
                            ranges.append((selected[0], selected[-1]))
                if ranges:
                    coverage_start = min(spec.start_date, *(start for start, _ in ranges))
                    coverage_end = max(spec.end_date, *(end for _, end in ranges))
                    try:
                        extended_calendar = HSC.issue_from_market_archive(
                            market_repo, archive_fingerprint=spec.market_data_fingerprint,
                            benchmark_symbol=request.benchmark_symbol,
                            start=coverage_start, end=coverage_end)
                    except ValueError:
                        # The owner cannot prove this expanded range. R30 records
                        # each affected case as unavailable instead of guessing.
                        extended_calendar = None
            if extended_calendar is not None and extended_calendar.calendar_fingerprint == calendar.calendar_fingerprint:
                extended_calendar = None
            report = RRUN.run_robustness(
                baseline_run=baseline, spec=spec, plan=plan,
                strategy_version=strategy_version, session_calendar=calendar,
                market_archive_repository=market_repo,
                universe_archive_repository=universe_repo,
                tradability_repository=tradability_repo,
                financial_features=financial_by_pair,
                required_financial_fields=dependencies["financial_fields"],
                extended_session_calendar=extended_calendar,
                date_range_validation_context={
                    "benchmark_symbol": request.benchmark_symbol,
                    "walk_forward_config": config,
                    "dataset_manifest": manifest,
                    "samples": samples,
                    "financial_feature_repository": financial_feature_repo
                        if expected_owners["financial_archive_fingerprint"] else None,
                },
                created_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            report["plan_fingerprint"] = plan.fingerprint
            repository = RREP.RobustnessRepository(conn)
            stored = repository.append_report(report)
            return {"status": "ok", "report_id": stored["id"],
                    "report_key": stored["report_key"],
                    "report_fingerprint": stored["report_fingerprint"],
                    "report": stored["report"]}
    except HTTPException:
        raise
    except RRUN.RobustnessBaselineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        reason = str(exc) if str(exc).isidentifier() else "robustness_request_invalid"
        raise HTTPException(status_code=422, detail=reason) from exc
    except RREP.RobustnessPersistenceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="robustness_inputs_unavailable") from exc


@router.get("/experiments/runs/{run_id}/robustness")
def canonical_robustness_reports(run_id: int = Path(..., gt=0),
                                 limit: int = Query(50, ge=1, le=200)):
    import experiment_validation_repository as EVR
    import robustness_repository as RREP
    try:
        with _experiment_validation_connection(query_only=True) as conn:
            ledger_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='experiment_validation_runs'").fetchone()
            report_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='robustness_reports'").fetchone()
            if not ledger_table or not report_table:
                return {"status": "ok", "reports": []}
            ledger = object.__new__(EVR.ExperimentValidationRepository)
            ledger.conn = conn
            baseline = ledger.get_run(run_id=run_id)
            if baseline is None:
                raise HTTPException(status_code=404, detail="validation_run_not_found")
            repository = object.__new__(RREP.RobustnessRepository)
            repository.conn = conn
            reports = repository.recent_reports(limit=limit, baseline_run_key=baseline["run_key"])
            return {"status": "ok", "reports": [
                {"id": item["id"], "report_key": item["report_key"],
                 "report_fingerprint": item["report_fingerprint"],
                 "plan_fingerprint": item["plan_fingerprint"],
                 "created_at": item["created_at"], "report": item["report"]}
                for item in reports]}
    except HTTPException:
        raise
    except RREP.RobustnessPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError:
        return {"status": "ok", "reports": []}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="robustness_history_unavailable") from exc


@router.get("/robustness/{report_id}")
def canonical_robustness_report(report_id: int = Path(..., gt=0)):
    import robustness_repository as RREP
    try:
        with _experiment_validation_connection(query_only=True) as conn:
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='robustness_reports'").fetchone()
            if not table:
                report = None
            else:
                repository = object.__new__(RREP.RobustnessRepository)
                repository.conn = conn
                report = repository.get_report(report_id)
    except RREP.RobustnessPersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError:
        report = None
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="robustness_history_unavailable") from exc
    if report is None:
        raise HTTPException(status_code=404, detail="robustness_report_not_found")
    return {"status": "ok", "report": report["report"], "plan": report["plan"],
            "report_key": report["report_key"],
            "report_fingerprint": report["report_fingerprint"]}


@router.post("/ai/analyze")
def run_ai_analysis(
    trigger: str = Query("manual-ui", max_length=80),
    window: str = Query("manual", max_length=30),
    scope: str = Query("all", max_length=30),
    as_of: str = Query(..., min_length=10, max_length=10),
    market_now: str = Query(..., min_length=20, max_length=40),
    account_id: str | None = Query(None, max_length=40),
    cycle_id: int | None = Query(None, gt=0),
    confirmed: bool = Query(False),
):
    """Run research at the caller-declared business day and market instant."""
    _require_confirmation(confirmed, "重试分时段AI分析")
    try:
        return adaptive.run_scheduled_ai_analysis(
            trigger=trigger, window=window, scope=scope, asof_day=as_of,
            market_now=market_now, account_id=account_id, cycle_id=cycle_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"运行AI分析失败：{type(exc).__name__}") from exc

@router.get("/trade-attributions")
def trade_attributions(
    limit: int = Query(160, ge=20, le=500),
    account_id: str | None = Query(None, max_length=40),
    trade_date: str | None = Query(None, min_length=10, max_length=10),
):
    """逐笔收盘归因：个股走势、大盘/板块贡献、公告事件和 AI 摘要。"""
    return adaptive.trade_attributions(limit=limit, account_id=account_id, trade_date=trade_date)


@router.post("/run")
def run(
    trigger: str = Query("manual", max_length=40),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行模拟盘学习")
    accepted, state = learning_dispatch.enqueue(trigger)
    return {
        "status": "accepted" if accepted else "busy",
        "message": "模拟盘学习已转入独立任务容器" if accepted else "已有模拟盘学习任务运行中",
        "run": state,
    }


@router.get("/run/status")
def run_status():
    return learning_dispatch.read_status()


@router.post("/advisor/run")
def run_advisor(
    trigger: str = Query("manual-ui", max_length=40),
    purpose: str = Query("data_quality", max_length=40),
    as_of: str | None = Query(None, min_length=10, max_length=10),
    market_now: str | None = Query(None, min_length=20, max_length=40),
    account_id: str | None = Query(None, max_length=40),
    cycle_id: int | None = Query(None, gt=0),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行数据质量审阅")
    try:
        return adaptive.run_advisor_review(
            trigger=trigger, purpose=purpose, asof_day=as_of, market_now=market_now,
            account_id=account_id, cycle_id=cycle_id,
        )
    except RuntimeError as exc:
        error = str(exc)
        messages = {
            "advisor_disabled": "DeepSeek 数据质量审阅尚未启用",
            "api_key_missing": "DeepSeek API 密钥尚未配置",
        }
        raise HTTPException(status_code=409, detail=messages.get(error, "DeepSeek 审阅暂不可用")) from exc
    except ValueError as exc:
        error = str(exc)
        if error == "attribution_context_required":
            raise HTTPException(status_code=422, detail="P&L 归因需要明确选择账户和周期") from exc
        if error == "portfolio_context_requires_account_and_cycle":
            raise HTTPException(status_code=422, detail="账户和周期必须同时提供") from exc
        raise HTTPException(status_code=422, detail="不支持的 DeepSeek 研究任务") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek 审阅失败：{type(exc).__name__}") from exc


@router.post("/advisor/suite")
def run_advisor_suite(
    trigger: str = Query("manual-suite", max_length=40),
    as_of: str = Query(..., min_length=10, max_length=10),
    market_now: str = Query(..., min_length=20, max_length=40),
    account_id: str | None = Query(None, max_length=40),
    cycle_id: int | None = Query(None, gt=0),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行研究套件")
    try:
        return adaptive.run_advisor_suite(
            trigger=trigger, asof_day=as_of, market_now=market_now,
            account_id=account_id, cycle_id=cycle_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="DeepSeek 研究套件尚未启用或密钥不可用") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek 研究套件运行失败：{type(exc).__name__}") from exc


@router.post("/ai/tune")
def run_ai_tuning(
    trigger: str = Query("manual-ai-tuning", max_length=80),
    mode: str = Query("intraday", max_length=30),
    confirmed: bool = Query(False),
):
    """Run the bounded DeepSeek tuner for the five paper accounts only."""
    if mode not in {"intraday", "close", "shadow"}:
        raise HTTPException(status_code=422, detail="调参模式只支持 intraday、close 或 shadow")
    _require_confirmation(confirmed, "运行 AI 有界调参")
    try:
        return adaptive.run_ai_tuning(trigger=trigger, mode=mode)
    except RuntimeError as exc:
        error = str(exc)
        messages = {
            "advisor_disabled": "DeepSeek 调参尚未启用",
            "api_key_missing": "DeepSeek API 密钥尚未配置",
        }
        raise HTTPException(status_code=409, detail=messages.get(error, "DeepSeek 调参暂不可用")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek 调参失败：{type(exc).__name__}") from exc


@router.post("/news/run")
def run_news_learning(
    trigger: str = Query("manual-ui", max_length=40),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "运行新闻学习")
    try:
        return adaptive.run_news_learning(trigger=trigger)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"新闻学习运行失败：{type(exc).__name__}: {exc}") from exc


@router.post("/feedback")
def feedback(
    decision_id: int = Query(..., gt=0),
    account_id: str = Query(...),
    verdict: str = Query(...),
    note: str = Query("", max_length=500),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "写入人工反馈")
    try:
        return adaptive.record_feedback(decision_id, account_id, verdict, note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/risk/apply")
def apply_risk_candidate(
    candidate_id: int = Query(..., gt=0),
    approved_by: str = Query("human", max_length=80),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "批准风控版本")
    try:
        # Keep the legacy query parameter for client compatibility, but never
        # trust it as the audit actor.
        return adaptive.apply_risk_candidate(candidate_id, _MANUAL_ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/risk/rollback")
def rollback_risk(
    account_id: str = Query(...),
    reason: str = Query("人工回滚", max_length=300),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "回滚风控版本")
    try:
        return adaptive.rollback_risk(account_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/selection/rollback")
def rollback_selection(
    account_id: str = Query(...),
    reason: str = Query("人工回滚", max_length=300),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "回滚选股版本")
    try:
        return adaptive.rollback_selection(account_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/selection/apply")
def apply_selection(
    candidate_id: int = Query(..., gt=0),
    approved_by: str = Query("human", min_length=1, max_length=80),
    confirmed: bool = Query(False),
):
    """人工确认结构化选股进化候选；参数级候选仍可由周期自动应用。"""
    _require_confirmation(confirmed, "批准选股版本")
    try:
        # Keep the legacy query parameter for client compatibility, but never
        # trust it as the audit actor.
        return adaptive.apply_selection(candidate_id, _MANUAL_ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# A 批自进化落地通道：Bandit 资金分摊 + 双AI共识提案
# ---------------------------------------------------------------------------

@router.post("/allocation/apply")
def apply_allocation(
    decision_id: int = Query(..., gt=0),
    approved_by: str = Query("human", min_length=1, max_length=80),
    confirmed: bool = Query(False),
):
    """人工批准把 Bandit 策略权重写入共享资金池分摊。"""
    _require_confirmation(confirmed, "批准资金分摊版本")
    import evolution_apply
    try:
        return evolution_apply.apply_allocation(
            adaptive._connect, adaptive.PAPER_DB_PATH, decision_id,
            approved_by=_MANUAL_ACTOR, confirmed=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/allocation/rollback")
def rollback_allocation(
    account_id: str = Query(...),
    reason: str = Query("人工回滚", max_length=300),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "回滚资金分摊版本")
    import evolution_apply
    try:
        return evolution_apply.rollback_allocation(
            adaptive.PAPER_DB_PATH, account_id, reason,
            approved_by=_MANUAL_ACTOR, confirmed=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tuner/apply")
def apply_tuner_proposals(
    run_id: int = Query(..., gt=0),
    approved_by: str = Query("human", min_length=1, max_length=80),
    confirmed: bool = Query(False),
):
    """人工批准把双AI共识提案写入选股因子权重覆盖。"""
    _require_confirmation(confirmed, "批准AI调参提案")
    import evolution_apply
    try:
        return evolution_apply.apply_tuner_proposals(
            adaptive._connect, adaptive.PAPER_DB_PATH, run_id,
            approved_by=_MANUAL_ACTOR, confirmed=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tuner/rollback")
def rollback_tuner_overlay(
    account_id: str = Query(...),
    reason: str = Query("人工回滚", max_length=300),
    confirmed: bool = Query(False),
):
    _require_confirmation(confirmed, "回滚AI调参覆盖")
    import evolution_apply
    try:
        return evolution_apply.rollback_tuner_overlay(
            adaptive._connect, adaptive.PAPER_DB_PATH, account_id, reason,
            approved_by=_MANUAL_ACTOR, confirmed=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/neural/approve")
def approve_neural_network(
    confirmed: bool = Query(False),
):
    """人工确认神经网络进入有界影子排序；不允许绕过任何交易硬门禁。"""
    _require_confirmation(confirmed, "批准神经网络影子评分")
    try:
        return adaptive.approve_neural_network(confirmed=True, approved_by=_MANUAL_ACTOR)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rebalance/rollback")
def rollback_rebalance(
    account_id: str = Query(...),
    reason: str = Query("人工回滚调仓版本", max_length=300),
    confirmed: bool = Query(False),
):
    """Semantic alias used by the paper-rebalance workspace."""
    _require_confirmation(confirmed, "回滚调仓版本")
    try:
        return adaptive.rollback_selection(account_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

# ─── 调仓扫描路由 ───

@router.get("/rebalance/status")
def rebalance_status():
    """当前 active cycle 的**实时**运营状态。

    **刻意不缓存**。这里曾经有一个 ``_cache_get("rebalance_status", ttl=30)``：
    它在解析当前周期**之前**就返回，于是 ``HTTP operational view`` 与
    ``authoritative current cycle`` 可以不一致 —— 同日 cycle 翻转后 30 秒内
    GET 仍返回旧周期的 payload；同周期内的 scan/plan 写入也要等 TTL 过期才
    可见；"没有 active cycle"时还会返回旧的 200 而不是 fail closed。

    Round-12 已经把 rebalance state 做成 cycle-owned，本端点必须每次都重新问
    账本"现在属于哪个周期"，而不是相信进程内的旧快照。缓存失效协议（按
    cycle 分键、scan/verify 后手工 clear、rollover 时 clear）都是**第二份
    authority**，会再次引入同一个缺陷类。

    读的是本地 SQLite 的少量 operational state（``rebalance_scans`` /
    ``rebalance_plans`` 各 LIMIT 10 + pending），不需要缓存。
    """
    try:
        import rebalance_scanner
        with _paper_rebalance_db() as conn:
            rebalance_scanner.ensure_schema(conn)
            # operational status 只回答"**当前周期**待执行什么"。历史跨周期的
            # recent history 若将来需要，应由独立接口提供，而不是混进这里。
            cycle_id = rebalance_scanner.resolve_cycle_id(conn)
            if cycle_id is None:
                raise HTTPException(status_code=409, detail={
                    "status": "no_active_cycle",
                    "message": "没有 active paper cycle，调仓状态不可判定",
                })
            return rebalance_scanner.get_rebalance_status(conn, cycle_id=cycle_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"调仓状态失败：{type(exc).__name__}") from exc


@router.post("/rebalance/scan")
def run_rebalance_scan(confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "运行调仓扫描")
    try:
        import rebalance_scanner
        # The network call must complete before opening the SQLite connection;
        # a slow quote source must never hold a write/read transaction open.
        quotes, quote_meta = _fetch_rebalance_quotes()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "status": "quote_unavailable", "error": type(exc).__name__,
        }) from exc
    try:
        # 一次扫描 = **一个** paper ledger 连接 = 一个事务。账本事实
        # （running 账户 / 当前持仓 lot / 委托 / 信号）与扫描写下的
        # rebalance 状态必须来自同一快照，否则持仓、风控状态与计划之间
        # 会在两个连接之间漂移。行情已在事务外取好，不会长事务阻塞网络。
        with _paper_rebalance_db() as conn:
            rebalance_scanner.ensure_schema(conn)
            # 周期在**打开连接后只读解析一次**，然后显式传下去。scanner 内部的
            # 每个 helper 都不得各自重新解析 active cycle —— 否则同一次扫描里
            # 的持仓、风控状态与计划可能来自不同的周期身份。
            cycle_id = rebalance_scanner.resolve_cycle_id(conn)
            if cycle_id is None:
                # fail closed：没有 active cycle 就不扫描、不写任何 rebalance
                # 状态。绝不用 MAX(paper_cycles.id) / 账户绑定 / 日期猜一个。
                raise HTTPException(status_code=409, detail={
                    "status": "no_active_cycle",
                    "message": "没有 active paper cycle，拒绝扫描并写入无归属的调仓状态",
                })
            accounts = []
            for acc in conn.execute("SELECT * FROM paper_accounts WHERE status='running'").fetchall():
                acc_dict = dict(acc)
                # 「当前持仓」必须来自权威 lot（cycle-scoped）。这里原本直接读
                # paper_positions 投影，而 daily_close_scan 会把这些行当作持仓
                # 评估质量并生成调仓计划 —— 旧周期残留镜像行会变成真实的调仓依据。
                acc_dict["positions"] = PPRM.current_positions(conn, account_id=acc["id"])
                accounts.append(acc_dict)
            result = rebalance_scanner.daily_close_scan(
                conn, accounts, quotes, cycle_id=cycle_id,
            )
            if isinstance(result, dict):
                result["quote_meta"] = quote_meta
            return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"调仓扫描失败：{type(exc).__name__}: {str(exc)[:200]}") from exc


@router.post("/rebalance/verify")
def verify_rebalance_plans(confirmed: bool = Query(False)):
    _require_confirmation(confirmed, "验证调仓计划")
    try:
        import rebalance_scanner
        with _paper_rebalance_db() as conn:
            rebalance_scanner.ensure_schema(conn)
            requested_cycle_id = rebalance_scanner.resolve_cycle_id(conn)
            if requested_cycle_id is None:
                raise HTTPException(status_code=409, detail={
                    "status": "no_active_cycle",
                    "message": "没有 active paper cycle，拒绝验证调仓计划",
                })
            plans = rebalance_scanner.get_pending_plans(conn, cycle_id=requested_cycle_id)
        if not plans:
            return {"message": "没有待验证的调仓计划", "plans": [],
                    "cycle_id": requested_cycle_id}
        # Fetch outside the connection scope; verification only writes after a
        # complete, auditable quote snapshot is available.
        quotes, quote_meta = _fetch_rebalance_quotes()
        # 验证写回的仍是**同一个** paper ledger：待验证计划是 scan 写在那里的，
        # 验证结果必须落在同一处，否则 status/plans 永远看不到验证状态。
        with _paper_rebalance_db() as conn:
            rebalance_scanner.ensure_schema(conn)
            # **周期竞态**：取计划与取行情之间隔着一次网络调用，期间周期可能
            # 从 8 翻到 9。此时手上的计划属于 cycle 8，而"当前周期"已是 9 ——
            # 绝不能在 cycle 9 里把 cycle 8 的计划验证掉。重新解析当前周期并要求
            # 它与请求时**逐字相同**，否则 fail closed（不写任何一行）。
            current_cycle_id = rebalance_scanner.resolve_cycle_id(conn)
            if current_cycle_id != requested_cycle_id:
                raise HTTPException(status_code=409, detail={
                    "status": "cycle_changed_during_verify",
                    "requested_cycle_id": requested_cycle_id,
                    "current_cycle_id": current_cycle_id,
                    "message": "取行情期间 paper cycle 已变化，拒绝验证 stale plan",
                })
            results = rebalance_scanner.verify_all_plans(
                conn, plans, quotes, cycle_id=current_cycle_id,
            )
            return {"plans": results, "quote_meta": quote_meta,
                    "cycle_id": current_cycle_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"验证失败：{type(exc).__name__}") from exc


@router.get("/rebalance/plans")
def get_rebalance_plans(status: str = Query("all")):
    try:
        import rebalance_scanner
        with _paper_rebalance_db() as conn:
            rebalance_scanner.ensure_schema(conn)
            # operational 视图只展示当前周期；历史跨周期计划不属于本接口。
            cycle_id = rebalance_scanner.resolve_cycle_id(conn)
            if cycle_id is None:
                raise HTTPException(status_code=409, detail={
                    "status": "no_active_cycle",
                    "message": "没有 active paper cycle，调仓计划不可判定",
                })
            if status == "all":
                rows = conn.execute(
                    "SELECT * FROM rebalance_plans WHERE cycle_id=? ORDER BY id DESC LIMIT 50",
                    (cycle_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rebalance_plans WHERE cycle_id=? AND status=?"
                    " ORDER BY id DESC LIMIT 50", (cycle_id, status)).fetchall()
            return {"plans": [dict(r) for r in rows], "cycle_id": cycle_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取计划失败：{type(exc).__name__}") from exc

# ─── AI 审核（通用槽位 ai1 / ai2）路由 ───
# 下面这组路由保留历史路径与字段名，方便旧界面/旧脚本继续工作；权威数据来自
# ai_review_service —— 运行期不存在任何"按厂商身份分支"的逻辑。

@router.get("/dual-ai/status")
def dual_ai_status():
    cached = _cache_get("dual_ai_status", ttl=30)
    if cached is not None:
        return cached
    try:
        result = adaptive.ai_review_status_fn()
        _cache_set("dual_ai_status", result)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"双AI状态失败：{type(exc).__name__}") from exc


@router.get("/ai-review/status")
def ai_review_status():
    """通用槽位状态的显式入口（与 /dual-ai/status 同源）。"""
    return dual_ai_status()


@router.get("/dual-ai/keys")
def dual_ai_api_keys():
    try:
        return {"keys": adaptive.get_dual_ai_api_keys_fn()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取API Key失败：{type(exc).__name__}") from exc


@router.post("/dual-ai/keys")
def update_dual_ai_api_key(
    provider: str = Query(..., max_length=20),
    api_key: str = Query(None, max_length=200),
    base_url: str = Query(None, max_length=500),
    model: str = Query(None, max_length=100),
    enabled: bool = Query(None),
    confirmed: bool = Query(False),
):
    """历史入口：``provider`` 作为槽位别名（mimo→ai1、deepseek→ai2）。"""
    _require_confirmation(confirmed, f"保存 {provider} API配置")
    try:
        adaptive.update_ai_slot_fn(
            provider, api_key=api_key, base_url=base_url, model=model, enabled=enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"keys": adaptive.get_dual_ai_api_keys_fn()}


@router.post("/dual-ai/tune")
def run_dual_ai_tuning(
    trigger: str = Query("manual-dual-ai", max_length=80),
    mode: str = Query("intraday", max_length=30),
    confirmed: bool = Query(False),
):
    if mode not in {"intraday", "close", "shadow"}:
        raise HTTPException(status_code=422, detail="调参模式只支持 intraday、close 或 shadow")
    _require_confirmation(confirmed, "运行双AI共识调参")
    try:
        return adaptive.run_dual_ai_tuning_fn(trigger=trigger, mode=mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"双AI调参失败：{type(exc).__name__}: {str(exc)[:200]}") from exc


@router.get("/dual-ai/runs")
def dual_ai_runs(limit: int = Query(20, ge=1, le=100)):
    try:
        import dual_ai_tuner
        with adaptive._connect() as conn:
            dual_ai_tuner.ensure_schema(conn)
            return {"runs": dual_ai_tuner.recent_runs(conn, limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取调参记录失败：{type(exc).__name__}") from exc


# ─── 自进化路由 ───

@router.get("/evolution/status")
def evolution_status():
    cached = _cache_get("evolution_status", ttl=30)
    if cached is not None:
        return cached
    try:
        result = adaptive.evolution_status_fn()
        _cache_set("evolution_status", result)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"自进化状态失败：{type(exc).__name__}") from exc


@router.get("/evolution/params")
def evolution_params(strategy_id: str = Query(None, max_length=100)):
    """读模型：显式区分 当前生效 / 最新候选 / 待激活候选。

    旧接口只回一行"当前参数"，无法分辨它到底是**生效版本**还是
    **刚插入的候选**。这里把三者分开返回。
    """
    try:
        return adaptive.get_evolution_params_fn(strategy_id)
    except SE.EvolutionLifecycleError as exc:
        # 指针损坏：绝不回落 latest row，直接暴露故障。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取进化参数失败：{type(exc).__name__}") from exc


@router.post("/evolution/evolve")
def trigger_evolution(confirmed: bool = Query(False)):
    """触发一次进化 —— 只**生成候选**，不激活。"""
    _require_confirmation(confirmed, "执行自进化")
    try:
        return adaptive.trigger_evolution_fn()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"执行进化失败：{type(exc).__name__}") from exc


@router.post("/evolution/activate")
def activate_evolution_candidate(
    params_id: int = Query(..., ge=1, description="要激活的候选参数版本 id"),
    confirmed: bool = Query(False),
    reason: str = Query(None, max_length=200),
):
    """显式激活一个已校验的候选：这是**唯一**能把候选变成 runtime 参数的入口。

    激活前仍要过 stale CAS —— 若候选创建时的基线已不是当前生效版本，
    返回 409，绝不静默 rebase 或覆盖。
    """
    _require_confirmation(confirmed, f"激活候选参数 #{params_id}")
    try:
        return adaptive.activate_evolution_candidate_fn(
            params_id, _MANUAL_ACTOR, reason=reason)
    except SE.CandidateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SE.EvolutionCandidateError as exc:
        # 未通过校验 / 已被拒绝 / 基线过期 / 并发推进：一律 409。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SE.EvolutionLifecycleError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"激活候选失败：{type(exc).__name__}") from exc


@router.get("/evolution/metrics")
def evolution_metrics(window: int = Query(20, ge=5, le=100)):
    try:
        return adaptive.evolution_metrics_fn(window)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取性能指标失败：{type(exc).__name__}") from exc


@router.get("/evolution/log")
def evolution_log(limit: int = Query(50, ge=1, le=200)):
    try:
        return {"log": adaptive.evolution_log_fn(limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取进化日志失败：{type(exc).__name__}") from exc


# ─── modlens 路由 ───

@router.get("/modlens/status")
def modlens_status():
    try:
        return adaptive.modlens_status_fn()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"modlens状态失败：{type(exc).__name__}") from exc


@router.post("/modlens/read-image")
def modlens_read_image(
    path: str = Query(..., max_length=1000),
    prompt: str = Query(None, max_length=500),
):
    try:
        return adaptive.modlens_read_image_fn(path, prompt)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"图片读取失败：{type(exc).__name__}") from exc
