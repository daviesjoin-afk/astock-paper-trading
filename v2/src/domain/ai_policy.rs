//! How an AI observation is allowed to change a trading day.
//!
//! The observer lives outside the domain (it talks to a model), so the domain
//! only receives a plain, already-clamped record. This module is that record
//! plus the one function that applies it, which keeps the rules testable
//! without a network and keeps the domain free of IO.
//!
//! The bounds are enforced twice on purpose: the Python observer clamps before
//! sending, and this module clamps again on receipt. A single clamp is only
//! safe if every caller is trusted, and the whole point of an advisory input is
//! that it is not.

use serde::{Deserialize, Serialize};

use super::SlotPolicy;

/// One scored name, as the observer sees it.
///
/// Every field is something the engine already used when it decided: the score,
/// the reason code, and the three session-shape numbers. Nothing forward-looking
/// is included, so advice cannot be hindsight.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ScoredCandidate {
    pub slot: String,
    pub symbol: String,
    pub score: i64,
    pub reason: String,
    pub klow_bps: Option<i64>,
    pub klen_bps: Option<i64>,
    pub kmid_bps: Option<i64>,
}

/// Caps applied to anything arriving from the observer.
pub const MAX_ENTRY_DELTA: i64 = 15;
pub const MAX_WEIGHT_DELTA_BPS: i64 = 1_000;
pub const MAX_POSITION_DELTA: i64 = 1;
pub const MAX_BLOCKED: usize = 3;

/// What the observer advised, after clamping.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AiAdjustment {
    pub bias: String,
    pub entry_delta: i64,
    pub weight_delta_bps: i64,
    pub position_delta: i64,
    #[serde(default)]
    pub blocked_symbols: Vec<String>,
    pub confidence: i64,
    pub reason: String,
    pub source: String,
    pub model: String,
    #[serde(default)]
    pub clamped: Vec<String>,
}

impl AiAdjustment {
    /// An adjustment that changes nothing.
    pub fn neutral(reason: &str) -> Self {
        Self {
            bias: "unclear".into(),
            entry_delta: 0,
            weight_delta_bps: 0,
            position_delta: 0,
            blocked_symbols: Vec::new(),
            confidence: 0,
            reason: reason.into(),
            source: "none".into(),
            model: String::new(),
            clamped: Vec::new(),
        }
    }

    /// The record as JSON, for an API response.
    ///
    /// Exists so the caller can see exactly what the kernel accepted, rather
    /// than assuming its own submission survived intact.
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "bias": self.bias,
            "entry_delta": self.entry_delta,
            "weight_delta_bps": self.weight_delta_bps,
            "position_delta": self.position_delta,
            "blocked_symbols": self.blocked_symbols,
            "confidence": self.confidence,
            "reason": self.reason,
            "source": self.source,
            "model": self.model,
            "clamped": self.clamped,
            "is_neutral": self.is_neutral(),
        })
    }

    pub fn is_neutral(&self) -> bool {
        self.entry_delta == 0
            && self.weight_delta_bps == 0
            && self.position_delta == 0
            && self.blocked_symbols.is_empty()
    }

    /// Re-clamp on receipt and report what had to be reduced.
    ///
    /// Returns the bounded adjustment plus the list of reductions, so a caller
    /// that received a doctored record can still write down that it arrived
    /// out of bounds.
    pub fn bounded(mut self) -> (Self, Vec<String>) {
        let mut notes = Vec::new();
        let tighten = |value: i64, low: i64, high: i64, name: &str, notes: &mut Vec<String>| {
            if value < low || value > high {
                notes.push(format!("{name}:{value}->{}", value.clamp(low, high)));
                value.clamp(low, high)
            } else {
                value
            }
        };
        self.entry_delta = tighten(self.entry_delta, -10, MAX_ENTRY_DELTA, "entry_delta", &mut notes);
        self.weight_delta_bps = tighten(
            self.weight_delta_bps,
            -MAX_WEIGHT_DELTA_BPS,
            MAX_WEIGHT_DELTA_BPS,
            "weight_delta_bps",
            &mut notes,
        );
        self.position_delta = tighten(
            self.position_delta,
            -MAX_POSITION_DELTA,
            MAX_POSITION_DELTA,
            "position_delta",
            &mut notes,
        );
        if self.blocked_symbols.len() > MAX_BLOCKED {
            notes.push(format!(
                "blocked_symbols:{}->{MAX_BLOCKED}",
                self.blocked_symbols.len()
            ));
            self.blocked_symbols.truncate(MAX_BLOCKED);
        }
        // Confidence below the floor means nothing moves. Enforced here too so
        // a hand-written or replayed record cannot bypass the Python guard.
        if self.confidence < 50 && !self.is_neutral() {
            notes.push("below_confidence_floor".into());
            self.entry_delta = 0;
            self.weight_delta_bps = 0;
            self.position_delta = 0;
            self.blocked_symbols.clear();
        }
        self.clamped.extend(notes.iter().cloned());
        (self, notes)
    }
}

/// Apply an adjustment to a slot's policy for one day.
///
/// Only two knobs move: the entry threshold and the position count. Both are
/// bounded, and neither can invert the strategy: the entry bar can rise but
/// never fall below the exit band, and the position count cannot go to zero.
///
/// The weight shift is returned rather than applied, because moving budget
/// between slots is a rebalance decision with its own accounting and must not
/// be smuggled in here.
pub fn apply_to_policy(policy: &mut SlotPolicy, adjustment: &AiAdjustment) -> i64 {
    if adjustment.confidence < 50 {
        return 0;
    }
    let before = policy.entry_score;
    let target = before + adjustment.entry_delta;
    // Never below the exit band: an entry bar under the exit threshold would
    // make every position an immediate exit the next day.
    policy.entry_score = target.clamp(super::EXIT_SCORE + 1, before + MAX_ENTRY_DELTA);

    let positions = policy.max_positions as i64 + adjustment.position_delta;
    policy.max_positions = positions.clamp(1, (policy.max_positions as i64 + 1).max(1)) as usize;

    adjustment.weight_delta_bps
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::StrategyId;

    fn adjustment(entry: i64, weight: i64, positions: i64, confidence: i64) -> AiAdjustment {
        AiAdjustment {
            bias: "bearish".into(),
            entry_delta: entry,
            weight_delta_bps: weight,
            position_delta: positions,
            blocked_symbols: Vec::new(),
            confidence,
            reason: "test".into(),
            source: "model".into(),
            model: "m".into(),
            clamped: Vec::new(),
        }
    }

    #[test]
    fn a_neutral_adjustment_changes_no_policy_field() {
        let mut policy = SlotPolicy::for_strategy(StrategyId::Aggressive);
        let before = (policy.entry_score, policy.max_positions);
        let moved = apply_to_policy(&mut policy, &AiAdjustment::neutral("none"));
        assert_eq!((policy.entry_score, policy.max_positions), before);
        assert_eq!(moved, 0);
    }

    #[test]
    fn a_confident_bearish_read_raises_the_entry_bar() {
        let mut policy = SlotPolicy::for_strategy(StrategyId::Aggressive);
        let before = policy.entry_score;
        apply_to_policy(&mut policy, &adjustment(10, 0, 0, 80));
        assert_eq!(policy.entry_score, before + 10);
    }

    #[test]
    fn the_entry_bar_can_never_fall_below_the_exit_band() {
        // -10 from a 40 entry point is 30; the exit band is -20, so this stays
        // legal, but the clamp must hold even if the input is absurd.
        let mut policy = SlotPolicy::for_strategy(StrategyId::Aggressive);
        policy.entry_score = -19;
        apply_to_policy(&mut policy, &adjustment(-10, 0, 0, 90));
        assert!(policy.entry_score > crate::domain::EXIT_SCORE);
    }

    #[test]
    fn position_count_cannot_go_to_zero_or_jump() {
        let mut policy = SlotPolicy::for_strategy(StrategyId::Aggressive);
        let before = policy.max_positions;
        apply_to_policy(&mut policy, &adjustment(0, 0, 1, 90));
        assert_eq!(policy.max_positions, before + 1);
        let mut policy = SlotPolicy::for_strategy(StrategyId::Aggressive);
        policy.max_positions = 1;
        apply_to_policy(&mut policy, &adjustment(0, 0, -1, 90));
        assert_eq!(policy.max_positions, 1, "never zero positions");
    }

    #[test]
    fn a_low_confidence_record_cannot_move_anything_even_if_it_arrives_doctored() {
        let mut policy = SlotPolicy::for_strategy(StrategyId::Steady);
        let before = (policy.entry_score, policy.max_positions);
        let (bounded, notes) = adjustment(15, 900, 1, 10).bounded();
        assert_eq!(notes.iter().filter(|n| *n == "below_confidence_floor").count(), 1);
        assert!(bounded.is_neutral());
        apply_to_policy(&mut policy, &bounded);
        assert_eq!((policy.entry_score, policy.max_positions), before);
    }

    #[test]
    fn out_of_bounds_values_are_reduced_and_reported() {
        let (bounded, notes) = adjustment(999, 99_999, 9, 90).bounded();
        assert_eq!(bounded.entry_delta, MAX_ENTRY_DELTA);
        assert_eq!(bounded.weight_delta_bps, MAX_WEIGHT_DELTA_BPS);
        assert_eq!(bounded.position_delta, MAX_POSITION_DELTA);
        assert_eq!(notes.len(), 3);
    }

    #[test]
    fn blocked_symbols_are_capped() {
        let mut record = adjustment(0, 0, 0, 90);
        record.blocked_symbols = vec!["a".into(), "b".into(), "c".into(), "d".into()];
        let (bounded, notes) = record.bounded();
        assert_eq!(bounded.blocked_symbols.len(), MAX_BLOCKED);
        assert!(notes.iter().any(|n| n.starts_with("blocked_symbols")));
    }
}
