//! Deterministic intraday factors, decision signals and AI clamping.
//!
//! The transport layer may send real-time quotes and raw LLM output. It never
//! supplies a threshold, confidence floor, score limit or ledger effect: every
//! active value is re-clamped here and all risk gates remain in `store.rs`.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::{BPS, IntradayResult, Quote, Slot, SlotBook};

pub const MIN_AI_CONFIDENCE: i64 = 50;
pub const MAX_AI_BIAS: i64 = 20;
pub const MAX_ENTRY_DELTA: i64 = 15;
pub const MAX_WEIGHT_TILT_BPS: i64 = 1_000;
pub const MAX_SUGGESTION_SCORE_DELTA: i64 = 20;
pub const MAX_SUGGESTION_WEIGHT_BPS: i64 = 2_000;
pub const MAX_ADVICE_ITEMS: usize = 8;
pub const MAX_BLOCKED_SYMBOLS: usize = 5;
pub const MAX_TEXT_CHARS: usize = 512;
pub const MAX_RAW_ADVICE_CHARS: usize = 16_000;
const MAX_FACTOR_BPS: i64 = 1_000_000;

/// Raw advisory payload. Python may transport it but cannot make any field
/// authoritative: `apply_ai_advice` re-reads and re-bounds it in Rust.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Advice {
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub confidence: Option<i64>,
    #[serde(default)]
    pub bias: Option<i64>,
    #[serde(default)]
    pub entry_delta: Option<i64>,
    #[serde(default)]
    pub weight_tilt_bps: Option<i64>,
    #[serde(default)]
    pub blocked_symbols: Vec<String>,
    #[serde(default)]
    pub suggestions: Vec<AdviceSuggestion>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub mode: Option<String>,
    /// An opaque LLM JSON object or a string containing that JSON. Rust parses
    /// it when structured fields are absent, and stores the original for audit.
    #[serde(default)]
    pub raw_advice: Option<Value>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct AdviceSuggestion {
    #[serde(default)]
    pub symbol: String,
    #[serde(default)]
    pub side: String,
    #[serde(default)]
    pub score_delta: Option<i64>,
    #[serde(default)]
    pub weight_bps: Option<i64>,
    #[serde(default)]
    pub target_weight_bps: Option<i64>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub slot: Option<String>,
}

/// A bounded suggestion. Unknown sides become inert `ignore`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClampedSuggestion {
    pub symbol: String,
    pub side: String,
    pub score_delta: i64,
    pub weight_bps: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_weight_bps: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub slot: Option<String>,
}

impl ClampedSuggestion {
    pub fn matches_slot(&self, slot: Slot) -> bool {
        match self.slot.as_deref() {
            Some("steady") => slot == Slot::Steady,
            Some("aggressive") => slot == Slot::Aggressive,
            _ => true,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SignalBaseline {
    pub compared: usize,
    pub side_changed: usize,
    pub fills: Vec<FillBaseline>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FillBaseline {
    pub slot: Slot,
    pub symbol: String,
    pub side: String,
    pub without_ai: String,
}

/// The sole AI effect for one round. `accepted=false` is neutral by construction.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AiApplication {
    pub round_id: String,
    pub model: String,
    pub raw_advice: String,
    pub accepted: bool,
    pub clamped: bool,
    pub confidence: i64,
    pub reason: String,
    pub bias: i64,
    pub entry_delta: i64,
    pub weight_tilt_bps: i64,
    pub blocked_symbols: Vec<String>,
    pub suggestions: Vec<ClampedSuggestion>,
    pub clamped_fields: Vec<String>,
    pub ai_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub signal_baseline: Option<SignalBaseline>,
    #[serde(default)]
    pub mode: String,
}

impl AiApplication {
    pub fn neutral(round_id: &str, reason: &str) -> Self {
        Self {
            round_id: round_id.to_string(),
            model: "none".into(),
            raw_advice: "null".into(),
            accepted: false,
            clamped: false,
            confidence: 0,
            reason: reason.into(),
            bias: 0,
            entry_delta: 0,
            weight_tilt_bps: 0,
            blocked_symbols: Vec::new(),
            suggestions: Vec::new(),
            clamped_fields: Vec::new(),
            ai_error: Some(reason.into()),
            signal_baseline: None,
            mode: "advisory".into(),
        }
    }

    pub fn ai_ok(&self) -> bool {
        self.accepted && self.ai_error.is_none()
    }

    pub fn is_deep(&self) -> bool {
        self.ai_ok() && self.mode == "deep"
    }

    pub fn effective_confidence(&self) -> i64 {
        if self.ai_ok() {
            self.confidence
        } else {
            0
        }
    }

    pub fn effective_bias(&self) -> i64 {
        if self.ai_ok() {
            self.bias
        } else {
            0
        }
    }

    pub fn effective_entry_delta(&self) -> i64 {
        if self.ai_ok() {
            self.entry_delta
        } else {
            0
        }
    }

    pub fn effective_weight_tilt_bps(&self) -> i64 {
        if self.ai_ok() {
            self.weight_tilt_bps
        } else {
            0
        }
    }

    pub fn active_blocked_symbols(&self) -> &[String] {
        if self.ai_ok() {
            &self.blocked_symbols
        } else {
            &[]
        }
    }

    pub fn active_suggestions(&self) -> &[ClampedSuggestion] {
        if self.ai_ok() {
            &self.suggestions
        } else {
            &[]
        }
    }
}

/// Explicit factors from one snapshot. `None` means the input did not carry
/// the fact; no zero, previous close, volume or amount is substituted for it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FactorBreakdown {
    pub momentum_prev_close_bps: Option<i64>,
    pub momentum_open_bps: Option<i64>,
    pub intraday_position_bps: Option<i64>,
    pub volume_ratio_bps: Option<i64>,
    pub amplitude_bps: Option<i64>,
    pub limit_distance_bps: Option<i64>,
    pub kbar_body_bps: Option<i64>,
    pub kbar_range_bps: Option<i64>,
    pub kbar_lower_wick_bps: Option<i64>,
    pub kbar_upper_wick_bps: Option<i64>,
    pub kbar_shift_bps: Option<i64>,
    pub trend_bps: Option<i64>,
    pub history_len: usize,
    pub used_history: bool,
    pub notes: Vec<String>,
}

impl Default for FactorBreakdown {
    fn default() -> Self {
        Self {
            momentum_prev_close_bps: None,
            momentum_open_bps: None,
            intraday_position_bps: None,
            volume_ratio_bps: None,
            amplitude_bps: None,
            limit_distance_bps: None,
            kbar_body_bps: None,
            kbar_range_bps: None,
            kbar_lower_wick_bps: None,
            kbar_upper_wick_bps: None,
            kbar_shift_bps: None,
            trend_bps: None,
            history_len: 0,
            used_history: false,
            notes: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvaluatedQuote {
    pub slot: Slot,
    pub symbol: String,
    pub score: i64,
    pub factors: FactorBreakdown,
}

/// One candidate's durable decision signal for one slot.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DecisionSignal {
    pub round_id: String,
    pub slot: Slot,
    pub symbol: String,
    pub score: i64,
    pub threshold: i64,
    pub reached_threshold: bool,
    pub side: String,
    pub blocked_reason: Option<String>,
    pub ai_bias: i64,
    pub ai_confidence: i64,
    pub ai_applied: bool,
    pub factors: FactorBreakdown,
    pub note: String,
}

/// Clamp raw advice without any I/O. `apply_ai_advice` binds it to a round;
/// `store.rs` writes the observation inside that round's single transaction.
pub fn ai_clamp(advice: Option<Advice>) -> AiApplication {
    clamp_inner("", advice)
}

/// Bind clamped advice to one round. Failure, an unparseable raw payload or a
/// confidence below the Rust floor produces a neutral application with an
/// `ai_error`; it cannot move a score, threshold, budget or order.
pub fn apply_ai_advice(round_id: &str, advice: Option<Advice>) -> AiApplication {
    clamp_inner(round_id, advice)
}

fn clamp_inner(round_id: &str, advice: Option<Advice>) -> AiApplication {
    let Some(advice) = advice else {
        return AiApplication::neutral(round_id, "no_advice");
    };

    let mut notes = Vec::new();
    let mut errors = Vec::new();
    let (raw_map, parse_error) = raw_fields(advice.raw_advice.as_ref());
    if let Some(error) = parse_error {
        errors.push(error);
    }

    let model = advice
        .model
        .as_deref()
        .or_else(|| raw_map.get("model").and_then(Value::as_str))
        .unwrap_or("unknown");
    let reason = advice
        .reason
        .as_deref()
        .or_else(|| raw_map.get("reason").and_then(Value::as_str))
        .unwrap_or("no_reason");
    let confidence_raw = advice
        .confidence
        .or_else(|| json_i64(raw_map.get("confidence")))
        .unwrap_or(0);
    if advice.confidence.is_none() && json_i64(raw_map.get("confidence")).is_none() {
        notes.push("missing_confidence".into());
    }
    let confidence = clamp_i64(confidence_raw, 0, 100, "confidence", &mut notes);

    let bias_raw = advice
        .bias
        .or_else(|| json_i64(raw_map.get("bias")))
        .unwrap_or(0);
    let entry_raw = advice
        .entry_delta
        .or_else(|| json_i64(raw_map.get("entry_delta")))
        .unwrap_or(0);
    let tilt_raw = advice
        .weight_tilt_bps
        .or_else(|| json_i64(raw_map.get("weight_tilt_bps")))
        .unwrap_or(0);

    let mut bias = clamp_i64(bias_raw, -MAX_AI_BIAS, MAX_AI_BIAS, "bias", &mut notes);
    let mut entry_delta = clamp_i64(
        entry_raw,
        -MAX_ENTRY_DELTA,
        MAX_ENTRY_DELTA,
        "entry_delta",
        &mut notes,
    );
    let mut weight_tilt_bps = clamp_i64(
        tilt_raw,
        -MAX_WEIGHT_TILT_BPS,
        MAX_WEIGHT_TILT_BPS,
        "weight_tilt_bps",
        &mut notes,
    );

    let blocked_raw = if advice.blocked_symbols.is_empty() {
        json_strings(raw_map.get("blocked_symbols"))
    } else {
        advice.blocked_symbols.clone()
    };
    let mut seen = BTreeSet::new();
    let mut blocked_symbols = Vec::new();
    for symbol in blocked_raw {
        let symbol = truncate_chars(symbol.trim(), super::MAX_SYMBOL_CHARS);
        if symbol.is_empty() || !seen.insert(symbol.clone()) {
            continue;
        }
        blocked_symbols.push(symbol);
    }
    if blocked_symbols.len() > MAX_BLOCKED_SYMBOLS {
        notes.push(format!(
            "blocked_symbols:{}->{MAX_BLOCKED_SYMBOLS}",
            blocked_symbols.len()
        ));
        blocked_symbols.truncate(MAX_BLOCKED_SYMBOLS);
    }

    let suggestions_raw = if advice.suggestions.is_empty() {
        json_suggestions(raw_map.get("suggestions"))
    } else {
        advice.suggestions.clone()
    };
    if suggestions_raw.len() > MAX_ADVICE_ITEMS {
        notes.push(format!(
            "suggestions:{}->{MAX_ADVICE_ITEMS}",
            suggestions_raw.len()
        ));
    }
    let mut suggestions = Vec::new();
    for item in suggestions_raw.into_iter().take(MAX_ADVICE_ITEMS) {
        let symbol = truncate_chars(item.symbol.trim(), super::MAX_SYMBOL_CHARS);
        if symbol.is_empty() {
            notes.push("empty_suggestion_symbol".into());
            continue;
        }
        let side = normalize_side(&item.side, &mut notes, &symbol);
        let score_delta = clamp_i64(
            item.score_delta
                .or_else(|| json_i64(raw_map.get("score_delta")))
                .unwrap_or(0),
            -MAX_SUGGESTION_SCORE_DELTA,
            MAX_SUGGESTION_SCORE_DELTA,
            &format!("suggestion_score_delta:{symbol}"),
            &mut notes,
        );
        let weight_bps = clamp_i64(
            item.weight_bps.unwrap_or(0),
            -MAX_SUGGESTION_WEIGHT_BPS,
            MAX_SUGGESTION_WEIGHT_BPS,
            &format!("suggestion_weight_bps:{symbol}"),
            &mut notes,
        );
        let target_weight_bps = item.target_weight_bps.map(|w| w.clamp(0, 10_000));
        let reason_text = item
            .reason
            .as_deref()
            .map(|r| truncate_chars(r.trim(), MAX_TEXT_CHARS))
            .filter(|r| !r.is_empty());
        let slot_filter = item
            .slot
            .as_deref()
            .map(|s| s.trim().to_ascii_lowercase())
            .filter(|s| matches!(s.as_str(), "steady" | "aggressive"));
        suggestions.push(ClampedSuggestion {
            symbol,
            side,
            score_delta,
            weight_bps,
            target_weight_bps,
            reason: reason_text,
            slot: slot_filter,
        });
    }
    let mode = match advice
        .mode
        .as_deref()
        .or_else(|| raw_map.get("mode").and_then(Value::as_str))
    {
        Some("deep") => "deep".to_string(),
        _ => "advisory".to_string(),
    };

    if confidence < MIN_AI_CONFIDENCE {
        errors.push("below_confidence_floor".into());
        notes.push("below_confidence_floor".into());
    }
    let ai_error = if errors.is_empty() {
        None
    } else {
        Some(errors.join(";"))
    };
    let accepted = ai_error.is_none();
    if !accepted {
        notes.push("fail_closed_neutral".into());
        bias = 0;
        entry_delta = 0;
        weight_tilt_bps = 0;
        blocked_symbols.clear();
        suggestions.clear();
    }

    let mut audit = advice.clone();
    audit.raw_advice = None;
    let raw_advice = match advice.raw_advice.as_ref() {
        Some(value) => serde_json::to_string(value).unwrap_or_else(|_| "\"unserializable\"".into()),
        None => serde_json::to_string(&audit).unwrap_or_else(|_| "null".into()),
    };
    let raw_advice = truncate_chars(&raw_advice, MAX_RAW_ADVICE_CHARS);
    notes.sort();
    notes.dedup();

    AiApplication {
        round_id: round_id.to_string(),
        model: truncate_chars(model, MAX_TEXT_CHARS),
        raw_advice,
        accepted,
        clamped: !notes.is_empty(),
        confidence,
        reason: truncate_chars(reason, MAX_TEXT_CHARS),
        bias,
        entry_delta,
        weight_tilt_bps,
        blocked_symbols,
        suggestions,
        clamped_fields: notes,
        signal_baseline: None,
        ai_error,
        mode,
    }
}

fn raw_fields(raw: Option<&Value>) -> (BTreeMap<String, Value>, Option<String>) {
    let Some(raw) = raw else {
        return (BTreeMap::new(), None);
    };
    let parsed = match raw {
        Value::Object(_) => Some(raw.clone()),
        Value::String(text) => serde_json::from_str::<Value>(text).ok(),
        _ => None,
    };
    match parsed {
        Some(Value::Object(map)) => (map.into_iter().collect(), None),
        _ => (BTreeMap::new(), Some("raw_advice_unparseable".into())),
    }
}

fn json_i64(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.trim().parse().ok(),
        _ => None,
    }
}

fn json_strings(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|item| item.as_str().map(str::to_string))
            .collect(),
        _ => Vec::new(),
    }
}

fn json_suggestions(value: Option<&Value>) -> Vec<AdviceSuggestion> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|item| serde_json::from_value::<AdviceSuggestion>(item.clone()).ok())
            .collect(),
        _ => Vec::new(),
    }
}

fn clamp_i64(value: i64, low: i64, high: i64, name: &str, notes: &mut Vec<String>) -> i64 {
    let bounded = value.clamp(low, high);
    if bounded != value {
        notes.push(format!("{name}:{value}->{bounded}"));
    }
    bounded
}

fn truncate_chars(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}

fn normalize_side(side: &str, notes: &mut Vec<String>, symbol: &str) -> String {
    let normalized = side.trim().to_ascii_lowercase();
    match normalized.as_str() {
        "buy" | "sell" | "block" | "avoid" => normalized,
        "" | "ignore" | "hold" => "ignore".into(),
        other => {
            notes.push(format!("suggestion_side:{symbol}:{other}->ignore"));
            "ignore".into()
        }
    }
}

fn checked_bps(numerator: i128, denominator: i128) -> Option<i64> {
    if denominator <= 0 {
        return None;
    }
    let scaled = numerator.checked_mul(BPS as i128)?;
    let rounded = if scaled >= 0 {
        (scaled + denominator / 2) / denominator
    } else {
        (scaled - denominator / 2) / denominator
    };
    i64::try_from(rounded)
        .ok()
        .map(|value| value.clamp(-MAX_FACTOR_BPS, MAX_FACTOR_BPS))
}

/// Deterministic factor extraction. `amount_cents` is audit-only and volume is
/// never interpreted as money flow. Missing facts produce `None` plus a note.
pub fn factor_breakdown(quote: &Quote) -> FactorBreakdown {
    let mut factors = FactorBreakdown::default();
    if !quote.has_live_price() {
        factors.notes.push("no_live_price".into());
        return factors;
    }
    let price = quote.price_cents as i128;
    let prev = quote.prev_close_cents as i128;
    if prev <= 0 {
        factors.notes.push("invalid_prev_close".into());
        return factors;
    }
    factors.momentum_prev_close_bps = checked_bps(price - prev, prev);

    if let Some(open) = quote.open() {
        factors.momentum_open_bps =
            checked_bps(price - open as i128, open as i128);
    } else {
        factors.notes.push("missing_open".into());
    }

    let high = quote.high().map(|value| value as i128);
    let low = quote.low().map(|value| value as i128);
    if let (Some(high), Some(low)) = (high, low) {
        let range = high - low;
        if range > 0 {
            factors.intraday_position_bps =
                checked_bps(price - low, range);
            factors.amplitude_bps =
                checked_bps(range, prev);
            if let Some(open) = quote.open() {
                let open = open as i128;
                factors.kbar_body_bps =
                    checked_bps(price - open, open);
                factors.kbar_range_bps = checked_bps(range, open);
                factors.kbar_lower_wick_bps =
                    checked_bps(open - low, open);
                factors.kbar_upper_wick_bps =
                    checked_bps(high - open, open);
                factors.kbar_shift_bps = checked_bps(2 * price - high - low, open);
            }
        } else {
            factors.notes.push("flat_or_sealed_range".into());
        }
    } else {
        factors.notes.push("missing_ohlc_range".into());
    }

    if let (Some(volume), Some(avg)) = (quote.volume(), quote.avg_volume()) {
        // `checked_bps` already scales by BPS; the caller passes the raw ratio
        // numerator. Multiplying again pushed every ratio into the
        // MAX_FACTOR_BPS clamp, pinning the volume component at full strength.
        factors.volume_ratio_bps =
            checked_bps(volume as i128, avg as i128);
        factors.notes.push("volume_used_only_for_volume_ratio".into());
    } else {
        factors.notes.push("volume_ratio_missing".into());
    }
    if quote.amount().is_some() {
        factors.notes.push("amount_audit_only".into());
    } else {
        factors.notes.push("amount_missing_audit".into());
    }

    let up = quote.effective_limit_up() as i128;
    let down = quote.effective_limit_down() as i128;
    let distance = (up - price).min(price - down).max(0);
    factors.limit_distance_bps = checked_bps(distance, prev);
    if quote.limit_up_cents.is_none() || quote.limit_down_cents.is_none() {
        factors.notes.push("limit_fallback_board".into());
    }

    factors.history_len = quote.trailing_closes.len();
    if quote.trailing_closes.len() >= 5 {
        let closes = &quote.trailing_closes;
        let first = closes[closes.len() - 5];
        let last = closes[closes.len() - 1];
        if first > 0 && last > 0 {
            factors.trend_bps = checked_bps(last as i128 - first as i128, first as i128);
            factors.used_history = true;
            factors
                .notes
                .push("explicit_trailing_closes_used".into());
        } else {
            factors.notes.push("invalid_trailing_closes".into());
        }
    } else {
        factors
            .notes
            .push("no_trailing_history; snapshot_only_factors".into());
    }
    factors.notes.sort();
    factors.notes.dedup();
    factors
}

/// Evaluate one quote for one slot, including bounded score tilt, bias and
/// per-symbol score deltas. No advice means the deterministic baseline.
pub fn evaluate_quote(
    quote: Option<&Quote>,
    slot: Slot,
    advice: Option<&AiApplication>,
    symbol: &str,
) -> EvaluatedQuote {
    let Some(quote) = quote else {
        let mut factors = FactorBreakdown::default();
        factors.notes.push("no_live_quote".into());
        return EvaluatedQuote {
            slot,
            symbol: symbol.to_string(),
            score: 0,
            factors,
        };
    };
    let mut factors = factor_breakdown(quote);
    if !quote.has_live_price() || quote.prev_close_cents <= 0 {
        factors.notes.push("score_not_computed".into());
        factors.notes.sort();
        factors.notes.dedup();
        return EvaluatedQuote {
            slot,
            symbol: symbol.to_string(),
            score: 0,
            factors,
        };
    }
    let advice = advice.filter(|item| item.ai_ok());
    let bias = advice.map(AiApplication::effective_bias).unwrap_or(0);
    let tilt = advice
        .map(AiApplication::effective_weight_tilt_bps)
        .unwrap_or(0);
    let suggestion_delta = advice
        .map(|item| {
            item.active_suggestions()
                .iter()
                .filter(|candidate| candidate.symbol == symbol)
                .fold(0i64, |sum, candidate| {
                    sum.saturating_add(candidate.score_delta)
                })
                .clamp(
                    -MAX_SUGGESTION_SCORE_DELTA,
                    MAX_SUGGESTION_SCORE_DELTA,
                )
        })
        .unwrap_or(0);
    let score = score_from_factors(slot, &factors, tilt, bias, suggestion_delta);
    EvaluatedQuote {
        slot,
        symbol: symbol.to_string(),
        score,
        factors,
    }
}

fn score_from_factors(
    slot: Slot,
    factors: &FactorBreakdown,
    tilt: i64,
    bias: i64,
    suggestion_delta: i64,
) -> i64 {
    // Missing factors contribute zero, matching `FactorBreakdown`'s contract:
    // `None` means the fact was absent and the factor is omitted, never a
    // full-strength negative component. Treating absence as worst-case would
    // penalise every symbol for facts the source never transports (e.g. there
    // is no `avg_volume` field in the snapshot feed).
    let get = |value: Option<i64>| value.unwrap_or(0);
    let momentum = get(factors.momentum_prev_close_bps);
    let open_momentum = get(factors.momentum_open_bps);
    // Centered intraday position: 0 bps = mid-range. Absent OHLC (a sealed or
    // untraded board) contributes 0 instead of the worst -20/-25 component.
    let position = factors
        .intraday_position_bps
        .map(|value| value - 5_000)
        .unwrap_or(0);
    let volume_ratio = factors
        .volume_ratio_bps
        .map(|value| value.saturating_sub(BPS))
        .unwrap_or(0);
    let amplitude = get(factors.amplitude_bps);
    let limit_distance = get(factors.limit_distance_bps);
    let shape = get(factors.kbar_shift_bps);
    let trend = get(factors.trend_bps);

    let (momentum_component, position_component, volume_component, shape_component, trend_component, penalty_component) =
        match slot {
            Slot::Steady => (
                (momentum / 25 + trend / 100).clamp(-40, 40),
                (position / 250).clamp(-20, 20),
                (volume_ratio / 1_000).clamp(-10, 10),
                (shape / 200).clamp(-10, 10),
                (trend / 100).clamp(-10, 10),
                -((amplitude.saturating_sub(300)) / 20).clamp(0, 20)
                    - ((100 - limit_distance) / 5).clamp(0, 20),
            ),
            Slot::Aggressive => (
                (momentum / 20 + open_momentum / 20).clamp(-50, 50),
                (position / 200).clamp(-25, 25),
                (volume_ratio / 500).clamp(-15, 20),
                (shape / 100).clamp(-20, 20),
                (trend / 50).clamp(-15, 15),
                -((amplitude.saturating_sub(1_000)) / 50).clamp(0, 10)
                    - ((100 - limit_distance) / 10).clamp(0, 10),
            ),
        };

    let tilt = tilt.clamp(-MAX_WEIGHT_TILT_BPS, MAX_WEIGHT_TILT_BPS);
    let momentum_weight = (BPS + tilt).clamp(0, 2 * BPS);
    let position_weight = (BPS - tilt / 2).clamp(0, 2 * BPS);
    let shape_weight = (BPS - tilt / 2).clamp(0, 2 * BPS);
    let trend_weight = (BPS + tilt / 2).clamp(0, 2 * BPS);
    let flat_weight = BPS;
    let weighted = momentum_component as i128 * momentum_weight as i128
        + position_component as i128 * position_weight as i128
        + volume_component as i128 * flat_weight as i128
        + shape_component as i128 * shape_weight as i128
        + trend_component as i128 * trend_weight as i128
        + penalty_component as i128 * flat_weight as i128;
    (weighted / BPS as i128 + bias as i128 + suggestion_delta as i128)
        .clamp(-100, 100) as i64
}

/// All candidate signals for one round, ordered by symbol then slot. Held
/// symbols without a live quote are explicit no-quote decisions, not zeros
/// silently treated as tradable.
pub fn score_candidates(
    round_id: &str,
    quotes: &[Quote],
    books: &[SlotBook; 2],
    advice: &AiApplication,
) -> Vec<DecisionSignal> {
    score_candidates_for_research(round_id, quotes, books, advice, 0, false)
}

pub(super) fn score_candidates_for_research(
    round_id: &str,
    quotes: &[Quote],
    books: &[SlotBook; 2],
    advice: &AiApplication,
    new_entry_delta: i64,
    pause_new_entries: bool,
) -> Vec<DecisionSignal> {
    let mut symbols = BTreeSet::new();
    let mut quote_by_symbol = BTreeMap::new();
    for quote in quotes {
        symbols.insert(quote.symbol.clone());
        quote_by_symbol.insert(quote.symbol.as_str(), quote);
    }
    for book in books {
        for position in &book.positions {
            if position.quantity > 0 {
                symbols.insert(position.symbol.clone());
            }
        }
    }

    let ai_active = advice.ai_ok();
    let ai_bias = advice.effective_bias();
    let ai_confidence = advice.effective_confidence();
    let blocked_symbols: BTreeSet<&str> = advice
        .active_blocked_symbols()
        .iter()
        .map(String::as_str)
        .collect();
    let mut signals = Vec::new();

    for symbol in symbols {
        let quote = quote_by_symbol.get(symbol.as_str()).copied();
        for slot in Slot::ALL {
            let book = books
                .iter()
                .find(|candidate| candidate.slot == slot)
                .expect("both slot books exist");
            let held = book
                .positions
                .iter()
                .find(|position| position.symbol == symbol && position.quantity > 0);
            let evaluated = evaluate_quote(quote, slot, Some(advice), &symbol);
            let (base_entry, exit_threshold) = slot.thresholds();
            let entry_threshold = (base_entry + advice.effective_entry_delta())
                .clamp(exit_threshold + 1, base_entry + MAX_ENTRY_DELTA);
            let threshold = if held.is_some() {
                exit_threshold
            } else {
                (entry_threshold + new_entry_delta).max(exit_threshold + 1)
            };
            let reached_threshold = if held.is_some() {
                evaluated.score <= exit_threshold
            } else {
                evaluated.score >= threshold
            };

            let mut notes = evaluated.factors.notes.clone();
            // "Do not buy this" applies to additions as well as new seats.
            let ai_blocked = blocked_symbols.contains(symbol.as_str());
            let mut side = if held.is_some() {
                let mut sell = reached_threshold;
                if let (Some(quote), Some(position)) = (quote, held)
                    && quote.has_live_price()
                    && position.avg_cost_cents > 0
                {
                    let pnl_bps = checked_bps(
                        quote.price_cents as i128 - position.avg_cost_cents as i128,
                        position.avg_cost_cents as i128,
                    )
                    .unwrap_or(0);
                    let (stop_loss, take_profit) = slot.exit_bands();
                    if pnl_bps <= stop_loss || pnl_bps >= take_profit {
                        sell = true;
                        notes.push("stop_or_take_profit".into());
                    }
                }
                if sell {
                    "sell".to_string()
                } else if !ai_blocked && evaluated.score >= entry_threshold {
                    // A held name that still clears the entry bar is a top-up,
                    // not a new seat. Order sizing and the per-symbol ceiling
                    // stay in `execute_signals`, so this cannot exceed them.
                    notes.push("scale_in".into());
                    "buy".to_string()
                } else {
                    "hold".to_string()
                }
            } else if reached_threshold && !ai_blocked {
                "buy".to_string()
            } else {
                "hold".to_string()
            };

            let mut ai_applied = ai_active
                && (advice.effective_bias() != 0
                    || advice.effective_entry_delta() != 0
                    || advice.effective_weight_tilt_bps() != 0
                    || !advice.active_blocked_symbols().is_empty()
                    || !advice.active_suggestions().is_empty());
            let matched_suggestion = advice
                .active_suggestions()
                .iter()
                .find(|item| item.symbol == symbol && item.side != "ignore" && item.matches_slot(slot));
            let suggestion_side = matched_suggestion.map(|item| item.side.as_str());
            let mom_prev = evaluated.factors.momentum_prev_close_bps.unwrap_or(0);
            let mom_open = evaluated.factors.momentum_open_bps.unwrap_or(0);
            let upper_wick = evaluated.factors.kbar_upper_wick_bps.unwrap_or(0);
            let limit_dist = evaluated.factors.limit_distance_bps.unwrap_or(1_000);
            // Compute intraday VWAP distance in bps when both volume and amount are present
            let vwap_distance_bps: Option<i64> = quote.and_then(|q| {
                let vol = q.volume()?;
                let amt = q.amount()?;
                let vwap = amt / vol;
                if vwap > 0 {
                    checked_bps(q.price_cents as i128 - vwap as i128, vwap as i128)
                } else {
                    None
                }
            });
            if let Some(vwap_bps) = vwap_distance_bps
                && vwap_bps <= -150
                && mom_prev >= 300
            {
                notes.push("vwap_breakdown_trap".into());
            }
            let chase_or_bull_trap = (mom_prev >= 650 && (mom_open <= -50 || upper_wick >= 150))
                || (slot == Slot::Steady && mom_prev >= 600 && limit_dist <= 180)
                || (mom_prev >= 350 && vwap_distance_bps.is_some_and(|v| v <= -180) && mom_open <= -50);
            if ai_active {
                // The model sometimes names a symbol it only read in the market
                // table. A sale of something not held cannot execute; record it
                // instead of letting the audit imply an order was placed.
                if held.is_none() && suggestion_side == Some("sell") {
                    notes.push("ai_sell_without_position".into());
                }
                if held.is_some() && suggestion_side == Some("sell") {
                    side = "sell".into();
                    notes.push("ai_force_sell".into());
                    ai_applied = true;
                } else if !ai_blocked
                    && suggestion_side == Some("buy")
                    && (held.is_none() || advice.is_deep())
                {
                    if chase_or_bull_trap {
                        notes.push("ai_chase_guarded".into());
                    } else {
                        if held.is_some() && side != "buy" {
                            notes.push("scale_in".into());
                        }
                        side = "buy".into();
                        notes.push("ai_force_buy".into());
                        ai_applied = true;
                    }
                }
                if let Some(reason_note) = matched_suggestion.and_then(|s| s.reason.as_deref()) {
                    notes.push(format!("ai_reason:{reason_note}"));
                }
            }

            let mut blocked_reason = match quote {
                None => Some("no_live_quote".into()),
                Some(item) if !item.has_live_price() => {
                    notes.push("prev_close_never_used_as_price".into());
                    Some("no_live_price".into())
                }
                Some(item) => item
                    .validate()
                    .err()
                    .map(|error| format!("invalid_quote:{error}")),
            };
            if blocked_reason.is_none() {
                if held.is_some() && quote.is_some_and(|item| !item.can_sell()) {
                    blocked_reason = Some("limit_down_sealed".into());
                } else if held.is_none() && quote.is_some_and(|item| !item.can_buy()) {
                    blocked_reason = Some("limit_up_sealed".into());
                } else if held.is_none()
                    && ai_active
                    && (ai_blocked || matches!(suggestion_side, Some("block" | "avoid")))
                {
                    blocked_reason = Some("ai_blocked".into());
                    ai_applied = true;
                } else if held.is_none()
                    && side == "buy"
                    && (mom_prev >= 650 && mom_open <= -100 && upper_wick >= 180
                        || (mom_prev >= 450 && vwap_distance_bps.is_some_and(|v| v <= -220) && mom_open <= -80))
                {
                    blocked_reason = Some("bull_trap_divergence".into());
                }
            }
            let deep_force_buy = advice.is_deep() && suggestion_side == Some("buy");
            if blocked_reason.is_none()
                && held.is_none()
                && side == "buy"
                && !deep_force_buy
                && (pause_new_entries || (new_entry_delta > 0 && !reached_threshold))
            {
                blocked_reason = Some("research_entry_gate".into());
            }
            if blocked_reason.is_some() {
                side = "hold".into();
            }
            notes.sort();
            notes.dedup();

            signals.push(DecisionSignal {
                round_id: round_id.to_string(),
                slot,
                symbol: symbol.clone(),
                score: evaluated.score,
                threshold,
                reached_threshold,
                side,
                blocked_reason,
                ai_bias,
                ai_confidence,
                ai_applied,
                factors: evaluated.factors,
                note: notes.join("; "),
            });
        }
    }
    signals
}

/// Human-readable bounded proposal input, used only for evolution validation.
pub fn proposal_json(profit_bps: i64, drawdown_bps: i64, round_count: usize) -> Value {
    let entry_delta = if profit_bps < -200 {
        5
    } else if profit_bps > 300 {
        -2
    } else {
        0
    };
    let weight_tilt_bps = if drawdown_bps > 300 {
        -200
    } else if profit_bps > 200 {
        100
    } else {
        0
    };
    let max_positions_delta = if round_count >= 8 && profit_bps > 0 {
        1
    } else if profit_bps < -200 {
        -1
    } else {
        0
    };
    let cash_floor_delta_bps = if drawdown_bps > 200 { 200 } else { 0 };
    json!({
        "entry_delta": entry_delta,
        "weight_tilt_bps": weight_tilt_bps,
        "max_positions_delta": max_positions_delta,
        "cash_floor_delta_bps": cash_floor_delta_bps,
        "scope": "proposal_only_not_applied",
        "basis": "persisted_round_equity",
    })
}

pub fn proposal_within_bounds(proposal: &Value) -> bool {
    let entry = proposal.get("entry_delta").and_then(Value::as_i64).unwrap_or(999);
    let tilt = proposal
        .get("weight_tilt_bps")
        .and_then(Value::as_i64)
        .unwrap_or(999);
    let positions = proposal
        .get("max_positions_delta")
        .and_then(Value::as_i64)
        .unwrap_or(999);
    let floor = proposal
        .get("cash_floor_delta_bps")
        .and_then(Value::as_i64)
        .unwrap_or(999);
    (-5..=5).contains(&entry)
        && (-200..=200).contains(&tilt)
        && (-1..=1).contains(&positions)
        && (0..=200).contains(&floor)
        && (entry != 0 || tilt != 0 || positions != 0 || floor != 0)
}

/// An explicit assertion helper for HTTP callers that want a validation error
/// before opening a transaction. Kept here so decision bounds have one owner.
pub fn validate_advice_shape(advice: &Advice) -> IntradayResult<()> {
    if advice.suggestions.len() > MAX_ADVICE_ITEMS * 10
        || advice.blocked_symbols.len() > MAX_BLOCKED_SYMBOLS * 10
    {
        return Err(super::IntradayError::Rules("advice_payload_too_large".into()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn low_confidence_advice_is_neutral_even_when_values_are_extreme() {
        let advice = Advice {
            confidence: Some(10),
            bias: Some(999),
            entry_delta: Some(-999),
            weight_tilt_bps: Some(99_999),
            blocked_symbols: vec!["600000.SH".into()],
            suggestions: vec![AdviceSuggestion {
                symbol: "600000.SH".into(),
                side: "buy".into(),
                score_delta: Some(999),
                weight_bps: Some(999),
                ..AdviceSuggestion::default()
            }],
            ..Advice::default()
        };
        let applied = apply_ai_advice("r1", Some(advice));
        assert!(!applied.accepted);
        assert_eq!(applied.bias, 0);
        assert_eq!(applied.entry_delta, 0);
        assert_eq!(applied.weight_tilt_bps, 0);
        assert!(applied.blocked_symbols.is_empty());
        assert!(applied.suggestions.is_empty());
        assert!(applied.ai_error.unwrap().contains("below_confidence_floor"));
    }

    #[test]
    fn raw_llm_json_is_bounded_by_rust_without_python_policy() {
        let advice = Advice {
            raw_advice: Some(json!({
                "model": "remote-llm",
                "confidence": 95,
                "bias": 1000,
                "entry_delta": -1000,
                "weight_tilt_bps": 5000,
                "blocked_symbols": ["A", "B", "C", "D", "E", "F"],
                "suggestions": [
                    {"symbol": "600000.SH", "side": "buy", "score_delta": 1000, "weight_bps": 9999}
                ],
                "reason": "transport only"
            })),
            ..Advice::default()
        };
        let applied = apply_ai_advice("r2", Some(advice));
        assert!(applied.ai_ok());
        assert_eq!(applied.confidence, 95);
        assert_eq!(applied.bias, MAX_AI_BIAS);
        assert_eq!(applied.entry_delta, -MAX_ENTRY_DELTA);
        assert_eq!(applied.weight_tilt_bps, MAX_WEIGHT_TILT_BPS);
        assert_eq!(applied.blocked_symbols.len(), MAX_BLOCKED_SYMBOLS);
        assert_eq!(applied.suggestions[0].score_delta, MAX_SUGGESTION_SCORE_DELTA);
        assert!(applied.clamped_fields.iter().any(|note| note.starts_with("bias:")));
    }

    #[test]
    fn missing_history_uses_only_snapshot_factors() {
        let quote = Quote {
            symbol: "600000.SH".into(),
            name: None,
            price_cents: 1_030,
            prev_close_cents: 1_000,
            open_cents: Some(1_010),
            high_cents: Some(1_040),
            low_cents: Some(1_000),
            limit_up_cents: Some(1_100),
            limit_down_cents: Some(900),
            volume: Some(110_000),
            amount_cents: Some(113_300_000),
            avg_volume: Some(100_000),
            trailing_closes: Vec::new(),
            tradable: Some(true),
        };
        let factors = factor_breakdown(&quote);
        assert!(factors.trend_bps.is_none());
        assert!(factors.used_history == false);
        assert!(factors.notes.iter().any(|note| note.contains("no_trailing_history")));
        assert!(factors.notes.iter().any(|note| note == "amount_audit_only"));
        assert!(factors.momentum_prev_close_bps.is_some());
        assert!(factors.intraday_position_bps.is_some());
        assert!(factors.kbar_shift_bps.is_some());
    }
}
