# -*- coding: utf-8 -*-
"""研究循环的生产入口：注册处理器、推进已登记实验、报告真实状态。

方案 S1 的第一条要求是"增加明确的生产注册/启动入口"——否则 ``agent_task_runner``
的 ``run`` 只会返回 ``no_handlers``，新模块永远没人调用。

这个入口只做三件事，且都可以在无模型、无网络的情况下验证：

1. **注册**：把所有研究类 handler 装进执行器（目前是成对回放的会话任务）。
2. **推进**：按上限领取并执行若干任务；领取、计算、发布各自短事务。
3. **报告**：打印每个实验的已发布/待跑会话、半侧残留、失败任务与两侧净值。

它**不**在批处理里直接调用模型：模型调用发生在研究 handler 内部，由配置 owner 决定
能不能发。宿主兜底线程也住在这里（``start_research_worker_fallback``），这样
``adaptive_engine`` 不需要 import 本模块——依赖方向是"入口 → 引擎"，不会成环。
"""
from __future__ import annotations

import argparse
import json
import os

import agent_task_queue as AQ
import agent_task_runner as AR
import ai_capability as CAP
import ai_research_agent as AGENT
import experiment_increment as EI
import replay_receipt_ledger as RL
import research_metrics as RM

WORKER_VERSION = "research-worker-v2"

#: 这个入口负责注册的全部研究任务类型。
HANDLERS = {
    EI.TASK_TYPE_SESSION: AR.HandlerSpec(handler=EI.handle_session_task, heavy=True),
    AGENT.TASK_TYPE_RESEARCH: AR.HandlerSpec(handler=AGENT.run_research_task),
}


def bind_config_owner():
    """把设置页配置 owner 绑定到研究运行时（组合根动作，幂等）。

    这是入口对引擎的唯一反向依赖：读槽位配置要打开 ``adaptive_engine`` 的库，
    而引擎不再 import 本模块，所以这条边不会绕回来。
    """
    import adaptive_engine as AE
    import research_runtime_config as RRC

    RRC.bind_config_connection(AE.adaptive_connection)
    return AE.adaptive_connection


def register() -> dict:
    """把研究 handler 注册进执行器模块表（供 ``agent_task_runner`` 的 CLI 复用）。"""
    for task_type in HANDLERS:
        AR.register(task_type, HANDLERS[task_type].handler,
                    heavy=HANDLERS[task_type].heavy)
    return {"registered": sorted(HANDLERS)}


def known_experiments(conn) -> list[str]:
    if not RL.has_table(conn, "experiment_runs"):
        return []
    return [str(row[0]) for row in conn.execute(
        "SELECT experiment_id FROM experiment_runs ORDER BY created_at").fetchall()]


def run_once_pass(conn, *, max_tasks: int = 5, worker_id: str | None = None,
                  clock=None) -> dict:
    """跑一轮：先把每个实验的下一会话补齐，再推进至多 max_tasks 个任务。"""
    now_clock = clock or (lambda: None)
    healed = []
    for experiment_id in known_experiments(conn):
        try:
            task = EI.ensure_next_session_enqueued(conn, experiment_id, now=now_clock())
        except EI.IncrementError:
            continue
        if task is not None:
            healed.append(experiment_id)
    AQ.commit(conn)
    results = AR.run_batch(conn, max_tasks=max_tasks, handlers=HANDLERS,
                           worker_id=worker_id or f"research-{os.getpid()}",
                           clock=now_clock)
    # 回执落库之后才判定"实验完成"：这样被唤醒的模型任务读到的是已提交证据。
    import research_orchestration as RO

    completed = RO.wake_completed_experiments(conn)
    AQ.commit(conn)
    return {"healed_experiments": sorted(set(healed)), "results": results,
            "completed_experiments": completed}


def _worker_loop():
    """宿主兜底 worker：持久等待队列，事件到来后按批次推进，不轮询正式参数。"""
    import time

    bind_config_owner()
    register()
    while True:
        try:
            with AQ.connect() as conn:
                report = run_once_pass(conn, max_tasks=8)
            time.sleep(1.0 if report["results"] else 5.0)
        except Exception:
            time.sleep(15.0)


def start_research_worker_fallback():
    """仅在现有 fallback threads 开关下启动；daemon 线程，不新增第二套调度。"""
    import threading

    thread = threading.Thread(target=_worker_loop,
                              name="research-persistent-worker", daemon=True)
    thread.start()
    return thread


def snapshot(conn) -> dict:
    """只读快照：队列现状 + 每个实验的进度（含半侧残留与失败任务）+ 耗时拆分。

    ``capability`` 永远是"当前生效的授权"。这里不自动加载任何凭据：没有凭据就是
    ADVISORY，只出建议、不自动采用。
    """
    experiments = []
    for experiment_id in known_experiments(conn):
        try:
            experiments.append(EI.experiment_progress(conn, experiment_id))
        except EI.IncrementError:
            experiments.append({"experiment_id": experiment_id, "error": "run_missing"})
    latency = RM.latency_report(conn)
    return {"worker_version": WORKER_VERSION, "queue": AQ.queue_report(conn),
            "stages": AQ.stage_report(conn), "experiments": experiments,
            # 队列连接只用于任务/实验；模型配置由 adaptive_engine owner 打开。
            "provider": AGENT.provider_readiness(),
            "latency": {"samples": latency["samples"],
                        "stages": latency["stages"],
                        "bottleneck": latency["bottleneck"],
                        "hint": RM.speed_hint(latency)},
            "capability": CAP.describe_capability(None)}


def _ledger_connection(path=None):
    """打开正式账本库读取 AI 槽位配置。

    ``get_slot_config`` 内部会做一次 ``ensure_schema``（表已存在时是空操作），所以这里
    不能只给只读连接；除此之外本函数不写任何业务数据。
    """
    import sqlite3

    import data_paths

    target = str(path or data_paths.data_path("paper_trading.sqlite3"))
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def provider_check(config_conn=None, *, slot: str = "ai1", max_tokens: int = 32) -> dict:
    """对设置页槽位发一次最小真实调用；禁用/无效地址不会发请求。

    配置只能来自 owner（``adaptive_engine``）的连接工厂。入口（本模块 CLI、
    宿主兜底 worker、main.py）都会先 ``bind_config_owner()``；绕开入口直接调用的
    调用方拿到明确的未绑定原因，而不是换一个库读配置。
    """
    import time

    import ai_review_service as AIR
    import ai_provider_transport as transport
    import research_runtime_config as RUNTIME_CONFIG

    try:
        with RUNTIME_CONFIG.configuration_connection(config_conn) as conn:
            config = AIR.get_slot_config(conn, slot)
        readiness = AIR.slot_readiness(config)
    except RUNTIME_CONFIG.ConfigurationConnectionUnbound as exc:
        return {"slot": slot, "ok": False, "stage": "config",
                "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                "hint": "先 bind_config_owner()（或经 research_worker.main / "
                        "start_research_worker_fallback 启动）再读配置"}
    except Exception as exc:  # noqa: BLE001
        return {"slot": slot, "ok": False, "stage": "config",
                "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    key_set = bool(str(config.get("api_key") or "").strip())
    model = str(config.get("model") or "").strip()
    host = str(config.get("base_url") or "").split("//")[-1].split("/")[0]
    if not readiness["ready"]:
        return {"slot": slot, "ok": False, "stage": "config", "key_set": key_set,
                "model": model or None, "base_host": host or None,
                "enabled": bool(config.get("enabled")),
                "error": f"provider_{readiness['reason']}"}
    started = time.perf_counter()
    try:
        _payload, in_tokens, out_tokens, latency = transport.call_json(
            config, "Reply JSON only.", 'Return exactly {"ok": true}',
            max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        return {"slot": slot, "ok": False, "stage": "call", "model": model,
                "base_host": host or None,
                "wall_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": f"{type(exc).__name__}: {str(exc)[:140]}"}
    return {"slot": slot, "ok": True, "stage": "called", "model": model,
            "base_host": host or None, "latency_ms": round(latency, 1),
            "input_tokens": in_tokens, "output_tokens": out_tokens}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="推进已登记实验的下一批会话任务")
    run.add_argument("--max-tasks", type=int, default=5)
    run.add_argument("--path", default=None, help="研究库路径；默认取运行时数据目录")
    status = sub.add_parser("status", help="只读查看队列与实验进度")
    status.add_argument("--path", default=None)
    check = sub.add_parser("provider-check", help="对某个 AI 槽位发一次最小真实调用")
    check.add_argument("--slot", default="ai1")
    # 模型配置固定复用 adaptive_engine._connect；不开放指向其他库的误配入口。
    attribution = sub.add_parser("attribution", help="复盘归因：AI 意见改变了哪些决策")
    attribution.add_argument("--strategy-id", default=None)
    attribution.add_argument("--ledger", default=None)
    bench = sub.add_parser("bench", help="实测审核给的三个耗时目标")
    bench.add_argument("--runs", type=int, default=3, help="研究问题次数")
    bench.add_argument("--sessions", type=int, default=6, help="小实验的会话数")
    bench.add_argument("--slot", default="ai1")
    bench.add_argument("--path", default=None)
    args = parser.parse_args(argv)

    register()
    # CLI 也是组合根：provider / 队列里的研究任务都要读设置页权威配置库。
    # 这一步只注册 owner 工厂（幂等），不读配置、不发请求。
    if args.command != "attribution":
        bind_config_owner()
    if args.command == "bench":
        import research_bench as RB

        with AQ.connect(args.path) as conn:
            report = RB.run_bench(conn, runs=args.runs, sessions=args.sessions,
                                  slot=args.slot)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        measured = [row for row in report["targets"].values() if row.get("met") is not None]
        return 0 if all(row["met"] for row in measured) else 1
    if args.command == "attribution":
        import ai_advice_service as ADV

        conn = _ledger_connection(args.ledger)
        try:
            summary = ADV.attribution_summary(conn, strategy_id=args.strategy_id)
        finally:
            conn.close()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "provider-check":
        result = provider_check(slot=args.slot)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "status":
        try:
            with AQ.connect_readonly(args.path) as conn:
                payload = snapshot(conn)
        except AQ.QueueNotInitialized:
            payload = {"worker_version": WORKER_VERSION, "initialized": False}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    with AQ.connect(args.path) as conn:
        report = run_once_pass(conn, max_tasks=args.max_tasks)
        state = snapshot(conn)
    print(json.dumps({"ran": len(report["results"]),
                      "healed_experiments": report["healed_experiments"],
                      "results": report["results"],
                      "experiments": state["experiments"]},
                     ensure_ascii=False, indent=2))
    failed = [row for row in report["results"]
              if row.get("status") not in (AQ.STATUS_SUCCEEDED, AQ.STATUS_WAITING_DATA)]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
