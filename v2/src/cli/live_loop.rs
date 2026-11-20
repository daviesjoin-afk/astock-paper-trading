//! Wall-clock scheduler and transport for the intraday paper kernel.

use crate::domain::{BUNDLED_CALENDAR_JSON, CalendarDay, TradingCalendar};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

const DEFAULT_API: &str = "http://127.0.0.1:8765";

/// Read a non-empty environment value, treating `""` as unset.
fn env_path(name: &str) -> Option<String> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(|value| value.to_string_lossy().into_owned())
}
const DEFAULT_INTERVAL: u64 = 180;
const DEFAULT_BATCH: usize = 800;
// The watchdog allows seven minutes: two default three-minute polls plus one
// minute of slack. Keep the same fixed ceiling even if --interval is longer.
pub(super) const LIVE_GRACE: chrono::TimeDelta = chrono::TimeDelta::minutes(7);
const TIMEZONE: &str = "Asia/Shanghai";
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_CONSECUTIVE_WAITS: usize = 5;
const MORNING_OPEN: NaiveTime = market_time(9, 30);
const MORNING_CLOSE: NaiveTime = market_time(11, 30);
const AFTERNOON_OPEN: NaiveTime = market_time(13, 0);
const AFTERNOON_CLOSE: NaiveTime = market_time(15, 0);

const HELP: &str = "Usage: astock-v2 live-loop [OPTIONS]

Options:
  --api URL           Kernel base URL (default: $ASTOCK_API, else :8765)
  --universe PATH     Symbol list (default: $ASTOCK_UNIVERSE, else work/...)
  --history-db PATH   Optional SQLite bar database (default: ASTOCK_HISTORY_DB)
  --calendar PATH     Trading calendar JSON (default: bundled 2026 SH/SZ schedule)
  --interval SECONDS  Polling interval (default: 180)
  --close HH:MM       Session close cutoff (default: 15:00)
  --ai off            Deterministic mode (default: off)
  --max-rounds COUNT  Exit after this many successful round posts
  --once              Attempt one round during the current session
  --timezone ZONE     Asia/Shanghai only
  --log PATH          Append JSON lines (default: v2/work/live/intraday-loop.log)
  -h, --help          Show help

Quote source time must be observed in the current session, no later than fetch,
and within seven minutes of submission (two default polls plus one minute).
Use v2/scripts/live_paper_loop.py for --ai on or a custom timezone.";

const fn market_time(hour: u32, minute: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(hour, minute, 0).unwrap()
}

pub(super) fn shanghai() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).unwrap()
}

struct Config {
    api: String,
    universe: PathBuf,
    history_db: Option<PathBuf>,
    calendar_path: Option<PathBuf>,
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
            // ASTOCK_API / ASTOCK_UNIVERSE are what the deployed unit and env
            // file already set; honouring them keeps a bare `live-loop` from
            // talking to 8765 while the kernel listens on 18765.
            api: env_path("ASTOCK_API")
                .unwrap_or_else(|| DEFAULT_API.to_owned())
                .trim_end_matches('/')
                .to_owned(),
            universe: env_path("ASTOCK_UNIVERSE")
                .map(PathBuf::from)
                .unwrap_or_else(|| work.join("live-a-universe.txt")),
            history_db: std::env::var_os("ASTOCK_HISTORY_DB")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from),
            calendar_path: None,
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
                    | "--max-rounds" | "--timezone" | "--log" | "--calendar",
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
                        "--calendar" => config.calendar_path = Some(value.into()),
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
    let calendar = match load_calendar(config.calendar_path.as_deref()) {
        Ok(calendar) => calendar,
        Err(reason) => {
            emit_log(
                &config.log,
                "calendar_unavailable",
                json!({"reason": reason}),
            );
            return 1;
        }
    };
    run_loop(
        &config,
        &calendar,
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

pub(super) fn load_calendar(path: Option<&Path>) -> Result<TradingCalendar, &'static str> {
    let calendar = match path {
        Some(path) => {
            let contents = fs::read_to_string(path).map_err(|error| match error.kind() {
                io::ErrorKind::NotFound => "FileNotFoundError",
                io::ErrorKind::PermissionDenied => "PermissionError",
                io::ErrorKind::InvalidData => "UnicodeDecodeError",
                _ => "OSError",
            })?;
            serde_json::from_str(&contents)
        }
        None => serde_json::from_str(BUNDLED_CALENDAR_JSON),
    }
    .map_err(|error| match error.classify() {
        serde_json::error::Category::Data => "invalid_calendar",
        _ => "JSONDecodeError",
    })?;
    let calendar: TradingCalendar = calendar;
    calendar.validate().map_err(|_| "invalid_calendar")?;
    Ok(calendar)
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

fn session_open(wall: NaiveTime, close: NaiveTime) -> Option<NaiveTime> {
    if (MORNING_OPEN..=MORNING_CLOSE).contains(&wall) {
        Some(MORNING_OPEN)
    } else if (AFTERNOON_OPEN..=AFTERNOON_CLOSE.min(close)).contains(&wall) {
        Some(AFTERNOON_OPEN)
    } else {
        None
    }
}

fn next_session_open(
    calendar: &TradingCalendar,
    now: DateTime<FixedOffset>,
) -> Option<DateTime<FixedOffset>> {
    let mut day = now.date_naive();
    loop {
        match calendar.day(day).ok()? {
            CalendarDay::Trading => {
                let open = day
                    .and_time(MORNING_OPEN)
                    .and_local_timezone(shanghai())
                    .single()?;
                if open > now {
                    return Some(open);
                }
            }
            CalendarDay::Unknown => return None,
            CalendarDay::Holiday | CalendarDay::Weekend => {}
        }
        day = day.succ_opt()?;
    }
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
    scheduled_at: DateTime<FixedOffset>,
    checked_at: DateTime<FixedOffset>,
    close: NaiveTime,
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
    let session_day = scheduled_at.date_naive();
    if !matches!(
        snapshot.get("quote_time_estimated"),
        None | Some(Value::Bool(false))
    ) || local.date_naive() != session_day
    {
        emit(
            "round_skipped",
            json!({"reason": "quote_time_not_observed_in_session_day", "quote_time": quote_time, "session_day": session_day.to_string()}),
        );
        return false;
    }
    let fetched_at = snapshot["fetched_at"]
        .as_str()
        .and_then(|stamp| DateTime::parse_from_rfc3339(stamp).ok());
    let reason = match fetched_at {
        _ if checked_at.date_naive() != session_day
            || session_open(local.time(), close) != session_open(scheduled_at.time(), close) =>
        {
            Some("quote_time_outside_session")
        }
        // Compare actual instants so future source or fetch times cannot extend freshness.
        Some(fetched_at) if local <= fetched_at && fetched_at <= checked_at => {
            (checked_at - local > LIVE_GRACE).then_some("stale_quote_snapshot")
        }
        _ => Some("invalid_quote_fetch_chronology"),
    };
    if let Some(reason) = reason {
        emit(
            "round_skipped",
            json!({
                "reason": reason,
                "quote_time": quote_time,
                "fetched_at": snapshot["fetched_at"],
                "checked_at": checked_at.to_rfc3339(),
            }),
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
    calendar: &TradingCalendar,
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
        let day = now.date_naive();
        let calendar_day = match calendar.day(day) {
            Ok(CalendarDay::Unknown) => {
                emit(
                    "calendar_unavailable",
                    json!({"reason": "calendar_coverage_missing", "day": day.to_string()}),
                );
                return 1;
            }
            Ok(calendar_day) => calendar_day,
            Err(_) => {
                emit(
                    "calendar_unavailable",
                    json!({"reason": "invalid_calendar", "day": day.to_string()}),
                );
                return 1;
            }
        };
        let trading = calendar_day == CalendarDay::Trading;
        if trading && session_open(now.time(), config.close).is_some() {
            last_idle_reason = None;
            waits = 0;
            let input = snapshot();
            let posted = run_round(
                &mut state,
                input,
                now,
                clock(),
                config.close,
                &mut post,
                &mut emit,
            );
            if config.once {
                return i32::from(!posted);
            }
            rounds += u64::from(posted);
            // A slow round can cross a polling boundary or the session close.
            now = clock();
            if now.date_naive() != day {
                continue;
            }
        } else {
            let reason = match calendar_day {
                CalendarDay::Weekend => "weekend",
                CalendarDay::Holiday => "market_holiday",
                _ => "not_in_session",
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
        let past_close = now.time() >= config.close;
        if trading && past_close && state.day == Some(day) {
            close_evolve(&mut state, now, &mut post, &mut emit);
        }
        if config.max_rounds.is_some_and(|limit| rounds >= limit) {
            return 0;
        }
        if past_close || !trading {
            let Some(target) = next_session_open(calendar, now) else {
                emit(
                    "calendar_unavailable",
                    json!({"reason": "calendar_coverage_missing", "day": day.to_string()}),
                );
                return 1;
            };
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
            "fetched_at": source_time(&json!(time)).map(|stamp| stamp.to_rfc3339()),
            "quote_time_estimated": false,
            "quotes": [{"symbol": "600000.SH", "price_cents": 1010, "tradable": false}],
        })
    }

    #[test]
    fn api_and_universe_defaults_come_from_the_deployment_environment() {
        // The installed unit exports these; a bare invocation must not fall
        // back to 8765, which is a different service.
        unsafe {
            std::env::set_var("ASTOCK_API", "http://127.0.0.1:18765/");
            std::env::set_var("ASTOCK_UNIVERSE", "/opt/astock-live/live-a-universe.txt");
        }
        let parsed = config(&["--once"]);
        assert_eq!(parsed.api, "http://127.0.0.1:18765");
        assert_eq!(
            parsed.universe,
            Path::new("/opt/astock-live/live-a-universe.txt")
        );
        // An explicit flag still wins over the environment.
        let explicit = config(&["--once", "--api", "http://127.0.0.1:9999"]);
        assert_eq!(explicit.api, "http://127.0.0.1:9999");
        unsafe {
            std::env::remove_var("ASTOCK_API");
            std::env::remove_var("ASTOCK_UNIVERSE");
        }
        let fallback = config(&["--once"]);
        assert_eq!(fallback.api, "http://127.0.0.1:8765");
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
            "--calendar=calendar.json",
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
        assert_eq!(
            parsed.calendar_path.as_deref(),
            Some(Path::new("calendar.json"))
        );
        assert_eq!(parsed.close, market_time(14, 30));
        assert_eq!(parsed.max_rounds, Some(2));
        assert!(parsed.once);
        assert_eq!(parsed.log, Path::new("loop.log"));
    }

    #[test]
    fn once_posts_one_unfiltered_snapshot_with_source_identity() {
        let now = local("2026-11-20T10:03:05");
        let snapshot = snapshot("2026-11-20T02:00:02.123Z");
        let mut events = Vec::new();
        let mut calls = 0;
        let code = run_loop(
            &config(&["--once"]),
            &load_calendar(None).unwrap(),
            || now,
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
            ("2026-10-05T10:03:05", None, 0, "outside_session"),
            ("2027-01-04T10:03:05", None, 1, "calendar_unavailable"),
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
                    &load_calendar(None).unwrap(),
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
    fn freshness_uses_post_fetch_clock_and_requires_current_session_quotes() {
        for (quote, fetched, reason) in [
            ("10:03:00", "10:03:05", None),
            ("10:02:59.999", "10:03:05", Some("stale_quote_snapshot")),
            (
                "10:03:06",
                "10:03:05",
                Some("invalid_quote_fetch_chronology"),
            ),
            (
                "10:03:06",
                "10:10:01",
                Some("invalid_quote_fetch_chronology"),
            ),
            (
                "10:03:06",
                "invalid",
                Some("invalid_quote_fetch_chronology"),
            ),
            ("09:29:59", "10:03:05", Some("quote_time_outside_session")),
        ] {
            let scheduled = local("2026-11-20T10:03:04");
            let checked = local("2026-11-20T10:10:00");
            let mut clocks = [scheduled, checked].into_iter();
            let mut input = snapshot(&format!("2026-11-20T{quote}+08:00"));
            input["fetched_at"] = json!(format!("2026-11-20T{fetched}+08:00"));
            let mut sent = false;
            assert_eq!(
                run_loop(
                    &config(&["--once", "--interval=1200"]),
                    &load_calendar(None).unwrap(),
                    || clocks.next().unwrap(),
                    |_| panic!("--once must not sleep"),
                    || Ok(input.clone()),
                    |path, _| {
                        assert_eq!(path, "/v2/live/round");
                        sent = true;
                        Ok(json!({}))
                    },
                    |event, fields| match reason {
                        Some(reason) => {
                            assert_eq!(event, "round_skipped");
                            assert_eq!(fields["reason"], reason);
                        }
                        None => assert_eq!(event, "round"),
                    },
                ),
                i32::from(reason.is_some())
            );
            assert_eq!(sent, reason.is_none());
        }
    }

    #[test]
    fn scheduler_jumps_holidays_and_stops_at_calendar_end() {
        let calendar = load_calendar(None).unwrap();
        assert_eq!(
            next_session_open(&calendar, local("2026-09-30T15:00:00")),
            Some(local("2026-10-08T09:30:00"))
        );
        let mut events = Vec::new();
        assert_eq!(
            run_loop(
                &config(&[]),
                &calendar,
                || local("2026-12-31T15:01:00"),
                |_| panic!("must not schedule beyond coverage"),
                || panic!("must not fetch outside the session"),
                |_, _| panic!("must not post without rounds"),
                |event, fields| events.push((event.to_owned(), fields)),
            ),
            1
        );
        assert_eq!(events.last().unwrap().0, "calendar_unavailable");
        assert_eq!(
            events.last().unwrap().1["reason"],
            "calendar_coverage_missing"
        );
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
            &load_calendar(None).unwrap(),
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
            &load_calendar(None).unwrap(),
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
            &load_calendar(None).unwrap(),
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
