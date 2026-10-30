"""Prefetch current public status facts; ingest only at an execution write boundary.

No historical backfill and no provider I/O in ``persist``. Source observations
retain their retrieval times and cannot authorize an earlier execution instant.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
import data_paths
import market_data_contract as MDC
import market_data_service as MDSvc
import marketdata_cache as MCache
from marketdata_transport import http_get
import tradability_archive as TA
import tradability_ingestion as TI
from env_utils import env_float

TZ = dt.timezone(dt.timedelta(hours=8))
STATUS_MAX_AGE_SECONDS = 120


# 停牌/上市日/ST 名单是低频状态：交易所当天只会增删少量记录，盘中每 3 分钟
# 重拉一次完整停牌报表没有收益（一天 80 次），却把 quote.tradability 拖到
# 数秒到三十秒。缓存窗口取 15 分钟（一天约 16 次），仍能在一个扫描周期内
# 发现新停牌；状态本身的新鲜度仍由 `STATUS_MAX_AGE_SECONDS` 对最终成交
# 观测单独校验，缓存只复用**报表内容**，不延后 observed_at。
STATUS_CACHE_TTL_SECONDS = env_float("ASTOCK_TRADABILITY_STATUS_TTL_SECONDS", 900, minimum=1.0)

# 个股档案（`stock/get`）里带着**实时价格证据**（``f43``）与行情时间
# （``f86``）。这类证据的有效期就是 `STATUS_MAX_AGE_SECONDS`（120 秒）：
# 把它按 15 分钟缓存，等于把一份 13 分钟前的价格当成当前价格发布，而
# 读取方又会按 120 秒把它判为过期 —— 结果是**整段缓存窗口内都拿不到
# 停牌/可交易观测**，哪怕行情源本身是健康的。
#
# 所以：低频状态（停牌报表）用长 TTL；带价格证据的档案用与它的新鲜度
# 门槛一致的短 TTL。两者共用同一个读取器，但 TTL 由调用方显式给出。
STATUS_CACHE_DIR = os.path.join(data_paths.data_dir(), "tradability_status")
_CACHE = {}
_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
PROFILE_SOURCE = "eastmoney_stock_profile+complete_suspension_report+verified_quote"
MARKET_SOURCE = "current_full_market_membership+complete_suspension_report+verified_quote_range"
#: 东财 ``stock/get`` 拿不到涨跌停价时，用**已验证的公开行情**（腾讯 / 新浪）
#: 推导涨跌停带的观测来源。推导值一律显式标注，绝不与真实观测混同。
DERIVED_LIMIT_SOURCE = (
    "public_quote_prev_close_derived_limit+complete_suspension_report+verified_quote"
)
#: 行情信封自带的日内 high/low（腾讯 / 新浪报文里的最高 / 最低）。这是**观测**：
#: 观测到的区间就是当天真实的成交区间，不需要任何假设。
RANGE_SOURCE = "public_quote_observed_intraday_range+complete_suspension_report+verified_quote"

# ── 涨跌停法定幅度（推导用） ────────────────────────────────────────────────
#
# 这张表是**推导**用的口径，不是观测。观测来源仍然是东财 ``stock/get`` 的
# ``f51``/``f52``；只有在它不可达时才退到"昨收 × 法定幅度"。
#
# 幅度按**代码段**决定（代码段在交易发生前就已知，不依赖任何行情）：
#   * 主板（``60``/``00`` 开头，含 001/002/003/605）→ ±10%
#   * 创业板（``30``）/ 科创板（``688``/``689``）→ ±20%
#   * ST / *ST / 退市整理 → ±5%（名称含 ``ST`` 或 ``退``）
#
# 与仓库权威实现 ``paper_trading_rules.limit_pct`` 的差异是**刻意的**：权威
# 实现返回的是"涨停识别阈值"（主板 9.5 / 创业板 19.5），那是为了识别"是否触及
# 涨停"而**留了余量**的保守阈值；这里需要的是**法定涨跌停价本身**，用留余量
# 的阈值会把一只真跌停票算成"还有 0.5% 空间"，从而错误地判成可卖。
LIMIT_AMPLITUDE_MAIN = Decimal("0.10")
LIMIT_AMPLITUDE_GROWTH = Decimal("0.20")
LIMIT_AMPLITUDE_ST = Decimal("0.05")

#: A 股价格最小变动单位是 1 分（0.01 元）。涨跌停价一律四舍五入到分。
PRICE_TICK = Decimal("0.01")

#: 推导出的涨跌停价与公开行情里的"昨收"必须自洽：昨收由``涨跌幅``反推时
#: 允许一点点浮点误差，超过这个比例说明两份数据互相矛盾 → 不得推导。
PREV_CLOSE_CONSISTENCY_TOLERANCE = 0.005

#: 独立行情源（腾讯 / 新浪）的一行报价。它们的字段位置在公开报文里是固定的，
#: 但我们仍然**按格式**而不是按固定下标去取时间戳，避免上游改版时静默错位。
_TENCENT_TIME_PATTERN = re.compile(r"^\d{14}$")


def _get(key, url, params, *, ttl_seconds=None):
    """Read one status payload, sharing it across runner processes.

    Each 3-minute scan is a fresh process, so an in-memory-only TTL never skips
    a fetch. The payload is published to ``data_cache`` with the same
    single-flight + atomic-replace contract used by other provider caches; the
    caller still applies its own 120-second observation gate.

    ``ttl_seconds`` lets the caller pick the window that matches the payload:
    a genuinely low-frequency report may be reused for 15 minutes, while a
    payload carrying live price evidence must not outlive the freshness gate
    that will judge it.
    """
    os.makedirs(STATUS_CACHE_DIR, exist_ok=True)
    path = os.path.join(
        STATUS_CACHE_DIR,
        hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16] + ".json",
    )

    def fetch():
        payload = json.loads(http_get(url, params=params, timeout=3, retries=1))
        return {"data": payload,
                "observed": dt.datetime.now(TZ).isoformat(timespec="microseconds")}

    value = MCache.persistent_cached(
        _CACHE, _LOCK, _FETCH_LOCK, key, path,
        STATUS_CACHE_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds), fetch,
        lambda item: isinstance(item, dict) and "data" in item,
    )
    return value["data"], value["observed"]


def _suspensions(day):
    params = {"sortColumns": "SUSPEND_START_DATE", "sortTypes": -1,
              "pageSize": 500, "pageNumber": 1,
              "reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE", "columns": "ALL",
              "source": "WEB", "client": "WEB",
              "filter": f'(MARKET="全部")(DATETIME=\'{day}\')'}
    rows, observed, expected = [], None, None
    pages = 1
    for page in range(1, 5):
        if page > pages:
            break
        params["pageNumber"] = page
        payload, stamp = _get(("suspend", day, page),
                              "https://datacenter-web.eastmoney.com/api/data/v1/get", params,
                              # 停牌报表是低频状态：交易所当天只增删少量记录，
                              # 用长窗口复用它的**内容**（observed_at 仍按实际
                              # 取数时刻记录）。
                              ttl_seconds=STATUS_CACHE_TTL_SECONDS)
        result = payload.get("result")
        if payload.get("success") is not True or not isinstance(result, dict):
            raise ValueError("suspension source unavailable")
        if page == 1:
            pages, expected = int(result["pages"]), int(result["count"])
        if (int(result["pages"]) != pages or int(result["count"]) != expected
                or pages < 0 or pages > 4 or not isinstance(result.get("data"), list)):
            raise ValueError("suspension page coverage inconsistent")
        rows.extend(result["data"])
        observed = max(observed or stamp, stamp)
    if (len(rows) != expected or len({json.dumps(r, sort_keys=True) for r in rows}) != len(rows)
            or any(len(str(r.get("SECURITY_CODE") or "")) != 6
                   or not str(r.get("SECURITY_CODE") or "").isdigit() for r in rows)):
        raise ValueError("suspension coverage incomplete")
    return rows, observed


def _time(value):
    if not value:
        return None
    text = str(value)
    result = (dt.datetime.strptime(text, "%Y%m%d%H%M%S") if text.isdigit() and len(text) == 14
              else dt.datetime.fromisoformat(text))
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def _suspended(rows, code, observed):
    relevant = [r for r in rows if str(r["SECURITY_CODE"]) == code]
    for row in relevant:
        start, end = _time(row.get("SUSPEND_START_TIME")), _time(row.get("SUSPEND_END_TIME"))
        if start is None or (end is not None and end < start):
            return None
        if start <= observed and (end is None or observed <= end):
            return True
    return False


# ── 涨跌停带：观测优先，推导兜底 ────────────────────────────────────────────
#
# 生产实测（2026-10-30，oci-main）：东财 ``push2*`` **所有**行情入口整段不可达，
# 于是 ``stock/get`` 的 ``f51``/``f52``（涨跌停价）取不到 → 成交性观测整段缺失
# → 执行计划判 ``TRADABILITY_UNKNOWN`` → 真实委托永久挂起。
#
# 但腾讯 / 新浪是**可达**的，它们的报文里带着昨收（与涨跌幅自洽），因此可以
# 退到"昨收 × 法定幅度"推导涨跌停带。这条路径的每个结论都必须显式标注为
# **推导**，绝不与东财的真实观测混同（见 :func:`_derived_limit_observation`）。


def _finite_quote_number(value):
    """有限正数或 ``None``。**绝不**把缺失/NaN/0 强转成可用价格。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _decimal_of(value):
    """把一个有限正数转成 :class:`Decimal`；不可用时返回 ``None``。

    涨跌停价必须**四舍五入到分**。用二进制浮点做 ``round(x, 2)`` 会踩边界误差
    （``6.03 * 0.9`` 在二进制里是 5.426999999999999，``round(..., 2)`` 给
    5.43 恰好对，但 ``2.35 * 1.1`` 这类会向下偏一分）。因此推导全程走
    ``Decimal`` + ``ROUND_HALF_UP``，与交易所的"四舍五入到分"口径一致。
    """
    number = _finite_quote_number(value)
    if number is None:
        return None
    try:
        return Decimal(str(number))
    except (InvalidOperation, ValueError):
        return None


def _is_st_label(name):
    """证券名称是否带 ST / *ST / 退市整理标记（5% 幅度的唯一依据）。

    判定口径与仓库权威实现 ``paper_trading_rules.is_st_or_delisting`` 一致
    （名称含 ``ST`` 或 ``退``）。这里只作为**推导幅度**与 ``is_st`` 的输入：
    名称本身必须来自真实行情（东财档案 ``f58`` 或公开行情的名称字段），
    拿不到名称时不猜（见 :func:`_limit_basis`）。
    """
    label = str(name or "").upper()
    return "ST" in label or "退" in str(name or "")


def _legal_limit_amplitude(code, name):
    """该证券当日适用的**法定**涨跌停幅度（推导用）。

    口径（按代码段 + ST 状态）：

    ==========================  ======
    代码段 / 状态                幅度
    ==========================  ======
    主板 ``60``/``00`` 开头       ±10%
    创业板 ``30`` 开头             ±20%
    科创板 ``688``/``689``        ±20%
    ST / *ST / 退市整理           ±5%
    ==========================  ======

    ST 优先级最高：ST 票在哪个板块都是 ±5%。
    """
    if _is_st_label(name):
        return LIMIT_AMPLITUDE_ST
    code_text = str(code or "")
    if code_text.startswith(("300", "301", "302", "688", "689")):
        return LIMIT_AMPLITUDE_GROWTH
    return LIMIT_AMPLITUDE_MAIN


def _prev_close_of(quote):
    """公开行情里的**昨收**（腾讯 / 新浪报文都带，东财 clist 带 ``f18``）。

    拿不到就返回 ``None`` —— 昨收不可得时**不得**编造涨跌停价（那会把
    "观测不可得"伪装成"观测成立"）。本模块**不**从涨跌幅反推昨收：反推值
    会把涨跌幅的舍入误差放大到涨跌停价上，可能把一只真跌停票算成"还有
    0.01 元空间"，从而错误地判成可卖。
    """
    for key in ("prev_close", "pre_close"):
        number = _finite_quote_number(quote.get(key))
        if number is not None:
            return number
    return None


def _prev_close_conflicts(quote, price, prev_close):
    """昨收与（现价, 涨跌幅）是否互相矛盾。

    公开行情的现价、涨跌幅、昨收来自**同一次响应**，本应自洽。若自相矛盾，
    说明这份数据不可信，据此推导的涨跌停价就是编造的 → 拒绝推导。

    容忍 0.5%（与仓库双源价格容差同量级）：不同源之间的采样时滞会带来
    一点点价差，但那不会让"昨收"本身失真。
    """
    pct = quote.get("pct")
    try:
        pct_value = float(pct)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(pct_value) or abs(pct_value) >= 99:
        return False
    implied = price / max(1.0 + pct_value / 100.0, 0.01)
    if not math.isfinite(implied) or implied <= 0:
        return False
    return abs(implied - prev_close) / prev_close > PREV_CLOSE_CONSISTENCY_TOLERANCE


def _derived_limit_band(code, *, name, price, prev_close):
    """用「昨收 × 法定幅度」推导涨跌停带，并判定现价是否被锁死。

    返回 ``None`` 表示**推导不成立**（调用方必须维持"观测不可得"的失败
    语义，绝不放宽）。返回字典时：

    * ``lower`` / ``upper``：推导出的涨跌停价（已四舍五入到分）；
    * ``locked``：现价是否**恰好落在**推导边界上（锁死）；
    * ``direction``：锁死方向（``"up"`` / ``"down"``）。

    成立条件（全部必须满足）：

    1. 昨收可得且有效（``prev_close > 0``）；
    2. 现价可得且有效（``price > 0``）；
    3. 推导出的上下限满足 ``0 < lower < upper``（区间非空）；
    4. 现价必须落在 ``[lower, upper]`` 内；**等于**任一边界即**锁死**。

    第 4 条是保守性的核心：现价**恰好等于**推导上限/下限时，那不是"还在
    区间里"，而是**钉在涨跌停板上** —— 买单/卖单会排队锁死。把它判成可卖
    就是把真跌停票当成正常成交，正是现网 601956 那类委托挂起的成因。

    现价**越出**推导区间则说明昨收与现价互相矛盾 → 推导不成立（返回
    ``None``），而不是"就近取整"。
    """
    previous = _decimal_of(prev_close)
    current = _decimal_of(price)
    if previous is None or current is None:
        return None
    amplitude = _legal_limit_amplitude(code, name)
    try:
        lower = (previous * (Decimal("1") - amplitude)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
        upper = (previous * (Decimal("1") + amplitude)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    if not (Decimal("0") < lower < upper):
        return None
    if current < lower or current > upper:
        # 越界：昨收与现价互相矛盾 → 推导不成立。
        return None
    band = {"lower": float(lower), "upper": float(upper),
            "amplitude": str(amplitude), "prev_close": float(previous)}
    if current == upper:
        return {**band, "locked": True, "direction": "up"}
    if current == lower:
        return {**band, "locked": True, "direction": "down"}
    return {**band, "locked": False, "direction": None}


def _quote_range_band(quote, price):
    """行情信封自带的日内高/低（**观测**，证据等级最高）。

    腾讯 / 新浪报文里本来就有最高（idx33）与最低（idx34），数据层已把它们
    透传进 quote 信封（``high`` / ``low``）。"现价严格位于日内区间内部"
    是一个**观测事实**：它证明价格没有钉在当日极值上。

    与"昨收 × 法定幅度"的**推导**不同，这里不需要任何假设 —— 观测到的
    low / high 就是当天真实的成交区间。

    只有当 high / low **同时**可得、且满足 ``low ≤ price ≤ high`` 时才成立；
    不自洽就返回 ``None``（绝不猜）。
    """
    low = _finite_quote_number(quote.get("low"))
    high = _finite_quote_number(quote.get("high"))
    if low is None or high is None or high < low:
        return None
    if not low <= price <= high:
        return None
    return {"low": low, "high": high}


def _limit_basis(code, quote, day, *, profile_transport_down=None):
    """解析该 code 当日的涨跌停带，并标注它的**证据等级**。

    依次尝试（证据等级从高到低）：

    1. ``profile`` —— 东财 ``stock/get`` 的真实 ``f51``/``f52``（**观测**）；
    2. ``range``   —— 行情信封自带的日内 low/high（**观测**，腾讯 / 新浪）；
    3. ``derived`` —— 昨收 × 法定幅度（**推导**，显式标注）。

    **观测永远优先于推导**：能观测到就不推导。

    第 1 条给出**真实的涨跌停价**，它同时回答"现价是不是钉在板上"；第 2 条
    只给出**日内成交区间**，因此它只能证明"现价不在极值上"（未锁死），**不能**
    证明"钉在极值上就是涨跌停"（那需要真实涨跌停价）；第 3 条给出**推导的
    涨跌停价**（东财整段不可达时的兜底），能**双向**回答"钉在板上还是不在"。

    返回 ``None`` 表示"拿不到任何可信的涨跌停带" → 调用方必须返回"观测
    不可得"，**不得**放宽。

    ``profile_transport_down`` 是一个 :class:`threading.Event`：一旦东财行情
    入口出现**传输层**失败（现网实测：``push2*`` 整段 TCP 443 FAIL），本轮
    剩下的 code 就直接走观测/推导，不再逐票等待同一个死主机的超时。这既是
    延迟上的必需（200 票 × 每次超时会把整轮扫描拖垮），也是对"整段不可达"
    这一现网事实的直接承认。
    """
    name = str(quote.get("name") or "").strip()
    price = _finite_quote_number(quote.get("price"))
    observed_range = _quote_range_band(quote, price) if price is not None else None

    # ── 1. 东财个股档案（真实观测） ──────────────────────────────────────
    if profile_transport_down is None or not profile_transport_down.is_set():
        try:
            payload, profile_at = _get(("profile", day, code),
                "https://push2.eastmoney.com/api/qt/stock/get",
                {"fltt": 2, "invt": 2, "secid": f"{1 if code.startswith('6') else 0}.{code}",
                 "fields": "f57,f58,f189,f51,f52,f43,f86"},
                # 这份档案带着实时价格与行情时间，缓存窗口必须与
                # `STATUS_MAX_AGE_SECONDS` 一致，否则它在被读取之前
                # 就已经过期，整段窗口内都拿不到可交易观测。
                ttl_seconds=STATUS_MAX_AGE_SECONDS)
            profile = payload.get("data") or {}
            if payload.get("rc") != 0:
                profile = {}
            lower = _finite_quote_number(profile.get("f52"))
            upper = _finite_quote_number(profile.get("f51"))
            profile_price = _finite_quote_number(profile.get("f43"))
            if (profile.get("f57") == code and profile.get("f58") and lower is not None
                    and upper is not None and profile_price is not None and profile.get("f86")):
                listing = None
                try:
                    listing = dt.datetime.strptime(str(profile.get("f189")), "%Y%m%d").date()
                except (TypeError, ValueError):
                    listing = None
                price_at = dt.datetime.fromtimestamp(float(profile["f86"]), TZ)
                if not lower < profile_price < upper:
                    # At a price limit, a last price cannot prove the book is
                    # unlocked. 这条路径的既有语义**不改**：拿不准就不成立。
                    return None
                return {
                    "basis": "profile",
                    "profile": profile,
                    "profile_at": profile_at,
                    "listing": listing,
                    "lower": lower, "upper": upper, "price": profile_price,
                    "price_at": price_at,
                    "source": PROFILE_SOURCE,
                    "locked": False, "direction": None,
                    "derived_evidence": None,
                    "range_evidence": observed_range,
                }
        except requests.RequestException:
            # 传输层失败 = 该主机整段不可达（现网实测）。本轮不再逐票重试。
            if profile_transport_down is not None:
                profile_transport_down.set()
        except (ValueError, TypeError, KeyError, OverflowError):
            pass

    # ── 2. 行情信封自带的日内 high/low（**观测**） ─────────────────────────
    #
    # 腾讯 / 新浪报文里有最高（idx33）与最低（idx34），数据层已经透传进
    # quote 信封。现价**严格位于**日内区间内部 → 价格没有钉在极值上 → 未锁死。
    #
    # 这是**观测**而非推导：观测到的区间就是当天真实的成交区间，不需要任何
    # 假设。Lead 的优先级：真实 high/low 观测 > 昨收推导。
    #
    # 现价**恰好等于**极值时，日内区间**证明不了**它是不是涨跌停（那需要
    # 真实涨跌停价）→ 继续往下走推导路径（推导能给出真实涨跌停价）。
    #
    # 名称必须可得：``is_st`` 由它判定，名称不明时不能断言"不是 ST"。
    if observed_range is not None and name:
        price_at = _time(quote.get("quote_at"))
        if price_at is not None and observed_range["low"] < price < observed_range["high"]:
            return {
                "basis": "range",
                "profile": {"f57": code, "f58": name},
                # 观测值的时点取**该行情自己的时间戳**。
                "profile_at": quote.get("quote_at"),
                "listing": None,
                "lower": observed_range["low"], "upper": observed_range["high"],
                "price": price,
                "price_at": price_at,
                "source": RANGE_SOURCE,
                "locked": False, "direction": None,
                "derived_evidence": None,
                "range_evidence": observed_range,
            }

    # ── 3. 推导：昨收 × 法定幅度（**显式标注为 derived**） ────────────────
    #
    # 没有昨收就没有昨收 → 维持"观测不可得"的失败语义，绝不编造。
    derived = None
    if name and price is not None:
        prev_close = _prev_close_of(quote)
        if prev_close is not None and not _prev_close_conflicts(quote, price, prev_close):
            derived = _derived_limit_band(code, name=name, price=price, prev_close=prev_close)
    if derived is not None:
        price_at = _time(quote.get("quote_at"))
        if price_at is not None:
            return {
                "basis": "derived",
                "profile": {"f57": code, "f58": name},
                # 推导值的时点取**该行情自己的时间戳**，绝不伪造成东财档案时点。
                "profile_at": quote.get("quote_at"),
                "listing": None,
                "lower": derived["lower"], "upper": derived["upper"], "price": price,
                "price_at": price_at,
                "source": DERIVED_LIMIT_SOURCE,
                "locked": derived["locked"], "direction": derived["direction"],
                "derived_evidence": {
                    "basis": "derived_from_prev_close",
                    "prev_close": derived["prev_close"],
                    "limit_amplitude": derived["amplitude"],
                    "lower": derived["lower"], "upper": derived["upper"],
                    "locked": derived["locked"], "direction": derived["direction"],
                    "quote_source": str(quote.get("source") or quote.get("quote_source") or "") or None,
                    "quote_at": quote.get("quote_at"),
                },
                "range_evidence": observed_range,
            }
    return None


def prefetch(quotes, *, asof_day):
    """Current-day only, bounded to 200 fresh validated provider quotes."""
    day = str(asof_day)[:10]
    if day != dt.datetime.now(TZ).date().isoformat():
        return {}
    selected = {str(k): v for k, v in quotes.items()
                if v.get("quote_source") == "live"
                and MDC.is_execution_quote_usable(MDC.classify(
                    MDC.symbol_quote_snapshot(v, asof_day=day), MDC.EXECUTION_QUOTE_POLICY,
                    now=dt.datetime.now(TZ), asof_day=day))}
    if not selected or len(selected) > 200:
        return {}
    try:
        suspensions, suspension_at = _suspensions(day)
    except Exception:
        return {}
    try:
        reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=dt.datetime.now(TZ), asof_day=day)
        market = ({str(r.get("code")): dict(r) for r in reading.rows()}
                  if reading.status == MDC.STATUS_FRESH and reading.snapshot is not None
                  and reading.snapshot.kind == MDSvc.KIND_FULL_MARKET_SNAPSHOT
                  and reading.snapshot.complete and reading.snapshot.as_of == day
                  and reading.snapshot.verification == MDC.VERIFICATION_VERIFIED else {})
        market_fingerprint = MDC.snapshot_fingerprint(reading.snapshot) if market else None
    except Exception:
        market, market_fingerprint = {}, None

    # 东财个股档案入口一旦出现传输层失败（现网实测：``push2*`` 整段 TCP 443
    # FAIL），本轮剩下的 code 就直接走推导，不再逐票等待同一个死主机。
    profile_transport_down = threading.Event()

    def one(item):
        code, quote = item
        try:
            if len(code) != 6 or not code.isdigit() or not code.startswith(("0", "3", "6")):
                return code, None
            primary_quote = quote
            cross = quote.get("quote_cross_check") or {}
            if (cross.get("passed") is True and cross.get("code") == code
                    and cross.get("source") in ("tencent_public_quote", "sina_public_quote")
                    and float(cross.get("price") or 0) > 0):
                current_at = _time(cross.get("quote_at"))
                if current_at is not None and current_at > _time(quote["quote_at"]):
                    # This is a status reference only. Never retimestamp the
                    # older primary price or its fund-flow fields.
                    refreshed = {**quote, "price": float(cross["price"]),
                                 "quote_at": current_at.isoformat(),
                                 "name": cross.get("name") or quote.get("name")}
                    # 现价与涨跌幅必须来自**同一次响应**：价格换成核验源的
                    # 之后，若涨跌幅还留在主源，两份数据就会互相矛盾，
                    # 昨收自洽校验会误判"昨收不可信"。核验源没带涨跌幅
                    # （哨兵 999 / 缺失）时就**不换**，宁可用主源的成对数据。
                    cross_pct = cross.get("pct")
                    if isinstance(cross_pct, (int, float)) and math.isfinite(cross_pct) \
                            and abs(cross_pct) < 100:
                        refreshed["pct"] = float(cross_pct)
                    quote = refreshed
            row = market.get(code) or {}
            price = float(quote["price"])
            low, high = row.get("low"), row.get("high")
            row_at = _time(row.get("quote_at"))
            interior = (isinstance(low, (int, float)) and isinstance(high, (int, float))
                        and math.isfinite(low) and math.isfinite(high) and 0 < low < price < high
                        and row_at is not None and row_at.date().isoformat() == day
                        and row_at <= dt.datetime.now(TZ))
            # 涨跌停带的来源，证据等级从高到低：
            #   1. ``market``   —— 完整全市场快照的日内 low/high（**观测**）；
            #   2. ``profile``  —— 东财 ``stock/get`` 的真实 ``f51``/``f52``（**观测**）；
            #   3. ``derived``  —— 昨收 × 法定幅度（**推导**，显式标注）；
            #   4. ``range``    —— 行情信封自带的日内 low/high（**观测**）。
            #
            # 第 3 条是本模块为"东财整段不可达"准备的兜底：腾讯 / 新浪可达且带
            # 昨收。推导值**永远**带 ``derived`` 标记，绝不冒充观测。
            if interior and row.get("name"):
                # A complete current-market observation proves current membership,
                # not an IPO date or historical membership. An interior price is
                # strictly inside any fixed daily limit: observed low/high already
                # lie inside that limit. No order-book lock is inferred at extrema.
                listing, lower, upper = None, float(low), float(high)
                profile = {"f57": code, "f58": quote.get("name") or row["name"]}
                profile_at = reading.snapshot.saved_at or reading.snapshot.observed_at
                price_at = _time(quote["quote_at"])
                source = MARKET_SOURCE
                market_identity = market_fingerprint
                limit_basis = "market"
                locked, lock_direction = False, None
                derived_evidence = None
                range_evidence = {"low": lower, "high": upper}
            else:
                basis = _limit_basis(code, quote, day, profile_transport_down=profile_transport_down)
                if basis is None:
                    # 既没有观测、也无法推导 → 观测不可得（不放宽）。
                    return code, None
                profile, profile_at = basis["profile"], basis["profile_at"]
                listing, lower, upper = basis["listing"], basis["lower"], basis["upper"]
                price, price_at = basis["price"], basis["price_at"]
                source, market_identity = basis["source"], None
                limit_basis = basis["basis"]
                locked, lock_direction = basis["locked"], basis["direction"]
                derived_evidence = basis["derived_evidence"]
                range_evidence = basis["range_evidence"]
            price_age = (dt.datetime.now(TZ) - price_at).total_seconds()
            if (not all(math.isfinite(v) for v in (lower, upper, price))
                    or not 0 < lower <= price <= upper
                    or price_at.date().isoformat() != day
                    or not -120 <= price_age <= STATUS_MAX_AGE_SECONDS
                    or abs(price - float(quote["price"])) / price > 0.005):
                # At a price limit, a last price cannot prove the book is unlocked.
                return code, None
            if limit_basis == "derived":
                # 推导路径：现价**恰好**落在推导边界上 → 钉在涨跌停板 → **确实锁死**。
                #
                # 这与"拿不到观测"是**两回事**，证据必须能区分开：
                #   * 锁死  → ``is_price_limit_locked=True`` + ``price_limit_direction``
                #             → 台账里是一条**已证事实**，卖出被"跌停锁死"拦下；
                #   * 观测不可得 → 整条观测缺席 → ``evidence_present=False``
                #             → 判定层报 ``UNKNOWN_STATE``。
                #
                # 两者在执行计划上都表现为"不成交"，但在**语义与审计**上完全不同：
                # 前者说"这只票现在钉在跌停板上"，后者说"我不知道"。把真跌停
                # 写成后者会让审计无法区分"数据源坏了"和"股票真的锁死"。
                if not lower < price < upper:
                    locked = True
                    lock_direction = "up" if price >= upper else "down"
            observed = max(_time(profile_at), _time(suspension_at), _time(quote.get("quote_at")))
            suspended = _suspended(suspensions, code, observed)
            if (listing is not None and listing > observed.date()) or suspended is None:
                return code, None
            volume = quote.get("volume")
            fields = {"is_listed": True, "listing_date": listing.isoformat() if listing is not None else None,
                      "is_st": "ST" in profile["f58"].upper() or "退" in profile["f58"],
                      "is_suspended": suspended, "has_market_quote": True,
                      "has_trade_volume": (volume > 0 if isinstance(volume, (int, float))
                                           and math.isfinite(volume) else None),
                      "is_price_limit_locked": bool(locked)}
            if lock_direction:
                fields["price_limit_direction"] = lock_direction
            return code, {"code": code, "session_date": day, "fields": fields,
                          "observed_at": observed.isoformat(), "effective_at": observed.isoformat(),
                          "source_observations": {"profile": profile_at, "suspension": suspension_at,
                                                  "quote": quote.get("quote_at"), "latest": observed.isoformat(),
                                                  "limit_basis": limit_basis},
                          "source_evidence": {"profile": profile, "suspension_count": len(suspensions),
                                              "status_price_reference": {"price": quote["price"], "quote_at": quote["quote_at"]},
                                              "primary_price_reference": {"price": primary_quote["price"], "quote_at": primary_quote["quote_at"]},
                                              "market_snapshot_fingerprint": market_identity,
                                              "observed_range": {"low": lower, "high": upper},
                                              "limit_basis": limit_basis,
                                              "derived_from_prev_close": derived_evidence,
                                              "observed_intraday_range": range_evidence,
                                              "suspension_rows": [r for r in suspensions if r["SECURITY_CODE"] == code],
                                              "suspension_digest": hashlib.sha256(json.dumps(
                                                  suspensions, sort_keys=True).encode()).hexdigest()},
                          "source": source}
        except (ValueError, TypeError, KeyError, OverflowError, requests.RequestException):
            return code, None
    with ThreadPoolExecutor(max_workers=min(8, len(selected))) as executor:
        return {code: fact for code, fact in executor.map(one, selected.items()) if fact is not None}


class _PrefetchedProvider(TI.TradabilityFactProvider):
    provider_id = "current_public_status"
    provider_version = "1"

    def __init__(self, observation):
        self.observation = observation

    def fetch(self, code, session):
        item = self.observation
        if code != item["code"] or session != item["session_date"]:
            raise ValueError("observation identity mismatch")
        return TI.ProviderResult(self.provider_id, self.provider_version, TI.OUTCOME_EVIDENCE,
                                 evidence=item["fields"], observed_kind=TI.OBSERVED_RETRIEVED_AT,
                                 observed_at=item["observed_at"], effective_at=item["effective_at"])


def persist(conn, quote, *, code, asof_day):
    """Ingest prefetched facts through the existing owner, without committing."""
    item = quote.get("tradability_observation")
    if not isinstance(item, dict):
        return
    instant = _time(quote.get("execution_asof"))
    observed, effective = _time(item.get("observed_at")), _time(item.get("effective_at"))
    if (item.get("code") != str(code) or item.get("session_date") != str(asof_day)[:10]
            or not instant or not observed or not effective or max(observed, effective) > instant
            or (instant - observed).total_seconds() > STATUS_MAX_AGE_SECONDS
            or observed.date().isoformat() != str(asof_day)[:10]
            or item.get("source") not in (PROFILE_SOURCE, MARKET_SOURCE, DERIVED_LIMIT_SOURCE,
                                         RANGE_SOURCE)
            or quote.get("quote_validation") not in {"cross_source_checked", "range_timestamp_checked"}):
        raise ValueError("current tradability identity/time evidence invalid")
    fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True, allow_nan=False).encode()).hexdigest()
    TI.IngestionService([_PrefetchedProvider(item)], TA.TradabilityArchiveRepository(conn),
                        cutoff=instant.isoformat(), audit_conn=conn).ingest(
        [str(code)], [str(asof_day)[:10]], write=True, run_id=f"live:{fingerprint}")
