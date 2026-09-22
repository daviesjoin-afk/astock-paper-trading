refactor(strategy): make selection provenance version-pinned

## 1. Base

- base branch: `master`
- **exact base SHA**: `c872ae18574ecba5e1b788112fcd3c818af475d9`

## 2. Before-fix reproduction

`work/r23_before_fix_repro.py` runs the same probes against the pre-fix tree
(detached worktree at the base SHA) and the fix tree, and **refuses to print a
conclusion** when the tree revision does not match the requested `--expect`.

| case | BEFORE | AFTER | what it measures |
|---|---|---|---|
| C1 current-head reinterpretation | REPRODUCED | NOT REPRODUCED | after v2 is published, a stored v1 selection must still resolve to v1 + v1 checksum |
| C2 same-day version overwrite | REPRODUCED | NOT REPRODUCED | `UNIQUE(trade_date, strategy_id)` deleted the v1 run when the same day produced a v2 run |
| C3 cycle pin vs current head | NOT REPRODUCED | NOT REPRODUCED | the account's cycle pin already won on the base tree — no bug to fix here; locked by SP-03 instead |
| C4 missing cycle pin | REPRODUCED | NOT REPRODUCED | with the pin removed, the writer silently adopted a legacy/current-head stamp instead of refusing |
| C5 wrong checksum | REPRODUCED | NOT REPRODUCED | a row carrying a checksum that does not match the immutable version was surfaced as if it agreed |
| C6 archived strategy history | REPRODUCED | NOT REPRODUCED | archiving a strategy hid its historical selections from the day's read |
| C7 as-of isolation | REPRODUCED | NOT REPRODUCED | a run on D+1 rewrote fields of the D selection |
| C8 research run scope | REPRODUCED | NOT REPRODUCED | a research run had no explicit scope, so "not in a cycle" was indistinguishable from "cycle unknown" |
| C9 missing historical version | REPRODUCED | NOT REPRODUCED | a `strategy_id`-only legacy row was current-filled on read |
| C10 order/signal consistency | REPRODUCED | NOT REPRODUCED | a signal had no durable cycle of its own, so its ownership was unknowable without an order |

C3 is reported as NOT REPRODUCED on the base tree deliberately: the cycle resolver
on `master` already preferred the cycle pin for the *account* stamp. The remaining
gap there was that the pin was never consulted for signal identity (C4/C10), which
is what this PR closes.

## 3. Authority model

There is exactly **one** authority per question, and no new one was created:

- **immutable strategy version** — `strategy_registry` (`paper_strategy_versions`
  + `paper_strategy_version_heads`). Untouched.
- **cycle → immutable version binding** — `strategy_registry.cycle_stamp_for_account`
  / `cycle_version_for_account` (`paper_cycle_strategy_versions`). Untouched; this
  PR makes the missing-pin case *reachable and fail-closed* instead of falling
  through to a legacy binding and finally to a user strategy's current head.
- **historical selection** — the **persisted row's own stamp**. A reader never
  re-resolves the registry to explain a row that already exists.
- **legacy rows** — honestly unprovable. `cycle_id IS NULL` / `strategy_version IS
  NULL` mean "not known", never "guess it".

Version resolution lives in one thin adapter,
`backend/strategy_selection_resolver.py`, which depends **downward only** (contract
+ registry) and is guarded against importing any caller (Guard 14d).

## 4. Provenance contract

`backend/strategy_selection_provenance.py` is a pure, frozen, dependency-free
contract:

- `StrategySelectionProvenance(strategy_id, strategy_version, strategy_checksum,
  asof_day, scope, cycle_id)` — validates and canonicalises on construction;
  `cycle` scope requires a cycle id, `research` scope forbids one;
- `ProvenanceReading` — a persisted read plus the reason it is (or is not)
  authoritative. Non-authoritative readings **cannot** carry a contract and
  `require()` raises rather than substituting a default;
- `run_provenance_key` — the run-identity string. Same evidence → same key
  (idempotent retry); different immutable version → different key, so a newer
  version can never delete older evidence;
- `resolve_asof_day` — explicit → a single distinct declared candidate → otherwise
  refuse. No `today()`, no `min`/`max`, no latest-factor-day fallback;
- `reading_from_row` — classifies a persisted row **without touching the
  Registry**, and refuses silent upgrades in both directions: a `verified` claim
  with an incomplete stamp is `unknown`, and a `legacy` claim backed by a complete
  stamp is `unknown`.

## 5. Historical behaviour

A stored selection, signal or order carries the exact version/checksum that
produced it, and later strategy edits cannot reinterpret it:

- `paper_selection_runs` keeps `strategy_version`, `strategy_checksum`, `asof_day`,
  `scope`, `cycle_id`, `provenance_status`, `provenance_key`;
- `paper_selection_picks` references its run by `run_id` (the `(trade_date,
  strategy_id, rank_no)` key is no longer the authoritative join);
- rename / pause / archive change *presentation* only: `strategy_name` on a
  historical row is the name at the time, exposed alongside
  `current_strategy_name` for display;
- `paper_signals` gains its own immutable `cycle_id`, so a signal that never
  became an order is still attributable.

## 6. Cycle vs research semantics

`scope` makes the two explicit and mutually exclusive:

- `SCOPE_CYCLE` requires an explicit `cycle_id`; resolution goes through the cycle
  pin only;
- `SCOPE_RESEARCH` requires `cycle_id IS NULL`. `NULL` means "this run does not
  belong to a cycle" — it never means "unknown, so adopt the active cycle". A DB
  trigger enforces this shape on both selection families.

Family A (`paper_selection_runs`/`paper_selection_picks`, the production research
ledger) and family B (`selection_runs`/`selection_picks`, the research tracking DB)
stay in their own databases and keep their existing schema owners; they now share
the same contract. Family B's `strategy` is a **model family** id that does not
exist in the registry, so its strategy axis is honestly
`provenance_status='not_applicable'` with `strategy_*` left NULL — no fabricated
mapping.

## 7. Legacy unknown behaviour

Upgrading rewrites no historical row. Migrations only `ADD COLUMN` / install
guards (v23 adds `paper_signals.cycle_id` and `paper_signals_archive.cycle_id`).
Signals written before the upgrade stay `cycle_id IS NULL`: their cycle cannot be
derived from any current state, because `paper_accounts.cycle_id` is a mutable
re-binding. Guards never re-scan existing rows, and the archive table
deliberately carries no INSERT guard so legacy rows remain archivable.

Legacy construction in tests uses the repo's existing idiom — lift the guard,
insert the historical shape, restore the guard — not a weakened guard.

## 8. Archive behaviour

`paper_signals_archive` gains `cycle_id` at the same position as the live table,
because retention still copies with `SELECT *`. Column order is asserted equal, not
just presence: a divergence silently shifts every archived column.

Provenance columns are immutable on both tables (`BEFORE UPDATE` triggers), so no
repair script can wash "unknown" into "known" — including `NULL → value`.

## 9. R19-R22 invariants

- **R19** BUY planning / commit convergence — untouched;
- **R20** `execution_planner.commit_fill` remains the only fill commit owner —
  untouched;
- **R21** `paper_risk_service` remains the risk application authority — untouched;
- **R22** `PortfolioReadContext(cycle_id, asof_day)` stays cycle/as-of bounded —
  untouched.

The full existing suite (3960 tests) passes, including the R19-R22 production-path
regressions. Guards 1-13 are unchanged and still green.

## 10. Maintainability impact

- duplicate version resolver: **0** — one adapter, contract separate;
- reverse import: **0** (Guard 14d);
- new current/latest historical fallback: **0** (Guard 14e/14g/14h);
- provenance authority count: **1**;
- production writer duplication: **0** — both signal writers resolve the account's
  provenance **once** outside the candidate loop and share it, so N picks never
  become N registry queries.

`paper_trading.py` LOC baseline moved 14847 → 14896 (+49) for wiring that must live
at the write sites (v23 migration call, the two `cycle_id` columns, and the
fail-closed guard in both signal writers). The resolver logic itself was extracted
instead of inlined, which is a net −43 lines in that region; the module-level
function count is unchanged at 280. Rationale is recorded next to the baseline, and
no unrelated subsystem was split just to hit the number.

## 11. Tests

- targeted: `test_strategy_selection_provenance` — **32 tests**, SP-01…SP-18 plus
  the contract units, purity checks and as-of isolation;
- architecture: `test_paper_trading_architecture_guard` — **105 tests**, incl. the
  new **Guard 14** (14a-14k);
- full backend: `python -m unittest discover -s backend -p "test_*.py"` —
  **3960 tests, OK (skipped=5)**;
- frontend: `npm run build` (committed `dist/` unchanged), `npm run test:unit`
  — **111/111 pass**, `node --check` on every `frontend/src/**/*.js`;
- e2e: runs in CI (`browser-e2e`); no frontend behaviour was changed by this PR.

Guard 14 replaces fragile substring scans with AST checks: attribute/call names
rather than raw text (the earlier `"sqlite3" in body` form was vacuously satisfied
by the word appearing in a docstring), f-strings excluded from the "no handwritten
status literal" rule (embedding a contract constant is the *opposite* of
handwriting a status), and docstrings excluded from all literal checks.

## 12. Mutation

`work/r23_mutation_check.py` — M-SP1…M-SP15, each bound to one permanent
regression, run with `--non-vacuity`:

- total: **15**
- caught: **15**
- survived: **0**
- fake: **0**
- baseline-red: **0**
- restore sha256: **PASS** (byte-identical restore verified per mutation)

A mutation that fails via `SyntaxError` / `ImportError` / `_FailedTest` counts as
FAKE, never as a kill. Each subprocess gets its own `PYTHONPYCACHEPREFIX`, and the
runner self-tests that the sequence is unique and increasing — a shared bytecode
cache between baseline and mutant would silently invalidate the whole matrix.

Seven mutations survived on the first run and every survival was real, not
cosmetic: four sat on unreachable or behaviourally-equivalent code, and three
exposed **missing assertions** (a fallback that only fires when the cycle pin is
absent, a lifecycle check only reachable through the resolver, and an archive guard
that was satisfied by presence rather than column *order*). The assertions were
added; no mutation was weakened to manufacture a kill.

## 13. Security

`scripts/security/scan-sensitive-data.py --repo . --scope all` → `values: 0`.
The only manual-review items are the pre-existing `docs/assets/dashboard.png`
image entries. No credentials, tokens, or connection strings are introduced.

## 14. Review round 1 (P1 ×2, P2 ×1) — fixed

Three findings arrived after the first CI green. Each was **reproduced against the
pre-fix tree before being fixed**; the probe (`work/r23_review_repro.py`) prints the
BEFORE values and can be pointed at the pre-fix source via `R23_SIGNAL_INSERT_SRC`.

| # | sev | before (measured) | after (measured) | file |
|---|---|---|---|---|
| F1 | P1 | child FK rewritten to `selection_runs_legacy`; pick insert → `OperationalError: no such table: main.selection_runs_legacy` | FK target back to `selection_runs`; picks survive and stay writable | `backend/selection_tracking.py` |
| F2 | P1 | interrupted rebuild → recovery throws `OperationalError: no such table: selection_runs`, history lost | leftover absorbed (promoted or folded back by `id`, stamped `legacy_unproven`), no row discarded | `backend/selection_tracking.py` |
| F3 | P2 | first post-upgrade refresh → `IntegrityError: signal cycle provenance is immutable` | refresh succeeds; existing row's provenance left untouched | `backend/paper_trading.py` |

### F1 — the rebuild must not rename the referenced parent

`ALTER TABLE selection_runs RENAME TO selection_runs_legacy` rewrites
`selection_picks.run_id`'s FK target **even with `foreign_keys` disabled**; dropping
the legacy table then left the child schema pointing at a nonexistent table. The
rebuild now uses the documented create → copy → drop → rename order, so the parent
name is valid at every point. Fresh and rebuilt tables share one `_runs_ddl()`
definition, which is what keeps the two paths from drifting again.

### F2 — absorb leftovers, never delete the only copy

The unconditional `DROP TABLE selection_runs_legacy` at the top of the migration
destroyed the sole remaining copy when a previous run died between the rename and the
copy. `_absorb_leftover_runs()` now promotes the leftover when the parent is gone, or
folds its rows back in by `id` (explicitly stamped `legacy_unproven`) when the parent
survived — and only then drops it. This implementation's own `selection_runs_new`
staging table is recovered the same way, so a crash *during recovery* is itself
recoverable.

### F3 — the class audit found the defect wider than reported

Reported as `cycle_id` only. A class audit (`work/r23_review_class_audit.py`) measured
that the **stamp trio** in the same `DO UPDATE SET` (`strategy_id` /
`strategy_version` / `strategy_checksum`) is rejected by the pre-existing
strategy-stamp immutability trigger in exactly the same way
(`strategy version stamp is immutable`), so removing `cycle_id` alone would have left
a second broken path. **All four immutable columns** are now out of the conflict
update.

The same audit checked the siblings and found them immune rather than missed:
`paper_selection_runs` / `paper_selection_picks` declare **no `REFERENCES` clause**, so
the F1/F2 shape does not exist there; and no other `ON CONFLICT ... DO UPDATE` in the
tree targets a guarded table (`rebalance_scans` uses `INSERT OR REPLACE` with
`cycle_id` already in the conflict target — existing intentional design).

### Regression coverage added

| test | covers |
|---|---|
| `RunTableRebuildTests.test_RF01` | child FK still points at `selection_runs`; picks not taken by CASCADE and still writable |
| `test_RF02` | interrupted rebuild keeps the history; recovered rows stamped `legacy_unproven` |
| `test_RF03` | rebuild is idempotent, no leftover temp tables (positive control) |
| `SignalRefreshTests.test_RF04` | first refresh after upgrade neither raises nor back-fills immutable columns |
| `test_RF05` | static assertion: `DO UPDATE SET` never names the four immutable columns |

Three fixture details were required to make these honest rather than vacuously green:
RF01-RF03 go through the real `ST.ensure_schema()` (the migration ordering — FK
toggling and guard installation — is itself the subject); the legacy run fixture
**carries `REFERENCES`** (without it the finding tests green forever); and RF04 runs
the production statement extracted from source with values in the statement's **true
column order** (a hand-written tuple silently took the insert-new-row path and never
touched the conflict branch).

### Verification for this round

| gate | result |
|---|---|
| full backend suite (post-fix) | **3973 tests, OK (skipped=5)** in 288s |
| `test_strategy_selection_provenance` | **44 tests OK** |
| targeted 4 modules (`test_strategy_selection_provenance`, `test_paper_trading_architecture_guard`, `test_paper_selection`, `test_selection_tradability`) | **288 tests OK** (44 + 105 + 15 + 124) |
| ruff / compileall | `All checks passed!` / clean |
| mutation matrix (18, non-vacuity, **serial**) | **18/18 CAUGHT; survived=0; fake=0; baseline-red=0; restore sha256 PASS** |
| leak scan `--scope worktree` and `--scope all` | `values: 0` |

Mutation note: the matrix **must** run serially. An earlier 5-way sharded run was
**invalid** — the shards rewrote the same production files concurrently and polluted
each other (tally 0-1/3). Sharding is only safe when shards touch disjoint files. The
matrix also restores byte-identically from a snapshot taken at launch, so production
files must not be edited while it runs.

## 15. Review round 2 — two provenance correctness blockers, plus a TOCTOU close

Review round 1's fixes stand (F1/F2/F3, commit `2344e70`), and its numbers above are
superseded by the rounds below: the matrix grew 18 → **21** and the backend suite
**3973 → 4006**. Only the blockers were touched — no R24, no market-data changes, no
strategy / risk threshold changes, no candidate scoring or execution-rule changes.

### 15.1 Signal cycle: mutable re-resolution + a validation→INSERT window

`strategy_selection_resolver.signal_cycle_provenance` re-read
`paper_accounts.cycle_id`, so candidates built under cycle A were stamped with cycle
B's strategy version when a rollover landed during the provider-I/O window. The cycle
is now **keyword-only and required**: the resolver performs no `paper_accounts` read
and never searches for a cycle. Both writers freeze a `SignalWriteContext` from the
cycle the candidates were built under and compare it against the cycle observed at
commit time; a mismatch drops the whole batch as `SignalStaleContext` with an
explicit `signal_stale_cycle_context` audit.

The final review blocker then closed the remaining window between that check and the
first `INSERT`:

- `generate_signals` commits inside **`BEGIN IMMEDIATE`**, so validation and the
  signal INSERT share one write boundary. All provider/network calls still finish
  before the lock is taken — the commit block contains local decisions and writes
  only, and Guard 14s asserts that.
- A **second layer** in the DB: `BEFORE INSERT ON paper_signals` now also requires
  `EXISTS (SELECT 1 FROM paper_accounts a WHERE a.id = NEW.account_id AND
  a.cycle_id = NEW.cycle_id)`. It constrains new rows only — no legacy backfill,
  `paper_signals_archive` still accepts legacy NULL, and rollover never rewrites an
  existing signal's cycle.

| test | BEFORE | AFTER |
|---|---|---|
| `RV01` close-signal rollover | cycle A candidates written as `cycle_id=B, version=2` | batch dropped as stale, explicit audit, nothing written to B |
| `RV03` bootstrap rollover | same shape | batch aborted |
| `RV08` rollover between validation and first INSERT | competitor rollover **commits** between the check and the INSERT (`committed=True, cycle_b=3`) | competitor is blocked by the write boundary; batch commits atomically under cycle A |
| `RV10` DB second layer | — | a signal for a cycle the account has left is rejected: `invalid signal cycle provenance` |
| `RV11` history | — | rollover leaves existing signal rows on their original cycle |
| `RV02`/`RV04`/`RV09` | — | positive controls: with no rollover, cycle A signals are written normally |

`RV08` was **vacuous on its first version** (it stayed green with a deferred
transaction) because an earlier optional-research audit write had already upgraded
the transaction to a write. The fixture now makes that ledger succeed, so the
test's first write is the signal INSERT — and it discriminates: `immediate` GREEN,
`deferred` RED with the competitor committing.

### 15.2 Research selection: post-hoc version attribution

`run_daily` resolved provenance after `_run_one`, so a strategy published
mid-computation was credited with a result it did not produce. The order is now
**pin → compute → as-of → combine → write**: the immutable version is pinned once
before `_run_one` and the as-of resolved afterwards. `RV07` publishes v2 *inside*
`_run_one`; the stored stamp must remain v1 (SP-01/SP-02 cannot see this, since their
version change happens after the read).

Scope is stated honestly rather than implied: for family A the selection semantics
come from `model_id` (`STRATEGY_MODEL` → `strategies.PAPER_WEIGHTS` /
`_paper_conditions`), i.e. from code — an edit to the immutable definition row does
not by itself change which stocks get picked. The pin therefore records the
**attributed published identity**, not a scored input. That distinction is written
into `ResearchVersionPin` and `_pin_research_version` so the DB never claims a
binding the execution path does not have.

### 15.3 Frozen stamp now spans the signal decision log

Both writers pass their batch-level frozen stamp to `_risk_log`, so candidate batch,
`paper_signals` row and `paper_risk_decisions` row carry the same provenance, and N
candidates no longer trigger N Registry stamp resolutions. Guard 14r asserts both
passing sites and forbids re-resolving inside the candidate loop.

### 15.4 Guards and evidence quality

New permanent guards (Guard 14 was 14l-14q from round 1):

| guard | assertion |
|---|---|
| `14r` | signal risk decisions consume the frozen batch stamp (no per-candidate re-resolution) |
| `14s` | the signal commit phase is `BEGIN IMMEDIATE`, with no provider call inside the lock |
| `14t` | the DB insert guard requires the account/cycle binding, keeps legacy NULL and archive semantics |

Evidence-quality fixes to the harness itself:

- **M-SP20 was not a valid mutant** — it deleted the `pin` assignment and killed the
  test with `NameError`, which proves nothing. It is now a runnable wrong
  implementation (pin taken after a successful `_run_one`). `RV07` fails on
  `AssertionError: 2 != 1` (stored v2, expected v1), with syntax valid and no wiring
  error — verified by `work/r23_round3_mutation_business_check.py`.
- The fake detector now rejects `NameError`, `UnboundLocalError`, `TabError` and
  similar runtime wiring errors, so none of them can count as a business CAUGHT.
- The Guard 14p non-vacuity mutant is now legal Python (pin moved inside the `try`
  after `_run_one`), and its runner classifies wiring errors as FAKE.
- The anchor audit now enforces **`count == 1`** for ordinary anchors (they previously
  passed with duplicates, while `_apply` replaces the first hit — an anchor could
  drift onto a different call site and still report CAUGHT). `last=True` anchors are
  validated by their own rule.

### 15.5 Verification for this round

| gate | result |
|---|---|
| `RV01`-`RV11` production regressions | all green; `RV08` discriminating (immediate GREEN / deferred RED) |
| targeted provenance + guard modules | **374 tests OK** (see §16.4 for the exact module list) |
| full backend suite | **4006 tests OK** (local isolated clone: `skipped=5`) |
| serial mutation matrix (21, non-vacuity) | **21/21 CAUGHT; survived=0; fake=0; baseline-red=0; restore sha256 PASS** |
| M-SP19/20/21 business-failure proof | syntax valid, no wiring error, fails on the contract assertion |
| Guard 14l-14p non-vacuity | **caught=5/5; survived=0; fake=0; restore sha256 PASS** |
| anchor audit | **21 anchors, count == 1 each; 0 missing; 0 duplicate** |
| ruff / compileall | `All checks passed!` (backend) / clean |
| frontend build / unit | build clean (no `dist` diff) / **111 pass, 0 fail** |
| leak scan `--scope worktree` and `--scope all` | `kinds: none; values: 0` |

### 15.6 Fixture corrections forced by the DB guard

Adding the account/cycle insert guard surfaced **seven** existing fixtures that
constructed a state production cannot reach — a signal bound to a cycle its account
does not belong to (production binds both together in `_create_cycle`). The fixtures
were corrected to the real shape (`test_order_intent_contract`,
`test_paper_cycle_service`, `test_replacement_asof_provenance`); the guard was not
relaxed. The guard also degrades safely on a minimal schema: without
`paper_accounts` it installs only the cycle-existence half, so it never references a
missing table.

## 16. Browser E2E stability investigation, and harness simplification

This round changed **no production logic**. It answered the E2E flakiness question
with measurements, and removed two pieces of verification complexity that had become
self-contradictory.

### 16.1 The browser job on `6fbf475` did have a flaky test — stated plainly

On `6fbf475685216cd71d85e455e023835181e20d1d` the CI *check* was green, but the
browser job log is not:

```
Running 32 tests using 1 worker
  ✘ 11 ... paper-runtime.spec.js:23:3 ... 面板只读：无定义编辑控件，且可跳回策略工坊详情 (1.0m)
  ✓ 12 ... paper-runtime.spec.js:23:3 ... (retry #1) (49.4s)
  1 flaky
  31 passed (3.7m)
```

The first attempt hit `Test timeout of 60000ms exceeded` while waiting for
`paper-runtime-strategies [data-testid^="paper-runtime-card-"]` to become visible.
So "CI is green" is **not** evidence that E2E is stable, and this PR does not claim
32/32 for that run.

### 16.2 Reproduction, then root cause

Round-robin reproduction (`--workers=1 --retries=0`, paper-runtime only, 5 serial
rounds): **5/5 PASS**, but round 1 cost 48.1s vs ~6.7s afterwards.

Timing the three read sources the page actually requests (cold and warm):

| endpoint | cold | warm |
|---|---|---|
| `/api/health` | 0.003s | 0.024s |
| **`/api/paper/allocation-explain`** | **4.637s** | **4.672s** |
| `/api/strategies?include_archived=true` | 0.046s | 0.042s |
| `/api/paper/strategy-center` | 0.018s | 0.017s |

Segment-timing the slow one shows the cost is entirely
`dfc.fetch_market_snapshot_full(max_age=240)`: **4.4s with network, 13.8s without**
(and it is paid on *every* call, because a failed refresh never populates the TTL
cache). The DB layer is not involved: `_db()` open+query 0.003s,
`_db(immediate=True)` open+query 0.004s.

That explains the CI behaviour: CI runs offline, so the read path pays ~13.8s —
against a 60s per-test budget that also covers `page.goto` and the workbench
navigation. It is a fixed environment-sensitive cost, not a lock or a regression.

### 16.3 Is it a R23 regression? No — verified two ways

**(a) Frontend is byte-identical.** `git diff origin/master..HEAD -- frontend/` is
empty, so the page requests exactly the same things in both versions. (Reproduce by
fetching the branch; the range is expressed with refs rather than hashes so it cannot
go stale as documentation commits land.)

**(b) Genuinely interleaved A/B on the slow path** (`work/r23_round4_ab_timing.py
--rounds 6`). Each round measures **both** versions back to back and the order swaps
every round (AB, BA, AB…), so drift in machine load is shared by both sides.
Independent process and independent temp data dir per measurement, same CI-like
no-network condition throughout. The runner asserts the schedule really is
interleaved (every round contains both versions, the leading version alternates,
longest same-version run ≤ 2) and prints the real order:

```
master → R23 → R23 → master → master → R23 → R23 → master → master → R23 → R23 → master
```

Two independent interleaved runs, `strategy_allocation_explain` median delta
(R23 − master):

| run | Δ explain | verdict |
|---|---|---|
| 1 | **+0.035s** | NO REGRESSION |
| 2 | **+0.088s** | NO REGRESSION |

Both are tens of milliseconds against a ~13.8s single-pass cost — noise, not a
difference. Per-sample seconds drift with runner load and are not reproduced here,
because they are not merge evidence.

`BEGIN IMMEDIATE` is not implicated: the lock audit (§16.6) shows no provider call
inside the write lock, and the DB costs 3-4 ms either way.

**Conclusion: pre-existing environment-sensitive E2E flake.** It lives in the
read-path snapshot refresh reaching the network, and it predates this PR. This PR
does **not** widen scope to fix it, and does not weaken any timeout or add retries to
hide it. Recorded as follow-up debt: `allocation-explain` is a read-only view and
should not synchronously refresh a network snapshot; the fix belongs with the
market-data boundary work (R24), where the read path can consume a cached snapshot
that the scan/job path keeps warm.

### 16.4 Independent stability verification (this round)

Under the CI-equivalent configuration (`--workers=1 --retries=0`):

- `paper-runtime.spec.js` × 5 serial rounds: **5/5 PASS, 0 flaky, 0 retry**
- full browser suite: **32 passed, exit 0, 0 flaky, 0 retry**

The exact-head CI run from §16.1 is reported as it happened (1 flaky / 31 passed);
the numbers above are a separate, later measurement under the same settings.

### 16.5 The exact-head browser job is clean

Browser job: **32 passed, `workers=1`, no retry, no flaky** — on every head of this PR
since the investigation in §16.1, current head included. `flaky` and `retry #` do not
appear anywhere in those logs (grep count 0).

GitHub's checks on the head actually being merged are the authority for the exact
revision. This section deliberately does not name one, because this PR's later
commits are documentation-only and would make any SHA written here stale — the same
drift that §16.5 previously suffered. What matters for merge is that the property
holds on the head under review, not that a particular hash was typed here.

The other exact-head checks are green as well: `tests (3.11)`, `tests (3.12)`
(**Ran 4006 tests, OK**), `syntax`, `quality`, `docker-smoke`, `frontend`,
`security-leak-scan` ×2.

Note that this clean run does not prove the underlying slowness is gone — it is the
same offline snapshot-refresh cost from §16.2, which happens to fit inside the budget
when the runner is not additionally loaded. That is exactly why it stays follow-up
debt rather than being declared fixed here.

Targeted module list and exact counts behind §15.5's **374**:

| module | tests |
|---|---|
| `test_provenance_inflight_change` | 11 |
| `test_strategy_selection_provenance` | 57 |
| `test_paper_trading_architecture_guard` | 114 |
| `test_paper_selection` | 15 |
| `test_selection_tradability` | 124 |
| `test_order_intent_contract` | 8 |
| `test_paper_cycle_service` | 12 |
| `test_replacement_asof_provenance` | 33 |
| **total** | **374** |

Skipped counts are environment-specific: the local isolated clone reports
`skipped=5`; GitHub Actions Python 3.12 on the previous head reported `skipped=1`.
They are not one number and are not presented as one.

### 16.6 Lock scope re-check

`generate_signals`'s commit phase (`paper_trading.py:7660-7742`) is audited by
`work/r23_round4_lock_scope_audit.py`: the 13 functions and 6 methods called inside
it are all local decisions (`_signal_approval`, `_order_intent_payload`,
`_with_decision_snapshot`, `_completed_kline` reading cached bars), DB writes
(`execute`, `fetchone`) and the context check
(`signal_write_context_or_error`). No provider/network entry point appears, so all
provider work still completes before the lock is taken. The stale-validation step was
deliberately kept inside the transaction — moving it out would reopen the
validation→INSERT window that `RV08` locks down.

### 16.7 Harness simplification

**Sharding removed.** The runner documented "must run serially" while still shipping
`--shard/--shards`; concurrent shards rewrite the same production files and pollute
each other (an earlier 5-way sharded run was invalid, tally 0-1/3). The flags, the
`index % shards` selection and the usage example are gone — serial is the only mode,
and the dangerous mode no longer exists.

**Anchor uniqueness enforced where the mutation happens.** `_apply()` used
`assert count >= 1` + `replace(..., 1)`: the function that actually rewrites source
accepted a duplicate anchor, letting the mutation land on a different call site while
still reporting CAUGHT. It now asserts `count == 1` with the id/count/file in the
message, so the safety condition does not depend on a reviewer remembering to run a
second script.

**`last=True` removed.** No mutation used it (0 of 21). The `rfind` branch in
`_apply()`, the special case and the `last_mode` tally in the anchor audit are
deleted rather than kept "in case". `work/r23_round2_anchor_check.py` is now a plain
audit report: 21 mutations, all anchors must be exactly 1.

One mutation = one unique anchor = one named regression.

## 17. Merge status

Not merged, not deployed. Awaiting human review.



