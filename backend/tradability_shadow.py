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
    import tradability_observation_ledger as OL
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT  # type: ignore
    from . import selection_tradability as ST  # type: ignore
    from . import tradability_archive as TA  # type: ignore
    from . import tradability_observation_ledger as OL  # type: ignore


#: v2 起比对身份包含 ``validation_as_of``（见 :meth:`ShadowComparison.identity`）。
#: 同一条历史决策在**不同知识时点**做验证是两个不同的结论快照，绝不能让 2026-10-01
#: 的验证覆盖（或与）2026-09-17 的验证冲突。
CONTRACT_VERSION = "tradability-shadow-v2"
FINGERPRINT_VERSION = "sha256-canonical-shadow-v2"
MIGRATION_DESCRIPTION = "016_add_tradability_shadow_comparisons"
SHADOW_TABLE = "tradability_shadow_comparisons"

#: 归档判定里表示"事实未知/不足"的原因（与 :class:`TA.TradabilityReason` 同源）。
_ARCHIVE_UNKNOWN_REASON = TA.TradabilityReason.UNKNOWN_STATE.value

#: "该 pair 在 archive 里有没有任何行"的探测时点。它只用于诊断标签，不参与 verdict。
_FAR_FUTURE = "9999-12-31T23:59:59+08:00"


def _diagnose_missing(knowledge: Any, decision_at: str) -> tuple:
    """``archive_missing`` 的**诊断细分**（不改变顶层 status）。

    返回 ``(status, diagnostic)``。所有分支都仍然 not_comparable，绝不进入 disagreement
    分母——它们回答的是"为什么没有可用证据"，而不是"两侧判断是否一致"。
    """
    if knowledge.never_observed:
        if knowledge.legacy_observation_unknown:
            return (
                ShadowStatus.ARCHIVE_MISSING.value,
                ShadowStatus.ARCHIVE_LEGACY_OBSERVATION_UNKNOWN.value,
            )
        return (
            ShadowStatus.ARCHIVE_MISSING.value,
            ShadowStatus.ARCHIVE_NEVER_OBSERVED.value,
        )
    outcomes = knowledge.provider_outcomes or {}
    if outcomes.get(OL.OBSERVED_ERROR):
        return (
            ShadowStatus.ARCHIVE_MISSING.value,
            ShadowStatus.ARCHIVE_PROVIDER_ERROR.value,
        )
    if outcomes.get(OL.OBSERVED_UNKNOWN):
        return (
            ShadowStatus.ARCHIVE_MISSING.value,
            ShadowStatus.ARCHIVE_PROVIDER_UNKNOWN.value,
        )
    # 观察到了证据，但截至 validation_as_of 它仍不足以证明 decision_at 当时可知
    # （例如 source_observed_at 晚于 decision）——这是"晚观察到"，不是"从未观察"。
    return ShadowStatus.ARCHIVE_UNPROVABLE.value, None


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
    #: 诊断细分（**不是**顶层 status）：``archive_missing`` 在"为什么没有证据"上的
    #: 进一步区分。它们仍然全部 not_comparable，绝不进入 disagreement 分母。
    ARCHIVE_NEVER_OBSERVED = "archive_never_observed"
    ARCHIVE_PROVIDER_UNKNOWN = "archive_provider_unknown"
    ARCHIVE_PROVIDER_ERROR = "archive_provider_error"
    ARCHIVE_LEGACY_OBSERVATION_UNKNOWN = "archive_legacy_observation_unknown"
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

    #: 我们站在哪个知识时点做这次验证（``validation_as_of``）。它是比对身份的组成部分。
    validation_as_of: Optional[str] = None

    #: Ledger 派生的**诊断**信息（``archive_missing`` 的细分原因）。它们不改变 status，
    #: 只是让人能回答"为什么没有证据"。含 ``can_buy`` / ``can_sell`` 就是架构违规。
    archive_diagnostic: Optional[str] = None
    first_observed_at: Optional[str] = None
    first_evidence_observed_at: Optional[str] = None
    market_provable_at_decision: Optional[bool] = None
    system_possessed_at_decision: Optional[bool] = None
    observation_count: int = 0
    provider_outcomes: Mapping[str, int] = field(default_factory=dict)

    contract_version: str = CONTRACT_VERSION

    @property
    def identity(self) -> tuple:
        """``(code, session, decision_at, side, validation_as_of, contract_version)``。

        ``validation_as_of`` 必须在身份里：同一条历史决策在 2026-09-17 与 2026-10-01
        做的验证是两个**不同的知识快照**。若它不参与身份，今天新摄取一条观察就会让
        昨天那条已持久化的比对产生 conflict——那正是"历史结论随今天数据库里有什么而
        漂移"。
        """
        return (
            self.code,
            self.session,
            self.decision_at,
            self.side,
            self.validation_as_of,
            self.contract_version,
        )

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
            "archive_diagnostic": self.archive_diagnostic,
            "market_provable_at_decision": self.market_provable_at_decision,
            "system_possessed_at_decision": self.system_possessed_at_decision,
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
            "validation_as_of": self.validation_as_of,
            "archive_diagnostic": self.archive_diagnostic,
            "first_observed_at": self.first_observed_at,
            "first_evidence_observed_at": self.first_evidence_observed_at,
            "market_provable_at_decision": self.market_provable_at_decision,
            "system_possessed_at_decision": self.system_possessed_at_decision,
            "observation_count": self.observation_count,
            "provider_outcomes": dict(sorted(self.provider_outcomes.items())),
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
        ledger: Optional[Any] = None,
        contract_version: str = CONTRACT_VERSION,
    ):
        if repository is None:
            raise ShadowError("ShadowComparator 需要 archive repository")
        self._repo = repository
        # 观察台账（可选）。它**只**用来回答"我们什么时候看到/尝试看到这条 pair"，
        # 绝不参与 archive verdict 的推导——那是 tradability_archive 的 authority。
        self._ledger = ledger
        self._contract_version = contract_version

    # ── 归档侧 ──
    def _archive_side(
        self,
        code: str,
        session: str,
        decision_at: str,
        side: str,
        *,
        ingested_later: bool = False,
        validation_as_of: Optional[str] = None,
    ) -> dict:
        """归档判定 + 证据可得性分类。

        archive verdict 一律来自 :func:`tradability_archive.tradability_at`（唯一
        authority）；本方法**不**重写任何市场规则。

        ``archive_missing`` 与 ``archive_unprovable`` 的区分由**观察台账**在明确的
        ``validation_as_of`` 知识时点下给出：

        * 截至 ``validation_as_of`` 没有任何 evidence 观察 → ``archive_missing``
          （并进一步诊断是"从未观察"、"只观察到 provider unknown/error"，还是
          "升级前的历史数据，真实观察时间不可知"）；
        * 有 evidence 观察，但 ``decision_at`` 当时不可证明（``source_observed_at`` /
          ``effective_at`` 晚于 decision，或系统是后来才摄取到的）→
          ``archive_unprovable``。

        台账缺失时退化为 v1 行为：只有调用方显式声明 ``ingested_later`` 才判
        unprovable，否则 ``archive_missing``。**绝不**自己去查"后来有没有"——那是
        未来事实，会让历史结论随之后的摄取而改写。
        """
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
            "archive_diagnostic": None,
            "first_observed_at": None,
            "first_evidence_observed_at": None,
            "market_provable_at_decision": None,
            "system_possessed_at_decision": None,
            "observation_count": 0,
            "provider_outcomes": {},
        }

        knowledge = self._knowledge(
            code, session, validation_as_of=validation_as_of, decision_at=decision_at
        )
        if knowledge is not None:
            base.update(
                {
                    "first_observed_at": knowledge.first_seen_at,
                    "first_evidence_observed_at": knowledge.first_evidence_seen_at,
                    "market_provable_at_decision": knowledge.market_provable_at_decision,
                    "system_possessed_at_decision": knowledge.system_possessed_at_decision,
                    "observation_count": knowledge.observation_count,
                    "provider_outcomes": dict(knowledge.provider_outcomes),
                }
            )

        if not decision.evidence_present:
            if knowledge is not None:
                base["archive_state"], base["archive_diagnostic"] = _diagnose_missing(
                    knowledge, decision_at
                )
            else:
                base["archive_state"] = (
                    ShadowStatus.ARCHIVE_UNPROVABLE.value
                    if ingested_later
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

    def _knowledge(
        self,
        code: str,
        session: str,
        *,
        validation_as_of: Optional[str],
        decision_at: str,
    ) -> Optional[Any]:
        """台账知识（台账缺失或 ``validation_as_of`` 非法时返回 ``None``）。"""
        if self._ledger is None:
            return None
        try:
            return self._ledger.knowledge_at(
                code,
                session,
                validation_as_of=validation_as_of,
                decision_at=decision_at,
                archive_has_row=self._archive_has_row(code, session),
            )
        except OL.ObservationError:
            return None

    def _archive_has_row(self, code: str, session: str) -> bool:
        """archive 里是否存在该 pair 的**任何**行（用于识别升级前的历史数据）。

        这个查询**只**用于诊断标签（"历史数据，真实观察时间不可知"），不参与任何
        verdict；而且它问的是"这条 pair 有没有事实行"，不是"它当时可不可见"。
        """
        try:
            return bool(self._repo.visible_evidence(code, session, _FAR_FUTURE))
        except Exception:  # pragma: no cover - 防御 repository 实现差异
            return False

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
        ingested_later: bool = False,
        validation_as_of: Any = None,
    ) -> ShadowComparison:
        """比对一条 ``(code, session, decision_at, side)``。

        ``production_verdict`` 必须是**生产路径真正产出的**判定对象（或它的
        ``as_dict()``）。传 ``None`` 表示生产侧没有结论 → ``production_unknown``。

        ``decision_at`` 与 ``validation_as_of`` 是两个不同的时间：

        * ``decision_at`` —— 被复盘的真实历史决策时点；
        * ``validation_as_of`` —— **我们站在哪个知识时点做这次验证**。

        例如 ``decision_at = 2025-03-01``、``validation_as_of = 2026-09-17``：可以诚实
        回答"截至 2026-09-17，我们知道某证据首次在 2025-03-10 被摄取，因此它在
        2025-03-01 决策时不可用，但它不是'永远没有数据'，而是 late-observed"。
        ``validation_as_of`` 也进入比对身份，因此不同知识时点的验证是不同快照。

        ``ingested_later`` 是**台账缺失时**的兜底声明（v1 行为）。台账可用时由台账
        在 ``validation_as_of`` 下判定，不需要调用方声明。
        """
        code_text = _text(code)
        session_text = _canonical_session(session)
        side_text = _text(side)
        moment = _canonical_instant(decision_at)
        # validation_as_of 缺失 = "用当前知识时点"（不设上界）；显式给出但不可解析
        # 则整条比对判 invalid，**绝不**退化成"看全部未来数据"。
        as_of_text = (
            None if validation_as_of is None else _canonical_instant(validation_as_of)
        )
        as_of_invalid = validation_as_of is not None and as_of_text is None
        production = self._production_side(production_verdict)

        # 生产 verdict 的 side 必须与本次比对的 side **一致**：一个 sell verdict 配
        # side="buy" 会把 sell 的结论当成 buy 的生产结果，再去比归档的 buy 结论，
        # 产出一条标签错误的"一致/分歧"。
        side_mismatch = (
            production_verdict is not None
            and production["side"] in ST.SIDES
            and production["side"] != side_text
        )
        invalid_identity = (
            code_text is None
            or session_text is None
            or moment is None
            or side_text not in ST.SIDES
            or (production_verdict is not None and production["side"] not in ST.SIDES)
            or side_mismatch
            or as_of_invalid
        )
        if invalid_identity:
            return self._build(
                code_text or "", session_text or "", moment or "", side_text or "",
                status=ShadowStatus.COMPARISON_INVALID,
                production=production,
                archive=self._empty_archive(),
                production_allowed=None,
                validation_as_of=as_of_text,
            )

        if production_verdict is None or production["status"] not in ST.TRADABILITY_STATUSES:
            status = ShadowStatus.PRODUCTION_UNKNOWN
            archive = self._empty_archive()
            return self._build(
                code_text, session_text, moment, side_text,
                status=status, production=production, archive=archive,
                production_allowed=None, validation_as_of=as_of_text,
            )

        if production["status"] == ST.STATUS_INVALID:
            return self._build(
                code_text, session_text, moment, side_text,
                status=ShadowStatus.COMPARISON_INVALID,
                production=production,
                archive=self._empty_archive(),
                production_allowed=None, validation_as_of=as_of_text,
            )

        if production["status"] == ST.STATUS_UNPROVEN:
            # 生产自己说"证据不足"：它不是一条可用于比对的结论。
            archive = self._archive_side(
                code_text, session_text, moment, side_text,
                ingested_later=ingested_later, validation_as_of=as_of_text,
            )
            return self._build(
                code_text, session_text, moment, side_text,
                status=ShadowStatus.PRODUCTION_UNKNOWN,
                production=production, archive=archive, production_allowed=None,
                validation_as_of=as_of_text,
            )

        archive = self._archive_side(
            code_text, session_text, moment, side_text,
            ingested_later=ingested_later, validation_as_of=as_of_text,
        )
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
            production_allowed=production_allowed, validation_as_of=as_of_text,
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
                    ingested_later=bool(item.get("ingested_later", False)),
                    validation_as_of=item.get("validation_as_of"),
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
        validation_as_of: Optional[str] = None,
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
            validation_as_of=validation_as_of,
            archive_diagnostic=archive.get("archive_diagnostic"),
            first_observed_at=archive.get("first_observed_at"),
            first_evidence_observed_at=archive.get("first_evidence_observed_at"),
            market_provable_at_decision=archive.get("market_provable_at_decision"),
            system_possessed_at_decision=archive.get("system_possessed_at_decision"),
            observation_count=int(archive.get("observation_count") or 0),
            provider_outcomes=dict(archive.get("provider_outcomes") or {}),
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
    "comparison_id", "code", "session", "decision_at", "side", "validation_as_of",
    "production_allowed", "production_reason", "production_status",
    "archive_allowed", "archive_reason", "archive_source", "archive_fingerprint",
    "archive_effective_at", "archive_observed_at", "archive_evidence_present",
    "archive_diagnostic", "first_observed_at", "first_evidence_observed_at",
    "market_provable_at_decision", "system_possessed_at_decision",
    "comparison_status", "comparable", "content_fingerprint",
    "contract_version", "created_at",
)


def ensure_shadow_schema(conn: sqlite3.Connection) -> dict:
    """建比对结果表（幂等）。

    唯一身份 = ``(code, session, decision_at, side, validation_as_of, contract_version)``
    —— ``validation_as_of`` 在身份里，因此 2026-09-17 与 2026-10-01 两次知识时点的验证
    各自成行，今天新摄取一条观察**不会**让昨天那条已持久化的比对变成 conflict。
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SHADOW_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comparison_id TEXT NOT NULL,
            code TEXT NOT NULL,
            session TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            side TEXT NOT NULL,
            validation_as_of TEXT NOT NULL DEFAULT '',
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
            archive_diagnostic TEXT,
            first_observed_at TEXT,
            first_evidence_observed_at TEXT,
            market_provable_at_decision INTEGER,
            system_possessed_at_decision INTEGER,
            comparison_status TEXT NOT NULL,
            comparable INTEGER NOT NULL DEFAULT 0,
            content_fingerprint TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(code, session, decision_at, side, validation_as_of, contract_version)
        )
        """
    )
    return {"table": SHADOW_TABLE, "migration": MIGRATION_DESCRIPTION}


def _db_flag(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(bool(value))


def _comparison_row(comparison: ShadowComparison, created_at: str) -> dict:
    return {
        "comparison_id": hashlib.sha256(
            "|".join(
                "" if part is None else str(part) for part in comparison.identity
            ).encode("utf-8")
        ).hexdigest()[:32],
        "code": comparison.code,
        "session": comparison.session,
        "decision_at": comparison.decision_at,
        "side": comparison.side,
        # 身份列不能为 NULL：SQLite 的 UNIQUE 把 NULL 视为互不相等，会让"同一身份
        # 重复写入"绕过唯一约束。身份里缺失的 validation_as_of 用空串占位。
        "validation_as_of": comparison.validation_as_of or "",
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
        "archive_diagnostic": comparison.archive_diagnostic,
        "first_observed_at": comparison.first_observed_at,
        "first_evidence_observed_at": comparison.first_evidence_observed_at,
        "market_provable_at_decision": _db_flag(comparison.market_provable_at_decision),
        "system_possessed_at_decision": _db_flag(comparison.system_possessed_at_decision),
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
        "WHERE code=? AND session=? AND decision_at=? AND side=? "
        "AND validation_as_of=? AND contract_version=?",
        (
            comparison.code,
            comparison.session,
            comparison.decision_at,
            comparison.side,
            comparison.validation_as_of or "",
            comparison.contract_version,
        ),
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
