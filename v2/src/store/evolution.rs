//! Append-only evolution decisions owned by the Store write path.
use super::*;

impl Store {
    pub fn record_evolution_event(
        &mut self,
        event_id: &str,
        decided_at: &str,
        decision: crate::domain::EvolutionDecision,
    ) -> StoreResult<EvolutionEventRecord> {
        self.record_evolution_event_with_links(event_id, decided_at, decision, None, None)
    }

    pub fn record_evolution_event_with_links(
        &mut self,
        event_id: &str,
        decided_at: &str,
        decision: crate::domain::EvolutionDecision,
        base_cycle: Option<&str>,
        candidate_cycle: Option<&str>,
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
            return evolution_event_record(
                event_id, decided_at, decision, base_cycle, candidate_cycle,
            );
        }
        txn.execute(
            "INSERT INTO v2_evolution_events(event_id,candidate_id,status,decision_json,decided_at,digest,base_cycle,candidate_cycle)
             VALUES(?,?,?,?,?,?,?,?)",
            params![
                event_id,
                decision.candidate_id,
                decision.status,
                raw_decision,
                decided_at,
                "",
                base_cycle,
                candidate_cycle,
            ],
        )?;
        txn.commit()?;
        evolution_event_record(event_id, decided_at, decision, base_cycle, candidate_cycle)
    }
}

fn evolution_event_record(
    event_id: &str,
    decided_at: &str,
    decision: crate::domain::EvolutionDecision,
    base_cycle: Option<&str>,
    candidate_cycle: Option<&str>,
) -> StoreResult<EvolutionEventRecord> {
    let mut record = EvolutionEventRecord {
        event_id: event_id.into(),
        candidate_id: decision.candidate_id.clone(),
        status: decision.status.clone(),
        decision,
        decided_at: decided_at.into(),
        base_cycle: base_cycle.map(str::to_string),
        candidate_cycle: candidate_cycle.map(str::to_string),
        digest: String::new(),
    };
    record.digest = evolution_event_digest(&record)?;
    Ok(record)
}

pub(super) fn evolution_event_digest(record: &EvolutionEventRecord) -> StoreResult<String> {
    let mut value = serde_json::to_value(record)?;
    if let Some(object) = value.as_object_mut() {
        object.remove("digest");
    }
    Ok(crate::domain::canonical_digest(&value))
}
