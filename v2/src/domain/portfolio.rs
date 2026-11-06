//! Cash and T+1 accounting for this policy's supported whole-lot universe.
//! This invariant does not classify instruments; execution checks eligibility.
use super::contract::{Fill, Lot, RuleError, Side, LOT_SIZE};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortfolioState {
    pub cash_cents: i64,
    pub lots: Vec<Lot>,
}

impl PortfolioState {
    pub fn new(initial_cash_cents: i64) -> Self {
        Self { cash_cents: initial_cash_cents, lots: Vec::new() }
    }

    pub fn quantity(&self, symbol: &str) -> i64 {
        self.lots.iter()
            .filter(|lot| lot.symbol == symbol)
            .map(|lot| lot.quantity)
            .sum()
    }

    pub fn sellable_quantity(&self, symbol: &str, trade_day: &str) -> i64 {
        self.lots.iter()
            .filter(|lot| lot.symbol == symbol && lot.bought_on.as_str() < trade_day)
            .map(|lot| lot.quantity)
            .sum()
    }

    pub fn apply(&mut self, fill: &Fill) -> Result<(), RuleError> {
        if fill.quantity <= 0 || fill.quantity % LOT_SIZE != 0
            || fill.price_cents <= 0 || fill.fees_cents < 0
        {
            return Err(RuleError("invalid_fill".into()));
        }
        let gross = fill.quantity.checked_mul(fill.price_cents)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        match fill.side {
            Side::Buy => {
                let total = gross.checked_add(fill.fees_cents)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?;
                if total > self.cash_cents {
                    return Err(RuleError("insufficient_cash".into()));
                }
                self.cash_cents -= total;
                self.lots.push(Lot {
                    symbol: fill.symbol.clone(),
                    quantity: fill.quantity,
                    bought_on: fill.trade_day.clone(),
                });
            }
            Side::Sell => {
                if fill.fees_cents > gross ||
                    self.sellable_quantity(&fill.symbol, &fill.trade_day) < fill.quantity
                {
                    return Err(RuleError("invalid_t1_sell".into()));
                }
                let mut remaining = fill.quantity;
                for lot in &mut self.lots {
                    if lot.symbol == fill.symbol &&
                        lot.bought_on < fill.trade_day && remaining > 0
                    {
                        let consumed = remaining.min(lot.quantity);
                        lot.quantity -= consumed;
                        remaining -= consumed;
                    }
                }
                self.lots.retain(|lot| lot.quantity > 0);
                let net = gross - fill.fees_cents;
                self.cash_cents = self.cash_cents.checked_add(net)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?;
            }
        }
        Ok(())
    }
}

