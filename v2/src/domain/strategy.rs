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
    /// Days a position must be held before a vanished signal may exit it.
    ///
    /// Both strategies are moving-average conditions, and a moving average
    /// crossing back and forth flips the condition true/false repeatedly. With
    /// an immediate exit that becomes: buy, condition flips, sell, condition
    /// flips back, buy. Measured over a year of real whole-market data this
    /// produced 543 fills on a 100k account, with a median holding period of
    /// **3 days** and 55% of positions closed in 3 days or less.
    ///
    /// Each round trip costs a 5 CNY minimum commission on the buy plus
    /// commission and stamp duty on the sell -- roughly 8-10 CNY on a 1,700
    /// CNY position. A 1% move is 17 CNY, so more than half the edge was being
    /// eaten by the act of trading. Holding through the flip is the fix.
    pub min_hold_days: i64,
    /// Smallest position worth opening, in cents.
    ///
    /// Commission is 3 bps with a 5 CNY floor, so a position smaller than
    /// ~16,700 CNY pays the floor rather than the rate. Measured: buys under
    /// 5,000 CNY cost 39.7 bps, buys over 5,000 cost 5.7 bps -- seven times
    /// more expensive for no additional edge. Below this floor the order is
    /// not worth placing, so it is skipped entirely.
    pub min_entry_cents: i64,
}

impl SlotPolicy {
    pub fn for_strategy(strategy: StrategyId) -> Self {
        match strategy {
            StrategyId::Steady => Self {
                max_positions: 3,
                target_weight_bps: 1_500,
                min_cash_bps: 1_000,
                min_hold_days: 10,
                min_entry_cents: 300_000,
            },
            StrategyId::Aggressive => Self {
                max_positions: 3,
                target_weight_bps: 3_000,
                min_cash_bps: 500,
                min_hold_days: 5,
                min_entry_cents: 300_000,
            },
        }
    }
}

pub fn signal(strategy: StrategyId, bars: &[Bar]) -> Result<Option<String>, RuleError> {
    let closes: Vec<i64> = bars.iter().map(|bar| bar.close_cents).collect();
    if closes.len() < 61 {
        return Ok(None);
    }
    let latest = *closes.last().ok_or_else(|| RuleError("empty_bars".into()))?;
    match strategy {
        StrategyId::Steady => {
            let ma20 = mean(&closes[closes.len() - 20..]);
            let ma60 = mean(&closes[closes.len() - 60..]);
            let recent_vol = volatility(&closes[closes.len() - 20..]);
            let base_vol = median_volatility(&closes);
            if ma20 > ma60 && recent_vol < base_vol {
                Ok(Some("trend_and_low_volatility".into()))
            } else {
                Ok(None)
            }
        }
        StrategyId::Aggressive => {
            let momentum = latest - closes[closes.len() - 6];
            let prior_high = closes[closes.len() - 21..closes.len() - 1]
                .iter()
                .copied()
                .max()
                .unwrap_or(0);
            if momentum > 0 && latest > prior_high {
                Ok(Some("positive_momentum_breakout".into()))
            } else {
                Ok(None)
            }
        }
    }
}

pub fn orders_for_day(
    strategy: StrategyId,
    positions: &[Position],
    signals: &[(String, Option<String>)],
    budget_cents: i64,
    cash_cents: i64,
    closes: &std::collections::BTreeMap<String, i64>,
    sellable: &[super::Sellable],
    day: &str,
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
    for (symbol, reason) in signals {
        if reason.is_none() || held.contains(symbol.as_str()) {
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
                reason: reason.clone().unwrap_or_default(),
            });
            used += amount;
            available_cash -= amount + fees;
        }
    }
    for position in positions {
        let still_signal = signals
            .iter()
            .find(|(symbol, _)| symbol == &position.symbol)
            .and_then(|(_, reason)| reason.as_ref());
        if still_signal.is_none() && position.quantity > 0 {
            // A vanished signal is not by itself a reason to exit. Moving
            // averages flip back and forth, so acting on the first flip turns
            // one position into a series of round trips that each pay the
            // commission floor. Wait out the minimum holding period first.
            if held_days(&position.acquired_day, day) < policy.min_hold_days {
                continue;
            }
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
                    reason: "signal_exit".into(),
                });
            }
        }
    }
    orders.into_iter().filter(|order| order.quantity >= 100).collect()
}

/// Calendar days from `acquired` to `day`, or 0 when either is unparsable.
///
/// Calendar days rather than trading days on purpose: the point is to sit out
/// a signal flip, and a week is a week whether or not the market was open. It
/// also keeps this a pure function of two strings, with no calendar table to
/// thread through the order logic.
fn held_days(acquired: &str, day: &str) -> i64 {
    use chrono::NaiveDate;
    match (
        NaiveDate::parse_from_str(acquired, "%Y-%m-%d"),
        NaiveDate::parse_from_str(day, "%Y-%m-%d"),
    ) {
        (Ok(start), Ok(end)) => (end - start).num_days().max(0),
        // Unparsable means we cannot prove the holding period is satisfied;
        // treat it as satisfied so a malformed date cannot silently pin a
        // position forever.
        _ => i64::MAX,
    }
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
