#!/bin/bash
# A股模拟盘健康告警检查：磁盘 / 内存 / 容器 / 数据库膨胀
# 输出 0=健康, 1=有告警；告警信息写日志供监控查看
set -u

LOG=/root/codex/reports/health-alert.log
ALERT=0

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# 1. 磁盘使用率
DISK_USED=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [[ "$DISK_USED" -ge 90 ]]; then
  log "[CRIT] disk usage ${DISK_USED}% >= 90%"
  ALERT=1
elif [[ "$DISK_USED" -ge 85 ]]; then
  log "[WARN] disk usage ${DISK_USED}% >= 85%"
  ALERT=1
fi

# 2. 内存可用
MEM_AVAIL=$(free -m | awk '/Mem:/ {print $7}')
if [[ "$MEM_AVAIL" -lt 200 ]]; then
  log "[CRIT] available memory ${MEM_AVAIL}MB < 200MB"
  ALERT=1
fi

# 3. 容器健康
if ! docker inspect --format '{{.State.Health.Status}}' astock-codex 2>/dev/null | grep -q healthy; then
  STATUS=$(docker inspect --format '{{.State.Status}}' astock-codex 2>/dev/null || echo unknown)
  log "[CRIT] astock-codex container not healthy (status=$STATUS)"
  ALERT=1
fi

# 4. API 可用性
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 http://127.0.0.1:18600/api/health 2>/dev/null)
if [[ "$HTTP_CODE" != "200" ]]; then
  log "[CRIT] api health http=$HTTP_CODE"
  ALERT=1
fi

# 5. 数据库膨胀检查（交易库 > 200MB 视为异常膨胀）
DB_SIZE=$(stat -c %s /root/codex/data_cache/paper_trading.sqlite3 2>/dev/null || echo 0)
if [[ "$DB_SIZE" -gt 209715200 ]]; then
  log "[WARN] paper_trading.sqlite3 ${DB_SIZE}B > 200MB, need VACUUM"
  ALERT=1
fi

# 6. 业务任务告警：scheduler 最近错误 / 连续 blocked（竞价失败、数据源故障等）
# 注意：本段必须在 exit 之前执行——此前 exit 写在第 50 行，业务告警永远
# 不会运行。
RECENT_ERR=$(tail -400 /root/codex/reports/scheduler.log 2>/dev/null | grep -c '"error"')
RECENT_BLOCKED=$(tail -400 /root/codex/reports/scheduler.log 2>/dev/null | grep -c 'blocked')
if [[ "$RECENT_ERR" -gt 0 ]]; then
  log "[WARN] scheduler 最近 ${RECENT_ERR} 次任务错误"
  ALERT=1
fi
if [[ "$RECENT_BLOCKED" -ge 3 ]]; then
  log "[WARN] scheduler 最近 ${RECENT_BLOCKED} 次 blocked（可能数据源故障/因子陈旧）"
  ALERT=1
fi

echo "health-check: $( [[ $ALERT -eq 1 ]] && echo ALERT || echo OK )"
exit $ALERT
