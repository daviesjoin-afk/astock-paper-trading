#!/usr/bin/env bash
# 三策略新周期启动器：只在"标记文件存在 + 当前周期是 paused"时恢复运行。
#
# 背景：2026-10-25 结束了五策略周期，并创建了 paused 的周期 15（三策略、预算 30 万、
# 归属 6 万/15 万/9 万）。本脚本由 /etc/cron.d/astock-three-strategy-start 在
# 2026-10-26 09:05 触发一次；成功后立即删除标记文件，之后再跑都是空转。
# 任何一步失败都保留标记文件与日志，等待人工介入。
#
# 不写死周期号：日志里报的是账本当前的真实周期，避免"日志说 14、实际 15"。
set -uo pipefail

ROOT="${ASTOCK_DEPLOY_ROOT:-/root/codex}"
FLAG="$ROOT/.start_three_strategy_cycle"
LOG_PREFIX="$(date -Is)"

if [ ! -f "$FLAG" ]; then
  echo "$LOG_PREFIX no flag file, nothing to do"
  exit 0
fi

echo "$LOG_PREFIX starting the paused three-strategy cycle"
# 显式处理失败：set -e 会让命令替换失败时静默退出，连日志都留不下。
if OUTPUT="$(docker exec astock-codex python -c "
import json
import sys
sys.path.insert(0, '/app/backend')
import paper_trading as PT
PT.init_db()
with PT._db() as conn:
    cycle = PT._active_cycle(conn)
    status = cycle['status']
    enabled = PT._loads(cycle['enabled_strategies'], []) or []
print('cycle', cycle['id'], status, len(enabled), 'sleeves')
if status != 'paused':
    print('already=' + status)
    raise SystemExit(0)
PT.set_accounts_status('running')
print('started=' + str(cycle['id']))
" 2>&1)"; then
  printf '%s\n' "$OUTPUT"
else
  status=$?
  printf '%s\n' "$OUTPUT"
  echo "$LOG_PREFIX start failed with exit $status; flag kept for manual retry" >&2
  exit 1
fi

if printf '%s' "$OUTPUT" | grep -q '^started='; then
  rm -f "$FLAG"
  echo "$LOG_PREFIX cycle is running"
  exit 0
fi
if printf '%s' "$OUTPUT" | grep -q '^already='; then
  rm -f "$FLAG"
  echo "$LOG_PREFIX cycle was not paused; cleared the stale flag"
  exit 0
fi
echo "$LOG_PREFIX unexpected output; flag kept for manual retry" >&2
exit 1
