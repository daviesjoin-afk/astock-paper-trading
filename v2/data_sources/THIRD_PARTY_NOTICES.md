# Third-party notices

## a-stock-data

This adapter contains logic adapted from:

- Project: `simonlin1212/a-stock-data`
- URL: https://github.com/simonlin1212/a-stock-data
- Version: `3.10.4`
- Commit: `f21534946c42be3ad048ac938eecb1bad84cb8ab`
- License: Apache License 2.0

The Tencent daily K-line parsing, Tongdaxin after-hours package parsing and
Sina qfq/hfq adjust-factor parsing in `a_stock_data.py` follow that project's
documented endpoint formats and data caveats. This adapter does not copy the
entire upstream skill file and does not claim upstream endorsement.

The upstream project is licensed under the Apache License, Version 2.0. A copy
of that license is available at:

https://www.apache.org/licenses/LICENSE-2.0

Modification note (2026-11-12): the existing adapters now use the project's
stdlib HTTP/snapshot-cache owner; original parsing/cross-check and reviewed-set
boundaries remain. `public_data.py` exposes on-demand queries without embedding
an upstream framework.

## Reference-data endpoint research

The SZSE, Eastmoney and CNINFO adapters in `company_disclosures.py` are independent
stdlib implementations of the public response formats and small samples recorded
in [the source investigation](../docs/MARKET-SOURCES-RESEARCH-2026-11-12.md).
The investigation also compares AKShare (MIT), efinance (MIT), mootdx (MIT),
adata (Apache-2.0), and a-stock-data (Apache-2.0) at pinned commits; those packages
are not bundled or imported by this layer. No upstream endorsement is implied.
Code licenses do not grant rights to redistribute the underlying market data or
announcement PDFs; source identity and website usage terms remain separate.
