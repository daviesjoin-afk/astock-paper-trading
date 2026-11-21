//! CLI subcommands for operational tooling previously hosted in Python.
//! Keeps `domain/` pure and preserves bare `astock-v2` (HTTP server) as default.

mod backfill;
pub mod db_tool;
mod feed;
pub(crate) mod http;
mod ingest;
mod intraday_replay;
mod live_loop;
mod live_watch;
mod paper_daemon;
pub mod price_set;
pub mod quotes;
pub mod sha256;

// The watchdog allows seven minutes: two default three-minute polls plus one
// minute of slack. Keep the same fixed ceiling even if --interval is longer.
const LIVE_GRACE: chrono::TimeDelta = chrono::TimeDelta::minutes(7);

/// Dispatch CLI subcommands when invoked with arguments.
/// Returns `None` when the process should boot the HTTP server (`serve` or no args).
pub fn maybe_run(args: &[String]) -> Option<i32> {
    let first = args.first()?.as_str();
    match first {
        "serve" => match &args[1..] {
            [] => None,
            [flag] if matches!(flag.as_str(), "-h" | "--help") => {
                println!("usage: astock-v2 serve");
                Some(0)
            }
            _ => {
                eprintln!("usage: astock-v2 serve");
                Some(2)
            }
        },
        "db" => Some(db_tool::run(&args[1..])),
        "price-set" => Some(price_set::run(&args[1..])),
        "live-feed" => Some(feed::run(&args[1..])),
        "live-loop" => Some(live_loop::run(&args[1..])),
        "live-watch" => Some(live_watch::run(&args[1..])),
        "replay-intraday" => Some(intraday_replay::run(&args[1..])),
        "ingest" => Some(ingest::run(&args[1..])),
        "backfill-halted" => Some(backfill::run(&args[1..])),
        "paper-daemon" => Some(paper_daemon::run(&args[1..])),
        "-h" | "--help" | "help" => {
            println!(
                "astock-v2 [serve | db <backup|restore|verify> | price-set ... | live-feed ... | live-loop ... | live-watch ... | replay-intraday | ingest ... | backfill-halted ... | paper-daemon ...]"
            );
            Some(0)
        }
        other => {
            eprintln!("error: unknown command: {other}");
            Some(2)
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn only_bare_serve_starts_the_server() {
        let run = |args: &[&str]| {
            super::maybe_run(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>())
        };
        assert_eq!(run(&[]), None);
        assert_eq!(run(&["serve"]), None);
        assert_eq!(run(&["--help"]), Some(0));
        assert_eq!(run(&["serve", "--help"]), Some(0));
        assert_eq!(run(&["serve", "--unknown"]), Some(2));
    }
}
