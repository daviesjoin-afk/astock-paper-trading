# -*- coding: utf-8 -*-
"""D1 诊断：真实库闭环断点定位（只读）。"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
DB = "/app/data_cache/adaptive_learning.sqlite3"


def q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:
        return [("ERR", str(e))]


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print("== evolution_tracking ==")
    rows = q(conn, "SELECT status, COUNT(*) c, MAX(created_at) latest FROM evolution_tracking GROUP BY status")
    for r in rows:
        print(f"  {r['status']}: count={r['c']} latest={r['latest']}")
    n = q(conn, "SELECT COUNT(*) c FROM evolution_tracking")[0]["c"]
    print(f"  TOTAL={n}")

    print("== 最近10条调参 ==")
    for r in q(conn, """SELECT id, status, applied, eval_score, consensus_confidence, market_regime, created_at
                        FROM evolution_tracking ORDER BY id DESC LIMIT 10"""):
        print(f"  id={r['id']} status={r['status']} applied={r['applied']} eval={r['eval_score']} "
              f"conf={r['consensus_confidence']} regime={r['market_regime']} at={r['created_at']}")

    print("== evolution_log 最近10条 ==")
    for r in q(conn, "SELECT id, event_type, detail, created_at FROM evolution_log ORDER BY id DESC LIMIT 10"):
        d = (r['detail'] or "")[:90]
        print(f"  id={r['id']} event={r['event_type']} detail={d} at={r['created_at']}")

    print("== evolution_loop_state ==")
    for r in q(conn, "SELECT * FROM evolution_loop_state ORDER BY id DESC LIMIT 5"):
        print(f"  {dict(r)}")

    print("== params_evolution 最近5条 ==")
    for r in q(conn, "SELECT * FROM params_evolution ORDER BY id DESC LIMIT 5"):
        print(f"  {dict(r)}")

    # 本地复算 should_evolve 全链路
    print("== should_evolve 复算 ==")
    import self_evolution as SE
    m = SE.get_performance_metrics(conn, 20)
    print(f"  metrics: {m}")
    should, reason = SE.should_evolve(conn)
    print(f"  should_evolve={should} reason={reason}")
    conn.close()


if __name__ == "__main__":
    main()
