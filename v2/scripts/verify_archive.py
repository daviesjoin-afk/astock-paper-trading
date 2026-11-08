#!/usr/bin/env python3
"""Verify an archive set: ledger backup + evolution export in one pass.

Combines db_tool (integrity, schema, table counts) and verify_export (canonical
digest, count/event agreement) and prints one manifest digest covering both
file digests. Read-only; no server required.

Exit codes: 0 verified, 1 any artifact invalid, 2 usage or unreadable input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db_tool  # noqa: E402
import verify_export  # noqa: E402


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_digest(entries: dict[str, str]) -> str:
    text = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="ledger backup (.sqlite3)")
    parser.add_argument("--export", required=True, help="evolution export (.json)")
    args = parser.parse_args(argv)

    problems: list[str] = []

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 2
    try:
        db_facts = db_tool.inspect(db_path)
    except Exception as error:  # noqa: BLE001 - reported, never raised onward
        print(f"INVALID: database unreadable: {error}", file=sys.stderr)
        return 1
    if db_facts["integrity"] != "ok":
        problems.append(f"database integrity: {db_facts['integrity']}")
    if db_facts["schema_version"] <= 0:
        problems.append("database has no V2 schema version")

    export_path = Path(args.export)
    if not export_path.is_file():
        print(f"error: export not found: {export_path}", file=sys.stderr)
        return 2
    try:
        document = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"INVALID: export unreadable: {error}", file=sys.stderr)
        return 1
    if not isinstance(document, dict):
        problems.append("export is not a JSON object")
    else:
        problems.extend(verify_export.verify(document))

    if problems:
        for problem in problems:
            print(f"INVALID: {problem}", file=sys.stderr)
        return 1

    manifest = manifest_digest({
        "db": file_digest(db_path),
        "export": file_digest(export_path),
    })
    print(
        f"archive ok schema={db_facts['schema_version']} accounts={db_facts['accounts']} "
        f"events={db_facts['events']} export_count={document['count']} "
        f"manifest={manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
