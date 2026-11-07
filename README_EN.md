# AStock V2 — independent paper-only kernel

This repository now contains **one application**: AStock V2 under `v2/`. The legacy V1 application (`backend/`, `frontend/`, `deploy/`, `plans/`, root `docs/` and the old Python/Docker entry points) was removed from the current branch; the original code is preserved by an archive tag (see [the V1 retirement record](docs/V1_RETIREMENT.md)).

V2 is a **local, offline research kernel**, not a deployable public service:

- one Cargo package, one Rust service on `127.0.0.1:8765`, and one SQLite writer (`store/`);
- Python produces offline research signals only and never touches the ledger;
- TypeScript renders read-only views;
- every price, fill level and capacity is a synthetic offline fixture — no broker, no market-data vendor, no real money, no V1 compatibility layer.

## Quick start

```bash
cd v2
cargo test --locked
cargo build --release --locked
cargo run --locked

# from the repository root, in another terminal
python3 -m unittest discover -s v2/research -p 'test_*.py' -v
bash v2/scripts/smoke.sh
```

## Where to read next

- [v2/README.md](v2/README.md) — scope, data boundaries and existing capabilities
- [v2/docs/MODULE_BOUNDARIES.md](v2/docs/MODULE_BOUNDARIES.md) — module ownership and invariants
- ADR 001/002/003 in [v2/docs](v2/docs) — experiment ownership, versioned market packs, reproducible research artifacts
- [docs/V1_RETIREMENT.md](docs/V1_RETIREMENT.md) — archive tag and restore procedure
- [SECURITY.md](SECURITY.md) — security boundary and the sensitive-data scanner
