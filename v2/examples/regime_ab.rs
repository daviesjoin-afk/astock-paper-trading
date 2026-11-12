//! Offline A/B evaluation. Both isolated books use the production settlement
//! path and exactly the same trailing price set; the input DB stays read-only.
use std::{env, error::Error, path::PathBuf};

use astock_v2::{
    domain::{AccountSpec, IndexedPriceSet, PriceSet, SlotSpec, StrategyId},
    store::{REGIME_LOOKBACK_DAYS, Store},
};
use serde_json::{Value, json};

const USAGE: &str = "regime_ab --db <existing-v2.sqlite3> --from YYYY-MM-DD --through YYYY-MM-DD [--initial-cash-cents 10000000]\nOutput: JSON on stdout; progress/errors on stderr. Requires 120 prior unadjusted data days.";

struct Options {
    db: PathBuf,
    from: String,
    through: String,
    initial: i64,
}

fn options(args: impl Iterator<Item = String>) -> Result<Options, Box<dyn Error>> {
    let mut flags = std::collections::BTreeMap::new();
    let mut args = args;
    while let Some(flag) = args.next() {
        if !["--db", "--from", "--through", "--initial-cash-cents"].contains(&flag.as_str()) {
            return Err(format!("unknown option: {flag}").into());
        }
        let value = args
            .next()
            .ok_or_else(|| format!("missing value: {flag}"))?;
        if flags.insert(flag.clone(), value).is_some() {
            return Err(format!("duplicate option: {flag}").into());
        }
    }
    let from = flags.remove("--from").ok_or("--from is required")?;
    let through = flags.remove("--through").ok_or("--through is required")?;
    for day in [&from, &through] {
        if chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")?.to_string() != *day {
            return Err("dates must use YYYY-MM-DD".into());
        }
    }
    if from > through {
        return Err("--from must not be after --through".into());
    }
    Ok(Options {
        db: flags.remove("--db").ok_or("--db is required")?.into(),
        from,
        through,
        initial: flags
            .remove("--initial-cash-cents")
            .unwrap_or_else(|| "10000000".into())
            .parse()?,
    })
}

fn compare(source: &Store, options: &Options) -> Result<Value, Box<dyn Error>> {
    let days = source.unadjusted_bar_days()?;
    let first = days
        .binary_search(&options.from)
        .map_err(|_| "from_day_has_no_unadjusted_bars")?;
    let last = days
        .binary_search(&options.through)
        .map_err(|_| "through_day_has_no_unadjusted_bars")?;
    if first < REGIME_LOOKBACK_DAYS {
        return Err(format!(
            "insufficient_warmup: need {REGIME_LOOKBACK_DAYS} prior data days, found {first}"
        )
        .into());
    }
    let spec = AccountSpec {
        schema_version: 1,
        account_id: "regime-ab".into(),
        initial_cash_cents: options.initial,
        created_at: format!("{}T00:00:00+08:00", options.from),
        slots: [
            SlotSpec {
                slot: StrategyId::Steady,
                weight_bps: 5_000,
            },
            SlotSpec {
                slot: StrategyId::Aggressive,
                weight_bps: 5_000,
            },
        ],
    };
    let mut stores = [Store::open(":memory:")?, Store::open(":memory:")?];
    for (index, store) in stores.iter_mut().enumerate() {
        store.regime_filter = index == 1;
        store.create_account(spec.clone())?;
    }
    // Unlike a whole-period rectangular set, a trailing set cannot remove a
    // candidate today just because a future bar is missing. No future rows
    // enter either the candidate universe or the classifier.
    let mut inputs = Vec::new();
    let mut fills = [[0usize; 2]; 2];
    let mut fees = [[0i64; 2]; 2];
    let mut invested_days = [[0usize; 2]; 2];
    let mut trades = [Vec::new(), Vec::new()];
    // Keep exactly the same trailing rows as price_set_from_bars, reading only
    // the new day after the first window. The builder still decides per-name
    // completeness; no future rows or stale-price fills are introduced.
    let mut window =
        source.unadjusted_closes_range(&days[first - REGIME_LOOKBACK_DAYS], &days[first])?;
    for index in first..=last {
        let started = std::time::Instant::now();
        let day = &days[index];
        let start = &days[index - REGIME_LOOKBACK_DAYS];
        if index > first {
        window.retain(|(_, observed, _, _)| observed >= start);
            window.extend(source.unadjusted_closes(day)?);
        }
        let bars: Vec<_> = window
            .iter()
            .map(|(symbol, observed, price_cents, tradable)| astock_v2::domain::BarRow {
                symbol: symbol.clone(),
                day: observed.clone(),
                price_cents: *price_cents,
                tradable: *tradable,
            })
            .collect();
        let set = PriceSet::from_bars(&bars, "regime-ab", start, day);
        if set.entries.is_empty() {
            return Err(format!("no_eligible_price_rows:{day}").into());
        }
        inputs.push(json!({
            "day": day, "window_start": start, "symbols": set.symbols().len(),
            "entries": set.entries.len(), "price_set_digest": set.source_digest,
        }));
        let price_index = IndexedPriceSet::new(set);
        let prepared = started.elapsed();
        for (variant, store) in stores.iter_mut().enumerate() {
            let run = match store.run_day_indexed(&spec, &price_index, day) {
                Ok(run) => run,
                Err(error) => {
                    eprintln!("settlement failed on {day}, regime_filter={}: {error}", store.regime_filter);
                    for book in store.books(&spec.account_id)? {
                        for position in book.positions {
                            eprintln!("held {} {} quantity={} indexed_close={:?} raw_bars={}",
                                book.slot.name(), position.symbol, position.quantity,
                                price_index.valuation(&position.symbol, day),
                                serde_json::to_string(&source.bars_for_symbol(&position.symbol, day, day)?)?);
                        }
                    }
                    return Err(error.into());
                }
            };
            for trade in &run.trades {
                let slot = usize::from(trade.slot == StrategyId::Aggressive);
                fills[variant][slot] += 1;
                fees[variant][slot] += trade.fees_cents;
            }
            trades[variant].extend(run.trades);
            for (slot, book) in store.books(&spec.account_id)?.iter().enumerate() {
                invested_days[variant][slot] += usize::from(!book.positions.is_empty());
            }
        }
        eprintln!(
            "replayed {day} ({}/{}) prepare={}ms settle={}ms",
            index - first + 1,
            last - first + 1,
            prepared.as_millis(),
            (started.elapsed() - prepared).as_millis(),
        );
    }
    let mut variants = Vec::new();
    for (variant, store) in stores.iter().enumerate() {
        let curves = store.account_performance(&spec, None)?;
        let books = store.books(&spec.account_id)?;
        let slots: Vec<_> = ["steady", "aggressive"].iter().enumerate().map(|(index, name)| json!({
            "slot": name,
            "performance": curves[index + 1].last(),
            "fills": fills[variant][index],
            "fees_cents": fees[variant][index],
            "invested_days": invested_days[variant][index],
            "open_positions": books[index].positions.len(),
            "net_transferred_cents": curves[index + 1].iter().map(|p| p.net_flow_cents).sum::<i64>(),
        })).collect();
        variants.push(json!({
            "regime_filter": store.regime_filter,
            "account": curves[0].last(),
            "slots": slots,
            "fills": fills[variant].iter().sum::<usize>(),
            "fees_cents": fees[variant].iter().sum::<i64>(),
            "trades": trades[variant],
            "rebalances": store.rebalances(&spec.account_id)?,
            "books": books,
            "weights_bps": store.weights(&spec.account_id)?,
            "curves": { "account": curves[0], "steady": curves[1], "aggressive": curves[2] },
        }));
    }
    Ok(json!({
        "schema_version": 1,
        "engine_version": env!("CARGO_PKG_VERSION"),
        "input": {
            "database": options.db, "adjust": "none", "from": options.from,
            "through": options.through, "settled_days": last - first + 1,
            "warmup_days": REGIME_LOOKBACK_DAYS, "initial_cash_cents": options.initial,
            "windows": inputs,
        },
        "units": { "money": "integer_cents", "rates": "basis_points", "slot_returns": "time_weighted_end_of_day_flows" },
        "unfiltered": variants[0],
        "filtered": variants[1],
        "limitations": [
            "Daily close simulation; signal and fill use the same close. This is not a next-session execution backtest.",
            "Unadjusted prices do not credit dividends or corporate actions; historical jumps can distort results.",
            "Explicit halted or zero-volume/zero-OHLC rows are valuation-only; they never fill, score or move the market regime. Delisted or wholly absent rows still fail the day rather than being carried forward.",
            "Allocation remains enabled; slot returns remove cash transfers but the two variants may have different capital paths.",
            "Fill counts are not completed round trips or a measure of trade quality. No profitability conclusion is implied."
        ],
    }))
}

fn run() -> Result<(), Box<dyn Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    if args == ["--help"] {
        println!("{USAGE}");
        return Ok(());
    }
    let options = options(args.into_iter())?;
    let source = Store::open_market_read_only(&options.db)?;
    let report = compare(&source, &options)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("regime_ab: {error}\n{USAGE}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use astock_v2::store::Bar;

    fn fixture() -> (Store, Vec<String>) {
        let mut source = Store::open(":memory:").unwrap();
        let start = chrono::NaiveDate::from_ymd_opt(2026, 1, 1).unwrap();
        let days: Vec<_> = (0..126)
            .map(|i| (start + chrono::Duration::days(i)).to_string())
            .collect();
        let mut bars = Vec::new();
        for (i, day) in days.iter().enumerate() {
            for (symbol, close) in [
                ("600000.SH", 1000 + i as i64),
                ("000001.SZ", 2000 - i as i64 * 4),
            ] {
                bars.push(Bar {
                    symbol: symbol.into(),
                    day: day.clone(),
                    name: "fixture".into(),
                    open_cents: close,
                    high_cents: close,
                    low_cents: close,
                    close_cents: close,
                    prev_close_cents: close,
                    volume_shares: 10000,
                    amount_cents: close * 10000,
                    adjust: "none".into(),
                    source: "synthetic-test".into(),
                    tradable: true,
                });
            }
        }
        // This symbol must not become an input, even at an absurd price.
        let mut qfq = bars[0].clone();
        qfq.symbol = "600519.SH".into();
        qfq.adjust = "qfq".into();
        qfq.close_cents = 999_999;
        bars.push(qfq);
        source.upsert_bars(&bars).unwrap();
        (source, days)
    }

    fn config(days: &[String]) -> Options {
        Options {
            db: "synthetic-fixture".into(),
            from: days[120].clone(),
            through: days[125].clone(),
            initial: 10_000_000,
        }
    }

    #[test]
    fn comparison_is_repeatable_uses_real_fills_and_does_not_write_source_ledger() {
        let (source, days) = fixture();
        let report = compare(&source, &config(&days)).unwrap();
        assert_eq!(report, compare(&source, &config(&days)).unwrap());
        assert_eq!(report["input"]["settled_days"], 6);
        assert_eq!(report["input"]["windows"][0]["symbols"], 2);
        for window in report["input"]["windows"].as_array().unwrap() {
            let reference = source.price_set_from_bars(
                "regime-ab", window["window_start"].as_str().unwrap(),
                window["day"].as_str().unwrap(),
            ).unwrap().unwrap();
            assert_eq!(window["price_set_digest"], reference.source_digest);
            assert_eq!(window["entries"], reference.entries.len());
        }
        assert!(report["unfiltered"]["slots"][1]["fills"].as_u64().unwrap() > 0);
        assert_eq!(report["filtered"]["slots"][1]["fills"], 0);
        for variant in ["unfiltered", "filtered"] {
            let slots = report[variant]["slots"].as_array().unwrap();
            let profit: i64 = slots
                .iter()
                .map(|s| s["performance"]["profit_cents"].as_i64().unwrap())
                .sum();
            assert_eq!(
                profit,
                report[variant]["account"]["profit_cents"].as_i64().unwrap()
            );
            assert_eq!(
                report[variant]["curves"]["account"]
                    .as_array()
                    .unwrap()
                    .len(),
                6
            );
        }
        assert!(source.account().unwrap().is_none());
        assert_eq!(source.bar_symbol_count(&days[0]).unwrap(), 3);
    }

    #[test]
    fn future_missing_data_cannot_change_the_first_days_universe_or_fills() {
        let (source, days) = fixture();
        // A future suspension used to remove names when a whole-period
        // rectangle was built. Short and longer runs must share their prefix.
        source
            .conn
            .execute(
                "DELETE FROM bar WHERE symbol='000001.SZ' AND day=?",
                [&days[124]],
            )
            .unwrap();
        let mut short = config(&days);
        short.through = short.from.clone();
        let prefix = compare(&source, &short).unwrap();
        let full = compare(&source, &config(&days)).unwrap();
        assert_eq!(prefix["input"]["windows"][0], full["input"]["windows"][0]);
        for variant in ["unfiltered", "filtered"] {
            assert_eq!(
                prefix[variant]["curves"]["account"][0],
                full[variant]["curves"]["account"][0]
            );
        }
    }

    #[test]
    fn short_history_and_qfq_only_dates_are_rejected() {
        let (mut source, days) = fixture();
        let mut cfg = config(&days);
        cfg.from = days[119].clone();
        assert!(
            compare(&source, &cfg)
                .unwrap_err()
                .to_string()
                .contains("insufficient_warmup")
        );
        let mut bar = source.bars_for_day(&days[0]).unwrap().remove(0);
        bar.day = "2025-12-31".into();
        bar.adjust = "qfq".into();
        source.upsert_bars(&[bar]).unwrap();
        cfg.from = "2025-12-31".into();
        assert!(
            compare(&source, &cfg)
                .unwrap_err()
                .to_string()
                .contains("no_unadjusted_bars")
        );
    }

    #[test]
    fn a_missing_held_quote_fails_instead_of_silently_dropping_a_position() {
        let (source, days) = fixture();
        source
            .conn
            .execute(
                "DELETE FROM bar WHERE symbol='600000.SH' AND day=?",
                [&days[122]],
            )
            .unwrap();
        let error = compare(&source, &config(&days)).unwrap_err().to_string();
        assert!(error.contains("missing_close_price"), "{error}");
    }

    #[test]
    fn arguments_require_explicit_canonical_dates_and_reject_duplicates() {
        let parse = |text: &str| options(text.split_whitespace().map(String::from));
        assert!(parse("--db x --from 2026-01-01 --through 2026-02-01").is_ok());
        for bad in [
            "--db x --from 2026-02-30 --through 2026-03-01",
            "--db x --from 2026-2-1 --through 2026-03-01",
            "--db x --from 2026-02-01 --through 2026-01-01",
            "--db x --db y --from 2026-01-01 --through 2026-02-01",
            "--db x --from 2026-01-01",
            "--unknown x",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
    }
}
