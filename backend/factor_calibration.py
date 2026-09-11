# -*- coding: utf-8 -*-
"""Deterministic factor-calibration primitives.

The production contract deliberately separates two concepts:

* ``alpha_score`` describes the economic signal carried by observed factors.
* ``evidence_quality`` describes how much trustworthy evidence supports that
  score.  Quality never scales or mutates a factor's economic value.

Missing observations remain missing.  When a composite has some observed
components, their configured weights are renormalised over the observed set;
when none are observed the composite is ``NaN``.  Research helpers in this
module are read-only and must never activate strategies or submit orders.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


CALIBRATION_VERSION = "factor-calibration-v2"
FULL_QUALITY = 1.0
PROXY_QUALITY = 0.60
UNADJUSTED_PRICE_QUALITY = 0.70
MIN_RUNTIME_EVIDENCE_QUALITY = 0.60


@dataclass(frozen=True)
class FactorScore:
    alpha_score: pd.Series
    evidence_quality: pd.Series
    observed_weight: pd.Series
    required_ok: pd.Series


def _float_series(values, index=None) -> pd.Series:
    series = pd.Series(values, index=index) if not isinstance(values, pd.Series) else values.copy()
    return pd.to_numeric(series, errors="coerce").astype("float64")


def weighted_available(
    components: Mapping[str, pd.Series],
    weights: Mapping[str, float],
    *,
    index=None,
) -> pd.Series:
    """Weighted composite with pairwise missingness and per-row renormalisation.

    ``NaN`` is not zero: only observed components contribute to the numerator
    and denominator.  A row with no observed component remains ``NaN``.
    """
    if not components:
        return pd.Series(dtype="float64", index=index)
    if index is None:
        first = next(iter(components.values()))
        index = first.index
    numerator = pd.Series(0.0, index=index, dtype="float64")
    denominator = pd.Series(0.0, index=index, dtype="float64")
    for name, values in components.items():
        weight = float(weights.get(name, 0.0))
        if not math.isfinite(weight) or weight <= 0:
            continue
        series = _float_series(values).reindex(index)
        known = series.notna()
        numerator = numerator.add(series.where(known, 0.0) * weight, fill_value=0.0)
        denominator = denominator.add(known.astype(float) * weight, fill_value=0.0)
    return numerator.div(denominator.where(denominator > 0))


def score_factors(
    frame: pd.DataFrame,
    weights: Mapping[str, float],
    *,
    quality_columns: Mapping[str, str] | None = None,
    required_factors: Sequence[str] = (),
) -> FactorScore:
    """Return alpha and evidence quality without conflating the two.

    Alpha is renormalised over observed factors.  Evidence quality uses the
    *original* configured weight budget, so missing evidence contributes zero
    quality instead of becoming a neutral alpha value.  Required factors are
    represented separately and may be used as a fail-closed runtime gate.
    """
    index = frame.index
    clean_weights = {
        str(name): float(weight)
        for name, weight in weights.items()
        if isinstance(weight, (int, float)) and not isinstance(weight, bool)
        and math.isfinite(float(weight)) and float(weight) > 0
    }
    total_weight = sum(clean_weights.values())
    if total_weight <= 0:
        nan = pd.Series(np.nan, index=index, dtype="float64")
        zero = pd.Series(0.0, index=index, dtype="float64")
        return FactorScore(nan, zero, zero, pd.Series(False, index=index, dtype=bool))

    numerator = pd.Series(0.0, index=index, dtype="float64")
    observed_weight = pd.Series(0.0, index=index, dtype="float64")
    quality_budget = pd.Series(0.0, index=index, dtype="float64")
    quality_columns = dict(quality_columns or {})

    for factor, weight in clean_weights.items():
        values = _float_series(
            frame[factor] if factor in frame else pd.Series(np.nan, index=index)
        ).reindex(index)
        observed = values.notna()
        numerator += values.where(observed, 0.0) * weight
        observed_weight += observed.astype(float) * weight

        quality_name = quality_columns.get(factor, f"{factor}_evidence_quality")
        if quality_name in frame:
            quality = _float_series(frame[quality_name]).reindex(index).clip(0.0, 1.0)
        else:
            quality = pd.Series(FULL_QUALITY, index=index, dtype="float64")
        quality_budget += quality.where(observed, 0.0).fillna(0.0) * weight

    alpha = numerator.div(observed_weight.where(observed_weight > 0))
    evidence_quality = quality_budget / total_weight
    required_ok = pd.Series(True, index=index, dtype=bool)
    for factor in required_factors:
        if factor not in frame:
            required_ok &= False
            continue
        required_ok &= _float_series(frame[factor]).reindex(index).notna()
    return FactorScore(
        alpha_score=alpha,
        evidence_quality=evidence_quality.clip(0.0, 1.0),
        observed_weight=(observed_weight / total_weight).clip(0.0, 1.0),
        required_ok=required_ok,
    )


def rank_ic(factor, forward_return, *, minimum_pairs: int = 5) -> float | None:
    """Spearman rank IC using only explicit factor/return pairs."""
    left = _float_series(factor)
    right = _float_series(forward_return).reindex(left.index)
    paired = pd.concat([left.rename("factor"), right.rename("forward")], axis=1).dropna()
    if len(paired) < int(minimum_pairs):
        return None
    value = paired["factor"].corr(paired["forward"], method="spearman")
    return float(value) if value is not None and math.isfinite(float(value)) else None


def icir(values: Sequence[float | None], *, minimum_periods: int = 3) -> float | None:
    """Information coefficient information ratio with sample standard deviation."""
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(clean) < int(minimum_periods):
        return None
    std = float(np.std(clean, ddof=1))
    if not math.isfinite(std) or std <= 0:
        return None
    return float(np.mean(clean) / std)


def neutralize_by_group(values, groups, *, minimum_group_size: int = 2) -> pd.Series:
    """Demean within an explicit group while preserving unknown groups/values."""
    series = _float_series(values)
    group_series = pd.Series(groups).reindex(series.index)
    result = pd.Series(np.nan, index=series.index, dtype="float64")
    for group, positions in group_series.dropna().groupby(group_series.dropna()).groups.items():
        del group  # group identity is intentionally not interpreted here.
        observed = series.loc[positions].dropna()
        if len(observed) < int(minimum_group_size):
            continue
        result.loc[observed.index] = observed - observed.mean()
    return result


def chronological_oos_split(index: Sequence, *, holdout_fraction: float = 0.40):
    """Return deterministic chronological train/holdout labels.

    The function never shuffles.  It is deliberately tiny so research callers
    cannot accidentally turn a chronological validation into random CV.
    """
    labels = list(index)
    if not labels:
        return [], []
    fraction = min(max(float(holdout_fraction), 0.05), 0.95)
    holdout = max(1, int(math.ceil(len(labels) * fraction)))
    split = max(0, len(labels) - holdout)
    return labels[:split], labels[split:]
