#!/usr/bin/env bash
# Advance every shadow candidate by one settled day.
#
# This is the cron entry point for P4. It is deliberately small: tick the
# shadow series, then ask the gate what it thinks. The gate is a dry run —
# a candidate moves only when a human reads the shortfalls and calls
# promote_candidate.py --apply.
#
# Why not promote automatically: the two evidence fields a shadow series
# cannot produce (rank_ic_bps, ir_bps) would then be the only thing standing
# between an unreviewed idea and live capital. Leaving the last step manual is
# the entire point of a governance layer.
#
#   ./scripts/shadow_advance.sh <ledger-db> <market-db> [day]
#
# Exit codes: 0 ran, 2 missing argument or database.

set -u
LEDGER="${1:-}"
MARKET="${2:-}"
DAY="${3:-}"

if [ -z "$LEDGER" ] || [ -z "$MARKET" ]; then
  echo "usage: $0 <ledger-db> <market-db> [day]" >&2
  exit 2
fi
if [ ! -f "$LEDGER" ]; then
  echo "ledger not found: $LEDGER" >&2
  exit 2
fi
if [ ! -f "$MARKET" ]; then
  echo "market database not found: $MARKET" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$DAY" ]; then
  DAY=$(python -B -c "
import sqlite3,sys
c=sqlite3.connect('file:$MARKET?mode=ro',uri=True)
row=c.execute('select max(day) from bar').fetchone()
print(row[0] or '')
")
  if [ -z "$DAY" ]; then
    echo "could not determine the latest day in $MARKET" >&2
    exit 2
  fi
fi

echo "shadow advance: day=$DAY"
python -B "$HERE/shadow_tick.py" --db "$LEDGER" --market-db "$MARKET" --day "$DAY" --apply \
  || { echo "shadow tick failed" >&2; exit 1; }

echo
echo "gate preview (no state changes):"
python -B -c "
import sqlite3, json, sys
ledger = sqlite3.connect('$LEDGER')
rows = ledger.execute(
    \"select candidate_id, state, evidence_json from shadow_candidate \"
    \"where state in ('shadow','eligible','active') order by candidate_id\"
).fetchall()
if not rows:
    print('  no shadow candidates')
for candidate_id, state, evidence_json in rows:
    days = ledger.execute('select count(*) from shadow_day where candidate_id=?',
                          (candidate_id,)).fetchone()[0]
    print(f'  {candidate_id}: state={state} shadow_days={days}')
    if state == 'shadow' and days < 20:
        print(f'    needs {20 - days} more days before the shadow gate can pass')
" 2>&1

echo
echo "state was not changed. To promote, review the shortfalls and run:"
echo "  python $HERE/promote_candidate.py --db $LEDGER --candidate <id> \\"
echo "      --report <hypothesis-report.json> --day $DAY --apply"
