# -*- coding: utf-8 -*-
"""R27-B2C-5 —— news owner typed evidence adapter 的**永久回归**（research 侧）。

被测对象是唯一那一条 owner→research 接缝：
``ai_research_news_adapter.evidence_ref_from_news_projection``。owner 侧的 typed fact
contract 在 ``test_news_fact_contract``。

本文件钉住的不变量：

```text
NEWS-03/04  source_type = news（复用 ARC.EVIDENCE_SOURCE_NEWS，不新建来源）；
            InformationEvent.kind 自动是 news_observed
NEWS-09/10  evidence_grade A / B / C **都不能**让任何事实变成 owner verified
NEWS-12     major 的 single_source_linked / unverified → unverified（**不是** verified）
NEWS-17/18  adapter 只接受真正的 NewsFactProjection：dict / 子类 / duck type 一律拒绝
NEWS-19/20  内容指纹确定性；identity / PIT / 核验相关变化必须改变指纹
NEWS-21     InformationEvent.payload 不能覆盖由 evidence_ref 派生的核验
NEWS-22     未来 news 证据不能进入更早的 InformationEvent（契约层 look-ahead 守卫）
NEWS-25     SUPPORTED_OWNER_ADAPTERS / owner→factory registry / 唯一批准签发调用者三者
            与真实代码双向一致
NEWS-26     news adapter 的 production 调用点 = 0（B2C-5 的预期状态，且已被文档记录）
NEWS-31/32  签名只接受 owner 投影：调用方不能传 source_id / as_of / verification
NEWS-33     adapter 是纯的：不 import DB / 网络 / 时钟，只 import 契约与 owner 投影模块
```

全部离线：临时 SQLite 账本 + owner 自己的 public read + owner 的私有签发口（合成只差一个
维度的投影）。**不连真实库、不读墙钟、不联网。**

    python -m unittest test_ai_research_news_adapter
"""
from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_research_contract as ARC  # noqa: E402
import ai_research_news_adapter as ANA  # noqa: E402
import news_learning as NL  # noqa: E402
import test_ai_research_evidence_ownership_guard as GUARD  # noqa: E402

ADAPTER_MODULE = "ai_research_news_adapter.py"

ASOF_DAY = "2026-09-20"
INGEST_DAY = "2026-09-21"
LATER_DAY = "2026-09-22"
FIRST_SEEN = f"{INGEST_DAY}T09:00:00+08:00"
CREATED_AT = f"{INGEST_DAY}T09:00:01+08:00"
CODE = "600000"
LINK = "https://example.invalid/a"

#: adapter 允许 import 的根模块 —— 纯接缝：无 DB、无网络、无时钟、无文件系统。
ALLOWED_ADAPTER_IMPORTS = {
    "__future__", "hashlib", "json", "typing", "ai_research_contract", "news_learning",
}


def _source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


class _StripDocstrings(ast.NodeTransformer):
    """把 docstring 整条剥掉 —— 只留**会执行**的代码。"""

    def visit_Module(self, node):  # noqa: N802 - ast API
        self.generic_visit(node)
        if node.body and isinstance(node.body[0], ast.Expr) and \
                isinstance(node.body[0].value, ast.Constant):
            node.body = node.body[1:]
        return node

    def _strip(self, node):
        self.generic_visit(node)
        if node.body and isinstance(node.body[0], ast.Expr) and \
                isinstance(node.body[0].value, ast.Constant):
            node.body = node.body[1:]
        return node

    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def _executable_module(name: str) -> str:
    tree = _StripDocstrings().visit(ast.parse(_source(name)))
    return ast.unparse(tree)


def _imported_roots(name: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(_source(name))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    return roots


def _production_modules() -> list[str]:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _projection_fields(**overrides) -> dict:
    """一条**合法的** news 投影的全部字段；用 override 精确改变一个维度。"""
    fields = {
        "version": NL.NEWS_FACT_CONTRACT_VERSION,
        "record_kind": NL.NEWS_RECORD_KIND_EVENT,
        "identity": "EK-1",
        "canonical_hash": "CH-1",
        "code": CODE,
        "title": "回购公告",
        "source_name": "巨潮资讯",
        "source_type": "announcement",
        "source_url": LINK,
        "article_id": None,
        "published_at": f"{ASOF_DAY}T10:00:00+08:00",
        "first_seen_at": FIRST_SEEN,
        "availability_day": "",
        "event_type": "buyback",
        "evidence_grade": "C",
        "owner_verification_status": NL.NEWS_OWNER_SINGLE_SOURCE,
        "ledger_verification_status": None,
        "significance_score": None,
        "themes": (),
        "affected_industries": (),
    }
    fields.update(overrides)
    return fields


def _synthetic(**overrides):
    """合成一条投影，**只**改变被点名的维度。

    走 owner 自己的私有签发口 —— 与 ``test_ai_research_portfolio_adapter`` 同一手法：
    ``NewsFactProjection`` 没有公开构造器，私有签发口是"只改一个维度"的唯一途径。
    """
    return NL._issue_news_fact_projection(**_projection_fields(**overrides))


def _major_synthetic(*, ledger_verification_status="single_source_linked", **overrides):
    owner_status = NL.NEWS_OWNER_SINGLE_SOURCE
    if ledger_verification_status == "unverified":
        owner_status = NL.NEWS_OWNER_UNVERIFIED
    return _synthetic(
        record_kind=NL.NEWS_RECORD_KIND_MAJOR_EVENT,
        identity="MK-1", code=None, source_type="news_aggregator",
        significance_score=0.9,
        owner_verification_status=owner_status,
        ledger_verification_status=ledger_verification_status,
        **overrides,
    )


def _ref(projection):
    return ANA.evidence_ref_from_news_projection(projection)


class NewsAdapterTests(unittest.TestCase):
    """news owner → research typed evidence 的唯一接缝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "adaptive_learning.sqlite3")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        NL.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _event(self, *, event_key="EK-1", source_url=LINK, article_id=None,
               first_seen_at=FIRST_SEEN, created_at=CREATED_AT, evidence_grade="C",
               published_at=f"{ASOF_DAY}T10:00:00+08:00"):
        self.conn.execute(
            """INSERT INTO news_events(event_key,canonical_hash,article_id,code,title,
                   source_name,source_type,source_url,evidence_grade,published_at,
                   first_seen_at,event_type,expected_direction,severity,parse_rule,
                   raw_payload,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_key, f"H-{event_key}", article_id, CODE, "回购公告", "巨潮资讯",
             "announcement", source_url, evidence_grade, published_at, first_seen_at,
             "buyback", 1, 0.5, "rule", "{}", created_at, created_at),
        )
        return event_key

    # ---------- NEWS-03 / NEWS-04：source_type 与 event kind ----------

    def test_NEWS_03_source_type_is_the_existing_news_source(self):
        """adapter 复用 ``ARC.EVIDENCE_SOURCE_NEWS``，不新建第二套来源类型。"""
        self._event(event_key="EK-E2E")
        fact = NL.news_fact_projections(self.conn, as_of=INGEST_DAY)[0]
        ref = _ref(fact)

        self.assertEqual(ref.source_type, ARC.EVIDENCE_SOURCE_NEWS)
        self.assertEqual(ref.source_type, "news")
        self.assertEqual(ref.source_id, f"{NL.NEWS_RECORD_KIND_EVENT}|EK-E2E")
        # 细分在 detail 里，不在 source_type 里。
        self.assertEqual(ref.detail["record_kind"], NL.NEWS_RECORD_KIND_EVENT)
        self.assertNotIn("news_event", ARC.EVIDENCE_SOURCE_TYPES)
        self.assertNotIn("major_news", ARC.EVIDENCE_SOURCE_TYPES)
        self.assertNotIn("announcement_news", ARC.EVIDENCE_SOURCE_TYPES)

        major = _major_synthetic()
        self.assertEqual(_ref(major).source_type, ARC.EVIDENCE_SOURCE_NEWS)
        self.assertEqual(_ref(major).source_id,
                         f"{NL.NEWS_RECORD_KIND_MAJOR_EVENT}|MK-1")

    def test_NEWS_04_information_event_kind_is_news_observed(self):
        """``InformationEvent.kind`` 是派生只读属性 —— 自动仍是 ``news_observed``。"""
        self._event(event_key="EK-KIND")
        fact = NL.news_fact_projections(self.conn, as_of=INGEST_DAY)[0]
        ref = _ref(fact)
        event = ARC.InformationEvent(as_of=INGEST_DAY, source="news_test", evidence_ref=ref)

        self.assertEqual(event.kind, ARC.EVENT_NEWS_OBSERVED)
        self.assertEqual(event.kind, "news_observed")
        self.assertEqual(event.evidence_id, ref.source_id)
        # verification_method 是 market-only 维度：对 news 是明确的"不适用"。
        self.assertIsNone(event.verification_method)

    # ---------- NEWS-09 / NEWS-10：grade 不是核验 ----------

    def test_NEWS_09_evidence_grade_a_never_becomes_owner_verified(self):
        """``grade="A"`` 是来源可追溯性分级，**不是**核验通过。"""
        for source_url in (LINK, None):
            with self.subTest(source_url=source_url):
                projection = _synthetic(evidence_grade="A", source_url=source_url,
                                        article_id="ART-1" if source_url is None else None)
                ref = _ref(projection)
                self.assertFalse(ref.is_verified)
                self.assertNotEqual(ref.owner_verification.outcome,
                                    ARC.OWNER_OUTCOME_VERIFIED)
                self.assertEqual(ref.verification_attributes["evidence_grade"], "A")

        major = _major_synthetic(evidence_grade="A")
        self.assertFalse(_ref(major).is_verified)

    def test_NEWS_10_evidence_grade_b_and_c_never_become_owner_verified(self):
        for grade in ("B", "C", "D", "a", "", "unknown"):
            with self.subTest(evidence_grade=grade):
                projection = _synthetic(evidence_grade=grade or "C")
                ref = _ref(projection)
                self.assertFalse(ref.is_verified)
                self.assertNotEqual(ref.owner_verification.outcome,
                                    ARC.OWNER_OUTCOME_VERIFIED)

    # ---------- NEWS-12：single_source_linked ≠ verified ----------

    def test_NEWS_12_single_source_linked_maps_to_unverified_not_verified(self):
        """「只有一条来源且可追踪」**绝不**等于"已核验"。"""
        linked = _ref(_major_synthetic(ledger_verification_status="single_source_linked"))
        self.assertEqual(linked.owner_verification.outcome, ARC.OWNER_OUTCOME_UNVERIFIED)
        self.assertFalse(linked.is_verified)
        self.assertEqual(linked.verification, NL.NEWS_OWNER_SINGLE_SOURCE)
        self.assertEqual(linked.verification_attributes["ledger_verification_status"],
                         "single_source_linked")

        plain = _ref(_major_synthetic(ledger_verification_status="unverified"))
        self.assertEqual(plain.owner_verification.outcome, ARC.OWNER_OUTCOME_UNVERIFIED)
        self.assertFalse(plain.is_verified)
        self.assertEqual(plain.verification, NL.NEWS_OWNER_UNVERIFIED)

        # 不可追溯 → 核验过程无从下手：与"未通过核验"必须是**不同**的结论。
        untraceable = _ref(_synthetic(
            source_url=None, article_id=None,
            owner_verification_status=NL.NEWS_OWNER_SOURCE_UNUSABLE,
        ))
        self.assertEqual(untraceable.owner_verification.outcome,
                         ARC.OWNER_OUTCOME_SOURCE_UNUSABLE)
        self.assertTrue(untraceable.owner_verification.source_unusable)

        # 闭集里没有 verified：今天不存在任何能把 news 事实归到 verified 的路径。
        self.assertEqual(
            {ARC.OWNER_OUTCOME_UNVERIFIED, ARC.OWNER_OUTCOME_SOURCE_UNUSABLE},
            set(ANA._NEWS_OUTCOME_BY_STATUS.values()),
        )

    # ---------- NEWS-17 / NEWS-18：exact-type 输入强制 ----------

    def test_NEWS_17_adapter_rejects_dict_and_any_mapping(self):
        with self.assertRaises(TypeError):
            ANA.evidence_ref_from_news_projection({})
        with self.assertRaises(TypeError):
            ANA.evidence_ref_from_news_projection(_projection_fields())
        for fake in (None, "news", 42, [], (), object()):
            with self.subTest(fake=type(fake).__name__):
                self.assertRaises(TypeError,
                                  ANA.evidence_ref_from_news_projection, fake)
        # 非空性：真投影必须**能**通过 —— 否则上面的拒绝可能只是函数一律抛错。
        self.assertIsInstance(_ref(_synthetic()), ARC.ResearchEvidenceRef)

    def test_NEWS_18_adapter_rejects_subclass_and_duck_typed_projections(self):
        """子类与 duck-typed 伪对象（哪怕字段一模一样）都不算 owner 投影。"""

        class Subclass(NL.NewsFactProjection):
            pass

        projection = _synthetic()
        sub = object.__new__(Subclass)
        for name, value in _projection_fields().items():
            object.__setattr__(sub, name, value)
        sub.__post_init__()
        self.assertIsInstance(sub, NL.NewsFactProjection)

        duck = SimpleNamespace(**_projection_fields())
        duck.owner_verification_status = NL.NEWS_OWNER_SINGLE_SOURCE
        duck.availability_day = INGEST_DAY
        duck.identity = "FAKE"

        for fake in (sub, duck):
            with self.subTest(fake=type(fake).__name__):
                with self.assertRaises(TypeError) as ctx:
                    ANA.evidence_ref_from_news_projection(fake)
                self.assertIn("NewsFactProjection", str(ctx.exception))
        # 对照：真正的投影通过，证明拒绝来自类型校验而不是别的东西。
        self.assertEqual(_ref(projection).source_id,
                         f"{NL.NEWS_RECORD_KIND_EVENT}|EK-1")

    # ---------- NEWS-19 / NEWS-20：指纹确定性与敏感性 ----------

    def test_NEWS_19_content_fingerprint_is_deterministic(self):
        first = _ref(_synthetic()).detail["content_fingerprint"]
        second = _ref(_synthetic()).detail["content_fingerprint"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(first, first.lower())
        self.assertTrue(all(char in "0123456789abcdef" for char in first))
        # 同一 identity 下内容不同 ⇒ 指纹必须不同（否则冲突检测失去意义）。
        changed_title = _ref(_synthetic(title="另一条公告")).detail["content_fingerprint"]
        self.assertNotEqual(first, changed_title)

    def test_NEWS_20_identity_pit_and_verification_changes_move_the_fingerprint(self):
        reference = _ref(_synthetic()).detail["content_fingerprint"]

        for label, overrides in (
            ("identity", {"identity": "EK-2"}),
            ("canonical_hash", {"canonical_hash": "CH-2"}),
            ("first_seen_at", {"first_seen_at": f"{LATER_DAY}T09:00:00+08:00"}),
            ("published_at", {"published_at": None}),
            ("source_url", {"source_url": "https://example.invalid/b"}),
            ("source_name", {"source_name": "另一家媒体"}),
            ("event_type", {"event_type": "shareholder_increase"}),
            ("evidence_grade", {"evidence_grade": "A"}),
            ("owner_verification_status", {
                "source_url": None, "article_id": None,
                "owner_verification_status": NL.NEWS_OWNER_SOURCE_UNUSABLE,
            }),
        ):
            with self.subTest(changed=label):
                self.assertIn(label, overrides)
                mutated = _ref(_synthetic(**overrides)).detail["content_fingerprint"]
                self.assertNotEqual(reference, mutated,
                                    f"{label} 变化没有改变内容指纹")

        major_reference = _ref(_major_synthetic()).detail["content_fingerprint"]
        major_changed = _ref(_major_synthetic(
            ledger_verification_status="unverified")).detail["content_fingerprint"]
        self.assertNotEqual(major_reference, major_changed,
                            "ledger 核验状态变化没有改变内容指纹")

    # ---------- NEWS-21 / NEWS-22：事件层不得改写核验，也不得 look-ahead ----------

    def test_NEWS_21_information_event_payload_cannot_override_verification(self):
        ref = _ref(_synthetic())
        event = ARC.InformationEvent(
            as_of=INGEST_DAY, source="news_test", evidence_ref=ref,
            payload={
                "verification": "verified",
                "is_verified": True,
                "verification_status": "multi_source_verified",
                "confidence": 0.99,
            },
        )
        self.assertEqual(event.verification, ref.verification)
        self.assertEqual(event.verification, NL.NEWS_OWNER_SINGLE_SOURCE)
        self.assertFalse(event.is_verified)
        self.assertEqual(event.projection()["verification"], ref.verification)
        self.assertEqual(event.verification_attributes, ref.verification_attributes)

    def test_NEWS_22_future_news_evidence_cannot_enter_an_earlier_event(self):
        """``InformationEvent`` 的 PIT 守卫在契约层：引用了未来事实即拒绝。"""
        ref = _ref(_synthetic(first_seen_at=f"{LATER_DAY}T09:00:00+08:00"))
        self.assertEqual(ref.as_of, LATER_DAY)
        with self.assertRaises(ValueError):
            ARC.InformationEvent(as_of=ASOF_DAY, source="news_test", evidence_ref=ref)
        # 对照：as_of 与证据同日就合法。
        event = ARC.InformationEvent(as_of=LATER_DAY, source="news_test", evidence_ref=ref)
        self.assertEqual(event.as_of, LATER_DAY)

    # ---------- NEWS-25：registry 三方双向一致 ----------

    def test_NEWS_25_owner_factory_registries_agree_with_the_news_adapter(self):
        """``SUPPORTED_OWNER_ADAPTERS`` ↔ owner→factory 映射 ↔ 唯一签发调用者，三者一致。"""
        self.assertIn(ARC.EVIDENCE_SOURCE_NEWS, ARC.SUPPORTED_OWNER_ADAPTERS)
        self.assertEqual(
            GUARD.EXPECTED_OWNER_FACTORIES["news"],
            ("ai_research_news_adapter", "evidence_ref_from_news_projection"),
        )
        self.assertEqual(
            [], GUARD._registry_problems(ARC.SUPPORTED_OWNER_ADAPTERS,
                                        GUARD.EXPECTED_OWNER_FACTORIES),
        )
        # 第三个条件：模块**真的**在 __all__ 里导出这个可调用符号。
        self.assertIn("evidence_ref_from_news_projection", ANA.__all__)
        self.assertTrue(GUARD._module_exports_factory(
            "ai_research_news_adapter", "evidence_ref_from_news_projection"))
        # 唯一批准签发调用者：news 的只能是这一个 (module, function) 对。
        self.assertIn(("ai_research_news_adapter.py", "evidence_ref_from_news_projection"),
                      GUARD.APPROVED_ISSUER_CALLERS)
        self.assertEqual(
            GUARD._issuer_caller_set(), GUARD.APPROVED_ISSUER_CALLERS,
            "私有签发口的实际调用者集合与 allowlist 漂移",
        )
        # 非空性：owner（news_learning）自己**不**调用私有签发口。
        self.assertNotIn(("news_learning.py", "news_fact_projections"),
                         GUARD.APPROVED_ISSUER_CALLERS)

    # ---------- NEWS-26：production 调用点 = 0（预期状态） ----------

    def test_NEWS_26_news_adapter_has_zero_production_callers(self):
        """B2C-5 的交付物是能力 + 契约 + 回归：runtime 迁移属于后续 convergence。

        判据是**真实 import**（AST），不是文本提及：契约的 docstring / 报错文案会**点名**
        adapter（"请使用该 owner 已批准的 factory"），那是文档，不是依赖。
        """
        importers = [
            name for name in _production_modules()
            if "ai_research_news_adapter" in _imported_roots(name)
        ]
        self.assertEqual([], importers,
                         f"news adapter 出现了 production 调用点：{importers}")
        # 非空性：扫描器真的在扫生产模块（否则上面可能是空集空转）。
        self.assertIn("ai_research_contract.py", _production_modules())
        self.assertIn("ai_research_contract", _imported_roots(ADAPTER_MODULE))
        self.assertIn("ai_research_contract", _imported_roots("deepseek_research.py"))

        # 文档必须**明说** legacy runtime 仍然存在且被推迟，不得声称已迁移。
        doc = ANA.__doc__ or ""
        self.assertIn("OPEN / REQUIRED", doc)
        self.assertIn("deepseek_research._event_evidence", doc)
        self.assertIn("DEFERRED", doc)
        self.assertIn("physical database origin", doc)
        self.assertIn("CLOSED", doc)
        self.assertNotIn("news runtime fully migrated", doc)
        self.assertNotIn("event_evidence runtime = COMPLETE", doc)

    # ---------- NEWS-31 / NEWS-32：签名只接受 owner 投影 ----------

    def test_NEWS_31_adapter_signature_takes_only_the_owner_projection(self):
        signature = inspect.signature(ANA.evidence_ref_from_news_projection)
        self.assertEqual(list(signature.parameters), ["projection"])
        parameter = signature.parameters["projection"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertNotIn(parameter.kind, (inspect.Parameter.VAR_POSITIONAL,
                                         inspect.Parameter.VAR_KEYWORD))
        self.assertEqual(tuple(ANA.__all__), ("evidence_ref_from_news_projection",))

    def test_NEWS_32_caller_cannot_name_the_identity_or_the_as_of(self):
        projection = _synthetic()
        for kwargs in ({"source_id": "FACT_A"}, {"as_of": ASOF_DAY},
                       {"verification": "verified"}, {"outcome": "verified"},
                       {"evidence_grade": "A"}, {"status": "verified"}):
            with self.subTest(kwarg=sorted(kwargs)):
                with self.assertRaises(TypeError):
                    ANA.evidence_ref_from_news_projection(projection, **kwargs)

        # identity 与 as_of 永远由 owner 投影派生，不受任何其它维度左右。
        for overrides in ({}, {"title": "x"}, {"evidence_grade": "A"},
                          {"source_name": "别的来源"}):
            with self.subTest(overrides=sorted(overrides)):
                ref = _ref(_synthetic(**overrides))
                self.assertEqual(ref.source_id,
                                 f"{NL.NEWS_RECORD_KIND_EVENT}|EK-1")
                self.assertEqual(ref.as_of, FIRST_SEEN[:10])

    # ---------- NEWS-33：adapter 是纯的 ----------

    def test_NEWS_33_adapter_is_pure_no_db_no_network_no_clock(self):
        """唯一接缝不允许有 IO：不 import DB / 网络 / 时钟 / 文件系统，也不发 SQL。"""
        self.assertEqual(
            ALLOWED_ADAPTER_IMPORTS, _imported_roots(ADAPTER_MODULE),
            f"{ADAPTER_MODULE} 的 import 集合发生变化",
        )
        executable = _executable_module(ADAPTER_MODULE)
        for forbidden in ("sqlite3", "execute(", "executemany(", "executescript(",
                          "commit(", "rollback(", "open(", "datetime", "time(",
                          "now(", "today(", "environ", "getenv", "data_fetcher",
                          "urllib", "urlopen", "requests", "httpx", "socket"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, executable,
                                 f"{ADAPTER_MODULE} 的可执行代码里出现了 {forbidden}")

        # 依赖方向：owner **不** import research；契约 **不** import adapter / owner。
        self.assertEqual(set(), _imported_roots("news_learning.py")
                         & {"ai_research_contract", "ai_research_news_adapter"})
        self.assertEqual(set(), _imported_roots("ai_research_contract.py")
                         & {"news_learning", "ai_research_news_adapter"})
        self.assertIn("ai_research_contract", _imported_roots(ADAPTER_MODULE))
        self.assertIn("news_learning", _imported_roots(ADAPTER_MODULE))

    # ---------- NEWS-34：availability_day 必须按 owner 时区归一 ----------

    def test_NEWS_34_availability_day_is_normalized_to_the_owner_timezone(self):
        """``first_seen_at`` 允许带任意 offset —— 业务日必须按 **owner 时区**算。

        owner 的日历语义是 Asia/Shanghai：read 的日边界是 ``as_of + 23:59:59+08:00``。
        如果 projection 的 ``availability_day`` 改照抄原始 offset 的 ``.date()``，两边就会
        用**两套日期口径**：一个跨过 UTC 午夜的 instant 会让 ref 声明一个比真实可用日**更早**
        的业务日，于是 ``InformationEvent`` 的 look-ahead guard 被绕过 —— 那正是 B2C-5 最
        核心的 PIT invariant。
        """
        # 2026-09-20T16:30+00:00 == 上海 2026-09-21 00:30 ⇒ 业务日是 9/21，不是 9/20。
        self._event(event_key="EK-UTC-MIDNIGHT", first_seen_at="2026-09-20T16:30:00+00:00")
        # 反向对照：2026-09-21T01:00+14:00 == 上海 2026-09-20 19:00 ⇒ 业务日是 9/20。
        self._event(event_key="EK-FAR-EAST", first_seen_at="2026-09-21T01:00:00+14:00")

        def identities(as_of):
            return {f.identity for f in NL.news_fact_projections(self.conn, as_of=as_of)}

        # 日边界本来就比较 aware instant，因此 filter 两个方向都正确（这是对照，不是被测点）。
        self.assertNotIn("EK-UTC-MIDNIGHT", identities(ASOF_DAY))
        self.assertIn("EK-UTC-MIDNIGHT", identities(INGEST_DAY))
        self.assertIn("EK-FAR-EAST", identities(ASOF_DAY))

        facts = {f.identity: f for f in NL.news_fact_projections(self.conn, as_of=INGEST_DAY)}
        # 被测点：业务日按 owner 时区归一，而不是原始 offset 的日期。
        self.assertEqual(facts["EK-UTC-MIDNIGHT"].availability_day, INGEST_DAY)
        self.assertEqual(facts["EK-FAR-EAST"].availability_day, ASOF_DAY)
        # exact timestamp 逐字保留 —— first_seen_at 与 availability_day 不得互换。
        self.assertEqual(facts["EK-UTC-MIDNIGHT"].first_seen_at, "2026-09-20T16:30:00+00:00")

        # 跨到 adapter：ref.as_of 必须是归一后的业务日，否则下游会拿到一个更早的日期。
        ref = _ref(facts["EK-UTC-MIDNIGHT"])
        self.assertEqual(ref.as_of, INGEST_DAY)
        event = ARC.InformationEvent(as_of=INGEST_DAY, source="news_test", evidence_ref=ref)
        self.assertEqual(event.as_of, INGEST_DAY)
        # 而且不得被塞进一个更早的事件里（否则 look-ahead guard 就形同虚设）。
        with self.assertRaises(ValueError):
            ARC.InformationEvent(as_of=ASOF_DAY, source="news_test", evidence_ref=ref)


if __name__ == "__main__":
    unittest.main()
