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

/// Refuse to replace a database that a live process still has open.
///
/// `rename(2)` over an open SQLite file succeeds on Linux, so `--force` would
/// swap the directory entry while the service keeps writing the *old* inode:
/// every post-restore write goes to a file nobody reads any more. On Windows
/// the open handle already blocks the replace, which is why this guard has to
/// be explicit. A missing `lsof` is not treated as proof of safety on POSIX;
/// the operator then has to pass `--force` with the service stopped.
fn database_in_use(target: &Path) -> Result<bool, String> {
    let path = target.to_str().ok_or("non-UTF-8 database path")?;
    let output = std::process::Command::new("lsof")
        .args(["-t", "--", path])
        .output();
    match output {
        Ok(output) if output.status.success() => Ok(!output.stdout.is_empty()),
        Ok(output) => {
            // Exit 1 means "no open file"; anything else (no permission, ...)
            // must not be read as "safe to overwrite".
            if output.status.code() == Some(1) {
                Ok(false)
            } else {
                Err(String::from_utf8_lossy(&output.stderr).trim().to_owned())
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.to_string()),
    }
}

fn check_destination(source: &Path, target: &Path, force: bool, label: &str) -> Result<(), i32> {
    if target.exists() {
        match database_in_use(target) {
            Ok(true) => {
                eprintln!(
                    "error: {label} {} is still open by another process; stop the service before replacing it",
                    target.display()
                );
                return Err(2);
            }
            Ok(false) => {}
            Err(error) => {
                eprintln!(
                    "error: cannot check whether {} is in use: {error}",
                    target.display()
                );
                return Err(2);
            }
        }
    }
    match fs::symlink_metadata(target) {
        Ok(_) if !force => {
            eprintln!(
                "refusing to overwrite {label}: {} (use --force)",
                target.display()
            );
            return Err(3);
        }
        Ok(metadata) => {
            if !metadata.is_file() || fs::canonicalize(source).ok() == fs::canonicalize(target).ok()
            {
                eprintln!(
                    "error: destination must be a separate regular file: {}",
                    target.display()
                );
                return Err(2);
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => {
            eprintln!("error: cannot inspect destination: {error}");
            return Err(2);
        }
    }
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
    Ok(())
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
    let digest = if restore {
        None
    } else {
        Some(sha256_file(&staged).map_err(|error| {
            eprintln!("error: failed to hash backup: {error}");
            2
        })?)
    };
    fs::OpenOptions::new()
        .write(true)
        .open(&staged)
        .and_then(|file| file.sync_all())
        .map_err(|error| {
            eprintln!("error: failed to sync snapshot: {error}");
            2
        })?;
    check_destination(source, target, force, label)?;
    let published = if force {
        fs::rename(&staged, target)
    } else {
        fs::hard_link(&staged, target)
    };
    published.map_err(|error| {
        eprintln!("error: failed to publish {label}: {error}");
        if !force && error.kind() == io::ErrorKind::AlreadyExists {
            3
        } else {
            2
        }
    })?;
    if let Some(digest) = digest {
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
