#!/bin/bash
# A股模拟盘一键部署脚本
# 用法：bash deploy/deploy.sh [--no-backup] [--no-migrate]
# 流程：备份 → 构建镜像 → 重启容器 → schema迁移 → 健康检查 → 最终验证
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
COMPOSE="docker compose -f docker-compose.server.yml"

echo "════════════════════════════════════════"
echo " A股模拟盘一键部署 $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════"

# 1. 备份（默认开启）
if [[ "${1:-}" != "--no-backup" ]]; then
  echo "▶ [1/5] 备份当前数据..."
  bash deploy/backup.sh
else
  echo "▶ [1/5] 跳过备份（--no-backup）"
fi

# 2. 构建镜像
echo "▶ [2/5] 构建镜像..."
$COMPOSE build

# 3. 重启容器
echo "▶ [3/5] 重启容器..."
$COMPOSE up -d
sleep 8

# 4. schema 迁移
if [[ "${1:-}" != "--no-migrate" ]]; then
  echo "▶ [4/5] 执行 schema 迁移..."
  docker exec astock-codex python /app/backend/db_migrate.py all
else
  echo "▶ [4/5] 跳过迁移（--no-migrate）"
fi

# 5. 健康检查
echo "▶ [5/5] 健康检查..."
HEALTH=$(docker inspect --format '{{.State.Health.Status}}' astock-codex 2>/dev/null || echo unknown)
echo "  容器状态: $HEALTH"
API=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:18600/api/health 2>/dev/null || echo 000)
echo "  API health: $API"
METRICS=$(curl -s --max-time 15 http://127.0.0.1:18600/metrics 2>/dev/null | grep -c '^astock_' || true)
echo "  /metrics 指标数: $METRICS"

if [[ "$HEALTH" == "healthy" && "$API" == "200" ]]; then
  echo "════════════════════════════════════════"
  echo "✅ 部署成功！"
  echo "════════════════════════════════════════"
else
  echo "❌ 部署异常：health=$HEALTH api=$API，请检查日志"
  docker logs astock-codex --tail 30 2>&1 || true
  exit 1
fi
