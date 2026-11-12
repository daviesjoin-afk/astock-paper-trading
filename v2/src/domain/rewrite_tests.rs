use std::collections::BTreeMap;

use super::*;

/// Build a signal entry. `score` is what the hysteresis band acts on.
fn scored(symbol: &str, score: i64) -> (String, SignalScore) {
    (
        symbol.to_string(),
        SignalScore {
            score,
            reason: if score >= ENTRY_SCORE {
                "test_entry"
            } else {
                "test_neutral"
            },
        },
    )
}

/// Strong positive score: enter and hold.
fn strong(symbol: &str) -> (String, SignalScore) {
    scored(symbol, ENTRY_SCORE + 20)
}

/// Below the exit band: leave.
fn weak(symbol: &str) -> (String, SignalScore) {
    scored(symbol, EXIT_SCORE - 20)
}

/// Inside the hysteresis band: neither enter nor exit.
fn neutral(symbol: &str) -> (String, SignalScore) {
    scored(symbol, 0)
}

fn bars(values: &[i64]) -> Vec<Bar> {
    values
        .iter()
        .enumerate()
        .map(|(index, close)| Bar {
            symbol: "600000.SH".into(),
            day: format!("2026-04-{:02}", index + 1),
            close_cents: *close,
            volume: 1,
            tradable: true,
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
fn aggressive_score_rewards_a_fresh_break() {
    let mut values = vec![1_000i64; 61];
    // Flat market: no momentum, no break.
    assert!(score_signal(StrategyId::Aggressive, &bars(&values)).score < ENTRY_SCORE);

    // A fresh break: close just past the prior 20-day high, small run-up.
    values[55..60].copy_from_slice(&[1_010, 1_020, 1_030, 1_040, 1_050]);
    values[60] = 1_055;
    let fresh = score_signal(StrategyId::Aggressive, &bars(&values));
    assert!(
        fresh.score >= ENTRY_SCORE,
        "fresh break should clear the entry band, got {} ({})",
        fresh.score,
        fresh.reason
    );

    // Below the prior high: the break failed.
    values[60] = 900;
    let failed = score_signal(StrategyId::Aggressive, &bars(&values));
    assert!(failed.score <= EXIT_SCORE, "failed break got {}", failed.score);
}

#[test]
fn aggressive_signal_rejects_an_already_extended_breakout() {
    // Same shape, but the close is 4.8% past the prior high. Measured over 203
    // real entries, breaks this extended went on to lose 2.70% over 5 days --
    // that is the tail of a move, not the start of one.
    let mut values = vec![1_000i64; 61];
    values[55..60].copy_from_slice(&[1_010, 1_020, 1_030, 1_040, 1_050]);
    values[60] = 1_100;
    let scored = score_signal(StrategyId::Aggressive, &bars(&values));
    assert!(
        scored.score < ENTRY_SCORE,
        "extended break must not clear the entry band, got {} ({})",
        scored.score,
        scored.reason
    );
}

#[test]
fn aggressive_signal_rejects_a_large_run_up_into_the_break() {
    // Momentum is positive and the close does clear the prior high, but the
    // stock has already climbed 25% off its 20-day low. Chasing that is what
    // the old rule did.
    let mut values = vec![1_000i64; 61];
    for (offset, value) in (0..20).map(|i| 850 + i * 12).enumerate() {
        values[41 + offset] = value;
    }
    values[60] = 1_080;
    let scored = score_signal(StrategyId::Aggressive, &bars(&values));
    assert!(
        scored.score < ENTRY_SCORE,
        "chasing an extended run-up must not enter, got {} ({})",
        scored.score,
        scored.reason
    );
}

#[test]
fn stop_loss_exits_inside_the_holding_period() {
    // The holding period must not pin a losing position. Steady stops at -12%.
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 1_000, 1_000, 5, "2026-11-02").unwrap();
    settle_overnight(&mut book).unwrap();
    // Down 13%: past the stop, but only 2 days held.
    let closes = BTreeMap::from([("600000.SH".to_string(), 870)]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[strong("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].side, "sell");
    assert_eq!(orders[0].reason, "stop_loss");
}

#[test]
fn a_small_drawdown_inside_the_holding_period_does_not_exit() {
    // Down 5%, inside the 12% stop and inside the holding period: hold.
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 1_000, 1_000, 5, "2026-11-02").unwrap();
    settle_overnight(&mut book).unwrap();
    let closes = BTreeMap::from([("600000.SH".to_string(), 950)]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[strong("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert!(orders.is_empty());
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
        &[weak("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert!(closed.is_empty());
    settle_overnight(&mut book).unwrap();
    // Next trading day with a clearly negative score: exits.
    let closed = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[weak("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].side, "sell");
    assert_eq!(closed[0].quantity, 100);
}

#[test]
fn a_halted_position_is_not_sold_without_a_tradable_close() {
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    settle_overnight(&mut book).unwrap();
    // The halted row has an official reference close for valuation, but no
    // fillable close today. The exit signal must wait for a trading day.
    let closes = BTreeMap::new();
    let valuations = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[weak("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &valuations,
        &book.sellable,
    );
    assert!(orders.is_empty(), "a halt must not emit an unfillable sell");
}

#[test]
fn a_halted_position_still_occupies_its_slot_budget() {
    let mut book = book();
    book.cash_cents = 5_000_000;
    apply_buy(&mut book, "000001.SZ", 500, 1_000, 5, "2026-10-30").unwrap();
    settle_overnight(&mut book).unwrap();
    let cash_after_buy = book.cash_cents;
    // 000001.SZ is halted today: only its valuation mark exists. The slot's
    // 15% target leaves 250,000 cents of room; the halted 500,000 cents must
    // still count as held, or the new buy would overshoot the target.
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    let valuations = BTreeMap::from([
        ("000001.SZ".to_string(), 1_000),
        ("600000.SH".to_string(), 1_000),
    ]);
    let orders = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[strong("600000.SH")],
        5_000_000,
        cash_after_buy,
        &closes,
        &valuations,
        &book.sellable,
    );
    assert!(
        orders.is_empty(),
        "a 250,000-cent order is below the 300,000-cent entry floor; \
         the halted position must reduce the room instead of being treated as cash"
    );
}

#[test]
fn a_halted_bar_never_changes_the_score() {
    let trading: Vec<_> = (0..61)
        .map(|index| {
            let mut bar = Bar {
                symbol: "600000.SH".into(),
                day: format!("2026-04-{:02}", index + 1),
                close_cents: 1_000 + index * 2,
                volume: 1,
                tradable: true,
            };
            if index == 60 {
                bar.close_cents = 1_120;
            }
            bar
        })
        .collect();
    let expected = score_registered("steady-v1", &trading).unwrap();
    let mut with_reference = trading;
    with_reference.push(Bar {
        symbol: "600000.SH".into(),
        day: "2026-06-01".into(),
        close_cents: 9_999,
        volume: 0,
        tradable: false,
    });
    assert_eq!(
        score_registered("steady-v1", &with_reference).unwrap(),
        expected
    );
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
        &[strong("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
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
        &[strong("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert!(
        orders.is_empty(),
        "target below min_entry_cents must not open a position"
    );
}

#[test]
fn a_position_stays_put_while_the_score_is_in_the_neutral_band() {
    // This is what replaces a fixed holding period: the position is not sold
    // merely because the score dipped, only when it falls through the exit
    // band. A condition oscillating around its threshold stays in the band and
    // therefore stops generating round trips.
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 2_000, 1_000, 5, "2026-11-02").unwrap();
    settle_overnight(&mut book).unwrap();
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);

    for (label, entry) in [("neutral", neutral("600000.SH")), ("faintly positive", scored("600000.SH", 10))] {
        let orders = orders_for_day(
            StrategyId::Steady,
            &book.positions,
            &[entry],
            book.cash_cents,
            book.cash_cents,
            &closes,
            &closes,
            &book.sellable,
        );
        assert!(orders.is_empty(), "{label} must not trigger an exit");
    }

    // Falls through the exit band: leave.
    let exit = orders_for_day(
        StrategyId::Steady,
        &book.positions,
        &[weak("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert_eq!(exit.len(), 1);
    assert_eq!(exit[0].side, "sell");
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
        &[strong("600000.SH")],
        1_000_000,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
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
            tradable: true,
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
                tradable: true,
            },
            PriceEntry {
                symbol: "600000.SH".into(),
                day: "2026-11-02".into(),
                price_cents: 1_010,
                source: "reviewed-source".into(),
                observed_at: "2026-11-02T15:00:00+08:00".into(),
                tradable: true,
            },
            PriceEntry {
                symbol: "000001.SZ".into(),
                day: "2026-10-30".into(),
                price_cents: 1_100,
                source: "reviewed-source".into(),
                observed_at: "2026-10-30T15:00:00+08:00".into(),
                tradable: true,
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
        tradable: true,
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
fn halted_price_set_valuations_use_the_reference_close_and_still_fail_when_absent() {
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-11-02").unwrap();
    let halted = BarRow {
        symbol: "600000.SH".into(),
        day: "2026-11-03".into(),
        price_cents: 995,
        tradable: false,
    };
    let set = PriceSet::from_bars(&[halted], "halted", "2026-11-03", "2026-11-03");
    set.validate().expect("a halt is a complete one-day row");
    assert_eq!(set.tradable_close("600000.SH", "2026-11-03"), None);
    assert_eq!(set.valuation("600000.SH", "2026-11-03"), Some(995));
    assert_eq!(
        market_value_from_set(&book, &set, "2026-11-03").unwrap(),
        99_500
    );
    assert_eq!(
        market_value_from_set(&book, &set, "2026-11-04")
            .unwrap_err()
            .0,
        "missing_close_price"
    );
}

#[test]
fn drawdown_uses_running_peak() {
    assert_eq!(drawdown_bps(&[100, 120, 90, 130, 117], 100), 2_500);
    assert_eq!(drawdown_bps(&[], 100), 0);
}

// --- Market regime applied to the slots -------------------------------

#[test]
fn steady_is_not_rewritten_by_the_market_regime() {
    // The steady score already demands a trend and low volatility per symbol.
    // Gating it again would double-count and stop it trading for no reason.
    for regime in [
        MarketRegime::Trending,
        MarketRegime::Choppy,
        MarketRegime::Declining,
        MarketRegime::Unknown,
    ] {
        let policy = SlotPolicy::for_strategy_in(StrategyId::Steady, regime);
        assert_eq!(policy.entry_score, ENTRY_SCORE, "{regime:?}");
        assert!(policy.allow_entries, "{regime:?}");
        assert_eq!(policy.target_weight_bps, 1_500, "{regime:?}");
        assert_eq!(policy.max_positions, 3, "{regime:?}");
    }
}

#[test]
fn trending_keeps_the_aggressive_baseline() {
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Trending);
    let baseline = SlotPolicy::for_strategy(StrategyId::Aggressive);
    assert_eq!(policy.entry_score, baseline.entry_score);
    assert!(policy.allow_entries);
    assert_eq!(policy.target_weight_bps, baseline.target_weight_bps);
    assert_eq!(policy.max_positions, baseline.max_positions);
}

#[test]
fn choppy_raises_the_entry_bar_and_shrinks_exposure() {
    for regime in [MarketRegime::Choppy, MarketRegime::Unknown] {
        let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, regime);
        assert_eq!(policy.entry_score, CHOPPY_ENTRY_SCORE, "{regime:?}");
        assert!(policy.entry_score > ENTRY_SCORE, "{regime:?}");
        assert!(policy.allow_entries, "{regime:?}");
        assert!(policy.target_weight_bps < 3_000, "{regime:?}");
        assert!(policy.max_positions < 3, "{regime:?}");
        // The exit band is untouched: the slot still leaves on the same
        // evidence, it just stops adding.
        assert_eq!(policy.stop_loss_bps, 700, "{regime:?}");
    }
}

#[test]
fn declining_blocks_entries_but_keeps_the_stop() {
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Declining);
    assert!(!policy.allow_entries);
    assert_eq!(policy.stop_loss_bps, 700);
}

#[test]
fn a_declining_market_refuses_new_entries_even_on_a_strong_score() {
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Declining);
    let signals = vec![strong("600000.SH")];
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    let orders = orders_for_day_in(
        StrategyId::Aggressive,
        &policy,
        &[],
        &signals,
        1_000_000,
        1_000_000,
        &closes,
        &closes,
        &[],
    );
    assert!(orders.is_empty(), "no entries in a declining market");
}

#[test]
fn choppy_still_enters_on_a_score_above_the_raised_bar() {
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Choppy);
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    // Exactly at the old threshold: refused, the bar moved.
    let marginal = vec![scored("600000.SH", ENTRY_SCORE + 5)];
    let orders = orders_for_day_in(
        StrategyId::Aggressive,
        &policy,
        &[],
        &marginal,
        1_000_000,
        1_000_000,
        &closes,
        &closes,
        &[],
    );
    assert!(orders.is_empty(), "a marginal break is not worth taking in a range");

    // Comfortably above the raised bar: taken. The budget is large enough
    // that the reduced 20% weight still clears `min_entry_cents`; in a choppy
    // market a small slot correctly skips the order as not worth the
    // commission, which is covered separately.
    let strong_signal = vec![scored("600000.SH", CHOPPY_ENTRY_SCORE + 10)];
    let orders = orders_for_day_in(
        StrategyId::Aggressive,
        &policy,
        &[],
        &strong_signal,
        10_000_000,
        10_000_000,
        &closes,
        &closes,
        &[],
    );
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].side, "buy");
}

#[test]
fn choppy_still_exits_a_position_whose_score_turned() {
    // The raised entry bar must not raise the exit bar: getting out is not
    // subject to a regime filter.
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Choppy);
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    settle_overnight(&mut book).unwrap();
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_010)]);
    let orders = orders_for_day_in(
        StrategyId::Aggressive,
        &policy,
        &book.positions,
        &[weak("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].side, "sell");
}

#[test]
fn declining_still_runs_the_hard_stop() {
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Declining);
    let mut book = book();
    apply_buy(&mut book, "600000.SH", 100, 1_000, 5, "2026-10-30").unwrap();
    settle_overnight(&mut book).unwrap();
    // 8% below cost: past the aggressive 7% stop, but the score is neutral.
    let closes = BTreeMap::from([("600000.SH".to_string(), 920)]);
    let orders = orders_for_day_in(
        StrategyId::Aggressive,
        &policy,
        &book.positions,
        &[neutral("600000.SH")],
        book.cash_cents,
        book.cash_cents,
        &closes,
        &closes,
        &book.sellable,
    );
    assert_eq!(orders.len(), 1);
    assert_eq!(orders[0].reason, "stop_loss");
}

#[test]
fn choppy_small_slot_skips_an_order_that_cannot_pay_its_commission() {
    // A 20% weight on a 10,000 CNY slot is 2,000 CNY, under the 3,000 CNY
    // minimum entry. Placing it would pay the 5 CNY commission floor on a
    // position too small to earn it back, so the slot correctly stands aside.
    let policy = SlotPolicy::for_strategy_in(StrategyId::Aggressive, MarketRegime::Choppy);
    let closes = BTreeMap::from([("600000.SH".to_string(), 1_000)]);
    let signals = vec![scored("600000.SH", CHOPPY_ENTRY_SCORE + 10)];
    let orders = orders_for_day_in(
        StrategyId::Aggressive,
        &policy,
        &[],
        &signals,
        1_000_000,
        1_000_000,
        &closes,
        &closes,
        &[],
    );
    assert!(orders.is_empty());
}
