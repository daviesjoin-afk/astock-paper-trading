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

/// Fixed algorithm contract. A different algorithm is a different version.
pub const RESEARCH_ALGORITHM_VERSION: &str = "research-signal-v1";
/// Fixed research runtime contract; the producer is stdlib-only Python.
pub const RESEARCH_RUNTIME_VERSION: &str = "python-3.13-stdlib";
pub const RESEARCH_ARTIFACT_SCHEMA_VERSION: u32 = 2;

/// Research-owned immutable content identity, not permission to mutate a ledger.
///
/// Identity alone is not enough: one `strategy_version_id` must never describe
/// two different algorithms, parameter sets or research inputs. Store rejects a
/// declaration that differs from the reviewed definition for the same id, and
/// recomputes every digest from repository bytes.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyArtifact {
    pub schema_version: u32,
    pub strategy_version_id: String,
    pub research_input_id: String,
    pub algorithm_version: String,
    pub source_digest: String,
    pub parameters_canonical: String,
    pub parameters_digest: String,
    pub research_input_digest: String,
    /// Explicit only when the reviewed algorithm uses randomness.
    pub random_seed: Option<i64>,
    pub runtime_version: String,
}

impl StrategyArtifact {
    /// Fail closed when the artifact's own content identity is inconsistent.
    pub fn verify_content(&self) -> Result<(), RuleError> {
        if self.schema_version != RESEARCH_ARTIFACT_SCHEMA_VERSION {
            return Err(RuleError("unsupported_strategy_artifact_version".into()));
        }
        if self.algorithm_version != RESEARCH_ALGORITHM_VERSION {
            return Err(RuleError("unsupported_research_algorithm".into()));
        }
        if self.runtime_version != RESEARCH_RUNTIME_VERSION {
            return Err(RuleError("unsupported_research_runtime".into()));
        }
        if !super::digest::is_canonical_json_text(&self.parameters_canonical) {
            return Err(RuleError("strategy_parameters_not_canonical".into()));
        }
        if self.parameters_digest != super::digest::sha256_hex(self.parameters_canonical.as_bytes()) {
            return Err(RuleError("strategy_parameters_digest_mismatch".into()));
        }
        Ok(())
    }
}

/// One frozen research observation: declared visibility, one quote, both sides.
///
/// `available_at` and `quote.observed_at` are *research declarations*. They are
/// never the system receipt time, which only Store records.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchInputEntry {
    pub sequence: u32,
    pub decision_at: String,
    pub available_at: String,
    pub quote: Quote,
    pub buy: ResearchInputLevel,
    pub sell: ResearchInputLevel,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchInputLevel {
    pub price_cents: i64,
    pub available_quantity: i64,
}

impl ResearchInputEntry {
    pub fn level(&self, side: &Side) -> &ResearchInputLevel {
        match side {
            Side::Buy => &self.buy,
            Side::Sell => &self.sell,
        }
    }
}

/// Immutable, content-addressed set of research inputs.
///
/// Order, timing and content are all inside `content_digest`, so appending,
/// replacing or reordering an entry changes the input identity.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchInputManifest {
    pub schema_version: u32,
    pub research_input_id: String,
    pub market_data_pack_id: String,
    pub description: String,
    pub entries: Vec<ResearchInputEntry>,
    pub content_digest: String,
}

impl ResearchInputManifest {
    /// Canonicalizable identity projection; `description` is not identity.
    pub fn projection(&self) -> serde_json::Value {
        serde_json::json!({
            "entries": serde_json::to_value(&self.entries).expect("entries serialize"),
            "market_data_pack_id": self.market_data_pack_id,
            "research_input_id": self.research_input_id,
            "schema_version": self.schema_version,
        })
    }

    pub fn computed_digest(&self) -> String {
        super::digest::canonical_digest(&self.projection())
    }

    pub fn entry_for(&self, reference: &QuoteRef) -> Option<&ResearchInputEntry> {
        self.entries.iter().find(|entry| {
            entry.quote.source == reference.source && entry.quote.quote_id == reference.quote_id
        })
    }

    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != 1 {
            return Err(RuleError("unsupported_research_input_version".into()));
        }
        if self.content_digest != self.computed_digest() {
            return Err(RuleError("research_input_content_digest_mismatch".into()));
        }
        let mut previous_sequence = 0;
        let mut previous_time: Option<chrono::DateTime<chrono::FixedOffset>> = None;
        for entry in &self.entries {
            if entry.sequence <= previous_sequence {
                return Err(RuleError("research_input_sequence_not_monotonic".into()));
            }
            previous_sequence = entry.sequence;
            let at = chrono::DateTime::parse_from_rfc3339(&entry.decision_at)
                .map_err(|_| RuleError("invalid_research_input_decision_time".into()))?;
            if previous_time.is_some_and(|previous| at < previous) {
                return Err(RuleError("research_input_decision_times_not_ordered".into()));
            }
            previous_time = Some(at);
            for level in [&entry.buy, &entry.sell] {
                if level.price_cents <= 0 || level.available_quantity < 0 {
                    return Err(RuleError("invalid_research_input_liquidity".into()));
                }
            }
        }
        Ok(())
    }
}

/// One research signal. Carries identity and direction only: never quantity,
/// price, fees or cash.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchSignal {
    pub sequence: u32,
    pub signal_id: String,
    pub decision_at: String,
    pub symbol: String,
    pub side: Side,
    pub source: String,
    pub quote_id: String,
    pub market_data_pack_id: String,
}

/// Reviewed research output: the frozen signals a fixed strategy and a fixed
/// input must reproduce. Produced by Python, verified in CI, compared by Rust.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResearchOutput {
    pub schema_version: u32,
    pub research_output_id: String,
    pub strategy_version_id: String,
    pub research_input_id: String,
    pub algorithm_version: String,
    pub algorithm_digest: String,
    pub parameters_digest: String,
    pub research_input_digest: String,
    pub signals: Vec<ResearchSignal>,
    pub content_digest: String,
}

impl ResearchOutput {
    pub fn projection(&self) -> serde_json::Value {
        serde_json::json!({
            "algorithm_digest": self.algorithm_digest,
            "algorithm_version": self.algorithm_version,
            "parameters_digest": self.parameters_digest,
            "research_input_digest": self.research_input_digest,
            "research_input_id": self.research_input_id,
            "research_output_id": self.research_output_id,
            "schema_version": self.schema_version,
            "signals": serde_json::to_value(&self.signals).expect("signals serialize"),
            "strategy_version_id": self.strategy_version_id,
        })
    }

    pub fn computed_digest(&self) -> String {
        super::digest::canonical_digest(&self.projection())
    }

    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != 1 {
            return Err(RuleError("unsupported_research_output_version".into()));
        }
        if self.content_digest != self.computed_digest() {
            return Err(RuleError("research_output_content_digest_mismatch".into()));
        }
        Ok(())
    }
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
