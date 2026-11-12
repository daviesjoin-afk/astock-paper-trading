use std::collections::BTreeMap;

use super::*;

fn signal(symbol: &str, score: i64) -> (String, SignalScore) {
    (symbol.into(), SignalScore { score, reason: "selection_fixture" })
}

fn book() -> SlotBook {
    SlotBook { slot: StrategyId::Steady, cash_cents: 5_000_000,
        positions: vec![], sellable: vec![] }
}

fn prices(symbols: &[&str]) -> BTreeMap<String, i64> {
    symbols.iter().map(|symbol| (symbol.to_string(), 1_000)).collect()
}

#[test]
fn highest_score_wins_the_last_position_regardless_of_stock_code() {
    let mut policy = SlotPolicy::for_strategy(StrategyId::Steady);
    policy.max_positions = 1;
    let closes = prices(&["000001.SZ", "600000.SH"]);
    let orders = orders_for_day_in(StrategyId::Steady, &policy, &[],
        &[signal("000001.SZ", 45), signal("600000.SH", 90)],
        5_000_000, 5_000_000, &closes, &closes, &[]);
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].symbol, "600000.SH");
}

#[test]
fn equal_scores_use_symbol_ties_and_ignore_input_permutations() {
    let mut policy = SlotPolicy::for_strategy(StrategyId::Steady);
    policy.max_positions = 2;
    let closes = prices(&["000001.SZ", "600000.SH", "300750.SZ"]);
    let signals = vec![signal("600000.SH", 80), signal("300750.SZ", 70),
        signal("000001.SZ", 80)];
    let first = orders_for_day_in(StrategyId::Steady, &policy, &[], &signals,
        5_000_000, 5_000_000, &closes, &closes, &[]);
    let reverse: Vec<_> = signals.into_iter().rev().collect();
    let second = orders_for_day_in(StrategyId::Steady, &policy, &[], &reverse,
        5_000_000, 5_000_000, &closes, &closes, &[]);
    assert_eq!(first, second);
    assert_eq!(first.iter().map(|order| order.symbol.as_str()).collect::<Vec<_>>(),
        ["000001.SZ", "600000.SH"]);
}

#[test]
fn an_existing_position_does_not_consume_other_symbols_targets() {
    let policy = SlotPolicy::for_strategy(StrategyId::Steady);
    let mut held = book();
    let fees = fees_cents("buy", 700_000);
    apply_buy(&mut held, "600000.SH", 700, 1_000, fees, "2026-11-10").unwrap();
    let closes = prices(&["600000.SH", "000001.SZ", "300750.SZ", "600001.SH"]);
    let orders = orders_for_day_in(StrategyId::Steady, &policy, &held.positions,
        &[signal("600000.SH", 100), signal("000001.SZ", 90),
          signal("300750.SZ", 80), signal("600001.SH", 70)],
        5_000_000, held.cash_cents, &closes, &closes, &held.sellable);
    assert_eq!(orders.len(), 2);
    assert_eq!(orders[0].symbol, "000001.SZ");
    assert_eq!(orders[1].symbol, "300750.SZ");
    for order in &orders {
        assert_eq!(order.side, "buy");
        assert_eq!(order.quantity, 700);
        assert!(order.quantity * closes[&order.symbol] <= 750_000);
    }
    assert_eq!(held.positions.len() + orders.len(), policy.max_positions);
}

#[test]
fn halted_holdings_still_occupy_the_total_budget_and_a_position() {
    let policy = SlotPolicy::for_strategy(StrategyId::Steady);
    let mut held = book();
    apply_buy(&mut held, "600000.SH", 1_000, 4_000,
        fees_cents("buy", 4_000_000), "2026-11-10").unwrap();
    let closes = prices(&["000001.SZ", "300750.SZ"]);
    let mut valuations = closes.clone();
    valuations.insert("600000.SH".into(), 4_000);
    let orders = orders_for_day_in(StrategyId::Steady, &policy, &held.positions,
        &[signal("000001.SZ", 90), signal("300750.SZ", 80),
          signal("600000.SH", -100)],
        5_000_000, held.cash_cents, &closes, &valuations, &held.sellable);
    assert_eq!(orders.len(), 1);
    assert_eq!((orders[0].symbol.as_str(), orders[0].side.as_str(), orders[0].quantity),
        ("000001.SZ", "buy", 400));
    let amount = orders[0].quantity * closes[&orders[0].symbol];
    let cost = amount + fees_cents("buy", amount);
    assert!(held.cash_cents - cost >= 500_000);
    assert!(4_000_000 + amount <= 5_000_000 - 500_000);
}

#[test]
fn pending_buys_respect_actual_cash_commission_floor_and_whole_lots() {
    let policy = SlotPolicy::for_strategy(StrategyId::Steady);
    let closes = prices(&["000001.SZ", "600000.SH", "300750.SZ"]);
    let orders = orders_for_day_in(StrategyId::Steady, &policy, &[],
        &[signal("000001.SZ", 90), signal("600000.SH", 80), signal("300750.SZ", 70)],
        5_000_000, 850_000, &closes, &closes, &[]);
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].quantity, 300);
    let total_cost: i64 = orders.iter().map(|order| {
        let amount = order.quantity * closes[&order.symbol];
        assert_eq!(order.quantity % 100, 0);
        assert!(amount <= 750_000);
        amount + fees_cents("buy", amount)
    }).sum();
    assert!(850_000 - total_cost >= 500_000);
}

#[test]
fn a_repeated_symbol_cannot_take_two_per_symbol_slots() {
    let mut policy = SlotPolicy::for_strategy(StrategyId::Steady);
    policy.max_positions = 3;
    let closes = prices(&["000001.SZ", "600000.SH"]);
    let orders = orders_for_day_in(StrategyId::Steady, &policy, &[],
        &[signal("000001.SZ", 90), signal("000001.SZ", 90), signal("600000.SH", 80)],
        5_000_000, 5_000_000, &closes, &closes, &[]);
    assert_eq!(orders.iter().filter(|order| order.symbol == "000001.SZ").count(), 1);
    assert_eq!(orders.len(), 2);
    let per_symbol: i64 = orders.iter().filter(|order| order.symbol == "000001.SZ")
        .map(|order| order.quantity * closes[&order.symbol]).sum();
    assert!(per_symbol <= 750_000, "one name exceeded its own cap: {per_symbol}");
}

#[test]
fn exits_do_not_pre_spend_sale_proceeds_or_free_a_position_early() {
    let mut policy = SlotPolicy::for_strategy(StrategyId::Steady);
    policy.max_positions = 1;
    let mut held = book();
    apply_buy(&mut held, "600000.SH", 700, 1_000,
        fees_cents("buy", 700_000), "2026-11-10").unwrap();
    settle_overnight(&mut held).unwrap();
    let closes = prices(&["600000.SH", "000001.SZ"]);
    // Independently exercise a full position limit with cash, and spare
    // position capacity with cash already at the floor. Neither may use the
    // pending sale to open the candidate before that sale has executed.
    for (max_positions, cash) in [(1, held.cash_cents), (3, 500_000)] {
        policy.max_positions = max_positions;
        let orders = orders_for_day_in(StrategyId::Steady, &policy, &held.positions,
            &[signal("000001.SZ", 90), signal("600000.SH", -100)],
            5_000_000, cash, &closes, &closes, &held.sellable);
        assert_eq!(orders.len(), 1, "position limit {max_positions}, cash {cash}");
        assert_eq!(orders[0].side, "sell");
        assert_eq!(orders[0].symbol, "600000.SH");
    }
}
