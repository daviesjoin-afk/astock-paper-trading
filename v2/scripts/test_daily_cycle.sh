#!/usr/bin/env bash
# Tests for daily_cycle.sh's decision logic.
#
# These run against a stub `python3` and a stub server so the ordering and
# failure behaviour can be checked without the network. What matters here is
# not that ingest works (bar_smoke.sh covers the real path) but that the cycle
# refuses to advance when ingest failed or produced nothing -- that is the one
# mistake that silently settles an account against a stale price.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/daily_cycle.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/astock-cycle-test-XXXXXX")"
PASS=0
FAIL=0

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; }

# A python3 stub that answers the two ways the script calls it: the ingest
# script (first arg ends in ingest_market.py) and the row-count here-doc.
make_stub() {
  local ingest_exit="$1" row_count="$2" dir="$3"
  cat >"$dir/python3" <<EOF
#!/usr/bin/env bash
if [ "\${1##*/}" = "ingest_market.py" ]; then
  echo "[stub] ingest called"
  exit $ingest_exit
fi
# Row-count here-doc: emit the configured count.
echo "$row_count"
EOF
  chmod +x "$dir/python3"
}

# A curl stub that records the advance call and returns a valid RunResult.
make_curl_stub() {
  local dir="$1"
  cat >"$dir/curl" <<'EOF'
#!/usr/bin/env bash
echo "[stub] curl $*" >> "$STUB_CURL_LOG"
printf '%s' '{"through_day":"2026-11-10","processed_days":["2026-11-10"],"trades":[],"rebalances":[],"account":{"last_settled_day":"2026-11-10"}}'
EOF
  chmod +x "$dir/curl"
}

touch "$WORK/fake.sqlite3"
mkdir -p "$WORK/scripts" "$WORK/bin"
cp "$HERE/../../v2/data_sources/ingest_market.py" "$WORK/scripts/ingest_market.py" 2>/dev/null \
  || printf '#!/usr/bin/env python3\n' >"$WORK/scripts/ingest_market.py"
export STUB_CURL_LOG="$WORK/curl.log"

run_cycle() {
  PATH="$WORK/bin:$PATH" \
  ASTOCK_V2_DB="$WORK/fake.sqlite3" \
  ASTOCK_SCRIPTS="$WORK/scripts" \
  ASTOCK_API="http://127.0.0.1:19999" \
  bash "$SCRIPT" "$@"
}

echo "== argument validation =="
if run_cycle >/dev/null 2>&1; then bad "missing --day should fail"; else ok "missing --day rejected"; fi
if run_cycle --day 2026/11/10 >/dev/null 2>&1; then bad "bad day format should fail"; else ok "bad day format rejected"; fi
if ASTOCK_V2_DB= bash "$SCRIPT" --day 2026-11-10 >/dev/null 2>&1; then
  bad "missing DB should fail"; else ok "missing DB rejected"; fi

echo "== ingest failure blocks advance =="
make_stub 1 5000 "$WORK/bin"
make_curl_stub "$WORK/bin"
: >"$STUB_CURL_LOG"
set +e
output=$(run_cycle --day 2026-11-10 2>&1); code=$?
set -e
[ "$code" -eq 4 ] && ok "exit 4 on ingest failure" || bad "expected exit 4, got $code"
if [ -s "$STUB_CURL_LOG" ]; then bad "advance was called despite ingest failure"; else ok "advance not called"; fi
case "$output" in
  *"account NOT advanced"*) ok "says the account was not advanced" ;;
  *) bad "missing not-advanced message" ;;
esac

echo "== zero rows blocks advance =="
make_stub 0 0 "$WORK/bin"
: >"$STUB_CURL_LOG"
set +e
output=$(run_cycle --day 2026-11-10 2>&1); code=$?
set -e
[ "$code" -eq 5 ] && ok "exit 5 on zero rows" || bad "expected exit 5, got $code"
if [ -s "$STUB_CURL_LOG" ]; then bad "advance was called with zero rows"; else ok "advance not called"; fi

echo "== success path advances =="
make_stub 0 5576 "$WORK/bin"
: >"$STUB_CURL_LOG"
set +e
output=$(run_cycle --day 2026-11-10 2>&1); code=$?
set -e
[ "$code" -eq 0 ] && ok "exit 0 on success" || bad "expected exit 0, got $code"
grep -q "v2/account/run" "$STUB_CURL_LOG" && ok "advance was called" || bad "advance not called"
grep -q '"day":"2026-11-10"' "$STUB_CURL_LOG" && ok "advanced to the right day" || bad "wrong payload"
case "$output" in
  *"ingested rows for 2026-11-10: 5576"*) ok "reports ingested row count" ;;
  *) bad "missing row count report" ;;
esac

echo "== dry-run ingests but does not advance =="
make_stub 0 5576 "$WORK/bin"
: >"$STUB_CURL_LOG"
set +e
run_cycle --day 2026-11-10 --dry-run >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 0 ] && ok "dry-run exits 0" || bad "dry-run exit $code"
if [ -s "$STUB_CURL_LOG" ]; then bad "dry-run advanced the account"; else ok "dry-run did not advance"; fi

echo "== advance failure is reported =="
make_stub 0 5576 "$WORK/bin"
cat >"$WORK/bin/curl" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
chmod +x "$WORK/bin/curl"
set +e
run_cycle --day 2026-11-10 >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 3 ] && ok "exit 3 on advance failure" || bad "expected exit 3, got $code"

echo
printf 'daily cycle tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
