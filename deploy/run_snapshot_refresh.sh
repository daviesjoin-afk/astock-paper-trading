#!/usr/bin/env bash
# Keep the data-worker's shared resource admission; never take a trading lock.
set -euo pipefail

# 锁目录必须跟 cron 模板里的重型任务落在同一个 .locks。
#
# sync-cron.sh 会把模板中的 /opt/astock-codex 改写成实际部署根目录，因此写死
# /opt 的默认值在非默认安装（例如 /root/codex）下会指向另一把锁：selection、
# history_recovery 与展示刷新从此互斥失效，两个任务同时进 data-worker 的
# 650MB cgroup。旧写法把这个差值藏在环境变量后面，运维不设置就静默出错。
#
# 这里与 sync-cron.sh 使用同一种推导：脚本所在目录的上一级即项目根目录，
# 即 <root>/deploy/run_snapshot_refresh.sh -> <root>/.locks。ASTOCK_LOCK_DIR
# 仍可显式覆盖（供测试或非常规布局使用）。
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock_dir="${ASTOCK_LOCK_DIR:-$(cd "$script_dir/.." && pwd)/.locks}"
# 建不了目录就失败，不退到 /tmp：换目录等于换锁，互斥会静默消失。
mkdir -p "$lock_dir"
exec 9>"$lock_dir/resource-heavy.lock"
if ! flock -n 9; then
  printf '{"event":"dashboard_refresh","lane":"dashboard-refresh","status":"skipped","reason":"resource_busy","at":"%s"}
' "$(date -Iseconds)"
  exit 0
fi
docker exec astock-data-worker python backend/paper_snapshot_worker.py
