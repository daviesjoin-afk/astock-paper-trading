# Maintainers

## Primary maintainer

- [@daviesjoin-afk](https://github.com/daviesjoin-afk) — project direction, architecture, releases, execution/risk semantics, reviews and maintenance.

## Maintainer responsibilities

The primary maintainer is responsible for keeping the public paper-trading boundary explicit and reproducible, including:

- reviewing changes to execution, T+1, lot-size, price-limit and suspension rules;
- maintaining fail-closed quote freshness and market-data quality behavior;
- reviewing risk-state, shared-capital, concurrency and audit changes;
- maintaining CI, releases, documentation and issue triage;
- preventing credentials, real holdings, runtime databases and host-specific private information from entering the public repository.

## Contributions

Focused external contributions are welcome. Good first areas include reproducible bug reports, market-data adapters, failure-mode regression tests, documentation, replay tooling and small strategy-interface improvements.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
