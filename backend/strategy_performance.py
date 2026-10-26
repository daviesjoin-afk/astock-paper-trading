# -*- coding: utf-8 -*-
"""版本级策略绩效事实 owner（方案 §2、切片 F2）。

回答**唯一**一个问题：

    在这个精确策略定义版本 + 明确观察窗口下，账本实际能支持哪些绩效事实？

它不是 policy：不判断策略好不好、不触发退休、不改健康阈值、不写任何账本。它也不是
收益计算器的万能替身 —— 每个指标单独给出状态，缺证据就返回 ``None`` 加稳定 reason，
绝不用 0、False 或"数据库里刚好有的行数"填补。

三层分工（对应方案 §2.1）：

1. **纯计算层**（本文件上半部分，无 DB）：只消费调用方已捕获的冻结事实，
   同一组输入必须得到同一个 ``fingerprint``。
2. **读取层**（本文件下半部分，只读连接）：把账本/归档转成冻结事实，遇到缺口
   记 reason 而不是猜。
3. 下游展示/健康接入**不在本模块**：``strategy_health_service._performance_dimension``
   仍是 stub，把它接到本 owner 是一次独立变更（方案 §2.5）。

关键口径（都来自代码核对，不是假设）：

- ``paper_orders.strategy_id == account_id``，因此"某策略的绩效"实际上是
  "该账户在绑定到该版本的周期里的绩效"。
- ``paper_nav`` 只有 ``(account_id, nav_date)`` 唯一键、**没有 cycle_id**，且换周期时
  被删除、归档时被清空 —— 历史 NAV 只存在于 ``paper_archives.snapshot``。所以
  "按日期猜历史周期"是禁止的：同名日期落在两个周期边界上时记 ``ambiguous_cycle_nav``。
- 买入现金流含未平仓成本，所以 ``sell_cash - buy_cash`` **不是**已实现利润；
  现金流与已实现盈亏分成两个指标、两套证据。
- 归档周期不能调用 ``paper_portfolio_read_model.realized_pnl``（它对非活动周期
  fail-closed），只能用归档里冻结的订单行并逐条核对精确身份。
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
from collections.abc import Iterable, Mapping, Sequence

import data_paths

OWNER_VERSION = "strategy-performance-owner-v1"
FACT_SCHEMA_VERSION = "strategy-performance-fact-v1"
METHOD_VERSION = "strategy-performance-method-v1"

STATUS_AVAILABLE = "AVAILABLE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUSES = (STATUS_AVAILABLE, STATUS_PARTIAL, STATUS_UNAVAILABLE)

# 稳定 reason 词表。展示层负责翻译，代码与判据只认这些字符串。
R_NO_EXACT_PIN = "no_exact_pin"
R_CYCLE_ROW_MISSING = "cycle_row_missing"
R_ARCHIVE_MISSING = "archive_missing"
R_ARCHIVE_CORRUPT = "archive_corrupt"
R_ARCHIVE_DUPLICATE = "archive_duplicate"
R_ARCHIVE_FORMAT_UNSUPPORTED = "archive_format_unsupported"
R_AMBIGUOUS_CYCLE_NAV = "ambiguous_cycle_nav"
R_MISSING_OPENING_VALUATION = "missing_opening_valuation"
R_MISSING_CLOSING_VALUATION = "missing_closing_valuation"
R_UNKNOWN_CAPITAL_FLOW = "unknown_capital_flow"
R_INCOMPLETE_COST_BASIS = "incomplete_cost_basis"
R_PARAMETER_BOUNDARY_UNOBSERVABLE = "parameter_boundary_unobservable"
R_PARTIAL_NAV_COVERAGE = "partial_nav_coverage"
R_DEGRADED_QUOTE_QUALITY = "degraded_quote_quality"
R_IDENTITY_STAMP_MISSING = "identity_stamp_missing"
R_IDENTITY_STAMP_MISMATCH = "identity_stamp_mismatch"
R_SESSION_CALENDAR_UNAVAILABLE = "session_calendar_unavailable"
R_NO_OBSERVATION_IN_WINDOW = "no_observation_in_window"
R_CYCLE_LINKING_NOT_DEFINED = "cycle_linking_not_defined"
R_OUTSIDE_WINDOW = "outside_window"

METRIC_TRADE_COUNT = "trade_count"
METRIC_TURNOVER_AMOUNT = "turnover_amount"
METRIC_FEES = "fees"
METRIC_CASH_FLOW = "cash_flow"
METRIC_REALIZED_PNL = "realized_pnl"
METRIC_CYCLE_RETURN = "cycle_return"
METRIC_MAX_DRAWDOWN = "max_drawdown"
METRIC_TIME_WEIGHTED_RETURN = "time_weighted_return"
ALL_METRICS = (
    METRIC_TRADE_COUNT, METRIC_TURNOVER_AMOUNT, METRIC_FEES, METRIC_CASH_FLOW,
    METRIC_REALIZED_PNL, METRIC_CYCLE_RETURN, METRIC_MAX_DRAWDOWN,
    METRIC_TIME_WEIGHTED_RETURN,
)

SOURCE_ARCHIVE = "archive"
SOURCE_ACTIVE = "active"

#: ``paper_nav.quote_status`` 的已确认取值。其它取值不当作"已验证"，而是降级。
QUOTE_VERIFIED = frozenset({"verified"})
QUOTE_DEGRADED = frozenset({"cost_fallback"})


class PerformanceEvidenceError(ValueError):
    """合同违约（不是"缺数据"）。缺数据用 status + reason 表达。"""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _iso_date(value, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise PerformanceEvidenceError(f"{label}_must_be_iso_date") from exc


def _finite(value, label: str):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PerformanceEvidenceError(f"{label}_must_be_finite_number") from exc
    if not math.isfinite(number):
        raise PerformanceEvidenceError(f"{label}_must_be_finite_number")
    return number


@dataclasses.dataclass(frozen=True, slots=True)
class NavPoint:
    nav_date: str
    nav: float
    quote_status: str = "verified"

    def __post_init__(self):
        object.__setattr__(self, "nav_date", _iso_date(self.nav_date, "nav_date"))
        object.__setattr__(self, "nav", _finite(self.nav, "nav"))
        object.__setattr__(self, "quote_status", str(self.quote_status or "unknown"))

    @property
    def verified(self) -> bool:
        return self.quote_status in QUOTE_VERIFIED


@dataclasses.dataclass(frozen=True, slots=True)
class FillRecord:
    fill_date: str
    side: str
    amount: float
    fees: float
    code: str = ""

    def __post_init__(self):
        object.__setattr__(self, "fill_date", _iso_date(self.fill_date, "fill_date"))
        object.__setattr__(self, "side", str(self.side or "").lower())
        if self.side not in ("buy", "sell"):
            raise PerformanceEvidenceError(f"fill_side_unknown:{self.side!r}")
        object.__setattr__(self, "amount", _finite(self.amount, "fill_amount"))
        object.__setattr__(self, "fees", _finite(self.fees, "fill_fees") or 0.0)


@dataclasses.dataclass(frozen=True, slots=True)
class ParameterVersion:
    effective_date: str
    version: str

    def __post_init__(self):
        object.__setattr__(self, "effective_date",
                           _iso_date(self.effective_date, "parameter_effective_date"))
        object.__setattr__(self, "version", str(self.version or ""))


@dataclasses.dataclass(frozen=True, slots=True)
class CycleSlice:
    """一个精确周期绑定下的冻结事实。两侧（活动/归档）形状一致，来源单独标记。"""

    cycle_id: int
    cycle_key: str
    cycle_status: str
    source: str
    started_at: str | None = None
    ended_at: str | None = None
    capital: float | None = None
    nav_points: tuple[NavPoint, ...] = ()
    fills: tuple[FillRecord, ...] = ()
    parameter_versions: tuple[ParameterVersion, ...] = ()
    archive_id: int | None = None
    archive_format: str | None = None
    archive_fingerprint: str | None = None
    excluded_fills: int = 0
    #: 归档周期里由本 owner 复用周期账本 owner 得到的已实现盈亏，`(value, status)`。
    owner_realized_pnl: tuple[float | None, str] | None = None
    #: 归档周期里可核验的卖出盈亏，**带成交日期**：`((fill_date, pnl), ...)`。
    #: 没有日期就无法按观察窗口裁剪，也就无法回答"这一段是否改善"。
    archived_sell_pnl: tuple[tuple[str, float], ...] = ()
    #: 卖出单存在但盈亏无法核验（缺 realized_pnl 或缺执行核验）的笔数。
    sell_pnl_incomplete: int = 0
    reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.source not in (SOURCE_ARCHIVE, SOURCE_ACTIVE):
            raise PerformanceEvidenceError(f"cycle_slice_source_unknown:{self.source!r}")
        object.__setattr__(self, "cycle_id", int(self.cycle_id))
        object.__setattr__(self, "cycle_key", str(self.cycle_key or ""))
        object.__setattr__(self, "capital", _finite(self.capital, "cycle_capital"))
        object.__setattr__(self, "nav_points",
                           tuple(sorted(self.nav_points, key=lambda point: point.nav_date)))
        object.__setattr__(self, "fills",
                           tuple(sorted(self.fills, key=lambda fill: fill.fill_date)))
        object.__setattr__(self, "parameter_versions", tuple(self.parameter_versions))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons or ()))
        object.__setattr__(self, "excluded_fills", int(self.excluded_fills or 0))
        object.__setattr__(self, "sell_pnl_incomplete", int(self.sell_pnl_incomplete or 0))
        dated = []
        for pair in self.archived_sell_pnl or ():
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise PerformanceEvidenceError("archived_sell_pnl_requires_fill_date")
            dated.append((_iso_date(pair[0], "archived_sell_pnl_date"),
                          _finite(pair[1], "archived_sell_pnl")))
        object.__setattr__(self, "archived_sell_pnl",
                           tuple(sorted(dated, key=lambda item: item[0])))

    def projection(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "cycle_key": self.cycle_key,
            "cycle_status": self.cycle_status, "source": self.source,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "capital": self.capital,
            "nav_points": [[point.nav_date, point.nav, point.quote_status]
                           for point in self.nav_points],
            "fill_count": len(self.fills), "excluded_fills": self.excluded_fills,
            "sell_pnl_incomplete": self.sell_pnl_incomplete,
            "parameter_versions": [[item.effective_date, item.version]
                                   for item in self.parameter_versions],
            "archive_id": self.archive_id, "archive_format": self.archive_format,
            "archive_fingerprint": self.archive_fingerprint,
            "owner_realized_pnl": None if self.owner_realized_pnl is None
            else [self.owner_realized_pnl[0], self.owner_realized_pnl[1]],
            "archived_sell_pnl": [[day, value] for day, value in self.archived_sell_pnl],
            "reasons": list(self.reasons),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class MetricFact:
    name: str
    status: str
    value: object = None
    unit: str | None = None
    method: str | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.name not in ALL_METRICS:
            raise PerformanceEvidenceError(f"unknown_metric:{self.name}")
        if self.status not in STATUSES:
            raise PerformanceEvidenceError(f"unknown_metric_status:{self.status}")
        if self.status == STATUS_UNAVAILABLE and self.value is not None:
            # 不可用就是不可用：带一个数值出去，下游一定会把它当结论。
            raise PerformanceEvidenceError(f"unavailable_metric_must_not_carry_a_value:{self.name}")
        if self.status != STATUS_UNAVAILABLE and self.value is None:
            raise PerformanceEvidenceError(f"available_metric_requires_a_value:{self.name}")
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons or ()))

    def projection(self) -> dict:
        return {"status": self.status, "value": self.value, "unit": self.unit,
                "method": self.method, "reasons": list(self.reasons)}


@dataclasses.dataclass(frozen=True, slots=True)
class Coverage:
    expected_nav_days: int | None
    observed_nav_days: int
    missing_nav_days: int | None
    excluded_fills: int
    included_fills: int
    ambiguous_nav_days: int
    degraded_nav_days: int
    cycles_included: int
    cycles_excluded: int
    reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.expected_nav_days is not None and self.expected_nav_days < self.observed_nav_days:
            raise PerformanceEvidenceError("coverage_expected_below_observed")
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons or ()))

    def projection(self) -> dict:
        return {"expected_nav_days": self.expected_nav_days,
                "observed_nav_days": self.observed_nav_days,
                "missing_nav_days": self.missing_nav_days,
                "excluded_fills": self.excluded_fills,
                "included_fills": self.included_fills,
                "ambiguous_nav_days": self.ambiguous_nav_days,
                "degraded_nav_days": self.degraded_nav_days,
                "cycles_included": self.cycles_included,
                "cycles_excluded": self.cycles_excluded,
                "reasons": list(self.reasons)}


@dataclasses.dataclass(frozen=True, slots=True)
class StrategyPerformanceFact:
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    observation_start: str
    observation_end: str
    status: str
    metrics: Mapping
    coverage: Coverage
    cycle_slices: tuple[CycleSlice, ...]
    source_watermark: Mapping
    blocking_reasons: tuple[str, ...] = ()
    fingerprint: str = ""
    owner_version: str = OWNER_VERSION
    schema_version: str = FACT_SCHEMA_VERSION
    method_version: str = METHOD_VERSION

    def metric(self, name: str) -> MetricFact:
        return self.metrics[name]

    def projection(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "owner_version": self.owner_version,
            "method_version": self.method_version,
            "fingerprint": self.fingerprint,
            "exact_identity": {"strategy_id": self.strategy_id,
                               "strategy_version": self.strategy_version,
                               "strategy_checksum": self.strategy_checksum},
            "observation_start": self.observation_start,
            "observation_end": self.observation_end,
            "status": self.status,
            "metrics": {name: fact.projection() for name, fact in self.metrics.items()},
            "coverage": self.coverage.projection(),
            "cycle_slices": [item.projection() for item in self.cycle_slices],
            "source_watermark": dict(self.source_watermark),
            "blocking_reasons": list(self.blocking_reasons),
        }

    def fingerprint_material(self) -> dict:
        material = self.projection()
        material.pop("fingerprint", None)
        return material


@dataclasses.dataclass(frozen=True, slots=True)
class PerformanceRequest:
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    observation_start: str
    observation_end: str
    requested_metrics: tuple[str, ...] = ALL_METRICS
    parameter_version_id: str | None = None
    ai_policy_fingerprint: str | None = None

    def __post_init__(self):
        identity = str(self.strategy_id or "").strip()
        if not identity:
            raise PerformanceEvidenceError("strategy_id_required")
        object.__setattr__(self, "strategy_id", identity)
        try:
            version = int(self.strategy_version)
        except (TypeError, ValueError) as exc:
            raise PerformanceEvidenceError("strategy_version_must_be_an_integer") from exc
        if version < 1:
            raise PerformanceEvidenceError("strategy_version_must_be_positive")
        object.__setattr__(self, "strategy_version", version)
        checksum = str(self.strategy_checksum or "")
        if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
            raise PerformanceEvidenceError("strategy_checksum_must_be_sha256")
        object.__setattr__(self, "strategy_checksum", checksum)
        start = _iso_date(self.observation_start, "observation_start")
        end = _iso_date(self.observation_end, "observation_end")
        if start >= end:
            raise PerformanceEvidenceError("observation_window_must_be_forward")
        object.__setattr__(self, "observation_start", start)
        object.__setattr__(self, "observation_end", end)
        metrics = tuple(self.requested_metrics or ALL_METRICS)
        if not metrics:
            raise PerformanceEvidenceError("requested_metrics_must_not_be_empty")
        unknown = [name for name in metrics if name not in ALL_METRICS]
        if unknown:
            raise PerformanceEvidenceError(f"unknown_metric:{unknown[0]}")
        object.__setattr__(self, "requested_metrics", metrics)


def _window_dates(start: str, end: str) -> tuple[str, str]:
    return start, end


def _in_window(day: str, start: str, end: str) -> bool:
    return start <= day < end


def _clip(slices: Sequence[CycleSlice], start: str, end: str) -> tuple[list[CycleSlice], int]:
    """按观察窗口裁剪每个周期的 NAV/fill；完全落在窗外的周期单独计数。"""
    kept: list[CycleSlice] = []
    excluded = 0
    for item in slices:
        nav_points = tuple(point for point in item.nav_points
                           if _in_window(point.nav_date, start, end))
        fills = tuple(fill for fill in item.fills if _in_window(fill.fill_date, start, end))
        # 盈亏也按同一半开窗口裁剪：没有这一步，窗口外的已实现收益会被算进窗口内。
        archived_sell_pnl = tuple(
            (day, value) for day, value in item.archived_sell_pnl
            if _in_window(day, start, end))
        cycle_started = (item.started_at or "")[:10]
        cycle_ended = (item.ended_at or "")[:10]
        overlaps = (
            (not cycle_started or cycle_started < end)
            and (not cycle_ended or cycle_ended >= start)
        )
        if not nav_points and not fills and not overlaps and not item.reasons:
            excluded += 1
            continue
        kept.append(dataclasses.replace(
            item, nav_points=nav_points, fills=fills, archived_sell_pnl=archived_sell_pnl))
    return kept, excluded


def _trade_metrics(slices: Sequence[CycleSlice], requested) -> dict:
    fills = [fill for item in slices for fill in item.fills]
    excluded = sum(item.excluded_fills for item in slices)
    facts = {}
    if METRIC_TRADE_COUNT in requested:
        reasons = (R_IDENTITY_STAMP_MISSING,) if excluded else ()
        facts[METRIC_TRADE_COUNT] = MetricFact(
            name=METRIC_TRADE_COUNT,
            status=STATUS_PARTIAL if excluded else STATUS_AVAILABLE,
            value=len(fills), unit="fills", method="verified_fill_rows",
            reasons=reasons)
    if METRIC_TURNOVER_AMOUNT in requested:
        facts[METRIC_TURNOVER_AMOUNT] = MetricFact(
            name=METRIC_TURNOVER_AMOUNT, status=STATUS_AVAILABLE,
            value=round(sum(fill.amount for fill in fills), 2), unit="CNY",
            method="sum_of_verified_fill_amounts")
    if METRIC_FEES in requested:
        facts[METRIC_FEES] = MetricFact(
            name=METRIC_FEES, status=STATUS_AVAILABLE,
            value=round(sum(fill.fees for fill in fills), 2), unit="CNY",
            method="sum_of_verified_fill_fees")
    return facts


def _cash_flow_metric(slices: Sequence[CycleSlice], requested) -> dict:
    if METRIC_CASH_FLOW not in requested:
        return {}
    buys = [fill for item in slices for fill in item.fills if fill.side == "buy"]
    sells = [fill for item in slices for fill in item.fills if fill.side == "sell"]
    buy_outflow = round(sum(fill.amount + fill.fees for fill in buys), 2)
    sell_inflow = round(sum(fill.amount - fill.fees for fill in sells), 2)
    capital = sum(item.capital or 0.0 for item in slices)
    return {METRIC_CASH_FLOW: MetricFact(
        name=METRIC_CASH_FLOW, status=STATUS_AVAILABLE,
        value={"buy_outflow": buy_outflow, "sell_inflow": sell_inflow,
               "net_external_flow": 0.0, "declared_capital": round(capital, 2)},
        unit="CNY", method="verified_fill_cash_flow_not_profit")}


def _realized_pnl_metric(slices: Sequence[CycleSlice], requested) -> tuple[dict, list[str]]:
    if METRIC_REALIZED_PNL not in requested:
        return {}, []
    total = 0.0
    reasons: list[str] = []
    unavailable = False
    for item in slices:
        if item.owner_realized_pnl is not None:
            value, status = item.owner_realized_pnl
            if value is None or status != STATUS_AVAILABLE:
                unavailable = True
                reasons.append(R_INCOMPLETE_COST_BASIS)
                continue
            total += float(value)
            continue
        if item.archived_sell_pnl:
            total += sum(value for _day, value in item.archived_sell_pnl)
            continue
        if item.sell_pnl_incomplete or any(fill.side == "sell" for fill in item.fills):
            # 有卖出但没有可核验的成本/盈亏证据：不能当成 0。
            unavailable = True
            reasons.append(R_INCOMPLETE_COST_BASIS)
    if unavailable and not total:
        return {METRIC_REALIZED_PNL: MetricFact(
            name=METRIC_REALIZED_PNL, status=STATUS_UNAVAILABLE,
            method="cost_basis_required", reasons=tuple(sorted(set(reasons))))}, reasons
    return {METRIC_REALIZED_PNL: MetricFact(
        name=METRIC_REALIZED_PNL,
        status=STATUS_PARTIAL if unavailable else STATUS_AVAILABLE,
        value=round(total, 2), unit="CNY",
        method="ledger_realized_pnl_with_exact_identity",
        reasons=tuple(sorted(set(reasons))))}, reasons


def _ambiguous_days(slices: Sequence[CycleSlice]) -> set[str]:
    """同一账户同一天出现在两个周期边界上时，日频数据不足以分配该日。"""
    seen: dict[str, int] = {}
    for item in slices:
        cycle_started = (item.started_at or "")[:10]
        cycle_ended = (item.ended_at or "")[:10]
        for day in {cycle_started, cycle_ended} - {""}:
            seen[day] = seen.get(day, 0) + 1
    return {day for day, count in seen.items() if count > 1}


def _cycle_returns_and_nav(slices: Sequence[CycleSlice], requested) -> tuple[dict, set[str]]:
    """逐周期收益：不连乘、不跨周期拼一条净值线（方案 §2.3）。"""
    returns: dict[str, float] = {}
    drawdowns: dict[str, float] = {}
    nav_change: dict[str, float] = {}
    reasons: set[str] = set()
    ambiguous = _ambiguous_days(slices)
    for item in slices:
        points = [point for point in item.nav_points if point.nav_date not in ambiguous]
        if ambiguous & {point.nav_date for point in item.nav_points}:
            reasons.add(R_AMBIGUOUS_CYCLE_NAV)
        opening = item.capital
        if opening is None or opening <= 0:
            reasons.add(R_MISSING_OPENING_VALUATION)
            continue
        if not points:
            reasons.add(R_MISSING_CLOSING_VALUATION)
            continue
        closing = points[-1].nav
        if closing is None:
            reasons.add(R_MISSING_CLOSING_VALUATION)
            continue
        nav_change[item.cycle_key] = round(closing - opening, 2)
        returns[item.cycle_key] = round(closing / opening - 1.0, 8)
        # 期初资本本身就是第一个估值点，所以 1 个 NAV 也能算回撤；
        # 但少于 2 个 NAV 观测看不到区间内的路径，只能给出 PARTIAL。
        drawdowns[item.cycle_key] = round(
            _max_drawdown([opening] + [point.nav for point in points]), 8)
        if len(points) < 2:
            reasons.add(R_PARTIAL_NAV_COVERAGE)
        if any(not point.verified for point in points):
            reasons.add(R_DEGRADED_QUOTE_QUALITY)
    facts: dict = {}
    if METRIC_CYCLE_RETURN in requested:
        if not returns:
            facts[METRIC_CYCLE_RETURN] = MetricFact(
                name=METRIC_CYCLE_RETURN, status=STATUS_UNAVAILABLE,
                method="per_cycle_nav_series", reasons=tuple(sorted(reasons)) or
                (R_NO_OBSERVATION_IN_WINDOW,))
        else:
            blocked = {R_MISSING_OPENING_VALUATION, R_MISSING_CLOSING_VALUATION,
                       R_AMBIGUOUS_CYCLE_NAV} & reasons
            facts[METRIC_CYCLE_RETURN] = MetricFact(
                name=METRIC_CYCLE_RETURN,
                status=STATUS_PARTIAL if (blocked or R_DEGRADED_QUOTE_QUALITY in reasons)
                else STATUS_AVAILABLE,
                value={"per_cycle": returns, "per_cycle_nav_change": nav_change},
                unit="ratio", method="per_cycle_valuations_never_compounded",
                reasons=tuple(sorted(reasons)))
    if METRIC_MAX_DRAWDOWN in requested:
        if not drawdowns:
            facts[METRIC_MAX_DRAWDOWN] = MetricFact(
                name=METRIC_MAX_DRAWDOWN, status=STATUS_UNAVAILABLE,
                method="per_cycle_nav_series",
                reasons=tuple(sorted(reasons)) or (R_PARTIAL_NAV_COVERAGE,))
        else:
            facts[METRIC_MAX_DRAWDOWN] = MetricFact(
                name=METRIC_MAX_DRAWDOWN,
                status=STATUS_PARTIAL if R_PARTIAL_NAV_COVERAGE in reasons else STATUS_AVAILABLE,
                value={"per_cycle": drawdowns},
                unit="ratio", method="per_cycle_nav_series_never_max_across_cycles",
                reasons=tuple(sorted(reasons)))
    return facts, reasons


def _max_drawdown(series: Sequence[float]) -> float:
    peak = series[0]
    worst = 0.0
    for value in series:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _time_weighted_return(slices: Sequence[CycleSlice], requested) -> dict:
    if METRIC_TIME_WEIGHTED_RETURN not in requested:
        return {}
    # 周期拥有经济资本、每个周期重置本金；把逐周期收益连乘成"账户真实累计收益"
    # 需要先定义归一化与连接规则，本 owner 不擅自发明它（方案 §2.3）。
    return {METRIC_TIME_WEIGHTED_RETURN: MetricFact(
        name=METRIC_TIME_WEIGHTED_RETURN, status=STATUS_UNAVAILABLE,
        method="requires_a_reviewed_unit_index_linking_rule",
        reasons=(R_CYCLE_LINKING_NOT_DEFINED,))}


def _parameter_scoped_reasons(slices: Sequence[CycleSlice],
                              parameter_version_id: str | None) -> set[str]:
    """参数边界是否可观测。

    只有日期粒度的 ``effective_date``：同一生效日出现多个参数版本，说明当天参数变过，
    日频 NAV 无法把两段参数分开。
    """
    reasons: set[str] = set()
    for item in slices:
        by_day: dict[str, int] = {}
        for parameter in item.parameter_versions:
            by_day[parameter.effective_date] = by_day.get(parameter.effective_date, 0) + 1
        if any(count > 1 for count in by_day.values()):
            reasons.add(R_PARAMETER_BOUNDARY_UNOBSERVABLE)
        if parameter_version_id is not None and item.parameter_versions:
            if parameter_version_id not in {item.version for item in item.parameter_versions}:
                reasons.add(R_PARAMETER_BOUNDARY_UNOBSERVABLE)
    return reasons


def build_performance_fact(
    request: PerformanceRequest,
    *,
    slices: Iterable[CycleSlice],
    expected_nav_days: int | None = None,
    source_watermark: Mapping | None = None,
) -> StrategyPerformanceFact:
    """把冻结事实装配成一个可核验的绩效事实。

    纯函数：无 DB、无网络、无机器时钟。相同输入必定得到相同 ``fingerprint``；
    源更正、方法或口径变化必定改变它。
    """
    if not isinstance(request, PerformanceRequest):
        raise PerformanceEvidenceError("performance_request_required")
    window_start, window_end = _window_dates(request.observation_start,
                                             request.observation_end)
    supplied = list(slices or ())
    if any(not isinstance(item, CycleSlice) for item in supplied):
        raise PerformanceEvidenceError("cycle_slices_must_be_cycle_slice_objects")
    kept, cycles_excluded = _clip(supplied, window_start, window_end)
    kept.sort(key=lambda item: (item.started_at or "", item.cycle_id))

    cycle_reasons = {reason for item in kept for reason in item.reasons}
    parameter_reasons = _parameter_scoped_reasons(kept, request.parameter_version_id)

    metrics: dict[str, MetricFact] = {}
    metrics.update(_trade_metrics(kept, request.requested_metrics))
    metrics.update(_cash_flow_metric(kept, request.requested_metrics))
    pnl_facts, pnl_reasons = _realized_pnl_metric(kept, request.requested_metrics)
    metrics.update(pnl_facts)
    nav_facts, nav_reasons = _cycle_returns_and_nav(kept, request.requested_metrics)
    metrics.update(nav_facts)
    metrics.update(_time_weighted_return(kept, request.requested_metrics))

    ambiguous = _ambiguous_days(kept)
    observed_days = sum(len(item.nav_points) for item in kept)
    degraded_days = sum(1 for item in kept for point in item.nav_points if not point.verified)
    coverage_reasons: list[str] = []
    if expected_nav_days is None:
        # 没有交易日历 owner 的注入就不编分母：用行数当分母正是要避免的错误。
        coverage_reasons.append(R_SESSION_CALENDAR_UNAVAILABLE)
    coverage = Coverage(
        expected_nav_days=None if expected_nav_days is None else int(expected_nav_days),
        observed_nav_days=observed_days,
        missing_nav_days=None if expected_nav_days is None
        else max(0, int(expected_nav_days) - observed_days),
        excluded_fills=sum(item.excluded_fills for item in kept),
        included_fills=sum(len(item.fills) for item in kept),
        ambiguous_nav_days=len(ambiguous),
        degraded_nav_days=degraded_days,
        cycles_included=len(kept),
        cycles_excluded=cycles_excluded,
        reasons=tuple(coverage_reasons),
    )

    if parameter_reasons:
        metrics = _apply_parameter_block(metrics, parameter_reasons)
    if not kept:
        # 一个可观测周期都没有时，"0 笔成交 / 0 元现金流"不是事实而是编造：
        # 这类指标必须整体不可用。
        metrics = _no_observation_block(
            metrics, R_NO_EXACT_PIN if not supplied else R_OUTSIDE_WINDOW)

    blocking = set(cycle_reasons) | set(pnl_reasons) | set(nav_reasons)
    blocking |= set(parameter_reasons) | set(coverage.reasons)
    if not kept:
        blocking.add(R_NO_EXACT_PIN if not supplied else R_OUTSIDE_WINDOW)

    statuses = [fact.status for fact in metrics.values()]
    if not statuses or all(item == STATUS_UNAVAILABLE for item in statuses):
        status = STATUS_UNAVAILABLE
    elif any(item == STATUS_AVAILABLE for item in statuses) and STATUS_UNAVAILABLE not in statuses:
        status = STATUS_PARTIAL if any(item == STATUS_PARTIAL for item in statuses) \
            else STATUS_AVAILABLE
    else:
        status = STATUS_PARTIAL

    watermark = {
        "cycles": [[item.cycle_id, item.archive_id, item.archive_fingerprint,
                    item.source] for item in kept],
        "window": [window_start, window_end],
        "parameter_version_id": request.parameter_version_id,
        "ai_policy_fingerprint": request.ai_policy_fingerprint,
        "expected_nav_days": coverage.expected_nav_days,
        "owner_version": OWNER_VERSION,
        "method_version": METHOD_VERSION,
    }
    watermark.update({str(key): value for key, value in (source_watermark or {}).items()})

    material = {
        "schema_version": FACT_SCHEMA_VERSION,
        "owner_version": OWNER_VERSION,
        "method_version": METHOD_VERSION,
        "exact_identity": {"strategy_id": request.strategy_id,
                           "strategy_version": request.strategy_version,
                           "strategy_checksum": request.strategy_checksum},
        "observation_start": window_start, "observation_end": window_end,
        "status": status,
        "metrics": {name: fact.projection() for name, fact in sorted(metrics.items())},
        "coverage": coverage.projection(),
        "cycle_slices": [item.projection() for item in kept],
        "source_watermark": watermark,
        "blocking_reasons": sorted(blocking),
    }
    return StrategyPerformanceFact(
        strategy_id=request.strategy_id, strategy_version=request.strategy_version,
        strategy_checksum=request.strategy_checksum,
        observation_start=window_start, observation_end=window_end, status=status,
        metrics=dict(sorted(metrics.items())), coverage=coverage,
        cycle_slices=tuple(kept), source_watermark=watermark,
        blocking_reasons=tuple(sorted(blocking)), fingerprint=fingerprint(material),
    )


def _no_observation_block(metrics: Mapping, reason: str) -> dict:
    """没有可观测周期：所有指标不可用。零值只有在真的观测到一个周期后才算事实。"""
    return {name: MetricFact(name=name, status=STATUS_UNAVAILABLE,
                             method="no_observable_cycle_in_window",
                             reasons=(reason,))
            for name in metrics}


def _apply_parameter_block(metrics: Mapping, reasons: set[str]) -> dict:
    """参数边界不可观测时，参数级指标整体不可用；其它指标保留。"""
    blocked = dict(metrics)
    for name in (METRIC_CYCLE_RETURN, METRIC_MAX_DRAWDOWN, METRIC_TIME_WEIGHTED_RETURN):
        if name in blocked:
            blocked[name] = MetricFact(
                name=name, status=STATUS_UNAVAILABLE,
                method="parameter_scoped_metric_requires_observable_boundary",
                reasons=tuple(sorted(reasons)))
    return blocked


# --------------------------------------------------------------------------- #
# 读取层：只读账本 → 冻结事实。这里允许读库，但绝不建表、不写库、不读 current/latest。
# --------------------------------------------------------------------------- #

LEDGER_FILENAME = "paper_trading.sqlite3"
#: 与 ``paper_cycle_service.ARCHIVE_FORMAT`` 保持同一口径；test_strategy_performance
#: 断言两者相等，漂移会在测试里立刻暴露（不在纯计算层引入仓储依赖）。
_ARCHIVE_FORMAT = "compact-ledger-v2"
_SNAPSHOT_TABLES = ("paper_accounts", "paper_orders", "paper_fills", "paper_nav",
                    "paper_parameter_versions")


class LedgerUnavailable(RuntimeError):
    """账本文件不在或不可读。与"缺证据"不同：这是环境问题。"""


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)).fetchone()
    return row is not None


def connect_readonly(db_path: str | None = None):
    """只读连接（``mode=ro`` + ``query_only``）：不迁移、不建表、不取写锁。"""
    target = str(db_path or data_paths.data_path(LEDGER_FILENAME))
    if not os.path.exists(target):
        raise LedgerUnavailable(f"账本不存在: {target}")

    @contextlib.contextmanager
    def _open():
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    return _open()


def resolve_exact_pins(conn, *, strategy_id: str, strategy_version: int,
                       strategy_checksum: str) -> list[dict]:
    """该精确版本的**全部**周期绑定。没有绑定就返回空，绝不回落到 head/latest。"""
    if not _table_exists(conn, "paper_cycle_strategy_versions"):
        return []
    rows = conn.execute(
        "SELECT cycle_id, account_id, strategy_id, strategy_version, strategy_checksum,"
        " bound_at FROM paper_cycle_strategy_versions"
        " WHERE strategy_id=? AND strategy_version=? AND strategy_checksum=?"
        " ORDER BY cycle_id",
        (str(strategy_id), int(strategy_version), str(strategy_checksum)),
    ).fetchall()
    return [dict(row) for row in rows]


def _cycle_row(conn, cycle_id: int):
    if not _table_exists(conn, "paper_cycles"):
        return None
    row = conn.execute(
        "SELECT id, cycle_key, status, capital, started_at, ended_at"
        " FROM paper_cycles WHERE id=?", (int(cycle_id),)).fetchone()
    return dict(row) if row is not None else None


def load_archive_snapshot(conn, cycle_id: int):
    """按 ``cycle_id`` 精确读归档快照，**不按日期猜周期**。"""
    if not _table_exists(conn, "paper_archives"):
        return None, [R_ARCHIVE_MISSING], {}
    rows = conn.execute(
        "SELECT id, snapshot, created_at FROM paper_archives WHERE cycle_id=? ORDER BY id",
        (int(cycle_id),)).fetchall()
    if not rows:
        return None, [R_ARCHIVE_MISSING], {}
    reasons: list[str] = []
    if len(rows) > 1:
        # 归档表对 cycle_id 没有唯一约束：重复来源只取最新一份，但必须留痕。
        reasons.append(R_ARCHIVE_DUPLICATE)
    row = rows[-1]
    meta = {"archive_id": int(row["id"])}
    try:
        snapshot = json.loads(row["snapshot"])
    except (TypeError, ValueError):
        return None, reasons + [R_ARCHIVE_CORRUPT], meta
    if not isinstance(snapshot, dict):
        return None, reasons + [R_ARCHIVE_CORRUPT], meta
    archive_format = str(snapshot.get("_archive_format") or "")
    meta["archive_format"] = archive_format or None
    if archive_format != _ARCHIVE_FORMAT:
        return None, reasons + [R_ARCHIVE_FORMAT_UNSUPPORTED], meta
    return snapshot, reasons, meta


def _snapshot_rows(snapshot, table: str) -> list[dict]:
    value = (snapshot or {}).get(table)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _stamp_problem(record: Mapping, request: PerformanceRequest) -> str:
    """订单 stamp 必须与请求的精确身份一致。缺 stamp 的旧行不自动属于当前版本。"""
    strategy_id = record.get("strategy_id")
    version = record.get("strategy_version")
    checksum = record.get("strategy_checksum")
    if not strategy_id or version in (None, "") or not checksum:
        return R_IDENTITY_STAMP_MISSING
    try:
        same = (str(strategy_id) == request.strategy_id
                and int(version) == request.strategy_version
                and str(checksum) == request.strategy_checksum)
    except (TypeError, ValueError):
        return R_IDENTITY_STAMP_MISSING
    return "" if same else R_IDENTITY_STAMP_MISMATCH


def _nav_points(rows: Iterable[Mapping], account_id: str) -> tuple[NavPoint, ...]:
    points = []
    for row in rows:
        if str(row.get("account_id") or "") != account_id:
            continue
        nav_date = str(row.get("nav_date") or "")[:10]
        nav = row.get("nav")
        if not nav_date or nav in (None, ""):
            continue
        try:
            points.append(NavPoint(nav_date=nav_date, nav=nav,
                                   quote_status=str(row.get("quote_status") or "unknown")))
        except PerformanceEvidenceError:
            continue
    return tuple(points)


def _archive_slice(pin: Mapping, request: PerformanceRequest, cycle: Mapping | None,
                   snapshot, reasons: list[str], meta: Mapping) -> CycleSlice:
    account_id = str(pin["account_id"])
    cycle_id = int(pin["cycle_id"])
    slice_reasons = list(reasons)
    nav_rows = _snapshot_rows(snapshot, "paper_nav")
    orders = [row for row in _snapshot_rows(snapshot, "paper_orders")
              if str(row.get("account_id") or "") == account_id]
    orders_by_id = {row.get("id"): row for row in orders}
    fills: list[FillRecord] = []
    excluded = 0
    for row in _snapshot_rows(snapshot, "paper_fills"):
        if str(row.get("account_id") or "") != account_id:
            continue
        order = orders_by_id.get(row.get("order_id"))
        if order is None:
            excluded += 1
            slice_reasons.append(R_IDENTITY_STAMP_MISSING)
            continue
        problem = _stamp_problem(order, request)
        if problem:
            excluded += 1
            slice_reasons.append(problem)
            continue
        if order.get("cycle_id") not in (None, "", cycle_id):
            excluded += 1
            slice_reasons.append(R_IDENTITY_STAMP_MISMATCH)
            continue
        try:
            fills.append(FillRecord(fill_date=str(row.get("fill_date") or "")[:10],
                                    side=str(row.get("side") or ""),
                                    amount=row.get("amount"), fees=row.get("fees"),
                                    code=str(row.get("code") or "")))
        except PerformanceEvidenceError:
            excluded += 1
            slice_reasons.append(R_INCOMPLETE_COST_BASIS)
    sell_pnl: list[tuple[str, float]] = []
    sell_pnl_incomplete = 0
    # 每张卖单的成交日期集合：跨日分批成交无法把一份订单级盈亏拆到日，必须放弃。
    fill_dates_by_order: dict[object, set[str]] = {}
    for row in _snapshot_rows(snapshot, "paper_fills"):
        if str(row.get("account_id") or "") != account_id:
            continue
        day = str(row.get("fill_date") or "")[:10]
        if day:
            fill_dates_by_order.setdefault(row.get("order_id"), set()).add(day)
    for order in orders:
        if str(order.get("side") or "").lower() != "sell":
            continue
        if _stamp_problem(order, request):
            continue
        value = order.get("realized_pnl")
        if value in (None, ""):
            sell_pnl_incomplete += 1
            slice_reasons.append(R_INCOMPLETE_COST_BASIS)
            continue
        if int(order.get("execution_verified") or 0) != 1:
            # 没有执行核验的卖出盈利不能当作已实现事实。
            sell_pnl_incomplete += 1
            slice_reasons.append(R_INCOMPLETE_COST_BASIS)
            continue
        days = fill_dates_by_order.get(order.get("id")) or set()
        if len(days) != 1:
            # 0 天（找不到成交日）或 >1 天（跨日分批）都无法可靠归日。
            sell_pnl_incomplete += 1
            slice_reasons.append(R_INCOMPLETE_COST_BASIS)
            continue
        try:
            sell_pnl.append((next(iter(days)), float(value)))
        except (TypeError, ValueError):
            sell_pnl_incomplete += 1
            slice_reasons.append(R_INCOMPLETE_COST_BASIS)
    account_rows = [row for row in _snapshot_rows(snapshot, "paper_accounts")
                    if str(row.get("id") or "") == account_id]
    capital = None
    if account_rows:
        capital = account_rows[0].get("initial_cash")
    cycle_capital = (cycle or {}).get("capital")
    if capital is None:
        capital = cycle_capital
    elif cycle_capital is not None and abs(float(capital) - float(cycle_capital)) > 0.005:
        # 账户初始资金与周期资本不一致：资金流不再是"周期内零外部流入"。
        slice_reasons.append(R_UNKNOWN_CAPITAL_FLOW)
    parameter_versions = []
    for row in _snapshot_rows(snapshot, "paper_parameter_versions"):
        if str(row.get("account_id") or "") != account_id:
            continue
        if row.get("cycle_id") not in (None, "", cycle_id):
            continue
        try:
            parameter_versions.append(ParameterVersion(
                effective_date=str(row.get("effective_date") or "")[:10],
                version=str(row.get("version") or "")))
        except PerformanceEvidenceError:
            continue
    nav_points = _nav_points(nav_rows, account_id)
    archive_fingerprint = fingerprint({
        "cycle_id": cycle_id, "account_id": account_id,
        "nav_points": [[p.nav_date, p.nav, p.quote_status] for p in nav_points],
        "fill_count": len(fills), "sell_pnl": sell_pnl,
        "capital": capital, "archive_format": meta.get("archive_format"),
    })
    return CycleSlice(
        cycle_id=cycle_id, cycle_key=str((cycle or {}).get("cycle_key") or ""),
        cycle_status=str((cycle or {}).get("status") or "archived"),
        source=SOURCE_ARCHIVE, started_at=(cycle or {}).get("started_at"),
        ended_at=(cycle or {}).get("ended_at"), capital=capital,
        nav_points=nav_points, fills=tuple(fills),
        parameter_versions=tuple(parameter_versions),
        archive_id=meta.get("archive_id"), archive_format=meta.get("archive_format"),
        archive_fingerprint=archive_fingerprint, excluded_fills=excluded,
        archived_sell_pnl=tuple(sell_pnl), sell_pnl_incomplete=sell_pnl_incomplete,
        reasons=tuple(sorted(set(slice_reasons))),
    )


def _active_slice(pin: Mapping, request: PerformanceRequest, cycle: Mapping,
                  conn) -> CycleSlice:
    account_id = str(pin["account_id"])
    cycle_id = int(pin["cycle_id"])
    slice_reasons: list[str] = []
    nav_points: tuple[NavPoint, ...] = ()
    if _table_exists(conn, "paper_nav"):
        nav_points = _nav_points(
            [dict(row) for row in conn.execute(
                "SELECT account_id, nav_date, nav, quote_status FROM paper_nav"
                " WHERE account_id=? ORDER BY nav_date", (account_id,)).fetchall()],
            account_id)
    fills: list[FillRecord] = []
    excluded = 0
    if _table_exists(conn, "paper_orders") and _table_exists(conn, "paper_fills"):
        rows = conn.execute(
            "SELECT f.fill_date, f.side, f.amount, f.fees, f.code,"
            " o.strategy_id, o.strategy_version, o.strategy_checksum, o.cycle_id"
            " FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id"
            " WHERE f.account_id=? ORDER BY f.fill_date", (account_id,)).fetchall()
        for row in rows:
            if row["cycle_id"] not in (None, "", cycle_id):
                continue
            problem = _stamp_problem(dict(row), request)
            if problem:
                excluded += 1
                slice_reasons.append(problem)
                continue
            try:
                fills.append(FillRecord(fill_date=str(row["fill_date"])[:10],
                                        side=str(row["side"]), amount=row["amount"],
                                        fees=row["fees"], code=str(row["code"] or "")))
            except PerformanceEvidenceError:
                excluded += 1
                slice_reasons.append(R_INCOMPLETE_COST_BASIS)
    parameter_versions = []
    if _table_exists(conn, "paper_parameter_versions"):
        for row in conn.execute(
                "SELECT effective_date, version FROM paper_parameter_versions"
                " WHERE cycle_id=? AND account_id=?", (cycle_id, account_id)).fetchall():
            try:
                parameter_versions.append(ParameterVersion(
                    effective_date=str(row["effective_date"])[:10],
                    version=str(row["version"] or "")))
            except PerformanceEvidenceError:
                continue
    capital = cycle.get("capital")
    account_row = None
    if _table_exists(conn, "paper_accounts"):
        row = conn.execute(
            "SELECT initial_cash, cycle_id FROM paper_accounts WHERE id=?",
            (account_id,)).fetchone()
        account_row = dict(row) if row is not None else None
    if account_row and account_row.get("cycle_id") == cycle_id:
        if capital is not None and account_row.get("initial_cash") is not None \
                and abs(float(account_row["initial_cash"]) - float(capital)) > 0.005:
            slice_reasons.append(R_UNKNOWN_CAPITAL_FLOW)
        capital = capital if capital is not None else account_row.get("initial_cash")
    owner_pnl = _owner_realized_pnl(conn, cycle_id, account_id, cycle, request)
    if owner_pnl is not None and owner_pnl[0] is None:
        slice_reasons.append(R_INCOMPLETE_COST_BASIS)
    return CycleSlice(
        cycle_id=cycle_id, cycle_key=str(cycle.get("cycle_key") or ""),
        cycle_status=str(cycle.get("status") or ""), source=SOURCE_ACTIVE,
        started_at=cycle.get("started_at"), ended_at=cycle.get("ended_at"),
        capital=capital, nav_points=nav_points, fills=tuple(fills),
        parameter_versions=tuple(parameter_versions), excluded_fills=excluded,
        owner_realized_pnl=owner_pnl, reasons=tuple(sorted(set(slice_reasons))),
    )


def _asof_day(cycle: Mapping, request: PerformanceRequest) -> dt.date:
    """观察窗口的最后一天（窗口右开），并夹到周期结束日；不读机器时钟。"""
    last = dt.date.fromisoformat(request.observation_end) - dt.timedelta(days=1)
    ended = str(cycle.get("ended_at") or "")[:10]
    if ended:
        try:
            return min(last, dt.date.fromisoformat(ended))
        except ValueError:
            pass
    return last


def _owner_realized_pnl(conn, cycle_id: int, account_id: str, cycle: Mapping,
                        request: PerformanceRequest):
    """复用周期账本 owner，并把它给出的**累计**值折算成**观察窗口内**的值。

    owner 的 ``realized_pnl`` 是"从周期开始累计到 asof"的口径。只取窗口末的累计值
    会把窗口之前已实现的收益一起算进来，所以必须减去窗口起点前一天的累计值。
    """
    try:
        import paper_portfolio_read_model as PRM
    except ImportError:  # pragma: no cover - 依赖缺失时不能假装有事实
        return (None, STATUS_UNAVAILABLE)

    def _read(asof):
        try:
            context = PRM.PortfolioReadContext(cycle_id=int(cycle_id), asof_day=asof)
            value, status = PRM.realized_pnl(conn, context, account_id=account_id)
        except Exception:  # noqa: BLE001 - owner 的拒绝本身就是一条事实
            return None
        if value is None or str(status) != "verified":
            return None
        return float(value)

    end_asof = _asof_day(cycle, request)
    end_value = _read(end_asof)
    if end_value is None:
        return (None, STATUS_UNAVAILABLE)
    window_start = dt.date.fromisoformat(request.observation_start)
    started = str(cycle.get("started_at") or "")[:10]
    start_day = None
    if started:
        try:
            start_day = dt.date.fromisoformat(started)
        except ValueError:
            start_day = None
    if start_day is not None and start_day >= window_start:
        # 周期从窗口内（或之后）才开始：窗口之前不可能有这个周期的已实现盈亏。
        return (round(end_value, 6), STATUS_AVAILABLE)
    before_value = _read(window_start - dt.timedelta(days=1))
    if before_value is None:
        return (None, STATUS_UNAVAILABLE)
    return (round(end_value - before_value, 6), STATUS_AVAILABLE)


def load_cycle_slice(conn, pin: Mapping, request: PerformanceRequest,
                     *, window_end: str) -> CycleSlice:
    """按 pin 精确装载一个周期：archived → 归档快照，其它 → 活动账本。"""
    cycle_id = int(pin["cycle_id"])
    cycle = _cycle_row(conn, cycle_id)
    if cycle is None:
        return CycleSlice(
            cycle_id=cycle_id, cycle_key="", cycle_status="", source=SOURCE_ARCHIVE,
            reasons=(R_CYCLE_ROW_MISSING,))
    if str(cycle.get("status") or "") == "archived":
        snapshot, reasons, meta = load_archive_snapshot(conn, cycle_id)
        return _archive_slice(pin, request, cycle, snapshot, reasons, meta)
    return _active_slice(pin, request, cycle, conn)


def collect_performance_fact(request: PerformanceRequest, *, db_path: str | None = None,
                             expected_nav_days: int | None = None) -> dict:
    """对外入口：精确 pin → 冻结事实 → 装配。只读，不写任何账本。"""
    if not isinstance(request, PerformanceRequest):
        raise PerformanceEvidenceError("performance_request_required")
    with connect_readonly(db_path) as conn:
        return build_fact_using_conn(conn, request, db_path=db_path,
                                     expected_nav_days=expected_nav_days)


def build_fact_using_conn(conn, request: PerformanceRequest, *,
                          db_path: str | None = None,
                          expected_nav_days: int | None = None) -> dict:
    """用调用方已有的连接读事实。

    上下文是只读调用（全部 SELECT），所以可以复用调用方连接，避免在同一个事务里
    再开第二条通向正式账本的连接。调用方必须保证自己不在此写账本。
    """
    if not isinstance(request, PerformanceRequest):
        raise PerformanceEvidenceError("performance_request_required")
    # 事实读取按列名取值，所以不依赖调用方连接的 row_factory；
    # 临时切到 Row 并在结束时还原，既不改调用方状态，也不用每个查询自己转字典。
    original_factory = conn.row_factory
    if original_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row
    try:
        pins = resolve_exact_pins(
            conn, strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            strategy_checksum=request.strategy_checksum)
        slices = [load_cycle_slice(conn, pin, request,
                                   window_end=request.observation_end) for pin in pins]
    finally:
        if original_factory is not sqlite3.Row:
            conn.row_factory = original_factory
    fact = build_performance_fact(
        request, slices=slices, expected_nav_days=expected_nav_days,
        source_watermark={"ledger": os.path.basename(str(db_path or LEDGER_FILENAME)),
                          "exact_pin_count": len(pins)})
    return fact.projection()
