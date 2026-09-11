# A-Share Paper Trading & Quantitative Research Engine

[中文 README](README.md)

[![CI](https://github.com/daviesjoin-afk/astock-paper-trading/actions/workflows/ci.yml/badge.svg)](https://github.com/daviesjoin-afk/astock-paper-trading/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

A local-first **A-share paper-trading and strategy platform**. It models market-specific execution constraints, layered risk controls, multi-source quote validation, replayable audit trails, and strategy-isolated accounting without connecting to a broker or touching real funds — and it runs both built-in strategy templates and user-defined strategies written in a declarative DSL.

The project is intended as reusable research infrastructure for developers who need to test A-share strategies under realistic execution rules rather than a generic backtest that assumes every order can always fill.

## What this is: built-in templates plus declarative custom strategies

The repository is no longer "five fixed strategies". It is a strategy platform:

- **Built-in templates (`origin=builtin`)**: five audited models that serve as runnable baselines.
- **User strategies (`origin=user`)**: defined in a **declarative DSL** — no Python, no uploaded code, no spawned process.
- Both travel the **same** pipeline: immutable version → DSL compiler → RuntimeContext → risk profile → signal → OrderIntent → allocation and sizing → execution planner → system risk gates → order/fill/position → performance and evolution.

Platform capabilities: immutable **strategy versions**, a **lifecycle** state machine, a pinned **RuntimeContext**, **risk compilation** (strategies can only tighten), **dynamic allocation** over a shared capital pool, a central **OrderIntent / Execution Planner**, **T+1-aware execution**, **deterministic replay**, **strategy evolution / Champion-Challenger**, and the browser-based **Strategy Workbench**.

See [strategy platform](docs/STRATEGY_PLATFORM.md), [evolution architecture](docs/EVOLUTION_ARCHITECTURE.md) and [architecture notes](ARCHITECTURE.md).

## Six platform semantics you must know

1. **Custom strategies do not run Python.** A user strategy is declarative DSL evaluated by the platform's allowlisted compiler and offline evaluator.
2. **A strategy cannot specify the final order quantity.** `qty` / `shares` / `amount` fields belong to the executor; supplying them rejects the signal. Size comes from cash, risk budget, exposure, industry and board-lot constraints.
3. **Activating a strategy does not add it to the current cycle.** `active` only determines eligibility for the **next** cycle.
4. **Current-cycle participants come from a cycle snapshot** frozen at cycle creation, intersected with the accounts bound to that cycle, minus lifecycle-`paused` strategies.
5. **Zero enabled strategies is a valid Idle mode.** No new signals are produced; risk scanning, existing-position exits and scheduling continue.
6. **The cycle owns economic capital; lifecycle only controls execution permission.** `pause` removes execution participation immediately but does **not** delete that strategy's economic ownership in the current cycle; `resume` only restores execution permission and never inflates capital.

> A strategy declares intent. The platform decides whether it may act, how much it may deploy, and how the order is executed.

## Why this project exists

Many open-source trading simulators are built around US-market or generic assumptions. China A-shares have execution rules that materially change whether a strategy is actually tradable. This project makes those rules first-class constraints instead of post-processing adjustments.

Key examples:

- **T+1 stock settlement**: shares bought today cannot be sold today.
- **100-share board lots** for ordinary stock orders.
- **Daily price-limit and suspension gates**: limit-up, limit-down and suspended securities are handled explicitly.
- **Fees and slippage** are validated in the execution path.
- **Fail-closed market data**: stale, missing or insufficiently covered quotes block simulated execution instead of silently falling back to old prices.
- **Replayable decisions**: signals, risk decisions, orders, fills, NAV and scan results are persisted so historical behavior can be audited round by round.

This makes the repository useful not only for strategy experiments, but also for studying execution correctness, data-quality failure modes, concurrency safety and reproducible risk decisions in an A-share environment.

## Strategies: built-in templates and custom strategies

The repository preserves five **built-in strategy templates** in one registry alongside user-defined strategies, and exposes the same identities to adaptive research, replay and audit views. Built-in templates are the platform's baselines, not the whole product: clone one and edit the copy, or create a declarative strategy from scratch in the Strategy Workbench.

| Built-in template | Status | Style | Purpose |
|---|---|---|---|
| `tq_breakout` | active | Momentum breakout | Volume/flow-confirmed short-horizon breakout candidates |
| `trend_pullback` | active | Trend pullback | Mid-term pullback observations inside an established uptrend |
| `sector_rotation` | active | Sector rotation | Sector heat, flow resonance and relative-strength rotation |
| `reported_profit_breakout` | active | Quality breakout | Disclosure- and earnings-driven breakout scoring and paper execution |
| `main_force_top10` | active | Main-fund flow | Candidates ranked by strong main-fund inflow and live confirmation |

**Who actually participates in a scan?** Not "whatever is active in the registry". The authoritative rule is the **current cycle snapshot**:

```text
participants = enabled_strategies frozen at cycle creation
             ∩ accounts bound to that cycle
             − strategies whose lifecycle is paused
```

Activating a strategy therefore only makes it eligible for the **next** cycle; a running cycle is never rewritten, and clearing the enabled set is a legal Idle mode. User strategies start in the **pilot** capital stage (25% of their budget) and are promoted manually after validation.

Built-in templates and custom strategies share execution, capital-allocation and audit infrastructure while keeping independent entry lanes, position limits and exit logic. None is silently deleted, renamed or replaced. Existing historical cycles are not silently rebalanced.

The engine is **paper trading only**. It does not include broker routing, leverage, short selling or real-money execution.

### Platform capabilities

| Capability | Notes |
| --- | --- |
| Declarative DSL | Allowlisted nodes/fields/indicators, no code-execution path, offline fail-closed evaluation |
| Immutable versions | Every change appends a version + structure checksum; a cycle pins the version it used |
| Lifecycle | draft / validated / active / paused / retiring / archived with validated transition edges |
| RuntimeContext | One pinned contract per version: DSL, risk profile, execution profile, lifecycle stage |
| Risk compilation | Fingerprint → profile → tighten-only merge (`min(production, template)`) with per-key audit |
| Dynamic allocation | N-strategy shared pool; total allocation never exceeds the pool cap |
| OrderIntent / Execution Planner | Strategies express intent only; planning, revalidation and commit are centralised |
| Deterministic replay | Signal → risk → order → fill → NAV evidence, compared byte-for-byte by golden replay |
| Evolution | Evidence → proposal → A/B validation → asymmetric risk gate → shadow challenger → promotion |
| Strategy Workbench | Definition, DSL, preview, versions, lifecycle and cloning, covered by browser E2E |

## Architecture highlights

### Execution and capital model

- Shared capital pool with strategy-level budget attribution and position-slot limits.
- The dust-order threshold is dynamic: `cycle capital × shared-pool exposure cap ÷ stock position limit × 60%`, rounded down to ¥100. A ¥100,000 cycle with an 82% cap and 15 slots therefore uses ¥3,200 instead of a fixed ¥10,000. The remaining 40% is reserved for risk-controlled adds after trend, drawdown and position checks.
- Reservation and cash deduction are separated to reduce double-spend risk under concurrent scans.
- SQLite savepoints protect order accounting during multi-step writes.
- Position sizing is price-aware and validates lot size, slippage and tradeability before simulated fills.

### Layered risk state machine

- Structured `approved`, `rejected`, `deferred_capacity` and `downside_warning` decisions.
- Downside protection with staged reduction, confirmation and full-exit paths.
- Hard stops, trailing stops, staged profit-taking and quality-based rotation.
- Stable reason codes and audit records make exit decisions replayable instead of relying only on human-readable text.

### Market-data quality

- Multiple public quote sources are cross-checked.
- Quote freshness is explicit; cached historical prices are not relabeled as live data.
- Full-market snapshots use a coverage gate before they can drive formal scans.
- Provider failure degrades or blocks the relevant path instead of relaxing safety constraints.

### Concurrency and deterministic execution

- Runtime lease + heartbeat + fencing-token protection ensures a single active writer for scheduled paper-trading slots.
- Expired workers can be reclaimed without allowing stale writers to overwrite newer state.
- Entry timing is modeled as a state machine so a single transient tick does not immediately create an order.

## Quick start

Requirements: **Python 3.11+**. Docker is optional.

### Windows

```powershell
.\start.ps1

# Force local Python mode
.\start.ps1 -Local

# Force Docker Compose
.\start.ps1 -Docker
```

You can also double-click `start.bat`.

### Linux / macOS

```bash
chmod +x start.sh
./start.sh
```

The startup scripts launch the API/Web dashboard but do not automatically create a paper-trading cycle.

For the complete clone → dependency install → dashboard → data bootstrap → scan workflow, see [`docs/RUNBOOK.md`](docs/RUNBOOK.md). For the module map (what each file belongs to), see [`docs/REPOSITORY_LAYOUT.md`](docs/REPOSITORY_LAYOUT.md).

## Manual development workflow

```bash
python -m venv .venv
# Activate the virtual environment for your platform
python -m pip install -r requirements.lock

python -m uvicorn backend.main:app --port 8600
python -m unittest discover -s backend -p "test_*.py" -v
```

Trigger one paper-trading slot manually:

```bash
cd backend
python paper_runner.py --slot open
```

Supported slots include `auction`, `open`, `risk`, `intraday`, `close` and `weekly-review`.

## Docker

```bash
docker compose up -d --build
docker compose logs -f app
```

Open `http://localhost:8600`.

The repository uses instance-specific Docker volumes and does not ship runtime databases, real holdings, credentials or host-specific deployment configuration.

## Validation and maintenance

- Backend regression tests run in GitHub Actions on Python 3.11 and 3.12, together with Ruff, lock-file consistency, pip-audit, frontend build consistency, **Playwright browser E2E** (Strategy Workbench journeys) and a Docker smoke job.
- The repository contains dedicated tests for execution guardrails, point-in-time data behavior, risk auditing, concurrency leases, strategy-entry constraints, strategy-platform invariants (version immutability, draft hard-delete boundary, cycle ownership, dynamic-allocation properties) and replay-related behavior.
- Releases and maintenance history are tracked through [GitHub Releases](https://github.com/daviesjoin-afk/astock-paper-trading/releases) and [`CHANGELOG.md`](CHANGELOG.md).
- Bugs, reproducible edge cases and focused pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

Current public roadmap items include:

- [Market-data adapters and failure degradation](https://github.com/daviesjoin-afk/astock-paper-trading/issues/1)
- [Pluggable strategy interface and replay contract](https://github.com/daviesjoin-afk/astock-paper-trading/issues/2) (the strategy platform and declarative DSL have landed; the issue tracks the remaining gaps)
- [Expanded execution/audit replay validation](https://github.com/daviesjoin-afk/astock-paper-trading/issues/3)

## Optional LLM-assisted research

The base engine does not require an LLM. Optional advisory/observation features can be enabled through `.env` using the documented placeholders in `.env.example`. Secrets are intentionally excluded from the repository.

LLM-assisted observations are kept separate from the formal paper-execution path and are not treated as guaranteed trading signals.

## Security and privacy boundary

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/daviesjoin-afk/astock-paper-trading/security/advisories/new). Do not place sensitive reproduction details in a public issue.

This repository intentionally excludes:

- broker credentials or broker integrations;
- real-money account information;
- runtime trading databases and private holdings;
- API keys and `.env` secrets;
- host-specific server credentials and deployment paths.

Please do not include real account data, credentials or non-sanitized production screenshots in issues or pull requests.

## Disclaimer

This project is research infrastructure for **simulated trading only**. Market data comes from public interfaces and does not represent full exchange order-book depth. Simulated fills, strategy results and historical observations are not guarantees of future performance and do not constitute investment advice.

## License

MIT. See [`LICENSE`](LICENSE).

## Release and deployment status

Current release: **v1.3.0** (see [GitHub Releases](https://github.com/daviesjoin-afk/astock-paper-trading/releases); the detailed [`CHANGELOG.md`](CHANGELOG.md) entries currently stop at v1.2.0). See [security boundaries](SECURITY.md), the [strategy platform](docs/STRATEGY_PLATFORM.md) and the [repository layout](docs/REPOSITORY_LAYOUT.md). CI covers Python 3.11/3.12. The historical API version 2.0.0 is not the release tag.

The local Compose mapping binds the host port to loopback only (`127.0.0.1:8600:8600`); inside the container Uvicorn still listens on `0.0.0.0` for port publishing, health checks and reverse proxying.

The HTTP control plane enforces a unified operator boundary (PR-2): `POST`/`PUT`/`PATCH`/`DELETE` require an operator token, while `GET` is read-only and needs no credential. **When no token is configured, write endpoints fail closed with 503** — the read-only dashboard keeps working. The token is sent via the `X-Operator-Token` header (or the standard `Authorization` header using the Bearer scheme), never in the URL. `confirmed=true` is a product confirmation step, not authentication. See [SECURITY.md](SECURITY.md) for configuration and the full threat model.
