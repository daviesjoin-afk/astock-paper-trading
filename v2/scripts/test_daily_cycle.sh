#!/usr/bin/env bash
# Tests for daily_cycle.sh's decision logic.
#
# These run against a stub `astock-v2`, a stub `python3`, and a stub server so the ordering and
# failure behaviour can be checked without the network. What matters here is
# not that ingest works (bar_smoke.sh covers the real path) but that the cycle
# refuses to advance when ingest failed or produced nothing -- that is the one
# mistake that silently settles an account against a stale price.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT="$HERE/daily_cycle.sh"
WORK="$HERE/../work/p0-daily-cycle-test"
rm -rf "$WORK"
mkdir -p "$WORK"
PASS=0
FAIL=0

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; }

# Stub only the external ingest and row-count calls.
make_stub() {
  local ingest_exit="$1" row_count="$2" dir="$3"
  cat >"$dir/astock-v2" <<EOF
#!/usr/bin/env bash
[ "\$1" = ingest ] || exit 2
printf '%s\n' "\$*" >> "\$STUB_INGEST_LOG"
exit $ingest_exit
EOF
  cat >"$dir/python3" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "-" ]; then echo "$row_count"; fi
EOF
  chmod +x "$dir/astock-v2" "$dir/python3"
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
mkdir -p "$WORK/bin"
export STUB_CURL_LOG="$WORK/curl.log"
export STUB_INGEST_LOG="$WORK/ingest.log"

run_cycle() {
  PATH="$WORK/bin:$PATH" \
  ASTOCK_V2_DB="$WORK/fake.sqlite3" \
  ASTOCK_V2_BIN="$WORK/bin/astock-v2" \
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

echo "== leftover access environment does not affect advance =="
make_stub 0 5576 "$WORK/bin"
make_curl_stub "$WORK/bin"
: >"$STUB_CURL_LOG"
set +e
ASTOCK_V2_TOKEN="ignored-legacy-value" run_cycle --day 2026-11-10 >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 0 ] && ok "legacy environment is ignored" || bad "legacy environment exit $code"
grep -q "v2/account/run" "$STUB_CURL_LOG" && ok "advance needs no credential" || bad "advance not called"
if grep -qi -e 'Authorization[ :]' -e 'Bearer' -e 'ignored-legacy-value' "$STUB_CURL_LOG"; then
  bad "unexpected credential header or value"
else
  ok "no credential sent"
fi

echo "== dry-run parses without writes or advance =="
make_stub 0 5576 "$WORK/bin"
: >"$STUB_CURL_LOG"
set +e
run_cycle --day 2026-11-10 --dry-run >/dev/null 2>&1; code=$?
set -e
tail -n 1 "$STUB_INGEST_LOG" | grep -q -- --dry-run && ok "dry-run reaches Rust ingest" || bad "dry-run would write rows"
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
