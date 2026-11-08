"""Tests for candidate artifact emission."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import emit_candidate_artifacts as emitter
import generate_candidates as generator
import research_contract as contract
import run_research


class CandidateArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        base = contract.read_json(contract.DATA / "strategy-momentum-v1.json")
        self.manifest = generator.build_manifest(
            base, {"lookback": [2, 10], "threshold_bps": [25]}, 32
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_emits_self_consistent_artifacts_and_signals(self) -> None:
        written = emitter.emit(self.manifest, self.dir / "out")
        self.assertEqual(len(written), 1, written)
        record = written[0]
        artifact = contract.read_json(self.dir / "out" / record["artifact"])
        output = contract.read_json(self.dir / "out" / record["output"])
        source_text = contract.read_source_text(contract.STRATEGY_SOURCES / "momentum.py")
        manifest_input = contract.read_json(run_research.DEFAULT_INPUT)
        contract.verify_artifact(artifact, source_text, manifest_input)
        contract.verify_output(output)
        self.assertEqual(artifact["strategy_version_id"], record["candidate_id"])
        self.assertEqual(artifact["parameters_digest"], record["parameters_digest"])
        # Same reviewed source, only parameters differ.
        base = contract.read_json(contract.DATA / "strategy-momentum-v1.json")
        self.assertEqual(artifact["source_digest"], base["source_digest"])
        self.assertNotEqual(artifact["parameters_digest"], base["parameters_digest"])

        # A lookback longer than the frozen input removes every signal.
        base_output = contract.read_json(contract.DATA / "research-output-momentum-v1.json")
        self.assertGreater(len(base_output["signals"]), 0)
        self.assertEqual(output["signals"], [])

    def test_emission_is_byte_deterministic(self) -> None:
        first = emitter.emit(self.manifest, self.dir / "a")
        second = emitter.emit(self.manifest, self.dir / "b")
        self.assertEqual(first, second)
        for record in first:
            left = (self.dir / "a" / record["output"]).read_bytes()
            right = (self.dir / "b" / record["output"]).read_bytes()
            self.assertEqual(left, right)

    def test_unknown_base_strategy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown_base_strategy"):
            emitter.strategy_module("research-invented-v1")

    def test_cli_reports_manifest(self) -> None:
        manifest_path = self.dir / "candidates.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        out = self.dir / "cli-out"
        self.assertEqual(
            emitter.main(["--manifest", str(manifest_path), "--out", str(out)]), 0
        )
        self.assertTrue(any(out.glob("*.artifact.json")))
        self.assertTrue(any(out.glob("*.output.json")))


if __name__ == "__main__":
    unittest.main()
