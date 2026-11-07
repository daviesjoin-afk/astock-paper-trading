## Summary

What changes, and why. This repository maintains AStock V2 only (`v2/`).

## Scope

- Module / owner touched:
- Behavior or contract changed:
- Data, schema or execution assumptions changed:

## Validation

Commands you actually ran (see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
cd v2 && cargo test --locked && cargo build --release --locked
python3 -m unittest discover -s v2/research -p 'test_*.py' -v
cd v2/web && npm ci && npm run build
bash v2/scripts/smoke.sh
python3 scripts/security/scan-sensitive-data.py --repo . --scope all
```

Note anything not run, and why.

## Checklist

- [ ] Single Cargo package, single Rust service, Store is the only SQLite writer.
- [ ] Paper-only: no broker routing, no real-money execution, no live market-data dependency.
- [ ] Explicit versions, timestamps and evidence; no implicit "current"/"latest"/clock reads.
- [ ] Replay and research reproduction are reported separately, never as each other's evidence.
- [ ] Synthetic fixtures are labelled as synthetic; no historical returns or strategy-advantage claims.
- [ ] No secrets, real holdings, private server paths or runtime databases.
- [ ] Docs/ADRs updated for behavior or contract changes.

## Reviewer notes

Anything a reviewer should look at first, plus known limits.
