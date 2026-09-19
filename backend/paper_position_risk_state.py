# -*- coding: utf-8 -*-
"""``paper_position_risk_state`` —— 持仓运行时风险状态的**唯一** authority 模块。

本模块是 ``paper_position_risk_state`` 表的 runtime 状态所有权边界。它只做一件事：
把「一个 position episode 的运行时状态」按**调用方已证明的 cycle provenance**
读写成确定的状态迁移。

不变量（本模块存在的全部理由）::

    paper_position_lots
        数量 / 归属 / 成本的唯一权威。

    paper_position_risk_state
        cycle-owned 运行时权威：episode 的 peak_price / take_stage /
        episode 起点 / 来源买单。每一行恰好属于一个不可变 cycle。

    paper_positions
        只有兼容展示价值，零执行权威。

边界（刻意窄，且必须保持窄）::

    * 零项目级 import —— 不 import ``paper_trading``（也不 import 任何其它
      backend 业务模块）。只依赖 stdlib 与「调用方交进来的 sqlite connection」。
    * 不拥有 transaction —— 绝不 ``commit`` / ``rollback`` / ``BEGIN``；也不写
      ``SAVEPOINT``。lot 消耗、状态收尾、订单/成交写入、执行盖章必须同处调用方
      的既有事务，风险状态的落库不能从成交事务里被切出去单独提交。
    * 不解析 active cycle —— ``cycle_id`` 一律由调用方显式传入（write-time
      provenance）。禁止 ``MAX(cycle_id)`` / ``paper_accounts.cycle_id`` /
      日期推断 / 「当前周期」查询。
    * 不拥有 schema —— DDL 仍在 ``paper_schema_migrations``，migration 注册仍在
      ``db_migrate``。本模块只做 runtime 状态操作，绝不产生第二套 schema 真相。
    * 不决定成交 —— 本模块只在 authoritative lot 消耗**已经成功之后**收尾；
      状态行的存在与否绝不反过来决定 SELL 是否成立。

依赖方向（单向，不可反转）::

    paper_trading      ──▶ paper_position_risk_state
    execution_planner  ──▶ paper_position_risk_state

    paper_position_risk_state ──▶ (stdlib only)
"""
from __future__ import annotations

import datetime as dt

__all__ = [
    "STATE_DELETED",
    "STATE_PRESERVED",
    "STATE_STAGE_UPDATED",
    "delete_episode",
    "finalize_sell",
    "initialize_episode",
    "remaining_qty",
    "update_peak",
    "update_take_stage",
]

#: 生产表名（module 是它的唯一 runtime 所有者）。
TABLE = "paper_position_risk_state"

#: ``finalize_sell`` 的 ``state_action`` 三态 —— 调用方与测试都读这个结论，
#: 不去猜「到底动了没有」。
STATE_DELETED = "deleted"
STATE_PRESERVED = "preserved"
STATE_STAGE_UPDATED = "stage_updated"

#: 与 ``paper_trading._now`` 完全一致的落库时间格式（全表时间列的既有约定）。
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return dt.datetime.now().strftime(_TIME_FORMAT)


def _require_cycle(cycle_id):
    """cycle provenance 必须显式可证明 —— 绝不在这里"找"一个周期出来。"""
    if cycle_id is None:
        raise ValueError(
            "paper_position_risk_state 的写入必须显式携带 cycle_id"
            "（write-time provenance）；本模块绝不解析 active cycle"
        )
    return int(cycle_id)


def initialize_episode(conn, *, cycle_id, account_id, code, peak_price,
                       opened_order_id=None, now=None):
    """verified BUY 的 ``0 -> >0``：开一个全新 position episode。

    ``peak_price`` = 本笔 verified 成交价、``take_stage`` = 0、
    ``opened_order_id`` = 来源 verified 买单（直接建仓路径无订单时诚实留 NULL，
    绝不从 active cycle / 日期 / ``MAX(order_id)`` 猜）。

    ``INSERT OR REPLACE``：同一 ``(cycle_id, account_id, code)`` 上任何残留行
    （上一 episode 未被显式清理时的防御性兜底）都不得继承给新 episode ——
    full exit 后的 same-cycle re-entry 必须拿到全新状态。注意 REPLACE 只保证
    「不会继承旧值」，**不能**替代 full exit 的显式清理：残留行本身就是缺陷
    （见 ``finalize_sell``），这里只是第二道防线。

    只写库，不提交：必须与成交结算处于调用方的同一事务。
    """
    cycle_id = _require_cycle(cycle_id)
    stamp = now or _now()
    conn.execute(
        """INSERT OR REPLACE INTO paper_position_risk_state(
               cycle_id,account_id,code,peak_price,take_stage,opened_order_id,
               initialized_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (cycle_id, account_id, code, float(peak_price), 0, opened_order_id,
         stamp, stamp),
    )


def update_peak(conn, *, cycle_id, account_id, code, peak_price, now=None):
    """峰值吸收：cycle-scoped、只升不降。

    调用方必须显式传 ``cycle_id``（write-time provenance）。行不存在 ⇒ no-op：
    初始化只发生在 verified BUY 的 episode 生命周期里，扫描 / 读取路径绝不创造
    权威状态；缺行持仓的 peak 维持读模型的成本锚（与全新 episode 默认一致）。
    """
    cycle_id = _require_cycle(cycle_id)
    conn.execute(
        "UPDATE paper_position_risk_state SET peak_price=MAX(peak_price,?),updated_at=?"
        " WHERE cycle_id=? AND account_id=? AND code=?",
        (float(peak_price), now or _now(), cycle_id, account_id, code),
    )


def update_take_stage(conn, *, cycle_id, account_id, code, take_stage, now=None):
    """阶梯止盈推进：cycle-scoped。

    调用方必须显式传 ``cycle_id`` —— 卖出订单自己已拥有 durable cycle
    provenance，写状态必须用同一个 cycle fact。行不存在（unknown stage）⇒
    no-op：未知档位既不能被推进，也不能被写成一个"有定义的 0"。
    """
    cycle_id = _require_cycle(cycle_id)
    conn.execute(
        "UPDATE paper_position_risk_state SET take_stage=?,updated_at=?"
        " WHERE cycle_id=? AND account_id=? AND code=?",
        (int(take_stage), now or _now(), cycle_id, account_id, code),
    )


def delete_episode(conn, *, cycle_id, account_id, code):
    """结束本 position episode：runtime 风险状态一并关闭。

    最小实现是 DELETE（订单 / 成交 / 审计表已经保留历史事实）；same-cycle
    re-entry 由 :func:`initialize_episode` 创建全新状态。行不存在时是 no-op
    （0 行受影响不算错误 —— 「已经没有状态」与「刚刚删掉」在语义上等价）。
    """
    cycle_id = _require_cycle(cycle_id)
    conn.execute(
        "DELETE FROM paper_position_risk_state WHERE cycle_id=? AND account_id=? AND code=?",
        (cycle_id, account_id, code),
    )


def remaining_qty(conn, *, cycle_id, account_id, code) -> int:
    """同 cycle / 同账户 / 同标的的 **authoritative** 剩余数量。

    只认 ``paper_position_lots`` —— 数量权威在那里，``paper_positions`` 只是投影。
    刻意**不**按 ``available_date`` 过滤：T+1 锁定的份额仍然是持仓，episode 是否
    结束取决于总剩余量，与今天能不能卖无关。
    """
    cycle_id = _require_cycle(cycle_id)
    row = conn.execute(
        "SELECT COALESCE(SUM(remaining_qty),0) FROM paper_position_lots"
        " WHERE cycle_id=? AND account_id=? AND code=?",
        (cycle_id, account_id, code),
    ).fetchone()
    return 0 if row is None else int(row[0] or 0)


def finalize_sell(conn, *, cycle_id, account_id, code, next_take_stage=None, now=None):
    """**所有生产 SELL 路径共用**的 episode 收尾原语。

    必须在 authoritative lot 消耗**已经成功之后**调用（本函数只读 lots 做判定，
    绝不参与「这笔卖出成不成立」的决策，也绝不替任何路径回滚成交）。

    判定完全来自 ``paper_position_lots`` 的同 cycle 聚合剩余量 —— 而不是让三个
    调用方各自用自己那份局部变量算一遍「position_closed」。真正的 episode
    终止事实只有一个::

        same-cycle authoritative remaining lots == 0

    状态迁移::

        remaining <= 0                     -> DELETE 状态（episode 结束）
        remaining >  0 且给了 next_take_stage -> UPDATE take_stage
        remaining >  0 且没给 next_take_stage -> 原样保留

    为什么「部分卖且没给 stage」必须**保留**：manual / deferred SELL 并没有
    ``_sell_plan()`` 的档位推进事实，把 take_stage 重置成 0 等于凭空多卖一轮
    止盈档位。没有事实就不写。

    返回（调用方与测试都读这个结论，不去猜动了什么）::

        {"remaining_qty": int, "position_closed": bool, "state_action": str}
    """
    cycle_id = _require_cycle(cycle_id)
    remaining = remaining_qty(conn, cycle_id=cycle_id, account_id=account_id, code=code)
    if remaining <= 0:
        delete_episode(conn, cycle_id=cycle_id, account_id=account_id, code=code)
        action = STATE_DELETED
    elif next_take_stage is not None:
        update_take_stage(
            conn, cycle_id=cycle_id, account_id=account_id, code=code,
            take_stage=next_take_stage, now=now,
        )
        action = STATE_STAGE_UPDATED
    else:
        action = STATE_PRESERVED
    return {
        "remaining_qty": remaining,
        "position_closed": remaining <= 0,
        "state_action": action,
    }
