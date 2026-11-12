//! Two deterministic close-price strategies: steady and aggressive.

use serde::{Deserialize, Serialize};

use super::{MarketRegime, Order, Position, RuleError};

mod aggressive;
mod steady;

#[cfg(test)]
mod library_tests;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StrategyId {
    Steady,
    Aggressive,
}

impl StrategyId {
    /// The ledger slot stays fixed while its scoring algorithm has a library ID.
    pub fn algorithm_id(self) -> &'static str {
        match self {
            StrategyId::Steady => steady::ID,
            StrategyId::Aggressive => aggressive::ID,
        }
    }

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
    /// A real close. Reference prices for halted symbols never enter scoring.
    pub tradable: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotPolicy {
    pub max_positions: usize,
    pub target_weight_bps: i64,
    pub min_cash_bps: i64,
    /// Score needed to open a new position.
    ///
    /// Normally `ENTRY_SCORE`. A choppy market raises it, because the measured
    /// problem there is not the number of signals but their quality: the same
    /// rule that made +2% a month in a trend lost 3% a month in a range.
    pub entry_score: i64,
    /// Whether the slot may open anything at all today. A declining market
    /// turns this off; exits and stops keep running.
    pub allow_entries: bool,
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
    /// Baseline policy: what the slot does in a normal, tradeable tape.
    pub fn for_strategy(strategy: StrategyId) -> Self {
        match strategy {
            StrategyId::Steady => Self {
                max_positions: 3,
                target_weight_bps: 1_500,
                min_cash_bps: 1_000,
                min_entry_cents: 300_000,
                stop_loss_bps: 1_200,
                entry_score: ENTRY_SCORE,
                allow_entries: true,
            },
            StrategyId::Aggressive => Self {
                max_positions: 3,
                target_weight_bps: 3_000,
                min_cash_bps: 500,
                min_entry_cents: 300_000,
                stop_loss_bps: 700,
                entry_score: ENTRY_SCORE,
                allow_entries: true,
            },
        }
    }

    /// Policy for a slot given what the broad market is doing.
    ///
    /// The steady slot is deliberately left alone. Its score already demands a
    /// confirmed trend and falling volatility *per symbol*, which is the same
    /// test a regime filter would apply at the market level -- gating it again
    /// would double-count and simply stop it trading. The aggressive slot is a
    /// pure breakout rule with no such self-filter, which is the one that
    /// measured -51.5% through a range-bound year.
    pub fn for_strategy_in(strategy: StrategyId, regime: MarketRegime) -> Self {
        let mut policy = Self::for_strategy(strategy);
        if strategy == StrategyId::Steady {
            return policy;
        }
        match regime {
            // A trend is exactly the tape a breakout rule is built for.
            MarketRegime::Trending => {}
            // Nothing is more expensive than a breakout that fails. Demand a
            // much stronger score, take fewer names and carry less weight.
            MarketRegime::Choppy | MarketRegime::Unknown => {
                policy.entry_score = CHOPPY_ENTRY_SCORE;
                policy.target_weight_bps = 2_000;
                policy.max_positions = 2;
            }
            // No participation is the sum of "breakouts fail" and "losses
            // run", so stop opening. Existing positions are still managed by
            // the score and the stop, which is what gets them out.
            MarketRegime::Declining => {
                policy.allow_entries = false;
                policy.target_weight_bps = 1_500;
            }
        }
        policy
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

/// A deterministic close-price scoring algorithm, independent of a ledger slot.
///
/// Implementations receive at least 61 closes, oldest first, through
/// `score_registered`. Position sizing, market filters and exits remain owned
/// by `SlotPolicy` and `orders_for_day_in`.
pub trait Strategy: Sync {
    fn id(&self) -> &'static str;
    fn score_closes(&self, closes: &[i64]) -> SignalScore;
}

/// Adding an algorithm here makes it available for explicit scoring; it does
/// not bind another trading slot or enable it in the paper account.
pub static STRATEGY_LIBRARY: &[&dyn Strategy] = &[&steady::Steady, &aggressive::Aggressive];

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

/// Entry score required of the aggressive slot while the market is choppy.
///
/// Raised from 40 to 60, which on the measured score distribution roughly
/// halves the qualifying names. The intent is not "trade less" but "only take
/// the ones that would have qualified anyway": a breakout that also carries
/// mild momentum and a shallow run-up, rather than the marginal break that
/// only worked while the tape was going up.
pub const CHOPPY_ENTRY_SCORE: i64 = 60;

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
    score_registered(strategy.algorithm_id(), bars)
        .expect("fixed slot algorithm must be registered")
}

/// Score an explicitly selected library algorithm. This is the one history
/// boundary for all registered algorithms; longer histories are kept intact.
pub fn score_registered(algorithm_id: &str, bars: &[Bar]) -> Result<SignalScore, RuleError> {
    let algorithm = STRATEGY_LIBRARY
        .iter()
        .find(|algorithm| algorithm.id() == algorithm_id)
        .ok_or_else(|| RuleError(format!("unknown_strategy:{algorithm_id}")))?;
    let closes: Vec<i64> = bars
        .iter()
        .filter(|bar| bar.tradable)
        .map(|bar| bar.close_cents)
        .collect();
    if closes.len() < 61 {
        return Ok(SignalScore::flat());
    }
    Ok(algorithm.score_closes(&closes))
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
    valuations: &std::collections::BTreeMap<String, i64>,
    sellable: &[super::Sellable],
) -> Vec<Order> {
    orders_for_day_in(
        strategy,
        &SlotPolicy::for_strategy(strategy),
        positions,
        signals,
        budget_cents,
        cash_cents,
        closes,
        valuations,
        sellable,
    )
}

/// Same as `orders_for_day`, with the policy supplied by the caller.
///
/// The policy carries the market-regime decision (`allow_entries`,
/// `entry_score`) as well as the per-slot sizing rules, so the exit path and
/// the sizing path stay in one place instead of being re-derived at the call
/// site from a regime the caller happened to compute.
#[allow(clippy::too_many_arguments)]
pub fn orders_for_day_in(
    strategy: StrategyId,
    policy: &SlotPolicy,
    positions: &[Position],
    signals: &[(String, SignalScore)],
    budget_cents: i64,
    cash_cents: i64,
    closes: &std::collections::BTreeMap<String, i64>,
    valuations: &std::collections::BTreeMap<String, i64>,
    sellable: &[super::Sellable],
) -> Vec<Order> {
    let mut orders = Vec::new();
    let held: std::collections::BTreeSet<&str> =
        positions.iter().map(|item| item.symbol.as_str()).collect();
    let target = budget_cents * policy.target_weight_bps / 10_000;
    let used: i64 = positions
        .iter()
        .map(|item| {
            valuations
                .get(&item.symbol)
                .map(|price| price * item.quantity)
                .expect("every held position must have a valuation")
        })
        .sum();
    let cash_floor = budget_cents * policy.min_cash_bps / 10_000;
    // The target is based on the slot's marked equity, but an order can only
    // spend cash the slot actually owns. Leave room for commission as well:
    // a buy of a board lot is not valid when `amount + fees` exceeds cash.
    let mut available_cash = (budget_cents - cash_floor - used)
        .min(cash_cents - cash_floor)
        .max(0);
    // Spend limited cash and position slots on the strongest evidence first.
    // Symbol order breaks equal scores so caller iteration cannot change fills.
    let mut ranked: Vec<_> = signals.iter().collect();
    ranked.sort_by(|left, right| {
        right.1.score.cmp(&left.1.score).then_with(|| left.0.cmp(&right.0))
    });
    for (symbol, scored) in ranked {
        // Enter only on a clearly positive score. The band between EXIT_SCORE
        // and the entry threshold is deliberately inert: a condition sitting on
        // its threshold would otherwise open and close the same position
        // repeatedly. In a choppy market the entry threshold is raised, which
        // narrows the entries without touching the exit band -- so the slot
        // still leaves on the same evidence, it just stops adding.
        if !policy.allow_entries
            || scored.score < policy.entry_score
            || held.contains(symbol.as_str())
        {
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
        // The target is per symbol. Existing holdings already reduce the
        // shared cash budget above; they do not consume another symbol's cap.
        let room = target.min(available_cash);
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

            // A halt has a valuation mark but no fill. Do not emit a sell that
            // settlement would have to reject for want of a tradable close.
            if let Some(reason) = exit_reason.filter(|_| closes.contains_key(&position.symbol)) {
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
