#!/usr/bin/env python3
"""Generate bounded strategy-parameter candidates from a reviewed base artifact.

Standard library only. This script never writes the ledger, never evaluates
performance and never promotes anything: it only emits a deterministic manifest
that the shadow pipeline can consume. Candidate identity is the SHA-256 of the
canonical parameter JSON, matching the Rust kernel's canonical digest.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1
PARAMETER_BOUNDS = {
    "lookback": (1, 250),
    "threshold_bps": (1, 5_000),
}
MAX_CANDIDATES = 32


def canonical_text(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(value) -> str:
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def load_base(path: Path) -> dict:
    base = json.loads(path.read_text(encoding="utf-8"))
    for field in ("strategy_version_id", "parameters_canonical", "parameters_digest"):
        if field not in base:
            raise ValueError(f"base_missing_field:{field}")
    parameters = json.loads(base["parameters_canonical"])
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("base_parameters_not_an_object")
    if digest_of(parameters) != base["parameters_digest"]:
        raise ValueError("base_parameters_digest_mismatch")
    return base


def load_grid(path: Path) -> dict:
    grid = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(grid, dict) or not grid:
        raise ValueError("grid_not_an_object")
    for name, values in grid.items():
        if name not in PARAMETER_BOUNDS:
            raise ValueError(f"unknown_parameter:{name}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"empty_parameter_values:{name}")
        low, high = PARAMETER_BOUNDS[name]
        for value in values:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"non_integer_parameter:{name}:{value}")
            if not low <= value <= high:
                raise ValueError(f"parameter_out_of_bounds:{name}:{value}")
    return grid


def build_manifest(base: dict, grid: dict, max_candidates: int) -> dict:
    base_parameters = json.loads(base["parameters_canonical"])
    for name in base_parameters:
        if name not in grid:
            raise ValueError(f"grid_missing_parameter:{name}")
    names = sorted(grid)
    total = 1
    for name in names:
        total *= len(grid[name])
    if total > max_candidates:
        raise ValueError(f"grid_too_large:{total}>{max_candidates}")

    candidates = []
    seen = set()
    for combo in itertools.product(*(grid[name] for name in names)):
        parameters = dict(zip(names, combo))
        text = canonical_text(parameters)
        if text == base["parameters_canonical"]:
            continue
        if text in seen:
            continue
        seen.add(text)
        parameters_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        candidates.append(
            {
                "candidate_id": f"{base['strategy_version_id']}-{parameters_digest[:8]}",
                "parameters_canonical": text,
                "parameters_digest": parameters_digest,
            }
        )
    if not candidates:
        raise ValueError("no_candidates_after_dedup")
    candidates.sort(key=lambda item: item["candidate_id"])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "base_strategy_version_id": base["strategy_version_id"],
        "base_parameters_digest": base["parameters_digest"],
        "candidates": candidates,
    }
    manifest["digest"] = digest_of(manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--grid", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES)
    args = parser.parse_args(argv)
    try:
        base = load_base(args.base)
        grid = load_grid(args.grid)
        manifest = build_manifest(base, grid, args.max_candidates)
    except (ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {args.out} candidates={len(manifest['candidates'])} "
        f"digest={manifest['digest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
