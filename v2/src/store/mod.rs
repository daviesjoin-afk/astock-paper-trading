//! SQLite is the only writer. A trading day is one atomic commit: settle,
//! plan, execute, value and rebalance either all succeed or none do.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Transaction};

use crate::domain::{
    apply_to_policy, apply_buy, apply_sell, classify_series, drawdown_bps, fees_cents,
    initial_split, orders_for_day_in_split, performance_curve, propose, score, score_signal_adjusted, settle_overnight, AccountSpec,
    AccountView, AdjustmentBook, AdvanceResult, DayRun, IndexedPriceSet, PositionView, PriceCoverage, PriceSet,
    PerformanceInput, PerformancePoint, Rebalance, RegimeView, RuleError, SignalScore, SlotBook, SlotPolicy, SlotSpec, SlotView,
    StrategyId, Trade,
};

pub const SCHEMA_VERSION: i64 = 2;

/// One unadjusted bar row read for settlement, including the session fields a
/// price-limit seal needs. The close alone cannot tell a sealed session from
/// one that traded through its limit.
pub struct UnadjustedRow {
    pub symbol: String,
    pub day: String,
    pub price_cents: i64,
    pub tradable: bool,
    pub open_cents: i64,
    pub high_cents: i64,
    pub low_cents: i64,
    pub prev_close_cents: i64,
    /// Whole shares traded. Zero when the source carried none.
    pub volume_shares: i64,
}

/// How many trailing closes a strategy needs to decide today.
///
/// The longest requirement in `domain::strategy` is 61 closes (a 60-day mean
/// plus the latest bar). Anything older cannot change the signal, so there is
/// no reason to carry it through the moving-average code -- on a 5.3k-symbol
/// universe that difference is ~4M rows versus ~320k per settled day.
pub const SIGNAL_LOOKBACK_DAYS: usize = 61;

/// Trading days of history the market-regime classifier needs.
///
/// Two slow windows: one to measure, one to make sure the fast average is not
/// dominated by a handful of days when a slice of the market was missing.
pub const REGIME_LOOKBACK_DAYS: usize = 120;

#[derive(Debug)]
pub enum StoreError {
    Database(rusqlite::Error),
    Encoding(serde_json::Error),
    Rules(RuleError),
    NotFound(String),
    Conflict(String),
    /// A run was deliberately stopped by a resource/deadline guard.
    /// Days already committed are durable; the response reports the break
    /// instead of pretending the whole batch ran.
    Aborted(String),
    Integrity(String),
}

impl From<rusqlite::Error> for StoreError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Database(error)
    }
}

impl From<serde_json::Error> for StoreError {
    fn from(error: serde_json::Error) -> Self {
        Self::Encoding(error)
    }
}

impl From<RuleError> for StoreError {
    fn from(error: RuleError) -> Self {
        Self::Rules(error)
    }
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Database(error) => write!(formatter, "database: {error}"),
            Self::Encoding(error) => write!(formatter, "encoding: {error}"),
            Self::Rules(error) => write!(formatter, "rules: {error}"),
            Self::NotFound(error) => write!(formatter, "not_found: {error}"),
            Self::Conflict(error) => write!(formatter, "conflict: {error}"),
            Self::Aborted(error) => write!(formatter, "aborted: {error}"),
            Self::Integrity(error) => write!(formatter, "integrity: {error}"),
        }
    }
}

impl std::error::Error for StoreError {}

pub type StoreResult<T> = Result<T, StoreError>;

/// One advance request's resource ceiling. Kept in `store` because the store
/// owns the whole-window settlement view; the HTTP layer supplies values from
/// environment configuration.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AdvanceLimits {
    /// Retained `PriceEntry` rows, each with several Strings. 700k keeps the
    /// ordinary daily window and rejects the old 260-day catch-up before the
    /// memory-heavy index is built.
    pub max_entries: usize,
    /// Maximum wall-clock seconds for one advance. Zero means no deadline.
    pub max_seconds: u64,
    /// Maximum trading days committed per lock chunk.
    pub chunk_days: usize,
}

impl Default for AdvanceLimits {
    fn default() -> Self {
        Self {
            max_entries: 700_000,
            max_seconds: 90,
            chunk_days: 10,
        }
    }
}

impl AdvanceLimits {
    /// The hard cap is deliberately generous compared with an ordinary daily
    /// advance and below the measured 1.4 GB whole-market catch-up window.
    pub fn from_env() -> Self {
        fn positive(name: &str, default: usize) -> usize {
            std::env::var(name)
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
                .filter(|value| *value > 0)
                .unwrap_or(default)
        }
        let defaults = Self::default();
        Self {
            max_entries: positive("ASTOCK_V2_MAX_ADVANCE_ENTRIES", defaults.max_entries),
            max_seconds: positive("ASTOCK_V2_MAX_ADVANCE_SECONDS", defaults.max_seconds as usize)
                as u64,
            chunk_days: positive("ASTOCK_V2_ADVANCE_CHUNK_DAYS", defaults.chunk_days),
        }
    }
}

pub struct Store {
    pub conn: Connection,
    /// When false, the aggressive slot ignores the market regime and uses its
    /// baseline policy.
    ///
    /// Exists so a replay can compare "with the filter" against "without it"
    /// on identical data. A regime filter that cannot be switched off cannot
    /// be shown to help, and the whole point of adding it was a measured
    /// claim, so it has to stay measurable.
    pub regime_filter: bool,
    /// When true, a trading day is decided from the previous session's close
    /// and filled at today's close. The default keeps the same-close book that
    /// earlier results were measured on, so the two timings can be compared on
    /// identical input instead of one replacing the other's baseline.
    ///
    /// A halt is still a halt: a symbol with no tradable close today remains
    /// untouched until it trades again, so a suspended name is skipped for
    /// that session rather than carried at a stale price.
    pub next_session_fills: bool,
    /// Drop the worst lower-wick tail from each day's candidate set.
    ///
    /// Measured on 122 real sessions: the longest-wick quintile underperformed
    /// the market by 89 bps over 5 days (t=-4.8), monotone across ten buckets,
    /// and the effect survived volatility, size and industry neutralisation.
    /// Removing that tail lifted the remaining universe by 14.7 bps over five
    /// days (t=4.1) without changing turnover.
    ///
    /// Off by default and switchable so a replay can compare against the
    /// unfiltered book on identical input. A filter that cannot be turned off
    /// cannot be shown to help.
    pub klow_filter: bool,
    /// Fraction of the cross-section to exclude from the lower-wick tail, in
    /// bps of the candidate count (2_000 = drop the worst 20%).
    pub klow_cut_bps: i64,
    /// Let a recorded AI observation narrow today's slot policy.
    ///
    /// Off by default and switchable so a replay can compare against the
    /// deterministic policy on identical input. With no observation stored the
    /// flag makes no difference at all, which is what makes "the model was
    /// unreachable" indistinguishable from "the model agreed" unless the
    /// `ai_observation` table is consulted -- and it is.
    pub ai_policy: bool,
    /// Resource guard for one advance. Tests may lower `max_entries` to prove
    /// the guard rejects before a large window is indexed.
    pub advance_limits: AdvanceLimits,
}

impl Store {
    /// Offline research reads an existing rewrite database without migrations,
    /// schema changes or creating a missing file. Ledger writes are rejected by SQLite.
    pub fn open_market_read_only(path: impl AsRef<Path>) -> StoreResult<Self> {
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        // Pin one SQLite read snapshot for the entire comparison, even if an
        // ingestion process appends bars while this connection is alive.
        conn.execute_batch("BEGIN DEFERRED")?;
        let legacy: i64 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
        let store = Self {
            conn,
            regime_filter: true,
            next_session_fills: false,
            klow_filter: false,
            klow_cut_bps: 2_000,
            ai_policy: false,
            advance_limits: AdvanceLimits::default(),
        };
        if legacy != 0 || store.schema_version()? != SCHEMA_VERSION {
            return Err(StoreError::Integrity("unsupported_replay_schema".into()));
        }
        Ok(store)
    }

    /// Open a live read-only connection after `Store::open` has initialized the
    /// schema. Unlike `open_market_read_only`, this does not pin a long-lived
    /// snapshot: WAL autocommit gives each fresh statement the committed state
    /// as of that statement.
    ///
    /// SQLite's read-only WAL mode needs a readable `-shm` file. The writer
    /// must already have created the database and its WAL sidecars, which the
    /// HTTP server guarantees by calling `Store::open` first.
    pub fn open_read_only(path: impl AsRef<Path>) -> StoreResult<Self> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        let store = Self {
            conn,
            regime_filter: true,
            next_session_fills: false,
            klow_filter: false,
            klow_cut_bps: 2_000,
            ai_policy: false,
            advance_limits: AdvanceLimits::default(),
        };
        let legacy: i64 =
            store
                .conn
                .pragma_query_value(None, "user_version", |row| row.get(0))?;
        if legacy != 0 || store.schema_version()? != SCHEMA_VERSION {
            return Err(StoreError::Integrity("unsupported_replay_schema".into()));
        }
        Ok(store)
    }

    pub fn open(path: impl AsRef<Path>) -> StoreResult<Self> {
        if path.as_ref() != Path::new(":memory:")
            && let Some(parent) = path.as_ref().parent()
        {
            std::fs::create_dir_all(parent)
                .map_err(|error| StoreError::Integrity(error.to_string()))?;
        }
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        let store = Self {
            conn,
            regime_filter: true,
            next_session_fills: false,
            klow_filter: false,
            klow_cut_bps: 2_000,
            ai_policy: false,
            advance_limits: AdvanceLimits::default(),
        };
        store.migrate()?;
        Ok(store)
    }

    fn migrate(&self) -> StoreResult<()> {
        // Check the old marker before CREATE TABLE. Opening a legacy file must
        // be read-only unless it is already this rewrite's schema.
        let legacy: i64 = self
            .conn
            .query_row("SELECT user_version FROM pragma_user_version", [], |row| {
                row.get(0)
            })?;
        if legacy > 0 {
            return Err(StoreError::Integrity(
                "legacy_v2_database_detected; keep it, start with a new ASTOCK_V2_DB".into(),
            ));
        }
        let has_schema_table: i64 = self.conn.query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_version'",
            [],
            |row| row.get(0),
        )?;
        let existing: Option<i64> = if has_schema_table > 0 {
            self.conn
                .query_row(
                    "SELECT version FROM schema_version LIMIT 1",
                    [],
                    |row| row.get(0),
                )
                .optional()?
        } else {
            None
        };
        if let Some(version) = existing
            && version != SCHEMA_VERSION
        {
            return Err(StoreError::Integrity(format!(
                "unsupported_schema_version:{version}; create a new database"
            )));
        }
        self.conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS account(
                account_id TEXT PRIMARY KEY,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS slot(
                account_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                weight_bps INTEGER NOT NULL,
                cash_cents INTEGER NOT NULL,
                PRIMARY KEY(account_id, slot)
            );
            CREATE TABLE IF NOT EXISTS position(
                account_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                average_cost_cents INTEGER NOT NULL,
                acquired_day TEXT NOT NULL,
                PRIMARY KEY(account_id, slot, symbol)
            );
            CREATE TABLE IF NOT EXISTS sellable(
                account_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY(account_id, slot, symbol)
            );
            CREATE TABLE IF NOT EXISTS slot_equity_day(
                account_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                day TEXT NOT NULL,
                equity_cents INTEGER NOT NULL,
                PRIMARY KEY(account_id, slot, day)
            );
            CREATE TABLE IF NOT EXISTS slot_day(
                account_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                settled_day TEXT NOT NULL,
                PRIMARY KEY(account_id, slot)
            );
            CREATE TABLE IF NOT EXISTS day_run(
                account_id TEXT NOT NULL,
                day TEXT NOT NULL,
                PRIMARY KEY(account_id, day)
            );
            -- What the AI observer advised for a slot on a day, after clamping.
            -- Stored whether or not it changed anything: a decision that was
            -- considered and ignored is evidence too, and without it a later
            -- reviewer cannot tell "the model said nothing" from "the model was
            -- never asked".
            CREATE TABLE IF NOT EXISTS ai_observation(
                account_id TEXT NOT NULL,
                day TEXT NOT NULL,
                slot TEXT NOT NULL,
                bias TEXT NOT NULL,
                entry_delta INTEGER NOT NULL,
                weight_delta_bps INTEGER NOT NULL,
                position_delta INTEGER NOT NULL,
                blocked_symbols TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL,
                clamped TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_id, day, slot)
            );
            CREATE TABLE IF NOT EXISTS trade(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                slot TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                fees_cents INTEGER NOT NULL,
                trade_day TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rebalance(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                old_steady_bps INTEGER NOT NULL,
                old_aggressive_bps INTEGER NOT NULL,
                new_steady_bps INTEGER NOT NULL,
                new_aggressive_bps INTEGER NOT NULL,
                moved_cents INTEGER NOT NULL,
                reason TEXT NOT NULL,
                applied_day TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bar(
                symbol TEXT NOT NULL,
                day TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                open_cents INTEGER NOT NULL,
                high_cents INTEGER NOT NULL,
                low_cents INTEGER NOT NULL,
                close_cents INTEGER NOT NULL,
                prev_close_cents INTEGER NOT NULL,
                volume_shares INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                -- 'none' (unadjusted, from the daytime package) or 'qfq'
                -- (forward-adjusted, from the V1 archive). Forward adjustment
                -- rewrites history using future dividends, so anything doing
                -- walk-forward evaluation must refuse 'qfq'.
                adjust TEXT NOT NULL DEFAULT 'none',
                source TEXT NOT NULL DEFAULT 'tdx',
                -- 1 when close_cents came from a real trade; 0 when the row
                -- carries the official reference close for a halted symbol.
                tradable INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(symbol, day)
            );
            CREATE INDEX IF NOT EXISTS bar_by_day ON bar(day);
            "#,
        )?;
        self.ensure_bar_columns()?;
        if existing.is_none() {
            self.conn.execute(
                "INSERT INTO schema_version(version) VALUES(?)",
                [SCHEMA_VERSION],
            )?;
        }
        Ok(())
    }

    /// Add missing bar metadata and classify legacy TDX zero-quote rows once.
    /// Existing explicit tradability is preserved on subsequent opens.
    fn ensure_bar_columns(&self) -> StoreResult<()> {
        let mut have: Vec<String> = Vec::new();
        {
            let mut stmt = self.conn.prepare("PRAGMA table_info(bar)")?;
            let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
            for row in rows {
                have.push(row?);
            }
        }
        let txn = self.conn.unchecked_transaction()?;
        for (name, ddl) in [
            ("adjust", "ALTER TABLE bar ADD COLUMN adjust TEXT NOT NULL DEFAULT 'none'"),
            ("source", "ALTER TABLE bar ADD COLUMN source TEXT NOT NULL DEFAULT 'tdx'"),
            ("tradable", "ALTER TABLE bar ADD COLUMN tradable INTEGER NOT NULL DEFAULT 1"),
        ] {
            if !have.iter().any(|column| column == name) {
                txn.execute(ddl, [])?;
                if name == "tradable" {
                    txn.execute(
                        "UPDATE bar SET tradable=0, \
                         close_cents=CASE WHEN prev_close_cents>0 THEN prev_close_cents ELSE 0 END \
                         WHERE adjust='none' AND source='tdx' \
                         AND volume_shares=0 AND open_cents=0 AND high_cents=0 AND low_cents=0",
                        [],
                    )?;
                }
            }
        }
        txn.commit()?;
        Ok(())
    }

    pub fn schema_version(&self) -> StoreResult<i64> {
        Ok(self.conn.query_row(
            "SELECT version FROM schema_version LIMIT 1",
            [],
            |row| row.get(0),
        )?)
    }

    // ---- whole-market bar table -------------------------------------------

    /// Insert or replace one day of bars in a single transaction.
    ///
    /// Re-running the same day is safe and idempotent: the daytime package can
    /// be re-fetched after a partial write without creating duplicate rows.
    pub fn upsert_bars(&mut self, bars: &[Bar]) -> StoreResult<usize> {
        if bars.is_empty() {
            return Ok(0);
        }
        let txn = self.conn.transaction()?;
        {
            let mut stmt = txn.prepare(
                "INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,\
                 close_cents,prev_close_cents,volume_shares,amount_cents,adjust,source,tradable) \
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(symbol,day) DO UPDATE SET \
                   name=excluded.name, open_cents=excluded.open_cents, \
                   high_cents=excluded.high_cents, low_cents=excluded.low_cents, \
                   close_cents=excluded.close_cents, \
                   prev_close_cents=excluded.prev_close_cents, \
                   volume_shares=excluded.volume_shares, \
                   amount_cents=excluded.amount_cents, \
                   adjust=excluded.adjust, source=excluded.source, \
                   tradable=excluded.tradable",
            )?;
            for bar in bars {
                stmt.execute(params![
                    bar.symbol,
                    bar.day,
                    bar.name,
                    bar.open_cents,
                    bar.high_cents,
                    bar.low_cents,
                    bar.close_cents,
                    bar.prev_close_cents,
                    bar.volume_shares,
                    bar.amount_cents,
                    bar.adjust,
                    bar.source,
                    i64::from(bar.tradable),
                ])?;
            }
        }
        txn.commit()?;
        Ok(bars.len())
    }

    /// Unadjusted closes over a day range, as symbol -> (day, close) rows
    /// oldest first. Feeds the market-regime classifier, which needs a
    /// contiguous window rather than whole-market snapshots.
    pub fn unadjusted_closes_through(
        &self,
        from_day: &str,
        to_day: &str,
    ) -> StoreResult<Vec<(String, String, i64)>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol, day, close_cents FROM bar \
             WHERE day BETWEEN ?1 AND ?2 AND adjust = 'none' \
             AND close_cents > 0 AND tradable = 1 ORDER BY symbol, day",
        )?;
        let rows = stmt.query_map(params![from_day, to_day], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, i64>(2)?))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Classify the broad market as of `day`, using the same unadjusted closes
    /// settlement uses.
    ///
    /// Forward-adjusted rows are excluded deliberately: a regime measured off
    /// rewritten history would describe a market nobody traded.
    pub fn market_regime(&self, day: &str) -> StoreResult<RegimeView> {
        let days = self.bar_days()?;
        let Some(position) = days.iter().position(|candidate| candidate == day) else {
            return Ok(RegimeView::unknown(0));
        };
        let start = days[position.saturating_sub(REGIME_LOOKBACK_DAYS - 1)].clone();
        let rows = self.unadjusted_closes_through(&start, day)?;
        let mut by_symbol: BTreeMap<String, Vec<i64>> = BTreeMap::new();
        for (symbol, _, close) in rows {
            by_symbol.entry(symbol).or_default().push(close);
        }
        Ok(classify_series(&by_symbol))
    }

    /// All bars for one trading day, ordered by symbol.
    pub fn bars_for_day(&self, day: &str) -> StoreResult<Vec<Bar>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol,day,name,open_cents,high_cents,low_cents,close_cents,\
             prev_close_cents,volume_shares,amount_cents,adjust,source,tradable \
             FROM bar WHERE day = ?1 ORDER BY symbol",
        )?;
        let rows = stmt.query_map([day], read_bar)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// One symbol's bars over an inclusive day range, oldest first.
    pub fn bars_for_symbol(
        &self,
        symbol: &str,
        from_day: &str,
        to_day: &str,
    ) -> StoreResult<Vec<Bar>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol,day,name,open_cents,high_cents,low_cents,close_cents,\
             prev_close_cents,volume_shares,amount_cents,adjust,source,tradable \
             FROM bar WHERE symbol = ?1 AND day BETWEEN ?2 AND ?3 ORDER BY day",
        )?;
        let rows = stmt.query_map(params![symbol, from_day, to_day], read_bar)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Unadjusted `(symbol, day, close_cents)` rows for one day.
    ///
    /// This is the settlement feed: forward-adjusted rows are excluded here
    /// rather than at the call site so no caller can forget.
    pub fn unadjusted_closes(&self, day: &str) -> StoreResult<Vec<UnadjustedRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol, day, close_cents, tradable, open_cents, high_cents, \
             low_cents, prev_close_cents, volume_shares FROM bar \
             WHERE day = ?1 AND adjust = 'none' AND close_cents > 0 ORDER BY symbol",
        )?;
        let rows = stmt.query_map([day], |row| {
            Ok(UnadjustedRow {
                symbol: row.get(0)?,
                day: row.get(1)?,
                price_cents: row.get(2)?,
                tradable: row.get(3)?,
                open_cents: row.get(4)?,
                high_cents: row.get(5)?,
                low_cents: row.get(6)?,
                prev_close_cents: row.get(7)?,
                volume_shares: row.get(8)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Unadjusted `(symbol, day, close_cents)` rows across an inclusive range.
    pub fn unadjusted_closes_range(
        &self,
        from_day: &str,
        to_day: &str,
    ) -> StoreResult<Vec<UnadjustedRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol, day, close_cents, tradable, open_cents, high_cents, \
                    low_cents, prev_close_cents, volume_shares FROM bar \
             WHERE day BETWEEN ?1 AND ?2 AND adjust = 'none' AND close_cents > 0 \
             ORDER BY symbol, day",
        )?;
        let rows = stmt.query_map(params![from_day, to_day], |row| {
            Ok(UnadjustedRow {
                symbol: row.get(0)?,
                day: row.get(1)?,
                price_cents: row.get(2)?,
                tradable: row.get(3)?,
                open_cents: row.get(4)?,
                high_cents: row.get(5)?,
                low_cents: row.get(6)?,
                prev_close_cents: row.get(7)?,
                volume_shares: row.get(8)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Build a `PriceSet` over the bar table for a settlement window.
    ///
    /// Returns `None` when the window has no unadjusted rows, so the caller
    /// reports "no data" instead of settling against an empty set.
    pub fn price_set_from_bars(
        &self,
        symbol: &str,
        coverage_start: &str,
        coverage_end: &str,
    ) -> StoreResult<Option<crate::domain::PriceSet>> {
        let rows = self.unadjusted_closes_range(coverage_start, coverage_end)?;
        if rows.is_empty() {
            return Ok(None);
        }
        let rows: Vec<crate::domain::BarRow> = rows
            .into_iter()
            .map(|row| crate::domain::BarRow {
                symbol: row.symbol,
                day: row.day,
                price_cents: row.price_cents,
                tradable: row.tradable,
                open_cents: row.open_cents,
                high_cents: row.high_cents,
                low_cents: row.low_cents,
                prev_close_cents: row.prev_close_cents,
                volume_shares: row.volume_shares,
            })
            .collect();
        Ok(Some(crate::domain::PriceSet::from_bars(
            &rows,
            symbol,
            coverage_start,
            coverage_end,
        )))
    }

    /// Per-day equity for both slots, oldest first.
    ///
    /// Returns `(day, steady_cents, aggressive_cents)` for every day the
    /// account has settled. This is the profit curve -- the single number the
    /// whole system exists to produce, so it gets a first-class query rather
    /// than being reconstructed from trades by the caller.
    pub fn equity_curve(&self, account_id: &str) -> StoreResult<Vec<(String, i64, i64)>> {
        let mut stmt = self.conn.prepare(
            "SELECT day,
                    COALESCE(SUM(CASE WHEN slot='steady' THEN equity_cents END), 0),
                    COALESCE(SUM(CASE WHEN slot='aggressive' THEN equity_cents END), 0)
             FROM slot_equity_day WHERE account_id = ?1
             GROUP BY day ORDER BY day",
        )?;
        let rows = stmt.query_map([account_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Read model only: account, steady and aggressive performance, in that
    /// order. Slot returns remove end-of-day transfers. Optional current marks
    /// support /account valuations after the most recent settled day.
    pub fn account_performance(
        &self,
        spec: &AccountSpec,
        mark: Option<(&str, [i64; 2])>,
    ) -> StoreResult<[Vec<PerformancePoint>; 3]> {
        let mut rows = self.equity_curve(&spec.account_id)?;
        if let Some((day, equities)) = mark {
            match rows.last_mut() {
                Some(last) if last.0 == day => {
                    last.1 = equities[0];
                    last.2 = equities[1];
                }
                Some(last) if last.0.as_str() > day => {
                    // Asking to value the book before a day it has already
                    // settled past is a conflict about the requested day, not
                    // a data gap: the prices exist, the run simply has nothing
                    // to do. Reporting it as a rules error sent callers looking
                    // for missing market data.
                    return Err(StoreError::Conflict(
                        "day_before_last_settled".into(),
                    ));
                }
                _ => rows.push((day.to_string(), equities[0], equities[1])),
            }
        }
        let mut flows = BTreeMap::<String, i64>::new();
        for item in self.rebalances(&spec.account_id)? {
            let signed = if item.to == StrategyId::Steady {
                item.moved_cents
            } else {
                -item.moved_cents
            };
            *flows.entry(item.applied_day).or_default() += signed;
        }
        let mut inputs: [Vec<PerformanceInput>; 3] =
            std::array::from_fn(|_| Vec::with_capacity(rows.len()));
        for (day, steady, aggressive) in rows {
            let flow = flows.get(&day).copied().unwrap_or(0);
            for (index, (equity, net_flow)) in
                [(steady + aggressive, 0), (steady, flow), (aggressive, -flow)]
                    .into_iter()
                    .enumerate()
            {
                inputs[index].push(PerformanceInput {
                    day: day.clone(),
                    equity_cents: equity,
                    net_flow_cents: net_flow,
                });
            }
        }
        let budgets = initial_split(spec.initial_cash_cents)?;
        Ok([
            performance_curve(spec.initial_cash_cents, &inputs[0])?,
            performance_curve(budgets[0].cash_cents, &inputs[1])?,
            performance_curve(budgets[1].cash_cents, &inputs[2])?,
        ])
    }

    /// Every distinct trading day we hold, oldest first.
    pub fn bar_days(&self) -> StoreResult<Vec<String>> {
        let mut stmt = self.conn.prepare("SELECT DISTINCT day FROM bar ORDER BY day")?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// Replay dates must come from usable unadjusted data, never qfq-only days.
    pub fn unadjusted_bar_days(&self) -> StoreResult<Vec<String>> {
        let mut stmt = self.conn.prepare(
            "SELECT DISTINCT day FROM bar WHERE adjust='none' AND close_cents>0 ORDER BY day",
        )?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    /// Number of symbols with a bar on `day`. Used only for reporting.
    pub fn bar_symbol_count(&self, day: &str) -> StoreResult<i64> {
        Ok(self.conn.query_row(
            "SELECT count(*) FROM bar WHERE day = ?1",
            [day],
            |row| row.get(0),
        )?)
    }

    /// Symbols whose close jumped more than `threshold_bps` from their own
    /// `prev_close` on `day`.
    ///
    /// This is not a validation gate -- nothing is rejected or blocked. It is a
    /// display hint: a gap this large is either an ex-dividend/split or bad
    /// data, and either way the K-line should not draw a solid bar across it.
    ///
    /// The caller decides the threshold. A single number does not work across
    /// boards: ChiNext and STAR move up to 20% in a day by rule, so a 11%
    /// threshold flags every limit-up as a corporate action. See
    /// `gap_threshold_bps`.
    pub fn price_gaps(&self, day: &str) -> StoreResult<Vec<Bar>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol,day,name,open_cents,high_cents,low_cents,close_cents,\
             prev_close_cents,volume_shares,amount_cents,adjust,source,tradable \
             FROM bar WHERE day = ?1 AND prev_close_cents > 0 \
             ORDER BY symbol",
        )?;
        let rows = stmt.query_map([day], read_bar)?;
        let mut out = Vec::new();
        for row in rows {
            let bar = row?;
            // Per-symbol threshold: a 20%-limit board closing +13% is an
            // ordinary day, not a corporate action, while a main-board stock
            // closing +13% cannot happen without one.
            // Headroom above the limit: a stock closing exactly at its limit
            // is an ordinary day, one closing *past* it cannot be. 500 bps of
            // margin absorbs tick rounding without hiding real gaps.
            let threshold = daily_limit_bps(&bar.symbol) + 500;
            if (bar.close_cents - bar.prev_close_cents).abs() * 10_000
                / bar.prev_close_cents
                > threshold
            {
                out.push(bar);
            }
        }
        Ok(out)
    }

    pub fn create_account(&mut self, spec: AccountSpec) -> StoreResult<()> {
        let budgets = initial_split(spec.initial_cash_cents)?;
        let txn = self.conn.transaction()?;
        txn.execute(
            "INSERT INTO account(account_id,spec_json,created_at) VALUES(?,?,?)",
            params![spec.account_id, serde_json::to_string(&spec)?, spec.created_at],
        )?;
        for (slot, budget) in spec.slots.iter().zip(budgets.iter()) {
            txn.execute(
                "INSERT INTO slot(account_id,slot,weight_bps,cash_cents) VALUES(?,?,?,?)",
                params![
                    spec.account_id,
                    slot_name(slot.slot),
                    budget.weight_bps,
                    budget.cash_cents
                ],
            )?;
            txn.execute(
                "INSERT INTO slot_day(account_id,slot,settled_day) VALUES(?,?,?)",
                params![spec.account_id, slot_name(slot.slot), ""],
            )?;
        }
        txn.commit()?;
        Ok(())
    }

    pub fn account(&self) -> StoreResult<Option<AccountSpec>> {
        let raw: Option<String> = self
            .conn
            .query_row("SELECT spec_json FROM account LIMIT 1", [], |row| {
                row.get(0)
            })
            .optional()?;
        raw.map(|value| serde_json::from_str(&value).map_err(StoreError::from))
            .transpose()
    }

    pub fn books(&self, account_id: &str) -> StoreResult<[SlotBook; 2]> {
        Ok([
            read_book(&self.conn, account_id, StrategyId::Steady)?,
            read_book(&self.conn, account_id, StrategyId::Aggressive)?,
        ])
    }

    pub fn weights(&self, account_id: &str) -> StoreResult<[i64; 2]> {
        Ok([
            slot_weight(&self.conn, account_id, StrategyId::Steady)?,
            slot_weight(&self.conn, account_id, StrategyId::Aggressive)?,
        ])
    }

    /// Record what the observer advised for one slot on one day.
    ///
    /// Re-running the same day overwrites: the observer is advisory, and the
    /// last word for that day is the one settlement saw. Keeping every
    /// historical attempt would make "what did we act on" ambiguous.
    pub fn record_observation(
        &self,
        account_id: &str,
        day: &str,
        slot: &str,
        adjustment: &crate::domain::AiAdjustment,
        applied: bool,
    ) -> StoreResult<()> {
        let blocked = serde_json::to_string(&adjustment.blocked_symbols)?;
        let clamped = serde_json::to_string(&adjustment.clamped)?;
        self.conn.execute(
            "INSERT INTO ai_observation(account_id,day,slot,bias,entry_delta,             weight_delta_bps,position_delta,blocked_symbols,confidence,reason,             source,model,clamped,applied)              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)              ON CONFLICT(account_id,day,slot) DO UPDATE SET                bias=excluded.bias, entry_delta=excluded.entry_delta,                weight_delta_bps=excluded.weight_delta_bps,                position_delta=excluded.position_delta,                blocked_symbols=excluded.blocked_symbols,                confidence=excluded.confidence, reason=excluded.reason,                source=excluded.source, model=excluded.model,                clamped=excluded.clamped, applied=excluded.applied",
            params![account_id, day, slot, adjustment.bias,
                    adjustment.entry_delta, adjustment.weight_delta_bps,
                    adjustment.position_delta, blocked, adjustment.confidence,
                    adjustment.reason, adjustment.source, adjustment.model,
                    clamped, i64::from(applied)],
        )?;
        Ok(())
    }

    /// The adjustment recorded for a slot on a day, if any.
    ///
    /// `None` means the observer was never asked, not that it said "no change".
    /// Settlement treats both as neutral, but an audit needs the difference.
    pub fn observation_for(
        &self,
        account_id: &str,
        day: &str,
        slot: &str,
    ) -> StoreResult<Option<crate::domain::AiAdjustment>> {
        let row: Option<(String, i64, i64, i64, String, i64, String, String, String, String, i64)> =
            self.conn
                .query_row(
                    "SELECT bias, entry_delta, weight_delta_bps, position_delta,                      blocked_symbols, confidence, reason, source, model, clamped, applied                      FROM ai_observation WHERE account_id=? AND day=? AND slot=?",
                    params![account_id, day, slot],
                    |row| {
                        Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?,
                            row.get(4)?, row.get(5)?, row.get(6)?, row.get(7)?,
                            row.get(8)?, row.get(9)?, row.get(10)?))
                    },
                )
                .optional()?;
        Ok(row.map(|(bias, entry, weight, position, blocked, confidence, reason,
                    source, model, clamped, _applied)| crate::domain::AiAdjustment {
            bias,
            entry_delta: entry,
            weight_delta_bps: weight,
            position_delta: position,
            blocked_symbols: serde_json::from_str(&blocked).unwrap_or_default(),
            confidence,
            reason,
            source,
            model,
            clamped: serde_json::from_str(&clamped).unwrap_or_default(),
        }))
    }

    pub fn last_settled_day(&self, account_id: &str) -> StoreResult<Option<String>> {
        let days = settled_days(&self.conn, account_id)?;
        Ok(days.last().cloned())
    }

    pub fn coverage(&self, set: Option<&PriceSet>, last_settled_day: Option<String>) -> PriceCoverage {
        PriceCoverage {
            price_set_id: set.map(|item| item.price_set_id.clone()),
            market_data_pack_id: set.map(|item| item.market_data_pack_id.clone()),
            coverage_start: set.map(|item| item.coverage_start.clone()),
            coverage_end: set.map(|item| item.coverage_end.clone()),
            days: set.map(PriceSet::days).unwrap_or_default(),
            symbols: set.map(PriceSet::symbols).unwrap_or_default(),
            last_settled_day,
        }
    }

    /// Advance through every reviewed price-set day after the last settled
    /// day. `through` is inclusive and may not exceed the price-set coverage.
    ///
    /// `should_continue` is checked before each committed day. Returning false
    /// stops the batch with `Aborted`; earlier daily transactions remain
    /// durable. Passing `None` keeps the old behavior for offline tests and
    /// examples.
    fn advance_inner(
        &mut self,
        spec: &AccountSpec,
        set: &PriceSet,
        through: &str,
        settle_from: Option<&str>,
        should_continue: Option<&dyn Fn() -> bool>,
        max_days: usize,
    ) -> StoreResult<AdvanceResult> {
        if through.len() != 10
            || through < set.coverage_start.as_str()
            || through > set.coverage_end.as_str()
        {
            return Err(StoreError::Rules(RuleError(
                "day_outside_price_set_coverage".into(),
            )));
        }
        let from_day = self.last_settled_day(&spec.account_id)?;
        // The price set may start well before the day settlement begins: those
        // earlier days are warm-up so the strategies have trailing closes.
        //
        // Two different lower bounds:
        // - `from_day` is the last settled day and is always exclusive
        //   (settling it again would be replaying, not advancing).
        // - `settle_from` is an explicit first day and is inclusive.
        let explicit_floor = settle_from.map(str::to_string);
        let days = set
            .days()
            .into_iter()
            .filter(|day| {
                let after_settled = day.as_str() > from_day.as_deref().unwrap_or("");
                let at_or_after_floor = match &explicit_floor {
                    Some(floor) => day.as_str() >= floor.as_str(),
                    None => true,
                };
                after_settled && at_or_after_floor && day.as_str() <= through
            })
            .collect::<Vec<_>>();
        if days.is_empty() {
            let current = self
                .account()?
                .ok_or_else(|| StoreError::NotFound("account_not_found".into()))?;
            return Ok(AdvanceResult {
                from_day: from_day.clone(),
                through_day: through.into(),
                processed_days: Vec::new(),
                trades: Vec::new(),
                rebalances: Vec::new(),
                account: self.view(&current, Some(set), Some(through))?,
            });
        }

        if set.entries.len() > self.advance_limits.max_entries {
            return Err(StoreError::Aborted(format!(
                "advance_memory_limit:{}>{}",
                set.entries.len(), self.advance_limits.max_entries
            )));
        }
        let deadline = (self.advance_limits.max_seconds > 0)
            .then(|| std::time::Instant::now() + std::time::Duration::from_secs(self.advance_limits.max_seconds));
        let mut all_trades = Vec::new();
        let mut all_rebalances = Vec::new();
        let mut processed = Vec::with_capacity(days.len());
        // Build the lookup index once for the whole advance. Rebuilding it per
        // day would re-walk millions of entries on every iteration.
        let index_set = IndexedPriceSet::new(set);
        let deadline_expired = || deadline.is_some_and(|end| std::time::Instant::now() >= end);
        let cancelled = |predicate: Option<&dyn Fn() -> bool>| {
            predicate.is_some_and(|predicate| !predicate())
        };
        for (number, day) in days.iter().enumerate() {
            if deadline_expired() {
                return Err(StoreError::Aborted(format!(
                    "advance_time_limit:{}s", self.advance_limits.max_seconds
                )));
            }
            if cancelled(should_continue) {
                return Err(StoreError::Aborted("advance_cancelled".into()));
            }
            let run = self.run_day_indexed(spec, &index_set, day)?;
            all_trades.extend(run.trades);
            all_rebalances.extend(run.rebalance);
            processed.push(day.clone());
            let last_in_chunk = max_days != usize::MAX && (number + 1) % max_days == 0;
            if last_in_chunk && number + 1 < days.len() {
                // The caller drops its writer guard here, so reads and other
                // queued writes can run between chunks. The next chunk starts
                // from the durable per-day commit.
                return self.finish_advance(
                    spec, set, through, from_day, processed,
                    all_trades, all_rebalances, false,
                );
            }
        }
        self.finish_advance(
            spec, set, through, from_day, processed,
            all_trades, all_rebalances, true,
        )
    }

    /// Settle every day from the last settled day through `through`.
    ///
    /// This keeps the original contract: one call advances as far as the input
    /// allows. Chunking exists so the HTTP layer can release its writer lock
    /// between batches, but a caller that asks this function to advance 90 days
    /// must get 90 days -- silently stopping at the chunk boundary changed
    /// results, not just latency.
    pub fn advance(
        &mut self,
        spec: &AccountSpec,
        set: &PriceSet,
        through: &str,
        settle_from: Option<&str>,
    ) -> StoreResult<AdvanceResult> {
        self.advance_inner(spec, set, through, settle_from, None, usize::MAX)
    }

    /// Advance at most one chunk, then return what was settled.
    ///
    /// The HTTP handler loops on this, dropping its lock between calls. A
    /// resource/deadline guard stops the loop instead of leaving an orphaned
    /// multi-minute job holding the writer.
    pub fn advance_one_chunk(
        &mut self,
        spec: &AccountSpec,
        set: &PriceSet,
        through: &str,
        settle_from: Option<&str>,
        should_continue: Option<&dyn Fn() -> bool>,
    ) -> StoreResult<AdvanceResult> {
        let chunk = self.advance_limits.chunk_days.max(1);
        self.advance_inner(spec, set, through, settle_from, should_continue, chunk)
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_advance(
        &self,
        _spec: &AccountSpec,
        set: &PriceSet,
        through: &str,
        from_day: Option<String>,
        processed_days: Vec<String>,
        trades: Vec<Trade>,
        rebalances: Vec<Rebalance>,
        completed: bool,
    ) -> StoreResult<AdvanceResult> {
        // Value at the last day actually settled, not at the day the caller
        // asked for: a chunked advance stops early when it hits a limit, and
        // valuing past that point would mark the book on prices it never
        // reached.
        let valuation_day = if completed {
            through.to_string()
        } else {
            processed_days
                .last()
                .cloned()
                .unwrap_or_else(|| through.to_string())
        };
        let current = self
            .account()?
            .ok_or_else(|| StoreError::NotFound("account_not_found".into()))?;
        let account = self.view(&current, Some(set), Some(valuation_day.as_str()))?;
        Ok(AdvanceResult {
            from_day,
            through_day: through.into(),
            processed_days,
            trades,
            rebalances,
            account,
        })
    }

    /// Signals for one slot on one day, over a bounded lookback window.
    ///
    /// Two things keep this affordable on a 5.3k-symbol universe:
    ///
    /// 1. **Bounded history.** The strategies need at most 61 closes (a 60-day
    ///    average plus the latest bar). Asking for the whole series would drag
    ///    ~930 rows per symbol through the moving-average code for no gain.
    ///    `SIGNAL_LOOKBACK_DAYS` is the window actually needed.
    /// 2. **Candidate filter.** A symbol is only worth a signal if it has a
    ///    close on `day` (otherwise it cannot be traded today). Symbols already
    ///    held are always included so exits and adds still see them.
    ///
    /// Everything else -- listing dates, delistings, suspensions -- shows up
    /// as a short or missing series, which `signal` already handles by
    /// returning `None`.
    /// What the slots scored on one day, for the AI observer to read.
    ///
    /// Read-only and bounded: the top `limit` names per slot, with the score,
    /// the reason, and the three session-shape numbers. Deliberately no
    /// forward-looking field -- the observer must see exactly what the engine
    /// saw at decision time, or its advice is hindsight dressed as judgement.
    pub fn observation_candidates(
        &self,
        set: &PriceSet,
        day: &str,
        limit: usize,
    ) -> StoreResult<Vec<crate::domain::ScoredCandidate>> {
        let index = IndexedPriceSet::new(set);
        let closes = index.tradable_closes_on(day);
        if closes.is_empty() {
            return Ok(Vec::new());
        }
        // Only names that can actually produce a signal today: a close exists.
        let candidates: std::collections::BTreeSet<&str> =
            closes.keys().map(String::as_str).collect();
        let mut rows = Vec::new();
        for slot in [StrategyId::Steady, StrategyId::Aggressive] {
            let mut scored = self.signals_for_day(
                slot, &index, day, &closes, std::iter::empty(), index.adjustments(),
            )?;
            scored.sort_by(|left, right| {
                right.1.score.cmp(&left.1.score).then_with(|| left.0.cmp(&right.0))
            });
            for (symbol, score) in scored.into_iter().take(limit) {
                let shape = index.latest_bar(&symbol, day).and_then(|bar| crate::domain::KBars::from_bar(&bar));
                rows.push(crate::domain::ScoredCandidate {
                    slot: match slot {
                        StrategyId::Steady => "steady".into(),
                        StrategyId::Aggressive => "aggressive".into(),
                    },
                    symbol,
                    score: score.score,
                    reason: score.reason_with_history(),
                    klow_bps: shape.map(|s| s.klow_bps),
                    klen_bps: shape.map(|s| s.klen_bps),
                    kmid_bps: shape.map(|s| s.kmid_bps),
                });
            }
        }
        let _ = candidates;
        Ok(rows)
    }

    /// The symbols in the worst `klow_cut_bps` of the day's lower-wick tail.
    ///
    /// Lower wick in bps of the session open: `(min(open, close) - low) / open`.
    /// Computed only from the day's own bar, so it cannot see the future.
    ///
    /// A symbol whose bar carries no usable shape is *not* excluded: the filter
    /// is a claim about measured wicks, and a missing open/low is an absence of
    /// evidence rather than evidence of a long wick. Excluding those would make
    /// the filter quietly drop every name whose feed lacks OHLC.
    fn worst_lower_wick_tail(
        &self,
        set: &IndexedPriceSet<'_>,
        day: &str,
        candidates: &BTreeSet<&str>,
        held: &BTreeSet<&str>,
    ) -> BTreeSet<String> {
        let mut scored: Vec<(&str, i64)> = Vec::with_capacity(candidates.len());
        for symbol in candidates {
            // Never consider a holding for exclusion: the caller exempts it,
            // but leaving it out of the ranking as well keeps the cut fraction
            // honest about how many *tradeable* names it is removing.
            if held.contains(symbol) {
                continue;
            }
            let Some(bar) = set.latest_bar(symbol, day) else {
                continue;
            };
            if !bar.has_session_shape() || bar.open_cents <= 0 {
                continue;
            }
            let lower = bar.open_cents.min(bar.close_cents) - bar.low_cents;
            let wick_bps = lower * 10_000 / bar.open_cents;
            scored.push((symbol, wick_bps));
        }
        let cut = (scored.len() as i64 * self.klow_cut_bps / 10_000) as usize;
        if cut == 0 || cut >= scored.len() {
            return BTreeSet::new();
        }
        // Sort ascending by wick, take the longest tail.
        scored.sort_by(|left, right| left.1.cmp(&right.1).then_with(|| left.0.cmp(right.0)));
        scored[scored.len() - cut..]
            .iter()
            .map(|(symbol, _)| symbol.to_string())
            .collect()
    }

    fn signals_for_day<'a, 'set>(
        &self,
        slot: StrategyId,
        set: &IndexedPriceSet<'set>,
        day: &str,
        closes: &'a BTreeMap<String, i64>,
        held: impl Iterator<Item = &'a str>,
        adjustment: &AdjustmentBook,
    ) -> StoreResult<Vec<(String, SignalScore)>> {
        // Held symbols are collected first: the filter has to exempt them,
        // because a position dropped from the signal list would never see its
        // exit rule -- a far worse failure than buying one extra long-wick
        // name. They also join the candidate set, so adds and exits are scored.
        let held_set: BTreeSet<&'a str> = held.collect();
        let mut candidates: BTreeSet<&'a str> = closes.keys().map(String::as_str).collect();
        candidates.extend(held_set.iter().copied());

        let mut signals = Vec::with_capacity(candidates.len());
        let excluded = if self.klow_filter {
            self.worst_lower_wick_tail(set, day, &candidates, &held_set)
        } else {
            BTreeSet::new()
        };
        for symbol in candidates {
            if excluded.contains(symbol) && !held_set.contains(symbol) {
                continue;
            }
            let mut bars = set.bars_for(symbol, day);
            if bars.len() > SIGNAL_LOOKBACK_DAYS {
                bars.drain(..bars.len() - SIGNAL_LOOKBACK_DAYS);
            }
            // Factors are applied inside scoring, never to the stored price:
            // the fill below uses `closes`, which stays unadjusted.
            signals.push((
                symbol.to_string(),
                score_signal_adjusted(slot, &bars, adjustment.for_symbol(symbol)),
            ));
        }
        Ok(signals)
    }

    /// Process exactly one day. Re-running a settled day returns its stored
    /// fills and rebalance without creating another trade.
    pub fn run_day(&mut self, spec: &AccountSpec, set: &PriceSet, day: &str) -> StoreResult<DayRun> {
        // Build the index once per day rather than per lookup. `advance` calls
        // this in a loop, so the index is rebuilt per day -- still far cheaper
        // than the linear scans it replaces, and it keeps `run_day` usable on
        // its own for a single-day settle.
        let index = IndexedPriceSet::new(set);
        self.run_day_indexed(spec, &index, day)
    }

    /// The same atomic day settlement with a caller-owned price index. Offline
    /// comparisons can reuse one input index across their isolated books.
    pub fn run_day_indexed(
        &mut self,
        spec: &AccountSpec,
        index_set: &IndexedPriceSet<'_>,
        day: &str,
    ) -> StoreResult<DayRun> {
        let set = index_set.set();
        validate_day(set, day)?;
        if let Some(last) = self.last_settled_day(&spec.account_id)?
            && day < last.as_str()
        {
            return Err(StoreError::Conflict("day_before_last_settled".into()));
        }
        if day_was_run(&self.conn, &spec.account_id, day)? {
            return Ok(DayRun {
                day: day.into(),
                trades: trades_for_day(&self.conn, &spec.account_id, day)?,
                rebalance: rebalance_for_day(&self.conn, &spec.account_id, day)?,
                already_settled: true,
            });
        }

        let mut books = self.books(&spec.account_id)?;
        let previous_day = self.last_settled_day(&spec.account_id)?;
        if previous_day.as_deref() != Some(day) {
            settle_overnight(&mut books[0])?;
            settle_overnight(&mut books[1])?;
        }
        let weights = self.weights(&spec.account_id)?;
        // Same-close book: the day's own close decides and fills. Next-session
        // model: the previous session's close decides and today's close fills,
        // so the price being filled at was unknown when the order was formed.
        // Warm-up days have no prior session, so the decision window stays on
        // `day` and the first settled day simply opens nothing new.
        let decision_day: &str = if self.next_session_fills {
            previous_day
                .as_deref()
                .filter(|prior| prior.len() == 10 && *prior < day)
                .unwrap_or(day)
        } else {
            day
        };
        // Stop trigger price: the close the decision was made from. In the
        // same-close book that is today's own close; in the next-session book
        // it is the previous session's. Never an empty map, which would
        // silently disable the hard stop.
        let trigger_closes = if self.next_session_fills && decision_day != day {
            closes_for_indexed(index_set, decision_day)
        } else {
            closes_for_indexed(index_set, day)
        };
        // The regime is a property of the day, not of a slot, and it costs one
        // pass over the index. Computing it once here keeps the two slots
        // looking at the same tape.
        let regime = regime_for_day(index_set, decision_day);
        let mut planned = Vec::new();
        for (index, slot) in [StrategyId::Steady, StrategyId::Aggressive]
            .into_iter()
            .enumerate()
        {
            let book = &books[index];
            let closes = closes_for_indexed(index_set, day);
            // Only symbols that can actually produce a signal need their
            // history replayed: those with a position to manage, plus those
            // with a close today. Computing a signal for a symbol that has no
            // bar on `day` cannot change the outcome.
            let signals = self.signals_for_day(
                slot,
                index_set,
                decision_day,
                &closes,
                book.positions.iter().map(|position| position.symbol.as_str()),
                index_set.adjustments(),
            )?;
            let budget = self.slot_equity(&spec.account_id, slot)?;
            let mut policy = if self.regime_filter {
                SlotPolicy::for_strategy_in(slot, regime.regime)
            } else {
                SlotPolicy::for_strategy(slot)
            };
            // The observer may narrow this slot for today. It is applied after
            // the regime policy so the two compose instead of one overwriting
            // the other, and it is bounded inside `apply_to_policy`: an
            // advisory input can raise the bar but cannot invert the strategy.
            if self.ai_policy {
                if let Some(advice) = self.observation_for(&spec.account_id, day, slot_name(slot))? {
                    apply_to_policy(&mut policy, &advice);
                }
            }
            let valuations: BTreeMap<String, i64> = book
                .positions
                .iter()
                .map(|position| {
                    index_set
                        .valuation(&position.symbol, day)
                        .map(|price| (position.symbol.clone(), price))
                        .ok_or_else(|| StoreError::Rules(RuleError("missing_close_price".into())))
                })
                .collect::<StoreResult<_>>()?;
            let orders = orders_for_day_in_split(
                slot,
                &policy,
                &book.positions,
                &signals,
                budget,
                book.cash_cents,
                &closes,
                &trigger_closes,
                &valuations,
                &book.sellable,
            );
            for order in orders {
                let price = closes
                    .get(&order.symbol)
                    .copied()
                    .ok_or_else(|| StoreError::Rules(RuleError("missing_close_price".into())))?;
                // A price-limit seal closes one side of the book. Having a
                // tradable close is not the same as being able to fill: a
                // single-price limit-down session has a price and no bid.
                let open = if order.side == "buy" {
                    index_set.can_buy(&order.symbol, day)
                } else {
                    index_set.can_sell(&order.symbol, day)
                };
                if !open {
                    continue;
                }
                planned.push((slot, order, price));
            }
            let _ = value_book_indexed(book, index_set, day)?;
        }

        for (slot, order, price) in &planned {
            let index = slot_index(*slot);
            let amount = order.quantity * price;
            let fees = fees_cents(&order.side, amount);
            match order.side.as_str() {
                "buy" => apply_buy(&mut books[index], &order.symbol, order.quantity, *price, fees, day)?,
                "sell" => apply_sell(&mut books[index], &order.symbol, order.quantity, *price, fees)?,
                _ => return Err(StoreError::Rules(RuleError("unsupported_side".into()))),
            }
        }

        let mut equities = [
            value_book(&books[0], set, day)?,
            value_book(&books[1], set, day)?,
        ];
        let rebalance = self.proposed_rebalance(
            &spec.account_id,
            day,
            &weights,
            &equities,
            &books,
        )?;
        if let Some(proposal) = &rebalance {
            let from = slot_index(proposal.from);
            let to = slot_index(proposal.to);
            books[from].cash_cents -= proposal.moved_cents;
            books[to].cash_cents += proposal.moved_cents;
            equities = [
                value_book(&books[0], set, day)?,
                value_book(&books[1], set, day)?,
            ];
        }

        let txn = self.conn.transaction()?;
        for (index, slot) in [StrategyId::Steady, StrategyId::Aggressive]
            .into_iter()
            .enumerate()
        {
            write_book(&txn, &spec.account_id, slot, &books[index])?;
            txn.execute(
                "UPDATE slot_day SET settled_day=? WHERE account_id=? AND slot=? AND settled_day<>?",
                params![day, spec.account_id, slot_name(slot), day],
            )?;
            txn.execute(
                "INSERT INTO slot_equity_day(account_id,slot,day,equity_cents) VALUES(?,?,?,?)",
                params![spec.account_id, slot_name(slot), day, equities[index]],
            )?;
        }
        let mut trades = Vec::new();
        for (slot, order, price) in planned {
            let amount = order.quantity * price;
            let fees = fees_cents(&order.side, amount);
            txn.execute(
                "INSERT INTO trade(account_id,slot,symbol,side,quantity,price_cents,fees_cents,trade_day,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                params![
                    spec.account_id,
                    slot_name(slot),
                    order.symbol,
                    order.side,
                    order.quantity,
                    price,
                    fees,
                    day,
                    order.reason
                ],
            )?;
            trades.push(Trade {
                id: txn.last_insert_rowid(),
                slot,
                symbol: order.symbol,
                side: order.side,
                quantity: order.quantity,
                price_cents: price,
                fees_cents: fees,
                trade_day: day.into(),
                reason: order.reason,
            });
        }
        let applied = match rebalance {
            Some(item) => Some(insert_rebalance(&txn, &spec.account_id, item)?),
            None => None,
        };
        if let Some(applied) = &applied {
            let mut updated = spec.clone();
            updated.slots = [
                SlotSpec {
                    slot: StrategyId::Steady,
                    weight_bps: applied.new_steady_bps,
                },
                SlotSpec {
                    slot: StrategyId::Aggressive,
                    weight_bps: applied.new_aggressive_bps,
                },
            ];
            txn.execute(
                "UPDATE account SET spec_json=? WHERE account_id=?",
                params![serde_json::to_string(&updated)?, spec.account_id],
            )?;
        }
        txn.execute(
            "INSERT INTO day_run(account_id,day) VALUES(?,?)",
            params![spec.account_id, day],
        )?;
        txn.commit()?;
        Ok(DayRun {
            day: day.into(),
            trades,
            rebalance: applied,
            already_settled: false,
        })
    }

    fn proposed_rebalance(
        &self,
        account_id: &str,
        day: &str,
        weights: &[i64; 2],
        equities: &[i64; 2],
        books: &[SlotBook; 2],
    ) -> StoreResult<Option<Rebalance>> {
        // This day has not been inserted into `slot_equity_day` yet: the
        // caller supplies its freshly valued equities below. Include today in
        // the history so the first eligible day can actually be evaluated.
        let mut days = settled_days(&self.conn, account_id)?;
        if !days.iter().any(|item| item == day) {
            days.push(day.into());
        }
        let Some(position) = days.iter().position(|item| item == day) else {
            return Ok(None);
        };
        if position < 2 {
            return Ok(None);
        }
        if let Some(last) = rebalances(&self.conn, account_id)?.first() {
            let since = days
                .iter()
                .filter(|item| item.as_str() > last.applied_day.as_str() && item.as_str() <= day)
                .count();
            if since < 2 {
                return Ok(None);
            }
        }
        let last_applied_day = rebalances(&self.conn, account_id)?
            .first()
            .map(|item| item.applied_day.clone());
        let steady_history = series_since_with_current(
            &self.conn,
            account_id,
            StrategyId::Steady,
            day,
            equities[0],
            last_applied_day.as_deref(),
        )?;
        let aggressive_history = series_since_with_current(
            &self.conn,
            account_id,
            StrategyId::Aggressive,
            day,
            equities[1],
            last_applied_day.as_deref(),
        )?;
        let steady_bps = return_bps(&steady_history, equities[0]);
        let aggressive_bps = return_bps(&aggressive_history, equities[1]);
        let steady_drawdown = drawdown_bps(&steady_history, *steady_history.first().unwrap_or(&1));
        let aggressive_drawdown =
            drawdown_bps(&aggressive_history, *aggressive_history.first().unwrap_or(&1));
        let proposal = propose(
            weights[0],
            score(steady_bps, steady_drawdown),
            score(aggressive_bps, aggressive_drawdown),
        )?;
        if proposal.steady_bps == weights[0] {
            return Ok(None);
        }
        let delta = proposal.steady_bps - weights[0];
        // Move the budget difference implied by the old and new weight. This
        // is an allocation of free cash, not a transfer of either strategy's
        // positions; the UI must not call it a change in marked equity.
        let total = equities[0] + equities[1];
        let old_target = total * weights[0] / 10_000;
        let new_target = total * proposal.steady_bps / 10_000;
        let moved = (new_target - old_target).unsigned_abs() as i64;
        let (from, to) = if delta > 0 {
            (StrategyId::Aggressive, StrategyId::Steady)
        } else {
            (StrategyId::Steady, StrategyId::Aggressive)
        };
        // A rebalance only moves free cash. If the source slot has already
        // invested it, keep the current weights instead of failing the whole
        // trading day or creating negative cash.
        if moved > books[slot_index(from)].cash_cents {
            return Ok(None);
        }
        Ok(Some(Rebalance {
            id: 0,
            old_steady_bps: weights[0],
            old_aggressive_bps: weights[1],
            new_steady_bps: proposal.steady_bps,
            new_aggressive_bps: proposal.aggressive_bps,
            moved_cents: moved,
            from,
            to,
            reason: proposal.reason,
            applied_day: day.into(),
        }))
    }

    pub fn trades(&self, account_id: &str, limit: i64) -> StoreResult<Vec<Trade>> {
        let mut statement = self.conn.prepare(
            "SELECT id,slot,symbol,side,quantity,price_cents,fees_cents,trade_day,reason FROM trade WHERE account_id=? ORDER BY id DESC LIMIT ?",
        )?;
        let rows = statement.query_map(params![account_id, limit.clamp(1, 500)], |row| {
            let slot: String = row.get(1)?;
            Ok(Trade {
                id: row.get(0)?,
                slot: parse_slot(&slot)?,
                symbol: row.get(2)?,
                side: row.get(3)?,
                quantity: row.get(4)?,
                price_cents: row.get(5)?,
                fees_cents: row.get(6)?,
                trade_day: row.get(7)?,
                reason: row.get(8)?,
            })
        })?;
        let mut trades = Vec::new();
        for row in rows {
            trades.push(row?);
        }
        Ok(trades)
    }

    pub fn rebalances(&self, account_id: &str) -> StoreResult<Vec<Rebalance>> {
        rebalances(&self.conn, account_id)
    }

    /// Current risk budget: the latest settled equity, otherwise the slot's
    /// share of the original principal.
    pub fn slot_equity(&self, account_id: &str, slot: StrategyId) -> StoreResult<i64> {
        let latest: Option<i64> = self
            .conn
            .query_row(
                "SELECT equity_cents FROM slot_equity_day WHERE account_id=? AND slot=? ORDER BY day DESC LIMIT 1",
                params![account_id, slot_name(slot)],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(value) = latest {
            return Ok(value.max(0));
        }
        let spec = self
            .account()?
            .ok_or_else(|| StoreError::NotFound("account_not_found".into()))?;
        if spec.account_id != account_id {
            return Err(StoreError::NotFound("account_not_found".into()));
        }
        let weight = spec
            .slots
            .iter()
            .find(|item| item.slot == slot)
            .map(|item| item.weight_bps)
            .ok_or_else(|| StoreError::NotFound("slot_not_found".into()))?;
        Ok(spec.initial_cash_cents * weight / 10_000)
    }

    pub fn view(
        &self,
        spec: &AccountSpec,
        set: Option<&PriceSet>,
        day: Option<&str>,
    ) -> StoreResult<AccountView> {
        let books = self.books(&spec.account_id)?;
        let weights = self.weights(&spec.account_id)?;
        let prices = set.zip(day);
        let market_values = match prices.as_ref() {
            Some((set, day)) => [
                Some(crate::domain::market_value_from_set(&books[0], set, day)?),
                Some(crate::domain::market_value_from_set(&books[1], set, day)?),
            ],
            None => [None, None],
        };
        let mark = day.zip(market_values[0].zip(market_values[1])).map(|(day, (steady, aggressive))| {
            (day, [books[0].cash_cents + steady, books[1].cash_cents + aggressive])
        });
        let performance = self.account_performance(spec, mark)?;
        let mut slots = Vec::new();
        let mut positions = Vec::new();
        for (index, book) in books.iter().enumerate() {
            let market_value = market_values[index];
            let last = performance[index + 1].last();
            let current_performance = mark.and(last);
            slots.push(SlotView {
                slot: book.slot,
                name: book.slot.name().into(),
                description: book.slot.description().into(),
                weight_bps: weights[index],
                cash_cents: book.cash_cents,
                market_value_cents: market_value,
                equity_cents: market_value.map(|value| book.cash_cents + value),
                profit_cents: current_performance.map(|point| point.profit_cents),
                return_bps: current_performance.map(|point| point.return_bps),
                drawdown_bps: last.map(|point| point.max_drawdown_bps),
            });
            for position in &book.positions {
                let close = prices
                    .as_ref()
                    .and_then(|(set, day)| set.valuation(&position.symbol, day));
                positions.push(PositionView {
                    slot: book.slot,
                    symbol: position.symbol.clone(),
                    quantity: position.quantity,
                    sellable_quantity: book
                        .sellable
                        .iter()
                        .find(|item| item.symbol == position.symbol)
                        .map(|item| item.quantity),
                    average_cost_cents: position.average_cost_cents,
                    close_cents: close,
                    market_value_cents: close.map(|price| price * position.quantity),
                });
            }
        }
        let cash_cents = books.iter().map(|book| book.cash_cents).sum();
        let equity_cents = if slots.iter().all(|slot| slot.equity_cents.is_some()) {
            Some(slots.iter().filter_map(|slot| slot.equity_cents).sum())
        } else {
            None
        };
        Ok(AccountView {
            account_id: spec.account_id.clone(),
            initial_cash_cents: spec.initial_cash_cents,
            cash_cents,
            equity_cents,
            profit_cents: equity_cents.map(|value| value - spec.initial_cash_cents),
            return_bps: equity_cents
                .map(|value| (value - spec.initial_cash_cents) * 10_000 / spec.initial_cash_cents.max(1)),
            as_of_day: day.map(str::to_string),
            created_at: spec.created_at.clone(),
            slots,
            positions,
            trades: self.trades(&spec.account_id, 100)?,
            rebalances: self.rebalances(&spec.account_id)?,
            price_set_id: set.map(|item| item.price_set_id.clone()),
            price_set_coverage_end: set.map(|item| item.coverage_end.clone()),
            last_settled_day: self.last_settled_day(&spec.account_id)?,
            warnings: match set {
                None => vec!["no_reviewed_price_set".into()],
                Some(_) if day.is_none() => vec!["no_reviewed_close_for_valuation".into()],
                Some(_) => Vec::new(),
            },
        })
    }

}

fn read_book(conn: &Connection, account_id: &str, slot: StrategyId) -> StoreResult<SlotBook> {
    let cash_cents: i64 = conn.query_row(
        "SELECT cash_cents FROM slot WHERE account_id=? AND slot=?",
        params![account_id, slot_name(slot)],
        |row| row.get(0),
    )?;
    let mut positions = Vec::new();
    let mut statement = conn.prepare(
        "SELECT symbol,quantity,average_cost_cents,acquired_day FROM position WHERE account_id=? AND slot=? ORDER BY symbol",
    )?;
    let rows = statement.query_map(params![account_id, slot_name(slot)], |row| {
        Ok(crate::domain::Position {
            symbol: row.get(0)?,
            quantity: row.get(1)?,
            average_cost_cents: row.get(2)?,
            acquired_day: row.get(3)?,
        })
    })?;
    for row in rows {
        positions.push(row?);
    }
    let mut sellable = Vec::new();
    let mut statement = conn.prepare(
        "SELECT symbol,quantity FROM sellable WHERE account_id=? AND slot=? ORDER BY symbol",
    )?;
    let rows = statement.query_map(params![account_id, slot_name(slot)], |row| {
        Ok(crate::domain::Sellable {
            symbol: row.get(0)?,
            quantity: row.get(1)?,
        })
    })?;
    for row in rows {
        sellable.push(row?);
    }
    Ok(SlotBook {
        slot,
        cash_cents,
        positions,
        sellable,
    })
}

fn write_book(
    txn: &Transaction<'_>,
    account_id: &str,
    slot: StrategyId,
    book: &SlotBook,
) -> StoreResult<()> {
    txn.execute(
        "UPDATE slot SET cash_cents=? WHERE account_id=? AND slot=?",
        params![book.cash_cents, account_id, slot_name(slot)],
    )?;
    txn.execute(
        "DELETE FROM position WHERE account_id=? AND slot=?",
        params![account_id, slot_name(slot)],
    )?;
    for position in &book.positions {
        txn.execute(
            "INSERT INTO position(account_id,slot,symbol,quantity,average_cost_cents,acquired_day) VALUES(?,?,?,?,?,?)",
            params![
                account_id,
                slot_name(slot),
                position.symbol,
                position.quantity,
                position.average_cost_cents,
                position.acquired_day
            ],
        )?;
    }
    txn.execute(
        "DELETE FROM sellable WHERE account_id=? AND slot=?",
        params![account_id, slot_name(slot)],
    )?;
    for item in &book.sellable {
        txn.execute(
            "INSERT INTO sellable(account_id,slot,symbol,quantity) VALUES(?,?,?,?)",
            params![account_id, slot_name(slot), item.symbol, item.quantity],
        )?;
    }
    Ok(())
}

fn insert_rebalance(
    txn: &Transaction<'_>,
    account_id: &str,
    proposal: Rebalance,
) -> StoreResult<Rebalance> {
    let aggressive = 10_000 - proposal.new_steady_bps;
    txn.execute(
        "UPDATE slot SET weight_bps=? WHERE account_id=? AND slot=?",
        params![proposal.new_steady_bps, account_id, slot_name(StrategyId::Steady)],
    )?;
    txn.execute(
        "UPDATE slot SET weight_bps=? WHERE account_id=? AND slot=?",
        params![aggressive, account_id, slot_name(StrategyId::Aggressive)],
    )?;
    txn.execute(
        "INSERT INTO rebalance(account_id,old_steady_bps,old_aggressive_bps,new_steady_bps,new_aggressive_bps,moved_cents,reason,applied_day) VALUES(?,?,?,?,?,?,?,?)",
        params![
            account_id,
            proposal.old_steady_bps,
            proposal.old_aggressive_bps,
            proposal.new_steady_bps,
            aggressive,
            proposal.moved_cents,
            proposal.reason,
            proposal.applied_day
        ],
    )?;
    Ok(Rebalance {
        id: txn.last_insert_rowid(),
        ..proposal
    })
}

fn validate_day(set: &PriceSet, day: &str) -> StoreResult<()> {
    if day.len() != 10 || day < set.coverage_start.as_str() || day > set.coverage_end.as_str() {
        return Err(StoreError::Rules(RuleError("day_outside_price_set_coverage".into())));
    }
    if !set.contains_day(day) {
        return Err(StoreError::Rules(RuleError("no_reviewed_close_for_day".into())));
    }
    Ok(())
}

/// One day's tradable closes, served from the day index instead of a full scan.
///
/// A whole-market set is ~4M entries; rebuilding a day's close map by scanning
/// all of them, once per trading day settled, is the difference between a
/// settlement pass that finishes and one that does not.
fn closes_for_indexed(index: &IndexedPriceSet<'_>, day: &str) -> BTreeMap<String, i64> {
    index.tradable_closes_on(day)
}

/// Classify the broad market as of `day`, from the same snapshot the
/// strategies settle against.
///
/// The lookback is bounded by `REGIME_LOOKBACK_DAYS`. Both moving averages are
/// computed from trailing means, so history older than the slow window cannot
/// change the answer -- but the window is twice the slow average so the index
/// has enough points to average over even when a slice of the market is
/// missing on some days.
fn regime_for_day(index: &IndexedPriceSet<'_>, day: &str) -> RegimeView {
    let by_symbol = index.recent_closes(day, REGIME_LOOKBACK_DAYS);
    classify_series(&by_symbol)
}

fn value_book_indexed(
    book: &SlotBook,
    index: &IndexedPriceSet<'_>,
    day: &str,
) -> StoreResult<i64> {
    let mut market = 0i64;
    for position in &book.positions {
        let price = index
            .valuation(&position.symbol, day)
            .ok_or_else(|| StoreError::Rules(RuleError("missing_close_price".into())))?;
        market = market
            .checked_add(price * position.quantity)
            .ok_or_else(|| StoreError::Rules(RuleError("amount_overflow".into())))?;
    }
    book.cash_cents
        .checked_add(market)
        .ok_or_else(|| StoreError::Rules(RuleError("amount_overflow".into())))
}

fn value_book(book: &SlotBook, set: &PriceSet, day: &str) -> StoreResult<i64> {
    let mut market = 0i64;
    for position in &book.positions {
        let price = set
            .valuation(&position.symbol, day)
            .ok_or_else(|| StoreError::Rules(RuleError("missing_close_price".into())))?;
        market = market
            .checked_add(price * position.quantity)
            .ok_or_else(|| StoreError::Rules(RuleError("amount_overflow".into())))?;
    }
    book.cash_cents
        .checked_add(market)
        .ok_or_else(|| StoreError::Rules(RuleError("amount_overflow".into())))
}

fn day_was_run(conn: &Connection, account_id: &str, day: &str) -> StoreResult<bool> {
    let count: i64 = conn.query_row(
        "SELECT count(*) FROM day_run WHERE account_id=? AND day=?",
        params![account_id, day],
        |row| row.get(0),
    )?;
    Ok(count > 0)
}

fn trades_for_day(conn: &Connection, account_id: &str, day: &str) -> StoreResult<Vec<Trade>> {
    let mut statement = conn.prepare(
        "SELECT id,slot,symbol,side,quantity,price_cents,fees_cents,trade_day,reason FROM trade WHERE account_id=? AND trade_day=? ORDER BY id",
    )?;
    let rows = statement.query_map(params![account_id, day], |row| {
        let slot: String = row.get(1)?;
        Ok(Trade {
            id: row.get(0)?,
            slot: parse_slot(&slot)?,
            symbol: row.get(2)?,
            side: row.get(3)?,
            quantity: row.get(4)?,
            price_cents: row.get(5)?,
            fees_cents: row.get(6)?,
            trade_day: row.get(7)?,
            reason: row.get(8)?,
        })
    })?;
    let mut trades = Vec::new();
    for row in rows {
        trades.push(row?);
    }
    Ok(trades)
}

fn rebalance_for_day(
    conn: &Connection,
    account_id: &str,
    day: &str,
) -> StoreResult<Option<Rebalance>> {
    Ok(rebalances(conn, account_id)?
        .into_iter()
        .find(|item| item.applied_day == day))
}

fn rebalances(conn: &Connection, account_id: &str) -> StoreResult<Vec<Rebalance>> {
    let mut statement = conn.prepare(
        "SELECT id,old_steady_bps,old_aggressive_bps,new_steady_bps,new_aggressive_bps,moved_cents,reason,applied_day FROM rebalance WHERE account_id=? ORDER BY id DESC",
    )?;
    let rows = statement.query_map([account_id], |row| {
        let old_steady_bps = row.get(1)?;
        let new_steady_bps = row.get(3)?;
        Ok(Rebalance {
            id: row.get(0)?,
            old_steady_bps,
            old_aggressive_bps: row.get(2)?,
            new_steady_bps,
            new_aggressive_bps: row.get(4)?,
            moved_cents: row.get(5)?,
            from: if new_steady_bps > old_steady_bps {
                StrategyId::Aggressive
            } else {
                StrategyId::Steady
            },
            to: if new_steady_bps > old_steady_bps {
                StrategyId::Steady
            } else {
                StrategyId::Aggressive
            },
            reason: row.get(6)?,
            applied_day: row.get(7)?,
        })
    })?;
    let mut items = Vec::new();
    for row in rows {
        items.push(row?);
    }
    Ok(items)
}

fn slot_weight(conn: &Connection, account_id: &str, slot: StrategyId) -> StoreResult<i64> {
    Ok(conn.query_row(
        "SELECT weight_bps FROM slot WHERE account_id=? AND slot=?",
        params![account_id, slot_name(slot)],
        |row| row.get(0),
    )?)
}

fn settled_days(conn: &Connection, account_id: &str) -> StoreResult<Vec<String>> {
    let mut statement = conn.prepare(
        "SELECT DISTINCT day FROM slot_equity_day WHERE account_id=? ORDER BY day",
    )?;
    let rows = statement.query_map([account_id], |row| row.get(0))?;
    let mut days = Vec::new();
    for row in rows {
        days.push(row?);
    }
    Ok(days)
}

fn series_until(
    conn: &Connection,
    account_id: &str,
    slot: StrategyId,
    day: &str,
) -> StoreResult<Vec<i64>> {
    let mut statement = conn.prepare(
        "SELECT equity_cents FROM slot_equity_day WHERE account_id=? AND slot=? AND day<=? ORDER BY day",
    )?;
    let rows = statement.query_map(params![account_id, slot_name(slot), day], |row| row.get(0))?;
    let mut values = Vec::new();
    for row in rows {
        values.push(row?);
    }
    Ok(values)
}

fn series_since_with_current(
    conn: &Connection,
    account_id: &str,
    slot: StrategyId,
    day: &str,
    current: i64,
    exclusive_start: Option<&str>,
) -> StoreResult<Vec<i64>> {
    let mut values = series_until(conn, account_id, slot, day)?;
    if let Some(start) = exclusive_start {
        let mut statement = conn.prepare(
            "SELECT equity_cents FROM slot_equity_day WHERE account_id=? AND slot=? AND day>? AND day<? ORDER BY day",
        )?;
        let rows = statement.query_map(
            params![account_id, slot_name(slot), start, day],
            |row| row.get(0),
        )?;
        values.clear();
        for row in rows {
            values.push(row?);
        }
    }
    values.push(current);
    Ok(values)
}

fn return_bps(points: &[i64], latest: i64) -> i64 {
    let base = points.first().copied().unwrap_or(latest).max(1);
    (latest - base) * 10_000 / base
}

/// One day's OHLCV row in the whole-market bar table.
///
/// `prev_close_cents` comes straight from the daytime package and is the only
/// cheap way to tell a real -10% limit-down from an ex-dividend gap. Without
/// it a 10-for-5 dividend looks identical to a crash.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Bar {
    pub symbol: String,
    pub day: String,
    pub name: String,
    pub open_cents: i64,
    pub high_cents: i64,
    pub low_cents: i64,
    pub close_cents: i64,
    pub prev_close_cents: i64,
    pub volume_shares: i64,
    pub amount_cents: i64,
    /// `none` for unadjusted (daytime package), `qfq` for forward-adjusted
    /// (V1 archive). Never mix the two in one series.
    pub adjust: String,
    pub source: String,
    /// A real close that may fill an order. A halted row carries the official
    /// reference previous close in `close_cents` and must be valued only.
    pub tradable: bool,
}

impl Bar {
    /// Change from the previous close in basis points.
    pub fn change_bps(&self) -> i64 {
        if self.prev_close_cents <= 0 {
            return 0;
        }
        (self.close_cents - self.prev_close_cents) * 10_000 / self.prev_close_cents
    }
}

/// Daily price limit for a symbol, in basis points.
///
/// A-shares cap daily moves per board: 30% Beijing, 20% ChiNext and STAR, 10%
/// main board. Getting this right matters for the gap hint -- a flat 11%
/// threshold flags every ChiNext limit-up as a corporate action, which is what
/// it did on 2026-11-11 (34 symbols, nearly all ordinary moves).
pub fn daily_limit_bps(symbol: &str) -> i64 {
    let code = symbol.split('.').next().unwrap_or("");
    let board = symbol.rsplit('.').next().unwrap_or("");
    let prefix = &code[..code.len().min(3)];
    match (board, prefix) {
        ("BJ", _) => 3_000,
        ("SH", "688") | ("SH", "689") => 2_000,
        ("SZ", "300") | ("SZ", "301") => 2_000,
        _ => 1_000,
    }
}

fn read_bar(row: &rusqlite::Row<'_>) -> rusqlite::Result<Bar> {
    Ok(Bar {
        symbol: row.get(0)?,
        day: row.get(1)?,
        name: row.get(2)?,
        open_cents: row.get(3)?,
        high_cents: row.get(4)?,
        low_cents: row.get(5)?,
        close_cents: row.get(6)?,
        prev_close_cents: row.get(7)?,
        volume_shares: row.get(8)?,
        amount_cents: row.get(9)?,
        adjust: row.get(10)?,
        source: row.get(11)?,
        tradable: row.get(12)?,
    })
}

pub fn slot_name(slot: StrategyId) -> &'static str {
    match slot {
        StrategyId::Steady => "steady",
        StrategyId::Aggressive => "aggressive",
    }
}

fn slot_index(slot: StrategyId) -> usize {
    match slot {
        StrategyId::Steady => 0,
        StrategyId::Aggressive => 1,
    }
}

fn parse_slot(value: &str) -> rusqlite::Result<StrategyId> {
    match value {
        "steady" => Ok(StrategyId::Steady),
        "aggressive" => Ok(StrategyId::Aggressive),
        _ => Err(rusqlite::Error::InvalidQuery),
    }
}

pub fn price_sets_dir() -> PathBuf {
    std::env::var("ASTOCK_PRICE_SETS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("data/price-sets"))
}

pub fn load_price_sets(dir: &Path) -> StoreResult<Option<PriceSet>> {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(StoreError::Integrity(error.to_string())),
    };
    let mut sets = Vec::new();
    let mut ids = BTreeSet::new();
    for entry in entries {
        let path = entry
            .map_err(|error| StoreError::Integrity(error.to_string()))?
            .path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let raw = std::fs::read_to_string(&path)
            .map_err(|error| StoreError::Integrity(error.to_string()))?;
        let set: PriceSet = serde_json::from_str(&raw)?;
        set.validate()?;
        if !ids.insert(set.price_set_id.clone()) {
            return Err(StoreError::Integrity("duplicate_price_set_id".into()));
        }
        sets.push(set);
    }
    sets.sort_by(|left, right| {
        right
            .coverage_end
            .cmp(&left.coverage_end)
            .then_with(|| right.coverage_start.cmp(&left.coverage_start))
            .then_with(|| right.price_set_id.cmp(&left.price_set_id))
    });
    Ok(sets.into_iter().next())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{PriceEntry, SlotSpec};

    fn bar(symbol: &str, day: &str, close: i64, prev: i64) -> Bar {
        Bar {
            symbol: symbol.into(),
            day: day.into(),
            name: format!("n-{symbol}"),
            open_cents: prev,
            high_cents: close.max(prev),
            low_cents: close.min(prev),
            close_cents: close,
            prev_close_cents: prev,
            volume_shares: 1_000,
            amount_cents: close * 1_000,
            adjust: "none".into(),
            source: "tdx".into(),
            tradable: true,
        }
    }

    #[test]
    fn bars_round_trip_through_the_store() {
        let mut store = Store::open(":memory:").unwrap();
        let rows = vec![
            bar("600000.SH", "2026-11-10", 1_020, 1_010),
            bar("000001.SZ", "2026-11-10", 1_186, 1_189),
        ];
        assert_eq!(store.upsert_bars(&rows).unwrap(), 2);

        let stored = store.bars_for_day("2026-11-10").unwrap();
        assert_eq!(stored.len(), 2);
        assert_eq!(stored[0].symbol, "000001.SZ"); // ordered by symbol
        assert_eq!(stored[1].close_cents, 1_020);
        assert_eq!(store.bar_symbol_count("2026-11-10").unwrap(), 2);
        assert_eq!(store.bar_days().unwrap(), vec!["2026-11-10".to_string()]);
    }

    #[test]
    fn research_open_is_read_only_and_pins_one_market_snapshot() {
        let path = temp_db("research-read-only");
        let mut writer = Store::open(&path).unwrap();
        writer.upsert_bars(&[bar("600000.SH", "2026-11-10", 1000, 1000)]).unwrap();
        let mut reader = Store::open_market_read_only(&path).unwrap();
        writer.upsert_bars(&[bar("600000.SH", "2026-11-11", 1001, 1000)]).unwrap();
        assert_eq!(reader.unadjusted_bar_days().unwrap(), ["2026-11-10"]);
        assert!(reader.create_account(spec()).is_err());
        assert!(reader.upsert_bars(&[bar("000001.SZ", "2026-11-10", 1000, 1000)]).is_err());
        assert!(writer.account().unwrap().is_none());
        assert_eq!(writer.bar_days().unwrap().len(), 2);
        drop(reader);
        drop(writer);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn research_open_does_not_create_missing_database() {
        let path = temp_db("research-missing");
        assert!(!path.exists());
        assert!(Store::open_market_read_only(&path).is_err());
        assert!(!path.exists());
    }

    #[test]
    fn upsert_bars_is_idempotent() {
        let mut store = Store::open(":memory:").unwrap();
        let rows = vec![bar("600000.SH", "2026-11-10", 1_020, 1_010)];
        store.upsert_bars(&rows).unwrap();
        store.upsert_bars(&rows).unwrap();
        assert_eq!(store.bar_symbol_count("2026-11-10").unwrap(), 1);

        // Re-ingesting with a corrected close updates in place, no duplicate.
        store
            .upsert_bars(&[bar("600000.SH", "2026-11-10", 1_025, 1_010)])
            .unwrap();
        let stored = store.bars_for_day("2026-11-10").unwrap();
        assert_eq!(stored.len(), 1);
        assert_eq!(stored[0].close_cents, 1_025);
    }

    #[test]
    fn empty_batch_writes_nothing() {
        let mut store = Store::open(":memory:").unwrap();
        assert_eq!(store.upsert_bars(&[]).unwrap(), 0);
        assert!(store.bar_days().unwrap().is_empty());
    }

    #[test]
    fn bars_for_symbol_filters_by_day_range() {
        let mut store = Store::open(":memory:").unwrap();
        store
            .upsert_bars(&[
                bar("600000.SH", "2026-11-03", 1_000, 1_000),
                bar("600000.SH", "2026-11-04", 1_010, 1_000),
                bar("600000.SH", "2026-11-05", 1_020, 1_010),
                bar("000001.SZ", "2026-11-04", 1_186, 1_189),
            ])
            .unwrap();

        let subset = store
            .bars_for_symbol("600000.SH", "2026-11-04", "2026-11-05")
            .unwrap();
        assert_eq!(subset.len(), 2);
        assert_eq!(subset[0].day, "2026-11-04");
        assert_eq!(subset[1].day, "2026-11-05");
    }

    /// A settlement window whose start is later than its end returns nothing.
    ///
    /// This is the trap behind a real defect: an account already settled to the
    /// newest day, asked to run again for an earlier day, built its one-day
    /// window from the table's newest day instead of from the requested day.
    /// `BETWEEN <newer> AND <older>` is empty, so the caller reported
    /// `no_price_data` for a day whose prices were present. The window must be
    /// anchored on the requested day; this test pins the empty result that the
    /// caller has to avoid.
    #[test]
    fn inverted_window_has_no_rows_so_the_window_must_follow_the_requested_day() {
        let mut store = Store::open(":memory:").unwrap();
        store
            .upsert_bars(&[
                bar("600000.SH", "2026-11-11", 1_000, 1_000),
                bar("600000.SH", "2026-11-12", 1_010, 1_000),
                bar("600000.SH", "2026-11-13", 1_020, 1_010),
            ])
            .unwrap();

        let inverted = store
            .price_set_from_bars("bar-table", "2026-11-13", "2026-11-12")
            .unwrap();
        assert!(inverted.is_none(), "a reversed window must not invent a set");

        let anchored = store
            .price_set_from_bars("bar-table", "2026-11-12", "2026-11-12")
            .unwrap()
            .expect("the requested day itself has prices");
        assert_eq!(anchored.days(), vec!["2026-11-12"]);
        assert!(anchored.contains_day("2026-11-12"));
        assert_eq!(anchored.symbols(), vec!["600000.SH".to_string()]);
    }

    #[test]
    fn price_gaps_flags_ex_dividend_and_limit_moves() {
        let mut store = Store::open(":memory:").unwrap();
        store
            .upsert_bars(&[
                // -10%: an ordinary main-board limit move, not a gap.
                bar("600000.SH", "2026-11-10", 900, 1_000),
                // -20%: impossible for a main-board stock without a corporate
                // action, so it is flagged.
                bar("600036.SH", "2026-11-10", 800, 1_000),
                // +5%: ordinary.
                bar("000001.SZ", "2026-11-10", 1_050, 1_000),
                // +15% on ChiNext: under its 20% limit, so ordinary.
                bar("300750.SZ", "2026-11-10", 1_150, 1_000),
                // +27% on ChiNext: past its 20% limit plus headroom, so
                // flagged. (+25% sits inside the 20%+5% margin.)
                bar("300752.SZ", "2026-11-10", 1_270, 1_000),
            ])
            .unwrap();

        let gaps = store.price_gaps("2026-11-10").unwrap();
        let symbols: Vec<&str> = gaps.iter().map(|b| b.symbol.as_str()).collect();
        assert_eq!(symbols, vec!["300752.SZ", "600036.SH"]);
    }

    #[test]
    fn bar_adjust_and_source_round_trip() {
        let mut store = Store::open(":memory:").unwrap();
        let mut qfq = bar("600519.SH", "2026-11-06", 145_300, 145_200);
        qfq.adjust = "qfq".into();
        qfq.source = "v1-qfq".into();
        store
            .upsert_bars(&[qfq, bar("600000.SH", "2026-11-06", 1_003, 987)])
            .unwrap();

        let stored = store.bars_for_day("2026-11-06").unwrap();
        assert_eq!(stored[0].adjust, "none");
        assert_eq!(stored[0].source, "tdx");
        assert_eq!(stored[1].adjust, "qfq");
        assert_eq!(stored[1].source, "v1-qfq");
    }

    #[test]
    fn older_bar_table_gains_adjust_columns_on_open() {
        // A database created before `adjust`/`source` existed must still open,
        // and must report the safe defaults rather than failing.
        let dir = std::env::temp_dir().join(format!("astock-bar-migrate-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("old.sqlite3");
        let _ = std::fs::remove_file(&path);
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version(version) VALUES(2);
                CREATE TABLE bar(
                    symbol TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                    open_cents INTEGER NOT NULL, high_cents INTEGER NOT NULL,
                    low_cents INTEGER NOT NULL, close_cents INTEGER NOT NULL,
                    prev_close_cents INTEGER NOT NULL, volume_shares INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL, PRIMARY KEY(symbol, day));
                INSERT INTO bar VALUES('600000.SH','2026-11-10','x',1000,1020,990,1020,1010,100,102000);
                "#,
            )
            .unwrap();
        }

        let store = Store::open(&path).unwrap();
        let rows = store.bars_for_day("2026-11-10").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].adjust, "none");
        assert_eq!(rows[0].source, "tdx");
        drop(store);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_tdx_halts_are_classified_once_when_tradable_is_added() {
        let path = temp_db("legacy-halt-classification");
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE bar(
                    symbol TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL,
                    open_cents INTEGER NOT NULL, high_cents INTEGER NOT NULL,
                    low_cents INTEGER NOT NULL, close_cents INTEGER NOT NULL,
                    prev_close_cents INTEGER NOT NULL, volume_shares INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL, adjust TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(symbol, day));
                INSERT INTO bar VALUES
                    ('000007.SZ','2026-11-10','halt',0,0,0,480,474,0,0,'none','tdx'),
                    ('000008.SZ','2026-11-10','no-ref',0,0,0,480,0,0,0,'none','tdx'),
                    ('600000.SH','2026-11-10','traded',1000,1020,990,1020,1010,100,102000,'none','tdx'),
                    ('600001.SH','2026-11-10','adjusted',0,0,0,480,474,0,0,'qfq','v1-qfq');
                "#,
            ).unwrap();
        }
        let mut store = Store::open(&path).unwrap();
        let rows = store.bars_for_day("2026-11-10").unwrap();
        assert_eq!((rows[0].close_cents, rows[0].tradable), (474, false));
        assert_eq!((rows[1].close_cents, rows[1].tradable), (0, false));
        assert_eq!((rows[2].close_cents, rows[2].tradable), (1020, true));
        assert_eq!((rows[3].close_cents, rows[3].tradable), (480, true));
        let prices = store.unadjusted_closes("2026-11-10").unwrap();
        assert!(prices.iter().any(|row|
            row.symbol == "000007.SZ" && row.price_cents == 474 && !row.tradable));
        assert!(!prices.iter().any(|row| row.symbol == "000008.SZ"));

        let mut explicit = bar("600000.SH", "2026-11-10", 1020, 1010);
        explicit.tradable = false;
        store.upsert_bars(&[explicit]).unwrap();
        drop(store);
        let reopened = Store::open(&path).unwrap();
        let rows = reopened.bars_for_day("2026-11-10").unwrap();
        assert_eq!((rows[0].close_cents, rows[0].tradable), (474, false));
        assert!(!rows[2].tradable);
        drop(reopened);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn bar_change_bps_is_signed_and_guards_zero_prev() {
        assert_eq!(bar("x.SH", "d", 1_100, 1_000).change_bps(), 1_000);
        assert_eq!(bar("x.SH", "d", 900, 1_000).change_bps(), -1_000);
        assert_eq!(bar("x.SH", "d", 1_000, 0).change_bps(), 0);
    }

    fn spec() -> AccountSpec {
        AccountSpec {
            schema_version: 1,
            account_id: "test-account".into(),
            initial_cash_cents: 10_000_000,
            created_at: "2026-07-01T09:00:00+08:00".into(),
            slots: [
                SlotSpec {
                    slot: StrategyId::Steady,
                    weight_bps: 5_000,
                },
                SlotSpec {
                    slot: StrategyId::Aggressive,
                    weight_bps: 5_000,
                },
            ],
        }
    }

    fn day(index: usize) -> String {
        let start = chrono::NaiveDate::from_ymd_opt(2026, 7, 1).unwrap();
        (start + chrono::Duration::days(index as i64))
            .format("%Y-%m-%d")
            .to_string()
    }

    fn price_set(days: usize) -> PriceSet {
        let mut entries = Vec::new();
        for index in 0..days {
            let close = 1_000 + index as i64 * 2;
            for symbol in ["600000.SH", "000001.SZ"] {
                entries.push(PriceEntry {
                    symbol: symbol.into(),
                    day: day(index),
                    price_cents: close,
                    source: "offline-test".into(),
                    observed_at: format!("{}T15:00:00+08:00", day(index)),
                    tradable: true,
                    can_buy: true,
                    can_sell: true,
                    open_cents: 0,
                    high_cents: 0,
                    low_cents: 0,
                    volume_shares: 0,
                });
            }
        }
        PriceSet {
            schema_version: 1,
            price_set_id: "test-set".into(),
            market_data_pack_id: "test-pack".into(),
            trust: "reviewed".into(),
            coverage_start: day(0),
            coverage_end: day(days - 1),
            entries,
            source_digest: "a".repeat(64),
        }
    }

    fn split_price_set(days: usize) -> PriceSet {
        let mut entries = Vec::new();
        for index in 0..days {
            // Before day 60 the symbols move together, so both slots hold the
            // same basket. From day 60 the two symbols diverge sharply.
            let first = if index < 60 {
                1_000 + index as i64
            } else {
                1_060 + (index as i64 - 60) * 40
            };
            let second = if index < 60 {
                1_000 + index as i64
            } else {
                1_060 + (index as i64 - 60) * 20
            };
            for (symbol, close) in [("600000.SH", first), ("000001.SZ", second)] {
                entries.push(PriceEntry {
                    symbol: symbol.into(),
                    day: day(index),
                    price_cents: close,
                    source: "offline-test".into(),
                    observed_at: format!("{}T15:00:00+08:00", day(index)),
                    tradable: true,
                    can_buy: true,
                    can_sell: true,
                    open_cents: 0,
                    high_cents: 0,
                    low_cents: 0,
                    volume_shares: 0,
                });
            }
        }
        PriceSet {
            schema_version: 1,
            price_set_id: "split-test-set".into(),
            market_data_pack_id: "test-pack".into(),
            trust: "reviewed".into(),
            coverage_start: day(0),
            coverage_end: day(days - 1),
            entries,
            source_digest: "b".repeat(64),
        }
    }

    fn store() -> Store {
        let mut store = Store::open(":memory:").unwrap();
        store.create_account(spec()).unwrap();
        store
    }

    fn temp_db(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "astock-v2-{label}-{}.sqlite3",
            std::process::id()
        ));
        for suffix in ["", "-wal", "-shm"] {
            let file = PathBuf::from(format!("{}{suffix}", path.display()));
            let _ = std::fs::remove_file(file);
        }
        path
    }

    fn config_change(store: &Store, steady: i64) {
        let mut updated = store.account().unwrap().unwrap();
        updated.slots[0].weight_bps = steady;
        updated.slots[1].weight_bps = 10_000 - steady;
        store
            .conn
            .execute(
                "UPDATE account SET spec_json=?",
                [serde_json::to_string(&updated).unwrap()],
            )
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE slot SET weight_bps=? WHERE slot='steady'",
                [steady],
            )
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE slot SET weight_bps=? WHERE slot='aggressive'",
                [10_000 - steady],
            )
            .unwrap();
    }

    #[test]
    fn advancing_fills_every_day_once_and_preserves_cash() {
        let mut store = store();
        let set = price_set(90);
        let result = store.advance(&spec(), &set, &day(89), None).unwrap();
        assert_eq!(result.processed_days.len(), 90);
        assert!(!result.trades.is_empty());
        let books = store.books("test-account").unwrap();
        let cash: i64 = books.iter().map(|book| book.cash_cents).sum();
        let view = store.view(&spec(), Some(&set), Some(&day(89))).unwrap();
        assert_eq!(view.cash_cents, cash);
        assert_eq!(
            view.equity_cents.unwrap(),
            cash
                + view
                    .positions
                    .iter()
                    .map(|position| position.market_value_cents.unwrap())
                    .sum::<i64>()
        );
        let again = store.advance(&spec(), &set, &day(89), None).unwrap();
        assert!(again.processed_days.is_empty());
        assert!(again.trades.is_empty());
        assert_eq!(
            store.trades("test-account", 500).unwrap().len(),
            result.trades.len()
        );
    }

    #[test]
    fn same_day_replay_returns_the_same_fills_without_new_rows() {
        let mut store = store();
        let set = price_set(90);
        let first = store.run_day(&spec(), &set, &day(70)).unwrap();
        let count = store.trades("test-account", 500).unwrap().len();
        let second = store.run_day(&spec(), &set, &day(70)).unwrap();
        assert!(second.already_settled);
        assert_eq!(first.trades.len(), second.trades.len());
        assert_eq!(store.trades("test-account", 500).unwrap().len(), count);
    }

    #[test]
    fn rebalance_moves_cash_and_weights_in_one_commit() {
        let mut store = store();
        let set = price_set(90);
        store.advance(&spec(), &set, &day(89), None).unwrap();
        config_change(&store, 5_400);
        let books = store.books("test-account").unwrap();
        let before: i64 = books.iter().map(|book| book.cash_cents).sum();
        let view = store.view(&spec(), Some(&set), Some(&day(89))).unwrap();
        let total = view.equity_cents.unwrap();
        let weights = store.weights("test-account").unwrap();
        assert_eq!(weights[0] + weights[1], 10_000);
        let books = store.books("test-account").unwrap();
        let after: i64 = books.iter().map(|book| book.cash_cents).sum();
        assert_eq!(before, after);
        assert!(total > 0);
    }

    #[test]
    fn rebalanced_day_records_equity_from_the_transferred_books() {
        let mut store = store();
        let set = split_price_set(75);
        let mut applied = None;
        for index in 0..=66 {
            let run = store.run_day(&spec(), &set, &day(index)).unwrap();
            if let Some(item) = run.rebalance.filter(|item| item.moved_cents > 0) {
                applied = Some((index, item));
                break;
            }
        }
        let (index, rebalance) = applied.expect("a cash-moving rebalance");
        let books = store.books("test-account").unwrap();
        let applied_day = day(index);
        let mut expected = [0i64; 2];
        for (slot_index, book) in books.iter().enumerate() {
            let market = book
                .positions
                .iter()
                .map(|position| {
                    set.valuation(&position.symbol, &applied_day).unwrap() * position.quantity
                })
                .sum::<i64>();
            expected[slot_index] = book.cash_cents + market;
        }
        let stored = [StrategyId::Steady, StrategyId::Aggressive].map(|slot| {
            store
                .conn
                .query_row(
                    "SELECT equity_cents FROM slot_equity_day WHERE account_id=? AND slot=? AND day=?",
                    params!["test-account", slot_name(slot), applied_day],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap()
        });
        assert_eq!(stored, expected);
        assert!(rebalance.moved_cents > 0);
    }

    #[test]
    fn slot_profit_excludes_cash_transfers_and_sums_to_account_profit() {
        let mut store = store();
        let set = split_price_set(75);
        store.advance(&spec(), &set, &day(66), None).unwrap();
        let last = store.last_settled_day("test-account").unwrap().unwrap();
        let view = store.view(&spec(), Some(&set), Some(&last)).unwrap();
        let slot_profit = view
            .slots
            .iter()
            .map(|slot| slot.profit_cents.unwrap())
            .sum::<i64>();
        assert_eq!(slot_profit, view.profit_cents.unwrap());
        for slot in &view.slots {
            assert_ne!(
                slot.weight_bps, 5_000,
                "the rebalance must resize a slot for this regression to be meaningful"
            );
            let net_flow: i64 = view.rebalances.iter().map(|item| {
                if item.to == slot.slot { item.moved_cents } else { -item.moved_cents }
            }).sum();
            assert_ne!(net_flow, 0);
            let expected = slot.equity_cents.unwrap() - spec().initial_cash_cents / 2 - net_flow;
            assert_eq!(slot.profit_cents, Some(expected));
        }
    }

    #[test]
    fn account_view_propagates_a_missing_held_quote() {
        let mut store = store();
        let mut set = price_set(90);
        let as_of = day(70);
        store.run_day(&spec(), &set, &as_of).unwrap();
        let held = store.books("test-account").unwrap().into_iter()
            .flat_map(|book| book.positions).next().expect("fixture has a position").symbol;
        set.entries.retain(|entry| entry.day != as_of || entry.symbol != held);
        let error = store.view(&spec(), Some(&set), Some(&as_of)).unwrap_err();
        assert!(matches!(error, StoreError::Rules(RuleError(ref message)) if message == "missing_close_price"));
    }

    #[test]
    fn a_cash_transfer_alone_does_not_create_strategy_profit_or_drawdown() {
        let store = store();
        for (index, equities) in [[5_000_000, 5_000_000], [6_000_000, 4_000_000]].iter().enumerate() {
            for (slot, equity) in ["steady", "aggressive"].into_iter().zip(equities) {
                store.conn.execute(
                    "INSERT INTO slot_equity_day VALUES(?,?,?,?)",
                    params!["test-account", slot, day(index), equity],
                ).unwrap();
            }
        }
        store.conn.execute(
            "INSERT INTO rebalance(account_id,old_steady_bps,old_aggressive_bps,new_steady_bps,new_aggressive_bps,moved_cents,reason,applied_day) VALUES(?,5000,5000,6000,4000,1000000,'test',?)",
            params!["test-account", day(1)],
        ).unwrap();
        store.conn.execute("UPDATE slot SET cash_cents=6000000 WHERE slot='steady'", []).unwrap();
        store.conn.execute("UPDATE slot SET cash_cents=4000000 WHERE slot='aggressive'", []).unwrap();
        let view = store.view(&spec(), Some(&price_set(2)), Some(&day(1))).unwrap();
        for slot in view.slots {
            assert_eq!(slot.profit_cents, Some(0));
            assert_eq!(slot.return_bps, Some(0));
            assert_eq!(slot.drawdown_bps, Some(0));
        }
        let curves = store.account_performance(&spec(), None).unwrap();
        assert!(curves.iter().flatten().all(|point| point.return_bps == 0));
        // Revaluing the settled date must retain its flow, not count it twice.
        assert_eq!(curves, store.account_performance(&spec(), Some((&day(1), [6_000_000, 4_000_000]))).unwrap());
        let later = store.account_performance(&spec(), Some((&day(2), [6_600_000, 4_000_000]))).unwrap();
        assert_eq!(later[1].last().unwrap().profit_cents, 600_000);
        assert_eq!(later[1].last().unwrap().return_bps, 1_000);
        assert_eq!(store.equity_curve("test-account").unwrap().len(), 2);
    }

    /// Marking the book before a day the account already settled past is a
    /// conflict about the request, not a missing-price rules error.
    ///
    /// The old code reported `valuation_before_last_settled` as a rules error,
    /// which the HTTP layer turned into a generic 422 that callers misread as
    /// "no market data for that day".
    #[test]
    fn marking_before_a_settled_day_is_a_conflict_not_a_missing_price() {
        let mut store = store();
        let set = price_set(4);
        let settled = store.advance(&spec(), &set, &day(3), None).unwrap();
        assert_eq!(settled.processed_days.last().map(String::as_str), Some(day(3).as_str()));
        let error = store
            .account_performance(&spec(), Some((&day(1), [6_000_000, 4_000_000])))
            .unwrap_err();
        match error {
            StoreError::Conflict(code) => assert_eq!(code, "day_before_last_settled"),
            other => panic!("expected a conflict, got {other:?}"),
        }
    }

    #[test]
    fn rebalance_without_enough_source_cash_keeps_weights_and_does_not_fail_the_day() {
        let mut store = store();
        let set = price_set(4);
        for index in 0..2 {
            for (slot, equity) in [
                ("steady", 5_000_000),
                ("aggressive", if index == 0 { 6_000_000 } else { 4_000_000 }),
            ] {
                store
                    .conn
                    .execute(
                        "INSERT INTO slot_equity_day(account_id,slot,day,equity_cents) VALUES(?,?,?,?)",
                        params!["test-account", slot, day(index), equity],
                    )
                    .unwrap();
            }
        }
        store
            .conn
            .execute(
                "UPDATE slot SET cash_cents=5000000 WHERE slot='steady'",
                [],
            )
            .unwrap();
        store
            .conn
            .execute("UPDATE slot SET cash_cents=0 WHERE slot='aggressive'", [])
            .unwrap();

        let run = store
            .run_day(&spec(), &set, &day(3))
            .expect("insufficient transfer cash must not abort the day");
        assert!(run.rebalance.is_none());
        assert_eq!(store.weights("test-account").unwrap(), [5_000, 5_000]);
    }

    #[test]
    fn older_v2_database_is_refused_before_it_is_reinterpreted() {
        let path = temp_db("legacy");
        let legacy = Connection::open(&path).unwrap();
        legacy.execute_batch("PRAGMA user_version = 15;").unwrap();
        legacy
            .execute_batch("CREATE TABLE v2_paper_accounts (account_id TEXT PRIMARY KEY);")
            .unwrap();
        drop(legacy);
        let error = Store::open(&path).err().expect("legacy db must fail");
        assert!(matches!(error, StoreError::Integrity(ref value) if value.contains("legacy_v2")));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn a_limit_down_seal_blocks_the_sale_and_keeps_the_position() {
        // A held name gaps to a single-price limit-down session. The row has a
        // real close, so a same-close model would book a fill at the bottom;
        // the seal must leave the position open instead.
        let mut store = store();
        let mut set = price_set(90);
        store.advance(&spec(), &set, &day(79), None).unwrap();
        let before = store.books("test-account").unwrap();
        let (book_slot, held) = before
            .iter()
            .flat_map(|book| book.positions.iter().map(move |item| (book.slot, item)))
            .next()
            .expect("fixture holds a position");
        let symbol = held.symbol.clone();
        let quantity = held.quantity;
        let crash = held.average_cost_cents * 850 / 1_000;
        for entry in set.entries.iter_mut() {
            if entry.symbol == symbol && entry.day.as_str() > day(79).as_str() {
                entry.price_cents = crash;
                entry.tradable = true;
                entry.can_buy = true;
                entry.can_sell = false;
            }
        }
        store.run_day(&spec(), &set, &day(80)).expect("sealed day still settles");
        let after = store.books("test-account").unwrap();
        let still_held: i64 = after
            .iter()
            .filter(|book| book.slot == book_slot)
            .flat_map(|book| book.positions.iter())
            .filter(|item| item.symbol == symbol)
            .map(|item| item.quantity)
            .sum();
        assert_eq!(still_held, quantity, "a sealed limit-down sale must not fill");
    }

    #[test]
    fn same_close_stop_still_fires_and_next_session_uses_the_prior_close() {
        // A 13% drawdown from cost must exit on the stop day in both timings.
        // The same-close book reads today's close; the next-session book reads
        // the previous close. An empty trigger map would silently disable the
        // stop, which is what this guards.
        let mut store = store();
        let mut set = price_set(90);
        store.advance(&spec(), &set, &day(79), None).unwrap();
        let holdings = store.books("test-account").unwrap();
        let (slot, held) = holdings
            .iter()
            .flat_map(|book| book.positions.iter().map(move |item| (book.slot, item)))
            .next()
            .expect("fixture holds a position");
        let symbol = held.symbol.clone();
        let cost = held.average_cost_cents;
        let crash = cost * 860 / 1_000;
        for entry in set.entries.iter_mut() {
            if entry.symbol == symbol && entry.day.as_str() > day(79).as_str() {
                entry.price_cents = crash;
            }
        }
        let day_name = day(80);
        let run = store
            .run_day(&spec(), &set, &day_name)
            .expect("a crash day must still settle");
        let stop = run
            .trades
            .iter()
            .find(|trade| trade.side == "sell" && trade.symbol == symbol);
        assert!(
            stop.is_some_and(|trade| trade.reason == "stop_loss"),
            "slot {:?} kept {symbol} through a stop-sized drop: {:?}", slot,
            run.trades.iter().map(|t| (t.symbol.clone(), t.side.clone(), t.reason.clone())).collect::<Vec<_>>()
        );
    }

    #[test]
    fn divergent_strategy_returns_trigger_a_bounded_rebalance() {
        let mut store = store();
        let set = split_price_set(75);
        store.advance(&spec(), &set, &day(66), None).unwrap();
        let rebalances = store.rebalances("test-account").unwrap();
        // Day 61 is the first distinct close in the two symbol paths. Correct
        // per-symbol sizing can expose that edge a day earlier than before.
        assert!(rebalances.iter().all(|item| item.applied_day >= day(61)));
        let rebalance = rebalances.iter()
            .find(|item| item.applied_day >= day(61))
            .expect("divergent symbol returns must trigger a rebalance");
        assert_ne!(rebalance.old_steady_bps, rebalance.new_steady_bps);
        assert!((rebalance.new_steady_bps - rebalance.old_steady_bps).abs() <= 1_000);
        assert_eq!(
            rebalance.new_steady_bps + rebalance.new_aggressive_bps,
            10_000
        );
        assert!(rebalance.moved_cents > 0);
        assert_eq!(
            store.weights("test-account").unwrap(),
            [rebalance.new_steady_bps, rebalance.new_aggressive_bps]
        );
    }

    #[test]
    fn advance_memory_guard_rejects_before_building_the_window() {
        let mut store = store();
        let set = price_set(3);
        store.advance_limits.max_entries = set.entries.len() - 1;
        let error = store.advance(&spec(), &set, &day(2), None).unwrap_err();
        match error {
            StoreError::Aborted(code) => assert!(code.starts_with("advance_memory_limit:")),
            other => panic!("expected a memory-limit abort, got {other:?}"),
        }
        // Negative control: no day was committed by the rejected batch.
        assert!(store.last_settled_day("test-account").unwrap().is_none());
    }

    #[test]
    fn advance_chunks_preserve_the_same_fills_as_one_batch() {
        let mut chunked = store();
        chunked.advance_limits.chunk_days = 2;
        let mut whole = store();
        let set = price_set(7);
        let mut all_days = Vec::new();
        loop {
            let run = chunked.advance_one_chunk(&spec(), &set, &day(6), None, None).unwrap();
            if run.processed_days.is_empty() {
                break;
            }
            all_days.extend(run.processed_days);
            if all_days.len() == 7 {
                break;
            }
        }
        let one = whole.advance(&spec(), &set, &day(6), None).unwrap();
        assert_eq!(all_days, one.processed_days);
        assert_eq!(
            chunked.trades("test-account", 500).unwrap().len(),
            whole.trades("test-account", 500).unwrap().len()
        );
        assert_eq!(
            chunked
                .view(&spec(), Some(&set), Some(&day(6)))
                .unwrap()
                .equity_cents,
            whole
                .view(&spec(), Some(&set), Some(&day(6)))
                .unwrap()
                .equity_cents
        );
    }

    #[test]
    fn live_read_only_connection_is_read_only() {
        let path = temp_db("live-read-only");
        let mut writer = Store::open(&path).unwrap();
        writer.create_account(spec()).unwrap();
        // A live read-only connection opens the initialized schema without
        // migration, sees committed rows, and cannot write. It deliberately
        // does not pin an old snapshot like `open_market_read_only`.
        let reader = Store::open_read_only(&path).unwrap();
        assert_eq!(reader.account().unwrap().unwrap().account_id, "test-account");
        assert!(reader
            .conn
            .execute("UPDATE account SET created_at='bad'", [])
            .is_err());
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", path.display()));
        }
    }

    // ---- lower-wick candidate filter ---------------------------------------

    fn wick_row(symbol: &str, day: &str, open: i64, low: i64, close: i64) -> Bar {
        Bar {
            symbol: symbol.into(),
            day: day.into(),
            name: String::new(),
            open_cents: open,
            high_cents: open,
            low_cents: low,
            close_cents: close,
            prev_close_cents: close,
            volume_shares: 100_000,
            amount_cents: close * 100_000,
            adjust: "none".into(),
            source: "tdx".into(),
            tradable: true,
        }
    }

    /// Ten symbols with strictly increasing lower wicks on 2026-02-16.
    fn wick_universe() -> Vec<Bar> {
        let mut bars = Vec::new();
        for index in 0..10 {
            let symbol = format!("60000{index}.SH");
            for day in 1..=40 {
                let close = 1_000 + day * 2;
                bars.push(wick_row(&symbol, &format!("2026-01-{day:02}"), close, close, close));
            }
            let open = 1_100;
            let low = open - index as i64 * 10;
            bars.push(wick_row(&symbol, "2026-02-16", open, low, open));
        }
        bars
    }

    fn scored_symbols(filter: bool, cut_bps: i64, held: &[&str]) -> Vec<String> {
        let mut store = Store::open(":memory:").unwrap();
        store.create_account(spec()).unwrap();
        store.klow_filter = filter;
        store.klow_cut_bps = cut_bps;
        store.upsert_bars(&wick_universe()).unwrap();
        let set = store
            .price_set_from_bars("wick", "2026-01-01", "2026-02-16")
            .unwrap()
            .expect("a price set over the fixture");
        let index = IndexedPriceSet::new(&set);
        let closes = closes_for_indexed(&index, "2026-02-16");
        store
            .signals_for_day(
                StrategyId::Steady,
                &index,
                "2026-02-16",
                &closes,
                held.iter().copied(),
                index.adjustments(),
            )
            .unwrap()
            .into_iter()
            .map(|(symbol, _)| symbol)
            .collect()
    }

    #[test]
    fn the_wick_filter_is_off_by_default_and_would_not_fire_unasked() {
        let store = Store::open(":memory:").unwrap();
        assert!(!store.klow_filter);
        assert_eq!(store.klow_cut_bps, 2_000);
        assert_eq!(scored_symbols(false, 2_000, &[]).len(), 10);
    }

    #[test]
    fn enabling_the_filter_drops_the_longest_lower_wicks() {
        let all = scored_symbols(false, 2_000, &[]);
        let kept = scored_symbols(true, 2_000, &[]);
        assert_eq!(all.len(), 10, "unfiltered scores every symbol");
        assert!(kept.len() < all.len(), "the filter must remove names");
        assert!(!kept.iter().any(|s| s == "600009.SH"), "longest wick excluded");
        assert!(!kept.iter().any(|s| s == "600008.SH"), "second longest excluded");
        assert!(kept.iter().any(|s| s == "600000.SH"), "flat wick kept");
    }

    #[test]
    fn a_held_name_is_scored_even_when_its_wick_is_the_worst() {
        // Dropping a holding from the signal list would skip its exit rules.
        let kept = scored_symbols(true, 2_000, &["600009.SH"]);
        assert!(kept.iter().any(|s| s == "600009.SH"));
    }

    #[test]
    fn a_bar_without_session_shape_is_not_read_as_a_long_wick() {
        // Only closes: no open/low to measure. Absence is not evidence.
        let mut store = Store::open(":memory:").unwrap();
        store.create_account(spec()).unwrap();
        store.klow_filter = true;
        let mut rows = Vec::new();
        for index in 0..10 {
            let symbol = format!("60000{index}.SH");
            for day in 1..=40 {
                let close = 1_000 + day * 2;
                rows.push(wick_row(&symbol, &format!("2026-01-{day:02}"), close, close, close));
            }
            // Reproduce the shape-less case through the price-set path: the
            // column exists but carries zero.
            let mut row = wick_row(&symbol, "2026-02-16", 1_100, 1_100, 1_100);
            row.open_cents = 0;
            row.high_cents = 0;
            row.low_cents = 0;
            rows.push(row);
        }
        store.upsert_bars(&rows).unwrap();
        let set = store
            .price_set_from_bars("wick", "2026-01-01", "2026-02-16")
            .unwrap()
            .expect("set");
        let index = IndexedPriceSet::new(&set);
        let closes = closes_for_indexed(&index, "2026-02-16");
        let kept: Vec<String> = store
            .signals_for_day(
                StrategyId::Steady,
                &index,
                "2026-02-16",
                &closes,
                std::iter::empty(),
                index.adjustments(),
            )
            .unwrap()
            .into_iter()
            .map(|(symbol, _)| symbol)
            .collect();
        assert_eq!(kept.len(), 10, "nothing measurable means nothing to exclude");
    }

    #[test]
    fn a_zero_or_full_cut_cannot_empty_the_universe() {
        assert_eq!(scored_symbols(true, 0, &[]).len(), 10);
        assert!(!scored_symbols(true, 10_000, &[]).is_empty());
    }
}
