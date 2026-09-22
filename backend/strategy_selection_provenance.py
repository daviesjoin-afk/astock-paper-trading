"""R23 —— Strategy / Selection Provenance 的**纯契约**。

本模块回答一个历史问题：*这条 candidate / ranking / selection / signal 是哪个
immutable strategy version 产生的？*

它只负责三件事（规格 §6）：

1. **validation** —— 一个 provenance 必须同时具备 ``strategy_id`` / ``strategy_version`` /
   ``strategy_checksum`` / ``asof_day``，且 ``scope`` 决定 ``cycle_id`` 的存在性；
2. **canonical representation** —— ``checksum`` 为 64 位小写 hex、``version >= 1``、
   ``asof_day`` 为 ``YYYY-MM-DD``，以及由这些字段派生的稳定 run identity
   （:func:`run_provenance_key`）；
3. **status semantics** —— ``verified`` / ``unknown`` / ``legacy_unproven`` /
   ``not_applicable`` 的判定规则与「是否可作为权威」的唯一判据。

**它不做的事**（规格 §6 硬性要求，架构门禁静态锁定）：

* 不开数据库、不 import ``paper_trading``；
* 不读环境变量、不读墙上时钟（因此**不可能**出现 ``today`` / ``latest`` 回退）；
* 不解析 active cycle、不解析 current Registry head。

只依赖标准库。版本解析一律由 ``strategy_selection_resolver`` 通过既有
``strategy_registry`` / ``strategy_runtime`` authority 完成。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SCOPE_CYCLE", "SCOPE_RESEARCH", "SCOPES",
    "STATUS_VERIFIED", "STATUS_UNKNOWN", "STATUS_LEGACY_UNPROVEN", "STATUS_NOT_APPLICABLE",
    "PROVENANCE_STATUSES", "AUTHORITATIVE_STATUSES",
    "StrategySelectionProvenance", "ProvenanceReading", "AsOfUnprovable",
    "canonical_checksum", "canonical_day", "canonical_version",
    "is_authoritative_status", "reading_from_row", "run_provenance_key",
    "resolve_asof_day", "legacy_reading", "not_applicable_reading",
]

# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------

#: 属于一个明确的 paper cycle：``cycle_id`` **必需**。
SCOPE_CYCLE = "cycle"
#: 研究型 run，不属于任何 cycle：``cycle_id`` **必须为 None**。
#: ``None`` 表示「此 run 不属于 cycle」，绝不表示「不知道 cycle，所以猜 active cycle」。
SCOPE_RESEARCH = "research"
SCOPES = (SCOPE_CYCLE, SCOPE_RESEARCH)

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

#: 持久化的 immutable stamp 完整、合法，且可被解析回同一 version/checksum。
STATUS_VERIFIED = "verified"
#: 证据缺失、部分缺失或自相矛盾 —— **不可**作为权威，也不许被猜出来。
STATUS_UNKNOWN = "unknown"
#: 升级前只留下 ``strategy_id`` 的历史行：不可证明当时的 version/checksum。
#: 与 ``unknown`` 的区别是「原因已知」而不是「数据损坏」，但同样不可作为权威。
STATUS_LEGACY_UNPROVEN = "legacy_unproven"
#: 该 run 的「策略」轴在注册表中根本不存在（例如 family B 的模型族 id），
#: 因此 strategy provenance 是**不适用**，而不是「未知」。
#: 绝不为了对齐字段而编造一个 strategy_id。
STATUS_NOT_APPLICABLE = "not_applicable"

PROVENANCE_STATUSES = (
    STATUS_VERIFIED, STATUS_UNKNOWN, STATUS_LEGACY_UNPROVEN, STATUS_NOT_APPLICABLE,
)
#: 唯一可作为历史权威的状态集合。其余状态一律 fail closed。
AUTHORITATIVE_STATUSES = frozenset({STATUS_VERIFIED})

_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 字段名 —— 持久化列与 :meth:`StrategySelectionProvenance.from_row` 共用一份，
#: 避免「列名改了、读取还在用旧名字」这类静默漂移。
RUN_COLUMNS = (
    "strategy_id", "strategy_version", "strategy_checksum",
    "asof_day", "scope", "cycle_id",
)


class AsOfUnprovable(ValueError):
    """无法从显式输入与已声明日期中**唯一**确定 as-of 日。

    这是 fail-closed 信号：调用方必须把该 run 记为 ``unknown``，
    **不得**退回 ``today()`` / 最新因子日 / min-max 猜测。
    """


# ---------------------------------------------------------------------------
# canonicalisation（全部显式，不读时钟）
# ---------------------------------------------------------------------------


def canonical_checksum(value: Any) -> str | None:
    """Return the canonical 64-hex lowercase checksum, or ``None`` if unusable."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if _CHECKSUM_RE.fullmatch(text) else None


def canonical_version(value: Any) -> int | None:
    """Return ``version >= 1``, or ``None``. ``0`` / negative / ``latest`` are invalid."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def canonical_day(value: Any) -> str | None:
    """Return ``YYYY-MM-DD`` for an explicit date value, or ``None``.

    Accepts ``date`` / ``datetime`` / ISO string. It never fills in "today":
    an absent value stays absent, which is exactly the fail-closed answer.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:10]
    if not _DAY_RE.fullmatch(candidate):
        return None
    try:
        dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def canonical_cycle_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def is_authoritative_status(status: Any) -> bool:
    """``True`` only for a status that may back a historical claim."""
    return str(status or "") in AUTHORITATIVE_STATUSES


# ---------------------------------------------------------------------------
# the frozen contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategySelectionProvenance:
    """One immutable, self-contained answer to "which strategy version?"."""

    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    asof_day: str
    scope: str
    cycle_id: int | None = None

    def __post_init__(self) -> None:
        strategy_id = str(self.strategy_id or "").strip()
        if not strategy_id:
            raise ValueError("selection provenance requires a strategy id")
        object.__setattr__(self, "strategy_id", strategy_id)

        version = canonical_version(self.strategy_version)
        if version is None:
            raise ValueError("selection provenance requires strategy_version >= 1")
        object.__setattr__(self, "strategy_version", version)

        checksum = canonical_checksum(self.strategy_checksum)
        if checksum is None:
            raise ValueError("selection provenance requires a 64-hex lowercase checksum")
        object.__setattr__(self, "strategy_checksum", checksum)

        day = canonical_day(self.asof_day)
        if day is None:
            raise ValueError("selection provenance requires an explicit as-of day")
        object.__setattr__(self, "asof_day", day)

        scope = str(self.scope or "").strip()
        if scope not in SCOPES:
            raise ValueError(f"unknown selection provenance scope: {scope!r}")
        object.__setattr__(self, "scope", scope)

        cycle_id = canonical_cycle_id(self.cycle_id)
        if scope == SCOPE_CYCLE and cycle_id is None:
            raise ValueError("cycle scope requires an explicit cycle id")
        if scope == SCOPE_RESEARCH and cycle_id is not None:
            raise ValueError("research scope must not carry a cycle id")
        object.__setattr__(self, "cycle_id", cycle_id)

    # ---------- projections ----------

    @property
    def is_cycle_scoped(self) -> bool:
        return self.scope == SCOPE_CYCLE

    @property
    def status(self) -> str:
        return STATUS_VERIFIED

    @property
    def is_authoritative(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        """Persistable projection — the exact column set :data:`RUN_COLUMNS` names."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_checksum": self.strategy_checksum,
            "asof_day": self.asof_day,
            "scope": self.scope,
            "cycle_id": self.cycle_id,
        }

    def identity(self) -> tuple:
        """Fields that make two runs the *same* evidence."""
        return (self.strategy_id, self.strategy_version, self.strategy_checksum,
                self.asof_day, self.scope, self.cycle_id)


@dataclass(frozen=True)
class ProvenanceReading:
    """A read-back answer plus the reason it is (or is not) authoritative.

    ``provenance is None`` is the only representation of "we cannot prove it";
    a reading never carries a guessed value.
    """

    provenance: StrategySelectionProvenance | None
    status: str
    detail: str = ""
    subject: str = ""

    def __post_init__(self) -> None:
        status = str(self.status or "").strip()
        if status not in PROVENANCE_STATUSES:
            raise ValueError(f"unknown provenance status: {status!r}")
        object.__setattr__(self, "status", status)
        if status == STATUS_VERIFIED and self.provenance is None:
            raise ValueError("verified provenance requires a resolved contract")
        if status != STATUS_VERIFIED and self.provenance is not None:
            raise ValueError("non-verified provenance must not carry a contract")

    @property
    def is_authoritative(self) -> bool:
        return is_authoritative_status(self.status)

    def require(self) -> StrategySelectionProvenance:
        """Return the contract or fail closed — never a default."""
        if self.provenance is None:
            raise AsOfUnprovable(
                f"selection provenance is {self.status}: {self.detail or 'no evidence'}"
            )
        return self.provenance

    def to_dict(self) -> dict[str, Any]:
        """Provenance columns only — never clobber a row's own identity columns.

        ``strategy_id`` is deliberately emitted **only** when a contract exists:
        a row always knows its own identity, while the *version* axis is exactly
        what may be unknown. Emitting a null ``strategy_id`` here would erase the
        identity of every legacy row.
        """
        payload = {
            "provenance_status": self.status,
            "provenance_detail": self.detail,
            "strategy_version": None,
            "strategy_checksum": None,
            "asof_day": None,
            "scope": None,
            "cycle_id": None,
        }
        if self.provenance is not None:
            payload.update(self.provenance.to_dict())
        return payload


def legacy_reading(subject: str = "", *, detail: str = "legacy row without version stamp",
                   asof_day: str | None = None, scope: str | None = None) -> ProvenanceReading:
    """Explicit "cannot prove it" for pre-provenance rows. Never backfills."""
    return ProvenanceReading(None, STATUS_LEGACY_UNPROVEN, detail, subject)


def not_applicable_reading(subject: str = "", *, detail: str = "") -> ProvenanceReading:
    """The strategy-version axis does not exist for this run (e.g. model families)."""
    return ProvenanceReading(None, STATUS_NOT_APPLICABLE, detail, subject)


# ---------------------------------------------------------------------------
# read-back: status semantics only, never resolution
# ---------------------------------------------------------------------------


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column from any of the four row shapes this repo produces.

    ``sqlite3.Row`` supports ``keys()`` and ``__getitem__`` but is neither a
    ``Mapping`` nor attribute-accessible; a plain tuple has neither. Missing
    keys must read as absent, not raise.
    """
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            names = list(keys())
        except Exception:  # pragma: no cover - defensive
            names = []
        if key in names:
            try:
                return row[key]
            except Exception:  # pragma: no cover - defensive
                return default
        return default
    if hasattr(row, key):
        return getattr(row, key)
    return default


def reading_from_row(row: Any, *, default_scope: str | None = None) -> ProvenanceReading:
    """Classify a **persisted** row without touching the Registry.

    Precedence:

    1. a **persisted** ``provenance_status`` is the row's own statement about
       itself, so it is honoured — but only when the fields agree with it. A
       ``verified`` claim with an incomplete stamp, or a non-``verified`` claim
       backed by a complete one, is ``unknown`` rather than a silent upgrade;
    2. otherwise derive conservatively from the fields: no strategy axis at all →
       ``not_applicable``; no version and no checksum → ``legacy_unproven``; only
       one of them, or any invalid value → ``unknown``; both valid → ``verified``
       **only if** the as-of day is also provable.
    """
    strategy_id = str(_row_get(row, "strategy_id") or "").strip()
    version_raw = _row_get(row, "strategy_version")
    checksum_raw = _row_get(row, "strategy_checksum")
    asof_raw = _row_get(row, "asof_day")
    asof_day = canonical_day(asof_raw)
    scope_raw = _row_get(row, "scope")
    scope = str(scope_raw or "").strip() or (default_scope or "")
    cycle_id = canonical_cycle_id(_row_get(row, "cycle_id"))
    version = canonical_version(version_raw)
    checksum = canonical_checksum(checksum_raw)

    subject = strategy_id
    complete = bool(strategy_id) and version is not None and checksum is not None

    def _contract() -> StrategySelectionProvenance | None:
        if not complete or asof_day is None or scope not in SCOPES:
            return None
        try:
            return StrategySelectionProvenance(
                strategy_id=strategy_id, strategy_version=version,
                strategy_checksum=checksum, asof_day=asof_day, scope=scope,
                cycle_id=cycle_id,
            )
        except ValueError:
            return None

    stored_status = str(_row_get(row, "provenance_status") or "").strip()
    if stored_status in PROVENANCE_STATUSES:
        if stored_status == STATUS_VERIFIED:
            provenance = _contract()
            if provenance is None:
                return ProvenanceReading(
                    None, STATUS_UNKNOWN,
                    f"persisted status {STATUS_VERIFIED!r} but the stamp is incomplete "
                    f"(strategy_id={strategy_id!r}, version={version_raw!r}, "
                    f"checksum={checksum_raw!r}, asof_day={asof_raw!r}, scope={scope_raw!r})",
                    subject,
                )
            return ProvenanceReading(provenance, STATUS_VERIFIED, "", subject)
        if complete and asof_day is not None:
            return ProvenanceReading(
                None, STATUS_UNKNOWN,
                f"persisted status {stored_status!r} contradicts a complete stamp",
                subject,
            )
        detail = f"persisted status {stored_status!r}"
        if stored_status == STATUS_LEGACY_UNPROVEN:
            return ProvenanceReading(None, STATUS_LEGACY_UNPROVEN, detail, subject)
        if stored_status == STATUS_NOT_APPLICABLE:
            return ProvenanceReading(None, STATUS_NOT_APPLICABLE, detail, subject)
        return ProvenanceReading(None, STATUS_UNKNOWN, detail, subject)

    if not strategy_id:
        return not_applicable_reading(detail="no strategy axis on this row")
    if version is None and checksum is None and not (version_raw or checksum_raw):
        return legacy_reading(strategy_id)
    if version is None or checksum is None:
        return ProvenanceReading(
            None, STATUS_UNKNOWN,
            f"partial or invalid stamp (version={version_raw!r}, checksum={checksum_raw!r})",
            subject,
        )
    if asof_day is None:
        return ProvenanceReading(
            None, STATUS_UNKNOWN,
            f"missing or invalid as-of day ({asof_raw!r})", subject,
        )
    if scope not in SCOPES:
        return ProvenanceReading(None, STATUS_UNKNOWN,
                                 f"unknown scope {scope_raw!r}", subject)
    provenance = _contract()
    if provenance is None:
        return ProvenanceReading(None, STATUS_UNKNOWN,
                                 "stamp does not form a valid contract", subject)
    return ProvenanceReading(provenance, STATUS_VERIFIED, "", subject)


# ---------------------------------------------------------------------------
# run identity
# ---------------------------------------------------------------------------


def run_provenance_key(*, scope: str, subject: str, asof_day: Any,
                       strategy_version: Any = None, strategy_checksum: Any = None,
                       cycle_id: Any = None, extra: str = "") -> str:
    """Stable identity for one persisted run's evidence.

    Same strategy/version/checksum/asof/scope/cycle → the same key, so a retry is
    an idempotent overwrite; a different immutable version → a different key, so
    a newer version can never delete the older evidence.

    ``subject`` is the strategy id when one exists, otherwise the honest
    non-strategy label (e.g. a model-family id). It is never invented.
    """
    scope = str(scope or "").strip()
    if scope not in SCOPES:
        raise ValueError(f"unknown provenance scope: {scope!r}")
    subject = str(subject or "").strip()
    if not subject:
        raise ValueError("run identity requires a subject")
    day = canonical_day(asof_day) or "unknown-asof"
    version = canonical_version(strategy_version)
    checksum = canonical_checksum(strategy_checksum)
    cycle = canonical_cycle_id(cycle_id)
    if scope == SCOPE_CYCLE and cycle is None:
        raise ValueError("cycle-scoped run identity requires a cycle id")
    if scope == SCOPE_RESEARCH and cycle is not None:
        raise ValueError("research run identity must not carry a cycle id")
    parts = [
        scope, subject,
        f"v{version}" if version is not None else "v?",
        checksum if checksum is not None else "c?",
        day,
        f"cycle{cycle}" if cycle is not None else "-",
    ]
    if extra:
        parts.append(str(extra))
    return "|".join(parts)


# ---------------------------------------------------------------------------
# as-of resolution（显式 → 唯一 → 混合即拒绝；绝不猜）
# ---------------------------------------------------------------------------


def resolve_asof_day(explicit: Any, declared: Sequence[tuple[str, Any]] | None = None) -> str:
    """Resolve exactly one as-of day, or raise :class:`AsOfUnprovable`.

    Order, mirroring ``strategy_trace.data_date``'s semantics (explicit wins →
    a single distinct declared value → mixed/unprovable refuses):

    1. an **explicit** value must be a valid date; an explicitly supplied but
       unparseable value is an error, never normalised to "absent";
    2. otherwise the declared candidate dates must collapse to exactly one
       distinct day;
    3. zero candidates, or more than one, fail closed — no ``today()``, no
       ``min``/``max``.
    """
    if explicit is not None:
        day = canonical_day(explicit)
        if day is None:
            raise AsOfUnprovable(f"explicit as-of day is invalid: {explicit!r}")
        return day
    seen: dict[str, list[str]] = {}
    for label, value in declared or ():
        day = canonical_day(value)
        if day is None:
            continue
        seen.setdefault(day, []).append(str(label))
    if not seen:
        raise AsOfUnprovable("cannot prove as-of day: no declared candidate date")
    if len(seen) > 1:
        described = ", ".join(
            f"{day}({'+'.join(sorted(labels))})" for day, labels in sorted(seen.items())
        )
        raise AsOfUnprovable(f"refuses mixed as-of days: {described}")
    return next(iter(seen))


def declared_asof_candidates(result: Any) -> list[tuple[str, Any]]:
    """Extract the *factor as-of* candidates a selection result declares.

    Only dates that describe the **factor snapshot the decision was made on**
    are candidates. ``reference_date`` is deliberately excluded: it is the
    *target* trading day, a different fact, and treating the two as competing
    inputs would fail closed on every healthy production run.
    """
    candidates: list[tuple[str, Any]] = []
    if not isinstance(result, Mapping):
        return candidates
    quality = result.get("data_quality")
    if isinstance(quality, Mapping):
        candidates.append(("data_quality.complete_cutoff", quality.get("complete_cutoff")))
    picks = result.get("picks")
    if isinstance(picks, Iterable):
        for index, pick in enumerate(list(picks)[:200]):
            if isinstance(pick, Mapping) and pick.get("historical_factor_date"):
                candidates.append((f"picks[{index}].historical_factor_date",
                                   pick.get("historical_factor_date")))
    return candidates
