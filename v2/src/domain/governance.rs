//! Promotion governance for research candidates.
//!
//! The dangerous transition in any self-improving system is "a candidate
//! becomes live". Everything here exists to make that transition:
//!
//!   * **explicit** — a named state change with recorded evidence, never a
//!     side effect of a good backtest;
//!   * **refusable** — thresholds can say no, and a refusal is a normal,
//!     recorded outcome rather than an error to work around;
//!   * **reversible** — a promoted candidate can be rolled back, and the
//!     rollback is recorded with the same care as the promotion.
//!
//! Nothing in this module touches a funded slot. A candidate can reach
//! `Active` in an isolated research namespace; making the real account trade
//! it is a separate, deliberate act that this module cannot perform.

use super::RuleError;

/// Where a candidate sits in its lifecycle.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateState {
    /// Proposed, not yet traded even on paper.
    Candidate,
    /// Trading in the isolated shadow book, accumulating evidence.
    Shadow,
    /// Enough evidence to be eligible for promotion. Still not live.
    Eligible,
    /// Promoted inside an isolated research namespace.
    Active,
    /// Refused at some gate. Terminal until re-proposed with new evidence.
    Rejected,
    /// Was active, then withdrawn.
    RolledBack,
}

impl CandidateState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Candidate => "candidate",
            Self::Shadow => "shadow",
            Self::Eligible => "eligible",
            Self::Active => "active",
            Self::Rejected => "rejected",
            Self::RolledBack => "rolled_back",
        }
    }

    /// The only transitions the governance allows.
    ///
    /// `Active -> RolledBack` and anything -> `Rejected` are deliberately
    /// allowed from several places: a review that refuses a candidate must be
    /// possible at any gate, and withdrawing a live candidate must always be
    /// possible.
    pub fn can_move_to(self, next: CandidateState) -> bool {
        use CandidateState::*;
        match (self, next) {
            // Forward path, one gate at a time.
            (Candidate, Shadow)
            | (Shadow, Eligible)
            | (Eligible, Active) => true,
            // A review may refuse at any gate.
            (Candidate | Shadow | Eligible, Rejected) => true,
            // Withdrawing something live must always be possible.
            (Active, RolledBack) => true,
            // Re-proposing is allowed after a refusal or a rollback, but it
            // starts from the bottom: evidence from the failed attempt cannot
            // be inherited.
            (Rejected | RolledBack, Candidate) => true,
            _ => false,
        }
    }
}

/// What the evidence says, in the units the gates are stated in.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Evidence {
    /// Forward shadow days actually observed. Not backtest days.
    pub shadow_days: i64,
    /// RankIC of the candidate's signal against forward returns, in bps.
    pub rank_ic_bps: i64,
    /// IR, or `None` when fewer than two periods were measured.
    pub ir_bps: Option<i64>,
    /// Whether any of the window overlapped parameters already tuned on it.
    pub tuned_on_same_window: bool,
    /// Whether the shadow run completed without a rule violation.
    pub ran_clean: bool,
}

/// The gate a candidate must clear, stated up front.
///
/// Explicit constants rather than "we'll know it when we see it": a threshold
/// that lives only in a reviewer's head cannot be audited, and cannot refuse
/// anything.
pub const MIN_SHADOW_DAYS: i64 = 20;
pub const MIN_RANK_IC_BPS: i64 = 200;
pub const MIN_IR_BPS: i64 = 300;

/// The verdict for one transition attempt.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Verdict {
    pub from: CandidateState,
    pub to: CandidateState,
    pub allowed: bool,
    /// Human-readable reasons. Present for refusals; may be present for
    /// approvals as a record of what was checked.
    pub reasons: Vec<String>,
    /// Which gates failed, so a report can say "how far off" it was.
    pub shortfalls: Vec<Shortfall>,
}

/// One gate that was not met, with the distance to it.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Shortfall {
    pub gate: &'static str,
    pub required: i64,
    pub actual: i64,
}

/// Decide whether a candidate may move, and say why.
///
/// The refusals are the product here: a system that can only say yes is not a
/// governance system.
pub fn review(
    from: CandidateState,
    to: CandidateState,
    evidence: &Evidence,
) -> Result<Verdict, RuleError> {
    if !from.can_move_to(to) {
        return Err(RuleError(format!(
            "illegal_transition:{}->{}",
            from.as_str(),
            to.as_str()
        )));
    }
    let mut reasons = Vec::new();
    let mut shortfalls = Vec::new();

    // A shadow run that broke a rule cannot become evidence, no matter how
    // good the numbers look.
    if !evidence.ran_clean && matches!(to, CandidateState::Eligible | CandidateState::Active) {
        reasons.push("shadow run did not complete cleanly".into());
        shortfalls.push(Shortfall {
            gate: "ran_clean",
            required: 1,
            actual: 0,
        });
    }

    // Leaving shadow requires real forward days in the shadow book.
    if to == CandidateState::Eligible && evidence.shadow_days < MIN_SHADOW_DAYS {
        reasons.push(format!(
            "shadow days {} < required {}",
            evidence.shadow_days, MIN_SHADOW_DAYS
        ));
        shortfalls.push(Shortfall {
            gate: "shadow_days",
            required: MIN_SHADOW_DAYS,
            actual: evidence.shadow_days,
        });
    }

    // Promotion requires a signal that actually sorts.
    if to == CandidateState::Active {
        if evidence.rank_ic_bps < MIN_RANK_IC_BPS {
            reasons.push(format!(
                "rank IC {} bps < required {}",
                evidence.rank_ic_bps, MIN_RANK_IC_BPS
            ));
            shortfalls.push(Shortfall {
                gate: "rank_ic_bps",
                required: MIN_RANK_IC_BPS,
                actual: evidence.rank_ic_bps,
            });
        }
        match evidence.ir_bps {
            Some(ir) if ir >= MIN_IR_BPS => {}
            Some(ir) => {
                reasons.push(format!("IR {} bps < required {}", ir, MIN_IR_BPS));
                shortfalls.push(Shortfall {
                    gate: "ir_bps",
                    required: MIN_IR_BPS,
                    actual: ir,
                });
            }
            None => {
                // An undefined IR is not a pass: stability was never measured.
                reasons.push("IR undefined: fewer than two measured periods".into());
                shortfalls.push(Shortfall {
                    gate: "ir_bps",
                    required: MIN_IR_BPS,
                    actual: 0,
                });
            }
        }
        // The single most common way a backtest lies: the parameters were
        // chosen on the same window used to judge them.
        if evidence.tuned_on_same_window {
            reasons.push("parameters were tuned on the same window used to judge them".into());
            shortfalls.push(Shortfall {
                gate: "out_of_sample",
                required: 1,
                actual: 0,
            });
        }
    }

    let allowed = shortfalls.is_empty();
    if allowed && to == CandidateState::Rejected {
        reasons.push("refused by review".into());
    }
    Ok(Verdict {
        from,
        to,
        allowed,
        reasons,
        shortfalls,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use CandidateState::*;

    fn good() -> Evidence {
        Evidence {
            shadow_days: 40,
            rank_ic_bps: 500,
            ir_bps: Some(600),
            tuned_on_same_window: false,
            ran_clean: true,
        }
    }

    #[test]
    fn the_only_forward_path_is_one_gate_at_a_time() {
        assert!(Candidate.can_move_to(Shadow));
        assert!(Shadow.can_move_to(Eligible));
        assert!(Eligible.can_move_to(Active));
        // No skipping: a candidate cannot jump straight to live.
        assert!(!Candidate.can_move_to(Active));
        assert!(!Candidate.can_move_to(Eligible));
        assert!(!Shadow.can_move_to(Active));
        // And nothing moves backwards along the forward path.
        assert!(!Active.can_move_to(Eligible));
        assert!(!Eligible.can_move_to(Shadow));
    }

    #[test]
    fn a_review_can_refuse_at_any_gate_and_withdrawal_is_always_possible() {
        for state in [Candidate, Shadow, Eligible] {
            assert!(state.can_move_to(Rejected), "{state:?} must be refusable");
        }
        assert!(Active.can_move_to(RolledBack));
        // A refusal is terminal until re-proposed from the bottom.
        assert!(Rejected.can_move_to(Candidate));
        assert!(!Rejected.can_move_to(Eligible));
        assert!(RolledBack.can_move_to(Candidate));
        assert!(!RolledBack.can_move_to(Active));
    }

    #[test]
    fn an_illegal_transition_is_an_error_not_a_verdict() {
        assert_eq!(
            review(Candidate, Active, &good()).unwrap_err().0,
            "illegal_transition:candidate->active"
        );
    }

    #[test]
    fn promotion_needs_forward_days_before_eligibility() {
        let thin = Evidence {
            shadow_days: 5,
            ..good()
        };
        let verdict = review(Shadow, Eligible, &thin).unwrap();
        assert!(!verdict.allowed);
        assert_eq!(
            verdict.shortfalls[0],
            Shortfall {
                gate: "shadow_days",
                required: MIN_SHADOW_DAYS,
                actual: 5
            }
        );

        let ok = review(Shadow, Eligible, &good()).unwrap();
        assert!(ok.allowed);
    }

    #[test]
    fn promotion_refuses_a_weak_signal_and_says_how_far_off_it_is() {
        let weak = Evidence {
            rank_ic_bps: 50,
            ..good()
        };
        let verdict = review(Eligible, Active, &weak).unwrap();
        assert!(!verdict.allowed);
        assert!(verdict
            .shortfalls
            .iter()
            .any(|s| s.gate == "rank_ic_bps" && s.required == MIN_RANK_IC_BPS && s.actual == 50));
        // The reason is stated in words, not only as a number.
        assert!(verdict.reasons.iter().any(|r| r.contains("rank IC")));
    }

    #[test]
    fn an_undefined_ir_is_a_refusal_not_a_pass() {
        let undefined = Evidence {
            ir_bps: None,
            ..good()
        };
        let verdict = review(Eligible, Active, &undefined).unwrap();
        assert!(!verdict.allowed);
        assert!(verdict.shortfalls.iter().any(|s| s.gate == "ir_bps"));
        assert!(verdict.reasons.iter().any(|r| r.contains("fewer than two")));
    }

    #[test]
    fn tuning_on_the_judging_window_blocks_promotion() {
        let overfit = Evidence {
            tuned_on_same_window: true,
            ..good()
        };
        let verdict = review(Eligible, Active, &overfit).unwrap();
        assert!(!verdict.allowed);
        assert_eq!(verdict.shortfalls[0].gate, "out_of_sample");
    }

    #[test]
    fn a_dirty_shadow_run_cannot_become_evidence() {
        let dirty = Evidence {
            ran_clean: false,
            ..good()
        };
        for target in [Eligible, Active] {
            let from = if target == Eligible { Shadow } else { Eligible };
            let verdict = review(from, target, &dirty).unwrap();
            assert!(!verdict.allowed, "{target:?} accepted a dirty run");
            assert!(verdict.shortfalls.iter().any(|s| s.gate == "ran_clean"));
        }
    }

    #[test]
    fn a_refusal_is_a_normal_recorded_outcome() {
        // Refusing is not an error: it produces a verdict the caller stores.
        let verdict = review(Eligible, Rejected, &good()).unwrap();
        assert!(verdict.allowed);
        assert!(!verdict.reasons.is_empty());
        assert_eq!(verdict.from, Eligible);
        assert_eq!(verdict.to, Rejected);
    }

    #[test]
    fn good_evidence_is_approved_and_the_verdict_says_what_was_checked() {
        let verdict = review(Eligible, Active, &good()).unwrap();
        assert!(verdict.allowed);
        assert!(verdict.shortfalls.is_empty());
    }
}
