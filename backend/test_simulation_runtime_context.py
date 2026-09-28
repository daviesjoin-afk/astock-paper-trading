"""R32-A immutable runtime identity tests."""
from __future__ import annotations

import ast
import hashlib
import pathlib
import unittest

import simulation_runtime_context as SRC


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build(**changes):
    values = {
        "strategy_id": "trend_pullback",
        "strategy_version": 1,
        "strategy_checksum": _sha("strategy-v1"),
        "session_date": "2026-09-28",
        "decision_at": "2026-09-28T10:00:00+08:00",
        "market_policy_name": "execution_quote",
        "market_snapshot_fingerprint": _sha("market"),
        "symbol_quote_fingerprints": {"600000": _sha("quote")},
        "tradability_evidence_fingerprints": {"600000@2026-09-28": _sha("tradability")},
        "execution_ruleset_version": "a-share-simulation-v1",
        "risk_policy_identity": {"compiled_profile": {"max_positions": 4}},
    }
    values.update(changes)
    return SRC.build_comparable_runtime_context(**values)


class ComparableRuntimeContextTests(unittest.TestCase):
    def test_same_semantics_are_stable_across_clock_and_mapping_order(self):
        one = _build(risk_policy_identity={"compiled_profile": {"max_positions": 4,
                                                                  "max_exposure": 0.5}})
        two = _build(
            decision_at="2026-09-28T02:00:00Z",
            symbol_quote_fingerprints={"600000": _sha("quote")},
            risk_policy_identity={"compiled_profile": {"max_exposure": 0.5,
                                                          "max_positions": 4}},
        )
        self.assertEqual(one.context_fingerprint, two.context_fingerprint)
        local_naive = _build(
            decision_at="2026-09-28T10:00:00",
            risk_policy_identity={"compiled_profile": {"max_positions": 4,
                                                          "max_exposure": 0.5}},
        )
        self.assertEqual(one.context_fingerprint, local_naive.context_fingerprint)

    def test_semantic_inputs_change_context_identity(self):
        baseline = _build().context_fingerprint
        variations = (
            {"strategy_version": 2, "strategy_checksum": _sha("strategy-v2")},
            {"strategy_version": 2},
            {"market_policy_name": "live_market"},
            {"execution_ruleset_version": "a-share-simulation-v2"},
            {"risk_policy_identity": {"compiled_profile": {"max_positions": 3}}},
            {"market_snapshot_fingerprint": _sha("market-v2")},
            {"tradability_evidence_fingerprints": {"600000@2026-09-28": _sha("changed")}},
        )
        for variation in variations:
            with self.subTest(variation=variation):
                self.assertNotEqual(baseline, _build(**variation).context_fingerprint)

    def test_identity_requires_full_exact_inputs(self):
        for variation in (
            {"strategy_checksum": "abc"},
            {"market_snapshot_fingerprint": ""},
            {"tradability_evidence_fingerprints": {}},
            {"execution_ruleset_version": ""},
            {"risk_policy_identity": {}},
        ):
            with self.subTest(variation=variation), self.assertRaises(ValueError):
                _build(**variation)

    def test_context_module_is_a_pure_contract(self):
        source = pathlib.Path(SRC.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"sqlite3", "requests", "urllib", "httpx", "paper_trading",
                     "paper_risk_service", "os", "pathlib", "time"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & forbidden)
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("date.today", source)


if __name__ == "__main__":
    unittest.main()
