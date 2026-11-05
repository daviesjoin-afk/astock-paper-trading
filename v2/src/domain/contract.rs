//! Strongly typed, versioned paper-only input/output contracts.
//! No database, HTTP, wall clock, IO, or legacy V1 dependency.
use serde::{Deserialize, Serialize};
use std::fmt;

pub const INITIAL_CASH_CENTS: i64 = 10_000_000;
pub const LOT_SIZE: i64 = 100;
pub const BUY_BUDGET_BPS: i64 = 1_000;
pub const COMMISSION_BPS: i64 = 3;
pub const MIN_COMMISSION_CENTS: i64 = 500;
pub const SELL_STAMP_DUTY_BPS: i64 = 5;
pub const MAX_QUOTE_AGE_SECONDS: i64 = 180;
pub const POLICY_VERSION: &str = "v2-paper-policy-1";

#[derive(Debug)]
pub struct RuleError(pub String);

impl fmt::Display for RuleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for RuleError {}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Signal {
    pub symbol: String,
    pub side: Side,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Quote {
    pub symbol: String,
    pub price_cents: i64,
    pub observed_at: String,
    pub source: String,
    pub quote_id: String,
    pub tradable: bool,
    pub limit_up: bool,
    pub limit_down: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimulationRequest {
    pub schema_version: u32,
    pub cycle_id: String,
    pub strategy_version_id: String,
    pub signal_id: String,
    pub decision_at: String,
    pub signal: Signal,
    pub quote: Quote,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fill {
    pub symbol: String,
    pub side: Side,
    pub quantity: i64,
    pub price_cents: i64,
    pub fees_cents: i64,
    pub trade_day: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lot {
    pub symbol: String,
    pub quantity: i64,
    pub bought_on: String,
}

