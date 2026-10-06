# -*- coding: utf-8 -*-
"""R36-A —— Experiment Search Controller 契约测试（S1–S22）。

覆盖的不变量：

* search run 的输入必须是 **exact generation_batch_id**（无 latest/current fallback）
* candidate pool 全部来自该 exact batch，且每个 candidate 从 canonical ledger 自证
* 未知 candidate schema fail closed；v1 / v2 继续自证
* candidate subset 显式、去重、order-independent
* budget overflow **fail closed**，绝不截断
* SearchSpec 是 content identity，search_run_id 是独立 opaque event identity
* N jobs + N queued events 原子创建；job declaration 不可变
* job 运营状态只来自 append-only events，``event_seq`` 是唯一定序权威
* retry 有上限；completed / cancelled 是终态；claim 并发安全
* claim 顺序 deterministic 且**非 ranking**
* controller 不存/不读实验指标，不执行 R29/R30，不依赖 AI provider
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import experiment_search_contract as ESC
import experiment_search_repository as ESR
import experiment_search_service as ESS
import paper_schema_migrations as PSM
import strategy_candidate as SC
import strategy_candidate_repository as SCRepo
import strategy_candidate_service as SCV
import strategy_generator as SG
import strategy_registry as SR

DAY = "2026-10-05"
UNIVERSE = {"scope_kind": "a_share_all"}
REGIME = "momentum"

_RULE = {
    "op": "strategy",
    "rule": {"op": "and", "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": {
             "op": "parameter", "parameter_id": "ma_period", "type": "integer",
             "value": 20, "min": 5, "max": 60, "max_step": 2, "locked": False,
             "risk_direction": "lower_is_riskier", "min_evidence": 0}}},
        {"op": "lt", "left": {"op": "indicator", "name": "rsi", "window": 14},
         "right": {"op": "const", "value": 70}}]},
    "parameters": [],
}

#: ma_period 的合法域是相对 parent 值 20、max_step=2 的 [18, 22]。
_LEGAL_VALUES = [18, 19, 20, 21, 22]


class _Base(unittest.TestCase):
    """每个测试一个临时 paper DB；连接显式、事务显式。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "paper.sqlite3")
        with self._open() as conn:
            PSM.ensure_strategy_candidates(conn)
            PSM.ensure_experiment_search(conn)
            SR.create_user_definition(conn, "r36_parent", "R36 Parent", dsl_ast=_RULE,
                                      metadata={"constraints": {"max_positions": 5}},
                                      actor="test")
        self.parent = self._lifecycle()

    def _lifecycle(self):
        with self._open() as conn:
            spec = SR.get("r36_parent", conn=conn)
            return {"version": spec.current_version, "checksum": spec.current_checksum}

    @contextlib.contextmanager
    def _open(self, immediate: bool = False):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
            with contextlib.suppress(sqlite3.Error):
                conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def connection(self, immediate: bool = False) -> sqlite3.Connection:
        """A fresh connection for concurrency tests; caller closes it."""
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        return conn

    # ── fixtures ──

    def batch(self, *, values=None, campaign="a") -> dict:
        """Generate one candidate batch for this parent."""
        from strategy_candidate_search_space import MAX_CANDIDATES_PER_GENERATION_REQUEST
        with self._open() as conn:
            return SCV.generate_and_record_candidates(
                conn, strategy_id="r36_parent", strategy_version=self.parent["version"],
                strategy_checksum=self.parent["checksum"], asof=DAY,
                generator_type=SG.PARAMETER_VARIANT_GENERATOR,
                generator_version=SG.PARAMETER_VARIANT_VERSION,
                parameter_variants={"ma_period": list(values or _LEGAL_VALUES[:3])},
                universe_spec=UNIVERSE, intended_market_regime=REGIME,
                evidence_count=1, hypothesis_id=f"h_{campaign}",
                max_candidates=MAX_CANDIDATES_PER_GENERATION_REQUEST)

    def create(self, batch, **overrides):
        call = {"generation_batch_id": batch["generation_batch_id"],
                "budget": ESC.SearchBudget(max_candidates=8)}
        call.update(overrides)
        with self._open(immediate=True) as conn:
            return ESS.create_search_run(conn, **call)

    def counts(self):
        with self._open() as conn:
            return {
                "runs": conn.execute(
                    "SELECT COUNT(*) FROM experiment_search_runs").fetchone()[0],
                "jobs": conn.execute(
                    "SELECT COUNT(*) FROM experiment_search_jobs").fetchone()[0],
                "events": conn.execute(
                    "SELECT COUNT(*) FROM experiment_search_job_events").fetchone()[0],
            }

    #: 状态机 / 运营投影相关测试共用的 fixture：N 个候选、显式 attempt 预算。
    def _run_with_jobs(self, count=3, attempts=2):
        batch = self.batch(values=_LEGAL_VALUES[:count])
        return batch, self.create(batch, budget=ESC.SearchBudget(
            max_candidates=count, max_attempts_per_job=attempts))

    @staticmethod
    def _code_without_comments(path: str) -> str:
        """源码去掉 comment / docstring —— 契约检查针对**代码**，不针对说明文字。

        "不要用 ORDER BY created_at DESC" 这类正确做法本身就是文档内容，因此对它的
        检查必须先剥掉注释与 docstring，否则文档会被当成违规。

        注意：若某个 class 的 body **只有** docstring，剥掉后必须补一个 ``pass``，
        否则 ``ast.unparse`` 会产出语法非法的空 class body。
        """
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                    ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value,
                                                                    ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body.pop(0)
                if not body:
                    node.body = [ast.Pass()]
        return ast.unparse(tree)


class ExactGenerationBatchTests(_Base):
    """S1 — exact generation batch only。"""

    def test_s1_known_batch_is_accepted_and_unknown_or_malformed_is_rejected(self):
        batch = self.batch()
        created = self.create(batch)
        self.assertEqual(batch["generation_batch_id"], created["generation_batch_id"])

        before = self.counts()
        for bad in ("0" * 64, "not-a-batch-id", "", "   ", "Z" * 64):
            with self.subTest(bad=bad):
                with self.assertRaises(ESS.ExperimentSearchError) as ctx:
                    self.create(batch, generation_batch_id=bad)
                self.assertEqual(ESS.REASON_BATCH_NOT_FOUND, ctx.exception.reason)
        self.assertEqual(before, self.counts(), "被拒绝的请求不得留下任何控制面行")

    def test_s1b_source_has_no_latest_or_recent_batch_lookup(self):
        """源码/AST 级（剥掉注释）：search controller 不得存在"最近一批"的读取。"""
        for module in (ESS, ESR):
            tree = ast.parse(self._code_without_comments(module.__file__))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
            for forbidden in ("latest_batch", "recent_batches", "recent_batch",
                              "current_batch", "list_recent"):
                self.assertNotIn(forbidden, names, f"{module.__name__}: {forbidden}")
            code = self._code_without_comments(module.__file__)
            self.assertNotIn("ORDER BY created_at DESC", code)

    def test_s1c_batch_payload_identity_must_match_the_lookup_key(self):
        """S1c — 行内 payload 自述的 batch 身份必须等于查找键。

        ``get_generation_batch`` 只按 id 取行并返回 ``batch_json``，它**不**校验 payload
        自述身份。若某行损坏、或错误地存了另一个 batch 的 JSON，请求 A 会静默拿到 B 的
        候选集合，并把 B 的事实当成 A 记录下来。因此 service 必须双向核对并 fail closed。
        """
        batch_a = self.batch(values=_LEGAL_VALUES[:2], campaign="a")
        batch_b = self.batch(values=[22], campaign="b")
        with self._open() as conn:
            row = conn.execute(
                "SELECT batch_json FROM strategy_candidate_generation_batches"
                " WHERE batch_id=?", (batch_a["generation_batch_id"],)).fetchone()
        payload = json.loads(row[0])
        # 请求 A，但行里自述的是 B。
        payload["batch_id"] = batch_b["generation_batch_id"]
        with self._open(immediate=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS strategy_candidate_generation_batches_no_update")
            conn.execute(
                "UPDATE strategy_candidate_generation_batches SET batch_json=?"
                " WHERE batch_id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")),
                 batch_a["generation_batch_id"]))
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch_a)
        self.assertEqual(ESS.REASON_BATCH_NOT_FOUND, ctx.exception.reason)
        self.assertIn("identity mismatch", ctx.exception.detail)
        self.assertEqual(before, self.counts(), "身份不符不得创建任何控制面行")

    def test_s1d_batch_payload_without_a_canonical_input_fingerprint_is_rejected(self):
        """payload 缺 canonical input fingerprint ⇒ fail closed，不带着空身份建 run。"""
        batch = self.batch()
        with self._open() as conn:
            row = conn.execute(
                "SELECT batch_json FROM strategy_candidate_generation_batches"
                " WHERE batch_id=?", (batch["generation_batch_id"],)).fetchone()
        payload = json.loads(row[0])
        payload["generation_input_fingerprint"] = ""
        with self._open(immediate=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS strategy_candidate_generation_batches_no_update")
            conn.execute(
                "UPDATE strategy_candidate_generation_batches SET batch_json=?"
                " WHERE batch_id=?",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")),
                 batch["generation_batch_id"]))
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch)
        self.assertEqual(ESS.REASON_BATCH_NOT_FOUND, ctx.exception.reason)
        self.assertEqual(before, self.counts())


class CandidatePoolTests(_Base):
    """S2/S3/S4 — pool 属于 batch、自证、schema allowlist。"""

    def test_s2_a_candidate_from_another_batch_is_rejected(self):
        batch_a = self.batch(values=_LEGAL_VALUES[:2], campaign="a")
        batch_b = self.batch(values=[22], campaign="b")
        with self._open() as conn:
            ids_b = [p["candidate_id"]
                     for p in SCRepo.list_batch_proposals(conn, batch_b["generation_batch_id"])]
        self.assertTrue(ids_b)
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch_a, candidate_ids=[ids_b[0]])
        self.assertEqual(ESS.REASON_CANDIDATE_NOT_IN_BATCH, ctx.exception.reason)
        self.assertEqual(before, self.counts())

    def test_s2b_mixed_subset_is_rejected_not_silently_filtered(self):
        batch_a = self.batch(values=_LEGAL_VALUES[:2], campaign="a")
        batch_b = self.batch(values=[22], campaign="b")
        with self._open() as conn:
            ids_a = [p["candidate_id"]
                     for p in SCRepo.list_batch_proposals(conn, batch_a["generation_batch_id"])]
            ids_b = [p["candidate_id"]
                     for p in SCRepo.list_batch_proposals(conn, batch_b["generation_batch_id"])]
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch_a, candidate_ids=[ids_a[0], ids_b[0]])
        self.assertEqual(ESS.REASON_CANDIDATE_NOT_IN_BATCH, ctx.exception.reason)

    def test_s3_tampered_candidate_row_fails_closed(self):
        batch = self.batch()
        with self._open() as conn:
            candidate_id = SCRepo.list_batch_proposals(
                conn, batch["generation_batch_id"])[0]["candidate_id"]
        # 直接篡改 candidate_json：指纹将不再自洽。
        with self._open(immediate=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS strategy_candidates_no_update")
            conn.execute("UPDATE strategy_candidates SET candidate_json=?"
                         " WHERE candidate_id=?",
                         ('{"tampered":true}', candidate_id))
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch)
        self.assertEqual(ESS.REASON_CANDIDATE_UNVERIFIABLE, ctx.exception.reason)
        self.assertEqual(before, self.counts(), "损坏 candidate 不得进入队列")

    def test_s3b_proposal_json_alone_cannot_vouch_for_a_candidate(self):
        """证明 pool 不是照抄 proposal 行：候选内容被改写后必须被 ledger 拒绝。

        表上有 ``CHECK(candidate_id = candidate_fingerprint)``，因此这里篡改
        ``candidate_json``（不动两个身份列），让内容与指纹不再自洽。
        """
        batch = self.batch()
        with self._open() as conn:
            candidate_id = SCRepo.list_batch_proposals(
                conn, batch["generation_batch_id"])[0]["candidate_id"]
            row = conn.execute("SELECT candidate_json FROM strategy_candidates"
                               " WHERE candidate_id=?", (candidate_id,)).fetchone()
        payload = json.loads(row[0])
        payload["asof"] = "1999-01-01"
        with self._open(immediate=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS strategy_candidates_no_update")
            conn.execute("UPDATE strategy_candidates SET candidate_json=?"
                         " WHERE candidate_id=?",
                         (json.dumps(payload, sort_keys=True, separators=(",", ":")),
                          candidate_id))
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch)
        self.assertEqual(ESS.REASON_CANDIDATE_UNVERIFIABLE, ctx.exception.reason)
        self.assertEqual(before, self.counts())

    def test_s3c_a_candidate_row_missing_from_the_ledger_is_not_skipped(self):
        """proposal 里有这个 id，但 canonical ledger 里没有对应行 ⇒ 必须拒绝。

        这是"只相信 proposal_json"的真实后果：如果列表里有幽灵 candidate，而校验环节
        对 ``get_candidate() is None`` 采取 ``continue``，search run 就会带着一个
        **账本里不存在的 candidate** 建起来 —— 队列会为一个不存在的实验主体排工作。

        直接 stub ``list_batch_proposals`` 造出幽灵 id，从而不依赖外键绕行。
        """
        batch = self.batch()
        with self._open() as conn:
            real = [p["candidate_id"]
                    for p in SCRepo.list_batch_proposals(
                        conn, batch["generation_batch_id"])]
        self.assertTrue(real)
        phantom = "f" * 64
        original = SCRepo.list_batch_proposals
        SCRepo.list_batch_proposals = lambda conn, batch_id: (
            *({"candidate_id": cid} for cid in real),
            {"candidate_id": phantom},
        )
        self.addCleanup(setattr, SCRepo, "list_batch_proposals", original)
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch)
        self.assertEqual(ESS.REASON_CANDIDATE_UNVERIFIABLE, ctx.exception.reason)
        self.assertEqual(before, self.counts(), "幽灵 candidate 不得进入队列")

    def test_s4_unknown_candidate_schema_is_rejected(self):
        """S4 — 未知 schema fail closed；v1 / v2 继续自证。"""
        # 契约层：显式 allowlist。
        for bad in ("strategy-candidate-v999", "future-v3", "", "Strategy-Candidate-V2"):
            with self.subTest(bad=bad):
                with self.assertRaises(SC.CandidateValidationError) as ctx:
                    SC.candidate_from_projection({"candidate_schema_version": bad})
                self.assertIn("unsupported_candidate_schema_version", str(ctx.exception))
        # 缺字段与 v2 拼写同样拒绝。
        with self.assertRaises(SC.CandidateValidationError):
            SC.candidate_from_projection({})
        self.assertEqual(frozenset({"strategy-candidate-v1", "strategy-candidate-v2"}),
                         SC.CANDIDATE_SCHEMA_VERSIONS)

        # controller 层：把一行改成未知版本后，search creation 必须拒绝。
        batch = self.batch()
        with self._open() as conn:
            candidate_id = SCRepo.list_batch_proposals(
                conn, batch["generation_batch_id"])[0]["candidate_id"]
            row = conn.execute("SELECT candidate_json FROM strategy_candidates"
                               " WHERE candidate_id=?", (candidate_id,)).fetchone()
        payload = json.loads(row[0])
        payload["candidate_schema_version"] = "strategy-candidate-v999"
        with self._open(immediate=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS strategy_candidates_no_update")
            conn.execute("UPDATE strategy_candidates SET candidate_json=?"
                         " WHERE candidate_id=?",
                         (json.dumps(payload, sort_keys=True, separators=(",", ":")),
                          candidate_id))
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch)
        self.assertEqual(ESS.REASON_CANDIDATE_UNVERIFIABLE, ctx.exception.reason)

    def test_s4b_v1_and_v2_rows_still_self_verify(self):
        """升级后 v1 历史行与 v2 新行都必须继续自证。"""
        batch = self.batch()
        with self._open() as conn:
            for proposal in SCRepo.list_batch_proposals(conn, batch["generation_batch_id"]):
                candidate = SCRepo.get_candidate(conn, proposal["candidate_id"])
                self.assertIsNotNone(candidate)
                self.assertTrue(SC.verify_candidate_fingerprint(candidate))
                self.assertEqual(SC.CANDIDATE_SCHEMA_VERSION,
                                 candidate.candidate_schema_version)
        # v1 行：用 v1 材料重建后仍需自证（走旧 schema 路径）。
        with self._open() as conn:
            v1_row = conn.execute(
                "SELECT candidate_json FROM strategy_candidates LIMIT 1").fetchone()
        v2_projection = json.loads(v1_row[0])
        self.assertIn(v2_projection["candidate_schema_version"], SC.CANDIDATE_SCHEMA_VERSIONS)


class CandidateSubsetTests(_Base):
    """S5/S6/S7/S8 — order-independent、去重、预算不截断。"""

    def test_s5_subset_order_does_not_change_identity_or_jobs(self):
        """输入顺序不承载语义：fingerprint 相同，且同一 run 内 job 集合与顺序相同。"""
        batch = self.batch()
        with self._open() as conn:
            ids = [p["candidate_id"]
                   for p in SCRepo.list_batch_proposals(conn, batch["generation_batch_id"])]
        forward = self.create(batch, candidate_ids=list(ids))
        backward = self.create(batch, candidate_ids=list(reversed(ids)))
        # content identity 与顺序无关。
        self.assertEqual(forward["search_input_fingerprint"],
                         backward["search_input_fingerprint"])
        # 两个 run 的 job **id** 不同（run 是事件身份，job id 含 search_run_id）……
        self.assertEqual(set(), set(forward["job_ids"]) & set(backward["job_ids"]))
        # ……但**候选集合与顺序**必须相同，且 claim 顺序也相同。
        with self._open() as conn:
            jobs_forward = ESS.list_search_jobs(conn, forward["search_run_id"])
            jobs_backward = ESS.list_search_jobs(conn, backward["search_run_id"])
        self.assertEqual([job["candidate_id"] for job in jobs_forward],
                         [job["candidate_id"] for job in jobs_backward])
        self.assertEqual(list(ids), [job["candidate_id"] for job in jobs_forward])

    def test_s6_duplicate_candidate_ids_are_rejected_not_deduped(self):
        batch = self.batch()
        with self._open() as conn:
            ids = [p["candidate_id"]
                   for p in SCRepo.list_batch_proposals(conn, batch["generation_batch_id"])]
        before = self.counts()
        # 契约层直接拒绝（不静默去重）。
        with self.assertRaises(ESC.SearchContractError) as ctx:
            ESC.canonical_candidate_ids([ids[0], ids[0], ids[1]])
        self.assertEqual("duplicate_candidate_id", ctx.exception.reason)
        # service 层同样拒绝，并把它映射成控制器稳定 reason。
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch, candidate_ids=[ids[0], ids[0], ids[1]])
        self.assertEqual("duplicate_candidate_id", ctx.exception.reason)
        self.assertEqual(before, self.counts(), "重复输入不得创建半批次")

    def test_s7_budget_overflow_without_explicit_subset_is_rejected(self):
        """S7 — 10 个候选、max_candidates=5、无显式子集 ⇒ 拒绝，绝不取前 5。"""
        batch = self.batch(values=_LEGAL_VALUES)  # 5 个候选
        self.assertEqual(5, batch["candidate_count"])
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch, budget=ESC.SearchBudget(max_candidates=4))
        self.assertEqual("candidate_count_exceeds_budget", ctx.exception.reason)
        self.assertEqual(before, self.counts(), "超限不得留下任何行（更不得截断）")

    def test_s8_explicit_subset_within_budget_is_accepted(self):
        batch = self.batch(values=_LEGAL_VALUES)
        with self._open() as conn:
            ids = sorted(p["candidate_id"]
                         for p in SCRepo.list_batch_proposals(
                             conn, batch["generation_batch_id"]))
        created = self.create(batch, candidate_ids=ids[:3],
                              budget=ESC.SearchBudget(max_candidates=3))
        self.assertEqual(3, created["candidate_count"])
        self.assertEqual(3, created["jobs_created"])
        self.assertEqual(3, created["queued_events_created"])
        with self._open() as conn:
            self.assertEqual(3, conn.execute(
                "SELECT COUNT(*) FROM experiment_search_jobs").fetchone()[0])
            self.assertEqual(3, conn.execute(
                "SELECT COUNT(*) FROM experiment_search_job_events"
                " WHERE event_kind='queued'").fetchone()[0])

    def test_s8b_whole_batch_within_budget_is_accepted(self):
        batch = self.batch(values=_LEGAL_VALUES)
        created = self.create(batch, budget=ESC.SearchBudget(max_candidates=5))
        self.assertEqual(5, created["candidate_count"])


class RunIdentityTests(_Base):
    """S9/S10/S11/S12 — 内容 vs 事件身份、原子创建、deterministic job id、初始态。"""

    def test_s9_same_spec_twice_same_input_fingerprint_different_run_id(self):
        batch = self.batch()
        first = self.create(batch)
        second = self.create(batch)
        self.assertEqual(first["search_input_fingerprint"],
                         second["search_input_fingerprint"])
        self.assertNotEqual(first["search_run_id"], second["search_run_id"])
        # 两个 run 各自独立 queue。
        with self._open() as conn:
            for run in (first, second):
                self.assertEqual(first["candidate_count"], len(
                    ESS.list_search_jobs(conn, run["search_run_id"])))

    def test_s9b_search_run_id_is_opaque_not_derived_from_content(self):
        """run id 不得等于任何 content fingerprint，也不得来自时钟/计数器。"""
        batch = self.batch()
        created = self.create(batch)
        self.assertNotEqual(created["search_run_id"], created["search_input_fingerprint"])
        self.assertTrue(ESC.is_search_identity(created["search_run_id"]))
        # 连续两次调用的 id 不同 ⇒ 不是进程内计数器 / 时间戳。
        self.assertNotEqual(ESC.search_run_id(), ESC.search_run_id())

    def test_s10_creation_is_atomic(self):
        """S10 — 注入第 N 个 job 插入失败：三张表 delta 全为 0。"""
        batch = self.batch(values=_LEGAL_VALUES)
        before = self.counts()
        original = ESR.record_job
        calls = {"n": 0}

        def _flaky(conn, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise sqlite3.OperationalError("injected job insert failure")
            return original(conn, **kwargs)

        ESR.record_job = _flaky
        self.addCleanup(setattr, ESR, "record_job", original)
        with self.assertRaises(sqlite3.OperationalError):
            self.create(batch, budget=ESC.SearchBudget(max_candidates=5))
        self.assertEqual(before, self.counts(), "整批必须回滚")

    def test_s11_job_identity_is_deterministic(self):
        batch = self.batch()
        with self._open() as conn:
            ids = sorted(p["candidate_id"]
                         for p in SCRepo.list_batch_proposals(
                             conn, batch["generation_batch_id"]))
        run_a = self.create(batch)
        run_b = self.create(batch)
        # 同一 run + candidate + stage ⇒ 同一 job id（声明身份，不随重启变化）。
        jobs_a = run_a["job_ids"]
        with self._open() as conn:
            spec_a = ESS.get_search_run(conn, run_a["search_run_id"])
        expected = [ESC.SearchJobSpec(search_run_id=run_a["search_run_id"],
                                      candidate_id=cid).job_id
                    for cid in sorted(j["candidate_id"] for j in spec_a["jobs"])]
        self.assertEqual(sorted(expected), sorted(jobs_a))
        # 不同 run ⇒ 不同 job id。
        self.assertEqual(set(), set(jobs_a) & set(run_b["job_ids"]))
        # 空集说明一切正常（无交集）。
        self.assertEqual(len(ids), len(jobs_a))

    def test_s12_initial_state_is_queued_with_zero_attempts(self):
        batch = self.batch()
        created = self.create(batch)
        with self._open() as conn:
            states = ESS.list_search_jobs(conn, created["search_run_id"])
            self.assertEqual(created["candidate_count"], len(states))
            for state in states:
                self.assertEqual("queued", state["current_state"])
                self.assertEqual(0, state["attempt_count"])
            # 每条 job 恰好一条 queued 事件。
            rows = conn.execute(
                "SELECT job_id, COUNT(*) c FROM experiment_search_job_events"
                " WHERE search_run_id=? GROUP BY job_id",
                (created["search_run_id"],)).fetchall()
            self.assertEqual(created["candidate_count"], len(rows))
            for row in rows:
                self.assertEqual(1, row["c"])


class StateMachineTests(_Base):
    """S13/S14/S15/S16 — claim、retry 预算、终态。"""

    def test_s13_queued_to_claimed_then_reclaim_is_rejected(self):
        _, run = self._run_with_jobs()
        with self._open(immediate=True) as conn:
            claimed = ESS.claim_next_job(conn, run["search_run_id"], actor="w1")
        self.assertIsNotNone(claimed["job_id"])
        self.assertEqual(1, claimed["attempt_number"])
        # 同一个 job 再次 claim：拒绝（claimed → claimed 不在转换表里）。
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            with self._open(immediate=True) as conn:
                ESS.record_job_event(conn, job_id=claimed["job_id"], event_kind="claimed")
        self.assertEqual(ESS.REASON_ILLEGAL_TRANSITION, ctx.exception.reason)

    def test_s13b_illegal_transitions_are_rejected(self):
        """claimed 状态下：``failed`` / ``completed`` 合法，其余一律拒绝。"""
        _, run = self._run_with_jobs(count=2)
        with self._open(immediate=True) as conn:
            claimed = ESS.claim_next_job(conn, run["search_run_id"])
        for illegal in ("claimed", "queued"):
            with self.subTest(target=illegal):
                with self.assertRaises(ESS.ExperimentSearchError) as ctx:
                    with self._open(immediate=True) as conn:
                        ESS.record_job_event(conn, job_id=claimed["job_id"],
                                             event_kind=illegal)
                self.assertEqual(ESS.REASON_ILLEGAL_TRANSITION, ctx.exception.reason)
        # queued 状态下不能直接完成 / 失败（必须先 claim）。
        with self._open(immediate=True) as conn:
            queued_job = [job for job in ESS.list_search_jobs(conn, run["search_run_id"])
                          if job["current_state"] == "queued"][0]["job_id"]
        for illegal in ("completed", "failed"):
            with self.subTest(state="queued", target=illegal):
                with self.assertRaises(ESS.ExperimentSearchError) as ctx:
                    with self._open(immediate=True) as conn:
                        ESS.record_job_event(conn, job_id=queued_job, event_kind=illegal)
                self.assertEqual(ESS.REASON_ILLEGAL_TRANSITION, ctx.exception.reason)

    def test_s14_attempt_budget_is_enforced(self):
        """S14 — max_attempts_per_job=2：第三次 claim 必须被拒绝。"""
        batch, run = self._run_with_jobs(count=1, attempts=2)
        for expected in (1, 2):
            with self._open(immediate=True) as conn:
                claimed = ESS.claim_next_job(conn, run["search_run_id"], actor="w1")
                self.assertEqual(expected, claimed["attempt_number"])
                ESS.record_job_event(conn, job_id=claimed["job_id"], event_kind="failed",
                                     reason="executor_unavailable")
        with self._open(immediate=True) as conn:
            with self.assertRaises(ESS.ExperimentSearchError) as ctx:
                ESS.record_job_event(conn, job_id=claimed["job_id"], event_kind="claimed")
        self.assertEqual(ESS.REASON_ATTEMPT_BUDGET_EXHAUSTED, ctx.exception.reason)
        # claim_next_job 也不再给出这条 job。
        with self._open(immediate=True) as conn:
            nothing = ESS.claim_next_job(conn, run["search_run_id"])
        self.assertIsNone(nothing["job_id"])

    def test_s15_completed_is_terminal(self):
        _, run = self._run_with_jobs(count=1)
        with self._open(immediate=True) as conn:
            claimed = ESS.claim_next_job(conn, run["search_run_id"])
            ESS.record_job_event(conn, job_id=claimed["job_id"], event_kind="completed",
                                 evidence_owner="experiment_validation_run",
                                 evidence_id="future-r29-run-key")
        for target in ("failed", "claimed", "cancelled"):
            with self.subTest(target=target):
                with self.assertRaises(ESS.ExperimentSearchError) as ctx:
                    with self._open(immediate=True) as conn:
                        ESS.record_job_event(conn, job_id=claimed["job_id"],
                                             event_kind=target)
                self.assertEqual(ESS.REASON_ILLEGAL_TRANSITION, ctx.exception.reason)

    def test_s16_cancelled_is_terminal(self):
        _, run = self._run_with_jobs(count=1)
        with self._open(immediate=True) as conn:
            states = ESS.list_search_jobs(conn, run["search_run_id"])
            job_id = states[0]["job_id"]
            ESS.record_job_event(conn, job_id=job_id, event_kind="cancelled",
                                 reason="operator_cancelled")
        with self.assertRaises(ESS.ExperimentSearchError):
            with self._open(immediate=True) as conn:
                ESS.record_job_event(conn, job_id=job_id, event_kind="claimed")
        with self._open(immediate=True) as conn:
            nothing = ESS.claim_next_job(conn, run["search_run_id"])
        self.assertIsNone(nothing["job_id"])


class EventOrderingTests(_Base):
    """S17 — current state 只由 event_seq 决定。"""

    def test_s17_current_state_uses_event_seq_not_created_at_or_event_id(self):
        _, run = self._run_with_jobs(count=1)
        stamp = "2026-10-05T00:00:00+00:00"
        with self._open(immediate=True) as conn:
            states = ESS.list_search_jobs(conn, run["search_run_id"])
            job_id = states[0]["job_id"]
            # 两条事件 created_at **完全相同**；顺序权威只能是 event_seq。
            ESS.record_job_event(conn, job_id=job_id, event_kind="claimed",
                                 created_at=stamp)
            ESS.record_job_event(conn, job_id=job_id, event_kind="failed",
                                 reason="executor_unavailable", created_at=stamp)
        with self._open() as conn:
            events = ESR.list_job_events(conn, job_id)
        self.assertEqual(["queued", "claimed", "failed"],
                         [event["event_kind"] for event in events])
        self.assertEqual(sorted(event["event_seq"] for event in events),
                         [event["event_seq"] for event in events])
        # claimed / failed 两条事件的 created_at **完全相同**（queued 是初始事件，
        # 用真实时间戳创建），因此先后只能由 event_seq 决定。
        transitions = [event for event in events if event["event_kind"] in ("claimed", "failed")]
        self.assertEqual(2, len(transitions))
        self.assertEqual(1, len({event["created_at"] for event in transitions}),
                         "两条转换事件必须共享同一个 created_at，才能真正测到 ordering")
        with self._open() as conn:
            state = ESS.list_search_jobs(conn, run["search_run_id"])[0]
        self.assertEqual("failed", state["current_state"])
        self.assertEqual(1, state["attempt_count"])

    def test_s17b_controller_never_orders_by_created_at(self):
        """源码级（剥掉注释）：三个控制面模块不得用 created_at / event_id 排序做状态权威。"""
        for module in (ESS, ESR):
            code = self._code_without_comments(module.__file__)
            self.assertNotIn("ORDER BY created_at", code)
            self.assertNotIn("ORDER BY event_id", code)
        self.assertIn("ORDER BY event_seq ASC",
                      self._code_without_comments(ESR.__file__))


class QueuePolicyTests(_Base):
    """S18/S19 — deterministic 非-ranking 顺序、并发安全。"""

    def test_s18_claim_order_is_canonical_and_insertion_order_independent(self):
        batch = self.batch(values=_LEGAL_VALUES[:3])
        with self._open() as conn:
            ids = sorted(p["candidate_id"]
                         for p in SCRepo.list_batch_proposals(
                             conn, batch["generation_batch_id"]))
        # 反转输入顺序不影响 canonical 顺序与 claim 顺序。
        forward = self.create(batch, candidate_ids=list(ids))
        backward = self.create(batch, candidate_ids=list(reversed(ids)))
        claimed_candidates = []
        for run in (forward, backward):
            order = []
            while True:
                with self._open(immediate=True) as conn:
                    claimed = ESS.claim_next_job(conn, run["search_run_id"], actor="w")
                    if not claimed["job_id"]:
                        break
                    job_id = claimed["job_id"]
                with self._open() as conn:
                    candidate_id = next(
                        job["candidate_id"] for job in ESS.list_search_jobs(
                            conn, run["search_run_id"]) if job["job_id"] == job_id)
                order.append(candidate_id)
            claimed_candidates.append(order)
        # 两个 run 的 claim 顺序都等于 canonical candidate order（不是 ranking）。
        self.assertEqual(ids, claimed_candidates[0])
        self.assertEqual(claimed_candidates[0], claimed_candidates[1])

    def test_s18b_queue_policy_version_is_part_of_the_identity(self):
        batch = self.batch()
        first = self.create(batch)
        with self._open() as conn:
            run = ESR.get_search_run(conn, first["search_run_id"])
        self.assertEqual(ESC.QUEUE_POLICY_VERSION,
                         run["search_spec"]["queue_policy_version"])
        self.assertIn("candidate-id-ascending", ESC.QUEUE_POLICY_VERSION)
        # 换 policy 版本 ⇒ 不同 input fingerprint（否则改 claim 顺序会悄悄改变语义）。
        other = ESC.ExperimentSearchSpec(
            generation_batch_id=run["generation_batch_id"],
            generation_input_fingerprint=run["generation_input_fingerprint"],
            candidate_ids=tuple(run["search_spec"]["candidate_ids"]),
            budget=ESC.SearchBudget(**{k: v for k, v in run["budget"].items()}),
            queue_policy_version="other-policy-v1")
        self.assertNotEqual(run["search_input_fingerprint"],
                            other.search_input_fingerprint)

    def test_s19_two_workers_never_double_claim(self):
        """S19 — 两条真实 SQLite 连接各自 claim：同一 queued job 绝不被 claim 两次。

        真实 worker 模式是"各自 ``BEGIN IMMEDIATE`` → claim → commit"，而不是同时持有
        两个写事务（后者在 SQLite 上是锁竞争，不是本层要表达的不变量）。因此这里用两条
        独立连接**顺序** claim，并断言两个 job 不同、且每条 job 只有一条 claimed 事件。
        """
        batch = self.batch(values=_LEGAL_VALUES[:3])
        run = self.create(batch, budget=ESC.SearchBudget(max_candidates=3))
        claims = []
        for actor in ("w1", "w2"):
            conn = self.connection(immediate=True)
            try:
                claims.append(ESS.claim_next_job(conn, run["search_run_id"], actor=actor))
                conn.execute("COMMIT")
            finally:
                conn.close()
        self.assertIsNotNone(claims[0]["job_id"])
        self.assertIsNotNone(claims[1]["job_id"])
        self.assertNotEqual(claims[0]["job_id"], claims[1]["job_id"],
                            "同一 job 不得被两个 worker 同时 claim")
        with self._open() as conn:
            rows = conn.execute(
                "SELECT job_id, COUNT(*) c FROM experiment_search_job_events"
                " WHERE search_run_id=? AND event_kind='claimed' GROUP BY job_id",
                (run["search_run_id"],)).fetchall()
        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertEqual(1, row["c"], "每条 job 恰好一条 claimed 事件")

    def test_s19c_a_second_claim_of_the_same_job_is_refused_by_the_transition_table(self):
        """同一 job 的第二次 claim 必须被拒 —— 这是 double-claim 的架构性防线。"""
        batch = self.batch(values=_LEGAL_VALUES[:1])
        run = self.create(batch, budget=ESC.SearchBudget(max_candidates=1,
                                                         max_attempts_per_job=3))
        conn = self.connection(immediate=True)
        try:
            first = ESS.claim_next_job(conn, run["search_run_id"], actor="w1")
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.assertIsNotNone(first["job_id"])
        # 另一个 worker 再来：队列里没有可 claim 的 job（该 job 已是 claimed）。
        conn = self.connection(immediate=True)
        try:
            second = ESS.claim_next_job(conn, run["search_run_id"], actor="w2")
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.assertIsNone(second["job_id"])
        with self._open() as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM experiment_search_job_events"
                " WHERE job_id=? AND event_kind='claimed'",
                (first["job_id"],)).fetchone()[0])

    def test_s19b_exhausted_queue_returns_no_job(self):
        batch = self.batch(values=_LEGAL_VALUES[:1])
        run = self.create(batch, budget=ESC.SearchBudget(max_candidates=1,
                                                         max_attempts_per_job=1))
        with self._open(immediate=True) as conn:
            self.assertIsNotNone(ESS.claim_next_job(conn, run["search_run_id"])["job_id"])
        with self._open(immediate=True) as conn:
            nothing = ESS.claim_next_job(conn, run["search_run_id"])
        self.assertIsNone(nothing["job_id"])
        self.assertFalse(nothing.get("claimed", False))


class TerminalSemanticsTests(_Base):
    """S20/S21/S22 — 无指标、无 evaluation 依赖、无 AI 依赖 + queue 状态无业务含义。"""

    def test_s20_controller_stores_no_evaluation_metrics(self):
        for module in (ESC, ESR, ESS):
            source = open(module.__file__, encoding="utf-8").read()
            tree = ast.parse(source)
            identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            identifiers |= {node.attr for node in ast.walk(tree)
                            if isinstance(node, ast.Attribute)}
            for forbidden in ("sharpe", "drawdown", "win_rate", "total_return",
                              "promotion", "winner", "expected_return"):
                self.assertNotIn(forbidden, identifiers,
                                 f"{module.__name__} 不得引用 {forbidden}")
        # 三张表的列里没有指标字段。
        with self._open() as conn:
            for table in ("experiment_search_runs", "experiment_search_jobs",
                          "experiment_search_job_events"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(set(), columns & ESR.FORBIDDEN_CONTROLLER_FIELDS)
                # 也没有可变状态列。
                self.assertEqual(set(),
                                 columns & {"status", "attempts", "claimed_at",
                                            "finished_at", "current_state"})

    def test_s20b_metric_shaped_payload_is_rejected(self):
        batch = self.batch()
        before = self.counts()
        with self.assertRaises(ESS.ExperimentSearchError) as ctx:
            self.create(batch, budget={"sharpe": 1.0})
        self.assertEqual("canonical_search_budget_required", ctx.exception.reason)
        self.assertEqual(before, self.counts())

    def test_s20c_queue_state_has_no_business_meaning(self):
        """current_state == completed 只表示"产生了一份外部 evidence"。"""
        _, run = self._run_with_jobs(count=1)
        with self._open(immediate=True) as conn:
            claimed = ESS.claim_next_job(conn, run["search_run_id"])
            ESS.record_job_event(conn, job_id=claimed["job_id"], event_kind="completed",
                                 evidence_owner="experiment_validation_run",
                                 evidence_id="some-exact-run-key")
        with self._open() as conn:
            state = ESS.list_search_jobs(conn, run["search_run_id"])[0]
        self.assertEqual("completed", state["current_state"])
        # 投影里只有运营字段，没有任何"通过 / 优秀 / 可晋级"结论。
        self.assertEqual({"job_id", "candidate_id", "stage", "current_state",
                          "attempt_count", "last_event_seq", "last_attempt_number"},
                         set(state))

    def test_s21_no_evaluation_or_promotion_dependency(self):
        for module in (ESC, ESR, ESS):
            source = open(module.__file__, encoding="utf-8").read()
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for forbidden in ("experiment_validation_runner", "robustness_runner",
                              "experiment_validation_repository", "robustness_repository",
                              "promotion_science", "strategy_promotion",
                              "strategy_lifecycle", "paper_trading",
                              "risk_application_service", "paper_allocation"):
                self.assertNotIn(forbidden, imported,
                                 f"{module.__name__} 不得依赖 {forbidden}")

    def test_s21b_controller_does_not_execute_runners(self):
        """R36-A 只调度"需要验证什么"，绝不执行 R29/R30。"""
        for module in (ESS, ESR, ESC):
            source = open(module.__file__, encoding="utf-8").read()
            for forbidden in ("run_validation(", "run_robustness(", "run_pit("):
                self.assertNotIn(forbidden, source)

    def test_s22_no_ai_provider_dependency(self):
        for module in (ESC, ESR, ESS):
            source = open(module.__file__, encoding="utf-8").read()
            for forbidden in ("strategy_ai_provider", "ai_provider_transport",
                              "ai_research_service", "ai_research_repository",
                              "requests.post", "urllib.request"):
                self.assertNotIn(forbidden, source,
                                 f"{module.__name__} 不得依赖 AI：{forbidden}")

    def test_s22b_no_selection_ranking_or_priority_authority(self):
        """没有 priority / ranking / 排序依据字段（剥掉注释后检查代码）。"""
        for module in (ESC, ESR, ESS):
            code = self._code_without_comments(module.__file__).lower()
            for forbidden in ("priority", "rank_candidates", "sort_by_score",
                              "order_by_sharpe", "order by score", "order by rank"):
                self.assertNotIn(forbidden, code,
                                 f"{module.__name__} 代码里不得出现 {forbidden}")


class SchemaTests(_Base):
    """migration v35 的表形状守卫。"""

    def test_append_only_triggers_reject_update_and_delete(self):
        batch = self.batch()
        run = self.create(batch)
        with self._open(immediate=True) as conn:
            for table, column, value in (
                ("experiment_search_runs", "search_run_id", run["search_run_id"]),
                ("experiment_search_jobs", "job_id", run["job_ids"][0]),
            ):
                with self.subTest(table=table):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(f"UPDATE {table} SET created_at='x'"
                                     f" WHERE {column}=?", (value,))
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(f"DELETE FROM {table} WHERE {column}=?", (value,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE experiment_search_job_events SET reason='x'"
                    " WHERE search_run_id=?", (run["search_run_id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM experiment_search_job_events WHERE search_run_id=?",
                    (run["search_run_id"],))

    def test_foreign_keys_are_enforced(self):
        batch = self.batch()
        self.create(batch)
        with self._open(immediate=True) as conn:
            self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO experiment_search_jobs(job_id,job_fingerprint,"
                    "search_run_id,candidate_id,stage,job_contract_version,job_json,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?)",
                    ("a" * 64, "a" * 64, "b" * 64, "c" * 64, "pit_validation", "v", "{}",
                     "2026-10-05T00:00:00+00:00"))

    def test_migration_is_idempotent_and_needs_no_backfill(self):
        with self._open(immediate=True) as conn:
            first = PSM.ensure_experiment_search(conn)
            second = PSM.ensure_experiment_search(conn)
        # setUp 里已经建过表，因此两次都应是 "ok"（幂等）。
        self.assertEqual({"ok"}, set(first.values()))
        self.assertEqual({"ok"}, set(second.values()))
        # 历史没有 search 概念 ⇒ 没有行就是正确状态，绝不回填。
        with self._open() as conn:
            for table in ("experiment_search_runs", "experiment_search_jobs",
                          "experiment_search_job_events"):
                self.assertEqual(0, conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_migration_reports_created_on_a_fresh_database(self):
        """全新库上必须报告 created，且三张表都建出来。"""
        fresh = os.path.join(self._dir.name, "fresh.sqlite3")
        conn = sqlite3.connect(fresh, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            first = PSM.ensure_experiment_search(conn)
            second = PSM.ensure_experiment_search(conn)
            created = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertEqual({"created"}, set(first.values()))
        self.assertEqual({"ok"}, set(second.values()))
        for table in ("experiment_search_runs", "experiment_search_jobs",
                      "experiment_search_job_events"):
            self.assertIn(table, created)

    def test_normal_bootstrap_creates_the_search_tables(self):
        """正常 bootstrap（``paper_trading.init_db()``）必须建出 search 三张表。

        v35 migration 只是**升级**路径。若正常 bootstrap 不建表，应用打开/新建的库就没有
        ``experiment_search_*``，第一次 search 写入会直接 ``no such table`` —— 而这条路径
        不经过 ``db_migrate``。这里直接调用真实 ``init_db()``，并真的写一次 search。
        """
        import paper_trading as PT
        fresh = os.path.join(self._dir.name, "bootstrap.sqlite3")
        original = PT.DB_PATH
        PT.DB_PATH = fresh
        self.addCleanup(setattr, PT, "DB_PATH", original)
        PT.init_db()

        conn = sqlite3.connect(fresh, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("experiment_search_runs", "experiment_search_jobs",
                          "experiment_search_job_events"):
                self.assertIn(table, tables,
                              f"正常 bootstrap 缺少 {table}（首次写入会 no such table）")
            # 真的写一次：证明不是"表存在但写不进去"。parent 必须在这个新库里建。
            spec = SR.create_user_definition(
                conn, "boot_parent", "Boot Parent", dsl_ast=_RULE,
                metadata={"constraints": {"max_positions": 5}}, actor="test")
            conn.execute("BEGIN IMMEDIATE")
            result = SCV.generate_and_record_candidates(
                conn, strategy_id="boot_parent", strategy_version=spec.current_version,
                strategy_checksum=spec.current_checksum, asof=DAY,
                generator_type=SG.PARAMETER_VARIANT_GENERATOR,
                generator_version=SG.PARAMETER_VARIANT_VERSION,
                parameter_variants={"ma_period": _LEGAL_VALUES[:2]},
                universe_spec=UNIVERSE, intended_market_regime=REGIME,
                evidence_count=1, hypothesis_id="h_boot", max_candidates=32)
            run = ESS.create_search_run(
                conn, generation_batch_id=result["generation_batch_id"],
                budget=ESC.SearchBudget(max_candidates=4))
            conn.execute("COMMIT")
            self.assertEqual(2, run["jobs_created"])
        finally:
            conn.close()

    def test_run_id_collision_with_different_content_is_a_conflict(self):
        batch = self.batch(values=_LEGAL_VALUES[:3])
        created = self.create(batch, budget=ESC.SearchBudget(max_candidates=3))
        # 同一个 run id、**不同**内容（更小的子集）⇒ 冲突，绝不覆盖。
        with self._open() as conn:
            spec = ESC.ExperimentSearchSpec(
                generation_batch_id=created["generation_batch_id"],
                generation_input_fingerprint=created["generation_input_fingerprint"],
                candidate_ids=tuple(sorted(created["search_spec"]["candidate_ids"])[:1]),
                budget=ESC.SearchBudget(max_candidates=1))
        self.assertNotEqual(created["search_input_fingerprint"],
                            spec.search_input_fingerprint)
        with self.assertRaises(ESR.ExperimentSearchRepositoryError) as ctx:
            with self._open(immediate=True) as conn:
                ESR.record_search_run(conn, spec=spec,
                                      run_id=created["search_run_id"])
        self.assertIn("idempotency_conflict", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
