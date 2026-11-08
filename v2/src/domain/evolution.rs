//! Evolution governance: promotion and rollback rules over *evidence metrics*.
//!
//! This layer never sees orders or prices. Callers must supply out-of-sample and
//! shadow metrics that were produced by the research/shadow pipeline; the rules
//! then decide promote / reject / revert with explicit reasons. No single lucky
//! backtest can promote a candidate: minimum days, minimum trades, drawdown cap
//! and a margin over the incumbent are all required.

use serde::{Deserialize, Serialize};

use crate::domain::{canonical_digest, RuleError};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvolutionPolicy {
    pub min_out_of_sample_days: usize,
    pub min_shadow_days: usize,
    pub min_trades: usize,
    pub max_drawdown_bps: i64,
    pub promote_margin_bps: i64,
    pub rollback_drawdown_bps: i64,
}

impl Default for EvolutionPolicy {
    fn default() -> Self {
        Self {
            min_out_of_sample_days: 20,
            min_shadow_days: 10,
            min_trades: 10,
            max_drawdown_bps: 1_500,
            promote_margin_bps: 50,
            rollback_drawdown_bps: 2_000,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CandidateMetrics {
    pub candidate_id: String,
    pub out_of_sample_days: usize,
    pub shadow_days: usize,
    pub trades: usize,
    pub return_bps: i64,
    pub drawdown_bps: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvolutionDecision {
    pub candidate_id: String,
    /// `promoted`, `rejected`, `insufficient_evidence`, `reverted` or `kept`.
    pub status: String,
    pub reasons: Vec<String>,
    pub digest: String,
}

fn seal(decision: EvolutionDecision) -> Result<EvolutionDecision, RuleError> {
    let mut value = serde_json::to_value(&decision)
        .map_err(|_| RuleError("evolution_encoding_failed".into()))?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    let mut sealed = decision;
    sealed.digest = canonical_digest(&value);
    Ok(sealed)
}

pub fn decide_promotion(
    incumbent: &CandidateMetrics,
    candidate: &CandidateMetrics,
    policy: &EvolutionPolicy,
) -> Result<EvolutionDecision, RuleError> {
    if policy.max_drawdown_bps < 0
        || policy.rollback_drawdown_bps < 0
        || policy.promote_margin_bps < 0
    {
        return Err(RuleError("invalid_evolution_policy".into()));
    }
    if candidate.candidate_id.trim().is_empty() || incumbent.candidate_id.trim().is_empty() {
        return Err(RuleError("invalid_evolution_candidate".into()));
    }
    let mut reasons = Vec::new();
    if candidate.out_of_sample_days < policy.min_out_of_sample_days
        || candidate.shadow_days < policy.min_shadow_days
        || candidate.trades < policy.min_trades
    {
        reasons.push(format!(
            "样本不足：样本外 {} 天 / 影子 {} 天 / 成交 {} 笔（要求 ≥ {}/{}/{}）",
            candidate.out_of_sample_days,
            candidate.shadow_days,
            candidate.trades,
            policy.min_out_of_sample_days,
            policy.min_shadow_days,
            policy.min_trades
        ));
        return seal(EvolutionDecision {
            candidate_id: candidate.candidate_id.clone(),
            status: "insufficient_evidence".into(),
            reasons,
            digest: String::new(),
        });
    }
    if candidate.drawdown_bps > policy.max_drawdown_bps {
        reasons.push(format!(
            "风险恶化：最大回撤 {} bps 超过上限 {} bps",
            candidate.drawdown_bps, policy.max_drawdown_bps
        ));
    }
    if candidate.return_bps < incumbent.return_bps + policy.promote_margin_bps {
        reasons.push(format!(
            "优势不足：收益 {} bps 未超过基准 {} + 门槛 {}",
            candidate.return_bps, incumbent.return_bps, policy.promote_margin_bps
        ));
    }
    let status = if reasons.is_empty() {
        reasons.push(format!(
            "通过：收益 {} bps 超过基准 {} + {}，回撤 {} bps 在 {} 以内",
            candidate.return_bps,
            incumbent.return_bps,
            policy.promote_margin_bps,
            candidate.drawdown_bps,
            policy.max_drawdown_bps
        ));
        "promoted".to_string()
    } else {
        "rejected".to_string()
    };
    seal(EvolutionDecision {
        candidate_id: candidate.candidate_id.clone(),
        status,
        reasons,
        digest: String::new(),
    })
}

/// Post-promotion monitoring: revert when the promoted version's risk or edge
/// deteriorates beyond the policy.
pub fn decide_rollback(
    promoted: &CandidateMetrics,
    current: &CandidateMetrics,
    policy: &EvolutionPolicy,
) -> Result<EvolutionDecision, RuleError> {
    let mut reasons = Vec::new();
    if current.drawdown_bps > policy.rollback_drawdown_bps {
        reasons.push(format!(
            "回撤恶化：当前 {} bps 超过回退阈值 {} bps",
            current.drawdown_bps, policy.rollback_drawdown_bps
        ));
    }
    if current.return_bps + policy.promote_margin_bps < promoted.return_bps {
        reasons.push(format!(
            "优势消失：当前收益 {} bps 低于晋级时 {} 减去门槛 {}",
            current.return_bps, promoted.return_bps, policy.promote_margin_bps
        ));
    }
    let status = if reasons.is_empty() {
        reasons.push("保持：风险与优势均在阈值内".into());
        "kept".to_string()
    } else {
        "reverted".to_string()
    };
    seal(EvolutionDecision {
        candidate_id: current.candidate_id.clone(),
        status,
        reasons,
        digest: String::new(),
    })
}
