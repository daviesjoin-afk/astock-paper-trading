//! Valuation: reviewed price sets and the pure marking of positions to market.
//!
//! No clock, no storage, no network. A price set is explicit data; a valuation
//! either covers every held symbol on the requested day or refuses entirely.

use serde::{Deserialize, Serialize};

use crate::domain::{canonical_digest, Fill, PortfolioState, RuleError};

pub const PRICE_SET_SCHEMA_VERSION: u32 = 1;
pub const TRUST_REVIEWED: &str = "reviewed";
pub const TRUST_SYNTHETIC: &str = "synthetic";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceEntry {
    pub symbol: String,
    /// China trading day, `YYYY-MM-DD`.
    pub day: String,
    pub price_cents: i64,
    pub source: String,
    pub observed_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceSet {
    pub schema_version: u32,
    pub price_set_id: String,
    pub market_data_pack_id: String,
    /// `reviewed` may produce profit fields; `synthetic` never may.
    pub trust: String,
    pub coverage_start: String,
    pub coverage_end: String,
    pub entries: Vec<PriceEntry>,
    pub source_digest: String,
}

impl PriceSet {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != PRICE_SET_SCHEMA_VERSION {
            return Err(RuleError("unsupported_price_set_version".into()));
        }
        if self.price_set_id.trim().is_empty() || self.market_data_pack_id.trim().is_empty() {
            return Err(RuleError("invalid_price_set_identity".into()));
        }
        if self.trust != TRUST_REVIEWED && self.trust != TRUST_SYNTHETIC {
            return Err(RuleError("invalid_price_set_trust".into()));
        }
        let start = chrono::NaiveDate::parse_from_str(&self.coverage_start, "%Y-%m-%d")
            .map_err(|_| RuleError("invalid_price_set_coverage".into()))?;
        let end = chrono::NaiveDate::parse_from_str(&self.coverage_end, "%Y-%m-%d")
            .map_err(|_| RuleError("invalid_price_set_coverage".into()))?;
        if start > end {
            return Err(RuleError("invalid_price_set_coverage".into()));
        }
        if self.entries.is_empty() {
            return Err(RuleError("empty_price_set".into()));
        }
        let mut seen = std::collections::BTreeSet::new();
        for entry in &self.entries {
            let day = chrono::NaiveDate::parse_from_str(&entry.day, "%Y-%m-%d")
                .map_err(|_| RuleError("invalid_price_set_day".into()))?;
            if day < start || day > end {
                return Err(RuleError("price_entry_outside_coverage".into()));
            }
            if entry.price_cents <= 0 {
                return Err(RuleError("invalid_price_entry".into()));
            }
            if entry.symbol.trim().is_empty() || entry.source.trim().is_empty() {
                return Err(RuleError("invalid_price_entry".into()));
            }
            if !seen.insert((entry.symbol.clone(), entry.day.clone())) {
                return Err(RuleError("duplicate_price_entry".into()));
            }
        }
        Ok(())
    }

    pub fn digest(&self) -> String {
        match serde_json::to_value(self) {
            Ok(value) => canonical_digest(&value),
            Err(_) => String::new(),
        }
    }

    fn price_on(&self, symbol: &str, day: &str) -> Option<i64> {
        self.entries
            .iter()
            .find(|entry| entry.symbol == symbol && entry.day == day)
            .map(|entry| entry.price_cents)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PositionValue {
    pub symbol: String,
    pub quantity: i64,
    pub price_cents: i64,
    pub market_value_cents: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountValuation {
    pub status: String,
    pub trust: String,
    pub as_of_day: String,
    pub price_set_id: String,
    pub price_set_digest: String,
    pub positions: Vec<PositionValue>,
    pub market_value_cents: i64,
    pub cash_cents: i64,
    pub equity_cents: i64,
    /// Only a reviewed price set may state profit; synthetic sets never do.
    pub profit_cents: Option<i64>,
}

/// Mark a book to market. Any held symbol missing from the set, or a pack
/// mismatch, refuses the whole valuation instead of valuing a subset.
pub fn value_account(
    initial_cash_cents: i64,
    cash_cents: i64,
    positions: &[(String, i64)],
    set: &PriceSet,
    as_of_day: &str,
    expected_pack_id: &str,
) -> Result<AccountValuation, RuleError> {
    set.validate()?;
    if set.market_data_pack_id != expected_pack_id {
        return Err(RuleError("valuation_pack_mismatch".into()));
    }
    let as_of = chrono::NaiveDate::parse_from_str(as_of_day, "%Y-%m-%d")
        .map_err(|_| RuleError("invalid_valuation_day".into()))?;
    let start = chrono::NaiveDate::parse_from_str(&set.coverage_start, "%Y-%m-%d")
        .map_err(|_| RuleError("invalid_price_set_coverage".into()))?;
    let end = chrono::NaiveDate::parse_from_str(&set.coverage_end, "%Y-%m-%d")
        .map_err(|_| RuleError("invalid_price_set_coverage".into()))?;
    if as_of < start || as_of > end {
        return Err(RuleError("valuation_day_outside_price_set".into()));
    }

    let mut valued = Vec::new();
    let mut market_value_cents = 0i64;
    for (symbol, quantity) in positions {
        if *quantity <= 0 {
            continue;
        }
        let Some(price_cents) = set.price_on(symbol, as_of_day) else {
            return Err(RuleError("valuation_missing_symbol".into()));
        };
        let value = quantity
            .checked_mul(price_cents)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        market_value_cents = market_value_cents
            .checked_add(value)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        valued.push(PositionValue {
            symbol: symbol.clone(),
            quantity: *quantity,
            price_cents,
            market_value_cents: value,
        });
    }
    let equity_cents = cash_cents
        .checked_add(market_value_cents)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    let profit_cents = if set.trust == TRUST_REVIEWED {
        Some(equity_cents - initial_cash_cents)
    } else {
        None
    };
    Ok(AccountValuation {
        status: "valued".into(),
        trust: set.trust.clone(),
        as_of_day: as_of_day.into(),
        price_set_id: set.price_set_id.clone(),
        price_set_digest: set.digest(),
        positions: valued,
        market_value_cents,
        cash_cents,
        equity_cents,
        profit_cents,
    })
}

/// One publishable price set in the catalogue.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PriceSetSummary {
    pub price_set_id: String,
    pub market_data_pack_id: String,
    pub trust: String,
    pub coverage_start: String,
    pub coverage_end: String,
    pub entries: usize,
    pub digest: String,
}

/// The reviewed catalogue, loaded from a bundled JSON artifact. It contains
/// data only: no runtime write path can add a set.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PriceSetRegistry {
    pub schema_version: u32,
    pub sets: Vec<PriceSet>,
}

impl PriceSetRegistry {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != PRICE_SET_SCHEMA_VERSION {
            return Err(RuleError("unsupported_price_registry_version".into()));
        }
        let mut seen = std::collections::BTreeSet::new();
        for set in &self.sets {
            set.validate()?;
            if !seen.insert(set.price_set_id.clone()) {
                return Err(RuleError("duplicate_price_set_id".into()));
            }
        }
        Ok(())
    }

    pub fn summaries(&self) -> Vec<PriceSetSummary> {
        self.sets.iter().map(|set| PriceSetSummary {
            price_set_id: set.price_set_id.clone(),
            market_data_pack_id: set.market_data_pack_id.clone(),
            trust: set.trust.clone(),
            coverage_start: set.coverage_start.clone(),
            coverage_end: set.coverage_end.clone(),
            entries: set.entries.len(),
            digest: set.digest(),
        }).collect()
    }

    /// Only a reviewed set that binds the pack and covers the day may be used
    /// for a user-facing valuation.
    pub fn select(&self, pack_id: &str, day: &str) -> Option<&PriceSet> {
        let as_of = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d").ok()?;
        self.sets.iter().find(|set| {
            if set.trust != TRUST_REVIEWED || set.market_data_pack_id != pack_id {
                return false;
            }
            let Ok(start) = chrono::NaiveDate::parse_from_str(&set.coverage_start, "%Y-%m-%d") else { return false };
            let Ok(end) = chrono::NaiveDate::parse_from_str(&set.coverage_end, "%Y-%m-%d") else { return false };
            start <= as_of && as_of <= end
        })
    }
}

/// One day of an account equity series.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EquityPoint {
    pub day: String,
    pub cash_cents: i64,
    pub market_value_cents: i64,
    pub equity_cents: i64,
}

/// A complete, reviewed valuation series. Missing coverage refuses the whole
/// series: days are never interpolated or inferred.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EquitySeries {
    pub status: String,
    pub trust: String,
    pub price_set_id: String,
    pub points: Vec<EquityPoint>,
    pub max_drawdown_cents: i64,
    pub max_drawdown_day: Option<String>,
    /// Only a reviewed price set may state profit.
    pub profit_cents: Option<i64>,
    pub digest: String,
}

impl PriceSetRegistry {
    /// Select a reviewed set that covers every requested day for one pack.
    pub fn select_covering(&self, pack_id: &str, first: &str, last: &str) -> Option<&PriceSet> {
        let start = chrono::NaiveDate::parse_from_str(first, "%Y-%m-%d").ok()?;
        let end = chrono::NaiveDate::parse_from_str(last, "%Y-%m-%d").ok()?;
        self.sets.iter().find(|set| {
            if set.trust != TRUST_REVIEWED || set.market_data_pack_id != pack_id {
                return false;
            }
            let Ok(set_start) = chrono::NaiveDate::parse_from_str(&set.coverage_start, "%Y-%m-%d") else { return false };
            let Ok(set_end) = chrono::NaiveDate::parse_from_str(&set.coverage_end, "%Y-%m-%d") else { return false };
            set_start <= start && end <= set_end
        })
    }
}

/// Build a complete equity series by replaying fills up to each day and marking
/// the book to market with one reviewed price set.
pub fn equity_series(
    initial_cash_cents: i64,
    fills: &[Fill],
    set: &PriceSet,
    days: &[String],
    expected_pack_id: &str,
) -> Result<EquitySeries, RuleError> {
    set.validate()?;
    if set.market_data_pack_id != expected_pack_id {
        return Err(RuleError("valuation_pack_mismatch".into()));
    }
    if days.is_empty() {
        return Err(RuleError("empty_equity_series".into()));
    }
    let mut previous: Option<chrono::NaiveDate> = None;
    for day in days {
        let parsed = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .map_err(|_| RuleError("invalid_equity_day".into()))?;
        if let Some(previous) = previous {
            if parsed <= previous {
                return Err(RuleError("equity_days_must_be_increasing".into()));
            }
        }
        previous = Some(parsed);
    }

    let mut points = Vec::new();
    let mut applied = 0usize;
    let mut peak: Option<i64> = None;
    let mut max_drawdown_cents = 0i64;
    let mut max_drawdown_day: Option<String> = None;
    let mut profit_cents: Option<i64> = None;

    // One book replayed forward: fills stay applied across days.
    let mut state = PortfolioState::new(initial_cash_cents);
    for day in days {
        while applied < fills.len() {
            let fill = &fills[applied];
            if fill.trade_day.as_str() > day.as_str() {
                break;
            }
            state.apply(fill)?;
            applied += 1;
        }
        let mut quantities: std::collections::BTreeMap<String, i64> =
            std::collections::BTreeMap::new();
        for lot in &state.lots {
            *quantities.entry(lot.symbol.clone()).or_default() += lot.quantity;
        }
        let positions: Vec<(String, i64)> = quantities.into_iter().collect();
        let valued = value_account(
            initial_cash_cents,
            state.cash_cents,
            &positions,
            set,
            day,
            expected_pack_id,
        )?;
        let equity = valued.equity_cents;
        match peak {
            Some(current) if equity >= current => peak = Some(equity),
            Some(current) => {
                let drawdown = current - equity;
                if drawdown > max_drawdown_cents {
                    max_drawdown_cents = drawdown;
                    max_drawdown_day = Some(day.clone());
                }
            }
            None => peak = Some(equity),
        }
        if set.trust == TRUST_REVIEWED {
            profit_cents = Some(equity - initial_cash_cents);
        }
        points.push(EquityPoint {
            day: day.clone(),
            cash_cents: state.cash_cents,
            market_value_cents: valued.market_value_cents,
            equity_cents: equity,
        });
    }

    let mut series = EquitySeries {
        status: "valued".into(),
        trust: set.trust.clone(),
        price_set_id: set.price_set_id.clone(),
        points,
        max_drawdown_cents,
        max_drawdown_day,
        profit_cents,
        digest: String::new(),
    };
    let mut value = serde_json::to_value(&series)
        .map_err(|_| RuleError("equity_series_encoding_failed".into()))?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    series.digest = canonical_digest(&value);
    Ok(series)
}
