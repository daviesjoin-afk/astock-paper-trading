# -*- coding: utf-8 -*-
"""任务状态迁移的 owner 契约与"唯一写入者"守卫。

`paper_jobs` / `paper_job_runs` 的认领、复用、续期、完成、失败、启动回收、清理以前
在 `paper_trading` 里被复制了 9 份（两张表 × 各自分支）。任何一次谓词改动都要求人
手改齐 9 处，漏一处就是"重复成交 / 活任务被抢 / 死亡任务不回收"的一道口子。

本文件锁两件事：

1. **行为**：CAS 语义（owner + fencing + status='running'）、重试竞争只有一方拿到、
   租约丢失时 rowcount=0、被回收的记录**合并** detail 而不是覆盖、清理只删过期行、
   全程不提交调用方事务；
2. **归属**：生产代码里对这两张表的 INSERT/UPDATE/DELETE 只允许出现在
   `paper_job_lifecycle`（CREATE 与 SELECT 不限）。扫描器会把模块级常量
   （`{DAILY_TABLE}` 这类）渲染回真实表名，避免 f-string 拼表名绕过检查。
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import unittest

import paper_job_lifecycle as PJL

BACKEND = os.path.dirname(os.path.abspath(__file__))
OWNER = os.path.join(BACKEND, "paper_job_lifecycle.py")
WRITE_ON_TABLE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:paper_jobs|paper_job_runs|paper_dashboard_refresh_jobs)\b",
    re.IGNORECASE,
)
SCHEMA = """
CREATE TABLE paper_jobs(
    slot TEXT, market_date TEXT, status TEXT, detail TEXT,
    started_at TEXT, finished_at TEXT, owner_key TEXT,
    heartbeat_at TEXT, expires_at TEXT, fencing_token INTEGER);
CREATE TABLE paper_job_runs(
    run_key TEXT, slot TEXT, market_date TEXT, status TEXT, detail TEXT,
    started_at TEXT, finished_at TEXT, owner_key TEXT,
    heartbeat_at TEXT, expires_at TEXT, fencing_token INTEGER);
"""


def _module_constants(tree):
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Constant) \
                    and isinstance(value.value, str):
                values[target.id] = value.value
    return values


def _literal_text(node):
    """Flatten a str constant or an f-string into text with ``{NAME}`` placeholders."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        pieces = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                pieces.append(part.value)
            elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
                pieces.append("{" + part.value.id + "}")
            else:
                pieces.append("{}")
        return "".join(pieces)
    return None


def _rendered_strings(tree):
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        text = _literal_text(node)
        if text is None:
            continue
        for name, value in constants.items():
            text = text.replace("{" + name + "}", value)
        yield getattr(node, "lineno", 0), text


def _write_offenders(path):
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=path)
    offenders = []
    for lineno, text in _rendered_strings(tree):
        if WRITE_ON_TABLE.search(text):
            offenders.append((os.path.basename(path), lineno))
    return offenders


def _production_sources():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [name for name in dirs
                   if not name.startswith(".") and name not in {"__pycache__", ".venv"}]
        for name in files:
            if name.endswith(".py") and not name.startswith("test_"):
                yield os.path.join(root, name)


class JobTableWriterGuard(unittest.TestCase):
    def test_only_the_lifecycle_owner_writes_the_job_tables(self):
        offenders = []
        for path in _production_sources():
            if os.path.abspath(path) == os.path.abspath(OWNER):
                continue
            offenders.extend(_write_offenders(path))
        self.assertEqual([], offenders, f"这两张表只能由 paper_job_lifecycle 写: {offenders}")

    def test_owner_really_contains_the_statements(self):
        # 非空性：同一个扫描器必须在 owner 里找得到写入语句，否则上面那条"空结果"没有意义。
        self.assertNotEqual([], _write_offenders(OWNER))

    def test_scan_resolves_module_constants(self):
        # f-string 拼表名是历史上绕开扫描的常见写法，必须被渲染回来。
        synthetic = ast.parse(
            'JOBS = "paper_jobs"\n'
            'SQL = f"UPDATE {JOBS} SET status=?"\n'
        )
        rendered = [text for _lineno, text in _rendered_strings(synthetic)]
        self.assertTrue(any(WRITE_ON_TABLE.search(text) for text in rendered), rendered)


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)

    def row(self, table, **where):
        clause = " AND ".join(f"{key}=?" for key in where)
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE {clause}", tuple(where.values()),
        ).fetchone()

    def rows(self, table):
        return self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()

    def test_claim_then_probe_round_trip_for_both_tables(self):
        PJL.claim_job(self.conn, slot="intraday", market_date="2026-10-28",
                  run_key="2026-10-28:0930", owner="o1", expires_at="later",
                  fencing_token=1, now="T0")
        PJL.claim_job(self.conn, slot="close", market_date="2026-10-28",
                  owner="o2", expires_at="later", fencing_token=1, now="T0")
        self.assertEqual(1, len(self.rows("paper_job_runs")))
        self.assertEqual(1, len(self.rows("paper_jobs")))
        daily = PJL.read_job(self.conn, slot="close", market_date="2026-10-28")
        intraday = PJL.read_job(self.conn, slot="intraday",
                             market_date="2026-10-28", run_key="2026-10-28:0930")
        self.assertEqual("running", daily["status"])
        self.assertEqual("running", intraday["status"])
        self.assertIsNone(PJL.read_job(self.conn, slot="open", market_date="2026-10-28"))
        self.assertTrue(self.conn.in_transaction, "owner 不得提交调用方事务")

    def test_reclaim_is_a_cas_that_only_one_caller_wins(self):
        PJL.claim_job(self.conn, slot="intraday", market_date="2026-10-28",
                  run_key="k", owner="o1", expires_at="later", fencing_token=1, now="T0")
        PJL.mark_interrupted(self.conn, slot="intraday", market_date="2026-10-28",
                             run_key="k", detail='{"retry_count": 1}', now="T1")
        first = PJL.reclaim_job(self.conn, slot="intraday", market_date="2026-10-28",
                            run_key="k", owner="o2", expires_at="later",
                            fencing_token=2, detail='{"retry_count": 2}', now="T2")
        second = PJL.reclaim_job(self.conn, slot="intraday", market_date="2026-10-28",
                             run_key="k", owner="o3", expires_at="later",
                             fencing_token=3, detail="{}", now="T3")
        self.assertEqual(1, first.rowcount)
        self.assertEqual(0, second.rowcount, "已回到 running 的记录不能再被抢")
        row = self.row("paper_job_runs", run_key="k")
        self.assertEqual("o2", row["owner_key"])
        self.assertEqual(2, row["fencing_token"])

    def test_renew_complete_and_fail_require_the_current_fence(self):
        PJL.claim_job(self.conn, slot="close", market_date="2026-10-28", owner="o1",
                  expires_at="later", fencing_token=7, now="T0")
        stale = PJL.renew_lease(self.conn, slot="close", market_date="2026-10-28",
                          owner="o1", fencing_token=6, heartbeat_at="T1",
                          expires_at="later2")
        self.assertEqual(0, stale.rowcount, "旧 fencing 不得续期")
        fresh = PJL.renew_lease(self.conn, slot="close", market_date="2026-10-28",
                          owner="o1", fencing_token=7, heartbeat_at="T1",
                          expires_at="later2")
        self.assertEqual(1, fresh.rowcount)
        wrong_owner = PJL.complete_job(self.conn, slot="close", market_date="2026-10-28",
                                   owner="intruder", fencing_token=7,
                                   status="completed", detail="{}", now="T2")
        self.assertEqual(0, wrong_owner.rowcount)
        done = PJL.complete_job(self.conn, slot="close", market_date="2026-10-28",
                            owner="o1", fencing_token=7, status="completed",
                            detail='{"ok": true}', now="T2")
        self.assertEqual(1, done.rowcount)
        self.assertEqual("completed", self.row("paper_jobs", slot="close")["status"])

        PJL.claim_job(self.conn, slot="risk", market_date="2026-10-28", owner="o9",
                  expires_at="later", fencing_token=1, now="T0")
        PJL.fail_job(self.conn, slot="risk", market_date="2026-10-28", owner="o9",
                 fencing_token=1, detail='{"error": "boom"}', now="T3")
        self.assertEqual("failed", self.row("paper_jobs", slot="risk")["status"])

    def test_recovery_merges_detail_and_spares_a_live_lease(self):
        self.conn.executescript(
            """
            INSERT INTO paper_job_runs(run_key,slot,market_date,status,detail,started_at,expires_at,fencing_token)
            VALUES('dead','intraday','2026-10-28','running','{"retry_count": 2, "factor_refresh": "ok"}',
                   '2026-10-28 09:00:00','2000-01-01 00:00:00',1);
            INSERT INTO paper_job_runs(run_key,slot,market_date,status,detail,started_at,expires_at,fencing_token)
            VALUES('live','intraday','2026-10-28','running','{}','2026-10-28 09:00:00',
                   '2999-01-01 00:00:00',1);
            INSERT INTO paper_jobs(slot,market_date,status,detail,started_at,expires_at,fencing_token)
            VALUES('close','2026-10-27','running','{}','2026-10-27 15:00:00','2000-01-01 00:00:00',1);
            """
        )
        counts = PJL.recover_abandoned(
            self.conn, now="2026-10-28 10:00:00",
            intraday_cutoff="2026-10-28 09:45:00", job_cutoff="2026-10-28 09:15:00",
        )
        self.assertEqual({"job_runs": 1, "jobs": 1}, counts)
        dead = self.row("paper_job_runs", run_key="dead")
        self.assertEqual("failed", dead["status"])
        # json_patch 会重排 JSON 的空白，所以按"键值都在"来断言，不按原文空白。
        self.assertIn('"retry_count":2', dead["detail"], "回收不得抹掉既有 detail")
        self.assertIn('"factor_refresh":"ok"', dead["detail"])
        self.assertIn("interrupted", dead["detail"])
        self.assertIn("elapsed_seconds", dead["detail"])
        self.assertEqual("running", self.row("paper_job_runs", run_key="live")["status"])

    def test_prune_only_drops_rows_past_the_window(self):
        self.conn.executescript(
            """
            INSERT INTO paper_job_runs(run_key,slot,market_date,status) VALUES('old','x','2020-01-01','completed');
            INSERT INTO paper_job_runs(run_key,slot,market_date,status) VALUES('new','x',date('now'),'completed');
            INSERT INTO paper_jobs(slot,market_date,status) VALUES('close','2020-01-01','completed');
            INSERT INTO paper_jobs(slot,market_date,status) VALUES('close',date('now'),'completed');
            """
        )
        counts = PJL.prune_jobs(self.conn)
        self.assertEqual({"job_runs_purged": 1, "jobs_purged": 1}, counts)
        self.assertEqual(["new"], [row[0] for row in self.rows("paper_job_runs")])
        self.assertEqual(1, len(self.rows("paper_jobs")))


if __name__ == "__main__":
    unittest.main()
