# -*- coding: utf-8 -*-
"""R27-B2C-5 —— news owner typed fact contract 的**永久回归**（owner 侧）。

本文件只测 owner（``news_learning``）：typed projection 与它的 public typed 历史读
``news_fact_projections``。research 侧的 adapter 归口在
``test_ai_research_news_adapter``。

四条不变量，每条都必须能被**具体的一条**测试钉住：

```text
NEWS-01/02  identity 来自 owner 的 durable event_key（不是调用方命名）
NEWS-05/06/07  historical availability 只由 first_seen_at 决定；
               published_at 永远不能把可用性提前
NEWS-08     first_seen_at 缺失 / 畸形 / naive → fail closed，且**没有**任何 fallback
NEWS-11/13  grade 不是核验；未审计过的 verification_status 不得默认成 unverified
NEWS-14/15  source reputation / candidate-link confidence 不在读路径里
NEWS-16     raw_payload 不能覆盖 normalized 列
NEWS-23/24  typed read 不联网；历史缺数据不触发 live fetch / backfill
NEWS-27     market_major_events 的 verification_status writer 闭集是**审计产物**
NEWS-28/29  first_seen_at 不可被后一次抓取覆盖；typed read 读 durable 行
```

全部离线：临时 SQLite 账本 + owner 自己的 public read。**不连真实库、不读墙钟、不联网。**

    python -m unittest test_news_fact_contract
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import news_learning as NL  # noqa: E402

MODULE_FILE = "news_learning.py"

#: 固定业务日 —— 与本机时钟无关，测试因此完全确定。
PUBLISHED_DAY = "2026-09-19"
ASOF_BEFORE_INGEST = "2026-09-20"
INGEST_DAY = "2026-09-21"
LATER_DAY = "2026-09-22"

PUBLISHED_AT = f"{PUBLISHED_DAY}T10:00:00+08:00"
FIRST_SEEN = f"{INGEST_DAY}T09:00:00+08:00"
CREATED_AT = f"{INGEST_DAY}T09:00:01+08:00"

CODE = "600000"
LINK = "https://example.invalid/a"

#: 读路径**绝不**允许出现的表 —— 它们分别是来源信誉、候选相关性映射、学习元数据。
FORBIDDEN_READ_TABLES = (
    "news_source_reputation",
    "market_event_candidate_links",
    "news_factor_versions",
    "news_event_outcomes",
    "news_effectiveness",
    "news_learning_runs",
    "news_candidate_snapshots",
)


def _source(name: str = MODULE_FILE) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _tree(name: str = MODULE_FILE) -> ast.Module:
    return ast.parse(_source(name))


def _string_literals(node: ast.AST) -> set[str]:
    return {
        child.value for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _insert_statement(node: ast.Call) -> str | None:
    """``conn.execute(<literal>, …)`` 的第一参数（必须是字符串字面量）。"""
    first = node.args[0] if node.args else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _execute_writers() -> list[tuple[ast.Call, str]]:
    found: list[tuple[ast.Call, str]] = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "execute":
            continue
        statement = _insert_statement(node)
        if statement is not None and len(node.args) >= 2:
            found.append((node, statement))
    return found


def _insert_columns(statement: str) -> list[str]:
    head = statement.split("VALUES", 1)[0]
    inner = head[head.index("(") + 1:head.rindex(")")]
    return [part.strip() for part in inner.split(",") if part.strip()]


def _major_event_writer() -> tuple[ast.Call, str, list[str], int, ast.AST]:
    """market_major_events 的 INSERT writer 及其 tuple 值的实际形状。"""
    for node, statement in _execute_writers():
        if "INTO market_major_events(" not in statement:
            continue
        columns = _insert_columns(statement)
        values = node.args[1]
        if not isinstance(values, ast.Tuple):
            raise AssertionError("market_major_events writer 的取值不是 tuple")
        return node, statement, columns, columns.index("verification_status"), values
    raise AssertionError("找不到 market_major_events 的 INSERT writer")


def _news_event_writer_columns() -> list[str]:
    for _node, statement in _execute_writers():
        if "INTO news_events(" in statement:
            return _insert_columns(statement)
    raise AssertionError("找不到 news_events 的 INSERT writer")


def _projection_fields(**overrides) -> dict:
    """一条**合法的** news_event 投影的全部字段；用 override 精确改变一个维度。"""
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
        "published_at": PUBLISHED_AT,
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


def _projection(**overrides):
    return NL._issue_news_fact_projection(**_projection_fields(**overrides))


class _RecordingConnection:
    """记录每一条被执行的 SQL —— 用来证明读路径**只**发 SELECT、且**只**碰两张事件账本。"""

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        return self._conn.execute(sql, *args)


class _FetchSpy:
    """把 data_fetcher 的网络入口换成记录器 —— 任何一次调用都必须让测试 RED。"""

    def __init__(self, module):
        self.calls: list[str] = []
        self._patch = []
        self._module = module

    def __enter__(self):
        from unittest import mock

        for name in ("fetch_company_announcements", "fetch_fast_news",
                     "load_cached_kline", "fetch_kline"):
            patcher = mock.patch.object(
                self._module, name,
                side_effect=self._make(name),
                create=True,
            )
            patcher.start()
            self._patch.append(patcher)
        return self

    def _make(self, name):
        def _stub(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"typed read 触发了 ingestion fetch：{name}")
        return _stub

    def __exit__(self, *exc):
        for patcher in reversed(self._patch):
            patcher.stop()
        return False


@contextmanager
def _no_network():
    with _FetchSpy(NL.dfc) as spy:
        yield spy


class NewsFactContractTests(unittest.TestCase):
    """owner 侧 typed fact contract。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "adaptive_learning.sqlite3")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        NL.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---------- fixtures ----------

    def _event(self, *, event_key="EK-1", code=CODE, title="回购公告", source_name="巨潮资讯",
               source_type="announcement", source_url=LINK, article_id=None,
               evidence_grade="C", published_at=PUBLISHED_AT, first_seen_at=FIRST_SEEN,
               created_at=CREATED_AT, event_type="buyback", raw_payload="{}"):
        self.conn.execute(
            """INSERT INTO news_events(event_key,canonical_hash,article_id,code,title,
                   source_name,source_type,source_url,evidence_grade,published_at,
                   first_seen_at,event_type,expected_direction,severity,parse_rule,
                   raw_payload,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_key, f"H-{event_key}", article_id, code, title, source_name, source_type,
             source_url, evidence_grade, published_at, first_seen_at, event_type, 1, 0.5,
             "rule", raw_payload, created_at, created_at),
        )
        return event_key

    def _major(self, *, event_key="MK-1", title="重大事件", source_name="财联社",
               source_type="news_aggregator", source_url=LINK, article_id=None,
               evidence_grade="C", published_at=PUBLISHED_AT, first_seen_at=FIRST_SEEN,
               created_at=CREATED_AT, event_type="ai_infrastructure",
               significance_score=0.9, themes='[{"id":"ai","label":"AI"}]',
               affected_industries='["半导体"]', verification_status="single_source_linked",
               raw_payload="{}"):
        self.conn.execute(
            """INSERT INTO market_major_events(event_key,canonical_hash,article_id,title,
                   summary,source_name,source_type,source_url,evidence_grade,published_at,
                   first_seen_at,event_type,significance_score,themes,affected_industries,
                   verification_status,raw_payload,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_key, f"H-{event_key}", article_id, title, "s", source_name, source_type,
             source_url, evidence_grade, published_at, first_seen_at, event_type,
             significance_score, themes, affected_industries, verification_status,
             raw_payload, created_at, created_at),
        )
        return event_key

    def _read(self, as_of, **kwargs):
        return NL.news_fact_projections(self.conn, as_of=as_of, **kwargs)

    # ---------- NEWS-01 / NEWS-02：identity 来自 owner 的 durable event_key ----------

    def test_NEWS_01_news_event_identity_comes_from_the_durable_event_key(self):
        """identity 是 owner 的 ``event_key``，不是调用方命名的字符串。"""
        self._event(event_key="OWNER-KEY-1", source_url=None, article_id="ART-1")
        facts = self._read(INGEST_DAY)
        self.assertEqual([f.identity for f in facts], ["OWNER-KEY-1"])
        self.assertEqual(facts[0].record_kind, NL.NEWS_RECORD_KIND_EVENT)
        # 非空性：reader 真的读到了这一行（否则上面的等值断言空转）。
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].code, CODE)

    def test_NEWS_02_major_event_identity_comes_from_the_durable_event_key(self):
        self._major(event_key="OWNER-MAJOR-1")
        facts = self._read(INGEST_DAY)
        self.assertEqual([f.identity for f in facts], ["OWNER-MAJOR-1"])
        self.assertEqual(facts[0].record_kind, NL.NEWS_RECORD_KIND_MAJOR_EVENT)
        # market 级事件没有 code —— 不补假值。
        self.assertIsNone(facts[0].code)

    # ---------- NEWS-05 / NEWS-06 / NEWS-07：PIT 只由 first_seen_at 决定 ----------

    def test_NEWS_05_as_of_comes_from_first_seen_not_published(self):
        """``published_at`` 早 1 天也不行：availability_day 必须是 first_seen 那一天。"""
        self._event(published_at=PUBLISHED_AT, first_seen_at=FIRST_SEEN)
        facts = self._read(INGEST_DAY)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].first_seen_at, FIRST_SEEN)
        self.assertEqual(facts[0].availability_day, INGEST_DAY)
        # 描述性的 published_at 仍在，只是**不参与**可用性：两者不得互换。
        self.assertEqual(facts[0].published_at, PUBLISHED_AT)
        self.assertNotEqual(facts[0].availability_day, PUBLISHED_DAY)

    def test_NEWS_06_published_at_cannot_make_evidence_available_earlier(self):
        """published=9/19、first_seen=9/21 ⇒ 在 as_of=9/20 必须**不可见**。

        这条直接封死 look-ahead：9/21 才摄取到的数据不得倒填进 9/20 的历史研究。
        """
        self._event(published_at=PUBLISHED_AT, first_seen_at=FIRST_SEEN)
        self.assertEqual(self._read(ASOF_BEFORE_INGEST), ())
        self.assertEqual(len(self._read(PUBLISHED_DAY)), 0)
        # 非空性：真正到齐之后它必须出现（否则"不可见"可能只是因为读坏了）。
        self.assertEqual(len(self._read(INGEST_DAY)), 1)

    def test_NEWS_07_future_first_seen_is_excluded_from_a_historical_read(self):
        """first_seen_at 晚于 as_of 的行一律排除；到齐当天才可见。"""
        self._event(event_key="EK-FUTURE", first_seen_at=f"{LATER_DAY}T09:00:00+08:00")
        self.assertEqual(self._read(INGEST_DAY), ())
        self.assertEqual(len(self._read(LATER_DAY)), 1)
        # as_of 是**日历日**：当日 23:59:59 之前被观测到的都算当日可见。
        self._event(event_key="EK-LATE", first_seen_at=f"{INGEST_DAY}T23:59:59+08:00")
        self.assertEqual({f.identity for f in self._read(INGEST_DAY)},
                         {"EK-LATE"})

    # ---------- NEWS-08：missing / malformed / naive first_seen → fail closed ----------

    def test_NEWS_08_missing_or_malformed_first_seen_fails_closed_without_any_fallback(self):
        """PIT 不可证明时**没有**任何 fallback：不回退 published_at / created_at / now。"""
        for value, reason in (
            ("", "missing_first_seen_at"),
            ("   ", "missing_first_seen_at"),
            (None, "missing_first_seen_at"),
            ("不是时间", "malformed_first_seen_at"),
            ("2026-13-45T09:00:00+08:00", "malformed_first_seen_at"),
            (f"{INGEST_DAY}T09:00:00", "naive_first_seen_at"),
            (f"{INGEST_DAY} 09:00", "naive_first_seen_at"),
            (INGEST_DAY, "naive_first_seen_at"),
        ):
            with self.subTest(first_seen_at=value):
                with self.assertRaises(NL.NewsFactContractError) as ctx:
                    _projection(first_seen_at=value)
                self.assertEqual(ctx.exception.reason, reason)

        # 同一个失败在**读路径**上也必须发生，而且即使 published_at/created_at 都在。
        self._event(event_key="EK-BAD-SEEN", first_seen_at="", published_at=PUBLISHED_AT)
        with self.assertRaises(NL.NewsFactContractError) as ctx:
            self._read(INGEST_DAY)
        self.assertEqual(ctx.exception.reason, "missing_first_seen_at")

    # ---------- NEWS-11：grade / 可追溯性都不是核验 ----------

    def test_NEWS_11_untraceable_or_low_grade_source_fails_closed_without_verified(self):
        """grade 不参与判定；不可追溯 → source_unusable；可追溯也不等于 verified。"""
        self._event(event_key="EK-UNTRACEABLE", source_url=None, article_id=None,
                    evidence_grade="A")
        self._event(event_key="EK-LINKED-D", source_url=LINK, article_id=None,
                    evidence_grade="D")
        by_key = {f.identity: f for f in self._read(INGEST_DAY)}

        self.assertEqual(
            by_key["EK-UNTRACEABLE"].owner_verification_status,
            NL.NEWS_OWNER_SOURCE_UNUSABLE,
        )
        # grade="A" 不能救一个不可追溯的来源。
        self.assertEqual(
            by_key["EK-LINKED-D"].owner_verification_status,
            NL.NEWS_OWNER_SINGLE_SOURCE,
        )
        for fact in by_key.values():
            with self.subTest(identity=fact.identity):
                # 本 owner 今天**没有** verified 状态 —— 所有现存单源事件都是中性未核验。
                self.assertFalse(fact.is_owner_verified)
                self.assertIn(fact.owner_verification_status,
                              NL.NEWS_OWNER_VERIFICATION_STATUSES)
        self.assertNotIn(
            "verified", NL.NEWS_OWNER_VERIFICATION_STATUSES,
            "news owner 的核验闭集里出现了 verified —— 审计证明今天不存在该状态",
        )

    # ---------- NEWS-13：未审计过的 verification_status 不得默认成 unverified ----------

    def test_NEWS_13_unknown_verification_status_is_a_hard_error_not_a_default(self):
        """writer 闭集之外的状态一律 hard error —— 绝不静默落进 default。"""
        with self.assertRaises(NL.NewsFactContractError) as ctx:
            NL._issue_news_fact_projection(**_projection_fields(
                record_kind=NL.NEWS_RECORD_KIND_MAJOR_EVENT,
                identity="MK-X", code=None, title="t", source_name="s",
                source_type="news_aggregator", evidence_grade="A",
                owner_verification_status=NL.NEWS_OWNER_UNVERIFIED,
                ledger_verification_status="multi_source_verified",
                significance_score=0.9,
            ))
        self.assertEqual(ctx.exception.reason, "unknown_verification_status")

        self._major(event_key="MK-UNKNOWN", verification_status="multi_source_verified")
        with self.assertRaises(NL.NewsFactContractError) as ctx:
            self._read(INGEST_DAY)
        self.assertEqual(ctx.exception.reason, "unknown_verification_status")

    # ---------- NEWS-14 / NEWS-15：reputation / candidate link 不在读路径里 ----------

    def test_NEWS_14_source_reputation_never_changes_event_verification(self):
        """``credibility_score`` 拉满也不改变一行事件的核验结论 —— 结构上不在读路径。"""
        self._event(event_key="EK-REP")
        clean = {f.identity: f.as_dict() for f in self._read(INGEST_DAY)}

        for score in (0.0, 1.0):
            self.conn.execute("DELETE FROM news_source_reputation")
            self.conn.execute(
                """INSERT INTO news_source_reputation(source_name,evidence_grade,
                       observed_events,linked_pct,unique_pct,outcome_coverage_pct,
                       credibility_score,detail,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("巨潮资讯", "A", 999, 100.0, 100.0, 100.0, score, "{}", CREATED_AT),
            )
            after = {f.identity: f.as_dict() for f in self._read(INGEST_DAY)}
            self.assertEqual(clean, after, f"credibility_score={score} 改变了事件核验结论")

        recording = _RecordingConnection(self.conn)
        NL.news_fact_projections(recording, as_of=INGEST_DAY)
        self._assert_read_is_plain_select(recording)

    def test_NEWS_15_candidate_link_confidence_never_changes_event_verification(self):
        """``confidence=0.95`` 只说明"相关性映射" —— 它不能让单源事件变成已核验。"""
        self._major(event_key="MK-LINK", verification_status="single_source_linked")
        clean = {f.identity: f.as_dict() for f in self._read(INGEST_DAY)}

        event_id = self.conn.execute(
            "SELECT id FROM market_major_events WHERE event_key=?", ("MK-LINK",)
        ).fetchone()[0]
        self.conn.execute(
            """INSERT INTO market_event_candidate_links(event_id,code,name,industry,
                   pool_tier,mapping_reason,confidence,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, CODE, "n", "半导体", "holding", "主题行业映射", 0.95, CREATED_AT),
        )
        after = {f.identity: f.as_dict() for f in self._read(INGEST_DAY)}
        self.assertEqual(clean, after, "candidate-link confidence 改变了事件核验结论")

        fact = self._read(INGEST_DAY)[0]
        self.assertEqual(fact.owner_verification_status, NL.NEWS_OWNER_SINGLE_SOURCE)
        self.assertEqual(fact.ledger_verification_status, "single_source_linked")

        recording = _RecordingConnection(self.conn)
        NL.news_fact_projections(recording, as_of=INGEST_DAY)
        self._assert_read_is_plain_select(recording)

    def _assert_read_is_plain_select(self, recording: _RecordingConnection) -> None:
        """读路径只发 SELECT，而且只碰两张事件账本。"""
        self.assertTrue(recording.statements, "读路径没有发任何语句（非空性）")
        for statement in recording.statements:
            with self.subTest(statement=statement[:60]):
                self.assertTrue(
                    statement.lstrip().upper().startswith("SELECT"),
                    f"typed read 发了非 SELECT 语句：{statement[:80]}",
                )
                for table in FORBIDDEN_READ_TABLES:
                    self.assertNotIn(
                        table, statement,
                        f"typed read 读了 {table} —— 它不属于事件核验",
                    )
        self.assertEqual(
            2, len({s.split(" FROM ")[-1].split()[0] for s in recording.statements}),
            "typed read 应该只读 news_events 与 market_major_events 两张表",
        )

    # ---------- NEWS-16：raw_payload 不能覆盖 normalized 列 ----------
    def test_NEWS_16_raw_payload_cannot_override_normalized_owner_columns(self):
        """ledger authority 是 normalized 列；raw_payload 只是原始观察材料。"""
        payload = json.dumps({
            "evidence_grade": "A",
            "first_seen_at": "2020-01-01T00:00:00+08:00",
            "source_url": "https://example.invalid/forged",
            "verification_status": "multi_source_verified",
            "title": "被篡改的标题",
        })
        self._event(event_key="EK-RAW", evidence_grade="D", source_url=LINK,
                    first_seen_at=FIRST_SEEN, raw_payload=payload)
        self._major(event_key="MK-RAW", verification_status="unverified",
                    evidence_grade="D", first_seen_at=FIRST_SEEN, raw_payload=payload)
        by_key = {f.identity: f for f in self._read(INGEST_DAY)}

        fact = by_key["EK-RAW"]
        self.assertEqual(fact.evidence_grade, "D")
        self.assertEqual(fact.first_seen_at, FIRST_SEEN)
        self.assertEqual(fact.availability_day, INGEST_DAY)
        self.assertEqual(fact.source_url, LINK)
        self.assertEqual(fact.title, "回购公告")

        major = by_key["MK-RAW"]
        self.assertEqual(major.evidence_grade, "D")
        self.assertEqual(major.ledger_verification_status, "unverified")
        self.assertEqual(major.owner_verification_status, NL.NEWS_OWNER_UNVERIFIED)
        self.assertNotEqual(major.evidence_grade, "A")

    # ---------- NEWS-30：created_at 也不是 availability authority ----------

    def test_NEWS_30_created_at_is_never_an_availability_authority(self):
        """``created_at``（行写入时刻）**不得**缩小或放大可用性；只有 first_seen_at 说了算。

        两个方向都要测：created_at 更晚会把事件推出历史窗口（漏报），created_at 更早会把
        事件倒填进它还没被观测到的日子（look-ahead）。两者都是 PIT 破坏。
        """
        # ① created_at 比 first_seen_at 晚：不得因此被排除。
        self._event(event_key="EK-LATE-WRITE", first_seen_at=f"{ASOF_BEFORE_INGEST}T09:00:00+08:00",
                    created_at=f"{LATER_DAY}T09:00:00+08:00")
        # ② created_at 比 first_seen_at 早：不得因此提前可用。
        self._event(event_key="EK-EARLY-WRITE", first_seen_at=f"{LATER_DAY}T09:00:00+08:00",
                    created_at=f"{INGEST_DAY}T09:00:00+08:00")
        self._event(event_key="EK-PLAIN", first_seen_at=f"{ASOF_BEFORE_INGEST}T09:00:00+08:00",
                    created_at=f"{ASOF_BEFORE_INGEST}T09:00:01+08:00")

        at_asof = {f.identity for f in self._read(ASOF_BEFORE_INGEST)}
        self.assertEqual(at_asof, {"EK-LATE-WRITE", "EK-PLAIN"},
                         "created_at 把可用性窗口弄歪了")
        later = {f.identity for f in self._read(LATER_DAY)}
        self.assertEqual(later, {"EK-LATE-WRITE", "EK-EARLY-WRITE", "EK-PLAIN"})
        # 非空性：EK-EARLY-WRITE 在更早的 as_of 上必须**不可见**。
        self.assertNotIn("EK-EARLY-WRITE",
                         {f.identity for f in self._read(ASOF_BEFORE_INGEST)})

    # ---------- NEWS-23 / NEWS-24：不联网；历史缺数据不 backfill ----------

    def test_NEWS_23_typed_read_never_calls_the_fetchers_and_is_a_pure_read(self):
        """typed read 只读 durable ledger：不发网络、不写库（含 DDL）。"""
        self._event(event_key="EK-OFFLINE")
        with _no_network() as spy:
            facts = self._read(INGEST_DAY)
        self.assertEqual(len(facts), 1)
        self.assertEqual(spy.calls, [], f"typed read 触发了 ingestion fetch：{spy.calls}")

        # 只读连接也能跑通 —— 这直接证明读路径不写任何东西（含 ensure_schema 的 DDL）。
        # 必须先把 fixture 落盘：只读连接看不到未提交的事务。
        self.conn.commit()
        readonly = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        readonly.row_factory = sqlite3.Row
        try:
            before = os.stat(self.path).st_size
            facts = NL.news_fact_projections(readonly, as_of=INGEST_DAY)
            self.assertEqual([f.identity for f in facts], ["EK-OFFLINE"])
            self.assertEqual(os.stat(self.path).st_size, before)
        finally:
            readonly.close()

    def test_NEWS_24_historical_miss_never_triggers_live_fetch_or_backfill(self):
        """ledger 不存在时 fail closed —— **不**创建表、**不**抓 live news、**不**回填。"""
        empty = os.path.join(self.tmp.name, "empty.sqlite3")
        conn = sqlite3.connect(empty)
        conn.row_factory = sqlite3.Row
        try:
            with _no_network() as spy:
                with self.assertRaises(NL.NewsFactContractError) as ctx:
                    NL.news_fact_projections(conn, as_of=INGEST_DAY)
            self.assertEqual(ctx.exception.reason, "ledger_unavailable")
            self.assertEqual(spy.calls, [], f"历史 miss 触发了 live fetch：{spy.calls}")
            # 空库必须**还是**空库：没有偷偷建表。
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, set(), f"历史读创建了表：{sorted(tables)}")
        finally:
            conn.close()

    # ---------- NEWS-27：writer 闭集是审计产物，不是一个 else ----------

    def test_NEWS_27_major_event_writer_status_closed_set_is_audited(self):
        """扫描 writer 源码：它能写出的 ``verification_status`` 字面量必须与登记完全一致。

        新增一个 writer 状态却没有更新 owner 映射时必须变 RED —— 这正是"不得用
        ``else: unverified`` 静默吞掉未来新状态"的可执行形式。
        """
        _node, _statement, columns, index, values = _major_event_writer()
        self.assertEqual(
            columns[index], "verification_status",
            "writer 的列顺序漂移了 —— 请重新核对下标与下方断言",
        )
        element = values.elts[index]

        # 审计结论的形状：`"single_source_linked" if source_url else "unverified"` ——
        # 它只表达"有没有链接"，这正是它不能当核验的原因。
        self.assertIsInstance(element, ast.IfExp, "writer 的核验状态形状已改变，请重新审计")
        self.assertEqual(getattr(element.test, "id", None), "source_url")
        literals = _string_literals(element)
        self.assertEqual(literals, set(NL.NEWS_MAJOR_EVENT_WRITER_STATUSES),
                         "writer 闭集与 NEWS_MAJOR_EVENT_WRITER_STATUSES 漂移")
        self.assertEqual(getattr(element.body, "value", None), "single_source_linked")
        self.assertEqual(getattr(element.orelse, "value", None), "unverified")

        # 双向：映射表与 writer 闭集精确一致（少一个 / 多一个都报问题）。
        self.assertEqual(
            [], NL.news_owner_status_mapping_problems(
                NL._NEWS_MAJOR_LEDGER_STATUS_TO_OWNER, NL.NEWS_MAJOR_EVENT_WRITER_STATUSES,
            ),
        )
        self.assertEqual(
            sorted(NL._NEWS_MAJOR_LEDGER_STATUS_TO_OWNER),
            sorted(NL.NEWS_MAJOR_EVENT_WRITER_STATUSES),
        )
        # **没有** verified writer：本模块的字符串字面量里不存在 "verified" 这个状态词。
        literals = {
            node.value for node in ast.walk(_tree())
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertNotIn("verified", literals, "owner 模块出现了 verified 状态词")
        self.assertEqual(
            set(NL.NEWS_OWNER_VERIFICATION_STATUSES),
            {NL.NEWS_OWNER_SINGLE_SOURCE, NL.NEWS_OWNER_UNVERIFIED,
             NL.NEWS_OWNER_SOURCE_UNUSABLE},
        )

        # news_events 根本没有核验列 —— 不得为了"看起来整齐"造一个。
        event_columns = _news_event_writer_columns()
        self.assertEqual(
            [], [name for name in event_columns if "verif" in name or "status" in name],
            "news_events 出现了核验列（本轮 DB migration 必须为 0）",
        )

    # ---------- NEWS-28 / NEWS-29：first_seen 不可变；读 durable 行 ----------

    def test_NEWS_28_first_seen_at_is_immutable_across_recaptures(self):
        """``INSERT OR IGNORE``：同一个 event_key 再抓到时 first_seen_at 保持 T1。"""
        item = {
            "code": CODE, "name": "n", "title": "同一事件", "source": "巨潮资讯",
            "source_type": "announcement", "source_url": LINK, "article_id": None,
            "evidence_grade": "B", "time": PUBLISHED_AT,
        }
        first = f"{INGEST_DAY}T09:00:00+08:00"
        second = f"{LATER_DAY}T09:00:00+08:00"
        with _patched_event_rows([item]):
            NL.capture_events(self.conn, first_seen_at=first, codes=[CODE])
            NL.capture_events(self.conn, first_seen_at=second, codes=[CODE])

        row = self.conn.execute("SELECT first_seen_at FROM news_events").fetchone()
        self.assertEqual(row["first_seen_at"], first, "后一次抓取覆盖了 first_seen_at")
        self.assertNotEqual(row["first_seen_at"], second)

    def test_NEWS_29_duplicate_identity_reads_the_durable_row_not_the_new_payload(self):
        """同 identity 再抓一次（标题变了）：durable 行是 authority，typed read 读它。"""
        original = {
            "code": CODE, "name": "n", "title": "原始标题", "source": "巨潮资讯",
            "source_type": "announcement", "source_url": LINK, "article_id": None,
            "evidence_grade": "B", "time": PUBLISHED_AT,
        }
        altered = {**original, "title": "被改写的标题", "evidence_grade": "A"}
        with _patched_event_rows([original]):
            NL.capture_events(self.conn, first_seen_at=FIRST_SEEN, codes=[CODE])
        with _patched_event_rows([altered]):
            NL.capture_events(self.conn, first_seen_at=FIRST_SEEN, codes=[CODE])

        stored = self.conn.execute(
            "SELECT event_key,canonical_hash,title,evidence_grade FROM news_events"
        ).fetchall()
        self.assertEqual(len(stored), 1, "同 identity 出现了两行 durable 事实")
        self.assertEqual(stored[0]["title"], "原始标题")
        self.assertEqual(stored[0]["evidence_grade"], "B")

        fact = self._read(INGEST_DAY)[0]
        self.assertEqual(fact.identity, stored[0]["event_key"])
        self.assertEqual(fact.title, "原始标题")
        self.assertEqual(fact.evidence_grade, "B")


@contextmanager
def _patched_event_rows(items):
    """替换 ingestion 的 fetch 结果 —— 让 capture_events 离线可跑（不联网）。"""
    from unittest import mock

    with mock.patch.object(NL, "_event_rows", return_value=list(items)):
        yield


if __name__ == "__main__":
    unittest.main()
