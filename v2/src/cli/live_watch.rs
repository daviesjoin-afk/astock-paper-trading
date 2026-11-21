//! Read-only host probe for the scheduled V2 paper account.

use super::LIVE_GRACE;
use super::live_loop::{load_calendar, shanghai};
use crate::domain::{CalendarDay, TradingCalendar};
use chrono::{DateTime, FixedOffset, NaiveTime, SecondsFormat, Utc};
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::path::Path;
use std::time::Duration;

const DEFAULT_API: &str = "http://127.0.0.1:18765";
const HELP: &str = "Usage: astock-v2 live-watch [OPTIONS]

Options:
  --api URL        Kernel base URL (default: http://127.0.0.1:18765)
  --calendar PATH  Trading calendar JSON (default: bundled 2026 SH/SZ schedule)
  --at DATETIME    Override the check time, with an explicit UTC offset
  -h, --help       Show help";

type Issue = &'static str;

struct Config<'a> {
    api: &'a str,
    calendar: Option<&'a Path>,
    at: Option<DateTime<FixedOffset>>,
}

impl<'a> Config<'a> {
    fn parse(args: &'a [String]) -> Result<Option<Self>, String> {
        let mut config = Self {
            api: DEFAULT_API,
            calendar: None,
            at: None,
        };
        let mut args = args.iter().map(String::as_str).peekable();
        while let Some(arg) = args.next() {
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg, None), |(flag, value)| (flag, Some(value)));
            if matches!(flag, "-h" | "--help") && inline.is_none() {
                return Ok(None);
            }
            if !matches!(flag, "--api" | "--calendar" | "--at") {
                return Err(format!("unknown argument {arg:?}"));
            }
            let value = inline
                .or_else(|| args.next_if(|next| !next.starts_with("--") && *next != "-h"))
                .ok_or_else(|| format!("{flag} requires a value"))?;
            match flag {
                "--api" => config.api = value.trim_end_matches('/'),
                "--calendar" => config.calendar = Some(Path::new(value)),
                "--at" => {
                    config.at = Some(
                        parse_timestamp(value)
                            .ok_or("--at requires an ISO timestamp with a UTC offset")?,
                    )
                }
                _ => unreachable!(),
            }
        }
        Ok(Some(config))
    }
}

fn parse_timestamp(text: &str) -> Option<DateTime<FixedOffset>> {
    // Python's CLI accepts ISO timestamps with a space separator, minute
    // precision, and basic as well as colon-delimited offsets.
    let mut normalized = text.replacen(' ', "T", 1);
    if matches!(normalized.as_bytes().get(16), Some(b'+' | b'-' | b'Z')) {
        normalized.insert_str(16, ":00");
    }
    DateTime::parse_from_rfc3339(&normalized)
        .or_else(|_| DateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M:%S%.f%z"))
        .ok()
}

pub fn run(args: &[String]) -> i32 {
    let config = match Config::parse(args) {
        Ok(Some(config)) => config,
        Ok(None) => {
            println!("{HELP}");
            return 0;
        }
        Err(error) => {
            eprintln!("live-watch: {error}");
            return 2;
        }
    };
    let now = config
        .at
        .unwrap_or_else(|| Utc::now().fixed_offset())
        .with_timezone(&shanghai());
    // Match the Python host probe: use a direct connection with a seven-second
    // timeout per GET, independent of the host's proxy environment.
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .timeout_global(Some(Duration::from_secs(7)))
        .build()
        .into();
    let mut report = load_calendar(config.calendar)
        .and_then(|calendar| observe(&calendar, now, |path| get(&agent, config.api, path)))
        .unwrap_or_else(|issue| json!({"status": "alert", "issues": [issue]}));
    report["event"] = json!("paper_watch");
    report["at"] = json!(now.to_rfc3339_opts(SecondsFormat::Secs, false));
    println!("{report}");
    i32::from(report["status"] == "alert")
}

fn get(agent: &ureq::Agent, base: &str, path: &str) -> Result<Value, Issue> {
    let mut response = agent
        .get(format!("{base}{path}"))
        .header("Accept", "application/json")
        .call()
        .map_err(http_issue)?;
    let body = response.body_mut().read_to_vec().map_err(http_issue)?;
    serde_json::from_slice(&body).map_err(|_| "JSONDecodeError")
}

fn http_issue(error: ureq::Error) -> Issue {
    match error {
        ureq::Error::StatusCode(_) | ureq::Error::TooManyRedirects => "HTTPError",
        ureq::Error::Timeout(_) => "TimeoutError",
        ureq::Error::BadUri(_) => "ValueError",
        _ => "URLError",
    }
}

fn decode<T: DeserializeOwned>(value: Value) -> Result<T, Issue> {
    serde_json::from_value(value).map_err(|error| {
        if error.to_string().starts_with("missing field ") {
            "KeyError"
        } else {
            "TypeError"
        }
    })
}

#[derive(Deserialize)]
struct Health {
    status: String,
}

#[derive(Deserialize)]
struct Round {
    started_at: String,
    round_id: String,
    quote_count: i64,
    trade_count: u64,
    ai_ok: bool,
}

#[derive(Deserialize)]
struct Proposal {
    day: String,
    accepted: bool,
    before_metrics: Value,
}

const fn time(hour: u32, minute: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(hour, minute, 0).unwrap()
}

fn observe(
    calendar: &TradingCalendar,
    now: DateTime<FixedOffset>,
    mut fetch: impl FnMut(&str) -> Result<Value, Issue>,
) -> Result<Value, Issue> {
    let day = now.date_naive();
    let calendar_day = calendar.day(day).map_err(|_| "invalid_calendar")?;
    if calendar_day == CalendarDay::Unknown {
        return Err("calendar_coverage_missing");
    }
    let clock = now.time();
    let during_session = (time(9, 37)..=time(11, 30)).contains(&clock)
        || (time(13, 7)..time(15, 0)).contains(&clock);
    let after_close = (time(15, 7)..=time(15, 20)).contains(&clock);
    if calendar_day == CalendarDay::Weekend || !(during_session || after_close) {
        return Ok(json!({"status": "skipped", "reason": "outside_check_window"}));
    }
    if calendar_day == CalendarDay::Holiday {
        return Ok(json!({"status": "skipped", "reason": "market_holiday"}));
    }

    let mut issues = Vec::new();
    let health: Health = decode(fetch("/v2/health")?)?;
    if health.status != "ok" {
        issues.push("kernel_health");
    }
    let mut rounds = Vec::new();
    for row in decode::<Vec<Round>>(fetch("/v2/live/rounds")?)? {
        let stamp = parse_timestamp(&row.started_at)
            .ok_or("ValueError")?
            .with_timezone(now.offset());
        if stamp.date_naive() == day && stamp <= now {
            rounds.push((stamp, row));
        }
    }
    rounds.sort_by_key(|(stamp, _)| *stamp);
    let fills = rounds
        .iter()
        .try_fold(0_u64, |total, (_, row)| total.checked_add(row.trade_count))
        .ok_or("ValueError")?;
    let mut report = json!({
        "rounds_today": rounds.len(),
        "ai_accepted_today": rounds.iter().filter(|(_, row)| row.ai_ok).count(),
        "simulated_fills_today": fills,
    });
    if let Some((stamp, latest)) = rounds.last() {
        report["latest_round"] = json!(latest.round_id);
        report["latest_quote_count"] = json!(latest.quote_count);
        report["latest_age_seconds"] = json!((now - *stamp).num_seconds());
        let horizon = if during_session {
            now
        } else {
            day.and_time(time(15, 0))
                .and_local_timezone(*now.offset())
                .single()
                .unwrap()
        };
        if horizon - *stamp > LIVE_GRACE {
            issues.push("latest_round_stale");
        }
        if latest.quote_count <= 0 {
            issues.push("latest_round_without_quotes");
        } else if rounds.len() >= 4
            && rounds.iter().rev().skip(1).take(3).all(|(_, row)| {
                i128::from(latest.quote_count) * 10 < i128::from(row.quote_count) * 9
            })
        {
            issues.push("quote_coverage_drop");
        }
        if rounds.len() >= 3 && rounds.iter().rev().take(3).all(|(_, row)| !row.ai_ok) {
            issues.push("ai_unavailable_three_rounds");
        }
    } else {
        issues.push("no_rounds_today");
    }

    if after_close {
        let day = day.to_string();
        let proposals: Vec<_> = decode::<Vec<Proposal>>(fetch("/v2/live/evolution")?)?
            .into_iter()
            .filter(|row| row.day == day)
            .collect();
        report["close_proposals"] = json!(proposals.len());
        match proposals.as_slice() {
            [proposal] if proposal.accepted => issues.push("close_proposal_active"),
            [proposal]
                if proposal
                    .before_metrics
                    .get("round_count")
                    .and_then(Value::as_u64)
                    != Some(rounds.len() as u64) =>
            {
                issues.push("close_proposal_round_count");
            }
            [_] => {}
            _ => issues.push("close_proposal_count"),
        }
    }
    report["status"] = json!(if issues.is_empty() { "ok" } else { "alert" });
    report["issues"] = json!(issues);
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    const DAY: &str = "2026-11-20";

    fn round(at: &str, quotes: i64, ai_ok: bool) -> Value {
        json!({"started_at": at, "round_id": at, "quote_count": quotes, "trade_count": 1, "ai_ok": ai_ok})
    }

    fn fixture(at: &str, rounds: Value, proposals: Value) -> (Value, Vec<&'static str>) {
        let mut calls = Vec::new();
        let result = observe(
            &load_calendar(None).unwrap(),
            parse_timestamp(at).unwrap().with_timezone(&shanghai()),
            |path| {
                let (path, value) = match path {
                    "/v2/health" => ("/v2/health", json!({"status": "ok"})),
                    "/v2/live/rounds" => ("/v2/live/rounds", rounds.clone()),
                    "/v2/live/evolution" => ("/v2/live/evolution", proposals.clone()),
                    _ => panic!("unexpected request {path}"),
                };
                calls.push(path);
                Ok(value)
            },
        )
        .unwrap_or_else(|issue| json!({"status": "alert", "issues": [issue]}));
        (result, calls)
    }

    #[test]
    fn filters_and_sorts_rounds_by_shanghai_time() {
        let latest = "2026-11-20T06:09:00Z";
        let (report, calls) = fixture(
            "2026-11-20T06:10:00Z",
            json!([
                round(latest, 5219, true),
                round("2026-11-20T14:11:00+08:00", 0, false),
                round("2026-11-20T13:58:00+08:00", 5219, false),
                round("2026-11-19T14:09:00+08:00", 0, false),
            ]),
            json!([]),
        );
        assert_eq!(calls, ["/v2/health", "/v2/live/rounds"]);
        assert_eq!(
            report,
            json!({"status": "ok", "issues": [], "rounds_today": 2,
            "latest_round": latest, "latest_quote_count": 5219, "latest_age_seconds": 60,
            "simulated_fills_today": 2, "ai_accepted_today": 1})
        );
    }

    #[test]
    fn alerts_keep_the_python_issue_order_and_strict_boundaries() {
        let (report, _) = fixture(
            "2026-11-20T14:10:00+08:00",
            json!([
                round("2026-11-20T13:48:00+08:00", 5219, true),
                round("2026-11-20T13:51:00+08:00", 5219, false),
                round("2026-11-20T13:54:00+08:00", 5219, false),
                round("2026-11-20T13:57:00+08:00", 3000, false),
            ]),
            json!([]),
        );
        assert_eq!(
            report["issues"],
            json!([
                "latest_round_stale",
                "quote_coverage_drop",
                "ai_unavailable_three_rounds"
            ])
        );
        for (count, issues) in [
            (90, json!([])),
            (89, json!(["quote_coverage_drop"])),
            (0, json!(["latest_round_without_quotes"])),
        ] {
            let (report, _) = fixture(
                "2026-11-20T14:10:00+08:00",
                json!([
                    round("2026-11-20T13:54:00+08:00", 100, true),
                    round("2026-11-20T13:57:00+08:00", 100, true),
                    round("2026-11-20T14:00:00+08:00", 100, true),
                    round("2026-11-20T14:03:00+08:00", count, true),
                ]),
                json!([]),
            );
            assert_eq!(report["issues"], issues);
        }
    }

    #[test]
    fn rejects_unknown_days_and_skips_closed_markets_without_http() {
        for (at, expected) in [
            (
                "2026-10-05T14:10:00+08:00",
                json!({"status":"skipped", "reason":"market_holiday"}),
            ),
            (
                "2026-11-21T14:10:00+08:00",
                json!({"status":"skipped", "reason":"outside_check_window"}),
            ),
            (
                "2026-11-20T13:06:59+08:00",
                json!({"status":"skipped", "reason":"outside_check_window"}),
            ),
            (
                "2027-01-04T14:10:00+08:00",
                json!({"status":"alert", "issues":["calendar_coverage_missing"]}),
            ),
            (
                "2027-01-02T00:00:00+08:00",
                json!({"status":"alert", "issues":["calendar_coverage_missing"]}),
            ),
        ] {
            assert_eq!(fixture(at, json!([]), json!([])), (expected, vec![]));
        }
    }

    #[test]
    fn close_uses_the_bell_and_requires_one_inert_complete_proposal() {
        let row = json!({"day": DAY, "accepted": false, "before_metrics": {"round_count": 1}});
        for (proposals, issues) in [
            (json!([row]), json!([])),
            (json!([]), json!(["close_proposal_count"])),
            (json!([row, row]), json!(["close_proposal_count"])),
            (
                json!([{"day": DAY, "accepted": true, "before_metrics": {"round_count": 1}}]),
                json!(["close_proposal_active"]),
            ),
            (
                json!([{"day": DAY, "accepted": false, "before_metrics": {}}]),
                json!(["close_proposal_round_count"]),
            ),
        ] {
            let (report, calls) = fixture(
                "2026-11-20T15:10:00+08:00",
                json!([round("2026-11-20T14:53:00+08:00", 5219, true),]),
                proposals,
            );
            assert_eq!(
                calls,
                ["/v2/health", "/v2/live/rounds", "/v2/live/evolution"]
            );
            assert_eq!(report["issues"], issues);
            assert_eq!(report["latest_age_seconds"], 1020);
        }
    }

    #[test]
    fn cli_uses_direct_gets_and_alerts_on_malformed_json() {
        for at in [
            "2026-11-20T06:10Z",
            "2026-11-20 14:10:00+0800",
            "2026-11-20T14:10:00+08:00",
        ] {
            assert_eq!(
                parse_timestamp(at).unwrap(),
                parse_timestamp("2026-11-20T06:10:00Z").unwrap()
            );
        }
        assert_eq!(run(&["--at=2026-11-20T14:10:00".into()]), 2);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            for (path, body) in [
                ("/v2/health", "{\"status\":\"ok\"}"),
                ("/v2/live/rounds", "invalid"),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut reader = BufReader::new(&mut stream);
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                assert_eq!(line.trim_end(), format!("GET {path} HTTP/1.1"));
                while line != "\r\n" {
                    line.clear();
                    assert_ne!(reader.read_line(&mut line).unwrap(), 0);
                }
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{body}",
                    body.len()
                )
                .unwrap();
            }
        });
        assert_eq!(
            run(&[
                format!("--api=http://{address}/"),
                "--at=2026-11-20T06:10:00Z".into()
            ]),
            1
        );
        server.join().unwrap();
    }
}
