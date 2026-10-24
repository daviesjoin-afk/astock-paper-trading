# -*- coding: utf-8 -*-
"""业绩公告事件的**只读**查询 API。

它是"系统到底看到了哪些业绩公告、核验结论是什么"的展示入口，**不是**研究或交易的
证据来源 —— 需要进研究链路的消费方必须走 ``ai_research_earnings_adapter`` 的
typed 证据路径，而不是读这里返回的 dict。

两条刻意的行为：

* 库还不存在时返回 ``available=false`` 而不是 500：页面在系统首次采集之前就应该能开。
* **不在读路径上建库**：``earnings_disclosure_store.connect_readonly`` 在文件缺失时直接
  拒绝，因此一个 GET 请求永远不会让"从未采集过"变成"采集过但为空"。
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query

import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_store as STORE

router = APIRouter(prefix="/api/earnings-disclosure", tags=["earnings-disclosure"])

TZ = LEDGER.TZ


def _today() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def _event_row(projection) -> dict:
    return {
        "identity": projection.identity,
        "announcement_id": projection.announcement_id,
        "code": projection.code,
        "kind": projection.kind,
        "report_period": projection.report_period,
        "is_correction": projection.is_correction,
        "title": projection.title,
        "verification_status": projection.verification_status,
        "is_owner_verified": projection.is_owner_verified,
        "superseded_by": projection.superseded_by,
        "first_seen_at": projection.first_seen_at,
        "availability_day": projection.availability_day,
        "published_at": projection.published_at,
        "net_profit_wan": {
            "low": None if projection.low_wan is None else str(projection.low_wan),
            "high": None if projection.high_wan is None else str(projection.high_wan),
            "unit": projection.unit,
        },
        "yoy_pct": {
            "low": None if projection.yoy_low_pct is None else str(projection.yoy_low_pct),
            "high": None if projection.yoy_high_pct is None else str(projection.yoy_high_pct),
        },
        "latest_claim": None if projection.latest_claim is None else {
            "status": projection.latest_claim.status,
            "disagreements": list(projection.latest_claim.disagreements),
            "checked_at": projection.latest_claim.checked_at,
        },
    }


@router.get("/events")
def events(
    as_of: str = Query("", description="业务日 YYYY-MM-DD；留空表示今天（仅展示用）"),
    limit: int = Query(50, ge=1, le=200),
):
    """到 ``as_of`` 当日为止系统**已经观测到**的业绩公告事件。"""
    day = as_of.strip() or _today()
    try:
        with STORE.connect_readonly() as conn:
            projections = LEDGER.earnings_event_projections(
                conn, as_of=day, limit=limit,
            )
    except LEDGER.EarningsLedgerError as exc:
        # 从未采集过 / 库不可读：展示层要能开，所以降级而不是 500。
        return {"available": False, "reason": exc.reason, "as_of": day, "events": []}

    events_payload = [_event_row(item) for item in projections]
    counts: dict[str, int] = {}
    for row in events_payload:
        status = row["verification_status"]
        counts[status] = counts.get(status, 0) + 1
    return {
        "available": True,
        "as_of": day,
        "count": len(events_payload),
        "by_status": counts,
        "events": events_payload,
        "note": (
            "只读展示：owner 复算出的数字与核验结论。研究/交易证据请走 typed "
            "evidence 路径（ai_research_earnings_adapter），不要直接消费本响应。"
        ),
    }
