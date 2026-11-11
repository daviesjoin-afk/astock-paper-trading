#!/usr/bin/env bash
# Advance the single V2 paper account to an explicit trading day.
# The caller supplies the day; the service never reads the wall clock.
#
# Env: ASTOCK_API (default http://127.0.0.1:18765)
set -euo pipefail

API="${ASTOCK_API:-http://127.0.0.1:18765}"
DAY=""

usage() {
  cat <<'TEXT'
usage: daily_auto.sh --day YYYY-MM-DD [--api URL]

Example cron entry (after the reviewed price set is published):
  30 15 * * 1-5 ASTOCK_API=http://127.0.0.1:18765 \
    /opt/astock-v2-rewrite/scripts/daily_auto.sh --day "$(TZ=Asia/Shanghai date +%F)"
TEXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --day) DAY="${2:-}"; shift 2 ;;
    --api) API="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! printf '%s' "$DAY" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "invalid or missing --day (expected YYYY-MM-DD): $DAY" >&2
  exit 2
fi

payload=$(printf '{"day":"%s"}' "$DAY")
response=$(curl -fsS --max-time 120 -X POST "${API%/}/v2/account/run" \
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
