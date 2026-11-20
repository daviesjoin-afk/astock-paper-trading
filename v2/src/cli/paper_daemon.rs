//! Schedules after-close ingestion and asks the paper kernel to settle held days.

use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

use chrono::{Datelike, Local, NaiveDate};
use serde_json::{Value, json};

use super::{http, ingest};
use crate::store::Store;

const DEFAULT_API: &str = "http://127.0.0.1:8765";
const DEFAULT_INTERVAL: Duration = Duration::from_secs(900);
const LOOKBACK_WEEKDAYS: usize = 12;
const ACCOUNT_TIMEOUT: Duration = Duration::from_secs(30);
const SETTLE_TIMEOUT: Duration = Duration::from_secs(900);
const HELP: &str = "Usage: astock-v2 paper-daemon --db PATH [OPTIONS]

  --db PATH           Existing V2 market database (required)
  --api URL           Kernel base URL (default: $ASTOCK_API, else :8765)
  --interval SECONDS  Positive seconds between cycles (default: 900)
  --once              Run one cycle and exit
  --no-settle         Ingest without advancing the account";

struct Config {
    db: PathBuf,
    api: String,
    interval: Duration,
    once: bool,
    no_settle: bool,
}

impl Config {
    fn parse(args: &[String]) -> Result<Option<Self>, String> {
        let mut db = None;
        let mut api = std::env::var_os("ASTOCK_API")
            .filter(|value| !value.is_empty())
            .map(|value| value.to_string_lossy().into_owned())
            .unwrap_or_else(|| DEFAULT_API.to_owned());
        let mut interval = DEFAULT_INTERVAL;
        let mut once = false;
        let mut no_settle = false;
        let mut args = args.iter().map(String::as_str).peekable();
        while let Some(arg) = args.next() {
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg, None), |(flag, value)| (flag, Some(value)));
            match (flag, inline) {
                ("-h" | "--help", None) => return Ok(None),
                ("--once", None) => once = true,
                ("--no-settle", None) => no_settle = true,
                ("--db" | "--api" | "--interval", _) => {
                    let value = inline
                        .or_else(|| args.next_if(|next| !next.starts_with("--")))
                        .filter(|value| !value.is_empty())
                        .ok_or_else(|| format!("{flag} requires a value"))?;
                    match flag {
                        "--db" => db = Some(PathBuf::from(value)),
                        "--api" => api = value.trim_end_matches('/').to_owned(),
                        "--interval" => {
                            interval = value
                                .parse::<f64>()
                                .ok()
                                .and_then(|seconds| Duration::try_from_secs_f64(seconds).ok())
                                .filter(|duration| !duration.is_zero())
                                .ok_or("--interval must be a positive, finite duration")?;
                        }
                        _ => unreachable!(),
                    }
                }
                _ => return Err(format!("unknown argument: {arg}")),
            }
        }
        Ok(Some(Self {
            db: db.ok_or("--db is required")?,
            api,
            interval,
            once,
            no_settle,
        }))
    }
}

pub fn run(args: &[String]) -> i32 {
    let config = match Config::parse(args) {
        Ok(Some(config)) => config,
        Ok(None) => {
            println!("{HELP}");
            return 0;
        }
        Err(error) => {
            eprintln!("error: {error}\n{HELP}");
            return 2;
        }
    };
    if !config.once {
        log(&format!(
            "daemon start db={} api={} interval={:.0}s",
            config.db.display(),
            config.api,
            config.interval.as_secs_f64()
        ));
    }
    loop {
        let result = one_cycle(
            &config,
            Local::now().date_naive(),
            |day| ingest::ingest_day(&config.db, day),
            |path, payload, timeout| {
                request_json(&format!("{}{path}", config.api), payload, timeout)
            },
            log,
        );
        if let Err(error) = &result {
            log(&format!("cycle failed: {error}"));
        }
        if config.once {
            return i32::from(result.is_err());
        }
        thread::sleep(config.interval);
    }
}

fn log(message: &str) {
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "{} {message}", Local::now().format("%H:%M:%S"));
    let _ = stdout.flush();
}

fn iso_day(day: &str) -> Result<NaiveDate, String> {
    NaiveDate::parse_from_str(day, "%Y-%m-%d")
        .ok()
        .filter(|date| day.len() == 10 && date.year() > 0 && date.to_string() == day)
        .ok_or_else(|| format!("invalid day: {day}"))
}

fn held_days(db: &Path) -> Result<Vec<NaiveDate>, String> {
    let days = Store::open_read_only(db)
        .and_then(|store| store.bar_days())
        .map_err(|error| format!("read bar days from {}: {error}", db.display()))?;
    days.iter().map(|day| iso_day(day)).collect()
}

fn candidate_days(
    last_held: Option<NaiveDate>,
    today: NaiveDate,
) -> impl Iterator<Item = NaiveDate> {
    last_held
        .and_then(|day| day.succ_opt())
        .into_iter()
        .flat_map(|day| day.iter_days())
        .take_while(move |day| *day <= today)
        .filter(|day| day.weekday().number_from_monday() <= 5)
        .take(LOOKBACK_WEEKDAYS)
}

#[derive(Debug)]
enum RequestError {
    Status(u16, String),
    Transport(String),
    InvalidResponse(String),
}

impl std::fmt::Display for RequestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Status(code, detail) => write!(f, "http={code} {detail}"),
            Self::Transport(detail) => write!(f, "http=0 {detail}"),
            Self::InvalidResponse(detail) => write!(f, "{detail}"),
        }
    }
}

fn request_json(
    url: &str,
    payload: Option<&Value>,
    timeout: Duration,
) -> Result<Value, RequestError> {
    let agent = http::agent(timeout, false);
    let mut response = match payload {
        Some(payload) => agent
            .post(url)
            .config()
            .http_status_as_error(false)
            .build()
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .send(payload.to_string().as_bytes()),
        None => agent
            .get(url)
            .config()
            .http_status_as_error(false)
            .build()
            .header("Accept", "application/json")
            .call(),
    }
    .map_err(|error| RequestError::Transport(error.to_string()))?;
    let status = response.status().as_u16();
    let body = response.body_mut();
    if status != 200 {
        let mut detail = Vec::new();
        let _ = body.as_reader().take(640).read_to_end(&mut detail);
        let detail = String::from_utf8_lossy(&detail).chars().take(160).collect();
        return Err(RequestError::Status(status, detail));
    }
    let bytes = body
        .read_to_vec()
        .map_err(|error| RequestError::Transport(format!("cannot read HTTP response: {error}")))?;
    serde_json::from_slice(&bytes)
        .map_err(|error| RequestError::InvalidResponse(format!("invalid JSON response: {error}")))
}

fn one_cycle(
    config: &Config,
    today: NaiveDate,
    mut ingest: impl FnMut(&str) -> Result<usize, ingest::IngestError>,
    mut request: impl FnMut(&str, Option<&Value>, Duration) -> Result<Value, RequestError>,
    mut emit: impl FnMut(&str),
) -> Result<(), String> {
    let days = held_days(&config.db)?;
    for date in candidate_days(days.last().copied(), today) {
        let day = date.to_string();
        match ingest(&day) {
            Ok(rows) if rows > 0 => emit(&format!("ingest {day}: {rows} rows")),
            Ok(_) => return Err(format!("ingest {day}: package had no usable rows")),
            Err(error) if error.code == "package_not_published" => {
                emit(&format!("ingest {day}: not published"));
                break;
            }
            Err(error) => return Err(format!("ingest {day}: {error}")),
        }
    }
    if config.no_settle {
        return Ok(());
    }

    let account = match request("/v2/account", None, ACCOUNT_TIMEOUT) {
        Ok(account) => account,
        Err(RequestError::Status(404, _)) => {
            emit("no account yet; create one with POST /v2/account");
            return Ok(());
        }
        Err(error) => return Err(format!("read account: {error}")),
    };
    let settled_through = match account.get("last_settled_day") {
        Some(Value::Null) => None,
        Some(Value::String(day)) if day.is_empty() => None,
        Some(Value::String(day)) => Some(iso_day(day)?),
        _ => return Err("invalid account response: expected last_settled_day".into()),
    };

    // Refresh after ingestion. Any failed or ambiguous POST stops this pass so
    // the next cycle can re-read the account cursor before proceeding.
    let days = held_days(&config.db)?;
    let mut pending = days
        .into_iter()
        .filter(|day| settled_through.is_none_or(|settled| *day > settled))
        .peekable();
    if pending.peek().is_none() {
        let last = settled_through.map_or_else(|| "none".into(), |day| day.to_string());
        emit(&format!("nothing to settle (last={last})"));
    }
    for day in pending {
        let result = request(
            "/v2/account/run",
            Some(&json!({ "day": day.to_string() })),
            SETTLE_TIMEOUT,
        )
        .map_err(|error| format!("settle {day}: {error}"))?;
        let trades = result["trades"].as_array();
        let account = result["account"].as_object();
        let (Some(trades), Some(account)) = (trades, account) else {
            return Err(format!("settle {day}: invalid settlement response"));
        };
        let format_metric = |key| {
            account
                .get(key)
                .and_then(Value::as_i64)
                .map_or_else(|| "None".to_owned(), |num| num.to_string())
        };
        emit(&format!(
            "settle {day}: trades={} equity={} ret_bps={}",
            trades.len(),
            format_metric("equity_cents"),
            format_metric("return_bps")
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Bar;
    use std::fs;
    use std::io::BufRead;
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Fixture(PathBuf);

    impl Fixture {
        fn new(days: &[&str]) -> Self {
            static SEQUENCE: AtomicUsize = AtomicUsize::new(0);
            let dir = std::env::temp_dir().join(format!(
                "astock-paper-daemon-{}-{}",
                std::process::id(),
                SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&dir).unwrap();
            let fixture = Self(dir);
            let mut store = Store::open(fixture.db()).unwrap();
            for day in days {
                store.upsert_bars(&[bar(day)]).unwrap();
            }
            fixture
        }

        fn db(&self) -> PathBuf {
            self.0.join("fixture.sqlite3")
        }

        fn config(&self, no_settle: bool) -> Config {
            Config {
                db: self.db(),
                api: DEFAULT_API.into(),
                interval: DEFAULT_INTERVAL,
                once: true,
                no_settle,
            }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn bar(day: &str) -> Bar {
        Bar {
            symbol: "600000.SH".into(),
            day: day.into(),
            name: "fixture".into(),
            open_cents: 0,
            high_cents: 0,
            low_cents: 0,
            close_cents: 1000,
            prev_close_cents: 1000,
            volume_shares: 0,
            amount_cents: 0,
            adjust: "none".into(),
            source: "tdx".into(),
            tradable: false,
        }
    }

    fn day(value: &str) -> NaiveDate {
        iso_day(value).unwrap()
    }

    #[test]
    fn candidates_require_a_seed_and_are_bounded_weekdays() {
        let today = day("2026-11-20");
        assert_eq!(candidate_days(None, today).count(), 0);
        assert_eq!(candidate_days(Some(today), today).count(), 0);
        let days: Vec<_> = candidate_days(Some(day("2026-10-30")), today).collect();
        assert_eq!(days.len(), 12);
        assert_eq!(days[0], day("2026-11-02"));
        assert_eq!(days[11], day("2026-11-17"));
        assert!(
            days.iter()
                .all(|day| day.weekday().number_from_monday() <= 5)
        );
    }

    #[test]
    fn no_settle_retries_the_frontier_and_classifies_errors_by_code() {
        let fixture = Fixture::new(&["2026-11-13"]);
        let mut probes = Vec::new();
        let mut logs = Vec::new();
        let config = fixture.config(true);
        for code in ["package_not_published", "package_network_error"] {
            let outcome = one_cycle(
                &config,
                day("2026-11-18"),
                |date| {
                    probes.push(date.to_owned());
                    Err(ingest::IngestError {
                        code,
                        detail: "package_not_published".into(),
                    })
                },
                |_, _, _| panic!("--no-settle must skip the kernel"),
                |message| logs.push(message.to_owned()),
            );
            if code == "package_not_published" {
                outcome.unwrap();
            } else {
                assert!(outcome.unwrap_err().contains("package_network_error"));
            }
        }
        assert_eq!(probes, ["2026-11-16", "2026-11-16"]);
        assert_eq!(logs, ["ingest 2026-11-16: not published"]);
        assert_eq!(held_days(&config.db).unwrap(), [day("2026-11-13")]);
    }

    #[test]
    fn settlement_refreshes_held_days_and_rechecks_cursor_after_an_ambiguous_post() {
        let fixture = Fixture::new(&["2026-11-13", "2026-11-16", "2026-11-17"]);
        let config = fixture.config(false);
        let mut calls = Vec::new();
        let mut logs = Vec::new();
        for (index, last_settled) in ["2026-11-13", "2026-11-16"].into_iter().enumerate() {
            let mut cursor_reads = 0;
            let result = one_cycle(
                &config,
                day("2026-11-18"),
                |date| {
                    assert_eq!(date, "2026-11-18");
                    assert_eq!(index, 0);
                    let mut store = Store::open(fixture.db()).unwrap();
                    Ok(store.upsert_bars(&[bar(date)]).unwrap())
                },
                |path, payload, timeout| {
                    if path == "/v2/account" {
                        assert!(payload.is_none());
                        assert_eq!(timeout, ACCOUNT_TIMEOUT);
                        cursor_reads += 1;
                        return Ok(json!({"last_settled_day": last_settled}));
                    }
                    assert_eq!(path, "/v2/account/run");
                    assert_eq!(timeout, SETTLE_TIMEOUT);
                    let date = payload.unwrap()["day"].as_str().unwrap();
                    calls.push(date.to_owned());
                    if index == 0 {
                        return Err(RequestError::Transport("lost response".into()));
                    }
                    Ok(json!({"trades": [], "account": {"equity_cents": 1000, "return_bps": 0}}))
                },
                |message| logs.push(message.to_owned()),
            );
            assert_eq!(cursor_reads, 1);
            if index == 0 {
                assert_eq!(
                    result.unwrap_err(),
                    "settle 2026-11-16: http=0 lost response"
                );
            } else {
                result.unwrap();
            }
        }
        assert_eq!(calls, ["2026-11-16", "2026-11-17", "2026-11-18"]);
        assert_eq!(logs[0], "ingest 2026-11-18: 1 rows");
        assert_eq!(logs[2], "settle 2026-11-18: trades=0 equity=1000 ret_bps=0");
    }

    #[test]
    fn missing_account_is_a_noop_but_malformed_responses_stop_settlement() {
        let fixture = Fixture::new(&["2026-11-13", "2026-11-16"]);
        let config = fixture.config(false);
        let mut logs = Vec::new();
        let mut reads = 0;
        one_cycle(
            &config,
            day("2026-11-16"),
            |_| panic!("no newer held day"),
            |path, _, _| {
                reads += 1;
                assert_eq!(path, "/v2/account");
                Err(RequestError::Status(404, "account_not_found".into()))
            },
            |message| logs.push(message.to_owned()),
        )
        .unwrap();
        assert_eq!(reads, 1);
        assert_eq!(logs, ["no account yet; create one with POST /v2/account"]);

        let mut posts = 0;
        let err = one_cycle(
            &config,
            day("2026-11-16"),
            |_| panic!("no newer held day"),
            |path, payload, _| match path {
                "/v2/account" => Ok(json!({"last_settled_day": null})),
                _ => {
                    posts += 1;
                    assert_eq!(payload.unwrap()["day"], "2026-11-13");
                    Ok(json!({"error": "response did not confirm settlement"}))
                }
            },
            |_| panic!("no successful settlement to log"),
        )
        .unwrap_err();
        assert_eq!(posts, 1);
        assert_eq!(err, "settle 2026-11-13: invalid settlement response");
    }

    #[test]
    fn shared_http_agent_preserves_response_status_and_json_errors() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let url = format!("http://{}/v2/account/run", listener.local_addr().unwrap());
        let timeout = Duration::from_secs(3);
        let server = thread::spawn(move || {
            for (status, content) in [
                ("422 Unprocessable Entity", "缺".repeat(161)),
                ("200 OK", "invalid JSON".into()),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(timeout)).unwrap();
                let mut reader = io::BufReader::new(&mut stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                assert_eq!(line.trim_end(), "POST /v2/account/run HTTP/1.1");
                let mut content_length = 0;
                loop {
                    line.clear();
                    assert!(reader.read_line(&mut line).unwrap() > 0);
                    if line == "\r\n" {
                        break;
                    }
                    if let Some(length) = line.to_ascii_lowercase().strip_prefix("content-length:")
                    {
                        content_length = length.trim().parse().unwrap();
                    }
                }
                let mut body = vec![0; content_length];
                reader.read_exact(&mut body).unwrap();
                assert_eq!(body, br#"{"day":"2026-11-16"}"#);
                write!(
                    stream,
                    "HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{content}",
                    content.len()
                )
                .unwrap();
            }
        });
        let payload = json!({"day": "2026-11-16"});
        match request_json(&url, Some(&payload), timeout).unwrap_err() {
            RequestError::Status(422, detail) => assert_eq!(detail, "缺".repeat(160)),
            error => panic!("unexpected response: {error}"),
        }
        assert!(matches!(
            request_json(&url, Some(&payload), timeout),
            Err(RequestError::InvalidResponse(_))
        ));
        server.join().unwrap();
    }
}
