#!/usr/bin/env bash
# Read-only preview of the published coverage before an advance.
set -euo pipefail

API="${ASTOCK_API:-http://127.0.0.1:18765}"
DAY=""
DRY_RUN=0

usage() {
  cat <<'TEXT'
usage: daily_advance.sh --day YYYY-MM-DD [--api URL] [--dry-run]
TEXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --day) DAY="${2:-}"; shift 2 ;;
    --api) API="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! printf '%s' "$DAY" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "invalid or missing --day (expected YYYY-MM-DD): $DAY" >&2
  exit 2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  coverage=$(curl -fsS --max-time 30 "${API%/}/v2/prices/coverage") || {
    echo "coverage request failed api=$API" >&2
    exit 3
  }
  printf '%s' "$coverage" | python3 -c '
import json, sys
try:
    coverage = json.load(sys.stdin)
except json.JSONDecodeError:
    print("coverage response is not valid JSON", file=sys.stderr)
    raise SystemExit(3)
print(
    "coverage start={start} end={end} days={days} symbols={symbols} "
    "last_settled={last}".format(
        start=coverage.get("coverage_start"),
        end=coverage.get("coverage_end"),
        days=len(coverage.get("days") or []),
        symbols=len(coverage.get("symbols") or []),
        last=coverage.get("last_settled_day"),
    )
)
'
  exit 0
fi

exec "$(dirname "$0")/daily_auto.sh" --day "$DAY" --api "$API"
