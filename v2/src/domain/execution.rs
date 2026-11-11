//! Guarded order execution against reviewed closes.

use std::collections::BTreeMap;

use super::{fees_cents, AccountSpec, RuleError, SlotBook, StrategyId};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fill {
    pub slot: StrategyId,
    pub symbol: String,
    pub side: String,
    pub quantity: i64,
    pub price_cents: i64,
    pub fees_cents: i64,
    pub reason: String,
}

pub fn validate_order(
    book: &SlotBook,
    slot: StrategyId,
    side: &str,
    symbol: &str,
    quantity: i64,
    price_cents: i64,
) -> Result<(), RuleError> {
    if slot != book.slot || quantity <= 0 || quantity % 100 != 0 || price_cents <= 0 {
        return Err(RuleError("invalid_order".into()));
    }
    match side {
        "buy" => {
            let amount = quantity * price_cents + fees_cents("buy", quantity * price_cents);
            if amount > book.cash_cents {
                return Err(RuleError("insufficient_slot_cash".into()));
            }
        }
        "sell" => {
            let sellable = book
                .sellable
                .iter()
                .find(|item| item.symbol == symbol)
                .map(|item| item.quantity)
                .unwrap_or(0);
            if sellable < quantity {
                return Err(RuleError("t_plus_one_violation".into()));
            }
            if !book
                .positions
                .iter()
                .any(|item| item.symbol == symbol && item.quantity >= quantity)
            {
                return Err(RuleError("insufficient_position".into()));
            }
        }
        _ => return Err(RuleError("unsupported_side".into())),
    }
    Ok(())
}

pub fn closing_prices(
    set: &super::PriceSet,
    day: &str,
    symbols: &[String],
) -> Result<BTreeMap<String, i64>, RuleError> {
    let mut prices = BTreeMap::new();
    for symbol in symbols {
        let price = set
            .contains(symbol, day)
            .ok_or_else(|| RuleError("missing_close_price".into()))?;
        prices.insert(symbol.clone(), price);
    }
    Ok(prices)
}

pub fn risk_limits(spec: &AccountSpec, slot: StrategyId) -> Result<(i64, i64), RuleError> {
    let budget = spec
        .slots
        .iter()
        .find(|item| item.slot == slot)
        .map(|item| spec.initial_cash_cents * item.weight_bps / 10_000)
        .ok_or_else(|| RuleError("slot_not_found".into()))?;
    let (max_symbol, min_cash) = match slot {
        StrategyId::Steady => (budget * 1_500 / 10_000, budget * 1_000 / 10_000),
        StrategyId::Aggressive => (budget * 3_000 / 10_000, budget * 500 / 10_000),
    };
    Ok((max_symbol, min_cash))
}
