//! Human-facing Chinese labels for live read models.
//!
//! This module is the single owner of the translation. It is pure and has no
//! I/O: audit values (`note`, `regime`, `side`, ...) are kept verbatim by their
//! stores, and only the read models add the `*_label` fields computed here.
//! Unknown keys are never dropped or guessed.

/// Market regime as a short sentence. Unknown values stay "未知".
pub fn regime_label(regime: &str) -> String {
    match regime {
        "risk_on" => "市场偏强",
        "risk_off" => "市场偏弱",
        "mixed" => "涨跌互现",
        _ => "未知",
    }
    .to_string()
}

/// AI participation as a short sentence. `ai_error` is kept raw for auditing.
pub fn ai_label(ai_ok: bool, ai_error: Option<&str>) -> String {
    if ai_ok {
        return "AI 已参与".to_string();
    }
    match ai_error {
        Some(error) if error.contains("below_confidence_floor") => {
            "AI 信心不足，本轮未干预"
        }
        Some(error) if error.contains("no_advice") => "AI 本轮未给建议",
        _ => "AI 异常",
    }
    .to_string()
}

/// Translate each semicolon-separated audit note item in place, preserving
/// order. Known `key=value` pairs become sentences; unknown items are kept
/// verbatim so no audit information is lost.
pub fn note_label(note: &str) -> String {
    note.split(';')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(note_item_label)
        .filter(|item| !item.is_empty())
        .collect::<Vec<_>>()
        .join("；")
}

fn note_item_label(item: &str) -> String {
    let (key, raw_value) = match item.split_once('=') {
        Some((key, value)) => (key.trim(), Some(value.trim())),
        None => (item, None),
    };
    let number = |value: Option<&str>| value.and_then(|value| value.parse::<i64>().ok());
    // Only keys whose meaning and value are fully represented get a sentence.
    // Anything else (notably `ai_fail_closed=<error>`, caller text or a future
    // audit key) stays verbatim so the human label never hides audit evidence.
    // Items that describe our own plumbing rather than the market are dropped
    // from the human sentence. The raw `note` field still carries them for
    // audit, so nothing is lost -- it just is not shown next to the numbers.
    let internal_plumbing = item.contains("live_paper_loop transport");
    if internal_plumbing {
        return String::new();
    }

    match (key, raw_value) {
        ("partial_trailing_history", value) if number(value).is_some() => {
            format!("{} 只带历史趋势", number(value).unwrap())
        }
        ("snapshot_only", value) if number(value).is_some() => {
            format!("{} 只新股无历史", number(value).unwrap())
        }
        ("explicit_trailing_closes", value) if number(value).is_some() => {
            format!("{} 只带完整历史趋势", number(value).unwrap())
        }
        ("no_trailing_history_supplied", None) => "未收到历史趋势数据".to_string(),
        ("ai_applied", Some("1")) => "AI 建议已生效".to_string(),
        // Fail-closed reasons, in words a reader can act on.
        ("ai_fail_closed", Some("no_advice")) => "本轮没有 AI 建议".to_string(),
        ("ai_fail_closed", Some("below_confidence_floor")) => {
            "AI 信心不足，没有干预".to_string()
        }
        // Unknown failure codes stay verbatim: they name a real fault and a
        // human label must not paper over which one it was.
        ("quotes", value) if number(value).is_some() => {
            format!("本轮扫描 {} 只股票", number(value).unwrap())
        }
        _ => item.to_string(),
    }
}

/// Trade side as a short sentence; other values stay verbatim.
pub fn trade_side_label(side: &str) -> String {
    match side {
        "buy" => "买入",
        "sell" => "卖出",
        _ => side,
    }
    .to_string()
}

/// Fill reason as a short sentence; other values stay verbatim.
pub fn trade_reason_label(reason: &str) -> String {
    match reason {
        "score_entry" => "评分达标买入",
        "stop_or_take_profit" => "止损或止盈卖出",
        "score_exit" => "评分转弱卖出",
        "ai_force_sell" => "AI 要求卖出",
        "ai_force_buy" => "AI 建议买入",
        "scale_in" => "加仓（评分仍达标）",
        "t_plus_one" => "今日买入，明日才能卖",
        _ => reason,
    }
    .to_string()
}

/// Decision side as a short sentence, including "观望".
pub fn decision_side_label(side: &str) -> String {
    match side {
        "buy" => "买入",
        "sell" => "卖出",
        "hold" => "观望",
        _ => side,
    }
    .to_string()
}

/// Why a decision could not become an order. `None` is a neutral dash.
pub fn blocked_label(blocked_reason: Option<&str>) -> String {
    match blocked_reason {
        None => "—",
        Some("limit_up_sealed") => "已涨停，买不进",
        Some("limit_down_sealed") => "已跌停，卖不出",
        Some("t_plus_one") => "今日买入，明日才能卖",
        Some("cash_budget") => "现金不够",
        Some("ai_blocked") => "AI 建议暂缓开仓",
        Some("no_live_price") => "暂无实时价",
        Some("max_positions") => "持仓已满",
        Some("lot_too_expensive") => "一手买不起（股价过高）",
        Some("fee_exceeds_notional") => "手续费超过金额",
        Some("morning_cooldown_guard") => "早盘开盘冷静期（09:30-09:41 防冲高诱多）",
        Some("bull_trap_divergence") => "主力诱多形态拦截（破均价线/冲高长上影回落）",
        Some(other) => other,
    }
    .to_string()
}

/// Strategy slot as a short sentence; other values stay verbatim.
pub fn slot_label(slot: &str) -> String {
    match slot {
        "steady" => "稳健",
        "aggressive" => "激进",
        _ => slot,
    }
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn regime_labels_cover_strong_weak_mixed_and_unknown() {
        assert_eq!(regime_label("risk_on"), "市场偏强");
        assert_eq!(regime_label("risk_off"), "市场偏弱");
        assert_eq!(regime_label("mixed"), "涨跌互现");
        assert_eq!(regime_label("no_tradable_breadth"), "未知");
    }

    #[test]
    fn ai_labels_distinguish_participation_from_fail_closed_reasons() {
        assert_eq!(ai_label(true, None), "AI 已参与");
        assert_eq!(
            ai_label(false, Some("below_confidence_floor;missing_confidence")),
            "AI 信心不足，本轮未干预"
        );
        assert_eq!(ai_label(false, Some("no_advice")), "AI 本轮未给建议");
        assert_eq!(ai_label(false, Some("raw_advice_unparseable")), "AI 异常");
        assert_eq!(ai_label(false, None), "AI 异常");
    }

    #[test]
    fn note_label_translates_each_key_keeps_unknowns_and_order() {
        assert_eq!(
            note_label("partial_trailing_history=10436; snapshot_only=2"),
            "10436 只带历史趋势；2 只新股无历史"
        );
        assert_eq!(note_label("ai_applied=1"), "AI 建议已生效");
        assert_eq!(note_label("quotes=5219"), "本轮扫描 5219 只股票");
        assert_eq!(
            note_label("quotes=2; a_future_audit_key=7; ops_note; ai_fail_closed=bad_json"),
            "本轮扫描 2 只股票；a_future_audit_key=7；ops_note；ai_fail_closed=bad_json"
        );
        assert_eq!(note_label(""), "");
        // A caller's own note is audit text and survives verbatim.
        assert_eq!(note_label("manual check by operator"), "manual check by operator");
        // ...but our own plumbing is not shown beside the numbers. The raw
        // `note` column still records it for audit.
        assert_eq!(note_label("live_paper_loop transport with raw LLM advice"), "");
        assert_eq!(
            note_label("quotes=5219; live_paper_loop transport with raw LLM advice"),
            "本轮扫描 5219 只股票"
        );
    }

    #[test]
    fn trade_labels_cover_all_known_sides_and_reasons() {
        assert_eq!(trade_side_label("buy"), "买入");
        assert_eq!(trade_side_label("sell"), "卖出");
        assert_eq!(trade_side_label("other"), "other");
        for (reason, expected) in [
            ("score_entry", "评分达标买入"),
            ("stop_or_take_profit", "止损或止盈卖出"),
            ("score_exit", "评分转弱卖出"),
            ("ai_force_sell", "AI 要求卖出"),
            ("ai_force_buy", "AI 建议买入"),
            ("t_plus_one", "今日买入，明日才能卖"),
        ] {
            assert_eq!(trade_reason_label(reason), expected);
        }
        assert_eq!(trade_reason_label("new_reason"), "new_reason");
    }

    #[test]
    fn decision_labels_cover_buy_sell_hold_blocks_and_slots() {
        assert_eq!(decision_side_label("buy"), "买入");
        assert_eq!(decision_side_label("sell"), "卖出");
        assert_eq!(decision_side_label("hold"), "观望");
        assert_eq!(decision_side_label("other"), "other");
        assert_eq!(blocked_label(None), "—");
        for (reason, expected) in [
            ("limit_up_sealed", "已涨停，买不进"),
            ("limit_down_sealed", "已跌停，卖不出"),
            ("t_plus_one", "今日买入，明日才能卖"),
            ("cash_budget", "现金不够"),
            ("no_live_price", "暂无实时价"),
            ("max_positions", "持仓已满"),
        ] {
            assert_eq!(blocked_label(Some(reason)), expected);
        }
        assert_eq!(blocked_label(Some("future_reason")), "future_reason");
        assert_eq!(slot_label("steady"), "稳健");
        assert_eq!(slot_label("aggressive"), "激进");
        assert_eq!(slot_label("other"), "other");
    }
}
