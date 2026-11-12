//! Bounded capital adjustment between steady and aggressive.

use super::{RuleError, TOTAL_BPS};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Allocation {
    pub steady_bps: i64,
    pub aggressive_bps: i64,
    pub reason: String,
}

pub fn propose(
    current_steady_bps: i64,
    steady_score_bps: i64,
    aggressive_score_bps: i64,
) -> Result<Allocation, RuleError> {
    if !(2_000..=8_000).contains(&current_steady_bps) {
        return Err(RuleError("invalid_current_weight".into()));
    }
    let difference = aggressive_score_bps - steady_score_bps;
    // The score is in basis points, so the difference can be used directly.
    // Require at least one percentage point of relative edge before moving
    // money; otherwise ordinary daily noise creates 0.02%-0.28% transfers
    // that cost a ledger row but do not change the allocation meaningfully.
    let step = if difference.abs() < 100 { 0 } else { difference };
    let step = step.clamp(-1_000, 1_000);
    let steady = (current_steady_bps - step).clamp(2_000, 8_000);
    let aggressive = TOTAL_BPS - steady;
    let reason = if step == 0 {
        "评分接近，保持当前分配".to_string()
    } else if step > 0 {
        format!(
            "激进策略相对评分高 {difference} bps，上调其权重 {} bps",
            step
        )
    } else {
        format!(
            "稳健策略相对评分高 {} bps，上调其权重 {} bps",
            -difference, -step
        )
    };
    Ok(Allocation {
        steady_bps: steady,
        aggressive_bps: aggressive,
        reason,
    })
}

/// A deterministic daily performance score input. Both values must come from
/// one reviewed equity point; the caller never substitutes a preliminary or
/// estimated mark.
pub fn score(return_bps: i64, drawdown_bps: i64) -> i64 {
    return_bps - drawdown_bps
}
