# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow Validation —— **只读**仓位证据适配器（v2）。

本模块回答的**唯一**问题是：

    在 ``decision_at`` 这个**精确时点**，某一个 ``(cycle, account, code)`` 的真实持仓里，
    哪些**份额**已被证明可以卖出，哪些被 T+1 锁住，哪些根本无法证明？

它**只读**，且**零 authority**：不写任何表、不提交/撤销/修改任何订单、不改变选股 /
风控 / 学习行为。它产出的 :class:`PositionSellabilityContext` 只是**观察素材**，
由 :mod:`tradability_position_shadow` 消费，绝不回流执行链路。

──────────────── v2 修正的三条承重契约（v1 是错的） ────────────────

**1. 历史数量必须重放，绝不能拿今天的余额冒充。**

``paper_position_lots.remaining_qty`` 会被后续 SELL **原地递减**
（``paper_trading._consume_available_lots`` 是唯一的扣减点）。因此当前余额只能
描述"现在"，不能描述 ``decision_at`` 当时的持仓。反例（规格逐字禁止）：::

    D 日买 1000
    D+5 卖 800
    今天 remaining_qty=200

    回看 D => proven held_quantity=200      ← 错，D 日实际持有 1000

本层从 **``qty``（建仓后不可变）** 出发，按**生产自己的 FIFO 语义**重放已发生的
SELL，得到每个 lot 在 ``decision_at`` 的历史余额。重放必须**自洽**：重放到今天的
终态必须逐 lot 等于账本现值，否则说明成交证据不完整 → 该 ``(cycle, account, code)``
整体 ``historical_quantity_unprovable``（fail closed）。

**2. 作用域必须显式：cycle + account。**

生产路径是 cycle-aware 且 account-specific 的：lot 归属**周期**，而 T+1 是
**账户级**约束。因此 ``cycle_id`` 与 ``account_id`` 都是**必填**，只读目标 cycle /
目标账户的行。绝不把多个 cycle 或多个账户的份额汇成一个 sellability context ——
``account_id=None`` 会让同 code 的多个账户被池化（规格逐字禁止）。

**3. PIT 必须消费调用方的精确 ``decision_at``。**

调用方给了 ``decision_at`` 就原样规范化后使用；只有确实没给时才回退到
``session_close_at(decision_session)``（明确契约）。显式给出但**不可解析**的值
→ ``position_invalid``，绝不退化成 ``None``（那会让 ``_visible(..., None)``
变成"无上界"，即 future leak）。

──────────────── 复用，而不是重写 ────────────────

T+1 规则只有一处权威实现：:mod:`selection_tradability`。本层**逐 lot**调用
``ST.exit_tradability(evidence, code=..., exit_session=D, entry_session=<lot 的真实建仓 session>)``
并只读它的 ``reason``：

* ``ST.REASON_T1_NOT_SELLABLE`` → 该 lot 的份额被锁（``t1_blocked``）；
* 停在 T+1 **之前**的门禁 → 改由**同一份权威**的 ``ST.earliest_sellable_session``
  单独回答 T+1 这一维（T+0 ETF 首当其冲：``security_scope`` 会先以证券类型拦下它）；
* 无法向权威提问 → ``unknown``（fail closed，**绝不**当可卖）。

本层没有自己比较 ``decision_session < entry_session + 1``，没有自己算周末 /
节假日，没有硬编码任何 ETF 前缀表 —— 那些字符串一次都没有出现。

──────────────── 真实数据链（审计结论） ────────────────

真实 acquisition session **只**来自 ``paper_fills.fill_date``。订单创建时刻
（``paper_orders.created_at``）是**意图**、不是成交；``paper_position_lots.acquired_at``
是系统**记录**时刻，与成交 session 是两件事。三者 PIT 上都要看，但**只有成交事实**
能证明建仓。

身份完整性：``lot.source_order_id → paper_orders → paper_fills`` 必须逐字段一致
（code / account_id / side=buy / 已验证）。任何冲突 → ``acquisition_unprovable``，
绝不允许借另一个账户或另一只股票的已验证买入来证明当前 lot。

──────────────── fail closed ────────────────

``position_unknown`` / ``position_unprovable`` / ``position_partial`` /
``position_invalid`` 一律**不可比**，不进任何 agreement / disagreement 分母。
"不知道"永远不能变成"可卖"。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path（生产与 ``cd backend`` 测试）
    import execution_verification as EV
    import paper_trading_rules as PTR
    import point_in_time as PIT
    import selection_tradability as ST
except ImportError:  # pragma: no cover - package-style import
    from . import execution_verification as EV
    from . import paper_trading_rules as PTR
    from . import point_in_time as PIT
    from . import selection_tradability as ST


POSITION_EVIDENCE_VERSION = "position-evidence-v2"
POSITION_EVIDENCE_FINGERPRINT_VERSION = "sha256-canonical-position-evidence-v2"

#: ``held_quantity`` 的口径。**只有这一种**是决策时点口径。
QUANTITY_BASIS_HISTORICAL_REPLAY = "historical_replay_fifo_at_decision"
#: 重放不自洽（成交证据不完整）→ 没有任何决策时点数量可被证明。
QUANTITY_BASIS_UNPROVABLE = "historical_quantity_unprovable"

#: 卖出事件的**周期归属**四态。生产 ``paper_orders`` / ``paper_fills`` 都没有
#: ``cycle_id``，所以归属只能靠周期时间窗证明 —— 这是一个**状态**，不是一个默认
#: 为真的布尔值。
CYCLE_ATTRIBUTION_PROVEN = "proven"
CYCLE_ATTRIBUTION_MISMATCH = "mismatch"
CYCLE_ATTRIBUTION_UNPROVABLE = "unprovable"
CYCLE_ATTRIBUTION_AMBIGUOUS = "ambiguous"

CYCLE_ATTRIBUTIONS = (
    CYCLE_ATTRIBUTION_PROVEN,
    CYCLE_ATTRIBUTION_MISMATCH,
    CYCLE_ATTRIBUTION_UNPROVABLE,
    CYCLE_ATTRIBUTION_AMBIGUOUS,
)

#: 归属诊断（逐字对应上面的非 ``proven`` 三态）。
_CYCLE_ATTRIBUTION_DIAGNOSTIC = {
    CYCLE_ATTRIBUTION_MISMATCH: "sell_fill_cycle_mismatch",
    CYCLE_ATTRIBUTION_UNPROVABLE: "sell_fill_cycle_unprovable",
    CYCLE_ATTRIBUTION_AMBIGUOUS: "sell_fill_cycle_ambiguous",
}

#: 生产**自己的**开放 position 定义：``paper_trading._position_rows`` 用
#: ``WHERE cycle_id=? AND remaining_qty>0`` 选出持仓。本层复用同一条件。
_OPEN_LOT_CLAUSE = "remaining_qty > 0"

#: 这些 reason 全部产生于 :func:`selection_tradability.tradability_at` 的第 5 步
#: （T+1）**之前**，因此 verdict 停在那里时 T+1 根本未被判定 —— 不能当"可卖"。
PRE_T1_REASONS = frozenset({
    ST.REASON_MISSING_CODE,
    ST.REASON_INVALID_SIDE,
    ST.REASON_INVALID_ACTION_TIME,
    ST.REASON_UNSUPPORTED_SECURITY_TYPE,
    ST.REASON_EVIDENCE_NOT_VISIBLE,
    ST.REASON_UNKNOWN_ST_STATUS,
    ST.REASON_EVIDENCE_SESSION_MISMATCH,
})


class PositionEvidenceError(Exception):
    """适配器输入不合法（缺 cycle / account 作用域，或毫无证据可读的表结构）。"""


class PositionEvidenceStatus:
    """仓位证据的可证明性（**不是**"可卖性"）。"""

    PROVEN = "position_proven"
    PARTIAL = "position_partial"
    UNKNOWN = "position_unknown"
    UNPROVABLE = "position_unprovable"
    INVALID = "position_invalid"


POSITION_EVIDENCE_STATUSES = (
    PositionEvidenceStatus.PROVEN,
    PositionEvidenceStatus.PARTIAL,
    PositionEvidenceStatus.UNKNOWN,
    PositionEvidenceStatus.UNPROVABLE,
    PositionEvidenceStatus.INVALID,
)


class AcquisitionStatus:
    """单个 lot 的建仓证据状态。"""

    PROVEN = "acquisition_proven"
    UNPROVABLE = "acquisition_unprovable"
    NO_ORDER = "acquisition_order_missing"
    UNVERIFIED = "acquisition_not_verified"
    AMBIGUOUS_SESSION = "acquisition_session_ambiguous"
    INVALID_SESSION = "acquisition_session_invalid"
    IDENTITY_MISMATCH = "acquisition_identity_mismatch"


#: 只有 ``PROVEN`` 的 lot 才算"建仓事实"。其余一律 fail closed。
ACQUISITION_PROVEN = AcquisitionStatus.PROVEN


class LotSellability:
    """单个 lot 在 ``decision_session`` 的 T+1 结论（由生产 authority 给出）。"""

    SELLABLE = "t1_sellable"
    BLOCKED = "t1_blocked"
    UNKNOWN = "t1_unknown"
    NOT_EVALUATED = "t1_not_evaluated"


class T1EvalSource:
    """该 lot 的 T+1 结论由哪条路径给出（审计用，**不**改变任何规则）。"""

    PRODUCTION_PASSED_T1 = "production_exit_tradability_t1_passed"
    PRODUCTION_T1_BLOCKED = "production_exit_tradability_t1_blocked"
    AUTHORITY_EARLIEST_SELLABLE = "authority_earliest_sellable_session"
    UNRESOLVED = "unresolved"


class SellabilityStatus:
    """整个 position 在 ``decision_session`` 的仓位层面结论。"""

    T1_SELLABLE = "t1_sellable"
    T1_BLOCKED = "t1_blocked"
    POSITION_UNKNOWN = "position_unknown"
    POSITION_UNPROVABLE = "position_unprovable"
    POSITION_INVALID = "position_invalid"


#: 只有这两个是**可比的** T+1 观察；其余一律不进 agreement/disagreement 分母。
COMPARABLE_SELLABILITY = (SellabilityStatus.T1_SELLABLE, SellabilityStatus.T1_BLOCKED)
NOT_COMPARABLE_SELLABILITY = (
    SellabilityStatus.POSITION_UNKNOWN,
    SellabilityStatus.POSITION_UNPROVABLE,
    SellabilityStatus.POSITION_INVALID,
)
SELLABILITY_STATUSES = COMPARABLE_SELLABILITY + NOT_COMPARABLE_SELLABILITY


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _session_of(value: Any) -> Optional[str]:
    """``YYYY-MM-DD``；不是一个合法 session 就给 ``None``（fail closed）。"""
    text = _text(value)
    if text is None or len(text) < 10:
        return None
    head = text[:10]
    try:
        import datetime as _dt

        _dt.date.fromisoformat(head)
    except ValueError:
        return None
    return head


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> Optional[int]:
    """严格整数：``bool`` / 非整数一律 ``None``（不把 ``True`` 当 1）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _positive_int_or_none(value: Any) -> Optional[int]:
    """请求卖出量必须 ``int > 0``；``0`` / 负数 / 非法值一律 ``None``（fail closed）。"""
    number = _int_or_none(value)
    if number is None or number <= 0:
        return None
    return number


def _instant(value: Any) -> Optional[str]:
    """任意时点 → 规范 ISO 字符串；不可解析 → ``None``（**绝不**回退到"现在"）。"""
    text = _text(value)
    if text is None:
        return None
    moment = PIT.parse_available_at(text)
    if moment is None:
        moment = PIT.parse_asof(text)
    return None if moment is None else moment.isoformat(timespec="seconds")


def _later_of(left: Optional[str], right: Optional[str]) -> Optional[str]:
    """两个规范时点里较晚的那个；任一缺失 → ``None``（缺证据不是"更早可用"）。"""
    if left is None or right is None:
        return None
    return left if left >= right else right


def _sha256(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _row_field(row: Any, name: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (TypeError, IndexError, KeyError):
        return getattr(row, name, None)

@dataclass(frozen=True, slots=True)
class PositionLotEvidence:
    """一个 acquisition lot 的**只读**证据（决策时点口径）。

    ``acquisition_session`` 只可能来自 ``paper_fills.fill_date``（真实成交），绝不来自
    订单创建 / 意图 / 信号 / 选股日期。

    数量有三个**互不相同**的口径，刻意分开命名，防止互相冒充：

    ``quantity``
        该 lot 建仓时的**总股数**（``qty``）。建仓后不可变，是重放的起点。
    ``historical_quantity``
        按生产 FIFO 语义重放到 ``decision_at`` 时该 lot 的**剩余股数**。
        这是本层唯一允许用来计算 ``held_quantity`` 的口径。
    ``remaining_quantity``
        账本**当前**的 ``remaining_qty``。它会被未来 SELL 原地递减，因此**只能**
        用于自洽性核对与诊断，绝不能当历史持仓数量。
    """

    code: str
    account_id: Optional[str]
    cycle_id: Optional[int]
    lot_id: Optional[int]
    acquisition_session: Optional[str]
    quantity: int
    historical_quantity: int
    remaining_quantity: int
    fill_id: Optional[int]
    source_order_id: Optional[int]
    recorded_at: Optional[str]
    effective_at: Optional[str]
    available_at: Optional[str]
    source: str
    verification_status: str
    asset_type_source: Optional[str]
    asset_type_authority: Optional[str]
    is_t_base: Optional[bool]
    acquisition_status: str
    sellability: str
    sellability_reason: Optional[str]
    t1_eval_source: str = T1EvalSource.UNRESOLVED
    diagnostics: tuple = ()

    @property
    def proven(self) -> bool:
        return self.acquisition_status == ACQUISITION_PROVEN

    def identity(self) -> tuple:
        """该 lot 的**内容身份**：影响结论的字段全在内。"""
        return (
            self.code,
            self.account_id,
            self.cycle_id,
            self.lot_id,
            self.acquisition_session,
            self.quantity,
            self.historical_quantity,
            self.remaining_quantity,
            self.fill_id,
            self.source_order_id,
            self.available_at,
            self.verification_status,
            self.acquisition_status,
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "account_id": self.account_id,
            "cycle_id": self.cycle_id,
            "lot_id": self.lot_id,
            "acquisition_session": self.acquisition_session,
            "quantity": self.quantity,
            "historical_quantity": self.historical_quantity,
            "remaining_quantity": self.remaining_quantity,
            "fill_id": self.fill_id,
            "source_order_id": self.source_order_id,
            "recorded_at": self.recorded_at,
            "effective_at": self.effective_at,
            "available_at": self.available_at,
            "source": self.source,
            "verification_status": self.verification_status,
            "asset_type_source": self.asset_type_source,
            "asset_type_authority": self.asset_type_authority,
            "is_t_base": self.is_t_base,
            "acquisition_status": self.acquisition_status,
            "sellability": self.sellability,
            "sellability_reason": self.sellability_reason,
            "t1_eval_source": self.t1_eval_source,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class PositionSellabilityContext:
    """一个 ``(cycle, account, code)`` 在 ``decision_at`` 的仓位可卖性观察。"""

    code: str
    account_id: Optional[str]
    cycle_id: Optional[int]
    decision_session: Optional[str]
    decision_at: Optional[str]
    validation_as_of: Optional[str]

    held_quantity: int
    sellable_quantity: int
    t1_locked_quantity: int
    unknown_quantity: int
    consumed_quantity: int

    requested_sell_quantity: Optional[int]
    evidence_status: str
    evidence_fingerprint: str
    quantity_basis: str

    t1_authority_version: str

    acquisition_sessions: tuple = ()
    lots: tuple = ()
    diagnostics: tuple = ()

    # ── 结论 ──

    @property
    def comparable(self) -> bool:
        """是否可比。

        ``evidence_status`` 是**承重**的单一真源：只有 ``position_proven``
        （决策时点的全部份额都已证明）才可比。``partial`` / ``unknown`` /
        ``unprovable`` / ``invalid`` 一律不可比 —— 它们不进任何 denominator。

        份额不变量同时被检查：任何份额都不能凭空消失或凭空可卖。
        """
        return (
            self.evidence_status == PositionEvidenceStatus.PROVEN
            and self.held_quantity > 0
            and self.sellable_quantity + self.t1_locked_quantity == self.held_quantity
        )

    @property
    def sellability_status(self) -> str:
        """仓位层面的 T+1 结论。

        刻意**不**用 ``requested <= sellable`` 这种本地规则下结论，而是：
        未证明的份额存在 → 不可比（fail closed）；否则看是否存在被锁份额。
        """
        if self.evidence_status in (
            PositionEvidenceStatus.INVALID,
            PositionEvidenceStatus.UNKNOWN,
            PositionEvidenceStatus.UNPROVABLE,
        ):
            return {
                PositionEvidenceStatus.INVALID: SellabilityStatus.POSITION_INVALID,
                PositionEvidenceStatus.UNKNOWN: SellabilityStatus.POSITION_UNKNOWN,
                PositionEvidenceStatus.UNPROVABLE: SellabilityStatus.POSITION_UNPROVABLE,
            }[self.evidence_status]
        if self.unknown_quantity > 0:
            return SellabilityStatus.POSITION_UNPROVABLE
        if self.requested_sell_quantity is not None:
            if self.requested_sell_quantity > self.sellable_quantity:
                return SellabilityStatus.T1_BLOCKED
            return SellabilityStatus.T1_SELLABLE
        return (
            SellabilityStatus.T1_BLOCKED
            if self.t1_locked_quantity > 0
            else SellabilityStatus.T1_SELLABLE
        )

    def fingerprint(self) -> str:
        """内容指纹：影响结论的仓位上下文全在内（含请求卖出量）。"""
        return _sha256({
            "version": POSITION_EVIDENCE_FINGERPRINT_VERSION,
            "code": self.code,
            "account_id": self.account_id,
            "cycle_id": self.cycle_id,
            "decision_session": self.decision_session,
            "decision_at": self.decision_at,
            "validation_as_of": self.validation_as_of,
            "requested_sell_quantity": self.requested_sell_quantity,
            "evidence_status": self.evidence_status,
            "quantity_basis": self.quantity_basis,
            "lots": [lot.identity() for lot in self.lots],
        })

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "account_id": self.account_id,
            "cycle_id": self.cycle_id,
            "decision_session": self.decision_session,
            "decision_at": self.decision_at,
            "validation_as_of": self.validation_as_of,
            "held_quantity": self.held_quantity,
            "sellable_quantity": self.sellable_quantity,
            "t1_locked_quantity": self.t1_locked_quantity,
            "unknown_quantity": self.unknown_quantity,
            "consumed_quantity": self.consumed_quantity,
            "requested_sell_quantity": self.requested_sell_quantity,
            "evidence_status": self.evidence_status,
            "sellability_status": self.sellability_status,
            "comparable": self.comparable,
            "evidence_fingerprint": self.evidence_fingerprint,
            "quantity_basis": self.quantity_basis,
            "t1_authority_version": self.t1_authority_version,
            "acquisition_sessions": list(self.acquisition_sessions),
            "positions": [lot.to_dict() for lot in self.lots],
            "diagnostics": list(self.diagnostics),
        }


def _invalid_context(code: Any, account_id: Any, cycle_id: Any, decision_session: Any, *,
                     decision_at: Any = None,
                     validation_as_of: Any, requested: Any, diagnostic: str
                     ) -> PositionSellabilityContext:
    """构造一个 ``position_invalid`` 观察（fail closed 的入口形状）。

    ``decision_at`` 只在调用方**确实给了**一个可解析值时才会落进 DTO —— 不可解析
    的显式值绝不被"修好"成 session close。
    """
    return PositionSellabilityContext(
        code=str(code or ""),
        account_id=_text(account_id),
        cycle_id=_int_or_none(cycle_id),
        decision_session=_session_of(decision_session),
        decision_at=_instant(decision_at),
        validation_as_of=_instant(validation_as_of),
        held_quantity=0, sellable_quantity=0, t1_locked_quantity=0,
        unknown_quantity=0, consumed_quantity=0,
        requested_sell_quantity=_positive_int_or_none(requested),
        evidence_status=PositionEvidenceStatus.INVALID,
        evidence_fingerprint="",
        quantity_basis=QUANTITY_BASIS_UNPROVABLE,
        t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
        diagnostics=(diagnostic,),
    )

class PositionEvidenceAdapter:
    """``paper_position_lots`` → :class:`PositionSellabilityContext`（只读）。

    唯一入口 :meth:`context_for`。它只做五件事：

    1. 在**显式的** ``(cycle_id, account_id, code)`` 作用域内读出 lot 行；
    2. 用 :mod:`execution_verification` 的权威谓词筛出**已验证**的买入成交；
    3. 按**生产自己的 FIFO 语义**把已发生的卖出重放回去，得到 ``decision_at``
       时每个 lot 的历史余额，并核对重放终态与账本现值是否自洽；
    4. 用 PIT 门禁把不该出现在该快照里的证据剔掉；
    5. 逐 lot 调用 :mod:`selection_tradability` 的卖出方向权威，汇总可卖份额。

    作用域是**必填**的：``cycle_id`` 与 ``account_id`` 缺一不可。生产路径是
    cycle-aware 且 account-specific 的，缺了任何一个都可能把别的周期 / 别的账户的
    份额汇进同一个 sellability context（规格逐字禁止）。
    """

    #: lot 行本身的读法（只读）。刻意**不带** ``remaining_qty > 0`` 过滤：
    #: 今天已被完全消耗的 lot 在 ``decision_at`` 当时可能仍然被持有，先读出来
    #: 交给重放判定，绝不能让它因为今天的余额为 0 就从历史持仓里消失。
    LOT_SQL = (
        "SELECT id,cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,"
        "acquired_at,available_date,asset_type,source_order_id,is_t_base "
        "FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=?"
    )

    def __init__(self, conn: sqlite3.Connection, *, evidence_provider: Optional[Callable] = None):
        self._conn = conn
        #: ``(code, session) -> ST.MarketEvidence``。给了才能让 T+1 authority 真的
        #: 判到 T+1 那一步；缺了所有可证明的 lot 一律 ``t1_unknown``（fail closed）。
        self._evidence_provider = evidence_provider
        #: ``paper_orders.cycle_id`` 是否存在（发现式，不写死假设）。
        self._orders_cycle_column: Optional[bool] = None
        #: ``cycle_id -> (started_at, ended_at)`` 日期窗口缓存。
        self._cycle_windows: dict = {}
        #: 全部周期行的缓存（``None`` = 尚未读取）。
        self._cycle_cache: Optional[list] = None

    # ── 读 ──

    def _rows(self, sql: str, args: Sequence[Any]) -> list:
        try:
            cursor = self._conn.execute(sql, tuple(args))
        except sqlite3.OperationalError:
            # 表还不存在（最小 fixture / 迁移中）→ 没有任何仓位证据。
            return []
        out = []
        for row in cursor:
            if isinstance(row, sqlite3.Row):
                out.append(dict(row))
            elif isinstance(row, Mapping):
                out.append(dict(row))
            else:  # pragma: no cover - 非 Row 游标
                out.append(dict(zip([d[0] for d in cursor.description], row,
                                    strict=False)))
        return out

    def load_lots(self, code: Any, *, cycle_id: Any = None, account_id: Any = None,
                  open_only: bool = False) -> list:
        """读出某 ``(cycle, account, code)`` 的 lot 行（**只读**）。

        ``cycle_id`` 与 ``account_id`` 都是必填；缺任何一个直接返回空列表 ——
        调用方拿不到跨周期 / 跨账户的池化证据。``open_only=True`` 时只保留账本
        当前仍有余额的行（诊断用途），历史口径的读取**必须**用 ``False``。
        """
        code_text = _text(code)
        cycle = _int_or_none(cycle_id)
        account = _text(account_id)
        if code_text is None or cycle is None or account is None:
            return []
        sql = self.LOT_SQL
        args: list = [cycle, account, code_text]
        if open_only:
            sql += f" AND {_OPEN_LOT_CLAUSE}"
        sql += " ORDER BY acquired_at,id"
        return self._rows(sql, args)

    def _orders_for(self, order_ids: Sequence[Any]) -> dict:
        ids = sorted({_int(value) for value in order_ids if value is not None})
        if not ids:
            return {}
        out: dict = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._rows(
                "SELECT id,account_id,side,code,name,status,created_at,executed_at,"
                "execution_status,execution_verified,execution_evidence_source "
                f"FROM paper_orders WHERE id IN ({placeholders})",
                chunk,
            )
            for row in rows:
                out[_int(_row_field(row, "id"))] = row
        return out

    def _buy_fills_for(self, order_ids: Sequence[Any]) -> dict:
        ids = sorted({_int(value) for value in order_ids if value is not None})
        if not ids:
            return {}
        grouped: dict = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._rows(
                "SELECT id,order_id,account_id,side,code,qty,price,fill_date,quote_at "
                f"FROM paper_fills WHERE side='buy' AND order_id IN ({placeholders}) "
                "ORDER BY id",
                chunk,
            )
            for row in rows:
                grouped.setdefault(_int(_row_field(row, "order_id")), []).append(row)
        return grouped

    def _sell_fills_for(self, account_id: str, code: str,
                        cycle_id: Any = None) -> list:
        """该 ``(account, code)`` 的**全部**卖出成交流水（含未验证行，供诊断）。

        每条都带来源委托的验证列，因此调用方可以用**同一份权威谓词**筛选。

        **刻意不在这里按周期过滤**：生产 ``paper_orders`` / ``paper_fills`` 都没有
        ``cycle_id``，一笔卖出的周期归属必须由 :meth:`_sell_events` 按时间窗**四态**
        判定（proven / mismatch / ambiguous / unprovable）并显式记录诊断。在这里
        用 SQL 直接筛掉，会让"明确属于别的周期"与"归属无法证明"两种截然不同的
        事实都消失成"没有这笔成交"，既无法诊断、也无法区分谁该 fail closed。

        ``cycle_id`` 参数保留仅为签名兼容（列存在时由 ``_sell_events`` 施加额外约束）。
        """
        return self._rows(
            "SELECT f.id AS fill_id, f.order_id AS order_id, f.account_id AS fill_account_id,"
            " f.code AS fill_code, f.side AS fill_side, f.qty AS fill_qty,"
            " f.fill_date AS fill_date, f.quote_at AS quote_at,"
            " o.account_id AS order_account_id, o.code AS order_code, o.side AS order_side,"
            " o.status AS order_status, o.executed_at AS executed_at,"
            " o.execution_status AS execution_status,"
            " o.execution_verified AS execution_verified,"
            " o.execution_evidence_source AS execution_evidence_source "
            "FROM paper_fills f JOIN paper_orders o ON o.id = f.order_id "
            "WHERE f.side='sell' AND f.account_id=? AND f.code=? "
            "ORDER BY f.fill_date, f.id",
            (account_id, code),
        )

    # ── 历史数量重放（生产 FIFO 语义） ──

    def _orders_have_cycle_column(self) -> bool:
        """``paper_orders`` 是否有 ``cycle_id`` 列（生产**没有**，但允许未来加上）。

        用 ``PRAGMA table_info`` 发现，而不是写死假设 —— 写死会在真实库上报
        ``no such column``，把整个仓位层打成异常。
        """
        if self._orders_cycle_column is None:
            columns = set()
            try:
                for row in self._conn.execute("PRAGMA table_info(paper_orders)"):
                    columns.add(str(_row_field(row, "name") or row[1]))
            except sqlite3.Error:  # pragma: no cover - 表不存在
                columns = set()
            self._orders_cycle_column = "cycle_id" in columns
        return self._orders_cycle_column

    def _cycle_rows(self) -> list:
        """全部周期行（只读，缓存一次）。周期数量是个位数，全读比按需查更省。"""
        if self._cycle_cache is None:
            self._cycle_cache = self._rows(
                "SELECT id, status, started_at, ended_at, created_at "
                "FROM paper_cycles ORDER BY id", ())
        return self._cycle_cache

    def _cycle_window(self, cycle_id: Any) -> Optional[tuple]:
        """**请求周期**的 ``(start, end)``（日期粒度），用于界定卖出归属。

        生产 ``paper_orders`` 没有 ``cycle_id``，因此周期归属只能靠**时间窗**。
        ``started_at`` 缺失时退回 ``created_at``（规格允许：该周期开始存在的最早
        时刻）；``ended_at`` 为 ``NULL`` 表示**开放窗口**（尚未结束）。两者都拿不到
        ⇒ ``None``：此时请求周期自身不可证明，任何卖出归属都必须 fail closed。
        """
        if cycle_id is None:
            return None
        if cycle_id in self._cycle_windows:
            return self._cycle_windows[cycle_id]
        window = None
        for row in self._cycle_rows():
            if _int_or_none(_row_field(row, "id")) != _int_or_none(cycle_id):
                continue
            start = _session_of(_row_field(row, "started_at")) or _session_of(
                _row_field(row, "created_at"))
            end = _session_of(_row_field(row, "ended_at"))
            window = (start, end)
            break
        self._cycle_windows[cycle_id] = window
        return window

    def _competing_cycles(self, cycle_id: Any, session: Optional[str]) -> tuple:
        """找出**同时**可能拥有该笔卖出的其它周期 → ``(ambiguous, skipped)``。

        生产 ``paper_fills`` / ``paper_orders`` 都没有 ``cycle_id``，所以一笔卖出
        是否属于请求周期只能靠时间窗。若另一个周期的时间窗**也**能证明包含该
        session，则这笔卖出的周期归属无法唯一确定 ⇒ ``ambiguous``，必须 fail
        closed（禁止用 ``latest id`` / ``latest start`` / ``active cycle`` 猜）。

        竞争周期的窗口**必须可证明**，因此要求它记录了 ``started_at``：
        ``created_at`` 只是行插入时刻，不构成「该周期曾运作」的证明。已核实生产
        数据里 cycles 5/6/7 正是「已运行并归档、但 ``started_at`` 为空」的行，
        所以「空 start」只说明**起点的证据缺失**，不能用它去否决一笔真实归属。
        被跳过的周期全部记入 ``skipped`` 诊断，绝不静默忽略。

        ``ended_at`` 已记录且早于该 session 的周期**可证明不竞争**（无论起点是否
        已知）—— 这是最常见的排除路径。
        """
        if cycle_id is None or session is None:
            return False, []
        requested = _int_or_none(cycle_id)
        ambiguous = False
        skipped: list = []
        for row in self._cycle_rows():
            other = _int_or_none(_row_field(row, "id"))
            if other is None or other == requested:
                continue
            start = _session_of(_row_field(row, "started_at"))
            end = _session_of(_row_field(row, "ended_at"))
            # 已结束且结束日早于该卖出 → 可证明不属于它（起点未知也无妨）。
            if end is not None and session > end:
                continue
            # 起点未知 → 无法证明它包含该卖出，也无法用它否决归属：记诊断后跳过。
            if start is None:
                skipped.append(other)
                continue
            if session < start:
                continue
            # 起点可证明 <= session 且未在 session 前结束 → 窗口包含该卖出。
            ambiguous = True
        return ambiguous, skipped

    def _sell_events(self, account_id: str, code: str,
                     cycle_id: Any = None) -> list:
        """归一化卖出事件，并做**身份完整性**与**周期归属**校验。

        每条事件带：``session`` / ``executed_at`` / ``qty`` / ``verified`` /
        ``identity_ok`` / ``cycle_attribution`` / ``cycle_ok`` / ``fill_id``。

        身份冲突（成交属于另一个账户或另一只股票）不是「跳过」，而是一个必须
        fail closed 的事实 —— 绝不允许借另一个账户的已验证卖出改变本账户的历史
        持仓。

        ``cycle_attribution`` 是**四态**，不是一个默认为真的布尔值：

        * ``proven``     —— 该卖出落在请求周期窗内，且没有任何其它周期能证明拥有它；
        * ``mismatch``   —— 明确落在请求周期窗外（另一资金池的成交，不得扣减）；
        * ``ambiguous``  —— 多个周期窗都能证明包含它，归属无法唯一确定；
        * ``unprovable`` —— 请求周期自身没有可用窗口（缺行 / 起点不可证明）。

        只有 ``proven`` 允许参与重放；其余三态一律 fail closed，**绝不**把
        「不知道」默认成 ``cycle_ok=True`` —— 那正是规格禁止的静默升级。

        ``executed_at`` 是委托的**真实成交时刻**：同一 session 内的卖出用它判断
        「是否发生在决策时点之前」，不再拿 session 收盘时刻去近似。
        """
        window = self._cycle_window(cycle_id)
        events = []
        for row in self._sell_fills_for(account_id, code, cycle_id=cycle_id):
            fill_account = _text(_row_field(row, "fill_account_id"))
            order_account = _text(_row_field(row, "order_account_id"))
            fill_code = _text(_row_field(row, "fill_code"))
            order_code = _text(_row_field(row, "order_code"))
            fill_side = str(_row_field(row, "fill_side") or "").lower()
            order_side = str(_row_field(row, "order_side") or "").lower()
            session = _session_of(_row_field(row, "fill_date"))
            identity_ok = (
                fill_account == account_id
                and order_account == account_id
                and fill_code == code
                and order_code == code
                and fill_side == "sell"
                and order_side == "sell"
            )

            # ── 周期归属：四态判定，绝不默认 proven ──
            attribution = CYCLE_ATTRIBUTION_UNPROVABLE
            skipped: list = []
            if cycle_id is not None:
                if window is None or session is None:
                    # 请求周期没有可用窗口 / 卖出 session 不可解析：归属无从证明。
                    attribution = CYCLE_ATTRIBUTION_UNPROVABLE
                else:
                    start_at, end_at = window
                    if start_at is None:
                        # 周期行存在但起点不可证明 → 无法断言该卖出属于它。
                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE
                    elif session < start_at or (end_at is not None and session > end_at):
                        attribution = CYCLE_ATTRIBUTION_MISMATCH
                    else:
                        ambiguous, skipped = self._competing_cycles(cycle_id, session)
                        attribution = (CYCLE_ATTRIBUTION_AMBIGUOUS if ambiguous
                                       else CYCLE_ATTRIBUTION_PROVEN)

                # 若将来 schema 真的有了 ``paper_orders.cycle_id``，它是**更强**的
                # 显式身份，因此优先采信；但与时间窗**直接冲突**时不得静默选一边。
                if self._orders_have_cycle_column():
                    order_cycle = _int_or_none(_row_field(row, "order_cycle_id"))
                    if order_cycle is None:
                        # 列存在却读不出值 → 显式身份缺失，无法采信。
                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE
                    elif order_cycle != _int_or_none(cycle_id):
                        # 列明确指向**别的**周期；若时间窗又说属于本周期，则是硬冲突。
                        attribution = (CYCLE_ATTRIBUTION_UNPROVABLE
                                       if attribution == CYCLE_ATTRIBUTION_PROVEN
                                       else CYCLE_ATTRIBUTION_MISMATCH)
                    elif attribution == CYCLE_ATTRIBUTION_MISMATCH:
                        # 列说属于本周期，时间窗却证明它在窗外 → 硬冲突。
                        attribution = CYCLE_ATTRIBUTION_UNPROVABLE
                    else:
                        # 列是更强约束：归属成立（时间窗的 ambiguous/unprovable
                        # 已被显式身份解决）。
                        attribution = CYCLE_ATTRIBUTION_PROVEN

            events.append({
                "fill_id": _int(_row_field(row, "fill_id")),
                "order_id": _int(_row_field(row, "order_id")),
                "session": session,
                "executed_at": _instant(_row_field(row, "executed_at")),
                "qty": _int(_row_field(row, "fill_qty")),
                "verified": bool(EV.is_verified_row(row)),
                "identity_ok": identity_ok,
                "cycle_attribution": attribution,
                "cycle_ok": attribution == CYCLE_ATTRIBUTION_PROVEN,
                "cycle_skipped": tuple(skipped),
            })
        return events

    @staticmethod
    def _eligible_lots(lots: list, remaining: dict, session: str,
                       sell_executed_at: Optional[str] = None) -> list:
        """生产 ``_consume_available_lots`` 的候选集 + **日内存在性**约束。

        生产语义（逐字复现）：余额 > 0 且 ``available_date <= session``。

        本层额外加一条 PIT 约束，因为生产是按**当下**账本消耗的，而这里要重建
        **过去某一时刻**的持仓：

        * ``lot_existence_at <= sell_executed_at`` —— 一笔卖出只能消费在它**成交
          之前就已经存在**的 lot。没有这条，13:00 才买入的 T+0 lot 会倒过来满足
          10:00 的卖出（PIT 时间穿越），把「当时根本不够卖」的历史粉饰成「卖得掉」。

        ``lot_existence_at`` 的定义（PR body 同步声明）：**该 lot 可被证明已经存在
        的权威时点**，由两个生产事实共同约束 —— 来源买单的 ``executed_at``（成交
        时刻）与 ``paper_position_lots.acquired_at``（lot 写入账本的时刻）。要求
        两者都早于卖出成交时刻，因此取**较晚**者作为闸门值（更严格的一侧生效）。
        任一都拿不到 → 该 lot 没有可证明的存在时点，**不得**参与任何卖出消费。

        ``sell_executed_at`` 缺失时：只有 ``available_date`` 严格早于卖出 session
        （即「昨天或更早就已可卖」）的 lot 才放行 —— 它的存在时点必定早于本
        session 的任何时刻。同日 lot 无法证明日内先后 → 排除，由调用方 fail closed。
        """
        eligible = []
        for lot in lots:
            if remaining.get(lot["id"], 0) <= 0:
                continue
            available = lot["available_date"]
            if available is None or str(available)[:10] > session:
                continue
            if sell_executed_at is None:
                # 拿不到卖出成交时刻：只有「早于本 session 就已可卖」的 lot 才
                # 必然先于该卖出存在。同日 lot 无法证明日内先后 → 不放行。
                if str(available)[:10] < session:
                    eligible.append(lot)
                continue
            existence = lot.get("existence_at")
            if existence is None or existence > sell_executed_at:
                # 无法证明它在卖出之前存在，或明确晚于卖出 → 不得消费。
                continue
            eligible.append(lot)
        return eligible

    def _replay(self, lots: list, events: list, *, decision_session: str,
                decision_at: Optional[str]) -> dict:
        """把已发生的卖出按生产 FIFO 语义重放，取 ``decision_at`` 的快照。

        返回 ``{"snapshot": {lot_id: qty}, "final": {...}, "consistent": bool,
        "diagnostics": [...]}``。

        三条不变量（任一不满足即 ``consistent=False``，调用方必须 fail closed）：

        1. **存在性**：一笔卖出只能消费在它**成交之前**就已经存在的 lot
           （``lot_existence_at <= sell_executed_at``）。没有这条，13:00 才买入的
           T+0 lot 会倒过来满足 10:00 的卖出 —— PIT 时间穿越。
        2. **可得性**：沿用生产 ``available_date <= session``（T+1 / T+0 语义来自
           生产写入的列，本层不自己推导）。
        3. **作用域**：同 account、同 cycle（周期归属必须是 ``proven``）。

        ``consistent`` 的判据是**可执行的**：重放到今天的终态必须逐 lot 等于账本
        现值。只要有一行不等，就说明成交证据不完整或身份冲突 —— 此时任何「历史
        数量」都不可信，调用方必须 fail closed（绝不退回当前余额）。

        **绝不能**因为「最终 remaining_qty 与今天账本碰巧相等」就宣布历史 proven：
        终态相等只是必要条件，存在性 / 可得性 / 作用域三条闸门都通过才成立。
        """
        diagnostics: list = []
        ordered = sorted(lots, key=lambda item: (str(item["acquired_at"] or ""),
                                                 _int(item["id"])))
        remaining = {lot["id"]: _int(lot["qty"]) for lot in ordered}
        if any(value <= 0 for value in remaining.values()):
            diagnostics.append("lot_quantity_not_positive")
            return {"snapshot": {}, "final": {}, "consistent": False,
                    "diagnostics": diagnostics}

        decision_close = ST.session_close_at(decision_session)
        snapshot: Optional[dict] = None

        for event in self._ordered_events(events):
            session = event["session"]
            if session is None:
                diagnostics.append("sell_fill_session_invalid")
                return {"snapshot": {}, "final": {}, "consistent": False,
                        "diagnostics": diagnostics}
            if not event["identity_ok"]:
                diagnostics.append("sell_fill_identity_mismatch")
                return {"snapshot": {}, "final": {}, "consistent": False,
                        "diagnostics": diagnostics}
            # ── 周期归属 ──
            #
            # ``mismatch``：这笔成交明确属于**另一个资金池**，因此不得扣减本周期
            # 的 lot。这里**跳过**而不是 fail closed —— 归属本身是被证明的（只是
            # 不属于我们）。若它实际上动了本周期的 lot，终态的账本自洽性检查会
            # 立刻发现（``replay_does_not_match_ledger``）并 fail closed。
            #
            # ``ambiguous`` / ``unprovable``：归属**无法证明**。既不能当成属于我们
            # （会凭另一个周期的成交伪造历史），也不能简单跳过（会把一次真实的
            # 作用域错误伪装成数据缺口）—— 必须显式 fail closed。
            attribution = event.get("cycle_attribution")
            if attribution == CYCLE_ATTRIBUTION_MISMATCH:
                diagnostics.append(_CYCLE_ATTRIBUTION_DIAGNOSTIC[attribution])
                continue
            if attribution is not None and attribution != CYCLE_ATTRIBUTION_PROVEN:
                diagnostics.append(_CYCLE_ATTRIBUTION_DIAGNOSTIC.get(
                    attribution, "sell_fill_cycle_unprovable"))
                diagnostics.append("historical_quantity_unprovable")
                return {"snapshot": {}, "final": {}, "consistent": False,
                        "diagnostics": diagnostics}
            if not event.get("cycle_ok", True):
                diagnostics.append("sell_fill_cycle_unprovable")
                diagnostics.append("historical_quantity_unprovable")
                return {"snapshot": {}, "final": {}, "consistent": False,
                        "diagnostics": diagnostics}
            if not event["verified"]:
                # 未验证的卖出：生产在成交时**已经**扣减了 lot，因此忽略它会让
                # 重放终态与账本对不上。这里显式记诊断并让自洽性检查去判定。
                diagnostics.append("unverified_sell_fill_excluded")
                continue

            # 该笔卖出是否发生在 decision_at 之前？
            #
            # 同 session 的卖出**有真实成交时刻**（``paper_orders.executed_at``，
            # 生产在成交时写入）——必须用它判断，而不是拿 session 收盘时刻近似：
            # 10:00 已成交的卖单，在 14:00 回看时应当已经被扣除。只有拿不到
            # ``executed_at`` 时，才回退到「是否已过收盘」，且**判断不了就 fail
            # closed**（绝不假设它已发生或未发生）。
            if session < decision_session:
                consumed_before_decision = True
            elif session == decision_session:
                executed_at = event.get("executed_at")
                if executed_at is not None:
                    if decision_at is None:
                        diagnostics.append("same_session_sell_not_orderable")
                        return {"snapshot": {}, "final": {}, "consistent": False,
                                "diagnostics": diagnostics}
                    consumed_before_decision = executed_at <= decision_at
                elif decision_at is None or decision_close is None:
                    diagnostics.append("same_session_sell_not_orderable")
                    return {"snapshot": {}, "final": {}, "consistent": False,
                            "diagnostics": diagnostics}
                elif decision_at >= decision_close:
                    # 决策时点已过该 session 收盘：这一天的成交（盘中何时都好）
                    # 必定先于决策，因此确定已被扣除。
                    consumed_before_decision = True
                else:
                    # 决策时点在该 session **盘中**，却拿不到真实成交时刻 ——
                    # 无法判定这笔同日卖出究竟在决策之前还是之后。猜「已发生」会把
                    # 未发生的卖出算进历史；猜「未发生」会把已卖出的份额当成仍持有
                    # （即「不知道」变成「可卖」/「可持有」）。两侧都不能猜。
                    diagnostics.append("same_session_sell_time_unknown")
                    return {"snapshot": {}, "final": {}, "consistent": False,
                            "diagnostics": diagnostics}
            else:
                consumed_before_decision = False

            if not consumed_before_decision and snapshot is None:
                # 到达决策时点：先把快照定下来，再继续重放到今天。
                snapshot = dict(remaining)

            qty = event["qty"]
            if qty <= 0:
                diagnostics.append("sell_fill_quantity_not_positive")
                return {"snapshot": {}, "final": {}, "consistent": False,
                        "diagnostics": diagnostics}

            eligible = self._eligible_lots(ordered, remaining, session,
                                           sell_executed_at=event.get("executed_at"))
            available_total = sum(remaining[lot["id"]] for lot in eligible)
            if available_total < qty:
                # 逐字复现生产：聚合可卖量不足时**不动任何 lot**（生产返回 0 消耗）。
                #
                # 但在「重建历史持仓」这件事上这不是一个可以继续走的小异常：它意味着
                # 有一笔卖出没有被任何 lot 解释，账本却被扣减过。此时任何历史数量
                # 都不可信，必须立刻把整组判为 unprovable —— 不能只记一条诊断然后
                # continue，那会让后续 lot 看起来「还在」，把不可证明的历史粉饰成
                # 可卖数量（规格明确禁止 missing ⇒ assume sellable）。
                diagnostics.append("sell_fill_exceeds_available_lots")
                if self._future_lot_would_be_needed(ordered, remaining, session, qty,
                                                    event.get("executed_at")):
                    # 更精确的根因：只有「未来 lot」才够补足 → 明确点名，
                    # 因为这是 PIT 时间穿越，而不是单纯的数据缺口。
                    diagnostics.append("future_lot_consumption_required")
                diagnostics.append("historical_quantity_unprovable")
                return {"snapshot": {}, "final": {}, "consistent": False,
                        "diagnostics": diagnostics}
            left = qty
            for lot in eligible:
                take = min(left, remaining[lot["id"]])
                if take <= 0:
                    continue
                remaining[lot["id"]] -= take
                left -= take
                if not left:
                    break

        if snapshot is None:
            snapshot = dict(remaining)

        final = dict(remaining)
        consistent = True
        for lot in ordered:
            if _int(lot["remaining_qty"]) != final[lot["id"]]:
                consistent = False
                diagnostics.append("replay_does_not_match_ledger")
                break
        return {"snapshot": snapshot, "final": final, "consistent": consistent,
                "diagnostics": diagnostics}

    @staticmethod
    def _with_existence_at(lots: list, orders: Mapping[Any, Mapping[str, Any]]) -> list:
        """给每个 lot 标注**可证明的存在时点** ``existence_at``（只读派生，不改库）。

        取值 = ``max(来源买单 executed_at, lot.acquired_at)``（两者都必须是"已发生"
        的事实，故取更严格的一侧）；两者都拿不到 → ``None``，该 lot 不得参与任何
        卖出消费（由 :meth:`_eligible_lots` 与 ``_replay`` 的闸门处理）。

        ``executed_at`` 只在来源委托**确实是买单**时采信：拿卖单的成交时刻去证明
        一个 lot 的存在时点没有任何意义，只会放宽闸门。
        """
        out = []
        for row in lots:
            order_id = _row_field(row, "source_order_id")
            order = orders.get(_int(order_id)) if order_id is not None else None
            executed = None
            if order is not None and str(_row_field(order, "side") or "").lower() == "buy":
                executed = _instant(_row_field(order, "executed_at"))
            recorded = _instant(_row_field(row, "acquired_at"))
            if executed is None and recorded is None:
                existence = None
            elif executed is None:
                existence = recorded
            elif recorded is None:
                existence = executed
            else:
                existence = executed if executed >= recorded else recorded
            item = dict(row)
            item["existence_at"] = existence
            out.append(item)
        return out

    @staticmethod
    def _ordered_events(events: list) -> list:
        """把卖出事件按**真实业务时间**排序，而不是按数据库 id。

        同一 session 内多笔卖出（10:00 卖 A、11:00 卖 B）必须保持时间顺序，否则
        FIFO 会把它们分配给不同的 lot，得出错误的历史持仓。

        排序键：``(session, executed_at, fill_id)``。``fill_id`` 只是**确定性
        tie-breaker**（保证同样的输入永远得到同样的输出），不冒充业务时间。

        ``session`` 或 ``executed_at`` 缺失时：保留 ``""`` 占位，让它们排在可证明
        的时点之前 —— 这种事件随后会被 ``_replay`` 的存在性闸门拦下并 fail closed，
        绝不会被当成「更早成交」而获得优先消费权。
        """
        return sorted(
            events,
            key=lambda item: (
                str(item.get("session") or ""),
                str(item.get("executed_at") or ""),
                _int(item.get("fill_id")),
            ),
        )

    @staticmethod
    def _future_lot_would_be_needed(lots: list, remaining: dict, session: str,
                                    qty: int,
                                    sell_executed_at: Optional[str]) -> bool:
        """该笔卖出的缺口是否**只能**由「当时还不存在」的 lot 补足。

        用于把根因写清楚（``future_lot_consumption_required``）：可卖量不足不是因为
        证据缺失，而是因为后续才建仓的 lot 被**存在性闸门**排除 —— 这正是 PIT 时间
        穿越的形状，与「成交证据本身不完整」是两件事。

        判据：按 ``available_date`` 够格、但被存在性闸门挡掉的 lot 加起来，正好能
        补上缺口 → 只有它们才够 → 未来 lot 会被迫参与消费。
        """
        if sell_executed_at is None:
            return False
        eligible_total = 0
        by_availability_total = 0
        for lot in lots:
            if remaining.get(lot["id"], 0) <= 0:
                continue
            available = lot.get("available_date")
            if available is None or str(available)[:10] > session:
                continue
            by_availability_total += remaining[lot["id"]]
            existence = lot.get("existence_at")
            if existence is not None and existence <= sell_executed_at:
                eligible_total += remaining[lot["id"]]
        return eligible_total < qty <= by_availability_total

    # ── 判定 ──

    def _visible(self, available_at: Optional[str], asof: Optional[str]) -> bool:
        """PIT 门禁。``asof is None`` = live 兼容模式（契约里可见）。

        **绝不**在 ``asof`` 不可解析时当成"无上界"：不可解析的值在调用方就已经被
        判成 ``position_invalid``，根本走不到这里。
        """
        if asof is None:
            return True
        if available_at is None:
            # 可用时点不可证明 → 严格快照下不可见（缺失不是"更早可用"）。
            return False
        return bool(PIT.is_visible_at(available_at, asof).get("visible"))

    @staticmethod
    def _fill_rows(fill: Any) -> list:
        """把 ``fill`` 归一成**成交行的列表**。

        ``paper_fills`` 的读法允许多行（同一笔委托可以有多条流水）。传进来的如果
        已是一条行（``Mapping``）就包成单元素列表；否则按序列处理。**绝不**把一行
        ``Mapping`` 迭代成它的字段名 —— 那会把证据悄悄丢掉。
        """
        if fill is None:
            return []
        if isinstance(fill, Mapping):
            return [fill]
        if isinstance(fill, (list, tuple)):
            return list(fill)
        return [fill]

    def _lot_evidence(self, row: Mapping[str, Any], order: Optional[Mapping[str, Any]],
                      fill: Any, *, code: str, account_id: str,
                      historical_quantity: int) -> PositionLotEvidence:
        """把一个 lot 行 + 它的订单/成交证据变成一条 :class:`PositionLotEvidence`。

        ``historical_quantity`` 由重放给出（**不是**账本现值），这里只负责把证据
        本身判定清楚。
        """
        diagnostics: list = []
        lot_id = _int(_row_field(row, "id"))
        qty = _int(_row_field(row, "qty"))
        remaining = _int(_row_field(row, "remaining_qty"))
        order_id = _row_field(row, "source_order_id")
        name = _text(_row_field(row, "name"))
        asset_type_source = _text(_row_field(row, "asset_type"))
        is_t_base_raw = _row_field(row, "is_t_base")
        is_t_base = None if is_t_base_raw is None else bool(_int(is_t_base_raw))

        # 权威资产类型（T+0 ETF 语义由它决定，不硬编码任何 ETF 列表）。
        try:
            asset_type_authority = PTR.asset_type(code, name)
        except Exception:  # pragma: no cover - 规则模块异常不得让适配器崩
            asset_type_authority = None

        recorded_at = _instant(_row_field(row, "acquired_at"))

        # ── 建仓事实：只认**已验证**的买入成交 ──
        acquisition_status = AcquisitionStatus.PROVEN
        fill_id = None
        fill_session = None
        effective_at = None
        verification_status = EV.EXECUTION_STATUS_UNKNOWN
        source = EV.EVIDENCE_SOURCE_ABSENT

        if order is None:
            acquisition_status = AcquisitionStatus.NO_ORDER
        else:
            verification_status = _text(_row_field(order, "execution_status")) or EV.EXECUTION_STATUS_UNKNOWN
            source = _text(_row_field(order, "execution_evidence_source")) or EV.EVIDENCE_SOURCE_ABSENT
            if str(_row_field(order, "side") or "").lower() != "buy":
                acquisition_status = AcquisitionStatus.NO_ORDER
                diagnostics.append("source_order_is_not_a_buy")
            elif not EV.is_verified_row(order):
                # ``paper_orders.status='filled'`` 是账本自称，不是成交证据。
                acquisition_status = AcquisitionStatus.UNVERIFIED
            else:
                fill_rows = self._fill_rows(fill)
                if not fill_rows:
                    acquisition_status = AcquisitionStatus.UNVERIFIED
                else:
                    # ── 身份完整性：lot / order / fill 三方必须指向同一事实 ──
                    fill_id = _int(_row_field(fill_rows[0], "id"))
                    order_account = _text(_row_field(order, "account_id"))
                    order_code = _text(_row_field(order, "code"))
                    lot_account = _text(_row_field(row, "account_id"))
                    lot_code = _text(_row_field(row, "code"))
                    if (order_account != account_id or lot_account != account_id
                            or order_code != code or lot_code != code):
                        acquisition_status = AcquisitionStatus.IDENTITY_MISMATCH
                        diagnostics.append("source_order_identity_mismatch")
                    else:
                        for item in fill_rows:
                            if (str(_row_field(item, "side") or "").lower() != "buy"
                                    or _text(_row_field(item, "account_id")) != account_id
                                    or _text(_row_field(item, "code")) != code):
                                acquisition_status = AcquisitionStatus.IDENTITY_MISMATCH
                                diagnostics.append("fill_identity_mismatch")
                                break
                    if acquisition_status == AcquisitionStatus.PROVEN:
                        sessions = {_session_of(_row_field(item, "fill_date")) for item in fill_rows}
                        if None in sessions or len(sessions) != 1:
                            # 多笔成交落在不同 session（或 session 不可解析）→ 无法用
                            # 单一 acquisition session 描述该 lot，fail closed。
                            acquisition_status = AcquisitionStatus.AMBIGUOUS_SESSION
                        else:
                            fill_session = sessions.pop()
                            effective_at = _instant(ST.session_close_at(fill_session))
                            if effective_at is None:
                                acquisition_status = AcquisitionStatus.INVALID_SESSION

        if (asset_type_source and asset_type_authority
                and asset_type_source != asset_type_authority):
            # lot 记的资产类型与权威口径冲突 → 无法确定 T+0/T+1 语义。
            diagnostics.append("asset_type_conflict")
            if acquisition_status == AcquisitionStatus.PROVEN:
                acquisition_status = AcquisitionStatus.UNVERIFIED

        available_at = _later_of(effective_at, recorded_at) if acquisition_status == ACQUISITION_PROVEN else None

        return PositionLotEvidence(
            code=code,
            account_id=_text(_row_field(row, "account_id")),
            cycle_id=_int_or_none(_row_field(row, "cycle_id")),
            lot_id=lot_id,
            acquisition_session=fill_session,
            quantity=qty,
            historical_quantity=historical_quantity,
            remaining_quantity=remaining,
            fill_id=fill_id,
            source_order_id=None if order_id is None else _int(order_id),
            recorded_at=recorded_at,
            effective_at=effective_at,
            available_at=available_at,
            source=source,
            verification_status=verification_status,
            asset_type_source=asset_type_source,
            asset_type_authority=asset_type_authority,
            is_t_base=is_t_base,
            acquisition_status=acquisition_status,
            sellability=(
                LotSellability.SELLABLE if acquisition_status == ACQUISITION_PROVEN
                else LotSellability.NOT_EVALUATED
            ),
            sellability_reason=None,
            diagnostics=tuple(diagnostics),
        )

    def _classify_lot(self, lot: PositionLotEvidence, *, decision_session: str) -> PositionLotEvidence:
        """用**生产 authority** 判定该 lot 的 T+1 结论。不重写任何规则。

        第一道闸：**建仓事实不可证明就绝不进入可卖性判定**。这条分支必须在这里、
        而不是靠 PIT 门禁"顺带"拦住 —— 靠 PIT 拦会把"建仓不可证明"伪装成
        "证据当时不可见"，两者是不同的诊断，而且前者的守卫会变成死代码。
        """
        if lot.acquisition_status != ACQUISITION_PROVEN:
            return dataclasses.replace(
                lot, sellability=LotSellability.UNKNOWN,
                sellability_reason=lot.acquisition_status,
                t1_eval_source=T1EvalSource.UNRESOLVED,
            )
        if self._evidence_provider is None:
            # 没有市场证据就没有权威判定 → fail closed，不当可卖。
            return dataclasses.replace(
                lot, sellability=LotSellability.UNKNOWN,
                sellability_reason="evidence_provider_absent",
                t1_eval_source=T1EvalSource.UNRESOLVED,
            )
        try:
            evidence = self._evidence_provider(lot.code, decision_session)
        except Exception as exc:  # pragma: no cover - 适配器不得让上游异常穿透
            return dataclasses.replace(
                lot, sellability=LotSellability.UNKNOWN,
                sellability_reason=f"evidence_provider_error:{type(exc).__name__}",
                t1_eval_source=T1EvalSource.UNRESOLVED,
            )
        if evidence is None:
            return dataclasses.replace(
                lot, sellability=LotSellability.UNKNOWN,
                sellability_reason="market_evidence_absent",
                t1_eval_source=T1EvalSource.UNRESOLVED,
            )

        verdict = ST.exit_tradability(
            evidence,
            code=lot.code,
            exit_session=decision_session,
            entry_session=lot.acquisition_session,
        )
        reason = _text(getattr(verdict, "reason", None)) or ST.REASON_UNKNOWN_REASON
        if reason == ST.REASON_T1_NOT_SELLABLE:
            return dataclasses.replace(
                lot, sellability=LotSellability.BLOCKED, sellability_reason=reason,
                t1_eval_source=T1EvalSource.PRODUCTION_T1_BLOCKED,
            )
        if reason not in PRE_T1_REASONS:
            return dataclasses.replace(
                lot, sellability=LotSellability.SELLABLE, sellability_reason=reason,
                t1_eval_source=T1EvalSource.PRODUCTION_PASSED_T1,
            )

        # 生产 verdict 停在 T+1 **之前**（账户证券权限 / ST 状态未知 / PIT 等），
        # T+1 这一维根本不是由它回答的。此时按规格的要求**不得**让 T+1 overlay
        # 阻断（T+0 ETF 首当其冲：``security_scope`` 先以证券类型拦下它）。
        # 因此直接消费**同一份权威**的 T+1 入口问这一维，而不是复制它的比较逻辑。
        try:
            earliest = ST.earliest_sellable_session(
                lot.code, name=None, entry_session=lot.acquisition_session
            )
        except Exception as exc:  # pragma: no cover - 权威异常一律 fail closed
            return dataclasses.replace(
                lot, sellability=LotSellability.UNKNOWN,
                sellability_reason=f"authority_error:{type(exc).__name__}",
                t1_eval_source=T1EvalSource.UNRESOLVED,
            )
        if earliest is None:
            # 权威无法确定最早可卖 session → fail closed。
            return dataclasses.replace(
                lot, sellability=LotSellability.UNKNOWN,
                sellability_reason=f"{reason}:{T1EvalSource.UNRESOLVED}",
                t1_eval_source=T1EvalSource.UNRESOLVED,
            )
        if str(decision_session)[:10] < str(earliest)[:10]:
            return dataclasses.replace(
                lot, sellability=LotSellability.BLOCKED,
                sellability_reason=f"{reason}:t1_not_yet_sellable",
                t1_eval_source=T1EvalSource.AUTHORITY_EARLIEST_SELLABLE,
            )
        return dataclasses.replace(
            lot, sellability=LotSellability.SELLABLE,
            sellability_reason=f"{reason}:t1_passed",
            t1_eval_source=T1EvalSource.AUTHORITY_EARLIEST_SELLABLE,
        )

    # ── 入口 ──

    def replay_diagnostics(self, code: Any, *, cycle_id: Any = None,
                           account_id: Any = None) -> dict:
        """**只读**的历史数量重放诊断入口（公开 API）。

        存在的理由：production-copy 探针需要"验证今天账本能被重放重建"，这**不是**
        某个决策时点的观察。以前探针只能自己调私有 ``_sell_events`` / ``_replay``
        并伪造 ``decision_session="2099-01-01"`` —— 那既复制了一份作用域逻辑（正式
        实现改了探针不会跟着改），也用一个不存在的未来 session 当哨兵。

        本入口把"重放到账本现值"这件事说清楚：

        * 按**生产 FIFO 语义**重放该 ``(cycle, account, code)`` 的全部已验证卖出；
        * ``decision_session`` 取**账本里最晚的那个成交 session**（不是伪造的未来
          日期），``decision_at`` 取该 session 的收盘时刻 —— 于是"决策时点"就是
          "账本走到今天"，快照即终态；
        * 周期归属 / 存在性 / 可得性 / 身份四道闸门与 ``context_for`` **完全同源**
          （同一个 ``_sell_events`` / ``_eligible_lots`` / ``_replay``），不存在
          第二套作用域逻辑；
        * 只读：不写任何表。

        返回 ``{"consistent": bool, "diagnostics": [...], "groups": ...}`` 形状；
        ``consistent=False`` 表示成交证据不足以重建该组的历史数量（调用方不得把
        它当成"可卖"）。
        """
        code_text = _text(code)
        cycle = _int_or_none(cycle_id)
        account = _text(account_id)
        if code_text is None or cycle is None or account is None:
            return {"consistent": False, "diagnostics": ("missing_scope",),
                    "decision_session": None, "lots": 0}
        lots = self.load_lots(code_text, cycle_id=cycle, account_id=account,
                              open_only=False)
        if not lots:
            return {"consistent": False, "diagnostics": ("no_position_lot_evidence",),
                    "decision_session": None, "lots": 0}
        events = self._sell_events(account, code_text, cycle_id=cycle)
        # 决策时点 = 账本里最晚的**已发生**成交 session（含买入），而不是未来哨兵。
        sessions = [_session_of(_row_field(row, "acquired_at")) for row in lots]
        sessions += [event["session"] for event in events]
        known = [item for item in sessions if item]
        decision_session = max(known) if known else None
        if decision_session is None:
            return {"consistent": False,
                    "diagnostics": ("replay_decision_session_unknown",),
                    "decision_session": None, "lots": len(lots)}
        order_ids = [_row_field(row, "source_order_id") for row in lots]
        prepared = self._with_existence_at(lots, self._orders_for(order_ids))
        replay = self._replay(prepared, events, decision_session=decision_session,
                              decision_at=_instant(ST.session_close_at(decision_session)))
        attribution: dict = {}
        for event in events:
            key = event.get("cycle_attribution") or CYCLE_ATTRIBUTION_UNPROVABLE
            attribution[key] = attribution.get(key, 0) + 1
        return {
            "consistent": bool(replay["consistent"]),
            "diagnostics": tuple(replay["diagnostics"]),
            "decision_session": decision_session,
            "lots": len(lots),
            "events": len(events),
            # 周期归属分布（四态）：调用方据此区分"证据不足"与"归属无法证明"。
            "attribution": attribution,
        }

    def context_for(self, code: Any, *, cycle_id: Any = None, account_id: Any = None,
                    decision_session: Any, decision_at: Any = None,
                    validation_as_of: Any = None,
                    requested_sell_quantity: Any = None) -> PositionSellabilityContext:
        """构造 ``decision_at`` 这一刻的仓位可卖性观察（**只读**）。

        作用域与 PIT 都是显式的：

        * ``cycle_id`` / ``account_id`` 必填；缺任何一个 → ``position_invalid``
          （绝不把跨周期 / 跨账户的份额汇成一个 context）；
        * ``decision_at`` 给了就用它（原样规范化），没给才回退到
          ``session_close_at(decision_session)``；给了但不可解析 → ``position_invalid``；
        * ``validation_as_of`` 给了但不可解析 → ``position_invalid``（**绝不**当成
          "无上界"，那是 future leak）；
        * ``requested_sell_quantity`` 给了就必须是 ``int > 0``，否则 → ``position_invalid``。
        """
        code_text = _text(code)
        session = _session_of(decision_session)
        cycle = _int_or_none(cycle_id)
        account = _text(account_id)
        if code_text is None or session is None:
            return _invalid_context(
                code_text, account_id, cycle_id, decision_session,
                decision_at=decision_at, validation_as_of=validation_as_of,
                requested=requested_sell_quantity,
                diagnostic="invalid_code_or_decision_session",
            )
        if cycle is None or account is None:
            return _invalid_context(
                code_text, account_id, cycle_id, session,
                decision_at=decision_at, validation_as_of=validation_as_of,
                requested=requested_sell_quantity,
                diagnostic="missing_cycle_or_account_scope",
            )

        # ── 精确 decision_at：给了就用，缺了才按契约回退 ──
        if decision_at is None:
            resolved_decision_at = _instant(ST.session_close_at(session))
        else:
            resolved_decision_at = _instant(decision_at)
            if resolved_decision_at is None:
                return _invalid_context(
                    code_text, account, cycle, session,
                    validation_as_of=validation_as_of,
                    requested=requested_sell_quantity,
                    diagnostic="invalid_decision_at",
                )

        # ── 显式 validation_as_of 必须合法；不可解析绝不是"无上界" ──
        if validation_as_of is None:
            asof = None
        else:
            asof = _instant(validation_as_of)
            if asof is None:
                return _invalid_context(
                    code_text, account, cycle, session,
                    decision_at=resolved_decision_at,
                    validation_as_of=validation_as_of,
                    requested=requested_sell_quantity,
                    diagnostic="invalid_validation_as_of",
                )

        # ── 请求卖出量：给了就必须是正整数 ──
        if requested_sell_quantity is None:
            requested = None
        else:
            requested = _positive_int_or_none(requested_sell_quantity)
            if requested is None:
                return _invalid_context(
                    code_text, account, cycle, session,
                    decision_at=resolved_decision_at, validation_as_of=asof,
                    requested=requested_sell_quantity,
                    diagnostic="invalid_requested_sell_quantity",
                )

        raw_lots = self.load_lots(code_text, cycle_id=cycle, account_id=account,
                                  open_only=False)
        if not raw_lots:
            return PositionSellabilityContext(
                code=code_text, account_id=account, cycle_id=cycle,
                decision_session=session, decision_at=resolved_decision_at,
                validation_as_of=asof,
                held_quantity=0, sellable_quantity=0, t1_locked_quantity=0,
                unknown_quantity=0, consumed_quantity=0,
                requested_sell_quantity=requested,
                evidence_status=PositionEvidenceStatus.UNKNOWN,
                evidence_fingerprint="",
                quantity_basis=QUANTITY_BASIS_UNPROVABLE,
                t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
                diagnostics=("no_position_lot_evidence",),
            )

        # ── lot 的存在时点：重放的日内闸门 ──
        #
        # 生产是按**当下**账本消耗 lot 的，而这里要重建过去某一时刻的持仓，因此
        # 必须能证明「该 lot 在卖出成交之前就已经存在」。两个生产事实共同约束：
        # 来源买单的 ``executed_at``（成交时刻）与 lot 行的 ``acquired_at``（记账
        # 时刻）。要求两者都早于卖出成交时刻 ⇒ 取较晚者作为闸门值。
        order_ids = [_row_field(row, "source_order_id") for row in raw_lots]
        orders = self._orders_for(order_ids)
        fills = self._buy_fills_for(order_ids)
        raw_lots = self._with_existence_at(raw_lots, orders)

        # ── 历史数量：先重放，再判定 ──
        events = self._sell_events(account, code_text, cycle_id=cycle)
        replay = self._replay(raw_lots, events, decision_session=session,
                              decision_at=resolved_decision_at)
        diagnostics: list = list(replay["diagnostics"])
        if not replay["consistent"]:
            # 重放与账本对不上 → 任何"历史数量"都不可信。整仓 fail closed：
            # **绝不**退回当前余额（那正是规格禁止的 current remaining 冒充历史）。
            diagnostics.append("historical_quantity_unprovable")
            return PositionSellabilityContext(
                code=code_text, account_id=account, cycle_id=cycle,
                decision_session=session, decision_at=resolved_decision_at,
                validation_as_of=asof,
                held_quantity=0, sellable_quantity=0, t1_locked_quantity=0,
                unknown_quantity=0,
                consumed_quantity=sum(max(0, _int(row["qty"]) - _int(row["remaining_qty"]))
                                      for row in raw_lots),
                requested_sell_quantity=requested,
                evidence_status=PositionEvidenceStatus.UNPROVABLE,
                evidence_fingerprint="",
                quantity_basis=QUANTITY_BASIS_UNPROVABLE,
                t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
                diagnostics=tuple(sorted(set(diagnostics))),
            )

        snapshot = replay["snapshot"]

        kept: list = []
        consumed_quantity = 0
        for row in raw_lots:
            lot_id = _int(_row_field(row, "id"))
            historical = _int(snapshot.get(lot_id, 0))
            if historical <= 0:
                # 该 lot 在 decision_at 之前已被完全消耗：它**不在**当时的持仓里，
                # 但这只影响它自己，绝不代表它"从未存在"（诊断桶保留证据）。
                consumed_quantity += max(0, _int(_row_field(row, "qty")) - historical)
                continue
            order_id = _row_field(row, "source_order_id")
            order = orders.get(_int(order_id)) if order_id is not None else None
            fill_rows = fills.get(_int(order_id)) if order_id is not None else None
            lot = self._lot_evidence(
                row, order, fill_rows,
                code=code_text, account_id=account, historical_quantity=historical,
            )
            # 先定 sellability（含"建仓不可证明 → unknown"这条 fail-closed 闸），
            # 再过 PIT 门禁。顺序不能倒：倒了之后"不可证明"会被 PIT 的不可见
            # 顺手盖住，那条守卫就永远执行不到。
            lot = self._classify_lot(lot, decision_session=session)
            # PIT 门禁。**两种不可见都不得丢弃该 lot**：丢弃会让 held_quantity 缩水，
            # 从而把一个"持仓存在但证据不可证明"的仓位粉饰成"可比/可卖"——这正是
            # 本 PR 禁止的退化（"不知道"不能变成"可卖"）。因此保留该 lot 并标
            # ``t1_unknown``，让它只进 unknown_quantity。
            if not self._visible(lot.available_at, resolved_decision_at):
                diagnostics.append("lot_not_visible_at_decision")
                kept.append(dataclasses.replace(
                    lot, sellability=LotSellability.UNKNOWN,
                    sellability_reason="not_visible_at_decision",
                    t1_eval_source=T1EvalSource.UNRESOLVED,
                ))
                continue
            if not self._visible(lot.available_at, asof):
                diagnostics.append("lot_recorded_after_validation_as_of")
                kept.append(dataclasses.replace(
                    lot, sellability=LotSellability.UNKNOWN,
                    sellability_reason="recorded_after_validation_as_of",
                    t1_eval_source=T1EvalSource.UNRESOLVED,
                ))
                continue
            kept.append(lot)

        # 份额口径：一律用**决策时点的历史数量**，绝不用当前余额。
        held = sum(max(0, lot.historical_quantity) for lot in kept)
        sellable = sum(max(0, lot.historical_quantity) for lot in kept
                       if lot.sellability == LotSellability.SELLABLE)
        locked = sum(max(0, lot.historical_quantity) for lot in kept
                     if lot.sellability == LotSellability.BLOCKED)
        unknown = sum(max(0, lot.historical_quantity) for lot in kept
                      if lot.sellability in (LotSellability.UNKNOWN,
                                             LotSellability.NOT_EVALUATED))
        # 不变量：三类之和必须等于 held（任何份额都不能凭空消失）。
        assert sellable + locked + unknown == held, "position quantity invariant violated"

        if not kept:
            status = PositionEvidenceStatus.UNKNOWN
            diagnostics.append("no_open_lot_visible_at_decision")
        elif unknown == 0:
            status = PositionEvidenceStatus.PROVEN
        elif sellable + locked > 0:
            status = PositionEvidenceStatus.PARTIAL
        else:
            status = PositionEvidenceStatus.UNPROVABLE

        if consumed_quantity:
            diagnostics.append("lots_fully_consumed_before_decision")

        quantity_basis = QUANTITY_BASIS_HISTORICAL_REPLAY
        fingerprint = _sha256({
            "version": POSITION_EVIDENCE_FINGERPRINT_VERSION,
            "code": code_text,
            "account_id": account,
            "cycle_id": cycle,
            "decision_session": session,
            "decision_at": resolved_decision_at,
            "validation_as_of": asof,
            "requested_sell_quantity": requested,
            "evidence_status": status,
            "quantity_basis": quantity_basis,
            "lots": [lot.identity() for lot in kept],
            # 判定结论也进指纹：**同一份 lot 证据在不同 decision_session 下的结论不同**，
            # 因此只放 lot 身份不足以区分两个快照。
            "conclusions": [
                [lot.lot_id, lot.sellability, lot.sellability_reason,
                 lot.t1_eval_source]
                for lot in kept
            ],
        })
        return PositionSellabilityContext(
            code=code_text,
            account_id=account,
            cycle_id=cycle,
            decision_session=session,
            decision_at=resolved_decision_at,
            validation_as_of=asof,
            held_quantity=held,
            sellable_quantity=sellable,
            t1_locked_quantity=locked,
            unknown_quantity=unknown,
            consumed_quantity=consumed_quantity,
            requested_sell_quantity=requested,
            evidence_status=status,
            evidence_fingerprint=fingerprint,
            quantity_basis=quantity_basis,
            t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
            acquisition_sessions=tuple(sorted({lot.acquisition_session for lot in kept
                                               if lot.acquisition_session})),
            lots=tuple(kept),
            diagnostics=tuple(sorted(set(diagnostics))),
        )


__all__ = [
    "CYCLE_ATTRIBUTIONS",
    "CYCLE_ATTRIBUTION_AMBIGUOUS",
    "CYCLE_ATTRIBUTION_MISMATCH",
    "CYCLE_ATTRIBUTION_PROVEN",
    "CYCLE_ATTRIBUTION_UNPROVABLE",
    "POSITION_EVIDENCE_VERSION",
    "POSITION_EVIDENCE_FINGERPRINT_VERSION",
    "QUANTITY_BASIS_HISTORICAL_REPLAY",
    "QUANTITY_BASIS_UNPROVABLE",
    "PRE_T1_REASONS",
    "PositionEvidenceError",
    "PositionEvidenceStatus",
    "POSITION_EVIDENCE_STATUSES",
    "AcquisitionStatus",
    "ACQUISITION_PROVEN",
    "LotSellability",
    "T1EvalSource",
    "SellabilityStatus",
    "COMPARABLE_SELLABILITY",
    "NOT_COMPARABLE_SELLABILITY",
    "SELLABILITY_STATUSES",
    "PositionLotEvidence",
    "PositionSellabilityContext",
    "PositionEvidenceAdapter",
]
