//! SQLite is the only writer. A trading day is one atomic commit: settle,
//! plan, execute, value and rebalance either all succeed or none do.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

use rusqlite::{params, Connection, OptionalExtension, Transaction};

use crate::domain::{
    apply_buy, apply_sell, drawdown_bps, fees_cents, initial_split, orders_for_day, propose, score,
    settle_overnight, signal, AccountSpec, AccountView, AdvanceResult, DayRun, IndexedPriceSet,
    PositionView, PriceCoverage, PriceSet, Rebalance, RuleError, SlotBook, SlotSpec, SlotView,
    StrategyId, Trade,
};

pub const SCHEMA_VERSION: i64 = 2;

/// How many trailing closes a strategy needs to decide today.
///
/// The longest requirement in `domain::strategy` is 61 closes (a 60-day mean
/// plus the latest bar). Anything older cannot change the signal, so there is
/// no reason to carry it through the moving-average code -- on a 5.3k-symbol
/// universe that difference is ~4M rows versus ~320k per settled day.
pub const SIGNAL_LOOKBACK_DAYS: usize = 61;

#[derive(Debug)]
pub enum StoreError {
    Database(rusqlite::Error),
    Encoding(serde_json::Error),
    Rules(RuleError),
    NotFound(String),
    Conflict(String),
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
            Self::Integrity(error) => write!(formatter, "integrity: {error}"),
        }
    }
}

impl std::error::Error for StoreError {}

pub type StoreResult<T> = Result<T, StoreError>;

pub struct Store {
    pub conn: Connection,
}

impl Store {
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
        let store = Self { conn };
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

    /// Add `adjust`/`source` to a `bar` table created before those columns
    /// existed. Idempotent: CREATE TABLE IF NOT EXISTS does nothing when the
    /// table is already there, so existing databases need this to catch up.
    fn ensure_bar_columns(&self) -> StoreResult<()> {
        let mut have: Vec<String> = Vec::new();
        {
            let mut stmt = self.conn.prepare("PRAGMA table_info(bar)")?;
            let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
            for row in rows {
                have.push(row?);
            }
        }
        for (name, ddl) in [
            ("adjust", "ALTER TABLE bar ADD COLUMN adjust TEXT NOT NULL DEFAULT 'none'"),
            ("source", "ALTER TABLE bar ADD COLUMN source TEXT NOT NULL DEFAULT 'tdx'"),
        ] {
            if !have.iter().any(|column| column == name) {
                self.conn.execute(ddl, [])?;
            }
        }
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
                 close_cents,prev_close_cents,volume_shares,amount_cents,adjust,source) \
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(symbol,day) DO UPDATE SET \
                   name=excluded.name, open_cents=excluded.open_cents, \
                   high_cents=excluded.high_cents, low_cents=excluded.low_cents, \
                   close_cents=excluded.close_cents, \
                   prev_close_cents=excluded.prev_close_cents, \
                   volume_shares=excluded.volume_shares, \
                   amount_cents=excluded.amount_cents, \
                   adjust=excluded.adjust, source=excluded.source",
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
                ])?;
            }
        }
        txn.commit()?;
        Ok(bars.len())
    }

    /// All bars for one trading day, ordered by symbol.
    pub fn bars_for_day(&self, day: &str) -> StoreResult<Vec<Bar>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol,day,name,open_cents,high_cents,low_cents,close_cents,\
             prev_close_cents,volume_shares,amount_cents,adjust,source \
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
             prev_close_cents,volume_shares,amount_cents,adjust,source \
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
    pub fn unadjusted_closes(&self, day: &str) -> StoreResult<Vec<(String, String, i64)>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol, day, close_cents FROM bar \
             WHERE day = ?1 AND adjust = 'none' AND close_cents > 0 ORDER BY symbol",
        )?;
        let rows = stmt.query_map([day], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, i64>(2)?))
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
    ) -> StoreResult<Vec<(String, String, i64)>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol, day, close_cents FROM bar \
             WHERE day BETWEEN ?1 AND ?2 AND adjust = 'none' AND close_cents > 0 \
             ORDER BY symbol, day",
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
        Ok(Some(crate::domain::PriceSet::from_bars(
            &rows,
            symbol,
            coverage_start,
            coverage_end,
        )))
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
    /// This is not a validation gate — nothing is rejected or blocked. It is a
    /// display hint: a gap this large is either an ex-dividend/split or bad
    /// data, and either way the K-line should not draw a solid bar across it.
    /// Default threshold 1100 bps clears the ±10% limit plus rounding; ST
    /// stocks (5% limit) will show up too, which is intended.
    pub fn price_gaps(&self, day: &str, threshold_bps: i64) -> StoreResult<Vec<Bar>> {
        let mut stmt = self.conn.prepare(
            "SELECT symbol,day,name,open_cents,high_cents,low_cents,close_cents,\
             prev_close_cents,volume_shares,amount_cents,adjust,source \
             FROM bar WHERE day = ?1 AND prev_close_cents > 0 \
             AND abs(close_cents - prev_close_cents) * 10000 / prev_close_cents > ?2 \
             ORDER BY symbol",
        )?;
        let rows = stmt.query_map(params![day, threshold_bps], read_bar)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
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
    pub fn advance(
        &mut self,
        spec: &AccountSpec,
        set: &PriceSet,
        through: &str,
        settle_from: Option<&str>,
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

        let mut all_trades = Vec::new();
        let mut all_rebalances = Vec::new();
        // Build the lookup index once for the whole advance. Rebuilding it per
        // day would re-walk millions of entries on every iteration.
        let index_set = IndexedPriceSet::new(set.clone());
        for day in &days {
            let run = self.run_day_indexed(spec, set, &index_set, day)?;
            all_trades.extend(run.trades);
            all_rebalances.extend(run.rebalance);
        }
        let current = self
            .account()?
            .ok_or_else(|| StoreError::NotFound("account_not_found".into()))?;
        Ok(AdvanceResult {
            from_day,
            through_day: through.into(),
            processed_days: days,
            trades: all_trades,
            rebalances: all_rebalances,
            account: self.view(&current, Some(set), Some(through))?,
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
    fn signals_for_day<'a>(
        &self,
        slot: StrategyId,
        set: &'a IndexedPriceSet,
        day: &str,
        closes: &'a BTreeMap<String, i64>,
        held: impl Iterator<Item = &'a str>,
    ) -> StoreResult<Vec<(String, Option<String>)>> {
        let mut candidates: BTreeSet<&'a str> = closes.keys().map(String::as_str).collect();
        candidates.extend(held);

        let mut signals = Vec::with_capacity(candidates.len());
        for symbol in candidates {
            let mut bars = set.bars_for(symbol, day);
            if bars.len() > SIGNAL_LOOKBACK_DAYS {
                bars.drain(..bars.len() - SIGNAL_LOOKBACK_DAYS);
            }
            signals.push((symbol.to_string(), signal(slot, &bars)?));
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
        let index = IndexedPriceSet::new(set.clone());
        self.run_day_indexed(spec, set, &index, day)
    }

    fn run_day_indexed(
        &mut self,
        spec: &AccountSpec,
        set: &PriceSet,
        index_set: &IndexedPriceSet,
        day: &str,
    ) -> StoreResult<DayRun> {
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
                day,
                &closes,
                book.positions.iter().map(|position| position.symbol.as_str()),
            )?;
            let budget = self.slot_equity(&spec.account_id, slot)?;
            let orders = orders_for_day(
                slot,
                &book.positions,
                &signals,
                budget,
                book.cash_cents,
                &closes,
                &book.sellable,
            );
            for order in orders {
                let price = closes
                    .get(&order.symbol)
                    .copied()
                    .ok_or_else(|| StoreError::Rules(RuleError("missing_close_price".into())))?;
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
        let initial_budgets = initial_split(spec.initial_cash_cents)?;
        let prices = set.zip(day).map(|(set, day)| closes_for(set, day));
        let mut slots = Vec::new();
        let mut positions = Vec::new();
        for (index, book) in books.iter().enumerate() {
            let market_value = prices.as_ref().and_then(|prices| {
                book.positions
                    .iter()
                    .try_fold(0i64, |total, position| {
                        prices
                            .get(&position.symbol)
                            .map(|price| total + price * position.quantity)
                    })
            });
            // Every slot starts from exactly half of the principal. Report
            // cumulative P&L against that original budget. Later cash
            // transfers only resize the risk budget, so using the live
            // weight here would make one slot look profitable merely because
            // capital was moved to it, while the other would look impaired.
            let budget = initial_budgets[index].cash_cents;
            let history = series(&self.conn, &spec.account_id, book.slot)?;
            slots.push(SlotView {
                slot: book.slot,
                name: book.slot.name().into(),
                description: book.slot.description().into(),
                weight_bps: weights[index],
                cash_cents: book.cash_cents,
                market_value_cents: market_value,
                equity_cents: market_value.map(|value| book.cash_cents + value),
                profit_cents: market_value.map(|value| book.cash_cents + value - budget),
                return_bps: market_value.map(|value| {
                    (book.cash_cents + value - budget) * 10_000 / budget.max(1)
                }),
                drawdown_bps: (!history.is_empty()).then_some(drawdown_bps(&history, budget)),
            });
            for position in &book.positions {
                let close = prices.as_ref().and_then(|map| map.get(&position.symbol).copied());
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

fn closes_for(set: &PriceSet, day: &str) -> BTreeMap<String, i64> {
    set.entries
        .iter()
        .filter(|entry| entry.day == day)
        .map(|entry| (entry.symbol.clone(), entry.price_cents))
        .collect()
}

/// Same as `closes_for` but served from the day index instead of a full scan.
///
/// A whole-market set is ~4M entries; rebuilding a day's close map by scanning
/// all of them, once per trading day settled, is the difference between a
/// settlement pass that finishes and one that does not.
fn closes_for_indexed(index: &IndexedPriceSet, day: &str) -> BTreeMap<String, i64> {
    index.closes_on(day)
}

fn value_book_indexed(
    book: &SlotBook,
    index: &IndexedPriceSet,
    day: &str,
) -> StoreResult<i64> {
    let mut market = 0i64;
    for position in &book.positions {
        let price = index
            .contains(&position.symbol, day)
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
            .contains(&position.symbol, day)
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

fn series(conn: &Connection, account_id: &str, slot: StrategyId) -> StoreResult<Vec<i64>> {
    let mut statement = conn.prepare(
        "SELECT equity_cents FROM slot_equity_day WHERE account_id=? AND slot=? ORDER BY day",
    )?;
    let rows = statement.query_map(params![account_id, slot_name(slot)], |row| row.get(0))?;
    let mut values = Vec::new();
    for row in rows {
        values.push(row?);
    }
    Ok(values)
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

    #[test]
    fn price_gaps_flags_ex_dividend_and_limit_moves() {
        let mut store = Store::open(":memory:").unwrap();
        store
            .upsert_bars(&[
                // -10%: ordinary limit-down, under the default 1100 bps threshold.
                bar("600000.SH", "2026-11-10", 900, 1_000),
                // -20%: beyond a main-board limit, so either ex-dividend or bad data.
                bar("600036.SH", "2026-11-10", 800, 1_000),
                // +5%: ST limit, but under the threshold.
                bar("000001.SZ", "2026-11-10", 1_050, 1_000),
            ])
            .unwrap();

        let gaps = store.price_gaps("2026-11-10", 1_100).unwrap();
        let symbols: Vec<&str> = gaps.iter().map(|b| b.symbol.as_str()).collect();
        assert_eq!(symbols, vec!["600036.SH"]);
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
                    set.contains(&position.symbol, &applied_day).unwrap() * position.quantity
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
    fn slot_profit_uses_original_budget_and_sums_to_account_profit() {
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
            let expected = slot.equity_cents.unwrap() - spec().initial_cash_cents / 2;
            assert_eq!(slot.profit_cents, Some(expected));
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
    fn divergent_strategy_returns_trigger_a_bounded_rebalance() {
        let mut store = store();
        let set = split_price_set(75);
        store.advance(&spec(), &set, &day(66), None).unwrap();
        let rebalance = store
            .rebalances("test-account")
            .unwrap()
            .into_iter()
            .find(|item| item.applied_day >= day(62))
            .expect("divergent symbol returns must trigger a rebalance");
        assert_ne!(rebalance.old_steady_bps, rebalance.new_steady_bps);
        assert!(rebalance.new_steady_bps - rebalance.old_steady_bps <= 1_000);
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
}
