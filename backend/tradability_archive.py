# -*- coding: utf-8 -*-
"""历史可交易性事实资产层（Historical Tradability Archive）。

本模块回答的是**事实**问题，不是**好恶**问题：

    在历史日期 D，股票 X 当时到底发生了什么、限制是什么、依据是什么？

它明确**不**回答"这只股票好不好"——策略评分、因子、收益预期、风险偏好都不在这里。
判断层只把历史事实翻译成"当时能不能买 / 能不能卖"，且判断规则的变化**不得**改写
历史事实。

三层严格分开::

    历史事实层   TradabilityEvidence      当时发生了什么（不含 can_buy / can_sell）
        ↓  normalize / persist
    事实资产     historical_tradability_archive   不可变、PIT 可复现
        ↓  TradabilityEvaluator
    判断层       TradabilityDecision      当时的可交易判断 + 阻断原因

──────────────────────── PIT 铁律 ────────────────────────
查询 ``decision_time`` 时刻的状态时，只有**当时可见**的证据参与判定：

* ``observed_at <= decision_time``   —— 当时是否已经观察到这条证据；
* ``effective_at <= decision_time``  —— 这条状态当时是否已经生效。

两条都必须成立。用"今天才知道"的退市公告去解释历史状态是最典型的污染，
本模块用 ``observed_at`` 把它挡在外面；时点缺失或不可解析一律 **fail closed**
（不可见），而不是默认可见。

同一 ``(code, session_date)`` 在某个 ``decision_time`` 下可能有多条可见证据
（例如当天先后出现"停牌"与"盘中复牌"，或同一生效时点的记录被上游修正），
取 ``effective_at`` 最大者，即当时**最新生效**的那条；同一 ``effective_at``
上若存在多条修订，取其中 ``observed_at`` 最大者——即决策当时**最新已知**的版本。
唯一约束 ``(code, session_date, effective_at, observed_at)`` 保证这个选择是确定性的，
同时允许"同一生效时点、更晚被观察到"的修订与原始记录并存，而不是被静默丢弃。

无任何可见记录 ≠ 可交易。没有记录时返回 ``UNKNOWN_STATE``，买卖都阻断。

──────────────────────── 复用，而不是另造 ────────────────────────
本模块**不**定义市场规则，也**不**复制 PIT 口径：

=================================  ============================================
关注点                              权威来源（本模块直接调用）
=================================  ============================================
PIT 可见性（observed_at）           :func:`point_in_time.is_visible_at`
时点解析（date-only = 当日收盘）      :func:`point_in_time.parse_asof`
=================================  ============================================

与仓库既有模块的分工（刻意不重叠）::

    security_state_point_in_time  历史**证券状态档**（JSON）：名称 / 风险标记，
                                  面向"这只票当时叫什么、有没有风险标记"

    selection_tradability         历史**选股可成交性**判定：输入调用方构造的
                                  MarketEvidence，涨跌停 / 权限 / T+1 规则委托
                                  paper_trading_rules

    tradability_archive（本模块）  历史**可交易性事实资产**（DB 表）：上市 / 退市 /
                                  ST / 停牌 / 行情存在性 / 成交存在性 / 涨跌停锁定，
                                  按 ``(code, session_date, effective_at)`` 收敛成
                                  可复现、可审计的事实记录

判断层的输入是**已被资产层收敛过的布尔事实**，因此这里不重新推导涨跌停幅度、
ST 名单或板块权限——那些是 ``paper_trading_rules`` 的职责。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

try:  # 生产路径（backend 在 sys.path 上）
    import point_in_time as PIT
except ImportError:  # pragma: no cover - 包内导入
    from . import point_in_time as PIT  # type: ignore


ARCHIVE_TABLE = "historical_tradability_archive"
CONTRACT_VERSION = "tradability-archive-v1"
FINGERPRINT_VERSION = "sha256-canonical-tradability-v1"
MIGRATION_DESCRIPTION = "013_add_historical_tradability_archive"
DEFAULT_CACHE_SIZE = 4096

SESSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 归档表的**查询列序**。显式列清单而非 ``SELECT *``：位置取值依赖顺序稳定，
# 而 ``SELECT *`` 的顺序由建表决定、也会随 schema 演进漂移。读取时两种行形态
# （sqlite3.Row 与默认连接的 tuple）都按这份清单对齐。
ARCHIVE_COLUMNS = (
    "id", "code", "session_date", "effective_at", "observed_at",
    "is_listed", "is_st", "is_suspended", "is_price_limit_locked",
    "price_limit_direction", "has_market_quote", "has_trade_volume",
    "source", "listing_date", "delisting_date", "suspension_reason",
    "created_at",
)

# 涨跌停方向。沿用 selection_tradability 的方向性语义：涨停只拦买、跌停只拦卖。
PRICE_LIMIT_UP = "up"
PRICE_LIMIT_DOWN = "down"
_PRICE_LIMIT_DIRECTIONS = (PRICE_LIMIT_UP, PRICE_LIMIT_DOWN)

# 上游可能给出的方向写法（中英文 / 大小写 / 简写）统一收敛到两个方向常量。
_PRICE_LIMIT_ALIASES = {
    "up": PRICE_LIMIT_UP,
    "limit_up": PRICE_LIMIT_UP,
    "limitup": PRICE_LIMIT_UP,
    "zhangting": PRICE_LIMIT_UP,
    "涨停": PRICE_LIMIT_UP,
    "up_lock": PRICE_LIMIT_UP,
    "down": PRICE_LIMIT_DOWN,
    "limit_down": PRICE_LIMIT_DOWN,
    "limitdown": PRICE_LIMIT_DOWN,
    "dieting": PRICE_LIMIT_DOWN,
    "跌停": PRICE_LIMIT_DOWN,
    "down_lock": PRICE_LIMIT_DOWN,
}

__all__ = [
    "ARCHIVE_TABLE",
    "CONTRACT_VERSION",
    "FINGERPRINT_VERSION",
    "MIGRATION_DESCRIPTION",
    "PRICE_LIMIT_UP",
    "PRICE_LIMIT_DOWN",
    "TradabilityArchiveError",
    "TradabilityReason",
    "TradabilityEvidence",
    "TradabilityDecision",
    "TradabilityProvider",
    "TradabilityArchiveRepository",
    "TradabilityEvaluator",
    "ensure_schema",
    "normalize_record",
    "evidence_fingerprint",
    "evaluate",
    "tradability_at",
]


class TradabilityArchiveError(ValueError):
    """事实记录无法成立时抛出（缺失/不可解析的 code、session、时点或来源）。"""


class TradabilityReason(str, Enum):
    """阻断原因的唯一词汇表。**禁止**在别处散落原因字符串。

    ``OK`` 是"没有阻断"的显式取值——判断层永远返回一个成员，而不是 ``None``
    或任意字符串，这样审计侧可以穷尽枚举而不必猜。
    """

    OK = "ok"
    NOT_LISTED = "not_listed"
    DELISTED = "delisted"
    ST_RESTRICTED = "st_restricted"
    SUSPENDED = "suspended"
    NO_QUOTE = "no_quote"
    NO_VOLUME = "no_volume"
    BUY_LIMIT_LOCKED = "buy_limit_locked"
    SELL_LIMIT_LOCKED = "sell_limit_locked"
    UNKNOWN_STATE = "unknown_state"


# ─────────────────────────────── 工具函数 ───────────────────────────────


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _session(value: Any) -> Optional[str]:
    """规范化成 ``YYYY-MM-DD``；非法日期返回 ``None``（fail closed）。"""
    text = _text(value)
    if text is None:
        return None
    match = SESSION_RE.search(text)
    if match is None:
        return None
    day = match.group(0)
    try:
        _dt.date.fromisoformat(day)
    except ValueError:
        return None
    return day


def _flag(value: Any) -> Optional[bool]:
    """严格布尔：只认 bool 与精确 0/1，其余（含 2 / -1 / 0.5 / inf / NaN）未知。

    复用 :func:`point_in_time.as_strict_bool`，与仓库 PR-149 的严格布尔口径一致。
    """
    return PIT.as_strict_bool(value)


def _direction(value: Any) -> Optional[str]:
    """规范化涨跌停方向；无法识别返回 ``None``（未知，而不是"无方向"）。

    只接受白名单词表。未知方向**保留**为 ``None`` 而不猜测，由判断层决定是否
    fail closed——这一层只负责如实保留事实。
    """
    text = _text(value)
    if text is None:
        return None
    return _PRICE_LIMIT_ALIASES.get(text.lower())


def _instant(value: Any) -> Optional["_dt.datetime"]:
    """解析时点。date-only 按仓库既有口径取**当日收盘**（见点内时间模块）。"""
    return PIT.parse_asof(value)


def _canonical_instant(value: Any) -> Optional[str]:
    moment = _instant(value)
    if moment is None:
        return None
    # 保留解析出来的**全部**精度（默认 isoformat 不截断）。
    #
    # 曾经写成 timespec="seconds"，后果有二：同一秒内被观察到的两次修订
    # （如 15:00:00.100 与 15:00:00.900）会塌缩成同一个身份，后者被唯一键静默
    # 丢弃；且截断会把证据的可见时点**提前**到该秒起点，让它比实际更早可见。
    return moment.isoformat()


_COLUMN_LIST = ", ".join(ARCHIVE_COLUMNS)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _db_flag(value: Optional[bool]) -> Optional[int]:
    return None if value is None else (1 if value else 0)


# ─────────────────────────────── 事实层 ───────────────────────────────


@dataclass(frozen=True, slots=True)
class TradabilityEvidence:
    """某个 session 上、某个时点生效的**历史事实**。

    只描述"当时发生了什么"。**不含** ``can_buy`` / ``can_sell``：可交易判断是
    判断层的结论，规则变化不应该回头改写历史事实本身。

    ``is_listed`` / ``is_st`` / ``is_suspended`` / ``has_market_quote`` /
    ``has_trade_volume`` / ``is_price_limit_locked``
        三态：``True`` / ``False`` / ``None``（未知）。``None`` 不等于 ``False``，
        也不等于"允许交易"——判断层对核心事实一律 fail closed。

    ``price_limit_direction``
        涨跌停锁定方向（``"up"`` 涨停 / ``"down"`` 跌停 / ``None`` 未知）。
        锁定是**有方向**的：涨停只拦买、跌停只拦卖，与仓库权威实现
        ``selection_tradability`` 的方向性口径一致。无方向布尔会在涨停时错误地
        禁止卖出——那恰恰是流动性最好的时候。

    ``observed_at``
        这条证据**被观察到**的时点。用它挡住"今天的数据解释过去"。
    ``effective_at``
        这条状态**开始生效**的时点。同一 session 可以有多条不同生效时点的记录
        （例如盘中停牌、盘后复牌）。
    """

    code: str
    session_date: str
    is_listed: Optional[bool]
    listing_date: Optional[str]
    delisting_date: Optional[str]
    is_st: Optional[bool]
    is_suspended: Optional[bool]
    suspension_reason: Optional[str]
    has_market_quote: Optional[bool]
    has_trade_volume: Optional[bool]
    is_price_limit_locked: Optional[bool]
    price_limit_direction: Optional[str]
    source: str
    observed_at: str
    effective_at: str

    @property
    def contract_version(self) -> str:
        return CONTRACT_VERSION

    def to_row(self) -> dict:
        """落库行。布尔三态存 ``1`` / ``0`` / ``NULL``。"""
        return {
            "code": self.code,
            "session_date": self.session_date,
            "effective_at": self.effective_at,
            "observed_at": self.observed_at,
            "is_listed": _db_flag(self.is_listed),
            "is_st": _db_flag(self.is_st),
            "is_suspended": _db_flag(self.is_suspended),
            "is_price_limit_locked": _db_flag(self.is_price_limit_locked),
            "price_limit_direction": self.price_limit_direction,
            "has_market_quote": _db_flag(self.has_market_quote),
            "has_trade_volume": _db_flag(self.has_trade_volume),
            "source": self.source,
            "listing_date": self.listing_date,
            "delisting_date": self.delisting_date,
            "suspension_reason": self.suspension_reason,
        }

    def to_dict(self) -> dict:
        payload = dict(self.to_row())
        payload["contract_version"] = CONTRACT_VERSION
        payload["fingerprint"] = evidence_fingerprint(self)
        return payload


@dataclass(frozen=True, slots=True)
class TradabilityDecision:
    """判断层结论：能不能买 / 能不能卖，以及**为什么不能**。

    审计字段（``source`` / ``effective_at`` / ``observed_at`` / ``fingerprint``）
    永远存在，用来回答"当时系统凭什么这么认为"。没有事实记录时它们为 ``None``
    且 ``evidence_present=False``。
    """

    code: str
    session_date: str
    decision_time: Optional[str]
    can_buy: bool
    can_sell: bool
    buy_block_reason: TradabilityReason
    sell_block_reason: TradabilityReason
    source: Optional[str]
    effective_at: Optional[str]
    observed_at: Optional[str]
    fingerprint: Optional[str]
    evidence_present: bool
    suspension_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "session_date": self.session_date,
            "decision_time": self.decision_time,
            "can_buy": self.can_buy,
            "can_sell": self.can_sell,
            "buy_block_reason": self.buy_block_reason.value,
            "sell_block_reason": self.sell_block_reason.value,
            "source": self.source,
            "effective_at": self.effective_at,
            "observed_at": self.observed_at,
            "fingerprint": self.fingerprint,
            "evidence_present": self.evidence_present,
            "suspension_reason": self.suspension_reason,
            "contract_version": CONTRACT_VERSION,
        }


# ───────────────────────────── 规范化 / 指纹 ─────────────────────────────


def _resolve_listed(
    session: str,
    listing_date: Optional[str],
    delisting_date: Optional[str],
    explicit: Optional[bool],
) -> Optional[bool]:
    """由上市/退市日期推导 ``is_listed``；推不出来就是 ``None``（fail closed）。

    显式给出的 ``is_listed`` 优先——它是上游已经证明过的事实。只有在上游没给
    的时候才由两个日期推导：落在 ``[listing_date, delisting_date)`` 内才算上市。
    两个日期都不知道 → ``None``，**绝不**因为"数据库里有这只票"就默认上市。
    """
    if explicit is not None:
        return explicit
    if listing_date is None and delisting_date is None:
        return None
    if listing_date is not None and session < listing_date:
        return False
    if delisting_date is not None and session >= delisting_date:
        return False
    if listing_date is not None:
        return True
    # 只有退市日、且 session 在它之前：能证明"尚未退市"，但证明不了"当时已上市"。
    return None


def normalize_record(
    raw: Mapping[str, Any],
    *,
    source: Optional[str] = None,
    observed_at: Optional[str] = None,
    effective_at: Optional[str] = None,
) -> TradabilityEvidence:
    """把一条上游原始记录规范化成事实证据。

    架构里的 **Normalizer** 层。缺失或不可解析的 ``code`` / ``session_date`` /
    ``observed_at`` / ``effective_at`` / ``source`` 一律抛错，不猜、不填默认值：
    没有可信时点的记录不是历史证据。
    """
    record = dict(raw or {})
    code = _text(record.get("code"))
    if code is None:
        raise TradabilityArchiveError("证据缺少 code")
    session = _session(record.get("session_date") or record.get("date") or record.get("session"))
    if session is None:
        raise TradabilityArchiveError(f"证据缺少可解析的 session_date: {code}")
    src = _text(source) or _text(record.get("source"))
    if src is None:
        raise TradabilityArchiveError(f"证据缺少 source: {code} {session}")

    observed = _canonical_instant(
        observed_at if observed_at is not None else record.get("observed_at")
    )
    if observed is None:
        raise TradabilityArchiveError(f"证据缺少可解析的 observed_at: {code} {session}")
    effective = _canonical_instant(
        effective_at if effective_at is not None else record.get("effective_at")
    )
    if effective is None:
        raise TradabilityArchiveError(f"证据缺少可解析的 effective_at: {code} {session}")

    listing_date = _session(record.get("listing_date"))
    delisting_date = _session(record.get("delisting_date"))
    is_listed = _resolve_listed(
        session,
        listing_date,
        delisting_date,
        _flag(record.get("is_listed")),
    )

    return TradabilityEvidence(
        code=code,
        session_date=session,
        is_listed=is_listed,
        listing_date=listing_date,
        delisting_date=delisting_date,
        is_st=_flag(record.get("is_st")),
        is_suspended=_flag(record.get("is_suspended")),
        suspension_reason=_text(record.get("suspension_reason")),
        has_market_quote=_flag(record.get("has_market_quote")),
        has_trade_volume=_flag(record.get("has_trade_volume")),
        is_price_limit_locked=_flag(record.get("is_price_limit_locked")),
        price_limit_direction=_direction(
            record.get("price_limit_direction") or record.get("limit_direction")
        ),
        source=src,
        observed_at=observed,
        effective_at=effective,
    )


def evidence_fingerprint(evidence: TradabilityEvidence) -> str:
    """内容寻址指纹：同一份事实重复计算得到同一个值，与插入顺序无关。"""
    payload = {
        "version": FINGERPRINT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "code": evidence.code,
        "session_date": evidence.session_date,
        "effective_at": evidence.effective_at,
        "observed_at": evidence.observed_at,
        "is_listed": evidence.is_listed,
        "listing_date": evidence.listing_date,
        "delisting_date": evidence.delisting_date,
        "is_st": evidence.is_st,
        "is_suspended": evidence.is_suspended,
        "suspension_reason": evidence.suspension_reason,
        "has_market_quote": evidence.has_market_quote,
        "has_trade_volume": evidence.has_trade_volume,
        "is_price_limit_locked": evidence.is_price_limit_locked,
        "price_limit_direction": evidence.price_limit_direction,
        "source": evidence.source,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ─────────────────────────────── Schema ───────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """正式 migration 013 的建表函数（幂等）。

    唯一约束 ``(code, session_date, effective_at, observed_at)``：同一天可以有多条不同生效
    时点的状态（盘中停牌 / 盘后复牌），且同一生效时点上的记录可以被更晚观察到的
    版本修订——两者都必须能并存，否则上游修正会被静默丢弃。

    ``listing_date`` / ``delisting_date`` / ``suspension_reason`` 是对任务书列出的
    字段的**追加**，不是替换：没有它们就无法回答"判断依据是什么"，审计链会断在
    半路（见模块文档的 PIT 与审计要求）。全部可空，且不参与唯一约束。
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            session_date TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            is_listed INTEGER,
            is_st INTEGER,
            is_suspended INTEGER,
            is_price_limit_locked INTEGER,
            price_limit_direction TEXT,
            has_market_quote INTEGER,
            has_trade_volume INTEGER,
            source TEXT NOT NULL,
            listing_date TEXT,
            delisting_date TEXT,
            suspension_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(code, session_date, effective_at, observed_at)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_hist_tradability_lookup"
        f" ON {ARCHIVE_TABLE}(code, session_date, effective_at, observed_at)"
    )
    return {"table": ARCHIVE_TABLE, "migration": MIGRATION_DESCRIPTION}


# ─────────────────────────────── 归档查询 ───────────────────────────────


def _row_to_evidence(row: Any) -> TradabilityEvidence:
    """把一行归档记录转成事实对象。

    必须同时支持两种行形态：``sqlite3.Row`` / 映射（按列名取），以及调用方用
    ``sqlite3.connect()`` 默认配置时得到的**普通 tuple**（按位置取）。

    只支持命名取值的后果很严重：默认连接的 ``SELECT *`` 返回 tuple，按下标用
    字符串取值会抛 ``TypeError``，被旧实现吞成 ``None``，于是每一列都变成未知、
    ``_visible_at`` 拒绝所有已落库的行，归档永久返回 ``UNKNOWN_STATE``——
    事实层静默失效，而且看起来像"没有数据"而不是像 bug。

    位置取值依赖 :data:`ARCHIVE_COLUMNS` 与建表/查询顺序一致，因此查询一律用
    显式列清单（``SELECT {ARCHIVE_COLUMNS}``），不使用 ``SELECT *``。
    """
    named = _row_mapping(row)
    if named is None:
        values = list(row)
        named = {
            column: values[index]
            for index, column in enumerate(ARCHIVE_COLUMNS)
            if index < len(values)
        }

    def _value(key: str) -> Any:
        return named.get(key)

    def _text_or_none(key: str) -> Optional[str]:
        raw = named.get(key)
        return None if raw is None else str(raw)

    return TradabilityEvidence(
        code=str(named.get("code") or ""),
        session_date=str(named.get("session_date") or ""),
        is_listed=_flag(_value("is_listed")),
        listing_date=_text_or_none("listing_date"),
        delisting_date=_text_or_none("delisting_date"),
        is_st=_flag(_value("is_st")),
        is_suspended=_flag(_value("is_suspended")),
        suspension_reason=_text_or_none("suspension_reason"),
        has_market_quote=_flag(_value("has_market_quote")),
        has_trade_volume=_flag(_value("has_trade_volume")),
        is_price_limit_locked=_flag(_value("is_price_limit_locked")),
        price_limit_direction=_direction(_value("price_limit_direction")),
        source=str(named.get("source") or ""),
        observed_at=str(named.get("observed_at") or ""),
        effective_at=str(named.get("effective_at") or ""),
    )


def _row_mapping(row: Any) -> Optional[dict]:
    """行 → 列名到值的映射；无法按名字取值时返回 ``None``（交给位置映射）。"""
    if row is None:
        return None
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {key: row[key] for key in keys()}
        except (IndexError, KeyError, TypeError):
            return None
    if isinstance(row, Mapping):
        return dict(row)
    return None


def _visible_at(evidence: TradabilityEvidence, decision_time: Any) -> bool:
    """证据在 ``decision_time`` 当时是否可见（两条都必须成立）。

    ``observed_at`` 走仓库唯一的 PIT 可见性口径
    (:func:`point_in_time.is_visible_at`)；``decision_time`` 缺失或不可解析时
    该函数返回 ``mode='live'``——那对应"实时兼容模式"，对**历史**归档是不成立的
    假设，所以这里显式要求 ``mode == 'strict'``，把 fail closed 做实。

    ``effective_at`` 不是"可用性"而是"生效顺序"，因此直接做时点比较。
    """
    if _text(decision_time) is None:
        return False
    verdict = PIT.is_visible_at(evidence.observed_at, decision_time)
    if verdict.get("mode") != "strict" or not verdict.get("visible"):
        return False
    effective = _instant(evidence.effective_at)
    reference = _instant(decision_time)
    if effective is None or reference is None:
        return False
    return effective <= reference


class TradabilityArchiveRepository:
    """事实资产的持久化与 PIT 查询。

    Provider **不得**直接写库：上游只产出原始记录，经 Normalizer 规范化后由本
    仓储落库，读取也一律经过本仓储的 ``decision_time`` 过滤。
    """

    def __init__(self, conn: sqlite3.Connection, *, cache_size: int = DEFAULT_CACHE_SIZE):
        self._conn = conn
        self._cache_size = max(0, int(cache_size))
        self._cache: "OrderedDict[tuple, Optional[TradabilityEvidence]]" = OrderedDict()
        # 上次对齐的数据库版本号，用来发现**别的连接**写入过归档。
        self._cache_version: Optional[int] = None

    # ── 写 ──
    def ensure_schema(self) -> dict:
        return ensure_schema(self._conn)

    def save(self, evidence: TradabilityEvidence) -> bool:
        """落库一条事实。同一 ``(code, session_date, effective_at, observed_at)``
        重复写入是 no-op。返回是否**新插入**。

        身份里含 ``observed_at``（双时态）：同一生效时点的记录可以被上游**修正**，
        例如最初观察到 ``is_st=False``、后来更正为 ``is_st=True``。若身份不含观测
        时点，这类修正会与原始记录撞唯一键并被 ``INSERT OR IGNORE`` 静默丢弃，
        修正之后的决策会继续使用过期的第一版事实——那正好破坏了本层存在的意义。

        选择 ``INSERT OR IGNORE`` 而不是 ``REPLACE``：同一条观测上的既有记录是
        已经发生过的事实，后到的写入不得覆盖它。
        """
        row = evidence.to_row()
        row["created_at"] = _now()
        cursor = self._conn.execute(
            f"""INSERT OR IGNORE INTO {ARCHIVE_TABLE}(
                    code, session_date, effective_at, observed_at, is_listed, is_st,
                    is_suspended, is_price_limit_locked, price_limit_direction,
                    has_market_quote,
                    has_trade_volume, source, listing_date, delisting_date,
                    suspension_reason, created_at)
                VALUES(:code, :session_date, :effective_at, :observed_at, :is_listed,
                       :is_st, :is_suspended, :is_price_limit_locked,
                       :price_limit_direction, :has_market_quote,
                       :has_trade_volume, :source, :listing_date, :delisting_date,
                       :suspension_reason, :created_at)""",
            row,
        )
        inserted = bool(cursor.rowcount)
        if inserted:
            self.clear_cache()
        return inserted

    def save_many(self, items: Iterable[TradabilityEvidence]) -> int:
        return sum(1 for item in items if self.save(item))

    # ── 读 ──
    def visible_evidence(
        self, code: Any, session: Any, decision_time: Any
    ) -> list:
        """该 ``(code, session)`` 在 ``decision_time`` 当时可见的全部事实。"""
        code_text = _text(code)
        session_text = _session(session)
        if code_text is None or session_text is None:
            return []
        rows = self._conn.execute(
            f"SELECT {_COLUMN_LIST} FROM {ARCHIVE_TABLE} "
            "WHERE code=? AND session_date=?",
            (code_text, session_text),
        ).fetchall()
        visible = [
            evidence
            for evidence in (_row_to_evidence(row) for row in rows)
            if _visible_at(evidence, decision_time)
        ]
        visible.sort(key=lambda item: (item.effective_at, item.observed_at))
        return visible

    def evidence_at(
        self, code: Any, session: Any, decision_time: Any
    ) -> Optional[TradabilityEvidence]:
        """当时**最新生效**的一条事实；没有可见记录 → ``None``（fail closed）。

        缓存键是 ``(code, session, decision_time)`` 三元组，而不是 ``(code, date)``：
        同一个日期在不同 ``decision_time`` 下的可见集合本来就不同，用二元键会把
        某个时点的结论错给另一个时点——那正是 PIT 污染本身。

        缓存还会按**数据库版本**失效（见 :meth:`_data_version`）。只在自己
        ``save()`` 时清缓存是不够的：入库与查询常常是两个实例、两条连接（摄取进程
        写、服务进程读），写入方清自己的私有缓存，读方的缓存却毫不知情，于是这个
        快速路径会**无限期**返回过期事实——后到的修正在读侧等于不存在。
        """
        code_text = _text(code)
        session_text = _session(session)
        reference = _canonical_instant(decision_time)
        if code_text is None or session_text is None or reference is None:
            return None
        self._sync_cache_with_database()
        key = (code_text, session_text, reference)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        visible = self.visible_evidence(code_text, session_text, reference)
        chosen = visible[-1] if visible else None
        if self._cache_size:
            self._cache[key] = chosen
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return chosen

    def _data_version(self) -> Optional[int]:
        """本连接看到的数据库版本号。

        SQLite 的 ``PRAGMA data_version`` 在**其他连接**提交后递增，在本连接自己
        写入时不变——正好是"外部是否改过归档"的信号。取不到时返回 ``None``，
        调用方据此退化为按自身写入失效（旧行为），而不会误判为"没变过"。
        """
        try:
            row = self._conn.execute("PRAGMA data_version").fetchone()
        except sqlite3.Error:  # pragma: no cover - 驱动/库不支持
            return None
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):  # pragma: no cover
            return None

    def _sync_cache_with_database(self) -> None:
        """数据库被外部改动过就丢掉整份缓存；版本不可得时不动缓存。"""
        current = self._data_version()
        if current is None:
            return
        if self._cache_version is None:
            self._cache_version = current
            return
        if current != self._cache_version:
            self._cache.clear()
            self._cache_version = current

    def fingerprint(
        self, code: Any, session: Any, decision_time: Any
    ) -> Optional[str]:
        evidence = self.evidence_at(code, session, decision_time)
        return evidence_fingerprint(evidence) if evidence is not None else None

    def count(self, code: Any = None) -> int:
        if code is None:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {ARCHIVE_TABLE}").fetchone()
        else:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {ARCHIVE_TABLE} WHERE code=?", (_text(code),)
            ).fetchone()
        return int(row[0]) if row else 0

    def clear_cache(self) -> None:
        self._cache.clear()


# ─────────────────────────────── 判断层 ───────────────────────────────


class TradabilityEvaluator:
    """把历史事实翻译成"当时能不能买 / 能不能卖"。

    只判断**事实**。禁止出现收益、策略评分、风险评分、因子评分——那些不属于这里，
    混进来会让"不可交易"变成"我不想交易"，两者在审计上完全不同。

    买入（全部必须**被证明为真**）::

        is_listed AND NOT is_suspended AND NOT ST AND has_market_quote
        AND has_trade_volume

    卖出::

        is_listed AND NOT is_suspended AND 行情存在

    核心事实为 ``None``（未知）时一律阻断并给 ``UNKNOWN_STATE``——"不知道"不能
    被当成"允许"。``is_price_limit_locked`` 是**追加**限制且**有方向**：涨停
    （``price_limit_direction == "up"``）只拦买、允许卖出；跌停只拦卖、允许买入；
    锁定为 ``True`` 但方向未知时两侧都拦（锁定事实已证、方向未知 → fail closed）。
    """

    @staticmethod
    def evaluate(
        evidence: TradabilityEvidence,
        *,
        decision_time: Any = None,
        fingerprint: Optional[str] = None,
    ) -> TradabilityDecision:
        reference = _canonical_instant(decision_time)
        buy_reason = TradabilityEvaluator._buy_reason(evidence)
        sell_reason = TradabilityEvaluator._sell_reason(evidence)
        return TradabilityDecision(
            code=evidence.code,
            session_date=evidence.session_date,
            decision_time=reference,
            can_buy=buy_reason is TradabilityReason.OK,
            can_sell=sell_reason is TradabilityReason.OK,
            buy_block_reason=buy_reason,
            sell_block_reason=sell_reason,
            source=evidence.source,
            effective_at=evidence.effective_at,
            observed_at=evidence.observed_at,
            fingerprint=(
                fingerprint if fingerprint is not None else evidence_fingerprint(evidence)
            ),
            evidence_present=True,
            suspension_reason=evidence.suspension_reason,
        )

    @staticmethod
    def _listing_reason(evidence: TradabilityEvidence) -> Optional[TradabilityReason]:
        """上市状态：只有**被证明**为上市才放行。"""
        if evidence.is_listed is True:
            return None
        if evidence.is_listed is False:
            delisted = evidence.delisting_date is not None and (
                evidence.session_date >= evidence.delisting_date
            )
            return TradabilityReason.DELISTED if delisted else TradabilityReason.NOT_LISTED
        return TradabilityReason.UNKNOWN_STATE

    @staticmethod
    def _buy_reason(evidence: TradabilityEvidence) -> TradabilityReason:
        reason = TradabilityEvaluator._listing_reason(evidence)
        if reason is not None:
            return reason
        if evidence.is_suspended is True:
            return TradabilityReason.SUSPENDED
        if evidence.is_suspended is None:
            return TradabilityReason.UNKNOWN_STATE
        if evidence.is_st is True:
            return TradabilityReason.ST_RESTRICTED
        if evidence.is_st is None:
            return TradabilityReason.UNKNOWN_STATE
        if evidence.has_market_quote is not True:
            return (
                TradabilityReason.NO_QUOTE
                if evidence.has_market_quote is False
                else TradabilityReason.UNKNOWN_STATE
            )
        if evidence.has_trade_volume is not True:
            return (
                TradabilityReason.NO_VOLUME
                if evidence.has_trade_volume is False
                else TradabilityReason.UNKNOWN_STATE
            )
        if evidence.is_price_limit_locked is True:
            # 涨停只拦买；方向未知时仍拦买（锁定事实已证，方向未知 → fail closed）
            if evidence.price_limit_direction == PRICE_LIMIT_DOWN:
                return TradabilityReason.OK
            return TradabilityReason.BUY_LIMIT_LOCKED
        return TradabilityReason.OK

    @staticmethod
    def _sell_reason(evidence: TradabilityEvidence) -> TradabilityReason:
        reason = TradabilityEvaluator._listing_reason(evidence)
        if reason is not None:
            return reason
        if evidence.is_suspended is True:
            return TradabilityReason.SUSPENDED
        if evidence.is_suspended is None:
            return TradabilityReason.UNKNOWN_STATE
        if evidence.has_market_quote is not True:
            return (
                TradabilityReason.NO_QUOTE
                if evidence.has_market_quote is False
                else TradabilityReason.UNKNOWN_STATE
            )
        if evidence.is_price_limit_locked is True:
            # 跌停只拦卖；方向未知时仍拦卖（锁定事实已证，方向未知 → fail closed）
            if evidence.price_limit_direction == PRICE_LIMIT_UP:
                return TradabilityReason.OK
            return TradabilityReason.SELL_LIMIT_LOCKED
        return TradabilityReason.OK


def evaluate(
    evidence: TradabilityEvidence,
    *,
    decision_time: Any = None,
    fingerprint: Optional[str] = None,
) -> TradabilityDecision:
    """模块级便捷入口，语义与 :meth:`TradabilityEvaluator.evaluate` 一致。"""
    return TradabilityEvaluator.evaluate(
        evidence, decision_time=decision_time, fingerprint=fingerprint
    )


# ─────────────────────────────── Provider ───────────────────────────────


class TradabilityProvider:
    """事实来源接口。第一版只定义契约，**不绑定**任何具体数据源。

    上游实现负责取数，产出原始记录；规范化与落库分别由 ``normalize_record`` 和
    :class:`TradabilityArchiveRepository` 完成，Provider 自己**不写数据库**。
    """

    def fetch(self, code: str, session: str) -> Optional[Mapping[str, Any]]:
        raise NotImplementedError


# ─────────────────────────────── 顶层入口 ───────────────────────────────


def tradability_at(
    code: Any,
    session: Any,
    *,
    decision_time: Any,
    repository: TradabilityArchiveRepository,
) -> TradabilityDecision:
    """历史日期上"当时能不能买 / 能不能卖"的最终结论。

    没有可见事实记录时返回 ``UNKNOWN_STATE``（买卖都阻断），并保持审计字段存在。
    """
    code_text = _text(code) or ""
    session_text = _session(session) or str(session or "")
    reference = _canonical_instant(decision_time)
    evidence = repository.evidence_at(code, session, decision_time)
    if evidence is None:
        return TradabilityDecision(
            code=code_text,
            session_date=session_text,
            decision_time=reference,
            can_buy=False,
            can_sell=False,
            buy_block_reason=TradabilityReason.UNKNOWN_STATE,
            sell_block_reason=TradabilityReason.UNKNOWN_STATE,
            source=None,
            effective_at=None,
            observed_at=None,
            fingerprint=None,
            evidence_present=False,
        )
    return TradabilityEvaluator.evaluate(
        evidence,
        decision_time=decision_time,
        fingerprint=evidence_fingerprint(evidence),
    )
