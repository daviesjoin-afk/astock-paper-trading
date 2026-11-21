//! Compatibility entry point for the kernel's isolated intraday replay.
fn main() {
    let mut args = vec!["replay-intraday".to_owned()];
    args.extend(std::env::args().skip(1));
    std::process::exit(astock_v2::cli::maybe_run(&args).unwrap_or(2));
}
