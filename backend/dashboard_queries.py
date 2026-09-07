# -*- coding: utf-8 -*-
"""纸盘只读查询（read-model）模块。

从 ``paper_trading.py`` 拆出的 dashboard 工作区只读投影。模块刻意不依赖
``paper_trading`` 的顶层导入顺序：对仍驻留在主编排文件中的共享辅助采用
函数体延迟导入，避免与主模块产生循环依赖。当前阶段仅承载 ``dashboard()``，
后续按同一模式继续迁出其它只读查询（risk/audit/read-model helpers）。
"""

import datetime as dt
import sqlite3
import time

def dashboard(include_activity=False, include_history_symbols=False):
    """Return the paper workspace read model without hidden-page ledger scans.

    The browser requests cross-cycle orders and the history selector only in
    their own workspaces.  Keeping those lifetime-growing queries out of the
    portfolio view is the difference between a fast refresh and a full ledger
    report on every click.
    """

    from paper_trading import (  # deferred: dashboard_queries stays import-order independent of the monolith
        ACCOUNT_SPECS, ENTRY_FROZEN_WAITLIST_STATUS, LOT_SIZE, SHARED_POOL_MAX_POSITIONS,
        STRATEGY_MAX_POSITIONS, _account_metric_inputs, _account_metrics,
        _account_reference_capital, _active_account_rows, _active_cycle, _date, _db,
        _dynamic_position_limits, _economic_pool_nav_history, _entry_freeze_status,
        _hold_days, _latest_price_map, _loads, _market_session, _market_state, _num,
        _pending_position_slots, _position_rows, _recent_orders_with_archives, _rows,
        _schedule_cache, _shared_initial_cash, _shared_metrics, _strategy_pool_budget,
        _today_position_performance, dfc, schedule_status,
    )

    # Cache schedule_status to avoid repeated init_db() calls
    now_ts = time.time()
    if _schedule_cache["data"] is not None and now_ts - _schedule_cache["ts"] < 60:
        schedule = _schedule_cache["data"]
    else:
        schedule = schedule_status()
        _schedule_cache["data"] = schedule
        _schedule_cache["ts"] = now_ts
    market_session = _market_session()
    with _db() as conn:
        cycle = _active_cycle(conn)
        account_rows = _active_account_rows(conn)
        positions = _position_rows(conn, asof_day=dt.date.today())
        today = dt.date.today().isoformat()
        today_sell_codes = {
            str(row["code"]) for row in conn.execute(
                """SELECT DISTINCT code FROM paper_orders
                   WHERE side='sell' AND status='filled'
                     AND substr(COALESCE(executed_at,created_at),1,10)=?""",
                (today,),
            ).fetchall()
        }
        codes = sorted({p["code"] for p in positions} | today_sell_codes)
        # A browser refresh must never perform two network quote requests plus
        # a flow enrichment while the database read transaction is open.  That
        # made the overview and health endpoints block behind a slow provider.
        # Trading/risk paths still call ``_quotes`` and retain their strict
        # dual-source validation; the dashboard is deliberately a fast,
        # cache-only read model.
        # 估值价格源：优先最近一次全市场快照缓存（15:15 收盘快照与风控刷新
        # 维护）。universe.json 是低频重建的股票池清单——其 price 字段曾被
        # 当作实时估值，7/28 版本的陈旧价格把全池 NAV 压低约 1.2 万并伪造
        # 出“今日亏损”，因此只作缺省兜底。
        quotes = {}
        _snapshot_rows = dfc.load_market_snapshot_full_cached()
        _snapshot_by_code = {
            str(row.get("code")): row for row in _snapshot_rows
            if row.get("code") and isinstance(row.get("price"), (int, float))
        } if _snapshot_rows else {}
        for _code in codes or []:
            row = _snapshot_by_code.get(str(_code))
            if row is not None:
                quotes[str(_code)] = dict(row)
        for _code, row in (_latest_price_map(codes) if codes else {}).items():
            existing = quotes.get(_code)
            if not isinstance(existing, dict) or not isinstance(existing.get("price"), (int, float)):
                quotes[_code] = row
        for quote in quotes.values():
            quote.setdefault("quote_source", "dashboard_cache")
            quote.setdefault("quote_validation", "dashboard_cached")
        # Keep the latest concentration review alongside each holding.  The
        # review is written by the risk cycle (not by the browser), so the
        # dashboard remains a read-only view and never creates a sell order.
        review_rows = _rows(
            conn,
            """SELECT * FROM paper_position_reviews
               WHERE cycle_id=? AND id IN (
                   SELECT MAX(id) FROM paper_position_reviews
                   WHERE cycle_id=? GROUP BY account_id,code
               )
               ORDER BY score ASC,id DESC""",
            (cycle["id"], cycle["id"]),
        )
        review_map = {(row["account_id"], row["code"]): row for row in review_rows}
        metric_cache = _account_metric_inputs(
            conn, [row["id"] for row in account_rows], today
        )
        sold_codes = sorted({
            str(order.get("code"))
            for rows in (metric_cache.get("sells") or {}).values()
            for order in rows
            if order.get("code")
        })
        # ``_account_metrics`` normally backfills today's sold symbols with a
        # live quote.  The compact risk read model is intentionally
        # network-free, so provide its cache-only marks up front instead.
        for code, row in (_latest_price_map(sold_codes) if sold_codes else {}).items():
            quotes.setdefault(code, row)
        accounts = [
            _account_metrics(
                conn,
                row,
                quotes=quotes,
                positions=[p for p in positions if p["account_id"] == row["id"]],
                metric_cache=metric_cache,
            )
            for row in account_rows
        ]
        shared = _shared_metrics(conn, cycle, positions, quotes)
        # Publish the same fair-budget calculation used by order sizing so
        # the dashboard, risk center and audit detail never show three copies
        # of the global 82% ceiling.
        account_rows_by_id = {row["id"]: row for row in account_rows}
        count_budget = _dynamic_position_limits(conn)
        pending_slots = _pending_position_slots(conn, positions)
        occupied_pool_slots = len({
            (str(item.get("account_id")), str(item.get("code")))
            for item in positions if int(_num(item.get("qty"))) >= LOT_SIZE
        } | pending_slots)
        free_pool_slots = max(0, int(count_budget["pool_limit"]) - occupied_pool_slots)
        shared["dynamic_position_slots_used"] = occupied_pool_slots
        shared["dynamic_position_slots_available"] = free_pool_slots
        # Make the auto entry circuit-breaker explain itself in the same
        # read-only dashboard response.  A frozen waitlist without these
        # checks looks identical to a strategy producing no candidates.
        shared["entry_freeze"] = _entry_freeze_status()
        shared["slot_borrow_policy"] = (
            "高分候选可从共享池未使用席位或其他策略空闲席位借用1席；"
            f"不突破硬上限{SHARED_POOL_MAX_POSITIONS}，仍须通过行情、资金、T+1与风控门禁"
        )
        # Allocation and borrowing are execution controls, so expose the same
        # version/history that _buy_order used.  The UI can now distinguish
        # "策略本身有6席" from "本轮从空闲席位借来1席", rather than making a
        # changed upper limit look like an unexplained manual override.
        active_version_id = int(str(count_budget.get("allocation_version") or "slots-v0").rsplit("v", 1)[-1] or 0)
        version_row = conn.execute(
            "SELECT id,pool_limit,limits,weights,inputs,source,effective_at FROM paper_position_limit_versions WHERE id=?",
            (active_version_id,),
        ).fetchone()
        version_inputs = _loads(version_row["inputs"], {}) if version_row else {}
        borrow_events = list(version_inputs.get("slot_borrow_events") or [])[-9:]
        shared["slot_allocation"] = {
            "hard_cap": SHARED_POOL_MAX_POSITIONS,
            "deployable_cap": int(count_budget.get("pool_limit") or 0),
            "limits": count_budget.get("limits") or {},
            "weights": count_budget.get("weights") or {},
            "version": count_budget.get("allocation_version"),
            "effective_at": count_budget.get("effective_at"),
            "source": count_budget.get("source"),
        }
        shared["slot_borrow_audit"] = borrow_events
        shared["slot_borrow_last"] = borrow_events[-1] if borrow_events else None
        for account in accounts:
            account_source = account_rows_by_id.get(account["id"], account)
            account["max_positions"] = max(
                1,
                int(count_budget["limits"].get(account["id"], (ACCOUNT_SPECS.get(account["id"]) or {}).get("max_positions", 5))),
            )
            account["position_limit_dynamic"] = True
            account["pool_position_limit"] = count_budget["pool_limit"]
            account["position_limit_source"] = count_budget["source"]
            account["position_limit_version"] = count_budget["allocation_version"]
            account["dynamic_position_slots_used"] = occupied_pool_slots
            account["dynamic_position_slots_available"] = free_pool_slots
            account["slot_borrow_available"] = bool(
                free_pool_slots > 0 or int(account.get("max_positions", 0)) < STRATEGY_MAX_POSITIONS
            )
            account["slot_borrow_policy"] = "高分候选自动借用1个空闲席位；借位不放宽资金/行情/风控"
            account["position_limit_excess"] = max(
                0, int(_num(account.get("position_count"))) - account["max_positions"],
            )
            # P3 审计修复（R6）：传入 market 使黄灯系数进入展示预算——
            # 旧口径黄/红灯下 deployment_remaining 系统性虚高。市场灯用
            # 已加载的缓存快照计算（allow_network=False，无新增网络开销）。
            try:
                _dash_market = _market_state(
                    _date(None), live_universe=_snapshot_rows,
                    allow_network=False,
                )
            except Exception:
                _dash_market = None
            budget = _strategy_pool_budget(
                conn, account_source,
                shared.get("nav"), positions, quotes,
                market=_dash_market,
            )
            account["strategy_budget_pct"] = budget["target_pct"]
            account["strategy_floor_pct"] = budget["floor_pct"]
            account["strategy_position_pct_pool"] = budget["current_pct"]
            account["strategy_position_value"] = budget["current_amount"]
            account["strategy_pending_reserve_amount"] = budget.get("pending_reserve_amount", 0.0)
            account["strategy_committed_amount"] = budget.get("current_total_amount", budget["current_amount"])
            account["strategy_budget_amount"] = budget["target_amount"]
            account["strategy_floor_amount"] = budget["floor_amount"]
            account["strategy_allowance_amount"] = budget["allowance_amount"]
            account["strategy_redistribution_amount"] = budget["redistribution_amount"]
            account["pool_exposure_pct"] = budget["pool_exposure_pct"]
            account["pool_limit_pct"] = budget["pool_limit_pct"]
            account["strategy_budget"] = budget
            # Existing consumers use these names for the visible budget.
            account["fund_utilization_pct"] = budget["current_pct"]
            account["deployment_limit_pct"] = budget["target_pct"]
            account["deployment_remaining"] = budget["allowance_amount"]
        # Never scale strategy P&L to hide a ledger discrepancy.  Publish the
        # independently calculated totals and an explicit reconciliation gap.
        raw_total = sum(_num(account.get("total_pnl")) for account in accounts)
        pool_total = _num(shared.get("nav")) - _num(shared.get("initial_cash"))
        raw_today = sum(_num(account.get("today_pnl")) for account in accounts)
        pool_today = shared.get("today_pnl")
        pool_today_base = shared.get("today_baseline_nav")
        today_complete = all(account.get("today_pnl") is not None for account in accounts)
        # ``paper_nav`` stores strategy-synthetic NAV: it intentionally
        # excludes cash transferred between strategy attribution buckets.
        # Comparing today's real shared cash+market value with yesterday's
        # sum of those synthetic rows turns an internal transfer into a large
        # fake daily profit/loss.  The four independently calculated daily
        # contributions are exhaustive and transfer-neutral, so use their sum
        # for the visible shared-pool daily P&L.  Preserve the legacy ledger
        # comparison explicitly for reconciliation diagnostics.
        shared["ledger_today_pnl"] = pool_today
        shared["ledger_today_baseline_nav"] = pool_today_base
        shared["ledger_today_reconciliation_delta"] = round(_num(pool_today) - raw_today, 2) if (
            pool_today is not None and today_complete
        ) else None
        if today_complete:
            economic_today = round(raw_today, 2)
            economic_base = _num(shared.get("nav")) - economic_today
            shared["today_pnl"] = economic_today
            shared["today_baseline_nav"] = round(economic_base, 2) if economic_base > 0 else None
            shared["today_return_pct"] = round(economic_today / economic_base * 100, 2) if economic_base > 0 else None
            shared["daily_loss_pct"] = round(max(0.0, -economic_today / economic_base * 100), 2) if economic_base > 0 else 0.0
            shared["today_pnl_source"] = "strategy_contribution_sum"
        for account in accounts:
            account["strategy_attributed_pnl"] = round(_num(account.get("total_pnl")), 2)
            account["strategy_attributed_today_pnl"] = account.get("today_pnl")
            account["strategy_pnl_pct_pool"] = round(
                account["strategy_attributed_pnl"] / max(_num(shared.get("initial_cash")), 1) * 100, 2
            )
            account["strategy_today_pct_pool"] = round(
                account["strategy_attributed_today_pnl"] / max(_num(shared.get("today_baseline_nav")), 1) * 100, 2
            ) if account.get("strategy_attributed_today_pnl") is not None and shared.get("today_baseline_nav") else None
        shared["strategy_pnl_sum"] = round(raw_total, 2)
        shared["pnl_reconciliation_delta"] = round(pool_total - raw_total, 2)
        shared["strategy_today_pnl_sum"] = round(raw_today, 2) if today_complete else None
        # The visible pool number and the five strategy cards now use the same
        # transfer-neutral definition and must reconcile exactly.  The former
        # synthetic-ledger discrepancy remains in ledger_* fields above.
        shared["today_pnl_reconciliation_delta"] = round(
            _num(shared.get("today_pnl")) - raw_today, 2
        ) if shared.get("today_pnl") is not None and today_complete else None
        for pos in positions:
            quality = review_map.get((pos["account_id"], pos["code"])) or {}
            pos["quality_score"] = _num(quality.get("score"), None)
            pos["quality_grade"] = quality.get("grade")
            pos["quality_action"] = quality.get("action")
            pos["quality_replacement_code"] = quality.get("replacement_code")
            pos["quality_replacement_score"] = _num(quality.get("replacement_score"), None)
            pos["quality_reasons"] = quality.get("reasons")
            pos["quality_review_date"] = quality.get("review_date")
            quality_detail = _loads(quality.get("detail"), {}) if quality else {}
            pos["quality_review_phase"] = quality_detail.get("review_phase")
            pos["quality_weights"] = quality_detail.get("weights")
            pos["quality_model_score"] = _num(quality_detail.get("model_score"), None)
            pos["quality_trend_score"] = _num(quality_detail.get("trend_score"), None)
            pos["quality_flow_score"] = _num(quality_detail.get("flow_score"), None)
            pos["quality_momentum_score"] = _num(quality_detail.get("momentum_score"), None)
            pos["quality_return_score"] = _num(quality_detail.get("return_score"), None)
            pos["quality_news_penalty"] = _num(quality_detail.get("news_penalty"), 0.0)
            pos["main_force_intent"] = quality_detail.get("main_force_intent")
            price = _num((quotes.get(pos["code"]) or {}).get("price"), _num(pos["cost"]))
            spec = ACCOUNT_SPECS.get(pos["account_id"]) or {"hard_stop": -0.05}
            pos["price"] = round(price, 2)
            pos["market_value"] = round(price * _num(pos["qty"]), 2)
            pos["cost_value"] = round(_num(pos["cost"]) * _num(pos["qty"]), 2)
            pos["account_weight_pct"] = round(pos["market_value"] / max(shared["nav"], 1) * 100, 2)
            pos["pool_weight_pct"] = pos["account_weight_pct"]
            pos["unrealized_pnl"] = round(pos["market_value"] - pos["cost_value"], 2)
            pos["ret_pct"] = round((price / _num(pos["cost"]) - 1) * 100, 2) if pos.get("cost") else None
            quote = quotes.get(pos["code"]) or {}
            pos["today_pnl"], pos["today_return_pct"], pos["today_baseline"] = _today_position_performance(
                pos, price, quote, dt.date.today()
            )
            pos["today_pnl_status"] = market_session["label"] if pos["today_pnl"] is None else ""
            pos["hold_days"] = _hold_days(pos, dt.date.today())
            pos["quote_at"] = (quotes.get(pos["code"]) or {}).get("quote_at")
            pos["quote_source"] = (quotes.get(pos["code"]) or {}).get("quote_source")
            pos["quote_validation"] = (quotes.get(pos["code"]) or {}).get("quote_validation")
            pos["quote_cross_check"] = (quotes.get(pos["code"]) or {}).get("quote_cross_check")
            pos["main_net"] = (quotes.get(pos["code"]) or {}).get("main_net")
            pos["super_net"] = (quotes.get(pos["code"]) or {}).get("super_net")
            pos["big_net"] = (quotes.get(pos["code"]) or {}).get("big_net")
            pos["mid_net"] = (quotes.get(pos["code"]) or {}).get("mid_net")
            pos["small_net"] = (quotes.get(pos["code"]) or {}).get("small_net")
            pos["main_pct"] = (quotes.get(pos["code"]) or {}).get("main_pct")
            pos["turnover"] = (quotes.get(pos["code"]) or {}).get("turnover")
            pos["vol_ratio"] = (quotes.get(pos["code"]) or {}).get("vol_ratio")
            pos["risk_price"] = round(_num(pos["cost"]) * (1 + spec["hard_stop"]), 3)
            pos["t1_status"] = "可卖" if int(pos.get("available_qty") or 0) > 0 else "今日不可卖"
            pos["t1_reason"] = (
                "已达到最早可卖日期"
                if int(pos.get("available_qty") or 0) > 0
                else f"买入份额锁定至 {pos.get('available_date')}"
            )
            ret = _num(pos.get("ret_pct"))
            pos["price_state"] = (
                "触及风控线" if price <= pos["risk_price"]
                else "盈利运行" if ret > 1
                else "弱势观察" if ret < -1
                else "成本附近"
            )
        account_names = {item["id"]: item["name"] for item in accounts}
        for account in accounts:
            account["position_count"] = sum(1 for p in positions if p["account_id"] == account["id"])
            account["pending_position_slots"] = sum(
                1 for pending_account, _ in pending_slots if pending_account == account["id"]
            )
            account["committed_position_count"] = (
                account["position_count"] + account["pending_position_slots"]
            )
            account["position_limit_excess"] = max(
                0, account["position_count"] - int(_num(account.get("max_positions"), 999)),
            )
            account["pending_order_count"] = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status IN ('pending_limit',?)",
                (account["id"], ENTRY_FROZEN_WAITLIST_STATUS),
            ).fetchone()[0]
            account["cooldown_until"] = conn.execute(
                "SELECT cooldown_until FROM paper_accounts WHERE id=?", (account["id"],)
            ).fetchone()[0]
        shared["position_limit"] = count_budget["pool_limit"]
        shared["pending_position_slots"] = len(pending_slots)
        shared["committed_position_count"] = shared.get("position_count", 0) + len(pending_slots)
        shared["position_limit_excess"] = max(0, shared.get("position_count", 0) - count_budget["pool_limit"])
        shared["position_limits"] = count_budget["limits"]
        shared["position_limit_dynamic"] = True
        shared["position_limit_version"] = count_budget["allocation_version"]
        # Recent activity is a cross-cycle audit view: merge immutable reset
        # snapshots only for the visible activity workspace.  Its archive scan
        # is intentionally never run for a portfolio refresh.
        orders = _recent_orders_with_archives(conn, account_names, limit=500) if include_activity else []
        fills = []
        risk_decisions = []
        if include_activity:
            # The activity workspace renders orders, the account strip and the
            # audit board only.  The candidate-signal projection (json_extract
            # across 120 payloads plus the execution join and the overlap
            # matrix) and the reviews/jobs reads are portfolio-view sections:
            # on the production ledger they were ~0.9MB of the 1.5MB response
            # and a measurable slice of every cold rebuild.  Portfolio reads
            # keep the full shape; the activity cache key is already distinct
            # (overview:1:*), so skipping them here cannot starve the other
            # workspace.
            signals = []
            candidate_overlap = []
            reviews = []
            last_jobs = []
        else:
            # Do not load the full signal payload here.  A signal's immutable
            # decision snapshot can be hundreds of KB, and loading 120 of them
            # just to render a candidate card was the main cold-page memory spike.
            # SQLite extracts the two small presentation fragments in-process;
            # the complete evidence remains in the ledger for the dedicated audit
            # endpoints and never needs to live in the web response cache.
            signal_fields = (
                "id,account_id,signal_date,intended_date,code,name,industry,close_price,"
                "rank_score,t_tier,t_score,status,reason,created_at"
            )
            try:
                signals = _rows(
                    conn,
                    f"""SELECT {signal_fields},
                               json_extract(payload,'$.pick.sector_heat') AS sector_heat_json,
                               json_extract(payload,'$.decision.entry_model') AS entry_model_json
                        FROM paper_signals WHERE intended_date=?
                        ORDER BY account_id,rank_score DESC,id DESC LIMIT 120""",
                    (dt.date.today().isoformat(),),
                )
            except sqlite3.OperationalError:
                # Older SQLite builds may omit JSON1.  Preserve a fast, useful
                # dashboard instead of falling back to fetching the large payload.
                signals = _rows(
                    conn,
                    f"""SELECT {signal_fields} FROM paper_signals WHERE intended_date=?
                        ORDER BY account_id,rank_score DESC,id DESC LIMIT 120""",
                    (dt.date.today().isoformat(),),
                )
            signal_ids = [int(signal["id"]) for signal in signals]
            execution_by_signal = {}
            if signal_ids:
                placeholders = ",".join("?" for _ in signal_ids)
                execution_rows = _rows(
                    conn,
                    f"""SELECT o.signal_id,o.status AS order_status,o.executed_at,o.filled_price,
                               f.quote_at AS execution_quote_at
                        FROM paper_orders o
                        LEFT JOIN paper_fills f ON f.order_id=o.id
                        WHERE o.signal_id IN ({placeholders})
                        ORDER BY o.id DESC""",
                    tuple(signal_ids),
                )
                for row in execution_rows:
                    execution_by_signal.setdefault(int(row["signal_id"]), row)
            for signal in signals:
                sector_heat = _loads(signal.pop("sector_heat_json", None), {}) or {}
                entry_model = _loads(signal.pop("entry_model_json", None), {}) or {}
                execution = execution_by_signal.get(int(signal["id"])) or {}
                signal["audit"] = {
                    "factor_date": signal.get("signal_date"),
                    "signal_quote_at": None,
                    "signal_quote_pct": None,
                    "signal_quote_source": None,
                    "decision_at": signal.get("created_at"),
                    "planned_review_date": signal.get("intended_date"),
                    "signal_mode": "overview_compact",
                    "execution_status": execution.get("order_status") or "not_executed",
                    "executed_at": execution.get("executed_at"),
                    "execution_quote_at": execution.get("execution_quote_at"),
                    "execution_price": execution.get("filled_price"),
                }
                signal["payload"] = {
                    "pick": {"sector_heat": sector_heat},
                    "decision": {"entry_model": entry_model},
                }
                signal["account_name"] = account_names.get(signal["account_id"], signal["account_id"])
            signal_sets = {}
            for signal in signals:
                signal_sets.setdefault(signal["account_id"], set()).add(signal["code"])
            candidate_overlap = []
            account_ids = [account["id"] for account in accounts]
            for index, left in enumerate(account_ids):
                for right in account_ids[index + 1:]:
                    intersection = sorted(signal_sets.get(left, set()) & signal_sets.get(right, set()))
                    union = signal_sets.get(left, set()) | signal_sets.get(right, set())
                    candidate_overlap.append({
                        "left": left, "left_name": account_names.get(left, left),
                        "right": right, "right_name": account_names.get(right, right),
                        "count": len(intersection), "codes": intersection,
                        "jaccard_pct": round(len(intersection) / len(union) * 100, 1) if union else 0.0,
                    })
            reviews = _rows(conn, "SELECT * FROM paper_reviews ORDER BY week_key DESC, account_id LIMIT 4")
            last_jobs = _rows(conn, "SELECT * FROM paper_jobs ORDER BY market_date DESC, started_at DESC LIMIT 8")
        history_symbols = _rows(
            conn,
            """SELECT code,MAX(name) AS name,COUNT(*) AS order_count,
                      MAX(COALESCE(executed_at,created_at)) AS last_activity_at
                 FROM paper_orders GROUP BY code
                 ORDER BY last_activity_at DESC,code""",
        ) if include_history_symbols else []
        monitor_runs = _rows(conn, "SELECT * FROM paper_job_runs ORDER BY started_at DESC LIMIT 24")
        for run in monitor_runs:
            detail = _loads(run.get("detail"), {})
            bootstrap = detail.get("bootstrap") or {}
            run["detail"] = {
                "reason": detail.get("reason"),
                "error": detail.get("error"),
                "observed": detail.get("observed"),
                "bootstrap": {"reason": bootstrap.get("reason")} if bootstrap else None,
            }
        observations = _rows(conn, "SELECT * FROM paper_intraday_observations WHERE cycle_id=? ORDER BY id DESC LIMIT 30", (cycle["id"],))
        for item in observations:
            item.pop("payload", None)
        versions = _rows(conn, "SELECT * FROM paper_parameter_versions WHERE cycle_id=? ORDER BY id DESC LIMIT 12", (cycle["id"],))
        for item in versions:
            item["params"] = _loads(item.get("params"))
        archives = _rows(conn, "SELECT id,cycle_key,reason,created_at FROM paper_archives ORDER BY id DESC LIMIT 12")
        exposure = {}
        for pos in positions:
            value = _num(pos["price"]) * _num(pos["qty"])
            exposure[pos.get("industry") or "未知"] = round(exposure.get(pos.get("industry") or "未知", 0.0) + value, 2)
        # Today's account cards are strategy attribution views.  The only
        # authoritative portfolio-level daily P&L is the shared-pool value;
        # summing strategy attribution can double-count marks around fills.
        today_pnl = shared.get("today_pnl")
        today_baseline = shared.get("today_baseline_nav")
        today_available = today_pnl is not None and today_baseline
        today_summary = {
            "asof_date": dt.date.today().isoformat(),
            "available_accounts": 1 if today_available else 0, "account_count": len(accounts),
            "pnl": round(today_pnl, 2) if today_pnl is not None else None,
            "return_pct": round(today_pnl / today_baseline * 100, 2) if today_pnl is not None and today_baseline else None,
            "baseline_nav": round(today_baseline, 2) if today_available else None,
            "pnl_available": today_pnl is not None,
            "market_session": market_session["code"],
            "market_session_label": market_session["label"],
        }
        nav_rows = _rows(
            conn,
            """SELECT account_id,nav_date,nav,benchmark,created_at
               FROM paper_nav ORDER BY nav_date,account_id""",
        )
        nav_dates = sorted({str(row.get("nav_date")) for row in nav_rows if row.get("nav_date")})
        nav_by_account = {}
        benchmark_by_date = {}
        for row in nav_rows:
            nav_by_account.setdefault(row["account_id"], {})[row["nav_date"]] = _num(row.get("nav"), None)
            if _num(row.get("benchmark"), None) is not None:
                benchmark_by_date[row["nav_date"]] = _num(row.get("benchmark"), None)
        curve_series = []
        shared_initial = _shared_initial_cash(conn, cycle)
        shared_values = [
            {
                "date": row["nav_date"],
                "nav": round(_num(row.get("nav")), 2),
                "return_pct": round((_num(row.get("nav")) / shared_initial - 1) * 100, 3)
                if shared_initial else None,
            }
            for row in _economic_pool_nav_history(conn, cycle)
        ]
        curve_series.append({"id": "shared_pool", "name": "总资金池", "values": shared_values})
        for account in accounts:
            initial = _account_reference_capital(account)
            values = []
            for nav_date in nav_dates:
                nav = nav_by_account.get(account["id"], {}).get(nav_date)
                values.append({
                    "date": nav_date,
                    "nav": round(nav, 2) if nav is not None else None,
                    "return_pct": round((nav / initial - 1) * 100, 3) if nav is not None and initial else None,
                })
            curve_series.append({"id": account["id"], "name": account["name"], "values": values})
        benchmark_base = next((value for value in (benchmark_by_date.get(day) for day in nav_dates) if value), None)
        benchmark_series = [
            {
                "date": nav_date,
                "value": round(benchmark_by_date.get(nav_date), 2) if benchmark_by_date.get(nav_date) is not None else None,
                "return_pct": (
                    round((benchmark_by_date[nav_date] / benchmark_base - 1) * 100, 3)
                    if benchmark_base and benchmark_by_date.get(nav_date) is not None else None
                ),
            }
            for nav_date in nav_dates
        ]
        equity_curve = {
            "dates": nav_dates,
            "series": curve_series,
            "benchmark": {"name": "沪深300（收盘快照）", "values": benchmark_series},
            "asof": max((row.get("created_at") for row in nav_rows if row.get("created_at")), default=None),
        }
        return {
            "accounts": accounts, "shared": shared, "capital_model": "shared_pool",
            "positions": positions,
            # 守仓评分明细（约 275KB）只服务于持仓卡片的 quality_* 投影，
            # 浏览器从不消费整表；activity 工作区不再重复下发。
            "position_reviews": [] if include_activity else review_rows,
            "orders": orders, "signals": signals,
            "today_summary": today_summary,
            "history_symbols": history_symbols,
            "candidate_overlap": candidate_overlap,
            "fills": fills, "risk_decisions": risk_decisions,
            "reviews": reviews, "jobs": last_jobs, "monitor_runs": monitor_runs,
            "observations": observations, "parameter_versions": versions,
            "archives": archives, "cycle": cycle, "industry_exposure": exposure, "schedule": schedule,
            "equity_curve": equity_curve,
            "disclaimer": "模拟交易，不连接券商；成交为快照价叠加费用和滑点的规则化假设，不代表真实可成交价格。",
        }
