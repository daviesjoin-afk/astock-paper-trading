"""R23 review findings: reproduce each before fixing.

F1 (P1) selection_tracking._migrate_runs renames the parent while child FKs
        point at it -> child schema ends up referencing a dropped table.
F2 (P1) an interrupted migration leaves selection_runs_legacy as the only copy,
        and the next startup drops it unconditionally -> history lost.
F3 (P2) the signal bootstrap scan's ON CONFLICT DO UPDATE rewrites cycle_id,
        which the v23 immutability trigger rejects -> scan aborts instead of
        refreshing a row that was already there before the upgrade.

Each check is self-contained and prints BEFORE values, so a fix can be proven
by re-running this file.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
sys.path.insert(0, str(BACKEND))
WORK = Path(tempfile.gettempdir()) / "r23_review_repro"
WORK.mkdir(parents=True, exist_ok=True)

import selection_tracking as ST  # noqa: E402

LEGACY_RUN_DDL = """
CREATE TABLE selection_runs (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    data_asof_date TEXT,
    benchmark_entry_price REAL,
    universe_size INTEGER,
    candidate_count INTEGER,
    selected_count INTEGER,
    executable_count INTEGER,
    source TEXT NOT NULL,
    result_json TEXT NOT NULL,
    UNIQUE(run_date, strategy)
)
"""

LEGACY_PICK_DDL = """
CREATE TABLE selection_picks (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES selection_runs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    rank_no INTEGER NOT NULL
)
"""


def _production_signal_upsert():
    """Extract the real ``INSERT INTO paper_signals ... ON CONFLICT`` statement.

    Extracting it from the source (instead of pasting a copy here) is the point:
    a hand-copied statement keeps passing after the production one changes, which
    is exactly how a "before/after" probe goes blind.

    Set ``R23_SIGNAL_INSERT_SRC`` to another revision's ``paper_trading.py`` to
    run the same probe against unfixed code.
    """
    src_path = os.environ.get("R23_SIGNAL_INSERT_SRC") or str(BACKEND / "paper_trading.py")
    src = Path(src_path).read_text(encoding="utf-8")
    start = src.index('"""INSERT INTO paper_signals(\n')
    end = src.index('"""', start + 3)
    stmt = src[start + 3:end]
    assert "ON CONFLICT(account_id,signal_date,code)" in stmt, "not a conflict upsert"
    return stmt


def _signal_column_order(stmt):
    """Column order of the INSERT, so the probe cannot pass its values wrong.

    The statement lists ``intended_date`` before ``code``; passing values in
    naive "date, code" order silently targets a different conflict row and the
    probe then never exercises the conflict branch at all.
    """
    head = stmt.split(")", 1)[0]
    return [part.strip() for part in head.split("(", 1)[1].split(",")]


def _legacy_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_RUN_DDL + ";" + LEGACY_PICK_DDL + ";")
    conn.execute(
        "INSERT INTO selection_runs(id, run_date, generated_at, strategy,"
        " strategy_name, source, result_json) VALUES(7,'2026-09-01','t',"
        "'trend_pullback','趋势','scheduled','{}')"
    )
    conn.execute("INSERT INTO selection_picks(id, run_id, code, rank_no) VALUES(1,7,'600000',1)")
    conn.commit()
    conn.close()


def f1_child_fk_target():
    path = WORK / "f1.sqlite3"
    path.unlink(missing_ok=True)
    _legacy_db(path)
    conn = sqlite3.connect(str(path))
    # Mirror production exactly: ensure_schema() disables FK enforcement for the
    # rebuild and re-enables it afterwards. Judging the FK target under a
    # different pragma than production is how this defect stayed invisible.
    conn.execute("PRAGMA foreign_keys = OFF")
    ST._migrate_runs(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='selection_picks'"
    ).fetchone()[0]
    tail = sql.split("REFERENCES")[1].split("(")[0].strip() if "REFERENCES" in sql else "<none>"
    print("F1 child FK now references:", tail)
    picks = conn.execute("SELECT COUNT(*) FROM selection_picks").fetchone()[0]
    print("F1 picks surviving the rebuild:", picks)
    try:
        conn.execute(
            "INSERT INTO selection_picks(id, run_id, code, rank_no) VALUES(2,7,'600001',2)"
        )
        conn.commit()
        verdict = "OK (pick insert works)"
    except sqlite3.Error as exc:
        verdict = f"BROKEN: {type(exc).__name__}: {exc}"
    print("F1 pick insert after migration:", verdict)
    conn.close()


def f2_interrupted_recovery():
    """Simulate: RENAME happened, process died before CREATE+INSERT+DROP."""
    path = WORK / "f2.sqlite3"
    path.unlink(missing_ok=True)
    _legacy_db(path)
    conn = sqlite3.connect(str(path))
    # exactly the first statement of the real executescript
    conn.execute("ALTER TABLE selection_runs RENAME TO selection_runs_legacy")
    conn.commit()
    rows_before = conn.execute("SELECT COUNT(*) FROM selection_runs_legacy").fetchone()[0]
    conn.close()

    # next startup
    conn = sqlite3.connect(str(path))
    ST._migrate_runs(conn)
    conn.commit()
    rows_after = conn.execute("SELECT COUNT(*) FROM selection_runs").fetchone()[0]
    legacy_left = ST._table_exists(conn, "selection_runs_legacy")
    conn.close()
    print(
        f"F2 rows in legacy before recovery={rows_before}; "
        f"rows in new table after recovery={rows_after}; legacy kept={legacy_left}"
    )
    print("F2 verdict:", "DATA LOST" if rows_after == 0 and rows_before else "preserved")


def f3_signal_conflict_update():
    """A signal row that predates the upgrade is refreshed by the bootstrap scan."""
    path = WORK / "f3.sqlite3"
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE paper_cycles (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO paper_cycles(id, status) VALUES(8,'running');
        CREATE TABLE paper_signals (
            id INTEGER PRIMARY KEY,
            account_id TEXT,
            signal_date TEXT,
            code TEXT,
            intended_date TEXT,
            name TEXT,
            industry TEXT,
            close_price REAL,
            rank_score REAL,
            t_tier TEXT,
            t_score REAL,
            payload TEXT,
            status TEXT,
            reason TEXT,
            created_at TEXT,
            strategy_id TEXT,
            strategy_version INTEGER,
            strategy_checksum TEXT,
            cycle_id INTEGER,
            UNIQUE(account_id, signal_date, code)
        );
        """
    )
    # a row written BEFORE the upgrade: cycle_id and the stamp are honestly unknown
    conn.execute(
        "INSERT INTO paper_signals(account_id, signal_date, code, status,"
        " created_at, strategy_id, strategy_version, strategy_checksum, cycle_id)"
        " VALUES('acct','2026-09-02','600000','pending','t1',NULL,NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    sys.path.insert(0, str(BACKEND))
    import paper_schema_migrations as PSM

    PSM._ensure_signal_cycle_provenance_guards(conn)
    # production also installs the strategy-stamp immutability trigger, and it
    # rejects the very same conflict update - so install it here too
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_paper_signals_strategy_stamp_immutable
           BEFORE UPDATE OF strategy_id,strategy_version,strategy_checksum ON paper_signals
           WHEN NEW.strategy_id IS NOT OLD.strategy_id
             OR NEW.strategy_version IS NOT OLD.strategy_version
             OR NEW.strategy_checksum IS NOT OLD.strategy_checksum
           BEGIN SELECT RAISE(ABORT, 'strategy version stamp is immutable'); END"""
    )
    conn.commit()

    stmt = _production_signal_upsert()
    values = {
        "account_id": "acct", "signal_date": "2026-09-02", "code": "600000",
        "intended_date": "2026-09-03", "name": "n", "industry": "i",
        "close_price": 10.0, "rank_score": 1.0, "t_tier": "A", "t_score": 1.0,
        "payload": "{}", "status": "ready", "reason": "r", "created_at": "t2",
        "strategy_id": "acct", "strategy_version": 3,
        "strategy_checksum": "abc", "cycle_id": 8,
    }
    order = _signal_column_order(stmt)
    assert set(order) == set(values), f"probe is blind to columns: {set(order) ^ set(values)}"
    try:
        conn.execute(stmt, tuple(values[column] for column in order))
        conn.commit()
        row = conn.execute(
            "SELECT status, strategy_id, strategy_version, cycle_id FROM paper_signals"
            " WHERE account_id='acct' AND code='600000'"
        ).fetchone()
        verdict = (
            f"OK -> status={row[0]} stamp={row[1]}/{row[2]} cycle_id={row[3]}"
        )
    except sqlite3.Error as exc:
        verdict = f"ABORTS: {type(exc).__name__}: {exc}"
    print("F3 conflict-update refresh after upgrade:", verdict)
    conn.close()


if __name__ == "__main__":
    for fn in (f1_child_fk_target, f2_interrupted_recovery, f3_signal_conflict_update):
        try:
            fn()
        except Exception as exc:  # one finding's crash must not hide the others
            print(f"{fn.__name__} RAISED: {type(exc).__name__}: {exc}")
