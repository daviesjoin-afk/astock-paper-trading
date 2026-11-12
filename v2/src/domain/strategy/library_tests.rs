use std::collections::BTreeSet;

use serde::Deserialize;

use super::*;

#[derive(Deserialize)]
struct ExpectedScore {
    score: i64,
    reason: String,
}

#[derive(Deserialize)]
struct ExpectedSlots {
    steady: ExpectedScore,
    aggressive: ExpectedScore,
}

#[derive(Deserialize)]
struct GoldenCase {
    symbol: String,
    as_of: String,
    closes_cents: Vec<i64>,
    expected: ExpectedSlots,
}

fn real_cases() -> Vec<GoldenCase> {
    serde_json::from_str(include_str!(
        "../../../fixtures/strategy-library-golden.json"
    ))
    .unwrap()
}

fn bars(case: &GoldenCase) -> Vec<Bar> {
    case.closes_cents
        .iter()
        .map(|close| Bar {
            symbol: case.symbol.clone(),
            day: case.as_of.clone(),
            close_cents: *close,
            volume: 0,
            tradable: true,
        })
        .collect()
}

#[test]
fn registered_algorithms_preserve_frozen_real_scores_and_slot_bindings() {
    for case in real_cases() {
        let bars = bars(&case);
        for (slot, expected) in [
            (StrategyId::Steady, &case.expected.steady),
            (StrategyId::Aggressive, &case.expected.aggressive),
        ] {
            let registered = score_registered(slot.algorithm_id(), &bars).unwrap();
            let via_slot = score_signal(slot, &bars);
            assert_eq!(registered, via_slot);
            assert_eq!(
                registered.score,
                expected.score,
                "{} {} {} closes / {}",
                case.symbol,
                case.as_of,
                bars.len(),
                slot.algorithm_id()
            );
            assert_eq!(registered.reason, expected.reason);
        }
    }
}

#[test]
fn library_ids_are_unique_and_both_fixed_slot_algorithms_are_registered() {
    let ids: BTreeSet<_> = STRATEGY_LIBRARY
        .iter()
        .map(|algorithm| algorithm.id())
        .collect();
    assert_eq!(ids.len(), STRATEGY_LIBRARY.len());
    for slot in [StrategyId::Steady, StrategyId::Aggressive] {
        assert!(ids.contains(slot.algorithm_id()));
    }
    assert_eq!(
        serde_json::to_string(&StrategyId::Steady).unwrap(),
        "\"steady\""
    );
    assert_eq!(
        serde_json::to_string(&StrategyId::Aggressive).unwrap(),
        "\"aggressive\""
    );
}

#[test]
fn insufficient_history_stays_flat_but_an_unknown_algorithm_is_an_error() {
    let case = real_cases().remove(0);
    let bars = bars(&case);
    for short in [&bars[..0], &bars[..60]] {
        for slot in [StrategyId::Steady, StrategyId::Aggressive] {
            assert_eq!(
                score_registered(slot.algorithm_id(), short).unwrap(),
                SignalScore {
                    score: 0,
                    reason: "no_edge"
                }
            );
        }
        assert_eq!(
            score_registered("unregistered", short).unwrap_err().0,
            "unknown_strategy:unregistered"
        );
    }
}

#[test]
fn long_real_histories_are_not_silently_cut_to_sixty_one_closes() {
    let changed_by_truncation = real_cases().into_iter().any(|case| {
        if case.closes_cents.len() != 80 {
            return false;
        }
        let bars = bars(&case);
        score_signal(StrategyId::Steady, &bars)
            != score_signal(StrategyId::Steady, &bars[bars.len() - 61..])
    });
    assert!(
        changed_by_truncation,
        "the golden sample exercises longer-history scoring"
    );
}
