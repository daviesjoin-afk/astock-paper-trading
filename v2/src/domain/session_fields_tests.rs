//! The OHLCV chain: bar table -> BarRow -> PriceEntry -> Bar -> factor.
//!
//! Every step of this chain used to drop the session's open/high/low/volume.
//! `bars_for` hard-coded `volume: 0`, and `PriceEntry` had no field to carry
//! them even if it had wanted to. Nine of Qlib's Alpha158 factors are pure
//! OHLC combinations (KMID/KLEN/KUP/KLOW/KSFT and their normalised variants),
//! so a whole factor family was unreachable not because the data was missing
//! but because nothing carried it.
//!
//! These tests pin each hop separately. A single end-to-end assertion would
//! pass even if an intermediate step silently zeroed the fields, because the
//! final consumer could still see a plausible-looking number.

use super::*;

fn halt(day: &str) -> BarRow {
    BarRow {
        symbol: "600000.SH".into(),
        day: day.into(),
        price_cents: 1_000,
        tradable: false,
        open_cents: 0,
        high_cents: 0,
        low_cents: 0,
        prev_close_cents: 1_000,
        volume_shares: 0,
    }
}

fn session(day: &str, open: i64, high: i64, low: i64, close: i64, volume: i64) -> BarRow {
    BarRow {
        symbol: "600000.SH".into(),
        day: day.into(),
        price_cents: close,
        tradable: true,
        open_cents: open,
        high_cents: high,
        low_cents: low,
        prev_close_cents: close,
        volume_shares: volume,
    }
}

/// Two days so the symbol's own span is real rather than a single point.
fn two_sessions() -> Vec<BarRow> {
    vec![
        session("2026-11-09", 1_000, 1_050, 990, 1_020, 500_000),
        session("2026-11-10", 1_020, 1_100, 1_010, 1_080, 700_000),
    ]
}

#[test]
fn session_fields_survive_the_bar_row_to_price_entry_hop() {
    let set = PriceSet::from_bars(&two_sessions(), "bar-table", "2026-11-09", "2026-11-10");
    let last = set
        .entries
        .iter()
        .find(|entry| entry.day == "2026-11-10")
        .expect("the second session must be present");
    assert_eq!(last.open_cents, 1_020);
    assert_eq!(last.high_cents, 1_100);
    assert_eq!(last.low_cents, 1_010);
    assert_eq!(last.volume_shares, 700_000);
}

#[test]
fn session_fields_survive_the_price_entry_to_bar_hop() {
    let set = PriceSet::from_bars(&two_sessions(), "bar-table", "2026-11-09", "2026-11-10");
    let index = IndexedPriceSet::new(&set);
    let bars = index.bars_for("600000.SH", "2026-11-10");
    let last = bars.last().expect("a bar for the last session");
    assert_eq!(last.close_cents, 1_080);
    assert_eq!(last.open_cents, 1_020);
    assert_eq!(last.high_cents, 1_100);
    assert_eq!(last.low_cents, 1_010);
    assert_eq!(last.volume, 700_000, "volume must reach the factor input");
    assert!(last.has_session_shape(), "the shape must be usable");
    assert!(last.has_volume());
}

#[test]
fn a_halted_row_reports_missing_shape_instead_of_a_zero_open() {
    // A halt carries the reference close and nothing else. A factor that reads
    // open/high/low has to skip it; `has_session_shape` is how it can tell.
    let bars = vec![
        session("2026-11-09", 1_000, 1_050, 990, 1_020, 500_000),
        halt("2026-11-10"),
    ];
    let set = PriceSet::from_bars(&bars, "bar-table", "2026-11-09", "2026-11-10");
    let index = IndexedPriceSet::new(&set);
    let all = index.bars_for("600000.SH", "2026-11-10");
    // The halted row is valuation-only: `bars_for` filters it out of scoring,
    // which is the existing rule and must not change here.
    assert!(all.iter().all(|bar| bar.tradable && bar.has_session_shape()));
}

/// The point of the chain: a KBAR factor is computable from a real bar.
#[test]
fn a_kbar_factor_is_computable_once_the_chain_carries_the_shape() {
    let set = PriceSet::from_bars(&two_sessions(), "bar-table", "2026-11-09", "2026-11-10");
    let index = IndexedPriceSet::new(&set);
    let bars = index.bars_for("600000.SH", "2026-11-10");
    let bar = bars.last().unwrap();
    assert!(bar.has_session_shape());
    // KMID = (close - open) / open, in bps.
    let kmid_bps = (bar.close_cents - bar.open_cents) * 10_000 / bar.open_cents;
    assert_eq!(kmid_bps, 588);
    // KLEN = (high - low) / open, in bps.
    let klen_bps = (bar.high_cents - bar.low_cents) * 10_000 / bar.open_cents;
    assert_eq!(klen_bps, 882);
}

#[test]
fn a_price_set_that_carries_only_closes_still_loads() {
    // Reviewed two-symbol price sets predate these fields. They must keep
    // deserialising with the session fields absent, and the factors that need
    // them must be able to see that they are absent.
    let json = r#"{
        "schema_version": 1,
        "price_set_id": "legacy",
        "market_data_pack_id": "pack",
        "trust": "reviewed",
        "coverage_start": "2026-11-10",
        "coverage_end": "2026-11-10",
        "source_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "entries": [{
            "symbol": "600000.SH",
            "day": "2026-11-10",
            "price_cents": 1000,
            "source": "reviewed",
            "observed_at": "2026-11-10T15:00:00+08:00"
        }]
    }"#;
    let set: PriceSet = serde_json::from_str(json).expect("legacy sets must still load");
    let index = IndexedPriceSet::new(&set);
    let bars = index.bars_for("600000.SH", "2026-11-10");
    let bar = bars.last().unwrap();
    assert_eq!(bar.close_cents, 1_000);
    assert!(!bar.has_session_shape(), "absent shape must be visible as absent");
    assert!(!bar.has_volume(), "absent volume must not read as a real zero");
}
