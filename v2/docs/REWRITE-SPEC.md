# V2 Rewrite Spec

## Product

One user-funded paper account. The user enters principal only. The server
creates two strategy slots, always named:

- `steady` / 稳健
- `aggressive` / 激进

Initial weights are 50% and 50%. There is no experiment picker, no second
experiment, and no manual weight input.

## Trading loop

Each trading day, for each slot:

1. Read the latest reviewed close price set for the account's market-data pack.
2. Read deterministic signals from the slot's strategy over that price window.
3. Generate target orders subject to the slot budget and risk limits.
4. Execute at that day's reviewed close with T+1 and whole-lot rules.
5. Persist decisions, fills and the account snapshot in one SQLite transaction.

No live broker, no intraday quote, no real-money order path.

## Strategy split

### Steady / 稳健

- 20-day moving average above 60-day moving average;
- 20-day volatility below its 60-day median;
- at most 3 positions;
- 15% single-symbol target, 10% cash floor.

### Aggressive / 激进

- 5-day momentum positive;
- 20-day breakout above the prior 20-day high;
- at most 3 positions;
- 30% single-symbol target, 5% cash floor.

Both strategies use the same deterministic close-price input and the same
execution rules. Their differences must be explainable from the inputs above.

## Allocation evolution

After each completed trading day, compute each slot's equity from reviewed
closes. Rebalance only when all of these hold:

- at least 2 completed days since the previous rebalance;
- both slots have a valid equity series;
- target weights stay in [20%, 80%];
- one rebalance moves at most 10 percentage points;
- total weights remain 100%.

The score is `return_bps - drawdown_bps`. Each applied rebalance records the
day, before/after weights, moved cash and a human-readable reason. If there is
no completed-day evidence, or the source slot has not enough free cash, the
account keeps its current weights and does not invent a transfer.

## Data source

`a-stock-data` is used only by the offline import command. The Rust service
never calls the network.

The importer:

- fetches unadjusted daily bars;
- cross-checks the close and volume against a second public source;
- checks adjusted-factor changes;
- writes a reviewed price-set artifact only when every check passes;
- writes an audit report next to the artifact.

## API

Only these routes are required:

```text
GET  /v2/health
GET  /v2/account
POST /v2/account
POST /v2/account/run
GET  /v2/account/positions
GET  /v2/account/trades
GET  /v2/account/rebalances
GET  /v2/prices/coverage
```

There is no experiment, replay, research-reproduction or evolution-event API.

## UI

One page, one column on mobile, two columns on wide screens:

- header: account principal, total equity, profit, cash;
- strategy cards: 稳健 and 激进, with weight, equity, return and drawdown;
- positions table;
- recent trades;
- capital-allocation history;
- one primary button: “推进到最新交易日”.

Every missing fact renders as “—”, never as zero or an estimated value.

## Verification

- Rust unit tests for strategy signals, T+1, whole lots, allocation bounds.
- HTTP tests for the routes above.
- Browser test for creating an account and rendering both strategy cards.
- Data-source tests run offline with fixture payloads.
- A full run is only called successful when it uses a reviewed price set and
  produces a persisted fill or an explicit rejection reason.
