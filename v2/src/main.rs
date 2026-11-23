//! Local-only V2 HTTP gateway. It serves one paper account and two strategies.

use std::{
    env,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
};

use astock_v2::domain::{AiAdjustment, AccountSpec, CreateAccountRequest, SlotSpec, StrategyId};
use astock_v2::intraday::{
    CreateAccountRequest as LiveCreateAccountRequest, IntradayError, IntradayStore,
    RunRoundRequest,
};
use astock_v2::intraday::store::EvolveRequest;
use astock_v2::research_engine::{self, evaluate_candidates};
use astock_v2::settings::{
    self, apply_patch, call_ai_live_advice, load_settings, redacted_view, save_settings,
    test_ai_connection, DaemonRuntimeStatus, SettingsPatch, SystemSettings,
};
use astock_v2::store::{load_price_sets, price_sets_dir, AdvanceLimits, Store, StoreError};

/// Shared settings & long-running daemon status state.
#[derive(Clone)]
struct SettingsAppState {
    path: std::path::PathBuf,
    settings: Arc<Mutex<SystemSettings>>,
    runtime: Arc<Mutex<DaemonRuntimeStatus>>,
}
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::{watch, Mutex};

/// The single writer and an independent read-only connection. Both are
/// mutex-protected because `rusqlite::Connection` is `Send` but not `Sync`;
/// important is that the reader never waits for the writer's long lock.
#[derive(Clone)]
struct AppState {
    writer: Arc<Mutex<Store>>,
    reader: Arc<Mutex<Store>>,
}

/// The listener gives each TCP connection a receiver whose sender is owned by
/// the listener. Dropping the connection task on a client abort closes that
/// receiver, which the handler can poll between chunks.
/// The intraday kernel has its own database and its own writer/reader pair.
/// Nothing here shares a connection, table or transaction with daily settlement.
#[derive(Clone)]
struct LiveAppState {
    writer: Arc<Mutex<IntradayStore>>,
    reader: Arc<Mutex<IntradayStore>>,
}

#[derive(Clone)]
struct ConnectionClosed(watch::Receiver<()>);

impl ConnectionClosed {
    fn get(&self) -> bool {
        // `has_changed()` reports the channel as closed once the connection's
        // sender has been dropped. A healthy connection's sender lives in the
        // stream and leaves this receiver open.
        self.0.has_changed().is_err()
    }
}

/// A TCP stream carrying the per-connection cancellation receiver. Its drop
/// closes the receiver; hyper owns this stream for the lifetime of the
/// connection task.
struct CancellingStream {
    inner: tokio::net::TcpStream,
    /// Keeps the per-connection watch channel open. Dropped with the stream
    /// when hyper closes the connection task.
    _sender: watch::Sender<()>,
    closed: watch::Receiver<()>,
}

impl tokio::io::AsyncRead for CancellingStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_read(cx, buf)
    }
}

impl tokio::io::AsyncWrite for CancellingStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(cx, buf)
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

/// An axum listener that attaches the cancellation receiver to request
/// extensions via `Router::into_make_service_with_connect_info`.
struct CancellingListener {
    inner: tokio::net::TcpListener,
}

impl axum::serve::Listener for CancellingListener {
    type Io = CancellingStream;
    type Addr = std::net::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match self.inner.accept().await {
                Ok((stream, address)) => {
                    let (sender, closed) = watch::channel(());
                    return (
                        CancellingStream {
                            inner: stream,
                            _sender: sender,
                            closed,
                        },
                        address,
                    );
                }
                Err(error) => eprintln!("accept failed: {error}"),
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.inner.local_addr()
    }
}

impl axum::extract::connect_info::Connected<axum::serve::IncomingStream<'_, CancellingListener>>
    for ConnectionClosed
{
    fn connect_info(
        stream: axum::serve::IncomingStream<'_, CancellingListener>,
    ) -> Self {
        ConnectionClosed(stream.io().closed.clone())
    }
}

type ApiResult = Result<Json<Value>, (StatusCode, Json<Value>)>;

fn api_error(error: StoreError) -> (StatusCode, Json<Value>) {
    let (status, message) = match error {
        StoreError::Rules(error) => (StatusCode::UNPROCESSABLE_ENTITY, error.0),
        StoreError::NotFound(error) => (StatusCode::NOT_FOUND, error),
        StoreError::Conflict(error) => (StatusCode::CONFLICT, error),
        StoreError::Aborted(error) => (StatusCode::SERVICE_UNAVAILABLE, error),
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

async fn health(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
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

/// The state digest the AI observer reads, as JSON.
///
/// Everything here is what the engine already knew on `day`: the regime, each
/// slot's standing, and its top scored candidates with their shape numbers.
/// There is deliberately no forward data, so advice produced from this cannot
/// be hindsight.
async fn market_observation(
    State(state): State<AppState>,
    Query(query): Query<BarsQuery>,
) -> ApiResult {
    let db = state.reader.lock().await;
    let days = db.bar_days().map_err(api_error)?;
    let Some(last) = days.last().cloned() else {
        return Err((StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({ "error": "no_market_data" }))));
    };
    let day = query.day.unwrap_or(last);
    let regime = db.market_regime(&day).map_err(api_error)?;
    let from_index = days
        .iter()
        .position(|item| item == &day)
        .unwrap_or(days.len().saturating_sub(1))
        .saturating_sub(SETTLEMENT_WARMUP_DAYS);
    let start = days[from_index].clone();
    let set = db
        .price_set_from_bars("bar-table", &start, &day)
        .map_err(api_error)?
        .ok_or_else(|| (StatusCode::UNPROCESSABLE_ENTITY,
                        Json(json!({ "error": "no_prices_for_day", "day": day }))))?;
    let candidates = db.observation_candidates(&set, &day, 12).map_err(api_error)?;

    let account = db.account().map_err(api_error)?;
    let mut slots = Vec::new();
    if let Some(spec) = &account {
        let curves = db.account_performance(spec, None).map_err(api_error)?;
        let books = db.books(&spec.account_id).map_err(api_error)?;
        for (index, slot) in [astock_v2::domain::StrategyId::Steady,
                              astock_v2::domain::StrategyId::Aggressive].into_iter().enumerate() {
            let last_point = curves[index].last();
            slots.push(json!({
                "slot": slot_cn(slot),
                "slot_id": match slot {
                    astock_v2::domain::StrategyId::Steady => "steady",
                    _ => "aggressive",
                },
                "return_bps": last_point.map(|point| point.return_bps),
                "max_drawdown_bps": last_point.map(|point| point.max_drawdown_bps),
                "positions": books[index].positions.len(),
            }));
        }
    }

    response(json!({
        "day": day,
        "regime": regime.regime.as_str(),
        "regime_separation_bps": regime.separation_bps,
        "regime_breadth_bps": regime.breadth_bps,
        "slots": slots,
        "candidates": candidates,
        "news": [],
        "recent_trades": [],
    }))
}

/// The one paragraph a person reads after settlement.
///
/// Generated from the stored account view rather than from a separate log, so
/// the wording cannot drift from the numbers it describes. A day with no
/// settlement yet is `422` with a reason, not an empty string: the reader has
/// to be able to tell "nothing happened" from "loading failed".
async fn daily_report(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((StatusCode::NOT_FOUND, Json(json!({ "error": "account_not_found" }))));
    };
    let day = db.last_settled_day(&spec.account_id).map_err(api_error)?;
    let Some(day) = day else {
        return Err((StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({ "error": "no_settlement_yet" }))));
    };
    let set = valuation_set(&db, Some(day.as_str()))?;
    let view = db.view(&spec, set.as_ref(), Some(day.as_str())).map_err(api_error)?;
    let trades = db.trades(&spec.account_id, 500).map_err(api_error)?;
    let today: Vec<&astock_v2::domain::Trade> =
        trades.iter().filter(|trade| trade.trade_day == day).collect();

    let mut text = format!("{day} 结算完成。
");
    text.push_str(&format!(
        "账户权益 {} 元，累计收益 {} 元（{}）。
",
        cents_to_yuan(view.equity_cents.unwrap_or(0)),
        cents_to_yuan(view.profit_cents.unwrap_or(0)),
        bps_label(view.return_bps.unwrap_or(0)),
    ));
    if today.is_empty() {
        text.push_str("当日没有成交。
");
    } else {
        text.push_str(&format!("当日成交 {} 笔：
", today.len()));
        for trade in today.iter().take(12) {
            text.push_str(&format!(
                "  {} {} {} {} 股 @ {} 元（{}）
",
                slot_cn(trade.slot),
                if trade.side == "buy" { "买入" } else { "卖出" },
                trade.symbol,
                trade.quantity,
                cents_to_yuan(trade.price_cents),
                trade.reason,
            ));
        }
    }
    let rebalances = db.rebalances(&spec.account_id).map_err(api_error)?;
    let today_rebalance = rebalances.iter().filter(|item| item.applied_day == day).count();
    if today_rebalance > 0 {
        text.push_str(&format!("当日调仓 {today_rebalance} 次（两个策略间资金调配）。
"));
    }
    response(json!({
        "day": day,
        "settled": true,
        "blocked": view.warnings.len(),
        "text": text,
    }))
}

fn cents_to_yuan(cents: i64) -> String {
    format!("{:.2}", cents as f64 / 100.0)
}

fn bps_label(bps: i64) -> String {
    format!("{:+.2}%", bps as f64 / 100.0)
}

fn slot_cn(slot: astock_v2::domain::StrategyId) -> &'static str {
    match slot {
        astock_v2::domain::StrategyId::Steady => "稳健",
        astock_v2::domain::StrategyId::Aggressive => "激进",
    }
}

/// Orders the engine refused to place on the last settled day.
///
/// Read-only. Each row names why: a limit seal, insufficient cash, or a lot
/// size the budget could not reach. Reported rather than silently dropped so a
/// quiet day can be told apart from a broken one.
async fn blocked_orders(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return response(json!([]));
    };
    let day = db.last_settled_day(&spec.account_id).map_err(api_error)?;
    let Some(day) = day else {
        return response(json!([]));
    };
    let set = valuation_set(&db, Some(day.as_str()))?;
    let view = db.view(&spec, set.as_ref(), Some(day.as_str())).map_err(api_error)?;
    let rows: Vec<Value> = view
        .warnings
        .iter()
        .map(|warning| json!({
            "slot": "steady",
            "symbol": "",
            "side": "",
            "quantity": 0,
            "decision_day": day,
            "blocked_day": day,
            "reason": warning,
        }))
        .collect();
    response(rows)
}

/// Research candidates, as the console shows them.
///
/// The kernel has no candidate ledger yet; an empty list is the honest answer
/// and the console renders "还没有影子候选" for it. Returning a fabricated row
/// would make an unimplemented view look working.
async fn research_candidates(State(_state): State<AppState>) -> ApiResult {
    let live_db = env::var("ASTOCK_V2_LIVE_DB")
        .unwrap_or_else(|_| "work/live/intraday.sqlite3".into());
    let path = research_engine::research_store_path(&live_db);
    response(research_engine::load_candidates(&path))
}

async fn run_research(State(state): State<AppState>) -> ApiResult {
    let daily_bars = {
        let db = state.reader.lock().await;
        let days = db.bar_days().map_err(api_error)?;
        let recent_days: Vec<String> = days.into_iter().rev().take(60).collect::<Vec<_>>().into_iter().rev().collect();
        let mut out = Vec::with_capacity(recent_days.len());
        for day in recent_days {
            let bars = db.bars_for_day(&day).map_err(api_error)?;
            out.push((day, bars));
        }
        out
    };
    if daily_bars.len() < 2 {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({ "error": "insufficient_market_bars", "detail": "至少需要 2 个交易日的行情数据才能运行研究评估。" })),
        ));
    }
    let candidates = evaluate_candidates(&daily_bars);
    let live_db = env::var("ASTOCK_V2_LIVE_DB")
        .unwrap_or_else(|_| "work/live/intraday.sqlite3".into());
    let path = research_engine::research_store_path(&live_db);
    if let Err(err) = research_engine::save_candidates(&path, &candidates) {
        return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(json!({ "error": err }))));
    }
    response(json!({
        "count": candidates.len(),
        "candidates": candidates,
    }))
}

async fn get_settings(State(state): State<SettingsAppState>) -> ApiResult {
    let settings = state.settings.lock().await.clone();
    let runtime = state.runtime.lock().await.clone();
    response(redacted_view(&settings, &runtime))
}

async fn update_settings(
    State(state): State<SettingsAppState>,
    Json(patch): Json<SettingsPatch>,
) -> ApiResult {
    let mut settings = state.settings.lock().await;
    let now = chrono::Utc::now().to_rfc3339();
    apply_patch(&mut settings, patch, &now);
    if let Err(err) = save_settings(&state.path, &settings) {
        return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(json!({ "error": err }))));
    }
    let mut runtime = state.runtime.lock().await;
    runtime.daemon_enabled = settings.live_daemon_enabled;
    runtime.phase = if settings.live_daemon_enabled {
        "已开启托管守护（按交易时段自动调度）".into()
    } else {
        "未开启托管（当前由外部 systemd / 手动调度）".into()
    };
    response(redacted_view(&settings, &runtime))
}

#[derive(Deserialize)]
struct TestAiRequest {
    ai_base_url: Option<String>,
    ai_model: Option<String>,
    ai_api_key: Option<String>,
    ai_timeout_secs: Option<u64>,
}

async fn test_ai_settings(
    State(state): State<SettingsAppState>,
    Json(req): Json<TestAiRequest>,
) -> ApiResult {
    let current = state.settings.lock().await.clone();
    let base = req.ai_base_url.unwrap_or(current.ai_base_url);
    let model = req.ai_model.unwrap_or(current.ai_model);
    let timeout = req.ai_timeout_secs.unwrap_or(current.ai_timeout_secs);
    let key = match req.ai_api_key {
        Some(k) if !k.trim().is_empty() && !k.contains("****") => k,
        _ => current.ai_api_key,
    };
    let result = tokio::task::spawn_blocking(move || test_ai_connection(&base, &key, &model, timeout))
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({ "error": e.to_string() }))))?;
    response(result)
}

async fn ai_advice_from_settings(
    State(state): State<SettingsAppState>,
    Json(context): Json<Value>,
) -> ApiResult {
    let settings = state.settings.lock().await.clone();
    if !settings.ai_enabled {
        return response(json!({ "ok": false, "disabled": true, "advice": null }));
    }
    let depth = settings.ai_depth.clone();
    let res = tokio::task::spawn_blocking(move || call_ai_live_advice(&settings, &context))
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({ "error": e.to_string() }))))?;
    match res {
        Ok(advice) => response(json!({ "ok": true, "mode": depth, "advice": advice })),
        Err(err) => response(json!({ "ok": false, "mode": depth, "error": err, "advice": null })),
    }
}

/// Accept an AI advice record for one slot on one day.
///
/// The kernel is the authority here: the record is re-bounded, an untrusted
/// symbol list is filtered against the day's real candidates, and the result --
/// including what had to be reduced -- is stored. Submitting advice never moves
/// a position by itself; settlement reads it on the next advance.
#[derive(Deserialize)]
struct AdviceRequest {
    day: String,
    slot: String,
    bias: String,
    entry_delta: Option<i64>,
    weight_delta_bps: Option<i64>,
    position_delta: Option<i64>,
    #[serde(default)]
    blocked_symbols: Vec<String>,
    confidence: Option<i64>,
    reason: String,
    #[serde(default)]
    source: String,
    #[serde(default)]
    model: String,
}

async fn submit_advice(
    State(state): State<AppState>,
    Json(request): Json<AdviceRequest>,
) -> ApiResult {
    let db = state.writer.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((StatusCode::NOT_FOUND, Json(json!({ "error": "account_not_found" }))));
    };
    if !["steady", "aggressive"].contains(&request.slot.as_str()) {
        return Err((StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({ "error": "unknown_slot", "slot": request.slot }))));
    }
    if request.day.len() != 10 {
        return Err((StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({ "error": "day_must_be_yyyy_mm_dd" }))));
    }
    // Assemble then re-bound. The observer is not trusted to have clamped, and
    // a hand-written POST is not trusted at all.
    let advice = AiAdjustment {
        bias: request.bias,
        entry_delta: request.entry_delta.unwrap_or(0),
        weight_delta_bps: request.weight_delta_bps.unwrap_or(0),
        position_delta: request.position_delta.unwrap_or(0),
        blocked_symbols: request.blocked_symbols,
        confidence: request.confidence.unwrap_or(0),
        reason: request.reason,
        source: if request.source.is_empty() { "http".into() } else { request.source },
        model: request.model,
        clamped: Vec::new(),
    };
    let (bounded, reduced) = advice.bounded();
    db.record_observation(&spec.account_id, &request.day, &request.slot, &bounded, true)
        .map_err(api_error)?;
    response(json!({
        "status": "recorded",
        "day": request.day,
        "slot": request.slot,
        "accepted": bounded.to_dict(),
        "reduced": reduced,
    }))
}

async fn create_account(
    State(state): State<AppState>,
    Json(request): Json<CreateAccountRequest>,
) -> ApiResult {
    let mut db = state.writer.lock().await;
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

async fn account(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
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

async fn positions(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
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

async fn trades(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    response(db.trades(&spec.account_id, 100).map_err(api_error)?)
}

async fn rebalances(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    response(db.rebalances(&spec.account_id).map_err(api_error)?)
}

async fn coverage(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
    let set = load_price_sets(&price_sets_dir()).map_err(api_error)?;
    let last_settled_day = match db.account().map_err(api_error)? {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };
    response(db.coverage(set.as_ref(), last_settled_day))
}

// ---- whole-market bar endpoints -------------------------------------------
//
// These read the `bar` table that `astock-v2 ingest` fills. The
// reviewed-price-set path above is unchanged and still drives settlement; this
// is the read side of the full-market data that the reviewed sets never had.

#[derive(Deserialize)]
struct BarsQuery {
    day: Option<String>,
    /// Defaults to 1100 bps, which clears a ±10% limit plus rounding.
    threshold_bps: Option<i64>,
}

/// Trading days we hold bars for, with the symbol count of the latest day.
/// Market regime as of a day: trending, choppy, declining, or unknown.
///
/// `day` defaults to the latest day held. The verdict is what the aggressive
/// slot actually saw when it settled that day -- same snapshot, same
/// unadjusted closes -- so the console cannot disagree with the trades.
async fn market_regime(State(state): State<AppState>, Query(query): Query<BarsQuery>) -> ApiResult {
    let db = state.reader.lock().await;
    let day = match query.day {
        Some(day) => day,
        None => match db.bar_days().map_err(api_error)?.last().cloned() {
            Some(day) => day,
            None => {
                return Err((
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({ "error": "no_market_data" })),
                ));
            }
        },
    };
    let view = db.market_regime(&day).map_err(api_error)?;
    response(json!({
        "day": day,
        "regime": view.regime.as_str(),
        "label": view.regime.label(),
        "index_points": view.index_points,
        "fast_average_cents": view.fast_average_cents,
        "slow_average_cents": view.slow_average_cents,
        "separation_bps": view.separation_bps,
        "breadth_bps": view.breadth_bps,
    }))
}

async fn bar_days(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
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
async fn bars(State(state): State<AppState>, Query(query): Query<BarsQuery>) -> ApiResult {
    let db = state.reader.lock().await;
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
async fn bar_gaps(State(state): State<AppState>, Query(query): Query<BarsQuery>) -> ApiResult {
    let db = state.reader.lock().await;
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
    // Per-symbol thresholds are applied inside the query; an explicit
    // `threshold_bps` from the caller overrides that and is filtered here.
    let mut rows = db.price_gaps(&day).map_err(api_error)?;
    if let Some(threshold) = query.threshold_bps {
        rows.retain(|bar| {
            bar.prev_close_cents > 0
                && (bar.close_cents - bar.prev_close_cents).abs() * 10_000
                    / bar.prev_close_cents
                    > threshold
        });
    }
    response(json!({
        "day": day,
        // null means "per-board limit + 2%", applied per symbol.
        "threshold_bps": query.threshold_bps,
        "threshold_mode": if query.threshold_bps.is_some() { "explicit" } else { "per_board_limit" },
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
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    Query(query): Query<SymbolBarsQuery>,
) -> ApiResult {
    let db = state.reader.lock().await;
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

async fn run(
    State(state): State<AppState>,
    axum::extract::ConnectInfo(closed): axum::extract::ConnectInfo<ConnectionClosed>,
    request: axum::extract::Request,
) -> Response {
    // Extract the small JSON body before touching the writer lock.
    let (parts, body) = request.into_parts();
    let _ = parts;
    let bytes = match axum::body::to_bytes(body, 64 * 1024).await {
        Ok(bytes) => bytes,
        Err(error) => return api_error_response(StoreError::Integrity(error.to_string())),
    };
    let run_request: RunRequest = match serde_json::from_slice(&bytes) {
        Ok(request) => request,
        Err(error) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({ "error": error.to_string() })),
            )
                .into_response()
        }
    };
    // The resource guards remain in force. A client abort closes the
    // per-connection cancellation receiver; checking it at day boundaries
    // stops the orphan instead of letting it run to the wall-clock deadline.
    let response = run_chunks(state, run_request, closed).await;
    response.into_response()
}

fn api_error_response(error: StoreError) -> Response {
    let (status, body) = api_error(error);
    (status, body).into_response()
}

async fn run_chunks(
    state: AppState,
    request: RunRequest,
    closed: ConnectionClosed,
) -> ApiResult {
    // One wall-clock deadline spans all chunks; `advance_one_chunk` also
    // enforces a per-chunk deadline so a single slow day cannot hold the lock
    // forever.
    let limits = AdvanceLimits::from_env();
    let deadline = tokio::time::Instant::now()
        + tokio::time::Duration::from_secs(limits.max_seconds);
    let mut all_trades: Vec<astock_v2::domain::Trade> = Vec::new();
    let mut all_rebalances: Vec<astock_v2::domain::Rebalance> = Vec::new();
    let mut processed_all: Vec<String> = Vec::new();
    #[allow(unused_assignments)]
    let mut final_result: Option<astock_v2::domain::AdvanceResult> = None;
    let mut input: Option<SettlementInput> = None;
    let mut account_spec: Option<AccountSpec> = None;
    loop {
        let mut db = state.writer.lock().await;
        if account_spec.is_none() {
            let Some(spec) = db.account().map_err(api_error)? else {
                return Err((
                    StatusCode::NOT_FOUND,
                    Json(json!({ "error": "account_not_found" })),
                ));
            };
            account_spec = Some(spec);
        }
        // A persisted catch-up plan (settled day plus 260 target days) is
        // checked against a hard entry cap before indexing. A client that
        // wants the whole catch-up must issue bounded calls; chunking releases
        // the writer lock while one accepted window settles. It does not
        // silently shrink a bad window into a different backtest.
        if input.is_none() {
            input = Some(match price_set_from_store(&db, request.day.as_deref())? {
                Some(input) => input,
                None => match load_price_sets(&price_sets_dir()).map_err(api_error)? {
                    Some(set) => SettlementInput { set, settle_from: None },
                    None => {
                        return Err((
                            StatusCode::UNPROCESSABLE_ENTITY,
                            Json(json!({ "error": "no_price_data",
                                         "detail": "neither the bar table nor a reviewed price set has rows" })),
                        ));
                    }
                },
            });
        }
        let prepared = input.as_ref().expect("settlement input is initialized");
        let day = prepared.set.coverage_end.clone();
        db.advance_limits = limits;
        let should_continue = || !closed.get();
        let result = db.advance_one_chunk(
            account_spec.as_ref().unwrap(),
            &prepared.set,
            &day,
            prepared.settle_from.as_deref(),
            Some(&should_continue),
        );
        drop(db);
        match result {
            Ok(result) => {
                let more = result.processed_days.len() >= limits.chunk_days
                    && !result.processed_days.is_empty()
                    && tokio::time::Instant::now() < deadline;
                processed_all.extend(result.processed_days.iter().cloned());
                all_trades.extend(result.trades.iter().cloned());
                all_rebalances.extend(result.rebalances.iter().cloned());
                final_result = Some(result);
                if !more {
                    break;
                }
                // Yield the writer lock between chunks. A queued read or write
                // gets a fair turn before the next chunk starts.
                tokio::task::yield_now().await;
            }
            Err(StoreError::Aborted(error)) => {
                return Err((
                    StatusCode::SERVICE_UNAVAILABLE,
                    Json(json!({ "error": error })),
                ));
            }
            Err(error) => return Err(api_error(error)),
        }
    }
    let result = final_result.take().expect("one chunk is always attempted");
    let merged = astock_v2::domain::AdvanceResult {
        from_day: result.from_day,
        through_day: result.through_day,
        processed_days: processed_all,
        trades: all_trades,
        rebalances: all_rebalances,
        account: result.account,
    };
    response(merged)
}

/// The profit curve: per-day equity for the account and each slot.
///
/// This is the primary read for the UI. The user's own acceptance criterion is
/// the equity curve, so it is served as its own endpoint rather than derived
/// from `/v2/account` (which is a point-in-time snapshot).
async fn equity_curve(State(state): State<AppState>) -> ApiResult {
    let db = state.reader.lock().await;
    let Some(spec) = db.account().map_err(api_error)? else {
        return Err((
            StatusCode::NOT_FOUND,
            Json(json!({ "error": "account_not_found" })),
        ));
    };
    let [total, steady, aggressive] = db.account_performance(&spec, None).map_err(api_error)?;
    if total.is_empty() {
        return response(json!({
            "account_id": spec.account_id,
            "initial_cash_cents": spec.initial_cash_cents,
            "points": [],
            "summary": null,
        }));
    }

    let points: Vec<Value> = total
        .iter()
        .enumerate()
        .map(|(index, point)| {
            json!({
                "day": point.day,
                "equity_cents": point.equity_cents,
                "steady_cents": steady[index].equity_cents,
                "aggressive_cents": aggressive[index].equity_cents,
                "profit_cents": point.profit_cents,
                "return_bps": point.return_bps,
                "steady_return_bps": steady[index].return_bps,
                "aggressive_return_bps": aggressive[index].return_bps,
            })
        })
        .collect();
    let last = total.last().expect("non-empty");
    let last_steady = steady.last().expect("non-empty");
    let last_aggressive = aggressive.last().expect("non-empty");

    response(json!({
        "account_id": spec.account_id,
        "initial_cash_cents": spec.initial_cash_cents,
        "slot_return_method": "time_weighted_end_of_day_flows",
        "points": points,
        "summary": {
            "first_day": total.first().map(|point| &point.day),
            "last_day": last.day,
            "days": total.len(),
            "equity_cents": last.equity_cents,
            "profit_cents": last.profit_cents,
            "return_bps": last.return_bps,
            "max_drawdown_bps": last.max_drawdown_bps,
            "peak_day": last.peak_day,
            "trough_day": last.trough_day,
            "steady_return_bps": last_steady.return_bps,
            "aggressive_return_bps": last_aggressive.return_bps,
            "steady_cents": last_steady.equity_cents,
            "aggressive_cents": last_aggressive.equity_cents,
        },
    }))
}

/// Diagnostic: how many symbols the settlement view would actually contain.
///
/// Read-only. Exists because "no_price_data" from /account/run is otherwise
/// indistinguishable from "the bar table is empty" -- this says which.
async fn settlement_preview(
    State(state): State<AppState>,
    Query(query): Query<BarsQuery>,
) -> ApiResult {
    let db = state.reader.lock().await;
    let days = db.bar_days().map_err(api_error)?;
    if days.is_empty() {
        return response(json!({ "bar_days": 0, "symbols": 0, "entries": 0 }));
    }
    let last_settled = match db.account().map_err(api_error)? {
        Some(spec) => db.last_settled_day(&spec.account_id).map_err(api_error)?,
        None => None,
    };
    let input = price_set_from_store(&db, query.day.as_deref())?;
    let limits = AdvanceLimits::from_env();
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
        "advance_max_entries": limits.max_entries,
        "advance_max_seconds": limits.max_seconds,
        "advance_chunk_days": limits.chunk_days,
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
        //
        // The window must be anchored on `end`, never on the newest day the
        // table holds. When a caller asks for a day earlier than the last
        // settled one, `end` is that earlier day; taking the table's newest
        // day instead produced `BETWEEN <newer> AND <older>` -- zero rows --
        // and the request failed as `no_price_data` even though prices for
        // `end` existed. `end` is already known to be a day we hold.
        let start = end.clone();
        return Ok(db
            .price_set_from_bars("bar-table", &start, &end)
            .map_err(api_error)?
            .map(|set| SettlementInput { set, settle_from: None }));
    };
    if settle_from.as_str() > end.as_str() {
        return Err((
            StatusCode::CONFLICT,
            Json(json!({ "error": "day_before_last_settled", "last_settled": last_settled,
                         "detail": "the account is already settled past this day; settlement only moves forward" })),
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

    let warmup = SETTLEMENT_WARMUP_DAYS;
    let from_index = days
        .iter()
        .position(|day| day == &settle_from)
        .unwrap_or(0)
        .saturating_sub(warmup);
    let start = days[from_index].clone();

    Ok(db
        .price_set_from_bars("bar-table", &start, &end)
        .map_err(api_error)?
        .map(|set| SettlementInput {
            set,
            settle_from: Some(settle_from),
        }))
}

// ---- independent intraday paper API --------------------------------------

type LiveApiResult = Result<Json<Value>, (StatusCode, Json<Value>)>;

fn live_api_error(error: IntradayError) -> (StatusCode, Json<Value>) {
    let (status, code) = match error {
        IntradayError::Rules(message) => (StatusCode::UNPROCESSABLE_ENTITY, message),
        IntradayError::NotFound(message) => (StatusCode::NOT_FOUND, message),
        IntradayError::Conflict(message) => (StatusCode::CONFLICT, message),
        IntradayError::Database(error) => {
            (StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
        }
        IntradayError::Encoding(error) => {
            (StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
        }
        IntradayError::Integrity(message) => (StatusCode::INTERNAL_SERVER_ERROR, message),
    };
    (status, Json(json!({ "error": code })))
}

fn live_response<T: serde::Serialize>(value: T) -> LiveApiResult {
    serde_json::to_value(value).map(Json).map_err(|error| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({ "error": error.to_string() })),
        )
    })
}

async fn live_create_account(
    State(state): State<LiveAppState>,
    Json(request): Json<LiveCreateAccountRequest>,
) -> LiveApiResult {
    let mut db = state.writer.lock().await;
    live_response(db.create_account(&request).map_err(live_api_error)?)
}

async fn live_account(State(state): State<LiveAppState>) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(db.account_view().map_err(live_api_error)?)
}

async fn live_round(
    State(state): State<LiveAppState>,
    Json(request): Json<RunRoundRequest>,
) -> LiveApiResult {
    let mut db = state.writer.lock().await;
    let outcome = db.run_round(&request).map_err(live_api_error)?;
    live_response(json!({
        "round": outcome.round,
        "decisions": outcome.decisions,
        "trades": outcome.trades,
        "equity": {
            "equity_cents": outcome.round.equity_cents,
            "account": outcome.account,
        },
        "ai": {
            "ok": outcome.round.ai_ok,
            "error": outcome.round.ai_error,
            "application": outcome.ai,
        },
    }))
}

async fn live_rounds(State(state): State<LiveAppState>) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(db.rounds().map_err(live_api_error)?)
}

#[derive(Deserialize)]
struct LiveDecisionQuery {
    round_id: String,
    /// Default true. A round holds ~10 438 rows but only ~2.6% are an order or
    /// a blocked order; sending all of them was 9.1 MB / 3.4 s per refresh.
    /// `signals_only=false` returns the complete audit set.
    #[serde(default)]
    signals_only: Option<bool>,
}

async fn live_decisions(
    State(state): State<LiveAppState>,
    Query(query): Query<LiveDecisionQuery>,
) -> LiveApiResult {
    let db = state.reader.lock().await;
    let signals_only = query.signals_only.unwrap_or(true);
    let (decisions, total) = db
        .decisions_split(&query.round_id, signals_only)
        .map_err(live_api_error)?;
    live_response(json!({
        "round_id": query.round_id,
        "signals_only": signals_only,
        "total": total,
        "returned": decisions.len(),
        "decisions": decisions,
    }))
}

async fn live_trades(State(state): State<LiveAppState>) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(db.trades().map_err(live_api_error)?)
}

async fn live_equity_curve(State(state): State<LiveAppState>) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(db.equity_curve().map_err(live_api_error)?)
}

#[derive(Deserialize)]
struct LiveSymbolQuery {
    symbol: String,
    #[serde(default)]
    limit: Option<usize>,
}

async fn live_symbol_archive(
    State(state): State<LiveAppState>,
    Query(query): Query<LiveSymbolQuery>,
) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(
        db.symbol_archive(&query.symbol, query.limit.unwrap_or(120).clamp(1, 400))
            .map_err(live_api_error)?,
    )
}

#[derive(Deserialize)]
struct LiveRiskQuery {
    #[serde(default)]
    rounds: Option<usize>,
}

async fn live_risk_summary(
    State(state): State<LiveAppState>,
    Query(query): Query<LiveRiskQuery>,
) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(
        db.risk_summary(query.rounds.unwrap_or(40).clamp(1, 200))
            .map_err(live_api_error)?,
    )
}

async fn live_ai_observations(
    State(state): State<LiveAppState>,
    Query(query): Query<LiveRiskQuery>,
) -> LiveApiResult {
    let _ = query.rounds; // unused; limit is fixed below
    let db = state.reader.lock().await;
    live_response(db.ai_observations(120).map_err(live_api_error)?)
}

async fn live_evolution_history(State(state): State<LiveAppState>) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(db.evolution().map_err(live_api_error)?)
}

async fn live_sentiment(State(state): State<LiveAppState>) -> LiveApiResult {
    let db = state.reader.lock().await;
    live_response(db.latest_sentiment().map_err(live_api_error)?)
}

async fn live_evolve(
    State(state): State<LiveAppState>,
    Json(request): Json<EvolveRequest>,
) -> LiveApiResult {
    let mut db = state.writer.lock().await;
    let preview = db.evolve(&request).map_err(live_api_error)?;
    live_response(json!({
        "preview": preview,
        "history": db.evolution().map_err(live_api_error)?,
    }))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli_args: Vec<String> = env::args().skip(1).collect();
    if let Some(code) = astock_v2::cli::maybe_run(&cli_args) {
        std::process::exit(code);
    }
    let database = env::var("ASTOCK_V2_DB").unwrap_or_else(|_| "v2.sqlite3".into());
    let address = env::var("ASTOCK_V2_ADDR").unwrap_or_else(|_| "127.0.0.1:8765".into());
    let mut store = Store::open(&database).map_err(|error| format!("{error:?}"))?;
    // `ASTOCK_V2_REGIME_FILTER=0` replays the pre-regime behaviour so the two
    // can be compared on identical data. Default is on.
    if env::var("ASTOCK_V2_REGIME_FILTER").is_ok_and(|value| value == "0") {
        store.regime_filter = false;
    }
    store.advance_limits = AdvanceLimits::from_env();
    let mut reader = Store::open_read_only(&database).map_err(|error| format!("{error:?}"))?;
    reader.regime_filter = store.regime_filter;
    reader.next_session_fills = store.next_session_fills;
    let state = AppState {
        writer: Arc::new(Mutex::new(store)),
        reader: Arc::new(Mutex::new(reader)),
    };
    let live_database = env::var("ASTOCK_V2_LIVE_DB")
        .unwrap_or_else(|_| "work/live/intraday.sqlite3".into());
    let live_writer = IntradayStore::open(&live_database)
        .map_err(|error| format!("{error}"))?;
    // The live writer initializes WAL and its sidecars; the reader is separate
    // so a long GET never waits on a round's write lock.
    let live_reader = IntradayStore::open_read_only(&live_database)
        .map_err(|error| format!("{error}"))?;
    // Backfill any daily closing OHLCV snapshots from the intraday ledger into
    // the market `bar` table so `/v2/market/*` and `/v2/research/*` stay current.
    if let Ok(live_bars) = live_reader.closing_bars_from_snapshots(None)
        && !live_bars.is_empty()
    {
        let mut db = state.writer.lock().await;
        let _ = db.upsert_bars(&live_bars);
    }
    let live_state = LiveAppState {
        writer: Arc::new(Mutex::new(live_writer)),
        reader: Arc::new(Mutex::new(live_reader)),
    };
    let settings_file = settings::settings_path(&live_database);
    let loaded_settings = load_settings(&settings_file);
    let initial_runtime = DaemonRuntimeStatus {
        daemon_enabled: loaded_settings.live_daemon_enabled,
        running: true,
        os: std::env::consts::OS.to_string(),
        phase: if loaded_settings.live_daemon_enabled {
            "托管守护就绪（等待交易时段）".into()
        } else {
            "手动 / systemd 外部调度模式".into()
        },
        last_heartbeat: Some(chrono::Utc::now().to_rfc3339()),
        last_round_id: None,
        last_error: None,
    };
    let settings_state = SettingsAppState {
        path: settings_file,
        settings: Arc::new(Mutex::new(loaded_settings)),
        runtime: Arc::new(Mutex::new(initial_runtime)),
    };
    // Background heartbeat & long-running daemon status updater for Oracle Cloud / standalone deployments.
    {
        let daemon_settings = settings_state.settings.clone();
        let daemon_runtime = settings_state.runtime.clone();
        let sync_daily_writer = state.writer.clone();
        let sync_live_reader = live_state.reader.clone();
        tokio::spawn(async move {
            let mut last_synced_count = 0usize;
            loop {
                let (enabled, interval_secs) = {
                    let s = daemon_settings.lock().await;
                    (s.live_daemon_enabled, s.live_interval_secs.clamp(30, 1800))
                };
                if let Ok(live_bars) = sync_live_reader.lock().await.closing_bars_from_snapshots(None)
                    && live_bars.len() != last_synced_count
                    && !live_bars.is_empty()
                {
                    last_synced_count = live_bars.len();
                    let _ = sync_daily_writer.lock().await.upsert_bars(&live_bars);
                }
                {
                    let mut rt = daemon_runtime.lock().await;
                    rt.daemon_enabled = enabled;
                    rt.last_heartbeat = Some(chrono::Utc::now().to_rfc3339());
                    if !enabled {
                        rt.phase = "手动 / systemd 外部调度模式".into();
                    } else {
                        rt.phase = format!("托管守护在线（每 {interval_secs} 秒巡检一次）");
                    }
                }
                tokio::time::sleep(std::time::Duration::from_secs(interval_secs.min(30))).await;
            }
        });
    }
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
        .route("/v2/market/regime", get(market_regime))
        .route("/v2/account/equity-curve", get(equity_curve))
        .route("/v2/ai/advice", post(submit_advice))
        .route("/v2/account/daily-report", get(daily_report))
        .route("/v2/account/blocked", get(blocked_orders))
        .route("/v2/research/candidates", get(research_candidates))
        .route("/v2/research/run", post(run_research))
        .route("/v2/market/observation", get(market_observation))
        .with_state(state);
    let live_routes = Router::new()
        .route("/v2/live/account", get(live_account).post(live_create_account))
        .route("/v2/live/round", post(live_round))
        .route("/v2/live/rounds", get(live_rounds))
        .route("/v2/live/decisions", get(live_decisions))
        .route("/v2/live/trades", get(live_trades))
        .route("/v2/live/equity-curve", get(live_equity_curve))
        .route("/v2/live/evolve", post(live_evolve))
        .route("/v2/live/evolution", get(live_evolution_history))
        .route("/v2/live/ai", get(live_ai_observations))
        .route("/v2/live/symbol", get(live_symbol_archive))
        .route("/v2/live/risk", get(live_risk_summary))
        .route("/v2/live/sentiment", get(live_sentiment))
        .with_state(live_state);
    let settings_routes = Router::new()
        .route("/v2/settings", get(get_settings).post(update_settings))
        .route("/v2/settings/test-ai", post(test_ai_settings))
        .route("/v2/settings/ai-advice", post(ai_advice_from_settings))
        .with_state(settings_state);
    let app = app.merge(live_routes).merge(settings_routes);
    let listener = tokio::net::TcpListener::bind(&address).await?;
    println!("astock-v2 listening at http://{address}");
    axum::serve(
        CancellingListener { inner: listener },
        app.into_make_service_with_connect_info::<ConnectionClosed>(),
    )
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_state_read_and_write_locks_are_independent() {
        let writer = Arc::new(Mutex::new(Store::open(":memory:").unwrap()));
        let reader = Arc::new(Mutex::new(Store::open(":memory:").unwrap()));
        let state = AppState { writer, reader };
        let writer_guard = state.writer.blocking_lock();
        // This is the real acceptance condition: holding the writer must not
        // prevent a read-only handler's state from being acquired.
        let reader_guard = state.reader.try_lock().expect("read state must be independent");
        drop(reader_guard);
        drop(writer_guard);
    }
}
