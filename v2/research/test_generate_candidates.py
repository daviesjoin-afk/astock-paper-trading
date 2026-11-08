"""Tests for the candidate generator."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import generate_candidates as generator

BASE = {
    "strategy_version_id": "research-momentum-v1",
    "parameters_canonical": '{"lookback":2,"threshold_bps":25}',
    "parameters_digest": hashlib.sha256(b'{"lookback":2,"threshold_bps":25}').hexdigest(),
}


class CandidateGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.base_path = self.dir / "base.json"
        self.base_path.write_text(json.dumps(BASE), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_grid(self, grid: dict) -> Path:
        path = self.dir / "grid.json"
        path.write_text(json.dumps(grid), encoding="utf-8")
        return path

    def build(self, grid: dict, max_candidates: int = 32) -> dict:
        base = generator.load_base(self.base_path)
        return generator.build_manifest(base, generator.load_grid(self.write_grid(grid)), max_candidates)

    def test_candidates_are_deterministic_and_exclude_the_base(self) -> None:
        grid = {"lookback": [2, 3], "threshold_bps": [25, 50]}
        first = self.build(grid)
        shuffled = {"threshold_bps": [50, 25], "lookback": [3, 2]}
        second = self.build(shuffled)
        self.assertEqual(first["digest"], second["digest"])
        # 2x2 grid minus the base combination itself.
        self.assertEqual(len(first["candidates"]), 3)
        digests = {item["parameters_digest"] for item in first["candidates"]}
        self.assertNotIn(BASE["parameters_digest"], digests)
        for candidate in first["candidates"]:
            self.assertEqual(len(candidate["parameters_digest"]), 64)
            self.assertTrue(candidate["candidate_id"].startswith("research-momentum-v1-"))

    def test_digest_matches_canonical_sha256(self) -> None:
        manifest = self.build({"lookback": [3], "threshold_bps": [25, 50]})
        for candidate in manifest["candidates"]:
            expected = hashlib.sha256(candidate["parameters_canonical"].encode()).hexdigest()
            self.assertEqual(candidate["parameters_digest"], expected)
            self.assertEqual(
                candidate["parameters_canonical"],
                json.dumps(
                    json.loads(candidate["parameters_canonical"]),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

    def test_unknown_or_missing_parameter_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown_parameter"):
            self.build({"lookback": [3], "threshold_bps": [25], "alpha": [1]})
        with self.assertRaisesRegex(ValueError, "grid_missing_parameter"):
            self.build({"lookback": [3]})

    def test_bounds_and_types_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "out_of_bounds"):
            self.build({"lookback": [0], "threshold_bps": [25]})
        with self.assertRaisesRegex(ValueError, "out_of_bounds"):
            self.build({"lookback": [3], "threshold_bps": [9_000]})
        with self.assertRaisesRegex(ValueError, "non_integer"):
            self.build({"lookback": [3.5], "threshold_bps": [25]})

    def test_grid_cap_and_no_op_grid_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "grid_too_large"):
            self.build({"lookback": list(range(1, 10)), "threshold_bps": list(range(1, 10))}, 32)
        with self.assertRaisesRegex(ValueError, "no_candidates_after_dedup"):
            self.build({"lookback": [2], "threshold_bps": [25]})

    def test_cli_writes_manifest_and_rejects_bad_grid(self) -> None:
        good = self.write_grid({"lookback": [3], "threshold_bps": [25, 50]})
        out = self.dir / "candidates.json"
        self.assertEqual(
            generator.main(
                ["--base", str(self.base_path), "--grid", str(good), "--out", str(out)]
            ),
            0,
        )
        manifest = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["base_parameters_digest"], BASE["parameters_digest"])
        self.assertEqual(len(manifest["candidates"]), 2)

        bad = self.write_grid({"lookback": [0], "threshold_bps": [25]})
        self.assertEqual(
            generator.main(
                ["--base", str(self.base_path), "--grid", str(bad), "--out", str(self.dir / "x.json")]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
