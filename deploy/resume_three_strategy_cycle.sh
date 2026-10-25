#!/usr/bin/env bash
# 三策略新周期启动器：只在"标记文件存在 + 当前周期是 paused"时恢复运行。
#
# 背景：2026-10-25 结束了五策略周期 13，并创建了 paused 的周期 14
# （三策略、预算 30 万、权重 20/50/30）。本脚本由 /etc/cron.d/astock-three-strategy-start
# 在 2026-10-26 09:05 触发一次；成功后立即删除标记文件，之后再跑都是空转。
# 任何一步失败都保留标记文件与日志，等待人工介入。
set -euo pipefail

ROOT="${ASTOCK_DEPLOY_ROOT:-/root/codex}"
FLAG="$ROOT/.start_three_strategy_cycle"
LOG_PREFIX="$(date -Is)"

if [ ! -f "$FLAG" ]; then
  echo "$LOG_PREFIX no flag file, nothing to do"
  exit 0
fi

echo "$LOG_PREFIX starting three-strategy cycle 14"
OUTPUT="$(docker exec astock-codex python -c "
import sys
sys.path.insert(0, '/app/backend')
import paper_trading as PT
PT.init_db()
with PT._db() as conn:
    cycle = PT._active_cycle(conn)
    status = cycle['status']
    enabled = len(cycle['enabled_strategies'] or ())
print('cycle', cycle['id'], status, enabled)
if status != 'paused':
    raise SystemExit(3)
PT.set_accounts_status('running')
print('started')
" 2>&1)"
echo "$OUTPUT"
if printf '%s' "$OUTPUT" | grep -q '^started$'; then
  rm -f "$FLAG"
  echo "$LOG_PREFIX cycle 14 is running"
  exit 0
fi
echo "$LOG_PREFIX could not start the cycle; flag kept for manual retry" >&2
exit 1
