//! Mark-to-market from explicit reviewed closes.

use super::{RuleError, SlotBook};

pub fn market_value(
    book: &SlotBook,
    prices: &std::collections::BTreeMap<String, i64>,
) -> Result<i64, RuleError> {
    let mut total = 0i64;
    for position in &book.positions {
        let price = prices
            .get(&position.symbol)
            .ok_or_else(|| RuleError("missing_close_price".into()))?;
        total = total
            .checked_add(price * position.quantity)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
    }
    Ok(total)
}

pub fn drawdown_bps(points: &[i64], base: i64) -> i64 {
    if points.is_empty() || base <= 0 {
        return 0;
    }
    let mut peak = points[0];
    let mut worst = 0i64;
    for point in points {
        peak = peak.max(*point);
        if peak > 0 {
            worst = worst.max((peak - *point) * 10_000 / peak);
        }
    }
    worst
}
