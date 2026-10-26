# -*- coding: utf-8 -*-
"""实验目标清单与参数消费证明（方案 §4.2 / 影子方案 F1 的 V0 层）。

**为什么先有这一层**：方案明确禁止"把三套策略都写成已支持"来凑数。实验目标必须先
回答三个问题，否则回放出来的数字没有意义：

1. 这个参数**真的被哪个运行函数读取**？（不是调参器控制参数，也不是没人读的覆盖）
2. 允许改的字段白名单是什么？（未知字段必须拒绝，不能静默忽略）
3. 谁消费它、走哪条采用路径、缺什么输入？

本模块只做 V0 机械检查：结构声明 + **消费证明**（敏感样本让参数变化确实改变目标函数
输出）。它**不做回测**：没有回放器、没有撮合、不产生收益结论。`require_replay_support()`
存在的意义就是让"清单齐全"无法被误读成"实验已经跑过"。

已核对的现状（不要凭印象改）：

- ``paper_orders.strategy_id == account_id``，策略与账户同名。
- ``tq_breakout`` → 模型族 ``first_board_relay``；``trend_pullback`` → ``pullback_reversal``/
  ``trend_continuation``；``reported_profit_breakout`` **没有任何参数化选股输入**，
  它的门槛是 Python 字面量（``strategies.py:779`` / ``799``），必须如实声明为不支持。
- ``self_evolution.get_strategy_params`` 里的是**调参器控制参数**
  （``max_weight_delta`` / ``hold_bias`` …），不进入选股打分，不能拿来冒充策略参数。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import strategies as S

MANIFEST_VERSION = "experiment-target-manifest-v1"
ADAPTER_VERSION = "native-selection-adapter-v1"

TARGET_SELECTION_WEIGHTS = "selection_weights"
TARGET_ENTRY_POLICY = "entry_policy"
TARGET_EXIT_POLICY = "exit_policy"
TARGET_ALLOCATION_OVERLAY = "allocation_overlay"
TARGET_STRATEGY_DSL = "strategy_dsl"
TARGET_TUNER_CONTROLLER = "tuner_controller"
TARGET_CODE_CHANGE = "code_change"
TARGET_KINDS = (
    TARGET_SELECTION_WEIGHTS, TARGET_ENTRY_POLICY, TARGET_EXIT_POLICY,
    TARGET_ALLOCATION_OVERLAY, TARGET_STRATEGY_DSL, TARGET_TUNER_CONTROLLER,
    TARGET_CODE_CHANGE,
)

APPLY_ROUTE_NEXT_CYCLE = "next_legal_cycle_pin"
"""正式覆盖必须带生效时间与参数版本，并排队到下一个合法周期（方案 §5.2）。"""

VERDICT_CONSUMED = "consumed"
VERDICT_NOT_CONSUMED = "not_consumed"
VERDICT_REFUSED = "refused"

REFUSAL_NO_PARAMETERIZED_INPUT = "no_parameterized_selection_input"
REFUSAL_REPLAY_NOT_IMPLEMENTED = "replay_not_implemented"
REFUSAL_FIELD_NOT_ALLOWED = "field_not_allowed"
REFUSAL_UNKNOWN_MODEL_FAMILY = "unknown_model_family"


class ManifestError(ValueError):
    """清单合同违约。"""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _numeric_defaults(model_family: str) -> tuple[str, ...]:
    defaults = S.PAPER_CONDITION_DEFAULTS.get(model_family) or {}
    return tuple(sorted(key for key in defaults if key != "enabled"))


@dataclasses.dataclass(frozen=True, slots=True)
class ExperimentTargetManifest:
    """一个实验目标的完整声明。支持的字段必须逐项列出，不支持必须给出原因。"""

    strategy_id: str
    target_kind: str
    model_family: str | None
    allowed_fields: tuple[str, ...]
    actual_consumer: str
    required_inputs: tuple[str, ...]
    apply_route: str = APPLY_ROUTE_NEXT_CYCLE
    baseline_defaults: tuple[tuple[str, float], ...] = ()
    unsupported_reason: str | None = None
    adapter_version: str = ADAPTER_VERSION

    def __post_init__(self):
        if self.target_kind not in TARGET_KINDS:
            raise ManifestError(f"unknown_target_kind:{self.target_kind}")
        strategy_id = str(self.strategy_id or "").strip()
        if not strategy_id:
            raise ManifestError("strategy_id_required")
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "allowed_fields",
                           tuple(str(item) for item in self.allowed_fields or ()))
        object.__setattr__(self, "required_inputs",
                           tuple(str(item) for item in self.required_inputs or ()))
        object.__setattr__(self, "baseline_defaults",
                           tuple((str(k), float(v)) for k, v in self.baseline_defaults or ()))
        if self.unsupported_reason:
            if self.allowed_fields:
                raise ManifestError("unsupported_target_must_not_allow_fields")
            return
        if not self.allowed_fields:
            raise ManifestError("supported_target_requires_an_explicit_field_allowlist")
        if not str(self.actual_consumer or "").strip():
            raise ManifestError("supported_target_requires_a_declared_consumer")
        duplicates = len(set(self.allowed_fields)) != len(self.allowed_fields)
        if duplicates:
            raise ManifestError("allowed_fields_must_be_unique")

    @property
    def supported(self) -> bool:
        return not self.unsupported_reason

    @property
    def key(self) -> str:
        return f"{self.strategy_id}:{self.target_kind}:{self.model_family or '-'}"

    def projection(self) -> dict:
        return {
            "key": self.key, "strategy_id": self.strategy_id,
            "target_kind": self.target_kind, "model_family": self.model_family,
            "allowed_fields": list(self.allowed_fields),
            "baseline_defaults": dict(self.baseline_defaults),
            "actual_consumer": self.actual_consumer,
            "required_inputs": list(self.required_inputs),
            "apply_route": self.apply_route,
            "adapter_version": self.adapter_version,
            "unsupported_reason": self.unsupported_reason,
            "supported": self.supported,
        }


def _condition_manifest(strategy_id: str, model_family: str, consumer: str,
                        inputs: tuple[str, ...]) -> ExperimentTargetManifest:
    defaults = S.PAPER_CONDITION_DEFAULTS.get(model_family) or {}
    fields = _numeric_defaults(model_family)
    return ExperimentTargetManifest(
        strategy_id=strategy_id, target_kind=TARGET_ENTRY_POLICY, model_family=model_family,
        allowed_fields=fields, actual_consumer=consumer, required_inputs=inputs,
        baseline_defaults=tuple((name, float(defaults[name])) for name in fields
                                if isinstance(defaults.get(name), (int, float))),
    )


def _weights_manifest(strategy_id: str, model_family: str) -> ExperimentTargetManifest:
    weights = S.PAPER_WEIGHTS.get(model_family) or {}
    fields = tuple(sorted(weights))
    return ExperimentTargetManifest(
        strategy_id=strategy_id, target_kind=TARGET_SELECTION_WEIGHTS,
        model_family=model_family, allowed_fields=fields,
        actual_consumer="strategies._run_paper_strategy → factors.score_factors(weights)",
        required_inputs=("frozen_factor_table", "universe_snapshot"),
        baseline_defaults=tuple((name, float(weights[name])) for name in fields),
    )


def _build_registry() -> dict[str, ExperimentTargetManifest]:
    manifests = [
        _condition_manifest(
            "tq_breakout", "first_board_relay",
            "strategies._run_paper_strategy (strategies.py:1530, score += first_board_bonus…)",
            ("frozen_factor_table", "first_board_codes", "universe_snapshot")),
        _weights_manifest("tq_breakout", "first_board_relay"),
        _condition_manifest(
            "trend_pullback", "pullback_reversal",
            "strategies._run_paper_strategy (strategies.py:1540)",
            ("frozen_factor_table", "universe_snapshot")),
        _weights_manifest("trend_pullback", "pullback_reversal"),
        _condition_manifest(
            "trend_pullback", "trend_continuation",
            "strategies._run_paper_strategy (strategies.py:1555)",
            ("frozen_factor_table", "universe_snapshot")),
        _weights_manifest("trend_pullback", "trend_continuation"),
        ExperimentTargetManifest(
            strategy_id="reported_profit_breakout", target_kind=TARGET_ENTRY_POLICY,
            model_family="reported_profit_breakout", allowed_fields=(),
            actual_consumer="", required_inputs=(),
            unsupported_reason=REFUSAL_NO_PARAMETERIZED_INPUT),
    ]
    return {manifest.key: manifest for manifest in manifests}


MANIFESTS = _build_registry()


def manifests_for(strategy_id: str) -> list[ExperimentTargetManifest]:
    return [item for item in MANIFESTS.values() if item.strategy_id == str(strategy_id)]


def supported_manifests() -> list[ExperimentTargetManifest]:
    return [item for item in MANIFESTS.values() if item.supported]


def manifest_for(strategy_id: str, target_kind: str,
                 model_family: str | None = None) -> ExperimentTargetManifest:
    candidates = [item for item in MANIFESTS.values()
                  if item.strategy_id == str(strategy_id) and item.target_kind == target_kind
                  and (model_family is None or item.model_family == model_family)]
    if not candidates:
        raise ManifestError(f"no_manifest_for:{strategy_id}:{target_kind}:{model_family}")
    if len(candidates) > 1:
        raise ManifestError(f"ambiguous_manifest_for:{strategy_id}:{target_kind}")
    return candidates[0]


def verify_declarations() -> list[str]:
    """结构自检：声明白名单必须与真实默认表一致。

    漂移要**响亮**：新增一个可调默认值而没人更新清单，或清单里写了不存在的字段，
    这里都会报出来。返回违规列表，空列表表示一致。
    """
    violations: list[str] = []
    for item in MANIFESTS.values():
        if not item.supported:
            continue
        if item.target_kind == TARGET_ENTRY_POLICY:
            if item.model_family not in S.PAPER_CONDITION_DEFAULTS:
                violations.append(f"{item.key}:unknown_model_family")
                continue
            actual = set(_numeric_defaults(item.model_family))
            declared = set(item.allowed_fields)
            for missing in sorted(actual - declared):
                violations.append(f"{item.key}:undeclared_default:{missing}")
            for extra in sorted(declared - actual):
                violations.append(f"{item.key}:declared_field_missing:{extra}")
        elif item.target_kind == TARGET_SELECTION_WEIGHTS:
            if item.model_family not in S.PAPER_WEIGHTS:
                violations.append(f"{item.key}:unknown_model_family")
                continue
            actual = set(S.PAPER_WEIGHTS[item.model_family])
            declared = set(item.allowed_fields)
            for missing in sorted(actual - declared):
                violations.append(f"{item.key}:undeclared_weight:{missing}")
            for extra in sorted(declared - actual):
                violations.append(f"{item.key}:declared_weight_missing:{extra}")
    return violations


def check_fields_allowed(manifest: ExperimentTargetManifest,
                         fields) -> tuple[str, ...]:
    """未知字段整条拒绝（方案 §3.1"未知字段/类型拒绝，不静默忽略"）。"""
    if not manifest.supported:
        raise ManifestError(f"unsupported_target:{manifest.unsupported_reason}")
    requested = [str(item) for item in fields or ()]
    if not requested:
        raise ManifestError("no_field_requested")
    disallowed = [name for name in requested if name not in manifest.allowed_fields]
    if disallowed:
        raise ManifestError(f"{REFUSAL_FIELD_NOT_ALLOWED}:{','.join(disallowed)}")
    return tuple(requested)


@dataclasses.dataclass(frozen=True, slots=True)
class ConsumptionProof:
    """一次消费证明的结果。判据是"参数变化量 == 输出变化量"，不是"结果看起来不同"。"""

    manifest_key: str
    field: str
    baseline_value: float
    variant_value: float
    baseline_output: float | None
    variant_output: float | None
    verdict: str
    notes: str = ""
    fingerprint: str = ""

    @property
    def consumed(self) -> bool:
        return self.verdict == VERDICT_CONSUMED

    def projection(self) -> dict:
        return {"manifest_key": self.manifest_key, "field": self.field,
                "baseline_value": self.baseline_value, "variant_value": self.variant_value,
                "baseline_output": self.baseline_output,
                "variant_output": self.variant_output, "verdict": self.verdict,
                "notes": self.notes, "fingerprint": self.fingerprint}


def _score_for(result, code: str):
    for pick in result.get("picks") or []:
        if str(pick.get("code")) == str(code):
            return pick
    return None


def prove_condition_consumption(
    table, *, model_family: str, field: str, variant_value: float,
    probe_code: str, first_board_codes=None, topn: int = 5,
    strategy_id: str | None = None, tolerance: float = 1e-9,
) -> ConsumptionProof:
    """让某个决策参数变化，验证目标代码的得分变化**恰好等于**参数变化量。

    ``first_board_bonus`` 这类加性项是最理想的第一例：如果实现没有真正读这个参数，
    差值会恰好是 0；如果读错了对象，差值不会等于参数增量。两种都会被判 not_consumed。
    """
    manifest = manifest_for(strategy_id or _default_strategy_for(model_family),
                            TARGET_ENTRY_POLICY, model_family)
    check_fields_allowed(manifest, (field,))
    defaults = S.PAPER_CONDITION_DEFAULTS.get(model_family) or {}
    if field not in defaults:
        raise ManifestError(f"field_not_in_defaults:{field}")
    baseline_value = float(defaults[field])
    if abs(baseline_value - float(variant_value)) < tolerance:
        raise ManifestError("variant_must_differ_from_the_baseline")

    common = dict(topn=int(topn), gate=None, first_board_codes=first_board_codes)
    baseline_result = S.run_strategy(model_family, table, condition_overrides={}, **common)
    variant_result = S.run_strategy(model_family, table,
                                    condition_overrides={field: float(variant_value)},
                                    **common)
    baseline_pick = _score_for(baseline_result, probe_code)
    variant_pick = _score_for(variant_result, probe_code)
    baseline_output = None if baseline_pick is None else float(baseline_pick.get("score") or 0.0)
    variant_output = None if variant_pick is None else float(variant_pick.get("score") or 0.0)

    expected = float(variant_value) - baseline_value
    verdict = VERDICT_NOT_CONSUMED
    notes = ""
    if baseline_output is None or variant_output is None:
        notes = "probe_code_not_selected_on_one_side"
    else:
        observed = variant_output - baseline_output
        if abs(observed - expected) <= 1e-6:
            verdict = VERDICT_CONSUMED
        else:
            notes = f"observed_delta_{observed:.6f}_expected_{expected:.6f}"
    material = {
        "manifest_key": manifest.key, "field": field,
        "baseline_value": baseline_value, "variant_value": float(variant_value),
        "baseline_output": baseline_output, "variant_output": variant_output,
        "verdict": verdict, "notes": notes,
        "conditions_used_baseline": baseline_result.get("conditions_used"),
        "conditions_used_variant": variant_result.get("conditions_used"),
    }
    return ConsumptionProof(
        manifest_key=manifest.key, field=field, baseline_value=baseline_value,
        variant_value=float(variant_value), baseline_output=baseline_output,
        variant_output=variant_output, verdict=verdict, notes=notes,
        fingerprint=fingerprint(material))


def _default_strategy_for(model_family: str) -> str:
    for item in MANIFESTS.values():
        if item.model_family == model_family and item.target_kind == TARGET_ENTRY_POLICY \
                and item.supported:
            return item.strategy_id
    raise ManifestError(f"no_strategy_for_model_family:{model_family}")


def require_replay_support(manifest: ExperimentTargetManifest) -> None:
    """明确拒绝：清单齐全 ≠ 实验跑过。回放器属于下一切片。"""
    if not manifest.supported:
        raise ManifestError(f"unsupported_target:{manifest.unsupported_reason}")
    raise ManifestError(REFUSAL_REPLAY_NOT_IMPLEMENTED)


def acceptance_snapshot() -> dict:
    """给运维/文档用的现状快照：支持什么、拒绝什么、为什么。"""
    return {
        "manifest_version": MANIFEST_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "supported": [item.projection() for item in supported_manifests()],
        "unsupported": [item.projection() for item in MANIFESTS.values() if not item.supported],
        "declaration_violations": verify_declarations(),
        "replay": "not_implemented",
    }
