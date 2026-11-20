//! Point-in-time price adjustment for factor inputs.
//!
//! Settlement fills at the unadjusted close: that is the price an order really
//! traded at, and rewriting it would book gains nobody could realise. Factors
//! are a different question. A 20/60-day moving average over unadjusted closes
//! treats an ex-rights gap as a real price move, so a stock that is trending up
//! scores as if its trend had broken the moment it splits.
//!
//! Measured on the two slots' actual scoring (trend weight is +-50, entry
//! threshold 40): a smooth uptrend scoring +3 drops to -50 after a 10-for-3
//! bonus issue, while a cash dividend of the size Moutai pays (-1.9% gap) costs
//! about 1 point. Cash dividends are noise here; bonus and transfer shares are
//! not. Both are handled the same way regardless, because the mechanism is the
//! same and guessing which one matters is how this went wrong.
//!
//! Forward adjustment (qfq) is deliberately *not* what this does. qfq rewrites
//! the whole series using the latest known factor, so yesterday's price changes
//! whenever a new dividend is announced: a backtest run in June and the same
//! backtest run in December would see different histories. This adjusts each
//! day by the factors published *on or before that day*, so a replay sees the
//! series as it was knowable at the time. That is the only form that is safe to
//! put in front of a walk-forward evaluation.
//!
//! Factors are expressed in basis points (10_000 = 1.0) to stay integer-exact.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::RuleError;

/// One distribution event, as published.
///
/// `ex_day` is the first session trading without the right. `cash_per_share_cents`
/// is the pre-tax cash dividend per share. `bonus_per_ten` and
/// `transfer_per_ten` are share counts per ten held, which is how the exchange
/// publishes them.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CorporateAction {
    pub ex_day: String,
    pub cash_per_share_cents: i64,
    /// Bonus shares per ten held (送股).
    pub bonus_per_ten: i64,
    /// Capitalisation shares per ten held (转增).
    pub transfer_per_ten: i64,
}

impl CorporateAction {
    /// Whether this event changes the share count and therefore the price.
    fn is_pure_cash(&self) -> bool {
        self.bonus_per_ten == 0 && self.transfer_per_ten == 0
    }

    /// Factor relative to the previous session, in bps.
    ///
    /// `prev_close_cents` is the last cum-rights close. For a distribution of
    /// `d` cash and `n` new shares per existing share, the theoretical ex-price
    /// is `(prev - d) / (1 + n)`, so the ratio old/new is
    /// `prev * (1 + n) / (prev - d)`.
    ///
    /// Returns `None` when the event cannot be evaluated honestly: a
    /// non-positive previous close, a distribution that would exhaust the
    /// price, or a share count that would invert it. A guessed factor is worse
    /// than no factor, because nothing downstream can tell the difference.
    pub fn factor_bps(&self, prev_close_cents: i64) -> Option<i64> {
        if prev_close_cents <= 0 {
            return None;
        }
        let new_shares_per_ten = self.bonus_per_ten.checked_add(self.transfer_per_ten)?;
        if new_shares_per_ten < 0 {
            return None;
        }
        // (1 + n) in thousandths of a share, so 10-for-10 gives 2000.
        let scale_thousandths: i64 = 1_000 + new_shares_per_ten * 100;
        // The cum-rights holder's claim per old share is
        //   prev  =  cash  +  (1 + n) * ex_price
        // so  ex_price = (prev - cash) / (1 + n)
        // and factor = prev / ex_price = prev * (1 + n) / (prev - cash).
        //
        // The share term multiplies the numerator; the cash term only ever
        // reduces the denominator. Putting both in the denominator (an earlier
        // version of this function did) makes a pure bonus issue come out as
        // exactly 1.0 -- the one case this module exists to handle.
        let net_of_cash = prev_close_cents.checked_sub(self.cash_per_share_cents)?;
        if net_of_cash <= 0 {
            // The dividend is larger than the price: not a distribution we can
            // describe with a ratio. Refuse rather than emit a fantasy factor.
            return None;
        }
        // `scale_thousandths` is already per-mille (1000 = 1.0), so the
        // divisor of 1000 there produces a plain ratio. Scaling the result to
        // bps is one more multiplication by 10_000 over 1000, i.e. by 10.
        let numerator = (prev_close_cents as i128)
            .checked_mul(scale_thousandths as i128)?
            .checked_mul(10)?;
        let denominator = net_of_cash as i128;
        let factor = numerator / denominator;
        if factor <= 0 || factor > 100_000_000 {
            // Above 10_000x is not a corporate action; it is a data error.
            return None;
        }
        i64::try_from(factor).ok()
    }

    fn validate(&self) -> Result<(), RuleError> {
        if self.ex_day.len() != 10 {
            return Err(RuleError(format!(
                "corporate_action_bad_ex_day:{}",
                self.ex_day
            )));
        }
        if self.cash_per_share_cents < 0
            || self.bonus_per_ten < 0
            || self.transfer_per_ten < 0
        {
            return Err(RuleError(format!(
                "corporate_action_negative:{}",
                self.ex_day
            )));
        }
        Ok(())
    }
}

/// Every known action for one symbol, plus the factor series derived from them.
///
/// Built once per symbol per advance, not per day: the factor on a given day is
/// a cumulative product, so recomputing it inside the scoring loop would be
/// O(days^2) for no gain.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AdjustmentSeries {
    /// Cumulative factor in bps, keyed by the day it starts applying.
    by_day: BTreeMap<String, i64>,
    /// Days whose action could not be priced honestly. Present so callers can
    /// report "this symbol's factor is incomplete" instead of silently using a
    /// series that is missing an event.
    unpriceable_days: Vec<String>,
}

impl AdjustmentSeries {
    /// A series that leaves every price untouched.
    pub fn identity() -> Self {
        Self::default()
    }

    /// Build from published actions and the closes needed to price them.
    ///
    /// `prev_close_for(day)` must return the last cum-rights close for the
    /// event's ex-day. Returning `None` marks that day unpriceable rather than
    /// assuming a factor of one: a missing previous close means the event
    /// cannot be evaluated, not that it had no effect.
    pub fn build(
        actions: &[CorporateAction],
        mut prev_close_for: impl FnMut(&str) -> Option<i64>,
    ) -> Result<Self, RuleError> {
        let mut ordered: Vec<&CorporateAction> = actions.iter().collect();
        ordered.sort_by(|left, right| left.ex_day.cmp(&right.ex_day));
        let mut series = Self::default();
        let mut running_bps: i64 = 10_000;
        let mut seen: Option<&str> = None;
        for action in ordered {
            action.validate()?;
            if seen == Some(action.ex_day.as_str()) {
                return Err(RuleError(format!(
                    "corporate_action_duplicate_day:{}",
                    action.ex_day
                )));
            }
            seen = Some(action.ex_day.as_str());
            let Some(prev_close) = prev_close_for(&action.ex_day) else {
                series.unpriceable_days.push(action.ex_day.clone());
                continue;
            };
            let Some(day_factor) = action.factor_bps(prev_close) else {
                series.unpriceable_days.push(action.ex_day.clone());
                continue;
            };
            // Factors compound: a rights issue after a bonus multiplies.
            running_bps = (running_bps as i128)
                .checked_mul(day_factor as i128)
                .and_then(|value| value.checked_div(10_000))
                .and_then(|value| i64::try_from(value).ok())
                .ok_or_else(|| RuleError("corporate_action_factor_overflow".into()))?;
            series.by_day.insert(action.ex_day.clone(), running_bps);
        }
        Ok(series)
    }

    /// Whether any event could not be priced.
    pub fn is_complete(&self) -> bool {
        self.unpriceable_days.is_empty()
    }

    pub fn unpriceable_days(&self) -> &[String] {
        &self.unpriceable_days
    }

    /// Cumulative factor in bps in force on `day`.
    ///
    /// 10_000 means "no adjustment". An event's factor applies *from* its ex-day
    /// onward, which is what makes this point-in-time: on the day before, the
    /// old factor is still the correct one.
    pub fn factor_on(&self, day: &str) -> i64 {
        self.by_day
            .range(..=day.to_string())
            .next_back()
            .map(|(_, factor)| *factor)
            .unwrap_or(10_000)
    }

    /// The adjusted close for a raw close on `day`, in cents.
    ///
    /// Rounding is half-up on the magnitude so a long series cannot drift by a
    /// cent per event in one direction. `None` when the multiplication would
    /// overflow; a wrapped price would be a fictional price.
    pub fn adjust_close(&self, raw_cents: i64, day: &str) -> Option<i64> {
        let factor = self.factor_on(day);
        if factor == 10_000 {
            return Some(raw_cents);
        }
        let scaled = (raw_cents as i128).checked_mul(factor as i128)?;
        let rounded = if scaled >= 0 {
            (scaled + 5_000) / 10_000
        } else {
            (scaled - 5_000) / 10_000
        };
        i64::try_from(rounded).ok()
    }

    /// Number of priced events. Used by callers to report coverage instead of
    /// claiming adjustment happened when no factor was found.
    pub fn events(&self) -> usize {
        self.by_day.len()
    }
}

/// Per-symbol adjustment series for one advance.
#[derive(Clone, Debug, Default)]
pub struct AdjustmentBook {
    by_symbol: BTreeMap<String, AdjustmentSeries>,
}

impl AdjustmentBook {
    pub fn new(by_symbol: BTreeMap<String, AdjustmentSeries>) -> Self {
        Self { by_symbol }
    }

    /// A book that adjusts nothing, for replays of data with no actions.
    pub fn identity() -> Self {
        Self::default()
    }

    /// The series for one symbol; absent means "no known actions", which is a
    /// factor of one and not an error.
    pub fn for_symbol(&self, symbol: &str) -> Option<&AdjustmentSeries> {
        self.by_symbol.get(symbol)
    }

    /// Symbols whose factor could not be fully priced. Callers should either
    /// exclude them from factor scoring or report the gap; silently scoring
    /// them on a partial series is the failure this type exists to prevent.
    pub fn incomplete_symbols(&self) -> Vec<&str> {
        self.by_symbol
            .iter()
            .filter(|(_, series)| !series.is_complete())
            .map(|(symbol, _)| symbol.as_str())
            .collect()
    }

    pub fn is_empty(&self) -> bool {
        self.by_symbol.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn action(day: &str, cash: i64, bonus: i64, transfer: i64) -> CorporateAction {
        CorporateAction {
            ex_day: day.into(),
            cash_per_share_cents: cash,
            bonus_per_ten: bonus,
            transfer_per_ten: transfer,
        }
    }

    #[test]
    fn cash_dividend_factor_matches_the_ex_price_ratio() {
        // 10 CNY on a 100 CNY stock: ex-price 90, factor = 100/90.
        let event = action("2026-06-26", 1_000, 0, 0);
        let factor = event.factor_bps(10_000).unwrap();
        assert_eq!(factor, 11_111);
    }

    #[test]
    fn ten_for_ten_doubles_the_share_count_and_halves_the_adjusted_price() {
        let event = action("2026-06-26", 0, 10, 0);
        // 10 bonus per 10 held = 1 new share per 1 held = factor 2.0.
        assert_eq!(event.factor_bps(10_000).unwrap(), 20_000);
        let series = AdjustmentSeries::build(&[event], |_| Some(10_000)).unwrap();
        assert_eq!(series.adjust_close(5_000, "2026-06-26"), Some(10_000));
        // The day before, the old price is still correct.
        assert_eq!(series.adjust_close(10_000, "2026-06-25"), Some(10_000));
    }

    #[test]
    fn ten_for_three_is_not_the_same_as_ten_for_ten() {
        let three = action("2026-06-26", 0, 3, 0).factor_bps(10_000).unwrap();
        let ten = action("2026-06-26", 0, 10, 0).factor_bps(10_000).unwrap();
        assert_eq!(three, 13_000);
        assert_eq!(ten, 20_000);
        assert_ne!(three, ten);
    }

    #[test]
    fn bonus_and_transfer_shares_compound_on_the_same_ten() {
        // 每10股送3转2 -> 5 new per 10 held -> factor 1.5.
        let event = action("2026-06-26", 0, 3, 2);
        assert_eq!(event.factor_bps(10_000).unwrap(), 15_000);
    }

    #[test]
    fn cash_and_shares_combine_the_way_the_exchange_formula_does() {
        // 1 CNY cash and 2 new per 10 held on a 20 CNY stock:
        // ex = (2000 - 100) / 1.2 = 1583.33
        let event = action("2026-06-26", 100, 2, 0);
        let factor = event.factor_bps(2_000).unwrap();
        // 2000 / 1583.33 = 1.2631...
        assert!((12_630..=12_632).contains(&factor), "got {factor}");
    }

    #[test]
    fn the_factor_is_point_in_time_not_forward_rewritten() {
        // Two actions; on the first ex-day only the first is in force. A qfq
        // series would already carry both.
        let first = action("2026-06-26", 0, 10, 0);
        let second = action("2027-06-26", 0, 10, 0);
        let series = AdjustmentSeries::build(&[first, second], |_| Some(10_000)).unwrap();
        assert_eq!(series.factor_on("2026-06-25"), 10_000);
        assert_eq!(series.factor_on("2026-06-26"), 20_000);
        assert_eq!(series.factor_on("2027-06-25"), 20_000);
        assert_eq!(series.factor_on("2027-06-26"), 40_000);
    }

    #[test]
    fn an_unpriceable_event_is_reported_not_assumed_away() {
        let event = action("2026-06-26", 0, 10, 0);
        let series = AdjustmentSeries::build(&[event.clone()], |_| None).unwrap();
        assert!(!series.is_complete());
        assert_eq!(series.unpriceable_days(), ["2026-06-26".to_string()]);
        // No factor means no adjustment: better an unadjusted close that is
        // labelled incomplete than a guessed multiplier.
        assert_eq!(series.factor_on("2026-06-26"), 10_000);
        assert_eq!(series.events(), 0);
    }

    #[test]
    fn a_dividend_larger_than_the_price_refuses_to_invent_a_factor() {
        let event = action("2026-06-26", 5_000, 0, 0);
        assert_eq!(event.factor_bps(1_000), None);
        let series = AdjustmentSeries::build(&[event], |_| Some(1_000)).unwrap();
        assert_eq!(series.events(), 0);
        assert!(!series.is_complete());
    }

    #[test]
    fn negative_or_malformed_actions_are_rejected() {
        assert!(action("2026-06-26", -1, 0, 0).validate().is_err());
        assert!(action("2026-06-26", 0, -1, 0).validate().is_err());
        assert!(action("2026-6-26", 0, 0, 0).validate().is_err());
        let duplicate = [
            action("2026-06-26", 0, 10, 0),
            action("2026-06-26", 0, 10, 0),
        ];
        assert!(AdjustmentSeries::build(&duplicate, |_| Some(10_000)).is_err());
    }

    #[test]
    fn pure_cash_is_recognised_separately_from_a_share_change() {
        assert!(action("d", 1_000, 0, 0).is_pure_cash());
        assert!(!action("d", 1_000, 1, 0).is_pure_cash());
        assert!(!action("d", 0, 0, 1).is_pure_cash());
    }

    #[test]
    fn the_book_lists_incomplete_symbols_instead_of_hiding_them() {
        let mut map = BTreeMap::new();
        map.insert(
            "600519.SH".to_string(),
            AdjustmentSeries::build(&[action("2026-06-26", 0, 10, 0)], |_| Some(10_000)).unwrap(),
        );
        map.insert(
            "000001.SZ".to_string(),
            AdjustmentSeries::build(&[action("2026-06-26", 0, 10, 0)], |_| None).unwrap(),
        );
        let book = AdjustmentBook::new(map);
        assert_eq!(book.incomplete_symbols(), vec!["000001.SZ"]);
        assert!(book.for_symbol("600519.SH").is_some());
        assert!(book.for_symbol("NOPE").is_none());
    }

    /// The defect this module exists to prevent, stated as a number.
    ///
    /// A smooth uptrend that is trending up, after a 10-for-10 bonus, must not
    /// look like a broken trend once the factor is applied.
    #[test]
    fn adjusting_keeps_a_rising_series_rising_through_a_bonus_issue() {
        // Smooth uptrend, then a 10-for-10 bonus on day 45.
        let mut raw: Vec<i64> = (0..61).map(|index| 1_000 + index * 20 / 60).collect();
        let split_at = 45;
        let cum_close = raw[split_at - 1];
        for price in raw.iter_mut().skip(split_at) {
            *price /= 2;
        }
        let series =
            AdjustmentSeries::build(&[action("2026-06-26", 0, 10, 0)], |_| Some(cum_close)).unwrap();
        let day_of = |index: usize| {
            if index < split_at {
                "2026-05-01"
            } else {
                "2026-06-26"
            }
        };
        let adjusted: Vec<i64> = raw
            .iter()
            .enumerate()
            .map(|(index, price)| series.adjust_close(*price, day_of(index)).unwrap())
            .collect();
        assert!(raw[split_at] < raw[split_at - 1], "raw must gap down");
        assert!(
            adjusted[split_at] >= adjusted[split_at - 1] - 1,
            "adjusted must not gap down: {} -> {}",
            adjusted[split_at - 1],
            adjusted[split_at]
        );
    }

    /// The measured harm, as the scoring code sees it.
    ///
    /// Trend score is clamped to +-50 and the entry threshold is 40, so a
    /// 10-for-3 bonus that turns a rising name into "-50" is not a rounding
    /// issue: it reads as a fully broken trend and drives an exit.
    #[test]
    fn a_bonus_issue_would_flip_the_trend_score_without_adjustment() {
        let mut raw: Vec<i64> = (0..61).map(|index| 1_000 + index * 20 / 60).collect();
        let split_at = 45;
        let cum_close = raw[split_at - 1];
        for price in raw.iter_mut().skip(split_at) {
            // 10-for-3: the ex-price is 10/13 of the cum price.
            *price = *price * 10 / 13;
        }
        let series =
            AdjustmentSeries::build(&[action("2026-06-26", 0, 3, 0)], |_| Some(cum_close)).unwrap();
        let day_of = |index: usize| {
            if index < split_at {
                "2026-05-01"
            } else {
                "2026-06-26"
            }
        };
        let adjusted: Vec<i64> = raw
            .iter()
            .enumerate()
            .map(|(index, price)| series.adjust_close(*price, day_of(index)).unwrap())
            .collect();

        let trend = |closes: &[i64]| {
            let mean = |window: &[i64]| window.iter().sum::<i64>() / window.len() as i64;
            let fast = mean(&closes[closes.len() - 20..]);
            let slow = mean(&closes[closes.len() - 60..]);
            (fast - slow) * 10_000 / slow.max(1)
        };
        let unadjusted_score = (trend(&raw) / 20).clamp(-50, 50);
        let adjusted_score = (trend(&adjusted) / 20).clamp(-50, 50);
        assert!(
            unadjusted_score <= -40,
            "the defect must actually show up as a broken trend, got {unadjusted_score}"
        );
        assert!(
            adjusted_score > unadjusted_score + 30,
            "adjustment must recover the trend: {unadjusted_score} -> {adjusted_score}"
        );
    }
}
