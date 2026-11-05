//! Deterministic A-share paper execution. No IO, clock, DB, network or broker.
use chrono::{DateTime, FixedOffset, Timelike};
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
pub struct Signal {
    pub symbol: String,
    pub side: Side,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
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

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionStatus {
    Filled,
    Rejected,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Outcome {
    pub status: DecisionStatus,
    pub reason: String,
    pub fill: Option<Fill>,
    pub cash_after_cents: i64,
}

fn rejected(state: &PortfolioState, reason: &str) -> Outcome {
    Outcome {
        status: DecisionStatus::Rejected, reason: reason.into(),
        fill: None, cash_after_cents: state.cash_cents,
    }
}

fn checked_fee(gross: i64, bps: i64) -> Result<i64, RuleError> {
    let numerator = gross.checked_mul(bps)
        .and_then(|v| v.checked_add(9_999))
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    Ok(numerator / 10_000)
}

fn timestamp(value: &str) -> Result<DateTime<FixedOffset>, RuleError> {
    let dt = DateTime::parse_from_rfc3339(value)
        .map_err(|_| RuleError("invalid_rfc3339_timestamp".into()))?;
    if dt.offset().local_minus_utc() != 8 * 3600 {
        return Err(RuleError("timestamp_must_have_china_utc_plus_8_offset".into()));
    }
    Ok(dt)
}

fn key_valid(key: &str) -> bool {
    !key.trim().is_empty() && key.len() <= 128
        && key.chars().all(|c| c.is_ascii_alphanumeric() || "-_.:".contains(c))
}

/// One immutable decision. Caller provides all time/quote inputs.
pub fn decide(state: &PortfolioState, req: &SimulationRequest) -> Result<Outcome, RuleError> {
    if req.schema_version != 1 {
        return Err(RuleError("unsupported_contract_version".into()));
    }
    if !key_valid(&req.cycle_id) || !key_valid(&req.strategy_version_id)
        || !key_valid(&req.signal_id) || !key_valid(&req.quote.quote_id)
        || !key_valid(&req.quote.source)
    {
        return Err(RuleError("invalid_identity".into()));
    }
    let symbol = req.signal.symbol.as_str();
    let valid_symbol = symbol.len() == 9 &&
        (symbol.ends_with(".SH") || symbol.ends_with(".SZ")) &&
        symbol.as_bytes()[..6].iter().all(u8::is_ascii_digit);
    if !valid_symbol || symbol != req.quote.symbol {
        return Err(RuleError("symbol_mismatch_or_invalid_a_share_symbol".into()));
    }
    if req.quote.price_cents <= 0 {
        return Err(RuleError("invalid_quote_price".into()));
    }
    let decision_at = timestamp(&req.decision_at)?;
    let observed_at = timestamp(&req.quote.observed_at)?;
    if observed_at > decision_at {
        return Ok(rejected(state, "quote_from_future"));
    }
    if observed_at.date_naive() != decision_at.date_naive() ||
        (decision_at - observed_at).num_seconds() > MAX_QUOTE_AGE_SECONDS
    {
        return Ok(rejected(state, "stale_quote"));
    }
    let time = decision_at.hour() * 60 + decision_at.minute();
    if !((570..=690).contains(&time) || (780..=900).contains(&time)) {
        return Ok(rejected(state, "outside_trading_session"));
    }
    if !req.quote.tradable {
        return Ok(rejected(state, "not_tradable"));
    }
    if req.signal.side == Side::Buy && req.quote.limit_up {
        return Ok(rejected(state, "limit_up_buy_blocked"));
    }
    if req.signal.side == Side::Sell && req.quote.limit_down {
        return Ok(rejected(state, "limit_down_sell_blocked"));
    }
    let trade_day = decision_at.date_naive().to_string();
    let price = req.quote.price_cents;
    let quantity = match req.signal.side {
        Side::Buy => {
            if state.quantity(symbol) > 0 {
                return Ok(rejected(state, "position_already_open"));
            }
            let budget = state.cash_cents.checked_mul(BUY_BUDGET_BPS)
                .ok_or_else(|| RuleError("amount_overflow".into()))? / 10_000;
            let board_price = price.checked_mul(LOT_SIZE)
                .ok_or_else(|| RuleError("amount_overflow".into()))?;
            let mut qty = (budget / board_price) * LOT_SIZE;
            while qty >= LOT_SIZE {
                let gross = qty.checked_mul(price)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?;
                let fee = checked_fee(gross, COMMISSION_BPS)?.max(MIN_COMMISSION_CENTS);
                let total = gross.checked_add(fee)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?;
                if total <= budget && total <= state.cash_cents { break; }
                qty -= LOT_SIZE;
            }
            if qty == 0 {
                return Ok(rejected(state, "insufficient_budget_for_board_lot_and_fees"));
            }
            qty
        }
        Side::Sell => {
            let qty = state.sellable_quantity(symbol, &trade_day);
            if qty < LOT_SIZE {
                return Ok(rejected(state, "no_t1_sellable_lot"));
            }
            qty
        }
    };
    let gross = quantity.checked_mul(price)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    let commission = checked_fee(gross, COMMISSION_BPS)?.max(MIN_COMMISSION_CENTS);
    let stamp = if req.signal.side == Side::Sell {
        checked_fee(gross, SELL_STAMP_DUTY_BPS)?
    } else { 0 };
    let fees = commission.checked_add(stamp)
        .ok_or_else(|| RuleError("amount_overflow".into()))?;
    let fill = Fill {
        symbol: symbol.into(),
        side: req.signal.side.clone(),
        quantity, price_cents: price,
        fees_cents: fees, trade_day,
    };
    let mut next = state.clone();
    next.apply(&fill)?;
    Ok(Outcome {
        status: DecisionStatus::Filled, reason: "filled".into(),
        fill: Some(fill), cash_after_cents: next.cash_cents,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(day: &str, side: Side) -> SimulationRequest {
        SimulationRequest {
            schema_version: 1, cycle_id: "demo-cycle".into(),
            strategy_version_id: "alpha-v1".into(), signal_id: "signal-1".into(),
            decision_at: format!("{day}T10:00:00+08:00"),
            signal: Signal { symbol: "600000.SH".into(), side },
            quote: Quote {
                symbol: "600000.SH".into(), price_cents: 1234,
                observed_at: format!("{day}T09:59:30+08:00"),
                source: "offline-fixture".into(), quote_id: "q-1".into(),
                tradable: true, limit_up: false, limit_down: false,
            },
        }
    }

    #[test]
    fn buy_is_board_lot_and_does_not_spend_more_than_budget() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let out = decide(&state, &request("2026-10-30", Side::Buy)).unwrap();
        let fill = out.fill.unwrap();
        assert_eq!(fill.quantity % 100, 0);
        assert!(fill.quantity > 0);
        assert!(fill.quantity * fill.price_cents + fill.fees_cents <= 1_000_000);
    }

    #[test]
    fn t_plus_one_blocks_sale_on_purchase_day_then_allows_next_day() {
        let mut state = PortfolioState::new(INITIAL_CASH_CENTS);
        let buy = decide(&state, &request("2026-10-30", Side::Buy)).unwrap().fill.unwrap();
        state.apply(&buy).unwrap();
        assert_eq!(decide(&state, &request("2026-10-30", Side::Sell)).unwrap().reason,
                   "no_t1_sellable_lot");
        let sell = decide(&state, &request("2026-11-02", Side::Sell)).unwrap();
        assert_eq!(sell.status, DecisionStatus::Filled);
        state.apply(sell.fill.as_ref().unwrap()).unwrap();
        assert_eq!(state.quantity("600000.SH"), 0);
    }

    #[test]
    fn stale_future_and_limit_up_quotes_fail_closed() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let mut req = request("2026-10-30", Side::Buy);
        req.quote.observed_at = "2026-10-30T09:50:00+08:00".into();
        assert_eq!(decide(&state, &req).unwrap().reason, "stale_quote");
        req.quote.observed_at = "2026-10-30T10:00:01+08:00".into();
        assert_eq!(decide(&state, &req).unwrap().reason, "quote_from_future");
        req.quote.observed_at = "2026-10-30T09:59:30+08:00".into();
        req.quote.limit_up = true;
        assert_eq!(decide(&state, &req).unwrap().reason, "limit_up_buy_blocked");
    }

    #[test]
    fn no_wall_clock_or_random_inputs_affect_decision() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let req = request("2026-10-30", Side::Buy);
        assert_eq!(decide(&state, &req).unwrap(), decide(&state, &req).unwrap());
    }

    #[test]
    fn false_symbol_and_wrong_timezone_are_rejected() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let mut req = request("2026-10-30", Side::Buy);
        req.quote.symbol = "000001.SZ".into();
        assert!(decide(&state, &req).is_err());
        req.quote.symbol = req.signal.symbol.clone();
        req.decision_at = "2026-10-30T10:00:00+00:00".into();
        assert!(decide(&state, &req).is_err());
    }
}
