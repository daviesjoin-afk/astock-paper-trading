//! Exposure analytics from cost basis only: no prices, no valuation, no returns.
//! Answers "how much of each budget is at cost, and is any symbol over its cap".

use serde::{Deserialize, Serialize};

use crate::domain::{canonical_digest, RuleError};

const TOTAL_BPS: i64 = 10_000;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExposurePosition {
    pub symbol: String,
    pub quantity: i64,
    pub cost_cents: i64,
    pub avg_cost_cents: i64,
    pub weight_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlotExposure {
    pub slot: String,
    pub budget_cents: i64,
    pub cash_cents: i64,
    pub cost_cents: i64,
    pub cash_utilization_bps: i64,
    pub positions: Vec<ExposurePosition>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExposureBreach {
    pub slot: String,
    pub kind: String,
    pub symbol: Option<String>,
    pub value_bps: i64,
    pub limit_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountExposure {
    pub account_id: String,
    pub slots: Vec<SlotExposure>,
    pub total_cost_cents: i64,
    pub total_cash_cents: i64,
    pub breaches: Vec<ExposureBreach>,
    pub digest: String,
}

pub struct SlotBook {
    pub slot: String,
    pub budget_cents: i64,
    pub cash_cents: i64,
    /// (symbol, quantity, cost basis cents)
    pub positions: Vec<(String, i64, i64)>,
}

fn weight_bps(value: i64, base: i64) -> i64 {
    if base <= 0 {
        return 0;
    }
    value.saturating_mul(TOTAL_BPS) / base
}

pub fn compute_exposure(
    account_id: &str,
    books: &[SlotBook],
    max_symbol_weight_bps: i64,
) -> Result<AccountExposure, RuleError> {
    let mut slots = Vec::new();
    let mut breaches = Vec::new();
    let mut total_cost_cents = 0i64;
    let mut total_cash_cents = 0i64;
    for book in books {
        if book.budget_cents <= 0 || book.cash_cents < 0 {
            return Err(RuleError("invalid_exposure_book".into()));
        }
        let mut positions = Vec::new();
        let mut cost = 0i64;
        for (symbol, quantity, cost_cents) in &book.positions {
            if *quantity < 0 || *cost_cents < 0 {
                return Err(RuleError("invalid_exposure_position".into()));
            }
            if *quantity == 0 {
                continue;
            }
            cost = cost.saturating_add(*cost_cents);
            let avg = cost_cents / quantity;
            let position_weight = weight_bps(*cost_cents, book.budget_cents);
            if position_weight > max_symbol_weight_bps {
                breaches.push(ExposureBreach {
                    slot: book.slot.clone(),
                    kind: "symbol_concentration".into(),
                    symbol: Some(symbol.clone()),
                    value_bps: position_weight,
                    limit_bps: max_symbol_weight_bps,
                });
            }
            positions.push(ExposurePosition {
                symbol: symbol.clone(),
                quantity: *quantity,
                cost_cents: *cost_cents,
                avg_cost_cents: avg,
                weight_bps: position_weight,
            });
        }
        positions.sort_by(|left, right| right.cost_cents.cmp(&left.cost_cents));
        total_cost_cents = total_cost_cents.saturating_add(cost);
        total_cash_cents = total_cash_cents.saturating_add(book.cash_cents);
        slots.push(SlotExposure {
            slot: book.slot.clone(),
            budget_cents: book.budget_cents,
            cash_cents: book.cash_cents,
            cost_cents: cost,
            cash_utilization_bps: weight_bps(cost, book.budget_cents),
            positions,
        });
    }
    let mut exposure = AccountExposure {
        account_id: account_id.into(),
        slots,
        total_cost_cents,
        total_cash_cents,
        breaches,
        digest: String::new(),
    };
    let mut value = serde_json::to_value(&exposure)
        .map_err(|_| RuleError("exposure_encoding_failed".into()))?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    exposure.digest = canonical_digest(&value);
    Ok(exposure)
}
