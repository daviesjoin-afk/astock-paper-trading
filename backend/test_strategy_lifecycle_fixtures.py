"""Fixture helpers for legacy consumers that need an already-migrated state."""
from __future__ import annotations

import strategy_lifecycle as SL
import strategy_registry as SR


_LEGACY_STATUS = {
    "draft": "draft", "validated": "validated", "active": "paper",
    "paper": "paper", "paused": "paused", "retiring": "retiring",
    "archived": "archived",
}
_LEGACY_COLUMN = {"draft": "draft", "validated": "validated", "paper": "active",
                  "paused": "paused", "retiring": "retiring", "archived": "archived"}


def seed_legacy_state(conn, strategy_id, target_state, **_legacy_arguments):
    """Seed one imported legacy row for downstream runtime tests.

    This models opening an existing pre-R31 database. It is test-only; production
    transitions go through Promotion Policy and the lifecycle owner.
    """
    strategy_id = str(strategy_id)
    state = _LEGACY_STATUS.get(str(target_state))
    if state is None:
        raise ValueError("legacy test fixture state has no pre-R31 mapping")
    version = SR.get_version(strategy_id, conn=conn)
    if version is None:
        raise ValueError("strategy version missing for lifecycle fixture")
    conn.execute("DROP TRIGGER IF EXISTS strategy_lifecycle_events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS strategy_lifecycle_events_no_delete")
    conn.execute("DELETE FROM strategy_lifecycle_events WHERE strategy_id=? AND strategy_version=?",
                 (strategy_id, version.version))
    conn.execute("DELETE FROM strategy_lifecycle_state WHERE strategy_id=? AND strategy_version=?",
                 (strategy_id, version.version))
    legacy = _LEGACY_COLUMN[state]
    conn.execute("UPDATE strategy_definitions SET lifecycle_status=?,supports_new_cycle=? WHERE id=?",
                 (legacy, int(state == "paper"), strategy_id))
    SL._insert_initial(conn, strategy_id, version.version, version.checksum, state,
        transition_kind="legacy_import",
        evidence={"migration_source": "legacy_strategy_registry", "legacy_imported": True,
                  "legacy_status": legacy},
        actor_type="system", actor_id="r31_test_fixture",
        reason_code="legacy_state_imported", reason_text="Seed migrated lifecycle for downstream test.")
    conn.execute("""CREATE TRIGGER strategy_lifecycle_events_no_update
        BEFORE UPDATE ON strategy_lifecycle_events
        BEGIN SELECT RAISE(ABORT,'append-only strategy lifecycle events'); END""")
    conn.execute("""CREATE TRIGGER strategy_lifecycle_events_no_delete
        BEFORE DELETE ON strategy_lifecycle_events
        BEGIN SELECT RAISE(ABORT,'append-only strategy lifecycle events'); END""")
    return SR.get(strategy_id, conn=conn)


def archive_state(conn, strategy_id, *, actor="test", reason="test-archive"):
    """Apply canonical safety transitions until this fixture is archived."""
    spec = SR.get(strategy_id, conn=conn)
    version = SR.get_version(strategy_id, conn=conn)
    state = spec.status
    if state == "archived":
        return spec
    if state == "draft":
        target = "archived"
        SL.transition(conn, strategy_id=strategy_id, strategy_version=version.version,
            strategy_checksum=version.checksum, expected_state=state, target_state=target,
            actor_type="human", actor_id=actor, transition_kind="safety",
            reason_code=reason, reason_text="Test fixture archive request")
        return SR.get(strategy_id, conn=conn)
    if state != "retiring":
        SL.transition(conn, strategy_id=strategy_id, strategy_version=version.version,
            strategy_checksum=version.checksum, expected_state=state, target_state="retiring",
            actor_type="human", actor_id=actor, transition_kind="safety",
            reason_code=reason, reason_text="Test fixture archive request")
        state = "retiring"
    SL.transition(conn, strategy_id=strategy_id, strategy_version=version.version,
        strategy_checksum=version.checksum, expected_state=state, target_state="archived",
        actor_type="human", actor_id=actor, transition_kind="safety",
        reason_code=reason, reason_text="Test fixture archive completed")
    return SR.get(strategy_id, conn=conn)
