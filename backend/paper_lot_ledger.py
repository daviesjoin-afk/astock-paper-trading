"""Lot quantity/cost mutations and compatibility projection rebuilding.

Uses the caller's connection and transaction. Never creates a cycle, opens a
connection, commits, reads quotes or imports the trading engine.
"""
from __future__ import annotations
import math
import paper_order_execution_authority as POEA
import paper_position_read_model as PPRM
import paper_position_risk_state as PPRS
import paper_repository as PRP
import paper_trading_rules as PTR
from paper_common import now_text as _now, as_date as _date

OrderCycleProvenanceUnknown = POEA.OrderCycleProvenanceUnknown
ORDER_CYCLE_ORDER_MISSING = POEA.ORDER_CYCLE_ORDER_MISSING


def _num(value, default=0.0):
    """严格数值契约：只接受 int/float，NaN 视为缺失。

    与 ``paper_numeric.num`` 同口径，但多一层 ``OverflowError`` 保护：整数大到
    超出 float 范围时 ``float(value)`` 会抛异常，而这里必须回落成默认值——
    在记账路径上"抛异常"比"读不出价格"严重得多。
    """
    if not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return default if math.isnan(number) else number


def position_rows(conn, account_id=None, asof_day=None, readonly=False):
    """Legacy call shape over the canonical, always read-only position reader."""
    return PPRM.current_positions(conn, account_id=account_id, asof_day=asof_day)


def consume_available_lots(conn, account_id, code, qty, asof_day, cycle_id=None, *, rows_fn=PRP.rows, num_fn=_num, date_fn=_date):
    """FIFO 消耗已结算股票底仓，返回真实成本；T+1 锁定份额永不被扣减。

    ``cycle_id`` 是**调用链里已经确定**的周期身份（v18 write-time fact），且现在是
    **必填语义**：成交路径必须把来源订单的 durable ``cycle_id`` 传进来（见
    ``execution_planner.commit_fill``）。传 ``None`` 一律 fail closed，**不再**回退到
    当前 active cycle —— 那正是本轮要修的 split-brain：cycle 8 创建的 pending SELL 在
    cycle 9 激活后成交，会去消费 cycle 9 的 lot，而成交单仍挂在 cycle 8 订单上。

    不做任何「库里没有周期就当匿名调用」的例外：那会重新打开同一条漏口，而且
    ``_order_cycle_id`` 在无周期库里会**顺手创建一个新周期**（``_ensure_cycle``），
    等于用一次写操作把「不知道」变成「知道」。
    """
    _rows, _num, _date = rows_fn, num_fn, date_fn
    if cycle_id is None:
        raise OrderCycleProvenanceUnknown(
            None, ORDER_CYCLE_ORDER_MISSING,
            "lot 消耗必须由来源订单显式提供周期；拒绝回退到当前 active cycle",
        )
    cycle_id = int(cycle_id)
    remaining = int(qty)
    cost_amount = 0.0
    lots = _rows(
        conn,
        """SELECT * FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=?
           AND remaining_qty>0 AND available_date<=? ORDER BY acquired_at,id""",
        (cycle_id, account_id, code, _date(asof_day).isoformat()),
    )
    # Validate the aggregate available quantity before mutating any lot.  A
    # malformed/partially-consumed lot must not leave a half-reduced ledger
    # when the requested sell quantity cannot be fully satisfied.
    available_total = sum(max(0, int(_num(lot.get("remaining_qty")))) for lot in lots)
    if available_total < remaining:
        return 0, 0.0
    for lot in lots:
        take = min(remaining, int(lot["remaining_qty"]))
        if take <= 0:
            continue
        conn.execute("UPDATE paper_position_lots SET remaining_qty=remaining_qty-? WHERE id=?", (take, lot["id"]))
        cost_amount += take * _num(lot["cost"])
        remaining -= take
        if not remaining:
            break
    return int(qty) - remaining, cost_amount


def record_lot(conn, account, signal, qty, fill_price, asof_day, order_id=None, is_t_base=True, fees=0.0, cycle_id=None, acquired_at=None, *, num_fn=_num, date_fn=_date, now_fn=_now, asset_type_fn=PTR.asset_type, next_trade_day_fn=PTR.next_weekday, provenance_fn=POEA.order_cycle_provenance_for_order):
    """写入买入 lot。周期归属必须与**来源买单**一致（§15 硬 invariant）。

    归属**只**接受两种来源，且都不允许回退到当前 active cycle：

    * 来源订单的 durable ``cycle_id`` 可证明（``proven``）⇒ lot 继承它；
    * **没有**来源订单（``order_id is None``）⇒ 这是订单创建前的直接建仓路径，
      才允许用显式传入 / 本事务解析的周期。此时不存在「订单与 lot 不一致」的问题。

    来源订单**存在但归属不可证明**（legacy NULL / pre-v18 schema / 行缺失）时必须
    fail closed：抛 :class:`OrderCycleProvenanceUnknown`，**不写任何 lot**。旧行为是
    ``order_cycle is None`` 就回退到 active cycle，那会让 legacy 订单的未知归属在
    lot 层被洗成一个「已知周期」的派生事实 —— lot 写着 cycle 8，而它引用的订单
    永远是 NULL，帐实两歧且无法回溯。规矩是：``"不知道"`` 不能变成 ``"可卖"``。
    """
    _num, _date, _now = num_fn, date_fn, now_fn
    _asset_type, _next_weekday = asset_type_fn, next_trade_day_fn
    _order_cycle_provenance_for_order = provenance_fn
    if order_id is not None:
        prov = _order_cycle_provenance_for_order(conn, order_id)
        if not prov.is_proven:
            raise OrderCycleProvenanceUnknown(
                order_id, prov.status,
                "来源买单的周期归属不可证明；拒绝创建带确定周期的新 lot",
            )
        cycle_id = prov.cycle_id
    else:
        if cycle_id is None:
            raise OrderCycleProvenanceUnknown(None, ORDER_CYCLE_ORDER_MISSING, "direct lot creation requires an explicit cycle")
        cycle_id = int(cycle_id)
    asset_type = _asset_type(signal.get("code"), signal.get("name"))
    available = _date(asof_day) if asset_type == "etf_t0" else _next_weekday(asof_day)
    # R14：episode 边界。本笔成交前同 cycle 权威 remaining_qty 之和为 0 ⇒ 全新
    # episode（peak=本笔成交价 / stage=0）；>0 ⇒ 同 episode 加仓，stage 保持、
    # peak 只升不降。判定统一走 risk-state 模块的权威聚合，不在这里另写一份 SQL。
    prior_qty = PPRS.remaining_qty(
        conn, cycle_id=cycle_id, account_id=account["id"], code=signal["code"])
    cursor = conn.execute(
        """INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,cost_fee_included,is_t_base,source_fill_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cycle_id, account["id"], signal["code"], signal.get("name"), signal.get("industry"), int(qty), int(qty),
         fill_price + _num(fees) / max(int(qty), 1), acquired_at or _now(), available.isoformat(), asset_type, order_id, 1, int(bool(is_t_base)), None),
    )
    lot_id = int(cursor.lastrowid)
    # 生命周期初始化/吸收由 paper_position_risk_state 持有；只传已证明的 cycle。
    if prior_qty <= 0:
        PPRS.initialize_episode(
            conn, cycle_id=cycle_id, account_id=account["id"], code=signal["code"],
            peak_price=fill_price, opened_order_id=order_id)
    else:
        PPRS.update_peak(
            conn, cycle_id=cycle_id, account_id=account["id"], code=signal["code"],
            peak_price=fill_price)
    return lot_id


def sync_positions(conn, account_id=None, asof_day=None, *, positions_fn=PPRM.current_positions):
    """保留聚合表供旧接口兼容；交易结算逻辑只读取 lots。

    R14 数据方向（唯一合法方向）：权威 lots + 权威 risk state → 本投影。投影可以
    **接收**权威，永远不能**创造**权威；这里写的 peak/take_stage 只是展示镜像。
    """
    positions = positions_fn(conn, account_id=account_id, asof_day=asof_day)
    if account_id:
        conn.execute("DELETE FROM paper_positions WHERE account_id=?", (account_id,))
    else:
        conn.execute("DELETE FROM paper_positions")
    for p in positions:
        conn.execute(
            """INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,available_date,asset_type,peak_price,take_stage)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (p["account_id"], p["code"], p.get("name"), p.get("industry"), p["qty"], p["cost"],
             p["entry_date"], p["available_date"], p["asset_type"], p["peak_price"],
             int(p.get("take_stage") or 0)),
        )
    return positions
