# -*- coding: utf-8 -*-
"""R27-B2C-4C —— ``pnl_attribution`` canonical typed runtime 的永久回归。

存在理由是这条不变量：

    **盈亏归因只能由三个 owner 各自**已证明**的 typed 事实组合出来：**
    execution（``ExecutionFactProjection``）、portfolio/accounting
    （``PortfolioFactProjection``）、market（R24 ``MarketDataReading``）。
    业务日必须由调用边界**显式声明**，组合层不得自己读 legacy 表
    （``paper_nav`` / ``paper_positions`` / ``paper_orders``）、不得推断业务日、
    也不得把"拿不到"补成零或当前值。

分组：

    PNL-00          非空性：组合真的产出了可观察结果
    PNL-01 ~ 02     业务日与 context：asof 只来自显式请求；缺 context fail closed
    PNL-03 ~ 08     execution owner typed fact（adapter 核验、payload 逐字段、
                    unknown 费用不得补零、known 零是合法事实）
    PNL-09 ~ 11     portfolio/accounting owner typed fact（已实现盈亏 / 成本 / 现金）
    PNL-12 ~ 14     legacy 表不是 authority（``paper_positions`` / ``paper_nav``）
    PNL-15 ~ 23     market owner（attribution policy / 只读 / as-of / 核验语义）
    PNL-24 ~ 25     canonical ``InformationEvent``（kind / verification 派生，
                    payload 不得覆盖）
    PNL-26 ~ 30     架构边界（生产调用点、无 legacy SQL fallback、展示元数据不是证据）
    PNL-31          历史行情缺口 fail closed
    PNL-32          stale-but-available 行情切片必须发布新鲜度（不得只看 availability）
    PNL-33          归因目标的唯一判据是 cycle 绑定，不是账户生命周期状态
                    （cycle ownership ≠ execution eligibility）
    PNL-34 ~ 36     编排边界必须显式声明 PIT context：签发口无参即失败；业务日必须**同时**是
                    完成交易日**且**等于声明的本日历日（否则 fail closed，绝不退到上一交易日）；

全部离线：临时 SQLite 账本 + owner 自己的 public read + 被 patch 的 R24 缓存事实。
不连真实库、不联网、不读墙钟（业务日固定为 ``DAY`` / ``NEXT``）。
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_engine as AE  # noqa: E402
import ai_research_contract as ARC  # noqa: E402
import ai_research_execution_adapter as XEA  # noqa: E402
import ai_research_portfolio_adapter as PFA  # noqa: E402
import deepseek_research as DS  # noqa: E402
import execution_evidence as EE  # noqa: E402
import execution_verification as EV  # noqa: E402
import market_data_contract as MDC  # noqa: E402
import market_data_service as MDS  # noqa: E402
import paper_portfolio_read_model as PPRM  # noqa: E402
import paper_position_read_model as PPOS  # noqa: E402
import paper_trading as PT  # noqa: E402
import paper_trading_rules as PTR  # noqa: E402
import universe as U  # noqa: E402

ACCOUNT = next(iter(PT.ACCOUNT_SPECS))
OTHER_ACCOUNT = next(item for item in PT.ACCOUNT_SPECS if item != ACCOUNT)
CODE = "600519"
OTHER_CODE = "600000"
THIRD_CODE = "000001"
DAY = dt.date(2026, 9, 20)
NEXT = DAY + dt.timedelta(days=1)
PREVIOUS = DAY - dt.timedelta(days=1)
DAY_TEXT = DAY.isoformat()
TZ = dt.timezone(dt.timedelta(hours=8))

#: ``pnl_attribution`` canonical 路径的**全部**函数。边界论断（"没有 legacy SQL"、
#: "没有 paper_nav"）必须对它们**整体**成立，而不是只对入口成立。
PNL_FUNCTIONS = (
    DS._pnl_evidence,
    DS._compose_pnl_attribution,
    DS._market_leg,
    DS._market_provenance,
    DS._execution_leg,
    DS._portfolio_leg,
    DS._matches_target,
    DS._evidence_field_state,
)

#: legacy 路径的越界记号：出现任何一个都说明某条读路径回退到了"自己读表"。
FORBIDDEN_PNL_TOKENS = (
    "paper_nav",
    "paper_positions",
    "paper_orders",
    "EV.VERIFIED_PREDICATE",
    "SELECT",
    "FROM",
)

#: SQL 关键字：只在**字符串常量**里出现（标识符视图看不见表名，必须单独查一次）。
SQL_STRING_MARKERS = ("SELECT", "FROM", "JOIN", "WHERE", "INSERT", "UPDATE", "DELETE")

#: 组合层本身：这一层**绝不允许**有 ``except`` 兜底分支（"读不到就退回 legacy"）。
#: ``_matches_target`` 刻意不在此列 —— 它唯一的 ``except`` 是类型守卫且**返回 False**
#: （排除该事实），不是回退；PNL-28 会单独把这个形状钉住。
COMPOSITION_FUNCTIONS = (
    DS._pnl_evidence,
    DS._compose_pnl_attribution,
    DS._market_leg,
    DS._market_provenance,
    DS._execution_leg,
    DS._portfolio_leg,
)

#: 我们**自己** patch 的市场缓存与请求，供需要重算 owner reading 的用例复用。
_MISSING = object()
_REAL_FACT_PROJECTION = EV.fact_projection
_REAL_READ_SNAPSHOT = MDS.read_snapshot
_REAL_PORTFOLIO_FOR_CONTEXT = PPRM.portfolio_for_context

#: 非空性对照说明：``_executable_source`` 抹掉字符串之后仍然必须看得见**标识符**
#: 形式的越界记号 —— 否则"错误文案里提到 ``max(paper_nav.nav_date)``"会被误当成
#: "代码真的读了 paper_nav"，而"真的读了"却可能被同一个 helper 静默放过。


class _BlankStringLiterals(ast.NodeTransformer):
    """把字符串常量换成占位符，保留代码结构。

    错误文案 / 日志里提到某张表**不等于**读了它；而"代码里真的出现 ``paper_nav``
    这个标识符 / 属性"才是越界证据。标识符、属性名、调用名一律原样保留。
    """

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value:
            return ast.copy_location(ast.Constant(value="…", kind=None), node)
        return node


def _executable_source(func) -> str:
    """模块级函数的**会执行**代码：docstring 与字符串常量都已抹掉。

    边界断言必须只看代码：本模块与 ``deepseek_research`` 的 docstring 刻意写清
    "为什么不再读 ``paper_nav``"，``_pnl_evidence`` 的错误文案里也逐字写着
    ``max(paper_nav.nav_date)`` —— 字符级子串搜索会把说明文字本身当成越界证据。
    """
    tree = ast.parse(inspect.getsource(func))
    body = list(tree.body[0].body)
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    stripped = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(stripped)
    return ast.unparse(_BlankStringLiterals().visit(stripped))


def _source_identifiers(func) -> set:
    """函数里出现的**标识符 / 属性名**集合（字符串常量刻意不计）。

    这是与字符串无关的越界判据：``paper_nav`` / ``paper_positions`` 只要在
    canonical 路径上被当作名字用过，就一定会出现在这里。
    """
    tree = ast.parse(inspect.getsource(func))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _control_token_probe(paper_positions=None) -> object:
    """非空性对照：可执行源码里**确实**含越界标识符。

    docstring 里的 ``SELECT FROM paper_orders`` 会被剥掉、字符串常量会被抹掉，
    因此这个探针靠**参数名**证明 helper 仍然看得见真正的越界引用。
    """
    return paper_positions


def _sql_control_probe() -> str:
    """非空性对照：函数体里的 SQL 字符串必须能被 :func:`_source_strings` 看见。

    这一条是"标识符视图"的必要补充：一条重新长出来的 legacy SQL 读路径，它的表名
    住在**字符串常量**里，抹掉字符串之后就只剩标识符视图看不出来 —— 所以必须同时
    对字符串常量做 SQL 关键字检查，否则"没有 legacy SQL"只是一句空话。
    """
    return "SELECT nav FROM paper_nav"


def _source_strings(func) -> tuple:
    """函数体里的**字符串常量**（docstring 除外）。"""
    tree = ast.parse(inspect.getsource(func))
    body = list(tree.body[0].body)
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return tuple(
        node.value for node in ast.walk(ast.Module(body=body, type_ignores=[]))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _numeric_leaves(value) -> list:
    """结构里所有数值叶子。

    "某个价格没有被搬进来"这类断言必须看**解析后的数值叶子**，而不是序列化后的子串：
    子串匹配会被无关数值碰撞（``"2199.0"`` 就含 ``"99.0"``），把 fixture 的正常调整
    变成假红，且失败时无法区分"真的泄漏"与"碰巧撞上"。
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            out.extend(_numeric_leaves(key))
            out.extend(_numeric_leaves(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_numeric_leaves(item))
        return out
    return []


def _pnl_source() -> str:
    """``pnl_attribution`` canonical 路径全部函数的可执行源码。"""
    return "\n".join(_executable_source(func) for func in PNL_FUNCTIONS)


def _pnl_identifiers() -> set:
    """``pnl_attribution`` canonical 路径全部函数用到的标识符 / 属性名。"""
    names = set()
    for func in PNL_FUNCTIONS:
        names |= _source_identifiers(func)
    return names


def _pnl_strings() -> tuple:
    """``pnl_attribution`` canonical 路径全部函数里的字符串常量。"""
    return tuple(item for func in PNL_FUNCTIONS for item in _source_strings(func))


def _reissue_projection(projection, **overrides):
    """用 owner 的私有签发口重发一条投影，只改指定的字段。

    用途：制造"owner 记录了这条 fact，但某个字段是 unknown"的**局部**缺口，
    让 fail-closed 行为可以被直接观察到（而不是只能靠改生产代码来验）。
    除被改的字段外其余字段逐字来自 owner 自己的投影。
    """
    fields = {
        item.name: getattr(projection, item.name)
        for item in dataclasses.fields(EV.ExecutionFactProjection)
    }
    fields.update(overrides)
    return EV._issue_fact_projection(**fields)


def _projection_override(**overrides):
    """把 owner 投影改掉若干字段后再交给组合层。"""
    def _patched(evidence, **kwargs):
        return _reissue_projection(_REAL_FACT_PROJECTION(evidence, **kwargs), **overrides)

    return mock.patch.object(EV, "fact_projection", side_effect=_patched)


def _call_sites(tree: ast.Module, symbol: str) -> dict:
    """``{enclosing_function: 调用次数}`` —— 源码里对该符号的**调用**点。

    只统计调用（``Call``），不统计定义 / 字符串 / import：本轮的论断是"谁在**调用**
    owner factory"，而 import 与 docstring 不是调用点。
    """
    sites: dict = {}

    def walk(node, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else ""
                )
                if name == symbol:
                    sites[enclosing] = sites.get(enclosing, 0) + 1
            walk(child, enclosing)

    walk(tree, "")
    return sites


def _production_modules() -> list:
    """``backend`` 下的生产模块（测试文件本身不算生产调用点）。"""
    return sorted(
        name for name in os.listdir(BACKEND)
        if name.endswith(".py") and not name.startswith("test_")
    )


def _module_text(name) -> str:
    with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
        return handle.read()


def _attribution_request_constructor_modules(sources=None) -> set:
    """生产模块里**构造** ``AttributionRequest`` 的集合（``sources`` 供非空性对照使用）。

    只看 ``ast.Call``：类的定义、docstring 与字符串里的写法都不算构造点。
    """
    if sources is None:
        sources = {name: _module_text(name) for name in _production_modules()}
    holders = set()
    for name, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (func.id if isinstance(func, ast.Name)
                      else func.attr if isinstance(func, ast.Attribute) else None)
            if called == "AttributionRequest":
                holders.add(name)
    return holders


class PnlAttributionTypedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "paper.sqlite3")
        self._patchers = (
            mock.patch.object(PT, "DB_PATH", self.path),
            mock.patch.object(PT, "_RUNBOOK_BOOT", None, create=True),
            #: 编排边界的 attribution context 签发口读的是 paper DB 的**当前**绑定，
            #: 因此必须指向本用例的临时账本（否则会读真实库）。
            mock.patch.object(AE, "PAPER_DB_PATH", self.path),
        )
        for patcher in self._patchers:
            patcher.start()
        PT.init_db()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.cycle = self._cycle("r27b2c4c-c1", "running")
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle, ACCOUNT)
        )
        self.conn.commit()
        self._attach(ACCOUNT)
        self.initial_cash = float(self.conn.execute(
            "SELECT initial_cash FROM paper_accounts WHERE id=?", (ACCOUNT,)
        ).fetchone()[0])
        self.refresh_mock = None
        self.last_attribution = None
        self.last_market_snapshot = None

    def tearDown(self):
        self.conn.close()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self.tmp.cleanup()

    # ------------------------------- fixtures -------------------------------

    def _cycle(self, key, status, capital=100000.0, created=DAY):
        stamp = f"{created.isoformat()} 09:00:00"
        return int(self.conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,"
            "updated_at,started_at) VALUES(?,?,?,?,?,?,?)",
            (key, status, capital, "shared_pool", stamp, stamp,
             stamp if status == "running" else None),
        ).lastrowid)

    def _attach(self, account_id=ACCOUNT, *, effective=DAY, cycle=None):
        """写入 account 属于该 cycle 的**有界挂载证据**（parameter version）。"""
        day = effective.isoformat()
        self.conn.execute(
            "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,"
            "reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cycle or self.cycle, account_id, "1", "default", "{}", "r27b2c4c",
             day, f"{day} 09:00:00"),
        )
        self.conn.commit()

    @staticmethod
    def _model_fees(side, amount):
        """仓库权威费用模型下的**对得上**的费用（否则 owner 会报不一致）。"""
        commission = float(PTR.commission(amount))
        if str(side) == EE.SIDE_SELL:
            return commission + float(amount) * float(PTR.STAMP_SELL)
        return commission

    def _order_and_fill(self, *, cycle_id, side, qty, price, fill_date, code=CODE,
                        verified=True, realized_pnl=None, fees=None, account=ACCOUNT):
        amount = qty * price
        if fees is None:
            fees = self._model_fees(side, amount)
        stamp = PT._strategy_stamp(self.conn, account)
        order_id = int(self.conn.execute(
            "INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,"
            "filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at,"
            "order_type,origin,strategy_id,strategy_version,strategy_checksum,cycle_id,"
            "execution_status,execution_verified,realized_pnl) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account, side, code, "测试股", qty, price, price, amount, fees, "filled",
             "r27b2c4c-test", "{}", f"{fill_date} 09:30:00", f"{fill_date} 09:30:01",
             "market", "seed", *stamp, cycle_id,
             "verified" if verified else "unknown", 1 if verified else 0, realized_pnl),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,"
            "fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, account, side, code, qty, price, amount, fees, fill_date,
             f"{fill_date} 09:30:00", "r27b2c4c-test"),
        )
        return order_id

    def _lot(self, cycle_id, qty, cost, *, acquired_at=None, remaining_qty=None,
             source_order_id=None, account_id=ACCOUNT, code=CODE):
        acquired_at = acquired_at or f"{DAY.isoformat()} 10:00:00"
        remaining_qty = qty if remaining_qty is None else remaining_qty
        return int(self.conn.execute(
            "INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,"
            "remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,"
            "cost_fee_included,is_t_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, account_id, code, "测试股", "测试", qty, remaining_qty, cost,
             acquired_at, NEXT.isoformat(), "stock_t1", source_order_id, 1, 1),
        ).lastrowid)

    def _paper_nav(self, nav_date, nav, *, account_id=ACCOUNT, quote_status="verified"):
        """legacy 净值行：本轮**不得**被任何 canonical 读路径消费。"""
        self.conn.execute(
            "INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,"
            "created_at,quote_status) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, nav_date, 123456.0, 654321.0, nav, None,
             f"{nav_date} 15:30:00", quote_status),
        )
        self.conn.commit()

    def _buy_with_lot(self, *, qty=100, price=10.0, code=CODE, cycle_id=None, account=ACCOUNT):
        """一笔已验证 BUY + 它的 durable lot（费用与权威模型对账）。"""
        cycle = self.cycle if cycle_id is None else cycle_id
        order_id = self._order_and_fill(
            cycle_id=cycle, side="buy", qty=qty, price=price, code=code,
            fill_date=DAY.isoformat(), account=account,
        )
        self._lot(cycle, qty, price, source_order_id=order_id, account_id=account, code=code)
        self.conn.commit()
        return order_id

    def _verified_sell(self, *, qty=30, price=12.0, code=CODE, realized_pnl=59.5):
        order_id = self._order_and_fill(
            cycle_id=self.cycle, side="sell", qty=qty, price=price, code=code,
            fill_date=DAY.isoformat(), realized_pnl=realized_pnl,
        )
        self.conn.commit()
        return order_id

    def _happy_fixture(self):
        """最小但完整的一天：已验证 BUY(+lot) + 已验证 SELL。"""
        buy = self._buy_with_lot()
        self._verified_sell()
        return buy

    @staticmethod
    def _snapshot(*, as_of=None, observed_at=None,
                  price=10.5, code=CODE, verification=MDC.VERIFICATION_VERIFIED,
                  method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY, complete=True):
        """一条 R24 缓存事实（离线构造，绝不触网）。"""
        as_of = DAY_TEXT if as_of is None else as_of
        observed_at = f"{DAY_TEXT} 15:55:00+08:00" if observed_at is None else observed_at
        return MDC.MarketDataSnapshot(
            kind=MDS.KIND_FULL_MARKET_SNAPSHOT,
            rows=({"code": code, "price": price},),
            as_of=as_of, observed_at=observed_at, saved_at=observed_at,
            source="test_fixture", complete=complete, expected_rows=1,
            verification=verification, verification_method=method,
        )

    def _request(self, *, asof_day=DAY, market_now=None, targets=None):
        return DS.AttributionRequest(
            asof_day=asof_day.isoformat(),
            market_now=market_now or dt.datetime(
                asof_day.year, asof_day.month, asof_day.day, 16, 0, tzinfo=TZ,
            ),
            targets=targets or ((ACCOUNT, self.cycle),),
        )

    def _compose(self, *, market=_MISSING, attribution=None, read_spy=None):
        """走**公开**入口 ``collect``，并且永不触网。

        市场腿始终由 :func:`market_data_service._load_cached_snapshot` 的 patch 供料；
        ``refresh_snapshot`` 一律被换成断言（研究路径**不得**刷新）。
        """
        attribution = attribution or self._request()
        snapshot = self._snapshot() if market is _MISSING else market
        self.last_attribution = attribution
        self.last_market_snapshot = snapshot
        with mock.patch.object(
            MDS, "refresh_snapshot",
            side_effect=AssertionError("pnl_attribution must never refresh market data"),
        ) as refresh:
            self.refresh_mock = refresh
            with mock.patch.object(MDS, "_load_cached_snapshot", return_value=snapshot):
                if read_spy is None:
                    return DS.collect(
                        "pnl_attribution", self.conn, self.path, attribution=attribution,
                    )
                with mock.patch.object(MDS, "read_snapshot", side_effect=read_spy):
                    return DS.collect(
                        "pnl_attribution", self.conn, self.path, attribution=attribution,
                    )

    def _owner_facts(self, *, asof=DAY, account=ACCOUNT, cycle=None):
        context = PPRM.PortfolioReadContext(cycle or self.cycle, asof)
        return PPRM.accounting_fact_projections(self.conn, context, account_id=account)

    def _fact_value(self, kind, *, asof=DAY, account=ACCOUNT):
        for projection in self._owner_facts(asof=asof, account=account):
            if projection.fact_kind == kind:
                value = projection.value
                return value.as_dict() if isinstance(value, PPRM.PositionCostSummary) else value
        raise AssertionError(f"owner published no {kind} fact")

    def _owner_projections(self, *, asof_day=DAY, account=ACCOUNT, cycle=None):
        """目标 (account, cycle, business_day) 上 owner 自认的 execution 投影。"""
        projections = []
        for evidence in EE.load_execution_evidence(self.conn, account_id=account, limit=500):
            projection = EV.fact_projection(evidence)
            if DS._matches_target(projection, account, cycle or self.cycle, asof_day.isoformat()):
                projections.append(projection)
        return projections

    def _market_reading(self, *, market=_MISSING, attribution=None):
        """用同一次 patch 重算 R24 reading —— 供"owner 谓词"类断言复用。"""
        attribution = attribution or self.last_attribution or self._request()
        snapshot = self.last_market_snapshot if market is _MISSING else market
        with mock.patch.object(MDS, "_load_cached_snapshot", return_value=snapshot):
            return MDS.read_snapshot(
                MDC.ATTRIBUTION_POLICY, now=attribution.market_now,
                asof_day=attribution.asof_day,
            )

    def _legacy_nav_dates(self, account=ACCOUNT):
        """该账户在 legacy ``paper_nav`` 上的净值日（升序）。"""
        return sorted(
            str(row[0]) for row in self.conn.execute(
                "SELECT nav_date FROM paper_nav WHERE account_id=?", (account,)
            )
        )

    def _account_row(self, result, account=ACCOUNT):
        rows = [row for row in result["accounts"] if row["account_id"] == account]
        self.assertEqual(1, len(rows), "expected exactly one account row")
        return rows[0]

    def _events_by_source(self, result, source_type):
        return [
            event for event in result["canonical_events"]
            if event["source_type"] == source_type
        ]

    # --------------------------- PNL-00：非空性 ---------------------------

    def test_PNL_00_suite_is_not_vacuous(self):
        """PNL-00：组合真的产出了一条可观察结果（否则下面全部用例都是空转）。"""
        self._happy_fixture()
        result = self._compose()

        self.assertEqual("paper_trading_only", result["scope"])
        self.assertEqual("pnl_attribution", result["purpose"])
        self.assertEqual(DAY.isoformat(), result["asof"])
        self.assertEqual("explicit_attribution_request", result["asof_source"])
        self.assertEqual(
            [{"account_id": ACCOUNT, "cycle_id": self.cycle}], result["targets"],
        )
        self.assertTrue(result["accounts"], "组合必须产出一条账户行")
        self.assertTrue(result["filled_trades"], "happy fixture 必须有成交行")
        self.assertTrue(result["canonical_events"], "happy fixture 必须有 canonical event")
        self.assertEqual(result["event_count"], len(result["canonical_events"]))
        self.assertEqual(
            {
                ARC.EVIDENCE_SOURCE_EXECUTION,
                ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH,
                ARC.EVIDENCE_SOURCE_MARKET_DATA,
            },
            {event["source_type"] for event in result["canonical_events"]},
            "三类 owner 的 typed event 必须都真的进入 runtime",
        )

    # ------------------- PNL-01 ~ 02：业务日与显式 context -------------------

    def test_PNL_01_asof_never_derived_from_paper_nav(self):
        """PNL-01：``asof`` 只来自显式请求，**绝不**从 ``paper_nav`` 推断。

        legacy 路径用 ``max(paper_nav.nav_date)`` 自己推业务日 —— 那等于让被解释的
        数据决定解释的口径。这里放两行**不等于**请求日的 legacy 净值（其中一行还晚于
        请求日，另一行更早），组合层的 ``asof`` 必须纹丝不动。
        """
        self._happy_fixture()
        self._paper_nav(PREVIOUS.isoformat(), 19000.0)
        self._paper_nav(NEXT.isoformat(), 21000.0)
        legacy_days = self._legacy_nav_dates()
        self.assertIn(PREVIOUS.isoformat(), legacy_days)
        self.assertIn(NEXT.isoformat(), legacy_days)
        self.assertGreaterEqual(len(legacy_days), 2)

        result = self._compose()
        self.assertEqual(DAY.isoformat(), result["asof"])
        self.assertEqual("explicit_attribution_request", result["asof_source"])
        self.assertNotIn(NEXT.isoformat(), result["asof"])
        self.assertNotIn(PREVIOUS.isoformat(), result["asof"])

        body = _pnl_source()
        self.assertNotIn("paper_nav", body)
        # 只禁掉"用 max(...) 从 paper_nav 推 asof"这一种用法。禁掉整条 ``max(`` 记号会
        # 把任何无关的合法用法（例如 ``max(1, int(limit))``）也判红，只增加易碎性、
        # 不增加判别力 —— 同一不变量已由标识符视图与行为断言覆盖。
        self.assertNotIn("max(paper_nav", body)
        self.assertNotIn("paper_nav", _pnl_identifiers())
        # 非空性：剥 docstring / 抹字符串是有效的 —— 原文（含错误文案）确实提到
        # paper_nav，剥掉后没有；而标识符集合会看见真正的引用（见 PNL-12 的对照）。
        raw = inspect.getsource(DS._pnl_evidence)
        self.assertIn("paper_nav", raw)
        self.assertNotIn("paper_nav", _executable_source(DS._pnl_evidence))
        self.assertIn(
            "paper_positions", _source_identifiers(_control_token_probe),
            "非空性对照：helper 必须看得见标识符形式的越界记号",
        )

    def test_PNL_02_explicit_context_is_mandatory(self):
        """PNL-02：没有显式 ``AttributionRequest`` 时 fail closed，而不是猜一个业务日。"""
        with self.assertRaises(ValueError) as caught:
            DS.collect("pnl_attribution", self.conn, self.path)
        self.assertIn(DS.PNL_UNAVAILABLE_NO_CONTEXT, str(caught.exception))
        self.assertEqual("attribution_context_required", DS.PNL_UNAVAILABLE_NO_CONTEXT)

        with self.assertRaises(TypeError):
            DS._pnl_evidence(self.conn, self.path, object())
        with self.assertRaises(TypeError):
            DS.collect("pnl_attribution", self.conn, self.path, attribution={"asof_day": "x"})

        market_now = dt.datetime(2026, 9, 20, 16, 0, tzinfo=TZ)
        rejected = (
            {"asof_day": "2026-9-20", "market_now": market_now, "targets": ((ACCOUNT, 1),)},
            {"asof_day": "not-a-day", "market_now": market_now, "targets": ((ACCOUNT, 1),)},
            {"asof_day": "2026-09-20 00:00:00", "market_now": market_now,
             "targets": ((ACCOUNT, 1),)},
            {"asof_day": DAY.isoformat(), "market_now": dt.datetime(2026, 9, 20, 16, 0),
             "targets": ((ACCOUNT, 1),)},
            {"asof_day": DAY.isoformat(), "market_now": "2026-09-20T16:00:00+08:00",
             "targets": ((ACCOUNT, 1),)},
            {"asof_day": DAY.isoformat(), "market_now": market_now, "targets": ()},
            {"asof_day": DAY.isoformat(), "market_now": market_now, "targets": None},
            {"asof_day": DAY.isoformat(), "market_now": market_now, "targets": (("", 1),)},
            {"asof_day": DAY.isoformat(), "market_now": market_now, "targets": ((ACCOUNT, 0),)},
            {"asof_day": DAY.isoformat(), "market_now": market_now,
             "targets": ((ACCOUNT, -3),)},
        )
        for kwargs in rejected:
            with self.subTest(kwargs={key: repr(value) for key, value in kwargs.items()}):
                with self.assertRaises(ValueError):
                    DS.AttributionRequest(**kwargs)

        # 非空性对照：合法 context 必须被接受（否则上面可能只是"构造器恒抛"）。
        accepted = self._request()
        self.assertEqual(DAY.isoformat(), accepted.asof_day)
        self.assertEqual(((ACCOUNT, self.cycle),), accepted.targets)
        self.assertIs(market_now.tzinfo, accepted.market_now.tzinfo)

    # ------------------- PNL-03 ~ 08：execution owner typed fact -------------------

    def test_PNL_03_execution_rows_go_through_owner_projection_and_adapter(self):
        """PNL-03：成交行来自 owner 投影 + adapter，核验判据是 ``ResearchEvidenceRef``。

        ``is_verified`` 必须等于同一条 owner 事实经
        ``evidence_ref_from_execution_projection`` 得到的 ``ref.is_verified``，
        **不是** ``verification["is_verified"]``（那是"整单是否完整成交"）。
        """
        self._buy_with_lot()
        result = self._compose()

        self.assertTrue(result["filled_trades"])
        expected = {}
        for projection in self._owner_projections():
            ref = XEA.evidence_ref_from_execution_projection(projection)
            expected[(projection.identity_kind, projection.identity)] = ref.is_verified
        self.assertEqual(1, len(expected), "fixture 只应有一条匹配的 execution fact")

        for row in result["filled_trades"]:
            with self.subTest(identity=row["identity"]):
                key = (row["identity_kind"], row["identity"])
                self.assertIn(key, expected)
                self.assertIs(expected[key], row["is_verified"])
        self.assertTrue(
            any(row["is_verified"] for row in result["filled_trades"]),
            "已验证 BUY（费用与权威模型对账）必须被 adapter 判为可信事实",
        )
        self.assertEqual(
            1, len(self._events_by_source(result, ARC.EVIDENCE_SOURCE_EXECUTION)),
            "一条 execution fact 恰好产出一个 canonical event",
        )

    def test_PNL_04_filled_trade_payload_comes_from_typed_fact(self):
        """PNL-04：成交 payload 的每个值逐字等于 owner 的 ``EvidenceField``。

        ``amount`` 是**派生展示值**（owner contract 没有这一列），只有成交数量与
        成交价格都 owner-known 时才允许派生，并必须标注它不是 owner raw fact。
        ``paper_orders.name`` 这类兼容列绝不进入 typed payload。
        """
        self._buy_with_lot()
        self.assertEqual(
            "测试股",
            self.conn.execute("SELECT name FROM paper_orders").fetchone()[0],
            "非空性：兼容列 name 真的有值，泄漏出来就会被看见",
        )
        result = self._compose()
        self.assertEqual(1, len(result["filled_trades"]))
        row = result["filled_trades"][0]

        projection = self._owner_projections()[0]
        for name in ("code", "action", "requested_qty", "filled_qty", "fill_price", "fees",
                     "business_day", "observed_at"):
            with self.subTest(field=name):
                self.assertEqual(getattr(projection, name).maybe(), row[name])
        for name in EV.EXECUTION_FACTUAL_FIELDS:
            with self.subTest(state=name):
                self.assertEqual(getattr(projection, name).state, row["field_states"][name])
        self.assertEqual(set(EV.EXECUTION_FACTUAL_FIELDS), set(row["field_states"]))
        self.assertEqual(
            {"code", "action", "requested_qty", "filled_qty", "fill_price", "fees"},
            set(EV.EXECUTION_FACTUAL_FIELDS),
        )

        self.assertEqual(
            round(float(projection.filled_qty.require()) * float(projection.fill_price.require()), 4),
            row["amount"],
        )
        self.assertEqual("derived_filled_qty_times_fill_price", row["amount_basis"])
        self.assertEqual("research_evidence_ref", row["verified_by"])

        self.assertNotIn("name", row)
        events = self._events_by_source(result, ARC.EVIDENCE_SOURCE_EXECUTION)
        self.assertEqual(1, len(events))
        self.assertNotIn("name", events[0]["payload_fields"])
        self.assertNotIn("paper_orders.name", json.dumps(result, default=str))
        self.assertNotIn("测试股", json.dumps(result, ensure_ascii=False, default=str))

    def test_PNL_05_unknown_business_day_never_falls_back_to_legacy_timestamps(self):
        """PNL-05：owner 的 ``business_day`` unknown ⇒ 该委托**被排除**。

        ``paper_orders`` 没有交易日列，因此一次被撤/被拒的委托今天报 ``unknown``。
        组合层不得用 ``executed_at[:10]`` 之类的墙钟时间戳替它补一个业务日 ——
        那会把一条不可证明的事实放进一个它并不一定属于的归因日。
        """
        self._buy_with_lot()
        executed_at = str(self.conn.execute(
            "SELECT executed_at FROM paper_orders"
        ).fetchone()[0])
        self.assertTrue(
            executed_at.startswith(DAY.isoformat()),
            "非空性：legacy 时间戳**恰好**落在请求日，回退就会看起来成功",
        )

        # 对照：owner 明确记录了业务日时，这条委托必须进入归因。
        control = self._compose()
        self.assertEqual(1, len(control["filled_trades"]))
        self.assertEqual(DAY.isoformat(), control["filled_trades"][0]["business_day"])

        with _projection_override(business_day=EE.EvidenceField.unknown("business_day")):
            blocked = self._compose()
        self.assertEqual([], blocked["filled_trades"])
        self.assertEqual([], self._events_by_source(blocked, ARC.EVIDENCE_SOURCE_EXECUTION))
        portfolio_events = self._events_by_source(blocked, ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH)
        self.assertEqual(
            len(portfolio_events) + 1,
            blocked["event_count"],
            "目标日只剩下 portfolio 事实与 market 事实两条腿",
        )
        for token in ("executed_at", "created_at"):
            with self.subTest(token=token):
                self.assertNotIn(token, _executable_source(DS._matches_target))
                self.assertNotIn(token, _executable_source(DS._execution_leg))

    def test_PNL_06_account_or_cycle_mismatch_is_excluded_fail_closed(self):
        """PNL-06：另一个 cycle / 另一个 account 的委托不得被重绑定到目标上。"""
        other_cycle = self._cycle("r27b2c4c-c2", "running")
        target = self._buy_with_lot(code=CODE)
        other_cycle_order = self._buy_with_lot(code=OTHER_CODE, cycle_id=other_cycle)
        other_account_order = self._buy_with_lot(code=THIRD_CODE, account=OTHER_ACCOUNT)

        rows = {
            int(row["id"]): (str(row["account_id"]), int(row["cycle_id"]), str(row["code"]))
            for row in self.conn.execute("SELECT id,account_id,cycle_id,code FROM paper_orders")
        }
        # 非空性：三笔委托都真的存在，且另外两笔确实不属于本次归因目标。
        self.assertEqual(3, len(rows))
        self.assertEqual((ACCOUNT, self.cycle, CODE), rows[target])
        self.assertEqual((ACCOUNT, other_cycle, OTHER_CODE), rows[other_cycle_order])
        self.assertEqual((OTHER_ACCOUNT, self.cycle, THIRD_CODE), rows[other_account_order])

        result = self._compose()
        self.assertEqual([CODE], [row["code"] for row in result["filled_trades"]])
        self.assertEqual(1, len(self._events_by_source(result, ARC.EVIDENCE_SOURCE_EXECUTION)))
        for row in result["accounts"]:
            self.assertEqual(ACCOUNT, row["account_id"])
            self.assertEqual(self.cycle, row["cycle_id"])
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn(OTHER_CODE, serialized)
        self.assertNotIn(THIRD_CODE, serialized)

    def test_PNL_07_unknown_fees_are_never_zero_filled(self):
        """PNL-07：owner 费用 unknown ⇒ 组合层 ``fees is None``，绝不补成 0。

        把"没证明费用"发布成"没有费用"是同一类错误的另一个方向：它会让一份
        归因报告看起来比它实际能证明的更完整。
        """
        self._buy_with_lot()
        control = self._compose()
        self.assertEqual("known", control["fees_availability"])
        self.assertAlmostEqual(self._model_fees(EE.SIDE_BUY, 1000.0), control["fees"], places=6)

        with _projection_override(fees=EE.EvidenceField.unknown("fees")):
            result = self._compose()
        self.assertTrue(result["filled_trades"], "资金腿仍应有成交行（排除未发生）")
        row = result["filled_trades"][0]
        self.assertEqual("unknown", row["field_states"]["fees"])
        self.assertIsNone(row["fees"])
        self.assertIsNone(result["fees"])
        self.assertNotEqual("known", result["fees_availability"])
        self.assertEqual(DS.PNL_UNAVAILABLE_OWNER_FACT, result["fees_availability"])
        self.assertNotEqual(0.0, result["fees"])
        # 现金腿与费用腿是两件事：费用不可证明不影响 portfolio owner 的现金事实。
        self.assertIsNotNone(self._account_row(result)["cash"])

    def test_PNL_08_known_zero_fees_are_a_legal_verified_zero(self):
        """PNL-08：owner 明确记录的 ``fees == 0.0`` 是**合法事实**，不得退化成 unknown。"""
        self._buy_with_lot()
        self.conn.execute("UPDATE paper_orders SET fees=0.0")
        self.conn.execute("UPDATE paper_fills SET fees=0.0")
        self.conn.commit()

        projection = self._owner_projections()[0]
        self.assertTrue(projection.fees.is_known, "owner 必须把记录的 0 发布为 known")
        self.assertEqual(0.0, projection.fees.value)

        result = self._compose()
        row = result["filled_trades"][0]
        self.assertEqual("known", row["field_states"]["fees"])
        self.assertEqual(0.0, row["fees"])
        self.assertEqual(0.0, result["fees"])
        self.assertEqual("known", result["fees_availability"])
        # 费用为零是"确认没有"，不是"读不出来"：二者不得压平。
        self.assertIsNotNone(result["fees"])
        self.assertNotEqual(DS.PNL_UNAVAILABLE_OWNER_FACT, result["fees_availability"])

    # ------------------ PNL-09 ~ 11：portfolio owner typed fact ------------------

    def test_PNL_09_realized_pnl_comes_from_portfolio_fact(self):
        """PNL-09：已实现盈亏来自 ``PORTFOLIO_FACT_REALIZED_PNL``，不是组合层自己扫表。

        这个列的 authority 属于 portfolio owner：owner 把**有界且已验证**的 SELL
        事实（``verified SELL execution facts``）折算成一条 typed fact。组合层不得
        自己写第二套 SQL，也不得让 owner **不统计**的行（BUY / 别的 cycle）移动结果。
        """
        buy = self._happy_fixture()
        result = self._compose()
        expected = self._fact_value(PPRM.PORTFOLIO_FACT_REALIZED_PNL)
        self.assertEqual(59.5, expected)
        self.assertEqual(round(expected, 4), result["realized_pnl"])
        self.assertEqual("known", result["realized_pnl_availability"])

        portfolio_events = self._events_by_source(result, ARC.EVIDENCE_SOURCE_PORTFOLIO_RESEARCH)
        self.assertIn(
            f"{PPRM.PORTFOLIO_FACT_REALIZED_PNL}|cycle={self.cycle}|account={ACCOUNT}",
            [event["source_id"] for event in portfolio_events],
            "已实现盈亏必须作为一条 canonical portfolio 事件被发布",
        )

        # owner **不统计**的行：BUY 行的 realized_pnl 与别的 cycle 的 SELL。
        other_cycle = self._cycle("r27b2c4c-c2", "running")
        other_order = self._order_and_fill(
            cycle_id=other_cycle, side="sell", qty=5, price=12.0, fill_date=DAY.isoformat(),
            realized_pnl=7777.0,
        )
        self.conn.execute("UPDATE paper_orders SET realized_pnl=? WHERE id=?", (8888.0, buy))
        self.conn.commit()
        raw_column_sum = float(self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM paper_orders WHERE cycle_id=?",
            (self.cycle,),
        ).fetchone()[0])
        self.assertNotEqual(round(expected, 4), round(raw_column_sum, 4))

        unchanged = self._compose()
        self.assertEqual(round(expected, 4), unchanged["realized_pnl"])
        self.assertAlmostEqual(
            expected, self._fact_value(PPRM.PORTFOLIO_FACT_REALIZED_PNL), places=6,
        )
        self.assertEqual(
            round(self._fact_value(PPRM.PORTFOLIO_FACT_REALIZED_PNL), 4),
            unchanged["realized_pnl"],
        )
        # 非空性：这两行真的存在，且**确实**带着被改写的 realized_pnl。
        mutated = {
            int(row[0]): float(row[1]) for row in self.conn.execute(
                "SELECT id,realized_pnl FROM paper_orders WHERE id IN (?,?)", (buy, other_order)
            )
        }
        self.assertEqual({buy: 8888.0, other_order: 7777.0}, mutated)

        # 反向对照：owner **统计**的那一笔 SELL 确实是它的输入 —— 组合值随 owner 走，
        # 而不是随任何别的路径走（否则上面的"不变"可能只是因为读了个常量）。
        sell = int(self.conn.execute(
            "SELECT id FROM paper_orders WHERE side='sell' AND cycle_id=?", (self.cycle,)
        ).fetchone()[0])
        self.conn.execute("UPDATE paper_orders SET realized_pnl=? WHERE id=?", (100.0, sell))
        self.conn.commit()
        self.assertAlmostEqual(
            100.0, self._fact_value(PPRM.PORTFOLIO_FACT_REALIZED_PNL), places=6,
        )
        self.assertEqual(100.0, self._compose()["realized_pnl"])

    def test_PNL_10_position_cost_summary_comes_from_portfolio_fact(self):
        """PNL-10：持仓成本摘要来自 ``PORTFOLIO_FACT_POSITION_COST_SUMMARY``。

        ``paper_positions`` 是 compatibility-only 投影（R22），它不得影响组合层发布
        的成本摘要 —— 组合层发布的是 owner 从 durable lots 派生的那一份。
        """
        self._happy_fixture()
        result = self._compose()
        expected = self._fact_value(PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY)
        self.assertEqual({"position_count": 1, "cost_value": 700.0}, expected)

        row = self._account_row(result)
        self.assertEqual(expected["position_count"], row["position_cost_summary"]["position_count"])
        self.assertEqual(expected["cost_value"], row["position_cost_summary"]["cost_value"])
        composed = [item for item in result["position_cost_summary"]
                    if item["account_id"] == ACCOUNT]
        self.assertEqual(1, len(composed))
        self.assertEqual(expected["cost_value"], composed[0]["cost_value"])
        self.assertEqual("portfolio_owner_typed_fact", composed[0]["authority"])

        self.conn.execute(
            "INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,"
            "available_date,asset_type,peak_price,take_stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ACCOUNT, CODE, "测试股", "测试", 999, 77.0, DAY.isoformat(),
             NEXT.isoformat(), "stock_t1", 0.0, 0),
        )
        self.conn.commit()
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0])
        after = self._compose()
        self.assertEqual(
            expected["position_count"],
            self._account_row(after)["position_cost_summary"]["position_count"],
        )
        self.assertEqual(expected["cost_value"], self._fact_value(
            PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY)["cost_value"])
        self.assertNotEqual(999 * 77.0, self._account_row(after)[
            "position_cost_summary"]["cost_value"])

    def test_PNL_11_cash_comes_from_portfolio_fact_not_current_account_state(self):
        """PNL-11：现金来自 ``PORTFOLIO_FACT_CASH`` 的**有界重建**。

        ``paper_accounts.cash`` 是当前可变状态，不是历史 as-of authority；改它不得
        改变一条归因事实，也不得让它看起来更新鲜。
        """
        self._buy_with_lot()
        result = self._compose()
        expected = self._fact_value(PPRM.PORTFOLIO_FACT_CASH)
        self.assertAlmostEqual(
            self.initial_cash - 1000.0 - self._model_fees(EE.SIDE_BUY, 1000.0), expected,
            places=6,
        )
        self.assertAlmostEqual(expected, self._account_row(result)["cash"], places=6)

        self.conn.execute("UPDATE paper_accounts SET cash=? WHERE id=?", (999999.0, ACCOUNT))
        self.conn.commit()
        self.assertEqual(999999.0, float(self.conn.execute(
            "SELECT cash FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone()[0]))
        after = self._compose()
        self.assertAlmostEqual(expected, self._account_row(after)["cash"], places=6)
        self.assertNotAlmostEqual(999999.0, self._account_row(after)["cash"])
        self.assertAlmostEqual(
            expected, self._fact_value(PPRM.PORTFOLIO_FACT_CASH), places=6,
        )

    # ------------- PNL-12 ~ 14：legacy 表不是 authority（positions / nav） -------------

    def test_PNL_12_canonical_path_never_reads_paper_positions(self):
        """PNL-12：canonical 路径不读 ``paper_positions``（兼容投影不是事实来源）。"""
        self._happy_fixture()
        self.assertNotIn("paper_positions", _pnl_source())
        self.assertNotIn("paper_positions", _pnl_identifiers())
        # 非空性对照：helper 真的看得见"标识符形式"的越界记号。
        probe = _source_identifiers(_control_token_probe)
        self.assertIn("paper_positions", probe)
        self.assertIn("paper_positions", _control_token_probe.__code__.co_varnames)
        # 组合真的跑过一遍（否则上面可能只是"函数根本没执行"）。
        self.assertTrue(self._compose()["accounts"])

    def test_PNL_13_paper_nav_is_not_a_market_authority(self):
        """PNL-13：``paper_nav`` 不是行情 authority：市场腿不可用时 NAV 就是 unavailable。"""
        self._buy_with_lot()
        self._paper_nav(DAY.isoformat(), 987654.0)
        self._paper_nav(NEXT.isoformat(), 987655.0)
        self.assertIn(DAY.isoformat(), self._legacy_nav_dates())

        result = self._compose(market=None)
        row = self._account_row(result)
        self.assertEqual("unavailable", row["latest_nav"]["availability"])
        self.assertIsNone(row["latest_nav"]["nav"])
        self.assertFalse(self._events_by_source(result, ARC.EVIDENCE_SOURCE_MARKET_DATA))
        self.assertNotIn("987654", json.dumps(result, default=str))

        self.assertNotIn("paper_nav", _pnl_source())
        self.assertNotIn("paper_nav", _pnl_identifiers())
        # 对照：R24 事实可用时 NAV 才可发布（说明上面的 unavailable 是"缺市场事实"，
        # 而不是"这个字段恒为 None"）。
        control = self._compose()
        control_nav = self._account_row(control)["latest_nav"]
        self.assertEqual("available", control_nav["availability"])
        self.assertIsNotNone(control_nav["nav"])
        self.assertNotEqual(987654.0, control_nav["nav"])

    def test_PNL_14_paper_nav_quote_status_never_enters_market_verification(self):
        """PNL-14：legacy ``paper_nav.quote_status`` 不得进入 market 核验维度。

        两个维度来源完全不同：市场核验只来自 R24 读数，拿不到时必须是 ``None``；
        legacy 净值行自称 ``verified`` 与"行情经过核验"是两件事。
        """
        self._buy_with_lot()
        self._paper_nav(DAY.isoformat(), 987654.0, quote_status="verified")
        legacy_status = self.conn.execute(
            "SELECT quote_status FROM paper_nav WHERE account_id=? AND nav_date=?",
            (ACCOUNT, DAY.isoformat()),
        ).fetchone()[0]
        self.assertEqual("verified", legacy_status, "非空性：legacy 行确实自称 verified")

        blocked = self._compose(market=None)
        self.assertIsNone(blocked["market"]["verification"])
        self.assertIsNone(blocked["market"]["verification_method"])
        self.assertIsNone(self._account_row(blocked)["latest_nav"]["market_verification"])
        self.assertIsNone(
            self._account_row(blocked)["latest_nav"]["market_verification_method"])

        single = self._snapshot(
            verification=MDC.VERIFICATION_SINGLE_SOURCE,
            method=MDC.VERIFICATION_METHOD_CROSS_SOURCE,
        )
        result = self._compose(market=single)
        nav = self._account_row(result)["latest_nav"]
        self.assertEqual(MDC.VERIFICATION_SINGLE_SOURCE, result["market"]["verification"])
        self.assertNotEqual(legacy_status, result["market"]["verification"])
        self.assertEqual(
            MDC.VERIFICATION_SINGLE_SOURCE, nav["market_verification"],
            "market 核验只能是 R24 读数，不能是 legacy 行的 quote_status",
        )
        self.assertEqual(MDC.VERIFICATION_METHOD_CROSS_SOURCE, nav["market_verification_method"])

    # ---------------------- PNL-15 ~ 23：market owner ----------------------

    def test_PNL_15_market_read_uses_attribution_policy(self):
        """PNL-15：market 腿用 ``MDC.ATTRIBUTION_POLICY`` 读，且窗口由它独占定义。"""
        self._buy_with_lot()
        captured = []

        def spy(policy, **kwargs):
            reading = _REAL_READ_SNAPSHOT(policy, **kwargs)
            captured.append((policy, kwargs, reading))
            return reading

        result = self._compose(read_spy=spy)
        self.assertEqual(1, len(captured), "一次归因只读一次市场事实")
        policy, kwargs, _reading = captured[0]
        self.assertIs(MDC.ATTRIBUTION_POLICY, policy)
        self.assertIsNot(MDC.LIVE_MARKET_POLICY, policy)
        self.assertEqual("trade_attribution", policy.name)
        self.assertEqual(MDC.ATTRIBUTION_POLICY.name, result["market"]["policy"])
        self.assertEqual(900.0, policy.max_age_seconds)
        self.assertEqual(DAY.isoformat(), kwargs["asof_day"])
        self.assertEqual(self.last_attribution.market_now, kwargs["now"])

    def test_PNL_16_market_read_is_read_only_and_never_refreshes(self):
        """PNL-16：研究路径**只读**，绝不触发 provider 刷新。"""
        self._buy_with_lot()
        readings = []

        def spy(policy, **kwargs):
            reading = _REAL_READ_SNAPSHOT(policy, **kwargs)
            readings.append(reading)
            return reading

        with mock.patch.object(
            MDS, "refresh_snapshot",
            side_effect=AssertionError("pnl_attribution must never refresh market data"),
        ):
            result = self._compose(read_spy=spy)
        self.assertTrue(result["accounts"])
        self.assertEqual(1, len(readings))
        self.assertEqual(MDC.ACCESS_READ, readings[0].access_mode)
        self.assertFalse(MDC.access_mode_allows_network(readings[0].access_mode))
        self.refresh_mock.assert_not_called()
        self.assertEqual(0, self.refresh_mock.call_count)

    def test_PNL_17_market_asof_mismatch_makes_valuation_unavailable(self):
        """PNL-17：as-of 对不上的市场事实不得用于估值。

        R24 的 PIT 规则是 ``observed_day > requested_day`` → ``asof_mismatch``
        （更早的观测对更晚的请求是允许的，见 ``MDPIT-04``）。缓存只能提供一个
        **晚于** 请求日的读数时，这条读数对本次归因没有任何解释力：
        估值必须整体 unavailable，并如实给出原因码。
        """
        self._buy_with_lot()
        later = self._snapshot(
            as_of=NEXT.isoformat(), observed_at=f"{NEXT.isoformat()} 09:10:00+08:00",
            price=99.0,
        )
        result = self._compose(market=later)
        self.assertEqual(MDC.AVAILABILITY_UNAVAILABLE, result["market"]["availability"])
        self.assertEqual(MDC.REASON_ASOF_MISMATCH, result["market"]["reason"])
        self.assertIn(result["market"]["reason"], MDC.REASONS)

        nav = self._account_row(result)["latest_nav"]
        self.assertEqual("unavailable", nav["availability"])
        self.assertIsNotNone(nav["reason"])
        self.assertIsNone(nav["nav"])
        self.assertIsNone(nav["market_value"])
        self.assertIsNone(nav["market_evidence_ref"])
        self.assertNotIn(99.0, _numeric_leaves(result),
                         "as-of mismatch 的报价不得出现在归因结构里")
        # 非空性：同一套扫描真的看得见一个存在的数值叶子。
        self.assertIn(99.0, _numeric_leaves({"price": 99.0}))

        control = self._compose()
        self.assertEqual(MDC.AVAILABILITY_AVAILABLE, control["market"]["availability"])
        self.assertIsNone(control["market"]["reason"])
        self.assertEqual("available", self._account_row(control)["latest_nav"]["availability"])
        self.assertIsNotNone(self._account_row(control)["latest_nav"]["nav"])

    def test_PNL_18_missing_market_leaves_nav_unavailable_not_cost_or_current(self):
        """PNL-18：没有市场事实 ⇒ NAV unavailable，且**不得**回落到成本或当前报价。"""
        self._buy_with_lot()
        result = self._compose(market=None)
        nav = self._account_row(result)["latest_nav"]
        self.assertEqual("unavailable", nav["availability"])
        self.assertEqual(MDC.REASON_MISSING, nav["reason"])
        self.assertIsNone(nav["nav"])
        self.assertIsNone(nav["market_value"])
        self.assertIsNone(nav["unrealized_pnl"])
        self.assertFalse(self._events_by_source(result, ARC.EVIDENCE_SOURCE_MARKET_DATA))

        # 非空性：成本与现金都真的有值 —— 回落到其中一个都会"看起来成功"。
        cost = self._fact_value(PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY)["cost_value"]
        cash = self._fact_value(PPRM.PORTFOLIO_FACT_CASH)
        self.assertGreater(cost, 0.0)
        self.assertGreater(cash, 0.0)
        self.assertIsNone(nav["nav"])

        control = self._compose()
        control_nav = self._account_row(control)["latest_nav"]
        self.assertIsNotNone(control_nav["nav"])
        self.assertNotEqual(cost, control_nav["nav"])
        self.assertNotEqual(cash, control_nav["nav"])

    def test_PNL_19_coverage_integrity_is_not_cross_source_verified(self):
        """PNL-19：``verified`` + ``coverage_integrity`` **不是**双源核验。"""
        snapshot = self._snapshot(
            verification=MDC.VERIFICATION_VERIFIED,
            method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
        )
        self.assertFalse(MDC.is_cross_source_verified(snapshot))
        result = self._compose(market=snapshot)
        self.assertEqual(MDC.VERIFICATION_VERIFIED, result["market"]["verification"])
        self.assertEqual(
            MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY, result["market"]["verification_method"],
        )
        self.assertIs(False, result["market"]["cross_source_verified"])
        nav = self._account_row(result)["latest_nav"]
        self.assertEqual(
            MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY, nav["market_verification_method"],
        )
        self.assertIs(False, nav["market_cross_source_verified"])

    def test_PNL_20_cross_source_metric_must_use_owner_predicate(self):
        """PNL-20：双源判据来自 owner 谓词，而不是 ``verification == "verified"``。

        两个快照的 ``verification`` **完全一样**（都是 ``verified``），只有 method
        不同；结论必须随之不同 —— 否则这个标志就是从 ``verification`` 猜出来的。
        """
        coverage = self._snapshot(method=MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY)
        cross = self._snapshot(method=MDC.VERIFICATION_METHOD_CROSS_SOURCE)
        self.assertEqual(coverage.verification, cross.verification)
        self.assertFalse(MDC.is_cross_source_verified(coverage))
        self.assertTrue(MDC.is_cross_source_verified(cross))

        self.assertIs(True, self._compose(market=cross)["market"]["cross_source_verified"])
        self.assertIs(False, self._compose(market=coverage)["market"]["cross_source_verified"])
        body = _executable_source(DS._market_provenance)
        self.assertIn("MDC.is_cross_source_verified", body)
        self.assertIn("is_cross_source_verified", _source_identifiers(DS._market_provenance))

    def test_PNL_21_portfolio_nav_status_cannot_alone_establish_market_trust(self):
        """PNL-21：组合侧的 ``nav_status == verified`` **不**等于市场已被核验。

        ``portfolio_for_context`` 的 ``nav_status`` 只说明"账本可重建 + 拿到了完整
        numeric valuations"，它不是市场核验（§20）。市场维度必须始终由 R24 读数
        单独发布；拿不到时是 ``None``，而不是"组合说没问题"。
        """
        self._happy_fixture()
        available = self._account_row(self._compose())["latest_nav"]
        self.assertEqual(PPRM.STATUS_VERIFIED, available["nav_status"])
        self.assertEqual(PPRM.STATUS_VERIFIED, available["market_value_status"])
        self.assertEqual(MDC.VERIFICATION_VERIFIED, available["market_verification"])
        self.assertEqual(
            MDC.VERIFICATION_METHOD_COVERAGE_INTEGRITY,
            available["market_verification_method"],
        )

        blocked = self._compose(market=None)
        row = self._account_row(blocked)
        provenance = row["owner_fact_provenance"]
        self.assertEqual(set(PPRM.PORTFOLIO_FACT_KINDS), set(provenance))
        self.assertEqual({PPRM.STATUS_VERIFIED}, {item["status"] for item in provenance.values()})
        self.assertTrue(all(item["is_verified"] for item in provenance.values()))
        self.assertIsNone(row["latest_nav"]["market_verification"])
        self.assertIsNone(row["latest_nav"]["market_verification_method"])
        self.assertNotIn(
            "nav_status", row["latest_nav"],
            "缺市场证据时组合层不得发布一个组合侧的 NAV 状态替市场背书",
        )
        # provenance 块必须照样发布（不可用不等于"不展示"），且必须同时带上新鲜度 ——
        # 只发布 availability 会让 stale-but-available 的切片与当日读数无从区分。
        self.assertEqual(
            {"availability", "reason", "freshness", "status", "age_seconds",
             "as_of", "observed_at", "verification", "verification_method",
             "cross_source_verified", "policy"},
            set(blocked["market"]),
        )
        self.assertEqual(MDC.AVAILABILITY_UNAVAILABLE, blocked["market"]["availability"])
        self.assertIn("market_cross_source_verified", row["latest_nav"])
        self.assertIs(False, row["latest_nav"]["market_cross_source_verified"])

    def test_PNL_22_caller_cannot_inject_a_bare_valuations_mapping(self):
        """PNL-22：调用方不能给组合层塞一个裸 ``valuations`` Mapping。

        否则 ``{"600519": 10.0}`` 就能冒充 canonical valuation evidence ——
        估值必须由组合层**自己**从 R24 读数构造。
        """
        signature = inspect.signature(DS._compose_pnl_attribution)
        self.assertEqual(["conn", "attribution"], list(signature.parameters))
        for forbidden in ("valuations", "valuation", "quotes", "quote", "prices", "price",
                          "latest", "nav"):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, signature.parameters)
        with self.assertRaises(TypeError):
            DS._compose_pnl_attribution(self.conn, self._request(), valuations={CODE: 10.5})
        for func in (DS._market_leg, DS._pnl_evidence):
            with self.subTest(function=func.__name__):
                self.assertNotIn("valuations", inspect.signature(func).parameters)

        captured = []

        def spy(conn, context, *, account_id=None, valuations=None):
            captured.append(valuations)
            return _REAL_PORTFOLIO_FOR_CONTEXT(
                conn, context, account_id=account_id, valuations=valuations,
            )

        self._buy_with_lot()
        with mock.patch.object(PPRM, "portfolio_for_context", side_effect=spy):
            self._compose()
        self.assertEqual(1, len(captured))
        self.assertEqual({CODE: 10.5}, captured[0])
        with mock.patch.object(PPRM, "portfolio_for_context", side_effect=spy):
            self._compose(market=self._snapshot(price=12.0))
        self.assertEqual({CODE: 12.0}, captured[-1], "估值必须来自 R24 读数而不是常量")

    def test_PNL_23_future_market_reading_cannot_explain_earlier_attribution(self):
        """PNL-23：晚于请求日的市场读数不得被用来解释更早的归因。"""
        self._buy_with_lot()
        future = self._snapshot(
            as_of=NEXT.isoformat(), observed_at=f"{NEXT.isoformat()} 09:10:00+08:00",
            price=99.0,
        )
        result = self._compose(market=future)
        self.assertFalse(self._events_by_source(result, ARC.EVIDENCE_SOURCE_MARKET_DATA))
        self.assertIsNone(self._account_row(result)["latest_nav"]["market_evidence_ref"])
        self.assertEqual(MDC.AVAILABILITY_UNAVAILABLE, result["market"]["availability"])
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        self.assertNotIn(99.0, _numeric_leaves(result),
                         "未来读数不得出现在更早归因的结构里")
        self.assertNotIn(NEXT.isoformat(), serialized)

        control = self._compose()
        events = self._events_by_source(control, ARC.EVIDENCE_SOURCE_MARKET_DATA)
        self.assertEqual(1, len(events))
        self.assertIn(DAY.isoformat(), events[0]["evidence_id"])
        self.assertEqual(
            f"{MDC.ATTRIBUTION_POLICY.name}|{MDS.KIND_FULL_MARKET_SNAPSHOT}|{CODE}",
            events[0]["evidence_id"].split("@")[0],
        )

    # ------------- PNL-24 ~ 25：canonical InformationEvent -------------

    def _expected_events(self, market_snapshot):
        """三条 owner 路径各自重算 canonical event（组合层不得自己解释 kind）。"""
        day = self.last_attribution.asof_day
        expected = []
        reading = self._market_reading(market=market_snapshot)
        if reading.availability == MDC.AVAILABILITY_AVAILABLE and reading.snapshot is not None:
            expected.append(ARC.InformationEvent(
                as_of=day, source=DS.PNL_MARKET_SOURCE,
                evidence_ref=ARC.evidence_ref_from_market_reading(reading), payload={},
            ))
        for projection in self._owner_facts():
            expected.append(ARC.InformationEvent(
                as_of=day, source=DS.PNL_PORTFOLIO_SOURCE,
                evidence_ref=PFA.evidence_ref_from_portfolio_projection(projection), payload={},
            ))
        for projection in self._owner_projections():
            expected.append(ARC.InformationEvent(
                as_of=day, source=DS.PNL_EXECUTION_SOURCE,
                evidence_ref=XEA.evidence_ref_from_execution_projection(projection), payload={},
            ))
        return expected

    def test_PNL_24_information_event_kind_derives_from_evidence_ref(self):
        """PNL-24：``kind`` 由 ``evidence_ref`` 派生，且事件集合与 owner 事实一一对应。"""
        self._happy_fixture()
        result = self._compose()
        expected = self._expected_events(self.last_market_snapshot)
        self.assertEqual(
            sorted(
                (event.evidence_ref.source_type, event.evidence_ref.source_id, event.kind,
                 event.source, event.verification, event.verification_method)
                for event in expected
            ),
            sorted(
                (item["source_type"], item["source_id"], item["kind"], item["source"],
                 item["verification"], item["verification_method"])
                for item in result["canonical_events"]
            ),
        )
        self.assertTrue(result["canonical_events"])

        # "没有任何调用方自述的 kind"：kind 是派生只读属性，不是可传字段。
        fields = {item.name for item in dataclasses.fields(ARC.InformationEvent)}
        self.assertEqual({"as_of", "source", "evidence_ref", "payload"}, fields)
        self.assertNotIn("kind", fields)
        self.assertIsInstance(
            inspect.getattr_static(ARC.InformationEvent, "kind"), property,
        )
        first_ref = ARC.evidence_ref_from_market_reading(self._market_reading())
        with self.assertRaises(TypeError):
            ARC.InformationEvent(
                as_of=DAY.isoformat(), source=DS.PNL_MARKET_SOURCE, evidence_ref=first_ref,
                payload={}, kind=ARC.EVENT_EXECUTION_OBSERVED,
            )

    def test_PNL_25_payload_cannot_override_verification(self):
        """PNL-25：payload 里放 ``verification`` / ``kind`` / ``as_of`` 都改不了派生属性。"""
        market_ref = ARC.evidence_ref_from_market_reading(
            self._market_reading(market=self._snapshot()))
        forged = {
            "kind": ARC.EVENT_EXECUTION_OBSERVED,
            "verification": "unverified",
            "verification_method": MDC.VERIFICATION_METHOD_NONE,
            "as_of": NEXT.isoformat(),
            "evidence_id": "forged",
            "is_verified": False,
        }
        event = ARC.InformationEvent(
            as_of=DAY.isoformat(), source=DS.PNL_MARKET_SOURCE, evidence_ref=market_ref,
            payload=forged,
        )
        self.assertEqual(ARC.EVENT_MARKET_OBSERVED, event.kind)
        self.assertEqual(market_ref.verification, event.verification)
        self.assertEqual(market_ref.verification_method, event.verification_method)
        self.assertEqual(DAY.isoformat(), event.as_of)
        self.assertEqual(market_ref.source_id, event.evidence_id)
        self.assertTrue(event.is_verified)
        # 非空性：payload 原样保留（被覆盖的是"派生属性"，不是 payload 本身）。
        for key, value in forged.items():
            with self.subTest(key=key):
                self.assertIn(key, event.payload)
                self.assertEqual(value, event.payload[key])

        for kind in (PPRM.PORTFOLIO_FACT_CASH, PPRM.PORTFOLIO_FACT_REALIZED_PNL,
                     PPRM.PORTFOLIO_FACT_POSITION_COST_SUMMARY):
            projection = next(
                item for item in self._owner_facts() if item.fact_kind == kind
            )
            ref = PFA.evidence_ref_from_portfolio_projection(projection)
            other = ARC.InformationEvent(
                as_of=DAY.isoformat(), source=DS.PNL_PORTFOLIO_SOURCE, evidence_ref=ref,
                payload=forged,
            )
            with self.subTest(fact=kind):
                self.assertEqual(ARC.EVENT_PORTFOLIO_RESEARCH_OBSERVED, other.kind)
                self.assertEqual(ref.verification, other.verification)
                self.assertIsNone(other.verification_method)
                self.assertEqual(ref.source_id, other.evidence_id)

    # ------------------ PNL-26 ~ 30：架构边界 ------------------

    @staticmethod
    def _adapter_call_sites(symbol):
        """生产模块里对该 owner factory 的调用点（文件 → 外层函数 → 次数）。"""
        sites = {}
        for name in _production_modules():
            with open(os.path.join(BACKEND, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            found = _call_sites(tree, symbol)
            if found:
                sites[name] = found
        return sites

    def test_PNL_26_execution_adapter_production_caller_is_the_pnl_path(self):
        """PNL-26：生产里只有一个 execution adapter 调用点，且它就在 ``_execution_leg``。"""
        probe = ast.parse("def outer():\n    def inner():\n        target()\n    inner()\n")
        self.assertEqual({"inner": 1}, _call_sites(probe, "target"), "扫描器非空性对照")
        sites = self._adapter_call_sites("evidence_ref_from_execution_projection")
        self.assertEqual({"deepseek_research.py": {"_execution_leg": 1}}, sites)
        self.assertEqual(
            {"_execution_leg"},
            {name for name, _count in sites["deepseek_research.py"].items()},
        )

    def test_PNL_27_portfolio_adapter_production_caller_is_the_pnl_path(self):
        """PNL-27：生产里只有一个 portfolio adapter 调用点，且它就在 ``_portfolio_leg``。"""
        probe = ast.parse("def outer():\n    def inner():\n        target()\n    inner()\n")
        self.assertEqual({"inner": 1}, _call_sites(probe, "target"), "扫描器非空性对照")
        sites = self._adapter_call_sites("evidence_ref_from_portfolio_projection")
        self.assertEqual({"deepseek_research.py": {"_portfolio_leg": 1}}, sites)
        self.assertEqual(
            {"_portfolio_leg"},
            {name for name, _count in sites["deepseek_research.py"].items()},
        )

    def test_PNL_28_no_legacy_sql_fallback_exists_in_the_pnl_path(self):
        """PNL-28：canonical 路径既没有 legacy SQL，也没有 ``except`` 兜底分支。"""
        body = _pnl_source()
        for token in FORBIDDEN_PNL_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, body)
        for name in ("paper_nav", "paper_positions", "VERIFIED_PREDICATE"):
            with self.subTest(identifier=name):
                self.assertNotIn(name, _pnl_identifiers())
        # 非空性对照 1：helper 真的看得见标识符 / 关键字 / SQL 字符串形式的越界记号。
        self.assertIn("paper_positions", _source_identifiers(_control_token_probe))
        self.assertIn("except", _executable_source(_except_probe))
        sql_probe = " ".join(_source_strings(_sql_control_probe)).upper()
        for marker in ("SELECT", "FROM"):
            with self.subTest(marker=marker, scope="sql-control"):
                self.assertIn(marker, sql_probe)
        # 组合层的字符串常量里没有任何 SQL 关键字（表名住在字符串里，必须单独查）。
        for text in _pnl_strings():
            with self.subTest(text=text[:40]):
                for marker in SQL_STRING_MARKERS:
                    self.assertNotIn(marker, text.upper())
        # 组合层本身没有任何捕获分支（"读不到就退回 legacy" 不可表达）。
        for func in COMPOSITION_FUNCTIONS:
            with self.subTest(function=func.__name__, scope="no-except"):
                self.assertNotIn("except", _executable_source(func))
        self.assertIn("finally", _executable_source(DS._pnl_evidence))
        # 唯一存在的 except 是 `_matches_target` 的类型守卫，而且它只返回 False。
        handlers = [
            node for node in ast.walk(ast.parse(inspect.getsource(DS._matches_target)))
            if isinstance(node, ast.ExceptHandler)
        ]
        self.assertTrue(handlers, "非空性：这里确实有一个 except，否则下面的检查是空的")
        for handler in handlers:
            with self.subTest(handler=ast.unparse(handler)):
                self.assertEqual(1, len(handler.body))
                self.assertIsInstance(handler.body[0], ast.Return)
                self.assertIs(False, handler.body[0].value.value)
        # 非空性对照 2：组合真的跑通了（不是"没执行所以没读表"）。
        self._happy_fixture()
        result = self._compose()
        self.assertTrue(result["accounts"])
        self.assertTrue(result["canonical_events"])

    def test_PNL_29_conflicting_same_identity_facts_still_fail_closed(self):
        """PNL-29：同一 identity 下内容不同的两条市场事实必须冲突 fail closed。"""
        first = ARC.evidence_ref_from_market_reading(
            self._market_reading(market=self._snapshot(price=10.5)))
        changed = ARC.evidence_ref_from_market_reading(
            self._market_reading(market=self._snapshot(price=11.9)))
        same = ARC.evidence_ref_from_market_reading(
            self._market_reading(market=self._snapshot(price=10.5)))

        self.assertEqual(MDC.ATTRIBUTION_POLICY.name, first.detail["policy"])
        self.assertEqual(first.identity(), changed.identity())
        self.assertEqual(first.identity(), same.identity())
        self.assertNotEqual(
            first.detail["content_fingerprint"], changed.detail["content_fingerprint"],
        )
        self.assertEqual(
            first.detail["content_fingerprint"], same.detail["content_fingerprint"],
        )

        def evidence(ref):
            return ARC.HypothesisEvidence(ref, ARC.RELATION_CONTEXT)

        for ordered in ((first, changed), (changed, first)):
            with self.subTest(order=[item.source_id for item in ordered]):
                with self.assertRaises(ARC.EvidenceConflict):
                    ARC._normalise_evidence(tuple(evidence(item) for item in ordered))
        # 非空性对照：内容一致时只是去重，不会误报冲突。
        normalised = ARC._normalise_evidence((evidence(first), evidence(same)))
        self.assertEqual(1, len(normalised))
        self.assertEqual(first.identity(), normalised[0].ref.identity())

    def test_PNL_30_presentation_metadata_is_not_typed_evidence(self):
        """PNL-30：展示权威标签只出现在账户行上，绝不进入 typed evidence。"""
        self._happy_fixture()
        result = self._compose()
        self.assertEqual("presentation_only_not_evidence", DS.PRESENTATION_AUTHORITY)
        self.assertEqual("research_composition_only_not_an_owner", result["authority"])

        for row in result["accounts"]:
            with self.subTest(account=row["account_id"]):
                self.assertIs(False, row["is_authoritative"])
                self.assertEqual(DS.PRESENTATION_AUTHORITY, row["presentation_authority"])
                self.assertEqual(set(PPRM.PORTFOLIO_FACT_KINDS),
                                 set(row["owner_fact_provenance"]))
                for kind, item in row["owner_fact_provenance"].items():
                    with self.subTest(fact=kind):
                        self.assertEqual("portfolio_owner_typed_fact", item["authority"])
                        self.assertIs(True, item["is_verified"])

        events = json.dumps(result["canonical_events"], ensure_ascii=False, default=str)
        self.assertNotIn(DS.PRESENTATION_AUTHORITY, events)
        self.assertNotIn("presentation_authority", events)
        self.assertNotIn("is_authoritative", events)
        for event in result["canonical_events"]:
            with self.subTest(source_id=event["source_id"]):
                for key in ("authority", "presentation_authority", "is_authoritative"):
                    self.assertNotIn(key, event["payload_fields"])
        # 非空性：这个标签确实进入了账户行（否则"不在事件里"是空的）。
        whole = json.dumps(result, ensure_ascii=False, default=str)
        self.assertIn(DS.PRESENTATION_AUTHORITY, whole)
        self.assertIn("is_authoritative", whole)

    # ------------------ PNL-31：历史行情缺口 ------------------

    def test_PNL_31_historical_market_gap_is_fail_closed(self):
        """PNL-31：证明不了前一交易日的行情时，日频归因整体 fail closed。"""
        self._buy_with_lot()
        gap = self._request(
            asof_day=NEXT, market_now=dt.datetime(2026, 9, 21, 16, 0, tzinfo=TZ),
        )
        result = self._compose(attribution=gap)
        self.assertEqual(NEXT.isoformat(), result["asof"])
        self.assertEqual("explicit_attribution_request", result["asof_source"])
        row = self._account_row(result)
        for field in ("prior_nav", "daily_pnl", "daily_return_pct"):
            with self.subTest(field=field):
                self.assertEqual(
                    {"value": None, "availability": "unavailable",
                     "reason": DS.PNL_UNAVAILABLE_HISTORICAL_MARKET},
                    row[field],
                )
                self.assertEqual("historical_market_evidence_unavailable", row[field]["reason"])
        self.assertEqual([], result["filled_trades"], "DAY 的成交不得被搬到 NEXT")

        # 缓存里的行情事实**不**被改写成请求日：证据身份仍是 DAY 的观测时点。
        # 注：R24 允许"更早的观测解释更晚的请求"（MDPIT-04），因此这条 DAY 缓存对
        # NEXT 请求是 stale 而**不是** unavailable；组合层如实沿用原观测时点，绝不
        # 把它改写成请求日 —— 日频归因字段因此仍然整体 fail closed。
        market_events = self._events_by_source(result, ARC.EVIDENCE_SOURCE_MARKET_DATA)
        self.assertEqual(1, len(market_events))
        self.assertIn(f"@{DAY.isoformat()} 15:55:00+08:00", market_events[0]["source_id"])

        # 完全没有市场事实时同样 fail closed。
        blocked = self._compose(market=None, attribution=gap)
        self.assertEqual(MDC.AVAILABILITY_UNAVAILABLE, blocked["market"]["availability"])
        blocked_row = self._account_row(blocked)
        for field in ("prior_nav", "daily_pnl", "daily_return_pct"):
            with self.subTest(field=field, scope="no-market"):
                self.assertEqual(
                    {"value": None, "availability": "unavailable",
                     "reason": DS.PNL_UNAVAILABLE_HISTORICAL_MARKET},
                    blocked_row[field],
                )
        self.assertFalse(self._events_by_source(blocked, ARC.EVIDENCE_SOURCE_MARKET_DATA))
        self.refresh_mock.assert_not_called()

    # ------------------ PNL-32：market 新鲜度 ------------------

    def test_PNL_32_stale_market_slice_publishes_freshness(self):
        """PNL-32：stale-but-available 的行情切片必须把新鲜度一起发布。

        R24 对 ``observed <= requested`` 的读数是 **stale-but-available**，不是
        unavailable。所以只看 ``availability`` 无法区分"当日读数"与"隔夜切片"：
        只发布 availability 会让一份过期行情在消费侧与当日行情无从分辨。组合层因此必须
        把 freshness / status / age_seconds / observed_at 一起发布，事件 payload 也要带上。
        """
        self._happy_fixture()
        # 阳性对照：当日读数确实是 fresh（否则下面的"stale"断言可能只是恒真）。
        fresh = self._compose(market=self._snapshot())["market"]
        self.assertEqual(MDC.FRESHNESS_FRESH, fresh["freshness"])

        stale_at = f"{DAY_TEXT} 09:00:00+08:00"
        result = self._compose(market=self._snapshot(as_of=DAY_TEXT, observed_at=stale_at))
        market = result["market"]
        self.assertEqual(MDC.AVAILABILITY_AVAILABLE, market["availability"])
        self.assertNotEqual(
            MDC.FRESHNESS_FRESH, market["freshness"],
            "隔夜切片不得被发布成 fresh",
        )
        self.assertIsNotNone(market["age_seconds"])
        self.assertGreater(market["age_seconds"], 3600.0)
        self.assertEqual(stale_at, market["observed_at"])

        row = self._account_row(result)
        self.assertEqual(market["freshness"], row["latest_nav"]["market_freshness"])
        self.assertEqual(market["status"], row["latest_nav"]["market_status"])
        self.assertEqual(market["age_seconds"], row["latest_nav"]["market_age_seconds"])
        self.assertIsNotNone(row["latest_nav"]["market_age_seconds"])

        events = self._events_by_source(result, ARC.EVIDENCE_SOURCE_MARKET_DATA)
        self.assertEqual(1, len(events))
        for key in ("freshness", "status", "observed_at", "verification_method"):
            self.assertIn(key, events[0]["payload_fields"])

    # ------------- PNL-33：归因目标的唯一判据是 cycle 绑定 -------------

    def test_PNL_33_attribution_targets_use_cycle_binding_not_account_status(self):
        """PNL-33：``attribution_targets`` 的判据是 **cycle 绑定**，不是账户生命周期状态。

        归因回答的是"哪个 account 属于哪个 cycle，因而其历史事实应被归因"，**不是**
        "这个 account 当前是否允许产生新交易"。按本仓库自己的 owner contract
        （``paper_cycle_ownership``：economic ownership = enabled_strategies ∩
        ``paper_accounts.cycle_id == 目标周期``，lifecycle pause 不改变该集合，
        cycle ownership ≠ execution eligibility），一个已绑定 cycle 但当前 paused 的账户，
        其 fees / realized_pnl / 持仓成本仍是**可证明事实**；在这里按 status 过滤会让它
        既不进入汇总、也不发布任何 unavailable 标记 —— 那是静默丢事实。

        本测试用**真实生产 schema**（``PT.init_db()`` 的 ``paper_accounts`` / ``paper_cycles``），
        不改任何 production enum：非 running 状态取仓库自己的 ``'paused'``。
        """
        a, b, c = self._three_real_accounts()
        cycle = self.cycle
        # 先把所有绑定清空，使目标集合只由本测试构造（避免依赖 seed 的偶然绑定）。
        self.conn.execute("UPDATE paper_accounts SET cycle_id=NULL")
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=?, status='running' WHERE id=?", (cycle, a))
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=?, status='paused' WHERE id=?", (cycle, b))
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=NULL, status='running' WHERE id=?", (c,))
        self.conn.commit()

        # ── 前置事实（非空性）：B 确实"非 running **且**已绑定目标 cycle"。
        # 没有这一步，下面的断言可能只是碰巧成立（例如 B 恰好是 running，或 B 没绑定）。
        b_row = self.conn.execute(
            "SELECT status, cycle_id FROM paper_accounts WHERE id=?", (b,)).fetchone()
        self.assertNotEqual("running", b_row["status"], "B 必须是非 running 状态")
        self.assertEqual(cycle, int(b_row["cycle_id"]), "B 必须绑定目标 cycle")
        c_row = self.conn.execute(
            "SELECT cycle_id FROM paper_accounts WHERE id=?", (c,)).fetchone()
        self.assertIsNone(c_row["cycle_id"], "C 必须没有 cycle 绑定")

        # ── 主证据（行为断言）：绑定即入选，与 status 无关；无绑定即落选。
        targets = dict(PPOS.attribution_targets(self.conn))
        self.assertEqual({a: cycle, b: cycle}, targets)

        # ── 非空性：证明本测试真的能区分"旧的借用执行资格"实现。
        # 在**同一份真实数据**上跑一遍旧判据（``cycle_id IS NOT NULL AND status='running'``），
        # 它必须会把 B 丢掉；否则上面"B 仍在"这条断言就没有判别力（碰巧成立）。
        legacy = {
            str(row["id"]): int(row["cycle_id"])
            for row in self.conn.execute(
                "SELECT id, cycle_id FROM paper_accounts"
                " WHERE cycle_id IS NOT NULL AND status='running' ORDER BY id"
            ).fetchall()
        }
        self.assertIn(a, legacy)
        self.assertNotIn(
            b, legacy,
            "旧判据（叠加 status='running'）会丢掉 paused 的绑定账户 —— 本测试正因此有判别力",
        )

        # ── 核心证明 1：改变 status **不**改变 cycle-bound 归属。
        self.conn.execute("UPDATE paper_accounts SET status='running' WHERE id=?", (b,))
        self.conn.commit()
        self.assertEqual(
            {a: cycle, b: cycle}, dict(PPOS.attribution_targets(self.conn)),
            "把绑定账户从 paused 改成 running 不得改变归因目标集合",
        )
        self.conn.execute("UPDATE paper_accounts SET status='paused' WHERE id=?", (a,))
        self.conn.commit()
        self.assertEqual(
            {a: cycle, b: cycle}, dict(PPOS.attribution_targets(self.conn)),
            "把另一个绑定账户改成 paused 也不得改变归因目标集合",
        )

        # ── 核心证明 2：cycle_id 非 NULL → NULL 必须让 target 消失。
        self.conn.execute("UPDATE paper_accounts SET cycle_id=NULL WHERE id=?", (a,))
        self.conn.commit()
        after = dict(PPOS.attribution_targets(self.conn))
        self.assertNotIn(a, after, "失去 cycle 绑定的账户必须从归因目标消失")
        self.assertIn(b, after, "另一个绑定账户不受影响")

        # ── 边界补充（行为证据是主证据，这里只补充实现边界）：可执行 SQL 里不得出现
        # account status 谓词，也不得回落"当前 cycle"（那会引入 current-cycle fallback）。
        sql = _attribution_targets_sql()
        self.assertIn("paper_accounts", sql, "非空性：确实取到了真实 SQL 字面量")
        self.assertNotIn("status", sql.lower(), "归因目标不得依赖账户生命周期状态")
        self.assertNotIn("paper_cycles", sql.lower(), "归因目标不得回落当前 cycle")

    # ------------- PNL-34 ~ 36：编排边界必须显式声明 PIT context -------------

    def test_PNL_34_post_close_context_requires_an_explicit_instant(self):
        """PNL-34：编排边界必须给出显式观测 instant；签发口不得自己读墙钟。

        ``_post_close_attribution_request`` 是 production 里唯一的 attribution context
        签发口。它一旦允许无参调用，业务日就会重新落到 ``datetime.now().date()`` 上 ——
        那正是本轮要关掉的 PIT 缺口。所以无参调用必须直接失败，而不是"帮调用方取现在"。
        """
        with self.assertRaises(TypeError):
            AE._post_close_attribution_request()
        # 显式给了非法值同样失败（绝不回落墙钟）。
        for bad in ("2026-09-20", dt.date(2026, 9, 20), 1.0, None):
            with self.subTest(now=repr(bad)):
                with self.assertRaises(ValueError):
                    AE._post_close_attribution_request(now=bad)
        # naive datetime 也不行：freshness 与交易日历都要求确定时区。
        with self.assertRaises(ValueError):
            AE._post_close_attribution_request(now=dt.datetime(2026, 9, 20, 16, 0))

        # 非空性：合法显式 instant 必须真的签发出 context，且业务日是一个**已完成交易日**。
        trading_day = self._a_trading_day_on_or_before(DAY)
        instant = self._instant(trading_day)
        request = AE._post_close_attribution_request(now=instant)
        self.assertIs(type(request), DS.AttributionRequest)
        self.assertEqual(trading_day.isoformat(), request.asof_day)
        self.assertTrue(U.is_trade_day(dt.date.fromisoformat(request.asof_day)))
        self.assertEqual(instant, request.market_now)

    def test_PNL_35_only_a_completed_same_day_can_be_declared(self):
        """PNL-35：只有"本日历日自身已是完成交易日"才签发；盘中与周末/节假日 fail closed。

        交易日历在盘中（未过 15:05）与周末/法定节假日会返回**上一个已完成交易日**。若照此
        签发，就会得到"历史业务日 + 当前 ``paper_accounts.cycle_id`` 绑定"这一组合 ——
        账户后来解绑或换周期后，那就是用当前归属解释历史日（current-state leak）。所以边界
        必须**拒绝回退**：``asof_day`` 恒等于调用方声明的那个本日历日，且该日本身必须是完成
        交易日；否则 fail closed。
        """
        t = self._a_trading_day_on_or_before(DAY)
        n = t
        for _ in range(7):
            n = n + dt.timedelta(days=1)
            if not U.is_trade_day(n):
                break
        self.assertFalse(U.is_trade_day(n), "需要找到一个非交易日（周末必然满足）")

        close = self._instant(t)              # 交易日收盘后（> 15:05）
        intraday = self._instant(t, hour=11)  # 交易日盘中（< 15:05）
        weekend = self._instant(n)            # 非交易日 16:00

        seen = []
        real = U.latest_complete_trade_date

        def spy(*, asof_day=None, now=None):
            seen.append((asof_day, now))
            return real(asof_day=asof_day, now=now)

        with mock.patch.object(U, "latest_complete_trade_date", side_effect=spy):
            # 盘中 / 非交易日：必须 fail closed，而且**不得**去发现 targets ——
            # 否则就真的构造过"历史业务日 + 当前绑定"这一对了。
            with mock.patch.object(
                PPOS, "attribution_targets",
                side_effect=AssertionError("fail-closed 分支不得发现 targets"),
            ):
                self.assertIsNone(
                    AE._post_close_attribution_request(now=intraday),
                    "交易日盘中时归因日会是上一交易日 —— 必须 fail closed",
                )
                self.assertIsNone(
                    AE._post_close_attribution_request(now=weekend),
                    "非交易日同样不得签发上一个交易日的归因",
                )
            # 交易日收盘后：正常签发。
            signed = AE._post_close_attribution_request(now=close)

        self.assertIs(type(signed), DS.AttributionRequest)
        self.assertEqual(t.isoformat(), signed.asof_day)
        # 本轮的核心不变量：成功签发的业务日**永远**是调用方声明的那个本日历日。
        self.assertEqual(close.date().isoformat(), signed.asof_day)
        # instant 被原样传下去：边界只声明一次时刻，签发口不读第二次钟。
        self.assertEqual([(None, intraday), (None, weekend), (None, close)], seen)

        # 边界补充（行为证据是主证据）：签发口不得出现墙钟读数，且必须真的调用交易日历。
        called = set()
        for node in ast.walk(ast.parse(_executable_source(AE._post_close_attribution_request))):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        self.assertIn("latest_complete_trade_date", called, "非空性：确实调用了交易日历")
        self.assertNotIn("now", called)
        self.assertNotIn("today", called)

    def test_PNL_36_historical_asof_cannot_reuse_current_binding(self):
        """PNL-36：历史业务日不得借用**当前** ``paper_accounts.cycle_id`` 归属。

        生产里唯一的签发口只回答"当日 post-close"：签名里没有业务日 / targets 参数，而且
        运行期会把"本日历日不是完成交易日"直接判为 fail closed（见 PNL-35），因此
        "历史 asof + 当前绑定自动发现 targets"不可达。这里额外锁两件事：只有编排边界能构造
        ``AttributionRequest``（任何模块都不得自述一个历史请求），以及 targets 是签发时刻的
        **冻结快照** —— 之后账户解绑或换周期，不得把已签发的 context 静默重绑定。
        """
        # 结构性拒绝：只接受一个显式 instant。
        self.assertEqual(
            {"now"}, set(inspect.signature(AE._post_close_attribution_request).parameters),
        )
        # 生产里构造 AttributionRequest 的模块只能是编排边界。
        self.assertEqual(
            {"adaptive_engine.py"}, _attribution_request_constructor_modules(),
            "只有编排边界的签发口可以构造 AttributionRequest",
        )
        # 非空性：扫描器真的看得见构造点。
        self.assertEqual(
            {"probe.py"},
            _attribution_request_constructor_modules(
                {"probe.py": "from deepseek_research import AttributionRequest\n"
                             "X = AttributionRequest(asof_day='2020-01-01', market_now=None, targets=())\n"}
            ),
        )

        instant = self._instant(self._a_trading_day_on_or_before(DAY))
        # 目标集合只由本用例构造：seed 库里账户本来就带 cycle 绑定，先清空。
        self.conn.execute("UPDATE paper_accounts SET cycle_id=NULL")
        self.conn.execute(
            "UPDATE paper_accounts SET cycle_id=? WHERE id=?", (self.cycle, ACCOUNT))
        self.conn.commit()

        request = AE._post_close_attribution_request(now=instant)
        before = request.targets
        self.assertEqual(((ACCOUNT, self.cycle),), before)
        self.assertEqual(instant.date().isoformat(), request.asof_day)

        # 冻结快照：账户解绑后，已签发 context 的 targets 必须一字不变。
        self.conn.execute("UPDATE paper_accounts SET cycle_id=NULL WHERE id=?", (ACCOUNT,))
        self.conn.commit()
        self.assertEqual(before, request.targets, "已签发的 context 不得被当前绑定改写")

        # 非空性：此刻**重新**签发才会反映当前状态（否则上面那条可能是发现逻辑空转）。
        self.assertIsNone(
            AE._post_close_attribution_request(now=instant),
            "没有可证明绑定的账户时必须 fail closed（返回 None）",
        )

    @staticmethod
    def _instant(day, *, hour=16):
        """一个**显式**的 tz-aware 观测 instant（固定给定，与墙钟无关）。"""
        return dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)

    @staticmethod
    def _a_trading_day_on_or_before(day):
        """从 ``day`` 往前找最近的一个交易日（交易日历离线可用；找不到即夹具问题）。"""
        cursor = day
        for _ in range(14):
            if U.is_trade_day(cursor):
                return cursor
            cursor = cursor - dt.timedelta(days=1)
        raise AssertionError("找不到交易日 —— 交易日历不可用，无法构造确定性夹具")

    def _three_real_accounts(self):
        """三个**真实**的 seed 账户 id（不新建账户、不改 production 配置）。"""
        ids = [str(key) for key in PT.ACCOUNT_SPECS]
        self.assertGreaterEqual(len(ids), 3, "fixture 需要至少 3 个真实账户")
        return ids[0], ids[1], ids[2]


def _attribution_targets_sql() -> str:
    """``attribution_targets`` 源码里的字符串字面量拼接（docstring 已剥掉）。

    刻意**不**复用模块级的 ``_executable_source``：那个助手为"标识符视图"服务，会把所有
    字符串字面量替换成 ``…``，因此拿不到 SQL 原文。这里要的恰恰是 SQL 文本本身。
    """
    tree = ast.parse(inspect.getsource(PPOS.attribution_targets))
    body = list(tree.body[0].body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    node = ast.Module(body=body, type_ignores=[])
    return " ".join(
        item.value for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    )


def _except_probe(value=None) -> object:
    """非空性对照：可执行源码里的 ``except`` 必须能被看见。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    unittest.main()
