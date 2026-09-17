# -*- coding: utf-8 -*-
"""issue #161 production-copy probe（§十九）。

**绝不修改真实 production DB**：本脚本只操作 DB 的**副本**。真实库里
``historical_tradability_archive`` 当前为空（0 行），因此无法在真实数据上复现原来的
24 行场景——脚本会如实报告这一点，并用**真实库 schema + 真实摄取写路径**构造等价
场景来证明：

1. 升级到 017 后，新摄取产生的 archive 行**全部**带行级 provenance；
2. 同一批行在历史知识时点不再被误标 ``legacy``（修掉假阳性）；
3. 人工构造的 pre-ledger 行仍然**如实**触发 legacy 诊断（没有修成"永不报 legacy"）。

用法::

    python work/probe_issue161_production_copy.py <db_copy_path>
"""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "backend")

import db_migrate
import tradability_archive as TA
import tradability_ingestion as TI
import tradability_observation_ledger as OL

SESSION = "2024-01-10"
DECISION = "2024-01-10T16:00:00+08:00"
CODE = "000001"


class ProbeProvider(TI.TradabilityFactProvider):
    provider_id = "listing"
    provider_version = "1"

    def fetch(self, code, session):
        return TI.ProviderResult(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            status=TI.OUTCOME_EVIDENCE,
            evidence={"is_listed": True, "listing_date": "2010-01-01"},
            observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
            observed_at="2024-01-09T00:00:00+08:00",
            effective_at="2024-01-09T00:00:00+08:00",
        )


def counts(conn):
    def one(sql):
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return "n/a"

    return {
        "archive": one(f"SELECT COUNT(*) FROM {TA.ARCHIVE_TABLE}"),
        "ledger": one(f"SELECT COUNT(*) FROM {OL.LEDGER_TABLE}"),
        "links": one(f"SELECT COUNT(*) FROM {OL.ARCHIVE_LINK_TABLE}"),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: probe_issue161_production_copy.py <db_copy_path>")
        return 2
    source = Path(argv[0])
    if not source.is_file():
        print(f"FAIL: 找不到 DB 副本 {source}")
        return 1

    # 再复制一次到临时目录，确保**连副本都不直接改**。
    workdir = Path(tempfile.mkdtemp(prefix="probe161_"))
    copy_path = workdir / "paper_trading.copy.sqlite3"
    shutil.copy2(source, copy_path)
    print(f"source   : {source}")
    print(f"probe copy: {copy_path}")

    conn = sqlite3.connect(str(copy_path))
    conn.row_factory = sqlite3.Row
    try:
        before = counts(conn)
        print(f"before   : {before}")

        # ── 施加 v17（以及任何 pending 迁移），走正式迁移入口 ──
        result = db_migrate.migrate("paper_trading", path=str(copy_path), backup=False)
        print(f"migration: {result}")
        after_migrate = counts(conn)
        print(f"after mig: {after_migrate}")

        # ── 用真实摄取写路径写入一行新事实 ──
        repo = TA.TradabilityArchiveRepository(conn)
        service = TI.IngestionService(
            [ProbeProvider()], repo, audit_conn=conn,
            cutoff="2024-01-10T16:00:00+08:00",
        )
        service.ingest([CODE], [SESSION], write=True, run_id="probe-run-1")
        conn.commit()
        after_ingest = counts(conn)
        print(f"after ing: {after_ingest}")

        ledger = OL.ObservationLedgerRepository(conn)
        rows = [
            {
                "code": row["code"], "session_date": row["session_date"],
                "effective_at": row["effective_at"], "observed_at": row["observed_at"],
            }
            for row in conn.execute(
                f"SELECT * FROM {TA.ARCHIVE_TABLE} WHERE code=? AND session_date=?",
                (CODE, SESSION),
            ).fetchall()
        ]

        failures = []
        # 1) 新摄取的行必须有链接。
        links = ledger.archive_links(CODE, SESSION)
        print(f"links for new row: {len(links)}")
        if not links:
            failures.append("新摄取的 archive 行没有行级 provenance 链接")

        # 2) 历史知识时点不再误标 legacy（原 #161 假阳性）。
        knowledge = ledger.knowledge_at(
            CODE, SESSION,
            validation_as_of="2024-01-10T00:00:00+08:00",
            decision_at=DECISION,
            archive_rows=rows,
        )
        print(f"post-ledger row @historical as_of: legacy={knowledge.legacy_observation_unknown}"
              f" covered={knowledge.archive_rows_with_observation_provenance}"
              f"/{knowledge.archive_row_count}")
        if knowledge.legacy_observation_unknown:
            failures.append("ledger-era 行仍被误标 legacy（#161 未修好）")

        # 3) 人工构造 pre-ledger 行，legacy 仍须真正触发。
        conn.execute(
            f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, observed_at,"
            " is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
            ("999999", SESSION, "2024-01-10T14:00:00+08:00",
             "2024-01-10T14:00:00+08:00", 1, "listing", "2026-01-01T00:00:00+08:00"),
        )
        conn.commit()
        legacy_rows = [
            {
                "code": row["code"], "session_date": row["session_date"],
                "effective_at": row["effective_at"], "observed_at": row["observed_at"],
            }
            for row in conn.execute(
                f"SELECT * FROM {TA.ARCHIVE_TABLE} WHERE code=? AND session_date=?",
                ("999999", SESSION),
            ).fetchall()
        ]
        legacy_knowledge = ledger.knowledge_at(
            "999999", SESSION, decision_at=DECISION, archive_rows=legacy_rows
        )
        print(f"true pre-ledger row: legacy={legacy_knowledge.legacy_observation_unknown}"
              f" first_seen={legacy_knowledge.first_seen_at}")
        if not legacy_knowledge.legacy_observation_unknown:
            failures.append("真正 pre-ledger 行不再触发 legacy（把假阳性修成了永不报）")
        if legacy_knowledge.first_seen_at is not None:
            failures.append("pre-ledger 行被伪造了 first_seen_at")

        print()
        if failures:
            for item in failures:
                print("FAIL:", item)
            return 1
        print("production-copy probe: PASS")
        return 0
    finally:
        conn.close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
