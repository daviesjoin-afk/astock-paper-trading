# -*- coding: utf-8 -*-
"""R24 —— Market Data 的**纯契约**：行情事实的状态语义与 freshness policy。

本模块只回答三个问题：

1. **在某个 freshness policy 下，系统目前拥有什么经过验证的市场事实？**
2. **这条事实可不可信？有多新？为什么不可用？**
3. **这次读取允许联网吗？**

──────────────────────── 为什么不合并成一句话 ────────────────────────

行情状态是**多个正交维度**，不是一个分数。把 ``fresh + verified + available``
压成 ``quality_score = 83`` 会让下游（Signal / Risk / AI Research / 前端）
只能猜。因此本契约保留四个独立维度：

    availability     available / unavailable
    freshness        fresh / stale / unknown
    verification     verified / single_source / disagreement /
                     unavailable / not_attempted
    as_of            这条事实对应的业务日

硬性禁止（由类型和 :func:`classify` 的穷举保证，而不是靠调用方自觉）::

    None        == "没有数据"        ← 未知不得当成"空"
    {}          == "网络失败"        ← 空容器不得当成"没数据"
    []          == "provider 冲突"   ← 空列表不得当成"不一致"
    "stale" 字符串 == 上面任何一个    ← 同一个原因不得有五个名字

「有最后一次可信 snapshot，但过期」（STALE）与「完全没有可信 snapshot」
（UNAVAILABLE）是**两个不同结论**，绝不合并；provider A/B 冲突是第三个结论
（verification=disagreement），绝不静默挑一个。

──────────────────────── 能力边界（诚实声明） ────────────────────────

本 contract **不**做 I/O：不开数据库、不联网、不读文件、不读墙上时钟
（``now`` 一律由调用方显式传入）。因此它**不可能**偷偷发起 provider refresh，
也**不可能**出现 ``date.today()`` 式的 current-fill。

* 它**能**判定：给定一份已观测的 snapshot 与一个 as-of 请求，这条事实是否
  可证明、是否新鲜、是否经过多源核验。
* 它**不能**制造数据：没有 snapshot 就是 UNAVAILABLE，绝不构造 0 / 默认指数 /
  昨天值冒充今天。

同一业务 freshness 规则只有一个来源（:data:`LIVE_MARKET_POLICY` 等），
散落在调用层的 ``max_age=240`` 由本模块统一。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping

__all__ = [
    # availability
    "AVAILABILITY_AVAILABLE", "AVAILABILITY_UNAVAILABLE", "AVAILABILITIES",
    # freshness
    "FRESHNESS_FRESH", "FRESHNESS_STALE", "FRESHNESS_UNKNOWN", "FRESHNESSES",
    # verification
    "VERIFICATION_VERIFIED", "VERIFICATION_SINGLE_SOURCE",
    "VERIFICATION_DISAGREEMENT", "VERIFICATION_UNAVAILABLE",
    "VERIFICATION_NOT_ATTEMPTED", "VERIFICATIONS",
    # verification method
    "VERIFICATION_METHOD_CROSS_SOURCE", "VERIFICATION_METHOD_COVERAGE_INTEGRITY",
    "VERIFICATION_METHOD_NONE", "VERIFICATION_METHODS",
    "is_cross_source_verified",
    # access mode
    "ACCESS_READ", "ACCESS_REFRESH", "ACCESS_MODES",
    # reasons
    "REASON_MISSING", "REASON_STALE", "REASON_INCOMPLETE",
    "REASON_PROVIDER_UNAVAILABLE", "REASON_REFRESH_FAILED",
    "REASON_CROSS_SOURCE_FAILED", "REASON_ASOF_UNPROVABLE",
    "REASON_ASOF_MISMATCH", "REASONS",
    # status label
    "STATUS_FRESH", "STATUS_STALE", "STATUS_DEGRADED", "STATUS_UNAVAILABLE",
    "STATUS_UNVERIFIED", "STATUSES",
    # policy
    "MarketDataPolicy", "LIVE_MARKET_POLICY", "AUCTION_PRESELECTION_POLICY",
    "OPENING_EVENT_POLICY", "UNIVERSE_BUILD_POLICY", "ATTRIBUTION_POLICY",
    "CLOSE_SNAPSHOT_POLICY", "MARKET_HEALTH_POLICY", "POLICIES", "policy_named",
    "CROSS_SECTION_MIN_FRESH_RATIO", "fresh_ratio",
    # snapshot / reading
    "MarketDataSnapshot", "MarketDataReading",
    # helpers
    "access_mode_allows_network", "canonical_day", "classify",
    "reading_for_refresh_failure", "unavailable_reading",
]

# ---------------------------------------------------------------------------
# availability —— 系统到底有没有这条事实
# ---------------------------------------------------------------------------

#: 系统拥有这条行情事实（freshness / verification 另行判定）。
AVAILABILITY_AVAILABLE = "available"
#: 系统**完全没有**可信事实。绝不构造 0 / {} / 默认指数来"填"这个洞。
AVAILABILITY_UNAVAILABLE = "unavailable"
AVAILABILITIES = (AVAILABILITY_AVAILABLE, AVAILABILITY_UNAVAILABLE)

# ---------------------------------------------------------------------------
# freshness —— 这条事实有多新（相对一个明确的 policy）
# ---------------------------------------------------------------------------

#: 观测时点在 policy 容忍窗口内。
FRESHNESS_FRESH = "fresh"
#: 有最后一次可信观测，但已超出 policy 窗口。**不是** unavailable。
FRESHNESS_STALE = "stale"
#: 观测时点不可解析，无法判定新鲜度。既不能算 fresh 也不算 stale。
FRESHNESS_UNKNOWN = "unknown"
FRESHNESSES = (FRESHNESS_FRESH, FRESHNESS_STALE, FRESHNESS_UNKNOWN)

# ---------------------------------------------------------------------------
# verification —— 这条事实经过什么核验
# ---------------------------------------------------------------------------

#: 多源核验通过（现状：主源 + 独立源在容差内一致）。
VERIFICATION_VERIFIED = "verified"
#: 只有单源证据：**可用但不能自称已核验**。
VERIFICATION_SINGLE_SOURCE = "single_source"
#: 多源之间超出容差 —— 两个源互相否证。绝不静默挑一个。
VERIFICATION_DISAGREEMENT = "disagreement"
#: 核验源本身不可用（异常/无返回/时间戳过期）；不等于"两个源不一致"。
VERIFICATION_UNAVAILABLE = "unavailable"
#: 本次读取根本没有发起核验（例如只读缓存路径）。
VERIFICATION_NOT_ATTEMPTED = "not_attempted"
VERIFICATIONS = (
    VERIFICATION_VERIFIED, VERIFICATION_SINGLE_SOURCE,
    VERIFICATION_DISAGREEMENT, VERIFICATION_UNAVAILABLE,
    VERIFICATION_NOT_ATTEMPTED,
)

# ---------------------------------------------------------------------------
# verification_method —— ``verified`` 到底"被什么验证过"（R24 复审修正）
# ---------------------------------------------------------------------------

#: 逐行/逐票的**第二独立来源**核验（例如主源 + 腾讯/新浪行情）。
#: 这才是"多源核验"的字面含义。
VERIFICATION_METHOD_CROSS_SOURCE = "cross_source"
#: **完整性与覆盖**核验：持久化 reader 记录了完整分页标记，且行数 / 唯一代码数 /
#: 期望覆盖率同时达标。它证明的是"这份横截面是完整的市场切片"，
#: **不**证明"每一行都有第二个源交叉确认过"。
VERIFICATION_METHOD_COVERAGE_INTEGRITY = "coverage_integrity"
#: 没有做任何核验（``not_attempted`` / 缓存只读直出）。
VERIFICATION_METHOD_NONE = "none"
VERIFICATION_METHODS = (
    VERIFICATION_METHOD_CROSS_SOURCE, VERIFICATION_METHOD_COVERAGE_INTEGRITY,
    VERIFICATION_METHOD_NONE,
)

#: 每个 verification 状态**允许**的 method —— 这是本轮复审的核心修正。
#:
#: 修正前：单源 Eastmoney 全市场快照只要完整性通过就直接标成
#: ``verification="verified"``，而 ``verified`` 的定义是"多源核验通过"。
#: 那会让一个**单源**快照在契约层看起来像双源验证过；R25/R27 直接消费这个
#: contract 时就会据此高估可信度。
#:
#: 修正后：``verified`` 的含义固定为"**该 kind 的 verification policy 已通过**"，
#: 而"通过了哪一套 policy"由 ``verification_method`` 显式表达，消费者必须同时
#: 读这两个字段，不得只看 ``verified`` 就假设是双源。
_VERIFICATION_METHODS_BY_STATE = {
    VERIFICATION_VERIFIED: (
        VERIFICATION_METHOD_CROSS_SOURCE, VERIFICATION_METHOD_COVERAGE_INTEGRITY,
    ),
    VERIFICATION_SINGLE_SOURCE: (VERIFICATION_METHOD_CROSS_SOURCE,),
    VERIFICATION_DISAGREEMENT: (VERIFICATION_METHOD_CROSS_SOURCE,),
    VERIFICATION_UNAVAILABLE: (VERIFICATION_METHOD_CROSS_SOURCE,),
    VERIFICATION_NOT_ATTEMPTED: (VERIFICATION_METHOD_NONE,),
}

# ---------------------------------------------------------------------------
# access mode —— 本轮最重要的 contract（§10 Network Policy）
# ---------------------------------------------------------------------------

#: **只读**：只允许读取当前已知事实，**绝不**同步发起 provider 网络刷新。
ACCESS_READ = "read"
#: **刷新/决策**：显式允许访问 provider。必须由调用方明确选择。
ACCESS_REFRESH = "refresh"
ACCESS_MODES = (ACCESS_READ, ACCESS_REFRESH)


def access_mode_allows_network(access_mode: str) -> bool:
    """唯一判据：只有 ``refresh`` 模式允许联网。未知模式一律 fail closed。"""
    return str(access_mode) == ACCESS_REFRESH


# ---------------------------------------------------------------------------
# reasons —— 稳定的失败语义（§47：同一个原因只有一个名字）
# ---------------------------------------------------------------------------

#: 完全没有持久化的 snapshot。
REASON_MISSING = "missing"
#: 有 snapshot 但超出 policy 窗口。
REASON_STALE = "stale"
#: snapshot 存在但结构上不完整（行数/覆盖率不达门槛）。
REASON_INCOMPLETE = "incomplete"
#: provider 不可达或返回不可用。
REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"
#: 显式刷新失败。
REASON_REFRESH_FAILED = "refresh_failed"
#: 多源核验失败（超容差或核验源不可用）。
REASON_CROSS_SOURCE_FAILED = "cross_source_failed"
#: 无法证明该 as-of 的事实（**禁止**回落到 current）。
REASON_ASOF_UNPROVABLE = "asof_unprovable"
#: snapshot 的业务日与请求的 as-of 不符。
REASON_ASOF_MISMATCH = "asof_mismatch"
REASONS = (
    REASON_MISSING, REASON_STALE, REASON_INCOMPLETE,
    REASON_PROVIDER_UNAVAILABLE, REASON_REFRESH_FAILED,
    REASON_CROSS_SOURCE_FAILED, REASON_ASOF_UNPROVABLE, REASON_ASOF_MISMATCH,
)

# ---------------------------------------------------------------------------
# 派生展示标签 —— 给前端/API 的单一投影（维度仍然全部保留）
# ---------------------------------------------------------------------------

#: 可核验、且满足 freshness。
STATUS_FRESH = "fresh"
#: 有事实但已过期（或结构不完整）。
STATUS_STALE = "stale"
#: 有事实但未经多源核验。
STATUS_DEGRADED = "degraded"
#: 没有任何可信事实。
STATUS_UNAVAILABLE = "unavailable"
#: 有事实但多源互相否证。
STATUS_UNVERIFIED = "unverified"
STATUSES = (
    STATUS_FRESH, STATUS_STALE, STATUS_DEGRADED,
    STATUS_UNAVAILABLE, STATUS_UNVERIFIED,
)

# ---------------------------------------------------------------------------
# freshness policy —— 同一业务规则只有一个来源（§14）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDataPolicy:
    """一个明确的业务 freshness 规则。

    ``max_age_seconds`` 是**观测时点**到 ``now`` 的容忍窗口。``name`` 用于
    审计与 reason 归因，让"为什么这条被判 stale"可追溯。

    ``min_fresh_ratio`` 是**横截面**要求：窗口内的行数占可解析总行数的比例
    必须达标。默认 0 表示"只看最新一条观测时点"，适用于单点事实（一条报价、
    一个指数）。**全市场横截面必须设正值** —— 否则"3999 条隔夜旧数据 + 1 条
    刚更新的数据"会因为是完整 payload、且最新一条够新而被判 fresh，
    而这显然不是一个可信的实时横截面。

    刻意不是配置框架：只有名字、一个窗口、一个比例。
    """

    name: str
    max_age_seconds: float
    min_fresh_ratio: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("market data policy requires a name")
        if not isinstance(self.max_age_seconds, (int, float)) or self.max_age_seconds < 0:
            raise ValueError(
                f"policy {self.name}: max_age_seconds must be a non-negative number"
            )
        if not isinstance(self.min_fresh_ratio, (int, float)) or not (
            0.0 <= self.min_fresh_ratio <= 1.0
        ):
            raise ValueError(
                f"policy {self.name}: min_fresh_ratio must be within [0, 1]"
            )


#: 横截面快照的默认覆盖要求（与既有 `_validated_live_universe` /
#: `main.health` 的"当日有效行达门槛"口径同源）。
CROSS_SECTION_MIN_FRESH_RATIO = 0.90


#: 全市场实时快照：周期扫描、市场门控、运行时读取、allocation-explain。
#: 这是唯一持有 240s 的地方；调用方不再各自写 ``max_age=240``。
#: 带 90% 覆盖要求：横截面的新鲜度是"多少行够新"，不是"最新那一行够新"。
LIVE_MARKET_POLICY = MarketDataPolicy(
    "live_market", 240.0, min_fresh_ratio=CROSS_SECTION_MIN_FRESH_RATIO,
)

#: 09:25 集合竞价预选：只认竞价窗口内的快照。
AUCTION_PRESELECTION_POLICY = MarketDataPolicy(
    "auction_preselection", 90.0, min_fresh_ratio=CROSS_SECTION_MIN_FRESH_RATIO,
)

#: 开盘事件监测：容忍窗口更短。
OPENING_EVENT_POLICY = MarketDataPolicy(
    "opening_event", 120.0, min_fresh_ratio=CROSS_SECTION_MIN_FRESH_RATIO,
)

#: 全市场名单构建：允许较宽松的重建窗口（名单是低频静态资产，覆盖比新鲜更
#: 重要，因此这里只要求新鲜度窗口、不额外要求比例）。
UNIVERSE_BUILD_POLICY = MarketDataPolicy("universe_build", 300.0)

#: 成交归因报告：盘后离线使用，窗口最宽。
ATTRIBUTION_POLICY = MarketDataPolicy("trade_attribution", 900.0)

#: 收盘/强制刷新：窗口为零，即"必须真的去取一次源"。
#: 既有的 ``max_age=0, force=True`` 语义由它统一表达，调用方不再各写一遍 0。
CLOSE_SNAPSHOT_POLICY = MarketDataPolicy("close_snapshot", 0.0)

#: ``/api/health`` 的 data-validity 展示：既有口径是盘中 1800s / 非盘中更宽。
#: 它**不**并入 ``LIVE_MARKET_POLICY`` 的 240s —— 两者业务问题不同：
#: 240s 回答"能不能拿它做实时决策"，1800s 回答"这份切片还值不值得展示"。
#: R24 复审前这个 1800s 硬编码在 ``main.health`` 里，属调用层第二份判决；
#: 现在由 authority 执行同一 policy。
MARKET_HEALTH_POLICY = MarketDataPolicy("market_health_display", 1800.0)

POLICIES = (
    LIVE_MARKET_POLICY, AUCTION_PRESELECTION_POLICY, OPENING_EVENT_POLICY,
    UNIVERSE_BUILD_POLICY, ATTRIBUTION_POLICY, CLOSE_SNAPSHOT_POLICY,
    MARKET_HEALTH_POLICY,
)

_POLICY_BY_NAME = {policy.name: policy for policy in POLICIES}


def policy_named(name: str) -> MarketDataPolicy:
    """按名字取 policy；未知名字 fail closed（不返回默认窗口）。"""
    try:
        return _POLICY_BY_NAME[str(name)]
    except KeyError:
        raise ValueError(f"unknown market data policy: {name!r}") from None


# ---------------------------------------------------------------------------
# 时间归一化 —— 全部显式，绝不读墙上时钟
# ---------------------------------------------------------------------------


def _freeze(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """把证据 mapping 冻结成只读视图；``None`` 归一成空。"""
    if not mapping:
        return MappingProxyType({})
    return MappingProxyType(dict(mapping))


def canonical_day(value: Any) -> str | None:
    """把日期归一成 ``YYYY-MM-DD``；无法解析即 ``None``（不猜）。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:10]
    try:
        return dt.date.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date().isoformat()


def _parse_instant(value: Any) -> dt.datetime | None:
    """把时间戳归一成带时区的 datetime；无法解析即 ``None``。"""
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        # 无偏移的源时间戳按 Asia/Shanghai 解释（与生产容器一致），
        # 绝不与 UTC 直接相减。
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return parsed


def _age_seconds(observed_at: Any, now: Any) -> float | None:
    """观测时点到 ``now`` 的间隔（**绝对值**）。

    取绝对值而不是有符号差值，是为了保留既有的 fail-closed 语义
    （``data_fetcher._fresh_full_snapshot_from_disk`` 同样用 ``abs()``）：
    源时间戳出现**未来漂移**时，那不是一个"比现在还新"的可信事实，而是
    一个不可采信的时点，必须按超窗处理，绝不因为它看起来"更新"就判 fresh。
    """
    observed = _parse_instant(observed_at)
    current = _parse_instant(now)
    if observed is None or current is None:
        return None
    return abs((current - observed).total_seconds())


# ---------------------------------------------------------------------------
# snapshot —— 权威输出的载荷
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDataSnapshot:
    """一条**已验证的市场事实**的不可变载体。

    ``rows`` 是 provider 归一化后的行情行（tuple，不可原位追加）。
    ``observed_at`` 是**源观测时点**（provider 揭示时间），``saved_at`` 是
    本地持久化时点；freshness 只按 ``observed_at`` 判定，因为磁盘写入时间
    晚于源时间会高估新鲜度。``observed_at`` 缺失时 freshness 判
    ``unknown``（绝不回落 ``saved_at``，那会把"抓取成功但源时间缺失"
    伪装成新鲜数据）。

    刻意**不**把多源核验压成布尔：``verification`` 是维度，
    ``verification_detail`` 保留逐源证据（价格差/涨跌差/日期一致性），
    上游不得自己重算。
    """

    kind: str
    rows: tuple = ()
    as_of: str | None = None
    observed_at: str | None = None
    saved_at: str | None = None
    source: str | None = None
    complete: bool = False
    expected_rows: int = 0
    verification: str = VERIFICATION_NOT_ATTEMPTED
    #: **这次** ``verified`` 是通过哪套 policy 得到的。消费者必须同时读它，
    #: 不得只看 ``verification == "verified"`` 就假设是双源核验。
    verification_method: str = VERIFICATION_METHOD_NONE
    verification_detail: Mapping[str, Any] = field(default_factory=dict)
    degraded_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple):
            object.__setattr__(self, "rows", tuple(self.rows or ()))
        if self.verification not in VERIFICATIONS:
            raise ValueError(
                f"snapshot {self.kind}: unknown verification {self.verification!r}"
            )
        allowed = _VERIFICATION_METHODS_BY_STATE.get(self.verification)
        if allowed is not None and self.verification_method not in allowed:
            raise ValueError(
                f"snapshot {self.kind}: verification={self.verification!r} cannot "
                f"carry verification_method={self.verification_method!r} "
                f"(allowed: {allowed})"
            )
        if self.degraded_reason is not None and self.degraded_reason not in REASONS:
            raise ValueError(
                f"snapshot {self.kind}: unknown reason {self.degraded_reason!r}"
            )
        frozen = _freeze(self.verification_detail)
        object.__setattr__(self, "verification_detail", frozen)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def by_code(self) -> dict[str, Mapping[str, Any]]:
        """按 code 建索引；无 code 的行被丢弃（与现有读模型同口径）。"""
        return {
            str(row.get("code")): row
            for row in self.rows
            if isinstance(row, Mapping) and row.get("code")
        }


# ---------------------------------------------------------------------------
# reading —— snapshot 在一个 policy 下的判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDataReading:
    """``MarketDataSnapshot`` + 判定结果。**只读路径的返回值。**

    ``snapshot`` 为 ``None`` **当且仅当** availability 是 unavailable。
    有 snapshot 但 stale 时 ``snapshot`` 仍然保留 —— 这正是 §28 要求的
    "stale positive control"：调用方必须能拿到最后一份可信事实，
    而不是被静默升级成 fresh 或被丢掉。
    """

    availability: str
    freshness: str
    status: str
    policy_name: str
    snapshot: MarketDataSnapshot | None = None
    reason: str | None = None
    age_seconds: float | None = None
    access_mode: str = ACCESS_READ

    def __post_init__(self) -> None:
        if self.availability not in AVAILABILITIES:
            raise ValueError(f"unknown availability {self.availability!r}")
        if self.freshness not in FRESHNESSES:
            raise ValueError(f"unknown freshness {self.freshness!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if self.reason is not None and self.reason not in REASONS:
            raise ValueError(f"unknown reason {self.reason!r}")
        if (self.availability == AVAILABILITY_UNAVAILABLE) != (self.snapshot is None):
            raise ValueError(
                "availability/snapshot disagree: unavailable iff snapshot is None"
            )

    @property
    def available(self) -> bool:
        return self.availability == AVAILABILITY_AVAILABLE

    @property
    def usable(self) -> bool:
        """可作为业务输入使用：有事实，且未被多源否证。"""
        return self.availability == AVAILABILITY_AVAILABLE and self.status in (
            STATUS_FRESH, STATUS_STALE, STATUS_DEGRADED,
        )

    def rows(self) -> tuple:
        return self.snapshot.rows if self.snapshot is not None else ()

    def projection(self) -> dict[str, Any]:
        """给前端/API 的稳定投影（§22）。

        前端只渲染，**不重算** freshness。provider 细节（重试次数、熔断、
        缓存键）刻意不出现在这里 —— 普通运行页面只需业务语义。
        """
        return {
            "status": self.status,
            "availability": self.availability,
            "freshness": self.freshness,
            "verification": (
                self.snapshot.verification if self.snapshot is not None
                else VERIFICATION_NOT_ATTEMPTED
            ),
            # ``verified`` 是"哪套 policy 通过了"。消费者必须同时读这一项：
            # coverage_integrity 不是多源交叉核验。
            "verification_method": (
                self.snapshot.verification_method if self.snapshot is not None
                else VERIFICATION_METHOD_NONE
            ),
            "as_of": self.snapshot.as_of if self.snapshot is not None else None,
            "observed_at": (
                self.snapshot.observed_at if self.snapshot is not None else None
            ),
            "age_seconds": (
                round(self.age_seconds, 1) if self.age_seconds is not None else None
            ),
            "reason": self.reason,
            "policy": self.policy_name,
            "row_count": self.snapshot.row_count if self.snapshot is not None else 0,
        }


# ---------------------------------------------------------------------------
# 唯一的分类函数 —— 数据驱动的稳定规则，不是散落的 if/elif（§25）
# ---------------------------------------------------------------------------

#: 判定顺序固定：先 availability，再 verification，最后 freshness。
#: 顺序本身是业务语义 —— "被否证"比"过期"更严重，"过期"比"未核验"更严重。
_VERIFICATION_RANK = {
    VERIFICATION_DISAGREEMENT: 0,
    VERIFICATION_UNAVAILABLE: 1,
    VERIFICATION_NOT_ATTEMPTED: 2,
    VERIFICATION_SINGLE_SOURCE: 3,
    VERIFICATION_VERIFIED: 4,
}


def unavailable_reading(
    policy: MarketDataPolicy,
    reason: str = REASON_MISSING,
    *,
    access_mode: str = ACCESS_READ,
) -> MarketDataReading:
    """构造一个明确的 unavailable 结论。绝不携带 payload。"""
    return MarketDataReading(
        availability=AVAILABILITY_UNAVAILABLE,
        freshness=FRESHNESS_UNKNOWN,
        status=STATUS_UNAVAILABLE,
        policy_name=policy.name,
        snapshot=None,
        reason=reason,
        age_seconds=None,
        access_mode=access_mode,
    )


def reading_for_refresh_failure(
    policy: MarketDataPolicy,
    *,
    last_known: MarketDataSnapshot | None = None,
    now: Any = None,
    reason: str = REASON_REFRESH_FAILED,
) -> MarketDataReading:
    """刷新失败的结论：**保留**最后一次可信事实，但降级标记。

    这是刷新路径失败的既有语义（"failed refresh 只回落 full-market cache"）：
    有旧事实就报 stale/degraded，没有才报 unavailable —— 两者绝不混淆。
    """
    if last_known is None:
        return unavailable_reading(policy, reason, access_mode=ACCESS_REFRESH)
    return classify(last_known, policy, now=now, access_mode=ACCESS_REFRESH,
                    refresh_failed=True)


def fresh_ratio(rows: Any, now: Any, max_age: float) -> float | None:
    """窗口内的行数占**可解析总行数**的比例；无可解析行时 ``None``。

    横截面新鲜度必须看比例，而不是"最新那一行够不够新"。
    """
    current = _parse_instant(now)
    if current is None:
        return None
    parsed = 0
    fresh = 0
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        age = _age_seconds(row.get("quote_at") or row.get("observed_at"), current)
        if age is None:
            continue
        parsed += 1
        if age <= max_age:
            fresh += 1
    if not parsed:
        return None
    return fresh / parsed


def _cross_section_freshness(
    rows: Any, now: dt.datetime | None, policy: MarketDataPolicy,
    newest_age: float | None,
) -> tuple[bool, float | None]:
    """横截面是否满足 policy 的要求；无比例要求时退化为"最新一条在窗口内"。

    ``newest_age`` 是调用方已经从 ``snapshot.observed_at`` 算好的最新观测间隔，
    因此单点 kind 不需要第二次遍历。返回 ``(satisfied, ratio)``。
    """
    if policy.min_fresh_ratio <= 0:
        return (
            newest_age is not None and newest_age <= policy.max_age_seconds
        ), None
    ratio = fresh_ratio(rows, now, policy.max_age_seconds)
    if ratio is None:
        return False, None
    return ratio >= policy.min_fresh_ratio, ratio


def classify(
    snapshot: MarketDataSnapshot | None,
    policy: MarketDataPolicy,
    *,
    now: Any,
    access_mode: str = ACCESS_READ,
    asof_day: str | None = None,
    refresh_failed: bool = False,
) -> MarketDataReading:
    """把一个 snapshot 在给定 policy / access mode 下判定成 reading。

    **纯函数**：无 I/O、无时钟、无全局状态。``now`` 必须显式传入。

    判定规则（每一条都是业务语义，不是实现细节）::

        没有 snapshot                        → UNAVAILABLE / missing
        as_of 无法证明（请求了别的业务日）      → UNAVAILABLE / asof_unprovable
        结构不完整                            → STALE     / incomplete
        多源不一致                            → UNVERIFIED/ cross_source_failed
        核验源不可用                          → DEGRADED  / cross_source_failed
        观测时点不可解析                       → STALE     / stale  (freshness=unknown)
        横截面新鲜覆盖不足 / 超窗              → STALE     / stale
        只有单源证据                          → DEGRADED
        通过该 kind 的 verification policy     → FRESH
        刷新失败但有旧事实                     → STALE     / refresh_failed
    """
    if snapshot is None:
        return unavailable_reading(policy, REASON_MISSING, access_mode=access_mode)

    # PIT：请求了明确业务日时，**只能**使用该日或更早的观测。
    # 无法证明即 fail closed —— 绝不拿 current snapshot 回填历史（§15）。
    if asof_day is not None:
        requested = canonical_day(asof_day)
        observed_day = canonical_day(snapshot.observed_at or snapshot.as_of)
        if requested is None or observed_day is None:
            return MarketDataReading(
                availability=AVAILABILITY_UNAVAILABLE,
                freshness=FRESHNESS_UNKNOWN,
                status=STATUS_UNAVAILABLE,
                policy_name=policy.name,
                snapshot=None,
                reason=REASON_ASOF_UNPROVABLE,
                age_seconds=None,
                access_mode=access_mode,
            )
        if observed_day > requested:
            return MarketDataReading(
                availability=AVAILABILITY_UNAVAILABLE,
                freshness=FRESHNESS_UNKNOWN,
                status=STATUS_UNAVAILABLE,
                policy_name=policy.name,
                snapshot=None,
                reason=REASON_ASOF_MISMATCH,
                age_seconds=None,
                access_mode=access_mode,
            )

    age = _age_seconds(snapshot.observed_at, now)
    deadline = _parse_instant(now)
    satisfied, ratio = _cross_section_freshness(
        snapshot.rows, deadline, policy, age,
    )
    detail = dict(snapshot.verification_detail)
    if ratio is not None:
        detail["fresh_ratio"] = round(ratio, 4)
        detail["min_fresh_ratio"] = policy.min_fresh_ratio

    def _reading(**overrides) -> MarketDataReading:
        base = {
            "policy_name": policy.name,
            "snapshot": _with_detail(snapshot, detail),
            "age_seconds": age,
            "access_mode": access_mode,
        }
        base.update(overrides)
        return MarketDataReading(**base)

    if not snapshot.complete:
        return _reading(
            availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_STALE,
            status=STATUS_STALE,
            reason=snapshot.degraded_reason or REASON_INCOMPLETE,
        )

    if snapshot.verification == VERIFICATION_DISAGREEMENT:
        return _reading(
            availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_UNKNOWN,
            status=STATUS_UNVERIFIED, reason=REASON_CROSS_SOURCE_FAILED,
        )

    if snapshot.verification == VERIFICATION_UNAVAILABLE:
        return _reading(
            availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_UNKNOWN,
            status=STATUS_DEGRADED, reason=REASON_CROSS_SOURCE_FAILED,
        )

    if refresh_failed:
        return _reading(
            availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_STALE,
            status=STATUS_STALE, reason=REASON_REFRESH_FAILED,
        )

    if not satisfied:
        # 源观测时点不可解析、或横截面新鲜覆盖不足 —— 都不能算 fresh。
        return _reading(
            availability=AVAILABILITY_AVAILABLE,
            freshness=FRESHNESS_UNKNOWN if age is None else FRESHNESS_STALE,
            status=STATUS_STALE, reason=REASON_STALE,
        )

    single_source = snapshot.verification in (
        VERIFICATION_SINGLE_SOURCE, VERIFICATION_NOT_ATTEMPTED,
    )
    return _reading(
        availability=AVAILABILITY_AVAILABLE, freshness=FRESHNESS_FRESH,
        status=STATUS_DEGRADED if single_source else STATUS_FRESH,
        reason=None if not single_source else REASON_CROSS_SOURCE_FAILED,
    )


def _with_detail(
    snapshot: MarketDataSnapshot, detail: Mapping[str, Any],
) -> MarketDataSnapshot:
    """带（补充后的）核验证据复制一份 snapshot；证据未变则原样返回。"""
    if detail == dict(snapshot.verification_detail):
        return snapshot
    return replace(snapshot, verification_detail=detail)


def worst_verification(verifications: Iterable[str]) -> str:
    """多个标的的核验状态聚合成最弱的一个（最保守），用于快照级判定。

    刻意用固定 rank 而不是布尔 ``all(...)``：需要把
    「冲突」与「核验源不可用」区分开，前者更严重。
    """
    found = [v for v in verifications if v in _VERIFICATION_RANK]
    if not found:
        return VERIFICATION_NOT_ATTEMPTED
    return min(found, key=lambda item: _VERIFICATION_RANK[item])


def verification_from_cross_status(status: Any) -> str:
    """把现有逐票 ``quote_validation`` 语义映射成本契约的核验维度。

    刻意复用**既有业务术语**而不是发明新枚举：
    ``cross_source_checked`` → verified，``cross_source_failed`` → disagreement，
    ``cross_source_unavailable`` → unavailable，``range_timestamp_checked`` →
    单源（只有区间/时间戳校验，没有第二源）。
    """
    text = str(status or "").strip()
    return {
        "cross_source_checked": VERIFICATION_VERIFIED,
        "cross_source_failed": VERIFICATION_DISAGREEMENT,
        "cross_source_unavailable": VERIFICATION_UNAVAILABLE,
        "range_timestamp_checked": VERIFICATION_SINGLE_SOURCE,
        "unverified": VERIFICATION_NOT_ATTEMPTED,
    }.get(text, VERIFICATION_NOT_ATTEMPTED)


def is_cross_source_verified(snapshot: MarketDataSnapshot | None) -> bool:
    """该快照是否**真的**通过了多源交叉核验。

    这是"``verified`` 不等于双源"的显式判据：``verified`` + method=coverage_integrity
    **不是**多源核验，只有 method=cross_source 才是。需要双源保证的消费者
    （例如 AI 调参门禁、R25/R27 的可信事实判定）必须调用它，而不是比较
    ``snapshot.verification == "verified"``。
    """
    if snapshot is None:
        return False
    return (
        snapshot.verification == VERIFICATION_VERIFIED
        and snapshot.verification_method == VERIFICATION_METHOD_CROSS_SOURCE
    )
