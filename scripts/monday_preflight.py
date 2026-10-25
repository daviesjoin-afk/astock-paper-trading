"""周一开盘前的只读自检：账本、三策略、预算、竞价归档、调度契约。

用法（在 API 容器里跑，容器内已有 backend 与数据目录）::

    docker exec astock-codex python /app/scripts/monday_preflight.py
    docker exec astock-codex python /app/scripts/monday_preflight.py --json

它**只读**：不建表、不补周期、不改权重、不联网、不下单。任何一项 FAIL 都会让退出码
变成 1；WARN 只提示，不阻塞。宿主机侧的检查（git 身份、镜像构建参数、容器健康、
cron 文件）在 ``deploy/preflight.sh`` 里，两者合起来才算"三端一致"。
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

CN = dt.timezone(dt.timedelta(hours=8))


def _check(rows, name, ok, detail, *, warn=False):
    rows.append({"check": name, "status": "PASS" if ok else ("WARN" if warn else "FAIL"),
                 "detail": str(detail)[:400]})
    return ok


def run(now=None):
    import strategy_roster as ROSTER
    import paper_trading as PT
    import runtime_settings as RSET
    import paper_cycle_ownership as PCY
    import strategy_registry as SR

    current = (now or dt.datetime.now(CN)).astimezone(CN)
    rows = []
    PT.init_db()
    with PT._db() as conn:
        cycle = PT._active_cycle(conn)
        # _active_cycle 返回账本原始行：enabled_strategies 是 JSON 文本而不是列表。
        raw_enabled = cycle.get("enabled_strategies")
        enabled = (tuple(PT._loads(raw_enabled, []) or ())
                   if isinstance(raw_enabled, str) else tuple(raw_enabled or ()))
        _check(rows, "cycle_window_status", cycle["status"] in ("paused", "running"),
               f"当前周期 {cycle['id']} 状态 {cycle['status']}")
        _check(rows, "cycle_three_sleeves", enabled == ROSTER.formal_strategy_ids(),
               f"启用集合 {enabled}（期望 {ROSTER.formal_strategy_ids()}）")
        _check(rows, "cycle_capital", abs(float(cycle.get("capital") or 0) - 300000.0) < 0.01,
               f"资本 {cycle.get('capital')}")
        accounts = {row["id"]: dict(row) for row in conn.execute(
            "SELECT id,initial_cash,cash,params,cycle_id FROM paper_accounts WHERE cycle_id=?",
            (cycle["id"],))}
        expected_share = {item.strategy_id: item.budget_share for item in ROSTER.formal_roles()}
        for strategy_id, share in expected_share.items():
            account = accounts.get(strategy_id)
            if account is None:
                _check(rows, f"account_present:{strategy_id}", False, "当前周期没有该账户")
                continue
            want = round(float(cycle["capital"]) * share, 2)
            _check(rows, f"account_capital:{strategy_id}",
                   abs(float(account["initial_cash"]) - want) < 1.0,
                   f"归属 {account['initial_cash']:.2f}（期望 {want:.2f}）")
            params = json.loads(account["params"] or "{}")
            allocation = params.get("adaptive_allocation") or {}
            weight = allocation.get("weight_pct")
            _check(rows, f"allocation_declared:{strategy_id}",
                   weight is not None and abs(float(weight) - share * 100) < 0.01,
                   f"权重 {weight}（期望 {share * 100:.1f}）eff={allocation.get('effective_date')}")
            policy = params.get("adaptive_allocation_policy") or {}
            _check(rows, f"daily_policy:{strategy_id}",
                   policy.get("id") == "authorized-daily-performance-v2"
                   and policy.get("min_weight_pct") == 5.0,
                   f"policy={policy.get('id')} bounds={policy.get('min_weight_pct')}-{policy.get('max_weight_pct')}",
                   warn=policy.get("id") != "authorized-daily-performance-v2")
        participants = PCY.execution_participant_ids(
            conn, cycle["id"], builtin_scope=tuple(SR.active_ids(conn=conn)))
        _check(rows, "execution_participants", tuple(participants) == ROSTER.formal_strategy_ids(),
               f"执行参与者 {participants}")
        scope = RSET.enabled_strategies(conn)
        _check(rows, "next_cycle_enabled", tuple(scope) == ROSTER.formal_strategy_ids(),
               f"下一周期启用 {scope}", warn=tuple(scope) != ROSTER.formal_strategy_ids())
        orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
        _check(rows, "clean_ledger", orders == 0, f"活动账本订单 {orders} 条", warn=True)

    ledger = Path(PT.DB_PATH)
    _check(rows, "ledger_readable", ledger.is_file(),
           f"{ledger} {ledger.stat().st_size if ledger.is_file() else 0} bytes")
    try:
        probe = sqlite3.connect(Path(PT.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        probe.execute("SELECT 1").fetchone()
        probe.close()
        readable = True
    except sqlite3.Error as exc:
        readable = False
        _check(rows, "ledger_ro_open", False, exc)
    if readable:
        _check(rows, "ledger_ro_open", True, "只读连接可用")

    import auction_tracking as AT
    folder = Path(AT.data_paths.data_path("auction_tracking"))
    _check(rows, "auction_dir_writable", os.access(folder if folder.exists() else folder.parent, os.W_OK),
           f"竞价归档目录 {folder}（{'已存在' if folder.exists() else '尚未创建，开盘后写入'}）")
    archived = AT.archived_days()
    _check(rows, "auction_archive", True,
           f"已归档 {len(archived)} 天" + (f"，最近 {archived[-1].stem}" if archived else "（10-26 起逐日落档）"),
           warn=not archived)

    import paper_schedule_contract as SC
    task = next((item for item in SC.SCHEDULE_TASKS if item.slot == "auction-track"), None)
    _check(rows, "schedule_auction_track", task is not None and getattr(task, "clock", None) == "09:15",
           f"竞价跟踪时钟 {getattr(task, 'clock', None) if task else '缺失'}")

    import data_fetcher as dfc  # noqa: F401  (确认数据层可导入)
    kline_dir = Path(getattr(dfc, "KLINE_DIR", "")) if getattr(dfc, "KLINE_DIR", None) else None
    if kline_dir is not None:
        count = len(list(kline_dir.glob("*.csv"))) if kline_dir.is_dir() else 0
        _check(rows, "kline_cache", count > 0, f"{kline_dir} 下 {count} 个 CSV", warn=count == 0)

    failures = [row for row in rows if row["status"] == "FAIL"]
    warnings = [row for row in rows if row["status"] == "WARN"]
    return {"as_of": current.isoformat(), "checks": rows,
            "summary": {"pass": len(rows) - len(failures) - len(warnings),
                        "warn": len(warnings), "fail": len(failures)},
            "ok": not failures}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    report = run()
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=1))
    else:
        print(f"周一开盘前自检 {report['as_of']}")
        for row in report["checks"]:
            print(f"  [{row['status']}] {row['check']}: {row['detail']}")
        summary = report["summary"]
        print(f"合计 PASS={summary['pass']} WARN={summary['warn']} FAIL={summary['fail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
