# -*- coding: utf-8 -*-
"""盘后持仓跟踪日报：检查跟踪池并生成 markdown 报告到 astock-quant/reports/
用法: python daily_report.py
优先走本地服务(8600)；服务未启动则直接调用 tracker 模块。
"""
import os, sys, json
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "backend"))
REPORT_DIR = os.path.join(BASE, "reports")


def get_check():
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8600/api/track/check", timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        import tracker
        return tracker.check_positions()


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    d = get_check()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 持仓跟踪日报 {today}", ""]
    port = d.get("portfolio")
    if not d.get("positions"):
        lines.append("跟踪池为空。可在系统「策略选股」页将选出的股票加入跟踪。")
    else:
        lines += [
            f"- 持仓数：{port['count']} | 平均收益：{port['avg_ret_pct']}% | 平均峰值回撤：{port['avg_peak_drawdown']}%",
            f"- 熔断状态：**{port['breaker']}**",
            f"- 卖出提示 {port['sell_signals']} 只 / 关注 {port['warn_signals']} 只",
        ]
        for w in port.get("concentration_warnings") or []:
            lines.append(f"- ⚠️ {w}")
    lines += ["", "| 代码 | 名称 | 成本 | 现价 | 收益% | 峰值回撤% | 持有日 | 卖出决策 | 强制止损 | 次日竞价 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for p in d["positions"]:
        sd = p.get("sell_decision", {})
        stops = "；".join(s["msg"] for s in sd.get("forced_stops", [])) or "-"
        auc = sd.get("auction_matrix", {})
        lines.append(
            f"| {p['code']} | {p['name']} | {p.get('cost')} | {p.get('price')} "
            f"| {p.get('ret_pct')} | {p.get('drawdown_from_peak')} | {p.get('hold_days')} "
            f"| **{sd.get('action', '-')}** ({sd.get('summary', '')}) | {stops} | {auc.get('tier', '-')} {auc.get('action', '-')} |")
    if port and port.get("suggested_weights_pct"):
        lines += ["", "**波动率倒数仓位建议**：" + "，".join(
            f"{c}:{w}%" for c, w in port["suggested_weights_pct"].items())]
    lines += ["", f"> 检查时间 {d.get('checked_at')}。{d.get('disclaimer')}"]
    path = os.path.join(REPORT_DIR, f"track_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("报告已生成:", path)
    print("\n".join(lines[:12]))
    return path


if __name__ == "__main__":
    main()
