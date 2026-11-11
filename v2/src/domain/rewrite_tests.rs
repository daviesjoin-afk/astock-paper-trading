use std::collections::BTreeMap;

use super::*;

fn bars(values: &[i64]) -> Vec<Bar> {
    values
        .iter()
        .enumerate()
        .map(|(index, close)| Bar {
            symbol: "600000.SH".into(),
            day: format!("2026-04-{:02}", index + 1),
            close_cents: *close,
            volume: 1,
        })
        .collect()
}

fn book() -> SlotBook {
    SlotBook {
        slot: StrategyId::Steady,
        cash_cents: 5_000_000,
        positions: Vec::new(),
        sellable: Vec::new(),
    }
}

#[test]
fn principal_is_split_exactly_fifty_fifty() {
    let split = initial_split(100_001).unwrap();
    assert_eq!(split[0].cash_cents + split[1].cash_cents, 100_001);
    assert_eq!(split[0].weight_bps, 5_000);
    assert_eq!(split[1].weight_bps, 5_000);
    assert_eq!(split[0].slot, StrategyId::Steady);
    assert_eq!(split[1].slot, StrategyId::Aggressive);
    assert!(initial_split(0).is_err());
}

#[test]
fn strategy_names_and_descriptions_are_the_new_pair() {
    assert_eq!(StrategyId::Steady.name(), "稳健");
    assert_eq!(StrategyId::Aggressive.name(), "激进");
    assert!(StrategyId::Steady.description().contains("低波动"));
    assert!(StrategyId::Aggressive.description().contains("突破"));
}

#[test]
fn steady_signal_requires_trend_and_low_volatility() {
    let mut values: Vec<i64> = (0..60).map(|index| 1_000 + index * 2).collect();
    values.extend((0..20).map(|index| 1_120 + index));
    assert!(signal(StrategyId::Steady, &bars(&values)).unwrap().is_some());
    let mut noisy = values.clone();
    for index in 0..values.len() {
        if index % 2 == 0 {
            noisy[index] += 300;
        }
    }
    assert!(signal(StrategyId::Steady, &bars(&noisy)).unwrap().is_none());
    let mut downtrend = values.clone();
    let len = downtrend.len();
    downtrend[len - 20..].reverse();
    for value in downtrend.iter_mut().skip(len - 20) {
        *value -= 400;
    }
    assert!(signal(StrategyId::Steady, &bars(&downtrend)).unwrap().is_none());
}

#[test]
fn aggressive_signal_requires_positive_momentum_and_breakout() {
    let mut values = vec![1_000i64; 61];
    assert!(signal(StrategyId::Aggressive, &bars(&values)).unwrap().is_none());
    values[55..60].copy_from_slice(&[1_010, 1_020, 1_030, 1_040, 1_050]);
    values[60] = 1_100;
    assert!(signal(StrategyId::Aggressive, &bars(&values)).unwrap().is_some());
    values[60] = 900;
    assert!(signal(StrategyId::Aggressive, &bars(&values)).unwrap().is_none());
}

#[test]
fn short_history_never_emits_a_signal() {
    for strategy in [StrategyId::Steady, StrategyId::Aggressive] {
        assert!(signal(strategy, &bars(&vec![1_000; 60])).unwrap().is_none());
    }
}

#[test]
fn a_buy_is_board_lots_charged_in_full_and_not_sellable_same_day() {
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    assert_eq!(book.positions[0].quantity, 100);
    assert_eq!(book.cash_cents, 5_000_000 - 100_000 - 5);
    assert!(book.sellable.is_empty());
    assert_eq!(
        apply_sell(&mut book, "600000.SH", 100, 1_100, 5).unwrap_err().0,
        "t_plus_one_violation"
    );
    assert_eq!(book.positions[0].quantity, 100);
}

#[test]
fn overnight_settlement_makes_only_existing_shares_sellable() {
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    settle_overnight(&mut book).unwrap();
    assert_eq!(book.sellable[0].quantity, 100);
    apply_buy(&mut book, "600000.SH", 100, 1_100, 5, "2026-11-02").unwrap();
    assert_eq!(book.positions[0].quantity, 200);
    assert_eq!(book.sellable[0].quantity, 100);
    apply_sell(&mut book, "600000.SH", 100, 1_200, 5).unwrap();
    assert_eq!(book.positions[0].quantity, 100);
    assert!(book.sellable.is_empty());
}

#[test]
fn sells_reject_odd_lots_missing_positions_and_overdrafts() {
    let mut book = book();
    assert_eq!(apply_sell(&mut book, "600000.SH", 100, 1_000, 5).unwrap_err().0, "t_plus_one_violation");
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    settle_overnight(&mut book).unwrap();
    assert_eq!(apply_sell(&mut book, "600000.SH", 50, 1_000, 5).unwrap_err().0, "invalid_sell");
    assert_eq!(apply_sell(&mut book, "000001.SZ", 100, 1_000, 5).unwrap_err().0, "t_plus_one_violation");
}

#[test]
fn fees_have_minimum_and_sell_stamp_duty() {
    assert_eq!(fees_cents("buy", 1_000), 500);
    assert_eq!(fees_cents("buy", 10_000_000), 3_000);
    assert_eq!(fees_cents("sell", 10_000_000), 8_000);
}

#[test]
fn allocation_moves_at_most_ten_points_and_stays_in_bounds() {
    let up = propose(5_000, 0, 5_000).unwrap();
    assert_eq!(up.steady_bps, 4_000);
    assert_eq!(up.aggressive_bps, 6_000);
    let capped = propose(7_500, 0, 50_000).unwrap();
    assert_eq!(capped.steady_bps, 6_500);
    assert_eq!(capped.aggressive_bps, 3_500);
    let floor = propose(2_500, 50_000, 0).unwrap();
    assert_eq!(floor.steady_bps, 3_500);
    assert_eq!(floor.aggressive_bps, 6_500);
    let noise = propose(5_000, 0, 99).unwrap();
    assert_eq!(noise.steady_bps, 5_000);
    assert_eq!(noise.aggressive_bps, 5_000);
    let neutral = propose(5_000, 0, 0).unwrap();
    assert_eq!(neutral.steady_bps, 5_000);
    assert_eq!(neutral.aggressive_bps, 5_000);
    let edge = propose(5_000, 0, 100).unwrap();
    assert_eq!(edge.steady_bps, 4_900);
    assert_eq!(edge.aggressive_bps, 5_100);
    let at_ceiling = propose(8_000, 0, 0).unwrap();
    assert_eq!(at_ceiling.steady_bps, 8_000);
    let at_floor = propose(2_000, 0, 0).unwrap();
    assert_eq!(at_floor.steady_bps, 2_000);
    assert!(propose(1_000, 0, 0).is_err());
}

#[test]
fn strategy_exit_uses_only_the_sellable_quantity() {
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    // Same day: nothing sellable yet (T+1).
    let closed = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), None)],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-10-30",
    );
    assert!(closed.is_empty());
    settle_overnight(&mut book).unwrap();
    // Next trading day, but still inside the minimum holding period, so a
    // vanished signal must not exit yet.
    let held_early = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), None)],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-02",
    );
    assert!(held_early.is_empty());
    // Past the holding period it exits.
    let closed = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), None)],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-20",
    );
    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].side, "sell");
    assert_eq!(closed[0].quantity, 100);
}

#[test]
fn strategy_does_not_buy_with_cash_reserved_for_the_cash_floor() {
    // Budget must be large enough that the steady slot's 15% target clears the
    // minimum-entry floor: 10_000_000 * 15% = 1_500_000 cents = 15_000 CNY.
    let book = SlotBook {
        cash_cents: 10_000_000,
        ..book()
    };
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), Some("trend".into()))],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-02",
    );
    assert_eq!(orders.len(), 1);
    let amount = orders[0].quantity * 1_000;
    // Cash floor for the steady slot is 10% of the budget.
    assert!(book.cash_cents - amount >= 1_000_000);
}

#[test]
fn a_position_too_small_to_clear_commission_is_not_opened() {
    // 3 bps commission with a 5 CNY floor means anything near the floor pays
    // ~30x the rate. Below `min_entry_cents` the order is skipped entirely.
    let book = SlotBook {
        cash_cents: 1_500_000, // steady target = 225_000 cents = 2_250 CNY
        ..book()
    };
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), Some("trend".into()))],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-02",
    );
    assert!(
        orders.is_empty(),
        "target below min_entry_cents must not open a position"
    );
}

#[test]
fn a_position_inside_the_holding_period_is_not_exited() {
    // Steady requires 10 calendar days. A signal that vanishes on day 3 must
    // not exit -- that is the flip-flop that produced 543 fills in a year.
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 1_000, 1_000, 5, "2026-11-02").unwrap();
    settle_overnight(&mut book).unwrap();
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);

    let early = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), None)],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-05",
    );
    assert!(early.is_empty(), "day 3 is inside the holding period");

    let later = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), None)],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-12",
    );
    assert_eq!(later.len(), 1, "day 10 is past the holding period");
    assert_eq!(later[0].side, "sell");
}

#[test]
fn buy_plan_leaves_room_for_the_minimum_commission() {
    let book = SlotBook {
        cash_cents: 100_100,
        ..book()
    };
    let closes = BTreeMap::from([("600000.SH".to_string(), 1)]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[("600000.SH".into(), Some("trend".into()))],
        1_000_000,
        book.cash_cents,
        &closes,
        &book.sellable,
        "2026-11-02",
    );
    assert!(orders.is_empty());
}

#[test]
fn allocation_score_is_return_minus_drawdown() {
    assert_eq!(score(250, 80), 170);
    assert_eq!(score(-100, 50), -150);
}

#[test]
fn price_set_validation_rejects_non_reviewed_duplicates_and_bad_days() {
    let valid = PriceSet {
        schema_version: 1,
        price_set_id: "set-1".into(),
        market_data_pack_id: "pack-1".into(),
        trust: "reviewed".into(),
        coverage_start: "2026-10-30".into(),
        coverage_end: "2026-10-30".into(),
        entries: vec![PriceEntry {
            symbol: "600000.SH".into(),
            day: "2026-10-30".into(),
            price_cents: 1_000,
            source: "reviewed-source".into(),
            observed_at: "2026-10-30T15:00:00+08:00".into(),
        }],
        source_digest: "a".repeat(64),
    };
    valid.validate().unwrap();
    for mutate in [
        |set: &mut PriceSet| set.trust = "synthetic".into(),
        |set: &mut PriceSet| set.schema_version = 2,
        |set: &mut PriceSet| set.coverage_start = "2026-10-31".into(),
        |set: &mut PriceSet| set.entries[0].price_cents = 0,
        |set: &mut PriceSet| set.entries[0].day = "2026-11-03".into(),
        |set: &mut PriceSet| set.entries.push(set.entries[0].clone()),
        |set: &mut PriceSet| set.source_digest = "x".repeat(64),
    ] {
        let mut changed = valid.clone();
        mutate(&mut changed);
        assert!(changed.validate().is_err());
    }
}

#[test]
fn price_set_validation_rejects_a_sparse_reviewed_rectangle() {
    let mut set = PriceSet {
        schema_version: 1,
        price_set_id: "sparse".into(),
        market_data_pack_id: "pack-1".into(),
        trust: "reviewed".into(),
        coverage_start: "2026-10-30".into(),
        coverage_end: "2026-11-02".into(),
        entries: vec![
            PriceEntry {
                symbol: "600000.SH".into(),
                day: "2026-10-30".into(),
                price_cents: 1_000,
                source: "reviewed-source".into(),
                observed_at: "2026-10-30T15:00:00+08:00".into(),
            },
            PriceEntry {
                symbol: "600000.SH".into(),
                day: "2026-11-02".into(),
                price_cents: 1_010,
                source: "reviewed-source".into(),
                observed_at: "2026-11-02T15:00:00+08:00".into(),
            },
            PriceEntry {
                symbol: "000001.SZ".into(),
                day: "2026-10-30".into(),
                price_cents: 1_100,
                source: "reviewed-source".into(),
                observed_at: "2026-10-30T15:00:00+08:00".into(),
            },
        ],
        source_digest: "c".repeat(64),
    };
    assert_eq!(set.validate().unwrap_err().0, "incomplete_price_set");
    set.entries.push(PriceEntry {
        symbol: "000001.SZ".into(),
        day: "2026-11-02".into(),
        price_cents: 1_110,
        source: "reviewed-source".into(),
        observed_at: "2026-11-02T15:00:00+08:00".into(),
    });
    set.validate().unwrap();
}

#[test]
fn missing_close_price_fails_the_whole_valuation() {
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    assert_eq!(market_value(&book, &BTreeMap::new()).unwrap_err().0, "missing_close_price");
    let prices = BTreeMap::from([("600000.SH".to_string(), 1_100)]);
    assert_eq!(market_value(&book, &prices).unwrap(), 110_000);
}

#[test]
fn drawdown_uses_running_peak() {
    assert_eq!(drawdown_bps(&[100, 120, 90, 130, 117], 100), 2_500);
    assert_eq!(drawdown_bps(&[], 100), 0);
}
