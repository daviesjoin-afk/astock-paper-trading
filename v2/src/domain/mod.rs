//! Pure domain: deterministic decisions, no clock, storage or network.
mod account;
mod ai_policy;
mod allocation;
mod calendar;
mod contract;
mod corporate_action;
mod cross_section;
mod execution;
mod factor;
mod factor_stats;
mod governance;
mod instrument;
mod liquidity;
mod kbars;
mod market;
mod regime;
mod session;
mod strategy;
mod valuation;

#[cfg(test)]
mod corporate_action_tests;
#[cfg(test)]
mod liquidity_tests;
#[cfg(test)]
mod rewrite_tests;
#[cfg(test)]
mod selection_tests;
#[cfg(test)]
mod session_fields_tests;
#[cfg(test)]
mod session_tests;

pub use account::*;
pub use ai_policy::*;
pub use allocation::*;
pub use calendar::*;
pub use contract::*;
pub use corporate_action::*;
pub use cross_section::*;
pub use execution::*;
pub use factor::*;
pub use factor_stats::*;
pub use governance::*;
pub use instrument::*;
pub use liquidity::*;
pub use kbars::*;
pub use market::*;
pub use regime::*;
pub use session::*;
pub use strategy::*;
pub use valuation::*;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuleError(pub String);

impl std::fmt::Display for RuleError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for RuleError {}
