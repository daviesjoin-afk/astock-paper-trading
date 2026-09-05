# A-Share Paper Trading & Quantitative Research Engine

[中文 README](README.md)

[![CI](https://github.com/daviesjoin-afk/astock-paper-trading/actions/workflows/ci.yml/badge.svg)](https://github.com/daviesjoin-afk/astock-paper-trading/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

A local-first **paper-trading and quantitative research engine for China A-shares**. It models market-specific execution constraints, layered risk controls, multi-source quote validation, replayable audit trails, and strategy-isolated accounting without connecting to a broker or touching real funds.

The project is intended as reusable research infrastructure for developers who need to test A-share strategies under realistic execution rules rather than a generic backtest that assumes every order can always fill.

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

## Current scope

The repository preserves five strategy definitions in one registry and exposes the same identities to adaptive research, replay and audit views. All five strategy accounts are active in a new paper cycle, with independent candidate lanes, risk profiles and scheduler time inside the shared capital pool.

| Strategy | Status | Style | Purpose |
|---|---|---|---|
| `tq_breakout` | active | Momentum breakout | Volume/flow-confirmed short-horizon breakout candidates |
| `trend_pullback` | active | Trend pullback | Mid-term pullback observations inside an established uptrend |
| `sector_rotation` | active | Sector rotation | Sector heat, flow resonance and relative-strength rotation |
| `reported_profit_breakout` | active | Quality breakout | Disclosure- and earnings-driven breakout scoring and paper execution |
| `main_force_top10` | active | Main-fund flow | Candidates ranked by strong main-fund inflow and live confirmation |

All five active strategies share execution, capital-allocation and audit infrastructure while keeping independent entry lanes, position limits and exit logic. Existing historical cycles are not silently rebalanced; a new or reset cycle allocates capital evenly across all five definitions. None is silently deleted, renamed or replaced.

The engine is **paper trading only**. It does not include broker routing, leverage, short selling or real-money execution.

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

For the complete clone → dependency install → dashboard → data bootstrap → scan workflow, see [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Manual development workflow

```bash
python -m venv .venv
# Activate the virtual environment for your platform
python -m pip install -r requirements.txt

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

- Backend regression tests run in GitHub Actions on Python 3.11 and 3.12.
- The repository contains dedicated tests for execution guardrails, point-in-time data behavior, risk auditing, concurrency leases, strategy-entry constraints and replay-related behavior.
- Releases and maintenance history are tracked through [GitHub Releases](https://github.com/daviesjoin-afk/astock-paper-trading/releases) and [`CHANGELOG.md`](CHANGELOG.md).
- Bugs, reproducible edge cases and focused pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

Current public roadmap items include:

- [Market-data adapters and failure degradation](https://github.com/daviesjoin-afk/astock-paper-trading/issues/1)
- [Pluggable strategy interface and replay contract](https://github.com/daviesjoin-afk/astock-paper-trading/issues/2)
- [Expanded execution/audit replay validation](https://github.com/daviesjoin-afk/astock-paper-trading/issues/3)

## Optional LLM-assisted research

The base engine does not require an LLM. Optional advisory/observation features can be enabled through `.env` using the documented placeholders in `.env.example`. Secrets are intentionally excluded from the repository.

LLM-assisted observations are kept separate from the formal paper-execution path and are not treated as guaranteed trading signals.

## Security and privacy boundary

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
