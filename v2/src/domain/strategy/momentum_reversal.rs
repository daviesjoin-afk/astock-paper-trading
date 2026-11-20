//! A library-only algorithm: 20-day momentum with a short-term reversal veto.
//!
//! It exists to prove the library can hold an algorithm that is *not* bound to
//! a book. The two funded slots keep their own algorithms; this one is scored
//! and reported, never traded. It uses only closes — no valuation, sector or
//! fundamental input is invented for it.

use super::{Strategy, SignalScore};

pub(super) const ID: &str = "momentum-reversal-v1";

pub(super) struct MomentumReversal;

impl Strategy for MomentumReversal {
    fn id(&self) -> &'static str {
        ID
    }

    fn required_history(&self) -> usize {
        // 20-day momentum needs 21 closes; the 5-day reversal needs 6.
        61
    }

    fn version(&self) -> &'static str {
        "v1"
    }

    fn score_closes(&self, closes: &[i64]) -> SignalScore {
        if closes.len() < 21 {
            return SignalScore {
                score: 0,
                reason: "no_edge",
                short_window: false,
            };
        }
        let latest = closes[closes.len() - 1];
        let month_ago = closes[closes.len() - 21];
        let week_ago = closes[closes.len() - 6];

        // Medium-term trend, in bps of the price 20 sessions back.
        let momentum_bps = if month_ago > 0 {
            (latest - month_ago) * 10_000 / month_ago
        } else {
            0
        };
        // A sharp five-day spike is treated as exhaustion, not strength: the
        // same "already gone" penalty the aggressive rule applies, expressed
        // on a different horizon.
        let week_bps = if week_ago > 0 {
            (latest - week_ago) * 10_000 / week_ago
        } else {
            0
        };

        let trend = (momentum_bps / 10).clamp(-50, 50);
        let exhaustion = if week_bps > 600 {
            -((week_bps - 600) / 20).clamp(0, 40)
        } else {
            0
        };
        let score = trend + exhaustion;
        let reason = if score >= super::ENTRY_SCORE {
            "steady_uptrend"
        } else if score <= super::EXIT_SCORE {
            "downtrend"
        } else {
            "no_trend"
        };
        SignalScore { score, reason, short_window: false }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ramp(start: i64, step: i64, count: usize) -> Vec<i64> {
        (0..count as i64).map(|i| start + i * step).collect()
    }

    #[test]
    fn a_steady_uptrend_scores_above_entry() {
        let score = MomentumReversal.score_closes(&ramp(1_000, 8, 61));
        assert!(score.score >= super::super::ENTRY_SCORE, "got {}", score.score);
        assert_eq!(score.reason, "steady_uptrend");
    }

    #[test]
    fn a_downtrend_scores_below_exit() {
        let score = MomentumReversal.score_closes(&ramp(2_000, -12, 61));
        assert!(score.score <= super::super::EXIT_SCORE, "got {}", score.score);
        assert_eq!(score.reason, "downtrend");
    }

    #[test]
    fn a_vertical_spike_is_penalised_not_rewarded() {
        // Flat for 55 days, then +3% a day for 5 days: momentum looks great,
        // but the recent move is exactly what the veto exists to refuse.
        let mut closes = ramp(1_000, 0, 56);
        let mut value = *closes.last().unwrap();
        for _ in 0..5 {
            value = value * 1_030 / 1_000;
            closes.push(value);
        }
        let spiked = MomentumReversal.score_closes(&closes);
        let steady = MomentumReversal.score_closes(&ramp(1_000, 8, 61));
        assert!(
            spiked.score < steady.score,
            "a spike must not outrank a steady trend: {} vs {}",
            spiked.score, steady.score
        );
    }

    #[test]
    fn short_history_is_flat_rather_than_guessed() {
        let score = MomentumReversal.score_closes(&[1_000, 1_010]);
        assert_eq!(score.score, 0);
        assert_eq!(score.reason, "no_edge");
    }
}
