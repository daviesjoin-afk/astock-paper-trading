//! Capital allocation policy: turn validated per-slot performance into target
//! weights. Pure arithmetic, bounded, explainable, never silently unbounded.
//!
//! This ADR only *proposes* weights. Applying a proposal requires a separate
//! ledger event (not implemented yet), so nothing here writes money.

use serde::{Deserialize, Serialize};

use crate::domain::{canonical_digest, RuleError};

pub const TOTAL_BPS: i64 = 10_000;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AllocationPolicy {
    pub min_slot_bps: i64,
    pub max_slot_bps: i64,
    pub reserve_bps: i64,
    pub max_step_bps: i64,
    pub min_days: usize,
}

impl Default for AllocationPolicy {
    fn default() -> Self {
        Self {
            min_slot_bps: 1_500,
            max_slot_bps: 6_000,
            reserve_bps: 1_000,
            max_step_bps: 1_000,
            min_days: 2,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlotScore {
    pub slot: String,
    pub return_bps: i64,
    pub drawdown_bps: i64,
    pub score_bps: i64,
    pub days: usize,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlotRationale {
    pub slot: String,
    pub current_bps: i64,
    pub target_bps: i64,
    pub score_bps: i64,
    pub note: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AllocationProposal {
    /// `proposed`, `unchanged` or `insufficient_evidence`.
    pub status: String,
    pub reserve_bps: i64,
    pub rationale: Vec<SlotRationale>,
    pub digest: String,
}

fn adjust_to_total(mut a: i64, mut b: i64, total: i64) -> (i64, i64) {
    let diff = total - (a + b);
    // Put any rounding remainder on B; the caller re-checks bounds.
    b += diff;
    if b < 0 {
        a += b;
        b = 0;
    }
    (a, b)
}

/// Propose weights for exactly two slots. Bounds, reserve floor and the
/// per-rebalance step cap are hard; no proposal can bypass them.
pub fn propose_allocation(
    current_a_bps: i64,
    current_b_bps: i64,
    scores: &[SlotScore],
    policy: &AllocationPolicy,
) -> Result<AllocationProposal, RuleError> {
    if scores.len() != 2 {
        return Err(RuleError("allocation_requires_two_slots".into()));
    }
    if policy.min_slot_bps < 0
        || policy.max_slot_bps > TOTAL_BPS
        || policy.min_slot_bps > policy.max_slot_bps
        || policy.reserve_bps < 0
        || policy.reserve_bps > TOTAL_BPS
        || policy.max_step_bps < 0
    {
        return Err(RuleError("invalid_allocation_policy".into()));
    }
    if policy.min_slot_bps * 2 + policy.reserve_bps > TOTAL_BPS {
        return Err(RuleError("allocation_bounds_exceed_total".into()));
    }
    if current_a_bps + current_b_bps + policy.reserve_bps == 0 {
        return Err(RuleError("invalid_current_weights".into()));
    }

    let mut rationale: Vec<SlotRationale> = scores
        .iter()
        .map(|score| SlotRationale {
            slot: score.slot.clone(),
            current_bps: if score.slot == "A" { current_a_bps } else { current_b_bps },
            target_bps: 0,
            score_bps: score.score_bps,
            note: String::new(),
        })
        .collect();

    let enough = scores.iter().all(|score| score.days >= policy.min_days);
    let free = TOTAL_BPS - policy.reserve_bps;
    let (mut target_a, mut target_b);
    let status;
    if !enough {
        status = "insufficient_evidence".into();
        target_a = current_a_bps;
        target_b = current_b_bps;
        for item in &mut rationale {
            item.note = "样本天数不足，保持当前权重".into();
        }
    } else {
        // Relative scoring: only differences between slots move capital; a
        // uniformly bad book keeps an even split instead of stalling.
        let worst = scores.iter().map(|score| score.score_bps).min().unwrap_or(0);
        let raw: Vec<i64> = scores
            .iter()
            .map(|score| (score.score_bps - worst).max(0))
            .collect();
        let sum: i64 = raw.iter().sum();
        if sum == 0 {
            target_a = free / 2;
            target_b = free - target_a;
            for item in &mut rationale {
                item.note = "两槽评分相同，均分可用资金".into();
            }
        } else {
            let floor = policy.min_slot_bps;
            let distributable = free - floor * 2;
            let a_share = distributable * raw[0] / sum;
            target_a = floor + a_share;
            target_b = floor + (distributable - a_share);
            for item in &mut rationale {
                item.note = if item.score_bps > 0 {
                    format!("评分 {} bps 高于对照，获得更多预算", item.score_bps)
                } else {
                    "评分不高于对照，保留下限".into()
                };
            }
        }
        target_a = target_a.clamp(policy.min_slot_bps, policy.max_slot_bps);
        target_b = target_b.clamp(policy.min_slot_bps, policy.max_slot_bps);
        let (sum_a, sum_b) = adjust_to_total(target_a, target_b, free);
        target_a = sum_a;
        target_b = sum_b;
        // Step cap, then hand any residual back to the reserve.
        let step_a = (target_a - current_a_bps).clamp(-policy.max_step_bps, policy.max_step_bps);
        let step_b = (target_b - current_b_bps).clamp(-policy.max_step_bps, policy.max_step_bps);
        target_a = current_a_bps + step_a;
        target_b = current_b_bps + step_b;
        status = if target_a == current_a_bps && target_b == current_b_bps {
            "unchanged".to_string()
        } else {
            "proposed".to_string()
        };
    }

    let reserve = TOTAL_BPS - target_a - target_b;
    if reserve < policy.reserve_bps {
        return Err(RuleError("allocation_breaches_reserve_floor".into()));
    }
    for item in &mut rationale {
        item.target_bps = if item.slot == "A" { target_a } else { target_b };
    }
    let mut proposal = AllocationProposal {
        status,
        reserve_bps: reserve,
        rationale,
        digest: String::new(),
    };
    let mut value = serde_json::to_value(&proposal)
        .map_err(|_| RuleError("allocation_encoding_failed".into()))?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    proposal.digest = canonical_digest(&value);
    Ok(proposal)
}
