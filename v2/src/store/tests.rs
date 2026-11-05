
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
            db.submit(request("2026-10-30", "disk-buy", Side::Buy)).unwrap();
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
        let mut req = request("2026-10-30", "expired", Side::Buy);
        req.quote.observed_at = "2026-10-30T09:56:59.999+08:00".into();
        let first = db.submit(req.clone()).unwrap();
        assert_eq!(first.outcome.reason, "stale_quote");
        assert_eq!(db.submit(req).unwrap().outcome, first.outcome);
        let mut req = request("2026-10-30", "boundary", Side::Buy);
        req.quote.observed_at = "2026-10-30T09:57:00+08:00".into();
        assert_eq!(db.submit(req).unwrap().outcome.status, DecisionStatus::Filled);
    }
    let db = Store::open(file.path(), INITIAL_CASH_CENTS).unwrap();
    for _ in 0..2 {
        let report = db.verify_replay().unwrap();
        assert_eq!((report.runs_verified, report.fills_verified), (2, 1));
    }
}
