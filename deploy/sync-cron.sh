#!/usr/bin/env bash
# Sync the project cron file without ever leaving backup copies in cron's active directory.
set -euo pipefail

ROOT_DIR="${ASTOCK_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CRON_DIR="${ASTOCK_CRON_DIR:-/etc/cron.d}"
CRON_BACKUP_DIR="${ASTOCK_CRON_BACKUP_DIR:-/var/backups/astock-codex/cron}"
CRON_NAME="astock-codex"
CRON_FILE="$CRON_DIR/$CRON_NAME"
TEMPLATE="$ROOT_DIR/deploy/$CRON_NAME.cron"

IS_ROOT=false
if [[ "$(id -u)" == "0" ]]; then
  IS_ROOT=true
elif [[ "$CRON_DIR" == "/etc/cron.d" ]]; then
  # Production deployment is root-only.  A non-root caller is allowed only
  # for an explicitly redirected, writable test directory.
  echo "cron sync requires root for /etc/cron.d" >&2
  exit 1
fi
if [[ ! -d "$CRON_DIR" ]]; then
  echo "cron directory does not exist: $CRON_DIR" >&2
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  echo "cron template does not exist: $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$CRON_BACKUP_DIR"
chmod 700 "$CRON_BACKUP_DIR"

migrated=0
shopt -s nullglob
for stale in "$CRON_DIR/$CRON_NAME".bak-*; do
  mv "$stale" "$CRON_BACKUP_DIR/"
  ((migrated += 1))
done
shopt -u nullglob
echo "  已迁出历史 cron 备份: $migrated"

if [[ -f "$CRON_FILE" ]]; then
  cp -a "$CRON_FILE" "$CRON_BACKUP_DIR/$CRON_NAME.bak-$(date +%Y%m%d%H%M%S)"
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed "s|/opt/astock-codex|$ROOT_DIR|g" "$TEMPLATE" > "$tmp"
chmod 0644 "$tmp"
if [[ "$IS_ROOT" == "true" ]]; then
  install -o root -g root -m 0644 "$tmp" "$CRON_FILE"
else
  install -m 0644 "$tmp" "$CRON_FILE"
fi

if compgen -G "$CRON_DIR/$CRON_NAME.bak-*" > /dev/null || \
   compgen -G "$CRON_DIR/$CRON_NAME.old*" > /dev/null || \
   compgen -G "$CRON_DIR/$CRON_NAME.copy*" > /dev/null; then
  echo "active cron directory contains stale $CRON_NAME copies" >&2
  exit 1
fi

echo "  cron active file: $CRON_FILE"
