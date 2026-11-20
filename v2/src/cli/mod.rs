//! CLI subcommands for operational tooling previously hosted in Python.
//! Keeps `domain/` pure and preserves bare `astock-v2` (HTTP server) as default.

mod backfill;
pub mod db_tool;
mod feed;
mod http;
mod ingest;
mod live_loop;
mod live_watch;
mod paper_daemon;
pub mod price_set;
pub mod quotes;
pub mod sha256;

/// Dispatch CLI subcommands when invoked with arguments.
/// Returns `None` when the process should boot the HTTP server (`serve` or no args).
pub fn maybe_run(args: &[String]) -> Option<i32> {
    let first = args.first()?.as_str();
    match first {
        "serve" => None,
        "db" => Some(db_tool::run(&args[1..])),
        "price-set" => Some(price_set::run(&args[1..])),
        "live-feed" => Some(feed::run(&args[1..])),
        "live-loop" => Some(live_loop::run(&args[1..])),
        "live-watch" => Some(live_watch::run(&args[1..])),
        "ingest" => Some(ingest::run(&args[1..])),
        "backfill-halted" => Some(backfill::run(&args[1..])),
        "paper-daemon" => Some(paper_daemon::run(&args[1..])),
        "-h" | "--help" | "help" => {
            println!(
                "astock-v2 [serve | db <backup|restore|verify> | price-set ... | live-feed ... | live-loop ... | live-watch ... | ingest ... | backfill-halted ... | paper-daemon ...]"
            );
            Some(0)
        }
        other => {
            eprintln!("error: unknown command: {other}");
            Some(2)
        }
    }
}
