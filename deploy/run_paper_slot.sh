#!/usr/bin/env bash
# Serialize every paper runner invocation across the host.  Fast-entry and
# intraday scans each import the full strategy stack; running them together in
# one Docker cgroup caused real OOM kills and a stalled audit trail.
set -euo pipefail

slot="${1:?slot is required}"
lock_dir="/opt/astock-codex/.locks"
mkdir -p "$lock_dir"

# ─── 全扫描优先（防止 fast-entry 抢锁把三分钟扫描饿死）────────────────────
#
# 生产实测：`paper-scan.lock` 被 fast-entry 持有时，整点/半点的全扫描只等
# 20 秒就放弃——2026-10-21..28 共 15 次 `not_acquired`，全部是 09:33 开盘
# 审批、10:27/11:06 盘中扫描这种"本来应该跑"的轮次。原因不是锁坏了，而是
# 优先级反了：fast-entry 每分钟两次、每次都要导入整套策略栈，它先拿到锁，
# 全扫描反而成了排队者。
#
# 契约改成两条：
#   1. 全扫描在等待前留下一个带 PID 的 **意图标记**；fast-entry 看到新鲜的
#      标记就直接跳过本轮（下一分钟还有一次，且全扫描本身会做同样的事）。
#   2. 全扫描的等待窗口从 20 秒放宽到 60 秒，但仍小于 3 分钟调度间隔——
#      快任务释放后立即接手，慢任务不会无限排队。
#
# 标记按 PID 命名并设置 TTL：全扫描进程被 SIGKILL 时，残留标记最多影响
# `ASTOCK_FULL_SCAN_INTENT_TTL_SECONDS`（默认 300 秒）的 fast-entry，之后
# 自动被视为陈旧并清理，不会永久饿死快通道。
intent_ttl="${ASTOCK_FULL_SCAN_INTENT_TTL_SECONDS:-300}"
marker="$lock_dir/full-scan-pending.$$"
cleanup_marker() { rm -f "$marker"; }
trap cleanup_marker EXIT INT TERM

if [[ "$slot" == "fast-entry" ]]; then
  now=$(date +%s)
  for intent in "$lock_dir"/full-scan-pending.*; do
    [[ -e "$intent" ]] || continue
    mtime=$(stat -c %Y "$intent" 2>/dev/null || echo 0)
    age=$(( now - mtime ))
    if (( age >= 0 && age < intent_ttl )); then
      printf '{"event":"paper_host_lock","status":"yielded_to_full_scan","slot":"%s","intent":"%s"}\n' \
        "$slot" "$(basename "$intent")" >&2
      exit 0
    fi
    rm -f "$intent"
  done
  lock_options=(-n)
else
  : > "$marker"
  lock_options=(-w 60)
fi

# Keep the same lock for the complete Docker invocation. SECONDS is Bash's
# elapsed timer (one-second resolution); do not confuse this inner lock with
# the outer cron slot lock or Docker/Python startup time.
exec 9>"$lock_dir/paper-scan.lock"
SECONDS=0
if flock "${lock_options[@]}" 9; then
  host_wait="$SECONDS"
  cleanup_marker
else
  result=$?
  printf '{"event":"paper_host_lock","status":"not_acquired","wait_seconds":%s,"exit_code":%s}\n' \
    "$SECONDS" "$result" >&2
  exit "$result"
fi

# 扫描进程的尾部预算：进程内超过 90 秒后，后续 HTTP 请求的超时压到 4 秒、
# 重试压到 1 次；跨进程熔断文件让下一轮不必重新"学习"同一个挂掉的主机。
# 三者都只影响"愿意等多久"，不改变任何 fail-closed / fail-open 判定。
docker exec \
  --env "ASTOCK_PAPER_HOST_LOCK_WAIT_SECONDS=$host_wait" \
  --env "ASTOCK_SCAN_BUDGET_SECONDS=${ASTOCK_SCAN_BUDGET_SECONDS:-150}" \
  --env "ASTOCK_SCAN_TAIL_AFTER_SECONDS=${ASTOCK_SCAN_TAIL_AFTER_SECONDS:-90}" \
  --env "ASTOCK_TRANSPORT_BREAKER_FILE=${ASTOCK_TRANSPORT_BREAKER_FILE:-/app/data_cache/transport_breaker.json}" \
  --env "ASTOCK_MICRO_BATCH_SIZE=${ASTOCK_MICRO_BATCH_SIZE:-6}" \
  astock-paper-worker python backend/paper_runner.py --slot "$slot"
