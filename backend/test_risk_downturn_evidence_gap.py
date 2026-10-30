# -*- coding: utf-8 -*-
"""2026-10-30 P0「下跌确认不可达 · 日内高低缺位」回归（RDEG-1 ~ RDEG-7）。

现网现场（oci-main，master@72c550b，2026-10-30 11:19 真实审计）::

    601956 东贝集团（趋势波段优选）
      level=warning, confirmed=True, basis=price_evidence, sell_ratio=0.0
      当日跌幅 -10.00%、相对沪深300 -8.97%、盘中高点回撤 10.00%
      main_force_intent: class=uncertain, distribution_score=40.0,
                          washout_score=19.0, confidence=0.4, usable=0.56
      missing=['主力净流入占比', '日内高低价', '超大单资金', '量比']

卡点（**只陈述，不重新论证**）：

1. ``distribution_score`` 的 14 分项「收盘靠近日内低位」需要 ``日内高低价``；
   全市场快照只有 price，``low/high`` 缺失 → 这 14 分永远拿不到 → 分布分恰好
   卡在 ``DOWNTURN_DISTRIBUTION_FLOOR=50`` 之下 10 分 → ``uncertain``。
2. ``downturn`` 分支还要求价格类证据齐全（无 ``price_missing``），而
   ``日内高低价`` 缺失让它直接出局。
3. 结果：跌停级、已两轮确认的票 **确认了却不减仓**（warning 级
   ``sell_ratio=0.0``）。

本组用例钉住修复后的语义边界：

* 「日内高低价」缺失 = **证据缺位**（中性，不加不扣），不再掩盖"证据充分"；
* 缺位中性口径的门槛比完整证据口径**更严**（自身深跌 ≤ -7% **且**明显跑输
  ≤ -5%），绝不因为缺证据就放松真正的阈值，也绝不因缺证据凭空加分；
* ``downturn`` 只允许 **partial**；``full`` 仍然只认 ``distribution``；
* 洗盘占优 / ``recovery_hold`` 一律不得落到 ``downturn``；
* 既有 ``distribution`` / ``washout`` / ``uncertain`` 判定逐字不变。
"""
from __future__ import annotations

import os
import sys
import unittest

BACKEND = os.path.dirname(os.path.abspath(__file__))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import paper_risk_decision as PRD  # noqa: E402
import paper_risk_evidence as PREv  # noqa: E402
import strategy_policies as SPOL  # noqa: E402

#: 现网 601956 东贝集团 2026-10-30 11:19 的真实快照（相对沪深300 -8.97%
#: ⇒ 当日指数 -1.03%）。跌幅/相对值都取自真实审计记录，不做任何美化。
DB_PCT = -10.00
DB_RELATIVE = -8.97
MARKET_PCT = DB_PCT - DB_RELATIVE        # -1.03

#: 同日实测的第二档：603239 浙江仙通 11:19 -7.28%（相对 -6.25%）、
#: 11:35 回升到 -6.13%（相对 -5.10%）。后者按缺位中性口径**不应**再判下跌确认。
XT_PCT_DEEP = -7.28
XT_PCT_RECOVERED = -6.13

#: 603341 龙旗科技 -4.78%（相对 -3.75%）：普通回调，绝不能因缺日内高低而升级。
LQ_PCT = -4.78


class RangeAbsentDownturnTests(unittest.TestCase):
    """纯域层语义（不碰 DB / 不碰时钟）：``main_force_intent`` 的缺位中性口径。"""

    ACCOUNT = "trend_pullback"        # 趋势波段优选（现网现场的那个策略）

    def position(self, *, cost=9.0):
        return {
            "account_id": self.ACCOUNT, "code": "601956", "name": "东贝集团",
            "qty": 300, "available_qty": 300, "cost": cost, "peak_price": cost,
            "today_acquired_qty": 0,
            "entry_date": "2026-10-29",
        }

    def intent(self, quote, *, market_pct=MARKET_PCT, news=None, cost=9.0):
        market = {} if market_pct is None else {"live_index_pct": market_pct}
        return PRD.main_force_intent(
            self.position(cost=cost), quote, market=market,
            news=[] if news is None else news)

    def guard(self, quote, *, market_pct=MARKET_PCT, news=None, cost=9.0):
        market = {} if market_pct is None else {"live_index_pct": market_pct}
        return PREv.intraday_downside_guard(
            self.position(cost=cost), quote, market=market,
            news=[] if news is None else news,
            asof_day="2026-10-30",
            deps=_EVIDENCE_DEPS,
        )

    # ── (i) 跌停 + 日内高低缺失 + 资金三件套缺失 → downturn / partial ──────
    def test_rdeg1_limit_down_range_absent_reaches_partial_not_full(self):
        quote = {"price": 6.03, "pct": DB_PCT}     # 无 high / low / 资金字段
        intent = self.intent(quote)
        self.assertEqual(intent["classification"], "downturn")
        self.assertEqual(intent["label"], "下跌确认（资金与日内高低证据缺失）")
        self.assertIn("日内高低缺失按中性不加分", intent["action_hint"])
        # 缺口如实上报（审计不遮丑），但缺位中性口径下它不再阻断判定。
        self.assertEqual(intent["price_evidence_missing"], ["日内高低价"])
        self.assertEqual(intent["price_evidence_missing_effective"], [])
        self.assertEqual(set(intent["flow_evidence_missing"]),
                         {"主力净流入占比", "超大单资金", "量比"})
        # **绝不因缺证据凭空加分**：可观测分布分仍是 28（跌幅）+ 12（相对）= 40。
        self.assertEqual(intent["distribution_score"], 40.0)
        self.assertEqual(intent["washout_score"], 19.0)
        self.assertTrue(intent["range_evidence_neutral"])
        self.assertTrue(intent["range_absent_deep_drop_leg"])
        self.assertTrue(intent["range_absent_relative_leg"])

        guard = self.guard(quote)
        policy = SPOL.intraday_downside_policy(self.ACCOUNT)
        self.assertEqual(guard["level"], "partial")
        self.assertEqual(guard["sell_ratio"], policy["partial_ratio"])
        self.assertNotEqual(guard["level"], "full")
        self.assertLess(guard["sell_ratio"], 1.0)
        self.assertEqual(guard["confirm_basis"], "price_evidence")
        # 审计文案必须说清"缺了什么、按什么判的"。
        self.assertIn("看不到日内高低与资金流", guard["reason"])
        self.assertIn("按价格证据判为下跌确认", guard["reason"])
        self.assertIn("只到部分减仓", guard["reason"])

    # ── (ii) 普通回调不得因缺日内高低而被误判深跌 ─────────────────────────
    def test_rdeg2_ordinary_pullback_never_becomes_downturn(self):
        for label, pct, market_pct in (
            ("龙旗 -4.78% / 相对 -3.75%", LQ_PCT, LQ_PCT - (-3.75)),
            ("仙通回升 -6.13% / 相对 -5.10%", XT_PCT_RECOVERED,
             XT_PCT_RECOVERED - (-5.10)),
        ):
            with self.subTest(label):
                quote = {"price": 6.03, "pct": pct}
                intent = self.intent(quote, market_pct=market_pct)
                self.assertEqual(
                    intent["classification"], "uncertain",
                    "缺日内高低不得让普通回调升级成下跌确认")
                self.assertFalse(intent["range_evidence_neutral"])
                self.assertFalse(intent["range_absent_deep_drop_leg"])
                guard = self.guard(quote, market_pct=market_pct)
                self.assertNotEqual(guard["level"], "partial")
                self.assertNotEqual(guard["level"], "full")
                self.assertEqual(guard["sell_ratio"], 0.0)

    # ── (iii) 洗盘占优不得落到 downturn ────────────────────────────────────
    def test_rdeg3_washout_dominant_blocks_downturn(self):
        # 资金证据齐全但**指向洗盘**（主力净流入 +3%、超大单净流入），
        # 同时日内高低缺失（缺位中性口径的两条腿都成立）。
        quote = {
            "price": 6.03, "pct": DB_PCT,
            "main_pct": 3.0, "super_net": 1.0e6, "vol_ratio": 1.6,
        }
        intent = self.intent(quote)
        self.assertGreater(intent["washout_score"], intent["distribution_score"])
        self.assertNotEqual(
            intent["classification"], "downturn",
            "洗盘占优时绝不允许落到下跌确认")
        guard = self.guard(quote)
        self.assertNotEqual(guard["level"], "partial")
        self.assertNotEqual(guard["level"], "full")
        self.assertEqual(guard["sell_ratio"], 0.0)

    # ── (iv) 日内高低存在时分布分更高（保持原口径，不因缺位而加分）────────
    def test_rdeg4_observed_range_scores_higher_than_missing(self):
        without = self.intent({"price": 6.03, "pct": DB_PCT})
        # 真跌停：最低价 = 现价 = 6.03 ⇒ range_pos = 0 ⇒ 「收盘靠近日内低位」成立。
        with_range = self.intent(
            {"price": 6.03, "pct": DB_PCT, "high": 6.31, "low": 6.03})
        self.assertEqual(without["range_position"], None)
        self.assertEqual(with_range["range_position"], 0.0)
        self.assertEqual(
            with_range["distribution_score"] - without["distribution_score"],
            PRD.RANGE_POSITION_DISTRIBUTION_POINTS,
            "观测到日内高低时分布分必须比缺失时高（且缺失时不得凭空补分）")
        self.assertEqual(with_range["price_evidence_missing"], [])
        self.assertEqual(with_range["classification"], "downturn")

    # ── (v) 既有 distribution / washout / uncertain 判定逐字不变 ───────────
    def test_rdeg5_existing_classifications_are_unchanged(self):
        # distribution：资金证据齐全且指向出货。
        distribution = self.intent({
            "price": 6.03, "pct": DB_PCT, "high": 6.31, "low": 6.03,
            "main_pct": -6.0, "super_net": -2.0e6, "vol_ratio": 1.8})
        self.assertEqual(distribution["classification"], "distribution")
        self.assertEqual(distribution["label"], "疑似出货")
        self.assertEqual(distribution["action_hint"],
                         "冻结加仓，优先复核可卖仓位")
        self.assertEqual(distribution["missing"], [])

        # washout：资金证据指向洗盘，且收复日内低位（range_pos ≥ 0.60）。
        washout = self.intent({
            "price": 9.179, "pct": -8.21, "high": 9.25, "low": 9.0,
            "main_pct": 3.0, "super_net": 1.0e6, "vol_ratio": 1.6},
            market_pct=-1.41)
        self.assertEqual(washout["classification"], "washout")
        self.assertEqual(washout["label"], "疑似洗盘")
        self.assertEqual(washout["action_hint"],
                         "不因单日下跌单独清仓，等待承接确认")

        # uncertain：证据不足（原口径不动）。
        uncertain = self.intent({"price": 10.0, "pct": -1.5})
        self.assertEqual(uncertain["classification"], "uncertain")
        self.assertEqual(uncertain["label"], "意图不确定")
        self.assertEqual(uncertain["action_hint"], "不得据此单独下单")

    # ── (vi) 指数不可用时相对腿不成立：宁可 uncertain 也不猜 ──────────────
    def test_rdeg6_market_reference_missing_never_upgrades(self):
        intent = self.intent({"price": 6.03, "pct": DB_PCT}, market_pct=None)
        self.assertEqual(intent["relative_to_market_pct"], None)
        self.assertFalse(intent["range_absent_relative_leg"])
        self.assertNotEqual(intent["classification"], "downturn")
        self.assertEqual(intent["classification"], "uncertain")

    # ── (vii) 缺位中性口径的阈值边界：-7% / -5% 必须精确生效 ──────────────
    def test_rdeg7_threshold_boundaries_are_exact(self):
        # 自身 -7.00%（恰好达线）+ 相对 -5.00%（恰好达线）→ 成立。
        at_limit = self.intent({"price": 6.03, "pct": -7.00},
                               market_pct=-7.00 - (-5.00))
        self.assertEqual(at_limit["classification"], "downturn")
        # 自身 -6.99%（差 0.01pp 未达线）→ 不成立。
        just_above = self.intent({"price": 6.03, "pct": -6.99},
                                 market_pct=-6.99 - (-5.00))
        self.assertEqual(just_above["classification"], "uncertain")
        # 相对 -4.99%（差 0.01pp 未达线）→ 不成立。
        rel_above = self.intent({"price": 6.03, "pct": -10.00},
                                market_pct=-10.00 - (-4.99))
        self.assertEqual(rel_above["classification"], "uncertain")

    # ── (viii) 洗盘占优（分数占优，不是分类占优）不得落到 downturn ───────
    def test_rdeg8_washout_score_dominance_blocks_downturn(self):
        """洗盘**分数**占优时，缺位中性口径必须否决下跌确认。

        构造：跌停级（深跌腿成立）+ 明显跑输（相对腿成立），但资金字段
        **部分**可得且偏向洗盘（主力净流入 +2% → 洗盘 +18），
        于是分布分 40 - 洗盘分 37 差距不足 10 分。
        """
        quote = {
            "price": 6.03, "pct": DB_PCT,
            "main_pct": 2.0,       # 主力仍为净流入 → 洗盘 +18
        }
        intent = self.intent(quote)
        self.assertTrue(intent["range_evidence_neutral"])
        self.assertLess(
            intent["distribution_score"] - intent["washout_score"],
            PRD.DOWNTURN_RANGE_ABSENT_WASHOUT_GAP,
            "本用例刻意构造洗盘分数占优的场景")
        self.assertNotEqual(
            intent["classification"], "downturn",
            "洗盘分数占优时绝不允许落到下跌确认")
        guard = self.guard(quote)
        self.assertNotEqual(guard["level"], "partial")
        self.assertEqual(guard["sell_ratio"], 0.0)

    # ── (ix) recovery_hold（低开高走收复日内低点）优先于下跌确认 ──────────
    def test_rdeg9_recovery_hold_blocks_downturn(self):
        """日内T 的"低开高走"恢复形态不得触发任何下跌减仓。

        ``recovery_hold`` 的成立条件含 ``pct >= -1.0``，与下跌确认的触发条件
        （当日跌幅 ≤ -7%）**互斥**：恢复形态与崩塌形态在同一条行情上不可能
        同时成立。守卫里的 ``downturn = False`` 是**防御性**的（保证即使
        分类被改坏，恢复通道也不会被下跌确认抢走），这里把这条互斥性与
        "恢复通道永不减仓"一起钉住。
        """
        # 低开（pct -0.5%）后收复日内低点 2%：low=9.0、price=9.18 → +2%。
        quote = {"price": 9.18, "pct": -0.5, "high": 9.30, "low": 9.0}
        position = self.position()
        position["account_id"] = "tq_breakout"
        guard = PREv.intraday_downside_guard(
            position, quote, market={"live_index_pct": 0.0}, news=[],
            asof_day="2026-10-30", deps=_EVIDENCE_DEPS)
        self.assertTrue(guard["recovery_hold"])
        self.assertNotEqual(guard["level"], "partial")
        self.assertNotEqual(guard["level"], "full")
        self.assertEqual(guard["sell_ratio"], 0.0)

        # 互斥性：恢复形态要求 pct ≥ -1%，下跌确认要求 pct ≤ -7%，两者
        # 不可能同时成立——所以 recovery_hold 永远压得住下跌确认。
        self.assertLess(PRD.DOWNTURN_RANGE_ABSENT_PCT_THRESHOLD, -1.0)


#: ``intraday_downside_guard`` 只需要 ``RiskEvidenceDeps`` 里的策略行为表；
#: 本组用例不读 DB、不取时钟，用最小依赖对象即可。
_EVIDENCE_DEPS = PREv.RiskEvidenceDeps(
    lot_size=100,
    max_sells_per_run=5,
    blocked_retry_minutes=30,
    small_pct=0.05,
    permission_scope_exit_max_per_strategy_day=1,
    permission_scope_exit_ratio=0.5,
    protective_exit_classes=frozenset({"hard_stop", "trailing_stop", "max_hold"}),
    entry_retry_signal_statuses=("pending",),
    entry_frozen_waitlist_status="deferred_capacity",
    intraday_interval_minutes=3,
    holding_quality_weights={},
    strategy_risk_behaviors={},
    main_force_strategy_id="main_force",
    review_policy=None,
    risk_version="v0",
    alt_data=None,
    load_kline=lambda *args, **kwargs: None,
)


if __name__ == "__main__":
    unittest.main()
