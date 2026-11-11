#!/usr/bin/env bash
# Build once on the target (or copy the two artifacts in), then install.
# Never enables or starts anything implicitly: review, then `systemctl enable --now`.
set -euo pipefail

PREFIX="${PREFIX:-/opt/astock-v2-rewrite}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
ENV_FILE="${ENV_FILE:-/etc/astock-v2-rewrite.env}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_SRC="${WEB_SRC:-$REPO/v2/web/dist}"
BIN_SRC="${BIN_SRC:-$REPO/v2/target/release/astock-v2}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 2; }; }
need install

[ -x "$BIN_SRC" ] || { echo "binary not found: $BIN_SRC (build with: cd v2 && cargo build --release --locked)" >&2; exit 2; }
[ -f "$WEB_SRC/index.html" ] || { echo "web build not found: $WEB_SRC (run: cd v2/web && npm ci && npm run build)" >&2; exit 2; }

id -u astock >/dev/null 2>&1 || useradd --system --home /var/lib/astock-v2-rewrite --shell /usr/sbin/nologin astock
install -d -o astock -g astock "$PREFIX/bin" "$PREFIX/web" /var/lib/astock-v2-rewrite /var/backups/astock-v2-rewrite
install -m 0755 "$BIN_SRC" "$PREFIX/bin/astock-v2"
cp -a "$WEB_SRC/." "$PREFIX/web/"
install -d -m 0755 "$PREFIX/scripts"
install -m 0755 "$REPO/v2/scripts/daily_auto.sh" "$PREFIX/scripts/daily_auto.sh"
install -m 0755 "$REPO/v2/scripts/daily_advance.sh" "$PREFIX/scripts/daily_advance.sh"
install -m 0755 "$REPO/v2/scripts/db_tool.py" "$PREFIX/scripts/db_tool.py"

install -m 0644 "$REPO/v2/deploy/astock-v2.service" "$UNIT_DIR/astock-v2-rewrite.service"
if [ ! -f "$ENV_FILE" ]; then
  install -m 0640 -o root -g astock "$REPO/v2/deploy/astock-v2.env.example" "$ENV_FILE"
  echo "wrote $ENV_FILE (edit before starting)"
fi

echo "installed binary -> $PREFIX/bin/astock-v2"
echo "installed console -> $PREFIX/web"
echo "next: edit $ENV_FILE, install v2/deploy/nginx.conf as astock-v2-rewrite.conf,"
echo "      then systemctl daemon-reload && systemctl enable --now astock-v2-rewrite"
