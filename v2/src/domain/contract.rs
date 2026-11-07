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
pub const POLICY_VERSION: &str = "v2-paper-policy-6";
pub const MARKET_DATA_PACK_ID: &str = "cn-offline-fixture-pack-1";

/// Research-owned immutable identity, not permission to mutate a ledger.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyArtifact {
    pub strategy_version_id: String,
    pub research_input_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExperimentSpec {
    pub schema_version: u32,
    /// The sole experiment/capital owner, not a scheduler cycle.
    pub cycle_id: String,
    pub initial_cash_cents: i64,
    pub strategy: StrategyArtifact,
    pub market_data_pack_id: String,
    pub execution_policy_version: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExperimentStatus { Active, Completed }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Experiment {
    pub spec: ExperimentSpec,
    pub status: ExperimentStatus,
}

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
    /// Offline reference only; execution requires a separate directional level.
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
    pub quote_ref: QuoteRef,
}

/// Identity of an immutable quote already held by Store; never an inline price.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuoteRef {
    pub market_data_pack_id: String,
    pub source: String,
    pub quote_id: String,
}

/// Untrusted offline import command. Server-owned receipt/trust fields are absent.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuoteImport {
    pub market_data_pack_id: String,
    pub schema_version: u32,
    pub available_at: String,
    pub quote: Quote,
    #[serde(default)]
    pub execution: Option<ExecutionLiquidity>,
}

/// One offline price level for one order direction, never provider authentication.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionLiquidity {
    pub schema_version: u32,
    pub quote_ref: QuoteRef,
    pub order_side: Side,
    pub price_cents: i64,
    pub available_quantity: i64,
    pub observed_at: String,
}

/// No authenticated provider is implemented. Unknown grades fail deserialization.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QuoteTrust {
    OfflineFixture,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Exchange { Sse, Szse, Unknown }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Board { MainBoard, Star, ChiNext, NotApplicable, Unknown }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SecurityType { OrdinaryAShare, Etf, Other, Unknown }

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

/// Durable facts constructed by Store, never accepted as an ingress command.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuoteEvidence {
    pub market_data_pack_id: String,
    pub schema_version: u32,
    pub available_at: String,
    pub quote: Quote,
    pub trust_level: QuoteTrust,
    pub provider_published_at: Option<String>,
    pub received_at: String,
    #[serde(default)]
    pub execution: Option<ExecutionLiquidity>,
    #[serde(default)]
    pub instrument: Option<InstrumentIdentity>,
}

/// Simulated result; its run pins the quote identity and execution evidence.
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


/// Immutable reviewed context manifest; hashes summarize fixed snapshot bytes,
/// not evidence that may be appended later.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarketDataPackManifest {
    pub pack_id: String,
    pub version: String,
    pub trust_level: QuoteTrust,
    pub coverage_start: String,
    pub coverage_end: String,
    pub sources: Vec<String>,
    pub description: String,
    pub content_summary: std::collections::BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarketDataPack {
    pub manifest: MarketDataPackManifest,
    pub calendar: super::TradingCalendar,
    pub sessions: super::SessionPolicy,
    pub instruments: super::InstrumentSnapshot,
}
