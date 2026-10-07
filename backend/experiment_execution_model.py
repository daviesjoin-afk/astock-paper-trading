"""Deterministic historical interpretation of explicitly pinned experiment assumptions.

Production order handling remains owned by the R26 paper execution path. This narrow
model supports one offline close-signal/next-open profile and reads no runtime settings.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

try:
    import experiment_contract as EC
    import candidate_experiment as CE
    import point_in_time as PIT
    import strategy_dsl_evaluator as EVAL
    import tradability_archive as TA
except ImportError:  # pragma: no cover
    from . import experiment_contract as EC
    from . import candidate_experiment as CE
    from . import point_in_time as PIT
    from . import strategy_dsl_evaluator as EVAL
    from . import tradability_archive as TA

PROFILE_VERSION = "r29-close-next-open-v1"


class ExperimentExecutionUnavailable(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _assumptions(spec: EC.ExperimentSpec, stress: Mapping[str, Any] | None = None) -> tuple[float, float, int, int, float, float, float, float]:
    execution = spec.execution_assumptions
    if execution.get("execution_profile_version") != PROFILE_VERSION:
        raise ExperimentExecutionUnavailable("execution_profile_unsupported")
    fill = execution.get("fill_assumptions")
    capacity = execution.get("capacity_assumptions")
    portfolio = spec.parameter_set.get("validation_portfolio")
    if not isinstance(fill, Mapping) or fill.get("signal") != "session_close" or fill.get("execution") != "next_session_open":
        raise ExperimentExecutionUnavailable("execution_assumptions_unproven")
    if execution.get("t_plus_one_semantics") != "sell_after_next_session":
        raise ExperimentExecutionUnavailable("execution_assumptions_unproven")
    if execution.get("price_limit_semantics") != "historical_tradability_archive_v1":
        raise ExperimentExecutionUnavailable("execution_assumptions_unproven")
    if execution.get("partial_fill_semantics") != "volume_participation_cap":
        raise ExperimentExecutionUnavailable("execution_assumptions_unproven")
    if not isinstance(capacity, Mapping) or not isinstance(portfolio, Mapping):
        raise ExperimentExecutionUnavailable("validation_portfolio_assumptions_missing")
    initial_cash, max_positions = portfolio.get("initial_cash"), portfolio.get("max_positions")
    sizing = portfolio.get("position_sizing")
    if (isinstance(initial_cash, bool) or not isinstance(initial_cash, (int, float))
            or not math.isfinite(float(initial_cash)) or initial_cash <= 0
            or isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 1
            or sizing != "equal_weight"):
        raise ExperimentExecutionUnavailable("validation_portfolio_assumptions_invalid")
    participation = capacity.get("participation_rate")
    cost_model = dict(spec.cost_model)
    cost_model.update((stress or {}).get("cost_model") or {})
    slippage = cost_model.get("slippage_parameters", {}).get("rate")
    if slippage is not None:
        slippage = float(slippage) * float((stress or {}).get("slippage_multiplier", 1.0))
    if (isinstance(participation, bool) or not isinstance(participation, (int, float))
            or not 0 < participation <= 1 or cost_model.get("slippage_model") != "fixed-rate-v1"
            or isinstance(slippage, bool) or not isinstance(slippage, (int, float))
            or not 0 <= slippage < 1):
        raise ExperimentExecutionUnavailable("execution_assumptions_unproven")
    commission, minimum, stamp = (cost_model.get(key) for key in
                                   ("commission_rate", "minimum_commission", "stamp_duty_rate"))
    return (float(initial_cash), float(participation), max_positions, 1,
            float(slippage), float(commission), float(minimum), float(stamp))


def simulate(spec: EC.ExperimentSpec, *, ast: Mapping[str, Any] | None = None,
             candidate_replay: CE.CandidateReplayDefinition | None = None,
             sessions: Sequence[str], members_by_session: Mapping[str, Sequence[Any]],
             bars: Sequence[Mapping[str, Any]], tradability_repository: Any,
             tradability_evidence: Mapping[tuple[str, str], Any] | None = None,
             financial_features: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
             required_financial_fields: Sequence[str] = (),
             stress: Mapping[str, Any] | None = None,
             include_trace: bool = False) -> dict[str, Any]:
    """Run one deterministic long-only replay; missing facts fail closed."""
    if (ast is None) == (candidate_replay is None):
        raise ExperimentExecutionUnavailable("ambiguous_replay_definition")
    if isinstance(spec, EC.CandidateExperimentSpec) and candidate_replay is None:
        raise ExperimentExecutionUnavailable("candidate_replay_definition_required")
    if candidate_replay is not None:
        if (not isinstance(candidate_replay, CE.CandidateReplayDefinition)
                or not isinstance(spec, EC.CandidateExperimentSpec) or candidate_replay.subject != spec.subject):
            raise ExperimentExecutionUnavailable("candidate_identity_mismatch")
        members_by_session = CE.filter_candidate_members(candidate_replay, members_by_session, spec.universe_fingerprint)
    stress = stress or {}
    execution_delay = stress.get("execution_delay_sessions", 0)
    signal_delay = stress.get("signal_delay_sessions", 0)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (execution_delay, signal_delay)):
        raise ExperimentExecutionUnavailable("session_delay_invalid")
    initial_cash, participation, max_positions, _unused, slippage, commission_rate, minimum_commission, stamp_rate = _assumptions(spec, stress)
    constraints = candidate_replay.candidate.constraints if candidate_replay is not None else {}
    max_positions = min(max_positions, constraints.get("max_positions", max_positions))
    if len(sessions) < 2:
        raise ExperimentExecutionUnavailable("execution_sessions_insufficient")
    bar_by_pair = {(row["code"], row["session"]): row for row in bars}
    history: dict[str, list[Mapping[str, Any]]] = {}
    positions: dict[str, dict[str, Any]] = {}
    cash = initial_cash
    total_cost = traded = capacity_sum = 0.0
    trade_count = 0
    capacity_count = 0
    equity_path = []
    exposure_sum = 0.0
    pending: dict[str, tuple[str, int]] = {}
    trace = []

    for index, session in enumerate(sessions):
        trades_before, cost_before, traded_before = trade_count, total_cost, traded
        day_members = sorted({str(item.get("code")) for item in members_by_session.get(session, ())
                              if isinstance(item, Mapping) and item.get("code")})
        current_bars = {code: bar_by_pair[(code, session)] for code in day_members
                        if (code, session) in bar_by_pair}
        for code in day_members:
            if code in current_bars:
                history.setdefault(code, []).append(current_bars[code])

        # Apply orders created by the prior close at this session's open.
        for code, (action, due_index) in sorted(list(pending.items())):
            if due_index > index:
                continue
            bar = current_bars.get(code)
            instant = f"{session}T09:30:00+08:00"
            evidence = ((tradability_evidence or {}).get((code, session))
                        if tradability_evidence is not None else
                        tradability_repository.evidence_at(code, session, instant)
                        if tradability_repository is not None else None)
            if evidence is None:
                raise ExperimentExecutionUnavailable("execution_tradability_unavailable")
            decision = TA.TradabilityEvaluator.evaluate(
                evidence, decision_time=instant,
                fingerprint=TA.evidence_fingerprint(evidence),
            )
            block_reason = (decision.buy_block_reason if action == "buy"
                            else decision.sell_block_reason)
            if block_reason == TA.TradabilityReason.UNKNOWN_STATE:
                raise ExperimentExecutionUnavailable("execution_tradability_unavailable")
            can_execute = decision.can_buy if action == "buy" else decision.can_sell
            if not can_execute:
                # A proven market block explains why no fill occurred. Unknown facts
                # were rejected above and cannot be recorded as strategy performance.
                pending.pop(code, None)
                continue
            if bar is None:
                raise ExperimentExecutionUnavailable("execution_market_bar_unavailable")
            direction = getattr(evidence, "price_limit_direction", None)
            if action == "buy" and (not decision.can_buy or direction == TA.PRICE_LIMIT_UP):
                continue
            if action == "sell" and (not decision.can_sell or direction == TA.PRICE_LIMIT_DOWN):
                continue
            volume = bar.get("volume")
            if isinstance(volume, bool) or not isinstance(volume, (int, float)) or volume <= 0:
                continue
            open_price = float(bar["open"])
            effective_volume = float(volume) * float(stress.get("liquidity_multiplier", 1.0))
            max_shares = math.floor(effective_volume * participation)
            if max_shares <= 0:
                continue
            if action == "buy" and code not in positions and len(positions) < max_positions:
                budget = min(cash, initial_cash / max_positions)
                if "max_weight_pct" in constraints:
                    budget = min(budget, initial_cash * constraints["max_weight_pct"])
                if "max_exposure_pct" in constraints:
                    gross = sum(position["shares"] * float(current_bars[name]["open"])
                                for name, position in positions.items() if name in current_bars)
                    budget = min(budget, max(0, initial_cash * constraints["max_exposure_pct"] - gross))
                price = open_price * (1 + slippage)
                shares = min(max_shares, math.floor(budget / price))
                if shares > 0:
                    notional = shares * price
                    cost = max(minimum_commission, notional * commission_rate)
                    while shares > 0 and notional + cost > cash:
                        shares -= 1; notional = shares * price
                        cost = max(minimum_commission, notional * commission_rate) if shares else 0.0
                    if shares:
                        cash -= notional + cost
                        positions[code] = {"shares": shares, "bought_index": index,
                                           "buy_notional": notional}
                        total_cost += cost; traded += notional; trade_count += 1
                        capacity_sum += shares / effective_volume; capacity_count += 1
            elif action == "sell" and code in positions:
                position = positions[code]
                if index <= position["bought_index"]:  # T+1: no same-session sale.
                    continue
                shares = min(position["shares"], max_shares)
                if shares <= 0:
                    continue
                price = open_price * (1 - slippage)
                notional = shares * price
                cost = max(minimum_commission, notional * commission_rate) + notional * stamp_rate
                cash += notional - cost
                position["shares"] -= shares
                total_cost += cost; traded += notional; trade_count += 1
                capacity_sum += shares / effective_volume; capacity_count += 1
                if position["shares"] == 0:
                    positions.pop(code)
            pending.pop(code, None)

        # Close marks and close-time signal decisions for next session.
        market_value = 0.0
        for code, position in list(positions.items()):
            row = current_bars.get(code)
            if row is None:
                raise ExperimentExecutionUnavailable("market_bar_missing_for_open_position")
            market_value += position["shares"] * float(row["close"])
        equity = cash + market_value
        equity_path.append(equity)
        exposure_sum += (market_value / equity) if equity > 0 else 0.0
        if include_trace:
            prior_equity = equity_path[-2] if len(equity_path) > 1 else initial_cash
            trace.append({"session": session, "equity": equity,
                          "daily_return": equity / prior_equity - 1 if prior_equity > 0 else None,
                          "exposure": market_value / equity if equity > 0 else 0.0,
                          "trade_count_delta": trade_count - trades_before,
                          "cost_delta": total_cost - cost_before,
                          "turnover_delta": (traded - traded_before) / initial_cash})
        if index + 1 >= len(sessions):
            continue
        next_session = sessions[index + 1]
        next_members = {str(item.get("code")) for item in members_by_session.get(next_session, ())
                        if isinstance(item, Mapping) and item.get("code")}
        signals = {}
        exits = {}
        for code in sorted(set(day_members) | set(positions)):
            rows = history.get(code, ())
            snapshot = {field: [row.get(field) for row in rows]
                        for field in ("open", "high", "low", "close", "volume", "amount")}
            if required_financial_fields and code in next_members:
                field_values = []
                for row in rows:
                    entry = (financial_features or {}).get((code, str(row.get("session"))))
                    if (not isinstance(entry, Mapping)
                            or any(name not in entry for name in required_financial_fields)):
                        raise ExperimentExecutionUnavailable("financial_feature_evidence_missing")
                    decision_at = entry.get("decision_at")
                    close_at = PIT.bar_available_at(str(row.get("session")))
                    decision = PIT.parse_asof(decision_at)
                    if decision is None or close_at is None or decision < close_at:
                        raise ExperimentExecutionUnavailable("financial_feature_decision_mismatch")
                    field_values.append(entry)
                snapshot["financials"] = {
                    name: [entry.get(name) for entry in field_values]
                    for name in required_financial_fields
                }
            if candidate_replay is None:
                signals[code] = EVAL.evaluate(ast, snapshot) if rows and code in next_members else False
            elif rows:
                signals[code], exits[code] = candidate_replay.signals(snapshot)
                signals[code] = signals[code] and code in next_members
            else:
                signals[code], exits[code] = False, False
        for code in sorted(set(positions) | set(signals)):
            sell_signal = exits.get(code, False) if candidate_replay is not None else not signals.get(code, False)
            action = ("sell" if code in positions and sell_signal else
                      "buy" if code not in positions and signals.get(code, False) else None)
            if action is not None and (not execution_delay and not signal_delay or code not in pending):
                due_index = index + 1 + signal_delay + execution_delay
                if due_index >= len(sessions):
                    raise ExperimentExecutionUnavailable("delayed_execution_outside_coverage")
                pending[code] = (action, due_index)

    if not equity_path:
        raise ExperimentExecutionUnavailable("execution_result_unavailable")
    returns = [(equity_path[i] / equity_path[i - 1] - 1) for i in range(1, len(equity_path))
               if equity_path[i - 1] > 0]
    peak, drawdown = equity_path[0], 0.0
    for value in equity_path:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1) if peak else drawdown
    mean_return = sum(returns) / len(returns) if returns else 0.0
    variance = sum((value - mean_return) ** 2 for value in returns) / len(returns) if returns else 0.0
    result = {"total_return": equity_path[-1] / initial_cash - 1,
            "max_drawdown": drawdown, "volatility": math.sqrt(variance),
            "turnover": traded / initial_cash, "trade_count": trade_count,
            "total_cost": total_cost, "exposure": exposure_sum / len(equity_path),
            "capacity_proxy": capacity_sum / capacity_count if capacity_count else 0.0,
            "data_coverage": 1.0, "regime_breakdown": {}}
    if include_trace:
        result["trace"] = trace
    return result


def simulate_with_trace(**kwargs) -> dict[str, Any]:
    """Run the same execution loop and include its deterministic session trace."""
    return simulate(**kwargs, include_trace=True)
