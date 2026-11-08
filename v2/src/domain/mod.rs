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

pub use contract::*;
pub use digest::*;
pub use calendar::*;
pub use instrument::*;
pub use execution::*;
pub use paper::*;
pub use portfolio::*;
pub use valuation::*;

#[cfg(test)]
mod tests;
