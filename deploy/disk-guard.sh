#!/usr/bin/env bash
# 磁盘水位守护：根分区超过阈值时清理构建缓存、旧镜像与过期备份。
#
# 触发条件由操作员在 2026-10-20 定下：占用超过 85% 就开始清理，不必每次询问。
# 清理顺序从保守到激进，任何一步失败都不影响后续步骤；永不触碰
# data_cache 下的账本、当日迁移前备份或正在使用的镜像。
set -uo pipefail

THRESHOLD="${ASTOCK_DISK_THRESHOLD:-85}"
KEEP_DAILY="${ASTOCK_DISK_KEEP_DAILY:-7}"
KEEP_IMAGE_TAGS="${ASTOCK_DISK_KEEP_IMAGE_TAGS:-2}"
BACKUP_ROOT="${ASTOCK_BACKUP_ROOT:-/var/backups/astock-codex}"

usage=$(df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ -z "${usage:-}" ] || [ "$usage" -lt "$THRESHOLD" ]; then
  echo "$(date -Is) disk ${usage:-unknown}% below ${THRESHOLD}%, nothing to do"
  exit 0
fi

echo "$(date -Is) disk ${usage}% >= ${THRESHOLD}%, cleaning"
df -h / | tail -1

docker builder prune -f 2>&1 | tail -1 || true

# 保留 latest 与最近若干个回滚/候选标签；其余 astock-codex 镜像删除。
if command -v docker >/dev/null 2>&1; then
  keep_tags=$(docker image ls --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' \
    | grep -P '\tastock-codex:' | grep -v ':latest$' | sort -r \
    | head -n "$KEEP_IMAGE_TAGS" | cut -f2)
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    case "$img" in *:latest) continue;; esac
    if printf '%s\n' "$keep_tags" | grep -qx -- "$img"; then continue; fi
    echo "  remove image $img"
    docker rmi "$img" >/dev/null 2>&1 || echo "    (in use, skipped)"
  done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep '^astock-codex:')
fi

# 每日备份按保留策略裁剪；历史（非 daily）目录只在超过 14 天后删除。
if [ -d "$BACKUP_ROOT/daily" ]; then
  ls -1t "$BACKUP_ROOT/daily" 2>/dev/null | tail -n +$((KEEP_DAILY + 1)) | while IFS= read -r name; do
    [ -n "$name" ] || continue
    echo "  remove backup daily/$name"
    rm -rf "${BACKUP_ROOT:?}/daily/$name"
  done
fi
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name daily -mtime +14 \
  -exec rm -rf {} + 2>/dev/null || true

echo "$(date -Is) after cleanup"
df -h / | tail -1
