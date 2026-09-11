# -*- coding: utf-8 -*-
"""因子单位契约：fraction ↔ percentage points。

仓库里存在两种"涨幅"表示，相差 100 倍：

**fraction**（本仓库收益率/动量因子的规范单位）
``mom5`` / ``mom20`` / ``mom60`` / ``rev5`` 以及因子表里的
``mom5_raw`` / ``mom20_raw`` / ``mom60_raw``，都由
``close_now / close_past - 1`` 产生：

    0.02  == +2%
    0.18  == +18%
    -0.03 == -3%

**percentage points**（行情快照字段）
``pct`` / ``main_pct`` 等来自行情源，本身就是百分点：

    2.0  == +2%

任何"把 fraction 字段与百分点字面量直接比较"的写法都会造成 100 倍阈值错误。
最隐蔽的后果是**死分支**：``mom5_raw >= 18`` 永远不成立（真实的 18% 是 0.18），
于是该打分/门禁分量恒为 0，而且不会有任何报错。

本模块只提供**显式**换算与量级判断，绝不隐式修正调用方的单位。
纯函数、仅依赖标准库；不 import pandas / 网络 / 数据库 / 策略模块。
"""
from __future__ import annotations

import math

__all__ = [
    "FRACTION_PER_PCT_POINT",
    "is_finite_number",
    "fraction_to_pct_points",
    "pct_points_to_fraction",
    "is_fraction_like",
]

#: 1 percentage point == 0.01 fraction
FRACTION_PER_PCT_POINT = 0.01


def is_finite_number(value):
    """仅当 ``value`` 是非布尔、有限的实数时返回 True。

    ``bool`` 被显式排除：``True``/``False`` 是 ``int`` 的子类，把它们当作
    阈值使用几乎总是笔误。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def fraction_to_pct_points(value):
    """fraction → percentage points：``0.18 -> 18.0``，``-0.03 -> -3.0``。

    非有限值（``NaN``/``inf``/``None``/非数值）抛 ``ValueError``——
    静默传播 ``NaN`` 会让下游比较全部恒假，正是本模块要消除的失败模式。
    """
    if not is_finite_number(value):
        raise ValueError("fraction 必须是有限数值，收到 %r" % (value,))
    return float(value) / FRACTION_PER_PCT_POINT


def pct_points_to_fraction(value):
    """percentage points → fraction：``18.0 -> 0.18``，``-3.0 -> -0.03``。

    非有限值抛 ``ValueError``（理由同 :func:`fraction_to_pct_points`）。
    """
    if not is_finite_number(value):
        raise ValueError("percentage points 必须是有限数值，收到 %r" % (value,))
    return float(value) * FRACTION_PER_PCT_POINT


def is_fraction_like(value, tolerance=1e-9):
    """``|value| <= 1`` 的量级启发式，供审计/守卫使用。

    这不是生产判定依据：fraction 与百分点在 ``[-1, 1]`` 区间重叠，
    本函数只用来回答"这个数看起来像 fraction 量级吗"，
    典型用途是断言"与 ``mom*_raw`` 直接比较的默认值必须是 fraction 量级"。
    """
    if not is_finite_number(value):
        return False
    return abs(float(value)) <= 1.0 + tolerance
