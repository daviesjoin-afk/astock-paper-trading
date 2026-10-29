# -*- coding: utf-8 -*-
"""审核三个耗时目标的实测入口（方案 S2 的验收工具）。

审核文档给的三条目标：

1. 已提交业务事件 → 持久化任务：p95 ≤ 2 秒；
2. 已有快照 → 首份结构化 AI 意见：p95 ≤ 30 秒（单次硬超时 60 秒）；
3. 已有合格数据 → 一组小实验结果：p95 ≤ 15 分钟。

这个模块**只测时间，不产出收益结论**。它把三条目标拆成可以分别测的两段：

- **实验段**（不需要模型）：登记一个成对回放实验，跑完并发布全部回执，量到端到端墙钟。
  这一段现在就能测，因为它只用回放引擎与本地计算。
- **Agent 段**（需要模型）：投递研究问题 → 模型判断 → 意见落库，量"入队到任务持久化"
  与"领取到意见落库"两段。没有配置 provider 时这一段会明确报 ``provider_not_configured``，
  而不是给一个空数字。

任何一段没测到，``met`` 就是 ``None``（未判定），不会因为"没报错"就写成达标。
"""
from __future__ import annotations

import datetime as dt
import time

import agent_task_queue as AQ
import ai_recommendation_store as STORE
import ai_research_agent as AGENT
import experiment_increment as EI
import native_paired_replay as NPR

BENCH_VERSION = "research-bench-v1"

TARGET_EVENT_TO_TASK = "event_to_durable_task"
TARGET_SNAPSHOT_TO_OPINION = "snapshot_to_first_opinion"
TARGET_SMALL_EXPERIMENT = "small_experiment_batch"

#: 审核文档给的初始验收目标（秒）。
TARGETS = {
    TARGET_EVENT_TO_TASK: 2.0,
    TARGET_SNAPSHOT_TO_OPINION: 30.0,
    TARGET_SMALL_EXPERIMENT: 15 * 60.0,
}

R_PROVIDER_NOT_CONFIGURED = "provider_not_configured"


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(samples, target):
    values = [float(item) for item in samples or ()]
    if not values:
        return {"samples": 0, "p50_s": None, "p95_s": None, "target_s": target,
                "met": None}
    p95 = round(_percentile(values, 0.95), 3)
    return {"samples": len(values),
            "p50_s": round(_percentile(values, 0.5), 3),
            "p95_s": p95,
            "max_s": round(max(values), 3),
            "target_s": target,
            "met": bool(p95 <= target)}


# --------------------------------------------------------------------------- #
# 实验段（不需要模型）
# --------------------------------------------------------------------------- #

def _trading_days(count: int, start: dt.date | None = None) -> list[str]:
    import universe as U

    day = start or dt.date(2026, 9, 28)
    days: list[str] = []
    while len(days) < count:
        if U.is_trade_day(day):
            days.append(day.isoformat())
        day += dt.timedelta(days=1)
    return days


def _factor_table(scale: float):
    import pandas as pd

    return pd.DataFrame([{
        "name": "甲", "industry": "A", "price": 10.0, "pct": 3.0, "amount": 5e7,
        "turnover": 3.0, "main_pct": 1.0, "super_net_raw": 1e6, "mom5_raw": 0.02,
        "mom20_raw": 0.03, "mom60_raw": 0.05, "mom_short": 0.02, "volsurge": 1.5,
        "vol_surge_raw": 2.0, "rsi14_raw": 55.0, "flow": 1.0, "sentiment": 0.0,
        "above_boll_mid": True, "boll_mid_breakout": True, "ma20": 9.5 * scale,
        "ma60": 9.0 * scale,
    }], index=["600001"])


def build_environment(sessions: int = 6) -> NPR.ReplayEnvironment:
    """一个**只用于计时**的合成环境：真实交易日 + 单标的，不构成任何收益证据。"""
    days = _trading_days(max(1, int(sessions)))
    built = []
    for index, day in enumerate(days):
        scale = 1.0 + 0.01 * index
        built.append(NPR.ReplaySession(
            session_date=day, decision_at=f"{day}T14:30:00+08:00",
            quotes=(NPR.ReplayQuote("600001", round(10.0 * scale, 2), prev_close=10.0),),
            factor_table=_factor_table(scale), first_board_codes=("600001",)))
    return NPR.ReplayEnvironment(sessions=tuple(built))


def bench_experiment(conn, *, sessions: int = 6, handlers=None,
                     worker_id: str = "bench") -> dict:
    """跑一组小实验并计时：登记 → 逐个会话推进 → 全部回执发布。"""
    import agent_task_runner as AR

    environment = build_environment(sessions)
    experiment_id = f"bench-{int(time.time() * 1000)}"
    champion, challenger = NPR.sides_from_manifest(
        "tq_breakout", target_kind="entry_policy", model_family="first_board_relay",
        variant_fields={"first_board_bonus": 0.0})
    started = time.perf_counter()
    EI.enqueue_experiment(
        conn, experiment_id=experiment_id, target_identity="bench",
        environment=environment, champion=champion, challenger=challenger,
        rules=NPR.ReplayRules(reference_capital=100_000.0, max_positions=1,
                              hold_sessions=1, topn=1))
    AQ.commit(conn)
    enqueued = time.perf_counter()
    # 基准也走生产资源准入，不能为了计时绕过重型任务租约。
    table = handlers or {
        EI.TASK_TYPE_SESSION: AR.HandlerSpec(
            handler=EI.handle_session_task, heavy=True)}
    AR.run_batch(conn, max_tasks=len(environment.sessions) + 2, handlers=table,
                 worker_id=worker_id)
    finished = time.perf_counter()
    progress = EI.experiment_progress(conn, experiment_id)
    return {
        "experiment_id": experiment_id,
        "sessions": len(environment.sessions),
        "published": progress["sessions_published"],
        "complete": progress["complete"],
        "enqueue_s": round(enqueued - started, 4),
        "run_s": round(finished - enqueued, 4),
        "total_s": round(finished - started, 4),
    }


# --------------------------------------------------------------------------- #
# Agent 段（需要模型）
# --------------------------------------------------------------------------- #

def bench_agent(conn, *, runs: int = 3, slot: str = "ai1", config_conn=None,
                provider=None, identity=None) -> dict:
    """投递研究问题并跑完，量"入队→持久化"与"领取→意见落库"两段。"""
    import agent_task_runner as AR


    readiness = AGENT.provider_readiness(config_conn, (slot,))[slot]
    if provider is None and not readiness.get("ready"):
        return {"configured": False, "reason": R_PROVIDER_NOT_CONFIGURED,
                "slots": readiness, "event_to_task_s": [], "claim_to_opinion_s": []}

    import functools

    handlers = {AGENT.TASK_TYPE_RESEARCH: AR.HandlerSpec(
        handler=functools.partial(
            AGENT.run_research_task, provider=provider, config_conn=config_conn))}
    exact_identity = identity or {"strategy_id": "tq_breakout",
                                  "strategy_version": 1,
                                  "strategy_checksum": "a" * 64}
    event_to_task: list[float] = []
    claim_to_opinion: list[float] = []
    without_opinion: list[float] = []
    opinions = 0
    for index in range(max(1, int(runs))):
        goal = f"耗时基准研究问题 #{index}"
        started = time.perf_counter()
        task, _created = AGENT.enqueue_research_question(
            conn, goal=goal, hypothesis="基准假设：门槛合理性",
            exact_identity=exact_identity,
            input_fingerprint=f"bench-{index}-{int(started * 1e6)}", slot=slot)
        AQ.commit(conn)
        event_to_task.append(time.perf_counter() - started)
        claimed_at = time.perf_counter()
        AR.run_batch(conn, max_tasks=1, handlers=handlers, worker_id="bench")
        elapsed = time.perf_counter() - claimed_at
        # 意见与终态在同一个发布事务里提交，所以"领取→终态"就是"领取→意见落库"的上界。
        # 同时记下这次是否真的产出了意见：没产出就不是"很快"，而是"没测到"。
        if STORE.list_for_task(conn, task["task_id"]):
            opinions += 1
            claim_to_opinion.append(elapsed)
        else:
            without_opinion.append(elapsed)
    return {
        "configured": True,
        "slots": readiness,
        "opinions": opinions,
        "runs_without_opinion_s": [round(item, 4) for item in without_opinion],
        "event_to_task_s": [round(item, 4) for item in event_to_task],
        "claim_to_opinion_s": [round(item, 4) for item in claim_to_opinion],
    }


def run_bench(conn, *, runs: int = 3, sessions: int = 6, slot: str = "ai1",
              skip_experiment: bool = False, provider=None, config_conn=None) -> dict:
    """把三条目标跑一遍，返回可核对的数字与"是否达标"。"""


    agent = bench_agent(conn, runs=runs, slot=slot, provider=provider,
                        config_conn=config_conn)
    experiment = None if skip_experiment else bench_experiment(conn, sessions=sessions)
    targets = {
        TARGET_EVENT_TO_TASK: _summary(agent.get("event_to_task_s"), 
                                       TARGETS[TARGET_EVENT_TO_TASK]),
        TARGET_SNAPSHOT_TO_OPINION: _summary(agent.get("claim_to_opinion_s"),
                                             TARGETS[TARGET_SNAPSHOT_TO_OPINION]),
        TARGET_SMALL_EXPERIMENT: _summary(
            [] if experiment is None else [experiment["total_s"]],
            TARGETS[TARGET_SMALL_EXPERIMENT]),
    }
    if agent.get("reason") == R_PROVIDER_NOT_CONFIGURED:
        # 没配置 provider：前两条不是"没达标"，而是"未测"。
        for name in (TARGET_EVENT_TO_TASK, TARGET_SNAPSHOT_TO_OPINION):
            targets[name] = {"samples": 0, "p50_s": None, "p95_s": None,
                             "target_s": TARGETS[name], "met": None,
                             "reason": agent["reason"]}
    return {
        "bench_version": BENCH_VERSION,
        "agent": agent,
        "experiment": experiment,
        "targets": targets,
        "note": "这里只测耗时；收益结论不能由耗时推出",
    }
