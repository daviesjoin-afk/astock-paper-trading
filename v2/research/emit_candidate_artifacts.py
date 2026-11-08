#!/usr/bin/env python3
"""Turn a candidate manifest into self-consistent candidate artifacts + signals.

A candidate reuses the reviewed strategy source (same source digest) but carries
its own parameters and identity. Outputs are produced by the same contract as
the reviewed ones, so a shadow experiment can consume them without a new
runtime.

This script never edits reviewed files, never evaluates P&L and never promotes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import research_contract as contract  # noqa: E402
import run_research  # noqa: E402


def strategy_module(base_strategy_version_id: str):
    for name, module in run_research.STRATEGIES.items():
        if module.STRATEGY_VERSION_ID == base_strategy_version_id:
            return name, module
    raise ValueError(f"unknown_base_strategy:{base_strategy_version_id}")


def emit(
    manifest: dict,
    out_dir: Path,
    input_path: Path = run_research.DEFAULT_INPUT,
) -> list[dict]:
    name, module = strategy_module(manifest["base_strategy_version_id"])
    manifest_input = contract.read_json(input_path)
    contract.verify_input(manifest_input)
    source_text = contract.read_source_text(contract.STRATEGY_SOURCES / f"{name}.py")
    base_artifact = contract.read_json(
        contract.DATA / f"strategy-{module.STRATEGY_VERSION_ID.removeprefix('research-').removesuffix('-v1')}-v1.json"
    )
    contract.verify_artifact(base_artifact, source_text, manifest_input)

    written = []
    for candidate in manifest["candidates"]:
        artifact = dict(base_artifact)
        artifact["strategy_version_id"] = f"shadow-{candidate['candidate_id']}"
        artifact["parameters_canonical"] = candidate["parameters_canonical"]
        artifact["parameters_digest"] = candidate["parameters_digest"]
        parameters = json.loads(candidate["parameters_canonical"])
        contract.verify_artifact(artifact, source_text, manifest_input)
        output = contract.build_output(
            artifact["strategy_version_id"], artifact, manifest_input, module
        )
        contract.verify_output(output)
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = out_dir / f"{candidate['candidate_id']}.artifact.json"
        output_path = out_dir / f"{candidate['candidate_id']}.output.json"
        contract.write_json(artifact_path, artifact)
        contract.write_json(output_path, output)
        written.append(
            {
                "candidate_id": artifact["strategy_version_id"],
                "parameters_digest": candidate["parameters_digest"],
                "signals": len(output["signals"]),
                "artifact": artifact_path.name,
                "output": output_path.name,
            }
        )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--input", type=Path, default=run_research.DEFAULT_INPUT)
    args = parser.parse_args(argv)
    try:
        manifest = contract.read_json(args.manifest)
        written = emit(manifest, args.out, args.input)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"candidates": written}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
