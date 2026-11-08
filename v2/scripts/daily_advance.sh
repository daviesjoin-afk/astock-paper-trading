#!/usr/bin/env bash
# Advance one paper account to an explicit China trading day.
#
# Deliberately takes the day from the caller: the service never reads the
# machine clock. Designed for cron/task systems, e.g.
#   30 15 * * 1-5  /opt/astock/scripts/daily_advance.sh --account paper-001 --day "$(date +%F)"
#
# Env: ASTOCK_API (default http://127.0.0.1:8765)
set -euo pipefail

API="${ASTOCK_API:-http://127.0.0.1:8765}"
ACCOUNT=""
DAY=""
DRY_RUN=0

usage() {
  cat <<'TEXT'
usage: daily_advance.sh --account ACCOUNT --day YYYY-MM-DD [--dry-run] [--api URL]
TEXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --account) ACCOUNT="${2:-}"; shift 2 ;;
    --day) DAY="${2:-}"; shift 2 ;;
    --api) API="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$ACCOUNT" ] || [ -z "$DAY" ]; then
  usage >&2
  exit 2
fi
if ! printf '%s' "$DAY" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "invalid --day (expected YYYY-MM-DD): $DAY" >&2
  exit 2
fi

payload=$(printf '{"day":"%s"}' "$DAY")
url="${API%/}/v2/paper-accounts/${ACCOUNT}/advance"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "POST $url"
  echo "content-type: application/json"
  echo "$payload"
  exit 0
fi

response=$(curl -fsS --max-time 30 -X POST "$url" \
  -H 'content-type: application/json' -d "$payload") || {
  echo "advance request failed for account=$ACCOUNT day=$DAY" >&2
  exit 3
}

printf '%s' "$response" | python -c '
import json, sys
result = json.load(sys.stdin)
print(
    "advanced account={account} day={day} sessions={sessions} steps={steps} "
    "filled={filled} rejected={rejected}".format(
        account=result.get("account_id"),
        day=result.get("day"),
        sessions=len(result.get("sessions", [])),
        steps=result.get("steps"),
        filled=result.get("filled"),
        rejected=result.get("rejected"),
    )
)
'
