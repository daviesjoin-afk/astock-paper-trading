//! Wall-clock scheduler and transport for the intraday paper kernel.

use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

const DEFAULT_API: &str = "http://127.0.0.1:8765";
const DEFAULT_INTERVAL: u64 = 180;
const DEFAULT_BATCH: usize = 800;
const TIMEZONE: &str = "Asia/Shanghai";
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_CONSECUTIVE_WAITS: usize = 5;
const MORNING_OPEN: NaiveTime = market_time(9, 30);
const MORNING_CLOSE: NaiveTime = market_time(11, 30);
const AFTERNOON_OPEN: NaiveTime = market_time(13, 0);
const AFTERNOON_CLOSE: NaiveTime = market_time(15, 0);

const HELP: &str = "Usage: astock-v2 live-loop [OPTIONS]

Options:
  --api URL           Kernel base URL (default: http://127.0.0.1:8765)
  --universe PATH     Symbol list (default: v2/work/live-a-universe.txt)
  --history-db PATH   Optional SQLite bar database (default: ASTOCK_HISTORY_DB)
  --interval SECONDS  Polling interval (default: 180)
  --close HH:MM       Session close cutoff (default: 15:00)
  --ai off            Deterministic mode (default: off)
  --max-rounds COUNT  Exit after this many successful round posts
  --once              Attempt one round during the current session
  --timezone ZONE     Asia/Shanghai only
  --log PATH          Append JSON lines (default: v2/work/live/intraday-loop.log)
  -h, --help          Show help

Use v2/scripts/live_paper_loop.py for --ai on or a custom timezone.";

const fn market_time(hour: u32, minute: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(hour, minute, 0).unwrap()
}

fn shanghai() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).unwrap()
}

struct Config {
    api: String,
    universe: PathBuf,
    history_db: Option<PathBuf>,
    interval: u64,
    close: NaiveTime,
    max_rounds: Option<u64>,
    once: bool,
    log: PathBuf,
}

impl Config {
    fn parse(args: &[String]) -> Result<Option<Self>, String> {
        let work = Path::new("work");
        let mut config = Self {
            api: DEFAULT_API.into(),
            universe: work.join("live-a-universe.txt"),
            history_db: std::env::var_os("ASTOCK_HISTORY_DB")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from),
            interval: DEFAULT_INTERVAL,
            close: AFTERNOON_CLOSE,
            max_rounds: None,
            once: false,
            log: work.join("live").join("intraday-loop.log"),
        };
        let mut args = args.iter().map(String::as_str).peekable();
        while let Some(arg) = args.next() {
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg, None), |(flag, v)| (flag, Some(v)));
            match (flag, inline) {
                ("-h" | "--help", None) => return Ok(None),
                ("--once", None) => config.once = true,
                (
                    "--api" | "--universe" | "--history-db" | "--interval" | "--close" | "--ai"
                    | "--max-rounds" | "--timezone" | "--log",
                    _,
                ) => {
                    let value = inline
                        .or_else(|| args.next_if(|next| !next.starts_with("--") && *next != "-h"))
                        .ok_or_else(|| format!("{flag} requires a value"))?;
                    match flag {
                        "--api" => config.api = value.trim_end_matches('/').into(),
                        "--universe" => config.universe = value.into(),
                        "--history-db" => {
                            config.history_db = (!value.is_empty()).then(|| value.into())
                        }
                        "--interval" => config.interval = positive(flag, value)?,
                        "--close" => {
                            config.close =
                                NaiveTime::parse_from_str(value, "%H:%M").map_err(|_| {
                                    format!("invalid --close time {value:?}, expected HH:MM")
                                })?;
                        }
                        "--ai" if value == "on" => {
                            return Err("--ai on requires v2/scripts/live_paper_loop.py".into());
                        }
                        "--ai" if value != "off" => return Err("--ai must be off or on".into()),
                        "--timezone" if value != TIMEZONE => {
                            return Err(format!(
                                "--timezone {value:?} requires v2/scripts/live_paper_loop.py (Rust supports {TIMEZONE})"
                            ));
                        }
                        "--max-rounds" => config.max_rounds = Some(positive(flag, value)?),
                        "--log" => config.log = value.into(),
                        _ => {}
                    }
                }
                _ => return Err(format!("unknown argument {arg:?}")),
            }
        }
        Ok(Some(config))
    }
}

fn positive(flag: &str, value: &str) -> Result<u64, String> {
    value
        .parse::<u64>()
        .ok()
        .filter(|&value| value > 0)
        .ok_or_else(|| format!("{flag} must be positive"))
}

pub fn run(args: &[String]) -> i32 {
    let config = match Config::parse(args) {
        Ok(Some(config)) => config,
        Ok(None) => {
            println!("{HELP}");
            return 0;
        }
        Err(error) => {
            eprintln!("live-loop: {error}");
            return 2;
        }
    };
    run_loop(
        &config,
        || Utc::now().with_timezone(&shanghai()),
        thread::sleep,
        || {
            super::feed::build_snapshot(
                &config.universe,
                config.history_db.as_deref(),
                DEFAULT_BATCH,
            )
        },
        |path, payload| {
            super::http::json(
                &format!("{}{path}", config.api),
                Some(payload),
                HTTP_TIMEOUT,
            )
        },
        |event, fields| emit_log(&config.log, event, fields),
    )
}

fn emit_log(path: &Path, event: &str, mut fields: Value) {
    fields["event"] = json!(event);
    let line = fields.to_string();
    write_stdout(&line);
    let append = || -> io::Result<()> {
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)?;
        }
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?
            .write_all(format!("{line}\n").as_bytes())
    };
    if let Err(error) = append() {
        write_stdout(&json!({"event": "log_write_failed", "error": error.to_string()}).to_string());
    }
}

fn write_stdout(line: &str) {
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "{line}").and_then(|_| stdout.flush());
}

fn in_session(now: DateTime<FixedOffset>, close: NaiveTime) -> bool {
    let wall = now.time();
    !is_weekend(now)
        && ((MORNING_OPEN..=MORNING_CLOSE).contains(&wall)
            || (AFTERNOON_OPEN..=AFTERNOON_CLOSE.min(close)).contains(&wall))
}

fn is_weekend(now: DateTime<FixedOffset>) -> bool {
    now.weekday().num_days_from_monday() >= 5
}

fn next_session_open(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    let mut candidate = now.date_naive().and_time(MORNING_OPEN);
    while candidate <= now.naive_local() || candidate.weekday().num_days_from_monday() >= 5 {
        candidate += Duration::from_secs(86_400);
    }
    shanghai().from_local_datetime(&candidate).single().unwrap()
}

fn next_boundary(now: DateTime<FixedOffset>, interval: u64) -> Duration {
    let elapsed = i128::from(now.timestamp()).rem_euclid(i128::from(interval)) as u64;
    Duration::from_secs(interval - elapsed)
        - Duration::from_nanos(u64::from(now.timestamp_subsec_nanos()))
}

fn source_time(value: &Value) -> Option<DateTime<FixedOffset>> {
    let text = value.as_str()?.trim();
    if !matches!(text.as_bytes().get(10), Some(b'T' | b' '))
        || text.as_bytes().get(13) != Some(&b':')
    {
        return None;
    }
    let normalized = text.replace(' ', "T");
    let full = match normalized.as_bytes().get(16) {
        None | Some(b'+' | b'-' | b'Z') => {
            format!("{}:00{}", normalized.get(..16)?, normalized.get(16..)?)
        }
        _ => normalized,
    };
    if let Ok(parsed) = DateTime::parse_from_rfc3339(&full)
        .or_else(|_| DateTime::parse_from_str(&full, "%Y-%m-%dT%H:%M:%S%.f%z"))
    {
        return Some(parsed.with_timezone(&shanghai()));
    }
    let naive = NaiveDateTime::parse_from_str(&full, "%Y-%m-%dT%H:%M:%S%.f").ok()?;
    shanghai().from_local_datetime(&naive).single()
}

#[derive(Default)]
struct RoundState {
    day: Option<NaiveDate>,
    round_ids: Vec<String>,
    start_equity: Option<i64>,
    last_equity: Option<i64>,
    peak_equity: Option<i64>,
    max_drawdown_bps: i128,
    evolved: HashSet<NaiveDate>,
}

impl RoundState {
    fn start_day(&mut self, day: NaiveDate) {
        if self.day != Some(day) {
            self.day = Some(day);
            self.round_ids.clear();
            self.start_equity = None;
            self.last_equity = None;
            self.peak_equity = None;
            self.max_drawdown_bps = 0;
        }
    }

    fn record(&mut self, round_id: &str, response: &Value) {
        if !self.round_ids.iter().any(|id| id == round_id) {
            self.round_ids.push(round_id.to_owned());
        }
        if let Some(value) = response["equity"]["equity_cents"].as_i64() {
            self.start_equity.get_or_insert(value);
            self.last_equity = Some(value);
            let peak = self.peak_equity.get_or_insert(value);
            *peak = (*peak).max(value);
            if *peak > 0 {
                let peak = i128::from(*peak);
                self.max_drawdown_bps = self
                    .max_drawdown_bps
                    .max((peak - i128::from(value)) * 10_000 / peak);
            }
        }
    }

    fn summary(&self) -> Value {
        let profit = self
            .last_equity
            .zip(self.start_equity)
            .map(|(last, start)| i128::from(last) - i128::from(start));
        json!({
            "round_count": self.round_ids.len(),
            "round_ids": self.round_ids,
            "profit_cents": profit.and_then(|value| i64::try_from(value).ok()),
            "equity_cents": self.last_equity,
            "max_drawdown_bps": i64::try_from(self.max_drawdown_bps).unwrap_or(i64::MAX),
            "source": "posted_round_responses",
        })
    }
}

fn blocked_top3(decisions: &[Value]) -> Vec<(&str, usize)> {
    let mut reasons: Vec<(&str, usize)> = Vec::new();
    for reason in decisions
        .iter()
        .filter_map(|item| item["blocked_reason"].as_str())
        .filter(|reason| !reason.is_empty())
    {
        if let Some((_, count)) = reasons.iter_mut().find(|(key, _)| *key == reason) {
            *count += 1;
        } else {
            reasons.push((reason, 1));
        }
    }
    reasons.sort_by_key(|&(_, count)| std::cmp::Reverse(count));
    reasons.truncate(3);
    reasons
}

fn run_round(
    state: &mut RoundState,
    snapshot: Result<Value, String>,
    session_day: NaiveDate,
    post: &mut impl FnMut(&str, &Value) -> Result<Value, String>,
    emit: &mut impl FnMut(&str, Value),
) -> bool {
    let snapshot = match snapshot {
        Ok(snapshot) => snapshot,
        Err(reason) => {
            emit("feed_failed", json!({"reason": reason}));
            return false;
        }
    };
    let quote_time = &snapshot["quote_time"];
    let Some(quotes) = snapshot["quotes"]
        .as_array()
        .filter(|quotes| !quotes.is_empty())
    else {
        emit(
            "round_skipped",
            json!({"reason": "empty_quote_snapshot", "quote_time": quote_time}),
        );
        return false;
    };
    let Some(local) = source_time(quote_time) else {
        emit(
            "round_skipped",
            json!({"reason": format!("quote_time cannot name a trading day: {quote_time}"), "quote_time": quote_time}),
        );
        return false;
    };
    if snapshot["quote_time_estimated"].as_bool() == Some(true) || local.date_naive() != session_day
    {
        emit(
            "round_skipped",
            json!({"reason": "quote_time_not_observed_in_session_day", "quote_time": quote_time, "session_day": session_day.to_string()}),
        );
        return false;
    }
    let quote_time = local.to_rfc3339();
    let round_id = local.format("%Y-%m-%d-%H%M%S").to_string();
    state.start_day(local.date_naive());
    let payload = json!({
        "round_id": round_id,
        "quote_time": quote_time,
        "fetched_at": snapshot["fetched_at"],
        "quotes": quotes,
        "advice": null,
        "note": "live_paper_loop transport",
    });
    let response = match post("/v2/live/round", &payload) {
        Ok(response) => response,
        Err(reason) => {
            emit(
                "round_post_failed",
                json!({"round_id": round_id, "reason": reason}),
            );
            return false;
        }
    };
    state.record(&round_id, &response);
    let decisions = response["decisions"].as_array();
    emit(
        "round",
        json!({
            "round_id": round_id,
            "quote_time": quote_time,
            "quote_time_estimated": snapshot["quote_time_estimated"].as_bool().unwrap_or(false),
            "sample_count": quotes.len(),
            "advice": false,
            "decisions": decisions.map(Vec::len),
            "trades": response["trades"].as_array().map(Vec::len),
            "equity": response["equity"]["equity_cents"],
            "ai": {"ok": response["ai"]["ok"], "error": response["ai"]["error"]},
            "blocked_top3": blocked_top3(decisions.map_or(&[], Vec::as_slice)),
        }),
    );
    true
}

fn close_evolve(
    state: &mut RoundState,
    now: DateTime<FixedOffset>,
    post: &mut impl FnMut(&str, &Value) -> Result<Value, String>,
    emit: &mut impl FnMut(&str, Value),
) {
    let Some(day) = state.day.filter(|day| !state.evolved.contains(day)) else {
        return;
    };
    let day_text = day.to_string();
    let payload = json!({
        "day": day_text,
        "created_at": now.to_rfc3339(),
        "summary": state.summary(),
        "accept": false,
    });
    match post("/v2/live/evolve", &payload) {
        Ok(response) => {
            state.evolved.insert(day);
            emit(
                "evolve",
                json!({
                    "day": day_text,
                    "accepted": false,
                    "reason": response["preview"]["reason"],
                    "proposal": response["preview"]["proposal"],
                    "summary": payload["summary"],
                }),
            );
        }
        Err(reason) => emit("evolve_failed", json!({"day": day_text, "reason": reason})),
    }
}

fn run_loop(
    config: &Config,
    mut clock: impl FnMut() -> DateTime<FixedOffset>,
    mut sleep: impl FnMut(Duration),
    mut snapshot: impl FnMut() -> Result<Value, String>,
    mut post: impl FnMut(&str, &Value) -> Result<Value, String>,
    mut emit: impl FnMut(&str, Value),
) -> i32 {
    let mut state = RoundState::default();
    let mut rounds = 0;
    let mut waits = 0;
    let mut last_idle_reason = None;
    loop {
        let mut now = clock();
        if in_session(now, config.close) {
            last_idle_reason = None;
            waits = 0;
            let posted = run_round(
                &mut state,
                snapshot(),
                now.date_naive(),
                &mut post,
                &mut emit,
            );
            if config.once {
                return i32::from(!posted);
            }
            rounds += u64::from(posted);
            // A slow round can cross a polling boundary or the session close.
            now = clock();
        } else {
            let reason = if is_weekend(now) {
                "weekend"
            } else {
                "not_in_session"
            };
            if last_idle_reason != Some(reason) {
                last_idle_reason = Some(reason);
                emit(
                    "outside_session",
                    json!({"reason": reason, "local_time": now.to_rfc3339()}),
                );
            }
            if config.once {
                return 0;
            }
        }
        let past_close = now.time() >= config.close || is_weekend(now);
        if past_close {
            close_evolve(&mut state, now, &mut post, &mut emit);
        }
        if config.max_rounds.is_some_and(|limit| rounds >= limit) {
            return 0;
        }
        if past_close {
            let target = next_session_open(now);
            waits += 1;
            if waits > MAX_CONSECUTIVE_WAITS {
                emit(
                    "wait_limit_reached",
                    json!({"waits": waits, "until": target.to_rfc3339()}),
                );
                return 1;
            }
            let delay = target
                .signed_duration_since(now)
                .to_std()
                .unwrap()
                .max(Duration::from_secs(1));
            emit(
                "waiting_for_open",
                json!({"until": target.to_rfc3339(), "seconds": delay.as_secs_f64().round_ties_even() as u64}),
            );
            sleep(delay);
        } else {
            sleep(next_boundary(now, config.interval));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Timelike;
    use std::cell::Cell;

    fn config(args: &[&str]) -> Config {
        Config::parse(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>())
            .unwrap()
            .unwrap()
    }

    fn local(text: &str) -> DateTime<FixedOffset> {
        source_time(&json!(text)).unwrap()
    }

    fn snapshot(time: &str) -> Value {
        json!({
            "quote_time": time,
            "fetched_at": "2026-11-20T10:03:07+08:00",
            "quote_time_estimated": false,
            "quotes": [{"symbol": "600000.SH", "price_cents": 1010, "tradable": false}],
        })
    }

    #[test]
    fn flags_reject_unsupported_modes_and_invalid_values() {
        for (args, message) in [
            (["--ai", "on"], "live_paper_loop.py"),
            (["--timezone", "UTC"], "live_paper_loop.py"),
            (["--interval", "0"], "must be positive"),
            (["--max-rounds", "-1"], "must be positive"),
            (["--close", "25:00"], "expected HH:MM"),
            (["--interval", "--once"], "requires a value"),
        ] {
            let args = args.map(str::to_owned);
            assert!(Config::parse(&args).err().unwrap().contains(message));
        }
        let parsed = config(&[
            "--api=http://kernel.invalid/",
            "--universe",
            "universe.txt",
            "--history-db",
            "bars.sqlite",
            "--interval",
            "90",
            "--close",
            "14:30",
            "--ai=off",
            "--max-rounds=2",
            "--once",
            "--timezone=Asia/Shanghai",
            "--log=loop.log",
        ]);
        assert_eq!(parsed.api, "http://kernel.invalid");
        assert_eq!(parsed.universe, Path::new("universe.txt"));
        assert_eq!(parsed.history_db.as_deref(), Some(Path::new("bars.sqlite")));
        assert_eq!(parsed.interval, 90);
        assert_eq!(parsed.close, market_time(14, 30));
        assert_eq!(parsed.max_rounds, Some(2));
        assert!(parsed.once);
        assert_eq!(parsed.log, Path::new("loop.log"));
    }

    #[test]
    fn once_posts_one_unfiltered_snapshot_with_source_identity() {
        let mut times = [local("2026-11-20T10:03:05")].into_iter();
        let snapshot = snapshot("2026-11-20T02:00:02.123Z");
        let mut events = Vec::new();
        let mut calls = 0;
        let code = run_loop(
            &config(&["--once"]),
            || times.next().unwrap(),
            |_| panic!("--once must not sleep"),
            || Ok(snapshot.clone()),
            |path, body| {
                calls += 1;
                assert_eq!(path, "/v2/live/round");
                assert_eq!(
                    *body,
                    json!({
                        "round_id": "2026-11-20-100002",
                        "quote_time": "2026-11-20T10:00:02.123+08:00",
                        "fetched_at": snapshot["fetched_at"],
                        "quotes": snapshot["quotes"],
                        "advice": null,
                        "note": "live_paper_loop transport",
                    })
                );
                Ok(json!({
                    "decisions": [
                        {"blocked_reason": "t_plus_one"}, {"blocked_reason": "cash_budget"},
                        {"blocked_reason": "cash_budget"}, {"blocked_reason": "stale_quote"},
                        {"blocked_reason": "other"}, {},
                    ],
                    "trades": [{}], "equity": {"equity_cents": 1000},
                    "ai": {"ok": false, "error": "no_advice"},
                }))
            },
            |event, fields| events.push((event.to_owned(), fields)),
        );
        assert_eq!(code, 0);
        assert_eq!(calls, 1);
        assert_eq!(
            events,
            [(
                "round".into(),
                json!({
                    "round_id": "2026-11-20-100002", "quote_time": "2026-11-20T10:00:02.123+08:00",
                    "quote_time_estimated": false, "sample_count": 1, "advice": false,
                    "decisions": 6, "trades": 1, "equity": 1000,
                    "ai": {"ok": false, "error": "no_advice"},
                    "blocked_top3": [["cash_budget", 2], ["t_plus_one", 1], ["stale_quote", 1]],
                })
            )]
        );
    }

    #[test]
    fn once_leaves_kernel_untouched_when_idle_or_missing_valid_quotes() {
        let mut estimated = snapshot("2026-11-20T10:02:01+08:00");
        estimated["quote_time_estimated"] = json!(true);
        for (at, input, code, expected) in [
            ("2026-11-21T10:03:05", None, 0, "outside_session"),
            ("2026-11-20T12:03:05", None, 0, "outside_session"),
            ("2026-11-20T09:29:59", None, 0, "outside_session"),
            ("2026-11-20T14:30:01", None, 0, "outside_session"),
            (
                "2026-11-20T10:03:05",
                Some(Err("batch unavailable".into())),
                1,
                "feed_failed",
            ),
            (
                "2026-11-20T10:03:05",
                Some(Ok(json!({"quotes": []}))),
                1,
                "round_skipped",
            ),
            (
                "2026-11-20T10:03:05",
                Some(Ok(snapshot("10:03:00"))),
                1,
                "round_skipped",
            ),
            (
                "2026-11-20T10:03:05",
                Some(Ok(snapshot("2026-11-19T14:59:00+08:00"))),
                1,
                "round_skipped",
            ),
            (
                "2026-11-20T10:03:05",
                Some(Ok(estimated)),
                1,
                "round_skipped",
            ),
        ] {
            let mut events = Vec::new();
            let mut input = input;
            assert_eq!(
                run_loop(
                    &config(&["--once", "--close=14:30"]),
                    || local(at),
                    |_| panic!("--once must not sleep"),
                    || input.take().expect("feed must not be called"),
                    |_, _| panic!("kernel must not be called"),
                    |event, _| events.push(event.to_owned()),
                ),
                code
            );
            assert_eq!(events, [expected]);
        }
    }

    #[test]
    fn failed_posts_retry_on_poll_boundaries_with_the_same_id() {
        let now = Cell::new(local("2026-11-20T10:02:59"));
        let mut polls = Vec::new();
        let mut attempts = Vec::new();
        let mut events = Vec::new();
        let mut delays = Vec::new();
        let code = run_loop(
            &config(&["--max-rounds=1"]),
            || now.get(),
            |delay| {
                delays.push(delay.as_secs());
                now.set(now.get() + delay);
            },
            || {
                polls.push(now.get());
                if polls.len() == 1 {
                    now.set(now.get() + Duration::from_secs(35));
                    return Err("batch unavailable".into());
                }
                Ok(snapshot("2026-11-20 10:03:00"))
            },
            |path, body| {
                assert_eq!(path, "/v2/live/round");
                attempts.push(body["round_id"].clone());
                if attempts.len() == 1 {
                    return Err("lost response".into());
                }
                Ok(json!({}))
            },
            |event, _| events.push(event.to_owned()),
        );
        assert_eq!(code, 0);
        assert_eq!(
            polls,
            [
                local("2026-11-20T10:02:59"),
                local("2026-11-20T10:06:00"),
                local("2026-11-20T10:09:00")
            ]
        );
        assert_eq!(delays, [146, 180]);
        assert_eq!(attempts, ["2026-11-20-100300", "2026-11-20-100300"]);
        assert_eq!(events, ["feed_failed", "round_post_failed", "round"]);
    }

    #[test]
    fn close_posts_inert_daily_summary_and_sleeps_until_monday() {
        let now = Cell::new(local("2026-11-20T14:57:00"));
        let mut events = Vec::new();
        let mut pauses = Vec::new();
        let mut calls = Vec::new();
        let code = run_loop(
            &config(&["--max-rounds=3"]),
            || now.get(),
            |delay| {
                pauses.push(delay.as_secs());
                now.set(now.get() + delay);
            },
            || Ok(snapshot(&now.get().to_rfc3339())),
            |path, body| {
                calls.push(path.to_owned());
                match path {
                    "/v2/live/round" => {
                        let equity = if now.get().minute() == 57 {
                            10_000
                        } else {
                            9_500
                        };
                        Ok(json!({"equity": {"equity_cents": equity}}))
                    }
                    "/v2/live/evolve" => {
                        assert_eq!(
                            *body,
                            json!({
                                "day": "2026-11-20",
                                "created_at": "2026-11-20T15:00:00+08:00",
                                "accept": false,
                                "summary": {
                                    "round_count": 2, "round_ids": ["2026-11-20-145700", "2026-11-20-150000"],
                                    "profit_cents": -500, "equity_cents": 9_500,
                                    "max_drawdown_bps": 500, "source": "posted_round_responses",
                                },
                            })
                        );
                        Ok(json!({"preview": {"reason": "inert"}}))
                    }
                    _ => panic!("unexpected kernel call {path}"),
                }
            },
            |event, _| events.push(event.to_owned()),
        );
        assert_eq!(code, 0);
        assert_eq!(
            calls,
            [
                "/v2/live/round",
                "/v2/live/round",
                "/v2/live/evolve",
                "/v2/live/round"
            ]
        );
        assert_eq!(
            events,
            ["round", "round", "evolve", "waiting_for_open", "round"]
        );
        assert_eq!(pauses, [180, 239_400]);
        assert_eq!(now.get(), local("2026-11-23T09:30:00"));
    }

    #[test]
    fn unmoving_weekend_clock_logs_once_and_hits_wait_safety_limit() {
        let mut sleeps = 0;
        let mut events = Vec::new();
        let code = run_loop(
            &config(&[]),
            || local("2026-11-21T10:03:05"),
            |_| sleeps += 1,
            || panic!("weekend must not fetch"),
            |_, _| panic!("weekend with no observed rounds must not post"),
            |event, _| events.push(event.to_owned()),
        );
        assert_eq!(code, 1);
        assert_eq!(sleeps, 5);
        assert_eq!(events[0], "outside_session");
        assert!(
            events[1..=5]
                .iter()
                .all(|event| event == "waiting_for_open")
        );
        assert_eq!(events[6], "wait_limit_reached");
    }
}
