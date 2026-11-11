//! Shared primitive types for instrument identity and quote trust.
//!
//! These were extracted from the old schema-15 `contract` module so that
//! `calendar` and `instrument` could be recovered without dragging in the
//! whole experiment/research contract surface. Keep this file small: it holds
//! only what identity and quantity rules actually need.

use serde::{Deserialize, Serialize};

/// No authenticated provider is implemented. Unknown grades fail deserialization.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QuoteTrust {
    OfflineFixture,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Exchange {
    Sse,
    Szse,
    Unknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Board {
    MainBoard,
    Star,
    ChiNext,
    NotApplicable,
    Unknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SecurityType {
    OrdinaryAShare,
    Etf,
    Other,
    Unknown,
}

/// Store-resolved offline classification; never accepted from research/import.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentIdentity {
    pub schema_version: u32,
    pub evidence_version: String,
    pub symbol: String,
    pub exchange: Exchange,
    pub board: Board,
    pub security_type: SecurityType,
    pub valid_from: String,
    pub valid_through: String,
    pub source_ids: Vec<String>,
    pub quantity_rule_version: Option<String>,
}
