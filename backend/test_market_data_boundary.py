# -*- coding: utf-8 -*-
"""R24 —— Market Data Boundary 的契约、parity 与失败模式回归。

本文件存在的全部理由，是这个不变量：

    **只读业务路径不能为了回答"当前已知事实是什么"，偷偷同步发起 provider 网络刷新。**

同时不允许为了性能破坏既有的 freshness / 多源核验 / fail-closed / PIT-as-of 语义。
因此本文件分五组：

    MD-*   contract：状态维度、policy、纯 classify（零 I/O、零时钟）
    MDR-*  read path：**绝不联网**（provider 全部 monkeypatch 成断言失败）
    MDP-*  parity：refresh / provider 失败 / 多源核验 / 缓存行为与迁移前一致
    MDPIT-* as-of：历史请求绝不被 current snapshot 回填
    MDG-*  architecture guard：依赖方向与重复实现不得回流

时间一律用**显式传入**的 ``now``，绝不 ``time.sleep()`` —— 睡眠制造的
边界测试是 flaky 的，而 freshness 边界恰恰是本轮最容易假绿的地方。
"""
from __future__ import annotations

import datetime as dt
import os
import ast
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_fetcher as dfc
import market_data_contract as MDC
import market_data_service as MDSvc

TZ = dt.timezone(dt.timedelta(hours=8))
#: 固定的"现在"：所有判定都相对它计算，与本机时钟无关。
NOW = dt.datetime(2026, 8, 28, 10, 30, 0, tzinfo=TZ)


def _rows(count=1, *, quote_at=None, start=600000):
    """生成 ``count`` 行行情；默认时间戳由 ``_ago`` 决定，缺省为 ``NOW``。"""
    stamp = quote_at if quote_at is not None else NOW.isoformat()
    return [
        {"code": str(start + index), "quote_at": stamp, "price": 10.0}
        for index in range(count)
    ]


def _full_rows(*, quote_at=None):
    """一份**完整**的全市场快照：行数必须达到既有的 4000 行门槛。"""
    return _rows(dfc.FULL_MARKET_MIN_ROWS, quote_at=quote_at)


def _freeze_now(testcase):
    """把 authority 取的墙钟固定成 ``NOW``，让 freshness 判定可确定复现。

    契约本身零时钟；墙钟由 authority 的 ``now_utc`` 提供，因此测试在那里
    收口，而不是依赖本机时间。
    """
    patcher = mock.patch.object(MDSvc, "now_utc", lambda: NOW)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return NOW


def _snapshot(
    *,
    observed_at=None,
    rows=None,
    complete=True,
    verification=MDC.VERIFICATION_VERIFIED,
    **kwargs,
):
    stamp = NOW.isoformat() if observed_at is None else observed_at
    payload = rows if rows is not None else ({"code": "600000", "quote_at": stamp},)
    return MDC.MarketDataSnapshot(
        kind="full_market_snapshot",
        rows=tuple(payload), complete=complete, verification=verification,
        observed_at=stamp, **kwargs,
    )


def _ago(seconds: float) -> str:
    return (NOW - dt.timedelta(seconds=seconds)).isoformat()


class _NetworkForbidden:
    """把 provider 入口全部换成断言失败 —— 只读路径一旦触网立即炸响。"""

    NAMES = (
        "fetch_market_snapshot_full", "fetch_market_snapshot", "fetch_indices",
        "fetch_realtime_for_codes", "fetch_independent_realtime_for_codes",
        "fetch_tencent_realtime_for_codes", "check_data_source_health",
    )

    def __init__(self, testcase):
        self.testcase = testcase
        self.attempts = []
        self._patches = []

    def __enter__(self):
        def _boom(name):
            def _inner(*_args, **_kwargs):
                self.attempts.append(name)
                raise AssertionError(
                    f"network must not be called from read path: {name}"
                )
            return _inner

        for name in self.NAMES:
            self._patches.append(mock.patch.object(dfc, name, _boom(name)))
        for patch in self._patches:
            patch.start()
        self.testcase.addCleanup(self._stop)
        return self

    def _stop(self):
        for patch in reversed(self._patches):
            patch.stop()

    def __exit__(self, *_exc):
        self._stop()


# ---------------------------------------------------------------------------
# MD：纯契约
# ---------------------------------------------------------------------------


class MarketDataContractTests(unittest.TestCase):
    """MD-01 ~ MD-12：状态语义本身。零 I/O、零时钟、零全局状态。"""

    def test_MD01_missing_snapshot_is_unavailable_not_empty_payload(self):
        """完全没有可信事实 → unavailable；**绝不**构造 {} / [] / 0。"""
        reading = MDC.classify(None, MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.AVAILABILITY_UNAVAILABLE, reading.availability)
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)
        self.assertEqual(MDC.REASON_MISSING, reading.reason)
        self.assertIsNone(reading.snapshot)
        self.assertEqual((), reading.rows())
        self.assertFalse(reading.usable)

    def test_MD02_fresh_verified_snapshot_is_fresh(self):
        reading = MDC.classify(_snapshot(), MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.STATUS_FRESH, reading.status)
        self.assertEqual(MDC.FRESHNESS_FRESH, reading.freshness)
        self.assertIsNone(reading.reason)
        self.assertTrue(reading.usable)

    def test_MD03_freshness_boundary_is_exact(self):
        """age < TTL / == TTL / > TTL 三态由显式时间戳决定。"""
        ttl = MDC.LIVE_MARKET_POLICY.max_age_seconds
        cases = (
            (ttl - 1.0, MDC.FRESHNESS_FRESH, MDC.STATUS_FRESH),
            (ttl, MDC.FRESHNESS_FRESH, MDC.STATUS_FRESH),
            (ttl + 1.0, MDC.FRESHNESS_STALE, MDC.STATUS_STALE),
        )
        for age, expected_freshness, expected_status in cases:
            with self.subTest(age=age):
                reading = MDC.classify(
                    _snapshot(observed_at=_ago(age)),
                    MDC.LIVE_MARKET_POLICY, now=NOW,
                )
                self.assertEqual(expected_freshness, reading.freshness)
                self.assertEqual(expected_status, reading.status)

    def test_MD04_stale_keeps_last_known_trusted_rows(self):
        """STALE ≠ UNAVAILABLE：过期仍必须能拿到最后一份可信事实。"""
        reading = MDC.classify(
            _snapshot(observed_at=_ago(3600)), MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.AVAILABILITY_AVAILABLE, reading.availability)
        self.assertEqual(MDC.REASON_STALE, reading.reason)
        self.assertEqual(1, len(reading.rows()))
        self.assertTrue(reading.usable)

    def test_MD05_unknown_observation_time_is_not_fresh(self):
        """源时间戳不可解析 → 既不能算 fresh，也不该算 unavailable。"""
        reading = MDC.classify(
            _snapshot(observed_at="not-a-timestamp"), MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.FRESHNESS_UNKNOWN, reading.freshness)
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertNotEqual(MDC.STATUS_FRESH, reading.status)

    def test_MD06_disagreement_is_reported_not_resolved(self):
        """多源冲突 → unverified + cross_source_failed；绝不静默挑一个源。"""
        reading = MDC.classify(
            _snapshot(verification=MDC.VERIFICATION_DISAGREEMENT),
            MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.STATUS_UNVERIFIED, reading.status)
        self.assertEqual(MDC.REASON_CROSS_SOURCE_FAILED, reading.reason)
        self.assertFalse(reading.usable)

    def test_MD07_cross_source_unavailable_differs_from_disagreement(self):
        """核验源不可用（degraded）与两源冲突（unverified）是两个结论。"""
        unavailable = MDC.classify(
            _snapshot(verification=MDC.VERIFICATION_UNAVAILABLE),
            MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        disagreement = MDC.classify(
            _snapshot(verification=MDC.VERIFICATION_DISAGREEMENT),
            MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.STATUS_DEGRADED, unavailable.status)
        self.assertNotEqual(unavailable.status, disagreement.status)

    def test_MD08_single_source_is_usable_but_never_labelled_verified(self):
        reading = MDC.classify(
            _snapshot(verification=MDC.VERIFICATION_SINGLE_SOURCE),
            MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.STATUS_DEGRADED, reading.status)
        self.assertEqual(MDC.FRESHNESS_FRESH, reading.freshness)
        self.assertTrue(reading.usable)
        self.assertEqual(
            MDC.VERIFICATION_SINGLE_SOURCE, reading.snapshot.verification,
        )

    def test_MD09_incomplete_snapshot_is_stale_with_reason(self):
        reading = MDC.classify(
            _snapshot(complete=False), MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.REASON_INCOMPLETE, reading.reason)

    def test_MD10_future_skewed_timestamp_is_not_fresh(self):
        """未来漂移的源时间不可采信：按超窗处理，不因"看起来更新"判 fresh。"""
        reading = MDC.classify(
            _snapshot(observed_at=_ago(-7200)), MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        self.assertEqual(MDC.STATUS_STALE, reading.status)

    def test_MD11_contract_never_reads_the_wall_clock(self):
        """``now`` 必须显式传入；契约不得自己取时钟。

        用 **AST** 而不是子串匹配：文档里出现 "``date.today()``"（正是在说明
        禁止它）不该让守卫变红，而真实调用必须被抓住。
        """
        with self.assertRaises(TypeError):
            MDC.classify(_snapshot(), MDC.LIVE_MARKET_POLICY)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "market_data_contract.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        clock_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("today", "now", "time", "monotonic"):
                    clock_calls.append(f"{node.func.attr}@line{node.lineno}")
        self.assertEqual(
            [], clock_calls,
            f"market data contract 读了墙上时钟：{clock_calls}",
        )

    def test_MD12_policies_are_single_sourced_and_named(self):
        """同一业务 freshness 规则只有一个来源。"""
        self.assertEqual(
            240.0, MDC.LIVE_MARKET_POLICY.max_age_seconds,
            "live market policy 的窗口改变了（调用层的既有语义是 240s）",
        )
        self.assertEqual(
            MDC.LIVE_MARKET_POLICY, MDC.policy_named("live_market"),
        )
        with self.assertRaises(ValueError):
            MDC.policy_named("does_not_exist")
        names = [policy.name for policy in MDC.POLICIES]
        self.assertEqual(len(names), len(set(names)), "policy 名字必须唯一")

    def test_MD13_projection_exposes_orthogonal_dimensions(self):
        """禁止把状态压成一个分数：维度必须全部保留。"""
        reading = MDC.classify(
            _snapshot(observed_at=_ago(60)), MDC.LIVE_MARKET_POLICY, now=NOW,
        )
        projection = reading.projection()
        for key in ("status", "availability", "freshness", "verification",
                    "as_of", "observed_at", "reason", "policy", "row_count"):
            self.assertIn(key, projection)
        self.assertNotIn("quality_score", projection)

    def test_MD14_projection_hides_provider_mechanics(self):
        """普通运行页面不需要 provider 细节（重试/熔断/缓存键）。"""
        projection = MDC.classify(
            _snapshot(), MDC.LIVE_MARKET_POLICY, now=NOW,
        ).projection()
        for leaked in ("eastmoney_retry", "tencent_fallback", "cache_key",
                       "circuit_open", "retry_after_seconds"):
            self.assertNotIn(leaked, projection)


# ---------------------------------------------------------------------------
# MDR：只读路径绝不联网
# ---------------------------------------------------------------------------


class MarketDataReadPathTests(unittest.TestCase):
    """MDR-01 ~ MDR-05：本 PR 最重要的新 regression。"""

    def test_MDR01_read_snapshot_with_cache_never_touches_provider(self):
        """有缓存 → 返回 facts + status，且**零** provider 调用。"""
        rows = _rows(3, quote_at=_ago(30))
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached", return_value=rows,
            ):
                reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual([], guard.attempts, "只读路径访问了 provider")
        self.assertTrue(reading.available)
        self.assertEqual(3, len(reading.rows()))

    def test_MDR02_read_snapshot_without_cache_is_unavailable_not_crash(self):
        """无缓存 → 明确 unavailable，既不崩也不联网。"""
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached", return_value=[],
            ):
                reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual([], guard.attempts, "只读路径访问了 provider")
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)
        self.assertIsNone(reading.snapshot)
        self.assertEqual([], list(reading.rows()))

    def test_MDR03_stale_read_positive_control(self):
        """stale positive control：report stale + 最后可信数据，不偷偷 refresh。"""
        rows = [{"code": "600000", "quote_at": _ago(7200), "price": 10.0}]
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached", return_value=rows,
            ):
                reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual([], guard.attempts, "stale 读取偷偷刷新了行情")
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.REASON_STALE, reading.reason)
        self.assertEqual(1, len(reading.rows()), "stale 必须保留最后可信事实")
        self.assertNotEqual(MDC.STATUS_FRESH, reading.status, "stale 被标成了 fresh")

    def test_MDR04_unavailable_read_positive_control(self):
        """unavailable positive control：绝不填充默认值。"""
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached", return_value=[],
            ):
                reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual([], guard.attempts, "只读路径访问了 provider")
        self.assertEqual(MDC.REASON_MISSING, reading.reason)
        self.assertEqual((), reading.rows())
        projection = reading.projection()
        self.assertIsNone(projection["as_of"])
        self.assertEqual(0, projection["row_count"])

    def test_MDR05_read_path_survives_provider_exception_internally(self):
        """持久化读取本身抛异常时也要降级成 unavailable，而不是 500。"""
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached",
                side_effect=RuntimeError("corrupt cache"),
            ):
                reading = MDSvc.read_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual([], guard.attempts)
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)

    def test_MDR06_strategy_allocation_explain_is_network_free(self):
        """已知 R23 债的生产入口：``strategy_allocation_explain`` 不联网。

        这正是本轮存在的理由 —— 迁移前每次只读请求都可能支付 ~13.8s
        provider 超时。
        """
        import paper_trading as PT

        _freeze_now(self)
        # 隔离账本：这个入口会 init_db()，不能碰 checkout 里的真实库。
        tmp = tempfile.mkdtemp(prefix="r24-explain-")
        patcher = mock.patch.object(PT, "DB_PATH", os.path.join(tmp, "paper.sqlite3"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached",
                return_value=_rows(3, quote_at=_ago(7200)),
            ):
                payload = PT.strategy_allocation_explain()
        self.assertEqual([], guard.attempts,
                         "allocation-explain 的行情读取访问了 provider")
        self.assertEqual(MDC.STATUS_STALE, payload["market_data"]["status"])
        self.assertEqual(5, len(payload["strategies"]),
                         "只读投影迁移后策略行数发生变化")

    def test_MDR07_dashboard_projection_is_network_free(self):
        """运行时只读投影同样不得联网。"""
        _freeze_now(self)
        with _NetworkForbidden(self) as guard:
            with mock.patch.object(
                dfc, "load_market_snapshot_full_cached",
                return_value=_rows(3, quote_at=_ago(30)),
            ):
                projection = MDSvc.read_projection()
        self.assertEqual([], guard.attempts, "运行时读投影访问了 provider")
        self.assertEqual(MDC.STATUS_FRESH, projection["status"])


# ---------------------------------------------------------------------------
# MDP：refresh 路径与 provider 失败 parity
# ---------------------------------------------------------------------------


class MarketDataRefreshTests(unittest.TestCase):
    """MDP-01 ~ MDP-06：refresh / 失败 / 缓存 / 多源语义与迁移前一致。"""

    def test_MDP01_refresh_success_returns_fresh_reading(self):
        rows = _full_rows(quote_at=_ago(10))
        with mock.patch.object(
            dfc, "fetch_market_snapshot_full", return_value=rows,
        ), mock.patch.object(
            dfc, "load_market_snapshot_full_cached", return_value=[],
        ):
            reading = MDSvc.refresh_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.STATUS_FRESH, reading.status)
        self.assertEqual(MDC.ACCESS_REFRESH, reading.access_mode)
        self.assertEqual(len(rows), len(reading.rows()))

    def test_MDP01b_undersized_refresh_is_incomplete_not_fresh(self):
        """一行"全市场快照"不是完整事实：必须 fail closed，不许标 fresh。"""
        with mock.patch.object(
            dfc, "fetch_market_snapshot_full", return_value=_rows(5, quote_at=_ago(5)),
        ), mock.patch.object(
            dfc, "load_market_snapshot_full_cached", return_value=[],
        ):
            reading = MDSvc.refresh_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.REASON_INCOMPLETE, reading.reason)
        self.assertEqual(MDC.FRESHNESS_STALE, reading.freshness)

    def test_MDP02_refresh_failure_keeps_last_known_and_marks_stale(self):
        """既有语义：failed refresh 回落 full-market cache，而不是假装成功。"""
        cached = [{"code": "600000", "quote_at": _ago(7200), "price": 9.0}]
        with mock.patch.object(
            dfc, "fetch_market_snapshot_full", return_value=[],
        ), mock.patch.object(
            dfc, "load_market_snapshot_full_cached", return_value=cached,
        ):
            reading = MDSvc.refresh_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.STATUS_STALE, reading.status)
        self.assertEqual(MDC.REASON_REFRESH_FAILED, reading.reason)
        self.assertEqual(1, len(reading.rows()), "旧事实必须保留")

    def test_MDP03_refresh_failure_without_cache_is_unavailable(self):
        with mock.patch.object(
            dfc, "fetch_market_snapshot_full", return_value=[],
        ), mock.patch.object(
            dfc, "load_market_snapshot_full_cached", return_value=[],
        ):
            reading = MDSvc.refresh_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)
        self.assertEqual(MDC.REASON_REFRESH_FAILED, reading.reason)
        self.assertIsNone(reading.snapshot)

    def test_MDP04_refresh_provider_exception_is_a_reading_not_a_raise(self):
        """provider 异常必须表达成 reading，而不是让调用方各自 try/except。"""
        with mock.patch.object(
            dfc, "fetch_market_snapshot_full",
            side_effect=RuntimeError("provider exploded"),
        ), mock.patch.object(
            dfc, "load_market_snapshot_full_cached", return_value=[],
        ):
            reading = MDSvc.refresh_snapshot(MDC.LIVE_MARKET_POLICY, now=NOW)
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)
        self.assertEqual(MDC.REASON_REFRESH_FAILED, reading.reason)

    def test_MDP05_refresh_requires_explicit_now(self):
        with self.assertRaises(MDSvc.MarketDataAccessError):
            MDSvc.refresh_snapshot(MDC.LIVE_MARKET_POLICY, now=None)

    def test_MDP06_access_mode_is_the_only_network_permission(self):
        """``read`` 不允许联网，``refresh`` 允许；未知模式 fail closed。"""
        self.assertFalse(MDC.access_mode_allows_network(MDC.ACCESS_READ))
        self.assertTrue(MDC.access_mode_allows_network(MDC.ACCESS_REFRESH))
        self.assertFalse(MDC.access_mode_allows_network(""))
        self.assertFalse(MDC.access_mode_allows_network("whatever"))

    def test_MDP07_forex_payload_flags_are_rejected(self):
        """契约在构造期就拒绝伪造的 verification / reason 取值。"""
        with self.assertRaises(ValueError):
            MDC.MarketDataSnapshot(kind="k", rows=(), verification="totally_fine")
        with self.assertRaises(ValueError):
            MDC.MarketDataSnapshot(kind="k", rows=(), degraded_reason="no_data")

    def test_MDP08_reading_cannot_disagree_with_its_payload(self):
        """``unavailable`` 与 ``snapshot=None`` 必须互相蕴含。"""
        with self.assertRaises(ValueError):
            MDC.MarketDataReading(
                availability=MDC.AVAILABILITY_UNAVAILABLE,
                freshness=MDC.FRESHNESS_UNKNOWN, status=MDC.STATUS_UNAVAILABLE,
                policy_name="live_market", snapshot=_snapshot(),
            )
        with self.assertRaises(ValueError):
            MDC.MarketDataReading(
                availability=MDC.AVAILABILITY_AVAILABLE,
                freshness=MDC.FRESHNESS_FRESH, status=MDC.STATUS_FRESH,
                policy_name="live_market", snapshot=None,
            )

    def test_MDP09_verification_mapping_reuses_existing_business_terms(self):
        """复用既有业务术语，不发明第五种名字。"""
        self.assertEqual(
            MDC.VERIFICATION_VERIFIED,
            MDC.verification_from_cross_status("cross_source_checked"),
        )
        self.assertEqual(
            MDC.VERIFICATION_DISAGREEMENT,
            MDC.verification_from_cross_status("cross_source_failed"),
        )
        self.assertEqual(
            MDC.VERIFICATION_UNAVAILABLE,
            MDC.verification_from_cross_status("cross_source_unavailable"),
        )
        self.assertEqual(
            MDC.VERIFICATION_NOT_ATTEMPTED,
            MDC.verification_from_cross_status("a_fifth_name"),
        )

    def test_MDP10_worst_verification_aggregates_conservatively(self):
        self.assertEqual(
            MDC.VERIFICATION_DISAGREEMENT,
            MDC.worst_verification(
                [MDC.VERIFICATION_VERIFIED, MDC.VERIFICATION_DISAGREEMENT]
            ),
        )
        self.assertEqual(
            MDC.VERIFICATION_VERIFIED,
            MDC.worst_verification([MDC.VERIFICATION_VERIFIED]),
        )
        self.assertEqual(
            MDC.VERIFICATION_NOT_ATTEMPTED, MDC.worst_verification([]),
        )


# ---------------------------------------------------------------------------
# MDPIT：as-of / PIT
# ---------------------------------------------------------------------------


class MarketDataPointInTimeTests(unittest.TestCase):
    """MDPIT-01 ~ MDPIT-04：历史请求绝不被 current snapshot 回填。"""

    def test_MDPIT01_current_snapshot_never_fills_an_earlier_asof(self):
        """as-of D 请求遇到 D+1 事实 → fail closed，绝不 current-fill。"""
        reading = MDC.classify(
            _snapshot(observed_at=NOW.isoformat()), MDC.LIVE_MARKET_POLICY,
            now=NOW, asof_day="2026-08-27",
        )
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)
        self.assertEqual(MDC.REASON_ASOF_MISMATCH, reading.reason)
        self.assertIsNone(reading.snapshot)
        self.assertEqual((), reading.rows())

    def test_MDPIT02_same_day_snapshot_satisfies_its_own_asof(self):
        reading = MDC.classify(
            _snapshot(observed_at=NOW.isoformat()), MDC.LIVE_MARKET_POLICY,
            now=NOW, asof_day="2026-08-28",
        )
        self.assertEqual(MDC.AVAILABILITY_AVAILABLE, reading.availability)
        self.assertEqual(1, len(reading.rows()))

    def test_MDPIT03_unprovable_asof_fails_closed(self):
        """快照没有可解析业务日时不得猜。"""
        reading = MDC.classify(
            _snapshot(observed_at="garbage"), MDC.LIVE_MARKET_POLICY,
            now=NOW, asof_day="2026-08-28",
        )
        self.assertEqual(MDC.STATUS_UNAVAILABLE, reading.status)
        self.assertEqual(MDC.REASON_ASOF_UNPROVABLE, reading.reason)

    def test_MDPIT04_earlier_snapshot_is_acceptable_for_a_later_asof(self):
        """``observed_day <= requested`` 是允许的 —— 不是任何晚于都拒绝。"""
        reading = MDC.classify(
            _snapshot(observed_at="2026-08-27T14:00:00+08:00"),
            MDC.LIVE_MARKET_POLICY, now=NOW, asof_day="2026-08-28",
        )
        self.assertEqual(MDC.AVAILABILITY_AVAILABLE, reading.availability)
        self.assertEqual(1, len(reading.rows()))


# ---------------------------------------------------------------------------
# MDG：architecture guard
# ---------------------------------------------------------------------------


class MarketDataArchitectureGuardTests(unittest.TestCase):
    """MDG-01 ~ MDG-06：依赖方向与"只读不联网"不得回流。

    一律用 **AST / import-level** 检查，不做脆弱的子串搜索 —— 文档里解释
    "禁止 X" 不该让守卫变红，而真实的 X 必须被抓住。
    """

    @staticmethod
    def _path(name):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

    @classmethod
    def _tree(cls, name):
        with open(cls._path(name), encoding="utf-8") as handle:
            return ast.parse(handle.read())

    @classmethod
    def _imported_modules(cls, name):
        """模块级与函数级 import 的顶层模块名集合。"""
        found = set()
        for node in ast.walk(cls._tree(name)):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
        return found

    @classmethod
    def _called_attrs(cls, name):
        """函数体内被调用的属性名集合（``x.commit()`` → ``commit``）。"""
        found = set()
        for node in ast.walk(cls._tree(name)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                found.add(node.func.attr)
        return found

    def test_MDG01_contract_has_zero_project_imports(self):
        """契约是纯的：只依赖标准库，不 import 任何项目模块、不碰 IO。"""
        stdlib_ok = {"__future__", "datetime", "dataclasses", "types", "typing"}
        imported = self._imported_modules("market_data_contract.py")
        self.assertEqual(
            set(), imported - stdlib_ok,
            f"market data contract 引入了非纯依赖：{sorted(imported - stdlib_ok)}",
        )

    def test_MDG02_service_never_opens_a_db_or_transaction(self):
        """authority 不拥有事务：provider I/O 不可能进入 writer lock。"""
        imported = self._imported_modules("market_data_service.py")
        for forbidden in ("sqlite3", "paper_trading", "paper_storage"):
            self.assertNotIn(
                forbidden, imported,
                f"market data service 引入了账本依赖：{forbidden}",
            )
        for forbidden in ("commit", "rollback", "executescript"):
            self.assertNotIn(
                forbidden, self._called_attrs("market_data_service.py"),
                f"market data service 触碰了事务：{forbidden}",
            )

    def test_MDG03_read_path_does_not_call_the_provider_refresh_function(self):
        """``read_snapshot`` 函数体内不得出现任何 provider 刷新调用。"""
        source_path = self._path("market_data_service.py")
        with open(source_path, encoding="utf-8") as handle:
            raw = handle.read()
        tree = ast.parse(raw)
        node = next(
            item for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef) and item.name == "read_snapshot"
        )
        names = {
            child.func.attr if isinstance(child.func, ast.Attribute)
            else getattr(child.func, "id", "")
            for child in ast.walk(node) if isinstance(child, ast.Call)
        }
        for forbidden in ("fetch_market_snapshot_full", "fetch_market_snapshot",
                          "fetch_realtime_for_codes", "fetch_indices", "http_get"):
            self.assertNotIn(
                forbidden, names,
                f"只读入口 read_snapshot 里出现了 provider 调用：{forbidden}",
            )

    def test_MDG04_allocation_explain_no_longer_calls_the_provider(self):
        """已知 R23 债：该只读函数不得再直接调 provider。"""
        tree = self._tree("paper_trading.py")
        node = next(
            item for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef) and item.name == "strategy_allocation_explain"
        )
        names = {
            child.func.attr if isinstance(child.func, ast.Attribute)
            else getattr(child.func, "id", "")
            for child in ast.walk(node) if isinstance(child, ast.Call)
        }
        self.assertNotIn(
            "fetch_market_snapshot_full", names,
            "strategy_allocation_explain 又直接调用了 provider（R23 债回流）",
        )
        self.assertIn(
            "read_snapshot", names,
            "allocation-explain 不再消费 Market Data Authority",
        )

    def test_MDG05_frontend_does_not_recompute_freshness(self):
        """前端不得复制后端 freshness 规则。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "frontend", "src", "features", "paper.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        # 真实风险是"拿本机时钟与一个毫秒阈值比较"；注释里提到这个反例不算。
        for forbidden in ("Date.now()-timestamp", "Date.now() - timestamp",
                          "240 * 1000", ">240000"):
            self.assertNotIn(
                forbidden, source,
                f"前端复制了后端 freshness 阈值：{forbidden}",
            )
        self.assertIn("paperMarketDataHtml", source,
                      "前端不再渲染后端给出的行情状态")
        # 前端必须消费后端给出的 reason，而不是自己推断失败原因。
        self.assertIn("md.reason", source,
                      "前端不再展示后端给出的 reason")

    def test_MDG06_read_paths_have_no_synchronous_provider_refresh(self):
        """只读 API 入口不得直接出现 provider 刷新调用。"""
        tree = self._tree("main.py")
        for name in ("hot", "health"):
            node = next(
                item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == name
            )
            names = {
                child.func.attr if isinstance(child.func, ast.Attribute)
                else getattr(child.func, "id", "")
                for child in ast.walk(node) if isinstance(child, ast.Call)
            }
            self.assertNotIn(
                "fetch_market_snapshot_full", names,
                f"只读入口 {name}() 又同步刷新了全市场快照",
            )

    def test_MDG07_read_only_consumers_use_the_authority_not_the_provider(self):
        """已迁移的只读消费者必须走 authority，而不是回退到 provider。"""
        targets = (
            ("paper_trading.py", "strategy_allocation_explain", "read_snapshot"),
            ("paper_trading.py", "_market_state", "refresh_rows"),
            ("main.py", "hot", "read_snapshot"),
            ("main.py", "health", "market_health_projection"),
        )
        for module, function, expected in targets:
            with self.subTest(module=module, function=function):
                tree = self._tree(module)
                node = next(
                    item for item in ast.walk(tree)
                    if isinstance(item, ast.FunctionDef) and item.name == function
                )
                names = {
                    child.func.attr if isinstance(child.func, ast.Attribute)
                    else getattr(child.func, "id", "")
                    for child in ast.walk(node) if isinstance(child, ast.Call)
                }
                self.assertIn(
                    expected, names,
                    f"{module}.{function} 不再经 Market Data Authority（{expected}）",
                )
                self.assertNotIn(
                    "fetch_market_snapshot_full", names,
                    f"{module}.{function} 回退到了 provider",
                )

    def test_MDG08_service_module_is_the_single_authority(self):
        """authority 只有一份实现：其他模块不得各自再写一套快照**读取**。

        直接读 ``market_snapshot_full.json`` 会绕过 ``_full_snapshot_payload_is_complete``
        的完整性校验。用 AST 判定"这个路径真的被交给 open()/json.load()"，
        因此缓存键签名、注释与演示夹具（写方）都不会误报。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        backend = os.path.join(root, "backend")
        allowed = {
            "market_data_service.py",   # authority 本身
            "data_fetcher.py",          # provider/cache 机制（写方）
            "marketdata_cache.py",      # 缓存原语
            "demo_seed.py",             # 演示夹具：构造快照，不是消费者
        }
        offenders = []
        for name in sorted(os.listdir(backend)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            if name in allowed:
                continue
            with open(os.path.join(backend, name), encoding="utf-8") as handle:
                raw = handle.read()
            if "MARKET_SNAPSHOT_FULL_CACHE_PATH" not in raw:
                continue
            tree = ast.parse(raw)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = (node.func.attr if isinstance(node.func, ast.Attribute)
                          else getattr(node.func, "id", ""))
                # 只有真正的**读取**才算绕过校验；``stat`` 式缓存键签名不算。
                if callee not in ("open", "load", "loads", "read_text",
                                  "read_bytes"):
                    continue
                if not any(
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "MARKET_SNAPSHOT_FULL_CACHE_PATH"
                    for arg in list(node.args) + [kw.value for kw in node.keywords]
                    for inner in ast.walk(arg)
                ):
                    continue
                offenders.append(f"{name}:{callee}@line{node.lineno}")
        self.assertEqual(
            [], offenders,
            f"这些模块绕过 authority 直接读快照文件：{offenders}",
        )

    def test_MDG09_health_does_not_keep_a_second_freshness_verdict(self):
        """``/api/health`` 不得在 authority 之外再判一次 fresh/stale。

        同一个响应里出现两个权威口径，会让"这条行情可不可信"没有唯一答案
        （R24 之前 health 自己用 ``age <= 1800`` 重算了一遍结论）。
        """
        tree = self._tree("main.py")
        node = next(
            item for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef) and item.name == "health"
        )
        assignments = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign):
                continue
            for target in child.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "status"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "live_snapshot"
                ):
                    assignments.append(child)
        self.assertTrue(assignments, "health 不再投影 live_snapshot.status")
        for assign in assignments:
            dumped = ast.dump(assign.value)
            self.assertIn(
                "market_data", dumped,
                "health 自己算了一遍 live_snapshot.status（应投影 authority）",
            )
            self.assertNotIn(
                "fresh", dumped,
                "health 里重新出现了内联 fresh 判决",
            )


if __name__ == "__main__":
    unittest.main()
