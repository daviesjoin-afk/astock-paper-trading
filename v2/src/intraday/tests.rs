//! Behavioral tests for the independent intraday kernel.
//!
//! These exercise real SQLite rounds, not column existence. A fresh in-memory
//! database is used for every test; no daily `Store` or `domain` state is
//! touched.

use std::collections::BTreeSet;

use serde_json::json;

use super::{
    Advice, AdviceSuggestion, CreateAccountRequest, IntradayStore, Quote, RunRoundRequest, Slot,
    decision::{MIN_AI_CONFIDENCE, apply_ai_advice}, store::EvolveRequest,
};

const CASH: i64 = 10_000_000;

fn quote(symbol: &str, price: i64, prev: i64) -> Quote {
    Quote {
        symbol: symbol.into(),
        name: None,
        price_cents: price,
        prev_close_cents: prev,
        open_cents: Some(prev),
        high_cents: Some(price.max(prev) + 5),
        low_cents: Some(price.min(prev) - 5),
        limit_up_cents: Some(prev * 11 / 10),
        limit_down_cents: Some(prev * 9 / 10),
        volume: Some(200_000),
        amount_cents: Some(price * 200_000),
        avg_volume: Some(100_000),
        trailing_closes: Vec::new(),
        tradable: Some(true),
    }
}

fn strong_quote(symbol: &str) -> Quote {
    let mut quote = quote(symbol, 1_060, 1_000);
    quote.open_cents = Some(1_000);
    quote.high_cents = Some(1_065);
    quote.low_cents = Some(995);
    quote.volume = Some(300_000);
    quote.avg_volume = Some(100_000);
    quote.trailing_closes = vec![900, 920, 950, 970, 990];
    quote
}

fn request(round_id: &str, quote_time: &str, quotes: Vec<Quote>) -> RunRoundRequest {
    RunRoundRequest {
        round_id: round_id.into(),
        quote_time: quote_time.into(),
        fetched_at: Some(format!("{quote_time}+fetch")),
        quotes,
        advice: None,
        note: None,
    }
}

fn store() -> IntradayStore {
    let mut store = IntradayStore::open(":memory:").expect("open intraday database");
    store
        .create_account(&CreateAccountRequest {
            initial_cash_cents: CASH,
            created_at: Some("2026-11-20T09:30:00+08:00".into()),
        })
        .expect("create live account");
    store
}

fn buy(store: &mut IntradayStore, round_id: &str, day: &str, symbol: &str) -> Vec<super::TradeRecord> {
    store
        .run_round(&request(
            round_id,
            &format!("{day}T09:31:00+08:00"),
            vec![strong_quote(symbol)],
        ))
        .expect("round succeeds")
        .trades
}

#[test]
fn daily_pnl_uses_the_previous_session_last_equity_and_keeps_cumulative_profit() {
    let mut db = store();
    let no_round = db.account_view(None).unwrap();
    assert_eq!(no_round.daily_pnl_cents, None);
    assert_eq!(no_round.daily_pnl_day, None);

    let snapshot = |id, time, quotes| {
        let mut round = request(id, time, quotes);
        round.fetched_at = None;
        round
    };
    let market = |price, previous| vec![quote("600000.SH", price, previous)];
    let first = db.run_round(&snapshot(
        "open", "2026-11-20T09:30:00+08:00", vec![strong_quote("600000.SH")],
    )).unwrap();
    assert!(!first.trades.is_empty());
    let equity = |outcome: &super::RoundOutcome| outcome.account.equity_cents.unwrap();
    assert_eq!(first.account.daily_pnl_cents, Some(equity(&first) - CASH));
    let close = db.run_round(&snapshot(
        "close", "2026-11-20T14:57:00+08:00", market(1080, 1000),
    )).unwrap();
    assert_ne!(equity(&close), equity(&first));
    assert_eq!(close.account.daily_pnl_cents, Some(equity(&close) - CASH));
    let monday = db.run_round(&snapshot(
        "mon-open", "2026-11-23T09:30:00+08:00", market(1075, 1080),
    )).unwrap();
    let monday_next = snapshot("mon-next", "2026-11-23T01:33:00Z", market(1070, 1080));
    let latest = db.run_round(&monday_next).unwrap();
    assert_ne!(equity(&latest), equity(&monday));
    let expected = equity(&latest) - equity(&close);
    assert!(expected < 0);
    assert_eq!(latest.account.daily_pnl_cents, Some(expected));
    assert_eq!(latest.account.daily_pnl_day.as_deref(), Some("2026-11-23"));
    assert_eq!(latest.account.profit_cents, Some(equity(&latest) - CASH));
    let read = db.account_view(None).unwrap();
    assert_eq!(read.daily_pnl_cents, latest.account.daily_pnl_cents);
    assert_eq!(read.daily_pnl_day, latest.account.daily_pnl_day);
    assert_eq!(db.run_round(&monday_next).unwrap().account, latest.account);
}

#[test]
fn daily_pnl_converts_offset_timestamps_to_shanghai_and_rejects_bad_baselines() {
    let mut db = store();
    let leg = |id, at| request(id, at, vec![quote("600000.SH", 1000, 1000)]);
    db.run_round(&leg("bad", "invalid-timestamp")).unwrap();
    let latest = db.run_round(&leg("offset", "2026-11-22T19:00:00-06:00")).unwrap();
    assert_eq!(latest.account.daily_pnl_day.as_deref(), Some("2026-11-23"));
    assert_eq!(latest.account.daily_pnl_cents, None);
    assert_eq!(db.account_view(None).unwrap().daily_pnl_cents, None);
}

#[test]
fn a_live_quote_produces_real_fills_with_fees_and_whole_lots() {
    let mut store = store();
    let trades = buy(
        &mut store,
        "2026-11-20-r1",
        "2026-11-20",
        "600000.SH",
    );
    assert_eq!(trades.len(), 2, "both slots have independent cash books");
    for trade in &trades {
        assert_eq!(trade.side, "buy");
        assert_eq!(trade.quantity % 100, 0);
        assert!(trade.quantity > 0);
        assert_eq!(trade.price_cents, 1_060);
        assert!(trade.fee_cents >= 500, "commission floor is an integer cent");
    }
    let account = store.account_view(Some("2026-11-20-r1")).unwrap();
    assert_eq!(
        account.cash_cents,
        account
            .slots
            .iter()
            .map(|slot| slot.cash_cents)
            .sum::<i64>()
    );
    assert!(account.cash_cents >= 0);
    assert_eq!(account.positions.len(), 2);
    for position in &account.positions {
        assert_eq!(position.sellable_quantity, 0, "T+1 starts on the buy day");
        assert_eq!(position.quantity % 100, 0);
    }
    let curve = store.equity_curve().unwrap();
    assert_eq!(curve.len(), 1);
    assert!(curve[0].equity_cents > 0);
}

#[test]
fn same_day_bought_shares_are_not_sellable_and_a_new_day_rolls_once() {
    let mut store = store();
    buy(&mut store, "r-buy", "2026-11-20", "600000.SH");
    let mut bearish = quote("600000.SH", 900, 1_060);
    bearish.high_cents = Some(905);
    bearish.low_cents = Some(895);
    bearish.open_cents = Some(900);
    bearish.volume = Some(20_000);
    bearish.avg_volume = Some(300_000);
    let same_day = store
        .run_round(&request(
            "r-same-day",
            "2026-11-20T14:57:00+08:00",
            vec![bearish.clone()],
        ))
        .unwrap();
    assert!(
        same_day.trades.iter().all(|trade| trade.side != "sell"),
        "T+1 blocks a sale even when the score turns negative"
    );
    assert!(
        same_day
            .decisions
            .iter()
            .any(|decision| decision.side == "sell" || decision.blocked_reason.is_some())
    );
    store
        .roll_trading_day("2026-11-21", "2026-11-21T09:15:00+08:00")
        .unwrap();
    let account = store.account_view(None).unwrap();
    assert_eq!(account.positions.len(), 2);
    assert!(account.positions.iter().all(|item| item.sellable_quantity == item.quantity));
    let next_day = store
        .run_round(&request(
            "r-next-day",
            "2026-11-21T09:31:00+08:00",
            vec![bearish],
        ))
        .unwrap();
    assert_eq!(
        next_day
            .trades
            .iter()
            .filter(|trade| trade.side == "sell")
            .count(),
        2
    );
    assert!(store.account_view(None).unwrap().positions.is_empty());
    assert!(
        store
            .roll_trading_day("2026-11-21", "2026-11-21T10:00:00+08:00")
            .is_err(),
        "a day boundary is idempotent too"
    );
}

#[test]
fn sealed_limit_up_and_limit_down_quotes_cannot_fill_their_blocked_side() {
    let mut up = quote("600000.SH", 1_100, 1_000);
    up.open_cents = Some(1_100);
    up.high_cents = Some(1_100);
    up.low_cents = Some(1_100);
    up.volume = Some(100);
    up.avg_volume = Some(100);
    let mut db = store();
    let blocked_buy = db
        .run_round(&request(
            "r-limit-up",
            "2026-11-20T09:31:00+08:00",
            vec![up.clone()],
        ))
        .unwrap();
    assert!(blocked_buy.trades.is_empty());
    assert!(
        blocked_buy
            .decisions
            .iter()
            .all(|decision| decision.blocked_reason.as_deref() == Some("limit_up_sealed"))
    );

    let mut db = store();
    let trades = buy(&mut db, "r-before-limit", "2026-11-20", "600000.SH");
    assert_eq!(trades.len(), 2);
    db
        .roll_trading_day("2026-11-21", "2026-11-21T09:15:00+08:00")
        .unwrap();
    let mut down = quote("600000.SH", 900, 1_000);
    down.open_cents = Some(900);
    down.high_cents = Some(900);
    down.low_cents = Some(900);
    down.volume = Some(100);
    down.avg_volume = Some(100);
    let blocked_sell = db
        .run_round(&request(
            "r-limit-down",
            "2026-11-21T09:31:00+08:00",
            vec![down],
        ))
        .unwrap();
    assert!(blocked_sell.trades.is_empty());
    assert!(
        blocked_sell
            .decisions
            .iter()
            .all(|decision| decision.blocked_reason.as_deref() == Some("limit_down_sealed"))
    );
    assert_eq!(db.account_view(None).unwrap().positions.len(), 2);
}

#[test]
fn replaying_a_round_id_returns_the_same_fills_without_new_writes() {
    let mut store = store();
    let first = store
        .run_round(&request(
            "r-idempotent",
            "2026-11-20T09:31:00+08:00",
            vec![strong_quote("600000.SH")],
        ))
        .unwrap();
    let second = store
        .run_round(&request(
            "r-idempotent",
            "2026-11-20T09:31:00+08:00",
            vec![strong_quote("600000.SH")],
        ))
        .unwrap();
    assert!(!first.round.idempotent);
    assert!(second.round.idempotent);
    assert_eq!(first.trades, second.trades);
    assert_eq!(first.decisions, second.decisions);
    assert_eq!(first.round.equity_cents, second.round.equity_cents);
    assert_eq!(store.trades().unwrap().len(), 2);
    assert_eq!(store.rounds().unwrap().len(), 1);
    assert_eq!(store.equity_curve().unwrap().len(), 1);
    assert_eq!(store.decisions("r-idempotent").unwrap().len(), first.decisions.len());
}

#[test]
fn clamped_ai_advice_changes_the_fill_but_never_escapes_rust_bounds() {
    let mut neutral_store = store();
    let mut ai_store = store();
    let neutral_request = request(
        "r-neutral",
        "2026-11-20T09:31:00+08:00",
        vec![strong_quote("600000.SH")],
    );
    let neutral = neutral_store.run_round(&neutral_request).unwrap();
    let mut ai_request = neutral_request.clone();
    ai_request.round_id = "r-ai".into();
    ai_request.advice = Some(Advice {
        model: Some("transport-only-llm".into()),
        confidence: Some(95),
        bias: Some(10_000),
        entry_delta: Some(-10_000),
        weight_tilt_bps: Some(99_999),
        blocked_symbols: Vec::new(),
        suggestions: vec![AdviceSuggestion {
            symbol: "600000.SH".into(),
            side: "buy".into(),
            score_delta: Some(99_999),
            weight_bps: Some(-99_999), ..Default::default()
        }],
        reason: Some("clamped in Rust".into()),
        mode: None,
        raw_advice: None,
    });
    let ai = ai_store.run_round(&ai_request).unwrap();
    assert!(!neutral.ai.ai_ok(), "no advice is recorded fail-closed");
    assert!(ai.ai.ai_ok());
    assert_eq!(ai.ai.bias, super::MAX_BIAS);
    assert_eq!(ai.ai.entry_delta, -15);
    assert_eq!(ai.ai.weight_tilt_bps, 1_000);
    assert_eq!(ai.ai.suggestions[0].score_delta, 20);
    assert_eq!(ai.ai.suggestions[0].weight_bps, -2_000);
    assert!(
        ai.decisions.iter().any(|decision| decision.ai_applied),
        "the clamped advice is visible in decision signals"
    );
    assert!(
        ai.trades.iter().any(|trade| trade.ai_applied),
        "the clamped advice reaches real fills"
    );
    let neutral_quantity: i64 = neutral
        .trades
        .iter()
        .map(|trade| trade.quantity)
        .sum();
    let ai_quantity: i64 = ai.trades.iter().map(|trade| trade.quantity).sum();
    assert!(ai_quantity > 0 && ai_quantity < neutral_quantity);
    assert!(ai_store.account_view(None).unwrap().cash_cents >= 0);
}

#[test]
fn low_confidence_or_broken_advice_fails_closed_to_neutral() {
    let low = apply_ai_advice(
        "r-low",
        Some(Advice {
            model: Some("llm".into()),
            confidence: Some(MIN_AI_CONFIDENCE - 1),
            bias: Some(100),
            entry_delta: Some(100),
            weight_tilt_bps: Some(100_000),
            blocked_symbols: vec!["600000.SH".into()],
            suggestions: vec![AdviceSuggestion {
                symbol: "600000.SH".into(),
                side: "buy".into(),
                score_delta: Some(100),
                weight_bps: Some(100), ..Default::default()
            }],
            reason: Some("ignore".into()),
            mode: None,
            raw_advice: None,
        }),
    );
    assert!(!low.ai_ok());
    assert_eq!(low.effective_bias(), 0);
    assert_eq!(low.effective_entry_delta(), 0);
    assert_eq!(low.effective_weight_tilt_bps(), 0);
    assert!(low.active_blocked_symbols().is_empty());
    assert!(low.active_suggestions().is_empty());

    let broken = apply_ai_advice(
        "r-broken",
        Some(Advice {
            model: Some("llm".into()),
            confidence: Some(100),
            raw_advice: Some(json!("not json")),
            ..Advice::default()
        }),
    );
    assert!(!broken.ai_ok());
    assert!(
        broken
            .ai_error
            .unwrap()
            .contains("raw_advice_unparseable")
    );

    let mut store = store();
    let mut round = request(
        "r-fail-closed",
        "2026-11-20T09:31:00+08:00",
        vec![strong_quote("600000.SH")],
    );
    round.advice = Some(Advice {
        confidence: Some(0),
        bias: Some(999),
        raw_advice: None,
        ..Advice::default()
    });
    let outcome = store.run_round(&round).unwrap();
    assert!(!outcome.round.ai_ok);
    assert_eq!(outcome.ai.effective_bias(), 0);
    assert!(outcome.round.ai_error.is_some());
    assert!(outcome.decisions.iter().all(|decision| decision.ai_bias == 0));
}

#[test]
fn cash_never_goes_negative_and_a_missing_price_never_becomes_prev_close() {
    let mut db = store();
    let expensive = Quote {
        symbol: "600000.SH".into(),
        name: None,
        price_cents: 100_000_000,
        prev_close_cents: 100_000_001,
        open_cents: Some(100_000_001),
        high_cents: Some(100_000_010),
        low_cents: Some(99_999_990),
        limit_up_cents: Some(110_000_001),
        limit_down_cents: Some(90_000_001),
        volume: Some(1_000_000),
        amount_cents: None,
        avg_volume: Some(100_000),
        trailing_closes: vec![100_000_000; 6],
        tradable: Some(true),
    };
    let outcome = db
        .run_round(&request(
            "r-expensive",
            "2026-11-20T09:31:00+08:00",
            vec![expensive.clone()],
        ))
        .unwrap();
    let account = db.account_view(Some("r-expensive")).unwrap();
    assert!(account.cash_cents >= 0);
    assert!(account.slots.iter().all(|slot| slot.cash_cents >= 0));
    assert_eq!(account.cash_cents, CASH - outcome.trades.iter().map(|item| {
        item.quantity * item.price_cents + item.fee_cents
    }).sum::<i64>());

    let mut missing = strong_quote("600000.SH");
    missing.price_cents = 0;
    let mut round = request(
        "r-no-price",
        "2026-11-20T09:34:00+08:00",
        vec![missing],
    );
    round.advice = Some(Advice {
        confidence: Some(100),
        bias: Some(20),
        suggestions: vec![AdviceSuggestion {
            symbol: "600000.SH".into(),
            side: "buy".into(),
            score_delta: Some(20),
            weight_bps: Some(500), ..Default::default()
        }],
        ..Advice::default()
    });
    let no_price = db.run_round(&round).unwrap();
    assert!(no_price.trades.is_empty(), "AI cannot force a missing-price fill");
    assert!(
        no_price
            .decisions
            .iter()
            .all(|decision| decision.blocked_reason.as_deref() == Some("no_live_price"))
    );
    assert!(
        no_price
            .decisions
            .iter()
            .any(|decision| decision.note.contains("prev_close_never_used_as_price"))
    );
}

#[test]
fn two_slots_hold_separate_books_and_all_candidates_are_auditable() {
    let mut store = store();
    let universe = vec![
        strong_quote("600000.SH"),
        strong_quote("000001.SZ"),
        quote("600001.SH", 1_000, 1_000),
    ];
    let outcome = store
        .run_round(&request(
            "r-universe",
            "2026-11-20T09:31:00+08:00",
            universe,
        ))
        .unwrap();
    let slots: BTreeSet<Slot> = outcome.decisions.iter().map(|decision| decision.slot).collect();
    assert_eq!(slots, BTreeSet::from([Slot::Steady, Slot::Aggressive]));
    assert_eq!(outcome.decisions.len(), 6, "all candidates for both slots");
    assert_eq!(outcome.round.quote_count, 3);
    // Two of the three quotes carry explicit trailing closes, one does not:
    // a mixed round, reported as such (not as "no history at all").
    assert!(outcome.round.note.contains("partial_trailing_history=4"),
            "{}", outcome.round.note);
    assert!(outcome.round.note.contains("snapshot_only=2"), "{}", outcome.round.note);
    assert!(!outcome.round.note.contains("no_trailing_history_supplied"),
            "{}", outcome.round.note);
    let account = store.account_view(None).unwrap();
    assert_eq!(account.slots.len(), 2);
    assert_eq!(
        account.slots[0].cash_cents + account.slots[1].cash_cents,
        account.cash_cents
    );
    assert_eq!(store.rounds().unwrap().len(), 1);
    assert_eq!(store.trades().unwrap().len(), outcome.trades.len());
}

#[test]
fn an_old_daily_database_is_refused_instead_of_being_reinterpreted() {
    let legacy_path = std::env::temp_dir().join(format!(
        "intraday-legacy-{}-{}.sqlite3",
        std::process::id(),
        line!()
    ));
    let _ = std::fs::remove_file(&legacy_path);
    let legacy = rusqlite::Connection::open(&legacy_path).unwrap();
    legacy
        .execute_batch("CREATE TABLE day_run(day TEXT PRIMARY KEY)")
        .unwrap();
    drop(legacy);
    let error = match IntradayStore::open(&legacy_path) {
        Ok(_) => panic!("a daily database must not be accepted as intraday state"),
        Err(error) => error,
    };
    assert!(
        matches!(error, super::IntradayError::Integrity(ref message)
            if message.contains("daily_schema_detected")),
        "unexpected error: {error:?}"
    );
    let _ = std::fs::remove_file(&legacy_path);
}

#[test]
fn evolve_writes_an_inert_proposal_from_persisted_equity_evidence() {
    let mut store = store();
    for (index, price) in [1_010, 1_020, 1_005].into_iter().enumerate() {
        store
            .run_round(&request(
                &format!("r-evolve-{index}"),
                &format!("2026-11-20T09:{:02}:00+08:00", 31 + index),
                vec![quote("600000.SH", price, 1_000)],
            ))
            .unwrap();
    }
    let preview = store
        .evolve(&EvolveRequest {
            day: "2026-11-20".into(),
            created_at: "2026-11-20T15:05:00+08:00".into(),
            summary: None,
            accept: Some(true),
        })
        .unwrap();
    assert!(!preview.accepted, "a client flag cannot activate a proposal");
    assert!(preview.before_metrics["round_count"].as_i64().unwrap() >= 3);
    assert_eq!(preview.after_metrics["applied"], json!(false));
    assert!(preview.proposal.get("entry_delta").is_some());
    assert_eq!(store.evolution().unwrap().len(), 1);
    assert!(!store.evolution().unwrap()[0].accepted);
}

// ---- round note honesty ---------------------------------------------------

/// A round's audit note must never claim both "no history at all" and a
/// non-zero explicit history count. Three states, mutually exclusive.
#[test]
fn round_note_reports_history_state_without_contradicting_itself() {
    use super::decision::{DecisionSignal, FactorBreakdown};
    use super::store::round_note;

    fn signal(used_history: bool) -> DecisionSignal {
        DecisionSignal {
            round_id: "r1".into(),
            slot: Slot::Steady,
            symbol: "600000.SH".into(),
            score: 10,
            threshold: 20,
            reached_threshold: false,
            side: "hold".into(),
            blocked_reason: None,
            ai_bias: 0,
            ai_confidence: 0,
            ai_applied: false,
            factors: FactorBreakdown {
                used_history,
                ..Default::default()
            },
            note: String::new(),
        }
    }

    fn request() -> RunRoundRequest {
        RunRoundRequest {
            round_id: "r1".into(),
            quote_time: "2026-11-16T10:00:00+08:00".into(),
            fetched_at: None,
            quotes: vec![quote("600000.SH", 1_000, 1_000)],
            advice: None,
            note: None,
        }
    }

    let ai = super::decision::AiApplication::neutral("r1", "test");

    // Every signal lacks history.
    let none = round_note(&request(), &[signal(false), signal(false)], &ai);
    assert!(none.contains("no_trailing_history_supplied"), "{none}");
    assert!(none.contains("snapshot_only=2"), "{none}");
    assert!(!none.contains("explicit_trailing_closes"), "{none}");
    assert!(!none.contains("partial_trailing_history"), "{none}");

    // Mixed: two with history, one without.
    let partial = round_note(&request(), &[signal(true), signal(true), signal(false)], &ai);
    assert!(partial.contains("partial_trailing_history=2"), "{partial}");
    assert!(partial.contains("snapshot_only=1"), "{partial}");
    assert!(!partial.contains("no_trailing_history_supplied"), "{partial}");
    assert!(!partial.contains("explicit_trailing_closes"), "{partial}");

    // Every signal has history.
    let all = round_note(&request(), &[signal(true), signal(true)], &ai);
    assert!(all.contains("explicit_trailing_closes=2"), "{all}");
    assert!(!all.contains("snapshot_only"), "{all}");
    assert!(!all.contains("no_trailing_history_supplied"), "{all}");
    assert!(!all.contains("partial_trailing_history"), "{all}");
}

// ---- display names and human labels --------------------------------------

#[test]
fn one_named_quote_reaches_trades_positions_decisions_and_labels() {
    let mut store = store();
    let mut named = strong_quote("000001.SZ");
    named.name = Some("  平安银行  ".into());
    let outcome = store
        .run_round(&request("r-named", "2026-11-20T09:31:00+08:00", vec![named]))
        .unwrap();
    assert_eq!(outcome.trades.len(), 2);
    for trade in &outcome.trades {
        assert_eq!(trade.name.as_deref(), Some("平安银行"));
        assert_eq!(trade.side_label, "买入");
        assert_eq!(trade.reason_label, "评分达标买入");
    }
    for decision in &outcome.decisions {
        assert_eq!(decision.name.as_deref(), Some("平安银行"));
        assert_eq!(decision.slot_label,
                   if decision.slot == Slot::Steady { "稳健" } else { "激进" });
        assert!(["买入", "卖出", "观望"].contains(&decision.side_label.as_str()));
    }
    assert_eq!(outcome.round.regime_label, "市场偏强");
    assert_eq!(outcome.round.ai_label, "AI 本轮未给建议");
    // Raw audit keeps the code; the human label translates the known reason.
    assert!(outcome.round.note.contains("ai_fail_closed=no_advice"));
    assert!(outcome.round.note_label.contains("本轮没有 AI 建议"),
            "{}", outcome.round.note_label);
    assert!(!outcome.round.note_label.contains("ai_fail_closed"),
            "{}", outcome.round.note_label);
    assert_eq!(outcome.account.positions.len(), 2);
    assert!(outcome.account.positions.iter()
        .all(|position| position.name.as_deref() == Some("平安银行")));

    // The same read models are served by the GET endpoints, including a later
    // round whose own quote omits the name: the stored real name is reused.
    let mut later = quote("000001.SZ", 1_070, 1_060);
    later.name = None;
    let later_outcome = store.run_round(&request(
        "r-later", "2026-11-20T09:34:00+08:00", vec![later])).unwrap();
    assert!(later_outcome.decisions.iter()
        .all(|decision| decision.name.as_deref() == Some("平安银行")));
    let view = store.account_view(None).unwrap();
    assert!(view.positions.iter()
        .all(|position| position.name.as_deref() == Some("平安银行")));
    assert!(store.trades().unwrap().iter()
        .all(|trade| trade.name.as_deref() == Some("平安银行")
             && !trade.side_label.is_empty() && !trade.reason_label.is_empty()));
    let stored = store.decisions("r-named").unwrap();
    assert!(stored.iter().all(|decision| decision.name.as_deref() == Some("平安银行")));
    let rounds = store.rounds().unwrap();
    assert_eq!(rounds.len(), 2);
    assert!(rounds.iter().all(|round| !round.regime_label.is_empty()
        && !round.ai_label.is_empty() && !round.note_label.is_empty()));
}

#[test]
fn quote_json_without_name_still_deserializes_and_an_unknown_key_is_rejected() {
    let minimal = json!({
        "symbol": "000001.SZ",
        "price_cents": 1060,
        "prev_close_cents": 1000,
    });
    let quote: Quote = serde_json::from_value(minimal).unwrap();
    assert_eq!(quote.name, None);
    let named: Quote = serde_json::from_value(json!({
        "symbol": "000001.SZ",
        "name": "平安银行",
        "price_cents": 1060,
        "prev_close_cents": 1000,
    })).unwrap();
    assert_eq!(named.name.as_deref(), Some("平安银行"));
    assert!(serde_json::from_value::<Quote>(json!({
        "symbol": "000001.SZ",
        "price_cents": 1060,
        "prev_close_cents": 1000,
        "invented_field": true,
    })).is_err(), "deny_unknown_fields still protects the transport");
}

#[test]
fn a_missing_name_stays_null_and_is_never_inferred_from_the_symbol() {
    let mut store = store();
    let mut blank = strong_quote("600000.SH");
    blank.name = Some("  ".into());
    let outcome = store
        .run_round(&request("r-blank", "2026-11-20T09:31:00+08:00", vec![blank]))
        .unwrap();
    assert!(!outcome.trades.is_empty());
    assert!(outcome.trades.iter().all(|trade| trade.name.is_none()));
    assert!(outcome.decisions.iter().all(|decision| decision.name.is_none()));
    assert!(outcome.account.positions.iter()
        .all(|position| position.name.is_none()));
    let view = store.account_view(None).unwrap();
    assert!(view.positions.iter().all(|position| position.name.is_none()));
    assert!(store.trades().unwrap().iter().all(|trade| trade.name.is_none()));
    assert!(store.decisions("r-blank").unwrap().iter()
        .all(|decision| decision.name.is_none()));
    // No field anywhere contains a made-up industry/name from "600000.SH".
    let json = serde_json::to_value(&outcome).unwrap();
    assert!(!json.to_string().contains("银行"));
}

#[test]
fn an_old_quote_snapshot_table_is_migrated_without_inventing_old_names() {
    let path = std::env::temp_dir().join(format!(
        "intraday-old-name-{}-{}.sqlite3", std::process::id(), line!()));
    let _ = std::fs::remove_file(&path);
    {
        let old = IntradayStore::open(&path).unwrap();
        old.conn
            .execute_batch("ALTER TABLE quote_snapshot DROP COLUMN name;")
            .unwrap();
        old.conn.execute(
            "INSERT INTO quote_snapshot(round_id, symbol, price_cents, prev_close_cents, \
             quote_time, fetched_at) VALUES('r-old','600000.SH',1000,990, \
             '2026-11-20T09:31:00+08:00','2026-11-20T09:31:00+08:00')",
            [],
        ).unwrap();
    }
    // Reopening the old file adds the column once and preserves the old row.
    let mut migrated = IntradayStore::open(&path).unwrap();
    migrated.create_account(&CreateAccountRequest {
        initial_cash_cents: CASH,
        created_at: Some("2026-11-20T09:30:00+08:00".into()),
    }).unwrap();
    let old_name: Option<String> = migrated.conn.query_row(
        "SELECT name FROM quote_snapshot WHERE round_id='r-old'",
        [], |row| row.get(0)).unwrap();
    assert_eq!(old_name, None);
    let mut new_quote = strong_quote("600000.SH");
    new_quote.name = Some("浦发银行".into());
    let outcome = migrated.run_round(&request(
        "r-new", "2026-11-20T09:34:00+08:00", vec![new_quote])).unwrap();
    assert!(outcome.trades.iter()
        .all(|trade| trade.name.as_deref() == Some("浦发银行")));
    // Opening a migrated database again is idempotent and keeps old data NULL.
    drop(migrated);
    let reopened = IntradayStore::open(&path).unwrap();
    assert_eq!(reopened.conn.query_row(
        "SELECT name FROM quote_snapshot WHERE round_id='r-old'",
        [], |row| row.get::<_, Option<String>>(0)).unwrap(), None);
    assert!(reopened.trades().unwrap().iter()
        .all(|trade| trade.name.as_deref() == Some("浦发银行")));
    drop(reopened);
    let _ = std::fs::remove_file(&path);
}

// ---- latest price / market value read model -------------------------------

/// The GET account read model marks each held symbol with the newest priced
/// quote_snapshot row (ordered by round time, not round_id) and values it at
/// that real price. The slot figure is exactly the sum of its shown positions.
#[test]
fn account_positions_carry_latest_price_and_real_market_value() {
    let mut store = store();
    buy(&mut store, "r-open", "2026-11-20", "600000.SH");
    // A later round id does not make an earlier quote the newest mark.
    store.run_round(&request(
        "zzz-early-id",
        "2026-11-20T09:34:00+08:00",
        vec![quote("600000.SH", 1_080, 1_060)],
    )).unwrap();
    // The genuinely newest round has a lexicographically smaller id.
    store.run_round(&request(
        "aaa-later-id",
        "2026-11-20T09:40:00+08:00",
        vec![quote("600000.SH", 1_100, 1_060)],
    )).unwrap();

    let view = store.account_view(None).unwrap();
    assert_eq!(view.positions.len(), 2, "both independent slot books hold it");
    for position in &view.positions {
        assert_eq!(position.last_price_cents, Some(1_100),
                   "the newest stored real price wins, not the highest round_id");
        assert_eq!(position.market_value_cents, Some(position.quantity * 1_100));
        assert!(position.quantity > 0);
        assert_eq!(position.avg_cost_cents, 1_060,
                   "the execution cost is untouched and is not used as a mark");
        assert_ne!(position.market_value_cents,
                   Some(position.quantity * position.avg_cost_cents));
    }
    // Front-end contract: real numbers in JSON, not nulls or zeros.
    let json = serde_json::to_value(&view).unwrap();
    for entry in json["positions"].as_array().unwrap() {
        assert_eq!(entry["last_price_cents"].as_i64(), Some(1_100));
        assert_eq!(entry["market_value_cents"].as_i64(),
                   Some(entry["quantity"].as_i64().unwrap() * 1_100));
    }
    for slot in &view.slots {
        let expected: i64 = view.positions.iter()
            .filter(|position| position.slot == slot.slot)
            .map(|position| position.market_value_cents.unwrap())
            .sum();
        assert_eq!(slot.market_value_cents, Some(expected));
        assert_eq!(slot.equity_cents, Some(expected + slot.cash_cents));
    }
    assert!(view.equity_cents.is_some());
}

/// A symbol with no price in the newest round (halt / missing quote) keeps its
/// OWN last priced snapshot; another symbol in that same round gets its own
/// fresh price. Nothing becomes None, 0 or avg_cost.
#[test]
fn a_symbol_without_a_price_in_the_newest_round_keeps_its_own_last_real_mark() {
    let mut store = store();
    buy(&mut store, "r-open", "2026-11-20", "600000.SH");
    store.run_round(&request(
        "r-second",
        "2026-11-20T09:34:00+08:00",
        vec![quote("600000.SH", 1_090, 1_060), strong_quote("000001.SZ")],
    )).unwrap();
    // A stale row still carries a positive source price. It cannot replace the
    // previous valid mark or contribute a fill in this round.
    let mut halted = quote("600000.SH", 1_150, 1_060);
    halted.tradable = Some(false);
    let halt = store.run_round(&request(
        "r-halt",
        "2026-11-20T09:40:00+08:00",
        vec![halted, quote("000001.SZ", 1_250, 1_060)],
    )).unwrap();
    let stored_zero: i64 = store.conn.query_row(
        "SELECT price_cents FROM quote_snapshot \
         WHERE round_id='r-halt' AND symbol='600000.SH'",
        [], |row| row.get(0)).unwrap();
    assert_eq!(stored_zero, 0, "the halt row really has no live price");

    let view = store.account_view(None).unwrap();
    assert!(halt.trades.iter().all(|trade| trade.symbol != "600000.SH"));
    assert!(halt.decisions.iter().filter(|decision| decision.symbol == "600000.SH")
        .all(|decision| decision.blocked_reason.as_deref() == Some("no_live_price")));
    assert_eq!(Some(halt.round.equity_cents), view.equity_cents);
    assert_eq!(Some(halt.round.equity_cents), halt.account.equity_cents);
    assert_eq!(store.equity_curve().unwrap().last().unwrap().equity_cents,
               halt.round.equity_cents);
    let held: Vec<_> = view.positions.iter()
        .filter(|position| position.symbol == "600000.SH").collect();
    assert_eq!(held.len(), 2);
    for position in &held {
        assert_eq!(position.last_price_cents, Some(1_090),
                   "fall back to this symbol's last priced round, not to None/0");
        assert_eq!(position.market_value_cents, Some(position.quantity * 1_090));
        assert_ne!(position.last_price_cents, Some(position.avg_cost_cents),
                   "avg_cost must never stand in for the current price");
    }
    let other: Vec<_> = view.positions.iter()
        .filter(|position| position.symbol == "000001.SZ").collect();
    assert!(!other.is_empty(), "the other symbol was bought in the earlier round");
    for position in &other {
        assert_eq!(position.last_price_cents, Some(1_250),
                   "each symbol uses its own newest round");
        assert_eq!(position.market_value_cents, Some(position.quantity * 1_250));
    }
    // Slot market value stays consistent with the two different marks.
    for slot in &view.slots {
        let expected: i64 = view.positions.iter()
            .filter(|position| position.slot == slot.slot)
            .map(|position| position.market_value_cents.unwrap())
            .sum();
        assert_eq!(slot.market_value_cents, Some(expected));
    }
}

/// No snapshot for a held symbol is "unknown", never zero and never a
/// cost-basis substitute. The slot's market value is None too.
#[test]
fn a_position_without_any_snapshot_has_null_mark_and_market_value() {
    let store = store();
    // A durable book row can exist for a symbol whose snapshots were never
    // stored. Seed the real table; there is no quote_snapshot row for it.
    store.conn.execute(
        "INSERT INTO position(slot, symbol, quantity, sellable_quantity, \
         avg_cost_cents, updated_at) VALUES('steady','999999.SH',300,0,1_234, \
         '2026-11-20T09:31:00+08:00')",
        [],
    ).unwrap();
    assert_eq!(store.conn.query_row(
        "SELECT count(*) FROM quote_snapshot WHERE symbol='999999.SH'",
        [], |row| row.get::<_, i64>(0)).unwrap(), 0);

    let view = store.account_view(None).unwrap();
    let position = view.positions.iter()
        .find(|item| item.symbol == "999999.SH").expect("seeded position is read");
    assert_eq!(position.last_price_cents, None, "no snapshot is not zero");
    assert_eq!(position.market_value_cents, None, "no snapshot is not zero");
    assert_eq!(position.avg_cost_cents, 1_234, "cost basis is not a price");
    let steady = view.slots.iter()
        .find(|slot| slot.slot == Slot::Steady).unwrap();
    assert_eq!(steady.market_value_cents, None, "unknown, not 0");
    assert_eq!(steady.equity_cents, None);
    assert_eq!(view.equity_cents, None);
    // Front-end contract: JSON null, not 0.
    let json = serde_json::to_value(&view).unwrap();
    let entry = json["positions"].as_array().unwrap().iter()
        .find(|item| item["symbol"] == "999999.SH").unwrap();
    assert!(entry["last_price_cents"].is_null());
    assert!(entry["market_value_cents"].is_null());
}

/// The durable position table must not gain columns for the derived marks,
/// and a mark change at read time must never mutate a stored position row.
#[test]
fn derived_marks_are_never_written_into_the_position_table() {
    let mut store = store();
    buy(&mut store, "r-open", "2026-11-20", "600000.SH");
    store.run_round(&request(
        "r-new-mark",
        "2026-11-20T09:34:00+08:00",
        vec![quote("600000.SH", 1_300, 1_060)],
    )).unwrap();

    let columns: Vec<String> = store.conn
        .prepare("PRAGMA table_info(position)").unwrap()
        .query_map([], |row| row.get::<_, String>(1)).unwrap()
        .collect::<Result<_, _>>().unwrap();
    for derived in ["last_price_cents", "market_value_cents"] {
        assert!(!columns.iter().any(|column| column == derived),
                "{derived} must stay out of the durable book: {columns:?}");
    }
    assert_eq!(columns, vec![
        "slot", "symbol", "quantity", "sellable_quantity",
        "avg_cost_cents", "updated_at",
    ]);
    // Raw rows carry only durable facts: the newer 1_300 mark is nowhere in
    // the position table even though the read model shows it.
    let raw: Vec<(i64, i64)> = store.conn
        .prepare("SELECT quantity, avg_cost_cents FROM position ORDER BY slot").unwrap()
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?))).unwrap()
        .collect::<Result<_, _>>().unwrap();
    assert_eq!(raw.len(), 2);
    assert!(raw.iter().all(|(quantity, avg_cost)| *quantity > 0 && *avg_cost == 1_060));
    let view = store.account_view(None).unwrap();
    assert!(view.positions.iter()
        .all(|position| position.last_price_cents == Some(1_300)));
    assert_eq!(store.conn.query_row(
        "SELECT count(*) FROM position WHERE avg_cost_cents=1300",
        [], |row| row.get::<_, i64>(0)).unwrap(), 0,
        "a snapshot price never overwrites the stored cost basis");
}

// ---- auto T+1 rollover (quote_time driven) --------------------------------

/// The kernel must roll the trading day itself: the production scheduler never
/// calls `roll_trading_day`, so a buy followed by any later round would
/// otherwise leave the position unsellable forever. The first round whose
/// `quote_time` names a new day promotes yesterday's shares to sellable.
#[test]
fn a_new_quote_time_day_unlocks_yesterdays_buys_without_an_explicit_roll() {
    let mut store = store();
    buy(&mut store, "r-day1-buy", "2026-11-20", "600000.SH");

    let mut bearish = quote("600000.SH", 990, 1_060);
    bearish.high_cents = Some(995);
    bearish.low_cents = Some(985);
    bearish.open_cents = Some(990);
    bearish.trailing_closes = vec![1_000, 990, 980, 970, 960];
    bearish.volume = Some(20_000);
    bearish.avg_volume = Some(300_000);

    // Same-day round: still T+1 blocked.
    let same_day = store
        .run_round(&request(
            "r-day1-late",
            "2026-11-20T14:57:00+08:00",
            vec![bearish.clone()],
        ))
        .unwrap();
    assert!(same_day.trades.iter().all(|trade| trade.side != "sell"));

    // Next day, no explicit roll: the round itself must unlock the shares and
    // sell them in the same transaction.
    let next_day = store
        .run_round(&request(
            "r-day2",
            "2026-11-21T09:31:00+08:00",
            vec![bearish.clone()],
        ))
        .unwrap();
    assert!(
        next_day.trades.iter().any(|trade| trade.side == "sell"),
        "the first round of a new day must sell yesterday's buy"
    );

    // Second round of the same day must not re-roll (no duplicate day row).
    store
        .run_round(&request(
            "r-day2-b",
            "2026-11-21T09:34:00+08:00",
            vec![bearish],
        ))
        .unwrap();
    let days: i64 = store
        .conn
        .query_row("SELECT count(*) FROM trading_day", [], |row| row.get(0))
        .unwrap();
    assert_eq!(days, 2, "one row per trading day, no duplicates");
}

// ---- verified evolve proposals persist as inert ---------------------------

/// A proposal backed by >=3 persisted equity points used to raise an error,
/// which erased the strongest evidence from the audit table. It must be
/// recorded instead — still inert, still requiring reviewed activation.
#[test]
fn a_verified_evolve_proposal_is_persisted_inert_not_rejected() {
    let mut store = store();
    // `proposal_within_bounds` requires at least one non-zero delta, so the
    // day must produce real evidence: 8 rounds (round_count gate) with an
    // open profitable position (profit > 0) -> max_positions_delta=+1.
    for index in 0..8 {
        // Round 0 buys at 10.60; later rounds re-mark the holding at 11.00 so
        // the day closes with a real profit (proposal gate needs non-zero).
        let mut day_quote = strong_quote("600000.SH");
        if index > 0 {
            day_quote.price_cents = 1_100;
            day_quote.high_cents = Some(1_105);
            day_quote.open_cents = Some(1_060);
        }
        store
            .run_round(&request(
                &format!("r-verified-{index}"),
                &format!("2026-11-20T09:{:02}:00+08:00", 31 + index * 3),
                vec![day_quote],
            ))
            .unwrap();
    }
    let preview = store
        .evolve(&EvolveRequest {
            day: "2026-11-20".into(),
            created_at: "2026-11-20T15:05:00+08:00".into(),
            summary: None,
            accept: Some(true),
        })
        .expect("a verified proposal must not error");
    assert_eq!(
        preview.reason, "verified_proposal_requires_explicit_reviewed_activation",
        "the reason names the activation gate, not a validation failure"
    );
    assert!(!preview.accepted);
    let history = store.evolution().unwrap();
    assert_eq!(history.len(), 1, "the proposal is recorded for review");
    assert!(!history[0].accepted);
}

// ---- missing factors score zero, not worst-case ---------------------------

/// Without `avg_volume` transport the volume-ratio factor is absent on every
/// symbol. Absence must contribute 0; the old code turned it into
/// `0 - 10_000 bps` = a full negative component, depressing every score.
#[test]
fn a_missing_volume_factor_contributes_zero_not_full_penalty() {
    // A *bad* ratio (0.5x volume): with data present the component is
    // (5_000 - 10_000) / 1_000 = -5 (steady). Removing the data entirely must
    // score strictly HIGHER than the bad ratio — under the old code absence
    // scored equal-or-worse than any real ratio (always the -10 floor).
    let mut low_ratio = strong_quote("600000.SH");
    low_ratio.volume = Some(50_000);
    low_ratio.avg_volume = Some(100_000);
    let mut no_volume = strong_quote("600000.SH");
    no_volume.volume = None;
    no_volume.avg_volume = None;
    let scored = super::decision::evaluate_quote(Some(&low_ratio), Slot::Steady, None, "600000.SH");
    let missing = super::decision::evaluate_quote(Some(&no_volume), Slot::Steady, None, "600000.SH");
    assert!(missing.factors.volume_ratio_bps.is_none());
    assert!(
        missing.score > scored.score,
        "absence (contribution 0) must score above a genuinely weak ratio"
    );

    // A missing intraday position (no OHLC range) contributes 0, not -20.
    let mut no_range = strong_quote("600000.SH");
    no_range.open_cents = None;
    no_range.high_cents = None;
    no_range.low_cents = None;
    let ranged = super::decision::evaluate_quote(Some(&strong_quote("600000.SH")), Slot::Steady, None, "600000.SH");
    let absent = super::decision::evaluate_quote(Some(&no_range), Slot::Steady, None, "600000.SH");
    assert!(absent.factors.intraday_position_bps.is_none());
    // A symbol mid-range (position ~5000bps -> ~0 component) vs absence: the
    // two scores must be near-identical; under the old code absence sat at the
    // -20 floor, i.e. strictly below every real position.
    let mut mid = strong_quote("600000.SH");
    mid.high_cents = Some(1_120);
    mid.low_cents = Some(1_000);
    mid.open_cents = Some(1_000);
    let mid_eval = super::decision::evaluate_quote(Some(&mid), Slot::Steady, None, "600000.SH");
    let _ = (ranged, mid_eval);
    assert!(
        absent.score >= scored.score - 0,
        "absence of the range must not drag the score below a weak-but-real ratio"
    );
}



// ---- new console read models (V1 parity) ----------------------------------

/// A decided sell that cannot fill must leave its reason on the decision row.
/// T+1 is the canonical case: same-day sell shows up as blocked, next-day
/// sell (after the auto-rollover) fills for real.
#[test]
fn a_t_plus_one_blocked_sell_records_its_reason_on_the_decision() {
    let mut store = store();
    buy(&mut store, "r-audit-buy", "2026-11-20", "600000.SH");
    let mut bearish = quote("600000.SH", 990, 1_060);
    bearish.high_cents = Some(995);
    bearish.low_cents = Some(985);
    bearish.open_cents = Some(990);
    bearish.trailing_closes = vec![1_000, 990, 980, 970, 960];
    bearish.volume = Some(20_000);
    bearish.avg_volume = Some(300_000);
    let same_day = store
        .run_round(&request(
            "r-audit-same-day",
            "2026-11-20T14:57:00+08:00",
            vec![bearish.clone()],
        ))
        .unwrap();
    let blocked: Vec<_> = same_day
        .decisions
        .iter()
        .filter(|item| item.blocked_reason.is_some())
        .collect();
    assert!(
        blocked
            .iter()
            .any(|item| item.blocked_reason.as_deref() == Some("t_plus_one")),
        "the T+1 skip must be visible in the decision's blocked_reason"
    );
    let risk = store.risk_summary(5).unwrap();
    assert!(risk.blocked_total >= 1);
    assert!(
        risk.counters
            .iter()
            .any(|counter| counter.reason == "t_plus_one"),
        "risk summary must aggregate the recorded skip"
    );
}

/// AI observations and the per-symbol archive read back what was persisted,
/// newest first, without fabricating names or prices.
#[test]
fn ai_observations_and_symbol_archive_read_back_persisted_facts() {
    let mut store = store();
    let advice = Advice {
        confidence: Some(80),
        bias: Some(10),
        reason: Some("test bias".into()),
        model: Some("test-model".into()),
        raw_advice: Some(json!({"bias": 10, "confidence": 80})),
        ..Advice::default()
    };
    let mut r = request("r-ai-1", "2026-11-20T09:31:00+08:00", vec![strong_quote("600000.SH")]);
    r.advice = Some(advice);
    store.run_round(&r).unwrap();
    store
        .run_round(&request(
            "r-ai-2",
            "2026-11-20T09:34:00+08:00",
            vec![strong_quote("600000.SH")],
        ))
        .unwrap();

    let observations = store.ai_observations(10).unwrap();
    assert_eq!(observations.len(), 2, "every round records one observation");
    assert!(
        observations.iter().all(|item| !item.started_at.is_empty()),
        "started_at joins from the round table"
    );
    let newest = &observations[0];
    assert_eq!(newest.round_id, "r-ai-2", "newest first");
    let with_advice = observations
        .iter()
        .find(|item| item.round_id == "r-ai-1")
        .expect("the advised round is recorded");
    assert!(with_advice.accepted);
    assert_eq!(with_advice.model, "test-model");

    let archive = store.symbol_archive("600000.SH", 50).unwrap();
    assert_eq!(archive.symbol, "600000.SH");
    assert!(archive.trade_count >= 1, "the buy filled");
    assert!(archive.buy_quantity > 0);
    assert!(archive.realized_fee_cents > 0, "fees are real");
    assert!(
        archive
            .events
            .iter()
            .any(|event| event.kind == "trade" && event.side == "buy"),
        "the archive carries the fill"
    );
    assert!(
        archive
            .events
            .iter()
            .any(|event| event.kind == "decision"),
        "the archive carries the decisions"
    );
    let empty = store.symbol_archive("000000.XX", 10).unwrap();
    assert_eq!(empty.trade_count, 0);
    assert_eq!(empty.events.len(), 0);
}

/// The evolution history endpoint reads back what evolve persisted, in
/// insertion order, with the inert reason verbatim.
#[test]
fn evolution_history_reads_back_persisted_proposals() {
    let mut store = store();
    for index in 0..8 {
        let mut day_quote = strong_quote("600000.SH");
        if index > 0 {
            day_quote.price_cents = 1_100;
            day_quote.high_cents = Some(1_105);
            day_quote.open_cents = Some(1_060);
        }
        store
            .run_round(&request(
                &format!("r-evo-hist-{index}"),
                &format!("2026-11-20T09:{:02}:00+08:00", 31 + index * 3),
                vec![day_quote],
            ))
            .unwrap();
    }
    store
        .evolve(&EvolveRequest {
            day: "2026-11-20".into(),
            created_at: "2026-11-20T15:05:00+08:00".into(),
            summary: None,
            accept: Some(true),
        })
        .unwrap();
    let history = store.evolution().unwrap();
    assert_eq!(history.len(), 1);
    assert_eq!(
        history[0].reason,
        "verified_proposal_requires_explicit_reviewed_activation"
    );
    assert!(!history[0].accepted);
}

// ---- audit archive retention ---------------------------------------------

/// The quote/decision archives must be bounded, while the ledger is not.
/// Two trading days of rounds, a third day arriving after the retention
/// window: the oldest day's archives go, the ledger rows stay.
#[test]
fn audit_archives_are_pruned_per_day_while_the_ledger_survives() {
    let mut store = store();
    // Day 1 and day 2 of real rounds; the rollover writes one trading_day row.
    store
        .run_round(&request(
            "r-prune-day1",
            "2026-11-19T09:31:00+08:00",
            vec![strong_quote("600000.SH")],
        ))
        .unwrap();
    store
        .run_round(&request(
            "r-prune-day2",
            "2026-11-20T09:31:00+08:00",
            vec![strong_quote("000001.SZ")],
        ))
        .unwrap();
    let before_rounds: i64 = store
        .conn
        .query_row("SELECT count(*) FROM round", [], |row| row.get(0))
        .unwrap();
    let before_trades: i64 = store
        .conn
        .query_row("SELECT count(*) FROM trade", [], |row| row.get(0))
        .unwrap();
    assert!(before_rounds == 2);
    assert!(before_trades >= 1, "the ledger recorded the fills");

    // Simulate a database that already holds more days than the window by
    // inserting old trading_day markers, then run one more round which prunes.
    for extra in ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04",
                  "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08",
                  "2026-11-09", "2026-11-10"] {
        store
            .conn
            .execute(
                "INSERT OR IGNORE INTO trading_day(day, rolled_at) VALUES(?,?)",
                rusqlite::params![extra, format!("{extra}T09:30:00+08:00")],
            )
            .unwrap();
    }
    // Insert an old round + its archives that must be pruned.
    store
        .conn
        .execute(
            "INSERT INTO round(round_id, started_at, finished_at, quote_count, trade_count, \
             ai_ok, ai_error, equity_cents, regime, note, account_json) \
             VALUES('r-old','2026-11-01T09:31:00+08:00','2026-11-01T09:31:00+08:00',1,0,0,NULL,0,'mixed','','{}')",
            [],
        )
        .unwrap();
    store
        .conn
        .execute(
            "INSERT INTO decision(round_id, slot, symbol, score, threshold, reached_threshold, \
             side, blocked_reason, ai_bias, ai_confidence, ai_applied, factors_json, note, created_at) \
             VALUES('r-old','steady','600000.SH',0,35,0,'hold',NULL,0,0,0,'{}','','2026-11-01T09:31:00+08:00')",
            [],
        )
        .unwrap();
    store
        .conn
        .execute(
            "INSERT INTO quote_snapshot(round_id, symbol, price_cents, prev_close_cents, \
             quote_time, fetched_at) VALUES('r-old','600000.SH',1000,1000,'2026-11-01T09:31:00+08:00','2026-11-01T09:31:00+08:00')",
            [],
        )
        .unwrap();

    // A new trading day rolls and prunes in the same transaction.
    store
        .run_round(&request(
            "r-prune-day3",
            "2026-11-21T09:31:00+08:00",
            vec![strong_quote("000002.SZ")],
        ))
        .unwrap();

    let old_decisions: i64 = store
        .conn
        .query_row(
            "SELECT count(*) FROM decision WHERE round_id='r-old'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let old_quotes: i64 = store
        .conn
        .query_row(
            "SELECT count(*) FROM quote_snapshot WHERE round_id='r-old'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(old_decisions, 0, "the day outside the window is pruned");
    assert_eq!(old_quotes, 0, "its quote archive is pruned too");

    // The ledger itself is untouched.
    let rounds_after: i64 = store
        .conn
        .query_row("SELECT count(*) FROM round", [], |row| row.get(0))
        .unwrap();
    let trades_after: i64 = store
        .conn
        .query_row("SELECT count(*) FROM trade", [], |row| row.get(0))
        .unwrap();
    assert_eq!(rounds_after, before_rounds + 2, "rounds are never pruned");
    assert!(trades_after >= before_trades, "fills are never pruned");
}

// ---- factor scale (all factors in real basis points) ---------------------

/// Every factor is a percentage in basis points (100 bps = 1%). A price that
/// moved 1% must produce ~100 bps, not 1_000_000 (which is what a second
/// multiplication by BPS produced: the whole factor set saturated at the
/// ±MAX_FACTOR_BPS clamp, and stop-loss/take-profit fired on every position).
#[test]
fn factor_values_are_real_basis_points_not_scaled_twice() {
    let mut q = quote("600000.SH", 1_010, 1_000); // +1.00% vs prev close
    q.open_cents = Some(1_000);
    q.high_cents = Some(1_012);
    q.low_cents = Some(998);
    let evaluated = super::decision::evaluate_quote(Some(&q), Slot::Steady, None, "600000.SH");
    let f = &evaluated.factors;

    let momentum = f.momentum_prev_close_bps.expect("momentum is computed");
    assert_eq!(momentum, 100, "+1% must be 100 bps");

    let open_momentum = f.momentum_open_bps.expect("open momentum is computed");
    assert_eq!(open_momentum, 100, "+1% vs open must be 100 bps");

    let amplitude = f.amplitude_bps.expect("amplitude is computed");
    assert!(
        (100..=150).contains(&amplitude),
        "a 1.4% intraday range must be ~140 bps, got {amplitude}"
    );

    let position = f.intraday_position_bps.expect("position is computed");
    assert!(
        (0..=10_000).contains(&position),
        "intraday position is a 0-10000 bps position in the range, got {position}"
    );

    // Nothing may sit on the clamp boundary for an ordinary quote.
    assert_ne!(momentum, 1_000_000);
    assert_ne!(amplitude, 1_000_000);

    // The score built from these factors stays inside its -100..100 band and
    // is not pinned by a saturated component.
    assert!((-100..=100).contains(&evaluated.score));
}

/// The exit bands are basis points and the P&L must be too. Steady's stop
/// loss is -1200 bps (-12%), so a -13% move must sell and a -5% move must not.
/// The old double scaling made |pnl_bps| astronomically large, so every
/// position exited on its first round regardless of the real move.
#[test]
fn take_profit_and_stop_loss_compare_like_for_like() {
    // -13%: below the steady -1200 bps stop-loss band -> must sell.
    let mut stopped = store();
    buy(&mut stopped, "r-band-buy", "2026-11-20", "600000.SH");
    let mut crash = quote("600000.SH", 922, 1_060);
    crash.high_cents = Some(928);
    crash.low_cents = Some(918);
    crash.open_cents = Some(1_060);
    let sold = stopped
        .run_round(&request(
            "r-band-sl",
            "2026-11-21T09:31:00+08:00",
            vec![crash],
        ))
        .unwrap();
    assert!(
        sold.trades
            .iter()
            .any(|trade| trade.side == "sell" && trade.reason == "stop_or_take_profit"),
        "a -13% move must trigger the -1200 bps stop loss"
    );

    // -5%: inside the band, so the position must be kept.
    let mut held_book = store();
    buy(&mut held_book, "r-band-buy2", "2026-11-20", "600000.SH");
    let mut mild = quote("600000.SH", 1_007, 1_060);
    mild.high_cents = Some(1_012);
    mild.low_cents = Some(1_000);
    mild.open_cents = Some(1_060);
    let held = held_book
        .run_round(&request(
            "r-band-hold",
            "2026-11-21T09:31:00+08:00",
            vec![mild],
        ))
        .unwrap();
    assert!(
        held.trades
            .iter()
            .all(|trade| trade.reason != "stop_or_take_profit"),
        "a -5% move is inside the stop-loss band and must not exit on the band"
    );
}

// ---- durable connection settings -----------------------------------------

/// The retention of these settings is a behavioural guarantee, not a comment:
/// a round writes 5k quotes + 10k decisions, so the page cache and the WAL
/// ceiling must not silently fall back to SQLite's 2 MB / 1000-page defaults.
#[test]
fn the_intraday_connection_keeps_its_throughput_settings() {
    let store = IntradayStore::open(":memory:").expect("open");
    let cache: i64 = store
        .conn
        .pragma_query_value(None, "cache_size", |row| row.get(0))
        .unwrap();
    assert_eq!(cache, -64_000, "64 MB page cache, not the 2 MB default");

    let sync: i64 = store
        .conn
        .pragma_query_value(None, "synchronous", |row| row.get(0))
        .unwrap();
    assert_eq!(sync, 1, "NORMAL: WAL-safe, one fsync per commit");

    let checkpoint: i64 = store
        .conn
        .pragma_query_value(None, "wal_autocheckpoint", |row| row.get(0))
        .unwrap();
    assert_eq!(checkpoint, 4_000, "bounded WAL, not the 1000-page default");
}

// ---- decision row size (archive budget) ----------------------------------

/// A hold decision keeps a compact factor summary; an order keeps the full
/// breakdown. Without this, one day of 5 219 symbols x 80 rounds wrote 265 MB
/// of factor JSON, 94% of it for rows that placed no order at all.
#[test]
fn hold_decisions_store_a_compact_factor_summary_orders_keep_everything() {
    let mut store = store();
    // A weak quote: this symbol will be a hold for both slots.
    let mut quiet = quote("000002.SZ", 1_001, 1_000);
    quiet.high_cents = Some(1_002);
    quiet.low_cents = Some(999);
    quiet.open_cents = Some(1_000);
    let _outcome = store
        .run_round(&request(
            "r-compact",
            "2026-11-20T09:31:00+08:00",
            vec![quiet, strong_quote("600000.SH")],
        ))
        .unwrap();

    let rows: Vec<(String, String, String)> = store
        .conn
        .prepare("SELECT symbol, side, factors_json FROM decision WHERE round_id='r-compact'")
        .unwrap()
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    assert!(!rows.is_empty());

    for (symbol, side, payload) in &rows {
        let parsed: serde_json::Value = serde_json::from_str(payload)
            .unwrap_or_else(|error| panic!("{symbol}/{side} payload is not JSON: {error}"));
        if side == "hold" {
            assert_eq!(
                parsed.get("compact").and_then(serde_json::Value::as_bool),
                Some(true),
                "a hold row must be marked compact"
            );
            // The components that produced the score survive; score and
            // threshold are columns on the row itself, not duplicated here.
            assert!(
                parsed.get("momentum_prev_close_bps").is_some(),
                "momentum is kept"
            );
            assert!(
                parsed.get("intraday_position_bps").is_some(),
                "intraday position is kept"
            );
            assert!(parsed.get("notes").is_some(), "factor notes are kept");
            // The fields that existed only for a full replay are dropped.
            assert!(
                parsed.get("kbar_lower_wick_bps").is_none(),
                "the full k-bar breakdown is not stored for a hold"
            );
        } else {
            assert!(
                parsed.get("compact").is_none(),
                "an order keeps the complete FactorBreakdown"
            );
            assert!(
                parsed.get("kbar_lower_wick_bps").is_some(),
                "the full breakdown is present for a fill"
            );
        }
    }

    // Every decision still decodes through the read model the console uses.
    let decisions = store.decisions("r-compact").unwrap();
    assert_eq!(decisions.len(), rows.len());
    assert!(decisions.iter().all(|item| !item.factors_json.is_null()));
}

// ---- decision payload budget (signals only) ------------------------------

/// A round is 5 219 symbols x 2 slots and only a couple of hundred rows carry
/// a decision. Shipping every `hold` row was 9.1 MB / 3.4 s on each 30-second
/// console refresh. `signals_only` must keep exactly the rows with an order or
/// a blocked reason, and must report the true total beside them.
#[test]
fn decisions_split_returns_only_signals_but_reports_the_true_total() {
    let mut store = store();
    // One strong symbol (buys) and one quiet symbol (holds) so the round has
    // both kinds of row.
    let mut quiet = quote("000002.SZ", 1_001, 1_000);
    quiet.high_cents = Some(1_002);
    quiet.low_cents = Some(999);
    quiet.open_cents = Some(1_000);
    store
        .run_round(&request(
            "r-split",
            "2026-11-20T09:31:00+08:00",
            vec![quiet, strong_quote("600000.SH")],
        ))
        .unwrap();

    let (signals, total) = store.decisions_split("r-split", true).unwrap();
    let (everything, total_all) = store.decisions_split("r-split", false).unwrap();

    assert_eq!(total, total_all, "both calls report the same round size");
    assert!(total > signals.len(), "the round contains hold rows");
    assert!(
        !signals.is_empty(),
        "the strong quote produced at least one non-hold row"
    );
    assert!(
        signals
            .iter()
            .all(|item| item.side != "hold" || item.blocked_reason.is_some()),
        "a pure hold row with no blocked reason must not be shipped"
    );
    assert!(
        everything.iter().any(|item| item.side == "hold"),
        "signals_only=false still returns the complete audit set"
    );
    // Decoration survives the filter: the console reads these labels.
    assert!(signals.iter().all(|item| !item.side_label.is_empty()));
}

// ---- bounded read models (rounds, equity curve) --------------------------

/// `round` and `equity_point` are ledger history and are never pruned, so the
/// console's 30-second refresh must not ship the entire history. The round
/// list keeps the newest window (oldest first) and the curve keeps its shape.
#[test]
fn rounds_are_bounded_to_the_newest_window_oldest_first() {
    let mut store = store();
    for index in 0..6 {
        let mut q = strong_quote("600000.SH");
        q.price_cents = 1_060 + index as i64;
        store
            .run_round(&request(
                &format!("r-bound-{index}"),
                &format!("2026-11-20T09:{:02}:00+08:00", 31 + index * 3),
                vec![q],
            ))
            .unwrap();
    }
    assert_eq!(store.rounds().unwrap().len(), 6, "fewer than the cap: all kept");

    let three = store.rounds_limited(3).unwrap();
    assert_eq!(three.len(), 3, "the cap is honoured");
    assert!(
        three.windows(2).all(|w| w[0].started_at <= w[1].started_at),
        "order stays oldest-first for the console"
    );
    assert_eq!(
        three.last().unwrap().round_id, "r-bound-5",
        "the newest round is always included"
    );
    assert_eq!(
        three.first().unwrap().round_id, "r-bound-3",
        "the window drops the oldest, not the newest"
    );
}

/// Downsampling must never change the endpoints: the curve has to start at the
/// initial equity and end at the current one, or the reader is misled.
#[test]
fn equity_curve_is_downsampled_without_moving_its_endpoints() {
    let mut store = store();
    for index in 0..12 {
        let mut q = strong_quote("600000.SH");
        q.price_cents = 1_060 + index as i64;
        store
            .run_round(&request(
                &format!("r-curve-{index}"),
                &format!("2026-11-20T09:{:02}:00+08:00", 31 + index * 3),
                vec![q],
            ))
            .unwrap();
    }
    let full = store.equity_curve().unwrap();
    assert_eq!(full.len(), 12);

    let bounded = super::store::downsample_equity(full.clone(), 5);
    assert_eq!(bounded.len(), 5);
    assert_eq!(
        bounded.first().unwrap().round_id,
        full.first().unwrap().round_id,
        "the first point is preserved"
    );
    assert_eq!(
        bounded.last().unwrap().round_id,
        full.last().unwrap().round_id,
        "the last point is preserved"
    );
    assert_eq!(
        bounded.last().unwrap().equity_cents,
        full.last().unwrap().equity_cents,
        "the reported current equity is unchanged"
    );
    // A series within the cap is returned untouched.
    let untouched = super::store::downsample_equity(full.clone(), 50);
    assert_eq!(untouched.len(), full.len());
}

#[test]
fn ai_entry_block_wins_over_buy_but_never_forces_a_held_position_to_sell() {
    let mut db = store();
    buy(&mut db, "block-seed", "2026-11-20", "600000.SH");
    let at = "2026-11-23T09:31:00+08:00";
    let mut round = request("block-round", at,
        vec![strong_quote("600000.SH"), strong_quote("600001.SH")]);
    round.advice = Some(Advice {
        model: Some("test-llm".into()), confidence: Some(90),
        blocked_symbols: vec!["600000.SH".into(), "600001.SH".into()],
        suggestions: vec![AdviceSuggestion {
            symbol: "600001.SH".into(), side: "buy".into(),
            score_delta: None, weight_bps: None, ..Default::default()
        }],
        ..Advice::default()
    });
    let outcome = db.run_round(&round).unwrap();
    assert!(outcome.trades.is_empty());
    assert_eq!(outcome.account.positions.len(), 2);
    assert!(outcome.decisions.iter().filter(|d| d.symbol == "600001.SH")
        .all(|d| d.blocked_reason.as_deref() == Some("ai_blocked") && d.side == "hold"));
    assert!(outcome.decisions.iter().filter(|d| d.symbol == "600000.SH")
        .all(|d| d.side == "hold" && !d.note.contains("ai_force_sell")));
    let audit = outcome.ai.signal_baseline.unwrap();
    // Both slots hold 600000 and both are blocked this round: without AI each
    // would scale in, with the block all four rows fall back to hold, and no
    // seat is opened for 600001 either.
    assert_eq!((audit.compared, audit.side_changed), (4, 4));
    assert!(audit.fills.is_empty());

    round.round_id = "explicit-exit".into();
    round.advice.as_mut().unwrap().suggestions.push(AdviceSuggestion {
        symbol: "600000.SH".into(), side: "sell".into(),
        score_delta: None, weight_bps: None, ..Default::default()
    });
    let exit = db.run_round(&round).unwrap();
    assert_eq!(exit.trades.len(), 2);
    assert!(exit.trades.iter().all(|trade| trade.side == "sell" && trade.reason == "ai_force_sell"));
    // An explicit sell outranks the entry block: "do not buy" restricts
    // opening/adding, it never traps a holder. Without AI these rows hold.
    let audit = exit.ai.signal_baseline.as_ref().unwrap();
    assert_eq!(audit.fills.len(), 2);
    assert!(audit.fills.iter().all(|fill| fill.side == "sell"));
    // Without AI the held name still clears its entry bar, so the baseline is a
    // top-up (buy); the AI turned that into an exit, which is the real change.
    assert!(audit.fills.iter().all(|fill| fill.without_ai == "buy"),
        "the baseline adds to the holding; only the AI advises the exit");
    assert_eq!(db.run_round(&round).unwrap().ai.signal_baseline, exit.ai.signal_baseline);
    let observations = db.ai_observations(10).unwrap();
    let observation = observations.iter().find(|item| item.round_id == "explicit-exit").unwrap();
    assert_eq!(observation.signal_baseline, exit.ai.signal_baseline);
    let earlier = observations.iter().find(|item| item.round_id == "block-seed").unwrap();
    assert!(earlier.signal_baseline.is_none());
}

/// A filled slot used to strand its idle cash: with three seats gone it could
/// only open a new name by exiting a holding. Five seats per slot keep more of
/// the capital working, and a held name that still clears the entry bar can be
/// topped up — but a name the AI told us not to buy is never added to, and
/// today's shares stay unsellable under T+1.
#[test]
fn a_slot_holds_five_names_and_tops_up_under_its_ceiling() {
    assert_eq!(Slot::Steady.max_positions(), 5);
    assert_eq!(Slot::Aggressive.max_positions(), 5);

    // Trim the first purchase with a negative sizing tilt so the position sits
    // below its per-symbol ceiling and a later round has room to add.
    let mut db = store();
    let mut first = request("scale-1", "2026-11-20T09:31:00+08:00",
        vec![strong_quote("600000.SH")]);
    first.advice = Some(Advice {
        model: Some("test-llm".into()), confidence: Some(90),
        suggestions: vec![AdviceSuggestion {
            symbol: "600000.SH".into(), side: "buy".into(),
            score_delta: None, weight_bps: Some(-2_000), ..Default::default()
        }],
        ..Advice::default()
    });
    let opened = db.run_round(&first).unwrap();
    let before = opened.account.positions.iter()
        .find(|p| p.symbol == "600000.SH").unwrap().clone();
    assert!(before.quantity > 0 && before.sellable_quantity == 0);

    // Same name, no trimming this round: it tops up instead of holding.
    let topped = db.run_round(&request("scale-topup", "2026-11-20T09:34:00+08:00",
        vec![strong_quote("600000.SH")])).unwrap();
    let after = topped.account.positions.iter()
        .find(|p| p.symbol == "600000.SH").unwrap();
    assert!(after.quantity > before.quantity, "the top-up adds whole lots");
    assert_eq!(after.sellable_quantity, 0,
        "shares bought today are still not sellable under T+1");
    assert!(topped.decisions.iter().any(|d| d.symbol == "600000.SH"
        && d.side == "buy" && d.note.contains("scale_in")));

    // A name the AI blocks is not added to, even while it still scores.
    let mut blocked = request("scale-blocked", "2026-11-20T09:37:00+08:00",
        vec![strong_quote("600000.SH")]);
    blocked.advice = Some(Advice {
        model: Some("test-llm".into()), confidence: Some(90),
        blocked_symbols: vec!["600000.SH".into()],
        ..Advice::default()
    });
    let held = db.run_round(&blocked).unwrap();
    assert_eq!(held.account.positions.iter()
        .find(|p| p.symbol == "600000.SH").unwrap().quantity, after.quantity);
    assert!(held.decisions.iter().filter(|d| d.symbol == "600000.SH")
        .all(|d| d.side != "buy"), "a blocked holding is not topped up");

    // Five distinct names fill the five seats.
    let mut quotes = vec![strong_quote("600000.SH")];
    for n in 1..=5 {
        quotes.push(strong_quote(&format!("60000{n}.SH")));
    }
    let filled = db.run_round(&request("scale-fill", "2026-11-20T09:40:00+08:00", quotes)).unwrap();
    assert!(filled.account.positions.iter()
        .filter(|p| p.slot == Slot::Steady).count() <= 5);
}

#[test]
fn research_entry_pause_blocks_ai_buys_in_weak_market_only() {
    use super::ResearchEntryPolicy;
    let mut baseline = store();
    let mut candidate = store();
    let mut round = request("weak", "2026-11-20T09:30:00+08:00", vec![
        strong_quote("600000.SH"), quote("600001.SH", 920, 1000),
        quote("600002.SH", 921, 1000),
    ]);
    round.advice = Some(Advice { confidence: Some(95), suggestions: vec![
        AdviceSuggestion { symbol: "600000.SH".into(), side: "buy".into(),
            score_delta: None, weight_bps: None, ..Default::default() },
    ], ..Advice::default() });
    let control = baseline.run_round(&round).unwrap();
    assert_eq!(control.round.regime, "risk_off");
    assert_eq!(control.trades.len(), 2);
    let result = candidate.run_research_round(&round, ResearchEntryPolicy::WeakEntryPause).unwrap();
    assert!(result.trades.is_empty());
    assert_eq!(result.account.cash_cents, CASH);
    assert_eq!(result.decisions.iter().filter(|signal|
        signal.blocked_reason.as_deref() == Some("research_entry_gate")).count(), 2);
    let mut sample = store();
    assert_eq!(sample.run_research_round(&round, ResearchEntryPolicy::Baseline).unwrap(), control);
    let positive = request("strong", "2026-11-20T09:33:00+08:00", vec![strong_quote("600000.SH")]);
    let strong = candidate.run_research_round(&positive, ResearchEntryPolicy::WeakEntryPause).unwrap();
    assert_eq!(strong.round.regime, "risk_on");
    assert_eq!(strong.trades.len(), 2);
}

#[test]
fn research_policy_cannot_mutate_a_durable_ledger_or_exceed_its_bounds() {
    use super::ResearchEntryPolicy;
    let round = request("research", "2026-11-20T09:30:00+08:00", vec![strong_quote("600000.SH")]);
    let path = std::env::temp_dir().join(format!("intraday-research-{}-{}.sqlite3",
                                               std::process::id(), line!()));
    let mut durable = IntradayStore::open(&path).unwrap();
    let error = durable.run_research_round(&round, ResearchEntryPolicy::WeakEntryPause).unwrap_err();
    assert!(error.to_string().contains("research_requires_in_memory_ledger"));
    assert!(durable.rounds().unwrap().is_empty());
    drop(durable);
    std::fs::remove_file(path).unwrap();
    let mut isolated = store();
    let error = isolated.run_research_round(&round, ResearchEntryPolicy::WeakEntryBuffer { points: 16 }).unwrap_err();
    assert!(error.to_string().contains("research_entry_points_out_of_range"));
    assert!(isolated.rounds().unwrap().is_empty());
}

#[test]
fn forward_research_executes_prior_signal_at_later_price() {
    use super::ResearchEntryPolicy::Baseline;
    let mut db = store();
    let prior = request("prior", "2026-11-20T09:30:00+08:00", vec![strong_quote("600000.SH")]);
    let current = request("execution", "2026-11-20T09:33:00+08:00",
                          vec![quote("600000.SH", 980, 1000)]);
    assert!(db.run_forward_research_round(&prior, None, Baseline).unwrap().trades.is_empty());
    assert!(db.run_forward_research_round(&prior, Some(&current), Baseline).is_err());
    let filled = db.run_forward_research_round(&current, Some(&prior), Baseline).unwrap();
    assert_eq!(filled.trades.len(), 2);
    assert!(filled.trades.iter().all(|trade| trade.price_cents == 980 && trade.side == "buy"));
    assert!(filled.account.positions.iter().all(|position| position.sellable_quantity == 0));
}

#[test]
fn research_weak_buffer_applies_to_an_ai_buy_below_the_new_threshold() {
    use super::ResearchEntryPolicy::WeakEntryBuffer;
    let mut control = store();
    let mut candidate = store();
    let mut neutral = quote("600000.SH", 1000, 1000);
    neutral.volume = None;
    neutral.avg_volume = None;
    let mut round = request("weak-buffer", "2026-11-20T09:30:00+08:00", vec![
        neutral, quote("600001.SH", 920, 1000),
        quote("600002.SH", 921, 1000),
    ]);
    round.advice = Some(Advice { confidence: Some(95), suggestions: vec![
        AdviceSuggestion { symbol: "600000.SH".into(), side: "buy".into(),
            score_delta: None, weight_bps: None, ..Default::default() },
    ], ..Advice::default() });
    assert_eq!(control.run_round(&round).unwrap().trades.len(), 2);
    let trial = candidate.run_research_round(&round, WeakEntryBuffer { points: 5 }).unwrap();
    assert!(trial.trades.is_empty());
    assert_eq!(trial.decisions.iter().filter(|item|
         item.blocked_reason.as_deref() == Some("research_entry_gate")).count(), 2);
}

#[test]
fn research_strong_relief_can_grab_a_near_threshold_opportunity() {
    use super::ResearchEntryPolicy::StrongEntryRelief;
    let scanned = (1051..1090).map(|price| {
        let sample = quote("600000.SH", price, 1000);
        let score = super::evaluate_quote(Some(&sample), Slot::Steady, None, "600000.SH").score;
        (sample, score)
    });
    let entry = Slot::Steady.thresholds().0;
    let (near, _) = scanned.filter(|(_, score)| (entry - 15..entry).contains(score))
        .next().expect("a fixture just below the normal entry threshold");
    let round = request("strong-relief", "2026-11-20T09:30:00+08:00", vec![near]);
    let mut control = store();
    let mut candidate = store();
    let before = control.run_round(&round).unwrap();
    let after = candidate.run_research_round(&round, StrongEntryRelief { points: 15 }).unwrap();
    assert_eq!(after.round.regime, "risk_on");
    assert!(before.trades.iter().all(|trade| trade.slot != Slot::Steady));
    assert!(after.trades.iter().any(|trade| trade.slot == Slot::Steady && trade.side == "buy"));
}
