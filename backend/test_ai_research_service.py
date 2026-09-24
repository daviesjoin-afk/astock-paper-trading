# -*- coding: utf-8 -*-
"""R27-B2B —— legacy research runtime 向 typed research path 收敛的回归。

存在理由是这一条不变量：

    **研究运行时只有一个 orchestration boundary，且它不拥有任何新权限。**

分七组：

    RUNTIME-01 ~ 03   迁移后的 production entrypoint 真的走到 canonical ledger，
                      provider 只被调用一次，append 只有一次（无 dual-write）
    RUNTIME-04 ~ 06   provider / contract / persistence 失败都**不留**canonical 行，
                      且绝不回落 legacy provider 或 legacy 表
    RUNTIME-07 ~ 08   legacy 历史行完全不迁移；迁移路径的 legacy writer = 0
    RUNTIME-09 ~ 10   research 不获得 signal / order / risk / promotion 权限；
                      读一条 canonical 行不会被当成重新授权
    RUNTIME-11 ~ 12   provider 网络 owner 仍然唯一；网络调用不在任何事务内
    RUNTIME-13 ~ 14   ``as_of`` 与 ``created_at`` 分离；重复执行 = 两条 run
    RUNTIME-15 ~ 17   读路径在首次运行之前可用、``overview`` 以 canonical 为准、
                      ``purpose`` 过滤精确且有界
    RUNTIME-18 ~ 21   provider 配置权威：DB-only 槽位可用、``enabled=False`` 零网络、
                      配置解析失败不裸逃逸、就绪判据只有一份
    架构 guard        service 的 import 闭集 / 无 SQL / 无网络 / 依赖方向不可反转

**全部离线**：provider 那一次真实 HTTP 调用被 ``ai_provider_transport.call_json`` 的桩
替换，``market_data_service`` 由 ``deepseek_advisor._market_reading`` 这一处 authority
接缝替换。测试因此不联网、不读墙上时钟（``created_at`` 用注入的 clock，``as_of`` 来自
reading 的业务日）。

刻意用**内存库 + 同一个连接**模拟 ``connect_factory``：仓库里不会留下临时 DB 产物。
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_provider_transport as transport  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import ai_research_repository as REP  # noqa: E402
import ai_research_service as SVC  # noqa: E402
import deepseek_advisor as advisor  # noqa: E402
import market_data_contract as MDC  # noqa: E402

SERVICE_MODULE = "ai_research_service.py"
ADVISOR_MODULE = "deepseek_advisor.py"
REPOSITORY_MODULE = "ai_research_repository.py"

#: 固定业务日 / 固定运维时刻 —— 与本机时钟无关，测试因此完全确定。
DAY = "2026-08-27"
OBSERVED_AT = f"{DAY}T10:30:00+08:00"
CREATED_AT = f"{DAY}T10:31:00+08:00"
CODE = "600000"
POLICY = "live_market"
VALIDATION_CROSS_SOURCE = "cross_source_checked"

PURPOSE = "data_quality"
TRIGGER = "unit-test"
QUESTION = "快照是否可用于研究？"

#: 合法的 provider 输出（严格协议：只有五个键）。
PROVIDER_PAYLOAD = {
    "thesis": "全市场快照在核验维度上足以支撑研究使用",
    "confidence": 0.66,
    "evidence_relations": [{"evidence_id": None, "relation": "supports"}],
    "narrative": "行数、代码覆盖与报价时间边界都在可接受范围内。",
    "counter_arguments": ["单一时刻的快照不能证明采集链路长期稳定。"],
}

#: service 允许 import 的模块（等值断言，不是"不含"断言）。
ALLOWED_SERVICE_IMPORTS = {
    "__future__", "datetime", "collections", "dataclasses", "typing", "zoneinfo",
    "ai_research_provider", "ai_research_repository",
}

#: service **不得**出现的 SQL / 网络词汇。
FORBIDDEN_SERVICE_TOKENS = (
    "INSERT", "UPDATE", "DELETE", "SELECT", "ON CONFLICT", "DROP", "ALTER",
    "urlopen", "requests.", "httpx", "socket.", "urllib",
)

#: research 结果**不得**带来的权限词汇（写成代码即越界）。
FORBIDDEN_AUTHORITY_TOKENS = (
    "commit_signal", "paper_signals", "paper_orders", "paper_fills",
    "SignalDecision", "risk_decision", "apply_tuner_proposals", "promotion",
)


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


def _reading():
    """一条**真实** R24 typed 事实（走 snapshot → classify → reading 的正规路径）。"""
    snapshot = MDC.symbol_quote_snapshot(
        {"code": CODE, "price": 10.5, "quote_at": OBSERVED_AT,
         "quote_source": "eastmoney", "quote_validation": VALIDATION_CROSS_SOURCE},
        asof_day=DAY,
    )
    return MDC.classify(snapshot, MDC.policy_named(POLICY), now=OBSERVED_AT, asof_day=DAY)


def _event():
    ref = ARC.evidence_ref_from_market_reading(_reading())
    return ARC.InformationEvent(
        as_of=ref.as_of, source="unit-test", evidence_ref=ref,
        payload={"row_count": 1},
    )


def _provider_config():
    return {
        "slot": "ai2", "api_key": "test-key",
        "base_url": "https://api.deepseek.com", "model": "test-model",
        "timeout_seconds": 30,
    }


def _payload_for(events):
    """把 ``PROVIDER_PAYLOAD`` 里的 evidence_id 占位换成真实输入 id。"""
    payload = dict(PROVIDER_PAYLOAD)
    payload["evidence_relations"] = [
        {"evidence_id": events[0].evidence_id, "relation": "supports"},
    ]
    return payload


class _TrackingFactory:
    """既当 ``connect_factory``，又记录"调用网络时是否持有连接"。

    RUNTIME-12 需要一个**可观测**的证据：如果 provider 是在事务里被调用的，
    ``depth`` 在那一刻必然 > 0。用计数器而不是读代码，是为了让"网络不许进事务"
    这条不变量真的能被改坏之后变红。
    """

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.depth = 0
        self.depth_at_network = []

    def close(self):
        self.conn.close()

    @contextlib.contextmanager
    def __call__(self):
        self.depth += 1
        try:
            yield self.conn
            self.conn.commit()
        finally:
            self.depth -= 1


class _TransportStub:
    """替换 provider 唯一的那次 HTTP 调用，并记录调用次数与调用时的连接深度。"""

    def __init__(self, factory, payload=None, error=None):
        self.factory = factory
        self.payload = payload
        self.error = error
        self.calls = 0
        self.legacy_calls = 0

    def call_json(self, provider_config, system_prompt, user_prompt, max_tokens=1800):
        self.calls += 1
        self.factory.depth_at_network.append(self.factory.depth)
        if self.error is not None:
            raise self.error
        return dict(self.payload or {}), 11, 7, 123

    def legacy_call_json(self, system, user, max_tokens=1800):
        """legacy provider —— 迁移后的路径**绝不**允许走到这里。"""
        self.legacy_calls += 1
        raise AssertionError("legacy provider 被调用了：迁移路径出现了第二个付费 owner")


def _install(stub):
    """把 transport + market authority 两条接缝装好，返回退出栈。"""
    return [
        mock.patch.object(transport, "call_json", side_effect=stub.call_json),
        mock.patch.object(advisor, "call_json", side_effect=stub.legacy_call_json),
        mock.patch.object(advisor, "_market_reading", return_value=(_reading(), {})),
    ]


def _source(name):
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _tree(name):
    return ast.parse(_source(name))


def _imported_roots(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _called_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _docstring_ids(tree):
    """模块 / 类 / 函数的第一条字符串表达式（docstring）的 ``id``。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _code_strings(tree, table):
    """模块里所有**可拼出 SQL 文本**的字符串（含 f-string），docstring 除外。

    * docstring 必须排除：文档要能逐字写出"谁写了这张表"，把说明当成语句会让护栏反过来
      禁止解释它自己。
    * f-string 在 AST 里是 ``JoinedStr``，``{TABLE}`` 会被还原成标识符 ``TABLE`` 而不是
      它的值。必须用模块级常量别名（``TABLE = "<table>"``）把它渲染回真实表名 —— 否则
      ``f"INSERT INTO {TABLE} ..."`` 这种写法会整个绕过 writer 扫描器，而 canonical 的
      真实写入口正是这种形式。
    """
    skip = _docstring_ids(tree)
    aliases = _table_constant_names(tree, table)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                out.append(" ".join(node.value.split()))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    rendered = ast.unparse(value.value)
                    parts.append(table if rendered in aliases else rendered)
            out.append(" ".join("".join(parts).split()))
    return out


def _table_constant_names(tree, table):
    """模块级 ``NAME = "<table>"`` 形式的常量名（f-string 占位符 → 表名）。"""
    names = set()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and node.value.value == table):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _production_insert_writers(table):
    """production 源码里出现 ``INSERT INTO <table>`` 的文件集合。

    判别只认**渲染出真实表名**的语句：``adaptive_engine`` 里有一个通用
    ``f"INSERT INTO {table} (...)"`` 拼表名的辅助语句（``table`` 是函数参数，没有模块级
    别名），把它当成"写了这张表"会让这条扫描器彻底失去意义。
    """
    needle = f"INSERT INTO {table}"
    writers = set()
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        if any(needle in text for text in _code_strings(_tree(name), table)):
            writers.add(name)
    return writers


def _code_words(tree):
    """模块**代码**里的标识符与字符串常量 —— **排除 docstring**。

    本轮的边界模块必须在 docstring 里写清"它不做什么"（否则这条 guard 会逼出"不敢写
    清楚边界"的文档），所以字符级子串搜索会把自己的说明文字当成越界证据。只看真正会
    执行的名字与字符串，才能让这条断言有语义。
    """
    skip = _docstring_ids(tree)
    words = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.add(node.id)
        elif isinstance(node, ast.Attribute):
            words.add(node.attr)
        elif isinstance(node, ast.arg):
            words.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            words.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            words.add(node.value)
    return words


def _function_source(name, func_name):
    """某个函数的源码片段（含签名与 docstring），供"迁移路径代码无越界词汇"断言。"""
    text = _source(name)
    for node in ast.walk(_tree(name)):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return "\n".join(text.splitlines()[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} 里找不到 {func_name}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.factory = _TrackingFactory()
        self.addCleanup(self.factory.close)
        advisor.ensure_schema(self.factory.conn)

    def env(self, **overrides):
        """设定 provider 相关环境变量；传 ``None`` 表示该变量**必须不存在**。

        ``None`` 这条路径是给"只在数据库 / UI 里配好槽位"的场景用的：canonical research
        的准入不允许再依赖厂商环境变量。
        """
        values = {
            "LLM_PROVIDER": "deepseek",
            "LLM_ADVISOR_ENABLED": "1",
            "DEEPSEEK_API_KEY": "test-key",
        }
        values.update(overrides)
        patcher = mock.patch.dict(
            os.environ, {k: v for k, v in values.items() if v is not None}, clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # LIFO：先把这个变量放回去，再让 patch.dict 还原整份快照。
        for name in (k for k, v in values.items() if v is None):
            if name in os.environ:
                original = os.environ.pop(name)
                self.addCleanup(os.environ.__setitem__, name, original)

    def slot_row(self, slot, **fields):
        """直接写 ``ai_provider_slots`` —— 模拟"只在数据库 / UI 里配置"的部署。"""
        import ai_review_service
        ai_review_service.update_slot(self.factory.conn, slot, **fields)
        self.factory.conn.commit()

    def canonical(self):
        """canonical 行数。表还不存在即 0 —— "一次都没写"是正确的零，不是错误。"""
        row = self.factory.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (REP.TABLE,),
        ).fetchone()
        if not row or not row[0]:
            return 0
        return self.factory.conn.execute(
            f"SELECT COUNT(*) FROM {REP.TABLE}"
        ).fetchone()[0]

    def legacy(self):
        return self.factory.conn.execute(
            "SELECT COUNT(*) FROM adaptive_advisor_runs"
        ).fetchone()[0]

    def service_call(self, *, events=None, payload=None, error=None, clock=None,
                     purpose=PURPOSE, trigger=TRIGGER, as_of=DAY, **kwargs):
        events = (_event(),) if events is None else events
        stub = _TransportStub(self.factory, payload=payload or _payload_for(events),
                              error=error)
        for patcher in _install(stub):
            patcher.start()
            self.addCleanup(patcher.stop)
        result = SVC.run_research_run(
            self.factory,
            purpose=purpose, trigger=trigger,
            hypothesis_id="H-1", as_of=as_of, subject=CODE, question=QUESTION,
            events=events, provider_config=_provider_config(),
            clock=clock, **kwargs,
        )
        return result, stub


# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME-01 ~ 03 —— 迁移后的 production entrypoint
# ─────────────────────────────────────────────────────────────────────────────


class MigratedRuntimeTests(_Base):
    def test_RUNTIME_01_production_entrypoint_reaches_the_canonical_ledger(self):
        """RUNTIME-01：真实 production entrypoint（``run_review``）进入 canonical ledger。

        ``deepseek_advisor.run_review`` 是被 ``adaptive_engine`` 四个 production 调用点
        调用的那个函数。迁移前它只写 ``adaptive_advisor_runs``，canonical 台账一行都没有。
        """
        self.env()
        stub = _TransportStub(self.factory, payload=_payload_for((_event(),)))
        for patcher in _install(stub):
            patcher.start()
            self.addCleanup(patcher.stop)

        result = advisor.run_review(
            self.factory, "/nonexistent/paper.sqlite3", ("/nonexistent/snapshot.json",),
            config={"llm_advisor_enabled": True}, trigger=TRIGGER,
        )

        self.assertEqual(1, self.canonical(), "production entrypoint 没有进入 canonical ledger")
        self.assertEqual("supported", result["status"])
        self.assertIsInstance(result["id"], int)
        row = REP.recent_runs(self.factory.conn, limit=1)[0]
        self.assertEqual(PURPOSE, row["purpose"])
        self.assertEqual(TRIGGER, row["trigger"])
        self.assertEqual(DAY, row["as_of"])
        self.assertEqual("research", row["authority"])
        self.assertFalse(row["is_authoritative"])
        # 迁移前这条路径的证据是 legacy 聚合 dict；现在必须是 typed 事实身份。
        self.assertEqual("market_data", row["hypothesis"]["evidence"][0]["source_type"])
        self.assertEqual("research", result["report"]["authority"])

    def test_RUNTIME_02_provider_is_called_exactly_once(self):
        """RUNTIME-02：provider 恰好被调用一次，且 legacy provider 一次都没有。"""
        result, stub = self.service_call()
        self.assertEqual(1, stub.calls, "typed provider 调用次数不为 1")
        self.assertEqual(0, stub.legacy_calls, "迁移路径又调了一次 legacy provider")
        self.assertEqual(1, self.canonical())

    def test_RUNTIME_03_canonical_append_happens_exactly_once_without_dual_write(self):
        """RUNTIME-03：canonical append 一次；legacy 表**零**写入（没有 dual-write）。"""
        self.env()
        stub = _TransportStub(self.factory, payload=_payload_for((_event(),)))
        for patcher in _install(stub):
            patcher.start()
            self.addCleanup(patcher.stop)

        advisor.run_review(
            self.factory, "/nonexistent/paper.sqlite3", ("/nonexistent/snapshot.json",),
            config={"llm_advisor_enabled": True}, trigger=TRIGGER,
        )

        self.assertEqual(1, self.canonical())
        self.assertEqual(0, self.legacy(), "迁移路径仍然写了 legacy 表 —— dual-write")


# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME-04 ~ 06 —— 三类失败都不留 canonical 行
# ─────────────────────────────────────────────────────────────────────────────


class FailureSemanticsTests(_Base):
    def test_RUNTIME_04_provider_failure_leaves_zero_canonical_rows(self):
        """RUNTIME-04：provider 失败 → canonical row = 0，且不回落 legacy。"""
        with self.assertRaises(SVC.ResearchServiceError) as caught:
            self.service_call(error=RuntimeError("boom"))
        self.assertEqual(SVC.REASON_PROVIDER_FAILED, caught.exception.reason)
        self.assertEqual("provider", caught.exception.stage)
        self.assertEqual(0, self.canonical(), "provider 失败却留下了半条 canonical run")

    def test_RUNTIME_05_contract_validation_failure_leaves_zero_canonical_rows(self):
        """RUNTIME-05：provider 输出违反协议（声明裁决字段）→ canonical row = 0。

        走的是**真实**的 provider 严格解析器：``status`` 出现在输出里即协议违规。
        """
        bad = dict(PROVIDER_PAYLOAD)
        bad["status"] = "supported"
        bad["evidence_relations"] = [{"evidence_id": _event().evidence_id, "relation": "supports"}]
        with self.assertRaises(SVC.ResearchServiceError) as caught:
            self.service_call(payload=bad)
        self.assertEqual(SVC.REASON_PROVIDER_FAILED, caught.exception.reason)
        self.assertEqual(0, self.canonical(), "契约校验失败却写入了 canonical ledger")

    def test_RUNTIME_06_persistence_failure_is_never_reported_as_success(self):
        """RUNTIME-06：canonical 写入失败必须显式上报，绝不假装成功。"""
        with mock.patch.object(REP, "append_run", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(SVC.ResearchServiceError) as caught:
                self.service_call()
        self.assertEqual(SVC.REASON_PERSISTENCE_FAILED, caught.exception.reason)
        self.assertEqual("persistence", caught.exception.stage)
        self.assertEqual(0, self.canonical())

        # production entrypoint 把失败翻译成明确的 ``failed``，而不是"AI 没意见"。
        self.env()
        stub = _TransportStub(self.factory, payload=_payload_for((_event(),)))
        for patcher in _install(stub):
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(REP, "append_run", side_effect=sqlite3.OperationalError("disk")):
            result = advisor.run_review(
                self.factory, "/nonexistent/paper.sqlite3", ("/nonexistent/snapshot.json",),
                config={"llm_advisor_enabled": True}, trigger=TRIGGER,
            )
        self.assertEqual("failed", result["status"])
        self.assertEqual("persistence_persistence_failed", result["error_code"])
        self.assertIsNone(result["report"])
        self.assertEqual(0, self.canonical())


# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME-07 ~ 08 —— legacy 历史行与 legacy writer 计数
# ─────────────────────────────────────────────────────────────────────────────


class LegacyBoundaryTests(_Base):
    def _historic_legacy_row(self):
        self.factory.conn.execute(
            "INSERT INTO adaptive_advisor_runs("
            "purpose,trigger,status,provider,model,evidence_hash,evidence,report,error_code,"
            "latency_ms,input_tokens,output_tokens,created_at,finished_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("data_quality", "legacy-2025", "completed", "DeepSeek", "old-model",
             "deadbeef", json.dumps({"legacy": True}), json.dumps({"verdict": "info"}),
             None, 10, 1, 1, "2025-01-02T10:00:00+08:00", "2025-01-02T10:00:01+08:00"),
        )
        self.factory.conn.commit()

    def test_RUNTIME_07_legacy_historical_rows_are_never_migrated(self):
        """RUNTIME-07：旧行既不被搬进 canonical ledger，也不被改写。"""
        self._historic_legacy_row()
        before = [dict(row) for row in self.factory.conn.execute(
            "SELECT * FROM adaptive_advisor_runs"
        )]
        self.env()
        stub = _TransportStub(self.factory, payload=_payload_for((_event(),)))
        for patcher in _install(stub):
            patcher.start()
            self.addCleanup(patcher.stop)
        advisor.run_review(
            self.factory, "/nonexistent/paper.sqlite3", ("/nonexistent/snapshot.json",),
            config={"llm_advisor_enabled": True}, trigger=TRIGGER,
        )
        after = [dict(row) for row in self.factory.conn.execute(
            "SELECT * FROM adaptive_advisor_runs"
        )]
        self.assertEqual(before, after, "legacy 历史行被改动或回填")
        self.assertEqual(1, self.legacy(), "legacy 历史行计数发生变化")
        # canonical 里只有本次迁移后的新研究，**没有**旧行的副本（不回填 provenance）。
        canonical = REP.recent_runs(self.factory.conn, limit=10)
        self.assertEqual(1, len(canonical))
        self.assertEqual(TRIGGER, canonical[0]["trigger"])
        self.assertNotIn("legacy-2025", {row["trigger"] for row in canonical})

        # 只有 legacy 行、还没有 canonical 行时，canonical 读入口必须返回 None ——
        # 把历史 free-form 行抬成"这里有一条 typed 研究"就是伪造 provenance。
        fresh = _TrackingFactory()
        self.addCleanup(fresh.close)
        advisor.ensure_schema(fresh.conn)
        fresh.conn.execute(
            "INSERT INTO adaptive_advisor_runs("
            "purpose,trigger,status,provider,model,evidence_hash,evidence,report,error_code,"
            "latency_ms,input_tokens,output_tokens,created_at,finished_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("data_quality", "legacy-only", "completed", "DeepSeek", "old", "h", "{}",
             "{}", None, 1, 0, 0, "2025-01-02T10:00:00+08:00", "2025-01-02T10:00:01+08:00"),
        )
        fresh.conn.commit()
        self.assertIsNone(
            advisor.latest_data_quality_research(fresh.conn),
            "legacy 历史行被当成 canonical typed 研究读出来了",
        )

    def test_RUNTIME_08_migrated_path_writes_zero_legacy_rows(self):
        """RUNTIME-08：迁移路径的 legacy writer = 0（静态 + 运行时两重证据）。

        静态：``deepseek_advisor.py`` 不再含 ``INSERT INTO adaptive_advisor_runs``，该表的
        production writer 收缩到尚未迁移的 ``deepseek_research.py``。
        运行时：跑一次迁移路径，legacy 行数不变。
        """
        writers = _production_insert_writers("adaptive_advisor_runs")
        self.assertNotIn(ADVISOR_MODULE, writers,
                         f"{ADVISOR_MODULE} 仍然是 adaptive_advisor_runs 的 writer：{writers}")
        self.assertEqual({"deepseek_research.py"}, writers,
                         f"adaptive_advisor_runs 的 writer 集合意外变化：{writers}")

        self.assertEqual(0, self.legacy())
        self.service_call()
        self.assertEqual(0, self.legacy(), "迁移路径写入了 legacy 表")


# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME-09 ~ 10 —— research 不获得任何新权限
# ─────────────────────────────────────────────────────────────────────────────


class AuthorityBoundaryTests(_Base):
    def test_RUNTIME_09_research_gains_no_signal_order_risk_or_promotion_authority(self):
        """RUNTIME-09：研究结果不具备 signal / order / risk / promotion 权限。"""
        result, _stub = self.service_call()
        self.assertEqual("research", result.hypothesis.authority)
        self.assertFalse(result.hypothesis.is_authoritative)
        self.assertTrue(result.hypothesis.is_supported)

        # ① orchestration boundary 的**代码**里没有 execution authority 词汇。
        #    只看代码（标识符 + 非 docstring 常量）：边界模块必须能在 docstring 里写清
        #    "它不做什么"，字符级子串搜索会把那些说明当成越界证据。
        words = _code_words(_tree(SERVICE_MODULE))
        for token in FORBIDDEN_AUTHORITY_TOKENS:
            with self.subTest(where=SERVICE_MODULE, token=token):
                self.assertFalse(
                    [word for word in words if token.lower() in word.lower()],
                    f"{SERVICE_MODULE} 的代码出现越界词汇 {token}",
                )

        # ② 迁移后的那个 entrypoint 本身也不含执行 / 应用词汇。刻意只断言这个函数，
        #    因为 deepseek_advisor 里**未迁移**的 tuner 路径合法地持有账本对账与
        #    shadow proposal 词汇 —— 那是 proposal 边界，不在本轮 research 收敛范围内。
        run_review_source = _function_source(ADVISOR_MODULE, "run_review")
        for token in FORBIDDEN_AUTHORITY_TOKENS:
            with self.subTest(where="run_review", token=token):
                self.assertNotIn(token, run_review_source)

        # ③ 真正落库的那一行不含任何执行词汇。
        persisted = json.dumps(
            REP.recent_runs(self.factory.conn, limit=1)[0], ensure_ascii=False,
        )
        for token in FORBIDDEN_AUTHORITY_TOKENS:
            with self.subTest(where="persisted row", token=token):
                self.assertNotIn(token, persisted)

        # ④ 读投影把 authority 写成**常量**，不会被 status 带动。
        view = advisor.research_report_view(result)
        self.assertEqual("research", view["authority"])
        self.assertFalse(view["is_authoritative"])
        self.assertEqual("evidence_review_not_truth_proof", view["truth_claim"])

    def test_RUNTIME_10_reading_a_canonical_row_does_not_re_authorize_research(self):
        """RUNTIME-10：读 canonical 行只是历史投影，不是重新授权后的结论。"""
        self.service_call()
        row = advisor.latest_data_quality_research(self.factory.conn)
        self.assertIsNotNone(row)
        self.assertEqual("supported", row["status"])
        self.assertFalse(row["is_authoritative"])
        self.assertEqual("research", row["authority"])

        view = advisor.research_report_view_from_row(row)
        self.assertEqual("research", view["authority"])
        self.assertFalse(view["is_authoritative"])
        # 同一形状：读出来的投影与刚跑完的投影不会各有一套渲染逻辑。
        fresh, _stub = self.service_call()
        self.assertEqual(
            sorted(advisor.research_report_view(fresh)),
            sorted(view),
        )
        # repository **不**重建 typed 对象：投影里没有 hypothesis 实例。
        self.assertNotIsInstance(row["hypothesis"], ARC.ResearchHypothesis)
        self.assertIsInstance(row["hypothesis"], dict)


# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME-11 ~ 12 —— 网络 owner 与事务边界
# ─────────────────────────────────────────────────────────────────────────────


class NetworkAndTransactionTests(_Base):
    def test_RUNTIME_11_provider_network_owner_is_still_unique(self):
        """RUNTIME-11：service 不拥有网络；provider 链路里 urlopen 只在 transport。"""
        service_tree = _tree(SERVICE_MODULE)
        self.assertNotIn("urlopen", _called_names(service_tree))
        for root in _imported_roots(service_tree):
            self.assertNotIn(root, {"urllib", "requests", "httpx", "socket"})

        providers = []
        for name in (SERVICE_MODULE, "ai_research_provider.py", REPOSITORY_MODULE,
                     "ai_provider_transport.py"):
            if "urlopen" in _called_names(_tree(name)):
                providers.append(name)
        self.assertEqual(["ai_provider_transport.py"], providers,
                         f"provider 链路里网络 owner 不再唯一：{providers}")
        # 非空性：transport 必须真的持有那次调用。
        self.assertIn("urlopen", _called_names(_tree("ai_provider_transport.py")))

    def test_RUNTIME_12_network_call_happens_outside_any_db_transaction(self):
        """RUNTIME-12：provider 的 HTTP 调用发生在**任何** DB 事务之外。"""
        _result, stub = self.service_call()
        self.assertEqual(1, len(stub.factory.depth_at_network))
        self.assertEqual(0, stub.factory.depth_at_network[0],
                         "网络调用发生在事务内 —— LLM 等待会持有 SQLite 写锁")

        # 非空性：这条断言必须能失败。手工在事务里调一次，同样的量测应当变红。
        with self.factory():
            during = self.factory.depth
        self.assertEqual(1, during, "连接深度探针本身失效（guard 会空转）")


# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME-13 ~ 14 —— 时间语义与重复执行
# ─────────────────────────────────────────────────────────────────────────────


class TimeAndIdempotencyTests(_Base):
    def test_RUNTIME_13_as_of_and_created_at_are_separate(self):
        """RUNTIME-13：``as_of`` 来自事实/调用方，``created_at`` 由 service 取运维时间。"""
        result, _stub = self.service_call(clock=lambda: CREATED_AT)
        self.assertEqual(DAY, result.hypothesis.as_of)
        self.assertEqual(DAY, result.created_at[:10])
        self.assertEqual(CREATED_AT, result.created_at)
        # as_of 绝不能等于"跑研究的那一刻"：注入一个与业务日不同的 clock 即可证明分离。
        other, _stub2 = self.service_call(clock=lambda: "2030-01-01T00:00:00+08:00")
        self.assertEqual(DAY, other.hypothesis.as_of)
        self.assertEqual("2030-01-01T00:00:00+08:00", other.created_at)
        # ``created_at`` 不参与研究内容指纹：两次内容相同、只有运维时间不同的运行，
        # ``record_hash`` 必须相同（否则"运维时间"被误当成研究内容）。
        rows = REP.recent_runs(self.factory.conn, limit=2)
        self.assertEqual(rows[0]["record_hash"], rows[1]["record_hash"])
        self.assertNotEqual(rows[0]["created_at"], rows[1]["created_at"])
        self.assertEqual({DAY}, {row["as_of"] for row in rows})

    def test_RUNTIME_14_repeated_invocation_appends_twice_without_a_business_key(self):
        """RUNTIME-14：两次明确执行 = 两条 run（append-only，无业务幂等键）。"""
        first, _a = self.service_call()
        second, _b = self.service_call()
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(2, self.canonical())
        self.assertEqual(2, len(REP.recent_runs(self.factory.conn, limit=10)))
        # 同一个 hypothesis_id 不构成唯一键：service 刻意不生成 business key。
        rows = REP.recent_runs(self.factory.conn, limit=10)
        self.assertEqual({"H-1"}, {row["hypothesis_id"] for row in rows})
        self.assertNotIn("research_run_key", REP.RUN_COLUMNS)


# ─────────────────────────────────────────────────────────────────────────────
# 读路径与过滤器 —— 迁移后的读侧接线
# ─────────────────────────────────────────────────────────────────────────────


class ReadPathBootstrapTests(_Base):
    def test_RUNTIME_15_canonical_read_path_works_before_the_first_run(self):
        """RUNTIME-15：canonical 表在首次研究运行成功之前**不存在**，读路径也必须能用。

        ``overview`` 是每次刷新概览都会走的路径（``adaptive_engine._overview_uncached``），
        而 canonical 台账只有在一次 append 之后才存在。读入口先 ``ensure_schema``，因此
        全新库（或尚未跑过研究的库）不会 ``no such table: ai_research_runs``。
        """
        tables = {row[0] for row in self.factory.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertNotIn(REP.TABLE, tables, "前置条件：此刻 canonical 表不该存在")

        self.assertIsNone(advisor.latest_data_quality_research(self.factory.conn))
        view = advisor.overview(self.factory.conn, {})
        self.assertIsNone(view["latest"])
        self.assertEqual({}, view["latest_by_purpose"])
        # 读路径只建表，不写研究行。
        self.assertEqual(0, self.canonical())

    def test_RUNTIME_16_overview_prefers_canonical_and_keeps_legacy_visible(self):
        """RUNTIME-16：writer 迁移后 ``overview`` 以 canonical 为准，旧行仍可见但不冒充最新。"""
        self.factory.conn.execute(
            "INSERT INTO adaptive_advisor_runs("
            "purpose,trigger,status,provider,model,evidence_hash,evidence,report,error_code,"
            "latency_ms,input_tokens,output_tokens,created_at,finished_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("data_quality", "legacy-old", "completed", "DeepSeek", "old", "h", "{}", "{}",
             None, 1, 0, 0, "2025-01-02T10:00:00+08:00", "2025-01-02T10:00:01+08:00"),
        )
        self.factory.conn.commit()
        self.service_call()

        view = advisor.overview(self.factory.conn, {})
        latest = view["latest"]
        self.assertEqual("canonical_research_ledger", latest["source"])
        self.assertEqual(TRIGGER, latest["trigger"])
        self.assertEqual("research", latest["report"]["authority"])
        self.assertFalse(latest["report"]["is_authoritative"])
        self.assertEqual(
            "canonical_research_ledger",
            view["latest_by_purpose"]["data_quality"]["source"],
        )


class PurposeFilterTests(_Base):
    def test_RUNTIME_17_purpose_filter_is_exact_and_bounded(self):
        """RUNTIME-17：canonical 读入口的 ``purpose`` 过滤是精确等值，且复用**写侧**上界。

        过滤只做等值筛选：它不排序、不聚合、不解释 purpose 的业务含义，因此"按 purpose 取
        最新"与"按 ``id DESC`` 取最新"是同一件事。上界与写路径共用
        ``MAX_PURPOSE_CHARS``，避免"读侧比写侧宽"造出一个永远无法满足的查询。
        """
        self.service_call()
        self.assertEqual(1, len(REP.recent_runs(self.factory.conn, limit=5, purpose=PURPOSE)))
        self.assertEqual(0, len(REP.recent_runs(self.factory.conn, limit=5, purpose="other")))
        # 不传 = 不过滤（与 B2A 的既有语义一致）。
        self.assertEqual(1, len(REP.recent_runs(self.factory.conn, limit=5)))
        for bad in ("", 42, "x" * (REP.MAX_PURPOSE_CHARS + 1)):
            with self.subTest(bad=repr(bad)[:24]):
                with self.assertRaises((TypeError, ValueError)):
                    REP.recent_runs(self.factory.conn, limit=5, purpose=bad)


# ─────────────────────────────────────────────────────────────────────────────
# provider 配置权威 —— canonical 槽位，不是 legacy 环境变量
# ─────────────────────────────────────────────────────────────────────────────


class ProviderConfigAuthorityTests(_Base):
    """这两半是同一个根因：provider 配置权威只收敛了一半。

    * 只查 legacy ``configured()`` → **只在数据库/UI 里配好的槽位**被错误判成"未配置"；
    * 只把 ``provider_config`` 交给 transport → ``enabled=False`` 被绕过，
      操作员的 disable 挡不住真实付费调用。
    """

    #: "只在数据库 / UI 里配置"的槽位：没有对应的厂商环境变量。
    DB_ONLY = {
        "api_key": "db-only-key",
        "base_url": "https://api.deepseek.com",
        "model": "db-only-model",
        "enabled": True,
    }

    def _run(self):
        return advisor.run_review(
            self.factory, "/nonexistent/paper.sqlite3", ("/nonexistent/snapshot.json",),
            config={"llm_advisor_enabled": True}, trigger=TRIGGER,
        )

    def _stub(self):
        stub = _TransportStub(self.factory, payload=_payload_for((_event(),)))
        for patcher in _install(stub):
            patcher.start()
            self.addCleanup(patcher.stop)
        return stub

    def test_RUNTIME_18_db_only_canonical_slot_runs_without_legacy_env_key(self):
        """RUNTIME-18：槽位只在 DB / UI 里配好、没有任何厂商环境变量时，research 必须能跑。

        ``configured()`` 在这个状态下是 **False** —— 断言它，才能证明旧的 legacy 准入条件
        真的被移除了，而不是碰巧两条判据同时为真。
        """
        self.env(DEEPSEEK_API_KEY=None, LLM_PROVIDER="deepseek")
        self.slot_row("ai2", **self.DB_ONLY)
        self.assertFalse(advisor.configured(), "前置条件：本用例必须没有 legacy 环境变量")

        stub = self._stub()
        result = self._run()

        self.assertEqual("supported", result["status"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(1, stub.calls, "DB-only 槽位没有发出那次 provider 调用")
        self.assertEqual(0, stub.legacy_calls)
        self.assertEqual(1, self.canonical())
        row = REP.recent_runs(self.factory.conn, limit=1)[0]
        self.assertEqual(TRIGGER, row["trigger"])
        self.assertEqual("db-only-model", row["provider_model"])
        self.assertEqual("ai2", row["provider_slot"])

    def test_RUNTIME_19_disabled_slot_performs_zero_provider_calls(self):
        """RUNTIME-19：``enabled=False`` 的槽位 —— 零网络、零 canonical row、零 legacy row。

        ``ai_provider_transport.call_json`` 只检查 ``api_key`` / ``base_url`` / ``model``，
        所以"被禁用"必须在交给它之前就拦下。否则操作员的 disable 只挡住了 UI，挡不住付费。
        """
        self.env(DEEPSEEK_API_KEY=None, LLM_PROVIDER="deepseek")
        self.slot_row("ai2", **{**self.DB_ONLY, "enabled": False})

        stub = self._stub()
        result = self._run()

        self.assertEqual(0, stub.calls, "被禁用的槽位仍然发起了 provider 调用")
        self.assertEqual(0, stub.legacy_calls)
        self.assertEqual(0, self.canonical(), "被禁用的槽位仍然写入了 canonical row")
        self.assertEqual(0, self.legacy())
        self.assertEqual("blocked", result["status"])
        self.assertEqual("research_disabled", result["error_code"])
        self.assertIsNone(result["report"])

    def test_RUNTIME_20_config_resolution_failure_obeys_declared_failure_semantics(self):
        """RUNTIME-20：配置解析失败不得裸异常逃逸 —— 按声明的 best-effort 契约稳定映射。

        两种失败都要覆盖：槽位映射不存在（``LLM_PROVIDER`` 指向没有 canonical 槽位的厂商），
        以及读取槽位配置本身抛错。两者都必须表现为返回值里的稳定 ``error_code``。
        """
        # ① 映射不存在：``kimi`` 没有 canonical 槽位（ai1/ai2 只对应 mimo/deepseek）。
        self.env(DEEPSEEK_API_KEY=None, KIMI_API_KEY="kimi-key", LLM_PROVIDER="kimi")
        stub = self._stub()
        result = self._run()
        self.assertEqual("blocked", result["status"])
        self.assertEqual("research_provider_slot_unavailable", result["error_code"])
        self.assertEqual(0, stub.calls)
        self.assertEqual(0, self.canonical())

        # ② 读取槽位配置抛错：不得逃逸。
        self.env()
        stub = self._stub()
        import ai_review_service
        with mock.patch.object(
            ai_review_service, "get_slot_config", side_effect=RuntimeError("boom"),
        ):
            result = self._run()
        self.assertEqual("failed", result["status"])
        self.assertEqual("research_config_RuntimeError", result["error_code"])
        self.assertEqual(0, stub.calls)
        self.assertEqual(0, self.canonical())
        self.assertEqual(0, self.legacy())

    def test_RUNTIME_21_readiness_predicate_is_single_and_public_view_agrees(self):
        """RUNTIME-21：就绪判据只有一份，GET 视图与 research runtime 从同一处取。"""
        import ai_review_service

        base = {
            "slot": "ai2", "display_name": "AI 2", "api_key": "k",
            "base_url": "https://api.deepseek.com", "model": "m", "enabled": True,
            "timeout_seconds": 40, "updated_at": None, "source": "database",
        }
        cases = [
            (dict(base), ai_review_service.SLOT_READY, True),
            ({**base, "api_key": ""}, ai_review_service.SLOT_NOT_CONFIGURED, False),
            ({**base, "api_key": "   "}, ai_review_service.SLOT_NOT_CONFIGURED, False),
            ({**base, "enabled": False}, ai_review_service.SLOT_DISABLED, False),
            ({**base, "base_url": "abc"}, ai_review_service.SLOT_BASE_URL_UNUSABLE, False),
            ({**base, "model": ""}, ai_review_service.SLOT_MODEL_MISSING, False),
        ]
        for cfg, reason, ready in cases:
            with self.subTest(reason=reason, ready=ready):
                verdict = ai_review_service.slot_readiness(cfg)
                self.assertEqual(ready, verdict["ready"])
                self.assertEqual(reason, verdict["reason"])
                # GET 视图的 ready 必须与判据一致（两处各写一遍必然漂移）。
                self.assertEqual(
                    verdict["ready"],
                    ai_review_service.slot_public_view(cfg)["ready"],
                )
        # 非空性：禁用确实会改变判据结果，而不是两种情况恰好同值。
        self.assertNotEqual(
            ai_review_service.slot_readiness({**base, "enabled": False})["reason"],
            ai_review_service.slot_readiness(dict(base))["reason"],
        )


# ─────────────────────────────────────────────────────────────────────────────
# 架构 guard —— service 的边界本身
# ─────────────────────────────────────────────────────────────────────────────


class ServiceArchitectureGuardTests(unittest.TestCase):
    def test_SERVICE_imports_are_exactly_stdlib_provider_and_repository(self):
        """service 的 import 是**等值**闭集：多一个依赖都必须是一次有意识的决定。"""
        roots = _imported_roots(_tree(SERVICE_MODULE))
        self.assertEqual(ALLOWED_SERVICE_IMPORTS, roots,
                         f"{SERVICE_MODULE} 的 import 集合发生变化：{sorted(roots)}")

    def test_SERVICE_does_not_import_the_research_contract(self):
        """依赖方向：service 只经由 provider / repository 使用契约。

        provider 交出来的已经是契约对象，repository 会再独立校验一次。service 再 import
        契约就等于多一份"什么算合法 typed 结论"的规则，必然漂移。
        """
        self.assertNotIn("ai_research_contract", _imported_roots(_tree(SERVICE_MODULE)))

    def test_REPOSITORY_and_PROVIDER_never_depend_on_the_service(self):
        """依赖方向不可反转：下层不得 import 上层 orchestration。"""
        for name in ("ai_research_provider.py", REPOSITORY_MODULE,
                     "ai_research_contract.py", "ai_provider_transport.py"):
            with self.subTest(module=name):
                self.assertNotIn(
                    "ai_research_service", _imported_roots(_tree(name)),
                    f"{name} 反向依赖 orchestration 层",
                )

    def test_SERVICE_contains_no_sql_and_no_network(self):
        """service 不持有 SQL、不持有网络、不持有第三套 provider 配置来源。"""
        text = _source(SERVICE_MODULE)
        for token in FORBIDDEN_SERVICE_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, text, f"{SERVICE_MODULE} 出现 {token}")
        roots = _imported_roots(_tree(SERVICE_MODULE))
        for root in ("os", "urllib", "requests", "httpx", "socket", "subprocess", "sqlite3"):
            with self.subTest(root=root):
                self.assertNotIn(root, roots, f"{SERVICE_MODULE} import 了 {root}")
        called = _called_names(_tree(SERVICE_MODULE))
        for token in ("getenv", "environ", "urlopen", "urlretrieve"):
            with self.subTest(called=token):
                self.assertNotIn(token, called, f"{SERVICE_MODULE} 调用了 {token}")

    def test_scanners_are_not_vacuously_passing(self):
        """护栏本身必须真的能失败，否则它只是装饰。"""
        self.assertIn("ai_research_repository",
                      _imported_roots(ast.parse("import ai_research_repository\n")))
        self.assertIn("urlopen",
                      _called_names(ast.parse("import urllib.request\nurllib.request.urlopen(r)\n")))
        self.assertIn("INSERT", "INSERT INTO x(a) VALUES(?)")
        self.assertNotIn("INSERT", ast.dump(ast.parse('"""docstring only"""\nX = 1\n')))
        # 扫描器必须能看出"谁写了这张表"。
        self.assertEqual({"deepseek_research.py"},
                         _production_insert_writers("adaptive_advisor_runs"))
        self.assertEqual({REPOSITORY_MODULE},
                         _production_insert_writers(REP.TABLE))


if __name__ == "__main__":
    unittest.main()
