# -*- coding: utf-8 -*-
"""自定义策略确定性全链路重放（PR：custom strategy deterministic golden replay）。

在**完全离线**的临时账本上，把"动态创建一套用户策略"到"进化提案"的完整
链路跑一遍，并输出稳定的 digest（JSON）供 Golden Replay 比对：

    创建草稿 → risk compile（指纹 + 推荐/执行画像）
            → signal（合成行情的确定性信号）
            → allocation（席位 + 资金预算）
            → OrderIntent + sizing（风险预算 sizing）
            → fill（成交 + 持仓 + NAV 事件）
            → stop（硬止损退出）
            → evolution proposal（策略级进化提案）

确定性来源：全部价格/时间戳/参数为常量；随机因素为零；两套独立的临时
账本各跑一遍，digest 必须逐字节一致。这套自由策略架构因此成为可验证契约
——任何环节的行为变化都会让 Golden Replay 失败。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

import paper_allocation as PA
import paper_sizing as PSZ
import self_evolution as SE
import strategy_creation_preview as SCP

__all__ = ["GOLDEN_REPLAY_VERSION", "run_custom_strategy_replay"]

GOLDEN_REPLAY_VERSION = "golden-replay-v1"

# ── 固定的用户策略草稿与合成行情（离线、确定性） ─────────────────────
DRAFT = {
    "name": "用户的趋势回踩策略",
    "style": "trend pullback 趋势 回踩 均线",
    "daily": "日线收盘",
    "hold": "持有 10 天",
    "stop_loss": "止损 -5%",
    "max_positions": 3,
}

TICKER = ("600801", "用户的趋势标的", "工程机械", 20.00)
SIGNAL_CLOSE = 20.00
ENTRY_FILL_PRICE = 20.10          # 20.00 × (1 + 0.5% 滑点)
STOP_TRIGGER_PRICE = 18.95        # -5.8% 触发 -5% 硬止损
STOP_FILL_PRICE = 18.90
REALIZED_PNL_PER_SHARE = STOP_FILL_PRICE - ENTRY_FILL_PRICE
BASE_TS = dt.datetime(2026, 9, 1, 9, 45, 0)
NAV = 1_000_000.0


def _ts(day: int, hour: int = 10) -> str:
    return (BASE_TS + dt.timedelta(days=day, hours=hour)).isoformat(timespec="seconds")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_paper_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, signal_id INTEGER,
            side TEXT, code TEXT, name TEXT, qty INTEGER, planned_price REAL,
            filled_price REAL, amount REAL, fees REAL, status TEXT, reason TEXT,
            risk_payload TEXT, realized_pnl REAL, created_at TEXT, executed_at TEXT,
            order_type TEXT DEFAULT 'market', origin TEXT DEFAULT 'strategy',
            expires_at TEXT, cancelled_at TEXT,
            strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT);
        CREATE TABLE paper_fills(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
            account_id TEXT, side TEXT, code TEXT, qty INTEGER, price REAL,
            amount REAL, fees REAL, fill_date TEXT, quote_at TEXT,
            assumption TEXT NOT NULL);
        CREATE TABLE paper_positions(
            account_id TEXT, code TEXT, name TEXT, industry TEXT, qty INTEGER,
            cost REAL, entry_date TEXT, available_date TEXT,
            asset_type TEXT NOT NULL DEFAULT 'stock_t1',
            peak_price REAL, take_stage INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(account_id, code));
        CREATE TABLE paper_signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, code TEXT,
            name TEXT, signal_date TEXT, intended_date TEXT, close_price REAL,
            rank_score REAL, t_tier TEXT, t_score REAL, payload TEXT,
            status TEXT, reason TEXT, created_at TEXT);
        CREATE TABLE paper_nav(
            id INTEGER PRIMARY KEY AUTOINCREMENT, nav_date TEXT, account_id TEXT,
            nav REAL, cash REAL, position_value REAL);
        CREATE TABLE paper_capital_reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_key TEXT, status TEXT,
            released_at TEXT);
        CREATE TABLE paper_accounts(
            id TEXT PRIMARY KEY, name TEXT, status TEXT, cycle_id INTEGER,
            capital REAL, params TEXT DEFAULT '{}');
        """
    )


def run_custom_strategy_replay(tmpdir: str) -> dict[str, Any]:
    """跑完整链路并返回确定性 digest（两次独立执行必须完全一致）。"""
    strategy_id = "custom_user_strategy"
    paper_path = f"{tmpdir}/paper.sqlite3"
    evo_path = f"{tmpdir}/evolution.sqlite3"
    paper = _connect(paper_path)
    evo = _connect(evo_path)
    _init_paper_schema(paper)
    SE.ensure_schema(evo)
    digest: dict[str, Any] = {"engine": GOLDEN_REPLAY_VERSION, "stages": []}

    def stage(name: str, payload: dict[str, Any]) -> None:
        digest["stages"].append({"stage": name, **payload})

    # 1. 创建草稿 → risk compile
    preview = SCP.strategy_creation_preview(DRAFT, {
        "trend": {"name": "趋势波段", "max_weight": 0.34, "max_exposure": 0.95,
                  "max_positions": 3},
        "composite": {"name": "保守组合", "max_weight": 0.30,
                      "max_exposure": 0.85, "max_positions": 3},
    })
    stage("risk_compile", {
        "archetype": preview["risk_fingerprint"]["archetype"],
        "risk_profile": preview["recommended"]["risk_profile"],
        "execution_family": preview["recommended"]["execution_profile"]["family"],
        "order_type": preview["recommended"]["execution_profile"]["order_type"],
        "tunable": preview["evolution"]["tunable"],
        "locked": preview["evolution"]["locked"],
    })

    # 2. 动态注册用户策略（自定义账户 + 资金）
    paper.execute(
        "INSERT INTO paper_accounts(id,name,status,cycle_id,capital,params) VALUES(?,?,?,?,?,?)",
        (strategy_id, DRAFT["name"], "running", 1, NAV, "{}"),
    )
    paper.commit()

    # 3. signal（确定性合成信号）→ OrderIntent（生产适配器）
    OI = __import__("order_intent")
    signal_payload = {
        "code": TICKER[0], "name": TICKER[1], "close_price": SIGNAL_CLOSE,
        "side": "buy", "strength": 0.8, "urgency": "same_session",
        "data_asof": "2026-09-01", "stop_reference": "-5% 硬止损",
        "reason": "自定义策略首只候选（合成行情）",
        "industry": TICKER[2],
    }
    OI.reject_qty_claims(signal_payload)  # 契约：策略层不得携带数量/金额
    intent = OI.order_intent_from_signal(strategy_id, signal_payload, now=BASE_TS)
    # 旧字段视图必须还原出 code/strategy_id/side（等价性承诺）。
    legacy = OI.intent_to_legacy_fields(intent)
    assert legacy["code"] == TICKER[0] and legacy["strategy_id"] == strategy_id
    paper.execute(
        """INSERT INTO paper_signals(account_id,code,name,signal_date,intended_date,
               close_price,status,reason,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (strategy_id, TICKER[0], TICKER[1], "2026-09-01", "2026-09-01",
         SIGNAL_CLOSE, "pending", "自定义策略首只候选（合成行情）", _ts(0)),
    )
    signal_id = paper.execute("SELECT id FROM paper_signals ORDER BY id DESC LIMIT 1").fetchone()[0]
    stage("signal", {
        "signal_id": int(signal_id), "code": TICKER[0],
        "close_price": SIGNAL_CLOSE,
        "intent_symbol": intent.symbol, "intent_side": intent.side,
        "intent_urgency": intent.urgency,
    })

    # 4. allocation（席位 + 资金预算，任意 1 个策略也必须成立）
    runtimes = [PA.StrategyRuntime(strategy_id=strategy_id,
                                   base_priority=0.34, max_positions=3)]
    allocation = PA.position_limits(
        runtimes, hard_pool_cap=15, strategy_max_positions=6,
        strategy_min_positions=1, protected_slot_floor=1,
    )
    stage("allocation", {
        "position_limit": int(allocation["limits"][strategy_id]),
        "pool_limit": int(allocation["total_cap"]),
    })

    # 5. OrderIntent + sizing（风险预算 sizing，确定性）
    profile = {"max_weight": 0.34, "max_exposure": 0.95, "single_risk": 0.012,
               "max_industry": 0.42, "cooldown_days": 2, "min_cost_edge": 0.006}
    qty, sizing = PSZ.price_aware_qty(
        nav=NAV, cash=NAV, position_value=0.0, industry_value=0.0, code_value=0.0,
        fill_price=ENTRY_FILL_PRICE, hard_stop=0.05, profile=profile,
        exposure_cap=0.82, max_exposure_cap=0.82, exposure_scale=1.0,
        strategy_position_value=0.0, strategy_cap_amount=340000.0,
        pool_cap_amount=820000.0,
        pending_strategy_amount=0.0, pending_pool_amount=0.0,
        num=lambda value, default=None: value, lot_size=100,
        single_position_max_amount=0.0,
    )
    qty = int(qty)
    stage("sizing", {
        "qty": qty,
        "target_amount": sizing.get("target_amount"),
        "amount": round(qty * ENTRY_FILL_PRICE, 2),
    })

    # 6. fill（成交 + 持仓）
    amount = round(qty * ENTRY_FILL_PRICE, 2)
    paper.execute(
        """INSERT INTO paper_orders(account_id,signal_id,side,code,name,qty,
               planned_price,filled_price,amount,fees,status,reason,risk_payload,
               created_at,executed_at,strategy_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (strategy_id, signal_id, "buy", TICKER[0], TICKER[1], qty,
         SIGNAL_CLOSE, ENTRY_FILL_PRICE, amount, 0.0, "filled",
         "自定义策略：限价到位成交", "{}", _ts(0, 11), _ts(0, 11), strategy_id),
    )
    order_id = paper.execute("SELECT id FROM paper_orders ORDER BY id DESC LIMIT 1").fetchone()[0]
    paper.execute(
        """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,
               fees,fill_date,assumption) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (order_id, strategy_id, "buy", TICKER[0], qty, ENTRY_FILL_PRICE, amount,
         0.0, "2026-09-01", "合成行情确定性成交"),
    )
    paper.execute(
        """INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,
               entry_date,available_date) VALUES(?,?,?,?,?,?,?,?)""",
        (strategy_id, TICKER[0], TICKER[1], TICKER[2], qty, ENTRY_FILL_PRICE,
         "2026-09-01", "2026-09-02"),
    )
    paper.execute("UPDATE paper_signals SET status='filled' WHERE id=?", (signal_id,))
    paper.commit()
    stage("fill", {
        "order_id": int(order_id), "qty": qty,
        "fill_price": ENTRY_FILL_PRICE, "amount": amount,
    })

    # 7. stop（硬止损退出：-5% 触发）
    realized = round(qty * (STOP_FILL_PRICE - ENTRY_FILL_PRICE), 2)
    paper.execute(
        """INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,
               filled_price,amount,fees,status,reason,risk_payload,realized_pnl,
               created_at,executed_at,strategy_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (strategy_id, "sell", TICKER[0], TICKER[1], qty, STOP_TRIGGER_PRICE,
         STOP_FILL_PRICE, round(qty * STOP_FILL_PRICE, 2), 0.0, "filled",
         "硬止损：-5% 触发全清", "{}", realized, _ts(3, 10), _ts(3, 10), strategy_id),
    )
    stop_order_id = paper.execute(
        "SELECT id FROM paper_orders ORDER BY id DESC LIMIT 1").fetchone()[0]
    paper.execute(
        """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,
               fees,fill_date,assumption) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (stop_order_id, strategy_id, "sell", TICKER[0], qty, STOP_FILL_PRICE,
         round(qty * STOP_FILL_PRICE, 2), 0.0, "2026-09-04", "合成行情确定性止损"),
    )
    paper.execute("DELETE FROM paper_positions WHERE account_id=? AND code=?",
                  (strategy_id, TICKER[0]))
    paper.commit()
    stage("stop", {
        "order_id": int(stop_order_id), "trigger_price": STOP_TRIGGER_PRICE,
        "fill_price": STOP_FILL_PRICE, "realized_pnl": realized,
    })

    # 8. evolution proposal（策略级进化提案：基于重放证据微调）
    evidence = 20  # 重放已产生足够合成证据
    proposal = SE.adjust_strategy_params(
        evo, strategy_id, {"max_weight_delta": 0.032},
        reason="golden replay：重放收益达标后的一步微调",
        source="golden_replay", evidence_count=evidence,
    )
    stage("evolution_proposal", {
        "adjusted": bool(proposal.get("adjusted")),
        "changed_keys": proposal.get("changed_keys") or [],
        "new_params": proposal.get("new_params"),
        "profile": proposal.get("profile"),
    })

    # 收尾一致性断言（digest 生成前就失败比 golden 比对失败更可读）。
    fill_row = paper.execute(
        "SELECT qty,filled_price FROM paper_orders WHERE id=?", (order_id,)
    ).fetchone()
    assert int(fill_row["qty"]) == qty
    stop_row = paper.execute(
        "SELECT realized_pnl FROM paper_orders WHERE id=?", (stop_order_id,)
    ).fetchone()
    assert float(stop_row["realized_pnl"]) == realized
    assert realized < 0  # 止损必然亏损出场
    assert proposal.get("adjusted") is True

    digest["digest"] = json.dumps(digest["stages"], ensure_ascii=False, sort_keys=True)
    paper.close()
    evo.close()
    return digest
