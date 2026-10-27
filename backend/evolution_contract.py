"""Shared version and global limits for evolution and candidate validation."""

EVOLUTION_VERSION = "self-evolution-v1"

# One shared object: the evolution generator and activation validator must use
# the same limits. Strategy-specific profiles can only narrow these limits.
BOUNDS = {
    "max_weight_delta": {"min": 0.01, "max": 0.06, "step": 0.003},
    "max_delta_threshold": {"min": 0.001, "max": 0.015, "step": 0.0005},
    "confidence_threshold": {"min": 50, "max": 90, "step": 3},
    "consensus_weight_ratio": {"min": 0.40, "max": 0.80, "step": 0.03},
    "consensus_direction_threshold": {"min": 0.002, "max": 0.015, "step": 0.001},
    "hold_bias": {"min": 0.0, "max": 0.5, "step": 0.02},
}
