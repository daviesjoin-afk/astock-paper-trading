//! Reviewed close-price set: explicit source, day and whole-cent price.

use serde::{Deserialize, Serialize};

use super::RuleError;

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
    /// Symbols missing a close on any day in the window are dropped entirely
    /// rather than carried with a stale price -- a hole in a series is not a
    /// halt, and inventing a price for it is exactly the failure mode the
    /// reviewed-set model existed to prevent.
    ///
    /// Input is `(symbol, day, close_cents)` already filtered to the window.
    /// Keeping it a tuple avoids the domain depending on the storage type.
    pub fn from_bars(
        bars: &[(String, String, i64)],
        price_set_id: impl Into<String>,
        coverage_start: &str,
        coverage_end: &str,
    ) -> Self {
        use std::collections::{BTreeMap, BTreeSet};

        let in_window = bars
            .iter()
            .filter(|(_, day, close)| {
                day.as_str() >= coverage_start
                    && day.as_str() <= coverage_end
                    && *close > 0
            })
            .collect::<Vec<_>>();

        // The window's day set comes from *all* input rows, not just the
        // positive ones. Deriving it from the filtered set would silently
        // shrink the window when a whole day is missing or zero-priced, and
        // then every remaining symbol would look complete.
        let days: BTreeSet<&str> = bars
            .iter()
            .map(|(_, day, _)| day.as_str())
            .filter(|day| *day >= coverage_start && *day <= coverage_end)
            .collect();
        let mut by_symbol: BTreeMap<&str, BTreeMap<&str, i64>> = BTreeMap::new();
        for (symbol, day, close) in &in_window {
            by_symbol
                .entry(symbol.as_str())
                .or_default()
                .insert(day.as_str(), *close);
        }

        let mut entries = Vec::new();
        for (symbol, closes) in by_symbol {
            // Drop the symbol unless it has a close on every day in the window.
            if closes.len() != days.len() {
                continue;
            }
            for (day, price_cents) in closes {
                entries.push(PriceEntry {
                    symbol: symbol.to_string(),
                    day: day.to_string(),
                    price_cents,
                    source: "bar/tdx".into(),
                    observed_at: format!("{day}T15:00:00+08:00"),
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

    pub fn contains(&self, symbol: &str, day: &str) -> Option<i64> {
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

    pub fn bars_for(&self, symbol: &str, through_day: &str) -> Vec<super::Bar> {
        let mut bars: Vec<super::Bar> = self
            .entries
            .iter()
            .filter(|entry| entry.symbol == symbol && entry.day.as_str() <= through_day)
            .map(|entry| super::Bar {
                symbol: entry.symbol.clone(),
                day: entry.day.clone(),
                close_cents: entry.price_cents,
                volume: 0,
            })
            .collect();
        bars.sort_by(|left, right| left.day.cmp(&right.day));
        bars
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
    let mut feed = |bytes: &[u8], state: &mut u64| {
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

    fn row(symbol: &str, day: &str, close: i64) -> (String, String, i64) {
        (symbol.to_string(), day.to_string(), close)
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
        assert_eq!(set.contains("600000.SH", "2026-11-10"), Some(1_020));
    }

    #[test]
    fn drops_symbols_with_a_hole() {
        // A missing close is not a halt. Carrying the previous price forward
        // would invent a trade that never happened, so the symbol is dropped.
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 1_020),
            row("000001.SZ", "2026-11-09", 1_186),
            // 000001.SZ has no 2026-11-10 close
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-10");
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
        assert_eq!(set.contains("600000.SH", "2026-11-06"), None);
        assert_eq!(set.contains("600000.SH", "2026-11-11"), None);
    }

    #[test]
    fn drops_non_positive_closes() {
        let rows = vec![
            row("600000.SH", "2026-11-09", 1_010),
            row("600000.SH", "2026-11-10", 0),
        ];
        let set = PriceSet::from_bars(&rows, "t", "2026-11-09", "2026-11-10");
        // The zero leaves a hole, so the symbol cannot be settled.
        assert!(set.symbols().is_empty());
    }

    #[test]
    fn empty_input_produces_empty_set() {
        let set = PriceSet::from_bars(&[], "t", "2026-11-09", "2026-11-10");
        assert!(set.symbols().is_empty());
        assert!(set.days().is_empty());
    }
}
