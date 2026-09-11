from pathlib import Path

path = Path('backend/strategies.py')
text = path.read_text(encoding='utf-8')

def replace_once(old, new, label):
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected one match, got {count}')
    text = text.replace(old, new, 1)

replace_once(
'''    weights = dict(PAPER_WEIGHTS[strategy_id])
    conditions = _paper_conditions(strategy_id, condition_overrides)
    enabled = conditions.get("enabled", {})
    if isinstance(weight_overrides, dict) and set(weight_overrides) == set(weights):
        weights = _bounded_weight_simplex(weight_overrides)
    required_factors = ("sentiment",) if strategy_id == "sentiment_pioneer" else ()
''',
'''    weights = dict(PAPER_WEIGHTS[strategy_id])
    conditions = _paper_conditions(strategy_id, condition_overrides)
    enabled = conditions.get("enabled", {})
    weight_override_rejected = False
    if isinstance(weight_overrides, dict) and set(weight_overrides) == set(weights):
        candidate_weights = _bounded_weight_simplex(weight_overrides)
        if FC.group_caps_ok(candidate_weights):
            weights = candidate_weights
        else:
            # Adaptive overlays cannot recreate correlated double-voting by
            # concentrating multiple factors from the same evidence family.
            # Fail closed to the audited strategy defaults rather than clamp a
            # different economic model silently.
            weight_override_rejected = True
    weight_group_totals = FC.weight_group_totals(weights)
    required_factors = ("sentiment",) if strategy_id == "sentiment_pioneer" else ()
''',
'weight override cap',
)
replace_once(
'''        "factor_calibration": {
            "version": FC.CALIBRATION_VERSION,
            "minimum_evidence_quality": FC.MIN_RUNTIME_EVIDENCE_QUALITY,
            "required_factors": list(required_factors),
            "eligible_evidence_rows": int(evidence_ok.sum()),
        },
''',
'''        "factor_calibration": {
            "version": FC.CALIBRATION_VERSION,
            "minimum_evidence_quality": FC.MIN_RUNTIME_EVIDENCE_QUALITY,
            "maximum_factor_group_weight": FC.MAX_FACTOR_GROUP_WEIGHT,
            "weight_group_totals": {
                key: round(value, 6) for key, value in weight_group_totals.items()
            },
            "weight_override_rejected": weight_override_rejected,
            "required_factors": list(required_factors),
            "eligible_evidence_rows": int(evidence_ok.sum()),
        },
''',
'calibration metadata group cap',
)
path.write_text(text, encoding='utf-8')
