# -*- coding: utf-8 -*-
"""F5 生产编排：事件入库、worker 接续、配置 owner 与只读实验结果。"""
from __future__ import annotations


def config_connection():
    """模型配置只来自配置 owner，不从研究队列库复制。

    连接工厂由宿主注册在 :mod:`research_runtime_config`；本模块不 import
    ``adaptive_engine``，否则编排 ↔ 学习引擎会直接成一环。
    """
    import research_runtime_config as RRC

    return RRC.configuration_connection()


def wake_experiment(conn, experiment_id: str) -> dict:
    """回执提交后的确定性反馈：标候选完成、唤醒等待者并记录实验事件。

    只对**受限研究候选**生效（其他实验没有候选行）。重复调用是幂等的：已经
    标完成的候选不会再次唤醒、也不会重复投递事件。
    """
    import research_candidate as RC
    import research_pipeline as RP

    candidate = RC.get(conn, experiment_id)
    if candidate is None:
        return {"status": "not_a_research_candidate", "experiment_id": experiment_id}
    if candidate.get("status") == RC.STATUS_COMPLETE:
        return {"status": "already_complete", "experiment_id": experiment_id,
                "woken_task_ids": []}
    progress = RC.result(conn, experiment_id)
    if not progress["complete"]:
        return {"status": "waiting_experiment", "experiment_id": experiment_id}
    RC.mark_complete(conn, experiment_id)
    woken = RC.wake_waiters(conn, experiment_id)
    event = RP.completed_experiment_event(conn, experiment_id)
    return {"status": "complete", "experiment_id": experiment_id,
            "woken_task_ids": sorted(set(woken + event.get("woken_task_ids", [])))}


def wake_completed_experiments(conn, experiment_ids=None) -> list[dict]:
    """worker 收尾：把已经跑完的研究候选推进到"可被模型读到结果"。

    一个候选出错不影响其他候选，也不影响 worker 主循环。
    """
    import sqlite3

    ids = list(experiment_ids or ())
    if experiment_ids is None:
        try:
            ids = [str(row[0]) for row in conn.execute(
                "SELECT experiment_id FROM research_flow_candidates"
                " WHERE status='running' ORDER BY created_at").fetchall()]
        except sqlite3.OperationalError:
            return []
    reports = []
    for experiment_id in ids:
        try:
            reports.append(wake_experiment(conn, experiment_id))
        except Exception as exc:  # noqa: BLE001 - 单个候选的失败不阻断批次
            reports.append({"status": "failed", "experiment_id": str(experiment_id),
                            "error": f"{type(exc).__name__}: {exc}"})
    return reports


def notify_research(event, *, queue_path=None, ledger_conn=None):
    import research_pipeline as RP
    return RP.submit_safe(event, queue_path=queue_path, ledger_conn=ledger_conn)


def notify_fill(*, order_id, account_id, code, side, status, strategy_stamp,
                now=None) -> dict:
    """成交事实的窄投递。研究侧失败只返回失败结果，绝不改变交易结论。"""
    strategy_id, strategy_version, strategy_checksum = tuple(strategy_stamp or (None,) * 3)
    return notify_research(fill_event({
        "id": order_id, "account_id": account_id, "code": code, "side": side,
        "status": status, "executed_at": now, "strategy_id": strategy_id,
        "strategy_version": strategy_version, "strategy_checksum": strategy_checksum}))


def sessions_from_rows(rows, *, decision_at=None) -> list[dict]:
    """将调用方已经冻结的行情/因子行转换为标准回放输入（不伪造行情）。"""
    import native_paired_replay as NPR

    sessions = []
    for row in rows or ():
        table = row["factor_table"]
        quotes = tuple(NPR.ReplayQuote(**quote) for quote in row["quotes"])
        session = NPR.ReplaySession(
            session_date=str(row["session_date"]),
            decision_at=str(row.get("decision_at") or decision_at
                            or f"{row['session_date']}T14:30:00+08:00"),
            quotes=quotes, factor_table=table,
            first_board_codes=tuple(row.get("first_board_codes") or ()))
        sessions.append(NPR.session_payload(session))
    return sessions


def session_from_quotes(quotes, *, session_date: str, decision_at: str | None = None,
                        first_board_codes=(), factor_table=None) -> list[dict]:
    """把交易 owner 已经冻结的收盘行情投影成一次回放会话。

    只搬运调用方给出的字段：没有价格的标的直接跳过，不补价、不换算、不猜
    前收。投影失败由调用方按 fail-open 处理，研究侧不改变交易结论。
    """
    projected = []
    for item in quotes or ():
        row = dict(item or {})
        code = str(row.get("code") or "").strip()
        price = row.get("price")
        if not code or price is None or isinstance(price, bool):
            continue
        projected.append({"code": code, "price": float(price),
                          "prev_close": row.get("prev_close"),
                          "volume": row.get("volume"),
                          "halted": bool(row.get("halted") or False),
                          "name": row.get("name"),
                          "risk_flag": bool(row.get("risk_flag") or False)})
    if not projected:
        return []
    return sessions_from_rows([{
        "session_date": session_date,
        "decision_at": decision_at or f"{session_date}T15:00:00+08:00",
        "quotes": projected, "factor_table": factor_table,
        "first_board_codes": tuple(first_board_codes or ()),
    }])


def fill_event(order) -> dict:
    return {"kind": "fill", "strategy_id": order.get("strategy_id") or order.get("account_id"),
            "code": order.get("code"), "order_id": order.get("id"),
            "observed_at": order.get("executed_at") or order.get("created_at"),
            "side": order.get("side"), "status": order.get("status"),
            "strategy_version": order.get("strategy_version"),
            "strategy_checksum": order.get("strategy_checksum")}


def market_event(strategy_id, *, session_date, environment_ref, exact_identity=None,
                 frozen_sessions=None) -> dict:
    return {"kind": "market", "strategy_id": strategy_id,
            "observed_at": f"{session_date}T15:00:00+08:00",
            "session_date": session_date, "environment_ref": environment_ref,
            "exact_identity": exact_identity,
            "frozen_sessions": list(frozen_sessions or ())}
