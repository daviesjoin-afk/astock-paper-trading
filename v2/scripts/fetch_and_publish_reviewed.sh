#!/usr/bin/env bash
# Fetch public closes, cross-check them, then publish a NEW reviewed set.
#
# The adapter itself never claims `reviewed`. This wrapper keeps that decision
# explicit: it refuses to run unless the operator passes --review and supplies
# a stable set id; a failed batch or any review finding stops publication.
#
# Env: ASTOCK_PRICE_SETS_DIR (default data/price-sets)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SYMBOLS=""
DAYS=""
SET_ID=""
PACK=""
OUT_DIR="${ASTOCK_PRICE_SETS_DIR:-data/price-sets}"
TRUST="reviewed"
REVIEW=0
PYTHON="${PYTHON:-python3}"

usage() {
  cat <<'TEXT'
usage: fetch_and_publish_reviewed.sh --symbols A,B --days d1,d2 --set-id ID \
         --pack PACK --review [--out DIR] [--trust reviewed|synthetic]

Fetches Tencent unadjusted closes, cross-checks TDX packages and Sina adjust
factors, writes a CSV + audit report under a temporary directory, and only then
publishes a new price set. --review acknowledges that a human checked the
audit report (corporate actions, suspension, coverage) before publication.
TEXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --symbols) SYMBOLS="${2:-}"; shift 2 ;;
    --days) DAYS="${2:-}"; shift 2 ;;
    --set-id) SET_ID="${2:-}"; shift 2 ;;
    --pack) PACK="${2:-}"; shift 2 ;;
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --trust) TRUST="${2:-}"; shift 2 ;;
    --review) REVIEW=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for required in SYMBOLS DAYS SET_ID PACK; do
  if [ -z "${!required}" ]; then
    echo "missing --${required,,}" >&2
    usage >&2
    exit 2
  fi
done
case "$TRUST" in
  reviewed|synthetic) ;;
  *) echo "invalid --trust: $TRUST" >&2; exit 2 ;;
esac
if [ "$TRUST" = "reviewed" ] && [ "$REVIEW" -ne 1 ]; then
  echo "refusing: reviewed publication requires --review after checking the audit report" >&2
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CSV="$WORK/prices.csv"
REPORT="$WORK/prices.report.json"

install_audit_evidence() {
  local csv="$1"
  local report="$2"
  local set_id="$3"
  local out_dir="$4"
  local audit_dir="$out_dir/audit"

  case "$set_id" in
    ""|*/*|*\\*|.|..)
      echo "invalid --set-id for audit evidence: $set_id" >&2
      return 2
      ;;
  esac
  mkdir -p "$audit_dir"
  cp "$csv" "$audit_dir/$set_id.csv"
  cp "$report" "$audit_dir/$set_id.report.json"
  chmod 0644 "$audit_dir/$set_id.csv" "$audit_dir/$set_id.report.json" 2>/dev/null || true
  echo "reviewed_acknowledged_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$audit_dir/$set_id.review.txt"
  echo "reviewed_by=${REVIEWER:-${USER:-unknown}}" >> "$audit_dir/$set_id.review.txt"
  echo "source_report_sha256=$(python3 - "$report" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
)" >> "$audit_dir/$set_id.review.txt"
  chmod 0644 "$audit_dir/$set_id.review.txt" 2>/dev/null || true
  echo "audit evidence: $audit_dir/$set_id.csv"
  echo "audit evidence: $audit_dir/$set_id.report.json"
  echo "audit evidence: $audit_dir/$set_id.review.txt"
}

"$PYTHON" data_sources/a_stock_data.py fetch \
  --symbols "$SYMBOLS" --days "$DAYS" --out "$CSV" --report "$REPORT"

"$PYTHON" - "$REPORT" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
if report.get("status") != "ok" or not report.get("review_complete"):
    findings = report.get("review_findings", [])
    print("refusing publication: batch needs human review", file=sys.stderr)
    for item in findings:
        print(
            "  {code} {symbol} {days}: {detail}".format(**item),
            file=sys.stderr,
        )
    raise SystemExit(3)
PY

IFS=',' read -r -a DAY_LIST <<< "$DAYS"
if [ "${#DAY_LIST[@]}" -eq 0 ]; then
  echo "empty --days" >&2
  exit 2
fi
START="${DAY_LIST[0]}"
END="${DAY_LIST[${#DAY_LIST[@]}-1]}"

if ! bash scripts/publish_price_set.sh \
  --input "$CSV" --set-id "$SET_ID" --pack "$PACK" --trust "$TRUST" \
  --coverage-start "$START" --coverage-end "$END" \
  --symbols "$SYMBOLS" --days "$DAYS" --out "$OUT_DIR"; then
  echo "publication failed; keeping no audit evidence for an uninstalled set" >&2
  exit 1
fi

install_audit_evidence "$CSV" "$REPORT" "$SET_ID" "$OUT_DIR"
