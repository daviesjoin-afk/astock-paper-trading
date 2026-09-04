# -*- coding: utf-8 -*-
import random
import unittest

import adaptive_genetics as genetics


class AdaptiveGeneticsTests(unittest.TestCase):
    def test_normalize_genome_is_scale_invariant(self):
        features = ("momentum", "value")
        self.assertEqual(
            genetics.normalize_genome({"momentum": 2, "value": -1}, features),
            genetics.normalize_genome({"momentum": 20, "value": -10}, features),
        )

    def test_mutation_and_crossover_keep_feature_shape(self):
        features = ("momentum", "value", "flow")
        parent = genetics.normalize_genome({"momentum": 1, "value": 2, "flow": 3}, features)
        child = genetics.mutate_genome(parent, random.Random(7), features)
        crossed = genetics.crossover(parent, child, random.Random(8), features)
        self.assertEqual(set(child), set(features))
        self.assertAlmostEqual(sum(abs(value) for value in crossed.values()), 1.0, places=6)

    def test_alpha_fitness_is_deterministic_for_same_rows(self):
        features = ("momentum", "value")
        rows = [
            {
                "profile_date": "2026-09-03", "horizon": 1,
                "momentum": index / 100, "value": 1.0,
                "excess_return_pct": index / 100,
            }
            for index in range(100)
        ]
        genome = genetics.normalize_genome({"momentum": 1, "value": 0}, features)
        first = genetics.alpha_fitness(genome, rows, features, {1: 1.0})
        second = genetics.alpha_fitness(genome, rows, features, {1: 1.0})
        self.assertEqual(first, second)
        self.assertEqual(first["windows"], 1)


if __name__ == "__main__":
    unittest.main()
