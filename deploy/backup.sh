#!/bin/bash
# A股模拟盘每日自动备份：SQLite 在线备份 + 关键配置，保留 N 天
# 用法：backup.sh [--keep 7]
# 在宿主机执行（不需要容器权限），SQLite .backup 保证一致性且不锁主库。
set -euo pipefail

KEEP_DAYS=7
if [[ "${1:-}" == "--keep" ]]; then
  KEEP_DAYS="${2:-7}"
fi

DATE=$(date +%Y%m%d-%H%M)
DEST=/var/backups/astock-codex/daily/$DATE
mkdir -p "$DEST"

# 1. SQLite 在线备份（宿主机直连 data_cache 文件，.backup 保证一致性）
for DB in paper_trading.sqlite3 adaptive_learning.sqlite3 paper_research.sqlite3 selection_tracking.db; do
  if [[ -f /opt/astock-codex/data_cache/$DB ]]; then
    sqlite3 "/opt/astock-codex/data_cache/$DB" ".backup '$DEST/$DB'"
    echo "backup $DB ok"
  fi
done

# 2. 关键配置与清单
cp /opt/astock-codex/data_cache/kline_manifest.json "$DEST/" 2>/dev/null || true
cp /opt/astock-codex/data_cache/universe.json "$DEST/" 2>/dev/null || true
cp /etc/nginx/conf.d/astock-codex.conf "$DEST/astock-codex.nginx.conf" 2>/dev/null || true
cp /opt/astock-codex/docker-compose.server.yml "$DEST/" 2>/dev/null || true
cp /etc/cron.d/astock-codex "$DEST/astock-codex.cron" 2>/dev/null || true
cp /opt/astock-codex/deploy/astock-codex.cron "$DEST/astock-codex.cron.source" 2>/dev/null || true

# 3. 将备份内容、大小与校验写入清单，恢复脚本先校验该清单再停服务。
(cd "$DEST" && find . -maxdepth 1 -type f -printf '%f\\n' | sort | xargs -r sha256sum) > "$DEST/SHA256SUMS"
printf 'created_at=%s\\nsource=/opt/astock-codex\\n' "$(date -Is)" > "$DEST/backup.meta"

# 4. 清理过期备份
find /var/backups/astock-codex/daily -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true

echo "backup done: $DEST"
ls -lh "$DEST"
