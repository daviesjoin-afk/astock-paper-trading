//! Replay identical round inputs through two independent in-memory ledgers.
use crate::intraday::{CreateAccountRequest, IntradayStore, ResearchEntryPolicy, RunRoundRequest};
use chrono::{DateTime, FixedOffset, NaiveDate, Timelike};
use serde::Deserialize;
use serde_json::json;
use std::{
    error::Error,
    io::{self, BufRead},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Experiment {
    initial_cash_cents: i64,
    formed_on: String,
    policy: ResearchEntryPolicy,
    #[serde(default)]
    diagnostic: bool,
}

pub(super) fn run(args: &[String]) -> i32 {
    const USAGE: &str = "usage: astock-v2 replay-intraday < experiment.jsonl";
    match args {
        [] => match replay(io::stdin().lock()) {
            Ok(report) => {
                println!("{report}");
                0
            }
            Err(error) => {
                eprintln!("replay-intraday: {error}");
                1
            }
        },
        [flag] if matches!(flag.as_str(), "-h" | "--help") => {
            println!("{USAGE}");
            0
        }
        _ => {
            eprintln!("{USAGE}");
            2
        }
    }
}

fn replay(input: impl BufRead) -> Result<serde_json::Value, Box<dyn Error>> {
    let mut lines = input.lines();
    let experiment: Experiment = serde_json::from_str(&lines.next().ok_or("missing experiment")??)?;
    let formation = NaiveDate::parse_from_str(&experiment.formed_on, "%Y-%m-%d")?;
    let mut baseline = IntradayStore::open(":memory:")?;
    let mut candidate = IntradayStore::open(":memory:")?;
    let create = CreateAccountRequest {
        initial_cash_cents: experiment.initial_cash_cents,
        created_at: None,
    };
    baseline.create_account(&create)?;
    candidate.create_account(&create)?;
    let shanghai = FixedOffset::east_opt(8 * 3600).unwrap();
    let mut previous_time: Option<DateTime<FixedOffset>> = None;
    let mut prior: Option<RunRoundRequest> = None;
    let mut day = None;
    let mut equity = [experiment.initial_cash_cents; 2];
    let mut start_equity = equity;
    let mut peak = equity;
    let mut drawdown = [0; 2];
    let mut trades = [0; 2];
    let mut buy_count = [0; 2];
    let mut rounds = 0;
    let mut buckets = std::collections::BTreeSet::new();
    let mut report = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for line in lines {
        let mut request: RunRoundRequest = serde_json::from_str(&line?)?;
        let timestamp = DateTime::parse_from_rfc3339(&request.quote_time)?.with_timezone(&shanghai);
        if previous_time.is_some_and(|past| past >= timestamp)
            || !seen.insert(request.round_id.clone())
        {
            return Err(
                "rounds require unique IDs and strictly increasing observation times".into(),
            );
        }
        let preceding_gap = previous_time.map(|past| (timestamp - past).num_seconds());
        previous_time = Some(timestamp);
        request.quote_time = timestamp.to_rfc3339();
        let next_day = timestamp.date_naive();
        if day != Some(next_day) {
            if let Some(done) = day {
                report.push(day_result(
                    done,
                    start_equity,
                    equity,
                    trades,
                    buy_count,
                    drawdown,
                    rounds,
                    buckets.len(),
                ));
            }
            day = Some(next_day);
            prior = None;
            start_equity = equity;
            peak = equity;
            drawdown = [0; 2];
            trades = [0; 2];
            buy_count = [0; 2];
            rounds = 0;
            buckets.clear();
        }
        let minutes = timestamp.hour() * 60 + timestamp.minute();
        if (570..690).contains(&minutes) || (780..900).contains(&minutes) {
            buckets.insert(minutes / 3);
        }
        let policy = if next_day > formation || (experiment.diagnostic && next_day == formation) {
            experiment.policy
        } else {
            ResearchEntryPolicy::Baseline
        };
        let eligible = prior
            .as_ref()
            .filter(|_| preceding_gap.is_some_and(|gap| (60..=420).contains(&gap)));
        let outcomes = [
            baseline.run_forward_research_round(
                &request,
                eligible,
                ResearchEntryPolicy::Baseline,
            )?,
            candidate.run_forward_research_round(&request, eligible, policy)?,
        ];
        for (i, outcome) in outcomes.iter().enumerate() {
            equity[i] = outcome
                .account
                .equity_cents
                .ok_or("missing closing marks")?;
            peak[i] = peak[i].max(equity[i]);
            if peak[i] > 0 {
                drawdown[i] = drawdown[i]
                    .max(((peak[i] - equity[i]) as i128 * 10_000 / peak[i] as i128) as i64);
            }
            trades[i] += outcome.trades.len();
            buy_count[i] += outcome
                .trades
                .iter()
                .filter(|trade| trade.side == "buy")
                .count();
        }
        prior = Some(request);
        rounds += 1;
    }
    let last_day = day.ok_or("no round data")?;
    report.push(day_result(
        last_day,
        start_equity,
        equity,
        trades,
        buy_count,
        drawdown,
        rounds,
        buckets.len(),
    ));
    Ok(json!({
        "formed_on": experiment.formed_on.to_string(), "policy": experiment.policy,
        "diagnostic": experiment.diagnostic, "fill_timing": "next_executable_snapshot",
        "days": report,
    }))
}

fn day_result(
    day: NaiveDate,
    start: [i64; 2],
    end: [i64; 2],
    trades: [usize; 2],
    buys: [usize; 2],
    drawdown: [i64; 2],
    rounds: usize,
    buckets: usize,
) -> serde_json::Value {
    let returns = (0..2)
        .map(|i| (start[i] > 0).then(|| (end[i] - start[i]) as f64 * 10_000.0 / start[i] as f64))
        .collect::<Vec<_>>();
    json!({ "day": day.to_string(), "start_equity_cents": start, "end_equity_cents": end,
        "fills": trades, "buys": buys, "max_drawdown_bps": drawdown, "rounds": rounds, "covered_buckets": buckets, "return_bps": returns })
}
