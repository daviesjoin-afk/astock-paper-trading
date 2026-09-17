# -*- coding: utf-8 -*-
"""Shadow Tradability Validation —— 生产判断 vs 历史归档判断的**只读**比对。

本模块回答的**唯一**问题是：

    同一个 ``(code, session, decision_at, side)`` 上，
    生产链路给出的可成交性结论，与 Historical Tradability Archive 的结论，
    是否一致？如果不一致，是因为哪一侧的证据不足？

它**不**回答、也**绝不**执行：

* 应该改订单 / 应该放行 / 应该阻断；
* 应该用哪一侧的结论作为权威。

──────────────────────── 权威边界（零 authority） ────────────────────────

Shadow 是**观察结果**，不是 authority。因此：

1. 生产侧必须复用**生产路径真正在用的**判定实现
   （:func:`selection_tradability.tradability_at`，经由
   ``entry_tradability`` / ``exit_tradability`` 消费），本模块**不复制**任何生产规则；
2. 归档侧必须复用 :func:`tradability_archive.tradability_at`，本模块**不读**
   ``historical_tradability_archive`` 的原始 SQL、也**不知道** ST / 停牌 / 涨跌停
   方向 / quote / volume 该怎么判——那些规则已经有 authority；
3. 本模块不 import 任何下单 / 持仓 / 成交 / 学习模块，也不向它们暴露任何入口。
   ``test_tradability_shadow_architecture_guard`` 用 AST 护栏把这条边界钉死。

──────────────────────── 可比与不可比必须分开 ────────────────────────

归档当前的证据仍有真实缺口（历史上市/退市权威源不足、停牌历史不完整、没有 Level2
封单证据、部分历史 bar 只能给 ``retrieved_at``）。把这些缺口算成"分歧"会让一致率
失去意义。因此本模块先把每条比对分类成 ``comparable`` / ``not_comparable``：

* **comparable**：生产结论有效（``executable`` / ``blocked``）**且**归档在
  ``decision_at`` 当时可证明（有可见证据且事实不是未知）；
* **not_comparable**：其余全部（归档缺证据 / 归档不可证明 / 归档事实未知 /
  生产自己也不确定 / 比对身份非法）。

只有 comparable 进入 agree / disagree 的分母。``comparable == 0`` 时
``agreement_rate`` / ``disagreement_rate`` 为 ``None``——**绝不**默认 0% 或 100%。

──────────────────────── 身份与幂等 ────────────────────────

比对身份：``(code, session, decision_at, side, contract_version)``。同一身份上
内容必须确定：持久化时相同身份 + 相同内容 = no-op；相同身份 + 冲突内容 =
:class:`ShadowConflictError`（fail closed，**禁止** last-write-wins）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # ``backend`` 在 sys.path（生产与 ``cd backend`` 测试）
    import point_in_time as PIT
    import selection_tradability as ST
    import tradability_archive as TA
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT  # type: ignore
    from . import selection_tradability as ST  # type: ignore
    from . import tradability_archive as TA  # type: ignore


CONTRACT_VERSION = "tradability-shadow-v1"
FINGERPRINT_VERSION = "sha256-canonical-shadow-v1"
MIGRATION_DESCRIPTION = "015_add_tradability_shadow_comparisons"
SHADOW_TABLE = "tradability_shadow_comparisons"

#: 查询"该 (code, session) 历史上是否**曾经**被摄取过"用的远端时点。
#: 只用于区分 ``archive_unprovable``（摄取了但当时不可知）与 ``archive_missing``
#: （从未摄取），不参与任何判定。
ARCHIVE_FAR_FUTURE = "9999-12-31T23:59:59+08:00"

#: 归档判定里表示"事实未知/不足"的原因（与 :class:`TA.TradabilityReason` 同源）。
_ARCHIVE_UNKNOWN_REASON = TA.TradabilityReason.UNKNOWN_STATE.value


class ShadowError(ValueError):
    """Shadow 比对无法成立（契约层，不是"数据不够"）。"""


class ShadowConflictError(ShadowError):
    """同一比对身份上出现了**内容冲突**的第二次写入。

    这**不是**幂等重放（内容相同 → no-op），而是"同一个身份被两条不同结论占用"。
    必须 fail closed：silently overwrite 会让审计看到最后写入者，而不是发生过什么。
    """


class ShadowStatus(str, Enum):
    """比对结论的**唯一**词汇表。只有前四项属于 comparable。

    ``agree_allow`` / ``agree_block``
        两侧都允许 / 都阻断。
    ``production_allow_archive_block`` / ``production_block_archive_allow``
        两侧方向相反——这是真正需要人看的**分歧**。
    ``archive_unknown``
        归档有可见证据，但该方向依赖的事实**当时未知**（例如 ST 状态未知）。
    ``archive_unprovable``
        归档里**有**这条 (code, session) 的事实，但它在 ``decision_at`` 当时
        **不可知**（PIT 不成立）——事实存在，历史不可证明。
    ``archive_missing``
        归档里**从未**摄取过这条 (code, session) 的事实。
    ``production_unknown``
        生产侧自己判 ``unproven``（证据不足）。
    ``comparison_invalid``
        比对身份不合法，或生产侧判 ``invalid``。
    """

    AGREE_ALLOW = "agree_allow"
    AGREE_BLOCK = "agree_block"
    PRODUCTION_ALLOW_ARCHIVE_BLOCK = "production_allow_archive_block"
    PRODUCTION_BLOCK_ARCHIVE_ALLOW = "production_block_archive_allow"

    ARCHIVE_UNKNOWN = "archive_unknown"
    ARCHIVE_UNPROVABLE = "archive_unprovable"
    ARCHIVE_MISSING = "archive_missing"
    PRODUCTION_UNKNOWN = "production_unknown"
    COMPARISON_INVALID = "comparison_invalid"


#: 进入 agree / disagree 分母的结论。
COMPARABLE_STATUSES = (
    ShadowStatus.AGREE_ALLOW,
    ShadowStatus.AGREE_BLOCK,
    ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK,
    ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW,
)

#: 不进分母的结论（归档证据缺口 + 生产自身不确定 + 身份非法）。
NOT_COMPARABLE_STATUSES = (
    ShadowStatus.ARCHIVE_UNKNOWN,
    ShadowStatus.ARCHIVE_UNPROVABLE,
    ShadowStatus.ARCHIVE_MISSING,
    ShadowStatus.PRODUCTION_UNKNOWN,
    ShadowStatus.COMPARISON_INVALID,
)

SHADOW_STATUSES = COMPARABLE_STATUSES + NOT_COMPARABLE_STATUSES

AGREE_STATUSES = (ShadowStatus.AGREE_ALLOW, ShadowStatus.AGREE_BLOCK)
DISAGREE_STATUSES = (
    ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK,
    ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW,
)

__all__ = [
    "CONTRACT_VERSION",
    "FINGERPRINT_VERSION",
    "MIGRATION_DESCRIPTION",
    "SHADOW_TABLE",
    "ARCHIVE_FAR_FUTURE",
    "ShadowError",
    "ShadowConflictError",
    "ShadowStatus",
    "COMPARABLE_STATUSES",
    "NOT_COMPARABLE_STATUSES",
    "SHADOW_STATUSES",
    "AGREE_STATUSES",
    "DISAGREE_STATUSES",
    "ShadowComparison",
    "ShadowComparisonSummary",
    "ShadowComparator",
    "ensure_shadow_schema",
    "save_comparison",
    "save_comparisons",
    "load_comparisons",
]


# ─────────────────────────────── 工具 ───────────────────────────────


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_instant(value: Any) -> Optional[str]:
    """任意时点 → 规范字符串；不可解析 → ``None``（**绝不**回退到"现在"）。"""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    moment = PIT.parse_asof(value)
    return None if moment is None else moment.isoformat()


def _canonical_session(value: Any) -> Optional[str]:
    """session 必须是合法日期（``YYYY-MM-DD``）。"""
    text = _text(value)
    if text is None:
        return None
    candidate = text[:10].replace("/", "-")
    try:
        _dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _sha256(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _verdict_field(verdict: Any, name: str, default: Any = None) -> Any:
    """生产判定对象的字段读取：``Mapping`` 与属性对象两种形态都支持。"""
    if isinstance(verdict, Mapping):
        return verdict.get(name, default)
    return getattr(verdict, name, default)


# ─────────────────────────────── 比对结果 ───────────────────────────────


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    """一条只读比对结果。**不是** TradabilityDecision / OrderDecision。

    命名刻意避开 ``*Decision``：这是一个**观察**，任何把它当权威消费的代码都是
    架构违规。它同时保留两侧的 reason，供后续按原因归类分析
    （ST / 停牌 / quote / volume / 涨跌停锁定 / 上市状态）。
    """

    code: str
    session: str
    decision_at: str
    side: str
    status: str
    comparable: bool

    production_allowed: Optional[bool]
    production_reason: str
    production_status: str

    archive_allowed: Optional[bool]
    archive_reason: Optional[str]
    archive_source: Optional[str]
    archive_fingerprint: Optional[str]
    archive_effective_at: Optional[str]
    archive_observed_at: Optional[str]
    archive_evidence_present: bool

    contract_version: str = CONTRACT_VERSION

    @property
    def identity(self) -> tuple:
        """``(code, session, decision_at, side, contract_version)``。"""
        return (self.code, self.session, self.decision_at, self.side, self.contract_version)

    def content(self) -> dict:
        """进入内容指纹的字段。**不含**时间戳等非身份信息。"""
        return {
            "status": self.status,
            "comparable": self.comparable,
            "production_allowed": self.production_allowed,
            "production_reason": self.production_reason,
            "production_status": self.production_status,
            "archive_allowed": self.archive_allowed,
            "archive_reason": self.archive_reason,
            "archive_source": self.archive_source,
            "archive_fingerprint": self.archive_fingerprint,
            "archive_effective_at": self.archive_effective_at,
            "archive_observed_at": self.archive_observed_at,
            "archive_evidence_present": self.archive_evidence_present,
        }

    @property
    def fingerprint(self) -> str:
        """内容指纹：同一身份上内容相同 → 同一指纹（幂等判据）。"""
        return _sha256(
            {
                "version": FINGERPRINT_VERSION,
                "identity": list(self.identity),
                "content": self.content(),
            }
        )

    def to_dict(self) -> dict:
        payload = {
            "code": self.code,
            "session": self.session,
            "decision_at": self.decision_at,
            "side": self.side,
            "status": self.status,
            "comparable": self.comparable,
            "production_allowed": self.production_allowed,
            "production_reason": self.production_reason,
            "production_status": self.production_status,
            "archive_allowed": self.archive_allowed,
            "archive_reason": self.archive_reason,
            "archive_source": self.archive_source,
            "archive_fingerprint": self.archive_fingerprint,
            "archive_effective_at": self.archive_effective_at,
            "archive_observed_at": self.archive_observed_at,
            "archive_evidence_present": self.archive_evidence_present,
            "contract_version": self.contract_version,
        }
        payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class ShadowComparisonSummary:
    """一批比对的汇总。所有比率在分母为 0 时是 ``None``，不是 0 或 100。"""

    requested: int = 0
    comparable: int = 0
    not_comparable: int = 0
    agree: int = 0
    disagree: int = 0

    agree_allow: int = 0
    agree_block: int = 0
    production_allow_archive_block: int = 0
    production_block_archive_allow: int = 0

    archive_unknown: int = 0
    archive_unprovable: int = 0
    archive_missing: int = 0
    production_unknown: int = 0
    comparison_invalid: int = 0

    by_side: Mapping[str, Any] = field(default_factory=dict)
    by_production_reason: Mapping[str, int] = field(default_factory=dict)
    by_archive_reason: Mapping[str, int] = field(default_factory=dict)
    by_session: Mapping[str, Any] = field(default_factory=dict)
    by_status: Mapping[str, int] = field(default_factory=dict)

    comparison_rate: Optional[float] = None
    agreement_rate: Optional[float] = None
    disagreement_rate: Optional[float] = None

    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict:
        return {
            "requested": self.requested,
            "comparable": self.comparable,
            "not_comparable": self.not_comparable,
            "agree": self.agree,
            "disagree": self.disagree,
            "agree_allow": self.agree_allow,
            "agree_block": self.agree_block,
            "production_allow_archive_block": self.production_allow_archive_block,
            "production_block_archive_allow": self.production_block_archive_allow,
            "archive_unknown": self.archive_unknown,
            "archive_unprovable": self.archive_unprovable,
            "archive_missing": self.archive_missing,
            "production_unknown": self.production_unknown,
            "comparison_invalid": self.comparison_invalid,
            "by_side": {key: dict(value) for key, value in self.by_side.items()},
            "by_production_reason": dict(self.by_production_reason),
            "by_archive_reason": dict(self.by_archive_reason),
            "by_session": {key: dict(value) for key, value in self.by_session.items()},
            "by_status": dict(self.by_status),
            "comparison_rate": self.comparison_rate,
            "agreement_rate": self.agreement_rate,
            "disagreement_rate": self.disagreement_rate,
            "contract_version": self.contract_version,
        }


# ─────────────────────────────── 比对器 ───────────────────────────────


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    """分母为 0 → ``None``（显式 not_available），**绝不**默认 0 / 1。"""
    if not denominator:
        return None
    return numerator / denominator


class ShadowComparator:
    """把生产判定与归档判定放在一起观察。

    只依赖两个**既有权威入口**：生产侧传进来的 ``TradabilityVerdict``（由调用方
    用生产路径取得），归档侧 :func:`tradability_archive.tradability_at`。本类不读
    原始 SQL、不判 ST、不判停牌、不判涨跌停方向。
    """

    def __init__(
        self,
        repository: TA.TradabilityArchiveRepository,
        *,
        contract_version: str = CONTRACT_VERSION,
    ):
        if repository is None:
            raise ShadowError("ShadowComparator 需要 archive repository")
        self._repo = repository
        self._contract_version = contract_version

    # ── 归档侧 ──
    def _archive_side(self, code: str, session: str, decision_at: str, side: str) -> dict:
        """归档判定 + 证据可得性分类（全部走 archive 的公开 API）。"""
        decision = TA.tradability_at(
            code, session, decision_time=decision_at, repository=self._repo
        )
        reason = (
            decision.buy_block_reason if side == ST.SIDE_BUY else decision.sell_block_reason
        )
        reason_value = reason.value if hasattr(reason, "value") else str(reason)
        allowed = decision.can_buy if side == ST.SIDE_BUY else decision.can_sell
        base = {
            "archive_allowed": allowed,
            "archive_reason": reason_value,
            "archive_source": decision.source,
            "archive_fingerprint": decision.fingerprint,
            "archive_effective_at": decision.effective_at,
            "archive_observed_at": decision.observed_at,
            "archive_evidence_present": bool(decision.evidence_present),
        }
        if not decision.evidence_present:
            # 当时不可见。区分"从未摄取"与"摄取了但当时不可知"——后者是 PIT 问题，
            # 前者是证据缺口，两者在归因上完全不同。
            base["archive_state"] = (
                ShadowStatus.ARCHIVE_UNPROVABLE.value
                if self._pair_was_ingested(code, session)
                else ShadowStatus.ARCHIVE_MISSING.value
            )
            base["archive_allowed"] = None
            base["archive_reason"] = None
            base["archive_source"] = None
            base["archive_fingerprint"] = None
            base["archive_effective_at"] = None
            base["archive_observed_at"] = None
            return base
        base["archive_state"] = (
            ShadowStatus.ARCHIVE_UNKNOWN.value
            if reason_value == _ARCHIVE_UNKNOWN_REASON
            else None
        )
        if base["archive_state"] is not None:
            base["archive_allowed"] = None
        return base

    def _pair_was_ingested(self, code: str, session: str) -> bool:
        """该 ``(code, session)`` 历史上是否被摄取过（与可见性无关）。"""
        rows = self._repo.visible_evidence(code, session, ARCHIVE_FAR_FUTURE)
        return bool(rows)

    # ── 生产侧 ──
    def _production_side(self, verdict: Any) -> dict:
        status = _text(_verdict_field(verdict, "status")) or ""
        reason = _text(_verdict_field(verdict, "reason")) or ""
        side = _text(_verdict_field(verdict, "side")) or ""
        return {
            "status": status,
            "reason": reason,
            "side": side,
            "allowed": status == ST.STATUS_EXECUTABLE,
        }

    # ── 主入口 ──
    def compare(
        self,
        production_verdict: Any,
        *,
        code: Any,
        session: Any,
        side: Any,
        decision_at: Any,
    ) -> ShadowComparison:
        """比对一条 ``(code, session, decision_at, side)``。

        ``production_verdict`` 必须是**生产路径真正产出的**判定对象（或它的
        ``as_dict()``）。传 ``None`` 表示生产侧没有结论 → ``production_unknown``。
        """
        code_text = _text(code)
        session_text = _canonical_session(session)
        side_text = _text(side)
        moment = _canonical_instant(decision_at)
        production = self._production_side(production_verdict)

        invalid_identity = (
            code_text is None
            or session_text is None
            or moment is None
            or side_text not in ST.SIDES
            or (production_verdict is not None and production["side"] not in ST.SIDES)
        )
        if invalid_identity:
            return self._build(
                code_text or "", session_text or "", moment or "", side_text or "",
                status=ShadowStatus.COMPARISON_INVALID,
                production=production,
                archive=self._empty_archive(),
                production_allowed=None,
            )

        if production_verdict is None or production["status"] not in ST.TRADABILITY_STATUSES:
            status = ShadowStatus.PRODUCTION_UNKNOWN
            archive = self._empty_archive()
            return self._build(
                code_text, session_text, moment, side_text,
                status=status, production=production, archive=archive,
                production_allowed=None,
            )

        if production["status"] == ST.STATUS_INVALID:
            return self._build(
                code_text, session_text, moment, side_text,
                status=ShadowStatus.COMPARISON_INVALID,
                production=production,
                archive=self._empty_archive(),
                production_allowed=None,
            )

        if production["status"] == ST.STATUS_UNPROVEN:
            # 生产自己说"证据不足"：它不是一条可用于比对的结论。
            archive = self._archive_side(code_text, session_text, moment, side_text)
            return self._build(
                code_text, session_text, moment, side_text,
                status=ShadowStatus.PRODUCTION_UNKNOWN,
                production=production, archive=archive, production_allowed=None,
            )

        archive = self._archive_side(code_text, session_text, moment, side_text)
        production_allowed = bool(production["allowed"])

        if archive["archive_state"] == ShadowStatus.ARCHIVE_MISSING.value:
            status = ShadowStatus.ARCHIVE_MISSING
        elif archive["archive_state"] == ShadowStatus.ARCHIVE_UNPROVABLE.value:
            status = ShadowStatus.ARCHIVE_UNPROVABLE
        elif archive["archive_state"] == ShadowStatus.ARCHIVE_UNKNOWN.value:
            status = ShadowStatus.ARCHIVE_UNKNOWN
        else:
            archive_allowed = bool(archive["archive_allowed"])
            if production_allowed and archive_allowed:
                status = ShadowStatus.AGREE_ALLOW
            elif not production_allowed and not archive_allowed:
                status = ShadowStatus.AGREE_BLOCK
            elif production_allowed and not archive_allowed:
                status = ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK
            else:
                status = ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW

        return self._build(
            code_text, session_text, moment, side_text,
            status=status, production=production, archive=archive,
            production_allowed=production_allowed,
        )

    def compare_many(self, items: Iterable[Mapping[str, Any]]) -> list:
        """批量比对；按身份排序，保证输出确定。"""
        out = []
        for item in items:
            out.append(
                self.compare(
                    item.get("production_verdict"),
                    code=item.get("code"),
                    session=item.get("session"),
                    side=item.get("side"),
                    decision_at=item.get("decision_at"),
                )
            )
        out.sort(key=lambda comparison: comparison.identity)
        return out

    @staticmethod
    def _empty_archive() -> dict:
        return {
            "archive_allowed": None,
            "archive_reason": None,
            "archive_source": None,
            "archive_fingerprint": None,
            "archive_effective_at": None,
            "archive_observed_at": None,
            "archive_evidence_present": False,
            "archive_state": None,
        }

    def _build(
        self,
        code: str,
        session: str,
        decision_at: str,
        side: str,
        *,
        status: ShadowStatus,
        production: Mapping[str, Any],
        archive: Mapping[str, Any],
        production_allowed: Optional[bool],
    ) -> ShadowComparison:
        return ShadowComparison(
            code=code,
            session=session,
            decision_at=decision_at,
            side=side,
            status=status.value,
            comparable=status in COMPARABLE_STATUSES,
            production_allowed=production_allowed,
            production_reason=production.get("reason") or "",
            production_status=production.get("status") or "",
            archive_allowed=archive.get("archive_allowed"),
            archive_reason=archive.get("archive_reason"),
            archive_source=archive.get("archive_source"),
            archive_fingerprint=archive.get("archive_fingerprint"),
            archive_effective_at=archive.get("archive_effective_at"),
            archive_observed_at=archive.get("archive_observed_at"),
            archive_evidence_present=bool(archive.get("archive_evidence_present")),
            contract_version=self._contract_version,
        )

    # ── 汇总 ──
    def summarize(self, comparisons: Sequence[ShadowComparison]) -> ShadowComparisonSummary:
        """汇总一批比对。分母只取 comparable；分母为 0 时比率是 ``None``。"""
        counts = {status.value: 0 for status in SHADOW_STATUSES}
        agree_values = {status.value for status in AGREE_STATUSES}
        disagree_values = {status.value for status in DISAGREE_STATUSES}
        by_side: dict = {}
        by_production_reason: dict = {}
        by_archive_reason: dict = {}
        by_session: dict = {}

        for comparison in comparisons:
            if comparison.status not in counts:
                # 未知 status 不得被静默吞掉：契约被违反。
                raise ShadowError(f"未知 comparison status: {comparison.status!r}")
            counts[comparison.status] += 1
            is_agree = comparison.status in agree_values
            is_disagree = comparison.status in disagree_values

            for bucket, key in ((by_side, comparison.side), (by_session, comparison.session)):
                entry = bucket.setdefault(
                    key, {"requested": 0, "comparable": 0, "agree": 0, "disagree": 0}
                )
                entry["requested"] += 1
                entry["comparable"] += int(comparison.comparable)
                entry["agree"] += int(is_agree)
                entry["disagree"] += int(is_disagree)

            production_reason = comparison.production_reason or "none"
            by_production_reason[production_reason] = (
                by_production_reason.get(production_reason, 0) + 1
            )
            archive_reason = comparison.archive_reason or "none"
            by_archive_reason[archive_reason] = by_archive_reason.get(archive_reason, 0) + 1

        agree = counts[ShadowStatus.AGREE_ALLOW.value] + counts[ShadowStatus.AGREE_BLOCK.value]
        disagree = (
            counts[ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value]
            + counts[ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW.value]
        )
        comparable = sum(counts[status.value] for status in COMPARABLE_STATUSES)
        not_comparable = sum(counts[status.value] for status in NOT_COMPARABLE_STATUSES)
        requested = len(comparisons)

        return ShadowComparisonSummary(
            requested=requested,
            comparable=comparable,
            not_comparable=not_comparable,
            agree=agree,
            disagree=disagree,
            agree_allow=counts[ShadowStatus.AGREE_ALLOW.value],
            agree_block=counts[ShadowStatus.AGREE_BLOCK.value],
            production_allow_archive_block=counts[
                ShadowStatus.PRODUCTION_ALLOW_ARCHIVE_BLOCK.value
            ],
            production_block_archive_allow=counts[
                ShadowStatus.PRODUCTION_BLOCK_ARCHIVE_ALLOW.value
            ],
            archive_unknown=counts[ShadowStatus.ARCHIVE_UNKNOWN.value],
            archive_unprovable=counts[ShadowStatus.ARCHIVE_UNPROVABLE.value],
            archive_missing=counts[ShadowStatus.ARCHIVE_MISSING.value],
            production_unknown=counts[ShadowStatus.PRODUCTION_UNKNOWN.value],
            comparison_invalid=counts[ShadowStatus.COMPARISON_INVALID.value],
            by_side={key: dict(sorted(value.items())) for key, value in sorted(by_side.items())},
            by_production_reason=dict(sorted(by_production_reason.items())),
            by_archive_reason=dict(sorted(by_archive_reason.items())),
            by_session={
                key: dict(sorted(value.items())) for key, value in sorted(by_session.items())
            },
            by_status=dict(sorted(counts.items())),
            comparison_rate=_ratio(comparable, requested),
            agreement_rate=_ratio(agree, comparable),
            disagreement_rate=_ratio(disagree, comparable),
            contract_version=self._contract_version,
        )


# ───────────────────────── 可选持久化（独立表） ─────────────────────────
#
# 默认**不**持久化：本 PR 的消费方是只读 CLI / 报告，无消费方的表不该提前建。
# 需要留存比对结果时显式调用 ``save_comparisons``；表与生产表完全隔离
# （不复用 historical_tradability_archive / tradability_ingestion_runs / orders /
# fills / positions / selection labels / learning tables）。

_SHADOW_COLUMNS = (
    "comparison_id", "code", "session", "decision_at", "side",
    "production_allowed", "production_reason", "production_status",
    "archive_allowed", "archive_reason", "archive_source", "archive_fingerprint",
    "archive_effective_at", "archive_observed_at", "archive_evidence_present",
    "comparison_status", "comparable", "content_fingerprint",
    "contract_version", "created_at",
)


def ensure_shadow_schema(conn: sqlite3.Connection) -> dict:
    """建比对结果表（幂等）。唯一身份 = (code, session, decision_at, side, contract)。"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SHADOW_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comparison_id TEXT NOT NULL,
            code TEXT NOT NULL,
            session TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            side TEXT NOT NULL,
            production_allowed INTEGER,
            production_reason TEXT,
            production_status TEXT,
            archive_allowed INTEGER,
            archive_reason TEXT,
            archive_source TEXT,
            archive_fingerprint TEXT,
            archive_effective_at TEXT,
            archive_observed_at TEXT,
            archive_evidence_present INTEGER NOT NULL DEFAULT 0,
            comparison_status TEXT NOT NULL,
            comparable INTEGER NOT NULL DEFAULT 0,
            content_fingerprint TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(code, session, decision_at, side, contract_version)
        )
        """
    )
    return {"table": SHADOW_TABLE, "migration": MIGRATION_DESCRIPTION}


def _db_flag(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(bool(value))


def _comparison_row(comparison: ShadowComparison, created_at: str) -> dict:
    return {
        "comparison_id": hashlib.sha256(
            "|".join(comparison.identity).encode("utf-8")
        ).hexdigest()[:32],
        "code": comparison.code,
        "session": comparison.session,
        "decision_at": comparison.decision_at,
        "side": comparison.side,
        "production_allowed": _db_flag(comparison.production_allowed),
        "production_reason": comparison.production_reason,
        "production_status": comparison.production_status,
        "archive_allowed": _db_flag(comparison.archive_allowed),
        "archive_reason": comparison.archive_reason,
        "archive_source": comparison.archive_source,
        "archive_fingerprint": comparison.archive_fingerprint,
        "archive_effective_at": comparison.archive_effective_at,
        "archive_observed_at": comparison.archive_observed_at,
        "archive_evidence_present": int(bool(comparison.archive_evidence_present)),
        "comparison_status": comparison.status,
        "comparable": int(bool(comparison.comparable)),
        "content_fingerprint": comparison.fingerprint,
        "contract_version": comparison.contract_version,
        "created_at": created_at,
    }


def save_comparison(conn: sqlite3.Connection, comparison: ShadowComparison) -> str:
    """写入一条比对结果。

    返回 ``"inserted"``（新行）或 ``"identical"``（同身份 + 同内容 = no-op）。
    同身份 + **不同内容** → :class:`ShadowConflictError`，**绝不** last-write-wins。
    """
    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    row = _comparison_row(comparison, created_at)
    existing = conn.execute(
        f"SELECT content_fingerprint FROM {SHADOW_TABLE} "
        "WHERE code=? AND session=? AND decision_at=? AND side=? AND contract_version=?",
        comparison.identity,
    ).fetchone()
    if existing is not None:
        stored = existing[0]
        if stored == row["content_fingerprint"]:
            return "identical"
        raise ShadowConflictError(
            "同一比对身份出现冲突内容："
            f"{comparison.identity} stored={stored} incoming={row['content_fingerprint']}"
        )
    columns = ", ".join(_SHADOW_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in _SHADOW_COLUMNS)
    conn.execute(
        f"INSERT INTO {SHADOW_TABLE}({columns}) VALUES({placeholders})", row
    )
    return "inserted"


def save_comparisons(
    conn: sqlite3.Connection, comparisons: Sequence[ShadowComparison]
) -> dict:
    """批量写入；先全部判冲突再落库，任何冲突都会抛错（调用方 rollback）。"""
    ensure_shadow_schema(conn)
    outcomes = {"inserted": 0, "identical": 0}
    for comparison in comparisons:
        outcomes[save_comparison(conn, comparison)] += 1
    return outcomes


def load_comparisons(
    conn: sqlite3.Connection, *, session: Any = None, code: Any = None
) -> list:
    """读回比对结果（按身份排序）。表不存在 → 空列表。"""
    clauses = []
    params: list = []
    if session is not None:
        clauses.append("session=?")
        params.append(str(session))
    if code is not None:
        clauses.append("code=?")
        params.append(str(code))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        cursor = conn.execute(
            f"SELECT {', '.join(_SHADOW_COLUMNS)} FROM {SHADOW_TABLE}{where} "
            "ORDER BY code, session, decision_at, side",
            tuple(params),
        )
    except sqlite3.OperationalError:
        return []
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=False)) for row in cursor.fetchall()]


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:  # pragma: no cover - 手工冒烟
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    repo = TA.TradabilityArchiveRepository(conn)
    comparator = ShadowComparator(repo)
    comparison = comparator.compare(
        None, code="000001", session="2024-01-10",
        side=ST.SIDE_BUY, decision_at="2024-01-10T15:00:00+08:00",
    )
    assert comparison.status == ShadowStatus.PRODUCTION_UNKNOWN.value, comparison.status
    summary = comparator.summarize([comparison])
    assert summary.requested == 1 and summary.comparable == 0
    assert summary.agreement_rate is None and summary.disagreement_rate is None
    print("tradability_shadow self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _self_check()
