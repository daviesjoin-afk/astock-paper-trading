# -*- coding: utf-8 -*-
"""Cycle-owned risk application service.

The public :func:`paper_trading.monitor_risk` facade owns the durable scan
lifecycle.  This module owns one claimed risk run: snapshot, external
evidence, write-time cycle fence, review/exit application, projection sync and
pending-manual bookkeeping.  It never imports :mod:`paper_trading`.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, replace
from typing import Any, Callable, ContextManager

import execution_planner as EP
import execution_verification as EV
import paper_account_specs as ACS
import paper_decision_audit as PDA
from paper_kline_snapshot import RoundKlineCache
import paper_portfolio_read_model as PPort
import paper_numeric as PNum
import paper_position_review as PReview
import paper_position_risk_state as PPRS
import paper_quote_policy as PQP
import paper_repository as PRP
import paper_risk_decision as PRD
import paper_risk_evidence as PREv
import paper_risk_scan_state as PRSS
import paper_risk_writes as PRW
import paper_schema_migrations as PSM
import strategy_policies as SPOL
import strategy_registry as SR
import strategy_selection_resolver as SRES
import strategy_risk_enforcement as SRE
import strategy_runtime as SRT
import user_strategy_participation as USP
from paper_trading_rules import limit_pct as _limit_pct
from paper_common import now_text as _now, as_date as _date, dump_json as _json

__all__ = ["RiskRunContext", "RiskServicePorts", "run"]

ACCOUNT_SPECS = ACS.ACCOUNT_SPECS
NEW_STRATEGY_ID = SPOL.NEW_STRATEGY_ID


@dataclass(frozen=True)
class RiskRunContext:
    """Immutable identity for one already-claimed risk run."""

    cycle_id: int
    asof_day: dt.date

    def __post_init__(self):
        if self.cycle_id is None:
            raise ValueError("RiskRunContext requires explicit cycle_id")
        if self.asof_day is None:
            raise ValueError("RiskRunContext requires explicit asof_day")
        try:
            cycle_id = int(self.cycle_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"RiskRunContext cycle_id is invalid: {self.cycle_id!r}") from exc
        day = _date(self.asof_day)
        object.__setattr__(self, "cycle_id", cycle_id)
        object.__setattr__(self, "asof_day", day)


@dataclass(frozen=True)
class RiskServicePorts:
    """Shared infrastructure ports owned by ``paper_trading``.

    Risk-only evidence and decisions live in :mod:`paper_risk_evidence`; only
    infrastructure/adapters remain injectable here.
    """

    evidence: PREv.RiskEvidenceDeps
    open_db: Callable[..., ContextManager[sqlite3.Connection]]
    load_market_inputs: Callable[..., dict[str, Any]]
    shared_exposure: Callable[..., tuple[float, float]]
    dynamic_position_limits: Callable[..., dict[str, Any]]
    risk_profile: Callable[..., dict[str, Any]]
    risk_exit_account_ids: Callable[..., set]
    rotation_buy: Callable[..., dict[str, Any]]
    projection: Any
    process_pending_manual_orders: Callable[..., list]
    assert_active_lease: Callable[..., Any]


#: 与 `paper_numeric.num` 同一实现（严格契约），不再保留本地副本。
_num = PNum.num


def _lease_lost(exc):
    return isinstance(exc, RuntimeError) and str(exc).startswith("paper lease lost:")


def _quote_is_fresh(quote, asof_date):
    return PQP.quote_is_fresh(quote, asof_date, date_fn=_date)


def _execution_quote_status(quote, asof_date, purpose="entry"):
    return PQP.execution_quote_status(
        quote, asof_date, purpose=purpose, quote_fresh=_quote_is_fresh,
    )


def _accounts_by_id(conn, account_ids):
    ids = sorted({str(item) for item in account_ids or () if item})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM paper_accounts WHERE id IN ({placeholders}) AND status=?",
        (*ids, "running"),
    ).fetchall()
    return [dict(row) for row in rows]


def _spec_for(account_id, conn, *, cycle_id):
    """Resolve a SELL base spec with strict cycle provenance for user strategies."""
    spec = ACS.builtin_spec(account_id)
    if spec is not None:
        return spec
    try:
        context = SRT.get_context_for_cycle(conn, account_id, cycle_id=cycle_id)
    except (ValueError, sqlite3.Error):
        return ACS.fallback_spec()
    return USP.user_spec_for(context, risk_profiles=ACS.RISK_PROFILES)


def _strategy_stamp(conn, account_id, *, cycle_id, signal_id=None):
    """Exact cycle-pinned stamp, or an explicit unknown. A ``signal_id`` must
    prove its own lineage, so the resolver raises instead of current-filling."""
    if signal_id is not None:
        order = SRES.signal_order_provenance(
            conn, signal_id=signal_id, account_id=account_id,
            expected_cycle_id=cycle_id)
        return order.stamp
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=cycle_id)
    return tuple(stamp) if stamp is not None else (None, None, None)


def _audit(conn, account_id, event, detail, *, strategy_stamp=None):
    return PRP.audit(
        conn, account_id, event, detail, _now(),
        strategy_stamp=strategy_stamp,
    )


def _rotation_buy_candidate(conn, account, replacement, quote, market, news, asof_day,
                            *, all_quotes=None, ports: RiskServicePorts,
                            deps: PREv.RiskEvidenceDeps):
    signal_id = replacement.get("signal_id") or replacement.get("id")
    if not signal_id:
        return {"status": "no_candidate", "reason": "没有可复用的候选信号"}
    row = conn.execute(
        "SELECT * FROM paper_signals WHERE id=? AND status IN ('pending','deferred_capacity',?)",
        (signal_id, deps.entry_frozen_waitlist_status),
    ).fetchone()
    if not row:
        return {"status": "candidate_expired", "reason": "候选已被其他流程处理"}
    result = ports.rotation_buy(
        conn,
        account,
        dict(row),
        quote or {},
        market or {},
        news or [],
        _date(asof_day),
        all_quotes=dict(all_quotes or {}),
    )
    result = dict(result or {})
    result.update({
        "signal_id": int(signal_id),
        "rotation": True,
        "replacement_code": replacement.get("code"),
        "replacement_score": replacement.get("score"),
    })
    return result


def _bounded_risk_scope(conn, context: PPort.PortfolioReadContext, *,
                        ports: RiskServicePorts):
    """Return bounded positions plus current-or-historical risk account IDs.

    快照读取须在事务内跑（``_snapshot_evidence`` 只在 ``conn.in_transaction``
    为真时缓存）。
    """
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        positions = PPort.risk_positions_for_context(conn, context)
        risk_ids = set(ports.risk_exit_account_ids(conn))
        risk_ids.update(str(r["account_id"]) for r in positions if r.get("account_id"))
        return positions, risk_ids
    finally:
        if owns:
            conn.rollback()


def run(context: RiskRunContext, *, ports: RiskServicePorts):
    """Run one risk workflow for the exact context already claimed by the facade."""
    day = context.asof_day
    cycle_id = context.cycle_id
    kline_cache = RoundKlineCache(ports.evidence.load_kline)

    def round_kline(code, cutoff, *, inclusive=True):
        return kline_cache.get(str(code), _date(cutoff), inclusive=inclusive)

    # Only this claimed risk run shares facts. Each consumer receives a copy;
    # neither the caller's ports nor another run inherits this cache.
    ports = replace(ports, evidence=replace(ports.evidence, load_kline=round_kline))
    manual_orders = []
    # 读上下文不可变（cycle_id + asof_day）；两处读取共用同一份身份事实。
    read_context = PPort.PortfolioReadContext(cycle_id, day)
    with ports.open_db() as snapshot_conn:
        # 快照阶段就固定到**已认领**的周期，而不是"此刻 active 的那个周期"。
        positions, risk_ids = _bounded_risk_scope(
            snapshot_conn, read_context, ports=ports,
        )
        held_by_account = {}
        for position in positions:
            held_by_account.setdefault(position["account_id"], set()).add(str(position["code"]))
        candidate_codes = {
            str(row["code"])
            for account_id, held_codes in held_by_account.items()
            for row in PREv.eligible_replacement_candidates(
                snapshot_conn, account_id, day, held_codes, deps=ports.evidence,
            )
        }
    market_inputs = ports.load_market_inputs(
        day=day,
        positions=positions,
        candidate_codes=candidate_codes,
    )
    market_context = market_inputs["market_context"]
    quote_map = market_inputs["quote_map"]
    news = market_inputs["news"]
    news_meta = market_inputs["news_meta"]
    flow_trajectory_map = market_inputs["flow_trajectory_map"]

    # Both boundaries are consumed: risk models exclude today, audit fallback
    # includes it. Do not substitute one for the other. Successful reads move
    # outside the writer; the owner hands empty/failed reads to their first
    # consumer unchanged, and later consumers can retry as before.
    for code in sorted({str(position["code"]) for position in positions}):
        for inclusive in (False, True):
            kline_cache.prefetch(code, day, inclusive=inclusive)

    def _with_decision_snapshot(payload=None, **kwargs):
        return PDA.with_decision_snapshot(
            payload,
            kline_loader=ports.evidence.load_kline,
            news_scan_meta=news_meta,
            risk_version=ports.evidence.risk_version,
            now_fn=_now,
            **kwargs,
        )

    def _risk_log(conn, account_id, code, side, decision, reason, payload, *,
                  authority=None, decision_kind=None, order_id=None):
        """Append one risk decision with its write-time provenance.

        Mirrors ``paper_trading._risk_log``: the Risk Authority states its own
        authority and kind here (never inferred by a reader), and an
        order-linked row must declare its authority because an id alone only
        proves which order the row belongs to.
        """
        if order_id is not None and authority is None:
            raise ValueError(
                "order-linked risk decision requires an explicit authority")
        payload = _with_decision_snapshot(
            payload or {}, account_id=account_id, code=code, side=side,
            decision=decision, reason=reason,
        )
        if authority is not None:
            payload["decision_provenance"] = PSM.risk_decision_provenance(
                authority, decision_kind or decision)
        strategy_stamp = _strategy_stamp(conn, account_id, cycle_id=cycle_id)
        PRW.insert_risk_decision(
            conn, account_id, code, side, decision, reason, payload,
            strategy_stamp=strategy_stamp, cycle_id=cycle_id, now=_now(),
            order_id=order_id,
        )
        # F5：真实退出/入场决定的消费点。ADVISORY 只追加解释，不改写上面的规则行。
        try:
            import ai_advice_service as ADV

            ADV.consume_risk_decision_safe(
                conn, account_id=account_id, code=code, side=side, decision=decision,
                strategy_stamp=strategy_stamp)
        except Exception:  # noqa: BLE001 - 意见链绝不改变规则风险决策
            pass

    if not positions:
        with ports.open_db(immediate=True, hot_path=True) as conn:
            PRSS.assert_cycle_active(conn, cycle_id=cycle_id)  # 空仓分支同样要 fence
            ports.projection.sync_positions(conn, day)
            ports.projection.record_nav(conn, day, quote_map)
        try:
            manual_orders = ports.process_pending_manual_orders(day)
        except Exception as exc:
            manual_orders = [{"status": "pending_batch_retry", "reason": str(exc)}]
        return {"slot": "risk", "date": day.isoformat(), "orders": [], "manual_orders": manual_orders}
    with ports.open_db(immediate=True, hot_path=True) as conn:
        # R16 cycle fence：外部 I/O 之后、正式写 transaction 打开的第一件事就是
        # 证明"已认领的周期仍是当前 active cycle"。周期变了 ⇒ fail closed。
        PRSS.assert_cycle_active(conn, cycle_id=cycle_id)
        positions, risk_ids = _bounded_risk_scope(
            conn, read_context, ports=ports,
        )
        account_map = {
            row["id"]: row for row in _accounts_by_id(conn, risk_ids)
        }
        pool_market_value, pool_nav = ports.shared_exposure(
            conn, day, quote_map, cycle_id=cycle_id,
        )
        held_by_account = {}
        for item in positions:
            held_by_account.setdefault(item["account_id"], set()).add(item["code"])
        count_budget = ports.dynamic_position_limits(
            conn, cycle_id=cycle_id, asof_day=day,
        )
        rotations_today = {
            account_id: conn.execute(
                """SELECT COUNT(*) FROM paper_audit
                   WHERE account_id=? AND event='quality_rotation'
                     AND substr(created_at,1,10)=?""",
                (account_id, day.isoformat()),
            ).fetchone()[0]
            for account_id in account_map
        }
        quality_reviews = {}
        for position in positions:
            replacement = PREv.best_replacement_candidate(
                conn, position["account_id"], day,
                held_by_account.get(position["account_id"], set()),
                deps=ports.evidence,
            )
            review = PREv.position_quality_score(
                conn, position, quote_map.get(position["code"], {}), day,
                cycle_id=cycle_id,
                news=news, replacement=replacement, nav=pool_nav,
                market=market_context,
                flow_trajectory=flow_trajectory_map.get(position["code"]),
                deps=ports.evidence, alt_evidence=market_inputs.get("position_alt_data") or {},
            )
            review["review_date"] = day
            review["dynamic_position_limit"] = count_budget["limits"].get(position["account_id"], 5)
            review["strategy_position_count"] = len(held_by_account.get(position["account_id"], set()))
            review["at_dynamic_limit"] = (
                review["strategy_position_count"] >= review["dynamic_position_limit"]
            )
            review["pool_position_limit"] = count_budget["pool_limit"]
            review["rotations_today"] = int(rotations_today.get(position["account_id"], 0))
            quality_reviews[(position["account_id"], position["code"])] = review
        capacity_exit_reasons = PREv.over_capacity_exit_candidates(
            conn, positions, quality_reviews, account_map, day,
            count_budget=count_budget, account_specs=ACCOUNT_SPECS,
            deps=ports.evidence,
        )
        permission_exit_reasons = PREv.permission_scope_exit_candidates(
            conn, positions, quality_reviews, quote_map, day,
            deps=ports.evidence,
        )
        # Capacity compression and full-slot rotation must evaluate the weakest
        # holdings first; database/lot insertion order must never decide which
        # stock is sacrificed for a stronger candidate.
        positions.sort(key=lambda item: (
            0 if (item["account_id"], item["code"]) in permission_exit_reasons else 1,
            0 if (item["account_id"], item["code"]) in capacity_exit_reasons else 1,
            _num((quality_reviews.get((item["account_id"], item["code"])) or {}).get("score"), 100.0),
        ))
        concentration_sells_used = 0
        permission_sells_used = 0
        rotation_swaps_used_by_account = dict(rotations_today)
        rotation_bought_codes = set()
        rotation_results = []
        orders = []
        for position in positions:
            ports.assert_active_lease(conn, "risk position")
            quote = quote_map.get(position["code"], {})
            quote_status = _execution_quote_status(quote, day, purpose="exit")
            quality_review = quality_reviews.get((position["account_id"], position["code"])) or {}
            if position["account_id"] == "trend_pullback":
                previous_review = conn.execute(
                    """SELECT action FROM paper_position_reviews
                       WHERE cycle_id=? AND account_id=? AND code=? AND review_date < ?
                       ORDER BY review_date DESC LIMIT 1""",
                    (cycle_id, position["account_id"], position["code"], day.isoformat()),
                ).fetchone()
                quality_review["quality_exit_confirmed"] = bool(
                    previous_review and previous_review["action"] in {
                        "watch", "consolidation_exit", "capacity_exit"
                    }
                )
            downside_guard = PREv.intraday_downside_guard(
                position, quote, market=market_context, news=news,
                policy_override=ports.risk_profile(
                    account_map.get(position["account_id"]) or {"id": position["account_id"]},
                    asof_day=day, conn=conn, cycle_id=cycle_id,
                ),
                flow_trajectory=flow_trajectory_map.get(position["code"]),
                asof_day=day, deps=ports.evidence,
            )
            permission_reason = permission_exit_reasons.get((position["account_id"], position["code"]))
            capacity_reason = capacity_exit_reasons.get((position["account_id"], position["code"]))
            quality_review["rotations_today"] = int(
                rotation_swaps_used_by_account.get(position["account_id"], 0)
            )
            if not quote_status["fresh"]:
                pending_ratio, pending_reason, _, pending_detail = PREv.sell_plan(
                    position, quote, day, news,
                    base_spec=_spec_for(position["account_id"], conn, cycle_id=cycle_id),
                    deps=ports.evidence,
                )
                quality_action, quality_reason = PReview.decide_action(
                    quality_review, position, quote_status, concentration_sells_used,
                    policy=ports.evidence.review_policy,
                )
                if permission_reason:
                    quality_action = "permission_scope_exit_pending_quote"
                    quality_reason = f"{permission_reason}；{quote_status['reason']}，等待可执行行情"
                elif capacity_reason:
                    quality_action = "capacity_exit_pending_quote"
                    quality_reason = f"{capacity_reason}；{quote_status['reason']}，等待可执行行情"
                if downside_guard.get("level") != "none" and not permission_reason and not capacity_reason:
                    quality_action = "downside_pending_quote"
                    quality_reason = f"下跌{downside_guard['level']}：{downside_guard['reason']}；{quote_status['reason']}"
                PREv.save_position_review(conn, cycle_id, quality_review, quality_action, quality_reason)
                if pending_ratio > 0 or capacity_reason or permission_reason or downside_guard.get("level") != "none":
                    detail = {
                        **pending_detail,
                        "quote_status": quote_status,
                        "position_quality": quality_review,
                        "downside_guard": downside_guard,
                        "intended_sell_ratio": pending_ratio,
                    }
                    reason = quality_reason if (capacity_reason or permission_reason or downside_guard.get("level") != "none") else (
                        f"{pending_reason}；{quote_status['reason']}，风险未解除"
                    )
                    _risk_log(
                        conn,
                        position["account_id"],
                        position["code"],
                        "sell",
                        "exit_pending_data",
                        reason,
                        detail,
                    )
                    orders.append({
                        "code": position["code"],
                        "status": "exit_pending_data",
                        "reason": reason,
                    })
                continue
            price = _num(quote.get("price"), 0)
            # 峰值同时吸收当日 high：两轮 3 分钟扫描之间的冲高若不记入
            # peak，移动止损会系统性延迟触发（与日内下行守卫的口径一致）。
            # 同日新仓例外：只用买入后的采样价（2026-08-31 P1），
            # 避免买入前的高点立即制造虚假回撤预警。
            scan_peak = (
                price if PRD.bought_today(position, asof_day=day)
                else max(price, _num(quote.get("high"), 0.0))
            )
            if scan_peak > _num(position.get("peak_price"), 0):
                # R14：峰值写入 cycle-owned 风险状态，cycle 取与卖出路径同一个
                # write-time provenance；绝不按 (account_id, code) 裸写无身份的
                # paper_positions 投影。
                PPRS.update_peak(
                    conn, cycle_id=cycle_id,
                    account_id=position["account_id"], code=position["code"],
                    peak_price=scan_peak,
                )
                position["peak_price"] = scan_peak
            # 二轮确认：intent（两轮疑似出货/收益回吐）或 price_evidence
            # （资金字段缺失、两轮价格证据一致）。后者只解锁部分减仓。
            confirmation = PREv.downside_confirmation(
                conn, position["account_id"], position["code"], day, downside_guard,
                deps=ports.evidence,
            )
            # 一次性减仓动作的当日去重统一走 execution_verification 的**唯一**判据：
            # "是否存在经过验证的**正成交**"。此前这里用整单谓词
            # （status='filled' + VERIFIED_PREDICATE），部分成交会被读成"没发生过"，
            # 于是每轮扫描再发一张同样的减仓单 —— 300+300+300… 突破配置比例。
            # 中文 reason LIKE 仅作标记上线前旧订单的兜底。
            # 首段减仓执行证据（成交价 + 距今分钟），全清据此要求独立确认。
            hard_stop_trim = EV.first_verified_positive_execution(
                conn, marker="hard_stop_first_trim",
                account_id=position["account_id"], code=position["code"],
                asof_day=day, legacy_reason_like="%硬止损首段减仓%")
            base_spec = _spec_for(position["account_id"], conn, cycle_id=cycle_id)
            ratio, reason, next_stage, detail = PREv.sell_plan(
                position, quote, day, news,
                # PR-30：卖出状态机的止损/移动止损/时间止损用 cycle-pinned 编译画像。
                spec_override=SRE.effective_spec_for_cycle(
                    conn, position["account_id"], base_spec, cycle_id=cycle_id,
                ),
                base_spec=base_spec,
                deps=ports.evidence,
                hard_stop_trim=hard_stop_trim,
            )
            quality_action, quality_reason = PReview.decide_action(
                quality_review, position, quote_status, concentration_sells_used,
                policy=ports.evidence.review_policy,
            )
            if permission_reason:
                # Permissions exits have their own per-strategy daily quota.
                # Do not let three ordinary capacity/quality rotations defer a
                # position the account is no longer allowed to reinforce.
                quality_action, quality_reason = "permission_scope_exit", permission_reason
            elif capacity_reason and concentration_sells_used < ports.evidence.max_sells_per_run:
                quality_action, quality_reason = "capacity_exit", capacity_reason
            concentration_triggered = quality_action in {
                "consolidation_exit", "capacity_exit", "permission_scope_exit",
            }
            if concentration_triggered:
                # A quality rotation is a full exit, even when the ordinary
                # ladder would only take a partial profit.  Otherwise a
                # low-quality one-lot can remain indefinitely after the first
                # partial sell and defeat the purpose of concentration.
                if quality_action == "permission_scope_exit":
                    ratio = max(ratio, ports.evidence.permission_scope_exit_ratio)
                    detail["permission_scope_exit"] = {
                        "tranche_ratio": ports.evidence.permission_scope_exit_ratio,
                        "daily_limit": ports.evidence.permission_scope_exit_max_per_strategy_day,
                    }
                    reason = (
                        quality_reason if not reason else
                        f"{quality_reason}；与常规风险卖出取较高比例"
                    )
                else:
                    ratio = 1.0
                    reason = quality_reason if ratio <= 0 or not reason else f"{quality_reason}；覆盖常规卖出比例"
                    # 集中轮换是全退，覆盖 _sell_plan 可能带出的首段减仓标记，
                    # 避免"硬止损首段减仓已发生"的状态被全退订单误报。
                    detail["exit_marker"] = "concentration_exit"
                detail["position_quality"] = quality_review
                detail["concentration_review"] = True
            elif ratio > 0:
                quality_action = "risk_exit"
                detail["position_quality"] = quality_review
            detail["downside_guard"] = downside_guard
            # 下面三处"当日是否已减仓过"的门禁同样走正成交判据：只有被证据证明
            # （完整**或部分**）卖出的委托才算减过仓。没有验证列的旧行不得吃掉今天
            # 的第一次减仓，否则一个"没发生过的卖出"会把真实需要减仓的持仓永久挡住。
            # 结构化 marker 是主判据（见下方 guard_actionable 分支写入
            # detail["exit_marker"]），中文 LIKE 仅作标记上线前旧订单的同日兜底。
            warning_trimmed = EV.has_verified_positive_execution(
                conn, marker="downside_warning_trim",
                account_id=position["account_id"], code=position["code"],
                asof_day=day, legacy_reason_like="%下跌预警首段减仓%",
            )
            # P3 审计修复（P1）：partial/full 缺少一次性消费标记——确认后
            # 每个扫描周期都重复减仓，弱势日 ~10 分钟内复利式清仓。按级别
            # 去重：partial 已卖不重复 partial，但条件恶化仍可升级到 full。
            guard_level = downside_guard.get("level")
            guard_level_trimmed = EV.has_verified_positive_execution(
                conn, marker=f"downside_{guard_level}",
                account_id=position["account_id"], code=position["code"],
                asof_day=day,
                legacy_reason_like=f"%下跌{guard_level}已连续两次确认%",
            )
            # 2026-09-03 二次确认减仓：warning 级首段减仓（一天一次）用完
            # 之后，若连续两轮扫描（满足最小扫描间隔）均确认"疑似出货"，
            # 允许追加一次 partial 比例的减仓——兑现"连续两次扫描确认后
            # 才允许部分减仓"的审计口径；当日一次，条件恶化仍可升级 full。
            # 2026-10-30：确认依据还可能是 price_evidence（资金证据缺失），
            # 该通道只到 partial；判据全部下沉到 PREv.downside_action_plan。
            warning_confirmed_trimmed = EV.has_verified_positive_execution(
                conn, marker="downside_warning_confirmed",
                account_id=position["account_id"], code=position["code"],
                asof_day=day, legacy_reason_like="%下跌预警连续两次确认减仓%",
            )
            plan = PREv.downside_action_plan(
                position["account_id"], downside_guard,
                confirmation=confirmation,
                warning_account_ids=("tq_breakout", NEW_STRATEGY_ID),
                warning_trimmed=warning_trimmed,
                warning_confirmed_trimmed=warning_confirmed_trimmed,
                level_trimmed=guard_level_trimmed,
            )
            downside_guard["confirmed"] = plan["confirmed"]
            if plan["confirm_basis"]:
                downside_guard["confirm_basis"] = plan["confirm_basis"]
            downside_guard["confirmation"] = plan
            if plan["actionable"] and not concentration_triggered:
                ratio = max(ratio, plan["trim_ratio"])
                quality_action = plan["action"]
                # 当日去重的结构化消费标记（P1 审计修复 2026-09-02）：
                # 卖出订单 payload 携带 exit_marker，次日/下一级别仍可升级。
                detail["exit_marker"] = quality_action
                quality_reason = plan["reason"]
                reason = f"{quality_reason}；覆盖常规卖出比例" if reason else quality_reason
            elif (plan["pending"] or guard_level == "warning") and not concentration_triggered:
                quality_action = "downside_warning"
                quality_reason = plan["pending_reason"]
                _risk_log(
                    conn, position["account_id"], position["code"], "sell",
                    plan["pending_decision"], quality_reason,
                    {"downside_guard": downside_guard, "quote_status": quote_status},
                    authority="RISK", decision_kind=plan["pending_decision_kind"],
                )
            PREv.save_position_review(conn, cycle_id, quality_review, quality_action, quality_reason)
            if ratio <= 0:
                continue
            detail["quote_status"] = quote_status
            if quote_status.get("degraded"):
                reason += "；主行情新鲜有效，备用行情未核验，按风控退出降级执行"
            if int(position.get("available_qty") or 0) < ports.evidence.lot_size:
                pending_action = (
                    "permission_scope_exit_t1_locked"
                    if quality_action == "permission_scope_exit" else "held_t1"
                )
                _risk_log(conn, position["account_id"], position["code"], "sell", pending_action,
                          "A股 T+1，暂不可卖", detail, authority="RISK",
                          decision_kind=pending_action)
                continue
            sellable = int(position.get("available_qty") or 0)
            if ratio >= 0.999:
                planned_qty = sellable
            else:
                partial_qty = int(sellable * ratio / ports.evidence.lot_size) * ports.evidence.lot_size
                # P3 审计修复（P2）：一手仓的部分比例取整后为 0，旧逻辑
                # max(ports.evidence.lot_size,…) 会把"预警轻减 25%"放大成整仓清仓。部分
                # 退出一手仓时跳过本次分批（保留观察），不违背分级语义。
                if partial_qty < ports.evidence.lot_size:
                    _risk_log(
                        conn, position["account_id"], position["code"], "sell",
                        "partial_skipped_min_lot",
                        f"可卖 {sellable} 股不足按 {ratio*100:.0f}% 部分减仓的最低一手，"
                        "保留观察不做整仓清仓",
                        detail, authority="RISK", decision_kind="partial_skipped_min_lot",
                    )
                    continue
                planned_qty = partial_qty
            planned_qty = min(planned_qty, sellable)
            # Reserve approved unfilled exits; the execution runner owns their continuation.
            planned_qty, working_id = PREv.unreserved_risk_sell_quantity(
                conn, cycle_id=cycle_id, account_id=position["account_id"], code=position["code"], requested_qty=planned_qty)
            if working_id and planned_qty < ports.evidence.lot_size:
                orders.append({"code": position["code"], "status": "pending_execution_wait",
                               "order_id": working_id, "reason": "已有风险卖出委托等待执行，本轮不重复下单"})
                continue
            pct = _num(quote.get("pct"))
            if price <= 0 or pct <= -_limit_pct(
                position["code"], position.get("name"), position.get("risk_flag")
            ) + 0.05:
                # A quote that remains locked at the same limit price cannot
                # produce a new paper fill every five-minute pass.  Keep one
                # auditable attempt, then retry after a short cooldown or as
                # soon as the quoted price changes (the lock may have opened).
                retry_after = (dt.datetime.now() - dt.timedelta(
                    minutes=ports.evidence.blocked_retry_minutes
                )).strftime("%Y-%m-%d %H:%M:%S")
                recent_block = conn.execute(
                    """SELECT id,planned_price FROM paper_orders
                       WHERE account_id=? AND side='sell' AND code=?
                         AND status='unfilled_limit_down' AND created_at>=?
                       ORDER BY id DESC LIMIT 1""",
                    (position["account_id"], position["code"], retry_after),
                ).fetchone()
                if recent_block and abs(_num(recent_block["planned_price"]) - price) < 0.001:
                    orders.append({
                        "code": position["code"],
                        "status": "unfilled_limit_down_wait",
                        "reason": f"同价跌停委托 {ports.evidence.blocked_retry_minutes} 分钟冷却中，行情解锁或冷却结束后重试",
                    })
                    continue
                status, order_reason = "unfilled_limit_down", (reason + "；跌停/无报价，不能虚构成交")
                ports.assert_active_lease(conn, "risk unfilled-order write")
                detail = _with_decision_snapshot(
                    detail, account_id=position["account_id"], code=position["code"],
                    side="sell", decision="unfilled", reason=order_reason,
                    asof_date=day, quote=quote, news=news,
                    kline=ports.evidence.load_kline(position["code"], day, inclusive=False),
                )
                strategy_stamp = _strategy_stamp(conn, position["account_id"], cycle_id=cycle_id)
                cursor = PRW.insert_sell_order(
                    conn, position, qty=planned_qty, price=price, status=status,
                    reason=order_reason, detail=detail, strategy_stamp=strategy_stamp,
                    cycle_id=cycle_id, now=_now(),
                )
                # The Risk Authority's own decision about the sell order it just
                # created: the same decision result, bound to that exact order at
                # write time (never re-derived afterwards).
                _risk_log(conn, position["account_id"], position["code"], "sell", "unfilled",
                          order_reason, detail, authority="RISK", decision_kind="unfilled",
                          order_id=int(cursor.lastrowid))
                orders.append({"code": position["code"], "status": status, "reason": order_reason})
                continue
            qty = planned_qty
            detail["remaining_qty"] = max(0, int(_num(position.get("qty"))) - qty)
            detail["position_closed"] = detail["remaining_qty"] < ports.evidence.lot_size
            if concentration_triggered and not detail["position_closed"]:
                detail["capacity_state"] = "partial_due_t1"
                reason += f"；仅卖出可卖底仓，仍有 {detail['remaining_qty']} 股受 T+1 约束，后续继续处理"
            detail = _with_decision_snapshot(
                detail, account_id=position["account_id"], code=position["code"], side="sell",
                decision="filled", reason=reason, asof_date=day, quote=quote, news=news,
                kline=ports.evidence.load_kline(position["code"], day, inclusive=False),
                final_score=quality_review.get("score"),
            )
            savepoint = f"risk_pos_{position['account_id']}_{position['code']}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                ports.assert_active_lease(conn, "risk sell order")
                sell_cycle_id = cycle_id
                strategy_stamp = _strategy_stamp(conn, position["account_id"], cycle_id=cycle_id)
                cursor = PRW.insert_sell_order(
                    conn, position, qty=qty, price=price, status="pending_execution",
                    reason=reason, detail=detail, strategy_stamp=strategy_stamp,
                    cycle_id=sell_cycle_id, now=_now(),
                )
                order_id = int(cursor.lastrowid)
                # R32-E1：这条卖出委托由 Risk Authority 的退出决策产生，因此在写入
                # 当刻把**当时实际消费的那份 owner 结论**绑定到该订单（不是事后重跑
                # 风控，也不是由 execution 结果冒充）。
                _risk_log(conn, position["account_id"], position["code"], "sell",
                          quality_action or "risk_exit", quality_reason or reason, detail,
                          order_id=order_id, authority="RISK",
                          decision_kind=str(quality_action or "risk_exit"))
                EP.commit_fill(
                    conn,
                    account=account_map.get(position["account_id"], {"id": position["account_id"]}),
                    plan={
                        "side": "sell", "code": position["code"],
                        "name": position.get("name"), "qty": qty,
                        "quote_at": quote.get("quote_at"),
                        "execution_quote": quote,
                    },
                    order_id=order_id,
                    asof_day=day,
                    side="sell",
                    action="filled",
                    audit_action="sell_filled",
                    audit_message=f"{position['code']} {qty}股，参考价 {price:.2f}",
                    reason=reason,
                    detail=detail,
                    assumption="由模拟执行权威根据已核验行情计算价格和费用",
                    sell_next_take_stage=next_stage,
                )
                committed = conn.execute(
                    "SELECT status,filled_qty,filled_price,execution_reasons "
                    "FROM paper_orders WHERE id=?", (order_id,),
                ).fetchone()
                if not committed or str(committed[0]) not in {"filled", "partially_filled"}:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    blocked = str(committed[3] or "execution_not_filled") if committed else "execution_state_unavailable"
                    orders.append({
                        "code": position["code"],
                        "status": str(committed[0]) if committed else "pending_execution",
                        "qty": int(committed[1] or 0) if committed else 0,
                        "remaining_qty": int(qty),
                        "reason": blocked,
                    })
                    continue
                qty = int(committed[1] or 0)
                fill_price = float(committed[2] or price)
                detail["remaining_qty"] = max(0, int(_num(position.get("qty"))) - qty)
                detail["position_closed"] = detail["remaining_qty"] < ports.evidence.lot_size
                if not detail["position_closed"]:
                    detail["capacity_state"] = "partial_execution"
                if detail.get("protective_exit") and ratio >= 0.999:
                    policy = SPOL.recovery_policy(position["account_id"])
                    _audit(conn, position["account_id"], "protective_exit_recovery_watch", _json({
                        "status": "watching", "code": position["code"],
                        "account_id": position["account_id"], "exit_class": detail.get("exit_class"),
                        "exit_reason_code": detail.get("exit_reason_code"),
                        "exit_price": fill_price, "exit_at": _now(),
                        "min_reclaim_pct": policy["reclaim_pct"],
                        "required_scans": policy["min_scans"],
                        "cooldown_minutes": policy["cooldown_minutes"],
                        "expires_on": (_date(day) + dt.timedelta(days=policy["max_days"])).isoformat(),
                        "probe_ratio": 0.25,
                        "volatility_shadow": detail.get("volatility_shadow"),
                    }), strategy_stamp=strategy_stamp)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                # F5：真实卖出成交后投递成交事件；研究侧失败不影响这笔退出。
                import research_orchestration as RO

                RO.notify_fill(order_id=order_id, account_id=position["account_id"],
                               code=position["code"], side="sell",
                               status=str(committed[0]), strategy_stamp=strategy_stamp,
                               now=_now())
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if _lease_lost(exc):
                    raise
                retry_reason = f"持仓卖出执行失败，可重试：{type(exc).__name__}: {exc}"
                _risk_log(conn, position["account_id"], position["code"], "sell", "execution_retry", retry_reason, {"error": str(exc), "retryable": True})
                orders.append({"code": position["code"], "status": "execution_retry", "reason": retry_reason})
                continue
            if concentration_triggered:
                # Rebuild the position snapshot before sizing the replacement
                # so the released value is visible to the shared-pool budget
                # in this same risk pass.
                ports.projection.sync_positions(conn, day)
                if quality_action == "permission_scope_exit":
                    permission_sells_used += 1
                else:
                    concentration_sells_used += 1
                if quality_action == "consolidation_exit":
                    rotation_swaps_used_by_account[position["account_id"]] = (
                        int(rotation_swaps_used_by_account.get(position["account_id"], 0)) + 1
                    )
                # P3 审计修复（S2）：质量轮换独立事件——旧计数把 capacity/
                # permission 退出也算进每日轮换额度，挤占真正的择强换仓。
                if quality_action == "consolidation_exit":
                    _audit(
                        conn, position["account_id"], "quality_rotation",
                        f"{position['code']} 择强换仓，质量评分 {quality_review.get('score', 0):.1f}",
                        strategy_stamp=strategy_stamp,
                    )
                _audit(
                    conn, position["account_id"], "concentration_rotation",
                    f"{position['code']} 质量评分 {quality_review.get('score', 0):.1f}，释放额度等待高分候选 {((quality_review.get('replacement') or {}).get('code') or '下一轮选股')}",
                    strategy_stamp=strategy_stamp,
                )
                if quality_action == "permission_scope_exit":
                    _audit(
                        conn, position["account_id"], "permission_scope_exit",
                        f"{position['code']} {permission_reason}；本次卖出 {qty} 股，剩余 {detail['remaining_qty']} 股",
                        strategy_stamp=strategy_stamp,
                    )
                # Reducing an over-cap strategy must lower its stock count.
                # A score-based rotation may enter a stronger replacement;
                # capacity compression deliberately releases cash instead.
                replacement = (
                    {} if quality_action in {"capacity_exit", "permission_scope_exit"} or not detail["position_closed"]
                    else (quality_review.get("replacement") or {})
                )
                replacement_code = replacement.get("code")
                if replacement_code and replacement_code not in rotation_bought_codes:
                    # Replacement quotes were prefetched with the candidate
                    # snapshot before this write transaction.  Never start
                    # network or disk I/O after the risk ledger is locked.
                    replacement_quote = quote_map.get(replacement_code) or {}
                    replacement_news = [
                        row for row in news if str(row.get("code") or "") == str(replacement_code)
                    ]
                    replacement_result = _rotation_buy_candidate(
                        conn,
                        account_map.get(position["account_id"], {"id": position["account_id"]}),
                        replacement,
                        replacement_quote,
                        market_context,
                        replacement_news,
                        day,
                        all_quotes=quote_map,
                        ports=ports,
                        deps=ports.evidence,
                    )
                    rotation_results.append(replacement_result)
                    if replacement_result.get("filled"):
                        rotation_bought_codes.add(replacement_code)
                    detail["replacement_buy"] = replacement_result
            orders.append({
                "code": position["code"], "status": str(committed[0]), "qty": qty,
                "reason": reason, "concentration_rotation": concentration_triggered,
                "quality_score": quality_review.get("score"),
                "position_closed": detail["position_closed"],
                "remaining_qty": detail["remaining_qty"],
                "replacement_buy": detail.get("replacement_buy"),
            })
        ports.projection.sync_positions(conn, day)
        ports.projection.record_nav(conn, day, quote_map)
        risk_result = {
            "slot": "risk", "date": day.isoformat(),
            "orders": orders, "manual_orders": manual_orders,
            "concentration": {
                "reviewed": len(quality_reviews),
                "rotated": concentration_sells_used,
                "permission_scope_exits": permission_sells_used,
                "replacements": rotation_results,
                "max_per_run": ports.evidence.max_sells_per_run,
                "pool_market_value": round(pool_market_value, 2),
                "pool_nav": round(pool_nav, 2),
            },
        }
    # Pending/manual buys run only after all risk exits have committed.  A
    # failed pending batch remains retryable and never hides a completed sell.
    try:
        manual_orders = ports.process_pending_manual_orders(day)
    except Exception as exc:
        if _lease_lost(exc):
            raise
        manual_orders = [{"status": "pending_batch_retry", "reason": str(exc)}]
        with ports.open_db(immediate=True, hot_path=True) as audit_conn:
            _audit(audit_conn, None, "pending_manual_batch_retry", str(exc))
    risk_result["manual_orders"] = manual_orders
    return risk_result
