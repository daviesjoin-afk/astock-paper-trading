"""Read-only adaptive runtime facts, owned by the adaptive ledger boundary.

This module does not import the training engine, research or trading services.
The existing adaptive_engine public names remain compatibility exports.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

ADAPTIVE_RUN_FACT_CONTRACT_VERSION = "adaptive-runtime-fact-v1"

ADAPTIVE_RUN_RECORD_KIND = "adaptive_run"

#: owner 自己能签发的 runtime lifecycle status 闭集。**不是**核验词表。
ADAPTIVE_RUN_LIFECYCLE_STATUSES = (
    "running", "completed", "failed", "killed",
    "intraday_observation", "intraday_failed",
    "advisor_skipped", "advisor_batch", "advisor_failed",
)

#: 终态 status 中 owner **确实**盖了终态 instant（`finished_at`）的子集。
#: 这些行的一次性终态写入把 `finished_at` 写成**写入瞬间**，因此终态可用性可证。
ADAPTIVE_RUN_TERMINAL_STATUSES = (
    "completed", "failed",
    "intraday_observation", "intraday_failed",
    "advisor_skipped", "advisor_batch", "advisor_failed",
)

#: `killed` 是终态，但终态 instant **不可证**：`_learning_detect_stale:94` 只改
#: `status` 与 `detail`，甚至在自己的 detail 里写明 `no finished_at`，因此该行的
#: `finished_at` 仍等于 `started_at`（`running` INSERT 写入的占位值）。
#:
#: 用 `started_at` 代替终态 instant 是**超前声明**（"进程被杀"这件事要到悬挂检测跑过
#: 才知道），所以本层**不签发** killed 行 —— 见 `adaptive_run_fact` 的 fail-closed 语义。
ADAPTIVE_RUN_TERMINAL_AVAILABILITY_UNPROVABLE_STATUSES = ("killed",)

#: owner 签发的**极小** factual verification 闭集。两态，且与 lifecycle status 词表
#: **零交集**。
#:
#: * ``adaptive_run_recorded``：owner 能证明这条 runtime 记录是**它自己签发**的、status
#:   属于它的闭集、且终态/进行中的时间戳形状自洽。含义**仅**是"这是一条可靠的 owner
#:   runtime 事实"，**不是**"这次运行成功"、**不是**"这是系统事故"、**不是**任何严重级别。
#: * ``adaptive_run_unproven``：记录可读，但 owner **无法自证**它的时间戳自洽。它仍然是
#:   一条事实，但 owner 不为它的来源背书。
ADAPTIVE_RUN_FACT_RECORDED = "adaptive_run_recorded"
ADAPTIVE_RUN_FACT_OWNER_UNPROVEN = "adaptive_run_unproven"
ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES = (
    ADAPTIVE_RUN_FACT_RECORDED, ADAPTIVE_RUN_FACT_OWNER_UNPROVEN,
)

#: typed adaptive 事实只支持 owner 可证明的终态瞬间。
ADAPTIVE_RUN_AVAILABILITY_TERMINAL = "terminal"
ADAPTIVE_RUN_AVAILABILITY_KINDS = (ADAPTIVE_RUN_AVAILABILITY_TERMINAL,)

#: typed 读侧要求的**必需归一列**。
_ADAPTIVE_RUN_REQUIRED_COLUMNS = (
    "id", "trigger", "status", "profile_date", "new_rewards", "detail",
    "started_at", "finished_at",
)


class AdaptiveRuntimeFactError(ValueError):
    """typed adaptive runtime fact 读侧的 **fail closed** 拒绝（坏 JSON / 缺列 / naive 时间戳）。"""


def _runtime_owner_instant(value: Any, *, what: str) -> dt.datetime:
    """owner 时间戳必须是 **timezone-aware** 的可解析瞬间，否则 fail closed。

    `adaptive_common._now()` 在 tzdata 可用时写出带 offset 的 ISO（生产路径），
    本模块 import 期就硬依赖 `ZoneInfo("Asia/Shanghai")`，所以"naive 时间戳"在本表里
    **不是**一种合法的历史格式 —— 它意味着这一行不是本 owner 的正常写路径产物，
    因此禁止用运行机器的本地时区去猜（那会让研究结论依赖进程跑在哪台机器上）。
    """
    text = str(value or "").strip()
    if not text:
        raise AdaptiveRuntimeFactError(f"{what} is required for a typed adaptive runtime fact")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise AdaptiveRuntimeFactError(
            f"{what} is not a parsable owner instant: {text!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise AdaptiveRuntimeFactError(
            f"{what} must be timezone-aware; got naive {text!r} — "
            "无法证明可用瞬间，禁止用本地时区猜"
        )
    return parsed


def _runtime_owner_day(instant: dt.datetime, *, what: str) -> str:
    """owner 时区（Asia/Shanghai）归一后的业务日。"""
    return instant.astimezone(TZ).date().isoformat()


def _runtime_business_day(value: Any, *, what: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise AdaptiveRuntimeFactError(
            f"{what} requires an explicit canonical YYYY-MM-DD business day; got {value!r}"
        ) from exc
    if parsed.isoformat() != text:
        raise AdaptiveRuntimeFactError(f"{what} is not canonical: {value!r}")
    return text


def _runtime_strict_mapping(value: Any, *, what: str) -> dict:
    """严格 JSON parse + 顶层必须是 object。坏 JSON → fail closed（绝不 ``{}``）。

    把 malformed JSON 变成空对象，等于把"这条记录损坏了"伪装成"这条记录没有 detail"，
    而 incident research 正是靠 detail 判断发生了什么。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AdaptiveRuntimeFactError(f"{what} is empty — 缺内容不是空对象")
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise AdaptiveRuntimeFactError(f"{what} is not parsable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AdaptiveRuntimeFactError(
            f"{what} must parse to a JSON object; got {type(parsed).__name__}"
        )
    return parsed


def _runtime_canonical_json(value: Any, *, what: str) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AdaptiveRuntimeFactError(f"{what} is not canonically serializable: {exc}") from exc


def _runtime_canonical_detail(value: Any, *, what: str) -> str:
    return _runtime_canonical_json(_runtime_strict_mapping(value, what=what), what=what)


def _runtime_row_dict(cursor, row) -> dict:
    """把一行转成 dict，**对行形状自适应**（与 ``execution_verification._row_as_dict`` 同手法）。

    三种行对象都会出现且都合法：生产读路径设了 ``sqlite3.Row``（有 ``keys()``）、
    ``db_migrate`` / 运维入口是裸 tuple（只能借 ``cursor.description`` 配列名）、
    测试替身可能已经是 ``dict``。行形状是**调用方**的事实，不是本层可以假定的前提。
    """
    if hasattr(row, "keys"):
        return dict(row)
    columns = [item[0] for item in (getattr(cursor, "description", None) or ())]
    return dict(zip(columns, row, strict=True))


def _adaptive_run_availability_kind(runtime_status: str) -> str | None:
    """owner 能否为这个 status **证明**一个可用瞬间；不能则 ``None``（不可签发）。

    * 终态且 owner 盖了 `finished_at` → ``terminal``；
    * `running`（detail 可变且无 revision 时间戳）、`killed`（终态 instant 不可证）与
      **任何不在终态闭集里的 status** → ``None``。

    未知 status 一并走这里，因此"新增一个 status 却没人决定它的可用性语义"不会静默
    落进某个默认分支被当成已记录事实 —— 它直接不可签发。
    """
    if runtime_status in ADAPTIVE_RUN_TERMINAL_STATUSES:
        return ADAPTIVE_RUN_AVAILABILITY_TERMINAL
    return None


def _adaptive_run_fact_verification_status(
    *, runtime_status: str, availability_kind: str, started_at: dt.datetime, finished_at: dt.datetime,
) -> str:
    """owner 自洽性 → 极小 factual verification 闭集。

    判据只回答"这条 runtime 记录是不是 owner 能自证的合法事实"：

    * status 必须落在 owner 的 lifecycle 闭集内；
    * **终态**行的 `finished_at >= started_at`（终态写入发生在开始之后）；
    * 只有终态 availability 可签发；`running` 的 detail 会持续变化，但没有 revision
      timestamp，故它不属于 typed research evidence。

    刻意**不看** status 的"好坏"：`failed` 与 `completed` 都可以是
    ``adaptive_run_recorded`` —— 见本段开头的三条分离。
    """
    if runtime_status not in ADAPTIVE_RUN_LIFECYCLE_STATUSES:
        return ADAPTIVE_RUN_FACT_OWNER_UNPROVEN
    if availability_kind == ADAPTIVE_RUN_AVAILABILITY_TERMINAL:
        if finished_at < started_at:
            return ADAPTIVE_RUN_FACT_OWNER_UNPROVEN
        return ADAPTIVE_RUN_FACT_RECORDED
    return ADAPTIVE_RUN_FACT_OWNER_UNPROVEN


@dataclasses.dataclass(frozen=True)
class AdaptiveRunFactProjection:
    """**adaptive owner 自己签发**的一次 runtime 运行事实 —— 不可变快照。

    identity 由 ``<run_id>@<runtime_status>@<finished_at>`` 构成。终态行落地后不再被
    改写（审计见模块段落），因此这就是这条事实的 revision identity；每次学习循环/观测
    轮次都是**新的 INSERT**，所以不存在"同一个 id 换了内容"的覆盖问题。

    ``availability_kind`` / ``availability_day`` 是 owner 的显式声明：业务日只从
    **可用瞬间**按 owner 时区派生，**不**从 `started_at`（当它是终态时）、
    **不**从 `profile_date`（那只是"这次学习关于哪一天"的标签）。

    **本投影不回答事故问题。** 它可以说"run 17 的终态是 failed"，**不能**说
    "根因是 provider 中断"、"严重级别 high"、"需要回滚"、"数据已确认损坏" ——
    那些仍是 research 结论。

    已知限制：contract-issued ≠ physical database origin；
    physical provenance 仍是 OPEN / REQUIRED（与 B2C-3~B2C-6 一致）。
    """

    version: str
    record_kind: str
    run_id: int
    trigger: str
    runtime_status: str
    profile_date: str | None
    new_rewards: int
    detail_canonical: str
    started_at: str
    finished_at: str
    availability_kind: str
    availability_day: str
    fact_verification_status: str
    content_fingerprint: str = ""

    def __post_init__(self) -> None:
        record_kind = str(self.record_kind or "").strip()
        if record_kind != ADAPTIVE_RUN_RECORD_KIND:
            raise AdaptiveRuntimeFactError(
                f"unknown adaptive runtime record_kind: {record_kind!r}; "
                f"allowed: {ADAPTIVE_RUN_RECORD_KIND!r}"
            )
        object.__setattr__(self, "record_kind", record_kind)
        object.__setattr__(
            self, "version", str(self.version or "").strip() or ADAPTIVE_RUN_FACT_CONTRACT_VERSION,
        )
        if isinstance(self.run_id, bool) or not isinstance(self.run_id, int):
            raise AdaptiveRuntimeFactError(f"adaptive run_id must be an int, got {self.run_id!r}")
        if self.run_id <= 0:
            raise AdaptiveRuntimeFactError(f"adaptive run_id must be positive, got {self.run_id}")

        trigger = str(self.trigger or "").strip()
        if not trigger:
            raise AdaptiveRuntimeFactError("adaptive runtime fact requires a non-empty trigger")
        object.__setattr__(self, "trigger", trigger)

        runtime_status = str(self.runtime_status or "").strip()
        if runtime_status not in ADAPTIVE_RUN_LIFECYCLE_STATUSES:
            raise AdaptiveRuntimeFactError(
                f"unknown adaptive runtime lifecycle status: {runtime_status!r}; "
                f"allowed: {ADAPTIVE_RUN_LIFECYCLE_STATUSES}"
            )
        object.__setattr__(self, "runtime_status", runtime_status)

        profile_date = self.profile_date
        if profile_date is not None:
            profile_date = _runtime_business_day(profile_date, what="adaptive runtime profile_date")
        object.__setattr__(self, "profile_date", profile_date)

        if isinstance(self.new_rewards, bool) or not isinstance(self.new_rewards, int):
            raise AdaptiveRuntimeFactError(
                f"adaptive runtime new_rewards must be an int, got {self.new_rewards!r}"
            )

        detail_canonical = str(self.detail_canonical or "")
        canonical = _runtime_canonical_detail(detail_canonical, what="adaptive runtime detail")
        if canonical != detail_canonical:
            raise AdaptiveRuntimeFactError(
                "adaptive runtime detail is not canonical — 事实内容必须能确定性重算指纹"
            )

        started_at = _runtime_owner_instant(self.started_at, what="adaptive runtime started_at")
        finished_at = _runtime_owner_instant(self.finished_at, what="adaptive runtime finished_at")
        object.__setattr__(self, "started_at", started_at.isoformat(timespec="seconds"))
        object.__setattr__(self, "finished_at", finished_at.isoformat(timespec="seconds"))

        expected_kind = _adaptive_run_availability_kind(runtime_status)
        if expected_kind is None:
            raise AdaptiveRuntimeFactError(
                f"adaptive runtime status {runtime_status!r} has no owner-provable typed availability"
            )

        kind = str(self.availability_kind or "").strip()
        if kind not in ADAPTIVE_RUN_AVAILABILITY_KINDS:
            raise AdaptiveRuntimeFactError(
                f"unknown adaptive runtime availability_kind: {kind!r}; "
                f"allowed: {ADAPTIVE_RUN_AVAILABILITY_KINDS}"
            )
        if kind != expected_kind:
            raise AdaptiveRuntimeFactError(
                f"adaptive runtime availability_kind {kind!r} disagrees with the status "
                f"{runtime_status!r} — 可用瞬间的种类只能由 owner 从 status 派生"
            )
        object.__setattr__(self, "availability_kind", kind)

        day = _runtime_business_day(self.availability_day, what="adaptive runtime availability_day")
        instant = finished_at
        expected = _runtime_owner_day(instant, what="adaptive runtime availability instant")
        if day != expected:
            raise AdaptiveRuntimeFactError(
                f"adaptive runtime availability_day {day} disagrees with the owner-timezone day "
                f"derived from the {kind} instant ({expected}) — 业务日只能由 owner 派生"
            )

        status = str(self.fact_verification_status or "").strip()
        if status not in ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES:
            raise AdaptiveRuntimeFactError(
                f"unknown adaptive runtime fact verification status: {status!r}; "
                f"allowed: {ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES}"
            )
        expected_status = _adaptive_run_fact_verification_status(
            runtime_status=runtime_status, availability_kind=kind,
            started_at=started_at, finished_at=finished_at,
        )
        if status != expected_status:
            raise AdaptiveRuntimeFactError(
                "adaptive runtime fact_verification_status disagrees with owner-derived "
                "verification from factual fields"
            )
        object.__setattr__(self, "fact_verification_status", expected_status)
        object.__setattr__(self, "content_fingerprint", self._fingerprint())

    @property
    def revision_identity(self) -> str:
        return f"{self.run_id}@{self.runtime_status}@{self.finished_at}"

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.record_kind, self.revision_identity, self.availability_day)

    @property
    def runtime_status_is_verification(self) -> bool:
        """**恒为** ``False``：lifecycle status 与 fact verification 是两个维度。"""
        return False

    @property
    def detail(self) -> dict:
        return json.loads(self.detail_canonical)

    def _fingerprint(self) -> str:
        payload = {
            "version": self.version,
            "record_kind": self.record_kind,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "runtime_status": self.runtime_status,
            "profile_date": self.profile_date,
            "new_rewards": self.new_rewards,
            "detail": self.detail_canonical,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
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
            "run_id": self.run_id,
            "trigger": self.trigger,
            "runtime_status": self.runtime_status,
            "profile_date": self.profile_date,
            "new_rewards": self.new_rewards,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "availability_kind": self.availability_kind,
            "availability_day": self.availability_day,
            "fact_verification_status": self.fact_verification_status,
            "content_fingerprint": self.content_fingerprint,
            "authority": "owner_fact",
            "runtime_status_is_verification": False,
        }


def adaptive_run_fact(conn, run_id, *, as_of):
    """把一条 adaptive run 行读成 typed owner fact。``as_of`` **必须显式**。

    **fail-closed 的可用性语义**（返回 ``None`` = UNAVAILABLE，绝不用别的值代替）：

    * 行不存在；
    * ``status='killed'`` —— 终态 instant 不可证（见常量段说明）；
    * status **不在** owner 闭集内 —— 没有人为它的可用性语义做过决定；
    * 当前 revision 的可用日**晚于** ``as_of`` —— 旧 revision 已被覆盖后不复存在，
      **不**倒填历史，也**不**按 `profile_date` 回退。

    刻意**没有** ``as_of=None → latest`` / 取墙钟 / 取"最近一次 run"。

    行存在、status 可签发但时间戳自洽性不成立 → 返回投影且
    ``fact_verification_status == adaptive_run_unproven``（"owner fact unproven"），
    而不是抛错 —— 记录确实存在，只是 owner 不为它背书。

    内容损坏（坏 JSON / 缺归一列 / naive 时间戳）则抛 :class:`AdaptiveRuntimeFactError`。
    """
    day = _runtime_business_day(as_of, what="adaptive run fact as_of")
    cursor = conn.execute("SELECT * FROM adaptive_runs WHERE id=?", (int(run_id),))
    row = cursor.fetchone()
    if row is None:
        return None
    item = _runtime_row_dict(cursor, row)
    missing = [name for name in _ADAPTIVE_RUN_REQUIRED_COLUMNS if name not in item]
    if missing:
        raise AdaptiveRuntimeFactError(
            f"adaptive run {run_id} is missing required normalized columns: {missing}"
        )

    runtime_status = str(item.get("status") or "").strip()
    kind = _adaptive_run_availability_kind(runtime_status)
    if kind is None:
        return None

    started_at = _runtime_owner_instant(item.get("started_at"), what="adaptive run started_at")
    finished_at = _runtime_owner_instant(item.get("finished_at"), what="adaptive run finished_at")
    instant = finished_at
    available_day = _runtime_owner_day(instant, what="adaptive run availability instant")
    if available_day > day:
        return None

    new_rewards = item.get("new_rewards")
    if isinstance(new_rewards, bool) or not isinstance(new_rewards, int):
        raise AdaptiveRuntimeFactError(
            f"adaptive run {run_id} new_rewards must be an int, got {new_rewards!r}"
        )
    profile_date = item.get("profile_date")
    if profile_date is not None:
        profile_date = str(profile_date)

    return AdaptiveRunFactProjection(
        version=ADAPTIVE_RUN_FACT_CONTRACT_VERSION,
        record_kind=ADAPTIVE_RUN_RECORD_KIND,
        run_id=int(item["id"]),
        trigger=str(item.get("trigger") or ""),
        runtime_status=runtime_status,
        profile_date=profile_date,
        new_rewards=new_rewards,
        detail_canonical=_runtime_canonical_detail(
            item.get("detail"), what=f"adaptive run {run_id} detail",
        ),
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
        availability_kind=kind,
        availability_day=available_day,
        fact_verification_status=_adaptive_run_fact_verification_status(
            runtime_status=runtime_status, availability_kind=kind,
            started_at=started_at, finished_at=finished_at,
        ),
    )


def adaptive_run_facts(conn, *, as_of, limit=100, read_fact=None):
    """按显式 ``as_of`` 发布可用的 adaptive run typed facts。

    查询与投影都留在 runtime owner 内。research 只拿到投影，不读 ``adaptive_runs``
    的裸行；``killed`` 和时间戳无法证明的记录由 :func:`adaptive_run_fact` 保持
    unavailable。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("adaptive_run_facts limit must be a positive int")
    reader = adaptive_run_fact if read_fact is None else read_fact
    size = min(limit, 500)
    facts = []
    offset = 0
    while len(facts) < size:
        rows = conn.execute(
            "SELECT id FROM adaptive_runs ORDER BY id DESC LIMIT ? OFFSET ?",
            (min(size, 100), offset),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            run_id = row[0] if not hasattr(row, "keys") else row["id"]
            projection = reader(conn, run_id, as_of=as_of)
            if projection is not None:
                facts.append(projection)
                if len(facts) == size:
                    break
        offset += len(rows)
    facts.sort(key=lambda fact: (fact.availability_day, fact.revision_identity), reverse=True)
    return tuple(facts[:size])
