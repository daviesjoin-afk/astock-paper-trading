# -*- coding: utf-8 -*-
"""R27-B2C-4B —— portfolio/accounting fact → typed research evidence adapter 的回归。

存在理由是这条不变量：

    **组合记账事实可以进入研究链路，但它的身份、业务日与核验结论全部由 portfolio owner
    发布；而"这条记账事实是否被证明"与"行情估值是否可信"是两个不同 owner 的问题。**

分七组：

    PORT-REF-01 ~ 04  签发形状：只有真正的 owner 投影可签发、dict/duck-typed/子类拒绝、
                      source_type 复用 portfolio_research、identity 完全由 owner 派生、
                      as_of 完全来自 owner context（无 created_at / today fallback）
    PORT-REF-05 ~ 06  status → owner-neutral outcome：verified / unknown 逐条断言
    PORT-REF-07 ~ 08  映射与 owner 的**公开**状态闭集双向穷尽一致，owner 词表漂移即 fail closed
    PORT-REF-09 ~ 12  同 identity + 内容变了 → EvidenceConflict；不同 account/cycle/kind
                      → 不同 identity
    PORT-REF-13       detail 不复制 owner 的 factual payload
    PORT-REF-14       market 核验词汇不被引入（method=None / cross_source=False /
                      attributes 里没有 market 维度）
    PORT-REF-15       InformationEvent.kind 派生成 EVENT_PORTFOLIO_RESEARCH_OBSERVED
    PORT-REF-16 ~ 18  production 调用点为 0、签名不给自述入口、依赖方向单向且
                      physical database provenance 仍 OPEN

全部离线：临时 SQLite 账本 + owner 自己的 public read，不连真实库、不读墙钟。
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from types import MappingProxyType
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_research_contract as ARC  # noqa: E402
import ai_research_portfolio_adapter as PFA  # noqa: E402
import paper_portfolio_read_model as PPRM  # noqa: E402
import paper_trading as PT  # noqa: E402

ADAPTER_MODULE = "ai_research_portfolio_adapter.py"
OWNER_MODULE = "paper_portfolio_read_model.py"
CONTRACT_MODULE = "ai_research_contract.py"

ACCOUNT = next(iter(PT.ACCOUNT_SPECS))
CODE = "600519"
DAY = dt.date(2026, 9, 20)
NEXT = DAY + dt.timedelta(days=1)


def _module_source(name: str) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


class _StripDocstrings(ast.NodeTransformer):
    """递归剥掉模块 / 类 / 函数的 docstring。

    边界模块必须在 docstring 里写清它**为什么**不解释 owner 词表、为什么不发布 NAV，
    字符级子串搜索会把那些说明文字本身当成越界证据。因此"这一层不得出现某词表"的断言
    只看**会执行**的代码。
    """

    def _strip(self, node):
        self.generic_visit(node)
        body = list(node.body)
        if (
            body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:]
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def _executable_module(name: str) -> str:
    """模块的**会执行**代码（全部 docstring 已剥掉）。"""
    tree = _StripDocstrings().visit(ast.parse(_module_source(name)))
    return ast.unparse(tree)


def _production_modules() -> list[str]:
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            roots.add(node.module.split(".")[0])
    return roots


def _factory_call_sites(source: str) -> int:
    """源码里对 ``evidence_ref_from_portfolio_projection`` 的**调用**点数量。

    只看 ``ast.Call``：``def`` 定义、``__all__`` 字符串、docstring 都不算调用。
    """
    count = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "evidence_ref_from_portfolio_projection":
            count += 1
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "evidence_ref_from_portfolio_projection"
        ):
            count += 1
    return count


class PortfolioAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patchers = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
        )
        for patcher in self._patchers:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle = self._cycle("r27b2c4b-adapter", "running")
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle, ACCOUNT)
        )
        self.conn.commit()
        self._attach(ACCOUNT)
        self.initial_cash = float(self.conn.execute(
            "SELECT initial_cash FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0])

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self.tmp.cleanup()

    # ---------- fixtures ----------

    def _cycle(self, key, status, capital=100000.0, created=DAY):
        stamp = f"{created.isoformat()} 09:00:00"
        return int(self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (key, status, capital, "shared_pool", stamp, stamp,
             stamp if status == "running" else None),
        ).lastrowid)

    def _attach(self, account_id=ACCOUNT, *, effective=DAY, cycle=None):
        day = effective.isoformat()
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,"
            "reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cycle or self.cycle, account_id, "1", "default", "{}", "r27b2c4b",
             day, f"{day} 09:00:00"),
        )
        self.conn.commit()

    def _order_and_fill(self, *, side, qty, price, fill_date, verified=True,
                        realized_pnl=None, fees=5.0):
        amount = qty * price
        stamp = PT._strategy_stamp(self.conn, ACCOUNT)
        order_id = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified,realized_pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, side, CODE, "测试股", qty, price, price, amount, fees, "filled",
             "r27b2c4b-test", "{}", f"{fill_date} 09:30:00", f"{fill_date} 09:30:01",
             "market", "seed", *stamp, self.cycle,
             "verified" if verified else "unknown", 1 if verified else 0, realized_pnl),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, ACCOUNT, side, CODE, qty, price, amount, fees, fill_date,
             f"{fill_date} 09:30:00", "r27b2c4b-test"),
        )
        self.conn.commit()
        return order_id

    def _lot(self, qty, cost, *, source_order_id=None, acquired_at=None):
        acquired_at = acquired_at or f"{DAY.isoformat()} 10:00:00"
        row = int(self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.cycle, ACCOUNT, CODE, "测试股", "测试", qty, qty, cost,
             acquired_at, NEXT.isoformat(), "stock_t1", source_order_id, 1, 1),
        ).lastrowid)
        self.conn.commit()
        return row

    def _seed_verified_book(self):
        """买 100 @10（lot）+ 卖 30 @12 且 realized_pnl=59.5：三条事实都可证明。"""
        buy = self._order_and_fill(side="buy", qty=100, price=10.0,
                                   fill_date=DAY.isoformat())
        self._lot(100, 10.0, source_order_id=buy)
        self._order_and_fill(side="sell", qty=30, price=12.0,
                             fill_date=DAY.isoformat(), realized_pnl=59.5)

    def _facts(self, *, asof=DAY, account=ACCOUNT, cycle=None):
        context = PPRM.PortfolioReadContext(cycle or self.cycle, asof)
        return PPRM.accounting_fact_projections(self.conn, context, account_id=account)

    def _ref(self, fact):
        return PFA.evidence_ref_from_portfolio_projection(fact)

    def _issue(self, **overrides):
        """owner 侧私有签发口 —— 用于构造只差某一个维度的合法 owner 投影。"""
        fields = {
            "version": PPRM.PORTFOLIO_FACT_CONTRACT_VERSION,
            "fact_kind": PPRM.PORTFOLIO_FACT_CASH,
            "cycle_id": self.cycle,
            "account_id": ACCOUNT,
            "asof_day": DAY.isoformat(),
            "status": PPRM.STATUS_VERIFIED,
            "value": 1.0,
        }
        fields.update(overrides)
        return PPRM._issue_portfolio_fact_projection(**fields)

    def _assert_conflict(self, first_ref, second_ref):
        self.assertEqual(first_ref.identity(), second_ref.identity(), "必须是同一条事实")
        self.assertNotEqual(first_ref.fact_state(), second_ref.fact_state())
        self.assertNotEqual(
            first_ref.detail["content_fingerprint"],
            second_ref.detail["content_fingerprint"],
        )
        for pair in ((first_ref, second_ref), (second_ref, first_ref)):
            with self.subTest(first=pair[0].verification):
                with self.assertRaises(ARC.EvidenceConflict):
                    ARC.ResearchHypothesis(
                        hypothesis_id="H-PORT-1", as_of=DAY.isoformat(),
                        subject=ACCOUNT, thesis="组合记账事实被改写",
                        evidence=(
                            ARC.HypothesisEvidence(
                                ref=pair[0], relation=ARC.RELATION_SUPPORTS),
                            ARC.HypothesisEvidence(
                                ref=pair[1], relation=ARC.RELATION_SUPPORTS),
                        ),
                    )

    # ---------- PORT-REF-01 ~ 04：签发形状 ----------

    def test_PORT_REF_01_only_exact_owner_projection_is_accepted(self):
        """PORT-REF-01：只接受**真正的** ``PortfolioFactProjection``（``type(...) is``）。

        dict / ``Mapping`` / duck-typed 伪对象（哪怕有 ``status`` / ``fact_kind`` /
        ``asof_day``）/ 子类一律拒绝 —— 否则调用方就能自述身份与核验结论冒充 owner 投影。
        """
        fact = self._facts()[0]
        for payload in (
            {"fact_kind": "cash", "cycle_id": self.cycle, "account_id": ACCOUNT,
             "asof_day": DAY.isoformat(), "status": "verified", "value": 1.0},
            {"status": "unknown", "value": None},
            "cash",
            42,
            None,
        ):
            with self.subTest(payload=repr(payload)[:40]):
                with self.assertRaises(TypeError):
                    PFA.evidence_ref_from_portfolio_projection(payload)

        class FakeProjection:
            fact_kind = "cash"
            cycle_id = 1
            account_id = "acct"
            asof_day = DAY.isoformat()
            status = "verified"
            value = 1.0
            version = PPRM.PORTFOLIO_FACT_CONTRACT_VERSION

        for fake in (FakeProjection(), mock.Mock(spec=PPRM.PortfolioFactProjection)):
            with self.subTest(fake=type(fake).__name__):
                with self.assertRaises(TypeError) as caught:
                    PFA.evidence_ref_from_portfolio_projection(fake)
                self.assertIn("PortfolioFactProjection", str(caught.exception))

        # 非空性：真投影必须能被签发（否则上面只是"入口恒抛"）。
        self.assertIs(type(self._ref(fact)), ARC.ResearchEvidenceRef)

    def test_PORT_REF_02_source_type_is_the_existing_portfolio_research_type(self):
        """PORT-REF-02：复用既有 ``EVIDENCE_SOURCE_PORTFOLIO_RESEARCH``，不另造词汇。"""
        self._seed_verified_book()
        facts = self._facts()
        self.assertEqual(3, len(facts))
        for fact in facts:
            with self.subTest(kind=fact.fact_kind):
                ref = self._ref(fact)
                self.assertEqual(ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH, ref.source_type)
                self.assertEqual("portfolio_research", ref.source_type)
                self.assertIn(ref.source_type, ARC.SUPPORTED_OWNER_ADAPTERS)
                self.assertIn(ref.source_type, ARC.EVIDENCE_SOURCE_TYPES)
        # 没有第二套 source type 被发明出来。
        adapter_source = _executable_module(ADAPTER_MODULE)
        for invented in ("portfolio_accounting", "portfolio_fact", "pnl_fact",
                         "accounting_research"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, adapter_source)

    def test_PORT_REF_03_source_id_is_entirely_derived_from_the_projection(self):
        """PORT-REF-03：``source_id`` 完全由 ``fact_kind`` / ``cycle_id`` / ``account_id`` 派生。"""
        self._seed_verified_book()
        for fact in self._facts():
            with self.subTest(kind=fact.fact_kind):
                expected = (
                    f"{fact.fact_kind}|cycle={self.cycle}|account={ACCOUNT}"
                )
                ref = self._ref(fact)
                self.assertEqual(expected, ref.source_id)
                # identity 三元组里的 source_id 逐字相同。
                self.assertEqual(
                    (ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH, expected, DAY.isoformat()),
                    ref.identity(),
                )
        # 同 identity、同内容 → 安全去重；确定性指纹（不受 hash 随机化影响）。
        first = self._ref(self._facts()[0])
        second = self._ref(self._facts()[0])
        self.assertEqual(first.fact_state(), second.fact_state())
        self.assertEqual(64, len(first.detail["content_fingerprint"]))

    def test_PORT_REF_04_as_of_comes_only_from_the_owner_context(self):
        """PORT-REF-04：``as_of`` 只能来自 ``PortfolioReadContext.asof_day``，零 fallback。

        刻意**不**回落 ``created_at`` / ``updated_at`` / ``today()`` / latest NAV date。
        """
        self._seed_verified_book()
        for asof in (DAY, NEXT):
            with self.subTest(asof=asof.isoformat()):
                for fact in self._facts(asof=asof):
                    ref = self._ref(fact)
                    self.assertEqual(asof.isoformat(), ref.as_of)
                    self.assertEqual(fact.asof_day, ref.as_of)
        # 同一身份在另一个业务日 → 另一条 identity（as_of 真的参与 identity）。
        self.assertEqual(
            self._ref(self._facts()[0]).identity()[:2],
            self._ref(self._facts(asof=NEXT)[0]).identity()[:2],
        )
        self.assertNotEqual(
            self._ref(self._facts()[0]).identity(),
            self._ref(self._facts(asof=NEXT)[0]).identity(),
        )
        # adapter 自身的代码里没有墙钟 / created_at 回落。
        body = _executable_module(ADAPTER_MODULE)
        for token in ("date.today", "datetime.now", "time.time", "created_at", "updated_at"):
            with self.subTest(token=token):
                self.assertNotIn(token, body)

    # ---------- PORT-REF-05 ~ 06：status → owner-neutral outcome ----------

    def test_PORT_REF_05_verified_maps_to_owner_neutral_verified(self):
        """PORT-REF-05：``STATUS_VERIFIED`` → ``OWNER_OUTCOME_VERIFIED``。"""
        self._seed_verified_book()
        facts = self._facts()
        self.assertEqual(
            [PPRM.STATUS_VERIFIED] * 3, [fact.status for fact in facts],
            "夹具必须给出三条已证明的事实，否则本用例空转",
        )
        for fact in facts:
            with self.subTest(kind=fact.fact_kind):
                ref = self._ref(fact)
                self.assertEqual(ARC.OWNER_OUTCOME_VERIFIED, ref.owner_verification.outcome)
                self.assertIs(True, ref.is_verified)
                # status 逐字保留 owner 自己的状态词。
                self.assertEqual(PPRM.STATUS_VERIFIED, ref.verification)
                self.assertEqual(fact.status, ref.owner_verification.status)

    def test_PORT_REF_06_unknown_maps_to_owner_neutral_unverified(self):
        """PORT-REF-06：``STATUS_UNKNOWN`` → ``OWNER_OUTCOME_UNVERIFIED``。

        **不是** ``source_unusable``：障碍不在"核验过程"，而在这条事实本身还不足以被证明。
        本 owner 今天没有发布"证据源不可用"这个独立状态，research 侧不得替它猜一个。
        """
        facts = self._facts(account="r27b2c4b-ghost")
        self.assertEqual(
            [PPRM.STATUS_UNKNOWN] * 3, [fact.status for fact in facts],
            "夹具必须给出三条未证明的事实，否则本用例空转",
        )
        for fact in facts:
            with self.subTest(kind=fact.fact_kind):
                ref = self._ref(fact)
                self.assertEqual(ARC.OWNER_OUTCOME_UNVERIFIED, ref.owner_verification.outcome)
                self.assertIs(False, ref.is_verified)
                self.assertIs(False, ref.owner_verification.source_unusable)
                self.assertNotEqual(
                    ARC.OWNER_OUTCOME_SOURCE_UNUSABLE,
                    ref.owner_verification.outcome,
                )
                self.assertEqual(PPRM.STATUS_UNKNOWN, ref.verification)
                self.assertIsNone(fact.value)

    # ---------- PORT-REF-07 ~ 08：映射双向穷尽 ----------

    def test_PORT_REF_07_mapping_exactly_covers_the_owner_status_closed_set(self):
        """PORT-REF-07：映射与 owner 的**公开**状态闭集**精确相等**，两个方向都要红。"""
        mapping = PFA._PORTFOLIO_OUTCOME_BY_STATUS
        statuses = PPRM.PORTFOLIO_FACT_STATUSES

        self.assertEqual(set(statuses), set(mapping))
        self.assertEqual([], PFA._portfolio_outcome_mapping_problems(mapping, statuses))
        for status in statuses:
            with self.subTest(status=status):
                self.assertIn(mapping[status], ARC.OWNER_OUTCOMES)
        # 非空性：两态都真的被用到（"穷尽"不能只是把一切归到一态）。
        self.assertEqual(
            {ARC.OWNER_OUTCOME_VERIFIED, ARC.OWNER_OUTCOME_UNVERIFIED},
            set(mapping.values()),
        )
        # owner 今天刻意**没有**发布 source_unusable —— 不得由 adapter 猜出来。
        self.assertNotIn(ARC.OWNER_OUTCOME_SOURCE_UNUSABLE, set(mapping.values()))

        # 正向：缺一个 owner 状态必须被发现。
        missing = PFA._portfolio_outcome_mapping_problems(
            {k: v for k, v in mapping.items() if k != PPRM.STATUS_UNKNOWN}, statuses,
        )
        self.assertTrue(missing, "缺一个 owner 状态未被发现")
        self.assertTrue(any("没有登记" in item for item in missing))
        # 反向：映射里多一个 owner 已不认识的状态必须被发现。
        extra = PFA._portfolio_outcome_mapping_problems(
            {**mapping, "brand_new_status": ARC.OWNER_OUTCOME_VERIFIED}, statuses,
        )
        self.assertTrue(extra, "多一个未知状态未被发现")
        self.assertTrue(any("已不认识" in item for item in extra))

    def test_PORT_REF_08_a_simulated_new_owner_status_fails_closed(self):
        """PORT-REF-08：模拟 owner 新增一个状态 → 归口必须 **fail closed**，不得落到 else。

        只 patch **owner 自己**的公开闭集（那正是 owner 侧一次契约变更的形态），不动
        adapter 源码 —— 从而证明这条保证真的挂在"与 owner 双向一致"上，而不是一句注释。
        """
        fact = self._facts()[0]
        with mock.patch.object(
            PPRM, "PORTFOLIO_FACT_STATUSES",
            (*PPRM.PORTFOLIO_FACT_STATUSES, "brand_new_status"),
        ):
            with self.assertRaises(ValueError) as caught:
                PFA._portfolio_owner_verification(fact)
            message = str(caught.exception)
            self.assertIn("drifted", message)
            self.assertIn("brand_new_status", message, "错误信息必须点名那个未归口的状态")

        # 反方向：owner **收回**一个状态（词表里少了已登记的那个）同样 fail closed。
        with mock.patch.object(
            PPRM, "PORTFOLIO_FACT_STATUSES", (PPRM.STATUS_VERIFIED,),
        ):
            with self.assertRaises(ValueError) as caught:
                PFA._portfolio_owner_verification(fact)
            self.assertIn("drifted", str(caught.exception))

        # 非空性对照：词表一致时同一调用必须成功 —— 否则上面可能只是因为该函数恒抛。
        self.assertEqual(
            ARC.OWNER_OUTCOME_VERIFIED,
            PFA._portfolio_owner_verification(fact).outcome,
        )

    # ---------- PORT-REF-09 ~ 12：内容指纹与 identity ----------

    def test_PORT_REF_09_a_changed_cash_value_is_a_conflict(self):
        """PORT-REF-09：同 identity + 现金值被改写 → ``EvidenceConflict``。"""
        before = self._ref(self._facts()[0])
        self.conn.execute(
            "UPDATE paper_accounts SET initial_cash=? WHERE id=?",
            (self.initial_cash + 1234.5, ACCOUNT),
        )
        self.conn.commit()
        after = self._ref(self._facts()[0])
        self.assertNotEqual(before.source_id, "")
        self.assertNotEqual(
            PPRM.accounting_fact_projections(
                self.conn, PPRM.PortfolioReadContext(self.cycle, DAY), account_id=ACCOUNT,
            )[0].value,
            self.initial_cash,
        )
        self._assert_conflict(before, after)

    def test_PORT_REF_10_a_changed_realized_pnl_is_a_conflict(self):
        """PORT-REF-10：同 identity + 已实现盈亏被改写 → ``EvidenceConflict``。"""
        self._seed_verified_book()
        before = self._ref(self._facts()[1])
        self.conn.execute(
            "UPDATE paper_orders SET realized_pnl=? WHERE side='sell'", (999.0,),
        )
        self.conn.commit()
        after = self._ref(self._facts()[1])
        self.assertEqual(
            999.0,
            PPRM.accounting_fact_projections(
                self.conn, PPRM.PortfolioReadContext(self.cycle, DAY), account_id=ACCOUNT,
            )[1].value,
        )
        self._assert_conflict(before, after)

    def test_PORT_REF_11_a_changed_position_cost_summary_is_a_conflict(self):
        """PORT-REF-11：同 identity + 持仓成本摘要被改写 → ``EvidenceConflict``。"""
        self._seed_verified_book()
        before = self._ref(self._facts()[2])
        self.conn.execute(
            "UPDATE paper_position_lots SET cost=? WHERE code=?", (11.0, CODE),
        )
        self.conn.commit()
        after = self._ref(self._facts()[2])
        self.assertNotEqual(
            before.detail["content_fingerprint"], after.detail["content_fingerprint"],
        )
        self._assert_conflict(before, after)

    def test_PORT_REF_12_identity_separates_account_cycle_and_kind(self):
        """PORT-REF-12：不同 account / cycle / fact_kind → 不同 identity（不碰撞）。"""
        base = self._ref(self._issue())
        self.assertEqual(
            f"cash|cycle={self.cycle}|account={ACCOUNT}", base.source_id,
        )
        variants = {
            "kind": self._ref(self._issue(fact_kind=PPRM.PORTFOLIO_FACT_REALIZED_PNL)),
            "account": self._ref(self._issue(account_id="another-account")),
            "cycle": self._ref(self._issue(cycle_id=self.cycle + 1)),
            "asof": self._ref(self._issue(asof_day=NEXT.isoformat())),
        }
        for dimension, ref in variants.items():
            with self.subTest(dimension=dimension):
                self.assertNotEqual(base.identity(), ref.identity())
                if dimension == "asof":
                    # as_of 是 identity 的第三个维度，但它**不**进 source_id：
                    # ``as_of`` 由 owner context 派生，不是 identity 字符串的一部分。
                    self.assertEqual(base.source_id, ref.source_id)
                    self.assertNotEqual(base.as_of, ref.as_of)
                else:
                    self.assertNotEqual(base.source_id, ref.source_id)
                # 不同 identity 的事实可以同时进入一个假设（不是冲突）。
                hypothesis = ARC.ResearchHypothesis(
                    hypothesis_id="H-PORT-2", as_of=NEXT.isoformat(),
                    subject=ACCOUNT, thesis="两条不同事实",
                    evidence=(
                        ARC.HypothesisEvidence(
                            ref=self._ref(self._issue()), relation=ARC.RELATION_SUPPORTS),
                        ARC.HypothesisEvidence(ref=ref, relation=ARC.RELATION_CONTEXT),
                    ),
                )
                self.assertEqual(2, len(hypothesis.evidence))
        # 非空性：同 identity 的两个 ref 必须仍然相同（否则上面的不等断言无意义）。
        self.assertEqual(base.identity(), self._ref(self._issue()).identity())

    # ---------- PORT-REF-13 ~ 15：detail、market 词汇、event kind ----------

    def test_PORT_REF_13_detail_does_not_duplicate_the_factual_value(self):
        """PORT-REF-13：``detail`` 只放 identity + 核验 + 指纹 + 最小审计元数据。

        owner 的 factual payload（现金数字 / 已实现盈亏 / 持仓数 / 成本合计）不在其中 ——
        需要事实真值的消费者读 owner 投影本身，不读 detail 里的第二份 payload。
        """
        self._seed_verified_book()
        for fact in self._facts():
            with self.subTest(kind=fact.fact_kind):
                ref = self._ref(fact)
                self.assertEqual(
                    {"content_fingerprint", "contract_version", "read_model_version"},
                    set(ref.detail),
                )
                self.assertEqual(PPRM.PORTFOLIO_FACT_CONTRACT_VERSION,
                                 ref.detail["contract_version"])
                self.assertEqual(PPRM.PORTFOLIO_READ_MODEL_VERSION,
                                 ref.detail["read_model_version"])
                for leaked in ("value", "cash", "realized_pnl", "position_count",
                               "cost_value", "status", "fact_kind", "cycle_id",
                               "account_id", "asof_day"):
                    with self.subTest(leaked=leaked):
                        self.assertNotIn(leaked, ref.detail)
        # 两种 owner 值形态都不得出现在 detail 里（数值与 summary 各一）。
        kinds = {fact.fact_kind: fact for fact in self._facts()}
        self.assertIsInstance(kinds[PPRM.PORTFOLIO_FACT_CASH].value, float)
        self.assertIsInstance(
            kinds[PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY].value,
            PPRM.PositionCostSummary,
        )

    def test_PORT_REF_14_market_verification_vocabulary_is_not_introduced(self):
        """PORT-REF-14：不把组合核验翻译成 market 词。

        ``verification_method`` / ``cross_source_verified`` 是 **market-only** 问题，对
        非 market owner 保持"不适用"（``None`` / ``False``），而不是"核验失败"。owner
        attributes 里也不得出现这两个 market 维度。
        """
        facts = self._facts(account="r27b2c4b-ghost") + self._facts()
        self.assertTrue(facts)
        for fact in facts:
            with self.subTest(kind=fact.fact_kind, status=fact.status):
                ref = self._ref(fact)
                self.assertIsNone(ref.verification_method)
                self.assertIs(False, ref.cross_source_verified)
                projected = ref.projection()
                self.assertIsNone(projected["verification_method"])
                self.assertIs(False, projected["cross_source_verified"])
                self.assertNotIn("verification_method", ref.verification_attributes)
                self.assertNotIn("cross_source_verified", ref.verification_attributes)
                self.assertNotIn("confidence", ref.verification_attributes)
                self.assertNotIn("score", ref.verification_attributes)
                self.assertNotIn("quality_score", ref.verification_attributes)
                self.assertEqual(
                    {
                        "verification_scope", "fact_contract_version",
                        "read_model_version", "fact_kind",
                    },
                    set(ref.verification_attributes),
                )
                self.assertEqual(
                    PPRM.PORTFOLIO_FACT_VERIFICATION_SCOPE,
                    ref.verification_attributes["verification_scope"],
                )
                self.assertIsInstance(ref.verification_attributes, MappingProxyType)
                # 事件上的 market-only 兼容面同样"不适用"。
                event = ARC.InformationEvent(
                    as_of=DAY.isoformat(), source="portfolio_owner", evidence_ref=ref,
                )
                self.assertIsNone(event.verification_method)
                self.assertEqual(ref.is_verified, event.is_verified)

        # adapter 源码里不得出现 market 核验词表。
        body = _executable_module(ADAPTER_MODULE)
        for token in ("market_data_contract", "MDC.", "VERIFICATION_METHOD",
                      "is_cross_source_verified", "cross_source"):
            with self.subTest(token=token):
                self.assertNotIn(token, body)

    def test_PORT_REF_15_event_kind_derives_to_portfolio_research_observed(self):
        """PORT-REF-15：``InformationEvent.kind`` 由 source_type 派生，调用方无法错标。"""
        self._seed_verified_book()
        for fact in self._facts():
            with self.subTest(kind=fact.fact_kind):
                event = ARC.InformationEvent(
                    as_of=DAY.isoformat(), source="portfolio_owner",
                    evidence_ref=self._ref(fact),
                )
                self.assertEqual(ARC.EVENT_PORTFOLIO_RESEARCH_OBSERVED, event.kind)
                self.assertEqual("portfolio_research_observed", event.kind)
                self.assertEqual(
                    ARC.EVENT_PORTFOLIO_RESEARCH_OBSERVED, event.projection()["kind"],
                )
                self.assertIn(event.kind, ARC.EVENT_KINDS)

    # ---------- PORT-REF-16 ~ 18：调用点、签名、依赖方向 ----------

    def test_PORT_REF_16_production_adapter_callers_are_zero_before_b2c4c(self):
        """PORT-REF-16：B2C-4C 之前 production 调用点必须**恰好为 0**。

        B2C-4B = capability + contract；runtime consumer migration 属于 B2C-4C。若本轮已经
        出现 production 调用者，那就是 scope violation，而不是"顺便接好了"。
        """
        callers = {
            name: _factory_call_sites(_module_source(name))
            for name in _production_modules()
        }
        offenders = {name: count for name, count in callers.items() if count}
        self.assertEqual({}, offenders, f"factory 出现 production 调用点：{offenders}")
        # 非空性：扫描器真的看得见调用点（在**测试**文件里找一个真调用）。
        self.assertIn(
            "evidence_ref_from_portfolio_projection",
            _module_source("test_ai_research_portfolio_adapter.py"),
        )
        probe = (
            "from ai_research_portfolio_adapter import "
            "evidence_ref_from_portfolio_projection\n"
            "evidence_ref_from_portfolio_projection(projection)\n"
        )
        self.assertEqual(1, _factory_call_sites(probe))
        self.assertEqual(0, _factory_call_sites("def f():\n    return 1\n"))
        # 明确点名的三个"将来才会迁移"的模块本轮不得 import adapter。
        for name in ("deepseek_research.py", "ai_analysis.py", "adaptive_engine.py"):
            with self.subTest(module=name):
                self.assertNotIn(
                    "ai_research_portfolio_adapter", _imported_roots(_module_source(name)),
                )
                self.assertNotIn(
                    "paper_portfolio_read_model", _imported_roots(_module_source(name)),
                )

    def test_PORT_REF_17_callers_cannot_supply_identity_day_or_outcome(self):
        """PORT-REF-17：签名里**只有** projection —— 身份 / 业务日 / 核验结论都不可自述。"""
        parameters = set(
            inspect.signature(PFA.evidence_ref_from_portfolio_projection).parameters
        )
        self.assertEqual({"projection"}, parameters)
        for forbidden in (
            "source_id", "source_type", "as_of", "asof_day", "verification", "outcome",
            "status", "cycle_id", "cycle", "account_id", "account", "fact_kind",
            "value", "detail",
        ):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, parameters)

        # 仍然没有公开 raw 构造器 —— adapter 也不提供绕道。
        with self.assertRaises(TypeError):
            ARC.ResearchEvidenceRef(
                source_type=ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH,
                source_id="forged", as_of=DAY.isoformat(),
            )
        self.assertFalse(hasattr(PFA, "_OWNER_ISSUED"))
        self.assertFalse(hasattr(ARC, "_OWNER_ISSUED"))
        # 公开面只有一个函数，且没有 registry / service / facade。
        self.assertEqual(("evidence_ref_from_portfolio_projection",), PFA.__all__)
        adapter_source = _executable_module(ADAPTER_MODULE)
        for forbidden in ("class BaseAdapter", "register_adapter", "ADAPTER_REGISTRY",
                          "class Manager", "class Service", "class Repository",
                          "class Facade"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, adapter_source)

    def test_PORT_REF_18_dependency_direction_is_one_way_and_provenance_still_open(self):
        """PORT-REF-18：``owner → adapter → contract`` 单向；physical database provenance 仍 OPEN。

        adapter 关闭的是"调用方不能自述身份 / 核验 / 业务日"，**不是**"输入投影确实来自
        canonical 数据库"。调用方仍可自造 SQLite connection / fixture 调 owner 的 public
        read 拿到投影 —— 那属于可信数据库 provenance，R27 完成前不得降级。
        """
        contract_imports = _imported_roots(_module_source(CONTRACT_MODULE))
        for name in ("paper_portfolio_read_model", "ai_research_portfolio_adapter"):
            with self.subTest(imported=name):
                self.assertNotIn(name, contract_imports, "research contract 不得 import portfolio")

        owner_imports = _imported_roots(_module_source(OWNER_MODULE))
        for name in ("ai_research_contract", "ai_research_portfolio_adapter"):
            with self.subTest(imported=name):
                self.assertNotIn(name, owner_imports, "portfolio owner 不得 import research")

        adapter_imports = _imported_roots(_module_source(ADAPTER_MODULE))
        self.assertIn("ai_research_contract", adapter_imports)
        self.assertIn("paper_portfolio_read_model", adapter_imports)

        # 同时认识两套词表的 production 模块**只有** adapter 一个。
        both = [
            name for name in _production_modules()
            if {"ai_research_contract", "paper_portfolio_read_model"}
            <= _imported_roots(_module_source(name))
        ]
        self.assertEqual([ADAPTER_MODULE], both, f"不止一个模块同时认识两套词表：{both}")

        # 已知限制必须被诚实写下，而不是声称已解决。
        doc = PFA.__doc__ or ""
        self.assertIn("OPEN", doc)
        self.assertIn("REQUIRED", doc)
        self.assertIn("physical database origin", doc)
        self.assertIn("CLOSED", doc)
        self.assertNotIn("all owner-origin provenance solved", doc)

        # 两步伪造（自造 fixture → owner public read → adapter）今天确实可以通过。
        forged = self._ref(self._facts()[0])
        self.assertIs(type(forged), ARC.ResearchEvidenceRef)
        self.assertIs(True, forged.is_verified)


if __name__ == "__main__":
    unittest.main()
