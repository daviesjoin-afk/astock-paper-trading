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
LEDGER_TABLE = "tradability_observation_ledger"

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
    "normalize_error_identity",
    "observation_fingerprint",
    "ensure_ledger_schema",
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
            "market_provable_at_decision": self.market_provable_at_decision,
            "system_possessed_at_decision": self.system_possessed_at_decision,
            "observation_count": self.observation_count,
            "provider_count": self.provider_count,
            "fingerprint": self.fingerprint,
            "contract_version": CONTRACT_VERSION,
        }


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
    return {"table": LEDGER_TABLE, "migration": MIGRATION_DESCRIPTION}


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
        archive_has_row: bool = False,
    ) -> ObservationKnowledge:
        """截至 ``validation_as_of`` 的观察知识（PIT）。

        ``decision_at`` 给出时同时报告 ``market_provable_at_decision``（事实层口径：
        ``source_observed_at`` 与 ``effective_at`` 都不晚于 decision）与
        ``system_possessed_at_decision``（系统口径：``recorded_at <= decision_at``）。

        ``archive_has_row`` 让调用方声明"archive 里确实有这条 pair 的行"。当 ledger
        里**完全没有**该 pair 的事件、而 archive 有行时，那是升级前的历史数据：真实
        ``first_seen_at`` 无法反推，因此记 ``legacy_observation_unknown``，
        **绝不**把 ``archive.created_at`` 或 ``session_date`` 伪装成 first_seen。
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
        legacy = never and bool(archive_has_row)

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
    archive_pairs: Optional[Sequence[tuple]] = None,
) -> ObservationCoverage:
    """按 **requested code-session pairs** 统计 observation coverage。

    ``pairs`` 里的每个 ``(code, session)`` 只计 **1 个 pair**，无论它有多少个观察事件、
    多少个 provider。事件数单独报告为 ``observation_events``。

    ``archive_pairs`` 用于识别"archive 有行、ledger 无事件"的升级前历史数据
    （``legacy_observation_unknown_pairs``）。
    """
    as_of_text = _canonical_instant(validation_as_of) if validation_as_of is not None else None
    if validation_as_of is not None and as_of_text is None:
        raise ObservationError(f"非法 validation_as_of: {validation_as_of!r}")
    decision_text = _canonical_instant(decision_at) if decision_at is not None else None

    archive_set = {
        (_text(code) or "", _canonical_session(session) or "")
        for code, session in (archive_pairs or ())
    }

    requested = observed = never = evidence_pairs = unknown_only = error_only = 0
    legacy_pairs = late = possessed = provable = 0
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

        rows = repository.events(code_text, session_text, as_of=as_of_text)
        events_total += len(rows)
        if not rows:
            never += 1
            if key in archive_set:
                legacy_pairs += 1
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
