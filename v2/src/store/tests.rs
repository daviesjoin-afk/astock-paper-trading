use super::*;
use crate::domain::{Side, Signal, Quote, ExecutionLiquidity, INITIAL_CASH_CENTS, DecisionStatus};

fn request(day: &str, signal_id: &str, side: Side) -> SimulationRequest {
    SimulationRequest {
        schema_version: 2, cycle_id: "cycle-1".into(),
        strategy_version_id: "strategy-v1".into(), signal_id: signal_id.into(),
        decision_at: format!("{day}T10:00:00+08:00"),
        signal: Signal { symbol: "600000.SH".into(), side },
        quote_ref: QuoteRef {
            source: "offline-fixture".into(), quote_id: format!("quote-{signal_id}"),
        },
    }
}

fn evidence(req: &SimulationRequest) -> QuoteImport {
    let day = &req.decision_at[..10];
    QuoteImport {
        schema_version: 1, available_at: format!("{day}T09:59:31+08:00"),
        execution: Some(ExecutionLiquidity {
            schema_version: 1, quote_ref: req.quote_ref.clone(),
            order_side: req.signal.side.clone(), price_cents: 1234, available_quantity: 800,
            observed_at: format!("{day}T09:59:30+08:00"),
        }),
        quote: Quote {
            symbol: req.signal.symbol.clone(), price_cents: 1234,
            observed_at: format!("{day}T09:59:30+08:00"),
            source: req.quote_ref.source.clone(), quote_id: req.quote_ref.quote_id.clone(),
            tradable: true, limit_up: false, limit_down: false,
        },
    }
}

fn ingest_and_submit(db: &mut Store, req: SimulationRequest) -> RunRecord {
    db.ingest_quote(evidence(&req)).unwrap();
    db.submit(req).unwrap()
}

// Fixed classifications and primary-source locators, not code-prefix inference.
// The price/capacity below are deliberately synthetic offline inputs.
fn assert_instrument_rejected(symbol: &str, reason: &str) {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let mut req = request("2026-10-30", "instrument-red", Side::Buy);
    req.signal.symbol = symbol.into();
    let mut input = evidence(&req);
    input.execution.as_mut().unwrap().price_cents = 6000;
    db.ingest_quote(input).unwrap();
    let first = db.submit(req.clone()).unwrap();
    if let Some(fill) = &first.outcome.fill {
        // Proves the old policy actually bought 100, rather than merely parsing.
        assert_eq!(fill.quantity, 100);
    }
    assert_eq!(first.outcome.status, DecisionStatus::Rejected);
    assert_eq!(first.outcome.reason, reason);
    assert!(first.outcome.fill.is_none());
    assert_eq!(first.outcome.cash_after_cents, INITIAL_CASH_CENTS);
    assert!(db.submit(req).unwrap().duplicate);
    assert_eq!(db.state(None).unwrap().runs_total, 1);
    assert!(db.state(None).unwrap().positions.is_empty());
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
}

#[test]
fn star_identity_cannot_use_a_100_share_main_board_fill() {
    let sample: serde_json::Value = serde_json::from_str(include_str!("../../data/instruments-offline-v1.json")).unwrap();
    let star = &sample["instruments"][1];
    assert_eq!(star["board"], "star");
    assert_instrument_rejected(star["symbol"].as_str().unwrap(), "unsupported_instrument_board");
}

#[test]
fn etf_identity_cannot_pass_as_an_ordinary_a_share() {
    let sample: serde_json::Value = serde_json::from_str(include_str!("../../data/instruments-offline-v1.json")).unwrap();
    let etf = &sample["instruments"][2];
    assert_eq!(etf["security_type"], "etf");
    assert_instrument_rejected(etf["symbol"].as_str().unwrap(), "unsupported_security_type");
}

#[test]
fn valid_code_format_without_identity_and_rules_cannot_fill() {
    assert_instrument_rejected("999999.SH", "instrument_identity_missing");
}

#[test]
fn reference_only_import_cannot_write_a_fill_and_replays_its_rejection() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "reference-only", Side::Buy);
    let mut raw = serde_json::to_value(evidence(&req)).unwrap();
    raw.as_object_mut().unwrap().remove("execution");
    db.ingest_quote(serde_json::from_value(raw).unwrap()).unwrap();
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.status, DecisionStatus::Rejected);
    assert_eq!(first.outcome.reason, "execution_evidence_missing");
    assert!(first.outcome.fill.is_none());
    for _ in 0..2 {
        let retry = db.submit(req.clone()).unwrap();
        assert!(retry.duplicate);
        assert_eq!(retry.outcome, first.outcome);
        let view = db.state(Some("2026-10-30")).unwrap();
        assert_eq!(view.cash_cents, INITIAL_CASH_CENTS);
        assert!(view.positions.is_empty());
        assert_eq!(view.runs_total, 1);
        let replay = db.verify_replay().unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (1, 0));
    }
}

#[test]
fn execution_import_is_strict_and_all_execution_fields_are_immutable() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "execution-import", Side::Buy);
    let good = evidence(&req);
    let changes = db.conn.total_changes();
    for field in ["quote_ref", "order_side", "price_cents", "available_quantity", "observed_at"] {
        let mut raw = serde_json::to_value(&good).unwrap();
        raw["execution"].as_object_mut().unwrap().remove(field);
        assert!(serde_json::from_value::<QuoteImport>(raw).is_err());
    }
    for field in ["received_at", "trust_level", "provider_response_id"] {
        let mut raw = serde_json::to_value(&good).unwrap();
        raw["execution"][field] = serde_json::json!("forged");
        assert!(serde_json::from_value::<QuoteImport>(raw).is_err());
    }
    let mut invalid = Vec::new();
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().schema_version = 0; invalid.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().price_cents = 0; invalid.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().available_quantity = -1; invalid.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().quote_ref.quote_id = "mismatch".into(); invalid.push(bad);
    for bad in invalid {
        for _ in 0..2 { assert!(db.ingest_quote(bad.clone()).is_err()); }
    }
    assert_eq!(db.conn.total_changes(), changes);
    let saved = db.ingest_quote(good.clone()).unwrap();
    assert_eq!(saved.schema_version, 3);
    assert_eq!(saved.execution, good.execution);
    assert_eq!(db.ingest_quote(good.clone()).unwrap(), saved);
    let mut variants = Vec::new();
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().price_cents += 1; variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().available_quantity += 1; variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().order_side = Side::Sell; variants.push(bad);
    let mut bad = good.clone(); bad.execution.as_mut().unwrap().observed_at = "2026-10-30T09:59:29+08:00".into(); variants.push(bad);
    let mut bad = good; bad.execution = None; variants.push(bad);
    for bad in variants {
        for _ in 0..2 { assert!(matches!(db.ingest_quote(bad.clone()), Err(StoreError::Conflict(_)))); }
    }
    assert_eq!(load_evidence(&db.conn, &req.quote_ref).unwrap(), Some(saved));
    assert_eq!(db.state(None).unwrap().runs_total, 0);
}

#[test]
fn insufficient_capacity_rejection_is_pinned_and_a_new_identity_can_fill() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "capacity-short", Side::Buy);
    let mut short = evidence(&req);
    short.execution.as_mut().unwrap().available_quantity = 799;
    db.ingest_quote(short).unwrap();
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.reason, "insufficient_execution_liquidity");
    assert!(first.outcome.fill.is_none());
    assert_eq!(first.outcome.cash_after_cents, INITIAL_CASH_CENTS);
    for _ in 0..2 {
        assert!(db.submit(req.clone()).unwrap().duplicate);
        assert!(matches!(db.ingest_quote(evidence(&req)), Err(StoreError::Conflict(_))));
        assert_eq!(db.state(Some("2026-10-30")).unwrap().cash_cents, INITIAL_CASH_CENTS);
        assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
    }
    let valid = request("2026-10-30", "capacity-full", Side::Buy);
    let filled = ingest_and_submit(&mut db, valid.clone());
    assert_eq!(filled.outcome.fill.as_ref().unwrap().quantity, 800);
    assert!(db.submit(valid).unwrap().duplicate);
    assert_eq!(db.submit(req).unwrap().outcome, first.outcome);
    let report = db.verify_replay().unwrap();
    assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    assert_eq!(db.state(Some("2026-10-30")).unwrap().runs_total, 2);
}

#[test]
fn one_sided_quote_cannot_multiply_fills_across_signals_strategies_or_reopen() {
    let file = DiskDatabase::new();
    let historical;
    let latest;
    let replay;
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let buy = request("2026-10-30", "one-buy", Side::Buy);
        let first = ingest_and_submit(&mut db, buy.clone());
        let mut other = buy.clone();
        other.cycle_id = "other-cycle".into();
        other.strategy_version_id = "other-strategy".into();
        other.signal_id = "other-buy".into();
        assert_eq!(db.submit(other).unwrap().outcome.reason, "position_already_open");
        historical = db.state(Some("2026-10-30")).unwrap();
        let sell = request("2026-11-02", "one-sell", Side::Sell);
        let sold = ingest_and_submit(&mut db, sell.clone());
        let mut other_sell = sell.clone();
        other_sell.cycle_id = "other-cycle".into();
        other_sell.strategy_version_id = "other-strategy".into();
        other_sell.signal_id = "other-sell".into();
        assert_eq!(db.submit(other_sell).unwrap().outcome.reason, "no_t1_sellable_lot");
        let mut tomorrow = buy.clone();
        tomorrow.signal_id = "reuse-next-day".into();
        tomorrow.decision_at = sell.decision_at.clone();
        assert_eq!(db.submit(tomorrow).unwrap().outcome.reason, "stale_quote");
        assert_eq!(db.submit(buy.clone()).unwrap().outcome, first.outcome);
        assert_eq!(db.submit(sell).unwrap().outcome, sold.outcome);
        assert_eq!(db.ingest_quote(evidence(&buy)).unwrap(), first.evidence.unwrap());
        let mut uses = std::collections::BTreeMap::new();
        for run in db.runs(100).unwrap() {
            if let Some(fill) = run.outcome.fill {
                assert!(fill.quantity <= run.evidence.unwrap().execution.unwrap().available_quantity);
                *uses.entry((run.request.quote_ref.source, run.request.quote_ref.quote_id)).or_insert(0) += 1;
            }
        }
        assert_eq!(uses.len(), 2);
        assert!(uses.values().all(|&count| count == 1));
        latest = db.state(None).unwrap();
        assert!(latest.positions.is_empty());
        assert_eq!(db.state(Some("2026-10-30")).unwrap(), historical);
        replay = db.verify_replay().unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (5, 2));
    }
    let db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        assert_eq!(db.state(Some("2026-10-30")).unwrap(), historical);
        assert_eq!(db.state(None).unwrap(), latest);
        assert_eq!(db.verify_replay().unwrap(), replay);
    }
}

#[test]
fn changed_execution_evidence_is_detected_without_rewriting_facts() {
    for (field, value) in [
        ("price_cents", serde_json::json!(1250)), ("available_quantity", serde_json::json!(799)),
        ("order_side", serde_json::json!("sell")), ("observed_at", serde_json::json!("2026-10-30T09:59:29+08:00")),
    ] {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "tamper-execution", Side::Buy);
        let first = ingest_and_submit(&mut db, req);
        let mut raw = serde_json::to_value(first.evidence.unwrap()).unwrap();
        raw["execution"][field] = value;
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [raw.to_string()]).unwrap();
        let changes = db.conn.total_changes();
        for _ in 0..2 { assert!(db.verify_replay().is_err()); }
        assert_eq!(db.conn.total_changes(), changes);
        assert_eq!(db.state(None).unwrap().runs_total, 1);
    }
}

#[test]
fn prior_reference_price_policy_file_is_refused_without_disk_mutation() {
    let file = DiskDatabase::new();
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "old-policy", Side::Buy));
        db.conn.execute("UPDATE v2_meta SET value='v2-paper-policy-4' WHERE key='policy_version'", []).unwrap();
    }
    Connection::open(file.path()).unwrap().execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
    let before = disk_snapshot(&file);
    for _ in 0..2 {
        assert!(Store::open(file.path(), INITIAL_CASH_CENTS).is_err());
        assert_eq!(disk_snapshot(&file), before);
    }
}

#[test]
fn dedup_is_exact_and_does_not_duplicate_fills() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    let first = ingest_and_submit(&mut db, req.clone());
    let second = db.submit(req).unwrap();
    assert!(!first.duplicate);
    assert!(second.duplicate);
    assert_eq!(first.id, second.id);
    assert_eq!(first.evidence, second.evidence);
    assert_eq!(first.outcome, second.outcome);
    let view = db.state(Some("2026-10-30")).unwrap();
    assert_eq!(view.runs_total, 1);
    assert_eq!(view.positions.len(), 1);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
    assert_eq!(db.runs(10).unwrap()[0], first);
}

#[test]
fn conflicting_payload_under_same_identity_is_rejected() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-10-30", "b1", Side::Buy));
    let mut changed = request("2026-10-30", "b1", Side::Buy);
    changed.quote_ref.quote_id = "another-price".into();
    assert!(matches!(db.submit(changed), Err(StoreError::Conflict(_))));
    assert_eq!(db.state(None).unwrap().runs_total, 1);
}

#[test]
fn replay_detects_tampered_fill() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-10-30", "b1", Side::Buy));
    assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
    db.conn.execute("UPDATE v2_fills SET fill_json='{}' WHERE id=1", []).unwrap();
    assert!(db.verify_replay().is_err());
}

#[test]
fn out_of_order_decision_does_not_mutate_the_ledger() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-11-02", "later", Side::Buy));
    let earlier = request("2026-10-30", "earlier", Side::Sell);
    assert!(matches!(db.submit(earlier), Err(StoreError::Conflict(_))));
    assert_eq!(db.state(None).unwrap().runs_total, 1);
    assert_eq!(db.verify_replay().unwrap().runs_verified, 1);
}

#[test]
fn rejection_is_persisted_without_fill() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let out = ingest_and_submit(&mut db, request("2026-11-02", "s1", Side::Sell));
    assert_eq!(out.outcome.status, DecisionStatus::Rejected);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
}

#[test]
fn buy_and_next_day_sell_replay() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-10-30", "b1", Side::Buy));
    let blocked = ingest_and_submit(&mut db, request("2026-10-30", "s0", Side::Sell));
    assert_eq!(blocked.outcome.reason, "no_t1_sellable_lot");
    let sold = ingest_and_submit(&mut db, request("2026-11-02", "s1", Side::Sell));
    assert_eq!(sold.outcome.status, DecisionStatus::Filled);
    assert_eq!(db.state(Some("2026-11-02")).unwrap().positions.len(), 0);
    let verified = db.verify_replay().unwrap();
    assert_eq!((verified.runs_verified, verified.fills_verified), (3, 2));
}

#[test]
fn historical_state_excludes_future_buys_sells_and_decisions() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let empty = db.state(Some("2026-10-29")).unwrap();
    let buy = request("2026-10-30", "pit-buy", Side::Buy);
    let bought = ingest_and_submit(&mut db, buy.clone());
    let buy_day = db.state(Some("2026-10-30")).unwrap();
    assert_eq!(buy_day.positions[0].quantity, 800);
    assert_eq!(buy_day.positions[0].sellable_quantity, Some(0));
    assert_eq!(buy_day.cash_cents, bought.outcome.cash_after_cents);
    assert_eq!(buy_day.runs_total, 1);
    let blocked = ingest_and_submit(&mut db, request("2026-10-30", "pit-t0", Side::Sell));
    assert_eq!(blocked.outcome.status, DecisionStatus::Rejected);
    let mut buy_day_with_rejection = buy_day;
    buy_day_with_rejection.runs_total = 2;
    let sold = ingest_and_submit(&mut db, request("2026-11-02", "pit-sell", Side::Sell));
    assert_eq!(sold.outcome.status, DecisionStatus::Filled);
    assert!(db.submit(buy).unwrap().duplicate);
    let changes = db.conn.total_changes();
    let replay = db.verify_replay().unwrap();
    for _ in 0..3 {
        assert_eq!(db.state(Some("2026-10-29")).unwrap(), empty);
        assert_eq!(db.state(Some("2026-10-30")).unwrap(), buy_day_with_rejection);
        let held = db.state(Some("2026-11-01")).unwrap();
        assert_eq!(held.positions[0].quantity, 800);
        assert_eq!(held.positions[0].sellable_quantity, Some(800));
        assert_eq!(held.cash_cents, buy_day_with_rejection.cash_cents);
        assert_eq!(held.runs_total, 2);
        let sell_day = db.state(Some("2026-11-02")).unwrap();
        assert!(sell_day.positions.is_empty() && sell_day.lots.is_empty());
        assert_eq!(sell_day.cash_cents, sold.outcome.cash_after_cents);
        assert_eq!(sell_day.runs_total, 3);
        assert_eq!(db.state(None).unwrap().cash_cents, sell_day.cash_cents);
        assert_eq!(db.verify_replay().unwrap(), replay);
    }
    assert_eq!(db.conn.total_changes(), changes);
}

#[test]
fn historical_state_uses_china_day_end_including_rejected_decisions() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let mut last = request("2026-10-30", "pit-last-nanosecond", Side::Buy);
    last.decision_at = "2026-10-30T23:59:59.999999999+08:00".into();
    // Missing quote is an immutable rejected decision and still counts on its day.
    assert_eq!(db.submit(last).unwrap().outcome.status, DecisionStatus::Rejected);
    let end = db.state(Some("2026-10-30")).unwrap();
    assert_eq!(end.runs_total, 1);
    let mut next = request("2026-10-31", "pit-next-midnight", Side::Buy);
    // This instant is still October 30 in UTC, but the next China date.
    next.decision_at = "2026-10-31T00:00:00+08:00".into();
    db.submit(next).unwrap();
    assert_eq!(db.state(Some("2026-10-30")).unwrap(), end);
    assert_eq!(db.state(Some("2026-10-31")).unwrap().runs_total, 2);
    assert_eq!(db.state(None).unwrap().runs_total, 2);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
}

#[test]
fn historical_state_survives_disk_reopen_without_writes_or_reinterpretation() {
    let file = DiskDatabase::new();
    let historical;
    let latest;
    let replay;
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        historical = {
            ingest_and_submit(&mut db, request("2026-10-30", "pit-disk-buy", Side::Buy));
            db.state(Some("2026-10-30")).unwrap()
        };
        ingest_and_submit(&mut db, request("2026-11-02", "pit-disk-sell", Side::Sell));
        latest = db.state(None).unwrap();
        replay = db.verify_replay().unwrap();
    }
    let before = disk_snapshot(&file);
    {
        let db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let changes = db.conn.total_changes();
        for _ in 0..2 {
            assert_eq!(db.state(Some("2026-10-30")).unwrap(), historical);
            assert_eq!(db.state(None).unwrap(), latest);
            assert_eq!(db.verify_replay().unwrap(), replay);
        }
        assert_eq!(db.conn.total_changes(), changes);
    }
    assert_eq!(disk_snapshot(&file), before);
}

#[test]
fn historical_state_empty_history_and_invalid_dates_are_read_only() {
    let db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let changes = db.conn.total_changes();
    for day in ["2026-10-29", "2025-01-01", "2027-01-01"] {
        let view = db.state(Some(day)).unwrap();
        assert_eq!(view.cash_cents, INITIAL_CASH_CENTS);
        assert!(view.positions.is_empty() && view.lots.is_empty());
        assert_eq!(view.runs_total, 0);
    }
    for day in ["bad", "2026-02-30", "2026-1-1", "2026-10-30T10:00:00+08:00"] {
        for _ in 0..2 { assert!(db.state(Some(day)).is_err(), "{day}"); }
    }
    assert_eq!(db.verify_replay().unwrap().runs_verified, 0);
    assert_eq!(db.conn.total_changes(), changes);
}

#[test]
fn quote_ingestion_is_idempotent_and_every_evidence_field_is_immutable() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    let quote = evidence(&req);
    let stored = db.ingest_quote(quote.clone()).unwrap();
    assert_eq!(stored.quote, quote.quote);
    assert_eq!(stored.available_at, quote.available_at);
    assert_eq!(db.ingest_quote(quote.clone()).unwrap(), stored);
    let mut variants = Vec::new();
    let mut bad = quote.clone();
    bad.quote.price_cents += 1;
    variants.push(bad);
    let mut bad = quote.clone();
    bad.available_at = "2026-10-30T09:59:32+08:00".into();
    variants.push(bad);
    let mut bad = quote.clone();
    bad.quote.observed_at = "2026-10-30T09:59:29+08:00".into();
    variants.push(bad);
    let mut bad = quote.clone();
    bad.quote.tradable = false;
    variants.push(bad);
    let mut bad = quote.clone();
    bad.quote.limit_up = true;
    variants.push(bad);
    let mut bad = quote.clone();
    bad.quote.limit_down = true;
    variants.push(bad);
    let mut bad = quote.clone();
    bad.quote.symbol = "000001.SZ".into();
    variants.push(bad);
    for bad in variants {
        for _ in 0..2 {
            assert!(matches!(db.ingest_quote(bad.clone()), Err(StoreError::Conflict(_))));
        }
    }
    assert_eq!(load_evidence(&db.conn, &req.quote_ref).unwrap(), Some(stored));
    let count: i64 = db.conn.query_row("SELECT COUNT(*) FROM v2_quote_evidence", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 1);
    assert_eq!(db.state(None).unwrap().runs_total, 0);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
    assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
}

#[test]
fn invalid_ingress_never_writes_and_cannot_be_retried_into_success() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let good = evidence(&request("2026-10-30", "b1", Side::Buy));
    let mut variants = Vec::new();
    let mut bad = good.clone();
    bad.quote.source = "claimed-live-provider".into();
    variants.push(bad);
    let mut bad = good.clone();
    bad.schema_version = 2;
    variants.push(bad);
    let mut bad = good.clone();
    bad.quote.price_cents = -1;
    variants.push(bad);
    let mut bad = good.clone();
    bad.available_at = "2026-10-30T09:59:29+08:00".into();
    variants.push(bad);
    let mut bad = good;
    bad.quote.quote_id = "".into();
    variants.push(bad);
    for bad in variants {
        for _ in 0..2 { assert!(db.ingest_quote(bad.clone()).is_err()); }
    }
    let count: i64 = db.conn.query_row("SELECT COUNT(*) FROM v2_quote_evidence", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 0);
    assert_eq!(db.verify_replay().unwrap().runs_verified, 0);
}

#[test]
fn missing_quote_rejection_stays_pinned_after_later_ingestion() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.reason, "quote_evidence_missing");
    assert!(first.evidence.is_none());
    db.ingest_quote(evidence(&req)).unwrap();
    let retry = db.submit(req.clone()).unwrap();
    assert!(retry.duplicate);
    assert_eq!(retry.outcome, first.outcome);
    assert!(retry.evidence.is_none());
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
    let mut next = req;
    next.signal_id = "b2".into();
    assert_eq!(db.submit(next).unwrap().outcome.status, DecisionStatus::Filled);
    for _ in 0..2 {
        let replay = db.verify_replay().unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (2, 1));
    }
}

#[test]
fn late_evidence_rejection_and_exact_availability_boundary_replay() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "late", Side::Buy);
    let mut quote = evidence(&req);
    quote.available_at = "2026-10-30T10:00:00.001+08:00".into();
    db.ingest_quote(quote).unwrap();
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.reason, "quote_not_yet_available");
    assert!(first.evidence.is_some());
    assert_eq!(db.submit(req).unwrap().outcome, first.outcome);
    let on_time = request("2026-10-30", "on-time", Side::Buy);
    let mut quote = evidence(&on_time);
    quote.available_at = on_time.decision_at.clone();
    db.ingest_quote(quote).unwrap();
    assert_eq!(db.submit(on_time).unwrap().outcome.status, DecisionStatus::Filled);
    for _ in 0..2 {
        let report = db.verify_replay().unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

#[test]
fn source_is_part_of_quote_identity_and_cannot_resolve_another_source() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let mut req = request("2026-10-30", "b1", Side::Buy);
    db.ingest_quote(evidence(&req)).unwrap();
    req.quote_ref.source = "forged-provider".into();
    let result = db.submit(req.clone()).unwrap();
    assert_eq!(result.outcome.reason, "quote_evidence_missing");
    assert_eq!(db.submit(req).unwrap().outcome, result.outcome);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
}

#[test]
fn replay_detects_changed_or_deleted_evidence_for_fills_and_rejections() {
    for late in [false, true] {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "b1", Side::Buy);
        let mut quote = evidence(&req);
        if late { quote.available_at = "2026-10-30T10:00:01+08:00".into(); }
        let mut stored = db.ingest_quote(quote.clone()).unwrap();
        db.submit(req).unwrap();
        assert!(db.verify_replay().unwrap().ok);
        let original = serde_json::to_string(&stored).unwrap();
        stored.quote.price_cents += 1;
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [serde_json::to_string(&stored).unwrap()]).unwrap();
        assert!(db.verify_replay().is_err());
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [original]).unwrap();
        assert!(db.verify_replay().unwrap().ok);
        db.conn.execute("DELETE FROM v2_quote_evidence", []).unwrap();
        assert!(db.verify_replay().is_err());
    }
}

#[test]
fn fill_failure_rolls_back_decision_but_retains_independent_evidence() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    db.ingest_quote(evidence(&req)).unwrap();
    let historical = db.state(Some("2026-10-30")).unwrap();
    db.conn.execute_batch("CREATE TRIGGER block_fill BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT,'test failure'); END;").unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert_eq!(db.state(None).unwrap().runs_total, 0);
    assert_eq!(db.state(None).unwrap().cash_cents, INITIAL_CASH_CENTS);
    assert_eq!(db.state(Some("2026-10-30")).unwrap(), historical);
    assert!(load_evidence(&db.conn, &req.quote_ref).unwrap().is_some());
    db.conn.execute_batch("DROP TRIGGER block_fill;").unwrap();
    assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
    assert_eq!(db.state(Some("2026-10-30")).unwrap().runs_total, 1);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
}

#[test]
fn evidence_and_missing_snapshots_survive_store_reopen() {
    let path = format!("file:v2-evidence-reopen-{}?mode=memory&cache=shared", std::process::id());
    let _keeper = Connection::open(&path).unwrap();
    let req = request("2026-10-30", "missing", Side::Buy);
    {
        let mut db = Store::open(&path, INITIAL_CASH_CENTS).unwrap();
        assert_eq!(db.submit(req.clone()).unwrap().outcome.reason, "quote_evidence_missing");
        db.ingest_quote(evidence(&req)).unwrap();
    }
    let mut reopened = Store::open(&path, INITIAL_CASH_CENTS).unwrap();
    let retry = reopened.submit(req.clone()).unwrap();
    assert!(retry.duplicate && retry.evidence.is_none());
    let mut next = req;
    next.signal_id = "after-reopen".into();
    let filled = reopened.submit(next).unwrap();
    assert_eq!(filled.outcome.status, DecisionStatus::Filled);
    drop(reopened);
    let reopened = Store::open(&path, INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        let report = reopened.verify_replay().unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

#[test]
fn old_policy_database_is_refused_before_adding_evidence_schema() {
    let path = format!("file:v2-old-policy-{}?mode=memory&cache=shared", std::process::id());
    let keeper = Connection::open(&path).unwrap();
    keeper.execute_batch("CREATE TABLE v2_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO v2_meta VALUES('policy_version','v2-paper-policy-1');").unwrap();
    assert!(matches!(Store::with_connection(Connection::open(&path).unwrap(), INITIAL_CASH_CENTS), Err(StoreError::Integrity(_))));
    let tables: i64 = keeper.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table'", [], |r| r.get(0)).unwrap();
    assert_eq!(tables, 1);
}

#[test]
fn server_receipt_and_offline_trust_survive_retries_reopen_and_replay() {
    let file = DiskDatabase::new();
    let req = request("2026-10-30", "receipt", Side::Buy);
    let input = evidence(&req);
    let saved;
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let before = chrono::Utc::now();
        saved = db.ingest_quote(input.clone()).unwrap();
        let after = chrono::Utc::now();
        let receipt = chrono::DateTime::parse_from_rfc3339(&saved.received_at).unwrap();
        assert!(receipt >= before && receipt <= after);
        assert_ne!(saved.received_at, input.available_at);
        assert_eq!(saved.trust_level, QuoteTrust::OfflineFixture);
        assert!(saved.provider_published_at.is_none());
        assert_eq!(db.ingest_quote(input.clone()).unwrap(), saved);
        let run = db.submit(req.clone()).unwrap();
        assert_eq!(run.outcome.status, DecisionStatus::Filled);
        assert_eq!(run.evidence, Some(saved.clone()));
    }
    let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
    assert_eq!(db.ingest_quote(input).unwrap(), saved);
    let retry = db.submit(req).unwrap();
    assert!(retry.duplicate);
    assert_eq!(retry.evidence, Some(saved));
    let first_replay = db.verify_replay().unwrap();
    assert_eq!((first_replay.runs_verified, first_replay.fills_verified), (1, 1));
    assert_eq!(db.verify_replay().unwrap(), first_replay);
}

#[test]
fn import_command_rejects_server_facts_and_invented_proofs() {
    let input = evidence(&request("2026-10-30", "b1", Side::Buy));
    let good = serde_json::to_value(&input).unwrap();
    assert!(serde_json::from_value::<QuoteImport>(good.clone()).is_ok());
    for field in ["received_at", "trust_level", "provider_published_at",
                  "provider_request_id", "provider_response_id"] {
        let mut forged = good.clone();
        forged[field] = serde_json::json!("forged-server-or-provider-fact");
        for _ in 0..2 {
            assert!(serde_json::from_value::<QuoteImport>(forged.clone()).is_err());
        }
    }
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let saved = db.ingest_quote(input).unwrap();
    assert!(serde_json::from_value::<QuoteImport>(serde_json::to_value(saved).unwrap()).is_err());
    assert_eq!(db.verify_replay().unwrap().runs_verified, 0);
}

#[test]
fn replay_detects_changed_receipt_or_trust_even_without_price_change() {
    for late in [false, true] {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "receipt", Side::Buy);
        let mut input = evidence(&req);
        if late { input.available_at = "2026-10-30T10:00:00.001+08:00".into(); }
        let saved = db.ingest_quote(input).unwrap();
        let run = db.submit(req.clone()).unwrap();
        assert_eq!(run.outcome.status, if late { DecisionStatus::Rejected } else { DecisionStatus::Filled });
        assert!(db.submit(req).unwrap().duplicate);
        let original = serde_json::to_string(&saved).unwrap();
        for (field, value) in [
            ("received_at", serde_json::json!("2000-01-01T00:00:00+08:00")),
            ("trust_level", serde_json::json!("authenticated_provider")),
            ("provider_published_at", serde_json::json!("2026-10-30T09:59:30+08:00")),
        ] {
            let mut changed = serde_json::to_value(&saved).unwrap();
            changed[field] = value;
            db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [changed.to_string()]).unwrap();
            assert!(db.verify_replay().is_err());
            db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [&original]).unwrap();
            assert!(db.verify_replay().unwrap().ok);
        }
        assert_eq!(db.verify_replay().unwrap(), db.verify_replay().unwrap());
    }
}

// Real disk files are essential: :memory: cannot expose journal-mode changes.
struct DiskDatabase(std::path::PathBuf);

impl DiskDatabase {
    fn new() -> Self {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        loop {
            let id = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let dir = std::env::temp_dir().join(format!("astock-v2-boundary-{}-{id}", std::process::id()));
            match std::fs::create_dir(&dir) {
                Ok(()) => return Self(dir.join("case.sqlite3")),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("create test directory: {error}"),
            }
        }
    }

    fn path(&self) -> &str { self.0.to_str().unwrap() }
}

impl Drop for DiskDatabase {
    fn drop(&mut self) {
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.path()));
        }
        let _ = std::fs::remove_dir(self.0.parent().unwrap());
    }
}

#[derive(Debug, PartialEq)]
struct DiskSnapshot {
    journal: String,
    schema: Vec<(String, String, String, Option<String>)>,
    rows: Vec<(String, Vec<Vec<rusqlite::types::Value>>)>,
    bytes: Vec<u8>,
    files: Vec<String>,
}

fn disk_snapshot(file: &DiskDatabase) -> DiskSnapshot {
    let conn = Connection::open_with_flags(file.path(), rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let journal = conn.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
    let schema: Vec<(String, String, String, Option<String>)> = conn.prepare(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).unwrap().query_map([], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))
        .unwrap().collect::<Result<_, _>>().unwrap();
    let mut rows = Vec::new();
    for (kind, name, _, _) in &schema {
        if kind == "table" {
            let mut stmt = conn.prepare(&format!("SELECT * FROM \"{}\" ORDER BY rowid", name.replace('"', "\"\""))).unwrap();
            let columns = stmt.column_count();
            let values = stmt.query_map([], |r| (0..columns).map(|i| r.get(i)).collect::<rusqlite::Result<Vec<rusqlite::types::Value>>>())
                .unwrap().collect::<Result<Vec<_>, _>>().unwrap();
            rows.push((name.clone(), values));
        }
    }
    drop(conn);
    let mut files: Vec<String> = std::fs::read_dir(file.0.parent().unwrap()).unwrap()
        .map(|entry| entry.unwrap().file_name().to_str().unwrap().to_owned()).collect();
    files.sort();
    DiskSnapshot { journal, schema, rows, bytes: std::fs::read(&file.0).unwrap(), files }
}

fn assert_disk_refusal_is_read_only(setup: &str) {
    let file = DiskDatabase::new();
    {
        let conn = Connection::open(file.path()).unwrap();
        conn.execute_batch(setup).unwrap();
        conn.execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
    }
    let before = disk_snapshot(&file);
    for _ in 0..2 {
        assert!(Store::open(file.path(), INITIAL_CASH_CENTS).is_err(), "foreign database accepted");
        assert_eq!(disk_snapshot(&file), before, "refused database changed on disk");
    }
}

#[test]
fn rejected_v1_disk_database_is_untouched() {
    assert_disk_refusal_is_read_only("CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,payload TEXT);
        INSERT INTO paper_orders VALUES(1,'preserve-v1-row');");
}

#[test]
fn rejected_old_v2_disk_database_is_untouched() {
    assert_disk_refusal_is_read_only("CREATE TABLE v2_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO v2_meta VALUES('policy_version','v2-paper-policy-0');
        CREATE TABLE v2_simulation_runs(id INTEGER PRIMARY KEY,payload TEXT);
        INSERT INTO v2_simulation_runs VALUES(7,'preserve-old-run');");
}

#[test]
fn rejected_unknown_disk_schema_is_untouched() {
    assert_disk_refusal_is_read_only("CREATE TABLE unrelated_business(id INTEGER PRIMARY KEY,payload BLOB);
        INSERT INTO unrelated_business VALUES(9,X'010203');");
}

#[test]
fn rejected_partial_v2_identity_is_untouched() {
    assert_disk_refusal_is_read_only("CREATE TABLE v2_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO v2_meta VALUES('policy_version','v2-paper-policy-1');");
}

#[test]
fn current_v2_with_foreign_schema_or_wrong_seed_is_untouched() {
    for foreign in [false, true] {
        let file = DiskDatabase::new();
        {
            let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
            ingest_and_submit(&mut db, request("2026-10-30", "disk-buy", Side::Buy));
        }
        {
            let conn = Connection::open(file.path()).unwrap();
            conn.execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
            if foreign {
                conn.execute_batch("CREATE TABLE unexpected(payload TEXT); INSERT INTO unexpected VALUES('keep');").unwrap();
            }
        }
        let before = disk_snapshot(&file);
        for _ in 0..2 {
            let cash = INITIAL_CASH_CENTS + if foreign { 0 } else { 1 };
            assert!(Store::open(file.path(), cash).is_err());
            assert_eq!(disk_snapshot(&file), before);
        }
    }
}

#[test]
fn empty_disk_initialization_reopen_and_millisecond_rejections_replay() {
    let file = DiskDatabase::new();
    std::fs::write(&file.0, []).unwrap();
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "expired", Side::Buy);
        let mut quote = evidence(&req);
        quote.quote.observed_at = "2026-10-30T09:56:59.999+08:00".into();
        db.ingest_quote(quote).unwrap();
        let first = db.submit(req.clone()).unwrap();
        assert_eq!(first.outcome.reason, "stale_quote");
        assert_eq!(db.submit(req).unwrap().outcome, first.outcome);
        let req = request("2026-10-30", "boundary", Side::Buy);
        let mut quote = evidence(&req);
        quote.quote.observed_at = "2026-10-30T09:57:00+08:00".into();
        db.ingest_quote(quote).unwrap();
        assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
    }
    let db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        let report = db.verify_replay().unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

fn market_input(day: &str, time: &str, id: &str) -> (SimulationRequest, QuoteImport) {
    let mut req = request(day, id, Side::Buy);
    req.decision_at = format!("{day}T{time}+08:00");
    let at = chrono::DateTime::parse_from_rfc3339(&req.decision_at).unwrap();
    let mut input = evidence(&req);
    input.quote.observed_at = (at - chrono::Duration::seconds(30)).to_rfc3339();
    input.available_at = (at - chrono::Duration::seconds(29)).to_rfc3339();
    input.execution.as_mut().unwrap().observed_at = input.quote.observed_at.clone();
    (req, input)
}

#[test]
fn closed_dates_and_precise_session_rejections_persist_and_replay() {
    for (day, time, reason) in [
        ("2026-10-31", "10:00:00", "weekend_market_closed"),
        ("2026-10-01", "10:00:00", "holiday_market_closed"),
        ("2027-01-04", "10:00:00", "unknown_trading_date"),
        ("2026-10-30", "11:30:00.001", "midday_break"),
        ("2026-10-30", "15:00:00.001", "outside_trading_session"),
        ("2026-10-30", "14:57:00", "closing_auction_not_supported"),
    ] {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let (req, input) = market_input(day, time, "closed");
        db.ingest_quote(input).unwrap();
        let first = db.submit(req.clone()).unwrap();
        assert_eq!(first.outcome.reason, reason, "{day} {time}");
        assert_eq!(first.outcome.status, DecisionStatus::Rejected);
        let retry = db.submit(req).unwrap();
        assert!(retry.duplicate);
        assert_eq!(retry.id, first.id);
        assert_eq!(retry.outcome, first.outcome);
        assert_eq!(db.state(None).unwrap().cash_cents, INITIAL_CASH_CENTS);
        for _ in 0..2 {
            let report = db.verify_replay().unwrap();
            assert_eq!((report.runs_verified, report.fills_verified), (1, 0));
        }
    }
}

#[test]
fn closed_day_decision_write_failure_rolls_back_then_retries_once() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let (req, input) = market_input("2026-10-31", "10:00:00", "rollback-closed");
    db.ingest_quote(input).unwrap();
    db.conn.execute_batch("CREATE TRIGGER block_run BEFORE INSERT ON v2_simulation_runs
        BEGIN SELECT RAISE(ABORT,'test failure'); END;").unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert_eq!(db.state(None).unwrap().runs_total, 0);
    assert_eq!(db.state(None).unwrap().cash_cents, INITIAL_CASH_CENTS);
    db.conn.execute_batch("DROP TRIGGER block_run;").unwrap();
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.reason, "weekend_market_closed");
    assert!(db.submit(req).unwrap().duplicate);
    assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
    assert_eq!(db.state(None).unwrap().runs_total, 1);
}

#[test]
fn fixed_market_snapshots_and_run_references_survive_disk_reopen() {
    let file = DiskDatabase::new();
    let snapshots;
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        for (day, id) in [("2026-10-30", "open"), ("2026-10-31", "closed")] {
            let (req, input) = market_input(day, "10:00:00", id);
            db.ingest_quote(input).unwrap();
            let first = db.submit(req.clone()).unwrap();
            assert_eq!(first.calendar_version, crate::domain::CALENDAR_VERSION);
            assert_eq!(first.session_policy_version, crate::domain::SESSION_POLICY_VERSION);
            let retry = db.submit(req).unwrap();
            assert!(retry.duplicate);
            assert_eq!(first.calendar_version, retry.calendar_version);
        }
        snapshots = (
            db.conn.query_row("SELECT value FROM v2_meta WHERE key='calendar_snapshot_json'", [], |r| r.get::<_, String>(0)).unwrap(),
            db.conn.query_row("SELECT value FROM v2_meta WHERE key='session_policy_snapshot_json'", [], |r| r.get::<_, String>(0)).unwrap(),
        );
    }
    let db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
    let (calendar, sessions) = load_market_context(&db.conn).unwrap();
    assert_eq!(serde_json::from_str::<TradingCalendar>(&snapshots.0).unwrap(), calendar);
    assert_eq!(serde_json::from_str::<SessionPolicy>(&snapshots.1).unwrap(), sessions);
    for _ in 0..2 {
        let report = db.verify_replay().unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

#[test]
fn changed_market_snapshots_or_run_versions_never_reinterpret_history() {
    for day in ["2026-10-30", "2026-10-31"] {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let (req, input) = market_input(day, "10:00:00", "pin");
        db.ingest_quote(input).unwrap();
        let saved = db.submit(req.clone()).unwrap();
        let state = db.state(None).unwrap();
        for key in ["calendar_snapshot_json", "session_policy_snapshot_json"] {
            let original: String = db.conn.query_row("SELECT value FROM v2_meta WHERE key=?", [key], |r| r.get(0)).unwrap();
            let mut changed: serde_json::Value = serde_json::from_str(&original).unwrap();
            if key == "calendar_snapshot_json" {
                changed["holidays"][6]["end"] = serde_json::json!("2026-10-08");
            } else {
                changed["continuous"][0]["end"] = serde_json::json!("11:31:00");
            }
            db.conn.execute("UPDATE v2_meta SET value=? WHERE key=?", params![changed.to_string(), key]).unwrap();
            assert!(db.verify_replay().is_err());
            let mut next = req.clone();
            next.signal_id = "next".into();
            assert!(db.submit(next).is_err());
            assert_eq!(db.state(None).unwrap(), state);
            db.conn.execute("UPDATE v2_meta SET value=? WHERE key=?", params![original, key]).unwrap();
            assert!(db.verify_replay().unwrap().ok);
        }
        for (column, original) in [
            ("calendar_version", saved.calendar_version.as_str()),
            ("session_policy_version", saved.session_policy_version.as_str()),
        ] {
            db.conn.execute(&format!("UPDATE v2_simulation_runs SET {column}='later-version'"), []).unwrap();
            assert!(db.verify_replay().is_err());
            assert!(db.submit(req.clone()).is_err());
            db.conn.execute(&format!("UPDATE v2_simulation_runs SET {column}=?"), [original]).unwrap();
            assert!(db.verify_replay().unwrap().ok);
        }
        assert_eq!(db.runs(10).unwrap()[0], saved);
        assert_eq!(db.verify_replay().unwrap(), db.verify_replay().unwrap());
    }
}

#[test]
fn invalid_pinned_calendar_is_refused_before_wal_or_schema_writes() {
    let file = DiskDatabase::new();
    {
        let mut db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "keep", Side::Buy));
    }
    {
        let conn = Connection::open(file.path()).unwrap();
        conn.execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
        let raw: String = conn.query_row("SELECT value FROM v2_meta WHERE key='calendar_snapshot_json'", [], |r| r.get(0)).unwrap();
        let mut changed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        changed["coverage_end"] = serde_json::json!("2026-12-30");
        conn.execute("UPDATE v2_meta SET value=? WHERE key='calendar_snapshot_json'", [changed.to_string()]).unwrap();
    }
    let before = disk_snapshot(&file);
    for _ in 0..2 {
        assert!(Store::open(file.path(), INITIAL_CASH_CENTS).is_err());
        assert_eq!(disk_snapshot(&file), before);
    }
}
