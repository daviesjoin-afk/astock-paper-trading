# -*- coding: utf-8 -*-
"""PR-35 golden replay v2：用户自建策略的生产路径端到端回放。

与 ``demo_seed`` 的手写账本不同，本测试**不 INSERT 任何 paper_orders /
paper_fills**——它只调用与生产完全相同的 Service 链：

  create → save DSL → validate → activate → enable setting → create cycle
  → runtime compile → factor evaluation → signal → OrderIntent → allocation
  → planner → revalidate → commit → T+1 → risk exit → evolution shadow
  → counterfactual → champion promotion

所有行情通过依赖注入固定：universe / 日线 / 全市场快照 / 实时报价全部
落到临时目录与补丁函数，断网执行。选股因子由生产的
``_rebuild_selection_factor_cache`` 从注入的日线构建，DSL 候选由生产的
``_candidate_rows`` → ``user_strategy_participation.dsl_strategy_raw``
产出，执行走 OrderIntent → ExecutionPlanner → reserve → revalidate →
commit 的强制契约（origin=user）。

回归锚点（PR-21~34 之前的老 master）：
- 用户策略未激活时不能进入新周期（enabled_strategies 校验拒绝）；
- 激活后若没有参与接线（注册表参与资格 / 账户开户），生产收盘扫描
  永远不会为该策略创建信号。本测试断言接线后的完整闭环成立。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import data_fetcher as dfc  # noqa: E402
import factors as FACTORS  # noqa: E402
import news_learning as NL  # noqa: E402
import paper_research as PR  # noqa: E402
import paper_trading as PT  # noqa: E402
import runtime_settings as RSET  # noqa: E402
import self_evolution as SE  # noqa: E402
import strategy_champion as SCM  # noqa: E402
import strategy_registry as SR  # noqa: E402
import strategy_runtime as SRT  # noqa: E402
import universe as U  # noqa: E402

STRATEGY_ID = "golden_replay_alpha"
D0 = dt.date(2026, 9, 8)   # 周二：因子截止日（最近完整收盘日）
D1 = dt.date(2026, 9, 9)   # 周三：信号意图交易日 / 开仓执行日
D2 = dt.date(2026, 9, 10)  # 周四：T+1 可卖 / 风控退出触发日
D3 = dt.date(2026, 9, 11)  # 周五：硬止损确认日（如需第二窗口）
D4 = dt.date(2026, 9, 14)  # 周一：连续深跌（分批退出续）
D5 = dt.date(2026, 9, 15)  # 周二：连续深跌（分批退出续）
D6 = dt.date(2026, 9, 16)  # 周三：分批退出兜底窗口
CAPITAL = 1_000_000.0

PASS_CODES = ("600901", "600902")
FAIL_CODES = ("600903", "600904", "600905", "600906")
ALL_CODES = PASS_CODES + FAIL_CODES
NAMES = {
    "600901": "回放甲", "600902": "回放乙", "600903": "回放丙",
    "600904": "回放丁", "600905": "回放戊", "600906": "回放己",
}
INDUSTRY = "回放行业"

# DSL：close > ma20 且 volume > volume_mean5 × 1.2（声明式、纯离线可判）。
RULE = {
    "op": "and",
    "args": [
        {"op": "gt", "left": {"op": "field", "name": "close"},
         "right": {"op": "indicator", "name": "ma", "window": 20}},
        {"op": "gt", "left": {"op": "field", "name": "volume"},
         "right": {"op": "mul", "left": {"op": "indicator", "name": "volume_mean", "window": 5},
                   "right": {"op": "const", "value": 1.2}}},
    ],
}

# 每只代码的"D 日 → 当日价格"。默认价为该代码最后一根收盘价 × 1.005。
QUOTE_PRICES: dict[tuple[str, str], float] = {}
# 按日注入的行情场景（风控日的崩盘形态：深跌 + 主力大幅流出 + 收在低位）。
QUOTE_SCENARIOS: dict[str, dict] = {}
LAST_CLOSE: dict[str, float] = {}


def _sessions(end: dt.date, count: int) -> list[dt.date]:
    days: list[dt.date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= dt.timedelta(days=1)
    return list(reversed(days))


def _synthetic_kline(code: str):
    import pandas as pd

    days = _sessions(D0, 170)
    passing = code in PASS_CODES
    closes: list[float] = []
    for index in range(len(days)):
        if passing:
            # 前 130 日缓跌，最后 40 日稳定上行 → 最后一根 close 明显在 ma20 上方。
            if index < 130:
                closes.append(20.0 - index * 0.02)
            else:
                closes.append(19.4 + (index - 129) * 0.05)
        else:
            # 持续阴跌：close 始终在 ma20 下方。
            closes.append(20.0 - index * 0.05)
    volumes = [1_000_000.0] * len(days)
    if passing:
        # 末日放量 2.5 倍：volume > volume_mean5 × 1.2 成立。
        volumes[-1] = 2_500_000.0
    frame = pd.DataFrame(
        {
            "open": [round(value * 0.995, 2) for value in closes],
            "high": [round(value * 1.02, 2) for value in closes],
            "low": [round(value * 0.98, 2) for value in closes],
            "close": [round(value, 2) for value in closes],
            "volume": volumes,
            "amount": [round(value * close, 2) for value, close in zip(volumes, closes, strict=True)],
        },
        index=pd.DatetimeIndex([dt.datetime.combine(day, dt.time(15, 0)) for day in days]),
    )
    frame.index.name = "date"
    frame.attrs["source"] = "unit_test_injection"
    frame.attrs["adjustment"] = "qfq"
    LAST_CLOSE[code] = round(closes[-1], 2)
    return frame


def _seed_market(tmp: str) -> None:
    os.makedirs(dfc.KLINE_DIR, exist_ok=True)
    stocks = []
    for code in ALL_CODES:
        frame = _synthetic_kline(code)
        dfc.save_kline(code, frame)
        stocks.append({
            "code": code, "name": NAMES[code], "board": "主板",
            "risk_flag": 0, "snapshot_tradable": True, "listing_status": "listed",
            "price": LAST_CLOSE[code], "pct": 0.6, "industry": INDUSTRY,
        })
    # 基准指数：缓步上行，保证 _market_state 具备 21 根完整日线。
    bench_days = _sessions(D0, 40)
    bench_closes = [3800.0 + index * 2.0 for index in range(40)]
    import pandas as pd

    bench = pd.DataFrame(
        {
            "open": bench_closes, "high": bench_closes, "low": bench_closes,
            "close": bench_closes, "volume": [1.0] * 40, "amount": [1.0] * 40,
        },
        index=pd.DatetimeIndex([dt.datetime.combine(day, dt.time(15, 0)) for day in bench_days]),
    )
    bench.index.name = "date"
    bench.attrs["source"] = "unit_test_injection"
    bench.attrs["adjustment"] = "qfq"
    dfc.save_kline("BENCH_000300", bench)
    # save_kline 的 manifest 攒批 50 次才落盘；测试规模小，显式刷盘。
    dfc._flush_kline_manifest_unlocked()
    universe = {
        "built_at": f"{D0.isoformat()} 08:00:00",
        "scope": "all_a_shares", "requested_limit": None,
        "stocks": stocks,
    }
    with open(U.UNIVERSE_PATH, "w", encoding="utf-8") as handle:
        json.dump(universe, handle, ensure_ascii=False)


def _default_price(code: str, day: dt.date) -> float:
    fixed = QUOTE_PRICES.get((code, day.isoformat()))
    if fixed is not None:
        return fixed
    return round(LAST_CLOSE[code] * 1.005, 2)


def _fake_validated_live_universe(rows, day, max_quote_age_minutes=None):
    """开盘执行通道注入：全部合成代码都带当日源时间戳的实时快照。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    return [
        {
            "code": code, "name": NAMES.get(code, code),
            "price": _default_price(code, day), "pct": 0.6,
            "quote_at": now, "source": "unit_test_injection", "risk_flag": 0,
            "amount": 50_000_000.0, "main_pct": 1.5,
        }
        for code in ALL_CODES
    ]


def _fake_quotes(codes, asof_date=None):
    if isinstance(asof_date, str):
        day = dt.date.fromisoformat(asof_date[:10])
    else:
        day = asof_date or D1
    result = {}
    for code in codes:
        code = str(code)
        price = _default_price(code, day)
        scenario = dict(QUOTE_SCENARIOS.get(day.isoformat()) or {})
        quote_time = scenario.pop("quote_time", "09:40")
        open_above = scenario.pop("open_above", False)
        # 当日实时报价必须带"刚刚"的源时间戳；历史回放日只要求日期一致。
        if day == dt.date.today():
            stamp = dt.datetime.now().isoformat(timespec="seconds")
        else:
            stamp = f"{day.isoformat()} {quote_time}:00"
        result[code] = {
            "code": code, "name": NAMES.get(code, code), "price": price,
            "pct": scenario.get("pct", 0.6),
            "open_price": round(price * (1.02 if open_above else 0.998), 2),
            "high": round(price * (1.021 if open_above else 1.01), 2),
            "low": round(price * (0.999 if open_above else 0.99), 2),
            "vol_ratio": scenario.get("vol_ratio", 1.3),
            "main_pct": scenario.get("main_pct", 1.5),
            "main_net": scenario.get("main_net", 3_000_000.0),
            "super_net": scenario.get("super_net", 2_000_000.0),
            "amount": 50_000_000.0,
            "quote_at": stamp,
            "quote_source": "live", "source": "unit_test_injection",
            "quote_validation": "cross_source_checked", "risk_flag": 0,
        }
    return result


class OfflinePaperEnv:
    """共享离线回放环境：临时目录 + 依赖注入补丁（黄金回放/归档回放共用）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="astock-golden-v2-")
        cls._patches = []
        for target, attr in (
            (dfc, "CACHE_DIR"), (dfc, "KLINE_DIR"), (dfc, "KLINE_MANIFEST_PATH"),
            (dfc, "MARKET_SNAPSHOT_FULL_CACHE_PATH"),
            (U, "UNIVERSE_PATH"),
            (PT, "DB_PATH"), (PT, "SELECTION_FACTORS_PATH"), (PT, "SELECTION_META_PATH"),
            (NL, "DB_PATH"), (NL, "PAPER_DB_PATH"), (PR, "DB_PATH"),
        ):
            old = getattr(target, attr)
            cls._patches.append((target, attr, old))
            if attr in {"CACHE_DIR", "KLINE_DIR"}:
                setattr(target, attr, os.path.join(cls._tmp, attr.lower()))
            elif attr == "DB_PATH":
                setattr(target, attr, os.path.join(cls._tmp, "paper_trading.sqlite3"))
            elif attr == "UNIVERSE_PATH":
                setattr(target, attr, os.path.join(cls._tmp, "universe.json"))
            else:
                setattr(target, attr, os.path.join(cls._tmp, f"{attr.lower()}.json"))
        # 部署规模阈值是策略常量而非逻辑：小样本注入下放宽，保持全链路
        # 代码路径与生产一致（demo 链路同样用 ASTOCK_FULL_MARKET_MIN_ROWS=1）。
        cls._patches.append((PT, "CANDIDATE_FACTOR_MIN_ROWS", PT.CANDIDATE_FACTOR_MIN_ROWS))
        PT.CANDIDATE_FACTOR_MIN_ROWS = 1
        cls._patches.append((PT, "LIVE_SCAN_GATE_MIN_ROWS", PT.LIVE_SCAN_GATE_MIN_ROWS))
        PT.LIVE_SCAN_GATE_MIN_ROWS = 1
        cls._patches.append((U, "FORMAL_UNIVERSE_MIN_ROWS", U.FORMAL_UNIVERSE_MIN_ROWS))
        U.FORMAL_UNIVERSE_MIN_ROWS = 1
        # 交易日历属于外部数据：离线环境按工作日近似（不影响被测逻辑）。
        cls._patches.append((U, "is_trade_day", U.is_trade_day))
        U.is_trade_day = staticmethod(lambda value=None: (_as_date(value) or dt.date.today()).weekday() < 5)
        cls._patches.append((PT, "schedule_status", PT.schedule_status))
        PT.schedule_status = staticmethod(lambda: {"scheduler": "unit-test", "enabled": False})
        cls._patches.append((PT, "AD", PT.AD))
        PT.AD = None
        cls._patches.append((PT, "_quotes", PT._quotes))
        PT._quotes = _fake_quotes
        cls._patches.append((PT, "_news_for", PT._news_for))
        PT._news_for = lambda names: []
        cls._patches.append((U, "refresh_history", U.refresh_history))
        U.refresh_history = lambda **kwargs: {"status": "up_to_date"}
        cls._patches.append((dfc, "fetch_market_snapshot_full", dfc.fetch_market_snapshot_full))
        dfc.fetch_market_snapshot_full = lambda *a, **k: []
        # 开盘执行通道的全市场探活/快照验证：注入合成实时宇宙（含当日时间戳）。
        cls._patches.append((dfc, "check_data_source_health", dfc.check_data_source_health))
        dfc.check_data_source_health = lambda *a, **k: {
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "healthy": True,
            "reconnected": False, "attempts": 1, "action": "unit-test",
        }
        cls._patches.append((PT, "_validated_live_universe", PT._validated_live_universe))
        PT._validated_live_universe = _fake_validated_live_universe
        cls._patches.append((dfc, "fetch_indices", dfc.fetch_indices))
        dfc.fetch_indices = lambda *a, **k: [
            {
                "code": "sh000300", "price": 3878.0, "pct": 0.5,
                "time": f"{day.strftime('%Y%m%d')}150000",
            }
            for day in (D0, D1, D2, D3)
        ]
        cls._patches.append((FACTORS, "overseas_risk_gate", FACTORS.overseas_risk_gate))
        FACTORS.overseas_risk_gate = lambda *a, **k: {"light": "green", "advice": "unit-test"}
        cls._patches.append((dfc, "fetch_finance_latest", dfc.fetch_finance_latest))
        dfc.fetch_finance_latest = lambda *a, **k: {"data": {}}
        cls._patches.append((dfc, "fetch_hot_sector_snapshot", dfc.fetch_hot_sector_snapshot))
        dfc.fetch_hot_sector_snapshot = lambda *a, **k: []
        cls._patches.append((dfc, "fetch_sector_flow", dfc.fetch_sector_flow))
        dfc.fetch_sector_flow = lambda *a, **k: []
        import disclosure_timeline as DT
        cls._patches.append((DT, "CACHE_PATH", DT.CACHE_PATH))
        DT.CACHE_PATH = os.path.join(cls._tmp, "disclosure_timeline.json")
        cls._patches.append((DT, "fetch_disclosure_timeline", DT.fetch_disclosure_timeline))
        DT.fetch_disclosure_timeline = lambda *a, **k: []
        # 入场熔断（_entry_freeze_status）消费的三类持久化工件：源健康、
        # K线覆盖、因子缓存。离线环境注入工件本体，生产闸门逻辑保持原样。
        cls._patches.append((dfc, "load_source_health", dfc.load_source_health))
        dfc.load_source_health = lambda: {
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "healthy": True,
        }
        cls._patches.append((U, "coverage_report", U.coverage_report))
        U.coverage_report = lambda *a, **k: {
            "ready": True, "fresh_coverage_pct": 100.0, "fresh_pct": 100.0,
        }
        cls._patches.append(
            (PT, "_selection_factor_manifest_signature", PT._selection_factor_manifest_signature)
        )
        PT._selection_factor_manifest_signature = lambda: ["unit-test-signature"]
        with open(PT.SELECTION_FACTORS_PATH, "w", encoding="utf-8") as handle:
            handle.write("code\n" + "\n".join(ALL_CODES) + "\n")
        selection_meta = {
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "factor_rows": len(ALL_CODES),
            "eligible_factor_coverage_pct": 100.0,
            "factor_date": U.latest_complete_trade_date().isoformat(),
            "signature": ["unit-test-signature"],
        }
        with open(PT.SELECTION_META_PATH, "w", encoding="utf-8") as handle:
            json.dump(selection_meta, handle)
        SRT.clear_cache()
        _seed_market(cls._tmp)
        PT.init_db()

    @classmethod
    def tearDownClass(cls):
        SRT.clear_cache()
        for target, attr, old in reversed(cls._patches):
            setattr(target, attr, old)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ---------- 断言辅助 ----------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(PT.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _one(self, conn, sql, params=()):
        return conn.execute(sql, params).fetchone()


class ProductionPathGoldenReplayTests(OfflinePaperEnv, unittest.TestCase):

    # ---------- 测试 ----------

    def test_production_path_golden_replay(self):
        with self._conn() as conn:
            SR.ensure_schema(conn)
            # 老回归锚点：draft 策略不允许进入新周期（未激活即无参与资格）。
            with self.assertRaises(ValueError):
                RSET.validate({"enabled_strategies": [STRATEGY_ID]}, conn=conn)

            # 1) create + save DSL（生产 create_user_definition 服务）
            strategy = SR.create_user_definition(
                conn, STRATEGY_ID, "黄金回放声明式策略", dsl_ast=RULE,
                metadata={"candidate_topn": 10, "style": "trend", "hold": 8},
                actor="golden-test",
            )
            self.assertEqual(strategy.origin, "user")
            # 2) validate → activate（生产生命周期服务；激活即 supports_new_cycle）
            SR.transition(conn, STRATEGY_ID, "validated", expected_status="draft",
                          reason="golden validate", actor="golden-test")
            SR.transition(conn, STRATEGY_ID, "active", expected_status="validated",
                          reason="golden activate", actor="golden-test")
            spec = SR.get(STRATEGY_ID, conn=conn)
            self.assertEqual(spec.status, "active")
            self.assertTrue(spec.supports_new_cycle)
            readiness = SR.runtime_readiness(conn, STRATEGY_ID)
            self.assertTrue(all(readiness["checks"].values()), readiness)

        # 3) 账户开户（init_db 幂等钩子）
        PT.init_db()
        with self._conn() as conn:
            account = self._one(conn, "SELECT * FROM paper_accounts WHERE id=?", (STRATEGY_ID,))
            self.assertIsNotNone(account, "用户策略激活后必须自动开户")
            self.assertEqual(account["status"], "paused")  # 等待周期分配资金

        # 4) enable setting + create cycle（生产设置与周期服务）
        with self._conn() as conn:
            RSET.update(conn, {"enabled_strategies": [STRATEGY_ID]}, actor="golden-test")
        summary, cycle = PT.start_new_cycle(capital=CAPITAL, include_dashboard=False)
        self.assertEqual(tuple(cycle["enabled_strategies"]), (STRATEGY_ID,))
        with self._conn() as conn:
            account = self._one(conn, "SELECT * FROM paper_accounts WHERE id=?", (STRATEGY_ID,))
            self.assertEqual(account["status"], "running")
            self.assertGreater(account["cash"], 0)
            # 5) runtime compile（同一编译管线：fingerprint → risk → execution）
            context = SRT.get_context(conn, STRATEGY_ID)
            self.assertIsNotNone(context.compiled_dsl)
            self.assertTrue(context.risk_profile.soft_limits)
            self.assertTrue(context.execution_profile)
            self.assertEqual(context.lifecycle_stage, "pilot")  # 用户策略默认试点

        # 6) 生产收盘扫描：因子重建 → DSL 选股 → 信号审批 → OrderIntent
        result = PT.generate_signals(D0)
        self.assertEqual(result.get("slot"), "close")
        user_rows = [row for row in result["accounts"] if row["id"] == STRATEGY_ID]
        self.assertTrue(user_rows, result["accounts"])
        self.assertFalse(user_rows[0].get("blocked"),
                         f"用户策略收盘扫描被阻断：{user_rows[0]}")
        with self._conn() as conn:
            blocked = conn.execute(
                "SELECT code,status,reason FROM paper_signals WHERE account_id=? AND status!='pending'",
                (STRATEGY_ID,),
            ).fetchall()
        self.assertGreater(
            user_rows[0]["created"], 0,
            f"{user_rows[0]} | rejected: {[dict(r) for r in blocked]}",
        )
        with self._conn() as conn:
            signal = self._one(
                conn,
                "SELECT * FROM paper_signals WHERE account_id=? AND signal_date=? ORDER BY id LIMIT 1",
                (STRATEGY_ID, D0.isoformat()),
            )
            self.assertIsNotNone(signal)
            self.assertEqual(signal["strategy_id"], STRATEGY_ID)
            payload = json.loads(signal["payload"])
            self.assertEqual(payload["pick"]["selection_mode"], "dsl")
            self.assertEqual(payload["pick"]["entry_path"], "dsl_rule")
            self.assertIn(signal["code"], PASS_CODES)

        # 7) T+1 开仓执行（OrderIntent → planner → revalidate → commit）
        opened = PT.run_slot("open", D1, force=True)
        self.assertNotEqual(opened.get("status"), "failed", opened)
        with self._conn() as conn:
            debug_orders = conn.execute(
                "SELECT id,code,side,status,reason,planned_price FROM paper_orders WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchall()
            fills = conn.execute(
                "SELECT * FROM paper_fills WHERE account_id=? AND side='buy'",
                (STRATEGY_ID,),
            ).fetchall()
            if not fills:  # pragma: no cover - 调试辅助
                debug_signals = conn.execute(
                    "SELECT code,status,reason FROM paper_signals WHERE account_id=?",
                    (STRATEGY_ID,),
                ).fetchall()
                raise AssertionError(
                    f"open slot 无成交。orders={[dict(r) for r in debug_orders]} "
                    f"signals={[dict(r) for r in debug_signals]} summary={opened}"
                )
            self.assertTrue(fills, "用户策略必须通过生产执行链产生真实成交")
            buy_fill = fills[0]
            self.assertGreater(buy_fill["qty"], 0)
            order = self._one(conn, "SELECT * FROM paper_orders WHERE id=?", (buy_fill["order_id"],))
            self.assertEqual(order["status"], "filled")
            self.assertEqual(order["strategy_id"], STRATEGY_ID)
            self.assertIsNotNone(order["strategy_version"])
            self.assertIsNotNone(order["strategy_checksum"])
            position = self._one(
                conn,
                "SELECT * FROM paper_positions WHERE account_id=? AND remaining_qty>0",
                (STRATEGY_ID,),
            ) if "remaining_qty" in (self._one(
                conn, "SELECT * FROM paper_positions WHERE account_id=? LIMIT 1",
                (STRATEGY_ID,),
            ) or {}).keys() else self._one(
                conn, "SELECT * FROM paper_positions WHERE account_id=?",
                (STRATEGY_ID,),
            )
            self.assertIsNotNone(position, "成交后必须有持仓")
            cost = float(position["cost"])

        # 8) 风控退出（T+1 后深跌至崩盘形态 → 生产硬止损全清）。
        #    生产语义：非崩盘形态首触先按守卫比例减仓、后续扫描确认后全清；
        #    崩盘形态（当日跌幅≥涨跌停幅度的80%）首扫即全清。注入 pct=-8.5：
        #    越过崩盘线（-8.0）但未封跌停（-9.95 以下不可虚构成交）。
        def _run_risk(day):
            # 风控去重按真实时钟分钟标记；测试在同分钟内连续多轮回放，
            # 清理调度标记（调度状态，非交易账本）后重放。
            with self._conn() as conn:
                conn.execute("DELETE FROM paper_jobs WHERE slot='risk'")
                conn.execute("DELETE FROM paper_audit WHERE event='risk_scan_state'")
            return PT.run_slot("risk", day, force=True)

        risk_days = (D2, D3, D4, D5, D6)
        sell_history = []
        for day in risk_days:
            QUOTE_SCENARIOS[day.isoformat()] = {
                "pct": -8.5, "main_pct": -5.0, "main_net": -5_000_000.0,
                "super_net": -4_000_000.0, "vol_ratio": 1.8, "open_above": True,
            }
            for code in PASS_CODES:
                QUOTE_PRICES[(code, day.isoformat())] = round(cost * 0.83, 2)
            _run_risk(day)
            with self._conn() as conn:
                day_sells = conn.execute(
                    "SELECT code,qty,price,fill_date FROM paper_fills "
                    "WHERE account_id=? AND side='sell' AND fill_date=?",
                    (STRATEGY_ID, day.isoformat()),
                ).fetchall()
                sell_history.extend(dict(r) for r in day_sells)
                open_qty = self._one(
                    conn,
                    "SELECT COALESCE(SUM(qty),0) AS n FROM paper_positions WHERE account_id=?",
                    (STRATEGY_ID,),
                )
            if int(open_qty["n"]) == 0:
                break
        self.assertTrue(sell_history, "深跌必须经生产风控退出链产生卖出成交")
        with self._conn() as conn:
            remaining = self._one(
                conn,
                "SELECT COALESCE(SUM(qty),0) AS n FROM paper_positions WHERE account_id=?",
                (STRATEGY_ID,),
            )
            debug = conn.execute(
                "SELECT decision,created_at,"
                "json_extract(payload,'$.exit_marker') AS marker "
                "FROM paper_risk_decisions WHERE account_id=? AND side='sell' "
                "AND decision LIKE 'downside%' ORDER BY id DESC LIMIT 8",
                (STRATEGY_ID,),
            ).fetchall()
        self.assertEqual(
            int(remaining["n"]), 0,
            f"连续深跌必须经生产风控确认后全清: sells={sell_history} "
            f"decisions={[dict(r) for r in debug]}",
        )

        # 9) evolution shadow：研究台账只收内置五套，用户策略的拒绝必须
        #    可审计（不静默、不崩溃），影子证据由 counterfactual 链路承接。
        with self._conn() as conn:
            audits = conn.execute(
                "SELECT * FROM paper_audit WHERE account_id=? AND event IN "
                "('research_shadow_failed','candidate_snapshot_failed')",
                (STRATEGY_ID,),
            ).fetchall()
        self.assertTrue(
            audits or self._shadow_rejected_by_research_ledger(),
            "用户策略的 shadow 处理必须留下可审计痕迹或被研究台账显式拒绝",
        )

        # 10) evolution shadow → counterfactual → champion promotion（收紧路径）
        paper = sqlite3.connect(PT.DB_PATH, timeout=30)
        paper.row_factory = sqlite3.Row
        evo = sqlite3.connect(":memory:")
        evo.row_factory = sqlite3.Row
        try:
            SCM.ensure_schema(paper)
            SE.ensure_schema(evo)
            SE.init_params(evo)
            before_version = SR.get_version(STRATEGY_ID, conn=paper).version
            before_head = SCM.active_runtime_checksum(evo, STRATEGY_ID)
            now = dt.datetime.now().replace(microsecond=0)
            # 收紧路径：max_weight_delta 是保守默认画像的声明可调键
            # （中性方向，无需观察期）；hard_stop 对用户策略锁死不可调。
            opened = SCM.open_challenger(
                paper, evo, STRATEGY_ID, {"max_weight_delta": 0.032},
                evidence_count=20, now=now - dt.timedelta(days=5),
            )
            self.assertTrue(opened.get("opened"), opened)
            SCM.run_shadow_counterfactual(
                paper, STRATEGY_ID,
                {"asof": D1.isoformat(), "600901": {"close": LAST_CLOSE["600901"]}},
                self._counterfactual_output(pnl=1000.0, nav=CAPITAL + 1000.0),
                self._counterfactual_output(pnl=2000.0, nav=CAPITAL + 2000.0),
                observed_at=now - dt.timedelta(days=1),
            )
            result = SCM.promote_challenger(paper, evo, STRATEGY_ID)
            self.assertTrue(result.get("promoted"), result)
            # 晋升推进的是进化参数头（active runtime checksum/version），
            # 注册库 definition 版本只在保存 DSL/定义时递增。
            after_head = SCM.active_runtime_checksum(evo, STRATEGY_ID)
            self.assertNotEqual(before_head["checksum"], after_head["checksum"])
            self.assertIsNotNone(after_head["version_id"])
            # 注册库 definition 版本只在保存 DSL/定义时递增，晋升不动它。
            self.assertEqual(
                before_version, SR.get_version(STRATEGY_ID, conn=paper).version
            )
        finally:
            paper.close()
            evo.close()

        # 11) 历史可回放性：订单/成交都能解析回原策略版本与 checksum。
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT strategy_id, strategy_version, strategy_checksum "
                "FROM paper_orders WHERE account_id=?",
                (STRATEGY_ID,),
            ).fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["strategy_id"], STRATEGY_ID)
            self.assertIsNotNone(row["strategy_version"])
            self.assertIsNotNone(row["strategy_checksum"])

    @staticmethod
    def _shadow_rejected_by_research_ledger() -> bool:
        try:
            with PR._connect() as conn:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                for table in tables:
                    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                    if "account_id" in columns:
                        row = conn.execute(
                            f"SELECT COUNT(*) AS n FROM {table} WHERE account_id=?",
                            (STRATEGY_ID,),
                        ).fetchone()
                        if row and row["n"]:
                            return True
        except Exception:
            return False
        return False

    @staticmethod
    def _counterfactual_output(*, pnl: float, nav: float, code="600901"):
        return {
            "signals": [{"signal_key": "golden-signal", "code": code, "side": "buy"}],
            "orders": [{"signal_key": "golden-signal", "code": code, "side": "buy", "qty": 100,
                        "planned_price": 10.0, "amount": 1000.0, "status": "filled"}],
            "fills": [
                {"code": code, "side": "buy", "qty": 100, "price": 10.0, "amount": 1000.0},
                {"code": code, "side": "sell", "qty": 100, "price": 10.0, "amount": 1000.0,
                 "realized_pnl": pnl},
            ],
            "nav": {"nav_date": D1.isoformat(), "cash": nav, "market_value": 0, "nav": nav},
        }


def _as_date(value=None):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value:
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


if __name__ == "__main__":
    unittest.main()
