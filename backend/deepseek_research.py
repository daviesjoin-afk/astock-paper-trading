# -*- coding: utf-8 -*-
"""Structured DeepSeek research tasks for the paper-trading system."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import sqlite3

import ai_research_contract as ARC
import ai_research_execution_adapter as XEA
import ai_research_portfolio_adapter as PFA
import deepseek_advisor as advisor
import execution_evidence as EE
import execution_verification as EV
import market_data_contract as MDC
import market_data_service as MDS
import paper_portfolio_read_model as PPRM
import ai_research_service
import learning_evaluation as LE
import news_learning


TASKS = {
    "pnl_attribution": {"label": "每日盈亏归因", "short": "解释净值、成交、费用与风险暴露的变化"},
    "candidate_challenge": {"label": "进化候选反方审查", "short": "专门寻找样本不足、口径漏洞和过度调参"},
    "incident_triage": {"label": "异常数据事故归因", "short": "把数据质量异常分级并提出最小处置动作"},
    "overfit_watch": {"label": "策略过拟合提示", "short": "监控训练验证差、样本规模与参数版本密度"},
    "event_evidence": {"label": "公告与新闻证据分级", "short": "只审阅带链接事件并区分公告与聚合新闻"},
}
SEVERITIES = ("critical", "high", "medium", "low", "info")
def _paper(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_data_quality(adaptive_conn):
    """上一轮数据质量研究只作为 research context 读取 canonical ledger。

    ``data_quality`` 这个 purpose 的 writer 已经迁到 ``ai_research_runs``（见
    ``deepseek_advisor.run_review``），所以这里改读 canonical 台账。读入口只有一处
    （``deepseek_advisor.latest_data_quality_research``），避免两个读侧各自实现
    "最近一条"而漂移。

    缺少上一轮结果时明确发布 unavailable，不合成空 evidence 或空 report。
    """
    row = advisor.latest_data_quality_research(adaptive_conn)
    if not row:
        return {
            "availability": "unavailable",
            "reason": "canonical_data_quality_research_unavailable",
            "authority": "research_context_only",
            "is_authoritative": False,
        }
    return {
        "status": row["status"],
        "finished_at": row["created_at"],
        "as_of": row["as_of"],
        "hypothesis": row["hypothesis"],
        "report": advisor.research_report_view_from_row(row),
        "authority": "research",
        "is_authoritative": False,
        "source": "canonical_research_ledger",
    }


# ---------------------------------------------------------------------------
# pnl_attribution：canonical typed runtime（R27-B2C-4C）
# ---------------------------------------------------------------------------
#: canonical 事件里的 source 标签。**不是** authority 声明 —— 事实的 owner 由
#: ``evidence_ref.source_type`` 保留（execution / portfolio_research / market_data）。
PNL_EXECUTION_SOURCE = "execution_owner"
PNL_PORTFOLIO_SOURCE = "portfolio_owner"
PNL_MARKET_SOURCE = "market_owner"

#: 不可用原因码。刻意保留字段的语义位置（而不是删掉字段）：把"拿不到"写清楚，
#: 和用 legacy fallback 填一个数字，是同一类错误的两个方向。
PNL_UNAVAILABLE_NO_CONTEXT = "attribution_context_required"
PNL_UNAVAILABLE_NO_MARKET = "market_evidence_unavailable"
PNL_UNAVAILABLE_HISTORICAL_MARKET = "historical_market_evidence_unavailable"
PNL_UNAVAILABLE_OWNER_FACT = "owner_fact_unknown"

#: compatibility 投影里明确标注的"仅展示"权威标签（绝不进 evidence identity）。
PRESENTATION_AUTHORITY = "presentation_only_not_evidence"


@dataclasses.dataclass(frozen=True)
class ResearchAsOfContext:
    """一次 research run 共享的显式 PIT context。

    三个字段都**没有默认值**，因为每一个都是"编排边界必须自己知道的事实"：

    * ``asof_day`` —— research 业务日（canonical ``YYYY-MM-DD``）。这是 §七 的核心修正：
      legacy 路径用 ``max(paper_nav.nav_date)`` 自己推断业务日，等于让**被解释的数据**
      决定**解释的口径**。现在只能由调用方声明，而且必须是一个**可证明的业务日**
      （任意显式声明的业务日，不由 evidence 推断）。post-close 自动任务使用
      ``adaptive_engine._post_close_attribution_request``：调用方传入观测时刻，交易日历只在
      本日历日自身已完成时签发；周末/法定假日与 15:05 前 fail closed，不退到"最近已完成交易日"。
      手动 research / ai_analysis API 则要求请求显式提供业务日与 market instant。
    * 与之配套的归属限制：那条生产路径的 ``targets`` 来自 ``paper_accounts.cycle_id``
      （**当前**绑定），因此它只支撑"当日 post-close"。手动 API 的 targets 也必须由请求显式提供。
      **历史业务日不得借用当前绑定** ——
      账户后来解绑或换周期后，用当前绑定解释历史日就是 current-state leak。这条限制是
      **运行期强制**的：签发口的 ``asof_day`` 恒等于调用方声明的那个本日历日，且该日本身
      必须是已完成交易日，否则返回 ``None``（fail closed），所以"历史 asof + 当前绑定自动发现
      targets"不可达。真要历史归因，必须由调用方**显式给出 targets**（详见
      ``OPEN PREREQUISITE: owner-provable historical cycle membership``）。
    * ``market_now`` —— R24 freshness 判定用的显式时刻（必须 timezone-aware）。
      ``market_data_service._resolve_now`` 只接受 ``datetime``，字符串会被它拒绝 ——
      所以这里不能直接透传 ``advisor._now()`` 的 ISO 字符串。
    * ``targets`` —— ``((account_id, cycle_id), ...)``；空 tuple 表示本次没有组合上下文，
      portfolio-dependent collector 必须 fail closed，其它 owner collector 可以继续。cycle 归属必须显式给出：
      portfolio owner 的 ``_cycle_initial`` 要求 ``paper_accounts.cycle_id`` 与
      context 的 cycle_id 一致，如果这里不给，owner 只能去猜"当前周期"。

    刻意**没有**任何 fallback：不取 active account、不取 current cycle、不取
    ``today()``、不取 latest。缺 context 时调用方应当拿到失败，而不是一个看起来
    正常的历史归因。
    """

    asof_day: str
    market_now: dt.datetime
    targets: tuple

    def __post_init__(self) -> None:
        day = str(self.asof_day or "").strip()
        try:
            parsed = dt.date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError(
                "AttributionRequest asof_day must be a canonical YYYY-MM-DD business day; "
                f"got {self.asof_day!r}（业务日必须由调用边界显式声明）"
            ) from exc
        if parsed.isoformat() != day:
            raise ValueError(f"AttributionRequest asof_day is not canonical: {self.asof_day!r}")
        object.__setattr__(self, "asof_day", day)

        if not isinstance(self.market_now, dt.datetime):
            raise ValueError(
                "AttributionRequest market_now must be an explicit datetime "
                "(market_data_service rejects strings/None)"
            )
        if self.market_now.tzinfo is None or self.market_now.tzinfo.utcoffset(self.market_now) is None:
            raise ValueError("AttributionRequest market_now must be timezone-aware")

        if self.targets is None:
            raise ValueError("AttributionRequest targets must be explicitly supplied")
        targets = tuple((str(account).strip(), int(cycle)) for account, cycle in self.targets)
        for account, cycle in targets:
            if not account:
                raise ValueError("AttributionRequest target account_id must be non-empty")
            if cycle <= 0:
                raise ValueError(f"AttributionRequest target cycle_id must be positive, got {cycle}")
        object.__setattr__(self, "targets", targets)


# 既有 pnl caller 的兼容名称；新 production paths 共用同一个 context contract。
AttributionRequest = ResearchAsOfContext


def _market_leg(attribution):
    """读 R24 owner 的 attributed market slice —— **只读**，绝不触发 provider refresh。

    返回 ``(valuations, market_evidence_ref, unavailable_reason)``。valuations 由本函数
    **从 ``reading.snapshot.rows`` 内部构造**：组合层刻意不接受调用方给的裸
    ``Mapping``，否则 ``{"600000": 10.0}`` 就能冒充 canonical valuation evidence。

    历史归因请求 D-1 而缓存是 D 时，R24 的 ``classify`` 会给出
    ``unavailable / asof_unprovable``（§23）—— 这里如实把 market leg 判为不可用，
    既不拿 D 回填 D-1，也不回头读 ``paper_nav``。
    """
    reading = MDS.read_snapshot(
        MDC.ATTRIBUTION_POLICY,
        now=attribution.market_now,
        asof_day=attribution.asof_day,
    )
    snapshot = reading.snapshot
    if reading.availability != MDC.AVAILABILITY_AVAILABLE or snapshot is None:
        return None, None, str(reading.reason or reading.status or "market_unavailable"), reading
    valuations = {}
    for row in snapshot.rows:
        if not hasattr(row, "get"):
            continue
        code = str(row.get("code") or "").strip()
        price = row.get("price")
        if not code or isinstance(price, bool) or not isinstance(price, (int, float)):
            continue
        if price > 0 and price == price and price not in (float("inf"), float("-inf")):
            valuations[code] = float(price)
    if not valuations:
        return None, None, "market_slice_has_no_usable_valuation", reading
    return valuations, ARC.evidence_ref_from_market_reading(reading), None, reading


def _market_provenance(reading, reason):
    """market leg 的 provenance —— 必须同时发布**新鲜度**与 `verification_method`。

    两个都容易被漏掉，而漏掉任何一个都会把"我知道得多不确定"这件事藏起来：

    * ``verification == "verified"`` 不等于双源核验：full-market snapshot 通常只是
      ``coverage_integrity``。所以这里同时发布 method 与
      :func:`market_data_contract.is_cross_source_verified` 的结论，且**不**把
      coverage_integrity 描述成 cross-source（§22）。
    * ``availability == "available"`` 也不等于"当日新鲜"：R24 对
      ``observed <= requested`` 的读数给出的是 stale-but-available。只发布 availability
      会让一份 24 小时前的切片在组合边界看起来与当日读数无从区分，因此 freshness /
      status / age_seconds / snapshot 自己的 as_of 与 observed_at 必须一起发布。
    """
    if reading is None:
        return {"availability": "unavailable", "reason": reason, "freshness": None,
                "status": None, "age_seconds": None, "as_of": None, "observed_at": None,
                "verification": None, "verification_method": None,
                "cross_source_verified": False}
    snapshot = reading.snapshot
    return {
        "availability": reading.availability,
        "reason": reading.reason or reason,
        "freshness": reading.freshness,
        "status": reading.status,
        "age_seconds": reading.age_seconds,
        "as_of": None if snapshot is None else snapshot.as_of,
        "observed_at": None if snapshot is None else snapshot.observed_at,
        "verification": None if snapshot is None else snapshot.verification,
        "verification_method": None if snapshot is None else snapshot.verification_method,
        "cross_source_verified": bool(MDC.is_cross_source_verified(snapshot)),
    }


def _evidence_field_state(holder) -> str:
    return str(getattr(holder, "state", "") or "")


def _matches_target(projection, account_id, cycle_id, asof_day) -> bool:
    """execution fact 是否属于本次归因的 (account, cycle, business_day)。

    三个 owner 字段必须**全部 known 且全部匹配**。任何一项 unknown 或不符即排除 ——
    绝不用 ``executed_at[:10]`` 之类的 legacy 时间戳猜业务日，也绝不重绑定
    （§11 / §12：owner 的数据缺口必须保持 visible）。
    """
    owner_account = projection.account_id
    owner_cycle = projection.cycle_id
    owner_day = projection.business_day
    if not (owner_account.is_known and owner_cycle.is_known and owner_day.is_known):
        return False
    if str(owner_account.value) != str(account_id):
        return False
    try:
        if int(owner_cycle.value) != int(cycle_id):
            return False
    except (TypeError, ValueError):
        return False
    return str(owner_day.value) == str(asof_day)


def _execution_leg(conn, account_id, cycle_id, attribution):
    """execution owner typed facts → InformationEvent。

    核验判据用的是 ``ResearchEvidenceRef.is_verified``（"owner 的这条结论是否可信"），
    **不是** ``verification["is_verified"]``（"整单是否完整成交"）：``partial`` /
    ``not_executed`` 配合账本证据同样是一条**可信的**研究事实（§13）。
    """
    events, trade_rows = [], []
    fee_total, fees_complete = 0.0, True
    for evidence in EE.load_execution_evidence(conn, account_id=account_id, limit=500):
        projection = EV.fact_projection(evidence)
        if not _matches_target(projection, account_id, cycle_id, attribution.asof_day):
            continue
        ref = XEA.evidence_ref_from_execution_projection(projection)
        filled_qty = projection.filled_qty.maybe()
        fill_price = projection.fill_price.maybe()
        amount = None
        amount_basis = None
        if filled_qty is not None and fill_price is not None:
            # 派生展示值：owner contract 没有 ``amount`` 列，只有在两腿都 owner-known
            # 时才允许派生，并明确标注它不是 owner raw fact（§14）。
            amount = round(float(filled_qty) * float(fill_price), 4)
            amount_basis = "derived_filled_qty_times_fill_price"
        payload = {
            "identity": projection.identity,
            "identity_kind": projection.identity_kind,
            "lifecycle_state": projection.lifecycle_state,
            "fill_verdict": projection.fill_verdict,
            "code": projection.code.maybe(),
            "action": projection.action.maybe(),
            "requested_qty": projection.requested_qty.maybe(),
            "filled_qty": filled_qty,
            "fill_price": fill_price,
            "fees": projection.fees.maybe(),
            "business_day": projection.business_day.maybe(),
            "observed_at": projection.observed_at.maybe(),
            "field_states": {
                name: _evidence_field_state(getattr(projection, name))
                for name in EV.EXECUTION_FACTUAL_FIELDS
            },
            "amount": amount,
            "amount_basis": amount_basis,
            "verified_by": "research_evidence_ref",
        }
        events.append(ARC.InformationEvent(
            as_of=attribution.asof_day, source=PNL_EXECUTION_SOURCE,
            evidence_ref=ref, payload=payload,
        ))
        trade_rows.append(dict(payload, is_verified=bool(ref.is_verified)))
        if projection.fees.is_known:
            fee_total += float(projection.fees.value)
        else:
            # unknown ≠ 0：把没证明的费用补零，就是把"不知道"发布成"没有费用"。
            fees_complete = False
    return events, trade_rows, (round(fee_total, 4) if fees_complete else None), fees_complete


def _portfolio_leg(conn, context, account_id, attribution):
    """portfolio owner typed facts → InformationEvent（cash / realized / position cost）。"""
    events, facts, provenance = [], {}, {}
    for projection in PPRM.accounting_fact_projections(conn, context, account_id=account_id):
        ref = PFA.evidence_ref_from_portfolio_projection(projection)
        verified = projection.status == PPRM.STATUS_VERIFIED
        payload = {
            "fact_kind": projection.fact_kind,
            "status": projection.status,
            "asof_day": projection.asof_day,
            "cycle_id": projection.cycle_id,
            "account_id": projection.account_id,
            "value": None,
        }
        if verified:
            if projection.fact_kind == PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY:
                summary = projection.value
                payload["position_count"] = summary.position_count
                payload["cost_value"] = summary.cost_value
                facts[projection.fact_kind] = {
                    "position_count": summary.position_count,
                    "cost_value": summary.cost_value,
                }
            else:
                payload["value"] = projection.value
                facts[projection.fact_kind] = projection.value
        provenance[projection.fact_kind] = {
            "status": projection.status,
            "source_id": ref.source_id,
            "as_of": ref.as_of,
            "is_verified": bool(ref.is_verified),
            "authority": "portfolio_owner_typed_fact",
        }
        events.append(ARC.InformationEvent(
            as_of=attribution.asof_day, source=PNL_PORTFOLIO_SOURCE,
            evidence_ref=ref, payload=payload,
        ))
    return events, facts, provenance


def _compose_pnl_attribution(conn, attribution):
    """cross-owner research composition。

    本层只做三件被允许的事：消费 owner typed facts、把它们组合成可归因的展示值、
    把"证明不了"如实写成 unavailable + reason。它**不是**第四个事实 owner：
    execution / portfolio / market 各自保留 identity（§31），也不签发新的
    ``ResearchEvidenceRef``（§32）。
    """
    events = []
    valuations, market_ref, market_reason, market_reading = _market_leg(attribution)
    market_provenance = _market_provenance(market_reading, market_reason)
    if market_ref is not None:
        events.append(ARC.InformationEvent(
            as_of=attribution.asof_day, source=PNL_MARKET_SOURCE, evidence_ref=market_ref,
            payload={"policy": MDC.ATTRIBUTION_POLICY.name, "asof_day": attribution.asof_day,
                     "valuation_codes": sorted(valuations),
                     # 新鲜度随证据一起发布：stale-but-available 与当日读数不得在
                     # 消费侧无从区分。
                     "freshness": market_provenance["freshness"],
                     "status": market_provenance["status"],
                     "observed_at": market_provenance["observed_at"],
                     "verification_method": market_provenance["verification_method"]},
        ))

    account_rows, trade_rows = [], []
    realized_total, realized_complete = 0.0, True
    fees_total, fees_complete = 0.0, True
    position_rows = []
    for account_id, cycle_id in attribution.targets:
        context = PPRM.PortfolioReadContext(cycle_id, attribution.asof_day)
        portfolio_events, facts, fact_provenance = _portfolio_leg(
            conn, context, account_id, attribution)
        events.extend(portfolio_events)

        execution_events, trades, fees_value, fees_ok = _execution_leg(
            conn, account_id, cycle_id, attribution)
        events.extend(execution_events)
        trade_rows.extend(trades)
        if fees_ok:
            fees_total += float(fees_value or 0.0)
        else:
            fees_complete = False

        realized = facts.get(PPRM.PORTFOLIO_FACT_REALIZED_PNL)
        if isinstance(realized, (int, float)) and not isinstance(realized, bool):
            realized_total += float(realized)
        else:
            realized_complete = False

        summary = facts.get(PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY)
        if isinstance(summary, dict):
            position_rows.append(dict(summary, account_id=account_id, cycle_id=cycle_id,
                                      authority="portfolio_owner_typed_fact"))
        else:
            position_rows.append({"account_id": account_id, "cycle_id": cycle_id,
                                  "position_count": None, "cost_value": None,
                                  "availability": PNL_UNAVAILABLE_OWNER_FACT,
                                  "authority": "portfolio_owner_typed_fact"})

        nav_block = {"nav": None, "market_value": None, "unrealized_pnl": None,
                     "availability": "unavailable", "reason": market_reason,
                     "market_evidence_ref": None,
                     "market_verification": market_provenance["verification"],
                     "market_verification_method": market_provenance["verification_method"],
                     "market_cross_source_verified": market_provenance["cross_source_verified"],
                     # NAV 的可信度基础必须完整：一份 stale-but-available 的切片不能因为
                     # ``availability == available`` 就被当成当日读数。
                     "market_freshness": market_provenance["freshness"],
                     "market_status": market_provenance["status"],
                     "market_age_seconds": market_provenance["age_seconds"],
                     "market_as_of": market_provenance["as_of"]}
        if valuations is not None:
            view = PPRM.portfolio_for_context(
                conn, context, account_id=account_id, valuations=valuations)
            nav_block.update({
                "nav": view.get("nav"),
                "market_value": view.get("market_value"),
                "unrealized_pnl": view.get("unrealized_pnl"),
                "availability": "available" if view.get("nav") is not None else "unavailable",
                "reason": None if view.get("nav") is not None else PNL_UNAVAILABLE_OWNER_FACT,
                "nav_status": view.get("nav_status"),
                "market_value_status": view.get("market_value_status"),
                "market_evidence_ref": market_ref.source_id if market_ref is not None else None,
            })
            # portfolio 的 nav_status 只说明"ledger 可重建 + 拿到了完整 numeric
            # valuations"，**不是**市场核验（§20）：市场侧 provenance 必须一起发布。
        account_rows.append({
            "account_id": account_id,
            "cycle_id": cycle_id,
            "cash": facts.get(PPRM.PORTFOLIO_FACT_CASH),
            "realized_pnl": realized,
            "position_cost_summary": summary,
            "latest_nav": nav_block,
            "prior_nav": {"value": None, "availability": "unavailable",
                          "reason": PNL_UNAVAILABLE_HISTORICAL_MARKET},
            "daily_pnl": {"value": None, "availability": "unavailable",
                          "reason": PNL_UNAVAILABLE_HISTORICAL_MARKET},
            "daily_return_pct": {"value": None, "availability": "unavailable",
                                 "reason": PNL_UNAVAILABLE_HISTORICAL_MARKET},
            "owner_fact_provenance": fact_provenance,
            "presentation_authority": PRESENTATION_AUTHORITY,
            "is_authoritative": False,
        })

    return {
        "scope": "paper_trading_only",
        "purpose": "pnl_attribution",
        "asof": attribution.asof_day,
        "asof_source": "explicit_attribution_request",
        "targets": [{"account_id": a, "cycle_id": c} for a, c in attribution.targets],
        "accounts": account_rows,
        "filled_trades": trade_rows,
        "fees": round(fees_total, 4) if fees_complete else None,
        "fees_availability": "known" if fees_complete else PNL_UNAVAILABLE_OWNER_FACT,
        "realized_pnl": round(realized_total, 4) if realized_complete else None,
        "realized_pnl_availability": "known" if realized_complete else PNL_UNAVAILABLE_OWNER_FACT,
        "position_cost_summary": position_rows,
        "market": dict(market_provenance, policy=MDC.ATTRIBUTION_POLICY.name),
        #: canonical typed 事件真实进入 runtime（§29）：每一条都携带
        #: ``evidence_ref``，kind / verification 由 ref 派生，payload 只是观察投影。
        "canonical_events": [
            {"source_type": event.evidence_ref.source_type, "source_id": event.evidence_ref.source_id,
             "kind": event.kind, "as_of": event.as_of, "source": event.source,
             "verification": event.verification,
             "verification_method": event.verification_method,
             "evidence_id": event.evidence_id,
             "payload_fields": sorted(event.payload)}
            for event in events
        ],
        "event_count": len(events),
        "limitations": [
            "不能证明前一交易日业务日 / 前一交易日组合状态 / 前一交易日 R24 行情时，"
            "不把累计浮盈伪装成单日归因（prior_nav / daily_pnl / daily_return 报 unavailable）",
            "归因只解释模拟盘账本，不推断实盘收益",
            "market leg 的 verification 必须连同 verification_method 一起读："
            "coverage_integrity 不是双源核验",
        ],
        "authority": "research_composition_only_not_an_owner",
    }


def _pnl_evidence(adaptive_conn, paper_db_path, attribution=None):
    """pnl_attribution 的 canonical typed 入口。

    没有显式 :class:`AttributionRequest` 时**fail closed**（抛出，而不是猜一个业务日）。
    ``adaptive_conn`` 刻意不参与：legacy 路径用它读 ``paper_nav``，现在 attribution
    与 adaptive 库无关 —— 保留参数只为与 ``COLLECTORS`` 其它四个 collector 同形。
    """
    if attribution is None:
        raise ValueError(
            f"{PNL_UNAVAILABLE_NO_CONTEXT}: pnl_attribution requires an explicit "
            "AttributionRequest (asof_day / market_now / targets) —— 拒绝用墙钟或 "
            "max(paper_nav.nav_date) 推断业务日"
        )
    if not isinstance(attribution, AttributionRequest):
        raise TypeError(
            "pnl_attribution attribution context must be an AttributionRequest, "
            f"got {type(attribution).__name__}"
        )
    if not attribution.targets:
        raise ValueError(PNL_UNAVAILABLE_NO_CONTEXT)
    paper = _paper(paper_db_path)
    try:
        return _compose_pnl_attribution(paper, attribution)
    finally:
        paper.close()


def _fact_payload(projection):
    """Expose owner factual fields to the model, keeping verification in the ref."""
    payload = projection.as_dict() if hasattr(projection, "as_dict") else projection.projection()
    return {
        key: value for key, value in payload.items()
        if key not in {
            "fact_verification_status", "owner_verification_status",
            "ledger_verification_status", "authority", "is_authoritative",
            "verification", "verification_method", "is_verified", "outcome",
        }
    }


def _event_from_owner(projection, adapter, context, source):
    ref = adapter(projection)
    if ref.as_of > context.asof_day:
        raise ValueError("owner fact is later than the shared research as-of day")
    return ARC.InformationEvent(
        as_of=context.asof_day, source=source, evidence_ref=ref,
        payload=_fact_payload(projection),
    )


def _pnl_events(paper, context):
    events = []
    valuations, market_ref, _reason, reading = _market_leg(context)
    if market_ref is not None:
        events.append(ARC.InformationEvent(
            as_of=context.asof_day, source=PNL_MARKET_SOURCE,
            evidence_ref=market_ref,
            payload={"policy": MDC.ATTRIBUTION_POLICY.name,
                     "valuation_codes": sorted(valuations or {}),
                     "observed_at": getattr(reading.snapshot, "observed_at", None)},
        ))
    for account_id, cycle_id in context.targets:
        portfolio_context = PPRM.PortfolioReadContext(cycle_id, context.asof_day)
        portfolio, _facts, _provenance = _portfolio_leg(
            paper, portfolio_context, account_id, context,
        )
        execution, _trades, _fees, _complete = _execution_leg(
            paper, account_id, cycle_id, context,
        )
        events.extend(portfolio)
        events.extend(execution)
    return tuple(events)


def _collect_typed_events(purpose, adaptive_conn, paper_db_path, context):
    """Read facts only through their owner and convert them through approved adapters."""
    if not isinstance(context, ResearchAsOfContext):
        raise ValueError("research_asof_context_required")
    if purpose == "pnl_attribution":
        if not context.targets:
            raise ValueError("attribution_context_required")
        paper = _paper(paper_db_path)
        try:
            return _pnl_events(paper, context)
        finally:
            paper.close()

    # Local imports avoid an adaptive_engine ↔ deepseek_research import cycle.
    if purpose == "incident_triage":
        import adaptive_engine as AE
        import paper_trading as PT
        import ai_research_runtime_adapter as RTA
        adaptive_facts = AE.adaptive_run_facts(
            adaptive_conn, as_of=context.asof_day, limit=12,
        )
        paper = _paper(paper_db_path)
        try:
            paper_facts = PT.paper_job_run_facts(
                paper, as_of=context.asof_day, limit=12,
            )
        finally:
            paper.close()
        return tuple(
            _event_from_owner(fact, RTA.evidence_ref_from_runtime_projection,
                              context, "runtime_owner.typed_fact")
            for fact in (*adaptive_facts, *paper_facts)
        )

    if purpose == "candidate_challenge":
        import adaptive_risk as AR
        import adaptive_selection as ASEL
        import ai_research_strategy_adapter as STA
        risk = AR.risk_candidate_facts(adaptive_conn, as_of=context.asof_day, limit=12)
        selection = ASEL.selection_candidate_facts(
            adaptive_conn, as_of=context.asof_day, limit=12,
        )
        evaluations = LE.experiment_evaluation_facts(
            adaptive_conn, as_of=context.asof_day, limit=12,
        )
        return tuple(
            _event_from_owner(fact, STA.evidence_ref_from_strategy_projection,
                              context, "strategy_owner.typed_fact")
            for fact in (*risk, *selection, *evaluations)
        )

    if purpose == "overfit_watch":
        import ai_research_strategy_adapter as STA
        evaluations = LE.experiment_evaluation_facts(
            adaptive_conn, as_of=context.asof_day, limit=40,
        )
        return tuple(
            _event_from_owner(fact, STA.evidence_ref_from_strategy_projection,
                              context, "strategy_owner.typed_fact")
            for fact in evaluations
        )

    if purpose == "event_evidence":
        import ai_research_news_adapter as NEA
        projections = news_learning.news_fact_projections(
            adaptive_conn, as_of=context.asof_day, limit=50,
        )
        return tuple(
            _event_from_owner(fact, NEA.evidence_ref_from_news_projection,
                              context, "news_owner.typed_fact")
            for fact in projections
        )
    raise ValueError("unsupported_advisor_purpose")


def _compatibility_evidence(purpose, events, context):
    """A display-only projection; owner identity and verification remain in typed refs."""
    return {
        "purpose": purpose,
        "as_of": context.asof_day,
        "facts": [
            {"kind": event.kind, "evidence_id": event.evidence_id,
             "as_of": event.as_of, "payload": dict(event.payload)}
            for event in events
        ],
        "authority": "research_composition_only_not_an_owner",
        "limitations": {
            "candidate_challenge": ["risk deployments and order-risk attribution unavailable"],
            "incident_triage": ["killed adaptive runs and unidentifiable job attempts unavailable",
                                "business order rejections are not incident evidence"],
            "overfit_watch": ["adaptive rewards, alpha candidates, parameter versions, and NAV unavailable"],
            "event_evidence": ["news owner facts only; no position-code fallback or live fetch"],
            "pnl_attribution": ["historical cycle membership and historical market gaps remain open"],
        }.get(purpose, []),
    }


def _candidate_evidence(adaptive_conn, paper_db_path, context=None):
    context = context or paper_db_path
    events = _collect_typed_events("candidate_challenge", adaptive_conn, None, context)
    return _compatibility_evidence("candidate_challenge", events, context)


def _incident_evidence(adaptive_conn, paper_db_path, context=None):
    context = context or paper_db_path
    events = _collect_typed_events("incident_triage", adaptive_conn, paper_db_path, context)
    result = _compatibility_evidence("incident_triage", events, context)
    result["latest_data_quality"] = _latest_data_quality(adaptive_conn)
    return result


def _overfit_evidence(adaptive_conn, paper_db_path, context=None):
    context = context or paper_db_path
    events = _collect_typed_events("overfit_watch", adaptive_conn, None, context)
    return _compatibility_evidence("overfit_watch", events, context)


def _event_evidence(adaptive_conn, paper_db_path, context=None):
    context = context or paper_db_path
    events = _collect_typed_events("event_evidence", adaptive_conn, None, context)
    return _compatibility_evidence("event_evidence", events, context)


COLLECTORS = {
    "pnl_attribution": _pnl_evidence,
    "candidate_challenge": _candidate_evidence,
    "incident_triage": _incident_evidence,
    "overfit_watch": _overfit_evidence,
    "event_evidence": _event_evidence,
}

_QUESTIONS = {
    "pnl_attribution": "基于 execution、portfolio 与 market owner typed facts 做有限归因；缺失事实必须保持不可用。",
    "candidate_challenge": "基于 strategy owner facts 做反方审查；不得重算资格、晋升、生命周期或应用门禁。",
    "incident_triage": "基于 runtime owner facts 研究可能事故；不得把业务拒单当事故，也不得推断严重度为 owner 事实。",
    "overfit_watch": "基于 experiment owner evaluation facts 做反方审查；缺失回报或 NAV 不得补零。",
    "event_evidence": "基于 news owner facts 审阅已知事件；published_at 只作描述，不得用于 PIT。",
}


def collect(purpose, adaptive_conn, paper_db_path, *, context=None, attribution=None):
    """Materialize a compatibility display from owner typed facts only."""
    if purpose not in COLLECTORS:
        raise ValueError("unsupported_advisor_purpose")
    context = context or attribution
    if purpose == "pnl_attribution":
        if context is None:
            raise ValueError(PNL_UNAVAILABLE_NO_CONTEXT)
        if not isinstance(context, ResearchAsOfContext):
            raise TypeError("pnl_attribution context must be a ResearchAsOfContext")
        return _pnl_evidence(adaptive_conn, paper_db_path, attribution=context)
    if not isinstance(context, ResearchAsOfContext):
        raise ValueError("research_asof_context_required")
    return COLLECTORS[purpose](adaptive_conn, paper_db_path, context)


def _run_evidence_task(connect_factory, purpose, events, context, trigger="manual", limitations=()):
    """Run one typed evidence set through the canonical provider and append owner."""
    if purpose not in TASKS:
        raise ValueError("unsupported_advisor_purpose")
    if not events:
        return {"id": None, "purpose": purpose, "status": "failed",
                "error_code": "owner_evidence_unavailable", "latency_ms": 0,
                "authority": "canonical_research_ledger_only"}
    try:
        config = advisor._research_provider_config(connect_factory)
        service = ai_research_service.run_research_run(
            connect_factory,
            purpose=purpose,
            trigger=str(trigger or "manual")[:120],
            hypothesis_id=f"{purpose}@{context.asof_day}",
            as_of=context.asof_day,
            subject=purpose,
            question=_QUESTIONS[purpose] + (" 已知限制：" + "；".join(limitations) if limitations else ""),
            events=events,
            provider_config=config,
        )
    except advisor.ResearchReadinessError as exc:
        return {"id": None, "purpose": purpose, "status": "blocked",
                "error_code": f"research_{exc.reason}"[:80], "latency_ms": 0,
                "authority": "canonical_research_ledger_only"}
    except ai_research_service.ResearchServiceError as exc:
        return {"id": None, "purpose": purpose, "status": "failed",
                "error_code": f"{exc.stage}_{exc.reason}"[:80], "latency_ms": 0,
                "authority": "canonical_research_ledger_only"}
    return {
        "id": service.run_id,
        "purpose": purpose,
        "status": "completed",
        "error_code": None,
        "latency_ms": service.latency_ms,
        "report": advisor.research_report_view(service),
        "authority": "canonical_research_ledger",
        "is_authoritative": False,
    }


def run_task(connect_factory, paper_db_path, purpose, trigger="manual", *, context=None,
             attribution=None):
    if purpose not in TASKS:
        raise ValueError("unsupported_advisor_purpose")
    context = context or attribution
    if not isinstance(context, ResearchAsOfContext):
        return {"id": None, "purpose": purpose, "status": "failed",
                "error_code": "research_asof_context_required", "latency_ms": 0}
    try:
        with connect_factory() as conn:
            events = _collect_typed_events(purpose, conn, paper_db_path, context)
    except Exception as exc:
        return {"id": None, "purpose": purpose, "status": "failed",
                "error_code": f"evidence_{type(exc).__name__}"[:80], "latency_ms": 0}
    limitations = _compatibility_evidence(purpose, events, context).get("limitations", ())
    return _run_evidence_task(
        connect_factory, purpose, events, context, trigger, limitations=limitations,
    )


def _collect_suite_snapshot(connect_factory, paper_db_path, context=None, *, attribution=None):
    """Collect all purposes once under one explicit immutable as-of context."""
    context = context or attribution
    if not isinstance(context, ResearchAsOfContext):
        raise ValueError("research_asof_context_required")
    snapshot_asof = context.market_now.isoformat(timespec="seconds")
    snapshot_id = hashlib.sha256(
        json.dumps({"as_of": context.asof_day, "market_now": snapshot_asof,
                    "purposes": list(TASKS)}, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    evidence_by_purpose = {}
    with connect_factory() as conn:
        for purpose in TASKS:
            try:
                evidence_by_purpose[purpose] = _collect_typed_events(
                    purpose, conn, paper_db_path, context,
                )
            except Exception as exc:
                evidence_by_purpose[purpose] = (exc,)
    return snapshot_id, snapshot_asof, evidence_by_purpose


def run_suite(connect_factory, paper_db_path, trigger="post-close", *, context=None,
              attribution=None):
    context = context or attribution
    if not isinstance(context, ResearchAsOfContext):
        return [{"purpose": purpose, "status": "failed",
                 "error_code": "research_asof_context_required"} for purpose in TASKS]
    try:
        snapshot_id, _snapshot_asof, evidence_by_purpose = _collect_suite_snapshot(
            connect_factory, paper_db_path, context,
        )
    except Exception as exc:
        return [{"purpose": purpose, "status": "failed",
                 "error_code": f"evidence_{type(exc).__name__}"[:80]} for purpose in TASKS]
    results = []
    for purpose in TASKS:
        events = evidence_by_purpose[purpose]
        if len(events) == 1 and isinstance(events[0], Exception):
            result = {"id": None, "purpose": purpose, "status": "failed",
                      "error_code": f"evidence_{type(events[0]).__name__}"[:80],
                      "latency_ms": 0}
        else:
            limitations = _compatibility_evidence(purpose, events, context).get("limitations", ())
            result = _run_evidence_task(
                connect_factory, purpose, events, context,
                trigger=f"{trigger}:{purpose}", limitations=limitations,
            )
        result["snapshot_id"] = snapshot_id
        results.append(result)
    return results


def task_catalog():
    return [{"purpose": key, **value} for key, value in TASKS.items()]
