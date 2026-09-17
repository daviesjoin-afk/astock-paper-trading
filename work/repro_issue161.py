# -*- coding: utf-8 -*-
"""issue #161 三 Case 复现：pair-level bool 的假阳性 / 真 legacy / mixed。

用真实摄取写路径（IngestionService.ingest）而不是手工 INSERT：provenance 链接
必须在真实事务里产生，否则复现的不是生产路径。
"""

import sqlite3
import sys

sys.path.insert(0, "backend")

import tradability_archive as TA
import tradability_ingestion as TI
import tradability_observation_ledger as OL

CODE = "000001"
SESSION = "2024-01-10"
DECISION = "2024-01-10T16:00:00+08:00"
RUN = "run-0001"


def open_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    TI.ensure_ingestion_schema(conn)
    return conn


class StaticProvider(TI.TradabilityFactProvider):
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


def archive_rows(conn, code=CODE, session=SESSION):
    return [
        {
            "code": row["code"],
            "session_date": row["session_date"],
            "effective_at": row["effective_at"],
            "observed_at": row["observed_at"],
        }
        for row in conn.execute(
            f"SELECT * FROM {TA.ARCHIVE_TABLE} WHERE code=? AND session_date=?",
            (code, session),
        ).fetchall()
    ]


def ingest(conn, repo, run_id, codes=(CODE,)):
    service = TI.IngestionService(
        providers=[StaticProvider()],
        repository=repo,
        audit_conn=conn,
        cutoff="2024-01-10T16:00:00+08:00",
    )
    result = service.ingest(list(codes), [SESSION], write=True, run_id=run_id)
    conn.commit()
    return result


def main():
    failures = []

    # ── Case 1：ledger-era 行，但历史知识时点看不到事件 ──
    # 旧逻辑：rows 为空 + archive 有行 → 误报 legacy（生产 24 行的假诊断）。
    conn = open_db()
    repo = TA.TradabilityArchiveRepository(conn)
    ledger = OL.ObservationLedgerRepository(conn)
    ingest(conn, repo, RUN)
    rows = archive_rows(conn)
    print(f"Case1 archive rows={len(rows)} links={len(ledger.archive_links(CODE, SESSION))}")
    # 站在决策时点之前的知识时点：recorded_at 晚于 as_of，因此事件不可见。
    knowledge = ledger.knowledge_at(
        CODE, SESSION,
        validation_as_of="2024-01-10T00:00:00+08:00",
        decision_at=DECISION,
        archive_rows=rows,
    )
    print(f"  never_observed={knowledge.never_observed} legacy={knowledge.legacy_observation_unknown}"
          f" covered={knowledge.archive_rows_with_observation_provenance}/{knowledge.archive_row_count}")
    if knowledge.legacy_observation_unknown:
        failures.append("Case1 被误标 legacy（issue #161 假阳性复现失败）")
    if not knowledge.never_observed:
        failures.append("Case1 应报告 never_observed（该知识时点看不到事件）")
    conn.close()

    # ── Case 2：真正 pre-ledger 行（无任何链接） ──
    conn = open_db()
    repo = TA.TradabilityArchiveRepository(conn)
    ledger = OL.ObservationLedgerRepository(conn)
    # 模拟升级前写入的历史行：直接 INSERT，不产生链接。
    conn.execute(
        f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, observed_at,"
        " is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
        (CODE, SESSION, "2024-01-10T15:05:00+08:00", "2024-01-10T15:05:00+08:00",
         1, "listing", "2026-01-01T00:00:00+08:00"),
    )
    rows = archive_rows(conn)
    knowledge = ledger.knowledge_at(
        CODE, SESSION, decision_at=DECISION, archive_rows=rows
    )
    print(f"Case2 rows={len(rows)} legacy={knowledge.legacy_observation_unknown}"
          f" first_seen={knowledge.first_seen_at}"
          f" uncovered={knowledge.archive_rows_without_observation_provenance}")
    if not knowledge.legacy_observation_unknown:
        failures.append("Case2 真 legacy 行未被识别")
    if knowledge.first_seen_at is not None:
        failures.append("Case2 伪造了 first_seen_at")
    conn.close()

    # ── Case 3：mixed pair（1 legacy + 1 ledger-era） ──
    conn = open_db()
    repo = TA.TradabilityArchiveRepository(conn)
    ledger = OL.ObservationLedgerRepository(conn)
    # A：pre-ledger 行
    conn.execute(
        f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, observed_at,"
        " is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
        (CODE, SESSION, "2024-01-10T14:00:00+08:00", "2024-01-10T14:00:00+08:00",
         1, "listing", "2026-01-01T00:00:00+08:00"),
    )
    # B：ledger-era 行（真实摄取写入，带链接）
    ingest(conn, repo, RUN)
    rows = archive_rows(conn)
    knowledge = ledger.knowledge_at(
        CODE, SESSION, decision_at=DECISION, archive_rows=rows
    )
    print(f"Case3 rows={knowledge.archive_row_count}"
          f" covered={knowledge.archive_rows_with_observation_provenance}"
          f" uncovered={knowledge.archive_rows_without_observation_provenance}"
          f" legacy={knowledge.legacy_observation_unknown}")
    if knowledge.archive_row_count != 2:
        failures.append(f"Case3 archive_row_count 应为 2，实为 {knowledge.archive_row_count}")
    if knowledge.archive_rows_with_observation_provenance != 1:
        failures.append("Case3 covered 应为 1")
    if knowledge.archive_rows_without_observation_provenance != 1:
        failures.append("Case3 uncovered 应为 1")
    if not knowledge.legacy_observation_unknown:
        failures.append("Case3 mixed pair 的真正 legacy 行被掩盖（issue #161 第二个 bug）")
    conn.close()

    print()
    if failures:
        for item in failures:
            print("FAIL:", item)
        return 1
    print("issue #161 reproduction: all three cases behave correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
