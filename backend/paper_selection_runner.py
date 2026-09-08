# -*- coding: utf-8 -*-
"""「策略选股」盘后自动选股 CLI。

用法
----
  python paper_selection_runner.py --slot daily              # 5 套策略各取 Top5 并覆盖当天结果
  python paper_selection_runner.py --slot daily --topn 3 --strategy 策略1
  python paper_selection_runner.py --status                  # 查看最近一次运行结果

调度约定：交易日盘后 17:25 由 cron 触发（17:55 兜底重试），与
selection_runner.py（研究策略快照）错开；重跑按 (交易日, 策略编号) 覆盖。
"""
import argparse
import json
import sys

import paper_selection as PS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", choices=["daily"], default="daily")
    parser.add_argument("--topn", type=int, default=PS.DEFAULT_TOPN)
    parser.add_argument("--strategy", default="",
                        help="只跑指定策略（编号或 strategy_id），逗号分隔")
    parser.add_argument("--date", default="", help="指定交易日（默认今天）")
    parser.add_argument("--status", action="store_true", help="只查看最近一次结果")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(PS.latest(), ensure_ascii=False))
        return 0

    strategies = [item.strip() for item in str(args.strategy or "").split(",") if item.strip()]
    result = PS.run_daily(
        strategies=strategies or None,
        topn=args.topn,
        run_date=args.date or None,
        source="manual" if strategies else "scheduled",
    )
    summary = {
        "trade_date": result["trade_date"],
        "topn": result["topn"],
        "strategies": [
            {"no": item["no"], "strategy_id": item["strategy_id"],
             "name": item["strategy_name"], "status": item["status"],
             "picks": len(item["picks"]), "message": item["message"]}
            for item in result["strategies"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False))
    # 数据门禁未通过不算脚本失败，避免 cron 重试风暴；只有异常状态才报错。
    return 0 if all(item["status"] != "error" for item in result["strategies"]) else 1


if __name__ == "__main__":
    sys.exit(main())
