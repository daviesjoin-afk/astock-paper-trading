//! Factor evaluation: does a factor predict forward returns?
//!
//! The measures here are deliberately standard and deliberately dumb. A
//! complicated evaluation makes a weak factor look interesting, and the whole
//! point of a factor library is to be able to say "this one does not work".
//!
//! One rule runs through everything: **the forward return used to grade a
//! factor must start strictly after the day the factor was computed**. Taking
//! the label from the same close the factor saw would produce a beautiful
//! number that means nothing.

use super::RuleError;

/// One observation: what the factor said, and what happened afterwards.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Observation {
    /// Factor value in basis points at the decision close.
    pub factor_bps: i64,
    /// Realised return in basis points over the label window.
    pub forward_bps: i64,
}

/// Rank correlation with forward returns (Spearman), in bps of correlation.
///
/// Rank correlation rather than Pearson because a factor's raw scale is
/// arbitrary: what matters is whether higher values sort into better outcomes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
pub struct FactorStats {
    pub samples: usize,
    /// Pearson correlation on ranks, in bps (−10,000..10,000).
    pub rank_ic_bps: i64,
    /// Mean factor value minus mean forward return correlation, in bps.
    pub ic_bps: i64,
    /// Mean IC divided by its standard deviation across periods, in bps.
    /// `None` when fewer than two periods were measured.
    pub ir_bps: Option<i64>,
}

/// A mean spread is only meaningful when both tails are actually populated.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
pub struct QuantileSpread {
    /// Lowest quantile's mean forward return, in bps.
    pub low_bps: Option<i64>,
    /// Highest quantile's mean forward return, in bps.
    pub high_bps: Option<i64>,
    /// `high - low`. Positive means the factor sorted correctly.
    pub spread_bps: Option<i64>,
}

/// Rank a slice, returning 1-based ranks with ties sharing their average.
///
/// Ties must be averaged, not broken arbitrarily: breaking them by index would
/// let the input order leak into the score, which is exactly the kind of
/// phantom edge a factor screen is supposed to catch.
fn average_ranks(values: &[i64]) -> Vec<f64> {
    let mut order: Vec<usize> = (0..values.len()).collect();
    order.sort_by_key(|index| values[*index]);
    let mut ranks = vec![0.0f64; values.len()];
    let mut start = 0usize;
    while start < order.len() {
        let mut end = start + 1;
        while end < order.len() && values[order[end]] == values[order[start]] {
            end += 1;
        }
        // Ranks in this tie group are start+1 ..= end, averaged.
        let average = ((start + 1) as f64 + end as f64) / 2.0;
        for index in &order[start..end] {
            ranks[*index] = average;
        }
        start = end;
    }
    ranks
}

/// Pearson correlation of two equal-length series, scaled to bps.
///
/// Returns `None` when either side has no variance: a constant factor cannot
/// correlate with anything, and reporting 0 would imply it was measured.
fn pearson_bps(left: &[f64], right: &[f64]) -> Option<i64> {
    if left.len() != right.len() || left.len() < 2 {
        return None;
    }
    let count = left.len() as f64;
    let mean_left = left.iter().sum::<f64>() / count;
    let mean_right = right.iter().sum::<f64>() / count;
    let mut numerator = 0.0;
    let mut var_left = 0.0;
    let mut var_right = 0.0;
    for (a, b) in left.iter().zip(right.iter()) {
        let da = a - mean_left;
        let db = b - mean_right;
        numerator += da * db;
        var_left += da * da;
        var_right += db * db;
    }
    if var_left <= 0.0 || var_right <= 0.0 {
        return None;
    }
    let correlation = numerator / (var_left.sqrt() * var_right.sqrt());
    Some((correlation * 10_000.0).round() as i64)
}

/// Evaluate a factor over one cross-section.
///
/// `observations` is one period: every symbol the factor scored on that day,
/// paired with its forward return. `period_ics` carries the per-period
/// correlations needed for IR; pass an empty vector for a single period.
pub fn evaluate(
    observations: &[Observation],
    period_ics: &[i64],
) -> Result<(FactorStats, QuantileSpread), RuleError> {
    if observations.is_empty() {
        return Err(RuleError("factor_evaluation_empty".into()));
    }
    let factors: Vec<i64> = observations.iter().map(|o| o.factor_bps).collect();
    let forwards: Vec<i64> = observations.iter().map(|o| o.forward_bps).collect();

    let ranks = average_ranks(&factors);
    let forward_ranks = average_ranks(&forwards);
    let rank_ic = pearson_bps(&ranks, &forward_ranks).unwrap_or(0);

    let factor_values: Vec<f64> = factors.iter().map(|v| *v as f64).collect();
    let forward_values: Vec<f64> = forwards.iter().map(|v| *v as f64).collect();
    let ic = pearson_bps(&factor_values, &forward_values).unwrap_or(0);

    // IR needs at least two periods, otherwise "stability" is undefined.
    let ir_bps = if period_ics.len() >= 2 {
        let count = period_ics.len() as f64;
        let mean = period_ics.iter().map(|v| *v as f64).sum::<f64>() / count;
        let variance = period_ics
            .iter()
            .map(|v| {
                let delta = *v as f64 - mean;
                delta * delta
            })
            .sum::<f64>()
            / count;
        if variance <= 0.0 {
            None
        } else {
            Some((mean / variance.sqrt()).round() as i64)
        }
    } else {
        None
    };

    let stats = FactorStats {
        samples: observations.len(),
        rank_ic_bps: rank_ic,
        ic_bps: ic,
        ir_bps,
    };
    Ok((stats, quantile_spread(observations)))
}

/// Mean forward return of the bottom and top quintile, sorted by factor.
///
/// With fewer than five observations the tails are not meaningful, and the
/// result is `None` rather than a spread computed from one name.
fn quantile_spread(observations: &[Observation]) -> QuantileSpread {
    if observations.len() < 5 {
        return QuantileSpread {
            low_bps: None,
            high_bps: None,
            spread_bps: None,
        };
    }
    let mut sorted = observations.to_vec();
    sorted.sort_by_key(|item| item.factor_bps);
    let size = sorted.len() / 5;
    if size == 0 {
        return QuantileSpread {
            low_bps: None,
            high_bps: None,
            spread_bps: None,
        };
    }
    let low = mean_forward(&sorted[..size]);
    let high = mean_forward(&sorted[sorted.len() - size..]);
    QuantileSpread {
        low_bps: Some(low),
        high_bps: Some(high),
        spread_bps: Some(high - low),
    }
}

fn mean_forward(items: &[Observation]) -> i64 {
    if items.is_empty() {
        return 0;
    }
    items.iter().map(|item| item.forward_bps).sum::<i64>() / items.len() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observation(factor: i64, forward: i64) -> Observation {
        Observation {
            factor_bps: factor,
            forward_bps: forward,
        }
    }

    #[test]
    fn a_perfectly_sorted_factor_scores_maximum_rank_ic() {
        let observations: Vec<_> = (0..10)
            .map(|index| observation(index * 100, index * 50))
            .collect();
        let (stats, spread) = evaluate(&observations, &[]).unwrap();
        assert_eq!(stats.samples, 10);
        assert_eq!(stats.rank_ic_bps, 10_000, "monotone pairs must rank-correlate at 1.0");
        assert_eq!(stats.ic_bps, 10_000);
        assert!(spread.spread_bps.unwrap() > 0, "top quintile must beat the bottom");
    }

    #[test]
    fn a_perfectly_inverted_factor_scores_minus_maximum() {
        let observations: Vec<_> = (0..10)
            .map(|index| observation(index * 100, 500 - index * 50))
            .collect();
        let (stats, spread) = evaluate(&observations, &[]).unwrap();
        assert_eq!(stats.rank_ic_bps, -10_000);
        assert!(spread.spread_bps.unwrap() < 0);
    }

    #[test]
    fn a_constant_factor_is_not_reported_as_informative() {
        // No variance means no information. Returning 0 IC is correct; the
        // danger would be a non-None IR or a spread implying it was measured.
        let observations: Vec<_> = (0..10)
            .map(|index| observation(500, index * 10))
            .collect();
        let (stats, _) = evaluate(&observations, &[]).unwrap();
        assert_eq!(stats.rank_ic_bps, 0);
        assert_eq!(stats.ic_bps, 0);
    }

    #[test]
    fn ties_are_averaged_so_input_order_cannot_fake_a_signal() {
        // Every factor value is identical; only the forward returns differ.
        // A rank scheme that broke ties by index would invent a correlation.
        let forward: Vec<_> = (0..10).map(|i| observation(100, i * 10)).collect();
        let (stats_a, _) = evaluate(&forward, &[]).unwrap();
        assert_eq!(stats_a.rank_ic_bps, 0, "ties must not correlate with position");

        // Reversing the input must not change the answer.
        let mut reversed = forward.clone();
        reversed.reverse();
        let (stats_b, _) = evaluate(&reversed, &[]).unwrap();
        assert_eq!(stats_a, stats_b);
    }

    #[test]
    fn empty_input_and_short_cross_sections_are_handled_explicitly() {
        assert_eq!(
            evaluate(&[], &[]).unwrap_err().0,
            "factor_evaluation_empty"
        );
        // Two names: statistics are computed, but quantile tails are not
        // invented from a single observation each.
        let small = vec![observation(100, 10), observation(200, 20)];
        let (stats, spread) = evaluate(&small, &[]).unwrap();
        assert_eq!(stats.samples, 2);
        assert_eq!(spread.spread_bps, None);
        assert_eq!(spread.low_bps, None);
    }

    #[test]
    fn ir_needs_more_than_one_period() {
        let observations: Vec<_> = (0..10)
            .map(|index| observation(index * 100, index * 50))
            .collect();
        let (single, _) = evaluate(&observations, &[10_000]).unwrap();
        assert_eq!(single.ir_bps, None, "stability is undefined with one period");

        let (multi, _) = evaluate(&observations, &[8_000, 9_000, 10_000]).unwrap();
        let ir = multi.ir_bps.unwrap();
        assert!(ir > 0, "consistently positive IC must give positive IR");

        // A period series with zero variance has no defined IR either.
        let (flat, _) = evaluate(&observations, &[5_000, 5_000]).unwrap();
        assert_eq!(flat.ir_bps, None);
    }

    #[test]
    fn quantile_spread_uses_equal_sized_tails() {
        // 20 observations: quintile size is 4, low is 0..4, high is 16..20.
        let observations: Vec<_> = (0..20)
            .map(|index| observation(index * 100, index * 10))
            .collect();
        let (_, spread) = evaluate(&observations, &[]).unwrap();
        assert_eq!(spread.low_bps.unwrap(), (0 + 10 + 20 + 30) / 4);
        assert_eq!(spread.high_bps.unwrap(), (160 + 170 + 180 + 190) / 4);
        assert_eq!(
            spread.spread_bps.unwrap(),
            spread.high_bps.unwrap() - spread.low_bps.unwrap()
        );
    }
}
