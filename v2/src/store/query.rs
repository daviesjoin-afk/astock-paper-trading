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

impl Store {
    /// A valuation snapshot that refuses to invent prices. It reports exactly
    /// which packs and symbols a reviewed price set would have to cover.
    pub fn paper_valuation(
        &self,
        account_id: &str,
        as_of_day: &str,
    ) -> StoreResult<PaperValuationReport> {
        let day = chrono::NaiveDate::parse_from_str(as_of_day, "%Y-%m-%d")
            .map_err(|_| StoreError::Rules(RuleError("invalid_valuation_day".into())))?;
        if day.to_string() != as_of_day {
            return Err(StoreError::Rules(RuleError("invalid_valuation_day".into())));
        }
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
        let mut requirements = Vec::new();
        for (index, slot) in spec.slots.iter().enumerate() {
            let state = Store::paper_slot_state(&self.conn, &spec, &split, index)?;
            let mut symbols: Vec<String> =
                state.lots.iter().map(|lot| lot.symbol.clone()).collect();
            symbols.sort();
            symbols.dedup();
            requirements.push(PaperValuationRequirement {
                slot: slot.slot.as_str().into(),
                market_data_pack_id: slot.market_data_pack_id.clone(),
                symbols,
            });
        }
        let pack = spec.slots[0].market_data_pack_id.clone();
        if spec.slots.iter().any(|slot| slot.market_data_pack_id != pack) {
            return Err(StoreError::Rules(RuleError("valuation_mixed_packs".into())));
        }
        let registry = load_price_sets(&price_sets_dir())?;
        let mut report = PaperValuationReport {
            account_id: account_id.into(),
            as_of_day: as_of_day.into(),
            status: "unavailable".into(),
            reason: "no_reviewed_price_set".into(),
            requirements,
            missing: vec!["reviewed_price_set".into()],
            priced_symbols: 0,
            valuation: None,
            digest: String::new(),
        };
        if let Some(set) = registry.select(&pack, as_of_day) {
            let valued = self.paper_valuation_with_set(&report.account_id, as_of_day, set)?;
            report.status = "valued".into();
            report.reason = String::new();
            report.missing.clear();
            report.priced_symbols = valued.positions.len();
            report.valuation = Some(valued);
        }
        let mut value = serde_json::to_value(&report)?;
        if let Some(object) = value.as_object_mut() {
            object.remove("digest");
        }
        report.digest = crate::domain::canonical_digest(&value);
        Ok(report)
    }
}

impl Store {
    /// Value an account with an explicit price set. The set must cover every
    /// held symbol on the requested day and bind to the slots' market pack.
    pub fn paper_valuation_with_set(
        &self,
        account_id: &str,
        as_of_day: &str,
        set: &crate::domain::PriceSet,
    ) -> StoreResult<crate::domain::AccountValuation> {
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
        let pack = spec.slots[0].market_data_pack_id.clone();
        if spec
            .slots
            .iter()
            .any(|slot| slot.market_data_pack_id != pack)
        {
            return Err(StoreError::Rules(RuleError("valuation_mixed_packs".into())));
        }
        let mut positions: std::collections::BTreeMap<String, i64> =
            std::collections::BTreeMap::new();
        let mut cash_cents = split.reserve_cents;
        for (index, _) in spec.slots.iter().enumerate() {
            let state = Store::paper_slot_state(&self.conn, &spec, &split, index)?;
            cash_cents += state.cash_cents;
            for lot in &state.lots {
                *positions.entry(lot.symbol.clone()).or_default() += lot.quantity;
            }
        }
        let positions: Vec<(String, i64)> = positions.into_iter().collect();
        crate::domain::value_account(
            spec.initial_cash_cents,
            cash_cents,
            &positions,
            set,
            as_of_day,
            &pack,
        )
        .map_err(StoreError::Rules)
    }
}

/// Where reviewed price sets live. Operators publish audited JSON artifacts
/// here; the service only reads them. Missing directory means "no data yet".
fn price_sets_dir() -> std::path::PathBuf {
    match std::env::var_os("ASTOCK_PRICE_SETS_DIR") {
        Some(value) => std::path::PathBuf::from(value),
        None => std::path::PathBuf::from("data/price-sets"),
    }
}

/// Load every `*.json` price set from a directory and validate the catalogue.
fn load_price_sets(dir: &std::path::Path) -> StoreResult<crate::domain::PriceSetRegistry> {
    let mut sets = Vec::new();
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(crate::domain::PriceSetRegistry { schema_version: 1, sets });
        }
        Err(error) => {
            return Err(StoreError::Integrity(format!("price_sets_dir_unreadable: {error}")))
        }
    };
    for entry in entries {
        let path = entry
            .map_err(|error| StoreError::Integrity(format!("price_sets_dir_unreadable: {error}")))?
            .path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let raw = std::fs::read_to_string(&path)
            .map_err(|error| StoreError::Integrity(format!("price_set_unreadable: {error}")))?;
        let set: crate::domain::PriceSet = serde_json::from_str(&raw)?;
        sets.push(set);
    }
    let registry = crate::domain::PriceSetRegistry { schema_version: 1, sets };
    registry.validate()?;
    Ok(registry)
}

impl Store {
    pub fn price_set_catalogue(&self) -> StoreResult<Vec<crate::domain::PriceSetSummary>> {
        self.price_set_catalogue_in(&price_sets_dir())
    }

    pub fn price_set_catalogue_in(
        &self,
        dir: &std::path::Path,
    ) -> StoreResult<Vec<crate::domain::PriceSetSummary>> {
        Ok(load_price_sets(dir)?.summaries())
    }

    /// Valuation using an explicit registry (tests can supply one).
    pub fn paper_valuation_in_dir(
        &self,
        account_id: &str,
        as_of_day: &str,
        dir: &std::path::Path,
    ) -> StoreResult<PaperValuationReport> {
        let registry = load_price_sets(dir)?;
        self.paper_valuation_with_registry(account_id, as_of_day, &registry)
    }

    pub fn paper_valuation_with_registry(
        &self,
        account_id: &str,
        as_of_day: &str,
        registry: &crate::domain::PriceSetRegistry,
    ) -> StoreResult<PaperValuationReport> {
        let day = chrono::NaiveDate::parse_from_str(as_of_day, "%Y-%m-%d")
            .map_err(|_| StoreError::Rules(RuleError("invalid_valuation_day".into())))?;
        if day.to_string() != as_of_day {
            return Err(StoreError::Rules(RuleError("invalid_valuation_day".into())));
        }
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
        let mut requirements = Vec::new();
        for (index, slot) in spec.slots.iter().enumerate() {
            let state = Store::paper_slot_state(&self.conn, &spec, &split, index)?;
            let mut symbols: Vec<String> =
                state.lots.iter().map(|lot| lot.symbol.clone()).collect();
            symbols.sort();
            symbols.dedup();
            requirements.push(PaperValuationRequirement {
                slot: slot.slot.as_str().into(),
                market_data_pack_id: slot.market_data_pack_id.clone(),
                symbols,
            });
        }
        let pack = spec.slots[0].market_data_pack_id.clone();
        if spec.slots.iter().any(|slot| slot.market_data_pack_id != pack) {
            return Err(StoreError::Rules(RuleError("valuation_mixed_packs".into())));
        }
        let mut report = PaperValuationReport {
            account_id: account_id.into(),
            as_of_day: as_of_day.into(),
            status: "unavailable".into(),
            reason: "no_reviewed_price_set".into(),
            requirements,
            missing: vec!["reviewed_price_set".into()],
            priced_symbols: 0,
            valuation: None,
            digest: String::new(),
        };
        if let Some(set) = registry.select(&pack, as_of_day) {
            let valued = self.paper_valuation_with_set(account_id, as_of_day, set)?;
            report.status = "valued".into();
            report.reason = String::new();
            report.missing.clear();
            report.priced_symbols = valued.positions.len();
            report.valuation = Some(valued);
        }
        let mut value = serde_json::to_value(&report)?;
        if let Some(object) = value.as_object_mut() {
            object.remove("digest");
        }
        report.digest = crate::domain::canonical_digest(&value);
        Ok(report)
    }
}
// ---------------------------------------------------------------------------
// Equity series: replay fills up to each requested day and mark to market with
// one reviewed price set. Missing coverage refuses the series.
// ---------------------------------------------------------------------------

impl Store {
    fn account_fills(&self, account_id: &str) -> StoreResult<Vec<Fill>> {
        let mut statement = self.conn.prepare(
            "SELECT f.fill_json FROM v2_paper_fills f
             JOIN v2_paper_orders o ON o.id=f.order_id
             WHERE o.account_id=? ORDER BY o.id",
        )?;
        let rows = statement.query_map([account_id], |row| row.get::<_, String>(0))?;
        let mut fills = Vec::new();
        for row in rows {
            fills.push(serde_json::from_str::<Fill>(&row?)?);
        }
        Ok(fills)
    }

    fn account_pack(&self, account_id: &str) -> StoreResult<(String, i64, i64)> {
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
        let pack = spec.slots[0].market_data_pack_id.clone();
        if spec.slots.iter().any(|slot| slot.market_data_pack_id != pack) {
            return Err(StoreError::Rules(RuleError("valuation_mixed_packs".into())));
        }
        Ok((pack, spec.initial_cash_cents, split.reserve_cents))
    }

    pub fn paper_equity_series(
        &self,
        account_id: &str,
        days: &[String],
    ) -> StoreResult<Option<crate::domain::EquitySeries>> {
        self.paper_equity_series_in(account_id, days, &price_sets_dir())
    }

    pub fn paper_equity_series_in(
        &self,
        account_id: &str,
        days: &[String],
        dir: &std::path::Path,
    ) -> StoreResult<Option<crate::domain::EquitySeries>> {
        let registry = load_price_sets(dir)?;
        self.paper_equity_series_with_registry(account_id, days, &registry)
    }

    pub fn paper_equity_series_with_registry(
        &self,
        account_id: &str,
        days: &[String],
        registry: &crate::domain::PriceSetRegistry,
    ) -> StoreResult<Option<crate::domain::EquitySeries>> {
        if days.is_empty() {
            return Err(StoreError::Rules(RuleError("empty_equity_series".into())));
        }
        let (pack, initial_cash_cents, _) = self.account_pack(account_id)?;
        let Some(set) = registry.select_covering(&pack, &days[0], &days[days.len() - 1]) else {
            return Ok(None);
        };
        let fills = self.account_fills(account_id)?;
        crate::domain::equity_series(initial_cash_cents, &fills, set, days, &pack)
            .map(Some)
            .map_err(StoreError::Rules)
    }
}
// ---------------------------------------------------------------------------
// Account audit: prove the ledger still balances and every fill is linked to
// exactly one stored order. Read-only; the result carries a canonical digest.
// ---------------------------------------------------------------------------

impl Store {
    pub fn paper_account_audit(&self, account_id: &str) -> StoreResult<PaperAuditReport> {
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
        let split = spec.validate().map_err(StoreError::Rules)?;
        let weights = Store::account_weights(&self.conn, &spec)?;
        let expected = spec
            .split_with_weights(weights)
            .map_err(StoreError::Rules)?;
        let view = self.paper_account(account_id)?;

        let mut checks = Vec::new();

        // 1. Budgets are exactly the split of the initial capital at current weights.
        let budgets_ok = view
            .slots
            .iter()
            .enumerate()
            .all(|(index, slot)| slot.budget_cents == expected.slot_budgets[index]);
        checks.push(PaperAuditCheck {
            name: "budget_split".into(),
            ok: budgets_ok,
            detail: format!(
                "budgets {:?} vs split {:?}",
                view.slots.iter().map(|slot| slot.budget_cents).collect::<Vec<_>>(),
                expected.slot_budgets
            ),
        });

        // 2. Fills replay cleanly into each slot state (already enforced by the view).
        checks.push(PaperAuditCheck {
            name: "fills_replay".into(),
            ok: true,
            detail: "slot states rebuilt from fills".into(),
        });

        // 3. Cash conservation: initial + net cash flow equals reserve + slot cash.
        let mut statement = self.conn.prepare(
            "SELECT f.fill_json, o.slot FROM v2_paper_fills f
             JOIN v2_paper_orders o ON o.id=f.order_id WHERE o.account_id=? ORDER BY o.id",
        )?;
        let rows = statement.query_map([account_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut net_flow = 0i64;
        let mut fill_count = 0i64;
        let mut per_slot: std::collections::BTreeMap<String, i64> =
            std::collections::BTreeMap::new();
        for row in rows {
            let (raw_fill, slot) = row?;
            let fill: Fill = serde_json::from_str(&raw_fill)?;
            let gross = fill.quantity.saturating_mul(fill.price_cents);
            match fill.side {
                crate::domain::Side::Buy => net_flow -= gross + fill.fees_cents,
                crate::domain::Side::Sell => net_flow += gross - fill.fees_cents,
            }
            fill_count += 1;
            *per_slot.entry(slot).or_default() += 1;
        }
        let expected_cash = spec.initial_cash_cents.saturating_add(net_flow);
        checks.push(PaperAuditCheck {
            name: "capital_conservation".into(),
            ok: expected_cash == view.cash_cents,
            detail: format!("expected {} cents, stored view {} cents", expected_cash, view.cash_cents),
        });

        // 4. Every fill links to an order and every filled order has exactly one fill.
        let mut orders_statement =
            self.conn.prepare("SELECT outcome_json FROM v2_paper_orders WHERE account_id=?")?;
        let orders = orders_statement.query_map([account_id], |row| row.get::<_, String>(0))?;
        let mut filled_orders = 0i64;
        for row in orders {
            let outcome: Outcome = serde_json::from_str(&row?)?;
            if outcome.status == crate::domain::DecisionStatus::Filled {
                filled_orders += 1;
            }
        }
        checks.push(PaperAuditCheck {
            name: "order_fill_link".into(),
            ok: filled_orders == fill_count,
            detail: format!("{filled_orders} filled orders vs {fill_count} fills"),
        });

        // 4b. Every stored fill must equal the fill recorded on its order outcome.
        let mut pairs = self.conn.prepare(
            "SELECT f.fill_json, o.outcome_json FROM v2_paper_fills f
             JOIN v2_paper_orders o ON o.id=f.order_id WHERE o.account_id=? ORDER BY o.id",
        )?;
        let rows = pairs.query_map([account_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut fill_matches = true;
        let mut mismatch_detail = Vec::new();
        for row in rows {
            let (raw_fill, raw_outcome) = row?;
            let fill: Fill = serde_json::from_str(&raw_fill)?;
            let outcome: Outcome = serde_json::from_str(&raw_outcome)?;
            if outcome.fill.as_ref() != Some(&fill) {
                fill_matches = false;
                mismatch_detail.push(format!("fill mismatch: {raw_fill}"));
            }
        }
        checks.push(PaperAuditCheck {
            name: "fill_matches_outcome".into(),
            ok: fill_matches,
            detail: if fill_matches { "fills equal order outcomes".into() } else { mismatch_detail.join("; ") },
        });

        // 5. Session cursors never point past the account's orders.
        let mut sessions_statement = self.conn.prepare(
            "SELECT session_id,cursor,last_order_id FROM v2_paper_sessions WHERE account_id=?",
        )?;
        let sessions = sessions_statement.query_map([account_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, Option<i64>>(2)?,
            ))
        })?;
        let mut session_ok = true;
        let mut session_detail = Vec::new();
        for row in sessions {
            let (session_id, cursor, last_order_id) = row?;
            let exists: i64 = match last_order_id {
                Some(order_id) => self.conn.query_row(
                    "SELECT COUNT(*) FROM v2_paper_orders WHERE account_id=? AND id=?",
                    params![account_id, order_id],
                    |row| row.get(0),
                )?,
                None => 0,
            };
            if last_order_id.is_some() && exists == 0 {
                session_ok = false;
            }
            session_detail.push(format!("{session_id}: cursor={cursor}, last_order={last_order_id:?}"));
        }
        checks.push(PaperAuditCheck {
            name: "session_link".into(),
            ok: session_ok,
            detail: session_detail.join("; "),
        });

        // 6. Session orders must equal the frozen signal prefix of their session.
        let mut sessions_statement = self.conn.prepare(
            "SELECT session_id,spec_json,cursor FROM v2_paper_sessions WHERE account_id=?",
        )?;
        let sessions = sessions_statement.query_map([account_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })?;
        let mut signals_ok = true;
        let mut signals_detail = Vec::new();
        for row in sessions {
            let (session_id, raw_spec, cursor) = row?;
            let spec: crate::domain::PaperSessionSpec = serde_json::from_str(&raw_spec)?;
            let prefix = format!("{session_id}:");
            let mut orders_statement = self.conn.prepare(
                "SELECT signal_id,request_json FROM v2_paper_orders
                 WHERE account_id=? AND signal_id LIKE ? ORDER BY id",
            )?;
            let rows = orders_statement.query_map(params![account_id, format!("{prefix}%")], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            let mut index = 0usize;
            for row in rows {
                let (signal_id, raw_request) = row?;
                let request: crate::domain::PaperOrderRequest = serde_json::from_str(&raw_request)?;
                let Some(expected) = spec.signals.get(index) else {
                    signals_ok = false;
                    signals_detail.push(format!("{session_id}: extra order {signal_id}"));
                    index += 1;
                    continue;
                };
                if signal_id != format!("{session_id}:{}", expected.signal_id)
                    || request.decision_at != expected.decision_at
                    || request.quote_ref != expected.quote_ref
                {
                    signals_ok = false;
                    signals_detail.push(format!("{session_id}: order {index} does not match frozen signal"));
                }
                index += 1;
            }
            if index as i64 != cursor {
                signals_ok = false;
                signals_detail.push(format!("{session_id}: {index} orders vs cursor {cursor}"));
            }
        }
        checks.push(PaperAuditCheck {
            name: "session_signals".into(),
            ok: signals_ok,
            detail: if signals_ok { "session orders equal frozen signals".into() } else { signals_detail.join("; ") },
        });

        // 7. Rebalance chain: weights flow without gaps and always keep the
        // reserve, and cash only moves between distinct slots.
        let mut chain_statement = self.conn.prepare(
            "SELECT from_slot,to_slot,amount_cents,weights_before_json,weights_after_json
             FROM v2_paper_transfers WHERE account_id=? ORDER BY id",
        )?;
        let chain_rows = chain_statement.query_map([account_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
            ))
        })?;
        let mut chain_ok = true;
        let mut chain_detail = Vec::new();
        let mut previous_after: Option<[i64; 2]> = None;
        for row in chain_rows {
            let (from_slot, to_slot, amount_cents, raw_before, raw_after) = row?;
            let before: Vec<i64> = serde_json::from_str(&raw_before)?;
            let after: Vec<i64> = serde_json::from_str(&raw_after)?;
            if before.len() != 2 || after.len() != 2 {
                chain_ok = false;
                chain_detail.push("weights array is not length 2".into());
                continue;
            }
            let before = [before[0], before[1]];
            let after = [after[0], after[1]];
            if let Some(previous) = previous_after {
                if previous != before {
                    chain_ok = false;
                    chain_detail.push(format!("weights gap {previous:?} -> {before:?}"));
                }
            }
            if after[0] + after[1] + spec.reserve_bps != 10_000 {
                chain_ok = false;
                chain_detail.push(format!("weights {after:?} do not keep the reserve"));
            }
            if amount_cents > 0 && from_slot == to_slot {
                chain_ok = false;
                chain_detail.push("transfer moves cash to the same slot".into());
            }
            previous_after = Some(after);
        }
        if let Some(last) = previous_after {
            if last != weights {
                chain_ok = false;
                chain_detail.push(format!("last weights {last:?} != current {weights:?}"));
            }
        }
        checks.push(PaperAuditCheck {
            name: "rebalance_chain".into(),
            ok: chain_ok,
            detail: if chain_ok { "rebalance weights chain consistent".into() } else { chain_detail.join("; ") },
        });

        let ok = checks.iter().all(|check| check.ok);
        let mut report = PaperAuditReport {
            account_id: account_id.into(),
            ok,
            checks,
            digest: String::new(),
        };
        let mut value = serde_json::to_value(&report)?;
        if let Some(object) = value.as_object_mut() {
            object.remove("digest");
        }
        report.digest = crate::domain::canonical_digest(&value);
        Ok(report)
    }
}
// ---------------------------------------------------------------------------
// Capital allocation proposal: per-slot equity from reviewed prices -> scores
// -> bounded, explainable target weights. Read-only; nothing is applied.
// ---------------------------------------------------------------------------

impl Store {
    fn slot_fills(&self, account_id: &str, slot: &str) -> StoreResult<Vec<Fill>> {
        let mut statement = self.conn.prepare(
            "SELECT f.fill_json FROM v2_paper_fills f
             JOIN v2_paper_orders o ON o.id=f.order_id
             WHERE o.account_id=? AND o.slot=? ORDER BY o.id",
        )?;
        let rows = statement.query_map(params![account_id, slot], |row| row.get::<_, String>(0))?;
        let mut fills = Vec::new();
        for row in rows {
            fills.push(serde_json::from_str::<Fill>(&row?)?);
        }
        Ok(fills)
    }

    pub fn paper_allocation_proposal(
        &self,
        account_id: &str,
        days: &[String],
        dir: &std::path::Path,
    ) -> StoreResult<Option<AllocationReport>> {
        if days.len() < 2 {
            return Err(StoreError::Rules(RuleError("allocation_requires_two_days".into())));
        }
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
        let split = spec.validate().map_err(StoreError::Rules)?;
        let pack = spec.slots[0].market_data_pack_id.clone();
        if spec.slots.iter().any(|slot| slot.market_data_pack_id != pack) {
            return Err(StoreError::Rules(RuleError("valuation_mixed_packs".into())));
        }
        let registry = load_price_sets(dir)?;
        let Some(set) = registry.select_covering(&pack, &days[0], &days[days.len() - 1]) else {
            return Ok(None);
        };

        let mut scores = Vec::new();
        for (index, slot) in spec.slots.iter().enumerate() {
            let budget = split.slot_budgets[index].max(1);
            let fills = self.slot_fills(account_id, slot.slot.as_str())?;
            let series = crate::domain::equity_series(budget, &fills, set, days, &pack)
                .map_err(StoreError::Rules)?;
            let last = series.points.last().map(|point| point.equity_cents).unwrap_or(budget);
            let return_bps = (last - budget) * 10_000 / budget;
            let drawdown_bps = series.max_drawdown_cents * 10_000 / budget;
            scores.push(crate::domain::SlotScore {
                slot: slot.slot.as_str().into(),
                return_bps,
                drawdown_bps,
                score_bps: return_bps - drawdown_bps,
                days: series.points.len(),
            });
        }
        let policy = crate::domain::AllocationPolicy::default();
        let proposal = crate::domain::propose_allocation(
            spec.slots[0].weight_bps,
            spec.slots[1].weight_bps,
            &scores,
            &policy,
        )
        .map_err(StoreError::Rules)?;
        Ok(Some(AllocationReport {
            proposal,
            scores,
            policy,
        }))
    }
}
// ---------------------------------------------------------------------------
// Rebalance read model + audit chain check (ADR 009).
// ---------------------------------------------------------------------------

impl Store {
    pub fn paper_rebalances(&self, account_id: &str) -> StoreResult<Vec<RebalanceRecord>> {
        let mut statement = self.conn.prepare(
            "SELECT account_id,from_slot,to_slot,amount_cents,applied_at,note,digest,
                    weights_before_json,weights_after_json
             FROM v2_paper_transfers WHERE account_id=? ORDER BY id",
        )?;
        let rows = statement.query_map([account_id], |row| {
            Ok((
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
                row.get::<_, String>(8)?,
            ))
        })?;
        let mut records = Vec::new();
        for row in rows {
            let (from_slot, to_slot, amount_cents, applied_at, note, digest, before, after) = row?;
            let weights_before: [i64; 2] = serde_json::from_str::<Vec<i64>>(&before)?
                .try_into()
                .map_err(|_| StoreError::Integrity("rebalance_weights_invalid".into()))?;
            let weights_after: [i64; 2] = serde_json::from_str::<Vec<i64>>(&after)?
                .try_into()
                .map_err(|_| StoreError::Integrity("rebalance_weights_invalid".into()))?;
            let mut record = RebalanceRecord {
                account_id: account_id.into(),
                from_slot,
                to_slot,
                amount_cents,
                weights_before,
                weights_after,
                applied_at,
                note,
                proposal_digest: digest,
                digest: String::new(),
            };
            record.digest = rebalance_digest(&record)?;
            records.push(record);
        }
        Ok(records)
    }
}
// ---------------------------------------------------------------------------
// Exposure read model: cost basis per slot, concentration vs risk limits.
// ---------------------------------------------------------------------------

impl Store {
    fn slot_cost_positions(
        &self,
        account_id: &str,
        slot: &str,
    ) -> StoreResult<Vec<(String, i64, i64)>> {
        let mut statement = self.conn.prepare(
            "SELECT f.fill_json FROM v2_paper_fills f
             JOIN v2_paper_orders o ON o.id=f.order_id
             WHERE o.account_id=? AND o.slot=? ORDER BY o.id",
        )?;
        let rows = statement.query_map(params![account_id, slot], |row| row.get::<_, String>(0))?;
        let mut lots: std::collections::BTreeMap<String, (i64, i64)> =
            std::collections::BTreeMap::new();
        for row in rows {
            let fill: Fill = serde_json::from_str(&row?)?;
            let entry = lots.entry(fill.symbol.clone()).or_insert((0, 0));
            let gross = fill.quantity.saturating_mul(fill.price_cents);
            match fill.side {
                crate::domain::Side::Buy => {
                    entry.0 += fill.quantity;
                    entry.1 += gross + fill.fees_cents;
                }
                crate::domain::Side::Sell => {
                    let sold = fill.quantity.min(entry.0);
                    if entry.0 > 0 {
                        let avg = entry.1 / entry.0;
                        entry.1 -= avg * sold;
                    }
                    entry.0 -= sold;
                }
            }
        }
        Ok(lots
            .into_iter()
            .map(|(symbol, (quantity, cost))| (symbol, quantity, cost))
            .collect())
    }

    pub fn paper_exposure(&self, account_id: &str) -> StoreResult<crate::domain::AccountExposure> {
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
        let initial = spec.validate().map_err(StoreError::Rules)?;
        let weights = Self::account_weights(&self.conn, &spec)?;
        let display = spec.split_with_weights(weights).map_err(StoreError::Rules)?;
        let mut books = Vec::new();
        for (index, slot) in spec.slots.iter().enumerate() {
            let state = Self::paper_slot_state(&self.conn, &spec, &initial, index)?;
            books.push(crate::domain::SlotBook {
                slot: slot.slot.as_str().into(),
                budget_cents: display.slot_budgets[index],
                cash_cents: state.cash_cents,
                positions: self.slot_cost_positions(account_id, slot.slot.as_str())?,
            });
        }
        crate::domain::compute_exposure(account_id, &books, spec.limits.max_symbol_weight_bps)
            .map_err(StoreError::Rules)
    }
}
// ---------------------------------------------------------------------------
// Evolution event log: persist promotion/rollback decisions for audit.
// ---------------------------------------------------------------------------

impl Store {
    pub fn record_evolution_event(
        &mut self,
        event_id: &str,
        decided_at: &str,
        decision: crate::domain::EvolutionDecision,
    ) -> StoreResult<EvolutionEventRecord> {
        let valid_id = !event_id.is_empty()
            && event_id.len() <= 128
            && event_id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "_-.:@".contains(c));
        if !valid_id {
            return Err(StoreError::Rules(RuleError("invalid_evolution_event_id".into())));
        }
        chrono::DateTime::parse_from_rfc3339(decided_at)
            .map_err(|_| StoreError::Rules(RuleError("invalid_evolution_time".into())))?;
        let txn = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing: Option<String> = txn
            .query_row(
                "SELECT decision_json FROM v2_evolution_events WHERE event_id=?",
                [event_id],
                |row| row.get(0),
            )
            .optional()?;
        let raw_decision = serde_json::to_string(&decision)?;
        if let Some(stored) = existing {
            if stored != raw_decision {
                return Err(StoreError::Conflict("evolution_event_conflict".into()));
            }
            drop(txn);
            let mut record = EvolutionEventRecord {
                event_id: event_id.into(),
                candidate_id: decision.candidate_id.clone(),
                status: decision.status.clone(),
                decision,
                decided_at: decided_at.into(),
                digest: String::new(),
            };
            record.digest = evolution_event_digest(&record)?;
            return Ok(record);
        }
        txn.execute(
            "INSERT INTO v2_evolution_events(event_id,candidate_id,status,decision_json,decided_at,digest)
             VALUES(?,?,?,?,?,?)",
            params![
                event_id,
                decision.candidate_id,
                decision.status,
                raw_decision,
                decided_at,
                "",
            ],
        )?;
        txn.commit()?;
        let mut record = EvolutionEventRecord {
            event_id: event_id.into(),
            candidate_id: decision.candidate_id.clone(),
            status: decision.status.clone(),
            decision,
            decided_at: decided_at.into(),
            digest: String::new(),
        };
        record.digest = evolution_event_digest(&record)?;
        Ok(record)
    }

    pub fn evolution_events(&self, limit: i64) -> StoreResult<Vec<EvolutionEventRecord>> {
        let mut statement = self.conn.prepare(
            "SELECT event_id,candidate_id,status,decision_json,decided_at
             FROM v2_evolution_events ORDER BY decided_at DESC, event_id DESC LIMIT ?",
        )?;
        let rows = statement.query_map([limit], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
            ))
        })?;
        let mut records = Vec::new();
        for row in rows {
            let (event_id, candidate_id, status, raw_decision, decided_at) = row?;
            let decision: crate::domain::EvolutionDecision = serde_json::from_str(&raw_decision)?;
            let mut record = EvolutionEventRecord {
                event_id,
                candidate_id,
                status,
                decision,
                decided_at,
                digest: String::new(),
            };
            record.digest = evolution_event_digest(&record)?;
            records.push(record);
        }
        Ok(records)
    }
}

fn evolution_event_digest(record: &EvolutionEventRecord) -> StoreResult<String> {
    let mut value = serde_json::to_value(record)?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    Ok(crate::domain::canonical_digest(&value))
}
