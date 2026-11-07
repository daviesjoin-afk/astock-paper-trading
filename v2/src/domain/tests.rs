use super::*;

fn calendar() -> TradingCalendar {
    serde_json::from_str(BUNDLED_CALENDAR_JSON).unwrap()
}

fn instruments() -> InstrumentSnapshot {
    serde_json::from_str(BUNDLED_INSTRUMENTS_JSON).unwrap()
}

fn sessions() -> SessionPolicy {
    serde_json::from_str(BUNDLED_SESSION_POLICY_JSON).unwrap()
}


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
        schema_version: 4, instrument: instruments().identity("600000.SH").cloned(), available_at: format!("{day}T09:59:31+08:00"),
        trust_level: QuoteTrust::OfflineFixture, provider_published_at: None,
        received_at: "2026-11-04T10:00:00+08:00".into(),
        execution: Some(ExecutionLiquidity {
            schema_version: 1,
            quote_ref: QuoteRef { source: "offline-fixture".into(), quote_id: "q-1".into() },
            order_side: Side::Buy, price_cents: 1234, available_quantity: 800,
            observed_at: format!("{day}T09:59:30+08:00"),
        }),
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
    let out = decide(&state, &request("2026-10-30", Side::Buy), Some(&evidence("2026-10-30")), &calendar(), &sessions(), &instruments()).unwrap();
    let fill = out.fill.unwrap();
    assert_eq!(fill.quantity % 100, 0);
    assert!(fill.quantity > 0);
    assert!(fill.quantity * fill.price_cents + fill.fees_cents <= 1_000_000);
}

#[test]
fn reference_quote_without_execution_evidence_cannot_fill() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let mut raw = serde_json::to_value(evidence("2026-10-30")).unwrap();
    raw.as_object_mut().unwrap().remove("execution");
    let reference_only: QuoteEvidence = serde_json::from_value(raw).unwrap();
    for _ in 0..2 {
        let out = decide(&state, &req, Some(&reference_only), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out.status, DecisionStatus::Rejected);
        assert_eq!(out.reason, "execution_evidence_missing");
        assert!(out.fill.is_none());
        assert_eq!(out.cash_after_cents, INITIAL_CASH_CENTS);
    }
}

#[test]
fn execution_price_and_capacity_bound_the_whole_candidate() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    for capacity in [0, 35, 600, 699, 700, 701] {
        let mut quote = evidence("2026-10-30");
        let level = quote.execution.as_mut().unwrap();
        level.price_cents = 1250; // Reference remains 1234; budget yields 700.
        level.available_quantity = capacity;
        let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
        if capacity < 700 {
            assert_eq!(out.reason, "insufficient_execution_liquidity");
            assert_eq!(out.status, DecisionStatus::Rejected);
            assert!(out.fill.is_none());
            assert_eq!(out.cash_after_cents, state.cash_cents);
        } else {
            let fill = out.fill.unwrap();
            assert_eq!(out.status, DecisionStatus::Filled);
            assert_eq!((fill.price_cents, fill.quantity), (1250, 700));
        }
    }
    let buy = decide(&state, &req, Some(&evidence("2026-10-30")), &calendar(), &sessions(), &instruments()).unwrap().fill.unwrap();
    let mut held = state;
    held.apply(&buy).unwrap();
    for capacity in [799, 800] {
        let mut quote = evidence("2026-11-02");
        let level = quote.execution.as_mut().unwrap();
        level.order_side = Side::Sell;
        level.price_cents = 1300;
        level.available_quantity = capacity;
        let sell = request("2026-11-02", Side::Sell);
        let out = decide(&held, &sell, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out, decide(&held, &sell, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
        if capacity == 799 {
            assert_eq!(out.reason, "insufficient_execution_liquidity");
            assert!(out.fill.is_none());
        } else {
            let fill = out.fill.unwrap();
            assert_eq!((fill.price_cents, fill.quantity), (1300, 800));
        }
    }
}

#[test]
fn execution_direction_visibility_and_precise_age_fail_closed() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let mut wrong_side = evidence("2026-10-30");
    wrong_side.execution.as_mut().unwrap().order_side = Side::Sell;
    let mut boundary = evidence("2026-10-30");
    boundary.execution.as_mut().unwrap().observed_at = "2026-10-30T09:57:00+08:00".into();
    let mut stale = boundary.clone();
    stale.execution.as_mut().unwrap().observed_at = "2026-10-30T09:56:59.999+08:00".into();
    let mut yesterday = boundary.clone();
    yesterday.execution.as_mut().unwrap().observed_at = "2026-10-29T10:00:00+08:00".into();
    let mut future = boundary.clone();
    future.available_at = "2026-10-30T10:00:00.001+08:00".into();
    future.execution.as_mut().unwrap().observed_at = future.available_at.clone();
    for (quote, reason) in [
        (wrong_side, "execution_side_mismatch"), (stale, "stale_execution_evidence"),
        (yesterday, "stale_execution_evidence"), (future, "quote_not_yet_available"),
    ] {
        for _ in 0..2 {
            let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
            assert_eq!(out.reason, reason);
            assert_eq!(out.status, DecisionStatus::Rejected);
            assert!(out.fill.is_none());
            assert_eq!(out.cash_after_cents, state.cash_cents);
        }
    }
    let out = decide(&state, &req, Some(&boundary), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(out.status, DecisionStatus::Filled);
    assert_eq!(out, decide(&state, &req, Some(&boundary), &calendar(), &sessions(), &instruments()).unwrap());
}

#[test]
fn malformed_execution_cannot_bypass_direct_domain_validation() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let good = evidence("2026-10-30");
    for field in ["schema_version", "quote_ref", "order_side", "price_cents", "available_quantity", "observed_at"] {
        let mut raw = serde_json::to_value(&good).unwrap();
        raw["execution"].as_object_mut().unwrap().remove(field);
        assert!(serde_json::from_value::<QuoteEvidence>(raw).is_err(), "{field}");
    }
    for field in ["received_at", "trust_level", "provider_request_id"] {
        let mut raw = serde_json::to_value(&good).unwrap();
        raw["execution"][field] = serde_json::json!("forged");
        assert!(serde_json::from_value::<QuoteEvidence>(raw).is_err());
    }
    let mut variants = Vec::new();
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().schema_version = 99; variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().quote_ref.quote_id = "other".into(); variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().quote_ref.source = "provider".into(); variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().price_cents = 0; variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().available_quantity = -1; variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().observed_at = "2026-10-30T10:00:00+00:00".into(); variants.push(bad);
    let mut bad = good; bad.execution.as_mut().unwrap().observed_at = "2026-10-30T09:59:32+08:00".into(); variants.push(bad);
    for bad in variants {
        for _ in 0..2 { assert!(decide(&state, &req, Some(&bad), &calendar(), &sessions(), &instruments()).is_err()); }
    }
}

#[test]
fn t_plus_one_blocks_sale_on_purchase_day_then_allows_next_day() {
    let mut state = PortfolioState::new(INITIAL_CASH_CENTS);
    let buy = decide(&state, &request("2026-10-30", Side::Buy), Some(&evidence("2026-10-30")), &calendar(), &sessions(), &instruments()).unwrap().fill.unwrap();
    state.apply(&buy).unwrap();
    let mut today = evidence("2026-10-30");
    today.execution.as_mut().unwrap().order_side = Side::Sell;
    assert_eq!(decide(&state, &request("2026-10-30", Side::Sell), Some(&today), &calendar(), &sessions(), &instruments()).unwrap().reason,
               "no_t1_sellable_lot");
    let mut tomorrow = evidence("2026-11-02");
    tomorrow.execution.as_mut().unwrap().order_side = Side::Sell;
    let sell = decide(&state, &request("2026-11-02", Side::Sell), Some(&tomorrow), &calendar(), &sessions(), &instruments()).unwrap();
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
    assert_eq!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap().reason, "stale_quote");
    quote.quote.observed_at = "2026-10-30T10:00:01+08:00".into();
    quote.available_at = quote.quote.observed_at.clone();
    assert_eq!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap().reason, "quote_from_future");
    quote = evidence("2026-10-30");
    quote.quote.limit_up = true;
    assert_eq!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap().reason, "limit_up_buy_blocked");
}

#[test]
fn evidence_availability_has_an_exact_boundary_and_repeats_deterministically() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let mut quote = evidence("2026-10-30");
    assert_eq!(decide(&state, &req, None, &calendar(), &sessions(), &instruments()).unwrap().reason, "quote_evidence_missing");
    quote.available_at = req.decision_at.clone();
    let filled = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(filled.status, DecisionStatus::Filled);
    assert_eq!(filled, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
    quote.available_at = "2026-10-30T10:00:00.001+08:00".into();
    let rejected = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(rejected.reason, "quote_not_yet_available");
    assert_eq!(rejected, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
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
        for _ in 0..2 { assert!(decide(&state, &req, Some(&bad), &calendar(), &sessions(), &instruments()).is_err()); }
    }
}

#[test]
fn false_symbol_wrong_timezone_and_old_contract_are_rejected() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let mut req = request("2026-10-30", Side::Buy);
    let quote = evidence("2026-10-30");
    req.signal.symbol = "bad".into();
    assert!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).is_err());
    req = request("2026-10-30", Side::Buy);
    req.decision_at = "2026-10-30T10:00:00+00:00".into();
    assert!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).is_err());
    req = request("2026-10-30", Side::Buy);
    req.schema_version = 1;
    assert!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).is_err());
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
    let boundary = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(boundary.status, DecisionStatus::Filled);
    assert_eq!(boundary, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
    quote.quote.observed_at = "2026-10-30T09:56:59.999+08:00".into();
    let stale = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(stale.reason, "stale_quote");
    assert!(stale.fill.is_none());
    assert_eq!(stale.cash_after_cents, state.cash_cents);
    assert_eq!(stale, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
}

#[test]
fn offline_trust_never_grants_provider_publication_or_authentication() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    let good = evidence("2026-10-30");
    let expected = decide(&state, &req, Some(&good), &calendar(), &sessions(), &instruments()).unwrap();
    // An offline simulation uses its declared historical availability. Actual
    // receipt is an independent audit fact, never a forged historical receipt.
    let mut later_receipt = good.clone();
    later_receipt.received_at = "2030-01-01T00:00:00+08:00".into();
    assert_eq!(decide(&state, &req, Some(&later_receipt), &calendar(), &sessions(), &instruments()).unwrap(), expected);
    let mut forged_publication = good.clone();
    forged_publication.provider_published_at = Some(good.quote.observed_at.clone());
    for _ in 0..2 {
        assert!(decide(&state, &req, Some(&forged_publication), &calendar(), &sessions(), &instruments()).is_err());
    }
    let mut invalid_receipt = good.clone();
    invalid_receipt.received_at = "not-a-receipt-time".into();
    assert!(decide(&state, &req, Some(&invalid_receipt), &calendar(), &sessions(), &instruments()).is_err());
    for grade in ["authenticated_provider", "verified", "live"] {
        let mut raw = serde_json::to_value(&good).unwrap();
        raw["trust_level"] = serde_json::json!(grade);
        assert!(serde_json::from_value::<QuoteEvidence>(raw).is_err());
    }
    assert_eq!(decide(&state, &req, Some(&good), &calendar(), &sessions(), &instruments()).unwrap(), expected);
}

fn market_request(day: &str, time: &str) -> (SimulationRequest, QuoteEvidence) {
    let mut req = request(day, Side::Buy);
    req.decision_at = format!("{day}T{time}+08:00");
    let at = chrono::DateTime::parse_from_rfc3339(&req.decision_at).unwrap();
    let mut quote = evidence(day);
    quote.quote.observed_at = (at - chrono::Duration::seconds(30)).to_rfc3339();
    quote.available_at = (at - chrono::Duration::seconds(29)).to_rfc3339();
    quote.execution.as_mut().unwrap().observed_at = quote.quote.observed_at.clone();
    (req, quote)
}

fn assert_market_rejection(day: &str, time: &str, reason: &str) {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let (req, quote) = market_request(day, time);
    let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(out.status, DecisionStatus::Rejected, "{day} {time}");
    assert_eq!(out.reason, reason);
    assert!(out.fill.is_none());
    assert_eq!(out.cash_after_cents, INITIAL_CASH_CENTS);
    assert_eq!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap(), out);
}

#[test]
fn weekend_quotes_cannot_fill() {
    for day in ["2026-10-31", "2026-11-01"] {
        assert_market_rejection(day, "10:00:00", "weekend_market_closed");
    }
}

#[test]
fn published_holidays_cannot_fill() {
    for day in ["2026-01-01", "2026-02-23", "2026-10-01"] {
        assert_market_rejection(day, "10:00:00", "holiday_market_closed");
    }
}

#[test]
fn unknown_calendar_dates_cannot_fill() {
    for day in ["2025-12-31", "2027-01-04"] {
        assert_market_rejection(day, "10:00:00", "unknown_trading_date");
    }
}

#[test]
fn midday_boundary_is_not_rounded_to_minutes() {
    for time in ["11:30:00.001", "11:30:59", "12:59:59.999"] {
        assert_market_rejection("2026-10-30", time, "midday_break");
    }
}

#[test]
fn after_close_boundary_is_not_rounded_to_minutes() {
    for time in ["15:00:00.001", "15:00:59", "15:05:00"] {
        assert_market_rejection("2026-10-30", time, "outside_trading_session");
    }
}

#[test]
fn auctions_are_explicitly_unsupported() {
    for time in ["09:15:00", "09:25:00"] {
        assert_market_rejection("2026-10-30", time, "opening_auction_not_supported");
    }
    for time in ["14:57:00", "14:59:59.999", "15:00:00"] {
        assert_market_rejection("2026-10-30", time, "closing_auction_not_supported");
    }
}

#[test]
fn known_continuous_session_boundaries_are_positive_controls() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    for time in ["09:30:00", "11:30:00", "13:00:00", "14:56:59.999"] {
        let (req, quote) = market_request("2026-10-30", time);
        let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out.status, DecisionStatus::Filled, "{time}");
        assert_eq!(decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap(), out);
    }
}

#[test]
fn market_versions_and_windows_cannot_be_overridden_by_research() {
    let (req, quote) = market_request("2026-10-30", "10:00:00");
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    for field in ["calendar_version", "session_policy_version", "calendar", "sessions"] {
        let mut raw = serde_json::to_value(&req).unwrap();
        raw[field] = serde_json::json!("override");
        assert!(serde_json::from_value::<SimulationRequest>(raw).is_err());
    }
    let mut bad = sessions();
    bad.version = "latest".into();
    assert!(decide(&state, &req, Some(&quote), &calendar(), &bad, &instruments()).is_err());
    bad = sessions();
    bad.continuous[0].end = "invalid-time".into();
    assert!(decide(&state, &req, Some(&quote), &calendar(), &bad, &instruments()).is_err());
    bad = sessions();
    bad.continuous[1].end_inclusive = true;
    assert!(bad.validate().is_err());
    bad = sessions();
    bad.continuous[0].end = "13:01:00".into();
    assert!(bad.validate().is_err());
}

#[test]
fn instrument_identity_is_mandatory_and_format_is_not_qualification() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    for symbol in ["600000.SH", "600001.SH", "688999.SH", "513151.SH", "600000.SZ", "000001.SZ"] {
        let mut req = request("2026-10-30", Side::Buy);
        req.signal.symbol = symbol.into();
        let mut quote = evidence("2026-10-30");
        quote.quote.symbol = symbol.into();
        quote.instrument = None;
        for _ in 0..2 {
            let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
            assert_eq!(out.reason, "instrument_identity_missing", "{symbol}");
            assert!(out.fill.is_none());
            assert_eq!(out.cash_after_cents, INITIAL_CASH_CENTS);
        }
    }
}

#[test]
fn conflicting_identity_cannot_relabel_a_quote_or_its_rule() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    for (field, value) in [
        ("schema_version", serde_json::json!(99)),
        ("evidence_version", serde_json::json!("latest")),
        ("symbol", serde_json::json!("688001.SH")),
        ("exchange", serde_json::json!("szse")),
        ("board", serde_json::json!("star")),
        ("security_type", serde_json::json!("etf")),
        ("quantity_rule_version", serde_json::Value::Null),
        ("source_ids", serde_json::json!(["fabricated"])),
        ("valid_through", serde_json::json!("2030-12-31")),
    ] {
        let mut raw = serde_json::to_value(evidence("2026-10-30")).unwrap();
        raw["instrument"][field] = value;
        let quote = serde_json::from_value(raw).unwrap();
        let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out.reason, "instrument_identity_conflict", "{field}");
        assert_eq!(out, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
        assert!(out.fill.is_none());
    }
    assert_eq!(decide(&state, &req, Some(&evidence("2026-10-30")), &calendar(), &sessions(), &instruments()).unwrap().status, DecisionStatus::Filled);
}

#[test]
fn instrument_coverage_is_bounded_and_deterministic() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    for (day, expected) in [("2026-10-26", "instrument_date_not_covered"),
        ("2026-10-27", "filled"), ("2026-11-05", "filled"), ("2026-11-06", "instrument_date_not_covered")]
    {
        let req = request(day, Side::Buy);
        let quote = evidence(day);
        let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out.reason, expected);
        assert_eq!(out, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
        assert_eq!(out.fill.is_some(), expected == "filled");
    }
}

#[test]
fn missing_unknown_and_out_of_date_quantity_rules_fail_closed() {
    let state = PortfolioState::new(INITIAL_CASH_CENTS);
    let req = request("2026-10-30", Side::Buy);
    for case in ["missing-reference", "missing-rule", "unknown-rule", "not-yet-effective", "expired"] {
        let mut snapshot = instruments();
        match case {
            "missing-reference" => snapshot.instruments[0].quantity_rule_version = None,
            "missing-rule" => snapshot.quantity_rules.clear(),
            "unknown-rule" => snapshot.instruments[0].quantity_rule_version = Some("unimplemented".into()),
            "not-yet-effective" => snapshot.quantity_rules[0].effective_from = "2026-11-01".into(),
            _ => snapshot.quantity_rules[0].covered_through = "2026-10-29".into(),
        }
        let mut quote = evidence("2026-10-30");
        quote.instrument = snapshot.identity("600000.SH").cloned();
        let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &snapshot).unwrap();
        let reason = match case {
            "missing-reference" | "missing-rule" => "instrument_rule_missing",
            "unknown-rule" => "unsupported_instrument_rule",
            _ => "instrument_rule_date_not_covered",
        };
        assert_eq!(out.reason, reason);
        assert_eq!(out, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &snapshot).unwrap());
        assert!(out.fill.is_none());
    }
}

#[test]
fn supported_main_board_quantity_minimum_step_and_maximum_are_enforced() {
    let req = request("2026-10-30", Side::Buy);
    for (cash, price, quantity, reason) in [
        (INITIAL_CASH_CENTS, 9995, Some(100), "filled"),
        (INITIAL_CASH_CENTS, 9996, None, "insufficient_budget_for_board_lot_and_fees"),
        (10_005_000, 1, Some(1_000_000), "filled"),
        (10_006_000, 1, None, "order_quantity_exceeds_instrument_limit"),
    ] {
        let state = PortfolioState::new(cash);
        let mut quote = evidence("2026-10-30");
        let level = quote.execution.as_mut().unwrap();
        level.price_cents = price;
        level.available_quantity = 2_000_000;
        let out = decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
        assert_eq!(out.reason, reason);
        assert_eq!(out.fill.as_ref().map(|fill| fill.quantity), quantity);
        assert_eq!(out, decide(&state, &req, Some(&quote), &calendar(), &sessions(), &instruments()).unwrap());
        if let Some(fill) = out.fill { assert_eq!(fill.quantity % 100, 0); }
    }
    let mut state = PortfolioState::new(INITIAL_CASH_CENTS);
    state.lots.push(Lot { symbol: "600000.SH".into(), quantity: 150, bought_on: "2026-10-29".into() });
    let mut quote = evidence("2026-10-30");
    quote.execution.as_mut().unwrap().order_side = Side::Sell;
    let out = decide(&state, &request("2026-10-30", Side::Sell), Some(&quote), &calendar(), &sessions(), &instruments()).unwrap();
    assert_eq!(out.reason, "unsupported_odd_lot_position");
    assert!(out.fill.is_none());
}

#[test]
fn malformed_or_conflicting_instrument_snapshots_never_execute() {
    let good = instruments();
    let mut variants = Vec::new();
    let mut bad = good.clone(); bad.version = "latest".into(); variants.push(bad);
    let mut bad = good.clone(); bad.coverage_start = "2026-10-32".into(); variants.push(bad);
    let mut bad = good.clone(); bad.instruments.push(bad.instruments[0].clone()); variants.push(bad);
    let mut bad = good.clone(); bad.instruments[0].exchange = Exchange::Szse; variants.push(bad);
    let mut bad = good.clone(); bad.instruments[0].source_ids.clear(); variants.push(bad);
    let mut bad = good.clone(); bad.quantity_rules[0].minimum = 1; variants.push(bad);
    let mut bad = good.clone(); bad.sources[0].sha256.clear(); variants.push(bad);
    let mut bad = good; bad.sources[0].published_on = "2026-12-01".into(); variants.push(bad);
    for snapshot in variants {
        for _ in 0..2 {
            assert!(decide(&PortfolioState::new(INITIAL_CASH_CENTS), &request("2026-10-30", Side::Buy),
                Some(&evidence("2026-10-30")), &calendar(), &sessions(), &snapshot).is_err());
        }
    }
}

#[test]
fn explicitly_unknown_classifications_never_inherit_supported_parameters() {
    // Deliberately altered test inputs, not claims about the real 600000 identity.
    // Store cannot replace its bundled snapshot with these inputs.
    for case in ["unknown-type", "other-type", "unknown-board", "unknown-exchange"] {
        let mut snapshot = instruments();
        let identity = &mut snapshot.instruments[0];
        match case {
            "unknown-type" => identity.security_type = SecurityType::Unknown,
            "other-type" => identity.security_type = SecurityType::Other,
            "unknown-board" => identity.board = Board::Unknown,
            _ => identity.exchange = Exchange::Unknown,
        }
        let mut quote = evidence("2026-10-30");
        quote.instrument = snapshot.identity("600000.SH").cloned();
        for _ in 0..2 {
            let out = decide(&PortfolioState::new(INITIAL_CASH_CENTS), &request("2026-10-30", Side::Buy),
                Some(&quote), &calendar(), &sessions(), &snapshot).unwrap();
            assert_eq!(out.status, DecisionStatus::Rejected);
            assert!(out.reason.starts_with("unsupported_"));
            assert!(out.fill.is_none());
        }
    }
    for (field, value) in [("board", "future_board"), ("exchange", "future_exchange"), ("security_type", "future_type")] {
        let mut raw = serde_json::to_value(evidence("2026-10-30")).unwrap();
        raw["instrument"][field] = serde_json::json!(value);
        assert!(serde_json::from_value::<QuoteEvidence>(raw).is_err());
    }
}
