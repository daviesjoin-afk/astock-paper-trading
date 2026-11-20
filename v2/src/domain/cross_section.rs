//! Cross-sectional scoring: rank a symbol's factor against today's universe.
//!
//! Every factor in this library is computed per symbol, from that symbol's own
//! history, and compared against a fixed threshold (`ENTRY_SCORE = 40`). That
//! comparison is only meaningful if the scores are on a common scale across
//! symbols. They are not: a score is a sum of terms each scaled by a hand-picked
//! divisor, and nothing normalises the result by the distribution of scores on
//! the day.
//!
//! Qlib's Alpha158 handles this with `CSRankNorm` / `CSZScoreNorm` applied
//! `groupby(level="datetime")`: every feature is normalised *within one day's
//! cross-section* before it reaches the model, using a median/MAD robust
//! z-score. The effect is that "high" means high relative to what else was
//! available that day, not high against a constant that was never re-estimated.
//!
//! This module supplies the same idea in the form this codebase can use: a
//! rank-based percentile in bps, computed over the day's candidates. Rank rather
//! than z-score because the score distribution has hard clamps at ±50/±30, so
//! its tails are flat and a mean/std normalisation would be dominated by
//! whatever fraction of the universe sits pinned at a bound.
//!
//! **It is off by default.** Turning it on changes which names are bought on
//! every day, so it has to earn its place through the same paired A/B the rest
//! of the system uses, not by being obviously more principled.

use std::collections::BTreeMap;

/// A score placed against the day's universe.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CrossSection {
    /// Percentile of this symbol's raw score within the day, in bps: 0 is the
    /// worst candidate, 10_000 the best.
    pub percentile_bps: i64,
    /// How many symbols were compared.
    pub universe: usize,
}

/// Rank every candidate's raw score against the others on the same day.
///
/// Ties share the average rank, so a day where every symbol scores identically
/// gives every symbol 5_000 rather than an arbitrary ordering by symbol name.
/// A universe of one returns 5_000 for the same reason: there is nothing to be
/// better or worse than, and inventing a 0 or 10_000 would silently gate entry.
pub fn rank_scores(scores: &[(String, i64)]) -> BTreeMap<String, CrossSection> {
    let mut out = BTreeMap::new();
    if scores.is_empty() {
        return out;
    }
    let universe = scores.len();
    if universe == 1 {
        out.insert(
            scores[0].0.clone(),
            CrossSection {
                percentile_bps: 5_000,
                universe,
            },
        );
        return out;
    }
    let mut ordered: Vec<(&str, i64)> = scores.iter().map(|(s, v)| (s.as_str(), *v)).collect();
    ordered.sort_by(|left, right| left.1.cmp(&right.1).then_with(|| left.0.cmp(right.0)));

    // Average rank for equal scores, expressed in bps of (universe - 1) so the
    // extremes land on exactly 0 and 10_000.
    let mut index = 0;
    while index < ordered.len() {
        let mut end = index + 1;
        while end < ordered.len() && ordered[end].1 == ordered[index].1 {
            end += 1;
        }
        let first_rank = index;
        let last_rank = end - 1;
        let average_rank = (first_rank + last_rank) as i64;
        let percentile = average_rank * 10_000 / (2 * (universe as i64 - 1));
        for (symbol, _) in &ordered[index..end] {
            out.insert(
                symbol.to_string(),
                CrossSection {
                    percentile_bps: percentile,
                    universe,
                },
            );
        }
        index = end;
    }
    out
}

/// Map a percentile onto the entry/exit thresholds the scorer already uses.
///
/// The raw score keeps its sign and magnitude; only its position among peers
/// decides the action. `entry_percentile_bps` of 8_000 means "top 20% of
/// today's candidates".
pub fn gates_for(percentile_bps: i64, entry_percentile_bps: i64, exit_percentile_bps: i64) -> (bool, bool) {
    let enter = percentile_bps >= entry_percentile_bps;
    let exit = percentile_bps <= exit_percentile_bps;
    (enter, exit)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scores(pairs: &[(&str, i64)]) -> Vec<(String, i64)> {
        pairs.iter().map(|(s, v)| (s.to_string(), *v)).collect()
    }

    #[test]
    fn the_best_and_worst_land_on_the_ends() {
        let ranked = rank_scores(&scores(&[("a", 10), ("b", 50), ("c", 30)]));
        assert_eq!(ranked["a"].percentile_bps, 0);
        assert_eq!(ranked["c"].percentile_bps, 5_000);
        assert_eq!(ranked["b"].percentile_bps, 10_000);
        assert_eq!(ranked["b"].universe, 3);
    }

    #[test]
    fn ties_share_the_middle_instead_of_being_ordered_by_name() {
        let ranked = rank_scores(&scores(&[("a", 20), ("b", 20), ("c", 20)]));
        for symbol in ["a", "b", "c"] {
            assert_eq!(
                ranked[symbol].percentile_bps, 5_000,
                "an all-equal day must not hand anyone an edge"
            );
        }
    }

    #[test]
    fn a_tie_at_the_top_does_not_make_one_of_them_the_sole_winner() {
        let ranked = rank_scores(&scores(&[("a", 99), ("b", 99), ("c", 1)]));
        assert_eq!(ranked["c"].percentile_bps, 0);
        assert_eq!(ranked["a"].percentile_bps, ranked["b"].percentile_bps);
        assert!(ranked["a"].percentile_bps > 5_000);
    }

    #[test]
    fn a_single_candidate_is_neither_promoted_nor_rejected() {
        let ranked = rank_scores(&scores(&[("only", 40)]));
        assert_eq!(ranked["only"].percentile_bps, 5_000);
        assert_eq!(ranked["only"].universe, 1);
    }

    #[test]
    fn an_empty_universe_produces_nothing_rather_than_a_zero() {
        assert!(rank_scores(&[]).is_empty());
    }

    /// The point of the change: an identical absolute score means different
    /// things on a strong day and a weak day.
    ///
    /// The symbol carrying raw 40 is the best candidate on the weak day and
    /// only mid-pack on the strong one, so a fixed threshold of 40 either
    /// admits the whole weak day or rejects most of the strong one.
    #[test]
    fn the_same_raw_score_gates_differently_on_different_days() {
        let weak = rank_scores(&scores(&[("x", 40), ("b", 20), ("c", 10), ("d", 0), ("e", -5)]));
        let strong = rank_scores(&scores(&[("x", 40), ("b", 95), ("c", 80), ("d", 60), ("e", 55)]));
        assert_eq!(
            weak["x"].percentile_bps, 10_000,
            "raw 40 is the best thing available on a weak day"
        );
        assert_eq!(
            strong["x"].percentile_bps, 0,
            "the same raw 40 is the worst thing available on a strong day"
        );
        // A fixed threshold cannot tell these two days apart.
        assert_eq!(weak["x"].percentile_bps > 8_000, !(strong["x"].percentile_bps > 8_000));
    }

    #[test]
    fn gates_follow_the_percentile_not_the_raw_score() {
        assert_eq!(gates_for(9_000, 8_000, 2_000), (true, false));
        assert_eq!(gates_for(5_000, 8_000, 2_000), (false, false));
        assert_eq!(gates_for(1_000, 8_000, 2_000), (false, true));
    }
}
