#!/usr/bin/env bash
# 周一开盘前的宿主机自检：三端一致（git 身份 / 镜像构建参数 / 容器健康）+ cron + 调度锁定。
#
# 用法：bash deploy/preflight.sh
# 只读：不重建、不重启、不改 cron、不碰账本。任何 FAIL 让退出码为 1。
set -u

ROOT="${ASTOCK_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE="docker compose -f $ROOT/docker-compose.server.yml"
FAIL=0
WARN=0

pass() { echo "  [PASS] $1"; }
warn() { echo "  [WARN] $1"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "宿主机自检 $(date '+%Y-%m-%d %H:%M:%S')"

# 1. 三端一致：git HEAD / 镜像构建参数
HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
IMAGE_SHA="$(docker image inspect astock-codex:latest --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || echo unknown)"
if [ "$HEAD_SHA" != "unknown" ] && [ "$HEAD_SHA" = "$IMAGE_SHA" ]; then
  pass "git HEAD 与镜像标签一致：$HEAD_SHA"
else
  fail "git HEAD=$HEAD_SHA 与镜像构建参数=$IMAGE_SHA 不一致（需要重建镜像）"
fi
if [ -z "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then
  pass "工作树干净（无未提交改动）"
else
  fail "工作树有未提交改动：$(git -C "$ROOT" status --porcelain | head -3 | tr '\n' ' ')"
fi

# 2. 容器
for name in astock-codex astock-task-worker astock-paper-worker astock-data-worker; do
  state="$(docker inspect "$name" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
  health="$(docker inspect "$name" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo none)"
  if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "none" ]; }; then
    pass "容器 $name running/$health"
  else
    fail "容器 $name 状态 $state/$health"
  fi
done
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18600/api/health 2>/dev/null || echo 000)"
[ "$code" = "200" ] && pass "API /api/health 200" || fail "API /api/health 返回 $code"

# 3. 调度：竞价跟踪 + 周一启动器
if grep -q "auction-track" /etc/cron.d/astock-codex 2>/dev/null; then
  pass "cron 含 auction-track"
else
  fail "cron 缺少 auction-track"
fi
if [ -x "$ROOT/deploy/resume_three_strategy_cycle.sh" ]; then
  if [ -f "$ROOT/.start_three_strategy_cycle" ]; then
    pass "周一启动器就绪（标记文件存在）"
  else
    warn "周一启动器已安装但标记文件不存在（周期不会自动启动）"
  fi
else
  fail "缺少 deploy/resume_three_strategy_cycle.sh"
fi

# 4. 磁盘与锁目录
FREE_GB="$(df -Pk / | awk 'NR==2 {printf "%d", $4/1024/1024}')"
if [ "${FREE_GB:-0}" -ge 2 ]; then pass "根分区可用 ${FREE_GB}GB"; else fail "根分区仅 ${FREE_GB}GB"; fi
[ -w "$ROOT/.locks" ] && pass "锁目录可写 $ROOT/.locks" || fail "锁目录不可写 $ROOT/.locks"

# 5. 容器内侧自检
if docker exec astock-codex python /app/scripts/monday_preflight.py 2>&1 | sed 's/^/    /'; then
  pass "容器内自检通过"
else
  fail "容器内自检未通过（见上面明细）"
fi

echo "合计 FAIL=$FAIL WARN=$WARN"
[ "$FAIL" -eq 0 ]
