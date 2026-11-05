//! Read-only views and deterministic replay of immutable ledger inputs.
use super::*;
use std::collections::BTreeMap;

impl Store {
    pub fn state(&self, asof_day: Option<&str>) -> StoreResult<PortfolioView> {
        // A dated view is the end of that China (+08:00) day. Apply only
        // persisted fills belonging to decisions on/before it; never re-decide.
        let (state, runs_total) = if let Some(day) = asof_day {
            let cutoff = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
                .map_err(|_| StoreError::Rules(RuleError("invalid_asof_day".into())))?;
            if cutoff.to_string() != day {
                return Err(StoreError::Rules(RuleError("invalid_asof_day".into())));
            }
            let mut state = PortfolioState::new(self.initial_cash_cents);
            let mut count = 0;
            // One SELECT gives the projection and count the same SQLite read
            // snapshot, including rejected decisions with no fill.
            let mut stmt = self.conn.prepare(
                "SELECT r.request_json,f.fill_json FROM v2_simulation_runs r
                 LEFT JOIN v2_fills f ON f.run_id=r.id ORDER BY r.id"
            )?;
            let rows = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
            })?;
            let china = chrono::FixedOffset::east_opt(8 * 3600).expect("valid China offset");
            for row in rows {
                let (raw_request, raw_fill) = row?;
                let request: SimulationRequest = serde_json::from_str(&raw_request)?;
                let at = chrono::DateTime::parse_from_rfc3339(&request.decision_at)
                    .map_err(|_| StoreError::Integrity("stored_decision_time_invalid".into()))?;
                if at.with_timezone(&china).date_naive() > cutoff { continue; }
                count += 1;
                if let Some(raw) = raw_fill {
                    state.apply(&serde_json::from_str::<Fill>(&raw)?)?;
                }
            }
            (state, count)
        } else {
            let state = load_state(&self.conn, self.initial_cash_cents)?;
            let count = self.conn.query_row(
                "SELECT COUNT(*) FROM v2_simulation_runs", [], |row| row.get(0)
            )?;
            (state, count)
        };
        let mut quantities = BTreeMap::<String, i64>::new();
        for lot in &state.lots {
            *quantities.entry(lot.symbol.clone()).or_default() += lot.quantity;
        }
        let positions = quantities.into_iter().map(|(symbol, quantity)| PositionView {
            sellable_quantity: asof_day.map(|day| state.sellable_quantity(&symbol, day)),
            symbol, quantity,
        }).collect();
        Ok(PortfolioView {
            initial_cash_cents: self.initial_cash_cents,
            cash_cents: state.cash_cents, asof_day: asof_day.map(str::to_owned),
            positions, lots: state.lots, runs_total,
        })
    }

    pub fn runs(&self, limit: i64) -> StoreResult<Vec<RunRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id,request_json,outcome_json,evidence_json,calendar_version,session_policy_version FROM v2_simulation_runs
             ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map([limit.clamp(1, 1000)], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?, row.get::<_, String>(3)?, row.get::<_, String>(4)?, row.get::<_, String>(5)?))
        })?;
        let mut runs = Vec::new();
        for row in rows {
            let (id, req, outcome, evidence, calendar_version, session_policy_version) = row?;
            runs.push(RunRecord {
                id, request: serde_json::from_str(&req)?,
                evidence: serde_json::from_str(&evidence)?,
                calendar_version, session_policy_version,
                outcome: serde_json::from_str(&outcome)?, duplicate: false,
            });
        }
        Ok(runs)
    }

    /// Re-run every stored decision from pinned inputs, comparing persisted facts.
    pub fn verify_replay(&self) -> StoreResult<ReplayReport> {
        let (calendar, sessions) = load_market_context(&self.conn)?;
        let mut state = PortfolioState::new(self.initial_cash_cents);
        let mut runs = self.conn.prepare(
            "SELECT id,request_json,outcome_json,evidence_json,calendar_version,session_policy_version FROM v2_simulation_runs ORDER BY id"
        )?;
        let rows = runs.query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?, row.get::<_, String>(3)?, row.get::<_, String>(4)?, row.get::<_, String>(5)?))
        })?;
        let mut run_count = 0;
        let mut fill_count = 0;
        for row in rows {
            let (id, req_json, result_json, evidence_json, calendar_version, session_policy_version) = row?;
            verify_market_versions(&calendar, &sessions, &calendar_version, &session_policy_version, id)?;
            let req: SimulationRequest = serde_json::from_str(&req_json)?;
            let recorded: Outcome = serde_json::from_str(&result_json)?;
            let evidence: Option<QuoteEvidence> = serde_json::from_str(&evidence_json)?;
            // Missing evidence is a pinned fact too: later ingestion cannot
            // turn an already-recorded rejection into a historical fill.
            if evidence.is_some() && load_evidence(&self.conn, &req.quote_ref)? != evidence {
                return Err(StoreError::Integrity(format!("replay_evidence_mismatch_at_run_{id}")));
            }
            let calculated = decide(&state, &req, evidence.as_ref(), &calendar, &sessions)?;
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

