//! V2 pure domain: no data source or persistence access.
//! Group by *one business responsibility*, not by function length.
mod contract;
mod digest;
mod calendar;
mod instrument;
mod execution;
mod paper;
mod portfolio;
mod valuation;
mod allocation;
mod exposure;
mod evolution;

pub use contract::*;
pub use digest::*;
pub use calendar::*;
pub use instrument::*;
pub use execution::*;
pub use paper::*;
pub use portfolio::*;
pub use valuation::*;
pub use allocation::*;
pub use exposure::*;
pub use evolution::*;

#[cfg(test)]
mod tests;
