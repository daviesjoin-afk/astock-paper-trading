# -*- coding: utf-8 -*-
"""Leakage-safe purged walk-forward evaluation boundaries.

本模块回答**唯一一个问题**：

    离线历史验证时，"训练集 / 验证集 / 测试集"到底该怎么切，
    才能保证训练信息严格早于被评估的那段时间？

它刻意不回答"模型好不好""因子有没有效"。那些是 metric，不是 boundary。

──────────────────────────── 契约 ────────────────────────────

把 #146 与 #147 两条契约接起来：

    #146:  feature evidence <= decision T
    #147:  label evidence  > decision T，且 verified label 有明确 label_available_at
    本模块: train.label_available_at < evaluation_start_at

即一个样本能进 train，**不仅**要求它的决策在过去，还要求它的**完整未来收益
标签**在被评估的那段时间开始之前就已经可用::

    train_eligible =
        decision_session < validation_start_session
        and label_available_at < validation_start_at      # 严格 <
        and not embargoed
        and pit_status == verified

只判断 ``decision_at < validation_start`` 是**不够**的：一个较早的决策，其未来
收益标签可能跨进验证区间，于是训练集里就出现了验证期的价格。

五条不可协商的性质
------------------
1. **只允许过去训练、未来验证/测试。** 没有随机切分，没有 ``shuffle``，
   没有"test 两边都能当 train"的经典 purged KFold 作为默认。
2. **按决策 session 切，不按数据库行切。** 同一个 decision session 的**全部**
   股票必须落在同一个 split 里；否则同一天的一部分股票在 train、一部分在
   test，横截面信息直接串台。
3. **purge 依据 ``label_available_at``，不依据"horizon 天数的自然日减法"。**
   周末、节假日、停牌、不同 horizon 都会让固定天数减法算错。即使两个样本
   ``decision_at`` 相同，只要 ``horizon`` 不同，它们也可能一个能进 train、
   一个必须 purge。
4. **embargo 按交易日计。** 它是 evaluation 之前的隔离带，用来削弱相邻观测的
   相关性、共享行情状态与重叠 feature window。
5. **切分只消费已经通过 PIT 契约的样本。** 本模块不查当前 universe、不查当前
   上市状态、不查当前行业；它不决定"某个旧样本是否存在"，只决定"它属于哪个
   split"。

``label_available_at`` 从哪来
-----------------------------
**不在这里重新发明。** close-to-close 标签的可用时刻 = ``exit_date`` 当日
收盘（Asia/Shanghai 15:00），权威规则在 :func:`point_in_time.bar_available_at`
（#147 的 :func:`selection_labels.selection_label` 用的就是它）。
:func:`label_available_at_for_close` 直接委托给它，绝不写第二套
"exit_date 00:00 / decision_date / 下一个自然日"。

``evaluation_start_at`` 怎么定
------------------------------
被评估的那段区间从它**第一个决策 session 的开始**算起，即
``YYYY-MM-DDT00:00:00+08:00``。因此

* train 样本的 ``exit_date`` 必须落在验证期第一个 session **之前**的某个
  session 上（它的标签在 15:00 成熟，早于验证期的 00:00）；
* 若 ``exit_date`` 正好等于验证期第一个 session，它的标签要用**验证期内**的
  收盘价才能算出来 → 必须 purge。

这条边界是刻意保守的：它不依赖"验证期的第一个决策恰好是盘中还是收盘后"，
因此不会因为决策时刻的细节而放行一条边界样本。

train / validation / test 的职责
--------------------------------
========================  ==================================================
``train``                 fit 模型、fit 预处理统计量（**唯一**允许的来源）
``validation``            选超参 / 阈值 / 模型变体（**只能**看这里）
``test``                  最终 held-out 评分（**不得**回流进任何 fit/tuning）
========================  ==================================================

结构上就不给"把标签和特征混在一起的行"：验证/测试侧返回的
:class:`FeatureView` **没有** ``target`` 字段，:class:`LabelView` **没有**
``features`` 字段，:func:`fit_feature_state` 只接受带 ``target`` 的训练行。
调用者没有办法"顺手"把 held-out 的标签喂进 fit。

Fold 状态
---------
``ready`` / ``not_ready`` / ``insufficient_train_history`` /
``insufficient_validation_history`` / ``insufficient_test_history`` /
``insufficient_verified_labels``。非 ``ready`` 的 fold **不携带任何可训练数据**
（三个 partition 都是空的），绝不自动缩短训练窗口、缩短 horizon 或扩大 test。
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path (production and ``cd backend`` test runs)
    import point_in_time as PIT
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT

try:
    import selection_labels as SL
except ImportError:  # pragma: no cover - package-style import
    from . import selection_labels as SL


# ───────────────────────────── versioning ─────────────────────────────

WALK_FORWARD_CONTRACT_VERSION = "walk-forward-validation-v1"

PARTITION_TRAIN = "train"
PARTITION_VALIDATION = "validation"
PARTITION_TEST = "test"
VALIDATION_PARTITIONS = (PARTITION_TRAIN, PARTITION_VALIDATION, PARTITION_TEST)

#: 只有 ``verified`` 能进任何 split。#147 的 authoritative gate 产出这个值。
PIT_VERIFIED = "verified"

# ───────────────────────────── fold status ─────────────────────────────

STATUS_READY = "ready"
STATUS_NOT_READY = "not_ready"
STATUS_INSUFFICIENT_TRAIN_HISTORY = "insufficient_train_history"
STATUS_INSUFFICIENT_VALIDATION_HISTORY = "insufficient_validation_history"
STATUS_INSUFFICIENT_TEST_HISTORY = "insufficient_test_history"
STATUS_INSUFFICIENT_VERIFIED_LABELS = "insufficient_verified_labels"
FOLD_STATUSES = (
    STATUS_READY,
    STATUS_NOT_READY,
    STATUS_INSUFFICIENT_TRAIN_HISTORY,
    STATUS_INSUFFICIENT_VALIDATION_HISTORY,
    STATUS_INSUFFICIENT_TEST_HISTORY,
    STATUS_INSUFFICIENT_VERIFIED_LABELS,
)

REASON_OK = "ok"
REASON_WINDOW_NOT_MATURED = "window_not_matured"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"

#: Fold 级排除原因（受控词表，机器可读）。
#: 前五项是**逐 fold** 计数；后五项是**逐 run** 计数（每个 fold 里重复出现，
#: 使单条 fold 记录自成完整审计）。
FOLD_EXCLUSION_REASONS = (
    "label_not_available_before_fold",
    "validation_label_overlaps_test",
    "embargo",
    "outside_fold_window",
    "insufficient_history",
    REASON_WINDOW_NOT_MATURED,
    "unverified_label",
    "invalid_sample_identity",
    "invalid_target",
    "duplicate_sample",
    "label_version_mismatch",
)

RUN_SCOPED_REASONS = (
    "unverified_label",
    "invalid_sample_identity",
    "invalid_target",
    "duplicate_sample",
    "label_version_mismatch",
)


class WalkForwardContractError(ValueError):
    """契约被违反（不是"数据不够"，而是"结果不可信"）。"""


# ───────────────────────────── time helpers ─────────────────────────────


def china_tz() -> _dt.tzinfo:
    return PIT.china_tz()


def session_start_at(session: Any) -> Optional[str]:
    """某个决策 session 的**开始**时刻（tz-aware ISO）。

    ``2024-06-17`` → ``2024-06-17T00:00:00+08:00``。这是 evaluation 区间的
    起点定义，刻意早于该 session 内的任何决策时刻，因此不依赖"第一个决策是
    盘中还是收盘后"。
    """
    day = _date_text(session)
    if day is None:
        return None
    moment = _dt.datetime.combine(
        _dt.date.fromisoformat(day), _dt.time(0, 0), tzinfo=china_tz()
    )
    return moment.isoformat(timespec="seconds")


def label_available_at_for_close(exit_date: Any) -> Optional[str]:
    """close-to-close 标签的可用时刻 = ``exit_date`` 当日收盘。

    权威规则来自 :func:`point_in_time.bar_available_at`（#147 的标签模块用的
    就是它）。这里**不**重新定义"标签什么时候可用"。
    """
    try:
        moment = PIT.bar_available_at(_date_text(exit_date) or exit_date)
    except Exception:  # pragma: no cover - 已归一过的日期必然可解析
        return None
    return None if moment is None else moment.isoformat(timespec="seconds")


def _instant(value: Any) -> Optional[_dt.datetime]:
    """归一成 tz-aware 时点。无法解析 → ``None``（调用方须 fail closed）。"""
    if isinstance(value, _dt.datetime):
        return PIT.parse_available_at(value)
    return PIT.parse_available_at(value)


def _date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in {"", "nan", "nat", "none", "null", "-", "--"}:
        return None
    candidate = text[:10].replace("/", "-")
    try:
        _dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"nan", "nat", "none", "null", "-", "--"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) else None


# ───────────────────────────── sample ─────────────────────────────


@dataclass(frozen=True, slots=True)
class ValidationSample:
    """一条已经通过 PIT 契约、可参与 walk-forward 切分的样本。

    ``decision_session`` 与 ``label_available_at`` 是两个**必须分开**的时间
    概念：前者是"这条样本属于哪一天"，后者是"它的未来结果什么时候才真的
    可被看到"。切分只信后者。

    ``pit_status`` 默认是 ``unproven``（fail closed）：只有显式声明
    ``verified`` 的样本才可能进 train。
    """

    sample_key: str
    code: str
    decision_session: str
    label_available_at: str
    target: float
    horizon: int = 0
    label_version: Optional[str] = None
    decision_at: Optional[str] = None
    entry_date: Optional[str] = None
    exit_date: Optional[str] = None
    features: Mapping[str, Optional[float]] = field(default_factory=dict)
    pit_status: str = "unproven"
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.pit_status == PIT_VERIFIED


@dataclass(frozen=True, slots=True)
class FeatureView:
    """决策时点可见的特征。**没有 ``target``** —— 结构上无法参与标签拟合。"""

    sample_key: str
    code: str
    decision_session: str
    horizon: int
    label_version: Optional[str]
    features: Mapping[str, Optional[float]]


@dataclass(frozen=True, slots=True)
class LabelView:
    """未来结果标签。**没有 ``features``** —— 只能用于评分/度量。"""

    sample_key: str
    target: float
    label_available_at: str
    exit_date: Optional[str]


def feature_view(sample: ValidationSample) -> FeatureView:
    return FeatureView(
        sample_key=sample.sample_key,
        code=sample.code,
        decision_session=sample.decision_session,
        horizon=int(sample.horizon),
        label_version=sample.label_version,
        features=dict(sample.features or {}),
    )


def label_view(sample: ValidationSample) -> LabelView:
    return LabelView(
        sample_key=sample.sample_key,
        target=float(sample.target),
        label_available_at=sample.label_available_at,
        exit_date=sample.exit_date,
    )


# ───────────────────────── sample adapters ─────────────────────────


def sample_from_selection_label_record(
    record: Mapping[str, Any],
    *,
    features: Optional[Mapping[str, Any]] = None,
    code: Any = None,
) -> Optional[ValidationSample]:
    """把一条 #147 标签记录适配成 :class:`ValidationSample`。

    ``verified`` 判定**只走 #147 的 authoritative gate**
    (:func:`selection_labels.verified_evidence`)：``pending`` / ``unavailable``
    / ``invalid`` 以及"自称 verified 但证据不成立"的记录一律返回 ``None``，
    绝不当作负例、0 收益或坏股票。

    ``label_available_at`` 由 ``exit_date`` 按 :func:`label_available_at_for_close`
    推出（= #147 的 close-to-close 口径），不读调用方给的任何近似值。
    """
    verdict = SL.verified_evidence(record)
    if not verdict.get("verified"):
        return None
    exit_date = verdict.get("exit_date")
    available = label_available_at_for_close(exit_date)
    if available is None:
        return None
    decision_at = record.get("decision_at")
    moment = PIT.parse_asof(decision_at)
    if moment is None:
        return None
    session = moment.astimezone(china_tz()).date().isoformat()
    key = verdict.get("sample_key") or record.get("sample_key")
    if not key:
        return None
    target = _finite(verdict.get("label_score"))
    if target is None:
        return None
    return ValidationSample(
        sample_key=str(key),
        code=str(code if code is not None else record.get("code") or "").strip(),
        decision_session=session,
        label_available_at=available,
        target=target,
        horizon=int(record.get("horizon") or 0),
        label_version=verdict.get("label_version"),
        decision_at=str(decision_at) if decision_at else None,
        entry_date=verdict.get("entry_date"),
        exit_date=exit_date,
        features={str(k): _finite(v) for k, v in dict(features or {}).items()},
        pit_status=PIT_VERIFIED,
        provenance={"source": "selection_labels", "label_reason": verdict.get("reason")},
    )


# ───────────────────────────── config ─────────────────────────────


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Walk-forward 切分配置。**没有任何"自动调参"字段。**

    ``min_train_sessions`` / ``validation_sessions`` / ``test_sessions``
    以**决策 session** 计。``step_sessions`` 为 ``None`` 时取
    ``validation_sessions + test_sessions``（fold 之间评估窗口不重叠，与
    "train S1-S60 / val S61-S70 / test S71-S80，下一折 train S1-S80" 的
    经典形态一致）。

    ``embargo_sessions`` 是 validation/test 之前的时间隔离带，按交易日计。
    默认 ``0``：本项目没有既有的 embargo 业务约定，**不拍脑袋调优**，由调用方
    显式设置。

    ``max_train_sessions`` 为 ``None`` 表示 expanding window；显式给值才是
    rolling window。绝不偷偷滚动。
    """

    min_train_sessions: int
    validation_sessions: int
    test_sessions: int
    step_sessions: Optional[int] = None
    embargo_sessions: int = 0
    max_train_sessions: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("min_train_sessions", "validation_sessions", "test_sessions"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise WalkForwardContractError(f"{name} must be a positive integer")
        if self.step_sessions is not None and (
            not isinstance(self.step_sessions, int)
            or isinstance(self.step_sessions, bool)
            or self.step_sessions < 1
        ):
            raise WalkForwardContractError("step_sessions must be a positive integer or None")
        if not isinstance(self.embargo_sessions, int) or isinstance(self.embargo_sessions, bool):
            raise WalkForwardContractError("embargo_sessions must be an integer")
        if self.embargo_sessions < 0:
            raise WalkForwardContractError("embargo_sessions must not be negative")
        if self.max_train_sessions is not None and (
            not isinstance(self.max_train_sessions, int)
            or isinstance(self.max_train_sessions, bool)
            or self.max_train_sessions < 1
        ):
            raise WalkForwardContractError("max_train_sessions must be a positive integer or None")

    @property
    def resolved_step(self) -> int:
        if self.step_sessions is not None:
            return int(self.step_sessions)
        return int(self.validation_sessions) + int(self.test_sessions)


# ───────────────────────────── fold ─────────────────────────────


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """一个 fold 的三段边界、结构分离的数据、以及可审计的元数据。

    ``train_rows`` 是唯一带 ``target`` 的集合；``validation_features`` /
    ``test_features`` 没有标签，``validation_labels`` / ``test_labels`` 没有
    特征。``refit_rows`` 是"train + validation 重新拟合"的合法集合
    （eligibility = ``label_available_at < test_start_at``，见 §30）。
    """

    fold_id: int
    status: str
    reason: str
    train_rows: tuple = ()
    validation_features: tuple = ()
    validation_labels: tuple = ()
    test_features: tuple = ()
    test_labels: tuple = ()
    refit_rows: tuple = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    exclusion_reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def validation_start_at(self) -> Optional[str]:
        return self.metadata.get("validation_start_at")

    @property
    def test_start_at(self) -> Optional[str]:
        return self.metadata.get("test_start_at")

    def keys(self, partition: str) -> frozenset:
        if partition == PARTITION_TRAIN:
            return frozenset(row.sample_key for row in self.train_rows)
        if partition == PARTITION_VALIDATION:
            return frozenset(row.sample_key for row in self.validation_features)
        if partition == PARTITION_TEST:
            return frozenset(row.sample_key for row in self.test_features)
        raise WalkForwardContractError(f"unknown partition: {partition!r}")


def assert_fold_disjoint(fold: WalkForwardFold) -> None:
    """断言三个 split 的 ``sample_key`` 两两不相交。

    同一条样本同时出现在 train 与 validation/test，等于用被评估的那段时间
    训练模型 —— 这是本模块存在的理由，因此在这里**硬断言**而不是"报告一下"。
    """
    train = fold.keys(PARTITION_TRAIN)
    validation = fold.keys(PARTITION_VALIDATION)
    test = fold.keys(PARTITION_TEST)
    for left_name, left, right_name, right in (
        (PARTITION_TRAIN, train, PARTITION_VALIDATION, validation),
        (PARTITION_TRAIN, train, PARTITION_TEST, test),
        (PARTITION_VALIDATION, validation, PARTITION_TEST, test),
    ):
        overlap = left & right
        if overlap:
            raise WalkForwardContractError(
                f"fold {fold.fold_id}: sample_key overlap between {left_name} and "
                f"{right_name}: {sorted(overlap)[:5]}"
            )


def assert_train_boundary(fold: WalkForwardFold) -> None:
    """断言每条 train 样本的标签在评估开始前**严格**可用（§29）。"""
    boundary = fold.validation_start_at or fold.test_start_at
    if boundary is None or not fold.train_rows:
        return
    limit = _instant(boundary)
    for row in fold.train_rows:
        available = _instant(row.label_available_at)
        if available is None or limit is None or not available < limit:
            raise WalkForwardContractError(
                f"fold {fold.fold_id}: train sample {row.sample_key} has "
                f"label_available_at={row.label_available_at!r} which is not strictly "
                f"before the evaluation start {boundary!r}"
            )


def assert_fold_ready(fold: WalkForwardFold) -> WalkForwardFold:
    """消费 fold 前的唯一入口：非 ready 一律抛错，而不是"自己看状态"。"""
    if not fold.ready:
        raise WalkForwardContractError(
            f"fold {fold.fold_id} is {fold.status} ({fold.reason}); refusing to hand out data"
        )
    assert_fold_disjoint(fold)
    assert_train_boundary(fold)
    return fold


def train_eligible(sample: Any, *, evaluation_start_at: Any) -> bool:
    """训练样本的唯一 purge 判据：标签在评估开始前**严格**可用。

    这是全仓**唯一**一处 purge 判定（``learning_dataset.chronological_split``
    也走它）。``label_available_at`` 缺失或不可解析 → fail closed（不可用）：
    "不知道标签什么时候可用"不等于"它早就可用了"。

    边界是严格 ``<``：``label_available_at == evaluation_start_at`` 仍然
    purge —— 在验证期第一个决策发生时，这条标签才刚刚可见，不能算"训练时
    就已经知道"。
    """
    available = _instant(getattr(sample, "label_available_at", None))
    limit = _instant(evaluation_start_at)
    if available is None or limit is None:
        return False
    return available < limit


# ───────────────────────── preprocessing / tuning ─────────────────────────


@dataclass(frozen=True, slots=True)
class FeatureFitState:
    """只在 train 上拟合出来的特征统计量。**不可从 validation/test 重建。**"""

    feature_names: tuple
    means: Mapping[str, float]
    scales: Mapping[str, float]
    rows: int
    sample_keys: tuple


def fit_feature_state(train_rows: Sequence[Any]) -> FeatureFitState:
    """在**训练行**上拟合每列均值/标准差。

    只接受带 ``target`` 的行（即 train 行）。:class:`FeatureView` 与
    :class:`LabelView` 都会被拒绝 —— "fit 全量数据"在这个接口上无法表达，
    而不是靠调用者自觉不看 held-out。
    """
    rows = list(train_rows or ())
    if not rows:
        raise WalkForwardContractError("cannot fit feature state on an empty train set")
    for row in rows:
        if isinstance(row, (FeatureView, LabelView)):
            raise WalkForwardContractError(
                "fit_feature_state only accepts training rows; a feature/label view "
                "carries no target and must never be used to fit preprocessing"
            )
        if not hasattr(row, "target") or not hasattr(row, "features"):
            raise WalkForwardContractError(
                f"fit_feature_state requires rows with features and target, got {type(row).__name__}"
            )
    names = sorted({str(name) for row in rows for name in (row.features or {})})
    means = {}
    scales = {}
    for name in names:
        values = [
            value
            for value in (_finite((row.features or {}).get(name)) for row in rows)
            if value is not None
        ]
        if not values:
            means[name] = 0.0
            scales[name] = 1.0
            continue
        mean = math.fsum(values) / len(values)
        variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
        means[name] = mean
        scales[name] = math.sqrt(variance) if variance > 0 else 1.0
    return FeatureFitState(
        feature_names=tuple(names),
        means=means,
        scales=scales,
        rows=len(rows),
        sample_keys=tuple(sorted(str(row.sample_key) for row in rows)),
    )


def transform_features(state: FeatureFitState, features: Mapping[str, Any]) -> dict:
    """用 **train** 拟合出来的统计量变换任意一组特征。"""
    out = {}
    for name in state.feature_names:
        value = _finite((features or {}).get(name))
        out[name] = None if value is None else (value - state.means[name]) / state.scales[name]
    return out


#: 任何"held-out 指标"容器都不得出现在候选里。
_FORBIDDEN_CANDIDATE_METRIC_KEYS = (
    "test_metrics",
    "holdout_metrics",
    "test",
    "holdout",
    "test_score",
    "holdout_score",
)


def select_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    metric_name: str,
    higher_is_better: bool = True,
) -> dict:
    """只用 **validation** 指标选候选配置。

    每个候选形如 ``{"candidate_id": "a", "validation_metrics": {"rank_ic": 0.03}}``。
    只要候选里出现任何 held-out 指标容器（``test_metrics`` / ``holdout_metrics``
    / ``test`` / ``holdout`` …），直接拒绝 —— 模型选择看到 test 表现，就把
    out-of-sample 声明变成了 in-sample。
    """
    items = list(candidates or ())
    if not items:
        raise WalkForwardContractError("no candidates to select from")
    for candidate in items:
        leaked = sorted(set(candidate) & set(_FORBIDDEN_CANDIDATE_METRIC_KEYS))
        if leaked:
            raise WalkForwardContractError(
                f"candidate selection must not see held-out metrics: {leaked}"
            )
    scored = []
    for candidate in items:
        metrics = candidate.get("validation_metrics")
        if not isinstance(metrics, Mapping):
            raise WalkForwardContractError("candidate is missing validation_metrics")
        value = _finite(metrics.get(metric_name))
        if value is None:
            raise WalkForwardContractError(
                f"candidate {candidate.get('candidate_id')!r} has no finite {metric_name!r}"
            )
        scored.append((value, str(candidate.get("candidate_id") or ""), candidate))
    best = max(scored, key=lambda item: item[0]) if higher_is_better else min(
        scored, key=lambda item: item[0]
    )
    return {
        "candidate_id": best[1],
        "metric_name": metric_name,
        "metric_value": best[0],
        "higher_is_better": bool(higher_is_better),
        "selection_partition": PARTITION_VALIDATION,
        "reason": REASON_OK,
    }


# ───────────────────────────── the splitter ─────────────────────────────


def _session_of(row: Any) -> Optional[str]:
    """样本的 canonical 决策 session（``YYYY-MM-DD``）。

    **所有**读取 ``decision_session`` 的地方都走这里：``2024/06/03`` 与
    ``2024-06-03`` 必须是**同一个** session，否则同一天会被拆成两天，或者与
    显式给出的 canonical ``sessions`` 静默匹配不上。
    """
    return _date_text(getattr(row, "decision_session", None))


def _normalize_samples(samples: Sequence[Any], *, label_version: Optional[str]) -> tuple:
    """全局预过滤：身份、去重、verified gate、有限 target。

    返回 ``(eligible, timeline, reasons, unverified_by_status)``。

    ``timeline`` 是**独立的决策 session 时间轴**：只要一条输入行的
    ``decision_session`` 结构上可解析就计入，**不看**它的标签是否 verified /
    是否 pending / target 是否有限 / 是否与别人冲突。

    Fold 边界必须由这条时间轴决定。若用"已经通过 verified gate 的样本"反推，
    一个全部标签仍 pending 的最近 session 会从时间轴上直接消失，validation/test
    边界随之左移 —— 真实的 fold 不是 ``not_ready``，而是根本不存在。标签状态
    只能决定 eligibility / readiness，不能决定"这一天是否存在"。
    """
    reasons = {reason: 0 for reason in FOLD_EXCLUSION_REASONS}
    unverified_by_status: dict = {}
    timeline = set()
    chosen: dict = {}
    conflicted = set()
    for raw in samples or ():
        key = str(getattr(raw, "sample_key", "") or "").strip()
        session = _session_of(raw)
        if session is not None:
            # 结构上合法的一天 —— 无论标签什么状态，这一天都真实存在过。
            timeline.add(session)
        available = _instant(getattr(raw, "label_available_at", None))
        if not key or session is None or available is None:
            reasons["invalid_sample_identity"] += 1
            continue
        if label_version is not None and str(getattr(raw, "label_version", "") or "") != label_version:
            reasons["label_version_mismatch"] += 1
            continue
        status = str(getattr(raw, "pit_status", "") or "")
        if status != PIT_VERIFIED:
            # pending != negative, unavailable != 0, invalid != bad stock.
            reasons["unverified_label"] += 1
            unverified_by_status[status or "unknown"] = (
                unverified_by_status.get(status or "unknown", 0) + 1
            )
            continue
        if _finite(getattr(raw, "target", None)) is None:
            reasons["invalid_target"] += 1
            continue
        if key in conflicted:
            reasons["duplicate_sample"] += 1
            continue
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = raw
            continue
        if previous == raw:
            # 逐字节相同的重复：任何输入顺序都得到同一个结果，安全去重。
            reasons["duplicate_sample"] += 1
            continue
        # 同一身份、不同内容：不选第一个也不选最后一个 —— 全部拒绝。
        del chosen[key]
        conflicted.add(key)
        reasons["duplicate_sample"] += 2
    ordered = sorted(chosen.values(), key=lambda row: (_session_of(row) or "", row.sample_key))
    return ordered, sorted(timeline), reasons, unverified_by_status


def _empty_fold(fold_id: int, status: str, reason: str, metadata: dict, reasons: dict) -> WalkForwardFold:
    return WalkForwardFold(
        fold_id=fold_id,
        status=status,
        reason=reason,
        metadata=metadata,
        exclusion_reasons={name: int(reasons.get(name, 0)) for name in FOLD_EXCLUSION_REASONS},
    )


def _base_metadata(
    *,
    config: WalkForwardConfig,
    label_version: Optional[str],
    validation_start_at: Optional[str] = None,
    test_start_at: Optional[str] = None,
) -> dict:
    return {
        "contract_version": WALK_FORWARD_CONTRACT_VERSION,
        "validation_start_at": validation_start_at,
        "test_start_at": test_start_at,
        "purge_cutoff_at": validation_start_at,
        "embargo_sessions": int(config.embargo_sessions),
        "min_train_sessions": int(config.min_train_sessions),
        "max_train_sessions": config.max_train_sessions,
        "window": "rolling" if config.max_train_sessions is not None else "expanding",
        "label_version": label_version,
        "train_decision_start": None,
        "train_decision_end": None,
        "validation_decision_start": None,
        "validation_decision_end": None,
        "test_decision_start": None,
        "test_decision_end": None,
        "train_candidate_rows": 0,
        "train_purged_rows": 0,
        "train_embargoed_rows": 0,
        "train_window_excluded_rows": 0,
        "train_final_rows": 0,
        "validation_candidate_rows": 0,
        "validation_purged_rows": 0,
        "validation_rows": 0,
        "test_rows": 0,
        "refit_rows": 0,
        "refit_excluded_rows": 0,
        "train_decision_sessions": [],
        "validation_decision_sessions": [],
        "test_decision_sessions": [],
        "train_decision_session_count": 0,
        "train_final_session_count": 0,
        "validation_decision_session_count": 0,
        "test_decision_session_count": 0,
        "train_max_label_available_at": None,
        "validation_max_label_available_at": None,
        "train_decision_max": None,
        "disjoint_ok": True,
        "boundary_ok": True,
    }


def label_ready_by_asof(sample: Any, *, asof: Any) -> bool:
    """标签在 ``asof`` 时点是否**已经可见**。

    这是 #146 的 PIT 可见性契约（:func:`point_in_time.is_visible_at`，
    ``available_at > asof → future``，即 ``available_at <= asof`` 才可见），
    与 #147 的 ``selection_label()`` 判定 ``pending`` 的口径一致
    （``asof < exit_available_at`` 才是 pending）。

    刻意与 :func:`train_eligible` 分开：训练证据要求**严格**早于评估开始
    （``<``），而"截至 asof 这条标签成熟了没有"是 ``<=``。两者混用会把
    ``label_available_at == asof`` 这种**已经可见**的标签误判成未成熟 ——
    例如 exit 收盘 15:00 可用、``asof`` 也是 15:00 时，#147 已经放行。
    """
    available = getattr(sample, "label_available_at", None)
    if _instant(available) is None:
        return False
    try:
        return bool(PIT.is_visible_at(available, asof).get("visible"))
    except Exception:  # pragma: no cover - is_visible_at 不抛异常
        return False


def _finalize_report(report: dict, folds: Sequence[WalkForwardFold]) -> dict:
    """把 fold 汇总写进 report —— **每一条返回路径**都必须经过这里。

    提前返回（历史不足）与正常返回如果各自拼一份 report，就会在"恰恰是
    not-ready 的那种情况"下给出互相矛盾的 fold 计数，或者干脆缺键。
    """
    report["folds"] = len(folds)
    report["ready_folds"] = sum(1 for fold in folds if fold.ready)
    report["fold_statuses"] = sorted({fold.status for fold in folds})
    report["ready"] = report["ready_folds"] > 0
    report["fold_ids"] = [fold.fold_id for fold in folds]
    return report


def build_walk_forward_folds(
    samples: Sequence[Any],
    config: WalkForwardConfig,
    *,
    sessions: Optional[Sequence[Any]] = None,
    asof: Any = None,
    label_version: Optional[str] = None,
) -> dict:
    """把已经 PIT 化的样本切成 chronological、past-only 的 walk-forward folds。

    ``sessions``
        权威交易日序列（升序）。不给时，fold 边界来自**独立的决策 session
        时间轴** —— 只要一条输入行的 ``decision_session`` 结构上可解析就计入，
        **不看**标签是否 verified。用一个"全部标签仍 pending"的最近 session 去
        反推日历，会让那一天从时间轴上消失、边界左移；标签状态只能决定
        eligibility / readiness，不能决定"这一天是否存在"。embargo 也按这条
        序列计**交易日**，因此给它真实日历才能让 "N 个 session" 不等于
        "N 个自然日"。
    ``asof``
        评估时点。给了以后，只要有一条 test 标签在 ``asof`` 时点**尚不可见**
        （#146 的 PIT 可见性，``label_available_at <= asof``），该 fold 一律
        ``not_ready / window_not_matured`` —— 绝不缩短 horizon、绝不把尚未成熟
        的 test 当 ready。
    ``label_version``
        只保留该版本的标签（其余计入 ``label_version_mismatch``）。不同 horizon
        的样本按各自 ``label_available_at`` 独立判定，不会被一起 purge。

    ``min_train_sessions`` 是**最终**训练集的 session 数下限：purge / embargo /
    rolling 之后不足就 ``insufficient_train_history``（不给任何可训练数据），
    但循环继续，后面的 fold 仍可 ready。
    """
    if not isinstance(config, WalkForwardConfig):
        raise WalkForwardContractError("config must be a WalkForwardConfig")

    ordered, timeline, reasons, unverified_by_status = _normalize_samples(
        samples, label_version=label_version
    )

    if sessions:
        calendar = sorted({day for day in (_date_text(item) for item in sessions) if day})
        timeline_source = "explicit_sessions"
    else:
        # Fold 边界来自**独立的决策 session 时间轴**，不是"已通过 verified gate
        # 的样本"—— 否则一个全部标签仍 pending 的最近 session 会从时间轴上消失，
        # 后续 validation/test 边界随标签成熟状态左移。
        calendar = list(timeline)
        timeline_source = "decision_timeline"

    by_session: dict = {}
    for row in ordered:
        by_session.setdefault(_session_of(row) or "", []).append(row)

    asof_instant = _instant(asof) if asof is not None else None
    if asof is not None and asof_instant is None:
        raise WalkForwardContractError("asof must be a parseable instant")

    report = {
        "contract_version": WALK_FORWARD_CONTRACT_VERSION,
        "input_rows": len(list(samples or ())),
        "eligible_rows": len(ordered),
        "sessions": len(calendar),
        "folds": 0,
        "ready_folds": 0,
        "ready": False,
        "fold_statuses": [],
        "timeline_source": timeline_source,
        "timeline_session_count": len(calendar),
        "exclusion_reasons": dict(reasons),
        "unverified_label_by_status": dict(sorted(unverified_by_status.items())),
        "config": {
            "min_train_sessions": config.min_train_sessions,
            "validation_sessions": config.validation_sessions,
            "test_sessions": config.test_sessions,
            "step_sessions": config.resolved_step,
            "embargo_sessions": config.embargo_sessions,
            "max_train_sessions": config.max_train_sessions,
        },
    }

    needed = config.min_train_sessions + config.validation_sessions + config.test_sessions
    if len(calendar) < needed:
        # fail closed：不缩短训练窗口、不缩短 horizon、不扩大 test。
        if len(calendar) < config.min_train_sessions:
            status = STATUS_INSUFFICIENT_TRAIN_HISTORY
        elif len(calendar) < config.min_train_sessions + config.validation_sessions:
            status = STATUS_INSUFFICIENT_VALIDATION_HISTORY
        else:
            status = STATUS_INSUFFICIENT_TEST_HISTORY
        metadata = _base_metadata(config=config, label_version=label_version)
        metadata["fold_id"] = 0
        metadata["sessions_required"] = needed
        metadata["sessions_available"] = len(calendar)
        fold = _empty_fold(
            0,
            status,
            REASON_INSUFFICIENT_HISTORY,
            metadata,
            {**reasons, "insufficient_history": len(ordered)},
        )
        report["exclusion_reasons"]["insufficient_history"] = len(ordered)
        _finalize_report(report, [fold])
        return {"folds": [fold], "report": report}

    folds = []
    fold_id = 0
    train_end = config.min_train_sessions
    step = config.resolved_step
    while train_end + config.validation_sessions + config.test_sessions <= len(calendar):
        validation_start_index = train_end
        validation_end_index = validation_start_index + config.validation_sessions
        test_start_index = validation_end_index
        test_end_index = test_start_index + config.test_sessions

        validation_window = calendar[validation_start_index:validation_end_index]
        test_window = calendar[test_start_index:test_end_index]
        validation_start_at = session_start_at(validation_window[0])
        test_start_at = session_start_at(test_window[0])

        metadata = _base_metadata(
            config=config,
            label_version=label_version,
            validation_start_at=validation_start_at,
            test_start_at=test_start_at,
        )
        metadata["fold_id"] = fold_id
        metadata["validation_decision_start"] = validation_window[0]
        metadata["validation_decision_end"] = validation_window[-1]
        metadata["test_decision_start"] = test_window[0]
        metadata["test_decision_end"] = test_window[-1]
        metadata["validation_decision_sessions"] = list(validation_window)
        metadata["test_decision_sessions"] = list(test_window)
        metadata["validation_decision_session_count"] = len(validation_window)
        metadata["test_decision_session_count"] = len(test_window)

        fold_reasons = {name: int(reasons.get(name, 0)) for name in FOLD_EXCLUSION_REASONS}

        validation_candidates = [
            row for day in validation_window for row in by_session.get(day, ())
        ]
        test_rows = [row for day in test_window for row in by_session.get(day, ())]

        # readiness 按**每个 test 标签是否已经可见**判定，不是按 test 最后一个决策
        # session 的日期：一个 06-27 的决策，horizon 2 的标签要到 07-01 才成熟，
        # 用日期比较会把它当成"已经可以评分"。判据是 #146 的 PIT 可见性
        # （``label_available_at <= asof``），**不是** train 侧的严格 ``<`` ——
        # 一条恰好在 asof 时刻可见的标签（exit 收盘 15:00、asof 也是 15:00）
        # 已经可评分。``asof`` 走 PIT 的时点语义（date-only = 当日结束，
        # 带时刻 = 精确时点），因此盘中 cutoff 不会被放大成整天。
        if asof_instant is not None and any(
            not label_ready_by_asof(row, asof=asof) for row in test_rows
        ):
            metadata["asof"] = asof_instant.isoformat(timespec="seconds")
            metadata["window_excluded_rows"] = len(ordered)
            fold_reasons[REASON_WINDOW_NOT_MATURED] = len(ordered)
            folds.append(
                _empty_fold(fold_id, STATUS_NOT_READY, REASON_WINDOW_NOT_MATURED, metadata, fold_reasons)
            )
            fold_id += 1
            train_end += step
            continue

        # ── train 候选：决策 session 严格早于 validation 起点 ──
        prior = [day for day in calendar if day < validation_window[0]]
        embargoed = set(prior[-config.embargo_sessions:]) if config.embargo_sessions else set()
        embargo_eligible = [day for day in prior if day not in embargoed]
        if config.max_train_sessions is not None:
            window_days = set(embargo_eligible[-config.max_train_sessions:])
        else:
            window_days = set(embargo_eligible)

        train_candidates = [row for day in prior for row in by_session.get(day, ())]
        train_rows = []
        for row in train_candidates:
            if not train_eligible(row, evaluation_start_at=validation_start_at):
                metadata["train_purged_rows"] += 1
                fold_reasons["label_not_available_before_fold"] += 1
                continue
            session = _session_of(row)
            if session in embargoed:
                metadata["train_embargoed_rows"] += 1
                fold_reasons["embargo"] += 1
                continue
            if session not in window_days:
                metadata["train_window_excluded_rows"] += 1
                fold_reasons["outside_fold_window"] += 1
                continue
            train_rows.append(row)

        # ``min_train_sessions`` 是**最终**训练集的 session 数下限，不是"purge 前
        # 有多少个 prior session"。purge / embargo / rolling 之后不够，就 fail
        # closed：这个 fold 不给任何可训练数据，但循环继续，后面的 fold 仍可 ready。
        # 计数先落进 metadata，这样 not-ready 的 fold 也自带完整审计记录。
        metadata["train_candidate_rows"] = len(train_candidates)
        metadata["train_final_rows"] = len(train_rows)
        train_sessions = sorted({_session_of(row) or "" for row in train_rows})
        metadata["train_decision_sessions"] = train_sessions
        metadata["train_decision_session_count"] = len(train_sessions)
        metadata["train_final_session_count"] = len(train_sessions)
        if len(train_sessions) < config.min_train_sessions:
            metadata["insufficient_train_history"] = True
            fold_reasons["insufficient_history"] = len(train_candidates)
            folds.append(
                _empty_fold(
                    fold_id,
                    STATUS_INSUFFICIENT_TRAIN_HISTORY,
                    REASON_INSUFFICIENT_HISTORY,
                    metadata,
                    fold_reasons,
                )
            )
            fold_id += 1
            train_end += step
            continue

        # ── validation 侧同样要按 **test 起点** purge ──
        # 一条在 test 期内才成熟的 validation 标签，其 exit 价就在 held-out 区间里：
        # 用它选超参等于让模型选择看见 test。只把它从 ``refit_rows`` 里去掉是不够的。
        validation_rows = []
        validation_purged = 0
        for row in validation_candidates:
            if not train_eligible(row, evaluation_start_at=test_start_at):
                validation_purged += 1
                continue
            validation_rows.append(row)

        # ── refit（train + validation）的 eligibility 是 label < test_start ──
        # 上面的 validation purge 已经保证了这一点；这里保留一次显式检查作为
        # 纵深防御：将来若有人放宽 validation 的 purge 规则，最终拟合仍然安全。
        refit_rows = []
        refit_excluded = 0
        for row in train_rows + validation_rows:
            if not train_eligible(row, evaluation_start_at=test_start_at):
                refit_excluded += 1
                continue
            refit_rows.append(row)

        metadata["validation_candidate_rows"] = len(validation_candidates)
        metadata["validation_purged_rows"] = validation_purged
        metadata["validation_rows"] = len(validation_rows)
        metadata["test_rows"] = len(test_rows)
        metadata["refit_rows"] = len(refit_rows)
        metadata["refit_excluded_rows"] = refit_excluded
        fold_reasons["validation_label_overlaps_test"] = validation_purged
        if validation_rows:
            metadata["validation_max_label_available_at"] = max(
                row.label_available_at for row in validation_rows
            )
        if train_rows:
            metadata["train_decision_start"] = train_sessions[0]
            metadata["train_decision_end"] = train_sessions[-1]
            metadata["train_max_label_available_at"] = max(
                row.label_available_at for row in train_rows
            )
            metadata["train_decision_max"] = train_sessions[-1]
        limit = _instant(validation_start_at)
        metadata["boundary_ok"] = bool(train_rows) and all(
            (_instant(row.label_available_at) or limit) < limit for row in train_rows
        )

        if not train_rows or not validation_rows or not test_rows:
            metadata["insufficient_verified_labels"] = True
            fold_reasons["insufficient_history"] = (
                len(train_candidates) + len(validation_rows) + len(test_rows)
            )
            folds.append(
                _empty_fold(
                    fold_id,
                    STATUS_INSUFFICIENT_VERIFIED_LABELS,
                    REASON_INSUFFICIENT_HISTORY,
                    metadata,
                    fold_reasons,
                )
            )
            fold_id += 1
            train_end += step
            continue

        fold = WalkForwardFold(
            fold_id=fold_id,
            status=STATUS_READY,
            reason=REASON_OK,
            train_rows=tuple(train_rows),
            validation_features=tuple(feature_view(row) for row in validation_rows),
            validation_labels=tuple(label_view(row) for row in validation_rows),
            test_features=tuple(feature_view(row) for row in test_rows),
            test_labels=tuple(label_view(row) for row in test_rows),
            refit_rows=tuple(refit_rows),
            metadata=metadata,
            exclusion_reasons=fold_reasons,
        )
        assert_fold_disjoint(fold)
        assert_train_boundary(fold)
        folds.append(fold)
        fold_id += 1
        train_end += step

    if not folds:
        metadata = _base_metadata(config=config, label_version=label_version)
        metadata["fold_id"] = 0
        metadata["sessions_required"] = needed
        metadata["sessions_available"] = len(calendar)
        folds.append(
            _empty_fold(
                0,
                STATUS_NOT_READY,
                REASON_INSUFFICIENT_HISTORY,
                metadata,
                {**reasons, "insufficient_history": len(ordered)},
            )
        )

    _finalize_report(report, folds)
    return {"folds": folds, "report": report}


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    sessions = [f"2024-06-{day:02d}" for day in range(3, 29)]
    samples = []
    for index, day in enumerate(sessions):
        exit_day = sessions[min(index + 2, len(sessions) - 1)]
        samples.append(
            ValidationSample(
                sample_key=f"k{index:03d}",
                code="600001",
                decision_session=day,
                label_available_at=label_available_at_for_close(exit_day),
                target=0.01,
                horizon=2,
                label_version="selection-label-v1/raw-return",
                exit_date=exit_day,
                pit_status=PIT_VERIFIED,
                features={"momentum": float(index)},
            )
        )
    config = WalkForwardConfig(
        min_train_sessions=10, validation_sessions=4, test_sessions=4
    )
    built = build_walk_forward_folds(samples, config)
    folds = built["folds"]
    # 第一折的 prior 窗口恰好等于 ``min_train_sessions``，而 purge 会从尾部拿走
    # horizon 条 → 第一折按定义不够，必须是显式的 insufficient_train_history，
    # 而不是"消失"或"降级为 ready"。
    assert folds[0].status == STATUS_INSUFFICIENT_TRAIN_HISTORY, folds[0].status
    ready = [fold for fold in folds if fold.ready]
    assert ready, built["report"]
    for fold in ready:
        assert_train_boundary(fold)
        assert_fold_disjoint(fold)
        assert fold.metadata["train_decision_session_count"] >= config.min_train_sessions
        assert fold.validation_start_at is not None
    assert built["report"]["timeline_source"] == "decision_timeline"
    print("walk_forward_validation self-check: ok")


if __name__ == "__main__":
    _self_check()
