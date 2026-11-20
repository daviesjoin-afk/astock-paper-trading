//! Repair valuation-only TDX halts in an explicitly designated, closed research copy.

use std::collections::BTreeMap;
use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use chrono::NaiveDate;
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};

use super::ingest::{fetch_package, parse_package};
use super::sha256::{sha256_file, sha256_hex};
use crate::store::Bar;

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const USAGE: &str = "usage: astock-v2 backfill-halted --db <closed research copy> \
                     --confirm-research-copy --manifest <path> --packages <directory>";
const SQLITE_SIDECARS: [&str; 3] = ["-wal", "-shm", "-journal"];

struct Options {
    db: PathBuf,
    manifest: PathBuf,
    packages: PathBuf,
}

#[derive(Deserialize, Serialize)]
struct Manifest {
    schema_version: u32,
    days: BTreeMap<String, DayReport>,
    #[serde(skip_serializing_if = "Option::is_none")]
    database: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    database_before_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    database_after_sha256: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum DayReport {
    Done(CompletedDay),
    NoHalted(CompletedDay),
    Failed {
        error: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        package_sha256: Option<String>,
    },
}

#[derive(Deserialize, Serialize)]
struct CompletedDay {
    package_sha256: String,
    parsed_rows: usize,
    halted_rows: usize,
    inserted: usize,
    updated: usize,
    seconds: f64,
}

fn options(args: &[String]) -> Result<Options> {
    let mut db = None;
    let mut manifest = None;
    let mut packages = None;
    let mut confirmed = false;
    let mut args = args.iter();
    while let Some(flag) = args.next() {
        let target = match flag.as_str() {
            "--db" => &mut db,
            "--manifest" => &mut manifest,
            "--packages" => &mut packages,
            "--confirm-research-copy" if !confirmed => {
                confirmed = true;
                continue;
            }
            _ => return Err(format!("unexpected argument: {flag}").into()),
        };
        let value = args
            .next()
            .filter(|value| !value.is_empty() && !value.starts_with("--"));
        if target.is_some() || value.is_none() {
            return Err(format!("expected one path for {flag}").into());
        }
        *target = value.map(PathBuf::from);
    }
    if !confirmed {
        return Err("--confirm-research-copy requires a closed research copy distinct from the source and live ledger".into());
    }
    Ok(Options {
        db: db.ok_or("--db is required")?,
        manifest: manifest.ok_or("--manifest is required")?,
        packages: packages.ok_or("--packages is required")?,
    })
}

fn open_copy(path: &Path) -> Result<Connection> {
    if !path.is_file() {
        return Err(format!("research database not found: {}", path.display()).into());
    }
    for configured in ["ASTOCK_V2_DB", "ASTOCK_V2_LIVE_DB"] {
        if std::env::var_os(configured)
            .and_then(|value| fs::canonicalize(value).ok())
            .as_deref()
            == Some(path)
        {
            return Err(format!("research copy is the configured database ({configured})").into());
        }
    }
    for default in ["v2.sqlite3", "work/live/intraday.sqlite3"] {
        if fs::canonicalize(default).ok().as_deref() == Some(path) {
            return Err(format!("research copy is a default service database ({default})").into());
        }
    }
    for suffix in SQLITE_SIDECARS {
        match fs::symlink_metadata(sidecar(path, suffix)) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
            Ok(_) => {
                return Err(format!(
                    "database sidecar {suffix} exists; close the research copy cleanly first"
                )
                .into());
            }
        }
    }

    let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    connection.busy_timeout(Duration::ZERO)?;
    // The exclusive lock survives daily commits, preventing another SQLite user
    // from changing the checked copy or contending with this manifest resume.
    connection
        .execute_batch("PRAGMA locking_mode=EXCLUSIVE; BEGIN EXCLUSIVE; COMMIT;")
        .map_err(|error| format!("could not lock closed research copy: {error}"))?;
    for table in ["account", "day_run", "v2_paper_accounts"] {
        let exists: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?1)",
            [table],
            |row| row.get(0),
        )?;
        if exists
            && connection.query_row(
                &format!("SELECT EXISTS(SELECT 1 FROM {table})"),
                [],
                |row| row.get::<_, bool>(0),
            )?
        {
            return Err(format!("research copy contains ledger rows in {table}").into());
        }
    }
    connection
        .prepare(
            "SELECT symbol, day, name, open_cents, high_cents, low_cents, close_cents,
                prev_close_cents, volume_shares, amount_cents, adjust, source, tradable
         FROM bar LIMIT 0",
        )
        .map_err(|error| {
            format!("research copy must already have the bar.tradable schema: {error}")
        })?;
    Ok(connection)
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    name.into()
}

fn parent(path: &Path) -> &Path {
    path.parent()
        .filter(|dir| !dir.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."))
}

// Stage in the destination directory and sync before publishing. A cache entry
// is immutable, so another publisher can win without us replacing its ZIP.
fn publish(path: &Path, bytes: &[u8], replace: bool) -> io::Result<()> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let (mut file, staged) = loop {
        let staged = parent(path).join(format!(
            ".backfill-{}-{}.tmp",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&staged)
        {
            Ok(file) => break (file, staged),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    };
    let written = file.write_all(bytes).and_then(|()| file.sync_all());
    drop(file);
    let published = written.and_then(|()| {
        if replace {
            fs::rename(&staged, path)
        } else {
            fs::hard_link(&staged, path)
        }
    });
    let _ = fs::remove_file(staged);
    published?;
    #[cfg(unix)]
    fs::File::open(parent(path))?.sync_all()?;
    Ok(())
}

fn load_manifest(path: &Path, db: &Path) -> Result<Manifest> {
    let mut manifest = match fs::read(path) {
        Ok(bytes) => serde_json::from_slice::<Manifest>(&bytes)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => Manifest {
            schema_version: 1,
            days: BTreeMap::new(),
            database: None,
            database_before_sha256: None,
            database_after_sha256: None,
        },
        Err(error) => return Err(error.into()),
    };
    if manifest.schema_version != 1 {
        return Err(format!(
            "unsupported manifest schema version {}",
            manifest.schema_version
        )
        .into());
    }
    if let Some(recorded) = &manifest.database {
        if recorded != db && fs::canonicalize(recorded).ok().as_deref() != Some(db) {
            return Err(format!(
                "manifest belongs to a different database: {}",
                recorded.display()
            )
            .into());
        }
    }
    manifest.database = Some(db.to_owned());
    Ok(manifest)
}

fn save_manifest(path: &Path, manifest: &Manifest) -> Result<()> {
    let mut bytes = serde_json::to_vec_pretty(manifest)?;
    bytes.push(b'\n');
    publish(path, &bytes, true)?;
    Ok(())
}

fn load_package(
    path: &Path,
    day: &str,
    fetch: &impl Fn(&str) -> Result<Vec<u8>>,
) -> Result<(Vec<Bar>, String)> {
    let parse = |bytes: &[u8]| -> Result<(Vec<Bar>, String)> {
        Ok((parse_package(bytes, day)?, sha256_hex(bytes)))
    };
    match fs::read(path) {
        Ok(bytes) => parse(&bytes),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let bytes = fetch(day)?;
            let validated = parse(&bytes)?;
            fs::create_dir_all(parent(path))?;
            match publish(path, &bytes, false) {
                Ok(()) => Ok(validated),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                    parse(&fs::read(path)?)
                }
                Err(error) => Err(error.into()),
            }
        }
        Err(error) => Err(error.into()),
    }
}

fn apply_day(connection: &mut Connection, day: &str, rows: &[Bar]) -> Result<(usize, usize)> {
    let mut inserted = 0;
    let mut updated = 0;
    let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
    {
        let mut existing = tx.prepare_cached(
            "SELECT open_cents, high_cents, low_cents, volume_shares, close_cents,
                    prev_close_cents, tradable FROM bar WHERE symbol=?1 AND day=?2",
        )?;
        let mut update = tx.prepare_cached(
            "UPDATE bar SET close_cents=?1, prev_close_cents=?2, tradable=0
             WHERE symbol=?3 AND day=?4",
        )?;
        let mut insert = tx.prepare_cached(
            "INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,close_cents,
                             prev_close_cents,volume_shares,amount_cents,adjust,source,tradable)
             VALUES(?1,?2,?3,0,0,0,?4,?5,0,0,'none','tdx',0)",
        )?;
        for row in rows.iter().filter(|row| !row.tradable) {
            let current = existing
                .query_row(params![row.symbol, day], |row| {
                    Ok((
                        [row.get::<_, i64>(0)?, row.get(1)?, row.get(2)?, row.get(3)?],
                        (
                            row.get::<_, i64>(4)?,
                            row.get::<_, i64>(5)?,
                            row.get::<_, i64>(6)?,
                        ),
                    ))
                })
                .optional()?;
            match current {
                Some((trades, _)) if trades != [0; 4] => {
                    return Err(format!(
                        "official halt conflicts with traded source row: {}:{day}",
                        row.symbol
                    )
                    .into());
                }
                Some((_, stored)) if stored != (row.close_cents, row.prev_close_cents, 0) => {
                    updated += update.execute(params![
                        row.close_cents,
                        row.prev_close_cents,
                        row.symbol,
                        day
                    ])?;
                }
                Some(_) => {}
                None => {
                    inserted += insert.execute(params![
                        row.symbol,
                        day,
                        row.name,
                        row.close_cents,
                        row.prev_close_cents
                    ])?;
                }
            }
        }
    }
    tx.commit()?;
    Ok((inserted, updated))
}

fn backfill(options: &Options, fetch: impl Fn(&str) -> Result<Vec<u8>>) -> Result<bool> {
    let db = fs::canonicalize(&options.db)?;
    let manifest_path = fs::canonicalize(parent(&options.manifest))?.join(
        options
            .manifest
            .file_name()
            .ok_or("--manifest must name a file")?,
    );
    let manifest_target =
        fs::canonicalize(&manifest_path).unwrap_or_else(|_| manifest_path.clone());
    if manifest_target == db {
        return Err("manifest and research database must be distinct files".into());
    }
    if SQLITE_SIDECARS
        .iter()
        .any(|suffix| sidecar(&db, suffix) == manifest_target)
    {
        return Err("manifest path is reserved for a database sidecar".into());
    }
    if fs::canonicalize(&options.packages).ok().as_deref() == manifest_target.parent()
        && manifest_target
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("zip"))
    {
        return Err("manifest path is reserved for a cached ZIP".into());
    }
    let mut connection = open_copy(&db)?;
    let mut manifest = load_manifest(&manifest_path, &db)?;
    let before = sha256_file(&db)?;
    manifest
        .database_before_sha256
        .get_or_insert_with(|| before.clone());
    let days = {
        let mut statement = connection.prepare(
            "SELECT DISTINCT day FROM bar WHERE adjust='none' AND close_cents>0 ORDER BY day",
        )?;
        statement
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
    };
    for day in &days {
        let date = NaiveDate::parse_from_str(day, "%Y-%m-%d")?;
        if day.len() != 10 || day.starts_with("0000") || date.to_string() != *day {
            return Err(format!("invalid calendar day in bar: {day}").into());
        }
    }
    for day in days {
        if matches!(
            manifest.days.get(&day),
            Some(DayReport::Done(_) | DayReport::NoHalted(_))
        ) {
            continue;
        }
        let started = Instant::now();
        let cached = options.packages.join(format!("{day}.zip"));
        let entry = match load_package(&cached, &day, &fetch) {
            Ok((rows, package_sha256)) => {
                let (inserted, updated) = apply_day(&mut connection, &day, &rows)?;
                let report = CompletedDay {
                    package_sha256,
                    parsed_rows: rows.len(),
                    halted_rows: rows.iter().filter(|row| !row.tradable).count(),
                    inserted,
                    updated,
                    seconds: (started.elapsed().as_secs_f64() * 1000.0).round() / 1000.0,
                };
                println!(
                    "{day} parsed={} halted={} inserted={inserted} updated={updated}",
                    report.parsed_rows, report.halted_rows
                );
                if report.halted_rows == 0 {
                    DayReport::NoHalted(report)
                } else {
                    DayReport::Done(report)
                }
            }
            Err(error) => {
                eprintln!("{day} FAILED {error}");
                DayReport::Failed {
                    error: error.to_string(),
                    package_sha256: sha256_file(&cached).ok(),
                }
            }
        };
        manifest.days.insert(day, entry);
        manifest.database_after_sha256 = None;
        save_manifest(&manifest_path, &manifest)?;
    }
    // A closed database may still be configured for WAL. Checkpoint our writes
    // so the hash covers committed rows in the main database file.
    let busy: i64 =
        connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| row.get(0))?;
    if busy != 0 {
        return Err("could not checkpoint the research copy".into());
    }
    let after = sha256_file(&db)?;
    manifest.database_after_sha256 = Some(after.clone());
    save_manifest(&manifest_path, &manifest)?;
    let failed = manifest
        .days
        .values()
        .filter(|entry| matches!(entry, DayReport::Failed { .. }))
        .count();
    println!("database {before} -> {after}; failed days={failed}");
    Ok(failed == 0)
}

pub fn run(args: &[String]) -> i32 {
    if matches!(args, [flag] if flag == "--help" || flag == "-h") {
        println!("{USAGE}");
        return 0;
    }
    let options = match options(args) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("error: {error}\n{USAGE}");
            return 2;
        }
    };
    match backfill(&options, |day| Ok(fetch_package(day)?)) {
        Ok(true) => 0,
        Ok(false) => 1,
        Err(error) => {
            eprintln!("error: {error}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::time::{SystemTime, UNIX_EPOCH};
    use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

    const DAY: &str = "2026-11-03";
    const NEXT_DAY: &str = "2026-11-04";

    struct Fixture {
        root: PathBuf,
        options: Options,
    }

    impl Fixture {
        fn new() -> Self {
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir()
                .join(format!("astock-backfill-{}-{nanos}", std::process::id()));
            fs::create_dir(&root).unwrap();
            let options = Options {
                db: root.join("research-copy.sqlite3"),
                manifest: root.join("manifest.json"),
                packages: root.join("packages"),
            };
            let connection = Connection::open(&options.db).unwrap();
            connection.execute_batch(
                "CREATE TABLE bar(symbol TEXT,day TEXT,name TEXT,open_cents INTEGER,
                    high_cents INTEGER,low_cents INTEGER,close_cents INTEGER,prev_close_cents INTEGER,
                    volume_shares INTEGER,amount_cents INTEGER,adjust TEXT,source TEXT,
                    tradable INTEGER,PRIMARY KEY(symbol,day));",
            ).unwrap();
            let fixture = Self { root, options };
            fixture.seed(DAY);
            fixture
        }

        fn connection(&self) -> Connection {
            Connection::open(&self.options.db).unwrap()
        }

        fn seed(&self, day: &str) {
            self.connection().execute(
                "INSERT INTO bar VALUES ('600000.SH',?1,'existing',0,0,0,1000,980,0,0,'none','tdx',1)",
                [day],
            ).unwrap();
        }

        fn cache(&self, day: &str, bytes: &[u8]) -> PathBuf {
            fs::create_dir_all(&self.options.packages).unwrap();
            let path = self.options.packages.join(format!("{day}.zip"));
            fs::write(&path, bytes).unwrap();
            path
        }

        fn manifest(&self) -> Manifest {
            serde_json::from_slice(&fs::read(&self.options.manifest).unwrap()).unwrap()
        }

        fn args(&self) -> Vec<String> {
            vec![
                "--db".into(),
                self.options.db.to_string_lossy().into_owned(),
                "--manifest".into(),
                self.options.manifest.to_string_lossy().into_owned(),
                "--packages".into(),
                self.options.packages.to_string_lossy().into_owned(),
                "--confirm-research-copy".into(),
            ]
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    // Include priced non-stock rows to exercise the production parser's market
    // floors with a small compressed fixture, without lowering its thresholds.
    fn package(day: &str, halts: bool) -> Vec<u8> {
        let mut archive = ZipWriter::new(Cursor::new(Vec::new()));
        let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        for (market, floor, stock) in [
            ("sh", 10_000, "600000"),
            ("sz", 3_000, "000001"),
            ("bj", 50, "920001"),
        ] {
            let halted = halts && market != "bj";
            let mut codes = vec![0; (floor + 1) * 150];
            let mut quotes = vec![0; (floor + 1) * 512];
            for (index, (record, block)) in codes
                .chunks_exact_mut(150)
                .zip(quotes.chunks_exact_mut(512))
                .enumerate()
            {
                let code = if index == 0 {
                    stock.to_owned()
                } else {
                    format!("{:06}", 200_000 + index)
                };
                record[..6].copy_from_slice(code.as_bytes());
                record[32..34].copy_from_slice(&(index as u16).to_le_bytes());
                record[40..47].copy_from_slice(b"fixture");
                block[4..12].copy_from_slice(&9.90_f64.to_le_bytes());
                if index != 0 || !halted {
                    block[12..20].copy_from_slice(&10.0_f64.to_le_bytes());
                    block[20..28].copy_from_slice(&10.0_f64.to_le_bytes());
                    block[28..36].copy_from_slice(&10.0_f64.to_le_bytes());
                    block[36..44].copy_from_slice(&10.0_f64.to_le_bytes());
                    block[56..64].copy_from_slice(&100_u64.to_le_bytes());
                }
            }
            let suffix = day[2..].replace('-', "");
            for (extension, bytes) in [("cod", codes), ("md1", quotes)] {
                archive
                    .start_file(format!("{market}{suffix}.{extension}"), options)
                    .unwrap();
                archive.write_all(&bytes).unwrap();
            }
        }
        archive.finish().unwrap().into_inner()
    }

    fn offline(day: &str) -> Result<Vec<u8>> {
        panic!("unexpected package download: {day}");
    }

    #[test]
    fn cached_zip_repairs_carried_quote_inserts_halt_and_resumes() {
        let fixture = Fixture::new();
        fixture
            .connection()
            .execute_batch("PRAGMA journal_mode=WAL;")
            .unwrap();
        let before = sha256_file(&fixture.options.db).unwrap();
        let zip = package(DAY, true);
        let cached = fixture.cache(DAY, &zip);
        assert_eq!(run(&fixture.args()), 0);
        let manifest = fixture.manifest();
        let DayReport::Done(report) = &manifest.days[DAY] else {
            panic!("expected done")
        };
        assert_eq!(
            (
                report.parsed_rows,
                report.halted_rows,
                report.inserted,
                report.updated
            ),
            (3, 2, 1, 1)
        );
        assert_eq!(report.package_sha256, sha256_hex(&zip));
        assert_eq!(
            manifest.database_before_sha256.as_deref(),
            Some(before.as_str())
        );
        let after = sha256_file(&fixture.options.db).unwrap();
        assert_eq!(
            manifest.database_after_sha256.as_deref(),
            Some(after.as_str())
        );
        assert_ne!(before, after);
        {
            let connection = fixture.connection();
            let mut statement = connection.prepare(
                "SELECT symbol,name,close_cents,prev_close_cents,tradable FROM bar ORDER BY symbol",
            ).unwrap();
            let rows = statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, i64>(3)?,
                        row.get::<_, i64>(4)?,
                    ))
                })
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap();
            assert_eq!(
                rows,
                vec![
                    ("000001.SZ".into(), "fixture".into(), 990, 990, 0),
                    ("600000.SH".into(), "existing".into(), 990, 990, 0),
                ]
            );
        }
        let published = fs::read(&fixture.options.manifest).unwrap();
        fs::remove_file(cached).unwrap();
        assert!(backfill(&fixture.options, offline).unwrap());
        assert_eq!(fs::read(&fixture.options.manifest).unwrap(), published);
        assert_eq!(sha256_file(&fixture.options.db).unwrap(), after);
        assert!(!fs::read_dir(&fixture.root).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".tmp")
        }));
    }

    #[test]
    fn traded_conflict_rolls_back_earlier_update_and_preserves_manifest() {
        let fixture = Fixture::new();
        fixture.seed(NEXT_DAY);
        fixture.connection().execute(
            "INSERT INTO bar VALUES ('000001.SZ',?1,'traded',0,0,0,1000,980,100,0,'none','tdx',1)",
            [NEXT_DAY],
        ).unwrap();
        fixture.cache(DAY, &package(DAY, false));
        fixture.cache(NEXT_DAY, &package(NEXT_DAY, true));
        let before = sha256_file(&fixture.options.db).unwrap();
        let error = backfill(&fixture.options, offline).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("official halt conflicts with traded source row: 000001.SZ")
        );
        assert_eq!(sha256_file(&fixture.options.db).unwrap(), before);
        let manifest = fixture.manifest();
        assert!(matches!(manifest.days[DAY], DayReport::NoHalted(_)));
        assert_eq!(manifest.days.len(), 1);
        assert!(manifest.database_after_sha256.is_none());
    }

    #[test]
    fn failed_package_is_retried_and_successful_download_is_published() {
        let fixture = Fixture::new();
        fixture.seed(NEXT_DAY);
        let invalid = b"incomplete download";
        let cached = fixture.cache(DAY, invalid);
        fixture.cache(NEXT_DAY, &package(NEXT_DAY, false));
        assert!(!backfill(&fixture.options, offline).unwrap());
        let failed = fixture.manifest();
        assert!(
            matches!(&failed.days[DAY], DayReport::Failed { error, package_sha256 }
            if error.contains("package_not_zip") && *package_sha256 == Some(sha256_hex(invalid)))
        );
        assert!(matches!(failed.days[NEXT_DAY], DayReport::NoHalted(_)));
        fs::remove_file(&cached).unwrap();
        let zip = package(DAY, true);
        assert!(
            backfill(&fixture.options, |day| {
                assert_eq!(day, DAY);
                Ok(zip.clone())
            })
            .unwrap()
        );
        assert_eq!(fs::read(cached).unwrap(), zip);
        assert!(
            matches!(&fixture.manifest().days[DAY], DayReport::Done(report)
            if report.package_sha256 == sha256_hex(&zip))
        );
    }

    #[test]
    fn copy_must_be_confirmed_closed_and_free_of_ledger_rows() {
        let fixture = Fixture::new();
        let mut args = fixture.args();
        args.pop();
        assert_eq!(run(&args), 2);
        let before = sha256_file(&fixture.options.db).unwrap();
        let journal = fixture.root.join("research-copy.sqlite3-wal");
        fs::write(&journal, []).unwrap();
        assert!(
            backfill(&fixture.options, offline)
                .unwrap_err()
                .to_string()
                .contains("sidecar")
        );
        fs::remove_file(journal).unwrap();
        let active = fixture.connection();
        active.execute_batch("BEGIN IMMEDIATE;").unwrap();
        assert!(
            backfill(&fixture.options, offline)
                .unwrap_err()
                .to_string()
                .contains("could not lock")
        );
        drop(active);
        assert_eq!(sha256_file(&fixture.options.db).unwrap(), before);
        fixture
            .connection()
            .execute_batch("CREATE TABLE account(id TEXT); INSERT INTO account VALUES ('demo');")
            .unwrap();
        assert!(
            backfill(&fixture.options, offline)
                .unwrap_err()
                .to_string()
                .contains("ledger rows")
        );
        assert!(!fixture.options.manifest.exists());
    }

    #[test]
    fn manifest_bound_to_a_different_copy_is_refused() {
        let mut fixture = Fixture::new();
        let other = Fixture::new();
        fs::write(
            &fixture.options.manifest,
            serde_json::to_vec(&serde_json::json!({
                "schema_version": 1, "days": {}, "database": other.options.db,
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(
            backfill(&fixture.options, offline)
                .unwrap_err()
                .to_string()
                .contains("different database")
        );
        fixture.options.manifest = fixture.options.db.clone();
        assert!(
            backfill(&fixture.options, offline)
                .unwrap_err()
                .to_string()
                .contains("distinct files")
        );
    }
}
