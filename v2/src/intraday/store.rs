//! The sole durable writer for the independent intraday kernel.
//!
//! Every live round uses one SQLite IMMEDIATE transaction: snapshots, all
//! candidate decisions, AI observation, trades, account/position updates,
//! equity point and the round marker commit together. A replayed `round_id`
//! reads its stored outcome and cannot append a second fill.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime};
use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::{
    CreateAccountRequest, MAX_REASON_CHARS, MAX_ROUND_ID_CHARS, MAX_SYMBOL_CHARS,
    MAX_TIME_CHARS, Position, Quote, ResearchEntryPolicy, RunRoundRequest, Slot, SlotBook, apply_buy, apply_sell,
    decision::{AiApplication, DecisionSignal, FillBaseline, SignalBaseline, proposal_json, proposal_within_bounds},
    fee_cents, labels, split_initial_cash, validate_advice_shape,
    decision::score_candidates_for_research,
};

#[derive(Debug)]
pub enum IntradayError {
    Database(rusqlite::Error),
    Encoding(serde_json::Error),
    Rules(String),
    NotFound(String),
    Conflict(String),
    Integrity(String),
}

impl From<rusqlite::Error> for IntradayError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Database(error)
    }
}

impl From<serde_json::Error> for IntradayError {
    fn from(error: serde_json::Error) -> Self {
        Self::Encoding(error)
    }
}

impl std::fmt::Display for IntradayError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Database(error) => write!(formatter, "database: {error}"),
            Self::Encoding(error) => write!(formatter, "encoding: {error}"),
            Self::Rules(error) => write!(formatter, "rules: {error}"),
            Self::NotFound(error) => write!(formatter, "not_found: {error}"),
            Self::Conflict(error) => write!(formatter, "conflict: {error}"),
            Self::Integrity(error) => write!(formatter, "integrity: {error}"),
        }
    }
}

impl std::error::Error for IntradayError {}

pub type IntradayResult<T> = Result<T, IntradayError>;

const INTRADAY_SCHEMA_VERSION: i64 = 1;

/// One fresh account, two slot books and the current equity view.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AccountView {
    pub id: String,
    pub initial_cash_cents: i64,
    pub cash_cents: i64,
    pub created_at: String,
    pub as_of_round_id: Option<String>,
    pub equity_cents: Option<i64>,
    pub profit_cents: Option<i64>,
    #[serde(default)]
    pub daily_pnl_cents: Option<i64>,
    #[serde(default)]
    pub daily_pnl_day: Option<String>,
    pub return_bps: Option<i64>,
    pub slots: Vec<SlotView>,
    pub positions: Vec<Position>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SlotView {
    pub slot: Slot,
    pub cash_cents: i64,
    pub market_value_cents: Option<i64>,
    pub equity_cents: Option<i64>,
    pub positions: usize,
    pub sellable_positions: usize,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StoredDecision {
    pub round_id: String,
    pub slot: Slot,
    pub symbol: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub side_label: String,
    #[serde(default)]
    pub blocked_label: String,
    #[serde(default)]
    pub slot_label: String,
    pub score: i64,
    pub threshold: i64,
    pub reached_threshold: bool,
    pub side: String,
    pub blocked_reason: Option<String>,
    pub ai_bias: i64,
    pub ai_confidence: i64,
    pub ai_applied: bool,
    pub factors_json: Value,
    pub note: String,
    pub created_at: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TradeRecord {
    pub id: i64,
    pub round_id: String,
    pub slot: Slot,
    pub symbol: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub side_label: String,
    #[serde(default)]
    pub reason_label: String,
    pub side: String,
    pub quantity: i64,
    pub price_cents: i64,
    pub fee_cents: i64,
    pub reason: String,
    pub ai_applied: bool,
    pub created_at: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RoundSummary {
    pub round_id: String,
    pub started_at: String,
    pub finished_at: String,
    pub quote_count: usize,
    pub trade_count: usize,
    pub ai_ok: bool,
    pub ai_error: Option<String>,
    pub equity_cents: i64,
    pub regime: String,
    #[serde(default)]
    pub regime_label: String,
    #[serde(default)]
    pub ai_label: String,
    /// Original audit note, never rewritten.
    pub note: String,
    #[serde(default)]
    pub note_label: String,
    pub idempotent: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RoundOutcome {
    pub round: RoundSummary,
    pub decisions: Vec<StoredDecision>,
    pub trades: Vec<TradeRecord>,
    pub account: AccountView,
    pub ai: AiApplication,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EquityPoint {
    pub round_id: String,
    pub quote_time: String,
    pub finished_at: String,
    pub equity_cents: i64,
    pub profit_cents: i64,
    pub return_bps: i64,
    pub steady_equity_cents: i64,
    pub aggressive_equity_cents: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EvolutionPreview {
    pub day: String,
    pub proposal: Value,
    pub before_metrics: Value,
    pub after_metrics: Value,
    pub accepted: bool,
    pub reason: String,
    pub created_at: String,
}

/// One AI observation for the console's AI view. `raw_advice` is the model's
/// verbatim JSON; `clamped_fields` names every value Rust had to bound.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AiObservation {
    pub round_id: String,
    pub started_at: String,
    pub model: String,
    pub raw_advice: String,
    pub accepted: bool,
    pub clamped: bool,
    pub confidence: i64,
    pub reason: String,
    pub clamped_fields: Value,
    pub signal_baseline: Option<SignalBaseline>,
}

/// One trade/decision row inside the per-symbol archive.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SymbolArchiveEvent {
    pub round_id: String,
    pub started_at: String,
    pub kind: String,
    pub slot: String,
    pub slot_label: String,
    pub side: String,
    pub side_label: String,
    pub quantity: i64,
    pub price_cents: Option<i64>,
    pub score: Option<i64>,
    pub blocked_label: String,
    pub note: String,
}

/// Per-symbol archive: every round that scored the symbol plus every fill.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SymbolArchive {
    pub symbol: String,
    pub name: Option<String>,
    pub trade_count: usize,
    pub buy_quantity: i64,
    pub sell_quantity: i64,
    pub realized_fee_cents: i64,
    pub decision_count: usize,
    pub events: Vec<SymbolArchiveEvent>,
}

/// One risk counter: why decided orders did not fill.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RiskCounter {
    pub reason: String,
    pub label: String,
    pub count: i64,
}

/// Risk center snapshot: blocked-decision counters over a bounded window.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RiskSummary {
    pub window_rounds: usize,
    pub blocked_total: i64,
    pub counters: Vec<RiskCounter>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvolveRequest {
    pub day: String,
    pub created_at: String,
    /// Optional caller-supplied closed-day summary. Missing fields are computed
    /// from persisted rounds for that day; supplied values are audited, not
    /// trusted for policy activation.
    #[serde(default)]
    pub summary: Option<Value>,
    /// A caller cannot turn a proposal on. Built-in validation is the only
    /// accepted gate; even its successful result remains a non-active
    /// proposal until a separate reviewed implementation exists.
    #[serde(default)]
    pub accept: Option<bool>,
}

enum SignalInput<'a> {
    Current,
    Prior(Option<&'a RunRoundRequest>),
}

pub struct IntradayStore {
    pub conn: Connection,
}

impl IntradayStore {
    /// Open (or create) a separate live database. `PRAGMA user_version` is
    /// checked for legacy V1/V2 files so no old table is migrated in place.
    pub fn open(path: impl AsRef<Path>) -> IntradayResult<Self> {
        if path.as_ref() != Path::new(":memory:")
            && let Some(parent) = path.as_ref().parent()
            && !parent.as_os_str().is_empty()
        {
            std::fs::create_dir_all(parent)
                .map_err(|error| IntradayError::Integrity(error.to_string()))?;
        }
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.pragma_update(None, "busy_timeout", 5_000)?;
        // The default page cache is 2 MB, which is far too small for a round
        // that writes 5 219 quotes and 10 438 decisions every three minutes:
        // every round re-read the same B-tree pages from disk. 64 MB is well
        // inside the server's headroom and removes that churn.
        conn.pragma_update(None, "cache_size", -64_000)?;
        // NORMAL is the WAL-safe setting: a committed transaction survives a
        // process crash and only an OS crash can lose the tail, which for a
        // paper ledger is an acceptable trade for one fewer fsync per round.
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        // Keep the WAL bounded. The default 1000-page auto-checkpoint let a
        // single session reach 35 MB of WAL; 4000 pages (~16 MB) checkpoints
        // often enough to stay small without stalling a round.
        conn.pragma_update(None, "wal_autocheckpoint", 4_000)?;
        let store = Self { conn };
        store.migrate()?;
        Ok(store)
    }

    /// Read-only connection to an already initialized intraday database.
    pub fn open_read_only(path: impl AsRef<Path>) -> IntradayResult<Self> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        let store = Self { conn };
        let version: i64 = store
            .conn
            .pragma_query_value(None, "user_version", |row| row.get(0))?;
        if version != INTRADAY_SCHEMA_VERSION {
            return Err(IntradayError::Integrity(format!(
                "unsupported_intraday_schema:{version}"
            )));
        }
        Ok(store)
    }

    fn migrate(&self) -> IntradayResult<()> {
        let legacy: i64 = self
            .conn
            .pragma_query_value(None, "user_version", |row| row.get(0))?;
        if legacy != 0 && legacy != INTRADAY_SCHEMA_VERSION {
            return Err(IntradayError::Integrity(format!(
                "unsupported_intraday_schema:{legacy}; use a fresh database"
            )));
        }
        // A legacy daily database has differently shaped tables with some of
        // the same names. Refuse it before CREATE TABLE or any migration, so
        // opening an old path never reinterprets its rows as live state.
        let legacy_table: Option<String> = self.conn.query_row(
            "SELECT name FROM sqlite_master WHERE type='table' \
             AND name IN ('day_run','slot_equity_day','schema_version','bar') LIMIT 1",
            [],
            |row| row.get(0),
        ).optional()?;
        if let Some(table) = legacy_table {
            return Err(IntradayError::Integrity(format!(
                "daily_schema_detected:{table}; use a fresh ASTOCK_V2_LIVE_DB"
            )));
        }
        self.conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS intraday_schema(version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS account(
                id TEXT PRIMARY KEY,
                initial_cash_cents INTEGER NOT NULL,
                cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS slot_book(
                slot TEXT PRIMARY KEY,
                cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0)
            );
            CREATE TABLE IF NOT EXISTS position(
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity >= 0),
                sellable_quantity INTEGER NOT NULL CHECK(sellable_quantity >= 0),
                avg_cost_cents INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(slot, symbol),
                CHECK(sellable_quantity <= quantity)
            );
            CREATE TABLE IF NOT EXISTS trade(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                fee_cents INTEGER NOT NULL,
                reason TEXT NOT NULL,
                ai_applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(round_id, slot, symbol, side)
            );
            CREATE TABLE IF NOT EXISTS quote_snapshot(
                round_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                price_cents INTEGER NOT NULL,
                prev_close_cents INTEGER NOT NULL,
                open_cents INTEGER,
                high_cents INTEGER,
                low_cents INTEGER,
                limit_up_cents INTEGER,
                limit_down_cents INTEGER,
                volume INTEGER,
                amount INTEGER,
                quote_time TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(round_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS decision(
                round_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                score INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                reached_threshold INTEGER NOT NULL,
                side TEXT NOT NULL,
                blocked_reason TEXT,
                ai_bias INTEGER NOT NULL,
                ai_confidence INTEGER NOT NULL,
                ai_applied INTEGER NOT NULL,
                factors_json TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(round_id, slot, symbol)
            );
            CREATE TABLE IF NOT EXISTS round(
                round_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                quote_count INTEGER NOT NULL,
                trade_count INTEGER NOT NULL,
                ai_ok INTEGER NOT NULL,
                ai_error TEXT,
                equity_cents INTEGER NOT NULL,
                regime TEXT NOT NULL,
                note TEXT NOT NULL,
                account_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_observation(
                round_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                raw_advice TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                clamped INTEGER NOT NULL,
                confidence INTEGER NOT NULL,
                reason TEXT NOT NULL,
                clamped_fields TEXT NOT NULL,
                application_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evolution(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                proposal TEXT NOT NULL,
                before_metrics TEXT NOT NULL,
                after_metrics TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity_point(
                round_id TEXT PRIMARY KEY,
                quote_time TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                equity_cents INTEGER NOT NULL,
                profit_cents INTEGER NOT NULL,
                return_bps INTEGER NOT NULL,
                steady_equity_cents INTEGER NOT NULL,
                aggressive_equity_cents INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trading_day(
                day TEXT PRIMARY KEY,
                rolled_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS decision_by_round ON decision(round_id);
            CREATE INDEX IF NOT EXISTS trade_by_round ON trade(round_id);
            CREATE INDEX IF NOT EXISTS quote_by_round ON quote_snapshot(round_id);
            -- Account reads resolve the latest mark per held symbol; without
            -- this the window query scans every snapshot row ever written.
            CREATE INDEX IF NOT EXISTS quote_by_symbol ON quote_snapshot(symbol, round_id);
            -- The per-symbol archive reads the newest decisions for one symbol.
            -- Without this, that query scans the whole decision table (83万行/天
            -- at 5219 symbols x ~80 rounds) and took 125ms on the first day.
            CREATE INDEX IF NOT EXISTS decision_by_symbol
                ON decision(symbol, round_id);
            -- The risk centre groups blocked decisions over a round window.
            CREATE INDEX IF NOT EXISTS decision_by_blocked
                ON decision(blocked_reason) WHERE blocked_reason IS NOT NULL;
            "#,
        )?;
        // A database created before quote names existed keeps its historical
        // rows NULL. Add the display column only when it is actually missing;
        // re-opening a migrated database is a no-op.
        let has_quote_name: bool = {
            let mut statement = self.conn.prepare("PRAGMA table_info(quote_snapshot)")?;
            let columns = statement
                .query_map([], |row| row.get::<_, String>(1))?
                .collect::<Result<Vec<_>, _>>()?;
            columns.iter().any(|column| column == "name")
        };
        if !has_quote_name {
            self.conn
                .execute_batch("ALTER TABLE quote_snapshot ADD COLUMN name TEXT;")?;
        }
        let rows: i64 = self.conn.query_row(
            "SELECT count(*) FROM intraday_schema",
            [],
            |row| row.get(0),
        )?;
        if rows == 0 {
            self.conn.execute(
                "INSERT INTO intraday_schema(version) VALUES(?)",
                params![INTRADAY_SCHEMA_VERSION],
            )?;
            self.conn
                .pragma_update(None, "user_version", INTRADAY_SCHEMA_VERSION)?;
        } else {
            let version: i64 = self.conn.query_row(
                "SELECT version FROM intraday_schema LIMIT 1",
                [],
                |row| row.get(0),
            )?;
            if version != INTRADAY_SCHEMA_VERSION {
                return Err(IntradayError::Integrity(format!(
                    "unsupported_intraday_schema:{version}"
                )));
            }
        }
        Ok(())
    }

    /// Create the one fresh paper account. This is a deliberately separate
    /// single-row reset from the daily kernel's account creation path.
    pub fn create_account(&mut self, request: &CreateAccountRequest) -> IntradayResult<AccountView> {
        let [steady, aggressive] = split_initial_cash(request.initial_cash_cents)?;
        let created_at = bounded_text(
            request
                .created_at
                .as_deref()
                .unwrap_or("caller_timestamp_required"),
            MAX_TIME_CHARS,
        );
        let transaction = self.conn.transaction_with_behavior(
            rusqlite::TransactionBehavior::Immediate,
        )?;
        let existing: Option<String> = transaction
            .query_row("SELECT id FROM account LIMIT 1", [], |row| row.get(0))
            .optional()?;
        if existing.is_some() {
            return Err(IntradayError::Conflict("account_already_exists".into()));
        }
        transaction.execute(
            "INSERT INTO account(id, initial_cash_cents, cash_cents, created_at) VALUES(?,?,?,?)",
            params![
                "live-paper-account",
                request.initial_cash_cents,
                request.initial_cash_cents,
                created_at
            ],
        )?;
        transaction.execute(
            "INSERT INTO slot_book(slot, cash_cents) VALUES('steady', ?), ('aggressive', ?)",
            params![steady, aggressive],
        )?;
        transaction.commit()?;
        self.account_view()
    }

    /// Move yesterday's bought shares into `sellable_quantity`. This is an
    /// explicit caller-owned day boundary, never a wall-clock inference.
    pub fn roll_trading_day(&mut self, day: &str, at: &str) -> IntradayResult<usize> {
        let day = require_bounded(day, "day", MAX_TIME_CHARS)?;
        let at = require_bounded(at, "at", MAX_TIME_CHARS)?;
        let transaction = self.conn.transaction_with_behavior(
            rusqlite::TransactionBehavior::Immediate,
        )?;
        ensure_account(&transaction)?;
        let already: Option<String> = transaction
            .query_row(
                "SELECT day FROM trading_day WHERE day=?",
                params![day],
                |row| row.get(0),
            )
            .optional()?;
        if already.is_some() {
            return Err(IntradayError::Conflict("day_already_rolled".into()));
        }
        let count = transaction.execute(
            "UPDATE position SET sellable_quantity=quantity, updated_at=? WHERE quantity>0",
            params![at],
        )?;
        transaction.execute(
            "INSERT INTO trading_day(day, rolled_at) VALUES(?,?)",
            params![day, at],
        )?;
        transaction.commit()?;
        Ok(count)
    }

    /// Execute exactly one round. Advice is clamped here, then active values
    /// are passed to decision scoring and order sizing. Replays return the
    /// stored result without writing any new quote, decision, trade or equity.
    pub fn run_round(&mut self, request: &RunRoundRequest) -> IntradayResult<RoundOutcome> {
        self.run_with_policy(request, ResearchEntryPolicy::Baseline, SignalInput::Current)
    }

    /// Apply an experimental rule only on a non-durable ledger.
    pub fn run_research_round(
        &mut self,
        request: &RunRoundRequest,
        policy: ResearchEntryPolicy,
    ) -> IntradayResult<RoundOutcome> {
        self.validate_research_policy(policy)?;
        self.run_with_policy(request, policy, SignalInput::Current)
    }

    /// Price orders at the next snapshot using only the previous round's
    /// signal inputs. The opening snapshot starts each session without orders.
    pub fn run_forward_research_round(
        &mut self,
        request: &RunRoundRequest,
        prior: Option<&RunRoundRequest>,
        policy: ResearchEntryPolicy,
    ) -> IntradayResult<RoundOutcome> {
        self.validate_research_policy(policy)?;
        if let Some(prior) = prior {
            let clock = |input: &RunRoundRequest| {
                chrono::DateTime::parse_from_rfc3339(&input.quote_time)
                    .map_err(|_| IntradayError::Rules("invalid_research_quote_time".into()))
            };
            let previous = clock(prior)?;
            let current = clock(request)?;
            if current <= previous || prior.quote_time.get(..10) != request.quote_time.get(..10) {
                return Err(IntradayError::Rules("research_requires_later_same_day_execution".into()));
            }
        }
        self.run_with_policy(request, policy, SignalInput::Prior(prior))
    }

    fn validate_research_policy(&self, policy: ResearchEntryPolicy) -> IntradayResult<()> {
        if self.conn.path() != Some("") {
            return Err(IntradayError::Rules("research_requires_in_memory_ledger".into()));
        }
        match policy {
            ResearchEntryPolicy::WeakEntryBuffer { points }
            | ResearchEntryPolicy::StrongEntryRelief { points } if !(1..=15).contains(&points) => {
                return Err(IntradayError::Rules("research_entry_points_out_of_range".into()));
            }
            _ => {}
        }
        Ok(())
    }

    fn run_with_policy(
        &mut self,
        request: &RunRoundRequest,
        policy: ResearchEntryPolicy,
        input: SignalInput<'_>,
    ) -> IntradayResult<RoundOutcome> {
        validate_round_request(request)?;
        if let Some(outcome) = self.stored_round_outcome(&request.round_id)? {
            return Ok(mark_idempotent(outcome));
        }
        let retain_full_audit = matches!(&input, SignalInput::Current);
        let (signal_quotes, advice) = match input {
            SignalInput::Current => (Some(request.quotes.as_slice()), request.advice.clone()),
            SignalInput::Prior(prior) => (prior.map(|round| round.quotes.as_slice()),
                                          prior.and_then(|round| round.advice.clone())),
        };
        let ai = super::apply_ai_advice(&request.round_id, advice);
        let transaction = self.conn.transaction_with_behavior(
            rusqlite::TransactionBehavior::Immediate,
        )?;
        // A concurrent POST with the same round_id waits on the writer; after
        // the lock is acquired, recheck rather than relying on a race-free UNIQUE.
        if let Some(outcome) = stored_round_outcome_tx(&transaction, &request.round_id)? {
            transaction.rollback()?;
            return Ok(mark_idempotent(outcome));
        }
        let account = ensure_account(&transaction)?;
        let mut books = read_books(&transaction)?;
        let outcome = run_round_tx(
            &transaction,
            request,
            &ai,
            &account,
            &mut books,
            policy,
            signal_quotes,
            retain_full_audit,
        )?;
        transaction.commit()?;
        Ok(outcome)
    }

    fn stored_round_outcome(
        &self,
        round_id: &str,
    ) -> IntradayResult<Option<RoundOutcome>> {
        let exists: Option<String> = self
            .conn
            .query_row(
                "SELECT round_id FROM round WHERE round_id=?",
                params![round_id],
                |row| row.get(0),
            )
            .optional()?;
        if exists.is_none() {
            return Ok(None);
        }
        let transaction = self.conn.unchecked_transaction()?;
        let outcome = stored_round_outcome_tx(&transaction, round_id)?;
        Ok(outcome)
    }

    /// The read model describes the live books, so its identity is the newest
    /// committed round. The AI digest uses this date to tell "today's ledger"
    /// (T+1 blocks every sale) from "yesterday's, rolled at the open"; leaving
    /// it NULL made the model report the ledger date as unknown every round.
    pub fn account_view(&self) -> IntradayResult<AccountView> {
        let latest: Option<String> = self
            .conn
            .query_row(
                "SELECT round_id FROM round ORDER BY rowid DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()?;
        self.account_view_as_of(latest.as_deref())
    }

    pub fn account_view_as_of(
        &self,
        as_of_round_id: Option<&str>,
    ) -> IntradayResult<AccountView> {
        let snapshot = self.conn.unchecked_transaction()?;
        let account = ensure_account(&snapshot)?;
        let books = read_books(&snapshot)?;
        // Only the held symbols need a mark. Ranking the whole snapshot archive
        // makes this read grow with every round ever run.
        let held: Vec<String> = books
            .iter()
            .flat_map(|book| book.positions.iter().map(|position| position.symbol.clone()))
            .collect();
        let (prices, names) = latest_quotes(&snapshot, &held)?;
        // A position's name is the most recent real name for its symbol. The
        // name map already excludes blanks and placeholders; absent stays NULL.
        let position_names: BTreeMap<String, Option<String>> = books
            .iter()
            .flat_map(|book| book.positions.iter().map(|position| position.symbol.clone()))
            .map(|symbol| {
                let name = names.get(&symbol).cloned().unwrap_or(None);
                (symbol, name)
            })
            .collect();
        let mut view = account_view_from(
            &account,
            &books,
            &prices,
            &position_names,
            as_of_round_id,
        );
        set_daily_pnl(&snapshot, &mut view)?;
        Ok(view)
    }

    pub fn decisions(&self, round_id: &str) -> IntradayResult<Vec<StoredDecision>> {
        let decisions = read_decisions_tx(&self.conn, round_id)?;
        decorate_decisions(&self.conn, round_id, None, decisions)
    }

    /// Decisions for one round, split into what a reader needs and the bulk.
    ///
    /// One round writes 10 438 rows (5 219 symbols x 2 slots) and only ~2.6%
    /// of them are an order or a blocked order; the rest are plain `hold`.
    /// Shipping all of them cost 9.1 MB and 3.4 s per request, and the console
    /// re-fetched that every 30 seconds. `signals_only` keeps the rows that
    /// carry a decision (any non-`hold` side, or a blocked reason) and reports
    /// the full count separately so the reader still sees "273 / 10 438".
    pub fn decisions_split(
        &self,
        round_id: &str,
        signals_only: bool,
    ) -> IntradayResult<(Vec<StoredDecision>, usize)> {
        let all = read_decisions_tx(&self.conn, round_id)?;
        let total = all.len();
        let kept = if signals_only {
            all.into_iter()
                .filter(|item| item.side != "hold" || item.blocked_reason.is_some())
                .collect()
        } else {
            all
        };
        let decorated = decorate_decisions(&self.conn, round_id, None, kept)?;
        Ok((decorated, total))
    }

    pub fn trades(&self) -> IntradayResult<Vec<TradeRecord>> {
        let mut statement = self.conn.prepare(
            "SELECT id, round_id, slot, symbol, side, quantity, price_cents, fee_cents, \
             reason, ai_applied, created_at FROM trade ORDER BY id DESC LIMIT 1000",
        )?;
        let rows = statement.query_map([], map_trade)?;
        let trades = rows.collect::<Result<Vec<_>, _>>()?;
        decorate_trades(&self.conn, None, trades)
    }

    pub fn rounds(&self) -> IntradayResult<Vec<RoundSummary>> {
        self.rounds_limited(ROUND_LIST_LIMIT)
    }

    /// Newest `limit` rounds, oldest first (the order the console expects).
    ///
    /// `round` is ledger history and is never pruned, so after a year it holds
    /// ~20 000 rows and the console was re-fetching all of them every 30
    /// seconds (10.5 MB per request). The scheduler view and the round picker
    /// only ever look at recent rounds; a bounded window keeps the payload flat
    /// without losing any stored history.
    pub fn rounds_limited(&self, limit: usize) -> IntradayResult<Vec<RoundSummary>> {
        let limit = limit.clamp(1, 5_000) as i64;
        let mut statement = self.conn.prepare(
            "SELECT round_id, started_at, finished_at, quote_count, trade_count, ai_ok, \
             ai_error, equity_cents, regime, note FROM ( \
                 SELECT round_id, started_at, finished_at, quote_count, trade_count, ai_ok, \
                        ai_error, equity_cents, regime, note FROM round \
                 ORDER BY started_at DESC, round_id DESC LIMIT ? \
             ) ORDER BY started_at, round_id",
        )?;
        let rows = statement.query_map(params![limit], map_round)?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    pub fn equity_curve(&self) -> IntradayResult<Vec<EquityPoint>> {
        let mut statement = self.conn.prepare(
            "SELECT round_id, quote_time, finished_at, equity_cents, profit_cents, \
             return_bps, steady_equity_cents, aggressive_equity_cents \
             FROM equity_point ORDER BY rowid",
        )?;
        let rows = statement.query_map([], |row| {
            Ok(EquityPoint {
                round_id: row.get(0)?,
                quote_time: row.get(1)?,
                finished_at: row.get(2)?,
                equity_cents: row.get(3)?,
                profit_cents: row.get(4)?,
                return_bps: row.get(5)?,
                steady_equity_cents: row.get(6)?,
                aggressive_equity_cents: row.get(7)?,
            })
        })?;
        let points = rows.collect::<Result<Vec<_>, _>>()?;
        Ok(downsample_equity(points, EQUITY_CURVE_POINTS))
    }

    /// AI observations, newest first. Bounded window: a full session is ~80
    /// rounds, so 160 covers one day plus carry-over without growing forever.
    pub fn ai_observations(&self, limit: usize) -> IntradayResult<Vec<AiObservation>> {
        let limit = limit.clamp(1, 500);
        let mut statement = self.conn.prepare(
            "SELECT o.round_id, COALESCE(r.started_at, ''), o.model, o.raw_advice, \
             o.accepted, o.clamped, o.confidence, o.reason, o.clamped_fields, o.application_json \
             FROM ai_observation o LEFT JOIN round r ON r.round_id = o.round_id \
             ORDER BY COALESCE(r.started_at, o.round_id) DESC LIMIT ?",
        )?;
        let rows = statement.query_map(params![limit as i64], |row| {
            Ok(AiObservation {
                round_id: row.get(0)?,
                started_at: row.get(1)?,
                model: row.get(2)?,
                raw_advice: row.get(3)?,
                accepted: row.get::<_, i64>(4)? != 0,
                clamped: row.get::<_, i64>(5)? != 0,
                confidence: row.get(6)?,
                reason: row.get(7)?,
                clamped_fields: serde_json::from_str(&row.get::<_, String>(8)?)
                    .unwrap_or(Value::Null),
                signal_baseline: serde_json::from_str::<AiApplication>(&row.get::<_, String>(9)?)
                    .ok()
                    .and_then(|application| application.signal_baseline),
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    /// Per-symbol archive: the symbol's fills and the recent decisions that
    /// mention it, newest first. This is V1's 个股档案, rebuilt on the
    /// intraday ledger's own tables — no cross-ledger join, no fabrication.
    pub fn symbol_archive(&self, symbol: &str, decision_limit: usize) -> IntradayResult<SymbolArchive> {
        let symbol = require_bounded(symbol, "symbol", super::MAX_SYMBOL_CHARS)?;
        let decision_limit = decision_limit.clamp(1, 400);
        let name: Option<String> = self
            .conn
            .query_row(
                "SELECT name FROM quote_snapshot WHERE symbol=? AND name IS NOT NULL \
                 AND TRIM(name) <> '' AND TRIM(name) <> '-' AND TRIM(name) <> 'None' \
                 ORDER BY rowid DESC LIMIT 1",
                params![symbol],
                |row| row.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten()
            .and_then(|name| sanitized_name(Some(name.as_str())));

        let mut statement = self.conn.prepare(
            "SELECT round_id, slot, side, quantity, price_cents, fee_cents, reason, created_at \
             FROM trade WHERE symbol=? ORDER BY id DESC LIMIT 200",
        )?;
        let trades = statement
            .query_map(params![symbol], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;

        let mut statement = self.conn.prepare(
            "SELECT d.round_id, d.slot, d.side, d.score, d.blocked_reason, d.note \
             FROM decision d WHERE d.symbol=? ORDER BY d.rowid DESC LIMIT ?",
        )?;
        let decisions = statement
            .query_map(params![symbol, decision_limit as i64], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, String>(5)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;

        // started_at per round for display; rounds absent from the round table
        // (in-flight replay) keep an empty string rather than a guess.
        let started = |round_id: &str| -> String {
            self.conn
                .query_row(
                    "SELECT started_at FROM round WHERE round_id=?",
                    params![round_id],
                    |row| row.get::<_, String>(0),
                )
                .optional()
                .ok()
                .flatten()
                .unwrap_or_default()
        };

        let mut events: Vec<SymbolArchiveEvent> = Vec::new();
        let mut buy_quantity = 0i64;
        let mut sell_quantity = 0i64;
        let mut realized_fee_cents = 0i64;
        for (round_id, slot, side, quantity, price, fee, reason, _created) in &trades {
            buy_quantity += if side == "buy" { *quantity } else { 0 };
            sell_quantity += if side == "sell" { *quantity } else { 0 };
            realized_fee_cents += fee;
            events.push(SymbolArchiveEvent {
                round_id: round_id.clone(),
                started_at: started(round_id),
                kind: "trade".into(),
                slot: slot.clone(),
                slot_label: labels::slot_label(slot),
                side: side.clone(),
                side_label: labels::decision_side_label(side),
                quantity: *quantity,
                price_cents: Some(*price),
                score: None,
                blocked_label: labels::trade_reason_label(reason),
                note: reason.clone(),
            });
        }
        for (round_id, slot, side, score, blocked, note) in &decisions {
            events.push(SymbolArchiveEvent {
                round_id: round_id.clone(),
                started_at: started(round_id),
                kind: "decision".into(),
                slot: slot.clone(),
                slot_label: labels::slot_label(slot),
                side: side.clone(),
                side_label: labels::decision_side_label(side),
                quantity: 0,
                price_cents: None,
                score: Some(*score),
                blocked_label: labels::blocked_label(blocked.as_deref()),
                note: note.clone(),
            });
        }
        events.sort_by(|left, right| right.started_at.cmp(&left.started_at));

        Ok(SymbolArchive {
            symbol: symbol.to_string(),
            name,
            trade_count: trades.len(),
            buy_quantity,
            sell_quantity,
            realized_fee_cents,
            decision_count: decisions.len(),
            events,
        })
    }

    /// Risk center: why decided orders did not fill, over the most recent
    /// rounds. Reads only persisted `blocked_reason` values — the same values
    /// `execute_signals` writes back when a decided order is skipped.
    pub fn risk_summary(&self, round_window: usize) -> IntradayResult<RiskSummary> {
        let window = round_window.clamp(1, 200) as i64;
        let rounds: Vec<String> = {
            let mut statement = self.conn.prepare(
                "SELECT round_id FROM round ORDER BY started_at DESC LIMIT ?",
            )?;
            let rows = statement
                .query_map(params![window], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?;
            rows
        };
        if rounds.is_empty() {
            return Ok(RiskSummary {
                window_rounds: 0,
                blocked_total: 0,
                counters: Vec::new(),
            });
        }
        let placeholders = vec!["?"; rounds.len()].join(",");
        let sql = format!(
            "SELECT blocked_reason, count(*) FROM decision \
             WHERE round_id IN ({placeholders}) AND blocked_reason IS NOT NULL \
             GROUP BY blocked_reason ORDER BY count(*) DESC"
        );
        let mut statement = self.conn.prepare(&sql)?;
        let params_ref: Vec<&dyn rusqlite::ToSql> =
            rounds.iter().map(|item| item as &dyn rusqlite::ToSql).collect();
        let rows = statement
            .query_map(params_ref.as_slice(), |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let counters: Vec<RiskCounter> = rows
            .into_iter()
            .map(|(reason, count)| RiskCounter {
                label: labels::blocked_label(Some(&reason)),
                count,
                reason,
            })
            .collect();
        let blocked_total: i64 = counters.iter().map(|item| item.count).sum();
        Ok(RiskSummary {
            window_rounds: rounds.len(),
            blocked_total,
            counters,
        })
    }

    /// Build a bounded post-close proposal from persisted equity points.
    /// `accepted` remains false unless the built-in verifier re-runs against
    /// the stored evidence; no proposal is applied to live policy here.
    pub fn evolution(&self) -> IntradayResult<Vec<EvolutionPreview>> {
        let mut statement = self.conn.prepare(
            "SELECT day, proposal, before_metrics, after_metrics, accepted, reason, created_at \
             FROM evolution ORDER BY id",
        )?;
        let rows = statement.query_map([], |row| {
            Ok(EvolutionPreview {
                day: row.get(0)?,
                proposal: serde_json::from_str(&row.get::<_, String>(1)?).unwrap_or(Value::Null),
                before_metrics: serde_json::from_str(&row.get::<_, String>(2)?)
                    .unwrap_or(Value::Null),
                after_metrics: serde_json::from_str(&row.get::<_, String>(3)?)
                    .unwrap_or(Value::Null),
                accepted: row.get::<_, i64>(4)? != 0,
                reason: row.get(5)?,
                created_at: row.get(6)?,
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    pub fn evolve(&mut self, request: &EvolveRequest) -> IntradayResult<EvolutionPreview> {
        let day = require_bounded(&request.day, "day", MAX_TIME_CHARS)?;
        let created_at = require_bounded(&request.created_at, "created_at", MAX_TIME_CHARS)?;
        let transaction = self.conn.transaction_with_behavior(
            rusqlite::TransactionBehavior::Immediate,
        )?;
        let account = ensure_account(&transaction)?;
        let mut statement = transaction.prepare(
            "SELECT equity_cents, profit_cents, return_bps FROM equity_point \
             WHERE substr(quote_time,1,10)=? ORDER BY rowid",
        )?;
        let points = statement
            .query_map(params![day], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        drop(statement);
        let (equity, profit, round_count, max_drawdown_bps) = if points.is_empty() {
            let supplied = request.summary.clone().unwrap_or(json!({}));
            let equity = supplied
                .get("equity_cents")
                .and_then(Value::as_i64)
                .unwrap_or(account.initial_cash_cents);
            let profit = supplied
                .get("profit_cents")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            let rounds = supplied
                .get("round_count")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0) as usize;
            let drawdown = supplied
                .get("max_drawdown_bps")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0);
            (equity, profit, rounds, drawdown)
        } else {
            let last = points.last().expect("non-empty");
            let mut peak = points[0].0;
            let mut max_drawdown = 0i64;
            for (equity, _, _) in &points {
                peak = peak.max(*equity);
                if peak > 0 {
                    max_drawdown = max_drawdown.max((peak - *equity) * 10_000 / peak);
                }
            }
            (last.0, last.1, points.len(), max_drawdown)
        };
        let profit_bps = profit
            .saturating_mul(10_000)
            .checked_div(account.initial_cash_cents)
            .unwrap_or(0);
        let before_metrics = json!({
            "source": if points.is_empty() { "caller_summary" } else { "persisted_equity_points" },
            "day": day,
            "equity_cents": equity,
            "profit_cents": profit,
            "profit_bps": profit_bps,
            "round_count": round_count,
            "max_drawdown_bps": max_drawdown_bps,
            "initial_cash_cents": account.initial_cash_cents,
        });
        let proposal = proposal_json(profit_bps, max_drawdown_bps, round_count);
        let verified = points.len() >= 3
            && proposal_within_bounds(&proposal)
            && before_metrics["profit_bps"].as_i64() == Some(profit_bps);
        let after_metrics = json!({
            "measured": false,
            "applied": false,
            "reason": "no_forward_test_or_holdout_validation",
        });
        let accepted = false;
        // A verified proposal is still recorded, but stays inert: activation
        // requires an explicit reviewed implementation, so refusing to persist
        // the proposal would only erase the strongest audit evidence.
        let reason = if verified {
            "verified_proposal_requires_explicit_reviewed_activation"
        } else if points.is_empty() {
            "insufficient_persisted_evidence"
        } else {
            "proposal_not_validated; kept_inert"
        };
        transaction.execute(
            "INSERT INTO evolution(day, proposal, before_metrics, after_metrics, accepted, reason, created_at) \
             VALUES(?,?,?,?,?,?,?)",
            params![
                day,
                proposal.to_string(),
                before_metrics.to_string(),
                after_metrics.to_string(),
                accepted as i64,
                reason,
                created_at
            ],
        )?;
        transaction.commit()?;
        Ok(EvolutionPreview {
            day: day.to_string(),
            proposal,
            before_metrics,
            after_metrics,
            accepted,
            reason: reason.into(),
            created_at: created_at.to_string(),
        })
    }

    /// Extract daily closing OHLCV bars from the final trading round of each
    /// recorded day in `quote_snapshot` (or a single `day_filter`).
    pub fn closing_bars_from_snapshots(
        &self,
        day_filter: Option<&str>,
    ) -> IntradayResult<Vec<crate::store::Bar>> {
        let mut round_stmt = self.conn.prepare(
            "SELECT substr(round_id, 1, 10) AS d, MAX(round_id) \
             FROM round \
             WHERE round_id NOT LIKE '%-close' AND length(round_id) >= 10 \
             GROUP BY d ORDER BY d ASC",
        )?;
        let day_rounds: Vec<(String, String)> = round_stmt
            .query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))?
            .collect::<Result<Vec<_>, _>>()?;

        let mut snap_stmt = self.conn.prepare(
            "SELECT symbol, COALESCE(name, ''), price_cents, prev_close_cents, \
             COALESCE(open_cents, price_cents), COALESCE(high_cents, price_cents), \
             COALESCE(low_cents, price_cents), COALESCE(volume, 0), COALESCE(amount, 0) \
             FROM quote_snapshot WHERE round_id = ?1 AND price_cents > 0 ORDER BY symbol ASC",
        )?;
        let mut out = Vec::new();
        for (day, round_id) in day_rounds {
            if day.len() != 10 || day.as_bytes().get(4) != Some(&b'-') {
                continue;
            }
            if let Some(target) = day_filter
                && day != target
            {
                continue;
            }
            let rows = snap_stmt.query_map(params![round_id], |row| {
                let close_cents: i64 = row.get(2)?;
                let prev_close_cents: i64 = row.get(3)?;
                let open_cents: i64 = row.get(4)?;
                let high_cents: i64 = row.get(5)?;
                let low_cents: i64 = row.get(6)?;
                let volume: i64 = row.get(7)?;
                let amount: i64 = row.get(8)?;
                Ok(crate::store::Bar {
                    symbol: row.get(0)?,
                    day: day.clone(),
                    name: row.get(1)?,
                    open_cents: if open_cents > 0 { open_cents } else { close_cents },
                    high_cents: if high_cents > 0 {
                        high_cents.max(close_cents)
                    } else {
                        close_cents
                    },
                    low_cents: if low_cents > 0 {
                        low_cents.min(close_cents)
                    } else {
                        close_cents
                    },
                    close_cents,
                    prev_close_cents: if prev_close_cents > 0 {
                        prev_close_cents
                    } else {
                        close_cents
                    },
                    volume_shares: volume.max(0),
                    amount_cents: amount.max(0),
                    adjust: "none".into(),
                    source: "live".into(),
                    tradable: close_cents > 0,
                })
            })?;
            for bar in rows {
                out.push(bar?);
            }
        }
        Ok(out)
    }

    /// Compute real-time cross-sectional market sentiment from the newest round's snapshot.
    pub fn latest_sentiment(&self) -> IntradayResult<super::MarketSentiment> {
        let latest: Option<(String, String)> = self
            .conn
            .query_row(
                "SELECT round_id, started_at FROM round \
                 WHERE round_id NOT LIKE '%-close' \
                 ORDER BY round_id DESC LIMIT 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((round_id, quote_time)) = latest else {
            return Ok(super::compute_market_sentiment(&[], None, None));
        };
        let mut stmt = self.conn.prepare(
            "SELECT symbol, name, price_cents, prev_close_cents, open_cents, high_cents, \
             low_cents, limit_up_cents, limit_down_cents, volume, amount \
             FROM quote_snapshot WHERE round_id = ?1",
        )?;
        let rows = stmt.query_map(params![round_id], |row| {
            Ok(Quote {
                symbol: row.get(0)?,
                name: row.get(1)?,
                price_cents: row.get(2)?,
                prev_close_cents: row.get(3)?,
                open_cents: row.get(4)?,
                high_cents: row.get(5)?,
                low_cents: row.get(6)?,
                limit_up_cents: row.get(7)?,
                limit_down_cents: row.get(8)?,
                volume: row.get(9)?,
                avg_volume: None,
                amount_cents: row.get(10)?,
                tradable: Some(true),
                trailing_closes: Vec::new(),
            })
        })?;
        let mut quotes = Vec::new();
        for q in rows {
            quotes.push(q?);
        }
        Ok(super::compute_market_sentiment(
            &quotes,
            Some(round_id),
            Some(quote_time),
        ))
    }
}

fn validate_round_request(request: &RunRoundRequest) -> IntradayResult<()> {
    require_bounded(&request.round_id, "round_id", MAX_ROUND_ID_CHARS)?;
    require_bounded(&request.quote_time, "quote_time", MAX_TIME_CHARS)?;
    if let Some(fetched_at) = request.fetched_at.as_deref() {
        require_bounded(fetched_at, "fetched_at", MAX_TIME_CHARS)?;
    }
    if request.quotes.is_empty() {
        return Err(IntradayError::Rules("empty_quote_universe".into()));
    }
    let mut seen = BTreeSet::new();
    for quote in &request.quotes {
        let _ = require_bounded(&quote.symbol, "symbol", MAX_SYMBOL_CHARS)?;
        if !seen.insert(quote.symbol.as_str()) {
            return Err(IntradayError::Rules(format!(
                "duplicate_quote_symbol:{}",
                quote.symbol
            )));
        }
    }
    if let Some(advice) = &request.advice {
        validate_advice_shape(advice)?;
    }
    Ok(())
}

/// How many trading days of raw quote/decision audit rows to keep. The ledger
/// (round, trade, position, equity_point, evolution) is never pruned; this only
/// bounds the two high-volume archives that exist for investigation.
pub const AUDIT_RETENTION_DAYS: i64 = 10;

/// Rounds returned by the console's round list.
///
/// The scheduler view and the round picker read recent rounds only. A full
/// session is ~80 rounds, so 600 is roughly a week — enough to see yesterday
/// without shipping years of ledger history on every 30-second refresh.
pub const ROUND_LIST_LIMIT: usize = 600;

/// Maximum points returned by the equity curve.
///
/// `equity_point` is ledger history and is never pruned. At 80 rounds a day the
/// curve reaches ~20 000 points in a year; the chart cannot resolve more than a
/// few hundred anyway. Beyond this cap the series is downsampled evenly, always
/// keeping the first and last point so the shape and the endpoints stay true.
pub const EQUITY_CURVE_POINTS: usize = 2_000;

/// Bound an equity series to `cap` points by taking evenly spaced samples.
///
/// Order and endpoints are preserved: the first and last point are always
/// kept, so the chart still starts at the initial value and ends at the
/// current one. A series already within the cap is returned untouched, so a
/// short history is never altered.
pub(crate) fn downsample_equity(points: Vec<EquityPoint>, cap: usize) -> Vec<EquityPoint> {
    if points.len() <= cap || cap < 2 {
        return points;
    }
    let last = points.len() - 1;
    let mut out = Vec::with_capacity(cap);
    for index in 0..cap {
        // Even spacing across the whole range, including both endpoints.
        let source = index * last / (cap - 1);
        out.push(points[source].clone());
    }
    out
}

/// Delete audit archive rows older than the retention window.
///
/// Both deletions are bounded by a day cut-off derived from the trading-day
/// table, so they run at most once per day and can never touch today's rows.
/// The ledger tables are deliberately untouched: deleting a `round` would
/// break `equity_point`'s history and the per-symbol archive.
fn prune_audit_archives(
    transaction: &Transaction<'_>,
    today: &str,
) -> IntradayResult<()> {
    let cutoff: Option<String> = transaction
        .query_row(
            "SELECT day FROM trading_day WHERE day<? ORDER BY day DESC LIMIT 1 OFFSET ?",
            params![today, AUDIT_RETENTION_DAYS - 1],
            |row| row.get(0),
        )
        .optional()?;
    let Some(cutoff) = cutoff else {
        // Fewer than AUDIT_RETENTION_DAYS trading days exist; nothing to prune.
        return Ok(());
    };
    // Rounds are identified by the day prefix of their own started_at, which is
    // the caller-supplied quote time — the same fact the rollover uses.
    transaction.execute(
        "DELETE FROM quote_snapshot WHERE round_id IN ( \
             SELECT round_id FROM round WHERE substr(started_at,1,10) < ? )",
        params![cutoff],
    )?;
    transaction.execute(
        "DELETE FROM decision WHERE round_id IN ( \
             SELECT round_id FROM round WHERE substr(started_at,1,10) < ? )",
        params![cutoff],
    )?;
    transaction.execute(
        "DELETE FROM ai_observation WHERE round_id IN ( \
             SELECT round_id FROM round WHERE substr(started_at,1,10) < ? )",
        params![cutoff],
    )?;
    Ok(())
}

fn store_quote_snapshot(
    transaction: &Transaction<'_>, request: &RunRoundRequest, quote: &Quote, started_at: &str,
) -> IntradayResult<()> {
    transaction.execute(
        "INSERT INTO quote_snapshot(round_id, symbol, name, price_cents, prev_close_cents, \
         open_cents, high_cents, low_cents, limit_up_cents, limit_down_cents, \
         volume, amount, quote_time, fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![
            request.round_id,
            quote.symbol,
            sanitized_name(quote.name.as_deref()),
            if quote.tradable == Some(false) { 0 } else { quote.price_cents },
            quote.prev_close_cents,
            quote.open_cents,
            quote.high_cents,
            quote.low_cents,
            quote.limit_up_cents,
            quote.limit_down_cents,
            quote.volume,
            quote.amount_cents,
            request.quote_time,
            started_at
        ],
    )?;
    Ok(())
}

fn run_round_tx(
    transaction: &Transaction<'_>,
    request: &RunRoundRequest,
    ai: &AiApplication,
    account: &AccountRow,
    books: &mut [SlotBook; 2],
    policy: ResearchEntryPolicy,
    signal_quotes: Option<&[Quote]>,
    retain_full_audit: bool,
) -> IntradayResult<RoundOutcome> {
    let started_at = request.fetched_at.as_deref().unwrap_or(&request.quote_time);
    let finished_at = &request.quote_time;
    let created_at = finished_at;

    // T+1 rollover is driven by the caller-supplied quote_time day, never by
    // a wall clock. The first round of a new trading day promotes yesterday's
    // bought shares to sellable; same-day replays of an already-rolled day are
    // a no-op, and this is invisible to idempotent replays (they return the
    // stored outcome before this runs).
    let round_day = request.quote_time.get(..10).map(str::to_string);
    if let Some(day) = &round_day {
        let rolled: Option<String> = transaction
            .query_row(
                "SELECT day FROM trading_day WHERE day=?",
                params![day],
                |row| row.get(0),
            )
            .optional()?;
        if rolled.is_none() {
            transaction.execute(
                "UPDATE position SET sellable_quantity=quantity, updated_at=? WHERE quantity>0",
                params![started_at],
            )?;
            for book in books.iter_mut() {
                for position in &mut book.positions {
                    if position.quantity > 0 {
                        position.sellable_quantity = position.quantity;
                    }
                }
            }
            transaction.execute(
                "INSERT INTO trading_day(day, rolled_at) VALUES(?,?)",
                params![day, started_at],
            )?;
            // Retention runs exactly once per trading day (here, after the day
            // marker is written), never inside a round's hot path. Without it
            // the archive grows by ~42万 quote rows and ~83万 decision rows a
            // day: 378 MB after one session, ~700 MB/day forever. Rounds,
            // trades, positions, equity points and evolution history are the
            // ledger and are never pruned; only the two audit archives are.
            prune_audit_archives(transaction, day)?;
        }
    }

    if retain_full_audit {
        for quote in &request.quotes {
            store_quote_snapshot(transaction, request, quote, started_at)?;
        }
    }

    let mut quote_by_symbol = BTreeMap::new();
    for quote in &request.quotes {
        quote_by_symbol.insert(quote.symbol.as_str(), quote);
    }
    let missing_marks: Vec<_> = books.iter()
        .flat_map(|book| book.positions.iter())
        .filter(|position| !quote_by_symbol.get(position.symbol.as_str())
            .is_some_and(|quote| quote.has_live_price()))
        .map(|position| position.symbol.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut marks = latest_prices_for(transaction, Some(&missing_marks))?;
    marks.extend(quote_by_symbol.iter()
        .filter(|(_, quote)| quote.has_live_price())
        .map(|(symbol, quote)| (symbol.to_string(), quote.price_cents)));
    let regime = audit_regime(&request.quotes);
    let signal_regime = if policy == ResearchEntryPolicy::Baseline {
        None
    } else {
        signal_quotes.map(audit_regime)
    };
    let new_entry_delta = match (signal_regime.as_deref(), policy) {
        (Some("risk_off"), ResearchEntryPolicy::WeakEntryBuffer { points }) => points,
        (Some("risk_on"), ResearchEntryPolicy::StrongEntryRelief { points }) => -points,
        _ => 0,
    };
    let pause_new_entries = signal_regime.as_deref() == Some("risk_off")
        && policy == ResearchEntryPolicy::WeakEntryPause;
    let score = |advice: &AiApplication| {
        signal_quotes.map(|quotes| score_candidates_for_research(
            &request.round_id, quotes, books, advice, new_entry_delta, pause_new_entries,
        )).unwrap_or_default()
    };
    let signals = score(ai);
    // Compare signals on the same inputs, before sizing and execution.
    let baseline = ai.ai_ok().then(|| {
        let neutral = AiApplication::neutral(&request.round_id, "signal_baseline");
        score(&neutral)
    });
    if retain_full_audit {
        for signal in &signals {
            store_decision(transaction, signal, created_at)?;
        }
    }

    let trades = execute_signals(
        transaction,
        request,
        ai,
        &signals,
        &quote_by_symbol,
        &marks,
        books,
        created_at,
    )?;
    let mut ai_audit = ai.clone();
    if let Some(baseline) = &baseline {
        let sides: BTreeMap<_, _> = baseline
            .iter()
            .map(|decision| ((decision.slot, decision.symbol.as_str()), decision.side.as_str()))
            .collect();
        ai_audit.signal_baseline = Some(SignalBaseline {
            compared: signals.len(),
            side_changed: signals.iter().filter(|decision| {
                sides[&(decision.slot, decision.symbol.as_str())] != decision.side
            }).count(),
            fills: trades.iter().map(|trade| FillBaseline {
                slot: trade.slot,
                symbol: trade.symbol.clone(),
                side: trade.side.clone(),
                without_ai: sides[&(trade.slot, trade.symbol.as_str())].to_string(),
            }).collect(),
        });
    }
    write_books(transaction, books, created_at)?;

    let (equity_cents, steady_equity, aggressive_equity) =
        value_equity(books, &marks)?;
    let profit_cents = equity_cents - account.initial_cash_cents;
    let return_bps = if account.initial_cash_cents > 0 {
        profit_cents * 10_000 / account.initial_cash_cents
    } else {
        0
    };
    transaction.execute(
        "INSERT INTO equity_point(round_id, quote_time, finished_at, equity_cents, \
         profit_cents, return_bps, steady_equity_cents, aggressive_equity_cents) \
         VALUES(?,?,?,?,?,?,?,?)",
        params![
            request.round_id,
            request.quote_time,
            finished_at,
            equity_cents,
            profit_cents,
            return_bps,
            steady_equity,
            aggressive_equity
        ],
    )?;

    let note = round_note(request, &signals, ai);
    let round_quotes: BTreeMap<String, Option<String>> = quote_by_symbol
        .iter()
        .map(|(symbol, quote)| {
            (
                symbol.to_string(),
                sanitized_name(quote.name.as_deref()),
            )
        })
        .collect();
    // The POST's own account snapshot follows the same name priority as the
    // GET: this round's quote first, then the most recent stored real name.
    let position_symbols: BTreeSet<String> = books
        .iter()
        .flat_map(|book| book.positions.iter().map(|position| position.symbol.clone()))
        .collect();
    if !retain_full_audit {
        // Forward research only needs durable marks for held symbols. The
        // source input already holds the full audit in the session archive.
        for symbol in &position_symbols {
            if let Some(quote) = quote_by_symbol.get(symbol.as_str()) {
                store_quote_snapshot(transaction, request, quote, started_at)?;
            }
        }
    }
    let round_names = resolve_names(
        transaction,
        &request.round_id,
        Some(&round_quotes),
        &position_symbols,
    )?;
    let mut account_view = account_view_from(
        account,
        books,
        &marks,
        &round_names,
        Some(&request.round_id),
    );
    set_daily_pnl(transaction, &mut account_view)?;
    transaction.execute(
        "INSERT INTO round(round_id, started_at, finished_at, quote_count, trade_count, \
         ai_ok, ai_error, equity_cents, regime, note, account_json) \
         VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        params![
            request.round_id,
            started_at,
            finished_at,
            request.quotes.len() as i64,
            trades.len() as i64,
            ai.ai_ok() as i64,
            ai.ai_error,
            equity_cents,
            regime,
            note,
            serde_json::to_string(&account_view)?
        ],
    )?;
    transaction.execute(
        "INSERT INTO ai_observation(round_id, model, raw_advice, accepted, clamped, \
         confidence, reason, clamped_fields, application_json, created_at) \
         VALUES(?,?,?,?,?,?,?,?,?,?)",
        params![
            ai.round_id,
            ai.model,
            ai.raw_advice,
            ai.accepted as i64,
            ai.clamped as i64,
            ai.confidence,
            ai.reason,
            serde_json::to_string(&ai.clamped_fields)?,
            serde_json::to_string(&ai_audit)?,
            created_at
        ],
    )?;

    let stored_decisions = decorate_decisions(
        transaction,
        &request.round_id,
        Some(&round_quotes),
        read_decisions_tx(transaction, &request.round_id)?,
    )?;
    let stored_trades = decorate_trades(
        transaction,
        Some(&round_quotes),
        read_trades_tx(transaction, &request.round_id)?,
    )?;
    Ok(RoundOutcome {
        round: RoundSummary {
            round_id: request.round_id.clone(),
            started_at: started_at.to_string(),
            finished_at: finished_at.to_string(),
            quote_count: request.quotes.len(),
            trade_count: stored_trades.len(),
            ai_ok: ai.ai_ok(),
            ai_error: ai.ai_error.clone(),
            equity_cents,
            regime_label: labels::regime_label(&regime),
            regime,
            ai_label: labels::ai_label(ai.ai_ok(), ai.ai_error.as_deref()),
            note: note.clone(),
            note_label: labels::note_label(&note),
            idempotent: false,
        },
        decisions: stored_decisions,
        trades: stored_trades,
        account: account_view,
        ai: ai_audit,
    })
}

/// The factor payload persisted with one decision.
///
/// A round scores 5 219 symbols for two slots, and the overwhelming majority
/// are `hold` (no order). Storing the full 12-field `FactorBreakdown` for every
/// hold row cost 265 MB in a single day. Only a decision that actually produced
/// an order keeps the complete factors; a hold keeps the handful of numbers a
/// reader needs to see *why* the score was where it was. Nothing is dropped
/// that the console or the per-symbol archive displays, and a hold row still
/// answers "what did it score, against what threshold, and which components
/// carried it".
fn decision_factors_json(signal: &DecisionSignal) -> IntradayResult<String> {
    let holds = signal.side == "hold";
    if !holds {
        return Ok(serde_json::to_string(&signal.factors)?);
    }
    let factors = &signal.factors;
    // `score` and `threshold` are their own columns on this row, so repeating
    // them here would only inflate the archive. What a reader cannot get from
    // the row itself is *which* components produced the score, so those stay.
    let compact = serde_json::json!({
        "compact": true,
        "momentum_prev_close_bps": factors.momentum_prev_close_bps,
        "intraday_position_bps": factors.intraday_position_bps,
        "trend_bps": factors.trend_bps,
        "amplitude_bps": factors.amplitude_bps,
        "notes": factors.notes,
    });
    Ok(serde_json::to_string(&compact)?)
}

fn store_decision(
    transaction: &Transaction<'_>,
    signal: &DecisionSignal,
    created_at: &str,
) -> IntradayResult<()> {
    transaction.execute(
        "INSERT INTO decision(round_id, slot, symbol, score, threshold, reached_threshold, \
         side, blocked_reason, ai_bias, ai_confidence, ai_applied, factors_json, note, created_at) \
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![
            signal.round_id,
            signal.slot.as_str(),
            signal.symbol,
            signal.score,
            signal.threshold,
            signal.reached_threshold as i64,
            signal.side,
            signal.blocked_reason,
            signal.ai_bias,
            signal.ai_confidence,
            signal.ai_applied as i64,
            decision_factors_json(signal)?,
            bounded_text(&signal.note, MAX_REASON_CHARS),
            created_at
        ],
    )?;
    Ok(())
}

fn execute_signals(
    transaction: &Transaction<'_>,
    request: &RunRoundRequest,
    ai: &AiApplication,
    signals: &[DecisionSignal],
    quote_by_symbol: &BTreeMap<&str, &Quote>,
    marks: &BTreeMap<String, i64>,
    books: &mut [SlotBook; 2],
    created_at: &str,
) -> IntradayResult<Vec<TradeRecord>> {
    let mut out = Vec::new();

    // A decided order that produced no fill must leave its reason on the
    // decision row. A silent `continue` would show "卖出/买入" on the console
    // with no trade and no explanation — the exact gap V1's 风控中心 closed.
    let record_skip = |transaction: &Transaction<'_>,
                       signal: &DecisionSignal,
                       reason: &str,
                       notes: &mut Vec<String>| -> IntradayResult<()> {
        notes.push(reason.to_string());
        notes.sort();
        notes.dedup();
        let note = notes.join("; ");
        transaction.execute(
            "UPDATE decision SET blocked_reason=?, note=? \
             WHERE round_id=? AND slot=? AND symbol=?",
            params![
                reason,
                bounded_text(&note, MAX_REASON_CHARS),
                signal.round_id,
                signal.slot.as_str(),
                signal.symbol
            ],
        )?;
        Ok(())
    };

    // Risk exits first: no sale is skipped because a later buy consumed cash,
    // and a T+1 restriction never becomes a negative position.
    let mut sells: Vec<&DecisionSignal> = signals
        .iter()
        .filter(|signal| signal.side == "sell")
        .collect();
    sells.sort_by(|left, right| {
        left.score
            .cmp(&right.score)
            .then_with(|| left.symbol.cmp(&right.symbol))
            .then_with(|| left.slot.cmp(&right.slot))
    });
    for signal in sells {
        let Some(quote) = quote_by_symbol.get(signal.symbol.as_str()).copied() else {
            continue;
        };
        let index = slot_index(signal.slot);
        let Some(position) = books[index]
            .positions
            .iter()
            .find(|item| item.symbol == signal.symbol)
            .cloned()
        else {
            continue;
        };
        let mut notes: Vec<String> = signal.note.split("; ").map(str::to_string).collect();
        let quantity = position.sellable_quantity.min(position.quantity);
        let quantity = quantity / 100 * 100;
        if quantity <= 0 {
            record_skip(transaction, signal, "t_plus_one", &mut notes)?;
            continue;
        }
        if !quote.can_sell() {
            record_skip(transaction, signal, "limit_down_sealed", &mut notes)?;
            continue;
        }
        let notional = quantity * quote.price_cents;
        let fee = fee_cents("sell", notional)?;
        if fee > notional {
            record_skip(transaction, signal, "fee_exceeds_notional", &mut notes)?;
            continue;
        }
        apply_sell(
            &mut books[index],
            &signal.symbol,
            quantity,
            quote.price_cents,
            fee,
            created_at,
        )?;
        let reason = if signal.note.contains("stop_or_take_profit") {
            "stop_or_take_profit"
        } else if signal.note.contains("ai_force_sell") {
            "ai_force_sell"
        } else {
            "score_exit"
        };
        let ai_applied = signal.ai_applied && ai.ai_ok();
        transaction.execute(
            "INSERT INTO trade(round_id, slot, symbol, side, quantity, price_cents, fee_cents, \
             reason, ai_applied, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            params![
                request.round_id,
                signal.slot.as_str(),
                signal.symbol,
                "sell",
                quantity,
                quote.price_cents,
                fee,
                reason,
                ai_applied as i64,
                created_at
            ],
        )?;
    }

    // Buys are ordered by score, then symbol/slot for deterministic ties.
    let full_market_round = request.quotes.len() >= 100;
    let sentiment_max_exposure_bps = if full_market_round {
        super::compute_market_sentiment(&request.quotes, None, None).recommended_max_exposure_bps
    } else {
        10_000
    };
    let is_morning_cooldown = full_market_round && {
        // 09:30~09:41 CST (or 01:30~01:41 UTC) opening impulse window
        let qt = request.quote_time.as_str();
        qt.contains("T09:3")
            || qt.contains(" 09:3")
            || qt.contains("T01:3")
            || qt.contains("T09:40")
            || qt.contains(" 09:40")
            || qt.contains("T01:40")
            || qt.contains("T09:41")
            || qt.contains(" 09:41")
            || qt.contains("T01:41")
    };
    let mut buys: Vec<&DecisionSignal> = signals
        .iter()
        .filter(|signal| signal.side == "buy")
        .collect();
    buys.sort_by(|left, right| {
        right
            .score
            .cmp(&left.score)
            .then_with(|| left.symbol.cmp(&right.symbol))
            .then_with(|| left.slot.cmp(&right.slot))
    });
    for signal in buys {
        let Some(quote) = quote_by_symbol.get(signal.symbol.as_str()).copied() else {
            continue;
        };
        if !quote.can_buy() || quote.validate().is_err() {
            continue;
        }
        let index = slot_index(signal.slot);
        let mut notes: Vec<String> = signal.note.split("; ").map(str::to_string).collect();
        if is_morning_cooldown && quote.prev_close_cents > 0 {
            let chg_bps = (quote.price_cents - quote.prev_close_cents) * 10_000 / quote.prev_close_cents;
            let fading_open = quote.open_cents.is_some_and(|o| quote.price_cents < o);
            if chg_bps >= 450 || fading_open {
                record_skip(transaction, signal, "morning_cooldown_guard", &mut notes)?;
                continue;
            }
        }
        let deep_target_bps = if ai.is_deep() {
            ai.active_suggestions()
                .iter()
                .find(|item| item.symbol == signal.symbol && item.side == "buy" && item.matches_slot(signal.slot))
                .and_then(|item| item.target_weight_bps)
                .filter(|&w| w > 0)
        } else {
            None
        };
        if deep_target_bps.is_none()
            && books[index].positions.len() >= signal.slot.max_positions()
            && !books[index]
                .positions
                .iter()
                .any(|item| item.symbol == signal.symbol)
        {
            record_skip(transaction, signal, "max_positions", &mut notes)?;
            continue;
        }
        let (single_bps, cash_floor_bps) = signal.slot.risk_bps();
        let slot_equity = slot_equity(&books[index], marks)?;
        let sentiment_cash_floor = if full_market_round {
            let cap_bps = if signal.slot == Slot::Steady {
                sentiment_max_exposure_bps.min(8_500)
            } else {
                sentiment_max_exposure_bps
            };
            slot_equity * (10_000 - cap_bps).max(cash_floor_bps) / 10_000
        } else {
            slot_equity * cash_floor_bps / 10_000
        };
        let current_value = books[index]
            .positions
            .iter()
            .find(|item| item.symbol == signal.symbol)
            .and_then(|position| {
                quote_by_symbol
                    .get(position.symbol.as_str())
                    .map(|quote| position.quantity * quote.price_cents)
            })
            .unwrap_or(0);
        let available = if let Some(target_bps) = deep_target_bps {
            // Deep AI mode: AI sets target position weight; Rust enforces cash & full-market sentiment circuit breaker.
            let target_value = slot_equity * target_bps / 10_000;
            let spendable_cash = if full_market_round {
                (books[index].cash_cents - sentiment_cash_floor).max(0)
            } else {
                books[index].cash_cents
            };
            (target_value - current_value).min(spendable_cash)
        } else {
            let single_limit = slot_equity * single_bps / 10_000;
            (single_limit - current_value).min(books[index].cash_cents - sentiment_cash_floor)
        };
        if available <= 0 {
            record_skip(transaction, signal, "cash_budget", &mut notes)?;
            continue;
        }
        let mut suggestion_weight = ai
            .active_suggestions()
            .iter()
            .filter(|item| item.symbol == signal.symbol)
            .fold(0i64, |sum, item| sum.saturating_add(item.weight_bps))
            .clamp(-2_000, 2_000);
        // A confident bullish bias is a bounded exposure tilt too. It cannot
        // raise the per-symbol cap or spend the cash floor; it only tilts the
        // order within those Rust-owned limits.
        if ai.ai_ok() {
            suggestion_weight = suggestion_weight
                .saturating_add(ai.effective_bias().saturating_mul(10))
                .clamp(-9_000, 2_000);
        }
        let ai_sizing = ai.ai_ok() && suggestion_weight != 0;
        let quantity = plan_buy_quantity(
            available,
            quote.price_cents,
            suggestion_weight,
        )?;
        if quantity <= 0 {
            record_skip(transaction, signal, "lot_too_expensive", &mut notes)?;
            continue;
        }
        let notional = quantity * quote.price_cents;
        let fee = fee_cents("buy", notional)?;
        if notional + fee > books[index].cash_cents {
            continue;
        }
        apply_buy(
            &mut books[index],
            &signal.symbol,
            quantity,
            quote.price_cents,
            fee,
            created_at,
        )?;
        let ai_applied = (signal.ai_applied || ai_sizing) && ai.ai_ok();
        transaction.execute(
            "INSERT INTO trade(round_id, slot, symbol, side, quantity, price_cents, fee_cents, \
             reason, ai_applied, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            params![
                request.round_id,
                signal.slot.as_str(),
                signal.symbol,
                "buy",
                quantity,
                quote.price_cents,
                fee,
                "score_entry",
                ai_applied as i64,
                created_at
            ],
        )?;
    }
    out.extend(read_trades_tx(transaction, &request.round_id)?);
    Ok(out)
}

fn plan_buy_quantity(
    available_cents: i64,
    price_cents: i64,
    suggestion_weight_bps: i64,
) -> IntradayResult<i64> {
    if available_cents <= 0 || price_cents <= 0 {
        return Ok(0);
    }
    let tilt = suggestion_weight_bps.clamp(-9_000, 2_000);
    let scaled = available_cents
        .saturating_mul(10_000 + tilt)
        .checked_div(10_000)
        .unwrap_or(0);
    // The margin for commission cannot be found by solving a piecewise fee;
    // step down whole lots until `notional + fee` fits the available cash.
    let lot_cost = price_cents
        .checked_mul(100)
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
    let mut lots = (scaled / lot_cost).max(0);
    while lots > 0 {
        let notional = lots
            .checked_mul(lot_cost)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
        let fee = fee_cents("buy", notional)?;
        if notional
            .checked_add(fee)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?
            <= available_cents
        {
            return Ok(lots * 100);
        }
        lots -= 1;
    }
    Ok(0)
}

fn slot_equity(
    book: &SlotBook,
    marks: &BTreeMap<String, i64>,
) -> IntradayResult<i64> {
    let mut equity = book.cash_cents;
    for position in &book.positions {
        let Some(&price) = marks.get(&position.symbol) else {
            continue;
        };
        equity = position.quantity.checked_mul(price)
            .and_then(|value| equity.checked_add(value))
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
    }
    Ok(equity)
}

fn value_equity(
    books: &[SlotBook; 2],
    marks: &BTreeMap<String, i64>,
) -> IntradayResult<(i64, i64, i64)> {
    let steady = slot_equity(&books[0], marks)?;
    let aggressive = slot_equity(&books[1], marks)?;
    Ok((
        steady
            .checked_add(aggressive)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?,
        steady,
        aggressive,
    ))
}

fn write_books(
    transaction: &Transaction<'_>,
    books: &[SlotBook; 2],
    created_at: &str,
) -> IntradayResult<()> {
    let mut cash_cents: i64 = 0;
    for book in books {
        if book.cash_cents < 0 {
            return Err(IntradayError::Rules("negative_cash".into()));
        }
        cash_cents = cash_cents
            .checked_add(book.cash_cents)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
        transaction.execute(
            "UPDATE slot_book SET cash_cents=? WHERE slot=?",
            params![book.cash_cents, book.slot.as_str()],
        )?;
        transaction.execute(
            "DELETE FROM position WHERE slot=?",
            params![book.slot.as_str()],
        )?;
        for position in &book.positions {
            if position.quantity <= 0
                || position.sellable_quantity < 0
                || position.sellable_quantity > position.quantity
            {
                return Err(IntradayError::Rules("invalid_position".into()));
            }
            transaction.execute(
                "INSERT INTO position(slot, symbol, quantity, sellable_quantity, \
                 avg_cost_cents, updated_at) VALUES(?,?,?,?,?,?)",
                params![
                    position.slot.as_str(),
                    position.symbol,
                    position.quantity,
                    position.sellable_quantity,
                    position.avg_cost_cents,
                    position.updated_at
                ],
            )?;
        }
    }
    transaction.execute(
        "UPDATE account SET cash_cents=?",
        params![cash_cents],
    )?;
    let _ = created_at;
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct AccountRow {
    id: String,
    initial_cash_cents: i64,
    cash_cents: i64,
    created_at: String,
}

fn ensure_account(conn: &impl ConnLike) -> IntradayResult<AccountRow> {
    conn.query_row(
        "SELECT id, initial_cash_cents, cash_cents, created_at FROM account LIMIT 1",
        [],
        |row| {
            Ok(AccountRow {
                id: row.get(0)?,
                initial_cash_cents: row.get(1)?,
                cash_cents: row.get(2)?,
                created_at: row.get(3)?,
            })
        },
    )
    .optional()?
    .ok_or_else(|| IntradayError::NotFound("account_not_found".into()))
}

fn read_books(conn: &impl ConnLike) -> IntradayResult<[SlotBook; 2]> {
    let mut books = [
        SlotBook {
            slot: Slot::Steady,
            cash_cents: 0,
            positions: Vec::new(),
        },
        SlotBook {
            slot: Slot::Aggressive,
            cash_cents: 0,
            positions: Vec::new(),
        },
    ];
    for book in &mut books {
        book.cash_cents = conn
            .query_row(
                "SELECT cash_cents FROM slot_book WHERE slot=?",
                params![book.slot.as_str()],
                |row| row.get(0),
            )
            .optional()?
            .ok_or_else(|| IntradayError::Integrity("missing_slot_book".into()))?;
    }
    let mut statement = conn.prepare(
        "SELECT slot, symbol, quantity, sellable_quantity, avg_cost_cents, updated_at \
         FROM position ORDER BY symbol",
    )?;
    let rows = statement.query_map([], |row| {
        let slot: String = row.get(0)?;
        Ok(Position {
            slot: Slot::parse(&slot).unwrap_or(Slot::Steady),
            symbol: row.get(1)?,
            name: None,
            quantity: row.get(2)?,
            sellable_quantity: row.get(3)?,
            avg_cost_cents: row.get(4)?,
            updated_at: row.get(5)?,
            // Read models are decorated by account_view_from; the durable
            // position table has no columns for these derived marks.
            last_price_cents: None,
            market_value_cents: None,
        })
    })?;
    for position in rows {
        let position = position?;
        books[slot_index(position.slot)].positions.push(position);
    }
    Ok(books)
}

/// Latest price per symbol, optionally restricted to the symbols a caller holds.
///
/// The unrestricted form ranks every snapshot row ever written. At 5k symbols
/// per round that grows without bound, and an account view only needs the marks
/// for its handful of positions -- so callers that know their symbols pass them
/// and the read stays proportional to the portfolio, not to the archive.
fn latest_prices_for(
    conn: &impl ConnLike,
    symbols: Option<&[String]>,
) -> IntradayResult<BTreeMap<String, i64>> {
    const RANKED: &str = "WITH ranked AS ( \
             SELECT q.symbol AS symbol, q.price_cents AS price_cents, \
                    ROW_NUMBER() OVER ( \
                        PARTITION BY q.symbol \
                        ORDER BY julianday(COALESCE(r.started_at, q.fetched_at)) DESC, \
                                 COALESCE(r.started_at, q.fetched_at) DESC, \
                                 q.round_id DESC, q.rowid DESC \
                    ) AS recency \
             FROM quote_snapshot q \
             LEFT JOIN round r ON r.round_id = q.round_id \
             WHERE q.price_cents > 0";
    match symbols {
        Some(list) if list.is_empty() => Ok(BTreeMap::new()),
        Some(list) => {
            let placeholders = vec!["?"; list.len()].join(",");
            let sql = format!(
                "{RANKED} AND q.symbol IN ({placeholders}) \
                 ) SELECT symbol, price_cents FROM ranked WHERE recency = 1"
            );
            let mut statement = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = list
                .iter()
                .map(|item| item as &dyn rusqlite::ToSql)
                .collect();
            Ok(statement
                .query_map(params.as_slice(), |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<BTreeMap<_, _>, _>>()?)
        }
        None => {
            let sql = format!(
                "{RANKED} \
                 ) SELECT symbol, price_cents FROM ranked WHERE recency = 1"
            );
            let mut statement = conn.prepare(&sql)?;
            Ok(statement
                .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<BTreeMap<_, _>, _>>()?)
        }
    }
}

/// Latest mark plus the latest non-empty display name for every quoted symbol.
/// An old row without a name is left NULL; no name is inferred from a code.
fn latest_quotes(
    conn: &impl ConnLike,
    symbols: &[String],
) -> IntradayResult<(BTreeMap<String, i64>, BTreeMap<String, Option<String>>)> {
    // A read-only connection opened against a database whose `name` column was
    // never added has no names to show; it must still return prices and NULLs.
    let has_name = conn
        .prepare("PRAGMA table_info(quote_snapshot)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?
        .iter()
        .any(|column| column == "name");
    if !has_name {
        return Ok((latest_prices_for(conn, Some(symbols))?, BTreeMap::new()));
    }
    let prices = latest_prices_for(conn, Some(symbols))?;
    // Names for the held symbols only. The archive gains a row per symbol per
    // round, so an unrestricted MAX(rowid) scan is not a cheap read.
    let placeholders = vec!["?"; symbols.len()].join(",");
    let name_sql = format!(
        "SELECT q.symbol, q.name FROM quote_snapshot q \
         JOIN (SELECT symbol, MAX(rowid) AS rowid FROM quote_snapshot \
               WHERE name IS NOT NULL AND TRIM(name) <> '' AND TRIM(name) <> '-' \
               AND TRIM(name) <> 'None' AND symbol IN ({placeholders}) \
               GROUP BY symbol) latest \
         ON latest.rowid=q.rowid"
    );
    let mut names = conn.prepare(&name_sql)?;
    let name_params: Vec<&dyn rusqlite::ToSql> = symbols
        .iter()
        .map(|item| item as &dyn rusqlite::ToSql)
        .collect();
    let known_names = names
        .query_map(name_params.as_slice(), |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<BTreeMap<String, String>, _>>()?
        .into_iter()
        .map(|(symbol, name)| (symbol, sanitized_name(Some(&name))))
        .collect();
    Ok((prices, known_names))
}

/// `prices` holds each symbol's newest real quote_snapshot mark (the POST
/// path overlays its own live quotes). Both `Position` derived fields and
/// `SlotView.market_value_cents` come from this one map, so a slot is exactly
/// the sum of its shown positions and never a cost-basis substitute.
fn account_view_from(
    account: &AccountRow,
    books: &[SlotBook; 2],
    prices: &BTreeMap<String, i64>,
    round_names: &BTreeMap<String, Option<String>>,
    as_of_round_id: Option<&str>,
) -> AccountView {
    let mut slots = Vec::new();
    let mut positions = Vec::new();
    let mut cash_cents: i64 = 0;
    let mut has_marks = true;
    for book in books {
        // The slot figure is the sum of exactly the same real marks shown on
        // its positions. A single unmarked position makes the whole slot
        // unknown (None), never a partial sum or a zero.
        let mut market_value = Some(0i64);
        let mut sellable_positions = 0;
        for position in &book.positions {
            let last_price_cents = prices
                .get(&position.symbol)
                .copied()
                .filter(|price| *price > 0);
            let market_value_cents =
                last_price_cents.map(|price| position.quantity * price);
            match market_value_cents {
                Some(value) => {
                    if let Some(total) = market_value.as_mut() {
                        *total += value;
                    }
                }
                None => {
                    // A missing real mark makes equity unknown, not zero. It
                    // never allows a fill and never falls back to avg_cost.
                    has_marks = false;
                    market_value = None;
                }
            }
            if position.sellable_quantity > 0 {
                sellable_positions += 1;
            }
            let mut shown = position.clone();
            shown.name = round_names
                .get(&position.symbol)
                .cloned()
                .unwrap_or(None);
            shown.last_price_cents = last_price_cents;
            shown.market_value_cents = market_value_cents;
            positions.push(shown);
        }
        cash_cents += book.cash_cents;
        slots.push(SlotView {
            slot: book.slot,
            cash_cents: book.cash_cents,
            market_value_cents: market_value,
            equity_cents: market_value.map(|value| value + book.cash_cents),
            positions: book.positions.len(),
            sellable_positions,
        });
    }
    let equity_cents = has_marks.then(|| {
        slots
            .iter()
            .filter_map(|slot| slot.equity_cents)
            .sum::<i64>()
    });
    let profit_cents = equity_cents.map(|equity| equity - account.initial_cash_cents);
    AccountView {
        id: account.id.clone(),
        initial_cash_cents: account.initial_cash_cents,
        cash_cents,
        created_at: account.created_at.clone(),
        as_of_round_id: as_of_round_id.map(str::to_string),
        equity_cents,
        profit_cents,
        daily_pnl_cents: None,
        daily_pnl_day: None,
        return_bps: profit_cents.map(|profit| {
            if account.initial_cash_cents > 0 {
                profit * 10_000 / account.initial_cash_cents
            } else {
                0
            }
        }),
        slots,
        positions,
    }
}

/// Use the last committed valuation from an earlier Shanghai session. A
/// ledger with no earlier session starts from its original cash. The caller
/// keeps account, marks, and the equity baseline in the same read snapshot.
fn set_daily_pnl(conn: &impl ConnLike, view: &mut AccountView) -> IntradayResult<()> {
    let mut statement = conn.prepare(
        "SELECT quote_time, equity_cents FROM equity_point ORDER BY rowid DESC",
    )?;
    let mut points = statement.query([])?;
    let Some(latest) = points.next()? else {
        return Ok(());
    };
    let Some(day) = shanghai_quote_day(&latest.get::<_, String>(0)?) else {
        return Ok(());
    };
    view.daily_pnl_day = Some(day.to_string());
    let Some(equity) = view.equity_cents else {
        return Ok(());
    };
    while let Some(point) = points.next()? {
        match shanghai_quote_day(&point.get::<_, String>(0)?) {
            Some(prior_day) if prior_day == day => continue,
            Some(prior_day) if prior_day < day => {
                view.daily_pnl_cents = equity.checked_sub(point.get(1)?);
            }
            _ => {} // A malformed or out-of-order predecessor cannot supply a baseline.
        }
        return Ok(());
    }
    view.daily_pnl_cents = equity.checked_sub(view.initial_cash_cents);
    Ok(())
}

fn shanghai_quote_day(stamp: &str) -> Option<NaiveDate> {
    if let Ok(at) = DateTime::parse_from_rfc3339(stamp) {
        return Some(at.with_timezone(&FixedOffset::east_opt(8 * 3600)?).date_naive());
    }
    // Legacy callers used local timestamp strings. Never truncate an offset
    // string that failed to parse, since it may belong to another session.
    ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f"]
        .iter()
        .find_map(|format| NaiveDateTime::parse_from_str(stamp, format).ok().map(|at| at.date()))
        .or_else(|| NaiveDate::parse_from_str(stamp, "%Y-%m-%d").ok())
}

fn stored_round_outcome_tx(
    transaction: &impl ConnLike,
    round_id: &str,
) -> IntradayResult<Option<RoundOutcome>> {
    let Some(mut round) = transaction
        .query_row(
            "SELECT round_id, started_at, finished_at, quote_count, trade_count, ai_ok, \
             ai_error, equity_cents, regime, note FROM round WHERE round_id=?",
            params![round_id],
            map_round,
        )
        .optional()?
    else {
        return Ok(None);
    };
    round.idempotent = false;
    let decisions = decorate_decisions(
        transaction,
        round_id,
        None,
        read_decisions_tx(transaction, round_id)?,
    )?;
    let trades = decorate_trades(transaction, None, read_trades_tx(transaction, round_id)?)?;
    let ai = read_ai_tx(transaction, round_id)?;
    // Replays are pure reads: no quote, decision, trade or equity row is added.
    // The account view is the one committed with this round, so a replay after
    // later rounds still returns the original result byte-for-byte.
    let account_json: String = transaction.query_row(
        "SELECT account_json FROM round WHERE round_id=?",
        params![round_id],
        |row| row.get(0),
    )?;
    let account_view: AccountView = serde_json::from_str(&account_json)?;
    Ok(Some(RoundOutcome {
        round,
        decisions,
        trades,
        account: account_view,
        ai,
    }))
}

fn mark_idempotent(mut outcome: RoundOutcome) -> RoundOutcome {
    outcome.round.idempotent = true;
    outcome
}

/// Reject blank placeholder names. Rust never invents a name from a symbol.
fn sanitized_name(name: Option<&str>) -> Option<String> {
    let trimmed = name?.trim();
    if trimmed.is_empty() || trimmed == "-" || trimmed == "None" {
        return None;
    }
    Some(trimmed.to_string())
}

/// Resolve display names for read models. The round's own quotes win; otherwise
/// fall back to the most recent non-empty name stored for that symbol. A symbol
/// with no real name anywhere stays NULL, including in an un-migrated old row.
fn resolve_names(
    conn: &impl ConnLike,
    round_id: &str,
    round_names: Option<&BTreeMap<String, Option<String>>>,
    symbols: &BTreeSet<String>,
) -> IntradayResult<BTreeMap<String, Option<String>>> {
    let has_name = conn
        .prepare("PRAGMA table_info(quote_snapshot)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?
        .iter()
        .any(|column| column == "name");
    let mut out = BTreeMap::new();
    for symbol in symbols {
        // A read-only old database can supply in-flight names from `round_names`
        // but has no stored column to query; no name is fabricated.
        let round_name = round_names
            .and_then(|names| names.get(symbol))
            .and_then(|name| sanitized_name(name.as_deref()));
        if !has_name {
            out.insert(symbol.clone(), round_name);
            continue;
        }
        let round_name = round_name.or_else(|| {
                conn.query_row(
                    "SELECT name FROM quote_snapshot WHERE round_id=? AND symbol=?",
                    params![round_id, symbol],
                    |row| row.get::<_, Option<String>>(0),
                )
                .optional()
                .ok()
                .flatten()
                .and_then(|name| sanitized_name(name.as_deref()))
            });
        let name = round_name.or_else(|| {
            conn.query_row(
                "SELECT name FROM quote_snapshot WHERE symbol=? \
                 AND name IS NOT NULL AND TRIM(name) <> '' AND TRIM(name) <> '-' \
                 AND TRIM(name) <> 'None' ORDER BY rowid DESC LIMIT 1",
                params![symbol],
                |row| row.get::<_, Option<String>>(0),
            )
            .optional()
            .ok()
            .flatten()
            .and_then(|name| sanitized_name(name.as_deref()))
        });
        out.insert(symbol.clone(), name);
    }
    Ok(out)
}

fn decorate_decisions(
    conn: &impl ConnLike,
    round_id: &str,
    round_names: Option<&BTreeMap<String, Option<String>>>,
    mut decisions: Vec<StoredDecision>,
) -> IntradayResult<Vec<StoredDecision>> {
    let symbols: BTreeSet<String> = decisions.iter().map(|item| item.symbol.clone()).collect();
    let names = resolve_names(conn, round_id, round_names, &symbols)?;
    for decision in &mut decisions {
        decision.name = names.get(&decision.symbol).cloned().unwrap_or(None);
        decision.side_label = labels::decision_side_label(&decision.side);
        decision.blocked_label =
            labels::blocked_label(decision.blocked_reason.as_deref());
        decision.slot_label = labels::slot_label(decision.slot.as_str());
    }
    Ok(decisions)
}

fn decorate_trades(
    conn: &impl ConnLike,
    round_names: Option<&BTreeMap<String, Option<String>>>,
    mut trades: Vec<TradeRecord>,
) -> IntradayResult<Vec<TradeRecord>> {
    // A GET may mix many rounds. Name resolution is per trade's own round;
    // group them so an in-flight round's quote names cannot leak across days.
    let mut by_round: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (index, trade) in trades.iter().enumerate() {
        by_round
            .entry(trade.round_id.clone())
            .or_default()
            .push(index);
    }
    for (round_id, indices) in by_round {
        let round_symbols: BTreeSet<String> = indices
            .iter()
            .map(|index| trades[*index].symbol.clone())
            .collect();
        let names = resolve_names(conn, &round_id, round_names, &round_symbols)?;
        for index in indices {
            let side = trades[index].side.clone();
            let reason = trades[index].reason.clone();
            trades[index].name = names.get(&trades[index].symbol).cloned().unwrap_or(None);
            trades[index].side_label = labels::trade_side_label(&side);
            trades[index].reason_label = labels::trade_reason_label(&reason);
        }
    }
    Ok(trades)
}

fn read_decisions_tx(
    transaction: &impl ConnLike,
    round_id: &str,
) -> IntradayResult<Vec<StoredDecision>> {
    let mut statement = transaction.prepare(
        "SELECT round_id, slot, symbol, score, threshold, reached_threshold, side, \
         blocked_reason, ai_bias, ai_confidence, ai_applied, factors_json, note, created_at \
         FROM decision WHERE round_id=? ORDER BY symbol, slot",
    )?;
    let rows = statement.query_map(params![round_id], map_decision)?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

fn read_trades_tx(
    transaction: &impl ConnLike,
    round_id: &str,
) -> IntradayResult<Vec<TradeRecord>> {
    let mut statement = transaction.prepare(
        "SELECT id, round_id, slot, symbol, side, quantity, price_cents, fee_cents, \
         reason, ai_applied, created_at FROM trade WHERE round_id=? ORDER BY id",
    )?;
    let rows = statement.query_map(params![round_id], map_trade)?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

fn read_ai_tx(
    transaction: &impl ConnLike,
    round_id: &str,
) -> IntradayResult<AiApplication> {
    let application_json: Option<String> = transaction
        .query_row(
            "SELECT application_json FROM ai_observation WHERE round_id=?",
            params![round_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(application_json) = application_json else {
        return Ok(AiApplication::neutral(round_id, "ai_observation_missing"));
    };
    let mut ai: AiApplication = serde_json::from_str(&application_json)?;
    ai.round_id = round_id.to_string();
    Ok(ai)
}

fn map_decision(row: &rusqlite::Row<'_>) -> Result<StoredDecision, rusqlite::Error> {
    let slot = Slot::parse(&row.get::<_, String>(1)?).unwrap_or(Slot::Steady);
    Ok(StoredDecision {
        round_id: row.get(0)?,
        slot,
        symbol: row.get(2)?,
        name: None,
        side_label: String::new(),
        blocked_label: String::new(),
        slot_label: labels::slot_label(slot.as_str()),
        score: row.get(3)?,
        threshold: row.get(4)?,
        reached_threshold: row.get::<_, i64>(5)? != 0,
        side: row.get(6)?,
        blocked_reason: row.get(7)?,
        ai_bias: row.get(8)?,
        ai_confidence: row.get(9)?,
        ai_applied: row.get::<_, i64>(10)? != 0,
        factors_json: serde_json::from_str(&row.get::<_, String>(11)?)
            .unwrap_or(Value::Null),
        note: row.get(12)?,
        created_at: row.get(13)?,
    })
}

fn map_trade(row: &rusqlite::Row<'_>) -> Result<TradeRecord, rusqlite::Error> {
    let slot = Slot::parse(&row.get::<_, String>(2)?).unwrap_or(Slot::Steady);
    Ok(TradeRecord {
        id: row.get(0)?,
        round_id: row.get(1)?,
        slot,
        symbol: row.get(3)?,
        name: None,
        side_label: String::new(),
        reason_label: String::new(),
        side: row.get(4)?,
        quantity: row.get(5)?,
        price_cents: row.get(6)?,
        fee_cents: row.get(7)?,
        reason: row.get(8)?,
        ai_applied: row.get::<_, i64>(9)? != 0,
        created_at: row.get(10)?,
    })
}

/// A small uniform read surface for `Connection` and `Transaction`.
trait ConnLike {
    fn prepare(&self, sql: &str) -> Result<rusqlite::Statement<'_>, rusqlite::Error>;
    fn query_row<T, F, P>(&self, sql: &str, params: P, f: F) -> Result<T, rusqlite::Error>
    where
        P: rusqlite::Params,
        F: FnOnce(&rusqlite::Row<'_>) -> Result<T, rusqlite::Error>;
    fn execute<P>(&self, sql: &str, params: P) -> Result<usize, rusqlite::Error>
    where
        P: rusqlite::Params;
}

impl ConnLike for Connection {
    fn prepare(&self, sql: &str) -> Result<rusqlite::Statement<'_>, rusqlite::Error> {
        Connection::prepare(self, sql)
    }
    fn query_row<T, F, P>(&self, sql: &str, params: P, f: F) -> Result<T, rusqlite::Error>
    where
        P: rusqlite::Params,
        F: FnOnce(&rusqlite::Row<'_>) -> Result<T, rusqlite::Error>,
    {
        Connection::query_row(self, sql, params, f)
    }
    fn execute<P>(&self, sql: &str, params: P) -> Result<usize, rusqlite::Error>
    where
        P: rusqlite::Params,
    {
        Connection::execute(self, sql, params)
    }
}

impl ConnLike for Transaction<'_> {
    fn prepare(&self, sql: &str) -> Result<rusqlite::Statement<'_>, rusqlite::Error> {
        (**self).prepare(sql)
    }
    fn query_row<T, F, P>(&self, sql: &str, params: P, f: F) -> Result<T, rusqlite::Error>
    where
        P: rusqlite::Params,
        F: FnOnce(&rusqlite::Row<'_>) -> Result<T, rusqlite::Error>,
    {
        (**self).query_row(sql, params, f)
    }
    fn execute<P>(&self, sql: &str, params: P) -> Result<usize, rusqlite::Error>
    where
        P: rusqlite::Params,
    {
        (**self).execute(sql, params)
    }
}

fn map_round(row: &rusqlite::Row<'_>) -> Result<RoundSummary, rusqlite::Error> {
    let ai_ok = row.get::<_, i64>(5)? != 0;
    let ai_error: Option<String> = row.get(6)?;
    let regime: String = row.get(8)?;
    let note: String = row.get(9)?;
    Ok(RoundSummary {
        round_id: row.get(0)?,
        started_at: row.get(1)?,
        finished_at: row.get(2)?,
        quote_count: row.get::<_, i64>(3)? as usize,
        trade_count: row.get::<_, i64>(4)? as usize,
        ai_ok,
        ai_label: labels::ai_label(ai_ok, ai_error.as_deref()),
        ai_error,
        equity_cents: row.get(7)?,
        regime_label: labels::regime_label(&regime),
        regime,
        note_label: labels::note_label(&note),
        note,
        idempotent: false,
    })
}

fn audit_regime(quotes: &[Quote]) -> String {
    let mut advances = 0;
    let mut declines = 0;
    let mut sum = 0i64;
    let mut count = 0i64;
    for quote in quotes {
        if !quote.has_live_price() || quote.prev_close_cents <= 0 {
            continue;
        }
        let change = quote.price_cents - quote.prev_close_cents;
        if change > 0 {
            advances += 1;
        } else if change < 0 {
            declines += 1;
        }
        sum += change * 10_000 / quote.prev_close_cents;
        count += 1;
    }
    if count == 0 {
        return "no_tradable_breadth".into();
    }
    let mean = sum / count;
    if mean > 50 && advances * 2 > count as usize {
        "risk_on".into()
    } else if mean < -50 && declines * 2 > count as usize {
        "risk_off".into()
    } else {
        "mixed".into()
    }
}

pub(crate) fn round_note(
    request: &RunRoundRequest,
    signals: &[DecisionSignal],
    ai: &AiApplication,
) -> String {
    let mut notes = Vec::new();
    let with_history = signals
        .iter()
        .filter(|signal| signal.factors.used_history)
        .count();
    let no_history = signals.len() - with_history;
    // Three mutually exclusive states. The previous wording printed
    // "no_trailing_history_supplied" beside a non-zero explicit count, which
    // reads as a contradiction in the audit trail.
    let total = signals.len();
    if total > 0 && with_history == 0 {
        notes.push(format!("no_trailing_history_supplied; snapshot_only={no_history}"));
    } else if with_history > 0 && no_history > 0 {
        notes.push(format!(
            "partial_trailing_history={with_history}; snapshot_only={no_history}"
        ));
    } else if with_history > 0 {
        notes.push(format!("explicit_trailing_closes={with_history}"));
    }
    notes.push(format!("quotes={}", request.quotes.len()));
    if let Some(error) = &ai.ai_error {
        notes.push(format!("ai_fail_closed={error}"));
    } else if ai.ai_ok() {
        notes.push("ai_applied=1".into());
    }
    if let Some(note) = &request.note {
        notes.push(bounded_text(note, MAX_REASON_CHARS));
    }
    notes.join("; ")
}

fn slot_index(slot: Slot) -> usize {
    match slot {
        Slot::Steady => 0,
        Slot::Aggressive => 1,
    }
}

fn require_bounded<'a>(value: &'a str, name: &str, max: usize) -> IntradayResult<&'a str> {
    if value.trim().is_empty() || value.chars().count() > max {
        return Err(IntradayError::Rules(format!("invalid_{name}")));
    }
    Ok(value)
}

fn bounded_text(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}
