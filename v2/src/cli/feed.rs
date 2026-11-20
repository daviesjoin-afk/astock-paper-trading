//! Fetch and normalize Tencent snapshots for the live-round CLI.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone, Utc};
use encoding_rs::GB18030;
use rusqlite::{Connection, OpenFlags};
use serde_json::{Value, json};

use super::http;

const SOURCE: &str = "tencent_quote";
const ENDPOINT: &str = "https://qt.gtimg.cn/q=";
const DEFAULT_BATCH: usize = 800;
const HISTORY_DAYS: usize = 20;
const MIN_COMPACT_HISTORY: usize = 5;
const SHANGHAI: FixedOffset = FixedOffset::east_opt(8 * 3600).unwrap();
const USAGE: &str = "Usage: astock-v2 live-feed [--universe PATH] [--batch N] [--out PATH] [--history-db PATH]\n\
    Default batch: 800. History defaults to ASTOCK_HISTORY_DB when set.";

type History = HashMap<String, Vec<i64>>;

pub fn run(args: &[String]) -> i32 {
    if args
        .iter()
        .any(|arg| matches!(arg.as_str(), "-h" | "--help"))
    {
        println!("{USAGE}");
        return 0;
    }
    let options = match Options::parse(args) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("live-feed: {error}\n{USAGE}");
            return 2;
        }
    };
    let result = build_snapshot(&options.universe, options.history.as_deref(), options.batch)
        .and_then(|snapshot| write_snapshot(options.out.as_deref(), &snapshot));
    match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("live-feed: {error}");
            1
        }
    }
}

struct Options {
    universe: PathBuf,
    history: Option<PathBuf>,
    out: Option<PathBuf>,
    batch: usize,
}

impl Options {
    fn parse(args: &[String]) -> Result<Self, String> {
        let mut options = Self {
            // Deployments export ASTOCK_UNIVERSE (see /etc/astock-live.env); the
            // repository-relative path is only the developer default. Without
            // this, running the command from an install directory failed with
            // "cannot read universe work/live-a-universe.txt".
            universe: std::env::var_os("ASTOCK_UNIVERSE")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("work/live-a-universe.txt")),
            history: std::env::var_os("ASTOCK_HISTORY_DB")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from),
            out: None,
            batch: DEFAULT_BATCH,
        };
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            let (flag, inline) = arg.split_once('=').unwrap_or((arg, ""));
            if !matches!(flag, "--universe" | "--batch" | "--out" | "--history-db") {
                return Err(format!("unknown argument: {flag}"));
            }
            let value = if arg.contains('=') {
                inline
            } else {
                args.next()
                    .filter(|value| !value.starts_with("--"))
                    .ok_or_else(|| format!("missing value for {flag}"))?
            };
            match flag {
                "--universe" => options.universe = value.into(),
                "--batch" => {
                    options.batch = value
                        .parse()
                        .map_err(|_| format!("invalid batch size: {value}"))?;
                }
                "--out" => options.out = Some(value.into()),
                "--history-db" => options.history = Some(value.into()),
                _ => unreachable!(),
            }
        }
        Ok(options)
    }
}

fn write_snapshot(path: Option<&Path>, snapshot: &Value) -> Result<(), String> {
    let mut data = serde_json::to_vec(snapshot).map_err(|error| error.to_string())?;
    data.push(b'\n');
    if let Some(path) = path {
        let result = (|| {
            if let Some(parent) = path
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty())
            {
                fs::create_dir_all(parent)?;
            }
            fs::write(path, data)
        })();
        return result.map_err(|error| format!("cannot write {}: {error}", path.display()));
    }
    let mut stdout = io::stdout().lock();
    stdout
        .write_all(&data)
        .and_then(|()| stdout.flush())
        .map_err(|error| format!("cannot write stdout: {error}"))
}

pub(crate) fn build_snapshot(
    universe_path: &Path,
    history_path: Option<&Path>,
    batch_size: usize,
) -> Result<Value, String> {
    let agent = http::agent(Duration::from_secs(10), true);
    snapshot_with(universe_path, history_path, batch_size, Utc::now, |codes| {
        let mut response = agent
            .get(format!("{ENDPOINT}{}", codes.join(",")))
            .call()
            .map_err(|error| error.to_string())?;
        response
            .body_mut()
            .read_to_vec()
            .map_err(|error| error.to_string())
    })
}

fn snapshot_with(
    universe_path: &Path,
    history_path: Option<&Path>,
    batch_size: usize,
    now: impl FnOnce() -> DateTime<Utc>,
    mut fetch: impl FnMut(&[String]) -> Result<Vec<u8>, String>,
) -> Result<Value, String> {
    let symbols = load_universe(universe_path)?;
    if batch_size == 0 {
        return Err("batch size must be positive".into());
    }
    let codes: Vec<String> = symbols.iter().map(|symbol| tencent_code(symbol)).collect();
    let mut rows = HashMap::new();
    for (index, batch) in codes.chunks(batch_size.min(DEFAULT_BATCH)).enumerate() {
        let result = fetch(batch).and_then(|bytes| parse_rows(&bytes, batch, &mut rows));
        result.map_err(|error| format!("{SOURCE} batch {} failed: {error}", index + 1))?;
    }
    let fetched_at = now();
    let observed = codes.iter().filter_map(|code| rows.get(code)?.time).min();
    let history = match (history_path, observed) {
        (Some(path), Some(time)) => load_history(path, time.with_timezone(&SHANGHAI).date_naive()),
        _ => History::new(),
    };
    let quotes: Vec<_> = symbols
        .iter()
        .zip(&codes)
        .filter_map(|(symbol, code)| {
            let closes = history.get(symbol).map(Vec::as_slice).unwrap_or_default();
            rows.get(code)?.quote(symbol, closes)
        })
        .collect();
    let mut result = json!({
        "count": quotes.len(),
        "quotes": quotes,
        "source": SOURCE,
        "quote_time": observed.unwrap_or_else(|| fetched_at.fixed_offset()).to_rfc3339(),
        "fetched_at": fetched_at.to_rfc3339(),
    });
    if observed.is_none() {
        result["quote_time_estimated"] = json!(true);
    }
    Ok(result)
}

fn load_universe(path: &Path) -> Result<Vec<String>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("cannot read universe {}: {error}", path.display()))?;
    let mut symbols = Vec::new();
    let mut seen = HashSet::new();
    for (index, line) in text.trim_start_matches('\u{feff}').lines().enumerate() {
        let symbol = line.trim().to_ascii_uppercase();
        if symbol.is_empty() || symbol.starts_with('#') {
            continue;
        }
        let valid = match symbol.split_once('.') {
            Some((code, suffix)) => {
                code.len() == 6
                    && code.bytes().all(|byte| byte.is_ascii_digit())
                    && matches!(suffix, "SH" | "SZ" | "BJ")
            }
            None => false,
        };
        if !valid {
            return Err(format!(
                "{}:{}: invalid universe symbol: {symbol:?}",
                path.display(),
                index + 1
            ));
        }
        if seen.insert(symbol.clone()) {
            symbols.push(symbol);
        }
    }
    if symbols.is_empty() {
        return Err(format!("universe is empty: {}", path.display()));
    }
    Ok(symbols)
}

fn tencent_code(symbol: &str) -> String {
    format!("{}{}", symbol[7..].to_ascii_lowercase(), &symbol[..6])
}

struct Row {
    time: Option<DateTime<FixedOffset>>,
    fields: Vec<String>,
}

fn parse_rows(
    bytes: &[u8],
    codes: &[String],
    rows: &mut HashMap<String, Row>,
) -> Result<(), String> {
    let text = GB18030
        .decode_without_bom_handling_and_without_replacement(bytes)
        .ok_or("invalid GB18030 quote response")?;
    let requested: HashSet<&str> = codes.iter().map(String::as_str).collect();
    for record in text.split(';') {
        let Some((key, payload)) = record.trim().split_once("=\"") else {
            continue;
        };
        let Some(code) = key.strip_prefix("v_") else {
            continue;
        };
        if !requested.contains(code) {
            continue;
        }
        let Some(payload) = payload.strip_suffix('"') else {
            continue;
        };
        let fields: Vec<String> = payload.split('~').map(str::to_owned).collect();
        if fields.len() < 5 || fields[2] != code[2..] {
            continue;
        }
        let time = fields.get(30).and_then(|text| source_time(text));
        rows.insert(code.into(), Row { time, fields });
    }
    Ok(())
}

fn source_time(text: &str) -> Option<DateTime<FixedOffset>> {
    let text = text.trim();
    if let Ok(time) = DateTime::parse_from_rfc3339(text) {
        return Some(time);
    }
    [
        "%Y%m%d%H%M%S",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S%.f",
    ]
    .iter()
    .find_map(|format| NaiveDateTime::parse_from_str(text, format).ok())
    .and_then(|time| SHANGHAI.from_local_datetime(&time).single())
}

impl Row {
    fn field(&self, index: usize) -> &str {
        self.fields
            .get(index)
            .map(|field| field.trim())
            .unwrap_or("")
    }

    fn number(&self, index: usize) -> Option<f64> {
        self.field(index)
            .parse::<f64>()
            .ok()
            .filter(|number| number.is_finite())
    }

    fn quote(&self, symbol: &str, closes: &[i64]) -> Option<Value> {
        let price = self.number(3)?;
        let prev_close = self.number(4)?;
        let amount = self.number(37);
        let stale = amount == Some(0.0) && price == prev_close;
        let mut quote = json!({
            "symbol": symbol,
            "price_cents": positive_units(price, 100.0)?,
            "prev_close_cents": positive_units(prev_close, 100.0)?,
            "tradable": !stale,
            "trailing_closes": closes,
        });
        let name = self.field(1);
        if !matches!(name, "" | "-" | "None") {
            quote["name"] = json!(name);
        }
        for (index, key) in [
            (5, "open_cents"),
            (33, "high_cents"),
            (34, "low_cents"),
            (47, "limit_up_cents"),
            (48, "limit_down_cents"),
        ] {
            if let Some(cents) = self
                .number(index)
                .and_then(|number| positive_units(number, 100.0))
            {
                quote[key] = json!(cents);
            }
        }
        if let Some(cents) = amount.and_then(|number| positive_units(number, 1_000_000.0)) {
            quote["amount_cents"] = json!(cents);
        }
        // Tencent reports hands, while the kernel uses whole shares.
        let volume = self
            .field(6)
            .parse::<i64>()
            .ok()
            .and_then(|hands| hands.checked_mul(100));
        if let Some(shares) = volume.filter(|shares| *shares > 0) {
            quote["volume"] = json!(shares);
        }
        Some(quote)
    }
}

fn positive_units(number: f64, scale: f64) -> Option<i64> {
    let units = (number * scale).round_ties_even();
    (units > 0.0 && units < i64::MAX as f64).then_some(units as i64)
}

fn load_history(path: &Path, cutoff: NaiveDate) -> History {
    if path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("json"))
    {
        compact_history(path, cutoff).unwrap_or_default()
    } else {
        sqlite_history(path, cutoff).unwrap_or_default()
    }
}

fn compact_history(path: &Path, cutoff: NaiveDate) -> Option<History> {
    let data = fs::read(path).ok()?;
    let payload: Value = serde_json::from_slice(&data).ok()?;
    let through = NaiveDate::parse_from_str(payload["through_day"].as_str()?, "%Y-%m-%d").ok()?;
    if through >= cutoff {
        return None;
    }
    let series = payload["per_symbol"].as_object()?;
    let window = series
        .values()
        .filter_map(Value::as_array)
        .map(Vec::len)
        .max()?
        .min(HISTORY_DAYS);
    if window < MIN_COMPACT_HISTORY {
        return None;
    }
    Some(
        series
            .iter()
            .filter_map(|(symbol, values)| {
                let values = values.as_array()?;
                let tail = values.get(values.len().checked_sub(window)?..)?;
                let closes = tail
                    .iter()
                    .map(|value| value.as_i64().filter(|value| *value > 0))
                    .collect::<Option<Vec<_>>>()?;
                Some((symbol.clone(), closes))
            })
            .collect(),
    )
}

fn sqlite_history(path: &Path, cutoff: NaiveDate) -> rusqlite::Result<History> {
    let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    connection.busy_timeout(Duration::from_secs(5))?;
    let mut statement = connection.prepare(
        "WITH window AS (
            SELECT DISTINCT day FROM bar WHERE day < ?1 AND date(day, '+0 days') = day
            ORDER BY day DESC LIMIT ?2
        )
        SELECT bar.symbol, bar.close_cents, (SELECT count(*) FROM window)
        FROM window JOIN bar ON bar.day = window.day
        WHERE typeof(bar.close_cents) = 'integer' AND bar.close_cents > 0
        GROUP BY bar.symbol, bar.day HAVING count(*) = 1
        ORDER BY bar.symbol, bar.day",
    )?;
    let mut rows = statement.query(rusqlite::params![cutoff.to_string(), HISTORY_DAYS])?;
    let mut history = History::new();
    let mut window = 0;
    while let Some(row) = rows.next()? {
        let symbol: String = row.get(0)?;
        history.entry(symbol).or_default().push(row.get(1)?);
        window = row.get(2)?;
    }
    history.retain(|_, closes| closes.len() == window);
    Ok(history)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Fixtures(PathBuf);

    impl Fixtures {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "astock-feed-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }

        fn write(&self, filename: &str, data: impl AsRef<[u8]>) -> PathBuf {
            let path = self.0.join(filename);
            fs::write(&path, data).unwrap();
            path
        }
    }

    impl Drop for Fixtures {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn now() -> DateTime<Utc> {
        "2026-11-20T01:15:00Z".parse().unwrap()
    }

    fn record(code: &str, extra: &[(usize, &str)]) -> Vec<u8> {
        let mut fields = [""; 53];
        fields[2] = &code[2..];
        fields[3] = "10.78";
        fields[4] = "10.40";
        for &(index, value) in extra {
            fields[index] = value;
        }
        format!("v_{code}=\"{}\";\n", fields.join("~")).into_bytes()
    }

    #[test]
    fn batches_gb18030_quotes_with_source_time_and_no_fabricated_fields() {
        let fixtures = Fixtures::new();
        let universe = fixtures.write(
            "universe.txt",
            "\u{feff}# ordered\r\n000001.sz\r\n000001.SH\r\n000001.SZ\r\n920001.bj\r\n",
        );
        let mut active = record(
            "sz000001",
            &[
                (5, "0"),
                (6, "1234"),
                (30, "20261116100020"),
                (33, "10.85"),
                (34, "10.35"),
                (37, "123.456"),
                (47, "11.44"),
                (48, "9.36"),
                (49, "1.2"),
            ],
        );
        let name_pos = active.iter().position(|byte| *byte == b'~').unwrap() + 1;
        active.splice(
            name_pos..name_pos,
            [
                0xc6, 0xd6, 0xb7, 0xa2, 0xd2, 0xf8, 0xd0, 0xd0, 0x95, 0x32, 0x82, 0x36,
            ],
        );
        let mut stale = record(
            "sh000001",
            &[
                (1, "None"),
                (3, "10.40"),
                (30, "2026-11-16T01:59:00+00:00"),
                (37, "0"),
            ],
        );
        stale.extend_from_slice(&record("bj920001", &[(4, "NaN")]));
        let mut calls = Vec::new();
        let result = snapshot_with(&universe, None, 2, now, |codes| {
            calls.push(codes.to_vec());
            if calls.len() == 1 {
                Ok([stale.clone(), active.clone()].concat())
            } else {
                Ok(record("bj920001", &[(4, "NaN")]))
            }
        })
        .unwrap();
        assert_eq!(calls, [vec!["sz000001", "sh000001"], vec!["bj920001"]]);
        assert_eq!(result["quote_time"], "2026-11-16T01:59:00+00:00");
        assert!(result.get("quote_time_estimated").is_none());
        assert_eq!(result["fetched_at"], "2026-11-20T01:15:00+00:00");
        assert_eq!(result["source"], "tencent_quote");
        assert_eq!(result["count"], 2);
        assert_eq!(
            result["quotes"],
            json!([
                {"symbol":"000001.SZ", "name":"浦发银行𠀀", "price_cents":1078,
                 "prev_close_cents":1040, "high_cents":1085, "low_cents":1035,
                 "limit_up_cents":1144, "limit_down_cents":936, "amount_cents":123456000,
                 "volume":123400, "tradable":true, "trailing_closes":[]},
                {"symbol":"000001.SH", "price_cents":1040, "prev_close_cents":1040,
                 "tradable":false, "trailing_closes":[]},
            ])
        );
        for quote in result["quotes"].as_array().unwrap() {
            serde_json::from_value::<crate::intraday::Quote>(quote.clone()).unwrap();
        }
    }

    #[test]
    fn history_uses_the_same_latest_market_days_for_every_symbol() {
        let fixtures = Fixtures::new();
        let universe = fixtures.write("universe.txt", "600000.SH\n000001.SZ\n");
        let database = fixtures.0.join("history.sqlite3");
        {
            let db = Connection::open(&database).unwrap();
            db.execute_batch("CREATE TABLE bar(symbol TEXT, day TEXT, close_cents INTEGER);")
                .unwrap();
            for day in (1..=23).rev() {
                for symbol in ["600000.SH", "000001.SZ", "999999.SH"] {
                    // Only the out-of-universe symbol has a bar on the oldest window day.
                    if day == 2 && symbol != "999999.SH" || day == 3 && symbol == "000001.SZ" {
                        continue;
                    }
                    db.execute(
                        "INSERT INTO bar VALUES (?1, ?2, ?3)",
                        (symbol, format!("2026-11-{day:02}"), 1000 + day),
                    )
                    .unwrap();
                }
            }
        }
        let run = |source_time| {
            snapshot_with(&universe, Some(&database), 2, now, |_| {
                Ok([
                    record("sh600000", &[(30, source_time)]),
                    record("sz000001", &[]),
                ]
                .concat())
            })
            .unwrap()
        };
        let first = run("20261122093000");
        assert_eq!(first["quotes"][0]["trailing_closes"], json!([]));
        let second = run("20261123093000");
        assert_eq!(
            second["quotes"][0]["trailing_closes"],
            json!((1003..=1022).collect::<Vec<_>>())
        );
        assert_eq!(second["quotes"][1]["trailing_closes"], json!([]));
        let missing = run("09:30:00");
        assert_eq!(missing["quote_time"], missing["fetched_at"]);
        assert_eq!(missing["quote_time_estimated"], true);
        assert_eq!(missing["quotes"][0]["trailing_closes"], json!([]));
    }

    #[test]
    fn compact_history_requires_a_prior_day_and_a_complete_window() {
        let fixtures = Fixtures::new();
        let universe = fixtures.write("universe.txt", "600000.SH\n000001.SZ\n");
        let run =
            |through_day| {
                let history = fixtures.write("history.json", json!({
                "through_day": through_day,
                "per_symbol": {"600000.SH": [1, 2, 3, 4, 5, 6], "000001.SZ": [2, 3, 4, 5, 6]}
            }).to_string());
                snapshot_with(&universe, Some(&history), 2, now, |_| {
                    Ok([
                        record("sh600000", &[(30, "2026-11-15T17:00:00+00:00")]),
                        record("sz000001", &[]),
                    ]
                    .concat())
                })
                .unwrap()
            };
        assert_eq!(
            run("2026-11-15")["quotes"][0]["trailing_closes"],
            json!([1, 2, 3, 4, 5, 6])
        );
        assert_eq!(run("2026-11-15")["quotes"][1]["trailing_closes"], json!([]));
        for invalid in ["2026-11-16", "2026-11-17", ""] {
            assert_eq!(run(invalid)["quotes"][0]["trailing_closes"], json!([]));
        }
    }

    #[test]
    fn a_failed_batch_fails_the_snapshot() {
        let fixtures = Fixtures::new();
        let universe = fixtures.write("universe.txt", "600000.SH\n000001.SZ\n");
        let error = snapshot_with(&universe, None, 1, now, |codes| match codes[0].as_str() {
            "sh600000" => Ok(record("sh600000", &[])),
            _ => Ok(vec![0x81]),
        })
        .unwrap_err();
        assert_eq!(
            error,
            "tencent_quote batch 2 failed: invalid GB18030 quote response"
        );
    }
}
