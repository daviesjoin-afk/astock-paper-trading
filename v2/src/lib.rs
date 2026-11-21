//! Independent V2 domain and store. Do not import or bind any V1 runtime module.
pub mod cli;
pub mod domain;
/// Independent intraday paper kernel; daily settlement remains in `store`.
pub mod intraday;
pub mod research_engine;
pub mod settings;
pub mod store;
