# -*- coding: utf-8 -*-
"""Tradability **Observation Ledger** —— 记录"我们什么时候看到/尝试看到这些事实"。

本模块回答的**唯一**问题是：

    截至某个知识时点 ``validation_as_of``，我们的系统对这个 ``(code, session)``
    究竟观察到了什么？第一次是什么时候观察到的？

它**不**回答、也**绝不**决定：

* 这只股票能不能买 / 能不能卖（那是 :mod:`tradability_archive` 的判断层）；
* 订单是否应该执行（那是执行链路）；
* 任何策略评分、因子、收益。

──────────────────────── 与 Archive 的分工（四层不得混） ────────────────────────

=================================  ====================================================
层                                  回答的问题
=================================  ====================================================
``tradability_archive``             **市场事实是什么**（上市/ST/停牌/行情/成交量/涨跌停锁定）
``tradability_observation_ledger``  我们**什么时候**看到 / 尝试看到这些事实（本模块）
``tradability_shadow``              生产判断与 archive 判断如何比较（只读观察）
execution                           真实订单 authority
=================================  ====================================================

Ledger 是 **audit fact sink**，不是"决定 archive 是否可写"的第二套规则层。

──────────────────────── 三个时间绝不能混写 ────────────────────────

``effective_at``
    市场事实什么时候**生效**。
``source_observed_at``
    **上游来源声明**该事实何时可知（``ProviderResult.observed_at``）。
``recorded_at``
    **我们自己的系统**什么时候真正摄取到这条观察。

特别禁止：``recorded_at = effective_at`` / ``recorded_at = session_date`` /
``first_seen_at = 某个历史日期``——除非事实真的在那个时间被系统观察到。这是时间旅行。

──────────────────────── Append-only ────────────────────────

只维护 ``(code, session) -> first_seen_at`` 并 UPDATE 是不够的：那样无法审计 provider
变化、``error -> evidence`` 的翻转、不同 provider 版本、同一 pair 的多次观察，也无法
证明 ``first_seen_at`` 的来源。因此本模块：

* **只追加 observation event**，永不 UPDATE / DELETE 既有事件；
* ``first_seen_at = MIN(recorded_at)`` 是**派生结果**，不是存储状态。

──────────────────────── 为什么需要它 ────────────────────────

Archive 只能回答"``decision_at`` 当时有没有可见证据"，不能回答"这条 pair 后来第一次
什么时候被摄取"。若直接拿"当前 archive 里有没有记录"来区分 ``archive_missing`` 与
``archive_unprovable``，今天补一条晚观察的记录就会改写昨天那条历史比对的分类——
那是用未来事实重写历史结论。Ledger 把这个判断建立在一个**不可变的历史观察序列**上，
并且必须显式带上 ``validation_as_of``（"我们站在哪个知识时点做这次验证"）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

try:  # ``backend`` 在 sys.path（生产与 ``cd backend`` 测试）
    import point_in_time as PIT
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT  # type: ignore


def _default_observed_kind() -> str:
    """``OBSERVED_UNPROVABLE`` 的**唯一来源**是 :mod:`tradability_ingestion`。

    这里刻意用惰性 import：``tradability_ingestion`` 需要 import 本模块来写 ledger，
    模块级互相 import 会成环。惰性 import 让两侧都能拿到同一个权威常量，而不是在本地
    复制一个字符串字面量（复制品会在上游改值时静默漂移）。
    """
    try:
        import tradability_ingestion as TI
    except ImportError:  # pragma: no cover - package-style import
        from . import tradability_ingestion as TI  # type: ignore
    return TI.OBSERVED_UNPROVABLE


CONTRACT_VERSION = "tradability-observation-ledger-v1"
FINGERPRINT_VERSION = "sha256-canonical-observation-v1"
MIGRATION_DESCRIPTION = "015_add_tradability_observation_ledger"
MIGRATION_DESCRIPTION_LINKS = "017_add_tradability_archive_observation_links"
LEDGER_TABLE = "tradability_observation_ledger"

#: archive 事实行 → observation event 的**行级** provenance 链接（append-only）。
#: 只由摄取写路径在**新插入** archive 行时写入，因此它证明的是"这条 archive 行与那次
#: observation event 同处一个 ingestion transaction"，而不是"内容看起来一样"。
ARCHIVE_LINK_TABLE = "tradability_archive_observation_links"

#: 观察结果三态（与 :mod:`tradability_ingestion` 的 provider outcome 同源）。
OBSERVED_EVIDENCE = "evidence"
OBSERVED_UNKNOWN = "unknown"
OBSERVED_ERROR = "error"
OBSERVED_STATUSES = (OBSERVED_EVIDENCE, OBSERVED_UNKNOWN, OBSERVED_ERROR)

#: 该 pair 在 Ledger 里**从未被观察**（既没有 evidence 也没有 unknown/error 事件）。
#: 与"观察过、但 provider 明确返回 unknown"不是同一件事。
NEVER_OBSERVED = "never_observed"

#: 该 pair 有 archive 行、但没有任何 ledger 事件：升级前的历史数据。
#: 真实的 ``first_seen_at`` **无法从 archive 反推**，因此这里诚实地说"不知道"，
#: 绝不把 ``archive.created_at`` 或 ``session_date`` 伪装成 first_seen。
LEGACY_OBSERVATION_UNKNOWN = "legacy_observation_unknown"

__all__ = [
    "CONTRACT_VERSION",
    "FINGERPRINT_VERSION",
    "MIGRATION_DESCRIPTION",
    "LEDGER_TABLE",
    "ARCHIVE_LINK_TABLE",
    "OBSERVED_EVIDENCE",
    "OBSERVED_UNKNOWN",
    "OBSERVED_ERROR",
    "OBSERVED_STATUSES",
    "NEVER_OBSERVED",
    "LEGACY_OBSERVATION_UNKNOWN",
    "ObservationError",
    "ObservationEvent",
    "ObservationKnowledge",
    "ObservationCoverage",
    "ArchiveObservationCoverage",
    "reconcile_archive_rows",
    "normalize_error_identity",
    "observation_fingerprint",
    "ensure_ledger_schema",
    "ensure_archive_link_schema",
    "ObservationLedgerRepository",
    "coverage",
]


class ObservationError(ValueError):
    """观察事件无法成立（缺失/不可解析的 code、session、provider 或时点）。"""


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


def now_utc() -> str:
    """系统当前时刻（``recorded_at`` 的默认来源）。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ───────────────────────── 错误身份规范化 ─────────────────────────
#
# 绝不能把完整异常文本当稳定身份：随机 request id、绝对路径、动态时间、临时 URL token
# 都会让 fingerprint 无意义地漂移。这里把它拆成：
#
#     error_class       稳定类别（异常类型名 / 受控词表）
#     error_diagnostic  已脱敏的诊断文本（**只给人看，不进指纹**）
#     error_fingerprint 只覆盖 error_class

#: 看起来像"会漂移的"片段：绝对路径、URL 带查询串、长十六进制/UUID、时间戳。
_PATH_RE = re.compile(r"(?i)(?:[A-Za-z]:[\\/]|/(?:root|home|Users|mnt|opt|srv|var)/)[^\s'\"]*")
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\"]*")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")
#: 常见异常类型名的形状（``TimeoutError`` / ``HTTPError`` / ``KeyError`` …）。
_CLASS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{1,60}(?:Error|Exception|Warning|Timeout))")


def normalize_error_identity(error: Any) -> tuple:
    """把 provider 错误归一成 ``(error_class, error_diagnostic)``。

    ``error_class`` 是**稳定类别**：优先取文本开头的异常类型名；取不到时退化为受控
    类别 ``"provider_error"``——绝不把整段文本当成类别。

    ``error_diagnostic`` 是脱敏后的文本，供人排查；它**不进入**任何指纹，因此其中的
    路径 / URL / id / 时间戳变化不会让 observation identity 漂移。
    """
    text = _text(error)
    if text is None:
        return "provider_error", ""
    match = _CLASS_RE.match(text)
    error_class = match.group(1) if match else "provider_error"

    diagnostic = text
    for pattern, replacement in (
        (_UUID_RE, "<uuid>"),
        (_TIMESTAMP_RE, "<time>"),
        (_URL_RE, "<url>"),
        (_PATH_RE, "<path>"),
        (_HEX_RE, "<hex>"),
    ):
        diagnostic = pattern.sub(replacement, diagnostic)
    return error_class, diagnostic


# ─────────────────────────────── 观察事件 ───────────────────────────────


@dataclass(frozen=True, slots=True)
class ObservationEvent:
    """一次 provider 观察（**append-only**，永不修改）。

    ``observation_fingerprint`` 描述"观察**内容**"；``recorded_at`` 描述"系统**何时**
    看到"。两者刻意分开：前者进 content identity，后者进 PIT 可见性。

    identity **不含** ``id`` / 插入顺序 / ``created_at`` / 随机 UUID。
    """

    code: str
    session_date: str
    provider_id: str
    provider_version: str
    provider_status: str

    source_observed_kind: str
    source_observed_at: Optional[str]
    effective_at: Optional[str]

    recorded_at: str
    ingestion_run_id: str

    evidence_fingerprint: Optional[str] = None
    error_class: Optional[str] = None
    error_fingerprint: Optional[str] = None
    error_diagnostic: Optional[str] = None

    contract_version: str = CONTRACT_VERSION

    @property
    def identity(self) -> tuple:
        """幂等键：同一次 run 里，同一个 provider 对同一 pair 的同一次观察。"""
        return (
            self.ingestion_run_id,
            self.code,
            self.session_date,
            self.provider_id,
            self.observation_fingerprint,
        )

    def content(self) -> dict:
        """进入 ``observation_fingerprint`` 的字段。

        **不含** ``recorded_at``（那是"系统何时看到"，不是"看到了什么"），也**不含**
        ``error_diagnostic``（已脱敏但仍是自由文本，会漂移）。
        """
        return {
            "contract_version": self.contract_version,
            "code": self.code,
            "session_date": self.session_date,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_status": self.provider_status,
            "source_observed_kind": self.source_observed_kind,
            "source_observed_at": self.source_observed_at,
            "effective_at": self.effective_at,
            "evidence_fingerprint": self.evidence_fingerprint,
            "error_fingerprint": self.error_fingerprint,
        }

    @property
    def observation_fingerprint(self) -> str:
        return _sha256(
            {"version": FINGERPRINT_VERSION, "content": self.content()}
        )

    def to_row(self) -> dict:
        return {
            "code": self.code,
            "session_date": self.session_date,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_status": self.provider_status,
            "source_observed_kind": self.source_observed_kind,
            "source_observed_at": self.source_observed_at,
            "effective_at": self.effective_at,
            "recorded_at": self.recorded_at,
            "ingestion_run_id": self.ingestion_run_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "observation_fingerprint": self.observation_fingerprint,
            "error_class": self.error_class,
            "error_fingerprint": self.error_fingerprint,
            "error_diagnostic": self.error_diagnostic,
            "contract_version": self.contract_version,
        }

    def to_dict(self) -> dict:
        payload = dict(self.to_row())
        payload["id"] = None  # 由存储层填充
        return payload


def observation_fingerprint(event: ObservationEvent) -> str:
    """模块级便捷入口，语义与 :attr:`ObservationEvent.observation_fingerprint` 一致。"""
    return event.observation_fingerprint


def event_from_provider_result(
    result: Any,
    *,
    code: Any,
    session: Any,
    recorded_at: Any,
    ingestion_run_id: Any,
) -> ObservationEvent:
    """把一次 :class:`TI.ProviderResult` 转成观察事件。

    三态**全部记录**：``evidence`` / ``unknown`` / ``error``。因为：

    * "从未调用 provider" 与 "调用了、provider 明确返回 unknown" 不是同一件事；
    * "provider error" 不能被降级成 "never observed"。

    只有 ``evidence`` 才带 ``evidence_fingerprint``；``error`` 带规范化后的错误身份。
    """
    code_text = _text(code)
    session_text = _canonical_session(session)
    provider_id = _text(getattr(result, "provider_id", None))
    provider_version = _text(getattr(result, "provider_version", None))
    status = _text(getattr(result, "status", None))
    moment = _canonical_instant(recorded_at)
    run_text = _text(ingestion_run_id)

    if code_text is None:
        raise ObservationError("观察事件缺少 code")
    if session_text is None:
        raise ObservationError(f"观察事件缺少可解析的 session: {code_text}")
    if provider_id is None or provider_version is None:
        raise ObservationError(f"观察事件缺少 provider 身份: {code_text} {session_text}")
    if status not in OBSERVED_STATUSES:
        raise ObservationError(f"观察事件状态非法: {status!r}")
    if moment is None:
        raise ObservationError(
            f"观察事件缺少可解析的 recorded_at: {code_text} {session_text}"
        )
    if run_text is None:
        raise ObservationError(f"观察事件缺少 ingestion_run_id: {code_text} {session_text}")

    evidence_fingerprint = None
    error_class = error_fingerprint = error_diagnostic = None

    if status == OBSERVED_EVIDENCE:
        evidence = getattr(result, "evidence", None) or {}
        if not isinstance(evidence, Mapping) or not evidence:
            # "evidence" 状态却没有任何字段：这是 provider 的契约违规，不是事实。
            raise ObservationError(
                f"evidence 观察缺少字段: {code_text} {session_text} {provider_id}"
            )
        evidence_fingerprint = _sha256(
            {"version": FINGERPRINT_VERSION, "evidence": dict(evidence)}
        )
    elif status == OBSERVED_ERROR:
        error_class, error_diagnostic = normalize_error_identity(
            getattr(result, "error", None)
        )
        error_fingerprint = _sha256(
            {"version": FINGERPRINT_VERSION, "error_class": error_class}
        )

    return ObservationEvent(
        code=code_text,
        session_date=session_text,
        provider_id=provider_id,
        provider_version=provider_version,
        provider_status=status,
        source_observed_kind=_text(getattr(result, "observed_kind", None)) or _default_observed_kind(),
        source_observed_at=_canonical_instant(getattr(result, "observed_at", None)),
        effective_at=_canonical_instant(getattr(result, "effective_at", None)),
        recorded_at=moment,
        ingestion_run_id=run_text,
        evidence_fingerprint=evidence_fingerprint,
        error_class=error_class,
        error_fingerprint=error_fingerprint,
        error_diagnostic=error_diagnostic,
    )


# ─────────────────────────────── 知识状态 ───────────────────────────────


@dataclass(frozen=True, slots=True)
class ObservationKnowledge:
    """截至 ``validation_as_of``，我们对一个 pair 的**观察知识**。

    **只做诊断**：不含 ``can_buy`` / ``can_sell``，也不参与任何判定。

    ``market_provable_at_decision``
        从"**市场事实**当时是否已公开"的角度看，``decision_at`` 当时是否可证明。
        判据沿用事实层既有 contract：``source_observed_at <= decision_at`` **且**
        ``effective_at <= decision_at``。这是 Historical Tradability Archive 的事实目标。
    ``system_possessed_at_decision``
        从"**我们的模拟系统**当时是否实际拥有它"的角度看：``recorded_at <= decision_at``。
        这是数据管线成熟度指标。

    两者刻意**不揉成一个 boolean**：一条 ``source_observed_at`` 早于 decision 但今天才
    被我们抓到的记录，市场层面可证明、系统层面则不然。把它们混起来会让人误以为
    "系统当时知道"，或反过来把一条真实的历史公开事实判成不可证明。
    """

    code: str
    session_date: str
    validation_as_of: Optional[str]

    first_seen_at: Optional[str]
    last_seen_at: Optional[str]

    provider_outcomes: Mapping[str, int] = field(default_factory=dict)
    provider_ids: tuple = ()

    evidence_seen: bool = False
    first_evidence_seen_at: Optional[str] = None

    provable_at_decision: bool = False
    late_observed: bool = False
    never_observed: bool = False
    legacy_observation_unknown: bool = False

    #: archive 侧的**行级** provenance 对账结果（见 :class:`ArchiveObservationCoverage`）。
    #: 这三个字段是 ``legacy_observation_unknown`` 的唯一依据：它问的是"有没有哪条
    #: archive 行缺少写入时固定的 provenance 链接"，而不是"这条 pair 有没有 ledger 事件"。
    archive_row_count: int = 0
    archive_rows_with_observation_provenance: int = 0
    archive_rows_without_observation_provenance: int = 0

    market_provable_at_decision: bool = False
    system_possessed_at_decision: bool = False

    observation_count: int = 0
    provider_count: int = 0

    fingerprint: str = ""

    @property
    def has_any_observation(self) -> bool:
        return self.observation_count > 0

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "session_date": self.session_date,
            "validation_as_of": self.validation_as_of,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "provider_outcomes": dict(sorted(self.provider_outcomes.items())),
            "provider_ids": list(self.provider_ids),
            "evidence_seen": self.evidence_seen,
            "first_evidence_seen_at": self.first_evidence_seen_at,
            "provable_at_decision": self.provable_at_decision,
            "late_observed": self.late_observed,
            "never_observed": self.never_observed,
            "legacy_observation_unknown": self.legacy_observation_unknown,
            "archive_row_count": self.archive_row_count,
            "archive_rows_with_observation_provenance": (
                self.archive_rows_with_observation_provenance
            ),
            "archive_rows_without_observation_provenance": (
                self.archive_rows_without_observation_provenance
            ),
            "market_provable_at_decision": self.market_provable_at_decision,
            "system_possessed_at_decision": self.system_possessed_at_decision,
            "observation_count": self.observation_count,
            "provider_count": self.provider_count,
            "fingerprint": self.fingerprint,
            "contract_version": CONTRACT_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ArchiveObservationCoverage:
    """archive 事实行的**行级** observation provenance 对账结果。

    这是"某条 archive 行当年是不是和某次观察一起落库"的**唯一权威实现**：调用方不得
    自己写一套 ``covered = ...`` / ``legacy = ...``（多套实现必然漂移，而漂移的正是
    issue #161 要修的诊断）。

    口径刻意基于 **distinct archive row**，不是事件数、不是 provider 数：

    * 一条 archive 行被 5 个后来的 observation event 重复观察到，它仍然只是 **1 条
      被覆盖的行**——``archive_rows_with_observation_provenance <= archive_row_count``
      恒成立，绝不会出现"5 个事件减 1 条事实 = -4"这种反向计数；
    * 反过来，一条 pre-ledger 行**不会**因为今天又看到同内容证据而被算作有 provenance
      ——"内容相同"与"写入时就有链接"是两件事（前者连 fingerprint 都可以相同）。

    ``legacy_observation_unknown`` 的依据是"存在**没有** provenance 链接的 archive 行"，
    而不是"这条 pair 的 ledger 事件数为 0"：mixed pair（既有 legacy 行、又有 ledger-era
    行）因此必须被如实报出来，而不是被 pair 上任何一条事件掩盖。
    """

    archive_row_count: int = 0
    covered_row_count: int = 0
    uncovered_row_count: int = 0

    @property
    def has_uncovered_rows(self) -> bool:
        """是否存在缺少写入时 provenance 的 archive 行（= 真正的 legacy 行）。"""
        return self.uncovered_row_count > 0

    def to_dict(self) -> dict:
        return {
            "archive_row_count": self.archive_row_count,
            "covered_row_count": self.covered_row_count,
            "uncovered_row_count": self.uncovered_row_count,
            "has_uncovered_rows": self.has_uncovered_rows,
            "contract_version": CONTRACT_VERSION,
        }


def _archive_row_identity(item: Any) -> Optional[tuple]:
    """archive 事实行的**身份**：``(code, session_date, effective_at, observed_at)``。

    这正是 archive 表的唯一键（见 ``tradability_archive.ensure_schema`` 的
    ``UNIQUE(code, session_date, effective_at, observed_at)``）。provenance 链接必须
    锚在这个身份上，而不是锚在 fingerprint 上——两边 fingerprint 属于不同的哈希域
    （``sha256-canonical-tradability-v1`` vs ``sha256-canonical-observation-v1``），
    直接比较只会得到"看起来相等"的假象。

    接受映射（``code`` / ``session_date`` / ``effective_at`` / ``observed_at`` 键）或
    等价四元组；身份不完整时返回 ``None``（调用方据此拒绝登记，而不是写一条残缺链接）。
    """
    if item is None:
        return None

    def _from(get: Any) -> Optional[tuple]:
        code = _text(get("code"))
        session = _canonical_session(get("session_date"))
        effective_at = _canonical_instant(get("effective_at"))
        observed_at = _canonical_instant(get("observed_at"))
        if code is None or session is None or effective_at is None or observed_at is None:
            return None
        return (code, session, effective_at, observed_at)

    if isinstance(item, Mapping):
        return _from(item.get)
    if hasattr(item, "keys") and hasattr(item, "__getitem__"):
        # ``sqlite3.Row`` 等行对象：既不是 ``Mapping`` 也不支持属性访问，但支持按名索引。
        # 漏掉这一类会让链接读回来时身份解析成 ``None``，对账恒为"未覆盖"——那正好把
        # 修复本身变成新的假阴性。
        return _from(lambda key: item[key] if key in item.keys() else None)
    if hasattr(item, "code") and hasattr(item, "session_date"):
        # ``TradabilityEvidence`` 等事实对象：按属性取身份。
        return _from(lambda key: getattr(item, key, None))
    try:
        code, session, effective_at, observed_at = item
    except (TypeError, ValueError):
        return None
    return _from(
        lambda key: {
            "code": code,
            "session_date": session,
            "effective_at": effective_at,
            "observed_at": observed_at,
        }.get(key)
    )


def reconcile_archive_rows(
    archive_rows: Optional[Sequence[Any]],
    links: Optional[Sequence[Any]],
) -> ArchiveObservationCoverage:
    """按**行级** provenance 对账 archive 事实行与 observation 链接。

    ``archive_rows`` 是 archive 侧的事实行身份；``links`` 是
    :data:`ARCHIVE_LINK_TABLE` 里的 provenance 链接。两者都接受映射（含
    ``code`` / ``session_date`` / ``effective_at`` / ``observed_at`` 键）或等价的
    四元组序列。

    判据是**身份相等**（四元组逐一匹配），不是计数相减、也不是 fingerprint 相等：
    只有链接表里真的存在指向这条行的记录，才说明它的写入与某次观察同处一个
    ingestion transaction。

    一条行被多条链接指向（同一次 run 的多个 provider 都观察到它）仍然只算 **1 条
    被覆盖的行**——用集合去重，而不是把链接条数当覆盖数。
    """
    distinct_rows = {
        identity
        for identity in (_archive_row_identity(row) for row in (archive_rows or ()))
        if identity is not None
    }
    linked = {
        identity
        for identity in (_archive_row_identity(link) for link in (links or ()))
        if identity is not None
    }
    covered = len(distinct_rows & linked)
    total = len(distinct_rows)
    return ArchiveObservationCoverage(
        archive_row_count=total,
        covered_row_count=covered,
        uncovered_row_count=total - covered,
    )


@dataclass(frozen=True, slots=True)
class ObservationCoverage:
    """Observation coverage 指标。

    分母**必须**是 ``requested code-session pairs``，绝不是 provider 行数或 observation
    event 数量：一个 pair 可以有 10 个事件、3 个 provider，coverage 里仍然只是 **1 个 pair**。
    event count 单独报告为 ``observation_events``。
    """

    requested_pairs: int = 0
    observed_pairs: int = 0
    never_observed_pairs: int = 0

    evidence_observed_pairs: int = 0
    unknown_only_pairs: int = 0
    error_only_pairs: int = 0
    legacy_observation_unknown_pairs: int = 0

    #: 缺 provenance 的 archive **事实行**数（不是 pair 数）。与
    #: ``legacy_observation_unknown_pairs`` 刻意分开：一个 pair 可以含多行 legacy 事实，
    #: 混用 pair count 与 row count 会同时高估 pair 数、低估行数。
    legacy_archive_rows: int = 0

    late_evidence_pairs: int = 0
    system_possessed_at_decision: int = 0
    market_provable_at_decision: int = 0

    observation_events: int = 0

    observed_ratio: Optional[float] = None
    evidence_ratio: Optional[float] = None
    late_evidence_ratio: Optional[float] = None
    system_possessed_ratio: Optional[float] = None
    market_provable_ratio: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "requested_pairs": self.requested_pairs,
            "observed_pairs": self.observed_pairs,
            "never_observed_pairs": self.never_observed_pairs,
            "evidence_observed_pairs": self.evidence_observed_pairs,
            "unknown_only_pairs": self.unknown_only_pairs,
            "error_only_pairs": self.error_only_pairs,
            "legacy_observation_unknown_pairs": self.legacy_observation_unknown_pairs,
            "legacy_archive_rows": self.legacy_archive_rows,
            "late_evidence_pairs": self.late_evidence_pairs,
            "system_possessed_at_decision": self.system_possessed_at_decision,
            "market_provable_at_decision": self.market_provable_at_decision,
            "observation_events": self.observation_events,
            "observed_ratio": self.observed_ratio,
            "evidence_ratio": self.evidence_ratio,
            "late_evidence_ratio": self.late_evidence_ratio,
            "system_possessed_ratio": self.system_possessed_ratio,
            "market_provable_ratio": self.market_provable_ratio,
            "contract_version": CONTRACT_VERSION,
        }


# ─────────────────────────────── Schema ───────────────────────────────


def ensure_ledger_schema(conn: sqlite3.Connection) -> dict:
    """正式 migration 015 的建表函数（幂等）。**只新增**，不改既有表、不回填历史行。"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            session_date TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            provider_version TEXT NOT NULL,
            provider_status TEXT NOT NULL,
            source_observed_kind TEXT NOT NULL,
            source_observed_at TEXT,
            effective_at TEXT,
            recorded_at TEXT NOT NULL,
            ingestion_run_id TEXT NOT NULL,
            evidence_fingerprint TEXT,
            observation_fingerprint TEXT NOT NULL,
            error_class TEXT,
            error_fingerprint TEXT,
            error_diagnostic TEXT,
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(ingestion_run_id, code, session_date, provider_id, observation_fingerprint)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_pair "
        f"ON {LEDGER_TABLE}(code, session_date, recorded_at)"
    )
    # 链接表与台账**同属一个 schema**：没有它，行级 provenance 对账就无法进行，任何
    # 建了台账却缺链接表的库都会在诊断时直接报 "no such table"。两者必须一起存在，
    # 因此这里一并确保（migration 017 仍保留，供已停在 v16 的既有库沿版本链升级）。
    links = ensure_archive_link_schema(conn)
    return {"table": LEDGER_TABLE, "migration": MIGRATION_DESCRIPTION,
            "archive_observation_links": links}


#: provenance 链接表的列序（显式列清单）。
ARCHIVE_LINK_COLUMNS = (
    "id", "code", "session_date", "effective_at", "observed_at",
    "ingestion_run_id", "provider_id", "observation_fingerprint",
    "recorded_at", "contract_version", "created_at",
)


def ensure_archive_link_schema(conn: sqlite3.Connection) -> dict:
    """正式 migration 017 的建表函数（幂等）。**append-only** 行级 provenance 链接。

    这张表回答的问题**只有一个**：

        这条 archive 事实行，是不是由某次 ingestion run 在写入它的同一个
        transaction 里、连同某个 observation event 一起产生的？

    因此它的身份是 ``(archive 行身份, ingestion_run_id, provider_id,
    observation_fingerprint)``——四个部分缺一不可：

    * 只记 ``ingestion_run_id`` 不够：一次 run 会产生多行事实与多个事件；
    * 只记 ``observation_fingerprint`` 不够：那只能证明"内容相同"，而一条 pre-ledger
      行后来被重新观察到同内容证据时也会得到同一个指纹（见 issue #161 §五/§十三）。

    它**不是**第二套判定层，也不含任何 ``can_*``：链接只说明"这条事实行当年是和哪次
    观察一起落库的"，绝不决定任何订单能不能执行。

    **绝不回填历史行**：升级前的 archive 行没有这种链接，那就是"原始观察时间不可知"
    ——诚实答案是 ``legacy_observation_unknown``，而不是编一条假链接。旧数据不得伪造
    link，本表只为**未来**的 ingestion 建立可靠 provenance。
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ARCHIVE_LINK_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            session_date TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            ingestion_run_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            observation_fingerprint TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(code, session_date, effective_at, observed_at,
                   ingestion_run_id, provider_id, observation_fingerprint)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{ARCHIVE_LINK_TABLE}_pair "
        f"ON {ARCHIVE_LINK_TABLE}(code, session_date)"
    )
    return {"table": ARCHIVE_LINK_TABLE, "migration": MIGRATION_DESCRIPTION_LINKS}


#: 查询列序（显式列清单，不依赖 ``SELECT *`` 的顺序）。
LEDGER_COLUMNS = (
    "id", "code", "session_date", "provider_id", "provider_version",
    "provider_status", "source_observed_kind", "source_observed_at", "effective_at",
    "recorded_at", "ingestion_run_id", "evidence_fingerprint",
    "observation_fingerprint", "error_class", "error_fingerprint",
    "error_diagnostic", "contract_version", "created_at",
)


def _row_mapping(row: Any) -> Optional[dict]:
    """把一行按 :data:`LEDGER_COLUMNS` 对齐。

    ``sqlite3.Row`` 与默认连接的 ``tuple`` 都要支持——只支持一种形态的读法会让另一种
    调用方看到"整表都是空的"。
    """
    if row is None:
        return None
    if isinstance(row, Mapping):
        return {name: row.get(name) for name in LEDGER_COLUMNS}
    if hasattr(row, "keys"):
        return {name: row[name] for name in LEDGER_COLUMNS}
    return dict(zip(LEDGER_COLUMNS, row, strict=False))


# ─────────────────────────────── Repository ───────────────────────────────


class ObservationLedgerRepository:
    """Observation Ledger 的**唯一**写入口与 PIT 查询入口。

    Provider **不得**直接写库；其它模块也不得自行 ``INSERT INTO``（架构 guard 用 AST /
    SQL 字符串扫描锁死这一点）。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def ensure_schema(self) -> dict:
        return ensure_ledger_schema(self._conn)

    def ensure_archive_link_schema(self) -> dict:
        """确保行级 provenance 链接表存在（幂等）。"""
        return ensure_archive_link_schema(self._conn)

    # ── 写（append-only） ──
    def append(self, event: ObservationEvent) -> bool:
        """追加一条观察事件。返回是否**新插入**。

        幂等键是 ``(ingestion_run_id, code, session_date, provider_id,
        observation_fingerprint)``：

        * **同一次 run** 的同内容观察重放 → 唯一键命中，no-op（不重复插事件）；
        * **不同 run** 观察到同一内容 → 允许形成**新的**事件，因为这证明系统在另一个
          时点又观察到了该事实（``recorded_at`` 不同，是新的一次观察）。

        **永不 UPDATE 既有事件**：历史观察是已经发生过的事实。
        """
        row = event.to_row()
        row["created_at"] = now_utc()
        columns = ", ".join(LEDGER_COLUMNS[1:-1])  # 去掉 id 与 created_at
        placeholders = ", ".join(f":{name}" for name in LEDGER_COLUMNS[1:-1])
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO {LEDGER_TABLE}({columns}, created_at) "
            f"VALUES({placeholders}, :created_at)",
            row,
        )
        return bool(cursor.rowcount)

    def append_many(self, events: Sequence[ObservationEvent]) -> int:
        return sum(1 for event in events if self.append(event))

    # ── 写：archive 行级 provenance 链接（append-only） ──
    def link_archive_row(
        self,
        evidence: Any,
        *,
        ingestion_run_id: Any,
        provider_id: Any,
        observation_fingerprint: Any,
        recorded_at: Any,
    ) -> bool:
        """为一条**本次新插入**的 archive 事实行登记 provenance 链接。

        调用方（摄取写路径）必须在**同一个 transaction** 内、且只在这条事实行确实是
        本次 run 新插入时调用它。返回是否新插入（重复登记是 no-op）。

        ``ingestion_run_id`` + ``observation_fingerprint`` 一起给出"这条事实行当年是
        和哪次观察一起落库的"证据；单独任一个都不足以证明 provenance。
        """
        identity = _archive_row_identity(evidence)
        if identity is None:
            raise ObservationError(f"archive 行身份不完整，无法登记 provenance: {evidence!r}")
        run_text = _text(ingestion_run_id)
        provider_text = _text(provider_id)
        fingerprint_text = _text(observation_fingerprint)
        moment = _canonical_instant(recorded_at)
        if run_text is None or provider_text is None or fingerprint_text is None:
            raise ObservationError("provenance 链接缺少 ingestion_run_id / provider / 指纹")
        if moment is None:
            raise ObservationError("provenance 链接缺少可解析的 recorded_at")
        cursor = self._conn.execute(
            f"""INSERT OR IGNORE INTO {ARCHIVE_LINK_TABLE}(
                    code, session_date, effective_at, observed_at,
                    ingestion_run_id, provider_id, observation_fingerprint,
                    recorded_at, contract_version, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                identity[0], identity[1], identity[2], identity[3],
                run_text, provider_text, fingerprint_text,
                moment, CONTRACT_VERSION, now_utc(),
            ),
        )
        return bool(cursor.rowcount)

    def append_links(self, links: Sequence[Mapping[str, Any]]) -> int:
        """批量登记 provenance 链接（每项须含 ``evidence`` 与链接字段）。"""
        written = 0
        for link in links:
            if self.link_archive_row(
                link.get("evidence"),
                ingestion_run_id=link.get("ingestion_run_id"),
                provider_id=link.get("provider_id"),
                observation_fingerprint=link.get("observation_fingerprint"),
                recorded_at=link.get("recorded_at"),
            ):
                written += 1
        return written

    # ── 读：provenance 链接与行级对账 ──
    def archive_links(self, code: Any = None, session: Any = None) -> list:
        """该 ``(code, session)`` 的 provenance 链接。

        **刻意不按 ``recorded_at`` 过滤**：链接说明的是 archive 行**写入时**的结构性
        provenance 类型，不是"截至某时点我们知道什么"。用它做 PIT 过滤会把一条确定是
        ledger-era 的行错标成 legacy（见 issue #161 §十四）。
        """
        clauses: list = []
        params: list = []
        code_text = _text(code)
        if code_text is not None:
            clauses.append("code=?")
            params.append(code_text)
        session_text = _canonical_session(session)
        if session_text is not None:
            clauses.append("session_date=?")
            params.append(session_text)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._conn.execute(
            f"SELECT {', '.join(ARCHIVE_LINK_COLUMNS)} FROM {ARCHIVE_LINK_TABLE}{where}",
            params,
        ).fetchall()

    def reconcile_archive_coverage(
        self, code: Any, session: Any, archive_rows: Optional[Sequence[Any]] = None
    ) -> ArchiveObservationCoverage:
        """按行级 provenance 对账该 pair 的 archive 事实行。

        传入 ``archive_rows`` 时按调用方给出的行身份对账；未传入时按本连接可见的
        archive 链接能覆盖的行来报告（``archive_row_count`` 等于被链接的行数，因此
        ``uncovered`` 为 0——**没有行身份就无法声称某行缺 provenance**，绝不凭空
        判 legacy）。
        """
        links = self.archive_links(code, session)
        if archive_rows is None:
            linked = {
                _archive_row_identity(link)
                for link in links
            }
            linked.discard(None)
            return ArchiveObservationCoverage(
                archive_row_count=len(linked),
                covered_row_count=len(linked),
                uncovered_row_count=0,
            )
        return reconcile_archive_rows(archive_rows, links)

    # ── 读（全部 PIT） ──
    def events(
        self,
        code: Any = None,
        session: Any = None,
        *,
        as_of: Any = None,
        run_id: Any = None,
    ) -> list:
        """观察事件（按 ``recorded_at`` 排序）。

        ``as_of`` 给出时**必须**显式过滤 ``recorded_at <= as_of``——否则就是拿今天的
        数据解释过去。
        """
        clauses: list = []
        params: list = []
        code_text = _text(code)
        if code_text is not None:
            clauses.append("code=?")
            params.append(code_text)
        session_text = _canonical_session(session)
        if session is not None and session_text is None:
            return []
        if session_text is not None:
            clauses.append("session_date=?")
            params.append(session_text)
        run_text = _text(run_id)
        if run_text is not None:
            clauses.append("ingestion_run_id=?")
            params.append(run_text)
        if as_of is not None:
            moment = _canonical_instant(as_of)
            if moment is None:
                # 给了 as_of 却无法解析：不能"当作没给"而看到全部未来数据。
                return []
            clauses.append("recorded_at<=?")
            params.append(moment)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            cursor = self._conn.execute(
                f"SELECT {', '.join(LEDGER_COLUMNS)} FROM {LEDGER_TABLE}{where} "
                "ORDER BY recorded_at, id",
                tuple(params),
            )
        except sqlite3.OperationalError:
            return []  # 表还不存在 = 没有任何观察
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=False)) for row in cursor.fetchall()]

    def count(self, code: Any = None, session: Any = None) -> int:
        try:
            if code is None and session is None:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM {LEDGER_TABLE}"
                ).fetchone()
            else:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM {LEDGER_TABLE} WHERE code=? AND session_date=?",
                    (str(code), str(session)[:10]),
                ).fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    # ── 权威查询 API ──
    def first_observation(
        self,
        code: Any,
        session: Any,
        *,
        as_of: Any = None,
        require_evidence: bool = False,
    ) -> Optional[dict]:
        """**第一次**观察事件（``as_of`` 给出时只看到 ``recorded_at <= as_of``）。

        ``require_evidence=True`` 时只考虑 ``provider_status == evidence`` 的事件
        （"第一次看到**事实**"而不是"第一次尝试观察"）。
        """
        rows = self.events(code, session, as_of=as_of)
        for row in rows:
            if require_evidence and row.get("provider_status") != OBSERVED_EVIDENCE:
                continue
            return row
        return None

    def knowledge_at(
        self,
        code: Any,
        session: Any,
        *,
        validation_as_of: Any = None,
        decision_at: Any = None,
        archive_rows: Optional[Sequence[Any]] = None,
    ) -> ObservationKnowledge:
        """截至 ``validation_as_of`` 的观察知识（PIT）。

        ``decision_at`` 给出时同时报告 ``market_provable_at_decision``（事实层口径：
        ``source_observed_at`` 与 ``effective_at`` 都不晚于 decision）与
        ``system_possessed_at_decision``（系统口径：``recorded_at <= decision_at``）。

        ``archive_rows`` 是调用方给出的**该 pair 的 archive 事实行身份**，用来做
        **行级** provenance 对账：哪些行当年是与某次观察一起落库的（有链接），哪些行
        没有（真正的升级前历史数据）。**必须**传行身份而不是一个 ``bool``——一个
        "这条 pair 有没有事实行"的布尔无法表达"2 行里 1 行是 legacy"，正是 issue #161
        的第二个 bug。

        判据是行级对账结果，**不是**"ledger 里有没有事件"：

        * 一条 ledger-era 行即使在某历史知识时点看不到观察事件（``recorded_at`` 晚于
          ``validation_as_of``），它的 provenance 类型依然是 ledger-era，因此
          ``legacy_observation_unknown`` 为假——它当时只是"不可见"，不是"从来不知道"；
        * 一条真正的 pre-ledger 行不会因为今天又观察到同内容证据而被洗白成有 provenance；
        * mixed pair 里只要有**一行**缺 provenance，``legacy_observation_unknown`` 就是真。

        ``first_seen_at`` 仍只来自真正的 ledger events；历史行没有可证明的首次观察时间，
        就诚实地说不知道——**绝不**把 ``archive.created_at`` 或 ``session_date`` 伪装成
        first_seen。
        """
        code_text = _text(code) or ""
        session_text = _canonical_session(session) or ""
        as_of_text = _canonical_instant(validation_as_of) if validation_as_of is not None else None
        if validation_as_of is not None and as_of_text is None:
            # 无法解析的 validation_as_of 不得退化成"看全部"。
            raise ObservationError(f"非法 validation_as_of: {validation_as_of!r}")

        rows = self.events(code_text, session_text, as_of=as_of_text)
        decision_text = _canonical_instant(decision_at) if decision_at is not None else None

        outcomes: dict = {}
        provider_ids = set()
        recorded: list = []
        evidence_recorded: list = []
        for row in rows:
            status = row.get("provider_status") or ""
            outcomes[status] = outcomes.get(status, 0) + 1
            provider_id = row.get("provider_id")
            if provider_id:
                provider_ids.add(provider_id)
            moment = _canonical_instant(row.get("recorded_at"))
            if moment is not None:
                recorded.append(moment)
            if status == OBSERVED_EVIDENCE and moment is not None:
                evidence_recorded.append(moment)

        first_seen = min(recorded) if recorded else None
        last_seen = max(recorded) if recorded else None
        first_evidence = min(evidence_recorded) if evidence_recorded else None
        evidence_seen = bool(evidence_recorded)

        market_provable = False
        system_possessed = False
        provable = False
        late = False
        if decision_text is not None and evidence_seen:
            # 市场口径：**事实层既有 contract**，不由 ledger 重写。
            market_provable = all(
                _not_after(row.get("source_observed_at"), decision_text)
                and _not_after(row.get("effective_at"), decision_text)
                for row in rows
                if row.get("provider_status") == OBSERVED_EVIDENCE
            )
            # 系统口径：我们当时是否已经拥有这份证据。
            system_possessed = bool(first_evidence) and first_evidence <= decision_text
            provable = market_provable
            late = not system_possessed

        never = not rows

        # ── 行级 provenance 对账（issue #161 的核心修复） ──
        #
        # 旧逻辑是 ``legacy = never and bool(archive_has_row)``：它问的是"这条 pair 有
        # 没有 ledger 事件"，于是把两件不同的事混成一件——(1) 一条 ledger-era 行在某历史
        # 知识时点看不到事件（它只是不可见），(2) 一条真正的升级前历史行。前者被误报成
        # legacy，就是生产环境里 24 行新摄取事实全被标 legacy 的假诊断；后者在 mixed pair
        # 里又会被同 pair 的其它事件掩盖。
        #
        # 现在按**行**对账：调用方给出该 pair 的 archive 行身份，本仓储用写入时固定的
        # provenance 链接逐行判断。没有行身份时无法声称任何一行缺 provenance，因此
        # 不判 legacy（fail closed：宁可不说，也不凭空指控历史数据）。
        archive_coverage = (
            self.reconcile_archive_coverage(code_text, session_text, archive_rows)
            if archive_rows is not None
            else ArchiveObservationCoverage()
        )
        legacy = archive_coverage.has_uncovered_rows

        knowledge = ObservationKnowledge(
            code=code_text,
            session_date=session_text,
            validation_as_of=as_of_text,
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            provider_outcomes=dict(sorted(outcomes.items())),
            provider_ids=tuple(sorted(provider_ids)),
            evidence_seen=evidence_seen,
            first_evidence_seen_at=first_evidence,
            provable_at_decision=provable,
            late_observed=late,
            never_observed=never,
            legacy_observation_unknown=legacy,
            archive_row_count=archive_coverage.archive_row_count,
            archive_rows_with_observation_provenance=(
                archive_coverage.covered_row_count
            ),
            archive_rows_without_observation_provenance=(
                archive_coverage.uncovered_row_count
            ),
            market_provable_at_decision=market_provable,
            system_possessed_at_decision=system_possessed,
            observation_count=len(rows),
            provider_count=len(provider_ids),
        )
        payload = knowledge.to_dict()
        payload.pop("fingerprint", None)
        return ObservationKnowledge(
            **{
                name: getattr(knowledge, name)
                for name in (
                    "code", "session_date", "validation_as_of", "first_seen_at",
                    "last_seen_at", "provider_outcomes", "provider_ids", "evidence_seen",
                    "first_evidence_seen_at", "provable_at_decision", "late_observed",
                    "never_observed", "legacy_observation_unknown",
                    "archive_row_count", "archive_rows_with_observation_provenance",
                    "archive_rows_without_observation_provenance",
                    "market_provable_at_decision", "system_possessed_at_decision",
                    "observation_count", "provider_count",
                )
            },
            fingerprint=_sha256(payload),
        )


def _not_after(value: Any, reference: str) -> bool:
    """``value`` 是否**不晚于** ``reference``。

    缺失时返回 ``False``：一条没有 ``source_observed_at`` 的记录**不能**证明"当时已
    公开"——不知道不等于可证明（fail closed，与事实层一致）。
    """
    moment = _canonical_instant(value)
    if moment is None:
        return False
    return moment <= reference


# ─────────────────────────────── Coverage ───────────────────────────────


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if not denominator:
        return None
    return numerator / denominator


def coverage(
    repository: ObservationLedgerRepository,
    pairs: Sequence[tuple],
    *,
    validation_as_of: Any = None,
    decision_at: Any = None,
    archive_rows_by_pair: Optional[Mapping[tuple, Sequence[Any]]] = None,
) -> ObservationCoverage:
    """按 **requested code-session pairs** 统计 observation coverage。

    ``pairs`` 里的每个 ``(code, session)`` 只计 **1 个 pair**，无论它有多少个观察事件、
    多少个 provider。事件数单独报告为 ``observation_events``。

    ``archive_rows_by_pair`` 把每个 ``(code, session)`` 映射到它的 archive **事实行身份
    列表**，用来做行级 provenance 对账：

    * ``legacy_observation_unknown_pairs`` 仍然按 **distinct code-session pair** 计数
      ——一个 pair 里哪怕有 3 条 legacy 行，它也只是 **1 个 pair**；
    * 行数单独报告为 ``legacy_archive_rows``，绝不与 pair count 混为一谈。

    ``legacy`` 的判据是"这个 pair 存在**缺少写入时 provenance** 的 archive 行"，不是
    "这个 pair 的 ledger 事件数为 0"。
    """
    as_of_text = _canonical_instant(validation_as_of) if validation_as_of is not None else None
    if validation_as_of is not None and as_of_text is None:
        raise ObservationError(f"非法 validation_as_of: {validation_as_of!r}")
    decision_text = _canonical_instant(decision_at) if decision_at is not None else None

    archive_rows_map = {
        (_text(code) or "", _canonical_session(session) or ""): rows
        for (code, session), rows in (archive_rows_by_pair or {}).items()
    }

    requested = observed = never = evidence_pairs = unknown_only = error_only = 0
    legacy_pairs = late = possessed = provable = 0
    legacy_rows = 0
    events_total = 0
    seen: set = set()

    for code, session in pairs:
        code_text = _text(code) or ""
        session_text = _canonical_session(session) or ""
        key = (code_text, session_text)
        if key in seen:
            # 同一个 pair 重复出现仍然只是 1 个 pair。
            continue
        seen.add(key)
        requested += 1

        # 行级对账与 PIT 可见性**互相独立**：provenance 类型是 archive 行自身的属性，
        # 不随 validation_as_of 变化（见 issue #161 §十四）。
        archive_rows = archive_rows_map.get(key)
        pair_coverage = (
            repository.reconcile_archive_coverage(code_text, session_text, archive_rows)
            if archive_rows is not None
            else None
        )
        if pair_coverage is not None and pair_coverage.has_uncovered_rows:
            legacy_pairs += 1
            legacy_rows += pair_coverage.uncovered_row_count

        rows = repository.events(code_text, session_text, as_of=as_of_text)
        events_total += len(rows)
        if not rows:
            never += 1
            continue

        observed += 1
        statuses = {row.get("provider_status") for row in rows}
        if OBSERVED_EVIDENCE in statuses:
            evidence_pairs += 1
        elif OBSERVED_UNKNOWN in statuses:
            unknown_only += 1
        else:
            error_only += 1

        if decision_text is None:
            continue
        evidence_rows = [r for r in rows if r.get("provider_status") == OBSERVED_EVIDENCE]
        if not evidence_rows:
            continue
        moments = [
            _canonical_instant(row.get("recorded_at")) for row in evidence_rows
        ]
        moments = [m for m in moments if m is not None]
        if not moments:
            continue
        first_evidence = min(moments)
        if first_evidence > decision_text:
            late += 1
        if first_evidence <= decision_text:
            possessed += 1
        if all(
            _not_after(row.get("source_observed_at"), decision_text)
            and _not_after(row.get("effective_at"), decision_text)
            for row in evidence_rows
        ):
            provable += 1

    return ObservationCoverage(
        requested_pairs=requested,
        observed_pairs=observed,
        never_observed_pairs=never,
        evidence_observed_pairs=evidence_pairs,
        unknown_only_pairs=unknown_only,
        error_only_pairs=error_only,
        legacy_observation_unknown_pairs=legacy_pairs,
        legacy_archive_rows=legacy_rows,
        late_evidence_pairs=late,
        system_possessed_at_decision=possessed,
        market_provable_at_decision=provable,
        observation_events=events_total,
        observed_ratio=_ratio(observed, requested),
        evidence_ratio=_ratio(evidence_pairs, requested),
        late_evidence_ratio=_ratio(late, evidence_pairs),
        system_possessed_ratio=_ratio(possessed, requested),
        market_provable_ratio=_ratio(provable, requested),
    )


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:  # pragma: no cover - 手工冒烟
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_ledger_schema(conn)
    repo = ObservationLedgerRepository(conn)
    assert repo.count() == 0
    knowledge = repo.knowledge_at("000001", "2024-01-10", decision_at="2024-01-10T15:00:00")
    assert knowledge.never_observed and not knowledge.evidence_seen
    assert knowledge.first_seen_at is None
    assert knowledge.fingerprint
    print("tradability_observation_ledger self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _self_check()
