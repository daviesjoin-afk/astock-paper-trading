#!/usr/bin/env bash
# End-to-end check for the whole-market bar pipeline.
#
# Ingests two real trading days into a throwaway database, starts the real
# binary on an isolated port, and exercises the new /v2/market/* endpoints.
# Nothing here touches production paths.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2_DIR="$(cd "$HERE/.." && pwd)"
PORT="${ASTOCK_SMOKE_PORT:-18791}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/astock-bar-smoke-XXXXXX")"
DB="$WORK/smoke.sqlite3"
LOG="$WORK/server.log"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$V2_DIR"

echo "== creating schema =="
python - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("""CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)""")
conn.execute("""INSERT INTO schema_version(version) VALUES(2)""")
conn.execute("""CREATE TABLE IF NOT EXISTS bar(
  symbol TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
  open_cents INTEGER NOT NULL, high_cents INTEGER NOT NULL, low_cents INTEGER NOT NULL,
  close_cents INTEGER NOT NULL, prev_close_cents INTEGER NOT NULL,
  volume_shares INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
  PRIMARY KEY(symbol, day))""")
conn.execute("CREATE INDEX IF NOT EXISTS bar_by_day ON bar(day)")
conn.commit(); conn.close()
PY

echo "== ingesting two real days =="
python data_sources/ingest_market.py --db "$DB" --days 2026-11-09:2026-11-10

echo "== starting server on 127.0.0.1:$PORT =="
ASTOCK_V2_DB="$DB" ASTOCK_V2_ADDR="127.0.0.1:$PORT" \
  "${ASTOCK_BIN:-$V2_DIR/target/debug/astock-v2.exe}" >"$LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/v2/health" >/dev/null 2>&1; then break; fi
  sleep 0.25
done
curl -fsS "http://127.0.0.1:$PORT/v2/health" >/dev/null || {
  echo "server did not come up; log:"; cat "$LOG"; exit 1;
}

echo "== /v2/market/days =="
DAYS=$(curl -fsS "http://127.0.0.1:$PORT/v2/market/days")
echo "$DAYS"
python - "$DAYS" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
assert payload["count"] == 2, payload["count"]
assert payload["latest_day"] == "2026-11-10", payload["latest_day"]
assert payload["latest_symbol_count"] > 5000, payload["latest_symbol_count"]
PY

echo "== /v2/market/bars (count only) =="
# The full day is ~1.5 MB of JSON, well past the OS argument limit, so pipe it
# into python on stdin instead of passing it as argv.
curl -fsS "http://127.0.0.1:$PORT/v2/market/bars?day=2026-11-10" | python -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["day"] == "2026-11-10"
assert payload["count"] > 5000, payload["count"]
first = payload["bars"][0]
for key in ("symbol", "day", "close_cents", "prev_close_cents"):
    assert key in first, key
print("first bar:", first["symbol"], first["close_cents"], "cents")
'

echo "== /v2/market/bars/600000.SH =="
ONE=$(curl -fsS "http://127.0.0.1:$PORT/v2/market/bars/600000.SH")
python - "$ONE" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
assert payload["symbol"] == "600000.SH"
assert payload["count"] == 2, payload["count"]
days = [b["day"] for b in payload["bars"]]
assert days == ["2026-11-09", "2026-11-10"], days
print("600000.SH closes:", [b["close_cents"] for b in payload["bars"]])
PY

echo "== /v2/market/gaps =="
GAPS=$(curl -fsS "http://127.0.0.1:$PORT/v2/market/gaps?day=2026-11-10&threshold_bps=1100")
python - "$GAPS" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
assert payload["threshold_bps"] == 1100
for bar in payload["bars"]:
    assert abs(bar["close_cents"] - bar["prev_close_cents"]) * 10000 // bar["prev_close_cents"] > 1100
print("gaps over 1100bps:", payload["count"])
PY

echo "== unknown symbol is a clean 404 =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/v2/market/bars/999999.SH")
test "$CODE" = "404" || { echo "expected 404, got $CODE"; exit 1; }

echo
echo "bar smoke ok"
