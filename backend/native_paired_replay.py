# -*- coding: utf-8 -*-
"""原生参数成对回放：一个冻结环境、两侧独立连续状态、单一执行与费用权威。

定位（方案 §3.2 / F1）：回答"同一市场、同一执行假设下，改一个允许的参数会让结果
怎样不同"，而**不**回答"这个参数能赚钱"。

三条硬约束：

1. **不算第二套撮合**。成交价与费用一律来自 ``execution_planner.estimated_fill_price``
   / ``estimate_execution_terms``（其费用权威是 ``paper_trading_rules``）；能不能成交
   一律来自 ``selection_tradability.entry_tradability`` / ``exit_tradability``。
   本模块只负责"把成交意图排好序、把状态往前推一格"。
2. **两侧状态各自连续**。每一侧从期初资本出发，逐会话自己滚动现金、持仓、T+1 可卖量；
   绝不每轮读正式持仓，也绝不每轮归零重算。只有参数不同，其余输入完全相同。
3. **不冒充结论**。``limitations`` 逐条列出本回放**没有**建模的东西；
   ``require_promotion_eligible()`` 永远拒绝 —— 历史回放不产生晋升资格。

为什么不复用 ``shadow_runtime.evaluate_shadow``：那条路径强制要求挑战者带
``dsl_ast``（``shadow_runtime.py:548``），而三套正式策略是**原生**实现。方案 §4.2 明确
"不能直接把 DSL AST 替换器当作原生回放器"，所以这里复用它的可取之处（冻结环境身份、
成对比较、连续状态），不套用它的 DSL 评估。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence

import execution_planner as EP
import paper_trading_rules as PTR
import selection_tradability as TA

REPLAY_VERSION = "native-paired-replay-v2"
"""v2：环境身份纳入因子表内容。旧回执（v1）不能冒充新格式的证据。"""
EVALUATOR_VERSION = "paired-nav-evaluator-v1"
RECEIPT_VERSION = "paired-replay-receipt-v1"

SIDE_CHAMPION = "champion"
SIDE_CHALLENGER = "challenger"

STATUS_OK = "OK"
STATUS_REFUSED = "REFUSED"

R_LIMITED_MODEL = "execution_model_is_deliberately_limited"
R_PROMOTION_NEEDS_LIVE = "historical_replay_cannot_qualify_a_promotion"
R_NO_SESSIONS = "no_frozen_sessions"
R_ENVIRONMENT_NOT_FROZEN = "environment_is_not_frozen"
R_SESSION_NOT_A_TRADING_DAY = "session_is_not_a_trading_day"
R_MISSING_POSITION_QUOTE = "missing_position_quote"

#: 本回放**没有**建模的东西。任何收益读数都必须同时读到这里。
LIMITATIONS = (
    "成交价与费用取自 execution_planner.estimated_fill_price / estimate_execution_terms；"
    "未建模 evaluate_simulated_execution 里的部分成交、1% 量能参与上限与会话时段闸门",
    "退出规则是显式声明的固定持有期（hold_sessions），不是策略真实的退出策略",
    "NAV 按会话冻结价逐日重估，不含盘中路径，也不含停牌期间的估值漂移",
    "两侧共用同一套执行与费用假设，因此差异只反映决策差异，不反映执行差异",
    "历史回放不是新鲜交易证据：不产生晋升资格，也不能替代 5 个不同交易日门槛",
)


class ReplayRefused(ValueError):
    """输入不足以构成一次可比较的回放。"""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _iso_date(value, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ReplayRefused(f"{label}_must_be_iso_date") from exc


def _instant(value, label: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ReplayRefused(f"{label}_must_be_a_timezone_aware_instant") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _positive(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReplayRefused(f"{label}_must_be_numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ReplayRefused(f"{label}_must_be_positive_and_finite")
    return number


@dataclasses.dataclass(frozen=True, slots=True)
class ReplayQuote:
    """一个会话上、单个标的的冻结行情（已 PIT 过滤，不含未来信息）。"""

    code: str
    price: float
    prev_close: float | None = None
    volume: float | None = None
    halted: bool | None = False
    name: str | None = None
    risk_flag: bool = False

    def __post_init__(self):
        code = str(self.code or "").strip()
        if not code:
            raise ReplayRefused("quote_code_required")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "price", _positive(self.price, "quote_price"))
        if self.prev_close is not None:
            object.__setattr__(self, "prev_close", _positive(self.prev_close, "prev_close"))

    def evidence(self, session_date: str):
        return TA.MarketEvidence(
            session=session_date, available_at=f"{session_date}T15:00:00+08:00",
            price=self.price, reference_price=self.prev_close, volume=self.volume,
            halted=self.halted, name=self.name, risk_flag=self.risk_flag,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ReplaySession:
    """左右两侧共享的同一步环境。`factor_table` 由调用方提供且必须已冻结。"""

    session_date: str
    decision_at: str
    quotes: tuple[ReplayQuote, ...]
    factor_table: object = None
    first_board_codes: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "session_date", _iso_date(self.session_date, "session_date"))
        object.__setattr__(self, "decision_at", _instant(self.decision_at, "decision_at"))
        # 休市日不是会话。放进来会虚增持有期、扭曲 T+1 解锁并让"每天一笔"变成假的。
        if not PTR.is_trade_weekday(self.session_date):
            raise ReplayRefused(R_SESSION_NOT_A_TRADING_DAY)
        object.__setattr__(self, "quotes", tuple(self.quotes or ()))
        if not self.quotes:
            raise ReplayRefused("session_requires_at_least_one_quote")
        codes = [quote.code for quote in self.quotes]
        if len(set(codes)) != len(codes):
            # 同一标的两条行情没有确定先后，会让回放身份依赖调用方顺序。
            raise ReplayRefused("session_quotes_must_be_unique_per_code")
        object.__setattr__(self, "first_board_codes",
                           tuple(str(code) for code in self.first_board_codes or ()))

    def quote_map(self) -> dict[str, ReplayQuote]:
        return {quote.code: quote for quote in self.quotes}

    def identity(self) -> dict:
        return {
            "session_date": self.session_date, "decision_at": self.decision_at,
            "quotes": [[q.code, q.price, q.prev_close, q.volume, q.halted, q.name,
                        q.risk_flag] for q in self.quotes],
            "first_board_codes": list(self.first_board_codes),
            # 因子表是真正决定选股顺序的输入，必须进身份：只冻结"表对象"而不管内容，
            # 会让改了 pct 的两份数据共用一个 environment_hash。
            "factor_table": _table_identity(self.factor_table),
        }


def _canon_value(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        return _canon_value(item())
    return str(value)


def _table_identity(table):
    """因子表的规范化身份：列、索引、逐行取值（保持行序，行序会影响平局裁决）。"""
    if table is None:
        return None
    if isinstance(table, Mapping):
        return {"kind": "mapping",
                "value": {str(key): _canon_value(value) for key, value in table.items()}}
    if not (hasattr(table, "columns") and hasattr(table, "index")
            and hasattr(table, "to_dict")):
        raise ReplayRefused("factor_table_not_identifiable")
    columns = [str(name) for name in table.columns]
    rows = []
    for record in table.to_dict(orient="records"):
        rows.append([_canon_value(record.get(name)) for name in table.columns])
    return {"kind": "records", "columns": columns,
            "index": [str(item) for item in table.index],
            "rows": rows}


@dataclasses.dataclass(frozen=True, slots=True)
class ReplayEnvironment:
    """一串按会话升序排列的冻结环境。`fingerprint` 就是方案里的 environment_hash。"""

    sessions: tuple[ReplaySession, ...]

    def __post_init__(self):
        sessions = tuple(self.sessions or ())
        if not sessions:
            raise ReplayRefused(R_NO_SESSIONS)
        dates = [item.session_date for item in sessions]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ReplayRefused(R_ENVIRONMENT_NOT_FROZEN)
        object.__setattr__(self, "sessions", sessions)

    @property
    def environment_hash(self) -> str:
        return fingerprint({"replay_version": REPLAY_VERSION,
                            "sessions": [item.identity() for item in self.sessions]})


@dataclasses.dataclass(frozen=True, slots=True)
class SideParameters:
    """一侧的全部可变输入。除这里之外，两侧没有任何差异。"""

    label: str
    model_family: str
    condition_overrides: tuple[tuple[str, float], ...] = ()
    weight_overrides: tuple[tuple[str, float], ...] | None = None

    def __post_init__(self):
        if self.label not in (SIDE_CHAMPION, SIDE_CHALLENGER):
            raise ReplayRefused(f"unknown_side_label:{self.label}")
        if not str(self.model_family or "").strip():
            raise ReplayRefused("model_family_required")
        object.__setattr__(self, "condition_overrides",
                           tuple((str(k), float(v)) for k, v in self.condition_overrides or ()))
        if self.weight_overrides is not None:
            weights = tuple((str(k), float(v)) for k, v in self.weight_overrides or ())
            # 空覆盖就是"没有覆盖"，不传一个会被下游判成"权重不全"的空 dict。
            object.__setattr__(self, "weight_overrides", weights or None)

    def condition_map(self) -> dict[str, float]:
        return dict(self.condition_overrides)

    def weight_map(self):
        return None if self.weight_overrides is None else dict(self.weight_overrides)

    def identity(self) -> dict:
        return {"label": self.label, "model_family": self.model_family,
                "condition_overrides": dict(self.condition_overrides),
                "weight_overrides": self.weight_map()}


@dataclasses.dataclass(frozen=True, slots=True)
class ReplayRules:
    """回放的显式规则。全部冻结，不接受"默认值"这种隐含输入。"""

    reference_capital: float
    max_positions: int = 5
    hold_sessions: int = 5
    topn: int = 5
    lot_size: int = 100
    ruleset_version: str = EP.SIMULATION_EXECUTION_RULESET

    def __post_init__(self):
        object.__setattr__(self, "reference_capital",
                           _positive(self.reference_capital, "reference_capital"))
        for name in ("max_positions", "hold_sessions", "topn", "lot_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ReplayRefused(f"{name}_must_be_a_positive_integer")
        if not str(self.ruleset_version or "").strip():
            raise ReplayRefused("ruleset_version_required")

    def identity(self) -> dict:
        return {"reference_capital": self.reference_capital,
                "max_positions": self.max_positions, "hold_sessions": self.hold_sessions,
                "topn": self.topn, "lot_size": self.lot_size,
                "ruleset_version": self.ruleset_version}


@dataclasses.dataclass(frozen=True, slots=True)
class Lot:
    code: str
    quantity: int
    entry_session: str
    entry_price: float
    #: 入场时在环境序列里的位置。持有期按会话步数算，不做日期差猜算。
    entry_index: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class SideState:
    """一侧的连续状态。两侧各自持有，永不共享。"""

    cash: float
    lots: tuple[Lot, ...] = ()
    fees_paid: float = 0.0
    realized_pnl: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "lots", tuple(self.lots or ()))
        if self.cash < 0 and abs(self.cash) > 1e-6:
            raise ReplayRefused("cash_cannot_be_negative")

    @classmethod
    def initial(cls, capital: float):
        return cls(cash=float(capital))

    def quantity(self, code: str) -> int:
        return sum(lot.quantity for lot in self.lots if lot.code == code)

    def projection(self) -> dict:
        return {"cash": round(self.cash, 6),
                "lots": [[lot.code, lot.quantity, lot.entry_session, lot.entry_price]
                         for lot in self.lots],
                "fees_paid": round(self.fees_paid, 6),
                "realized_pnl": round(self.realized_pnl, 6)}


@dataclasses.dataclass(frozen=True, slots=True)
class Fill:
    session_date: str
    code: str
    side: str
    quantity: int
    price: float
    amount: float
    fees: float
    reason: str | None = None

    def projection(self) -> dict:
        return {"session_date": self.session_date, "code": self.code, "side": self.side,
                "quantity": self.quantity, "price": self.price,
                "amount": round(self.amount, 6), "fees": round(self.fees, 6),
                "reason": self.reason}


@dataclasses.dataclass(frozen=True, slots=True)
class SideStep:
    side: str
    session_date: str
    cash: float
    nav: float
    positions_value: float
    fills: tuple[Fill, ...]
    blocked: tuple[tuple[str, str], ...]
    fees_paid_cumulative: float
    state: SideState

    def projection(self) -> dict:
        return {"side": self.side, "session_date": self.session_date,
                "cash": round(self.cash, 6), "nav": round(self.nav, 6),
                "positions_value": round(self.positions_value, 6),
                "fills": [fill.projection() for fill in self.fills],
                "blocked": [list(item) for item in self.blocked],
                "fees_paid_cumulative": round(self.fees_paid_cumulative, 6),
                "state": self.state.projection()}


@dataclasses.dataclass(frozen=True, slots=True)
class Receipt:
    """方案 §4.1 的原子回执键。重复请求必须读到同一份回执，不能重新发布结果。"""

    experiment_id: str
    target_identity: str
    environment_hash: str
    transition_sequence: int
    evaluator_version: str
    side: str
    session_date: str
    receipt_fingerprint: str
    receipt_schema_version: str = RECEIPT_VERSION

    @property
    def key(self) -> tuple:
        return (self.experiment_id, self.target_identity, self.environment_hash,
                self.transition_sequence, self.evaluator_version)

    def projection(self) -> dict:
        return {"receipt_schema_version": self.receipt_schema_version,
                "experiment_id": self.experiment_id,
                "target_identity": self.target_identity,
                "environment_hash": self.environment_hash,
                "transition_sequence": self.transition_sequence,
                "evaluator_version": self.evaluator_version,
                "side": self.side, "session_date": self.session_date,
                "receipt_fingerprint": self.receipt_fingerprint}


@dataclasses.dataclass(frozen=True, slots=True)
class PairedReplayResult:
    environment_hash: str
    champion: SideParameters
    challenger: SideParameters
    rules: ReplayRules
    champion_steps: tuple[SideStep, ...]
    challenger_steps: tuple[SideStep, ...]
    receipts: tuple[Receipt, ...]
    invariants: tuple[str, ...]
    limitations: tuple[str, ...]
    fingerprint: str = ""
    replay_version: str = REPLAY_VERSION
    evaluator_version: str = EVALUATOR_VERSION

    def nav_series(self, side: str) -> tuple[float, ...]:
        steps = self.champion_steps if side == SIDE_CHAMPION else self.challenger_steps
        return tuple(step.nav for step in steps)

    def paired_excess_return(self) -> float | None:
        """两侧末值净值差之比。只表示差异，不表示谁"更好"。"""
        champion = self.nav_series(SIDE_CHAMPION)
        challenger = self.nav_series(SIDE_CHALLENGER)
        if not champion or not challenger or champion[-1] <= 0 or challenger[-1] <= 0:
            return None
        return round(challenger[-1] / champion[-1] - 1.0, 10)

    def projection(self) -> dict:
        return {"replay_version": self.replay_version,
                "evaluator_version": self.evaluator_version,
                "fingerprint": self.fingerprint,
                "environment_hash": self.environment_hash,
                "champion": self.champion.identity(),
                "challenger": self.challenger.identity(),
                "rules": self.rules.identity(),
                "champion_steps": [step.projection() for step in self.champion_steps],
                "challenger_steps": [step.projection() for step in self.challenger_steps],
                "receipts": [item.projection() for item in self.receipts],
                "invariants": list(self.invariants),
                "limitations": list(self.limitations),
                "paired_excess_return": self.paired_excess_return()}


# --------------------------------------------------------------------------- #
# 执行与费用：全部委托给既有权威，本模块不重算价格或费率。
# --------------------------------------------------------------------------- #

def _fill_price(reference_price: float, side: str) -> float:
    price = EP.estimated_fill_price(reference_price, side)
    if price is None:
        raise ReplayRefused("fill_price_unavailable")
    return float(price)


def _terms(reference_price: float, quantity: int, side: str):
    price, amount, fees = EP.estimate_execution_terms(reference_price, quantity, side)
    return float(price), float(amount), float(fees)


def _size_quantity(cash: float, price: float, lot_size: int, budget: float) -> int:
    """按可用预算与整手取整。现金不够一手就是 0，绝不透支。"""
    if price <= 0 or budget <= 0:
        return 0
    affordable = min(float(cash), float(budget))
    lots = int(affordable // (price * lot_size))
    return max(0, lots * lot_size)


def _can_buy(session: ReplaySession, quote: ReplayQuote):
    return TA.entry_tradability(quote.evidence(session.session_date), code=quote.code,
                                entry_session=session.session_date)


def _can_sell(session: ReplaySession, quote: ReplayQuote, entry_session: str):
    return TA.exit_tradability(quote.evidence(session.session_date), code=quote.code,
                               exit_session=session.session_date,
                               entry_session=entry_session)


# --------------------------------------------------------------------------- #
# 驱动
# --------------------------------------------------------------------------- #

def run_paired_replay(
    *,
    environment: ReplayEnvironment,
    champion: SideParameters,
    challenger: SideParameters,
    rules: ReplayRules,
    select,
    experiment_id: str,
    target_identity: str,
) -> PairedReplayResult:
    """逐会话推进两侧状态，返回成对结果与逐次回执。

    ``select(model_family, session, condition_overrides, weight_overrides, topn)``
    返回按偏好排序的标的代码序列（真实实现是 ``strategies.run_strategy`` 的适配器）。
    两侧调用同一个 ``select``，只有参数不同。
    """
    if not callable(select):
        raise ReplayRefused("select_callable_required")
    if not str(experiment_id or "").strip() or not str(target_identity or "").strip():
        raise ReplayRefused("experiment_identity_required")

    states = {SIDE_CHAMPION: SideState.initial(rules.reference_capital),
              SIDE_CHALLENGER: SideState.initial(rules.reference_capital)}
    steps: dict[str, list[SideStep]] = {SIDE_CHAMPION: [], SIDE_CHALLENGER: []}
    receipts: list[Receipt] = []
    invariants: list[str] = []
    environment_hash = environment.environment_hash

    for index, session in enumerate(environment.sessions):
        paired = run_paired_session(
            session=session, index=index, champion_state=states[SIDE_CHAMPION],
            challenger_state=states[SIDE_CHALLENGER], champion=champion,
            challenger=challenger, rules=rules, select=select,
            experiment_id=experiment_id, target_identity=target_identity,
            environment_hash=environment_hash)
        steps[SIDE_CHAMPION].append(paired.champion_step)
        steps[SIDE_CHALLENGER].append(paired.challenger_step)
        receipts.extend(paired.receipts)
        invariants.extend(paired.invariants)
        states[SIDE_CHAMPION] = paired.champion_state
        states[SIDE_CHALLENGER] = paired.challenger_state

    _check_receipt_uniqueness(receipts)
    material = {"replay_version": REPLAY_VERSION,
                "evaluator_version": EVALUATOR_VERSION,
                "environment_hash": environment_hash,
                "champion": champion.identity(), "challenger": challenger.identity(),
                "rules": rules.identity(),
                "steps": {label: [step.projection() for step in rows]
                          for label, rows in steps.items()},
                "receipts": [item.receipt_fingerprint for item in receipts]}
    return PairedReplayResult(
        environment_hash=environment_hash, champion=champion, challenger=challenger,
        rules=rules, champion_steps=tuple(steps[SIDE_CHAMPION]),
        challenger_steps=tuple(steps[SIDE_CHALLENGER]), receipts=tuple(receipts),
        invariants=tuple(invariants), limitations=LIMITATIONS,
        fingerprint=fingerprint(material))


def session_payload(session: ReplaySession) -> dict:
    """把一次会话冻结成可持久化的 JSON 形状。

    ``factor_table`` 只有能无损往返的形态才允许持久化：DataFrame 走 records+index，
    ``None`` 走空。其它对象一律拒绝 —— 存不下来的输入不能假装"已冻结"。
    """
    table = session.factor_table
    if table is None:
        table_payload = None
    elif isinstance(table, Mapping):
        table_payload = {"kind": "mapping",
                         "value": {str(key): _canon_value(value)
                                   for key, value in table.items()}}
    elif hasattr(table, "to_dict") and hasattr(table, "index"):
        table_payload = {"kind": "records", "records": table.to_dict(orient="records"),
                         "index": [str(item) for item in table.index]}
    else:
        raise ReplayRefused("factor_table_not_serializable")
    return {
        "session_date": session.session_date, "decision_at": session.decision_at,
        "quotes": [{"code": q.code, "price": q.price, "prev_close": q.prev_close,
                    "volume": q.volume, "halted": q.halted, "name": q.name,
                    "risk_flag": q.risk_flag} for q in session.quotes],
        "first_board_codes": list(session.first_board_codes),
        "factor_table": table_payload,
    }


def session_from_payload(payload: Mapping) -> ReplaySession:
    """从持久化形状重建会话。往返必须逐位一致，否则回放身份会漂移。"""
    import pandas as pd

    table = payload.get("factor_table")
    if table is None:
        factor_table = None
    elif isinstance(table, Mapping) and table.get("kind") == "mapping":
        factor_table = dict(table.get("value") or {})
    elif isinstance(table, Mapping) and table.get("kind") == "records":
        factor_table = pd.DataFrame(list(table.get("records") or ()),
                                    index=[str(item) for item in table.get("index") or ()])
    else:
        raise ReplayRefused("factor_table_payload_unsupported")
    return ReplaySession(
        session_date=payload["session_date"], decision_at=payload["decision_at"],
        quotes=tuple(ReplayQuote(code=row["code"], price=row["price"],
                                  prev_close=row.get("prev_close"),
                                  volume=row.get("volume"), halted=row.get("halted"),
                                  name=row.get("name"), risk_flag=row.get("risk_flag"))
                     for row in payload.get("quotes") or ()),
        first_board_codes=tuple(payload.get("first_board_codes") or ()),
        factor_table=factor_table,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class SessionPair:
    """一个会话的成对推进结果。增量循环与整段回放共用这一份产出。"""

    session_date: str
    champion_step: SideStep
    challenger_step: SideStep
    champion_state: SideState
    challenger_state: SideState
    receipts: tuple[Receipt, Receipt]
    invariants: tuple[str, ...] = ()

    def projection(self) -> dict:
        return {"session_date": self.session_date,
                "champion": self.champion_step.projection(),
                "challenger": self.challenger_step.projection(),
                "receipts": [item.projection() for item in self.receipts],
                "invariants": list(self.invariants)}


def run_paired_session(
    *, session: ReplaySession, index: int,
    champion_state: SideState, challenger_state: SideState,
    champion: SideParameters, challenger: SideParameters,
    rules: ReplayRules, select,
    experiment_id: str, target_identity: str, environment_hash: str,
) -> SessionPair:
    """把一个会话同时推进两侧，产出成对 step、后继状态与一张回执对。

    增量执行与整段回放走的是**同一个**函数：所以"分多次跑完"与"一次跑完"必须发布
    逐位相同的回执，不存在两套语义。
    """
    if not callable(select):
        raise ReplayRefused("select_callable_required")
    receipts: list[Receipt] = []
    steps: dict[str, SideStep] = {}
    states: dict[str, SideState] = {}
    for label, params, state in (
            (SIDE_CHAMPION, champion, champion_state),
            (SIDE_CHALLENGER, challenger, challenger_state)):
        next_state, step = _advance_side(state, side_params=params, session=session,
                                         index=index, rules=rules, select=select)
        steps[label] = step
        states[label] = next_state
        sequence = _sequence_for(index, label)
        receipts.append(Receipt(
            experiment_id=experiment_id, target_identity=target_identity,
            environment_hash=environment_hash, transition_sequence=sequence,
            evaluator_version=EVALUATOR_VERSION, side=label,
            session_date=session.session_date,
            receipt_fingerprint=_receipt_material(
                experiment_id, target_identity, environment_hash, sequence, label,
                session.session_date, step)))
    invariants = _check_invariants(SIDE_CHAMPION, session.session_date,
                                   states[SIDE_CHAMPION], rules,
                                   steps[SIDE_CHAMPION].nav,
                                   steps[SIDE_CHAMPION].positions_value)
    invariants += _check_invariants(SIDE_CHALLENGER, session.session_date,
                                    states[SIDE_CHALLENGER], rules,
                                    steps[SIDE_CHALLENGER].nav,
                                    steps[SIDE_CHALLENGER].positions_value)
    return SessionPair(
        session_date=session.session_date, champion_step=steps[SIDE_CHAMPION],
        challenger_step=steps[SIDE_CHALLENGER],
        champion_state=states[SIDE_CHAMPION], challenger_state=states[SIDE_CHALLENGER],
        receipts=(receipts[0], receipts[1]), invariants=tuple(invariants))


def _sequence_for(index: int, label: str) -> int:
    """每个会话固定两格：冠军在前、挑战者在后。增量与整段必须给出同一序号。"""
    return int(index) * 2 + (1 if label == SIDE_CHAMPION else 2)


def _receipt_material(experiment_id: str, target_identity: str, environment_hash: str,
                      sequence: int, label: str, session_date: str,
                      step: SideStep) -> str:
    return fingerprint({"experiment_id": experiment_id,
                        "target_identity": target_identity,
                        "environment_hash": environment_hash,
                        "transition_sequence": sequence,
                        "evaluator_version": EVALUATOR_VERSION,
                        "side": label, "session_date": session_date,
                        "fills": [fill.projection() for fill in step.fills],
                        "state": step.state.projection()})


def _advance_side(state: SideState, *, side_params: SideParameters,
                  session: ReplaySession, index: int, rules: ReplayRules, select):
    quotes = session.quote_map()
    fills: list[Fill] = []
    blocked: list[tuple[str, str]] = []

    state, sells, sell_blocked = _sell_phase(state, session, quotes, rules, index)
    fills.extend(sells)
    blocked.extend(sell_blocked)

    ranked = select(side_params.model_family, session,
                    side_params.condition_map(), side_params.weight_map(), rules.topn)
    state, buys, buy_blocked = _buy_phase(state, session, quotes, rules, ranked, index)
    fills.extend(buys)
    blocked.extend(buy_blocked)

    positions_value = sum(lot.quantity * quotes[lot.code].price
                          for lot in state.lots if lot.code in quotes)
    # 缺报价的持仓留在账上却不进市值，会凭空造出一笔亏损（报价恢复后再造一笔上涨）。
    # 这里直接拒绝发布该会话：宁可等数据，也不给一个残缺的估值。
    unquoted = sorted({lot.code for lot in state.lots if lot.code not in quotes})
    if unquoted:
        raise ReplayRefused(f"{R_MISSING_POSITION_QUOTE}:{','.join(unquoted)}")
    nav = state.cash + positions_value
    step = SideStep(
        side=side_params.label, session_date=session.session_date, cash=state.cash,
        nav=nav, positions_value=positions_value, fills=tuple(fills),
        blocked=tuple(blocked), fees_paid_cumulative=state.fees_paid, state=state)
    return state, step


def _sell_phase(state: SideState, session: ReplaySession, quotes: Mapping[str, ReplayQuote],
                rules: ReplayRules, index: int):
    fills: list[Fill] = []
    blocked: list[tuple[str, str]] = []
    remaining_lots: list[Lot] = []
    for lot in state.lots:
        if index - lot.entry_index < rules.hold_sessions:
            remaining_lots.append(lot)
            continue
        quote = quotes.get(lot.code)
        if quote is None:
            blocked.append((lot.code, "quote_missing"))
            remaining_lots.append(lot)
            continue
        verdict = _can_sell(session, quote, lot.entry_session)
        if not verdict.executable:
            # 卖不出去不是"不用卖"：持仓照旧保留，并记录不可成交原因。
            blocked.append((lot.code, verdict.reason))
            remaining_lots.append(lot)
            continue
        price, amount, fees = _terms(quote.price, lot.quantity, "sell")
        proceeds = amount - fees
        realized = proceeds - lot.quantity * lot.entry_price
        state = dataclasses.replace(
            state, cash=state.cash + proceeds, fees_paid=state.fees_paid + fees,
            realized_pnl=state.realized_pnl + realized)
        fills.append(Fill(session_date=session.session_date, code=lot.code, side="sell",
                          quantity=lot.quantity, price=price, amount=amount, fees=fees,
                          reason="hold_horizon_reached"))
    return dataclasses.replace(state, lots=tuple(remaining_lots)), fills, blocked


def _buy_phase(state: SideState, session: ReplaySession, quotes: Mapping[str, ReplayQuote],
               rules: ReplayRules, ranked: Sequence[str], index: int):
    fills: list[Fill] = []
    blocked: list[tuple[str, str]] = []
    held_codes = {lot.code for lot in state.lots}
    slots = max(0, rules.max_positions - len(held_codes))
    if slots <= 0:
        return state, fills, blocked
    per_position_budget = rules.reference_capital / rules.max_positions
    for code in list(ranked or ()):
        if slots <= 0:
            break
        if code in held_codes:
            continue
        quote = quotes.get(code)
        if quote is None:
            blocked.append((code, "quote_missing"))
            continue
        verdict = _can_buy(session, quote)
        if not verdict.executable:
            blocked.append((code, verdict.reason))
            continue
        price = _fill_price(quote.price, "buy")
        quantity = _size_quantity(state.cash, price, rules.lot_size, per_position_budget)
        if quantity <= 0:
            blocked.append((code, "insufficient_cash_for_a_lot"))
            continue
        price, amount, fees = _terms(quote.price, quantity, "buy")
        if amount + fees > state.cash + 1e-9:
            blocked.append((code, "insufficient_cash_after_fees"))
            continue
        state = dataclasses.replace(
            state, cash=state.cash - amount - fees, fees_paid=state.fees_paid + fees,
            lots=state.lots + (Lot(code=code, quantity=quantity,
                                   entry_session=session.session_date,
                                   entry_price=price, entry_index=index),))
        held_codes.add(code)
        slots -= 1
        fills.append(Fill(session_date=session.session_date, code=code, side="buy",
                          quantity=quantity, price=price, amount=amount, fees=fees,
                          reason="selected"))
    return state, fills, blocked


def _check_invariants(label: str, session_date: str, state: SideState,
                      rules: ReplayRules, nav: float, positions_value: float) -> list[str]:
    """每一步都核账。资金不守恒就是回放实现错误，不能"大概对"。"""
    problems = []
    if state.cash < -1e-6:
        problems.append(f"{label}@{session_date}:cash_negative")
    if abs(nav - (state.cash + positions_value)) > 1e-6:
        problems.append(f"{label}@{session_date}:nav_mismatch")
    for lot in state.lots:
        if lot.quantity <= 0 or lot.quantity % rules.lot_size:
            problems.append(f"{label}@{session_date}:lot_not_whole_lot:{lot.code}")
        if lot.entry_price <= 0:
            problems.append(f"{label}@{session_date}:lot_price_invalid:{lot.code}")
    if len({lot.code for lot in state.lots}) > rules.max_positions:
        problems.append(f"{label}@{session_date}:too_many_positions")
    if problems:
        raise ReplayRefused(";".join(problems))
    return []


def _check_receipt_uniqueness(receipts: Iterable[Receipt]) -> None:
    keys = [item.key for item in receipts]
    if len(set(keys)) != len(keys):
        raise ReplayRefused("duplicate_receipt_key")


def require_promotion_eligible() -> None:
    """历史回放永远不产生晋升资格（方案 §3.4）。这里永久拒绝。"""
    raise ReplayRefused(R_PROMOTION_NEEDS_LIVE)


def sides_from_manifest(strategy_id: str, *, target_kind: str, model_family: str,
                        variant_fields: Mapping[str, float],
                        champion_fields: Mapping[str, float] | None = None,
                        ) -> tuple[SideParameters, SideParameters]:
    """按实验清单构造两侧参数。

    清单**管住**回放能改什么：未声明字段整条拒绝，不支持的策略直接拒绝，
    变体必须真的与基线不同（否则这是控制组，不是实验）。
    """
    import experiment_target_manifest as ETM
    import strategy_naming as NAMING

    manifest = ETM.manifest_for(strategy_id, target_kind, model_family)
    if not manifest.supported:
        raise ReplayRefused(f"unsupported_target:{manifest.unsupported_reason}")
    changed = dict(variant_fields or {})
    if not changed:
        raise ReplayRefused("variant_must_change_at_least_one_allowed_field")
    champion_overrides = dict(champion_fields or {})
    try:
        ETM.check_fields_allowed(manifest, tuple(changed))
        if champion_overrides:
            ETM.check_fields_allowed(manifest, tuple(champion_overrides))
    except ETM.ManifestError as exc:
        raise ReplayRefused(str(exc)) from exc
    defaults = dict(manifest.baseline_defaults)
    for name, value in changed.items():
        baseline = champion_overrides.get(name, defaults.get(name))
        if baseline is not None and abs(float(baseline) - float(value)) < 1e-12:
            raise ReplayRefused(f"variant_does_not_change:{name}")
    # 挑战侧保留冠军侧的其它覆盖，只把变体字段叠上去 —— 否则"只改一个字段"会顺手
    # 悄悄丢掉冠军侧其它显式设置，两侧差异就不止一处了。
    challenger_overrides = {**champion_overrides, **changed}
    canonical = NAMING.canonical_model_family(model_family)
    if target_kind == ETM.TARGET_SELECTION_WEIGHTS:
        return (
            SideParameters(label=SIDE_CHAMPION, model_family=canonical,
                           weight_overrides=tuple(sorted(champion_overrides.items()))),
            SideParameters(label=SIDE_CHALLENGER, model_family=canonical,
                           weight_overrides=tuple(sorted(challenger_overrides.items()))),
        )
    return (
        SideParameters(label=SIDE_CHAMPION, model_family=canonical,
                       condition_overrides=tuple(sorted(champion_overrides.items()))),
        SideParameters(label=SIDE_CHALLENGER, model_family=canonical,
                       condition_overrides=tuple(sorted(challenger_overrides.items()))),
    )


# --------------------------------------------------------------------------- #
# 生产接线：把真实选股接到回放上
# --------------------------------------------------------------------------- #

def strategies_selector(module=None):
    """把 ``strategies.run_strategy`` 适配成回放的 ``select`` 可调用对象。

    选股本身完全交给既有实现（含它的权重/条件覆盖与拒绝语义）；本适配器只负责
    取出"按偏好排序的代码序列"。
    """
    if module is None:
        import strategies as module  # noqa: PLW2901 - 显式依赖注入点

    def select(model_family, session, condition_overrides, weight_overrides, topn):
        result = module.run_strategy(
            model_family, session.factor_table, topn=int(topn), gate=None,
            first_board_codes=list(session.first_board_codes or ()),
            weight_overrides=weight_overrides,
            condition_overrides=dict(condition_overrides or {}))
        return [str(pick.get("code")) for pick in (result.get("picks") or ())]

    return select
