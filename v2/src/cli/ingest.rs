//! TDX after-close daily bars. The parser and HTTPS/cache transport are shared
//! by operational callers; the Store owns transactional bar writes.

use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::{self, Cursor, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, NaiveDate, Utc};
use encoding_rs::GBK;
use serde::{Deserialize, Serialize};
use zip::{ZipArchive, result::ZipError};

use super::{http, quotes, sha256::sha256_hex};
use crate::store::{Bar, Store};

const CODE_SIZE: usize = 150;
const BLOCK_SIZE: usize = 512;
const MAX_RECORDS: usize = 1 + u16::MAX as usize;
const MAX_PACKAGE_BYTES: usize = 64 * 1024 * 1024;
const BJ_FIRST_DAY: &str = "2022-05-06";
const MARKETS: [(&str, usize, &[&str]); 3] = [
    ("sh", 10_000, &["600", "601", "603", "605", "688", "689"]),
    ("sz", 3_000, &["000", "001", "002", "003", "300", "301"]),
    ("bj", 50, &["43", "83", "87", "88", "92"]),
];

#[derive(Debug)]
pub struct IngestError {
    pub code: &'static str,
    pub detail: String,
}

impl IngestError {
    pub(crate) fn new(code: &'static str, detail: impl ToString) -> Self {
        Self {
            code,
            detail: detail.to_string(),
        }
    }
}

impl fmt::Display for IngestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

impl std::error::Error for IngestError {}

pub fn is_ordinary_a_share(market: &str, code: &str) -> bool {
    MARKETS.iter().any(|(exchange, _, prefixes)| {
        market == *exchange && prefixes.iter().any(|prefix| code.starts_with(prefix))
    })
}

fn parse_day(day: &str) -> Result<NaiveDate, IngestError> {
    NaiveDate::parse_from_str(day, "%Y-%m-%d")
        .ok()
        .filter(|date| !day.starts_with("0000") && day.len() == 10 && date.to_string() == day)
        .ok_or_else(|| IngestError::new("invalid_day", day))
}

/// Expand an ISO day or an inclusive ISO calendar range, including weekends.
pub fn expand_days(spec: &str) -> Result<Vec<String>, IngestError> {
    let Some((start, end)) = spec.split_once(':') else {
        return Ok(vec![parse_day(spec)?.to_string()]);
    };
    let invalid = || IngestError::new("bad_day_range", spec);
    let first = parse_day(start).map_err(|_| invalid())?;
    let last = parse_day(end).map_err(|_| invalid())?;
    if first > last {
        return Err(invalid());
    }
    Ok(first
        .iter_days()
        .take((last - first).num_days() as usize + 1)
        .map(|day| day.to_string())
        .collect())
}

fn read_member(
    archive: &mut ZipArchive<Cursor<&[u8]>>,
    name: &str,
    record_size: usize,
) -> Result<Vec<u8>, IngestError> {
    let zip_error = |err: ZipError| {
        let code = match err {
            ZipError::FileNotFound => "package_missing_file",
            _ => "package_bad_member",
        };
        IngestError::new(code, format!("{name}: {err}"))
    };
    let mut entry = archive.by_name(name).map_err(zip_error)?;
    let limit = (MAX_RECORDS * record_size) as u64;
    if entry.size() > limit {
        return Err(IngestError::new("package_bad_block_size", name));
    }
    let mut bytes = Vec::with_capacity(entry.size() as usize);
    let expected = entry.size();
    (&mut entry)
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|err| IngestError::new("package_bad_member", format!("{name}: {err}")))?;
    if bytes.len() as u64 != expected || bytes.len() % record_size != 0 {
        return Err(IngestError::new("package_bad_block_size", name));
    }
    Ok(bytes)
}

fn quote_field(block: &[u8], offset: usize) -> f64 {
    f64::from_le_bytes(block[offset..offset + 8].try_into().unwrap())
}

fn cents(value: f64, symbol: &str) -> Result<i64, IngestError> {
    let result = (value * 100.0).round_ties_even();
    if !(i64::MIN as f64..-(i64::MIN as f64)).contains(&result) {
        return Err(IngestError::new("package_value_out_of_range", symbol));
    }
    Ok(result as i64)
}

/// Decode a complete ZIP for an ISO day or Python-compatible `YYYYMMDD` day.
/// Returns SH, SZ, BJ rows in package order after validating each market.
pub fn parse_package(content: &[u8], day: &str) -> Result<Vec<Bar>, IngestError> {
    let iso_day = if day.len() == 8 && day.bytes().all(|b| b.is_ascii_digit()) {
        format!("{}-{}-{}", &day[..4], &day[4..6], &day[6..])
    } else {
        day.to_owned()
    };
    let date = parse_day(&iso_day)?;
    let suffix = date.format("%y%m%d");
    let mut archive = ZipArchive::new(Cursor::new(content))
        .map_err(|err| IngestError::new("package_not_zip", err))?;
    let mut rows = Vec::new();
    for (market, floor, _) in MARKETS {
        let cod_name = format!("{market}{suffix}.cod");
        let md1_name = format!("{market}{suffix}.md1");
        let has_cod = archive.index_for_name(&cod_name).is_some();
        let has_md1 = archive.index_for_name(&md1_name).is_some();
        if market == "bj" && iso_day.as_str() < BJ_FIRST_DAY && !has_cod && !has_md1 {
            continue;
        }
        if !has_cod || !has_md1 {
            return Err(IngestError::new(
                "package_missing_file",
                format!("{cod_name}/{md1_name}"),
            ));
        }
        let cod = read_member(&mut archive, &cod_name, CODE_SIZE)?;
        let md1 = read_member(&mut archive, &md1_name, BLOCK_SIZE)?;
        let count = cod.len() / CODE_SIZE;
        if count != md1.len() / BLOCK_SIZE {
            return Err(IngestError::new("package_length_mismatch", market));
        }

        let before = rows.len();
        let mut priced = 0;
        let mut seen_codes = std::collections::HashSet::with_capacity(count);
        let mut seen_seqs = vec![false; count];
        for record in cod.chunks_exact(CODE_SIZE) {
            let code = &record[..6];
            if !code.iter().all(u8::is_ascii_digit) {
                return Err(IngestError::new("package_bad_code", market));
            }
            let code = std::str::from_utf8(code).unwrap();
            let seq = u16::from_le_bytes(record[32..34].try_into().unwrap()) as usize;
            let seen = seen_seqs.get_mut(seq).ok_or_else(|| {
                IngestError::new("package_block_out_of_range", format!("{market}{code}"))
            })?;
            if !seen_codes.insert(code) || std::mem::replace(seen, true) {
                return Err(IngestError::new(
                    "package_duplicate",
                    format!("{market} {code} seq={seq}"),
                ));
            }

            let block = &md1[seq * BLOCK_SIZE..(seq + 1) * BLOCK_SIZE];
            let prev = quote_field(block, 4);
            let mut open = quote_field(block, 12);
            let mut high = quote_field(block, 20);
            let mut low = quote_field(block, 28);
            let mut close = quote_field(block, 36);
            let mut amount = quote_field(block, 72);
            if ![prev, open, high, low, close, amount]
                .into_iter()
                .all(f64::is_finite)
            {
                return Err(IngestError::new(
                    "package_non_finite",
                    format!("{market}{code}"),
                ));
            }
            priced += usize::from(close > 0.0);
            if !is_ordinary_a_share(market, code) {
                continue;
            }
            let mut volume = u64::from_le_bytes(block[56..64].try_into().unwrap());
            let tradable =
                close > 0.0 && !(volume == 0 && open == 0.0 && high == 0.0 && low == 0.0);
            if !tradable {
                if prev <= 0.0 {
                    continue;
                }
                close = prev;
                (open, high, low, amount, volume) = (0.0, 0.0, 0.0, 0.0, 0);
            }
            let symbol = quotes::to_symbol(market, code);
            let raw_name = record[40..72].split(|byte| *byte == 0).next().unwrap();
            let name = decode_name(raw_name)
                .ok_or_else(|| IngestError::new("package_bad_name_encoding", &symbol))?;
            let name = name.trim();
            if name.is_empty() {
                return Err(IngestError::new("package_missing_name", &symbol));
            }
            rows.push(Bar {
                day: iso_day.clone(),
                name: name.to_owned(),
                open_cents: cents(open, &symbol)?,
                high_cents: cents(high, &symbol)?,
                low_cents: cents(low, &symbol)?,
                close_cents: cents(close, &symbol)?,
                prev_close_cents: cents(prev, &symbol)?,
                volume_shares: i64::try_from(volume)
                    .map_err(|_| IngestError::new("package_value_out_of_range", &symbol))?,
                amount_cents: cents(amount, &symbol)?,
                adjust: "none".into(),
                source: "tdx".into(),
                tradable,
                symbol,
            });
        }
        if priced < floor {
            return Err(IngestError::new(
                "package_below_floor",
                format!("{market}: {priced} priced rows < {floor}"),
            ));
        }
        if rows.len() == before {
            return Err(IngestError::new("package_no_stocks", market));
        }
    }
    Ok(rows)
}

fn decode_name(raw: &[u8]) -> Option<std::borrow::Cow<'_, str>> {
    // WHATWG GBK also decodes GB18030 sequences. Python GBK accepts only
    // the ASCII and two-byte forms.
    let mut bytes = raw.iter().copied();
    while let Some(lead) = bytes.next() {
        if lead.is_ascii() {
            continue;
        }
        if !(0x81..=0xfe).contains(&lead)
            || !matches!(bytes.next(), Some(0x40..=0x7e | 0x80..=0xfe))
        {
            return None;
        }
    }
    GBK.decode_without_bom_handling_and_without_replacement(raw)
}

/// Opt-in Python-compatible snapshot cache. Historical and offline lookups
/// never use the network. `read_only_cache` also suppresses cache publication
/// after an otherwise permitted online fetch.
#[derive(Clone, Debug)]
pub struct FetchOptions {
    pub cache_dir: Option<PathBuf>,
    pub cache_ttl: f64,
    pub as_of: Option<DateTime<Utc>>,
    pub offline: bool,
    pub read_only_cache: bool,
}

impl Default for FetchOptions {
    fn default() -> Self {
        Self {
            cache_dir: None,
            cache_ttl: 86_400.0,
            as_of: None,
            offline: false,
            read_only_cache: false,
        }
    }
}

// Field order is the sorted-key Python RequestIdentity serialization used for
// the SHA-256 cache filename. TDX identity values are always ASCII.
#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CacheIdentity {
    adjust: String,
    date: String,
    method: String,
    payload: String,
    source: String,
    symbol: String,
    url: String,
}

#[derive(Serialize, Deserialize)]
struct CacheDocument {
    identity: CacheIdentity,
    source: String,
    fetched_at: String,
    sha256: String,
    response: String,
}

fn timestamp(value: &str) -> Result<DateTime<Utc>, IngestError> {
    DateTime::parse_from_rfc3339(value)
        .map(|value| value.to_utc())
        .map_err(|_| IngestError::new("invalid_timestamp", value))
}

fn read_snapshot(
    path: &Path,
    identity: &CacheIdentity,
    options: &FetchOptions,
) -> Result<Option<Vec<u8>>, IngestError> {
    let corrupt = || IngestError::new("cache_corrupt", path.display());
    let file = match fs::File::open(path) {
        Ok(file) => file,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(corrupt()),
    };
    let limit = (MAX_PACKAGE_BYTES.div_ceil(3) * 4 + 4096) as u64;
    let mut bytes = Vec::new();
    file.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| corrupt())?;
    if bytes.len() as u64 > limit {
        return Err(corrupt());
    }
    let doc: CacheDocument = serde_json::from_slice(&bytes).map_err(|_| corrupt())?;
    let fetched = timestamp(&doc.fetched_at).map_err(|_| corrupt())?;
    let raw = STANDARD.decode(&doc.response).map_err(|_| corrupt())?;
    if doc.identity != *identity
        || doc.source != identity.source
        || raw.is_empty()
        || raw.len() > MAX_PACKAGE_BYTES
        || sha256_hex(&raw) != doc.sha256
    {
        return Err(corrupt());
    }
    let now = Utc::now();
    if fetched > now {
        return Err(IngestError::new("cache_future_timestamp", path.display()));
    }
    let cutoff = options.as_of.unwrap_or(now);
    if fetched > cutoff {
        return Err(IngestError::new("snapshot_after_as_of", doc.fetched_at));
    }
    let age = (cutoff - fetched).as_seconds_f64();
    if age <= options.cache_ttl {
        return Ok(Some(raw));
    }
    if options.offline || options.as_of.is_some() {
        return Err(IngestError::new("cache_expired", path.display()));
    }
    Ok(None)
}

fn write_snapshot(path: &Path, identity: CacheIdentity, raw: &[u8]) -> io::Result<()> {
    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
    let dir = path.parent().unwrap();
    fs::create_dir_all(dir)?;
    let doc = CacheDocument {
        source: identity.source.clone(),
        identity,
        fetched_at: Utc::now().to_rfc3339(),
        sha256: sha256_hex(raw),
        response: STANDARD.encode(raw),
    };
    let (mut file, temporary) = loop {
        let id = NEXT_TEMP.fetch_add(1, Ordering::Relaxed);
        let temporary = dir.join(format!(".tdx-{}-{id}.tmp", std::process::id()));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
        {
            Ok(file) => break (file, temporary),
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err),
        }
    };
    let result = (|| {
        serde_json::to_writer(&mut file, &doc)?;
        file.flush()?;
        file.sync_all()?;
        Ok(())
    })();
    drop(file);
    let result = result.and_then(|()| fs::rename(&temporary, path));
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}

/// Fetch and validate one ISO day, using the same cache identity as Python.
/// Performs no SQLite writes. `package_not_published` specifically means HTTP 404.
pub fn fetch_day(day: &str, options: &FetchOptions) -> Result<Vec<Bar>, IngestError> {
    let date = parse_day(day)?;
    if !options.cache_ttl.is_finite() || options.cache_ttl < 0.0 {
        return Err(IngestError::new(
            "invalid_transport_config",
            "cache TTL must be finite and nonnegative",
        ));
    }
    let identity = tdx_identity(day, date);
    let key = cache_key(&identity);
    let path = options
        .cache_dir
        .as_ref()
        .map(|dir| dir.join(format!("{key}.json")));
    // Serialize same-key refreshes inside the process without retaining one
    // mutex for every historical day ever requested.
    static CACHE_LOCKS: [std::sync::Mutex<()>; 32] = [const { std::sync::Mutex::new(()) }; 32];
    let slot = usize::from_str_radix(&key[..2], 16).unwrap() % CACHE_LOCKS.len();
    let _guard = path.as_ref().map(|_| {
        CACHE_LOCKS[slot]
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    });
    if let Some(path) = &path {
        if let Some(raw) = read_snapshot(path, &identity, options)? {
            return parse_package(&raw, day);
        }
    }
    if options.as_of.is_some() {
        return Err(IngestError::new(
            "historical_snapshot_unavailable",
            identity.url,
        ));
    }
    if options.offline {
        return Err(IngestError::new("offline_cache_miss", identity.url));
    }

    let timeout = if path.is_some() { 30 } else { 90 };
    let raw = request_package(&identity.url, day, Duration::from_secs(timeout))?;
    let rows = parse_package(&raw, day)?;
    if let Some(path) = &path {
        if !options.read_only_cache {
            write_snapshot(path, identity, &raw)
                .map_err(|err| IngestError::new("cache_write_error", err))?;
        }
    }
    Ok(rows)
}

fn tdx_identity(day: &str, date: NaiveDate) -> CacheIdentity {
    CacheIdentity {
        adjust: "none".into(),
        date: day.into(),
        method: "GET".into(),
        payload: "".into(),
        source: "tdx".into(),
        symbol: "*".into(),
        url: format!(
            "https://www.tdx.com.cn/products/data/data/g4day/{}.zip",
            date.format("%Y%m%d")
        ),
    }
}

fn cache_key(identity: &CacheIdentity) -> String {
    sha256_hex(&serde_json::to_vec(identity).expect("TDX identity is serializable"))
}

/// Download a raw daily ZIP without using the shared snapshot cache. Call
/// `parse_package` before publishing it to a caller-owned artifact cache.
pub fn fetch_package(day: &str) -> Result<Vec<u8>, IngestError> {
    let date = parse_day(day)?;
    request_package(&tdx_identity(day, date).url, day, Duration::from_secs(90))
}

/// Fetch and atomically upsert one day into an existing V2 bar database.
pub fn ingest_day(db: &Path, day: &str) -> Result<usize, IngestError> {
    let mut store = open_store(db)?;
    let bars = fetch_day(day, &FetchOptions::default())?;
    store
        .upsert_bars(&bars)
        .map_err(|err| IngestError::new("store_write_failed", err))
}

fn open_store(db: &Path) -> Result<Store, IngestError> {
    let open_error = |err: &dyn fmt::Display| IngestError::new("store_open_failed", err);
    // The service owns database initialization. Like the Python importer,
    // require a bar table before allowing its schema to be upgraded.
    let check =
        rusqlite::Connection::open_with_flags(db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|err| open_error(&err))?;
    check
        .prepare("SELECT 1 FROM bar LIMIT 0")
        .map_err(|err| open_error(&err))?;
    drop(check);
    let store = Store::open(db).map_err(|err| open_error(&err))?;
    store
        .conn
        .busy_timeout(Duration::from_secs(60))
        .map_err(|err| open_error(&err))?;
    Ok(store)
}

fn request_package(url: &str, day: &str, timeout: Duration) -> Result<Vec<u8>, IngestError> {
    let agent = http::agent(timeout, true);
    let mut response = agent.get(url).call().map_err(|err| match err {
        ureq::Error::StatusCode(404) => IngestError::new("package_not_published", day),
        ureq::Error::StatusCode(status) => IngestError::new("package_http_error", status),
        _ => IngestError::new("package_network_error", err),
    })?;
    let raw = response
        .body_mut()
        .with_config()
        .limit(MAX_PACKAGE_BYTES as u64)
        .read_to_vec()
        .map_err(|err| IngestError::new("package_network_error", err))?;
    if raw.is_empty() {
        return Err(IngestError::new("upstream_empty", url));
    }
    Ok(raw)
}

const USAGE: &str = "astock-v2 ingest --db PATH (--day YYYY-MM-DD | --days START:END)\n\
    [--workers N] [--skip-missing] [--dry-run] [--cache-dir PATH]\n\
    [--cache-ttl SECONDS] [--offline] [--as-of TIMESTAMP]";

struct IngestArgs {
    db: PathBuf,
    days: Vec<String>,
    workers: usize,
    skip_missing: bool,
    dry_run: bool,
    fetch: FetchOptions,
}

impl IngestArgs {
    fn parse(args: &[String]) -> Result<Option<Self>, String> {
        let mut db = None;
        let mut day = None;
        let mut days = None;
        let mut workers = 8;
        let mut skip_missing = false;
        let mut dry_run = false;
        let mut fetch = FetchOptions::default();
        let mut args = args.iter().map(String::as_str);
        while let Some(arg) = args.next() {
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg, None), |(k, v)| (k, Some(v)));
            let mut value = || {
                inline
                    .or_else(|| args.next().filter(|s| !s.starts_with("--")))
                    .ok_or_else(|| format!("{flag} requires a value"))
            };
            match flag {
                "-h" | "--help" if inline.is_none() => return Ok(None),
                "--db" => db = Some(PathBuf::from(value()?)),
                "--day" => day = Some(value()?),
                "--days" => days = Some(value()?),
                "--workers" => {
                    workers = value()?
                        .parse::<isize>()
                        .map_err(|_| "invalid --workers".to_owned())?
                        .max(1) as usize
                }
                "--cache-dir" => fetch.cache_dir = Some(PathBuf::from(value()?)),
                "--cache-ttl" => {
                    fetch.cache_ttl = value()?
                        .parse()
                        .map_err(|_| "invalid --cache-ttl".to_owned())?
                }
                "--as-of" => {
                    fetch.as_of = Some(timestamp(value()?).map_err(|err| err.to_string())?)
                }
                "--offline" if inline.is_none() => fetch.offline = true,
                "--skip-missing" if inline.is_none() => skip_missing = true,
                "--dry-run" if inline.is_none() => dry_run = true,
                _ => return Err(format!("unknown argument: {arg}")),
            }
        }
        let db = db.ok_or("--db is required")?;
        let days = if let Some(day) = day {
            vec![parse_day(day).map_err(|err| err.to_string())?.to_string()]
        } else {
            expand_days(days.ok_or("one of --day or --days is required")?)
                .map_err(|err| err.to_string())?
        };
        if !fetch.cache_ttl.is_finite() || fetch.cache_ttl < 0.0 {
            return Err("--cache-ttl must be finite and nonnegative".into());
        }
        fetch.read_only_cache = dry_run;
        Ok(Some(Self {
            db,
            days,
            workers,
            skip_missing,
            dry_run,
            fetch,
        }))
    }
}

/// `args` starts after `ingest`. Exit 2 for usage or a missing write target,
/// 1 for operational failures, and 0 for success (including skipped HTTP 404s).
pub fn run(args: &[String]) -> i32 {
    let args = match IngestArgs::parse(args) {
        Ok(Some(args)) => args,
        Ok(None) => {
            println!("{USAGE}");
            return 0;
        }
        Err(err) => {
            eprintln!("error: {err}\n{USAGE}");
            return 2;
        }
    };
    let mut store = if args.dry_run {
        None
    } else {
        if !args.db.is_file() {
            eprintln!("error: database not found: {}", args.db.display());
            return 2;
        }
        match open_store(&args.db) {
            Ok(store) => Some(store),
            Err(err) => {
                eprintln!("error: {err}");
                return 1;
            }
        }
    };
    let mut total = 0;
    let mut failed = Vec::new();
    let fetch = &args.fetch;
    for chunk in args.days.chunks(args.workers) {
        // A batch bounds buffered days and commits in requested date order,
        // even when downloads finish out of order.
        std::thread::scope(|scope| {
            let jobs = chunk
                .iter()
                .map(|day| {
                    let job = std::thread::Builder::new().spawn_scoped(scope, move || {
                        let start = Instant::now();
                        let result = fetch_day(day, fetch);
                        (result, start.elapsed().as_secs_f64())
                    });
                    (day, job)
                })
                .collect::<Vec<_>>();
            for (day, job) in jobs {
                let result = job
                    .map_err(|err| IngestError::new("worker_start_failed", err))
                    .and_then(|job| {
                        job.join()
                            .map_err(|_| IngestError::new("worker_failed", day))
                    })
                    .and_then(|(result, seconds)| result.map(|rows| (rows, seconds)))
                    .and_then(|(rows, seconds)| {
                        let count = match &mut store {
                            Some(store) => store
                                .upsert_bars(&rows)
                                .map_err(|err| IngestError::new("store_write_failed", err))?,
                            None => rows.len(),
                        };
                        Ok((count, seconds))
                    });
                match result {
                    Ok((count, seconds)) => {
                        total += count;
                        if args.dry_run {
                            println!("{day}  parsed {count} rows in {seconds:.2}s (dry run)");
                        } else {
                            println!("{day}  wrote {count} rows in {seconds:.2}s");
                        }
                    }
                    Err(err) if args.skip_missing && err.code == "package_not_published" => {
                        println!("{day}  skipped ({})", err.code);
                    }
                    Err(err) => {
                        eprintln!("{day}  FAILED  {err}");
                        failed.push((day, err.code));
                    }
                }
            }
        });
    }
    if !failed.is_empty() {
        if args.dry_run {
            eprintln!("\n{} day(s) failed", failed.len());
            return 1;
        }
        eprintln!("\n{} day(s) failed:", failed.len());
        for (day, code) in failed.iter().take(20) {
            eprintln!("  {day}  {code}");
        }
        if failed.len() > 20 {
            eprintln!("  ... and {} more", failed.len() - 20);
        }
    }
    let action = if args.dry_run { "parsed" } else { "written" };
    println!("\ntotal {action}: {total}");
    i32::from(!failed.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

    const DAY: &str = "2022-05-06";
    const KEY: &str = "815770730aed38e151bf944ab6b5fb845379238f096bd48945fd307186ed128d";

    fn fixture(
        day: &str,
        include_bj: bool,
        mutate_sh: impl FnOnce(&mut Vec<u8>, &mut Vec<u8>),
    ) -> Vec<u8> {
        let mut zip = ZipWriter::new(Cursor::new(Vec::new()));
        let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        let suffix = parse_day(day).unwrap().format("%y%m%d");
        let mut mutate_sh = Some(mutate_sh);
        for (market, floor, _) in MARKETS {
            if market == "bj" && !include_bj {
                continue;
            }
            let stock_base = match market {
                "sh" => 600_000,
                "sz" => 300_000,
                _ => 920_000,
            };
            let mut cod = vec![0; (floor + 3) * CODE_SIZE];
            let mut md1 = vec![0; (floor + 3) * BLOCK_SIZE];
            for (seq, (record, block)) in cod
                .chunks_exact_mut(CODE_SIZE)
                .zip(md1.chunks_exact_mut(BLOCK_SIZE))
                .enumerate()
            {
                // Enough priced non-stocks clear the floor before filtering.
                let code = if seq < 4 {
                    stock_base + seq
                } else {
                    190_000 + seq
                };
                record[..6].copy_from_slice(format!("{code:06}").as_bytes());
                record[32..34].copy_from_slice(&(seq as u16).to_le_bytes());
                record[40..45].copy_from_slice(b"\xd6\xd0\xb9\xfa ");
                let (prev, close, shape, volume): (f64, f64, f64, u64) = match seq {
                    0 => (10.10, 10.125, 10.125, 1_234),
                    1 => (10.25, 0.0, 0.0, 1),
                    2 => (10.20, 10.25, 0.0, 0),
                    3 => (0.0, 0.0, 0.0, 0),
                    _ => (10.0, 10.0, 10.0, 1),
                };
                for (offset, value) in [
                    (4, prev),
                    (12, shape),
                    (20, shape),
                    (28, shape),
                    (36, close),
                    (72, 12345.67),
                ] {
                    block[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
                }
                block[56..64].copy_from_slice(&volume.to_le_bytes());
            }
            if market == "sh" {
                mutate_sh.take().unwrap()(&mut cod, &mut md1);
            }
            zip.start_file(format!("{market}{suffix}.cod"), options)
                .unwrap();
            zip.write_all(&cod).unwrap();
            zip.start_file(format!("{market}{suffix}.md1"), options)
                .unwrap();
            zip.write_all(&md1).unwrap();
        }
        zip.finish().unwrap().into_inner()
    }

    #[test]
    fn keeps_only_ordinary_trades_and_valuation_rows() {
        let raw = fixture(DAY, true, |_, _| {});
        let rows = parse_package(&raw, "20220506").unwrap();
        assert_eq!(
            rows.iter().map(|r| r.symbol.as_str()).collect::<Vec<_>>(),
            [
                "600000.SH",
                "600001.SH",
                "600002.SH",
                "300000.SZ",
                "300001.SZ",
                "300002.SZ",
                "920000.BJ",
                "920001.BJ",
                "920002.BJ",
            ]
        );
        let trade = &rows[0];
        assert_eq!(
            (&*trade.day, &*trade.name, &*trade.source, &*trade.adjust),
            (DAY, "中国", "tdx", "none")
        );
        assert_eq!(
            (
                trade.open_cents,
                trade.high_cents,
                trade.low_cents,
                trade.close_cents
            ),
            (1012, 1012, 1012, 1012)
        );
        assert_eq!(
            (
                trade.prev_close_cents,
                trade.volume_shares,
                trade.amount_cents,
                trade.tradable
            ),
            (1010, 1234, 1_234_567, true)
        );
        for (halt, reference) in [(&rows[1], 1025), (&rows[2], 1020)] {
            assert_eq!(
                (halt.close_cents, halt.prev_close_cents, halt.tradable),
                (reference, reference, false)
            );
            assert_eq!(
                (
                    halt.open_cents,
                    halt.high_cents,
                    halt.low_cents,
                    halt.volume_shares,
                    halt.amount_cents
                ),
                (0, 0, 0, 0, 0)
            );
        }
        for (market, ordinary, excluded) in [
            (
                "sh",
                &["600", "601", "603", "605", "688", "689"][..],
                &["019", "510", "880", "000"][..],
            ),
            (
                "sz",
                &["000", "001", "002", "003", "300", "301"],
                &["159", "399"],
            ),
            ("bj", &["43", "83", "87", "88", "92"], &["90", "81"]),
        ] {
            for prefix in ordinary {
                assert!(is_ordinary_a_share(market, &format!("{prefix:0<6}")));
            }
            for prefix in excluded {
                assert!(!is_ordinary_a_share(market, &format!("{prefix:0<6}")));
            }
        }
        assert!(!is_ordinary_a_share("hk", "600000"));
    }

    #[test]
    fn historical_packages_and_day_ranges() {
        let prior = "2022-05-05";
        assert_eq!(
            parse_package(&fixture(prior, false, |_, _| {}), prior)
                .unwrap()
                .len(),
            6
        );
        assert_eq!(
            parse_package(&fixture(DAY, false, |_, _| {}), DAY)
                .unwrap_err()
                .code,
            "package_missing_file"
        );
        assert_eq!(
            expand_days("2022-05-06:2022-05-09").unwrap(),
            ["2022-05-06", "2022-05-07", "2022-05-08", "2022-05-09"]
        );
        assert_eq!(
            expand_days("2022-05-09:2022-05-06").unwrap_err().code,
            "bad_day_range"
        );
        assert_eq!(expand_days("2022-02-29").unwrap_err().code, "invalid_day");
    }

    #[test]
    fn rejects_corrupt_or_incomplete_markets() {
        type Mutate = fn(&mut Vec<u8>, &mut Vec<u8>);
        let cases: [(&str, Mutate); 11] = [
            ("package_bad_block_size", |cod, _| cod.push(0)),
            ("package_length_mismatch", |cod, _| {
                cod.truncate(cod.len() - CODE_SIZE)
            }),
            ("package_duplicate", |cod, _| {
                cod[CODE_SIZE..CODE_SIZE + 6].copy_from_slice(b"600000")
            }),
            ("package_duplicate", |cod, _| {
                cod[CODE_SIZE + 32..CODE_SIZE + 34].fill(0)
            }),
            ("package_no_stocks", |cod, _| {
                for (index, record) in cod[..4 * CODE_SIZE].chunks_exact_mut(CODE_SIZE).enumerate()
                {
                    record[..6].copy_from_slice(format!("19000{index}").as_bytes());
                }
            }),
            ("package_block_out_of_range", |cod, _| {
                cod[32..34].copy_from_slice(&u16::MAX.to_le_bytes())
            }),
            ("package_bad_code", |cod, _| cod[0] = b'A'),
            ("package_bad_name_encoding", |cod, _| {
                cod[40..42].copy_from_slice(b"\xff\0")
            }),
            ("package_missing_name", |cod, _| cod[40] = 0),
            ("package_non_finite", |_, md1| {
                md1[4 * BLOCK_SIZE + 72..4 * BLOCK_SIZE + 80]
                    .copy_from_slice(&f64::NAN.to_le_bytes())
            }),
            ("package_below_floor", |_, md1| {
                md1[36..44].fill(0);
                md1[2 * BLOCK_SIZE + 36..2 * BLOCK_SIZE + 44].fill(0);
            }),
        ];
        for (expected, mutate) in cases {
            assert_eq!(
                parse_package(&fixture(DAY, true, mutate), DAY)
                    .unwrap_err()
                    .code,
                expected
            );
        }
        assert_eq!(
            parse_package(b"<html>outage</html>", DAY).unwrap_err().code,
            "package_not_zip"
        );
    }

    struct TestDir(PathBuf);
    impl TestDir {
        fn new() -> Self {
            static ID: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "astock-ingest-{}-{}-{}",
                std::process::id(),
                Utc::now().timestamp_nanos_opt().unwrap(),
                ID.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn offline_fixture(cache: &Path, day: &str, fetched_at: &str) -> PathBuf {
        let identity = tdx_identity(day, parse_day(day).unwrap());
        let path = cache.join(format!("{}.json", cache_key(&identity)));
        let raw = fixture(day, true, |_, _| {});
        fs::create_dir_all(cache).unwrap();
        // Same field names, timestamp format and base64 as SourceHttp.
        let bytes = serde_json::to_vec(&serde_json::json!({
            "identity": identity, "source": "tdx", "fetched_at": fetched_at,
            "sha256": sha256_hex(&raw), "response": STANDARD.encode(&raw),
        }))
        .unwrap();
        fs::write(&path, bytes).unwrap();
        path
    }

    #[test]
    fn shared_snapshot_respects_integrity_as_of_and_ttl() {
        let cache = TestDir::new();
        let path = offline_fixture(&cache.0, DAY, "2022-05-06T09:00:00+00:00");
        assert_eq!(path.file_stem().unwrap(), KEY);
        let mut options = FetchOptions {
            cache_dir: Some(cache.0.clone()),
            ..Default::default()
        };
        for (cutoff, expected) in [
            ("2022-05-06T17:00:00+08:00", None),
            ("2022-05-06T08:59:59Z", Some("snapshot_after_as_of")),
            ("2022-05-07T09:00:00Z", None),
            ("2022-05-07T09:00:01Z", Some("cache_expired")),
        ] {
            options.as_of = Some(timestamp(cutoff).unwrap());
            let result = fetch_day(DAY, &options);
            if let Some(code) = expected {
                assert_eq!(result.unwrap_err().code, code);
            } else {
                assert_eq!(result.unwrap().len(), 9);
            }
        }
        assert_eq!(
            fetch_day("2022-05-07", &options).unwrap_err().code,
            "historical_snapshot_unavailable"
        );
        options.as_of = None;
        options.offline = true;
        assert_eq!(
            fetch_day("2022-05-07", &options).unwrap_err().code,
            "offline_cache_miss"
        );
        assert_eq!(fetch_day(DAY, &options).unwrap_err().code, "cache_expired");
        offline_fixture(&cache.0, DAY, "9999-12-31T00:00:00Z");
        assert_eq!(
            fetch_day(DAY, &options).unwrap_err().code,
            "cache_future_timestamp"
        );
        let mut doc: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        doc["sha256"] = serde_json::json!("wrong digest");
        fs::write(&path, serde_json::to_vec(&doc).unwrap()).unwrap();
        assert_eq!(fetch_day(DAY, &options).unwrap_err().code, "cache_corrupt");
    }

    #[test]
    fn cli_dry_run_is_read_only_and_offline_range_writes_idempotently() {
        let temp = TestDir::new();
        let db = temp.0.join("bars.sqlite3");
        let cache = temp.0.join("cache");
        let path = offline_fixture(&cache, DAY, "2022-05-06T09:00:00+00:00");
        let original = fs::read(&path).unwrap();
        let mut args = vec![
            "--db".into(),
            db.to_string_lossy().into_owned(),
            "--days".into(),
            "2022-05-06:2022-05-07".into(),
            "--cache-dir".into(),
            cache.to_string_lossy().into_owned(),
            "--as-of".into(),
            "2022-05-06T09:00:00Z".into(),
            "--workers=2".into(),
            "--skip-missing".into(),
        ];
        assert_eq!(run(&args), 2);
        args.push("--dry-run".into());
        // A historical cache miss remains an error with --skip-missing.
        assert_eq!(run(&args), 1);
        assert!(!db.exists());
        args[3] = DAY.into();
        assert_eq!(run(&args), 0);
        assert!(!db.exists());
        assert_eq!(fs::read(&path).unwrap(), original);
        let store = Store::open(&db).unwrap();
        assert_eq!(run(&args), 0);
        assert!(store.bars_for_day(DAY).unwrap().is_empty());
        args.pop();
        args[3] = "2022-05-06:2022-05-07".into();
        assert_eq!(run(&args), 1);
        args[3] = DAY.into();
        assert_eq!(run(&args), 0);
        let bars = store.bars_for_day(DAY).unwrap();
        assert_eq!(bars.len(), 9);
        assert_eq!(bars.iter().filter(|bar| bar.tradable).count(), 3);
        assert_eq!(run(&args), 0);
        assert_eq!(store.bars_for_day(DAY).unwrap(), bars);
    }
}
