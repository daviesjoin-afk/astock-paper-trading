//! V2 pure domain: no data source or persistence access.
//! Group by *one business responsibility*, not by function length.
mod contract;
mod calendar;
mod instrument;
mod execution;
mod portfolio;

pub use contract::*;
pub use calendar::*;
pub use instrument::*;
pub use execution::*;
pub use portfolio::*;

#[cfg(test)]
mod tests;
