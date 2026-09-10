# Security leak scan

Blocks commits/pushes that publish personal or infrastructure data.

## Policy (P0 — always fail)

* personal email addresses (author, committer, message, any file)
* public IPv4/IPv6 addresses of real machines
* credentials: passwords, API keys, tokens, cookies, session ids, private keys
* broker / trading account numbers
* absolute local paths that identify a machine or user (`C:\Users\<name>`, `/home/<name>`, deploy roots)
* database, dump, backup and log files committed at all

## Allowed by design

Documentation-safe values only: `user@example.com`, `example.com`, `localhost`,
`127.0.0.1`, `::1`, RFC5737 ranges (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`),
public vendor/market-data endpoints, and placeholders (`<PROJECT_ROOT>`, `<USER_HOME>`,
`<SERVER_HOST>`, `<REDACTED_*>`).

## Run it

```bash
python scripts/security/scan-sensitive-data.py --repo . --scope all
```

* exit 0 = clean, exit 1 = violation
* `--scope worktree|history|all`
* `--preview` prints masked context so findings can be triaged without revealing values

## Exceptions

Add a `host:` / `suffix:` / `path:` / `value:` line to `.security-allowlist` **with a
comment explaining why it is publishable**. Never allowlist real emails, public IPs,
credentials, private keys or live broker accounts.
