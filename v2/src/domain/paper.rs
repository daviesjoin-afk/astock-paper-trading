//! Paper account: one user-funded account with two strategy slots.
//!
//! The account owns the user's initial cash. Each slot owns a fixed budget,
//! its own positions and its own decision ledger; the reserve never trades.
//! Nothing here touches storage, clocks or the network: the caller supplies an
//! explicit `created_at`, and the split is a pure function of the spec.

use serde::{Deserialize, Serialize};

use crate::domain::{QuoteRef, RuleError, Signal, StrategyArtifact};

pub const PAPER_ACCOUNT_SCHEMA_VERSION: u32 = 1;
pub const PAPER_ORDER_SCHEMA_VERSION: u32 = 1;
pub const PAPER_SESSION_SCHEMA_VERSION: u32 = 1;
pub const PAPER_SLOT_COUNT: usize = 2;
pub const TOTAL_BPS: i64 = 10_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum PaperSlotId {
    A,
    B,
}

impl PaperSlotId {
    pub fn as_str(self) -> &'static str {
        match self {
            PaperSlotId::A => "A",
            PaperSlotId::B => "B",
        }
    }
}

/// Explicit risk boundaries for a paper account. Cost-basis based, no prices
/// are required: limits are fractions of the slot budget.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperRiskLimits {
    /// Max cost basis of one symbol as bps of the slot budget.
    pub max_symbol_weight_bps: i64,
    /// Max gross notional of a single order as bps of the slot budget.
    pub max_order_notional_bps: i64,
    /// Slot cash that must remain after a fill, bps of the slot budget.
    pub min_slot_cash_bps: i64,
}

impl Default for PaperRiskLimits {
    fn default() -> Self {
        Self { max_symbol_weight_bps: 2_500, max_order_notional_bps: 5_000, min_slot_cash_bps: 1_000 }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperSlotSpec {
    pub slot: PaperSlotId,
    pub strategy: StrategyArtifact,
    pub market_data_pack_id: String,
    pub execution_policy_version: String,
    /// Share of the account's initial cash, in basis points of the whole account.
    pub weight_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperAccountSpec {
    pub schema_version: u32,
    pub account_id: String,
    pub initial_cash_cents: i64,
    /// Cash that never trades, in basis points of the account.
    pub reserve_bps: i64,
    /// Explicit RFC3339 instant; the domain never reads a machine clock.
    pub created_at: String,
    pub slots: Vec<PaperSlotSpec>,
    #[serde(default)]
    pub limits: PaperRiskLimits,
}

/// Integer-cent split of the initial cash. Floor division per slot, with the
/// indivisible remainder going to the reserve so the account always balances.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaperCapitalSplit {
    pub slot_budgets: [i64; PAPER_SLOT_COUNT],
    pub reserve_cents: i64,
}

pub(crate) fn key_valid(key: &str) -> bool {
    !key.trim().is_empty()
        && key.len() <= 128
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.:".contains(c))
}

impl PaperAccountSpec {
    pub fn validate(&self) -> Result<PaperCapitalSplit, RuleError> {
        if self.schema_version != PAPER_ACCOUNT_SCHEMA_VERSION {
            return Err(RuleError("unsupported_paper_account_version".into()));
        }
        if !key_valid(&self.account_id) {
            return Err(RuleError("invalid_account_id".into()));
        }
        if self.initial_cash_cents <= 0 {
            return Err(RuleError("invalid_initial_cash".into()));
        }
        if self.reserve_bps < 0 || self.reserve_bps >= TOTAL_BPS {
            return Err(RuleError("invalid_reserve_bps".into()));
        }
        if chrono::DateTime::parse_from_rfc3339(&self.created_at).is_err() {
            return Err(RuleError("invalid_created_at".into()));
        }
        if self.slots.len() != PAPER_SLOT_COUNT {
            return Err(RuleError("paper_account_requires_two_slots".into()));
        }
        let mut seen_slots = [false; PAPER_SLOT_COUNT];
        let mut seen_strategies: Vec<&str> = Vec::new();
        let mut total_bps = self.reserve_bps;
        for slot in &self.slots {
            let index = match slot.slot {
                PaperSlotId::A => 0,
                PaperSlotId::B => 1,
            };
            if seen_slots[index] {
                return Err(RuleError("duplicate_paper_slot".into()));
            }
            seen_slots[index] = true;
            if slot.weight_bps <= 0 {
                return Err(RuleError("invalid_slot_weight".into()));
            }
            total_bps += slot.weight_bps;
            slot.strategy.verify_content()?;
            if !key_valid(&slot.market_data_pack_id) {
                return Err(RuleError("invalid_market_data_pack_id".into()));
            }
            if !key_valid(&slot.execution_policy_version) {
                return Err(RuleError("invalid_execution_policy_version".into()));
            }
            if seen_strategies.contains(&slot.strategy.strategy_version_id.as_str()) {
                return Err(RuleError("duplicate_strategy_in_paper_account".into()));
            }
            seen_strategies.push(slot.strategy.strategy_version_id.as_str());
        }
        if total_bps != TOTAL_BPS {
            return Err(RuleError("paper_account_weights_must_sum_to_total".into()));
        }
        for value in [
            self.limits.max_symbol_weight_bps,
            self.limits.max_order_notional_bps,
            self.limits.min_slot_cash_bps,
        ] {
            if !(0..=TOTAL_BPS).contains(&value) {
                return Err(RuleError("invalid_risk_limits".into()));
            }
        }

        let mut slot_budgets = [0i64; PAPER_SLOT_COUNT];
        for slot in &self.slots {
            let index = match slot.slot {
                PaperSlotId::A => 0,
                PaperSlotId::B => 1,
            };
            slot_budgets[index] = self
                .initial_cash_cents
                .checked_mul(slot.weight_bps)
                .ok_or_else(|| RuleError("account_cash_overflow".into()))?
                / TOTAL_BPS;
        }
        let assigned: i64 = slot_budgets.iter().sum();
        let reserve_cents = self
            .initial_cash_cents
            .checked_sub(assigned)
            .ok_or_else(|| RuleError("account_cash_overflow".into()))?;
        if reserve_cents < 0 {
            return Err(RuleError("account_cash_overflow".into()));
        }
        Ok(PaperCapitalSplit { slot_budgets, reserve_cents })
    }

    /// Recompute budgets from explicit current weights (after a rebalance).
    /// History is untouched: this only says how the *initial* capital is
    /// divided now, so cash must be moved separately and audited.
    pub fn split_with_weights(
        &self,
        weights: [i64; PAPER_SLOT_COUNT],
    ) -> Result<PaperCapitalSplit, RuleError> {
        self.validate()?;
        if weights.iter().any(|weight| *weight <= 0) {
            return Err(RuleError("invalid_slot_weight".into()));
        }
        if weights.iter().sum::<i64>() + self.reserve_bps > TOTAL_BPS {
            return Err(RuleError("paper_account_weights_must_sum_to_total".into()));
        }
        let mut slot_budgets = [0i64; PAPER_SLOT_COUNT];
        for (index, weight) in weights.iter().enumerate() {
            slot_budgets[index] = self
                .initial_cash_cents
                .checked_mul(*weight)
                .ok_or_else(|| RuleError("account_cash_overflow".into()))?
                / TOTAL_BPS;
        }
        let assigned: i64 = slot_budgets.iter().sum();
        let reserve_cents = self
            .initial_cash_cents
            .checked_sub(assigned)
            .ok_or_else(|| RuleError("account_cash_overflow".into()))?;
        if reserve_cents < 0 {
            return Err(RuleError("account_cash_overflow".into()));
        }
        Ok(PaperCapitalSplit { slot_budgets, reserve_cents })
    }
}

/// One simulated order submitted to a strategy slot. The slot, not the client,
/// owns the strategy identity, budget and cash; the request only names the
/// intent and the quote evidence it relies on.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperOrderRequest {
    pub schema_version: u32,
    pub account_id: String,
    pub slot: PaperSlotId,
    pub signal_id: String,
    pub decision_at: String,
    pub signal: Signal,
    pub quote_ref: QuoteRef,
}

impl PaperOrderRequest {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != PAPER_ORDER_SCHEMA_VERSION {
            return Err(RuleError("unsupported_paper_order_version".into()));
        }
        if !key_valid(&self.account_id) {
            return Err(RuleError("invalid_account_id".into()));
        }
        if !key_valid(&self.signal_id) {
            return Err(RuleError("invalid_signal_id".into()));
        }
        if chrono::DateTime::parse_from_rfc3339(&self.decision_at).is_err() {
            return Err(RuleError("invalid_decision_time".into()));
        }
        if !key_valid(&self.quote_ref.market_data_pack_id)
            || !key_valid(&self.quote_ref.source)
            || !key_valid(&self.quote_ref.quote_id)
        {
            return Err(RuleError("invalid_identity".into()));
        }
        Ok(())
    }
}

/// One frozen signal the session will submit when its clock reaches it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperSessionSignal {
    pub slot: PaperSlotId,
    pub signal_id: String,
    pub decision_at: String,
    pub signal: Signal,
    pub quote_ref: QuoteRef,
}

/// A deterministic paper session: explicit start instant plus an ordered,
/// frozen signal list. The session never reads a machine clock.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperSessionSpec {
    pub schema_version: u32,
    pub session_id: String,
    pub account_id: String,
    pub start_at: String,
    pub signals: Vec<PaperSessionSignal>,
}

impl PaperSessionSpec {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != PAPER_SESSION_SCHEMA_VERSION {
            return Err(RuleError("unsupported_paper_session_version".into()));
        }
        if !key_valid(&self.session_id) || !key_valid(&self.account_id) {
            return Err(RuleError("invalid_session_identity".into()));
        }
        let start = chrono::DateTime::parse_from_rfc3339(&self.start_at)
            .map_err(|_| RuleError("invalid_session_start".into()))?;
        if self.signals.is_empty() {
            return Err(RuleError("paper_session_requires_signals".into()));
        }
        let mut previous = start;
        for signal in &self.signals {
            if !key_valid(&signal.signal_id) {
                return Err(RuleError("invalid_signal_id".into()));
            }
            if !key_valid(&signal.quote_ref.market_data_pack_id)
                || !key_valid(&signal.quote_ref.source)
                || !key_valid(&signal.quote_ref.quote_id)
            {
                return Err(RuleError("invalid_identity".into()));
            }
            let at = chrono::DateTime::parse_from_rfc3339(&signal.decision_at)
                .map_err(|_| RuleError("invalid_decision_time".into()))?;
            if at < start {
                return Err(RuleError("paper_session_signal_before_start".into()));
            }
            if at < previous {
                return Err(RuleError("paper_session_signals_must_be_ordered".into()));
            }
            previous = at;
        }
        Ok(())
    }
}

/// Client-facing account request: strategies are named by reviewed id and the
/// server resolves the artifact, so the browser never has to carry digests.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperAccountRequestSlot {
    pub slot: PaperSlotId,
    pub strategy_version_id: String,
    pub market_data_pack_id: String,
    pub execution_policy_version: String,
    pub weight_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperAccountRequest {
    pub schema_version: u32,
    pub account_id: String,
    pub initial_cash_cents: i64,
    pub reserve_bps: i64,
    pub created_at: String,
    pub slots: Vec<PaperAccountRequestSlot>,
    #[serde(default)]
    pub limits: PaperRiskLimits,
}
