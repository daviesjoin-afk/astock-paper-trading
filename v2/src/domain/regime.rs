//! Market regime: is the broad market trending, or chopping sideways?
//!
//! Motivation is measured, not theoretical. Replaying 2026 with the scored
//! aggressive strategy produced 1-2 months of +2% entries and 3+ months of
//! -3% entries from the *same* rule. A breakout rule is not broken by that
//! result -- it is a trend-following rule being run through a market that
//! stopped trending. The fix is not another strategy, it is knowing which
//! regime you are in.
//!
//! The classifier is deliberately blunt. A three-state answer from a breadth
//! reading and a 20/60 moving-average pair is something that can be explained,
//! debugged and argued with; a fitted probability would not be.

use super::Bar;

/// What the market looked like on a settled day.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MarketRegime {
    /// Index above a rising 60-day average, and most names participating.
    Trending,
    /// Index without direction, participation patchy. Breakouts fail here.
    Choppy,
    /// Index below a falling 60-day average. Breakouts fail *and* the losses
    /// run, so this is not merely "not trending".
    Declining,
    /// Not enough index history to say. Treated as choppy by the policy, so an
    /// unknown market never unlocks the aggressive slot.
    Unknown,
}

impl MarketRegime {
    pub fn as_str(self) -> &'static str {
        match self {
            MarketRegime::Trending => "trending",
            MarketRegime::Choppy => "choppy",
            MarketRegime::Declining => "declining",
            MarketRegime::Unknown => "unknown",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            MarketRegime::Trending => "趋势",
            MarketRegime::Choppy => "震荡",
            MarketRegime::Declining => "下行",
            MarketRegime::Unknown => "未知",
        }
    }
}

/// Slow moving average, in trading days. Also the minimum history required
/// before a regime is reported at all.
pub const REGIME_SLOW_DAYS: usize = 60;
/// Fast moving average, in trading days.
pub const REGIME_FAST_DAYS: usize = 20;
/// How far the fast average must sit above the slow one to count as trending,
/// in bps of the slow average.
///
/// Below roughly 50 bps the 20-day average crosses the 60-day constantly and
/// the label flips every few days, which would move the aggressive slot's
/// weight on noise alone. 50 bps is a 0.5% separation, which held for weeks
/// rather than days.
pub const TREND_SEPARATION_BPS: i64 = 50;
/// Same separation, but negative, marks a decline. Symmetric on purpose: an
/// asymmetric band would make "choppy" a residual bucket for an arbitrary
/// side of the distribution.
pub const DECLINE_SEPARATION_BPS: i64 = -50;
/// Share of names above their own 20-day average, in bps, needed to confirm a
/// trend. Under half is not participation.
///
/// Deliberately a veto on `trending` only. It is *not* a reason to call a
/// falling market choppy: after a sharp drop most names sit above their own
/// short average while the index is still far below its slow one, and measured
/// on the real feed that combination (separation -1063 bps, breadth 8105 bps)
/// showed up on 2026-08-14. Reading breadth as "participation" there would
/// have relabelled a clear decline as a range and switched the aggressive slot
/// back on in the middle of it.
pub const TREND_BREADTH_BPS: i64 = 5_000;

/// Everything the classifier looked at, kept so the console can explain the
/// verdict instead of just asserting it.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct RegimeView {
    pub regime: MarketRegime,
    pub index_points: usize,
    pub fast_average_cents: Option<i64>,
    pub slow_average_cents: Option<i64>,
    /// Fast average above slow, in bps of the slow average.
    pub separation_bps: Option<i64>,
    /// Names above their own 20-day average, in bps of the names measured.
    pub breadth_bps: Option<i64>,
}

impl RegimeView {
    /// A verdict of `unknown` with no measurements behind it.
    pub fn unknown(points: usize) -> Self {
        Self {
            regime: MarketRegime::Unknown,
            index_points: points,
            fast_average_cents: None,
            slow_average_cents: None,
            separation_bps: None,
            breadth_bps: None,
        }
    }
}

/// Classify the market as of the last bar in `index_closes`.
///
/// `index_closes` is a synthetic equal-weight index: the mean close of every
/// eligible symbol on each day, oldest first. An equal-weight index is used
/// rather than a real cap-weighted one because the market feed is a whole
/// market snapshot with no free-float data, and because equal weighting is
/// what the strategy actually trades -- it picks among the whole universe
/// rather than among large caps.
///
/// `advancing_share` is the fraction of names above their own 20-day average
/// on the same day, in bps. `None` when too few names had enough history.
pub fn classify(index_closes: &[i64], advancing_share: Option<i64>) -> RegimeView {
    if index_closes.len() < REGIME_SLOW_DAYS {
        return RegimeView::unknown(index_closes.len());
    }
    let fast = mean(&index_closes[index_closes.len() - REGIME_FAST_DAYS..]);
    let slow = mean(&index_closes[index_closes.len() - REGIME_SLOW_DAYS..]);
    if slow <= 0 {
        return RegimeView::unknown(index_closes.len());
    }
    let separation_bps = (fast - slow) * 10_000 / slow;
    let breadth_bps = advancing_share;

    // Order matters. A decline is decided on the index alone, so a short-term
    // bounce inside a downtrend cannot reclassify the market. Breadth is only
    // consulted to confirm or deny a trend.
    let regime = if separation_bps <= DECLINE_SEPARATION_BPS {
        MarketRegime::Declining
    } else if separation_bps >= TREND_SEPARATION_BPS
        && breadth_bps.map_or(true, |breadth| breadth >= TREND_BREADTH_BPS)
    {
        MarketRegime::Trending
    } else {
        MarketRegime::Choppy
    };

    RegimeView {
        regime,
        index_points: index_closes.len(),
        fast_average_cents: Some(fast),
        slow_average_cents: Some(slow),
        separation_bps: Some(separation_bps),
        breadth_bps,
    }
}

/// Build the equal-weight index and breadth series from per-symbol closes.
///
/// `series` is one entry per symbol: its closes up to the settled day, oldest
/// first. Each day's index value is the mean of that day's closes. Building the
/// index from the same snapshot the strategies see is deliberate -- a regime
/// computed from a different, possibly later, data source would leak
/// information into the decision.
///
/// Short series are right-aligned: the newest bar of every symbol is the same
/// calendar day, so trailing bars are matched first and a symbol only drops
/// out once the window reaches back past its history.
pub fn equal_weight_index(series: &[Vec<i64>]) -> Vec<i64> {
    let longest = series.iter().map(Vec::len).max().unwrap_or(0);
    let mut index = Vec::with_capacity(longest);
    for back in (0..longest).rev() {
        let mut sum: i128 = 0;
        let mut count: i64 = 0;
        for closes in series {
            if closes.len() <= back {
                continue;
            }
            let close = closes[closes.len() - 1 - back];
            if close > 0 {
                sum += close as i128;
                count += 1;
            }
        }
        if count == 0 {
            continue;
        }
        index.push((sum / count as i128) as i64);
    }
    index
}

/// Share of symbols whose last close is above their own 20-day average, in bps.
///
/// Breadth is the second opinion on the index: an index can be held up by a
/// handful of heavy names while most of the market falls, and that is exactly
/// the tape where a fresh-breakout rule gets nothing but failures.
pub fn advancing_share_bps(series: &[Vec<i64>]) -> Option<i64> {
    let mut measured: i64 = 0;
    let mut advancing: i64 = 0;
    for closes in series {
        if closes.len() < REGIME_FAST_DAYS {
            continue;
        }
        let average = mean(&closes[closes.len() - REGIME_FAST_DAYS..]);
        if average <= 0 {
            continue;
        }
        measured += 1;
        if *closes.last().expect("checked length") > average {
            advancing += 1;
        }
    }
    (measured > 0).then(|| advancing * 10_000 / measured)
}

fn mean(values: &[i64]) -> i64 {
    if values.is_empty() {
        return 0;
    }
    values.iter().copied().sum::<i64>() / values.len() as i64
}

/// Whether a symbol may feed the index.
///
/// Only ordinary A-share codes qualify. Funds, bonds and indices are excluded
/// because the market feed carries them but the strategies cannot trade them;
/// including 22k bonds would make the "market" mostly a bond market.
pub fn index_eligible_symbol(symbol: &str) -> bool {
    let Some((code, venue)) = symbol.split_once('.') else {
        return false;
    };
    if code.len() != 6 || !code.bytes().all(|byte| byte.is_ascii_digit()) {
        return false;
    }
    match venue {
        "SH" => code.starts_with('6'),
        "SZ" => code.starts_with("00") || code.starts_with("30"),
        "BJ" => code.starts_with('4') || code.starts_with('8'),
        _ => false,
    }
}

/// Per-symbol close history from flat bars, deduplicated and ordered by day.
pub fn closes_by_symbol(
    bars: &[Bar],
    through_day: &str,
) -> std::collections::BTreeMap<String, Vec<i64>> {
    let mut by_symbol: std::collections::BTreeMap<String, Vec<i64>> =
        std::collections::BTreeMap::new();
    for bar in bars {
        if bar.day.as_str() > through_day || bar.close_cents <= 0 || !bar.tradable {
            continue;
        }
        by_symbol
            .entry(bar.symbol.clone())
            .or_default()
            .push(bar.close_cents);
    }
    by_symbol
}

/// Classify from per-symbol close history in one step.
pub fn classify_series(by_symbol: &std::collections::BTreeMap<String, Vec<i64>>) -> RegimeView {
    let series: Vec<Vec<i64>> = by_symbol
        .iter()
        .filter(|(symbol, _)| index_eligible_symbol(symbol))
        .map(|(_, closes)| closes.clone())
        .collect();
    if series.is_empty() {
        return RegimeView::unknown(0);
    }
    let index = equal_weight_index(&series);
    classify(&index, advancing_share_bps(&series))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A steady ramp: `step` bps per day off `start`, `days` long.
    fn ramp(start: i64, step_bps: i64, days: usize) -> Vec<i64> {
        let mut value = start;
        let mut out = Vec::with_capacity(days);
        for _ in 0..days {
            out.push(value);
            value += value * step_bps / 10_000;
        }
        out
    }

    #[test]
    fn too_little_history_is_unknown_not_choppy() {
        let view = classify(&ramp(1000, 20, REGIME_SLOW_DAYS - 1), Some(9_000));
        assert_eq!(view.regime, MarketRegime::Unknown);
        assert_eq!(view.separation_bps, None);
    }

    #[test]
    fn a_rising_index_with_breadth_is_trending() {
        // +20 bps a day for 90 days separates the 20-day mean well past 50 bps.
        let view = classify(&ramp(1000, 20, 90), Some(6_500));
        assert_eq!(view.regime, MarketRegime::Trending);
        assert!(view.separation_bps.unwrap() >= TREND_SEPARATION_BPS);
    }

    #[test]
    fn a_flat_index_is_choppy() {
        let flat = vec![1000i64; 90];
        let view = classify(&flat, Some(5_000));
        assert_eq!(view.regime, MarketRegime::Choppy);
        assert_eq!(view.separation_bps, Some(0));
    }

    #[test]
    fn a_falling_index_is_declining() {
        let view = classify(&ramp(1000, -20, 90), Some(2_000));
        assert_eq!(view.regime, MarketRegime::Declining);
    }

    #[test]
    fn a_rising_index_without_breadth_is_not_trending() {
        // Index up, but only 40% of names participating: exactly the tape
        // where a handful of heavy names mask a broad decline.
        let view = classify(&ramp(1000, 20, 90), Some(4_000));
        assert_eq!(view.regime, MarketRegime::Choppy);
    }

    #[test]
    fn breadth_is_optional_and_missing_breadth_does_not_fake_a_trend() {
        // No breadth reading at all: the separation is all we have, and it is
        // enough to say trending rather than silently defaulting to choppy.
        let view = classify(&ramp(1000, 20, 90), None);
        assert_eq!(view.regime, MarketRegime::Trending);
        assert_eq!(view.breadth_bps, None);
    }

    #[test]
    fn equal_weight_index_right_aligns_short_series() {
        // A two-day symbol must contribute to the last two days, not the first
        // two, because the newest bar of every symbol is the same day.
        let series = vec![vec![100, 100, 100, 100], vec![200, 200]];
        let index = equal_weight_index(&series);
        assert_eq!(index.len(), 4);
        // Days 0-1 have only the first symbol.
        assert_eq!(index[0], 100);
        assert_eq!(index[1], 100);
        // Days 2-3 average both.
        assert_eq!(index[2], 150);
        assert_eq!(index[3], 150);
    }

    #[test]
    fn advancing_share_counts_names_above_their_own_average() {
        // One clearly above, one clearly below, one too short to measure.
        let series = vec![
            ramp(1000, 100, 30),
            ramp(3000, -100, 30),
            vec![1000, 1000],
        ];
        let share = advancing_share_bps(&series).expect("two measurable names");
        assert_eq!(share, 5_000);
    }

    #[test]
    fn no_measurable_names_means_no_breadth() {
        let series = vec![vec![1000, 1000], vec![2000]];
        assert_eq!(advancing_share_bps(&series), None);
    }

    #[test]
    fn eligibility_rejects_non_equity_and_foreign_codes() {
        assert!(index_eligible_symbol("600000.SH"));
        assert!(index_eligible_symbol("000001.SZ"));
        assert!(index_eligible_symbol("300750.SZ"));
        assert!(index_eligible_symbol("430047.BJ"));
        // A fund, a bond, and an index must not move the "market".
        assert!(!index_eligible_symbol("510300.SH"));
        assert!(!index_eligible_symbol("110038.SH"));
        assert!(!index_eligible_symbol("000001.SH"));
        assert!(!index_eligible_symbol("600000"));
        assert!(!index_eligible_symbol("ABCDEF.SH"));
    }

    #[test]
    fn a_bounce_inside_a_downtrend_is_still_a_decline() {
        // Measured 2026-08-14: the index sat 1063 bps below its slow average
        // while 81% of names were above their own 20-day average, because the
        // drop had just happened. Breadth must not be able to relabel that as
        // a range and switch the aggressive slot back on mid-decline.
        let mut index = ramp(1000, 25, 75);
        for _ in 0..15 {
            let last = *index.last().unwrap();
            index.push(last * 960 / 1000);
        }
        let view = classify(&index, Some(8_105));
        assert_eq!(view.regime, MarketRegime::Declining);
    }

    #[test]
    fn classify_series_ignores_ineligible_symbols() {
        // Only a bond, no equities: nothing to measure.
        let mut by_symbol = std::collections::BTreeMap::new();
        by_symbol.insert("110038.SH".to_string(), ramp(1000, 20, 90));
        assert_eq!(classify_series(&by_symbol).regime, MarketRegime::Unknown);
    }

    #[test]
    fn closes_by_symbol_ignores_valuation_only_halted_rows() {
        let bar = |close_cents, tradable| Bar {
            symbol: "600000.SH".into(),
            day: "2026-11-10".into(),
            close_cents,
            volume: 0,
            tradable,
        };
        let by_symbol = closes_by_symbol(
            &[
                bar(1_000, true),
                bar(1_010, true),
                bar(999_999, false),
            ],
            "2026-11-10",
        );
        assert_eq!(by_symbol["600000.SH"], vec![1_000, 1_010]);
    }
}
