//! The nine KBAR factors from Qlib's Alpha158, in integer basis points.
//!
//! Source: `microsoft/qlib`, `qlib/contrib/data/loader.py`,
//! `Alpha158DL.get_feature_config()`:
//!
//! ```text
//! KMID  = (close - open) / open
//! KLEN  = (high - low) / open
//! KMID2 = (close - open) / (high - low)
//! KUP   = (high - max(open, close)) / open
//! KUP2  = (high - max(open, close)) / (high - low)
//! KLOW  = (min(open, close) - low) / open
//! KLOW2 = (min(open, close) - low) / (high - low)
//! KSFT  = (2*close - high - low) / open
//! KSFT2 = (2*close - high - low) / (high - low)
//! ```
//!
//! They describe the shape of one session rather than a trend over many: where
//! the close sits inside the day's range, how much of the range the body covers,
//! and how much of the move the wicks gave back. The two existing slots score
//! only close-to-close moves, so a day that opened at the low and closed at the
//! high looks identical to one that spiked at the open and filled back — both
//! are "up N cents" to a moving average.
//!
//! ## Two deliberate departures from the reference implementation
//!
//! **Units.** Qlib returns floats. This returns basis points as `i64`, matching
//! every other factor in this codebase, so a stored score is reproducible from
//! integers and there is no float drift between a replay and a live run.
//!
//! **A zero range is `None`, not near-infinity.** Qlib guards every
//! range-normalised denominator with `+1e-12`, so a session where `high == low`
//! — a one-price limit-up or limit-down seal, or a halt with a single reference
//! price — yields a number around 1e12 instead of failing. That value is not a
//! factor, it is an artefact of the guard, and any downstream standardisation
//! would then be dominated by sealed sessions. Here those four fields are
//! `None`: a session that never traded through a price genuinely carries no
//! intraday shape information, and a caller has to decide what to do about it.
//!
//! ## Not wired into scoring
//!
//! Nothing calls this from `score_closes` yet. Adding nine factors to the live
//! score changes which names are bought on every day, which is exactly the kind
//! of change this project requires to clear a paired A/B against the current
//! rule before it is enabled. Until then this is a factor *library*: computable,
//! tested, and available to research and evaluation, with the live decision
//! path untouched.

use super::Bar;

/// Nine session-shape factors for one bar, all in basis points.
///
/// The four fields normalised by the session range are `Option`: they are `None`
/// when the session had no range at all.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct KBars {
    /// `(close - open) / open`: the body, signed.
    pub kmid_bps: i64,
    /// `(high - low) / open`: the whole range, scaled by price.
    pub klen_bps: i64,
    /// `(close - open) / (high - low)`: body as a share of the range.
    pub kmid2_bps: Option<i64>,
    /// `(high - max(open, close)) / open`: upper wick as a share of price.
    pub kup_bps: i64,
    /// `(high - max(open, close)) / (high - low)`: upper wick as a share of
    /// the range.
    pub kup2_bps: Option<i64>,
    /// `(min(open, close) - low) / open`: lower wick as a share of price.
    pub klow_bps: i64,
    /// `(min(open, close) - low) / (high - low)`.
    pub klow2_bps: Option<i64>,
    /// `(2*close - high - low) / open`: close relative to the range midpoint.
    pub ksft_bps: i64,
    /// `(2*close - high - low) / (high - low)`.
    pub ksft2_bps: Option<i64>,
}

impl KBars {
    /// Compute the nine factors, or `None` when the bar carries no shape.
    ///
    /// `None` means "this input did not carry open/high/low", which is a
    /// different situation from "the session had no range" — that is reported
    /// per field, so a caller can tell the two apart.
    pub fn from_bar(bar: &Bar) -> Option<Self> {
        if !bar.has_session_shape() {
            return None;
        }
        let open = bar.open_cents;
        let high = bar.high_cents;
        let low = bar.low_cents;
        let close = bar.close_cents;
        // A bar whose high is below its low is not a session, it is corrupt
        // input. Refusing is safer than producing nine plausible numbers.
        if high < low {
            return None;
        }
        let range = high.checked_sub(low)?;
        let body = close.checked_sub(open)?;
        let upper = high.checked_sub(open.max(close))?;
        let lower = open.min(close).checked_sub(low)?;
        let half = 2i128 * close as i128 - high as i128 - low as i128;

        let per_open = |numerator: i64| -> Option<i64> {
            scale_bps(numerator as i128, open as i128)
        };
        let per_range = |numerator: i64| -> Option<i64> {
            if range == 0 {
                // No intraday range: the reference implementation would guard
                // this to ~1e12, which is an artefact, not a factor.
                return None;
            }
            scale_bps(numerator as i128, range as i128)
        };

        Some(Self {
            kmid_bps: per_open(body)?,
            klen_bps: per_open(range)?,
            kmid2_bps: per_range(body),
            kup_bps: per_open(upper)?,
            kup2_bps: per_range(upper),
            klow_bps: per_open(lower)?,
            klow2_bps: per_range(lower),
            ksft_bps: scale_bps(half, open as i128)?,
            ksft2_bps: if range == 0 {
                None
            } else {
                scale_bps(half, range as i128)
            },
        })
    }

    /// Whether every range-normalised field was computable.
    ///
    /// False for a session that never moved through a range — a limit seal or
    /// a single-price halt. Callers that need a complete shape should check
    /// this rather than treating the `None`s as zero.
    pub fn has_full_shape(&self) -> bool {
        self.kmid2_bps.is_some()
            && self.kup2_bps.is_some()
            && self.klow2_bps.is_some()
            && self.ksft2_bps.is_some()
    }
}

/// `numerator / denominator * 10_000`, rounded half away from zero.
///
/// `i128` intermediates so a 20,000-cent price times a large numerator cannot
/// wrap. A zero denominator returns `None` rather than a sentinel.
fn scale_bps(numerator: i128, denominator: i128) -> Option<i64> {
    if denominator <= 0 {
        return None;
    }
    let scaled = numerator.checked_mul(10_000)?;
    let rounded = if scaled >= 0 {
        (scaled + denominator / 2) / denominator
    } else {
        (scaled - denominator / 2) / denominator
    };
    i64::try_from(rounded).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar(open: i64, high: i64, low: i64, close: i64) -> Bar {
        Bar {
            symbol: "600000.SH".into(),
            day: "2026-11-10".into(),
            close_cents: close,
            volume: 100_000,
            open_cents: open,
            high_cents: high,
            low_cents: low,
            tradable: true,
        }
    }

    /// Opened at the low, closed at the high: a full-range up day.
    fn full_body_up() -> Bar {
        bar(1_000, 1_100, 1_000, 1_100)
    }

    #[test]
    fn a_full_range_up_day_fills_every_shape_field_in_the_expected_direction() {
        let k = KBars::from_bar(&full_body_up()).unwrap();
        // (1100-1000)/1000 = +10%
        assert_eq!(k.kmid_bps, 1_000);
        // (1100-1000)/1000 = 10% of price
        assert_eq!(k.klen_bps, 1_000);
        // Body equals the whole range.
        assert_eq!(k.kmid2_bps, Some(10_000));
        // No upper wick: the high is the close.
        assert_eq!(k.kup_bps, 0);
        assert_eq!(k.kup2_bps, Some(0));
        // No lower wick: the low is the open.
        assert_eq!(k.klow_bps, 0);
        assert_eq!(k.klow2_bps, Some(0));
        // 2*1100-1100-1000 = 100 over open 1000 = +10%
        assert_eq!(k.ksft_bps, 1_000);
        assert_eq!(k.ksft2_bps, Some(10_000));
        assert!(k.has_full_shape());
    }

    #[test]
    fn a_hammer_and_a_shooting_star_are_told_apart_by_shape_alone() {
        // Same range (900..1100), same open (1050). One closes near the high,
        // the other near the low. Every close-to-close number is identical;
        // only the session shape separates them.
        let hammer = KBars::from_bar(&bar(1_050, 1_100, 900, 1_090)).unwrap();
        let star = KBars::from_bar(&bar(1_050, 1_100, 900, 910)).unwrap();
        // KSFT is the close relative to the range midpoint (1000), scaled by
        // open 1050: (2*1090-1100-900)/1050 = +1714 bps; the star mirrors it.
        assert_eq!(hammer.ksft_bps, 1_714, "close 1090 vs midpoint 1000");
        assert_eq!(star.ksft_bps, -1_714, "mirrored; rounding goes away from zero");
        assert_eq!(hammer.ksft_bps, -star.ksft_bps, "a symmetric close gives a symmetric score");
        assert!(hammer.ksft_bps > 0 && star.ksft_bps < 0);
        // The star's body is negative while the hammer's is positive.
        assert!(hammer.kmid_bps > 0);
        assert!(star.kmid_bps < 0);
        // Both still carry a complete shape.
        assert!(hammer.has_full_shape() && star.has_full_shape());
    }

    #[test]
    fn a_one_price_seal_reports_missing_range_instead_of_a_huge_number() {
        // Limit-up seal: everything at 1100. Qlib's +1e-12 guard would produce
        // ~1e12 for the four range-normalised fields; here they are absent.
        let k = KBars::from_bar(&bar(1_100, 1_100, 1_100, 1_100)).unwrap();
        assert_eq!(k.kmid_bps, 0);
        assert_eq!(k.klen_bps, 0);
        assert_eq!(k.kmid2_bps, None);
        assert_eq!(k.kup2_bps, None);
        assert_eq!(k.klow2_bps, None);
        assert_eq!(k.ksft2_bps, None);
        assert!(!k.has_full_shape());
    }

    #[test]
    fn a_bar_without_a_session_shape_produces_nothing() {
        let mut b = full_body_up();
        b.open_cents = 0;
        assert_eq!(KBars::from_bar(&b), None);
        let mut b = full_body_up();
        b.high_cents = 0;
        assert_eq!(KBars::from_bar(&b), None);
    }

    #[test]
    fn a_high_below_the_low_is_refused_rather_than_scored() {
        assert_eq!(KBars::from_bar(&bar(1_000, 900, 1_100, 1_000)), None);
    }

    #[test]
    fn a_flat_session_still_yields_the_price_scaled_fields() {
        // Open == close == 1000, ranged 990..1010: no body, but a real range.
        let k = KBars::from_bar(&bar(1_000, 1_010, 990, 1_000)).unwrap();
        assert_eq!(k.kmid_bps, 0);
        assert_eq!(k.kmid2_bps, Some(0));
        assert_eq!(k.klen_bps, 200);
        // Upper wick 1010-1000 = 10 -> 1% of price, half the range.
        assert_eq!(k.kup_bps, 100);
        assert_eq!(k.kup2_bps, Some(5_000));
        assert_eq!(k.klow_bps, 100);
        assert_eq!(k.klow2_bps, Some(5_000));
        assert!(k.has_full_shape());
    }

    #[test]
    fn a_down_day_is_signed_correctly() {
        // Open 1100, close 1000: -9.09%.
        let k = KBars::from_bar(&bar(1_100, 1_100, 1_000, 1_000)).unwrap();
        assert_eq!(k.kmid_bps, -909);
        assert_eq!(k.kmid2_bps, Some(-10_000));
        assert_eq!(k.kup_bps, 0);
        // Lower wick 1000-1000 = 0
        assert_eq!(k.klow_bps, 0);
    }

    #[test]
    fn large_prices_do_not_overflow() {
        // 20,000 CNY = 2,000,000 cents, the top of the A-share range.
        let k = KBars::from_bar(&bar(2_000_000, 2_100_000, 1_900_000, 2_050_000)).unwrap();
        assert_eq!(k.kmid_bps, 250);
        assert_eq!(k.klen_bps, 1_000);
        // (2_050_000 - 2_000_000) / (2_100_000 - 1_900_000) = 0.25
        assert_eq!(k.kmid2_bps, Some(2_500));
    }

    #[test]
    fn scaling_rounds_half_away_from_zero() {
        assert_eq!(scale_bps(1, 2), Some(5_000));
        assert_eq!(scale_bps(1, 3), Some(3_333));
        assert_eq!(scale_bps(-1, 2), Some(-5_000));
        assert_eq!(scale_bps(-1, 3), Some(-3_333));
        assert_eq!(scale_bps(1, 0), None);
    }
}
