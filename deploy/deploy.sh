#!/bin/bash
# A股模拟盘一键部署脚本
# 用法：bash deploy/deploy.sh [--no-backup] [--no-migrate]
# 流程：备份 → 构建镜像 → 重启容器 → schema迁移 → cron同步 → 健康检查
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
COMPOSE="docker compose -f docker-compose.server.yml"

echo "════════════════════════════════════════"
echo " A股模拟盘一键部署 $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════"

# 1. 备份（默认开启）
if [[ "${1:-}" != "--no-backup" ]]; then
  echo "▶ [1/6] 备份当前数据..."
  bash deploy/backup.sh
else
  echo "▶ [1/6] 跳过备份（--no-backup）"
fi

# 2. 构建镜像。镜像上下文明确排除了 .git，因此把唯一允许进入运行时的
# 版本信息限定为当前 commit short hash；策略回放只落这个值，不复制环境。
echo "▶ [2/6] 构建镜像..."
ASTOCK_GIT_COMMIT="$(git rev-parse --short=12 HEAD)"
if [[ ! "$ASTOCK_GIT_COMMIT" =~ ^[0-9a-fA-F]{7,12}$ ]]; then
  echo "❌ 无法确定当前 git commit，拒绝构建不可追溯镜像"
  exit 1
fi
export ASTOCK_GIT_COMMIT
$COMPOSE build --build-arg "ASTOCK_GIT_COMMIT=$ASTOCK_GIT_COMMIT"

# 3. 重启容器
echo "▶ [3/6] 重启容器..."
$COMPOSE up -d
sleep 8

# 4. schema 迁移
if [[ "${1:-}" != "--no-migrate" ]]; then
  echo "▶ [4/6] 执行 schema 迁移..."
  docker exec astock-codex python /app/backend/db_migrate.py all
else
  echo "▶ [4/6] 跳过迁移（--no-migrate）"
fi

# 5. cron 同步（模板里的 /opt/astock-codex 跟随本次部署的实际根目录，防止
#    覆盖服务器手工版后出现"路径指向不存在的目录 → 所有任务静默失败"）。
#    2026-09-08 事故：仓库模板路径 /opt/astock-codex 覆盖了服务器 /root/codex
#    手工版，/opt 下无 deploy/reports，11:06 起盘中监控全部秒失败。
if [[ "$(id -u)" == "0" && -d /etc/cron.d ]]; then
  echo "▶ [5/6] 同步 cron（路径随部署目录 $ROOT）..."
  cp -a /etc/cron.d/astock-codex "/etc/cron.d/astock-codex.bak-$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  sed "s|/opt/astock-codex|$ROOT|g" deploy/astock-codex.cron > /etc/cron.d/astock-codex
  chmod 644 /etc/cron.d/astock-codex
else
  echo "▶ [5/6] 跳过 cron 同步（非 root 或无 /etc/cron.d）"
fi

# 6. 健康检查
echo "▶ [6/6] 健康检查..."
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
