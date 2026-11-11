//! Two deterministic close-price strategies: steady and aggressive.

use serde::{Deserialize, Serialize};

use super::{Order, Position, RuleError};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StrategyId {
    Steady,
    Aggressive,
}

impl StrategyId {
    pub fn name(self) -> &'static str {
        match self {
            StrategyId::Steady => "稳健",
            StrategyId::Aggressive => "激进",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            StrategyId::Steady => "趋势确认 + 低波动过滤，单票 15%，现金底线 10%",
            StrategyId::Aggressive => "5 日动量为正 + 突破 20 日高点，单票 30%，现金底线 5%",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Bar {
    pub symbol: String,
    pub day: String,
    pub close_cents: i64,
    pub volume: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotPolicy {
    pub max_positions: usize,
    pub target_weight_bps: i64,
    pub min_cash_bps: i64,
    /// Smallest position worth opening, in cents.
    ///
    /// Commission is 3 bps with a 5 CNY floor, so a position smaller than
    /// ~16,700 CNY pays the floor rather than the rate. Measured: buys under
    /// 5,000 CNY cost 39.7 bps, buys over 5,000 cost 5.7 bps -- seven times
    /// more expensive for no additional edge. Below this floor the order is
    /// not worth placing, so it is skipped entirely.
    pub min_entry_cents: i64,
    /// Hard stop, in basis points below the average cost.
    ///
    /// Fires regardless of the signal score. Sitting out a wobble only makes
    /// sense while the thesis is roughly intact; once the price has fallen
    /// past this, waiting just deepens the loss. Measured: any exit rule
    /// without a stop let the aggressive slot run to -74%.
    pub stop_loss_bps: i64,
}

impl SlotPolicy {
    pub fn for_strategy(strategy: StrategyId) -> Self {
        match strategy {
            StrategyId::Steady => Self {
                max_positions: 3,
                target_weight_bps: 1_500,
                min_cash_bps: 1_000,
                min_entry_cents: 300_000,
                stop_loss_bps: 1_200,
            },
            StrategyId::Aggressive => Self {
                max_positions: 3,
                target_weight_bps: 3_000,
                min_cash_bps: 500,
                min_entry_cents: 300_000,
                stop_loss_bps: 700,
            },
        }
    }
}

/// How strongly a strategy wants to be in a position today.
///
/// Ranges roughly -100..+100. Positive means "hold or open", negative means
/// "get out". `orders_for_day` applies a hysteresis band to this rather than
/// acting on a boolean, so a condition hovering around its threshold does not
/// flip the position on and off.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
pub struct SignalScore {
    pub score: i64,
    pub reason: &'static str,
}

impl SignalScore {
    fn flat() -> Self {
        Self {
            score: 0,
            reason: "no_edge",
        }
    }
}

/// Score below which an open position is closed, and below which a new one is
/// not opened.
///
/// The gap between this and `ENTRY_SCORE` is the hysteresis band. A boolean
/// condition sitting on its threshold oscillates; measuring a year of real
/// data, the aggressive slot turned over 543 times with a median holding
/// period of 3 days. Requiring a meaningfully positive score to enter, and a
/// meaningfully negative one to exit, keeps the position still while the
/// condition wobbles.
pub const ENTRY_SCORE: i64 = 40;
pub const EXIT_SCORE: i64 = -20;

/// Break past the prior 20-day high that still counts as fresh, in bps.
///
/// Measured: the lowest quartile of breakout size (2-68 bps) went on to make
/// +1.80% over 5 days; every larger bucket lost money. 100 bps (1%) is the top
/// of the profitable band with a little room.
pub const FRESH_BREAKOUT_BPS: i64 = 100;

/// 5-day momentum above which the move is treated as a spike, in bps.
/// Measured: the lowest quartile (under ~3.4%) made +0.55%, the highest
/// (over ~10%) lost 3.22%.
pub const MILD_MOMENTUM_BPS: i64 = 340;

/// 20-day run-up off the low above which the stock is "already gone", in bps.
/// Measured: the lowest quartile (under ~9.3%) made +0.97%, the highest
/// (over ~21.8%) lost 3.32%.
pub const MILD_RUN_UP_BPS: i64 = 930;

/// Score a strategy's view of one symbol today.
///
/// The score is a sum of signed contributions rather than a pass/fail test, so
/// that (a) the exit decision can differ from the entry decision, and (b) an
/// eventual AI overlay has a real number to argue with instead of a boolean.
pub fn score_signal(strategy: StrategyId, bars: &[Bar]) -> SignalScore {
    let closes: Vec<i64> = bars.iter().map(|bar| bar.close_cents).collect();
    if closes.len() < 61 {
        return SignalScore::flat();
    }
    let latest = *closes.last().expect("non-empty");
    match strategy {
        StrategyId::Steady => steady_score(&closes),
        StrategyId::Aggressive => aggressive_score(&closes, latest),
    }
}

/// Trend above a rising average, and calm enough to be worth holding.
fn steady_score(closes: &[i64]) -> SignalScore {
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
fn aggressive_score(closes: &[i64], latest: i64) -> SignalScore {
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

/// Back-compat shim: `Some` when the score clears the entry threshold.
pub fn signal(strategy: StrategyId, bars: &[Bar]) -> Result<Option<String>, RuleError> {
    let scored = score_signal(strategy, bars);
    Ok((scored.score >= ENTRY_SCORE).then(|| scored.reason.to_string()))
}

pub fn orders_for_day(
    strategy: StrategyId,
    positions: &[Position],
    signals: &[(String, SignalScore)],
    budget_cents: i64,
    cash_cents: i64,
    closes: &std::collections::BTreeMap<String, i64>,
    sellable: &[super::Sellable],
) -> Vec<Order> {
    let policy = SlotPolicy::for_strategy(strategy);
    let mut orders = Vec::new();
    let held: std::collections::BTreeSet<&str> =
        positions.iter().map(|item| item.symbol.as_str()).collect();
    let target = budget_cents * policy.target_weight_bps / 10_000;
    let mut used: i64 = positions
        .iter()
        .filter_map(|item| closes.get(&item.symbol).map(|price| *price * item.quantity))
        .sum();
    let cash_floor = budget_cents * policy.min_cash_bps / 10_000;
    // The target is based on the slot's marked equity, but an order can only
    // spend cash the slot actually owns. Leave room for commission as well:
    // a buy of a board lot is not valid when `amount + fees` exceeds cash.
    let mut available_cash = (budget_cents - cash_floor - used)
        .min(cash_cents - cash_floor)
        .max(0);
    for (symbol, scored) in signals {
        // Enter only on a clearly positive score. The band between EXIT_SCORE
        // and ENTRY_SCORE is deliberately inert: a condition sitting on its
        // threshold would otherwise open and close the same position
        // repeatedly.
        if scored.score < ENTRY_SCORE || held.contains(symbol.as_str()) {
            continue;
        }
        let pending = orders
            .iter()
            .filter(|order: &&Order| order.side == "buy")
            .count();
        if positions.len() + pending >= policy.max_positions {
            continue;
        }
        let Some(price) = closes.get(symbol) else {
            continue;
        };
        let room = (target - used).min(available_cash);
        let mut quantity = room / price / 100 * 100;
        while quantity >= 100 {
            let amount = quantity * price;
            let fees = super::fees_cents("buy", amount);
            if amount + fees <= available_cash {
                break;
            }
            quantity -= 100;
        }
        // Skip orders too small to be worth the commission floor. Placing them
        // costs 39.7 bps measured versus 5.7 bps for a normal size, with no
        // extra edge -- the position is simply less able to pay for itself.
        if quantity >= 100 && quantity * price >= policy.min_entry_cents {
            let amount = quantity * price;
            let fees = super::fees_cents("buy", amount);
            orders.push(Order {
                slot: strategy,
                symbol: symbol.clone(),
                side: "buy".into(),
                quantity,
                reason: scored.reason.to_string(),
            });
            used += amount;
            available_cash -= amount + fees;
        }
    }
    for position in positions {
        let still_scored = signals
            .iter()
            .find(|(symbol, _)| symbol == &position.symbol)
            .map(|(_, scored)| *scored);
        // A symbol can be absent from the signal list only when it had no bar
        // today. Distinguish that from "present with a weak score".
        let signal_present = signals.iter().any(|(symbol, _)| symbol == &position.symbol);
        if position.quantity > 0 {
            // Exit on the score falling through the exit band, not on the
            // signal merely turning false. This is what replaces a fixed
            // holding period: the position stays put while the score wobbles
            // in the neutral band, and leaves as soon as it genuinely turns.
            let mut exit_reason = match still_scored {
                Some(scored) if scored.score <= EXIT_SCORE => Some(scored.reason),
                // No score at all means no bar today, which is not a reason to
                // sell -- a suspended or delisted symbol simply cannot trade.
                None if !signal_present => None,
                None => Some("momentum_lost"),
                Some(_) => None,
            };

            // Hard stop is independent of the score, and fires immediately.
            // Holding through a wobble is reasonable while the thesis is
            // roughly intact; once the price has fallen past the stop, waiting
            // just deepens the loss. Measured: introducing a holding period
            // without a stop took the aggressive slot from -48% to -74%.
            let stop_hit = closes.get(&position.symbol).is_some_and(|price| {
                position.average_cost_cents > 0
                    && (*price - position.average_cost_cents) * 10_000
                        / position.average_cost_cents
                        <= -policy.stop_loss_bps
            });
            if stop_hit {
                exit_reason = Some("stop_loss");
            }

            if let Some(reason) = exit_reason {
                let can_sell = sellable
                    .iter()
                    .find(|item| item.symbol == position.symbol)
                    .map(|item| item.quantity)
                    .unwrap_or(0);
                if can_sell >= 100 {
                    orders.push(Order {
                        slot: strategy,
                        symbol: position.symbol.clone(),
                        side: "sell".into(),
                        quantity: can_sell,
                        reason: reason.into(),
                    });
                }
            }
        }
    }
    orders.into_iter().filter(|order| order.quantity >= 100).collect()
}

fn mean(values: &[i64]) -> i64 {
    values.iter().copied().sum::<i64>() / values.len().max(1) as i64
}

fn volatility(values: &[i64]) -> i64 {
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

fn median_volatility(values: &[i64]) -> i64 {
    let mut windows = Vec::new();
    for end in 20..=values.len() {
        windows.push(volatility(&values[end - 20..end]));
    }
    windows.sort_unstable();
    windows.get(windows.len() / 2).copied().unwrap_or(0)
}
