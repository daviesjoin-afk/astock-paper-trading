//! Liquidity refusal: order size against the session's own turnover.
//!
//! The behaviour that matters is not the arithmetic, it is the two edges:
//! an order at the limit must pass, and a missing turnover must not read as an
//! illiquid market.

use super::{check, parse_limit_bps, Liquidity, DEFAULT_MAX_TURNOVER_SHARE_BPS};

#[test]
fn a_small_order_against_a_large_day_is_fine() {
    // 10k order, 10M turnover = 10 bps, well under the 100 bps default.
    assert_eq!(check(1_000_000, 100_000_000, 100), Liquidity::Fine);
}

#[test]
fn an_order_equal_to_the_whole_day_is_refused() {
    let verdict = check(100_000_000, 100_000_000, 100);
    match verdict {
        Liquidity::TooLarge { share_bps, limit_bps, .. } => {
            assert_eq!(share_bps, 10_000);
            assert_eq!(limit_bps, 100);
        }
        Liquidity::Fine => panic!("an order for the entire session must be refused"),
    }
}

#[test]
fn an_order_exactly_at_the_limit_passes() {
    // 100 bps of 100M is 1M. At the limit is allowed, above it is not.
    assert_eq!(check(1_000_000, 100_000_000, 100), Liquidity::Fine);
    assert!(matches!(check(1_000_001, 100_000_000, 100), Liquidity::TooLarge { .. }));
}

#[test]
fn rounding_does_not_let_a_slightly_oversized_order_through() {
    // 1_000_001 / 100_000_000 is 100.0001 bps; integer division would floor to
    // 100 and pass it. The ceiling division is what stops that.
    let verdict = check(1_000_001, 100_000_000, 100);
    assert!(matches!(verdict, Liquidity::TooLarge { share_bps, .. } if share_bps == 101));
}

#[test]
fn unknown_turnover_skips_the_check_instead_of_blocking_everything() {
    // A hand-authored price set has no amount column. Treating that as zero
    // liquidity would freeze the whole ledger, and a frozen ledger reads as
    // "no signals" rather than "no data".
    assert_eq!(check(5_000_000, 0, 100), Liquidity::Fine);
    assert_eq!(check(5_000_000, -1, 100), Liquidity::Fine);
}

#[test]
fn a_non_positive_order_never_triggers_a_refusal() {
    assert_eq!(check(0, 100, 100), Liquidity::Fine);
    assert_eq!(check(-5, 100, 100), Liquidity::Fine);
}

#[test]
fn a_non_positive_limit_disables_the_check() {
    // Not an error at the rule level; the caller decides policy. This keeps the
    // check out of the way when a deployment explicitly turns it off.
    assert_eq!(check(100_000_000, 100, 0), Liquidity::Fine);
}

#[test]
fn the_refusal_reason_states_what_was_measured() {
    let verdict = check(50_000_000, 100_000_000, 100);
    let reason = verdict.reason().expect("must explain itself");
    assert!(reason.contains("5000 bps"), "reason should carry the measured share: {reason}");
    assert!(reason.contains("100 bps"), "reason should carry the limit: {reason}");
    assert!(reason.starts_with("illiquid"));
}

#[test]
fn a_fine_verdict_has_no_reason() {
    assert!(check(1, 100_000_000, 100).reason().is_none());
}

#[test]
fn the_default_is_loose_enough_to_be_a_sanity_check_not_a_strategy() {
    // One percent. Tightening this is a policy decision, and the value is
    // exported so a caller can see it rather than guess.
    assert_eq!(DEFAULT_MAX_TURNOVER_SHARE_BPS, 100);
}

#[test]
fn the_policy_parser_rejects_nonsense_rather_than_clamping() {
    assert_eq!(parse_limit_bps("250").unwrap(), 250);
    assert!(parse_limit_bps("0").is_err(), "zero would silently disable the check");
    assert!(parse_limit_bps("-5").is_err());
    assert!(parse_limit_bps("abc").is_err());
    assert!(parse_limit_bps("").is_err());
}
