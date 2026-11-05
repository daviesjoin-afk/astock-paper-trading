//! Pure decision policy: explicit snapshots in, deterministic outcome out.
use super::contract::*;
use super::portfolio::PortfolioState;
use chrono::{DateTime, FixedOffset, Timelike};
use serde::{Deserialize, Serialize};

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

fn symbol_valid(symbol: &str) -> bool {
    symbol.len() == 9 &&
        (symbol.ends_with(".SH") || symbol.ends_with(".SZ")) &&
        symbol.as_bytes()[..6].iter().all(u8::is_ascii_digit)
}

/// Pure evidence validation. This phase deliberately accepts offline fixtures only.
pub fn validate_evidence(evidence: &QuoteEvidence) -> Result<(), RuleError> {
    if evidence.schema_version != 1 {
        return Err(RuleError("unsupported_evidence_version".into()));
    }
    let quote = &evidence.quote;
    if quote.source != "offline-fixture" {
        return Err(RuleError("unsupported_quote_source".into()));
    }
    if !key_valid(&quote.quote_id) || !symbol_valid(&quote.symbol) || quote.price_cents <= 0 {
        return Err(RuleError("invalid_quote_evidence".into()));
    }
    if timestamp(&quote.observed_at)? > timestamp(&evidence.available_at)? {
        return Err(RuleError("quote_observed_after_availability".into()));
    }
    Ok(())
}

/// One immutable decision. Caller provides all time/quote inputs.
pub fn decide(
    state: &PortfolioState, req: &SimulationRequest, evidence: Option<&QuoteEvidence>,
) -> Result<Outcome, RuleError> {
    if req.schema_version != 2 {
        return Err(RuleError("unsupported_contract_version".into()));
    }
    if !key_valid(&req.cycle_id) || !key_valid(&req.strategy_version_id)
        || !key_valid(&req.signal_id) || !key_valid(&req.quote_ref.quote_id)
        || !key_valid(&req.quote_ref.source)
    {
        return Err(RuleError("invalid_identity".into()));
    }
    let symbol = req.signal.symbol.as_str();
    if !symbol_valid(symbol) {
        return Err(RuleError("invalid_a_share_symbol".into()));
    }
    let decision_at = timestamp(&req.decision_at)?;
    let Some(evidence) = evidence else {
        return Ok(rejected(state, "quote_evidence_missing"));
    };
    validate_evidence(evidence)?;
    let quote = &evidence.quote;
    if req.quote_ref.source != quote.source || req.quote_ref.quote_id != quote.quote_id {
        return Err(RuleError("quote_evidence_identity_mismatch".into()));
    }
    if symbol != quote.symbol {
        return Err(RuleError("symbol_mismatch_or_invalid_a_share_symbol".into()));
    }
    let observed_at = timestamp(&quote.observed_at)?;
    if observed_at > decision_at {
        return Ok(rejected(state, "quote_from_future"));
    }
    if timestamp(&evidence.available_at)? > decision_at {
        return Ok(rejected(state, "quote_not_yet_available"));
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
    if !quote.tradable {
        return Ok(rejected(state, "not_tradable"));
    }
    if req.signal.side == Side::Buy && quote.limit_up {
        return Ok(rejected(state, "limit_up_buy_blocked"));
    }
    if req.signal.side == Side::Sell && quote.limit_down {
        return Ok(rejected(state, "limit_down_sell_blocked"));
    }
    let trade_day = decision_at.date_naive().to_string();
    let price = quote.price_cents;
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

