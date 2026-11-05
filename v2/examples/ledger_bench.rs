//! Offline measurement of the real Store path; no bulk inserts or cached state.
use astock_v2::{domain::*, store::Store};
use chrono::{Datelike, NaiveDate};
use serde_json::{json, Value};
use std::{error::Error, path::PathBuf, time::Instant};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Result<Self> {
        for id in 0..10_000 {
            let path = std::env::temp_dir().join(format!("astock-v2-bench-{}-{id}", std::process::id()));
            match std::fs::create_dir(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e.into()),
            }
        }
        Err("no unused benchmark directory".into())
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        // Only files created by this benchmark, never user database paths.
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let _ = std::fs::remove_file(self.0.join(format!("ledger.sqlite3{suffix}")));
        }
        let _ = std::fs::remove_dir(&self.0);
    }
}

fn distribution(values: &[f64]) -> Value {
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let n = sorted.len();
    json!({"samples": n, "mean_ms": sorted.iter().sum::<f64>() / n as f64,
        "p50_ms": sorted[(n - 1) / 2], "p95_ms": sorted[(95 * n).div_ceil(100) - 1]})
}

fn timed<T>(operation: impl FnOnce() -> Result<T>) -> Result<(T, f64)> {
    let start = Instant::now();
    let value = operation()?;
    Ok((value, start.elapsed().as_secs_f64() * 1000.0))
}

fn measure(size: usize, disk: bool, fills: bool, days: &[NaiveDate]) -> Result<Value> {
    let scratch = Scratch::new()?;
    let path = scratch.0.join("ledger.sqlite3");
    let path = path.to_str().ok_or("non-UTF8 temporary path")?;
    let mut db = if disk { Store::open(path, INITIAL_CASH_CENTS)? }
        else { Store::in_memory(INITIAL_CASH_CENTS)? };
    let cutoff = days[if fills { ((size - 1) / 25) / 2 } else { 0 }].to_string();
    let mut expected_historical = None;
    let mut submit_ms = Vec::with_capacity(size);
    let mut ingest_ms = Vec::with_capacity(size);
    let mut filled = 0;
    let mut last_request = None;
    for i in 0..size {
        let day_index = if fills { i / 25 } else { 0 };
        let day = days[day_index].to_string();
        let symbol = format!("{:06}.SH", 600000 + if fills { i % 25 } else { 0 });
        let req = SimulationRequest {
            schema_version: 2, cycle_id: "ledger-bench-v2".into(),
            strategy_version_id: "offline-fixed-v1".into(), signal_id: format!("signal-{i}"),
            decision_at: format!("{day}T10:00:00+08:00"),
            signal: Signal { symbol: symbol.clone(),
                side: if fills && day_index % 2 == 1 { Side::Sell } else { Side::Buy } },
            quote_ref: QuoteRef { source: "offline-fixture".into(), quote_id: format!("quote-{i}") },
        };
        let input = QuoteImport {
            schema_version: 1, available_at: format!("{day}T10:00:00+08:00"),
            execution: Some(ExecutionLiquidity {
                schema_version: 1, quote_ref: req.quote_ref.clone(),
                order_side: req.signal.side.clone(), price_cents: 100,
                available_quantity: 1_000_000,
                observed_at: format!("{day}T09:59:59+08:00"),
            }),
            quote: Quote { symbol, price_cents: 100,
                observed_at: format!("{day}T09:59:59+08:00"),
                source: req.quote_ref.source.clone(), quote_id: req.quote_ref.quote_id.clone(),
                tradable: true, limit_up: false, limit_down: false },
        };
        let (_, elapsed) = timed(|| Ok(db.ingest_quote(input)?))?;
        ingest_ms.push(elapsed);
        let (record, elapsed) = timed(|| Ok(db.submit(req.clone())?))?;
        submit_ms.push(elapsed);
        if fills || i == 0 {
            assert_eq!(record.outcome.status, DecisionStatus::Filled);
            filled += 1;
        } else {
            assert_eq!(record.outcome.reason, "position_already_open");
        }
        // Capture the prefix before any later day is appended, outside timing.
        if day == cutoff && (i + 1 == size || (fills && (i + 1) % 25 == 0)) {
            expected_historical = Some(db.state(Some(&cutoff))?);
        }
        last_request = Some(req);
    }
    let expected_historical = expected_historical.ok_or("missing cutoff prefix")?;
    let latest = db.state(None)?;
    assert_eq!(latest.runs_total, size as i64);
    let mut latest_ms = Vec::new();
    let mut historical_ms = Vec::new();
    let mut replay_ms = Vec::new();
    for _ in 0..9 {
        let (view, elapsed) = timed(|| Ok(db.state(None)?))?;
        assert_eq!(view, latest);
        latest_ms.push(elapsed);
        let (view, elapsed) = timed(|| Ok(db.state(Some(&cutoff))?))?;
        assert_eq!(view, expected_historical);
        historical_ms.push(elapsed);
    }
    let mut report = None;
    for _ in 0..3 {
        let (verified, elapsed) = timed(|| Ok(db.verify_replay()?))?;
        assert!(verified.ok);
        assert_eq!((verified.runs_verified, verified.fills_verified), (size, filled));
        assert_eq!(verified.cash_cents, latest.cash_cents);
        replay_ms.push(elapsed);
        report = Some(verified);
    }
    // Idempotency and failed conflicting submission must not change any view.
    let req = last_request.unwrap();
    assert!(db.submit(req.clone())?.duplicate);
    let mut conflict = req;
    conflict.quote_ref.quote_id = "conflict".into();
    assert!(db.submit(conflict).is_err());
    assert_eq!(db.state(None)?, latest);
    assert_eq!(db.state(Some(&cutoff))?, expected_historical);
    assert_eq!(Some(db.verify_replay()?), report);
    if disk {
        drop(db);
        let reopened = Store::open(path, INITIAL_CASH_CENTS)?;
        assert_eq!(reopened.state(None)?, latest);
        assert_eq!(reopened.state(Some(&cutoff))?, expected_historical);
        assert_eq!(Some(reopened.verify_replay()?), report);
    }
    let window = size.min(50);
    Ok(json!({"storage": if disk { "disk_wal" } else { "memory" },
        "scenario": if fills { "all_fills" } else { "mostly_rejected" },
        "runs": size, "fills": filled, "cutoff": cutoff,
        "historical_runs": expected_historical.runs_total,
        "submit_total_ms": submit_ms.iter().sum::<f64>(),
        "submit": distribution(&submit_ms), "ingest": distribution(&ingest_ms),
        "submit_first_50": distribution(&submit_ms[..window]),
        "submit_last_50": distribution(&submit_ms[size-window..]),
        "state_latest": distribution(&latest_ms), "state_historical": distribution(&historical_ms),
        "verify_replay": distribution(&replay_ms), "correctness_checks": "passed"}))
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let sizes: Vec<usize> = if args.is_empty() { vec![100, 1000, 5000] }
        else { args.iter().map(|v| v.parse()).collect::<std::result::Result<_, _>>()? };
    let calendar: TradingCalendar = serde_json::from_str(BUNDLED_CALENDAR_JSON)?;
    let mut days = Vec::new();
    let mut day = NaiveDate::from_ymd_opt(2026, 1, 1).unwrap();
    while day.year_ce().1 == 2026 {
        if calendar.day(day)? == CalendarDay::Trading { days.push(day); }
        day = day.succ_opt().unwrap();
    }
    if sizes.iter().any(|&n| n == 0 || n > 5000) {
        return Err("sizes must be in 1..=5000 (fixed 2026 offline coverage)".into());
    }
    let mut cases = Vec::new();
    for size in sizes {
        for disk in [false, true] {
            for fills in [true, false] {
                eprintln!("benchmark runs={size} disk={disk} all_fills={fills}");
                cases.push(measure(size, disk, fills, &days)?);
            }
        }
    }
    println!("{}", serde_json::to_string_pretty(&json!({
        "generator": "ledger-bench-v2", "profile": if cfg!(debug_assertions) { "debug" } else { "release" },
        "os": std::env::consts::OS, "arch": std::env::consts::ARCH,
        "logical_cpus": std::thread::available_parallelism()?.get(),
        "sqlite": rusqlite::version(), "cases": cases
    }))?);
    Ok(())
}
