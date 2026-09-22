"""Class audit: does the same defect shape exist elsewhere?

Q1 does the strategy-stamp immutable trigger ALSO reject the same conflict update?
   (the review named cycle_id; if the stamp columns abort too, that is the same
   defect one column over and must be fixed in the same change)
Q2 does any table reference the parents renamed by the sibling rebuilds?
Q3 is the sibling rename order already safe (create/copy/drop/rename)?
"""
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
WORK = Path(tempfile.gettempdir()) / "r23_review_repro"
WORK.mkdir(parents=True, exist_ok=True)

import paper_schema_migrations as PSM  # noqa: E402

# ---- Q1: both triggers installed, which one aborts? ----
path = WORK / "q1.sqlite3"
path.unlink(missing_ok=True)
conn = sqlite3.connect(str(path))
conn.executescript(
    """
    CREATE TABLE paper_cycles (id INTEGER PRIMARY KEY, status TEXT);
    INSERT INTO paper_cycles(id, status) VALUES(8,'running');
    CREATE TABLE paper_strategy_versions (
        strategy_id TEXT, version INTEGER, checksum TEXT,
        UNIQUE(strategy_id, version));
    CREATE TABLE paper_signals (
        id INTEGER PRIMARY KEY, account_id TEXT, signal_date TEXT, code TEXT,
        intended_date TEXT, name TEXT, industry TEXT, close_price REAL,
        rank_score REAL, t_tier TEXT, t_score REAL, payload TEXT, status TEXT,
        reason TEXT, created_at TEXT, strategy_id TEXT, strategy_version INTEGER,
        strategy_checksum TEXT, cycle_id INTEGER,
        UNIQUE(account_id, signal_date, code));
    """
)
# a pre-R23 row: stamp and cycle both honestly NULL
conn.execute(
    "INSERT INTO paper_signals(account_id, signal_date, code, status, created_at,"
    " strategy_id, strategy_version, strategy_checksum, cycle_id)"
    " VALUES('acct','2026-09-02','600000','pending','t1',NULL,NULL,NULL,NULL)"
)
conn.commit()
conn.close()

conn = sqlite3.connect(str(path))
PSM._ensure_signal_cycle_provenance_guards(conn)
# install the strategy-stamp immutability trigger exactly as production does
if {"strategy_id", "strategy_version", "strategy_checksum"}.issubset(
    PSM.table_columns(conn, "paper_signals")
):
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_paper_signals_strategy_stamp_immutable
           BEFORE UPDATE OF strategy_id,strategy_version,strategy_checksum ON paper_signals
           WHEN NEW.strategy_id IS NOT OLD.strategy_id
             OR NEW.strategy_version IS NOT OLD.strategy_version
             OR NEW.strategy_checksum IS NOT OLD.strategy_checksum
           BEGIN SELECT RAISE(ABORT, 'strategy version stamp is immutable'); END"""
    )
conn.commit()

stmt = ("INSERT INTO paper_signals(account_id,signal_date,code,status,created_at,"
        "strategy_id,strategy_version,strategy_checksum,cycle_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(account_id,signal_date,code) DO UPDATE SET"
        " status=excluded.status, strategy_id=excluded.strategy_id,"
        " strategy_version=excluded.strategy_version,"
        " strategy_checksum=excluded.strategy_checksum,"
        " cycle_id=excluded.cycle_id")
try:
    conn.execute(stmt, ("acct", "2026-09-02", "600000", "pending", "t2",
                        "acct", 3, "abc", 8))
    print("Q1 both triggers installed -> refresh:", "OK")
except sqlite3.Error as exc:
    print("Q1 both triggers installed -> refresh ABORTS:", type(exc).__name__, exc)

# and: if only the stamp columns are dropped from SET, does it survive?
conn.rollback()
stmt2 = ("INSERT INTO paper_signals(account_id,signal_date,code,status,created_at,"
         "strategy_id,strategy_version,strategy_checksum,cycle_id)"
         " VALUES(?,?,?,?,?,?,?,?,?)"
         " ON CONFLICT(account_id,signal_date,code) DO UPDATE SET"
         " status=excluded.status")
try:
    conn.execute(stmt2, ("acct", "2026-09-02", "600000", "pending", "t2",
                         "acct", 3, "abc", 8))
    row = conn.execute(
        "SELECT status, strategy_id, strategy_version, cycle_id FROM paper_signals"
        " WHERE account_id='acct' AND code='600000'"
    ).fetchone()
    print("Q1 refresh WITHOUT provenance columns in SET ->", "OK", row)
except sqlite3.Error as exc:
    print("Q1 refresh WITHOUT provenance columns ABORTS:", type(exc).__name__, exc)
conn.close()

# ---- Q2/Q3: who references the renamed parents, and in what order? ----
print()
SRC = (BACKEND / "selection_tracking.py").read_text(encoding="utf-8")
PSRC = (BACKEND / "paper_selection.py").read_text(encoding="utf-8")
for label, text, parent in (
    ("familyB selection_runs", SRC, "selection_runs"),
    ("familyA paper_selection_runs", PSRC, "paper_selection_runs"),
    ("familyA paper_selection_picks", PSRC, "paper_selection_picks"),
):
    refs = re.findall(r"REFERENCES\s+(\w+)", text)
    children = sorted({r for r in refs if r == parent})
    print(f"Q2 {label}: children declaring FK to it = {children or 'none'}")

# family A picks DDL: does run_id carry a REFERENCES clause?
m = re.search(r"def _picks_ddl\(.*?\n(.*?)\n\n", PSRC, re.S)
print("Q2 family A picks DDL contains REFERENCES:",
      "REFERENCES" in (m.group(1) if m else ""))

# order in family B
order = re.findall(r"(RENAME TO|DROP TABLE|CREATE TABLE|INSERT INTO)\s*\w*",
                   SRC[SRC.index("def _migrate_runs"):SRC.index("def _ensure_provenance_guards")])
print("Q3 family B _migrate_runs statement order:", order[:8])
