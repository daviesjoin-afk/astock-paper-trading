# -*- coding: utf-8 -*-
"""Historical Tradability Archive 回填工具。

用法::

    # 单日 dry-run（默认只测量，不写库）
    python work/backfill_tradability_archive.py --session 2025-06-10 --dry-run

    # 日期范围 + 显式写入
    python work/backfill_tradability_archive.py --start 2025-06-01 --end 2025-06-30 --write

    # 指定代码列表（逗号分隔）
    python work/backfill_tradability_archive.py --session 2025-06-10 --codes 000001,600000

本 CLI **只**走 :class:`tradability_ingestion.IngestionService`，绝不绕过摄取层
直接写 SQL。写库 authority 与 PIT / 冲突 / 幂等逻辑全部在 ingestion service 内。

默认 dry-run：实际写入必须显式 ``--write``。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sqlite3
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import data_paths  # noqa: E402
import tradability_archive as TA  # noqa: E402
import tradability_ingestion as TI  # noqa: E402


def _load_listing_records():
    """从当前 universe.json 取上市状态，作为**historical reference**（不伪造 PIT）。

    仓库当前没有上市/退市日历史源，universe.json 也不含 list_date / delist_date。
    因此这里只能给"今天仍存在"的 code 一个弱上市事实（``listing_date`` 缺失），
    observed_kind 记 retrieved_at——这条证据**不能**用于过去 decision_time。
    """
    try:
        import universe as U

        rows = U.load_universe()
    except Exception:
        rows = []
    records = {}
    for row in rows or []:
        code = str(row.get("code") or "").strip()
        if len(code) == 6 and code.isdigit():
            # 当前快照里存在 = 今天仍在册。没有 listing_date/delisting_date，
            # 因此 is_listed 只能被 archive 的 _resolve_listed 判为 None（未知）。
            records[code] = {}
    return records


def _build_kline_reader():
    """把共享 K 线缓存包装成 kline_reader(code) -> {date: {close, volume}}。"""
    import data_fetcher as DF

    def reader(code):
        frame = DF.load_shared_kline(code)
        if frame is None or frame.empty:
            return {}
        out = {}
        for index, row in frame.iterrows():
            date = str(index.date()) if hasattr(index, "date") else str(index)[:10]
            out[date] = {
                "close": float(row.get("close")) if row.get("close") is not None else None,
                "volume": float(row.get("volume")) if row.get("volume") is not None else None,
            }
        return out

    return reader


def _build_security_state_archive():
    """加载 security_state_history.json（若存在），否则返回 None → ST 全 unknown。"""
    try:
        import security_state_point_in_time as SSPIT

        payload = SSPIT.load_archive()
        return SSPIT.SecurityStateArchive.from_payload(payload)
    except Exception:
        return None


def _default_providers():
    """按仓库**真实事实来源**构造 provider 集。全部走 adapter，不写库。"""
    observed_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    providers = [
        TI.ListingStatusProvider(
            _load_listing_records(),
            observed_kind=TI.OBSERVED_RETRIEVED_AT,
            observed_at=observed_at,
        ),
        TI.SecurityStateHistoryProvider(_build_security_state_archive()),
        TI.HistoricalMarketSessionProvider(
            _build_kline_reader(), observed_at=observed_at
        ),
    ]
    # 仓库没有 level2 / 封单证据，涨跌停锁定 provider 恒 unknown（诚实未知）。
    return providers


def _resolve_sessions(args) -> list:
    if args.session:
        return [args.session]
    start = args.start or _dt.date.today().isoformat()
    end = args.end or start
    sessions = []
    cursor = _dt.date.fromisoformat(str(start)[:10])
    finish = _dt.date.fromisoformat(str(end)[:10])
    while cursor <= finish:
        sessions.append(cursor.isoformat())
        cursor += _dt.timedelta(days=1)
    return sessions


def _resolve_codes(args) -> list:
    if args.codes:
        return [c.strip() for c in args.codes.split(",") if c.strip()]
    listing = _load_listing_records()
    if args.limit:
        return sorted(listing.keys())[: args.limit]
    return sorted(listing.keys())


def run_backfill(conn, providers, codes, sessions, *, write=True, run_id=None):
    """核心回填：建 service、ingest、commit/rollback。

    * ``write=True``（生产回填）：显式注入 ``audit_conn``（修复 run audit 永远为空），
      成功路径 ``commit()``，失败路径 ``rollback()``——否则 CLI 报告 ``persisted>0``
      但数据库为空（SQLite 关闭连接时回滚未提交事务，``repository.save`` 只是
      execute，不会自行 commit）。
    * ``write=False``（dry-run）：不传 ``audit_conn``，只测量不落任何东西——
      archive 与 ``tradability_ingestion_runs`` 都保持空。
    """
    repo = TA.TradabilityArchiveRepository(conn)
    # dry-run 不写 audit：IngestionService 的 audit_conn 缺省即"只测量不落 run"。
    audit_conn = conn if write else None
    service = TI.IngestionService(providers, repo, audit_conn=audit_conn)
    run_id = run_id or uuid.uuid4().hex
    try:
        result = service.ingest(codes, sessions, write=write, run_id=run_id)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Historical Tradability Archive 回填")
    parser.add_argument("--session", help="单个 session（YYYY-MM-DD）")
    parser.add_argument("--start", help="日期范围起点（YYYY-MM-DD）")
    parser.add_argument("--end", help="日期范围终点（YYYY-MM-DD）")
    parser.add_argument("--codes", help="逗号分隔的代码列表；缺省用当前 universe")
    parser.add_argument("--limit", type=int, default=None, help="只回填前 N 只（调试）")
    parser.add_argument("--write", action="store_true", help="实际写入 archive（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只测量与报告，不写库")
    parser.add_argument("--run-id", default=None, help="显式 run_id（幂等重放用同一个）")
    args = parser.parse_args(argv)

    if args.session and (args.start or args.end):
        print("--session 与 --start/--end 互斥")
        return 2
    if not args.session and not args.start:
        print("需要 --session 或 --start")
        return 2

    sessions = _resolve_sessions(args)
    codes = _resolve_codes(args)
    if not codes:
        print("没有可回填的代码（universe 为空或 --codes 无效）")
        return 2

    db_path = data_paths.data_path("paper_trading.sqlite3")
    if not os.path.exists(db_path):
        print(f"数据库不存在（跳过）: {db_path}")
        return 2
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        TA.ensure_schema(conn)
        TI.ensure_ingestion_schema(conn)
        providers = _default_providers()
        result = run_backfill(
            conn,
            providers,
            codes,
            sessions,
            write=bool(args.write and not args.dry_run),
            run_id=args.run_id,
        )
    finally:
        conn.close()

    import json

    print("=== backfill result ===")
    print(f"run_id: {result.run_id}")
    print(f"status: {result.status}")
    print(f"mode: {'write' if (args.write and not args.dry_run) else 'dry-run'}")
    print(f"codes: {len(codes)}  sessions: {len(sessions)}")
    print(f"persisted: {len(result.persisted)}  conflicts: {len(result.conflicts)}")
    print(f"unprovable: {len(result.unprovable)}")
    print(f"run_fingerprint: {result.run_fingerprint}")
    print("provider_versions:", json.dumps(result.provider_versions, ensure_ascii=False))
    print("coverage:")
    print(json.dumps(result.coverage, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
