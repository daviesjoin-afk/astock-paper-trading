# Third-party notices for `astock_public/_upstream`

This directory contains a documented publication derivative of:

- Project: `simonlin1212/a-stock-data`
- Version: `3.10.4`
- Commit: `f21534946c42be3ad048ac938eecb1bad84cb8ab`
- License: Apache License 2.0
- Source: <https://github.com/simonlin1212/a-stock-data>
- Original upstream `SKILL.md` SHA-256: `304e7cc270b595c3031b3c9bd5431d6617ae684b66396092bebbc9f8622f60a6`
- Publication derivative SHA-256: `072ca0857c48212d7220a97744ab2f7dcc86b7d622e1139de44df954ba49c799`
- Runtime verifies the derivative; attribution and refresh verification retain
  the original digest independently.
- Personal contact addresses are replaced by a publication marker. Copyright
  names, Apache-2.0 license and the upstream source URL are preserved.
- Fixed TCP peer addresses are removed. The existing mootdx discovery fallback
  remains the default; explicit peers use `ASTOCK_TDX_SERVERS` JSON pairs.
- Bare single-letter mock URL hosts in `test_v39_sources.py` use reserved
  example URLs, preserving distinct sources without real host metadata.
- `../refresh_upstream.py` recreates these changes from the exact original
  source with default read-only comparison or an explicit new overlay directory;
  the repository-root `scripts/update_astock_public_snapshot.py` refreshes the
  same pinned files and prints both integrity digests.

A full copy of the upstream Apache-2.0 license is in `LICENSE`. The upstream
README, changelog, integration notes, tests and fixtures are retained so the
exact source contracts and offline regression evidence remain auditable.

## Modifications in this integration

- The package never executes `SKILL.md` verbatim. `source_snapshot.py` parses
  every embedded Python block and retains only imports, definitions and pure
  constants; top-level demos, network calls, self-tests and filesystem writes
  are skipped.
- `requests`, `urllib.request` and `urllib` are dependency-injected shims.
  A project `SourceHttp` can own caching, validation, identity, retries and
  as-of restrictions; without an injected transport, egress fails closed.
- Missing optional packages are reported per capability and do not disable the
  package or unrelated capabilities.
- Temporal restrictions, source credentials, market exclusions and observed
  blockers are declared in `registry_data.py`; a one-time timeout is never
  recorded as permanent unavailability.

- The old `stock_fund_flow_120d` parser's `-` sentinel is changed to `None`
  during AST adaptation, rather than being presented as a measured zero.
- Requests-compatible HTTP errors retain status codes (including an allowed
  404), and import-time transport injection is serialized across threads.
- Client-method examples for mootdx finance/F10/legacy market data are exposed
  as materialized queries; query-owned TCP connections are disconnected.
- Business-effective dates and historical reporting periods are not labeled
  as proof of historical first-seen versions; the common SDK freezes successful
  semantic results and does not cache failed/incomplete results as successes.

Code licenses do not grant redistribution or commercial rights to underlying
market data, announcements, reports or PDFs. Source identity and each provider's
website terms remain separate. No upstream endorsement is implied.
