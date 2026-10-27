#!/bin/bash
# A股模拟盘一键部署脚本
# 用法：bash deploy/deploy.sh [--backup] [--no-migrate]
# 流程：备份(可选) → 构建镜像 → 重启容器 → schema迁移 → cron同步 → 健康检查 → 写部署元数据
#
# 数据库备份默认**关闭**：部署只替换代码，不改账本数据，而每次全量复制
# paper_trading(2.9G)+adaptive_learning(1G) 会拖慢部署并挤占磁盘。数据保护
# 由每日 20:10 的 cron 备份负责。确实要在迁移前留档时显式加 --backup。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)
COMPOSE="docker compose -f docker-compose.server.yml"

echo "════════════════════════════════════════"
echo " A股模拟盘一键部署 $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════"

# 1. 备份（默认关闭，显式 --backup 才执行）
if [[ "${1:-}" == "--backup" ]]; then
  echo "▶ [1/6] 备份当前数据..."
  bash deploy/backup.sh
else
  echo "▶ [1/6] 跳过数据库备份（部署不改数据；每日 cron 备份仍在运行）"
fi

# 2. 构建镜像。镜像上下文明确排除了 .git，因此把完整 commit SHA
# 显式传入构建参数；ExperimentSpec 只接受完整 lowercase SHA。
echo "▶ [2/6] 构建镜像..."
ASTOCK_GIT_COMMIT="$(git rev-parse HEAD)"
if [[ ! "$ASTOCK_GIT_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
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
if [[ "$(id -u)" == "0" && -d "${ASTOCK_CRON_DIR:-/etc/cron.d}" ]]; then
  echo "▶ [5/6] 同步 cron（路径随部署目录 $ROOT）..."
  ASTOCK_DEPLOY_ROOT="$ROOT" bash deploy/sync-cron.sh
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
  # 7. 部署元数据：把这次真正跑起来的提交与镜像身份写进 .deploy-source.json。
  #    该文件此前无人自动维护（2026-10-27 核查时仍指向上一版），而"服务器
  #    跑的是哪个提交"只能靠它一眼看出。放在健康检查**之后**写：失败的部署
  #    不会把自己的身份留在文件里。非 root 或目录不可写时跳过并明确提示。
  echo "▶ 写入部署元数据..."
  META="$ROOT/.deploy-source.json"
  IMAGE_ID=$(docker inspect astock-codex --format '{{.Image}}' 2>/dev/null | sed 's/^sha256://')
  BASE_COMMIT=$(git rev-parse --verify --quiet HEAD^1 || true)
  if git symbolic-ref --quiet --short HEAD >/dev/null 2>&1; then
    SOURCE_REF="github:$(git symbolic-ref --quiet --short HEAD)"
  else
    SOURCE_REF="detached:$(git rev-parse --short HEAD)"
  fi
  META_TMP="$(mktemp)"
  {
    echo "commit=$(git rev-parse HEAD)"
    echo "image=${IMAGE_ID}"
    echo "base_commit=${BASE_COMMIT}"
    echo "deployed_at=$(date -Iseconds)"
    echo "source=${SOURCE_REF}"
    echo "health=${HEALTH}"
    echo "api_health=${API}"
  } > "$META_TMP"
  if install -m 0644 "$META_TMP" "$META" 2>/dev/null; then
    echo "  $META"
  else
    echo "  跳过：$META 不可写（非 root 或只读目录）" >&2
  fi
  rm -f "$META_TMP"
  echo "════════════════════════════════════════"
  echo "✅ 部署成功！"
  echo "════════════════════════════════════════"
else
  echo "❌ 部署异常：health=$HEALTH api=$API，请检查日志"
  docker logs astock-codex --tail 30 2>&1 || true
  exit 1
fi
