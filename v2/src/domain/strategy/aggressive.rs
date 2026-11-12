use super::{
    ENTRY_SCORE, EXIT_SCORE, FRESH_BREAKOUT_BPS, MILD_MOMENTUM_BPS, MILD_RUN_UP_BPS, SignalScore,
    Strategy,
};

pub(super) const ID: &str = "aggressive-v1";

pub(super) struct Aggressive;

/// A fresh break past the prior 20-day high, not an extended chase.
///
/// Weights are calibrated against 197 real entries, each bucketed by quartile
/// of the factor value and scored by the next 5 sessions' return:
///
///   5-day momentum   Q1 (+0.55%)  Q2 (-0.87%)  Q3 (-0.39%)  Q4 (-3.22%)
///   breakout size    Q1 (+1.80%)  Q2 (-1.27%)  Q3 (-2.43%)  Q4 (-2.05%)
///   20-day run-up    Q1 (+0.97%)  Q2 (-0.44%)  Q3 (-1.14%)  Q4 (-3.32%)
///
/// Every factor is monotonically *worse* as it gets larger. The only quarter
/// with a clearly positive expectancy is a small, fresh break. So the score
/// rewards "barely past the high" and punishes "already run", and the
/// penalties are much steeper than the rewards -- being late is the expensive
/// mistake, not being early.
impl Strategy for Aggressive {
    fn id(&self) -> &'static str {
        ID
    }

    fn score_closes(&self, closes: &[i64]) -> SignalScore {
        let latest = *closes.last().expect("history checked by score_registered");
        let momentum = latest - closes[closes.len() - 6];
        let prior_high = closes[closes.len() - 21..closes.len() - 1]
            .iter()
            .copied()
            .max()
            .unwrap_or(0);
        if prior_high <= 0 {
            return SignalScore::flat();
        }

        // Breakout size, in bps past the prior high. This is the dominant signal:
        // it separates +1.80% from -2% on its own.
        let breakout_bps = (latest - prior_high) * 10_000 / prior_high;
        let breakout_score = if breakout_bps < 0 {
            // Fell back below the prior high. Distance matters: a deep failure is
            // worse than a marginal one.
            (breakout_bps / 10).clamp(-70, 0)
        } else if breakout_bps == 0 {
            // Close exactly at the prior high: no break at all. Without this the
            // "smallest break is best" scale would hand a flat market full marks.
            0
        } else if breakout_bps <= FRESH_BREAKOUT_BPS {
            // A genuinely fresh break: the only positive-expectancy bucket.
            // Best right at the high, tapering as the break extends.
            60 - (breakout_bps * 30 / FRESH_BREAKOUT_BPS.max(1))
        } else {
            // Past the fresh band. Measured Q2-Q4 lost 1.3-2.4% over 5 days, so
            // this is a penalty, not a smaller bonus.
            -30 - ((breakout_bps - FRESH_BREAKOUT_BPS) / 8).clamp(0, 40)
        };

        // 5-day momentum. Q4 (over ~10%) lost 3.22%; Q1 (under ~3.4%) made 0.55%.
        // Reward mild momentum, punish a spike.
        let momentum_bps = momentum * 10_000 / prior_high;
        let momentum_score = if momentum_bps < 0 {
            (momentum_bps / 25).clamp(-40, 0)
        } else if momentum_bps <= MILD_MOMENTUM_BPS {
            (momentum_bps * 25 / MILD_MOMENTUM_BPS.max(1)).clamp(0, 25)
        } else {
            -((momentum_bps - MILD_MOMENTUM_BPS) / 40).clamp(0, 45)
        };

        // 20-day run-up off the low. Q1 (under ~9.3%) made 0.97%; Q4 (over ~21.8%)
        // lost 3.32%. Matters most as a veto on chasing.
        let recent_low = closes[closes.len() - 20..]
            .iter()
            .copied()
            .min()
            .unwrap_or(0);
        let run_up_score = if recent_low > 0 {
            let run_up_bps = (latest - recent_low) * 10_000 / recent_low;
            if run_up_bps <= MILD_RUN_UP_BPS {
                (run_up_bps * 20 / MILD_RUN_UP_BPS.max(1)).clamp(0, 20)
            } else {
                -((run_up_bps - MILD_RUN_UP_BPS) / 25).clamp(5, 50)
            }
        } else {
            0
        };

        let score = breakout_score + momentum_score + run_up_score;
        let reason = if score >= ENTRY_SCORE {
            "fresh_breakout"
        } else if score <= EXIT_SCORE {
            "momentum_lost"
        } else {
            "momentum_stalling"
        };
        SignalScore { score, reason }
    }
}
