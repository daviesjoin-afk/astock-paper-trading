# -*- coding: utf-8 -*-
"""选股样本的唯一权威“未来结果”标签契约（point-in-time correct labels）。

本模块回答**唯一一个问题**：

    在 ``T`` 做出的选股，究竟应该怎样根据 ``T`` 之后的真实结果打标签？

它刻意不回答“这只股票好不好”“模型选得准不准”。那些是 predictor /
exposure，不是 ground truth。

──────────────────────────── 契约 ────────────────────────────

三条不变量（其余全部由此推出）::

    features_at_T <= T
    label_evidence > T
    label_evidence 不得回流进 features

时间轴（全部以**交易日**计）::

    decision_at               决策时刻（tz-aware；naive 读作 Asia/Shanghai）
      ↓  decision_trade_date  决策所属自然日
      ↓  entry_date           决策日**之后**第一个交易日
      ↓  horizon              以 *交易日* 计的持有期（不是自然日）
      ↓  exit_date            entry 之后的第 horizon 个交易日
      ↓  raw/benchmark/excess forward return
    label_status               verified / pending / unavailable / invalid

``entry`` 为什么一定是决策日**之后**的交易日
-------------------------------------------
* 盘中决策（10:00）时，当日收盘价**尚未发生**，拿它当成交价就是未来函数。
  仓库没有盘中成交价证据（tick / intraday bar），所以不得凭空假设。
* 收盘后决策时，当日收盘价已经过去，**回填**它模拟成交等于在现实中先成交
  后决策。A 股 T+1 下这同样不可执行。

两条合起来只有一种可执行解释：``entry`` = 决策日之后第一个交易日。
本模块不发明 execution 规则，只坚持“标签必须描述现实中可执行的结果”。

``label_status`` 的语义（**不知道 != 失败**）
--------------------------------------------
======================  ==================================================
``verified``            未来窗口完整走完，entry/exit 价格证据完整，标签可用
``pending``             未来 horizon 尚未走完（尚未发生 != 失败）
``unavailable``         窗口本应走完，但缺可信价格 / 停牌 / 日历不全（缺数据 != 0%）
``invalid``             决策本身或时间关系非法
======================  ==================================================

绝不允许的降级：``pending -> negative``、``missing -> 0 return``、
``unavailable -> bad stock``。任何非 ``verified`` 的状态下，
``label_score`` / ``label_class`` / ``label`` 一律为 ``None``。

停牌与缺失
----------
市场有交易日但该股停牌、行情缺失、股票已退市，是**三种不同的证据状态**，
本模块分别给出 ``halted`` / ``missing`` / 价格非法，绝不归并成同一个 0。
entry 或 exit 无法成交 → ``unavailable``（fail closed，不自行发明“顺延”规则）。

版本化
------
``label_version`` 形如 ``selection-label-v1/raw-return``：``v1`` 固定口径，
后缀是 outcome basis。仓库当前没有可靠的 PIT benchmark，因此默认
authoritative basis 是 ``raw-return``；``excess-return`` 只在调用方
真的提供了 PIT benchmark 证据时才可用，且此时 benchmark 缺失 = fail closed，
绝不悄悄退回 raw。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

try:  # ``backend`` on sys.path (production and ``cd backend`` test runs)
    import point_in_time as PIT
except ImportError:  # pragma: no cover - package-style import
    from . import point_in_time as PIT


# ───────────────────────────── versioning ─────────────────────────────

LABEL_CONTRACT_VERSION = "selection-label-contract-v1"
LABEL_VERSION = "selection-label-v1"

BASIS_RAW = "raw-return"
BASIS_EXCESS = "excess-return"
LABEL_BASES = (BASIS_RAW, BASIS_EXCESS)
DEFAULT_BASIS = BASIS_RAW


def label_version(basis: str = DEFAULT_BASIS) -> str:
    """``selection-label-v1/<basis>`` —— 版本化，使旧标签永远可复算、可比较。"""
    text = str(basis or "").strip().lower()
    if text not in LABEL_BASES:
        raise ValueError(f"unsupported label basis: {basis!r}")
    return f"{LABEL_VERSION}/{text}"


# ───────────────────────────── label status ─────────────────────────────

STATUS_VERIFIED = "verified"
STATUS_PENDING = "pending"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INVALID = "invalid"
LABEL_STATUSES = (STATUS_VERIFIED, STATUS_PENDING, STATUS_UNAVAILABLE, STATUS_INVALID)

# 机器可读原因。任何非 verified 的结果都必须携带其中一个。
REASON_OK = "ok"
REASON_INVALID_CODE = "invalid_code"
REASON_INVALID_DECISION_TIMESTAMP = "invalid_decision_timestamp"
REASON_INVALID_EVALUATION_ASOF = "invalid_evaluation_asof"
REASON_INVALID_HORIZON = "invalid_horizon"
REASON_INVALID_PHASE = "invalid_decision_phase"
REASON_NO_SESSION_AFTER_DECISION = "no_session_after_decision"
REASON_SESSION_CALENDAR_INCOMPLETE = "session_calendar_incomplete"
REASON_FEATURE_WINDOW_UNRESOLVED = "feature_window_unresolved"
REASON_ENTRY_HALTED = "entry_halted"
REASON_ENTRY_PRICE_MISSING = "entry_price_missing"
REASON_ENTRY_PRICE_INVALID = "entry_price_invalid"
REASON_EXIT_HALTED = "exit_halted"
REASON_EXIT_PRICE_MISSING = "exit_price_missing"
REASON_EXIT_PRICE_INVALID = "exit_price_invalid"
REASON_HORIZON_NOT_MATURED = "horizon_not_matured"
REASON_BENCHMARK_EVIDENCE_MISSING = "benchmark_evidence_missing"
REASON_DECISION_AFTER_EVALUATION = "decision_after_evaluation"

# ───────────────────────────── market phases ─────────────────────────────

PHASE_INTRADAY = "intraday"
PHASE_AFTER_CLOSE = "after_close"
MARKET_PHASES = (PHASE_INTRADAY, PHASE_AFTER_CLOSE)
#: A 股连续竞价收市（Asia/Shanghai）。
MARKET_CLOSE = _dt.time(15, 0)

# ───────────────────────────── price evidence ─────────────────────────────

PRICE_OK = "ok"
PRICE_HALTED = "halted"
PRICE_MISSING = "missing"
PRICE_INVALID = "invalid"

_HALTED_TRUE_KEYS = ("halted", "is_halted", "suspended")
_TRADABLE_KEYS = ("tradable", "is_tradable", "trading")
_CLOSE_KEYS = ("close", "close_price", "price")
_MISSING_TOKENS = {"", "nan", "nat", "none", "null", "-", "--", "inf", "-inf", "+inf"}
#: 价格证据里的“不存在”与“被破坏”是两件事，和 ``learning_dataset`` 对
#: ``missing_feature`` / ``non_finite_feature`` 的区分保持一致。
#: ``NaN`` / ``Infinity`` 是**被破坏**的数值 → ``invalid``，不是缺失。
_MISSING_PRICE_TOKENS = {"", "none", "null", "nat", "-", "--"}


def _finite(value: Any) -> Optional[float]:
    """有限浮点或 ``None``。**绝不**把缺失/NaN 强转成 ``0``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in _MISSING_TOKENS:
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


def price_point(raw: Any) -> tuple:
    """把一条价格证据归一成 ``(price | None, state)``。

    四种状态严格区分，绝不合并：``ok`` / ``halted``（市场有交易日但该股停牌）
    / ``missing``（行情缺失）/ ``invalid``（非有限、<= 0 或不可解析）。
    ``state != ok`` 时 ``price`` 必为 ``None``。
    """
    if raw is None:
        return None, PRICE_MISSING
    if isinstance(raw, bool):
        return None, PRICE_INVALID
    if isinstance(raw, str) and raw.strip().lower() in _MISSING_PRICE_TOKENS:
        return None, PRICE_MISSING
    if isinstance(raw, Mapping):
        for key in _HALTED_TRUE_KEYS:
            if raw.get(key) is True:
                return None, PRICE_HALTED
        for key in _TRADABLE_KEYS:
            if raw.get(key) is False:
                return None, PRICE_HALTED
        candidate = None
        for key in _CLOSE_KEYS:
            if raw.get(key) is not None:
                candidate = raw.get(key)
                break
        if candidate is None:
            return None, PRICE_MISSING
        raw = candidate
    value = _finite(raw)
    if value is None:
        return None, PRICE_INVALID
    if value <= 0:
        return None, PRICE_INVALID
    return value, PRICE_OK


# ───────────────────────────── thresholds ─────────────────────────────


@dataclass(frozen=True, slots=True)
class LabelThresholds:
    """集中定义的分类阈值 —— 禁止散落 ``return > 0.03`` 这类常量。

    ``label_score`` 始终保留原始数值，分类只是它的一个派生视图，
    因此将来重新评估阈值不需要重算历史 outcome。
    """

    positive: float = 0.0
    negative: float = 0.0

    def classify(self, score: Optional[float]) -> Optional[str]:
        if score is None:
            return None
        if score > self.positive:
            return CLASS_POSITIVE
        if score < self.negative:
            return CLASS_NEGATIVE
        return CLASS_NEUTRAL


CLASS_POSITIVE = "positive"
CLASS_NEUTRAL = "neutral"
CLASS_NEGATIVE = "negative"
LABEL_CLASSES = (CLASS_POSITIVE, CLASS_NEUTRAL, CLASS_NEGATIVE)

#: 最中性的契约：无中性带，``score > 0`` 正、``score < 0`` 负、``score == 0``
#: 中性（**不是**负）。这不是策略优化，只是把“没有阈值”写成一个明确值。
DEFAULT_THRESHOLDS = LabelThresholds(positive=0.0, negative=0.0)


# ───────────────────────────── sessions ─────────────────────────────


def normalize_sessions(sessions: Any) -> list:
    """交易日序列 → 升序去重的 ``YYYY-MM-DD`` 列表。

    非交易日/不可解析项被剔除（不猜），调用方拿到的就是真实可用的 session 集。
    """
    seen = set()
    for raw in sessions or ():
        text = _date_text(raw)
        if text is not None:
            seen.add(text)
    return sorted(seen)


def _date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in _MISSING_TOKENS:
        return None
    candidate = text[:10].replace("/", "-")
    try:
        _dt.date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def default_session_calendar():
    """仓库里**唯一**的 A 股交易日历判定（``universe.is_trade_day``）。

    懒加载：``universe`` 会拉起数据抓取模块，标签层不该在 import 时付出这个代价。
    本模块不自己写第二套日历。
    """
    try:
        import universe as U
    except ImportError:  # pragma: no cover - package-style import
        from . import universe as U
    return U.is_trade_day


def sessions_between(start: Any, end: Any, *, calendar=None) -> list:
    """枚举 ``[start, end]`` 内的交易日。``calendar`` 可注入以便测试。"""
    first, last = _date_text(start), _date_text(end)
    if first is None or last is None or last < first:
        return []
    is_session = calendar or default_session_calendar()
    out = []
    day = _dt.date.fromisoformat(first)
    stop = _dt.date.fromisoformat(last)
    while day <= stop:
        if is_session(day):
            out.append(day.isoformat())
        day += _dt.timedelta(days=1)
    return out


# ───────────────────────────── identity ─────────────────────────────


def sample_identity(
    code: Any, decision_at: Any, horizon: Any, version: str = label_version()
) -> str:
    """稳定样本身份：``(code, decision_at, horizon, label_version)``。

    **绝不**只用 ``code``：同一天不同时刻的决策、不同 horizon、不同标签版本
    都是不同样本，只按 ``code`` join 会把一个未来结果错配给多个决策。
    """
    moment = _canonical_decision(decision_at)
    digest = hashlib.sha256(
        f"{str(code or '').strip()}|{moment or ''}|{int(horizon)}|{version}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


def _canonical_decision(value: Any) -> Optional[str]:
    """决策时刻 → 规范字符串（tz-aware，Asia/Shanghai）。"""
    moment = PIT.parse_asof(value)
    return None if moment is None else moment.isoformat(timespec="seconds")


# ───────────────────────────── the label ─────────────────────────────


@dataclass(frozen=True, slots=True)
class SelectionLabel:
    """一条选股样本的未来结果标签。字段是公开 API，供 dataset/eval 消费。"""

    code: str
    decision_at: Optional[str]
    decision_trade_date: Optional[str]
    decision_phase: Optional[str]
    entry_date: Optional[str]
    entry_price: Optional[float]
    horizon: int
    exit_date: Optional[str]
    exit_price: Optional[float]
    raw_forward_return: Optional[float]
    benchmark_return: Optional[float]
    excess_return: Optional[float]
    label_score: Optional[float]
    label_class: Optional[str]
    label: Optional[str]
    label_status: str
    label_reason: str
    label_version: str
    basis: str
    sample_key: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.label_status == STATUS_VERIFIED

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "decision_at": self.decision_at,
            "decision_trade_date": self.decision_trade_date,
            "decision_phase": self.decision_phase,
            "entry_date": self.entry_date,
            "entry_price": self.entry_price,
            "horizon": self.horizon,
            "exit_date": self.exit_date,
            "exit_price": self.exit_price,
            "raw_forward_return": self.raw_forward_return,
            "benchmark_return": self.benchmark_return,
            "excess_return": self.excess_return,
            "label_score": self.label_score,
            "label_class": self.label_class,
            "label": self.label,
            "label_status": self.label_status,
            "label_reason": self.label_reason,
            "label_version": self.label_version,
            "basis": self.basis,
            "sample_key": self.sample_key,
            "evidence": dict(self.evidence or {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unresolved(
    *,
    code: Any,
    moment: Any,
    phase: Any,
    horizon: Any,
    version: str,
    basis: str,
    status: str,
    reason: str,
    trade_date: Any = None,
    entry_date: Any = None,
    entry_price: Any = None,
    exit_date: Any = None,
    evidence: Optional[Mapping[str, Any]] = None,
) -> SelectionLabel:
    """非 ``verified`` 结果的唯一构造点。

    ``label_score`` / ``label_class`` / ``label`` 在这里被**硬性**置为 ``None``：
    这就是“不知道 != 失败”“尚未发生 != 失败”“缺数据 != 0%”在类型层面的落点。
    """
    canonical = _canonical_decision(moment) if moment is not None else None
    return SelectionLabel(
        code=str(code or "").strip(),
        decision_at=canonical,
        decision_trade_date=_date_text(trade_date),
        decision_phase=phase,
        entry_date=_date_text(entry_date),
        entry_price=entry_price,
        horizon=int(horizon) if isinstance(horizon, int) and not isinstance(horizon, bool) else 0,
        exit_date=_date_text(exit_date),
        exit_price=None,
        raw_forward_return=None,
        benchmark_return=None,
        excess_return=None,
        label_score=None,
        label_class=None,
        label=None,
        label_status=status,
        label_reason=reason,
        label_version=version,
        basis=basis,
        sample_key=sample_identity(code, canonical or "", horizon if horizon is not None else 0, version),
        evidence=dict(evidence or {}),
    )


def _phase_of(moment: _dt.datetime, phase: Any) -> str:
    if phase is None:
        local = moment.astimezone(PIT.china_tz())
        return PHASE_AFTER_CLOSE if local.time() >= MARKET_CLOSE else PHASE_INTRADAY
    text = str(phase).strip().lower()
    if text not in MARKET_PHASES:
        raise ValueError(f"unsupported decision phase: {phase!r}")
    return text


def selection_label(
    *,
    code: Any,
    decision_at: Any,
    horizon: Any,
    sessions: Sequence[Any],
    prices: Mapping[str, Any],
    asof: Any,
    decision_phase: Any = None,
    basis: str = DEFAULT_BASIS,
    benchmark_prices: Optional[Mapping[str, Any]] = None,
    thresholds: Optional[LabelThresholds] = None,
) -> SelectionLabel:
    """生成一条严格 PIT 的选股标签。纯函数：无网络、无落库、无全局状态。

    ``sessions``
        权威交易日序列（``YYYY-MM-DD``，升序）。必须覆盖决策日**之后**的
        ``horizon + 1`` 个交易日；覆盖不到时按 ``asof`` 判定是
        ``pending``（尚未走完）还是 ``unavailable``（日历不全）。
    ``prices``
        ``{日期: 价格}`` 或 ``{日期: {"close": .., "halted": ..}}``。
        指数/任意映射都行；缺失日期 = ``missing``，停牌 = ``halted``。
    ``asof``
        评估时点（重建数据集的冻结点）。日期精度表示该交易日收盘后。
        它让本函数**可复现**：同一份证据 + 同一个 ``asof`` = 同一个结果。
    """
    version = label_version(basis)
    clean_basis = version.split("/", 1)[1]
    moment = PIT.parse_asof(decision_at)
    if moment is None:
        return _unresolved(
            code=code, moment=None, phase=None, horizon=horizon,
            version=version, basis=clean_basis, status=STATUS_INVALID,
            reason=REASON_INVALID_DECISION_TIMESTAMP,
        )
    try:
        phase = _phase_of(moment, decision_phase)
    except ValueError:
        return _unresolved(
            code=code, moment=moment, phase=None, horizon=horizon,
            version=version, basis=clean_basis, status=STATUS_INVALID,
            reason=REASON_INVALID_PHASE,
        )

    text_code = str(code or "").strip()
    if not text_code:
        return _unresolved(
            code=code, moment=moment, phase=phase, horizon=horizon,
            version=version, basis=clean_basis, status=STATUS_INVALID,
            reason=REASON_INVALID_CODE,
        )

    clean_horizon = horizon if isinstance(horizon, int) and not isinstance(horizon, bool) else None
    if clean_horizon is None or clean_horizon < 1:
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=horizon,
            version=version, basis=clean_basis, status=STATUS_INVALID,
            reason=REASON_INVALID_HORIZON,
        )

    evaluation = PIT.parse_asof(asof)
    if evaluation is None:
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_INVALID,
            reason=REASON_INVALID_EVALUATION_ASOF,
        )
    asof_day = evaluation.astimezone(PIT.china_tz()).date().isoformat()
    trade_date = moment.astimezone(PIT.china_tz()).date().isoformat()

    if trade_date > asof_day:
        # 决策发生在评估时点之后 —— 这不是一个可评估的历史样本。
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_INVALID,
            reason=REASON_DECISION_AFTER_EVALUATION, trade_date=trade_date,
        )

    calendar = normalize_sessions(sessions)
    if not calendar:
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_UNAVAILABLE,
            reason=REASON_SESSION_CALENDAR_INCOMPLETE, trade_date=trade_date,
        )

    # ── entry：决策日**之后**第一个交易日。绝不用决策日自己。 ──
    later = [day for day in calendar if day > trade_date]
    if not later:
        if calendar[-1] >= asof_day:
            return _unresolved(
                code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
                version=version, basis=clean_basis, status=STATUS_PENDING,
                reason=REASON_HORIZON_NOT_MATURED, trade_date=trade_date,
            )
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_UNAVAILABLE,
            reason=REASON_SESSION_CALENDAR_INCOMPLETE, trade_date=trade_date,
        )
    entry_date = later[0]
    entry_index = calendar.index(entry_date)
    exit_index = entry_index + clean_horizon

    if exit_index >= len(calendar):
        status = STATUS_PENDING if calendar[-1] >= asof_day else STATUS_UNAVAILABLE
        reason = (
            REASON_HORIZON_NOT_MATURED
            if status == STATUS_PENDING
            else REASON_SESSION_CALENDAR_INCOMPLETE
        )
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=status, reason=reason,
            trade_date=trade_date, entry_date=entry_date,
        )
    exit_date = calendar[exit_index]

    if exit_date > asof_day:
        # 未来窗口尚未走完。**不得**因此判成负例。
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_PENDING,
            reason=REASON_HORIZON_NOT_MATURED, trade_date=trade_date, entry_date=entry_date,
            exit_date=exit_date,
        )

    # ── 价格证据：entry 与 exit 都必须可成交。 ──
    entry_price, entry_state = price_point(dict(prices or {}).get(entry_date))
    if entry_state != PRICE_OK:
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_UNAVAILABLE,
            reason={
                PRICE_HALTED: REASON_ENTRY_HALTED,
                PRICE_MISSING: REASON_ENTRY_PRICE_MISSING,
            }.get(entry_state, REASON_ENTRY_PRICE_INVALID),
            trade_date=trade_date, entry_date=entry_date, exit_date=exit_date,
        )
    exit_price, exit_state = price_point(dict(prices or {}).get(exit_date))
    if exit_state != PRICE_OK:
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_UNAVAILABLE,
            reason={
                PRICE_HALTED: REASON_EXIT_HALTED,
                PRICE_MISSING: REASON_EXIT_PRICE_MISSING,
            }.get(exit_state, REASON_EXIT_PRICE_INVALID),
            trade_date=trade_date, entry_date=entry_date, entry_price=entry_price,
            exit_date=exit_date,
        )

    raw_return = exit_price / entry_price - 1.0

    benchmark_return = None
    if benchmark_prices is not None:
        bench_entry, bench_entry_state = price_point(dict(benchmark_prices).get(entry_date))
        bench_exit, bench_exit_state = price_point(dict(benchmark_prices).get(exit_date))
        if (
            bench_entry_state == PRICE_OK
            and bench_exit_state == PRICE_OK
            and bench_entry > 0
        ):
            benchmark_return = bench_exit / bench_entry - 1.0
    excess_return = (
        raw_return - benchmark_return if benchmark_return is not None else None
    )

    if clean_basis == BASIS_EXCESS and excess_return is None:
        # 声明了超额口径却拿不到基准证据 → fail closed，绝不悄悄退回 raw。
        return _unresolved(
            code=text_code, moment=moment, phase=phase, horizon=clean_horizon,
            version=version, basis=clean_basis, status=STATUS_UNAVAILABLE,
            reason=REASON_BENCHMARK_EVIDENCE_MISSING, trade_date=trade_date,
            entry_date=entry_date, entry_price=entry_price, exit_date=exit_date,
        )

    score = excess_return if clean_basis == BASIS_EXCESS else raw_return
    scale = thresholds or DEFAULT_THRESHOLDS
    label_class = scale.classify(score)
    return SelectionLabel(
        code=text_code,
        decision_at=moment.isoformat(timespec="seconds"),
        decision_trade_date=trade_date,
        decision_phase=phase,
        entry_date=entry_date,
        entry_price=entry_price,
        horizon=clean_horizon,
        exit_date=exit_date,
        exit_price=exit_price,
        raw_forward_return=raw_return,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        label_score=score,
        label_class=label_class,
        label=label_class,
        label_status=STATUS_VERIFIED,
        label_reason=REASON_OK,
        label_version=version,
        basis=clean_basis,
        sample_key=sample_identity(text_code, moment, clean_horizon, version),
        evidence={
            "trading_days": clean_horizon,
            "entry_session_index": entry_index,
            "exit_session_index": exit_index,
            "asof": asof_day,
            "price_source": "provided_evidence",
        },
    )


# ───────────────────────── evidence windows ─────────────────────────


def evidence_windows(
    *,
    decision_at: Any,
    horizon: Any,
    sessions: Sequence[Any],
    decision_phase: Any = None,
) -> dict:
    """决策时点看到的**特征窗口**与之后**标签窗口**的边界。

    这是把“features_at_T <= T < label_evidence”变成可断言不变量的一处落点：
    ``feature_window_end`` 永远不会 >= ``label_window_start``。

    * ``after_close`` 决策：特征可以是决策日收盘本身。
    * ``intraday`` 决策：决策日收盘**尚未发生**，特征窗口只能到前一个交易日。
    """
    moment = PIT.parse_asof(decision_at)
    if moment is None:
        return {
            "valid": False, "reason": REASON_INVALID_DECISION_TIMESTAMP,
            "decision_trade_date": None, "decision_phase": None,
            "feature_window_end": None, "label_window_start": None, "label_window_end": None,
        }
    try:
        phase = _phase_of(moment, decision_phase)
    except ValueError:
        return {
            "valid": False, "reason": REASON_INVALID_PHASE,
            "decision_trade_date": None, "decision_phase": None,
            "feature_window_end": None, "label_window_start": None, "label_window_end": None,
        }
    trade_date = moment.astimezone(PIT.china_tz()).date().isoformat()
    calendar = normalize_sessions(sessions)
    clean_horizon = horizon if isinstance(horizon, int) and not isinstance(horizon, bool) else None
    if clean_horizon is None or clean_horizon < 1:
        return {
            "valid": False, "reason": REASON_INVALID_HORIZON,
            "decision_trade_date": trade_date, "decision_phase": phase,
            "feature_window_end": None, "label_window_start": None, "label_window_end": None,
        }

    earlier = [day for day in calendar if day < trade_date]
    later = [day for day in calendar if day > trade_date]
    if phase == PHASE_AFTER_CLOSE:
        feature_end = trade_date
    else:
        if not earlier:
            return {
                "valid": False, "reason": REASON_FEATURE_WINDOW_UNRESOLVED,
                "decision_trade_date": trade_date, "decision_phase": phase,
                "feature_window_end": None, "label_window_start": None, "label_window_end": None,
            }
        feature_end = earlier[-1]
    if not later:
        return {
            "valid": False, "reason": REASON_NO_SESSION_AFTER_DECISION,
            "decision_trade_date": trade_date, "decision_phase": phase,
            "feature_window_end": feature_end, "label_window_start": None, "label_window_end": None,
        }
    label_start = later[0]
    index = calendar.index(label_start) + clean_horizon
    label_end = calendar[index] if index < len(calendar) else None
    return {
        "valid": label_end is not None,
        "reason": REASON_OK if label_end is not None else REASON_SESSION_CALENDAR_INCOMPLETE,
        "decision_trade_date": trade_date,
        "decision_phase": phase,
        "feature_window_end": feature_end,
        "label_window_start": label_start,
        "label_window_end": label_end,
        "trading_days": clean_horizon,
    }


# ──────────────── feature / label record separation & joining ────────────────

#: 决策时点特征记录允许携带的键。``selected`` / ``score`` 之类只是 predictor /
#: exposure：它们可以进特征，但**永远不得**决定标签。
FEATURE_RECORD_FIELDS = (
    "code",
    "decision_at",
    "horizon",
    "label_version",
    "features",
    "selected",
    "score",
)

#: 标签记录允许携带的键。
LABEL_RECORD_FIELDS = (
    "code",
    "decision_at",
    "horizon",
    "label_version",
    "label_status",
    "label_reason",
    "label_score",
    "label_class",
    "label",
    "raw_forward_return",
    "benchmark_return",
    "excess_return",
    "entry_date",
    "entry_price",
    "exit_date",
    "exit_price",
)

#: 只有标签才有的字段。共享的身份键（``code`` / ``decision_at`` / ``horizon`` /
#: ``label_version``）不算泄漏 —— 它们是 join 键，必然两侧都有。
LABEL_ONLY_FIELDS = tuple(
    name for name in LABEL_RECORD_FIELDS if name not in FEATURE_RECORD_FIELDS
)

_ASSEMBLY_MISSING_LABEL = "missing_label"
_ASSEMBLY_MISSING_FEATURE = "missing_feature"


def assert_feature_record(record: Mapping[str, Any]) -> dict:
    """校验并复制一条**特征**记录。

    任何标签字段出现在特征记录里 → 抛错。这是 label leakage 的结构性守卫：
    把未来 outcome 塞进特征表会让训练集看起来更“全”，却是彻底的泄漏。
    """
    if not isinstance(record, Mapping):
        raise TypeError("feature record must be a mapping")
    leaked = sorted(set(record) & set(LABEL_ONLY_FIELDS))
    if leaked:
        raise ValueError(f"label fields leaked into feature record: {leaked}")
    unknown = sorted(set(record) - set(FEATURE_RECORD_FIELDS))
    if unknown:
        raise ValueError(f"unknown feature record fields: {unknown}")
    return dict(record)


def assert_label_record(record: Mapping[str, Any]) -> dict:
    """校验并复制一条**标签**记录（特征字段一律拒绝）。"""
    if not isinstance(record, Mapping):
        raise TypeError("label record must be a mapping")
    leaked = sorted(set(record) & {"features", "selected", "score"})
    if leaked:
        raise ValueError(f"feature fields leaked into label record: {leaked}")
    return dict(record)


def label_record(label: SelectionLabel) -> dict:
    """``SelectionLabel`` → 可持久化/可 join 的标签记录（只含标签字段）。"""
    data = label.as_dict()
    return {key: data[key] for key in LABEL_RECORD_FIELDS}


def feature_record(
    *,
    code: Any,
    decision_at: Any,
    horizon: Any,
    version: str = label_version(),
    features: Mapping[str, Any],
    selected: Any = None,
    score: Any = None,
) -> dict:
    """构造一条特征记录。``selected`` / ``score`` 仅作为 predictor 保留。"""
    return assert_feature_record(
        {
            "code": str(code or "").strip(),
            "decision_at": _canonical_decision(decision_at) or "",
            "horizon": int(horizon),
            "label_version": version,
            "features": dict(features or {}),
            "selected": selected,
            "score": score,
        }
    )


def _record_identity(record: Mapping[str, Any], *, join_on_version: bool) -> Optional[str]:
    code = str(record.get("code") or "").strip()
    horizon = record.get("horizon")
    if not code or not isinstance(horizon, int) or isinstance(horizon, bool):
        return None
    version = str(record.get("label_version") or "") if join_on_version else ""
    return sample_identity(code, record.get("decision_at"), horizon, version)


def assemble_learning_rows(
    feature_records: Sequence[Mapping[str, Any]],
    label_records: Sequence[Mapping[str, Any]],
    *,
    require_verified: bool = True,
    join_on_version: bool = True,
) -> dict:
    """在 dataset assembly 阶段按稳定身份 join 特征与标签。

    * join 键 = ``(code, decision_at, horizon, label_version)`` —— **不是** ``code``。
    * 标签按状态过滤：``require_verified=True`` 时只有 ``verified`` 能进 rows；
      被排除的样本按状态**逐个计数上报**，绝不静默消失。
    * 特征记录里出现标签字段会被 :func:`assert_feature_record` 直接拒绝。

    返回 ``{"rows", "report"}``；``report`` 是机器可读的审计口径。
    """
    report = {
        "feature_rows": 0,
        "label_rows": 0,
        "assembled_rows": 0,
        "label_status_counts": {status: 0 for status in LABEL_STATUSES},
        "excluded": {
            _ASSEMBLY_MISSING_LABEL: 0,
            _ASSEMBLY_MISSING_FEATURE: 0,
            "unverified_label": 0,
            "invalid_feature_identity": 0,
            "invalid_label_identity": 0,
        },
        "require_verified": bool(require_verified),
        "join_on_version": bool(join_on_version),
    }

    features_by_key = {}
    for raw in feature_records or ():
        record = assert_feature_record(raw)
        report["feature_rows"] += 1
        key = _record_identity(record, join_on_version=join_on_version)
        if key is None:
            report["excluded"]["invalid_feature_identity"] += 1
            continue
        features_by_key[key] = record

    labels_by_key = {}
    for raw in label_records or ():
        record = assert_label_record(raw)
        report["label_rows"] += 1
        status = str(record.get("label_status") or "")
        if status in report["label_status_counts"]:
            report["label_status_counts"][status] += 1
        key = _record_identity(record, join_on_version=join_on_version)
        if key is None:
            report["excluded"]["invalid_label_identity"] += 1
            continue
        labels_by_key[key] = record

    rows = []
    for key, feature in sorted(features_by_key.items()):
        label = labels_by_key.get(key)
        if label is None:
            report["excluded"][_ASSEMBLY_MISSING_LABEL] += 1
            continue
        if require_verified and label.get("label_status") != STATUS_VERIFIED:
            report["excluded"]["unverified_label"] += 1
            continue
        rows.append(
            {
                "sample_key": key,
                "code": feature["code"],
                "decision_at": feature["decision_at"],
                "horizon": feature["horizon"],
                "label_version": feature.get("label_version") or label.get("label_version"),
                "features": dict(feature.get("features") or {}),
                "selected": feature.get("selected"),
                "score": feature.get("score"),
                "label_status": label.get("label_status"),
                "label_score": label.get("label_score"),
                "label_class": label.get("label_class"),
                "raw_forward_return": label.get("raw_forward_return"),
                "benchmark_return": label.get("benchmark_return"),
                "excess_return": label.get("excess_return"),
                "entry_date": label.get("entry_date"),
                "exit_date": label.get("exit_date"),
            }
        )
    for key in labels_by_key:
        if key not in features_by_key:
            report["excluded"][_ASSEMBLY_MISSING_FEATURE] += 1
    report["assembled_rows"] = len(rows)
    return {"rows": rows, "report": report}


# ───────────────────────────── self-check ─────────────────────────────


def _self_check() -> None:
    sessions = [
        "2024-06-12", "2024-06-13", "2024-06-14",
        "2024-06-17", "2024-06-18", "2024-06-19", "2024-06-20",
    ]
    prices = {day: 10.0 for day in sessions}
    prices["2024-06-20"] = 11.0

    label = selection_label(
        code="600001",
        decision_at="2024-06-14T16:00:00+08:00",
        horizon=3,
        sessions=sessions,
        prices=prices,
        asof="2024-06-30",
    )
    assert label.label_status == STATUS_VERIFIED, label
    assert label.entry_date == "2024-06-17", label.entry_date
    assert label.exit_date == "2024-06-20", label.exit_date
    assert abs(label.raw_forward_return - 0.1) < 1e-12, label.raw_forward_return
    assert label.label_class == CLASS_POSITIVE

    pending = selection_label(
        code="600001",
        decision_at="2024-06-14T16:00:00+08:00",
        horizon=3,
        sessions=sessions,
        prices=prices,
        asof="2024-06-17",
    )
    assert pending.label_status == STATUS_PENDING, pending
    assert pending.label_score is None and pending.label_class is None

    windows = evidence_windows(
        decision_at="2024-06-14T10:00:00+08:00", horizon=3, sessions=sessions
    )
    assert windows["feature_window_end"] == "2024-06-13", windows
    assert windows["label_window_start"] == "2024-06-17", windows
    assert windows["feature_window_end"] < windows["label_window_start"]
    print("selection_labels self-check: ok")


if __name__ == "__main__":
    _self_check()
