# -*- coding: utf-8 -*-
"""R27-B2A —— R27 research 的**唯一** append-only 持久化 owner。

回答一个问题：

    一个已经由 R27-A 契约派生好的 :class:`~ai_research_contract.ResearchHypothesis`，
    被**正式**记录在哪里？

    ResearchHypothesis
            │
            ▼
    append-only persistence          ← 本模块
            │
            ▼
    audit / read projection

──────────────── 这不是新的 authority ────────────────

``ai_research_runs`` 是 **audit ledger**：不是 market-data owner、不是 verification
owner、不是 signal / risk / promotion authority。因此：

* ``status`` / ``reason`` / ``confidence`` / ``authority`` / ``is_authoritative``
  **全部从 typed hypothesis 派生**，:func:`append_run` 签名里根本没有这些参数。
  若 status 可由调用方给出，"给一个没有证据的假设贴上 supported"就只是一个关键字参数。
* ``verification`` / ``verification_method`` / ``cross_source_verified`` 只**逐字记录**
  R27-A 投影给出的事实维度，本模块**不重算** —— 尤其绝不把 ``verified`` 重新解释成
  ``cross_source_verified``（``coverage_integrity`` 的 ``verified`` 不是逐票双源）。
* 读出来的 row 是**历史 research artifact**，不是重新核验过的实时证据。

**Persistence does not re-authorize research.** 一条历史 ``supported`` 行不等于当前
signal、不等于 current verified fact、不等于 promotion permission。任何要用研究结论的
下游都必须回到对应的 authority（R24 / R25 / R26）取正式事实。

──────────────── 本轮（B2A）刻意不做 ────────────────

只建立 canonical owner。**不**迁 legacy runtime、**不**删旧表、**不**回填历史、
**不** dual-write、**不**改 provider、**不**改 scheduler、**不**改 API / frontend。
``adaptive_advisor_runs`` / ``adaptive_ai_analysis_runs`` 仍是 legacy：**legacy rows
stay legacy**。把没有 R27 typed contract 保证的历史 free-form 行灌进 canonical ledger
会伪造 provenance 与语义；从 B2B 完成迁移的那一刻起，新 typed research 才进入本表。

──────────────── 能力边界 ────────────────

本模块**可以**访问 DB，但**不做**：联网、调 LLM、读 market / signal / execution、
做 risk decision、做 strategy promotion、生成 research status、修改 hypothesis。
它只依赖 stdlib 与 :mod:`ai_research_contract`，不 import 任何 authority 模块，也不读
墙上时钟 —— ``created_at`` 必须由调用方显式提供（``as_of`` 是业务时间，``created_at``
是 operational persistence time，两者必须分离，测试才可能完全确定）。

**事务语义**：:func:`append_run` 只做一次本地 DB write，**不在 transaction 内发网络
请求**（本模块天然没有网络依赖）。R27-B1 已经完成 provider 调用，未来 orchestration 的
顺序是：provider 完成 → 得到 typed result → 短事务 append。

**append-only**：本模块只出现 CREATE / INSERT / SELECT。没有 UPDATE、DELETE、
``INSERT OR REPLACE``、``ON CONFLICT DO UPDATE``。同一个 hypothesis 再保存一次
→ **第二条** audit run，这不是 bug：两次明确执行就是两条运行记录；业务幂等键留给未来
orchestration，本层不擅自引入。

**Secret 边界**：本表**绝不**出现 api_key / authorization / request_headers /
raw_prompt / system_prompt / user_prompt / raw_response。``provider_config`` 整体
**不**传进本模块，最多接受 ``provider_slot`` 与 ``provider_model`` 两个 audit label。

**持久化的是什么**：``hypothesis.projection()`` —— hypothesis **audit projection**，
而**不是** "完整重放模型输入"。R27-A 的 hypothesis 只保存 evidence reference + relation，
不保存原始 ``InformationEvent.payload``；本 PR 不偷偷改变这个契约，也不顺手塞
raw prompt / raw response。完整 provider-visible evidence input persistence 若未来需要，
另立明确 contract。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import ai_research_contract as ARC

__all__ = [
    "TABLE",
    "REASON_CORRUPT_RECORD",
    "MAX_ROWS",
    "MAX_PURPOSE_CHARS",
    "MAX_TRIGGER_CHARS",
    "MAX_NARRATIVE_CHARS",
    "MAX_COUNTER_ARGUMENTS",
    "MAX_COUNTER_ARGUMENT_CHARS",
    "MAX_PROVIDER_SLOT_CHARS",
    "MAX_PROVIDER_MODEL_CHARS",
    "MAX_CREATED_AT_CHARS",
    "MAX_FILTER_CHARS",
    "RUN_COLUMNS",
    "ResearchPersistenceError",
    "ensure_schema",
    "append_run",
    "recent_runs",
    "get_run",
]

#: canonical R27 research ledger。刻意**不**叫 ``deepseek_*`` / ``advisor_*`` /
#: ``adaptive_ai_*``：canonical research contract 已经与具体厂商、adaptive tuning
#: 解耦，表名跟着解耦，否则"这是谁的事实"会被名字重新绑定到某个厂商。
TABLE = "ai_research_runs"

#: 稳定 machine reason。读路径 fail closed 时只暴露它，绝不带 raw row / SQL 文本 /
#: 原始 JSON —— 损坏内容的诊断价值远低于把它重新带进日志与下游的代价。
REASON_CORRUPT_RECORD = "corrupt_research_record"

#: 读取上界：非法 ``limit`` 不允许退化成无限查询。
MAX_ROWS = 200

#: audit metadata 的硬上限。``purpose`` / ``trigger`` **只是审计标签**：它们不控制
#: status、不选择 writer、不选择 authority、不参与任何判定（禁止 ``if purpose == "signal"``）。
MAX_PURPOSE_CHARS = 80
MAX_TRIGGER_CHARS = 120
#: 与 R27-B1 provider 的长度上界兼容，避免"provider 收窄了、ledger 还收着旧的宽版本"。
MAX_NARRATIVE_CHARS = 8000
MAX_COUNTER_ARGUMENTS = 20
MAX_COUNTER_ARGUMENT_CHARS = 1000
#: ``provider_slot`` / ``provider_model`` 是 audit label，同样只做长度上界。
MAX_PROVIDER_SLOT_CHARS = 32
MAX_PROVIDER_MODEL_CHARS = 120
#: ``created_at`` 由调用方提供；只要求非空且有界。
MAX_CREATED_AT_CHARS = 64
#: 读取过滤值（``as_of`` / ``subject``）的上界。
MAX_FILTER_CHARS = 200

#: 列清单 —— 显式且只有一份真值，不依赖 ``SELECT *`` 的顺序。
RUN_COLUMNS = (
    "id",
    "purpose",
    "trigger",
    "hypothesis_id",
    "as_of",
    "subject",
    "status",
    "reason",
    "confidence",
    "authority",
    "is_authoritative",
    "provider_slot",
    "provider_model",
    "hypothesis",
    "narrative",
    "counter_arguments",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "record_hash",
    "created_at",
)

#: 写路径的列（``id`` 由 AUTOINCREMENT 生成，不参与 INSERT）。
_WRITE_COLUMNS = tuple(name for name in RUN_COLUMNS if name != "id")

#: SQL 里的列名统一双引号引用。``trigger`` 是 SQLite 关键字：裸用虽然当前版本接受，
#: 但引用之后语义与 SQLite 版本无关。统一引用也让列清单只需在 :data:`RUN_COLUMNS`
#: 维护一份，不必在每条 SQL 里再抄一遍。
_SELECT_LIST = ", ".join(f'"{name}"' for name in RUN_COLUMNS)
_WRITE_LIST = ", ".join(f'"{name}"' for name in _WRITE_COLUMNS)
_WRITE_PLACEHOLDERS = ", ".join("?" for _ in _WRITE_COLUMNS)

#: 只建**实际有意义的读取索引**，不为每个字段建索引。
_INDEXES = (
    ("as_of", '"as_of", "id" DESC'),
    ("subject", '"subject", "as_of", "id" DESC'),
    ("hypothesis", '"hypothesis_id", "id" DESC'),
)


class ResearchPersistenceError(ValueError):
    """已持久化的研究记录无法被安全读出 —— fail closed。

    ``reason`` 是唯一可被程序依赖的字段。刻意**不**把 SQL 语句、raw row 内容或原始
    损坏 JSON 拼进文案：诊断价值低于把这些内容重新扩散出去的代价。

    刻意**不**在损坏时返回 ``{}`` 或静默替换成 ``None``：一条读不出来的审计记录被当成
    "这里没有研究结论"，会把**数据损坏**伪装成**业务结论**。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason)
        text = self.reason if not detail else "%s: %s" % (self.reason, detail)
        super().__init__(text)


# ─────────────────────────────────────────────────────────────────────────────
# 输入校验 —— 只做 storage shape / resource bounds
# ─────────────────────────────────────────────────────────────────────────────
#
# 这里**只**约束"能不能安全落库"（类型、非空、长度、资源上界），刻意不重新实现 provider
# 的语义解析器，也不重新判定 research 语义。不因为"provider 已经检查过"就省略这些基础
# 防御：repository 是独立的写入口，不能把自己的安全性建立在调用方的正确性之上。


def _required_text(value: Any, *, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{what} requires a non-empty value")
    if len(text) > limit:
        raise ValueError(f"{what} exceeds {limit} chars")
    return text


def _optional_text(value: Any, *, what: str, limit: int) -> str:
    """可空文本：``None`` 归一成空串，但**不**接受非字符串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, got {type(value).__name__}")
    if len(value) > limit:
        raise ValueError(f"{what} exceeds {limit} chars")
    return value


def _provider_slot(value: Any) -> str | None:
    """``provider_slot`` 是**纯 audit label**，不具备业务语义。

    刻意**不**在这里校验 ``ai1`` / ``ai2``：那需要 import ``ai_review_service`` 的槽位
    定义，等于让持久化 owner 依赖整个 provider orchestration，并造出第二份槽位词表
    （两处必然漂移）。本层只做长度上界，使"有哪些槽位"不成为持久化层要维护的知识。

    真正的不变量是它**不影响** status / authority：整条写路径里 ``provider_slot``
    只被写进一列，从不参与任何判定。
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else None
    if text is None:
        raise TypeError(f"provider_slot must be a string or None, got {type(value).__name__}")
    if len(text) > MAX_PROVIDER_SLOT_CHARS:
        raise ValueError(f"provider_slot exceeds {MAX_PROVIDER_SLOT_CHARS} chars")
    return text or None


def _non_negative_int(value: Any, *, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{what} must be >= 0, got {value}")
    return value


def _counter_arguments(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(
            f"counter_arguments must be a sequence of strings, got {type(value).__name__}"
        )
    if len(value) > MAX_COUNTER_ARGUMENTS:
        raise ValueError(f"counter_arguments exceeds {MAX_COUNTER_ARGUMENTS} entries")
    return tuple(
        _required_text(item, what="counter_argument", limit=MAX_COUNTER_ARGUMENT_CHARS)
        for item in value
    )


def _canonical_json(payload: Any) -> str:
    """稳定 JSON serialization —— 不依赖 ``repr()`` / ``hash()`` / pickle。

    ``sort_keys`` 让 dict 插入顺序不影响结果，``separators`` 去掉随 Python 版本变化的
    空白，``allow_nan=False`` 拒绝 NaN / Infinity（它们不是合法 JSON，会让不同解析器
    各说各话）。
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _record_hash(payload: Any) -> str:
    """``record_hash`` = 实际持久化 research 内容的 SHA-256。

    刻意**不含** ``created_at``：hash 表示"研究产物内容"，而 ``created_at`` 是
    operational persistence time —— 把运维时间混进内容指纹，会让同一次研究产物在不同
    落库时刻得到不同 hash。刻意也**不含** DB ``id``（自增主键不是产物身份），更不含
    api_key / Authorization / raw HTTP body。
    """
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """建立 canonical research ledger（幂等）。**只新增**，不改既有表、不回填历史行。

    **刻意没有任何 UNIQUE 约束**（除了主键）：R27-A 从未声明"同一个 ``hypothesis_id``
    永远只能持久化一次"，所以本层不擅自引入业务幂等语义。append-only 意味着
    **两次明确执行 → 两条运行记录**；真正的幂等键属于未来 orchestration 的 business key，
    不是持久化层的猜测。

    ``CHECK`` 约束把本层的两条核心不变量写进 schema 本身，使它们不依赖调用方自觉：
    ``authority`` 只能是 ``research``，``is_authoritative`` 只能是 ``0``，``confidence``
    必须在 ``[0, 1]``，token / latency 不得为负。
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            purpose TEXT NOT NULL,
            "trigger" TEXT NOT NULL,

            hypothesis_id TEXT NOT NULL,
            as_of TEXT NOT NULL,
            subject TEXT NOT NULL,

            status TEXT NOT NULL,
            reason TEXT,

            confidence REAL NOT NULL,

            authority TEXT NOT NULL CHECK(authority = 'research'),
            is_authoritative INTEGER NOT NULL CHECK(is_authoritative = 0),

            provider_slot TEXT,
            provider_model TEXT NOT NULL DEFAULT '',

            hypothesis TEXT NOT NULL,

            narrative TEXT NOT NULL DEFAULT '',
            counter_arguments TEXT NOT NULL,

            input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
            output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
            latency_ms INTEGER NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),

            record_hash TEXT NOT NULL,

            created_at TEXT NOT NULL,

            CHECK(confidence >= 0.0 AND confidence <= 1.0)
        )
        """
    )
    for suffix, columns in _INDEXES:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_{suffix} ON {TABLE}({columns})"
        )
    return {"table": TABLE, "indexes": [f"idx_{TABLE}_{suffix}" for suffix, _ in _INDEXES]}


# ─────────────────────────────────────────────────────────────────────────────
# 写：append-only，唯一写入口
# ─────────────────────────────────────────────────────────────────────────────


def append_run(
    conn: sqlite3.Connection,
    *,
    hypothesis: ARC.ResearchHypothesis,
    purpose: Any,
    trigger: Any,
    created_at: Any,
    provider_slot: Any = None,
    provider_model: Any = "",
    narrative: Any = "",
    counter_arguments: Any = (),
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    latency_ms: Any = 0,
) -> int:
    """把一条 typed research hypothesis **追加**进 canonical ledger；返回新 ``id``。

    **只接受 typed hypothesis。** 不是 dict、不是裸字符串、不是带同名字段的假对象：
    若 dict 能冒充，调用方就能自述 ``status="supported"`` / ``authority="signal"`` ——
    那正是本层要根除的默认批准。

    因此本函数签名里**没有** ``status`` / ``reason`` / ``authority`` /
    ``is_authoritative`` / ``verification`` / ``verification_method`` 参数：它们全部由
    typed hypothesis 派生。"调用方不能自己声明裁决"在这里不是口头约定，而是**类型与
    签名上都表达不出来**。

    这**一次**调用只做一次本地 DB write：不在 transaction 内发网络请求。

    **同一 hypothesis 再 append 一次会得到第二条记录**，这是 append-only 的正确行为，
    不是需要被"修复"的重复。
    """
    if not isinstance(hypothesis, ARC.ResearchHypothesis):
        raise TypeError(
            "append_run requires a typed ai_research_contract.ResearchHypothesis; "
            f"got {type(hypothesis).__name__} — dict 或裸字符串不得冒充研究结论"
        )

    purpose_text = _required_text(purpose, what="purpose", limit=MAX_PURPOSE_CHARS)
    trigger_text = _required_text(trigger, what="trigger", limit=MAX_TRIGGER_CHARS)
    created_text = _required_text(created_at, what="created_at", limit=MAX_CREATED_AT_CHARS)
    slot = _provider_slot(provider_slot)
    model = _optional_text(
        provider_model, what="provider_model", limit=MAX_PROVIDER_MODEL_CHARS,
    )
    narrative_text = _optional_text(
        narrative, what="narrative", limit=MAX_NARRATIVE_CHARS,
    )
    counters = _counter_arguments(counter_arguments)
    in_tokens = _non_negative_int(input_tokens, what="input_tokens")
    out_tokens = _non_negative_int(output_tokens, what="output_tokens")
    latency = _non_negative_int(latency_ms, what="latency_ms")

    # canonical hypothesis JSON：typed contract 的稳定展示投影。
    # 直接用它，是因为**本层不重新解释** evidence 语义 —— relation / verification /
    # verification_method / cross_source_verified 全部照抄 R27-A（它再委托 R24）。
    projection = hypothesis.projection()
    hypothesis_json = _canonical_json(projection)

    content = {
        "purpose": purpose_text,
        "hypothesis": projection,
        "provider_slot": slot,
        "provider_model": model,
        "narrative": narrative_text,
        "counter_arguments": list(counters),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "latency_ms": latency,
    }

    row = {
        "purpose": purpose_text,
        "trigger": trigger_text,
        "hypothesis_id": hypothesis.hypothesis_id,
        "as_of": hypothesis.as_of,
        "subject": hypothesis.subject,
        # ── 以下五项**全部**从 typed hypothesis 派生，调用方无法提供 ──
        "status": hypothesis.status,
        "reason": hypothesis.reason,
        "confidence": float(hypothesis.confidence),
        "authority": hypothesis.authority,
        "is_authoritative": 1 if hypothesis.is_authoritative else 0,
        "provider_slot": slot,
        "provider_model": model,
        "hypothesis": hypothesis_json,
        "narrative": narrative_text,
        "counter_arguments": _canonical_json(list(counters)),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "latency_ms": latency,
        "record_hash": _record_hash(content),
        "created_at": created_text,
    }

    cursor = conn.execute(
        f"INSERT INTO {TABLE}({_WRITE_LIST}) VALUES({_WRITE_PLACEHOLDERS})",
        [row[name] for name in _WRITE_COLUMNS],
    )
    return int(cursor.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
# 读：audit projection
# ─────────────────────────────────────────────────────────────────────────────


def _json_object(raw: Any, *, what: str) -> dict:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise ResearchPersistenceError(
            REASON_CORRUPT_RECORD, f"{what} is not valid JSON",
        ) from None
    if not isinstance(parsed, dict):
        raise ResearchPersistenceError(
            REASON_CORRUPT_RECORD, f"{what} is not a JSON object",
        )
    return parsed


def _json_list(raw: Any, *, what: str) -> list:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        raise ResearchPersistenceError(
            REASON_CORRUPT_RECORD, f"{what} is not valid JSON",
        ) from None
    if not isinstance(parsed, list):
        raise ResearchPersistenceError(
            REASON_CORRUPT_RECORD, f"{what} is not a JSON array",
        )
    return parsed


def _row_values(row: Any) -> dict:
    """把一行按 :data:`RUN_COLUMNS` 对齐。

    ``sqlite3.Row`` 与默认连接的 ``tuple`` 都要支持 —— 只支持一种形态的读法会让另一种
    调用方看到"整张表都是空的"。
    """
    if isinstance(row, dict):
        return {name: row.get(name) for name in RUN_COLUMNS}
    if hasattr(row, "keys"):
        return {name: row[name] for name in RUN_COLUMNS}
    return dict(zip(RUN_COLUMNS, row, strict=False))


def _projection_from_row(row: Any) -> dict:
    """一行 → audit projection。损坏即 fail closed（:class:`ResearchPersistenceError`）。

    ``authority`` / ``is_authoritative`` 在返回前被重新校验一次，并在投影里**写成常量**：
    schema 的 ``CHECK`` 已经保证本模块写不出别的值，这里再校验是为了挡住"由别的 writer
    造出来的行"——一条自称权威的历史记录，绝不能被本层当成权威继续传播。投影里的
    ``is_authoritative`` 因此**恒为** ``False``，不会因为 ``status == supported`` 变成真。
    """
    values = _row_values(row)
    hypothesis = _json_object(values["hypothesis"], what="persisted hypothesis")
    counter_arguments = _json_list(
        values["counter_arguments"], what="persisted counter_arguments",
    )

    authority = values["authority"]
    stored_authoritative = values["is_authoritative"]
    if str(authority) != "research" or stored_authoritative != 0:
        raise ResearchPersistenceError(
            REASON_CORRUPT_RECORD,
            "persisted row claims an authority outside research",
        )

    return {
        "id": int(values["id"]),
        "purpose": values["purpose"],
        "trigger": values["trigger"],
        "hypothesis_id": values["hypothesis_id"],
        "as_of": values["as_of"],
        "subject": values["subject"],
        "status": values["status"],
        "reason": values["reason"],
        "confidence": float(values["confidence"]),
        # 常量而非列值：本层读出的任何记录都只是研究产物。
        "authority": "research",
        "is_authoritative": False,
        "provider_slot": values["provider_slot"],
        "provider_model": values["provider_model"],
        "hypothesis": hypothesis,
        "narrative": values["narrative"],
        "counter_arguments": counter_arguments,
        "input_tokens": int(values["input_tokens"]),
        "output_tokens": int(values["output_tokens"]),
        "latency_ms": int(values["latency_ms"]),
        "record_hash": values["record_hash"],
        "created_at": values["created_at"],
    }


def _row_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(f"limit must be an int, got {type(limit).__name__}")
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be within [1, {MAX_ROWS}], got {limit}")
    return limit


def _filters(as_of: Any, subject: Any) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if as_of is not None:
        clauses.append('"as_of" = ?')
        params.append(_required_text(as_of, what="as_of filter", limit=MAX_FILTER_CHARS))
    if subject is not None:
        clauses.append('"subject" = ?')
        params.append(_required_text(subject, what="subject filter", limit=MAX_FILTER_CHARS))
    return (" WHERE " + " AND ".join(clauses) if clauses else "", params)


def recent_runs(
    conn: sqlite3.Connection,
    *,
    limit: Any = 50,
    as_of: Any = None,
    subject: Any = None,
) -> list[dict]:
    """最近的研究运行，**按 ``id DESC``** 稳定返回（写入顺序，即 append 顺序）。

    刻意按自增主键而不是 ``created_at`` 排序：``created_at`` 由调用方提供，可能相同、
    可能乱序，用它排序会让"最近"依赖于调用方传了什么。``id`` 是**持久化顺序**本身。

    **返回的是 persisted research projection，不是重新核验过的现在事实。** 调用方不得
    把一行 ``status='supported'`` 读成"当前仍然支持"：它是历史研究产物，persistence
    不重新授权 research。

    损坏的行**不会**被跳过或替换成空记录 —— 直接 fail closed（见
    :class:`ResearchPersistenceError`）。
    """
    size = _row_limit(limit)
    where, params = _filters(as_of, subject)
    rows = conn.execute(
        f"SELECT {_SELECT_LIST} FROM {TABLE}{where} ORDER BY id DESC LIMIT ?",
        [*params, size],
    ).fetchall()
    return [_projection_from_row(row) for row in rows]


def get_run(conn: sqlite3.Connection, run_id: Any) -> dict | None:
    """按 ``id`` 取一条研究运行；不存在返回 ``None``（**不**抛错，这是"查无此行"）。

    与 :func:`recent_runs` 一样，返回的是历史 audit projection，且同样 fail closed。
    """
    if isinstance(run_id, bool) or not isinstance(run_id, int):
        raise TypeError(f"run_id must be an int, got {type(run_id).__name__}")
    row = conn.execute(
        f"SELECT {_SELECT_LIST} FROM {TABLE} WHERE id = ?", (run_id,),
    ).fetchone()
    if row is None:
        return None
    return _projection_from_row(row)
