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
