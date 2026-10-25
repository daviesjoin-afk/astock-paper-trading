"""五套内置策略 → 三策略名册：只读审计 + 受控退出两个旧策略。

用法：
    python scripts/three_strategy_switch.py --paper-db <账本.sqlite3>
    python scripts/three_strategy_switch.py --paper-db <账本.sqlite3> \
        --apply --confirm-retire sector_rotation,main_force_top10

默认**只读**：打开 ``mode=ro``、``PRAGMA query_only=ON``，不建表、不补周期、
不写任何一行。``--apply`` 只做一件事——把两个旧策略的**当前精确版本**从允许
正式周期的生命周期状态转到 ``retiring``；账户、持仓、订单、成本、资金与
``paper_cycles.enabled_strategies`` 一律不动，旧持仓仍保留退出与保护性卖出。

它**不**做：卖出持仓、搬迁成本、删除账户、重置收益、写资金权重、启动新周期、
部署或调用任何网络接口。预算权重（20 / 50 / 30）需要按周期声明，脚本只报告中
心差异与冲突，不代改。
"""
import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ROSTER_IDS = ("tq_breakout", "trend_pullback", "sector_rotation",
              "reported_profit_breakout", "main_force_top10")
FORMAL_IDS = ("tq_breakout", "trend_pullback", "reported_profit_breakout")
EXIT_ONLY_IDS = ("sector_rotation", "main_force_top10")
RETIRABLE_STATES = ("paper", "production_sim", "paused", "degraded")


def _ro(path):
    import sqlite3
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _state_rows(conn):
    """每个已知策略的精确版本生命周期状态（缺失时返回空表，不抛异常）。"""
    if "strategy_lifecycle_state" not in _tables(conn):
        return {}
    rows = conn.execute(
        """SELECT s.strategy_id,s.strategy_version,s.strategy_checksum,s.state,s.updated_at
             FROM strategy_lifecycle_state s
             JOIN (SELECT strategy_id,MAX(strategy_version) v
                     FROM strategy_lifecycle_state GROUP BY strategy_id) m
               ON m.strategy_id=s.strategy_id AND m.v=s.strategy_version"""
    ).fetchall()
    return {row["strategy_id"]: dict(row) for row in rows}


def _definition_rows(conn):
    if "strategy_definitions" not in _tables(conn):
        return {}
    rows = conn.execute(
        """SELECT id,name,lifecycle_status,supports_new_cycle,current_version,sort_order
             FROM strategy_definitions"""
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def _cycle(conn):
    if "paper_cycles" not in _tables(conn):
        return None
    row = conn.execute(
        "SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused')"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["enabled_strategies"] = json.loads(result.get("enabled_strategies") or "null")
    except (TypeError, ValueError):
        result["enabled_strategies"] = "unparsable"
    return result


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _accounts(conn):
    """tolerant 读取：缺列走缺省，不因为半个 schema 直接崩。"""
    if "paper_accounts" not in _tables(conn):
        return {}
    available = _columns(conn, "paper_accounts")
    wanted = ("id", "name", "status", "cycle_id", "initial_cash", "cash", "params")
    selected = [name for name in wanted if name in available]
    if "id" not in selected:
        return {}
    rows = conn.execute(
        "SELECT " + ",".join(selected) + " FROM paper_accounts").fetchall()
    result = {}
    for row in rows:
        item = dict(row)
        try:
            params = json.loads(item.pop("params") or "{}")
        except (TypeError, ValueError):
            params = {}
        allocation = params.get("adaptive_allocation") if isinstance(params, dict) else None
        item["allocation_weight_pct"] = (allocation or {}).get("weight_pct")
        item["dynamic_allocation_policy"] = bool(
            isinstance(params, dict) and params.get("adaptive_allocation_policy"))
        result[item["id"]] = item
    return result


def _counts(conn):
    counts = {}
    for table in ("paper_accounts", "paper_orders", "paper_positions", "paper_lots"):
        if table in _tables(conn):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if "paper_accounts" in _tables(conn):
        available = _columns(conn, "paper_accounts")
        exprs = ["COUNT(*) n"]
        for name in ("cash", "initial_cash"):
            exprs.append(f"COALESCE(SUM({name}),0) {name}" if name in available else "0 " + name)
        row = conn.execute("SELECT " + ",".join(exprs) + " FROM paper_accounts").fetchone()
        counts["cash_total"] = round(float(row["cash"]), 2)
        counts["initial_cash_total"] = round(float(row["initial_cash"]), 2)
    return counts


def audit(path):
    """只读快照：不导入 backend 模块，不依赖本地数据目录。"""
    conn = _ro(path)
    try:
        states = _state_rows(conn)
        definitions = _definition_rows(conn)
        accounts = _accounts(conn)
        result = {
            "source": str(path), "mode": "read_only",
            "schema_ready": {
                "strategy_lifecycle_state": "strategy_lifecycle_state" in _tables(conn),
                "strategy_definitions": "strategy_definitions" in _tables(conn),
                "paper_parameter_versions": "paper_parameter_versions" in _tables(conn),
            },
            "cycle": _cycle(conn),
            "counts": _counts(conn),
            "strategies": {},
        }
        for strategy_id in ROSTER_IDS:
            state = states.get(strategy_id) or {}
            definition = definitions.get(strategy_id) or {}
            account = accounts.get(strategy_id) or {}
            result["strategies"][strategy_id] = {
                "name": definition.get("name") or account.get("name"),
                "lifecycle_state": state.get("state") or definition.get("lifecycle_status"),
                "lifecycle_version": state.get("strategy_version", definition.get("current_version")),
                "supports_new_cycle": definition.get("supports_new_cycle"),
                "cycle_id": account.get("cycle_id"),
                "status": account.get("status"),
                "initial_cash": account.get("initial_cash"),
                "cash": account.get("cash"),
                "allocation_weight_pct": account.get("allocation_weight_pct"),
                "dynamic_allocation_policy": account.get("dynamic_allocation_policy"),
                "roster": "formal" if strategy_id in FORMAL_IDS else "exit_only",
            }
        result["retirement_plan"] = _retirement_plan(result["strategies"])
        result["warnings"] = _warnings(result)
        return result
    finally:
        conn.close()


def _retirement_plan(strategies):
    plan = []
    for strategy_id in EXIT_ONLY_IDS:
        item = strategies.get(strategy_id) or {}
        state = item.get("lifecycle_state")
        if state == "retiring":
            action = "already_retiring"
        elif state in RETIRABLE_STATES:
            action = "transition_to_retiring"
        elif state in ("archived", "rejected", "validation_failed"):
            action = "no_action_terminal_state"
        elif state is None:
            action = "unavailable_lifecycle_state"
        else:
            action = "manual_review_required"
        plan.append({
            "strategy_id": strategy_id, "name": item.get("name"),
            "current_state": state, "target_state": "retiring", "action": action,
            "keeps": "账户、持仓、成本与订单不改；旧持仓仍可保护性卖出",
        })
    return plan


def _warnings(report):
    warnings = []
    cycle = report.get("cycle") or {}
    enabled = cycle.get("enabled_strategies")
    if isinstance(enabled, list) and set(EXIT_ONLY_IDS) & set(enabled):
        warnings.append(
            "当前周期 enabled_strategies 仍包含退出策略；它们在周期账本里保留经济所有权，"
            "因此退出独立账户后仍会计入总池占用，符合方案但需在报告中标注。")
    for strategy_id in FORMAL_IDS:
        item = report["strategies"].get(strategy_id) or {}
        if item.get("allocation_weight_pct") is None:
            warnings.append(f"{strategy_id} 没有按周期的分配权重声明；下次开新周期会由名册写入 20/50/30。")
        elif item.get("dynamic_allocation_policy"):
            # v2 的边界是 5%–60%，50% 可以表示；v1 的旧授权在新代码里会变成
            # not_authorized（fail closed），必须在部署后按 v2 重新授权。
            warnings.append(
                f"{strategy_id} 存在每日动态分配策略：v2 规则允许 5%–60%，与 20/50/30 兼容；"
                "但 v1 旧授权在新代码中会被判为 not_authorized，需按 v2 重新授权后才继续调权。")
    if not report["schema_ready"]["strategy_lifecycle_state"]:
        warnings.append("账本缺少 strategy_lifecycle_state 表：无法在这里切换生命周期。")
    return warnings


def apply_retirement(path, confirmed_ids):
    """把一个或多个旧策略的当前版本转入 retiring；只动生命周期状态行。"""
    sys.path.insert(0, str(BACKEND))
    import sqlite3

    import strategy_lifecycle as SL
    import strategy_registry as SR

    if sorted(confirmed_ids) != sorted(EXIT_ONLY_IDS):
        raise SystemExit("--confirm-retire 必须恰好是 " + ",".join(EXIT_ONLY_IDS))
    before = audit(path)
    steps = []
    conn = sqlite3.connect(str(Path(path).resolve()), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        for strategy_id in EXIT_ONLY_IDS:
            # 事务内重新读取精确版本与状态，不信审计快照（别的进程可能刚改过）。
            head = SR.get(strategy_id, conn=conn)
            if head is None or head.current_version is None:
                raise SystemExit(f"{strategy_id} 缺少注册表版本头，已回滚未做修改")
            record = SR.get_version(strategy_id, int(head.current_version), conn=conn)
            if record is None:
                raise SystemExit(f"{strategy_id} 缺少精确版本快照，已回滚未做修改")
            current = SL.get_state(conn, strategy_id, int(head.current_version),
                                   checksum=record.checksum)
            state = (current or {}).get("state")
            if state == "retiring":
                steps.append({"strategy_id": strategy_id, "result": "already_retiring"})
                continue
            if state not in RETIRABLE_STATES:
                raise SystemExit(
                    f"{strategy_id} 当前状态 {state!r} 不能转移到 retiring，已回滚未做修改")
            SL.transition(
                conn, strategy_id=strategy_id, strategy_version=int(head.current_version),
                strategy_checksum=record.checksum, expected_state=state,
                target_state="retiring", actor_type="human",
                actor_id="three-strategy-switch",
                transition_kind="safety",
                reason_code="five_to_three_retirement",
                reason_text=("三策略名册：该策略退出独立资金账户，保留旧持仓退出与保护性卖出"),
                evidence={"roster": "three-strategy-roster-v1",
                          "previous_state": state},
            )
            steps.append({"strategy_id": strategy_id, "result": "retired",
                          "from_state": state, "to_state": "retiring",
                          "version": int(head.current_version)})
        after_counts = _counts(conn)
        if after_counts != before["counts"]:
            raise SystemExit(
                "账户/持仓/订单计数或资金合计发生变化，已回滚（切换只允许改生命周期）")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    after = audit(path)
    return {"steps": steps, "before": before, "after": after,
            "verified": verify_switch(after)}


BUDGET_REASON = "三策略名册初始预算 20/50/30"


def write_budget_weights(path, *, effective_date=None):
    """给三个正式策略**追加**一条按周期的预算声明（20 / 50 / 30）。

    只追加、不删除历史：写入 ``paper_accounts.params`` 的当前值，并在
    ``paper_parameter_versions`` 里留一条带时区的历史行（分配权重解析器会跳过
    naive 时间戳）。``adaptive_allocation_previous`` 保留被替换的前值，回滚只需
    再追加一条旧值。退出策略与用户策略一律不碰。
    """
    import datetime as dt

    sys.path.insert(0, str(BACKEND))
    import sqlite3

    import strategy_roster as ROSTER

    day = str(effective_date or dt.date.today().isoformat())[:10]
    stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(Path(path).resolve()), timeout=15)
    conn.row_factory = sqlite3.Row
    steps = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for strategy_id in ROSTER.formal_strategy_ids():
            row = conn.execute(
                "SELECT id,cycle_id,params,version,style FROM paper_accounts WHERE id=?",
                (strategy_id,)).fetchone()
            if row is None:
                raise SystemExit(f"账本缺少账户 {strategy_id}，已回滚未做修改")
            params = json.loads(row["params"] or "{}")
            previous = params.get("adaptive_allocation")
            target = ROSTER.initial_params(strategy_id, effective_date=day)["adaptive_allocation"]
            if previous == target:
                steps.append({"strategy_id": strategy_id, "result": "already_declared",
                              "weight_pct": target["weight_pct"]})
                continue
            params["adaptive_allocation_previous"] = previous
            params["adaptive_allocation"] = target
            payload = json.dumps(params, ensure_ascii=False)
            conn.execute("UPDATE paper_accounts SET params=?, updated_at=? WHERE id=?",
                         (payload, stamp, strategy_id))
            conn.execute(
                "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,"
                "reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (row["cycle_id"], strategy_id, row["version"], row["style"], payload,
                 BUDGET_REASON, day, stamp))
            conn.execute(
                "INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
                (strategy_id, "roster_budget_declared",
                 json.dumps({"weight_pct": target["weight_pct"], "effective_date": day,
                             "previous": previous, "roster": ROSTER.ROSTER_VERSION},
                            ensure_ascii=False), stamp))
            steps.append({"strategy_id": strategy_id, "result": "declared",
                          "weight_pct": target["weight_pct"],
                          "previous_weight_pct": (previous or {}).get("weight_pct")})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    report = audit(path)
    declared = {row["strategy_id"]: row["weight_pct"] for row in steps}
    return {"steps": steps, "after": report,
            "verified": verify_budget(report, declared)}


def verify_budget(report, declared):
    """预算声明后的验收：三个正式策略的权重必须正好是 20 / 50 / 30。"""
    problems = []
    expected = {"tq_breakout": 20.0, "trend_pullback": 50.0, "reported_profit_breakout": 30.0}
    for strategy_id, weight in expected.items():
        item = report["strategies"].get(strategy_id) or {}
        actual = item.get("allocation_weight_pct")
        if actual is None or abs(float(actual) - weight) > 1e-6:
            problems.append(f"{strategy_id} 权重为 {actual}，期望 {weight}")
    for strategy_id in EXIT_ONLY_IDS:
        item = report["strategies"].get(strategy_id) or {}
        # 退出策略的历史权重行会保留（旧周期确实按它分过钱），只要它们不再是
        # 执行参与者就不参与分配；这里只做提示，不算失败。
        if item.get("allocation_weight_pct") is not None:
            problems_note = (f"{strategy_id} 保留历史权重 "
                             f"{item['allocation_weight_pct']}%（已退出执行，不参与分配）")
            report.setdefault("notes", []).append(problems_note)
    return {"ok": not problems, "problems": problems, "declared": declared}


def verify_switch(report):
    """切换后的验收断言；任何一条不成立都返回 False 与原因。"""
    problems = []
    for strategy_id in EXIT_ONLY_IDS:
        state = (report["strategies"].get(strategy_id) or {}).get("lifecycle_state")
        if state != "retiring":
            problems.append(f"{strategy_id} 未进入 retiring：{state}")
    for strategy_id in FORMAL_IDS:
        state = (report["strategies"].get(strategy_id) or {}).get("lifecycle_state")
        if state not in ("paper", "production_sim"):
            problems.append(f"{strategy_id} 不再是正式策略状态：{state}")
    return {"ok": not problems, "problems": problems}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--paper-db", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="把两个旧策略转入 retiring（默认只读）")
    parser.add_argument("--confirm-retire", default="",
                        help="必须显式写成 sector_rotation,main_force_top10")
    parser.add_argument("--compact", action="store_true", help="只打印摘要")
    parser.add_argument("--write-budget-weights", action="store_true",
                        help="给三个正式策略追加 20/50/30 预算声明（写账本，需 --apply）")
    parser.add_argument("--effective-date", default="",
                        help="预算声明的生效日，缺省为今天（YYYY-MM-DD）")
    args = parser.parse_args(argv)

    if not Path(args.paper_db).exists():
        raise SystemExit(f"账本不存在：{args.paper_db}")
    if not args.apply:
        report = audit(args.paper_db)
        print(json.dumps(_summary(report) if args.compact else report,
                         ensure_ascii=True, indent=2))
        return 0
    confirmed = [item.strip() for item in args.confirm_retire.split(",") if item.strip()]
    report = apply_retirement(args.paper_db, confirmed)
    print(json.dumps(report["steps"], ensure_ascii=True, indent=2))
    ok = report["verified"]["ok"]
    if args.write_budget_weights:
        budget = write_budget_weights(args.paper_db,
                                      effective_date=args.effective_date or None)
        print(json.dumps(budget["steps"], ensure_ascii=True, indent=2))
        print(json.dumps({"budget_verified": budget["verified"]},
                         ensure_ascii=True, indent=2))
        ok = ok and budget["verified"]["ok"]
    print(json.dumps({"verified": report["verified"],
                      "after": _summary(report["after"])}, ensure_ascii=True, indent=2))
    return 0 if ok else 1


def _summary(report):
    return {
        "source": report["source"],
        "cycle": report["cycle"],
        "strategies": {key: {"state": value["lifecycle_state"],
                             "roster": value["roster"],
                             "weight_pct": value["allocation_weight_pct"]}
                       for key, value in report["strategies"].items()},
        "retirement_plan": report["retirement_plan"],
        "warnings": report["warnings"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
