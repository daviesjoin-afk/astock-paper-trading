//! Local-only V2 HTTP gateway. It serves one paper account and two strategies.

use std::{env, sync::Arc};

use astock_v2::domain::{AccountSpec, CreateAccountRequest, SlotSpec, StrategyId};
use astock_v2::store::{load_price_sets, price_sets_dir, Store, StoreError};
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::Mutex;

type Shared = Arc<Mutex<Store>>;
type ApiResult = Result<Json<Value>, (StatusCode, Json<Value>)>;

fn api_error(error: StoreError) -> (StatusCode, Json<Value>) {
    let (status, message) = match error {
        StoreError::Rules(error) => (StatusCode::UNPROCESSABLE_ENTITY, error.0),
        StoreError::NotFound(error) => (StatusCode::NOT_FOUND, error),
        StoreError::Conflict(error) => (StatusCode::CONFLICT, error),
        StoreError::Encoding(error) => (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
        StoreError::Database(error) => (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()),
        StoreError::Integrity(error) => (StatusCode::INTERNAL_SERVER_ERROR, error),
    };
    (status, Json(json!({ "error": message })))
}

fn response<T: serde::Serialize>(value: T) -> ApiResult {
    serde_json::to_value(value).map(Json).map_err(|error| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": error.to_string() })),
        )
    })
}

async fn health(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let version = db.schema_version().map_err(api_error)?;
    let set = load_price_sets(&price_sets_dir()).map_err(api_error)?;
    let account = db.account().map_err(api_error)?;
    let last_settled_day = match &account {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };
    response(json!({
        "status": "ok",
        "system": "astock-v2",
        "schema_version": version,
        "paper_only": true,
        "account": account.map(|account| account.account_id),
        "price_set": set.as_ref().map(|set| set.price_set_id.clone()),
        "last_settled_day": last_settled_day,
    }))
}

async fn create_account(
    State(state): State<Shared>,
    Json(request): Json<CreateAccountRequest>,
) -> ApiResult {
    let mut db = state.lock().await;
    if db.account().map_err(api_error)?.is_some() {
        return Err((
            StatusCode::CONFLICT,
            Json(json!({ "error": "account_already_exists" })),
        ));
    }
    let spec = AccountSpec {
        schema_version: 1,
        account_id: "paper-account".into(),
        initial_cash_cents: request.initial_cash_cents,
        created_at: Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        slots: [
            SlotSpec { slot: StrategyId::Steady, weight_bps: 5_000 },
            SlotSpec { slot: StrategyId::Aggressive, weight_bps: 5_000 },
        ],
    };
    db.create_account(spec.clone()).map_err(api_error)?;
    let set = load_price_sets(&price_sets_dir()).map_err(api_error)?;
    response(db.view(&spec, set.as_ref(), None).map_err(api_error)?)
}

async fn account(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    let set = load_price_sets(&price_sets_dir()).map_err(api_error)?;
    let day = db.last_settled_day(&spec.account_id).map_err(api_error)?;
    response(db.view(&spec, set.as_ref(), day.as_deref()).map_err(api_error)?)
}

async fn positions(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    let set = load_price_sets(&price_sets_dir()).map_err(api_error)?;
    let day = db.last_settled_day(&spec.account_id).map_err(api_error)?;
    let view = db.view(&spec, set.as_ref(), day.as_deref()).map_err(api_error)?;
    response(view.positions)
}

async fn trades(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    response(db.trades(&spec.account_id, 100).map_err(api_error)?)
}

async fn rebalances(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    response(db.rebalances(&spec.account_id).map_err(api_error)?)
}

async fn coverage(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let set = load_price_sets(&price_sets_dir()).map_err(api_error)?;
    let last_settled_day = match db.account().map_err(api_error)? {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };
    response(db.coverage(set.as_ref(), last_settled_day))
}

// ---- whole-market bar endpoints -------------------------------------------
//
// These read the `bar` table that `data_sources/ingest_market.py` fills. The
// reviewed-price-set path above is unchanged and still drives settlement; this
// is the read side of the full-market data that the reviewed sets never had.

#[derive(Deserialize)]
struct BarsQuery {
    day: Option<String>,
    /// Defaults to 1100 bps, which clears a ±10% limit plus rounding.
    threshold_bps: Option<i64>,
}

/// Trading days we hold bars for, with the symbol count of the latest day.
async fn bar_days(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let days = db.bar_days().map_err(api_error)?;
    let latest = days.last().cloned();
    let latest_symbols = match &latest {
        Some(day) => db.bar_symbol_count(day).map_err(api_error)?,
        None => 0,
    };
    response(json!({
        "days": days,
        "count": days.len(),
        "latest_day": latest,
        "latest_symbol_count": latest_symbols,
    }))
}

/// All bars for one trading day. `day` defaults to the latest day held.
async fn bars(State(state): State<Shared>, Query(query): Query<BarsQuery>) -> ApiResult {
    let db = state.lock().await;
    let day = match query.day {
        Some(day) => day,
        None => match db.bar_days().map_err(api_error)?.last() {
            Some(day) => day.clone(),
            None => {
                return Err((
                    StatusCode::NOT_FOUND,
                    Json(json!({ "error": "no_bars_ingested" })),
                ));
            }
        },
    };
    let rows = db.bars_for_day(&day).map_err(api_error)?;
    if rows.is_empty() {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "no_bars_for_day", "day": day })),
        ));
    }
    response(json!({ "day": day, "count": rows.len(), "bars": rows }))
}

/// Bars whose close gapped past `threshold_bps` from their own prev close.
///
/// Not a validation result: nothing is rejected. This is the display hint that
/// keeps an ex-dividend gap from being drawn as a solid -20% crash on a chart.
async fn bar_gaps(State(state): State<Shared>, Query(query): Query<BarsQuery>) -> ApiResult {
    let db = state.lock().await;
    let day = match query.day {
        Some(day) => day,
        None => match db.bar_days().map_err(api_error)?.last() {
            Some(day) => day.clone(),
            None => {
                return Err((
                    StatusCode::NOT_FOUND,
                    Json(json!({ "error": "no_bars_ingested" })),
                ));
            }
        },
    };
    let threshold = query.threshold_bps.unwrap_or(1_100);
    let rows = db.price_gaps(&day, threshold).map_err(api_error)?;
    response(json!({
        "day": day,
        "threshold_bps": threshold,
        "count": rows.len(),
        "bars": rows,
    }))
}

#[derive(Deserialize)]
struct SymbolBarsQuery {
    from: Option<String>,
    to: Option<String>,
}

/// One symbol's recent bars, oldest first. Defaults to the last 250 days held.
async fn symbol_bars(
    State(state): State<Shared>,
    Path(symbol): Path<String>,
    Query(query): Query<SymbolBarsQuery>,
) -> ApiResult {
    let db = state.lock().await;
    let days = db.bar_days().map_err(api_error)?;
    let Some(last) = days.last().cloned() else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "no_bars_ingested" })),
        ));
    };
    let to = query.to.unwrap_or(last);
    let from = query.from.unwrap_or_else(|| {
        let start = days.len().saturating_sub(250);
        days.get(start).cloned().unwrap_or_else(|| to.clone())
    });
    let rows = db
        .bars_for_symbol(&symbol, &from, &to)
        .map_err(api_error)?;
    if rows.is_empty() {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "no_bars_for_symbol", "symbol": symbol })),
        ));
    }
    response(json!({
        "symbol": symbol,
        "from": from,
        "to": to,
        "count": rows.len(),
        "bars": rows,
    }))
}

#[derive(Deserialize)]
struct RunRequest {
    day: Option<String>,
}

async fn run(State(state): State<Shared>, Json(request): Json<RunRequest>) -> ApiResult {
    let mut db = state.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    // Prefer the whole-market bar table; fall back to a published reviewed
    // price set only when the bar table has nothing. This keeps the two-symbol
    // reviewed path working while the 5.6k-symbol bar feed becomes the norm.
    let set = match price_set_from_store(&db)? {
        Some(set) => set,
        None => match load_price_sets(&price_sets_dir()).map_err(api_error)? {
            Some(set) => set,
            None => {
                return Err((
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({ "error": "no_price_data",
                                 "detail": "neither the bar table nor a reviewed price set has rows" })),
                ));
            }
        },
    };
    let day = request.day.unwrap_or_else(|| set.coverage_end.clone());
    response(db.advance(&spec, &set, &day).map_err(api_error)?)
}

/// Settlement view built from the bar table, or `None` when it is empty.
///
/// The window starts at the account's last settled day (or the earliest bar
/// we hold) so a long-running account does not rebuild every historical close
/// on each advance.
fn price_set_from_store(db: &Store) -> Result<Option<astock_v2::domain::PriceSet>, (StatusCode, Json<Value>)> {
    let days = db.bar_days().map_err(api_error)?;
    let Some(last_day) = days.last() else {
        return Ok(None);
    };
    let last_settled = match db.account().map_err(api_error)? {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };
    // Start one day after the last settlement; the kernel advances strictly
    // forward and re-running a day is already idempotent.
    let start = match last_settled {
        Some(day) => days
            .iter()
            .find(|candidate| candidate.as_str() > day.as_str())
            .cloned()
            .unwrap_or_else(|| last_day.clone()),
        None => days.first().cloned().unwrap_or_else(|| last_day.clone()),
    };
    db.price_set_from_bars("bar-table", &start, last_day)
        .map_err(api_error)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let database = env::var("ASTOCK_V2_DB").unwrap_or_else(|_| "v2.sqlite3".into());
    let address = env::var("ASTOCK_V2_ADDR").unwrap_or_else(|_| "127.0.0.1:8765".into());
    let store = Store::open(database).map_err(|error| format!("{error:?}"))?;
    let state = Arc::new(Mutex::new(store));
    let app = Router::new()
        .route("/v2/health", get(health))
        .route("/v2/account", get(account).post(create_account))
        .route("/v2/account/run", post(run))
        .route("/v2/account/positions", get(positions))
        .route("/v2/account/trades", get(trades))
        .route("/v2/account/rebalances", get(rebalances))
        .route("/v2/prices/coverage", get(coverage))
        .route("/v2/market/days", get(bar_days))
        .route("/v2/market/bars", get(bars))
        .route("/v2/market/gaps", get(bar_gaps))
        .route("/v2/market/bars/{symbol}", get(symbol_bars))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(&address).await?;
    println!("astock-v2 listening at http://{address}");
    axum::serve(listener, app).await?;
    Ok(())
}
