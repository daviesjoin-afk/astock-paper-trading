//! Read-only views and deterministic replay of immutable ledger inputs.
use super::*;
use std::collections::BTreeMap;

impl Store {
    /// Read-only catalogue of registered experiments for the picker.
    ///
    /// Stable ordering by `cycle_id`, clamped limit, no writes and no derived
    /// metrics: only facts the ledger already stores.
    pub fn experiments(&self, limit: i64) -> StoreResult<ExperimentCatalogue> {
        let total: i64 = self.conn.query_row("SELECT COUNT(*) FROM v2_experiments", [], |row| row.get(0))?;
        let mut statement = self.conn.prepare(
            "SELECT e.cycle_id, e.status, e.spec_json,
                    (SELECT COUNT(*) FROM v2_simulation_runs r WHERE r.cycle_id = e.cycle_id)
             FROM v2_experiments e ORDER BY e.cycle_id LIMIT ?"
        )?;
        let rows = statement.query_map([limit.clamp(1, 500)], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })?;
        let mut experiments = Vec::new();
        let mut skipped = 0usize;
        for row in rows {
            let (cycle_id, status, raw_spec, runs_total) = row?;
            let parsed = serde_json::from_str::<ExperimentSpec>(&raw_spec)
                .ok()
                .filter(|spec| spec.cycle_id == cycle_id)
                .and_then(|spec| match status.as_str() {
                    "active" => Some((spec, ExperimentStatus::Active)),
                    "completed" => Some((spec, ExperimentStatus::Completed)),
                    _ => None,
                });
            let Some((spec, status)) = parsed else {
                // One unreadable row must not take the whole picker down.
                eprintln!("v2 catalogue: skipping unreadable experiment row {cycle_id}");
                skipped += 1;
                continue;
            };
            experiments.push(ExperimentSummary {
                cycle_id,
                status,
                strategy_version_id: spec.strategy.strategy_version_id,
                research_input_id: spec.strategy.research_input_id,
                market_data_pack_id: spec.market_data_pack_id,
                execution_policy_version: spec.execution_policy_version,
                initial_cash_cents: spec.initial_cash_cents,
                runs_total,
            });
        }
        Ok(ExperimentCatalogue { experiments, total, skipped })
    }

    pub fn market_data_pack(&self, pack_id: &str) -> StoreResult<MarketDataPack> {
        load_market_pack(&self.conn, pack_id)
    }

    pub fn experiment(&self, cycle_id: &str) -> StoreResult<Experiment> {
        load_experiment(&self.conn, cycle_id)
    }

    pub fn state(&self, cycle_id: &str, asof_day: Option<&str>) -> StoreResult<PortfolioView> {
        let experiment = load_experiment(&self.conn, cycle_id)?;
        // A dated view is the end of that China (+08:00) day. Apply only
        // persisted fills belonging to decisions on/before it; never re-decide.
        let (state, runs_total) = if let Some(day) = asof_day {
            let cutoff = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
                .map_err(|_| StoreError::Rules(RuleError("invalid_asof_day".into())))?;
            if cutoff.to_string() != day {
                return Err(StoreError::Rules(RuleError("invalid_asof_day".into())));
            }
            let mut state = PortfolioState::new(experiment.spec.initial_cash_cents);
            let mut count = 0;
            // One SELECT gives the projection and count the same SQLite read
            // snapshot, including rejected decisions with no fill.
            let mut stmt = self.conn.prepare(
                "SELECT r.request_json,f.fill_json FROM v2_simulation_runs r
                 LEFT JOIN v2_fills f ON f.run_id=r.id WHERE r.cycle_id=? ORDER BY r.id"
            )?;
            let rows = stmt.query_map([cycle_id], |row| {
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
            let state = load_state(&self.conn, &experiment.spec)?;
            let count = self.conn.query_row(
                "SELECT COUNT(*) FROM v2_simulation_runs WHERE cycle_id=?", [cycle_id], |row| row.get(0)
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
            cycle_id: cycle_id.into(),
            initial_cash_cents: experiment.spec.initial_cash_cents,
            cash_cents: state.cash_cents, asof_day: asof_day.map(str::to_owned),
            positions, lots: state.lots, runs_total,
        })
    }

    pub fn runs(&self, cycle_id: &str, limit: i64) -> StoreResult<Vec<RunRecord>> {
        load_experiment(&self.conn, cycle_id)?;
        let mut stmt = self.conn.prepare(
            "SELECT id,request_json,outcome_json,evidence_json,calendar_version,session_policy_version,instrument_version FROM v2_simulation_runs
             WHERE cycle_id=? ORDER BY id DESC LIMIT ?"
        )?;
        let rows = stmt.query_map(params![cycle_id, limit.clamp(1, 1000)], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?, row.get::<_, String>(3)?, row.get::<_, String>(4)?, row.get::<_, String>(5)?, row.get::<_, String>(6)?))
        })?;
        let mut runs = Vec::new();
        for row in rows {
            let (id, req, outcome, evidence, calendar_version, session_policy_version, instrument_version) = row?;
            runs.push(RunRecord {
                id, request: serde_json::from_str(&req)?,
                evidence: serde_json::from_str(&evidence)?,
                calendar_version, session_policy_version, instrument_version,
                outcome: serde_json::from_str(&outcome)?, duplicate: false,
            });
        }
        Ok(runs)
    }

    /// Re-run every stored decision from pinned inputs, comparing persisted facts.
    pub fn verify_replay(&self, cycle_id: &str) -> StoreResult<ReplayReport> {
        let experiment = load_experiment(&self.conn, cycle_id)?;
        let (calendar, sessions, instruments) = load_market_context(&self.conn, &experiment.spec.market_data_pack_id)?;
        let mut state = PortfolioState::new(experiment.spec.initial_cash_cents);
        let mut runs = self.conn.prepare(
            "SELECT id,request_json,outcome_json,evidence_json,calendar_version,session_policy_version,instrument_version FROM v2_simulation_runs WHERE cycle_id=? ORDER BY id"
        )?;
        let rows = runs.query_map([cycle_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?,
                row.get::<_, String>(2)?, row.get::<_, String>(3)?, row.get::<_, String>(4)?, row.get::<_, String>(5)?, row.get::<_, String>(6)?))
        })?;
        let mut previous_at = None;
        let mut run_count = 0;
        let mut fill_count = 0;
        for row in rows {
            let (id, req_json, result_json, evidence_json, calendar_version, session_policy_version, instrument_version) = row?;
            verify_market_versions(&calendar, &sessions, &instruments, &calendar_version, &session_policy_version, &instrument_version, id)?;
            let req: SimulationRequest = serde_json::from_str(&req_json)?;
            if req.quote_ref.market_data_pack_id != experiment.spec.market_data_pack_id || req.cycle_id != cycle_id || req.strategy_version_id != experiment.spec.strategy.strategy_version_id {
                return Err(StoreError::Integrity("replay_experiment_ownership_mismatch".into()));
            }
            let at = chrono::DateTime::parse_from_rfc3339(&req.decision_at)
                .map_err(|_| StoreError::Integrity("stored_decision_time_invalid".into()))?;
            if previous_at.is_some_and(|previous| at < previous) {
                return Err(StoreError::Integrity("replay_decision_order_mismatch".into()));
            }
            previous_at = Some(at);
            let recorded: Outcome = serde_json::from_str(&result_json)?;
            let evidence: Option<QuoteEvidence> = serde_json::from_str(&evidence_json)?;
            if let Some(pinned) = &evidence { verify_instrument_identity(&instruments, pinned)?; }
            // Missing evidence is a pinned fact too: later ingestion cannot
            // turn an already-recorded rejection into a historical fill.
            if evidence.is_some() && load_evidence(&self.conn, &req.quote_ref)? != evidence {
                return Err(StoreError::Integrity(format!("replay_evidence_mismatch_at_run_{id}")));
            }
            let calculated = decide(&state, &req, evidence.as_ref(), &calendar, &sessions, &instruments)?;
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
        let persisted = load_state(&self.conn, &experiment.spec)?;
        if state != persisted {
            return Err(StoreError::Integrity("replay_portfolio_mismatch".into()));
        }
        Ok(ReplayReport {
            cycle_id: cycle_id.into(),
            ok: true, runs_verified: run_count, fills_verified: fill_count,
            cash_cents: state.cash_cents,
        })
    }

    /// Prove that this experiment's stored decisions are exactly the reviewed
    /// research signals for its frozen strategy definition and frozen input.
    ///
    /// Replay answers "do the trading facts follow from the pinned inputs".
    /// This answers "are the research signals the ones the fixed definition and
    /// the fixed input must produce". Neither substitutes for the other.
    pub fn verify_research_reproduction(&self, cycle_id: &str) -> StoreResult<ResearchReproductionReport> {
        let experiment = load_experiment(&self.conn, cycle_id)?;
        let artifact = experiment.spec.strategy.clone();
        let output = reviewed_research_output(&artifact.strategy_version_id)?;
        let manifest = reviewed_research_input(&artifact.research_input_id)?;
        if output.research_input_id != manifest.research_input_id
            || output.research_input_digest != manifest.content_digest {
            return Err(StoreError::Integrity("reviewed_research_output_mismatch".into()));
        }
        let mut statement = self.conn.prepare(
            "SELECT id,request_json,evidence_json FROM v2_simulation_runs WHERE cycle_id=? ORDER BY id"
        )?;
        let rows = statement.query_map([cycle_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?, row.get::<_, String>(2)?))
        })?;
        let mut index = 0usize;
        for row in rows {
            let (id, request_json, evidence_json) = row?;
            let frozen = output.signals.get(index).ok_or_else(|| StoreError::Integrity(
                format!("research_output_has_no_signal_for_run_{id}")))?;
            let request: SimulationRequest = serde_json::from_str(&request_json)?;
            let evidence: Option<QuoteEvidence> = serde_json::from_str(&evidence_json)?;
            if request.cycle_id != cycle_id
                || request.strategy_version_id != artifact.strategy_version_id
                || request.signal_id != frozen.signal_id
                || request.decision_at != frozen.decision_at
                || request.signal.symbol != frozen.symbol
                || request.signal.side != frozen.side
                || request.quote_ref.market_data_pack_id != frozen.market_data_pack_id
                || request.quote_ref.source != frozen.source
                || request.quote_ref.quote_id != frozen.quote_id
            {
                return Err(StoreError::Integrity(format!("research_signal_mismatch_at_run_{id}")));
            }
            let entry = manifest.entry_for(&request.quote_ref).ok_or_else(|| {
                StoreError::Integrity(format!("research_input_missing_entry_for_run_{id}"))
            })?;
            if entry.decision_at != request.decision_at || entry.quote.symbol != request.signal.symbol {
                return Err(StoreError::Integrity(
                    format!("research_input_entry_mismatch_at_run_{id}")));
            }
            // Missing evidence stays missing: only a pinned snapshot is compared,
            // and a substituted quote or liquidity level fails instead of being
            // silently accepted because the identity still resolves.
            if let Some(saved) = &evidence {
                if saved.market_data_pack_id != manifest.market_data_pack_id
                    || saved.quote != entry.quote
                    || saved.available_at != entry.available_at
                {
                    return Err(StoreError::Integrity(
                        format!("research_input_evidence_substitution_at_run_{id}")));
                }
                if let Some(level) = &saved.execution {
                    let frozen_level = entry.level(&level.order_side);
                    if level.price_cents != frozen_level.price_cents
                        || level.available_quantity != frozen_level.available_quantity
                        || level.observed_at != entry.quote.observed_at
                        || level.quote_ref != request.quote_ref
                    {
                        return Err(StoreError::Integrity(
                            format!("research_input_liquidity_substitution_at_run_{id}")));
                    }
                }
            }
            index += 1;
        }
        if index != output.signals.len() {
            return Err(StoreError::Integrity(
                format!("research_signal_missing_after_run_{index}")));
        }
        Ok(ResearchReproductionReport {
            cycle_id: cycle_id.into(),
            ok: true,
            strategy_version_id: artifact.strategy_version_id,
            research_input_id: artifact.research_input_id,
            research_output_id: output.research_output_id,
            research_output_digest: output.content_digest,
            parameters_digest: artifact.parameters_digest,
            source_digest: artifact.source_digest,
            signals_verified: index,
        })
    }
}

// ---------------------------------------------------------------------------
// Paper account read model (ADR 005). Read-only; the split is recomputed from
// the frozen spec so a restart cannot change it.
// ---------------------------------------------------------------------------
impl Store {
    pub fn paper_account(&self, account_id: &str) -> StoreResult<PaperAccountView> {
        let raw: Option<String> = self
            .conn
            .query_row(
                "SELECT spec_json FROM v2_paper_accounts WHERE account_id=?",
                [account_id],
                |row| row.get(0),
            )
            .optional()?;
        let raw = raw.ok_or_else(|| StoreError::NotFound("paper_account_not_found".into()))?;
        let spec: crate::domain::PaperAccountSpec = serde_json::from_str(&raw)?;
        let split = spec.validate()?;
        crate::store::Store::paper_account_view_with_conn(&self.conn, &spec, &split)
    }
}

impl Store {
    /// Read one account's slot orders in append order. Facts only.
    pub fn paper_orders(
        &self,
        account_id: &str,
        limit: i64,
    ) -> StoreResult<Vec<PaperOrderRecord>> {
        let mut statement = self.conn.prepare(
            "SELECT id,account_id,slot,strategy_version_id,request_json,outcome_json
             FROM v2_paper_orders WHERE account_id=? ORDER BY id DESC LIMIT ?",
        )?;
        let rows = statement.query_map(params![account_id, limit.clamp(1, 500)], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
            ))
        })?;
        let mut orders = Vec::new();
        for row in rows {
            let (id, account_id, slot, strategy_version_id, request_json, outcome_json) = row?;
            orders.push(PaperOrderRecord {
                id,
                account_id,
                slot,
                strategy_version_id,
                request: serde_json::from_str(&request_json)?,
                outcome: serde_json::from_str(&outcome_json)?,
                duplicate: false,
            });
        }
        Ok(orders)
    }
}
// ---------------------------------------------------------------------------
// Paper session catalogue (read-only).
// ---------------------------------------------------------------------------

impl Store {
    pub fn paper_sessions(
        &self,
        account_id: Option<&str>,
        limit: i64,
    ) -> StoreResult<Vec<PaperSessionView>> {
        let mut statement = self.conn.prepare(
            "SELECT session_id,account_id,spec_json,status,clock_at,cursor,last_order_id
             FROM v2_paper_sessions
             WHERE (?1 IS NULL OR account_id = ?1)
             ORDER BY session_id LIMIT ?2",
        )?;
        let rows = statement.query_map(params![account_id, limit.clamp(1, 200)], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, Option<i64>>(6)?,
            ))
        })?;
        let mut sessions = Vec::new();
        for row in rows {
            let (session_id, account_id, spec_json, status, clock_at, cursor, last_order_id) = row?;
            let spec: crate::domain::PaperSessionSpec = serde_json::from_str(&spec_json)?;
            sessions.push(PaperSessionView {
                session_id,
                account_id,
                status,
                clock_at,
                cursor: cursor.max(0) as usize,
                total: spec.signals.len(),
                last_order_id,
            });
        }
        Ok(sessions)
    }
}
