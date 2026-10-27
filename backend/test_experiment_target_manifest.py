# -*- coding: utf-8 -*-
"""实验目标清单与消费证明的验证（方案 §3.1 / §4.2 的 V0 机械检查）。"""
import unittest
from unittest import mock

import pandas as pd

import experiment_target_manifest as ETM

PROBE = "600001"


def factor_table():
    """两行最小因子表：PROBE 是首板候选，另一行不是。"""
    rows = [
        {"name": "甲", "industry": "行业A", "price": 10.0, "pct": 3.0, "amount": 5e7,
         "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
         "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
         "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
         "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5, "ma60": 9.0},
        {"name": "乙", "industry": "行业A", "price": 12.0, "pct": 2.0, "amount": 4e7,
         "turnover": 2.0, "main_pct": 0.8, "super_net_raw": 8e5, "mom5_raw": 0.01,
         "mom20_raw": 0.02, "mom60_raw": 0.04, "mom_short": 0.01, "volsurge": 1.2,
         "vol_surge_raw": 1.5, "rsi14_raw": 52.0, "flow": 0.9, "sentiment": 0.0,
         "above_boll_mid": True, "boll_mid_breakout": False, "ma20": 11.5, "ma60": 11.0},
    ]
    return pd.DataFrame(rows, index=["600001", "600002"])


class DeclarationTests(unittest.TestCase):
    def test_declared_allowlists_match_the_real_default_tables(self):
        self.assertEqual(ETM.verify_declarations(), [])

    def test_drift_is_loud_when_a_default_appears_without_a_declaration(self):
        defaults = dict(ETM.S.PAPER_CONDITION_DEFAULTS["first_board_relay"])
        trimmed = {key: value for key, value in defaults.items()
                   if key not in ("chase_penalty",)}
        with mock.patch.dict(ETM.S.PAPER_CONDITION_DEFAULTS,
                             {"first_board_relay": trimmed}, clear=False):
            violations = ETM.verify_declarations()
        self.assertIn("tq_breakout:entry_policy:first_board_relay:declared_field_missing:chase_penalty",
                      violations)

    def test_reported_profit_breakout_is_declared_unsupported_with_a_reason(self):
        manifest = ETM.manifest_for("reported_profit_breakout", ETM.TARGET_ENTRY_POLICY)
        self.assertFalse(manifest.supported)
        self.assertEqual(manifest.unsupported_reason, ETM.REFUSAL_NO_PARAMETERIZED_INPUT)
        self.assertEqual(manifest.allowed_fields, ())
        with self.assertRaises(ETM.ManifestError):
            ETM.check_fields_allowed(manifest, ("anything",))

    def test_an_unsupported_target_cannot_smuggle_an_allowlist(self):
        with self.assertRaises(ETM.ManifestError):
            ETM.ExperimentTargetManifest(
                strategy_id="reported_profit_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
                model_family="reported_profit_breakout", allowed_fields=("first_board_bonus",),
                actual_consumer="pretend", required_inputs=(),
                unsupported_reason=ETM.REFUSAL_NO_PARAMETERIZED_INPUT)

    def test_a_supported_target_needs_an_allowlist_and_a_consumer(self):
        with self.assertRaises(ETM.ManifestError):
            ETM.ExperimentTargetManifest(
                strategy_id="tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
                model_family="first_board_relay", allowed_fields=(), actual_consumer="x",
                required_inputs=())
        with self.assertRaises(ETM.ManifestError):
            ETM.ExperimentTargetManifest(
                strategy_id="tq_breakout", target_kind=ETM.TARGET_ENTRY_POLICY,
                model_family="first_board_relay", allowed_fields=("first_board_bonus",),
                actual_consumer="", required_inputs=())

    def test_unknown_target_kind_is_rejected(self):
        with self.assertRaises(ETM.ManifestError):
            ETM.ExperimentTargetManifest(
                strategy_id="tq_breakout", target_kind="vibes", model_family=None,
                allowed_fields=("a",), actual_consumer="x", required_inputs=())

    def test_unknown_fields_are_rejected_rather_than_silently_ignored(self):
        manifest = ETM.manifest_for("tq_breakout", ETM.TARGET_ENTRY_POLICY, "first_board_relay")
        self.assertEqual(ETM.check_fields_allowed(manifest, ("first_board_bonus",)),
                         ("first_board_bonus",))
        with self.assertRaises(ETM.ManifestError) as caught:
            ETM.check_fields_allowed(manifest, ("first_board_bonus", "magic_field"))
        self.assertIn(ETM.REFUSAL_FIELD_NOT_ALLOWED, str(caught.exception))

    def test_trend_pullback_declares_both_model_families(self):
        kinds = sorted(item.model_family for item in ETM.manifests_for("trend_pullback"))
        self.assertEqual(kinds, ["pullback_reversal", "pullback_reversal",
                                 "trend_continuation", "trend_continuation"])

    def test_the_tuner_controller_parameters_are_not_offered_as_strategy_targets(self):
        for manifest in ETM.supported_manifests():
            self.assertNotIn("hold_bias", manifest.allowed_fields)
            self.assertNotIn("max_weight_delta", manifest.allowed_fields)


class ConsumptionProofTests(unittest.TestCase):
    def prove(self, variant_value=0.0):
        return ETM.prove_condition_consumption(
            factor_table(), model_family="first_board_relay", field="first_board_bonus",
            variant_value=variant_value, probe_code=PROBE,
            first_board_codes=[PROBE], topn=2)

    def test_the_bonus_moves_the_probe_score_by_exactly_its_delta(self):
        proof = self.prove(0.0)
        self.assertTrue(proof.consumed, proof.notes)
        self.assertAlmostEqual(proof.baseline_output - proof.variant_output, 0.22, places=6)
        self.assertEqual(proof.verdict, ETM.VERDICT_CONSUMED)

    def test_a_different_variant_gives_a_different_but_exact_delta(self):
        proof = self.prove(0.5)
        self.assertTrue(proof.consumed, proof.notes)
        self.assertAlmostEqual(proof.variant_output - proof.baseline_output, 0.28, places=6)

    def test_the_harness_refuses_a_dead_wire(self):
        """负向对照：消费端没接线时，得分不变，证明必须判 not_consumed。"""
        original = ETM.S.run_strategy

        def dead_wire(*args, **kwargs):
            result = original(*args, **{**kwargs, "condition_overrides": {}})
            return result

        with mock.patch.object(ETM.S, "run_strategy", side_effect=dead_wire):
            proof = self.prove(0.0)
        self.assertFalse(proof.consumed)
        self.assertEqual(proof.verdict, ETM.VERDICT_NOT_CONSUMED)

    def test_a_non_additive_parameter_is_not_accepted_as_consumed(self):
        """pct_low 确实被读，但影响是区间开关而非加性增量：证明不该判 consumed。"""
        proof = ETM.prove_condition_consumption(
            factor_table(), model_family="first_board_relay", field="pct_low",
            variant_value=4.0, probe_code=PROBE, first_board_codes=[PROBE], topn=2)
        self.assertFalse(proof.consumed)

    def test_a_variant_equal_to_the_baseline_is_rejected(self):
        with self.assertRaises(ETM.ManifestError):
            self.prove(0.22)

    def test_a_field_outside_the_allowlist_is_rejected(self):
        with self.assertRaises(ETM.ManifestError):
            ETM.prove_condition_consumption(
                factor_table(), model_family="first_board_relay", field="hold_bias",
                variant_value=0.2, probe_code=PROBE, first_board_codes=[PROBE], topn=2)

    def test_the_proof_is_deterministic(self):
        first = self.prove(0.0)
        second = self.prove(0.0)
        self.assertEqual(first.fingerprint, second.fingerprint)


class ReplayRefusalTests(unittest.TestCase):
    def test_a_complete_manifest_is_not_a_finished_experiment(self):
        manifest = ETM.manifest_for("tq_breakout", ETM.TARGET_ENTRY_POLICY, "first_board_relay")
        with self.assertRaises(ETM.ManifestError) as caught:
            ETM.require_replay_support(manifest)
        self.assertIn(ETM.REFUSAL_REPLAY_NOT_IMPLEMENTED, str(caught.exception))

    def test_acceptance_snapshot_states_that_replay_is_missing(self):
        snapshot = ETM.acceptance_snapshot()
        self.assertEqual(snapshot["replay"], "not_implemented")
        self.assertEqual(snapshot["declaration_violations"], [])
        self.assertTrue(snapshot["supported"])
        self.assertEqual([item["strategy_id"] for item in snapshot["unsupported"]],
                         ["reported_profit_breakout"])


if __name__ == "__main__":
    unittest.main()
