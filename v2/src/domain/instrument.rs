//! Fixed offline instrument identity and quantity-rule applicability only.
//! Consumed by execution; Store pins these inputs. No clock, IO or prices.
use super::contract::{Board, Exchange, InstrumentIdentity, QuoteTrust, SecurityType};
use super::RuleError;
use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

pub const INSTRUMENT_VERSION: &str = "cn-instruments-offline-v1";
pub const QUANTITY_RULE_VERSION: &str = "sse-main-board-whole-lot-2026-v1";
pub const BUNDLED_INSTRUMENTS_JSON: &str = include_str!("../../data/instruments-offline-v1.json");

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentSource {
    pub id: String,
    pub url: String,
    pub sha256: String,
    pub published_on: String,
    pub locator: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuantityRule {
    pub version: String,
    pub effective_from: String,
    pub covered_through: String,
    pub minimum: i64,
    pub step: i64,
    pub maximum: i64,
    pub source_ids: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentSnapshot {
    pub schema_version: u32,
    pub version: String,
    pub trust_level: QuoteTrust,
    pub coverage_start: String,
    pub coverage_end: String,
    pub sources: Vec<InstrumentSource>,
    pub instruments: Vec<InstrumentIdentity>,
    pub quantity_rules: Vec<QuantityRule>,
}

fn date(value: &str) -> Result<NaiveDate, RuleError> {
    let day = NaiveDate::parse_from_str(value, "%Y-%m-%d")
        .map_err(|_| RuleError("invalid_instrument_date".into()))?;
    if day.to_string() != value { return Err(RuleError("noncanonical_instrument_date".into())); }
    Ok(day)
}

impl InstrumentSnapshot {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != 1 || !matches!(self.version.as_str(), INSTRUMENT_VERSION | "cn-instruments-offline-v2") {
            return Err(RuleError("unsupported_instrument_snapshot".into()));
        }
        let start = date(&self.coverage_start)?;
        let end = date(&self.coverage_end)?;
        if start > end || self.sources.is_empty() || self.instruments.is_empty() {
            return Err(RuleError("incomplete_instrument_snapshot".into()));
        }
        let mut sources = BTreeSet::new();
        for source in &self.sources {
            if source.id.is_empty() || !sources.insert(source.id.as_str())
                || !source.url.starts_with("https://") || source.locator.is_empty()
                || source.sha256.len() != 64 || !source.sha256.bytes().all(|v| v.is_ascii_hexdigit())
                || date(&source.published_on)? > start
            { return Err(RuleError("invalid_instrument_source".into())); }
        }
        let sourced = |ids: &[String]| !ids.is_empty() && ids.iter().all(|id| sources.contains(id.as_str()));
        let mut symbols = BTreeSet::new();
        for item in &self.instruments {
            let first = date(&item.valid_from)?;
            let last = date(&item.valid_through)?;
            // Suffix consistency is a cross-check, never board/type inference.
            let suffix_ok = match item.exchange {
                Exchange::Sse => item.symbol.ends_with(".SH"),
                Exchange::Szse => item.symbol.ends_with(".SZ"),
                Exchange::Unknown => true,
            };
            if item.schema_version != 1 || item.evidence_version != self.version
                || item.symbol.len() != 9 || !item.symbol.as_bytes()[..6].iter().all(u8::is_ascii_digit)
                || !suffix_ok || !symbols.insert(item.symbol.as_str())
                || first > last || first < start || last > end || !sourced(&item.source_ids)
                || (item.board == Board::Star && item.exchange != Exchange::Sse)
                || (item.board == Board::ChiNext && item.exchange != Exchange::Szse)
            { return Err(RuleError("invalid_or_conflicting_instrument_identity".into())); }
        }
        let mut versions = BTreeSet::new();
        for rule in &self.quantity_rules {
            if rule.version.is_empty() || !versions.insert(rule.version.as_str())
                || date(&rule.effective_from)? > date(&rule.covered_through)?
                || rule.minimum <= 0 || rule.step <= 0 || rule.maximum < rule.minimum
                || rule.minimum % rule.step != 0 || rule.maximum % rule.step != 0
                || !sourced(&rule.source_ids)
                || (rule.version == QUANTITY_RULE_VERSION &&
                    (rule.minimum, rule.step, rule.maximum) != (100, 100, 1_000_000))
            { return Err(RuleError("invalid_instrument_quantity_rule".into())); }
        }
        Ok(())
    }

    /// Exact code lookup in explicitly classified records, never a prefix map.
    pub fn identity(&self, symbol: &str) -> Option<&InstrumentIdentity> {
        self.instruments.iter().find(|item| item.symbol == symbol)
    }

    /// Callers validate the snapshot before this pure applicability calculation.
    /// Missing evidence stays missing even when a later input knows the code.
    pub fn quantity_rule(
        &self, symbol: &str, pinned: Option<&InstrumentIdentity>, day: NaiveDate,
    ) -> Result<&QuantityRule, &'static str> {
        let identity = pinned.ok_or("instrument_identity_missing")?;
        if self.identity(symbol) != Some(identity) {
            return Err("instrument_identity_conflict");
        }
        // Dates were parsed by validate; compare canonical YYYY-MM-DD values.
        let day = day.to_string();
        if day < identity.valid_from || day > identity.valid_through {
            return Err("instrument_date_not_covered");
        }
        if identity.security_type != SecurityType::OrdinaryAShare {
            return Err("unsupported_security_type");
        }
        if identity.board != Board::MainBoard {
            return Err("unsupported_instrument_board");
        }
        if identity.exchange != Exchange::Sse {
            return Err("unsupported_instrument_exchange");
        }
        let version = identity.quantity_rule_version.as_deref().ok_or("instrument_rule_missing")?;
        if version != QUANTITY_RULE_VERSION { return Err("unsupported_instrument_rule"); }
        let rule = self.quantity_rules.iter().find(|rule| rule.version == version)
            .ok_or("instrument_rule_missing")?;
        if day < rule.effective_from || day > rule.covered_through {
            return Err("instrument_rule_date_not_covered");
        }
        Ok(rule)
    }
}
