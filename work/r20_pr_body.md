# fix(execution): converge sell fills on one commit boundary

## Base

- PR #176: `MERGED`
- Base commit: `61399bf4d8dc6da4ced32ccf644a514a9b5a9c26`
- Branch: `codex/sell-fill-commit-convergence`

## Before-fix

Before-fix reproduction was run on the R20 base (`61399bf`) before any production edit:

```text
R20-C1 risk sell bypasses centralized fill commit: REPRODUCED
    fills=1 commit_fill_calls=0
R20-C2 intraday sell bypasses centralized fill commit: REPRODUCED
    fills=1 commit_fill_calls=0 reason=高抛成交
R20-C3 multiple runtime paper_fills writers: REPRODUCED
    paper_trading=['_intraday_sell', '_monitor_risk_impl'] execution_planner=['commit_fill']
R20-C4 intraday sell failure leaves partial ledger: REPRODUCED
    fills 1->2 lots 100->0 cash_delta=1098.24 filled_orders=1
R20 before-fix reproduced: 4/4
```

After the fix, the same probe reports `0/4` for the bypass/partial-ledger cases.

## Authority after R20

```text
BUY commit:
execution_planner.commit_fill

manual/deferred commit:
execution_planner.commit_fill

risk SELL commit:
execution_planner.commit_fill

intraday/opening-event SELL commit:
execution_planner.commit_fill
```

`paper_trading._monitor_risk_impl` and `paper_trading._intraday_sell` now only:

```text
decision / quantity / price / pending order / SAVEPOINT / failure-retry handling
```

They no longer perform lot consumption, cash credit, `paper_fills` INSERT, execution verification stamping, episode finalization, or duplicate risk/audit logging.

## SELL invariants

```text
cycle provenance      PASS
order identity        PASS
T+1 lot authority     PASS
cash credit           PASS
realized pnl          PASS
episode finalize      PASS
execution verified    PASS
risk/audit exactly 1  PASS
```

Additional behavior-preserving details:

- `commit_fill` SELL keeps the order durable `cycle_id` for FIFO lot consumption; it never resolves the active cycle.
- `PPRS.finalize_sell` is called only by `execution_planner.commit_fill`.
- Risk partial take-profit passes `sell_next_take_stage`; intraday/manual SELL passes `None`, so partial sells preserve the existing stage.
- Full exits are decided by the finalizer from authoritative `paper_position_lots`, not by caller-local `position.qty`.
- Paused/archived out-of-cycle accounts retain their existing risk-exit capability when the order cycle equals the active cycle and the order is a SELL; unknown / legacy NULL / identity mismatch / cross-cycle orders still fail closed.
- The legacy execution-verification wiring guard now expects only `execution_planner.commit_fill` and the `demo_seed` fixture writer as `paper_fills` write owners.

## Tests

```text
Targeted suite:
398 tests OK

Full backend:
3805 tests OK (skipped=5)

Frontend:
npm --prefix frontend run build: PASS
npm --prefix frontend test --if-present: PASS
dist drift: 0

Syntax / quality:
ruff check backend: All checks passed
python -m compileall -q backend: exit 0
```

## Mutation

```text
M-EXE1..M-EXE18
18/18 RED
survived=0
restore bytes PASS
restore sha256 PASS
```

## Ratchet

```text
before:
16046 / 282

after:
16045 / 282

delta:
-1 LOC / 0 defs
```

The R20 hard gate is met (`LOC < 16046`, `defs <= 282`). The strong target `LOC <= 16010` was not met; the remaining size is retained for readability and existing application orchestration, and R21 is the next step for extracting `_monitor_risk_impl`.

## Security

```text
kinds: none
values: 0
```

The repository security scanner reported `kinds: none / values: 0`; two pre-existing image assets were listed for manual review only.

## Merge status

```text
Merge: NOT MERGED
Deploy: NOT DEPLOYED
```
