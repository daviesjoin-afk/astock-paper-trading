# -*- coding: utf-8 -*-
"""把**已核验的业绩公告事实**作为选股结果的**参考依据**附在读取路径上。

这个模块存在的唯一目的，是把"公告是依据之一"这条边界变成可执行的东西：

```text
允许：把该股票当时已知的、已核验的公告事实，作为参考字段附在选股结果旁边
禁止：据此筛选股票、改变排序、剔除候选、或构成任何否决/触发
```

三条刻意的性质：

1. **零决策**。本模块只读、只组装展示数据。它不改 ``picks``、不改排序、不改任何门禁
   —— ``test_selection_disclosure_reference`` 用"接与不接，选出的股票必须逐字段一致"
   来机械证明这一点。
2. **PIT 按选股业务日**。参考的是"那个交易日系统**当时**已经观测到"的公告，而不是
   "现在库里有的"。晚到的公告不会倒填进历史候选。
3. **降级而不是抛错**。公告库还没建、路径不可读时返回
   ``available=False`` 与原因 —— 选股读取不该因为一个旁路数据库而失败；但也绝不假装
   "没有公告"（那会把"没采到"和"没有"混成一件事）。

**方向不进判断。** 正向（预增/扭亏）与负向（预亏/预减/修正下修）在这里得到完全相同的
待遇：都只是参考事实。是否需要因此调整候选，是研究层（AI 阅读证据并给出支持/反驳/反证）
与既有固定规则的职责，不是本模块的。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_store as STORE

__all__ = (
    "references_for_codes",
)


def _number(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _fact(projection) -> dict:
    """一条公告事实的**参考**形状（不含任何"该不该买"的暗示）。"""
    return {
        "identity": projection.identity,
        "kind": projection.kind,
        "report_period": projection.report_period,
        "is_correction": projection.is_correction,
        "title": projection.title,
        "verification_status": projection.verification_status,
        "is_owner_verified": projection.is_owner_verified,
        "superseded_by": projection.superseded_by,
        "first_seen_at": projection.first_seen_at,
        "availability_day": projection.availability_day,
        "net_profit_wan": {
            "low": _number(projection.low_wan),
            "high": _number(projection.high_wan),
            "unit": projection.unit,
        },
        "yoy_pct": {
            "low": _number(projection.yoy_low_pct),
            "high": _number(projection.yoy_high_pct),
        },
    }


def references_for_codes(
    codes: Iterable[Any],
    *,
    as_of: Any,
    path: Any = None,
    limit: int = LEDGER.EARNINGS_EVENT_READ_LIMIT,
) -> dict:
    """返回 ``{code: [参考事实, ...]}`` 以及这次读取是否可用。

    只读、按 ``as_of`` 做 PIT 过滤、异常降级为 ``available=False``。
    """
    wanted = {str(code).strip() for code in codes or () if str(code).strip()}
    try:
        with STORE.connect_readonly(path) as conn:
            projections = LEDGER.earnings_event_projections(
                conn, as_of=as_of, limit=limit,
            )
    except LEDGER.EarningsLedgerError as exc:
        return {"available": False, "reason": exc.reason, "as_of": as_of, "by_code": {}}

    by_code: dict[str, list[dict]] = {}
    for projection in projections:
        if projection.code not in wanted:
            continue
        by_code.setdefault(projection.code, []).append(_fact(projection))
    return {"available": True, "reason": None, "as_of": as_of, "by_code": by_code}
