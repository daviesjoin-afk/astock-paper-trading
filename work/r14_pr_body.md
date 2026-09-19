## ⛔ STATUS: DO NOT MERGE / DO NOT DEPLOY — awaiting human review

## Summary

`peak_price` (trailing-stop peak) and `take_stage` (staged take-profit pointer)
are execution-adjacent runtime risk state, but they lived in the
`paper_positions` projection, which has **no cycle identity and no position
episode identity** (`PRIMARY KEY(account_id, code)`). Stale values from an old
cycle/episode leaked into the new cycle and **genuinely changed sell
decisions** (before-fix repro: `work/r14_before_fix_repro.py`, R14-C1/C2/C3
reproduced on base; C4 not reproducible on base, reported honestly).

This PR moves that state into a new **cycle-owned authority table** and demotes
`paper_positions` to a zero-execution-authority compatibility projection.

## Authority matrix

| Fact | Authority | Legal consumers | Must never come from |
|---|---|---|---|
| quantity / ownership / cost | `paper_position_lots` | read model | `paper_positions` |
| runtime peak / take_stage | `paper_position_risk_state` (same cycle only) | read model → `_sell_plan`, monitor peak write | `paper_positions` |
| compatibility display | `paper_positions` | display/panel only | any execution decision |

Missing risk state is fail-safe: peak anchored to cost (identical to a fresh
episode default; intraday-high absorption still applies), `take_stage=None`
(staged take-profit skipped — unknown is never guessed). Hard stop / max hold
are untouched and keep working. **Unknown must not be upgraded into known.**

## Changes

- `paper_schema_migrations.py` + `db_migrate.py`: v20 — new table
  `paper_position_risk_state(cycle_id, account_id, code, peak_price,
  take_stage, opened_order_id, initialized_at, updated_at)`,
  PK `(cycle_id, account_id, code)`, `cycle_id NOT NULL`,
  BEFORE INSERT (cycle required & must exist in `paper_cycles`) and
  BEFORE UPDATE OF cycle_id (immutable) triggers. Lives in
  `paper_trading.sqlite3`. **Never backfilled** from `paper_positions`.
- `paper_position_read_model.py` / `paper_portfolio.py`: read path sources
  peak/take_stage **only** from same-cycle risk state; pure read; no fallback
  to the projection; missing state → fail-safe + `risk_state_source`.
- `paper_position_risk_state.py` (**NEW**, Round-2): the sole runtime owner of
  the table — `initialize_episode` / `update_peak` (max-only) /
  `update_take_stage` / `delete_episode` / `finalize_sell`. Zero project-level
  imports; owns no transaction; resolves no active cycle.
- `paper_trading.py`: BUY episode lifecycle in `_record_lot`
  (`0 -> qty` initializes, add-on preserves state and only raises peak) and the
  risk-scan sell finalization now delegate to that module. The four Round-1
  write primitives (`init_position_risk_state` / `update_position_peak` /
  `update_position_take_stage` / `delete_position_risk_state`) were **moved
  out**; no forwarding wrappers remain. `_sync_positions` stays
  authority → projection only.
- `paper_cycle_service.py`: ledger/purge lists include the new table.
- `demo_seed.py`: seeds write the cycle-owned table, not just the projection.
- Contract flips: `test_authoritative_position_consumers` (mirror has zero
  risk authority), `test_legacy_position_rematerialization` (LP6 fail-safe).

## Tests

- New `backend/test_position_risk_state.py`: PRS-1..PRS-15 + v20 migration
  suite (fresh / registered / upgrade-with-guards / idempotent / row
  preservation / **never backfill** / cycle-required & immutable triggers) +
  dual-DB ownership (adaptive_learning never receives the table) +
  cycle purge coverage + production sell-path coverage — 38 cases, all green.
- Related suites: authoritative position consumers, rebalance cycle scope,
  execution verification, deferred-fill cycle binding, legacy position
  rematerialization, paper portfolio, db migrate, cycle service,
  cycle ledger ownership, risk-exit / sell-rebuy / asymmetric risk.
- Frontend: build + dist freshness, unit 111/111, Chromium E2E
  (1 worker).
- Negative mutation matrix `work/r14_mutation_check.py` (M-PRS1..M-PRS15):
  baseline GREEN, every mutation RED with the failure coming from the
  corresponding contract, files restored byte-identical (sha256).
- Before-fix repro `work/r14_before_fix_repro.py` run on the unmodified base:
  C1/C2/C3 reproduced, C4 not reproducible (documented).

## Security

- No credentials, tokens, cookies, or API keys introduced.
- No personal emails, hostnames, server addresses, or account data.
- `scan-sensitive-data.py` worktree + all: values=0, exit 0.
- gitleaks on `origin/master..HEAD`: no leaks found (existing baseline
  findings unchanged, all pre-existing test fixtures / ignored runtime cache).
- Git identity: `daviesjoin-afk` / noreply address only.

## Risk

Read-model fail-safe defaults are deliberately identical to fresh-episode
behavior, so legacy positions (no risk-state row) behave like new episodes —
no risk parameter (hard_stop / trail / take_profit / hold_max) is changed.

## Rollback

Revert this PR.

---

## Round-2: full-exit lifecycle closure + architecture boundary

Review found that risk-scan full exits cleared position risk state,
but execution_planner SELL and intraday SELL could consume the last
authoritative lot without closing the position episode.

The runtime risk-state CRUD also lived directly in paper_trading.py,
increasing the 16k-line god module.

Round-2 fixes both:
- one cycle-scoped sell finalizer shared by every production SELL path;
- runtime risk-state authority moved to paper_position_risk_state.py;
- paper_trading keeps orchestration only;
- architecture guard prevents the authority from moving back.

### Blocker A — every full-exit path now closes the episode

`_monitor_risk_impl` was the only path that finalized the episode. Two further
production SELL paths could consume the final authoritative lot and leave a
live-looking `paper_position_risk_state` row behind:

- `execution_planner.commit_fill` SELL branch (manual / deferred orders);
- `_intraday_sell` — with `available == LOT_SIZE == 100`,
  `qty = max(LOT_SIZE, int(available * 0.30 / LOT_SIZE) * LOT_SIZE) = 100`,
  i.e. an intraday trim on a one-lot holding **is** a full exit.

The fix is one shared finalizer, `paper_position_risk_state.finalize_sell`,
which reads the authoritative same-cycle quantity itself:

```text
remaining_qty <= 0                      -> DELETE state (episode ends)
remaining_qty >  0 and next_take_stage  -> UPDATE take_stage
remaining_qty >  0 and no stage fact    -> preserve state as-is
```

The termination fact is `same-cycle authoritative remaining lots == 0`, taken
from `paper_position_lots` — not a `position_closed` boolean recomputed by each
caller from its own local variables. Every call site passes the cycle it has
already proven: `sell_cycle_id` for the risk scan and intraday trims,
`order_cycle_id` for `commit_fill`. The finalizer never resolves an active
cycle and never owns a transaction.

`position_closed` is still computed in the risk scan and still used for
capacity / recovery-watch / detail semantics — only its use as a *risk-state
lifecycle* trigger was removed.

#### SELL path matrix (repository audit, not just the two reported functions)

| Sell path | Full exit possible | Lifecycle finalizer |
|---|---:|---|
| risk monitor (`_monitor_risk_impl`) | yes | PASS (`PPRS.finalize_sell`) |
| `execution_planner.commit_fill` SELL | yes | PASS (`PPRS.finalize_sell`) |
| intraday sell (`_intraday_sell`) | yes (`available == LOT_SIZE`) | PASS (`PPRS.finalize_sell`) |
| seeded demo data (`demo_seed.py`) | n/a (fixture writer, not an execution path) | not applicable — seeds write the cycle-owned row directly and `_drop_position` clears it |

Audit method: every occurrence of `_consume_available_lots(` and every
`INSERT INTO paper_fills` with `side='sell'` was classified (production path /
lot consumption method / cycle provenance / finalizer / full-exit capable).
No fourth production full-exit path exists. BUY side was audited the same way:
every production BUY reaches `_record_lot` (strategy fill path and
`execution_planner.commit_fill`), which is the single episode-initialization
hook.

### Blocker B — authority left the god module

The four runtime CRUD helpers that Round-1 added to `paper_trading.py` are now
owned by `backend/paper_position_risk_state.py`:

```text
paper_trading      -> paper_position_risk_state
execution_planner  -> paper_position_risk_state   (direct dependency, no PT forwarding)
paper_position_risk_state -> stdlib only
```

The new module has **zero project-level imports**, owns no schema (v20 DDL stays
in `paper_schema_migrations`), and exposes no active-cycle resolver. No
forwarding wrappers were kept in `paper_trading.py`: these are new APIs with no
legacy compatibility value.

### Architecture metrics

| Revision | `paper_trading.py` lines | module-level functions |
|---|---:|---:|
| master `4cb871c` | 16314 | 287 |
| Round-1 head `94dee6b` | 16447 | 291 |
| **Round-2 final** | **16365** | **287** |

Round-2 is **82 lines and 4 functions below** the Round-1 head, and the
function count is back to the master baseline. The residual +51 lines over
master are orchestration/wiring only, line by line:

- `import paper_position_risk_state as PPRS` — 1 line;
- two `PSM.ensure_position_risk_state(conn)` calls with comments — 4 lines;
- `_record_lot`: `prior_qty` read via the authority module + the
  `initialize_episode` / `update_peak` branch plus comment — 14 lines;
- `_monitor_risk_impl`: peak write delegation (3 lines) — 5 lines;
- `_monitor_risk_impl`: `finalize_sell` call replacing the previous two-branch
  write (`if position_closed` / else) — 6 lines;
- `_intraday_sell`: `finalize_sell` call — 6 lines;
- `_sell_plan` / `_sync_positions`: `take_stage=None` fail-safe handling and
  the projection-direction note — 15 lines.

No domain implementation stays in `paper_trading.py`: the table's runtime
ownership, the episode-termination predicate and the three-state transition
logic all live in the new module.

### Architecture guard (first version, `backend/test_paper_trading_architecture_guard.py`)

An anti-regression baseline, not a full refactor. Ten deterministic AST /
source assertions:

1. the listed domain modules must not reverse-import `paper_trading`
   (allowlist is empty; `paper_position_risk_state.py` may never join it);
2. `paper_trading.py` must contain zero `paper_position_risk_state` CRUD SQL and
   none of the four removed wrapper defs;
3. `paper_trading.py` LOC / module-level function count must not exceed the
   Round-2 baseline (no env-var or `skip if CI` escape hatch);
4. the new module must have zero project-level imports and must not own
   transactions or resolve cycles (docstrings excluded from the SQL scan);
5. all three production SELL paths must call `PPRS.finalize_sell`, and
   `execution_planner` must not reach it through `paper_trading`.

### Tests added / replaced for Blocker A

The previous `test_PRS7_full_exit_clears_state_via_sell_finalization` called
`_consume_available_lots` and `delete_position_risk_state` itself — it proved
the delete helper works, not that production invokes it. It is replaced by
tests that drive the real paths:

- `test_risk_full_exit_deletes_position_risk_state` — `PT.monitor_risk`;
- `test_execution_planner_full_sell_deletes_position_risk_state` — real
  `EP.commit_fill` SELL;
- `test_execution_planner_partial_sell_preserves_position_risk_state` —
  reverse guard against "every sell deletes";
- `test_intraday_full_sell_deletes_position_risk_state` — real
  `PT._intraday_sell`, 100-share one-lot full exit;
- `test_intraday_partial_sell_preserves_position_risk_state`;
- `test_full_exit_then_same_cycle_reentry_starts_fresh_episode` — BUY → full
  SELL → same-cycle BUY, asserting new peak / stage 0 / new episode timestamp;
- `test_buy_through_commit_fill_records_the_source_order` — BUY-side audit.

### Before-fix reproduction (`work/r14_round2_before_fix_repro.py`)

Run against base `94dee6b`, before the fix:

```text
R14-R2-C1 execution_planner full SELL: remaining_lots=0 risk_state_present=True   REPRODUCED
R14-R2-C2 intraday 100-share full SELL: sold=100 remaining_lots=0 risk_state_present=True   REPRODUCED
```

After the fix: both `NOT REPRODUCED`. Temporary SQLite only; the production
database is never touched.

### Mutation matrix (Round-2 additions)

| ID | Mutation | Designated contract |
|---|---|---|
| M-PRS11 | `commit_fill` SELL drops `finalize_sell` | execution_planner full sell |
| M-PRS12 | `_intraday_sell` drops `finalize_sell` | intraday full sell |
| M-PRS13 | `finalize_sell` treats partial as full exit | execution_planner partial sell |
| M-PRS14 | `finalize_sell` reads lots without `cycle_id` | cross-cycle predicate isolation |
| M-PRS15 | new module reverse-imports `paper_trading` | architecture guard (Guard 4) |

All 15 mutations RED, `survived = 0`, every mutated file restored
byte-identical (sha256 verified).

### Validation (Round-2)

- targeted suites: 315 tests OK (skipped=1);
- focused new suites: 48 tests OK (`test_position_risk_state` 38 +
  `test_paper_trading_architecture_guard` 10);
- full backend: **3489 tests OK** (skipped=5), exit 0;
- ruff: `All checks passed`; `compileall -q backend`: exit 0;
- frontend: `npm run build` PASS, unit **111/111** (0 fail),
  `git diff --exit-code -- frontend/dist` clean;
- Chromium E2E (local, `--workers=1`): **local caveat reported honestly** —
  three consecutive full runs each produced `31 passed / 1 failed`, with a
  *different* spec failing each time (`responsive-accessibility` 390px,
  `journey1-create-draft`, `bridge-contract`), and every failure a pure
  timeout (`page.goto` 30s / `waitForResponse` 15s / `not.toContainText` 15s)
  rather than an assertion failure. Each affected spec passes in isolation and
  passes inside other full runs; no frontend file is touched by this PR. This
  is a local Windows environment timing artifact, so the authoritative
  `browser-e2e (chromium)` signal is the CI job on the new exact head;
- `scan-sensitive-data.py`: worktree `kinds: none / values: 0`; all
  `kinds: none / values: 0`;
- gitleaks unchanged baseline;
- review thread "Clear risk state on every full-exit path" resolved after
  confirming the production paths actually invoke the finalizer.

### Scope

No risk parameter changed (`hard_stop` / `trail_after` / `trail_stop` /
`take_profit` / `hold_max` / `qty_ratio` / opening-event threshold / intraday
threshold / `SLIPPAGE` / commission all untouched). Sell decisions are
unchanged: the finalizer runs only after authoritative lot consumption
succeeds and never influences whether a sell fills. v20 migration version,
table identity, PK, cycle guards and no-backfill semantics are unchanged. No
changes to selection, learning, rebalance strategy, frontend features, AI or
market data.
