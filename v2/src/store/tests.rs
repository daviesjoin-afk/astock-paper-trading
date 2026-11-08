use super::*;
use crate::domain::{PaperAccountSpec, PaperOrderRequest, PaperSessionSignal, PaperSessionSpec, PaperSlotId, PaperSlotSpec};

#[test]
fn tampering_evidence_pack_identity_is_rejected_on_retry_and_replay() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "identity-corruption", Side::Buy);
    ingest_and_submit(&mut db, req.clone());
    db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=json_set(evidence_json,'$.market_data_pack_id','cn-offline-fixture-pack-2')", []).unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert!(db.verify_replay("cycle-1").is_err());
    db.conn.execute("UPDATE v2_simulation_runs SET evidence_json=json_set(evidence_json,'$.market_data_pack_id','cn-offline-fixture-pack-2')", []).unwrap();
    assert!(db.submit(req).is_err());
    assert!(db.verify_replay("cycle-1").is_err());
}


#[test]
fn two_packs_keep_equal_external_ids_independent_after_completion_and_restart() {
    let file = DiskDatabase::new();
    let a_req = request("2026-10-30", "external-shared", Side::Buy);
    let mut b_req = a_req.clone();
    b_req.cycle_id = "pack-b".into();
    b_req.quote_ref.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    let a_old;
    let a_replay;
    let b_old;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let a_fill = ingest_and_submit(&mut db, a_req.clone());
        assert_eq!(a_fill.outcome.fill.unwrap().quantity, 800);
        a_old = db.state("cycle-1", Some("2026-10-30")).unwrap();
        db.complete_experiment("cycle-1").unwrap();
        a_replay = db.verify_replay("cycle-1").unwrap();
        // A is completed before B's pack registration and evidence arrival.
        let mut b_spec = spec("pack-b", INITIAL_CASH_CENTS);
        b_spec.market_data_pack_id = b_req.quote_ref.market_data_pack_id.clone();
        db.create_experiment(b_spec).unwrap();
        let mut input = evidence(&b_req);
        input.quote.price_cents = 2000;
        input.execution.as_mut().unwrap().price_cents = 2000;
        let b_evidence = db.ingest_quote(input.clone()).unwrap();
        db.conn.execute_batch("CREATE TRIGGER fail_b_fill BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT,'B fill rollback'); END;").unwrap();
        assert!(db.submit(b_req.clone()).is_err());
        assert_eq!(db.state("pack-b", None).unwrap().runs_total, 0);
        assert_eq!(db.verify_replay("cycle-1").unwrap(), a_replay);
        db.conn.execute_batch("DROP TRIGGER fail_b_fill;").unwrap();
        let second = db.submit(b_req.clone()).unwrap();
        assert_eq!(second.outcome.fill.as_ref().unwrap().quantity, 400);
        assert_eq!(second.outcome.fill.as_ref().unwrap().price_cents, 2000);
        assert_eq!(second.instrument_version, "cn-instruments-offline-v2");
        assert_eq!(db.ingest_quote(input.clone()).unwrap(), b_evidence);
        assert!(db.submit(b_req.clone()).unwrap().duplicate);
        input.execution.as_mut().unwrap().price_cents += 1;
        assert!(matches!(db.ingest_quote(input), Err(StoreError::Conflict(_))));
        let mut sell = b_req.clone();
        sell.signal_id = "b-t1".into(); sell.signal.side = Side::Sell;
        sell.quote_ref.quote_id = "b-t1-quote".into();
        assert_eq!(ingest_and_submit(&mut db, sell).outcome.reason, "no_t1_sellable_lot");
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), a_old);
        assert_eq!(db.verify_replay("cycle-1").unwrap(), a_replay);
        b_old = db.state("pack-b", None).unwrap();
        assert_eq!(b_old.runs_total, 2);
        assert_eq!(a_old.runs_total, 1);
        assert_ne!(a_old.cash_cents, b_old.cash_cents);
    }
    let mut db = Store::open(file.path()).unwrap();
    assert_eq!(db.verify_replay("cycle-1").unwrap(), a_replay);
    assert_eq!(db.verify_replay("pack-b").unwrap().fills_verified, 1);
    assert_eq!(db.state("pack-b", None).unwrap(), b_old);
    assert!(db.submit(a_req).unwrap().duplicate);
    assert!(db.submit(b_req).unwrap().duplicate);
}

#[test]
fn pack_identity_summary_contents_and_experiment_references_are_verified() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let mut b_spec = spec("pack-b", INITIAL_CASH_CENTS);
        b_spec.market_data_pack_id = "cn-offline-fixture-pack-2".into();
        db.create_experiment(b_spec).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "keep-a", Side::Buy));
        let before = db.verify_replay("cycle-1").unwrap();
        let original: String = db.conn.query_row("SELECT pack_json FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-2'", [], |r| r.get(0)).unwrap();
        for path in ["manifest.pack_id", "manifest.version", "manifest.content_summary.instruments", "instruments.version", "calendar.version"] {
            db.conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json,?,'tampered') WHERE pack_id='cn-offline-fixture-pack-2'", [format!("$.{path}")]).unwrap();
            assert!(db.verify_replay("pack-b").is_err());
            assert!(db.state("pack-b", None).is_err());
            assert_eq!(db.verify_replay("cycle-1").unwrap(), before);
            assert!(db.register_reviewed_pack("cn-offline-fixture-pack-2").is_err());
            db.conn.execute("UPDATE v2_market_data_packs SET pack_json=? WHERE pack_id='cn-offline-fixture-pack-2'", [&original]).unwrap();
        }
        db.conn.execute("UPDATE v2_experiments SET spec_json=json_set(spec_json,'$.market_data_pack_id','cn-offline-fixture-pack-1') WHERE cycle_id='pack-b'", []).unwrap();
        assert!(db.verify_replay("pack-b").is_err());
        assert_eq!(db.verify_replay("cycle-1").unwrap(), before);
    }
    let before = disk_snapshot(&file);
    assert!(Store::open(file.path()).is_err());
    assert_eq!(disk_snapshot(&file), before);
}

#[test]
fn pack_registration_is_idempotent_rejects_replacement_and_rolls_back_with_creation() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let a = db.state("cycle-1", None).unwrap();
    let mut b = spec("pack-b", INITIAL_CASH_CENTS);
    b.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    db.conn.execute_batch("CREATE TRIGGER fail_pack_experiment BEFORE INSERT ON v2_experiments BEGIN SELECT RAISE(ABORT,'test rollback'); END;").unwrap();
    assert!(db.create_experiment(b.clone()).is_err());
    let count: i64 = db.conn.query_row("SELECT count(*) FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-2'", [], |r| r.get(0)).unwrap();
    assert_eq!(count, 0);
    db.conn.execute_batch("DROP TRIGGER fail_pack_experiment;").unwrap();
    db.create_experiment(b).unwrap();
    let saved = db.register_reviewed_pack("cn-offline-fixture-pack-2").unwrap();
    assert_eq!(db.register_reviewed_pack("cn-offline-fixture-pack-2").unwrap(), saved);
    let mut bad = saved.clone(); bad.manifest.description = "same-id-different-content".into();
    assert!(matches!(register_pack(&db.conn, &bad), Err(StoreError::Conflict(_))));
    assert_eq!(load_market_pack(&db.conn, "cn-offline-fixture-pack-2").unwrap(), saved);
    assert!(db.register_reviewed_pack("unknown-pack").is_err());
    assert_eq!(db.state("cycle-1", None).unwrap(), a);
}

#[test]
fn cross_pack_execution_and_request_references_fail_without_writes() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.register_reviewed_pack("cn-offline-fixture-pack-2").unwrap();
    let mut req = request("2026-10-30", "cross", Side::Buy);
    let mut input = evidence(&req);
    input.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    assert!(db.ingest_quote(input).is_err()); // Nested execution still claims A.
    req.quote_ref.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    assert!(db.submit(req).is_err()); // Experiment still pins A.
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
    let mut b = spec("pack-b", INITIAL_CASH_CENTS);
    b.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    db.create_experiment(b).unwrap();
    db.conn.execute("DELETE FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-2'", []).unwrap();
    assert!(db.verify_replay("pack-b").is_err());
    assert!(db.state("pack-b", None).is_err());
    assert!(db.verify_replay("cycle-1").unwrap().ok);
}


#[test]
fn pack_selection_allows_a_second_reviewed_experiment() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let mut b = spec("pack-b", INITIAL_CASH_CENTS);
    b.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    assert!(db.create_experiment(b).is_ok(), "an independently reviewed pack must be selectable");
}

#[test]
fn packs_can_ingest_equal_external_quote_ids_with_different_prices() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "same-external-id", Side::Buy);
    let a = evidence(&req);
    db.register_reviewed_pack("cn-offline-fixture-pack-2").unwrap();
    let mut b = a.clone();
    b.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    b.execution.as_mut().unwrap().quote_ref.market_data_pack_id = b.market_data_pack_id.clone();
    b.quote.price_cents = 2000;
    b.execution.as_mut().unwrap().price_cents = 2000;
    db.ingest_quote(a).unwrap();
    assert!(db.ingest_quote(b).is_ok(), "B must not collide with A's external quote ID");
}

#[test]
fn adding_reviewed_context_does_not_break_a_completed_experiment_on_reopen() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "old-pack", Side::Buy));
        db.complete_experiment("cycle-1").unwrap();
        db.register_reviewed_pack("cn-offline-fixture-pack-2").unwrap();
    }
    let reopened = Store::open(file.path());
    assert!(reopened.is_ok(), "installing B must leave completed A readable");
    assert_eq!(reopened.unwrap().verify_replay("cycle-1").unwrap().fills_verified, 1);
}

#[test]
fn experiments_select_market_coverage_without_redefining_a_global_context() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-27", "coverage", Side::Buy);
    assert_eq!(ingest_and_submit(&mut db, req).outcome.status, DecisionStatus::Filled);
    let before = db.state("cycle-1", None).unwrap();
    let mut b = spec("pack-b", INITIAL_CASH_CENTS);
    b.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    let created = db.create_experiment(b);
    assert!(created.is_ok(), "B's narrower coverage must not redefine A's context");
    let mut b_req = request("2026-10-27", "coverage-b", Side::Buy);
    b_req.cycle_id = "pack-b".into();
    b_req.quote_ref.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    assert_eq!(ingest_and_submit(&mut db, b_req).outcome.reason, "instrument_date_not_covered");
    assert_eq!(db.state("cycle-1", None).unwrap(), before);
}


#[test]
fn failed_experiment_creation_and_completion_roll_back_without_touching_other_experiments() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let a = db.state("cycle-1", None).unwrap();
    let mut b = spec("b", 5_000_000);
    b.strategy = reversion_artifact();
    db.conn.execute_batch("CREATE TRIGGER fail_create BEFORE INSERT ON v2_experiments BEGIN SELECT RAISE(ABORT,'create failure'); END;").unwrap();
    assert!(db.create_experiment(b.clone()).is_err());
    let artifacts: i64 = db.conn.query_row("SELECT count(*) FROM v2_strategy_artifacts WHERE strategy_version_id='research-reversion-v1'", [], |r| r.get(0)).unwrap();
    assert_eq!(artifacts, 0);
    db.conn.execute_batch("DROP TRIGGER fail_create;").unwrap();
    db.create_experiment(b).unwrap();
    let mut req = request("2026-10-30", "new-version-buy", Side::Buy);
    req.cycle_id = "b".into();
    req.strategy_version_id = "research-reversion-v1".into();
    assert_eq!(ingest_and_submit(&mut db, req).outcome.status, DecisionStatus::Filled);
    db.conn.execute_batch("CREATE TRIGGER fail_complete BEFORE UPDATE ON v2_experiments BEGIN SELECT RAISE(ABORT,'complete failure'); END;").unwrap();
    assert!(db.complete_experiment("b").is_err());
    assert_eq!(db.experiment("b").unwrap().status, ExperimentStatus::Active);
    db.conn.execute_batch("DROP TRIGGER fail_complete;").unwrap();
    assert_eq!(db.complete_experiment("b").unwrap().status, ExperimentStatus::Completed);
    assert_eq!(db.state("cycle-1", None).unwrap(), a);
    assert_eq!(db.experiment("cycle-1").unwrap().status, ExperimentStatus::Active);
}

#[test]
fn completed_missing_evidence_stays_missing_after_another_experiment_ingests_it() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_experiment(spec("b", INITIAL_CASH_CENTS)).unwrap();
    let req = request("2026-10-30", "missing-at-a", Side::Buy);
    let missing = db.submit(req.clone()).unwrap();
    assert_eq!(missing.outcome.reason, "quote_evidence_missing");
    db.complete_experiment("cycle-1").unwrap();
    let old = db.state("cycle-1", None).unwrap();
    db.ingest_quote(evidence(&req)).unwrap();
    let mut b = req.clone(); b.cycle_id = "b".into();
    assert_eq!(db.submit(b).unwrap().outcome.status, DecisionStatus::Filled);
    let retry = db.submit(req).unwrap();
    assert!(retry.duplicate);
    assert_eq!(retry.evidence, None);
    assert_eq!(retry.outcome, missing.outcome);
    assert_eq!(db.state("cycle-1", None).unwrap(), old);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
    assert_eq!(db.verify_replay("b").unwrap().fills_verified, 1);
}


#[test]
fn experiments_isolate_cash_t1_history_counts_time_order_and_reopen() {
    let file = DiskDatabase::new();
    let a_history;
    let a_final;
    let b_final;
    let a_buy = request("2026-10-30", "shared-buy", Side::Buy);
    let mut b_buy = a_buy.clone();
    b_buy.cycle_id = "b".into();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        db.create_experiment(spec("b", INITIAL_CASH_CENTS / 2)).unwrap();
        db.ingest_quote(evidence(&a_buy)).unwrap();
        assert_eq!(db.submit(a_buy.clone()).unwrap().outcome.fill.unwrap().quantity, 800);
        a_history = db.state("cycle-1", Some("2026-10-30")).unwrap();
        assert_eq!(db.state("b", None).unwrap().cash_cents, INITIAL_CASH_CENTS / 2);
        assert_eq!(db.state("b", None).unwrap().runs_total, 0);
        // Advance A before submitting B's earlier independent timeline.
        let a_sell = request("2026-11-02", "shared-sell", Side::Sell);
        ingest_and_submit(&mut db, a_sell.clone());
        a_final = db.state("cycle-1", None).unwrap();
        assert_eq!(db.submit(b_buy.clone()).unwrap().outcome.fill.unwrap().quantity, 400);
        assert!(db.submit(b_buy.clone()).unwrap().duplicate);
        assert!(db.submit(a_buy.clone()).unwrap().duplicate);
        let mut t1 = request("2026-10-30", "b-t1", Side::Sell);
        t1.cycle_id = "b".into();
        assert_eq!(ingest_and_submit(&mut db, t1).outcome.reason, "no_t1_sellable_lot");
        let mut b_sell = a_sell;
        b_sell.cycle_id = "b".into();
        assert_eq!(db.submit(b_sell).unwrap().outcome.fill.unwrap().quantity, 400);
        b_final = db.state("b", None).unwrap();
        assert_eq!(db.state("cycle-1", None).unwrap(), a_final);
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), a_history);
        assert_eq!(a_final.runs_total, 2);
        assert_eq!(b_final.runs_total, 3);
        assert_eq!(db.runs("b", 100).unwrap().len(), 3);
        assert!(db.runs("b", 100).unwrap().iter().all(|r| r.request.cycle_id == "b"));
        assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 2);
        assert_eq!(db.verify_replay("b").unwrap().fills_verified, 2);
    }
    let mut db = Store::open(file.path()).unwrap();
    assert_eq!(db.state("cycle-1", None).unwrap(), a_final);
    assert_eq!(db.state("b", None).unwrap(), b_final);
    assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), a_history);
    assert!(db.submit(a_buy).unwrap().duplicate);
    assert!(db.submit(b_buy).unwrap().duplicate);
    assert_eq!(db.verify_replay("b").unwrap().runs_verified, 3);
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 2);
}

#[test]
fn experiments_isolate_transaction_failure_evidence_conflict_and_capital_limits() {
    let file = DiskDatabase::new();
    let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
    db.create_experiment(spec("b", 1)).unwrap();
    let a = request("2026-10-30", "same", Side::Buy);
    db.ingest_quote(evidence(&a)).unwrap();
    let mut b = a.clone(); b.cycle_id = "b".into();
    let poor = db.submit(b).unwrap();
    assert!(poor.outcome.fill.is_none());
    assert_eq!(poor.outcome.cash_after_cents, 1);
    let b_before = db.state("b", None).unwrap();
    db.conn.execute_batch("CREATE TRIGGER fail_a BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT,'simulated failure'); END;").unwrap();
    assert!(matches!(db.submit(a.clone()), Err(StoreError::Database(_))));
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
    assert_eq!(db.state("cycle-1", None).unwrap().cash_cents, INITIAL_CASH_CENTS);
    assert_eq!(db.state("b", None).unwrap(), b_before);
    db.conn.execute_batch("DROP TRIGGER fail_a;").unwrap();
    assert_eq!(db.submit(a.clone()).unwrap().outcome.status, DecisionStatus::Filled);
    let a_before = db.state("cycle-1", None).unwrap();
    let mut bad = evidence(&a); bad.execution.as_mut().unwrap().price_cents += 1;
    assert!(matches!(db.ingest_quote(bad), Err(StoreError::Conflict(_))));
    assert_eq!(db.state("cycle-1", None).unwrap(), a_before);
    assert_eq!(db.state("b", None).unwrap(), b_before);
    drop(db);
    let db = Store::open(file.path()).unwrap();
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
    assert_eq!(db.verify_replay("b").unwrap().fills_verified, 0);
    assert_eq!(db.state("b", None).unwrap(), b_before);
}

#[test]
fn experiment_inputs_versions_and_completion_are_immutable() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let original = db.experiment("cycle-1").unwrap();
    assert_eq!(db.create_experiment(original.spec.clone()).unwrap(), original);
    let mut changed = original.spec.clone(); changed.initial_cash_cents += 1;
    assert!(matches!(db.create_experiment(changed), Err(StoreError::Conflict(_))));
    let mut changed = spec("b", INITIAL_CASH_CENTS); changed.strategy.research_input_id = "other-input".into();
    assert!(matches!(db.create_experiment(changed), Err(StoreError::Conflict(_))));
    assert!(db.experiment("b").is_err());
    for field in ["policy", "pack", "cash", "schema"] {
        let mut bad = spec("invalid", INITIAL_CASH_CENTS);
        match field {
            "policy" => bad.execution_policy_version = "unsupported-old-policy".into(),
            "pack" => bad.market_data_pack_id = "unverified-pack".into(),
            "cash" => bad.initial_cash_cents = 0,
            _ => bad.schema_version = 0,
        }
        assert!(db.create_experiment(bad).is_err());
        assert!(db.experiment("invalid").is_err());
    }
    let buy = request("2026-10-30", "buy", Side::Buy);
    ingest_and_submit(&mut db, buy.clone());
    let before = db.state("cycle-1", None).unwrap();
    assert_eq!(db.complete_experiment("cycle-1").unwrap().status, ExperimentStatus::Completed);
    assert_eq!(db.complete_experiment("cycle-1").unwrap().status, ExperimentStatus::Completed);
    assert_eq!(db.create_experiment(original.spec).unwrap().status, ExperimentStatus::Completed);
    assert!(db.submit(buy.clone()).unwrap().duplicate);
    let mut next = buy.clone(); next.signal_id = "new-signal".into();
    assert!(matches!(db.submit(next), Err(StoreError::Conflict(_))));
    let mut wrong = buy; wrong.strategy_version_id = "different-strategy".into();
    assert!(matches!(db.submit(wrong), Err(StoreError::Conflict(_))));
    assert_eq!(db.state("cycle-1", None).unwrap(), before);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
    db.create_experiment(spec("b", INITIAL_CASH_CENTS)).unwrap();
    assert_eq!(db.experiment("b").unwrap().status, ExperimentStatus::Active);
}

#[test]
fn uncreated_experiment_and_legacy_contract_never_mutate_ledger() {
    let mut db = Store::in_memory().unwrap();
    let mut req = request("2026-10-30", "buy", Side::Buy);
    assert!(db.submit(req.clone()).is_err());
    assert!(db.state("cycle-1", None).is_err());
    assert!(db.runs("cycle-1", 100).is_err());
    assert!(db.verify_replay("cycle-1").is_err());
    db.create_experiment(spec("cycle-1", INITIAL_CASH_CENTS)).unwrap();
    req.schema_version = 2;
    assert!(db.submit(req).is_err());
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
}

#[test]
fn replay_refuses_cross_experiment_request_and_unsupported_old_policy() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        db.create_experiment(spec("b", INITIAL_CASH_CENTS)).unwrap();
        let req = request("2026-10-30", "buy", Side::Buy);
        ingest_and_submit(&mut db, req);
        db.conn.execute("UPDATE v2_simulation_runs SET cycle_id='b'", []).unwrap();
        assert!(db.verify_replay("b").is_err());
        assert!(db.complete_experiment("b").is_err());
        assert_eq!(db.experiment("b").unwrap().status, ExperimentStatus::Active);
        db.conn.execute("UPDATE v2_simulation_runs SET cycle_id='cycle-1'", []).unwrap();
        db.conn.execute("UPDATE v2_experiments SET spec_json=json_set(spec_json,'$.execution_policy_version','unknown') WHERE cycle_id='b'", []).unwrap();
        assert!(db.verify_replay("b").is_err());
        assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
    }
    let before = disk_snapshot(&file);
    assert!(Store::open(file.path()).is_err());
    assert_eq!(disk_snapshot(&file), before);
}

use crate::domain::{Side, Signal, Quote, ExecutionLiquidity, INITIAL_CASH_CENTS, DecisionStatus};

#[test]
fn independent_experiments_can_buy_the_same_signal_and_evidence() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let a = request("2026-10-30", "shared-signal", Side::Buy);
    let mut b = a.clone();
    b.cycle_id = "experiment-b".into();
    db.create_experiment(spec("experiment-b", INITIAL_CASH_CENTS)).unwrap();
    db.ingest_quote(evidence(&a)).unwrap();
    assert_eq!(db.submit(a).unwrap().outcome.status, DecisionStatus::Filled);
    let second = db.submit(b).unwrap();
    assert_eq!(second.outcome.status, DecisionStatus::Filled,
        "B must use its own capital; actual rejection: {}", second.outcome.reason);
}

fn request(day: &str, signal_id: &str, side: Side) -> SimulationRequest {
    SimulationRequest {
        schema_version: 4, cycle_id: "cycle-1".into(),
        strategy_version_id: "research-momentum-v1".into(), signal_id: signal_id.into(),
        decision_at: format!("{day}T10:00:00+08:00"),
        signal: Signal { symbol: "600000.SH".into(), side },
        quote_ref: QuoteRef { market_data_pack_id: crate::domain::MARKET_DATA_PACK_ID.into(),
            source: "offline-fixture".into(), quote_id: format!("quote-{signal_id}"),
        },
    }
}

fn evidence(req: &SimulationRequest) -> QuoteImport {
    let day = &req.decision_at[..10];
    QuoteImport { market_data_pack_id: req.quote_ref.market_data_pack_id.clone(),
        schema_version: 2, available_at: format!("{day}T09:59:31+08:00"),
        execution: Some(ExecutionLiquidity {
            schema_version: 2, quote_ref: req.quote_ref.clone(),
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
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 1);
    assert!(db.state("cycle-1", None).unwrap().positions.is_empty());
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
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
fn fixed_instrument_facts_rejections_and_pit_survive_disk_reopen() {
    let file = DiskDatabase::new();
    let historical;
    let latest;
    let replay;
    let mut saved = Vec::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        for (symbol, id, reason) in [
            ("688001.SH", "star", "unsupported_instrument_board"),
            ("513150.SH", "etf", "unsupported_security_type"),
            ("999999.SH", "missing", "instrument_identity_missing"),
            ("600000.SH", "main", "filled"),
        ] {
            let mut req = request("2026-10-30", id, Side::Buy);
            req.signal.symbol = symbol.into();
            let input = evidence(&req);
            let pinned = db.ingest_quote(input.clone()).unwrap();
            assert_eq!(pinned.instrument.is_some(), symbol != "999999.SH");
            assert_eq!(pinned.trust_level, QuoteTrust::OfflineFixture);
            if let Some(identity) = &pinned.instrument {
                assert_eq!(identity.evidence_version, crate::domain::INSTRUMENT_VERSION);
                assert_eq!(identity.symbol, symbol);
            }
            let first = db.submit(req.clone()).unwrap();
            assert_eq!(first.instrument_version, crate::domain::INSTRUMENT_VERSION);
            assert_eq!(first.outcome.reason, reason);
            assert_eq!(first.evidence, Some(pinned.clone()));
            assert_eq!(db.ingest_quote(input).unwrap(), pinned);
            assert!(db.submit(req).unwrap().duplicate);
            saved.push(first);
        }
        historical = db.state("cycle-1", Some("2026-10-30")).unwrap();
        assert_eq!(historical.positions[0].quantity, 800);
        ingest_and_submit(&mut db, request("2026-11-02", "main-sell", Side::Sell));
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), historical);
        latest = db.state("cycle-1", None).unwrap();
        assert!(latest.positions.is_empty());
        replay = db.verify_replay("cycle-1").unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (5, 2));
    }
    let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
    for first in saved {
        for _ in 0..2 {
            let retry = db.submit(first.request.clone()).unwrap();
            assert!(retry.duplicate);
            assert_eq!(retry.outcome, first.outcome);
            assert_eq!(retry.evidence, first.evidence);
            assert_eq!(retry.instrument_version, first.instrument_version);
            assert_eq!(db.verify_replay("cycle-1").unwrap(), replay);
            assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), historical);
            assert_eq!(db.state("cycle-1", None).unwrap(), latest);
        }
    }
}

#[test]
fn identity_rejection_transaction_failure_cannot_leave_a_partial_run() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let mut req = request("2026-10-30", "blocked-identity", Side::Buy);
    req.signal.symbol = "688001.SH".into();
    let pinned = db.ingest_quote(evidence(&req)).unwrap();
    let before = db.state("cycle-1", None).unwrap();
    db.conn.execute_batch("CREATE TRIGGER fail_identity_run BEFORE INSERT ON v2_simulation_runs
        BEGIN SELECT RAISE(ABORT,'injected identity run failure'); END;").unwrap();
    for _ in 0..2 {
        assert!(db.submit(req.clone()).is_err());
        assert_eq!(db.state("cycle-1", None).unwrap(), before);
        assert_eq!(load_evidence(&db.conn, &req.quote_ref).unwrap(), Some(pinned.clone()));
        assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 0);
    }
    db.conn.execute_batch("DROP TRIGGER fail_identity_run;").unwrap();
    assert_eq!(db.submit(req.clone()).unwrap().outcome.reason, "unsupported_instrument_board");
    assert!(db.submit(req).unwrap().duplicate);
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 1);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
}

#[test]
fn research_and_import_cannot_supply_instrument_qualification() {
    let req = request("2026-10-30", "no-self-qualification", Side::Buy);
    for field in ["instrument", "instrument_version", "board", "exchange", "security_type", "quantity_rule_version"] {
        let mut request_json = serde_json::to_value(&req).unwrap();
        request_json[field] = serde_json::json!("forged-main-board");
        assert!(serde_json::from_value::<SimulationRequest>(request_json).is_err());
        let mut request_json = serde_json::to_value(&req).unwrap();
        request_json["signal"][field] = serde_json::json!("forged-main-board");
        assert!(serde_json::from_value::<SimulationRequest>(request_json).is_err());
        let mut import = serde_json::to_value(evidence(&req)).unwrap();
        import[field] = serde_json::json!("forged-main-board");
        assert!(serde_json::from_value::<QuoteImport>(import).is_err());
        let mut import = serde_json::to_value(evidence(&req)).unwrap();
        import["quote"][field] = serde_json::json!("forged-main-board");
        assert!(serde_json::from_value::<QuoteImport>(import).is_err());
    }
}

#[test]
fn identity_tampering_in_both_evidence_copies_cannot_rewrite_history() {
    for (symbol, day) in [("600000.SH", "2026-10-30"), ("688001.SH", "2026-10-30"),
        ("999999.SH", "2026-10-30"), ("600000.SH", "2026-10-31"), ("999999.SH", "2026-10-31")] {
        let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
        let mut req = request(day, "tamper-identity", Side::Buy);
        req.signal.symbol = symbol.into();
        let first = ingest_and_submit(&mut db, req.clone());
        let mut changed = serde_json::to_value(first.evidence.unwrap()).unwrap();
        let snapshot: InstrumentSnapshot = serde_json::from_str(BUNDLED_INSTRUMENTS_JSON).unwrap();
        let mut forged = snapshot.instruments[0].clone();
        forged.symbol = symbol.into();
        forged.evidence_version = "later-unverified".into();
        changed["instrument"] = serde_json::to_value(forged).unwrap();
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [changed.to_string()]).unwrap();
        db.conn.execute("UPDATE v2_simulation_runs SET evidence_json=?", [changed.to_string()]).unwrap();
        let before = db.state("cycle-1", None).unwrap();
        let writes = db.conn.total_changes();
        for _ in 0..2 { assert!(db.verify_replay("cycle-1").is_err()); }
        assert_eq!(db.state("cycle-1", None).unwrap(), before);
        assert_eq!(db.conn.total_changes(), writes);
        assert!(db.ingest_quote(evidence(&req)).is_err());
    }
}

#[test]
fn instrument_snapshot_and_run_version_cannot_be_changed_in_place() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "pin-identity-version", Side::Buy);
    let saved = ingest_and_submit(&mut db, req.clone());
    let state = db.state("cycle-1", None).unwrap();
    let original: String = db.conn.query_row("SELECT json_extract(pack_json,'$.instruments') FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-1'", [], |r| r.get(0)).unwrap();
    let mut changed: serde_json::Value = serde_json::from_str(&original).unwrap();
    changed["sources"][0]["locator"] = serde_json::json!("same-version-different-content");
    db.conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json,'$.instruments',json(?)) WHERE pack_id='cn-offline-fixture-pack-1'", [changed.to_string()]).unwrap();
    for _ in 0..2 {
        assert!(db.submit(req.clone()).is_err());
        assert!(db.ingest_quote(evidence(&req)).is_err());
        assert!(db.verify_replay("cycle-1").is_err());
        assert!(db.state("cycle-1", None).is_err());
    }
    db.conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json,'$.instruments',json(?)) WHERE pack_id='cn-offline-fixture-pack-1'", [original]).unwrap();
    db.conn.execute("UPDATE v2_simulation_runs SET instrument_version='later-version'", []).unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert!(db.verify_replay("cycle-1").is_err());
    db.conn.execute("UPDATE v2_simulation_runs SET instrument_version=?", [saved.instrument_version]).unwrap();
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
    assert_eq!(db.state("cycle-1", None).unwrap(), state);
}

#[test]
fn old_instrument_policy_or_changed_snapshot_is_refused_before_disk_writes() {
    for old_policy in [true, false] {
        let file = DiskDatabase::new();
        {
            let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
            ingest_and_submit(&mut db, request("2026-10-30", "keep-identity", Side::Buy));
            if old_policy {
                db.conn.execute("UPDATE v2_experiments SET spec_json=json_set(spec_json,'$.execution_policy_version','v2-paper-policy-5')", []).unwrap();
            } else {
                let mut changed: serde_json::Value = serde_json::from_str(BUNDLED_INSTRUMENTS_JSON).unwrap();
                changed["sources"][0]["locator"] = serde_json::json!("tampered");
                db.conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json,'$.instruments',json(?)) WHERE pack_id='cn-offline-fixture-pack-1'", [changed.to_string()]).unwrap();
            }
        }
        Connection::open(file.path()).unwrap().execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
        let before = disk_snapshot(&file);
        for _ in 0..2 {
            assert!(test_open(file.path(), INITIAL_CASH_CENTS).is_err());
            assert_eq!(disk_snapshot(&file), before);
        }
    }
}

#[test]
fn instrument_quantity_boundaries_are_persisted_idempotent_and_replayed() {
    for (cash, price, reason, quantity) in [
        (INITIAL_CASH_CENTS, 9995, "filled", Some(100)),
        (INITIAL_CASH_CENTS, 9996, "insufficient_budget_for_board_lot_and_fees", None),
        (10_005_000, 1, "filled", Some(1_000_000)),
        (10_006_000, 1, "order_quantity_exceeds_instrument_limit", None),
    ] {
        let mut db = test_store(cash).unwrap();
        let req = request("2026-10-30", "quantity-boundary", Side::Buy);
        let mut input = evidence(&req);
        input.execution.as_mut().unwrap().price_cents = price;
        input.execution.as_mut().unwrap().available_quantity = 2_000_000;
        let pinned = db.ingest_quote(input.clone()).unwrap();
        let first = db.submit(req.clone()).unwrap();
        assert_eq!(first.outcome.reason, reason);
        assert_eq!(first.outcome.fill.as_ref().map(|fill| fill.quantity), quantity);
        let view = db.state("cycle-1", Some("2026-10-30")).unwrap();
        let report = db.verify_replay("cycle-1").unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (1, usize::from(quantity.is_some())));
        for _ in 0..2 {
            assert_eq!(db.ingest_quote(input.clone()).unwrap(), pinned);
            let retry = db.submit(req.clone()).unwrap();
            assert!(retry.duplicate);
            assert_eq!(retry.outcome, first.outcome);
            assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), view);
            assert_eq!(db.verify_replay("cycle-1").unwrap(), report);
        }
    }
}

#[test]
fn reference_only_import_cannot_write_a_fill_and_replays_its_rejection() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
        let view = db.state("cycle-1", Some("2026-10-30")).unwrap();
        assert_eq!(view.cash_cents, INITIAL_CASH_CENTS);
        assert!(view.positions.is_empty());
        assert_eq!(view.runs_total, 1);
        let replay = db.verify_replay("cycle-1").unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (1, 0));
    }
}

#[test]
fn execution_import_is_strict_and_all_execution_fields_are_immutable() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
    assert_eq!(saved.schema_version, 5);
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
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
}

#[test]
fn insufficient_capacity_rejection_is_pinned_and_a_new_identity_can_fill() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap().cash_cents, INITIAL_CASH_CENTS);
        assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
    }
    let valid = request("2026-10-30", "capacity-full", Side::Buy);
    let filled = ingest_and_submit(&mut db, valid.clone());
    assert_eq!(filled.outcome.fill.as_ref().unwrap().quantity, 800);
    assert!(db.submit(valid).unwrap().duplicate);
    assert_eq!(db.submit(req).unwrap().outcome, first.outcome);
    let report = db.verify_replay("cycle-1").unwrap();
    assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap().runs_total, 2);
}

#[test]
fn one_sided_quote_cannot_multiply_fills_within_experiment_or_reopen() {
    let file = DiskDatabase::new();
    let historical;
    let latest;
    let replay;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let buy = request("2026-10-30", "one-buy", Side::Buy);
        let first = ingest_and_submit(&mut db, buy.clone());
        let mut other = buy.clone();
        other.signal_id = "other-buy".into();
        assert_eq!(db.submit(other).unwrap().outcome.reason, "position_already_open");
        historical = db.state("cycle-1", Some("2026-10-30")).unwrap();
        let sell = request("2026-11-02", "one-sell", Side::Sell);
        let sold = ingest_and_submit(&mut db, sell.clone());
        let mut other_sell = sell.clone();
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
        for run in db.runs("cycle-1", 100).unwrap() {
            if let Some(fill) = run.outcome.fill {
                assert!(fill.quantity <= run.evidence.unwrap().execution.unwrap().available_quantity);
                *uses.entry((run.request.quote_ref.source, run.request.quote_ref.quote_id)).or_insert(0) += 1;
            }
        }
        assert_eq!(uses.len(), 2);
        assert!(uses.values().all(|&count| count == 1));
        latest = db.state("cycle-1", None).unwrap();
        assert!(latest.positions.is_empty());
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), historical);
        replay = db.verify_replay("cycle-1").unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (5, 2));
    }
    let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), historical);
        assert_eq!(db.state("cycle-1", None).unwrap(), latest);
        assert_eq!(db.verify_replay("cycle-1").unwrap(), replay);
    }
}

#[test]
fn changed_execution_evidence_is_detected_without_rewriting_facts() {
    for (field, value) in [
        ("price_cents", serde_json::json!(1250)), ("available_quantity", serde_json::json!(799)),
        ("order_side", serde_json::json!("sell")), ("observed_at", serde_json::json!("2026-10-30T09:59:29+08:00")),
    ] {
        let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "tamper-execution", Side::Buy);
        let first = ingest_and_submit(&mut db, req);
        let mut raw = serde_json::to_value(first.evidence.unwrap()).unwrap();
        raw["execution"][field] = value;
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [raw.to_string()]).unwrap();
        let changes = db.conn.total_changes();
        for _ in 0..2 { assert!(db.verify_replay("cycle-1").is_err()); }
        assert_eq!(db.conn.total_changes(), changes);
        assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 1);
    }
}

#[test]
fn prior_reference_price_policy_file_is_refused_without_disk_mutation() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "old-policy", Side::Buy));
        db.conn.execute("UPDATE v2_experiments SET spec_json=json_set(spec_json,'$.execution_policy_version','v2-paper-policy-4')", []).unwrap();
    }
    Connection::open(file.path()).unwrap().execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
    let before = disk_snapshot(&file);
    for _ in 0..2 {
        assert!(test_open(file.path(), INITIAL_CASH_CENTS).is_err());
        assert_eq!(disk_snapshot(&file), before);
    }
}

#[test]
fn dedup_is_exact_and_does_not_duplicate_fills() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    let first = ingest_and_submit(&mut db, req.clone());
    let second = db.submit(req).unwrap();
    assert!(!first.duplicate);
    assert!(second.duplicate);
    assert_eq!(first.id, second.id);
    assert_eq!(first.evidence, second.evidence);
    assert_eq!(first.outcome, second.outcome);
    let view = db.state("cycle-1", Some("2026-10-30")).unwrap();
    assert_eq!(view.runs_total, 1);
    assert_eq!(view.positions.len(), 1);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
    assert_eq!(db.runs("cycle-1", 10).unwrap()[0], first);
}

#[test]
fn conflicting_payload_under_same_identity_is_rejected() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-10-30", "b1", Side::Buy));
    let mut changed = request("2026-10-30", "b1", Side::Buy);
    changed.quote_ref.quote_id = "another-price".into();
    assert!(matches!(db.submit(changed), Err(StoreError::Conflict(_))));
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 1);
}

#[test]
fn replay_detects_tampered_fill() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-10-30", "b1", Side::Buy));
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
    db.conn.execute("UPDATE v2_fills SET fill_json='{}' WHERE id=1", []).unwrap();
    assert!(db.verify_replay("cycle-1").is_err());
}

#[test]
fn out_of_order_decision_does_not_mutate_the_ledger() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-11-02", "later", Side::Buy));
    let earlier = request("2026-10-30", "earlier", Side::Sell);
    assert!(matches!(db.submit(earlier), Err(StoreError::Conflict(_))));
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 1);
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 1);
}

#[test]
fn rejection_is_persisted_without_fill() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let out = ingest_and_submit(&mut db, request("2026-11-02", "s1", Side::Sell));
    assert_eq!(out.outcome.status, DecisionStatus::Rejected);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
}

#[test]
fn buy_and_next_day_sell_replay() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    ingest_and_submit(&mut db, request("2026-10-30", "b1", Side::Buy));
    let blocked = ingest_and_submit(&mut db, request("2026-10-30", "s0", Side::Sell));
    assert_eq!(blocked.outcome.reason, "no_t1_sellable_lot");
    let sold = ingest_and_submit(&mut db, request("2026-11-02", "s1", Side::Sell));
    assert_eq!(sold.outcome.status, DecisionStatus::Filled);
    assert_eq!(db.state("cycle-1", Some("2026-11-02")).unwrap().positions.len(), 0);
    let verified = db.verify_replay("cycle-1").unwrap();
    assert_eq!((verified.runs_verified, verified.fills_verified), (3, 2));
}

#[test]
fn historical_state_excludes_future_buys_sells_and_decisions() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let empty = db.state("cycle-1", Some("2026-10-29")).unwrap();
    let buy = request("2026-10-30", "pit-buy", Side::Buy);
    let bought = ingest_and_submit(&mut db, buy.clone());
    let buy_day = db.state("cycle-1", Some("2026-10-30")).unwrap();
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
    let replay = db.verify_replay("cycle-1").unwrap();
    for _ in 0..3 {
        assert_eq!(db.state("cycle-1", Some("2026-10-29")).unwrap(), empty);
        assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), buy_day_with_rejection);
        let held = db.state("cycle-1", Some("2026-11-01")).unwrap();
        assert_eq!(held.positions[0].quantity, 800);
        assert_eq!(held.positions[0].sellable_quantity, Some(800));
        assert_eq!(held.cash_cents, buy_day_with_rejection.cash_cents);
        assert_eq!(held.runs_total, 2);
        let sell_day = db.state("cycle-1", Some("2026-11-02")).unwrap();
        assert!(sell_day.positions.is_empty() && sell_day.lots.is_empty());
        assert_eq!(sell_day.cash_cents, sold.outcome.cash_after_cents);
        assert_eq!(sell_day.runs_total, 3);
        assert_eq!(db.state("cycle-1", None).unwrap().cash_cents, sell_day.cash_cents);
        assert_eq!(db.verify_replay("cycle-1").unwrap(), replay);
    }
    assert_eq!(db.conn.total_changes(), changes);
}

#[test]
fn historical_state_uses_china_day_end_including_rejected_decisions() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let mut last = request("2026-10-30", "pit-last-nanosecond", Side::Buy);
    last.decision_at = "2026-10-30T23:59:59.999999999+08:00".into();
    // Missing quote is an immutable rejected decision and still counts on its day.
    assert_eq!(db.submit(last).unwrap().outcome.status, DecisionStatus::Rejected);
    let end = db.state("cycle-1", Some("2026-10-30")).unwrap();
    assert_eq!(end.runs_total, 1);
    let mut next = request("2026-10-31", "pit-next-midnight", Side::Buy);
    // This instant is still October 30 in UTC, but the next China date.
    next.decision_at = "2026-10-31T00:00:00+08:00".into();
    db.submit(next).unwrap();
    assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), end);
    assert_eq!(db.state("cycle-1", Some("2026-10-31")).unwrap().runs_total, 2);
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 2);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
}

#[test]
fn historical_state_survives_disk_reopen_without_writes_or_reinterpretation() {
    let file = DiskDatabase::new();
    let historical;
    let latest;
    let replay;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        historical = {
            ingest_and_submit(&mut db, request("2026-10-30", "pit-disk-buy", Side::Buy));
            db.state("cycle-1", Some("2026-10-30")).unwrap()
        };
        ingest_and_submit(&mut db, request("2026-11-02", "pit-disk-sell", Side::Sell));
        latest = db.state("cycle-1", None).unwrap();
        replay = db.verify_replay("cycle-1").unwrap();
    }
    let before = disk_snapshot(&file);
    {
        let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let changes = db.conn.total_changes();
        for _ in 0..2 {
            assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), historical);
            assert_eq!(db.state("cycle-1", None).unwrap(), latest);
            assert_eq!(db.verify_replay("cycle-1").unwrap(), replay);
        }
        assert_eq!(db.conn.total_changes(), changes);
    }
    assert_eq!(disk_snapshot(&file), before);
}

#[test]
fn historical_state_empty_history_and_invalid_dates_are_read_only() {
    let db = test_store(INITIAL_CASH_CENTS).unwrap();
    let changes = db.conn.total_changes();
    for day in ["2026-10-29", "2025-01-01", "2027-01-01"] {
        let view = db.state("cycle-1", Some(day)).unwrap();
        assert_eq!(view.cash_cents, INITIAL_CASH_CENTS);
        assert!(view.positions.is_empty() && view.lots.is_empty());
        assert_eq!(view.runs_total, 0);
    }
    for day in ["bad", "2026-02-30", "2026-1-1", "2026-10-30T10:00:00+08:00"] {
        for _ in 0..2 { assert!(db.state("cycle-1", Some(day)).is_err(), "{day}"); }
    }
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 0);
    assert_eq!(db.conn.total_changes(), changes);
}

#[test]
fn quote_ingestion_is_idempotent_and_every_evidence_field_is_immutable() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
    assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
}

#[test]
fn invalid_ingress_never_writes_and_cannot_be_retried_into_success() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let good = evidence(&request("2026-10-30", "b1", Side::Buy));
    let mut variants = Vec::new();
    let mut bad = good.clone();
    bad.quote.source = "claimed-live-provider".into();
    variants.push(bad);
    let mut bad = good.clone();
    bad.schema_version = 1;
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
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 0);
}

#[test]
fn missing_quote_rejection_stays_pinned_after_later_ingestion() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.reason, "quote_evidence_missing");
    assert!(first.evidence.is_none());
    db.ingest_quote(evidence(&req)).unwrap();
    let retry = db.submit(req.clone()).unwrap();
    assert!(retry.duplicate);
    assert_eq!(retry.outcome, first.outcome);
    assert!(retry.evidence.is_none());
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
    let mut next = req;
    next.signal_id = "b2".into();
    assert_eq!(db.submit(next).unwrap().outcome.status, DecisionStatus::Filled);
    for _ in 0..2 {
        let replay = db.verify_replay("cycle-1").unwrap();
        assert_eq!((replay.runs_verified, replay.fills_verified), (2, 1));
    }
}

#[test]
fn late_evidence_rejection_and_exact_availability_boundary_replay() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
        let report = db.verify_replay("cycle-1").unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

#[test]
fn source_is_part_of_quote_identity_and_cannot_resolve_another_source() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let mut req = request("2026-10-30", "b1", Side::Buy);
    db.ingest_quote(evidence(&req)).unwrap();
    req.quote_ref.source = "forged-provider".into();
    let result = db.submit(req.clone()).unwrap();
    assert_eq!(result.outcome.reason, "quote_evidence_missing");
    assert_eq!(db.submit(req).unwrap().outcome, result.outcome);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
}

#[test]
fn replay_detects_changed_or_deleted_evidence_for_fills_and_rejections() {
    for late in [false, true] {
        let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "b1", Side::Buy);
        let mut quote = evidence(&req);
        if late { quote.available_at = "2026-10-30T10:00:01+08:00".into(); }
        let mut stored = db.ingest_quote(quote.clone()).unwrap();
        db.submit(req).unwrap();
        assert!(db.verify_replay("cycle-1").unwrap().ok);
        let original = serde_json::to_string(&stored).unwrap();
        stored.quote.price_cents += 1;
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [serde_json::to_string(&stored).unwrap()]).unwrap();
        assert!(db.verify_replay("cycle-1").is_err());
        db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [original]).unwrap();
        assert!(db.verify_replay("cycle-1").unwrap().ok);
        db.conn.execute("DELETE FROM v2_quote_evidence", []).unwrap();
        assert!(db.verify_replay("cycle-1").is_err());
    }
}

#[test]
fn fill_failure_rolls_back_decision_but_retains_independent_evidence() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let req = request("2026-10-30", "b1", Side::Buy);
    db.ingest_quote(evidence(&req)).unwrap();
    let historical = db.state("cycle-1", Some("2026-10-30")).unwrap();
    db.conn.execute_batch("CREATE TRIGGER block_fill BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT,'test failure'); END;").unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
    assert_eq!(db.state("cycle-1", None).unwrap().cash_cents, INITIAL_CASH_CENTS);
    assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap(), historical);
    assert!(load_evidence(&db.conn, &req.quote_ref).unwrap().is_some());
    db.conn.execute_batch("DROP TRIGGER block_fill;").unwrap();
    assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
    assert_eq!(db.state("cycle-1", Some("2026-10-30")).unwrap().runs_total, 1);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 1);
}

#[test]
fn evidence_and_missing_snapshots_survive_store_reopen() {
    let path = format!("file:v2-evidence-reopen-{}?mode=memory&cache=shared", std::process::id());
    let _keeper = Connection::open(&path).unwrap();
    let req = request("2026-10-30", "missing", Side::Buy);
    {
        let mut db = test_open(&path, INITIAL_CASH_CENTS).unwrap();
        assert_eq!(db.submit(req.clone()).unwrap().outcome.reason, "quote_evidence_missing");
        db.ingest_quote(evidence(&req)).unwrap();
    }
    let mut reopened = test_open(&path, INITIAL_CASH_CENTS).unwrap();
    let retry = reopened.submit(req.clone()).unwrap();
    assert!(retry.duplicate && retry.evidence.is_none());
    let mut next = req;
    next.signal_id = "after-reopen".into();
    let filled = reopened.submit(next).unwrap();
    assert_eq!(filled.outcome.status, DecisionStatus::Filled);
    drop(reopened);
    let reopened = test_open(&path, INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        let report = reopened.verify_replay("cycle-1").unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

#[test]
fn old_policy_database_is_refused_before_adding_evidence_schema() {
    let path = format!("file:v2-old-policy-{}?mode=memory&cache=shared", std::process::id());
    let keeper = Connection::open(&path).unwrap();
    keeper.execute_batch("CREATE TABLE v2_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO v2_meta VALUES('policy_version','v2-paper-policy-1');").unwrap();
    assert!(matches!(Store::with_connection(Connection::open(&path).unwrap()), Err(StoreError::Integrity(_))));
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
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
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
    let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
    assert_eq!(db.ingest_quote(input).unwrap(), saved);
    let retry = db.submit(req).unwrap();
    assert!(retry.duplicate);
    assert_eq!(retry.evidence, Some(saved));
    let first_replay = db.verify_replay("cycle-1").unwrap();
    assert_eq!((first_replay.runs_verified, first_replay.fills_verified), (1, 1));
    assert_eq!(db.verify_replay("cycle-1").unwrap(), first_replay);
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
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let saved = db.ingest_quote(input).unwrap();
    assert!(serde_json::from_value::<QuoteImport>(serde_json::to_value(saved).unwrap()).is_err());
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 0);
}

#[test]
fn replay_detects_changed_receipt_or_trust_even_without_price_change() {
    for late in [false, true] {
        let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
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
            assert!(db.verify_replay("cycle-1").is_err());
            db.conn.execute("UPDATE v2_quote_evidence SET evidence_json=?", [&original]).unwrap();
            assert!(db.verify_replay("cycle-1").unwrap().ok);
        }
        assert_eq!(db.verify_replay("cycle-1").unwrap(), db.verify_replay("cycle-1").unwrap());
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
        assert!(test_open(file.path(), INITIAL_CASH_CENTS).is_err(), "foreign database accepted");
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
fn current_v2_with_foreign_schema_or_previous_schema_version_is_untouched() {
    for foreign in [false, true] {
        let file = DiskDatabase::new();
        {
            let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
            ingest_and_submit(&mut db, request("2026-10-30", "disk-buy", Side::Buy));
        }
        {
            let conn = Connection::open(file.path()).unwrap();
            conn.execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
            if foreign {
                conn.execute_batch("CREATE TABLE unexpected(payload TEXT); INSERT INTO unexpected VALUES('keep');").unwrap();
            } else {
                conn.pragma_update(None, "user_version", 6).unwrap();
            }
        }
        let before = disk_snapshot(&file);
        for _ in 0..2 {
            assert!(Store::open(file.path()).is_err());
            assert_eq!(disk_snapshot(&file), before);
        }
    }
}

#[test]
fn empty_disk_initialization_reopen_and_millisecond_rejections_replay() {
    let file = DiskDatabase::new();
    std::fs::write(&file.0, []).unwrap();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
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
    let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        let report = db.verify_replay("cycle-1").unwrap();
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
        let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
        let (req, input) = market_input(day, time, "closed");
        db.ingest_quote(input).unwrap();
        let first = db.submit(req.clone()).unwrap();
        assert_eq!(first.outcome.reason, reason, "{day} {time}");
        assert_eq!(first.outcome.status, DecisionStatus::Rejected);
        let retry = db.submit(req).unwrap();
        assert!(retry.duplicate);
        assert_eq!(retry.id, first.id);
        assert_eq!(retry.outcome, first.outcome);
        assert_eq!(db.state("cycle-1", None).unwrap().cash_cents, INITIAL_CASH_CENTS);
        for _ in 0..2 {
            let report = db.verify_replay("cycle-1").unwrap();
            assert_eq!((report.runs_verified, report.fills_verified), (1, 0));
        }
    }
}

#[test]
fn closed_day_decision_write_failure_rolls_back_then_retries_once() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let (req, input) = market_input("2026-10-31", "10:00:00", "rollback-closed");
    db.ingest_quote(input).unwrap();
    db.conn.execute_batch("CREATE TRIGGER block_run BEFORE INSERT ON v2_simulation_runs
        BEGIN SELECT RAISE(ABORT,'test failure'); END;").unwrap();
    assert!(db.submit(req.clone()).is_err());
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
    assert_eq!(db.state("cycle-1", None).unwrap().cash_cents, INITIAL_CASH_CENTS);
    db.conn.execute_batch("DROP TRIGGER block_run;").unwrap();
    let first = db.submit(req.clone()).unwrap();
    assert_eq!(first.outcome.reason, "weekend_market_closed");
    assert!(db.submit(req).unwrap().duplicate);
    assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 0);
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 1);
}

#[test]
fn fixed_market_snapshots_and_run_references_survive_disk_reopen() {
    let file = DiskDatabase::new();
    let snapshots;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
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
            db.conn.query_row("SELECT json_extract(pack_json,'$.calendar') FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-1'", [], |r| r.get::<_, String>(0)).unwrap(),
            db.conn.query_row("SELECT json_extract(pack_json,'$.sessions') FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-1'", [], |r| r.get::<_, String>(0)).unwrap(),
        );
    }
    let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
    let (calendar, sessions, _) = load_market_context(&db.conn, MARKET_DATA_PACK_ID).unwrap();
    assert_eq!(serde_json::from_str::<TradingCalendar>(&snapshots.0).unwrap(), calendar);
    assert_eq!(serde_json::from_str::<SessionPolicy>(&snapshots.1).unwrap(), sessions);
    for _ in 0..2 {
        let report = db.verify_replay("cycle-1").unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}

#[test]
fn changed_market_snapshots_or_run_versions_never_reinterpret_history() {
    for day in ["2026-10-30", "2026-10-31"] {
        let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
        let (req, input) = market_input(day, "10:00:00", "pin");
        db.ingest_quote(input).unwrap();
        let saved = db.submit(req.clone()).unwrap();
        let state = db.state("cycle-1", None).unwrap();
        for key in ["calendar", "sessions"] {
            let original: String = db.conn.query_row("SELECT json_extract(pack_json, '$.' || ?) FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-1'", [key], |r| r.get(0)).unwrap();
            let mut changed: serde_json::Value = serde_json::from_str(&original).unwrap();
            if key == "calendar" {
                changed["holidays"][6]["end"] = serde_json::json!("2026-10-08");
            } else {
                changed["continuous"][0]["end"] = serde_json::json!("11:31:00");
            }
            db.conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json, '$.' || ?2,json(?1)) WHERE pack_id='cn-offline-fixture-pack-1'", params![changed.to_string(), key]).unwrap();
            assert!(db.verify_replay("cycle-1").is_err());
            let mut next = req.clone();
            next.signal_id = "next".into();
            assert!(db.submit(next).is_err());
            assert!(db.state("cycle-1", None).is_err());
            db.conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json, '$.' || ?2,json(?1)) WHERE pack_id='cn-offline-fixture-pack-1'", params![original, key]).unwrap();
            assert!(db.verify_replay("cycle-1").unwrap().ok);
        }
        for (column, original) in [
            ("calendar_version", saved.calendar_version.as_str()),
            ("session_policy_version", saved.session_policy_version.as_str()),
        ] {
            db.conn.execute(&format!("UPDATE v2_simulation_runs SET {column}='later-version'"), []).unwrap();
            assert!(db.verify_replay("cycle-1").is_err());
            assert!(db.submit(req.clone()).is_err());
            db.conn.execute(&format!("UPDATE v2_simulation_runs SET {column}=?"), [original]).unwrap();
            assert!(db.verify_replay("cycle-1").unwrap().ok);
        }
        assert_eq!(db.runs("cycle-1", 10).unwrap()[0], saved);
        assert_eq!(db.verify_replay("cycle-1").unwrap(), db.verify_replay("cycle-1").unwrap());
    }
}

#[test]
fn invalid_pinned_calendar_is_refused_before_wal_or_schema_writes() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "keep", Side::Buy));
    }
    {
        let conn = Connection::open(file.path()).unwrap();
        conn.execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
        let raw: String = conn.query_row("SELECT json_extract(pack_json,'$.calendar') FROM v2_market_data_packs WHERE pack_id='cn-offline-fixture-pack-1'", [], |r| r.get(0)).unwrap();
        let mut changed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        changed["coverage_end"] = serde_json::json!("2026-12-30");
        conn.execute("UPDATE v2_market_data_packs SET pack_json=json_set(pack_json,'$.calendar',json(?)) WHERE pack_id='cn-offline-fixture-pack-1'", [changed.to_string()]).unwrap();
    }
    let before = disk_snapshot(&file);
    for _ in 0..2 {
        assert!(test_open(file.path(), INITIAL_CASH_CENTS).is_err());
        assert_eq!(disk_snapshot(&file), before);
    }
}

fn reviewed_artifact() -> StrategyArtifact {
    serde_json::from_str(include_str!("../../data/strategy-momentum-v1.json")).unwrap()
}

fn reversion_artifact() -> StrategyArtifact {
    serde_json::from_str(include_str!("../../data/strategy-reversion-v1.json")).unwrap()
}

fn spec(cycle_id: &str, initial_cash_cents: i64) -> ExperimentSpec {
    ExperimentSpec {
        schema_version: 1, cycle_id: cycle_id.into(), initial_cash_cents,
        strategy: reviewed_artifact(),
        market_data_pack_id: MARKET_DATA_PACK_ID.into(), execution_policy_version: POLICY_VERSION.into(),
    }
}

// Test setup only: production has no implicit experiment or seed fallback.
fn test_store(cash: i64) -> StoreResult<Store> {
    let mut db = Store::in_memory()?;
    db.create_experiment(spec("cycle-1", cash))?;
    Ok(db)
}
fn test_open(path: &str, cash: i64) -> StoreResult<Store> {
    let mut db = Store::open(path)?;
    db.create_experiment(spec("cycle-1", cash))?;
    Ok(db)
}


// --- Frozen research input and reproducible research output ---------------

fn frozen_manifest() -> ResearchInputManifest {
    serde_json::from_str(include_str!("../../data/research-input-offline-v1.json")).unwrap()
}

fn frozen_momentum_output() -> ResearchOutput {
    serde_json::from_str(include_str!("../../data/research-output-momentum-v1.json")).unwrap()
}

fn frozen_quote_ref(signal: &crate::domain::ResearchSignal) -> QuoteRef {
    QuoteRef {
        market_data_pack_id: signal.market_data_pack_id.clone(),
        source: signal.source.clone(),
        quote_id: signal.quote_id.clone(),
    }
}

fn frozen_evidence(pack: &str, entry: &crate::domain::ResearchInputEntry, side: Side) -> QuoteImport {
    let reference = QuoteRef {
        market_data_pack_id: pack.into(),
        source: entry.quote.source.clone(),
        quote_id: entry.quote.quote_id.clone(),
    };
    QuoteImport {
        market_data_pack_id: pack.into(),
        schema_version: 2,
        available_at: entry.available_at.clone(),
        quote: entry.quote.clone(),
        execution: Some(ExecutionLiquidity {
            schema_version: 2,
            quote_ref: reference,
            order_side: side.clone(),
            price_cents: entry.level(&side).price_cents,
            available_quantity: entry.level(&side).available_quantity,
            observed_at: entry.quote.observed_at.clone(),
        }),
    }
}

fn frozen_request(cycle_id: &str, signal: &crate::domain::ResearchSignal) -> SimulationRequest {
    SimulationRequest {
        schema_version: 4,
        cycle_id: cycle_id.into(),
        strategy_version_id: "research-momentum-v1".into(),
        signal_id: signal.signal_id.clone(),
        decision_at: signal.decision_at.clone(),
        signal: Signal { symbol: signal.symbol.clone(), side: signal.side.clone() },
        quote_ref: frozen_quote_ref(signal),
    }
}

fn realise_frozen_signals(db: &mut Store, cycle_id: &str, output: &ResearchOutput) {
    let manifest = frozen_manifest();
    for signal in &output.signals {
        let entry = manifest.entry_for(&frozen_quote_ref(signal)).expect("frozen entry");
        db.ingest_quote(frozen_evidence(MARKET_DATA_PACK_ID, entry, signal.side.clone())).unwrap();
        let _ = db.submit(frozen_request(cycle_id, signal)).unwrap();
    }
}

fn fills_of(db: &Store, cycle_id: &str) -> Vec<Fill> {
    let mut statement = db.conn.prepare(
        "SELECT f.fill_json FROM v2_fills f JOIN v2_simulation_runs r ON r.id=f.run_id
         WHERE r.cycle_id=? ORDER BY f.id").unwrap();
    statement.query_map([cycle_id], |row| row.get::<_, String>(0)).unwrap()
        .map(|row| serde_json::from_str::<Fill>(&row.unwrap()).unwrap()).collect()
}

#[test]
fn strategy_identity_with_changed_content_is_rejected() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let reviewed = reviewed_artifact();
    // Same id, different reviewed parameters. Digests are recomputed so the
    // artifact is internally consistent: only content identity can reject it.
    let mut changed = reviewed.clone();
    changed.parameters_canonical = "{\"lookback\":3,\"threshold_bps\":25}".into();
    changed.parameters_digest = crate::domain::sha256_hex(changed.parameters_canonical.as_bytes());
    changed.verify_content().unwrap();
    let mut candidate = spec("param-change", INITIAL_CASH_CENTS);
    candidate.strategy = changed;
    assert!(matches!(db.create_experiment(candidate), Err(StoreError::Conflict(_))));

    // Same id, different research source bytes and different frozen input.
    let mut source_changed = reviewed.clone();
    source_changed.source_digest = "0".repeat(64);
    let mut candidate = spec("source-change", INITIAL_CASH_CENTS);
    candidate.strategy = source_changed;
    assert!(matches!(db.create_experiment(candidate), Err(StoreError::Conflict(_))));

    let mut input_changed = reviewed.clone();
    input_changed.research_input_digest = "1".repeat(64);
    let mut candidate = spec("input-change", INITIAL_CASH_CENTS);
    candidate.strategy = input_changed;
    assert!(matches!(db.create_experiment(candidate), Err(StoreError::Conflict(_))));

    // Unreviewed identities never register.
    let mut invented = reviewed.clone();
    invented.strategy_version_id = "research-invented-v1".into();
    let mut candidate = spec("invented", INITIAL_CASH_CENTS);
    candidate.strategy = invented;
    assert!(matches!(db.create_experiment(candidate), Err(StoreError::Rules(_))));

    // A legacy two-field artifact cannot carry content at all.
    assert!(serde_json::from_value::<StrategyArtifact>(serde_json::json!({
        "strategy_version_id": "research-momentum-v1",
        "research_input_id": "offline-research-input-v1",
    })).is_err());

    for cycle in ["param-change", "source-change", "input-change", "invented"] {
        assert!(db.experiment(cycle).is_err(), "{cycle} must not be registered");
    }
    assert_eq!(db.state("cycle-1", None).unwrap().runs_total, 0);
}

#[test]
fn research_reproduction_matches_frozen_signals_and_rust_owned_fills() {
    let file = DiskDatabase::new();
    let output = frozen_momentum_output();
    let facts;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        db.create_experiment(spec("reproduction", INITIAL_CASH_CENTS)).unwrap();
        realise_frozen_signals(&mut db, "cycle-1", &output);
        realise_frozen_signals(&mut db, "reproduction", &output);
        let first = db.verify_research_reproduction("cycle-1").unwrap();
        let second = db.verify_research_reproduction("reproduction").unwrap();
        assert_eq!(first.signals_verified, output.signals.len());
        assert_eq!(first.research_output_digest, output.content_digest);
        assert_eq!(first.strategy_version_id, "research-momentum-v1");
        assert_eq!(first.research_input_id, "offline-research-input-v1");
        assert_eq!(first.parameters_digest, reviewed_artifact().parameters_digest);
        assert_eq!(first.source_digest, reviewed_artifact().source_digest);
        assert_eq!(second.research_output_digest, first.research_output_digest);
        // Same fixed definition and input, two independent experiments: the
        // research identity is equal and Rust's own trading facts are equal.
        facts = fills_of(&db, "cycle-1");
        assert_eq!(facts, fills_of(&db, "reproduction"));
        assert_eq!(facts.len(), 2);
        assert_eq!((facts[0].side.clone(), facts[0].quantity), (Side::Buy, 700));
        assert_eq!((facts[1].side.clone(), facts[1].quantity), (Side::Sell, 700));
        assert_eq!(facts[0].price_cents, 1295);
        assert_eq!(facts[1].price_cents, 1245);
        // Replay and research reproduction are separate proofs, both required.
        assert_eq!(db.verify_replay("cycle-1").unwrap().fills_verified, 2);
        assert_eq!(db.verify_replay("reproduction").unwrap().fills_verified, 2);
        // Idempotent retries are still the frozen signals.
        realise_frozen_signals(&mut db, "cycle-1", &output);
        assert_eq!(db.verify_research_reproduction("cycle-1").unwrap(), first);
    }
    // Fresh process on the same file: the reviewed definitions must still match
    // byte for byte, and the stored facts must still verify.
    let db = Store::open(file.path()).unwrap();
    assert_eq!(db.verify_research_reproduction("cycle-1").unwrap().signals_verified, 2);
    assert!(db.verify_replay("cycle-1").unwrap().ok);
    assert_eq!(fills_of(&db, "cycle-1"), facts);
}

#[test]
fn research_reproduction_refuses_extra_missing_and_substituted_inputs() {
    // Extra decision: replay still accepts it, research reproduction rejects it.
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let output = frozen_momentum_output();
    realise_frozen_signals(&mut db, "cycle-1", &output);
    let entry = frozen_manifest().entries.last().cloned().unwrap();
    let post_hoc = crate::domain::ResearchSignal {
        sequence: entry.sequence,
        signal_id: "post-hoc-signal".into(),
        decision_at: entry.decision_at.clone(),
        symbol: entry.quote.symbol.clone(),
        side: Side::Buy,
        source: entry.quote.source.clone(),
        quote_id: entry.quote.quote_id.clone(),
        market_data_pack_id: MARKET_DATA_PACK_ID.into(),
    };
    db.ingest_quote(frozen_evidence(MARKET_DATA_PACK_ID, &entry, Side::Buy)).unwrap();
    db.submit(frozen_request("cycle-1", &post_hoc)).unwrap();
    assert!(db.verify_replay("cycle-1").unwrap().ok, "replay only follows pinned inputs");
    assert!(matches!(db.verify_research_reproduction("cycle-1"), Err(StoreError::Integrity(_))));

    // Missing decision.
    let mut short = test_store(INITIAL_CASH_CENTS).unwrap();
    let first = output.signals[0].clone();
    let entry = frozen_manifest().entry_for(&frozen_quote_ref(&first)).cloned().unwrap();
    short.ingest_quote(frozen_evidence(MARKET_DATA_PACK_ID, &entry, first.side.clone())).unwrap();
    short.submit(frozen_request("cycle-1", &first)).unwrap();
    assert!(short.verify_research_reproduction("cycle-1").is_err());

    // Substituted evidence content inside the pinned decision snapshot.
    let mut substituted = test_store(INITIAL_CASH_CENTS).unwrap();
    realise_frozen_signals(&mut substituted, "cycle-1", &output);
    substituted.conn.execute(
        "UPDATE v2_simulation_runs SET evidence_json=json_set(evidence_json,'$.quote.price_cents',9999)
         WHERE signal_id='research-momentum-v1-0003-buy'", []).unwrap();
    assert!(substituted.verify_research_reproduction("cycle-1").is_err());
    // Changing the pack-level evidence row instead is replay's business.
    let mut repacked = test_store(INITIAL_CASH_CENTS).unwrap();
    realise_frozen_signals(&mut repacked, "cycle-1", &output);
    repacked.conn.execute(
        "UPDATE v2_quote_evidence SET evidence_json=json_set(evidence_json,'$.quote.price_cents',9999)
         WHERE quote_id='offline-quote-20261030-1359'", []).unwrap();
    assert!(repacked.verify_replay("cycle-1").is_err());
    assert!(repacked.verify_research_reproduction("cycle-1").is_ok());

    // A different reviewed pack cannot host the same research input's signals.
    let mut other_pack = test_store(INITIAL_CASH_CENTS).unwrap();
    let mut pack_spec = spec("pack-two", INITIAL_CASH_CENTS);
    pack_spec.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    other_pack.create_experiment(pack_spec).unwrap();
    other_pack.ingest_quote(frozen_evidence("cn-offline-fixture-pack-2", &entry, first.side.clone())).unwrap();
    let mut elsewhere = frozen_request("pack-two", &first);
    elsewhere.quote_ref.market_data_pack_id = "cn-offline-fixture-pack-2".into();
    other_pack.submit(elsewhere).unwrap();
    assert!(other_pack.verify_research_reproduction("pack-two").is_err());
}

#[test]
fn frozen_research_input_registration_is_immutable_and_tamper_proof() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        assert_eq!(db.experiment("cycle-1").unwrap().status, ExperimentStatus::Active);
        db.create_experiment(spec("second", INITIAL_CASH_CENTS)).unwrap();
        let count: i64 = db.conn.query_row("SELECT count(*) FROM v2_research_inputs", [], |r| r.get(0)).unwrap();
        assert_eq!(count, 1, "one frozen input identity is registered once");
    }
    {
        let conn = Connection::open(file.path()).unwrap();
        let original: String = conn.query_row(
            "SELECT manifest_json FROM v2_research_inputs WHERE research_input_id='offline-research-input-v1'",
            [], |r| r.get(0)).unwrap();
        let mut changed: serde_json::Value = serde_json::from_str(&original).unwrap();
        changed["entries"][2]["quote"]["price_cents"] = serde_json::json!(1);
        conn.execute_batch("PRAGMA journal_mode=DELETE;").unwrap();
        conn.execute("UPDATE v2_research_inputs SET manifest_json=? WHERE research_input_id='offline-research-input-v1'",
            [changed.to_string()]).unwrap();
    }
    let before = disk_snapshot(&file);
    for _ in 0..2 {
        assert!(Store::open(file.path()).is_err(), "tampered frozen input must refuse the file");
        assert_eq!(disk_snapshot(&file), before);
    }
}

#[test]
fn experiment_catalogue_lists_stored_facts_without_writing() {
    let file = DiskDatabase::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        db.create_experiment(spec("aaa-alpha", 5_000_000)).unwrap();
        db.create_experiment(spec("zzz-omega", 7_000_000)).unwrap();
        ingest_and_submit(&mut db, request("2026-10-30", "catalogue-run", Side::Buy));
        let catalogue = db.experiments(100).unwrap();
        assert_eq!(catalogue.total, 3);
        assert_eq!(catalogue.skipped, 0);
        let listed = &catalogue.experiments;
        let cycles: Vec<&str> = listed.iter().map(|row| row.cycle_id.as_str()).collect();
        assert_eq!(cycles, vec!["aaa-alpha", "cycle-1", "zzz-omega"], "stable cycle_id order");
        let cycle = listed.iter().find(|row| row.cycle_id == "cycle-1").unwrap();
        assert_eq!(cycle.status, ExperimentStatus::Active);
        assert_eq!(cycle.strategy_version_id, "research-momentum-v1");
        assert_eq!(cycle.research_input_id, "offline-research-input-v1");
        assert_eq!(cycle.market_data_pack_id, MARKET_DATA_PACK_ID);
        assert_eq!(cycle.execution_policy_version, POLICY_VERSION);
        assert_eq!(cycle.initial_cash_cents, INITIAL_CASH_CENTS);
        assert_eq!(cycle.runs_total, 1);
        // Read-only: listing never changes the ledger, and limits are clamped.
        let replay = db.verify_replay("cycle-1").unwrap();
        assert_eq!(db.experiments(1).unwrap().experiments.len(), 1);
        assert_eq!(db.experiments(0).unwrap().experiments.len(), 1);
        assert_eq!(db.experiments(10_000).unwrap().experiments.len(), 3);
        assert_eq!(db.verify_replay("cycle-1").unwrap(), replay);
        db.complete_experiment("aaa-alpha").unwrap();
        let completed = db.experiments(100).unwrap();
        assert_eq!(completed.experiments[0].status, ExperimentStatus::Completed);
    }
    let db = Store::open(file.path()).unwrap();
    let reopened = db.experiments(100).unwrap();
    assert_eq!(reopened.experiments.len(), 3);
    assert!(reopened.experiments.iter().all(|row| row.initial_cash_cents > 0));
}

#[test]
fn one_unreadable_experiment_row_does_not_hide_the_rest() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_experiment(spec("aaa", 5_000_000)).unwrap();
    db.create_experiment(spec("zzz", 7_000_000)).unwrap();
    db.conn.execute("UPDATE v2_experiments SET spec_json='{}' WHERE cycle_id='aaa'", []).unwrap();
    let catalogue = db.experiments(100).unwrap();
    assert_eq!(catalogue.total, 3);
    assert_eq!(catalogue.skipped, 1);
    let cycles: Vec<&str> = catalogue.experiments.iter().map(|row| row.cycle_id.as_str()).collect();
    assert_eq!(cycles, vec!["cycle-1", "zzz"]);
    // The rest of the read models still work for readable experiments.
    assert_eq!(db.verify_replay("cycle-1").unwrap().runs_verified, 0);
    // A missing experiment is a distinct, non-integrity failure now.
    assert!(matches!(db.experiment("nope"), Err(StoreError::NotFound(_))));
}

// ---------------------------------------------------------------------------
// ADR 005: paper account + dual strategy capital core
// ---------------------------------------------------------------------------

fn paper_slot(slot: PaperSlotId, strategy: StrategyArtifact, weight_bps: i64) -> PaperSlotSpec {
    PaperSlotSpec {
        slot,
        strategy,
        market_data_pack_id: MARKET_DATA_PACK_ID.into(),
        execution_policy_version: POLICY_VERSION.into(),
        weight_bps,
    }
}

fn paper_spec(account_id: &str, cash: i64) -> PaperAccountSpec {
    PaperAccountSpec {
        schema_version: 1,
        account_id: account_id.into(),
        initial_cash_cents: cash,
        reserve_bps: 1_000,
        created_at: "2026-11-08T09:00:00+08:00".into(),
        limits: crate::domain::PaperRiskLimits::default(),
        slots: vec![
            paper_slot(PaperSlotId::A, reviewed_artifact(), 4_500),
            paper_slot(PaperSlotId::B, reversion_artifact(), 4_500),
        ],
    }
}

#[test]
fn paper_capital_split_is_exact_and_the_reserve_absorbs_remainders() {
    let split = paper_spec("acct-split", 10_000_000).validate().unwrap();
    assert_eq!(split.slot_budgets, [4_500_000, 4_500_000]);
    assert_eq!(split.reserve_cents, 1_000_000);

    // 1 cent cannot be split by the weights: it must stay in the reserve.
    let odd = paper_spec("acct-odd", 10_000_001).validate().unwrap();
    assert_eq!(odd.slot_budgets, [4_500_000, 4_500_000]);
    assert_eq!(odd.reserve_cents, 1_000_001);
    assert_eq!(
        odd.reserve_cents + odd.slot_budgets.iter().sum::<i64>(),
        10_000_001
    );
}

#[test]
fn paper_account_rejects_specs_that_cannot_preserve_capital() {
    let mut weights = paper_spec("acct-weights", 10_000_000);
    weights.slots[1].weight_bps = 4_000;
    assert!(matches!(weights.validate(), Err(RuleError(ref code)) if code == "paper_account_weights_must_sum_to_total"));

    let mut duplicate_slot = paper_spec("acct-dupe-slot", 10_000_000);
    duplicate_slot.slots[1].slot = PaperSlotId::A;
    assert!(matches!(duplicate_slot.validate(), Err(RuleError(ref code)) if code == "duplicate_paper_slot"));

    let mut duplicate_strategy = paper_spec("acct-dupe-strategy", 10_000_000);
    duplicate_strategy.slots[1].strategy = reviewed_artifact();
    assert!(matches!(duplicate_strategy.validate(), Err(RuleError(ref code)) if code == "duplicate_strategy_in_paper_account"));

    let mut zero_cash = paper_spec("acct-zero", 0);
    zero_cash.initial_cash_cents = 0;
    assert!(matches!(zero_cash.validate(), Err(RuleError(ref code)) if code == "invalid_initial_cash"));

    let mut bad_time = paper_spec("acct-time", 10_000_000);
    bad_time.created_at = "2026-11-08 09:00".into();
    assert!(matches!(bad_time.validate(), Err(RuleError(ref code)) if code == "invalid_created_at"));

    let mut bad_reserve = paper_spec("acct-reserve", 10_000_000);
    bad_reserve.reserve_bps = 10_000;
    assert!(matches!(bad_reserve.validate(), Err(RuleError(ref code)) if code == "invalid_reserve_bps"));
}

#[test]
fn paper_account_is_idempotent_conflict_checked_and_survives_reopen() {
    let file = DiskDatabase::new();
    let spec = paper_spec("acct-main", 10_000_000);
    let created;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        created = db.create_paper_account(spec.clone()).unwrap();
        assert_eq!(created.initial_cash_cents, 10_000_000);
        assert_eq!(created.reserve_cents, 1_000_000);
        assert_eq!(created.cash_cents, 10_000_000);
        assert_eq!(created.slots.len(), 2);
        assert_eq!(created.slots[0].slot, "A");
        assert_eq!(created.slots[1].slot, "B");
        assert_eq!(created.slots[0].budget_cents, 4_500_000);
        assert_eq!(created.slots[1].budget_cents, 4_500_000);
        assert!(created.slots.iter().all(|slot| slot.positions.is_empty()));
        assert_eq!(created.slots[0].cash_cents, created.slots[0].budget_cents);
        assert_ne!(created.slots[0].strategy_version_id, created.slots[1].strategy_version_id);

        // Capital conservation: no cash exists outside the reserve and the slots.
        let accounted = created.reserve_cents + created.slots.iter().map(|slot| slot.cash_cents).sum::<i64>();
        assert_eq!(accounted, created.initial_cash_cents);

        // Identical re-post is idempotent; a different account under the same id conflicts.
        let again = db.create_paper_account(spec.clone()).unwrap();
        assert_eq!(again, created);
        let mut changed = spec.clone();
        changed.initial_cash_cents = 20_000_000;
        assert!(matches!(db.create_paper_account(changed), Err(StoreError::Conflict(_))));
    }
    {
        let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let reopened = db.paper_account("acct-main").unwrap();
        assert_eq!(reopened, created, "a restart must not change the split");
        assert!(matches!(db.paper_account("acct-missing"), Err(StoreError::NotFound(_))));
    }
}

// ---------------------------------------------------------------------------
// ADR 005 order path: slot-owned orders, fills and capital conservation
// ---------------------------------------------------------------------------

fn paper_order(account: &str, slot: PaperSlotId, signal: &crate::domain::ResearchSignal) -> PaperOrderRequest {
    PaperOrderRequest {
        schema_version: 1,
        account_id: account.into(),
        slot,
        signal_id: signal.signal_id.clone(),
        decision_at: signal.decision_at.clone(),
        signal: Signal { symbol: signal.symbol.clone(), side: signal.side.clone() },
        quote_ref: frozen_quote_ref(signal),
    }
}

fn ingest_signal_evidence(db: &mut Store, signal: &crate::domain::ResearchSignal) {
    let manifest = frozen_manifest();
    let entry = manifest.entry_for(&frozen_quote_ref(signal)).expect("frozen entry");
    db.ingest_quote(frozen_evidence(MARKET_DATA_PACK_ID, entry, signal.side.clone())).unwrap();
}

#[test]
fn paper_order_charges_only_its_slot_and_keeps_capital_conserved() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("paper-main", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);

    let record = db.submit_paper_order(paper_order("paper-main", PaperSlotId::A, buy)).unwrap();
    assert_eq!(record.outcome.status, DecisionStatus::Filled);
    let fill = record.outcome.fill.as_ref().expect("filled");
    assert!(fill.quantity > 0 && fill.quantity % 100 == 0);
    let bought = fill.quantity;
    let spent = fill.quantity * fill.price_cents + fill.fees_cents;

    let view = db.paper_account("paper-main").unwrap();
    let slot_a = &view.slots[0];
    let slot_b = &view.slots[1];
    assert_eq!(slot_a.budget_cents, 4_500_000);
    assert_eq!(slot_a.cash_cents, 4_500_000 - spent);
    assert_eq!(slot_b.cash_cents, 4_500_000);
    assert_eq!(view.reserve_cents, 1_000_000);
    assert_eq!(slot_a.positions.len(), 1);
    assert_eq!(slot_a.positions[0].symbol, "600000.SH");
    assert_eq!(slot_a.positions[0].quantity, bought);
    assert!(slot_b.positions.is_empty());
    assert_eq!(slot_a.orders_total, 1);
    assert_eq!(slot_b.orders_total, 0);
    // Cash conservation: the reserve and both slots explain every remaining cent.
    assert_eq!(
        view.reserve_cents + slot_a.cash_cents + slot_b.cash_cents,
        view.initial_cash_cents - spent
    );
    assert_eq!(view.cash_cents, view.reserve_cents + slot_a.cash_cents + slot_b.cash_cents);

    // The same signal is idempotent; a different payload under the same identity conflicts.
    let again = db.submit_paper_order(paper_order("paper-main", PaperSlotId::A, buy)).unwrap();
    assert!(again.duplicate);
    assert_eq!(again.id, record.id);
    let mut conflicting = paper_order("paper-main", PaperSlotId::A, buy);
    conflicting.decision_at = "2026-10-30T14:00:01+08:00".into();
    assert!(matches!(
        db.submit_paper_order(conflicting),
        Err(StoreError::Conflict(ref code)) if code == "same_signal_identity_with_different_paper_request"
    ));
}

#[test]
fn paper_slots_are_isolated_and_append_only() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("paper-iso", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let first = &output.signals[0];
    let second = &output.signals[1];
    ingest_signal_evidence(&mut db, first);
    ingest_signal_evidence(&mut db, second);

    // Slot B can hold the same signal identity without touching slot A.
    db.submit_paper_order(paper_order("paper-iso", PaperSlotId::A, first)).unwrap();
    let view = db.paper_account("paper-iso").unwrap();
    let a_after_first = view.slots[0].cash_cents;
    db.submit_paper_order(paper_order("paper-iso", PaperSlotId::B, first)).unwrap();
    let view = db.paper_account("paper-iso").unwrap();
    assert_eq!(view.slots[0].cash_cents, a_after_first);
    assert!(view.slots[1].cash_cents < view.slots[1].budget_cents);

    // A later decision, then an earlier one in the same slot: append-only refuses it.
    db.submit_paper_order(paper_order("paper-iso", PaperSlotId::A, second)).unwrap();
    let mut earlier = paper_order("paper-iso", PaperSlotId::A, first);
    earlier.signal_id = "paper-earlier-synthetic".into();
    assert!(matches!(
        db.submit_paper_order(earlier),
        Err(StoreError::Conflict(ref code)) if code == "out_of_order_paper_decision"
    ));

    let orders = db.paper_orders("paper-iso", 10).unwrap();
    assert_eq!(orders.len(), 3);
    assert!(orders[0].id > orders[1].id);
}

#[test]
fn paper_order_without_evidence_is_rejected_and_changes_nothing() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("paper-no-evidence", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];

    let record = db.submit_paper_order(paper_order("paper-no-evidence", PaperSlotId::A, buy)).unwrap();
    assert_eq!(record.outcome.status, DecisionStatus::Rejected);
    assert_eq!(record.outcome.reason, "quote_evidence_missing");
    assert!(record.outcome.fill.is_none());

    let view = db.paper_account("paper-no-evidence").unwrap();
    assert_eq!(view.cash_cents, view.initial_cash_cents);
    assert_eq!(view.slots[0].cash_cents, view.slots[0].budget_cents);
    assert!(view.slots[0].positions.is_empty());
}

#[test]
fn paper_account_with_orders_survives_reopen_unchanged() {
    let file = DiskDatabase::new();
    let output = frozen_momentum_output();
    let before;
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        db.create_paper_account(paper_spec("paper-reopen", 10_000_000)).unwrap();
        let buy = &output.signals[0];
        ingest_signal_evidence(&mut db, buy);
        db.submit_paper_order(paper_order("paper-reopen", PaperSlotId::A, buy)).unwrap();
        before = db.paper_account("paper-reopen").unwrap();
        assert!(before.slots[0].cash_cents < before.slots[0].budget_cents);
    }
    {
        let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let after = db.paper_account("paper-reopen").unwrap();
        assert_eq!(after, before, "restart must not change account facts");
        assert_eq!(db.paper_orders("paper-reopen", 10).unwrap().len(), 1);
    }
}

// ---------------------------------------------------------------------------
// ADR 005 phase 2: deterministic paper sessions
// ---------------------------------------------------------------------------

fn session_signal(slot: PaperSlotId, signal: &crate::domain::ResearchSignal) -> PaperSessionSignal {
    PaperSessionSignal {
        slot,
        signal_id: signal.signal_id.clone(),
        decision_at: signal.decision_at.clone(),
        signal: Signal { symbol: signal.symbol.clone(), side: signal.side.clone() },
        quote_ref: frozen_quote_ref(signal),
    }
}

fn session_spec(account: &str, session: &str, signals: Vec<PaperSessionSignal>) -> PaperSessionSpec {
    PaperSessionSpec {
        schema_version: 1,
        session_id: session.into(),
        account_id: account.into(),
        start_at: "2026-10-30T00:00:00+08:00".into(),
        signals,
    }
}

#[test]
fn paper_session_steps_one_frozen_signal_at_a_time() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("session-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    for signal in &output.signals {
        ingest_signal_evidence(&mut db, signal);
    }
    let spec = session_spec(
        "session-acct",
        "session-1",
        vec![
            session_signal(PaperSlotId::A, &output.signals[0]),
            session_signal(PaperSlotId::B, &output.signals[1]),
        ],
    );
    let created = db.create_paper_session(spec.clone()).unwrap();
    assert_eq!(created.status, "created");
    assert_eq!(created.cursor, 0);
    assert_eq!(created.total, 2);
    assert_eq!(created.clock_at, spec.start_at);
    assert_eq!(db.create_paper_session(spec.clone()).unwrap(), created);
    let mut changed = spec.clone();
    changed.start_at = "2026-10-30T01:00:00+08:00".into();
    assert!(matches!(db.create_paper_session(changed), Err(StoreError::Conflict(_))));

    // A created session does not trade.
    assert!(matches!(
        db.step_paper_session("session-1"),
        Err(StoreError::Conflict(ref code)) if code == "paper_session_not_running"
    ));
    let running = db.paper_session_action("session-1", "start").unwrap();
    assert_eq!(running.status, "running");

    let first = db.step_paper_session("session-1").unwrap();
    let first_order = first.order.expect("first order");
    assert_eq!(first_order.outcome.status, DecisionStatus::Filled);
    assert_eq!(first.session.cursor, 1);
    assert_eq!(first.session.clock_at, output.signals[0].decision_at);
    assert_eq!(first.session.status, "running");

    let second = db.step_paper_session("session-1").unwrap();
    assert_eq!(second.session.cursor, 2);
    assert_eq!(second.session.status, "completed");
    assert_eq!(second.session.clock_at, output.signals[1].decision_at);
    assert!(matches!(
        db.step_paper_session("session-1"),
        Err(StoreError::Conflict(ref code)) if code == "paper_session_not_running"
    ));

    // The account received exactly the session's two slot orders.
    let view = db.paper_account("session-acct").unwrap();
    assert_eq!(view.slots[0].orders_total, 1);
    assert_eq!(view.slots[1].orders_total, 1);
}

#[test]
fn paper_session_pause_resume_and_stop_gate_trading() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("pause-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    ingest_signal_evidence(&mut db, &output.signals[0]);
    ingest_signal_evidence(&mut db, &output.signals[1]);
    db.create_paper_session(session_spec(
        "pause-acct",
        "pause-session",
        vec![
            session_signal(PaperSlotId::A, &output.signals[0]),
            session_signal(PaperSlotId::A, &output.signals[1]),
        ],
    ))
    .unwrap();
    db.paper_session_action("pause-session", "start").unwrap();
    db.step_paper_session("pause-session").unwrap();

    let paused = db.paper_session_action("pause-session", "pause").unwrap();
    assert_eq!(paused.status, "paused");
    assert!(matches!(
        db.step_paper_session("pause-session"),
        Err(StoreError::Conflict(ref code)) if code == "paper_session_not_running"
    ));
    // Pausing never rewrites history.
    assert_eq!(db.paper_account("pause-acct").unwrap().slots[0].orders_total, 1);

    let resumed = db.paper_session_action("pause-session", "resume").unwrap();
    assert_eq!(resumed.status, "running");

    let stopped = db.paper_session_action("pause-session", "stop").unwrap();
    assert_eq!(stopped.status, "stopped");
    assert!(matches!(
        db.step_paper_session("pause-session"),
        Err(StoreError::Conflict(ref code)) if code == "paper_session_not_running"
    ));
    assert!(matches!(
        db.paper_session_action("pause-session", "resume"),
        Err(StoreError::Conflict(ref code)) if code == "invalid_paper_session_transition"
    ));
    assert_eq!(db.paper_account("pause-acct").unwrap().slots[0].orders_total, 1);
}

#[test]
fn paper_sessions_are_deterministic_across_accounts_and_reopen() {
    let file = DiskDatabase::new();
    let output = frozen_momentum_output();
    let build = |session: &str, account: &str| -> PaperSessionSpec {
        session_spec(
            account,
            session,
            vec![
                session_signal(PaperSlotId::A, &output.signals[0]),
                session_signal(PaperSlotId::B, &output.signals[1]),
            ],
        )
    };
    let mut facts = Vec::new();
    {
        let mut db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        for account in ["det-a", "det-b"] {
            db.create_paper_account(paper_spec(account, 10_000_000)).unwrap();
            for signal in &output.signals {
                ingest_signal_evidence(&mut db, signal);
            }
        }
        for (account, session) in [("det-a", "det-session-a"), ("det-b", "det-session-b")] {
            db.create_paper_session(build(session, account)).unwrap();
            db.paper_session_action(session, "start").unwrap();
            db.step_paper_session(session).unwrap();
            db.step_paper_session(session).unwrap();
            let view = db.paper_account(account).unwrap();
            let cash: Vec<i64> = view.slots.iter().map(|slot| slot.cash_cents).collect();
            let positions: Vec<Vec<(String, i64)>> = view
                .slots
                .iter()
                .map(|slot| slot.positions.iter().map(|p| (p.symbol.clone(), p.quantity)).collect())
                .collect();
            facts.push((cash, positions));
        }
        assert_eq!(facts[0], facts[1], "same inputs must produce the same ledger");
    }
    {
        let db = test_open(file.path(), INITIAL_CASH_CENTS).unwrap();
        let reopened = db.paper_account("det-a").unwrap();
        let cash: Vec<i64> = reopened.slots.iter().map(|slot| slot.cash_cents).collect();
        assert_eq!(cash, facts[0].0);
        let session = db.paper_session("det-session-a").unwrap();
        assert_eq!(session.status, "completed");
        assert_eq!(session.cursor, 2);
    }
}

#[test]
fn paper_session_specs_must_be_explicit_and_ordered() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("order-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let mut unsorted = session_spec(
        "order-acct",
        "unsorted",
        vec![
            session_signal(PaperSlotId::A, &output.signals[1]),
            session_signal(PaperSlotId::A, &output.signals[0]),
        ],
    );
    assert!(matches!(unsorted.validate(), Err(RuleError(ref code)) if code == "paper_session_signals_must_be_ordered"));

    let mut before_start = session_spec(
        "order-acct",
        "before-start",
        vec![session_signal(PaperSlotId::A, &output.signals[0])],
    );
    before_start.start_at = "2026-10-30T15:00:00+08:00".into();
    assert!(matches!(before_start.validate(), Err(RuleError(ref code)) if code == "paper_session_signal_before_start"));

    let empty = session_spec("order-acct", "empty", Vec::new());
    assert!(matches!(empty.validate(), Err(RuleError(ref code)) if code == "paper_session_requires_signals"));

    let missing_account = session_spec("nope", "missing", vec![session_signal(PaperSlotId::A, &output.signals[0])]);
    assert!(matches!(db.create_paper_session(missing_account), Err(StoreError::NotFound(_))));
}

#[test]
fn paper_session_run_advances_to_completion_or_pause() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("run-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    for signal in &output.signals {
        ingest_signal_evidence(&mut db, signal);
    }
    db.create_paper_session(session_spec(
        "run-acct",
        "run-session",
        vec![
            session_signal(PaperSlotId::A, &output.signals[0]),
            session_signal(PaperSlotId::B, &output.signals[1]),
        ],
    ))
    .unwrap();
    db.paper_session_action("run-session", "start").unwrap();
    let run = db.run_paper_session("run-session").unwrap();
    assert_eq!(run.steps, 2);
    assert_eq!(run.filled + run.rejected, 2);
    assert_eq!(run.session.status, "completed");
    assert_eq!(run.session.cursor, 2);
    assert_eq!(run.session.clock_at, output.signals[1].decision_at);
    let view = db.paper_account("run-acct").unwrap();
    assert_eq!(view.slots[0].orders_total + view.slots[1].orders_total, 2);

    // Paused sessions do not run; the call is safe and reports the pause.
    db.create_paper_account(paper_spec("pause-run-acct", 10_000_000)).unwrap();
    db.create_paper_session(session_spec(
        "pause-run-acct",
        "pause-run",
        vec![session_signal(PaperSlotId::A, &output.signals[0])],
    ))
    .unwrap();
    db.paper_session_action("pause-run", "start").unwrap();
    db.paper_session_action("pause-run", "pause").unwrap();
    let paused_run = db.run_paper_session("pause-run").unwrap();
    assert_eq!(paused_run.steps, 0);
    assert_eq!(paused_run.session.status, "paused");
    assert_eq!(db.paper_account("pause-run-acct").unwrap().slots[0].orders_total, 0);

    let listed = db.paper_sessions(Some("run-acct"), 10).unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].session_id, "run-session");
    assert_eq!(db.paper_sessions(None, 10).unwrap().len(), 2);
}

#[test]
fn paper_account_request_resolves_reviewed_strategies_by_id() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let request = crate::domain::PaperAccountRequest {
        schema_version: 1,
        account_id: "request-acct".into(),
        initial_cash_cents: 10_000_000,
        reserve_bps: 1_000,
        created_at: "2026-11-08T09:00:00+08:00".into(),
        limits: crate::domain::PaperRiskLimits::default(),
        slots: vec![
            crate::domain::PaperAccountRequestSlot {
                slot: PaperSlotId::A,
                strategy_version_id: "research-momentum-v1".into(),
                market_data_pack_id: MARKET_DATA_PACK_ID.into(),
                execution_policy_version: POLICY_VERSION.into(),
                weight_bps: 4_500,
            },
            crate::domain::PaperAccountRequestSlot {
                slot: PaperSlotId::B,
                strategy_version_id: "research-reversion-v1".into(),
                market_data_pack_id: MARKET_DATA_PACK_ID.into(),
                execution_policy_version: POLICY_VERSION.into(),
                weight_bps: 4_500,
            },
        ],
    };
    let view = db.create_paper_account_request(request.clone()).unwrap();
    assert_eq!(view.slots[0].strategy_version_id, "research-momentum-v1");
    assert_eq!(view.slots[1].strategy_version_id, "research-reversion-v1");
    assert_eq!(view.slots[0].research_input_id, "offline-research-input-v1");

    let mut unknown = request.clone();
    unknown.account_id = "unknown-strategy".into();
    unknown.slots[0].strategy_version_id = "research-invented-v1".into();
    assert!(matches!(
        db.create_paper_account_request(unknown),
        Err(StoreError::Rules(RuleError(ref code))) if code == "unsupported_strategy_artifact"
    ));

    let catalogue = db.strategy_catalogue().unwrap();
    assert_eq!(catalogue.strategies.len(), 2);
    assert_eq!(catalogue.reserve_bps, 1_000);
}

#[test]
fn paper_session_can_be_imported_from_an_experiment() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("import-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    realise_frozen_signals(&mut db, "cycle-1", &output);

    let session = db
        .create_paper_session_from_experiment(
            "import-session",
            "import-acct",
            "cycle-1",
            PaperSlotId::A,
            None,
        )
        .unwrap();
    assert_eq!(session.status, "created");
    assert_eq!(session.total, output.signals.len());
    assert_eq!(session.clock_at, "2026-10-30T00:00:00+08:00");

    // The same experiment cannot feed a slot with a different strategy.
    assert!(matches!(
        db.create_paper_session_from_experiment("b-session", "import-acct", "cycle-1", PaperSlotId::B, None),
        Err(StoreError::Rules(RuleError(ref code))) if code == "paper_slot_strategy_mismatch"
    ));
    // An experiment without decisions cannot seed a session.
    db.create_experiment(spec("empty-cycle", INITIAL_CASH_CENTS)).unwrap();
    assert!(matches!(
        db.create_paper_session_from_experiment("empty-session", "import-acct", "empty-cycle", PaperSlotId::A, None),
        Err(StoreError::Rules(RuleError(ref code))) if code == "paper_session_requires_signals"
    ));
    // A missing experiment is a distinct failure.
    assert!(matches!(
        db.create_paper_session_from_experiment("missing-session", "import-acct", "nope", PaperSlotId::A, None),
        Err(StoreError::NotFound(_))
    ));

    // An explicit start instant is honoured, and the session runs the imported signals.
    let explicit = db
        .create_paper_session_from_experiment(
            "explicit-session",
            "import-acct",
            "cycle-1",
            PaperSlotId::A,
            Some("2026-10-29T09:00:00+08:00".into()),
        )
        .unwrap();
    assert_eq!(explicit.clock_at, "2026-10-29T09:00:00+08:00");
    db.paper_session_action("explicit-session", "start").unwrap();
    let run = db.run_paper_session("explicit-session").unwrap();
    assert_eq!(run.steps, output.signals.len());
    assert_eq!(run.session.status, "completed");
    assert!(run.filled + run.rejected >= 1);
}

#[test]
fn paper_session_advances_only_through_reached_days() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("advance-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    for signal in &output.signals {
        ingest_signal_evidence(&mut db, signal);
    }
    db.create_paper_session(session_spec(
        "advance-acct",
        "advance-session",
        vec![
            session_signal(PaperSlotId::A, &output.signals[0]),
            session_signal(PaperSlotId::A, &output.signals[1]),
        ],
    ))
    .unwrap();
    db.paper_session_action("advance-session", "start").unwrap();

    // Nothing is scheduled before the first decision day.
    let early = db.advance_paper_session_to("advance-session", "2026-10-29").unwrap();
    assert_eq!(early.steps, 0);
    assert_eq!(early.session.cursor, 0);

    let first = db.advance_paper_session_to("advance-session", "2026-10-30").unwrap();
    assert_eq!(first.steps, 1);
    assert_eq!(first.session.cursor, 1);
    assert_eq!(first.session.clock_at, output.signals[0].decision_at);

    // A day between decisions moves nothing.
    let gap = db.advance_paper_session_to("advance-session", "2026-11-01").unwrap();
    assert_eq!(gap.steps, 0);
    assert_eq!(gap.session.cursor, 1);

    let last = db.advance_paper_session_to("advance-session", "2026-11-02").unwrap();
    assert_eq!(last.steps, 1);
    assert_eq!(last.session.status, "completed");
    assert_eq!(last.session.cursor, 2);

    assert!(matches!(
        db.advance_paper_session_to("advance-session", "2026-13-01"),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_advance_day"
    ));

    // Accounts state their valuation availability instead of implying a value.
    let view = db.paper_account("advance-acct").unwrap();
    assert_eq!(view.valuation.status, "unavailable");
    assert_eq!(view.valuation.reason, "no_reviewed_price_set");
}

#[test]
fn valuation_refuses_without_a_reviewed_price_set() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("valuation-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    db.submit_paper_order(paper_order("valuation-acct", PaperSlotId::A, buy)).unwrap();

    let report = db.paper_valuation("valuation-acct", "2026-11-02").unwrap();
    assert_eq!(report.status, "unavailable");
    assert_eq!(report.reason, "no_reviewed_price_set");
    assert_eq!(report.priced_symbols, 0);
    assert_eq!(report.missing, vec!["reviewed_price_set".to_string()]);
    assert_eq!(report.requirements[0].slot, "A");
    assert_eq!(report.requirements[0].market_data_pack_id, MARKET_DATA_PACK_ID);
    assert!(report.requirements[0].symbols.contains(&"600000.SH".to_string()));
    assert_eq!(report.digest.len(), 64);
    assert_eq!(report, db.paper_valuation("valuation-acct", "2026-11-02").unwrap());

    // No value fields leak into the JSON.
    let json = serde_json::to_value(&report).unwrap();
    for forbidden in ["market_value", "equity", "profit", "return", "nav"] {
        assert!(json.get(forbidden).is_none(), "{forbidden} must be absent");
    }

    assert!(matches!(
        db.paper_valuation("valuation-acct", "2026-13-01"),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_valuation_day"
    ));
    assert!(matches!(
        db.paper_valuation("missing-acct", "2026-11-02"),
        Err(StoreError::NotFound(_))
    ));
}

fn price_set(trust: &str, day: &str, price_cents: i64, pack: &str) -> crate::domain::PriceSet {
    crate::domain::PriceSet {
        schema_version: 1,
        price_set_id: format!("reviewed-prices-{day}"),
        market_data_pack_id: pack.into(),
        trust: trust.into(),
        coverage_start: day.into(),
        coverage_end: day.into(),
        entries: vec![crate::domain::PriceEntry {
            symbol: "600000.SH".into(),
            day: day.into(),
            price_cents,
            source: "offline-fixture".into(),
            observed_at: format!("{day}T15:00:00+08:00"),
        }],
        source_digest: "0".repeat(64),
    }
}

#[test]
fn valuation_marks_positions_only_when_the_set_covers_them() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("value-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("value-acct", PaperSlotId::A, buy)).unwrap();
    let fill = order.outcome.fill.expect("filled");
    let day = &fill.trade_day;
    let account = db.paper_account("value-acct").unwrap();

    let synthetic = price_set("synthetic", day, 1_300, MARKET_DATA_PACK_ID);
    let valuation = db.paper_valuation_with_set("value-acct", day, &synthetic).unwrap();
    assert_eq!(valuation.status, "valued");
    assert_eq!(valuation.trust, "synthetic");
    assert_eq!(valuation.positions.len(), 1);
    assert_eq!(valuation.positions[0].quantity, fill.quantity);
    assert_eq!(valuation.positions[0].price_cents, 1_300);
    assert_eq!(valuation.market_value_cents, fill.quantity * 1_300);
    assert_eq!(valuation.cash_cents, account.cash_cents);
    assert_eq!(valuation.equity_cents, account.cash_cents + fill.quantity * 1_300);
    // Synthetic prices may never state profit.
    assert_eq!(valuation.profit_cents, None);
    assert_eq!(valuation.price_set_digest.len(), 64);

    let reviewed = price_set("reviewed", day, 1_300, MARKET_DATA_PACK_ID);
    let reviewed_value = db.paper_valuation_with_set("value-acct", day, &reviewed).unwrap();
    assert_eq!(
        reviewed_value.profit_cents,
        Some(reviewed_value.equity_cents - 10_000_000)
    );

    // Missing coverage refuses the whole valuation instead of a subset.
    let mut incomplete = price_set("reviewed", day, 1_300, MARKET_DATA_PACK_ID);
    incomplete.entries[0].symbol = "000001.SZ".into();
    assert!(matches!(
        db.paper_valuation_with_set("value-acct", day, &incomplete),
        Err(StoreError::Rules(RuleError(ref code))) if code == "valuation_missing_symbol"
    ));

    let wrong_pack = price_set("reviewed", day, 1_300, "another-pack");
    assert!(matches!(
        db.paper_valuation_with_set("value-acct", day, &wrong_pack),
        Err(StoreError::Rules(RuleError(ref code))) if code == "valuation_pack_mismatch"
    ));

    assert!(matches!(
        db.paper_valuation_with_set("value-acct", "2026-11-30", &reviewed),
        Err(StoreError::Rules(RuleError(ref code))) if code == "valuation_day_outside_price_set"
    ));

    let mut duplicate = price_set("reviewed", day, 1_300, MARKET_DATA_PACK_ID);
    duplicate.entries.push(duplicate.entries[0].clone());
    assert!(matches!(
        db.paper_valuation_with_set("value-acct", day, &duplicate),
        Err(StoreError::Rules(RuleError(ref code))) if code == "duplicate_price_entry"
    ));

    let mut bad_trust = price_set("guessed", day, 1_300, MARKET_DATA_PACK_ID);
    bad_trust.trust = "guessed".into();
    assert!(matches!(
        db.paper_valuation_with_set("value-acct", day, &bad_trust),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_price_set_trust"
    ));
}

#[test]
fn risk_limits_reject_breaching_fills_without_touching_the_ledger() {
    let output = frozen_momentum_output();
    let buy = &output.signals[0];

    let mut capped = paper_spec("risk-order", 10_000_000);
    capped.limits = crate::domain::PaperRiskLimits {
        max_symbol_weight_bps: 10_000,
        max_order_notional_bps: 500,
        min_slot_cash_bps: 0,
    };
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(capped).unwrap();
    ingest_signal_evidence(&mut db, buy);
    let record = db.submit_paper_order(paper_order("risk-order", PaperSlotId::A, buy)).unwrap();
    assert_eq!(record.outcome.status, DecisionStatus::Rejected);
    assert_eq!(record.outcome.reason, "risk_order_notional_exceeded");
    assert!(record.outcome.fill.is_none());
    let view = db.paper_account("risk-order").unwrap();
    assert_eq!(view.slots[0].cash_cents, view.slots[0].budget_cents);
    assert!(view.slots[0].positions.is_empty());

    let mut floor = paper_spec("risk-cash", 10_000_000);
    floor.limits = crate::domain::PaperRiskLimits {
        max_symbol_weight_bps: 10_000,
        max_order_notional_bps: 10_000,
        min_slot_cash_bps: 10_000,
    };
    db.create_paper_account(floor).unwrap();
    let record = db.submit_paper_order(paper_order("risk-cash", PaperSlotId::A, buy)).unwrap();
    assert_eq!(record.outcome.reason, "risk_cash_floor_exceeded");
    assert!(record.outcome.fill.is_none());

    let mut concentrated = paper_spec("risk-symbol", 10_000_000);
    concentrated.limits = crate::domain::PaperRiskLimits {
        max_symbol_weight_bps: 0,
        max_order_notional_bps: 10_000,
        min_slot_cash_bps: 0,
    };
    db.create_paper_account(concentrated).unwrap();
    let record = db.submit_paper_order(paper_order("risk-symbol", PaperSlotId::A, buy)).unwrap();
    assert_eq!(record.outcome.reason, "risk_symbol_concentration_exceeded");
    assert!(record.outcome.fill.is_none());

    // A sell is not constrained by the buy-side notional/concentration caps.
    let mut selling = paper_spec("risk-sell", 10_000_000);
    selling.limits = crate::domain::PaperRiskLimits {
        max_symbol_weight_bps: 0,
        max_order_notional_bps: 0,
        min_slot_cash_bps: 0,
    };
    db.create_paper_account(selling.clone()).unwrap();
    let mut invalid = selling.clone();
    invalid.account_id = "risk-invalid".into();
    invalid.limits.max_order_notional_bps = 10_001;
    assert!(matches!(
        db.create_paper_account(invalid),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_risk_limits"
    ));
}

#[test]
fn price_set_registry_selects_only_reviewed_covering_sets() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("registry-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("registry-acct", PaperSlotId::A, buy)).unwrap();
    let day = order.outcome.fill.expect("filled").trade_day;

    // The bundled catalogue starts empty: no invented prices.
    assert!(db.price_set_catalogue().unwrap().is_empty());
    let unavailable = db.paper_valuation("registry-acct", &day).unwrap();
    assert_eq!(unavailable.status, "unavailable");
    assert!(unavailable.valuation.is_none());

    let reviewed = price_set("reviewed", &day, 1_300, MARKET_DATA_PACK_ID);
    let registry = crate::domain::PriceSetRegistry {
        schema_version: 1,
        sets: vec![reviewed.clone()],
    };
    registry.validate().unwrap();
    let valued = db
        .paper_valuation_with_registry("registry-acct", &day, &registry)
        .unwrap();
    assert_eq!(valued.status, "valued");
    assert!(valued.missing.is_empty());
    assert_eq!(valued.priced_symbols, 1);
    let valuation = valued.valuation.expect("valued model");
    assert!(valuation.profit_cents.is_some());

    // A synthetic set is never selected for a user-facing valuation.
    let synthetic_registry = crate::domain::PriceSetRegistry {
        schema_version: 1,
        sets: vec![price_set("synthetic", &day, 1_300, MARKET_DATA_PACK_ID)],
    };
    let still_unavailable = db
        .paper_valuation_with_registry("registry-acct", &day, &synthetic_registry)
        .unwrap();
    assert_eq!(still_unavailable.status, "unavailable");
    assert!(still_unavailable.valuation.is_none());
    assert_eq!(still_unavailable.digest.len(), 64);
}

#[test]
fn equity_series_marks_each_reviewed_day_and_refuses_gaps() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("series-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("series-acct", PaperSlotId::A, buy)).unwrap();
    let fill = order.outcome.fill.expect("filled");
    let day1 = fill.trade_day.clone();
    let day2 = "2026-11-02".to_string();
    let quantity = fill.quantity;

    let mut set = price_set("reviewed", &day1, 1_300, MARKET_DATA_PACK_ID);
    set.coverage_end = day2.clone();
    set.entries.push(crate::domain::PriceEntry {
        symbol: "600000.SH".into(),
        day: day2.clone(),
        price_cents: 1_200,
        source: "offline-fixture".into(),
        observed_at: format!("{day2}T15:00:00+08:00"),
    });
    let registry = crate::domain::PriceSetRegistry { schema_version: 1, sets: vec![set.clone()] };
    registry.validate().unwrap();

    let series = db
        .paper_equity_series_with_registry("series-acct", &[day1.clone(), day2.clone()], &registry)
        .unwrap()
        .expect("reviewed set covers both days");
    assert_eq!(series.status, "valued");
    assert_eq!(series.points.len(), 2);
    let account = db.paper_account("series-acct").unwrap();
    assert_eq!(series.points[0].cash_cents, account.cash_cents);
    assert_eq!(series.points[0].market_value_cents, quantity * 1_300);
    assert_eq!(series.points[1].market_value_cents, quantity * 1_200);
    // A 100-cent-per-share drop is the drawdown.
    assert_eq!(series.max_drawdown_cents, quantity * 100);
    assert_eq!(series.max_drawdown_day.as_deref(), Some(day2.as_str()));
    assert!(series.profit_cents.is_some());
    assert_eq!(series.digest.len(), 64);

    // A day outside the set coverage means the series is unavailable, not clipped.
    assert!(db
        .paper_equity_series_with_registry(
            "series-acct",
            &[day1.clone(), "2026-11-30".into()],
            &registry
        )
        .unwrap()
        .is_none());

    // Synthetic sets are never used for a user-facing series.
    let mut synthetic = price_set("synthetic", &day1, 1_300, MARKET_DATA_PACK_ID);
    synthetic.coverage_end = day2.clone();
    synthetic.entries.push(set.entries[1].clone());
    let synthetic_registry = crate::domain::PriceSetRegistry { schema_version: 1, sets: vec![synthetic] };
    assert!(db
        .paper_equity_series_with_registry("series-acct", &[day1.clone(), day2.clone()], &synthetic_registry)
        .unwrap()
        .is_none());

    // Missing a price for a held symbol refuses the series.
    let mut incomplete = registry.sets[0].clone();
    incomplete.entries.pop();
    let incomplete_registry = crate::domain::PriceSetRegistry { schema_version: 1, sets: vec![incomplete] };
    assert!(matches!(
        db.paper_equity_series_with_registry("series-acct", &[day1.clone(), day2], &incomplete_registry),
        Err(StoreError::Rules(RuleError(ref code))) if code == "valuation_missing_symbol"
    ));

    // The bundled catalogue is empty: the HTTP path stays unavailable.
    assert!(db.paper_equity_series("series-acct", &[day1]).unwrap().is_none());
}

#[test]
fn account_audit_balances_and_detects_tampering() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("audit-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    db.submit_paper_order(paper_order("audit-acct", PaperSlotId::A, buy)).unwrap();

    let report = db.paper_account_audit("audit-acct").unwrap();
    assert!(report.ok, "checks: {:?}", report.checks);
    let names: Vec<&str> = report.checks.iter().map(|check| check.name.as_str()).collect();
    assert_eq!(names, vec!["budget_split", "fills_replay", "capital_conservation", "order_fill_link", "fill_matches_outcome", "session_link", "session_signals", "rebalance_chain"]);
    assert_eq!(report.digest.len(), 64);
    assert_eq!(report, db.paper_account_audit("audit-acct").unwrap());

    // Tampering with a stored fill must break the conservation check.
    let raw: String = db.conn.query_row("SELECT fill_json FROM v2_paper_fills LIMIT 1", [], |row| row.get(0)).unwrap();
    let mut fill: Fill = serde_json::from_str(&raw).unwrap();
    fill.quantity += 100;
    db.conn.execute(
        "UPDATE v2_paper_fills SET fill_json=? WHERE id=(SELECT MIN(id) FROM v2_paper_fills)",
        [serde_json::to_string(&fill).unwrap()],
    ).unwrap();
    let tampered = db.paper_account_audit("audit-acct").unwrap();
    assert!(!tampered.ok);
    assert!(tampered.checks.iter().any(|check| check.name == "fill_matches_outcome" && !check.ok));

    // A session cursor pointing at a missing order is caught as well.
    let mut clean = test_store(INITIAL_CASH_CENTS).unwrap();
    clean.create_paper_account(paper_spec("audit-session", 10_000_000)).unwrap();
    let dangling = crate::domain::PaperSessionSpec {
        schema_version: 1,
        session_id: "s1".into(),
        account_id: "audit-session".into(),
        start_at: "2026-10-30T00:00:00+08:00".into(),
        signals: vec![],
    };
    clean.conn.execute(
        "INSERT INTO v2_paper_sessions(session_id,account_id,spec_json,status,clock_at,cursor,last_order_id)
         VALUES('s1','audit-session',?,'created','2026-10-30T00:00:00+08:00',0,999)",
        [serde_json::to_string(&dangling).unwrap()],
    ).unwrap();
    let linked = clean.paper_account_audit("audit-session").unwrap();
    assert!(!linked.ok);
    assert!(linked.checks.iter().any(|check| check.name == "session_link" && !check.ok));

    assert!(matches!(db.paper_account_audit("nope"), Err(StoreError::NotFound(_))));
}

#[test]
fn account_audit_verifies_session_orders_against_frozen_signals() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("audit-signals", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    for signal in &output.signals {
        ingest_signal_evidence(&mut db, signal);
    }
    db.create_paper_session(session_spec(
        "audit-signals",
        "audit-signals-session",
        vec![
            session_signal(PaperSlotId::A, &output.signals[0]),
            session_signal(PaperSlotId::A, &output.signals[1]),
        ],
    ))
    .unwrap();
    db.paper_session_action("audit-signals-session", "start").unwrap();
    db.step_paper_session("audit-signals-session").unwrap();

    let report = db.paper_account_audit("audit-signals").unwrap();
    assert!(report.ok, "checks: {:?}", report.checks);
    assert!(report.checks.iter().any(|check| check.name == "session_signals" && check.ok));

    // Tampering with a session order's decision time is caught.
    let raw: String = db
        .conn
        .query_row(
            "SELECT request_json FROM v2_paper_orders WHERE signal_id LIKE 'audit-signals-session:%' LIMIT 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let mut request: crate::domain::PaperOrderRequest = serde_json::from_str(&raw).unwrap();
    request.decision_at = "2026-10-30T15:00:00+08:00".into();
    db.conn
        .execute(
            "UPDATE v2_paper_orders SET request_json=? WHERE signal_id LIKE 'audit-signals-session:%'",
            [serde_json::to_string(&request).unwrap()],
        )
        .unwrap();
    let tampered = db.paper_account_audit("audit-signals").unwrap();
    assert!(!tampered.ok);
    assert!(tampered.checks.iter().any(|check| check.name == "session_signals" && !check.ok));
}

fn temp_price_dir(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "astock-price-sets-{name}-{}-{}",
        std::process::id(),
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn price_sets_load_from_a_directory_and_refuse_bad_artifacts() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("loader-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("loader-acct", PaperSlotId::A, buy)).unwrap();
    let day = order.outcome.fill.expect("filled").trade_day;

    // A missing directory means "no reviewed data yet", not an error.
    let missing = std::env::temp_dir().join(format!("astock-missing-{}", std::process::id()));
    assert!(db.price_set_catalogue_in(&missing).unwrap().is_empty());

    // Synthetic files are listed but never selected for a user valuation.
    let dir = temp_price_dir("synthetic");
    let synthetic = price_set("synthetic", &day, 1_300, MARKET_DATA_PACK_ID);
    std::fs::write(
        dir.join("set-1.json"),
        serde_json::to_string(&synthetic).unwrap(),
    )
    .unwrap();
    let catalogue = db.price_set_catalogue_in(&dir).unwrap();
    assert_eq!(catalogue.len(), 1);
    assert_eq!(catalogue[0].trust, "synthetic");
    assert_eq!(catalogue[0].entries, 1);
    assert_eq!(catalogue[0].digest.len(), 64);
    let report = db.paper_valuation_in_dir("loader-acct", &day, &dir).unwrap();
    assert_eq!(report.status, "unavailable");

    // Duplicate ids across files are rejected by the catalogue.
    let duplicate_dir = temp_price_dir("duplicate");
    std::fs::write(
        duplicate_dir.join("a.json"),
        serde_json::to_string(&synthetic).unwrap(),
    )
    .unwrap();
    std::fs::write(
        duplicate_dir.join("b.json"),
        serde_json::to_string(&synthetic).unwrap(),
    )
    .unwrap();
    assert!(matches!(
        db.price_set_catalogue_in(&duplicate_dir),
        Err(StoreError::Rules(RuleError(ref code))) if code == "duplicate_price_set_id"
    ));

    // Malformed artifacts fail loudly instead of being skipped.
    let broken_dir = temp_price_dir("broken");
    std::fs::write(broken_dir.join("bad.json"), "{\"schema_version\":1}").unwrap();
    assert!(db.price_set_catalogue_in(&broken_dir).is_err());

    for path in [dir, duplicate_dir, broken_dir] {
        let _ = std::fs::remove_dir_all(path);
    }
}

#[test]
fn allocation_proposal_scores_slots_and_respects_bounds() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("alloc-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("alloc-acct", PaperSlotId::A, buy)).unwrap();
    let day1 = order.outcome.fill.expect("filled").trade_day;
    let day2 = "2026-11-02".to_string();

    let mut set = price_set("reviewed", &day1, 1_300, MARKET_DATA_PACK_ID);
    set.coverage_end = day2.clone();
    set.entries.push(crate::domain::PriceEntry {
        symbol: "600000.SH".into(),
        day: day2.clone(),
        price_cents: 1_200,
        source: "offline-fixture".into(),
        observed_at: format!("{day2}T15:00:00+08:00"),
    });
    let dir = temp_price_dir("alloc");
    std::fs::write(dir.join("set.json"), serde_json::to_string(&set).unwrap()).unwrap();

    let report = db
        .paper_allocation_proposal("alloc-acct", &[day1.clone(), day2.clone()], &dir)
        .unwrap()
        .expect("reviewed set covers both days");
    assert_eq!(report.proposal.status, "proposed");
    assert_eq!(report.scores.len(), 2);
    assert_eq!(report.scores[0].slot, "A");
    assert_eq!(report.scores[0].days, 2);
    assert!(report.scores[0].return_bps < 0, "{:?}", report.scores[0]);
    assert_eq!(report.scores[1].return_bps, 0);

    let targets: i64 = report
        .proposal
        .rationale
        .iter()
        .map(|item| item.target_bps)
        .sum();
    assert_eq!(targets + report.proposal.reserve_bps, 10_000);
    for item in &report.proposal.rationale {
        assert!((1_500..=6_000).contains(&item.target_bps), "{:?}", item);
        if item.slot == "A" {
            assert!(item.target_bps < 4_500);
        } else {
            assert!(item.target_bps > 4_500);
        }
    }
    assert!(report.proposal.reserve_bps >= 1_000);
    assert_eq!(report.proposal.digest.len(), 64);

    // Too few observations keeps the current weights instead of guessing.
    let policy = crate::domain::AllocationPolicy::default();
    let thin = crate::domain::propose_allocation(
        4_500,
        4_500,
        &[
            crate::domain::SlotScore {
                slot: "A".into(), return_bps: 900, drawdown_bps: 0, score_bps: 900, days: 1,
            },
            crate::domain::SlotScore {
                slot: "B".into(), return_bps: 0, drawdown_bps: 0, score_bps: 0, days: 1,
            },
        ],
        &policy,
    )
    .unwrap();
    assert_eq!(thin.status, "insufficient_evidence");
    assert!(thin.rationale.iter().all(|item| item.target_bps == 4_500));

    // A single requested day cannot support an allocation.
    assert!(matches!(
        db.paper_allocation_proposal("alloc-acct", &[day1.clone()], &dir),
        Err(StoreError::Rules(RuleError(ref code))) if code == "allocation_requires_two_days"
    ));

    // Synthetic data never drives an allocation.
    let synthetic_dir = temp_price_dir("alloc-synthetic");
    let mut synthetic = set.clone();
    synthetic.trust = "synthetic".into();
    std::fs::write(
        synthetic_dir.join("set.json"),
        serde_json::to_string(&synthetic).unwrap(),
    )
    .unwrap();
    assert!(db
        .paper_allocation_proposal("alloc-acct", &[day1, day2], &synthetic_dir)
        .unwrap()
        .is_none());

    let _ = std::fs::remove_dir_all(dir);
    let _ = std::fs::remove_dir_all(synthetic_dir);
}

#[test]
fn rebalance_moves_cash_between_slots_and_keeps_history() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("rebalance-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    db.submit_paper_order(paper_order("rebalance-acct", PaperSlotId::A, buy)).unwrap();
    let before = db.paper_account("rebalance-acct").unwrap();
    let total_before = before.cash_cents;

    let digest = "a".repeat(64);
    let record = db
        .apply_rebalance(
            "rebalance-acct",
            [3_500, 5_500],
            &digest,
            "2026-11-03T09:00:00+08:00",
            "reduce momentum slot",
        )
        .unwrap();
    assert_eq!(record.weights_before, [4_500, 4_500]);
    assert_eq!(record.weights_after, [3_500, 5_500]);
    assert_eq!(record.from_slot, "A");
    assert_eq!(record.to_slot, "B");
    let expected_move = before.slots[0].cash_cents - before.slots[0].budget_cents + 1_000_000;
    assert_eq!(record.amount_cents, expected_move);
    assert_eq!(record.digest.len(), 64);

    let after = db.paper_account("rebalance-acct").unwrap();
    assert_eq!(after.slots[0].weight_bps, 3_500);
    assert_eq!(after.slots[0].budget_cents, 3_500_000);
    assert_eq!(after.slots[1].budget_cents, 5_500_000);
    assert_eq!(after.slots[0].cash_cents, before.slots[0].cash_cents - record.amount_cents);
    assert_eq!(after.slots[1].cash_cents, before.slots[1].cash_cents + record.amount_cents);
    assert_eq!(after.cash_cents, total_before, "cash is conserved");
    // Fills are untouched by a capital decision.
    assert_eq!(after.slots[0].positions, before.slots[0].positions);
    assert_eq!(after.slots[0].orders_total, before.slots[0].orders_total);
    assert!(db.paper_account_audit("rebalance-acct").unwrap().ok);

    // Same digest and same target is idempotent.
    let replay = db
        .apply_rebalance(
            "rebalance-acct",
            [3_500, 5_500],
            &digest,
            "2026-11-03T09:00:00+08:00",
            "reduce momentum slot",
        )
        .unwrap();
    assert_eq!(replay.amount_cents, record.amount_cents);
    // Same digest with a different target conflicts.
    assert!(matches!(
        db.apply_rebalance(
            "rebalance-acct",
            [4_000, 5_000],
            &digest,
            "2026-11-03T09:00:00+08:00",
            "different",
        ),
        Err(StoreError::Conflict(ref code)) if code == "rebalance_digest_conflict"
    ));

    // Step cap, bounds and reserve floor are hard rejections.
    assert!(matches!(
        db.apply_rebalance(
            "rebalance-acct",
            [1_000, 8_000],
            &"b".repeat(64),
            "2026-11-04T09:00:00+08:00",
            "out of bounds",
        ),
        Err(StoreError::Rules(RuleError(ref code))) if code == "rebalance_weight_out_of_bounds"
    ));
    assert!(matches!(
        db.apply_rebalance(
            "rebalance-acct",
            [3_500, 5_500],
            "not-a-digest",
            "2026-11-04T09:00:00+08:00",
            "bad digest",
        ),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_proposal_digest"
    ));
    assert!(matches!(
        db.apply_rebalance(
            "rebalance-acct",
            [5_000, 4_500],
            &"d".repeat(64),
            "2026-11-04T09:00:00+08:00",
            "bad sum",
        ),
        Err(StoreError::Rules(RuleError(ref code))) if code == "rebalance_weights_must_keep_reserve"
    ));

    // The read model exposes exactly what was applied.
    let applied = db.paper_rebalances("rebalance-acct").unwrap();
    assert_eq!(applied.len(), 1);
    assert_eq!(applied[0].weights_after, [3_500, 5_500]);
    assert_eq!(applied[0].digest.len(), 64);

    // Tampering with the stored weight chain is caught by the audit.
    db.conn
        .execute(
            "UPDATE v2_paper_transfers SET weights_after_json=? WHERE account_id='rebalance-acct'",
            [serde_json::to_string(&vec![5_000, 4_000]).unwrap()],
        )
        .unwrap();
    let tampered = db.paper_account_audit("rebalance-acct").unwrap();
    assert!(!tampered.ok);
    assert!(tampered
        .checks
        .iter()
        .any(|check| check.name == "rebalance_chain" && !check.ok));
}

#[test]
fn exposure_reports_cost_basis_and_concentration_breaches() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("exposure-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("exposure-acct", PaperSlotId::A, buy)).unwrap();
    let fill = order.outcome.fill.expect("filled");

    let exposure = db.paper_exposure("exposure-acct").unwrap();
    assert_eq!(exposure.account_id, "exposure-acct");
    assert_eq!(exposure.slots.len(), 2);
    let slot_a = &exposure.slots[0];
    assert_eq!(slot_a.cost_cents, fill.quantity * fill.price_cents + fill.fees_cents);
    assert_eq!(slot_a.positions.len(), 1);
    assert_eq!(slot_a.positions[0].quantity, fill.quantity);
    assert_eq!(
        slot_a.positions[0].weight_bps,
        slot_a.cost_cents * 10_000 / slot_a.budget_cents
    );
    assert_eq!(slot_a.cash_utilization_bps, slot_a.positions[0].weight_bps);
    assert!(exposure.breaches.is_empty());
    assert_eq!(exposure.digest.len(), 64);

    // Selling out clears the cost basis.
    let sell = &output.signals[1];
    ingest_signal_evidence(&mut db, sell);
    db.submit_paper_order(paper_order("exposure-acct", PaperSlotId::A, sell)).unwrap();
    let after = db.paper_exposure("exposure-acct").unwrap();
    assert_eq!(after.slots[0].positions.len(), 0);
    assert_eq!(after.slots[0].cost_cents, 0);

    // A position above the symbol cap is reported as a breach.
    let breaching = crate::domain::compute_exposure(
        "manual",
        &[crate::domain::SlotBook {
            slot: "A".into(),
            budget_cents: 1_000_000,
            cash_cents: 0,
            positions: vec![("600000.SH".into(), 10, 300_000)],
        }],
        2_500,
    )
    .unwrap();
    assert_eq!(breaching.breaches.len(), 1);
    assert_eq!(breaching.breaches[0].kind, "symbol_concentration");
    assert_eq!(breaching.breaches[0].value_bps, 3_000);
    assert_eq!(breaching.breaches[0].limit_bps, 2_500);
}

#[test]
fn account_advance_drives_running_sessions_only() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("advance-account", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    for signal in &output.signals {
        ingest_signal_evidence(&mut db, signal);
    }
    for id in ["acct-sess-1", "acct-sess-2"] {
        db.create_paper_session(session_spec(
            "advance-account",
            id,
            vec![
                session_signal(PaperSlotId::A, &output.signals[0]),
                session_signal(PaperSlotId::A, &output.signals[1]),
            ],
        ))
        .unwrap();
        db.paper_session_action(id, "start").unwrap();
    }
    db.paper_session_action("acct-sess-2", "pause").unwrap();

    let first = db.advance_account_to("advance-account", "2026-10-30").unwrap();
    assert_eq!(first.sessions.len(), 1, "a paused session must not advance");
    assert_eq!(first.sessions[0].session.session_id, "acct-sess-1");
    assert_eq!(first.steps, 1);
    assert_eq!(db.paper_session("acct-sess-2").unwrap().cursor, 0);

    let second = db.advance_account_to("advance-account", "2026-11-02").unwrap();
    assert_eq!(second.steps, 1);
    assert_eq!(second.sessions[0].session.status, "completed");
    assert_eq!(db.paper_session("acct-sess-2").unwrap().status, "paused");

    // A day with nothing left to do is a no-op, not an error.
    let idle = db.advance_account_to("advance-account", "2026-11-05").unwrap();
    assert_eq!(idle.steps, 0);
    assert!(idle.sessions.is_empty());

    assert!(matches!(
        db.advance_account_to("advance-account", "2026-13-01"),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_advance_day"
    ));
    assert!(matches!(
        db.advance_account_to("missing-account", "2026-11-02"),
        Err(StoreError::NotFound(_))
    ));
}

#[test]
fn evolution_decisions_require_evidence_margin_and_risk() {
    let policy = crate::domain::EvolutionPolicy::default();
    let incumbent = crate::domain::CandidateMetrics {
        candidate_id: "incumbent".into(),
        out_of_sample_days: 30,
        shadow_days: 15,
        trades: 40,
        return_bps: 500,
        drawdown_bps: 800,
    };
    let strong = crate::domain::CandidateMetrics {
        candidate_id: "candidate-strong".into(),
        out_of_sample_days: 25,
        shadow_days: 12,
        trades: 25,
        return_bps: 700,
        drawdown_bps: 900,
    };
    let promoted = crate::domain::decide_promotion(&incumbent, &strong, &policy).unwrap();
    assert_eq!(promoted.status, "promoted");
    assert!(promoted.reasons[0].contains("通过"));
    assert_eq!(promoted.digest.len(), 64);

    // Too little evidence never promotes, even with great numbers.
    let thin = crate::domain::CandidateMetrics {
        out_of_sample_days: 5,
        shadow_days: 2,
        trades: 3,
        return_bps: 5_000,
        ..strong.clone()
    };
    let insufficient = crate::domain::decide_promotion(&incumbent, &thin, &policy).unwrap();
    assert_eq!(insufficient.status, "insufficient_evidence");
    assert!(insufficient.reasons[0].contains("样本不足"));

    // Drawdown above the cap rejects regardless of return.
    let risky = crate::domain::CandidateMetrics {
        drawdown_bps: 2_000,
        ..strong.clone()
    };
    let rejected = crate::domain::decide_promotion(&incumbent, &risky, &policy).unwrap();
    assert_eq!(rejected.status, "rejected");
    assert!(rejected.reasons.iter().any(|reason| reason.contains("风险恶化")));

    // A marginal gain below the margin is not enough.
    let marginal = crate::domain::CandidateMetrics {
        return_bps: 520,
        ..strong.clone()
    };
    let marginal_decision = crate::domain::decide_promotion(&incumbent, &marginal, &policy).unwrap();
    assert_eq!(marginal_decision.status, "rejected");
    assert!(marginal_decision.reasons.iter().any(|reason| reason.contains("优势不足")));

    // Post-promotion monitoring reverts on risk deterioration and keeps calm.
    let calm = crate::domain::CandidateMetrics {
        candidate_id: "candidate-strong".into(),
        drawdown_bps: 1_000,
        return_bps: 680,
        ..strong.clone()
    };
    let kept = crate::domain::decide_rollback(&strong, &calm, &policy).unwrap();
    assert_eq!(kept.status, "kept");
    let storm = crate::domain::CandidateMetrics {
        drawdown_bps: 2_500,
        return_bps: 100,
        ..strong.clone()
    };
    let reverted = crate::domain::decide_rollback(&strong, &storm, &policy).unwrap();
    assert_eq!(reverted.status, "reverted");
    assert!(reverted.reasons.iter().any(|reason| reason.contains("回撤恶化")));
    assert!(reverted.reasons.iter().any(|reason| reason.contains("优势消失")));
}

#[test]
fn evolution_events_are_idempotent_and_listed() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let policy = crate::domain::EvolutionPolicy::default();
    let incumbent = crate::domain::CandidateMetrics {
        candidate_id: "base".into(),
        out_of_sample_days: 30,
        shadow_days: 15,
        trades: 40,
        return_bps: 500,
        drawdown_bps: 800,
    };
    let candidate = crate::domain::CandidateMetrics {
        candidate_id: "cand".into(),
        out_of_sample_days: 25,
        shadow_days: 12,
        trades: 25,
        return_bps: 700,
        drawdown_bps: 900,
    };
    let promoted = crate::domain::decide_promotion(&incumbent, &candidate, &policy).unwrap();
    let record = db
        .record_evolution_event("evo-1", "2026-11-05T15:00:00+08:00", promoted.clone())
        .unwrap();
    assert_eq!(record.status, "promoted");
    assert_eq!(record.digest.len(), 64);

    // Same event id + same decision is idempotent.
    let replay = db
        .record_evolution_event("evo-1", "2026-11-05T15:00:00+08:00", promoted.clone())
        .unwrap();
    assert_eq!(replay.digest, record.digest);

    // Same id with a different decision conflicts.
    let risky = crate::domain::CandidateMetrics {
        drawdown_bps: 2_000,
        ..candidate.clone()
    };
    let rejected = crate::domain::decide_promotion(&incumbent, &risky, &policy).unwrap();
    assert!(matches!(
        db.record_evolution_event("evo-1", "2026-11-05T15:00:00+08:00", rejected),
        Err(StoreError::Conflict(ref code)) if code == "evolution_event_conflict"
    ));

    let events = db.evolution_events(10, None, None).unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].event_id, "evo-1");
    assert_eq!(events[0].candidate_id, "cand");

    assert!(matches!(
        db.record_evolution_event("bad id!", "2026-11-05T15:00:00+08:00", promoted.clone()),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_evolution_event_id"
    ));
    assert!(matches!(
        db.record_evolution_event("evo-2", "not-a-time", promoted),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_evolution_time"
    ));
}

#[test]
fn evolution_events_filter_by_candidate_and_status() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let promoted = crate::domain::EvolutionDecision {
        candidate_id: "cand-a".into(),
        status: "promoted".into(),
        reasons: vec!["通过".into()],
        digest: "a".repeat(64),
    };
    let rejected = crate::domain::EvolutionDecision {
        candidate_id: "cand-b".into(),
        status: "rejected".into(),
        reasons: vec!["收益不足".into()],
        digest: "b".repeat(64),
    };
    db.record_evolution_event("evo-a", "2026-11-05T15:00:00+08:00", promoted).unwrap();
    db.record_evolution_event("evo-b", "2026-11-04T15:00:00+08:00", rejected).unwrap();

    assert_eq!(db.evolution_events(10, None, None).unwrap().len(), 2);
    let only_a = db.evolution_events(10, Some("cand-a"), None).unwrap();
    assert_eq!(only_a.len(), 1);
    assert_eq!(only_a[0].event_id, "evo-a");
    let rejected_only = db.evolution_events(10, None, Some("rejected")).unwrap();
    assert_eq!(rejected_only.len(), 1);
    assert_eq!(rejected_only[0].candidate_id, "cand-b");
    assert_eq!(db.evolution_events(10, Some("cand-a"), Some("rejected")).unwrap().len(), 0);
    assert_eq!(db.evolution_events(1, None, None).unwrap().len(), 1);
}

#[test]
fn shadow_experiments_require_a_reviewed_source_digest() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let reviewed = reviewed_artifact();
    let mut shadow = reviewed.clone();
    shadow.strategy_version_id = "shadow-research-momentum-v1-test1234".into();
    let parameters = serde_json::json!({"lookback": 3, "threshold_bps": 25});
    shadow.parameters_canonical = crate::domain::canonical_json_text(&parameters);
    shadow.parameters_digest = crate::domain::canonical_digest(&parameters);

    let spec = |artifact: crate::domain::StrategyArtifact| crate::domain::ExperimentSpec {
        schema_version: 1,
        cycle_id: "shadow-cycle".into(),
        strategy: artifact,
        market_data_pack_id: MARKET_DATA_PACK_ID.into(),
        execution_policy_version: POLICY_VERSION.into(),
        initial_cash_cents: INITIAL_CASH_CENTS,
    };

    // Same reviewed source, different parameters: allowed for research shadowing.
    let experiment = db.create_experiment(spec(shadow.clone())).unwrap();
    assert_eq!(experiment.spec.strategy.strategy_version_id, shadow.strategy_version_id);
    assert_eq!(experiment.spec.strategy.parameters_digest, shadow.parameters_digest);

    // A shadow id with an unknown source digest is still refused.
    let mut unknown = shadow.clone();
    unknown.strategy_version_id = "shadow-unknown-source".into();
    unknown.source_digest = "0".repeat(64);
    assert!(matches!(
        db.create_experiment(spec(unknown)),
        Err(StoreError::Rules(RuleError(ref code))) if code == "unsupported_strategy_artifact"
    ));

    // Reviewed ids keep the exact-content requirement.
    let mut drifted = reviewed.clone();
    drifted.parameters_digest = "1".repeat(64);
    assert!(matches!(
        db.create_experiment(spec(drifted)),
        Err(StoreError::Conflict(_)) | Err(StoreError::Rules(_))
    ));
}

#[test]
fn signal_diff_reports_only_changed_signals() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_experiment(spec("base-cycle", INITIAL_CASH_CENTS)).unwrap();
    let output = frozen_momentum_output();
    realise_frozen_signals(&mut db, "base-cycle", &output);

    // A shadow experiment reusing the reviewed source but different parameters.
    let reviewed = reviewed_artifact();
    let mut shadow = reviewed.clone();
    shadow.strategy_version_id = "shadow-research-momentum-v1-diff".into();
    let parameters = serde_json::json!({"lookback": 3, "threshold_bps": 25});
    shadow.parameters_canonical = crate::domain::canonical_json_text(&parameters);
    shadow.parameters_digest = crate::domain::canonical_digest(&parameters);
    db.create_experiment(crate::domain::ExperimentSpec {
        schema_version: 1,
        cycle_id: "shadow-cycle".into(),
        strategy: shadow.clone(),
        market_data_pack_id: MARKET_DATA_PACK_ID.into(),
        execution_policy_version: POLICY_VERSION.into(),
        initial_cash_cents: INITIAL_CASH_CENTS,
    })
    .unwrap();

    // Copy the first base run, retime it and re-identify it for the shadow cycle.
    let mut request = db.runs("base-cycle", 10).unwrap().into_iter().next().unwrap().request;
    request.cycle_id = "shadow-cycle".into();
    request.strategy_version_id = shadow.strategy_version_id.clone();
    request.signal_id = "shadow-diff-signal".into();
    request.decision_at = "2026-10-30T14:30:00+08:00".into();
    db.conn
        .execute(
            r#"INSERT INTO v2_simulation_runs
             (cycle_id,strategy_version_id,signal_id,request_json,outcome_json,evidence_json,
              calendar_version,session_policy_version,instrument_version)
             SELECT 'shadow-cycle',?,'shadow-diff-signal',?,'{"status":"rejected","reason":"x","fill":null,"cash_after_cents":1}',
                    evidence_json,calendar_version,session_policy_version,instrument_version
             FROM v2_simulation_runs WHERE cycle_id='base-cycle' ORDER BY id LIMIT 1"#,
            rusqlite::params![shadow.strategy_version_id, serde_json::to_string(&request).unwrap()],
        )
        .unwrap();

    let diff = db.signal_diff("base-cycle", "shadow-cycle").unwrap();
    assert_eq!(diff.base_runs, 2);
    assert_eq!(diff.candidate_runs, 1);
    assert_eq!(diff.matched, 0);
    assert_eq!(diff.base_only.len() + diff.matched as usize, 2);
    assert_eq!(diff.candidate_only.len(), 1);
    assert_eq!(diff.candidate_only[0].decision_at, "2026-10-30T14:30:00+08:00");
    assert_eq!(diff.digest.len(), 64);
    assert_eq!(diff, db.signal_diff("base-cycle", "shadow-cycle").unwrap());

    assert!(matches!(
        db.signal_diff("missing-cycle", "shadow-cycle"),
        Err(StoreError::NotFound(_))
    ));
}

#[test]
fn evolution_pages_use_timestamp_and_id_cursor() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    let make = |id: &str| crate::domain::EvolutionDecision {
        candidate_id: id.into(),
        status: "kept".into(),
        reasons: vec!["保持".into()],
        digest: "a".repeat(64),
    };
    db.record_evolution_event("e1", "2026-11-05T10:00:00+08:00", make("c1")).unwrap();
    db.record_evolution_event("e2", "2026-11-05T10:00:00+08:00", make("c2")).unwrap();
    db.record_evolution_event("e3", "2026-11-05T09:00:00+08:00", make("c3")).unwrap();

    let first = db.evolution_events_page(2, None, None, None, None, None).unwrap();
    assert_eq!(first.events.len(), 2);
    assert_eq!(first.events[0].event_id, "e2");
    assert_eq!(first.events[1].event_id, "e1");
    assert_eq!(first.next_before.as_deref(), Some("2026-11-05T10:00:00+08:00"));
    assert_eq!(first.next_before_id.as_deref(), Some("e1"));
    assert_eq!(first.scanned, 3);

    let second = db
        .evolution_events_page(
            2,
            None,
            None,
            first.next_before.as_deref(),
            first.next_before_id.as_deref(),
            None,
        )
        .unwrap();
    assert_eq!(second.events.len(), 1);
    assert_eq!(second.events[0].event_id, "e3");
    assert!(second.next_before.is_none());

    let after = db
        .evolution_events_page(10, None, None, None, None, Some("2026-11-05T09:30:00+08:00"))
        .unwrap();
    assert_eq!(after.events.len(), 2);

    let filtered = db
        .evolution_events_page(10, Some("c3"), Some("kept"), None, None, None)
        .unwrap();
    assert_eq!(filtered.events.len(), 1);
    assert_eq!(filtered.events[0].event_id, "e3");

    assert!(matches!(
        db.evolution_events_page(10, None, None, Some("not-a-time"), None, None),
        Err(StoreError::Rules(RuleError(ref code))) if code == "invalid_evolution_cursor"
    ));
}

#[test]
fn performance_requires_complete_reviewed_coverage() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_paper_account(paper_spec("perf-acct", 10_000_000)).unwrap();
    let output = frozen_momentum_output();
    let buy = &output.signals[0];
    ingest_signal_evidence(&mut db, buy);
    let order = db.submit_paper_order(paper_order("perf-acct", PaperSlotId::A, buy)).unwrap();
    let fill = order.outcome.fill.expect("filled");
    let day1 = fill.trade_day.clone();
    let day2 = "2026-11-02".to_string();

    let mut set = price_set("reviewed", &day1, 1_300, MARKET_DATA_PACK_ID);
    set.coverage_end = day2.clone();
    set.entries.push(crate::domain::PriceEntry {
        symbol: "600000.SH".into(),
        day: day2.clone(),
        price_cents: 1_200,
        source: "offline-fixture".into(),
        observed_at: format!("{day2}T15:00:00+08:00"),
    });
    let dir = temp_price_dir("perf");
    std::fs::write(dir.join("set.json"), serde_json::to_string(&set).unwrap()).unwrap();

    let report = db
        .paper_performance("perf-acct", &[day1.clone(), day2.clone()], &dir)
        .unwrap()
        .expect("reviewed coverage");
    let series = db
        .paper_equity_series_with_registry(
            "perf-acct",
            &[day1.clone(), day2.clone()],
            &crate::domain::PriceSetRegistry { schema_version: 1, sets: vec![set.clone()] },
        )
        .unwrap()
        .unwrap();
    assert_eq!(report.days, 2);
    assert_eq!(report.start_equity_cents, series.points[0].equity_cents);
    assert_eq!(report.end_equity_cents, series.points[1].equity_cents);
    assert_eq!(report.profit_cents, report.end_equity_cents - 10_000_000);
    assert_eq!(report.return_bps, report.profit_cents * 10_000 / 10_000_000);
    assert_eq!(report.fees_cents, fill.fees_cents);
    assert_eq!(report.trades, 1);
    let peak = series.points.iter().map(|point| point.equity_cents).max().unwrap();
    assert_eq!(report.drawdown_bps, series.max_drawdown_cents * 10_000 / peak);
    assert_eq!(report.max_drawdown_day.as_deref(), Some(day2.as_str()));
    assert_eq!(report.digest.len(), 64);
    assert_eq!(
        report,
        db.paper_performance("perf-acct", &[day1.clone(), day2.clone()], &dir)
            .unwrap()
            .unwrap()
    );

    // Synthetic or missing coverage yields no performance number at all.
    let synthetic_dir = temp_price_dir("perf-synthetic");
    let mut synthetic = set.clone();
    synthetic.trust = "synthetic".into();
    std::fs::write(
        synthetic_dir.join("set.json"),
        serde_json::to_string(&synthetic).unwrap(),
    )
    .unwrap();
    assert!(db
        .paper_performance("perf-acct", &[day1.clone(), day2.clone()], &synthetic_dir)
        .unwrap()
        .is_none());
    assert!(matches!(
        db.paper_performance("perf-acct", &[], &dir),
        Err(StoreError::Rules(RuleError(ref code))) if code == "empty_equity_series"
    ));

    let _ = std::fs::remove_dir_all(dir);
    let _ = std::fs::remove_dir_all(synthetic_dir);
}

#[test]
fn experiment_performance_uses_fills_and_reviewed_prices() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_experiment(spec("exp-perf", INITIAL_CASH_CENTS)).unwrap();
    let output = frozen_momentum_output();
    realise_frozen_signals(&mut db, "exp-perf", &output);

    let day1 = output.signals[0].decision_at[..10].to_string();
    let day2 = output.signals[1].decision_at[..10].to_string();
    let mut set = price_set("reviewed", &day1, 1_300, MARKET_DATA_PACK_ID);
    set.coverage_end = day2.clone();
    set.entries.push(crate::domain::PriceEntry {
        symbol: "600000.SH".into(),
        day: day2.clone(),
        price_cents: 1_200,
        source: "offline-fixture".into(),
        observed_at: format!("{day2}T15:00:00+08:00"),
    });
    let dir = temp_price_dir("exp-perf");
    std::fs::write(dir.join("set.json"), serde_json::to_string(&set).unwrap()).unwrap();

    let report = db
        .experiment_performance("exp-perf", &[day1.clone(), day2.clone()], &dir)
        .unwrap()
        .expect("reviewed coverage");
    assert_eq!(report.days, 2);
    assert_eq!(report.cycle_id, "exp-perf");
    assert_eq!(report.profit_cents, report.end_equity_cents - INITIAL_CASH_CENTS);
    assert!(report.trades >= 1);
    assert!(report.fees_cents > 0);
    assert_eq!(report.digest.len(), 64);
    assert_eq!(
        report,
        db.experiment_performance("exp-perf", &[day1, day2], &dir)
            .unwrap()
            .unwrap()
    );

    let synthetic_dir = temp_price_dir("exp-perf-synthetic");
    let mut synthetic = set.clone();
    synthetic.trust = "synthetic".into();
    std::fs::write(
        synthetic_dir.join("set.json"),
        serde_json::to_string(&synthetic).unwrap(),
    )
    .unwrap();
    assert!(db
        .experiment_performance(
            "exp-perf",
            &[output.signals[0].decision_at[..10].to_string(), output.signals[1].decision_at[..10].to_string()],
            &synthetic_dir
        )
        .unwrap()
        .is_none());

    assert!(matches!(
        db.experiment_performance("missing-cycle", &["2026-10-30".into()], &dir),
        Err(StoreError::NotFound(_))
    ));

    let _ = std::fs::remove_dir_all(dir);
    let _ = std::fs::remove_dir_all(synthetic_dir);
}

#[test]
fn experiment_segments_require_each_window_to_be_covered() {
    let mut db = test_store(INITIAL_CASH_CENTS).unwrap();
    db.create_experiment(spec("seg-perf", INITIAL_CASH_CENTS)).unwrap();
    let output = frozen_momentum_output();
    realise_frozen_signals(&mut db, "seg-perf", &output);
    let day1 = output.signals[0].decision_at[..10].to_string();
    let day2 = output.signals[1].decision_at[..10].to_string();
    let mut set = price_set("reviewed", &day1, 1_300, MARKET_DATA_PACK_ID);
    set.coverage_end = day2.clone();
    set.entries.push(crate::domain::PriceEntry {
        symbol: "600000.SH".into(),
        day: day2.clone(),
        price_cents: 1_200,
        source: "offline-fixture".into(),
        observed_at: format!("{day2}T15:00:00+08:00"),
    });
    let dir = temp_price_dir("seg-perf");
    std::fs::write(dir.join("set.json"), serde_json::to_string(&set).unwrap()).unwrap();

    let report = db
        .experiment_performance_segments(
            "seg-perf",
            &[vec![day1.clone()], vec![day2.clone()]],
            &dir,
        )
        .unwrap();
    assert_eq!(report.segments.len(), 2);
    assert!(report.segments.iter().all(|segment| segment.status == "valued"));
    assert!(report
        .segments
        .iter()
        .all(|segment| segment.performance.is_some()));
    assert_eq!(report.digest.len(), 64);

    let missing = db
        .experiment_performance_segments(
            "seg-perf",
            &[vec![day1.clone()], vec!["2026-11-30".into()]],
            &dir,
        )
        .unwrap();
    assert_eq!(missing.segments[0].status, "valued");
    assert_eq!(missing.segments[1].status, "unavailable");
    assert_eq!(missing.segments[1].reason, "no_reviewed_price_set");

    assert!(matches!(
        db.experiment_performance_segments("seg-perf", &[vec![]], &dir),
        Err(StoreError::Rules(RuleError(ref code))) if code == "empty_rolling_segment"
    ));

    let _ = std::fs::remove_dir_all(dir);
}
