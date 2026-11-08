#!/usr/bin/env bash
# Run every V2 verification suite and print one summary.
#
# Usage:
#   bash scripts/verify_all.sh [--fast] [--no-scan] [--dry-run]
#
#   --fast       skip the browser suite (needs a release build + Playwright)
#   --no-scan    skip the sensitive-data scan
#   --archive D  also verify every ledger/export pair in directory D
#   --dry-run    print the commands instead of running them
#
# Exit code: 0 all green, 1 any suite failed, 2 usage error.
set -uo pipefail

FAST=0
SCAN=1
DRY=0
ARCHIVE=
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1; shift ;;
    --no-scan) SCAN=0; shift ;;
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
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
  add "archive" "$PYTHON scripts/verify_archive.py --dir '$ARCHIVE'"
fi

if [ "$DRY" -eq 1 ]; then
  for index in "${!NAMES[@]}"; do
    printf '%-14s %s\n' "${NAMES[$index]}" "${COMMANDS[$index]}"
  done
  exit 0
fi

FAILED=0
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
done

if [ "$FAILED" -ne 0 ]; then
  echo "verification failed" >&2
  exit 1
fi
echo "all verification suites passed"
