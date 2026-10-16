"""Optional indexed storage for unchanged raw disclosure evidence.

An explicit one-time import activates the sidecar. Until then, or after an old
version replaces the legacy JSON, the caller retains its original JSON path.
This is a provider cache, never the paper ledger or a filtered report archive.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import uuid


def _signature(path):
    stat = os.stat(path)
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns, stat.st_ctime_ns])


def _connect(path, mode):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=" + mode,
                           uri=True, timeout=3)


def _ready(conn, path):
    row = conn.execute("SELECT value FROM cache_meta WHERE key='legacy_signature'").fetchone()
    return row is not None and row[0] == _signature(path)


def load(path, codes):
    """Read only requested entries; None means use the unchanged legacy path."""
    conn = None
    try:
        conn = _connect(os.fspath(path) + ".sqlite3", "ro")
        conn.execute("BEGIN")
        if not _ready(conn, path):
            return None
        rows = {}
        codes = list(dict.fromkeys(codes))
        for start in range(0, len(codes), 400):
            batch = codes[start:start + 400]
            sql = "SELECT code,payload FROM cache_rows WHERE code IN (" + ",".join("?" for _ in batch) + ")"
            for code, payload in conn.execute(sql, batch):
                value = json.loads(payload)
                if not isinstance(value, dict):
                    return None
                rows[code] = value
        if not _ready(conn, path):
            return None
        return {"rows": rows}
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _entry(code, value):
    epoch = float(value.get("fetched_epoch", 0))
    if not math.isfinite(epoch):
        raise ValueError("invalid disclosure cache timestamp")
    return str(code), epoch, json.dumps(value, ensure_ascii=False, allow_nan=False)


_UPSERT = """INSERT INTO cache_rows(code,fetched_epoch,payload) VALUES(?,?,?)
    ON CONFLICT(code) DO UPDATE SET fetched_epoch=excluded.fetched_epoch,payload=excluded.payload
    WHERE excluded.fetched_epoch>=cache_rows.fetched_epoch"""


def save(path, rows):
    """Commit only fetched entries; failed/late writes cannot replace newer rows."""
    conn = None
    try:
        conn = _connect(os.fspath(path) + ".sqlite3", "rw")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _ready(conn, path):
                return False
            conn.executemany(_UPSERT, (_entry(code, value) for code, value in rows.items()))
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False
    finally:
        if conn is not None:
            conn.close()


def migrate(path):
    """Import once, verify every entry, then publish; never replace a live store.

    Run while old provider writers are stopped/serialized. The legacy file is
    retained byte-for-byte, including original fetch/publication timestamps.
    """
    path = os.fspath(path)
    destination = path + ".sqlite3"
    if os.path.exists(destination):
        raise FileExistsError(destination)
    signature = _signature(path)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("rows")
    if not isinstance(rows, dict):
        raise ValueError("disclosure cache rows must be a mapping")
    temporary = destination + "." + uuid.uuid4().hex + ".tmp"
    conn = None
    try:
        conn = sqlite3.connect(temporary)
        with conn:
            conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("""CREATE TABLE cache_rows(code TEXT PRIMARY KEY,
                         fetched_epoch REAL NOT NULL,payload TEXT NOT NULL) WITHOUT ROWID""")
            conn.executemany(_UPSERT, (_entry(code, value) for code, value in rows.items()))
            # Verify raw nested evidence rather than just counts.
            for code, raw in conn.execute("SELECT code,payload FROM cache_rows"):
                if json.loads(raw) != rows[code]:
                    raise ValueError("disclosure import evidence mismatch")
            if conn.execute("SELECT COUNT(*) FROM cache_rows").fetchone()[0] != len(rows):
                raise ValueError("disclosure import count mismatch")
            conn.execute("INSERT INTO cache_meta VALUES('legacy_signature',?)", (signature,))
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("disclosure import integrity failure")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        conn = None
        if _signature(path) != signature:
            raise ValueError("legacy disclosure cache changed during import")
        # Hard link is atomic and refuses to overwrite a concurrently published store.
        os.link(temporary, destination)
        return {"rows": len(rows), "verified": True}
    finally:
        if conn is not None:
            conn.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
