# -*- coding: utf-8 -*-
"""Historical Tradability Archive 回填的**应用 / 编排层**。

本模块是回填工具的可复用实现，供三处消费：

* ``work/backfill_tradability_archive.py`` —— 薄操作员 CLI（argument parsing /
  格式化 / exit code），**只调用**本模块，不复制第二套逻辑；
* ``backend/test_*`` —— 契约测试；
* 未来的 scheduler —— 复用同一套 ``run_backfill`` / scope 解析。

为什么放 ``backend/`` 而不是 ``work/``：Dockerfile 只 ``COPY backend/frontend/
deploy``，``work/`` 不进运行时镜像。把编排逻辑留在 ``work/`` 会让
``docker-smoke``（``--network none`` 在镜像内跑 backend unit suite）无法
import 它——这是本 PR 要修掉的事故根因。分层因此固定为：

    backend/tradability_backfill.py   = 可导入的生产 / 应用代码
    work/backfill_tradability_archive.py = 薄操作员 CLI

写库 authority 与 PIT / 冲突 / 幂等逻辑全部在
:mod:`tradability_ingestion.IngestionService` 内，本模块**绝不**绕过摄取层
直接写 SQL。

默认 dry-run：实际写入必须显式 ``--write``。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import uuid
from typing import Any, Mapping, Optional, Sequence

try:  # ``backend`` 在 sys.path（生产与 ``cd backend`` 测试）
    import tradability_archive as TA
    import tradability_ingestion as TI
except ImportError:  # pragma: no cover - package-style import
    from . import tradability_archive as TA  # type: ignore
    from . import tradability_ingestion as TI  # type: ignore


# ───────────────────────────── scope resolution ─────────────────────────────


def resolve_sessions(
    start: Any = None,
    end: Any = None,
    *,
    session: Any = None,
    calendar: Any = None,
) -> list:
    """把 ``--session`` / ``--from`` / ``--to`` 解析成**实际 trading sessions**。

    coverage contract 是 ``requested code × requested trading session``，不是
    ``requested code × calendar day``。因此日期范围**不得**按自然日逐日枚举：
    周末 / 法定休市日不能进入 ``requested_session_count``、coverage denominator、
    unknown / missing evidence 计数。

    复用仓库唯一的 A 股交易日历判定（:func:`selection_labels.sessions_between`
    → ``universe.is_trade_day``），本模块不另造交易日规则。

    显式 ``--session`` 保留单 session 语义（不经过日历过滤），作为操作员手动
    指定单一 session 的既有契约。
    """
    if session:
        return [str(session)]
    from selection_labels import sessions_between  # 复用权威交易日历

    first = start or _dt.date.today().isoformat()
    last = end or first
    if calendar is None:
        return sessions_between(first, last)
    return sessions_between(first, last, calendar=calendar)


def load_listing_records() -> Mapping[str, Mapping[str, Any]]:
    """从当前 ``universe.json`` 取上市状态，作为 **historical reference**（不伪造 PIT）。

    仓库当前没有上市/退市日历史源，``universe.json`` 也不含 list_date /
    delist_date。因此这里只能给"今天仍存在"的 code 一个弱上市事实
    （``listing_date`` 缺失），observed_kind 记 retrieved_at——这条证据**不能**
    用于过去 decision_time。
    """
    try:
        import universe as U

        rows = U.load_universe()
    except Exception:  # pragma: no cover - 防御无 universe 环境
        rows = []
    records: dict = {}
    for row in rows or []:
        code = str(row.get("code") or "").strip()
        if len(code) == 6 and code.isdigit():
            # 当前快照里存在 = 今天仍在册。没有 listing_date/delisting_date，
            # 因此 is_listed 只能被 archive 的 _resolve_listed 判为 None（未知）。
            records[code] = {}
    return records


def resolve_codes(codes: Any = None, *, limit: Optional[int] = None) -> list:
    """把 ``--codes`` / ``--limit`` 解析成代码列表；缺省用当前 universe。"""
    if codes is None:
        selected: Optional[list] = None
    elif isinstance(codes, str):
        selected = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        selected = [str(c).strip() for c in codes if str(c).strip()]
    if selected:
        return selected[:limit] if limit else selected
    listing = load_listing_records()
    if limit:
        return sorted(listing.keys())[:limit]
    return sorted(listing.keys())


# ───────────────────────────── provider construction ─────────────────────────


def build_kline_reader():
    """把共享 K 线缓存包装成 ``kline_reader(code) -> {date: {close, volume}}``。

    每个 code 的 K 线在**本次回填运行内只解析一次**：多 session 回填会反复调用
    ``reader(code)``，若不缓存，每次都要 ``load_shared_kline`` →
    ``load_cached_kline`` → ``pandas.read_csv`` 重读整个 CSV 并重建 date 映射，
    30 天范围就把同一份历史读 30 遍，市场级范围被重复磁盘 IO 与解析拖垮。
    """
    import data_fetcher as DF

    cache: dict = {}

    def reader(code):
        if code in cache:
            return cache[code]
        frame = DF.load_shared_kline(code)
        if frame is None or frame.empty:
            out = {}
        else:
            out = {}
            for index, row in frame.iterrows():
                date = str(index.date()) if hasattr(index, "date") else str(index)[:10]
                out[date] = {
                    "close": float(row.get("close")) if row.get("close") is not None else None,
                    "volume": float(row.get("volume")) if row.get("volume") is not None else None,
                }
        cache[code] = out
        return out

    return reader


def build_security_state_archive():
    """加载 ``security_state_history.json``（若存在），否则返回 None → ST 全 unknown。"""
    try:
        import security_state_point_in_time as SSPIT

        payload = SSPIT.load_archive()
        return SSPIT.SecurityStateArchive.from_payload(payload)
    except Exception:  # pragma: no cover - 无状态源时 ST 全 unknown
        return None


def default_providers() -> list:
    """按仓库**真实事实来源**构造 provider 集。全部走 adapter，不写库。"""
    observed_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    providers = [
        TI.ListingStatusProvider(
            load_listing_records(),
            observed_kind=TI.OBSERVED_RETRIEVED_AT,
            observed_at=observed_at,
        ),
        TI.SecurityStateHistoryProvider(build_security_state_archive()),
        TI.HistoricalMarketSessionProvider(
            build_kline_reader(), observed_at=observed_at
        ),
    ]
    # 仓库没有 level2 / 封单证据，涨跌停锁定 provider 恒 unknown（诚实未知）。
    return providers


# ───────────────────────────── core backfill run ─────────────────────────────


def run_backfill(
    conn: sqlite3.Connection,
    providers: Sequence,
    codes: Sequence[str],
    sessions: Sequence[str],
    *,
    write: bool = True,
    run_id: Optional[str] = None,
):
    """核心回填：建 service、ingest、commit/rollback。

    * 显式注入 ``audit_conn``（修复 run audit 永远为空）；dry-run 是否写 audit 由
      ``IngestionService.ingest(write=...)`` 统一门控（``write=False`` 时 archive 与
      run audit 都不落库）。
    * 成功路径 ``commit()``，失败路径 ``rollback()``——否则 CLI 报告 ``persisted>0``
      但数据库为空（SQLite 关闭连接时回滚未提交事务，``repository.save`` 只是
      execute，不会自行 commit）。
    * ``write=False`` 是 dry-run：**完全不碰事务**，不 commit、不 rollback，也不做
      任何 schema / DB mutation。schema 准备是调用方（CLI 的 write 分支 / 测试
      setUp）的责任，本函数绝不隐式建表。
    """
    repo = TA.TradabilityArchiveRepository(conn)
    service = TI.IngestionService(providers, repo, audit_conn=conn)
    run_id = run_id or uuid.uuid4().hex
    if not write:
        return service.ingest(codes, sessions, write=False, run_id=run_id)
    try:
        result = service.ingest(codes, sessions, write=True, run_id=run_id)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result
