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
import re
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


class ScopeError(ValueError):
    """操作员给出的 scope 无法解析成有效工作集（operator error，不是数据 unknown）。"""


class CodeScopeError(ScopeError):
    """显式 ``--codes`` 未解析出任何**合法**代码。

    与"``codes is None`` → 用默认 universe"是**两件不同的事**。操作员显式给了一个
    范围（哪怕它在语法上是空的、或全是非法代码），把那个范围静默放大成全市场正是
    本类要阻止的事故：``--codes ,`` 配 ``--write`` 会变成一次全市场写入，而操作员
    以为自己只处理了很小的一批。
    """


class SessionScopeError(ScopeError):
    """请求的日期范围解析成**零个交易日**（倒序 / 只有周末 / 只有法定休市日 / 非法日期）。

    零 session 的 run 不得报告 ``completed``：它没有覆盖任何 ``(code, session)`` 对，
    也不该落一行审计把自己记成一次成功的回填。
    """


_SESSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _session_text(value: Any) -> Optional[str]:
    """严格 ``YYYY-MM-DD`` → 规范字符串；其余（含空、非日期、超长）→ ``None``。

    只做**日期格式**校验。某个日期是不是交易日由 :func:`_is_session` /
    ``selection_labels.sessions_between`` 决定，本模块不另造交易日规则。
    """
    if value is None:
        return None
    text = str(value).strip().replace("/", "-")
    if not _SESSION_RE.fullmatch(text):
        return None
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _is_session(text: str, calendar: Any = None) -> bool:
    """该日期是否为交易日。委托仓库唯一权威判定，可注入 ``calendar`` 以便测试。"""
    if calendar is not None:
        try:
            return bool(calendar(_dt.date.fromisoformat(text)))
        except Exception:  # pragma: no cover - 注入日历自身异常 → fail closed
            return False
    import universe as U  # 权威交易日历（:func:`universe.is_trade_day`）

    return bool(U.is_trade_day(text))


def normalize_code(value: Any) -> Optional[str]:
    """单个 code token → 规范 6 位代码；无法成立时 ``None``。

    归一化委托 :func:`disclosure_timeline.normalize_code`（它负责 ``SH`` / ``SZ`` /
    ``BJ`` 前缀与补零），接受判据沿用 :func:`marketdata_feeds.normalize_codes` 的
    口径——**必须**是 6 位数字。本模块**不**另造第三套 code 规则。
    """
    if value is None:
        return None
    raw = str(value).strip().upper()
    if raw.startswith(("SH", "SZ", "BJ")):
        raw = raw[2:]
    # 严格判据先于归一化：``normalize_code`` 会补零 / 剥非数字字符，``"12345"``
    # 会被它补成 ``"012345"``、``"600000abc"`` 会被它读成 ``"600000"``。操作员输入
    # 必须精确，否则一次笔误就静默变成另一只股票的写入。
    if len(raw) != 6 or not raw.isdigit():
        return None
    from disclosure_timeline import normalize_code as _canonical

    canonical = _canonical(raw)
    if len(canonical) != 6 or not canonical.isdigit():
        return None
    return canonical


def normalize_codes(codes: Any) -> list:
    """显式 code 输入 → 规范代码列表（保序去重）。

    任何非法 token、或最终解析为空 → :class:`CodeScopeError`。**绝不**静默丢弃
    非法代码、也绝不回退默认 universe。
    """
    if isinstance(codes, str):
        tokens: list = codes.split(",")
    elif codes is None:
        tokens = []
    else:
        tokens = list(codes)
    cleaned = [str(token).strip() for token in tokens]
    cleaned = [token for token in cleaned if token]
    if not cleaned:
        raise CodeScopeError("显式 --codes 未解析出任何代码（空输入或只有分隔符）")
    out: list = []
    invalid: list = []
    for token in cleaned:
        canonical = normalize_code(token)
        if canonical is None:
            invalid.append(token)
        elif canonical not in out:
            out.append(canonical)
    if invalid:
        raise CodeScopeError(
            "显式 --codes 含非法代码: " + ", ".join(sorted(set(invalid)))
        )
    if not out:
        raise CodeScopeError("显式 --codes 未解析出任何代码")
    return out


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

    显式 ``--session`` 保留单 session 语义（不经过范围过滤），但仍要求它是**合法日期
    且是交易日**——"零个交易日"的请求同样是操作员错误，不得静默成功。

    任何解析成零 session 的输入（倒序范围 / 只有周末 / 只有休市日 / 非法日期）→
    :class:`SessionScopeError`。
    """
    if session:
        text = _session_text(session)
        if text is None:
            raise SessionScopeError(f"非法 session 日期: {session!r}")
        if not _is_session(text, calendar):
            raise SessionScopeError(f"{text} 不是交易日，无 session 可回填")
        return [text]
    from selection_labels import sessions_between  # 复用权威交易日历

    # ``None`` = 未提供（可用今天兜底）；**显式给了值但解析不出日期** = operator error。
    if start is not None and _session_text(start) is None:
        raise SessionScopeError(f"非法起始日期: {start!r}")
    if end is not None and _session_text(end) is None:
        raise SessionScopeError(f"非法结束日期: {end!r}")
    first = start or _dt.date.today().isoformat()
    last = end or first
    if calendar is None:
        sessions = sessions_between(first, last)
    else:
        sessions = sessions_between(first, last, calendar=calendar)
    if not sessions:
        raise SessionScopeError(
            f"日期范围 {first} → {last} 解析出 0 个交易日，无 session 可回填"
        )
    return sessions


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
    """把 ``--codes`` / ``--limit`` 解析成代码列表；缺省用当前 universe。

    **两种输入必须严格区分**：

    * ``codes is None``（未传 ``--codes``）→ 允许使用默认 universe；
    * ``codes`` 显式给出但归一化后为空 / 含非法代码 → :class:`CodeScopeError`。

    绝不允许"显式 targeted input 解析失败 → 自动 fallback 全市场"：在 ``--write``
    下，一次 ``--codes ,`` 会从小范围命令放大成全市场写入。
    """
    if codes is None:
        listing = load_listing_records()
        selected = sorted(listing.keys())
    else:
        selected = normalize_codes(codes)
    return selected[:limit] if limit else selected


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
