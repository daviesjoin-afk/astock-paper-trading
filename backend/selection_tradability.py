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
REASON_UNKNOWN_ST_STATUS = "unknown_st_status"
REASON_EVIDENCE_NOT_VISIBLE = "evidence_not_visible_at_action"
REASON_EVIDENCE_SESSION_MISMATCH = "evidence_session_mismatch"
REASON_EXIT_EVIDENCE_MISSING = "exit_evidence_missing"
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
    REASON_UNKNOWN_ST_STATUS,
    REASON_EVIDENCE_NOT_VISIBLE,
    REASON_EVIDENCE_SESSION_MISMATCH,
    REASON_EXIT_EVIDENCE_MISSING,
    REASON_INVALID_SIDE,
    REASON_INVALID_ACTION_TIME,
    REASON_MISSING_CODE,
)

# ST 涨跌停幅度**没有本地常量**：唯一权威来源是
# :func:`paper_trading_rules.limit_pct`（它按板块 + ST 决定）。这里只暴露一个
# 强制 ST 口径的薄包装，供"歧义区间"判定复用，绝不复制那个数字。


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


def normalize_risk_flag(value: Any) -> Optional[bool]:
    """把风险警示标记严格归一成 ``True`` / ``False`` / ``None``。

    委托 :func:`point_in_time.as_strict_bool`。``None`` 表示**无法判定**
    （未声明或未知写法），不是"非 ST"。

    **绝不**用内置 ``bool()``：``bool("false") == True``，一个显式写
    ``"risk_flag": "false"`` 的历史归档会因此被读成 ST（或被误判为非 ST），
    两种方向都是"用错误的状态重写历史结论"。
    """
    return PIT.as_strict_bool(value)


def is_visible_at(available_at: Any, asof: Any) -> bool:
    """PIT 可见性（``available_at <= asof``）。委托 :func:`point_in_time.is_visible_at`。

    暴露成 contract 的一部分，让消费者（报告层、状态源适配器）不必自己重新
    实现可见性比较——那正是容易写出"未来状态重写历史"的地方。
    """
    return bool(PIT.is_visible_at(available_at, asof).get("visible"))


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
        缺失不是"非 ST"，而是"ST 状态未知" → ``unproven / unknown_st_status``。
        仓库里 ``selection_picks`` / ``paper_signals`` 都记了决策当时的 ``name``，
        那是 PIT 正确的历史证据；**当前**快照里的名称不是。
    ``volume_required``
        该证据是否需要成交量才算完整。默认 ``False``：一个收盘价有效的 bar 本身
        就证明当天成交过，成交量是冗余佐证。需要更严的调用方可以显式打开，
        此时缺成交量 → ``unproven / missing_volume``。
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
    volume_required: bool = False
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


def st_limit_pct(code: Any, *, name: Any = None, risk_flag: Any = True) -> Optional[float]:
    """ST 口径的涨跌停幅度。**不写常量**，委托 :func:`paper_trading_rules.limit_pct`。

    传 ``risk_flag=True`` 即强制走 ST 分支，因此返回的是权威实现给出的 ST 上限
    （当前为 5.0）。权威口径若调整，这里自动跟随；本地复制的数字则会静默漂移。
    """
    if code in (None, ""):
        return None
    flag = normalize_risk_flag(risk_flag)
    return float(PTR.limit_pct(str(code), name, flag if flag is not None else True))


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
    flag = normalize_risk_flag(risk_flag)
    if risk_flag is not None and flag is None:
        # 字段存在但写法无法判定（例如 ``"maybe"``）：不能当作非 ST 静默放行。
        return None, REASON_UNKNOWN_PRICE_LIMIT
    if name is not None or flag is not None:
        # ST 与否**以及**对应幅度全部委托 :func:`paper_trading_rules.limit_pct`
        # 这一唯一权威实现：它内部决定 ST 是 5.0、主板 9.5 等。本地复制任何一个
        # 数字（例如 ``ST_LIMIT_PCT``）都会在权威口径变化时静默漂移。
        return float(PTR.limit_pct(str(code), name, flag if flag is not None else False)), None
    if pct_change is None:
        return None, REASON_UNKNOWN_PRICE_LIMIT
    magnitude = abs(pct_change)
    st_pct = st_limit_pct(code, name=None, risk_flag=True)
    if magnitude < st_pct / 100.0 or magnitude >= board_pct / 100.0:
        # 无论 ST 与否结论都一样 → ST 状态不影响判定。
        return board_pct, None
    return None, REASON_UNKNOWN_PRICE_LIMIT


def security_permission(code: Any, *, name: Any, risk_flag: Any) -> dict:
    """账户证券权限（板块 / ST / 证券类型）。委托仓库唯一入口。

    ``risk_flag`` 先严格归一，**绝不**用 ``bool(risk_flag)``（``"false"`` 会变成
    True）。无法判定（``None``）时按保守方向处理 —— 交给
    :func:`paper_trading_rules.security_scope` 决定，并保持"缺证据即 fail closed"
    的上游门禁语义。
    """
    flag = normalize_risk_flag(risk_flag)
    return dict(PTR.security_scope(str(code or ""), name, flag is True))


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
    2. 代码段证券权限（板块 / 证券类型；**不消费证据**）。
    3. 证据可见性（``available_at <= action_at``，PIT 硬门禁）。
       必须先于一切**证据派生**字段：名称、风险标记、停牌、价格、成交量、参考价。
    4. 证据派生证券权限（当时的名称 / 风险标记 → ST 与板块）。
    5. T+1（仅卖出方向，且调用方给了 ``entry_session``）。
    6. 停牌。
    7. 价格证据（ok / halted / missing / invalid）。
    8. 成交量证据（缺失 / 0）。
    9. 涨跌停参考价与幅度。
    10. 该方向的涨跌停是否被触及。
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

    # ── 2. 代码段权限（**不消费任何行情/名称证据**，因此可以先判） ──
    # 板块由**代码段**决定，历史上完全可知。这里只用 code，不碰 name/risk_flag。
    board_scope = dict(PTR.security_scope(code_text, None, False))
    if not board_scope.get("allowed"):
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_UNSUPPORTED_SECURITY_TYPE, side=side,
            code=code_text, action_at=moment, evidence=evidence,
            board=str(board_scope.get("board") or "") or None,
            detail={"permission": board_scope.get("reason"), "basis": "security_code"},
        )

    # ── 3. PIT：证据必须在动作时点已经可见（**先于任何证据派生字段**） ──
    # 顺序本身是契约的一部分：name / risk_flag / halted / price / volume /
    # reference 全是**证据派生**字段，只能在可见性通过之后才允许消费。
    # 否则一条"未来才可见"的 *ST 名称/风险标记就能把更早的动作从
    # unproven/evidence_not_visible_at_action 改写成
    # blocked/unsupported_security_type —— 那就是用未来信息重写历史结论。
    if not bool(PIT.is_visible_at(evidence.available_at, action_at).get("visible")):
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_EVIDENCE_NOT_VISIBLE, side=side,
            code=code_text, action_at=moment, evidence=evidence,
            board=str(board_scope.get("board") or "") or None,
        )

    # ── 4. 证据派生权限：**当时的**名称 / 风险标记（已过 PIT） ──
    # 风险标记先严格归一：``"false"`` / ``"0"`` 必须读成"非 ST"，不能靠
    # ``bool("false") == True`` 把一只正常股票判成 ST（反向亦然）。归一结果为
    # ``None`` 表示**该字段存在但无法判定**（未知写法），同样 fail closed。
    risk_flag_value = normalize_risk_flag(evidence.risk_flag)
    if evidence.risk_flag is not None and risk_flag_value is None:
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_UNKNOWN_ST_STATUS, side=side,
            code=code_text, action_at=moment, evidence=evidence,
            board=str(board_scope.get("board") or "") or None,
        )
    if evidence.name is None and risk_flag_value is None:
        # "不知道当时是不是 ST" 不等于"当时不是 ST"：账户权限无法证明 → fail closed。
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_UNKNOWN_ST_STATUS, side=side,
            code=code_text, action_at=moment, evidence=evidence,
            board=str(board_scope.get("board") or "") or None,
        )
    permission = security_permission(
        code_text, name=evidence.name, risk_flag=risk_flag_value
    )
    board = str(permission.get("board") or "") or None
    if not permission.get("allowed"):
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_UNSUPPORTED_SECURITY_TYPE, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            detail={"permission": permission.get("reason"), "basis": "historical_name"},
        )

    # ── 5. T+1（只有卖出方向才有意义） ──
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

    # ── 6. 停牌 ──
    price_value, price_state = SL.price_point(evidence.price)
    if evidence.halted is True or price_state == SL.PRICE_HALTED:
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_SUSPENDED, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
        )

    # ── 7. 价格证据 ──
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

    # ── 8. 成交量证据 ──
    # 一个收盘价有效的 bar 本身就证明当天成交过；成交量是冗余佐证。只有调用方
    # 显式要求（``volume_required``）时缺失才算证据不足；显式 0 则是矛盾数据 → block。
    volume_value = _finite(evidence.volume)
    if volume_value is None and evidence.volume_required:
        return _verdict(
            status=STATUS_UNPROVEN, reason=REASON_MISSING_VOLUME, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value,
        )
    if volume_value is not None and volume_value <= 0:
        return _verdict(
            status=STATUS_BLOCKED, reason=REASON_ZERO_VOLUME, side=side,
            code=code_text, action_at=moment, evidence=evidence, board=board,
            price=price_value, volume=volume_value,
        )

    # ── 9. 涨跌停参考价与幅度 ──
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

    # ── 10. 方向性：买入只看涨停，卖出只看跌停 ──
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


def _resolve_action_session(
    evidence: MarketEvidence, *, intended_session: Any, side: str, code: str
) -> tuple:
    """动作 session 只能来自**调用方声明的目标 session**，不能由证据自己改。

    若 evidence 带的是**另一个 session** 的 bar，绝不能"就地按它的收盘判定"，
    否则一条目标 entry=06-18 的行配上一根 06-19 的 bar，会被判成可执行并记成
    ``actual_entry_session=06-19`` —— 那正是契约禁止的"静默顺延到下一个 session"。
    返回 ``(session | None, mismatch | None)``。
    """
    intended = str(intended_session)[:10] if intended_session is not None else None
    actual = str(evidence.session)[:10] if evidence.session is not None else None
    if intended is None:
        return actual, None
    if actual is None:
        return intended, None
    if actual != intended:
        return None, {"intended_session": intended, "evidence_session": actual}
    return intended, None


def entry_tradability(
    evidence: MarketEvidence, *, code: Any, entry_session: Any = None
) -> TradabilityVerdict:
    """入场（买入方向）。``action_at`` = 目标 entry session 的收盘可用时点。"""
    session, mismatch = _resolve_action_session(
        evidence, intended_session=entry_session, side=SIDE_BUY, code=str(code or "")
    )
    if mismatch is not None:
        return _verdict(
            status=STATUS_INVALID, reason=REASON_EVIDENCE_SESSION_MISMATCH,
            side=SIDE_BUY, code=str(code or ""),
            action_at=session_close_at(mismatch["intended_session"]), evidence=evidence,
            detail=mismatch,
        )
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
    session, mismatch = _resolve_action_session(
        evidence, intended_session=exit_session, side=SIDE_SELL, code=str(code or "")
    )
    if mismatch is not None:
        return _verdict(
            status=STATUS_INVALID, reason=REASON_EVIDENCE_SESSION_MISMATCH,
            side=SIDE_SELL, code=str(code or ""),
            action_at=session_close_at(mismatch["intended_session"]), evidence=evidence,
            detail=mismatch,
        )
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

    #: **入场侧**是否可执行。它与 :attr:`executable` 不是一回事：一条只建模了
    #: 入场（未提供离场证据）的样本可以 ``entry_executable=True`` 而
    #: ``executable=False`` —— "买得进"不等于"这笔完整交易可执行"。
    entry_executable: bool = False
    #: 入场**与**离场都真正可执行，才算一个完整的 executable outcome。
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
            "entry_executable": self.entry_executable,
            "executable": self.executable,
            "market_label_status": self.market_label_status,
            "market_label_value": self.market_label_value,
            "executable_return": self.executable_return,
            "policy_version": self.policy_version,
        }


# ─────────────────── authoritative outcome buckets ───────────────────
#: outcome 桶的**唯一权威分类**。``build_executable_outcomes`` 与所有 report /
#: 消费者都必须调用 :func:`outcome_bucket`，**不得**各自复制一套优先级。
OUTCOME_BUCKET_EXECUTABLE = "executable"
OUTCOME_BUCKET_BLOCKED_ENTRY = "blocked_entry"
OUTCOME_BUCKET_BLOCKED_EXIT = "blocked_exit"
OUTCOME_BUCKET_UNPROVEN = "unproven"
OUTCOME_BUCKET_INVALID = "invalid"
OUTCOME_BUCKET_NOT_SELECTED = "not_selected"
#: 选股口径的桶（不含 ``not_selected``）。
OUTCOME_BUCKETS = (
    OUTCOME_BUCKET_EXECUTABLE,
    OUTCOME_BUCKET_BLOCKED_ENTRY,
    OUTCOME_BUCKET_BLOCKED_EXIT,
    OUTCOME_BUCKET_UNPROVEN,
    OUTCOME_BUCKET_INVALID,
)
#: 含 ``not_selected`` 的完整键集合（审计计数用）。
OUTCOME_BUCKET_KEYS = (OUTCOME_BUCKET_NOT_SELECTED,) + OUTCOME_BUCKETS


def outcome_bucket(outcome: "ExecutableSelectionOutcome") -> str:
    """一条 outcome 的权威桶。**唯一**实现，优先级与 ``build_executable_outcomes`` 一致。

    判定顺序（先到先判）：

    1. ``selected is False`` → ``not_selected``（不是选股样本，不进选股口径）；
    2. entry 与 exit 都真正可执行 → ``executable``；
    3. entry 未通过 → 按 **entry** 状态判 ``blocked_entry`` / ``unproven`` / ``invalid``；
    4. entry 通过但 exit 未通过 → 按 **exit** 状态判 ``blocked_exit`` / ``unproven`` / ``invalid``。

    第 3 步是关键：``entry=unproven`` 且 ``exit=blocked`` 时桶是 ``unproven``
    —— entry 先失败，exit 的 blocked 不再决定分类。report 层若自行按
    "先看 exit 是否 blocked" 排序，就会得到 ``blocked_exit``，产生与 contract
    不一致的数字。这正是本函数要消灭的语义漂移。
    """
    if not outcome.selected:
        return OUTCOME_BUCKET_NOT_SELECTED
    if outcome.executable:
        return OUTCOME_BUCKET_EXECUTABLE
    if outcome.entry_status != STATUS_EXECUTABLE:
        if outcome.entry_status == STATUS_BLOCKED:
            return OUTCOME_BUCKET_BLOCKED_ENTRY
        if outcome.entry_status == STATUS_UNPROVEN:
            return OUTCOME_BUCKET_UNPROVEN
        return OUTCOME_BUCKET_INVALID
    if outcome.exit_status is None or outcome.exit_status == STATUS_UNPROVEN:
        return OUTCOME_BUCKET_UNPROVEN
    if outcome.exit_status == STATUS_BLOCKED:
        return OUTCOME_BUCKET_BLOCKED_EXIT
    return OUTCOME_BUCKET_INVALID


def outcome_bucket_counts(outcomes: Sequence["ExecutableSelectionOutcome"]) -> dict:
    """按权威 :func:`outcome_bucket` 统计每个桶的样本数（含 ``not_selected``）。"""
    counts = {key: 0 for key in OUTCOME_BUCKET_KEYS}
    for outcome in outcomes or ():
        counts[outcome_bucket(outcome)] += 1
    return counts


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

        # T+1 的基准 session 必须是**已解析出的**入场 session，而不是可能为 None 的
        # ``row.intended_entry_session``：当调用方省略 intended_entry_session 但提供了
        # 带日期的入场证据时（契约显式允许），传 None 会让 ``tradability_at`` 整段跳过
        # T+1，于是"同日买、同日卖"会被判成可执行。优先用权威 verdict 记下的 session，
        # 其次退回入场证据自身的 session。
        entry_session_for_t1 = row.intended_entry_session
        if entry_session_for_t1 is None:
            entry_session_for_t1 = entry.session or entry_evidence.session

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
                entry_session=entry_session_for_t1,
            )
            exit_status = exit_verdict.status
            exit_reason = exit_verdict.reason
        else:
            # 没有离场证据 = 这笔完整交易的可执行性**无法成立**，而不是"离场没问题"。
            # 明确记成 unproven + 机器 reason，而不是留一个 None 让上层当成通过。
            exit_status = STATUS_UNPROVEN
            exit_reason = REASON_EXIT_EVIDENCE_MISSING
        _bump(exit_reason)

        entry_ok = entry.status == STATUS_EXECUTABLE
        # 完整 executable outcome 要求**两侧**都真正可执行：只有入场可执行、
        # 没有离场证据的样本，只是"买得进"，不是一笔可执行交易。
        exit_ok = exit_verdict is not None and exit_verdict.status == STATUS_EXECUTABLE
        executable = bool(entry_ok and exit_ok)

        outcome = ExecutableSelectionOutcome(
            sample_key=row.sample_key,
            security_code=str(row.code or ""),
            selected=True,
            entry_status=entry.status,
            entry_reason=entry.reason,
            intended_entry_session=row.intended_entry_session,
            # blocked / unproven 一律不顺延：没有权威 retry 证据就不编一个成交日。
            actual_entry_session=(
                row.intended_entry_session if entry_ok else None
            ),
            exit_status=exit_status,
            exit_reason=exit_reason,
            intended_exit_session=row.intended_exit_session,
            actual_exit_session=(
                row.intended_exit_session if executable else None
            ),
            entry_executable=entry_ok,
            executable=executable,
            market_label_status=row.market_label_status,
            market_label_value=_finite(row.market_label_value),
            # 只有 entry 与 exit 都真正可执行时才有 executable return。
            executable_return=(
                _finite(row.market_label_value) if executable else None
            ),
        )
        outcomes.append(outcome)
        # 桶分类只有一处实现（``outcome_bucket``）：report 计数**消费**它，
        # 绝不在这里再写一份优先级。否则 entry=unproven + exit=blocked 这类
        # 冲突组合会在两处给出不同的桶。
        report[outcome_bucket(outcome)] += 1

        if entry_ok:
            report["tradability_verified"] += 1

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
        name="某某股份",
    )
    verdict = entry_tradability(ok, code="600001", entry_session="2024-06-18")
    assert verdict.status == STATUS_EXECUTABLE, verdict
    assert verdict.reason == REASON_OK, verdict

    limit_up = MarketEvidence(
        session="2024-06-18", available_at=session_close_at("2024-06-18"),
        price=11.0, reference_price=10.0, volume=1_000_000.0, halted=False,
        name="某某股份",
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
                    name="某某股份",
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
