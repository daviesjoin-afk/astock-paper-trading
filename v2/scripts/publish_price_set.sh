#!/usr/bin/env bash
# Publish one reviewed price-set artifact: generate, validate, install.
#
# The generator already refuses gaps, duplicate rows and bad prices; this script
# adds an explicit install step so operators never write half-valid data into
# the live catalog.
#
# Env: ASTOCK_PRICE_SETS_DIR (default data/price-sets)
#      ASTOCK_V2_BIN (default: this checkout's release binary)
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

BINARY="${ASTOCK_V2_BIN:-$(dirname "${BASH_SOURCE[0]}")/../target/release/astock-v2}"
if [ -z "${ASTOCK_V2_BIN:-}" ] && [ ! -x "$BINARY" ] && [ -x "$BINARY.exe" ]; then
  BINARY="$BINARY.exe"
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
  printf '%q price-set' "$BINARY"
  printf ' %q' "${GEN_ARGS[@]}"
  printf '\n'
  echo "install: $STAGED -> $TARGET"
  exit 0
fi

if [ -e "$TARGET" ] && [ "$FORCE" -ne 1 ]; then
  echo "refusing to overwrite existing artifact: $TARGET (use --force after review)" >&2
  exit 3
fi

if [ -d "$TARGET" ]; then
  echo "artifact destination is a directory: $TARGET" >&2
  exit 2
fi

BUILD_SUMMARY=$("$BINARY" price-set "${GEN_ARGS[@]}")
printf '%s\n' "$BUILD_SUMMARY"
# Use the builder's validated metadata from its success line.
if [[ $BUILD_SUMMARY =~ \ entries=([0-9]+)\ missing=0\ source_digest=([0-9a-f]{64})$ ]]; then
  ENTRIES="${BASH_REMATCH[1]}"
  DIGEST="${BASH_REMATCH[2]}"
else
  echo "unexpected price-set build summary" >&2
  exit 2
fi
mkdir -p "$OUT_DIR"
cp "$STAGED" "$TARGET"
if [ ! -f "$TARGET" ]; then
  echo "artifact was not installed as a file: $TARGET" >&2
  exit 2
fi
chmod 0644 "$TARGET" 2>/dev/null || true

printf 'published set=%s trust=%s entries=%s source_digest=%s\n' "$SET_ID" "$TRUST" "$ENTRIES" "$DIGEST"
echo "next: GET /v2/price-sets and GET /v2/price-sets/coverage?pack_id=$PACK&symbols=$SYMBOLS&days=${DAYS:-$START,$END}"
