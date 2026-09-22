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

## 14. Merge status

Not merged, not deployed. Awaiting human review.
