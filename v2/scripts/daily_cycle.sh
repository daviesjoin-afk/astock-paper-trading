#!/usr/bin/env bash
# One daily cycle: ingest the market, then advance the account.
#
# This is the piece that makes "automatic" real. Before it, the two halves
# were separate manual steps and the gap between them is exactly where a
# day silently gets skipped:
#
#   1. ingest the after-close package for $DAY     (data_sources/ingest_market.py)
#   2. POST /v2/account/run with that day          (kernel settles + trades)
#
# Order matters and is enforced: the account never advances on a day whose
# bars were not ingested, so a missing package surfaces as a refusal instead
# of a silent stale-price settlement.
#
# Env: ASTOCK_API        kernel base URL           (default http://127.0.0.1:18765)
#      ASTOCK_V2_DB      database path            (required)
#      ASTOCK_SCRIPTS    dir holding ingest_market.py (default: this script's dir)
#
# Usage:
#   daily_cycle.sh --day 2026-11-10
#   daily_cycle.sh --day 2026-11-10 --dry-run      # ingest check only, no advance
#
# Exit codes:
#   0  ingested and advanced
#   3  advance request failed
#   4  ingest failed -- account deliberately NOT advanced
#   5  ingest reported zero rows for the day
set -euo pipefail

API="${ASTOCK_API:-http://127.0.0.1:18765}"
DB="${ASTOCK_V2_DB:-}"
DAY=""
DRY_RUN="no"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="${ASTOCK_SCRIPTS:-$HERE}"

usage() {
  cat <<'TEXT'
usage: daily_cycle.sh --day YYYY-MM-DD [--dry-run]

Env:
  ASTOCK_V2_DB    database path (required)
  ASTOCK_API      kernel base URL (default http://127.0.0.1:18765)
  ASTOCK_SCRIPTS  dir with ingest_market.py (default: this script's dir)

Example cron entry (weekdays after the after-close package is published):
  10 18 * * 1-5 ASTOCK_V2_DB=/var/lib/astock-v2-rewrite/v2.sqlite3 \
    /opt/astock-v2-rewrite/scripts/daily_cycle.sh --day "$(TZ=Asia/Shanghai date +\%F)" \
    >> /var/log/astock-daily.log 2>&1
TEXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --day) DAY="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="yes"; shift ;;
    --api) API="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! printf '%s' "$DAY" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "invalid or missing --day (expected YYYY-MM-DD): $DAY" >&2
  exit 2
fi
if [ -z "$DB" ]; then
  echo "ASTOCK_V2_DB is required" >&2
  exit 2
fi
INGEST="$SCRIPTS/ingest_market.py"
if [ ! -f "$INGEST" ]; then
  echo "ingest script not found: $INGEST" >&2
  exit 2
fi

echo "=== step 1/2 ingest $DAY ==="
# --workers 8 measured 7x over serial; a single day is ~2.4s wall clock.
if ! python3 "$INGEST" --db "$DB" --day "$DAY" --workers 8; then
  echo "ingest failed for $DAY; account NOT advanced" >&2
  exit 4
fi

# Confirm rows actually landed. ingest_market.py exits 0 for a published
# package even if every row were filtered out, which would let the kernel
# settle against a day with no prices.
rows=$(python3 - "$DB" "$DAY" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    n = conn.execute(
        "SELECT count(*) FROM bar WHERE day = ?1 AND adjust = 'none'", (sys.argv[2],)
    ).fetchone()[0]
finally:
    conn.close()
print(n)
PY
)
if [ "$rows" -eq 0 ]; then
  echo "no unadjusted bars for $DAY after ingest; account NOT advanced" >&2
  exit 5
fi
echo "ingested rows for $DAY: $rows"

if [ "$DRY_RUN" = "yes" ]; then
  echo "--dry-run: skipping advance"
  exit 0
fi

echo "=== step 2/2 advance to $DAY ==="
payload=$(printf '{"day":"%s"}' "$DAY")
response=$(curl -fsS --max-time 180 -X POST "${API%/}/v2/account/run" \
  -H 'content-type: application/json' -d "$payload") || {
  echo "advance request failed day=$DAY" >&2
  exit 3
}

printf '%s' "$response" | python3 -c '
import json, sys
try:
    result = json.load(sys.stdin)
except json.JSONDecodeError:
    print("advance response is not valid JSON", file=sys.stderr)
    raise SystemExit(3)
print(
    "advance day={day} processed={processed} trades={trades} rebalances={rebalances} "
    "last_settled={last}".format(
        day=result.get("through_day"),
        processed=len(result.get("processed_days") or []),
        trades=len(result.get("trades") or []),
        rebalances=len(result.get("rebalances") or []),
        last=(result.get("account") or {}).get("last_settled_day"),
    )
)
'
