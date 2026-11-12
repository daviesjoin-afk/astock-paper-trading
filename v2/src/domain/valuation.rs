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

/// Mark an existing book from a price set.
///
/// `valuation` is the only read that may use a halted row's carried reference
/// close. A missing row still fails the whole valuation: absent data cannot be
/// confused with a suspension.
pub fn market_value_from_set(
    book: &SlotBook,
    set: &super::PriceSet,
    day: &str,
) -> Result<i64, RuleError> {
    let mut total = 0i64;
    for position in &book.positions {
        let price = set
            .valuation(&position.symbol, day)
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

/// Closing equity and the cash moved in after that day's trading/valuation.
/// Transfers between slots are external flows for a slot, zero for the account.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PerformanceInput {
    pub day: String,
    pub equity_cents: i64,
    pub net_flow_cents: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct PerformancePoint {
    pub day: String,
    pub equity_cents: i64,
    pub net_flow_cents: i64,
    pub profit_cents: i64,
    pub return_bps: i64,
    pub max_drawdown_bps: i64,
    /// None means the initial principal, before the first closing mark.
    pub peak_day: Option<String>,
    pub trough_day: Option<String>,
}

/// Display-only, time-weighted performance. Do not use this to change the
/// existing allocation rule. A day's return is (equity - end-of-day flow) /
/// previous closing equity; chain those ratios to remove capital transfers.
pub fn performance_curve(
    initial_cents: i64,
    inputs: &[PerformanceInput],
) -> Result<Vec<PerformancePoint>, RuleError> {
    const SCALE: i128 = 1_000_000_000_000_000_000;
    if initial_cents <= 0 {
        return Err(RuleError("invalid_performance_principal".into()));
    }
    let mut previous = initial_cents;
    let mut flows = 0i64;
    let mut nav = SCALE;
    let mut peak = SCALE;
    let mut equity_peak = initial_cents;
    let mut had_flows = false;
    let mut running_peak_day = None;
    let mut worst_peak_day = None;
    let mut trough_day = None;
    let mut worst = 0;
    let mut result: Vec<PerformancePoint> = Vec::with_capacity(inputs.len());
    for input in inputs {
        if result.last().is_some_and(|last| last.day >= input.day) {
            return Err(RuleError("performance_days_not_increasing".into()));
        }
        let before_flow = input
            .equity_cents
            .checked_sub(input.net_flow_cents)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        if input.equity_cents < 0 || before_flow < 0 || (previous == 0 && before_flow != 0) {
            return Err(RuleError("invalid_performance_flow".into()));
        }
        had_flows |= input.net_flow_cents != 0;
        if !had_flows {
            nav = i128::from(input.equity_cents) * SCALE / i128::from(initial_cents);
        } else if previous > 0 {
            nav = nav
                .checked_mul(i128::from(before_flow))
                .and_then(|value| value.checked_add(i128::from(previous) / 2))
                .ok_or_else(|| RuleError("amount_overflow".into()))?
                / i128::from(previous);
        }
        flows = flows
            .checked_add(input.net_flow_cents)
            .ok_or_else(|| RuleError("amount_overflow".into()))?;
        if nav > peak {
            peak = nav;
            running_peak_day = Some(input.day.clone());
        }
        equity_peak = equity_peak.max(input.equity_cents);
        let dd = if !had_flows {
            i128::from(equity_peak - input.equity_cents) * 10_000 / i128::from(equity_peak)
        } else {
            (peak - nav)
                .checked_mul(10_000)
                .ok_or_else(|| RuleError("amount_overflow".into()))?
                / peak
        };
        if dd > worst {
            worst = dd;
            worst_peak_day = running_peak_day.clone();
            trough_day = Some(input.day.clone());
        }
        result.push(PerformancePoint {
            day: input.day.clone(),
            equity_cents: input.equity_cents,
            net_flow_cents: input.net_flow_cents,
            profit_cents: input
                .equity_cents
                .checked_sub(initial_cents)
                .and_then(|value| value.checked_sub(flows))
                .ok_or_else(|| RuleError("amount_overflow".into()))?,
            return_bps: i64::try_from(if !had_flows {
                i128::from(input.equity_cents - initial_cents) * 10_000 / i128::from(initial_cents)
            } else {
                (nav - SCALE)
                    .checked_mul(10_000)
                    .ok_or_else(|| RuleError("amount_overflow".into()))?
                    / SCALE
            })
            .map_err(|_| RuleError("amount_overflow".into()))?,
            max_drawdown_bps: worst as i64,
            peak_day: worst_peak_day.clone(),
            trough_day: trough_day.clone(),
        });
        previous = input.equity_cents;
    }
    Ok(result)
}

#[cfg(test)]
mod performance_tests {
    use super::*;

    fn curve(initial: i64, rows: &[(i64, i64)]) -> Vec<PerformancePoint> {
        let inputs: Vec<_> = rows
            .iter()
            .enumerate()
            .map(|(index, &(equity, flow))| PerformanceInput {
                day: format!("2026-11-{:02}", index + 1),
                equity_cents: equity,
                net_flow_cents: flow,
            })
            .collect();
        performance_curve(initial, &inputs).unwrap()
    }

    #[test]
    fn transfers_alone_have_no_return_or_drawdown() {
        let points = curve(100, &[(100, 0), (150, 50), (40, -110)]);
        assert!(
            points
                .iter()
                .all(|p| p.profit_cents == 0 && p.return_bps == 0 && p.max_drawdown_bps == 0)
        );
        assert_eq!(points.last().unwrap().trough_day, None);
    }

    #[test]
    fn end_of_day_flow_does_not_dilute_that_days_return() {
        let points = curve(100, &[(160, 50), (171, -5)]);
        assert_eq!(points[0].profit_cents, 10);
        assert_eq!(points[0].return_bps, 1_000);
        assert_eq!(points[1].profit_cents, 26);
        assert_eq!(points[1].return_bps, 2_100);
    }

    #[test]
    fn later_high_does_not_rewrite_the_worst_drawdown_dates() {
        let points = curve(100, &[(120, 0), (90, 0), (130, 0), (117, 0)]);
        let last = points.last().unwrap();
        assert_eq!(last.max_drawdown_bps, 2_500);
        assert_eq!(last.peak_day.as_deref(), Some("2026-11-01"));
        assert_eq!(last.trough_day.as_deref(), Some("2026-11-02"));
    }

    #[test]
    fn first_day_loss_is_a_drawdown_from_principal() {
        let points = curve(100, &[(90, 0)]);
        assert_eq!(points[0].max_drawdown_bps, 1_000);
        assert_eq!(points[0].peak_day, None);
        assert_eq!(points[0].trough_day.as_deref(), Some("2026-11-01"));
    }

    #[test]
    fn no_flow_returns_keep_exact_cent_precision_over_multiple_days() {
        let points = curve(10_001, &[(10_002, 0), (20_002, 0), (10_001, 0)]);
        assert_eq!(points[1].return_bps, 10_000);
        assert_eq!(points[2].return_bps, 0);
        assert_eq!(points[2].max_drawdown_bps, 5_000);
    }

    #[test]
    fn withdrawing_all_capital_and_refunding_does_not_create_a_return() {
        let points = curve(100, &[(0, -100), (50, 50), (55, 0)]);
        assert_eq!(points[2].return_bps, 1_000);
        assert_eq!(points[2].profit_cents, 5);
    }

    #[test]
    fn malformed_performance_evidence_is_rejected() {
        let input = PerformanceInput {
            day: "2026-11-01".into(),
            equity_cents: 10,
            net_flow_cents: 20,
        };
        assert!(performance_curve(100, std::slice::from_ref(&input)).is_err());
        assert!(performance_curve(0, &[]).is_err());
        let valid = PerformanceInput {
            net_flow_cents: 0,
            ..input
        };
        assert!(performance_curve(100, &[valid.clone(), valid]).is_err());
    }
}
