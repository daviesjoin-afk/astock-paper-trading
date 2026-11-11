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
    let day = db.last_settled_day(&spec.account_id).map_err(api_error)?;
    let set = valuation_set(&db, day.as_deref())?;
    response(db.view(&spec, set.as_ref(), day.as_deref()).map_err(api_error)?)
}

/// One day of closes, for valuing existing positions.
///
/// Loading a settlement window here would pull months of bars just to price a
/// handful of positions; `view` only reads the day it is asked about.
fn valuation_set(
    db: &Store,
    day: Option<&str>,
) -> Result<Option<astock_v2::domain::PriceSet>, (StatusCode, Json<Value>)> {
    if let Some(day) = day
        && let Some(set) = db.price_set_from_bars("bar-table", day, day).map_err(api_error)?
    {
        return Ok(Some(set));
    }
    load_price_sets(&price_sets_dir()).map_err(api_error)
}

async fn positions(State(state): State<Shared>) -> ApiResult {
    let db = state.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    let day = db.last_settled_day(&spec.account_id).map_err(api_error)?;
    let set = valuation_set(&db, day.as_deref())?;
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
    let (set, settle_from) = match price_set_from_store(&db, request.day.as_deref())? {
        Some(input) => (input.set, input.settle_from),
        None => match load_price_sets(&price_sets_dir()).map_err(api_error)? {
            // A reviewed set is already the exact settlement rectangle; there
            // is no warm-up region in it, so settlement starts wherever the
            // account left off.
            Some(set) => (set, None),
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
    response(
        db.advance(&spec, &set, &day, settle_from.as_deref())
            .map_err(api_error)?,
    )
}

/// Diagnostic: how many symbols the settlement view would actually contain.
///
/// Read-only. Exists because "no_price_data" from /account/run is otherwise
/// indistinguishable from "the bar table is empty" -- this says which.
async fn settlement_preview(
    State(state): State<Shared>,
    Query(query): Query<BarsQuery>,
) -> ApiResult {
    let db = state.lock().await;
    let days = db.bar_days().map_err(api_error)?;
    if days.is_empty() {
        return response(json!({ "bar_days": 0, "symbols": 0, "entries": 0 }));
    }
    let last_settled = match db.account().map_err(api_error)? {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };
    let input = price_set_from_store(&db, query.day.as_deref())?;
    response(json!({
        "bar_days": days.len(),
        "first_bar_day": days.first(),
        "last_bar_day": days.last(),
        "last_settled_day": last_settled,
        "window": input.as_ref().map(|i| (i.set.coverage_start.clone(), i.set.coverage_end.clone())),
        "settle_from": input.as_ref().and_then(|i| i.settle_from.clone()),
        "symbols": input.as_ref().map(|i| i.set.symbols().len()).unwrap_or(0),
        "entries": input.as_ref().map(|i| i.set.entries.len()).unwrap_or(0),
        "max_settle_days_per_call": MAX_SETTLE_DAYS_PER_CALL,
        "warmup_days": SETTLEMENT_WARMUP_DAYS,
    }))
}

/// Settlement view built from the bar table, or `None` when it is empty.
///
/// The window starts at the account's last settled day (or the earliest bar
/// we hold) so a long-running account does not rebuild every historical close
/// on each advance.
/// Trading days of history loaded per advance, on top of the days being
/// settled.
///
/// The strategies need at most 61 trailing closes; 120 calendar-ish trading
/// days leaves comfortable headroom for suspensions without dragging years of
/// bars into memory on every request. Long-history work (a real backtest) is a
/// separate job, not something a single advance should quietly pay for.
const SETTLEMENT_WARMUP_DAYS: usize = 120;

/// Cap on how many days one `advance` will settle.
///
/// A request that asks to settle three years in one shot is almost always a
/// mistake, and it would build a settlement view far larger than the rolling
/// window this is designed around. Catch-up runs should be issued per period.
const MAX_SETTLE_DAYS_PER_CALL: usize = 260;

/// The settlement window plus the first day that should actually be settled.
///
/// The two are not the same: the window starts earlier so strategies have
/// trailing closes, and `settle_from` says where real settlement begins.
struct SettlementInput {
    set: astock_v2::domain::PriceSet,
    settle_from: Option<String>,
}

fn price_set_from_store(
    db: &Store,
    through: Option<&str>,
) -> Result<Option<SettlementInput>, (StatusCode, Json<Value>)> {
    let days = db.bar_days().map_err(api_error)?;
    let Some(last_day) = days.last().cloned() else {
        return Ok(None);
    };

    // The window ends at the requested day when it is one we hold, otherwise
    // at the latest day available.
    let end = match through {
        Some(day) if days.iter().any(|candidate| candidate == day) => day.to_string(),
        Some(day) => {
            return Err((
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({ "error": "day_not_in_bar_table", "day": day,
                             "latest": last_day })),
            ));
        }
        None => last_day,
    };

    let last_settled = match db.account().map_err(api_error)? {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };

    // The days this call will actually settle: everything after the last
    // settlement up to `end`.
    //
    // A brand-new account has nothing settled yet, but that does not mean it
    // wants three years of history replayed. It starts at `end` -- one day of
    // settlement -- and real history work (a backtest) is a separate job.
    // Otherwise the very first advance on a fresh account would immediately
    // trip the size guard, which is a confusing way to learn this.
    let settle_from = match &last_settled {
        Some(day) => days
            .iter()
            .find(|candidate| candidate.as_str() > day.as_str())
            .cloned(),
        None => Some(end.clone()),
    };
    let Some(settle_from) = settle_from else {
        // Nothing new to settle. A one-day window keeps `view` able to value
        // existing positions without pretending there is more work.
        let start = days
            .iter()
            .rev()
            .take(1)
            .next()
            .cloned()
            .unwrap_or_else(|| end.clone());
        return Ok(db
            .price_set_from_bars("bar-table", &start, &end)
            .map_err(api_error)?
            .map(|set| SettlementInput { set, settle_from: None }));
    };
    if settle_from.as_str() > end.as_str() {
        return Err((
            StatusCode::CONFLICT,
            Json(json!({ "error": "day_before_last_settled", "last_settled": last_settled })),
        ));
    }

    let to_settle = days
        .iter()
        .filter(|day| day.as_str() >= settle_from.as_str() && day.as_str() <= end.as_str())
        .count();
    if to_settle > MAX_SETTLE_DAYS_PER_CALL {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({
                "error": "advance_too_large",
                "days_requested": to_settle,
                "max_per_call": MAX_SETTLE_DAYS_PER_CALL,
                "hint": "catch up in periods, or run a backtest job instead",
            })),
        ));
    }

    // Load the settlement days plus a bounded warm-up so the strategies have
    // enough trailing closes on the first day settled.
    let from_index = days
        .iter()
        .position(|day| day == &settle_from)
        .unwrap_or(0)
        .saturating_sub(SETTLEMENT_WARMUP_DAYS);
    let start = days[from_index].clone();

    Ok(db
        .price_set_from_bars("bar-table", &start, &end)
        .map_err(api_error)?
        .map(|set| SettlementInput {
            set,
            settle_from: Some(settle_from),
        }))
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
        .route("/v2/market/settlement-preview", get(settlement_preview))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(&address).await?;
    println!("astock-v2 listening at http://{address}");
    axum::serve(listener, app).await?;
    Ok(())
}
