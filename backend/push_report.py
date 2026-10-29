# -*- coding: utf-8 -*-
"""按运行时设置发送日报/周报。供调度器调用：``python backend/push_report.py daily``。

设计要点：
- 发送时间来自设置中心，因此调度器每 10 分钟跑一次，由本脚本判断是否落在
  发送窗口内，而不是把时间硬编码进 crontab。
- 同一天同一类型只发一次，用报告目录下的标记文件去重，避免窗口内重复推送。
- 未启用、未配置、不在窗口都返回 exit 0 并打印原因；真正发送失败才返回非 0，
  这样调度器不会把「本来就不该发」当成故障。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

import notify_wxpusher as NOTIFY
import paper_trading as P
import paper_storage as PST
import runtime_settings as RSET

TZ = ZoneInfo("Asia/Shanghai")
WINDOW_MINUTES = 10


def _target_time(kind: str, settings: dict) -> tuple[str, str]:
    """返回 (HH:MM, 不可发送原因)。"""
    if kind == "daily":
        if not settings.get("wxpusher_daily_enabled"):
            return "", "日报未启用"
        return str(settings.get("wxpusher_daily_time") or "15:40"), ""
    if not settings.get("wxpusher_weekly_enabled"):
        return "", "周报未启用"
    weekday = int(settings.get("wxpusher_weekly_weekday") or 5)
    return str(settings.get("wxpusher_weekly_time") or "15:45"), f"weekday:{weekday}"


def _in_window(now: dt.datetime, target: str) -> bool:
    try:
        hour, minute = (int(part) for part in target.split(":"))
    except (TypeError, ValueError):
        return False
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(minutes=WINDOW_MINUTES)


def _report_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("ASTOCK_REPORT_DIR") or "reports")


def main(argv: list[str]) -> int:
    kind = (argv[1] if len(argv) > 1 else "daily").strip().lower()
    if kind == "all":
        # 调度器每 10 分钟只唤醒一次，日报与周报各自判断是否落在自己的窗口。
        return max(main(["", item]) for item in ("daily", "weekly"))
    if kind not in {"daily", "weekly"}:
        print(json.dumps({"sent": False, "reason": "kind 必须是 daily 或 weekly"},
                         ensure_ascii=False))
        return 2
    now = dt.datetime.now(TZ)
    P.init_db()
    with PST.paper_db() as conn:
        settings = RSET.read(conn)["notification"]
        if not settings.get("wxpusher_enabled"):
            print(json.dumps({"sent": False, "reason": "推送总开关未开启"}, ensure_ascii=False))
            return 0
        target, blocked = _target_time(kind, settings)
        if not target:
            print(json.dumps({"sent": False, "reason": blocked}, ensure_ascii=False))
            return 0
        if blocked.startswith("weekday:"):
            if now.isoweekday() != int(blocked.split(":", 1)[1]):
                print(json.dumps({"sent": False, "reason": "今天不是周报日"}, ensure_ascii=False))
                return 0
        if not _in_window(now, target):
            print(json.dumps({"sent": False, "reason": f"不在 {target} 发送窗口"},
                             ensure_ascii=False))
            return 0
        marker = _report_dir() / f".push-{kind}-{now.date().isoformat()}"
        if marker.exists():
            print(json.dumps({"sent": False, "reason": "今天已发送过"}, ensure_ascii=False))
            return 0
        try:
            result = NOTIFY.dispatch(conn, kind, settings, now.date().isoformat())
        except RuntimeError as exc:
            print(json.dumps({"sent": False, "reason": str(exc)}, ensure_ascii=False))
            return 1
    if not result.get("sent"):
        print(json.dumps(result, ensure_ascii=False))
        return 0
    try:
        marker.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        # 标记写不进去只意味着可能重复发送一次，不应把已成功的推送报成失败。
        pass
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
