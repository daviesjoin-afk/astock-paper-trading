use super::{ENTRY_SCORE, EXIT_SCORE, SignalScore, Strategy, mean, median_volatility, volatility};

pub(super) const ID: &str = "steady-v1";

pub(super) struct Steady;

/// Trend above a rising average, and calm enough to be worth holding.
impl Strategy for Steady {
    fn id(&self) -> &'static str {
        ID
    }

    fn score_closes(&self, closes: &[i64]) -> SignalScore {
        let ma20 = mean(&closes[closes.len() - 20..]);
        let ma60 = mean(&closes[closes.len() - 60..]);
        let recent_vol = volatility(&closes[closes.len() - 20..]);
        let base_vol = median_volatility(closes);

        // Trend: how far the 20-day average sits above the 60-day, in bps.
        let trend = if ma60 > 0 {
            (ma20 - ma60) * 10_000 / ma60
        } else {
            0
        };
        // Volatility: positive when recent volatility is BELOW the historical
        // median (calm), negative when it is above.
        let calm = if base_vol > 0 {
            (base_vol - recent_vol) * 10_000 / base_vol
        } else {
            0
        };

        // Trend contributes up to ±50, calm up to ±30. A stock in a firm uptrend
        // scores high even in choppy conditions; a stock that is merely quiet but
        // going nowhere does not.
        let mut score = 0;
        score += (trend / 20).clamp(-50, 50);
        score += (calm / 20).clamp(-30, 30);

        let reason = if score >= ENTRY_SCORE {
            "trend_and_low_volatility"
        } else if score <= EXIT_SCORE {
            "trend_broken"
        } else {
            "trend_flattening"
        };
        SignalScore { score, reason }
    }
}
