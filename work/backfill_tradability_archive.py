# -*- coding: utf-8 -*-
"""Historical Tradability Archive 回填工具（薄操作员 CLI）。

用法::

    # 单日 dry-run（默认只测量，不写库）
    python work/backfill_tradability_archive.py --session 2025-06-10 --dry-run

    # 日期范围 + 显式写入（日期范围按 A 股交易日历过滤，非自然日）
    python work/backfill_tradability_archive.py --from 2025-06-01 --to 2025-06-30 --write

    # 指定代码列表（逗号分隔）
    python work/backfill_tradability_archive.py --session 2025-06-10 --codes 000001,600000

本 CLI **只**做 argument parsing / CLI 格式化 / exit code，绝不绕过摄取层直接写
SQL，也**不复制**任何回填编排逻辑——全部逻辑在
:mod:`backend.tradability_backfill`。写库 authority 与 PIT / 冲突 / 幂等逻辑全部
在 ingestion service 内。

默认 dry-run：实际写入必须显式 ``--write``。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import data_paths  # noqa: E402
import tradability_archive as TA  # noqa: E402
import tradability_backfill as TB  # noqa: E402
import tradability_ingestion as TI  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Historical Tradability Archive 回填")
    parser.add_argument("--session", help="单个 session（YYYY-MM-DD）")
    parser.add_argument("--from", dest="from_date", help="日期范围起点（YYYY-MM-DD）")
    parser.add_argument("--to", dest="to_date", help="日期范围终点（YYYY-MM-DD）")
    parser.add_argument("--codes", help="逗号分隔的代码列表；缺省用当前 universe")
    parser.add_argument("--limit", type=int, default=None, help="只回填前 N 只（调试）")
    parser.add_argument("--write", action="store_true", help="实际写入 archive（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只测量与报告，不写库")
    parser.add_argument("--run-id", default=None, help="显式 run_id（幂等重放用同一个）")
    args = parser.parse_args(argv)

    start = args.from_date
    end = args.to_date

    if args.session and (start or end):
        print("--session 与 --from/--to 互斥")
        return 2
    if not args.session and not start:
        print("需要 --session 或 --from")
        return 2

    sessions = TB.resolve_sessions(start, end, session=args.session)
    codes = TB.resolve_codes(args.codes, limit=args.limit)
    if not codes:
        print("没有可回填的代码（universe 为空或 --codes 无效）")
        return 2

    write = bool(args.write and not args.dry_run)

    db_path = data_paths.data_path("paper_trading.sqlite3")
    if not os.path.exists(db_path):
        print(f"数据库不存在（跳过）: {db_path}")
        return 2
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        if write:
            # 只有实际写入才准备 schema；dry-run 要求数据库已 migration，
            # 绝不通过 ensure_schema 悄悄建表（dry-run 无数据库副作用）。
            TA.ensure_schema(conn)
            TI.ensure_ingestion_schema(conn)
        providers = TB.default_providers()
        result = TB.run_backfill(
            conn,
            providers,
            codes,
            sessions,
            write=write,
            run_id=args.run_id,
        )
    finally:
        conn.close()

    print("=== backfill result ===")
    print(f"run_id: {result.run_id}")
    print(f"status: {result.status}")
    print(f"mode: {'write' if write else 'dry-run'}")
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
