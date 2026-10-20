#!/bin/bash
# A股模拟盘每日自动备份：SQLite 在线备份 + 关键配置，保留 N 天
# 用法：backup.sh [--keep 7]
# 在宿主机执行；优先使用容器内 SQLite 的在线备份，保持一致性。
# 项目目录自动探测：优先 /opt/astock-codex（开源部署），回退 /root/codex（腾讯云单机）。
set -euo pipefail

KEEP_DAYS=7
if [[ "${1:-}" == "--keep" ]]; then
  KEEP_DAYS="${2:-7}"
fi

if [[ -n "${APP_DIR:-}" ]]; then
  PROJECT_DIR="$APP_DIR"
elif [[ -d /opt/astock-codex/data_cache ]]; then
  PROJECT_DIR=/opt/astock-codex
elif [[ -d /root/codex/data_cache ]]; then
  PROJECT_DIR=/root/codex
else
  echo "backup failed: 未找到项目目录（尝试过 APP_DIR、/opt/astock-codex、/root/codex）" >&2
  exit 1
fi

DATE=$(date +%Y%m%d-%H%M)
DEST=${BACKUP_ROOT:-/var/backups/astock-codex}/daily/$DATE
mkdir -p "$DEST"

# 1. SQLite 在线备份（宿主机直连 data_cache 文件，.backup 保证一致性）。
#    账本体积大（paper_trading 2G+、adaptive 700M+），gzip 压缩后落盘，
#    否则 keep 7 的日备份会在一周内占满整块磁盘。
#
#    读取引擎优先用容器内的 SQLite：镜像已从钉死的 amalgamation 重建为 3.51+
#    （含 WAL-reset 修复），而 CentOS 宿主机的 sqlite3 CLI 仍是 3.34.1，
#    两者都缺修复时读一个正在写入的 WAL 库正是审计点名的风险路径。
#    容器不可用时才回退到宿主机 CLI，并在日志里写明用的是哪个引擎。
BACKUP_IMAGE=${ASTOCK_BACKUP_IMAGE:-astock-codex:latest}
backup_one_db() {
  local src="$1" dst="$2"
  if command -v docker >/dev/null 2>&1 && docker image inspect "$BACKUP_IMAGE" >/dev/null 2>&1; then
    if docker run --rm --user root \
        -v "$(dirname "$src"):/backup-src" \
        -v "$(dirname "$dst"):/backup-dst" \
        --entrypoint python "$BACKUP_IMAGE" -c \
        'import sqlite3, sys
# WAL 读取可能需要创建/维护 -shm，因此目录必须可写；连接本身仍只读。
src = "file:/backup-src/" + sys.argv[1] + "?mode=ro"
live = sqlite3.connect(src, uri=True)
copy = sqlite3.connect("/backup-dst/" + sys.argv[2])
with copy:
    live.backup(copy)
copy.close()
live.close()
print("engine=container sqlite=" + sqlite3.sqlite_version)' \
        "$(basename "$src")" "$(basename "$dst")" 2>/dev/null; then
      return 0
    fi
    echo "container backup failed for $(basename "$src"), falling back to host sqlite3" >&2
  fi
  sqlite3 "$src" ".backup '$dst'"
  echo "engine=host sqlite=$(sqlite3 --version 2>/dev/null | cut -d' ' -f1)"
}

for DB in paper_trading.sqlite3 adaptive_learning.sqlite3 paper_research.sqlite3 selection_tracking.db; do
  if [[ -f "$PROJECT_DIR/data_cache/$DB" ]]; then
    backup_one_db "$PROJECT_DIR/data_cache/$DB" "$DEST/$DB"
    gzip -f "$DEST/$DB"
    echo "backup $DB.gz ok ($(du -h "$DEST/$DB.gz" | cut -f1))"
  fi
done

# 2. 关键配置与清单
cp "$PROJECT_DIR/data_cache/kline_manifest.json" "$DEST/" 2>/dev/null || true
cp "$PROJECT_DIR/data_cache/universe.json" "$DEST/" 2>/dev/null || true
cp /etc/nginx/conf.d/astock-codex.conf "$DEST/astock-codex.nginx.conf" 2>/dev/null || true
cp "$PROJECT_DIR/docker-compose.server.yml" "$DEST/" 2>/dev/null || true
cp /etc/cron.d/astock-codex "$DEST/astock-codex.cron" 2>/dev/null || true
cp "$PROJECT_DIR/deploy/astock-codex.cron" "$DEST/astock-codex.cron.source" 2>/dev/null || true

# 3. 将备份内容、大小与校验写入清单，恢复脚本先校验该清单再停服务。
# 注意：find -printf '%f\n' 必须是单反斜杠；写成 '%f\\n' 会生成一整行
# 畸形文件名清单，导致 restore.sh 的 sha256sum -c 校验必然失败。
(cd "$DEST" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | sort | xargs -r sha256sum) > "$DEST/SHA256SUMS"
printf 'created_at=%s\nsource=%s\n' "$(date -Is)" "$PROJECT_DIR" > "$DEST/backup.meta"

# 4. 清单自校验：备份产出后立即验证，坏清单当场报错而不是等到恢复时。
(cd "$DEST" && sha256sum -c SHA256SUMS --quiet) || {
  echo "backup failed: SHA256SUMS 自校验未通过" >&2
  exit 1
}

# 5. 清理过期备份
find "${BACKUP_ROOT:-/var/backups/astock-codex}/daily" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true

echo "backup done: $DEST"
ls -lh "$DEST"
