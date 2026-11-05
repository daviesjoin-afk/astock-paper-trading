//! Local-only V2 HTTP gateway. Only Rust can write to V2 SQLite.
use astock_v2::{domain::{SimulationRequest, INITIAL_CASH_CENTS}, store::{Store, StoreError}};
use axum::{
    extract::{Query, State}, http::StatusCode,
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

#[derive(Deserialize)]
struct PortfolioQuery {
    asof_day: Option<String>,
}

async fn portfolio(
    State(state): State<Shared>, Query(query): Query<PortfolioQuery>,
) -> ApiResult {
    let db = state.lock().await;
    response(db.state(query.asof_day.as_deref()).map_err(api_error)?)
}

async fn runs(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.runs(100).map_err(api_error)?)
}

async fn replay(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    response(db.verify_replay().map_err(api_error)?)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db_path = env::var("ASTOCK_V2_DB").unwrap_or_else(|_| "./v2-paper.sqlite3".into());
    let initial = env::var("ASTOCK_V2_INITIAL_CASH_CENTS")
        .ok().map(|x| x.parse::<i64>()).transpose()?
        .unwrap_or(INITIAL_CASH_CENTS);
    let db = Arc::new(Mutex::new(Store::open(&db_path, initial)?));
    // Always local-only. No network exposure, broker or real-money endpoints.
    let app = Router::new()
        .route("/v2/health", get(health))
        .route("/v2/simulations", post(submit).get(runs))
        .route("/v2/portfolio", get(portfolio))
        .route("/v2/replay", get(replay))
        .with_state(db);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:8765").await?;
    println!("astock-v2 paper-only gateway listening at http://127.0.0.1:8765");
    axum::serve(listener, app).await?;
    Ok(())
}
