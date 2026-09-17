# -*- coding: utf-8 -*-
"""§三十五 的 7 项 adversarial 自查（Codex review 因额度不可用时的替代验证）。

每项都写成**可执行断言**，而不是口头结论。
"""

import sqlite3
import sys

sys.path.insert(0, "backend")

import tradability_archive as TA
import tradability_ingestion as TI
import tradability_observation_ledger as OL
import tradability_shadow as TS

CODE, SESSION = "000001", "2024-01-10"
DECISION = "2024-01-10T16:00:00+08:00"
RUN = "run-0001"
FAIL = []


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    TA.ensure_schema(conn)
    TI.ensure_ingestion_schema(conn)
    return conn


def row(eff="2024-01-10T15:05:00+08:00", obs="2024-01-10T15:05:00+08:00"):
    return {"code": CODE, "session_date": SESSION,
            "effective_at": eff, "observed_at": obs}


def link(repo, r, fp="fp-1", provider="listing", rec="2024-01-10T16:00:00+08:00"):
    repo.link_archive_row(r, ingestion_run_id=RUN, provider_id=provider,
                          observation_fingerprint=fp, recorded_at=rec)


# 1. repeated observation 是否导致 over-count
conn = db(); repo = OL.ObservationLedgerRepository(conn)
r = row()
for i in range(7):
    link(repo, r, fp=f"fp-{i}", provider=f"p{i}")
k = repo.knowledge_at(CODE, SESSION, decision_at=DECISION, archive_rows=[r])
ok = (k.archive_rows_with_observation_provenance == 1
      and k.archive_rows_with_observation_provenance <= k.archive_row_count)
print(f"1 over-count: covered={k.archive_rows_with_observation_provenance}"
      f" rows={k.archive_row_count} -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("1")
conn.close()

# 2. later re-observation 是否洗白 legacy
conn = db(); repo = OL.ObservationLedgerRepository(conn)
legacy = row(eff="2024-01-10T14:00:00+08:00", obs="2024-01-10T14:00:00+08:00")
repo.append(OL.event_from_provider_result(
    TI.ProviderResult(provider_id="listing", provider_version="1",
                      status=TI.OUTCOME_EVIDENCE, evidence={"listing_date": "2010-01-01"},
                      observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                      observed_at="2024-01-09T00:00:00+08:00",
                      effective_at="2024-01-09T00:00:00+08:00"),
    code=CODE, session=SESSION, recorded_at="2026-01-01T00:00:00+08:00",
    ingestion_run_id="later-run"))
k = repo.knowledge_at(CODE, SESSION, decision_at=DECISION, archive_rows=[legacy])
# 旧行必须保持 legacy；而 first_seen 只能来自**真正的 ledger event**（这里是那次
# later run 的 recorded_at），绝不能由 archive.created_at 伪造。
ok = (k.legacy_observation_unknown
      and k.first_seen_at == "2026-01-01T00:00:00+08:00")
print(f"2 whitewash: legacy={k.legacy_observation_unknown} first_seen={k.first_seen_at}"
      f" -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("2")
conn.close()

# 3. mixed pair 是否仍能找到真正 legacy
conn = db(); repo = OL.ObservationLedgerRepository(conn)
covered = row()
link(repo, covered)
legacy = row(eff="2024-01-10T14:00:00+08:00", obs="2024-01-10T14:00:00+08:00")
k = repo.knowledge_at(CODE, SESSION, decision_at=DECISION, archive_rows=[covered, legacy])
ok = (k.archive_row_count == 2 and k.archive_rows_with_observation_provenance == 1
      and k.archive_rows_without_observation_provenance == 1
      and k.legacy_observation_unknown)
print(f"3 mixed: rows={k.archive_row_count} covered={k.archive_rows_with_observation_provenance}"
      f" uncovered={k.archive_rows_without_observation_provenance}"
      f" legacy={k.legacy_observation_unknown} -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("3")
conn.close()

# 4. validation_as_of 是否泄漏未来 observation
conn = db(); repo = OL.ObservationLedgerRepository(conn)
repo.append(OL.event_from_provider_result(
    TI.ProviderResult(provider_id="listing", provider_version="1",
                      status=TI.OUTCOME_EVIDENCE, evidence={"listing_date": "2010-01-01"},
                      observed_kind=TI.OBSERVED_SNAPSHOT_TIMESTAMP,
                      observed_at="2024-01-09T00:00:00+08:00",
                      effective_at="2024-01-09T00:00:00+08:00"),
    code=CODE, session=SESSION, recorded_at="2024-06-01T00:00:00+08:00",
    ingestion_run_id=RUN))
early = repo.knowledge_at(CODE, SESSION, validation_as_of="2024-01-12T00:00:00+08:00",
                          decision_at=DECISION)
late = repo.knowledge_at(CODE, SESSION, validation_as_of="2024-07-01T00:00:00+08:00",
                         decision_at=DECISION)
ok = (early.never_observed and early.first_seen_at is None
      and not early.evidence_seen and late.evidence_seen)
print(f"4 future leak: early never={early.never_observed} evidence={early.evidence_seen};"
      f" late evidence={late.evidence_seen} -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("4")
conn.close()

# 5. structural provenance 与 PIT knowledge 是否混淆
conn = db(); repo = OL.ObservationLedgerRepository(conn)
r = row()
link(repo, r, rec="2026-01-01T00:00:00+08:00")
k = repo.knowledge_at(CODE, SESSION, validation_as_of="2024-01-12T00:00:00+08:00",
                      decision_at=DECISION, archive_rows=[r])
ok = (k.never_observed and not k.legacy_observation_unknown
      and k.archive_rows_with_observation_provenance == 1)
print(f"5 confusion: never={k.never_observed} legacy={k.legacy_observation_unknown}"
      f" covered={k.archive_rows_with_observation_provenance} -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("5")
conn.close()

# 6. 是否意外改变 top-level comparison status
conn = db(); repo = TA.TradabilityArchiveRepository(conn)
ledger = OL.ObservationLedgerRepository(conn)
conn.execute(
    f"INSERT INTO {TA.ARCHIVE_TABLE}(code, session_date, effective_at, observed_at,"
    " is_listed, source, created_at) VALUES(?,?,?,?,?,?,?)",
    (CODE, SESSION, "2024-01-09T00:00:00+08:00", "2026-01-01T00:00:00+08:00",
     1, "listing", "2026-01-01T00:00:00+08:00"))
link(ledger, {"code": CODE, "session_date": SESSION,
              "effective_at": "2024-01-09T00:00:00+08:00",
              "observed_at": "2026-01-01T00:00:00+08:00"},
     rec="2026-01-01T00:00:00+08:00")
comparator = TS.ShadowComparator(repo, ledger=ledger)
verdict = TS.ST.__dict__.get("STATUS_EXECUTABLE")
cmp_ = comparator.compare(
    {"status": verdict, "reason": "", "side": "buy"},
    code=CODE, session=SESSION, side="buy", decision_at=DECISION,
    validation_as_of="2024-01-12T00:00:00+08:00")
ok = (not cmp_.comparable and cmp_.status == TS.ShadowStatus.ARCHIVE_MISSING.value
      and cmp_.archive_diagnostic != TS.ShadowStatus.ARCHIVE_LEGACY_OBSERVATION_UNKNOWN.value)
print(f"6 top-level: status={cmp_.status} comparable={cmp_.comparable}"
      f" diag={cmp_.archive_diagnostic} -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("6")
conn.close()

# 7. execution authority leak：**台账面**不得出现 can_*（与架构 guard 的
#    FORBIDDEN_AUTHORITY_TOKENS 同范围）。archive 的 TradabilityDecision.can_buy /
#    can_sell 是事实层既有的 verdict API（master 上就有），不在本 PR 管辖内。
import ast
import pathlib
leaks = []
tree = ast.parse(pathlib.Path("backend", "tradability_observation_ledger.py")
                 .read_text(encoding="utf-8"))
for node in ast.walk(tree):
    if isinstance(node, ast.Attribute) and node.attr.startswith("can_"):
        leaks.append(f"ledger:{node.attr}")
    if isinstance(node, ast.Name) and node.id.startswith("can_"):
        leaks.append(f"ledger:{node.id}")
ok = not leaks
print(f"7 authority leak (ledger surface): {leaks or 'none'} -> {'OK' if ok else 'FAIL'}")
if not ok: FAIL.append("7")

print()
if FAIL:
    print("adversarial self-review FAILED on:", ", ".join(FAIL))
    raise SystemExit(1)
print("adversarial self-review (7/7): PASS")
