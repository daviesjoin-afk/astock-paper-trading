"""The activation validator uses the same limits without loading the generator."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EvolutionContractBoundaryTests(unittest.TestCase):
    def test_activation_can_read_limits_without_importing_generator(self):
        script = '''
import sys
import evolution_activation as activation
import evolution_contract as contract
assert "self_evolution" not in sys.modules
assert activation._version() == "self-evolution-v1"
assert "self_evolution" not in sys.modules
import self_evolution as generator
assert generator.BOUNDS is contract.BOUNDS
assert generator.EVOLUTION_VERSION == contract.EVOLUTION_VERSION
assert contract.BOUNDS["max_weight_delta"] == {"min": 0.01, "max": 0.06, "step": 0.003}
'''
        with tempfile.TemporaryDirectory(prefix="evolution-boundary-") as data_dir:
            env = os.environ.copy()
            env["ASTOCK_DATA_DIR"] = data_dir
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=Path(__file__).parent,
                env=env, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
