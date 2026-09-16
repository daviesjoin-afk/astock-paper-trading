# -*- coding: utf-8 -*-
"""执行真实性证据契约（execution reality evidence contract）。

本模块只回答一个问题：

    这笔委托到底有没有真的成交？成交数量、成交价格、成交时段可不可信？

三层必须严格分开::

    selection_score       策略认为股票有多好             （不由本模块决定）
    tradability           那个历史时点能不能执行          （selection_tradability，PR149）
    execution_evidence    实际有没有成交、成交价是否可信    （本模块）

``selection_tradability`` 回答"理论上可以买"；本模块回答"实际上成交了没有"。
两者**不允许**互相替代：``tradability == executable`` 不等于成交，
``paper_orders.status == 'filled'`` 也**不自动**等于"有成交证据"。

──────────────────────── 三态，而不是 Optional ────────────────────────

每个证据字段都必须显式声明为三种状态之一::

    known             有证据，且值就是 ``value``
    unknown           证据缺失 / 无法重建（**不等于** 0）
    not_applicable    该问题在这笔委托上不存在（**不等于** 0，也不等于 unknown）

硬性禁止（用**构造期校验**强制，而不是靠调用方自觉）::

    None == 0                 未知数量不得当成零
    None == no fill           未知成交不得当成"没成交"
    None == rejected          未知拒单不得当成"被拒绝"
    None == not_applicable

因此 ``known`` **必须**携带非 ``None`` 的值；``unknown`` / ``not_applicable``
**必须不**携带值。两者任一违反即 :class:`ExecutionEvidenceError`。

成交语义由 :func:`fill_verdict` 给出**唯一**的六分类，消费者不得自行推断::

    fill_verified        已证实全部成交
    fill_partial         已证实部分成交
    fill_pending         已提交，快照时点成交量为零（还不是终态结论）
    fill_none_confirmed  已证实没有成交（affirmative zero，**不是** None）
    fill_not_attempted   从未提交过，"成交"问题不存在（**不是** fill_none_confirmed）
    fill_unknown         成交状态未知（例如订单写着 filled 却没有成交流水）

──────────────────────── 能力边界（诚实声明） ────────────────────────

本 contract **能**证明：仓单（``paper_orders``）与成交流水（``paper_fills``）里
真实存在的成交数量、成交价格、成交时段、费用，以及相对计划价的滑点。

本 contract **不能**制造仓库没有的数据：

* 仓库**没有**券商/交易所客户端，也就没有独立受理回报 —— ``ACCEPTED`` 层
  没有证据，谁都不能自称受理过（见 :mod:`execution_lifecycle`）；
* 仓库**没有**按委托落库的"下单时点可用数量"：``available_qty`` 是持仓读模型
  在查询时从 lots 派生的，历史委托**无法重建** —— 卖单缺证据时一律 ``unknown``，
  绝不用当前持仓冒充历史可用数量；
* 仓库**没有**独立的佣金字段（只有合并后的 ``fees``），因此佣金只在能用权威
  费用模型（:mod:`paper_trading_rules`）**逐分对账通过**时才判 ``known``，
  对不上就老实报 ``unknown``；
* 仓库**没有**落库的滑点数值：滑点由 ``filled_price`` 与 ``planned_price``
  派生，两者缺一即 ``unknown``。

凡证据不足，一律 ``unknown``，**绝不**默认成交。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

try:  # ``backend`` on sys.path (production and ``cd backend`` test runs)
    import execution_lifecycle as EL
    import paper_trading_rules as PTR
except ImportError:  # pragma: no cover - package-style import
    from . import execution_lifecycle as EL
    from . import paper_trading_rules as PTR

__all__ = [
    "EVIDENCE_KNOWN",
    "EVIDENCE_NOT_APPLICABLE",
    "EVIDENCE_STATES",
    "EVIDENCE_UNKNOWN",
    "EXECUTION_EVIDENCE_EXTRA_FIELDS",
    "EXECUTION_EVIDENCE_FIELDS",
    "EXECUTION_EVIDENCE_VERSION",
    "FILL_IDENTITY_FIELDS",
    "FILL_VERDICTS",
    "FILL_VERDICT_NONE_CONFIRMED",
    "FILL_VERDICT_NOT_ATTEMPTED",
    "FILL_VERDICT_PARTIAL",
    "FILL_VERDICT_PENDING",
    "FILL_VERDICT_UNKNOWN",
    "FILL_VERDICT_VERIFIED",
    "INCONSISTENCY_FILL_IDENTITY_MISMATCH",
    "KNOWN_EVIDENCE_FIELD_NAMES",
    "RETURN_FIELDS",
    "ExecutionEvidence",
    "ExecutionEvidenceError",
    "EvidenceField",
    "UnknownEvidenceAccess",
    "evidence_from_order",
    "fill_verdict",
    "load_execution_evidence",
    "reconcile_fees",
]

EXECUTION_EVIDENCE_VERSION = "execution-evidence-v1"

EVIDENCE_KNOWN = "known"
EVIDENCE_UNKNOWN = "unknown"
EVIDENCE_NOT_APPLICABLE = "not_applicable"
EVIDENCE_STATES = (EVIDENCE_KNOWN, EVIDENCE_UNKNOWN, EVIDENCE_NOT_APPLICABLE)

#: 契约要求的证据字段（一个都不能少）。
EXECUTION_EVIDENCE_FIELDS = (
    "code",
    "order_time",
    "action",
    "requested_qty",
    "filled_qty",
    "fill_price",
    "fill_session",
    "reject_reason",
    "cancel_reason",
    "available_qty",
    "commission",
    "slippage",
)
#: 契约之外的补充证据。总费用不是佣金，单独存放，避免两者被混为一谈。
EXECUTION_EVIDENCE_EXTRA_FIELDS = ("fees",)
#: 证据字段（ExecutionEvidence 的属性集合）。收益层的三个字段**不在**这里：
#: 收益不是"执行证据"，它由 :mod:`execution_outcome` 从证据派生。
ALL_EVIDENCE_FIELDS = EXECUTION_EVIDENCE_FIELDS + EXECUTION_EVIDENCE_EXTRA_FIELDS

#: 收益层字段名。它们与执行证据共用同一套三态语义（known/unknown/
#: not_applicable），但**不是** :class:`ExecutionEvidence` 的成员。
RETURN_FIELDS = ("market_return", "selection_return", "execution_return")
#: :class:`EvidenceField` 认可的全部字段名。
KNOWN_EVIDENCE_FIELD_NAMES = ALL_EVIDENCE_FIELDS + RETURN_FIELDS

FILL_VERDICT_VERIFIED = "fill_verified"
FILL_VERDICT_PARTIAL = "fill_partial"
FILL_VERDICT_PENDING = "fill_pending"
FILL_VERDICT_NONE_CONFIRMED = "fill_none_confirmed"
FILL_VERDICT_NOT_ATTEMPTED = "fill_not_attempted"
FILL_VERDICT_UNKNOWN = "fill_unknown"
FILL_VERDICTS = (
    FILL_VERDICT_VERIFIED,
    FILL_VERDICT_PARTIAL,
    FILL_VERDICT_PENDING,
    FILL_VERDICT_NONE_CONFIRMED,
    FILL_VERDICT_NOT_ATTEMPTED,
    FILL_VERDICT_UNKNOWN,
)

# 不一致码：机器可读，消费者不得解析自然语言。
INCONSISTENCY_STORED_FILLED_WITHOUT_FILL_ROW = "stored_filled_without_fill_row"
INCONSISTENCY_FILL_EXCEEDS_REQUESTED = "fill_total_exceeds_requested"
INCONSISTENCY_FILL_BELOW_REQUESTED_BUT_FILLED = "fill_below_requested_but_marked_filled"
INCONSISTENCY_SELL_WITHOUT_AVAILABLE_QTY = "sell_without_available_qty_evidence"
INCONSISTENCY_FEES_NOT_RECONCILED = "fees_not_reconciled_with_fee_model"
INCONSISTENCY_PLANNED_PRICE_MISSING = "planned_price_missing"
INCONSISTENCY_REJECTED_WITHOUT_REASON = "rejected_without_reason"
INCONSISTENCY_CANCELLED_WITHOUT_REASON = "cancelled_without_reason"
INCONSISTENCY_UNRECOGNIZED_STATUS = "unrecognized_order_status"
#: 成交流水与委托的身份（账户/方向/标的）不一致。仓库的 ``paper_fills`` 没有外键，
#: 也没有这三项与委托一致的 CHECK，因此历史或手工导入的错行只能在这里挡住。
INCONSISTENCY_FILL_IDENTITY_MISMATCH = "fill_identity_mismatch"

SIDE_BUY = "buy"
SIDE_SELL = "sell"

#: 一条成交流水要归属于某笔委托，必须在这三项上一致。三项都是 ``paper_fills``
#: 的真实列；只有比较过的项才构成证据，缺失项不构成"不一致"。
FILL_IDENTITY_FIELDS = ("account_id", "side", "code")

#: 费用模型对账容差（1 分）。仓库写入的是未取整的浮点费用，容差只为兜住
#: 历史数据里的取整残留；对不上就报 unknown，不强行解释。
FEE_RECONCILE_TOLERANCE = 0.01


class ExecutionEvidenceError(ValueError):
    """执行证据契约被违反。"""


class UnknownEvidenceAccess(ExecutionEvidenceError):
    """试图把一个非 ``known`` 的字段当成具体值使用。"""


@dataclass(frozen=True, slots=True)
class EvidenceField:
    """一个三态证据字段。

    ``__post_init__`` 是本契约的执行点：它让"用 ``None`` 冒充零 / 冒充没成交 /
    冒充被拒绝"在**构造时**就失败，而不是靠下游消费者记得检查 ``is None``。
    """

    name: str
    state: str
    value: Any = None
    source: Optional[str] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.name not in KNOWN_EVIDENCE_FIELD_NAMES:
            raise ExecutionEvidenceError(
                f"unknown evidence field: {self.name!r} "
                f"(expected one of {KNOWN_EVIDENCE_FIELD_NAMES})"
            )
        if self.state not in EVIDENCE_STATES:
            raise ExecutionEvidenceError(
                f"{self.name}: state must be one of {EVIDENCE_STATES}, got {self.state!r}"
            )
        if self.state == EVIDENCE_KNOWN:
            if self.value is None:
                raise ExecutionEvidenceError(
                    f"{self.name}: known evidence must carry a value; None must never be "
                    "recorded as known (None is not zero, not 'no fill', not 'rejected')"
                )
        elif self.value is not None:
            raise ExecutionEvidenceError(
                f"{self.name}: {self.state} evidence must not carry a value "
                f"(got {self.value!r}); use known(...) if the value is real"
            )

    # ── 构造 ──
    @classmethod
    def known(cls, name: str, value: Any, *, source: Optional[str] = None,
              detail: Optional[str] = None) -> "EvidenceField":
        return cls(name=name, state=EVIDENCE_KNOWN, value=value, source=source, detail=detail)

    @classmethod
    def unknown(cls, name: str, *, source: Optional[str] = None,
                detail: Optional[str] = None) -> "EvidenceField":
        return cls(name=name, state=EVIDENCE_UNKNOWN, source=source, detail=detail)

    @classmethod
    def not_applicable(cls, name: str, *, source: Optional[str] = None,
                       detail: Optional[str] = None) -> "EvidenceField":
        return cls(name=name, state=EVIDENCE_NOT_APPLICABLE, source=source, detail=detail)

    # ── 读取 ──
    @property
    def is_known(self) -> bool:
        return self.state == EVIDENCE_KNOWN

    @property
    def is_unknown(self) -> bool:
        return self.state == EVIDENCE_UNKNOWN

    @property
    def is_not_applicable(self) -> bool:
        return self.state == EVIDENCE_NOT_APPLICABLE

    def require(self) -> Any:
        """取已知值；非 ``known`` 一律抛 :class:`UnknownEvidenceAccess`。"""
        if self.state != EVIDENCE_KNOWN:
            raise UnknownEvidenceAccess(
                f"{self.name}: value is {self.state} and must not be read as a concrete value"
                + (f" ({self.detail})" if self.detail else "")
            )
        return self.value

    def maybe(self) -> Optional[Any]:
        """``known`` 时给值，否则给 ``None``。只用于展示/序列化，不用于判定。"""
        return self.value if self.state == EVIDENCE_KNOWN else None

    def fingerprint(self) -> str:
        """状态指纹：用来证明三种状态**互不相同**（unknown ≠ 0 ≠ not_applicable）。"""
        return f"{self.state}:{self.value!r}"

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "value": self.value,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """一笔委托的执行证据。**每个字段**都带状态，不携带隐式 ``None`` 语义。

    字段名必须等于证据字段名（``__post_init__`` 校验），避免复制粘贴把
    ``filled_qty`` 装到 ``requested_qty`` 上。
    """

    code: EvidenceField
    order_time: EvidenceField
    action: EvidenceField
    requested_qty: EvidenceField
    filled_qty: EvidenceField
    fill_price: EvidenceField
    fill_session: EvidenceField
    reject_reason: EvidenceField
    cancel_reason: EvidenceField
    available_qty: EvidenceField
    commission: EvidenceField
    slippage: EvidenceField
    fees: EvidenceField
    order_id: Any = None
    stored_status: str = ""
    lifecycle_state: str = EL.STATE_UNKNOWN
    provenance: Any = field(default_factory=dict)
    version: str = EXECUTION_EVIDENCE_VERSION

    def __post_init__(self) -> None:
        for name in ALL_EVIDENCE_FIELDS:
            holder = getattr(self, name)
            if not isinstance(holder, EvidenceField):
                raise ExecutionEvidenceError(f"{name}: must be an EvidenceField")
            if holder.name != name:
                raise ExecutionEvidenceError(
                    f"{name}: evidence field name mismatch (carrying {holder.name!r})"
                )
        if self.lifecycle_state not in EL.ORDER_STATES:
            raise ExecutionEvidenceError(
                f"unknown lifecycle state: {self.lifecycle_state!r}"
            )

    # ── 读取 ──
    def field(self, name: str) -> EvidenceField:
        if name not in ALL_EVIDENCE_FIELDS:
            raise ExecutionEvidenceError(f"unknown evidence field: {name!r}")
        return getattr(self, name)

    def fill_verdict_value(self) -> str:
        return fill_verdict(self)

    def has_positive_fill(self) -> bool:
        """是否存在**正的**成交数量证据（不判断是否全部成交）。"""
        filled = self.filled_qty
        return bool(filled.is_known and filled.value is not None and filled.value > 0)

    def proves_fill(self) -> bool:
        """是否被证据证明为一次**完整的**成交。这是"execution_verified"的唯一依据。"""
        return self.fill_verdict_value() == FILL_VERDICT_VERIFIED

    def inconsistencies(self) -> tuple:
        """返回机器可读的不一致码；空元组表示没有发现问题。"""
        codes: list = []
        provenance = self.provenance if isinstance(self.provenance, dict) else {}
        fill_rows = int(provenance.get("fill_rows") or 0)
        requested = self.requested_qty.maybe()
        filled = self.filled_qty.maybe()
        planned = _num(provenance.get("planned_price"))

        if self.stored_status == "filled" and fill_rows == 0:
            codes.append(INCONSISTENCY_STORED_FILLED_WITHOUT_FILL_ROW)
        if (
            self.filled_qty.is_known and requested is not None and filled is not None
            and filled > requested
        ):
            codes.append(INCONSISTENCY_FILL_EXCEEDS_REQUESTED)
        if self.lifecycle_state == EL.STATE_FILLED and self.fill_verdict_value() != (
            FILL_VERDICT_VERIFIED
        ):
            codes.append(INCONSISTENCY_FILL_BELOW_REQUESTED_BUT_FILLED)
        if self.action.maybe() == SIDE_SELL and self.available_qty.is_unknown:
            codes.append(INCONSISTENCY_SELL_WITHOUT_AVAILABLE_QTY)
        if self.commission.is_unknown and fill_rows > 0:
            codes.append(INCONSISTENCY_FEES_NOT_RECONCILED)
        if fill_rows > 0 and (planned is None or planned <= 0):
            codes.append(INCONSISTENCY_PLANNED_PRICE_MISSING)
        if self.lifecycle_state == EL.STATE_REJECTED and self.reject_reason.is_unknown:
            codes.append(INCONSISTENCY_REJECTED_WITHOUT_REASON)
        if self.lifecycle_state in (EL.STATE_CANCELLED, EL.STATE_EXPIRED) and (
            self.cancel_reason.is_unknown
        ):
            codes.append(INCONSISTENCY_CANCELLED_WITHOUT_REASON)
        if self.lifecycle_state == EL.STATE_UNKNOWN:
            codes.append(INCONSISTENCY_UNRECOGNIZED_STATUS)
        if provenance.get("fill_identity_mismatches"):
            codes.append(INCONSISTENCY_FILL_IDENTITY_MISMATCH)
        return tuple(codes)

    def as_dict(self) -> dict:
        payload = {
            "version": self.version,
            "order_id": self.order_id,
            "stored_status": self.stored_status,
            "lifecycle_state": self.lifecycle_state,
            "fill_verdict": self.fill_verdict_value(),
            "inconsistencies": list(self.inconsistencies()),
            "provenance": dict(self.provenance) if isinstance(self.provenance, dict) else {},
        }
        for name in ALL_EVIDENCE_FIELDS:
            payload[name] = self.field(name).as_dict()
        return payload

    def fingerprint(self) -> dict:
        """三态指纹：用于断言"未知"与"已知零"**不是**同一件事。"""
        return {name: self.field(name).fingerprint() for name in ALL_EVIDENCE_FIELDS}


def fill_verdict(evidence: Any) -> str:
    """成交判定的**唯一**实现。消费者不得自行复制优先级。

    判定顺序（先到先判）：

    1. ``filled_qty`` 未知 → ``fill_unknown``。"订单写着成交了但查不到流水"
       在这一步现形，**绝不**顺着 stored status 认成交；
    2. ``filled_qty`` 不适用（从未提交）→ ``fill_not_attempted``；
    3. ``filled_qty`` 已知为零 → 按生命周期区分 ``fill_pending`` 与
       ``fill_none_confirmed``（两者都不是 unknown，也都不是彼此的别名）；
    4. 有正成交数量：数量等于目标数量**且**价格与时段都可信才认 ``fill_verified``；
       数量少于（或多于）目标数量一律 ``fill_partial`` / ``fill_unknown``，
       **绝不**把部分成交提升为全部成交。

    "成交数量对得上但价格/时段证据不足"返回 ``fill_unknown`` 而不是
    ``fill_partial``：数量不是问题，可信度才是问题，如实报告。
    """
    filled = evidence.field("filled_qty")
    if filled.is_unknown:
        return FILL_VERDICT_UNKNOWN
    if filled.is_not_applicable:
        return FILL_VERDICT_NOT_ATTEMPTED
    quantity = filled.value
    state = str(evidence.lifecycle_state or "")
    if quantity is None or quantity <= 0:
        if state == EL.STATE_CREATED:
            return FILL_VERDICT_NOT_ATTEMPTED
        if state in (EL.STATE_SUBMITTED, EL.STATE_ACCEPTED):
            return FILL_VERDICT_PENDING
        if state in EL.NO_FILL_STATES:
            return FILL_VERDICT_NONE_CONFIRMED
        return FILL_VERDICT_UNKNOWN
    requested = evidence.field("requested_qty").maybe()
    if requested is None or requested <= 0:
        # 有正成交量、但目标数量不明：能证明"有成交"，不能证明"全部成交"。
        return FILL_VERDICT_PARTIAL
    if quantity > requested:
        # 成交量超过目标数量：证据自相矛盾，不认一次干净的成交。
        return FILL_VERDICT_UNKNOWN
    if quantity < requested:
        return FILL_VERDICT_PARTIAL
    verdict = EL.observed_fill_supported(
        lifecycle_state=state,
        requested_qty=requested,
        filled_qty=quantity,
        fill_price=evidence.field("fill_price").maybe(),
        fill_session=evidence.field("fill_session").maybe(),
    )
    return FILL_VERDICT_VERIFIED if verdict["supported"] else FILL_VERDICT_UNKNOWN


def reconcile_fees(side: Any, amount: Any, fees: Any) -> dict:
    """用仓库权威费用模型逐分对账合并后的 ``fees``，反解出佣金。

    仓库只有合并字段（买入=佣金；卖出=佣金+印花税），所以佣金只有在
    "对得上"时才允许判 ``known``；对不上说明该行用了别的费用口径，
    此时**报 unknown**，绝不猜一个佣金出来。
    """
    normalized = str(side or "").strip().lower()
    total = _num(fees)
    gross = _num(amount)
    if normalized not in (SIDE_BUY, SIDE_SELL):
        return {"reconciled": False, "commission": None, "stamp": None,
                "reason": "unknown side; the fee model is side dependent"}
    if gross is None or gross <= 0:
        return {"reconciled": False, "commission": None, "stamp": None,
                "reason": "gross amount missing or non-positive"}
    if total is None or total < 0:
        return {"reconciled": False, "commission": None, "stamp": None,
                "reason": "stored fees missing or negative"}
    commission = float(PTR.commission(gross))
    stamp = float(gross * PTR.STAMP_SELL) if normalized == SIDE_SELL else 0.0
    expected = commission + stamp
    if abs(expected - total) > FEE_RECONCILE_TOLERANCE:
        return {
            "reconciled": False, "commission": None, "stamp": None,
            "reason": (
                f"stored fees {total!r} do not match the authoritative model "
                f"({expected!r}); the commission cannot be decomposed"
            ),
        }
    return {"reconciled": True, "commission": commission, "stamp": stamp, "reason": None}


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _text(value: Any) -> Optional[str]:
    text = str(value if value is not None else "").strip()
    return text or None


def _get(record: Any, key: str, default: Any = None) -> Any:
    if hasattr(record, "keys") and key in record.keys():
        return record[key]
    if isinstance(record, dict):
        return record.get(key, default)
    return default


def _aggregate_fills(fill_rows: Any) -> dict:
    """汇总成交流水。``qty<=0`` 的行不是成交，单独计数而不是静默丢弃。"""
    total_qty = 0.0
    total_amount = 0.0
    total_fees = 0.0
    sessions: list = []
    usable = 0
    ignored = 0
    for row in fill_rows or ():
        quantity = _num(_get(row, "qty"))
        price = _num(_get(row, "price"))
        if quantity is None or quantity <= 0 or price is None or price <= 0:
            ignored += 1
            continue
        usable += 1
        total_qty += quantity
        amount = _num(_get(row, "amount"))
        total_amount += amount if amount is not None else quantity * price
        fees = _num(_get(row, "fees"))
        total_fees += fees if fees is not None else 0.0
        session = _text(_get(row, "fill_date"))
        if session:
            sessions.append(session)
    weighted = (total_amount / total_qty) if total_qty > 0 else None
    return {
        "fill_rows": usable,
        "ignored_fill_rows": ignored,
        "filled_qty": int(total_qty) if float(total_qty).is_integer() else total_qty,
        "amount": total_amount if usable else None,
        "fees": total_fees if usable else None,
        "weighted_price": weighted,
        "sessions": sessions,
    }


def _text_or_none(value: Any) -> Optional[str]:
    """把值规整成非空文本；空串 / None / 纯空白一律 ``None``（"没有可比对的证据"）。"""
    text = str(value if value is not None else "").strip()
    return text or None


def _fill_identity_mismatches(
    order: Any,
    fill_rows: Any,
    *,
    identity_rows: Any = None,
    identity_known: bool = True,
) -> tuple:
    """成交流水与委托在 ``account_id`` / ``side`` / ``code`` 上的不一致明细。

    仓库的 ``paper_fills`` **没有**外键，也没有"这三项必须与委托一致"的约束，
    而 :func:`load_execution_evidence` 只按 ``order_id`` 关联。于是一条历史错行或
    手工导入行会被当成权威证据，把一笔委托**错误地**验证成成交（甚至用别人的
    标的与方向去算一次"往返收益"）。

    判定口径（只报告，不聚合、不改写）：

    * ``identity_known=False``（按 ``order_id`` 关联的调用方没有读取这三列）时
      返回空元组：**没有检查**不等于**发现了不一致**，这里绝不臆造违规；
    * 委托侧或流水侧任一为空（列缺失 / NULL / 空串）时跳过该项 —— 没有可比对的
      证据就不判违规；
    * 两侧都有值且不相等 → 记一项，含期望值与实际值，供审计定位错行。

    返回 ``((field, expected, actual), ...)``，字段顺序稳定。
    """
    if not identity_known or identity_rows is None:
        return ()
    mismatches: list = []
    for row in identity_rows or ():
        for column in FILL_IDENTITY_FIELDS:
            expected = _text_or_none(_get(order, column))
            actual = _text_or_none(_get(row, column))
            if expected is None or actual is None:
                continue
            if expected != actual:
                mismatches.append((column, expected, actual))
    return tuple(mismatches)


def evidence_from_order(
    order: Any,
    fill_rows: Any = (),
    *,
    available_qty: Any = None,
    available_source: Optional[str] = None,
    source: str = "paper_orders+paper_fills",
    fill_identity_rows: Any = None,
    fill_identity_known: Optional[bool] = None,
) -> ExecutionEvidence:
    """把一行 ``paper_orders``（+ 它的 ``paper_fills``）翻译成执行证据。

    ``available_qty`` 必须由调用方**显式**提供（并注明来源）——仓库无法从历史
    委托重建"下单时点的可用数量"，所以不提供时卖单一律 ``unknown``。

    ``fill_identity_rows`` 是同一批流水的身份列（``account_id`` / ``side`` /
    ``code``），用于核对"这条流水真的属于这笔委托"。**按 order_id 关联**的调用方
    应当传入（见 :func:`load_execution_evidence`）。

    ``fill_identity_known`` 默认由**证据本身**决定：传了身份行就是核对过，没传就是
    没核对。这个默认值刻意做成"没有证据就不能自称核对过"——若它默认 ``True``，
    调用方只要省略 ``fill_identity_rows`` 就能在 ``provenance`` 里留下
    ``fill_identity_checked=True``，而实际**一项都没比对**，"未核对"被读成
    "核对通过"，身份不符的流水照样能把委托验证成成交。
    """
    order = order if order is not None else {}
    aggregated = _aggregate_fills(fill_rows)
    if fill_identity_known is None:
        fill_identity_known = fill_identity_rows is not None
    identity_mismatches = _fill_identity_mismatches(
        order,
        fill_rows,
        identity_rows=fill_identity_rows,
        identity_known=fill_identity_known,
    )
    stored_status = str(_get(order, "status", "") or "")
    #: 身份对不上的流水**不是**这笔委托的证据：一律不聚合、不据以验证成交，
    #: 只作为不一致上报（见 :data:`INCONSISTENCY_FILL_IDENTITY_MISMATCH`）。
    #: 宁可判"没有证据"（unknown），也不能用别人的流水把委托验证成成交。
    excluded_rows = 0
    if identity_mismatches:
        excluded_rows = int(aggregated["fill_rows"])
        aggregated = _aggregate_fills(())
    lifecycle_state = EL.canonical_state(
        stored_status, has_fill=aggregated["fill_rows"] > 0
    )
    side = str(_get(order, "side", "") or "").strip().lower()
    action = side if side in (SIDE_BUY, SIDE_SELL) else None
    created_at = _text(_get(order, "created_at"))
    reason = _text(_get(order, "reason"))
    planned_price = _num(_get(order, "planned_price"))
    requested_qty = _num(_get(order, "qty"))
    requested_qty = int(requested_qty) if requested_qty is not None else None
    if requested_qty is not None and requested_qty <= 0:
        requested_qty = None

    code_value = _text(_get(order, "code"))
    fields = {
        "code": (
            EvidenceField.known("code", code_value, source=source)
            if code_value else
            EvidenceField.unknown("code", source=source, detail="order row carries no code")
        ),
        "order_time": (
            EvidenceField.known("order_time", created_at, source=source)
            if created_at else
            EvidenceField.unknown(
                "order_time", source=source, detail="order row carries no created_at"
            )
        ),
        "action": (
            EvidenceField.known("action", action, source=source)
            if action else
            EvidenceField.unknown(
                "action", source=source, detail=f"unrecognized side {side!r}"
            )
        ),
        "requested_qty": (
            EvidenceField.known("requested_qty", requested_qty, source=source)
            if requested_qty is not None else
            EvidenceField.unknown(
                "requested_qty", source=source, detail="order row carries no positive quantity"
            )
        ),
    }

    fields["filled_qty"] = _filled_qty_field(lifecycle_state, aggregated, requested_qty, source)
    fields["fill_price"] = _fill_price_field(lifecycle_state, aggregated, source)
    fields["fill_session"] = _fill_session_field(lifecycle_state, aggregated, source)
    fields["reject_reason"] = _reason_field(
        "reject_reason", lifecycle_state, {EL.STATE_REJECTED}, reason, source
    )
    fields["cancel_reason"] = _reason_field(
        "cancel_reason", lifecycle_state, {EL.STATE_CANCELLED, EL.STATE_EXPIRED}, reason, source
    )
    fields["available_qty"] = _available_qty_field(action, available_qty, available_source)
    fields["commission"] = _commission_field(
        lifecycle_state, aggregated, requested_qty, action, source
    )
    fields["fees"] = _fees_field(lifecycle_state, aggregated, source)
    fields["slippage"] = _slippage_field(
        lifecycle_state, aggregated, planned_price, action, source
    )

    return ExecutionEvidence(
        order_id=_get(order, "id"),
        stored_status=stored_status,
        lifecycle_state=lifecycle_state,
        provenance={
            "source": source,
            "fill_rows": aggregated["fill_rows"],
            "ignored_fill_rows": aggregated["ignored_fill_rows"],
            "excluded_fill_rows": excluded_rows,
            "fill_identity_mismatches": [
                {"field": field, "expected": expected, "actual": actual}
                for field, expected, actual in identity_mismatches
            ],
            "fill_identity_checked": bool(fill_identity_known),
            "planned_price": planned_price,
            "order_type": _text(_get(order, "order_type")),
            "fill_sessions": aggregated["sessions"],
            "available_qty_source": available_source,
        },
        fees=fields["fees"],
        code=fields["code"],
        order_time=fields["order_time"],
        action=fields["action"],
        requested_qty=fields["requested_qty"],
        filled_qty=fields["filled_qty"],
        fill_price=fields["fill_price"],
        fill_session=fields["fill_session"],
        reject_reason=fields["reject_reason"],
        cancel_reason=fields["cancel_reason"],
        available_qty=fields["available_qty"],
        commission=fields["commission"],
        slippage=fields["slippage"],
    )


def _filled_qty_field(lifecycle_state, aggregated, requested_qty, source) -> EvidenceField:
    if aggregated["fill_rows"] > 0:
        return EvidenceField.known(
            "filled_qty", aggregated["filled_qty"], source=f"{source}:paper_fills",
            detail="aggregated from paper_fills rows",
        )
    if lifecycle_state == EL.STATE_CREATED:
        # 从未提交：成交问题不存在。**不是** known(0)，更不是 unknown。
        return EvidenceField.not_applicable(
            "filled_qty", source=source,
            detail="order was never submitted, so a fill quantity question does not arise",
        )
    if lifecycle_state in EL.NO_FILL_STATES:
        return EvidenceField.known(
            "filled_qty", 0, source=source,
            detail=f"terminal {lifecycle_state} with zero fill rows: an affirmative zero, not None",
        )
    if lifecycle_state == EL.STATE_FILLED:
        # 订单写着成交，但没有任何成交流水：证据缺失，绝不当成"按目标量成交"。
        return EvidenceField.unknown(
            "filled_qty", source=source,
            detail=(
                "stored status claims filled but no paper_fills row exists; "
                "the fill quantity cannot be reconstructed and must not be assumed"
            ),
        )
    if lifecycle_state in (EL.STATE_SUBMITTED, EL.STATE_ACCEPTED):
        return EvidenceField.known(
            "filled_qty", 0, source=source,
            detail="no fill row as of this snapshot while the order is still in flight",
        )
    return EvidenceField.unknown(
        "filled_qty", source=source,
        detail=f"lifecycle state {lifecycle_state} cannot attest any fill quantity",
    )


def _fill_price_field(lifecycle_state, aggregated, source) -> EvidenceField:
    if aggregated["fill_rows"] > 0:
        price = aggregated["weighted_price"]
        if price is not None and price > 0:
            return EvidenceField.known(
                "fill_price", price, source=f"{source}:paper_fills",
                detail="quantity weighted average of paper_fills rows",
            )
        return EvidenceField.unknown(
            "fill_price", source=source, detail="fill rows exist but carry no usable price"
        )
    if lifecycle_state == EL.STATE_FILLED:
        return EvidenceField.unknown(
            "fill_price", source=source,
            detail="stored status claims filled but no fill price evidence exists",
        )
    return EvidenceField.not_applicable(
        "fill_price", source=source,
        detail="no fill has been recorded, so a fill price question does not arise",
    )


def _fill_session_field(lifecycle_state, aggregated, source) -> EvidenceField:
    sessions = aggregated["sessions"]
    if aggregated["fill_rows"] > 0:
        if len(sessions) == aggregated["fill_rows"] and sessions:
            unique = sorted(set(sessions))
            return EvidenceField.known(
                "fill_session", unique[0] if len(unique) == 1 else unique,
                source=f"{source}:paper_fills",
                detail="paper_fills.fill_date",
            )
        return EvidenceField.unknown(
            "fill_session", source=source, detail="fill rows exist without a fill_date"
        )
    if lifecycle_state == EL.STATE_FILLED:
        return EvidenceField.unknown(
            "fill_session", source=source,
            detail="stored status claims filled but no fill session evidence exists",
        )
    return EvidenceField.not_applicable(
        "fill_session", source=source,
        detail="no fill has been recorded, so a fill session question does not arise",
    )


def _reason_field(name, lifecycle_state, applies_to, reason, source) -> EvidenceField:
    if lifecycle_state not in applies_to:
        return EvidenceField.not_applicable(
            name, source=source,
            detail=f"{lifecycle_state} is not a {name.replace('_', ' ')} state",
        )
    if reason:
        return EvidenceField.known(name, reason, source=f"{source}:paper_orders.reason")
    return EvidenceField.unknown(
        name, source=source,
        detail=f"{lifecycle_state} was recorded without a reason text",
    )


def _available_qty_field(action, available_qty, available_source) -> EvidenceField:
    if action is None:
        return EvidenceField.unknown(
            "available_qty", detail="the order side is unknown, so sellability cannot be scoped"
        )
    if action == SIDE_BUY:
        return EvidenceField.not_applicable(
            "available_qty",
            detail="sellable quantity is a sell-side concept and does not apply to a buy order",
        )
    value = _num(available_qty)
    if value is None:
        return EvidenceField.unknown(
            "available_qty",
            source=available_source,
            detail=(
                "the repo does not persist the sellable quantity at order time; it derives it "
                "from live position lots, so a historical order's available quantity is not "
                "reconstructible"
            ),
        )
    return EvidenceField.known(
        "available_qty", int(value), source=available_source or "caller_supplied",
        detail="supplied by the caller with explicit provenance",
    )


def _commission_field(lifecycle_state, aggregated, requested_qty, action, source) -> EvidenceField:
    if aggregated["fill_rows"] == 0:
        if lifecycle_state == EL.STATE_FILLED:
            return EvidenceField.unknown(
                "commission", source=source,
                detail="stored status claims filled but no fee evidence exists",
            )
        return EvidenceField.not_applicable(
            "commission", source=source,
            detail="no fill has been recorded, so no commission was incurred",
        )
    reconciled = reconcile_fees(action, aggregated["amount"], aggregated["fees"])
    if reconciled["reconciled"]:
        return EvidenceField.known(
            "commission", reconciled["commission"],
            source="paper_trading_rules.commission",
            detail="decomposed from paper_fills.fees against the authoritative fee model",
        )
    return EvidenceField.unknown(
        "commission", source=source,
        detail=reconciled["reason"],
    )


def _fees_field(lifecycle_state, aggregated, source) -> EvidenceField:
    if aggregated["fill_rows"] == 0:
        if lifecycle_state == EL.STATE_FILLED:
            return EvidenceField.unknown(
                "fees", source=source,
                detail="stored status claims filled but no fee evidence exists",
            )
        return EvidenceField.not_applicable(
            "fees", source=source, detail="no fill has been recorded, so no fees were incurred"
        )
    return EvidenceField.known(
        "fees", aggregated["fees"], source=f"{source}:paper_fills",
        detail="total fees as stored; commission and stamp tax are not separated in the repo",
    )


def _slippage_field(lifecycle_state, aggregated, planned_price, action, source) -> EvidenceField:
    if aggregated["fill_rows"] == 0:
        return EvidenceField.not_applicable(
            "slippage", source=source, detail="no fill has been recorded, so slippage is undefined"
        )
    fill_price = aggregated["weighted_price"]
    if fill_price is None or planned_price is None or planned_price <= 0 or action is None:
        return EvidenceField.unknown(
            "slippage", source=source,
            detail=(
                "slippage is derived from filled_price vs planned_price; one of them (or the "
                "order side) is missing, so it cannot be computed"
            ),
        )
    raw_bps = (fill_price / planned_price - 1.0) * 10000.0
    adverse_bps = raw_bps if action == SIDE_BUY else -raw_bps
    return EvidenceField.known(
        "slippage", adverse_bps,
        source="derived:filled_price_vs_planned_price",
        detail=(
            "basis points, positive is adverse for both sides; for limit orders this measures "
            "the fill against the limit price rather than against the arrival price"
        ),
    )


def load_execution_evidence(
    conn,
    *,
    account_id: Optional[str] = None,
    limit: int = 500,
    order_ids: Any = None,
) -> list:
    """从真实账本读出执行证据（``paper_orders`` + ``paper_fills``）。

    纯只读；不做任何写操作，也不触碰资金或持仓。
    """
    params: list = []
    where: list = []
    if account_id:
        where.append("o.account_id = ?")
        params.append(str(account_id))
    if order_ids:
        keys = [int(item) for item in order_ids]
        where.append("o.id IN (%s)" % ",".join("?" for _ in keys))
        params.extend(keys)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        """SELECT o.id,o.account_id,o.side,o.code,o.qty,o.planned_price,o.filled_price,
                  o.amount,o.fees,o.status,o.reason,o.created_at,o.executed_at,
                  o.cancelled_at,o.order_type
             FROM paper_orders o"""
        + clause
        + " ORDER BY o.id DESC LIMIT ?",
        (*params, max(1, int(limit))),
    ).fetchall()
    orders = [dict(row) for row in rows]
    if not orders:
        return []
    keys = [int(order["id"]) for order in orders]
    #: 身份列必须与数量/价格一起读出来：``paper_fills`` 没有外键，只按
    #: ``order_id`` 关联会把历史错行或手工导入行当成权威成交证据。
    fill_rows = conn.execute(
        "SELECT order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at "
        "FROM paper_fills "
        "WHERE order_id IN (%s) ORDER BY id" % ",".join("?" for _ in keys),
        tuple(keys),
    ).fetchall()
    grouped: dict = {}
    for row in fill_rows:
        record = dict(row)
        grouped.setdefault(int(record["order_id"]), []).append(record)
    return [
        evidence_from_order(
            order,
            grouped.get(int(order["id"]), ()),
            fill_identity_rows=grouped.get(int(order["id"]), ()),
            fill_identity_known=True,
        )
        for order in orders
    ]
