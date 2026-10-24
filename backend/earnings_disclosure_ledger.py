# -*- coding: utf-8 -*-
"""业绩公告事件的 **durable ledger + typed projection**（owner 层）。

分工只有一句话：

```text
earnings_disclosure_facts     纯判定：公告原文说了什么、claim 是否忠实于原文
earnings_disclosure_ledger    持久事实：谁在什么时候第一次观测到它、它现在是否仍然有效
```

三层语义刻意分开，因为它们的失败代价不同：

* **可解析性**（facts 的 `body_unparsable`）是"我们读不懂这份版式"；
* **有效性**（本模块的 `superseded`）是"这份公告已经被修正公告取代"；
* **可用性**（`first_seen_at` / `availability_day`）是"系统当时到底知不知道"。

把它们压成一个状态字段，会让"读不懂"和"过期"在复盘时无法区分 —— 而这两者的处置
完全不同（补解析器 vs 换用新公告）。

──────────────────────── PIT：唯一权威是 first_seen_at ────────────────────────

``published_at`` 是**来源声称的**发布时间，只作描述性字段保存，**从不**参与可用性边界。
一篇 9/20 发布、9/21 才被摄取到的公告，在 ``as_of=9/20`` 必须不可见 —— 否则就是把
9/21 才知道的事情倒填进历史。这条与 ``news_learning`` 的 R27-B2C-5 一节是同一口径。

`first_seen_at` 由**调用方在摄取时给出的观测时刻**决定，且必须带时区；重复摄取
**不会**刷新它（第一次观测才是历史事实）。这里没有 `now()` 兜底。

──────────────────────── 三条写入纪律 ────────────────────────

1. **内容冲突 fail closed。** 同一个 ``announcement_id`` 再次进来时，标题/原文指纹/
   分类/数字任何一项变了 → 抛 ``announcement_content_conflict``，**不覆盖**已记录的行。
   公告被修正是**新公告、新行**，不是原地改写（原地改写会让"当时看到的是哪一版"消失）。
2. **修正链只向前。** 修正/更正公告落库时，把同一 (代码, 报告期, 公告类型) 下**更早**
   且尚未被取代的行标上 ``superseded_by``。这个字段只允许 ``NULL → value``，
   不允许回退，也不允许改指向。
3. **本模块不 commit。** 连接与事务归调用方；所有校验在写入前完成，写入只有一条
   INSERT（加至多两条 UPDATE），不存在半写状态。

──────────────────────── 本轮**没有**做的部分 ────────────────────────

```text
research adapter（evidence_ref_from_*）与其守卫登记     NOT IN THIS MODULE
持续编排 / 调度                                        NOT IN THIS MODULE
与 reported_profit_breakout 基线的"更早发现"对照        NOT IN THIS MODULE
```

因此本模块仍**不产生**可直接进研究链路的 ``ResearchEvidenceRef``；它产出的是 owner
typed projection，由下一步的 adapter 翻译成 research 证据。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import earnings_disclosure_facts as EDF

__all__ = (
    "EARNINGS_LEDGER_VERSION",
    "EARNINGS_EVENT_READ_LIMIT",
    "VERIFICATION_VERIFIED",
    "VERIFICATION_UNPARSABLE",
    "VERIFICATION_SUPERSEDED",
    "VERIFICATION_STATUSES",
    "INGEST_RECORDED",
    "INGEST_DUPLICATE",
    "INGEST_REJECTED",
    "INGEST_STATUSES",
    "VERIFICATION_METHOD_BODY_RECOMPUTE",
    "EarningsLedgerError",
    "IngestOutcome",
    "ClaimCheckRecord",
    "EarningsEventProjection",
    "ensure_schema",
    "record_announcement",
    "record_claim_check",
    "earnings_event_projections",
)


#: 事实契约版本。projection 自带它，版本漂移在构造期暴露，而不是在下游审计时才发现。
EARNINGS_LEDGER_VERSION = "earnings-disclosure-ledger-v1"

#: owner 日历。日边界是 ``as_of + 23:59:59+08:00``，与 facts 的口径一致。
TZ = ZoneInfo("Asia/Shanghai")

EARNINGS_EVENTS_TABLE = "earnings_disclosure_events"
EARNINGS_CLAIM_CHECKS_TABLE = "earnings_disclosure_claim_checks"

#: 一次 typed read 的默认上限（在 PIT 过滤**之后**应用）。
EARNINGS_EVENT_READ_LIMIT = 200

# ---------------------------------------------------------------------------
# 词汇
# ---------------------------------------------------------------------------

#: 原文复算出了数字，且这份公告没有被修正公告取代 —— 这是本 owner 唯一"通过"的状态。
VERIFICATION_VERIFIED = "verified"
#: 有原文，但版式不在 facts 声明的形状族里：读不懂，不是过期。
VERIFICATION_UNPARSABLE = "unparsable"
#: 同一 (代码, 报告期, 公告类型) 已有更晚的修正公告：数字本身可能自洽，但已不是当前事实。
VERIFICATION_SUPERSEDED = "superseded"

VERIFICATION_STATUSES = (
    VERIFICATION_VERIFIED,
    VERIFICATION_UNPARSABLE,
    VERIFICATION_SUPERSEDED,
)

#: 核验方法：从公告原文复算。刻意只有一个取值 —— 写"多源交叉"就是伪造维度。
VERIFICATION_METHOD_BODY_RECOMPUTE = "body_recompute"

INGEST_RECORDED = "recorded"
INGEST_DUPLICATE = "duplicate"
INGEST_REJECTED = "rejected"

INGEST_STATUSES = (INGEST_RECORDED, INGEST_DUPLICATE, INGEST_REJECTED)


class EarningsLedgerError(ValueError):
    """owner 层的拒绝 —— fail closed。

    ``reason`` 是稳定 machine code：

    * ``ledger_unavailable`` —— 表不存在 / 不可读（**不**创建、**不**抓取）；
    * ``bad_observed_at`` —— 观测时刻缺失、无法解析或没有时区（**不**回退墙钟）；
    * ``missing_announcement_id`` / ``missing_source`` —— 公告身份不完整；
    * ``announcement_content_conflict`` —— 同一公告 id 的内容变了（不覆盖）；
    * ``bad_as_of`` / ``bad_limit`` —— 读取参数非法；
    * ``unknown_event`` —— 复核记录指向不存在的事件。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason)
        super().__init__(self.reason if not detail else f"{self.reason}: {detail}")


# ---------------------------------------------------------------------------
# 时间 / 文本助手
# ---------------------------------------------------------------------------


def _parse_observed_at(value: Any) -> dt.datetime:
    """观测时刻必须带**可证明的时区**，并归一到 owner 日历。

    刻意没有任何 fallback：缺失 / 无法解析 / naive 一律拒绝。PIT 不可证明的事实
    不该被记下来，否则它会在某个 ``as_of`` 上出现，而没人能说明那时系统是否真的知道。
    """
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise EarningsLedgerError("bad_observed_at", "empty")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise EarningsLedgerError("bad_observed_at", repr(text)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EarningsLedgerError(
            "bad_observed_at", f"{parsed!r} 没有时区语义 —— 不得假设本机时区",
        )
    return parsed.astimezone(TZ)


def _format_instant(value: dt.datetime) -> str:
    return value.astimezone(TZ).isoformat(timespec="seconds")


def _as_of_boundary(value: Any) -> dt.datetime:
    """``'YYYY-MM-DD'`` → 该日历日在 owner 时区的日末边界。"""
    text = str(value or "").strip()
    try:
        day = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise EarningsLedgerError("bad_as_of", f"{value!r} 不是 YYYY-MM-DD") from exc
    if day.isoformat() != text:
        raise EarningsLedgerError("bad_as_of", f"{value!r} 不是规范日期")
    return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=TZ)


def _validated_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EarningsLedgerError("bad_limit", repr(value))
    return int(value)


def _decimal_text(value: Decimal | None) -> str | None:
    """Decimal → 规范文本（**不**用 float，避免读回来时变成另一个数）。"""
    if value is None:
        return None
    return format(value, "f")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _text_or_none(value: Any) -> str | None:
    text = str(value if value is not None else "").strip()
    return text or None


def _select_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """执行 SELECT 并返回 dict 行，不依赖调用方设置的 ``row_factory``。"""
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {EARNINGS_EVENTS_TABLE}(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    announcement_id TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    kind TEXT NOT NULL,
    report_period TEXT NOT NULL,
    is_correction INTEGER NOT NULL,
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    verification_method TEXT NOT NULL,
    low_wan TEXT,
    high_wan TEXT,
    yoy_low_pct TEXT,
    yoy_high_pct TEXT,
    unit TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earnings_events_seen
    ON {EARNINGS_EVENTS_TABLE}(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_earnings_events_period
    ON {EARNINGS_EVENTS_TABLE}(code, report_period, kind);
CREATE TABLE IF NOT EXISTS {EARNINGS_CLAIM_CHECKS_TABLE}(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    claim_fingerprint TEXT NOT NULL,
    claim_json TEXT NOT NULL,
    status TEXT NOT NULL,
    disagreements TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earnings_claim_checks_event
    ON {EARNINGS_CLAIM_CHECKS_TABLE}(event_key, checked_at DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建立本 owner 的两张表（幂等）。**只有写入路径**才调用它。"""
    conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """一次摄取的结果（不含任何"可信度"评分）。"""

    status: str
    event_key: str | None
    reason: str | None
    verification_status: str | None


def _event_key(code: str, report_period: str, kind: str, announcement_id: str) -> str:
    return f"earnings|{code}|{report_period}|{kind}|{announcement_id}"


def _claim_fingerprint(claim: EDF.EarningsClaim) -> str:
    payload = {
        "kind": claim.kind,
        "report_period": claim.report_period,
        "metric": claim.metric,
        "low_wan": _decimal_text(claim.low_wan),
        "high_wan": _decimal_text(claim.high_wan),
        "yoy_low_pct": _decimal_text(claim.yoy_low_pct),
        "yoy_high_pct": _decimal_text(claim.yoy_high_pct),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_announcement(
    conn: sqlite3.Connection,
    *,
    code: Any,
    title: Any,
    announcement_id: Any,
    source_name: Any,
    source_type: Any,
    source_url: Any = None,
    published_at: Any = None,
    body: Any,
    observed_at: Any,
) -> IngestOutcome:
    """把一条公告落成 owner 事实。**不 commit**（事务归调用方）。

    处理顺序刻意是"先判身份，再算数字，最后判修正链"：

    ```text
    标题归不进业绩公告族   → rejected（带 machine reason，不写行）
    公告 id / 来源缺失     → 抛错（身份不完整的事实不该存在）
    原文复算失败           → 仍然写行，verification_status=unparsable（"看过但读不懂"）
    原文复算成功           → 写行 + 数字
    本行是修正公告         → 把同 (代码, 报告期, 类型) 更早的行标 superseded
    ```

    "读不懂也写行"是有意为之：它让"这份公告我们收到过、但解析器没覆盖"成为可审计
    的事实，而不是静默消失。代价是它**不可能**通过核验（状态不是 verified）。
    """
    meta = EDF.classify_announcement(title)
    event_code = str(code or "").strip()
    if not meta.supported:
        return IngestOutcome(INGEST_REJECTED, None, meta.unsupported_reason, None)
    if len(event_code) != 6 or not event_code.isdigit():
        raise EarningsLedgerError("missing_announcement_id", f"code={event_code!r}")
    announcement = str(announcement_id or "").strip()
    if not announcement:
        raise EarningsLedgerError("missing_announcement_id", "announcement_id 为空")
    name = str(source_name or "").strip()
    kind_source = str(source_type or "").strip()
    if not name or not kind_source:
        raise EarningsLedgerError("missing_source", "source_name / source_type 为空")
    observed = _parse_observed_at(observed_at)

    assert meta.kind is not None and meta.report_period is not None
    event_key = _event_key(event_code, meta.report_period, meta.kind, announcement)
    body_text = str(body or "")
    body_sha256 = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

    figures: EDF.ReportedFigures | None
    try:
        figures = EDF.parse_reported_figures(meta.kind, body_text)
    except EDF.EarningsFactError:
        figures = None
    status = VERIFICATION_VERIFIED if figures is not None else VERIFICATION_UNPARSABLE

    existing = _select_dicts(
        conn,
        f"SELECT * FROM {EARNINGS_EVENTS_TABLE} WHERE announcement_id = ?",
        (announcement,),
    )
    if existing:
        row = existing[0]
        same = (
            row["event_key"] == event_key
            and row["body_sha256"] == body_sha256
            and _decimal_or_none(row["low_wan"])
            == (figures.low_wan if figures is not None else None)
            and _decimal_or_none(row["high_wan"])
            == (figures.high_wan if figures is not None else None)
        )
        if not same:
            # 同一公告 id 的内容变了：这是"当时看到的是哪一版"被抹掉的开始，必须拒绝。
            raise EarningsLedgerError(
                "announcement_content_conflict",
                f"announcement_id={announcement!r} 已存在的记录与本次内容不一致",
            )
        # 重复摄取不刷新 first_seen_at：第一次观测才是历史事实。
        return IngestOutcome(INGEST_DUPLICATE, event_key, None, str(row["verification_status"]))

    stamp = _format_instant(observed)
    conn.execute(
        f"INSERT INTO {EARNINGS_EVENTS_TABLE}("
        "event_key, announcement_id, code, kind, report_period, is_correction, title,"
        " source_name, source_type, source_url, published_at, first_seen_at, body_sha256,"
        " verification_status, verification_method, low_wan, high_wan, yoy_low_pct,"
        " yoy_high_pct, unit, superseded_by, created_at, updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_key, announcement, event_code, meta.kind, meta.report_period,
            1 if meta.is_correction else 0, str(title or "").strip(), name, kind_source,
            _text_or_none(source_url), _text_or_none(published_at), stamp, body_sha256,
            status, VERIFICATION_METHOD_BODY_RECOMPUTE,
            _decimal_text(figures.low_wan) if figures else None,
            _decimal_text(figures.high_wan) if figures else None,
            _decimal_text(figures.yoy_low_pct) if figures else None,
            _decimal_text(figures.yoy_high_pct) if figures else None,
            figures.unit if figures else None, None, stamp, stamp,
        ),
    )

    if meta.is_correction:
        _supersede_earlier(conn, event_key=event_key, announcement=announcement,
                           code=event_code, report_period=meta.report_period,
                           kind=meta.kind, observed=observed, stamp=stamp)
    return IngestOutcome(INGEST_RECORDED, event_key, None, status)


def _supersede_earlier(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    announcement: str,
    code: str,
    report_period: str,
    kind: str,
    observed: dt.datetime,
    stamp: str,
) -> None:
    """把同一 (代码, 报告期, 类型) 下**更早**且仍为当前的行标成已被取代。

    "单向"这条不变量**只有一处**判定：下面 UPDATE 的 ``superseded_by IS NULL``。
    刻意不在上面的 SELECT 里再写一遍同义过滤 —— 两处等价的 fail-closed 会互相掩盖，
    使"某个守卫被删掉"这类退化在一次变异里根本看不出来（R27-B2C-4B 的教训）。
    SELECT 只负责把候选捞出来，权威判据留在写入语句里（它同时挡住并发下的重复标记）。

    这一字段因此是**单向**的：一旦指向某份修正公告，就不会被后来的第二次修正改写
    指向 —— 后者会让"这份数据是被谁取代的"这段历史凭空改变。更新同时把
    ``verification_status`` 推到 ``superseded``，但**保留**原来的数字：过期≠错误，
    复盘时要能看到当时拿到的数。
    """
    candidates = _select_dicts(
        conn,
        f"SELECT event_key, first_seen_at, superseded_by FROM {EARNINGS_EVENTS_TABLE}"
        " WHERE code = ? AND report_period = ? AND kind = ? AND event_key != ?",
        (code, report_period, kind, event_key),
    )
    for row in candidates:
        earlier = _parse_observed_at(row["first_seen_at"])
        if earlier >= observed:
            continue
        conn.execute(
            f"UPDATE {EARNINGS_EVENTS_TABLE}"
            " SET superseded_by = ?, verification_status = ?, updated_at = ?"
            " WHERE event_key = ? AND superseded_by IS NULL",
            (announcement, VERIFICATION_SUPERSEDED, stamp, row["event_key"]),
        )


def record_claim_check(
    conn: sqlite3.Connection,
    *,
    event_key: Any,
    claim: Any,
    verdict: Any,
    checked_at: Any,
) -> str:
    """记录一次"AI 提出的数字 vs 公告原文"的复核结论。**不 commit**。

    ``verdict`` 必须是 ``facts.verify_earnings_claim`` 签发的
    :class:`EDF.ClaimVerification`（没有公开构造器），本函数只负责持久化 ——
    判定与容差只有 facts 一个权威，这里不重新判断任何东西。

    返回新记录的 ``claim_fingerprint``。
    """
    key = str(event_key or "").strip()
    if type(claim) is not EDF.EarningsClaim:
        raise TypeError(
            f"claim must be a typed EarningsClaim, got {type(claim).__name__}"
        )
    if type(verdict) is not EDF.ClaimVerification:
        raise TypeError(
            "verdict must be the ClaimVerification issued by "
            "earnings_disclosure_facts.verify_earnings_claim, got "
            f"{type(verdict).__name__} — 复核结论不能由调用方自述"
        )
    known = _select_dicts(
        conn, f"SELECT body_sha256 FROM {EARNINGS_EVENTS_TABLE} WHERE event_key = ?", (key,),
    )
    if not known:
        raise EarningsLedgerError("unknown_event", key)
    if known[0]["body_sha256"] != verdict.body_sha256:
        # 结论是对**另一份原文**做的。存下来就等于把复核挂到了一条不对应的事实上。
        raise EarningsLedgerError(
            "announcement_content_conflict",
            f"复核结论的 body_sha256 与事件 {key!r} 记录的原著不一致",
        )
    fingerprint = _claim_fingerprint(claim)
    conn.execute(
        f"INSERT INTO {EARNINGS_CLAIM_CHECKS_TABLE}("
        "event_key, claim_fingerprint, claim_json, status, disagreements,"
        " body_sha256, checked_at) VALUES (?,?,?,?,?,?,?)",
        (
            key, fingerprint,
            json.dumps({
                "kind": claim.kind, "report_period": claim.report_period,
                "metric": claim.metric, "low_wan": _decimal_text(claim.low_wan),
                "high_wan": _decimal_text(claim.high_wan),
                "yoy_low_pct": _decimal_text(claim.yoy_low_pct),
                "yoy_high_pct": _decimal_text(claim.yoy_high_pct),
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            verdict.status, json.dumps(list(verdict.disagreements)),
            verdict.body_sha256, _format_instant(_parse_observed_at(checked_at)),
        ),
    )
    return fingerprint


# ---------------------------------------------------------------------------
# typed projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimCheckRecord:
    """一条历史复核记录（只读展示形状）。"""

    claim_fingerprint: str
    status: str
    disagreements: tuple[str, ...]
    checked_at: str

    def as_dict(self) -> Mapping:
        return {
            "claim_fingerprint": self.claim_fingerprint,
            "status": self.status,
            "disagreements": list(self.disagreements),
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True, init=False)
class EarningsEventProjection:
    """owner 对**一条业绩公告事实**的正式投影。

    **没有公开构造器。** ``EarningsEventProjection(...)`` 一律抛 ``TypeError``；
    唯一签发路径是 :func:`earnings_event_projections`（它再走私有
    :func:`_issue_earnings_event_projection`）。否则任何调用方都能自述
    ``first_seen_at`` / ``verification_status`` 然后声称"这是 owner 事实" ——
    PIT 与核验结论就都只是一句声明。

    字段归属刻意分明：

    * ``first_seen_at`` / ``availability_day`` —— 可用性（PIT），只由系统观测时刻派生；
    * ``published_at`` —— 来源声称的发布时间，**描述性**，不参与任何边界计算；
    * ``verification_status`` —— 本 owner 的三态结论（verified / unparsable / superseded）；
    * ``superseded_by`` —— 取代它的公告 id（无则为 ``None``）；
    * ``latest_claim`` —— 最近一次 AI 数字复核记录（没有就是 ``None``，**不补假值**）。
    """

    version: str
    identity: str
    announcement_id: str
    code: str
    kind: str
    report_period: str
    is_correction: bool
    title: str
    source_name: str
    source_type: str
    source_url: str | None
    published_at: str | None
    first_seen_at: str
    availability_day: str
    body_sha256: str
    verification_status: str
    verification_method: str
    low_wan: Decimal | None
    high_wan: Decimal | None
    yoy_low_pct: Decimal | None
    yoy_high_pct: Decimal | None
    unit: str | None
    superseded_by: str | None
    latest_claim: ClaimCheckRecord | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "EarningsEventProjection has no public constructor: 调用方不能自述一条业绩公告"
            "事实的身份 / PIT / 核验结论。请使用 "
            "earnings_disclosure_ledger.earnings_event_projections(conn, as_of=...)"
        )

    def __post_init__(self) -> None:
        if self.version != EARNINGS_LEDGER_VERSION:
            raise ValueError(
                f"projection version {self.version!r} != {EARNINGS_LEDGER_VERSION!r}"
            )
        if self.verification_status not in VERIFICATION_STATUSES:
            raise ValueError(
                f"unknown verification status {self.verification_status!r}; "
                f"allowed: {VERIFICATION_STATUSES}"
            )
        if self.kind not in EDF.ANNOUNCEMENT_KINDS:
            raise ValueError(f"unknown announcement kind {self.kind!r}")
        for name in ("identity", "announcement_id", "title", "source_name",
                     "source_type", "body_sha256", "verification_method"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"projection field {name} must be non-empty")
            object.__setattr__(self, name, value)
        if len(str(self.code)) != 6 or not str(self.code).isdigit():
            raise ValueError(f"projection code {self.code!r} 不是六位数字代码")
        instant = _parse_observed_at(self.first_seen_at)
        object.__setattr__(self, "availability_day", instant.date().isoformat())
        # 状态与数字必须自洽：verified 必须有数字，unparsable 必须没有。
        has_figures = self.low_wan is not None and self.high_wan is not None
        if self.verification_status == VERIFICATION_UNPARSABLE and has_figures:
            raise ValueError("unparsable 投影不得携带数字 —— 读不懂就不该有结论")
        if self.verification_status in (VERIFICATION_VERIFIED, VERIFICATION_SUPERSEDED):
            if not has_figures:
                raise ValueError(
                    f"{self.verification_status} 投影必须携带复算出的数字"
                )
        if self.verification_status == VERIFICATION_SUPERSEDED:
            if not self.superseded_by:
                raise ValueError("superseded 投影必须指明取代它的公告 id")
        if self.verification_status != VERIFICATION_SUPERSEDED and self.superseded_by:
            raise ValueError(
                f"superseded_by={self.superseded_by!r} 与状态 "
                f"{self.verification_status!r} 不自洽"
            )

    @property
    def is_owner_verified(self) -> bool:
        """owner 是否判定这条事实**通过**了自己的核验（原文复算 + 未被取代）。"""
        return self.verification_status == VERIFICATION_VERIFIED

    def as_dict(self) -> Mapping:
        """canonical 形状（审计 / 指纹共用）。"""
        return {
            "version": self.version,
            "identity": self.identity,
            "announcement_id": self.announcement_id,
            "code": self.code,
            "kind": self.kind,
            "report_period": self.report_period,
            "is_correction": self.is_correction,
            "title": self.title,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "published_at": self.published_at,
            "first_seen_at": self.first_seen_at,
            "availability_day": self.availability_day,
            "body_sha256": self.body_sha256,
            "verification_status": self.verification_status,
            "verification_method": self.verification_method,
            "low_wan": _decimal_text(self.low_wan),
            "high_wan": _decimal_text(self.high_wan),
            "yoy_low_pct": _decimal_text(self.yoy_low_pct),
            "yoy_high_pct": _decimal_text(self.yoy_high_pct),
            "unit": self.unit,
            "superseded_by": self.superseded_by,
            "latest_claim": self.latest_claim.as_dict() if self.latest_claim else None,
        }


def _issue_earnings_event_projection(**fields: Any) -> EarningsEventProjection:
    """签发一条投影。唯一调用点是 :func:`earnings_event_projections`。"""
    projection = object.__new__(EarningsEventProjection)
    for name, value in fields.items():
        object.__setattr__(projection, name, value)
    projection.__post_init__()
    return projection


_EVENT_COLUMNS = (
    "event_key, announcement_id, code, kind, report_period, is_correction, title,"
    " source_name, source_type, source_url, published_at, first_seen_at, body_sha256,"
    " verification_status, verification_method, low_wan, high_wan, yoy_low_pct,"
    " yoy_high_pct, unit, superseded_by"
)


def _latest_claims(conn: sqlite3.Connection) -> dict[str, ClaimCheckRecord]:
    """每个事件最近一次的复核记录（一次查询，避免逐行 N+1）。"""
    rows = _select_dicts(
        conn,
        f"SELECT event_key, claim_fingerprint, status, disagreements, checked_at"
        f" FROM {EARNINGS_CLAIM_CHECKS_TABLE} ORDER BY checked_at DESC, id DESC",
    )
    latest: dict[str, ClaimCheckRecord] = {}
    for row in rows:
        key = row["event_key"]
        if key in latest:
            continue
        try:
            disagreements = tuple(json.loads(row["disagreements"]) or ())
        except (TypeError, ValueError):
            disagreements = ()
        latest[key] = ClaimCheckRecord(
            claim_fingerprint=str(row["claim_fingerprint"]),
            status=str(row["status"]),
            disagreements=tuple(str(item) for item in disagreements),
            checked_at=str(row["checked_at"]),
        )
    return latest


def earnings_event_projections(
    conn: sqlite3.Connection,
    *,
    as_of: Any,
    limit: Any = EARNINGS_EVENT_READ_LIMIT,
) -> tuple[EarningsEventProjection, ...]:
    """owner 的**唯一** public typed 历史读入口。

    ``as_of`` 是必填关键字参数且没有默认值：一个能同时表示"当前"与"历史某日"的双义
    接口后面很难审。本函数只回答"到 ``as_of`` 这个日历日为止，系统**当时**已经观测到
    哪些业绩公告"：

    ```text
    保留条件：  first_seen_at <= as_of 当日 23:59:59（owner 时区）
    排除条件：  first_seen_at >  as_of        ⇒ 那时系统还不知道它
    ```

    ``published_at`` **不参与边界计算**。**不联网、不写库**：只发 SELECT，刻意**不**
    调用 :func:`ensure_schema`（那会写 DDL）—— 表不存在时 fail closed 成
    ``ledger_unavailable``，**不**建表、**不**回退去抓 live 公告、**不**做 backfill。
    历史缺数据就是"当时没有证据"，不是"现在补一条再假装当时知道"。

    刻意**没有** ``latest`` / ``recent`` / ``today`` 这类隐式回退入口。
    """
    boundary = _as_of_boundary(as_of)
    bound = _validated_limit(limit)
    try:
        rows = _select_dicts(
            conn, f"SELECT {_EVENT_COLUMNS} FROM {EARNINGS_EVENTS_TABLE}",
        )
        claims = _latest_claims(conn)
    except sqlite3.OperationalError as exc:
        raise EarningsLedgerError(
            "ledger_unavailable", f"业绩公告 ledger 不可读：{exc}",
        ) from exc

    available: list[tuple[dt.datetime, str, EarningsEventProjection]] = []
    for row in rows:
        instant = _parse_observed_at(row["first_seen_at"])
        if instant > boundary:
            continue
        projection = _issue_earnings_event_projection(
            version=EARNINGS_LEDGER_VERSION,
            identity=row["event_key"],
            announcement_id=row["announcement_id"],
            code=row["code"],
            kind=row["kind"],
            report_period=row["report_period"],
            is_correction=bool(row["is_correction"]),
            title=row["title"],
            source_name=row["source_name"],
            source_type=row["source_type"],
            source_url=_text_or_none(row["source_url"]),
            published_at=_text_or_none(row["published_at"]),
            first_seen_at=row["first_seen_at"],
            availability_day="",
            body_sha256=row["body_sha256"],
            verification_status=row["verification_status"],
            verification_method=row["verification_method"],
            low_wan=_decimal_or_none(row["low_wan"]),
            high_wan=_decimal_or_none(row["high_wan"]),
            yoy_low_pct=_decimal_or_none(row["yoy_low_pct"]),
            yoy_high_pct=_decimal_or_none(row["yoy_high_pct"]),
            unit=_text_or_none(row["unit"]),
            superseded_by=_text_or_none(row["superseded_by"]),
            latest_claim=claims.get(row["event_key"]),
        )
        available.append((instant, projection))
    # 固定顺序：先按 owner 观测时刻，再按 owner identity。
    available.sort(key=lambda item: (item[0], item[1].identity))
    return tuple(item[1] for item in available[:bound])
