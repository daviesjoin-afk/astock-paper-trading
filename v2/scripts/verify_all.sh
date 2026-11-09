#!/usr/bin/env bash
# Run every V2 verification suite and print one summary.
#
# Usage:
#   bash scripts/verify_all.sh [--fast] [--no-scan] [--dry-run]
#
#   --fast       skip the browser suite (needs a release build + Playwright)
#   --no-scan    skip the sensitive-data scan
#   --archive D  also verify every ledger/export pair in directory D
#   --report F   write a JSON summary of every suite to F
#   --dry-run    print the commands instead of running them
#
# Exit code: 0 all green, 1 any suite failed, 2 usage error.
set -uo pipefail

FAST=0
SCAN=1
DRY=0
ARCHIVE=""
REPORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1; shift ;;
    --no-scan) SCAN=0; shift ;;
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --report) REPORT="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CARGO="${CARGO:-cargo +1.99.1-x86_64-pc-windows-gnu}"
PYTHON="${PYTHON:-python}"
export RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN:-1.99.1-x86_64-pc-windows-gnu}"

NAMES=()
COMMANDS=()
add() { NAMES+=("$1"); COMMANDS+=("$2"); }

add "rust" "$CARGO test --locked"
add "research" "$PYTHON -m unittest discover -s research -p 'test_*.py'"
add "smoke" "bash scripts/smoke.sh"
if [ "$FAST" -eq 0 ]; then
  add "browser" "(cd web && npm run build && npx playwright test)"
fi
if [ "$SCAN" -eq 1 ]; then
  add "security-scan" "$PYTHON scripts/security/scan-sensitive-data.py --repo . --scope worktree"
fi
if [ -n "$ARCHIVE" ]; then
  ARCHIVE_CMD="$PYTHON scripts/verify_archive.py --dir '$ARCHIVE'"
  if [ -n "$REPORT" ]; then
    ARCHIVE_CMD="$ARCHIVE_CMD --manifest-out '${REPORT}.archive.json'"
  fi
  add "archive" "$ARCHIVE_CMD"
fi

if [ "$DRY" -eq 1 ]; then
  for index in "${!NAMES[@]}"; do
    printf '%-14s %s\n' "${NAMES[$index]}" "${COMMANDS[$index]}"
  done
  exit 0
fi

FAILED=0
RESULTS="$(mktemp)"
trap 'rm -f "$RESULTS"' EXIT
printf '%-14s %-8s %s\n' "suite" "result" "seconds"
for index in "${!NAMES[@]}"; do
  name="${NAMES[$index]}"
  start=$(date +%s)
  if eval "${COMMANDS[$index]}"; then
    result="pass"
  else
    result="FAIL"
    FAILED=1
  fi
  elapsed=$(( $(date +%s) - start ))
  printf '%-14s %-8s %s\n' "$name" "$result" "$elapsed"
  printf '%s|%s|%s\n' "$name" "$result" "$elapsed" >> "$RESULTS"
done

if [ -n "$REPORT" ]; then
  "$PYTHON" - "$REPORT" "$RESULTS" "$FAILED" <<'REPORT_PY'
import json
import sys
from datetime import datetime, timezone

target, results, failed = sys.argv[1], sys.argv[2], int(sys.argv[3])
suites = []
with open(results, encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        name, result, seconds = line.split("|")
        suites.append({"name": name, "result": result, "seconds": int(seconds)})
payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "ok": failed == 0,
    "suites": suites,
}
with open(target, "w", encoding="utf-8") as out:
    json.dump(payload, out, indent=2, sort_keys=True)
    out.write("\n")
REPORT_PY
fi

if [ "$FAILED" -ne 0 ]; then
  echo "verification failed" >&2
  exit 1
fi
echo "all verification suites passed"
