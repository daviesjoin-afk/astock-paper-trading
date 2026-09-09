# -*- coding: utf-8 -*-
"""用户自建策略接入生产运行链路（PR-35）。

此前生产链路（``generate_signals`` / ``run_slot``）通过
``ACTIVE_ACCOUNT_IDS = SR.active_ids() ∩ ACCOUNT_SPECS`` 参与选股，任何
用户自建（origin=user）策略即使激活后也不会产生信号——
``_candidate_rows`` 还会直接 ``ACCOUNT_SPECS[account_id]`` KeyError。
本模块补上缺失的声明式通道：

1. ``user_participant_ids``：注册表中 active 且 supports_new_cycle=1 的
   用户策略 id 集合（动态参与资格的唯一事实来源）；
2. ``user_spec_for``：从 StrategyRuntimeContext（Risk Fingerprint →
   Risk Profile → Execution Profile）派生纸盘账户 spec，替代固定五套
   ACCOUNT_SPECS 表——这正是 PR-37 声明化的方向；
3. ``dsl_strategy_raw``：在共享因子表上用编译后的 DSL（纯离线、
   fail-closed 求值器）产出与内置模型同构的候选 picks。

约束：本模块无网络 I/O、无订单写入；行情/K线通过调用方注入
（``kline_loader``），断网可执行。不引入任何 ``if account_id == XXX``
分支——用户策略与内置策略的区别只有 ``selection_mode`` 能力位。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping

import strategy_dsl_schema as DSL
from strategy_dsl_evaluator import (
    StrategyDslEvaluationError,
    evaluate as dsl_evaluate,
)
from strategy_dsl_schema import StrategyDslValidationError

# 用户策略默认声明式参数（保守值；可被编译画像覆盖的键以编译画像为准）。
USER_MAX_FACTOR_LAG = 1
USER_DEFAULT_ENTRY_PCT_HIGH = 6.5
USER_DEFAULT_GAP_Q2 = (-0.025, 0.07)
USER_DEFAULT_HOLD_MAX = 8
USER_DEFAULT_CANDIDATE_TOPN = 10
USER_SOURCE_STRATEGY = "strategy_dsl"

_SNAPSHOT_PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_FINANCIAL_FIELDS = ("pe", "pb", "roe", "profit_yoy")
_FLOW_FIELDS = ("main_pct", "super_net")


def user_participant_ids(conn: sqlite3.Connection) -> tuple[str, ...]:
    """返回允许参与当前运行的用户策略 id（active ∧ supports_new_cycle=1）。

    注册表尚未建表（极早期数据库）时返回空集，绝不抛异常阻断主流程。
    """
    try:
        rows = conn.execute(
            "SELECT id FROM strategy_definitions "
            "WHERE origin='user' AND lifecycle_status='active' AND supports_new_cycle=1 "
            "ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(str(row[0]) for row in rows)


def user_spec_for(context, *, risk_profiles: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """从 StrategyRuntimeContext 派生纸盘账户 spec（声明式，纯函数）。

    风险帽（max_positions/max_weight/max_exposure）与纪律参数
    （hard_stop/trail/hold_max）一律取编译 Risk Profile 的 soft/evolvable
    值，让 ``effective_spec`` 的"编译画像收紧"语义对用户策略同样成立。
    """
    definition = dict(context.definition or {})
    metadata = definition.get("metadata") if isinstance(definition.get("metadata"), dict) else {}
    soft = dict(getattr(context.risk_profile, "soft_limits", None) or {})
    evo = dict(getattr(context.risk_profile, "evolvable_params", None) or {})
    risk_profile_key = str(metadata.get("paper_risk_profile") or "").strip()
    if risk_profile_key not in risk_profiles:
        risk_profile_key = "trend"
    hold_max = evo.get("holding_days") or USER_DEFAULT_HOLD_MAX
    try:
        hold_max = max(1, int(hold_max))
    except (TypeError, ValueError):
        hold_max = USER_DEFAULT_HOLD_MAX
    name = str(definition.get("name") or context.strategy_id)
    return {
        "name": name,
        "source_strategy": USER_SOURCE_STRATEGY,
        "selection_mode": "dsl",
        "mode": "swing",
        "cycle_days": max(1, int(metadata.get("cycle_days") or 8)),
        "max_positions": max(1, int(soft.get("max_positions") or 3)),
        "max_weight": float(soft.get("max_weight") or 0.30),
        "max_exposure": float(soft.get("max_exposure") or 0.65),
        "risk_profile": risk_profile_key,
        "strategy_version": f"v{context.version}",
        "default_style": "pullback",
        "entry_model_name": name,
        "max_factor_lag": USER_MAX_FACTOR_LAG,
        "entry_pct_high": float(metadata.get("entry_pct_high") or USER_DEFAULT_ENTRY_PCT_HIGH),
        "gap_q2": tuple(metadata.get("gap_q2") or USER_DEFAULT_GAP_Q2),
        "allowed_q": ("Q1", "Q2"),
        "hold_min": 1,
        "hold_max": hold_max,
        "hard_stop": float(evo.get("hard_stop") or -0.05),
        "trail_after": float(evo.get("trail_after") or 0.05),
        "trail_stop": float(evo.get("trail_stop") or 0.06),
        "take_profit": [(0.10, 1 / 3), (0.16, 1 / 3)],
        "candidate_topn": max(1, int(metadata.get("candidate_topn") or USER_DEFAULT_CANDIDATE_TOPN)),
        "lifecycle_stage": context.lifecycle_stage,
        "dsl_version": context.version,
        "dsl_checksum": context.checksum,
    }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _snapshot_for_code(row: Mapping[str, Any], kline) -> dict[str, Any] | None:
    """把因子表行 + 注入的日线组装成 DSL 求值快照（缺失即 fail-closed）。"""
    if kline is None or len(kline) < 2:
        return None
    snapshot: dict[str, Any] = {}
    for field in _SNAPSHOT_PRICE_FIELDS:
        if field not in kline.columns:
            continue
        series = [value for value in (_finite(item) for item in kline[field]) if value is not None]
        if series:
            snapshot[field] = series
    financials = {}
    for field in _FINANCIAL_FIELDS:
        value = _finite(row.get(field)) if hasattr(row, "get") else None
        if value is not None:
            financials[field] = [value]
    if financials:
        snapshot["financials"] = financials
    fund_flow = {}
    for field in _FLOW_FIELDS:
        value = _finite(row.get(field)) if hasattr(row, "get") else None
        if value is not None:
            fund_flow[field] = [value]
    if fund_flow:
        snapshot["fund_flow"] = fund_flow
    if "close" not in snapshot:
        return None
    return snapshot


def dsl_strategy_raw(
    context,
    spec: Mapping[str, Any],
    table,
    *,
    asof_date,
    kline_loader: Callable[[str, Any], Any],
) -> dict[str, Any]:
    """在共享因子表上执行编译后的 DSL，返回与内置模型同构的 raw 负载。

    - 只做过滤与确定性排序：DSL 是布尔门，排序用当日涨幅降序、代码升序
      打破平局（后续 PR 可在声明式 schema 中扩展排序表达）。
    - 任何一行数据不足 / DSL 求值异常都按"不通过"处理（fail-closed），
      计数进入 metadata 供审计，绝不让坏行中断整轮扫描。
    """
    compiled = context.compiled_dsl
    if compiled is None and (context.definition or {}).get("dsl_ast") is not None:
        compiled = DSL.normalize(context.definition["dsl_ast"])
    topn = int(spec.get("candidate_topn") or USER_DEFAULT_CANDIDATE_TOPN)
    metadata = {
        "selection_mode": "dsl",
        "model_family": USER_SOURCE_STRATEGY,
        "strategy_id": context.strategy_id,
        "dsl_version": context.version,
        "dsl_checksum": context.checksum,
        "factor_asof": str(asof_date),
        "topn": topn,
        "evaluated": 0, "passed": 0, "kline_missing": 0, "eval_errors": 0,
    }
    if compiled is None:
        return {"picks": [], "metadata": {**metadata, "blocked": True,
                                          "reason": "策略定义缺少 DSL 规则，无法产生候选"}}
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for code in table.index:
        code = str(code)
        try:
            row = table.loc[code]
        except KeyError:
            continue
        kline = kline_loader(code, asof_date)
        snapshot = _snapshot_for_code(row, kline)
        if snapshot is None:
            metadata["kline_missing"] += 1
            continue
        metadata["evaluated"] += 1
        try:
            passed = dsl_evaluate(compiled, snapshot)
        except (StrategyDslValidationError, StrategyDslEvaluationError):
            metadata["eval_errors"] += 1
            continue
        if not passed:
            continue
        metadata["passed"] += 1
        pct = _finite(row.get("pct")) if hasattr(row, "get") else None
        price = _finite(row.get("price")) if hasattr(row, "get") else None
        if price is None and snapshot.get("close"):
            price = snapshot["close"][-1]
        name = None
        if hasattr(row, "get"):
            raw_name = row.get("name")
            if raw_name is not None and str(raw_name).strip() not in {"", "nan"}:
                name = str(raw_name)
        pick = {
            "code": code,
            "name": name or code,
            "price": price,
            "pct": pct,
            "score": 1.0,
            "entry_path": "dsl_rule",
            "selection_mode": "dsl",
            "strategy_id": context.strategy_id,
            "dsl_version": context.version,
            "dsl_checksum": context.checksum,
        }
        ranked.append((-(pct if pct is not None else 0.0), code, pick))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return {"picks": [item[2] for item in ranked[:topn]], "metadata": metadata}
