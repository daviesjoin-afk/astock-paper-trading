//! Online backup, restore and verification for V2 SQLite databases.
//! Uses SQLite snapshots that include committed WAL data.

use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

use rusqlite::{Connection, OpenFlags};

use super::sha256::sha256_file;
use crate::store::SCHEMA_VERSION;

#[derive(Debug, PartialEq, Eq)]
pub struct DbFacts {
    pub integrity: String,
    pub schema_version: i64,
    pub accounts: i64,
    pub settled_days: i64,
}

pub fn inspect(path: &Path) -> Result<DbFacts, String> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|err| err.to_string())?;
    let legacy: i64 = conn
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(|err| err.to_string())?;
    if legacy != 0 {
        return Err(format!("unsupported SQLite user_version {legacy}"));
    }
    let integrity: String = conn
        .query_row("PRAGMA integrity_check", [], |row| row.get(0))
        .map_err(|err| err.to_string())?;
    let has_schema: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_version'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|err| err.to_string())?
        > 0;
    let has_account: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='account'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|err| err.to_string())?
        > 0;
    if !has_schema || !has_account {
        return Err("not a V2 rewrite database".to_string());
    }
    let schema_version: i64 = conn
        .query_row("SELECT version FROM schema_version LIMIT 1", [], |row| {
            row.get(0)
        })
        .map_err(|err| err.to_string())?;
    let accounts: i64 = conn
        .query_row("SELECT COUNT(*) FROM account", [], |row| row.get(0))
        .map_err(|err| err.to_string())?;
    let settled_days: i64 = conn
        .query_row("SELECT COUNT(*) FROM day_run", [], |row| row.get(0))
        .map_err(|err| err.to_string())?;
    Ok(DbFacts {
        integrity,
        schema_version,
        accounts,
        settled_days,
    })
}

pub fn require_ok(path: &Path, label: &str) -> Result<DbFacts, i32> {
    if !path.is_file() {
        eprintln!("error: {label} not found: {}", path.display());
        return Err(2);
    }
    let facts = match inspect(path) {
        Ok(facts) => facts,
        Err(err) => {
            eprintln!("INVALID: {label} is not a readable database: {err}");
            return Err(1);
        }
    };
    if facts.integrity != "ok" {
        eprintln!("INVALID: integrity check failed: {}", facts.integrity);
        return Err(1);
    }
    if facts.schema_version != SCHEMA_VERSION {
        eprintln!(
            "INVALID: expected V2 schema {SCHEMA_VERSION}, found {}",
            facts.schema_version
        );
        return Err(1);
    }
    Ok(facts)
}

struct StagingDirectory(PathBuf);

impl StagingDirectory {
    fn create(parent: &Path) -> io::Result<Self> {
        static NEXT_ID: AtomicU64 = AtomicU64::new(0);
        loop {
            let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
            let path = parent.join(format!(".astock-v2-db-{}-{id}", std::process::id()));
            match fs::create_dir(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
    }
}

impl Drop for StagingDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn snapshot(source: &Path, staged: &Path) -> Result<(), String> {
    let connection = Connection::open_with_flags(
        source,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| error.to_string())?;
    let staged = staged.to_str().ok_or("non-UTF-8 staging path")?;
    connection
        .execute("VACUUM INTO ?1", [staged])
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn check_destination(source: &Path, target: &Path, force: bool, label: &str) -> Result<bool, i32> {
    let exists = match fs::symlink_metadata(target) {
        Ok(_) if !force => {
            eprintln!(
                "refusing to overwrite {label}: {} (use --force)",
                target.display()
            );
            return Err(3);
        }
        Ok(metadata) => {
            if !metadata.is_file() || !matches!(same_file::is_same_file(source, target), Ok(false))
            {
                eprintln!(
                    "error: destination must be a separate regular file: {}",
                    target.display()
                );
                return Err(2);
            }
            true
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => {
            eprintln!("error: cannot inspect destination: {error}");
            return Err(2);
        }
    };
    for suffix in ["-wal", "-shm", "-journal"] {
        let mut sidecar = target.as_os_str().to_os_string();
        sidecar.push(suffix);
        match fs::symlink_metadata(sidecar) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            _ => {
                eprintln!(
                    "error: database sidecar {suffix} exists or is unreadable. Close the database before replacing it: {}",
                    target.display()
                );
                return Err(2);
            }
        }
    }
    Ok(exists)
}

/// Keep the destination inode and let SQLite acquire its own write lock and
/// commit or roll back the replacement. A file-level rename can orphan a
/// connection that opens the destination after a separate occupancy probe.
fn replace_database(staged: &Path, target: &Path) -> Result<(), String> {
    let source = Connection::open_with_flags(staged, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|error| error.to_string())?;
    let mut destination = Connection::open_with_flags(target, OpenFlags::SQLITE_OPEN_READ_WRITE)
        .map_err(|error| error.to_string())?;
    destination
        .busy_timeout(std::time::Duration::ZERO)
        .map_err(|error| error.to_string())?;
    destination
        .pragma_update(None, "synchronous", "FULL")
        .map_err(|error| error.to_string())?;
    let copy = rusqlite::backup::Backup::new(&source, &mut destination)
        .map_err(|error| error.to_string())?;
    match copy.step(-1).map_err(|error| error.to_string())? {
        rusqlite::backup::StepResult::Done => Ok(()),
        _ => Err("destination database is busy or locked".into()),
    }
}

fn publish_snapshot(source: &Path, target: &Path, force: bool, restore: bool) -> Result<(), i32> {
    require_ok(source, if restore { "backup" } else { "database" })?;
    let label = if restore { "database" } else { "backup" };
    check_destination(source, target, force, label)?;
    let parent = target
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let staging = fs::create_dir_all(parent)
        .and_then(|()| StagingDirectory::create(parent))
        .map_err(|error| {
            eprintln!("error: cannot stage {label}: {error}");
            2
        })?;
    let staged = staging.0.join("snapshot.sqlite3");
    snapshot(source, &staged).map_err(|error| {
        eprintln!("INVALID: online snapshot failed: {error}");
        1
    })?;
    let facts = require_ok(&staged, label)?;
    fs::OpenOptions::new()
        .write(true)
        .open(&staged)
        .and_then(|file| file.sync_all())
        .map_err(|error| {
            eprintln!("error: failed to sync snapshot: {error}");
            2
        })?;
    let existing = check_destination(source, target, force, label)?;
    if existing {
        require_ok(target, label)?;
        replace_database(&staged, target).map_err(|error| {
            eprintln!("error: cannot replace {label} via SQLite: {error}");
            2
        })?;
    } else {
        fs::hard_link(&staged, target).map_err(|error| {
            eprintln!("error: failed to publish {label}: {error}");
            if error.kind() == io::ErrorKind::AlreadyExists {
                3
            } else {
                2
            }
        })?;
    }
    if !restore {
        let digest = sha256_file(target).map_err(|error| {
            eprintln!("error: failed to hash backup: {error}");
            2
        })?;
        println!(
            "backup ok schema={} accounts={} settled_days={} out={} sha256={digest}",
            facts.schema_version,
            facts.accounts,
            facts.settled_days,
            target.display()
        );
    } else {
        println!(
            "restore ok schema={} accounts={} settled_days={} db={}",
            facts.schema_version,
            facts.accounts,
            facts.settled_days,
            target.display()
        );
    }
    Ok(())
}

pub fn backup_db(source: &Path, target: &Path, force: bool) -> i32 {
    publish_snapshot(source, target, force, false)
        .err()
        .unwrap_or(0)
}

pub fn restore_db(backup: &Path, target: &Path, force: bool) -> i32 {
    publish_snapshot(backup, target, force, true)
        .err()
        .unwrap_or(0)
}

pub fn verify_db(path: &Path) -> i32 {
    let facts = match require_ok(path, "database") {
        Ok(f) => f,
        Err(code) => return code,
    };
    println!(
        "verify ok schema={} accounts={} settled_days={}",
        facts.schema_version, facts.accounts, facts.settled_days
    );
    0
}

pub fn run(args: &[String]) -> i32 {
    const USAGE: &str = "usage: astock-v2 db <backup|restore|verify> ...";
    let Some((sub, rest)) = args.split_first() else {
        eprintln!("{USAGE}");
        return 2;
    };
    let usage = match sub.as_str() {
        "backup" => "usage: astock-v2 db backup --db <path> --out <path> [--force]",
        "restore" => "usage: astock-v2 db restore --backup <path> --db <path> [--force]",
        "verify" => "usage: astock-v2 db verify --db <path>",
        "-h" | "--help" => {
            println!("{USAGE}");
            return 0;
        }
        other => {
            eprintln!("error: unknown db subcommand: {other}");
            return 2;
        }
    };

    let mut db = None;
    let mut out = None;
    let mut backup = None;
    let mut force = false;
    let mut flags = rest.iter().map(String::as_str).peekable();
    while let Some(arg) = flags.next() {
        let (flag, inline) = arg
            .split_once('=')
            .map_or((arg, None), |(f, v)| (f, Some(v)));
        let destination = match flag {
            "--db" => &mut db,
            "--out" if sub == "backup" => &mut out,
            "--backup" if sub == "restore" => &mut backup,
            "--force" if sub != "verify" && inline.is_none() => {
                force = true;
                continue;
            }
            "-h" | "--help" if inline.is_none() => {
                println!("{usage}");
                return 0;
            }
            _ => {
                eprintln!("error: unknown argument: {arg}");
                return 2;
            }
        };
        let value =
            inline.or_else(|| flags.next_if(|next| !next.starts_with("--") && *next != "-h"));
        let Some(value) = value else {
            eprintln!("error: missing value for {flag}");
            return 2;
        };
        *destination = Some(Path::new(value));
    }
    match (sub.as_str(), db, out, backup) {
        ("backup", Some(src), Some(dst), _) => backup_db(src, dst, force),
        ("restore", Some(dst), _, Some(src)) => restore_db(src, dst, force),
        ("verify", Some(path), _, _) => verify_db(path),
        _ => {
            eprintln!("{usage}");
            2
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(label: &str) -> std::path::PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("astock-db-tool-{label}-{nanos}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn seed_v2_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE schema_version(version INTEGER NOT NULL);
             INSERT INTO schema_version VALUES(2);
             CREATE TABLE account(account_id TEXT PRIMARY KEY);
             CREATE TABLE day_run(account_id TEXT, day TEXT);",
        )
        .unwrap();
    }

    #[test]
    fn cli_preserves_help_and_rejects_invalid_flags() {
        let call =
            |args: &[&str]| run(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>());
        assert_eq!(call(&["--help"]), 0);
        assert_eq!(call(&["backup", "--help"]), 0);
        assert_eq!(call(&["restore", "-h"]), 0);
        assert_eq!(call(&["verify", "--help"]), 0);
        assert_eq!(call(&["backup", "--db", "--out", "backup.sqlite3"]), 2);
        assert_eq!(call(&["verify", "--force", "--db=anything.sqlite3"]), 2);
    }

    #[test]
    fn backup_verify_and_restore_round_trip() {
        let dir = temp_dir("roundtrip");
        let db = dir.join("v2.sqlite3");
        let bak = dir.join("backup.sqlite3");
        let restored = dir.join("restored.sqlite3");
        seed_v2_db(&db);

        assert_eq!(backup_db(&db, &bak, false), 0);
        assert_eq!(verify_db(&bak), 0);
        assert_eq!(
            backup_db(&db, &bak, false),
            3,
            "must refuse overwrite without --force"
        );
        assert_eq!(backup_db(&db, &bak, true), 0);
        assert_eq!(restore_db(&bak, &restored, false), 0);
        assert_eq!(restore_db(&bak, &restored, false), 3);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn forced_operations_preserve_existing_files_when_validation_fails() {
        let dir = temp_dir("preserve");
        let db = dir.join("source.sqlite3");
        let target = dir.join("target.sqlite3");
        seed_v2_db(&db);
        seed_v2_db(&target);
        let original = fs::read(&target).unwrap();
        assert_eq!(backup_db(&target, &target, true), 2);
        let connection = Connection::open(&db).unwrap();
        connection
            .execute("UPDATE schema_version SET version = 999", [])
            .unwrap();
        drop(connection);
        assert_eq!(backup_db(&db, &target, true), 1);
        assert_eq!(restore_db(&db, &target, true), 1);
        assert_eq!(fs::read(&target).unwrap(), original);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn restore_preserves_committed_wal_rows_and_rejects_active_destination() {
        let dir = temp_dir("wal");
        let db = dir.join("source.sqlite3");
        let restored = dir.join("restored.sqlite3");
        seed_v2_db(&db);
        let writer = Connection::open(&db).unwrap();
        writer
            .execute_batch(
                "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
            INSERT INTO day_run VALUES ('demo', '2026-11-20');",
            )
            .unwrap();
        assert_eq!(restore_db(&db, &restored, false), 0);
        assert_eq!(inspect(&restored).unwrap().settled_days, 1);
        assert_eq!(restore_db(&restored, &db, true), 2);
        drop(writer);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn force_uses_sqlite_locking_without_replacing_the_file() {
        let dir = temp_dir("sqlite-locking");
        let backup = dir.join("backup.sqlite3");
        let target = dir.join("target.sqlite3");
        seed_v2_db(&backup);
        seed_v2_db(&target);
        let source = Connection::open(&backup).unwrap();
        source
            .execute("INSERT INTO account VALUES ('new')", [])
            .unwrap();
        drop(source);

        let target_identity = same_file::Handle::from_path(&target).unwrap();
        let reader = Connection::open(&target).unwrap();
        reader
            .execute_batch("BEGIN; SELECT * FROM account;")
            .unwrap();
        assert_eq!(restore_db(&backup, &target, true), 2);
        assert_eq!(inspect(&target).unwrap().accounts, 0);
        reader.execute_batch("ROLLBACK;").unwrap();
        drop(reader);

        assert_eq!(restore_db(&backup, &target, true), 0);
        assert_eq!(inspect(&target).unwrap().accounts, 1);
        assert_eq!(
            target_identity,
            same_file::Handle::from_path(&target).unwrap()
        );
        drop(target_identity);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn force_refuses_hard_link_alias_and_incompatible_destination() {
        let dir = temp_dir("alias");
        let db = dir.join("source.sqlite3");
        let alias = dir.join("alias.sqlite3");
        let incompatible = dir.join("incompatible.sqlite3");
        seed_v2_db(&db);
        fs::hard_link(&db, &alias).unwrap();
        assert_eq!(restore_db(&db, &alias, true), 2);
        seed_v2_db(&incompatible);
        let connection = Connection::open(&incompatible).unwrap();
        connection.execute_batch("PRAGMA user_version=1;").unwrap();
        drop(connection);
        let bytes = fs::read(&incompatible).unwrap();
        assert_eq!(restore_db(&db, &incompatible, true), 1);
        assert_eq!(fs::read(&incompatible).unwrap(), bytes);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn non_v2_database_fails_verification() {
        let dir = temp_dir("nonv2");
        let bad = dir.join("plain.sqlite3");
        let conn = Connection::open(&bad).unwrap();
        conn.execute("CREATE TABLE unrelated(id INTEGER)", [])
            .unwrap();
        drop(conn);

        assert_eq!(verify_db(&bad), 1);
        let _ = fs::remove_dir_all(&dir);
    }
}
