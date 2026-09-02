# -*- coding: utf-8 -*-
"""自进化闭环引擎的 CLI 入口（镜像 adaptive_runner 风格）。

用法示例
--------
  # 跑 3 代（自动续跑被中断的代），受 50s 预算约束
  python evolution_loop_runner.py --generations 3 --time-budget 50

  # 仅查询当前闭环状态
  python evolution_loop_runner.py --status

  # 不续跑被中断的代（从头开新代）
  python evolution_loop_runner.py --generations 1 --no-resume

设计：本 runner 与 adaptive_runner 的盘后学习读写同一份 SQLite
（data_cache/adaptive_learning.sqlite3，D1 统一库），共享 evolution_tracking
样本与 evolution_params 版本，互不阻塞。建议在 cron 中低频调度
（如每个交易日收盘后），由 time-budget 保证不跨时段久占。
"""
import argparse
import json
import sqlite3

import evolution_loop as EL
from evolution_loop import ProductionBackend
import self_evolution as SE  # B1：接线确保 self_evolution 表存在且已有初始参数版本


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    # B1 接线缺口修复：进化闭环的 MUTATE 阶段依赖 self_evolution 的
    # evolution_tracking / evolution_params 等表，首次运行前必须建表并初始化参数版本，
    # 否则 MUTATE 会抛 no such table。
    EL.ensure_loop_schema(conn)
    SE.ensure_schema(conn)
    SE.init_params(conn)  # 幂等：仅首次写入默认参数版本
    return conn


def main():
    parser = argparse.ArgumentParser(description="自进化逻辑闭环驱动")
    parser.add_argument("--db", default=None,
                        help="SQLite 路径（默认用 paper_trading 的 DB_PATH）")
    parser.add_argument("--generations", type=int, default=1)
    parser.add_argument("--time-budget", type=float, default=None,
                        help="单轮最大运行秒数，超时优雅收尾")
    parser.add_argument("--no-resume", action="store_true",
                        help="不续跑被中断的代，直接开新代")
    parser.add_argument("--status", action="store_true",
                        help="仅打印闭环状态后退出")
    args = parser.parse_args()

    if args.db:
        db_path = args.db
    else:
        try:
            # D1：统一库。进化闭环与盘后学习（adaptive_engine）共用
            # adaptive_learning.sqlite3，EVALUATE 才能读到 learning cycle
            # 经 track_run 写入的 evolution_tracking 样本，参数演化才有据可依。
            import adaptive_engine as AE
            db_path = AE.DB_PATH
        except Exception:
            db_path = "data_cache/adaptive_learning.sqlite3"

    conn = _connect(db_path)
    try:
        if args.status:
            status = EL.loop_status(conn)
            print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
            return

        report = EL.run_loop(
            conn,
            ProductionBackend(),
            generations=args.generations,
            time_budget_seconds=args.time_budget,
            resume=not args.no_resume,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
