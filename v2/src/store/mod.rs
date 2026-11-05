//! SQLite is an append-only simulation ledger with exactly one Rust write owner.
use crate::domain::{decide, validate_evidence, Fill, Lot, Outcome, POLICY_VERSION, PortfolioState, QuoteEvidence, QuoteRef, RuleError, SimulationRequest};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use std::fmt;

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
    pub initial_cash_cents: i64,
    pub cash_cents: i64,
    pub asof_day: Option<String>,
    pub positions: Vec<PositionView>,
    pub lots: Vec<Lot>,
    pub runs_total: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayReport {
    pub ok: bool,
    pub runs_verified: usize,
    pub fills_verified: usize,
    pub cash_cents: i64,
}

pub struct Store {
    conn: Connection,
    initial_cash_cents: i64,
}

impl Store {
    pub fn open(path: &str, initial_cash_cents: i64) -> StoreResult<Self> {
        Self::with_connection(Connection::open(path)?, initial_cash_cents)
    }

    pub fn in_memory(initial_cash_cents: i64) -> StoreResult<Self> {
        Self::with_connection(Connection::open_in_memory()?, initial_cash_cents)
    }

    fn with_connection(mut conn: Connection, initial_cash_cents: i64) -> StoreResult<Self> {
        const APPLICATION_ID: i64 = 0x41535632; // ASV2, independent SQLite file identity.
        const SCHEMA_VERSION: i64 = 2;
        if initial_cash_cents <= 0 {
            return Err(StoreError::Integrity("initial_cash_must_be_positive".into()));
        }
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
                ("v2_simulation_runs", &["id", "cycle_id", "strategy_version_id", "signal_id", "request_json", "evidence_json", "outcome_json"]),
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
            let policy: Option<String> = conn.query_row(
                "SELECT value FROM v2_meta WHERE key='policy_version'", [], |r| r.get(0)
            ).optional()?;
            if policy.as_deref() != Some(POLICY_VERSION) {
                return Err(StoreError::Integrity("policy_version_mismatch_migration_required".into()));
            }
            let seed: Option<String> = conn.query_row(
                "SELECT value FROM v2_meta WHERE key='initial_cash_cents'", [], |r| r.get(0)
            ).optional()?;
            if seed.as_deref() != Some(initial_cash_cents.to_string().as_str()) {
                return Err(StoreError::Integrity("initial_cash_mismatch_refusing_to_reinterpret_ledger".into()));
            }
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
                CREATE TABLE v2_simulation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    strategy_version_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
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
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('initial_cash_cents',?)",
                [initial_cash_cents.to_string()])?;
            txn.execute("INSERT INTO v2_meta(key,value) VALUES('policy_version',?)", [POLICY_VERSION])?;
            txn.commit()?;
        }
        Ok(Self { conn, initial_cash_cents })
    }

    /// Immutable evidence, owned by the same single writer as the ledger.
    pub fn ingest_quote(&mut self, evidence: QuoteEvidence) -> StoreResult<QuoteEvidence> {
        validate_evidence(&evidence)?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let identity = QuoteRef {
            source: evidence.quote.source.clone(), quote_id: evidence.quote.quote_id.clone(),
        };
        if let Some(stored) = load_evidence(&txn, &identity)? {
            if stored != evidence {
                return Err(StoreError::Conflict("quote_identity_payload_conflict".into()));
            }
            return Ok(stored);
        }
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
        let prior: Option<(i64, String, String, String)> = txn.query_row(
            "SELECT id,request_json,outcome_json,evidence_json FROM v2_simulation_runs
             WHERE cycle_id=? AND strategy_version_id=? AND signal_id=?",
            params![request.cycle_id, request.strategy_version_id, request.signal_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        ).optional()?;
        if let Some((id, raw_request, raw_outcome, raw_evidence)) = prior {
            let old_request: SimulationRequest = serde_json::from_str(&raw_request)?;
            if old_request != request {
                return Err(StoreError::Conflict(
                    "same_signal_identity_with_different_request".into()
                ));
            }
            let outcome = serde_json::from_str(&raw_outcome)?;
            let evidence = serde_json::from_str(&raw_evidence)?;
            return Ok(RunRecord { id, request, evidence, outcome, duplicate: true });
        }
        // V2 is append-only. Out-of-order historical corrections require a
        // separate explicit rebase/import operation; never silently recalculate
        // past positions by appending a request with an earlier decision time.
        let previous_request: Option<String> = txn.query_row(
            "SELECT request_json FROM v2_simulation_runs ORDER BY id DESC LIMIT 1",
            [], |row| row.get(0),
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
        let state = load_state(&txn, self.initial_cash_cents)?;
        let evidence = load_evidence(&txn, &request.quote_ref)?;
        let outcome = decide(&state, &request, evidence.as_ref())?;
        txn.execute(
            "INSERT INTO v2_simulation_runs
             (cycle_id,strategy_version_id,signal_id,request_json,outcome_json,evidence_json)
             VALUES(?,?,?,?,?,?)",
            params![
                request.cycle_id, request.strategy_version_id, request.signal_id,
                serde_json::to_string(&request)?, serde_json::to_string(&outcome)?,
                serde_json::to_string(&evidence)?
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
        Ok(RunRecord { id, request, evidence, outcome, duplicate: false })
    }

}

fn load_evidence(conn: &Connection, identity: &QuoteRef) -> StoreResult<Option<QuoteEvidence>> {
    let raw: Option<String> = conn.query_row(
        "SELECT evidence_json FROM v2_quote_evidence WHERE source=? AND quote_id=?",
        params![identity.source, identity.quote_id], |row| row.get(0),
    ).optional()?;
    Ok(raw.map(|value| serde_json::from_str(&value)).transpose()?)
}

fn load_state(conn: &Connection, initial_cash: i64) -> StoreResult<PortfolioState> {
    let mut state = PortfolioState::new(initial_cash);
    let mut stmt = conn.prepare("SELECT fill_json FROM v2_fills ORDER BY id")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
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
