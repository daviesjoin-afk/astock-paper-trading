"""Reproducibility contract tests for the fixed research path.

These are the *redo* tests: they regenerate research identity and research
signals from reviewed strategy sources plus a frozen input and require byte
equality with the committed artifacts. The kernel's replay tests are a
different proof and live in the Rust suite.

Run from v2/: python -m unittest discover -s research -p 'test_*.py' -v
"""
import json
import unittest
from pathlib import Path

import research_contract as contract
import run_research

FORBIDDEN_SIGNAL_KEYS = {
    "quantity", "shares", "amount", "cash", "cash_cents", "price_cents",
    "fees", "fees_cents", "fill", "fills", "initial_cash_cents",
}

MOMENTUM = "research-momentum-v1"
REVERSION = "research-reversion-v1"
INPUT = contract.DATA / "research-input-offline-v1.json"


def walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


class ReviewedDataTests(unittest.TestCase):
    def test_every_reviewed_digest_matches_repository_bytes(self):
        manifest = contract.read_json(INPUT)
        contract.verify_input(manifest)
        for name in ("momentum", "reversion"):
            artifact_path, output_path = run_research.reviewed_paths(name)
            artifact = contract.read_json(artifact_path)
            source = contract.read_source_text(contract.STRATEGY_SOURCES / f"{name}.py")
            contract.verify_artifact(artifact, source, manifest)
            output = contract.read_json(output_path)
            contract.verify_output(output)
            self.assertEqual(output["algorithm_digest"], artifact["source_digest"])
            self.assertEqual(output["parameters_digest"], artifact["parameters_digest"])
            self.assertEqual(output["research_input_digest"], manifest["content_digest"])

    def test_reviewed_files_equal_a_fresh_regeneration(self):
        for name in ("momentum", "reversion"):
            artifact_path, output_path = run_research.reviewed_paths(name)
            artifact, output = run_research.regenerate(name)
            self.assertEqual(artifact, contract.read_json(artifact_path), name)
            self.assertEqual(output, contract.read_json(output_path), name)

    def test_two_regenerations_are_byte_identical(self):
        for name in ("momentum", "reversion"):
            first = run_research.regenerate(name)
            second = run_research.regenerate(name)
            self.assertEqual(contract.canonical_json(first[0]),
                             contract.canonical_json(second[0]))
            self.assertEqual(contract.canonical_json(first[1]),
                             contract.canonical_json(second[1]))
            self.assertEqual(first[1]["content_digest"], second[1]["content_digest"])

    def test_check_reports_the_frozen_identity(self):
        report = run_research.check("momentum")
        self.assertEqual(report["strategy_version_id"], MOMENTUM)
        self.assertEqual(report["research_input_id"], "offline-research-input-v1")
        self.assertEqual(report["signals"], 2)


class ResearchSignalTests(unittest.TestCase):
    def test_signals_carry_identity_and_direction_only(self):
        for name in ("momentum", "reversion"):
            output = run_research.regenerate(name)[1]
            for signal in output["signals"]:
                self.assertEqual(set(signal), set(contract.SIGNAL_FIELDS))
                self.assertFalse(FORBIDDEN_SIGNAL_KEYS & set(walk_keys(signal)),
                                 f"{name} leaked trading authority: {signal}")
                self.assertIn(signal["side"], ("buy", "sell"))

    def test_signals_follow_the_frozen_entries_in_order(self):
        manifest = contract.read_json(INPUT)
        by_quote = {entry["quote"]["quote_id"]: entry for entry in manifest["entries"]}
        for name in ("momentum", "reversion"):
            output = run_research.regenerate(name)[1]
            sequences = [signal["sequence"] for signal in output["signals"]]
            self.assertEqual(sequences, sorted(sequences))
            for signal in output["signals"]:
                entry = by_quote[signal["quote_id"]]
                self.assertEqual(signal["decision_at"], entry["decision_at"])
                self.assertEqual(signal["symbol"], entry["quote"]["symbol"])
                self.assertEqual(signal["market_data_pack_id"], manifest["market_data_pack_id"])
                self.assertEqual(signal["signal_id"],
                                 contract.signal_id(output["strategy_version_id"],
                                                    signal["sequence"], signal["side"]))

    def test_the_two_reviewed_strategies_differ_on_the_same_input(self):
        momentum = run_research.regenerate("momentum")[1]
        reversion = run_research.regenerate("reversion")[1]
        self.assertEqual(momentum["research_input_digest"], reversion["research_input_digest"])
        momentum_pairs = [(s["sequence"], s["side"]) for s in momentum["signals"]]
        reversion_pairs = [(s["sequence"], s["side"]) for s in reversion["signals"]]
        self.assertNotEqual(momentum_pairs, reversion_pairs)
        self.assertNotEqual(momentum["parameters_digest"], reversion["parameters_digest"])
        self.assertNotEqual(momentum["algorithm_digest"], reversion["algorithm_digest"])


class ContentIdentityTests(unittest.TestCase):
    def test_changing_parameters_changes_content_identity(self):
        manifest = contract.read_json(INPUT)
        source = contract.read_source_text(contract.STRATEGY_SOURCES / "momentum.py")
        base = contract.build_artifact(MOMENTUM, {"lookback": 2, "threshold_bps": 25},
                                       source, manifest)
        changed = contract.build_artifact(MOMENTUM, {"lookback": 3, "threshold_bps": 25},
                                          source, manifest)
        self.assertNotEqual(base["parameters_digest"], changed["parameters_digest"])
        self.assertNotEqual(contract.canonical_json(base), contract.canonical_json(changed))
        # Reusing the reviewed id with different parameters is not a valid artifact.
        reviewed = contract.read_json(run_research.reviewed_paths("momentum")[0])
        self.assertNotEqual(reviewed["parameters_digest"], changed["parameters_digest"])

    def test_changing_the_input_changes_identity_and_signals(self):
        manifest = contract.read_json(INPUT)
        base = run_research.regenerate("momentum")[1]
        for name, mutate in [
            ("repriced", lambda value: value["entries"][2]["quote"].__setitem__("price_cents", 1300)),
            ("appended", lambda value: value["entries"].append(dict(value["entries"][0], sequence=6))),
            ("reordered", lambda value: value["entries"].reverse()),
        ]:
            changed = json.loads(json.dumps(manifest))
            mutate(changed)
            changed["content_digest"] = contract.input_digest(changed)
            self.assertNotEqual(changed["content_digest"], manifest["content_digest"], name)
            artifact = contract.build_artifact(
                MOMENTUM, run_research.STRATEGIES["momentum"].PARAMETERS,
                contract.read_source_text(contract.STRATEGY_SOURCES / "momentum.py"), changed)
            output = contract.build_output(MOMENTUM, artifact, changed,
                                           run_research.STRATEGIES["momentum"])
            self.assertNotEqual(artifact["research_input_digest"], base["research_input_digest"], name)
            self.assertNotEqual(output["content_digest"], base["content_digest"], name)

    def test_frozen_input_never_records_a_receipt_time(self):
        manifest = contract.read_json(INPUT)
        self.assertNotIn("received_at", set(walk_keys(manifest)))
        for entry in manifest["entries"]:
            self.assertLessEqual(entry["quote"]["observed_at"], entry["available_at"])
            self.assertLessEqual(entry["available_at"], entry["decision_at"])
            for side in ("buy", "sell"):
                self.assertGreater(entry[side]["price_cents"], 0)
                self.assertGreaterEqual(entry[side]["available_quantity"], 0)


if __name__ == "__main__":
    unittest.main()
