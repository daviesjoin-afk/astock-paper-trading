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
        const SCHEMA_VERSION: i64 = 9;
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
                ("v2_paper_accounts", &["account_id", "spec_json", "created_at"]),
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
                    created_at TEXT NOT NULL
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
            return Ok(paper_account_view(&spec, &split));
        }
        txn.execute(
            "INSERT INTO v2_paper_accounts(account_id,spec_json,created_at) VALUES(?,?,?)",
            params![spec.account_id, raw, spec.created_at],
        )?;
        txn.commit()?;
        Ok(paper_account_view(&spec, &split))
    }
}

/// Build the read model from a validated spec. Slot cash starts at its budget:
/// the reserve never trades and no cash exists outside the two slots.
pub(crate) fn paper_account_view(
    spec: &crate::domain::PaperAccountSpec,
    split: &crate::domain::PaperCapitalSplit,
) -> PaperAccountView {
    let mut slots = Vec::new();
    for slot in &spec.slots {
        let index = match slot.slot {
            crate::domain::PaperSlotId::A => 0,
            crate::domain::PaperSlotId::B => 1,
        };
        let budget_cents = split.slot_budgets[index];
        slots.push(PaperSlotView {
            slot: slot.slot.as_str().into(),
            strategy_version_id: slot.strategy.strategy_version_id.clone(),
            research_input_id: slot.strategy.research_input_id.clone(),
            market_data_pack_id: slot.market_data_pack_id.clone(),
            execution_policy_version: slot.execution_policy_version.clone(),
            weight_bps: slot.weight_bps,
            budget_cents,
            cash_cents: budget_cents,
            positions: Vec::new(),
        });
    }
    let cash_cents = split.reserve_cents + split.slot_budgets.iter().sum::<i64>();
    PaperAccountView {
        account_id: spec.account_id.clone(),
        initial_cash_cents: spec.initial_cash_cents,
        created_at: spec.created_at.clone(),
        cash_cents,
        reserve_cents: split.reserve_cents,
        slots,
    }
}
