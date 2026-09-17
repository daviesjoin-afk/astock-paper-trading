# -*- coding: utf-8 -*-
"""回复并 resolve PR #158 第二轮 review 的 3 条 thread。幂等。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_reply_threads2.py <exact-head-sha>
"""

from __future__ import annotations

import json
import subprocess
import sys

QUERY = """
{ repository(owner:"daviesjoin-afk", name:"astock-paper-trading") {
    pullRequest(number:158) {
      reviewThreads(last:40) { nodes { id isResolved path line
        comments(first:1){nodes{body}} } } } } }
"""

REPLIES = (
    (
        "backend/tradability_ingestion.py",
        "Include unprovable audit outcomes in the replay fingerprint",
        "Confirmed and fixed. Reproduced: a provider returning identical evidence and "
        "timestamps but switching `observed_kind` from a provable kind to `unprovable` "
        "leaves the normalized evidence and the outcome map untouched, while "
        "`unprovable_records` and `detail_json.unprovable` change in the audit row — so "
        "the same-`run_id` retry was accepted and `INSERT OR IGNORE` kept the stale row.\n\n"
        "The fingerprint now also covers the unprovable pair list and the conflict details "
        "(`field`/`providers`/`values`). The rule I applied: anything that reaches the "
        "audit row is content identity, so covering only part of it is not enough.\n\n"
        "Regression: `ReplayIdentityCoversUnprovableOutcomes` (asserts the retry is "
        "rejected and the audit row is byte-identical before/after) and "
        "`ReplayIdentityCoversConflicts`. Mutations `M-R7` / `M-R8` blank those fields and "
        "are CAUGHT.\n\n"
        "One test-quality note: `M-R8` initially **survived**, because provider-set changes "
        "also alter normalized evidence, so the ingest-level test passed for the wrong "
        "reason. I added `FingerprintCoversEveryAuditVisibleDifference`, which drives "
        "`_run_fingerprint` directly and asserts every audit-visible input moves the "
        "fingerprint — including that two *different* conflict value sets hash differently."
    ),
    (
        "work/tradability_shadow_validation.py",
        "Stop deriving ingested_later from the current archive",
        "Confirmed and fixed — you are right that relocating the probe did not make it "
        "point-in-time stable. It was still current state: false before a late record was "
        "inserted, true afterwards, flipping the same historical comparison from "
        "`archive_missing` to `archive_unprovable` and changing its fingerprint.\n\n"
        "The CLI now declares nothing (`ingested_later=False`), which is the conclusion "
        "actually available at `decision_at`: no visible evidence → `archive_missing`. The "
        "`_pair_has_records` probe is retained only as a read-only placeholder and is no "
        "longer consumed.\n\n"
        "Declaring `archive_unprovable` honestly requires a real ingestion ledger that "
        "records when a pair was *first observed* — a fact current state cannot reconstruct. "
        "That is recorded as follow-up rather than approximated. Both statuses remain "
        "not_comparable, so neither enters the agreement/disagreement denominator; the CLI "
        "smoke now asserts `archive_unprovable == 0` with `archive_missing == 4` for the "
        "two never-visible pairs."
    ),
    (
        "backend/tradability_ingestion.py",
        "Bind replay auditing to the archive connection",
        "Confirmed and fixed. With an `audit_conn` different from `repository.connection`, "
        "the replay check could read an empty runs table while the archive database already "
        "held that `run_id`, admitting a divergent replay into the archive and recording its "
        "identity in a different database — and the fact write and audit insert could not be "
        "committed atomically across two connections, so \"the whole transaction rolls back\" "
        "was not actually true.\n\n"
        "`IngestionService` now rejects a mismatched connection at construction, before any "
        "work happens. Production already passes the same connection "
        "(`run_backfill`), so this only forbids the inconsistent configuration.\n\n"
        "Regression: `AuditConnectionMustMatchArchiveConnection` — "
        "`test_audit_conn_on_a_different_connection_is_rejected` and "
        "`test_same_connection_is_accepted` as the non-vacuity pair. Mutation `M-R9` is CAUGHT."
    ),
)


def gh(*args) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit(proc.returncode)
    return proc.stdout


def main() -> int:
    head = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = gh("api", "graphql", "-f", f"query={QUERY}")
    nodes = json.loads(raw)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    for path, keyword, body in REPLIES:
        match = None
        for node in nodes:
            first = (node["comments"]["nodes"][0]["body"] or "")
            if node["path"] == path and keyword in first:
                match = node
                break
        if match is None:
            print(f"[SKIP] no thread matched {path} :: {keyword}")
            continue
        if match["isResolved"]:
            print(f"[DONE] already resolved: {path} :: {keyword}")
            continue
        text = body + (f"\n\nFixed on head `{head}`." if head else "")
        gh(
            "api", "graphql",
            "-f", "query=mutation($t:ID!,$b:String!){"
                  "addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$t, body:$b})"
                  "{comment{id}}}",
            "-f", f"t={match['id']}", "-f", f"b={text}",
        )
        gh(
            "api", "graphql",
            "-f", "query=mutation($t:ID!){resolveReviewThread(input:{threadId:$t})"
                  "{thread{isResolved}}}",
            "-f", f"t={match['id']}",
        )
        print(f"[REPLIED+RESOLVED] {path} :: {keyword}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
