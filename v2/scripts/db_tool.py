#!/usr/bin/env python3
"""Backup, restore and verify the V2 SQLite database.

Uses SQLite's online backup API, so it is safe while the single writer is
running (WAL included) and needs no external sqlite3 binary.

Exit codes: 0 ok, 1 verification failure, 2 usage/IO error, 3 refused
(overwrite without --force).
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        accounts = connection.execute("SELECT COUNT(*) FROM v2_paper_accounts").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM v2_evolution_events").fetchone()[0]
    finally:
        connection.close()
    return {
        "integrity": integrity,
        "schema_version": version,
        "accounts": accounts,
        "events": events,
    }


def require_ok(path: Path, label: str) -> int:
    if not path.is_file():
        print(f"error: {label} not found: {path}", file=sys.stderr)
        return 2
    try:
        facts = inspect(path)
    except sqlite3.DatabaseError as error:
        print(f"INVALID: {label} is not a readable database: {error}", file=sys.stderr)
        return 1
    if facts["integrity"] != "ok":
        print(f"INVALID: integrity check failed: {facts['integrity']}", file=sys.stderr)
        return 1
    if facts["schema_version"] <= 0:
        print("INVALID: database has no V2 schema version", file=sys.stderr)
        return 1
    return 0


def command_backup(args: argparse.Namespace) -> int:
    source = Path(args.db)
    target = Path(args.out)
    if not source.is_file():
        print(f"error: database not found: {source}", file=sys.stderr)
        return 2
    if target.exists() and not args.force:
        print(f"refusing to overwrite backup: {target} (use --force)", file=sys.stderr)
        return 3
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    code = require_ok(target, "backup")
    if code != 0:
        return code
    facts = inspect(target)
    print(
        f"backup ok schema={facts['schema_version']} accounts={facts['accounts']} "
        f"events={facts['events']} out={target} sha256={sha256_file(target)}"
    )
    return 0


def command_restore(args: argparse.Namespace) -> int:
    backup = Path(args.backup)
    target = Path(args.db)
    code = require_ok(backup, "backup")
    if code != 0:
        return code
    if target.exists() and not args.force:
        print(f"refusing to overwrite database: {target} (use --force)", file=sys.stderr)
        return 3
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    target.write_bytes(backup.read_bytes())
    code = require_ok(target, "restored database")
    if code != 0:
        return code
    facts = inspect(target)
    print(
        f"restore ok schema={facts['schema_version']} accounts={facts['accounts']} "
        f"events={facts['events']} db={target}"
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    path = Path(args.db)
    code = require_ok(path, "database")
    if code != 0:
        return code
    facts = inspect(path)
    print(
        f"verify ok schema={facts['schema_version']} accounts={facts['accounts']} "
        f"events={facts['events']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="online backup of a live database")
    backup.add_argument("--db", required=True)
    backup.add_argument("--out", required=True)
    backup.add_argument("--force", action="store_true")
    backup.set_defaults(handler=command_backup)

    restore = sub.add_parser("restore", help="install a verified backup")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--db", required=True)
    restore.add_argument("--force", action="store_true")
    restore.set_defaults(handler=command_restore)

    verify = sub.add_parser("verify", help="integrity and schema check")
    verify.add_argument("--db", required=True)
    verify.set_defaults(handler=command_verify)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
