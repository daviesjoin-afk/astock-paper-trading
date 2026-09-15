# -*- coding: utf-8 -*-
"""历史选股的**点时可成交性**判定（selection tradability contract）。

本模块只回答一个问题：

    在那个历史时点、那个方向上，这个交易动作能不能真的执行？

它**不**回答"这只股票好不好"。三层必须严格分开::

    selection_score       策略认为股票有多好          （不由本模块决定）
    tradability           该动作在那个时点能不能执行   （本模块）
    evaluation_eligibility 该结果能否作为"真实可执行结果"统计（本模块的消费者）

因此 ``高分 + 不可买`` 仍然是一条**高分 selection**，但它的 execution status
是 ``blocked``；它**不能**被当成 executable winner，也**不能**因此从历史记录里
消失。

──────────────────────── 复用，而不是另造 ────────────────────────

本模块**不**定义任何市场规则。所有规则委托给仓库已有的唯一实现：

=================================  ====================================================
关注点                              权威来源（本模块直接调用）
=================================  ====================================================
涨跌停幅度（按板块 + ST）           :func:`paper_trading_rules.limit_pct`
涨跌停阈值（按板块）                :func:`factors.limit_up_threshold`
ST / 退市判定                       :func:`paper_trading_rules.is_st_or_delisting`
证券权限 / 板块                      :func:`paper_trading_rules.security_scope`
T+0 / T+1 分类                      :func:`paper_trading_rules.asset_type`
T+1 解锁日                          :func:`paper_trading_rules.next_weekday`
价格证据（ok/halted/missing/invalid）:func:`selection_labels.price_point`
证据可见性（PIT）                    :func:`point_in_time.is_visible_at`
=================================  ====================================================

**绝不**写 ``所有股票统一 ±10%`` 这类常量：不同板块（主板 9.5 / 创业板·科创板
19.5 / 北交所 29.5）与 ST（5.0）本来就不同，而上面那个函数就是仓库的口径。

──────────────────────── 能力边界（诚实声明） ────────────────────────

本 contract **能**证明：

* 该 session 有没有合法的价格与成交量证据（以及证据在那个时点是否可见）；
* 该股在决策时是否已被明确标记停牌；
* 按仓库权威口径，该 session 的涨跌幅是否触及**该方向**的涨跌停；
* 该证券是否在账户权限范围内（板块 / ST / 证券类型）；
* 卖出方向是否满足 T+1。

本 contract **不能**证明（仓库只有日线，没有 level2）：

* 盘口排队位置、封单量、盘口深度；
* 逐笔成交与真实冲击成本；
* "涨停封板到底能不能排进去"——只能按权威阈值判**触及**，不能判**必然成交**；
* 真实 retry 后的实际成交 session（生产 TTL 是**盘中** 90/240 分钟，日线无法重放）。

凡是证据不足的，一律 ``unproven``，**绝不**默认 ``tradable``。

──────────────────────── 三条硬门禁 ────────────────────────

1. **PIT**：只读 ``available_at <= action_at`` 的证据。EOD 成交量不能用来证明
   盘中 09:31 可成交。
2. **current state 不得重写历史**：本模块只消费调用方传入的**历史**证据，
   自己不读当前 universe / 当前名称 / 当前 ST / 当前上市状态。
3. **blocked 不得静默顺延**：买不进就是买不进。``actual_*`` 保持 ``None``，
   绝不用"下一根有价格的 K 线"补一个策略从未拥有过的成交。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path (production and ``cd backend`` test runs)
    import point_in_time as PIT
    import paper_trading_rules as PTR
    import selection_labels as SL
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT
    from . import paper_trading_rules as PTR
    from . import selection_labels as SL


TRADABILITY_CONTRACT_VERSION = "selection-tradability-v1"
#: 本 contract 消费的规则集合版本。规则本身来自仓库权威实现，这里只标识
#: "本 contract 用哪一版口径解释它们"。
TRADABILITY_POLICY_VERSION = "market-microstructure-v1"

SIDE_BUY = "buy"
SIDE_SELL = "sell"
SIDES = (SIDE_BUY, SIDE_SELL)

STATUS_EXECUTABLE = "executable"
STATUS_BLOCKED = "blocked"
STATUS_UNPROVEN = "unproven"
STATUS_INVALID = "invalid"
TRADABILITY_STATUSES = (
    STATUS_EXECUTABLE,
    STATUS_BLOCKED,
    STATUS_UNPROVEN,
    STATUS_INVALID,
)

REASON_OK = "ok"
REASON_SUSPENDED = "suspended"
REASON_LIMIT_UP_BUY_BLOCKED = "limit_up_buy_blocked"
REASON_LIMIT_DOWN_SELL_BLOCKED = "limit_down_sell_blocked"
REASON_MISSING_PRICE = "missing_price"
REASON_INVALID_PRICE = "invalid_price"
REASON_MISSING_VOLUME = "missing_volume"
REASON_ZERO_VOLUME = "zero_volume"
REASON_MISSING_REFERENCE_PRICE = "missing_reference_price"
REASON_UNKNOWN_PRICE_LIMIT = "unknown_price_limit"
REASON_UNKNOWN_SUSPENSION_STATUS = "unknown_suspension_status"
REASON_T1_NOT_SELLABLE = "t1_not_sellable"
REASON_UNSUPPORTED_SECURITY_TYPE = "unsupported_security_type"
REASON_EVIDENCE_NOT_VISIBLE = "evidence_not_visible_at_action"
REASON_INVALID_SIDE = "invalid_side"
REASON_INVALID_ACTION_TIME = "invalid_action_time"
REASON_MISSING_CODE = "missing_security_code"
REASON_UNKNOWN_REASON = "unknown"

#: 受控词表：consumer 只读 code，绝不解析自然语言。
TRADABILITY_REASONS = (
    REASON_OK,
    REASON_SUSPENDED,
    REASON_LIMIT_UP_BUY_BLOCKED,
    REASON_LIMIT_DOWN_SELL_BLOCKED,
    REASON_MISSING_PRICE,
    REASON_INVALID_PRICE,
    REASON_MISSING_VOLUME,
    REASON_ZERO_VOLUME,
    REASON_MISSING_REFERENCE_PRICE,
    REASON_UNKNOWN_PRICE_LIMIT,
    REASON_UNKNOWN_SUSPENSION_STATUS,
    REASON_T1_NOT_SELLABLE,
    REASON_UNSUPPORTED_SECURITY_TYPE,
    REASON_EVIDENCE_NOT_VISIBLE,
    REASON_INVALID_SIDE,
    REASON_INVALID_ACTION_TIME,
    REASON_MISSING_CODE,
)

#: ST 证券的涨跌停幅度（仓库口径，见 :func:`paper_trading_rules.limit_pct`）。
ST_LIMIT_PCT = 5.0


class TradabilityContractError(ValueError):
    """契约被违反（不是"数据不够"，而是"结论不可信"）。"""


def _finite(value: Any) -> Optional[float]:
    """有限浮点或 ``None``。**绝不**把缺失/NaN 强转成 ``0``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"", "nan", "nat", "none", "null", "-", "--"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return number if math.isfinite(number) else None


def _instant(value: Any) -> Optional[str]:
    moment = PIT.parse_available_at(value)
    return None if moment is None else moment.isoformat(timespec="seconds")


def session_close_at(session: Any) -> Optional[str]:
    """某个 session 的收盘时点（``15:00`` Asia/Shanghai），即该 bar 的可用时点。

    委托 :func:`point_in_time.bar_available_at` —— 与 #147 判定 label 成熟用的是
    同一条规则，不另立一套。
    """
    try:
        moment = PIT.bar_available_at(session)
    except Exception:  # pragma: no cover - 已归一过的日期必然可解析
        return None
    return None if moment is None else moment.isoformat(timespec="seconds")


# ───────────────────────────── evidence ─────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    """某个 session 上、某个时点可见的**历史**行情证据。

    这是本模块唯一的输入面。调用方负责把它从 PIT 过滤后的数据里构造出来；
    本模块不读任何"当前状态"。

    ``halted``
        显式停牌证据。``True`` = 该 session 该股停牌；``False`` = 明确未停牌；
        ``None`` = **未知**（fail closed：与"没有 bar"一起判 ``unproven``）。
        仓库里"市场有交易日但该股没有 bar"就是停牌（#147 的口径），调用方应传
        ``halted=True``。
    ``name`` / ``risk_flag``
        **该 session 当时的**证券名称与风险警示标记，用于 ST 判定与账户权限。
        缺失不是"非 ST"，而是"ST 状态未知"。
    """

    session: Optional[str] = None
    available_at: Optional[str] = None
    price: Any = None
    reference_price: Any = None
    volume: Any = None
    amount: Any = None
    turnover: Any = None
    halted: Optional[bool] = None
    name: Any = None
    risk_flag: Any = None
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TradabilityVerdict:
    """一次可成交性判定的完整结果。"""

    status: str
    reason: str
    side: str
    action_at: Optional[str]
    security_code: str

    price: Optional[float] = None
    reference_price: Optional[float] = None
    volume: Optional[float] = None

    policy_version: str = TRADABILITY_POLICY_VERSION
    evidence_available_at: Optional[str] = None

    executable: bool = False

    session: Optional[str] = None
    board: Optional[str] = None
    limit_pct: Optional[float] = None
    pct_change: Optional[float] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "side": self.side,
            "action_at": self.action_at,
            "security_code": self.security_code,
            "price": self.price,
            "reference_price": self.reference_price,
            "volume": self.volume,
            "policy_version": self.policy_version,
            "evidence_available_at": self.evidence_available_at,
            "executable": self.executable,
            "session": self.session,
            "board": self.board,
            "limit_pct": self.limit_pct,
            "pct_change": self.pct_change,
            "detail": dict(self.detail or {}),
        }


def _verdict(
    *,
    status: str,
    reason: str,
    side: str,
    code: str,
    action_at: Optional[str],
    evidence: MarketEvidence,
    price: Optional[float] = None,
    reference_price: Optional[float] = None,
    volume: Optional[float] = None,
    board: Optional[str] = None,
    limit_pct: Optional[float] = None,
    pct_change: Optional[float] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> TradabilityVerdict:
    return TradabilityVerdict(
        status=status,
        reason=reason,
        side=side,
        action_at=action_at,
        security_code=code,
        price=price,
        reference_price=reference_price,
        volume=volume,
        evidence_available_at=_instant(evidence.available_at),
        executable=status == STATUS_EXECUTABLE,
        session=evidence.session,
        board=board,
        limit_pct=limit_pct,
        pct_change=pct_change,
        detail=dict(detail or {}),
    )


# ───────────────────────── authoritative rule adapters ─────────────────────────


def board_limit_pct(code: Any) -> Optional[float]:
    """按板块的涨跌停幅度（**不含** ST 覆盖）。委托仓库权威阈值。"""
    if code in (None, ""):
        return None
    return float(PTR.limit_pct(str(code), None, False))


def resolve_limit_pct(
    code: Any,
    *,
    name: Any,
    risk_flag: Any,
    pct_change: Optional[float],
) -> tuple:
    """解出该 session 该股适用的涨跌停幅度。

    返回 ``(limit_pct | None, reason | None)``。

    ST 证据缺失时**不是**默认非 ST。歧义区间是"两个候选幅度给出**不同**结论"的
    那一段，即 ``ST 上限 <= |涨跌幅| < 板块上限``：

    * ``|涨跌幅| < ST 上限``：两个候选都碰不到涨跌停 → 结论与 ST 无关；
    * ``|涨跌幅| >= 板块上限``：两个候选都会被拦 → 结论与 ST 无关；
    * 落在中间：ST 与否会给出相反结论 → ``unknown_price_limit``（fail closed）。
    """
    board_pct = board_limit_pct(code)
    if board_pct is None:
        return None, REASON_MISSING_CODE
    if name is not None or risk_flag is not None:
        is_st = bool(PTR.is_st_or_delisting(name, bool(risk_flag)))
        return (ST_LIMIT_PCT if is_st else board_pct), None
    if pct_change is None:
        return None, REASON_UNKNOWN_PRICE_LIMIT
    magnitude = abs(pct_change)
    if magnitude < ST_LIMIT_PCT / 100.0 or magnitude >= board_pct / 100.0:
        # 无论 ST 与否结论都一样 → ST 状态不影响判定。
        return board_pct, None
    return None, REASON_UNKNOWN_PRICE_LIMIT


def security_permission(code: Any, *, name: Any, risk_flag: Any) -> dict:
    """账户证券权限（板块 / ST / 证券类型）。委托仓库唯一入口。"""
    return dict(PTR.security_scope(str(code or ""), name, bool(risk_flag)))


def earliest_sellable_session(code: Any, *, name: Any, entry_session: Any) -> Optional[str]:
    """入场之后最早可卖的 session（T+1）。委托仓库权威实现。

    普通股票 = 下一个交易日；场内 T+0 ETF = 当日即可卖。
    """
    entry = str(entry_session or "")[:10] or None
    if entry is None:
        return None
    if PTR.asset_type(str(code or ""), name) == "etf_t0":
        return entry
    nxt = PTR.next_weekday(entry)
    return str(nxt)[:10] if nxt else None


# ───────────────────────── the verdict ─────────────────────────


def tradability_at(
    evidence: MarketEvidence,
    *,
    code: Any,
    side: str,
    action_at: Any,
    entry_session: Any = None,
) -> TradabilityVerdict:
    """在 ``action_at`` 这个时点、``side`` 这个方向上判定可成交性。

    判定顺序（先到先判，绝不"降级为可用"）：

    1. 身份与方向合法（code / side / action_at 可解析）。
    2. 账户证券权限（板块 / ST / 证券类型）。
    3. 证据可见性（``available_at <= action_at``，PIT 硬门禁）。
    4. T+1（仅卖出方向，且调用方给了 ``entry_session``）。
    5. 停牌。
    6. 价格证据（ok / halted / missing / invalid）。
    7. 成交量证据（缺失 / 0）。
    8. 涨跌停参考价与幅度。
    9. 该方向的涨跌停是否被触及。
    """
    code_text = str(code or "").strip()
    if not code_text:
        return _verdict(
            status=STATUS_INVALID, reason=REASON_MISSING_CODE, side=str(side or ""),
            code=code_text, action_at=None, evidence=evidence,
        )
    if side not in SIDES:
        return _verdict(
            status=STATUS_INVALID, reason=REASON_INVALID_SIDE, side=str(side or ""),
            code=code_text, action_at=None, evidence=evidence,
        )
    moment = _instant(action_at)
    if moment is None:
        return _verdict(
            status=STATUS_INVALID, reason=REASON_INVALID_ACTION_TIME, side=side,
            code=code_text, action_at=None, evidence=evidence,
        )

    # ── 2. 账户权限（不依赖行情证据，先判） ──
    permission = security_permission(code_text, name=evidence.name, risk_flag=evidence.risk_flag)
    board = str(permission.get("board") or "") or None
    if not permission.get("allowed"):
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_UNSUPPORTED_SECURITY_TYPE, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            detail={"permission": permission.get("reason")},
        )

    # ── 3. PIT：证据必须在动作时点已经可见 ──
    if not bool(PIT.is_visible_at(evidence.available_at, action_at).get("visible")):
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_EVIDENCE_NOT_VISIBLE, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
        )

    # ── 4. T+1（只有卖出方向才有意义） ──
    if side == SIDE_SELL and entry_session is not None:
        earliest = earliest_sellable_session(
            code_text, name=evidence.name, entry_session=entry_session
        )
        action_session = str(evidence.session or moment)[:10]
        if earliest is None or action_session < earliest:
            return _verdict(
                status=STATUS_BLOCKED, reason=REASON_T1_NOT_SELLABLE, side=side,
                code=code_text, action_at=moment, evidence=evidence, board=board,
                detail={"entry_session": str(entry_session)[:10], "earliest_sellable": earliest},
            )

    # ── 5. 停牌 ──
    price_value, price_state = SL.price_point(evidence.price)
    if evidence.halted is True or price_state == SL.PRICE_HALTED:
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_SUSPENDED, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
        )

    # ── 6. 价格证据 ──
    if price_state == SL.PRICE_INVALID:
        return _verdict(
            status=STATUS_INVALID, reason=REASON_INVALID_PRICE, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
        )
    if price_state != SL.PRICE_OK:
        # 没有 bar 且没有显式停牌标记 → 分不清"停牌"还是"数据缺口"，fail closed。
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_UNKNOWN_SUSPENSION_STATUS, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
        )

    # ── 7. 成交量证据 ──
    volume_value = _finite(evidence.volume)
    if volume_value is None:
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_MISSING_VOLUME, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value,
        )
    if volume_value <= 0:
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_ZERO_VOLUME, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value, volume=volume_value,
        )

    # ── 8. 涨跌停参考价与幅度 ──
    reference_value = _finite(evidence.reference_price)
    if reference_value is None or reference_value <= 0:
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_MISSING_REFERENCE_PRICE, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value, volume=volume_value,
        )
    pct_change = price_value / reference_value - 1.0
    limit_pct, limit_reason = resolve_limit_pct(
        code_text, name=evidence.name, risk_flag=evidence.risk_flag, pct_change=pct_change
    )
    if limit_pct is None:
        return _verdict(
            status=STATUS_UNPROVEN, reason=limit_reason or REASON_UNKNOWN_PRICE_LIMIT,
            side=side, code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value, reference_price=reference_value, volume=volume_value,
            pct_change=pct_change,
        )

    # ── 9. 方向性：买入只看涨停，卖出只看跌停 ──
    threshold = limit_pct / 100.0
    if side == SIDE_BUY and pct_change >= threshold:
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_LIMIT_UP_BUY_BLOCKED, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value, reference_price=reference_value, volume=volume_value,
            limit_pct=limit_pct, pct_change=pct_change,
        )
    if side == SIDE_SELL and pct_change <= -threshold:
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_LIMIT_DOWN_SELL_BLOCKED, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value, reference_price=reference_value, volume=volume_value,
            limit_pct=limit_pct, pct_change=pct_change,
        )

    return _verdict(
        status=STATUS_EXECUTABLE, reason=REASON_OK, side=side,
        code=code_text, action_at=moment, evidence=evidence, board=board,
        price=price_value, reference_price=reference_value, volume=volume_value,
        limit_pct=limit_pct, pct_change=pct_change,
    )


def entry_tradability(
    evidence: MarketEvidence, *, code: Any, entry_session: Any = None
) -> TradabilityVerdict:
    """入场（买入方向）。``action_at`` = 目标 entry session 的收盘可用时点。"""
    session = evidence.session or entry_session
    return tradability_at(
        evidence, code=code, side=SIDE_BUY, action_at=session_close_at(session)
    )


def exit_tradability(
    evidence: MarketEvidence,
    *,
    code: Any,
    exit_session: Any = None,
    entry_session: Any = None,
) -> TradabilityVerdict:
    """离场（卖出方向）。``entry_session`` 给了就一并校验 T+1。"""
    session = evidence.session or exit_session
    return tradability_at(
        evidence,
        code=code,
        side=SIDE_SELL,
        action_at=session_close_at(session),
        entry_session=entry_session,
    )


# ───────────────────────── executable outcomes ─────────────────────────


@dataclass(frozen=True, slots=True)
class ExecutableSelectionOutcome:
    """一条选股的**可执行结果**。与 market counterfactual 严格分开。

    ``selected`` 永远保留历史事实：即使 entry 被 blocked，这条记录依然存在，
    只是 ``executable=False``。**不允许**把它过滤掉，否则回头看会像"策略根本
    没选过它"，从而掩盖"策略喜欢追不可成交股票"这个真问题。
    """

    sample_key: str
    security_code: str
    selected: bool

    entry_status: str
    entry_reason: str
    intended_entry_session: Optional[str] = None
    actual_entry_session: Optional[str] = None

    exit_status: Optional[str] = None
    exit_reason: Optional[str] = None
    intended_exit_session: Optional[str] = None
    actual_exit_session: Optional[str] = None

    executable: bool = False

    market_label_status: Optional[str] = None
    market_label_value: Optional[float] = None
    executable_return: Optional[float] = None

    policy_version: str = TRADABILITY_POLICY_VERSION

    def as_dict(self) -> dict:
        return {
            "sample_key": self.sample_key,
            "security_code": self.security_code,
            "selected": self.selected,
            "entry_status": self.entry_status,
            "entry_reason": self.entry_reason,
            "intended_entry_session": self.intended_entry_session,
            "actual_entry_session": self.actual_entry_session,
            "exit_status": self.exit_status,
            "exit_reason": self.exit_reason,
            "intended_exit_session": self.intended_exit_session,
            "actual_exit_session": self.actual_exit_session,
            "executable": self.executable,
            "market_label_status": self.market_label_status,
            "market_label_value": self.market_label_value,
            "executable_return": self.executable_return,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class SelectionRow:
    """一条待判定的选股。``entry_evidence`` / ``exit_evidence`` 必须是 PIT 证据。"""

    sample_key: str
    code: str
    selected: bool = True
    intended_entry_session: Optional[str] = None
    entry_evidence: Optional[MarketEvidence] = None
    intended_exit_session: Optional[str] = None
    exit_evidence: Optional[MarketEvidence] = None
    market_label_status: Optional[str] = None
    market_label_value: Any = None


def build_executable_outcomes(rows: Sequence[SelectionRow]) -> dict:
    """把选股事实 + 历史可成交性合成 executable outcome，并给出完整审计计数。

    关键不变式：

    * ``selected`` 原样保留（selection fact 不因不可成交而消失）；
    * ``market_label_*`` 原样透传（market counterfactual 不被 execution 覆盖）；
    * blocked entry **不**顺延到下一个 session：``actual_entry_session`` 保持
      ``None``，``executable_return`` 保持 ``None``；
    * 每一条输入选股都落在某个 ``reason_counts`` 桶里，不静默丢行。
    """
    outcomes = []
    reason_counts: dict = {}
    report = {
        "contract_version": TRADABILITY_CONTRACT_VERSION,
        "policy_version": TRADABILITY_POLICY_VERSION,
        "input_rows": 0,
        "input_selected": 0,
        "tradability_verified": 0,
        "executable": 0,
        "blocked_entry": 0,
        "blocked_exit": 0,
        "unproven": 0,
        "invalid": 0,
        "not_selected": 0,
        "reason_counts": reason_counts,
        "selection_fact_preserved": True,
        "blocked_entries_carry_no_return": True,
    }

    def _bump(reason: str) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for row in rows or ():
        report["input_rows"] += 1
        if not row.selected:
            report["not_selected"] += 1
            outcomes.append(
                ExecutableSelectionOutcome(
                    sample_key=row.sample_key,
                    security_code=str(row.code or ""),
                    selected=False,
                    entry_status=STATUS_UNPROVEN,
                    entry_reason=REASON_OK,
                    intended_entry_session=row.intended_entry_session,
                    market_label_status=row.market_label_status,
                    market_label_value=_finite(row.market_label_value),
                )
            )
            continue

        report["input_selected"] += 1
        entry_evidence = row.entry_evidence or MarketEvidence(session=row.intended_entry_session)
        entry = entry_tradability(
            entry_evidence, code=row.code, entry_session=row.intended_entry_session
        )
        _bump(entry.reason)

        exit_status = None
        exit_reason = None
        exit_verdict = None
        if row.intended_exit_session is not None or row.exit_evidence is not None:
            exit_evidence = row.exit_evidence or MarketEvidence(
                session=row.intended_exit_session
            )
            exit_verdict = exit_tradability(
                exit_evidence,
                code=row.code,
                exit_session=row.intended_exit_session,
                entry_session=entry_evidence.session or row.intended_entry_session,
            )
            exit_status = exit_verdict.status
            exit_reason = exit_verdict.reason
            _bump(exit_reason)

        entry_ok = entry.status == STATUS_EXECUTABLE
        exit_ok = exit_verdict is None or exit_verdict.status == STATUS_EXECUTABLE
        executable = bool(entry_ok and exit_ok)

        if entry_ok and (exit_verdict is None or exit_ok):
            report["executable"] += 1
        elif not entry_ok:
            if entry.status == STATUS_BLOCKED:
                report["blocked_entry"] += 1
            elif entry.status == STATUS_UNPROVEN:
                report["unproven"] += 1
            else:
                report["invalid"] += 1
        else:
            if exit_verdict.status == STATUS_BLOCKED:
                report["blocked_exit"] += 1
            elif exit_verdict.status == STATUS_UNPROVEN:
                report["unproven"] += 1
            else:
                report["invalid"] += 1

        if entry.status == STATUS_EXECUTABLE and exit_verdict is not None:
            report["tradability_verified"] += 1
        elif entry.status == STATUS_EXECUTABLE:
            report["tradability_verified"] += 1

        outcomes.append(
            ExecutableSelectionOutcome(
                sample_key=row.sample_key,
                security_code=str(row.code or ""),
                selected=True,
                entry_status=entry.status,
                entry_reason=entry.reason,
                intended_entry_session=row.intended_entry_session,
                # blocked / unproven 一律不顺延：没有权威 retry 证据就不编一个成交日。
                actual_entry_session=(
                    entry_evidence.session or row.intended_entry_session
                    if entry.status == STATUS_EXECUTABLE
                    else None
                ),
                exit_status=exit_status,
                exit_reason=exit_reason,
                intended_exit_session=row.intended_exit_session,
                actual_exit_session=(
                    (row.exit_evidence.session if row.exit_evidence else row.intended_exit_session)
                    if executable
                    else None
                ),
                executable=executable,
                market_label_status=row.market_label_status,
                market_label_value=_finite(row.market_label_value),
                # 只有 entry 与 exit 都真正可执行时才有 executable return。
                executable_return=(
                    _finite(row.market_label_value) if executable else None
                ),
            )
        )

    return {"outcomes": outcomes, "report": report}


def audit_totals(report: Mapping[str, Any]) -> dict:
    """把 report 的计数换算成"每条输入都有归属"的校验视图。"""
    total = int(report.get("input_rows", 0))
    accounted = (
        int(report.get("not_selected", 0))
        + int(report.get("executable", 0))
        + int(report.get("blocked_entry", 0))
        + int(report.get("blocked_exit", 0))
        + int(report.get("unproven", 0))
        + int(report.get("invalid", 0))
    )
    return {"input_rows": total, "accounted": accounted, "complete": total == accounted}


#: 评估口径。**默认是 market counterfactual**：接入本 contract 不得静默改变任何
#: 既有 research consumer 的数字，必须由调用方显式选择可执行子集。
MODE_MARKET = "market_counterfactual"
MODE_EXECUTABLE = "executable_only"
EVALUATION_MODES = (MODE_MARKET, MODE_EXECUTABLE)


def evaluation_eligibility(
    outcomes: Sequence[ExecutableSelectionOutcome],
    *,
    mode: str = MODE_MARKET,
) -> dict:
    """按**显式**口径挑出可参与评估的选股。

    ``MODE_MARKET``（默认）
        "如果按标签定义观察未来价格，股票后来怎样" —— 保留全部已选样本，
        含被 blocked 的（它们的 market counterfactual 仍然有分析价值：
        "alpha 有，但执行吃不到"）。
    ``MODE_EXECUTABLE``
        "按历史真实成交约束，能否进入真实可执行评价" —— 只保留 entry 与 exit
        都真正可执行的样本。生产选股质量评估应当显式使用这一档。
    """
    if mode not in EVALUATION_MODES:
        raise TradabilityContractError(f"unknown evaluation mode: {mode!r}")
    selected = [outcome for outcome in outcomes or () if outcome.selected]
    if mode == MODE_EXECUTABLE:
        eligible = [outcome for outcome in selected if outcome.executable]
    else:
        eligible = list(selected)
    return {
        "mode": mode,
        "policy_version": TRADABILITY_POLICY_VERSION,
        "selected": len(selected),
        "eligible": len(eligible),
        "excluded_by_execution": len(selected) - len(eligible),
        "sample_keys": [outcome.sample_key for outcome in eligible],
        "outcomes": eligible,
    }


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    ok = MarketEvidence(
        session="2024-06-18", available_at=session_close_at("2024-06-18"),
        price=10.2, reference_price=10.0, volume=1_000_000.0, halted=False,
    )
    verdict = entry_tradability(ok, code="600001", entry_session="2024-06-18")
    assert verdict.status == STATUS_EXECUTABLE, verdict
    assert verdict.reason == REASON_OK, verdict

    limit_up = MarketEvidence(
        session="2024-06-18", available_at=session_close_at("2024-06-18"),
        price=11.0, reference_price=10.0, volume=1_000_000.0, halted=False,
    )
    buy = entry_tradability(limit_up, code="600001", entry_session="2024-06-18")
    assert buy.status == STATUS_BLOCKED and buy.reason == REASON_LIMIT_UP_BUY_BLOCKED, buy
    sell = exit_tradability(limit_up, code="600001", exit_session="2024-06-18")
    assert sell.status == STATUS_EXECUTABLE, sell

    built = build_executable_outcomes(
        [
            SelectionRow(
                sample_key="a", code="600001", intended_entry_session="2024-06-18",
                entry_evidence=ok, intended_exit_session="2024-06-20",
                exit_evidence=MarketEvidence(
                    session="2024-06-20", available_at=session_close_at("2024-06-20"),
                    price=11.5, reference_price=11.0, volume=900_000.0, halted=False,
                ),
                market_label_status="verified", market_label_value=0.045,
            ),
            SelectionRow(
                sample_key="b", code="600001", intended_entry_session="2024-06-18",
                entry_evidence=limit_up,
                market_label_status="verified", market_label_value=0.25,
            ),
        ]
    )
    totals = audit_totals(built["report"])
    assert totals["complete"], built["report"]
    assert built["report"]["executable"] == 1, built["report"]
    assert built["report"]["blocked_entry"] == 1, built["report"]
    blocked = [o for o in built["outcomes"] if not o.executable][0]
    assert blocked.selected is True and blocked.actual_entry_session is None, blocked
    assert blocked.executable_return is None, blocked
    print("selection_tradability self-check: ok")


if __name__ == "__main__":
    _self_check()
