//! Whether a symbol could actually have traded in one direction today.
//!
//! A price-limit seal is not a price question: a stock can close at the
//! limit-down price with every bid gone, and no sale executes. Treating the
//! close as a fill silently turns "could not sell" into "sold at the bottom",
//! which both overstates realised losses and understates the positions a book
//! would still be carrying.

/// Which side of the order book was open.
///
/// `sealed_down` blocks sales, `sealed_up` blocks purchases. Both are decided
/// from the session's own high/low against the board's limit, so a stock that
/// merely closed at the limit after trading away from it stays fillable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Tradeability {
    pub can_buy: bool,
    pub can_sell: bool,
}

impl Tradeability {
    pub const OPEN: Self = Self {
        can_buy: true,
        can_sell: true,
    };
}

/// Daily price limit in basis points for an A-share code.
///
/// 30% Beijing, 20% ChiNext and STAR, 10% main board. Kept next to the
/// tradeability rule so a board's limit and the seal test cannot drift apart.
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

/// Decide from one session's OHLC whether each side could fill.
///
/// A sealed limit-down session is one where the day never traded above its own
/// low (a single price for the whole session) and that price sits at the
/// limit. The same test mirrored gives a sealed limit-up. `prev_close_cents`
/// must be the exchange's own previous close: without it a 10-for-5 dividend
/// looks like a limit-down and would block every sale for a day.
///
/// Missing or non-positive open/high/low cannot prove a seal, so the session
/// stays open rather than inventing a restriction.
pub fn tradeability(
    symbol: &str,
    prev_close_cents: i64,
    open_cents: i64,
    high_cents: i64,
    low_cents: i64,
    close_cents: i64,
    tradable: bool,
) -> Tradeability {
    if !tradable || prev_close_cents <= 0 || open_cents <= 0 || high_cents <= 0 || low_cents <= 0 {
        return Tradeability::OPEN;
    }
    // A-share limits are rounded to the cent, so allow one cent of slack.
    let limit = daily_limit_bps(symbol);
    let floor = prev_close_cents - (prev_close_cents * limit + 9_999) / 10_000;
    let ceiling = prev_close_cents + (prev_close_cents * limit) / 10_000;
    let single_price = open_cents == high_cents && high_cents == low_cents && low_cents == close_cents;
    if single_price && close_cents <= floor + 1 {
        return Tradeability {
            can_buy: true,
            can_sell: false,
        };
    }
    if single_price && close_cents >= ceiling - 1 {
        return Tradeability {
            can_buy: false,
            can_sell: true,
        };
    }
    Tradeability::OPEN
}
