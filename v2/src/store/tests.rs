use super::*;
use crate::domain::{Side, Signal, Quote, INITIAL_CASH_CENTS, DecisionStatus};

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

fn evidence(req: &SimulationRequest) -> QuoteEvidence {
    let day = &req.decision_at[..10];
    QuoteEvidence {
        schema_version: 1, available_at: format!("{day}T09:59:31+08:00"),
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
fn quote_ingestion_is_idempotent_and_every_evidence_field_is_immutable() {
    let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    let quote = evidence(&req);
    assert_eq!(db.ingest_quote(quote.clone()).unwrap(), quote);
    assert_eq!(db.ingest_quote(quote.clone()).unwrap(), quote);
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
    assert_eq!(load_evidence(&db.conn, &req.quote_ref).unwrap(), Some(quote));
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
        db.ingest_quote(quote.clone()).unwrap();
        db.submit(req).unwrap();
        assert!(db.verify_replay().unwrap().ok);
        let original = serde_json::to_string(&quote).unwrap();
        quote.quote.price_cents += 1;
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [serde_json::to_string(&quote).unwrap()]).unwrap();
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
    db.conn.execute_batch("CREATE TRIGGER block_fill BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT,'test failure'); END;").unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert_eq!(db.state(None).unwrap().runs_total, 0);
    assert_eq!(db.state(None).unwrap().cash_cents, INITIAL_CASH_CENTS);
    assert!(load_evidence(&db.conn, &req.quote_ref).unwrap().is_some());
    db.conn.execute_batch("DROP TRIGGER block_fill;").unwrap();
    assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
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
