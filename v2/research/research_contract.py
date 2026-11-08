"""Content identity for versioned research: strategy artifacts, frozen inputs
and reproducible research outputs.

Standard library only. This module never opens the trading ledger, never reads a
clock, never decides order quantity, price, fees or cash, and never executes
caller-supplied code. It is the *producer* half of the contract; the Rust kernel
is the verifying half and never imports Python.

Canonicalization: UTF-8, keys sorted, compact separators, no trailing spaces.
Rust reproduces the same text through `serde_json` (BTreeMap key order) and
recomputes every digest from repository bytes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 2
ALGORITHM_VERSION = "research-signal-v1"
RUNTIME_VERSION = "python-3.13-stdlib"

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STRATEGY_SOURCES = ROOT / "research" / "strategies"


def canonical_json(value) -> str:
    """The single canonical text form shared with the Rust verifier."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_hex(value) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return hashlib.sha256(data).hexdigest()


def normalize_newlines(text: str) -> str:
    """Source bytes are hashed after newline normalization so that a CRLF
    working tree and an LF checkout produce one identity."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_source_text(path) -> str:
    return normalize_newlines(Path(path).read_bytes().decode("utf-8"))


def read_json(path):
    return json.loads(read_source_text(path))


def write_json(path, value) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
# Frozen research input
# ---------------------------------------------------------------------------
_INPUT_IDENTITY_FIELDS = ("entries", "market_data_pack_id", "research_input_id", "schema_version")


def input_projection(manifest) -> dict:
    return {name: manifest[name] for name in _INPUT_IDENTITY_FIELDS}


def input_digest(manifest) -> str:
    return sha256_hex(canonical_json(input_projection(manifest)))


def verify_input(manifest) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_research_input_version")
    if manifest.get("content_digest") != input_digest(manifest):
        raise ValueError("research_input_content_digest_mismatch")
    sequences = [entry["sequence"] for entry in manifest["entries"]]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise ValueError("research_input_sequence_not_monotonic")
    decision_times = [entry["decision_at"] for entry in manifest["entries"]]
    if decision_times != sorted(decision_times):
        raise ValueError("research_input_decision_times_not_ordered")


def level_for(entry, side: str) -> dict:
    if side not in ("buy", "sell"):
        raise ValueError("unsupported_research_side")
    return entry[side]


# ---------------------------------------------------------------------------
# Strategy artifact
# ---------------------------------------------------------------------------
ARTIFACT_FIELDS = (
    "schema_version", "strategy_version_id", "research_input_id", "algorithm_version",
    "source_digest", "parameters_canonical", "parameters_digest",
    "research_input_digest", "random_seed", "runtime_version",
)


def build_artifact(strategy_version_id: str, parameters, source_text: str, manifest,
                   *, random_seed=None, runtime_version: str = RUNTIME_VERSION) -> dict:
    parameters_canonical = canonical_json(parameters)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "strategy_version_id": strategy_version_id,
        "research_input_id": manifest["research_input_id"],
        "algorithm_version": ALGORITHM_VERSION,
        "source_digest": sha256_hex(normalize_newlines(source_text)),
        "parameters_canonical": parameters_canonical,
        "parameters_digest": sha256_hex(parameters_canonical),
        "research_input_digest": input_digest(manifest),
        "random_seed": random_seed,
        "runtime_version": runtime_version,
    }


def verify_artifact(artifact, source_text: str, manifest) -> None:
    missing = [name for name in ARTIFACT_FIELDS if name not in artifact]
    if missing:
        raise ValueError("strategy_artifact_missing_fields:" + ",".join(missing))
    if artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported_strategy_artifact_version")
    if artifact["algorithm_version"] != ALGORITHM_VERSION:
        raise ValueError("unsupported_research_algorithm")
    if artifact["runtime_version"] != RUNTIME_VERSION:
        raise ValueError("unsupported_research_runtime")
    if artifact["parameters_canonical"] != canonical_json(json.loads(artifact["parameters_canonical"])):
        raise ValueError("strategy_parameters_not_canonical")
    if artifact["parameters_digest"] != sha256_hex(artifact["parameters_canonical"]):
        raise ValueError("strategy_parameters_digest_mismatch")
    if artifact["source_digest"] != sha256_hex(normalize_newlines(source_text)):
        raise ValueError("strategy_source_digest_mismatch")
    if artifact["research_input_id"] != manifest["research_input_id"]:
        raise ValueError("strategy_research_input_identity_mismatch")
    if artifact["research_input_digest"] != input_digest(manifest):
        raise ValueError("strategy_research_input_digest_mismatch")


# ---------------------------------------------------------------------------
# Research output
# ---------------------------------------------------------------------------
_OUTPUT_IDENTITY_FIELDS = (
    "algorithm_digest", "algorithm_version", "parameters_digest",
    "research_input_digest", "research_input_id", "research_output_id",
    "schema_version", "signals", "strategy_version_id",
)
SIGNAL_FIELDS = (
    "decision_at", "market_data_pack_id", "quote_id", "sequence", "side",
    "signal_id", "source", "symbol",
)


def output_projection(output) -> dict:
    return {name: output[name] for name in _OUTPUT_IDENTITY_FIELDS}


def output_digest(output) -> str:
    return sha256_hex(canonical_json(output_projection(output)))


def verify_output(output) -> None:
    if output.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_research_output_version")
    for signal in output["signals"]:
        if tuple(sorted(signal)) != tuple(sorted(SIGNAL_FIELDS)):
            raise ValueError("research_signal_has_unsupported_fields")
        if signal["side"] not in ("buy", "sell"):
            raise ValueError("research_signal_has_unsupported_side")
    if output.get("content_digest") != output_digest(output):
        raise ValueError("research_output_content_digest_mismatch")


def signal_id(strategy_version_id: str, sequence: int, side: str) -> str:
    return f"{strategy_version_id}-{sequence:04d}-{side}"


def build_output(strategy_version_id: str, artifact: dict, manifest, strategy) -> dict:
    signals = [{
        "decision_at": entry["decision_at"],
        "market_data_pack_id": manifest["market_data_pack_id"],
        "quote_id": entry["quote"]["quote_id"],
        "sequence": entry["sequence"],
        "side": side,
        "signal_id": signal_id(strategy_version_id, entry["sequence"], side),
        "source": entry["quote"]["source"],
        "symbol": entry["quote"]["symbol"],
    } for entry, side in strategy.signals(
        manifest["entries"], json.loads(artifact["parameters_canonical"]))]
    output = {
        "schema_version": SCHEMA_VERSION,
        "research_output_id": f"research-output-{strategy_version_id}",
        "strategy_version_id": strategy_version_id,
        "research_input_id": manifest["research_input_id"],
        "algorithm_version": artifact["algorithm_version"],
        "algorithm_digest": artifact["source_digest"],
        "parameters_digest": artifact["parameters_digest"],
        "research_input_digest": artifact["research_input_digest"],
        "signals": signals,
    }
    output["content_digest"] = output_digest(output)
    return output
