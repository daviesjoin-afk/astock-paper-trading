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
