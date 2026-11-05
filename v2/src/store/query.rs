//! Read-only views and deterministic replay of immutable ledger inputs.
use super::*;
use std::collections::BTreeMap;

impl Store {
    pub fn state(&self, asof_day: Option<&str>) -> StoreResult<PortfolioView> {
        if let Some(day) = asof_day {
            chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
                .map_err(|_| StoreError::Rules(RuleError("invalid_asof_day".into())))?;
        }
        let state = load_state(&self.conn, self.initial_cash_cents)?;
        let mut quantities = BTreeMap::<String, i64>::new();
        for lot in &state.lots {
            *quantities.entry(lot.symbol.clone()).or_default() += lot.quantity;
        }
        let positions = quantities.into_iter().map(|(symbol, quantity)| PositionView {
            sellable_quantity: asof_day.map(|day| state.sellable_quantity(&symbol, day)),
            symbol, quantity,
        }).collect();
        let runs_total = self.conn.query_row(
            "SELECT COUNT(*) FROM v2_simulation_runs", [], |row| row.get(0)
        )?;
        Ok(PortfolioView {
            initial_cash_cents: self.initial_cash_cents,
            cash_cents: state.cash_cents, asof_day: asof_day.map(str::to_owned),
            positions, lots: state.lots, runs_total,
        })
    }

    pub fn runs(&self, limit: i64) -> StoreResult<Vec<RunRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id,request_json,outcome_json FROM v2_simulation_runs
             ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map([limit.clamp(1, 1000)], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?))
        })?;
        let mut runs = Vec::new();
        for row in rows {
            let (id, req, outcome) = row?;
            runs.push(RunRecord {
                id, request: serde_json::from_str(&req)?,
                outcome: serde_json::from_str(&outcome)?, duplicate: false,
            });
        }
        Ok(runs)
    }

    /// Re-run every stored decision from pinned inputs, comparing persisted facts.
    pub fn verify_replay(&self) -> StoreResult<ReplayReport> {
        let mut state = PortfolioState::new(self.initial_cash_cents);
        let mut runs = self.conn.prepare(
            "SELECT id,request_json,outcome_json FROM v2_simulation_runs ORDER BY id"
        )?;
        let rows = runs.query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?))
        })?;
        let mut run_count = 0;
        let mut fill_count = 0;
        for row in rows {
            let (id, req_json, result_json) = row?;
            let req: SimulationRequest = serde_json::from_str(&req_json)?;
            let recorded: Outcome = serde_json::from_str(&result_json)?;
            let calculated = decide(&state, &req)?;
            if calculated != recorded {
                return Err(StoreError::Integrity(
                    format!("replay_outcome_mismatch_at_run_{id}")
                ));
            }
            let stored_fill: Option<String> = self.conn.query_row(
                "SELECT fill_json FROM v2_fills WHERE run_id=?",
                [id], |row| row.get(0),
            ).optional()?;
            match (calculated.fill.as_ref(), stored_fill) {
                (None, None) => {}
                (Some(fill), Some(raw)) => {
                    let saved: Fill = serde_json::from_str(&raw)?;
                    if *fill != saved {
                        return Err(StoreError::Integrity(
                            format!("replay_fill_mismatch_at_run_{id}")
                        ));
                    }
                    state.apply(fill)?;
                    fill_count += 1;
                }
                _ => return Err(StoreError::Integrity(
                    format!("missing_or_unexpected_fill_at_run_{id}")
                )),
            };
            run_count += 1;
        }
        let persisted = load_state(&self.conn, self.initial_cash_cents)?;
        if state != persisted {
            return Err(StoreError::Integrity("replay_portfolio_mismatch".into()));
        }
        Ok(ReplayReport {
            ok: true, runs_verified: run_count, fills_verified: fill_count,
            cash_cents: state.cash_cents,
        })
    }
}

