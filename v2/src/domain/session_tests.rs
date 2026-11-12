use super::*;

#[test]
fn a_single_price_limit_down_session_blocks_sales_only() {
    // 10.00 -> 9.00 is the main-board floor. Every trade printed at 9.00, so
    // there was no bid left to sell into, while a buyer could still get filled.
    let trade = tradeability("600000.SH", 1_000, 900, 900, 900, 900, true);
    assert!(!trade.can_sell);
    assert!(trade.can_buy);
}

#[test]
fn a_single_price_limit_up_session_blocks_purchases_only() {
    let trade = tradeability("600000.SH", 1_000, 1_100, 1_100, 1_100, 1_100, true);
    assert!(trade.can_sell);
    assert!(!trade.can_buy);
}

#[test]
fn closing_at_the_limit_after_trading_away_stays_fillable() {
    // The session traded 9.90-10.10 and closed on the floor. A seller could
    // have dealt during the day, so the close alone must not seal the book.
    let trade = tradeability("600000.SH", 1_000, 1_010, 1_010, 990, 900, true);
    assert!(trade.can_sell && trade.can_buy);
}

#[test]
fn board_limits_are_not_a_flat_ten_percent() {
    assert_eq!(daily_limit_bps("600000.SH"), 1_000);
    assert_eq!(daily_limit_bps("300750.SZ"), 2_000);
    assert_eq!(daily_limit_bps("688111.SH"), 2_000);
    assert_eq!(daily_limit_bps("920001.BJ"), 3_000);
    // A 15% drop is inside the ChiNext limit and must not be read as a seal.
    let chi_next = tradeability("300750.SZ", 1_000, 850, 850, 850, 850, true);
    assert!(chi_next.can_sell);
}

#[test]
fn a_halt_or_missing_session_fields_never_invents_a_seal() {
    let halted = tradeability("600000.SH", 1_000, 900, 900, 900, 900, false);
    assert!(halted.can_buy && halted.can_sell);
    let no_ohlc = tradeability("600000.SH", 1_000, 0, 0, 0, 900, true);
    assert!(no_ohlc.can_buy && no_ohlc.can_sell);
    let no_prev = tradeability("600000.SH", 0, 900, 900, 900, 900, true);
    assert!(no_prev.can_buy && no_prev.can_sell);
}

#[test]
fn an_ex_dividend_gap_needs_the_reference_close_to_avoid_false_seals() {
    // Unadjusted history: 10.00 -> 5.00 is a 2-for-1 split, not a limit-down.
    // With the exchange's reference close (500) the session is plainly open;
    // with a stale 1000 reference it would look sealed, which is exactly why
    // the reference close is a required input rather than the prior raw close.
    let corrected = tradeability("600000.SH", 500, 500, 500, 500, 500, true);
    assert!(corrected.can_sell);
}
