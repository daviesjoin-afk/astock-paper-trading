#!/usr/bin/env python3
"""Verify an evolution-event export bundle against its own digest.

The bundle digest is the SHA-256 of canonical JSON (sorted keys, compact
separators) over the document with `digest` removed — the same rule the Rust
kernel uses. This script never talks to the server; it only re-checks bytes.

Exit codes: 0 verified, 1 mismatch, 2 usage or unreadable input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def canonical_digest(document: dict) -> str:
    body = {key: value for key, value in document.items() if key != "digest"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(document: dict) -> list[str]:
    problems: list[str] = []
    events = document.get("events")
    if not isinstance(events, list):
        problems.append("events must be a list")
        events = []
    count = document.get("count")
    if count != len(events):
        problems.append(f"count {count!r} does not match {len(events)} events")
    declared = document.get("digest")
    if not isinstance(declared, str) or len(declared) != 64:
        problems.append("digest must be a 64-character hex string")
    else:
        actual = canonical_digest(document)
        if actual != declared:
            problems.append(f"digest mismatch: declared {declared}, recomputed {actual}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="path to an exported JSON bundle")
    args = parser.parse_args(argv)
    try:
        document = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not isinstance(document, dict):
        print("error: export must be a JSON object", file=sys.stderr)
        return 2
    problems = verify(document)
    if problems:
        for problem in problems:
            print(f"INVALID: {problem}", file=sys.stderr)
        return 1
    print(f"verified count={document['count']} digest={document['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
