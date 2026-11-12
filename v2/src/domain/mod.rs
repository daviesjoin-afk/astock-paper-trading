//! Pure domain: deterministic decisions, no clock, storage or network.
mod account;
mod allocation;
mod calendar;
mod contract;
mod execution;
mod instrument;
mod market;
mod regime;
mod strategy;
mod valuation;

#[cfg(test)]
mod rewrite_tests;

pub use account::*;
pub use allocation::*;
pub use calendar::*;
pub use contract::*;
pub use execution::*;
pub use instrument::*;
pub use market::*;
pub use regime::*;
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
