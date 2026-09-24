# -*- coding: utf-8 -*-
"""R27-B2A —— canonical research persistence owner 的语义与架构回归。

存在理由是这条不变量：

    **研究结论有正式持久化 owner；但数据库不是新的 authority。**

分六组：

    RPERSIST-01 ~ 02   schema 幂等、只接受 typed hypothesis
    RPERSIST-03 ~ 06   status / reason / authority 全部派生；调用方传不进来
    RPERSIST-07 ~ 09   evidence 维度逐字保存（single_source / coverage_integrity / relation）
    RPERSIST-10 ~ 13   append-only 真成立、record_hash 覆盖真正的研究内容
    RPERSIST-14        secret / raw prompt 绝不落库
    RPERSIST-15 ~ 19   损坏行 fail closed；读取有界、顺序稳定、过滤正确
    RPERSIST-20 ~ 25   边界 guard：无时钟、无网络、无 authority import、唯一 writer
    非空性             护栏必须真的能失败，否则它只是装饰

时间一律**显式**传入（固定业务日与固定 created_at 字符串），绝不读墙上时钟 ——
本轮最容易假绿的地方正是"created_at 到底由谁决定"。

刻意**不**起真实数据库文件：全部用例跑在 ``sqlite3.connect(":memory:")`` 上，
因此没有任何临时 DB 产物会留进仓库。
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_research_contract as ARC  # noqa: E402
import ai_research_repository as REP  # noqa: E402
import market_data_contract as MDC  # noqa: E402

MODULE = "ai_research_repository.py"

#: 固定业务日 / 固定运维时刻 —— 与本机时钟无关，测试因此完全确定。
DAY = "2026-08-27"
OBSERVED_AT = f"{DAY}T10:30:00+08:00"
CREATED_AT = f"{DAY}T10:31:00+08:00"
OTHER_CREATED_AT = f"{DAY}T23:59:59+08:00"
CODE = "600000"
POLICY = "live_market"

#: R24 既有的逐票核验术语。
VALIDATION_CROSS_SOURCE = "cross_source_checked"
VALIDATION_SINGLE_SOURCE = "range_timestamp_checked"
VALIDATION_DISAGREEMENT = "cross_source_failed"

#: 调用方**不得**能提供的裁决 / 核验字段。它们必须全部由 typed hypothesis 派生。
FORBIDDEN_CALLER_FIELDS = frozenset({
    "status", "reason", "authority", "is_authoritative",
    "verification", "verification_method", "cross_source_verified",
    "source_type", "source_id", "as_of", "hypothesis_id",
})

#: secret / raw prompt 相关的列名与参数名：本表与签名里都不允许出现。
#: 刻意用**精确**名字而不是 "auth" 这类前缀 —— 那会把 ``authority`` 也误判成 secret。
SECRET_FIELD_TOKENS = (
    "api_key", "apikey", "authorization", "bearer", "secret", "password",
    "credential", "header", "prompt", "raw_response", "request_body", "access_token",
)

#: repository 允许 import 的东西（等值断言，不是"不含"断言）。
ALLOWED_IMPORTS = {"__future__", "hashlib", "json", "sqlite3", "typing",
                   "ai_research_contract"}

#: repository 明确**不得** import 的 authority / orchestration 模块。
FORBIDDEN_IMPORTS = {
    "market_data_service", "data_fetcher", "signal_service",
    "execution_planner", "execution_evidence", "execution_dispatch",
    "execution_lifecycle", "execution_verification", "execution_outcome",
    "manual_orders", "paper_trading", "paper_repository", "paper_storage",
    "paper_risk_service", "paper_risk_decision", "risk_center",
    "promotion_science", "self_evolution", "evolution_apply", "strategy_champion",
    "adaptive_engine", "api_adaptive", "ai_review_service", "dual_ai_tuner",
    "deepseek_advisor", "deepseek_research", "ai_analysis",
    "ai_research_provider", "ai_provider_transport",
    "urllib", "requests", "httpx", "socket", "subprocess",
}


class RepositoryTestCase(unittest.TestCase):
    """把所有内存库登记到 ``addCleanup`` —— 用例不会留下未关闭的连接告警。

    刻意**不**用真实数据库文件：全部用例跑在 ``:memory:`` 上，因此仓库里不会出现
    任何临时 DB 产物。
    """

    def conn(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        REP.ensure_schema(conn)
        return conn

    def lenient_conn(self):
        """一个**不受本模块 CHECK 约束**的同名表。

        用来模拟"由别的 writer（或更早的版本）造出来的行"：本模块的 schema 保证自己
        写不出那样的行，但持久化层不能假设**只有自己**写过这张表。
        """
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            f"CREATE TABLE {REP.TABLE}("
            + ", ".join(f'"{name}"' for name in REP.RUN_COLUMNS)
            + ")"
        )
        return conn


# ─────────────────────────────────────────────────────────────────────────────
# helpers —— 走真实 R24 → R27 路径产出 typed hypothesis
# ─────────────────────────────────────────────────────────────────────────────


def _cross_source_ref(code=CODE):
    """逐票双源核验的 market reading（真实路径：snapshot → classify → evidence_ref）。"""
    snapshot = MDC.symbol_quote_snapshot(
        {"code": code, "price": 10.5, "quote_at": OBSERVED_AT,
         "quote_source": "eastmoney", "quote_validation": VALIDATION_CROSS_SOURCE},
        asof_day=DAY,
    )
    return ARC.evidence_ref_from_market_reading(
        MDC.classify(snapshot, MDC.policy_named(POLICY), now=OBSERVED_AT, asof_day=DAY),
    )


def _single_source_ref(code=CODE):
    snapshot = MDC.symbol_quote_snapshot(
        {"code": code, "price": 10.5, "quote_at": OBSERVED_AT,
         "quote_source": "eastmoney", "quote_validation": VALIDATION_SINGLE_SOURCE},
        asof_day=DAY,
    )
    return ARC.evidence_ref_from_market_reading(
        MDC.classify(snapshot, MDC.policy_named(POLICY), now=OBSERVED_AT, asof_day=DAY),
    )


def _coverage_integrity_ref(code=CODE):
    """``verified`` + ``coverage_integrity`` —— R24 的永久不变量：**不是**逐票双源。

    刻意不手工伪造 ``verification="verified"`` 字符串，而是构造一个真实声明了该
    ``(verification, verification_method)`` 的 R24 snapshot，再让它自己 classify。
    """
    snapshot = MDC.MarketDataSnapshot(
        kind="symbol_quote",
        rows=({"code": code, "price": 10.5, "quote_at": OBSERVED_AT},),
        as_of=DAY, observed_at=OBSERVED_AT, source="eastmoney", complete=True,
        expected_rows=1,
        verification=MDC.VERIFICATION_VERIFIED,
        verification_method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
    )
    return ARC.evidence_ref_from_market_reading(
        MDC.classify(snapshot, MDC.policy_named(POLICY), now=OBSERVED_AT, asof_day=DAY),
    )


def _hypothesis(*, ref=None, relation=ARC.RELATION_SUPPORTS, evidence=None,
                confidence=0.62, hypothesis_id="H-1", subject=CODE, thesis="momentum 延续"):
    if evidence is None:
        evidence = (ARC.HypothesisEvidence(ref=ref or _cross_source_ref(), relation=relation),)
    return ARC.ResearchHypothesis(
        hypothesis_id=hypothesis_id, as_of=DAY, subject=subject, thesis=thesis,
        evidence=tuple(evidence), confidence=confidence,
    )


def _plain_hypothesis(**kwargs):
    """没有任何 evidence 的假设 —— ``insufficient_evidence`` / ``no_evidence``。"""
    return _hypothesis(evidence=(), **kwargs)


#: ``_append`` 的哨兵 —— 让显式传入的 ``None`` **不被**当成"没传"。
_MISSING = object()


def _append(conn, hypothesis=_MISSING, **overrides):
    call = {
        "purpose": "manual_research",
        "trigger": "cli",
        "created_at": CREATED_AT,
    }
    if hypothesis is _MISSING:
        call["hypothesis"] = _hypothesis()
    else:
        call["hypothesis"] = hypothesis
    call.update(overrides)
    return REP.append_run(conn, **call)


def _row_dict(conn, run_id):
    """直读原始行（绕过投影），供损坏注入与列检查使用。"""
    cursor = conn.execute(f'SELECT {", ".join(REP.RUN_COLUMNS)} FROM {REP.TABLE} WHERE id=?',
                          (run_id,))
    names = [item[0] for item in cursor.description]
    return dict(zip(names, cursor.fetchone(), strict=False))


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-01 ~ 02 —— schema 与 typed 输入
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceSchemaTests(RepositoryTestCase):
    def test_RPERSIST_01_ensure_schema_is_idempotent(self):
        """RPERSIST-01：``ensure_schema`` 可重复调用，且只新增。"""
        conn = self.conn()
        first = REP.ensure_schema(conn)
        second = REP.ensure_schema(conn)
        self.assertEqual(first, second)
        self.assertEqual(first, REP.ensure_schema(conn))
        self.assertEqual(first["table"], REP.TABLE)

        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn(REP.TABLE, tables)

        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        for name in first["indexes"]:
            self.assertIn(name, indexes)

        # 幂等必须包含"不重复建索引"，也不在第二次调用时改表结构。
        before = conn.execute(f"PRAGMA table_info({REP.TABLE})").fetchall()
        REP.ensure_schema(conn)
        self.assertEqual(before, conn.execute(f"PRAGMA table_info({REP.TABLE})").fetchall())

        # 表名刻意不携带厂商 / tuning 语义。
        for forbidden in ("deepseek", "advisor", "adaptive_ai"):
            self.assertNotIn(forbidden, REP.TABLE)

    def test_RPERSIST_01b_schema_pins_the_authority_invariants(self):
        """RPERSIST-01b：两条核心不变量写进 schema，而不是只靠调用方自觉。"""
        conn = self.conn()
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({REP.TABLE})")}
        self.assertEqual(set(REP.RUN_COLUMNS), columns)

        # 直接写一行自称 authority 的记录：必须被 CHECK 拒绝。
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f'INSERT INTO {REP.TABLE}(purpose, "trigger", hypothesis_id, as_of, subject, '
                f"status, confidence, authority, is_authoritative, hypothesis, "
                f"counter_arguments, record_hash, created_at) "
                f"VALUES('p','t','H','{DAY}','{CODE}','supported',0.5,'signal',0,'{{}}','[]','h','c')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f'INSERT INTO {REP.TABLE}(purpose, "trigger", hypothesis_id, as_of, subject, '
                f"status, confidence, authority, is_authoritative, hypothesis, "
                f"counter_arguments, record_hash, created_at) "
                f"VALUES('p','t','H','{DAY}','{CODE}','supported',0.5,'research',1,'{{}}','[]','h','c')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f'INSERT INTO {REP.TABLE}(purpose, "trigger", hypothesis_id, as_of, subject, '
                f"status, confidence, authority, is_authoritative, hypothesis, "
                f"counter_arguments, record_hash, created_at) "
                f"VALUES('p','t','H','{DAY}','{CODE}','supported',1.5,'research',0,'{{}}','[]','h','c')"
            )

    def test_RPERSIST_01c_schema_declares_no_business_uniqueness(self):
        """RPERSIST-01c：**没有** UNIQUE 约束 —— 不擅自引入业务幂等语义。

        R27-A 从未声明"同一个 ``hypothesis_id`` 只能持久化一次"。加一个 UNIQUE 就等于
        由持久化层自己发明一条业务规则，并让"两次明确执行"变成错误。
        """
        conn = self.conn()
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (REP.TABLE,),
        ).fetchone()[0]
        self.assertNotIn("UNIQUE", ddl.upper())

        # 索引里也没有唯一索引（``PRAGMA index_list`` 第 3 列是 unique 标志）。
        unique_indexes = [
            row[1] for row in conn.execute(f"PRAGMA index_list({REP.TABLE})") if row[2]
        ]
        self.assertEqual([], unique_indexes)
        self.assertEqual(
            sorted(REP.ensure_schema(conn)["indexes"]),
            sorted(row[1] for row in conn.execute(f"PRAGMA index_list({REP.TABLE})")),
        )


class ResearchPersistenceTypedInputTests(RepositoryTestCase):
    def test_RPERSIST_02_only_typed_hypotheses_are_accepted(self):
        """RPERSIST-02：dict / 裸字符串 / duck-typed 假对象一律 fail closed。"""
        conn = self.conn()
        fake = type("FakeHypothesis", (), {
            "status": "supported", "reason": None, "confidence": 1.0,
            "authority": "signal", "is_authoritative": True,
            "hypothesis_id": "H", "as_of": DAY, "subject": CODE, "thesis": "t",
        })()

        for bad in (
            {"status": "supported", "authority": "research"},
            "supported",
            None,
            42,
            fake,
            _hypothesis().projection(),
        ):
            with self.subTest(value=type(bad).__name__):
                with self.assertRaises(TypeError) as caught:
                    _append(conn, hypothesis=bad)
                self.assertIn("ResearchHypothesis", str(caught.exception))
                self.assertIn("不得冒充", str(caught.exception))

        # 一个都没有落库 —— 拒绝发生在任何写入之前。
        self.assertEqual([], REP.recent_runs(conn))

    def test_RPERSIST_06_append_run_exposes_no_caller_supplied_verdict(self):
        """RPERSIST-06：签名里**根本没有**裁决 / 核验参数。

        这不是约定而是类型事实：调用方无法自述 ``status`` / ``reason`` / ``authority`` /
        ``verification``。用等值断言锁住整个签名，顺带锁住"没有人日后悄悄加回来"。
        """
        parameters = set(inspect.signature(REP.append_run).parameters) - {"conn"}
        self.assertEqual(set(), parameters & FORBIDDEN_CALLER_FIELDS)
        self.assertEqual(
            {
                "hypothesis", "purpose", "trigger", "created_at",
                "provider_slot", "provider_model", "narrative", "counter_arguments",
                "input_tokens", "output_tokens", "latency_ms",
            },
            parameters,
        )

        # 位置参数被禁用（keyword-only）：调用方不能靠位置把字段塞进错误的语义。
        signature = inspect.signature(REP.append_run)
        for name, parameter in signature.parameters.items():
            if name == "conn":
                continue
            self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)

    def test_RPERSIST_06b_caller_metadata_cannot_override_derived_columns(self):
        """RPERSIST-06b：即便把裁决字段塞进 metadata，也影响不了派生列。"""
        conn = self.conn()
        hypothesis = _hypothesis()
        run_id = _append(
            conn, hypothesis=hypothesis,
            purpose="signal", trigger="status=supported",
            narrative="authority=research status=supported",
            provider_slot="ai9",
        )
        row = REP.get_run(conn, run_id)
        self.assertEqual(hypothesis.status, row["status"])
        self.assertEqual("research", row["authority"])
        self.assertIs(False, row["is_authoritative"])


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-03 ~ 05 —— 派生值
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceDerivedValueTests(RepositoryTestCase):
    def test_RPERSIST_03_supported_is_derived_from_the_typed_hypothesis(self):
        """RPERSIST-03：supported 落库后 status / reason / authority / 非权威全部照抄派生值。"""
        conn = self.conn()
        hypothesis = _hypothesis(relation=ARC.RELATION_SUPPORTS)
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, hypothesis.status)

        row = REP.get_run(conn, _append(conn, hypothesis=hypothesis))
        self.assertEqual(hypothesis.status, row["status"])
        self.assertEqual(hypothesis.reason, row["reason"])
        self.assertEqual(hypothesis.confidence, row["confidence"])
        self.assertEqual("research", row["authority"])
        self.assertIs(False, row["is_authoritative"])
        self.assertIsNone(row["reason"])
        # 投影里的 hypothesis JSON 也只渲染契约给的东西。
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, row["hypothesis"]["status"])
        self.assertIs(False, row["hypothesis"]["is_authoritative"])

    def test_RPERSIST_04_unsupported_keeps_the_derived_reason(self):
        """RPERSIST-04：unsupported 的 derived reason 逐字保存。"""
        conn = self.conn()
        hypothesis = _hypothesis(relation=ARC.RELATION_CONTRADICTS)
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, hypothesis.status)

        row = REP.get_run(conn, _append(conn, hypothesis=hypothesis))
        self.assertEqual(ARC.HYPOTHESIS_UNSUPPORTED, row["status"])
        self.assertEqual(ARC.RESEARCH_REASON_EVIDENCE_CONTRADICTED, row["reason"])
        self.assertEqual(hypothesis.reason, row["reason"])

    def test_RPERSIST_05_insufficient_evidence_keeps_the_derived_reason(self):
        """RPERSIST-05：insufficient_evidence 的各种 reason 都被准确保存。"""
        conn = self.conn()
        cases = (
            (_plain_hypothesis(), ARC.RESEARCH_REASON_NO_EVIDENCE),
            (
                _hypothesis(ref=_single_source_ref(), relation=ARC.RELATION_SUPPORTS),
                ARC.RESEARCH_REASON_EVIDENCE_NOT_VERIFIED,
            ),
            (
                _hypothesis(ref=_cross_source_ref(), relation=ARC.RELATION_CONTEXT),
                ARC.RESEARCH_REASON_NO_SUPPORTING_EVIDENCE,
            ),
        )
        for hypothesis, expected in cases:
            with self.subTest(reason=expected):
                self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, hypothesis.status)
                row = REP.get_run(conn, _append(conn, hypothesis=hypothesis))
                self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, row["status"])
                self.assertEqual(expected, row["reason"])
                self.assertEqual(hypothesis.reason, row["reason"])

    def test_RPERSIST_05b_confidence_never_influences_the_persisted_status(self):
        """RPERSIST-05b：confidence 不参与 status 判定 —— 高自信的空假设仍不是 supported。"""
        conn = self.conn()
        for confidence in (0.0, 0.5, 1.0):
            with self.subTest(confidence=confidence):
                hypothesis = _plain_hypothesis(confidence=confidence)
                row = REP.get_run(conn, _append(conn, hypothesis=hypothesis))
                self.assertEqual(confidence, row["confidence"])
                self.assertEqual(ARC.HYPOTHESIS_INSUFFICIENT_EVIDENCE, row["status"])
                self.assertEqual(ARC.RESEARCH_REASON_NO_EVIDENCE, row["reason"])


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-07 ~ 09 —— evidence 维度逐字保存
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceEvidenceTests(RepositoryTestCase):
    """持久化只**记录** typed contract 给出的核验结果，绝不重算。"""

    def _persisted_evidence(self, conn, hypothesis):
        run_id = _append(conn, hypothesis=hypothesis)
        return REP.get_run(conn, run_id)["hypothesis"]["evidence"]

    def test_RPERSIST_07_single_source_stays_single_source(self):
        """RPERSIST-07：single_source 落库后仍是 single_source，不升级成 verified。"""
        ref = _single_source_ref()
        self.assertNotEqual(MDC.VERIFICATION_VERIFIED, ref.verification)

        evidence = self._persisted_evidence(
            self.conn(), _hypothesis(ref=ref, relation=ARC.RELATION_SUPPORTS),
        )
        self.assertEqual(1, len(evidence))
        self.assertEqual(ref.verification, evidence[0]["verification"])
        self.assertEqual(ref.verification_method, evidence[0]["verification_method"])
        self.assertIs(False, evidence[0]["cross_source_verified"])
        self.assertNotEqual(MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
                            evidence[0]["verification_method"])

    def test_RPERSIST_08_coverage_integrity_verified_is_not_cross_source(self):
        """RPERSIST-08：``verified`` + ``coverage_integrity`` 落库后仍**不是**逐票双源。

        这是 R24 的永久不变量。数据库**不能**重新解释它，更不能把它算成
        ``cross_source_verified == (verification == "verified")``。
        """
        ref = _coverage_integrity_ref()
        self.assertEqual(MDC.VERIFICATION_VERIFIED, ref.verification)
        self.assertEqual(MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY, ref.verification_method)
        self.assertIs(False, ref.cross_source_verified)

        evidence = self._persisted_evidence(
            self.conn(), _hypothesis(ref=ref, relation=ARC.RELATION_SUPPORTS),
        )
        self.assertEqual(MDC.VERIFICATION_VERIFIED, evidence[0]["verification"])
        self.assertEqual(MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
                         evidence[0]["verification_method"])
        self.assertIs(False, evidence[0]["cross_source_verified"])

    def test_RPERSIST_08b_cross_source_evidence_records_both_dimensions(self):
        """RPERSIST-08b：逐票双源的事实两个维度都如实记录（非空性对照）。"""
        ref = _cross_source_ref()
        self.assertEqual(MDC.VERIFICATION_VERIFIED, ref.verification)
        self.assertIs(True, ref.cross_source_verified)

        evidence = self._persisted_evidence(
            self.conn(), _hypothesis(ref=ref, relation=ARC.RELATION_SUPPORTS),
        )
        self.assertEqual(MDC.VERIFICATION_VERIFIED, evidence[0]["verification"])
        self.assertIs(True, evidence[0]["cross_source_verified"])

    def test_RPERSIST_09_relation_is_saved_verbatim(self):
        """RPERSIST-09：supports / contradicts / context 逐字保存，不被改写。"""
        conn = self.conn()
        for relation in ARC.RELATIONS:
            with self.subTest(relation=relation):
                hypothesis = _hypothesis(ref=_cross_source_ref(), relation=relation)
                evidence = self._persisted_evidence(conn, hypothesis)
                self.assertEqual(relation, evidence[0]["relation"])
                self.assertEqual(hypothesis.evidence[0].projection(), evidence[0])

    def test_RPERSIST_09b_evidence_source_identity_is_saved_verbatim(self):
        """RPERSIST-09b：evidence 身份字段齐全，数据库不重建 identity。"""
        conn = self.conn()
        ref = _cross_source_ref()
        evidence = self._persisted_evidence(conn, _hypothesis(ref=ref, relation=ARC.RELATION_SUPPORTS))
        for field in ("source_type", "source_id", "as_of", "verification",
                      "verification_method", "cross_source_verified", "relation"):
            with self.subTest(field=field):
                self.assertIn(field, evidence[0])
        self.assertEqual(ref.projection()["source_id"], evidence[0]["source_id"])
        self.assertIn(POLICY, evidence[0]["source_id"])
        self.assertIn(CODE, evidence[0]["source_id"])


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-10 ~ 13 —— append-only 与 record_hash
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceAppendOnlyTests(RepositoryTestCase):
    def test_RPERSIST_10_appending_twice_creates_two_audit_runs(self):
        """RPERSIST-10：第二次 append 同一 hypothesis → 行数 +1，没有静默去重 / upsert。"""
        conn = self.conn()
        hypothesis = _hypothesis()
        first = _append(conn, hypothesis=hypothesis)
        second = _append(conn, hypothesis=hypothesis)
        third = _append(conn, hypothesis=hypothesis)

        self.assertEqual([first, second, third], sorted([first, second, third]))
        self.assertNotEqual(first, second)
        self.assertEqual(3, len(REP.recent_runs(conn)))
        self.assertEqual(3, conn.execute(f"SELECT COUNT(*) FROM {REP.TABLE}").fetchone()[0])

        # 三条记录内容相同（除 id）—— 这不是 bug，是本轮明确选择的 append-only 语义。
        projections = REP.recent_runs(conn)
        hashes = {item["record_hash"] for item in projections}
        self.assertEqual(1, len(hashes))

    def test_RPERSIST_10b_failed_append_writes_nothing(self):
        """RPERSIST-10b：校验失败时不留下半条记录。"""
        conn = self.conn()
        with self.assertRaises(ValueError):
            _append(conn, purpose="")
        with self.assertRaises(ValueError):
            _append(conn, purpose="x" * (REP.MAX_PURPOSE_CHARS + 1))
        with self.assertRaises(ValueError):
            _append(conn, trigger="x" * (REP.MAX_TRIGGER_CHARS + 1))
        with self.assertRaises(ValueError):
            _append(conn, created_at="")
        with self.assertRaises(ValueError):
            _append(conn, narrative="x" * (REP.MAX_NARRATIVE_CHARS + 1))
        with self.assertRaises(ValueError):
            _append(conn, counter_arguments=["a"] * (REP.MAX_COUNTER_ARGUMENTS + 1))
        with self.assertRaises(ValueError):
            _append(conn, counter_arguments=["x" * (REP.MAX_COUNTER_ARGUMENT_CHARS + 1)])
        with self.assertRaises(ValueError):
            _append(conn, input_tokens=-1)
        with self.assertRaises(ValueError):
            _append(conn, latency_ms=-1)
        with self.assertRaises(TypeError):
            _append(conn, input_tokens="11")
        with self.assertRaises(TypeError):
            _append(conn, counter_arguments="不是一个字符串序列")
        with self.assertRaises(TypeError):
            _append(conn, narrative=b"bytes")
        self.assertEqual([], REP.recent_runs(conn))

    def test_RPERSIST_11_record_hash_is_stable_for_identical_content(self):
        """RPERSIST-11：完全相同的内容 → 完全相同的 record_hash。"""
        conn = self.conn()
        hypothesis = _hypothesis(ref=_cross_source_ref())
        run_a = _append(conn, hypothesis=hypothesis, narrative="n", counter_arguments=["c"])
        run_b = _append(conn, hypothesis=hypothesis, narrative="n", counter_arguments=["c"])
        self.assertEqual(
            REP.get_run(conn, run_a)["record_hash"],
            REP.get_run(conn, run_b)["record_hash"],
        )

        # 内容相同但落库时刻不同 → hash 仍相同（见 RPERSIST-13）。
        run_c = _append(conn, hypothesis=hypothesis, narrative="n", counter_arguments=["c"],
                        created_at=OTHER_CREATED_AT)
        self.assertEqual(
            REP.get_run(conn, run_a)["record_hash"],
            REP.get_run(conn, run_c)["record_hash"],
        )

    def test_RPERSIST_12_record_hash_covers_the_research_content(self):
        """RPERSIST-12：thesis / evidence relation / verification_method 任一变化都改变 hash。

        这条是 record_hash 的**非空性**：一个只覆盖 ``purpose`` 的 hash 也会"稳定"，
        但它证明不了研究产物内容。
        """
        conn = self.conn()
        baseline = _hypothesis(ref=_cross_source_ref(), relation=ARC.RELATION_SUPPORTS,
                               thesis="momentum 延续")
        base_hash = REP.get_run(conn, _append(conn, hypothesis=baseline))["record_hash"]

        mutations = {
            "thesis": _hypothesis(ref=_cross_source_ref(), relation=ARC.RELATION_SUPPORTS,
                                  thesis="momentum 反转"),
            "relation": _hypothesis(ref=_cross_source_ref(),
                                    relation=ARC.RELATION_CONTRADICTS,
                                    thesis="momentum 延续"),
            "verification_method": _hypothesis(ref=_coverage_integrity_ref(),
                                               relation=ARC.RELATION_SUPPORTS,
                                               thesis="momentum 延续"),
            "hypothesis_id": _hypothesis(ref=_cross_source_ref(),
                                         relation=ARC.RELATION_SUPPORTS,
                                         thesis="momentum 延续", hypothesis_id="H-2"),
        }
        for label, hypothesis in mutations.items():
            with self.subTest(changed=label):
                changed = REP.get_run(conn, _append(conn, hypothesis=hypothesis))["record_hash"]
                self.assertNotEqual(base_hash, changed,
                                    f"{label} 变化没有改变 record_hash")

        # 反向：非研究内容（purpose 之外的 audit metadata）变化也必须改变 hash，
        # 因为它确实被持久化了。
        audit_changed = REP.get_run(
            conn, _append(conn, hypothesis=baseline, narrative="不同的叙述"),
        )["record_hash"]
        self.assertNotEqual(base_hash, audit_changed)

    def test_RPERSIST_13_created_at_is_outside_the_content_hash(self):
        """RPERSIST-13：``created_at`` **不**进入 record_hash（契约固定，测试锁住）。

        hash 表示"研究产物内容"，``created_at`` 是 operational persistence time。
        把运维时间混进内容指纹，会让同一次研究产物在不同落库时刻得到不同 hash。
        """
        conn = self.conn()
        hypothesis = _hypothesis(ref=_cross_source_ref())
        run_a = _append(conn, hypothesis=hypothesis, created_at=CREATED_AT)
        run_b = _append(conn, hypothesis=hypothesis, created_at=OTHER_CREATED_AT)

        row_a, row_b = REP.get_run(conn, run_a), REP.get_run(conn, run_b)
        self.assertEqual(CREATED_AT, row_a["created_at"])
        self.assertEqual(OTHER_CREATED_AT, row_b["created_at"])
        self.assertEqual(row_a["record_hash"], row_b["record_hash"])

        # 直接证明 hash 输入集合不含 created_at / id。
        content = {
            "purpose": row_a["purpose"],
            "hypothesis": row_a["hypothesis"],
            "provider_slot": row_a["provider_slot"],
            "provider_model": row_a["provider_model"],
            "narrative": row_a["narrative"],
            "counter_arguments": row_a["counter_arguments"],
            "input_tokens": row_a["input_tokens"],
            "output_tokens": row_a["output_tokens"],
            "latency_ms": row_a["latency_ms"],
        }
        self.assertEqual(REP._record_hash(content), row_a["record_hash"])
        self.assertNotIn("created_at", content)
        self.assertNotIn("id", content)
        self.assertNotIn("record_hash", content)


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-14 —— secret / raw prompt 边界
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceSecretTests(RepositoryTestCase):
    def test_RPERSIST_14_no_secret_or_prompt_can_reach_the_table(self):
        """RPERSIST-14：API key / Authorization / raw prompt 绝不进入本层。

        四条独立证据：
          schema 没有这种列；签名没有这种参数；投影没有这种键；模块不 import 任何
          provider 配置来源（``provider_config`` 整体不进本层）。
        """
        conn = self.conn()
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({REP.TABLE})")]
        parameters = set(inspect.signature(REP.append_run).parameters)
        keys = set(REP.get_run(conn, _append(conn)))
        keys |= set(REP.recent_runs(conn, limit=1)[0])

        for surface_name, surface in (
            ("schema columns", columns),
            ("append_run parameters", parameters),
            ("read projection keys", keys),
        ):
            for token in SECRET_FIELD_TOKENS:
                with self.subTest(surface=surface_name, token=token):
                    hits = [item for item in surface if token in item.lower()]
                    self.assertEqual([], hits,
                                     f"{surface_name} 出现了 secret/raw prompt 相关字段 {hits}")

        # 模块不 import 任何 provider 配置来源：本层没有"顺手拿一份 provider_config"的路。
        roots = _imported_roots(_tree(MODULE))
        for forbidden in ("ai_review_service", "os", "urllib", "requests", "httpx"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, roots)

    def test_RPERSIST_14b_record_hash_input_excludes_secret_fields(self):
        """RPERSIST-14b：hash 输入集合恰好是研究内容 + audit label，没有别的。"""
        conn = self.conn()
        row = REP.get_run(conn, _append(conn, provider_slot="ai1", provider_model="m"))
        content = {
            "purpose": row["purpose"],
            "hypothesis": row["hypothesis"],
            "provider_slot": row["provider_slot"],
            "provider_model": row["provider_model"],
            "narrative": row["narrative"],
            "counter_arguments": row["counter_arguments"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "latency_ms": row["latency_ms"],
        }
        self.assertEqual(REP._record_hash(content), row["record_hash"])
        for token in SECRET_FIELD_TOKENS:
            with self.subTest(token=token):
                self.assertEqual([], [key for key in content if token in key.lower()])

        # 序列化后的记录里也不含任何 secret 形状的键。
        raw = _row_dict(conn, row["id"])
        blob = json.dumps({key: str(value) for key, value in raw.items()},
                          ensure_ascii=False).lower()
        for token in ("api_key", "authorization", "bearer", "raw_response", "system_prompt"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-15 ~ 19 —— 损坏 fail closed、读取有界与顺序
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceCorruptReadTests(RepositoryTestCase):
    def test_RPERSIST_15_corrupt_hypothesis_json_fails_closed(self):
        """RPERSIST-15：hypothesis JSON 损坏 → :class:`ResearchPersistenceError`，**不**返回 {}。"""
        conn = self.conn()
        run_id = _append(conn)
        conn.execute(f"UPDATE {REP.TABLE} SET hypothesis=? WHERE id=?", ("{不是 JSON", run_id))

        for reader in (lambda: REP.recent_runs(conn), lambda: REP.get_run(conn, run_id)):
            with self.subTest(reader=reader):
                with self.assertRaises(REP.ResearchPersistenceError) as caught:
                    reader()
                self.assertEqual(REP.REASON_CORRUPT_RECORD, caught.exception.reason)

        # 非 object 的合法 JSON 同样拒绝。
        conn.execute(f"UPDATE {REP.TABLE} SET hypothesis=? WHERE id=?", ("[1,2]", run_id))
        with self.assertRaises(REP.ResearchPersistenceError):
            REP.get_run(conn, run_id)

    def test_RPERSIST_16_corrupt_counter_arguments_fails_closed(self):
        """RPERSIST-16：counter_arguments JSON 损坏同样 fail closed。"""
        conn = self.conn()
        run_id = _append(conn, counter_arguments=["a", "b"])
        for broken in ("{", "null", '"a string"', "42"):
            with self.subTest(value=broken):
                conn.execute(
                    f"UPDATE {REP.TABLE} SET counter_arguments=? WHERE id=?", (broken, run_id),
                )
                with self.assertRaises(REP.ResearchPersistenceError) as caught:
                    REP.recent_runs(conn)
                self.assertEqual(REP.REASON_CORRUPT_RECORD, caught.exception.reason)

    def test_RPERSIST_16b_corrupt_errors_do_not_echo_the_damaged_payload(self):
        """RPERSIST-16b：fail-closed 错误不回显原始损坏内容（也不回显 SQL / raw row）。"""
        conn = self.conn()
        run_id = _append(conn)
        marker = "SENSITIVE-MARKER-DO-NOT-ECHO"
        conn.execute(
            f"UPDATE {REP.TABLE} SET hypothesis=? WHERE id=?",
            (f'{{"leak": "{marker}"', run_id),
        )
        with self.assertRaises(REP.ResearchPersistenceError) as caught:
            REP.get_run(conn, run_id)
        blob = "%s|%r|%s" % (caught.exception, caught.exception, caught.exception.reason)
        self.assertNotIn(marker, blob)
        self.assertNotIn("SELECT", blob.upper())
        self.assertEqual(REP.REASON_CORRUPT_RECORD, caught.exception.reason)

    def test_RPERSIST_17_limit_is_bounded(self):
        """RPERSIST-17：``limit`` 有硬上界，非法值不得造成无限查询。"""
        conn = self.conn()
        for _ in range(3):
            _append(conn)

        self.assertEqual(3, len(REP.recent_runs(conn, limit=REP.MAX_ROWS)))
        for bad in (0, -1, REP.MAX_ROWS + 1, 10 ** 9):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    REP.recent_runs(conn, limit=bad)
        for bad in ("10", 2.5, None, True):
            with self.subTest(limit=repr(bad)):
                with self.assertRaises(TypeError):
                    REP.recent_runs(conn, limit=bad)

    def test_RPERSIST_18_recent_runs_order_is_stable(self):
        """RPERSIST-18：按 ``id DESC``（自增持久化顺序）稳定返回。

        刻意**不**按 ``created_at`` 排序：那个值由调用方提供，可能相同、可能乱序；
        用它排序会让"最近"变成"调用方传了什么"的函数。
        """
        conn = self.conn()
        ids = [
            _append(conn, created_at=OTHER_CREATED_AT),
            _append(conn, created_at=CREATED_AT),
            _append(conn, created_at=OTHER_CREATED_AT),
        ]
        self.assertEqual(list(reversed(ids)), [item["id"] for item in REP.recent_runs(conn)])
        self.assertEqual(list(reversed(ids)), [item["id"] for item in REP.recent_runs(conn)])
        self.assertEqual([ids[-1]], [item["id"] for item in REP.recent_runs(conn, limit=1)])

    def test_RPERSIST_19_filters_are_exact(self):
        """RPERSIST-19：``as_of`` / ``subject`` 过滤精确，且不接受空值。"""
        conn = self.conn()
        here = _append(conn, hypothesis=_hypothesis(subject=CODE))
        other = _append(conn, hypothesis=_hypothesis(subject="000001",
                                                    ref=_cross_source_ref(code="000001")))

        self.assertEqual({here, other},
                         {item["id"] for item in REP.recent_runs(conn, as_of=DAY)})
        self.assertEqual([here],
                         [item["id"] for item in REP.recent_runs(conn, subject=CODE)])
        self.assertEqual([other],
                         [item["id"] for item in REP.recent_runs(conn, subject="000001")])
        self.assertEqual(
            [here],
            [item["id"] for item in REP.recent_runs(conn, as_of=DAY, subject=CODE)],
        )
        self.assertEqual([], REP.recent_runs(conn, as_of="2026-01-01"))
        self.assertEqual([], REP.recent_runs(conn, subject="不存在"))

        for bad in ("", "   ", None, 42):
            with self.subTest(as_of=repr(bad)):
                if bad is None:
                    self.assertEqual(2, len(REP.recent_runs(conn, as_of=None)))
                    continue
                with self.assertRaises((TypeError, ValueError)):
                    REP.recent_runs(conn, as_of=bad)


# ─────────────────────────────────────────────────────────────────────────────
# RPERSIST-20 ~ 25 —— 架构边界
# ─────────────────────────────────────────────────────────────────────────────


class ResearchPersistenceBoundaryGuardTests(RepositoryTestCase):
    def test_RPERSIST_20_repository_never_reads_a_clock(self):
        """RPERSIST-20：repository 不读墙上时钟；``created_at`` 必须由调用方显式提供。"""
        tree = _tree(MODULE)
        roots = _imported_roots(tree)
        for forbidden in ("time", "datetime", "random", "uuid", "zoneinfo"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, roots, f"{MODULE} import 了 {forbidden}")

        called = _called_names(tree)
        for forbidden in ("now", "utcnow", "today", "localtime", "gmtime",
                          "monotonic", "perf_counter", "time", "time_ns"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called, f"{MODULE} 调用了 {forbidden}()")

        # created_at 没有默认值 —— 无法"忘了传就自动填现在"。
        parameters = inspect.signature(REP.append_run).parameters
        self.assertEqual(inspect.Parameter.empty, parameters["created_at"].default)
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameters["created_at"].kind)

    def test_RPERSIST_21_repository_has_no_network_dependency(self):
        """RPERSIST-21：repository 无网络依赖、无网络调用。"""
        tree = _tree(MODULE)
        roots = _imported_roots(tree)
        for forbidden in ("urllib", "requests", "httpx", "socket", "http", "ssl",
                          "aiohttp", "asyncio"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, roots, f"{MODULE} import 了 {forbidden}")

        called = _called_names(tree)
        for forbidden in ("urlopen", "urlretrieve", "Request", "socket", "create_connection"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, called, f"{MODULE} 调用了 {forbidden}()")

        # 只看**代码面**（docstring 被排除）：说明文字里写着"绝不带 Authorization"
        # 是文档，不是网络调用。
        code_surface = " ".join(sorted(_sql_strings(_tree(MODULE))))
        for token in ("http://", "https://", "Authorization", "Bearer"):
            with self.subTest(token=token):
                self.assertNotIn(token, code_surface, f"{MODULE} 代码面出现了 {token}")

    def test_RPERSIST_22_repository_imports_only_stdlib_and_the_contract(self):
        """RPERSIST-22：repository 只依赖 stdlib 与 R27-A 契约，**不** import 任何 authority。"""
        roots = _imported_roots(_tree(MODULE))
        self.assertEqual(set(), roots & FORBIDDEN_IMPORTS,
                         f"{MODULE} import 了被禁止的模块 {sorted(roots & FORBIDDEN_IMPORTS)}")
        self.assertEqual(ALLOWED_IMPORTS, roots,
                         "出现了未登记的 import，请审慎评估后再放行")

        project_modules = {name[:-3] for name in os.listdir(BACKEND) if name.endswith(".py")}
        leaked = sorted((roots & project_modules) - {"ai_research_contract"})
        self.assertEqual([], leaked, f"{MODULE} import 了未登记的项目模块 {leaked}")

    def test_RPERSIST_23_the_repository_is_the_only_canonical_writer(self):
        """RPERSIST-23：``INSERT INTO ai_research_runs`` 只允许出现在 repository 里。

        同时断言**非空性**：repository 本身必须真的持有这条 INSERT，否则
        "唯一 writer"会被一个根本不存在 writer 的树满足。
        """
        writers = _canonical_insert_files()
        self.assertEqual([MODULE], sorted(writers),
                         f"canonical research 写入口不唯一：{sorted(writers)}")
        self.assertTrue(writers[MODULE], "repository 里没有找到 canonical INSERT（护栏空转）")
        self.assertIn(f"INSERT INTO {REP.TABLE}", writers[MODULE][0].replace('"', ""))

    def test_RPERSIST_24_no_update_delete_replace_or_upsert_on_the_canonical_table(self):
        """RPERSIST-24：任何引用 canonical 表的 SQL 都不得是改数据语句。

        append-only 不是文档承诺，而是可扫描的事实：production tree 里凡是提到
        ``ai_research_runs`` 的 SQL，只能是 CREATE / INSERT / SELECT。
        """
        offenders = []
        scanned = 0
        for name in _production_modules():
            for sql in _sql_strings(_tree(name)):
                flat = sql.replace('"', "")
                if REP.TABLE not in flat:
                    continue
                scanned += 1
                upper = flat.upper()
                for keyword in ("UPDATE ", "DELETE FROM", "REPLACE INTO", "DROP TABLE",
                                "ALTER TABLE", "OR REPLACE", "ON CONFLICT", "UPSERT"):
                    if keyword in upper:
                        offenders.append(f"{name}: {keyword.strip()} -> {flat[:80]}")
        self.assertEqual([], offenders, f"canonical 台账出现改数据语句：{offenders}")
        # 非空性：扫描器确实看到了引用该表的语句。
        self.assertGreater(scanned, 0, "扫描器没有看到任何 ai_research_runs 语句")

    def test_RPERSIST_25_db_projection_is_never_authoritative(self):
        """RPERSIST-25：DB 读投影的 ``is_authoritative`` 恒为 ``False``。

        即便是 ``status == supported`` 的历史行，也不会因为"结论很强"就变成权威；
        而且 schema 的 ``CHECK`` 之外，读路径自己还要再挡一次由**别的 writer**
        造出来的自称权威的行。
        """
        conn = self.conn()
        supported = _hypothesis(relation=ARC.RELATION_SUPPORTS)
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, supported.status)
        row = REP.get_run(conn, _append(conn, hypothesis=supported))
        self.assertEqual(ARC.HYPOTHESIS_SUPPORTED, row["status"])
        self.assertIs(False, row["is_authoritative"])
        self.assertEqual("research", row["authority"])
        self.assertIs(False, row["hypothesis"]["is_authoritative"])

        # 另一条 supported 记录读出来同样不是权威。
        for item in REP.recent_runs(conn):
            with self.subTest(run=item["id"]):
                self.assertIs(False, item["is_authoritative"])
                self.assertEqual("research", item["authority"])

        # 无 CHECK 的同名表（模拟别的 writer/更早的版本）：自称权威的行必须 fail closed，
        # 而不是被当成"这条研究现在是权威结论"读出来。
        for claimed in (("signal", 0), ("research", 1)):
            with self.subTest(claimed=claimed):
                foreign = self.lenient_conn()
                foreign.execute(
                    f'INSERT INTO {REP.TABLE}({", ".join(REP.RUN_COLUMNS)}) '
                    f"VALUES(" + ", ".join("?" for _ in REP.RUN_COLUMNS) + ")",
                    _foreign_row(authority=claimed[0], is_authoritative=claimed[1]),
                )
                with self.assertRaises(REP.ResearchPersistenceError) as caught:
                    REP.recent_runs(foreign)
                self.assertEqual(REP.REASON_CORRUPT_RECORD, caught.exception.reason)

    def test_RPERSIST_25b_repository_never_becomes_a_signal_or_promotion_writer(self):
        """RPERSIST-25b：本层没有任何 signal / order / risk / promotion 写路径。"""
        tree = _tree(MODULE)
        surface = " ".join(sorted(_sql_strings(tree))) + " " + " ".join(
            sorted(_called_names(tree))
        )
        for forbidden in ("commit_signal", "apply_tuner_proposals", "paper_signals",
                          "paper_orders", "paper_fills", "risk_decision",
                          "promotion", "evolution_apply", "SignalDecision"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, surface)

        # 也不从持久化记录重建 research 语义：读路径不 import 契约的构造函数之外的判定。
        for forbidden in ("_derive_status", "evidence_ref_from_market_reading",
                          "HypothesisEvidence", "InformationEvent"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, surface,
                                 f"{MODULE} 在读路径重新构造 research 语义")


# ─────────────────────────────────────────────────────────────────────────────
# 测试基础设施：内存库、外来行、AST 扫描
# ─────────────────────────────────────────────────────────────────────────────


def _foreign_row(*, authority="research", is_authoritative=0):
    """一行"合法 JSON、但自称权威"的记录（列序按 :data:`REP.RUN_COLUMNS`）。"""
    values = {
        "id": 1,
        "purpose": "p",
        "trigger": "t",
        "hypothesis_id": "H-1",
        "as_of": DAY,
        "subject": CODE,
        "status": "supported",
        "reason": None,
        "confidence": 0.5,
        "authority": authority,
        "is_authoritative": is_authoritative,
        "provider_slot": None,
        "provider_model": "",
        "hypothesis": json.dumps({"hypothesis_id": "H-1"}, ensure_ascii=False),
        "narrative": "",
        "counter_arguments": json.dumps([]),
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
        "record_hash": "h",
        "created_at": CREATED_AT,
    }
    return tuple(values[name] for name in REP.RUN_COLUMNS)


def _source(name):
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _tree(name):
    return ast.parse(_source(name))


def _imported_roots(tree):
    """import 的顶层模块名（``import a.b`` 取 ``a``）。"""
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _called_names(tree):
    """被调用的名字 / 属性名 / 参数名 / 关键字参数名。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
    return out


def _docstring_ids(tree):
    """所有 docstring 常量节点的 ``id()``（模块 / 类 / 函数的第一条字符串表达式）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _table_constant_names(tree):
    """模块级 ``NAME = "ai_research_runs"`` 形式的常量名。

    f-string 在 AST 里是 ``JoinedStr``，``{TABLE}`` 会被 ``ast.unparse`` 还原成标识符
    ``TABLE`` 而不是它的值。扫描器必须知道"哪些名字就是 canonical 表名"，否则
    ``f"INSERT INTO {TABLE} ..."`` 这种写法会整个绕过 writer-ownership 护栏 ——
    而真实写入口正是这种形式。
    """
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and \
                node.value.value == REP.TABLE:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _sql_strings(tree):
    """源码里所有**可拼出 SQL 文本**的字符串（含 f-string），docstring 除外。

    docstring 必须排除：本模块的说明文字里**故意**写着"没有 UPDATE / DELETE /
    ``ON CONFLICT DO UPDATE``"，那是文档而不是语句。把说明当成语句会让护栏反过来禁止
    解释它自己，也会让"护栏忽略 docstring"这件事失去非空性。
    """
    docstrings = _docstring_ids(tree)
    aliases = _table_constant_names(tree)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.add(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    rendered = ast.unparse(value.value)
                    parts.append(REP.TABLE if rendered in aliases else rendered)
            out.add("".join(parts))
    return out


def _production_modules():
    """backend 生产模块（``test_*`` 不在其中）。"""
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _canonical_insert_files():
    """production tree 里出现 ``INSERT INTO ai_research_runs`` 的文件 → 语句列表。"""
    hits = {}
    for name in _production_modules():
        for sql in _sql_strings(_tree(name)):
            if f"INSERT INTO {REP.TABLE}" in sql.replace('"', ""):
                hits.setdefault(name, []).append(sql)
    return hits


class GuardIsNotVacuouslyPassing(RepositoryTestCase):
    """护栏必须真的能失败；否则它只是装饰。"""

    def test_sql_scanner_sees_the_fstring_form(self):
        """扫描器必须看得见 ``f"INSERT INTO {TABLE} ..."`` —— 真实写入口正是这种形式。"""
        tree = ast.parse(f'TABLE = "{REP.TABLE}"\nSQL = f"INSERT INTO {{TABLE}}(a) VALUES(?)"\n')
        self.assertTrue(
            any(f"INSERT INTO {REP.TABLE}" in sql for sql in _sql_strings(tree)),
            "扫描器对 f-string 形式失明：writer-ownership 护栏会静默空转",
        )

    def test_sql_scanner_sees_the_literal_form(self):
        tree = ast.parse(f'SQL = "INSERT INTO {REP.TABLE}(a) VALUES(?)"\n')
        self.assertTrue(
            any(f"INSERT INTO {REP.TABLE}" in sql for sql in _sql_strings(tree)),
        )

    def test_sql_scanner_fires_on_a_mutating_statement(self):
        tree = ast.parse(f'TABLE = "{REP.TABLE}"\nSQL = f"DELETE FROM {{TABLE}}"\n')
        self.assertTrue(
            any(
                "DELETE FROM" in sql.upper() and REP.TABLE in sql
                for sql in _sql_strings(tree)
            ),
            "扫描器对引用 canonical 表的 DELETE 失明",
        )

    def test_sql_scanner_ignores_docstrings(self):
        """文档里解释"禁止 UPDATE"不该让护栏变红。"""
        tree = ast.parse(f'"""Never UPDATE {REP.TABLE} here."""\nX = 1\n')
        self.assertEqual(
            set(), {sql for sql in _sql_strings(tree) if "UPDATE" in sql.upper()},
        )

    def test_writer_guard_can_actually_fail(self):
        """canonical INSERT 被换成 UPDATE 时，RPERSIST-23/24 的判据必须能识别出来。"""
        tree = ast.parse(
            f'TABLE = "{REP.TABLE}"\n'
            'SQL = f"INSERT INTO {TABLE}(a) VALUES(?)"\n'
            'OTHER = f"UPDATE {TABLE} SET a=1"\n'
        )
        sqls = [item.replace('"', "") for item in _sql_strings(tree)]
        self.assertTrue(any(f"INSERT INTO {REP.TABLE}" in item for item in sqls))
        self.assertTrue(any(f"UPDATE {REP.TABLE}" in item for item in sqls))

    def test_import_scanner_reads_from_imports_too(self):
        self.assertIn("ai_research_contract",
                      _imported_roots(ast.parse("from ai_research_contract import X\n")))

    def test_corrupt_scan_fixture_is_really_corrupt(self):
        """RPERSIST-15/16 的注入确实把 JSON 弄坏，而不是恰好也是合法 JSON。"""
        for broken in ("{不是 JSON", "[1,2]", "null", '"a string"', "42"):
            with self.subTest(value=broken):
                try:
                    parsed = json.loads(broken)
                except ValueError:
                    continue
                self.assertNotIsInstance(parsed, dict, f"{broken} 其实是合法 object")


if __name__ == "__main__":
    unittest.main()


