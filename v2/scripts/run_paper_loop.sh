#!/usr/bin/env bash
# Drive the paper account forward one trading day at a time, calling the AI
# observer before each advance.
#
# This is the "run it for real" loop the owner asked for: instead of replaying
# history in a single batch and reading one number at the end, each day is
# settled on its own, the observer looks at that day's state, and the result is
# recorded. The reason is that a batch replay cannot show whether the AI
# involvement helps or hurts -- by the time it finishes, the decisions and their
# consequences are already collapsed into one equity figure.
#
# What the wall clock is NOT used for: which day to settle. The trading calendar
# comes from the ingested data, so the loop is reproducible and does not depend
# on what today's date happens to be.
#
# Usage:
#   run_paper_loop.sh --from 2026-06-02 --days 20 [--ai on|off] [--api URL]
#
# Every iteration writes one line to the log and leaves the ledger untouched on
# any failure, so an interrupted run can be resumed by starting from its last
# settled day.

set -uo pipefail

API="${ASTOCK_API:-http://127.0.0.1:8765}"
FROM=""
DAYS=10
AI="on"
LOG_DIR="${ASTOCK_LOOP_LOG:-work/live/loop}"

while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --days) DAYS="${2:-10}"; shift 2 ;;
    --ai) AI="${2:-on}"; shift 2 ;;
    --api) API="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! printf '%s' "$FROM" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "--from YYYY-MM-DD is required" >&2
  exit 2
fi

mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/loop-$STAMP.log"

note() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

# Trading days we hold bars for, from the kernel rather than a local calendar.
# `day_list.py` does the JSON parse and prints clean LF-separated days; doing it
# inline needed nested quoting plus CRLF stripping, and both broke silently.
ALL_DAYS=()
while IFS= read -r d; do
  [ -n "$d" ] && ALL_DAYS+=("$d")
done < <(python "$(dirname "$0")/day_list.py" --api "$API" --from "$FROM" 2>/dev/null)

if [ "${#ALL_DAYS[@]}" -eq 0 ]; then
  note "no trading day at or after $FROM"; exit 3
fi

note "loop start from=${ALL_DAYS[0]} days=$DAYS ai=$AI api=$API"

settled=0
for ((i = 0; i < ${#ALL_DAYS[@]} && settled < DAYS; i++)); do
  DAY="${ALL_DAYS[$i]}"

  if [ "$AI" = "on" ]; then
    # Ask the observer first. A failure here is logged and ignored: the day
    # must settle on the deterministic policy rather than wait for a model.
    obs_file="$LOG_DIR/observe-$DAY.json"
    if curl -fsS --max-time 20 "$API/v2/market/observation?day=$DAY" \
         -o "$obs_file" 2>/dev/null; then
      advice_out=$(timeout 180 python data_sources/market_observer.py \
        --observation "$obs_file" --api "$API" 2>&1)
      note "  observe $DAY: $(printf '%s' "$advice_out" | head -c 200)"
    else
      note "  observe $DAY: no observation endpoint, skipped"
    fi
  fi

  result_file="$LOG_DIR/advance-$DAY.json"
  code=$(curl -sS --max-time 600 -o "$result_file" -w '%{http_code}' \
    -X POST "$API/v2/account/run" \
    -H 'content-type: application/json' -d "{\"day\":\"$DAY\"}")
  if [ "$code" != "200" ]; then
    note "  $DAY settle failed http=$code $(head -c 160 "$result_file")"
    continue
  fi

  summary=$(python - "$result_file" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
trades = payload.get("trades", [])
account = payload.get("account", {})
buys = [t for t in trades if t.get("side") == "buy"]
sells = [t for t in trades if t.get("side") == "sell"]
print("trades=%d buy=%d sell=%d equity=%s ret_bps=%s" % (
    len(trades), len(buys), len(sells),
    account.get("equity_cents"), account.get("return_bps")))
PY
)
  note "  $DAY $summary"
  settled=$((settled + 1))
done

note "loop done settled=$settled log=$LOG"
