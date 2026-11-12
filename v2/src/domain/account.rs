//! One paper account with two named strategy slots.

use serde::{Deserialize, Serialize};

use super::{RuleError, StrategyId};

pub const ACCOUNT_SCHEMA_VERSION: u32 = 1;
pub const TOTAL_BPS: i64 = 10_000;
pub const MIN_CASH_CENTS: i64 = 10_000;
pub const MAX_CASH_CENTS: i64 = 100_000_000_000;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateAccountRequest {
    pub initial_cash_cents: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountSpec {
    pub schema_version: u32,
    pub account_id: String,
    pub initial_cash_cents: i64,
    pub created_at: String,
    pub slots: [SlotSpec; 2],
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SlotSpec {
    pub slot: StrategyId,
    pub weight_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlotBook {
    pub slot: StrategyId,
    pub cash_cents: i64,
    pub positions: Vec<Position>,
    pub sellable: Vec<Sellable>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Position {
    pub symbol: String,
    pub quantity: i64,
    pub average_cost_cents: i64,
    pub acquired_day: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Sellable {
    pub symbol: String,
    pub quantity: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Trade {
    pub id: i64,
    pub slot: StrategyId,
    pub symbol: String,
    pub side: String,
    pub quantity: i64,
    pub price_cents: i64,
    pub fees_cents: i64,
    pub trade_day: String,
    pub reason: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rebalance {
    pub id: i64,
    pub old_steady_bps: i64,
    pub old_aggressive_bps: i64,
    pub new_steady_bps: i64,
    pub new_aggressive_bps: i64,
    pub moved_cents: i64,
    pub from: StrategyId,
    pub to: StrategyId,
    pub reason: String,
    pub applied_day: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountView {
    pub account_id: String,
    pub initial_cash_cents: i64,
    pub cash_cents: i64,
    pub equity_cents: Option<i64>,
    pub profit_cents: Option<i64>,
    pub return_bps: Option<i64>,
    pub as_of_day: Option<String>,
    pub created_at: String,
    pub slots: Vec<SlotView>,
    pub positions: Vec<PositionView>,
    pub trades: Vec<Trade>,
    pub rebalances: Vec<Rebalance>,
    pub price_set_id: Option<String>,
    pub price_set_coverage_end: Option<String>,
    pub last_settled_day: Option<String>,
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayRun {
    pub day: String,
    pub trades: Vec<Trade>,
    pub rebalance: Option<Rebalance>,
    pub already_settled: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AdvanceResult {
    pub from_day: Option<String>,
    pub through_day: String,
    pub processed_days: Vec<String>,
    pub trades: Vec<Trade>,
    pub rebalances: Vec<Rebalance>,
    pub account: AccountView,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PriceCoverage {
    pub price_set_id: Option<String>,
    pub market_data_pack_id: Option<String>,
    pub coverage_start: Option<String>,
    pub coverage_end: Option<String>,
    pub days: Vec<String>,
    pub symbols: Vec<String>,
    pub last_settled_day: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunResult {
    pub from_day: Option<String>,
    pub through_day: String,
    pub processed_days: Vec<String>,
    pub trades: Vec<Trade>,
    pub rebalances: Vec<Rebalance>,
    pub account: AccountView,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlotView {
    pub slot: StrategyId,
    pub name: String,
    pub description: String,
    pub weight_bps: i64,
    pub cash_cents: i64,
    pub market_value_cents: Option<i64>,
    pub equity_cents: Option<i64>,
    pub profit_cents: Option<i64>,
    pub return_bps: Option<i64>,
    pub drawdown_bps: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PositionView {
    pub slot: StrategyId,
    pub symbol: String,
    pub quantity: i64,
    pub sellable_quantity: Option<i64>,
    pub average_cost_cents: i64,
    pub close_cents: Option<i64>,
    pub market_value_cents: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Order {
    pub slot: StrategyId,
    pub symbol: String,
    pub side: String,
    pub quantity: i64,
    pub reason: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProposedOrder {
    pub order: Order,
    pub price_cents: i64,
}

pub fn validate_principal(cents: i64) -> Result<(), RuleError> {
    if !(MIN_CASH_CENTS..=MAX_CASH_CENTS).contains(&cents) {
        return Err(RuleError("invalid_initial_cash".into()));
    }
    Ok(())
}

pub fn initial_split(initial_cash_cents: i64) -> Result<[SlotBudget; 2], RuleError> {
    validate_principal(initial_cash_cents)?;
    let steady = initial_cash_cents * 5_000 / TOTAL_BPS;
    let aggressive = initial_cash_cents - steady;
    Ok([
        SlotBudget { slot: StrategyId::Steady, cash_cents: steady, weight_bps: 5_000 },
        SlotBudget { slot: StrategyId::Aggressive, cash_cents: aggressive, weight_bps: 5_000 },
    ])
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotBudget {
    pub slot: StrategyId,
    pub cash_cents: i64,
    pub weight_bps: i64,
}

/// Move the overnight position quantity into the sellable bucket. Call once
/// for every new trading day after the previous day has fully settled.
pub fn settle_overnight(book: &mut SlotBook) -> Result<(), RuleError> {
    for position in &book.positions {
        if position.quantity <= 0 {
            return Err(RuleError("invalid_position".into()));
        }
        if let Some(item) = book.sellable.iter_mut().find(|item| item.symbol == position.symbol) {
            if item.quantity < 0 || item.quantity > position.quantity {
                return Err(RuleError("invalid_sellable_quantity".into()));
            }
        }
    }
    book.sellable = book
        .positions
        .iter()
        .map(|position| Sellable {
            symbol: position.symbol.clone(),
            quantity: position.quantity,
        })
        .collect();
    Ok(())
}

pub fn apply_buy(
    book: &mut SlotBook,
    symbol: &str,
    quantity: i64,
    price_cents: i64,
    fees_cents: i64,
    day: &str,
) -> Result<(), RuleError> {
    if quantity <= 0 || quantity % 100 != 0 || price_cents <= 0 || fees_cents < 0 {
        return Err(RuleError("invalid_buy".into()));
    }
    let notional = quantity
        .checked_mul(price_cents)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    let total = notional
        .checked_add(fees_cents)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    if total > book.cash_cents {
        return Err(RuleError("insufficient_slot_cash".into()));
    }
    book.cash_cents -= total;
    if let Some(position) = book.positions.iter_mut().find(|item| item.symbol == symbol) {
        let old_cost = position
            .average_cost_cents
            .checked_mul(position.quantity)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        let added = price_cents
            .checked_mul(quantity)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        position.quantity = position
            .quantity
            .checked_add(quantity)
            .ok_or_else(|| RuleError("quantity_overflow".into()))?;
        position.average_cost_cents = old_cost
            .checked_add(added)
            .ok_or_else(|| RuleError("amount_overflow".into()))?
            / position.quantity;
    } else {
        book.positions.push(Position {
            symbol: symbol.into(),
            quantity,
            average_cost_cents: price_cents,
            acquired_day: day.into(),
        });
    }
    // T+1: today's new shares are deliberately not added to `sellable`.
    Ok(())
}

pub fn apply_sell(
    book: &mut SlotBook,
    symbol: &str,
    quantity: i64,
    price_cents: i64,
    fees_cents: i64,
) -> Result<(), RuleError> {
    if quantity <= 0 || quantity % 100 != 0 || price_cents <= 0 || fees_cents < 0 {
        return Err(RuleError("invalid_sell".into()));
    }
    let sellable_index = book
        .sellable
        .iter()
        .position(|item| item.symbol == symbol);
    let sellable_quantity = sellable_index
        .map(|index| book.sellable[index].quantity)
        .unwrap_or(0);
    if quantity > sellable_quantity {
        return Err(RuleError("t_plus_one_violation".into()));
    }
    let position_index = book
        .positions
        .iter()
        .position(|item| item.symbol == symbol)
        .ok_or_else(|| RuleError("position_not_found".into()))?;
    if book.positions[position_index].quantity < quantity {
        return Err(RuleError("insufficient_position".into()));
    }
    let notional = quantity
        .checked_mul(price_cents)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    let cash_after = book
        .cash_cents
        .checked_add(notional)
        .ok_or_else(|| RuleError("amount_overflow".into()))?
        .checked_sub(fees_cents)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    if cash_after < 0 {
        return Err(RuleError("negative_cash".into()));
    }
    book.cash_cents = cash_after;
    book.positions[position_index].quantity -= quantity;
    if let Some(index) = sellable_index {
        book.sellable[index].quantity -= quantity;
    }
    if book.positions[position_index].quantity == 0 {
        book.positions.retain(|item| item.symbol != symbol);
    }
    book.sellable.retain(|item| item.quantity > 0);
    Ok(())
}

pub fn fees_cents(side: &str, amount_cents: i64) -> i64 {
    let commission = ((amount_cents as i128) * 3 / 10_000).max(500) as i64;
    if side == "sell" {
        commission + amount_cents * 5 / 10_000
    } else {
        commission
    }
}
