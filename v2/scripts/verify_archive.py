#!/usr/bin/env python3
"""Verify archive artifacts: one ledger backup plus one evolution export.

Two modes:

* single pair:   --db FILE --export FILE
* directory:     --dir DIR   (pairs ``<name>.sqlite3`` with ``<name>.export.json``
                             or ``<name>.json``; an optional ``<name>.manifest.json``
                             sidecar is cross-checked)

Combines db_tool (integrity, schema, table counts) and verify_export (canonical
digest, count/event agreement) and prints a manifest digest covering both file
digests. Read-only; no server required.

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


def check_pair(db_path: Path, export_path: Path) -> tuple[list[str], dict, dict, str]:
    """Return (problems, db facts, export document, manifest) for one pair."""
    problems: list[str] = []
    db_facts: dict = {}
    document: dict = {}
    manifest = ""

    if not db_path.is_file():
        return [f"database not found: {db_path}"], db_facts, document, manifest
    try:
        db_facts = db_tool.inspect(db_path)
    except Exception as error:  # noqa: BLE001 - reported, never raised onward
        return [f"database unreadable: {error}"], db_facts, document, manifest
    if db_facts["integrity"] != "ok":
        problems.append(f"database integrity: {db_facts['integrity']}")
    if db_facts["schema_version"] <= 0:
        problems.append("database has no V2 schema version")

    if not export_path.is_file():
        problems.append(f"export not found: {export_path}")
        return problems, db_facts, document, manifest
    try:
        document = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        problems.append(f"export unreadable: {error}")
        return problems, db_facts, document, manifest
    if not isinstance(document, dict):
        problems.append("export is not a JSON object")
    else:
        problems.extend(verify_export.verify(document))

    if not problems:
        manifest = manifest_digest({
            "db": file_digest(db_path),
            "export": file_digest(export_path),
        })
    return problems, db_facts, document, manifest


def write_sidecar(path: Path, db_path: Path, export_path: Path, facts: dict, document: dict, manifest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "db": {"path": db_path.name, "sha256": file_digest(db_path)},
        "export": {"path": export_path.name, "sha256": file_digest(export_path)},
        "schema_version": facts["schema_version"],
        "export_count": document["count"],
        "manifest": manifest,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def export_for(db_path: Path) -> Path | None:
    stem = db_path.with_suffix("")
    for candidate in (Path(str(stem) + ".export.json"), Path(str(stem) + ".json")):
        if candidate.is_file():
            return candidate
    return None


def sidecar_for(db_path: Path) -> Path:
    return Path(str(db_path.with_suffix("")) + ".manifest.json")


def run_single(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    export_path = Path(args.export)
    if not db_path.is_file():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 2
    if not export_path.is_file():
        print(f"error: export not found: {export_path}", file=sys.stderr)
        return 2
    problems, facts, document, manifest = check_pair(db_path, export_path)
    if problems:
        for problem in problems:
            print(f"INVALID: {problem}", file=sys.stderr)
        return 1
    if args.manifest_out:
        write_sidecar(Path(args.manifest_out), db_path, export_path, facts, document, manifest)
    print(
        f"archive ok schema={facts['schema_version']} accounts={facts['accounts']} "
        f"events={facts['events']} export_count={document['count']} manifest={manifest}"
    )
    return 0


def run_directory(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    dbs = sorted(root.glob("*.sqlite3"))
    if not dbs:
        print(f"error: no *.sqlite3 found in {root}", file=sys.stderr)
        return 2
    failed = 0
    for db_path in dbs:
        export_path = export_for(db_path)
        if export_path is None:
            print(f"INVALID: {db_path.name}: no matching export", file=sys.stderr)
            failed += 1
            continue
        problems, facts, document, manifest = check_pair(db_path, export_path)
        sidecar = sidecar_for(db_path)
        if not problems and sidecar.is_file():
            declared = json.loads(sidecar.read_text(encoding="utf-8")).get("manifest")
            if declared != manifest:
                problems.append(f"sidecar manifest mismatch: {declared} != {manifest}")
        if problems:
            for problem in problems:
                print(f"INVALID: {db_path.name}: {problem}", file=sys.stderr)
            failed += 1
            continue
        print(f"{db_path.name} ok manifest={manifest}")
    if failed:
        print(f"{failed} of {len(dbs)} archives failed", file=sys.stderr)
        return 1
    print(f"all {len(dbs)} archives verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="ledger backup (.sqlite3)")
    parser.add_argument("--export", help="evolution export (.json)")
    parser.add_argument("--dir", dest="directory", help="verify every <name>.sqlite3 pair in this directory")
    parser.add_argument("--manifest-out", help="write a JSON manifest sidecar here (single mode)")
    args = parser.parse_args(argv)

    if args.directory:
        if args.db or args.export:
            print("error: --dir cannot be combined with --db/--export", file=sys.stderr)
            return 2
        return run_directory(args)
    if not args.db or not args.export:
        print("error: provide --db and --export, or --dir", file=sys.stderr)
        return 2
    return run_single(args)


if __name__ == "__main__":
    raise SystemExit(main())
