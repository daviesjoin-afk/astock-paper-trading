//! SQLite is an append-only simulation ledger with exactly one Rust write owner.
use crate::domain::{decide, Fill, Lot, Outcome, POLICY_VERSION, PortfolioState, RuleError, SimulationRequest};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
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

    fn with_connection(conn: Connection, initial_cash_cents: i64) -> StoreResult<Self> {
        if initial_cash_cents <= 0 {
            return Err(StoreError::Integrity("initial_cash_must_be_positive".into()));
        }
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;")?;
        let legacy: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN \
             ('paper_orders','paper_fills','dual_ai_tuning_runs')",
            [], |row| row.get(0),
        )?;
        if legacy > 0 {
            return Err(StoreError::Integrity(
                "refusing_to_open_legacy_v1_database".into()
            ));
        }
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS v2_meta (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_simulation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL,
                strategy_version_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                UNIQUE(cycle_id, strategy_version_id, signal_id)
            );
            CREATE TABLE IF NOT EXISTS v2_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL UNIQUE
                    REFERENCES v2_simulation_runs(id) ON DELETE RESTRICT,
                fill_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v2_runs_cycle
                ON v2_simulation_runs(cycle_id, id);"
        )?;
        let existing: Option<String> = conn.query_row(
            "SELECT value FROM v2_meta WHERE key='initial_cash_cents'",
            [], |row| row.get(0),
        ).optional()?;
        match existing {
            None => {
                conn.execute(
                    "INSERT INTO v2_meta(key,value) VALUES('initial_cash_cents',?)",
                    [initial_cash_cents.to_string()],
                )?;
            }
            Some(stored) if stored == initial_cash_cents.to_string() => {}
            Some(_) => return Err(StoreError::Integrity(
                "initial_cash_mismatch_refusing_to_reinterpret_ledger".into()
            )),
        }
        let policy: Option<String> = conn.query_row(
            "SELECT value FROM v2_meta WHERE key='policy_version'",
            [], |row| row.get(0),
        ).optional()?;
        match policy {
            None => {
                conn.execute(
                    "INSERT INTO v2_meta(key,value) VALUES('policy_version',?)",
                    [POLICY_VERSION],
                )?;
            }
            Some(stored) if stored == POLICY_VERSION => {}
            Some(_) => return Err(StoreError::Integrity(
                "policy_version_mismatch_migration_required".into()
            )),
        }
        Ok(Self { conn, initial_cash_cents })
    }

    /// All mutations and idempotency checks occur under one IMMEDIATE transaction.
    pub fn submit(&mut self, request: SimulationRequest) -> StoreResult<RunRecord> {
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior: Option<(i64, String, String)> = txn.query_row(
            "SELECT id,request_json,outcome_json FROM v2_simulation_runs
             WHERE cycle_id=? AND strategy_version_id=? AND signal_id=?",
            params![request.cycle_id, request.strategy_version_id, request.signal_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        ).optional()?;
        if let Some((id, raw_request, raw_outcome)) = prior {
            let old_request: SimulationRequest = serde_json::from_str(&raw_request)?;
            if old_request != request {
                return Err(StoreError::Conflict(
                    "same_signal_identity_with_different_request".into()
                ));
            }
            let outcome = serde_json::from_str(&raw_outcome)?;
            return Ok(RunRecord { id, request, outcome, duplicate: true });
        }
        let state = load_state(&txn, self.initial_cash_cents)?;
        let outcome = decide(&state, &request)?;
        txn.execute(
            "INSERT INTO v2_simulation_runs
             (cycle_id,strategy_version_id,signal_id,request_json,outcome_json)
             VALUES(?,?,?,?,?)",
            params![
                request.cycle_id, request.strategy_version_id, request.signal_id,
                serde_json::to_string(&request)?, serde_json::to_string(&outcome)?
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
        Ok(RunRecord { id, request, outcome, duplicate: false })
    }

    pub fn state(&self, asof_day: Option<&str>) -> StoreResult<PortfolioView> {
        if let Some(day) = asof_day {
            chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
                .map_err(|_| StoreError::Rules(RuleError("invalid_asof_day".into())))?;
        }
        let state = load_state(&self.conn, self.initial_cash_cents)?;
        let mut quantities = BTreeMap::<String, i64>::new();
        for lot in &state.lots {
            *quantities.entry(lot.symbol.clone()).or_default() += lot.quantity;
        }
        let positions = quantities.into_iter().map(|(symbol, quantity)| PositionView {
            sellable_quantity: asof_day.map(|day| state.sellable_quantity(&symbol, day)),
            symbol, quantity,
        }).collect();
        let runs_total = self.conn.query_row(
            "SELECT COUNT(*) FROM v2_simulation_runs", [], |row| row.get(0)
        )?;
        Ok(PortfolioView {
            initial_cash_cents: self.initial_cash_cents,
            cash_cents: state.cash_cents, asof_day: asof_day.map(str::to_owned),
            positions, lots: state.lots, runs_total,
        })
    }

    pub fn runs(&self, limit: i64) -> StoreResult<Vec<RunRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id,request_json,outcome_json FROM v2_simulation_runs
             ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map([limit.clamp(1, 1000)], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?))
        })?;
        let mut runs = Vec::new();
        for row in rows {
            let (id, req, outcome) = row?;
            runs.push(RunRecord {
                id, request: serde_json::from_str(&req)?,
                outcome: serde_json::from_str(&outcome)?, duplicate: false,
            });
        }
        Ok(runs)
    }

    /// Re-run every stored decision from pinned inputs, comparing persisted facts.
    pub fn verify_replay(&self) -> StoreResult<ReplayReport> {
        let mut state = PortfolioState::new(self.initial_cash_cents);
        let mut runs = self.conn.prepare(
            "SELECT id,request_json,outcome_json FROM v2_simulation_runs ORDER BY id"
        )?;
        let rows = runs.query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?))
        })?;
        let mut run_count = 0;
        let mut fill_count = 0;
        for row in rows {
            let (id, req_json, result_json) = row?;
            let req: SimulationRequest = serde_json::from_str(&req_json)?;
            let recorded: Outcome = serde_json::from_str(&result_json)?;
            let calculated = decide(&state, &req)?;
            if calculated != recorded {
                return Err(StoreError::Integrity(
                    format!("replay_outcome_mismatch_at_run_{id}")
                ));
            }
            let stored_fill: Option<String> = self.conn.query_row(
                "SELECT fill_json FROM v2_fills WHERE run_id=?",
                [id], |row| row.get(0),
            ).optional()?;
            match (calculated.fill.as_ref(), stored_fill) {
                (None, None) => {}
                (Some(fill), Some(raw)) => {
                    let saved: Fill = serde_json::from_str(&raw)?;
                    if *fill != saved {
                        return Err(StoreError::Integrity(
                            format!("replay_fill_mismatch_at_run_{id}")
                        ));
                    }
                    state.apply(fill)?;
                    fill_count += 1;
                }
                _ => return Err(StoreError::Integrity(
                    format!("missing_or_unexpected_fill_at_run_{id}")
                )),
            };
            run_count += 1;
        }
        let persisted = load_state(&self.conn, self.initial_cash_cents)?;
        if state != persisted {
            return Err(StoreError::Integrity("replay_portfolio_mismatch".into()));
        }
        Ok(ReplayReport {
            ok: true, runs_verified: run_count, fills_verified: fill_count,
            cash_cents: state.cash_cents,
        })
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{Side, Signal, Quote, INITIAL_CASH_CENTS, DecisionStatus};

    fn request(day: &str, signal_id: &str, side: Side) -> SimulationRequest {
        SimulationRequest {
            schema_version: 1, cycle_id: "cycle-1".into(),
            strategy_version_id: "strategy-v1".into(),
            signal_id: signal_id.into(),
            decision_at: format!("{day}T10:00:00+08:00"),
            signal: Signal { symbol: "600000.SH".into(), side },
            quote: Quote {
                symbol: "600000.SH".into(), price_cents: 1234,
                observed_at: format!("{day}T09:59:30+08:00"),
                source: "offline-fixture".into(), quote_id: format!("quote-{signal_id}"),
                tradable: true, limit_up: false, limit_down: false,
            },
        }
    }

    #[test]
    fn dedup_is_exact_and_does_not_duplicate_fills() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "b1", Side::Buy);
        let first = db.submit(req.clone()).unwrap();
        let second = db.submit(req).unwrap();
        assert!(!first.duplicate);
        assert!(second.duplicate);
        assert_eq!(first.id, second.id);
        let view = db.state(Some("2026-10-30")).unwrap();
        assert_eq!(view.runs_total, 1);
        assert_eq!(view.positions.len(), 1);
        assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
    }

    #[test]
    fn conflicting_payload_under_same_identity_is_rejected() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-10-30", "b1", Side::Buy)).unwrap();
        let mut changed = request("2026-10-30", "b1", Side::Buy);
        changed.quote.price_cents = 999;
        assert!(matches!(db.submit(changed), Err(StoreError::Conflict(_))));
        assert_eq!(db.state(None).unwrap().runs_total, 1);
    }

    #[test]
    fn replay_detects_tampered_fill() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-10-30", "b1", Side::Buy)).unwrap();
        assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
        db.conn.execute("UPDATE v2_fills SET fill_json='{}' WHERE id=1", []).unwrap();
        assert!(db.verify_replay().is_err());
    }

    #[test]
    fn rejection_is_persisted_without_fill() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let out = db.submit(request("2026-11-02", "s1", Side::Sell)).unwrap();
        assert_eq!(out.outcome.status, DecisionStatus::Rejected);
        assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
    }

    #[test]
    fn buy_and_next_day_sell_replay() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-10-30", "b1", Side::Buy)).unwrap();
        let blocked = db.submit(request("2026-10-30", "s0", Side::Sell)).unwrap();
        assert_eq!(blocked.outcome.reason, "no_t1_sellable_lot");
        let sold = db.submit(request("2026-11-02", "s1", Side::Sell)).unwrap();
        assert_eq!(sold.outcome.status, DecisionStatus::Filled);
        assert_eq!(db.state(Some("2026-11-02")).unwrap().positions.len(), 0);
        let verified = db.verify_replay().unwrap();
        assert_eq!((verified.runs_verified, verified.fills_verified), (3, 2));
    }
}
