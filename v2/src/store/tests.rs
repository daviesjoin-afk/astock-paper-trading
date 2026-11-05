
    use super::*;
    use crate::domain::{Side, Signal, Quote, INITIAL_CASH_CENTS, DecisionStatus};

    fn request(day: &str, signal_id: &str, side: Side) -> SimulationRequest {
        SimulationRequest {
            schema_version: 1, cycle_id: "cycle-1".into(),
            strategy_version_id: "strategy-v1".into(),
            signal_id: signal_id.into(),
            decision_at: format!("{day}T10:00:00+08:00"),
            signal: Signal { symbol: "600000.SH".into(), side },
            quote: Quote {
                symbol: "600000.SH".into(), price_cents: 1234,
                observed_at: format!("{day}T09:59:30+08:00"),
                source: "offline-fixture".into(), quote_id: format!("quote-{signal_id}"),
                tradable: true, limit_up: false, limit_down: false,
            },
        }
    }

    #[test]
    fn dedup_is_exact_and_does_not_duplicate_fills() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let req = request("2026-10-30", "b1", Side::Buy);
        let first = db.submit(req.clone()).unwrap();
        let second = db.submit(req).unwrap();
        assert!(!first.duplicate);
        assert!(second.duplicate);
        assert_eq!(first.id, second.id);
        let view = db.state(Some("2026-10-30")).unwrap();
        assert_eq!(view.runs_total, 1);
        assert_eq!(view.positions.len(), 1);
        assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
    }

    #[test]
    fn conflicting_payload_under_same_identity_is_rejected() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-10-30", "b1", Side::Buy)).unwrap();
        let mut changed = request("2026-10-30", "b1", Side::Buy);
        changed.quote.price_cents = 999;
        assert!(matches!(db.submit(changed), Err(StoreError::Conflict(_))));
        assert_eq!(db.state(None).unwrap().runs_total, 1);
    }

    #[test]
    fn replay_detects_tampered_fill() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-10-30", "b1", Side::Buy)).unwrap();
        assert_eq!(db.verify_replay().unwrap().fills_verified, 1);
        db.conn.execute("UPDATE v2_fills SET fill_json='{}' WHERE id=1", []).unwrap();
        assert!(db.verify_replay().is_err());
    }

    #[test]
    fn out_of_order_decision_does_not_mutate_the_ledger() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-11-02", "later", Side::Buy)).unwrap();
        let earlier = request("2026-10-30", "earlier", Side::Sell);
        assert!(matches!(db.submit(earlier), Err(StoreError::Conflict(_))));
        assert_eq!(db.state(None).unwrap().runs_total, 1);
        assert_eq!(db.verify_replay().unwrap().runs_verified, 1);
    }

    #[test]
    fn rejection_is_persisted_without_fill() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        let out = db.submit(request("2026-11-02", "s1", Side::Sell)).unwrap();
        assert_eq!(out.outcome.status, DecisionStatus::Rejected);
        assert_eq!(db.verify_replay().unwrap().fills_verified, 0);
    }

    #[test]
    fn buy_and_next_day_sell_replay() {
        let mut db = Store::in_memory(INITIAL_CASH_CENTS).unwrap();
        db.submit(request("2026-10-30", "b1", Side::Buy)).unwrap();
        let blocked = db.submit(request("2026-10-30", "s0", Side::Sell)).unwrap();
        assert_eq!(blocked.outcome.reason, "no_t1_sellable_lot");
        let sold = db.submit(request("2026-11-02", "s1", Side::Sell)).unwrap();
        assert_eq!(sold.outcome.status, DecisionStatus::Filled);
        assert_eq!(db.state(Some("2026-11-02")).unwrap().positions.len(), 0);
        let verified = db.verify_replay().unwrap();
        assert_eq!((verified.runs_verified, verified.fills_verified), (3, 2));
    }
