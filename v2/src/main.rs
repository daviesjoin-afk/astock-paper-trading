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

async fn health() -> Json<Value> {
    Json(json!({"status":"ok", "system":"astock-v2", "paper_only":true}))
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

async fn strategy_catalogue(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.strategy_catalogue().map_err(api_error)?)
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
        .route("/v2/strategies", get(strategy_catalogue))
        .route("/v2/paper-accounts/{account_id}", get(paper_account))
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
