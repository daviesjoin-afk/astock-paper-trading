#!/usr/bin/env bash
# 安装/更新三策略周期启动器的 cron 片段（独立文件，不并入 astock-codex.cron）。
#
# 用法：sudo bash deploy/install-three-strategy-cron.sh
set -euo pipefail

ROOT="${ASTOCK_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CRON_DIR="${ASTOCK_CRON_DIR:-/etc/cron.d}"
SRC="$ROOT/deploy/astock-three-strategy-start.cron"
DST="$CRON_DIR/astock-three-strategy-start"

if [ ! -f "$SRC" ]; then
  echo "cron 模板缺失：$SRC" >&2
  exit 1
fi
if [ ! -d "$CRON_DIR" ]; then
  echo "cron 目录不存在：$CRON_DIR" >&2
  exit 1
fi
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed "s|/opt/astock-codex|$ROOT|g" "$SRC" > "$tmp"
install -m 644 "$tmp" "$DST"
echo "已安装 $DST"
grep -v '^#' "$DST" | grep -v '^$' || true
