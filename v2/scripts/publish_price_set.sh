#!/usr/bin/env bash
# Publish one reviewed price-set artifact: generate, validate, install.
#
# The generator already refuses gaps, duplicate rows and bad prices; this script
# adds an explicit install step so operators never write half-valid data into
# the live catalog.
#
# Env: ASTOCK_PRICE_SETS_DIR (default data/price-sets)
set -euo pipefail

INPUT=""
SET_ID=""
PACK=""
START=""
END=""
SYMBOLS=""
DAYS=""
TRUST="reviewed"
OUT_DIR="${ASTOCK_PRICE_SETS_DIR:-data/price-sets}"
DRY_RUN=0
FORCE=0

usage() {
  cat <<'TEXT'
usage: publish_price_set.sh --input CSV --set-id ID --pack PACK \
         --coverage-start YYYY-MM-DD --coverage-end YYYY-MM-DD \
         --symbols A,B [--days d1,d2] [--trust reviewed|synthetic] \
         [--out DIR] [--dry-run] [--force]
TEXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --input) INPUT="${2:-}"; shift 2 ;;
    --set-id) SET_ID="${2:-}"; shift 2 ;;
    --pack) PACK="${2:-}"; shift 2 ;;
    --coverage-start) START="${2:-}"; shift 2 ;;
    --coverage-end) END="${2:-}"; shift 2 ;;
    --symbols) SYMBOLS="${2:-}"; shift 2 ;;
    --days) DAYS="${2:-}"; shift 2 ;;
    --trust) TRUST="${2:-}"; shift 2 ;;
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for required in INPUT SET_ID PACK START END SYMBOLS; do
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
if [ ! -f "$INPUT" ]; then
  echo "input not found: $INPUT" >&2
  exit 2
fi

TARGET="$OUT_DIR/$SET_ID.json"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT
STAGED="$STAGE_DIR/$SET_ID.json"
REPORT="$STAGE_DIR/report.json"

GEN_ARGS=(--input "$INPUT" --out "$STAGED" --report "$REPORT"
  --set-id "$SET_ID" --pack "$PACK" --trust "$TRUST"
  --coverage-start "$START" --coverage-end "$END" --symbols "$SYMBOLS")
if [ -n "$DAYS" ]; then
  GEN_ARGS+=(--days "$DAYS")
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "python research/make_price_set.py ${GEN_ARGS[*]}"
  echo "install: $STAGED -> $TARGET"
  exit 0
fi

if [ -e "$TARGET" ] && [ "$FORCE" -ne 1 ]; then
  echo "refusing to overwrite existing artifact: $TARGET (use --force after review)" >&2
  exit 3
fi

python research/make_price_set.py "${GEN_ARGS[@]}"
mkdir -p "$OUT_DIR"
cp "$STAGED" "$TARGET"
chmod 0644 "$TARGET" 2>/dev/null || true

DIGEST=$(python - "$TARGET" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(
    "published set={} trust={} entries={} source_digest={}".format(
        data["price_set_id"], data["trust"], len(data["entries"]), data["source_digest"]
    )
)
PY
)
echo "$DIGEST"
echo "next: GET /v2/price-sets and GET /v2/price-sets/coverage?pack_id=$PACK&symbols=$SYMBOLS&days=${DAYS:-$START,$END}"
