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
            open_cents: 0,
            high_cents: 0,
            low_cents: 0,
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
            let registered = score_registered(slot.algorithm_id(), &bars, None).unwrap();
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

/// Below the shortest usable window there is genuinely nothing to score.
///
/// This used to also cover 60 bars, because the boundary was "61 or nothing".
/// That boundary silently excluded every recently listed or long-suspended
/// name from the universe: it was visible in the list, could never be bought,
/// and nothing recorded why. The 60-bar case therefore moved to the
/// shortened-history test below, which asserts the score is produced *and*
/// labelled rather than dropped.
#[test]
fn too_little_history_stays_flat_but_an_unknown_algorithm_is_an_error() {
    let case = real_cases().remove(0);
    let bars = bars(&case);
    for short in [&bars[..0], &bars[..MIN_USABLE_CLOSES - 1]] {
        for slot in [StrategyId::Steady, StrategyId::Aggressive] {
            assert_eq!(
                score_registered(slot.algorithm_id(), short, None).unwrap(),
                SignalScore {
                    score: 0,
                    reason: "no_edge",
                    short_window: false
                }
            );
        }
        assert_eq!(
            score_registered("unregistered", short, None).unwrap_err().0,
            "unknown_strategy:unregistered"
        );
    }
}

/// A short history is scored on what exists, and says so.
///
/// The alternative -- returning `flat` -- is indistinguishable from "this
/// symbol has no edge", so a universe quietly loses every new listing.
#[test]
fn a_short_but_usable_history_is_scored_and_labelled() {
    let case = real_cases().remove(0);
    let bars = bars(&case);
    let short = &bars[..FULL_WINDOW_CLOSES - 1];
    for slot in [StrategyId::Steady, StrategyId::Aggressive] {
        let scored = score_registered(slot.algorithm_id(), short, None).unwrap();
        assert!(
            scored.short_window,
            "{:?} must mark a sub-window score",
            slot
        );
        assert!(
            scored.reason_with_history().ends_with("_short_history"),
            "the stored reason must carry the shortened-history fact: {}",
            scored.reason_with_history()
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

#[test]
fn the_library_only_algorithm_scores_but_is_not_bound_to_a_slot() {
    // momentum-reversal-v1 exists to prove the library can hold an algorithm
    // that is scored and reported without owning a book. Both halves of that
    // claim are checked here: it must produce a real score, and it must not be
    // reachable through any trading slot.
    let case = real_cases().into_iter().next().expect("a fixture case");
    let bars = bars(&case);
    let scored = score_registered("momentum-reversal-v1", &bars, None)
        .expect("a library algorithm must be scoreable by id");
    assert!(!scored.reason.is_empty(),
            "a scored algorithm must always name its reason");

    for slot in [StrategyId::Steady, StrategyId::Aggressive] {
        assert_ne!(slot.algorithm_id(), "momentum-reversal-v1",
                   "a library-only algorithm must not be bound to a trading slot");
    }
}

#[test]
fn a_library_algorithm_with_no_history_scores_flat_rather_than_guessing() {
    // A short series must not be scored as if it were complete.
    let short: Vec<Bar> = (0..10)
        .map(|index| Bar {
            symbol: "600000.SH".into(),
            day: format!("2026-01-{:02}", index + 1),
            close_cents: 1_000 + index,
            volume: 0,
            open_cents: 0,
            high_cents: 0,
            low_cents: 0,
            tradable: true,
        })
        .collect();
    let scored = score_registered("momentum-reversal-v1", &short, None).unwrap();
    assert_eq!(scored.score, 0, "no history means no opinion");
}
