# -*- coding: utf-8 -*-
"""R24 —— Market Data Authority：行情事实的**唯一业务权威**。

上层不再问：Eastmoney 怎么拿？cache 有没有？要不要 refresh？provider A 和 B
谁可信？多久算 stale？失败后 fallback 到哪里？它只问一个问题：

    在指定 as-of / freshness policy 下，系统目前拥有什么经过验证的市场事实？

──────────────────────── 调用方向（单向） ────────────────────────

    provider / cache implementation        （data_fetcher，不动）
                    ↑
         本模块（唯一 authority）
                    ↓
    Selection / Signal / Risk / Execution / Read Models

``data_fetcher`` **不知道**本模块存在；本模块只把它当 provider/cache 机制用。

──────────────────────── 两种访问模式，显式声明（§10） ────────────────

:func:`read_snapshot`  **只读**：只读缓存/持久化事实，**绝不**同步访问 provider。

    这是本模块存在的主要理由。R23 已确认的真实维护债：
    ``GET /api/paper/allocation-explain`` 每次只读请求都会穿透到 provider，
    在网络不可用时每次支付 ~13.8s 连接超时/重试。只读业务路径不能为了回答
    "当前已知事实是什么" 而偷偷发起 provider 网络刷新。

:func:`refresh_snapshot`  **刷新/决策**：显式允许联网，必须由调用方主动选择。

    只有 scheduled scan / 收盘任务 / 显式市场刷新 / 后台行情任务可以使用。
    禁止在深层 helper 里隐式决定 "cache miss → 自动访问网络"。

两条路径返回**同一个** :class:`market_data_contract.MarketDataReading`，
因此调用方永远能看到 ``fresh / stale / degraded / unavailable``，
而不是 ``{}`` / ``None`` / 空 list。

──────────────────────── 硬性边界 ────────────────────────

* 不拥有事务：不 BEGIN、不 commit、不 rollback，也不接受连接对象。
  因此 provider I/O **不可能**被搬进 DB writer transaction（§18）。
* 不读墙上时钟：``now`` 一律由调用方显式传入（缺省仅在 API/前端投影处补）。
* 不伪造数据：没有持久化事实就返回 unavailable，绝不构造 0 / 默认指数 /
  昨值冒充今值（§29）。
* cache 只是存储/投放机制，**不是** fact authority（§44）：``cache 有值``
  不等于 ``一定可信``，仍要过 as_of / freshness / verification。
* health 与 data fact 分离（§45）：本模块只回答数据事实；
  provider/系统健康由 ``data_fetcher.load_source_health`` 继续回答，
  两者不得互相推导。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

try:  # ``backend`` on sys.path (production and ``cd backend`` test runs)
    import data_fetcher as dfc_module
    import market_data_contract as MDC
except ImportError:  # pragma: no cover - package-style import
    from . import data_fetcher as dfc_module
    from . import market_data_contract as MDC

__all__ = [
    "KIND_FULL_MARKET_SNAPSHOT", "KINDS",
    "MarketDataAccessError",
    "full_market_snapshot_projection", "market_health_projection",
    "now_utc", "read_projection", "read_snapshot", "read_snapshot_with_meta",
    "refresh_rows", "refresh_snapshot", "snapshot_from_cached_payload",
]

#: 全市场横截面快照 —— 目前唯一的 Market Data fact kind。
#: 刻意**不**在这里堆一个万能 ``get_data(type, args, flags...)``：不同数据
#: （指数 / K线 / 板块流 / 基本面）有完全不同的生命周期与核验机制，塞进一个
#: god API 只会制造新的耦合。它们继续由各自的既有 owner 负责。
KIND_FULL_MARKET_SNAPSHOT = "full_market_snapshot"
KINDS = (KIND_FULL_MARKET_SNAPSHOT,)


class MarketDataAccessError(RuntimeError):
    """调用方以不允许的方式访问 Market Data（例如只读模式要求联网）。"""


# ---------------------------------------------------------------------------
# payload → snapshot
# ---------------------------------------------------------------------------


def _newest_quote_at(rows: Sequence[Mapping[str, Any]]) -> str | None:
    newest = None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        stamp = row.get("quote_at")
        if stamp and (newest is None or str(stamp) > str(newest)):
            newest = str(stamp)
    return newest


def _older_instant(first: Any, second: Any) -> str | None:
    """取两个时间戳中**较早**的一个（按解析后的绝对时刻比较）。

    不能用字符串比较：源时间戳带 ``+08:00``、本地 ``saved_at`` 是 UTC
    ``+00:00``，字典序与真实先后不一致。按解析后的时刻比较才是正确语义。

    刷新后写入的文件必然晚于源观测时点，所以正常情况下取到的是源时间。
    只有源时间戳出现未来漂移时才会取到本地写入时间 —— 那时"更旧"的那个
    才是真正能证明的下界，属保守方向。
    """
    parsed = [
        (MDC._parse_instant(item), str(item))
        for item in (first, second)
    ]
    usable = [(instant, text) for instant, text in parsed if instant is not None]
    if not usable:
        return None
    return min(usable, key=lambda pair: pair[0])[1]


def snapshot_from_cached_payload(
    payload: Mapping[str, Any] | None,
    *,
    kind: str = KIND_FULL_MARKET_SNAPSHOT,
) -> MDC.MarketDataSnapshot | None:
    """把持久化 payload 归一成 snapshot；结构不完整时返回 ``None``。

    复用 ``data_fetcher`` 既有的完整性判据（``complete`` 标记 + 行数门槛 +
    90% 期望覆盖），**不复制**第二套判断标准。
    """
    if not isinstance(payload, Mapping):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    complete = bool(dfc_module._full_snapshot_payload_is_complete(payload))
    saved_at = payload.get("saved_at")
    observed_at = _older_instant(_newest_quote_at(rows), saved_at)
    try:
        expected = int(payload.get("expected_rows") or 0)
    except (TypeError, ValueError):
        expected = 0
    return _full_market_snapshot(
        rows=rows,
        observed_at=observed_at,
        saved_at=str(saved_at) if saved_at else None,
        complete=complete,
        expected_rows=expected,
        kind=kind,
    )


def _full_market_snapshot(
    *,
    rows,
    observed_at: str | None,
    saved_at: str | None,
    complete: bool,
    expected_rows: int,
    kind: str,
) -> MDC.MarketDataSnapshot:
    """全市场横截面快照的唯一构造点（R24 复审修正后的核验语义）。

    关键：这个 kind 的核验机制是**完整性与覆盖**（持久化 reader 的 complete
    marker + 行数/唯一代码/期望覆盖率门槛），**不是**逐票双源交叉核验。
    因此：

    * 完整性通过 → ``verification="verified"`` + ``verification_method=
      "coverage_integrity"`` —— 表示"本 kind 的 policy 通过了"，
      **不**表示每一行都有第二个源确认过；
    * 完整性不通过 → ``not_attempted`` + ``degraded_reason=incomplete``。

    修正前这里是直接标 ``verification="verified"`` 且不带 method，等于让一个
    单源快照在契约层看起来像双源验证过；R25/R27 消费时会据此高估可信度。
    需要双源保证的消费者必须用 :func:`market_data_contract.is_cross_source_verified`。
    """
    row_list = [row for row in rows if isinstance(row, Mapping)]
    return MDC.MarketDataSnapshot(
        kind=kind,
        rows=tuple(row_list),
        as_of=MDC.canonical_day(observed_at),
        observed_at=observed_at,
        saved_at=saved_at,
        source="eastmoney_clist_full_snapshot",
        complete=complete,
        expected_rows=expected_rows,
        verification=(
            MDC.VERIFICATION_VERIFIED if complete else MDC.VERIFICATION_NOT_ATTEMPTED
        ),
        verification_method=(
            MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY if complete
            else MDC.VERIFICATION_METHOD_NONE
        ),
        # 逐源证据：让"凭什么说它完整"可审计，而不是一个裸布尔。
        verification_detail=(
            {
                "policy": "coverage_integrity",
                "rows": len(row_list),
                "unique_codes": len({
                    str(row.get("code")) for row in row_list if row.get("code")
                }),
                "expected_rows": expected_rows,
            }
            if complete else {"policy": "coverage_integrity", "passed": False}
        ),
        degraded_reason=None if complete else MDC.REASON_INCOMPLETE,
    )


def _load_cached_snapshot_with_meta(
    kind: str,
) -> tuple[MDC.MarketDataSnapshot | None, Mapping[str, Any]]:
    """读持久化事实，并**保留完整 payload metadata**。

    R24 复审修正：旧实现只取 ``rows``，于是 ``saved_at`` / ``expected_rows``
    被丢成 ``None`` / ``0`` —— ``/api/health`` 的 ``live_snapshot.saved_at``
    与基于它算出的 ``age_seconds`` 也随之丢失，属未声明的行为回退。

    这里返回 ``(snapshot, payload)``，让需要源元数据的消费者（health 的
    data-validity 明细）拿得到原始字段，而不是被迫重新 ``open()`` 那个文件
    （那条旁路会绕过完整性校验）。
    """
    if kind != KIND_FULL_MARKET_SNAPSHOT:
        raise ValueError(f"unknown market data kind: {kind!r}")
    try:
        payload = dfc_module.load_market_snapshot_full_payload()
    except Exception:
        return None, {}
    if not isinstance(payload, Mapping):
        return None, {}
    return snapshot_from_cached_payload(payload, kind=kind), payload


def _load_cached_snapshot(
    kind: str,
) -> MDC.MarketDataSnapshot | None:
    """读持久化事实。**只做本地读取，绝不触网。**"""
    return _load_cached_snapshot_with_meta(kind)[0]


def read_snapshot_with_meta(
    policy: MDC.MarketDataPolicy = MDC.LIVE_MARKET_POLICY,
    *,
    now: Any,
    asof_day: Any = None,
    kind: str = KIND_FULL_MARKET_SNAPSHOT,
) -> tuple[MDC.MarketDataReading, Mapping[str, Any]]:
    """:func:`read_snapshot` + 原始 payload metadata。

    给需要源元数据的消费者（``/api/health`` 的 data-validity 明细：
    ``saved_at`` / ``expected_rows`` / 行覆盖）使用，让它们不必绕过
    authority 去 ``open()`` 那个文件。
    """
    deadline = _resolve_now(policy, now)
    snapshot, payload = _load_cached_snapshot_with_meta(kind)
    reading = MDC.classify(
        snapshot, policy, now=deadline, access_mode=MDC.ACCESS_READ,
        asof_day=asof_day,
    )
    return reading, payload


# ---------------------------------------------------------------------------
# 唯一读取入口（只读）
# ---------------------------------------------------------------------------


def read_snapshot(
    policy: MDC.MarketDataPolicy = MDC.LIVE_MARKET_POLICY,
    *,
    now: Any,
    asof_day: Any = None,
    kind: str = KIND_FULL_MARKET_SNAPSHOT,
) -> MDC.MarketDataReading:
    """**只读**读取当前已知事实。绝不访问 provider / 网络。

    有缓存但过期 → ``status=stale`` + 最后一份可信 rows（§28）。
    完全没有可信事实 → ``status=unavailable``（§29）。
    任何情况下都**不会**为了"让页面看起来正常"而同步刷新。
    """
    deadline = _resolve_now(policy, now)
    snapshot = _load_cached_snapshot(kind)
    return MDC.classify(
        snapshot, policy, now=deadline, access_mode=MDC.ACCESS_READ,
        asof_day=asof_day,
    )


# ---------------------------------------------------------------------------
# 唯一刷新入口（显式允许联网）
# ---------------------------------------------------------------------------


def refresh_snapshot(
    policy: MDC.MarketDataPolicy = MDC.LIVE_MARKET_POLICY,
    *,
    now: Any,
    asof_day: Any = None,
    force: bool = False,
    kind: str = KIND_FULL_MARKET_SNAPSHOT,
) -> MDC.MarketDataReading:
    """**显式允许联网**地从 provider 刷新，然后按同一 contract 判定。

    只有 scheduled / 收盘 / 显式刷新 / 后台行情任务可以调用它。失败时保留
    最后一次可信事实并标记 stale（既有语义：failed refresh 只回落
    full-market cache，绝不回落 20 页风险样本），没有旧事实才报 unavailable。

    本函数不打开、也不接受任何 DB 连接 —— provider I/O 与账本写入之间
    存在结构性隔离。
    """
    if kind != KIND_FULL_MARKET_SNAPSHOT:
        raise ValueError(f"unknown market data kind: {kind!r}")
    deadline = _resolve_now(policy, now)
    last_known = _load_cached_snapshot(kind)
    try:
        rows = dfc_module.fetch_market_snapshot_full(
            max_age=policy.max_age_seconds, force=bool(force),
        )
    except Exception:
        rows = None
    if not isinstance(rows, list) or not rows:
        return MDC.reading_for_refresh_failure(
            policy, last_known=last_known, now=deadline,
            reason=MDC.REASON_REFRESH_FAILED,
        )
    newest = _newest_quote_at(rows)
    # 刷新已把结果写入持久化层；优先用那份**经同一完整性判据**的 payload 作
    # metadata 来源（能拿到 saved_at / expected_rows / 真实覆盖），与只读路径
    # 完全同口径。拿不到时才退回"只凭本次 rows"的保守判定。
    persisted, _payload = _load_cached_snapshot_with_meta(kind)
    if persisted is not None and persisted.observed_at is not None:
        refreshed = persisted
    else:
        refreshed = _full_market_snapshot(
            rows=rows,
            observed_at=newest,
            saved_at=None,
            complete=len(rows) >= dfc_module.FULL_MARKET_MIN_ROWS,
            expected_rows=0,
            kind=kind,
        )
    return MDC.classify(
        refreshed, policy, now=deadline, access_mode=MDC.ACCESS_REFRESH,
        asof_day=asof_day,
    )


def _resolve_now(policy: MDC.MarketDataPolicy, now: Any) -> dt.datetime:
    """``now`` 必须显式传入（契约不读墙上时钟）。

    只接受 datetime；字符串/None 一律 fail fast，避免出现隐式 ``today``
    回退把陈旧事实判成新鲜。
    """
    if not isinstance(now, dt.datetime):
        raise MarketDataAccessError(
            f"market data policy {policy.name}: 'now' must be an explicit datetime"
        )
    return now


# ---------------------------------------------------------------------------
# 给业务编排层的两个薄入口（§16：墙钟只在编排层取**一次**）
# ---------------------------------------------------------------------------


def now_utc() -> dt.datetime:
    """本模块**唯一**读取墙上时钟的地方。

    契约零时钟；墙钟只在这里取一次，因此同一次判定里 ``now`` 与调用方看到的
    是同一个时刻，不会在契约内部再取一次而漂移。测试通过 monkeypatch 本函数
    即可让整个边界确定化。
    """
    return dt.datetime.now(dt.timezone.utc)


def read_projection(
    now: Any = None,
    *,
    policy: MDC.MarketDataPolicy = MDC.LIVE_MARKET_POLICY,
    asof_day: Any = None,
) -> dict[str, Any]:
    """只读投影的便利入口（只读路径共用；绝不联网）。"""
    return read_snapshot(
        policy, now=now if now is not None else now_utc(), asof_day=asof_day,
    ).projection()


def refresh_rows(
    *,
    force: bool = False,
    now: Any = None,
    policy: MDC.MarketDataPolicy = MDC.LIVE_MARKET_POLICY,
) -> list[dict[str, Any]]:
    """决策路径的刷新入口（**显式允许联网**），返回既有 ``list[dict]`` 形状。

    刻意不抛异常：provider 异常是这条路径的**正常失败模式**（既有行为是
    "refresh 失败 → 本轮不用旧快照"），由 reading 的 status/reason 表达，
    而不是让每个调用方各自包一层 try/except。

    刻意**不**返回 stale 事实：决策路径的既有语义是「本轮没刷新成功就不能用
    旧快照做横截面扫描」。只读路径请改用 :func:`read_snapshot`，它会保留
    最后一份可信事实并如实标记 stale。
    """
    try:
        reading = refresh_snapshot(
            policy, now=now if now is not None else now_utc(), force=force,
        )
    except Exception:
        return []
    if not reading.available or reading.freshness != MDC.FRESHNESS_FRESH:
        return []
    return [dict(row) for row in reading.rows() if isinstance(row, Mapping)]


def read_snapshot_legacy_shape() -> tuple[list[dict[str, Any]], str | None, str | None]:
    """只读全市场事实，返回既有消费者期望的 ``(rows, saved_at, source_label)``。

    存在的理由：``adaptive_engine`` / ``deepseek_advisor`` / ``trade_attribution``
    历史上各自 ``open()`` 那两个 JSON 文件，并在第一个文件失败时**回退**
    ``market_snapshot.json`` —— 那是 20 页风险样本（约 1/25 个市场），
    把它当全市场快照会系统性歪曲板块/个股统计。裸读也绕过了
    ``_full_snapshot_payload_is_complete``。

    这里只返回**经完整性校验**的 full-market 事实；拿不到就返回空元组，
    让调用方走"无快照"分支（如实降级，好过用错误 artifact 冒充）。
    """
    try:
        reading, payload = read_snapshot_with_meta(now=now_utc())
    except Exception:
        return [], None, None
    rows = [dict(row) for row in reading.rows()]
    if not rows:
        return [], None, None
    saved_at = payload.get("saved_at") if isinstance(payload, Mapping) else None
    return rows, (str(saved_at) if saved_at else None), "market_snapshot_full"


# ---------------------------------------------------------------------------
# 给 API / 前端的唯一投影（§22 / §46）
# ---------------------------------------------------------------------------


def full_market_snapshot_projection(*, now: Any) -> dict[str, Any]:
    """只读投影：前端只渲染，**不重算** freshness。

    返回 :meth:`MarketDataReading.projection` 的全部正交维度
    （status / availability / freshness / verification / as_of / reason），
    刻意**不**暴露 provider 机制（重试次数、熔断、缓存键）。
    """
    reading = read_snapshot(MDC.LIVE_MARKET_POLICY, now=now)
    return reading.projection()


def market_health_projection(*, now: Any) -> dict[str, Any]:
    """系统健康入口的 market_data 段：数据事实 + provider 健康，**分开**。

    §45：provider health 红色**不**自动作废最后已验证的 snapshot；
    snapshot 存在也**不**代表 provider 当前健康。两者并列展示、互不推导。

    ``data_fact`` 用 ``MARKET_HEALTH_POLICY``（1800s）判定 —— 这是 health 页
    既有的展示口径，**不是** ``LIVE_MARKET_POLICY`` 的 240s（R24 复审前
    1800s 硬编码在 ``main.health`` 里，属调用层的第二份 freshness 判决）。
    同时保留完整 payload metadata，让 consumer 不必绕过 authority 读文件。
    """
    reading, payload = read_snapshot_with_meta(MDC.MARKET_HEALTH_POLICY, now=now)
    provider_health = dfc_module.load_source_health() or {}
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    rows = rows if isinstance(rows, list) else []
    try:
        expected_rows = int(payload.get("expected_rows") or 0) if payload else 0
    except (TypeError, ValueError):
        expected_rows = 0
    return {
        "data_fact": reading.projection(),
        # 源元数据：health 的 data-validity 明细直接消费这些字段。
        "snapshot_meta": {
            "saved_at": payload.get("saved_at") if payload else None,
            "expected_rows": expected_rows,
            "rows": len(rows),
            "complete": bool(
                reading.snapshot.complete if reading.snapshot is not None else False
            ),
        },
        # provider/系统健康：诊断语义，普通运行页面默认不展示细节。
        "provider_health": {
            "healthy": bool(provider_health.get("healthy")),
            "checked_at": provider_health.get("checked_at"),
            "action": provider_health.get("action"),
        },
    }
