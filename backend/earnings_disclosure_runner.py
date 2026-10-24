# -*- coding: utf-8 -*-
"""「业绩公告摄取」CLI —— cron 的生产入口。

用法
----
  python earnings_disclosure_runner.py                    # 抓最近 2 天并落库
  python earnings_disclosure_runner.py --days 5           # 补抓更长窗口（重跑幂等）
  python earnings_disclosure_runner.py --dry-run          # 写临时库，不碰生产
  python earnings_disclosure_runner.py --status           # 只读查看当前账本

调度约定：每天 08:10–22:10 每 2 小时一轮（见 ``deploy/astock-codex.cron``）。
刻意**不**跳过非交易日：业绩预告/快报在周末与节假日也会披露，跳过就等于系统性漏报。
重跑是幂等的 —— 同一 ``art_code`` 只会落一行，``first_seen_at`` 不会被刷新。

退出码：0 = 本轮跑完（含"没有候选"）；1 = 列表都取不到，这一轮等于没跑。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

import earnings_disclosure_ingestion as ING
import earnings_disclosure_ledger as LEDGER
import earnings_disclosure_store as STORE


def _status(path=None) -> dict:
    """只读状态：账本计数 + 最近的事件。库还没建时返回 ``available=False``。"""
    target = path or STORE.db_path()
    try:
        with STORE.connect_readonly(path) as conn:
            counts = conn.execute(
                "SELECT verification_status, COUNT(*) FROM %s GROUP BY 1"
                % LEDGER.EARNINGS_EVENTS_TABLE
            ).fetchall()
            checks = conn.execute(
                "SELECT COUNT(*) FROM %s" % LEDGER.EARNINGS_CLAIM_CHECKS_TABLE
            ).fetchone()[0]
            latest = conn.execute(
                "SELECT first_seen_at, code, kind, report_period, verification_status"
                " FROM %s ORDER BY first_seen_at DESC LIMIT 10"
                % LEDGER.EARNINGS_EVENTS_TABLE
            ).fetchall()
    except LEDGER.EarningsLedgerError as exc:
        return {"available": False, "reason": exc.reason, "path": target}
    except sqlite3.Error as exc:
        return {"available": False, "reason": type(exc).__name__, "path": target}
    return {
        "available": True,
        "path": target,
        "by_status": {str(row[0]): int(row[1]) for row in counts},
        "claim_checks": int(checks or 0),
        "latest": [
            {"first_seen_at": row[0], "code": row[1], "kind": row[2],
             "report_period": row[3], "verification_status": row[4]}
            for row in latest
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=ING.DEFAULT_LOOKBACK_DAYS,
                        help="回看天数（含今天），默认 2")
    parser.add_argument("--max-pages", type=int, default=ING.DEFAULT_MAX_PAGES)
    parser.add_argument("--max-bodies", type=int, default=ING.DEFAULT_MAX_BODIES)
    parser.add_argument("--dry-run", action="store_true",
                        help="写临时库并打印路径，绝不碰生产库")
    parser.add_argument("--db", default="", help="显式数据库路径（排错用）")
    parser.add_argument("--status", action="store_true", help="只读查看账本")
    parser.add_argument("--observed-at", default="",
                        help="覆盖观测时刻（ISO8601 带时区，排错/回放用）")
    args = parser.parse_args(argv)

    path = args.db or None
    if args.status:
        print(json.dumps(_status(path), ensure_ascii=False))
        return 0

    if args.dry_run:
        directory = tempfile.mkdtemp(prefix="earnings-disclosure-")
        path = os.path.join(directory, STORE.DB_FILENAME)
        print(json.dumps({"dry_run": True, "path": path}, ensure_ascii=False))

    STORE.ensure(path)
    observed = args.observed_at or dt.datetime.now(dt.timezone.utc)
    try:
        with STORE.connect_writable(path) as conn:
            summary = ING.ingest_window(
                conn=conn,
                days=args.days,
                observed_at=observed,
                max_pages=args.max_pages,
                max_bodies=args.max_bodies,
            )
    except ING.IngestionError as exc:
        # 列表都取不到 = 这一轮没跑。cron 需要看到非零退出码，但不要抛栈。
        print(json.dumps({"error": "list_unavailable", "detail": str(exc)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
