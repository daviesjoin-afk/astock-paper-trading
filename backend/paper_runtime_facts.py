# -*- coding: utf-8 -*-
"""Paper 任务事实契约与只读读取器。

调用方提供连接和明确的历史时点；本模块不打开数据库、不迁移、不写入，
也不依赖交易编排。任务写入仍由 paper_trading 负责。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# R27-B2C-7 —— paper runtime attempt（`paper_job_runs`）的 typed owner fact
# ---------------------------------------------------------------------------
#
# 契约之前，research 侧在 incident_triage 里读的是 **裸行**：
#
#     SELECT slot,market_date,status,detail,started_at,finished_at
#       FROM paper_jobs WHERE status NOT IN ('completed','success')
#
# 于是 `deepseek_research` 必须自己解释"这一行是 current slot state 还是某次 attempt"、
# "market_date 能不能当失败被知悉的时刻"。本段把这两个问题还给 owner。
#
# ────────────── 为什么是 `paper_job_runs`（attempt 级）而不是 `paper_jobs` ──────────────
#
# `paper_jobs` 的 PK 是 `(slot, market_date)` —— **每天每 slot 只有一行**，retry 会把
# `failed` 物理覆盖成 `running`（`:13613`），旧失败**不复存在**。它是 current slot state，
# **不是** attempt ledger。用当前行解释历史时点就是倒填。
#
# `paper_job_runs` 的 PK 是 `run_key = intraday:YYYYMMDDHHMM`（`_intraday_business_key:358`，
# 三分钟窗口），identity 属于**attempt 窗口**而不是"今天这个 slot"。本段因此只发布
# attempt 级事实。
#
# ────────────── writer 审计（闭集来自真实写路径） ──────────────
#
# `paper_job_runs` 的生产 writer 仍是 paper_trading；以下历史行号用于追溯：
#
#     INSERT running          :13587
#     UPDATE → failed         :13534（stale row 回收）/ :1521（_recover_stale_runtime_state）
#                             / :13751（异常路径）
#     UPDATE → running        :13593（retry；输入 `status IN ('failed','interrupted')`）
#     UPDATE → completed      :13732
#     UPDATE heartbeat        :13656（只动 heartbeat_at / expires_at / fencing_token）
#
# 写侧字面量 = `running` / `completed` / `failed`。
#
# ────────────── 三条分离 ──────────────
#
#     runtime lifecycle status（running / completed / failed）  ≠ owner fact verification
#     incident severity（critical / high / …）                  ≠ owner fact verification
#     业务拒绝 / 风控拒单                                        ≠ 系统事故（见 paper_orders）
#
# `completed` **不是** `verified`，`failed` 也**不是** `unverified`：一个 `failed` 事实
# 只要 owner 能证明记录，就是 owner-verified 的**事实**。`verified` 的意思是
# "这个事实可信"，不是"任务成功"。

PAPER_JOB_RUN_FACT_CONTRACT_VERSION = "paper-job-run-runtime-fact-v1"

PAPER_JOB_RUN_RECORD_KIND = "paper_job_run"

#: owner 自己能签发的 runtime lifecycle status 闭集。**不是**核验词表。
PAPER_JOB_RUN_LIFECYCLE_STATUSES = ("running", "completed", "failed")

#: retry CAS 接受的**输入**词汇（`:13548` / `:13597`），但**当前没有任何 production
#: writer 会写出它**（旧版本留下的行可能存在）。因为它没有对应的写路径，owner 无法定义
#: 它的时间戳形状，所以它**不在**可签发闭集里：读到这样一行时 `paper_job_run_fact`
#: 返回 ``None``（UNAVAILABLE），而不是猜一个可用瞬间。
PAPER_JOB_RUN_LEGACY_READONLY_STATUSES = ("interrupted",)

#: 终态 status：owner 的终态写入（`:13732` / `:13751` / `:13534` / `:1521`）把
#: `finished_at` 写成**写入瞬间**，因此终态可用性可证。
PAPER_JOB_RUN_TERMINAL_STATUSES = ("completed", "failed")

#: 非终态：行只声明"这一轮正在跑"，其**可用瞬间**是 `started_at`。
PAPER_JOB_RUN_IN_PROGRESS_STATUSES = ("running",)

#: owner 签发的**极小** factual verification 闭集。两态，与 lifecycle status 词表
#: **零交集**，与 adaptive runtime 的闭集也**零交集**。
#:
#: * ``paper_job_run_recorded``：owner 能证明这条 attempt 记录是**它自己签发**的、
#:   status 属于它的闭集、且终态/进行中的时间戳形状自洽。含义**仅**是"这是一条可靠的
#:   owner runtime 事实"，**不是**"这个 job 成功"、**不是**"这是系统事故"。
#: * ``paper_job_run_unproven``：记录可读，但 owner **无法自证**它的时间戳形状。
#:   它仍然是一条事实，但 owner 不为它背书。
PAPER_JOB_RUN_FACT_RECORDED = "paper_job_run_recorded"
PAPER_JOB_RUN_FACT_OWNER_UNPROVEN = "paper_job_run_unproven"
PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES = (
    PAPER_JOB_RUN_FACT_RECORDED, PAPER_JOB_RUN_FACT_OWNER_UNPROVEN,
)

#: 这条事实的可用瞬间是**哪一类**。进指纹，因此"把 `market_date` 或 `started_at` 当成
#: 终态可用性"不可能只靠读侧疏忽发生。
PAPER_JOB_RUN_AVAILABILITY_TERMINAL = "terminal"
PAPER_JOB_RUN_AVAILABILITY_IN_PROGRESS = "in_progress"
PAPER_JOB_RUN_AVAILABILITY_KINDS = (
    PAPER_JOB_RUN_AVAILABILITY_TERMINAL, PAPER_JOB_RUN_AVAILABILITY_IN_PROGRESS,
)

#: 调度器时间戳的**owner 声明的**历史格式：服务器本地时间的裸
#: ``%Y-%m-%d %H:%M:%S``（`_now:905`），而生产容器的时区由
#: ``TZ=Asia/Shanghai``（`Dockerfile` / `docker-compose.yml`）与
#: ``deploy/install-centos9.sh`` 的 ``timedatectl set-timezone Asia/Shanghai`` 固定，
#: `_market_session:914` 也逐字声明"生产环境为 Asia/Shanghai"。
#:
#: 因此这是一个**owner 明确拥有并一致使用**的 legacy 契约，而不是"naive 就当本地时间"
#: 的默认猜测。`_PAPER_JOB_INSTANT_FORMAT` 之外的一切（含带 offset 的 ISO、date-only、
#: 其它分隔符）都 fail closed —— 见 :func:`_paper_runtime_owner_instant`。
_PAPER_JOB_INSTANT_FORMAT = "%Y-%m-%d %H:%M:%S"
_PAPER_RUNTIME_OWNER_TZ_NAME = "Asia/Shanghai"

try:  # pragma: no cover - 只在 tzdata 缺失时
    _PAPER_RUNTIME_OWNER_TZ = ZoneInfo(_PAPER_RUNTIME_OWNER_TZ_NAME)
except Exception:
    _PAPER_RUNTIME_OWNER_TZ = None

#: typed 读侧要求的**必需归一列**。
_PAPER_JOB_RUN_REQUIRED_COLUMNS = (
    "run_key", "slot", "market_date", "status", "detail",
    "started_at", "finished_at", "owner_key", "fencing_token",
)


class PaperJobRuntimeFactError(ValueError):
    """typed paper job attempt fact 读侧的 **fail closed** 拒绝（坏 JSON / 缺列 / 非法时间戳）。"""


def _paper_runtime_owner_tz(*, what: str):
    if _PAPER_RUNTIME_OWNER_TZ is None:  # pragma: no cover - 只在 tzdata 缺失时
        raise PaperJobRuntimeFactError(
            f"{what}: owner timezone ({_PAPER_RUNTIME_OWNER_TZ_NAME}) unavailable — "
            "拒绝派生业务日（naive 时间戳的归属只能由 owner 的时区证明）"
        )
    return _PAPER_RUNTIME_OWNER_TZ


def _paper_runtime_owner_instant(value: Any, *, what: str) -> dt.datetime:
    """调度器时间戳 → Asia/Shanghai 的 aware 瞬间；其它形状一律 fail closed。

    **不接受**带 offset 的 ISO：那意味着这行不是本 owner 的正常写路径产物。也**不接受**
    date-only —— 它会被静默当成"当天 00:00 可用"，正是本层要杜绝的猜测。naive 时间戳
    之所以可接受，仅仅因为 `_PAPER_JOB_INSTANT_FORMAT` 是 owner 自己声明并一致使用的
    容器本地（Asia/Shanghai）格式。
    """
    text = str(value or "").strip()
    if not text:
        raise PaperJobRuntimeFactError(f"{what} is required for a typed paper job attempt fact")
    try:
        parsed = dt.datetime.strptime(text, _PAPER_JOB_INSTANT_FORMAT)
    except ValueError as exc:
        raise PaperJobRuntimeFactError(
            f"{what} must be the owner runner's naive "
            f"'{_PAPER_JOB_INSTANT_FORMAT}' timestamp; got {text!r} — "
            "其它形状（带 offset / date-only / 其它分隔符）无法证明可用瞬间"
        ) from exc
    return parsed.replace(tzinfo=_paper_runtime_owner_tz(what=what))


def _paper_runtime_owner_day(instant: dt.datetime, *, what: str) -> str:
    """owner 时区（Asia/Shanghai）归一后的业务日。"""
    return instant.astimezone(_paper_runtime_owner_tz(what=what)).date().isoformat()


def _paper_runtime_business_day(value: Any, *, what: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise PaperJobRuntimeFactError(
            f"{what} requires an explicit canonical YYYY-MM-DD business day; got {value!r}"
        ) from exc
    if parsed.isoformat() != text:
        raise PaperJobRuntimeFactError(f"{what} is not canonical: {value!r}")
    return text


def _paper_runtime_strict_mapping(value: Any, *, what: str) -> dict:
    """严格 JSON parse + 顶层必须是 object。坏 JSON → fail closed（绝不 ``{}``）。

    `paper_jobs` / `paper_job_runs` 的 `detail` 由 `_json:943` 写入，而它在遇到
    不可序列化对象时会 ``default=str`` —— 因此"能被读回"并不等于"内容完好"。
    把 malformed JSON 变成空对象，等于把"这条记录损坏了"伪装成"这条记录没有 detail"。

    SQL ``NULL`` **不**走这里：owner 的 `running` INSERT（`:13587`）刻意不写 `detail`
    列，因此 NULL 是合法形状，由 :func:`_paper_runtime_canonical_detail` 单独处理。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PaperJobRuntimeFactError(f"{what} is empty — 缺内容不是空对象")
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise PaperJobRuntimeFactError(f"{what} is not parsable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PaperJobRuntimeFactError(
            f"{what} must parse to a JSON object; got {type(parsed).__name__}"
        )
    return parsed


def _paper_runtime_canonical_json(value: Any, *, what: str) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PaperJobRuntimeFactError(f"{what} is not canonically serializable: {exc}") from exc


def _paper_runtime_canonical_detail(value: Any, *, what: str) -> str:
    """`detail` 列 → canonical JSON 文本。

    SQL ``NULL`` / 全空白是 owner 的**合法** in-progress 形状（`running` 的 INSERT 不写该
    列），归一成 JSON 字面量 ``null`` —— 刻意**不是** ``{}``：``null`` 表示"owner 还没有写
    detail"，``{}`` 表示"owner 写了空对象"，两者不是同一件事。其余坏 JSON 一律 fail closed。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "null"
    if isinstance(value, str) and value.strip() == "null":
        return "null"
    return _paper_runtime_canonical_json(_paper_runtime_strict_mapping(value, what=what), what=what)


def _paper_runtime_row_dict(cursor, row) -> dict:
    """把一行转成 dict，**对行形状自适应**（与 ``execution_verification._row_as_dict`` 同手法）。

    生产读路径是 ``sqlite3.Row``（有 ``keys()``），迁移 / 运维入口是裸 tuple
    （只能借 ``cursor.description`` 配列名）。行形状是**调用方**的事实。
    """
    if hasattr(row, "keys"):
        return dict(row)
    columns = [item[0] for item in (getattr(cursor, "description", None) or ())]
    return dict(zip(columns, row, strict=True))


def _paper_job_run_availability_kind(runtime_status: str) -> str | None:
    """owner 能否为这个 status **证明**一个可用瞬间；不能则 ``None``（不可签发）。

    * `completed` / `failed` → ``terminal``；
    * `running` → ``in_progress``（可用瞬间 = `started_at`）；
    * 其余（含 legacy 只读的 `interrupted` 与**任何**不在闭集里的 status）→ ``None``。

    未知 status 一并走这里，因此"新增一个 status 却没人决定它的可用性语义"不会静默
    落进某个默认分支被当成已记录事实 —— 它直接不可签发。
    """
    if runtime_status in PAPER_JOB_RUN_TERMINAL_STATUSES:
        return PAPER_JOB_RUN_AVAILABILITY_TERMINAL
    if runtime_status in PAPER_JOB_RUN_IN_PROGRESS_STATUSES:
        return PAPER_JOB_RUN_AVAILABILITY_IN_PROGRESS
    return None


def _paper_job_run_fact_verification_status(
    *, runtime_status: str, availability_kind: str,
    started_at: dt.datetime, finished_at: dt.datetime | None,
) -> str:
    """owner 自洽性 → 极小 factual verification 闭集。

    * status 必须落在 owner 的 lifecycle 闭集内；
    * **终态**行必须有 `finished_at` 且 `finished_at >= started_at`；
    * **进行中**行必须**没有** `finished_at` —— 这是 owner 的形状（INSERT 不写该列、
      retry 显式置 NULL）；一个 `running` 行若带着终态瞬间，owner 无法自证它。

    刻意**不看** status 的"好坏"：`failed` 与 `completed` 都可以是
    ``paper_job_run_recorded`` —— 见本段开头的三条分离。
    """
    if runtime_status not in PAPER_JOB_RUN_LIFECYCLE_STATUSES:
        return PAPER_JOB_RUN_FACT_OWNER_UNPROVEN
    if availability_kind == PAPER_JOB_RUN_AVAILABILITY_TERMINAL:
        if finished_at is None or finished_at < started_at:
            return PAPER_JOB_RUN_FACT_OWNER_UNPROVEN
        return PAPER_JOB_RUN_FACT_RECORDED
    if availability_kind == PAPER_JOB_RUN_AVAILABILITY_IN_PROGRESS:
        if finished_at is not None:
            return PAPER_JOB_RUN_FACT_OWNER_UNPROVEN
        return PAPER_JOB_RUN_FACT_RECORDED
    return PAPER_JOB_RUN_FACT_OWNER_UNPROVEN


@dataclasses.dataclass(frozen=True)
class PaperJobRunFactProjection:
    """**paper runtime scheduler 自己签发**的一次 attempt 事实 —— 不可变快照。

    identity 由 ``<run_key>@<started_at>|<finished_at>`` 构成。`run_key` 只是**窗口**
    身份（同一窗口的 retry 会改写同一行），因此 revision identity 必须带上两个时间戳：
    每一次**改变事实内容**的改写都会推进 `started_at`（retry）或 `finished_at`（终态写入）
    中的至少一个，而租约心跳两个都不动。于是"内容一变 identity 就变"，同时心跳不会把
    同一条事实变成多条。

    ``availability_kind`` / ``availability_day`` 是 owner 的显式声明：业务日只从
    **可用瞬间**按 owner 时区（Asia/Shanghai）派生，**不**从 `market_date`
    （那是"这次任务属于哪个交易日"的业务标签，D 日失败可能要 D 15:30 甚至 D+1 的回收
    扫描才被系统知道），也**不**从 `started_at`（当它是终态时）。

    ``heartbeat_at`` / ``expires_at`` **不在**本投影里：它们是租约上下文，**不是**事实。
    "租约过期"本身**不**等于失败 —— 本层不做这个转换。

    本投影**不复制执行 authority**：订单是否成交、是否被风控拒绝、not_executed / partial
    全部继续归 ``execution_owner``（`ExecutionFactProjection`）。本层只说
    "某次 runtime attempt 的当前记录是什么"。

    **本投影不回答事故问题。** 它可以说"job close/D 的这一次尝试终态是 failed"，
    **不能**说"根因是数据源故障"、"严重级别 high"、"需要重跑"、"数据已损坏"。

    已知限制：contract-issued ≠ physical database origin；
    physical provenance 仍是 OPEN / REQUIRED（与 B2C-3~B2C-6 一致）。
    """

    version: str
    record_kind: str
    run_key: str
    slot: str
    market_date: str
    runtime_status: str
    detail_canonical: str
    started_at: str
    finished_at: str | None
    owner_key: str | None
    fencing_token: int
    availability_kind: str
    availability_day: str
    fact_verification_status: str
    content_fingerprint: str = ""

    def __post_init__(self) -> None:
        record_kind = str(self.record_kind or "").strip()
        if record_kind != PAPER_JOB_RUN_RECORD_KIND:
            raise PaperJobRuntimeFactError(
                f"unknown paper job attempt record_kind: {record_kind!r}; "
                f"allowed: {PAPER_JOB_RUN_RECORD_KIND!r}"
            )
        object.__setattr__(self, "record_kind", record_kind)
        object.__setattr__(
            self, "version", str(self.version or "").strip() or PAPER_JOB_RUN_FACT_CONTRACT_VERSION,
        )
        for name in ("run_key", "slot"):
            text = str(getattr(self, name) or "").strip()
            if not text:
                raise PaperJobRuntimeFactError(f"paper job attempt fact requires {name}")
            object.__setattr__(self, name, text)
        object.__setattr__(
            self, "market_date",
            _paper_runtime_business_day(self.market_date, what="paper job attempt market_date"),
        )

        runtime_status = str(self.runtime_status or "").strip()
        if runtime_status not in PAPER_JOB_RUN_LIFECYCLE_STATUSES:
            raise PaperJobRuntimeFactError(
                f"unknown paper job runtime lifecycle status: {runtime_status!r}; "
                f"allowed: {PAPER_JOB_RUN_LIFECYCLE_STATUSES}"
            )
        object.__setattr__(self, "runtime_status", runtime_status)

        detail_canonical = str(self.detail_canonical or "")
        canonical = _paper_runtime_canonical_detail(detail_canonical, what="paper job attempt detail")
        if canonical != detail_canonical:
            raise PaperJobRuntimeFactError(
                "paper job attempt detail is not canonical — 事实内容必须能确定性重算指纹"
            )

        started_at = _paper_runtime_owner_instant(
            self.started_at, what="paper job attempt started_at",
        )
        object.__setattr__(self, "started_at", started_at.strftime(_PAPER_JOB_INSTANT_FORMAT))
        finished_at = None
        if self.finished_at is not None:
            finished_at = _paper_runtime_owner_instant(
                self.finished_at, what="paper job attempt finished_at",
            )
            object.__setattr__(
                self, "finished_at", finished_at.strftime(_PAPER_JOB_INSTANT_FORMAT),
            )
        else:
            object.__setattr__(self, "finished_at", None)

        owner_key = self.owner_key
        if owner_key is not None:
            owner_key = str(owner_key).strip() or None
        object.__setattr__(self, "owner_key", owner_key)
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int):
            raise PaperJobRuntimeFactError(
                f"paper job attempt fencing_token must be an int, got {self.fencing_token!r}"
            )
        if self.fencing_token < 0:
            raise PaperJobRuntimeFactError(
                f"paper job attempt fencing_token must not be negative, got {self.fencing_token}"
            )

        kind = str(self.availability_kind or "").strip()
        if kind not in PAPER_JOB_RUN_AVAILABILITY_KINDS:
            raise PaperJobRuntimeFactError(
                f"unknown paper job attempt availability_kind: {kind!r}; "
                f"allowed: {PAPER_JOB_RUN_AVAILABILITY_KINDS}"
            )
        if kind != _paper_job_run_availability_kind(runtime_status):
            raise PaperJobRuntimeFactError(
                f"paper job attempt availability_kind {kind!r} disagrees with the status "
                f"{runtime_status!r} — 可用瞬间的种类只能由 owner 从 status 派生"
            )
        if kind == PAPER_JOB_RUN_AVAILABILITY_TERMINAL and finished_at is None:
            raise PaperJobRuntimeFactError(
                f"paper job attempt claims terminal status {runtime_status!r} but carries no "
                "finished_at — 终态记录没有终态瞬间，owner 无法证明它何时可用"
            )
        object.__setattr__(self, "availability_kind", kind)

        day = _paper_runtime_business_day(
            self.availability_day, what="paper job attempt availability_day",
        )
        instant = finished_at if kind == PAPER_JOB_RUN_AVAILABILITY_TERMINAL else started_at
        expected = _paper_runtime_owner_day(instant, what="paper job attempt availability instant")
        if day != expected:
            raise PaperJobRuntimeFactError(
                f"paper job attempt availability_day {day} disagrees with the owner-timezone "
                f"day derived from the {kind} instant ({expected}) — 业务日只能由 owner 派生"
            )

        status = str(self.fact_verification_status or "").strip()
        if status not in PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES:
            raise PaperJobRuntimeFactError(
                f"unknown paper job attempt fact verification status: {status!r}; "
                f"allowed: {PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES}"
            )
        expected_status = _paper_job_run_fact_verification_status(
            runtime_status=runtime_status, availability_kind=kind,
            started_at=started_at, finished_at=finished_at,
        )
        if status != expected_status:
            raise PaperJobRuntimeFactError(
                "paper job attempt fact_verification_status disagrees with owner-derived "
                "verification from factual fields"
            )
        object.__setattr__(self, "fact_verification_status", expected_status)
        object.__setattr__(self, "content_fingerprint", self._fingerprint())

    @property
    def revision_identity(self) -> str:
        return f"{self.run_key}@{self.started_at}|{self.finished_at or 'unfinished'}"

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.record_kind, self.revision_identity, self.availability_day)

    @property
    def runtime_status_is_verification(self) -> bool:
        """**恒为** ``False``：lifecycle status 与 fact verification 是两个维度。"""
        return False

    @property
    def detail(self) -> dict | None:
        """owner 写的 detail；``None`` 表示 owner 尚未写 detail（不是"空对象"）。"""
        return json.loads(self.detail_canonical)

    def _fingerprint(self) -> str:
        payload = {
            "version": self.version,
            "record_kind": self.record_kind,
            "run_key": self.run_key,
            "slot": self.slot,
            "market_date": self.market_date,
            "runtime_status": self.runtime_status,
            "detail": self.detail_canonical,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "owner_key": self.owner_key,
            "fencing_token": self.fencing_token,
            "availability_kind": self.availability_kind,
            "availability_day": self.availability_day,
            "fact_verification_status": self.fact_verification_status,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def projection(self) -> dict:
        return {
            "version": self.version,
            "record_kind": self.record_kind,
            "identity": self.revision_identity,
            "run_key": self.run_key,
            "slot": self.slot,
            "market_date": self.market_date,
            "runtime_status": self.runtime_status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "owner_key": self.owner_key,
            "fencing_token": self.fencing_token,
            "availability_kind": self.availability_kind,
            "availability_day": self.availability_day,
            "fact_verification_status": self.fact_verification_status,
            "content_fingerprint": self.content_fingerprint,
            "authority": "owner_fact",
            "runtime_status_is_verification": False,
        }


def paper_job_run_fact(conn, run_key, *, as_of):
    """把一条 attempt 行读成 typed owner fact。``as_of`` **必须显式**。

    **fail-closed 的可用性语义**（返回 ``None`` = UNAVAILABLE，绝不用别的值代替）：

    * 行不存在；
    * status 不在 owner 闭集内（含 legacy 只读的 `interrupted`）—— 没有人为它的可用性
      语义做过决定；
    * 当前 revision 的可用日**晚于** ``as_of`` —— 旧 revision 已被覆盖后不复存在，
      **不**倒填历史，也**不**按 `market_date` / `started_at` 回退。

    刻意**没有** ``as_of=None → latest`` / 取墙钟 / 取"今天这个 slot 的当前行"。

    行存在、status 可签发但时间戳形状自洽性不成立 → 返回投影且
    ``fact_verification_status == paper_job_run_unproven``（"owner fact unproven"），
    而不是抛错 —— 记录确实存在，只是 owner 不为它背书。终态行**没有** ``finished_at``
    属于记录损坏，抛 :class:`PaperJobRuntimeFactError`。

    **retry 覆盖语义（必须显式知道）**：同一 `run_key` 的 `failed` 会被 retry 改写成
    `running`（`finished_at=NULL`）再成 `completed`，旧失败**物理消失**。因此本函数
    **不可能**从当前行重建那次失败；对早已被覆盖的 as_of 它只能返回 ``None``。
    非 intraday slot 的同类缺口见 `OPEN PREREQUISITE: append-only attempt identity for
    non-intraday paper job failures`。
    """
    day = _paper_runtime_business_day(as_of, what="paper job attempt fact as_of")
    cursor = conn.execute("SELECT * FROM paper_job_runs WHERE run_key=?", (str(run_key),))
    row = cursor.fetchone()
    if row is None:
        return None
    item = _paper_runtime_row_dict(cursor, row)
    missing = [name for name in _PAPER_JOB_RUN_REQUIRED_COLUMNS if name not in item]
    if missing:
        raise PaperJobRuntimeFactError(
            f"paper job run {run_key} is missing required normalized columns: {missing}"
        )

    runtime_status = str(item.get("status") or "").strip()
    kind = _paper_job_run_availability_kind(runtime_status)
    if kind is None:
        return None

    started_at = _paper_runtime_owner_instant(
        item.get("started_at"), what=f"paper job run {run_key} started_at",
    )
    finished_raw = item.get("finished_at")
    finished_at = None
    if finished_raw is not None and str(finished_raw).strip():
        finished_at = _paper_runtime_owner_instant(
            finished_raw, what=f"paper job run {run_key} finished_at",
        )
    if kind == PAPER_JOB_RUN_AVAILABILITY_TERMINAL and finished_at is None:
        raise PaperJobRuntimeFactError(
            f"paper job run {run_key} claims terminal status {runtime_status!r} but carries no "
            "finished_at — 终态记录没有终态瞬间，owner 无法证明它何时可用"
        )

    instant = finished_at if kind == PAPER_JOB_RUN_AVAILABILITY_TERMINAL else started_at
    available_day = _paper_runtime_owner_day(
        instant, what=f"paper job run {run_key} availability instant",
    )
    if available_day > day:
        return None

    fencing_token = item.get("fencing_token")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise PaperJobRuntimeFactError(
            f"paper job run {run_key} fencing_token must be an int, got {fencing_token!r}"
        )
    owner_key = item.get("owner_key")

    return PaperJobRunFactProjection(
        version=PAPER_JOB_RUN_FACT_CONTRACT_VERSION,
        record_kind=PAPER_JOB_RUN_RECORD_KIND,
        run_key=str(item["run_key"]),
        slot=str(item.get("slot") or ""),
        market_date=str(item.get("market_date") or ""),
        runtime_status=runtime_status,
        detail_canonical=_paper_runtime_canonical_detail(
            item.get("detail"), what=f"paper job run {run_key} detail",
        ),
        started_at=started_at.strftime(_PAPER_JOB_INSTANT_FORMAT),
        finished_at=None if finished_at is None else finished_at.strftime(_PAPER_JOB_INSTANT_FORMAT),
        owner_key=None if owner_key is None else str(owner_key),
        fencing_token=fencing_token,
        availability_kind=kind,
        availability_day=available_day,
        fact_verification_status=_paper_job_run_fact_verification_status(
            runtime_status=runtime_status, availability_kind=kind,
            started_at=started_at, finished_at=finished_at,
        ),
    )


def paper_job_run_facts(conn, *, as_of, limit=100):
    """按显式 ``as_of`` 发布可证明的 job attempt typed facts。

    run key 的枚举与 row → projection 的转换都属于 paper runtime owner。非 intraday
    attempt、被 retry 覆盖的失败 revision 与其他不可证明记录继续由单条读取器返回为
    unavailable；本 API 不创建历史 ledger。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("paper_job_run_facts limit must be a positive int")
    size = min(limit, 500)
    facts = []
    offset = 0
    while len(facts) < size:
        rows = conn.execute(
            "SELECT run_key FROM paper_job_runs ORDER BY started_at DESC,run_key DESC LIMIT ? OFFSET ?",
            (min(size, 100), offset),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            run_key = row[0] if not hasattr(row, "keys") else row["run_key"]
            projection = paper_job_run_fact(conn, run_key, as_of=as_of)
            if projection is not None:
                facts.append(projection)
                if len(facts) == size:
                    break
        offset += len(rows)
    facts.sort(key=lambda fact: (fact.availability_day, fact.revision_identity), reverse=True)
    return tuple(facts[:size])


