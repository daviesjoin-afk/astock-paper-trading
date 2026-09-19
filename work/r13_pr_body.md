# fix(ledger): route current-position consumers through authoritative lots

## What this is

PR #168 established `paper_position_lots` as the executable position authority and
`paper_positions` as a compatibility projection. This PR removes the next layer of
the same problem: modules that still ran their own
`SELECT ... FROM paper_positions` and read the result as **"what we hold right
now"**.

`paper_positions` carries no `cycle_id`, no `source_order_id` and no verified
acquisition evidence, so it cannot prove that a row belongs to the current cycle.
A stale mirror row left over from an old cycle therefore fed candidate exclusion,
holding classification and the shadow portfolio.

```
paper_positions is never evidence that a position is currently held.
```

**Round-11 update.** The current-position *source* was corrected in Round-10, but
the *database ownership* was still wrong: the rebalance endpoints kept opening
`adaptive_learning.sqlite3` while reading paper-ledger facts.

**Round-12 update.** Round-11 fixed the database ownership, but the rebalance
state itself was still not cycle-owned. All three rebalance state tables had no
`cycle_id`, so rebalance facts could cross a paper-cycle boundary. See
"Round-12" below.

## Base SHA

`0c1546a90c72269ee002c843c18d6089bce0ad70`

## Final exact head

`8b02299` (Round-13 fix) — supersedes `4fa216222ac600238e300bb5bf0bcb9ae02a5c3f` (Round-12), `5b04c9b48235c010d299b59a2b0dc92e469187ec` (Round-11) and `c48a80ca6991c72cab7a96284983a8425fc42c16` (Round-10). The exact head also includes the Round-13 mutation-correction and PR-body commits; see the checks section for the current exact head.

## The unified reader

New module `backend/paper_position_read_model.py` is now the **single**
implementation of "current positions":

| requirement | how it is met |
| --- | --- |
| read-only | writes nothing; `connect_readonly` uses `mode=ro` |
| current active cycle only | `WHERE cycle_id=?` with the resolved active cycle |
| active-cycle lookup is read-only | `active_cycle_id()` never calls `_ensure_cycle` |
| no active cycle ⇒ `[]` | fails closed; **never** falls back to the projection |
| quantity from lots | `paper_position_lots.remaining_qty` |
| never creates a position from the projection | legacy rows only enrich metadata |

It depends only on the zero-dependency `execution_verification` and
`paper_portfolio` modules, so `paper_trading`, `news_learning`,
`rebalance_scanner` and `adaptive_engine` can all share it without a circular
import. `paper_trading._position_rows` now delegates to it, so there is exactly
one current-position truth implementation (spec §4).

Public surface: `current_positions`, `current_holding_keys`, `current_held_codes`,
`current_holding_rows`, `active_cycle_id`, `connect_readonly`.

## Consumer audit (spec §2)

| consumer | class | disposition |
| --- | --- | --- |
| `paper_trading._position_rows` | A current-position authority | delegates to the reader |
| `paper_portfolio.aggregate_positions` | A (aggregation primitive) | unchanged: lots drive qty; mirror supplies `peak_price`/`take_stage` only |
| `news_learning.candidate_pool` | B decision-adjacent | **switched** to authoritative holding rows |
| `rebalance_scanner.find_replacement_candidates` | B decision-adjacent | **switched** `held_codes` to authoritative holdings |
| `api_adaptive` rebalance scan | B decision-adjacent | **switched** the positions handed to `daily_close_scan` |
| `adaptive_engine._portfolio_shadow_arbitration` | D shadow/advisory | **switched** portfolio input |
| `adaptive_engine._disclosure_scope_codes` | D shadow/advisory | **switched** current-holding codes |
| `news_learning._paper_codes` | E display/discovery | **kept on the projection on purpose** (see below) |
| `strategy_champion.collect_ledger_metrics` | dead / legacy | **registered legacy**; carried value now lot-evidence based |
| `dashboard_queries` | E display | unchanged (already via `_position_rows`) |
| `deepseek_advisor` / `deepseek_research` / `ai_analysis` | E display/diagnostics | unchanged; documented as non-authoritative |
| `paper_replay_regression` | G consistency check | unchanged |
| `demo_seed` | H test/dev fixture | unchanged |
| `paper_cycle_service` | G archive/purge | unchanged |
| `paper_schema_migrations` | G schema | unchanged |

## Two things I audited instead of mechanically replacing (spec §6/§9)

**`news_learning._paper_codes` stays on the projection — deliberately.**
Its job is bounded *recent symbol discovery* for the announcement scan, not a
holding claim. Using `entry_date` as a "was seen" timestamp is correct for that
purpose. What was wrong was that the same function's caller marked everything as
`pool_tier="holding"`. That path now comes from authoritative lots, and the
distinction is documented in the function so the next reader cannot re-conflate
them. It can no longer mark anything as holding, raise holding priority, or
participate in candidate exclusion.

**`strategy_champion.collect_ledger_metrics` is dead in production.**
Promotion uses `collect_shadow_ledger_metrics`; the only caller in the repo is
`test_strategy_invariants`. It is registered as legacy in its docstring and now
returns `legacy_helper: True`. Its `carried_value` used to read
`SUM(qty*cost) FROM paper_positions WHERE entry_date<since` — i.e. today's
projection standing in for a historical window. It now sums
`remaining_qty*cost` over lots acquired before `since`, and reports
`carried_value_evidence: "unprovable_no_lot_ledger"` with 0 when no lot ledger
exists, rather than falling back to the projection.

## A live consumer the review did not list

`api_adaptive.py`'s rebalance endpoint feeds `daily_close_scan`, which evaluates
holding quality and **generates rebalancing plans** from those positions. That is
an HTTP-reachable production path, not the unreachable helper the spec assumed
for `find_replacement_candidates`.

**Correction (Round-11).** Round-10 fixed the current-position *source* on that
path but left its *database ownership* wrong. The claim "an HTTP-reachable
production path, and was fixed" was incomplete. What Round-11 found:

> Round-11 review found that the endpoint still opened
> `adaptive_learning.sqlite3` while reading paper-ledger facts.
>
> The current-position source was corrected in Round-10, but the database
> ownership itself was still wrong.

---

# Round-11: rebalance database ownership

## The defect

`adaptive_engine` draws a hard line:

```
DB_PATH       = adaptive_learning.sqlite3
PAPER_DB_PATH = paper_trading.sqlite3
```

All four rebalance endpoints used `adaptive._connect()` (⇒ `DB_PATH`) and then ran
paper-ledger queries on it:

- `SELECT * FROM paper_accounts WHERE status='running'`
- `PPRM.current_positions(conn, ...)`, which requires `paper_cycles`,
  `paper_position_lots`, `paper_positions`, `paper_orders`

`adaptive_learning.sqlite3` has **none** of those tables and does not `ATTACH`
the paper ledger. So the endpoint could not answer a current-position question at
all — and, worse, `ensure_schema(conn)` created `rebalance_scans` /
`rebalance_plans` / `rebalance_cooldown` **inside the adaptive DB**.

## Before-fix reproduction (spec §2)

Two physically separate SQLite files, real `paper_trading.init_db()` for the paper
DB and real `adaptive._connect()` for the adaptive DB, quotes patched so no
network is touched, then the real handler is called:

```
work/r11_before_fix_repro.py

paper.sqlite3    = ...\paper.sqlite3
adaptive.sqlite3 = ...\adaptive.sqlite3
same file? False

paper.sqlite3   : paper_accounts YES  paper_cycles YES  paper_position_lots YES
adaptive.sqlite3: paper_accounts no   paper_cycles no   paper_position_lots no

paper DB running accounts = [('tq_breakout', 1)]
paper DB active cycle     = 1

── observed endpoint behaviour ──
RAISED HTTPException: 500: 调仓扫描失败：OperationalError: no such table: paper_accounts
  cause: OperationalError: no such table: paper_accounts
  at api_adaptive.py:583  for acc in conn.execute("SELECT * FROM paper_accounts ...")

── where rebalance_* schema appeared after the call ──
paper.sqlite3        []
adaptive.sqlite3     ['rebalance_scans', 'rebalance_plans', 'rebalance_cooldown']

BEFORE-FIX REPRODUCTION: PASS
```

This is the failure the review predicted, observed rather than inferred, and it
also shows the second half of the damage: the wrong DB got the rebalance schema.

## Database ownership audit (spec §3/§8/§9/§27)

`rebalance_scanner` is **paper-ledger coupled by construction**:

| table | class | in production schema |
| --- | --- | --- |
| `paper_orders` | Paper ledger fact | YES |
| `paper_signals` | Paper ledger fact | YES |
| `risk_log` | Paper ledger fact | **NO** (see below) |
| `rebalance_scans` | Rebalance state | created by `ensure_schema` |
| `rebalance_plans` | Rebalance state | created by `ensure_schema` |
| `rebalance_cooldown` | Rebalance state | created by `ensure_schema` |

`daily_close_scan` reads paper facts and writes rebalance state **on the same
connection**. Those persisted plans must come from the same snapshot as the
positions they were derived from, so:

```
rebalance_scanner production DB owner = paper_trading.sqlite3
```

Production row counts (read-only, `mode=ro&immutable=1`, no copy taken):

```
adaptive DB:  paper_accounts present: False   paper_position_lots present: False
              rebalance_scans: absent   rebalance_plans: absent   rebalance_cooldown: absent

paper DB:     paper_accounts present: True    paper_position_lots present: True
              active cycle: 162   open lots (remaining_qty>0): 4
              rebalance_scans: absent   rebalance_plans: absent   rebalance_cooldown: absent
```

That audit was taken **before** Round-12. Afterwards the local pair reads:

```
paper DB:     rebalance_scans rows=0   rebalance_plans rows=0   rebalance_cooldown rows=0
adaptive DB:  rebalance_scans ABSENT   rebalance_plans ABSENT   rebalance_cooldown ABSENT
```

The three tables now exist in the paper DB with **zero rows**, created by the v19
migration through `init_db()`. Empty state is zero-authority, and the adaptive DB
still holds none — which is the invariant that matters.

`adaptive_learning.sqlite3` holds **no** rebalance rows, so **no data migration is
needed** by this PR (spec §10). Nothing was deleted, copied or overwritten.

## The fix (spec §4–§7)

```python
@contextmanager
def _paper_rebalance_db():
    with PST.db(adaptive.PAPER_DB_PATH) as conn:
        yield conn
```

- Built on the project's existing write helper `paper_storage.db` — no bespoke
  WAL / timeout / commit logic.
- Deliberately **not** `paper_ledger_reader` / `PPRM.connect_readonly`: those are
  `mode=ro` + `PRAGMA query_only=ON`, and a scan must write.
- Deliberately **no** `ATTACH DATABASE`: cross-database transactions add locking,
  partial-commit and backup complexity, and the scanner already depends on the
  paper ledger. The minimal correct model is *rebalance state lives with the paper
  ledger*.
- One scan = **one** connection = **one** transaction. Quotes are still fetched
  before the connection opens, so no transaction spans the network.
- All four endpoints (`status` / `scan` / `verify` / `plans`) share it, so
  "scan writes A, status reads B" is not expressible.

`/rebalance/rollback` is unchanged: it calls `adaptive.rollback_selection` and
touches only adaptive-owned selection state, never the scanner (spec §26
exception). `/dual-ai/runs` likewise remains on `adaptive._connect()`.

## A second defect found while reproducing the first

`rebalance_scanner._is_risk_handled` queried `FROM risk_log`:

```
SELECT decision, reason FROM risk_log WHERE ... decision IN ('hard_stop', ...)
```

**That table does not exist.** `paper_trading.init_db()` creates 40 tables and
`risk_log` is not one of them; a repo-wide search finds no `CREATE TABLE
risk_log` anywhere, and `git log -S` shows the query has been there since the
initial commit. `risk_log` is the name of the `_risk_log()` **function**, not a
table. The statement therefore always raised `no such table: risk_log`, failing
the entire scan 100% of the time — even once the database was correct:

```
daily_close_scan on real production schema (pre-fix)
  RAISED OperationalError: no such table: risk_log
```

The authoritative replacement is `paper_orders.risk_payload`, where `_sell_plan`
records `exit_class` / `exit_reason_code`. This is the same source
`paper_trading` itself uses for its own "did a protective exit already happen"
decision (`hard_stop_touched_today`), so no second reading of the same fact is
introduced. As with the neighbouring `filled_sell` check, only an
**evidence-verified** fill suppresses rebalancing.

## Tests (spec §11–§18)

17 new end-to-end tests in `backend/test_rebalance_db_ownership.py`, driving the
real HTTP handlers (`ensure_schema` / `PPRM.current_positions` /
`daily_close_scan` / `verify_all_plans`) — not `mock.patch` on the scanner:

| id | asserts |
| --- | --- |
| E2E-RB1 | scan reads the paper ledger; no rebalance state appears in the adaptive DB |
| E2E-RB2 | a stale mirror produces no `rebalance_scans` / `rebalance_plans` row via the real API |
| E2E-RB3 | the authoritative lot reaches the scanner; scan qty comes from the lot, not the mirror |
| E2E-RB4 | rebalance writes land in the paper DB only |
| E2E-RB5 | `status` and `plans` see exactly what `scan` wrote (split-brain impossible) |
| E2E-RB6 | `verify` reads and updates the paper DB plan |
| risk_log | the missing table is gone; a real-schema scan completes; verified protective exit ⇒ `risk_handled`; unverified and non-protective exits do not |

Every case begins by asserting the two database paths differ
(`os.path.realpath`), because if both point at one file the defect is
unobservable by construction.

## Architecture guard tightened (spec §19/§20)

`ProjectionContractGuard` previously exempted **whole modules** —
`"backend/api_adaptive.py": "display read model"`. That module also contains the
decision-adjacent `/rebalance/scan` write path, so "the same file has a display
function" is not a reason to exempt the file.

- Whitelist is now **function-granular** (`backend/<file>.py::<function>`).
- `api_adaptive.py` is **removed entirely** — it no longer reads the projection.
- Docstrings are skipped by the AST scan rather than whitelisted, so no exemption
  exists for prose.
- Stale whitelist entries now fail, and a module-level key fails by construction.
- Verified non-vacuous by re-adding the whole-module exemption: the guard fails.

## Mutation (spec §24/§25)

Run as 8 disjoint shards, each in its own `git worktree` (independent source tree
and lock file, so no two processes ever share a source file):

```
new mutations M-PC8..M-PC13
matrix       83/83 CAUGHT, survived: none
             (no missing id, no extra id, no contradictory result)
non-vacuity  83/83 ok (incl. NV-RB1/NV-RB2/NV-RB3)
restore      83 × bytes_match=True sha256_match=True
```

**Non-vacuity caught one inert mutation, which is exactly its job.** M-PC12 was
first written to replace the fixture's DB-separation *assertion*. That assertion
runs *after* the fixture has already been built, so collapsing the paths there
changed nothing any test could observe — the mutation was semantically inert and
no test could ever have killed it:

```
M-PC12: mutated GREEN (vacuous!)
```

It now replaces the adaptive **path assignment** itself, so the two databases
really do collapse onto one file and the fixture's own separation assertion fires:

```
M-PC12: baseline GREEN -> mutated RED  (ok)
M-PC12: CAUGHT  (API 夹具把 adaptive.DB_PATH 与 PAPER_DB_PATH 指向同一文件)
applied in place -> AssertionError: paper/adaptive DB 指向同一文件 ⇒ 数据库归属缺陷不可能被测出
restored byte-for-byte: OK
```

This is the "mutation aimed at a decision point that is not the one being
guarded" failure mode: the surviving mutation was a *harness* defect, not a weak
test. Two generations are kept so the correction is auditable (`gen2/` = the
vacuous reading, `gen3/` = the corrected one).

---

# Round-12: rebalance state cycle isolation

## Round-11 fixed database ownership, but rebalance state itself was still not cycle-owned

Round-11 answered *which database* owns rebalance state. It did not answer *which
cycle* owns it. The three state tables had no `cycle_id` at all, so:

```
paper ledger ownership correct  ≠  rebalance state ownership correct
```

## Before-fix reproduction (spec §2)

`work/r12_before_fix_repro.py` — deterministic fixture on the **unmodified**
Round-11 head, two physically separate SQLite files, quotes patched so no network
is touched, real handlers driven. Cycle 8 holds a scan (`quality_score=90`,
`fund_flow_trend=outflow`) and a `planned` plan (`sell_qty=100`); the active cycle
then moves to 9 and the same account/code is scanned again:

```
── cycle 9 scan 写下的行 ──
  quality_score             = 50.0
  prev_quality_score        = 90.0      ← read from cycle 8
  consecutive_outflow_days  = 5         ← cycle 8's five rows
  action                    = sell
  plans_created             = 1

  rebalance_scans 行数 before/after     = 5/5
  今日 (2026-09-19) A/X 的 scan 行数        = 1 (quality=[50.0])   ← cycle 8's same-day row was replaced
  cycle 8 plan #1 status                = planned -> verified
  get_pending_plans() 返回的 plan id    = [1, 2]                  ← includes cycle 8's plan
  rebalance_scans 列                    = [id, scan_date, ...]    ← no cycle_id
  rebalance_cooldown 列 / PK            = [code, account_id, sold_date, cooldown_until] / [code, account_id]

  BEFORE-FIX REPRODUCTION: PASS (7/7 claims)
```

| id | reproduced fact |
| --- | --- |
| C1 | `prev_quality_score` read cycle 8's 90 as cycle 9's baseline |
| C2 | `consecutive_outflow` counted cycle 8's rows |
| C3 | `get_pending_plans()` returned cycle 8's plan |
| C4 | `verify_all_plans()` rewrote cycle 8's plan to `verified` |
| C5 | same-day `UNIQUE(scan_date, account_id, code)` made cycle 9's scan replace cycle 8's row |
| C6 | neither `rebalance_scans` nor `rebalance_plans` had a `cycle_id` column |
| C7 | `rebalance_cooldown` PK was `(code, account_id)` — no cycle partition |

Cycle 9 reached `action=sell` and created a plan on a position whose only
"deterioration" came from cycle 8's state: the leak changes real decisions, not
just bookkeeping.

**Probe hygiene note.** The first version of this probe patched `PT.DB_PATH` but
not `adaptive.PAPER_DB_PATH`, which is what `api_adaptive._paper_rebalance_db()`
actually opens — so the scan ran against the real local ledger. The probe now
patches every path, asserts fixture/live separation, asserts the scan produced
new fixture rows, and snapshots both live ledgers before/after asserting zero
change (both report `unchanged = True`). The contamination was identified by row
timestamps and removed; both local ledgers were re-audited back to the state
Round-11 recorded (all three tables absent).

## The invariant

```
Every rebalance fact belongs to exactly one paper cycle.
```

Every scan, plan and cooldown carries `cycle_id`; a current-cycle decision reads
only rows with the same `cycle_id`. **Cycle N state never influences a cycle N+1
decision.**

## Schema: all three tables get `cycle_id` (spec §4–§7)

```
rebalance_scans.cycle_id     INTEGER
rebalance_plans.cycle_id     INTEGER
rebalance_cooldown.cycle_id  INTEGER
```

New rows must be non-NULL (enforced by a `BEFORE INSERT` guard). Legacy rows keep
`cycle_id = NULL` and are **never** backfilled from the current active cycle,
`MAX(paper_cycles.id)`, account binding or dates.

**Why a rebuild, not `ALTER TABLE ADD COLUMN`.** `rebalance_scans`' existing
`UNIQUE(scan_date, account_id, code)` is itself the cross-cycle defect: a same-day
rollover makes cycle 9's `INSERT OR REPLACE` overwrite cycle 8's row. The final
key must be `UNIQUE(cycle_id, scan_date, account_id, code)`. Likewise
`rebalance_cooldown`'s `PRIMARY KEY(code, account_id)` lets an old cycle's
cooldown suppress a new one, so it becomes `PRIMARY KEY(cycle_id, code, account_id)`.
Both are safe, idempotent table rebuilds: old rows preserved, `cycle_id = NULL`,
every other field carried over verbatim by explicit column name (never `SELECT *`).

`rebalance_plans` has no constraint change, so it takes the cheap path — a plain
`ADD COLUMN`.

**Migration idempotency.** Registered as v19 in `db_migrate.py`, and wired into
`init_db()`'s existing-ledger fast path — without that, production ledgers (which
return before the new-DB DDL block) would never receive the column or the new
unique contract. Repeated `ensure_schema` / migration runs are stable: the second
call returns all-`"ok"` and rewrites nothing.

## Scanner and API (spec §8–§17)

- `daily_close_scan(conn, accounts, quotes, cycle_id=...)` **requires** a cycle
  and fails closed (`NoActiveCycle`) when there is none — `cycle_id=None` is not a
  permissive fallback. No cycle ⇒ no scan, no state.
- One scan transaction resolves the cycle **once**, in the endpoint, and passes it
  down explicitly; no helper re-resolves the active cycle.
- `prev_quality_score` and `consecutive_outflow` are same-cycle only. A new
  cycle's first scan therefore has no prior baseline (change = 0) and no inherited
  outflow streak.
- `_is_risk_handled` adds `cycle_id=?` to all three order queries. Relying on
  `created_at >= today` is not a substitute for cycle identity: on a same-day
  rollover, cycle 8's morning sell would otherwise mark a brand-new cycle 9
  position `risk_handled` and silently skip it.
- `get_pending_plans(conn, cycle_id)` returns only that cycle; legacy NULL-cycle
  plans are never pending.
- `verify_all_plans(..., cycle_id)` is defense in depth: it validates
  `plan.cycle_id` against the requested cycle *and* updates with
  `WHERE id=? AND cycle_id=?`, failing closed when `rowcount != 1`. A caller
  cannot inject a stale plan by lying about its cycle.
- `/rebalance/verify` records `requested_cycle_id`, fetches quotes outside the
  transaction, re-resolves the cycle on reopen and fails closed with
  `cycle_changed_during_verify` (409) if it moved — a cycle 8 plan is never
  verified in cycle 9.
- `/rebalance/status` and `/rebalance/plans` expose only the current cycle's
  operational state. Recent cross-cycle history, if wanted, belongs in a separate
  future endpoint.

## Tests (spec §22–§26)

26 new tests in `backend/test_rebalance_cycle_scope.py`, driving the real route
functions:

| id | asserts |
| --- | --- |
| RB-C1 | a plan created by a cycle-8 scan carries `cycle_id=8`; scan and plan share one cycle |
| RB-C2 | cycle 8's plan is invisible to `get_pending_plans(cycle=9)`; legacy NULL plans are never pending |
| RB-C3 | a cycle-8 plan passed directly to `verify_all_plans(cycle=9)` is rejected and **not updated**; a forged plan whose `cycle_id` lies is still stopped by the UPDATE guard |
| RB-C4 | same-cycle verification works (positive control) |
| RB-C5 | cycle 8 `quality=90`, cycle 9 first scan `quality=50` ⇒ `prev_quality_score=50`, **not** 90 |
| RB-C6 | cycle 8's five outflows + cycle 9's own ⇒ streak `1`, **not** 5 |
| RB-C7/C8 | a cycle-8 verified hard-stop sell today does **not** mark cycle 9's position `risk_handled`; cycle 9's own does |
| status | `pending_plans` and recent scans are current-cycle only |
| race | cycle change during verify ⇒ fail closed, cycle 8's plan unchanged, 0 verified |
| rollover | both `cycle 8 / date / A / X` and `cycle 9 / date / A / X` coexist — no replace, no conflict |
| schema | `cycle_id` on all three tables; cooldown PK includes it; new NULL-cycle rows rejected; ownership immutable |
| migration | legacy rows preserved with `cycle_id NULL`; second run is a no-op; legacy rows are operationally invisible |

## Archive strategy (spec §19)

Chosen: **rebalance rows stay in the paper DB, partitioned permanently by
`cycle_id`.** They are therefore *not* added to `PURGED_TABLES` /
`LEDGER_TABLES` / `COUNTED_TABLES`, and no archive test changes. Every
operational query is cycle-scoped, which is what makes this safe.

## Mutation (spec §28)

Run as 8 disjoint shards, each in its own `git worktree`:

```
new mutations M-RC1..M-RC9   → 9/9 CAUGHT, survived: none
re-run M-PC8..M-PC13         → 6/6 CAUGHT (Round-11 kills preserved)
matrix                       → 15/15 CAUGHT
non-vacuity                  → 15/15 ok
restore                      → 15 × bytes_match=True sha256_match=True
anchor audit                 → PASS (93 anchors, each exactly once)
```

**Non-vacuity caught one vacuous mutation, and it was mine.** M-RC9 rewrites the
scanner's `_require_cycle_id` fail-closed guard, but I had designated the
*API-level* test `test_scan_without_active_cycle_creates_no_state`. The endpoint
has its own independent `cycle_id is None` check that fires *before* the scanner
is ever called — the two fail-closed layers are deliberate defense in depth — so
that test kept its verdict for a reason unrelated to the mutation:

```
M-RC9: 指名测试在变异后仍然全绿（空洞）
```

Re-aimed at `test_daily_close_scan_requires_cycle_id`, which calls the scanner
directly:

```
M-RC9: baseline GREEN -> mutated RED  (ok)
M-RC9: CAUGHT  (新 scan/plan 的 cycle_id 写成 NULL（无归属事实）)
```

This is the "mutation aimed at a decision point that is not the one being
guarded" failure mode. It is worth noting *why* it happened here: the defense in
depth that makes the production code correct is exactly what made a shallow
non-vacuity check pass. The anchor audit separately caught that M-PC10/M-PC11 had
stopped matching after the endpoints were rewritten; both were re-aimed and both
still kill.

## Migration on a real legacy schema (spec §21)

`work/r12_migration_legacy_check.py` builds a ledger with the **pre-Round-12**
schema (three tables, `UNIQUE(scan_date, account_id, code)`,
`PRIMARY KEY(code, account_id)`) plus one legacy row in each table, then runs the
migration:

```
旧 scans 唯一契约         = ('scan_date', 'account_id', 'code')
旧 cooldown 主键          = ('code', 'account_id')

第一次迁移 changes         = {'rebalance_scans': 'rebuilt', 'rebalance_plans': 'altered',
                            'rebalance_cooldown': 'rebuilt'}
新 scans 唯一契约         = ('cycle_id', 'scan_date', 'account_id', 'code')
新 cooldown 主键          = ('cycle_id', 'code', 'account_id')
legacy cycle_id 值        = {'scans': [(None,)], 'plans': [(None,)], 'cool': [(None,)]}
legacy scans 其它字段逐字保留 = True

第二次迁移 changes         = {'rebalance_scans': 'ok', 'rebalance_plans': 'ok',
                            'rebalance_cooldown': 'ok'}
```

| check | result |
| --- | --- |
| row count preserved (1/1/1 before and after both runs) | PASS |
| scans UNIQUE includes `cycle_id` | PASS |
| cooldown PK includes `cycle_id` | PASS |
| legacy other fields preserved verbatim | PASS |
| legacy `cycle_id` stays NULL | PASS |
| second run is a no-op | PASS |

## Backend / tooling

```
python -m unittest discover -s backend -p "test_*.py"
  Ran 3437 tests in 448.276s / OK (skipped=5) / exit=0
  (3411 + 26 new cycle-scope tests)
python -m ruff check backend     All checks passed!
python -m compileall -q backend  exit=0
```

## Frontend (spec §29)

```
node build.mjs                        build exit=0
git diff --exit-code -- dist          dist-clean exit=0
npm run test:unit                     pass 111 / fail 0
npx playwright test --project=chromium --workers=1
                                      32 passed / 0 failed
```

The Chromium suite is fully green on this head — the pre-existing strategy
workbench load flake recorded in Round-11 did not reproduce.

已检查前端消费链路，本 PR 无需前端修改。

## Security (spec §31)

```
--scope worktree   kinds: none   values: 0
--scope all        kinds: none   values: 0
```

`IMAGE_REVIEW | docs/assets/dashboard.png` is a pre-existing master file
(`0f9e655`, 2026-09-03) with no diff against `HEAD`; unchanged by this PR.
Production databases were audited read-only; nothing was copied, committed or
uploaded, and no paths or business rows are printed here.

## Non-goals / not touched

Strategy-parameter optimisation, return optimisation, AI tuning, T+1 rule
changes, risk-threshold changes, ranking changes, Shadow authority and deployment
are all out of scope. `tradability_position_evidence`, historical quantity replay,
the Historical Tradability Archive and the Shadow historical denominator are
untouched.

## Known evidence gaps

- Current production data has `mirror-only = 0`, so the "stale mirror" scenario is
  proven by the deterministic fixture, not by a live row.
- `adaptive_engine` remains shadow/advisory; switching its input does not give it
  execution authority, and the tests assert `mode == "shadow"`.
- The **server-side** production audit (the deployed `paper_trading.sqlite3` /
  `adaptive_learning.sqlite3`) was not re-run this round: no SSH credential was
  available in the working session. The row-count audit above is from the local
  `data_cache` pair, which shows the same ownership split. The deployed server
  remains on `0c1546a` and is **not** affected by this PR until it is deployed.
- `rebalance_cooldown` has no read/write call sites anywhere in the repo (verified
  by repo-wide search): it is dead code. It is kept, not deleted, but its schema is
  cycle-safe so that reconnecting it later cannot leak an old cycle's cooldown into
  a new cycle.

## Codex review

Codex review unavailable due to usage limit.

## CI

Exact-head checks — see the checks section of this PR.

---

# Round-13: rebalance operational status freshness

## Defect

Round-12 made persisted rebalance state cycle-owned, but
`GET /api/adaptive/rebalance/status` still returned a process-local
30-second cache entry **before** resolving the current active paper cycle.

Therefore a same-day cycle rollover could produce:

```
cycle 8 status cached
-> cycle rolls to 9
-> immediate GET status
-> stale cycle 8 operational state returned
```

The same cache also hid same-cycle scan/plan writes until TTL expiry, and it
bypassed the `no_active_cycle` fail-closed path entirely (returning a cached
`200` with the old `cycle_id` instead of `409`).

```
HTTP operational view  !=  authoritative current cycle
```

## Before-fix reproduction (on the unmodified head `4fa2162`)

`work/r13_before_fix_repro.py` -- deterministic, temp SQLite only, no sleep, no
network, no production `data_cache`, quotes patched. All three claims
**REPRODUCED (3/3)**, exit 0, both live ledgers asserted `unchanged = True`:

```
R13-C1 REPRODUCED  同日 cycle 翻转后立即 GET 仍返回旧周期 cached status
        GET #1 cycle_id=2 recent_scans=['OLD8']
        db active cycle = 3        <- already rolled over
        GET #2 cycle_id=2 recent_scans=['OLD8']   <- stale
R13-C2 REPRODUCED  同周期 scan 之后立即 GET 仍返回 scan 之前的 view
        POST /rebalance/scan -> db_scans=['OLD8'] db_pending=1
        GET #2 recent_scans=[] pending=0          <- invisible
R13-C3 REPRODUCED  无 active cycle 时立即 GET 仍返回旧周期 cached 200
        db active cycle = None
        GET #2 http=200 cycle_id=2                <- fail-closed bypassed
```

## Fix

`/rebalance/status` is no longer cached. Every request now:

```
paper ledger
-> resolve current active cycle (read-only)
-> read cycle-scoped rebalance state
-> return
```

`_cache_get("rebalance_status", ...)` and `_cache_set("rebalance_status", ...)`
are both removed. No cache-invalidation protocol is introduced -- keying by
`cycle_id`, clearing after scan/verify, or clearing on rollover are all **second
authorities** that reintroduce the same defect class. The endpoint reads a small
amount of local SQLite operational state (`rebalance_scans` / `rebalance_plans`,
`LIMIT 10` plus pending), so no caching is warranted.

The generic cache framework is **untouched**: `_cache`, `_cache_ts`,
`_cache_get`, `_cache_set`, `_cache_clear` are still used by `dual_ai_status`
and `evolution_status`. Only the `rebalance_status` coupling was removed.

Unchanged: HTTP path, success response schema, `no_active_cycle` 409 semantics,
paper/adaptive DB ownership, and the `scan` / `verify` / `plans` endpoints.

## Regression

R13-C1: cycle rollover cannot return previous-cycle status
R13-C2: same-cycle scan is visible immediately
R13-C3: no active cycle cannot return a cached historical status

Three new tests in `backend/test_rebalance_cycle_scope.py`, class
`RB_C_StatusFreshness`, all driving the **real handler** -- no sleep, no manual
cache clearing, no direct `API._cache` manipulation:

```
test_rebalance_status_does_not_leak_previous_cycle_after_rollover
test_rebalance_status_reflects_same_cycle_scan_immediately
test_rebalance_status_no_active_cycle_cannot_return_cached_old_cycle
```

Cycle 8 and cycle 9 use deliberately different state (`OLD8`/`NEW9`, quality
90/50) so the tests cannot pass by merely checking the `cycle_id` field while
internal state mixes.

## Mutation

`M-RC10` restores the complete defect (read **and** write under one fixed key).
`M-RC11` installs the per-cycle-key cache that spec section 4 explicitly forbids,
which still hides same-cycle writes until TTL expiry.

**Non-vacuity caught two inert mutations of mine, which is exactly its job.**
The first generation added only the cache *read* (M-RC10) or only the *write*
(M-RC11); each is a no-op -- an empty cache is never hit, and a written entry is
never consumed. Both came back `UNDETECTED`:

```
M-RC10: UNDETECTED   M-RC11: UNDETECTED   (gen1)
```

They now span the whole endpoint body as one contiguous anchor and both are
killed by the freshness tests:

```
M-RC10: CAUGHT  (status 重新引入固定 key 的 30 秒 cache（读+写；旧周期快照泄漏）)
M-RC11: CAUGHT  (status 改成按 cycle 分键的 30 秒 cache（同周期写入在 TTL 内不可见）)
```

Both generations are retained so the correction is auditable.

## Invariant

Operational rebalance status is always derived from the current active
paper cycle at request time.

## Final invariants

```
paper_position_lots is the current executable position authority.

Rebalance state is paper-ledger state,
but it is also cycle-owned state.

A scan, plan, cooldown, prior-quality baseline,
outflow streak and risk-coordination decision
must never cross a paper-cycle boundary.

A plan created in cycle N must never be verified
or acted on in cycle N+1.

Legacy rebalance rows with unknown cycle ownership
must remain unknown and operationally invisible.

adaptive_learning.sqlite3 must never own rebalance state.

The rebalance engine must read paper facts from the paper ledger.

A rebalance scan, its risk checks, its current positions,
and its persisted rebalance state must share one coherent
paper-ledger transaction.

A green unit suite is not proof of database ownership
unless the test uses two physically separate SQLite files.

paper_positions is never evidence that a position is currently held.

Historical Tradability Archive remains zero-authority.

Position-aware Shadow remains observation-only.
```

