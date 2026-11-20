//! Online backup, restore and verification for V2 SQLite databases.
//! Rust equivalent of `v2/scripts/db_tool.py` using SQLite's online backup API.

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

fn check_destination(source: &Path, target: &Path, force: bool, label: &str) -> Result<(), i32> {
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
    let Some((sub, rest)) = args.split_first() else {
        eprintln!("usage: astock-v2 db <backup|restore|verify> ...");
        return 2;
    };
    let mut db = None;
    let mut out = None;
    let mut backup = None;
    let mut force = false;
    let mut i = 0;
    while i < rest.len() {
        match rest[i].as_str() {
            "--db" if i + 1 < rest.len() => {
                db = Some(Path::new(&rest[i + 1]).to_path_buf());
                i += 2;
            }
            "--out" if i + 1 < rest.len() => {
                out = Some(Path::new(&rest[i + 1]).to_path_buf());
                i += 2;
            }
            "--backup" if i + 1 < rest.len() => {
                backup = Some(Path::new(&rest[i + 1]).to_path_buf());
                i += 2;
            }
            "--force" => {
                force = true;
                i += 1;
            }
            other => {
                eprintln!("error: unknown argument: {other}");
                return 2;
            }
        }
    }
    match sub.as_str() {
        "backup" => match (db, out) {
            (Some(src), Some(dst)) => backup_db(&src, &dst, force),
            _ => {
                eprintln!("usage: astock-v2 db backup --db <path> --out <path> [--force]");
                2
            }
        },
        "restore" => match (backup, db) {
            (Some(src), Some(dst)) => restore_db(&src, &dst, force),
            _ => {
                eprintln!("usage: astock-v2 db restore --backup <path> --db <path> [--force]");
                2
            }
        },
        "verify" => match db {
            Some(path) => verify_db(&path),
            None => {
                eprintln!("usage: astock-v2 db verify --db <path>");
                2
            }
        },
        other => {
            eprintln!("error: unknown db subcommand: {other}");
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
