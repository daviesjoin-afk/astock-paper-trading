//! Pure decision policy: explicit snapshots in, deterministic outcome out.
use super::contract::*;
use super::calendar::{CalendarDay, TradingCalendar};
use super::portfolio::PortfolioState;
use super::instrument::InstrumentSnapshot;
use chrono::{DateTime, FixedOffset, NaiveTime};
use serde::{Deserialize, Serialize};

pub const SESSION_POLICY_VERSION: &str = "cn-a-share-continuous-only-v1";
pub const BUNDLED_SESSION_POLICY_JSON: &str = include_str!("../../data/session-policy-v1.json");

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionWindow {
    pub start: String,
    pub end: String,
    pub end_inclusive: bool,
}

fn session_time(value: &str) -> Result<NaiveTime, RuleError> {
    NaiveTime::parse_from_str(value, "%H:%M:%S%.f")
        .map_err(|_| RuleError("invalid_session_policy_time".into()))
}

impl SessionWindow {
    fn bounds(&self) -> Result<(NaiveTime, NaiveTime), RuleError> {
        let start = session_time(&self.start)?;
        let end = session_time(&self.end)?;
        if start >= end { return Err(RuleError("invalid_session_window".into())); }
        Ok((start, end))
    }

    fn contains(&self, time: NaiveTime) -> Result<bool, RuleError> {
        let (start, end) = self.bounds()?;
        Ok(time >= start && (time < end || (self.end_inclusive && time == end)))
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionPolicy {
    pub schema_version: u32,
    pub version: String,
    pub continuous: [SessionWindow; 2],
    pub opening_auction: SessionWindow,
    pub closing_auction: SessionWindow,
    pub sources: [String; 2],
}

impl SessionPolicy {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != 1 || self.version != SESSION_POLICY_VERSION
            || self.sources.iter().any(String::is_empty)
        { return Err(RuleError("unsupported_session_policy".into())); }
        let morning = self.continuous[0].bounds()?;
        let afternoon = self.continuous[1].bounds()?;
        let opening = self.opening_auction.bounds()?;
        let closing = self.closing_auction.bounds()?;
        if opening.1 >= morning.0 || morning.1 >= afternoon.0 || afternoon.1 != closing.0
            || self.continuous[1].end_inclusive
        {
            return Err(RuleError("overlapping_or_inconsistent_sessions".into()));
        }
        Ok(())
    }

    fn rejection(&self, time: NaiveTime) -> Result<Option<&'static str>, RuleError> {
        self.validate()?;
        if self.opening_auction.contains(time)? { return Ok(Some("opening_auction_not_supported")); }
        if self.closing_auction.contains(time)? { return Ok(Some("closing_auction_not_supported")); }
        for window in &self.continuous {
            if window.contains(time)? { return Ok(None); }
        }
        if time > self.continuous[0].bounds()?.1 && time < self.continuous[1].bounds()?.0 {
            return Ok(Some("midday_break"));
        }
        Ok(Some("outside_trading_session"))
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

fn symbol_format_valid(symbol: &str) -> bool {
    symbol.len() == 9 &&
        (symbol.ends_with(".SH") || symbol.ends_with(".SZ")) &&
        symbol.as_bytes()[..6].iter().all(u8::is_ascii_digit)
}

/// Validate a command without granting it receipt or provider authority.
pub fn validate_quote_import(input: &QuoteImport) -> Result<(), RuleError> {
    if input.schema_version != 1 {
        return Err(RuleError("unsupported_quote_import_version".into()));
    }
    validate_fixture_quote(&input.quote, &input.available_at, input.execution.as_ref())
}

/// Pure validation of pinned facts; never reads a clock or provider.
pub fn validate_evidence(evidence: &QuoteEvidence) -> Result<(), RuleError> {
    if evidence.schema_version != 4 {
        return Err(RuleError("unsupported_evidence_version".into()));
    }
    if evidence.provider_published_at.is_some() {
        return Err(RuleError("offline_fixture_has_no_verified_provider_publication".into()));
    }
    timestamp(&evidence.received_at)?;
    validate_fixture_quote(&evidence.quote, &evidence.available_at, evidence.execution.as_ref())
}

fn validate_fixture_quote(quote: &Quote, available_at: &str, execution: Option<&ExecutionLiquidity>) -> Result<(), RuleError> {
    if quote.source != "offline-fixture" {
        return Err(RuleError("unsupported_quote_source".into()));
    }
    if !key_valid(&quote.quote_id) || !symbol_format_valid(&quote.symbol) || quote.price_cents <= 0 {
        return Err(RuleError("invalid_quote_evidence".into()));
    }
    if timestamp(&quote.observed_at)? > timestamp(available_at)? {
        return Err(RuleError("quote_observed_after_availability".into()));
    }
    if let Some(level) = execution {
        if level.schema_version != 1 || level.quote_ref.source != quote.source
            || level.quote_ref.quote_id != quote.quote_id || level.price_cents <= 0
            || level.available_quantity < 0
        {
            return Err(RuleError("invalid_execution_evidence".into()));
        }
        if timestamp(&level.observed_at)? > timestamp(available_at)? {
            return Err(RuleError("execution_observed_after_availability".into()));
        }
    }
    Ok(())
}

/// One immutable decision. Caller provides all time/quote inputs.
pub fn decide(
    state: &PortfolioState, req: &SimulationRequest, evidence: Option<&QuoteEvidence>,
    calendar: &TradingCalendar, sessions: &SessionPolicy,
    instruments: &InstrumentSnapshot,
) -> Result<Outcome, RuleError> {
    calendar.validate()?;
    sessions.validate()?;
    instruments.validate()?;
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
    if !symbol_format_valid(symbol) {
        return Err(RuleError("invalid_symbol_format".into()));
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
        return Err(RuleError("symbol_mismatch".into()));
    }
    let observed_at = timestamp(&quote.observed_at)?;
    if observed_at > decision_at {
        return Ok(rejected(state, "quote_from_future"));
    }
    if timestamp(&evidence.available_at)? > decision_at {
        return Ok(rejected(state, "quote_not_yet_available"));
    }
    if observed_at.date_naive() != decision_at.date_naive() ||
        (decision_at - observed_at) > chrono::Duration::seconds(MAX_QUOTE_AGE_SECONDS)
    {
        return Ok(rejected(state, "stale_quote"));
    }
    match calendar.day(decision_at.date_naive())? {
        CalendarDay::Trading => {}
        CalendarDay::Weekend => return Ok(rejected(state, "weekend_market_closed")),
        CalendarDay::Holiday => return Ok(rejected(state, "holiday_market_closed")),
        CalendarDay::Unknown => return Ok(rejected(state, "unknown_trading_date")),
    }
    if let Some(reason) = sessions.rejection(decision_at.time())? {
        return Ok(rejected(state, reason));
    }
    let rule = match instruments.quantity_rule(symbol, evidence.instrument.as_ref(), decision_at.date_naive()) {
        Ok(rule) => rule,
        Err(reason) => return Ok(rejected(state, reason)),
    };
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
    let Some(level) = evidence.execution.as_ref() else {
        return Ok(rejected(state, "execution_evidence_missing"));
    };
    if level.order_side != req.signal.side {
        return Ok(rejected(state, "execution_side_mismatch"));
    }
    let execution_at = timestamp(&level.observed_at)?;
    if execution_at.date_naive() != decision_at.date_naive()
        || (decision_at - execution_at) > chrono::Duration::seconds(MAX_QUOTE_AGE_SECONDS)
    {
        return Ok(rejected(state, "stale_execution_evidence"));
    }
    let price = level.price_cents;
    let quantity = match req.signal.side {
        Side::Buy => {
            if state.quantity(symbol) > 0 {
                return Ok(rejected(state, "position_already_open"));
            }
            let budget = state.cash_cents.checked_mul(BUY_BUDGET_BPS)
                .ok_or_else(|| RuleError("amount_overflow".into()))? / 10_000;
            let board_price = price.checked_mul(rule.step)
                .ok_or_else(|| RuleError("amount_overflow".into()))?;
            let mut qty = (budget / board_price) * rule.step;
            while qty >= rule.minimum {
                let gross = qty.checked_mul(price)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?;
                let fee = checked_fee(gross, COMMISSION_BPS)?.max(MIN_COMMISSION_CENTS);
                let total = gross.checked_add(fee)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?;
                if total <= budget && total <= state.cash_cents { break; }
                qty -= rule.step;
            }
            if qty < rule.minimum {
                return Ok(rejected(state, "insufficient_budget_for_board_lot_and_fees"));
            }
            qty
        }
        Side::Sell => {
            let qty = state.sellable_quantity(symbol, &trade_day);
            if qty < rule.minimum {
                return Ok(rejected(state, "no_t1_sellable_lot"));
            }
            qty
        }
    };
    if quantity % rule.step != 0 {
        return Ok(rejected(state, "unsupported_odd_lot_position"));
    }
    if quantity > rule.maximum {
        return Ok(rejected(state, "order_quantity_exceeds_instrument_limit"));
    }
    // Whole candidate or no fill: never silently downsize to visible capacity.
    if quantity > level.available_quantity {
        return Ok(rejected(state, "insufficient_execution_liquidity"));
    }
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

