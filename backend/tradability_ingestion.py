# -*- coding: utf-8 -*-
"""历史可交易性**证据摄取**层（Historical Tradability Evidence Ingestion）。

本模块回答的问题是：``historical_tradability_archive`` 里的数据——

* 从哪里来？
* 什么时候当时已经可知（``observed_at``）？
* 是否足以支持历史决策？
* 哪些日期 / 股票仍然 UNKNOWN？
* 是否有来源冲突？
* 覆盖率到底是多少？

它建立在 PR156 的 archive core（:mod:`tradability_archive`）之上，**复用**其契约：
``TradabilityEvidence`` / ``normalize_record`` / ``evidence_fingerprint`` /
``TradabilityArchiveRepository`` / ``tradability_at``。本模块**不**定义第二套
``HistoricalTradability`` / ``TradeableState`` / ``RepositoryV2`` 之类的平行系统。

──────────────────────── 分层（严格单向） ────────────────────────

    历史事实来源（K 线 / 状态归档 / 公告）
        ↓  Provider（只取事实，**不写 archive**）
    原始 partial evidence（字段可部分为 ``None`` = 未知）
        ↓  PIT provenance 校验（effective / observed 分开）
        ↓  Normalizer / Composer（确定性合并 + 冲突识别）
        ↓  TradabilityArchiveRepository（唯一写入口）
        ↓  Coverage / Audit Report

Provider **永远不写** ``historical_tradability_archive``，也**不调用**
``TradabilityArchiveRepository.save``。写库 authority 只在 ingestion
orchestration（:class:`IngestionService`）。Provider 是 adapter，不是新的
HTTP framework——复用 ``data_fetcher`` 的 session/retry/circuit breaker，不另写
``requests.get``。

──────────────────────── PIT 铁律 ────────────────────────

``effective_at <= decision_time`` **且** ``observed_at <= decision_time``。

历史回填**禁止时间穿越**：今天抓到一条"某股 2025-06-01 开始 ST"的状态，若 API
没有提供公告时间 / 发布时间 / 原始历史 snapshot 时间，就**不得**把
``observed_at`` 伪造成 ``2025-06-01``。只能：

1. 使用真实可证明的 historical publication / availability timestamp；或
2. ``observed_at`` 用实际获取时间（``retrieved_at``），意味着这条数据**不能**用于
   过去 decision_time；或
3. 不写正式 PIT archive，并记录 ``unprovable_observed_at``。

本模块优先 **宁可 UNKNOWN，不伪造 PIT**。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import point_in_time as PIT
    import tradability_archive as TA
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT  # type: ignore
    from . import tradability_archive as TA  # type: ignore


# ───────────────────────────── 常量与词表 ─────────────────────────────

CONTRACT_VERSION = "tradability-ingestion-v1"
FINGERPRINT_VERSION = "sha256-ingestion-run-v1"
MIGRATION_DESCRIPTION = "014_add_tradability_ingestion_runs"
INGESTION_RUNS_TABLE = "tradability_ingestion_runs"

#: 摄取运行状态词表。
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_GAPS = "completed_with_gaps"
STATUS_FAILED = "failed"
RUN_STATUSES = (STATUS_RUNNING, STATUS_COMPLETED, STATUS_COMPLETED_WITH_GAPS, STATUS_FAILED)

#: Provider 结果状态词表（三态：evidence / unknown / error，**绝不**混成 bool）。
OUTCOME_EVIDENCE = "evidence"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_ERROR = "error"
OUTCOME_STATUSES = (OUTCOME_EVIDENCE, OUTCOME_UNKNOWN, OUTCOME_ERROR)

#: observed_at 的来源类型（建议至少区分这些）。
OBSERVED_SOURCE_TIMESTAMP = "source_timestamp"
OBSERVED_PUBLICATION_TIMESTAMP = "publication_timestamp"
OBSERVED_SNAPSHOT_TIMESTAMP = "snapshot_timestamp"
OBSERVED_RETRIEVED_AT = "retrieved_at"
OBSERVED_UNPROVABLE = "unprovable"
OBSERVED_KINDS = (
    OBSERVED_SOURCE_TIMESTAMP,
    OBSERVED_PUBLICATION_TIMESTAMP,
    OBSERVED_SNAPSHOT_TIMESTAMP,
    OBSERVED_RETRIEVED_AT,
    OBSERVED_UNPROVABLE,
)

#: 可**证明历史可见性**的 observed 类型：只有这些类型能作为"当年已经可知"的证据。
#: ``retrieved_at``（今天才抓到）与 ``unprovable`` 一律不构成历史可见性证明。
PIT_PROVABLE_OBSERVED_KINDS = {
    OBSERVED_SOURCE_TIMESTAMP,
    OBSERVED_PUBLICATION_TIMESTAMP,
    OBSERVED_SNAPSHOT_TIMESTAMP,
}

#: 合成时需要合并的事实字段（**固定顺序**，保证确定性；不依赖 dict/set 顺序）。
_COMPOSED_FIELDS = (
    "is_listed",
    "listing_date",
    "delisting_date",
    "is_st",
    "is_suspended",
    "suspension_reason",
    "has_market_quote",
    "has_trade_volume",
    "is_price_limit_locked",
    "price_limit_direction",
)

#: 三态布尔字段（比较时用 ``is`` 语义，None 表示未知）。
_BOOL_FIELDS = (
    "is_listed",
    "is_st",
    "is_suspended",
    "has_market_quote",
    "has_trade_volume",
    "is_price_limit_locked",
)


class IngestionError(ValueError):
    """摄取无法成立时抛出（run 级别失败，不是单条数据 unknown）。"""


# ───────────────────────────── 审计工具 ─────────────────────────────

def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _canonical_instant(value: Any) -> Optional[str]:
    moment = PIT.parse_asof(value)
    if moment is None:
        return None
    return moment.isoformat()


def _sha256(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ───────────────────────────── Provider 结果 ─────────────────────────────


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """单个 Provider 对 ``(code, session)`` 的一次取数结果。

    ``status`` 严格三态：

    * ``OUTCOME_EVIDENCE`` —— 取到了 partial evidence（字段可部分为 ``None``）；
    * ``OUTCOME_UNKNOWN`` —— 该源对这条 ``(code, session)`` 正常地没有数据
      （**不是** error，也**不是** False/True）；
    * ``OUTCOME_ERROR`` —— Provider 抛异常 / 超时 / 坏 payload，**绝不**转成
      可交易事实，一律 fail unknown。

    ``observed_kind`` / ``observed_at`` 记录这条证据的观测时点来源类型与取值。
    ``observed_at`` 为 ``None`` 时由摄取层用 run cutoff 兜底，并标记 unprovable。
    """

    provider_id: str
    provider_version: str
    status: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    observed_kind: str = OBSERVED_UNPROVABLE
    observed_at: Optional[str] = None
    effective_at: Optional[str] = None
    error: Optional[str] = None


# ───────────────────────────── Provider 基类 ─────────────────────────────


class TradabilityFactProvider:
    """事实来源 adapter 的公共契约。**不写 archive、不 import repository.save**。

    每个 Provider 必须有稳定身份 ``provider_id`` + ``provider_version``；版本可随
    source mapping 变化而提升，不依赖 Python class name。
    """

    provider_id: str = "base"
    provider_version: str = "1"

    def fetch(self, code: str, session: str) -> ProviderResult:
        raise NotImplementedError


def _result(
    provider: TradabilityFactProvider,
    status: str,
    evidence: Optional[Mapping[str, Any]] = None,
    *,
    observed_kind: str = OBSERVED_UNPROVABLE,
    observed_at: Any = None,
    effective_at: Any = None,
    error: Optional[str] = None,
) -> ProviderResult:
    return ProviderResult(
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        status=status,
        evidence=dict(evidence or {}),
        observed_kind=observed_kind,
        observed_at=_canonical_instant(observed_at),
        effective_at=_canonical_instant(effective_at),
        error=error,
    )


# ───────────────────────────── Listing / Delisting Provider ─────────────────


class ListingStatusProvider(TradabilityFactProvider):
    """上市 / 退市事实源。

    只接受**注入的历史 listing 归档**（``code -> {listing_date, delisting_date}``）。
    仓库当前没有上市/退市日历史源；没有记录 → ``unknown``（不是"已上市"）。

    判定（半开区间）::

        session <  listing_date            -> is_listed = False
        listing_date <= session < delisting_date -> is_listed = True
        session >= delisting_date          -> is_listed = False

    ``listing_date`` / ``delisting_date`` 本身也需要 provenance：若调用方只给今天
    整理的静态主表，把它作为 **historical reference evidence**（``observed_kind``
    记为 snapshot/retrieved），**不伪造过去 observed_at**。
    """

    provider_id = "listing_status"
    provider_version = "1"

    def __init__(
        self,
        listing_records: Optional[Mapping[str, Mapping[str, Any]]],
        *,
        observed_kind: str = OBSERVED_RETRIEVED_AT,
        observed_at: Any = None,
    ):
        self._records = {str(k): dict(v) for k, v in (listing_records or {}).items()}
        self._observed_kind = observed_kind
        self._observed_at = _canonical_instant(observed_at)

    def fetch(self, code: str, session: str) -> ProviderResult:
        record = self._records.get(str(code))
        if record is None:
            return _result(self, OUTCOME_UNKNOWN, observed_kind=self._observed_kind)
        listing_date = record.get("listing_date")
        delisting_date = record.get("delisting_date")
        if listing_date is None and delisting_date is None:
            # 空快照（今天在册、无任何上市/退市日期）不构成任何 listing 事实：
            # 既推不出 is_listed，也没有可写的历史字段。诚实返回 unknown，而不是
            # 生成一条全 None 的 evidence、虚报 evidence_present 与覆盖率。
            return _result(self, OUTCOME_UNKNOWN, observed_kind=self._observed_kind)
        is_listed = TA._resolve_listed(  # noqa: SLF001 - 复用 archive 的唯一推导口径
            str(session), listing_date, delisting_date, None
        )
        return _result(
            self,
            OUTCOME_EVIDENCE,
            {
                "is_listed": is_listed,
                "listing_date": listing_date,
                "delisting_date": delisting_date,
            },
            observed_kind=self._observed_kind,
            observed_at=self._observed_at,
            effective_at=listing_date,
        )


# ───────────────────────────── ST Provider ─────────────────────────────


class SecurityStateHistoryProvider(TradabilityFactProvider):
    """历史 ST / 风险警示状态源。复用 :mod:`security_state_point_in_time`。

    **禁止** ``name.startswith("ST")`` / ``name contains ST`` / 用当前名称反推过去。
    ST 必须来自带 ``effective_from`` + ``available_at`` 的历史状态归档。

    ``risk_flag`` → ``is_st``；没有覆盖该 session 的记录 → ``unknown``（**不是**非 ST）。
    """

    provider_id = "security_state_history"
    provider_version = "1"

    def __init__(self, archive: Any):
        # ``archive`` 是 security_state_point_in_time.SecurityStateArchive（或等价）。
        self._archive = archive

    def fetch(self, code: str, session: str) -> ProviderResult:
        if self._archive is None:
            return _result(self, OUTCOME_UNKNOWN)
        try:
            state = self._archive.state_at(str(code), str(session))
        except Exception as exc:  # pragma: no cover - 防御 adapter 边界
            return _result(self, OUTCOME_ERROR, error=f"{type(exc).__name__}: {exc}")
        if state is None:
            return _result(self, OUTCOME_UNKNOWN)
        risk_flag = state.get("risk_flag")
        is_st = PIT.as_strict_bool(risk_flag)
        available_at = state.get("available_at")
        effective_from = state.get("effective_from")
        return _result(
            self,
            OUTCOME_EVIDENCE,
            {"is_st": is_st},
            observed_kind=OBSERVED_SNAPSHOT_TIMESTAMP if available_at else OBSERVED_UNPROVABLE,
            observed_at=available_at,
            effective_at=effective_from,
        )


# ───────────────────────────── Suspension Provider ─────────────────────────


class SuspensionHistoryProvider(TradabilityFactProvider):
    """历史停牌状态源。只接受注入的历史停牌归档。

    归档行::

        {"code", "effective_from", "effective_to", "reason", "available_at"}

    半开区间 ``effective_from <= session < effective_to`` → 停牌中。区间之外**不**
    因为"今天查询不是停牌"就推广为"历史任何日期都没停牌"：只有归档显式声明
    ``complete``（完整覆盖该 code 的停牌史）时，落在所有区间之外的 session 才判
    ``is_suspended=False``（已复牌）——**包括该 code 在归档里完全没有记录**的情况：
    完整归档里"没有记录"就是"没有停牌"。否则 → ``unknown``。来源不完整（
    ``complete=False``）→ ``unknown``。

    "未停牌"这个**否定事实**同样需要可证明的观测时点：仅当 ``complete=True`` 且
    ``availability_basis="session_close"``（归档声明"状态在该 session 收盘时记下"）
    时才用 session 收盘兜底 observed_at，与 ``security_state_history`` 的
    availability_basis 口径一致；否则记为 unprovable（fail closed，历史不可见）。
    """

    provider_id = "suspension_history"
    provider_version = "1"

    SESSION_CLOSE = "session_close"

    def __init__(
        self,
        suspension_rows: Optional[Iterable[Mapping[str, Any]]],
        *,
        complete: bool = False,
        availability_basis: Optional[str] = None,
    ):
        self._by_code: dict = {}
        self._complete = bool(complete)
        self._session_close_basis = (
            str(availability_basis or "").strip() == self.SESSION_CLOSE
        )
        for row in suspension_rows or ():
            if not isinstance(row, Mapping):
                continue
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            self._by_code.setdefault(code, []).append(dict(row))

    def fetch(self, code: str, session: str) -> ProviderResult:
        rows = self._by_code.get(str(code)) or ()
        target = _text(session)
        for row in rows:
            start = _text(row.get("effective_from"))
            end = _text(row.get("effective_to")) or _next_day(start)
            if start is None or end is None:
                continue
            if start <= target < end:
                return _result(
                    self,
                    OUTCOME_EVIDENCE,
                    {"is_suspended": True, "suspension_reason": _text(row.get("reason"))},
                    observed_kind=OBSERVED_SNAPSHOT_TIMESTAMP
                    if _text(row.get("available_at"))
                    else OBSERVED_UNPROVABLE,
                    observed_at=_text(row.get("available_at")),
                    effective_at=start,
                )
        # 落在所有区间之外——**包括该 code 在归档里完全没有记录**。
        # 声明完整的归档里"没有记录"就是"没有停牌"，与"区间不覆盖该 session"
        # 是同一条否定事实，走同一路径；只有来源不完整时才退回 unknown。
        if self._complete:
            effective_at = PIT.bar_available_at(session)
            if self._session_close_basis and effective_at is not None:
                # 归档声明"状态在 session 收盘记下" → 复牌是当时可知的事实。
                return _result(
                    self,
                    OUTCOME_EVIDENCE,
                    {"is_suspended": False},
                    observed_kind=OBSERVED_SNAPSHOT_TIMESTAMP,
                    observed_at=effective_at.isoformat(timespec="seconds"),
                    effective_at=effective_at,
                )
            # 完整但无 availability basis：否定事实 unprovable，不伪造过去可见。
            return _result(
                self,
                OUTCOME_EVIDENCE,
                {"is_suspended": False},
                observed_kind=OBSERVED_UNPROVABLE,
                effective_at=effective_at,
            )
        return _result(self, OUTCOME_UNKNOWN)


def _next_day(session: Optional[str]) -> Optional[str]:
    if not session:
        return None
    try:
        day = _dt.date.fromisoformat(str(session)[:10])
    except ValueError:
        return None
    return (day + _dt.timedelta(days=1)).isoformat()


# ───────────────────────────── Market Session Provider ─────────────────────


class HistoricalMarketSessionProvider(TradabilityFactProvider):
    """历史行情 / 成交量存在性源（从历史日线派生）。

    严格区分 ``quote existed`` 与 ``trade volume existed``：

    * 有效收盘价 → ``has_market_quote=True``；
    * ``volume > 0`` → ``has_trade_volume=True``；
    * ``volume == 0`` → 取决于数据源定义：无法证明"真实零成交"还是"缺失/占位"
      时返回 ``None``（未知），**不**擅自判 ``False``；
    * 该 session 完全没有 bar → ``unknown``（分不清停牌还是数据缺口）。

    日线事实只有在该 session 收盘后才完整可知，所以 ``effective_at`` 一律取
    :func:`point_in_time.bar_available_at`（session 收盘），**不硬编码 15:00 /
    16:00 / midnight**。``observed_at`` 用 ``retrieved_at``（今天才抓到的历史日线），
    因此这类证据**不能**证明"当年盘中已知"——只能作为 historical reference。
    """

    provider_id = "historical_market_session"
    provider_version = "1"

    def __init__(self, kline_reader, *, observed_at: Any = None):
        """``kline_reader(code) -> Mapping[date_str, {"close", "volume"}]``。"""
        self._kline_reader = kline_reader
        self._observed_at = _canonical_instant(observed_at)

    def fetch(self, code: str, session: str) -> ProviderResult:
        try:
            bars = self._kline_reader(str(code))
        except Exception as exc:  # pragma: no cover - 防御 adapter 边界
            return _result(self, OUTCOME_ERROR, error=f"{type(exc).__name__}: {exc}")
        if not isinstance(bars, Mapping):
            return _result(self, OUTCOME_ERROR, error="kline_reader 返回了非映射结果")
        bar = bars.get(_text(session))
        if not isinstance(bar, Mapping):
            return _result(self, OUTCOME_UNKNOWN)
        close = _finite(bar.get("close"))
        volume = _finite(bar.get("volume"))
        has_quote = True if close is not None else None
        if volume is None:
            has_volume = None
        elif volume > 0:
            has_volume = True
        else:
            # volume == 0：无法区分真实零成交与缺失/占位 → 未知。
            has_volume = None
        evidence = {"has_market_quote": has_quote, "has_trade_volume": has_volume}
        effective_at = PIT.bar_available_at(session)
        return _result(
            self,
            OUTCOME_EVIDENCE,
            evidence,
            observed_kind=OBSERVED_RETRIEVED_AT,
            observed_at=self._observed_at,
            effective_at=effective_at,
        )


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    import math

    return number if math.isfinite(number) else None


# ───────────────────────────── Price Limit Provider ─────────────────────────


class PriceLimitProvider(TradabilityFactProvider):
    """涨跌停锁定状态源。**只接受显式 locked/sealed 证据**。

    "触及涨停" ≠ "无法买入"。只有来源能证明 ``locked / sealed / execution
    unavailable`` 才写 ``is_price_limit_locked=True`` 并给出方向；否则一律
    ``unknown``，**不为了 coverage 从日线 ``close == limit price`` 推断**。

    仓库没有 level2 / 封单数据，因此生产上本 Provider 恒返回 unknown；测试通过
    注入显式 locked 证据验证方向语义（up 只拦买、down 只拦卖）与"不推断"。
    """

    provider_id = "price_limit_lock"
    provider_version = "1"

    def __init__(self, lock_records: Optional[Mapping[str, Mapping[str, Any]]] = None):
        """``lock_records[code][session] = {"locked", "direction"}``（显式封单证据）。"""
        self._records = lock_records or {}

    def fetch(self, code: str, session: str) -> ProviderResult:
        record = (self._records.get(str(code)) or {}).get(_text(session))
        if not isinstance(record, Mapping):
            return _result(self, OUTCOME_UNKNOWN)
        locked = PIT.as_strict_bool(record.get("locked"))
        direction = TA._direction(record.get("direction"))  # noqa: SLF001 - 复用白名单
        if locked is None:
            return _result(self, OUTCOME_UNKNOWN)
        return _result(
            self,
            OUTCOME_EVIDENCE,
            {"is_price_limit_locked": locked, "price_limit_direction": direction},
            observed_kind=OBSERVED_SNAPSHOT_TIMESTAMP
            if _text(record.get("observed_at"))
            else OBSERVED_UNPROVABLE,
            observed_at=_text(record.get("observed_at")),
            effective_at=_text(record.get("effective_at")),
        )


# ───────────────────────────── Composition / Conflict ─────────────────────


@dataclass(frozen=True, slots=True)
class Conflict:
    """同一 PIT 时点、同一字段的**来源冲突**。默认 fail unknown，绝不 last-write-wins。"""

    field: str
    providers: tuple
    values: tuple
    session: str
    effective_at: Optional[str] = None
    observed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "providers": list(self.providers),
            "values": list(self.values),
            "session": self.session,
            "effective_at": self.effective_at,
            "observed_at": self.observed_at,
        }

    def identity_payload(self) -> dict:
        """进入**内容身份**的冲突明细。

        与 :meth:`to_dict` 同源——``detail_json`` 会持久化 ``session`` /
        ``effective_at`` / ``observed_at``，因此这些字段也必须参与 replay identity。
        只保留 field/providers/values 会让"同一组冲突从一个 session 挪到另一个
        session"被当成幂等重放，而审计行里描述的仍是旧 session 的冲突。
        """
        return self.to_dict()


def _compose_fields(results: Sequence[ProviderResult]) -> tuple[dict, list, list]:
    """把多个 Provider 的 partial evidence 确定性合并。

    返回 ``(composed, conflicts, unprovable_fields)``：

    * 字段在**所有** provider 里都缺失 → ``None``（未知），**绝不**默认 False/True；
    * 字段只有一个非 None 值 → 该值；
    * 字段有多个非 None 值且全部一致 → 该值；
    * 字段有多个非 None 值且不一致 → 冲突：字段记 ``None``，冲突计入 ``conflicts``。

    冲突明细记录 ``field / providers / values``，供 coverage 与 run audit 消费。
    """
    composed: dict = {}
    conflicts: list = []
    unprovable: list = []
    for fname in _COMPOSED_FIELDS:
        contributions = []  # (provider_id, value)
        for result in results:
            if fname not in result.evidence:
                continue
            value = result.evidence[fname]
            if value is None:
                continue
            contributions.append((result.provider_id, value))
        if not contributions:
            composed[fname] = None
            continue
        distinct = {value for _, value in contributions}
        if len(distinct) > 1:
            composed[fname] = None
            conflicts.append(
                Conflict(
                    field=fname,
                    providers=tuple(p for p, _ in contributions),
                    values=tuple(contributions[i][1] for i in range(len(contributions))),
                    session="",
                )
            )
        else:
            composed[fname] = contributions[0][1]
    return composed, conflicts, unprovable


def _compose_source(results: Sequence[ProviderResult]) -> str:
    """``source`` 必须 deterministic：按 provider id 排序组合，不依赖 set 顺序。"""
    ids = sorted({r.provider_id for r in results if r.status == OUTCOME_EVIDENCE})
    return "+".join(ids)


def _compose_times(results: Sequence[ProviderResult], cutoff: Any, session: str) -> dict:
    """合成 ``(observed_at, effective_at, unprovable)``。

    ``observed_at`` 取所有贡献 provider 的 observed_at 的**最大值**（一条 evidence
    只有在最晚的贡献事实也被观察到之后才完整可知）；任一 provider 的 observed_kind
    不属可证明类型 → 整条标记 ``unprovable``。observed_at 全部缺失时用 cutoff 兜底
    并标 unprovable（"今天才抓到"不能证明当年已知）。

    ``effective_at`` 取各 provider 的 effective_at 最大值；全部缺失时用该 session
    收盘（:func:`point_in_time.bar_available_at`）兜底——effective 是"状态何时生效"，
    最保守的假设是"该 session 内已生效"，**绝不**用 cutoff（今天）兜底：那会把一条
    带着历史 observed_at 的证据也判成 effective_at=今天、从而对所有历史 decision
    都不可见，fail closed 过头。真正挡"未来才知道"的闸门是 observed_at，不是
    effective_at。
    """
    observed_values = []
    effective_values = []
    unprovable = False
    for result in results:
        if result.status != OUTCOME_EVIDENCE:
            continue
        if result.observed_kind not in PIT_PROVABLE_OBSERVED_KINDS:
            unprovable = True
        if result.observed_at is not None:
            observed_values.append(result.observed_at)
        if result.effective_at is not None:
            effective_values.append(result.effective_at)
    if not observed_values:
        unprovable = True
        observed = _canonical_instant(cutoff)
    else:
        observed = max(observed_values)
    if effective_values:
        effective = max(effective_values)
    else:
        effective = (
            PIT.bar_available_at(session).isoformat()
            if PIT.bar_available_at(session) is not None
            else _canonical_instant(cutoff)
        )
    return {"observed_at": observed, "effective_at": effective, "unprovable": unprovable}


# ───────────────────────────── Coverage / Audit ─────────────────────────────


@dataclass(frozen=True, slots=True)
class TradabilityCoverageReport:
    """一次 ingestion 的覆盖率报告。只测量，不设阈值、不 gate execution。"""

    session: str
    requested_symbols: int
    archive_records: int
    evidence_present: int
    fully_proven: int
    unknown_listing: int
    unknown_st: int
    unknown_suspension: int
    unknown_quote: int
    unknown_volume: int
    unknown_limit_state: int
    conflicts: int
    unprovable_observed_at: int
    source_counts: Mapping[str, int]
    coverage_ratio: float
    fingerprint: str

    def to_dict(self) -> dict:
        return {
            "session": self.session,
            "requested_symbols": self.requested_symbols,
            "archive_records": self.archive_records,
            "evidence_present": self.evidence_present,
            "fully_proven": self.fully_proven,
            "unknown_listing": self.unknown_listing,
            "unknown_st": self.unknown_st,
            "unknown_suspension": self.unknown_suspension,
            "unknown_quote": self.unknown_quote,
            "unknown_volume": self.unknown_volume,
            "unknown_limit_state": self.unknown_limit_state,
            "conflicts": self.conflicts,
            "unprovable_observed_at": self.unprovable_observed_at,
            "source_counts": dict(self.source_counts),
            "coverage_ratio": self.coverage_ratio,
            "fingerprint": self.fingerprint,
        }


def _coverage_fingerprint(report: Mapping[str, Any]) -> str:
    payload = {
        "version": FINGERPRINT_VERSION,
        "session": report.get("session"),
        "requested_symbols": report.get("requested_symbols"),
        "archive_records": report.get("archive_records"),
        "evidence_present": report.get("evidence_present"),
        "evidence_present_pairs": report.get("evidence_present_pairs"),
        "fully_proven": report.get("fully_proven"),
        "known": report.get("known"),
        "unknown": report.get("unknown"),
        "conflict": report.get("conflict"),
        "unprovable_observed_at": report.get("unprovable_observed_at"),
        "unknown_fields": report.get("unknown_fields"),
        "unknown_listing": report.get("unknown_listing"),
        "unknown_st": report.get("unknown_st"),
        "unknown_suspension": report.get("unknown_suspension"),
        "unknown_quote": report.get("unknown_quote"),
        "unknown_volume": report.get("unknown_volume"),
        "unknown_limit_state": report.get("unknown_limit_state"),
        "conflicts": report.get("conflicts"),
        "source_counts": dict(report.get("source_counts") or {}),
        "coverage_ratio": report.get("coverage_ratio"),
        "evidence_presence_ratio": report.get("evidence_presence_ratio"),
    }
    return _sha256(payload)


# ───────────────────────────── Schema（migration 014） ─────────────────────


def ensure_ingestion_schema(conn: sqlite3.Connection) -> dict:
    """正式 migration 014 的建表函数（幂等）。摄取运行审计表。"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {INGESTION_RUNS_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            cutoff TEXT,
            session_start TEXT,
            session_end TEXT,
            provider_set TEXT,
            status TEXT NOT NULL,
            requested_codes INTEGER NOT NULL DEFAULT 0,
            requested_sessions INTEGER NOT NULL DEFAULT 0,
            raw_records INTEGER NOT NULL DEFAULT 0,
            normalized_records INTEGER NOT NULL DEFAULT 0,
            persisted_records INTEGER NOT NULL DEFAULT 0,
            skipped_records INTEGER NOT NULL DEFAULT 0,
            unknown_records INTEGER NOT NULL DEFAULT 0,
            conflict_records INTEGER NOT NULL DEFAULT 0,
            unprovable_records INTEGER NOT NULL DEFAULT 0,
            error_records INTEGER NOT NULL DEFAULT 0,
            detail_json TEXT,
            run_fingerprint TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    return {"table": INGESTION_RUNS_TABLE, "migration": MIGRATION_DESCRIPTION}


# ───────────────────────────── Ingestion Service ─────────────────────────────


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """一次摄取运行的结果：写入的证据 + 覆盖率报告 + 审计。"""

    run_id: str
    status: str
    persisted: Sequence[TA.TradabilityEvidence]
    coverage: Mapping[str, Any]
    conflicts: Sequence[Conflict]
    unprovable: Sequence[str]
    run_fingerprint: str
    provider_versions: Mapping[str, str]


class IngestionService:
    """摄取编排器：Provider orchestration → PIT validation → composition →
    conflict detection → normalization → repository persistence → audit。

    写库 authority **只**在这里（调 ``TradabilityArchiveRepository.save``）；
    Provider 永不写库。
    """

    def __init__(
        self,
        providers: Sequence[TradabilityFactProvider],
        repository: TA.TradabilityArchiveRepository,
        *,
        cutoff: Any = None,
        audit_conn: Optional[sqlite3.Connection] = None,
    ):
        if not providers:
            raise IngestionError("ingestion 至少需要一个 provider")
        self._providers = list(providers)
        self._repo = repository
        self._cutoff = _canonical_instant(cutoff) or _now_utc()
        # 可选：写 ``tradability_ingestion_runs`` 审计表的连接。不传则只做
        # archive 写入与 coverage，不落 run 记录（纯内存测试可省略）。
        #
        # **审计连接必须就是归档连接**。replay identity 是"归档里这条 run 记了什么
        # 指纹"，它必须与事实写入在同一个库、同一个事务里：分属两条连接时，既可能读到
        # 一个空的 runs 表（而归档库里其实已经有该 run_id），也可能让事实与审计跨库提交
        # ——"整个事务 rollback"就无从谈起。与其在写入时才发现，不如在构造时就拒绝。
        if audit_conn is not None and audit_conn is not repository.connection:
            raise IngestionError(
                "audit_conn 必须与 repository 使用同一个连接：replay identity 的查询与"
                "事实写入必须在同一个库、同一个事务内，否则事实与审计无法原子提交"
            )
        self._audit_conn = audit_conn
        # 同一个连接**还不够**：``isolation_level=None`` 是 SQLite 的 autocommit 模式，
        # 那里每次 ``repository.save()`` 与审计插入都各自立即提交。若 ``_persist_run``
        # 随后失败（审计表上的 trigger / 约束中止插入），事实已经durable、``rollback()``
        # 无效、审计行也不存在——"整个事务 rollback"就成了空话。
        #
        # 判据：构造时探测连接是否处于显式事务模式。``in_transaction`` 只反映"此刻有没有
        # 打开事务"，因此不能只看它；这里直接读 ``isolation_level``（``None`` = autocommit）。
        self._enforce_explicit_transactions = (
            audit_conn is not None
            and getattr(audit_conn, "isolation_level", "") is None
        )

    @property
    def provider_versions(self) -> Mapping[str, str]:
        return {p.provider_id: p.provider_version for p in self._providers}

    def _fetch(self, code: str, session: str) -> Sequence[ProviderResult]:
        """逐个 provider 取数。单个 provider 失败**不**影响其它股票/provider。"""
        outcomes: list = []
        for provider in self._providers:
            try:
                result = provider.fetch(code, session)
                if not isinstance(result, ProviderResult):
                    result = _result(
                        provider, OUTCOME_ERROR, error="provider 返回了非法结果类型"
                    )
            except Exception as exc:  # pragma: no cover - 防御任何 provider 异常
                result = _result(provider, OUTCOME_ERROR, error=f"{type(exc).__name__}: {exc}")
            outcomes.append(result)
        return outcomes

    def ingest(
        self,
        codes: Sequence[str],
        sessions: Sequence[str],
        *,
        write: bool = True,
        run_id: Optional[str] = None,
    ) -> IngestionResult:
        """对 ``codes × sessions`` 做完整摄取。

        ``write=False`` 是 dry-run：只计算 coverage / 审计，**不落库**。
        ``run_id`` 可注入（幂等重放用同一个 id）；缺省生成 uuid。
        """
        codes = [str(c) for c in codes if _text(c)]
        sessions = [str(s) for s in sessions if _text(s)]
        # 零 scope 的 run 不得存在：它没有覆盖任何 (code, session) 对，也不该落一行
        # 审计把自己记成一次完成的摄取。这是**操作员错误**，不是"数据 unknown"。
        # 必须在任何持久化之前拒绝（调用方据此 rollback / 非零退出）。
        if not codes:
            raise IngestionError("ingestion 拒绝空 code scope（0 个代码）")
        if not sessions:
            raise IngestionError("ingestion 拒绝空 session scope（0 个交易日）")
        run_id = run_id or uuid.uuid4().hex
        started_at = _now_utc()

        persisted: list = []
        normalized_evidence: list = []
        conflicts: list = []
        unprovable: list = []
        raw_records = 0
        unknown_records = 0
        error_records = 0
        normalized_records = 0
        conflict_records = 0
        unprovable_records = 0
        skipped_records = 0
        source_counts: dict = {}
        per_session: dict = {}

        for session in sessions:
            session_stats = self._init_session_stats(len(codes))
            for code in codes:
                outcomes = self._fetch(code, session)
                raw_records += len([r for r in outcomes if r.status == OUTCOME_EVIDENCE])
                unknown_records += len([r for r in outcomes if r.status == OUTCOME_UNKNOWN])
                error_records += len([r for r in outcomes if r.status == OUTCOME_ERROR])

                evidence_outcomes = [r for r in outcomes if r.status == OUTCOME_EVIDENCE]
                if not evidence_outcomes:
                    # 没有任何 provider 给出事实 → 这条 (code, session) 无证据，
                    # pair-level 判 unknown（只 +1 一次）。
                    self._record_unknown_fields(session_stats)
                    session_stats["unknown"] += 1
                    continue

                composed, field_conflicts, _ = _compose_fields(evidence_outcomes)
                times = _compose_times(evidence_outcomes, self._cutoff, session)
                source = _compose_source(evidence_outcomes)

                # 冲突：该 code/session 不得 fully proven，冲突字段 fail unknown。
                if field_conflicts:
                    for conflict in field_conflicts:
                        conflict = Conflict(
                            field=conflict.field,
                            providers=conflict.providers,
                            values=conflict.values,
                            session=session,
                            effective_at=times["effective_at"],
                            observed_at=times["observed_at"],
                        )
                        conflicts.append(conflict)
                    conflict_records += 1

                if times["unprovable"]:
                    unprovable.append(f"{code}:{session}")
                    unprovable_records += 1

                record = {
                    "code": code,
                    "session_date": session,
                    "source": source,
                    "observed_at": times["observed_at"],
                    "effective_at": times["effective_at"],
                    **composed,
                }

                # source_counts 独立于本次是否新增落库：dry-run 与幂等重放也必须
                # 报告相同的来源拆分，不能因为唯一键命中就变成空。
                for source_id in evidence_outcomes:
                    source_counts[source_id.provider_id] = (
                        source_counts.get(source_id.provider_id, 0) + 1
                    )

                # 规范化在 persistence gate **之外**完成：canonical normalized
                # evidence 必须与 write=False / write=True 无关——run_fingerprint
                # 只由事实输入 + scope + cutoff + provider/provenance identity 决定，
                # 绝不随"这次有没有真正 INSERT"漂移。dry-run 与幂等重放都产生同一
                # 份 normalized evidence，从而得到相同的 fingerprint。
                try:
                    evidence = TA.normalize_record(record)
                except TA.TradabilityArchiveError:
                    # 规范化失败（缺失时点/来源）→ 记 skipped，不落库、不抛散。
                    skipped_records += 1
                    continue
                normalized_records += 1
                normalized_evidence.append(evidence)

                self._record_session_stats(
                    session_stats, composed, field_conflicts, times["unprovable"]
                )

            per_session[session] = self._finalize_session_stats(session_stats)

        status = self._run_status(error_records, unknown_records, conflicts, unprovable)
        coverage = self._build_coverage(per_session, sessions, codes, source_counts)
        run_fingerprint = self._run_fingerprint(
            codes,
            sessions,
            self._cutoff,
            normalized_evidence,
            # provider 结果分布进指纹：error→unknown 的翻转在 evidence 上不可见，
            # 但会改变 status 与 audit 计数，因此属于内容身份（见 _run_fingerprint）。
            {
                "evidence": raw_records,
                "unknown": unknown_records,
                "error": error_records,
                "skipped": skipped_records,
                "status": status,
            },
            unprovable,
            conflicts,
        )

        # ── 两阶段：先算完所有结论与内容身份，再决定要不要落库 ──
        #
        # replay identity 必须在**任何不可逆持久化之前**校验。``run_id`` 是审计身份，
        # ``run_fingerprint`` 是内容身份；同一个 run_id 配不同的内容指纹意味着
        # "拿一个新 run 冒充旧 run 的重放"，必须 fail closed。绝不能先 save() 再发现
        # 冲突——那时 archive 已经被写入，而 audit 行仍在描述旧 run。
        #
        # 因此**所有 archive 写入都在这一步之后**：校验不过就直接抛错，archive 行数
        # 与 audit 行都不会变（不依赖调用方是否记得 rollback）。
        if write:
            self._assert_replay_identity(run_id, run_fingerprint)
            # autocommit 连接上没有事务可回滚，事实与审计无法原子提交 → 显式拒绝。
            if self._enforce_explicit_transactions:
                raise IngestionError(
                    "write=True 需要显式事务：该连接处于 autocommit（isolation_level=None），"
                    "事实写入与审计插入会各自立即提交，任一后续失败都无法整体回滚"
                )
            for evidence in normalized_evidence:
                if self._repo.save(evidence):
                    persisted.append(evidence)
                else:
                    skipped_records += 1  # 幂等重放：唯一键命中，逻辑状态不变。

        # dry-run（write=False）不写任何东西：archive 与 run audit 都不落库。
        if write:
            self._persist_run(
                run_id=run_id,
                started_at=started_at,
                cutoff=self._cutoff,
                sessions=sessions,
                status=status,
                requested_codes=len(codes),
                raw_records=raw_records,
                normalized_records=normalized_records,
                persisted_records=len(persisted),
                skipped_records=skipped_records,
                unknown_records=unknown_records,
                conflict_records=conflict_records,
                unprovable_records=unprovable_records,
                error_records=error_records,
                conflicts=conflicts,
                unprovable=unprovable,
                run_fingerprint=run_fingerprint,
            )

        return IngestionResult(
            run_id=run_id,
            status=status,
            persisted=persisted,
            coverage=coverage,
            conflicts=conflicts,
            unprovable=unprovable,
            run_fingerprint=run_fingerprint,
            provider_versions=self.provider_versions,
        )

    # ── coverage 内部 ──
    @staticmethod
    def _init_session_stats(requested: int) -> dict:
        return {
            "requested": requested,
            "archive_records": 0,
            "evidence_present": 0,
            "evidence_present_pairs": 0,
            "fully_proven": 0,
            "known": 0,
            "unknown": 0,
            "conflict": 0,
            "unknown_listing": 0,
            "unknown_st": 0,
            "unknown_suspension": 0,
            "unknown_quote": 0,
            "unknown_volume": 0,
            "unknown_limit_state": 0,
            "conflicts": 0,
            "unprovable": 0,
        }

    @staticmethod
    def _record_unknown_fields(stats: dict) -> None:
        stats["unknown_listing"] += 1
        stats["unknown_st"] += 1
        stats["unknown_suspension"] += 1
        stats["unknown_quote"] += 1
        stats["unknown_volume"] += 1
        stats["unknown_limit_state"] += 1

    @staticmethod
    def _record_session_stats(
        stats: dict, composed: Mapping[str, Any], conflicts: list, unprovable: bool
    ) -> None:
        stats["archive_records"] += 1
        stats["evidence_present"] += 1
        stats["evidence_present_pairs"] += 1
        if composed.get("is_listed") is None:
            stats["unknown_listing"] += 1
        if composed.get("is_st") is None:
            stats["unknown_st"] += 1
        if composed.get("is_suspended") is None:
            stats["unknown_suspension"] += 1
        if composed.get("has_market_quote") is None:
            stats["unknown_quote"] += 1
        if composed.get("has_trade_volume") is None:
            stats["unknown_volume"] += 1
        if composed.get("is_price_limit_locked") is None:
            stats["unknown_limit_state"] += 1
        if conflicts:
            stats["conflicts"] += 1
        # 核心事实全部非 None 且无冲突、且观测时点可证明 → 与 #156 evaluator 的
        # fully_proven 口径一致。price_limit_locked 是追加方向性限制，不参与
        # 核心 fully_proven 判定（保持 #156 语义不动）。
        core_fields_complete = (
            composed.get("is_listed") is not None
            and composed.get("is_st") is not None
            and composed.get("is_suspended") is not None
            and composed.get("has_market_quote") is not None
            and composed.get("has_trade_volume") is not None
        )
        if core_fields_complete and not conflicts and not unprovable:
            stats["fully_proven"] += 1
        # pair-level exclusive classification，优先级 conflict > unprovable >
        # unknown > known。一个 pair 最终只 +1 一次，保证四类总和恒等于 requested_pairs。
        if conflicts:
            stats["conflict"] += 1
        elif unprovable:
            stats["unprovable"] += 1
        elif not core_fields_complete:
            stats["unknown"] += 1
        else:
            stats["known"] += 1

    @staticmethod
    def _finalize_session_stats(stats: dict) -> dict:
        stats["coverage_ratio"] = (
            round(stats["known"] / stats["requested"] * 100, 1)
            if stats["requested"]
            else 0.0
        )
        stats["evidence_presence_ratio"] = (
            round(stats["evidence_present_pairs"] / stats["requested"] * 100, 1)
            if stats["requested"]
            else 0.0
        )
        return stats

    @staticmethod
    def _build_coverage(
        per_session: Mapping[str, dict],
        sessions: Sequence[str],
        codes: Sequence[str],
        source_counts: Mapping[str, int],
    ) -> Mapping[str, Any]:
        sessions = list(sessions) or per_session.keys()  # type: ignore[assignment]
        summary = {}
        for session in sessions:
            stats = per_session.get(session)
            if stats is None:
                continue
            summary[session] = {
                key: stats[key]
                for key in (
                    "requested",
                    "archive_records",
                    "evidence_present",
                    "evidence_present_pairs",
                    "fully_proven",
                    "known",
                    "unknown",
                    "conflict",
                    "unprovable",
                    "unknown_listing",
                    "unknown_st",
                    "unknown_suspension",
                    "unknown_quote",
                    "unknown_volume",
                    "unknown_limit_state",
                    "conflicts",
                    "unprovable",
                    "coverage_ratio",
                    "evidence_presence_ratio",
                )
            }
        # 分母口径：requested code-session pairs（调用方传入的 code × session 全笛卡尔积）。
        # 绝不宣称 whole_market_coverage，除非历史 universe 本身也是 PIT 可证明。
        total_requested = len(codes)
        requested_pairs = total_requested * len(sessions)
        total_present = sum(s["evidence_present"] for s in summary.values())
        total_present_pairs = sum(s["evidence_present_pairs"] for s in summary.values())
        total_fully = sum(s["fully_proven"] for s in summary.values())
        total_conflicts = sum(s["conflicts"] for s in summary.values())
        total_unknown_fields = sum(
            s["unknown_listing"] + s["unknown_st"] + s["unknown_suspension"]
            + s["unknown_quote"] + s["unknown_volume"] + s["unknown_limit_state"]
            for s in summary.values()
        )
        total_known = sum(s["known"] for s in summary.values())
        total_unknown_pairs = sum(s["unknown"] for s in summary.values())
        total_conflict_pairs = sum(s["conflict"] for s in summary.values())
        total_unprovable_pairs = sum(s["unprovable"] for s in summary.values())
        aggregate = {
            "version": CONTRACT_VERSION,
            "scope": "requested_code_coverage",
            "requested_symbols": total_requested,
            "requested_codes": total_requested,
            "requested_sessions": len(sessions),
            "requested_pairs": requested_pairs,
            "evidence_present": total_present,
            "evidence_present_pairs": total_present_pairs,
            "known": total_known,
            "unknown": total_unknown_pairs,
            "conflict": total_conflict_pairs,
            "unprovable_observed_at": total_unprovable_pairs,
            "fully_proven": total_fully,
            "unknown_fields": total_unknown_fields,
            "unknown_field_count": total_unknown_fields,
            "unknown_listing": sum(s["unknown_listing"] for s in summary.values()),
            "unknown_st": sum(s["unknown_st"] for s in summary.values()),
            "unknown_suspension": sum(s["unknown_suspension"] for s in summary.values()),
            "unknown_quote": sum(s["unknown_quote"] for s in summary.values()),
            "unknown_volume": sum(s["unknown_volume"] for s in summary.values()),
            "unknown_limit_state": sum(s["unknown_limit_state"] for s in summary.values()),
            "conflicts": total_conflicts,
            "source_counts": dict(source_counts),
            "coverage_ratio": round(total_known / requested_pairs * 100, 1)
            if requested_pairs
            else 0.0,
            "evidence_presence_ratio": round(total_present_pairs / requested_pairs * 100, 1)
            if requested_pairs
            else 0.0,
            "sessions": summary,
        }
        aggregate["fingerprint"] = _coverage_fingerprint(aggregate)
        return aggregate

    @staticmethod
    def _run_status(errors: int, unknown: int, conflicts: list, unprovable: list) -> str:
        if errors:
            return STATUS_COMPLETED_WITH_GAPS
        # UNKNOWN 是数据事实，不是失败；只有 provider error 才构成 gap。
        return STATUS_COMPLETED

    def _run_fingerprint(
        self,
        codes: Sequence[str], sessions: Sequence[str], cutoff: str,
        normalized: Sequence[TA.TradabilityEvidence],
        outcomes: Optional[Mapping[str, Any]] = None,
        unprovable: Optional[Sequence[Any]] = None,
        conflicts: Optional[Sequence[Any]] = None,
    ) -> str:
        """内容身份指纹：由 normalized facts + requested scope + cutoff +
        provider/provenance identity + **provider 结果分布**决定。run_id 是 audit
        identity，**绝不**参与内容指纹；同一份事实用不同 run_id 重放必须得到同一
        fingerprint。数据变化时指纹必须变化。

        输入是**规范化后的证据**而非本次新插入的 persisted 行：幂等重放时第二次
        ``ingest`` 不会产生任何新插入（唯一键命中），若用 persisted 作输入，同一份
        输入的指纹会在第一次与第二次 replay 之间漂移，破坏 idempotent replay 的语义。

        同时把 **provider/provenance identity**（provider_id → provider_version）
        纳入指纹：version 属于 provenance 身份，改变它必须改变指纹。

        ``outcomes`` 是本次运行的 provider 结果分布（evidence / unknown / error 计数）。
        它必须进入指纹：一个 provider 返回 ``error`` 与后来返回 ``unknown``，在 scope /
        cutoff / provider 版本完全相同的情况下**不产生任何 normalized evidence**，
        若指纹只看 evidence，两次运行会得到同一个指纹而被判成"幂等重放"，于是
        ``INSERT OR IGNORE`` 保留第一行 audit —— 本次运行报告 ``completed``，审计里
        却仍是 ``completed_with_gaps``。

        ``unprovable`` / ``conflicts`` 同理：provider 给出的**事实与时间戳完全相同**、
        只把 ``observed_kind`` 从可证明类型换成 ``unprovable`` 时，normalized evidence
        与 outcomes 计数都不变，但审计行的 ``unprovable_records`` 与
        ``detail_json.unprovable`` 变了。规则是：**凡是会写进审计行的差异都属于内容
        身份**，不能只覆盖其中一部分。
        """
        payload = {
            "version": FINGERPRINT_VERSION,
            "codes": sorted(codes),
            "sessions": sorted(sessions),
            "cutoff": cutoff,
            "provider_versions": dict(sorted(self.provider_versions.items())),
            "evidence_fingerprints": sorted(TA.evidence_fingerprint(e) for e in normalized),
            "outcomes": dict(sorted((outcomes or {}).items())),
            "unprovable": sorted(str(pair) for pair in (unprovable or ())),
            # 完整冲突明细（含 session / effective_at / observed_at），并**规范化排序**：
            # detail_json 持久化的是 to_dict() 的全字段，指纹只取一部分就会漏掉
            # "冲突挪到另一个 session" 这类审计可见变化；不排序则依赖列表顺序，
            # 同一个冲突集合换个到达顺序就会得到不同指纹，破坏幂等重放。
            "conflicts": sorted(
                (
                    json.dumps(c.identity_payload(), sort_keys=True, ensure_ascii=False, default=str)
                    for c in (conflicts or ())
                )
            ),
        }
        return _sha256(payload)

    def _assert_replay_identity(self, run_id: str, run_fingerprint: str) -> None:
        """同 run_id 的重放必须内容一致，否则 fail closed（在写 archive 之前）。

        contract::

            same run_id + same run_fingerprint  => 幂等重放，允许
            same run_id + different fingerprint => IngestionError
                                                   → 整个事务 rollback
                                                   → archive 不变、audit 不变

        这一步**必须**发生在任何 archive / audit 写入之前。默认 provider 每次运行都
        生成新的 ``retrieved_at``，因此"同一个 run_id 再跑一次"往往**不是**重放而是
        一次内容不同的新 run；那种情况必须被拒绝，而不是让 archive 保存新 revision
        而 audit 行继续描述旧 run。

        **没有持久审计存储就必须拒绝**：replay identity 的判据是"上次那个 run_id 记了
        什么指纹"，而那个事实**只**存在于审计表里。没有审计连接时，这次调用既读不到
        既有指纹、``_persist_run`` 也不会记下自己的指纹，于是第二次同 run_id 写入必然
        读到"无既有行"而放行——正好是这条契约要挡的事故。因此这里把"无持久审计"直接
        判为不可重放，而不是静默退化成一个永远通过的门。
        """
        if self._audit_conn is None:
            raise IngestionError(
                "write=True 需要持久审计存储（audit_conn）才能校验 run_id 的 replay "
                "identity；缺少它时既读不到既有指纹也无法记录本次指纹，"
                "同 run_id 的 divergent replay 将无法被拒绝"
            )
        try:
            row = self._audit_conn.execute(
                f"SELECT run_fingerprint FROM {INGESTION_RUNS_TABLE} WHERE run_id=?",
                (run_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # 审计表不存在 = 无法证明"这是同一次重放" → fail closed，绝不当作"没有冲突"。
            raise IngestionError(
                f"审计表 {INGESTION_RUNS_TABLE} 不可读（{exc}），无法校验 run_id "
                f"{run_id!r} 的 replay identity"
            ) from exc
        if row is None:
            return
        stored = row[0] if not isinstance(row, Mapping) else row.get("run_fingerprint")
        if stored is None:
            # 既有 run 没有记录内容指纹：无法证明它是同一次重放 → fail closed。
            raise IngestionError(
                f"run_id {run_id!r} 已存在但未记录 run_fingerprint，"
                "无法证明是同一次重放"
            )
        if stored != run_fingerprint:
            raise IngestionError(
                f"run_id {run_id!r} 的 divergent replay 被拒绝："
                f"stored={stored} incoming={run_fingerprint}"
            )

    def _persist_run(
        self,
        *,
        run_id: str,
        started_at: str,
        cutoff: str,
        sessions: Sequence[str],
        status: str,
        requested_codes: int,
        raw_records: int,
        normalized_records: int,
        persisted_records: int,
        skipped_records: int,
        unknown_records: int,
        conflict_records: int,
        unprovable_records: int,
        error_records: int,
        conflicts: Sequence[Conflict],
        unprovable: Sequence[str],
        run_fingerprint: str,
    ) -> None:
        """把 run 审计写入 ``tradability_ingestion_runs``（幂等：run_id 唯一）。"""
        if self._audit_conn is None:
            return
        detail = {
            "sessions": list(sessions),
            "conflicts": [c.to_dict() for c in conflicts],
            "unprovable": list(unprovable),
        }
        self._audit_conn.execute(
            f"""
            INSERT OR IGNORE INTO {INGESTION_RUNS_TABLE}(
                run_id, started_at, completed_at, cutoff, session_start, session_end,
                provider_set, status, requested_codes, requested_sessions,
                raw_records, normalized_records, persisted_records, skipped_records,
                unknown_records, conflict_records, unprovable_records, error_records,
                detail_json, run_fingerprint, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                started_at,
                _now_utc(),
                cutoff,
                min(sessions) if sessions else None,
                max(sessions) if sessions else None,
                "+".join(sorted(self.provider_versions)),
                status,
                requested_codes,
                len(sessions),
                raw_records,
                normalized_records,
                persisted_records,
                skipped_records,
                unknown_records,
                conflict_records,
                unprovable_records,
                error_records,
                json.dumps(detail, ensure_ascii=False),
                run_fingerprint,
                _now_utc(),
            ),
        )


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    repo = TA.TradabilityArchiveRepository(conn)

    listing = ListingStatusProvider(
        {"000001": {"listing_date": "2010-01-01"}},
        observed_kind=OBSERVED_SNAPSHOT_TIMESTAMP,
        observed_at="2025-06-01T09:00:00+08:00",
    )
    st = SecurityStateHistoryProvider(None)
    market = HistoricalMarketSessionProvider(
        lambda code: {"2025-06-10": {"close": 10.0, "volume": 100}},
        observed_at="2025-06-11T09:00:00+08:00",
    )
    service = IngestionService([listing, st, market], repo)
    result = service.ingest(["000001"], ["2025-06-10"], write=True)
    assert result.status == STATUS_COMPLETED, result.status
    assert result.coverage["evidence_present"] == 1
    # ST 未知（无状态源）与涨跌停未知，不应计入 fully_proven。
    assert result.coverage["sessions"]["2025-06-10"]["fully_proven"] == 0
    print("tradability_ingestion self-check: ok")


if __name__ == "__main__":  # pragma: no cover
    _self_check()
