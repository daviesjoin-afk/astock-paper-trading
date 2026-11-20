//! Reviewed close-price set: explicit source, day and whole-cent price.

use serde::{Deserialize, Serialize};

use super::RuleError;

/// One unadjusted row from the `bar` table, before it becomes a `PriceEntry`.
#[derive(Clone, Debug)]
pub struct BarRow {
    pub symbol: String,
    pub day: String,
    pub price_cents: i64,
    /// A real close that may fill an order. A halted row carries the official
    /// reference previous close and is valuation-only.
    pub tradable: bool,
    /// The session's own open/high/low and official previous close. Used only
    /// to decide whether a price-limit seal blocked one side of the book;
    /// zero means the input did not carry them and no seal is inferred.
    pub open_cents: i64,
    pub high_cents: i64,
    pub low_cents: i64,
    pub prev_close_cents: i64,
    /// Whole shares traded on the session. Zero means the input did not carry
    /// it; a volume-derived factor must skip the symbol rather than read a
    /// fabricated zero as "nobody traded".
    pub volume_shares: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceSet {
    pub schema_version: u32,
    pub price_set_id: String,
    pub market_data_pack_id: String,
    pub trust: String,
    pub coverage_start: String,
    pub coverage_end: String,
    pub entries: Vec<PriceEntry>,
    pub source_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceEntry {
    pub symbol: String,
    pub day: String,
    pub price_cents: i64,
    pub source: String,
    pub observed_at: String,
    /// Whether this price is a real close that may fill an order.
    #[serde(default = "default_tradable")]
    pub tradable: bool,
    /// Whether the buy side, respectively the sell side, could fill on this
    /// session. A price-limit seal closes one of them. Defaults keep older
    /// authored price sets valid: an explicit close is an open session unless
    /// the row proves otherwise.
    #[serde(default = "default_tradable")]
    pub can_buy: bool,
    #[serde(default = "default_tradable")]
    pub can_sell: bool,
    /// Session open/high/low in cents, and whole shares traded.
    ///
    /// Optional so every previously authored price set keeps deserialising.
    /// Zero means "the input did not carry it", which is deliberately
    /// distinguishable from a real zero: a factor needing the field has to
    /// skip the symbol rather than compute on a fabricated value.
    #[serde(default)]
    pub open_cents: i64,
    #[serde(default)]
    pub high_cents: i64,
    #[serde(default)]
    pub low_cents: i64,
    #[serde(default)]
    pub volume_shares: i64,
}

/// One symbol-day as the settlement view holds it.
struct Session {
    price_cents: i64,
    open_cents: i64,
    high_cents: i64,
    low_cents: i64,
    volume_shares: i64,
    tradable: bool,
    tradeability: super::Tradeability,
}

fn default_tradable() -> bool {
    true
}

/// A held symbol's market value on a settlement day.
///
/// A row with `tradable = true` is a real close and may be used to fill an
/// order. A halted symbol has no close of its own; its `price_cents` is the
/// exchange's official previous close, which may value an existing position but
/// must never become a fill or a scoring input.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Quote {
    pub price_cents: i64,
    pub open_cents: i64,
    pub high_cents: i64,
    pub low_cents: i64,
    pub volume_shares: i64,
    pub tradable: bool,
    /// Which side of the book was open. A limit-down seal still has a price to
    /// value a position at, but nothing could be sold into it.
    pub can_buy: bool,
    pub can_sell: bool,
}

/// A `PriceSet` with an index over `(symbol, day)`.
///
/// `PriceSet::contains` scans `entries` linearly. That is fine for a reviewed
/// rectangle of a handful of symbols and hopeless for the whole market: a
/// 930-day, 5.3k-symbol set is ~4M entries, and settling one day asks for a
/// price per held symbol. Linear lookup turns a settlement pass into an
/// O(days x symbols x entries) crawl -- measured at >5 minutes of solid CPU
/// for a 2.5-year advance before this existed.
///
/// The wire format is unchanged; this is a read-side index built once.
///
/// The index stores integer dictionary keys instead of duplicating each
/// `PriceEntry`'s four `String`s in two BTreeMaps. The dictionaries themselves
/// hold one copy of each distinct symbol and day. `set()`/`Deref` still expose
/// the original `&PriceSet`; consumers that ask for owned strings still receive
/// freshly allocated ones, exactly as before.
///
/// The index is deliberately built around references: `advance` and `run_day`
/// can keep their caller-owned `PriceSet` and pass a borrow here. Dropping the
/// owned copy removes the second full window of strings from the settlement
/// peak; the two BTreeMaps now hold `u32` keys plus a `Copy` quote.
#[derive(Clone, Debug)]
pub struct IndexedPriceSet<'a> {
    set: &'a PriceSet,
    symbols: Vec<String>,
    days: Vec<String>,
    by_day: std::collections::BTreeMap<u32, std::collections::BTreeMap<u32, Quote>>,
    by_symbol: std::collections::BTreeMap<u32, std::collections::BTreeMap<u32, Quote>>,
    /// Point-in-time factors for factor scoring.
    ///
    /// Carried with the index rather than threaded through every call: the
    /// factor is a property of the input series, and `run_day_indexed` already
    /// takes the series. An empty book means "no known actions", which is a
    /// factor of one, not a missing input.
    adjustments: crate::domain::AdjustmentBook,
}

impl<'a> IndexedPriceSet<'a> {
    pub fn new(set: &'a PriceSet) -> Self {
        Self::with_adjustments(set, crate::domain::AdjustmentBook::identity())
    }

    /// Build the index with corporate-action factors attached.
    pub fn with_adjustments(set: &'a PriceSet, adjustments: crate::domain::AdjustmentBook) -> Self {
        let mut indexed = Self::index(set);
        indexed.adjustments = adjustments;
        indexed
    }

    /// Factors in force for this series; empty means unadjusted scoring.
    pub fn adjustments(&self) -> &crate::domain::AdjustmentBook {
        &self.adjustments
    }

    fn index(set: &'a PriceSet) -> Self {
        let mut distinct_symbols = std::collections::BTreeSet::new();
        let mut distinct_days = std::collections::BTreeSet::new();
        for entry in &set.entries {
            distinct_symbols.insert(entry.symbol.as_str());
            distinct_days.insert(entry.day.as_str());
        }
        let symbols: Vec<String> = distinct_symbols.into_iter().map(str::to_string).collect();
        let days: Vec<String> = distinct_days.into_iter().map(str::to_string).collect();
        // Both dictionaries are sorted, so one binary search locates the
        // integer key for any symbol or day without a second string map.
        let symbol_key = |symbol: &str| -> u32 {
            symbols.binary_search_by(|probe| probe.as_str().cmp(symbol)).unwrap_or(0) as u32
        };
        let day_key = |day: &str| -> u32 {
            days.binary_search_by(|probe| probe.as_str().cmp(day)).unwrap_or(0) as u32
        };
        let mut by_day: std::collections::BTreeMap<u32, std::collections::BTreeMap<u32, Quote>> =
            std::collections::BTreeMap::new();
        let mut by_symbol: std::collections::BTreeMap<u32, std::collections::BTreeMap<u32, Quote>> =
            std::collections::BTreeMap::new();
        for entry in &set.entries {
            let quote = Quote {
                price_cents: entry.price_cents,
                open_cents: entry.open_cents,
                high_cents: entry.high_cents,
                low_cents: entry.low_cents,
                volume_shares: entry.volume_shares,
                tradable: entry.tradable,
                can_buy: entry.can_buy,
                can_sell: entry.can_sell,
            };
            let symbol = symbol_key(&entry.symbol);
            let day = day_key(&entry.day);
            by_day.entry(day).or_default().insert(symbol, quote);
            by_symbol.entry(symbol).or_default().insert(day, quote);
        }
        Self {
            set,
            symbols,
            days,
            by_day,
            by_symbol,
            adjustments: crate::domain::AdjustmentBook::identity(),
        }
    }

    fn keys(&self, symbol: &str, day: &str) -> Option<(u32, u32)> {
        let symbol = self
            .symbols
            .binary_search_by(|probe| probe.as_str().cmp(symbol))
            .ok()? as u32;
        let day = self
            .days
            .binary_search_by(|probe| probe.as_str().cmp(day))
            .ok()? as u32;
        Some((symbol, day))
    }

    fn quote(&self, symbol: &str, day: &str) -> Option<&Quote> {
        let (symbol, day) = self.keys(symbol, day)?;
        self.by_day.get(&day)?.get(&symbol)
    }

    fn day_key(&self, day: &str) -> Option<u32> {
        self.days
            .binary_search_by(|probe| probe.as_str().cmp(day))
            .ok()
            .map(|key| key as u32)
    }

    fn symbol_key(&self, symbol: &str) -> Option<u32> {
        self.symbols
            .binary_search_by(|probe| probe.as_str().cmp(symbol))
            .ok()
            .map(|key| key as u32)
    }

    /// The valuation mark for a held symbol, regardless of tradeability.
    pub fn valuation(&self, symbol: &str, day: &str) -> Option<i64> {
        self.quote(symbol, day).map(|quote| quote.price_cents)
    }

    /// Whether `symbol` has a real, fillable close on `day`.
    pub fn is_tradable(&self, symbol: &str, day: &str) -> bool {
        self.quote(symbol, day).is_some_and(|quote| quote.tradable)
    }

    /// Whether a purchase could fill: a real close and no limit-up seal.
    pub fn can_buy(&self, symbol: &str, day: &str) -> bool {
        self.quote(symbol, day)
            .is_some_and(|quote| quote.tradable && quote.can_buy)
    }

    /// Whether a sale could fill: a real close and no limit-down seal.
    pub fn can_sell(&self, symbol: &str, day: &str) -> bool {
        self.quote(symbol, day)
            .is_some_and(|quote| quote.tradable && quote.can_sell)
    }

    /// Tradable closes with the buy side open.
    pub fn buyable_closes_on(&self, day: &str) -> std::collections::BTreeMap<String, i64> {
        self.tradable_closes_on(day)
            .into_iter()
            .filter(|(symbol, _)| self.can_buy(symbol, day))
            .collect()
    }

    /// Tradable closes with the sell side open.
    pub fn sellable_closes_on(&self, day: &str) -> std::collections::BTreeMap<String, i64> {
        self.tradable_closes_on(day)
            .into_iter()
            .filter(|(symbol, _)| self.can_sell(symbol, day))
            .collect()
    }

    /// All tradable closes for one day, as symbol -> price.
    ///
    /// Halted valuations stay out: a signal computed from a carried reference
    /// price would invent a trade that could not happen.
    pub fn tradable_closes_on(&self, day: &str) -> std::collections::BTreeMap<String, i64> {
        let Some(day) = self.day_key(day) else {
            return std::collections::BTreeMap::new();
        };
        self.by_day
            .get(&day)
            .map(|closes| {
                closes
                    .iter()
                    .filter(|(_, quote)| quote.tradable)
                    .map(|(symbol, quote)| {
                        (self.symbols[*symbol as usize].clone(), quote.price_cents)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// One symbol's close history up to `through_day`, oldest first.
    ///
    /// Served from a per-symbol index instead of scanning every entry. The
    /// strategy needs a symbol's full history to compute a moving average, so
    /// this is called once per symbol per settled day; scanning ~4M entries
    /// each time is what made a multi-year advance take minutes of solid CPU.
    pub fn bars_for(&self, symbol: &str, through_day: &str) -> Vec<super::Bar> {
        let Some(symbol_key) = self.symbol_key(symbol) else {
            return Vec::new();
        };
        self.by_symbol
            .get(&symbol_key)
            .map(|series| {
                series
                    .iter()
                    .filter(|(day, _)| self.days[**day as usize].as_str() <= through_day)
                    .filter(|(_, quote)| quote.tradable)
                    .map(|(day, quote)| super::Bar {
                        symbol: symbol.to_string(),
                        day: self.days[*day as usize].clone(),
                        close_cents: quote.price_cents,
                        volume: quote.volume_shares,
                        open_cents: quote.open_cents,
                        high_cents: quote.high_cents,
                        low_cents: quote.low_cents,
                        tradable: true,
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Per-symbol close history through `through_day`, capped at the most
    /// recent `days` bars per symbol.
    ///
    /// Served straight from the per-symbol index rather than through
    /// `bars_for`, which allocates a `Bar` (and clones a symbol string) per
    /// row. The regime classifier only needs numbers, and on 5.5k symbols a
    /// settle day that is 5.5k x 120 needless allocations.
    pub fn recent_closes(
        &self,
        through_day: &str,
        days: usize,
    ) -> std::collections::BTreeMap<String, Vec<i64>> {
        let mut out = std::collections::BTreeMap::new();
        for (symbol, series) in &self.by_symbol {
            let mut closes: Vec<i64> = series
                .iter()
                .filter(|(day, _)| self.days[**day as usize].as_str() <= through_day)
                .filter(|(_, quote)| quote.tradable)
                .map(|(_, quote)| quote.price_cents)
                .collect();
            if closes.len() > days {
                closes.drain(..closes.len() - days);
            }
            if !closes.is_empty() {
                out.insert(self.symbols[*symbol as usize].clone(), closes);
            }
        }
        out
    }

    /// One symbol's bar on `day`, with its session shape intact.
    ///
    /// `bars_for` returns a series filtered to tradable sessions; this answers
    /// the single-day question a cross-sectional filter needs, and returns
    /// `None` when the symbol has no row that day.
    pub fn latest_bar(&self, symbol: &str, day: &str) -> Option<super::Bar> {
        let symbol_key = self.symbol_key(symbol)?;
        let day_key = self.day_key(day)?;
        let quote = self.by_symbol.get(&symbol_key)?.get(&day_key)?;
        Some(super::Bar {
            symbol: symbol.to_string(),
            day: day.to_string(),
            close_cents: quote.price_cents,
            volume: quote.volume_shares,
            open_cents: quote.open_cents,
            high_cents: quote.high_cents,
            low_cents: quote.low_cents,
            tradable: quote.tradable,
        })
    }

    pub fn set(&self) -> &PriceSet {
        self.set
    }

    pub fn into_set(self) -> &'a PriceSet {
        self.set
    }
}

impl<'a> std::ops::Deref for IndexedPriceSet<'a> {
    type Target = PriceSet;
    fn deref(&self) -> &PriceSet {
        self.set
    }
}

#[cfg(test)]
mod indexed_tests {
    use super::*;

    fn entry(symbol: &str, day: &str, price: i64, tradable: bool, buy: bool, sell: bool) -> PriceEntry {
        PriceEntry {
            open_cents: 0,
            high_cents: 0,
            low_cents: 0,
            volume_shares: 0,
            symbol: symbol.into(),
            day: day.into(),
            price_cents: price,
            source: "fixture".into(),
            observed_at: format!("{day}T15:00:00+08:00"),
            tradable,
            can_buy: buy,
            can_sell: sell,
        }
    }

    fn set() -> PriceSet {
        PriceSet {
            schema_version: 1,
            price_set_id: "index-memory".into(),
            market_data_pack_id: "fixture".into(),
            trust: "reviewed".into(),
            coverage_start: "2026-01-05".into(),
            coverage_end: "2026-01-07".into(),
            entries: vec![
                entry("000001.SZ", "2026-01-05", 1000, true, true, true),
                entry("600000.SH", "2026-01-05", 2000, true, true, true),
                entry("000001.SZ", "2026-01-06", 1100, true, false, true),
                entry("600000.SH", "2026-01-06", 2100, false, true, false),
                entry("000001.SZ", "2026-01-07", 1200, true, true, false),
                entry("600000.SH", "2026-01-07", 2200, true, true, true),
            ],
            source_digest: "a".repeat(64),
        }
    }

    #[test]
    fn integer_key_index_preserves_all_read_semantics() {
        let raw = set();
        let index = IndexedPriceSet::new(&raw);
        // `set()` and Deref must still expose the original PriceSet.
        assert_eq!(index.set(), &raw);
        assert_eq!(index.entries.len(), raw.entries.len());
        assert_eq!(index.coverage_end, "2026-01-07");
        assert_eq!(index.into_set(), &raw);

        let index = IndexedPriceSet::new(&raw);
        assert_eq!(index.valuation("600000.SH", "2026-01-06"), Some(2100));
        assert_eq!(index.valuation("600000.SH", "2026-01-01"), None);
        assert!(!index.is_tradable("600000.SH", "2026-01-06"));
        assert!(!index.can_buy("000001.SZ", "2026-01-06"));
        assert!(index.can_sell("000001.SZ", "2026-01-06"));
        assert!(!index.can_sell("000001.SZ", "2026-01-07"));
        assert_eq!(index.tradable_closes_on("2026-01-06").len(), 1);
        assert_eq!(index.tradable_closes_on("2026-01-06")["000001.SZ"], 1100);
        assert_eq!(index.buyable_closes_on("2026-01-05").len(), 2);
        assert_eq!(index.sellable_closes_on("2026-01-07")["600000.SH"], 2200);

        let bars = index.bars_for("000001.SZ", "2026-01-07");
        assert_eq!(bars.len(), 3);
        assert_eq!(bars[0].day, "2026-01-05");
        assert_eq!(bars[2].close_cents, 1200);
        assert!(index.bars_for("missing", "2026-01-07").is_empty());
        let recent = index.recent_closes("2026-01-07", 2);
        assert_eq!(recent["000001.SZ"], vec![1100, 1200]);
        // The halted row stays out of closes, but is still a valuation mark.
        assert_eq!(recent["600000.SH"], vec![2000, 2200]);
    }

    #[test]
    fn indexing_holds_one_string_per_distinct_symbol_and_day() {
        let raw = set();
        let index = IndexedPriceSet::new(&raw);
        assert_eq!(index.symbols, vec!["000001.SZ", "600000.SH"]);
        assert_eq!(index.days, vec!["2026-01-05", "2026-01-06", "2026-01-07"]);
        // Negative control: the two maps must not contain owned String keys.
        assert_eq!(std::mem::size_of_val(&index.by_day), std::mem::size_of_val(&index.by_symbol));
    }
}

impl PriceSet {
    /// Build a settlement view over the `bar` table so the kernel can settle
    /// against whole-market data instead of a hand-published rectangle.
    ///
    /// Only `adjust = "none"` rows are eligible. Forward-adjusted (qfq) rows
    /// have had ex-dividend events from the future folded into their history,
    /// so settling on them would book gains and costs nobody could have
    /// realised at the time.
    ///
    /// Which symbols are eligible, and over what sub-window.
    ///
    /// The reviewed-set model required a complete rectangle: every symbol had
    /// a close on every declared day, or the whole batch was rejected. At two
    /// symbols that is fine. Across the whole market it is impossible --
    /// measured on the real feed, of 5573 symbols only **one** has a close on
    /// all 930 trading days, because listings, delistings and suspensions are
    /// ordinary events.
    ///
    /// So completeness is judged per symbol over the span *it* covers, and a
    /// symbol joins the set from its first close to its last. Days outside a
    /// symbol's own span are not holes; days inside it are, and a symbol with
    /// an interior hole is dropped rather than carried at a stale price.
    ///
    /// A halted row is kept separately: its price is the official previous
    /// close, used only to value a position that was already held. The symbol
    /// remains complete over its own span so a halt does not erase its history,
    /// but its score, market-regime input and fills exclude the halted day.
    pub fn from_bars(
        bars: &[BarRow],
        price_set_id: impl Into<String>,
        coverage_start: &str,
        coverage_end: &str,
    ) -> Self {
        use std::collections::{BTreeMap, BTreeSet};

        let in_window = bars
            .iter()
            .filter(|row| {
                row.day.as_str() >= coverage_start
                    && row.day.as_str() <= coverage_end
                    && row.price_cents > 0
            })
            .collect::<Vec<_>>();

        // Every trading day the window actually contains, taken from all input
        // rows. Deriving this from the filtered set would shrink the window
        // whenever a whole day is missing and make survivors look complete.
        let market_days: Vec<&str> = {
            let set: BTreeSet<&str> = bars
                .iter()
                .map(|row| row.day.as_str())
                .filter(|day| *day >= coverage_start && *day <= coverage_end)
                .collect();
            set.into_iter().collect()
        };

        let mut by_symbol: BTreeMap<&str, BTreeMap<&str, Session>> = BTreeMap::new();
        for row in in_window.iter().copied() {
            let session = Session {
                price_cents: row.price_cents,
                open_cents: row.open_cents,
                high_cents: row.high_cents,
                low_cents: row.low_cents,
                volume_shares: row.volume_shares,
                tradable: row.tradable,
                tradeability: super::tradeability(
                    &row.symbol,
                    row.prev_close_cents,
                    row.open_cents,
                    row.high_cents,
                    row.low_cents,
                    row.price_cents,
                    row.tradable,
                ),
            };
            by_symbol
                .entry(row.symbol.as_str())
                .or_default()
                .insert(row.day.as_str(), session);
        }

        let mut entries = Vec::new();
        for (symbol, closes) in by_symbol {
            let Some(first) = closes.keys().next() else {
                continue;
            };
            let Some(last) = closes.keys().next_back() else {
                continue;
            };
            // Days this symbol is expected to have traded on: market days
            // inside its own [first, last] span.
            let expected: Vec<&str> = market_days
                .iter()
                .copied()
                .filter(|day| *day >= *first && *day <= *last)
                .collect();
            if expected.len() != closes.len() {
                // Interior hole: no row at all on a day the market traded
                // while this symbol was already live. A halt must be ingested
                // as an explicit valuation-only row, not inferred from a gap.
                continue;
            }
            for (day, session) in closes {
                entries.push(PriceEntry {
                    symbol: symbol.to_string(),
                    day: day.to_string(),
                    price_cents: session.price_cents,
                    source: "bar/tdx".into(),
                    observed_at: format!("{day}T15:00:00+08:00"),
                    tradable: session.tradable,
                    can_buy: session.tradeability.can_buy,
                    can_sell: session.tradeability.can_sell,
                    open_cents: session.open_cents,
                    high_cents: session.high_cents,
                    low_cents: session.low_cents,
                    volume_shares: session.volume_shares,
                });
            }
        }

        Self {
            schema_version: 1,
            price_set_id: price_set_id.into(),
            market_data_pack_id: "bar".into(),
            trust: "reviewed".into(),
            coverage_start: coverage_start.to_string(),
            coverage_end: coverage_end.to_string(),
            source_digest: digest_of(&entries),
            entries,
        }
    }

    /// Reject anything the paper ledger cannot use safely. Loading is not a
    /// repair step: malformed coverage, duplicate observations and non-positive
    /// prices fail closed instead of being skipped or interpolated.
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != 1 {
            return Err(RuleError("unsupported_price_set_schema".into()));
        }
        if self.price_set_id.trim().is_empty() || self.market_data_pack_id.trim().is_empty() {
            return Err(RuleError("invalid_price_set_identity".into()));
        }
        let allow_synthetic = std::env::var("ASTOCK_ALLOW_SYNTHETIC").as_deref() == Ok("1");
        if self.trust != "reviewed" && !(allow_synthetic && self.trust == "synthetic") {
            return Err(RuleError("price_set_not_reviewed".into()));
        }
        if !is_day(&self.coverage_start) || !is_day(&self.coverage_end) {
            return Err(RuleError("invalid_price_set_coverage".into()));
        }
        if self.coverage_start > self.coverage_end {
            return Err(RuleError("invalid_price_set_coverage".into()));
        }
        if self.source_digest.len() != 64
            || !self.source_digest.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(RuleError("invalid_price_set_digest".into()));
        }
        if self.entries.is_empty() {
            return Err(RuleError("empty_price_set".into()));
        }
        let mut seen = std::collections::BTreeSet::new();
        for entry in &self.entries {
            if entry.symbol.trim().is_empty()
                || !is_day(&entry.day)
                || entry.price_cents <= 0
                || entry.source.trim().is_empty()
                || entry.observed_at.trim().is_empty()
            {
                return Err(RuleError("invalid_price_entry".into()));
            }
            if entry.day < self.coverage_start || entry.day > self.coverage_end {
                return Err(RuleError("price_entry_outside_coverage".into()));
            }
            if !seen.insert((entry.symbol.as_str(), entry.day.as_str())) {
                return Err(RuleError("duplicate_price_entry".into()));
            }
        }
        // A reviewed set is a complete rectangle: every declared symbol must
        // have one close on every date present in the set. Sparse input must
        // not become an accidental "hold" or a silently shortened window.
        // Weekends and holidays are absent by construction, so the dates are
        // taken from the data rather than from the calendar span.
        let symbols = self.symbols();
        let dates = self.days();
        if symbols.len() * dates.len() != self.entries.len()
            || dates.first() != Some(&self.coverage_start)
            || dates.last() != Some(&self.coverage_end)
        {
            return Err(RuleError("incomplete_price_set".into()));
        }
        Ok(())
    }

    /// The close used to decide and fill a trade.
    ///
    /// A halted row is deliberately invisible here; only `valuation` may read
    /// its carried reference price.
    pub fn tradable_close(&self, symbol: &str, day: &str) -> Option<i64> {
        self.entries
            .iter()
            .find(|entry| entry.symbol == symbol && entry.day == day && entry.tradable)
            .map(|entry| entry.price_cents)
    }

    /// The mark used only to value a position that is already held.
    pub fn valuation(&self, symbol: &str, day: &str) -> Option<i64> {
        self.entries
            .iter()
            .find(|entry| entry.symbol == symbol && entry.day == day)
            .map(|entry| entry.price_cents)
    }

    pub fn days(&self) -> Vec<String> {
        let mut days: Vec<String> = self.entries.iter().map(|entry| entry.day.clone()).collect();
        days.sort();
        days.dedup();
        days
    }

    pub fn contains_day(&self, day: &str) -> bool {
        self.entries.iter().any(|entry| entry.day == day)
    }

    pub fn symbol_days(&self, symbol: &str) -> Vec<String> {
        let mut days: Vec<String> = self
            .entries
            .iter()
            .filter(|entry| entry.symbol == symbol)
            .map(|entry| entry.day.clone())
            .collect();
        days.sort();
        days
    }

    pub fn symbols(&self) -> Vec<String> {
        let mut symbols: Vec<String> = self.entries.iter().map(|entry| entry.symbol.clone()).collect();
        symbols.sort();
        symbols.dedup();
        symbols
    }
}

/// Deterministic 64-hex-digit digest over the sorted entry list.
///
/// `PriceSet::validate` requires a 64-character hex `source_digest`, and it
/// exists so a price set can be identified by its content. This is FNV-1a
/// expanded to 32 bytes by re-hashing with a rotating offset -- not a
/// cryptographic hash, and not treated as one. Its only job is to make two
/// differently-populated sets compare unequal and identical ones compare equal.
fn digest_of(entries: &[PriceEntry]) -> String {
    let mut state: u64 = 0xcbf2_9ce4_8422_2325;
    let feed = |bytes: &[u8], state: &mut u64| {
        for byte in bytes {
            *state ^= u64::from(*byte);
            *state = state.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    // Entries are already emitted in (symbol, day) order from the BTreeMap.
    for entry in entries {
        feed(entry.symbol.as_bytes(), &mut state);
        feed(b"|", &mut state);
        feed(entry.day.as_bytes(), &mut state);
        feed(b"|", &mut state);
        feed(&entry.price_cents.to_le_bytes(), &mut state);
        if !entry.tradable {
            // Keep every existing tradable-set digest stable: the halt marker
            // is appended only where it distinguishes content.
            feed(b"|halted", &mut state);
        }
    }
    let mut out = String::with_capacity(64);
    let mut round = state;
    for index in 0..4 {
        // Mix the round index so the four 16-digit blocks are not identical.
        round ^= (index as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        round = round.wrapping_mul(0x0000_0100_0000_01b3).rotate_left(17);
        out.push_str(&format!("{round:016x}"));
    }
    out
}

fn is_day(value: &str) -> bool {
    value.len() == 10
        && value.as_bytes()[4] == b'-'
        && value.as_bytes()[7] == b'-'
        && value
            .chars()
            .enumerate()
            .all(|(index, ch)| index == 4 || index == 7 || ch.is_ascii_digit())
        && chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d").is_ok()
}

#[cfg(test)]
mod from_bars_tests {
    use super::*;

    fn row(symbol: &str, day: &str, close: i64) -> BarRow {
        BarRow {
            symbol: symbol.to_string(),
            day: day.to_string(),
            price_cents: close,
            tradable: true,
            open_cents: 0,
            high_cents: 0,
            low_cents: 0,
            prev_close_cents: 0,
            volume_shares: 0,
        }
    }

    fn halted(symbol: &str, day: &str, reference: i64) -> BarRow {
        BarRow {
            symbol: symbol.to_string(),
            day: day.to_string(),
            price_cents: reference,
            tradable: false,
            open_cents: 0,
            high_cents: 0,
            low_cents: 0,
            prev_close_cents: 0,
            volume_shares: 0,
        }
    }

    #[test]
    fn builds_entries_for_complete_symbols() {
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("000001.SZ", "2026-11-09", 1_186),
            row("000001.SZ", "2026-11-10", 1_190),
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-10");
        set.validate().expect("valid price set");
        assert_eq!(set.days(), vec!["2026-11-09", "2026-11-10"]);
        assert_eq!(set.symbols(), vec!["000001.SZ", "600000.SH"]);
        assert_eq!(set.tradable_close("600000.SH", "2026-11-10"), Some(1_020));
    }

    #[test]
    fn drops_symbols_with_an_interior_hole() {
        // A missing close is not a halt. Carrying the previous price forward
        // would invent a trade that never happened, so the symbol is dropped.
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("000001.SZ", "2026-11-09", 1_186),
            // 000001.SZ is missing 2026-11-10 but trades again on 11-11, so
            // the hole is interior and the symbol cannot be settled.
            row("000001.SZ", "2026-11-11", 1_190),
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-11");
        assert_eq!(set.symbols(), vec!["600000.SH"]);
    }

    #[test]
    fn keeps_a_symbol_that_lists_mid_window() {
        // Ordinary listings must not be mistaken for holes. This symbol simply
        // was not trading before 2026-11-10.
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("600000.SH", "2026-11-11", 1_030),
            row("301999.SZ", "2026-11-10", 2_500),
            row("301999.SZ", "2026-11-11", 2_600),
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-11");
        assert_eq!(set.symbols(), vec!["301999.SZ", "600000.SH"]);
        assert_eq!(set.tradable_close("301999.SZ", "2026-11-09"), None);
        assert_eq!(set.tradable_close("301999.SZ", "2026-11-11"), Some(2_600));
    }

    #[test]
    fn drops_a_symbol_that_delists_mid_window_with_a_gap() {
        // Ends early with no gap: kept. Ends early *and* skips days it should
        // have traded: dropped.
        let clean = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("600000.SH", "2026-11-11", 1_030),
            row("600999.SH", "2026-11-09", 900),
        ];
        let set = PriceSet::from_bars(&clean, "t", "2026-11-09", "2026-11-11");
        assert_eq!(set.symbols(), vec!["600000.SH", "600999.SH"]);

        let gapped = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("600000.SH", "2026-11-11", 1_030),
            row("600999.SH", "2026-11-09", 900),
            // skips 11-10 (market traded) then appears on 11-11
            row("600999.SH", "2026-11-11", 910),
        ];
        let set = PriceSet::from_bars(&gapped, "t", "2026-11-09", "2026-11-11");
        assert_eq!(set.symbols(), vec!["600000.SH"]);
    }

    #[test]
    fn respects_the_coverage_window() {
        let rows = vec![
            row("600000.SH", "2026-11-06", 999),  // before window
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("600000.SH", "2026-11-11", 1_030), // after window
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-10");
        assert_eq!(set.days(), vec!["2026-11-09", "2026-11-10"]);
        assert_eq!(set.tradable_close("600000.SH", "2026-11-06"), None);
        assert_eq!(set.tradable_close("600000.SH", "2026-11-11"), None);
    }

    #[test]
    fn a_zero_close_is_a_hole_not_a_price() {
        // 0 is what a halted symbol often reports. Treating it as a price
        // would book a -100% move, so it must be a hole. Here the zero sits
        // between two real closes, making the hole interior -> dropped.
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 0),
            row("600000.SH", "2026-11-11", 1_020),
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-11");
        assert!(set.symbols().is_empty());
    }

    #[test]
    fn a_trailing_zero_close_is_also_a_hole() {
        // The zero is the last row, so it defines the symbol's span. Excluding
        // it from the span would silently accept a symbol whose final day has
        // no usable price, which is how a halted position gets valued at 0.
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("600000.SH", "2026-11-11", 0),
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-11");
        assert_eq!(set.symbols(), vec!["600000.SH"]);
        assert_eq!(set.tradable_close("600000.SH", "2026-11-11"), None);
    }

    #[test]
    fn a_halted_row_values_a_position_but_cannot_be_filled_or_scored() {
        let source = vec![
            row("600000.SH", "2026-11-09", 1_010),
            halted("600000.SH", "2026-11-10", 1_005),
            row("600000.SH", "2026-11-11", 1_020),
        ];
        let set = PriceSet::from_bars(&source, "t", "2026-11-09", "2026-11-11");
        set.validate().expect("explicit halt keeps the row");
        assert_eq!(set.tradable_close("600000.SH", "2026-11-10"), None);
        assert_eq!(set.valuation("600000.SH", "2026-11-10"), Some(1_005));
        assert!(
            !set.entries
                .iter()
                .find(|entry| entry.day == "2026-11-10")
                .expect("halted row")
                .tradable
        );
    }

    #[test]
    fn a_halted_trailing_row_does_not_become_a_fill_price() {
        let source = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            halted("600000.SH", "2026-11-11", 1_020),
        ];
        let set = PriceSet::from_bars(&source, "t", "2026-11-09", "2026-11-11");
        assert_eq!(set.tradable_close("600000.SH", "2026-11-11"), None);
        assert_eq!(set.valuation("600000.SH", "2026-11-11"), Some(1_020));
    }

    #[test]
    fn empty_input_produces_empty_set() {
        let set = PriceSet::from_bars(&[], "t", "2026-11-09", "2026-11-10");
        assert!(set.symbols().is_empty());
        assert!(set.days().is_empty());
    }
}
