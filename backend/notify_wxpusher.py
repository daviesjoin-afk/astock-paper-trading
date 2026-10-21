# -*- coding: utf-8 -*-
"""WxPusher 出站推送：日报与周报。

只做两件事：从模拟盘账本**只读**汇总摘要，再通过 HTTPS 把摘要发出去。
不写交易账本、不改任何周期状态。凭据由 runtime_settings 提供，调用方负责
掩码；本模块从不把 appToken 写进日志或异常消息。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ENDPOINT = "https://wxpusher.zjiecode.com/api/send/message"
CONTENT_MARKDOWN = 3

ACCOUNT_LABELS = {
    "tq_breakout": "短线日内做T",
    "trend_pullback": "趋势波段优选",
    "sector_rotation": "板块轮动先锋",
    "reported_profit_breakout": "三日策略",
    "main_force_top10": "超强主力股",
}


def mask_token(token: str) -> str:
    """只回显足以辨认的尾部，任何长度都不会泄露完整 token。"""
    token = str(token or "")
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "*" * (len(token) - 8) + token[-4:]


def _label(account_id: str) -> str:
    return ACCOUNT_LABELS.get(account_id, account_id or "未知策略")


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _nav_rows(conn: sqlite3.Connection, since: str | None = None):
    sql = "SELECT account_id, nav_date, nav, benchmark FROM paper_nav"
    args: tuple = ()
    if since:
        sql += " WHERE nav_date >= ?"
        args = (since,)
    sql += " ORDER BY account_id, nav_date"
    return conn.execute(sql, args).fetchall()


def _daily_stats(conn: sqlite3.Connection, asof: str) -> list[dict]:
    """每个账户：asof 当日净值对比上一净值日，外加当日成交笔数。"""
    rows = _nav_rows(conn)
    by_account: dict[str, list] = {}
    for row in rows:
        by_account.setdefault(row["account_id"], []).append(row)
    fills = {
        row["account_id"]: row["n"]
        for row in conn.execute(
            "SELECT account_id, COUNT(*) n FROM paper_fills WHERE fill_date=? GROUP BY account_id",
            (asof,),
        )
    }
    stats = []
    for account_id, series in by_account.items():
        current = next((r for r in reversed(series) if r["nav_date"] <= asof), None)
        if current is None:
            continue
        previous = next((r for r in reversed(series) if r["nav_date"] < current["nav_date"]), None)
        nav = float(current["nav"] or 0.0)
        prev_nav = float(previous["nav"] or 0.0) if previous else None
        change = (nav / prev_nav - 1.0) * 100.0 if prev_nav else None
        bench = current["benchmark"]
        bench_prev = previous["benchmark"] if previous else None
        bench_change = (
            (float(bench) / float(bench_prev) - 1.0) * 100.0
            if bench and bench_prev and float(bench_prev) else None
        )
        excess = change - bench_change if change is not None and bench_change is not None else None
        stats.append({
            "account_id": account_id, "nav": nav, "nav_date": current["nav_date"],
            "change_pct": change, "benchmark": bench,
            "benchmark_change_pct": bench_change, "excess_pct": excess,
            "fills": int(fills.get(account_id, 0)),
        })
    return sorted(stats, key=lambda item: item["account_id"])


def daily_report(conn: sqlite3.Connection, asof: str | None = None) -> tuple[str, str]:
    """返回 (summary, markdown 正文)。asof 缺省为今天。"""
    asof = asof or dt.datetime.now(TZ).date().isoformat()
    stats = _daily_stats(conn, asof)
    if not stats:
        return (f"模拟盘日报 {asof}", f"## 模拟盘日报 {asof}\n\n当日没有可用的净值记录。")
    bench = next((item["benchmark"] for item in stats if item["benchmark"]), None)
    bench_change = next((item["benchmark_change_pct"] for item in stats
                         if item["benchmark_change_pct"] is not None), None)
    lines = [f"## 模拟盘日报 {asof}", ""]
    if bench is not None:
        lines.append(f"基准 {_money(float(bench))}（{_pct(bench_change)}）")
        lines.append("")
    lines.append("| 策略 | 净值 | 日变动 | 超额 | 成交 |")
    lines.append("|---|---|---|---|---|")
    total_fills = 0
    for item in stats:
        total_fills += item["fills"]
        lines.append(
            f"| {_label(item['account_id'])} | {_money(item['nav'])} | "
            f"{_pct(item['change_pct'])} | {_pct(item['excess_pct'])} | {item['fills']} |"
        )
    lines.append("")
    lines.append(f"当日成交合计 {total_fills} 笔。模拟盘数据，不构成投资建议。")
    summary = f"模拟盘日报 {asof}：成交 {total_fills} 笔"
    return (summary, "\n".join(lines))


def weekly_report(conn: sqlite3.Connection, asof: str | None = None) -> tuple[str, str]:
    """本周（截至 asof）各账户净值变化与超额。"""
    today = dt.date.fromisoformat(asof) if asof else dt.datetime.now(TZ).date()
    start = (today - dt.timedelta(days=6)).isoformat()
    end = today.isoformat()
    rows = _nav_rows(conn, since=start)
    by_account: dict[str, list] = {}
    for row in rows:
        by_account.setdefault(row["account_id"], []).append(row)
    stats = []
    for account_id, series in by_account.items():
        first, last = series[0], series[-1]
        first_nav = float(first["nav"] or 0.0)
        last_nav = float(last["nav"] or 0.0)
        change = (last_nav / first_nav - 1.0) * 100.0 if first_nav else None
        bench_first = first["benchmark"]
        bench_last = last["benchmark"]
        bench_change = (
            (float(bench_last) / float(bench_first) - 1.0) * 100.0
            if bench_first and bench_last and float(bench_first) else None
        )
        excess = change - bench_change if change is not None and bench_change is not None else None
        stats.append({
            "account_id": account_id, "first_date": first["nav_date"],
            "last_date": last["nav_date"], "first_nav": first_nav, "last_nav": last_nav,
            "change_pct": change, "benchmark_change_pct": bench_change, "excess_pct": excess,
        })
    fills = conn.execute(
        "SELECT COUNT(*) FROM paper_fills WHERE fill_date >= ? AND fill_date <= ?",
        (start, end)).fetchone()[0]
    if not stats:
        return (f"模拟盘周报 {end}",
                f"## 模拟盘周报 {end}\n\n{start} ~ {end} 没有可用的净值记录。")
    bench_change = next((item["benchmark_change_pct"] for item in stats
                         if item["benchmark_change_pct"] is not None), None)
    lines = [f"## 模拟盘周报 {end}", "", f"窗口 {start} ~ {end}，基准 {_pct(bench_change)}", ""]
    lines.append("| 策略 | 周末净值 | 周变动 | 超额 |")
    lines.append("|---|---|---|---|")
    for item in sorted(stats, key=lambda x: (x["excess_pct"] is None, -(x["excess_pct"] or 0))):
        lines.append(
            f"| {_label(item['account_id'])} | {_money(item['last_nav'])} | "
            f"{_pct(item['change_pct'])} | {_pct(item['excess_pct'])} |"
        )
    lines.append("")
    lines.append(f"本周成交合计 {int(fills)} 笔。模拟盘数据，不构成投资建议。")
    summary = f"模拟盘周报 {end}：成交 {int(fills)} 笔"
    return (summary, "\n".join(lines))


def send(app_token: str, content: str, summary: str,
         topic_id: str | None = None, timeout: float = 15.0) -> dict:
    """调用 WxPusher 发送接口。失败抛 RuntimeError，消息里不含 token。"""
    app_token = str(app_token or "").strip()
    if not app_token:
        raise RuntimeError("WxPusher appToken 未配置")
    payload = {
        "appToken": app_token,
        "content": content,
        "summary": summary,
        "contentType": CONTENT_MARKDOWN,
    }
    topic_id = str(topic_id or "").strip()
    if topic_id:
        payload["topicIds"] = [int(topic_id)]
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"WxPusher 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"WxPusher 连接失败：{exc.reason}") from exc
    try:
        result = json.loads(body)
    except ValueError as exc:
        raise RuntimeError("WxPusher 返回内容不是 JSON") from exc
    if int(result.get("code") or 0) != 1000:
        raise RuntimeError(f"WxPusher 拒绝发送：{result.get('msg') or result.get('code')}")
    return result


def dispatch(conn: sqlite3.Connection, kind: str, settings: dict,
             asof: str | None = None, *, sender=None) -> dict:
    """按设置发送日报/周报。kind ∈ {daily, weekly}。

    settings 是 ``runtime_settings.read`` 的 notification 分组。
    返回 ``{"sent": bool, "reason": str, "summary": str}``；
    未启用或未配置时 sent=False 并给出原因，不抛异常。
    """
    kind = str(kind or "").strip().lower()
    if kind not in {"daily", "weekly"}:
        raise ValueError("kind 必须是 daily 或 weekly")
    if not settings.get("wxpusher_enabled"):
        return {"sent": False, "reason": "推送总开关未开启", "summary": ""}
    enabled_key = "wxpusher_daily_enabled" if kind == "daily" else "wxpusher_weekly_enabled"
    if not settings.get(enabled_key):
        return {"sent": False, "reason": f"{kind} 未启用", "summary": ""}
    token = str(settings.get("wxpusher_app_token") or "")
    if not token:
        return {"sent": False, "reason": "未配置 appToken", "summary": ""}
    builder = daily_report if kind == "daily" else weekly_report
    summary, content = builder(conn, asof)
    sender = sender or send
    sender(token, content, summary, settings.get("wxpusher_topic_id"))
    return {"sent": True, "reason": "", "summary": summary}
