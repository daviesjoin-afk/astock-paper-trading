# -*- coding: utf-8 -*-
"""统一 point-in-time（PIT）可用性契约。

本模块只回答一个问题：**在决策时点 ``asof``，某条数据是否已经真实可用？**

三条独立事实，禁止互相替代：

``decision_asof``
    决策发生的时点。历史/回放模式必须显式给出。
``available_at``
    该数据在真实世界**何时可被观测到**。它是数据源的属性，
    与"我们什么时候把它下载到本地"无关，也与"它的统计期是什么"无关。
``reason``
    机器可读的判定码。**机器只读 code，绝不解析任何自然语言。**

模式边界（这是本仓库最重要的一条分层）
------------------------------------

``asof is None``
    **live compatibility mode**。实时/纸盘路径保持既有行为不变，
    任何 PIT 收紧都不得在此模式下生效。
``asof is not None``
    **strict PIT historical mode**。核心原则：

    ``未知 availability ≠ 默认可用``
    ``未知 availability = 不可用于历史选股``

    历史模式宁可缺失，也不允许为了让结果"看起来完整"而回退到当前数据。

核心不变式
----------

``data_available_at <= decision_asof``

不满足即 fail closed：不进入因子、不落库、不用今日数据兜底。

时间口径
--------

中国股票市场的 naive timestamp 一律按 ``Asia/Shanghai`` 解释，绝不与 UTC naive
静默比较。date-only（``YYYY-MM-DD``）表示**该自然日结束**；对于"本身只有
date-only 可用性"的数据源，不得假设当天 00:00 已经可用（见
``parse_available_at`` 与 ``bar_available_at``）。
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "REASON_VISIBLE",
    "REASON_FUTURE",
    "REASON_AVAILABILITY_UNKNOWN",
    "REASON_INVALID_TIMESTAMP",
    "REASON_FUTURE_PERIOD",
    "REASON_CODES",
    "MEMBERSHIP_MEMBER",
    "MEMBERSHIP_NOT_LISTED_YET",
    "MEMBERSHIP_DELISTED",
    "MEMBERSHIP_UNKNOWN",
    "CHINA_TZ_NAME",
    "CN_MARKET_CLOSE",
    "SNAPSHOT_AVAILABLE_AT_KEYS",
    "CLASSIFICATION_VALUE_KEYS",
    "CLASSIFICATION_EFFECTIVE_FROM_KEYS",
    "CLASSIFICATION_EFFECTIVE_TO_KEYS",
    "UNIVERSE_LIST_DATE_KEYS",
    "UNIVERSE_DELIST_DATE_KEYS",
    "china_tz",
    "parse_asof",
    "parse_available_at",
    "asof_is_strict",
    "live_decision_time",
    "is_visible_at",
    "filter_visible_rows",
    "bar_available_at",
    "snapshot_available_at",
    "classification_visibility",
    "universe_membership",
    "universe_asof_members",
    "pit_flags",
]

# ─────────────────────────────────────────────────────────────────────────────
# 固定判定码（机器只读这些字面量）
# ─────────────────────────────────────────────────────────────────────────────

#: 数据在 ``asof`` 时点已经可用。
REASON_VISIBLE = "visible"
#: 数据的可用时点晚于 ``asof``（典型的未来数据泄漏）。
REASON_FUTURE = "future"
#: 数据没有可信的可用时点，历史模式下**不等于**可用。
REASON_AVAILABILITY_UNKNOWN = "availability_unknown"
#: 时间戳存在但无法解析；历史模式下不可作为可用性依据。
REASON_INVALID_TIMESTAMP = "invalid_timestamp"
#: 数据自身的统计期晚于 ``asof``（例如报告期还没到），它不是可用性证据。
REASON_FUTURE_PERIOD = "future_period"

REASON_CODES = (
    REASON_VISIBLE,
    REASON_FUTURE,
    REASON_AVAILABILITY_UNKNOWN,
    REASON_INVALID_TIMESTAMP,
    REASON_FUTURE_PERIOD,
)

# ─────────────────────────────────────────────────────────────────────────────
# 历史 universe 成员资格状态（选股成分是独立问题，不复用上面 5 个数据可用性码）
# ─────────────────────────────────────────────────────────────────────────────

#: 有上市日证据且 ``list_date <= asof < delist_date``。
MEMBERSHIP_MEMBER = "member"
#: 上市日晚于 ``asof``：当时尚未上市，绝不能进历史 universe。
MEMBERSHIP_NOT_LISTED_YET = "not_listed_yet"
#: ``asof >= delist_date``：当时已经退市。
MEMBERSHIP_DELISTED = "delisted"
#: 缺少任何上市/退市日证据 → 历史成员资格**未经证明**。
MEMBERSHIP_UNKNOWN = "membership_unknown"

CHINA_TZ_NAME = "Asia/Shanghai"

#: A 股日线 bar 在当日收盘完成。收盘之前，当日 bar 不得参与决策。
CN_MARKET_CLOSE = _dt.time(15, 0)
_END_OF_DAY = _dt.time(23, 59, 59, 999999)

#: snapshot 行的可用时点候选键（按可信度排序，取第一个可用者）。
SNAPSHOT_AVAILABLE_AT_KEYS = (
    "observed_at",
    "available_at",
    "quote_at",
    "quote_ts",
    "snapshot_at",
    "fetched_at",
)

#: 分类字段（行业等）的取值键。
CLASSIFICATION_VALUE_KEYS = ("industry", "sector", "industry_name", "sw_industry")
#: 分类生效区间的候选键。分类会被事后重算，因此需要 effective window 而不是快照时间。
CLASSIFICATION_EFFECTIVE_FROM_KEYS = (
    "industry_effective_from",
    "classification_effective_from",
    "effective_from",
)
CLASSIFICATION_EFFECTIVE_TO_KEYS = (
    "industry_effective_to",
    "classification_effective_to",
    "effective_to",
)

#: 上市日 / 退市日的候选键。
UNIVERSE_LIST_DATE_KEYS = ("list_date", "listing_date", "ipo_date", "LISTING_DATE", "LIST_DATE")
UNIVERSE_DELIST_DATE_KEYS = (
    "delist_date",
    "delisting_date",
    "delisted_at",
    "DELIST_DATE",
    "out_date",
)

_MISSING_STRINGS = {"", "nan", "nat", "none", "null", "-", "--"}
_DATE_ONLY = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_DATETIME_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

_UTC8 = _dt.timezone(_dt.timedelta(hours=8), CHINA_TZ_NAME)
_TZ_CACHE: list = []


def china_tz() -> _dt.tzinfo:
    """中国市场时区。

    优先 ``zoneinfo("Asia/Shanghai")``（含历史 DST 与历史偏移），
    运行环境缺 tzdata 时回退到固定 ``+08:00``——对 1991 年以后的日期两者等价。
    """
    if _TZ_CACHE:
        return _TZ_CACHE[0]
    tz: _dt.tzinfo = _UTC8
    try:  # pragma: no cover - 取决于运行环境是否带 tzdata
        import zoneinfo

        tz = zoneinfo.ZoneInfo(CHINA_TZ_NAME)
    except Exception:
        tz = _UTC8
    _TZ_CACHE.append(tz)
    return tz


# ─────────────────────────────────────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────────────────────────────────────

def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, str):
        return value.strip().lower() in _MISSING_STRINGS
    return False


def _parse(value: Any) -> tuple[Optional[_dt.datetime], bool, str]:
    """返回 ``(aware_datetime, has_time, status)``；``status`` ∈ ok/missing/invalid。"""
    if _is_missing(value):
        return None, False, "missing"
    if isinstance(value, _dt.datetime):  # 含 pandas.Timestamp
        moment = value
        has_time = moment.time() != _dt.time(0, 0)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=china_tz())
        else:
            moment = moment.astimezone(china_tz())
        return moment, has_time, "ok"
    if isinstance(value, _dt.date):
        moment = _dt.datetime.combine(value, _END_OF_DAY, tzinfo=china_tz())
        return moment, False, "ok"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 不猜 epoch：数值型时间戳语义不明确，历史模式下必须显式失败。
        return None, False, "invalid"
    text = str(value).strip()
    if _DATE_ONLY.match(text):
        try:
            day = _dt.date.fromisoformat(text.replace("/", "-"))
        except ValueError:
            return None, False, "invalid"
        return _dt.datetime.combine(day, _END_OF_DAY, tzinfo=china_tz()), False, "ok"
    normalized = text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text
    parsed: Optional[_dt.datetime] = None
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in _DATETIME_FORMATS:
            try:
                parsed = _dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None, False, "invalid"
    has_time = parsed.time() != _dt.time(0, 0)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=china_tz())
    else:
        parsed = parsed.astimezone(china_tz())
    return parsed, has_time, "ok"


def parse_asof(value: Any) -> Optional[_dt.datetime]:
    """把 ``decision_asof`` 归一成 tz-aware（Asia/Shanghai）时点。

    ``None``/空值 → ``None``（= live compatibility mode）。date-only 表示
    **该自然日结束**。无法解析 → ``None``（调用方须按 fail-closed 处理，
    不要再回退到"当前时间"）。
    """
    moment, _has_time, status = _parse(value)
    return moment if status == "ok" else None


def parse_available_at(value: Any) -> Optional[_dt.datetime]:
    """把数据可用时点归一成 tz-aware（Asia/Shanghai）时点。

    date-only 一律解释为**该自然日结束**：只有日粒度可用性的数据源不能
    证明"当天 00:00 已经可用"，盘中回放因此看不到当日数据。
    """
    moment, _has_time, status = _parse(value)
    return moment if status == "ok" else None


def asof_is_strict(asof: Any) -> bool:
    """``asof`` 是否请求 strict PIT historical mode。"""
    return not _is_missing(asof)


def live_decision_time(now: Any = None) -> _dt.datetime:
    """live 路径当前**实时截面**的决策时点（tz-aware，Asia/Shanghai）。

    实时选股有两套不同的 cutoff，绝不能混用：

    * **已收盘日线 / 财务披露** 的 cutoff = 最近一个完整交易日
      （盘中就是昨天，因为今天的日线还没收完）；
    * **当日实时截面**（快照 PE/PB/换手/资金/行业）的决策时点 = **现在**——
      它就是在这一刻被观测到的。

    拿"最近完整交易日"去判当日实时行，会把每一行都判成 ``future`` 并清空
    PE/PB/换手/行业（这会直接打掉生产盘中扫描）。历史回放**不得**使用本函数。
    """
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if not isinstance(moment, _dt.datetime):
        parsed = parse_asof(moment)
        return parsed or _dt.datetime.now(_dt.timezone.utc).astimezone(china_tz())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(china_tz())


def _iso(value: Optional[_dt.datetime]) -> Optional[str]:
    return None if value is None else value.isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# 可见性判定
# ─────────────────────────────────────────────────────────────────────────────

def is_visible_at(available_at: Any, asof: Any, *, period: Any = None) -> dict:
    """判定某条数据在 ``asof`` 时点是否可见。

    返回 ``{"visible", "available_at", "reason", "asof", "mode"}``；
    机器逻辑只读 ``reason``（取值必在 :data:`REASON_CODES` 内）。

    判定顺序（先到先判，绝不"降级为可用"）：

    1. ``asof is None`` → live compatibility mode，可见（保持现有实时行为）。
    2. ``asof`` 无法解析 → ``invalid_timestamp``，不可见（错误的 cutoff 不是回放依据）。
    3. ``period`` 晚于 ``asof`` → ``future_period``，不可见（统计期不是可用性证据）。
    4. ``available_at`` 缺失 → ``availability_unknown``，不可见。
    5. ``available_at`` 无法解析 → ``invalid_timestamp``，不可见。
    6. ``available_at > asof`` → ``future``，不可见。
    7. 否则：可见。
    """
    moment, _has_time, _status = _parse(available_at)
    period_moment, _p_has_time, _p_status = _parse(period)

    if not asof_is_strict(asof):
        return {
            "visible": True,
            "available_at": _iso(moment) or (None if _is_missing(available_at) else str(available_at)),
            "reason": REASON_VISIBLE,
            "asof": None,
            "mode": "live",
        }

    asof_moment = parse_asof(asof)
    result = {
        "visible": False,
        "available_at": _iso(moment) or (None if _is_missing(available_at) else str(available_at)),
        "reason": REASON_AVAILABILITY_UNKNOWN,
        "asof": _iso(asof_moment) if asof_moment else (None if _is_missing(asof) else str(asof)),
        "mode": "strict",
    }
    if asof_moment is None:
        result["reason"] = REASON_INVALID_TIMESTAMP
        return result
    if period_moment is not None and period_moment > asof_moment:
        result["reason"] = REASON_FUTURE_PERIOD
        return result
    if _is_missing(available_at):
        result["reason"] = REASON_AVAILABILITY_UNKNOWN
        return result
    if moment is None:
        result["reason"] = REASON_INVALID_TIMESTAMP
        return result
    if moment > asof_moment:
        result["reason"] = REASON_FUTURE
        return result
    result["visible"] = True
    result["reason"] = REASON_VISIBLE
    return result


def filter_visible_rows(rows: Optional[Iterable[Any]], asof: Any, *,
                        at_keys: Sequence[str] = SNAPSHOT_AVAILABLE_AT_KEYS,
                        period_keys: Sequence[str] = (),
                        key: Optional[str] = None) -> list:
    """按可用性过滤行，返回**仅**在 ``asof`` 可见的行。

    ``asof is None`` 时原样返回（live compatibility）。历史模式下，
    缺少可信可用时点的行会被**丢弃**而不是保留。
    """
    items = [row for row in (rows or []) if isinstance(row, Mapping)]

    def _target(row):
        return row.get(key) if key else row

    if not asof_is_strict(asof):
        return [_target(row) for row in items]
    out = []
    for row in items:
        moment = None
        for name in at_keys:
            if name in row and not _is_missing(row.get(name)):
                moment = row.get(name)
                break
        period = None
        for name in period_keys:
            if name in row and not _is_missing(row.get(name)):
                period = row.get(name)
                break
        if is_visible_at(moment, asof, period=period)["visible"]:
            out.append(_target(row))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 价格 / K 线
# ─────────────────────────────────────────────────────────────────────────────

def bar_available_at(index_value: Any) -> Optional[_dt.datetime]:
    """日线 bar 的可用时点。

    * 索引只有日期（naive midnight，生产解析器的口径）→ **当日收盘 15:00**。
      因此 ``asof = 当日 10:00`` 看不到当日那根完整 OHLCV。
    * 索引带有显式时刻（例如 15:00，或真正的分钟 bar）→ 视为该 bar
      自身的完成时刻，原样信任。

    无法解析 → ``None``（历史模式须 fail closed，不许猜）。
    """
    moment, has_time, status = _parse(index_value)
    if status != "ok" or moment is None:
        return None
    if not has_time:
        return _dt.datetime.combine(moment.date(), CN_MARKET_CLOSE, tzinfo=moment.tzinfo)
    return moment


# ─────────────────────────────────────────────────────────────────────────────
# snapshot / 分类
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_available_at(row: Optional[Mapping[str, Any]]) -> tuple[Any, Optional[str]]:
    """从 snapshot 行解析可用时点。

    返回 ``(原值, 归一化 ISO)``；找不到返回 ``(None, None)``。
    **绝不**用"当前墙钟"兜底：那会把"不知道"伪装成"现在就有"。
    """
    if not isinstance(row, Mapping):
        return None, None
    for name in SNAPSHOT_AVAILABLE_AT_KEYS:
        value = row.get(name)
        if _is_missing(value):
            continue
        moment, _has_time, status = _parse(value)
        if status == "ok" and moment is not None:
            return value, _iso(moment)
        if status == "invalid":
            return value, None
    return None, None


def classification_visibility(row: Optional[Mapping[str, Any]], asof: Any, *,
                              value_keys: Sequence[str] = CLASSIFICATION_VALUE_KEYS,
                              from_keys: Sequence[str] = CLASSIFICATION_EFFECTIVE_FROM_KEYS,
                              to_keys: Sequence[str] = CLASSIFICATION_EFFECTIVE_TO_KEYS) -> dict:
    """分类字段（行业）在 ``asof`` 的可见性。

    行业分类会被**事后重算**（股票从行业 A 调整到行业 B），所以"今天的分行业"
    不能拿来重写历史。可接受的历史分类证据只有两种：

    1. 显式生效区间：``effective_from <= asof < effective_to``；
    2. 该行本身带可信观测时点且 ``observed_at <= asof``（即这一行当时就被观测到了）。

    两者都没有 → ``availability_unknown``：历史模式取不到值（返回 None），
    而不是回退到当前行业。live 模式保持现有行为。
    """
    data = row if isinstance(row, Mapping) else {}
    value = None
    for name in value_keys:
        if not _is_missing(data.get(name)):
            value = data.get(name)
            break

    if not asof_is_strict(asof):
        return {"visible": True, "value": value, "reason": REASON_VISIBLE,
                "mode": "live", "basis": "live"}
    asof_moment = parse_asof(asof)
    if asof_moment is None:
        return {"visible": False, "value": None, "reason": REASON_INVALID_TIMESTAMP,
                "mode": "strict", "basis": "invalid_asof"}
    if value is None:
        return {"visible": False, "value": None, "reason": REASON_AVAILABILITY_UNKNOWN,
                "mode": "strict", "basis": "no_value"}

    raw_from = next((data.get(k) for k in from_keys if not _is_missing(data.get(k))), None)
    if raw_from is not None:
        start, _has, status = _parse(raw_from)
        if status != "ok" or start is None:
            return {"visible": False, "value": None, "reason": REASON_INVALID_TIMESTAMP,
                    "mode": "strict", "basis": "invalid_effective_from"}
        if start > asof_moment:
            return {"visible": False, "value": None, "reason": REASON_FUTURE,
                    "mode": "strict", "basis": "effective_from_after_asof"}
        raw_to = next((data.get(k) for k in to_keys if not _is_missing(data.get(k))), None)
        if raw_to is not None:
            end, _has, status = _parse(raw_to)
            if status != "ok" or end is None:
                return {"visible": False, "value": None, "reason": REASON_INVALID_TIMESTAMP,
                        "mode": "strict", "basis": "invalid_effective_to"}
            if asof_moment >= end:
                # 区间已结束：当前值不再代表 asof 当时的分类，而历史分类我们并不知道。
                return {"visible": False, "value": None, "reason": REASON_AVAILABILITY_UNKNOWN,
                        "mode": "strict", "basis": "effective_window_expired"}
        return {"visible": True, "value": value, "reason": REASON_VISIBLE,
                "mode": "strict", "basis": "effective_window"}

    raw_at, _normalized = snapshot_available_at(data)
    observed, _has, status = _parse(raw_at)
    if status == "ok" and observed is not None:
        if observed > asof_moment:
            return {"visible": False, "value": None, "reason": REASON_FUTURE,
                    "mode": "strict", "basis": "observed_after_asof"}
        return {"visible": True, "value": value, "reason": REASON_VISIBLE,
                "mode": "strict", "basis": "observed_at"}

    # 无生效区间、无观测时点：这正是"用今天的分类重写历史"的场景。
    return {"visible": False, "value": None, "reason": REASON_AVAILABILITY_UNKNOWN,
            "mode": "strict", "basis": "no_historical_classification"}


# ─────────────────────────────────────────────────────────────────────────────
# 历史 universe 成员资格
# ─────────────────────────────────────────────────────────────────────────────

def universe_membership(row: Optional[Mapping[str, Any]], asof: Any, *,
                        list_keys: Sequence[str] = UNIVERSE_LIST_DATE_KEYS,
                        delist_keys: Sequence[str] = UNIVERSE_DELIST_DATE_KEYS) -> dict:
    """历史 universe 成员资格：``list_date <= asof < delist_date``。

    历史选股绝不允许"先取今天仍上市的股票，再回放五年前"——那会同时引入
    "未来上市"与"漏掉退市"两类幸存者偏差。

    ``proven`` 表示成员资格是否有明确日期证据。缺少上市/退市日期数据时
    不会伪装成已证明：``status = membership_unknown`` 且 ``proven = False``，
    由调用方决定是否 fail closed（``strict`` 模式下 ``member`` 仍为 True，
    因为"没有证据"不等于"证据表明未上市"——把全市场判成未上市是另一种编造）。
    """
    data = row if isinstance(row, Mapping) else {}
    raw_list = next((data.get(k) for k in list_keys if not _is_missing(data.get(k))), None)
    raw_delist = next((data.get(k) for k in delist_keys if not _is_missing(data.get(k))), None)
    base = {
        "member": True,
        "status": MEMBERSHIP_UNKNOWN,
        "proven": False,
        "list_date": None,
        "delist_date": None,
        "mode": "live" if not asof_is_strict(asof) else "strict",
    }
    if not asof_is_strict(asof):
        return base

    asof_moment = parse_asof(asof)
    if asof_moment is None:
        base.update({"member": False, "status": MEMBERSHIP_UNKNOWN,
                     "reason": REASON_INVALID_TIMESTAMP})
        return base

    if raw_list is not None:
        listed, _has, status = _parse(raw_list)
        if status == "ok" and listed is not None:
            base["list_date"] = listed.date().isoformat()
            base["proven"] = True
            if listed > asof_moment:
                base.update({"member": False, "status": MEMBERSHIP_NOT_LISTED_YET})
                return base
    if raw_delist is not None:
        delisted, _has, status = _parse(raw_delist)
        if status == "ok" and delisted is not None:
            base["delist_date"] = delisted.date().isoformat()
            base["proven"] = True
            if asof_moment >= delisted:
                base.update({"member": False, "status": MEMBERSHIP_DELISTED})
                return base
    if not base["proven"]:
        return base
    base["status"] = MEMBERSHIP_MEMBER
    return base


def universe_asof_members(rows: Optional[Iterable[Any]], asof: Any, *,
                          strict: bool = True, drop_unproven: bool = False,
                          key: Optional[str] = None) -> dict:
    """按 ``asof`` 过滤历史 universe 成分。

    ``strict=True``（默认）时丢弃 ``not_listed_yet`` 与 ``delisted`` 两类。
    ``membership_unknown`` 的行默认**保留**但计入 ``report["unproven"]``：
    缺少上市日数据时把全市场判成"当时未上市"是另一种编造。需要完全
    fail-closed 的调用方传 ``drop_unproven=True``。
    """
    items = [row for row in (rows or []) if isinstance(row, Mapping)]
    if not asof_is_strict(asof):
        return {"members": list(items), "report": {"mode": "live", "kept": len(items)}}
    kept, dropped = [], []
    counts = {MEMBERSHIP_MEMBER: 0, MEMBERSHIP_NOT_LISTED_YET: 0,
              MEMBERSHIP_DELISTED: 0, MEMBERSHIP_UNKNOWN: 0}
    for row in items:
        verdict = universe_membership(row, asof)
        counts[verdict["status"]] = counts.get(verdict["status"], 0) + 1
        target = row.get(key) if key else row
        unknown = verdict["status"] == MEMBERSHIP_UNKNOWN
        if verdict["member"] and not (unknown and drop_unproven):
            kept.append(target)
        else:
            dropped.append(target)
    report = {
        "mode": "strict",
        "kept": len(kept),
        "dropped": len(dropped),
        "unproven": counts[MEMBERSHIP_UNKNOWN],
        "counts": counts,
    }
    return {"members": kept, "report": report}


# ─────────────────────────────────────────────────────────────────────────────
# provenance
# ─────────────────────────────────────────────────────────────────────────────

def pit_flags(**flags: Any) -> dict:
    """构造按数据类分层的 PIT provenance。

    绝不使用单个模糊的 ``data_ok=True``：不同数据类的可用性证据完全不同，
    合并成一个布尔值会把问题藏起来。所有分类默认 ``False``（fail closed）。
    """
    out = {
        "decision_asof": None,
        "mode": "live",
        "price_pit_safe": False,
        "financial_pit_safe": False,
        "snapshot_pit_safe": False,
        "classification_pit_safe": False,
        "sentiment_pit_safe": False,
    }
    out.update(flags)
    return out
