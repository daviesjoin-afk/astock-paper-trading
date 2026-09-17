# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow Validation —— 仓位层的**只读**观察叠加。

本模块是 :mod:`tradability_shadow` 之上的一个**附加观察维度**，回答的**唯一**
问题是：

    在 ``decision_session`` 卖出某只股票时，**真实持仓层面**的 A 股 T+1 限制
    是怎么说的？它与市场层面的结论叠加后，观察到的可执行性是什么？

──────────────────────── 铁律 ────────────────────────

**零 authority。** 本模块与本层新增的一切：

* 不改写已经跑出来的市场层面 Shadow 结果（它们是历史观察，不是可重算的缓存）；
* 不改动任何订单 / 成交 / 持仓 / 现金；
* 不改动 selection / risk / learning 的任何决策；
* 不开 archive 的 execution authority；
* 不自动调整任何交易参数。

**BUY 完全不受影响。** 仓位层只在卖出方向有意义。BUY 的
:class:`PositionShadowComparison` 不携带任何仓位字段，其 status 与市场层面
逐字段相同（测试里有等值断言）。

**不可比不进分母。** 仓位证据缺失 / 不可证明时进 ``position_not_comparable``，
它在任何 agreement / disagreement 分母里都不出现 —— "archive 没有 listing"、
"lot 不完整"、"fill 未验证" 这些**是证据缺口，不是规则分歧**。

──────────────────────── 分层（刻意不合并） ────────────────────────

::

    market tradability      ← backend/selection_tradability（市场事实）
    + position sellability  ← tradability_position_evidence（账户/持仓约束）
    = position-aware shadow observation

T+1 **不是**市场事实：同一天同一只股票，昨天买入的账户可以卖、今天买入的账户
不能卖。因此本层**绝不**把仓位信息塞进 ``tradability_archive.tradability_at()``
—— 归档描述的必须是市场事实，混进账户持仓会让它不再是市场事实。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import selection_tradability as ST
    import tradability_position_evidence as PE
    import tradability_shadow as TS
except ImportError:  # pragma: no cover - package-style import
    from . import selection_tradability as ST
    from . import tradability_position_evidence as PE
    from . import tradability_shadow as TS


POSITION_SHADOW_VERSION = "position-aware-shadow-v1"
POSITION_SHADOW_FINGERPRINT_VERSION = "sha256-canonical-position-shadow-v1"


class PositionShadowStatus:
    """仓位层的观察结论（**不是**生产判定，也**不是**放行）。"""

    #: 仓位证据可证明，且 T+1 这一维不阻断 → 该 lot 份额当日可卖。
    COMPARABLE_T1_PASS = "position_comparable_t1_pass"
    #: 仓位证据可证明，但 T+1 锁住了（部分或全部）请求卖出的份额。
    COMPARABLE_T1_BLOCKED = "position_comparable_t1_blocked"
    #: 仓位证据不能证明 → **不可比**，不进任何 denominator。
    NOT_COMPARABLE = "position_not_comparable"


POSITION_SHADOW_STATUSES = (
    PositionShadowStatus.COMPARABLE_T1_PASS,
    PositionShadowStatus.COMPARABLE_T1_BLOCKED,
    PositionShadowStatus.NOT_COMPARABLE,
)

COMPARABLE_POSITION_STATUSES = (
    PositionShadowStatus.COMPARABLE_T1_PASS,
    PositionShadowStatus.COMPARABLE_T1_BLOCKED,
)


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sha256(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PositionShadowComparison:
    """一条仓位层观察。

    市场层面的字段原样引用既有比对结果，**不重算**：本层只负责叠加。
    """

    code: str
    session: str
    side: str
    decision_at: Optional[str]
    validation_as_of: Optional[str]

    market_status: Optional[str]
    market_production_allowed: Optional[bool]

    position_status: str
    position_evidence_status: Optional[str]
    position_sellability_status: Optional[str]
    position_comparable: bool

    account_id: Optional[str]
    held_quantity: int
    sellable_quantity: int
    t1_locked_quantity: int
    unknown_quantity: int
    requested_sell_quantity: Optional[int]

    acquisition_sessions: tuple = ()
    lot_diagnostics: tuple = ()
    lot_evidence: tuple = ()
    evidence_fingerprint: Optional[str] = None
    quantity_basis: Optional[str] = None
    t1_authority_version: Optional[str] = None

    #: 生产层面的 reason（市场 verdict 给出的原话，不翻译）。
    production_reason: Optional[str] = None
    #: 仓位层的诊断（人类可读的一句话，**不**参与机器判定）。
    position_diagnostic: Optional[str] = None

    def identity(self) -> tuple:
        """观察身份。仓位证据的指纹**不在**身份里。

        身份必须稳定：同一 ``(code, session, side, decision_at, validation_as_of)``
        在不同次运行里必须落到同一行。仓位证据变了是**内容**变了，由
        :meth:`content` / :meth:`fingerprint` 负责暴露（同身份 + 冲突内容 → 由调用方
        fail closed，绝不 last-write-wins）。
        """
        return (
            self.code,
            self.session,
            self.side,
            self.decision_at,
            self.validation_as_of,
        )

    def content(self) -> dict:
        """影响结论的全部内容（含仓位上下文身份）。"""
        return {
            "version": POSITION_SHADOW_VERSION,
            "market_status": self.market_status,
            "market_production_allowed": self.market_production_allowed,
            "position_status": self.position_status,
            "position_evidence_status": self.position_evidence_status,
            "position_sellability_status": self.position_sellability_status,
            "position_comparable": self.position_comparable,
            "account_id": self.account_id,
            "held_quantity": self.held_quantity,
            "sellable_quantity": self.sellable_quantity,
            "t1_locked_quantity": self.t1_locked_quantity,
            "unknown_quantity": self.unknown_quantity,
            "requested_sell_quantity": self.requested_sell_quantity,
            "acquisition_sessions": list(self.acquisition_sessions),
            "lot_diagnostics": list(self.lot_diagnostics),
            "evidence_fingerprint": self.evidence_fingerprint,
            "quantity_basis": self.quantity_basis,
            "t1_authority_version": self.t1_authority_version,
            "production_reason": self.production_reason,
        }

    def fingerprint(self) -> str:
        return _sha256({
            "version": POSITION_SHADOW_FINGERPRINT_VERSION,
            "identity": list(self.identity()),
            "content": self.content(),
        })

    @property
    def comparable(self) -> bool:
        """仓位层是否可比（不可比的观察不进任何 denominator）。"""
        return self.position_status in COMPARABLE_POSITION_STATUSES

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "session": self.session,
            "side": self.side,
            "decision_at": self.decision_at,
            "validation_as_of": self.validation_as_of,
            "market_status": self.market_status,
            "market_production_allowed": self.market_production_allowed,
            "position_status": self.position_status,
            "position_evidence_status": self.position_evidence_status,
            "position_sellability_status": self.position_sellability_status,
            "position_comparable": self.position_comparable,
            "account_id": self.account_id,
            "held_quantity": self.held_quantity,
            "sellable_quantity": self.sellable_quantity,
            "t1_locked_quantity": self.t1_locked_quantity,
            "unknown_quantity": self.unknown_quantity,
            "requested_sell_quantity": self.requested_sell_quantity,
            "acquisition_sessions": list(self.acquisition_sessions),
            "lot_diagnostics": list(self.lot_diagnostics),
            "position_evidence": [dict(lot) for lot in self.lot_evidence],
            "evidence_fingerprint": self.evidence_fingerprint,
            "quantity_basis": self.quantity_basis,
            "t1_authority_version": self.t1_authority_version,
            "production_reason": self.production_reason,
            "position_diagnostic": self.position_diagnostic,
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True, slots=True)
class PositionShadowSummary:
    """仓位层的汇总。**每个比率都带自己的分母**（绝不只报百分比）。"""

    requested: int = 0
    sell_comparisons: int = 0
    buy_comparisons: int = 0

    position_comparable: int = 0
    position_not_comparable: int = 0

    position_evidence_proven: int = 0
    position_evidence_partial: int = 0
    position_evidence_unknown: int = 0
    position_evidence_unprovable: int = 0
    position_evidence_invalid: int = 0

    t1_pass: int = 0
    t1_blocked: int = 0

    requested_sell_quantity: int = 0
    proven_sellable_quantity: int = 0
    t1_locked_quantity: int = 0
    unknown_quantity: int = 0

    by_side: Mapping[str, int] = field(default_factory=dict)
    by_position_status: Mapping[str, int] = field(default_factory=dict)
    by_evidence_status: Mapping[str, int] = field(default_factory=dict)
    by_market_status: Mapping[str, int] = field(default_factory=dict)

    @property
    def position_comparison_rate(self) -> Optional[float]:
        """仓位证据可比的比率；分母为 0 → ``None``（**绝不**默认 0 / 1）。"""
        if not self.sell_comparisons:
            return None
        return self.position_comparable / self.sell_comparisons

    def to_dict(self) -> dict:
        return {
            "requested": self.requested,
            "sell_comparisons": self.sell_comparisons,
            "buy_comparisons": self.buy_comparisons,
            "position_comparable": self.position_comparable,
            "position_not_comparable": self.position_not_comparable,
            "position_evidence_proven": self.position_evidence_proven,
            "position_evidence_partial": self.position_evidence_partial,
            "position_evidence_unknown": self.position_evidence_unknown,
            "position_evidence_unprovable": self.position_evidence_unprovable,
            "position_evidence_invalid": self.position_evidence_invalid,
            "t1_pass": self.t1_pass,
            "t1_blocked": self.t1_blocked,
            "requested_sell_quantity": self.requested_sell_quantity,
            "proven_sellable_quantity": self.proven_sellable_quantity,
            "t1_locked_quantity": self.t1_locked_quantity,
            "unknown_quantity": self.unknown_quantity,
            "position_comparison_rate": self.position_comparison_rate,
            "by_side": dict(self.by_side),
            "by_position_status": dict(self.by_position_status),
            "by_evidence_status": dict(self.by_evidence_status),
            "by_market_status": dict(self.by_market_status),
        }


class PositionShadowObserver:
    """把仓位证据叠加到既有市场层面 Shadow 结果之上（只读）。"""

    def __init__(self, comparator: Optional[TS.ShadowComparator],
                 adapter: PE.PositionEvidenceAdapter, *,
                 account_id: Optional[str] = None):
        """``comparator=None`` = 调用方**每次都会**把算好的市场层面结果传进来。

        这条路径是给"市场层面已经在别处算过、本层只做叠加"的调用方用的（操作员 CLI
        就是如此）。此时若有人忘了传 ``market_comparison``，本层**报错**而不是现造
        一个市场层面结论 —— 现造就等于把市场层面的口径在这儿重算了一遍。
        """
        self._comparator = comparator
        self._adapter = adapter
        self._account_id = account_id

    def observe(self, *, code: Any, session: Any, side: Any,
                production_verdict: Any, decision_at: Any = None,
                validation_as_of: Any = None,
                requested_sell_quantity: Any = None,
                market_comparison: Optional[TS.ShadowComparison] = None,
                market_kwargs: Optional[Mapping[str, Any]] = None
                ) -> PositionShadowComparison:
        """观察一条动作。

        ``market_comparison`` 给出时直接复用（调用方已经算过）；否则用
        :meth:`TS.ShadowComparator.compare` 现算一条。**市场层面的东西一律不重算
        成别的口径** —— 本层只是叠加。
        """
        code_text = _text(code) or ""
        session_text = str(session or "")[:10]
        side_text = str(side or "")

        if market_comparison is None:
            if self._comparator is None:
                raise ValueError(
                    "本 observer 未持有市场层面 comparator：调用方必须传入 "
                    "market_comparison（本层绝不自行重算市场层面结论）"
                )
            kwargs = dict(market_kwargs or {})
            # ``decision_at`` 既可以由本方法的参数给出，也可以随 ``market_kwargs``
            # 一起传进来 —— 两者只允许落成一个值，绝不重复下发（那会直接 TypeError）。
            market_decision_at = kwargs.pop("decision_at", None)
            if decision_at is None:
                decision_at = market_decision_at
            market_comparison = self._comparator.compare(
                production_verdict,
                code=code_text,
                session=session_text,
                side=side_text,
                decision_at=(decision_at if decision_at is not None
                             else ST.session_close_at(session_text)),
                **kwargs,
            )

        market_status = _text(getattr(market_comparison, "status", None))
        raw_allowed = getattr(market_comparison, "production_allowed", None)
        market_allowed = None if raw_allowed is None else bool(raw_allowed)
        production_reason = _text(getattr(market_comparison, "production_reason", None))

        # ── BUY：仓位层不参与，字段一律为空，status 与市场层面逐字相同 ──
        if side_text != ST.SIDE_SELL:
            return PositionShadowComparison(
                code=code_text, session=session_text, side=side_text,
                decision_at=decision_at, validation_as_of=validation_as_of,
                market_status=market_status,
                market_production_allowed=market_allowed,
                position_status=market_status or PositionShadowStatus.NOT_COMPARABLE,
                position_evidence_status=None,
                position_sellability_status=None,
                position_comparable=False,
                account_id=None,
                held_quantity=0, sellable_quantity=0,
                t1_locked_quantity=0, unknown_quantity=0,
                requested_sell_quantity=None,
                production_reason=production_reason,
                position_diagnostic="buy_unaffected_by_position_layer",
            )

        context = self._adapter.context_for(
            code_text,
            account_id=self._account_id,
            decision_session=session_text,
            validation_as_of=validation_as_of,
            requested_sell_quantity=requested_sell_quantity,
        )
        comparable = context.comparable
        if not comparable:
            status = PositionShadowStatus.NOT_COMPARABLE
        elif context.sellability_status == PE.SellabilityStatus.T1_SELLABLE:
            status = PositionShadowStatus.COMPARABLE_T1_PASS
        else:
            status = PositionShadowStatus.COMPARABLE_T1_BLOCKED

        lot_diagnostics: list = []
        for lot in context.lots:
            lot_diagnostics.extend(lot.diagnostics)

        return PositionShadowComparison(
            code=code_text, session=session_text, side=side_text,
            decision_at=context.decision_at,
            validation_as_of=context.validation_as_of,
            market_status=market_status,
            market_production_allowed=market_allowed,
            position_status=status,
            position_evidence_status=context.evidence_status,
            position_sellability_status=context.sellability_status,
            position_comparable=comparable,
            account_id=context.account_id,
            held_quantity=context.held_quantity,
            sellable_quantity=context.sellable_quantity,
            t1_locked_quantity=context.t1_locked_quantity,
            unknown_quantity=context.unknown_quantity,
            requested_sell_quantity=context.requested_sell_quantity,
            acquisition_sessions=context.acquisition_sessions,
            lot_diagnostics=tuple(sorted(set(lot_diagnostics)
                                          | set(context.diagnostics))),
            lot_evidence=tuple(lot.to_dict() for lot in context.lots),
            evidence_fingerprint=context.evidence_fingerprint,
            quantity_basis=context.quantity_basis,
            t1_authority_version=context.t1_authority_version,
            production_reason=production_reason,
            position_diagnostic=None,
        )

    def observe_many(self, items: Sequence[Mapping[str, Any]]
                     ) -> list:
        """``observe`` 的批量入口；逐项独立，任一项失败都不得影响其它项。"""
        out = []
        for item in items:
            out.append(self.observe(**dict(item)))
        return out

    def summarize(self, comparisons: Sequence[PositionShadowComparison]
                  ) -> PositionShadowSummary:
        """汇总。**BUY 与不可比项都不进卖出的分母。**"""
        requested = len(comparisons)
        sell = [c for c in comparisons if c.side == ST.SIDE_SELL]
        buy = [c for c in comparisons if c.side != ST.SIDE_SELL]

        by_side: dict = {}
        by_position_status: dict = {}
        by_evidence_status: dict = {}
        by_market_status: dict = {}
        evidence_counts = {
            PE.PositionEvidenceStatus.PROVEN: 0,
            PE.PositionEvidenceStatus.PARTIAL: 0,
            PE.PositionEvidenceStatus.UNKNOWN: 0,
            PE.PositionEvidenceStatus.UNPROVABLE: 0,
            PE.PositionEvidenceStatus.INVALID: 0,
        }
        t1_pass = t1_blocked = comparable = not_comparable = 0
        req_qty = sellable_qty = locked_qty = unknown_qty = 0

        for item in comparisons:
            by_side[item.side] = by_side.get(item.side, 0) + 1
            by_position_status[item.position_status] = (
                by_position_status.get(item.position_status, 0) + 1
            )
            market_key = item.market_status or "unknown"
            by_market_status[market_key] = by_market_status.get(market_key, 0) + 1
            if item.position_evidence_status:
                key = item.position_evidence_status
                by_evidence_status[key] = by_evidence_status.get(key, 0) + 1
                if key in evidence_counts:
                    evidence_counts[key] += 1
            if item.side != ST.SIDE_SELL:
                continue
            if item.comparable:
                comparable += 1
                if item.position_status == PositionShadowStatus.COMPARABLE_T1_PASS:
                    t1_pass += 1
                else:
                    t1_blocked += 1
            else:
                not_comparable += 1
            if item.requested_sell_quantity is not None:
                req_qty += item.requested_sell_quantity
            if item.comparable:
                sellable_qty += item.sellable_quantity
                locked_qty += item.t1_locked_quantity
                unknown_qty += item.unknown_quantity

        return PositionShadowSummary(
            requested=requested,
            sell_comparisons=len(sell),
            buy_comparisons=len(buy),
            position_comparable=comparable,
            position_not_comparable=not_comparable,
            position_evidence_proven=evidence_counts[PE.PositionEvidenceStatus.PROVEN],
            position_evidence_partial=evidence_counts[PE.PositionEvidenceStatus.PARTIAL],
            position_evidence_unknown=evidence_counts[PE.PositionEvidenceStatus.UNKNOWN],
            position_evidence_unprovable=evidence_counts[PE.PositionEvidenceStatus.UNPROVABLE],
            position_evidence_invalid=evidence_counts[PE.PositionEvidenceStatus.INVALID],
            t1_pass=t1_pass,
            t1_blocked=t1_blocked,
            requested_sell_quantity=req_qty,
            proven_sellable_quantity=sellable_qty,
            t1_locked_quantity=locked_qty,
            unknown_quantity=unknown_qty,
            by_side=by_side,
            by_position_status=by_position_status,
            by_evidence_status=by_evidence_status,
            by_market_status=by_market_status,
        )


__all__ = [
    "POSITION_SHADOW_VERSION",
    "POSITION_SHADOW_FINGERPRINT_VERSION",
    "PositionShadowStatus",
    "POSITION_SHADOW_STATUSES",
    "COMPARABLE_POSITION_STATUSES",
    "PositionShadowComparison",
    "PositionShadowSummary",
    "PositionShadowObserver",
]
