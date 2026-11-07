//! SQLite is an append-only simulation ledger with exactly one Rust write owner.
use crate::domain::{decide, validate_quote_import, Fill, Lot, Outcome, POLICY_VERSION, PortfolioState, QuoteEvidence, QuoteImport, QuoteRef, QuoteTrust, RuleError, SimulationRequest};
use crate::domain::{TradingCalendar, SessionPolicy, BUNDLED_CALENDAR_JSON, BUNDLED_SESSION_POLICY_JSON};
use crate::domain::{InstrumentSnapshot, BUNDLED_INSTRUMENTS_JSON};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use std::fmt;
use crate::domain::{Experiment, ExperimentSpec, ExperimentStatus, StrategyArtifact, MARKET_DATA_PACK_ID};

#[derive(Debug)]
pub enum StoreError {
    Database(rusqlite::Error),
    Encoding(serde_json::Error),
    Rules(RuleError),
    Conflict(String),
    Integrity(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Database(error) => write!(f, "database: {error}"),
            Self::Encoding(error) => write!(f, "encoding: {error}"),
            Self::Rules(error) => write!(f, "rules: {error}"),
            Self::Conflict(error) => write!(f, "conflict: {error}"),
            Self::Integrity(error) => write!(f, "integrity: {error}"),
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
        const SCHEMA_VERSION: i64 = 6;
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
                ("v2_experiments", &["cycle_id", "spec_json", "status"]),
                ("v2_strategy_artifacts", &["strategy_version_id", "artifact_json"]),
                ("v2_simulation_runs", &["id", "cycle_id", "strategy_version_id", "signal_id", "request_json", "evidence_json", "outcome_json", "calendar_version", "session_policy_version", "instrument_version"]),
                ("v2_fills", &["id", "run_id", "fill_json"]),
                ("v2_quote_evidence", &["source", "quote_id", "evidence_json"]),
            ];
            for (kind, name, table) in &objects {
                let known = (kind == "table" && tables.iter().any(|(t, _)| t == name))
                    || (kind == "index" && name == "idx_v2_runs_cycle" && table == "v2_simulation_runs");
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
        } else {
            // Persisted market facts must be checked before changing journal mode.
            load_market_context(&conn)?;
        }
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
                CREATE TABLE v2_strategy_artifacts (
                    strategy_version_id TEXT PRIMARY KEY NOT NULL,
                    artifact_json TEXT NOT NULL
                );
                CREATE TABLE v2_experiments (
                    cycle_id TEXT PRIMARY KEY NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','completed'))
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
                CREATE TABLE v2_quote_evidence (
                    source TEXT NOT NULL,
                    quote_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    PRIMARY KEY(source, quote_id)
                );"
            )?;
            txn.pragma_update(None, "application_id", APPLICATION_ID)?;
            txn.pragma_update(None, "user_version", SCHEMA_VERSION)?;
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('market_data_pack_id',?)", [MARKET_DATA_PACK_ID])?;
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('calendar_snapshot_json',?)", [BUNDLED_CALENDAR_JSON])?;
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('session_policy_snapshot_json',?)", [BUNDLED_SESSION_POLICY_JSON])?;
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('instrument_snapshot_json',?)", [BUNDLED_INSTRUMENTS_JSON])?;
            txn.commit()?;
        }
        Ok(Self { conn })
    }

    /// Explicit, immutable capital ownership; no implicit default experiment.
    pub fn create_experiment(&mut self, spec: ExperimentSpec) -> StoreResult<Experiment> {
        validate_experiment_spec(&spec)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        load_market_context(&txn)?;
        let existing: Option<String> = txn.query_row("SELECT spec_json FROM v2_experiments WHERE cycle_id=?", [&spec.cycle_id], |r| r.get(0)).optional()?;
        if let Some(raw) = existing {
            if serde_json::from_str::<ExperimentSpec>(&raw)? != spec {
                return Err(StoreError::Conflict("experiment_identity_payload_conflict".into()));
            }
            return load_experiment(&txn, &spec.cycle_id);
        }
        let artifact: Option<String> = txn.query_row("SELECT artifact_json FROM v2_strategy_artifacts WHERE strategy_version_id=?", [&spec.strategy.strategy_version_id], |r| r.get(0)).optional()?;
        if let Some(raw) = artifact {
            if serde_json::from_str::<StrategyArtifact>(&raw)? != spec.strategy {
                return Err(StoreError::Conflict("strategy_artifact_identity_conflict".into()));
            }
        } else {
            txn.execute("INSERT INTO v2_strategy_artifacts VALUES(?,?)", params![spec.strategy.strategy_version_id, serde_json::to_string(&spec.strategy)?])?;
        }
        txn.execute("INSERT INTO v2_experiments VALUES(?,?,'active')", params![spec.cycle_id, serde_json::to_string(&spec)?])?;
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

    /// Immutable evidence, owned by the same single writer as the ledger.
    pub fn ingest_quote(&mut self, input: QuoteImport) -> StoreResult<QuoteEvidence> {
        validate_quote_import(&input)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let (_, _, instruments) = load_market_context(&txn)?;
        let identity = QuoteRef {
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
            schema_version: 4,
            instrument: instruments.identity(&input.quote.symbol).cloned(),
            available_at: input.available_at, quote: input.quote,
            execution: input.execution,
            trust_level: QuoteTrust::OfflineFixture,
            provider_published_at: None, received_at,
        };
        txn.execute(
            "INSERT INTO v2_quote_evidence(source,quote_id,evidence_json) VALUES(?,?,?)",
            params![identity.source, identity.quote_id, serde_json::to_string(&evidence)?],
        )?;
        txn.commit()?;
        Ok(evidence)
    }

    /// All mutations and idempotency checks occur under one IMMEDIATE transaction.
    pub fn submit(&mut self, request: SimulationRequest) -> StoreResult<RunRecord> {
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let experiment = load_experiment(&txn, &request.cycle_id)?;
        if request.schema_version != 3 {
            return Err(StoreError::Rules(RuleError("unsupported_contract_version".into())));
        }
        if request.strategy_version_id != experiment.spec.strategy.strategy_version_id {
            return Err(StoreError::Conflict("experiment_strategy_mismatch".into()));
        }
        let (calendar, sessions, instruments) = load_market_context(&txn)?;
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
            let evidence = serde_json::from_str(&raw_evidence)?;
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

fn load_market_context(conn: &Connection) -> StoreResult<(TradingCalendar, SessionPolicy, InstrumentSnapshot)> {
    let pack: String = conn.query_row("SELECT value FROM v2_meta WHERE key='market_data_pack_id'", [], |r| r.get(0))?;
    if pack != MARKET_DATA_PACK_ID { return Err(StoreError::Integrity("unsupported_market_data_pack".into())); }
    // This equality is the integrity check for this explicitly selected pack,
    // not a schema version. See ADR-001 before adding another supported pack.
    let raw_calendar: String = conn.query_row(
        "SELECT value FROM v2_meta WHERE key='calendar_snapshot_json'", [], |r| r.get(0)
    )?;
    let raw_sessions: String = conn.query_row(
        "SELECT value FROM v2_meta WHERE key='session_policy_snapshot_json'", [], |r| r.get(0)
    )?;
    let calendar: TradingCalendar = serde_json::from_str(&raw_calendar)?;
    let sessions: SessionPolicy = serde_json::from_str(&raw_sessions)?;
    let raw_instruments: String = conn.query_row(
        "SELECT value FROM v2_meta WHERE key='instrument_snapshot_json'", [], |r| r.get(0)
    )?;
    let instruments: InstrumentSnapshot = serde_json::from_str(&raw_instruments)?;
    calendar.validate()?;
    sessions.validate()?;
    instruments.validate()?;
    // Never reinterpret a ledger using a later edited file or the same version
    // name with different content. Unsupported versions require an explicit path.
    if serde_json::to_value(&calendar)? != serde_json::from_str::<serde_json::Value>(BUNDLED_CALENDAR_JSON)?
        || serde_json::to_value(&sessions)? != serde_json::from_str::<serde_json::Value>(BUNDLED_SESSION_POLICY_JSON)?
        || serde_json::to_value(&instruments)? != serde_json::from_str::<serde_json::Value>(BUNDLED_INSTRUMENTS_JSON)?
    {
        return Err(StoreError::Integrity("pinned_market_snapshot_mismatch".into()));
    }
    Ok((calendar, sessions, instruments))
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
        "SELECT evidence_json FROM v2_quote_evidence WHERE source=? AND quote_id=?",
        params![identity.source, identity.quote_id], |row| row.get(0),
    ).optional()?;
    Ok(raw.map(|value| serde_json::from_str(&value)).transpose()?)
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
    if spec.market_data_pack_id != MARKET_DATA_PACK_ID {
        return Err(StoreError::Rules(RuleError("unsupported_market_data_pack".into())));
    }
    Ok(())
}

fn load_experiment(conn: &Connection, cycle_id: &str) -> StoreResult<Experiment> {
    let row: Option<(String, String)> = conn.query_row("SELECT spec_json,status FROM v2_experiments WHERE cycle_id=?", [cycle_id], |r| Ok((r.get(0)?, r.get(1)?))).optional()?;
    let (raw, status) = row.ok_or_else(|| StoreError::Rules(RuleError("experiment_not_found".into())))?;
    let spec: ExperimentSpec = serde_json::from_str(&raw)?;
    validate_experiment_spec(&spec)?;
    if spec.cycle_id != cycle_id { return Err(StoreError::Integrity("experiment_identity_mismatch".into())); }
    let raw_artifact: String = conn.query_row("SELECT artifact_json FROM v2_strategy_artifacts WHERE strategy_version_id=?", [&spec.strategy.strategy_version_id], |r| r.get(0))?;
    if serde_json::from_str::<StrategyArtifact>(&raw_artifact)? != spec.strategy {
        return Err(StoreError::Integrity("experiment_artifact_mismatch".into()));
    }
    let status = match status.as_str() {
        "active" => ExperimentStatus::Active,
        "completed" => ExperimentStatus::Completed,
        _ => return Err(StoreError::Integrity("invalid_experiment_status".into())),
    };
    Ok(Experiment { spec, status })
}
