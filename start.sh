#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

PORT="${PORT:-8600}"
MODE="auto"
SKIP_INSTALL=0
NO_BROWSER=0
NO_SCHEDULER=0

usage() {
  cat <<'EOF'
Usage: ./start.sh [--local|--docker] [--port PORT] [--skip-install] [--no-browser] [--no-scheduler]

Docker Compose is preferred when available; otherwise a local .venv is used.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) MODE="local" ;;
    --docker) MODE="docker" ;;
    --port)
      shift
      [[ $# -gt 0 ]] || { echo "--port requires a value" >&2; exit 2; }
      PORT="$1"
      ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    --no-scheduler) NO_SCHEDULER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# One-click startup enables the in-process three-minute paper scheduler. Use
# --no-scheduler only when an external scheduler is responsible for all slots.
if [[ "$NO_SCHEDULER" -eq 1 ]]; then
  export ASTOCK_ENABLE_FALLBACK_THREADS=0
else
  export ASTOCK_ENABLE_FALLBACK_THREADS=1
fi

[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || {
  echo "PORT must be between 1 and 65535" >&2
  exit 2
}

open_dashboard() {
  [[ "$NO_BROWSER" -eq 1 ]] && return 0
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:${PORT}/" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "http://localhost:${PORT}/" >/dev/null 2>&1 || true
  fi
}

wait_for_http() {
  local url="http://localhost:${PORT}/api/health"
  for _ in $(seq 1 60); do
    if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "服务尚未通过健康检查，请稍后手动打开 http://localhost:${PORT}/。" >&2
  return 0
}

if [[ "$MODE" != "local" && "$PORT" == "8600" ]] && command -v docker >/dev/null 2>&1 \
  && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "正在构建并启动 Docker 服务..."
  if docker compose up -d --build; then
    wait_for_http
    open_dashboard
    echo "看板已启动：http://localhost:${PORT}/"
    echo "停止 Docker 服务：docker compose down"
    exit 0
  elif [[ "$MODE" == "docker" ]]; then
    echo "Docker Compose 启动失败。" >&2
    exit 1
  else
    echo "Docker 启动失败，将切换到本地模式。" >&2
  fi
elif [[ "$MODE" == "docker" ]]; then
  echo "未找到可用的 Docker Compose，请安装并启动 Docker 后重试。" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON="python"
else
  echo "未找到 Python 3。请安装 Python 3.11 或更高版本后重试。" >&2
  exit 1
fi

VENV_DIR="$ROOT_DIR/.venv"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "正在创建本地 Python 虚拟环境..."
  "$PYTHON" -m venv "$VENV_DIR"
fi
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  if command -v sha256sum >/dev/null 2>&1; then
    REQUIREMENTS_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
  else
    REQUIREMENTS_HASH="$(shasum -a 256 requirements.txt | awk '{print $1}')"
  fi
  INSTALLED_HASH=""
  [[ -f "$VENV_DIR/.requirements.sha256" ]] && INSTALLED_HASH="$(cat "$VENV_DIR/.requirements.sha256")"
  if [[ "$REQUIREMENTS_HASH" != "$INSTALLED_HASH" ]]; then
    echo "正在安装或更新 Python 依赖..."
    "$VENV_PYTHON" -m pip install -r requirements.txt
    printf '%s' "$REQUIREMENTS_HASH" > "$VENV_DIR/.requirements.sha256"
  fi
fi

echo "正在启动本地服务..."
"$VENV_PYTHON" -m uvicorn backend.main:app --host localhost --port "$PORT" --workers 1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
wait_for_http
open_dashboard
echo "看板已启动：http://localhost:${PORT}/"
echo "按 Ctrl+C 停止服务。"
wait "$SERVER_PID"
