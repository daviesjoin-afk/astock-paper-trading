
    use super::*;

    fn request(day: &str, side: Side) -> SimulationRequest {
        SimulationRequest {
            schema_version: 1, cycle_id: "demo-cycle".into(),
            strategy_version_id: "alpha-v1".into(), signal_id: "signal-1".into(),
            decision_at: format!("{day}T10:00:00+08:00"),
            signal: Signal { symbol: "600000.SH".into(), side },
            quote: Quote {
                symbol: "600000.SH".into(), price_cents: 1234,
                observed_at: format!("{day}T09:59:30+08:00"),
                source: "offline-fixture".into(), quote_id: "q-1".into(),
                tradable: true, limit_up: false, limit_down: false,
            },
        }
    }

    #[test]
    fn buy_is_board_lot_and_does_not_spend_more_than_budget() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let out = decide(&state, &request("2026-10-30", Side::Buy)).unwrap();
        let fill = out.fill.unwrap();
        assert_eq!(fill.quantity % 100, 0);
        assert!(fill.quantity > 0);
        assert!(fill.quantity * fill.price_cents + fill.fees_cents <= 1_000_000);
    }

    #[test]
    fn t_plus_one_blocks_sale_on_purchase_day_then_allows_next_day() {
        let mut state = PortfolioState::new(INITIAL_CASH_CENTS);
        let buy = decide(&state, &request("2026-10-30", Side::Buy)).unwrap().fill.unwrap();
        state.apply(&buy).unwrap();
        assert_eq!(decide(&state, &request("2026-10-30", Side::Sell)).unwrap().reason,
                   "no_t1_sellable_lot");
        let sell = decide(&state, &request("2026-11-02", Side::Sell)).unwrap();
        assert_eq!(sell.status, DecisionStatus::Filled);
        state.apply(sell.fill.as_ref().unwrap()).unwrap();
        assert_eq!(state.quantity("600000.SH"), 0);
    }

    #[test]
    fn stale_future_and_limit_up_quotes_fail_closed() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let mut req = request("2026-10-30", Side::Buy);
        req.quote.observed_at = "2026-10-30T09:50:00+08:00".into();
        assert_eq!(decide(&state, &req).unwrap().reason, "stale_quote");
        req.quote.observed_at = "2026-10-30T10:00:01+08:00".into();
        assert_eq!(decide(&state, &req).unwrap().reason, "quote_from_future");
        req.quote.observed_at = "2026-10-30T09:59:30+08:00".into();
        req.quote.limit_up = true;
        assert_eq!(decide(&state, &req).unwrap().reason, "limit_up_buy_blocked");
    }

    #[test]
    fn no_wall_clock_or_random_inputs_affect_decision() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let req = request("2026-10-30", Side::Buy);
        assert_eq!(decide(&state, &req).unwrap(), decide(&state, &req).unwrap());
    }

    #[test]
    fn false_symbol_and_wrong_timezone_are_rejected() {
        let state = PortfolioState::new(INITIAL_CASH_CENTS);
        let mut req = request("2026-10-30", Side::Buy);
        req.quote.symbol = "000001.SZ".into();
        assert!(decide(&state, &req).is_err());
        req.quote.symbol = req.signal.symbol.clone();
        req.decision_at = "2026-10-30T10:00:00+00:00".into();
        assert!(decide(&state, &req).is_err());
    }

#[test]
fn nested_request_cannot_smuggle_execution_quantity_or_extra_quote_fields() {
    let req = request("2026-10-30", Side::Buy);
    let raw = serde_json::to_value(&req).unwrap();
    assert!(serde_json::from_value::<SimulationRequest>(raw.clone()).is_ok());

    let mut injected_qty = raw.clone();
    injected_qty["signal"]["quantity"] = serde_json::json!(100);
    assert!(serde_json::from_value::<SimulationRequest>(injected_qty).is_err(),
            "research must never select final execution quantity");

    let mut injected_amount = raw.clone();
    injected_amount["signal"]["amount"] = serde_json::json!(12_340);
    assert!(serde_json::from_value::<SimulationRequest>(injected_amount).is_err(),
            "research must never select final cash to spend");

    let mut injected_quote = raw;
    injected_quote["quote"]["override_price_cents"] = serde_json::json!(1);
    assert!(serde_json::from_value::<SimulationRequest>(injected_quote).is_err(),
            "unknown quote fields must fail closed");
}

#[test]
fn quote_freshness_preserves_subsecond_precision_at_boundary() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let mut req = request("2026-10-30", Side::Buy);
    req.quote.observed_at = "2026-10-30T09:57:00+08:00".into();
    let boundary = decide(&state, &req).unwrap();
    assert_eq!(boundary.status, DecisionStatus::Filled);
    assert_eq!(boundary, decide(&state, &req).unwrap());
    req.quote.observed_at = "2026-10-30T09:56:59.999+08:00".into();
    let stale = decide(&state, &req).unwrap();
    assert_eq!(stale.reason, "stale_quote");
    assert!(stale.fill.is_none());
    assert_eq!(stale.cash_after_cents, state.cash_cents);
    assert_eq!(stale, decide(&state, &req).unwrap());
}
