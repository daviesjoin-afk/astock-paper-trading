# -*- coding: utf-8 -*-
"""回复并 resolve PR #158 的 6 条 review thread。

每条回复先说明"复现 → 修法 → 回归证据"，再 resolve。脚本幂等（已 resolve 的跳过）。

用法::

    PY=<仓库 venv 的 python>
    $PY work/pr160_reply_threads.py <exact-head-sha>
"""

from __future__ import annotations

import json
import subprocess
import sys

REPO = "daviesjoin-afk/astock-paper-trading"
PR = 158

QUERY = """
{ repository(owner:"daviesjoin-afk", name:"astock-paper-trading") {
    pullRequest(number:158) {
      reviewThreads(last:30) { nodes { id isResolved path line
        comments(first:1){nodes{body}} } } } } }
"""

# 按 (path, 首条评论里的标题关键词) 匹配，避免依赖节点顺序。
REPLIES = (
    (
        "backend/tradability_ingestion.py",
        "Require durable audit storage before accepting replay IDs",
        "Confirmed and fixed. `IngestionService`'s documented default is `audit_conn=None`, "
        "and in that configuration `_persist_run` returns immediately — so this run's "
        "fingerprint was never recorded anywhere, and the next same-`run_id` write read "
        "\"no existing row\" and passed. That is exactly the accident the replay contract "
        "exists to prevent.\n\n"
        "`write=True` now requires a durable audit store: no `audit_conn` → `IngestionError` "
        "before anything is persisted. An absent/unreadable `tradability_ingestion_runs` "
        "table is also fail-closed rather than treated as \"no conflict\". `write=False` "
        "(dry-run) is unaffected — it persists nothing, so it needs no audit store.\n\n"
        "Regression: `test_tradability_backfill.ReplayIdentityNeedsDurableAudit` — "
        "`test_write_without_audit_conn_is_rejected` (asserts the DB snapshot is unchanged "
        "across the rejection), `test_divergent_replay_is_impossible_without_durable_audit`, "
        "`test_dry_run_without_audit_conn_is_still_allowed`. Mutation `M-R5` injects the old "
        "behaviour and is CAUGHT."
    ),
    (
        "backend/tradability_ingestion.py",
        "Include provider outcomes in replay identity",
        "Confirmed and fixed. Reproduced: a provider that returns `error` and later returns "
        "`unknown`, with identical scope/cutoff/provider versions, produces zero normalized "
        "evidence both times, so the old fingerprint was byte-identical. The second run was "
        "therefore accepted as an idempotent replay, `INSERT OR IGNORE` kept the first audit "
        "row, and the run could report `completed` while its audit still said "
        "`completed_with_gaps`.\n\n"
        "The fingerprint now covers the provider outcome distribution "
        "(`evidence`/`unknown`/`error`/`skipped`/`status`). The rule applied: anything that "
        "reaches the audit row is content identity, not just normalized evidence.\n\n"
        "Regression: `ReplayIdentityCoversProviderOutcomes` — "
        "`test_error_then_unknown_is_not_an_idempotent_replay` (asserts the second write is "
        "rejected and the audit row is byte-identical before/after) and "
        "`test_outcome_distribution_is_part_of_the_fingerprint`. Mutation `M-R6` sets "
        "`outcomes` to `{}` and is CAUGHT."
    ),
    (
        "work/tradability_shadow_validation.py",
        "Avoid fabricating a same-session entry for sell verdicts",
        "Confirmed and fixed. The CLI has no real holding-entry data, and passing "
        "`entry_session=session` told the production path the position was bought that same "
        "day, so ordinary T+1 securities came back `t1_not_sellable` — which the comparator "
        "then honestly reported as `production_block_archive_allow`. Those were false "
        "disagreements manufactured by the harness, not observed ones.\n\n"
        "The CLI compares **market-level** tradability; T+1 is a position-level concern it "
        "cannot speak to. It now omits `entry_session` (no T+1 evaluation) instead of "
        "inventing one. The CLI smoke output confirms it: every sell row now has "
        "`p_reason=ok`, and the smoke asserts `t1_not_sellable` never appears on the sell "
        "side.\n\n"
        "Mutation `M-SH11` re-injects the fabricated `entry_session=session` and is CAUGHT "
        "by the CLI contract guard."
    ),
    (
        "backend/tradability_shadow.py",
        "Keep future ingestions out of historical classification",
        "Confirmed and fixed. You are right that this was a future fact leaking into a "
        "historical conclusion: asking \"was this pair ingested *later*?\" meant that "
        "inserting a row whose `observed_at` postdates a past `decision_at` flipped that "
        "comparison from `archive_missing` to `archive_unprovable` and changed its "
        "fingerprint, conflicting with anything already persisted.\n\n"
        "Classification now depends only on evidence visible at `decision_at`. The "
        "missing-vs-unprovable distinction is an **explicit caller declaration** "
        "(`ingested_later=`), not something the comparator discovers by looking into the "
        "future. With no visible evidence the default is `archive_missing` — \"no usable "
        "evidence existed at decision time\" is a conclusion available at decision time.\n\n"
        "The far-future probe (`ARCHIVE_FAR_FUTURE` / `_pair_was_ingested`) is gone "
        "entirely. The CLI supplies the declaration from a plain existence check on the "
        "pair, which is caller-supplied input rather than a comparator-side future lookup.\n\n"
        "Regression: `PointInTimeGolden.test_future_ingestion_cannot_change_an_earlier_"
        "comparison` asserts both status and fingerprint are stable across a later "
        "ingestion; `test_ingested_later_declaration_is_the_only_unprovable_path` pins the "
        "only route to `archive_unprovable`. Both remain not_comparable, so neither enters "
        "the disagreement denominator."
    ),
    (
        "backend/tradability_shadow.py",
        "Reject verdicts whose side differs from the comparison side",
        "Confirmed and fixed. The old condition only checked that both sides belonged to "
        "`ST.SIDES`, not that they matched, so a valid `sell` verdict passed with "
        "`side=\"buy\"` was accepted and its status was reused as the production result "
        "while the archive's buy result was evaluated — a mislabeled agreement/disagreement.\n\n"
        "`production[\"side\"] != side` is now `comparison_invalid`.\n\n"
        "Regression: `test_production_verdict_side_must_match_the_comparison_side` (both "
        "directions) plus `test_matching_side_is_not_rejected` as the non-vacuity pair. "
        "Mutation `M-SH9` — which survived on the first attempt because the original "
        "injection was inert — now injects `or False` and is CAUGHT."
    ),
    (
        "backend/test_tradability_shadow_architecture_guard.py",
        "Restrict the missing-CLI skip to file-reading tests",
        "Confirmed and fixed. A class-level `setUp` skip took all five tests with it, "
        "including the two detectors that never read the CLI file — so inside the runtime "
        "image the guard's own non-vacuity checks disappeared, which is the opposite of the "
        "stated intent.\n\n"
        "The skip now lives in a `_cli_source()` helper called only by the tests that "
        "actually read `work/tradability_shadow_validation.py`. The detector tests "
        "(forbidden flag, write statement, read-only statement, fabricated entry_session) "
        "run in every environment.\n\n"
        "Verified by simulating the image layout (backend/ copied without work/): the file "
        "reports `OK (skipped=4)` with 73 tests across the two shadow modules, and the "
        "non-vacuity detectors genuinely execute."
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
