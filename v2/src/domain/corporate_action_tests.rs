//! End-to-end: a settlement must differ once factors are attached.
//!
//! The unit tests prove the factor arithmetic. These prove the wiring: that a
//! close score delivered through `IndexedPriceSet` actually consults the
//! adjustment book, and that the *fill* price stays unadjusted while the
//! *factor* input does not. Getting one and not the other is the failure mode
//! this whole change exists to remove, so it is asserted here rather than
//! inferred from the code reading correctly.

use std::collections::BTreeMap;

use super::*;

fn entry(symbol: &str, day: &str, price_cents: i64) -> PriceEntry {
    PriceEntry {
        symbol: symbol.into(),
        day: day.into(),
        price_cents,
        source: "offline-test".into(),
        observed_at: format!("{day}T15:00:00+08:00"),
        tradable: true,
        can_buy: true,
        can_sell: true,
        open_cents: 0,
        high_cents: 0,
        low_cents: 0,
        volume_shares: 0,
    }
}

/// 61 sessions on one smooth, slowly rising line.
///
/// The 10-for-10 bonus is a pure reporting change: the holder's wealth does not
/// move, so the *adjusted* series is the smooth line and the raw series is the
/// line with a 50% hole punched in it. Building it the other way round (price
/// keeps climbing on the halved basis) would hand the raw series an artificial
/// +100% jump and make the unadjusted score look *better*, which is the
/// opposite of the defect.
fn split_series() -> Vec<PriceEntry> {
    let bonus_at = 40;
    (0..61)
        .map(|index| {
            let economic = 1_000 + index * 40; // the true, continuous price
            let traded = if index < bonus_at {
                economic
            } else {
                // After 10-for-10 the same wealth is two shares at half price.
                economic / 2
            };
            entry("600000.SH", &format!("2026-{:02}-{:02}", 1 + index / 28, 1 + index % 28), traded)
        })
        .collect()
}

fn bonus_book() -> AdjustmentBook {
    let event = CorporateAction {
        ex_day: "2026-02-13".into(),  // fixture day index 40
        cash_per_share_cents: 0,
        bonus_per_ten: 10,
        transfer_per_ten: 0,
    };
    let series = AdjustmentSeries::build(&[event], |_| Some(1_000 + 39 * 400)).unwrap();
    assert!(series.is_complete(), "the fixture factor must be priceable");
    let mut map = BTreeMap::new();
    map.insert("600000.SH".to_string(), series);
    AdjustmentBook::new(map)
}

fn bars_for(set: &IndexedPriceSet<'_>, symbol: &str, day: &str) -> Vec<Bar> {
    set.bars_for(symbol, day)
}

#[test]
fn the_adjustment_book_reaches_the_score_through_the_index() {
    let entries = split_series();
    let last = entries.last().unwrap().day.clone();
    let set = PriceSet {
        schema_version: 1,
        price_set_id: "deal-test".into(),
        market_data_pack_id: "pack".into(),
        trust: "reviewed".into(),
        coverage_start: entries.first().unwrap().day.clone(),
        coverage_end: last.clone(),
        entries: entries.clone(),
        source_digest: "a".repeat(64),
    };
    let bare = IndexedPriceSet::new(&set);
    let adjusted = IndexedPriceSet::with_adjustments(&set, bonus_book());

    let bars = bars_for(&bare, "600000.SH", &last);
    let without = score_signal_adjusted(StrategyId::Steady, &bars, bare.adjustments().for_symbol("600000.SH"));
    let with = score_signal_adjusted(StrategyId::Steady, &bars, adjusted.adjustments().for_symbol("600000.SH"));

    // The bare index has no book, so the two must be able to disagree; if they
    // cannot, the wiring is not being read at all.
    assert!(
        adjusted.adjustments().for_symbol("600000.SH").is_some(),
        "the book must be reachable from the index"
    );
    assert_ne!(
        without.score, with.score,
        "a 10-for-10 bonus must change the trend score (bare {} vs adjusted {})",
        without.score, with.score
    );
    // The raw series has a 50% hole at the ex-day, so the 20-day average is
    // dragged far below the 60-day one: the name reads as a broken trend.
    // Restoring the economic series removes that artefact.
    assert!(
        without.score < 0,
        "the unadjusted series must read as a broken trend, got {}",
        without.score
    );
    assert!(
        with.score >= 0,
        "the adjusted series must no longer read as broken, got {}",
        with.score
    );
}

#[test]
fn the_fill_price_is_never_rewritten_by_a_factor() {
    let entries = split_series();
    let last = entries.last().unwrap().day.clone();
    let set = PriceSet {
        schema_version: 1,
        price_set_id: "deal-test".into(),
        market_data_pack_id: "pack".into(),
        trust: "reviewed".into(),
        coverage_start: entries.first().unwrap().day.clone(),
        coverage_end: last.clone(),
        entries: entries.clone(),
        source_digest: "a".repeat(64),
    };
    let raw_close = set.valuation("600000.SH", &last).unwrap();
    let adjusted = IndexedPriceSet::with_adjustments(&set, bonus_book());
    // `closes_for_indexed` feeds the fill; it must equal the stored close.
    let closes = adjusted.tradable_closes_on(&last);
    assert_eq!(
        closes.get("600000.SH").copied(),
        Some(raw_close),
        "settlement fills at the traded price, not the factor-adjusted one"
    );
}

#[test]
fn a_symbol_with_no_known_action_scores_as_before() {
    let entries: Vec<PriceEntry> = (0..61)
        .map(|index| entry("000001.SZ", &format!("2026-{:02}-{:02}", 1 + index / 28, 1 + index % 28),
            1_000 + index * 10))
        .collect();
    let set = PriceSet {
        schema_version: 1,
        price_set_id: "deal-test".into(),
        market_data_pack_id: "pack".into(),
        trust: "reviewed".into(),
        coverage_start: entries.first().unwrap().day.clone(),
        coverage_end: entries.last().unwrap().day.clone(),
        entries,
        source_digest: "a".repeat(64),
    };
    let with_book = IndexedPriceSet::with_adjustments(&set, bonus_book());
    let bare = IndexedPriceSet::new(&set);
    let day = bare.coverage_end.clone();
    let bars = bars_for(&bare, "000001.SZ", &day);
    assert_eq!(
        score_signal_adjusted(StrategyId::Steady, &bars, bare.adjustments().for_symbol("600000.SH")),
        score_signal_adjusted(StrategyId::Steady, &bars, with_book.adjustments().for_symbol("000001.SZ")),
        "a book that does not mention this symbol must not change its score"
    );
}

#[test]
fn an_incomplete_factor_is_reported_by_the_book() {
    let event = CorporateAction {
        ex_day: "2026-02-13".into(),
        cash_per_share_cents: 0,
        bonus_per_ten: 10,
        transfer_per_ten: 0,
    };
    let series = AdjustmentSeries::build(&[event], |_| None).unwrap();
    let mut map = BTreeMap::new();
    map.insert("600000.SH".to_string(), series);
    let book = AdjustmentBook::new(map);
    assert_eq!(book.incomplete_symbols(), vec!["600000.SH"]);
}
