//! Local-only V2 HTTP gateway. Only Rust can write to V2 SQLite.
use astock_v2::{domain::{ExperimentSpec, PaperAccountRequest, PaperOrderRequest, PaperSessionSpec, PaperSlotId, QuoteImport, SimulationRequest}, store::{Store, StoreError}};
use axum::{
    extract::{Path, Query, State}, http::StatusCode,
    routing::{get, post}, Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{env, sync::Arc};
use tokio::sync::Mutex;

type Shared = Arc<Mutex<Store>>;
type ApiResult = Result<Json<Value>, (StatusCode, Json<Value>)>;

fn api_error(error: StoreError) -> (StatusCode, Json<Value>) {
    let (code, reason) = match error {
        StoreError::Conflict(message) => (StatusCode::CONFLICT, message),
        StoreError::Rules(error) => (StatusCode::UNPROCESSABLE_ENTITY, error.0),
        StoreError::Integrity(error) => (StatusCode::INTERNAL_SERVER_ERROR, error),
        StoreError::NotFound(error) => (StatusCode::NOT_FOUND, error),
        StoreError::Database(error) => {
            eprintln!("V2 storage failure: {error}");
            (StatusCode::INTERNAL_SERVER_ERROR, "storage_error".into())
        }
        StoreError::Encoding(error) => {
            eprintln!("V2 encoding failure: {error}");
            (StatusCode::INTERNAL_SERVER_ERROR, "encoding_error".into())
        }
    };
    (code, Json(json!({"error":reason})))
}

fn response<T: serde::Serialize>(value: T) -> ApiResult {
    serde_json::to_value(value).map(Json).map_err(|error| {
        (StatusCode::INTERNAL_SERVER_ERROR,
         Json(json!({"error":format!("encoding_error: {error}")})))
    })
}

async fn health(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let schema_version: i64 = db.schema_version().map_err(api_error)?;
    let price_sets = db.price_set_catalogue().map(|sets| sets.len()).unwrap_or(0);
    response(json!({
        "status": "ok",
        "system": "astock-v2",
        "paper_only": true,
        "schema_version": schema_version,
        "price_sets": price_sets,
        "writer": "store-single-writer",
    }))
}

async fn submit(
    State(state): State<Shared>, Json(request): Json<SimulationRequest>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(db.submit(request).map_err(api_error)?)
}

async fn ingest_quote(
    State(state): State<Shared>, Json(input): Json<QuoteImport>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(db.ingest_quote(input).map_err(api_error)?)
}

#[derive(Deserialize)]
struct PortfolioQuery {
    cycle_id: String,
    asof_day: Option<String>,
}

async fn portfolio(
    State(state): State<Shared>, Query(query): Query<PortfolioQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(db.state(&query.cycle_id, query.asof_day.as_deref()).map_err(api_error)?)
}

async fn runs(State(state): State<Shared>, Query(query): Query<PortfolioQuery>) -> ApiResult {
    let db = state.lock().await;
    response(db.runs(&query.cycle_id, 100).map_err(api_error)?)
}

async fn replay(State(state): State<Shared>, Query(query): Query<PortfolioQuery>) -> ApiResult {
    let db = state.lock().await;
    response(db.verify_replay(&query.cycle_id).map_err(api_error)?)
}

async fn research_reproduction(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    let db = state.lock().await;
    response(db.verify_research_reproduction(&id).map_err(api_error)?)
}

#[derive(Deserialize)]
struct PriceDaysQuery {
    pack_id: String,
    symbol: String,
}

async fn price_set_days(
    State(state): State<Shared>,
    Query(query): Query<PriceDaysQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(
        db.price_set_days(&query.pack_id, &query.symbol)
            .map_err(api_error)?,
    )
}

#[derive(Deserialize)]
struct CoverageQuery {
    pack_id: String,
    symbols: String,
    days: String,
}

async fn price_set_coverage(
    State(state): State<Shared>,
    Query(query): Query<CoverageQuery>,
) -> ApiResult {
    let symbols: Vec<String> = query
        .symbols
        .split(',')
        .map(|symbol| symbol.trim().to_string())
        .filter(|symbol| !symbol.is_empty())
        .collect();
    let days: Vec<String> = query
        .days
        .split(',')
        .map(|day| day.trim().to_string())
        .filter(|day| !day.is_empty())
        .collect();
    let db = state.lock().await;
    response(
        db.price_set_coverage(&query.pack_id, &symbols, &days)
            .map_err(api_error)?,
    )
}

#[derive(Deserialize)]
struct SwitchQuery {
    #[serde(default)]
    slot: Option<String>,
}

#[derive(Deserialize)]
struct StrategySwitchRequest {
    slot: String,
    to_version: String,
    effective_from: String,
    reason: String,
    switch_id: String,
}

async fn post_strategy_switch(
    State(state): State<Shared>,
    Path(account_id): Path<String>,
    Json(body): Json<StrategySwitchRequest>,
) -> ApiResult {
    let slot = match body.slot.as_str() {
        "A" => astock_v2::domain::PaperSlotId::A,
        "B" => astock_v2::domain::PaperSlotId::B,
        _ => {
            return Err(api_error(astock_v2::store::StoreError::Rules(
                astock_v2::domain::RuleError("unknown_paper_slot".into()),
            )))
        }
    };
    let mut db = state.lock().await;
    response(
        db.record_strategy_switch(
            &account_id,
            slot,
            &body.to_version,
            &body.effective_from,
            &body.reason,
            &body.switch_id,
        )
        .map_err(api_error)?,
    )
}

async fn strategy_switches(
    State(state): State<Shared>,
    Path(account_id): Path<String>,
    Query(query): Query<SwitchQuery>,
) -> ApiResult {
    let slot = match query.slot.as_deref() {
        None => None,
        Some("A") => Some(astock_v2::domain::PaperSlotId::A),
        Some("B") => Some(astock_v2::domain::PaperSlotId::B),
        Some(_) => {
            return Err(api_error(astock_v2::store::StoreError::Rules(
                astock_v2::domain::RuleError("unknown_paper_slot".into()),
            )))
        }
    };
    let db = state.lock().await;
    response(db.strategy_switches(&account_id, slot).map_err(api_error)?)
}

async fn accounts_overview(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.accounts_overview().map_err(api_error)?)
}

async fn paper_accounts(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_accounts(100).map_err(api_error)?)
}

async fn create_paper_account(
    State(state): State<Shared>,
    Json(request): Json<PaperAccountRequest>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(db.create_paper_account_request(request).map_err(api_error)?)
}

async fn price_set_catalogue(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.price_set_catalogue().map_err(api_error)?)
}

async fn strategy_catalogue(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.strategy_catalogue().map_err(api_error)?)
}

#[derive(Deserialize)]
struct ValuationQuery {
    as_of_day: String,
}

#[derive(Deserialize)]
struct EquitySeriesQuery {
    days: String,
}

async fn paper_allocation_proposal(
    State(state): State<Shared>,
    Path(id): Path<String>,
    Query(query): Query<EquitySeriesQuery>,
) -> ApiResult {
    let days: Vec<String> = query
        .days
        .split(',')
        .map(|day| day.trim().to_string())
        .filter(|day| !day.is_empty())
        .collect();
    let db = state.lock().await;
    let dir = std::path::PathBuf::from(
        std::env::var_os("ASTOCK_PRICE_SETS_DIR")
            .map(std::ffi::OsString::from)
            .unwrap_or_else(|| "data/price-sets".into()),
    );
    match db
        .paper_allocation_proposal(&id, &days, &dir)
        .map_err(api_error)?
    {
        Some(report) => response(report),
        None => response(json!({
            "status": "unavailable",
            "reason": "no_reviewed_price_set",
            "days": days.len(),
        })),
    }
}

#[derive(Deserialize)]
struct RebalanceBody {
    targets: [i64; 2],
    proposal_digest: String,
    applied_at: String,
    note: String,
}

async fn apply_rebalance(
    State(state): State<Shared>,
    Path(id): Path<String>,
    Json(body): Json<RebalanceBody>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(
        db.apply_rebalance(
            &id,
            body.targets,
            &body.proposal_digest,
            &body.applied_at,
            &body.note,
        )
        .map_err(api_error)?,
    )
}

async fn paper_rebalances(
    State(state): State<Shared>,
    Path(id): Path<String>,
) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_rebalances(&id).map_err(api_error)?)
}

#[derive(Deserialize)]
struct AccountAdvanceBody {
    day: String,
}

#[derive(Deserialize)]
struct EvolutionRequest {
    kind: String,
    incumbent: astock_v2::domain::CandidateMetrics,
    candidate: astock_v2::domain::CandidateMetrics,
    #[serde(default)]
    policy: Option<astock_v2::domain::EvolutionPolicy>,
}

#[derive(Deserialize)]
struct EvolutionEventRequest {
    event_id: String,
    decided_at: String,
    kind: String,
    incumbent: astock_v2::domain::CandidateMetrics,
    candidate: astock_v2::domain::CandidateMetrics,
    #[serde(default)]
    policy: Option<astock_v2::domain::EvolutionPolicy>,
    #[serde(default)]
    base_cycle: Option<String>,
    #[serde(default)]
    candidate_cycle: Option<String>,
}

async fn record_evolution_event(
    State(state): State<Shared>,
    Json(body): Json<EvolutionEventRequest>,
) -> ApiResult {
    let policy = body.policy.clone().unwrap_or_default();
    let decision = match body.kind.as_str() {
        "promotion" => astock_v2::domain::decide_promotion(&body.incumbent, &body.candidate, &policy),
        "rollback" => astock_v2::domain::decide_rollback(&body.incumbent, &body.candidate, &policy),
        _ => {
            return Err((
                axum::http::StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"error": "unsupported_evolution_kind"})),
            ))
        }
    }
    .map_err(|error| (axum::http::StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"error": error.0}))))?;
    let mut db = state.lock().await;
    response(
        db.record_evolution_event_with_links(
            &body.event_id,
            &body.decided_at,
            decision,
            body.base_cycle.as_deref(),
            body.candidate_cycle.as_deref(),
        )
        .map_err(api_error)?,
    )
}

#[derive(Deserialize)]
struct EvolutionListQuery {
    #[serde(default = "default_evolution_limit")]
    limit: i64,
    #[serde(default)]
    candidate: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    base_cycle: Option<String>,
    #[serde(default)]
    candidate_cycle: Option<String>,
    #[serde(default)]
    from_day: Option<String>,
    #[serde(default)]
    to_day: Option<String>,
}

#[derive(Deserialize)]
struct EvolutionPageQuery {
    #[serde(default = "default_evolution_limit")]
    limit: i64,
    #[serde(default)]
    candidate: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    before: Option<String>,
    #[serde(default)]
    before_id: Option<String>,
    #[serde(default)]
    after: Option<String>,
    #[serde(default)]
    base_cycle: Option<String>,
    #[serde(default)]
    candidate_cycle: Option<String>,
    #[serde(default)]
    from_day: Option<String>,
    #[serde(default)]
    to_day: Option<String>,
}

async fn evolution_export(
    State(state): State<Shared>,
    Query(query): Query<EvolutionListQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(
        db.evolution_export(astock_v2::store::EvolutionQuery {
            limit: 2_000,
            candidate_id: query.candidate.as_deref(),
            status: query.status.as_deref(),
            base_cycle: query.base_cycle.as_deref(),
            candidate_cycle: query.candidate_cycle.as_deref(),
            from_day: query.from_day.as_deref(),
            to_day: query.to_day.as_deref(),
        })
        .map_err(api_error)?,
    )
}

async fn evolution_events_page(
    State(state): State<Shared>,
    Query(query): Query<EvolutionPageQuery>,
) -> ApiResult {
    let db = state.lock().await;
    let limit = query.limit.clamp(1, 200);
    response(
        db.evolution_events_page_query(astock_v2::store::EvolutionPageQuery {
            query: astock_v2::store::EvolutionQuery {
                limit,
                candidate_id: query.candidate.as_deref(),
                status: query.status.as_deref(),
                base_cycle: query.base_cycle.as_deref(),
                candidate_cycle: query.candidate_cycle.as_deref(),
                from_day: query.from_day.as_deref(),
                to_day: query.to_day.as_deref(),
            },
            before: query.before.as_deref(),
            before_id: query.before_id.as_deref(),
            after: query.after.as_deref(),
        })
        .map_err(api_error)?,
    )
}

fn default_evolution_limit() -> i64 {
    50
}

async fn evolution_events(
    State(state): State<Shared>,
    Query(query): Query<EvolutionListQuery>,
) -> ApiResult {
    let db = state.lock().await;
    let limit = query.limit.clamp(1, 200);
    response(
        db.evolution_events_query(astock_v2::store::EvolutionQuery {
            limit,
            candidate_id: query.candidate.as_deref(),
            status: query.status.as_deref(),
            base_cycle: query.base_cycle.as_deref(),
            candidate_cycle: query.candidate_cycle.as_deref(),
            from_day: query.from_day.as_deref(),
            to_day: query.to_day.as_deref(),
        })
        .map_err(api_error)?,
    )
}

#[derive(Deserialize)]
struct SignalDiffQuery {
    base_cycle: String,
    candidate_cycle: String,
}

async fn signal_diff(
    State(state): State<Shared>,
    Query(query): Query<SignalDiffQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(
        db.signal_diff(&query.base_cycle, &query.candidate_cycle)
            .map_err(api_error)?,
    )
}

async fn evolution_decision(Json(body): Json<EvolutionRequest>) -> ApiResult {
    let policy = body.policy.clone().unwrap_or_default();
    let decision = match body.kind.as_str() {
        "promotion" => astock_v2::domain::decide_promotion(&body.incumbent, &body.candidate, &policy),
        "rollback" => astock_v2::domain::decide_rollback(&body.incumbent, &body.candidate, &policy),
        _ => return Err((
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error": "unsupported_evolution_kind"})),
        )),
    }
    .map_err(|error| (axum::http::StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"error": error.0}))))?;
    response(decision)
}

async fn advance_account(
    State(state): State<Shared>,
    Path(id): Path<String>,
    Json(body): Json<AccountAdvanceBody>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(db.advance_account_to(&id, &body.day).map_err(api_error)?)
}

async fn paper_exposure(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_exposure(&id).map_err(api_error)?)
}

#[derive(Deserialize)]
struct SegmentsQuery {
    segments: String,
}

async fn experiment_performance_segments(
    State(state): State<Shared>,
    Path(cycle_id): Path<String>,
    Query(query): Query<SegmentsQuery>,
) -> ApiResult {
    let segments: Vec<Vec<String>> = query
        .segments
        .split(';')
        .map(|segment| {
            segment
                .split(',')
                .map(|day| day.trim().to_string())
                .filter(|day| !day.is_empty())
                .collect::<Vec<_>>()
        })
        .filter(|segment| !segment.is_empty())
        .collect();
    let dir = std::path::PathBuf::from(
        std::env::var_os("ASTOCK_PRICE_SETS_DIR")
            .map(std::ffi::OsString::from)
            .unwrap_or_else(|| "data/price-sets".into()),
    );
    let db = state.lock().await;
    response(
        db.experiment_performance_segments(&cycle_id, &segments, &dir)
            .map_err(api_error)?,
    )
}

async fn experiment_performance(
    State(state): State<Shared>,
    Path(cycle_id): Path<String>,
    Query(query): Query<EquitySeriesQuery>,
) -> ApiResult {
    let days: Vec<String> = query
        .days
        .split(',')
        .map(|day| day.trim().to_string())
        .filter(|day| !day.is_empty())
        .collect();
    let dir = std::path::PathBuf::from(
        std::env::var_os("ASTOCK_PRICE_SETS_DIR")
            .map(std::ffi::OsString::from)
            .unwrap_or_else(|| "data/price-sets".into()),
    );
    let db = state.lock().await;
    match db
        .experiment_performance(&cycle_id, &days, &dir)
        .map_err(api_error)?
    {
        Some(report) => response(report),
        None => response(json!({
            "status": "unavailable",
            "reason": "no_reviewed_price_set",
            "days": days.len(),
        })),
    }
}

async fn paper_performance(
    State(state): State<Shared>,
    Path(id): Path<String>,
    Query(query): Query<EquitySeriesQuery>,
) -> ApiResult {
    let days: Vec<String> = query
        .days
        .split(',')
        .map(|day| day.trim().to_string())
        .filter(|day| !day.is_empty())
        .collect();
    let dir = std::path::PathBuf::from(
        std::env::var_os("ASTOCK_PRICE_SETS_DIR")
            .map(std::ffi::OsString::from)
            .unwrap_or_else(|| "data/price-sets".into()),
    );
    let db = state.lock().await;
    match db
        .paper_performance(&id, &days, &dir)
        .map_err(api_error)?
    {
        Some(report) => response(report),
        None => response(json!({
            "status": "unavailable",
            "reason": "no_reviewed_price_set",
            "days": days.len(),
        })),
    }
}

async fn paper_account_audit(
    State(state): State<Shared>,
    Path(id): Path<String>,
) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_account_audit(&id).map_err(api_error)?)
}

async fn paper_equity_series(
    State(state): State<Shared>,
    Path(id): Path<String>,
    Query(query): Query<EquitySeriesQuery>,
) -> ApiResult {
    let days: Vec<String> = query
        .days
        .split(',')
        .map(|day| day.trim().to_string())
        .filter(|day| !day.is_empty())
        .collect();
    if days.is_empty() {
        return Err((
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error": "empty_equity_series"})),
        ));
    }
    let db = state.lock().await;
    match db.paper_equity_series(&id, &days).map_err(api_error)? {
        Some(series) => response(series),
        None => response(json!({
            "status": "unavailable",
            "reason": "no_reviewed_price_set",
            "days": days.len(),
        })),
    }
}

async fn paper_valuation(
    State(state): State<Shared>,
    Path(id): Path<String>,
    Query(query): Query<ValuationQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_valuation(&id, &query.as_of_day).map_err(api_error)?)
}

async fn paper_account(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_account(&id).map_err(api_error)?)
}

async fn submit_paper_order(
    State(state): State<Shared>,
    Path(account_id): Path<String>,
    Json(mut request): Json<PaperOrderRequest>,
) -> ApiResult {
    if request.account_id != account_id {
        return Err((
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error": "paper_account_path_mismatch"})),
        ));
    }
    request.account_id = account_id;
    let mut db = state.lock().await;
    response(db.submit_paper_order(request).map_err(api_error)?)
}

async fn paper_orders(
    State(state): State<Shared>,
    Path(account_id): Path<String>,
) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_orders(&account_id, 100).map_err(api_error)?)
}

#[derive(Deserialize)]
struct AdvanceBody {
    day: String,
}

#[derive(Deserialize)]
struct SessionFromExperiment {
    session_id: String,
    account_id: String,
    cycle_id: String,
    slot: PaperSlotId,
    start_at: Option<String>,
}

async fn create_session_from_experiment(
    State(state): State<Shared>,
    Json(body): Json<SessionFromExperiment>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(
        db.create_paper_session_from_experiment(
            &body.session_id,
            &body.account_id,
            &body.cycle_id,
            body.slot,
            body.start_at,
        )
        .map_err(api_error)?,
    )
}

async fn create_paper_session(
    State(state): State<Shared>,
    Json(spec): Json<PaperSessionSpec>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(db.create_paper_session(spec).map_err(api_error)?)
}

#[derive(Deserialize)]
struct SessionListQuery {
    account_id: Option<String>,
}

async fn paper_sessions(
    State(state): State<Shared>,
    Query(query): Query<SessionListQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(
        db.paper_sessions(query.account_id.as_deref(), 100)
            .map_err(api_error)?,
    )
}

async fn paper_session(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    let db = state.lock().await;
    response(db.paper_session(&id).map_err(api_error)?)
}

async fn paper_session_advance(
    State(state): State<Shared>,
    Path(session_id): Path<String>,
    Json(body): Json<AdvanceBody>,
) -> ApiResult {
    let mut db = state.lock().await;
    response(db.advance_paper_session_to(&session_id, &body.day).map_err(api_error)?)
}

async fn paper_session_action(
    State(state): State<Shared>,
    Path((session_id, action)): Path<(String, String)>,
) -> ApiResult {
    let mut db = state.lock().await;
    match action.as_str() {
        "start" | "pause" | "resume" | "stop" => {
            response(db.paper_session_action(&session_id, &action).map_err(api_error)?)
        }
        "step" => response(db.step_paper_session(&session_id).map_err(api_error)?),
        "run" => response(db.run_paper_session(&session_id).map_err(api_error)?),
        _ => Err((
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error": "unsupported_paper_session_action"})),
        )),
    }
}

async fn market_data_pack(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    response(state.lock().await.market_data_pack(&id).map_err(api_error)?)
}

#[derive(Deserialize)]
struct ListQuery {
    limit: Option<i64>,
}

/// Read-only catalogue for the experiment picker: no write path, no derived
/// metrics, no "latest" shortcut. Ordering is the Store's stable cycle_id order.
async fn list_experiments(State(state): State<Shared>, Query(query): Query<ListQuery>) -> ApiResult {
    let db = state.lock().await;
    let catalogue = db.experiments(query.limit.unwrap_or(200)).map_err(api_error)?;
    response(catalogue)
}

async fn create_experiment(State(state): State<Shared>, Json(spec): Json<ExperimentSpec>) -> ApiResult {
    response(state.lock().await.create_experiment(spec).map_err(api_error)?)
}
async fn experiment(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    response(state.lock().await.experiment(&id).map_err(api_error)?)
}
async fn complete_experiment(State(state): State<Shared>, Path(id): Path<String>) -> ApiResult {
    response(state.lock().await.complete_experiment(&id).map_err(api_error)?)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db_path = env::var("ASTOCK_V2_DB").unwrap_or_else(|_| "./v2-market-packs.sqlite3".into());
    if env::var_os("ASTOCK_V2_INITIAL_CASH_CENTS").is_some() {
        return Err("global initial cash is unsupported; create an experiment explicitly".into());
    }
    let db = Arc::new(Mutex::new(Store::open(&db_path)?));
    // Always local-only. No network exposure, broker or real-money endpoints.
    let app = Router::new()
        .route("/v2/health", get(health))
        .route("/v2/experiments", post(create_experiment).get(list_experiments))
        .route("/v2/market-data-packs/{pack_id}", get(market_data_pack))
        .route("/v2/paper-accounts", post(create_paper_account).get(paper_accounts))
        .route("/v2/paper-accounts/overview", get(accounts_overview))
        .route(
            "/v2/paper-accounts/{account_id}/strategy-switches",
            get(strategy_switches).post(post_strategy_switch),
        )
        .route("/v2/strategies", get(strategy_catalogue))
        .route("/v2/evolution/decisions", post(evolution_decision))
        .route("/v2/signal-diff", get(signal_diff))
        .route(
            "/v2/evolution/events",
            post(record_evolution_event).get(evolution_events),
        )
        .route("/v2/evolution/events/page", get(evolution_events_page))
        .route("/v2/evolution/events/export", get(evolution_export))
        .route("/v2/price-sets", get(price_set_catalogue))
        .route("/v2/price-sets/days", get(price_set_days))
        .route("/v2/price-sets/coverage", get(price_set_coverage))
        .route("/v2/paper-accounts/{account_id}", get(paper_account))
        .route(
            "/v2/paper-accounts/{account_id}/valuation",
            get(paper_valuation),
        )
        .route(
            "/v2/paper-accounts/{account_id}/equity-series",
            get(paper_equity_series),
        )
        .route(
            "/v2/paper-accounts/{account_id}/audit",
            get(paper_account_audit),
        )
        .route(
            "/v2/paper-accounts/{account_id}/performance",
            get(paper_performance),
        )
        .route(
            "/v2/experiments/{cycle_id}/performance",
            get(experiment_performance),
        )
        .route(
            "/v2/experiments/{cycle_id}/performance/segments",
            get(experiment_performance_segments),
        )
        .route(
            "/v2/paper-accounts/{account_id}/exposure",
            get(paper_exposure),
        )
        .route(
            "/v2/paper-accounts/{account_id}/advance",
            post(advance_account),
        )
        .route(
            "/v2/paper-accounts/{account_id}/rebalances",
            post(apply_rebalance).get(paper_rebalances),
        )
        .route(
            "/v2/paper-accounts/{account_id}/allocation-proposal",
            get(paper_allocation_proposal),
        )
        .route(
            "/v2/paper-accounts/{account_id}/orders",
            post(submit_paper_order).get(paper_orders),
        )
        .route("/v2/paper-sessions", post(create_paper_session).get(paper_sessions))
        .route(
            "/v2/paper-sessions/from-experiment",
            post(create_session_from_experiment),
        )
        .route("/v2/paper-sessions/{session_id}", get(paper_session))
        .route(
            "/v2/paper-sessions/{session_id}/advance",
            post(paper_session_advance),
        )
        .route(
            "/v2/paper-sessions/{session_id}/{action}",
            post(paper_session_action),
        )
        .route("/v2/experiments/{cycle_id}", get(experiment))
        .route("/v2/experiments/{cycle_id}/complete", post(complete_experiment))
        .route("/v2/quotes", post(ingest_quote))
        .route("/v2/simulations", post(submit).get(runs))
        .route("/v2/portfolio", get(portfolio))
        .route("/v2/replay", get(replay))
        .route("/v2/experiments/{cycle_id}/research-reproduction", get(research_reproduction))
        .with_state(db);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:8765").await?;
    println!("astock-v2 paper-only gateway listening at http://127.0.0.1:8765");
    axum::serve(listener, app).await?;
    Ok(())
}
