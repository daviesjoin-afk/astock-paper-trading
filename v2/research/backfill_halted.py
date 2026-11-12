#!/usr/bin/env python3
"""Backfill explicit halted rows into a research-database copy.

The original research ingest predated the valuation-only halt rule and dropped
zero-quote A-share rows. This reads the official TDX daytime package for each
market day, keeps only rows the current parser marks `tradable=0`, and inserts
those that the copy lacks. Existing zero-volume/zero-OHLC rows are reconciled
to the official reference and marked halted. A conflicting traded row fails.

The output is a JSON manifest with package hashes, inserted counts, and the
copy's before/after hashes. Run against a closed research copy with the current
bar.tradable schema, never an active ledger or the source database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_sources"))

from ingest_market import IngestError, fetch_package, parse_package  # noqa: E402


INSERT_SQL = """
INSERT INTO bar(symbol,day,name,open_cents,high_cents,low_cents,close_cents,
                prev_close_cents,volume_shares,amount_cents,adjust,source,tradable)
VALUES(?,?,?,?,?,?,?,?,?,?,'none','tdx',0)
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "days": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def fetch(day: str, package_dir: Path) -> tuple[bytes, str]:
    cached = package_dir / f"{day}.zip"
    if cached.exists():
        content = cached.read_bytes()
    else:
        content = fetch_package(day)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(content)
    return content, hashlib.sha256(content).hexdigest()


def backfill(db_path: Path, manifest_path: Path, package_dir: Path) -> dict:
    manifest = load_manifest(manifest_path)
    connection = sqlite3.connect(db_path, timeout=60)
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        days = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT day FROM bar WHERE adjust='none' AND close_cents>0 ORDER BY day"
            )
        ]
        for day in days:
            previous = manifest["days"].get(day)
            if previous and previous.get("status") in ("done", "no_halted"):
                continue
            started = time.monotonic()
            try:
                content, digest = fetch(day, package_dir)
                rows = parse_package(content, day.replace("-", ""))
            except (IngestError, OSError, urllib.error.URLError) as error:
                manifest["days"][day] = {
                    "status": "failed",
                    "error": str(error),
                }
                save_manifest(manifest_path, manifest)
                print(f"{day} FAILED {error}", file=sys.stderr)
                continue
            halted = [row for row in rows if not row.tradable]
            existing = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT symbol,open_cents,high_cents,low_cents,volume_shares,"
                    "close_cents,prev_close_cents,tradable FROM bar WHERE day=?", (day,)
                )
            }
            missing = [row for row in halted if row.symbol not in existing]
            updates = []
            for row in halted:
                if row.symbol not in existing:
                    continue
                stored = existing[row.symbol]
                if any(stored[:4]):
                    raise ValueError(
                        f"official halt conflicts with traded source row: {row.symbol}:{day}"
                    )
                if stored[4:] != (row.close_cents, row.prev_close_cents, 0):
                    updates.append(row)
            with connection:
                connection.executemany(
                    "UPDATE bar SET close_cents=?,prev_close_cents=?,tradable=0 "
                    "WHERE symbol=? AND day=?",
                    [(row.close_cents, row.prev_close_cents, row.symbol, day)
                     for row in updates],
                )
                connection.executemany(
                    INSERT_SQL,
                    [
                        (
                            row.symbol,
                            row.day,
                            row.name,
                            row.open_cents,
                            row.high_cents,
                            row.low_cents,
                            row.close_cents,
                            row.prev_close_cents,
                            row.volume_shares,
                            row.amount_cents,
                        )
                        for row in missing
                    ],
                )
            manifest["days"][day] = {
                "status": "done" if halted else "no_halted",
                "package_sha256": digest,
                "parsed_rows": len(rows),
                "halted_rows": len(halted),
                "inserted": len(missing),
                "updated": len(updates),
                "seconds": round(time.monotonic() - started, 3),
            }
            save_manifest(manifest_path, manifest)
            print(
                f"{day} parsed={len(rows)} halted={len(halted)} "
                f"inserted={len(missing)} updated={len(updates)}",
                flush=True,
            )
    finally:
        connection.close()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="Closed research copy with bar.tradable; never the source or active ledger")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--packages", required=True, type=Path)
    args = parser.parse_args()
    if not args.db.exists():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 2
    before = sha256(args.db)
    manifest = backfill(args.db, args.manifest, args.packages)
    after = sha256(args.db)
    manifest["database"] = str(args.db.resolve())
    manifest["database_before_sha256"] = before
    manifest["database_after_sha256"] = after
    save_manifest(args.manifest, manifest)
    failed = sum(
        entry.get("status") == "failed" for entry in manifest["days"].values()
    )
    print(f"database {before} -> {after}; failed days={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
