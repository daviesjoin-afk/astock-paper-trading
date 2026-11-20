//! Whether a day's turnover could have absorbed the order at all.
//!
//! A paper ledger will happily buy ¥300,000 of a stock that traded ¥40,000 all
//! day. The fill price is then a fiction, and every downstream number inherits
//! it. This module adds the cheapest honest defence: refuse the order when it is
//! a large fraction of the day's turnover.
//!
//! What it deliberately does **not** do is estimate slippage. That needs the
//! order book — depth, spreads, how the book refills — none of which this
//! project ingests. A made-up slippage model would be a fabricated number with
//! a plausible shape, which is worse than a known simplification. So the rule
//! here is binary: fillable or not, at the close price.
//!
//! The threshold is a share of turnover, expressed in basis points. It is a
//! policy choice with no empirical backing in this codebase, so it is a
//! parameter rather than a constant, and the reason a trade was refused records
//! the fraction that was measured.

use super::RuleError;

/// Default ceiling: an order may not exceed this share of the day's turnover.
///
/// One percent is deliberately loose. It is not a claim about market impact —
/// it is the point past which "we filled at the close" stops being defensible
/// even as an approximation. A tighter number would reject orders that a real
/// book would absorb; a looser one lets a ¥300k order into a ¥50k day.
pub const DEFAULT_MAX_TURNOVER_SHARE_BPS: i64 = 100;

/// The outcome of a liquidity check.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Liquidity {
    /// The order is small enough, or turnover is unknown.
    Fine,
    /// The order would have moved more of the day than policy allows.
    TooLarge {
        order_cents: i64,
        turnover_cents: i64,
        share_bps: i64,
        limit_bps: i64,
    },
}

impl Liquidity {
    pub fn reason(&self) -> Option<String> {
        match self {
            Self::Fine => None,
            Self::TooLarge {
                order_cents,
                turnover_cents,
                share_bps,
                limit_bps,
            } => Some(format!(
                "illiquid: {order_cents} of {turnover_cents} cents ({share_bps} bps > {limit_bps} bps)"
            )),
        }
    }
}

/// Check one order against one day's turnover.
///
/// Unknown turnover (zero or negative) passes. That is the same fail-open
/// choice `tradeability` makes for a missing OHLC: refusing to fill everything
/// whenever the amount column is absent would silently freeze the ledger, and
/// a frozen ledger looks like "no signals" rather than "no data".
pub fn check(order_cents: i64, turnover_cents: i64, limit_bps: i64) -> Liquidity {
    if order_cents <= 0 || turnover_cents <= 0 || limit_bps <= 0 {
        return Liquidity::Fine;
    }
    // Share in bps, rounded up: an order at exactly the limit passes.
    let share_bps = (order_cents.saturating_mul(10_000) + turnover_cents - 1) / turnover_cents;
    if share_bps > limit_bps {
        Liquidity::TooLarge {
            order_cents,
            turnover_cents,
            share_bps,
            limit_bps,
        }
    } else {
        Liquidity::Fine
    }
}

/// Parse a policy value, rejecting nonsense rather than silently clamping.
pub fn parse_limit_bps(raw: &str) -> Result<i64, RuleError> {
    let value: i64 = raw
        .trim()
        .parse()
        .map_err(|_| RuleError("invalid_liquidity_limit".into()))?;
    if value <= 0 {
        return Err(RuleError("invalid_liquidity_limit".into()));
    }
    Ok(value)
}
