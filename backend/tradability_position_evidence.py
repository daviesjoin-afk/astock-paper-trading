# -*- coding: utf-8 -*-
"""Position-aware T+1 Shadow Validation —— **只读**仓位证据适配器。

本模块回答的**唯一**问题是：

    在 ``decision_session`` 这一天，某个 (account, code) 的真实持仓里，
    哪些**份额**已被证明可以卖出，哪些被 T+1 锁住，哪些根本无法证明？

它**只读**，且**零 authority**：

* 不写任何表（``INSERT`` / ``UPDATE`` / ``DELETE`` / ``REPLACE`` 一律没有）；
* 不提交 / 撤销 / 修改任何订单；
* 不改变选股、风控、学习行为；
* 它产出的 :class:`PositionSellabilityContext` 只是一份**观察素材**，
  由 :mod:`tradability_position_shadow` 消费，绝不回流到执行链路。

──────────────────────── 为什么需要这一层 ────────────────────────

``backend/tradability_shadow.py`` 比对的是**市场层面**可交易性。它刻意不声明
``entry_session``：市场层面没有"入场时点"这个概念，编一个出来就会造出假的
T+1 分歧（见 ``work/tradability_shadow_validation.py`` 中那段注释）。

真实持仓层面的 A 股 T+1 限制因此一直**没有被观察过**。本层把真实历史
持仓/成交证据接进来补上这一维。

──────────────────────── 复用，而不是重写 ────────────────────────

T+1 规则只有一处权威实现：:mod:`selection_tradability`。本层**逐 lot**调用它：

* ``ST.exit_tradability(evidence, code=..., exit_session=D, entry_session=<lot 的真实建仓 session>)``
  —— 生产卖出方向的真实入口。它内部第 5 步调用 ``earliest_sellable_session``
  （普通股票 → 权威下一个交易日；``etf_t0`` → 当日）。
* 本层只读它的 ``reason``：

  ====================================  ==========================================
  ``reason``                             本层结论
  ====================================  ==========================================
  ``t1_not_sellable``                    ``t1_blocked``（该 lot 的份额被锁）
  发生在 T+1 **之前**的门禁（见下）        ``unknown``（fail closed，**绝不**当可卖）
  其它（T+1 已通过，可能被市场层面拦）      ``t1_sellable``（T+1 这一维不拦）
  ====================================  ==========================================

  本层**没有**自己比较 ``decision_session < entry_session + 1``，也没有自己写
  周末 / 节假日 / ETF T+0 规则 —— 那些字符串判断一次都没有出现。

  发生在 T+1 之前的门禁会让 verdict 停在那里，T+1 根本没被判到。此时把 lot
  当"可卖"就是拿"不知道"冒充"可以卖"，因此归入 ``unknown``：
  ``missing_security_code`` / ``invalid_side`` / ``invalid_action_time`` /
  ``unsupported_security_type`` / ``evidence_not_visible_at_action`` /
  ``unknown_st_status`` / ``evidence_session_mismatch``。

──────────────────────── 真实数据链（本 PR 的审计结论） ────────────────────────

真实 acquisition session **只**来自成交事实，绝不来自订单意图日期：

``paper_orders.created_at``
    委托创建时刻 —— **不是**成交 session（可能当天没成交、次日才成交）。
``paper_orders.executed_at``
    账本自称的执行时刻，仍未与成交流水交叉验证。
``paper_fills.fill_date``
    **真实成交 session**（``execution_evidence`` 的 ``fill_session`` 证据字段
    逐字就是它：``detail="paper_fills.fill_date"``）。本层只用它。
``paper_position_lots.acquired_at``
    系统**记录**该 lot 的时刻，与成交 session 是两件事，PIT 上都要看。

多 lot 是真实存在的：``paper_position_lots`` 按 ``(cycle, account, code)`` 可以
有任意多行，``paper_positions`` 只是 ``(account, code)`` 的聚合视图。
因此**单个 ``entry_session`` 不足以描述一个 position**，本层按 lot 逐条处理。

──────────────────────── PIT ────────────────────────

每条 lot 证据有两个时间：

``effective_at``
    成交事实**生效**的时点 = 该成交 session 的收盘可用时点
    （``ST.session_close_at`` → ``point_in_time.bar_available_at``）。
``recorded_at``
    系统**记录/观察到**它的时点（``paper_position_lots.acquired_at``）。

``available_at`` 取两者之中**更晚**的那个：一条今天才被系统记录下来的、
但号称是三天前成交的证据，不能用来重写三天前的持仓快照。

两个门禁都必须通过：

1. 证据必须在 ``decision_at`` 已可见 —— 否则它不能证明决策当时持有该 lot；
2. 证据必须在 ``validation_as_of`` 已可见（给出时）—— 否则验证者当时并不知道它。

不可见时该 lot **不进**该快照（fail closed），绝不降级为"可见"。

──────────────────────── fail closed ────────────────────────

``position_unknown``
    决策时点没有任何可见的开放 lot —— 没有可观察的持仓（不是"可卖"）。
``position_unprovable``
    有开放 lot，但它们的 acquisition 都无法证明 —— "不知道"不等于"可卖"。
``position_partial``
    部分 lot 可证明，部分不可 —— 未证明的部分只进 ``unknown_quantity``。

禁止的退化：``missing entry_session => assume sellable`` /
``missing entry_session => assume yesterday`` /
``current position created_at => entry session`` /
``order intent date => actual fill date``。

──────────────────────── 已知证据缺口（本 PR 明确登记） ────────────────────────

* ``quantity_basis`` 说明 ``held_quantity`` 的口径：本层用**生产自己的开放
  position 定义**（``paper_position_lots.remaining_qty > 0``，与
  ``paper_trading._position_rows`` 的选择条件一致），而不是"D 当天实际持有多少"。
  已被完全消耗的 lot 是否在 D 当天仍被持有，需要重放生产的 FIFO 消耗规则才能
  回答；本层**不重放、不发明 FIFO**，把它们记在 ``consumed_quantity`` 诊断桶里。
* 因此本层**不**声称 ``held_quantity`` 是"决策当日的精确持仓"；当
  ``decision_session`` 早于账本最后一次变动时，``ledger_not_point_in_time``
  诊断会显式标出这一点。
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


POSITION_EVIDENCE_VERSION = "position-evidence-v1"
POSITION_EVIDENCE_FINGERPRINT_VERSION = "sha256-canonical-position-evidence-v1"

#: ``held_quantity`` 的口径说明（逐字进 DTO，供报告层引用，避免读者过度解读）。
QUANTITY_BASIS_OPEN_LOTS = "production_open_lots_remaining_qty_at_validation"

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
    """适配器输入不合法（例如毫无证据可读的表结构）。"""


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

    #: 生产 verdict 在 T+1 那一步通过（可能随后被市场层面拦，那是市场维）。
    PRODUCTION_PASSED_T1 = "production_exit_tradability_t1_passed"
    #: 生产 verdict 在 T+1 那一步拦下（``t1_not_sellable``）。
    PRODUCTION_T1_BLOCKED = "production_exit_tradability_t1_blocked"
    #: 生产 verdict 停在 T+1 **之前**（如账户证券权限），改由权威
    #: :func:`selection_tradability.earliest_sellable_session` 单独回答 T+1 这一维。
    AUTHORITY_EARLIEST_SELLABLE = "authority_earliest_sellable_session"
    #: 无法向权威提出问题（无证据 / 无 provider / authority 返回 None）→ fail closed。
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
    """一个 acquisition lot 的**只读**证据。

    ``acquisition_session`` 只可能来自 ``paper_fills.fill_date``（真实成交），
    绝不来自订单创建 / 意图 / 信号 / 选股日期。
    """

    code: str
    account_id: Optional[str]
    lot_id: Optional[int]
    acquisition_session: Optional[str]
    quantity: int
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
            self.lot_id,
            self.acquisition_session,
            self.quantity,
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
            "lot_id": self.lot_id,
            "acquisition_session": self.acquisition_session,
            "quantity": self.quantity,
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
    """一个 ``(account, code)`` 在 ``decision_session`` 的仓位可卖性观察。"""

    code: str
    account_id: Optional[str]
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
        （全部开放份额都已证明）才可比。``position_partial`` / ``unknown`` /
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
            "decision_session": self.decision_session,
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


def _invalid_context(code: Any, account_id: Any, decision_session: Any, *,
                     validation_as_of: Any, requested: Any, diagnostic: str
                     ) -> PositionSellabilityContext:
    return PositionSellabilityContext(
        code=str(code or ""),
        account_id=_text(account_id),
        decision_session=_session_of(decision_session),
        decision_at=None,
        validation_as_of=_instant(validation_as_of),
        held_quantity=0, sellable_quantity=0, t1_locked_quantity=0,
        unknown_quantity=0, consumed_quantity=0,
        requested_sell_quantity=None if requested is None else _int(requested),
        evidence_status=PositionEvidenceStatus.INVALID,
        evidence_fingerprint="",
        quantity_basis=QUANTITY_BASIS_OPEN_LOTS,
        t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
        diagnostics=(diagnostic,),
    )


class PositionEvidenceAdapter:
    """``paper_position_lots`` → :class:`PositionSellabilityContext`（只读）。

    唯一入口 :meth:`context_for`。它只做四件事：

    1. 读出该 code 的 lot 行 + 对应的 ``paper_orders`` / ``paper_fills``；
    2. 用 :mod:`execution_verification` 的权威谓词筛出**已验证**的买入成交；
    3. 用 PIT 门禁把不该出现在该快照里的证据剔掉；
    4. 逐 lot 调用 :mod:`selection_tradability` 的卖出方向权威，汇总可卖份额。
    """

    #: lot 行本身的读法（只读；条件与 ``paper_trading._position_rows`` 一致）。
    LOT_SQL = (
        "SELECT id,cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,"
        "acquired_at,available_date,asset_type,source_order_id,is_t_base "
        "FROM paper_position_lots WHERE code=?"
    )

    def __init__(self, conn: sqlite3.Connection, *, evidence_provider: Optional[Callable] = None):
        self._conn = conn
        #: ``(code, session) -> ST.MarketEvidence``。给了才能让 T+1 authority 真的
        #: 判到 T+1 那一步；缺了所有可证明的 lot 一律 ``t1_unknown``（fail closed）。
        self._evidence_provider = evidence_provider

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

    def load_lots(self, code: Any, *, account_id: Any = None,
                  open_only: bool = True) -> list:
        """读出某 code 的 lot 行（可按 account 过滤）。"""
        code_text = _text(code)
        if code_text is None:
            return []
        sql = self.LOT_SQL
        args: list = [code_text]
        if account_id:
            sql += " AND account_id=?"
            args.append(str(account_id))
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

    # ── 判定 ──

    def _visible(self, available_at: Optional[str], asof: Optional[str]) -> bool:
        """PIT 门禁。``asof is None`` = live 兼容模式（契约里可见）。"""
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
                      fill: Any, *, code: str,
                      decision_session: str) -> PositionLotEvidence:
        """把一个 lot 行 + 它的订单/成交证据变成一条 :class:`PositionLotEvidence`。"""
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
                    fill_id = _int(_row_field(fill_rows[0], "id"))
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
            lot_id=lot_id,
            acquisition_session=fill_session,
            quantity=qty,
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
        # 阻断（T+0 ETF 首当其冲：``security_scope`` 先以账户权限拦下它）。
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

    def context_for(self, code: Any, *, account_id: Any = None,
                    decision_session: Any, validation_as_of: Any = None,
                    requested_sell_quantity: Any = None,
                    open_only: bool = True) -> PositionSellabilityContext:
        """构造 ``decision_session`` 这一天的仓位可卖性观察。"""
        code_text = _text(code)
        session = _session_of(decision_session)
        if code_text is None or session is None:
            return _invalid_context(
                code_text, account_id, decision_session,
                validation_as_of=validation_as_of,
                requested=requested_sell_quantity,
                diagnostic="invalid_code_or_decision_session",
            )

        asof = _instant(validation_as_of)
        decision_at = _instant(ST.session_close_at(session))
        if decision_at is None:  # pragma: no cover - 已归一过的 session 必然可解析
            return _invalid_context(
                code_text, account_id, session, validation_as_of=validation_as_of,
                requested=requested_sell_quantity,
                diagnostic="unresolvable_decision_at",
            )

        requested = None if requested_sell_quantity is None else _int(requested_sell_quantity)

        raw_lots = self.load_lots(code_text, account_id=account_id, open_only=False)
        if not raw_lots:
            return PositionSellabilityContext(
                code=code_text, account_id=_text(account_id),
                decision_session=session, decision_at=decision_at,
                validation_as_of=asof,
                held_quantity=0, sellable_quantity=0, t1_locked_quantity=0,
                unknown_quantity=0, consumed_quantity=0,
                requested_sell_quantity=requested,
                evidence_status=PositionEvidenceStatus.UNKNOWN,
                evidence_fingerprint="",
                quantity_basis=QUANTITY_BASIS_OPEN_LOTS,
                t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
                diagnostics=("no_position_lot_evidence",),
            )

        orders = self._orders_for([_row_field(row, "source_order_id") for row in raw_lots])
        order_ids = [_row_field(row, "source_order_id") for row in raw_lots]
        fills = self._buy_fills_for(order_ids)

        diagnostics: list = []
        kept: list = []
        consumed_quantity = 0
        for row in raw_lots:
            order_id = _row_field(row, "source_order_id")
            order = orders.get(_int(order_id)) if order_id is not None else None
            fill_rows = fills.get(_int(order_id)) if order_id is not None else None
            remaining = _int(_row_field(row, "remaining_qty"))
            if remaining <= 0:
                # 已被完全消耗：是否在 decision_session 当天仍被持有，需要重放生产
                # FIFO 消耗规则才能回答 —— 本层不发明 FIFO，只记诊断桶。
                consumed_quantity += max(0, _int(_row_field(row, "qty")) - remaining)
                continue
            lot = self._lot_evidence(
                row, order, fill_rows,
                code=code_text, decision_session=session,
            )
            # 先定 sellability（含"建仓不可证明 → unknown"这条 fail-closed 闸），
            # 再过 PIT 门禁。顺序不能倒：倒了之后"不可证明"会被 PIT 的不可见
            # 顺手盖住，那条守卫就永远执行不到。
            lot = self._classify_lot(lot, decision_session=session)
            # PIT 门禁。**两种不可见都不得丢弃该 lot**：丢弃会让 held_quantity 缩水，
            # 从而把一个"持仓存在但证据不可证明"的仓位粉饰成"可比/可卖"——这正是
            # 本 PR 禁止的退化（"不知道"不能变成"可卖"）。因此保留该 lot 并标
            # ``t1_unknown``，让它只进 unknown_quantity。
            if not self._visible(lot.available_at, decision_at):
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

        held = sum(max(0, lot.remaining_quantity) for lot in kept)
        sellable = sum(max(0, lot.remaining_quantity) for lot in kept
                       if lot.sellability == LotSellability.SELLABLE)
        locked = sum(max(0, lot.remaining_quantity) for lot in kept
                     if lot.sellability == LotSellability.BLOCKED)
        unknown = sum(max(0, lot.remaining_quantity) for lot in kept
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
            diagnostics.append("consumed_lots_excluded_fifo_not_replayed")

        fingerprint = _sha256({
            "version": POSITION_EVIDENCE_FINGERPRINT_VERSION,
            "code": code_text,
            "account_id": _text(account_id),
            "decision_session": session,
            "validation_as_of": asof,
            "requested_sell_quantity": requested,
            "evidence_status": status,
            "quantity_basis": QUANTITY_BASIS_OPEN_LOTS,
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
            account_id=_text(account_id),
            decision_session=session,
            decision_at=decision_at,
            validation_as_of=asof,
            held_quantity=held,
            sellable_quantity=sellable,
            t1_locked_quantity=locked,
            unknown_quantity=unknown,
            consumed_quantity=consumed_quantity,
            requested_sell_quantity=requested,
            evidence_status=status,
            evidence_fingerprint=fingerprint,
            quantity_basis=QUANTITY_BASIS_OPEN_LOTS,
            t1_authority_version=ST.TRADABILITY_POLICY_VERSION,
            acquisition_sessions=tuple(sorted({
                lot.acquisition_session for lot in kept if lot.acquisition_session
            })),
            lots=tuple(kept),
            diagnostics=tuple(diagnostics),
        )


__all__ = [
    "POSITION_EVIDENCE_VERSION",
    "POSITION_EVIDENCE_FINGERPRINT_VERSION",
    "QUANTITY_BASIS_OPEN_LOTS",
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
