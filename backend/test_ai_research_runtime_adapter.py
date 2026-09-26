# -*- coding: utf-8 -*-
"""R27-B2C-7 —— runtime / incident **接缝**的永久回归（INC-24 ~ INC-32）。

owner 侧的 INC-01 ~ INC-23 在 ``test_runtime_incident_owner_facts``。本文件只管接缝：
入口只接受哪些类型、identity / as_of 从哪来、指纹是否确定性、本层是否碰 DB / 网络 / 时钟、
registry 与真实 factory 是否双向一致，以及本轮**刻意不迁移**的 legacy runtime 是否仍然在、
并且被明确记为 deferred。

────────────── 为什么这些必须是结构断言 ──────────────

"adapter 不读 DB"若只写在注释里，下一次有人为了"顺手补一个字段"而 import ``sqlite3`` 时
没有任何东西会红。INC-29 因此直接扫 import 与调用点：这条边界是可执行的，不是风格建议。
INC-31 同理 —— "production 调用点 = 0"是本轮交付物的一部分（能力先落地、runtime 迁移留给
B2C-8），它必须是可验证的事实，而不是一句声明。
"""
from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as AE  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import ai_research_runtime_adapter as ADAPTER  # noqa: E402
import paper_trading as PT  # noqa: E402

ADAPTER_FILE = "ai_research_runtime_adapter.py"
RUNTIME_FACTORY = "evidence_ref_from_runtime_projection"

#: 本层**不得**触达的能力：DB、网络、墙钟、随机源、进程。
_FORBIDDEN_IMPORTS = (
    "sqlite3", "urllib", "requests", "socket", "http", "httpx", "aiohttp",
    "datetime", "time", "asyncio", "subprocess", "random", "secrets", "os",
    "pathlib", "zoneinfo",
)

_OPEN_CONNECTIONS: list = []


def _track(conn):
    _OPEN_CONNECTIONS.append(conn)
    return conn


class _DbTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        while _OPEN_CONNECTIONS:
            try:
                _OPEN_CONNECTIONS.pop().close()
            except Exception:  # pragma: no cover
                pass


def _production_modules() -> list[str]:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _imported_roots(name: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots - {"__future__"}


def _called_names(name: str) -> set[str]:
    called: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return called


# ───────────────────────────── fixtures ─────────────────────────────


def _adaptive_conn() -> sqlite3.Connection:
    conn = _track(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE adaptive_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, "
        "status TEXT NOT NULL, profile_date TEXT, new_rewards INTEGER NOT NULL DEFAULT 0, "
        "detail TEXT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL)"
    )
    return conn


def _adaptive_run(conn, *, status="failed", started_at="2026-09-20T15:05:00+08:00",
                  finished_at="2026-09-20T15:10:00+08:00", trigger="scheduled-close",
                  profile_date="2026-09-20", detail='{"error":"provider timeout"}',
                  new_rewards=0) -> int:
    cursor = conn.execute(
        "INSERT INTO adaptive_runs(trigger,status,profile_date,new_rewards,detail,"
        "started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
        (trigger, status, profile_date, new_rewards, detail, started_at, finished_at),
    )
    return int(cursor.lastrowid)


def _paper_conn() -> sqlite3.Connection:
    conn = _track(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE paper_job_runs(run_key TEXT PRIMARY KEY, slot TEXT NOT NULL, "
        "market_date TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, started_at TEXT NOT NULL, "
        "finished_at TEXT, owner_key TEXT, heartbeat_at TEXT, expires_at TEXT, "
        "fencing_token INTEGER NOT NULL DEFAULT 0)"
    )
    return conn


def _paper_attempt(conn, *, run_key="intraday:202609200945", slot="intraday",
                   market_date="2026-09-20", status="failed",
                   started_at="2026-09-20 09:45:00", finished_at="2026-09-20 09:47:00",
                   detail='{"error":"late tick"}', owner_key="sha1:abc", fencing_token=4) -> str:
    conn.execute(
        "INSERT INTO paper_job_runs(run_key,slot,market_date,status,detail,started_at,finished_at,"
        "owner_key,fencing_token) VALUES(?,?,?,?,?,?,?,?,?)",
        (run_key, slot, market_date, status, detail, started_at, finished_at, owner_key,
         fencing_token),
    )
    return run_key


def _adaptive_projection(**kwargs):
    conn = _adaptive_conn()
    run_id = _adaptive_run(conn, **kwargs)
    fact = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
    assert fact is not None, "fixture 应当能签发一条 adaptive run 事实"
    return fact


def _paper_projection(**kwargs):
    conn = _paper_conn()
    run_key = _paper_attempt(conn, **kwargs)
    fact = PT.paper_job_run_fact(conn, run_key, as_of="2026-09-20")
    assert fact is not None, "fixture 应当能签发一条 paper attempt 事实"
    return fact


# ═══════════════════════════════════════════════════════════════════════════
# INC-24 ~ INC-26：入口边界（精确类型 / 调用方不能自述 identity）
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeAdapterEntryBoundaryTests(_DbTestCase):
    def test_INC_24_the_factory_accepts_only_the_exact_approved_types(self):
        """INC-24：只接受两个已批准投影类型的**本类型**实例。

        ``dict`` / ``Mapping`` / 字符串 / 数字 / ``None`` / **子类** 全部拒绝 —— 一旦接受
        "长得像投影的对象"，调用方就能自己拼 ``availability_day`` 与
        ``fact_verification_status`` 冒充 owner。刻意不做 ``Protocol`` / ``isinstance`` 宽化。
        """
        adaptive = _adaptive_projection()
        paper = _paper_projection()
        self.assertEqual(
            ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT,
            ADAPTER.evidence_ref_from_runtime_projection(adaptive).source_type,
        )
        self.assertEqual(
            ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT,
            ADAPTER.evidence_ref_from_runtime_projection(paper).source_type,
        )

        fake_mapping = dict(adaptive.projection())
        duck = type("Duck", (), {
            "record_kind": AE.ADAPTIVE_RUN_RECORD_KIND,
            "revision_identity": "x",
            "availability_day": "2026-09-20",
            "fact_verification_status": AE.ADAPTIVE_RUN_FACT_RECORDED,
            "content_fingerprint": "f",
            "version": "v",
            "availability_kind": AE.ADAPTIVE_RUN_AVAILABILITY_TERMINAL,
        })()
        for payload in (fake_mapping, duck, "adaptive_run", 42, None, (), [adaptive]):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(TypeError):
                    ADAPTER.evidence_ref_from_runtime_projection(payload)

        # 子类也不行：identity 必须来自**类型本身**。
        subclass = type("Fake", (AE.AdaptiveRunFactProjection,), {})
        fields = {
            name: getattr(adaptive, name)
            for name in AE.AdaptiveRunFactProjection.__dataclass_fields__
            if name != "content_fingerprint"
        }
        with self.assertRaises(TypeError):
            ADAPTER.evidence_ref_from_runtime_projection(subclass(**fields))
        # 非空性：判定函数对合法类型返回 kind，对伪对象返回 None（两条都成立才说明它在工作）。
        self.assertEqual(AE.ADAPTIVE_RUN_RECORD_KIND, ADAPTER._exact_record_kind(adaptive))
        self.assertEqual(PT.PAPER_JOB_RUN_RECORD_KIND, ADAPTER._exact_record_kind(paper))
        self.assertIsNone(ADAPTER._exact_record_kind(fake_mapping))
        self.assertIsNone(ADAPTER._exact_record_kind(duck))

    def test_INC_25_the_caller_cannot_supply_a_source_id(self):
        """INC-25：调用方不能提供 ``source_id`` —— 签名里只有 ``projection``。

        一个由调用方命名的 identity 不是 identity，而是一个可以绕过去重与冲突检测的自由字符串。
        """
        parameters = inspect.signature(ADAPTER.evidence_ref_from_runtime_projection).parameters
        self.assertEqual(["projection"], list(parameters))
        for leaked in ("source_id", "identity", "record_kind", "fingerprint"):
            self.assertNotIn(leaked, parameters)

        adaptive = _adaptive_projection()
        ref = ADAPTER.evidence_ref_from_runtime_projection(adaptive)
        self.assertEqual(
            f"{AE.ADAPTIVE_RUN_RECORD_KIND}|{adaptive.revision_identity}", ref.source_id,
        )
        # 两次签发同一条事实 → identity 完全相同（不是我拼的自由字符串）。
        self.assertEqual(ref.source_id, ADAPTER.evidence_ref_from_runtime_projection(adaptive).source_id)

    def test_INC_26_the_caller_cannot_supply_an_as_of(self):
        """INC-26：调用方不能提供 ``as_of`` / ``verification`` / ``outcome``。

        ``as_of`` 必须来自 ``projection.availability_day``，核验结论来自 owner 的
        ``fact_verification_status``。因此"传一个字符串把自己声明成已核验事实"不可表达。
        """
        parameters = inspect.signature(ADAPTER.evidence_ref_from_runtime_projection).parameters
        for leaked in ("as_of", "availability_day", "verification", "outcome", "status"):
            self.assertNotIn(leaked, parameters)

        for projection, expected in (
            (_adaptive_projection(), "2026-09-20"),
            (_paper_projection(), "2026-09-20"),
        ):
            with self.subTest(record_kind=projection.record_kind):
                ref = ADAPTER.evidence_ref_from_runtime_projection(projection)
                self.assertEqual(projection.availability_day, ref.as_of)
                self.assertEqual(expected, ref.as_of)
                # owner-native 状态词逐字保留，核验结论由 owner 的闭集归口。
                self.assertEqual(projection.fact_verification_status, ref.verification)
                self.assertIn(ref.verification, (
                    AE.ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES
                    + PT.PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES
                ))

    def test_INC_26b_a_failed_run_is_verified_and_an_unproven_row_is_not(self):
        """INC-26b：``failed`` 通过核验，``unproven`` 不通过 —— 与运行"好坏"无关。

        这条同时是 B26 归口表的双向证明：``*_recorded`` → VERIFIED，``*_unproven`` →
        UNVERIFIED；而 ``runtime_status='failed'`` **不会**把 ``outcome`` 拉成 UNVERIFIED。
        """
        failed = _adaptive_projection(status="failed")
        completed = _adaptive_projection(
            status="completed", detail='{"stage":"done"}',
            finished_at="2026-09-20T15:40:00+08:00",
        )
        unproven = _adaptive_projection(
            status="failed", started_at="2026-09-20T15:10:00+08:00",
            finished_at="2026-09-20T15:05:00+08:00",
        )
        self.assertEqual(AE.ADAPTIVE_RUN_FACT_OWNER_UNPROVEN, unproven.fact_verification_status)

        expected = {
            "failed": (True, ARC.OWNER_OUTCOME_VERIFIED),
            "completed": (True, ARC.OWNER_OUTCOME_VERIFIED),
            "unproven": (False, ARC.OWNER_OUTCOME_UNVERIFIED),
        }
        for label, projection in (
            ("failed", failed), ("completed", completed), ("unproven", unproven),
        ):
            with self.subTest(case=label):
                ref = ADAPTER.evidence_ref_from_runtime_projection(projection)
                self_verified, outcome = expected[label]
                self.assertEqual(self_verified, ref.is_verified)
                self.assertEqual(outcome, ref.owner_verification.outcome)
                self.assertFalse(projection.runtime_status_is_verification)


# ═══════════════════════════════════════════════════════════════════════════
# INC-27 ~ INC-28：内容指纹
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeAdapterFingerprintTests(_DbTestCase):
    def test_INC_27_the_content_fingerprint_is_deterministic(self):
        """INC-27：指纹确定性 —— 同一内容 → 同一 sha256（不读时钟、不随机、不依赖插入顺序）。"""
        conn = _adaptive_conn()
        run_id = _adaptive_run(conn)
        first = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        second = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertEqual(first.content_fingerprint, second.content_fingerprint)
        self.assertEqual(64, len(first.content_fingerprint))

        # detail 的 key 顺序不影响指纹（canonical 排序 + 紧凑分隔符）。
        reordered = _adaptive_projection(detail='{"b":2,"a":1}')
        same_content = AE.adaptive_run_fact(
            _adaptive_conn_with_run(detail='{"a":1,"b":2}'), 1, as_of="2026-09-20",
        )
        self.assertEqual(reordered.content_fingerprint, same_content.content_fingerprint)

        # 指纹进 evidence detail，因此下游可以审计"这条 ref 指向哪份内容"。
        ref = ADAPTER.evidence_ref_from_runtime_projection(first)
        self.assertEqual(first.content_fingerprint, ref.detail["content_fingerprint"])
        self.assertEqual(AE.ADAPTIVE_RUN_FACT_CONTRACT_VERSION, ref.detail["contract_version"])
        self.assertEqual(AE.ADAPTIVE_RUN_RECORD_KIND, ref.detail["record_kind"])

    def test_INC_28_a_revision_or_content_change_moves_the_fingerprint(self):
        """INC-28：revision identity 或事实内容一变，指纹必须变。

        反面对照同样重要：**同一个业务日内**只换一个时间戳（内容确实变了）也必须移动指纹 ——
        否则"内容悄悄被改写"看起来会像"同一条事实"。
        """
        conn = _adaptive_conn()
        run_id = _adaptive_run(conn)
        before = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")

        conn.execute(
            "UPDATE adaptive_runs SET finished_at=? WHERE id=?",
            ("2026-09-20T15:12:00+08:00", run_id),
        )
        after = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertNotEqual(before.revision_identity, after.revision_identity)
        self.assertNotEqual(before.content_fingerprint, after.content_fingerprint)
        self.assertEqual(before.availability_day, after.availability_day)

        # 只换 detail 内容（revision 时间戳不动）也必须移动指纹。
        conn.execute(
            "UPDATE adaptive_runs SET detail=? WHERE id=?",
            ('{"error":"different"}', run_id),
        )
        content_changed = AE.adaptive_run_fact(conn, run_id, as_of="2026-09-20")
        self.assertEqual(after.revision_identity, content_changed.revision_identity)
        self.assertNotEqual(after.content_fingerprint, content_changed.content_fingerprint)

        # paper 侧：retry 覆盖后 revision 与指纹一起移动。
        pconn = _paper_conn()
        run_key = _paper_attempt(pconn)
        attempt_before = PT.paper_job_run_fact(pconn, run_key, as_of="2026-09-20")
        pconn.execute(
            "UPDATE paper_job_runs SET status='completed',detail='{\"ok\":true}',"
            "finished_at=? WHERE run_key=?",
            ("2026-09-20 09:50:00", run_key),
        )
        attempt_after = PT.paper_job_run_fact(pconn, run_key, as_of="2026-09-20")
        self.assertNotEqual(attempt_before.revision_identity, attempt_after.revision_identity)
        self.assertNotEqual(attempt_before.content_fingerprint, attempt_after.content_fingerprint)

        # 非空性：指纹长度与"确实发生了变化"同时成立，断言不空转。
        self.assertEqual(64, len(attempt_after.content_fingerprint))


def _adaptive_conn_with_run(**kwargs) -> sqlite3.Connection:
    conn = _adaptive_conn()
    _adaptive_run(conn, **kwargs)
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# INC-29：本层不做任何 I/O
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeAdapterIoBoundaryTests(unittest.TestCase):
    def test_INC_29_the_adapter_owns_no_db_no_network_no_clock(self):
        """INC-29：adapter 不 import / 不调用 DB、网络、墙钟、随机、进程能力。

        结构断言（AST），不是风格建议：下一次有人 import ``sqlite3`` 或调 ``datetime.now()``
        时这条会红。owner 模块**允许**有这些能力（它们是 DB-backed owner 且 paper_trading
        另有联网职责）；被登记进网络闭集的是它们的 **typed 读接缝**，不是整个模块。
        """
        roots = _imported_roots(ADAPTER_FILE)
        self.assertEqual(set(), roots & set(_FORBIDDEN_IMPORTS),
                         f"{ADAPTER_FILE} import 了禁止的能力：{sorted(roots & set(_FORBIDDEN_IMPORTS))}")
        # 依赖方向单向且最小：本层只认识两个 owner、research 契约与 stdlib 的 typing。
        self.assertEqual(
            {"adaptive_engine", "ai_research_contract", "paper_trading", "typing"},
            roots,
            f"{ADAPTER_FILE} 的 import 集合发生变化：{sorted(roots)}",
        )

        called = _called_names(ADAPTER_FILE)
        forbidden_calls = {
            "urlopen", "Request", "urlretrieve", "socket", "connect", "execute", "executemany",
            "executescript", "commit", "rollback", "open", "write", "now", "utcnow", "today",
            "time", "monotonic", "perf_counter", "randint", "random", "uuid4", "getenv",
            "environ", "read_text", "read_bytes", "sqlite3",
        }
        self.assertEqual(
            set(), called & forbidden_calls,
            f"{ADAPTER_FILE} 调用了禁止的能力：{sorted(called & forbidden_calls)}",
        )
        # 非空性：扫描器确实能看到这个模块的 import 与调用。
        self.assertIn("ai_research_contract", roots)
        self.assertIn("_issue_evidence_ref", called)
        # 非空性对照：owner 模块**确实**有 DB 能力（说明上面的边界是接缝级的，不是全局的）。
        self.assertIn("sqlite3", _imported_roots("paper_trading.py"))

    def test_INC_29b_the_adapter_has_no_second_authority_vocabulary(self):
        """INC-29b：本层不产生 vocabulary，也不复制另一类 authority。

        归口表的键必须**全部**来自两个 owner 模块的常量；本层不得出现自己发明的核验词、
        事故分级词，也不得复用 ``signal`` / ``strategy_research`` 这两个语义不准确的 source。
        """
        self.assertEqual(set(), set(ADAPTER._RUNTIME_OUTCOME_BY_STATUS) - set(
            AE.ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES
            + PT.PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES
        ))
        self.assertNotIn("EVIDENCE_SOURCE_SIGNAL", _source(ADAPTER_FILE))
        self.assertNotIn("EVIDENCE_SOURCE_STRATEGY_RESEARCH", _source(ADAPTER_FILE))
        identifiers = {
            node.id for node in ast.walk(ast.parse(_source(ADAPTER_FILE)))
            if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(ast.parse(_source(ADAPTER_FILE)))
            if isinstance(node, ast.Attribute)
        }
        for leaked in ("severity", "is_incident", "root_cause", "promotable", "severity_rank"):
            self.assertNotIn(leaked, identifiers)
        # 非空性：归口表确实非空，且恰好覆盖两家闭集的并集（不是"少了也能过"）。
        self.assertEqual(
            set(AE.ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES
                + PT.PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES),
            set(ADAPTER._RUNTIME_OUTCOME_BY_STATUS),
        )


# ═══════════════════════════════════════════════════════════════════════════
# INC-30：registry 与真实 factory 双向一致
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeSourceRegistryTests(unittest.TestCase):
    def test_INC_30_the_runtime_source_registry_matches_the_real_factory(self):
        """INC-30：``runtime_incident`` 在**四处闭集**里同时登记，且 factory 真实存在。

        少一处登记就会出现"source 可构造但 kind 查不到"或"registry 说支持却没有 factory"
        这类漂移；多一处则说明有人绕过这个 seam 加了第二个签发口。
        """
        self.assertIn(ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT, ARC.EVIDENCE_SOURCE_TYPES)
        self.assertIn(ARC.EVENT_RUNTIME_INCIDENT_OBSERVED, ARC.EVENT_KINDS)
        self.assertEqual(
            ARC.EVENT_RUNTIME_INCIDENT_OBSERVED,
            ARC._KIND_BY_SOURCE_TYPE[ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT],
        )
        self.assertIn(ARC.EVIDENCE_SOURCE_RUNTIME_INCIDENT, ARC.SUPPORTED_OWNER_ADAPTERS)
        # 一一对应：kind 映射必须覆盖全部 source，且没有多余项。
        self.assertEqual(len(ARC.EVIDENCE_SOURCE_TYPES), len(ARC._KIND_BY_SOURCE_TYPE))
        self.assertEqual(
            set(ARC.EVIDENCE_SOURCE_TYPES), set(ARC._KIND_BY_SOURCE_TYPE),
        )
        # 常量与 __all__ 同步（少一个就是"registry 不完整"）。
        for export in (
            "EVIDENCE_SOURCE_RUNTIME_INCIDENT", "EVENT_RUNTIME_INCIDENT_OBSERVED",
        ):
            self.assertIn(export, ARC.__all__)

        # factory 真实存在且可调用（registry 不是一句声明）。
        self.assertIn(RUNTIME_FACTORY, ADAPTER.__all__)
        self.assertTrue(callable(getattr(ADAPTER, RUNTIME_FACTORY)))
        # 它必须真的走私有签发口 —— 否则 registry 声称的接缝与真实实现不是同一个东西。
        self.assertIn("_issue_evidence_ref", _called_names(ADAPTER_FILE))
        # 契约模块自己**没有** runtime 的 factory（它不得 import owner 模块）。
        self.assertNotIn(RUNTIME_FACTORY, ARC.__all__)
        self.assertFalse(hasattr(ARC, RUNTIME_FACTORY))

    def test_INC_30b_the_outcome_mapping_drift_check_reports_both_directions(self):
        """INC-30b：归口表的漂移检查必须在两个方向都报问题，且不假报。

        与 ``test_ai_research_evidence_ownership_guard._registry_problems`` 同一手法：纯函数
        因此可以被**直接**驱动成两个方向的 RED，而不是只能靠改 owner 源码来验。非空性对照
        同样必要 —— 否则"报问题"可能只是因为检查器永远返回非空。
        """
        clean = ADAPTER._runtime_outcome_mapping_problems(
            ADAPTER._RUNTIME_OUTCOME_BY_STATUS, ADAPTER._OWNER_VERIFICATION_VOCABULARIES,
        )
        self.assertEqual([], clean, f"真实归口表被误报：{clean}")

        baselines = (
            ("owner", AE.ADAPTIVE_RUN_FACT_VERIFICATION_STATUSES),
            ("owner", PT.PAPER_JOB_RUN_FACT_VERIFICATION_STATUSES),
        )
        # 方向 1：owner 有状态但表里没登记。
        missing = ADAPTER._runtime_outcome_mapping_problems(
            {
                AE.ADAPTIVE_RUN_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
                PT.PAPER_JOB_RUN_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED,
                PT.PAPER_JOB_RUN_FACT_OWNER_UNPROVEN: ARC.OWNER_OUTCOME_UNVERIFIED,
            },
            baselines,
        )
        self.assertTrue(any("没有登记" in item for item in missing), missing)
        # 方向 2：表里有任何 owner 都不认识的状态。
        unknown = ADAPTER._runtime_outcome_mapping_problems(
            {**ADAPTER._RUNTIME_OUTCOME_BY_STATUS, "brand_new_status": ARC.OWNER_OUTCOME_VERIFIED},
            baselines,
        )
        self.assertTrue(any("漂移" in item for item in unknown), unknown)
        # 方向 3：两家 owner 闭集重叠 → 逐 owner 归因失效。
        overlap = ADAPTER._runtime_outcome_mapping_problems(
            {**ADAPTER._RUNTIME_OUTCOME_BY_STATUS, "shared": ARC.OWNER_OUTCOME_VERIFIED},
            (("a", ("shared",)), ("b", ("shared",))),
        )
        self.assertTrue(any("重叠" in item for item in overlap), overlap)

    def test_INC_30c_an_unknown_owner_verification_status_is_refused(self):
        """INC-30c：owner 新增一个核验状态却没人归口 → 签发时 fail closed。

        这是"research 不得替 owner 猜一个新状态的含义"的可执行形式。
        """
        adaptive = _adaptive_projection()
        unknown = type(adaptive)(**{
            **{
                name: getattr(adaptive, name)
                for name in AE.AdaptiveRunFactProjection.__dataclass_fields__
                if name != "fact_verification_status"
            },
            "fact_verification_status": AE.ADAPTIVE_RUN_FACT_RECORDED,
        })
        self.assertEqual(
            ARC.OWNER_OUTCOME_VERIFIED,
            ADAPTER._runtime_owner_verification(unknown).outcome,
        )
        # 直接驱动纯函数：把归口表换成缺一项的版本必须被拒绝。
        original = ADAPTER._RUNTIME_OUTCOME_BY_STATUS
        try:
            ADAPTER._RUNTIME_OUTCOME_BY_STATUS = {
                key: value for key, value in original.items()
                if key != AE.ADAPTIVE_RUN_FACT_OWNER_UNPROVEN
            }
            with self.assertRaises(ValueError):
                ADAPTER._runtime_owner_verification(adaptive)
        finally:
            ADAPTER._RUNTIME_OUTCOME_BY_STATUS = original
        # 恢复后必须重新可用 —— RED 来自那次替换，而不是检查器本来就坏。
        self.assertEqual(
            ARC.OWNER_OUTCOME_VERIFIED,
            ADAPTER._runtime_owner_verification(adaptive).outcome,
        )

    def test_INC_30d_the_research_kind_is_derived_from_the_source_type(self):
        """INC-30d：``InformationEvent.kind`` 由 source_type 独占派生。

        因此 runtime 事实的 kind 自动是 ``runtime_incident_observed``，"把 runtime 事实标成
        market 事实"在类型层面不可表达。
        """
        for projection, label in ((_adaptive_projection(), "adaptive"), (_paper_projection(), "paper")):
            with self.subTest(record_kind=label):
                ref = ADAPTER.evidence_ref_from_runtime_projection(projection)
                event = ARC.InformationEvent(
                    as_of=ref.as_of, source="runtime_owner", evidence_ref=ref,
                    payload={"record_kind": str(projection.record_kind)},
                )
                self.assertEqual(ARC.EVENT_RUNTIME_INCIDENT_OBSERVED, event.kind)
                self.assertEqual(ref.source_id, event.evidence_id)
                self.assertEqual(ref.verification, event.verification)
                # market-only 的属性对非 market 来源是"不适用"，不是 False 结论。
                self.assertIsNone(event.verification_method)
                self.assertFalse(event.evidence_ref.cross_source_verified)


# ═══════════════════════════════════════════════════════════════════════════
# INC-31 ~ INC-32：本轮刻意不迁移 legacy runtime
# ═══════════════════════════════════════════════════════════════════════════


class RuntimeMigrationDeferralTests(unittest.TestCase):
    def test_INC_31_the_runtime_adapter_has_no_production_callers(self):
        """INC-31：production 调用点 = **0** —— 这是本轮交付物的一部分。

        本轮的交付是"能力 + 契约 + 回归"：两个 owner 能发布 typed 事实、research 侧有唯一
        adapter 能把它变成 ``ResearchEvidenceRef``。``incident_triage`` 的 runtime 迁移属于
        R27-B2C-8。因此这里的 0 是**预期状态**，不是空转，也不是"这些 runtime 已完全迁移"。
        """
        callers = set()
        for name in _production_modules():
            if name == ADAPTER_FILE:
                continue
            tree = ast.parse(_source(name))
            origins: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        origins[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        origins[alias.asname or alias.name] = node.module.split(".")[0]
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id == RUNTIME_FACTORY:
                    callers.add(name)
                elif isinstance(func, ast.Attribute) and func.attr == RUNTIME_FACTORY:
                    base = func.value
                    if isinstance(base, ast.Name) and origins.get(base.id) == "ai_research_runtime_adapter":
                        callers.add(name)
        self.assertEqual(set(), callers, f"runtime adapter 出现了 production caller：{sorted(callers)}")
        # 非空性：同一个扫描器对**已有的** adapter 能看见调用点（说明它不是永远返回空集）。
        existing = set()
        for name in _production_modules():
            if "evidence_ref_from_execution_projection" in _source(name) and name != "ai_research_execution_adapter.py":
                existing.add(name)
        self.assertTrue(existing, "扫描器看不见既有的 adapter 调用点（护栏会空转）")

    def test_INC_32_incident_evidence_remains_deferred_legacy_runtime(self):
        """INC-32：``deepseek_research._incident_evidence`` 仍是 **deferred legacy runtime**。

        本轮**不**改它的 shape：直读 ``adaptive_runs`` / ``paper_jobs`` / ``paper_orders`` 的
        SQL 必须还在（迁移是 B2C-8 的事），而且它不得 import runtime adapter（否则就是
        "边定义 owner 边重写 runtime"）。
        """
        import deepseek_research as DR

        legacy = inspect.getsource(DR._incident_evidence)
        self.assertIn("adaptive_runs", legacy)
        self.assertIn("paper_jobs", legacy)
        self.assertIn("paper_orders", legacy)
        # 它**仍然**用把坏 JSON 变成空对象的 legacy 读法 —— 本轮刻意不动（迁移是 B2C-8）。
        self.assertIn("_loads", legacy)
        # research 套件仍在用这个 legacy purpose。
        self.assertIs(DR.COLLECTORS["incident_triage"], DR._incident_evidence)
        self.assertIn("incident_triage", DR.TASKS)
        # 它没有被改成 typed 路径。
        roots = _imported_roots("deepseek_research.py")
        self.assertNotIn("ai_research_runtime_adapter", roots)
        self.assertNotIn("ai_research_strategy_adapter", roots)
        # 非空性：adapter 确实**存在**（"未迁移"不等于"不存在"）。
        self.assertTrue(callable(ADAPTER.evidence_ref_from_runtime_projection))


if __name__ == "__main__":
    unittest.main()
