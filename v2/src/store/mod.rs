//! SQLite is an append-only simulation ledger with exactly one Rust write owner.
use crate::domain::{decide, validate_quote_import, Fill, Lot, Outcome, POLICY_VERSION, PortfolioState, QuoteEvidence, QuoteImport, QuoteRef, QuoteTrust, RuleError, SimulationRequest};
use crate::domain::{TradingCalendar, SessionPolicy, BUNDLED_CALENDAR_JSON, BUNDLED_SESSION_POLICY_JSON};
use crate::domain::{InstrumentSnapshot, BUNDLED_INSTRUMENTS_JSON};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use std::fmt;
use crate::domain::{Experiment, ExperimentSpec, ExperimentStatus, StrategyArtifact, MARKET_DATA_PACK_ID, MarketDataPack, MarketDataPackManifest};
use crate::domain::{ResearchInputManifest, ResearchOutput};

#[derive(Debug)]
pub enum StoreError {
    Database(rusqlite::Error),
    Encoding(serde_json::Error),
    Rules(RuleError),
    Conflict(String),
    Integrity(String),
    NotFound(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Database(error) => write!(f, "database: {error}"),
            Self::Encoding(error) => write!(f, "encoding: {error}"),
            Self::Rules(error) => write!(f, "rules: {error}"),
            Self::Conflict(error) => write!(f, "conflict: {error}"),
            Self::Integrity(error) => write!(f, "integrity: {error}"),
            Self::NotFound(error) => write!(f, "not found: {error}"),
        }
    }
}
impl std::error::Error for StoreError {}
impl From<rusqlite::Error> for StoreError {
    fn from(value: rusqlite::Error) -> Self { Self::Database(value) }
}
impl From<serde_json::Error> for StoreError {
    fn from(value: serde_json::Error) -> Self { Self::Encoding(value) }
}
impl From<RuleError> for StoreError {
    fn from(value: RuleError) -> Self { Self::Rules(value) }
}

pub type StoreResult<T> = Result<T, StoreError>;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunRecord {
    pub id: i64,
    pub request: SimulationRequest,
    pub evidence: Option<QuoteEvidence>,
    pub calendar_version: String,
    pub session_policy_version: String,
    pub instrument_version: String,
    pub outcome: Outcome,
    pub duplicate: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PositionView {
    pub symbol: String,
    pub quantity: i64,
    pub sellable_quantity: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioView {
    pub cycle_id: String,
    pub initial_cash_cents: i64,
    pub cash_cents: i64,
    pub asof_day: Option<String>,
    pub positions: Vec<PositionView>,
    pub lots: Vec<Lot>,
    pub runs_total: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayReport {
    pub cycle_id: String,
    pub ok: bool,
    pub runs_verified: usize,
    pub fills_verified: usize,
    pub cash_cents: i64,
}

/// One strategy slot of a paper account: its own budget, cash and positions.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperSlotView {
    pub slot: String,
    pub strategy_version_id: String,
    pub research_input_id: String,
    pub market_data_pack_id: String,
    pub execution_policy_version: String,
    pub weight_bps: i64,
    pub budget_cents: i64,
    pub cash_cents: i64,
    pub positions: Vec<PositionView>,
    pub orders_total: i64,
}

/// One user-funded paper account with two frozen strategy slots.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperAccountView {
    pub account_id: String,
    pub initial_cash_cents: i64,
    pub created_at: String,
    /// Reserve plus every slot's cash. Never below zero, never above initial.
    pub cash_cents: i64,
    pub reserve_cents: i64,
    pub slots: Vec<PaperSlotView>,
    /// No reviewed price set exists yet, so no market value or return is computed.
    pub valuation: PaperValuation,
    pub limits: crate::domain::PaperRiskLimits,
}

/// Summary returned by a batch run.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperSessionRun {
    pub session: PaperSessionView,
    pub steps: usize,
    pub filled: usize,
    pub rejected: usize,
}

/// One slot's price requirements for a valuation snapshot.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperValuationRequirement {
    pub slot: String,
    pub market_data_pack_id: String,
    pub symbols: Vec<String>,
}

/// Why a valuation cannot be produced. Deliberately contains no value fields:
/// until a reviewed price set exists, nothing may be marked to market.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperValuationReport {
    pub account_id: String,
    pub as_of_day: String,
    pub status: String,
    pub reason: String,
    pub requirements: Vec<PaperValuationRequirement>,
    pub missing: Vec<String>,
    pub priced_symbols: usize,
    /// Present only when a reviewed set covered the book.
    pub valuation: Option<crate::domain::AccountValuation>,
    pub digest: String,
}

/// Valuation availability for an account. Always explicit, never inferred.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperValuation {
    pub status: String,
    pub reason: String,
}

/// One window of a rolling validation, valued or explicitly unavailable.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentResult {
    pub days: Vec<String>,
    pub status: String,
    pub reason: String,
    pub performance: Option<ExperimentPerformanceReport>,
}

/// Rolling windows for one experiment; each window must stand on its own.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentReport {
    pub cycle_id: String,
    pub segments: Vec<SegmentResult>,
    pub digest: String,
}

/// Performance metrics for one experiment cycle over a reviewed series.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExperimentPerformanceReport {
    pub cycle_id: String,
    pub days: usize,
    pub start_equity_cents: i64,
    pub end_equity_cents: i64,
    pub profit_cents: i64,
    pub return_bps: i64,
    pub max_drawdown_cents: i64,
    pub max_drawdown_day: Option<String>,
    pub drawdown_bps: i64,
    pub fees_cents: i64,
    pub trades: usize,
    pub price_set_id: String,
    pub digest: String,
}

/// Performance metrics over a complete reviewed equity series.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PerformanceReport {
    pub account_id: String,
    pub days: usize,
    pub start_equity_cents: i64,
    pub end_equity_cents: i64,
    pub profit_cents: i64,
    pub return_bps: i64,
    pub max_drawdown_cents: i64,
    pub max_drawdown_day: Option<String>,
    pub drawdown_bps: i64,
    pub fees_cents: i64,
    pub trades: usize,
    pub price_set_id: String,
    pub digest: String,
}

/// Which (symbol, day) pairs a reviewed price set still has to cover.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct MissingPrice {
    pub symbol: String,
    pub day: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PriceCoverageReport {
    pub pack_id: String,
    pub symbols: Vec<String>,
    pub days: Vec<String>,
    pub covered_pairs: i64,
    pub missing: Vec<MissingPrice>,
    pub digest: String,
}

/// Session counts for one account, by lifecycle status.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionCounts {
    pub created: i64,
    pub running: i64,
    pub paused: i64,
    pub completed: i64,
    pub stopped: i64,
}

/// Compact slot facts for the multi-account overview.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountSlotOverview {
    pub slot: String,
    pub weight_bps: i64,
    pub budget_cents: i64,
    pub cash_cents: i64,
}

/// One row of the account overview: money, sessions and audit health.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountOverview {
    pub account_id: String,
    pub initial_cash_cents: i64,
    pub cash_cents: i64,
    pub reserve_cents: i64,
    pub created_at: String,
    pub slots: Vec<AccountSlotOverview>,
    pub sessions: SessionCounts,
    pub audit_ok: bool,
}

/// Auditable snapshot of a filtered slice of the evolution log.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvolutionExport {
    pub schema_version: u32,
    pub candidate_id: Option<String>,
    pub status: Option<String>,
    pub from_day: Option<String>,
    pub to_day: Option<String>,
    pub count: i64,
    pub events: Vec<EvolutionEventRecord>,
    pub digest: String,
}

/// Query options for the evolution event log (all filters optional).
#[derive(Clone, Copy, Debug, Default)]
pub struct EvolutionQuery<'a> {
    pub limit: i64,
    pub candidate_id: Option<&'a str>,
    pub status: Option<&'a str>,
    pub base_cycle: Option<&'a str>,
    pub candidate_cycle: Option<&'a str>,
    /// Inclusive China-day bounds (`YYYY-MM-DD`).
    pub from_day: Option<&'a str>,
    pub to_day: Option<&'a str>,
}

/// Cursor-paginated variant of the same filters.
#[derive(Clone, Copy, Debug)]
pub struct EvolutionPageQuery<'a> {
    pub query: EvolutionQuery<'a>,
    pub before: Option<&'a str>,
    pub before_id: Option<&'a str>,
    pub after: Option<&'a str>,
}

/// One page of the evolution event log, newest first.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvolutionEventPage {
    pub events: Vec<EvolutionEventRecord>,
    pub next_before: Option<String>,
    pub next_before_id: Option<String>,
    pub scanned: usize,
}

/// Version-independent identity of one research signal.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignalFingerprint {
    pub decision_at: String,
    pub quote_ref: crate::domain::QuoteRef,
    pub symbol: String,
    pub side: crate::domain::Side,
}

/// Read-only comparison of two experiments' signals.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignalDiffReport {
    pub base_cycle: String,
    pub candidate_cycle: String,
    pub base_runs: usize,
    pub candidate_runs: usize,
    pub matched: i64,
    pub base_only: Vec<SignalFingerprint>,
    pub candidate_only: Vec<SignalFingerprint>,
    pub digest: String,
}

/// One persisted evolution decision. Evidence metrics travel with it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvolutionEventRecord {
    pub event_id: String,
    pub candidate_id: String,
    pub status: String,
    pub decision: crate::domain::EvolutionDecision,
    pub decided_at: String,
    /// Optional experiment whose metrics formed the incumbent side.
    pub base_cycle: Option<String>,
    /// Optional experiment that produced the candidate metrics.
    pub candidate_cycle: Option<String>,
    pub digest: String,
}

/// Aggregate result of advancing every running session on an account.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountAdvance {
    pub account_id: String,
    pub day: String,
    pub sessions: Vec<PaperSessionRun>,
    pub steps: usize,
    pub filled: usize,
    pub rejected: usize,
}

/// One applied capital rebalance: cash moved between slots plus the weight change.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RebalanceRecord {
    pub account_id: String,
    pub from_slot: String,
    pub to_slot: String,
    pub amount_cents: i64,
    pub weights_before: [i64; 2],
    pub weights_after: [i64; 2],
    pub applied_at: String,
    pub note: String,
    pub proposal_digest: String,
    pub digest: String,
}

/// Allocation proposal plus the scores it was derived from.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AllocationReport {
    pub proposal: crate::domain::AllocationProposal,
    pub scores: Vec<crate::domain::SlotScore>,
    pub policy: crate::domain::AllocationPolicy,
}

/// One invariant check in an account audit.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperAuditCheck {
    pub name: String,
    pub ok: bool,
    pub detail: String,
}

/// Read-only proof that the account ledger still balances and stays linked.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperAuditReport {
    pub account_id: String,
    pub ok: bool,
    pub checks: Vec<PaperAuditCheck>,
    pub digest: String,
}

/// Status snapshot of one deterministic paper session.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperSessionView {
    pub session_id: String,
    pub account_id: String,
    pub status: String,
    pub clock_at: String,
    pub cursor: usize,
    pub total: usize,
    pub last_order_id: Option<i64>,
}

/// Result of advancing one deterministic step.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperSessionStep {
    pub session: PaperSessionView,
    pub order: Option<PaperOrderRecord>,
}

/// Everything the account form needs to build a valid request.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategyCatalogue {
    pub strategies: Vec<StrategySummary>,
    pub market_data_pack_id: String,
    pub execution_policy_version: String,
    pub reserve_bps: i64,
}

/// Reviewed strategy available to the account form. Content identity only.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategySummary {
    pub strategy_version_id: String,
    pub research_input_id: String,
    pub research_input_digest: String,
}

/// Lightweight account row for the console picker.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperAccountSummarySlot {
    pub slot: String,
    pub strategy_version_id: String,
    pub weight_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperAccountSummary {
    pub account_id: String,
    pub initial_cash_cents: i64,
    pub reserve_bps: i64,
    pub created_at: String,
    pub slots: Vec<PaperAccountSummarySlot>,
}

/// One simulated order recorded against a strategy slot.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperOrderRecord {
    pub id: i64,
    pub account_id: String,
    pub slot: String,
    pub strategy_version_id: String,
    pub request: crate::domain::PaperOrderRequest,
    pub outcome: Outcome,
    pub duplicate: bool,
}

/// Read-only catalogue: available rows plus how many stored rows exist and how
/// many are unreadable. A single corrupt row never hides the rest.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExperimentCatalogue {
    pub experiments: Vec<ExperimentSummary>,
    pub total: i64,
    pub skipped: usize,
}

/// Read-only catalogue row for the experiment picker. Summary of stored facts
/// only: no valuation, no derived return, no invented timestamp.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExperimentSummary {
    pub cycle_id: String,
    pub status: ExperimentStatus,
    pub strategy_version_id: String,
    pub research_input_id: String,
    pub market_data_pack_id: String,
    pub execution_policy_version: String,
    pub initial_cash_cents: i64,
    pub runs_total: i64,
}

/// Proof that one experiment realized exactly the reviewed research signals for
/// its frozen strategy definition and frozen research input. It describes
/// research identity only: fills, prices, fees and cash stay with replay.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResearchReproductionReport {
    pub cycle_id: String,
    pub ok: bool,
    pub strategy_version_id: String,
    pub research_input_id: String,
    pub research_output_id: String,
    pub research_output_digest: String,
    pub parameters_digest: String,
    pub source_digest: String,
    pub signals_verified: usize,
}

pub struct Store {
    conn: Connection,
}

impl Store {
    pub fn open(path: &str) -> StoreResult<Self> {
        Self::with_connection(Connection::open(path)?)
    }

    pub fn in_memory() -> StoreResult<Self> {
        Self::with_connection(Connection::open_in_memory()?)
    }

    fn with_connection(mut conn: Connection) -> StoreResult<Self> {
        const APPLICATION_ID: i64 = 0x41535632; // ASV2, independent SQLite file identity.
        const SCHEMA_VERSION: i64 = 14;
        // Everything above the acceptance boundary is read-only. In particular,
        // journal_mode, CREATE, metadata writes and transactions must wait.
        let objects: Vec<(String, String, String)> = conn.prepare(
            "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
        )?.query_map([], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?
            .collect::<Result<_, _>>()?;
        let app_id: i64 = conn.query_row("PRAGMA application_id", [], |r| r.get(0))?;
        let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        let fresh = objects.is_empty() && app_id == 0 && version == 0;
        if !fresh {
            if objects.iter().any(|(_, name, _)| matches!(name.as_str(),
                "paper_orders" | "paper_fills" | "dual_ai_tuning_runs")) {
                return Err(StoreError::Integrity("refusing_to_open_legacy_v1_database".into()));
            }
            if app_id != APPLICATION_ID || version != SCHEMA_VERSION {
                return Err(StoreError::Integrity("database_identity_or_schema_version_mismatch".into()));
            }
            let tables: &[(&str, &[&str])] = &[
                ("v2_meta", &["key", "value"]),
                ("v2_experiments", &["cycle_id", "spec_json", "status", "pack_manifest_json"]),
                ("v2_market_data_packs", &["pack_id", "pack_json"]),
                ("v2_research_inputs", &["research_input_id", "manifest_json"]),
                ("v2_strategy_artifacts", &["strategy_version_id", "artifact_json"]),
                ("v2_simulation_runs", &["id", "cycle_id", "strategy_version_id", "signal_id", "request_json", "evidence_json", "outcome_json", "calendar_version", "session_policy_version", "instrument_version"]),
                ("v2_fills", &["id", "run_id", "fill_json"]),
                ("v2_quote_evidence", &["pack_id", "source", "quote_id", "evidence_json"]),
                ("v2_paper_accounts", &["account_id", "spec_json", "created_at", "weights_json"]),
                ("v2_paper_orders", &["id", "account_id", "slot", "strategy_version_id", "signal_id", "request_json", "evidence_json", "outcome_json", "calendar_version", "session_policy_version", "instrument_version"]),
                ("v2_paper_fills", &["id", "order_id", "fill_json"]),
                ("v2_paper_sessions", &["session_id", "account_id", "spec_json", "status", "clock_at", "cursor", "last_order_id"]),
                ("v2_paper_transfers", &["id", "account_id", "from_slot", "to_slot", "amount_cents", "applied_at", "note", "digest", "weights_before_json", "weights_after_json"]),
                ("v2_evolution_events", &["event_id", "candidate_id", "status", "decision_json", "decided_at", "digest", "base_cycle", "candidate_cycle"]),
            ];
            for (kind, name, table) in &objects {
                let known = (kind == "table" && tables.iter().any(|(t, _)| t == name))
                    || (kind == "index" && name == "idx_v2_runs_cycle" && table == "v2_simulation_runs")
                || (kind == "index" && name == "idx_v2_paper_orders_account" && table == "v2_paper_orders");
                if !known {
                    return Err(StoreError::Integrity("unknown_database_schema".into()));
                }
            }
            for (table, expected_columns) in tables {
                let actual: Vec<String> = conn.prepare(&format!("PRAGMA table_info({table})"))?
                    .query_map([], |r| r.get(1))?.collect::<Result<_, _>>()?;
                if actual != *expected_columns {
                    return Err(StoreError::Integrity("unexpected_or_incomplete_v2_schema".into()));
                }
            }
            let pack_ids = conn.prepare("SELECT pack_id FROM v2_market_data_packs")?
                .query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
            for id in pack_ids { load_market_pack(&conn, &id)?; }
            let input_ids = conn.prepare("SELECT research_input_id FROM v2_research_inputs")?
                .query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
            for id in input_ids { load_research_input(&conn, &id)?; }
            let mut stmt = conn.prepare("SELECT cycle_id FROM v2_experiments")?;
            let ids = stmt.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
            for id in ids { load_experiment(&conn, &id)?; }

        }

        if fresh {
            let calendar: TradingCalendar = serde_json::from_str(BUNDLED_CALENDAR_JSON)?;
            let sessions: SessionPolicy = serde_json::from_str(BUNDLED_SESSION_POLICY_JSON)?;
            let instruments: InstrumentSnapshot = serde_json::from_str(BUNDLED_INSTRUMENTS_JSON)?;
            calendar.validate()?;
            sessions.validate()?;
            instruments.validate()?;
        } // Existing packs and experiment references were verified above, before writes.
        // Accepted empty/current V2 file only. No refused file reaches a write.
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;")?;
        if fresh {
            let txn = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
            txn.execute_batch(
                "CREATE TABLE v2_meta (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                );
                CREATE TABLE v2_market_data_packs (
                    pack_id TEXT PRIMARY KEY NOT NULL,
                    pack_json TEXT NOT NULL
                );
                CREATE TABLE v2_research_inputs (
                    research_input_id TEXT PRIMARY KEY NOT NULL,
                    manifest_json TEXT NOT NULL
                );
                CREATE TABLE v2_strategy_artifacts (
                    strategy_version_id TEXT PRIMARY KEY NOT NULL,
                    artifact_json TEXT NOT NULL
                );
                CREATE TABLE v2_experiments (
                    cycle_id TEXT PRIMARY KEY NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','completed')),
                    pack_manifest_json TEXT NOT NULL
                );
                CREATE TABLE v2_simulation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL REFERENCES v2_experiments(cycle_id) ON DELETE RESTRICT,
                    strategy_version_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    calendar_version TEXT NOT NULL,
                    session_policy_version TEXT NOT NULL,
                    instrument_version TEXT NOT NULL,
                    UNIQUE(cycle_id, strategy_version_id, signal_id)
                );
                CREATE TABLE v2_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE
                        REFERENCES v2_simulation_runs(id) ON DELETE RESTRICT,
                    fill_json TEXT NOT NULL
                );
                CREATE INDEX idx_v2_runs_cycle ON v2_simulation_runs(cycle_id, id);
                CREATE TABLE v2_paper_accounts (
                    account_id TEXT PRIMARY KEY NOT NULL,
                    spec_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    weights_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE v2_paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES v2_paper_accounts(account_id) ON DELETE RESTRICT,
                    slot TEXT NOT NULL CHECK(slot IN ('A','B')),
                    strategy_version_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    calendar_version TEXT NOT NULL,
                    session_policy_version TEXT NOT NULL,
                    instrument_version TEXT NOT NULL,
                    UNIQUE(account_id, slot, strategy_version_id, signal_id)
                );
                CREATE INDEX idx_v2_paper_orders_account ON v2_paper_orders(account_id, slot, id);
                CREATE TABLE v2_paper_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL UNIQUE REFERENCES v2_paper_orders(id) ON DELETE RESTRICT,
                    fill_json TEXT NOT NULL
                );
                CREATE TABLE v2_paper_sessions (
                    session_id TEXT PRIMARY KEY NOT NULL,
                    account_id TEXT NOT NULL REFERENCES v2_paper_accounts(account_id) ON DELETE RESTRICT,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('created','running','paused','stopped','completed')),
                    clock_at TEXT NOT NULL,
                    cursor INTEGER NOT NULL,
                    last_order_id INTEGER
                );
                CREATE TABLE v2_paper_transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL REFERENCES v2_paper_accounts(account_id) ON DELETE RESTRICT,
                    from_slot TEXT NOT NULL CHECK(from_slot IN ('A','B')),
                    to_slot TEXT NOT NULL CHECK(to_slot IN ('A','B')),
                    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
                    applied_at TEXT NOT NULL,
                    note TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    weights_before_json TEXT NOT NULL,
                    weights_after_json TEXT NOT NULL,
                    UNIQUE(account_id, digest)
                );
                CREATE TABLE v2_evolution_events (
                    event_id TEXT PRIMARY KEY NOT NULL,
                    candidate_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    base_cycle TEXT,
                    candidate_cycle TEXT
                );
                CREATE TABLE v2_quote_evidence (
                    pack_id TEXT NOT NULL REFERENCES v2_market_data_packs(pack_id) ON DELETE RESTRICT,
                    source TEXT NOT NULL,
                    quote_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    PRIMARY KEY(pack_id, source, quote_id)
                );"
            )?;
            txn.pragma_update(None, "application_id", APPLICATION_ID)?;
            txn.pragma_update(None, "user_version", SCHEMA_VERSION)?;
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('storage_contract','pack-ledger-v1')", [])?;
            register_pack(&txn, &reviewed_market_pack(MARKET_DATA_PACK_ID)?)?;
            txn.commit()?;
        }
        Ok(Self { conn })
    }

    /// Explicit, immutable capital ownership; no implicit default experiment.
    pub fn create_experiment(&mut self, spec: ExperimentSpec) -> StoreResult<Experiment> {
        validate_experiment_spec(&spec)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing: Option<String> = txn.query_row("SELECT spec_json FROM v2_experiments WHERE cycle_id=?", [&spec.cycle_id], |r| r.get(0)).optional()?;
        if let Some(raw) = existing {
            if serde_json::from_str::<ExperimentSpec>(&raw)? != spec {
                return Err(StoreError::Conflict("experiment_identity_payload_conflict".into()));
            }
            return load_experiment(&txn, &spec.cycle_id);
        }
        let pack = reviewed_market_pack(&spec.market_data_pack_id)?;
        register_pack(&txn, &pack)?;
        register_research_input(&txn, &reviewed_research_input(&spec.strategy.research_input_id)?)?;
        let artifact: Option<String> = txn.query_row("SELECT artifact_json FROM v2_strategy_artifacts WHERE strategy_version_id=?", [&spec.strategy.strategy_version_id], |r| r.get(0)).optional()?;
        if let Some(raw) = artifact {
            if serde_json::from_str::<StrategyArtifact>(&raw)? != spec.strategy {
                return Err(StoreError::Conflict("strategy_artifact_identity_conflict".into()));
            }
        } else {
            txn.execute("INSERT INTO v2_strategy_artifacts VALUES(?,?)", params![spec.strategy.strategy_version_id, serde_json::to_string(&spec.strategy)?])?;
        }
        txn.execute("INSERT INTO v2_experiments VALUES(?,?,'active',?)", params![spec.cycle_id, serde_json::to_string(&spec)?, serde_json::to_string(&pack.manifest)?])?;
        txn.commit()?;
        Ok(Experiment { spec, status: ExperimentStatus::Active })
    }

    pub fn complete_experiment(&mut self, cycle_id: &str) -> StoreResult<Experiment> {
        // The HTTP mutex serializes this verification and transition with submit.
        // BEGIN IMMEDIATE also serializes against other SQLite connections.
        let txn = rusqlite::Transaction::new_unchecked(&self.conn, TransactionBehavior::Immediate)?;
        self.verify_replay(cycle_id)?;
        txn.execute("UPDATE v2_experiments SET status='completed' WHERE cycle_id=?", [cycle_id])?;
        let result = load_experiment(&txn, cycle_id)?;
        txn.commit()?;
        Ok(result)
    }

    /// Only reviewed IDs are accepted; there is no arbitrary pack upload API.
    pub fn register_reviewed_pack(&mut self, pack_id: &str) -> StoreResult<MarketDataPack> {
        let pack = reviewed_market_pack(pack_id)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        register_pack(&txn, &pack)?;
        txn.commit()?;
        Ok(pack)
    }

    /// Immutable evidence, owned by the same single writer as the ledger.
    pub fn ingest_quote(&mut self, input: QuoteImport) -> StoreResult<QuoteEvidence> {
        validate_quote_import(&input)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (_, _, instruments) = load_market_context(&txn, &input.market_data_pack_id)?;
        let identity = QuoteRef {
            market_data_pack_id: input.market_data_pack_id.clone(),
            source: input.quote.source.clone(), quote_id: input.quote.quote_id.clone(),
        };
        if let Some(stored) = load_evidence(&txn, &identity)? {
            if stored.quote != input.quote || stored.available_at != input.available_at
                || stored.execution != input.execution {
                return Err(StoreError::Conflict("quote_identity_payload_conflict".into()));
            }
            verify_instrument_identity(&instruments, &stored)?;
            return Ok(stored);
        }
        // Only this Rust ingress records actual receipt. Caller declarations
        // remain offline claims and never become authenticated provider facts.
        let received_at = chrono::Utc::now()
            .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).expect("valid China offset"))
            .to_rfc3339();
        let evidence = QuoteEvidence {
            schema_version: 5,
            market_data_pack_id: input.market_data_pack_id,
            instrument: instruments.identity(&input.quote.symbol).cloned(),
            available_at: input.available_at, quote: input.quote,
            execution: input.execution,
            trust_level: QuoteTrust::OfflineFixture,
            provider_published_at: None, received_at,
        };
        txn.execute(
            "INSERT INTO v2_quote_evidence(pack_id,source,quote_id,evidence_json) VALUES(?,?,?,?)",
            params![identity.market_data_pack_id, identity.source, identity.quote_id, serde_json::to_string(&evidence)?],
        )?;
        txn.commit()?;
        Ok(evidence)
    }

    /// All mutations and idempotency checks occur under one IMMEDIATE transaction.
    pub fn submit(&mut self, request: SimulationRequest) -> StoreResult<RunRecord> {
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let experiment = load_experiment(&txn, &request.cycle_id)?;
        if request.schema_version != 4 {
            return Err(StoreError::Rules(RuleError("unsupported_contract_version".into())));
        }
        if request.strategy_version_id != experiment.spec.strategy.strategy_version_id {
            return Err(StoreError::Conflict("experiment_strategy_mismatch".into()));
        }
        if request.quote_ref.market_data_pack_id != experiment.spec.market_data_pack_id {
            return Err(StoreError::Conflict("experiment_quote_data_pack_mismatch".into()));
        }
        let (calendar, sessions, instruments) = load_market_context(&txn, &experiment.spec.market_data_pack_id)?;
        let prior: Option<(i64, String, String, String, String, String, String)> = txn.query_row(
            "SELECT id,request_json,outcome_json,evidence_json,calendar_version,session_policy_version,instrument_version FROM v2_simulation_runs
             WHERE cycle_id=? AND strategy_version_id=? AND signal_id=?",
            params![request.cycle_id, request.strategy_version_id, request.signal_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?, row.get(6)?)),
        ).optional()?;
        if let Some((id, raw_request, raw_outcome, raw_evidence, calendar_version, session_policy_version, instrument_version)) = prior {
            verify_market_versions(&calendar, &sessions, &instruments, &calendar_version, &session_policy_version, &instrument_version, id)?;
            let old_request: SimulationRequest = serde_json::from_str(&raw_request)?;
            if old_request != request {
                return Err(StoreError::Conflict(
                    "same_signal_identity_with_different_request".into()
                ));
            }
            let outcome = serde_json::from_str(&raw_outcome)?;
            let evidence: Option<QuoteEvidence> = serde_json::from_str(&raw_evidence)?;
            if let Some(pinned) = &evidence {
                if pinned.market_data_pack_id != request.quote_ref.market_data_pack_id
                    || pinned.quote.source != request.quote_ref.source || pinned.quote.quote_id != request.quote_ref.quote_id {
                    return Err(StoreError::Integrity("stored_evidence_identity_mismatch".into()));
                }
                verify_instrument_identity(&instruments, pinned)?;
                if load_evidence(&txn, &request.quote_ref)?.as_ref() != Some(pinned) {
                    return Err(StoreError::Integrity("stored_evidence_payload_mismatch".into()));
                }
            }
            return Ok(RunRecord { id, request, evidence, calendar_version, session_policy_version, instrument_version, outcome, duplicate: true });
        }
        if experiment.status != ExperimentStatus::Active {
            return Err(StoreError::Conflict("experiment_completed".into()));
        }
        // V2 is append-only. Out-of-order historical corrections require a
        // separate explicit rebase/import operation; never silently recalculate
        // past positions by appending a request with an earlier decision time.
        let previous_request: Option<String> = txn.query_row(
            "SELECT request_json FROM v2_simulation_runs WHERE cycle_id=? ORDER BY id DESC LIMIT 1",
            [&request.cycle_id], |row| row.get(0),
        ).optional()?;
        if let Some(raw) = previous_request {
            let previous: SimulationRequest = serde_json::from_str(&raw)?;
            let previous_at = chrono::DateTime::parse_from_rfc3339(&previous.decision_at)
                .map_err(|_| StoreError::Integrity("stored_decision_time_invalid".into()))?;
            let next_at = chrono::DateTime::parse_from_rfc3339(&request.decision_at)
                .map_err(|_| StoreError::Rules(RuleError("invalid_decision_time".into())))?;
            if next_at < previous_at {
                return Err(StoreError::Conflict(
                    "out_of_order_decision_requires_separate_replay_branch".into()
                ));
            }
        }
        let state = load_state(&txn, &experiment.spec)?;
        let evidence = load_evidence(&txn, &request.quote_ref)?;
        if let Some(pinned) = &evidence { verify_instrument_identity(&instruments, pinned)?; }
        let outcome = decide(&state, &request, evidence.as_ref(), &calendar, &sessions, &instruments)?;
        txn.execute(
            "INSERT INTO v2_simulation_runs
             (cycle_id,strategy_version_id,signal_id,request_json,outcome_json,evidence_json,calendar_version,session_policy_version,instrument_version)
             VALUES(?,?,?,?,?,?,?,?,?)",
            params![
                request.cycle_id, request.strategy_version_id, request.signal_id,
                serde_json::to_string(&request)?, serde_json::to_string(&outcome)?,
                serde_json::to_string(&evidence)?, calendar.version, sessions.version, instruments.version
            ],
        )?;
        let id = txn.last_insert_rowid();
        if let Some(fill) = &outcome.fill {
            txn.execute(
                "INSERT INTO v2_fills(run_id,fill_json) VALUES(?,?)",
                params![id, serde_json::to_string(fill)?],
            )?;
        }
        txn.commit()?;
        Ok(RunRecord { id, request, evidence, calendar_version: calendar.version,
            session_policy_version: sessions.version, instrument_version: instruments.version,
            outcome, duplicate: false })
    }

}

fn load_market_context(conn: &Connection, pack_id: &str) -> StoreResult<(TradingCalendar, SessionPolicy, InstrumentSnapshot)> {
    let pack = load_market_pack(conn, pack_id)?;
    Ok((pack.calendar, pack.sessions, pack.instruments))
}

/// The reviewed catalogue retains old definitions; no latest/current fallback.
fn reviewed_market_pack(pack_id: &str) -> StoreResult<MarketDataPack> {
    let (manifest, instruments) = match pack_id {
        MARKET_DATA_PACK_ID => (include_str!("../../data/market-pack-1.json"), BUNDLED_INSTRUMENTS_JSON),
        "cn-offline-fixture-pack-2" => (include_str!("../../data/market-pack-2.json"), include_str!("../../data/instruments-offline-v2.json")),
        _ => return Err(StoreError::Rules(RuleError("unsupported_market_data_pack".into()))),
    };
    let pack = MarketDataPack {
        manifest: serde_json::from_str(manifest)?,
        calendar: serde_json::from_str(BUNDLED_CALENDAR_JSON)?,
        sessions: serde_json::from_str(BUNDLED_SESSION_POLICY_JSON)?,
        instruments: serde_json::from_str(instruments)?,
    };
    pack.calendar.validate()?;
    pack.sessions.validate()?;
    pack.instruments.validate()?;
    if pack.manifest.pack_id != pack_id || pack.manifest.coverage_start != pack.instruments.coverage_start
        || pack.manifest.coverage_end != pack.instruments.coverage_end || pack.manifest.sources.is_empty()
        || pack.manifest.content_summary.len() != 3 {
        return Err(StoreError::Integrity("invalid_reviewed_pack_manifest".into()));
    }
    Ok(pack)
}

/// Reviewed strategy research records: definition, source bytes, frozen input
/// and the research output those two must reproduce.
///
/// A strategy definition is data plus the exact source bytes whose digest the
/// artifact pins. Adding a strategy means adding both records and a reviewed
/// definition, never accepting an uploaded one.
fn strategy_source(strategy_version_id: &str) -> StoreResult<&'static str> {
    Ok(match strategy_version_id {
        "research-momentum-v1" => include_str!("../../research/strategies/momentum.py"),
        "research-reversion-v1" => include_str!("../../research/strategies/reversion.py"),
        _ => return Err(StoreError::Rules(RuleError("unsupported_strategy_artifact".into()))),
    })
}

fn reviewed_strategy_artifact(strategy_version_id: &str) -> StoreResult<StrategyArtifact> {
    let raw = match strategy_version_id {
        "research-momentum-v1" => include_str!("../../data/strategy-momentum-v1.json"),
        "research-reversion-v1" => include_str!("../../data/strategy-reversion-v1.json"),
        _ => return Err(StoreError::Rules(RuleError("unsupported_strategy_artifact".into()))),
    };
    let artifact: StrategyArtifact = serde_json::from_str(raw)?;
    artifact.verify_content()?;
    if artifact.strategy_version_id != strategy_version_id {
        return Err(StoreError::Integrity("reviewed_strategy_identity_mismatch".into()));
    }
    let source = crate::domain::normalize_newlines(strategy_source(strategy_version_id)?);
    if artifact.source_digest != crate::domain::sha256_hex(source.as_bytes()) {
        return Err(StoreError::Integrity("reviewed_strategy_source_digest_mismatch".into()));
    }
    Ok(artifact)
}

fn reviewed_research_input(research_input_id: &str) -> StoreResult<ResearchInputManifest> {
    let raw = match research_input_id {
        "offline-research-input-v1" => include_str!("../../data/research-input-offline-v1.json"),
        _ => return Err(StoreError::Rules(RuleError("unsupported_research_input".into()))),
    };
    let manifest: ResearchInputManifest = serde_json::from_str(raw)?;
    manifest.validate()?;
    if manifest.research_input_id != research_input_id {
        return Err(StoreError::Integrity("research_input_identity_mismatch".into()));
    }
    Ok(manifest)
}

fn reviewed_research_output(strategy_version_id: &str) -> StoreResult<ResearchOutput> {
    let raw = match strategy_version_id {
        "research-momentum-v1" => include_str!("../../data/research-output-momentum-v1.json"),
        "research-reversion-v1" => include_str!("../../data/research-output-reversion-v1.json"),
        _ => return Err(StoreError::Rules(RuleError("unsupported_strategy_artifact".into()))),
    };
    let output: ResearchOutput = serde_json::from_str(raw)?;
    output.validate()?;
    if output.strategy_version_id != strategy_version_id {
        return Err(StoreError::Integrity("reviewed_research_output_identity_mismatch".into()));
    }
    Ok(output)
}

fn register_research_input(conn: &Connection, manifest: &ResearchInputManifest) -> StoreResult<()> {
    if *manifest != reviewed_research_input(&manifest.research_input_id)? {
        return Err(StoreError::Conflict("research_input_identity_content_conflict".into()));
    }
    let existing: Option<String> = conn.query_row(
        "SELECT manifest_json FROM v2_research_inputs WHERE research_input_id=?",
        [&manifest.research_input_id], |r| r.get(0)).optional()?;
    if let Some(raw) = existing {
        if serde_json::from_str::<ResearchInputManifest>(&raw)? != *manifest {
            return Err(StoreError::Conflict("research_input_identity_content_conflict".into()));
        }
    } else {
        conn.execute("INSERT INTO v2_research_inputs VALUES(?,?)",
            params![manifest.research_input_id, serde_json::to_string(manifest)?])?;
    }
    Ok(())
}

fn load_research_input(conn: &Connection, research_input_id: &str) -> StoreResult<ResearchInputManifest> {
    let expected = reviewed_research_input(research_input_id)?;
    let raw: Option<String> = conn.query_row(
        "SELECT manifest_json FROM v2_research_inputs WHERE research_input_id=?",
        [research_input_id], |r| r.get(0)).optional()?;
    let saved: ResearchInputManifest = serde_json::from_str(
        &raw.ok_or_else(|| StoreError::Integrity("research_input_missing".into()))?)?;
    if saved != expected {
        return Err(StoreError::Integrity("pinned_research_input_mismatch".into()));
    }
    Ok(saved)
}

fn register_pack(conn: &Connection, pack: &MarketDataPack) -> StoreResult<()> {
    if *pack != reviewed_market_pack(&pack.manifest.pack_id)? {
        return Err(StoreError::Conflict("pack_identity_content_conflict".into()));
    }
    let existing: Option<String> = conn.query_row("SELECT pack_json FROM v2_market_data_packs WHERE pack_id=?", [&pack.manifest.pack_id], |r| r.get(0)).optional()?;
    if let Some(raw) = existing {
        if serde_json::from_str::<MarketDataPack>(&raw)? != *pack {
            return Err(StoreError::Conflict("pack_identity_content_conflict".into()));
        }
    } else {
        conn.execute("INSERT INTO v2_market_data_packs VALUES(?,?)", params![pack.manifest.pack_id, serde_json::to_string(pack)?])?;
    }
    Ok(())
}

fn load_market_pack(conn: &Connection, pack_id: &str) -> StoreResult<MarketDataPack> {
    let expected = reviewed_market_pack(pack_id)?;
    let raw: Option<String> = conn.query_row("SELECT pack_json FROM v2_market_data_packs WHERE pack_id=?", [pack_id], |r| r.get(0)).optional()?;
    let saved: MarketDataPack = serde_json::from_str(&raw.ok_or_else(|| StoreError::Integrity("market_data_pack_missing".into()))?)?;
    if saved != expected { return Err(StoreError::Integrity("pinned_market_snapshot_mismatch".into())); }
    Ok(saved)
}

fn verify_market_versions(
    calendar: &TradingCalendar, sessions: &SessionPolicy, instruments: &InstrumentSnapshot,
    calendar_version: &str, session_version: &str, instrument_version: &str, id: i64,
) -> StoreResult<()> {
    if calendar_version != calendar.version || session_version != sessions.version || instrument_version != instruments.version {
        return Err(StoreError::Integrity(format!("run_market_version_mismatch_at_{id}")));
    }
    Ok(())
}

fn load_evidence(conn: &Connection, identity: &QuoteRef) -> StoreResult<Option<QuoteEvidence>> {
    let raw: Option<String> = conn.query_row(
        "SELECT evidence_json FROM v2_quote_evidence WHERE pack_id=? AND source=? AND quote_id=?",
        params![identity.market_data_pack_id, identity.source, identity.quote_id], |row| row.get(0),
    ).optional()?;
    let evidence: Option<QuoteEvidence> = raw.map(|value| serde_json::from_str(&value)).transpose()?;
    if evidence.as_ref().is_some_and(|e| e.market_data_pack_id != identity.market_data_pack_id
        || e.quote.source != identity.source || e.quote.quote_id != identity.quote_id) {
        return Err(StoreError::Integrity("stored_evidence_identity_mismatch".into()));
    }
    Ok(evidence)
}

// Storage referential integrity applies even to closed-date/rejected runs.
fn verify_instrument_identity(snapshot: &InstrumentSnapshot, evidence: &QuoteEvidence) -> StoreResult<()> {
    if evidence.instrument.as_ref() != snapshot.identity(&evidence.quote.symbol) {
        return Err(StoreError::Integrity("stored_instrument_identity_mismatch".into()));
    }
    Ok(())
}

fn load_state(conn: &Connection, experiment: &ExperimentSpec) -> StoreResult<PortfolioState> {
    let mut state = PortfolioState::new(experiment.initial_cash_cents);
    let mut stmt = conn.prepare("SELECT f.fill_json FROM v2_fills f JOIN v2_simulation_runs r ON r.id=f.run_id WHERE r.cycle_id=? ORDER BY f.id")?;
    let rows = stmt.query_map([&experiment.cycle_id], |row| row.get::<_, String>(0))?;
    for row in rows {
        let fill: Fill = serde_json::from_str(&row?)?;
        state.apply(&fill)?;
    }
    Ok(state)
}


// Read models and replay cannot submit or commit trades.
mod query;
#[cfg(test)]
mod tests;

fn validate_experiment_spec(spec: &ExperimentSpec) -> StoreResult<()> {
    fn key(s: &str) -> bool { !s.is_empty() && s.len() <= 128 && s.bytes().all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c)) }
    if spec.schema_version != 1 || !key(&spec.cycle_id) || !key(&spec.strategy.strategy_version_id)
        || !key(&spec.strategy.research_input_id) || spec.initial_cash_cents <= 0 {
        return Err(StoreError::Rules(RuleError("invalid_experiment_spec".into())));
    }
    if spec.execution_policy_version != POLICY_VERSION {
        return Err(StoreError::Rules(RuleError("unsupported_execution_policy".into())));
    }
    reviewed_market_pack(&spec.market_data_pack_id)?;
    // Identity alone never authorizes an experiment: the artifact must equal the
    // reviewed definition for that id, and its digests must be recomputable from
    // repository bytes and the frozen research input.
    spec.strategy.verify_content()?;
    // Shadow candidates (ADR-014) reuse a reviewed source but carry their own
    // parameters. They are research-only: the source digest must match one of
    // the reviewed strategies, and the frozen input must still be reviewed.
    if spec.strategy.strategy_version_id.starts_with("shadow-") {
        let known_source = ["research-momentum-v1", "research-reversion-v1"]
            .iter()
            .filter_map(|id| reviewed_strategy_artifact(id).ok())
            .any(|artifact| artifact.source_digest == spec.strategy.source_digest);
        if !known_source {
            return Err(StoreError::Rules(RuleError("unsupported_strategy_artifact".into())));
        }
        let input = reviewed_research_input(&spec.strategy.research_input_id)?;
        if input.content_digest != spec.strategy.research_input_digest {
            return Err(StoreError::Integrity("research_input_digest_mismatch".into()));
        }
        return Ok(());
    }
    let reviewed = reviewed_strategy_artifact(&spec.strategy.strategy_version_id)?;
    if reviewed != spec.strategy {
        return Err(StoreError::Conflict("strategy_artifact_content_mismatch".into()));
    }
    let input = reviewed_research_input(&spec.strategy.research_input_id)?;
    if input.content_digest != spec.strategy.research_input_digest {
        return Err(StoreError::Integrity("research_input_digest_mismatch".into()));
    }
    // The artifact fixes the input; an experiment that selects another pack can
    // still be created, but its decisions can never match the frozen signals, so
    // reproduction fails instead of silently claiming the same research result.
    let output = reviewed_research_output(&spec.strategy.strategy_version_id)?;
    if output.research_input_id != spec.strategy.research_input_id
        || output.algorithm_version != spec.strategy.algorithm_version
        || output.algorithm_digest != spec.strategy.source_digest
        || output.parameters_digest != spec.strategy.parameters_digest
        || output.research_input_digest != spec.strategy.research_input_digest {
        return Err(StoreError::Integrity("reviewed_research_output_mismatch".into()));
    }
    Ok(())
}

fn load_experiment(conn: &Connection, cycle_id: &str) -> StoreResult<Experiment> {
    let row: Option<(String, String, String)> = conn.query_row("SELECT spec_json,status,pack_manifest_json FROM v2_experiments WHERE cycle_id=?", [cycle_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional()?;
    let (raw, status, raw_manifest) = row
        .ok_or_else(|| StoreError::NotFound("experiment_not_found".into()))?;
    let spec: ExperimentSpec = serde_json::from_str(&raw)?;
    validate_experiment_spec(&spec)?;
    let pack = load_market_pack(conn, &spec.market_data_pack_id)?;
    if serde_json::from_str::<MarketDataPackManifest>(&raw_manifest)? != pack.manifest {
        return Err(StoreError::Integrity("experiment_pack_reference_mismatch".into()));
    }
    if spec.cycle_id != cycle_id { return Err(StoreError::Integrity("experiment_identity_mismatch".into())); }
    let raw_artifact: String = conn.query_row("SELECT artifact_json FROM v2_strategy_artifacts WHERE strategy_version_id=?", [&spec.strategy.strategy_version_id], |r| r.get(0))?;
    if serde_json::from_str::<StrategyArtifact>(&raw_artifact)? != spec.strategy {
        return Err(StoreError::Integrity("experiment_artifact_mismatch".into()));
    }
    // The frozen research input is an independently referenced fact; it must
    // still resolve and still equal the reviewed bytes on every read.
    load_research_input(conn, &spec.strategy.research_input_id)?;
    let status = match status.as_str() {
        "active" => ExperimentStatus::Active,
        "completed" => ExperimentStatus::Completed,
        _ => return Err(StoreError::Integrity("invalid_experiment_status".into())),
    };
    Ok(Experiment { spec, status })
}
// ---------------------------------------------------------------------------
// Paper account: one user-funded account, two frozen strategy slots.
// Store is the only writer; the split itself stays a pure domain decision.
// ---------------------------------------------------------------------------

impl Store {
    /// Register an account once. Re-posting the identical spec is idempotent;
    /// a different spec under the same id is a conflict, never a silent update.
    pub fn create_paper_account(
        &mut self,
        spec: crate::domain::PaperAccountSpec,
    ) -> StoreResult<PaperAccountView> {
        let split = spec.validate()?;
        let raw = serde_json::to_string(&spec)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        for slot in &spec.slots {
            if slot.execution_policy_version != POLICY_VERSION {
                return Err(StoreError::Rules(RuleError("unsupported_execution_policy".into())));
            }
            load_market_pack(&txn, &slot.market_data_pack_id)?;
        }
        if let Some(existing) = txn
            .query_row(
                "SELECT spec_json FROM v2_paper_accounts WHERE account_id=?",
                [&spec.account_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?
        {
            if existing != raw {
                return Err(StoreError::Conflict("paper_account_conflict".into()));
            }
            return Self::paper_account_view_with_conn(&txn, &spec, &split);
        }
        txn.execute(
            "INSERT INTO v2_paper_accounts(account_id,spec_json,created_at,weights_json) VALUES(?,?,?,?)",
            params![
                spec.account_id,
                raw,
                spec.created_at,
                serde_json::to_string(&[
                    spec.slots[0].weight_bps,
                    spec.slots[1].weight_bps
                ])?
            ],
        )?;
        txn.commit()?;
        Ok(Self::paper_account_view_with_conn(&self.conn, &spec, &split)?)
    }
}

// ---------------------------------------------------------------------------
// Paper account order path (ADR 005): slot-owned orders and fills.
// Reuses the same pure decision policy as experiments; the slot owns budget,
// cash and positions, and the reserve never trades.
// ---------------------------------------------------------------------------

impl Store {
    /// Current slot weights after any rebalances; falls back to the spec.
    fn account_weights(
        conn: &Connection,
        spec: &crate::domain::PaperAccountSpec,
    ) -> StoreResult<[i64; 2]> {
        let raw: Option<String> = conn
            .query_row(
                "SELECT weights_json FROM v2_paper_accounts WHERE account_id=?",
                [&spec.account_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(raw) = raw {
            if let Ok(weights) = serde_json::from_str::<Vec<i64>>(&raw) {
                if weights.len() == 2 && weights.iter().all(|weight| *weight > 0) {
                    return Ok([weights[0], weights[1]]);
                }
            }
        }
        Ok([spec.slots[0].weight_bps, spec.slots[1].weight_bps])
    }

    fn paper_slot_state(
        conn: &Connection,
        spec: &crate::domain::PaperAccountSpec,
        split: &crate::domain::PaperCapitalSplit,
        slot_index: usize,
    ) -> StoreResult<PortfolioState> {
        let slot = if slot_index == 0 { "A" } else { "B" };
        let mut state = PortfolioState::new(split.slot_budgets[slot_index]);
        let mut statement = conn.prepare(
            "SELECT f.fill_json FROM v2_paper_fills f
             JOIN v2_paper_orders o ON o.id=f.order_id
             WHERE o.account_id=? AND o.slot=? ORDER BY o.id",
        )?;
        let rows = statement.query_map(params![spec.account_id, slot], |row| {
            row.get::<_, String>(0)
        })?;
        for row in rows {
            let fill: Fill = serde_json::from_str(&row?)?;
            state.apply(&fill).map_err(StoreError::Rules)?;
        }
        // Rebalances move cash only, and always after fills, so history is
        // never reinterpreted by a later capital decision.
        let mut transfers = conn.prepare(
            "SELECT from_slot,to_slot,amount_cents FROM v2_paper_transfers
             WHERE account_id=? ORDER BY id",
        )?;
        let rows = transfers.query_map([&spec.account_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })?;
        for row in rows {
            let (from, to, amount) = row?;
            if from == slot {
                state.cash_cents = state
                    .cash_cents
                    .checked_sub(amount)
                    .ok_or_else(|| StoreError::Rules(RuleError("paper_slot_cash_underflow".into())))?;
            }
            if to == slot {
                state.cash_cents = state
                    .cash_cents
                    .checked_add(amount)
                    .ok_or_else(|| StoreError::Rules(RuleError("paper_cash_overflow".into())))?;
            }
        }
        Ok(state)
    }

    fn paper_account_view_with_conn(
        conn: &Connection,
        spec: &crate::domain::PaperAccountSpec,
        split: &crate::domain::PaperCapitalSplit,
    ) -> StoreResult<PaperAccountView> {
        let weights = Self::account_weights(conn, spec)?;
        let display = spec.split_with_weights(weights).map_err(StoreError::Rules)?;
        let mut slots = Vec::new();
        let mut cash_cents = split.reserve_cents;
        for (index, slot) in spec.slots.iter().enumerate() {
            let state = Self::paper_slot_state(conn, spec, split, index)?;
            let orders_total: i64 = conn.query_row(
                "SELECT COUNT(*) FROM v2_paper_orders WHERE account_id=? AND slot=?",
                params![spec.account_id, slot.slot.as_str()],
                |row| row.get(0),
            )?;
            cash_cents += state.cash_cents;
            slots.push(PaperSlotView {
                slot: slot.slot.as_str().into(),
                strategy_version_id: slot.strategy.strategy_version_id.clone(),
                research_input_id: slot.strategy.research_input_id.clone(),
                market_data_pack_id: slot.market_data_pack_id.clone(),
                execution_policy_version: slot.execution_policy_version.clone(),
                weight_bps: weights[index],
                budget_cents: display.slot_budgets[index],
                cash_cents: state.cash_cents,
                positions: state
                    .lots
                    .iter()
                    .fold(Vec::<PositionView>::new(), |mut acc, lot| {
                        match acc.iter_mut().find(|position| position.symbol == lot.symbol) {
                            Some(position) => position.quantity += lot.quantity,
                            None => acc.push(PositionView {
                                symbol: lot.symbol.clone(),
                                quantity: lot.quantity,
                                sellable_quantity: None,
                            }),
                        }
                        acc
                    }),
                orders_total,
            });
        }
        Ok(PaperAccountView {
            account_id: spec.account_id.clone(),
            initial_cash_cents: spec.initial_cash_cents,
            created_at: spec.created_at.clone(),
            cash_cents,
            reserve_cents: split.reserve_cents,
            slots,
            valuation: PaperValuation {
                status: "unavailable".into(),
                reason: "no_reviewed_price_set".into(),
            },
            limits: spec.limits.clone(),
        })
    }

    /// Submit one simulated order to a slot. Append-only per slot, idempotent on
    /// the signal identity, and fail-closed on evidence and budget.
    pub fn submit_paper_order(
        &mut self,
        request: crate::domain::PaperOrderRequest,
    ) -> StoreResult<PaperOrderRecord> {
        request.validate()?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let raw_spec: Option<String> = txn
            .query_row(
                "SELECT spec_json FROM v2_paper_accounts WHERE account_id=?",
                [&request.account_id],
                |row| row.get(0),
            )
            .optional()?;
        let raw_spec =
            raw_spec.ok_or_else(|| StoreError::NotFound("paper_account_not_found".into()))?;
        let spec: crate::domain::PaperAccountSpec = serde_json::from_str(&raw_spec)?;
        let split = spec.validate()?;
        let slot_index = spec
            .slots
            .iter()
            .position(|slot| slot.slot == request.slot)
            .ok_or_else(|| StoreError::Rules(RuleError("paper_slot_not_found".into())))?;
        let slot_spec = spec.slots[slot_index].clone();
        if request.quote_ref.market_data_pack_id != slot_spec.market_data_pack_id {
            return Err(StoreError::Conflict("paper_quote_data_pack_mismatch".into()));
        }
        let (calendar, sessions, instruments) =
            load_market_context(&txn, &slot_spec.market_data_pack_id)?;

        let prior: Option<(i64, String, String)> = txn
            .query_row(
                "SELECT id,request_json,outcome_json FROM v2_paper_orders
                 WHERE account_id=? AND slot=? AND strategy_version_id=? AND signal_id=?",
                params![
                    request.account_id,
                    slot_spec.slot.as_str(),
                    slot_spec.strategy.strategy_version_id,
                    request.signal_id
                ],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        if let Some((id, raw_request, raw_outcome)) = prior {
            let stored: crate::domain::PaperOrderRequest = serde_json::from_str(&raw_request)?;
            if stored != request {
                return Err(StoreError::Conflict(
                    "same_signal_identity_with_different_paper_request".into(),
                ));
            }
            let outcome: Outcome = serde_json::from_str(&raw_outcome)?;
            return Ok(PaperOrderRecord {
                id,
                account_id: request.account_id.clone(),
                slot: slot_spec.slot.as_str().into(),
                strategy_version_id: slot_spec.strategy.strategy_version_id,
                request,
                outcome,
                duplicate: true,
            });
        }

        // Append-only: a slot never accepts a decision earlier than its last one.
        let previous: Option<String> = txn
            .query_row(
                "SELECT request_json FROM v2_paper_orders
                 WHERE account_id=? AND slot=? ORDER BY id DESC LIMIT 1",
                params![request.account_id, slot_spec.slot.as_str()],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(raw) = previous {
            let previous: crate::domain::PaperOrderRequest = serde_json::from_str(&raw)?;
            let previous_at = chrono::DateTime::parse_from_rfc3339(&previous.decision_at)
                .map_err(|_| StoreError::Integrity("stored_decision_time_invalid".into()))?;
            let next_at = chrono::DateTime::parse_from_rfc3339(&request.decision_at)
                .map_err(|_| StoreError::Rules(RuleError("invalid_decision_time".into())))?;
            if next_at < previous_at {
                return Err(StoreError::Conflict(
                    "out_of_order_paper_decision".into(),
                ));
            }
        }

        let state = Self::paper_slot_state(&txn, &spec, &split, slot_index)?;
        let internal = SimulationRequest {
            schema_version: 4,
            cycle_id: spec.account_id.clone(),
            strategy_version_id: slot_spec.strategy.strategy_version_id.clone(),
            signal_id: request.signal_id.clone(),
            decision_at: request.decision_at.clone(),
            signal: request.signal.clone(),
            quote_ref: request.quote_ref.clone(),
        };
        let evidence = load_evidence(&txn, &request.quote_ref)?;
        let mut outcome = decide(
            &state,
            &internal,
            evidence.as_ref(),
            &calendar,
            &sessions,
            &instruments,
        )
        .map_err(StoreError::Rules)?;
        // Risk boundaries become an explicit rejection, never a partial fill.
        if let Some(fill) = outcome.fill.clone() {
            let weights = Self::account_weights(&txn, &spec)?;
            let risk_split = spec.split_with_weights(weights).map_err(StoreError::Rules)?;
            let budget = risk_split.slot_budgets[slot_index];
            if let Some(reason) = risk_violation(&spec.limits, budget, &state, &fill) {
                outcome = Outcome {
                    status: crate::domain::DecisionStatus::Rejected,
                    reason: reason.into(),
                    fill: None,
                    cash_after_cents: state.cash_cents,
                };
            }
        }

        txn.execute(
            "INSERT INTO v2_paper_orders(account_id,slot,strategy_version_id,signal_id,
                request_json,evidence_json,outcome_json,calendar_version,session_policy_version,instrument_version)
             VALUES(?,?,?,?,?,?,?,?,?,?)",
            params![
                request.account_id,
                slot_spec.slot.as_str(),
                slot_spec.strategy.strategy_version_id,
                request.signal_id,
                serde_json::to_string(&request)?,
                serde_json::to_string(&evidence)?,
                serde_json::to_string(&outcome)?,
                calendar.version,
                sessions.version,
                instruments.version,
            ],
        )?;
        let id = txn.last_insert_rowid();
        if let Some(fill) = &outcome.fill {
            txn.execute(
                "INSERT INTO v2_paper_fills(order_id,fill_json) VALUES(?,?)",
                params![id, serde_json::to_string(fill)?],
            )?;
        }
        txn.commit()?;
        Ok(PaperOrderRecord {
            id,
            account_id: request.account_id.clone(),
            slot: slot_spec.slot.as_str().into(),
            strategy_version_id: slot_spec.strategy.strategy_version_id,
            request,
            outcome,
            duplicate: false,
        })
    }
}
// ---------------------------------------------------------------------------
// Paper account catalogue (ADR 005): read-only list for the account console.
// ---------------------------------------------------------------------------

impl Store {
    pub fn paper_accounts(&self, limit: i64) -> StoreResult<Vec<PaperAccountSummary>> {
        let mut statement = self.conn.prepare(
            "SELECT account_id,spec_json,created_at FROM v2_paper_accounts ORDER BY account_id LIMIT ?",
        )?;
        let rows = statement.query_map([limit.clamp(1, 200)], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })?;
        let mut accounts = Vec::new();
        for row in rows {
            let (account_id, spec_json, created_at) = row?;
            let spec: crate::domain::PaperAccountSpec = serde_json::from_str(&spec_json)?;
            accounts.push(PaperAccountSummary {
                account_id,
                initial_cash_cents: spec.initial_cash_cents,
                reserve_bps: spec.reserve_bps,
                created_at,
                slots: spec
                    .slots
                    .iter()
                    .map(|slot| PaperAccountSummarySlot {
                        slot: slot.slot.as_str().into(),
                        strategy_version_id: slot.strategy.strategy_version_id.clone(),
                        weight_bps: slot.weight_bps,
                    })
                    .collect(),
            });
        }
        Ok(accounts)
    }
}
// ---------------------------------------------------------------------------
// Paper session (ADR 005 phase 2): deterministic clock over a frozen signal list.
// The session stores its own spec and status; every step submits exactly one
// slot order and advances an explicit simulated instant.
// ---------------------------------------------------------------------------

impl Store {
    pub fn create_paper_session(
        &mut self,
        spec: crate::domain::PaperSessionSpec,
    ) -> StoreResult<PaperSessionView> {
        spec.validate()?;
        let raw = serde_json::to_string(&spec)?;
        let account_exists: Option<String> = self
            .conn
            .query_row(
                "SELECT account_id FROM v2_paper_accounts WHERE account_id=?",
                [&spec.account_id],
                |row| row.get(0),
            )
            .optional()?;
        account_exists
            .ok_or_else(|| StoreError::NotFound("paper_account_not_found".into()))?;
        if let Some(existing) = self
            .conn
            .query_row(
                "SELECT spec_json FROM v2_paper_sessions WHERE session_id=?",
                [&spec.session_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?
        {
            if existing != raw {
                return Err(StoreError::Conflict("paper_session_conflict".into()));
            }
            return self.paper_session(&spec.session_id);
        }
        self.conn.execute(
            "INSERT INTO v2_paper_sessions(session_id,account_id,spec_json,status,clock_at,cursor,last_order_id)
             VALUES(?,?,?,?,?,?,NULL)",
            params![spec.session_id, spec.account_id, raw, "created", spec.start_at, 0i64],
        )?;
        self.paper_session(&spec.session_id)
    }

    pub fn paper_session(&self, session_id: &str) -> StoreResult<PaperSessionView> {
        let (account_id, spec_json, status, clock_at, cursor, last_order_id) = self
            .conn
            .query_row(
                "SELECT account_id,spec_json,status,clock_at,cursor,last_order_id
                 FROM v2_paper_sessions WHERE session_id=?",
                [session_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, i64>(4)?,
                        row.get::<_, Option<i64>>(5)?,
                    ))
                },
            )
            .optional()?
            .ok_or_else(|| StoreError::NotFound("paper_session_not_found".into()))?;
        let spec: crate::domain::PaperSessionSpec = serde_json::from_str(&spec_json)?;
        Ok(PaperSessionView {
            session_id: session_id.into(),
            account_id,
            status,
            clock_at,
            cursor: cursor.max(0) as usize,
            total: spec.signals.len(),
            last_order_id,
        })
    }

    fn set_session_status(&self, session_id: &str, status: &str) -> StoreResult<()> {
        self.conn.execute(
            "UPDATE v2_paper_sessions SET status=? WHERE session_id=?",
            params![status, session_id],
        )?;
        Ok(())
    }

    /// created → running, running → paused, paused → running, running/paused → stopped.
    pub fn paper_session_action(
        &mut self,
        session_id: &str,
        action: &str,
    ) -> StoreResult<PaperSessionView> {
        let current = self.paper_session(session_id)?;
        let next = match (action, current.status.as_str()) {
            ("start", "created") => "running",
            ("pause", "running") => "paused",
            ("resume", "paused") => "running",
            ("stop", "running" | "paused") => "stopped",
            _ => return Err(StoreError::Conflict("invalid_paper_session_transition".into())),
        };
        self.set_session_status(session_id, next)?;
        self.paper_session(session_id)
    }

    /// Submit the next frozen signal. One step = one simulated instant and at
    /// most one slot order; the session clock only moves to that decision time.
    pub fn step_paper_session(&mut self, session_id: &str) -> StoreResult<PaperSessionStep> {
        let current = self.paper_session(session_id)?;
        if current.status != "running" {
            return Err(StoreError::Conflict("paper_session_not_running".into()));
        }
        let spec_json: String = self.conn.query_row(
            "SELECT spec_json FROM v2_paper_sessions WHERE session_id=?",
            [session_id],
            |row| row.get(0),
        )?;
        let spec: crate::domain::PaperSessionSpec = serde_json::from_str(&spec_json)?;
        if current.cursor >= spec.signals.len() {
            self.set_session_status(session_id, "completed")?;
            return Ok(PaperSessionStep { session: self.paper_session(session_id)?, order: None });
        }
        let signal = &spec.signals[current.cursor];
        let request = crate::domain::PaperOrderRequest {
            schema_version: crate::domain::PAPER_ORDER_SCHEMA_VERSION,
            account_id: spec.account_id.clone(),
            slot: signal.slot,
            signal_id: format!("{}:{}", session_id, signal.signal_id),
            decision_at: signal.decision_at.clone(),
            signal: signal.signal.clone(),
            quote_ref: signal.quote_ref.clone(),
        };
        let order = self.submit_paper_order(request)?;
        let next_cursor = current.cursor + 1;
        let status = if next_cursor >= spec.signals.len() { "completed" } else { "running" };
        self.conn.execute(
            "UPDATE v2_paper_sessions SET cursor=?, clock_at=?, last_order_id=?, status=? WHERE session_id=?",
            params![next_cursor as i64, signal.decision_at, order.id, status, session_id],
        )?;
        Ok(PaperSessionStep { session: self.paper_session(session_id)?, order: Some(order) })
    }
}
// ---------------------------------------------------------------------------
// Paper session batch run: advance the deterministic clock until the session
// stops being "running" (paused, stopped or completed). One order per step.
// ---------------------------------------------------------------------------

impl Store {
    /// Advance the clock only through signals whose China day is on/before `day`.
    /// Future signals stay untouched: no look-ahead is ever executed.
    pub fn advance_paper_session_to(
        &mut self,
        session_id: &str,
        day: &str,
    ) -> StoreResult<PaperSessionRun> {
        let target = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .map_err(|_| StoreError::Rules(RuleError("invalid_advance_day".into())))?;
        if target.to_string() != day {
            return Err(StoreError::Rules(RuleError("invalid_advance_day".into())));
        }
        let mut steps = 0usize;
        let mut filled = 0usize;
        let mut rejected = 0usize;
        loop {
            let view = self.paper_session(session_id)?;
            if view.status != "running" || view.cursor >= view.total {
                break;
            }
            let spec_json: String = self.conn.query_row(
                "SELECT spec_json FROM v2_paper_sessions WHERE session_id=?",
                [session_id],
                |row| row.get(0),
            )?;
            let spec: crate::domain::PaperSessionSpec = serde_json::from_str(&spec_json)?;
            let next = &spec.signals[view.cursor];
            let at = chrono::DateTime::parse_from_rfc3339(&next.decision_at)
                .map_err(|_| StoreError::Integrity("stored_decision_time_invalid".into()))?;
            let china = at.with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).expect("offset"));
            if china.date_naive() > target {
                break;
            }
            let step = self.step_paper_session(session_id)?;
            match step.order {
                Some(order) => {
                    steps += 1;
                    match order.outcome.status {
                        crate::domain::DecisionStatus::Filled => filled += 1,
                        crate::domain::DecisionStatus::Rejected => rejected += 1,
                    }
                }
                None => break,
            }
            if steps > view.total {
                break;
            }
        }
        Ok(PaperSessionRun {
            session: self.paper_session(session_id)?,
            steps,
            filled,
            rejected,
        })
    }

    pub fn run_paper_session(&mut self, session_id: &str) -> StoreResult<PaperSessionRun> {
        let mut steps = 0usize;
        let mut filled = 0usize;
        let mut rejected = 0usize;
        loop {
            let view = self.paper_session(session_id)?;
            if view.status != "running" {
                break;
            }
            let step = self.step_paper_session(session_id)?;
            match step.order {
                Some(order) => {
                    steps += 1;
                    match order.outcome.status {
                        crate::domain::DecisionStatus::Filled => filled += 1,
                        crate::domain::DecisionStatus::Rejected => rejected += 1,
                    }
                }
                None => break,
            }
            if steps >= view.total.saturating_add(1) {
                break;
            }
        }
        Ok(PaperSessionRun {
            session: self.paper_session(session_id)?,
            steps,
            filled,
            rejected,
        })
    }
}

impl Store {
    /// The reviewed strategies a new account may reference. Never user-supplied.
    pub fn strategy_catalogue(&self) -> StoreResult<StrategyCatalogue> {
        let mut strategies = Vec::new();
        for id in ["research-momentum-v1", "research-reversion-v1"] {
            let artifact = reviewed_strategy_artifact(id)?;
            strategies.push(StrategySummary {
                strategy_version_id: artifact.strategy_version_id,
                research_input_id: artifact.research_input_id,
                research_input_digest: artifact.research_input_digest,
            });
        }
        Ok(StrategyCatalogue {
            strategies,
            market_data_pack_id: MARKET_DATA_PACK_ID.into(),
            execution_policy_version: POLICY_VERSION.into(),
            reserve_bps: 1_000,
        })
    }

    /// Resolve a client request (strategy ids) into the internal spec.
    pub fn create_paper_account_request(
        &mut self,
        request: crate::domain::PaperAccountRequest,
    ) -> StoreResult<PaperAccountView> {
        let mut slots = Vec::new();
        for slot in &request.slots {
            slots.push(crate::domain::PaperSlotSpec {
                slot: slot.slot,
                strategy: reviewed_strategy_artifact(&slot.strategy_version_id)?,
                market_data_pack_id: slot.market_data_pack_id.clone(),
                execution_policy_version: slot.execution_policy_version.clone(),
                weight_bps: slot.weight_bps,
            });
        }
        self.create_paper_account(crate::domain::PaperAccountSpec {
            schema_version: crate::domain::PAPER_ACCOUNT_SCHEMA_VERSION,
            account_id: request.account_id,
            initial_cash_cents: request.initial_cash_cents,
            reserve_bps: request.reserve_bps,
            created_at: request.created_at,
            slots,
            limits: request.limits,
        })
    }
}
// ---------------------------------------------------------------------------
// Session import: turn an existing experiment's stored decisions into a frozen
// paper-session signal list. Read-only with respect to the experiment.
// ---------------------------------------------------------------------------

impl Store {
    pub fn create_paper_session_from_experiment(
        &mut self,
        session_id: &str,
        account_id: &str,
        cycle_id: &str,
        slot: crate::domain::PaperSlotId,
        start_at: Option<String>,
    ) -> StoreResult<PaperSessionView> {
        let raw_spec: Option<String> = self
            .conn
            .query_row(
                "SELECT spec_json FROM v2_paper_accounts WHERE account_id=?",
                [account_id],
                |row| row.get(0),
            )
            .optional()?;
        let raw_spec =
            raw_spec.ok_or_else(|| StoreError::NotFound("paper_account_not_found".into()))?;
        let account: crate::domain::PaperAccountSpec = serde_json::from_str(&raw_spec)?;
        account.validate()?;
        let slot_spec = account
            .slots
            .iter()
            .find(|candidate| candidate.slot == slot)
            .ok_or_else(|| StoreError::Rules(RuleError("paper_slot_not_found".into())))?;

        let experiment = load_experiment(&self.conn, cycle_id)?;
        if experiment.spec.strategy.strategy_version_id != slot_spec.strategy.strategy_version_id {
            return Err(StoreError::Rules(RuleError("paper_slot_strategy_mismatch".into())));
        }
        if experiment.spec.market_data_pack_id != slot_spec.market_data_pack_id {
            return Err(StoreError::Rules(RuleError("paper_session_pack_mismatch".into())));
        }
        if experiment.spec.execution_policy_version != slot_spec.execution_policy_version {
            return Err(StoreError::Rules(RuleError("paper_session_policy_mismatch".into())));
        }

        let mut signals = Vec::new();
        {
            let mut statement = self.conn.prepare(
                "SELECT request_json FROM v2_simulation_runs WHERE cycle_id=? ORDER BY id",
            )?;
            let rows = statement.query_map([cycle_id], |row| row.get::<_, String>(0))?;
            for row in rows {
                let request: SimulationRequest = serde_json::from_str(&row?)?;
                signals.push(crate::domain::PaperSessionSignal {
                    slot,
                    signal_id: request.signal_id,
                    decision_at: request.decision_at,
                    signal: request.signal,
                    quote_ref: request.quote_ref,
                });
            }
        }
        if signals.is_empty() {
            return Err(StoreError::Rules(RuleError("paper_session_requires_signals".into())));
        }
        let start_at = match start_at {
            Some(value) => value,
            None => {
                let first = chrono::DateTime::parse_from_rfc3339(&signals[0].decision_at)
                    .map_err(|_| StoreError::Integrity("stored_decision_time_invalid".into()))?;
                let china = first.with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).expect("offset"));
                format!("{}T00:00:00+08:00", china.date_naive())
            }
        };
        self.create_paper_session(crate::domain::PaperSessionSpec {
            schema_version: crate::domain::PAPER_SESSION_SCHEMA_VERSION,
            session_id: session_id.into(),
            account_id: account_id.into(),
            start_at,
            signals,
        })
    }
}

/// Cost-basis risk check. Prices come from the fill itself, so no valuation is
/// required and the decision stays deterministic.
fn risk_violation(
    limits: &crate::domain::PaperRiskLimits,
    budget_cents: i64,
    state: &PortfolioState,
    fill: &Fill,
) -> Option<&'static str> {
    if fill.side != crate::domain::Side::Buy {
        return None;
    }
    let gross = (fill.quantity as i128) * (fill.price_cents as i128);
    let budget = budget_cents as i128;
    if gross * 10_000 > budget * limits.max_order_notional_bps as i128 {
        return Some("risk_order_notional_exceeded");
    }
    let held = state.quantity(&fill.symbol) as i128;
    let basis = (held + fill.quantity as i128) * fill.price_cents as i128;
    if basis * 10_000 > budget * limits.max_symbol_weight_bps as i128 {
        return Some("risk_symbol_concentration_exceeded");
    }
    let cash_after = state.cash_cents as i128 - gross - fill.fees_cents as i128;
    if cash_after * 10_000 < budget * limits.min_slot_cash_bps as i128 {
        return Some("risk_cash_floor_exceeded");
    }
    None
}
// ---------------------------------------------------------------------------
// Capital rebalance (ADR 009): move slot cash toward the target weights and
// record the decision. Fills stay untouched; transfers apply after them, so a
// later capital decision can never rewrite trade history.
// ---------------------------------------------------------------------------

impl Store {
    pub fn apply_rebalance(
        &mut self,
        account_id: &str,
        target: [i64; 2],
        proposal_digest: &str,
        applied_at: &str,
        note: &str,
    ) -> StoreResult<RebalanceRecord> {
        if proposal_digest.len() != 64
            || !proposal_digest.chars().all(|value| value.is_ascii_hexdigit())
        {
            return Err(StoreError::Rules(RuleError("invalid_proposal_digest".into())));
        }
        chrono::DateTime::parse_from_rfc3339(applied_at)
            .map_err(|_| StoreError::Rules(RuleError("invalid_rebalance_time".into())))?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let raw: Option<String> = txn
            .query_row(
                "SELECT spec_json FROM v2_paper_accounts WHERE account_id=?",
                [account_id],
                |row| row.get(0),
            )
            .optional()?;
        let raw = raw.ok_or_else(|| StoreError::NotFound("paper_account_not_found".into()))?;
        let spec: crate::domain::PaperAccountSpec = serde_json::from_str(&raw)?;
        let initial_split = spec.validate().map_err(StoreError::Rules)?;
        let current = Self::account_weights(&txn, &spec)?;
        let policy = crate::domain::AllocationPolicy::default();
        if target
            .iter()
            .any(|weight| *weight < policy.min_slot_bps || *weight > policy.max_slot_bps)
        {
            return Err(StoreError::Rules(RuleError(
                "rebalance_weight_out_of_bounds".into(),
            )));
        }
        if target[0] + target[1] + spec.reserve_bps != 10_000 {
            return Err(StoreError::Rules(RuleError(
                "rebalance_weights_must_keep_reserve".into(),
            )));
        }
        for index in 0..2 {
            if (target[index] - current[index]).abs() > policy.max_step_bps {
                return Err(StoreError::Rules(RuleError("rebalance_step_exceeded".into())));
            }
        }

        // Idempotency: one transfer row per proposal digest.
        let existing: Option<(String, String, i64)> = txn
            .query_row(
                "SELECT from_slot,to_slot,amount_cents FROM v2_paper_transfers
                 WHERE account_id=? AND digest=?",
                params![account_id, proposal_digest],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        if let Some((from_slot, to_slot, amount_cents)) = existing {
            let stored_weights: Vec<i64> = serde_json::from_str(&txn.query_row(
                "SELECT weights_after_json FROM v2_paper_transfers WHERE account_id=? AND digest=?",
                params![account_id, proposal_digest],
                |row| row.get::<_, String>(0),
            )?)?;
            if stored_weights != vec![target[0], target[1]] {
                return Err(StoreError::Conflict("rebalance_digest_conflict".into()));
            }
            txn.commit()?;
            let mut record = RebalanceRecord {
                account_id: account_id.into(),
                from_slot,
                to_slot,
                amount_cents,
                weights_before: current,
                weights_after: target,
                applied_at: applied_at.into(),
                note: note.into(),
                proposal_digest: proposal_digest.into(),
                digest: String::new(),
            };
            record.digest = rebalance_digest(&record)?;
            return Ok(record);
        }

        let next_split = spec
            .split_with_weights(target)
            .map_err(StoreError::Rules)?;
        let state_a = Self::paper_slot_state(&txn, &spec, &initial_split, 0)?;
        let state_b = Self::paper_slot_state(&txn, &spec, &initial_split, 1)?;
        let delta = next_split.slot_budgets[0] - state_a.cash_cents;
        let (from_slot, to_slot, amount_cents) = if delta > 0 {
            if state_b.cash_cents < delta {
                return Err(StoreError::Rules(RuleError(
                    "rebalance_insufficient_cash".into(),
                )));
            }
            ("B", "A", delta)
        } else if delta < 0 {
            let amount = -delta;
            if state_a.cash_cents < amount {
                return Err(StoreError::Rules(RuleError(
                    "rebalance_insufficient_cash".into(),
                )));
            }
            ("A", "B", amount)
        } else {
            ("B", "A", 0)
        };

        txn.execute(
            "INSERT INTO v2_paper_transfers
             (account_id,from_slot,to_slot,amount_cents,applied_at,note,digest,
              weights_before_json,weights_after_json)
             VALUES(?,?,?,?,?,?,?,?,?)",
            params![
                account_id,
                from_slot,
                to_slot,
                amount_cents,
                applied_at,
                note,
                proposal_digest,
                serde_json::to_string(&current)?,
                serde_json::to_string(&target)?,
            ],
        )?;
        txn.execute(
            "UPDATE v2_paper_accounts SET weights_json=? WHERE account_id=?",
            params![serde_json::to_string(&target)?, account_id],
        )?;
        txn.commit()?;

        let mut record = RebalanceRecord {
            account_id: account_id.into(),
            from_slot: from_slot.into(),
            to_slot: to_slot.into(),
            amount_cents,
            weights_before: current,
            weights_after: target,
            applied_at: applied_at.into(),
            note: note.into(),
            proposal_digest: proposal_digest.into(),
            digest: String::new(),
        };
        record.digest = rebalance_digest(&record)?;
        Ok(record)
    }
}

fn rebalance_digest(record: &RebalanceRecord) -> StoreResult<String> {
    let mut value = serde_json::to_value(record)?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    Ok(crate::domain::canonical_digest(&value))
}
// ---------------------------------------------------------------------------
// Account-level day advance: orchestrate every running session on one account
// against an explicit China trading day. No daemon, no machine clock.
// ---------------------------------------------------------------------------

impl Store {
    pub fn advance_account_to(
        &mut self,
        account_id: &str,
        day: &str,
    ) -> StoreResult<AccountAdvance> {
        chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .map_err(|_| StoreError::Rules(RuleError("invalid_advance_day".into())))?;
        let exists: Option<String> = self
            .conn
            .query_row(
                "SELECT account_id FROM v2_paper_accounts WHERE account_id=?",
                [account_id],
                |row| row.get(0),
            )
            .optional()?;
        if exists.is_none() {
            return Err(StoreError::NotFound("paper_account_not_found".into()));
        }
        let mut statement = self
            .conn
            .prepare("SELECT session_id,status FROM v2_paper_sessions WHERE account_id=? ORDER BY session_id")?;
        let sessions: Vec<(String, String)> = statement
            .query_map([account_id], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?;
        drop(statement);
        let mut runs = Vec::new();
        let mut steps = 0usize;
        let mut filled = 0usize;
        let mut rejected = 0usize;
        for (session_id, status) in sessions {
            if status != "running" {
                continue;
            }
            let run = self.advance_paper_session_to(&session_id, day)?;
            steps += run.steps;
            filled += run.filled;
            rejected += run.rejected;
            runs.push(run);
        }
        Ok(AccountAdvance {
            account_id: account_id.into(),
            day: day.into(),
            sessions: runs,
            steps,
            filled,
            rejected,
        })
    }
}
