"""Order cycle provenance and execution authorization; SELECT/PRAGMA only.

No active-cycle creation, account rebinding, transaction ownership or engine imports.
"""
from __future__ import annotations
import sqlite3

ORDER_CYCLE_PROVEN = "proven"
ORDER_CYCLE_LEGACY_UNKNOWN = "legacy_unknown"
ORDER_CYCLE_ORDER_MISSING = "order_missing"
ORDER_CYCLE_PRE_V18_SCHEMA = "pre_v18_schema"

def active_cycle_id_readonly(conn):
    """**只读**读取当前 active cycle id；不存在则返回 ``None``，绝不创建周期。

    与 :func:`_active_cycle` 的关键区别：后者在没有任何周期时会调用
    ``_ensure_cycle`` **INSERT 一个新周期**。验证「一张已存在的历史订单是否还
    有权成交」是一个纯查询问题 —— 如果因为库是空的就顺手建一个周期，那等于用
    一次写操作回答了「现在属于哪个周期」，而这个答案本来是「不存在」。

    ``paper_cycles`` 表不存在（极简 schema / 部分测试库）同样返回 ``None``：
    读取不到周期事实就是「无法证明」，而不是让底层 ``OperationalError`` 冒出去。
    """
    try:
        row = conn.execute(
            "SELECT id FROM paper_cycles WHERE status IN ('draft','running','paused')"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row["id"] if hasattr(row, "keys") else row[0])
    except (TypeError, ValueError):
        return None



def account_cycle_id_readonly(conn, account_id):
    """**只读**读取账户当前绑定的经济周期；账户缺失或为 NULL 时返回 ``None``。"""
    if account_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - 表不存在
        return None
    if row is None:
        return None
    value = row["cycle_id"] if hasattr(row, "keys") else row[0]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None



class OrderCycleProvenanceUnknown(RuntimeError):
    """成交阶段遇到**不可证明**的订单周期归属 ⇒ fail closed。

    规格 §4/§8 明确要求：legacy NULL-cycle 订单与 post-v18 缺 cycle 的订单都不得
    靠「当前 active cycle」顶上。异常文本里带 ``legacy_order_cycle_unproven`` /
    ``order_cycle_provenance_unknown`` 标记，便于上层（手动待成交扫描、策略重试、
    延迟成交）把它转换成明确的 not-filled / retry-blocked / supersede-required，
    而不是留下半成交状态。

    继承 ``RuntimeError`` 是有意的：现有调用方已经用 ``except Exception`` 隔离单个
    订单（``process_pending_manual_orders`` 回滚 savepoint 并标记可重试），因此
    fail closed 会自然落到「本轮不成交、下轮重试」，不会静默成交。
    """

    def __init__(self, order_id, status, message=""):
        self.order_id = order_id
        self.status = status
        marker = (
            "legacy_order_cycle_unproven"
            if status == ORDER_CYCLE_LEGACY_UNKNOWN
            else "order_cycle_provenance_unknown"
        )
        self.marker = marker
        super().__init__(f"{marker}: order_id={order_id} status={status} {message}".strip())


class OrderExecutionCycleChanged(RuntimeError):
    """订单的周期归属**可证明**，但经济账本已经搬到别的周期 ⇒ fail closed。

    这是「归属未知」之外的另一类拒绝：``order.cycle_id`` 是可信的（例如 8），
    但 ``paper_accounts`` 已被重绑到 cycle 9、active cycle 也是 9。此时该订单
    若继续成交，会形成 cross-cycle 账本错配：

    * SELL：卖掉 cycle 8 的持仓，把钱打进 cycle 9 的现金；
    * BUY：用 cycle 9 的现金买出一个 cycle 8 的 lot。

    ``paper_accounts`` 是**当前**经济账户，新周期创建时会被重绑并重置，系统里
    并不存在可独立写入的历史周期现金账本。因此正确的 contract 不是「想办法把
    cash 写回旧周期」，而是：**持久化的周期归属不等于成交授权** —— 账本一旦
    搬家，旧订单即 stale，必须 fail closed。

    标记 ``order_execution_cycle_changed`` 携带四个 id，便于上层做终态化与诊断。
    """

    def __init__(self, order_id, order_cycle_id, account_cycle_id, active_cycle_id,
                 message=""):
        self.order_id = order_id
        self.order_cycle_id = order_cycle_id
        self.account_cycle_id = account_cycle_id
        self.active_cycle_id = active_cycle_id
        self.marker = "order_execution_cycle_changed"
        super().__init__(
            f"order_execution_cycle_changed: order_id={order_id} "
            f"order_cycle_id={order_cycle_id} account_cycle_id={account_cycle_id} "
            f"active_cycle_id={active_cycle_id} {message}".strip()
        )


class OrderCycleProvenance:
    """订单的 durable 周期归属**四态**结论（不变量：``proven`` ⟺ ``cycle_id`` 非 None）。

    调用方必须按 ``status`` 分支，而不是按 ``cycle_id is None`` 分支 —— 后者把
    「未知」与「schema 太旧」混为一谈，正是本轮要消灭的漏洞。
    """

    __slots__ = ("order_exists", "schema_has_cycle_column", "cycle_id", "status")

    def __init__(self, order_exists, schema_has_cycle_column, cycle_id, status):
        self.order_exists = bool(order_exists)
        self.schema_has_cycle_column = bool(schema_has_cycle_column)
        self.cycle_id = cycle_id
        self.status = status

    @property
    def is_proven(self):
        """只有这一种状态允许把周期写进 lot / 用于 lot 消耗。"""
        return self.status == ORDER_CYCLE_PROVEN

    def as_dict(self):
        return {
            "order_exists": self.order_exists,
            "schema_has_cycle_column": self.schema_has_cycle_column,
            "cycle_id": self.cycle_id,
            "status": self.status,
        }

    def __repr__(self):  # pragma: no cover - 诊断用
        return (
            f"OrderCycleProvenance(status={self.status!r}, "
            f"cycle_id={self.cycle_id!r}, order_exists={self.order_exists}, "
            f"schema_has_cycle_column={self.schema_has_cycle_column})"
        )


def order_cycle_provenance_for_order(conn, order_id) -> OrderCycleProvenance:
    """**已存在订单**的 durable 周期归属：四态，绝不回退到当前 active cycle。

    这是成交阶段的唯一权威读法。``order_id`` 缺失、行缺失、schema 无列、列为
    NULL —— 四种情形必须可区分，且**没有一种**允许调用方去问「现在是哪个周期」。

    先看 schema 再看行：schema 无 ``cycle_id`` 列时，``SELECT cycle_id`` 必然报错，
    而「报错」与「行不存在」在同一个 ``except`` 里无法区分 —— 那样 pre-v18 库上的
    每一行都会被报成 ``order_missing``，于是「schema 太旧」这个**环境事实**被
    伪装成「订单不存在」这个**调用错误**，运维会去查订单号而不会去跑迁移。
    """
    has_column = orders_have_cycle_column(conn)
    if order_id is None:
        return OrderCycleProvenance(False, has_column, None, ORDER_CYCLE_ORDER_MISSING)
    if not has_column:
        # 列不存在 ⇒ 该订单创建于 v18 之前，回归属**不可能**被证明；但仍然要把
        # 「行到底在不在」查清楚，否则两种完全不同的故障会被混成一个结论。
        exists = False
        try:
            exists = conn.execute(
                "SELECT 1 FROM paper_orders WHERE id=?", (int(order_id),)
            ).fetchone() is not None
        except (sqlite3.Error, TypeError, ValueError):
            exists = False
        status = ORDER_CYCLE_PRE_V18_SCHEMA if exists else ORDER_CYCLE_ORDER_MISSING
        return OrderCycleProvenance(exists, False, None, status)
    try:
        row = conn.execute(
            "SELECT cycle_id FROM paper_orders WHERE id=?", (int(order_id),)
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):  # pragma: no cover - 表不存在
        return OrderCycleProvenance(False, True, None, ORDER_CYCLE_ORDER_MISSING)
    if row is None:
        return OrderCycleProvenance(False, True, None, ORDER_CYCLE_ORDER_MISSING)
    value = row["cycle_id"] if hasattr(row, "keys") else row[0]
    try:
        cycle = int(value)
    except (TypeError, ValueError):
        cycle = None
    if cycle is None:
        return OrderCycleProvenance(True, True, None, ORDER_CYCLE_LEGACY_UNKNOWN)
    return OrderCycleProvenance(True, True, cycle, ORDER_CYCLE_PROVEN)



def order_cycle_id_for_order(conn, order_id):
    """**只读**入口：仅当归属可证明时返回 ``int``，否则 ``None``。

    保留旧签名供 ``_record_lot`` 使用；成交路径必须用
    :func:`order_cycle_provenance_for_order` 拿到四态并据此 fail closed。
    """
    provenance = order_cycle_provenance_for_order(conn, order_id)
    return provenance.cycle_id if provenance.is_proven else None



def assert_order_execution_cycle(conn, order_id, *, account_id=None, provenance=None, allow_out_of_cycle_account=False):
    """**已存在订单**的成交授权闸门：返回可证明的 ``order_cycle_id``，否则抛异常。

    这是 execution-cycle invariant 的**唯一**实现（§8：不要把判断抄成三四份）。
    任何让一张已存在的 ``paper_order`` 进入成交语义的路径都必须先过这里。

    三项必须同时成立（§5）：

    1. ``order.cycle_id`` 可证明（否则 :class:`OrderCycleProvenanceUnknown`）；
    2. ``paper_accounts.cycle_id == order.cycle_id``；
    3. active execution cycle id == ``order.cycle_id``。

    为什么 account 一致还不够：``paper_accounts`` 会在新周期创建时被**重绑并重置**
    （``_create_cycle`` 对未启用策略还会把 ``cycle_id`` 置 NULL），所以「账户 id 相同」
    绝不代表「仍是同一经济周期」。订单归属可信 + 账本已搬家，就是
    :class:`OrderExecutionCycleChanged`。

    ``provenance`` 允许调用方把已经读到的归属结论传进来（§12 要求先读归属、再校验
    身份、最后校验周期一致性）。传入时不再重复查询；结论本身仍只由
    :func:`order_cycle_provenance_for_order` 产生，所以「同一个事实的两套读法」不会
    出现。

    **纯只读**（§9）：只做 SELECT / PRAGMA。刻意不使用 :func:`_active_cycle` —— 后者
    在没有周期时会 ``_ensure_cycle`` **INSERT 一个周期**，那等于用一次写操作回答
    「现在属于哪个周期」，把「不存在」变成一个凭空造出的答案。这里拿不到就是
    fail closed。
    """
    if provenance is None:
        provenance = order_cycle_provenance_for_order(conn, order_id)
    if not provenance.is_proven:
        raise OrderCycleProvenanceUnknown(
            order_id, provenance.status,
            "订单周期归属不可证明；拒绝进入成交语义",
        )
    order_cycle_id = provenance.cycle_id

    account_cycle_id = account_cycle_id_readonly(conn, account_id)
    active_cycle_id = active_cycle_id_readonly(conn)
    # 暂停/归档/退出当前周期的账户仍可清掉旧 lot；仅 SELL 且订单周期 == active cycle 时放行。
    archived_out_of_cycle = account_cycle_id is None and active_cycle_id == order_cycle_id and (conn.execute("SELECT status FROM paper_accounts WHERE id=?", (account_id,)).fetchone() or [None])[0] in ("paused", "archived")
    if (account_cycle_id != order_cycle_id and not (allow_out_of_cycle_account and archived_out_of_cycle)) or active_cycle_id != order_cycle_id:
        raise OrderExecutionCycleChanged(
            order_id, order_cycle_id, account_cycle_id, active_cycle_id,
            "订单周期与经济账本/active 周期不一致；旧周期订单已 stale",
        )
    return order_cycle_id



def orders_have_cycle_column(conn):
    """``paper_orders`` 是否有 ``cycle_id``（v18 之后恒为真；旧库可能尚未迁移）。

    用 ``PRAGMA`` 发现而不是写死：写死会在 pre-v18 库上报 ``no such column``，
    把整条交易链路打成异常。

    **刻意不缓存。** 这里曾经把一个裸布尔值缓存进模块级全局，于是同一进程里任何
    一个 pre-v18 形状的连接（例如手写 schema 的测试库、或诊断用的旧快照）先被
    查询，就会把整个进程的答案永久钉成 ``False`` —— 真实 v18 库随后被误判为
    pre-v18。而「schema 太旧」与「legacy NULL」恰恰是必须分开的两种情形，所以
    这个「优化」会直接制造错误结论。``PRAGMA table_info`` 只读 schema、不碰数据
    页，调用点都在成交/写单路径而非热循环里，每次询问一次是正确性的合理代价。

    拿不到列定义（表不存在）⇒ ``False``，并且**不**把它记成结论。
    """
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_orders)")}
    except sqlite3.Error:  # pragma: no cover - 表不存在
        return False
    return "cycle_id" in columns

# ---------------------------------------------------------------------------
# 兼容别名：模块内部已全部改用公开名；下面这些只为旧调用方/旧测试保留。
_order_cycle_provenance_for_order = order_cycle_provenance_for_order
_account_cycle_id_readonly = account_cycle_id_readonly
_active_cycle_id_readonly = active_cycle_id_readonly
_assert_order_execution_cycle = assert_order_execution_cycle
_order_cycle_id_for_order = order_cycle_id_for_order
_orders_have_cycle_column = orders_have_cycle_column
_order_cycle_id_for_order = order_cycle_id_for_order
