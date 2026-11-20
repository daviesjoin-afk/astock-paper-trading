//! A small, typed factor expression language.
//!
//! Why not a scripting engine: a factor must be reproducible from a stored
//! definition, and it must be impossible for a definition to reach the filesystem,
//! the clock, the network or a future bar. An expression tree over a fixed
//! primitive set gives all three properties and makes "what does this factor
//! mean" answerable by reading one enum.
//!
//! The one rule that shapes everything here: **an expression may only look at
//! closes at or before its evaluation index**. `Shift` is bounded to the past
//! for that reason, and there is no operator that can reach forward.

use super::RuleError;

/// Longest window any operator may request.
///
/// A factor asking for 10,000 days would read history the caller did not load
/// and silently produce a different answer depending on how much was fetched.
/// Fixed so the requirement is declarative rather than incidental.
pub const MAX_WINDOW: usize = 250;

/// Nesting limit. Guards against a definition that is technically finite but
/// expands into an unbounded amount of work.
pub const MAX_DEPTH: usize = 12;

/// A factor definition: a tree of operations over one symbol's closes.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub enum Factor {
    /// Close at `index - offset`. Offset 0 is today.
    Close { offset: usize },
    /// Arithmetic mean of the last `window` closes.
    Mean { window: usize },
    /// Sample standard deviation of the last `window` closes.
    StdDev { window: usize },
    /// Highest close in the last `window` sessions.
    Max { window: usize },
    /// Lowest close in the last `window` sessions.
    Min { window: usize },
    /// Today's close minus the close `window` sessions ago, in basis points.
    Momentum { window: usize },
    /// Sum of the two operands.
    Add(Box<Factor>, Box<Factor>),
    /// Left minus right.
    Sub(Box<Factor>, Box<Factor>),
    /// Left divided by right, scaled to basis points. Division by zero is an
    /// error, never an infinity or a zero.
    Div(Box<Factor>, Box<Factor>),
    /// A session-shape factor from the latest bar, in basis points.
    ///
    /// These read the last bar's open/high/low/close, so they need a `Bar`
    /// rather than a close series; evaluating one against bare closes is
    /// `FactorError::NeedsSessionShape`. The four range-normalised variants
    /// (`Kmid2`, `Kup2`, `Klow2`, `Ksft2`) are `DivideByZero` on a session with
    /// no range -- a one-price seal -- rather than the ~1e12 artefact the
    /// reference implementation's `+1e-12` guard would produce.
    Kbar { field: KbarField },
    /// Constant, in the factor's own units (basis points for ratios).
    Const { value: i64 },
}

/// Which of the nine Alpha158 KBAR factors to read.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KbarField {
    /// (close - open) / open
    Kmid,
    /// (high - low) / open
    Klen,
    /// (close - open) / (high - low)
    Kmid2,
    /// (high - max(open, close)) / open
    Kup,
    /// (high - max(open, close)) / (high - low)
    Kup2,
    /// (min(open, close) - low) / open
    Klow,
    /// (min(open, close) - low) / (high - low)
    Klow2,
    /// (2*close - high - low) / open
    Ksft,
    /// (2*close - high - low) / (high - low)
    Ksft2,
}

impl KbarField {
    /// Every field, for enumerating the family.
    pub const ALL: [KbarField; 9] = [
        KbarField::Kmid,
        KbarField::Klen,
        KbarField::Kmid2,
        KbarField::Kup,
        KbarField::Kup2,
        KbarField::Klow,
        KbarField::Klow2,
        KbarField::Ksft,
        KbarField::Ksft2,
    ];

    /// Whether this field divides by the session range.
    pub fn needs_range(self) -> bool {
        matches!(
            self,
            KbarField::Kmid2 | KbarField::Kup2 | KbarField::Klow2 | KbarField::Ksft2
        )
    }
}

/// What a factor produced, or why it could not.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FactorError {
    /// Not enough closes to evaluate. Carries what was needed and available.
    InsufficientHistory { needed: usize, available: usize },
    /// Division by zero at this index.
    DivideByZero,
    /// Arithmetic left the i64 range.
    Overflow,
    /// A KBAR factor was evaluated against closes with no session shape.
    ///
    /// Zeroing it instead would make "no open/high/low in the input" look like
    /// "the session had no body", which are opposite conclusions.
    NeedsSessionShape,
}

impl Factor {
    /// Check the definition before it is ever evaluated against data.
    ///
    /// A rejected definition cannot enter a report, so a typo becomes a
    /// configuration error rather than a factor that scores everything as zero.
    pub fn validate(&self) -> Result<(), RuleError> {
        self.check(0)
    }

    fn check(&self, depth: usize) -> Result<(), RuleError> {
        if depth > MAX_DEPTH {
            return Err(RuleError("factor_too_deep".into()));
        }
        match self {
            Factor::Close { offset } => {
                if *offset >= MAX_WINDOW {
                    return Err(RuleError("factor_offset_out_of_range".into()));
                }
            }
            Factor::Mean { window }
            | Factor::StdDev { window }
            | Factor::Max { window }
            | Factor::Min { window } => {
                if *window == 0 || *window > MAX_WINDOW {
                    return Err(RuleError("factor_window_out_of_range".into()));
                }
            }
            Factor::Momentum { window } => {
                if *window == 0 || *window >= MAX_WINDOW {
                    return Err(RuleError("factor_window_out_of_range".into()));
                }
            }
            Factor::Add(left, right) | Factor::Sub(left, right) | Factor::Div(left, right) => {
                left.check(depth + 1)?;
                right.check(depth + 1)?;
            }
            Factor::Kbar { .. } => {}
            Factor::Const { .. } => {}
        }
        Ok(())
    }

    /// Closes this factor needs before it can be evaluated at the last index.
    ///
    /// Used to decide whether a symbol has enough history, and to keep the
    /// store's window in step with what the factor actually reads.
    pub fn required_history(&self) -> usize {
        match self {
            Factor::Close { offset } => offset + 1,
            Factor::Mean { window }
            | Factor::StdDev { window }
            | Factor::Max { window }
            | Factor::Min { window } => *window,
            Factor::Momentum { window } => window + 1,
            Factor::Add(left, right) | Factor::Sub(left, right) | Factor::Div(left, right) => {
                left.required_history().max(right.required_history())
            }
            // A KBAR reads only the latest bar.
            Factor::Kbar { .. } => 1,
            Factor::Const { .. } => 1,
        }
    }

    /// Evaluate at the final element of `closes`.
    ///
    /// `closes` is oldest-first and the evaluation point is always its last
    /// element: there is no way to ask this function about a future bar,
    /// which is the property the whole design exists to guarantee.
    pub fn evaluate(&self, closes: &[i64]) -> Result<i64, FactorError> {
        self.evaluate_with(closes, None)
    }

    /// Evaluate with the latest bar available, so KBAR fields can be read.
    ///
    /// `bars` is oldest-first and must correspond to `closes`; only its last
    /// element is read, which is the same no-future-access rule `evaluate`
    /// enforces. Passing `None` keeps every close-only factor working exactly
    /// as before and makes a KBAR factor fail loudly instead of silently
    /// returning a value computed from a close series that has no shape.
    pub fn evaluate_bars(&self, bars: &[super::Bar]) -> Result<i64, FactorError> {
        let closes: Vec<i64> = bars.iter().map(|bar| bar.close_cents).collect();
        self.evaluate_with(&closes, bars.last())
    }

    fn evaluate_with(
        &self,
        closes: &[i64],
        latest_bar: Option<&super::Bar>,
    ) -> Result<i64, FactorError> {
        let needed = self.required_history();
        if closes.len() < needed {
            return Err(FactorError::InsufficientHistory {
                needed,
                available: closes.len(),
            });
        }
        match self {
            Factor::Close { offset } => Ok(closes[closes.len() - 1 - offset]),
            Factor::Mean { window } => Ok(mean(&closes[closes.len() - window..])),
            Factor::StdDev { window } => Ok(std_dev(&closes[closes.len() - window..])),
            Factor::Max { window } => Ok(*closes[closes.len() - window..]
                .iter()
                .max()
                .unwrap_or(&0)),
            Factor::Min { window } => Ok(*closes[closes.len() - window..]
                .iter()
                .min()
                .unwrap_or(&0)),
            Factor::Momentum { window } => {
                let latest = closes[closes.len() - 1];
                let prior = closes[closes.len() - 1 - window];
                if prior <= 0 {
                    return Err(FactorError::DivideByZero);
                }
                (latest - prior)
                    .checked_mul(10_000)
                    .map(|value| value / prior)
                    .ok_or(FactorError::Overflow)
            }
            Factor::Add(left, right) => left
                .evaluate_with(closes, latest_bar)?
                .checked_add(right.evaluate_with(closes, latest_bar)?)
                .ok_or(FactorError::Overflow),
            Factor::Sub(left, right) => left
                .evaluate_with(closes, latest_bar)?
                .checked_sub(right.evaluate_with(closes, latest_bar)?)
                .ok_or(FactorError::Overflow),
            Factor::Div(left, right) => {
                let denominator = right.evaluate_with(closes, latest_bar)?;
                if denominator == 0 {
                    return Err(FactorError::DivideByZero);
                }
                left.evaluate_with(closes, latest_bar)?
                    .checked_mul(10_000)
                    .map(|value| value / denominator)
                    .ok_or(FactorError::Overflow)
            }
            Factor::Kbar { field } => {
                let bar = latest_bar.ok_or(FactorError::NeedsSessionShape)?;
                let shape = super::KBars::from_bar(bar).ok_or(FactorError::NeedsSessionShape)?;
                kbar_value(&shape, *field)
            }
            Factor::Const { value } => Ok(*value),
        }
    }
}

/// Map a computed `KBars` onto the requested field.
fn kbar_value(shape: &super::KBars, field: KbarField) -> Result<i64, FactorError> {
    let value = match field {
        KbarField::Kmid => Some(shape.kmid_bps),
        KbarField::Klen => Some(shape.klen_bps),
        KbarField::Kmid2 => shape.kmid2_bps,
        KbarField::Kup => Some(shape.kup_bps),
        KbarField::Kup2 => shape.kup2_bps,
        KbarField::Klow => Some(shape.klow_bps),
        KbarField::Klow2 => shape.klow2_bps,
        KbarField::Ksft => Some(shape.ksft_bps),
        KbarField::Ksft2 => shape.ksft2_bps,
    };
    value.ok_or(FactorError::DivideByZero)
}

fn mean(values: &[i64]) -> i64 {
    if values.is_empty() {
        return 0;
    }
    values.iter().copied().sum::<i64>() / values.len() as i64
}

fn std_dev(values: &[i64]) -> i64 {
    if values.len() < 2 {
        return 0;
    }
    let average = mean(values);
    let variance = values
        .iter()
        .map(|value| {
            let delta = (*value - average) as i128;
            delta * delta
        })
        .sum::<i128>()
        / values.len() as i128;
    (variance as f64).sqrt() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ramp(count: usize) -> Vec<i64> {
        (0..count as i64).map(|index| 1_000 + index * 10).collect()
    }

    #[test]
    fn a_definition_is_rejected_before_it_sees_data() {
        // Zero-length windows and absurd offsets are configuration errors: they
        // must not quietly become "score everything zero".
        assert_eq!(
            Factor::Mean { window: 0 }.validate().unwrap_err().0,
            "factor_window_out_of_range"
        );
        assert_eq!(
            Factor::Mean { window: MAX_WINDOW + 1 }.validate().unwrap_err().0,
            "factor_window_out_of_range"
        );
        assert_eq!(
            Factor::Close { offset: MAX_WINDOW }.validate().unwrap_err().0,
            "factor_offset_out_of_range"
        );
        assert!(Factor::Momentum { window: 5 }.validate().is_ok());

        // Depth is bounded so a deep tree cannot be used as a denial of service.
        let mut deep = Factor::Const { value: 1 };
        for _ in 0..MAX_DEPTH + 2 {
            deep = Factor::Add(Box::new(deep), Box::new(Factor::Const { value: 1 }));
        }
        assert_eq!(deep.validate().unwrap_err().0, "factor_too_deep");
    }

    #[test]
    fn a_factor_cannot_see_past_the_evaluation_point() {
        // Evaluate on a prefix: the answer must depend only on that prefix.
        let closes = ramp(40);
        let factor = Factor::Mean { window: 5 };
        let on_prefix = factor.evaluate(&closes[..10]).unwrap();
        // Pretending the future existed must not change the prefix answer.
        let with_future = factor.evaluate(&closes).unwrap();
        assert_ne!(on_prefix, with_future);
        // Mean of the last `window` elements of the prefix, i.e. indices 5..10.
        assert_eq!(on_prefix, mean(&closes[5..10]));
        // closes[i] = 1000 + i*10, so the prefix 5..10 is 1050..1090.
        assert_eq!(on_prefix, 1_070);
        // Appending future bars moves the evaluation point, so the answer
        // changes — but only because the last five closes changed, never
        // because the factor peeked ahead.
        let mut longer = closes[..10].to_vec();
        longer.extend([-999_999, 999_999]);
        // The new tail is (-999_999 + 999_999 + 1_090 + 1_080 + 1_070) / 5.
        assert_eq!(factor.evaluate(&longer).unwrap(), 648);
        // Re-evaluating a prefix that ends where the earlier one did still
        // gives the earlier answer, regardless of what followed.
        assert_eq!(factor.evaluate(&longer[..10]).unwrap(), on_prefix);
    }

    #[test]
    fn short_history_is_an_error_not_a_zero() {
        let closes = ramp(3);
        assert_eq!(
            Factor::Mean { window: 20 }.evaluate(&closes).unwrap_err(),
            FactorError::InsufficientHistory {
                needed: 20,
                available: 3
            }
        );
        assert_eq!(Factor::Mean { window: 20 }.required_history(), 20);
        assert_eq!(Factor::Momentum { window: 5 }.required_history(), 6);
        assert_eq!(Factor::Close { offset: 3 }.required_history(), 4);
        // A nested factor reports the deepest requirement, not the sum.
        let nested = Factor::Div(
            Box::new(Factor::Mean { window: 20 }),
            Box::new(Factor::Mean { window: 5 }),
        );
        assert_eq!(nested.required_history(), 20);
    }

    #[test]
    fn division_by_zero_is_an_error_everywhere() {
        let flat_zero = vec![0i64; 10];
        assert_eq!(
            Factor::Div(
                Box::new(Factor::Const { value: 100 }),
                Box::new(Factor::Close { offset: 0 }),
            )
            .evaluate(&flat_zero)
            .unwrap_err(),
            FactorError::DivideByZero
        );
        // A zero prior close cannot produce momentum: the percentage is undefined.
        assert_eq!(
            Factor::Momentum { window: 1 }.evaluate(&[0, 1_000]).unwrap_err(),
            FactorError::DivideByZero
        );
    }

    #[test]
    fn arithmetic_operators_produce_the_documented_units() {
        let closes = ramp(10);
        // Momentum is basis points: +10 on 1,090 is ~91.7 bps.
        let momentum = Factor::Momentum { window: 1 }.evaluate(&closes).unwrap();
        assert_eq!(momentum, (1_090 - 1_080) * 10_000 / 1_080);
        // Div also returns bps, so two ratios can be compared directly.
        let ratio = Factor::Div(
            Box::new(Factor::Const { value: 1_500 }),
            Box::new(Factor::Const { value: 1_000 }),
        )
        .evaluate(&closes)
        .unwrap();
        // 1500/1000 = 1.5, expressed in bps.
        assert_eq!(ratio, 15_000);
        let sum = Factor::Add(
            Box::new(Factor::Const { value: 7 }),
            Box::new(Factor::Const { value: -3 }),
        )
        .evaluate(&closes)
        .unwrap();
        assert_eq!(sum, 4);
    }

    #[test]
    fn overflow_is_reported_rather_than_wrapping() {
        let huge = vec![i64::MAX - 1, i64::MAX - 1];
        assert_eq!(
            Factor::Add(
                Box::new(Factor::Close { offset: 0 }),
                Box::new(Factor::Close { offset: 1 }),
            )
            .evaluate(&huge)
            .unwrap_err(),
            FactorError::Overflow
        );
    }

    // ---- KBAR fields as first-class factors -------------------------------

    fn shaped(open: i64, high: i64, low: i64, close: i64) -> super::super::Bar {
        super::super::Bar {
            symbol: "600000.SH".into(),
            day: "2026-11-10".into(),
            close_cents: close,
            volume: 10_000,
            open_cents: open,
            high_cents: high,
            low_cents: low,
            tradable: true,
        }
    }

    #[test]
    fn a_kbar_field_is_readable_through_the_expression_language() {
        let bars = vec![shaped(1_000, 1_100, 1_000, 1_100)];
        let kmid = Factor::Kbar { field: KbarField::Kmid };
        assert_eq!(kmid.evaluate_bars(&bars).unwrap(), 1_000);
        let klen = Factor::Kbar { field: KbarField::Klen };
        assert_eq!(klen.evaluate_bars(&bars).unwrap(), 1_000);
    }

    #[test]
    fn kbar_fields_compose_with_the_existing_operators() {
        // KUP2 = 0 on this bar, so KUP2 + 500 should be 500: the point is that
        // a KBAR is an ordinary Factor and not a special case in the tree.
        let bars = vec![shaped(1_000, 1_100, 1_000, 1_100)];
        let expr = Factor::Add(
            Box::new(Factor::Kbar { field: KbarField::Kup2 }),
            Box::new(Factor::Const { value: 500 }),
        );
        assert_eq!(expr.evaluate_bars(&bars).unwrap(), 500);
    }

    #[test]
    fn a_kbar_against_bare_closes_fails_instead_of_inventing_a_shape() {
        // This is the important one: without a bar the only honest answers are
        // "I cannot" or a fabricated zero. A fabricated zero would read as
        // "the body was flat", which is a claim about a session nobody saw.
        let closes = vec![1_000, 1_010];
        assert_eq!(
            Factor::Kbar { field: KbarField::Kmid }.evaluate(&closes).unwrap_err(),
            FactorError::NeedsSessionShape
        );
    }

    #[test]
    fn a_bar_without_open_high_low_is_not_a_zero_shape() {
        let mut bar = shaped(1_000, 1_100, 1_000, 1_100);
        bar.open_cents = 0;
        assert_eq!(
            Factor::Kbar { field: KbarField::Kmid }
                .evaluate_bars(&[bar])
                .unwrap_err(),
            FactorError::NeedsSessionShape
        );
    }

    #[test]
    fn a_range_normalised_kbar_on_a_sealed_session_is_a_division_error() {
        // One price all day: there is no range to divide by. Reporting zero
        // would say "the close sat exactly at the midpoint of a range" -- a
        // statement about a range that did not exist.
        let sealed = vec![shaped(1_100, 1_100, 1_100, 1_100)];
        assert_eq!(
            Factor::Kbar { field: KbarField::Ksft2 }
                .evaluate_bars(&sealed)
                .unwrap_err(),
            FactorError::DivideByZero
        );
        // A price-normalised field is still well defined.
        assert_eq!(
            Factor::Kbar { field: KbarField::Kmid }
                .evaluate_bars(&sealed)
                .unwrap(),
            0
        );
    }

    #[test]
    fn every_kbar_field_validates_and_needs_one_bar() {
        for field in KbarField::ALL {
            let factor = Factor::Kbar { field };
            factor.validate().expect("every declared field must validate");
            assert_eq!(factor.required_history(), 1);
        }
    }

    #[test]
    fn kbar_fields_round_trip_through_serde() {
        for field in KbarField::ALL {
            let factor = Factor::Kbar { field };
            let json = serde_json::to_string(&factor).unwrap();
            let back: Factor = serde_json::from_str(&json).unwrap();
            assert_eq!(back, factor);
        }
    }
}
