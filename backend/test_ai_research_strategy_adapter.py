# -*- coding: utf-8 -*-
"""R27-B2C-6 —— strategy research adapter 的永久回归（EXP-19 ~ EXP-28）。

owner 侧的 EXP-01 ~ EXP-18 在 ``test_adaptive_experiment_evidence_ownership``。本文件只管
**接缝**：入口只接受哪些类型、identity / as_of 从哪来、指纹是否确定性、本层是否碰 DB / 网络 /
时钟、registry 与真实 factory 是否双向一致，以及本轮**刻意不迁移**的 legacy runtime 是否
仍然在、并且被明确记为 deferred。

────────────── 为什么这些必须是结构断言 ──────────────

"adapter 不读 DB"若只写在注释里，下一次有人为了"顺手补一个字段"而 import ``sqlite3`` 时没有
任何东西会红。``EXP-24`` 因此直接扫 import 与调用点：这条边界是可执行的，不是风格建议。
``EXP-26`` 同理 —— "production 调用点 = 0"是本轮交付物的一部分（能力先落地、runtime 迁移
留给后续），它必须是可验证的事实，而不是一句声明。
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import sqlite3
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_risk as AR  # noqa: E402
import adaptive_selection as ASEL  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import ai_research_strategy_adapter as ADAPTER  # noqa: E402
import deepseek_research as DR  # noqa: E402
import learning_dataset as LD  # noqa: E402
import learning_evaluation as LE  # noqa: E402

ADAPTER_FILE = "ai_research_strategy_adapter.py"
STRATEGY_FACTORY = "evidence_ref_from_strategy_projection"

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
    """模块真正 import 的顶层模块名（``__future__`` 除外 —— 它不是依赖）。"""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots - {"__future__"}


def _calls_in(name: str) -> list[tuple[str, int]]:
    """模块里每个 ``name(...)`` 调用点（``(模块, 行号)``）。只看调用，不看定义/字符串。"""
    calls = []
    for node in ast.walk(ast.parse(_source(name))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if called == name:
            calls.append((name, node.lineno))
    return calls


# ───────────────────────────── fixtures ─────────────────────────────


def _selection_fact(conn, **overrides):
    baseline = {"weights": dict(ASEL.BASE_WEIGHTS["one_to_two"])}
    candidate = dict(baseline["weights"])
    keys = sorted(candidate)
    candidate[keys[0]] = round(candidate[keys[0]] + 0.02, 6)
    candidate[keys[1]] = round(candidate[keys[1]] - 0.02, 6)
    candidate_id = ASEL.record_shadow_proposal(
        conn, run_date=overrides.get("run_date", "2026-09-18"),
        account_id=overrides.get("account_id", "tq_breakout"),
        regime=overrides.get("regime", "momentum:ai:1030"),
        model_id=overrides.get("model_id", "one_to_two"),
        baseline_params=baseline, candidate_params={"weights": candidate},
        evidence={"source": "DeepSeek"}, reason="bounded",
        now=overrides.get("now", "2026-09-21T09:10:00+08:00"),
    )
    return ASEL.selection_candidate_fact(conn, candidate_id, as_of="2026-09-21")


def _risk_fact(conn, *, updated_at="2026-09-21T09:00:00+08:00"):
    cursor = conn.execute(
        """INSERT INTO adaptive_risk_candidates(
           run_date,account_id,regime,baseline_params,candidate_params,evidence,
           risk_reduction_pct,change_kind,status,application_mode,reason,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("2026-09-18", "tq_breakout", "momentum", "{}", "{}", "{}", 1.25,
         "conservative_tighten", "eligible_auto_tighten", "shadow", "r",
         "2026-09-18T09:00:00+08:00", updated_at),
    )
    return AR.risk_candidate_fact(conn, int(cursor.lastrowid), as_of="2026-09-30")


def _experiment_fact(conn, *, cutoff="2026-09-20", blockers=None):
    dataset = LD.build_manifest(
        {}, exclusions={}, cutoff=cutoff, feature_names=["mom_short"],
        horizon_semantics=LD.HORIZON_SEMANTICS_OBSERVED_CLOSE_STEPS,
        split_spec=LD.DEFAULT_SPLIT_SPEC, source_row_count=0,
    )
    if not LD.persist_manifest(conn, dataset):
        raise RuntimeError("dataset manifest fixture was not persisted")
    manifest = {
        "evaluation_fingerprint": "e" * 64,
        "evaluation_schema_version": LE.EVALUATION_SCHEMA_VERSION,
        "evaluation_contract_version": LE.EVALUATION_CONTRACT_VERSION,
        "metric": LE.METRIC_SPEARMAN_RANK_IC,
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "model_id": "shadow-a", "holdout_partition": "test",
        "scoring": {"kind": "rank"}, "prediction_digest": "d" * 64,
        "evaluated_dates": 5, "evaluated_rows": 20, "dropped_dates": 0,
        "mean_rank_ic": "0.120000", "confidence_label": "research_only",
        "per_date_ic": {"2026-01-05": "0.1"}, "exclusion_reasons": {},
        "evaluation_blockers": list(blockers or []),
        "evaluation_contract_ok": not blockers,
        "execution_authority": "none", "evaluation_scope": "research_read_only",
        "created_at": "2026-09-21T02:00:00+00:00",
        "coverage": {}, "holdout": {},
    }
    if not LE.persist_evaluation_manifest(conn, manifest):
        raise RuntimeError("evaluation manifest fixture was not persisted")
    return LE.experiment_evaluation_fact(conn, "e" * 64, as_of="2026-09-21")


def _selection_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ASEL.ensure_schema(conn)
    return _track(conn)


def _risk_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    AR.ensure_schema(conn)
    return _track(conn)


def _learning_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    LD.ensure_schema(conn)
    LE.ensure_schema(conn)
    return _track(conn)


ALL_THREE = ("selection", "risk", "experiment")


def _all_facts() -> dict:
    return {
        "selection": _selection_fact(_selection_conn()),
        "risk": _risk_fact(_risk_conn()),
        "experiment": _experiment_fact(_learning_conn()),
    }


class _RiskSubclass(AR.AdaptiveRiskFactProjection):
    """risk 投影的子类 —— 刻意**不**算已批准类型（``type() is`` 而非 ``isinstance``）。"""


class ExactTypeBoundaryTests(_DbTestCase):
    """EXP-19 ~ EXP-21：入口只接受已批准类型的**本类型**，且不接受 caller 命名。"""

    def test_EXP_19_adapter_accepts_only_exact_approved_owner_types(self):
        """EXP-19：``type(...) is`` 精确判定；dict / Mapping / 伪对象 / **子类**全部拒绝。

        接受"长得像投影的对象"就等于允许调用方自己拼 ``availability_day`` 与
        ``fact_verification_status`` 冒充 owner 投影。子类也必须拒绝：``isinstance`` 会让
        "继承一个已批准类型再覆写字段"成为绕过路径。
        """
        facts = _all_facts()
        for label, fact in facts.items():
            with self.subTest(accepted=label):
                ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
                self.assertEqual(ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH, ref.source_type)

        risk_fact = facts["risk"]
        subclass_instance = _RiskSubclass(**{
            item.name: getattr(risk_fact, item.name)
            for item in dataclasses.fields(risk_fact)
        })
        self.assertIsInstance(subclass_instance, AR.AdaptiveRiskFactProjection)

        rejects = {
            "dict": {"record_kind": "risk_candidate"},
            "mapping proxy": __import__("types").MappingProxyType({}),
            "plain object": object(),
            "duck-typed": type("Fake", (), {
                "record_kind": "risk_candidate",
                "revision_identity": "1@2026-09-21T09:00:00+08:00",
                "availability_day": "2026-09-21",
                "fact_verification_status": AR.RISK_FACT_RECORDED,
                "content_fingerprint": "0" * 64,
                "version": AR.RISK_FACT_CONTRACT_VERSION,
            })(),
            "subclass": subclass_instance,
            "none": None,
            "string": "risk_candidate|1",
        }
        for label, payload in rejects.items():
            with self.subTest(rejected=label):
                with self.assertRaises(TypeError):
                    ADAPTER.evidence_ref_from_strategy_projection(payload)

    def test_EXP_19b_source_type_reuses_the_existing_family_seam(self):
        """EXP-19（补充）：复用既有的 ``strategy_research``，不新增 source type。

        新增 ``adaptive`` / ``experiment`` / ``candidate`` / ``alpha_experiment`` 会让
        kind ↔ source_type 的映射长出第二套词表，而 ``InformationEvent.kind`` 是派生只读的
        —— 细分只能在 ``record_kind`` 里。
        """
        self.assertEqual("strategy_research", ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH)
        self.assertIn(ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH, ARC.EVIDENCE_SOURCE_TYPES)
        for invented in ("adaptive", "experiment", "candidate", "alpha_experiment"):
            self.assertNotIn(invented, ARC.EVIDENCE_SOURCE_TYPES)
        facts = _all_facts()
        for label, fact in facts.items():
            with self.subTest(record_kind=label):
                # 三个 owner 的 record kind 都落在 detail 里，而不是 source_type 上。
                self.assertNotEqual(fact.record_kind, ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH)
                self.assertNotIn(fact.record_kind, ARC.EVIDENCE_SOURCE_TYPES)
        self.assertEqual(
            {AR.RISK_CANDIDATE_RECORD_KIND, ASEL.SELECTION_CANDIDATE_RECORD_KIND,
             LE.EXPERIMENT_EVALUATION_RECORD_KIND},
            {fact.record_kind for fact in facts.values()},
            "三个 owner 的 record kind 必须互不相同（identity 才能按类型唯一归属）",
        )

    def test_EXP_20_adapter_cannot_take_a_caller_supplied_source_id(self):
        """EXP-20：签名里**只有** projection —— 没有 ``source_id`` 参数。

        调用方命名的 identity 不是 identity，而是一个可以用来绕过去重与冲突检测的自由字符串。
        """
        parameters = inspect.signature(
            ADAPTER.evidence_ref_from_strategy_projection
        ).parameters
        self.assertEqual(["projection"], list(parameters))
        for forbidden in ("source_id", "identity", "revision", "record_kind", "detail"):
            self.assertNotIn(forbidden, parameters)

    def test_EXP_21_adapter_cannot_take_a_caller_supplied_as_of(self):
        """EXP-21：签名里也没有 ``as_of`` —— 可用性只能来自 owner 证明的瞬间。"""
        parameters = inspect.signature(
            ADAPTER.evidence_ref_from_strategy_projection
        ).parameters
        for forbidden in ("as_of", "availability", "availability_day", "observed_at",
                          "cutoff", "created_at"):
            self.assertNotIn(forbidden, parameters)
        # 非空性对照：as_of 确实来自投影，而不是某个默认值。
        facts = _all_facts()
        for label, fact in facts.items():
            with self.subTest(owner=label):
                ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
                self.assertEqual(fact.availability_day, ref.as_of)


class FingerprintTests(_DbTestCase):
    """EXP-22 ~ EXP-23：指纹确定性，且随事实/修订变化。"""

    def test_EXP_22_content_fingerprint_is_deterministic(self):
        """EXP-22：同一内容必须得到同一个指纹，且是 sha256 十六进制。

        用 ``hash()`` / ``repr(object)`` / 内存地址 / 当前时间都会让"内容没变"被报成
        "内容变了"（或反之），冲突检测随之失去意义。
        """
        conn = _selection_conn()
        fact = _selection_fact(conn)
        again = ASEL.selection_candidate_fact(
            conn, int(fact.candidate_id), as_of="2026-09-21",
        )
        self.assertEqual(fact.content_fingerprint, again.content_fingerprint)
        for label, projection in _all_facts().items():
            with self.subTest(owner=label):
                fingerprint = projection.content_fingerprint
                self.assertEqual(64, len(fingerprint))
                self.assertTrue(all(ch in "0123456789abcdef" for ch in fingerprint))
                # 同进程内重算不得漂移（把时间/内存地址混进指纹会立刻违反）。
                rebuilt = dataclasses.replace(projection)
                self.assertEqual(fingerprint, rebuilt.content_fingerprint)

    def test_EXP_23_factual_or_revision_change_moves_the_fingerprint(self):
        """EXP-23：事实内容变了、或 revision 变了，指纹必须移动。

        两类都要覆盖：``updated_at`` 变化（revision 移动）与 payload 变化（内容移动）。
        只覆盖其中一类会让"同 identity 下内容被改写"静默通过。
        """
        conn = _selection_conn()
        fact = _selection_fact(conn)
        # revision 移动必须移动指纹。这里刻意只在**同一业务日内**换一个瞬间：如果同时改
        # ``availability_day``，断言会因为业务日变化而"假通过"，从而掩盖"指纹忽略了
        # revision identity"这个缺陷 —— 而那正是同一天内重评候选会被误判成"内容没变"的
        # 原因。EXP-23 因此用同一业务日的另一个时点隔离出 revision identity 的贡献。
        same_day = dataclasses.replace(fact, revision_at="2026-09-21T15:00:00+08:00")
        self.assertEqual("2026-09-21", same_day.availability_day)
        self.assertNotEqual(fact.revision_identity, same_day.revision_identity)
        self.assertNotEqual(
            fact.content_fingerprint, same_day.content_fingerprint,
            "同一业务日内的 revision 移动也必须移动指纹",
        )
        self.assertNotEqual(
            fact.content_fingerprint,
            dataclasses.replace(
                fact, revision_at="2026-09-22T09:10:00+08:00",
                availability_day="2026-09-22",
            ).content_fingerprint,
            "跨业务日的 revision 移动必须移动指纹",
        )
        self.assertNotEqual(
            fact.content_fingerprint,
            dataclasses.replace(
                fact, candidate_params_canonical='{"weights":{"flow":1.0}}',
            ).content_fingerprint,
            "事实内容变化必须移动指纹",
        )
        self.assertNotEqual(
            fact.content_fingerprint,
            dataclasses.replace(
                fact, lifecycle_status="applied",
            ).content_fingerprint,
            "lifecycle 变化也是事实内容变化",
        )
        self.assertNotEqual(
            fact.content_fingerprint,
            dataclasses.replace(fact, reason="different").content_fingerprint,
        )
        # 非空性对照：完全相同的输入必须得到同一个指纹。
        self.assertEqual(fact.content_fingerprint, dataclasses.replace(fact).content_fingerprint)


class AdapterPurityTests(unittest.TestCase):
    """EXP-24：adapter 不读 DB、不联网、不读墙钟。"""

    def test_EXP_24_adapter_has_no_db_network_or_clock_access(self):
        """EXP-24：import 扫描 + 调用点扫描，两条都必须是干净的。

        这一层只做 owner projection → ``ResearchEvidenceRef`` 的映射。重算 eligibility /
        fitness / promotion gate / risk reduction 都归各自 owner；碰 DB 或墙钟会让"同一条
        事实的引用"在不同环境或不同时刻变成不同的东西。
        """
        roots = _imported_roots(ADAPTER_FILE)
        offenders = sorted(roots & set(_FORBIDDEN_IMPORTS))
        self.assertEqual([], offenders, f"{ADAPTER_FILE} 不得 import：{offenders}")

        # 运行期对照：模块对象里不该有任何 DB / 网络 / 时钟模块。
        self.assertFalse(hasattr(ADAPTER, "sqlite3"))
        self.assertFalse(hasattr(ADAPTER, "datetime"))
        self.assertFalse(hasattr(ADAPTER, "time"))

        # 调用点级：不得有 now() / today() / connect() / urlopen() 这类入口。
        clock_or_io = {
            "now", "today", "utcnow", "time", "monotonic", "connect", "urlopen",
            "request", "getenv", "SystemRandom",
        }
        offenders = []
        for node in ast.walk(ast.parse(_source(ADAPTER_FILE))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None
            )
            if called in clock_or_io:
                offenders.append(called)
        self.assertEqual([], sorted(set(offenders)), "adapter 不得读墙钟或做 IO")

        # adapter 只 import 三个 owner + research core + typing。
        self.assertEqual(
            {"typing", "adaptive_risk", "adaptive_selection", "ai_research_contract",
             "learning_evaluation"},
            roots,
        )
        # research core 反过来**不** import 任何 adapter（依赖方向单向）。
        self.assertNotIn("ai_research_strategy_adapter", _imported_roots("ai_research_contract.py"))


class StrategyRegistryTests(unittest.TestCase):
    """EXP-25 ~ EXP-26：registry 双向一致，且 production 调用点为 0。"""

    def test_EXP_25_strategy_research_registry_and_real_factory_agree(self):
        """EXP-25：``strategy_research`` 在 registry 与真实 factory 之间双向一致。

        少一边就是"声明了却没有能力"或"有能力却没人登记"，两者都会让"谁能签发证据"不再受
        边界管辖。同时验证漂移检查函数**两个方向都会红**（否则它只是装饰）。
        """
        self.assertIn(ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH, ARC.SUPPORTED_OWNER_ADAPTERS)
        self.assertIn(STRATEGY_FACTORY, ADAPTER.__all__)
        self.assertTrue(callable(getattr(ADAPTER, STRATEGY_FACTORY)))
        # guard 的 registry 指向真实的 adapter + 符号。
        from test_ai_research_evidence_ownership_guard import EXPECTED_OWNER_FACTORIES
        self.assertEqual(
            ("ai_research_strategy_adapter", STRATEGY_FACTORY),
            EXPECTED_OWNER_FACTORIES[ARC.EVIDENCE_SOURCE_STRATEGY_RESEARCH],
        )
        # 归口表与三家 owner 闭集双向干净。
        self.assertEqual(
            [], ADAPTER._strategy_outcome_mapping_problems(
                ADAPTER._STRATEGY_OUTCOME_BY_STATUS, ADAPTER._OWNER_VERIFICATION_VOCABULARIES,
            ),
        )
        # 双向都会红：缺一个 owner 状态、多一个陌生状态，都必须报问题。
        missing = ADAPTER._strategy_outcome_mapping_problems(
            {AR.RISK_FACT_RECORDED: ARC.OWNER_OUTCOME_VERIFIED},
            ADAPTER._OWNER_VERIFICATION_VOCABULARIES,
        )
        self.assertTrue(missing, "缺失 owner 状态未被发现")
        self.assertTrue(any("adaptive_selection" in item for item in missing))
        drifty = dict(ADAPTER._STRATEGY_OUTCOME_BY_STATUS)
        drifty["something_no_owner_issues"] = ARC.OWNER_OUTCOME_VERIFIED
        problems = ADAPTER._strategy_outcome_mapping_problems(
            drifty, ADAPTER._OWNER_VERIFICATION_VOCABULARIES,
        )
        self.assertTrue(problems, "陌生状态未被发现")
        self.assertTrue(any("都不认识" in item for item in problems))
        # 重叠也会红 —— 状态词归属必须唯一，否则无法逐 owner 归因。
        overlap = ADAPTER._strategy_outcome_mapping_problems(
            ADAPTER._STRATEGY_OUTCOME_BY_STATUS,
            (("left", ("a", "b")), ("right", ("b", "c"))),
        )
        self.assertTrue(any("重叠" in item for item in overlap))

    def test_EXP_26_strategy_adapter_has_zero_production_callers(self):
        """EXP-26：production 里对 strategy factory 的调用点是 **0**（本轮预期）。

        B2C-6 交付的是**能力 + 契约 + 回归**；``deepseek_research`` 的 runtime 迁移属于后续
        convergence。因此"0"是预期状态而不是空转 —— 一旦有人在 runtime 里接上它，这条会红，
        提醒 reviewer 那属于 migration，需要单独一轮的 PIT / provenance 论证。
        """
        callers = {
            name for name in _production_modules() if _calls_in(name) and name != ADAPTER_FILE
        }
        offenders = []
        for name in _production_modules():
            if name == ADAPTER_FILE:
                continue
            for _module, line in _calls_in(name):
                offenders.append((name, line))
        self.assertEqual([], offenders, f"strategy adapter 出现 production 调用点：{offenders}")
        self.assertEqual(set(), callers)
        # 非空性对照：扫描器确实看得见这种调用形状（否则空集合断言会空转）。
        probe = ast.parse("import x\ndef f(p):\n    return x.evidence_ref_from_strategy_projection(p)\n")
        seen = [
            node.lineno for node in ast.walk(probe)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == STRATEGY_FACTORY
        ]
        self.assertEqual([3], seen)

    def test_EXP_26b_adapter_detail_does_not_copy_the_owner_payload(self):
        """EXP-26（补充）：``detail`` 只放最小审计信息，不复制 owner 的 factual payload。

        正确分层是：owner 投影 = 事实，``ResearchEvidenceRef`` = identity + 核验 + 指纹，
        ``InformationEvent.payload`` = 一次研究观测。把 params / metrics / blockers 抄进
        detail 会制造第二份事实副本，两份迟早漂移。
        """
        facts = _all_facts()
        for label, fact in facts.items():
            with self.subTest(owner=label):
                ref = ADAPTER.evidence_ref_from_strategy_projection(fact)
                self.assertEqual(
                    {"content_fingerprint", "contract_version", "record_kind"},
                    set(ref.detail),
                )
                self.assertEqual(fact.content_fingerprint, ref.detail["content_fingerprint"])
                self.assertEqual(fact.record_kind, ref.detail["record_kind"])
                # owner 的 factual payload 一个都不在 detail 里。
                for leaked in ("baseline_params", "candidate_params", "coverage", "holdout",
                               "evaluation_blockers", "per_date_ic", "risk_reduction_pct"):
                    self.assertNotIn(leaked, ref.detail)


class DeferredLegacyRuntimeTests(unittest.TestCase):
    """EXP-27 ~ EXP-28：非结构化判定的 legacy runtime 仍然在，且明确记为 deferred。"""

    def test_EXP_27_candidate_challenge_runtime_is_unchanged_and_deferred(self):
        """EXP-27：``candidate_challenge`` 的 legacy 直读 runtime **仍然存在**。

        B2C-6 **不**迁移它（那属于后续 convergence）。把它删掉会假装"已经迁移"，把 PIT /
        provenance 的论证留白；因此本轮断言它逐字还在，并且仍然走 legacy 直读。
        """
        self.assertIn("candidate_challenge", DR.COLLECTORS)
        self.assertIs(DR._candidate_evidence, DR.COLLECTORS["candidate_challenge"])
        source = _source("deepseek_research.py")
        self.assertIn("_candidate_evidence", source)
        # legacy 直读仍然存在（这就是"未迁移"的可执行证据）。
        self.assertIn("FROM adaptive_risk_candidates", source)
        self.assertIn("FROM adaptive_selection_candidates", source)
        # 但 legacy collector **不**经 adapter：它不 import strategy adapter 的任何符号。
        self.assertNotIn("ai_research_strategy_adapter", _imported_roots("deepseek_research.py"))

    def test_EXP_28_overfit_watch_runtime_is_unchanged_and_deferred(self):
        """EXP-28：``overfit_watch`` 的 legacy 直读 runtime 同样还在、同样 deferred。

        它的输入里有 ``adaptive_rewards`` / ``adaptive_alpha_candidates`` —— 这两张表在本轮
        被判定为 **NOT EVIDENCE**（无可证可用性 / identity 会被 DELETE+INSERT 重建），因此
        它们**没有** typed 投影，也**不得**被硬套成 ``ResearchEvidenceRef``。
        """
        self.assertIn("overfit_watch", DR.COLLECTORS)
        self.assertIs(DR._overfit_evidence, DR.COLLECTORS["overfit_watch"])
        source = _source("deepseek_research.py")
        self.assertIn("FROM adaptive_rewards", source)
        self.assertIn("FROM adaptive_alpha_candidates", source)
        # 这两张表**没有**得到 typed owner fact contract —— 本轮刻意不伪造 identity。
        # 断言方式刻意选"发布了几个投影类型"而不是"哪个模块提到了哪张表"：owner 读自己的
        # 其它列（例如 ``adaptive_risk._evidence`` 读 ``adaptive_rewards``）是合法的，
        # 真正的不变量是**没有任何一个投影承载它们**。
        projections = (
            AR.AdaptiveRiskFactProjection,
            ASEL.AdaptiveSelectionFactProjection,
            LE.ExperimentEvaluationProjection,
        )
        self.assertEqual(3, len(projections))
        for projection in projections:
            with self.subTest(projection=projection.__name__):
                lowered = projection.__name__.lower()
                self.assertNotIn("alpha", lowered)
                self.assertNotIn("reward", lowered)
                self.assertNotIn("alpha", projection.__dataclass_fields__)
                self.assertNotIn("reward", projection.__dataclass_fields__)
        # 归口表精确等于 3 owner × 2 状态，不多不少。
        self.assertEqual(6, len(ADAPTER._STRATEGY_OUTCOME_BY_STATUS))
        for key in ADAPTER._STRATEGY_OUTCOME_BY_STATUS:
            with self.subTest(status=key):
                self.assertTrue(
                    key.startswith(("risk_candidate", "selection_candidate",
                                    "experiment_evaluation")),
                    f"{key!r} 不属于三个已批准的 owner record kind",
                )
        # 三个 owner 的 record kind 里也没有 alpha / reward。
        for record_kind in (AR.RISK_CANDIDATE_RECORD_KIND, ASEL.SELECTION_CANDIDATE_RECORD_KIND,
                            LE.EXPERIMENT_EVALUATION_RECORD_KIND):
            self.assertNotIn("alpha", record_kind)
            self.assertNotIn("reward", record_kind)


if __name__ == "__main__":
    unittest.main()
