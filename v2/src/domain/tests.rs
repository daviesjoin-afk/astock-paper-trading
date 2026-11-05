use super::*;

fn request(day: &str, side: Side) -> SimulationRequest {
    SimulationRequest {
        schema_version: 2, cycle_id: "demo-cycle".into(),
        strategy_version_id: "alpha-v1".into(), signal_id: "signal-1".into(),
        decision_at: format!("{day}T10:00:00+08:00"),
        signal: Signal { symbol: "600000.SH".into(), side },
        quote_ref: QuoteRef { source: "offline-fixture".into(), quote_id: "q-1".into() },
    }
}

fn evidence(day: &str) -> QuoteEvidence {
    QuoteEvidence {
        schema_version: 1, available_at: format!("{day}T09:59:31+08:00"),
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
    let out = decide(&state, &request("2026-10-30", Side::Buy), Some(&evidence("2026-10-30"))).unwrap();
    let fill = out.fill.unwrap();
    assert_eq!(fill.quantity % 100, 0);
    assert!(fill.quantity > 0);
    assert!(fill.quantity * fill.price_cents + fill.fees_cents <= 1_000_000);
}

#[test]
fn t_plus_one_blocks_sale_on_purchase_day_then_allows_next_day() {
    let mut state = PortfolioState::new(INITIAL_CASH_CENTS);
    let buy = decide(&state, &request("2026-10-30", Side::Buy), Some(&evidence("2026-10-30"))).unwrap().fill.unwrap();
    state.apply(&buy).unwrap();
    assert_eq!(decide(&state, &request("2026-10-30", Side::Sell), Some(&evidence("2026-10-30"))).unwrap().reason,
               "no_t1_sellable_lot");
    let sell = decide(&state, &request("2026-11-02", Side::Sell), Some(&evidence("2026-11-02"))).unwrap();
    assert_eq!(sell.status, DecisionStatus::Filled);
    state.apply(sell.fill.as_ref().unwrap()).unwrap();
    assert_eq!(state.quantity("600000.SH"), 0);
}

#[test]
fn stale_future_and_limit_up_quotes_fail_closed() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let mut quote = evidence("2026-10-30");
    quote.quote.observed_at = "2026-10-30T09:50:00+08:00".into();
    assert_eq!(decide(&state, &req, Some(&quote)).unwrap().reason, "stale_quote");
    quote.quote.observed_at = "2026-10-30T10:00:01+08:00".into();
    quote.available_at = quote.quote.observed_at.clone();
    assert_eq!(decide(&state, &req, Some(&quote)).unwrap().reason, "quote_from_future");
    quote = evidence("2026-10-30");
    quote.quote.limit_up = true;
    assert_eq!(decide(&state, &req, Some(&quote)).unwrap().reason, "limit_up_buy_blocked");
}

#[test]
fn evidence_availability_has_an_exact_boundary_and_repeats_deterministically() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let mut quote = evidence("2026-10-30");
    assert_eq!(decide(&state, &req, None).unwrap().reason, "quote_evidence_missing");
    quote.available_at = req.decision_at.clone();
    let filled = decide(&state, &req, Some(&quote)).unwrap();
    assert_eq!(filled.status, DecisionStatus::Filled);
    assert_eq!(filled, decide(&state, &req, Some(&quote)).unwrap());
    quote.available_at = "2026-10-30T10:00:00.001+08:00".into();
    let rejected = decide(&state, &req, Some(&quote)).unwrap();
    assert_eq!(rejected.reason, "quote_not_yet_available");
    assert_eq!(rejected, decide(&state, &req, Some(&quote)).unwrap());
    assert!(rejected.fill.is_none());
    assert_eq!(state.cash_cents, rejected.cash_after_cents);
}

#[test]
fn invalid_evidence_cannot_be_consumed_even_by_direct_domain_callers() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let good = evidence("2026-10-30");
    let mut variants = Vec::new();
    let mut bad = good.clone();
    bad.quote.source = "unverified-provider".into();
    variants.push(bad);
    let mut bad = good.clone();
    bad.schema_version = 99;
    variants.push(bad);
    let mut bad = good.clone();
    bad.quote.price_cents = 0;
    variants.push(bad);
    let mut bad = good.clone();
    bad.available_at = "2026-10-30T09:59:29+08:00".into();
    variants.push(bad);
    let mut bad = good.clone();
    bad.available_at = "2026-10-30T09:59:31+00:00".into();
    variants.push(bad);
    let mut bad = good.clone();
    bad.quote.quote_id = "different-id".into();
    variants.push(bad);
    let mut bad = good;
    bad.quote.symbol = "000001.SZ".into();
    variants.push(bad);
    for bad in variants {
        for _ in 0..2 { assert!(decide(&state, &req, Some(&bad)).is_err()); }
    }
}

#[test]
fn false_symbol_wrong_timezone_and_old_contract_are_rejected() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let mut req = request("2026-10-30", Side::Buy);
    let quote = evidence("2026-10-30");
    req.signal.symbol = "bad".into();
    assert!(decide(&state, &req, Some(&quote)).is_err());
    req = request("2026-10-30", Side::Buy);
    req.decision_at = "2026-10-30T10:00:00+00:00".into();
    assert!(decide(&state, &req, Some(&quote)).is_err());
    req = request("2026-10-30", Side::Buy);
    req.schema_version = 1;
    assert!(decide(&state, &req, Some(&quote)).is_err());
}

#[test]
fn research_cannot_smuggle_prices_quantities_or_inline_quotes() {
    let req = request("2026-10-30", Side::Buy);
    let raw = serde_json::to_value(&req).unwrap();
    assert!(serde_json::from_value::<SimulationRequest>(raw.clone()).is_ok());
    for field in ["quantity", "shares", "amount"] {
        let mut injected = raw.clone();
        injected["signal"][field] = serde_json::json!(100);
        assert!(serde_json::from_value::<SimulationRequest>(injected).is_err());
    }
    let mut injected = raw.clone();
    injected["quote"] = serde_json::to_value(evidence("2026-10-30").quote).unwrap();
    assert!(serde_json::from_value::<SimulationRequest>(injected).is_err());
    let mut injected = raw;
    injected["quote_ref"]["price_cents"] = serde_json::json!(1);
    assert!(serde_json::from_value::<SimulationRequest>(injected).is_err());
    let mut injected = serde_json::to_value(evidence("2026-10-30")).unwrap();
    injected["quote"]["override_price_cents"] = serde_json::json!(1);
    assert!(serde_json::from_value::<QuoteEvidence>(injected).is_err());
}

#[test]
fn quote_freshness_preserves_subsecond_precision_at_boundary() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let mut quote = evidence("2026-10-30");
    quote.quote.observed_at = "2026-10-30T09:57:00+08:00".into();
    let boundary = decide(&state, &req, Some(&quote)).unwrap();
    assert_eq!(boundary.status, DecisionStatus::Filled);
    assert_eq!(boundary, decide(&state, &req, Some(&quote)).unwrap());
    quote.quote.observed_at = "2026-10-30T09:56:59.999+08:00".into();
    let stale = decide(&state, &req, Some(&quote)).unwrap();
    assert_eq!(stale.reason, "stale_quote");
    assert!(stale.fill.is_none());
    assert_eq!(stale.cash_after_cents, state.cash_cents);
    assert_eq!(stale, decide(&state, &req, Some(&quote)).unwrap());
}
