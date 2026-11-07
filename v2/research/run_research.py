"""Regenerate the reviewed research output from a reviewed strategy definition
and a frozen research input.

Usage (from v2/):
    python research/run_research.py --strategy momentum --check
    python research/run_research.py --strategy momentum --output /tmp/output.json
    python research/run_research.py --strategy momentum --emit   # maintainers only

`--check` is the reproducibility gate: it recomputes the artifact and the
research output from repository bytes and requires them to equal the reviewed
files, byte for byte. No clock, no network, no ledger access, no randomness.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import research_contract as contract  # noqa: E402
from strategies import momentum, reversion  # noqa: E402

STRATEGIES = {"momentum": momentum, "reversion": reversion}
DEFAULT_INPUT = contract.DATA / "research-input-offline-v1.json"


def reviewed_paths(name: str) -> tuple[Path, Path]:
    strategy = STRATEGIES[name]
    stem = strategy.STRATEGY_VERSION_ID.removeprefix("research-").removesuffix("-v1")
    return (contract.DATA / f"strategy-{stem}-v1.json",
            contract.DATA / f"research-output-{stem}-v1.json")


def regenerate(name: str, input_path: Path = DEFAULT_INPUT) -> tuple[dict, dict]:
    """Return (artifact, research output) recomputed from repository bytes."""
    strategy = STRATEGIES[name]
    manifest = contract.read_json(input_path)
    contract.verify_input(manifest)
    source = contract.read_source_text(contract.STRATEGY_SOURCES / f"{name}.py")
    artifact = contract.build_artifact(
        strategy.STRATEGY_VERSION_ID, strategy.PARAMETERS, source, manifest)
    contract.verify_artifact(artifact, source, manifest)
    output = contract.build_output(strategy.STRATEGY_VERSION_ID, artifact, manifest, strategy)
    contract.verify_output(output)
    return artifact, output


def check(name: str, input_path: Path = DEFAULT_INPUT) -> dict:
    """Fail closed when the reviewed files drifted from repository bytes."""
    artifact_path, output_path = reviewed_paths(name)
    artifact, output = regenerate(name, input_path)
    reviewed_artifact = contract.read_json(artifact_path)
    reviewed_output = contract.read_json(output_path)
    if reviewed_artifact != artifact:
        raise SystemExit(f"reviewed strategy artifact drifted: {artifact_path}")
    if reviewed_output != output:
        raise SystemExit(f"reviewed research output drifted: {output_path}")
    return {"strategy_version_id": artifact["strategy_version_id"],
            "research_input_id": artifact["research_input_id"],
            "research_input_digest": artifact["research_input_digest"],
            "parameters_digest": artifact["parameters_digest"],
            "source_digest": artifact["source_digest"],
            "research_output_id": output["research_output_id"],
            "research_output_digest": output["content_digest"],
            "signals": len(output["signals"])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=sorted(STRATEGIES), required=True)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--emit", action="store_true",
                        help="rewrite reviewed data files from current repository bytes")
    args = parser.parse_args()
    if args.emit:
        artifact_path, output_path = reviewed_paths(args.strategy)
        artifact, output = regenerate(args.strategy, args.input)
        contract.write_json(artifact_path, artifact)
        contract.write_json(output_path, output)
        print(f"Wrote {artifact_path.name} and {output_path.name}")
        return
    if args.check:
        print(json.dumps(check(args.strategy, args.input), ensure_ascii=False, indent=2))
        return
    _, output = regenerate(args.strategy, args.input)
    text = json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    print(text, end="")


if __name__ == "__main__":
    main()
