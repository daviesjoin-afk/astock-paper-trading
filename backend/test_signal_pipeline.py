# -*- coding: utf-8 -*-
"""R25 —— Signal Pipeline 的永久回归（Candidate → Evidence → Decision → Ledger）。

规格 §31/§32。本文件覆盖 R25 真正建立的不变量，而不是"代码长什么样"：

* **单一 persistence owner** —— close 与 bootstrap 两条路径都经唯一 writer
  落库，且该 writer 是 production 里唯一出现 ``INSERT INTO paper_signals`` 的地方；
* **candidate / signal 生命周期边界** —— 候选 dict 不会"加字段加到变成 DB 行"，
  落库必须经过一个显式 decision；
* **decision outcome/reason 显式** —— 不返回裸布尔；
* **evidence 语义** —— 逐票双源判据来自 R24 authority，``verified`` +
  ``coverage_integrity`` **不得**被当成双源；stale 与 unavailable 不合并；
* **frozen context** —— cycle / strategy version 冻结，rollover 后不重贴历史；
* **bootstrap parity** —— bootstrap 与 close 共用同一 writer 与同一冲突契约；
* **幂等重跑** —— close 路径同键重跑不覆盖既有行；
* **写锁内零网络** —— provider I/O 不得进入 writer transaction。

夹具复用 ``test_production_path_golden_replay`` 的离线回放环境（真实生产入口
+ 注入行情），因此这些断言跑在真实调用链上，而不是手写的替身。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import market_data_contract as MDC
import news_learning as NL
import paper_trading as PT
import runtime_settings as RSET
import signal_service as SIG
import strategy_registry as SR
from test_production_path_golden_replay import (
    CAPITAL,
    D0,
    D1,
    OfflinePaperEnv,
    RULE,
    STRATEGY_ID,
    QUOTE_SCENARIOS,
)

try:
    import paper_research as PR
except Exception:  # pragma: no cover - optional module in reduced deployments
    PR = None


# ---------------------------------------------------------------------------
# Pure contract tests —— decision / evidence，零 I/O
# ---------------------------------------------------------------------------


class SignalContractTests(unittest.TestCase):
    """SignalDecision / SignalEvidence 的业务契约。"""

    def test_SIG01_decision_exposes_outcome_and_reason_not_a_bare_bool(self):
        approved = SIG.decide_signal(passed=True, reason="")
        blocked = SIG.decide_signal(passed=False, reason="缺少有效报价")
        self.assertEqual(approved.outcome, "approved")
        self.assertEqual(approved.status, "pending")
        self.assertEqual(blocked.outcome, "blocked")
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.reason, "缺少有效报价")
        # outcome 是封闭集合：拼错的状态必须在构造期就炸，而不是落库后才发现。
        with self.assertRaises(ValueError):
            SIG.SignalDecision(outcome="maybe", reason="", status="pending")

    def test_SIG02_decision_substatus_is_expressed_by_the_caller(self):
        """waitlist / recovery 等细分状态在 approved 之上覆盖，而不是新 outcome。"""
        waitlisted = SIG.decide_signal(
            passed=True, reason="冻结", status_if_passed="entry_frozen_waitlist",
        )
        recovery = SIG.decide_signal(
            passed=False, reason="止损后观察", status_if_blocked="recovery_watch",
        )
        self.assertEqual(waitlisted.outcome, "approved")
        self.assertEqual(waitlisted.status, "entry_frozen_waitlist")
        self.assertEqual(recovery.outcome, "blocked")
        self.assertEqual(recovery.status, "recovery_watch")

    def test_SIG03_evidence_requires_real_cross_source_not_merely_verified(self):
        """R24 §3 的核心混淆：``verified`` 不等于双源。

        ``coverage_integrity`` 也是 ``verified``，但它只说明"快照完整且覆盖达标"。
        逐票双源要求必须由 ``is_cross_source_verified`` 回答。
        """
        cross = SIG.signal_evidence(
            {"quote_validation": "cross_source_checked", "quote_at": "2026-09-08T09:35:00"},
            asof_day="2026-09-08",
        )
        self.assertEqual(cross.verification, MDC.VERIFICATION_VERIFIED)
        self.assertEqual(cross.verification_method, MDC.VERIFICATION_METHOD_CROSS_SOURCE)
        self.assertTrue(cross.cross_source_verified)

        # 覆盖完整性通过 —— 同样是 verified，但**不是**双源。
        coverage = MDC.MarketDataSnapshot(
            kind="full_market_snapshot", verification=MDC.VERIFICATION_VERIFIED,
            verification_method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
        )
        self.assertEqual(coverage.verification, MDC.VERIFICATION_VERIFIED)
        self.assertFalse(
            MDC.is_cross_source_verified(coverage),
            "coverage_integrity 被误判成双源核验",
        )

    def test_SIG04_single_source_and_unavailable_are_not_cross_source(self):
        """只有区间/时间戳校验（``range_timestamp_checked``）是单源，绝不升格。

        两个维度都要断言：``cross_source_verified`` 为假，**且**"从未核验"
        （``not_attempted``，含所有未知状态文本）必须诚实地带
        ``verification_method=none``。后者才是"没核验过"与"核验过但没通过"的
        分界：一旦未核验的证据被贴上 cross_source 标签，审阅者就无法区分
        "我们核验了并且通过"和"我们根本没看"。
        """
        for status, expected_verification in (
            ("range_timestamp_checked", MDC.VERIFICATION_SINGLE_SOURCE),
            ("cross_source_failed", MDC.VERIFICATION_DISAGREEMENT),
            ("cross_source_unavailable", MDC.VERIFICATION_UNAVAILABLE),
            ("unverified", MDC.VERIFICATION_NOT_ATTEMPTED),
            ("", MDC.VERIFICATION_NOT_ATTEMPTED),
            ("something_new_from_a_future_source", MDC.VERIFICATION_NOT_ATTEMPTED),
        ):
            evidence = SIG.signal_evidence({"quote_validation": status})
            self.assertEqual(evidence.verification, expected_verification, status)
            self.assertFalse(evidence.cross_source_verified, f"{status} 被当成双源")
            self.assertEqual(
                evidence.projection()["cross_source_verified"], False,
                f"{status} 的投影声称双源",
            )
            if expected_verification == MDC.VERIFICATION_NOT_ATTEMPTED:
                self.assertEqual(
                    evidence.verification_method, MDC.VERIFICATION_METHOD_NONE,
                    f"{status!r} 未核验却带上了 {evidence.verification_method!r} method",
                )

    def test_SIG05_evidence_is_frozen_and_projects_only_business_semantics(self):
        evidence = SIG.signal_evidence(
            {"quote_validation": "cross_source_checked", "quote_at": "2026-09-08T09:35:00"},
            asof_day="2026-09-08", policy="live_market",
        )
        projection = evidence.projection()
        # 前端拿到的必须是后端算好的业务谓词，而不是自己去比较 verified。
        self.assertIs(projection["cross_source_verified"], True)
        self.assertEqual(projection["verification_method"], "cross_source")
        self.assertEqual(projection["asof_day"], "2026-09-08")
        # provider 机制（重试/熔断/缓存键）不进投影。
        for leaked in ("retries", "circuit", "cache_key", "attempts"):
            self.assertNotIn(leaked, projection)
        # 不可变：detail 是只读视图。
        with self.assertRaises(TypeError):
            evidence.detail["quote_validation"] = "tampered"


class SignalWriterContractTests(unittest.TestCase):
    """唯一 writer 的语句与 **Decision→Commit 契约**（不依赖任何 DB）。

    R25 的核心不变量是 Candidate→Evidence→Decision→FrozenContext→Commit。
    writer 只认 frozen context 是不够的：如果它同时信任 caller 传进来的
    ``status`` / ``reason`` / 裁决 payload，那么任何模块都能自己拼一行
    ``status="pending"`` 直接落库，绕过整个决策链 —— "唯一 writer" 就只是把
    内联 SQL 换了个位置。本类把这条边界钉死。
    """

    @staticmethod
    def _conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE paper_signals(id INTEGER PRIMARY KEY, account_id TEXT,"
            " signal_date TEXT, intended_date TEXT, code TEXT, name TEXT, industry TEXT,"
            " close_price REAL, rank_score REAL, t_tier TEXT, t_score REAL, payload TEXT,"
            " status TEXT, reason TEXT, created_at TEXT, strategy_id TEXT,"
            " strategy_version INTEGER, strategy_checksum TEXT, cycle_id INTEGER)"
        )
        return conn

    @staticmethod
    def _context():
        return SIG.SignalWriteContext(
            account_id="acc", cycle_id=1, strategy_id="s", strategy_version=1,
            strategy_checksum="c", asof_day="2026-09-08",
        )

    @staticmethod
    def _row(**overrides):
        row = {
            "signal_date": "2026-09-08", "intended_date": "2026-09-09", "code": "600901",
            "name": "测试", "industry": None, "close_price": 10.0, "rank_score": 1.0,
            "t_tier": "A", "t_score": 1.0, "payload": {"pick": {"code": "600901"}},
            "created_at": "2026-09-08T15:00:00",
        }
        row.update(overrides)
        return row

    def _stored(self, conn):
        return conn.execute(
            "SELECT status, reason, payload FROM paper_signals"
        ).fetchone()

    def test_SIG06_conflict_statement_is_the_single_sql_constructor(self):
        ignore = SIG.conflict_statement(SIG.CONFLICT_IGNORE)
        refresh = SIG.conflict_statement(SIG.CONFLICT_REFRESH)
        self.assertTrue(ignore.startswith("INSERT OR IGNORE INTO paper_signals("))
        self.assertNotIn("ON CONFLICT", ignore)
        self.assertIn("ON CONFLICT(account_id,signal_date,code) DO UPDATE", refresh)
        with self.assertRaises(ValueError):
            SIG.conflict_statement("upsert_maybe")

    def test_SIG07_provenance_columns_are_absent_from_the_refresh_set_clause(self):
        """刷新只碰业务列：不可变 provenance 永不出现在 ``DO UPDATE SET``。"""
        refresh = SIG.conflict_statement(SIG.CONFLICT_REFRESH)
        set_clause = refresh.split("DO UPDATE SET", 1)[1]
        for column in ("cycle_id", "strategy_id", "strategy_version", "strategy_checksum"):
            self.assertNotIn(f"{column}=excluded.{column}", set_clause,
                             f"刷新语句会改写不可变列 {column}")
        # 业务列确实在刷新（否则 upsert 变成 ignore）。
        for column in ("status", "reason", "payload", "created_at", "t_score"):
            self.assertIn(f"{column}=excluded.{column}", set_clause)

    def test_SIG08_commit_rejects_a_row_missing_business_columns(self):
        """缺列必须在写之前炸，而不是把半行静默落库。"""
        context = self._context()
        decision = SIG.decide_signal(passed=True, reason="通过")
        with self.assertRaises(ValueError) as caught:
            SIG.commit_signal(
                self._conn(), context=context, decision=decision, row={"code": "600901"},
            )
        self.assertIn("missing columns", str(caught.exception))

    def test_SIG09_commit_requires_a_frozen_context(self):
        """``context`` 必须是 R23 的 frozen :class:`SignalWriteContext`。

        只断言"抛异常"不够：一个把 ``context or row`` 当兜底的实现也会在
        ``row={}`` 上抛 KeyError/ValueError —— 但它其实**接受**了行数据携带
        provenance，等于让"候选批一套戳、落库另一套戳"重新变得可表达。
        所以这里同时断言：非 frozen context（无论是 None 还是一个装有 provenance
        的 dict）都被明确拒绝，且拒绝理由指向"需要 frozen context"。
        """
        conn = self._conn()
        decision = SIG.decide_signal(passed=True, reason="通过")
        row = self._row()
        with self.assertRaises(ValueError) as caught:
            SIG.commit_signal(conn, context=None, decision=decision, row=row)
        self.assertIn("frozen", str(caught.exception).lower())

        # 一个"看起来像 context"的 dict 也不能被接受：它没有 frozen 语义，
        # 且会把 provenance 的决定权交回调用方。
        impostor = {
            "account_id": "acc", "cycle_id": 1, "strategy_id": "s",
            "strategy_version": 1, "strategy_checksum": "c", "asof_day": "2026-09-08",
        }
        with self.assertRaises((ValueError, AttributeError, TypeError)):
            SIG.commit_signal(conn, context=impostor, decision=decision, row=row)
        rows = conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0]
        self.assertEqual(rows, 0, "非 frozen context 仍然写入了 signal")
        conn.close()

    # ---------------------------------------------------------------
    # SIG-WRITER-01..03 —— Decision→Commit 契约（复核要求的 permanent regression）
    # ---------------------------------------------------------------

    def test_SIGW01_commit_rejects_a_missing_decision(self):
        """SIG-WRITER-01：没有 SignalDecision 就不能 commit。

        这条是一个"缺失参数"型漏洞：如果 ``decision`` 是可选的，caller 只要
        自己拼 row 就能伪造一条正式 signal，完全不经过 Candidate / Evidence /
        Decision。未来 R27 的 AI candidate producer 一旦拿到 writer 就能这样做 ——
        因此这里必须拒绝，而不是"默认放行"。

        拒绝由**签名**保证（keyword-only 且无默认值），因此缺参是 ``TypeError``；
        传了非 decision 的值则由 writer 自己 raise ``ValueError``。两种都要覆盖：
        只测后者会漏掉"签名允许省略"的实现。
        """
        conn = self._conn()
        row = self._row(status="pending", reason="我自己说通过")
        with self.assertRaises(TypeError) as caught:
            # 完全不传 decision：模拟"只想写一行"的 caller。
            SIG.commit_signal(conn, context=self._context(), row=row)
        self.assertIn("decision", str(caught.exception))

        # 传一个"看起来像 decision"的 dict / None 同样不行。
        for not_a_decision in (None, {"outcome": "approved", "status": "pending", "reason": ""}):
            with self.assertRaises(ValueError) as caught_value:
                SIG.commit_signal(
                    conn, context=self._context(), decision=not_a_decision, row=row,
                )
            self.assertIn("SignalDecision", str(caught_value.exception))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0], 0,
            "缺失/伪造 decision 仍然写入了 signal",
        )
        conn.close()

    def test_SIGW02_commit_rejects_a_row_that_forges_the_decision_status(self):
        """SIG-WRITER-02：decision=blocked，但 caller 伪造 ``status=pending`` → 拒绝。

        这正是"靠调用者自觉遵守"与"真正的 authority contract"的分界：
        writer 必须**拒绝** caller 提供的裁决字段，而不是静默忽略它
        （静默忽略会让旁路继续以"能跑"的形式存在，掩盖 caller 的误解）。
        """
        conn = self._conn()
        blocked = SIG.decide_signal(passed=False, reason="缺少有效报价")
        # 伪造一个可执行 status。
        with self.assertRaises(ValueError) as caught:
            SIG.commit_signal(
                conn, context=self._context(), decision=blocked,
                row=self._row(status="pending", reason="缺少有效报价"),
            )
        self.assertIn("status", str(caught.exception))
        # 即使"伪造的值与 decision 恰好一致"也必须拒绝：一致性不是调用方的职责，
        # 允许它就等于允许 caller 决定裁决来源。
        with self.assertRaises(ValueError):
            SIG.commit_signal(
                conn, context=self._context(), decision=blocked,
                row=self._row(status="blocked", reason="缺少有效报价"),
            )
        # 只伪造 reason 也不行。
        with self.assertRaises(ValueError) as caught_reason:
            SIG.commit_signal(
                conn, context=self._context(), decision=blocked,
                row=self._row(reason="缺少有效报价"),
            )
        self.assertIn("reason", str(caught_reason.exception))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0], 0,
            "伪造裁决字段仍然写入了 signal",
        )
        conn.close()

    def test_SIGW03_commit_rejects_a_payload_that_forges_the_evidence(self):
        """SIG-WRITER-03：decision 的 evidence=A，caller 在 payload 伪造成 B → 拒绝。

        证据与裁决同属 writer 独占：如果允许 caller 预置 ``signal_evidence``，
        落库的"为什么这条 signal 成立"就不再来自决策对象，而是来自最后一个动手
        写 payload 的人。
        """
        conn = self._conn()
        real = SIG.signal_evidence(
            {"quote_validation": "range_timestamp_checked"}, asof_day="2026-09-08",
        )
        decision = SIG.decide_signal(passed=True, reason="通过", evidence=real)
        forged = SIG.signal_evidence(
            {"quote_validation": "cross_source_checked"}, asof_day="2026-09-08",
        ).projection()
        self.assertFalse(real.cross_source_verified)
        self.assertTrue(forged["cross_source_verified"])

        for key, value in (("signal_evidence", forged), ("signal_decision", {"outcome": "approved"})):
            with self.assertRaises(ValueError) as caught:
                SIG.commit_signal(
                    conn, context=self._context(), decision=decision,
                    row=self._row(payload={"pick": {}, key: value}),
                )
            self.assertIn(key, str(caught.exception))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0], 0,
            "伪造 payload 裁决键仍然写入了 signal",
        )
        conn.close()

    def test_SIGW04_writer_injects_canonical_decision_and_evidence(self):
        """正对照：writer 自己注入裁决与证据，并返回落库的 canonical payload。

        没有这条，前面三条"拒绝"可能是"反正什么都不写"造成的假绿。
        """
        conn = self._conn()
        evidence = SIG.signal_evidence(
            {"quote_validation": "cross_source_checked", "quote_at": "2026-09-08T09:35:00"},
            asof_day="2026-09-08",
        )
        decision = SIG.decide_signal(passed=True, reason="", evidence=evidence)
        payload = {"pick": {"code": "600901"}}
        returned = SIG.commit_signal(
            conn, context=self._context(), decision=decision,
            row=self._row(payload=payload),
        )
        status, reason, stored_payload = self._stored(conn)
        self.assertEqual(status, decision.status)
        self.assertEqual(reason, decision.reason)
        stored = json.loads(stored_payload)
        # writer 注入的裁决与证据必须落在账本里，且与 decision 一致。
        self.assertEqual(stored["signal_decision"]["outcome"], "approved")
        self.assertEqual(stored["signal_decision"]["status"], decision.status)
        self.assertEqual(
            stored["signal_evidence"]["cross_source_verified"],
            evidence.cross_source_verified,
        )
        # 调用方拿到的返回值就是落库内容，可直接用于 risk log（避免二次注入点）。
        self.assertEqual(returned["signal_decision"], stored["signal_decision"])
        # writer 不得把 payload 写回调用方传进来的 mapping（那是共享可变状态）。
        self.assertNotIn("signal_decision", payload)
        self.assertNotIn("signal_evidence", payload)
        conn.close()

    def test_SIGW05_payload_must_be_a_mapping_not_a_preserialized_string(self):
        """调用方不能预序列化 payload：writer 必须在写入前注入裁决与证据。"""
        conn = self._conn()
        decision = SIG.decide_signal(passed=True, reason="")
        with self.assertRaises(ValueError) as caught:
            SIG.commit_signal(
                conn, context=self._context(), decision=decision,
                row=self._row(payload='{"pick": {}}'),
            )
        self.assertIn("mapping", str(caught.exception))
        conn.close()


# ---------------------------------------------------------------------------
# Architecture guards —— 所有权与依赖方向
# ---------------------------------------------------------------------------


def _backend_source(name):
    return (Path(BACKEND) / name).read_text(encoding="utf-8")


def _docstring_nodes(tree):
    """Ids of every ``ast.Constant`` that is actually a docstring."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            out.add(id(first.value))
    return out


def _sql_literals(path):
    """Yield the static text of every non-docstring string literal in ``path``.

    ``JoinedStr`` (f-strings) contribute their literal chunks, so a statement
    assembled as ``f"INSERT ... {columns} ..."`` is still visible to the guard.
    Docstrings/comment text are excluded on purpose: mentioning a statement in
    prose is not a write site, and a guard that counts prose would reward
    hiding SQL inside documentation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.value
        elif isinstance(node, ast.JoinedStr):
            chunks = [
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            if chunks:
                yield "".join(chunks)


def _executed_sql_literals(path):
    """Non-docstring string literals from a module (alias, for readability)."""
    return list(_sql_literals(path))


def _touches_name(node, names):
    """True when ``node`` references any identifier in ``names``."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
    return False


def _production_modules():
    return sorted(
        path for path in Path(BACKEND).glob("*.py")
        if not path.name.startswith("test_")
    )


class SignalArchitectureGuardTests(unittest.TestCase):
    """R25 §55/§56：少量高价值门禁（writer 所有权 / 零 provider / 写锁内零网络）。"""

    def test_SIGG01_signal_insert_has_exactly_one_production_owner(self):
        """生产代码中 ``INSERT INTO paper_signals`` 只允许出现在 signal_service。

        按 **AST 中的字符串常量**判定，而不是扫原文：docstring / 注释里提到这句
        SQL 不算写入点，只有真正被执行的语句才算。否则门禁会奖励"把 SQL 藏进
        文档"，也会被文档里的一句话误伤。唯一 owner 模块自身是**允许**的例外。
        """
        owner = "signal_service.py"
        offenders = []
        for path in _production_modules():
            if path.name == owner:
                continue
            for literal in _sql_literals(path):
                for match in re.finditer(
                    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+paper_signals\b(?!_archive)",
                    literal,
                ):
                    offenders.append(f"{path.name}: {match.group(0)!r}")
        self.assertEqual(
            offenders, [],
            "paper_signals 的生产写入点必须收敛到 signal_service.commit_signal；"
            f"发现其他 owner：{offenders}",
        )
        # 唯一 owner 确实是构造这两种语句的地方。
        writer = list(_sql_literals(Path(BACKEND) / owner))
        self.assertTrue(
            any("INSERT OR IGNORE INTO paper_signals" in sql for sql in writer),
            "signal_service 没有构造幂等 INSERT",
        )
        self.assertTrue(
            any("ON CONFLICT(account_id,signal_date,code) DO UPDATE" in sql for sql in writer),
            "signal_service 没有构造 bootstrap upsert",
        )

    def test_SIGG02_signal_paths_commit_through_the_writer_only(self):
        """close / bootstrap 两条路径都经唯一 writer，且都不再内联 SQL。"""
        raw = _backend_source("paper_trading.py")
        tree = ast.parse(raw)
        for function in ("generate_signals", "_bootstrap_signals_for_today"):
            node = next(
                (n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == function),
                None,
            )
            self.assertIsNotNone(node, f"找不到 {function}")
            body = "\n".join(raw.splitlines()[node.lineno - 1:node.end_lineno])
            self.assertIn("SIG.commit_signal(", body,
                          f"{function} 没有经唯一 writer 落库")
            self.assertNotIn("INSERT INTO paper_signals", body,
                             f"{function} 又内联了 signal INSERT")
            self.assertNotIn("INSERT OR IGNORE INTO paper_signals", body,
                             f"{function} 又内联了 signal INSERT")

    def test_SIGG03_signal_service_has_no_provider_or_clock_dependency(self):
        """Signal writer 不得自己取数、读时钟、开事务（§20/§52/§60）。"""
        source = _backend_source("signal_service.py")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {
            "data_fetcher", "marketdata_cache", "marketdata_feeds",
            "marketdata_providers", "marketdata_transport", "marketdata_normalizers",
            "paper_trading", "main", "api_paper", "fastapi", "requests", "urllib",
            "datetime", "time",
        }
        self.assertEqual(imported & forbidden, set(),
                         f"signal_service 依赖了被禁止的模块：{imported & forbidden}")
        # 允许的唯一项目依赖：R24 contract 与 R23 provenance 契约。
        self.assertLessEqual(
            {name for name in imported if name in {
                "market_data_contract", "market_data_service",
                "strategy_selection_resolver", "strategy_selection_provenance",
                "strategy_registry",
            }},
            {"market_data_contract", "strategy_selection_resolver"},
            "signal_service 引入了超出契约层的项目依赖",
        )
        # 不自开事务、不联网、不读时钟。按 AST 判定**调用接收者**，而不是只看
        # 方法名：``quote.get(...)`` 是字典读取，``conn.execute(...)`` 才是 DB 写。
        # ``conn.execute`` 是被允许的（writer 的本职就是执行已构造好的 INSERT）；
        # 被禁止的是**开/结束事务**、联网与读时钟 —— 那三件事会破坏
        # "调用方负责 fencing" 与"writer 不引入时序"这两条契约。
        # docstring 里解释"本模块不开事务"同样不算违规：那种宽松 substring 门禁
        # 最终会被人整条关掉。
        db_owners = {"conn", "connection", "db", "cursor"}
        http_modules = {"requests", "urllib", "httpx", "aiohttp"}
        clock_modules = {"datetime", "time"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            receiver = func.value
            root = (
                receiver.id if isinstance(receiver, ast.Name)
                else receiver.attr if isinstance(receiver, ast.Attribute)
                else None
            )
            if root in db_owners and func.attr in {"commit", "rollback", "executescript"}:
                offenders.append(f"{root}.{func.attr}")
            if root in db_owners and func.attr == "execute":
                # 只允许执行本模块构造的 INSERT；一旦出现事务控制语句即违规。
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        upper = argument.value.upper()
                        if "BEGIN" in upper or "COMMIT" in upper or "ROLLBACK" in upper:
                            offenders.append(f"{root}.{func.attr}({argument.value[:24]!r})")
            if root in http_modules:
                offenders.append(f"{root}.{func.attr}")
            if root in clock_modules:
                offenders.append(f"{root}.{func.attr}")
        self.assertEqual(
            offenders, [],
            "signal_service 不得自开事务 / 联网 / 读时钟；发现：" + repr(sorted(set(offenders))),
        )
        # 不得出现 BEGIN 之类的锁语句（只查被执行的 SQL 字面量）。
        for sql in _sql_literals(Path(BACKEND) / "signal_service.py"):
            self.assertNotIn("BEGIN", sql.upper(),
                             "signal_service 自己开了事务边界")

    def test_SIGG04_no_network_io_inside_the_signal_write_transactions(self):
        """provider/network I/O 不得出现在 signal 的 BEGIN IMMEDIATE 块内（§20）。"""
        raw = _backend_source("paper_trading.py")
        tree = ast.parse(raw)
        network = (
            "_quotes(", "_news_for(", "fetch_sector_flow", "fetch_hot_sector_snapshot",
            "fetch_market_snapshot_full", "refresh_rows", "fetch_realtime_for_codes",
            "fetch_independent_realtime_for_codes", "fetch_hot_rank",
            "news_keyword_scan", "enrich_live_flow_details",
        )
        checked = 0
        for function in ("generate_signals", "_bootstrap_signals_for_today"):
            node = next(
                (n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == function),
                None,
            )
            for with_node in ast.walk(node):
                if not isinstance(with_node, ast.With):
                    continue
                call = with_node.items[0].context_expr
                if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_db"):
                    continue
                keywords = {kw.arg: kw.value for kw in call.keywords}
                immediate = keywords.get("immediate")
                if not (isinstance(immediate, ast.Constant) and immediate.value is True):
                    continue
                checked += 1
                block = "\n".join(
                    raw.splitlines()[with_node.lineno - 1:with_node.end_lineno])
                for io_name in network:
                    self.assertNotIn(
                        io_name, block,
                        f"{function} 的写锁内出现网络/取数调用 {io_name}",
                    )
        self.assertGreaterEqual(checked, 1, "找不到任何 signal 写事务（门禁空转）")

    def test_SIGG05_production_callsites_must_pass_an_explicit_decision(self):
        """两条 production signal 路径都必须交入明确的 ``SignalDecision``。

        这是 R25 最核心不变的守卫：Candidate→Evidence→Decision→Commit。
        writer 的签名已经强制要求 ``decision``（见 SIG-WRITER-01），本门禁额外
        钉住**调用点**——防止将来有人把两条路径改成"先 commit、decision 算了但
        没传"，或者新增第三条只写 row 的路径。

        判定基于 AST：要求每个 ``SIG.commit_signal(...)`` 调用点都带
        ``decision=`` 关键字，且该值不是字面量 None。
        """
        raw = _backend_source("paper_trading.py")
        tree = ast.parse(raw)
        callsites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "commit_signal"):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords}
            callsites.append((node.lineno, keywords))

        self.assertGreaterEqual(
            len(callsites), 2,
            f"production 里有 {len(callsites)} 个 commit_signal 调用点（期望 ≥2：close + bootstrap）",
        )
        for lineno, keywords in callsites:
            self.assertIn(
                "decision", keywords,
                f"paper_trading.py:{lineno} 的 commit_signal 没有传 decision "
                "（绕过了 Candidate→Evidence→Decision 边界）",
            )
            decision = keywords["decision"]
            self.assertFalse(
                isinstance(decision, ast.Constant) and decision.value is None,
                f"paper_trading.py:{lineno} 的 decision 是字面量 None",
            )
            # 裁决字段不得由调用点提供 —— 它们由 decision 独占。
            row = keywords.get("row")
            if isinstance(row, ast.Dict):
                keys = {
                    key.value for key in row.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                leaked = keys & {"status", "reason"}
                self.assertEqual(
                    leaked, set(),
                    f"paper_trading.py:{lineno} 在 row 里自述裁决字段 {leaked}"
                    "（这些由 SignalDecision 独占）",
                )


# ---------------------------------------------------------------------------
# Ledger integration —— 真实生产入口
# ---------------------------------------------------------------------------


class SignalLedgerTests(OfflinePaperEnv, unittest.TestCase):
    """真实 ``generate_signals`` / ``_bootstrap_signals_for_today`` 链路。

    每个用例使用**独立账本文件**：signal 的幂等 / rollover / evidence 断言彼此
    之间不能靠残留状态成立，否则一条用例的通过会污染另一条的失败。
    """

    def setUp(self):
        # Defensive: quote injection is per-class state; always start clean.
        from test_production_path_golden_replay import QUOTE_PRICES
        QUOTE_PRICES.clear()
        QUOTE_SCENARIOS.clear()
        # 每个用例独立账本文件：否则前一条用例留下的 signal / cycle / 版本会让
        # 幂等与 rollover 断言"因为残留状态"而假通过（或互相污染而假失败）。
        # 共享的行情/因子工件仍由 OfflinePaperEnv.setUpClass 提供。
        seq = SignalLedgerTests._counter = getattr(SignalLedgerTests, "_counter", 0) + 1
        db_path = os.path.join(self._tmp, f"r25_case_{seq}.sqlite3")
        self._db_patch = mock.patch.object(PT, "DB_PATH", db_path)
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)
        self._news_patch = mock.patch.object(NL, "PAPER_DB_PATH", db_path)
        self._news_patch.start()
        self.addCleanup(self._news_patch.stop)
        PT.init_db()

    def _boot_account(self, capital=CAPITAL):
        """建立「已激活策略 + 运行中账户」前提，返回 cycle_id。"""
        with self._conn() as conn:
            SR.ensure_schema(conn)
            SR.create_user_definition(
                conn, STRATEGY_ID, "R25 信号链策略", dsl_ast=RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="r25-test",
            )
            SR.transition(conn, STRATEGY_ID, "validated", expected_status="draft",
                          reason="r25 validate", actor="r25-test")
            SR.transition(conn, STRATEGY_ID, "active", expected_status="validated",
                          reason="r25 activate", actor="r25-test")
        PT.init_db()
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="r25-test")
        summary, cycle = PT.start_new_cycle(capital=capital, include_dashboard=False)
        cycle_id = int(cycle["id"])
        with self._conn() as conn:
            account = conn.execute(
                "SELECT cycle_id,status FROM paper_accounts WHERE id=?", (STRATEGY_ID,),
            ).fetchone()
        self.assertEqual(int(account["cycle_id"]), cycle_id)
        return cycle_id

    def _signals(self, **where):
        clause = ""
        params = ()
        if where:
            clause = " WHERE " + " AND ".join(f"{key}=?" for key in where)
            params = tuple(where.values())
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM paper_signals{clause} ORDER BY id", params)]

    def test_SIG10_close_signal_carries_decision_evidence_and_frozen_provenance(self):
        """真实 close 路径：无论通过与否，都要留下可复核的 outcome/reason/evidence。"""
        cycle_id = self._boot_account()
        result = PT.generate_signals(D0)
        rows = [row for row in result["accounts"] if row["id"] == STRATEGY_ID]
        self.assertTrue(rows, result["accounts"])
        signals = self._signals(account_id=STRATEGY_ID, signal_date=D0.isoformat())
        self.assertTrue(signals, "收盘扫描没有产生任何 signal 行")

        with self._conn() as conn:
            pinned = SR.cycle_stamp_for_account(conn, STRATEGY_ID, cycle_id=cycle_id)
        self.assertIsNotNone(pinned, "前提：周期必须有 immutable pin")
        for signal in signals:
            # provenance 四列 = 该周期的 frozen pin，绝不是 current head。
            self.assertEqual(signal["strategy_id"], pinned[0])
            self.assertEqual(int(signal["strategy_version"]), int(pinned[1]))
            self.assertEqual(signal["strategy_checksum"], pinned[2])
            self.assertEqual(int(signal["cycle_id"]), cycle_id)
            # 决策显式：状态与原因都在账本里，reviewer 不必去 helper 里推导。
            self.assertTrue(signal["reason"] is not None)
            payload = json.loads(signal["payload"])
            evidence = payload.get("signal_evidence")
            self.assertIsNotNone(evidence, "signal 没有留下 evidence 投影")
            self.assertIn("cross_source_verified", evidence)
            self.assertIn("verification_method", evidence)
            # 双源结论必须有 method 支撑，而不是只写一个 verified。
            if evidence["cross_source_verified"]:
                self.assertEqual(evidence["verification_method"], "cross_source",
                                 "声称双源却没有 cross_source method")
            # 落库的裁决必须显式记录 outcome —— 否则"为什么这条 signal 被写进
            # 系统"只能从 status 反推（而 status 会被后续生命周期改写）。
            decision = payload.get("signal_decision")
            self.assertIsNotNone(decision, "signal 没有落库裁决 outcome/reason")
            self.assertIn(decision.get("outcome"), {"approved", "blocked"})
            self.assertEqual(decision.get("status"), signal["status"],
                             "裁决里的 status 与行上的 status 不一致")
            # outcome 必须与 status 自洽：blocked 的裁决不会被写成可执行状态。
            if decision["outcome"] == "blocked":
                self.assertNotEqual(
                    signal["status"], "pending",
                    f"{signal['code']} 裁决 blocked 却写成 pending 可执行状态",
                )

    def test_SIG10b_api_projection_exposes_decision_and_evidence(self):
        """读模型投影把裁决/证据交给前端，使前端无需反推（§43）。"""
        import dashboard_queries as DQ

        self._boot_account()
        PT.generate_signals(D0)
        signals = self._signals(account_id=STRATEGY_ID, signal_date=D0.isoformat())
        self.assertTrue(signals, "需要至少一条 signal 才能断言投影")
        # 把 signal 的意图交易日挪到今天，使 overview 投影（按 intended_date=today
        # 过滤）能取到刚写入的行。这里只搬运日期，不改任何裁决字段。
        today = dt.date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE paper_signals SET intended_date=? WHERE account_id=? AND signal_date=?",
                (today, STRATEGY_ID, D0.isoformat()),
            )
            conn.commit()
        # signal 投影是**组合视图**的段落（activity 视图刻意不加载它，以省下
        # ~0.9MB 的 payload 解析），所以这里按生产读模型的实际入口取组合视图。
        overview = DQ.dashboard()
        projected = [
            row for row in overview.get("signals", [])
            if row.get("account_id") == STRATEGY_ID
        ]
        self.assertTrue(projected, "overview 投影没有返回刚写入的 signal")
        for row in projected:
            decision = row.get("signal_decision")
            self.assertIsNotNone(decision, "投影没有 signal_decision 字段")
            self.assertIn(decision.get("outcome"), {"approved", "blocked"})
            self.assertIn("reason", decision)
            evidence = decision.get("evidence")
            self.assertIsNotNone(evidence, "投影没有 evidence 状态")
            self.assertIn("cross_source_verified", evidence)
            self.assertIn("verification_method", evidence)
            # 投影必须与账本一致：前端看到的双源结论就是后端算好的那个。
            payload = json.loads(
                next(s["payload"] for s in signals if s["id"] == row.get("id"))
            )
            self.assertEqual(
                evidence["cross_source_verified"],
                payload["signal_evidence"]["cross_source_verified"],
                "投影的双源结论与账本不一致",
            )

    def test_SIG11_close_rerun_for_the_same_key_is_idempotent(self):
        """同键重跑不覆盖既有行（``INSERT OR IGNORE``），也不产生第二行。"""
        self._boot_account()
        PT.generate_signals(D0)
        first = self._signals(account_id=STRATEGY_ID, signal_date=D0.isoformat())
        self.assertTrue(first)
        before = {(row["code"]): (row["id"], row["status"], row["reason"], row["payload"])
                  for row in first}

        PT.generate_signals(D0)
        second = self._signals(account_id=STRATEGY_ID, signal_date=D0.isoformat())
        after = {(row["code"]): (row["id"], row["status"], row["reason"], row["payload"])
                 for row in second}
        # 键集合不变：没有新建重复行。
        self.assertEqual(set(before), set(after), "重跑改变了当日 signal 的键集合")
        for code, snapshot in before.items():
            self.assertEqual(
                after[code][0], snapshot[0],
                f"{code} 重跑后 id 变了（插入了新行而不是幂等跳过）",
            )

    def test_SIG12_bootstrap_and_close_share_one_writer_and_one_contract(self):
        """bootstrap 不存在独立 persistence path：与 close 同一 writer、同一列集。"""
        cycle_id = self._boot_account()
        PT.generate_signals(D0)
        close_rows = self._signals(account_id=STRATEGY_ID)
        self.assertTrue(close_rows)
        close_columns = set(close_rows[0])

        # bootstrap 走真实入口（盘中扫描）。它可能因候选/门禁而少产出，但只要有行，
        # 就必须与 close 行**同形状** —— 这正是"统一 authority"的可观察含义。
        bootstrap = PT._bootstrap_signals_for_today(D1, source_slot="intraday")
        self.assertNotEqual(bootstrap.get("status"), "error", bootstrap)
        bootstrap_rows = self._signals(account_id=STRATEGY_ID, intended_date=D1.isoformat())
        self.assertTrue(
            bootstrap_rows,
            "bootstrap 没有产生任何 signal —— 本用例会空转，无法证明两路径同契约",
        )
        for row in bootstrap_rows:
            self.assertEqual(set(row), close_columns,
                             "bootstrap 行的列集与 close 行不同（存在第二套写路径）")
            self.assertIsNotNone(row["cycle_id"],
                                 "bootstrap 行必须带不可变 cycle 归属")
            self.assertIsNotNone(row["strategy_version"],
                                 "bootstrap 行必须带 frozen strategy version")
        self.assertEqual(int(bootstrap_rows[0]["cycle_id"]), cycle_id)
        # 两条路径的 signal_date / intended_date 语义各自保持（close 用因子日 D0、
        # 意图次日；bootstrap 用当日意图）—— 统一的是 authority，不是业务语义。
        self.assertTrue(all(row["intended_date"] == D1.isoformat()
                            for row in bootstrap_rows))

    def test_SIG13_live_signal_path_rejects_a_quote_that_is_not_cross_source_verified(self):
        """逐票双源是 close signal 的硬门禁；单源/未核验一律拒绝（§18）。"""
        self._boot_account()
        # 把注入行情降级成"只通过区间/时间戳校验"（单源），并保留当日时间戳，
        # 使 freshness 仍然通过 —— 于是唯一能拦住它的就是双源要求本身。
        from test_production_path_golden_replay import _fake_quotes
        original = PT._quotes

        def single_source_quotes(codes, asof_date=None):
            quotes = _fake_quotes(codes, asof_date)
            for quote in quotes.values():
                quote["quote_validation"] = "range_timestamp_checked"
            return quotes

        PT._quotes = single_source_quotes
        try:
            result = PT.generate_signals(D0)
        finally:
            PT._quotes = original
        rows = [row for row in result["accounts"] if row["id"] == STRATEGY_ID]
        self.assertTrue(rows, result["accounts"])
        signals = self._signals(account_id=STRATEGY_ID, signal_date=D0.isoformat())
        self.assertTrue(signals, "单源行情下仍应留下被拒绝的 signal 记录")
        for signal in signals:
            self.assertNotEqual(
                signal["status"], "pending",
                f"{signal['code']} 在单源行情下仍被放行成可执行信号",
            )
            self.assertIn("交叉核验", signal["reason"] or "",
                          f"{signal['code']} 被拒的原因没有指向双源核验：{signal['reason']!r}")
            payload = json.loads(signal["payload"])
            evidence = payload.get("signal_evidence") or {}
            self.assertIs(evidence.get("cross_source_verified"), False)
            self.assertEqual(evidence.get("verification"), MDC.VERIFICATION_SINGLE_SOURCE)

    def test_SIG14_provider_is_never_called_inside_the_write_transaction(self):
        """写锁内零网络的**行为**证明（不是读源码）。

        做法：包住 ``PT._db``，在 ``immediate=True`` 的事务区间内置位一个标志；
        再把 ``_quotes`` / ``_news_for`` / ``fetch_sector_flow`` 换成探针，只要
        它们在标志置位期间被调用就立即失败。这直接观测运行期行为，因而即使将来
        有人改写了函数结构（而不是删掉取数），门禁依然有效。
        """
        from contextlib import contextmanager
        self._boot_account()
        PT.generate_signals(D0)

        calls = []
        in_write_txn = {"flag": False}
        original_db = PT._db

        @contextmanager
        def observing_db(immediate=False, hot_path=False):
            if immediate:
                in_write_txn["flag"] = True
            try:
                with original_db(immediate=immediate, hot_path=hot_path) as conn:
                    yield conn
            finally:
                if immediate:
                    in_write_txn["flag"] = False

        def probe(name, original):
            def wrapped(*args, **kwargs):
                if in_write_txn["flag"]:
                    raise AssertionError(
                        f"{name} 在 BEGIN IMMEDIATE 写事务内被调用（provider I/O 进入写锁）"
                    )
                calls.append(name)
                return original(*args, **kwargs)
            return wrapped

        import data_fetcher as dfc
        originals = (
            (PT, "_db", PT._db),
            (PT, "_quotes", PT._quotes),
            (PT, "_news_for", PT._news_for),
            (dfc, "fetch_sector_flow", dfc.fetch_sector_flow),
            (dfc, "fetch_hot_sector_snapshot", dfc.fetch_hot_sector_snapshot),
        )
        try:
            PT._db = observing_db
            PT._quotes = probe("_quotes", originals[1][2])
            PT._news_for = probe("_news_for", originals[2][2])
            dfc.fetch_sector_flow = probe("fetch_sector_flow", originals[3][2])
            dfc.fetch_hot_sector_snapshot = probe(
                "fetch_hot_sector_snapshot", originals[4][2])
            PT._bootstrap_signals_for_today(D1, source_slot="intraday")
        finally:
            for target, attr, original in originals:
                setattr(target, attr, original)

        self.assertGreater(
            len(calls), 0,
            "探针没有观察到任何取数调用 —— 用例空转，无法证明取证发生在写锁之外",
        )


class SignalCrashSafetyTests(OfflinePaperEnv, unittest.TestCase):
    """写路径失败必须 fail closed：不写错误 signal，并给出可诊断原因（§50）。"""

    def test_SIG15_commit_failure_does_not_leave_a_partial_signal(self):
        """writer 抛错时整批回滚：既不留半行，也不吞掉异常。

        用 ghost account 触发 DB 层的 cycle provenance trigger（R23 第二道门），
        因此异常必须来自 SQL 层而不是参数校验 —— 后者根本走不到 INSERT。
        """
        with self._conn() as conn:
            SR.ensure_schema(conn)
        PT.init_db()
        context = SIG.SignalWriteContext(
            account_id="ghost_account", cycle_id=1, strategy_id="s",
            strategy_version=1, strategy_checksum="c", asof_day="2026-09-08",
        )
        decision = SIG.decide_signal(passed=True, reason="通过")
        conn = sqlite3.connect(PT.DB_PATH, timeout=5)
        try:
            with self.assertRaises(sqlite3.Error):
                SIG.commit_signal(
                    conn, context=context, decision=decision,
                    conflict=SIG.CONFLICT_IGNORE,
                    row={
                        "signal_date": "2026-09-08", "intended_date": "2026-09-09",
                        "code": "600901", "name": "幻影", "industry": None,
                        "close_price": 10.0, "rank_score": 1.0, "t_tier": "A",
                        "t_score": 1.0, "payload": {}, "created_at": "2026-09-08T15:00:00",
                    },
                )
            conn.rollback()
        finally:
            conn.close()
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM paper_signals WHERE account_id='ghost_account'",
            ).fetchone()[0]
        self.assertEqual(count, 0, "失败的写入留下了残留行")


if __name__ == "__main__":
    unittest.main()
