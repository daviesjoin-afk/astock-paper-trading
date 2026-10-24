# -*- coding: utf-8 -*-
"""账本层共用的时间/JSON 小工具（stdlib only）。

这些函数此前在 5 个模块里各有一份**逐字相同**的副本（``paper_trading``、
``paper_risk_service``、``paper_risk_evidence``、``paper_lot_ledger``、
``paper_decision_audit``）。副本本身没有问题，问题是任何一次口径调整都得记得改
五处——漏一处就是两个模块对同一段时间给出不同解释。

**只收进"完全相同"的实现。** 语义不同的同名函数刻意留在原处（并在原处注明差异），
例如 ``paper_position_read_model._date`` 解析失败时静默回退到今天、
``paper_replacement_decision._num`` 显式拒绝 bool。把它们合并会悄悄改变行为。

本模块**不 import 任何项目内模块**，因此可以被任何层级引用而不会形成环。
"""
from __future__ import annotations

import datetime as dt
import json


def now_text() -> str:
    """当前本地时间，固定 ``YYYY-MM-DD HH:MM:SS``（账本所有时间列的口径）。"""
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def as_date(value=None):
    """把 ``None`` / datetime / date / ISO 串规整成 ``date``。

    ``None`` 取今天；无法解析时抛 ``ValueError``——**不静默回退**。调用方若
    需要"解析失败就用今天"的宽松语义，必须自己 catch，不能依赖这里。
    """
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def dump_json(value) -> str:
    """账本 JSON 列的序列化口径：不转义中文，未知类型降级为 ``str``。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def load_json(value, default=None):
    """账本 JSON 列的反序列化口径：空值/损坏一律回落到 ``default``（默认 ``{}``）。"""
    fallback = default if default is not None else {}
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback
