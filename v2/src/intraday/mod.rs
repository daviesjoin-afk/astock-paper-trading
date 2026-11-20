//! Independent intraday paper kernel.
//!
//! This module owns its own SQLite database and every write in a round. It is
//! deliberately separate from `crate::store`: daily settlement has different
//! semantics and must not be reused, migrated, or modified by live decisions.
//! All facts about time come from the caller; no wall-clock trading-day choice
//! is made here. Python callers may fetch quotes and transport model output,
//! but scoring, risk limits, clamping and ledger writes are owned by Rust.

pub mod decision;
pub mod labels;
pub mod store;
#[cfg(test)]
mod tests;

use serde::{Deserialize, Serialize};

pub use decision::{
    Advice, AdviceSuggestion, AiApplication, ClampedSuggestion, DecisionSignal, EvaluatedQuote,
    FactorBreakdown, MAX_ADVICE_ITEMS, MAX_AI_BIAS, MIN_AI_CONFIDENCE, ai_clamp, apply_ai_advice,
    evaluate_quote, proposal_json, proposal_within_bounds, score_candidates,
    validate_advice_shape,
};
pub use store::{
    AccountView, AiObservation, EquityPoint, EvolutionPreview, IntradayError, IntradayResult,
    IntradayStore, RiskCounter, RiskSummary, RoundOutcome, RoundSummary, SlotView,
    StoredDecision, SymbolArchive, SymbolArchiveEvent, TradeRecord,
};

/// A-share boards use different price limits. Callers must supply the real
/// snapshot's limits; the fallback is only a bounded arithmetic guard.
pub const BPS: i64 = 10_000;
pub const LOT_SIZE: i64 = 100;
pub const MIN_INITIAL_CASH_CENTS: i64 = 10_000;
pub const MAX_INITIAL_CASH_CENTS: i64 = 100_000_000_000;
pub const MIN_COMMISSION_CENTS: i64 = 500;
pub const COMMISSION_BPS: i64 = 3;
pub const STAMP_DUTY_BPS: i64 = 5;
pub const MIN_CONFIDENCE: i64 = MIN_AI_CONFIDENCE;
pub const MAX_SUGGESTIONS: usize = MAX_ADVICE_ITEMS;
pub const MAX_BIAS: i64 = MAX_AI_BIAS;
pub const MAX_BLOCKED_SYMBOLS: usize = decision::MAX_BLOCKED_SYMBOLS;
pub const MAX_SYMBOL_CHARS: usize = 32;
pub const MAX_ROUND_ID_CHARS: usize = 128;
pub const MAX_TIME_CHARS: usize = 128;
pub const MAX_REASON_CHARS: usize = 512;

/// One account's two independently cash-managed strategy slots.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Slot {
    Steady,
    Aggressive,
}

impl Slot {
    pub const ALL: [Self; 2] = [Self::Steady, Self::Aggressive];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Steady => "steady",
            Self::Aggressive => "aggressive",
        }
    }

    pub fn parse(raw: &str) -> IntradayResult<Self> {
        match raw {
            "steady" => Ok(Self::Steady),
            "aggressive" => Ok(Self::Aggressive),
            _ => Err(IntradayError::Rules(format!("unknown_slot:{raw}"))),
        }
    }

    /// Each slot starts with half of one account's cash. The odd cent, if any,
    /// is assigned to aggressive so the two books always sum to the account.
    pub fn initial_cash(self, initial_cash_cents: i64) -> i64 {
        let steady = initial_cash_cents / 2;
        match self {
            Self::Steady => steady,
            Self::Aggressive => initial_cash_cents - steady,
        }
    }

    /// Single-symbol ceiling and cash floor as basis points of slot equity.
    pub fn risk_bps(self) -> (i64, i64) {
        match self {
            Self::Steady => (1_500, 1_000),
            Self::Aggressive => (3_000, 500),
        }
    }

    /// Score required to open, and score at which an existing holding exits.
    pub fn thresholds(self) -> (i64, i64) {
        match self {
            Self::Steady => (35, -10),
            Self::Aggressive => (28, -18),
        }
    }

    /// Hard loss and gain bands relative to average cost, in basis points.
    pub fn exit_bands(self) -> (i64, i64) {
        match self {
            Self::Steady => (-1_200, 2_000),
            Self::Aggressive => (-700, 1_500),
        }
    }

    /// Distinct symbols one slot may hold. Five leaves room for the idle cash
    /// a filled slot used to strand: at three names the book reached its
    /// position limit while ~40% of the capital stayed in cash, and a filled
    /// slot could only free a seat by exiting a holding.
    pub fn max_positions(self) -> usize {
        match self {
            Self::Steady => 5,
            Self::Aggressive => 5,
        }
    }
}

/// A real-time snapshot. Every price is integer cents and every share count is
/// a whole number. Optional fields mean "not supplied", never zero.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Quote {
    pub symbol: String,
    /// Display name supplied by the snapshot source. It is never derived from
    /// the symbol; an absent or blank name stays NULL downstream.
    #[serde(default)]
    pub name: Option<String>,
    /// Last traded price at `quote_time`; never a previous close substitute.
    pub price_cents: i64,
    pub prev_close_cents: i64,
    #[serde(default)]
    pub open_cents: Option<i64>,
    #[serde(default)]
    pub high_cents: Option<i64>,
    #[serde(default)]
    pub low_cents: Option<i64>,
    #[serde(default)]
    pub limit_up_cents: Option<i64>,
    #[serde(default)]
    pub limit_down_cents: Option<i64>,
    #[serde(default)]
    pub volume: Option<i64>,
    #[serde(default)]
    pub amount_cents: Option<i64>,
    /// Average volume over an explicit trailing window supplied by the caller.
    /// No value means the volume-ratio factor is omitted, not zero.
    #[serde(default)]
    pub avg_volume: Option<i64>,
    /// Optional trailing daily closes, oldest first, from an explicit historical
    /// source. Short or absent history restricts the score to snapshot factors.
    #[serde(default)]
    pub trailing_closes: Vec<i64>,
    #[serde(default)]
    pub tradable: Option<bool>,
}

impl Quote {
    /// A fresh quote is required for both scoring and filling. A missing or
    /// non-positive last price is never replaced with `prev_close_cents`.
    pub fn has_live_price(&self) -> bool {
        self.price_cents > 0 && self.tradable.unwrap_or(true)
    }

    pub fn open(&self) -> Option<i64> {
        self.open_cents.filter(|value| *value > 0)
    }

    pub fn high(&self) -> Option<i64> {
        self.high_cents.filter(|value| *value > 0)
    }

    pub fn low(&self) -> Option<i64> {
        self.low_cents.filter(|value| *value > 0)
    }

    pub fn volume(&self) -> Option<i64> {
        self.volume.filter(|value| *value > 0)
    }

    pub fn avg_volume(&self) -> Option<i64> {
        self.avg_volume.filter(|value| *value > 0)
    }

    pub fn amount(&self) -> Option<i64> {
        self.amount_cents.filter(|value| *value > 0)
    }

    /// Board-derived fallback when the quote transport omitted explicit limits.
    /// It is used only for safety gates; audit rows retain the supplied values.
    pub fn effective_limit_up(&self) -> i64 {
        if let Some(limit) = self.limit_up_cents.filter(|value| *value > 0) {
            return limit;
        }
        let bps = daily_limit_bps(&self.symbol);
        self.prev_close_cents + self.prev_close_cents.saturating_mul(bps) / BPS
    }

    pub fn effective_limit_down(&self) -> i64 {
        if let Some(limit) = self.limit_down_cents.filter(|value| *value > 0) {
            return limit;
        }
        let bps = daily_limit_bps(&self.symbol);
        self.prev_close_cents - (self.prev_close_cents.saturating_mul(bps) + BPS - 1) / BPS
    }

    /// Validate facts needed for a real fill. Missing shape/volume fields are
    /// permitted and only make their factors absent; price and previous close
    /// are required because no other field may stand in for them.
    pub fn validate(&self) -> IntradayResult<()> {
        if self.symbol.trim().is_empty()
            || self.symbol.chars().count() > MAX_SYMBOL_CHARS
        {
            return Err(IntradayError::Rules("invalid_symbol".into()));
        }
        if self.price_cents <= 0 {
            return Err(IntradayError::Rules("invalid_live_price".into()));
        }
        if self.prev_close_cents <= 0 {
            return Err(IntradayError::Rules("invalid_prev_close".into()));
        }
        if !self.tradable.unwrap_or(true) {
            return Err(IntradayError::Rules("not_tradable".into()));
        }
        Ok(())
    }

    /// A sealed board is fail-closed: a one-price session at a limit blocks the
    /// side that cannot fill. Merely trading at the limit with a real range is
    /// allowed; a missing OHLC range never invents a seal.
    pub fn can_buy(&self) -> bool {
        if !self.has_live_price() {
            return false;
        }
        let (Some(open), Some(high), Some(low)) = (self.open(), self.high(), self.low()) else {
            return true;
        };
        !(open == high && high == low && low == self.price_cents
            && self.price_cents >= self.effective_limit_up() - 1)
    }

    pub fn can_sell(&self) -> bool {
        if !self.has_live_price() {
            return false;
        }
        let (Some(open), Some(high), Some(low)) = (self.open(), self.high(), self.low()) else {
            return true;
        };
        !(open == high && high == low && low == self.price_cents
            && self.price_cents <= self.effective_limit_down() + 1)
    }
}

/// 30% Beijing, 20% STAR/ChiNext, 10% main board, in basis points.
pub fn daily_limit_bps(symbol: &str) -> i64 {
    let code = symbol.split('.').next().unwrap_or("");
    let board = symbol.rsplit('.').next().unwrap_or("");
    let prefix = &code[..code.len().min(3)];
    match (board, prefix) {
        ("BJ", _) => 3_000,
        ("SH", "688") | ("SH", "689") => 2_000,
        ("SZ", "300") | ("SZ", "301") => 2_000,
        _ => 1_000,
    }
}

/// Commission: max(3 bps, 5 CNY), plus 5 bps stamp duty on sells. All integer
/// cents; overflow returns a rule error so a bad input cannot wrap cash.
pub fn fee_cents(side: &str, notional_cents: i64) -> IntradayResult<i64> {
    if notional_cents <= 0 {
        return Err(IntradayError::Rules("invalid_notional".into()));
    }
    let commission = notional_cents
        .checked_mul(COMMISSION_BPS)
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?
        / BPS;
    let commission = commission.max(MIN_COMMISSION_CENTS);
    let stamp = match side {
        "buy" => 0,
        "sell" => notional_cents
            .checked_mul(STAMP_DUTY_BPS)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?
            / BPS,
        _ => return Err(IntradayError::Rules("unsupported_side".into())),
    };
    commission
        .checked_add(stamp)
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))
}

pub fn split_initial_cash(initial_cash_cents: i64) -> IntradayResult<[i64; 2]> {
    if !(MIN_INITIAL_CASH_CENTS..=MAX_INITIAL_CASH_CENTS).contains(&initial_cash_cents) {
        return Err(IntradayError::Rules("invalid_initial_cash".into()));
    }
    Ok([
        Slot::Steady.initial_cash(initial_cash_cents),
        Slot::Aggressive.initial_cash(initial_cash_cents),
    ])
}

/// One bounded entry experiment for an isolated research ledger.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ResearchEntryPolicy {
    #[default]
    Baseline,
    WeakEntryPause,
    WeakEntryBuffer { points: i64 },
    StrongEntryRelief { points: i64 },
}

/// Request to execute one exactly identified three-minute round.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRoundRequest {
    pub round_id: String,
    pub quote_time: String,
    #[serde(default)]
    pub fetched_at: Option<String>,
    pub quotes: Vec<Quote>,
    #[serde(default)]
    pub advice: Option<Advice>,
    /// Optional caller-supplied label; the kernel also derives its own regime
    /// note from the actual quotes it saw.
    #[serde(default)]
    pub note: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateAccountRequest {
    pub initial_cash_cents: i64,
    /// Caller-owned timestamp. No wall clock is read by the kernel.
    pub created_at: Option<String>,
}

/// One slot's durable book, loaded at the start of a round transaction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotBook {
    pub slot: Slot,
    pub cash_cents: i64,
    pub positions: Vec<Position>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Position {
    pub slot: Slot,
    pub symbol: String,
    #[serde(default)]
    pub name: Option<String>,
    pub quantity: i64,
    pub sellable_quantity: i64,
    pub avg_cost_cents: i64,
    pub updated_at: String,
    /// Latest traded price from the most recent stored snapshot for this symbol.
    /// Absent when no snapshot exists; never filled from avg_cost or a bar.
    #[serde(default)]
    pub last_price_cents: Option<i64>,
    /// quantity × last_price at read time. Absent when last_price is absent.
    #[serde(default)]
    pub market_value_cents: Option<i64>,
}

/// Apply a buy in memory. New shares are deliberately not sellable until the
/// caller starts a new trading day through `roll_trading_day`.
pub fn apply_buy(
    book: &mut SlotBook,
    symbol: &str,
    quantity: i64,
    price_cents: i64,
    fee: i64,
    at: &str,
) -> IntradayResult<()> {
    if quantity <= 0 || quantity % LOT_SIZE != 0 || price_cents <= 0 || fee < 0 {
        return Err(IntradayError::Rules("invalid_buy".into()));
    }
    let notional = quantity
        .checked_mul(price_cents)
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
    let total = notional
        .checked_add(fee)
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
    if total > book.cash_cents {
        return Err(IntradayError::Rules("insufficient_cash".into()));
    }
    if let Some(position) = book
        .positions
        .iter_mut()
        .find(|item| item.symbol == symbol)
    {
        let old_quantity = position.quantity;
        let old_cost = position
            .avg_cost_cents
            .checked_mul(old_quantity)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
        let new_quantity = old_quantity
            .checked_add(quantity)
            .ok_or_else(|| IntradayError::Rules("quantity_overflow".into()))?;
        position.avg_cost_cents = old_cost
            .checked_add(notional)
            .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?
            / new_quantity;
        position.quantity = new_quantity;
        position.updated_at = at.to_string();
    } else {
        book.positions.push(Position {
            slot: book.slot,
            symbol: symbol.to_string(),
            name: None,
            quantity,
            sellable_quantity: 0,
            avg_cost_cents: price_cents,
            updated_at: at.to_string(),
            // Derived at read time from quote_snapshot; the durable book never
            // stores a mark that could go stale.
            last_price_cents: None,
            market_value_cents: None,
        });
    }
    book.cash_cents = book
        .cash_cents
        .checked_sub(total)
        .ok_or_else(|| IntradayError::Rules("negative_cash".into()))?;
    if book.cash_cents < 0 {
        return Err(IntradayError::Rules("negative_cash".into()));
    }
    Ok(())
}

/// Apply a sell only against T+1 sellable shares.
pub fn apply_sell(
    book: &mut SlotBook,
    symbol: &str,
    quantity: i64,
    price_cents: i64,
    fee: i64,
    at: &str,
) -> IntradayResult<()> {
    if quantity <= 0 || quantity % LOT_SIZE != 0 || price_cents <= 0 || fee < 0 {
        return Err(IntradayError::Rules("invalid_sell".into()));
    }
    let index = book
        .positions
        .iter()
        .position(|item| item.symbol == symbol)
        .ok_or_else(|| IntradayError::Rules("position_not_found".into()))?;
    let existing = &book.positions[index];
    if quantity > existing.sellable_quantity || quantity > existing.quantity {
        return Err(IntradayError::Rules("t_plus_one_violation".into()));
    }
    let notional = quantity
        .checked_mul(price_cents)
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
    if fee > notional {
        return Err(IntradayError::Rules("negative_cash".into()));
    }
    book.cash_cents = book
        .cash_cents
        .checked_add(notional)
        .and_then(|value| value.checked_sub(fee))
        .ok_or_else(|| IntradayError::Rules("amount_overflow".into()))?;
    if book.cash_cents < 0 {
        return Err(IntradayError::Rules("negative_cash".into()));
    }
    {
        let position = &mut book.positions[index];
        position.quantity -= quantity;
        position.sellable_quantity -= quantity;
        position.updated_at = at.to_string();
        if position.sellable_quantity < 0 || position.quantity < 0 {
            return Err(IntradayError::Rules("oversold_position".into()));
        }
    }
    if book.positions[index].quantity == 0 {
        book.positions.remove(index);
    }
    Ok(())
}
