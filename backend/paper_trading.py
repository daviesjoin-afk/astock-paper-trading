# -*- coding: utf-8 -*-
"""低占用、可审计的 A 股模拟交易引擎。

只处理虚拟资金和规则化成交假设，不连接券商、不保存真实账户信息，也不发送实盘委托。
自动任务由 ``paper_runner.py`` 作为一次性进程执行，避免 Web 服务启动后持续扫描市场。
"""
from __future__ import annotations

import datetime as dt
import gc
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pandas as pd

import data_fetcher as dfc
import decision_engine as DE
import factors as F
import strategies as S
import universe as U
import risk_center as RC
import news_learning as NL
import paper_research as PR
from market_policy import market_light_scale, market_light_scales
try:
    import disclosure_timeline as DT
except ImportError:  # 数据源模块故障不能中断风险卖出或候选扫描。
    DT = None
try:
    import alt_data as AD
except ImportError:  # 替代数据层缺失只降级信号丰富度，不中断交易。
    AD = None
try:
    import entry_timing as ET
except ImportError:  # 时机状态机缺失时退回原有逐轮评估行为。
    ET = None
try:
    import ignition_entry as IGN
except ImportError:  # 点火通道缺失时主力候选退回常规承接，不中断交易。
    IGN = None

# 点火买入总开关（2026-08-31 复核结论）：影子对比期默认关闭——点火区间
# 候选照常被 30 秒通道确认并记录影子对比（原规则 vs 点火规则，命中后
# 30/60 分钟表现），但入场评估层始终以 blocker 拦下，不产生真实委托。
# 影子数据足够后再置 PAPER_IGNITION_BUY=1 正式放开。
IGNITION_BUY_ENABLED = os.environ.get("PAPER_IGNITION_BUY", "").strip().lower() in {
    "1", "true", "yes",
}

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "data_cache", "paper_trading.sqlite3")
REPORT_DIR = os.path.join(BASE, "reports")
SELECTION_FACTORS_PATH = os.path.join(BASE, "data_cache", "selection_factors.csv")
SELECTION_META_PATH = os.path.join(BASE, "data_cache", "selection_cache.json")
ARCHIVE_ORDERS_CACHE_PATH = os.path.join(BASE, "data_cache", "paper_archive_orders_projection.json")
# 日线因子只在足够覆盖可交易全市场的完整交易日时才允许覆盖上一版本。
# 盘后源短暂限流时，宁可保留最后一份已验证因子，也不能用几百只股票
# 的截面重排全市场。候选阶段还会再次按策略时效复核该覆盖率。
# 盘后增量期间允许小幅缺口，避免少量源失败冻结整批新候选；
# 低于该值仍保留待买名单，风险卖出不受影响。
SELECTION_FACTOR_MIN_COVERAGE = 0.85

# 佣金万一免五（2026-08-28 起）：万1费率，无 5 元最低收费；印花税/滑点不变
COMMISSION = 0.0001
MIN_COMMISSION = 0.0
STAMP_SELL = 0.0005
SLIPPAGE = 0.001
NEWS_UNVERIFIED_TTL_SECONDS = 12 * 60 * 60
NEWS_VERIFIED_TTL_SECONDS = 3 * 24 * 60 * 60
_NEWS_SCAN_META = {"observed_at": None, "stale": False, "error": None}
RISK_VERSION = "paper-risk-v4"
# The reported-profit breakout model is an independent paper account.  Keep
# its identity/version explicit in every execution payload so a future model
# change cannot be mistaken for one of the legacy three accounts.
NEW_STRATEGY_ID = "reported_profit_breakout"
NEW_STRATEGY_VERSION = "reported-profit-breakout-v1"
MAIN_FORCE_STRATEGY_ID = "main_force_top10"
MAIN_FORCE_STRATEGY_VERSION = "main-force-top10-v1"
LOT_SIZE = 100
# 集中化配对约束：席位稀缺（15席）时，低于该金额的新开仓是"尘埃仓"——
# 占用一席却几乎不承载资金（2026-08-25 000978 案例黄灯预算剩 ¥3.2K 仍
# 买出 ¥1.9K 仓）。预算不足时转入 deferred 队列等轮换释放额度，而不是
# 用小仓填满席位。仅约束预算侧约束（cash/weight/exposure/industry）
# 导致的小单；risk 约束导致的小单是低价股合法整手规模，不受此限。
MIN_ORDER_AMOUNT = 10000.0
# The three strategies make independent decisions but draw from one capital
# pool.  Their historical profile exposure values are kept as sizing hints;
# using any one of them as the pool ceiling makes the other strategies see a
# permanently full account (for example 70% pool utilisation is already above
# the 55% breakout ceiling).  Keep one explicit pool-level hard cap instead.
SHARED_POOL_MAX_EXPOSURE = 0.82
# 超强主力资金优先（2026-09-03）：该策略池内收益最高、仅 3 席集中持仓，
# 市场黄灯 60% 缩水后目标额度可能不足一手高价股（例：300489 一手
# ¥23,202，黄灯下剩余额度仅约 ¥20,000）。为其保留不随灯色缩水的最低
# 预算份额（占共享池 NAV 比例），只抬高可下单额度，不改变
# _price_aware_qty “金额约束仅影响下单规模”的机制，也不突破 82% 池硬上限。
MAIN_FORCE_PRIORITY_FLOOR_PCT = 0.15
# 三分钟频率兼顾开盘/午后节奏与全市场双源校验耗时；09:30、13:00 仍由首轮立即触发。
INTRADAY_INTERVAL_MINUTES = 3
INTRADAY_WINDOWS = (("09:30", "11:25"), ("13:00", "14:55"))
# 开盘事件是共享执行引擎，三套策略只通过各自的策略画像决定阈值和仓位比例。
# 这样既能在 09:30/09:31 抓住冲高回落，也不会把趋势/轮动策略改造成追涨策略。
OPENING_EVENT_CLOCKS = {"09:30", "09:31", "13:00"}
OPENING_EVENT_POLICIES = {
    "tq_breakout": {
        "name": "短线日内做T",
        "enabled": True,
        "min_peak_pct": 3.0,
        "min_retrace_pct": 2.2,
        "min_current_pct": -1.0,
        "min_peak_edge_pct": -8.0,
        "trim_ratio": 0.30,
        "allow_loss_trim": True,
        "rebuy_rebound_pct": 1.2,
        "rebuy_max_sold_ratio": 0.995,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": -0.25,
        "rebuy_min_current_pct": -1.5,
    },
    "trend_pullback": {
        "name": "趋势波段优选",
        "enabled": True,
        "min_peak_pct": 4.0,
        "min_retrace_pct": 2.6,
        "min_current_pct": -0.8,
        "min_peak_edge_pct": 1.5,
        "trim_ratio": 0.20,
        "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.8,
        "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": 0.20,
        "rebuy_min_current_pct": -0.8,
    },
    "sector_rotation": {
        "name": "板块轮动先锋",
        "enabled": True,
        "min_peak_pct": 4.5,
        "min_retrace_pct": 2.8,
        "min_current_pct": -1.0,
        "min_peak_edge_pct": 1.0,
        "trim_ratio": 0.25,
        "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.5,
        "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": 0.50,
        "rebuy_min_current_pct": -0.5,
    },
    NEW_STRATEGY_ID: {
        "name": "财报突破质量",
        "enabled": True,
        "min_peak_pct": 3.5,
        "min_retrace_pct": 2.4,
        "min_current_pct": -0.8,
        "min_peak_edge_pct": 1.5,
        "trim_ratio": 0.25,
        "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.6,
        "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2,
        "rebuy_min_main_pct": 0.20,
        "rebuy_min_current_pct": -0.8,
    },
    MAIN_FORCE_STRATEGY_ID: {"name": "超强主力股", "enabled": True,
        "min_peak_pct": 4.0, "min_retrace_pct": 2.5, "min_current_pct": -1.0,
        "min_peak_edge_pct": 1.5, "trim_ratio": 0.25, "allow_loss_trim": False,
        "rebuy_rebound_pct": 1.8, "rebuy_max_sold_ratio": 0.985,
        "rebuy_min_observations": 2, "rebuy_min_main_pct": 0.50,
        "rebuy_min_current_pct": -0.5},
}
# Concentration is a quality gate, not a crude fixed position-count limit.
# A small holding is retained when its quality is high; only a weak, low-impact
# holding can be rotated out automatically.  This avoids replacing one bad
# rule (27 tiny lots) with another (blindly capping positions).
# Active replacement needs a strategy-specific observation window. A single
# two-day constant made the fast sector-rotation account behave like the trend
# account: a sellable weak holding could occupy the last slot while a clearly
# stronger live candidate waited. T+1 remains the first gate, so a zero-day
# rotation window never makes same-day bought shares sellable.
POSITION_REVIEW_MIN_HOLD_DAYS = 2  # conservative fallback for unknown accounts
POSITION_REVIEW_MIN_HOLD_DAYS_BY_STRATEGY = {
    "tq_breakout": 1,
    "trend_pullback": 2,
    "sector_rotation": 0,
    NEW_STRATEGY_ID: 1,
    MAIN_FORCE_STRATEGY_ID: 1,
}
POSITION_REVIEW_SMALL_PCT = 0.0125
POSITION_REVIEW_EXIT_SCORE = 38.0
POSITION_REVIEW_REPLACE_SCORE = 52.0
POSITION_REVIEW_ANY_REPLACE_SCORE = 42.0
POSITION_REVIEW_REPLACEMENT_EDGE = 18.0
POSITION_FULL_CAP_REPLACEMENT_EDGE = 10.0
POSITION_FULL_CAP_MAX_SCORE = 68.0
# 换仓的分数差必须先覆盖卖出费用、买入滑点与候选信号衰减；硬止损、
# 权限退出和容量压缩不受这个主动换仓缓冲影响。
POSITION_REPLACEMENT_EXECUTION_BUFFER = 3.0
# 每策略每日主动调仓换股上限（2026-08-28 起为 2 只）；容量/权限退出不占额度
POSITION_ROTATION_MAX_PER_STRATEGY_DAY = 2
POSITION_ADD_MIN_SCORE = 60.0
POSITION_REVIEW_MAX_SELLS_PER_RUN = 3
POSITION_REVIEW_BLOCKED_RETRY_MINUTES = 15
# 短线日内做T的低额委托经常被最低佣金、卖出印花税和双向滑点吞掉。
# 这不是提高仓位上限，而是拒绝“理论有收益、成本后没有意义”的碎单。
TQ_MIN_EFFECTIVE_ENTRY_AMOUNT = 4_000.0
TQ_MIN_EXPECTED_EDGE_PCT = 0.008
# Every strategy has its own candidate queue.  A structural rejection occupies
# no shared/global blacklist: it only yields the relevant strategy's limited
# live-review slots to deeper candidates for a short period.
BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES = {
    "tq_breakout": 6,
    "trend_pullback": 12,
    "sector_rotation": 9,
    NEW_STRATEGY_ID: 15,
    MAIN_FORCE_STRATEGY_ID: 9,
}
# A candidate enters this cooldown only after *two* same-day entry-risk
# rejections.  One refusal may be a transient intraday condition, while two
# consecutive reviews are enough to stop the same name monopolising a
# strategy's three-minute review slots.  Durations are deliberately strategy
# local: an intraday T setup may recover quickly, while a quality/trend setup
# must wait for more meaningful structure change.  Existing holdings, exits,
# source errors and capacity/waitlist states are never cooled down.
RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES = {
    "tq_breakout": 30,
    "trend_pullback": 75,
    "sector_rotation": 45,
    NEW_STRATEGY_ID: 90,
    MAIN_FORCE_STRATEGY_ID: 45,
}
RISK_REJECT_COOLDOWN_MAX_MINUTES = 240
RISK_REJECT_COOLDOWN_MAX_SAMPLES = 8
PERMISSION_SCOPE_EXIT_MAX_PER_STRATEGY_DAY = 1
# Legacy positions outside the account's tradable scope are unwound in
# tranches.  A hard-stop may still override this value, but a normal scope
# migration must not silently liquidate an entire available position at once.
PERMISSION_SCOPE_EXIT_RATIO = 1.0 / 3.0

# Protective exits are not the same as tactical profit-taking.  A protective
# exit may earn one controlled recovery observation path; it must never turn
# into an immediate chase or bypass the ordinary entry gates.
RECOVERY_WATCH_STATUS = "recovery_watch"
RECOVERY_POLICIES = {
    "tq_breakout": {"min_scans": 2, "cooldown_minutes": 15, "reclaim_pct": 0.005, "max_days": 1},
    "trend_pullback": {"min_scans": 2, "cooldown_minutes": 60, "reclaim_pct": 0.008, "max_days": 3},
    "sector_rotation": {"min_scans": 3, "cooldown_minutes": 60, "reclaim_pct": 0.010, "max_days": 2},
    NEW_STRATEGY_ID: {"min_scans": 2, "cooldown_minutes": 45, "reclaim_pct": 0.008, "max_days": 2},
    MAIN_FORCE_STRATEGY_ID: {"min_scans": 2, "cooldown_minutes": 45, "reclaim_pct": 0.010, "max_days": 2},
}
PROTECTIVE_EXIT_CLASSES = {"hard_stop", "trailing_stop", "max_hold", "downside_guard"}

# Entry capacity is slot-first.  Strategy/pool amount budgets and small-order
# economics shape the requested quantity, but do not turn an otherwise valid
# candidate into a permanent risk rejection.  A whole-lot shortfall is kept in
# the retryable waitlist and rechecked after cash/slots/budget are released.
ENTRY_CAPACITY_POLICY = "count-primary-amount-soft-v1"

# New simulated entries use a data-quality driven circuit breaker.  ``auto``
# is the default: healthy source/K-line/factor gates open entries, while any
# stale or incomplete gate freezes only new entries.  Operators can still use
# ``1`` for a hard freeze or ``0`` for a controlled replay; risk exits never
# consult this switch.
ENTRY_FREEZE_ENV = "PAPER_ENTRY_FREEZE"
ENTRY_FROZEN_WAITLIST_STATUS = "entry_frozen_waitlist"
ENTRY_RETRY_SIGNAL_STATUSES = ("pending", "deferred_capacity", ENTRY_FROZEN_WAITLIST_STATUS)
MANUAL_EXECUTION_RETRY_STATUS = "manual_execution_retry"
STRATEGY_EXECUTION_RETRY_STATUS = "execution_retry"
ENTRY_RETRY_ORDER_STATUSES = (
    "pending_limit", "deferred_capacity", ENTRY_FROZEN_WAITLIST_STATUS,
    MANUAL_EXECUTION_RETRY_STATUS, STRATEGY_EXECUTION_RETRY_STATUS,
)
# Retry/waitlist states are kept for research and re-ranking, but are not all
# real capacity reservations.  In particular, a deferred candidate has no
# cash reservation and must never consume a slot merely because it is waiting
# for a later ranking pass.  Keeping it in ENTRY_RETRY_ORDER_STATUSES remains
# necessary for lifecycle reconciliation; slot accounting uses this narrower
# set only.
ENTRY_SLOT_OCCUPYING_ORDER_STATUSES = (
    "pending_limit", MANUAL_EXECUTION_RETRY_STATUS, STRATEGY_EXECUTION_RETRY_STATUS,
)
ENTRY_AUTO_SOURCE_MAX_AGE_SECONDS = 15 * 60
ENTRY_AUTO_FACTOR_MAX_AGE_SECONDS = 2 * 24 * 60 * 60
# The trading-day stamped factor date is the primary freshness gate.  This
# wider wall-clock allowance only covers a weekend/holiday between a valid
# close rebuild and the next session; it must never make an old factor date
# executable.
SELECTION_FACTOR_MAX_CACHE_AGE_SECONDS = 4 * 24 * 60 * 60
_ENTRY_FREEZE_CACHE = {"at": 0.0, "status": None}
# A container restart kills in-memory workers but leaves SQLite job rows.  Do
# one explicit boot recovery before the new scheduler starts; later init_db
# calls in the same process must never steal an active scan.
_RUNNER_BOOT_ID = f"{os.getpid()}-{time.time_ns()}"
_RUNNER_BOOT_RECOVERED = False
_PERFORMANCE_INDEXES_READY = False
# A scheduled slot keeps the lease context on the worker thread.  The context
# is deliberately absent for direct read/manual API calls, while every
# scheduler-owned order/fill path validates it before mutating the ledger.
_LEASE_CONTEXT = threading.local()


def _set_lease_context(lock_key, owner_key, fencing_token, lost_event=None):
    _LEASE_CONTEXT.value = {
        "lock_key": str(lock_key),
        "owner_key": str(owner_key),
        "fencing_token": int(fencing_token or 0),
        "lost_event": lost_event or threading.Event(),
    }
    return _LEASE_CONTEXT.value


def _clear_lease_context():
    try:
        del _LEASE_CONTEXT.value
    except AttributeError:
        pass


def _active_lease_context():
    return getattr(_LEASE_CONTEXT, "value", None)


def _lease_lost(exc):
    return isinstance(exc, RuntimeError) and str(exc).startswith("paper lease lost:")


def _assert_active_lease(conn, phase="ledger mutation"):
    """Fence scheduler side effects after a lease hand-off.

    A TTL only prevents a dead process from blocking recovery; it does not
    stop an old process that wakes up after another worker has taken over.
    Every transaction that can create an order/fill or publish NAV therefore
    performs an owner+fencing-token compare-and-check before its write.
    """
    context = _active_lease_context()
    if not context:
        return True
    lost_event = context.get("lost_event")
    if lost_event is not None and lost_event.is_set():
        raise RuntimeError(f"paper lease lost: {phase}")
    row = conn.execute(
        "SELECT owner_key,fencing_token,expires_at FROM paper_runtime_locks WHERE lock_key=?",
        (context["lock_key"],),
    ).fetchone()
    expires = str(row["expires_at"] or "")[:19] if row else ""
    try:
        expired = bool(expires) and dt.datetime.fromisoformat(expires) <= dt.datetime.now()
    except (TypeError, ValueError):
        expired = True
    if (
        not row
        or str(row["owner_key"] or "") != context["owner_key"]
        or int(row["fencing_token"] or 0) != int(context["fencing_token"])
        or expired
    ):
        if lost_event is not None:
            lost_event.set()
        raise RuntimeError(f"paper lease lost: {phase}")
    return True


def _intraday_business_key(now=None):
    """Return one idempotency key for the normalized three-minute window."""
    now = now if isinstance(now, dt.datetime) else dt.datetime.now()
    minute = (now.minute // INTRADAY_INTERVAL_MINUTES) * INTRADAY_INTERVAL_MINUTES
    bucket = now.replace(minute=minute, second=0, microsecond=0)
    return f"intraday:{bucket.strftime('%Y%m%d%H%M')}"


def paper_cache_generation():
    """Cheap cross-process cache generation for the HTTP read models.

    SQLite WAL writes do not always change the main database mtime, so include
    the WAL identity and the latest audit/order/NAV/job IDs.  This is read-only
    and intentionally never calls ``init_db``.
    """
    parts = []
    for path in (DB_PATH, f"{DB_PATH}-wal"):
        try:
            stat = os.stat(path)
            parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(f"{path}:missing")
    try:
        with _db_readonly() as conn:
            latest = []
            for table, column in (
                ("paper_audit", "id"), ("paper_orders", "id"),
                ("paper_nav", "id"), ("paper_jobs", "rowid"),
            ):
                try:
                    latest.append(str(conn.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone()[0]))
                except sqlite3.Error:
                    latest.append("0")
            parts.append("ids:" + ":".join(latest))
    except Exception:
        parts.append("ids:unavailable")
    return "|".join(parts)

# A small, recently updated subset is not a valid substitute for the A-share
# market when ranking candidates.  The three paper strategies may use
# different factor lags, but each one must still be ranked against a broad,
# contemporaneous cross section.  Otherwise a transient cache failure can
# silently turn a full-market strategy into a few-hundred-stock strategy.
CANDIDATE_FACTOR_MIN_ROWS = 4_000
CANDIDATE_FACTOR_MIN_COVERAGE = 0.85

# Holding-quality is a review score, not an entry score.  It therefore needs
# strategy-specific weights: forcing the intraday-T model to share the
# long-trend MA weighting was the reason many fresh, valid T positions looked
# artificially weak on the dashboard.
HOLDING_QUALITY_WEIGHTS = {
    "tq_breakout": {"model": 0.35, "trend": 0.05, "flow": 0.25, "momentum": 0.25, "return": 0.10},
    "trend_pullback": {"model": 0.25, "trend": 0.40, "flow": 0.15, "momentum": 0.05, "return": 0.15},
    "sector_rotation": {"model": 0.25, "trend": 0.20, "flow": 0.30, "momentum": 0.15, "return": 0.10},
    NEW_STRATEGY_ID: {"model": 0.35, "trend": 0.25, "flow": 0.20, "momentum": 0.10, "return": 0.10},
    MAIN_FORCE_STRATEGY_ID: {"model": 0.25, "trend": 0.10, "flow": 0.40, "momentum": 0.15, "return": 0.10},
}

# still requires two consecutive confirmed scans and the normal quote, T+1,
# limit-down and lot-size gates.
INTRADAY_DOWNSIDE_POLICIES = {
    "tq_breakout": {
        "warning_pct": -2.0, "partial_pct": -3.0, "full_pct": -5.0,
        "relative_pct": -2.5, "peak_retrace_pct": 3.5,
        "partial_ratio": 0.35,
        # 首次下跌预警不是硬止损：只处理当日可卖仓的四分之一，
        # 每标的一天最多一次；后续恶化仍走 partial/full 防线。
        "warning_trim_ratio": 0.25,
        # Protect a profitable position from giving back its edge even when
        # low-frequency flow looks like a possible washout.
        "giveback_partial_pct": 5.5, "giveback_full_pct": 9.0,
        "giveback_min_peak_return_pct": 4.0,
    },
    "trend_pullback": {
        "warning_pct": -2.2, "partial_pct": -3.0, "full_pct": -5.0,
        "relative_pct": -2.7, "peak_retrace_pct": 4.0,
        "partial_ratio": 0.35,
        "giveback_partial_pct": 5.0, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 4.0,
    },
    "sector_rotation": {
        # Recent ledger results show that this fast-rotation sleeve was
        # allowing weak hot-theme names to become large losses.  A genuine
        # sector leader should either recover promptly or be replaced; retain
        # two distinct scans, but tighten the loss/retrace ladder.
        "warning_pct": -2.2, "partial_pct": -3.0, "full_pct": -4.5,
        "relative_pct": -2.5, "peak_retrace_pct": 3.5,
        "partial_ratio": 0.40,
        "giveback_partial_pct": 5.0, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 4.0,
    },
    NEW_STRATEGY_ID: {
        # A quality/breakout holding tolerates less structural damage than a
        # broad trend position, while requiring a distinct confirmation scan
        # before partial/full exits.  Warning trim remains independent from
        # the legacy TQ thresholds.
        "warning_pct": -1.8, "partial_pct": -3.2, "full_pct": -5.8,
        "relative_pct": -2.4, "peak_retrace_pct": 3.2,
        "partial_ratio": 0.40, "warning_trim_ratio": 0.20,
        "giveback_partial_pct": 4.5, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 3.5,
    },
    MAIN_FORCE_STRATEGY_ID: {
        "warning_pct": -2.0, "partial_pct": -3.0, "full_pct": -5.0,
        "relative_pct": -2.5, "peak_retrace_pct": 4.0,
        "partial_ratio": 0.50, "warning_trim_ratio": 0.0,
        "giveback_partial_pct": 5.0, "giveback_full_pct": 8.0,
        "giveback_min_peak_return_pct": 4.0,
    },
}

# 风控不是一个“全策略通用止损器”。这些字段同时用于审计与执行，
# 明确每个账户面对洗盘/回吐时的容忍边界，避免仅凭同一个主力意图标签
# 让三套策略做出相同动作。
STRATEGY_RISK_BEHAVIORS = {
    "tq_breakout": {
        "entry_style": "实时动量确认，可小仓追高",
        "downside_style": "快响应：当日弱势、相对大盘走弱或收益回吐优先保护",
        "washout_loss_override_pct": -3.0,
        "overheat_action": "不使用板块过热硬否决，但追高必须双源/Q1/资金量能确认",
    },
    "trend_pullback": {
        "entry_style": "回踩结构确认，不追高",
        "downside_style": "结构优先：确认走弱时分段保护，保留洗盘确认而不死扛回吐",
        "washout_loss_override_pct": -5.0,
        "overheat_action": "不因单日加速买入，等待回踩结构",
    },
    "sector_rotation": {
        "entry_style": "板块强度与个股位置双确认，不买末端加速",
        "downside_style": "板块退潮/个股背离时收紧，热点内洗盘仍需确认",
        "washout_loss_override_pct": -4.0,
        "overheat_action": "连续加速、均线乖离或布林上轨末端禁止追入；回踩后仅小仓复核",
    },
    NEW_STRATEGY_ID: {
        "entry_style": "已披露财报质量与突破结构双确认，不追末端加速",
        "downside_style": "质量失真/突破失败快速收紧，保留二次确认后分段退出",
        "washout_loss_override_pct": -3.8,
        "overheat_action": "突破后远离均线或接近涨停时冻结追入，等待回踩再确认",
    },
    MAIN_FORCE_STRATEGY_ID: {
        "entry_style": "每日十只资金观察池，连续资金和微观成交确认后最多持有三只",
        "downside_style": "主力出货需连续窗口与真实成交共振确认，再全部退出",
        "washout_loss_override_pct": -4.0,
        "overheat_action": "涨停附近仅确认热点，不模拟普通追单",
    },
}

MAIN_BOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
CHINEXT_PREFIXES = ("300", "301", "302")
STAR_PREFIXES = ("688", "689")

# 新股没有 120 根历史日线是正常现象，不能把“上市时间短”与“历史缓存缺失”混为一谈。
# 仅对主板/创业板、K 线起点与上市期一致且覆盖率足够的标的启用这条观察路径；
# 科创/北交/ST 仍由证券权限门禁直接禁止新增仓位。
NEW_LISTING_MAX_CALENDAR_DAYS = 210
NEW_LISTING_MIN_COVERAGE = 0.75
NEW_LISTING_POLICIES = {
    "tq_breakout": {
        "min_rows": 20, "max_rows": 119, "scale": 0.25,
        "max_intraday_pct": 7.0,
        # 新股绝对成交额差异极大。流动性门槛以最近自身成交中位数为主，
        # 再用较低的绝对底线兜底，避免小流通盘永远被 3,000 万/5,000 万误杀。
        "liquidity_floor": 5_000_000, "liquidity_cap": 25_000_000,
        "liquidity_history_ratio": 0.35, "liquidity_lookback": 5,
        "base_score_threshold": 0.59,
        "min_main_pct": -1.0, "min_vol_ratio": 0.80, "max_recent_range": 30.0,
    },
    "trend_pullback": {
        # 趋势策略仍要有至少一段 MA20/MA60 可计算区间，不能因“新股”放弃结构判断。
        "min_rows": 60, "max_rows": 119, "scale": 0.20,
        "max_intraday_pct": 4.0,
        "liquidity_floor": 8_000_000, "liquidity_cap": 30_000_000,
        "liquidity_history_ratio": 0.45, "liquidity_lookback": 8,
        "base_score_threshold": 0.63,
        "min_main_pct": 0.0, "min_vol_ratio": 0.90, "max_recent_range": 22.0,
    },
    "sector_rotation": {
        "min_rows": 30, "max_rows": 119, "scale": 0.25,
        "max_intraday_pct": 5.0,
        "liquidity_floor": 8_000_000, "liquidity_cap": 30_000_000,
        "liquidity_history_ratio": 0.40, "liquidity_lookback": 5,
        "base_score_threshold": 0.61,
        "min_main_pct": 0.0, "min_vol_ratio": 1.00, "max_recent_range": 25.0,
    },
    NEW_STRATEGY_ID: {
        "min_rows": 60, "max_rows": 119, "scale": 0.20,
        "max_intraday_pct": 5.0,
        "liquidity_floor": 10_000_000, "liquidity_cap": 35_000_000,
        "liquidity_history_ratio": 0.45, "liquidity_lookback": 8,
        "base_score_threshold": 0.66,
        "min_main_pct": 0.5, "min_vol_ratio": 1.00, "max_recent_range": 22.0,
    },
    MAIN_FORCE_STRATEGY_ID: {
        "min_rows": 60, "max_rows": 119, "scale": 0.20, "max_intraday_pct": 6.0,
        "liquidity_floor": 10_000_000, "liquidity_cap": 35_000_000,
        "liquidity_history_ratio": 0.45, "liquidity_lookback": 8,
        "base_score_threshold": 0.68, "min_main_pct": 2.0,
        "min_vol_ratio": 1.00, "max_recent_range": 25.0,
    },
}

# 只有普通股票会进入日内做 T 模型。ETF 的 T+0 能力仍由通用结算层识别，
# 但本轮日内策略不会把 ETF 当成候选标的。
T0_ETF_PREFIXES = ("51", "52", "56", "58", "15", "16", "18")

STYLE_PROFILES = {
    "strong": {"name": "强势接力", "source_strategy": "one_to_two"},
    "pullback": {"name": "趋势回踩", "source_strategy": "bottom_reversal"},
    "sector": {"name": "板块轮动", "source_strategy": "sentiment_pioneer"},
    "quality": {"name": "三日策略", "source_strategy": NEW_STRATEGY_ID},
    "main_force": {"name": "超强主力股", "source_strategy": MAIN_FORCE_STRATEGY_ID},
}

RISK_PROFILES = {
    # 2026-08-24 集中化改造：总硬上限 15（动态分配），单笔风险预算
    # 提升至 ~1.2% NAV，使 risk 约束给出的单仓 ≈ ¥16-22K，与 weight 上限
    # 大致同量级。资金利用率目标从 ~35% 提升到 ~75%。
    # 单账户最坏并发止损 = 3 × 1.2% = 3.6% NAV，daily_loss/drawdown 同步
    # 放宽以避免集中仓位在普通波动日频繁触发冷却。
    "breakout": {
        "name": "接力快进快出", "max_weight": 0.32, "max_exposure": 0.95,
        "max_industry": 0.42, "single_risk": 0.012,
        "daily_loss": 0.035, "drawdown": 0.11,
        "cooldown_days": 2, "min_cost_edge": 0.006,
    },
    "trend": {
        "name": "趋势集中持有", "max_weight": 0.34, "max_exposure": 0.95,
        "max_industry": 0.45, "single_risk": 0.012,
        "daily_loss": 0.040, "drawdown": 0.13,
        "cooldown_days": 3, "min_cost_edge": 0.004,
    },
    "sector": {
        "name": "热点轮动集中", "max_weight": 0.32, "max_exposure": 0.92,
        # 热点轮动允许集中，但不能把“板块强势”误当成可无限叠加同业的理由。
        # 42% 仍保留核心主题表达，同时给后续轮动留出缓冲。
        "max_industry": 0.42, "single_risk": 0.012,
        "daily_loss": 0.040, "drawdown": 0.12,
        "cooldown_days": 2, "min_cost_edge": 0.005,
    },
    "core_quality": {
        "name": "三日策略独立风控", "max_weight": 0.32, "max_exposure": 0.90,
        "max_industry": 0.38, "single_risk": 0.012,
        "daily_loss": 0.035, "drawdown": 0.11,
        "cooldown_days": 3, "min_cost_edge": 0.005,
    },
    "main_force": {
        "name": "主力持续性独立风控", "max_weight": 0.34, "max_exposure": 0.95,
        "max_industry": 0.45, "single_risk": 0.012,
        "daily_loss": 0.040, "drawdown": 0.12,
        "cooldown_days": 2, "min_cost_edge": 0.005,
    },
}

# Defense-in-depth bounds for versioned risk overlays produced by the adaptive
# subsystem.  The execution engine clamps every value again even when the
# candidate has already passed the learning service's validation.
# 2026-08-24: bounds widened in lockstep with the concentration rework so
# adaptive overlays can express (but not exceed) the new base policy.
ADAPTIVE_RISK_BOUNDS = {
    "max_weight": (0.08, 0.36), "max_exposure": (0.35, 0.96),
    "max_industry": (0.18, 0.50), "single_risk": (0.0020, 0.0130),
    "daily_loss": (0.010, 0.050), "drawdown": (0.040, 0.150),
    "cooldown_days": (2, 5), "min_cost_edge": (0.004, 0.012),
    # Evolution may carry a versioned downside policy into the executor.
    # Direction/step validation is performed by adaptive_risk as well; these
    # bounds are a second safety net at order-evaluation time.
    "downside_warning_pct": (-4.0, -1.0),
    "downside_partial_pct": (-7.0, -2.0),
    "downside_full_pct": (-10.0, -3.0),
    "downside_relative_pct": (-6.0, -1.0),
    "downside_peak_retrace_pct": (2.0, 8.0),
    "downside_partial_ratio": (0.20, 0.50),
}

ACCOUNT_SPECS = {
    "tq_breakout": {
        "name": "短线日内做T",
        "mode": "intraday_t",
        "source_strategy": "one_to_two",
        "risk_profile": "breakout",
        "entry_model_name": "强势日内候选实时确认",
        # 盘中使用上一交易日完整收盘因子；0 会把正常隔夜数据误判为过期。
        "max_factor_lag": 1,
        "allowed_q": ("Q1", "Q2"),
        "default_style": "strong",
        "cycle_days": 5,
        "hold_min": 1,
        "hold_max": 8,
        # 高换手也不能靠几十只一手仓分散风险；只保留最强的少数标的。
        # 2026-08-24 集中化：3 席 × ~30% 权重，替代 5 席 × 15%。
        "max_positions": 3,
        "max_weight": 0.32,
        "max_exposure": 0.95,
        "hard_stop": -0.05,
        "trail_after": 0.04,
        "trail_stop": 0.05,
        "take_profit": [(0.08, 0.50)],
        "min_t_score": 0.76,
        "gap_q1": (-0.015, 0.035),
        # 盘中追高上限：现价较今日开盘的溢价上限。做T策略允许动量，
        # 但 5% 以上的日内拉升不再追。
        "max_open_runup_pct": 0.05,
        # 5% 以上不是当然不可交易：仅在盘中确认强势时允许小仓试错，
        # 但仍不模拟涨停板排队成交。
        "gap_q2": (-0.03, 0.07),
    },
    "trend_pullback": {
        "name": "趋势波段优选",
        "mode": "swing",
        "source_strategy": "bottom_reversal",
        "risk_profile": "trend",
        "entry_model_name": "趋势回踩结构确认",
        "max_factor_lag": 2,
        "allowed_q": ("Q1", "Q2", "Q3"),
        "default_style": "pullback",
        "cycle_days": 10,
        "hold_min": 3,
        "hold_max": 10,
        # 波段策略以结构质量为主；集中化后 3 席保证单票有意义的仓位。
        "max_positions": 3,
        "max_weight": 0.34,
        "max_exposure": 0.95,
        "hard_stop": -0.04,
        "trail_after": 0.05,
        "trail_stop": 0.06,
        "take_profit": [(0.07, 1 / 3), (0.12, 1 / 3)],
        "min_t_score": 0.72,
        "gap_q1": (-0.015, 0.025),
        # 回踩策略只在开盘价附近/回踩位入场，严禁追日内拉升。
        "max_open_runup_pct": 0.015,
        "gap_q2": (-0.025, 0.04),
    },
    "sector_rotation": {
        "name": "板块轮动先锋",
        "mode": "swing",
        "source_strategy": "sentiment_pioneer",
        "risk_profile": "sector",
        "entry_model_name": "热点板块相对强度",
        "max_factor_lag": 1,
        "allowed_q": ("Q1", "Q2"),
        "default_style": "sector",
        "cycle_days": 5,
        "hold_min": 2,
        "hold_max": 7,
        # 板块轮动保留跨板块比较空间，但不再铺成大量试探仓。
        "max_positions": 3,
        "max_weight": 0.32,
        "max_exposure": 0.92,
        "hard_stop": -0.045,
        "trail_after": 0.045,
        "trail_stop": 0.055,
        "take_profit": [(0.06, 1 / 3), (0.10, 1 / 3)],
        "min_t_score": 0.74,
        "gap_q1": (-0.015, 0.03),
        # Hot-lane candidates are discovered from live sector/concept flow;
        # allow a little more room to enter before the move is considered
        # exhausted, while the separate position scale keeps risk bounded.
        "max_open_runup_pct": 0.04,
        "gap_q2": (-0.025, 0.06),
    },
    NEW_STRATEGY_ID: {
        "name": "三日策略",
        "mode": "swing",
        "source_strategy": NEW_STRATEGY_ID,
        "risk_profile": "core_quality",
        "strategy_version": NEW_STRATEGY_VERSION,
        "entry_model_name": "已披露财报质量与突破确认",
        "max_factor_lag": 2,
        "allowed_q": ("Q1", "Q2"),
        "default_style": "quality",
        "cycle_days": 12,
        "hold_min": 2,
        "hold_max": 12,
        "max_positions": 3,
        "max_weight": 0.32,
        "max_exposure": 0.90,
        "hard_stop": -0.055,
        "trail_after": 0.045,
        "trail_stop": 0.060,
        "take_profit": [(0.085, 0.40), (0.15, 0.35)],
        "min_t_score": 0.74,
        "gap_q1": (-0.02, 0.03),
        "max_open_runup_pct": 0.02,
        "gap_q2": (-0.03, 0.055),
        "entry_pct_high": 6.5,
    },
    MAIN_FORCE_STRATEGY_ID: {
        "name": "超强主力股", "mode": "swing",
        "source_strategy": MAIN_FORCE_STRATEGY_ID, "risk_profile": "main_force",
        "strategy_version": MAIN_FORCE_STRATEGY_VERSION,
        "entry_model_name": "主力持续性与微观成交确认",
        "max_factor_lag": 1, "allowed_q": ("Q1", "Q2"),
        "default_style": "main_force", "cycle_days": 8,
        "hold_min": 1, "hold_max": 8, "max_positions": 3,
        "max_weight": 0.34, "max_exposure": 0.95,
        "hard_stop": -0.05, "trail_after": 0.05, "trail_stop": 0.06,
        "take_profit": [(0.10, 1 / 3), (0.16, 1 / 3)],
        "min_t_score": 0.76, "gap_q1": (-0.015, 0.04),
        "max_open_runup_pct": 0.035, "gap_q2": (-0.025, 0.07),
        "entry_pct_high": 8.8, "daily_candidate_limit": 10,
        "ignition_zone": (3.5, 7.5), "first_tranche_cap_pct": 0.12,
    },
}


def strategy_center():
    """模拟盘策略说明的单一事实来源；纯只读，不影响账户或交易。"""
    narratives = {
        "tq_breakout": {
            "candidate": "强势日内候选：昨日首板只作加分，同时筛选当日量价、资金和动量领先的股票。",
            "entry": "优选涨幅 +0.2% 至 +3.5%；接近涨停时必须通过双源实时行情、Q1 后市评分、主力净流入≥2%及量比≥1.5确认，仅按35%仓位追买；未满足条件只观察。",
            "exit": "-5%硬止损（非崩盘形态首触先减35%，跌破确认后全清）；盈利+4%后5%移动止盈；+8%先止盈50%。",
        },
        "trend_pullback": {
            "candidate": "趋势回踩：MA20/MA60结构、回踩位置、中期动量与资金稳定性共同确认。",
            "entry": "优先选择MA20附近回踩；允许MA20与MA60处于温和过渡区，但不接受明显破位、急跌或远离均线的标的。",
            "exit": "-4%硬止损；+5%后6%移动止盈；+7%、+12%分两档各卖约三分之一。",
        },
        "sector_rotation": {
            "candidate": "热点板块：板块排名、板块涨幅、个股相对强度、资金共振与成交活跃度共同筛选。",
            "entry": "动态入场门槛（约0.54–0.72，由市场灯、上涨家数宽度、热点扩散与板块强度共同决定）。普通路径：板块前8且涨幅≥0.8%、板块广度≥5只成分且60%上涨、板块资金净流入，个股主力净流入≥0.5%且量比≥1.1；实时热点车道：板块前10且涨幅≥1.2%，个股主力≥0.3%、量比≥0.9；个股强势例外：+3.5%~+8.5%、主力≥2.5%、量比≥1.5，板块热度仅降权不否决。过热股禁入，hot+回踩确认后仅0.20档、caution 0.25档、热点车道/个股强势0.5档试仓。绿灯/黄灯按市场系数缩放，红灯停新开仓。",
            "exit": "-4.5%硬止损（首触非崩盘先减40%，跌破确认或崩盘全清）；+4.5%后5.5%移动止盈；+6%、+10%阶梯止盈各约三分之一，跳空越档时单轮连续消费；持有超过7个交易日退出；质量评分≤38或满席择强时全仓轮出（质量轮出不适用止盈分档）。",
        },
        NEW_STRATEGY_ID: {
            "candidate": "三日策略：三连阳/BOLL突破与已披露利润证据、主要均线、量价和超大单资金同步确认。",
            "entry": "仅在当期财报披露证据、突破结构、实时双源行情、Q1/Q2、板块权限、T+1 与共享资金槽位全部通过时开仓；接近涨停或末端加速只观察。",
            "exit": "-5.5%硬止损；峰值回撤6%触发独立移动风控；+8.5%、+15%分段止盈；持有超过12个交易日退出。",
        },
        MAIN_FORCE_STRATEGY_ID: {
            "candidate": "每日10只观察池（早盘冻结，盘中最多替换2只）：板块扩散、主力占比、超大单强度及成交活跃度复合排名。",
            "entry": "常规承接（≤+3.5%）：资金持续、量价承接和微观结构确认的前三只；点火分支（+3.5%~+7.5%）：需通过分钟量能结构、VWAP承接、低点抬高、主力资金增量与持续率、微观盘口八项确认，30秒快速通道2次严格确认；接近涨停不模拟排队。",
            "exit": "连续两个独立窗口确认疑似出货后退出；同时保留-5%硬止损、移动保护、T+1及跌停门禁。",
        },
    }
    total_profile_exposure = sum(
        _num(RISK_PROFILES[item["risk_profile"]].get("max_exposure"), 0.0)
        for item in ACCOUNT_SPECS.values()
    ) or 1.0
    rows = []
    for account_id, spec in ACCOUNT_SPECS.items():
        risk = RISK_PROFILES[spec["risk_profile"]]
        event_policy = OPENING_EVENT_POLICIES.get(account_id) or {}
        pool_budget_pct = SHARED_POOL_MAX_EXPOSURE * risk["max_exposure"] / total_profile_exposure * 100
        rows.append({
            "id": account_id,
            "name": spec["name"],
            "mode": "日内做T" if spec["mode"] == "intraday_t" else "波段交易",
            "entry_model": spec["entry_model_name"],
            "hold_range": f"{spec['hold_min']}–{spec['hold_max']} 个交易日",
            "position_limit": (
                "固定 3 个策略席位"
                if account_id == MAIN_FORCE_STRATEGY_ID
                else "动态 3–6 个策略席位"
            ),
            "max_weight_pct": round(spec["max_weight"] * 100, 1),
            "max_exposure_pct": round(spec["max_exposure"] * 100, 1),
            "pool_budget_pct": round(pool_budget_pct, 2),
            "pool_floor_pct": round(pool_budget_pct * STRATEGY_POOL_FLOOR_RATIO, 2),
            "industry_limit_pct": round(risk["max_industry"] * 100, 1),
            "daily_loss_pct": round(risk["daily_loss"] * 100, 1),
            "drawdown_pct": round(risk["drawdown"] * 100, 1),
            "cooldown_days": risk["cooldown_days"],
            "opening_event_policy": {
                "enabled": bool(event_policy.get("enabled")),
                "min_peak_pct": event_policy.get("min_peak_pct"),
                "min_retrace_pct": event_policy.get("min_retrace_pct"),
                "trim_ratio_pct": round(_num(event_policy.get("trim_ratio")) * 100, 1),
                "rebuy_rebound_pct": event_policy.get("rebuy_rebound_pct"),
                "note": "共享开盘事件引擎；减仓与回补阈值按本策略独立执行",
            },
            "risk_behavior": STRATEGY_RISK_BEHAVIORS.get(account_id, {}),
            **narratives[account_id],
        })
    return {
        "strategies": rows,
        "shared_guards": [
            "仅使用独立模拟资金与规则化成交假设，不连接券商或真实账户。",
            "可买范围仅限沪深主板与创业板；ST/退市风险、科创板和北交所一律禁止新买，科创板行情只作同产业映射加分。",
            f"四套策略共用一个总资金池，持仓与待成交买单合计不得超过总净值的 {SHARED_POOL_MAX_EXPOSURE * 100:.0f}%；这是硬上限，任何特级机会都不能突破。",
            "策略额度按风险画像分配目标和保护底线；某策略未用额度只会在其他策略底线满足后转入，不再各自重复计算总池上限。",
            f"股票持仓按策略席位计数：四策略共享总硬上限 {SHARED_POOL_MAX_POSITIONS} 个；满席时高分候选进入替补池，只有明显优于弱持仓才允许先卖后买。",
            "换仓必须通过实时双源行情、新闻、T+1与质量分复核；每策略每日最多一次主动择强换股，硬止损不受此限制。",
            "待成交买单会预占现金和总池额度；成交后核销，撤单、过期或风控拒绝后释放。",
            "自动开仓须使用当日实时行情，并通过东方财富与腾讯行情交叉核验。",
            "09:30/09:31/13:00 共享开盘事件引擎可识别冲高回落；各策略都能用，但各自阈值、减仓比例和回补确认线不同。",
            "开盘事件卖出只使用已结算底仓；回补必须看到卖出后的低点和反弹确认，不把快速下跌当成低吸。",
            "市场红灯暂停新开仓；黄灯按各策略对应仓位缩放执行。",
            "普通A股遵守T+1：当天买入的股份当天不可卖出。",
        ],
    }


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _market_session(now=None):
    """返回当前 A 股交易时段，并决定是否允许展示“今日盈亏”。

    盘前和集合竞价阶段没有完整的可比成交时段，不能把开盘前的
    快照价或旧行情误算成今日收益。收盘后则保留最后一个完整收盘快照。
    时间使用服务器本地时区（生产环境为 Asia/Shanghai）。
    """
    now = now if isinstance(now, dt.datetime) else dt.datetime.now()
    if now.weekday() >= 5:
        return {"code": "non_trading_day", "label": "非交易日，暂无当日收益", "today_pnl_available": False}
    current = now.time()
    if current < dt.time(9, 15):
        return {"code": "pre_open", "label": "盘前未开盘", "today_pnl_available": False}
    if current < dt.time(9, 30):
        return {"code": "auction", "label": "集合竞价中", "today_pnl_available": False}
    if current <= dt.time(11, 30):
        return {"code": "morning", "label": "交易中", "today_pnl_available": True}
    if current < dt.time(13, 0):
        return {"code": "midday_break", "label": "午间休市，沿用最新收益", "today_pnl_available": True}
    if current <= dt.time(15, 0):
        return {"code": "afternoon", "label": "交易中", "today_pnl_available": True}
    return {"code": "closed", "label": "已收盘", "today_pnl_available": True}


def _date(value=None):
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError):
        return default if default is not None else {}


def _num(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) and pd.notna(value) else default


def _entry_freeze_enabled():
    return bool(_entry_freeze_status().get("enabled", True))


def _entry_freeze_status(force=False):
    """Return the live entry circuit-breaker state.

    In automatic mode this is intentionally read-only: it consumes the
    persisted health/coverage artifacts produced by the three-minute scanner
    and history recovery job.  It never performs a network request or changes
    a risk/sell decision.  A missing artifact is treated as unsafe and keeps
    new buys in the waitlist.
    """
    now = time.time()
    cached = _ENTRY_FREEZE_CACHE.get("status")
    if not force and cached and now - float(_ENTRY_FREEZE_CACHE.get("at") or 0) < 15:
        return dict(cached)
    raw = str(os.getenv(ENTRY_FREEZE_ENV, "auto") or "auto").strip().lower()
    false_values = {"0", "false", "no", "off", "disabled", "n"}
    true_values = {"1", "true", "yes", "on", "enabled", "y"}
    if raw in true_values:
        status = {"enabled": True, "mode": "manual_freeze", "reason": "人工强制冻结", "checks": {}}
    elif raw in false_values:
        status = {"enabled": False, "mode": "manual_open", "reason": "人工强制解冻", "checks": {}}
    else:
        checks = {}
        reasons = []
        # Source health is written by the scanner's reconnect/probe step.
        source = {}
        try:
            source = dfc.load_source_health() or {}
        except Exception as exc:
            reasons.append(f"行情源健康状态不可读：{type(exc).__name__}")
        checked_at = source.get("checked_at") if isinstance(source, dict) else None
        source_age = None
        if checked_at:
            try:
                parsed = dt.datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                source_age = max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed.astimezone(dt.timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                source_age = None
        source_ok = bool(source.get("healthy")) and source_age is not None and source_age <= ENTRY_AUTO_SOURCE_MAX_AGE_SECONDS
        checks["source_health"] = {"healthy": bool(source.get("healthy")), "age_seconds": source_age,
                                    "max_age_seconds": ENTRY_AUTO_SOURCE_MAX_AGE_SECONDS, "passed": source_ok}
        if not source_ok:
            reasons.append("行情源未通过最近15分钟健康检查")
        coverage = {}
        try:
            coverage = U.coverage_report() or {}
        except Exception as exc:
            reasons.append(f"K线覆盖状态不可读：{type(exc).__name__}")
        # ``fresh_selection_pct`` describes the derived factor snapshot and
        # can stay low while K-lines are already complete during an
        # incremental rebuild.  It must not freeze entries when the raw daily
        # history itself is fresh and broad enough; the factor cache has its
        # own fallback gate below.
        fresh_kline_pct = float(
            coverage.get("fresh_coverage_pct")
            or coverage.get("fresh_pct")
            or coverage.get("fresh_kline_pct")
            or 0.0
        )
        coverage_ok = fresh_kline_pct >= CANDIDATE_FACTOR_MIN_COVERAGE * 100
        checks["kline_coverage"] = {"ready": bool(coverage.get("ready")),
                                     "fresh_selection_pct": coverage.get("fresh_selection_pct"),
                                     "fresh_coverage_pct": fresh_kline_pct,
                                     "passed": coverage_ok}
        if not coverage_ok:
            reasons.append("完整日线覆盖或新鲜度未达到90%")
        factor_ok = False
        factor_meta = {}
        try:
            if os.path.exists(SELECTION_META_PATH):
                with open(SELECTION_META_PATH, encoding="utf-8") as handle:
                    factor_meta = json.load(handle) or {}
            built_at = factor_meta.get("built_at")
            built_age = None
            if built_at:
                parsed = dt.datetime.fromisoformat(str(built_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                built_age = max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed.astimezone(dt.timezone.utc)).total_seconds())
            expected_factor_date = U.latest_complete_trade_date().isoformat()
            cached_factor_date = str(factor_meta.get("factor_date") or "")[:10]
            cached_coverage = float(factor_meta.get("eligible_factor_coverage_pct") or 0.0)
            factor_lag = _trading_weekday_lag(cached_factor_date, expected_factor_date) if cached_factor_date else None
            # 允许上一完整交易日的完整快照作为降级排序输入。盘中实时
            # 行情/双源门禁仍独立校验；缺口超过 1 个交易日继续冻结新买。
            factor_date_ok = cached_factor_date == expected_factor_date or (
                factor_lag == 1 and cached_coverage >= CANDIDATE_FACTOR_MIN_COVERAGE * 100
            )
            current_signature = _selection_factor_manifest_signature()
            signature_ok = bool(current_signature and factor_meta.get("signature") == current_signature)
            degraded_signature_ok = bool(
                factor_lag == 1
                and int(factor_meta.get("factor_rows") or 0) >= CANDIDATE_FACTOR_MIN_ROWS
                and cached_coverage >= CANDIDATE_FACTOR_MIN_COVERAGE * 100
            )
            factor_ok = (int(factor_meta.get("factor_rows") or 0) >= CANDIDATE_FACTOR_MIN_ROWS
                         and built_age is not None and built_age <= SELECTION_FACTOR_MAX_CACHE_AGE_SECONDS
                         and cached_coverage >= CANDIDATE_FACTOR_MIN_COVERAGE * 100
                         and factor_date_ok and signature_ok and os.path.exists(SELECTION_FACTORS_PATH))
            if not factor_ok and degraded_signature_ok and built_age is not None and built_age <= 4 * 86400 and os.path.exists(SELECTION_FACTORS_PATH):
                factor_ok = True
            checks["factor_cache"] = {"rows": int(factor_meta.get("factor_rows") or 0),
                                       "age_seconds": built_age, "max_age_seconds": ENTRY_AUTO_FACTOR_MAX_AGE_SECONDS,
                                       "factor_date": cached_factor_date, "expected_factor_date": expected_factor_date,
                                       "factor_lag": factor_lag, "degraded_fallback": bool(degraded_signature_ok and not signature_ok),
                                       "eligible_factor_coverage_pct": cached_coverage,
                                       "factor_date_ok": factor_date_ok, "manifest_signature_ok": signature_ok,
                                       "passed": factor_ok}
        except (OSError, TypeError, ValueError, RuntimeError):
            checks["factor_cache"] = {"rows": 0, "age_seconds": None, "passed": False}
        if not factor_ok:
            reasons.append("因子缓存不存在、行数不足或已过期")
        status = {"enabled": not (source_ok and coverage_ok and factor_ok), "mode": "auto",
                  "reason": "；".join(reasons) if reasons else "行情源、日线覆盖和因子缓存均达标，自动解冻",
                  "checks": checks}
    status["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _ENTRY_FREEZE_CACHE.update({"at": now, "status": dict(status)})
    return status


def _entry_frozen_reason(source=None):
    suffix = f"（{source}）" if source else ""
    state = _entry_freeze_status()
    if state.get("mode") == "auto":
        return f"新增模拟买入已自动冻结{suffix}；{state.get('reason') or '数据质量门禁未通过'}，保留待买名单，数据恢复后自动重试"
    return (
        f"新增模拟买入已冻结{suffix}；{state.get('reason') or '人工强制冻结'}，保留待买名单，解冻后按原策略、行情和风险门禁重试"
    )


def _record_entry_frozen_waitlist(
    conn, account_id, code, *, name=None, qty=0, planned_price=None,
    risk_payload=None, signal_id=None, asof_day=None, source="entry",
):
    """Persist one auditable, idempotent frozen-entry waitlist record.

    This helper never reserves cash, writes a fill, or creates a position.  A
    signal-linked row is de-duplicated by ``signal_id``; direct strategy
    observations are de-duplicated by account/code/day/source so a three-minute
    scan cannot flood the order ledger while the freeze remains enabled.
    """
    code = str(code or "")
    day = _date(asof_day).isoformat() if asof_day is not None else dt.date.today().isoformat()
    existing = None
    if signal_id is not None:
        existing = conn.execute(
            """SELECT id FROM paper_orders
               WHERE signal_id=? AND side='buy' AND status=?
               ORDER BY id DESC LIMIT 1""",
            (int(signal_id), ENTRY_FROZEN_WAITLIST_STATUS),
        ).fetchone()
    else:
        existing = conn.execute(
            """SELECT id FROM paper_orders
               WHERE account_id=? AND side='buy' AND code=? AND status=?
                 AND substr(created_at,1,10)=? AND reason LIKE ?
               ORDER BY id DESC LIMIT 1""",
            (account_id, code, ENTRY_FROZEN_WAITLIST_STATUS, day, "新增模拟买入已冻结%"),
        ).fetchone()
    reason = _entry_frozen_reason(source)
    payload = dict(risk_payload or {})
    payload["entry_freeze"] = {
        "enabled": True,
        "env": ENTRY_FREEZE_ENV,
        "status": ENTRY_FROZEN_WAITLIST_STATUS,
        "source": source,
        "asof_date": day,
        "reason": reason,
    }
    if signal_id is not None:
        payload["signal_id"] = int(signal_id)
    if existing:
        return int(existing["id"]), False, reason, payload
    cursor = conn.execute(
        """INSERT INTO paper_orders(
               account_id,signal_id,side,code,name,qty,planned_price,status,reason,
               risk_payload,origin,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            account_id, signal_id, "buy", code, name, max(0, int(_num(qty))),
            _num(planned_price, None), ENTRY_FROZEN_WAITLIST_STATUS, reason,
            _json(payload), "strategy", _now(),
        ),
    )
    order_id = int(cursor.lastrowid)
    _risk_log(conn, account_id, code, "buy", ENTRY_FROZEN_WAITLIST_STATUS, reason, payload)
    _audit(conn, account_id, ENTRY_FROZEN_WAITLIST_STATUS, f"{source}: {code} order={order_id}")
    return order_id, True, reason, payload


# Decision records are deliberately kept inside the existing JSON payloads so
# older SQLite schemas and API consumers remain compatible.  Every candidate,
# risk review and order can therefore be replayed with one stable envelope,
# while fields that were not available at decision time stay explicit null/
# ``unknown`` values instead of being back-filled from a later snapshot.
DECISION_SNAPSHOT_VERSION = "decision-snapshot-v1"


def _snapshot_safe(value):
    """Return JSON-safe values without turning missing evidence into strings."""
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _snapshot_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_snapshot_safe(item) for item in value]
    if isinstance(value, float):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    # numpy scalar values occur in factor/K-line frames.  ``item`` preserves
    # ordinary numbers while avoiding an import solely for numpy.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _snapshot_safe(item())
        except (TypeError, ValueError):
            pass
    return value


def _snapshot_date(value):
    """Normalise a replay date, returning None for invalid/future-free input."""
    if value is None or value == "":
        return None
    try:
        return _date(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _snapshot_first(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _snapshot_kline(kline, asof_date=None):
    """Serialise every completed daily bar and explicitly count future bars."""
    if kline is None or not hasattr(kline, "iterrows"):
        return {
            "source": "unknown", "rows": [], "count": 0,
            "first_date": None, "last_date": None, "future_rows": 0,
            "status": "unknown",
        }
    cutoff = _snapshot_date(asof_date)
    rows, future_rows = [], 0
    raw_columns = getattr(kline, "columns", None)
    columns = list(raw_columns) if raw_columns is not None else []
    for index, row in kline.iterrows():
        try:
            bar_date = _date(index).isoformat()
        except (TypeError, ValueError, OverflowError):
            bar_date = str(index)[:10] or None
        if cutoff and bar_date and bar_date > cutoff:
            future_rows += 1
            continue
        item = {"date": bar_date}
        for column in columns:
            try:
                item[str(column)] = _snapshot_safe(row[column])
            except (KeyError, TypeError, IndexError):
                item[str(column)] = None
        rows.append(item)
    dates = [item.get("date") for item in rows if item.get("date")]
    # Audit payloads are written on every risk decision (every 3 minutes, per
    # candidate, inside BEGIN IMMEDIATE transactions).  Serialising the full
    # multi-year bar history made each payload row tens of KB and was a major
    # driver of paper_risk_decisions growth and memory pressure.  Keep a
    # bounded recent window as evidence; the summary fields below still
    # describe the complete series.
    max_stored_bars = 120
    omitted_rows = max(0, len(rows) - max_stored_bars)
    if omitted_rows:
        rows = rows[-max_stored_bars:]
    source = _snapshot_safe(getattr(kline, "attrs", {}).get("source")) or "unknown"
    return {
        "source": source,
        "rows": rows,
        "count": len(rows) + omitted_rows,
        "rows_stored": len(rows),
        "omitted_rows": omitted_rows,
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "future_rows": future_rows,
        "status": (
            "ok" if rows and not future_rows
            else ("future_excluded" if future_rows else "unknown")
        ) + ("_truncated" if omitted_rows else ""),
    }


def _snapshot_factor_evidence(payload):
    """Extract raw factors and contribution evidence already produced upstream."""
    payload = payload if isinstance(payload, dict) else {}
    pick = payload.get("pick") if isinstance(payload.get("pick"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    entry = decision.get("entry_model") if isinstance(decision.get("entry_model"), dict) else {}
    raw = dict(pick.get("factor_snapshot") or {})
    # Preserve scalar factors that pre-date factor_snapshot and any financial
    # fields passed through by the selection model.
    for key in (
        "mom5", "mom20", "mom60", "pe", "pb", "roe", "profit_yoy",
        "net_profit", "annual_net_profit", "report_date", "annual_report_date",
        "disclosure_at", "financial_source",
    ):
        if key in pick and key not in raw:
            raw[key] = pick.get(key)
    components = dict(pick.get("score_components") or {})
    weights = components.get("weights") if isinstance(components.get("weights"), dict) else {}
    contributions = {}
    for key, weight in weights.items():
        raw_value = raw.get(key)
        try:
            contributions[key] = round(float(raw_value) * float(weight), 8) if raw_value is not None else None
        except (TypeError, ValueError):
            contributions[key] = None
    checks = entry.get("checks") if isinstance(entry.get("checks"), list) else []
    entry_contributions = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        try:
            contribution = round(float(check.get("score")) * float(check.get("weight")), 8)
        except (TypeError, ValueError):
            contribution = None
        entry_contributions.append({
            "name": check.get("name"), "raw_score": check.get("score"),
            "weight": check.get("weight"), "contribution": contribution,
            "detail": check.get("detail"),
        })
    return {
        "raw": _snapshot_safe(raw),
        "contributions": _snapshot_safe(contributions),
        "score_components": _snapshot_safe(components),
        "entry_checks": entry_contributions,
    }


def _decision_snapshot(
    payload=None, *, account_id=None, code=None, side=None, decision=None,
    reason=None, asof_date=None, quote=None, kline=None, news=None,
    final_score=None, decision_at=None,
):
    """Build one point-in-time evidence envelope without changing trade rules."""
    payload = payload if isinstance(payload, dict) else {}
    pick = payload.get("pick") if isinstance(payload.get("pick"), dict) else {}
    model = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    if not model and isinstance(payload.get("model"), dict):
        model = payload.get("model")
    if not model and isinstance(payload.get("signal"), dict):
        model = payload.get("signal")
    entry = model.get("entry_model") if isinstance(model.get("entry_model"), dict) else {}
    if not entry and isinstance(payload.get("entry_model"), dict):
        entry = payload.get("entry_model")
    quote_data = dict(quote or payload.get("quote") or {})
    resolved_code = str(code or pick.get("code") or payload.get("code") or "") or None
    resolved_asof = _snapshot_date(
        asof_date or payload.get("asof") or payload.get("asof_date")
        or payload.get("execution_day") or payload.get("signal_date")
        or payload.get("fill_date")
    )
    if kline is None and resolved_code and resolved_asof:
        try:
            # This is local-cache only; no network fetch is allowed while
            # writing an immutable decision record.
            kline = _completed_kline(resolved_code, resolved_asof, inclusive=True)
        except Exception:
            kline = None
    quote_at = _snapshot_first(quote_data, "quote_at", "time", "timestamp")
    quote_source = _snapshot_first(quote_data, "quote_source", "source") or "unknown"
    history = payload.get("history") or payload.get("history_meta") or payload.get("factor", {})
    history = history if isinstance(history, dict) else {}
    factor_evidence = _snapshot_factor_evidence(payload)
    financial_source = _snapshot_first(
        pick, "financial_source", "finance_source", "fundamental_source"
    ) or _snapshot_first(history, "financial_source", "finance_source") or "unknown"
    report_period = _snapshot_first(
        pick, "report_date", "report_period", "financial_period"
    ) or _snapshot_first(factor_evidence.get("raw"), "report_date", "report_period")
    disclosure_at = _snapshot_first(
        pick, "disclosure_at", "disclosure_time", "announce_at", "announcement_at"
    ) or _snapshot_first(history, "disclosure_at", "disclosure_time", "announce_at")
    annual_report_date = _snapshot_first(pick, "annual_report_date") or _snapshot_first(
        factor_evidence.get("raw"), "annual_report_date"
    )
    news_rows = news if news is not None else payload.get("news")
    news_rows = news_rows if isinstance(news_rows, list) else []
    news_events = []
    announcement_times = []
    for item in news_rows:
        if not isinstance(item, dict):
            continue
        event_time = _snapshot_first(item, "time", "quote_at", "published_at", "announcement_at")
        event = _snapshot_safe(dict(item))
        event["event_at"] = event_time
        news_events.append(event)
        if item.get("verified") or item.get("source_type") == "announcement_aggregator":
            if event_time:
                announcement_times.append(event_time)
    threshold_context = entry.get("threshold_context") if isinstance(entry.get("threshold_context"), dict) else {}
    factor_meta = payload.get("factor") if isinstance(payload.get("factor"), dict) else {}
    selection_meta = factor_meta.get("selection_evolution") if isinstance(factor_meta.get("selection_evolution"), dict) else {}
    news_learning = entry.get("news_learning") if isinstance(entry.get("news_learning"), dict) else {}
    components = factor_evidence.get("score_components") or {}
    threshold_version = (
        threshold_context.get("version") or selection_meta.get("version")
        or factor_meta.get("risk_version") or components.get("version")
        or RISK_VERSION
    )
    threshold_value = _snapshot_first(entry, "threshold") or _snapshot_first(threshold_context, "threshold")
    threshold_delta = _snapshot_first(news_learning, "threshold_delta")
    if threshold_delta is None:
        threshold_delta = _snapshot_first(selection_meta, "entry_score_delta")
    if final_score is None:
        final_score = (
            _snapshot_first(entry, "score") or _snapshot_first(model, "final_score", "avg_score")
            or _snapshot_first(components, "final_score") or _snapshot_first(pick, "score")
        )
    final_reason = reason or payload.get("reason") or _snapshot_first(entry, "reason")
    if not final_reason:
        reasons = entry.get("reasons") if isinstance(entry.get("reasons"), list) else []
        blockers = entry.get("blockers") if isinstance(entry.get("blockers"), list) else []
        final_reason = "；".join(str(item) for item in (reasons or blockers) if item) or None
    kline_evidence = _snapshot_kline(kline, resolved_asof)
    history_last = _snapshot_first(history, "last_date", "factor_date")
    quote_validation = _snapshot_first(quote_data, "quote_validation") or "unknown"
    data_quality = {
        "quote": "ok" if quote_at and quote_source != "unknown" and quote_validation in {"cross_source_checked", "range_timestamp_checked"} else ("degraded" if quote_at else "unknown"),
        "kline": kline_evidence.get("status") or "unknown",
        "financial": "ok" if report_period else "unknown",
        "news": "ok" if news_rows else ("stale" if _NEWS_SCAN_META.get("stale") else "unknown"),
        "history_last_date": history_last,
        "news_scan": _snapshot_safe(dict(_NEWS_SCAN_META)),
    }
    quality_values = [
        value for key, value in data_quality.items()
        if key in {"quote", "kline", "financial", "news"} and isinstance(value, str)
    ]
    data_quality["overall"] = "degraded" if any(value in {"degraded", "stale", "future_excluded"} for value in quality_values) else (
        "ok" if all(value in {"ok", None} for value in quality_values[:4]) else "unknown"
    )
    return _snapshot_safe({
        "version": DECISION_SNAPSHOT_VERSION,
        "decision_at": decision_at or _now(),
        "asof": resolved_asof,
        "account_id": account_id,
        # Account IDs are strategy IDs in the paper ledger.  Persisting the
        # explicit field keeps audit consumers forward-compatible when a new
        # account is added without changing the decision schema.
        "strategy_id": account_id,
        "code": resolved_code,
        "side": side,
        "quote": {
            "quote_at": quote_at, "source": quote_source,
            "validation": quote_validation,
            "cross_check": quote_data.get("quote_cross_check"),
            "price": quote_data.get("price"), "pct": quote_data.get("pct"),
        },
        "kline": kline_evidence,
        "financial": {
            "report_period": report_period, "report_date": report_period,
            "annual_report_date": annual_report_date,
            "disclosure_at": disclosure_at, "source": financial_source,
        },
        "news": {"events": news_events, "announcement_times": announcement_times},
        "factors": factor_evidence,
        "threshold": {
            "version": threshold_version, "value": threshold_value,
            "delta": threshold_delta,
            "dynamic": bool(threshold_delta is not None or threshold_context or selection_meta),
        },
        "data_quality": data_quality,
        "final": {
            "score": final_score, "reason": final_reason,
            "decision": decision or payload.get("decision_name") or "unknown",
        },
    })


def _with_decision_snapshot(payload=None, **kwargs):
    enriched = dict(payload or {}) if isinstance(payload, dict) else {}
    if kwargs.get("account_id") and "strategy_id" not in enriched:
        enriched["strategy_id"] = kwargs.get("account_id")
    enriched["decision_snapshot"] = _decision_snapshot(enriched, **kwargs)
    return enriched


def _rebuild_realized_pnl(conn):
    """按成交流水 FIFO 重放卖出成本，兼容升级前未含买入费用的历史记录。"""
    lots = {}
    rows = _rows(
        conn,
        """SELECT id,account_id,code,side,qty,amount,filled_price,fees,status
           FROM paper_orders WHERE status='filled' ORDER BY id""",
    )
    for order in rows:
        key = (order["account_id"], order["code"])
        qty = int(_num(order.get("qty")))
        if qty <= 0:
            continue
        if order["side"] == "buy":
            gross = _num(order.get("amount"), _num(order.get("filled_price")) * qty)
            unit_cost = (gross + _num(order.get("fees"))) / qty
            lots.setdefault(key, []).append([qty, unit_cost])
            continue
        remaining, cost_amount = qty, 0.0
        for lot in lots.get(key, []):
            take = min(remaining, lot[0])
            if take:
                cost_amount += take * lot[1]
                lot[0] -= take
                remaining -= take
            if not remaining:
                break
        # 只有完整匹配到历史买入的成交才重写；异常旧数据保留原账面值供审计。
        if remaining == 0:
            realized = _num(order.get("amount")) - _num(order.get("fees")) - cost_amount
            conn.execute("UPDATE paper_orders SET realized_pnl=? WHERE id=?", (realized, order["id"]))


def _next_weekday(value):
    """T+1 unlocks on the next Shanghai trading day, not merely a weekday."""
    return U.next_trade_day(_date(value))


def _is_trade_weekday(value):
    return U.is_trade_day(_date(value))


def _commission(amount):
    return max(MIN_COMMISSION, amount * COMMISSION)


def _is_st_or_delisting(name=None, risk_flag=False):
    """Return whether the current security identity uses the A-share ST limit.

    The trade engine intentionally keeps legacy ST positions sellable while
    blocking any new purchase.  Their 5% price limit must therefore be known
    to the sell gate as well; a code-only board rule would treat an ST limit
    down as an executable ordinary-board quote.
    """
    label = str(name or "").upper()
    return bool(risk_flag) or "ST" in label or "退" in str(name or "")


def _limit_pct(code, name=None, risk_flag=False):
    """Return the applicable down-limit percentage for a tradable position."""
    if _is_st_or_delisting(name, risk_flag):
        return 5.0
    return F.limit_up_threshold(str(code)) * 100


def _asset_type(code, name=None):
    """A 股普通股票 T+1；常见场内 ETF 代码段按 T+0 处理。

    不把可转债、指数或未识别证券冒充 ETF；未来接入证券类型字段时可优先使用该字段。
    """
    code = str(code or "")
    label = str(name or "")
    if code.startswith(T0_ETF_PREFIXES) and ("ETF" in label.upper() or code.startswith(("51", "52", "56", "58", "15"))):
        return "etf_t0"
    return "stock_t1"


def _security_scope(code, name=None, risk_flag=False):
    """Single source of truth for securities the simulated account may buy."""
    raw = str(code or "").strip()
    normalized = raw.zfill(6) if raw.isdigit() else raw
    label = str(name or "").strip()
    upper = label.upper()
    if risk_flag or "ST" in upper or "退" in label:
        return {"allowed": False, "board": "风险警示", "reason": "ST/退市风险标的不在账户权限范围"}
    if normalized.startswith(STAR_PREFIXES):
        return {"allowed": False, "board": "科创板", "reason": "科创板不在账户权限范围"}
    if normalized.startswith(("92",)) or normalized.startswith(("4", "8")):
        return {"allowed": False, "board": "北交所", "reason": "北交所不在账户权限范围"}
    if normalized.startswith(CHINEXT_PREFIXES):
        return {"allowed": True, "board": "创业板", "reason": "创业板普通股票"}
    if normalized.startswith(MAIN_BOARD_PREFIXES):
        return {"allowed": True, "board": "沪深主板", "reason": "沪深主板普通股票"}
    return {"allowed": False, "board": "其他证券", "reason": "仅允许沪深主板和创业板普通股票"}


@contextmanager
def _db(immediate=False):
    """获取数据库连接。"""
    path = DB_PATH
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        # 异常退出：回滚本事务块内尚未提交的写入，避免脏数据残留
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    else:
        # 正常退出：提交本事务块内的全部写入。
        # 修复：此前缺少显式 commit，Python sqlite3 默认 isolation_level=''
        # 会在 conn.close() 时隐式回滚，导致 paper_signals / paper_orders /
        # paper_risk_decisions 等所有经 _db() 写入的数据全部丢失（审计记录
        # 自 2026-08-18 10:46:48 起冻结）。
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                # 锁冲突：写入仍在事务里，必须原样重试；二次失败先回滚，
                # 不能让 close() 对半提交状态做隐式处理。
                time.sleep(0.2)
                try:
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                # 非锁类提交失败：显式回滚，保证连接关闭前状态确定。
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
    finally:
        conn.close()


def _wal_checkpoint():
    """显式收缩 WAL 日志，防止 -wal/-shm 无界增长。

    仅建议在批量写完成后调用（init_db 建表/迁移、每日清理归档等），
    避免高频写路径上每次阻塞。TRUNCATE 需要无活跃读事务的独占条件，
    失败时降级为 PASSIVE 尽力收缩；仍失败则静默等待下一轮。
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
    except Exception:
        pass


def _execute_with_retry(conn, sql, params=(), max_retries=3):
    """执行 SQL 并在数据库锁定时重试。

    Args:
        conn: 数据库连接
        sql: SQL 语句
        params: 参数
        max_retries: 最大重试次数

    Returns:
        cursor
    """
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def _executemany_with_retry(conn, sql, params_list, max_retries=3):
    """执行批量 SQL 并在数据库锁定时重试。"""
    for attempt in range(max_retries):
        try:
            return conn.executemany(sql, params_list)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def _commit_with_retry(conn, max_retries=3):
    """提交事务并在数据库锁定时重试。"""
    for attempt in range(max_retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise

@contextmanager
def _db_readonly():
    """Open a ledger reader that never runs migrations or takes a write lock.

    Browser history/audit views must remain available while the intraday worker
    owns the writer.  ``init_db`` is intentionally a maintenance operation (it
    reconciles lots and performs schema upgrades), so it must not be called by
    a request that only displays archived orders.
    """
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_runtime_lease_columns(conn):
    """Add lease metadata to ledgers created by older deployments.

    The scheduler and API share one SQLite file, so this migration is kept
    deliberately small and idempotent.  Nullable columns preserve all legacy
    rows; a missing expiry is handled by the conservative started_at fallback
    in ``_recover_stale_runtime_state``.
    """
    migrations = {
        "paper_jobs": {
            "owner_key": "TEXT",
            "heartbeat_at": "TEXT",
            "expires_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_job_runs": {
            "owner_key": "TEXT",
            "heartbeat_at": "TEXT",
            "expires_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_runtime_locks": {
            "heartbeat_at": "TEXT",
            "fencing_token": "INTEGER NOT NULL DEFAULT 0",
        },
        "paper_nav": {"quote_status": "TEXT NOT NULL DEFAULT 'verified'"},
    }
    for table, definitions in migrations.items():
        try:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            continue
        for column, definition in definitions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    # Older runner builds used an ISO ``T`` separator for runtime locks while
    # the rest of the ledger used a space.  Normalize those legacy values once
    # so the expiry comparisons remain correct across the migration boundary.
    for table in ("paper_jobs", "paper_job_runs", "paper_runtime_locks"):
        try:
            table_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column in ("started_at", "acquired_at", "heartbeat_at", "expires_at"):
                if column not in table_columns:
                    continue
                conn.execute(
                    f"UPDATE {table} SET {column}=replace({column},'T',' ') "
                    f"WHERE {column} IS NOT NULL AND instr({column},'T')>0"
                )
        except sqlite3.Error:
            continue


def _recover_stale_runtime_state(conn, *, boot_recovery=False):
    """Recover abandoned scheduler claims without running expensive migrations.

    ``init_db`` is also reached by API processes.  Existing ledgers therefore
    take this narrow path on every call: only an expired lease (or a legacy
    row with no lease metadata and an old started_at) can be reclaimed.  A
    fresh process must never treat another live worker as abandoned.
    """
    now = _now()
    # API processes and short-lived scheduler workers both call ``init_db``.
    # Importing a new process is not evidence that another worker died: the
    # former boot branch interrupted every running job and deleted every
    # runtime lease, which allowed duplicate scans/orders.  Recovery is now
    # governed only by the explicit lease expiry (or the legacy timestamp
    # fallback below).  ``boot_recovery`` remains for compatibility with old
    # callers but is intentionally ignored.
    intraday_cutoff = (dt.datetime.now() - dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    job_cutoff = (dt.datetime.now() - dt.timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    detail = _json({"error": "任务进程中断或超时，系统已自动回收", "recovered_at": now})
    conn.execute(
        """UPDATE paper_job_runs
           SET status='failed',detail=?,finished_at=?
           WHERE status='running'
             AND ((expires_at IS NOT NULL AND expires_at<?)
                  OR (expires_at IS NULL AND started_at<?))""",
        (detail, now, now, intraday_cutoff),
    )
    conn.execute(
        """UPDATE paper_jobs
           SET status='failed',detail=?,finished_at=?
           WHERE status='running'
             AND ((expires_at IS NOT NULL AND expires_at<?)
                  OR (expires_at IS NULL AND started_at<?))""",
        (detail, now, now, job_cutoff),
    )
    # Never delete a live lease merely because a new process started.
    conn.execute("DELETE FROM paper_runtime_locks WHERE expires_at<?", (now,))
    # A capacity wait is a single live intent per strategy/code, not an ever
    # growing order stream.  Keep the newest active marker for re-ranking and
    # retain older rows as audit evidence without letting them occupy the
    # waitlist or distort dashboard counts.
    conn.execute(
        """UPDATE paper_orders AS older
           SET status='superseded',
               reason=COALESCE(older.reason,'') || '；同标的较新等待池记录已保留'
           WHERE older.side='buy' AND older.status='deferred_capacity'
             AND EXISTS(
                 SELECT 1 FROM paper_orders AS newer
                 WHERE newer.account_id=older.account_id
                   AND newer.code=older.code AND newer.side='buy'
                   AND newer.status IN ('deferred_capacity',?)
                   AND newer.id>older.id
             )""",
        (ENTRY_FROZEN_WAITLIST_STATUS,),
    )
    conn.execute(
        """UPDATE paper_signals
           SET status='superseded',
               reason=COALESCE(reason,'') || '；等待池旧委托已由后续记录替代'
           WHERE id IN (
               SELECT signal_id FROM paper_orders
               WHERE status='superseded' AND side='buy' AND signal_id IS NOT NULL
                 AND reason LIKE '%等待池记录已保留%'
           )
             AND status IN (?,?,?)""",
        ENTRY_RETRY_SIGNAL_STATUSES,
    )


def _reconcile_signal_order_states(conn):
    """Repair active entry orders whose linked signal lifecycle drifted.

    A signal is the research intent and an entry order is its execution
    attempt.  A crash between either write can leave a superseded signal with
    a live waitlist/deferred order, which then gets picked up again by the
    recheck worker.  Reconcile only retryable entry states during init; all
    changes are terminal/idempotent and preserve the original rows for audit.
    """
    required = {"paper_signals", "paper_orders", "paper_capital_reservations"}
    present = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            tuple(required),
        ).fetchall()
    }
    if present != required:
        # A partially-created legacy database is completed by the surrounding
        # init path; do not make the migration itself fail on that state.
        return {"status": "skipped", "reason": "reconciliation_tables_not_ready"}

    # ``execution_retry`` is a recoverable attempt, not a second intent.  A
    # crash between two retries used to leave several live retry rows for one
    # signal, each of which could reserve a slot/cash on the next pass.  Keep
    # the newest row as the canonical retry and retire older rows before the
    # partial unique index is installed.
    retry_rows = conn.execute(
        """SELECT id,signal_id FROM paper_orders
           WHERE side='buy' AND status='execution_retry' AND signal_id IS NOT NULL
           ORDER BY signal_id,id DESC"""
    ).fetchall()
    seen_retry_signals = set()
    for retry in retry_rows:
        signal_id = int(retry["signal_id"])
        if signal_id in seen_retry_signals:
            conn.execute(
                """UPDATE paper_orders
                      SET status='superseded',
                          reason=COALESCE(reason,'') || '；同一信号仅保留最新执行重试'
                    WHERE id=? AND status='execution_retry'""",
                (int(retry["id"]),),
            )
            _finish_capital_reservation(conn, int(retry["id"]), "released")
        else:
            seen_retry_signals.add(signal_id)
    try:
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_orders_signal_retry
               ON paper_orders(signal_id)
               WHERE side='buy' AND signal_id IS NOT NULL AND status='execution_retry'"""
        )
    except sqlite3.IntegrityError:
        # A legacy database may be repaired by the next init pass; never make
        # an otherwise healthy paper ledger unavailable because an index was
        # created concurrently by another process.
        pass

    order_statuses = tuple(ENTRY_RETRY_ORDER_STATUSES)
    placeholders = ",".join("?" for _ in order_statuses)
    rows = conn.execute(
        f"""SELECT o.id AS order_id,o.status AS order_status,o.reason AS order_reason,
                         s.id AS signal_id,s.status AS signal_status
                    FROM paper_orders o
                    JOIN paper_signals s ON s.id=o.signal_id
                   WHERE o.side='buy' AND o.status IN ({placeholders})
                   ORDER BY o.id""",
        order_statuses,
    ).fetchall()
    if not rows:
        return {"status": "ok", "orders_checked": 0, "orders_reconciled": 0}

    # Terminal signals must never leave an active retry order behind.  In
    # particular, superseded signals are not allowed to resurrect through the
    # waitlist after a newer signal/order has replaced them.
    terminal_signal_statuses = {
        "superseded", "filled", "rejected", "blocked", "expired", "shadow_q3",
    }
    signal_for_order = {
        "pending_limit": "pending",
        "deferred_capacity": "deferred_capacity",
        ENTRY_FROZEN_WAITLIST_STATUS: ENTRY_FROZEN_WAITLIST_STATUS,
        MANUAL_EXECUTION_RETRY_STATUS: "pending",
        STRATEGY_EXECUTION_RETRY_STATUS: "pending",
    }
    now = _now()
    reconciled = 0
    for row in rows:
        order_id = int(row["order_id"])
        order_status = str(row["order_status"] or "")
        signal_id = int(row["signal_id"])
        signal_status = str(row["signal_status"] or "")
        if signal_status in terminal_signal_statuses:
            reason = (
                "已关联终态信号，回收遗留活动委托"
                if signal_status != "superseded" else
                "信号已替代，回收遗留活动等待委托"
            )
            conn.execute(
                """UPDATE paper_orders
                      SET status='superseded',
                          reason=COALESCE(reason,'') || '；' || ?
                    WHERE id=? AND status IN ("""
                + placeholders + ")",
                (reason, order_id, *order_statuses),
            )
            conn.execute(
                """UPDATE paper_capital_reservations
                      SET status='released',released_at=COALESCE(released_at,?)
                    WHERE status='reserved' AND CAST(order_key AS TEXT)=?""",
                (now, str(order_id)),
            )
            reconciled += 1
            continue

        desired_signal = signal_for_order.get(order_status)
        if desired_signal and signal_status != desired_signal:
            conn.execute(
                """UPDATE paper_signals
                      SET status=?,
                          reason=COALESCE(reason,'') || '；按活动委托状态对账'
                    WHERE id=? AND status<>?""",
                (desired_signal, signal_id, desired_signal),
            )
            reconciled += 1

    return {
        "status": "ok",
        "orders_checked": len(rows),
        "orders_reconciled": reconciled,
    }


def _supersede_signal_execution_retries(conn, signal_id):
    """Keep one retry row for a signal and release every older reservation."""
    if signal_id is None:
        return 0
    rows = conn.execute(
        """SELECT id FROM paper_orders
           WHERE signal_id=? AND side='buy' AND status='execution_retry'
           ORDER BY id DESC""",
        (int(signal_id),),
    ).fetchall()
    if not rows:
        return 0
    # The current caller is about to create the next retry.  Retire every
    # previous row; the partial unique index protects against a concurrent
    # second insert when a scheduler process is recovered.
    for row in rows:
        order_id = int(row["id"])
        conn.execute(
            """UPDATE paper_orders
                  SET status='superseded',
                      reason=COALESCE(reason,'') || '；新一轮执行重试已接管'
                WHERE id=? AND status='execution_retry'""",
            (order_id,),
        )
        _finish_capital_reservation(conn, order_id, "released")
    return len(rows)


def _ensure_performance_indexes(conn):
    """Install idempotent read-path indexes once per process.

    These indexes only accelerate existing ledger reads; they do not alter
    selection, risk, sizing, or order semantics.  Keeping this migration
    separate from the large bootstrap script also upgrades databases created
    by older deployments without rebuilding any rows.
    """
    global _PERFORMANCE_INDEXES_READY
    if _PERFORMANCE_INDEXES_READY:
        return
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_paper_lots_account_code_remaining
            ON paper_position_lots(cycle_id, account_id, code, remaining_qty, available_date);
        CREATE INDEX IF NOT EXISTS idx_paper_orders_account_code_status
            ON paper_orders(account_id, code, status, side, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_paper_fills_order_account_side
            ON paper_fills(order_id, account_id, side, code);
        CREATE INDEX IF NOT EXISTS idx_paper_audit_event_recent
            ON paper_audit(event, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_paper_risk_decisions_latest
            ON paper_risk_decisions(account_id, code, created_at DESC, id DESC);
        """
    )
    _PERFORMANCE_INDEXES_READY = True


def _ensure_ignition_shadow_table(conn):
    """影子对比表的独立迁移。

    既有账本（paper_accounts 已存在）会走 init_db 的快路径直接返回，
    永远不会执行新建库的 executescript 建表块；新增表必须有自己的
    幂等迁移，否则线上库永远缺表（2026-08-31 部署时发现）。
    """
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_ignition_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                bucket TEXT NOT NULL,
                code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                price REAL,
                pct REAL,
                runup REAL,
                old_rule_passed INTEGER NOT NULL DEFAULT 0,
                old_rule_reason TEXT,
                ignition_passed INTEGER NOT NULL DEFAULT 0,
                ignition_reasons TEXT,
                price_30m REAL,
                at_30m TEXT,
                price_60m REAL,
                at_60m TEXT,
                resolved INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_ignition_shadow_unique
                ON paper_ignition_shadow(day, bucket, code)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_paper_ignition_shadow_recent
                ON paper_ignition_shadow(day, resolved)"""
        )
    except Exception:
        pass  # 影子表缺失只降级影子记录，绝不阻断交易主链路


def init_db():
    global _RUNNER_BOOT_RECOVERED
    """创建独立账本。不会启动任务、不会请求行情。"""
    # Quick check: if tables already exist, skip the expensive executescript
    try:
        with _db() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_accounts'"
            ).fetchall()]
            if tables:
                # Existing ledgers still need configuration migrations.  A
                # fast-path return previously skipped newly registered
                # strategy accounts forever, so code/UI could show a strategy
                # that the scheduler never ran.
                _ensure_accounts(conn)
                _ensure_cycle(conn)
                _ensure_runtime_lease_columns(conn)
                _recover_stale_runtime_state(conn)
                _reconcile_signal_order_states(conn)
                _ensure_performance_indexes(conn)
                _ensure_ignition_shadow_table(conn)
                return
    except Exception:
        pass
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source_strategy TEXT NOT NULL,
                status TEXT NOT NULL, initial_cash REAL NOT NULL, cash REAL NOT NULL,
                cycle_days INTEGER NOT NULL, max_positions INTEGER NOT NULL,
                max_weight REAL NOT NULL, max_exposure REAL NOT NULL, version TEXT NOT NULL,
                benchmark_start REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                signal_date TEXT NOT NULL, intended_date TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT, industry TEXT, close_price REAL, rank_score REAL, t_tier TEXT,
                t_score REAL, payload TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, signal_date, code)
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
                qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
                amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
                risk_payload TEXT NOT NULL, realized_pnl REAL, created_at TEXT NOT NULL,                 executed_at TEXT
            );
            -- 归档表：列集与活跃表严格一致（清理函数用 SELECT * 归档），避免列错位。
            CREATE TABLE IF NOT EXISTS paper_orders_archive (
                id INTEGER, account_id TEXT, signal_id INTEGER, side TEXT, code TEXT, name TEXT,
                qty INTEGER, planned_price REAL, filled_price REAL, amount REAL, fees REAL,
                status TEXT, reason TEXT, risk_payload TEXT, realized_pnl REAL,
                created_at TEXT, executed_at TEXT, order_type TEXT, origin TEXT,
                expires_at TEXT, cancelled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_signals_archive (
                id INTEGER, account_id TEXT, signal_date TEXT, intended_date TEXT, code TEXT, name TEXT,
                industry TEXT, close_price REAL, rank_score REAL, t_tier TEXT, t_score REAL,
                payload TEXT, status TEXT, reason TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
                qty INTEGER NOT NULL, cost REAL NOT NULL, entry_date TEXT NOT NULL,
                available_date TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'stock_t1', peak_price REAL, take_stage INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_id, code)
            );
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
                qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL, fees REAL NOT NULL,
                fill_date TEXT NOT NULL, quote_at TEXT, assumption TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                code TEXT, side TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT,
                payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_risk_decisions_recent
                ON paper_risk_decisions(id DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_risk_decisions_filter
                ON paper_risk_decisions(account_id, created_at DESC, code, decision);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_recent
                ON paper_orders(id DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_account_status
                ON paper_orders(account_id, status, created_at DESC);
            CREATE TABLE IF NOT EXISTS paper_capital_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                order_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                code TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                fees REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'reserved',
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_capital_reservations_cycle_status
                ON paper_capital_reservations(cycle_id, status);
            CREATE TABLE IF NOT EXISTS paper_ignition_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                bucket TEXT NOT NULL,
                code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                price REAL,
                pct REAL,
                runup REAL,
                old_rule_passed INTEGER NOT NULL DEFAULT 0,
                old_rule_reason TEXT,
                ignition_passed INTEGER NOT NULL DEFAULT 0,
                ignition_reasons TEXT,
                price_30m REAL,
                at_30m TEXT,
                price_60m REAL,
                at_60m TEXT,
                resolved INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_ignition_shadow_unique
                ON paper_ignition_shadow(day, bucket, code);
            CREATE INDEX IF NOT EXISTS idx_paper_ignition_shadow_recent
                ON paper_ignition_shadow(day, resolved);
            CREATE TABLE IF NOT EXISTS paper_position_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                code TEXT NOT NULL,
                review_date TEXT NOT NULL,
                score REAL NOT NULL,
                grade TEXT NOT NULL,
                action TEXT NOT NULL,
                market_value REAL NOT NULL DEFAULT 0,
                position_pct REAL NOT NULL DEFAULT 0,
                replacement_code TEXT,
                replacement_score REAL,
                reasons TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(cycle_id, account_id, code, review_date)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_position_reviews_recent
                ON paper_position_reviews(cycle_id, review_date, score);
            CREATE TABLE IF NOT EXISTS paper_nav (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                nav_date TEXT NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL,
                nav REAL NOT NULL, benchmark REAL, created_at TEXT NOT NULL,
                quote_status TEXT NOT NULL DEFAULT 'verified',
                UNIQUE(account_id, nav_date)
            );
            CREATE TABLE IF NOT EXISTS paper_jobs (
                slot TEXT NOT NULL, market_date TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT, started_at TEXT NOT NULL, finished_at TEXT,
                 owner_key TEXT, heartbeat_at TEXT, expires_at TEXT,
                 fencing_token INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(slot, market_date)
            );
            CREATE TABLE IF NOT EXISTS paper_reviews (
                week_key TEXT NOT NULL, account_id TEXT NOT NULL, report TEXT NOT NULL,
                recommendation TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(week_key, account_id)
            );
            CREATE TABLE IF NOT EXISTS paper_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event TEXT NOT NULL,
                detail TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, capital REAL NOT NULL, risk_profile TEXT NOT NULL,
                started_at TEXT, ended_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_position_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
                qty INTEGER NOT NULL, remaining_qty INTEGER NOT NULL, cost REAL NOT NULL,
                acquired_at TEXT NOT NULL, available_date TEXT NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'stock_t1', source_order_id INTEGER,
                cost_fee_included INTEGER NOT NULL DEFAULT 0,
                is_t_base INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_paper_lots_active
                ON paper_position_lots(cycle_id, account_id, code, available_date);
            CREATE TABLE IF NOT EXISTS paper_intraday_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, code TEXT, observed_at TEXT NOT NULL,
                price REAL, action TEXT NOT NULL, reason TEXT, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_intraday_obs
                ON paper_intraday_observations(cycle_id, account_id, observed_at);
            CREATE TABLE IF NOT EXISTS paper_parameter_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, version TEXT NOT NULL, style TEXT NOT NULL,
                params TEXT NOT NULL, reason TEXT NOT NULL, effective_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, cycle_key TEXT,
                reason TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_job_runs (
                run_key TEXT PRIMARY KEY, slot TEXT NOT NULL, market_date TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, started_at TEXT NOT NULL, finished_at TEXT,
                 owner_key TEXT, heartbeat_at TEXT, expires_at TEXT,
                 fencing_token INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS paper_runtime_locks (
                lock_key TEXT PRIMARY KEY,
                owner_key TEXT NOT NULL,
                slot TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT,
                expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS paper_position_limit_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                allocation_key TEXT NOT NULL,
                pool_limit INTEGER NOT NULL,
                limits TEXT NOT NULL,
                weights TEXT NOT NULL,
                inputs TEXT NOT NULL,
                source TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(cycle_id, allocation_key)

            );
            CREATE INDEX IF NOT EXISTS idx_paper_position_limit_versions_cycle
                ON paper_position_limit_versions(cycle_id, effective_at DESC);

"""
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
        if "realized_pnl" not in columns:
            conn.execute("ALTER TABLE paper_orders ADD COLUMN realized_pnl REAL")
        order_migrations = {
            "order_type": "TEXT NOT NULL DEFAULT 'market'",
            "origin": "TEXT NOT NULL DEFAULT 'strategy'",
            "expires_at": "TEXT",
            "cancelled_at": "TEXT",
        }
        for column, definition in order_migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE paper_orders ADD COLUMN {column} {definition}")
        lot_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_position_lots)").fetchall()}
        if "cost_fee_included" not in lot_columns:
            conn.execute("ALTER TABLE paper_position_lots ADD COLUMN cost_fee_included INTEGER NOT NULL DEFAULT 0")
        # 升级前的 lot 只记录成交价。来源订单存在时，将已实际扣除的买入费用
        # 分摊到每股成本；无来源的历史兼容 lot 不臆造费用，保留原始成本。
        conn.execute(
            """UPDATE paper_position_lots
               SET cost=cost+COALESCE((SELECT fees / NULLIF(qty,0) FROM paper_orders o WHERE o.id=paper_position_lots.source_order_id),0),
                   cost_fee_included=1
               WHERE cost_fee_included=0 AND source_order_id IS NOT NULL"""
        )
        # A crashed worker must not leave a phantom reservation blocking the
        # shared pool forever.  Filled orders consume their reservation;
        # cancelled/expired/rejected orders release it.  Pending limit and
        # entry-frozen waitlist orders are intentionally kept reserved for the
        # next trigger/retry pass.
        conn.execute(
            """UPDATE paper_capital_reservations
               SET status='consumed',released_at=COALESCE(released_at,?)
               WHERE status='reserved' AND EXISTS(
                   SELECT 1 FROM paper_orders o
                   WHERE CAST(o.id AS TEXT)=paper_capital_reservations.order_key
                     AND o.status='filled'
               )""",
            (_now(),),
        )
        retry_order_placeholders = ",".join("?" for _ in ENTRY_RETRY_ORDER_STATUSES)
        conn.execute(
            f"""UPDATE paper_capital_reservations
               SET status='released',released_at=COALESCE(released_at,?)
               WHERE status='reserved' AND NOT EXISTS(
                   SELECT 1 FROM paper_orders o
                   WHERE CAST(o.id AS TEXT)=paper_capital_reservations.order_key
                     AND o.status IN ({retry_order_placeholders})
               )""",
            (_now(), *ENTRY_RETRY_ORDER_STATUSES),
        )
        # Backfill waitlist markers produced before the recheck lifecycle was
        # unified.  They are historical audit evidence, but must not remain
        # visible as live pending orders once a newer decision for the same
        # account/code has been written.
        conn.execute(
            """UPDATE paper_orders AS marker
               SET status='superseded',
                   reason=COALESCE(marker.reason,'') || '；已由后续实时复核委托替代'
               WHERE marker.status=? AND marker.side='buy'
                 AND EXISTS(
                     SELECT 1 FROM paper_orders AS newer
                     WHERE newer.account_id=marker.account_id
                       AND newer.code=marker.code AND newer.side='buy'
                       AND newer.id>marker.id
                 )""",
            (ENTRY_FROZEN_WAITLIST_STATUS,),
        )
        _rebuild_realized_pnl(conn)
        position_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_positions)").fetchall()}
        if "asset_type" not in position_columns:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'stock_t1'")
        account_columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_accounts)").fetchall()}
        account_migrations = {
            "cycle_id": "INTEGER", "mode": "TEXT NOT NULL DEFAULT 'swing'",
            "style": "TEXT NOT NULL DEFAULT 'pullback'", "risk_profile": "TEXT NOT NULL DEFAULT 'aggressive'",
            "params": "TEXT NOT NULL DEFAULT '{}'", "daily_start_nav": "REAL",
            "daily_nav_date": "TEXT", "cooldown_until": "TEXT",
        }
        for column, definition in account_migrations.items():
            if column not in account_columns:
                conn.execute(f"ALTER TABLE paper_accounts ADD COLUMN {column} {definition}")
        _ensure_accounts(conn)
        _ensure_cycle(conn)
        _ensure_runtime_lease_columns(conn)
        _recover_stale_runtime_state(conn)
        _reconcile_signal_order_states(conn)
        _ensure_performance_indexes(conn)
        _RUNNER_BOOT_RECOVERED = True
    # H9: 建表/迁移/启动回收是批量写，显式收缩 WAL，避免 -wal 无界增长。
    _wal_checkpoint()


def _benchmark_close():
    frame = dfc.load_cached_kline("BENCH_000300")
    if frame is None or frame.empty:
        return None
    return _num(frame["close"].iloc[-1], None)


def _ensure_accounts(conn):
    benchmark = _benchmark_close()
    active_cycle = conn.execute(
        "SELECT id,status,capital FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    configured_share = (
        _num(active_cycle["capital"], 0.0) / max(len(ACCOUNT_SPECS), 1)
        if active_cycle and _num(active_cycle["capital"], 0.0) > 0
        else 20000.0
    )
    configured_status = active_cycle["status"] if active_cycle else "paused"
    for account_id, spec in ACCOUNT_SPECS.items():
        exists = conn.execute("SELECT 1 FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        new_run = not bool(exists)
        if exists:
            conn.execute(
                """UPDATE paper_accounts SET name=?,source_strategy=?,cycle_days=?,
                   max_weight=?,max_exposure=?,mode=COALESCE(NULLIF(mode,''),?),
                   risk_profile=? WHERE id=?""",
                (spec["name"], spec["source_strategy"], spec["cycle_days"],
                 spec["max_weight"], spec["max_exposure"], spec["mode"], spec["risk_profile"], account_id),
            )
            # Version only the newly introduced strategy.  Existing account
            # versions remain untouched so historical three-strategy audits
            # are not rewritten by a configuration refresh.
            if spec.get("strategy_version"):
                conn.execute(
                    "UPDATE paper_accounts SET version=? WHERE id=?",
                    (spec["strategy_version"], account_id),
                )
            continue
        now = _now()
        conn.execute(
            """INSERT INTO paper_accounts
            (id,name,source_strategy,status,initial_cash,cash,cycle_days,max_positions,max_weight,max_exposure,risk_profile,version,benchmark_start,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, spec["name"], spec["source_strategy"], configured_status, configured_share, configured_share,
             spec["cycle_days"], spec["max_positions"], spec["max_weight"], spec["max_exposure"],
             spec["risk_profile"], spec.get("strategy_version") or "v3.0", benchmark, now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
            (account_id, dt.date.today().isoformat(), configured_share, 0.0, configured_share, benchmark, now),
        )


def _reconcile_shared_cash(conn, cycle_id):
    """Repair cash drift from the one-time shared-pool migration.

    Executed fills are the authoritative cash ledger.  If a prior process
    stopped after debiting shared cash but before writing its fill, restore the
    difference proportionally to the strategy ledgers so the pool NAV cannot
    be understated.  The correction is idempotent and is audited.
    """
    accounts = _rows(conn, "SELECT id,initial_cash,cash FROM paper_accounts WHERE cycle_id=? ORDER BY id", (cycle_id,))
    if not accounts:
        return 0.0
    cycle = conn.execute("SELECT capital FROM paper_cycles WHERE id=?", (cycle_id,)).fetchone()
    # The cycle declaration is the only economic capital authority.  Strategy
    # ledgers are attribution buckets and a late-joining strategy may have a
    # display-only reference capital, so summing account.initial_cash can mint
    # money into the shared pool.
    initial_total = max(0.0, _num(cycle["capital"] if cycle else 0.0))
    ledger_net = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN side='sell' THEN amount-fees
                                      WHEN side='buy' THEN -(amount+fees)
                                      ELSE 0 END),0)
             FROM paper_fills WHERE account_id IN (SELECT id FROM paper_accounts WHERE cycle_id=?)""",
        (cycle_id,),
    ).fetchone()[0]
    expected = initial_total + _num(ledger_net)
    actual = sum(_num(row.get("cash")) for row in accounts)
    drift = expected - actual
    if abs(drift) < 0.01 or initial_total <= 0:
        return 0.0
    if drift > 0:
        funded = [row for row in accounts if _num(row.get("initial_cash")) > 0]
        weight_total = sum(_num(row.get("initial_cash")) for row in funded)
        targets = funded or accounts
        weight_total = weight_total or len(targets)
        for row in targets:
            weight = (
                _num(row.get("initial_cash")) / weight_total
                if funded else 1.0 / max(len(targets), 1)
            )
            conn.execute(
                "UPDATE paper_accounts SET cash=cash+?,updated_at=? WHERE id=?",
                (drift * weight, _now(), row["id"]),
            )
    else:
        # Remove excess cash without making any attribution bucket negative.
        # This is intentionally equivalent to a shared-pool debit, not a
        # proportional rewrite of strategy history.
        _debit_shared_cash(conn, -drift)
    _audit(conn, None, "shared_cash_reconciled", f"共享资金池现金账差 {drift:+.2f} 元，已按周期固定本金与成交现金流修复")
    return drift


def _available_cycle_ledger_capital(conn, cycle, account_id):
    """Return unallocated economic capital without enlarging the cycle."""
    allocated = conn.execute(
        "SELECT COALESCE(SUM(initial_cash),0) s,COUNT(*) n FROM paper_accounts WHERE cycle_id=? AND id<>?",
        (cycle["id"], account_id),
    ).fetchone()
    if int(allocated["n"] or 0) <= 0:
        return _num(cycle["capital"], 0.0) / max(len(ACCOUNT_SPECS), 1)
    existing_initial = _num(allocated["s"])
    return max(0.0, _num(cycle["capital"], 0.0) - existing_initial)


def _late_join_reference_capital(conn, cycle, account_id):
    """Display-only performance base for a sleeve joining a funded cycle.

    Match the share already attributed to the sleeves funded in this cycle
    instead of re-dividing the cycle capital by the new strategy count.  The
    funded sleeves keep their original share, so ``capital / len(ACCOUNT_SPECS)``
    would compare strategies on different bases: with a 300k cycle the four
    funded sleeves hold 75k each while the late joiner would be measured on
    60k, inflating its percentage return for the same P&L.
    """
    funded = conn.execute(
        """SELECT COALESCE(SUM(initial_cash),0) s, COUNT(*) n
             FROM paper_accounts WHERE cycle_id=? AND id<>? AND initial_cash>0""",
        (cycle["id"], account_id),
    ).fetchone()
    if int(funded["n"] or 0) > 0 and _num(funded["s"]) > 0:
        return round(_num(funded["s"]) / int(funded["n"]), 2)
    return round(_num(cycle["capital"], 0.0) / max(len(ACCOUNT_SPECS), 1), 2)


def _ensure_cycle(conn):
    """给旧版账户补一个可归档周期；不删除任何已有记录。"""
    active = conn.execute("SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1").fetchone()
    if active is None:
        account = conn.execute("SELECT * FROM paper_accounts ORDER BY id LIMIT 1").fetchone()
        capital = _num(account["initial_cash"], 100000.0) if account else 100000.0
        now = _now()
        key = "legacy-" + now.replace("-", "").replace(":", "").replace(" ", "-")
        conn.execute(
            "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (key, "paused", capital, "shared_pool", now, now),
        )
        active = conn.execute("SELECT * FROM paper_cycles WHERE cycle_key=?", (key,)).fetchone()
    # Only a synthetic legacy cycle may infer its declared capital from old
    # account ledgers.  A normal shared-pool cycle keeps paper_cycles.capital
    # authoritative; otherwise adding a strategy can silently enlarge it.
    account_total = conn.execute(
        "SELECT COALESCE(SUM(initial_cash),0) FROM paper_accounts WHERE cycle_id=?",
        (active["id"],),
    ).fetchone()[0]
    if str(active["cycle_key"] or "").startswith("legacy-") and _num(account_total) > _num(active["capital"]):
        conn.execute("UPDATE paper_cycles SET capital=?,updated_at=? WHERE id=?",
                     (_num(account_total), _now(), active["id"]))
        active = conn.execute("SELECT * FROM paper_cycles WHERE id=?", (active["id"],)).fetchone()
    for account_id, spec in ACCOUNT_SPECS.items():
        current = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if current is None:
            continue
        style = current["style"] if current["style"] in STYLE_PROFILES else spec["default_style"]
        # 旧版数据库新增列的默认值为 pullback；给日内模型纠正为强势风格，且不影响已有成交。
        if account_id == "tq_breakout" and style == "pullback":
            has_fills = conn.execute("SELECT 1 FROM paper_fills WHERE account_id=? LIMIT 1", (account_id,)).fetchone()
            if not has_fills:
                style = "strong"
        # 新增策略时也要加入当前周期并继承该周期的状态/虚拟资金；不能留下默认
        # 的 20,000 元暂停账户，否则会破坏同本金公平对比。
        is_new_for_cycle = current["cycle_id"] is None
        if is_new_for_cycle:
            capital = _num(active["capital"], 100000.0)
            # Preserve the shared-pool synthetic attribution used by
            # _create_cycle: a newly added strategy receives one equal ledger
            # share, never the entire pool as a private bankroll.  When
            # joining an already active legacy cycle, only unallocated
            # configured capital is attributed; zero is safe and keeps the
            # shared cash ledger from being inflated.
            account_capital = _available_cycle_ledger_capital(conn, active, account_id)
            benchmark = _benchmark_close()
            conn.execute(
                "UPDATE paper_accounts SET cycle_id=?,mode=?,style=?,status=?,initial_cash=?,cash=?,benchmark_start=?,daily_start_nav=?,daily_nav_date=?,risk_profile=?,params=COALESCE(NULLIF(params,''),'{}') WHERE id=?",
                (active["id"], spec["mode"], style, active["status"], account_capital, account_capital, benchmark,
                 account_capital, _date().isoformat(), spec["risk_profile"], account_id),
            )
            conn.execute("DELETE FROM paper_nav WHERE account_id=?", (account_id,))
            conn.execute(
                "INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
                (account_id, _date().isoformat(), account_capital, 0.0, account_capital, benchmark, _now()),
            )
            conn.execute(
                "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (active["id"], account_id, spec.get("strategy_version") or "v2.0", style, "{}", "新增并行策略", _date().isoformat(), _now()),
            )
        else:
            conn.execute(
                "UPDATE paper_accounts SET mode=?,style=?,risk_profile=?,params=COALESCE(NULLIF(params,''),'{}') WHERE id=?",
                (spec["mode"], style, spec["risk_profile"], account_id),
            )
            # A newly deployed strategy may have been inserted by an earlier
            # process with the legacy paused/default row.  If the active cycle
            # is already running and this account has no activity, activate it
            # with the cycle rather than silently omitting today's signals.
            if (
                account_id in {NEW_STRATEGY_ID, MAIN_FORCE_STRATEGY_ID}
                and active["status"] == "running"
                and str(current["status"] or "") != "running"
                and not conn.execute(
                    "SELECT 1 FROM paper_fills WHERE account_id=? LIMIT 1", (account_id,)
                ).fetchone()
            ):
                account_capital = _available_cycle_ledger_capital(conn, active, account_id)
                conn.execute(
                    "UPDATE paper_accounts SET cycle_id=?,status='running',initial_cash=?,cash=?,daily_start_nav=?,daily_nav_date=?,updated_at=? WHERE id=?",
                    (active["id"], account_capital, account_capital, account_capital,
                     _date().isoformat(), _now(), account_id),
                )
                conn.execute("DELETE FROM paper_nav WHERE account_id=?", (account_id,))
                conn.execute(
                    "INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
                    (account_id, _date().isoformat(), account_capital, 0.0, account_capital, _benchmark_close(), _now()),
                )
        # ``paper_accounts.style`` predates the main-force sleeve and its
        # schema default is ``pullback``.  A late-created account therefore
        # looked valid to the generic migration but had every main-force top-10
        # candidate sent through the trend-pullback pre-filter.  Correct only
        # an untouched main-force account: historical fills/positions always
        # keep their original strategy style and evidence.
        if account_id == MAIN_FORCE_STRATEGY_ID and current is not None:
            # Refresh after the activation branch: ``current`` was read before
            # its economic ledger allocation was reduced to zero.
            current = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
            has_activity = bool(conn.execute(
                "SELECT 1 FROM paper_fills WHERE account_id=? LIMIT 1", (account_id,)
            ).fetchone()) or bool(conn.execute(
                "SELECT 1 FROM paper_position_lots WHERE account_id=? AND remaining_qty>0 LIMIT 1", (account_id,)
            ).fetchone())
            if not has_activity:
                desired_style = spec["default_style"]
                params = _loads(current["params"], {}) or {}
                repairs = []
                if str(current["style"] or "") != desired_style:
                    repairs.append(f"风格 {current['style'] or '未知'}→{desired_style}")
                # The current cycle's real cash remains shared and fully
                # allocated to its original sleeves.  Store a reference only
                # for this new sleeve's independent risk/performance display;
                # it is never added to pool cash or pool NAV.
                if _num(current["initial_cash"], 0.0) <= 0 and _num(params.get("shared_pool_reference_capital"), 0.0) <= 0:
                    reference_capital = _late_join_reference_capital(conn, active, account_id)
                    if reference_capital > 0:
                        params.update({
                            "shared_pool_reference_capital": reference_capital,
                            "shared_pool_reference_effective_date": _date().isoformat(),
                            "shared_pool_reference_mode": "late_join_shared_pool",
                        })
                        repairs.append(f"共享池参考资金 ¥{reference_capital:,.2f}")
                if repairs:
                    now = _now()
                    conn.execute(
                        "UPDATE paper_accounts SET style=?,params=?,updated_at=? WHERE id=?",
                        (desired_style, _json(params), now, account_id),
                    )
                    conn.execute(
                        "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (active["id"], account_id, spec.get("strategy_version") or "v2.0", desired_style,
                         _json(params), "中途接入主力策略修复：" + "；".join(repairs), _date().isoformat(), now),
                    )
                    _audit(conn, account_id, "main_force_activation_repaired", "；".join(repairs) + "；共享池现金和历史成交未改写")
                reference_capital = max(
                    _num(current["initial_cash"], 0.0),
                    _num(params.get("shared_pool_reference_capital"), 0.0),
                )
                if _num(current["initial_cash"], 0.0) <= 0 and reference_capital > 0:
                    conn.execute(
                        "UPDATE paper_accounts SET daily_start_nav=?,daily_nav_date=? WHERE id=?",
                        (reference_capital, _date().isoformat(), account_id),
                    )
                    conn.execute(
                        """UPDATE paper_nav SET cash=0,market_value=0,nav=?
                             WHERE account_id=? AND nav_date=?""",
                        (reference_capital, account_id, _date().isoformat()),
                    )
    _reconcile_shared_cash(conn, active["id"])


def _active_cycle(conn):
    cycle = conn.execute("SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1").fetchone()
    if cycle is None:
        _ensure_cycle(conn)
        cycle = conn.execute("SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(cycle)


def _shared_account_rows(conn, cycle_id=None):
    """Return the strategy ledgers participating in the shared capital pool."""
    if cycle_id is None:
        cycle = _active_cycle(conn)
        cycle_id = cycle["id"]
    return _rows(conn, "SELECT * FROM paper_accounts WHERE cycle_id=? ORDER BY id", (cycle_id,))


def _shared_initial_cash(conn, cycle=None):
    """Total capital for the current simulation cycle, not per strategy."""
    cycle = cycle or _active_cycle(conn)
    declared = _num(cycle.get("capital"))
    if declared > 0:
        return declared
    accounts = _shared_account_rows(conn, cycle["id"])
    return max(sum(_num(row.get("initial_cash")) for row in accounts), 0.0)


def _account_reference_capital(account):
    """Return the strategy's display/risk reference without minting pool cash.

    A strategy can be introduced after a shared-pool cycle has already
    attributed every yuan to the existing sleeves.  Giving that new sleeve a
    database ``cash`` balance would inflate the pool; leaving its only
    reference at zero, however, makes independent performance/risk displays
    meaningless.  The optional reference below is therefore deliberately
    *not* part of ``_shared_initial_cash`` or ``_shared_cash``.  Execution
    continues to use the one real shared pool.
    """
    initial = max(0.0, _num((account or {}).get("initial_cash"), 0.0))
    if initial > 0:
        return initial
    if str((account or {}).get("id") or "") != MAIN_FORCE_STRATEGY_ID:
        return initial
    params = _loads((account or {}).get("params"), {}) or {}
    return max(0.0, _num(params.get("shared_pool_reference_capital"), 0.0))


def _economic_pool_nav_history(conn, cycle=None):
    """Build fixed-capital pool NAV from strategy-attributed P&L.

    ``paper_nav.nav`` is a synthetic per-strategy series.  Its reference
    capital must be subtracted before strategy contributions are aggregated,
    otherwise a late-joining strategy creates a fake deposit in pool history.
    """
    cycle = cycle or _active_cycle(conn)
    accounts = _shared_account_rows(conn, cycle["id"])
    references = {row["id"]: _account_reference_capital(row) for row in accounts}
    rows = _rows(
        conn,
        """SELECT account_id,nav_date,nav FROM paper_nav
             WHERE account_id IN (SELECT id FROM paper_accounts WHERE cycle_id=?)
             ORDER BY nav_date,account_id""",
        (cycle["id"],),
    )
    dates = sorted({str(row.get("nav_date")) for row in rows if row.get("nav_date")})
    by_account = {}
    for row in rows:
        by_account.setdefault(row["account_id"], {})[row["nav_date"]] = _num(row.get("nav"), None)
    latest = {}
    result = []
    for nav_date in dates:
        for account_id, values in by_account.items():
            if nav_date in values and values[nav_date] is not None:
                latest[account_id] = values[nav_date]
        contribution = sum(
            _num(nav) - _num(references.get(account_id))
            for account_id, nav in latest.items()
        )
        result.append({
            "nav_date": nav_date,
            "nav": _shared_initial_cash(conn, cycle) + contribution,
        })
    return result


def _shared_cash(conn, cycle_id=None):
    return sum(_num(row.get("cash")) for row in _shared_account_rows(conn, cycle_id))


def _pending_buy_reservations(conn, cycle_id=None, exclude_order_key=None):
    """Return reserved buy capital by strategy and in total.

    Reservations are deliberately separate from the cash ledger: a pending
    limit order has not filled yet, but its buying power and 82% pool capacity
    are no longer available to another strategy.  ``exclude_order_key`` is
    used while rechecking an existing pending order so that the order does
    not reserve itself twice.

    P3 审计修复：统计不再按 cycle_id 过滤——周期切换后旧周期的手动
    限价单仍处于 reserved 且其预占真实占用现金；漏统计会让新周期订单
    重复使用同一笔资金，先后触发时后者误拒。
    """
    params = []
    where = "side='buy' AND status='reserved'"
    if exclude_order_key is not None:
        where += " AND order_key<>?"
        params.append(str(exclude_order_key))
    rows = _rows(
        conn,
        f"""SELECT account_id,COALESCE(SUM(amount+fees),0) AS amount
            FROM paper_capital_reservations WHERE {where}
            GROUP BY account_id""",
        tuple(params),
    )
    by_account = {row["account_id"]: max(0.0, _num(row.get("amount"))) for row in rows}
    return by_account, sum(by_account.values())


def _pending_position_slots(conn, positions=None, exclude_order_key=None):
    """Return distinct slots held by executable pending buy orders.

    ``deferred_capacity`` and ``entry_frozen_waitlist`` are research queue
    markers, not executable orders: neither has reserved cash nor a claim on
    a strategy seat.  Counting them here turns every waitlist candidate into
    a phantom position and can permanently report impossible values such as
    62/15 occupied seats.
    """
    positions = positions if positions is not None else _position_rows(conn)
    existing = {
        (str(item.get("account_id")), str(item.get("code")))
        for item in positions if int(_num(item.get("qty"))) >= LOT_SIZE
    }
    params = []
    placeholders = ",".join("?" for _ in ENTRY_SLOT_OCCUPYING_ORDER_STATUSES)
    where = f"origin IN ('manual','strategy') AND side='buy' AND status IN ({placeholders})"
    params.extend(ENTRY_SLOT_OCCUPYING_ORDER_STATUSES)
    if exclude_order_key is not None:
        where += " AND id<>?"
        params.append(int(exclude_order_key))
    rows = _rows(conn, f"SELECT account_id,code FROM paper_orders WHERE {where}", tuple(params))
    return {
        (str(row.get("account_id")), str(row.get("code")))
        for row in rows
        if (str(row.get("account_id")), str(row.get("code"))) not in existing
    }


def _reserve_shared_capital(conn, order_key, account_id, code, amount, fees=0.0):
    """Atomically reserve buying power for a not-yet-finalised buy order.

    This is a second line of defence behind the sizing model.  It prevents
    two concurrent requests from spending the same cash while the order is
    still pending, without ever allowing the shared pool's hard 82% ceiling
    to be bypassed.
    """
    order_key = str(order_key)
    amount = max(0.0, _num(amount))
    fees = max(0.0, _num(fees))
    existing = conn.execute(
        "SELECT status FROM paper_capital_reservations WHERE order_key=?", (order_key,)
    ).fetchone()
    if existing and existing["status"] == "consumed":
        return False, "该订单资金预占已经消费，禁止重复成交"

    # A triggered limit order can have a different fill price/quantity from
    # its original limit-price reservation.  Re-size the existing reservation
    # while excluding the old amount belonging to this same order; otherwise
    # the order appears funded but can consume another order's cash.
    _, pending_total = _pending_buy_reservations(
        conn, exclude_order_key=order_key,
    )
    available_cash = _shared_cash(conn) - pending_total
    if amount + fees > available_cash + 1e-6:
        return False, f"待成交买单已预占 ¥{pending_total:,.2f}，共享可用现金不足"
    if existing:
        conn.execute(
            """UPDATE paper_capital_reservations
               SET status='reserved',released_at=NULL,amount=?,fees=?,created_at=?
               WHERE order_key=?""",
            (amount, fees, _now(), order_key),
        )
        return True, None
    cycle = _active_cycle(conn)
    conn.execute(
        """INSERT INTO paper_capital_reservations
           (cycle_id,order_key,account_id,code,side,amount,fees,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (cycle["id"], order_key, account_id, code, "buy", amount, fees,
         "reserved", _now()),
    )
    return True, None


def _finish_capital_reservation(conn, order_key, status):
    if status not in {"consumed", "released"}:
        raise ValueError("非法资金预占状态")
    conn.execute(
        "UPDATE paper_capital_reservations SET status=?,released_at=? WHERE order_key=? AND status='reserved'",
        (status, _now(), str(order_key)),
    )


def _debit_shared_cash(conn, amount, preferred_account_id=None):
    """Debit a shared cash pool while retaining per-strategy audit ownership."""
    amount = max(0.0, _num(amount))
    rows = _shared_account_rows(conn)
    rows.sort(key=lambda row: (0 if row["id"] == preferred_account_id else 1, -_num(row.get("cash"))))
    if amount > sum(_num(row.get("cash")) for row in rows) + 1e-6:
        raise ValueError("共享资金池可用现金不足")
    remaining = amount
    for row in rows:
        debit = min(max(0.0, _num(row.get("cash"))), remaining)
        if debit:
            conn.execute("UPDATE paper_accounts SET cash=cash-?,updated_at=? WHERE id=?", (debit, _now(), row["id"]))
            remaining -= debit
        if remaining <= 1e-6:
            break
    return True


def _credit_shared_cash(conn, amount, account_id):
    amount = _num(amount)
    # A negative credit is a debit that bypasses _debit_shared_cash's balance
    # check and would silently corrupt the pool ledger; refuse it loudly.
    if amount < 0:
        raise ValueError(f"_credit_shared_cash 收到负数金额 {amount}（account={account_id}），疑似上游计算错误")
    conn.execute("UPDATE paper_accounts SET cash=cash+?,updated_at=? WHERE id=?", (amount, _now(), account_id))


def _shared_account_exposure(conn, quotes, asof_day=None):
    positions = _position_rows(conn, asof_day=asof_day)
    value = 0.0
    industries = {}
    codes = {}
    for pos in positions:
        price = _num((quotes.get(pos["code"]) or {}).get("price"), _num(pos["cost"]))
        item_value = _num(pos["qty"]) * price
        value += item_value
        codes[pos["code"]] = codes.get(pos["code"], 0.0) + item_value
        industry = pos.get("industry") or "未知"
        industries[industry] = industries.get(industry, 0.0) + item_value
    cash = _shared_cash(conn)
    return positions, value, cash + value, industries, codes


def _shared_risk_state(conn, account, nav, asof_day):
    """Apply each strategy's risk profile to the same pool-level NAV path."""
    profile = _risk_profile(account)
    day = _date(asof_day).isoformat()
    # P3 审计修复（R3）：按当前周期过滤——归档时 paper_nav 全表删除前
    # 无影响，但未来多周期并存时未过滤会把其他周期的净值混入熔断基线。
    rows = _economic_pool_nav_history(conn)
    previous = [(_num(row.get("nav")), row.get("nav_date")) for row in rows if row.get("nav_date") < day]
    start_nav = previous[-1][0] if previous else _shared_initial_cash(conn)
    peak = max([value for value, _ in previous] + [nav, _shared_initial_cash(conn)])
    daily_loss = 1 - nav / start_nav if start_nav else 0.0
    drawdown = 1 - nav / peak if peak else 0.0
    reasons = []
    cooldown_triggered = False
    if daily_loss >= profile["daily_loss"]:
        reasons.append(f"共享资金池单日亏损 {daily_loss*100:.2f}% 触发熔断")
    if drawdown >= profile["drawdown"]:
        reasons.append(f"共享资金池滚动回撤 {drawdown*100:.2f}% 触发风控")
        cooldown_triggered = True
    # P3 审计修复（P0）：回撤冷却此前整体失效——唯一的 cooldown_until
    # 写入者在从未被调用的死函数里，回撤触发只瞬时阻断，NAV 反弹即恢复
    # 开仓。现在：回撤触发写入冷静期；冷静期内即使 NAV 反弹也维持封锁，
    # 直到 cooldown_until 到期。
    cooldown_until = str(account.get("cooldown_until") or "")
    cooldown_active = bool(cooldown_until) and cooldown_until >= day
    if cooldown_triggered and not cooldown_active:
        try:
            until = _next_weekday(
                _date(asof_day) + dt.timedelta(days=max(int(profile.get("cooldown_days", 2)), 1) - 1)
            ).isoformat()
        except Exception:
            until = (_date(asof_day) + dt.timedelta(days=max(int(profile.get("cooldown_days", 2)), 1))).isoformat()
        # 池级回撤冷却所有账户：回撤是按池 NAV 计算的，单一账户触发
        # 说明整个资金池处于回撤状态；只冷却触发者会让其他账户继续
        # 在回撤底部加仓。
        conn.execute("UPDATE paper_accounts SET cooldown_until=?", (until,))
        account["cooldown_until"] = until
        cooldown_until, cooldown_active = until, True
        reasons.append(f"回撤风控冷却期至 {until}，暂停新开仓")
    elif cooldown_active:
        reasons.append(f"回撤风控冷却期内（至 {cooldown_until}），暂停新开仓")
    return {"blocked": bool(reasons), "reasons": reasons,
            "daily_loss_pct": round(daily_loss * 100, 2),
            "drawdown_pct": round(drawdown * 100, 2),
            "cooldown_until": cooldown_until or None,
            "cooldown_active": cooldown_active,
            "pool_initial_cash": round(_shared_initial_cash(conn), 2),
            "pool_cash": round(_shared_cash(conn), 2)}


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _audit(conn, account_id, event, detail):
    conn.execute("INSERT INTO paper_audit(account_id,event,detail,created_at) VALUES(?,?,?,?)",
                 (account_id, event, detail, _now()))


def _volatility_shadow(code, asof_day, price=None):
    """Return volatility diagnostics only; never changes a risk threshold."""
    result = {"version": "volatility-shadow-v1", "status": "unknown", "atr20_pct": None,
              "daily_std_pct": None, "samples": 0, "asof_date": _date(asof_day).isoformat()}
    try:
        frame = _completed_kline(str(code), _date(asof_day), inclusive=False)
        if frame is None or len(frame) < 15:
            result["reason"] = "历史K线不足15根"
            return result
        closes, trs, prev = [], [], None
        for _, row in frame.iterrows():
            close = _num(row.get("close"), None)
            high = _num(row.get("high"), close)
            low = _num(row.get("low"), close)
            if not close or close <= 0:
                continue
            closes.append(close)
            if prev:
                trs.append(max(high - low, abs(high - prev), abs(low - prev)))
            prev = close
        if len(closes) < 15:
            result["reason"] = "有效收盘价不足15根"
            return result
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        recent_tr = trs[-20:]
        recent_ret = returns[-20:]
        mean_ret = sum(recent_ret) / len(recent_ret) if recent_ret else 0.0
        variance = sum((value - mean_ret) ** 2 for value in recent_ret) / max(len(recent_ret) - 1, 1)
        last_close = _num(price, closes[-1]) or closes[-1]
        result.update({
            "status": "ok", "samples": len(closes),
            "atr20_pct": round(sum(recent_tr) / len(recent_tr) / last_close * 100, 3) if recent_tr else None,
            "daily_std_pct": round((variance ** 0.5) * 100, 3),
            "last_close": round(last_close, 4),
        })
    except Exception as exc:
        result["reason"] = f"影子计算失败：{type(exc).__name__}"
    return result


def _recovery_policy(account_id):
    return dict(RECOVERY_POLICIES.get(account_id, RECOVERY_POLICIES["trend_pullback"]))


def _recovery_watches(conn, account_id, day):
    """Latest protective watches, bounded to avoid stale audit rows."""
    cutoff = (_date(day) - dt.timedelta(days=5)).isoformat()
    watches = {}
    rows = _rows(conn, """SELECT detail,created_at FROM paper_audit
                         WHERE account_id=? AND event='protective_exit_recovery_watch'
                           AND substr(created_at,1,10)>=? ORDER BY id DESC LIMIT 100""",
                 (account_id, cutoff))
    for row in rows:
        detail = _loads(row.get("detail"), {})
        code = str(detail.get("code") or "")
        if not code or code in watches or detail.get("status") not in {"watching", "observing"}:
            continue
        try:
            if _date(detail.get("expires_on")) < _date(day):
                continue
        except Exception:
            continue
        watches[code] = detail
    return watches


def _recovery_observation(conn, account_id, code, watch, quote, day):
    policy = _recovery_policy(account_id)
    now = dt.datetime.now()
    exit_at = str(watch.get("exit_at") or "")[:19]
    if exit_at:
        try:
            elapsed = (now - dt.datetime.fromisoformat(exit_at)).total_seconds() / 60
            if elapsed < policy["cooldown_minutes"]:
                return False, f"止损后恢复观察冷却中，还需 {policy['cooldown_minutes'] - int(elapsed)} 分钟", {"scans": 0}
        except (TypeError, ValueError):
            pass
    price = _num(quote.get("price"), 0)
    exit_price = _num(watch.get("exit_price"), 0)
    if not price or not exit_price:
        return False, "恢复观察缺少有效价格", {"scans": 0}
    obs = conn.execute("""SELECT COUNT(*) FROM paper_audit
        WHERE account_id=? AND event='protective_recovery_observation'
          AND detail LIKE ? AND created_at>=?""",
        (account_id, f'%"code": "{code}"%', exit_at or "0000-01-01")).fetchone()[0]
    observation = {"code": code, "price": price, "exit_price": exit_price, "scans": int(obs) + 1,
                   "required_scans": policy["min_scans"], "reclaim_pct": policy["reclaim_pct"]}
    _audit(conn, account_id, "protective_recovery_observation", _json(observation))
    if price < exit_price * (1 + policy["reclaim_pct"]):
        return False, f"止损后尚未收复 {policy['reclaim_pct']*100:.1f}%", observation
    if int(obs) + 1 < policy["min_scans"]:
        return False, f"恢复观察需连续 {policy['min_scans']} 次确认", observation
    if account_id == "sector_rotation" and _num(quote.get("pct"), -99) < 0:
        return False, "板块轮动恢复时个股仍为负涨幅", observation
    return True, "止损后恢复观察通过，仍须重新通过完整入场门禁", observation


def _risk_log(conn, account_id, code, side, decision, reason, payload):
    payload = _with_decision_snapshot(
        payload or {}, account_id=account_id, code=code, side=side,
        decision=decision, reason=reason,
    )
    conn.execute(
        "INSERT INTO paper_risk_decisions(account_id,code,side,decision,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)",
        (account_id, code, side, decision, reason, _json(payload), _now()),
    )


def _bootstrap_structural_recheck_cooldown(conn, account_id, code, asof_day):
    """Return a short cooldown for unchanged structural bootstrap rejections.

    This is account-local by design.  The three strategies may independently
    trade the same code, but a structurally rejected candidate must not occupy
    the same strategy's limited live-review slots every three minutes.
    Quote/source failures are intentionally *not* cooled down because they
    should be retried as soon as the backup source recovers.
    """
    cooldown_minutes = int(BOOTSTRAP_STRUCTURAL_RECHECK_COOLDOWN_MINUTES.get(account_id, 0))
    if cooldown_minutes <= 0:
        return None
    row = conn.execute(
        """SELECT reason,created_at FROM paper_risk_decisions
           WHERE account_id=? AND code=? AND side='buy'
             AND decision='rejected_bootstrap'
             AND substr(created_at,1,10)=?
           ORDER BY id DESC LIMIT 1""",
        (account_id, str(code), _date(asof_day).isoformat()),
    ).fetchone()
    if not row:
        return None
    reason = str(row["reason"] or "")
    structural_tokens_by_account = {
        "tq_breakout": (
            "短线候选实时涨幅", "量比", "主力净流入", "接近涨停",
            "盘中已上涨", "接力追买所需资金、量能或候选质量不足",
        ),
        "trend_pullback": (
            "偏离 MA20", "趋势回踩结构确认评分", "MA20 明显低于 MA60",
            "趋势结构仍未修复", "均线结构", "主力净流出占比",
            "实时跌幅", "回踩执行区间",
        ),
        "sector_rotation": (
            "所属板块未进入前15强正收益热点", "板块轮动", "板块排名",
            "板块涨幅", "个股实时跌幅", "个股相对强度", "过热",
            "接近涨停", "主力净流出占比",
        ),
        NEW_STRATEGY_ID: (
            "财报", "披露", "突破路径", "三连阳", "MA5", "主要均线",
            "超大单资金", "实时量比", "质量突破", "接近涨停",
        ),
    }
    structural_tokens = structural_tokens_by_account.get(account_id, ())
    if not any(token in reason for token in structural_tokens):
        return None
    try:
        checked_at = dt.datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is not None:
            checked_at = checked_at.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
    elapsed = (dt.datetime.now() - checked_at).total_seconds() / 60.0
    if elapsed < 0 or elapsed >= cooldown_minutes:
        return None
    return {
        "reason": reason,
        "checked_at": str(row["created_at"]),
        "remaining_minutes": round(cooldown_minutes - elapsed, 1),
        "strategy": account_id,
    }


def _bootstrap_risk_rejection_cooldown(conn, account_id, code, asof_day):
    """Return an escalating cooldown after two same-day risk rejections.

    Structural/quote failures are deliberately excluded: those should retry
    as soon as data recovers.  Capacity and waitlist decisions are also
    excluded because they represent valid candidates waiting for a released
    slot.  Only risk decisions for a new buy candidate participate here.
    """
    base = int(RISK_REJECT_COOLDOWN_AFTER_TWO_MINUTES.get(account_id, 0))
    if base <= 0:
        return None
    day = _date(asof_day).isoformat()
    rows = _rows(
        conn,
        """SELECT decision,reason,created_at FROM paper_risk_decisions
           WHERE account_id=? AND code=? AND side='buy'
             AND substr(created_at,1,10)=?
           ORDER BY id DESC LIMIT ?""",
        (account_id, str(code), day, RISK_REJECT_COOLDOWN_MAX_SAMPLES),
    )
    if not rows:
        return None
    data_tokens = (
        "行情源", "行情过期", "行情未返回", "实时行情不足", "缺少实时",
        "双源差异", "双源未", "无法取得报价", "数据不足", "源异常",
    )
    capacity_tokens = (
        "容量", "动态上限", "共享硬上限", "席位", "资金池预占",
        "预占失败", "等待池", "deferred_capacity", "entry_frozen_waitlist",
    )
    risk_tokens = (
        "风控", "风险", "止损", "跌停", "涨停", "追高", "过热",
        "回撤", "主力净流出", "单票", "行业", "市场状态", "T+1",
        "可卖", "质量不足", "相对强度", "波动过大",
    )
    eligible = []
    for row in rows:
        decision = str(row.get("decision") or "")
        reason = str(row.get("reason") or "")
        if decision not in {"rejected", "rejected_bootstrap", "risk_rejected"}:
            continue
        if any(token in reason for token in data_tokens):
            continue
        if any(token in reason for token in capacity_tokens):
            continue
        if not any(token in reason for token in risk_tokens):
            continue
        eligible.append(row)
    if not eligible:
        return None
    count = min(len(eligible), RISK_REJECT_COOLDOWN_MAX_SAMPLES)
    # First refusal remains observable and can be rechecked next scan.  The
    # second refusal opens the strategy-local cooldown; later refusals only
    # happen after a cooldown expires and extend it conservatively.
    if count < 2:
        return None
    latest = eligible[0]
    try:
        checked_at = dt.datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is not None:
            checked_at = checked_at.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
    cooldown_minutes = min(
        RISK_REJECT_COOLDOWN_MAX_MINUTES,
        base * (2 ** min(count - 2, 3)),
    )
    elapsed = (dt.datetime.now() - checked_at).total_seconds() / 60.0
    if elapsed < 0 or elapsed >= cooldown_minutes:
        return None
    return {
        "reason": str(latest.get("reason") or "风险门禁拒绝"),
        "checked_at": str(latest.get("created_at") or ""),
        "rejection_count": count,
        "cooldown_minutes": cooldown_minutes,
        "remaining_minutes": round(cooldown_minutes - elapsed, 1),
        "strategy": account_id,
        "scope": "new_entry_only",
    }


def _migrate_legacy_positions(conn):
    """首次升级时把旧聚合持仓拆成一笔可结算 lot；之后只以 lots 为准。"""
    cycle = _active_cycle(conn)
    legacy = _rows(conn, "SELECT * FROM paper_positions")
    for row in legacy:
        exists = conn.execute(
            "SELECT 1 FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=? LIMIT 1",
            (cycle["id"], row["account_id"], row["code"]),
        ).fetchone()
        if exists or _num(row.get("qty")) <= 0:
            continue
        conn.execute(
            """INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,is_t_base)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (cycle["id"], row["account_id"], row["code"], row.get("name"), row.get("industry"),
             int(row["qty"]), int(row["qty"]), _num(row["cost"]), row.get("entry_date") or _date().isoformat(),
             row.get("available_date") or _date().isoformat(), row.get("asset_type") or "stock_t1"),
        )


def _position_rows(conn, account_id=None, asof_day=None, readonly=False):
    """从 lot 聚合持仓，并显式区分可卖底仓和当日锁定份额。"""
    if readonly:
        # Read panels must not call _ensure_cycle/_migrate_legacy_positions:
        # both may write while the 3-minute worker is settling orders.
        cycle_row = conn.execute(
            "SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if cycle_row is None:
            return []
        cycle = dict(cycle_row)
    else:
        _migrate_legacy_positions(conn)
        cycle = _active_cycle(conn)
    day = _date(asof_day).isoformat()
    sql = "SELECT * FROM paper_position_lots WHERE cycle_id=? AND remaining_qty>0"
    params = [cycle["id"]]
    if account_id:
        sql += " AND account_id=?"
        params.append(account_id)
    lots = _rows(conn, sql, tuple(params))
    grouped = {}
    for lot in lots:
        key = (lot["account_id"], lot["code"])
        item = grouped.setdefault(key, {
            "account_id": lot["account_id"], "code": lot["code"], "name": lot.get("name"),
            "industry": lot.get("industry") or "未知", "qty": 0, "cost_amount": 0.0,
            "entry_date": lot["acquired_at"][:10], "available_qty": 0, "locked_qty": 0,
            "asset_type": lot.get("asset_type") or "stock_t1", "available_date": lot["available_date"],
        })
        qty = int(lot["remaining_qty"])
        item["qty"] += qty
        item["cost_amount"] += qty * _num(lot["cost"])
        if str(lot.get("acquired_at") or "")[:10] == day:
            item["today_acquired_qty"] = int(item.get("today_acquired_qty") or 0) + qty
            item["today_acquired_cost"] = _num(item.get("today_acquired_cost")) + qty * _num(lot["cost"])
        item["entry_date"] = min(item["entry_date"], lot["acquired_at"][:10])
        item["available_date"] = min(item["available_date"], lot["available_date"])
        if lot["available_date"] <= day:
            item["available_qty"] += qty
        else:
            item["locked_qty"] += qty
    legacy = {(p["account_id"], p["code"]): p for p in _rows(conn, "SELECT * FROM paper_positions")}
    # 终端展示常用“摊薄成本”：已卖出部分的净回款抵减尚未卖出仓位成本。
    # 风控、卖出结转仍使用下面的 FIFO settlement_cost，不能用摊薄成本替代。
    cash_flows = {
        (row["account_id"], row["code"]): row
        for row in _rows(
            conn,
            """SELECT account_id,code,
                      SUM(CASE WHEN side='buy' THEN COALESCE(amount,0)+COALESCE(fees,0) ELSE 0 END) AS buy_cash,
                      SUM(CASE WHEN side='sell' THEN COALESCE(amount,0)-COALESCE(fees,0) ELSE 0 END) AS sell_cash
                 FROM paper_orders WHERE status='filled' GROUP BY account_id,code""",
        )
    }
    out = []
    for key, item in grouped.items():
        item["cost"] = item.pop("cost_amount") / max(item["qty"], 1)
        item["settlement_cost"] = item["cost"]
        flow = cash_flows.get(key) or {}
        net_invested = _num(flow.get("buy_cash")) - _num(flow.get("sell_cash"))
        item["display_cost"] = net_invested / max(item["qty"], 1)
        old = legacy.get(key, {})
        item["peak_price"] = _num(old.get("peak_price"), item["cost"])
        item["take_stage"] = int(_num(old.get("take_stage"), 0))
        out.append(item)
    return out


# 今日盈亏可接受的报价来源白名单。dashboard_cache 是 overview 读模型从
# 全市场快照缓存（盘中每 3 分钟由 intraday 扫描刷新）打的标签，quote_at
# 与 live 同源同鲜度；只排除 local_cache（universe.json 陈旧价）。
# 此前硬编码 == "live" 导致仪表盘路径今日盈亏永远显示"交易中"占位。
_TODAY_PNL_QUOTE_SOURCES = {"live", "dashboard_cache", "live_snapshot"}


def _today_quote_is_usable(quote, asof_day):
    """Require a same-day, bounded-age mark before publishing daily P&L."""
    if not isinstance(quote, dict) or quote.get("quote_source") not in _TODAY_PNL_QUOTE_SOURCES:
        return False
    quote_at = str(quote.get("quote_at") or "")
    if quote_at[:10] != _date(asof_day).isoformat():
        return False
    try:
        parsed = dt.datetime.fromisoformat(quote_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        if _date(asof_day) != dt.date.today():
            return True
        age_seconds = (dt.datetime.now(dt.timezone.utc) - parsed.astimezone(dt.timezone.utc)).total_seconds()
        return -120 <= age_seconds <= 20 * 60
    except (TypeError, ValueError, OverflowError):
        return False


def _open_runup_bonus(now=None):
    """开盘追高上限的时段余量：早盘宽、随时间收紧、午后无。

    开盘价只是集合竞价单点打印，且热点主题的日一动量集中在开盘
    半小时；固定 2% 上限会把板块异动车道要抓的进场窗口全部堵死
    （2026-08-25 09:34 600737 较开盘 +5.77% 被拒的教训）。余量：
    09:30-09:45 +2.5pp、09:45-10:30 +1.0pp、其余时段 0。
    """
    now = now if isinstance(now, dt.datetime) else dt.datetime.now()
    t = now.time()
    if dt.time(9, 30) <= t <= dt.time(9, 45):
        return 0.025
    if dt.time(9, 45) < t <= dt.time(10, 30):
        return 0.010
    return 0.0


def _today_position_performance(position, price, quote, asof_day=None):
    """今日盈亏按持仓来源分段：当日买入按成交成本，隔夜仓按昨收。"""
    day = _date(asof_day).isoformat()
    # 只对当天的实时盘面应用交易时段门控；历史回看仍按历史行情计算。
    if day == dt.date.today().isoformat() and not _market_session()["today_pnl_available"]:
        return None, None, None
    if not _today_quote_is_usable(quote, asof_day):
        return None, None, None
    qty = int(_num(position.get("qty")))
    if qty <= 0 or price <= 0:
        return None, None, None
    bought_today = min(int(_num(position.get("today_acquired_qty"))), qty)
    today_cost = _num(position.get("today_acquired_cost"))
    carried_qty = qty - bought_today
    quote_pct = _num(quote.get("pct"), None)
    if carried_qty and (quote_pct is None or quote_pct <= -99.9):
        return None, None, None
    previous_close = _num(quote.get("previous_close"), 0.0) if carried_qty else 0.0
    if carried_qty and previous_close <= 0:
        previous_close = price / (1 + quote_pct / 100)
    baseline = today_cost + carried_qty * previous_close
    pnl = (price * bought_today - today_cost) + (price - previous_close) * carried_qty
    return round(pnl, 2), (round(pnl / baseline * 100, 2) if baseline else None), round(baseline, 2)


def _today_sell_performance(sells, quotes, asof_day=None):
    """Today's contribution from sold overnight shares, not lifetime trade P&L.

    A-share sellable lots are carried positions under T+1.  Their opening
    baseline is yesterday's close; using order.realized_pnl here would charge
    all gains/losses since acquisition to the sell date and double count the
    dashboard's daily movement.
    """
    day = _date(asof_day).isoformat()
    pnl = 0.0
    baseline = 0.0
    covered = 0
    missing = []
    for order in sells:
        quote = quotes.get(str(order.get("code") or "")) or {}
        if not _today_quote_is_usable(quote, asof_day):
            missing.append(str(order.get("code") or ""))
            continue
        price = _num(quote.get("price"), 0.0)
        pct = _num(quote.get("pct"), None)
        qty = int(_num(order.get("qty")))
        fill_price = _num(order.get("filled_price"), 0.0)
        if price <= 0 or pct is None or pct <= -99.9 or qty <= 0 or fill_price <= 0:
            missing.append(str(order.get("code") or ""))
            continue
        previous_close = _num(quote.get("previous_close"), 0.0)
        if previous_close <= 0:
            previous_close = price / (1 + pct / 100)
        basis = previous_close * qty
        proceeds = fill_price * qty - _num(order.get("fees"))
        pnl += proceeds - basis
        baseline += basis
        covered += 1
    return {
        "pnl": round(pnl, 2),
        "baseline": round(baseline, 2),
        "covered": covered,
        "total": len(sells),
        "missing_codes": sorted({code for code in missing if code}),
    }


def _sync_positions(conn, account_id=None, asof_day=None):
    """保留聚合表供旧接口兼容；交易结算逻辑只读取 lots。"""
    positions = _position_rows(conn, account_id, asof_day)
    if account_id:
        conn.execute("DELETE FROM paper_positions WHERE account_id=?", (account_id,))
    else:
        conn.execute("DELETE FROM paper_positions")
    for p in positions:
        conn.execute(
            """INSERT INTO paper_positions(account_id,code,name,industry,qty,cost,entry_date,available_date,asset_type,peak_price,take_stage)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (p["account_id"], p["code"], p.get("name"), p.get("industry"), p["qty"], p["cost"],
             p["entry_date"], p["available_date"], p["asset_type"], p["peak_price"], p["take_stage"]),
        )
    return positions


def _consume_available_lots(conn, account_id, code, qty, asof_day):
    """FIFO 消耗已结算股票底仓，返回真实成本；T+1 锁定份额永不被扣减。"""
    cycle = _active_cycle(conn)
    remaining = int(qty)
    cost_amount = 0.0
    lots = _rows(
        conn,
        """SELECT * FROM paper_position_lots WHERE cycle_id=? AND account_id=? AND code=?
           AND remaining_qty>0 AND available_date<=? ORDER BY acquired_at,id""",
        (cycle["id"], account_id, code, _date(asof_day).isoformat()),
    )
    # Validate the aggregate available quantity before mutating any lot.  A
    # malformed/partially-consumed lot must not leave a half-reduced ledger
    # when the requested sell quantity cannot be fully satisfied.
    available_total = sum(max(0, int(_num(lot.get("remaining_qty")))) for lot in lots)
    if available_total < remaining:
        return 0, 0.0
    for lot in lots:
        take = min(remaining, int(lot["remaining_qty"]))
        if take <= 0:
            continue
        conn.execute("UPDATE paper_position_lots SET remaining_qty=remaining_qty-? WHERE id=?", (take, lot["id"]))
        cost_amount += take * _num(lot["cost"])
        remaining -= take
        if not remaining:
            break
    return int(qty) - remaining, cost_amount


def _record_lot(conn, account, signal, qty, fill_price, asof_day, order_id=None, is_t_base=True, fees=0.0):
    cycle = _active_cycle(conn)
    asset_type = _asset_type(signal.get("code"), signal.get("name"))
    available = _date(asof_day) if asset_type == "etf_t0" else _next_weekday(asof_day)
    conn.execute(
        """INSERT INTO paper_position_lots(cycle_id,account_id,code,name,industry,qty,remaining_qty,cost,acquired_at,available_date,asset_type,source_order_id,cost_fee_included,is_t_base)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cycle["id"], account["id"], signal["code"], signal.get("name"), signal.get("industry"), int(qty), int(qty),
         fill_price + _num(fees) / max(int(qty), 1), _now(), available.isoformat(), asset_type, order_id, 1, int(bool(is_t_base))),
    )


def _latest_price_map(codes=None):
    rows = U.load_universe() or []
    wanted = set(codes or [])
    out = {}
    for row in rows:
        code = str(row.get("code") or "")
        if not wanted or code in wanted:
            out[code] = dict(row)
    # universe.json 是低频重建的股票池清单，其 price 字段可能滞后数周
    # （7/28 版本曾把全池估值压低 ~1.2 万）。叠加最近一次全市场快照缓存
    # （15:15 收盘快照 / 风控刷新维护）作为价格覆盖层，universe 只提供
    # name/industry 等静态属性与缺省价格。
    try:
        snapshot_rows = dfc.load_market_snapshot_full_cached()
    except Exception:
        snapshot_rows = []
    for row in snapshot_rows or []:
        code = str(row.get("code") or "")
        target = out.get(code)
        if target is None:
            continue
        price = row.get("price")
        if isinstance(price, (int, float)):
            # 快照缓存必然新于 universe 重建（每日 15:15 收盘快照维护），
            # 无条件覆盖价格字段；静态属性保留 universe 的值。
            merged = dict(target)
            for key in ("price", "pct", "open_price", "high", "low",
                        "prev_close", "volume", "amount", "quote_at", "quote_ts"):
                value = row.get(key)
                if isinstance(value, (int, float)) or (key == "quote_at" and value):
                    merged[key] = value
            merged["price_source"] = "market_snapshot_full"
            out[code] = merged
    return out


# universe.json 的 built_at 为低频字段，但 _quotes 每次调用都会读取；
# 按文件 mtime 缓存，避免同一轮扫描里每个账户重复读 1.86MB JSON。
_UNIVERSE_TIME_CACHE = {"sig": None, "value": None}


def _universe_snapshot_time():
    try:
        sig = (os.path.getmtime(U.UNIVERSE_PATH), os.path.getsize(U.UNIVERSE_PATH))
    except OSError:
        return None
    if _UNIVERSE_TIME_CACHE["sig"] == sig:
        return _UNIVERSE_TIME_CACHE["value"]
    value = None
    try:
        with open(U.UNIVERSE_PATH, encoding="utf-8") as handle:
            value = (_loads(handle.read(), {}) or {}).get("built_at")
    except OSError:
        value = None
    _UNIVERSE_TIME_CACHE.update({"sig": sig, "value": value})
    return value


def _previous_trade_weekday(value):
    return U.previous_trade_day(_date(value))


def _reference_date_is_fresh(reference_date, asof_date):
    """盘中允许使用当日或上一交易日的收盘因子；更早的数据一律视为过期。"""
    if not reference_date:
        return False
    try:
        reference = _date(str(reference_date)[:10])
    except (TypeError, ValueError):
        return False
    day = _date(asof_date)
    return reference in {day, _previous_trade_weekday(day)}


def _trading_weekday_lag(reference_date, asof_date):
    """返回参考日相对信号日落后的工作日数；未来日期或无效日期返回 None。"""
    if not reference_date:
        return None
    try:
        reference = _date(str(reference_date)[:10])
    except (TypeError, ValueError):
        return None
    day = _date(asof_date)
    if reference > day:
        return None
    lag = 0
    cursor = reference
    while cursor < day:
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            lag += 1
    return lag


def _strategy_reference_is_usable(account_id, reference_date, asof_date):
    spec = ACCOUNT_SPECS[account_id]
    lag = _trading_weekday_lag(reference_date, asof_date)
    return lag is not None and lag <= spec["max_factor_lag"], lag


def _quote_is_fresh(quote, asof_date):
    """只有带源时间戳的当日公开行情才可触发成交。"""
    if not quote or quote.get("quote_source") != "live":
        return False
    try:
        quote_time = dt.datetime.fromisoformat(str(quote.get("quote_at") or ""))
    except (TypeError, ValueError):
        return False
    day = _date(asof_date)
    if quote_time.date() != day:
        return False
    # 回放历史日期时只校验源日期；当日运行还要防止接口返回长时间未更新的收盘价。
    if day != dt.date.today():
        return True
    now = dt.datetime.now(quote_time.tzinfo) if quote_time.tzinfo else dt.datetime.now()
    age_seconds = (now - quote_time).total_seconds()
    return -120 <= age_seconds <= 20 * 60


def _is_trading_active(quote):
    """判断股票是否在活跃交易（非停牌）。"""
    pct = _num(quote.get("pct"), None)
    amount = _num(quote.get("amount"), 0)
    turnover = _num(quote.get("turnover"), 0)
    volume = _num(quote.get("volume"), 0)
    if pct == 0 and amount == 0 and turnover == 0 and volume == 0:
        return False
    return True


def _execution_quote_status(quote, asof_date, purpose="entry"):
    """自动成交行情门禁。

    开仓与回补必须双源核验；风险卖出允许在主行情带当日时间戳且数值有效时
    降级执行，避免备用公共接口短暂故障把止损/止盈仓位锁死。
    """
    if not quote:
        return {"fresh": False, "status": "missing", "reason": "缺少行情"}
    cross = dict(quote.get("quote_cross_check") or {})
    validation = str(quote.get("quote_validation") or "")
    diagnostics = {
        "quote_source": quote.get("quote_source"),
        "quote_validation": validation or None,
        "quote_at": quote.get("quote_at"),
        "quote_cross_check": cross,
    }
    if validation == "incomplete":
        return {"fresh": False, "status": "invalid", "reason": "主行情价格或涨跌幅无效", **diagnostics}
    if quote.get("quote_source") != "live":
        return {
            "fresh": False,
            "status": "local_cache",
            "reason": "行情来自本地缓存，禁止虚构自动成交",
            "quote_at": quote.get("quote_at"),
            **diagnostics,
        }
    if not _quote_is_fresh(quote, asof_date):
        return {
            "fresh": False,
            "status": "stale",
            "reason": "实时行情源时间戳已过期，等待下一次有效报价",
            "quote_at": quote.get("quote_at"),
            **diagnostics,
        }
    if quote.get("quote_validation") == "cross_source_checked":
        return {
        "fresh": True,
        "status": "cross_source_checked",
        "reason": "带当日源时间戳且双源核验通过的实时行情",
        "quote_at": quote.get("quote_at"),
        **diagnostics,
    }
    if validation == "cross_source_failed":
        detail = cross.get("failure_reason") or "独立行情源返回结果与主行情不一致"
        return {"fresh": False, "status": "cross_source_failed", "reason": detail, **diagnostics}
    if validation == "cross_source_unavailable":
        detail = cross.get("failure_reason") or "本次未获得独立行情源的有效返回"
        if purpose == "exit":
            return {"fresh": True, "status": "degraded_cross_source", "reason": detail + "；仅允许风控退出", "degraded": True, **diagnostics}
        # 自动开仓、回补和确认加仓必须与最初信号使用同一双源门槛。
        # 单源降级只适用于风险退出，不能在待买信号生成后绕过复核。
        return {"fresh": False, "status": "cross_source_unavailable", "reason": detail + "；自动买入等待双源恢复", **diagnostics}
    if validation == "range_timestamp_checked" and purpose == "exit":
        return {"fresh": True, "status": "degraded_cross_source", "reason": "主行情新鲜有效，但未完成独立行情源校验；仅允许风控退出", "degraded": True, **diagnostics}
    return {"fresh": False, "status": "unverified", "reason": "未获得通过的独立行情源校验结果", **diagnostics}
def _market_state(asof_date, live_universe=None, *, allow_network=True):
    """实时指数决定当日门控；历史日线只提供中期趋势，不冒充盘中行情。"""
    # 盘中市场宽度必须使用同一轮全市场实时快照，不能读取可能滞后数日的
    # universe.json。外部源暂时不可用时保守地返回未知，而不伪装成实时数据。
    if live_universe is None and allow_network:
        try:
            live_universe = dfc.fetch_market_snapshot_full(max_age=240)
        except Exception:
            live_universe = []
    elif live_universe is None:
        live_universe = []
    latest = {str(row.get("code")): row for row in (live_universe or []) if row.get("code")}
    breadth_snapshot_at = max(
        (str(row.get("quote_at")) for row in latest.values() if row.get("quote_at")),
        default=None,
    )
    breadth_is_current = str(breadth_snapshot_at or "")[:10] == _date(asof_date).isoformat()
    priced = [r for r in latest.values() if isinstance(r.get("pct"), (int, float))] if breadth_is_current else []
    up = sum(1 for row in priced if row["pct"] > 0) if breadth_is_current else None
    down = sum(1 for row in priced if row["pct"] < 0) if breadth_is_current else None
    breadth = up / max(up + down, 1) if breadth_is_current and up is not None and down is not None else None
    bench = dfc.load_cached_kline("BENCH_000300")
    stale = True
    benchmark_history_ready = False
    above_ma20 = None
    bench_5d = None
    last_date = None
    if bench is not None and len(bench) >= 21:
        benchmark_history_ready = True
        last_date = str(pd.Timestamp(bench.index[-1]).date())
        stale = not _reference_date_is_fresh(last_date, asof_date)
        close = _num(bench["close"].iloc[-1], 0)
        above_ma20 = close > _num(bench["close"].tail(20).mean(), close)
        bench_5d = (close / _num(bench["close"].iloc[-6], close) - 1) * 100 if len(bench) >= 6 else None
    live_index = None
    if allow_network:
        try:
            live_index = next(
                (
                    row for row in dfc.fetch_indices()
                    if row.get("code") == "sh000300"
                    and str(row.get("time") or "")[:8] == _date(asof_date).strftime("%Y%m%d")
                ),
                None,
            )
        except Exception:
            live_index = None
        try:
            overseas = F.overseas_risk_gate()
        except Exception:
            overseas = {"light": "unknown", "advice": "海外数据不可用"}
    else:
        live_index = None
        overseas = {"light": "unknown", "advice": "外部数据在账本事务中不可用"}
    live_pct = _num((live_index or {}).get("pct"), None)
    if stale and not benchmark_history_ready and live_pct is not None:
        # 盘中历史基准可能暂时只有单日快照（例如基准缓存刚完成补齐）。
        # 不能因此把有效的实时指数判成 unknown；使用实时涨跌执行保守黄灯，
        # 等至少 21 根完整日线恢复后再启用 MA20/5日趋势判断。
        light = "red" if overseas.get("light") == "red" or live_pct <= -2.0 else "yellow"
        reason = f"实时沪深300 {live_pct:+.2f}%；中期趋势日线不足，按谨慎仓位执行"
    elif stale and live_pct is not None:
        # 盘中最后一根完整日线落后一个交易日是正常现象；只要实时指数
        # 有当日时间戳，就按保守黄灯运行，不能把“趋势待更新”误判成未知
        # 并暂停四套策略全部新开仓。
        light = "red" if overseas.get("light") == "red" or live_pct <= -2.0 else "yellow"
        reason = f"实时沪深300 {live_pct:+.2f}%；中期趋势收盘数据待更新，按谨慎仓位执行"
    elif stale:
        light, reason = "unknown", "沪深300收盘趋势数据早于上一交易日且缺少当日实时指数"
    elif live_pct is None:
        light, reason = "unknown", "未取得当日沪深300实时行情"
    elif overseas.get("light") == "red" or live_pct <= -2.0:
        light, reason = "red", f"实时沪深300 {live_pct:+.2f}% 或海外风险偏弱"
    elif live_pct <= -0.6 or (not above_ma20 and (bench_5d or 0) <= -3):
        light, reason = "yellow", f"实时沪深300 {live_pct:+.2f}%，中期趋势偏谨慎"
    elif overseas.get("light") in ("yellow", "unknown") or not above_ma20:
        light, reason = "yellow", f"实时沪深300 {live_pct:+.2f}%，按谨慎仓位执行"
    else:
        light, reason = "green", f"实时沪深300 {live_pct:+.2f}%，指数趋势正常"
    # 北向资金（P1）：同花顺分钟累计净流入。仅作情绪上下文展示与审计，
    # 不改变 light 门控——北向数据的当日语义（是否含买断额度）仍不稳。
    northbound = None
    if AD is not None:
        try:
            northbound = AD.northbound_realtime()
        except Exception:
            northbound = None
    nb_reason = ""
    if isinstance(northbound, dict) and isinstance(northbound.get("net_yi"), (int, float)):
        direction = "净流入" if northbound["net_yi"] > 0 else "净流出"
        nb_reason = f"；北向{direction} {abs(northbound['net_yi']):.1f}亿"
    return {
        "light": light, "reason": reason + nb_reason, "breadth_up_pct": round(breadth * 100, 1) if breadth is not None else None,
        "up": up, "down": down, "benchmark_above_ma20": above_ma20,
        "benchmark_5d_pct": round(bench_5d, 2) if bench_5d is not None else None,
        "data_date": last_date, "overseas": overseas,
        "northbound": northbound,
        "live_index_pct": round(live_pct, 2) if live_pct is not None else None,
        "live_index_price": _num((live_index or {}).get("price"), None),
        "live_index_time": (live_index or {}).get("time"),
        "market_data_source": "腾讯实时指数+本地前复权趋势",
        "breadth_source": f"全市场实时快照 {breadth_snapshot_at or '未知'}（有效样本 {len(priced)}）",
        "market_snapshot_count": len(latest),
        "market_snapshot_valid_count": len(priced),
        "market_snapshot_source": "东方财富全市场实时行情缓存",
    }


# 全市场因子 CSV（1.6MB）在同一轮扫描里会被 _bootstrap_signals_for_today
# 外层与每个账户的 _candidate_rows 重复读取（4 账户 = 5 次/轮）。文件由
# 收盘重建任务更新，按 (mtime, size) 缓存即可安全复用。
_SELECTION_INPUTS_CACHE = {"sig": None, "value": None}


def _selection_inputs():
    def _sig():
        try:
            return (
                os.path.getmtime(SELECTION_FACTORS_PATH),
                os.path.getsize(SELECTION_FACTORS_PATH),
                os.path.getmtime(SELECTION_META_PATH) if os.path.exists(SELECTION_META_PATH) else None,
            )
        except OSError:
            return None
    sig = _sig()
    if sig is not None and _SELECTION_INPUTS_CACHE["sig"] == sig:
        return _SELECTION_INPUTS_CACHE["value"]
    if not os.path.exists(SELECTION_FACTORS_PATH):
        raise RuntimeError("未找到已持久化的选股因子，请先完成全市场数据初始化")
    price_f = pd.read_csv(SELECTION_FACTORS_PATH, dtype={"code": str}, index_col="code")
    if os.path.exists(SELECTION_META_PATH):
        with open(SELECTION_META_PATH, encoding="utf-8") as handle:
            meta = _loads(handle.read(), {})
    else:
        meta = {}
    value = (price_f, set(meta.get("first_board_codes", [])))
    if sig is not None:
        _SELECTION_INPUTS_CACHE.update({"sig": sig, "value": value})
    return value


def _selection_factor_cache_meta():
    """Read factor-cache provenance without allowing a broken sidecar to pass."""
    try:
        with open(SELECTION_META_PATH, encoding="utf-8") as handle:
            value = _loads(handle.read(), {})
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _selection_factor_manifest_signature():
    """Return the history-manifest identity used to build the factor cache."""
    try:
        return [
            os.path.getmtime(dfc.KLINE_MANIFEST_PATH),
            os.path.getsize(dfc.KLINE_MANIFEST_PATH),
            dfc.SHARED_KLINE_SOURCE_VERSION,
        ]
    except (OSError, AttributeError):
        return None


def _selection_factor_freshness(price_f, universe, asof_date, meta=None):
    """Require a complete, exact-date factor snapshot before candidate ranking.

    A cache containing a few fresh rows plus thousands of old rows is not a
    valid full-market snapshot.  We therefore check the sidecar provenance,
    require every retained factor row to carry the target complete trade day,
    and independently require the eligible A-share universe coverage.
    """
    try:
        target = U.latest_complete_trade_date(_date(asof_date))
        target_text = target.isoformat()
    except (TypeError, ValueError, RuntimeError):
        return {"passed": False, "reason": "无法确定最近完整交易日"}
    meta = meta if isinstance(meta, dict) else _selection_factor_cache_meta()
    factor_dates = price_f["last_date"].astype(str).str[:10] if "last_date" in price_f else pd.Series(dtype=str)
    eligible_codes = {
        str(row.get("code") or "")
        for row in (universe or [])
        if str(row.get("code") or "")
        and _security_scope(row.get("code"), row.get("name"), row.get("risk_flag"))["allowed"]
    }
    exact_mask = factor_dates.eq(target_text)
    factor_rows = int(len(price_f))
    exact_rows = int(exact_mask.sum())
    exact_eligible = len(set(price_f.index.astype(str)[exact_mask.to_numpy()]) & eligible_codes)
    eligible_coverage = exact_eligible / max(len(eligible_codes), 1)
    cached_date = str(meta.get("factor_date") or "")[:10]
    current_signature = _selection_factor_manifest_signature()
    cached_signature = meta.get("signature")
    signature_ok = bool(current_signature and isinstance(cached_signature, list)
                        and cached_signature == current_signature)
    try:
        built_at = dt.datetime.fromisoformat(str(meta.get("built_at")).replace("Z", "+00:00"))
        if built_at.tzinfo is None:
            built_at = built_at.replace(tzinfo=dt.timezone.utc)
        built_age = max(0.0, (dt.datetime.now(dt.timezone.utc) - built_at.astimezone(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        built_age = None
    age_ok = built_age is not None and built_age <= SELECTION_FACTOR_MAX_CACHE_AGE_SECONDS
    # 周末/节假日不应把上一完整交易日的因子按自然小时过期；只在
    # 文件本身单一日期、覆盖达标且不超过四个自然日时降级保留。
    calendar_age_ok = built_age is not None and built_age <= 4 * 86400
    # A provider can finish writing the K-line manifest before the compact
    # factor rebuild completes.  Do not stop every automatic scan in that
    # narrow window: a single completed-trading-day lag is safe for ranking
    # when the retained factor snapshot itself is complete and recent.  The
    # fallback is explicitly marked degraded and never changes hard risk or
    # execution gates.
    cached_lag = _trading_weekday_lag(cached_date, target_text) if cached_date else None
    fallback_mask = factor_dates.eq(cached_date) if cached_date and cached_lag == 1 else exact_mask
    fallback_rows = int(fallback_mask.sum())
    fallback_eligible = len(set(price_f.index.astype(str)[fallback_mask.to_numpy()]) & eligible_codes)
    fallback_coverage = fallback_eligible / max(len(eligible_codes), 1)
    degraded_factor_fallback = bool(
        cached_lag == 1
        and (exact_eligible < len(eligible_codes) * CANDIDATE_FACTOR_MIN_COVERAGE)
        and factor_rows >= CANDIDATE_FACTOR_MIN_ROWS
        and fallback_rows == factor_rows
        and fallback_coverage >= CANDIDATE_FACTOR_MIN_COVERAGE
        and age_ok
    )
    calendar_degraded_fallback = bool(
        factor_rows >= CANDIDATE_FACTOR_MIN_ROWS
        and exact_rows == factor_rows
        and cached_date == target_text
        and eligible_coverage >= CANDIDATE_FACTOR_MIN_COVERAGE
        and calendar_age_ok
        and not signature_ok
    )
    # 旁车元数据与行情 manifest 的 mtime 可能因盘后增量重试而变化；
    # 只要 CSV 是单一已知日期且 sidecar 覆盖达标，允许继续用该完整快照。
    sidecar_degraded_fallback = bool(
        cached_date
        and factor_dates.nunique(dropna=True) == 1
        and str(factor_dates.dropna().iloc[0])[:10] == cached_date
        and int(meta.get("factor_rows") or 0) >= CANDIDATE_FACTOR_MIN_ROWS
        and float(meta.get("eligible_factor_coverage_pct") or 0.0) >= CANDIDATE_FACTOR_MIN_COVERAGE * 100
        and calendar_age_ok
        and _date(cached_date) <= _date(target_text)
    )
    passed = bool(
        factor_rows >= CANDIDATE_FACTOR_MIN_ROWS
        and exact_rows == factor_rows
        and cached_date == target_text
        and signature_ok
        and age_ok
        and eligible_codes
        and eligible_coverage >= CANDIDATE_FACTOR_MIN_COVERAGE
    ) or degraded_factor_fallback or calendar_degraded_fallback or sidecar_degraded_fallback
    return {
        "passed": passed, "target_date": target_text, "factor_date": cached_date,
        "factor_rows": factor_rows, "exact_date_rows": exact_rows,
        "eligible_factor_rows": exact_eligible, "eligible_universe_rows": len(eligible_codes),
        "eligible_factor_coverage_pct": round(eligible_coverage * 100, 2),
        "manifest_signature_ok": signature_ok,
        "built_age_seconds": built_age, "max_cache_age_seconds": SELECTION_FACTOR_MAX_CACHE_AGE_SECONDS,
        "degraded_fallback": bool(degraded_factor_fallback or calendar_degraded_fallback or sidecar_degraded_fallback),
        "fallback_factor_date": cached_date if (degraded_factor_fallback or calendar_degraded_fallback or sidecar_degraded_fallback) else None,
        "fallback_coverage_pct": round(fallback_coverage * 100, 2),
        "reason": ("使用上一完整交易日因子快照，等待当日因子重建" if (degraded_factor_fallback or calendar_degraded_fallback or sidecar_degraded_fallback) else (None if passed else "历史因子未形成最近完整交易日的全市场快照")),
    }


def _selection_factor_history_gate(universe, cutoff):
    """Return a non-mutating complete-day coverage gate for factor rebuilds."""
    target = _date(cutoff)
    # 覆盖率分母必须与可买范围一致；科创/北交/ST等研究背景标的不能
    # 让主板/创业板的完整因子重建被无关缺失数据拖成“假完整”。
    codes = {
        str(row.get("code") or "")
        for row in (universe or [])
        if str(row.get("code") or "")
        and _security_scope(row.get("code"), row.get("name"), row.get("risk_flag"))["allowed"]
    }
    manifest = _history_manifest()
    covered = 0
    for code in codes:
        try:
            last_day = _date((manifest.get(code) or {}).get("last_date"))
        except (TypeError, ValueError):
            continue
        # 只接受目标完整交易日，不能用未来日或旧日近似覆盖。
        if last_day == target:
            covered += 1
    required = len(codes)
    minimum = int(required * SELECTION_FACTOR_MIN_COVERAGE + 0.999999)
    return {
        "target_date": target.isoformat(),
        "target_rows": covered,
        "required_rows": required,
        "minimum_rows": minimum,
        "coverage_pct": round(covered / required * 100, 2) if required else 0.0,
        "scope": "仅沪深主板和创业板可交易范围",
        "passed": bool(required and covered >= minimum),
    }

def _rebuild_selection_factor_cache(asof_date=None):
    """在收盘历史库更新成功后重建盘中读取的紧凑因子文件。"""
    cutoff = _date(asof_date) if asof_date is not None else None
    universe = U.load_universe()
    if cutoff is not None:
        gate = _selection_factor_history_gate(universe, cutoff)
        if not gate["passed"]:
            return {
                "status": "blocked",
                "reason": "完整日线覆盖不足，保留上一有效因子版本",
                "refresh_gate": gate,
            }
    klines = {}
    for row in universe:
        code = str(row.get("code") or "")
        frame = dfc.load_shared_kline(code)
        if frame is not None and cutoff is not None:
            # 数据源可能提前暴露未收盘日线；因子只允许使用完整交易日。
            frame = frame.loc[frame.index.date <= cutoff]
        if frame is not None and len(frame) > 65:
            klines[code] = frame
    price_f = F.compute_price_factors(klines)
    eligible_codes = {
        str(row.get("code") or "")
        for row in (universe or [])
        if str(row.get("code") or "")
        and _security_scope(row.get("code"), row.get("name"), row.get("risk_flag"))["allowed"]
    }
    built_codes = set(price_f.index.astype(str)) if not price_f.empty else set()
    date_by_code = dict(zip(price_f.index.astype(str), price_f["last_date"].astype(str))) if "last_date" in price_f else {}
    exact_codes = {
        code for code in built_codes
        if str(date_by_code.get(code) or "")[:10] == cutoff.isoformat()
    } if cutoff is not None else set()
    factor_coverage = len(exact_codes & eligible_codes) / max(len(eligible_codes), 1)
    try:
        signature = [
            os.path.getmtime(dfc.KLINE_MANIFEST_PATH),
            os.path.getsize(dfc.KLINE_MANIFEST_PATH),
            dfc.SHARED_KLINE_SOURCE_VERSION,
        ]
    except OSError:
        signature = [0, 0]
    if (
        cutoff is not None
        and (
            len(price_f) < CANDIDATE_FACTOR_MIN_ROWS
            or factor_coverage < SELECTION_FACTOR_MIN_COVERAGE
        )
    ):
        return {
            "status": "blocked",
            "reason": "重建后的完整交易日因子覆盖不足，保留上一有效因子版本",
            "factor_rows": len(price_f),
            "eligible_universe_rows": len(eligible_codes),
            "eligible_factor_rows": len(exact_codes & eligible_codes),
            "coverage_pct": round(factor_coverage * 100, 2),
            "required_coverage_pct": SELECTION_FACTOR_MIN_COVERAGE * 100,
            "kline_manifest_hash": hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16],
                "factor_date": cutoff.isoformat() if cutoff is not None else None,
        }
    # Do not persist a mixed-date factor file.  Old rows are excluded from the
    # cache itself; a later selector cannot silently fall back to them.
    if cutoff is not None:
        price_f = price_f.loc[price_f.index.astype(str).isin(exact_codes)].copy()
    first_board_codes = set(F.find_first_board_candidates(klines))
    factors_tmp = SELECTION_FACTORS_PATH + ".tmp"
    price_f.to_csv(factors_tmp, encoding="utf-8")
    os.replace(factors_tmp, SELECTION_FACTORS_PATH)
    meta_tmp = SELECTION_META_PATH + ".tmp"
    with open(meta_tmp, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "signature": signature,
                # Aware UTC timestamp: readers normalize naive values to UTC,
                # which made a Shanghai-local built_at appear 8h fresher than
                # reality and permanently defeated the cache-age gates.
                "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "factor_rows": len(price_f),
                "eligible_universe_rows": len(eligible_codes),
                "eligible_factor_rows": len(exact_codes & eligible_codes),
                "eligible_factor_coverage_pct": round(factor_coverage * 100, 2),
                "factor_date": cutoff.isoformat() if cutoff is not None else None,
                "first_board_codes": sorted(first_board_codes),
            },
            handle,
            ensure_ascii=False,
        )
    os.replace(meta_tmp, SELECTION_META_PATH)
    latest = None
    if "last_date" in price_f and not price_f.empty:
        latest = str(price_f["last_date"].dropna().max())[:10]
    return {"status": "ok", "factor_rows": len(price_f), "factor_date": latest}


def _history_manifest():
    # 复用 data_fetcher 的 manifest 内存缓存（按 mtime 失效），避免每轮
    # 扫描/每只候选重复读 893KB 的 kline_manifest.json。
    try:
        return dfc.load_kline_manifest() or {}
    except (OSError, ValueError, TypeError):
        return {}


def _completed_kline(code, cutoff_date, *, inclusive=True):
    """只返回截止指定完整交易日的日线，排除当天尚未收盘的半根 K 线。"""
    frame = dfc.load_cached_kline(str(code))
    if frame is None or frame.empty:
        return frame
    cutoff = _date(cutoff_date)
    try:
        mask = frame.index.date <= cutoff if inclusive else frame.index.date < cutoff
        return frame.loc[mask].copy()
    except Exception:
        return frame


def _new_listing_profile(account_id, code, kline, history_meta, asof_date):
    """Classify a genuine recent listing without treating a cache gap as one.

    A short series is eligible only when its first K-line is recent, consecutive
    coverage is plausible, and it falls within the strategy-specific observation
    window.  Missing/old/corrupt history deliberately remains the normal 120-bar
    rejection path.
    """
    policy = NEW_LISTING_POLICIES.get(str(account_id))
    if not policy or kline is None or len(kline) <= 0:
        return {"eligible": False, "reason": "无可用新股日线"}
    rows = int(len(kline))
    if rows < int(policy["min_rows"]) or rows > int(policy["max_rows"]):
        return {"eligible": False, "reason": "日线数量不在新股观察窗口", "rows": rows}
    try:
        first_date = _date(kline.index.min())
        last_date = _date(kline.index.max())
    except Exception:
        return {"eligible": False, "reason": "新股日线日期无法识别", "rows": rows}
    day = _date(asof_date)
    age_days = (day - first_date).days
    if first_date > day or age_days < 0 or age_days > NEW_LISTING_MAX_CALENDAR_DAYS:
        return {"eligible": False, "reason": "日线起点不符合近期上市特征", "rows": rows}
    expected_sessions = max(1, sum(
        1 for offset in range(age_days + 1)
        if (first_date + dt.timedelta(days=offset)).weekday() < 5
    ))
    coverage = rows / expected_sessions
    if coverage < NEW_LISTING_MIN_COVERAGE:
        return {
            "eligible": False, "reason": "新股日线覆盖不足，疑似缓存缺失或停牌",
            "rows": rows, "coverage": round(coverage, 3),
        }
    meta = history_meta or {}
    meta_rows = int(_num(meta.get("rows"), rows) or rows)
    if meta_rows and abs(meta_rows - rows) > 2:
        return {"eligible": False, "reason": "日线清单与缓存不一致", "rows": rows}
    return {
        "eligible": True,
        "rows": rows,
        "first_date": first_date.isoformat(),
        "last_date": last_date.isoformat(),
        "listing_age_days": age_days,
        "coverage": round(coverage, 3),
        "policy": dict(policy),
    }


def _new_listing_liquidity_context(kline, quote, policy):
    """Return a scale-aware liquidity gate for a recent listing.

    Listing size is not a reason to require the same RMB turnover as a large
    mature stock.  The comparable evidence is the stock's own recent turnover;
    an absolute floor only prevents a thin, untradeable quote from being treated
    as liquid when the short history is incomplete.
    """
    amount = _num(quote.get("amount"), _num(quote.get("turnover_amount")))
    history_amount = None
    history_column = None
    if kline is not None and not kline.empty:
        for column in ("amount", "turnover_amount", "成交额"):
            if column not in kline.columns:
                continue
            values = pd.to_numeric(kline[column], errors="coerce").dropna()
            values = values[values > 0]
            if not values.empty:
                lookback = max(3, int(policy.get("liquidity_lookback", 5)))
                history_amount = float(values.tail(lookback).median())
                history_column = column
                break
    floor = max(0.0, _num(policy.get("liquidity_floor")))
    cap = max(floor, _num(policy.get("liquidity_cap"), floor))
    ratio = max(0.10, min(1.00, _num(policy.get("liquidity_history_ratio"), 0.4)))
    source = "绝对底线"
    threshold = floor
    if history_amount and history_amount > 0:
        threshold = min(cap, max(floor, history_amount * ratio))
        source = f"近{max(3, int(policy.get('liquidity_lookback', 5)))}日成交额中位数的 {ratio:.0%}"
    relative_ratio = amount / history_amount if amount > 0 and history_amount else None
    score = min(1.0, amount / max(threshold * 1.25, 1.0)) if amount > 0 else 0.0
    return {
        "amount": round(amount, 2),
        "threshold": round(threshold, 2),
        "floor": round(floor, 2),
        "cap": round(cap, 2),
        "history_median": round(history_amount, 2) if history_amount else None,
        "history_column": history_column,
        "relative_ratio": round(relative_ratio, 3) if relative_ratio is not None else None,
        "source": source,
        "score": round(score, 3),
    }


def _new_listing_entry_assessment(account, pick, quote, kline, listing, market=None):
    """New-listing entry guard: a small, liquid observation position only.

    It supplements, rather than replaces, quote freshness, security scope,
    negative-news and market gates.  It intentionally does not infer a trend
    from unavailable long-term averages.
    """
    policy = dict(listing.get("policy") or NEW_LISTING_POLICIES[account["id"]])
    pct = _num(quote.get("pct"), -999.0)
    main_pct = _num(quote.get("main_pct"))
    vol_ratio = _num(quote.get("vol_ratio"))
    price = _num(quote.get("price"))
    rows = int(listing.get("rows") or 0)
    liquidity = _new_listing_liquidity_context(kline, quote, policy)
    amount = _num(liquidity.get("amount"))
    reasons = []
    checks = []

    def add(name, value, detail, weight):
        checks.append({"name": name, "score": round(_clip01(value), 3), "weight": weight, "detail": detail})

    if price <= 0:
        reasons.append("新股缺少有效实时价格")
    if pct <= -4.0:
        reasons.append(f"新股当日跌幅 {pct:+.2f}% 过大，不做抄底")
    if pct > float(policy["max_intraday_pct"]):
        reasons.append(f"新股当日涨幅 {pct:+.2f}% 过高，仅观察不追入")
    if main_pct < float(policy["min_main_pct"]):
        reasons.append(f"新股主力净流入 {main_pct:+.2f}% 未达观察线 {float(policy['min_main_pct']):+.2f}%")
    if vol_ratio and vol_ratio < float(policy["min_vol_ratio"]):
        reasons.append(f"新股量比 {vol_ratio:.2f} 未达观察线 {float(policy['min_vol_ratio']):.2f}")
    if amount > 0 and amount < _num(liquidity.get("threshold")):
        detail = f"新股成交额 ¥{amount:,.0f} 未达动态流动性线 ¥{_num(liquidity.get('threshold')):,.0f}"
        if liquidity.get("history_median"):
            detail += f"（近{int(policy.get('liquidity_lookback', 5))}日中位 ¥{_num(liquidity.get('history_median')):,.0f}，{liquidity.get('source')}）"
        else:
            detail += f"（历史成交额不足，采用基础底线）"
        reasons.append(detail)
    if amount <= 0:
        reasons.append("新股缺少成交额，无法评估流动性")
    try:
        closes = pd.to_numeric(kline["close"], errors="coerce").dropna()
        recent_range = (closes.max() / max(closes.min(), 0.01) - 1) * 100 if len(closes) else None
    except Exception:
        recent_range = None
    if recent_range is None:
        reasons.append("新股日线无有效收盘价")
    elif recent_range > float(policy["max_recent_range"]):
        reasons.append(f"新股上市后波动 {recent_range:.1f}% 过大，等待稳定")

    add("上市后样本", min(rows / max(float(policy["min_rows"]), 1), 1.0),
        f"已积累 {rows} 根日线，上市约 {listing.get('listing_age_days')} 天", 0.22)
    add("实时资金", (main_pct + 2.0) / 5.0,
        f"主力净流入占比 {main_pct:+.2f}%", 0.24)
    add("相对流动性", _num(liquidity.get("score")),
        f"成交额 ¥{amount:,.0f}；动态线 ¥{_num(liquidity.get('threshold')):,.0f}；"
        f"量比 {vol_ratio:.2f}；{liquidity.get('source')}", 0.28)
    add("波动稳定", 1 - max((recent_range or 0) - 8.0, 0) / max(float(policy["max_recent_range"]) - 8.0, 1),
        f"上市后区间波动 {recent_range:.1f}%" if recent_range is not None else "区间波动未知", 0.26)
    weight_total = sum(item["weight"] for item in checks) or 1.0
    score = sum(item["score"] * item["weight"] for item in checks) / weight_total
    # 新股越早、波动越大或市场越弱，要求越高；样本与流动性成熟后门槛回落。
    threshold = _num(policy.get("base_score_threshold"), 0.61)
    min_rows = max(1, int(policy.get("min_rows", 20)))
    if rows < min_rows * 1.5:
        threshold += 0.025
    if recent_range is not None and recent_range > float(policy["max_recent_range"]) * 0.75:
        threshold += 0.025
    market_light = str((market or {}).get("light") or "").lower()
    if market_light == "yellow":
        threshold += 0.015
    elif market_light == "red":
        threshold += 0.035
    if main_pct >= max(1.5, float(policy["min_main_pct"]) + 1.5) and (vol_ratio or 0) >= float(policy["min_vol_ratio"]) * 1.35:
        threshold -= 0.02
    threshold = max(0.56, min(0.72, threshold))
    passed = not reasons and score >= threshold
    if score < threshold:
        reasons.append(f"新股观察评分 {score:.2f} 未达当日动态门槛 {threshold:.2f}")
    return {
        "passed": passed,
        "score": round(score, 3),
        "threshold": round(threshold, 3),
        "checks": checks,
        "reasons": reasons,
        "liquidity": liquidity,
        "position_scale": float(policy["scale"]),
        "execution_mode": "新股观察试仓",
    }


def _sector_key(value):
    text = str(value or "").strip()
    for suffix in ("行业", "Ⅱ", "Ⅲ", "II", "III", "概念"):
        text = text.replace(suffix, "")
    return text


SECTOR_SURGE_LANE_VERSION = "sector-surge-lane-v1"


def _sector_surge_lane_candidates(sector_heat, universe, live_map,
                                  seen_codes=None, market=None,
                                  max_total=4, per_sector=2):
    """盘中板块异动直通车道（仅 sector_rotation 候选层）。

    背景：候选池锚定昨夜因子排名，主题在盘中引爆时（如 2026-08-20 白银
    首日 +6%~7%）成员股因 mom5/mom20 还停留在昨日平淡数据而排不进
    top-N，直到第 3 日才被看到——此时只剩追高或踏空。本车道在板块
    热度榜前 5 且板块涨幅 ≥2% 时，把该板块实时主力资金最强的成员直接
    注入候选。注入候选仍需通过全部执行闸门（Q 级、追高上限、入场
    模型、单票风险、双源校验），不是绕过风控的直通车。
    """
    seen_codes = set(seen_codes or ())
    surge = [
        heat for heat in (sector_heat or {}).values()
        if int(_num(heat.get("rank"), 999)) <= 5 and _num(heat.get("pct")) >= 2.0
    ]
    if not surge:
        return [], None
    # 双确认：板块异动需同花顺题材命中才注入（2026-08-27）
    # 单独板块异动易被大盘普涨稀释，单独题材命中可能在弱板块中；两者叠加才注入。
    # AD 缺失时退化为单确认（fail-open）；AD 可用但题材为空时不注入（fail-closed）。
    ad_available = AD is not None
    ths_hot_codes = set()
    if ad_available:
        try:
            ths_rows = AD.ths_hot_reason() or []
            for _r in ths_rows:
                _c = str(_r.get("code") or "")
                if _c:
                    ths_hot_codes.add(_c)
        except Exception:
            ths_hot_codes = set()
    if ad_available:
        code_to_sector = {str(r.get("code") or ""): _sector_key(str(r.get("industry") or "")) for r in (universe or [])}
        hot_sector_keys = {code_to_sector.get(c) for c in ths_hot_codes if code_to_sector.get(c)}
        hot_sector_keys.discard("")
        confirmed = [h for h in surge if _sector_key(h.get("name") or "") in hot_sector_keys]
        if confirmed:
            surge = confirmed
        else:
            # 无题材确认：返回空并带审计说明，不注入
            return [], {"version": SECTOR_SURGE_LANE_VERSION, "trigger": "板块热度 rank≤5 且 pct≥2.0% (题材未命中)",
                        "sectors": [h.get("name") for h in surge], "injected": [], "theme_hit": False,
                        "note": "板块异动但同花顺题材未命中，双确认不注入（等待题材确认）"}
    heat_by_key = {}
    for heat in surge:
        heat_by_key[_sector_key(heat.get("name") or "")] = heat
    members = []
    for row in universe or []:
        code = str(row.get("code") or "")
        if not code or code in seen_codes:
            continue
        if row.get("risk_flag") or "ST" in str(row.get("name") or "").upper() or "退" in str(row.get("name") or ""):
            continue
        key = _sector_key(str(row.get("industry") or ""))
        if key not in heat_by_key:
            continue
        live = live_map.get(code) or {}
        price = _num(live.get("price"), _num(row.get("price")))
        pct = _num(live.get("pct"), None)
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # 已涨停附近/下跌的成员不注入；0.5% 以下尚未启动也不占车道。
        if not isinstance(pct, (int, float)) or pct <= 0.5 or pct >= 9.0:
            continue
        main_pct = _num(live.get("main_pct"), 0.0)
        vol_ratio = _num(live.get("vol_ratio"), 0.0)
        members.append((main_pct, vol_ratio, code, row, live, price, pct, key))
    if not members:
        return [], None
    members.sort(key=lambda item: (item[0], item[1]), reverse=True)
    lane, per_count, meta_rows = [], {}, []
    for main_pct, vol_ratio, code, row, live, price, pct, key in members:
        if per_count.get(key, 0) >= per_sector or len(lane) >= max_total:
            continue
        heat = heat_by_key[key]
        # 合成评分只用真实主力资金证据，且硬顶 0.74：替补评分合成中
        # rank_score 占 20% 权重，若车道评分达到 0.75+ 会凭空武装紧急
        # 择强换仓（门槛 75 分）。车道候选未经完整模型排名，不允许驱动
        # 强制换仓——只能凭真实入场评估通过后正常入场。
        lane_score = min(0.74, 0.55 + max(main_pct, 0.0) / 25.0)
        lane.append({
            "code": code, "name": row.get("name"), "industry": row.get("industry"),
            "price": price, "pct": pct,
            "score": round(lane_score, 3),
            "entry_path": "sector_surge",
            "sector_heat": heat,
            "sector_threshold_context": _sector_entry_threshold_context(
                sector_heat, market, heat),
            "candidate_status": "sector_surge_lane",
            "surge_lane_context": {
                "version": SECTOR_SURGE_LANE_VERSION,
                "main_pct": main_pct, "vol_ratio": vol_ratio,
                "reason": "盘中板块异动直通：热度前5且板块涨幅≥2%，按实时主力资金排名注入",
            },
        })
        per_count[key] = per_count.get(key, 0) + 1
        meta_rows.append({"code": code, "name": row.get("name"),
                          "sector": heat.get("name"), "main_pct": main_pct, "pct": pct})
    meta = None
    if lane:
        meta = {
            "version": SECTOR_SURGE_LANE_VERSION,
            "trigger": "板块热度 rank≤5 且板块涨幅≥2.0% + 同花顺题材双确认",
            "sectors": [h.get("name") for h in surge],
            "injected": meta_rows,
            "theme_hit": True,
            "note": "双确认注入：板块异动且题材命中；候选仍走全部执行闸门",
        }
    return lane, meta


CONCEPT_EXPANSION_LANE_VERSION = "concept-expansion-lane-v3"


def _concept_reverse_map_leaders(universe, live_map, max_leaders=3):
    """Return a bounded set of live leaders for reverse concept discovery.

    This is discovery-only.  It deliberately excludes the leader from the
    later peer lane, so a limit-up stock cannot turn its own tag into a chase
    order.  The high threshold also prevents a broad market of merely green
    names from fanning out into dozens of concept requests.
    """
    base_by_code = {str(row.get("code") or ""): row for row in (universe or [])}
    leaders = []
    for code, live in (live_map or {}).items():
        base = base_by_code.get(str(code)) or {}
        scope = _security_scope(code, live.get("name") or base.get("name"), base.get("risk_flag"))
        if not scope["allowed"]:
            continue
        pct = _num(live.get("pct"), -999.0)
        if (pct < 8.5 or pct > 19.5 or _num(live.get("price"), 0.0) <= 0
                or _num(live.get("main_net"), 0.0) <= 0
                or _num(live.get("vol_ratio"), 0.0) < 0.8):
            continue
        leaders.append({
            "code": str(code), "name": live.get("name") or base.get("name"),
            "pct": pct, "price": _num(live.get("price"), 0.0),
            "main_net": _num(live.get("main_net"), 0.0),
            "main_pct": _num(live.get("main_pct"), 0.0),
            "vol_ratio": _num(live.get("vol_ratio"), 0.0),
        })
    leaders.sort(key=lambda row: (row["pct"], row["main_net"], row["vol_ratio"]), reverse=True)
    return leaders[:max(1, min(int(max_leaders), 3))]


def _concept_expansion_lane_candidates(concepts, universe, live_map,
                                       seen_codes=None, market=None,
                                       asof_date=None, max_total=6, per_concept=2):
    """Expand strong concepts into fresh, funded, not-yet-limit-up peers."""
    seen_codes = set(seen_codes or ())
    universe_by_code = {str(row.get("code") or ""): row for row in (universe or []) if row.get("code")}
    expected_day = _date(asof_date or dt.date.today()).isoformat()
    pool, skipped_stale = [], 0
    for concept in concepts or []:
        rank = int(_num(concept.get("rank"), 999))
        concept_pct = _num(concept.get("pct"), 0.0)
        concept_flow = _num(concept.get("main_net"), 0.0)
        breadth = _num(concept.get("positive_ratio"), 0.0)
        leader_context = list(concept.get("leader_context") or [])
        leader_driven = bool(leader_context)
        if not concept.get("complete") or breadth < 0.45:
            continue
        if leader_driven:
            # Reverse mapping is allowed to discover a board which has not
            # reached the top of the aggregate flow ranking yet, but only when
            # its strong leader is independently corroborated by breadth and
            # at least two tradeable peers.  It never includes that leader.
            if int(_num(concept.get("member_count"), 0)) < 5 or int(_num(concept.get("active_peer_count"), 0)) < 2:
                continue
        elif rank > 10 or concept_pct <= 0.4 or concept_flow <= 0:
            continue
        leader_codes = {str(row.get("code") or "") for row in leader_context}
        for member in concept.get("members") or []:
            code = str(member.get("code") or "")
            base = universe_by_code.get(code)
            live = live_map.get(code)
            # Formal candidates must be present in the full-market live map;
            # concept-member quotes are discovery evidence only.
            if not code or code in seen_codes or code in leader_codes or base is None or live is None:
                continue
            quote_at = str(live.get("quote_at") or "")
            if not quote_at.startswith(expected_day):
                skipped_stale += 1
                continue
            scope = _security_scope(code, live.get("name") or base.get("name"), base.get("risk_flag"))
            if not scope["allowed"]:
                continue
            price, pct = _num(live.get("price"), 0.0), _num(live.get("pct"), 999.0)
            main_pct, main_net = _num(live.get("main_pct"), 0.0), _num(live.get("main_net"), 0.0)
            super_net, vol_ratio = _num(live.get("super_net"), 0.0), _num(live.get("vol_ratio"), 0.0)
            if price <= 0 or not (0.2 < pct < 9.0) or main_net <= 0 or main_pct <= 0 or vol_ratio < 0.65:
                continue
            price_stage = max(0.0, 1.0 - abs(pct - 3.0) / 6.0)
            score = min(0.74, 0.46 + (0.035 if leader_driven else max(0, 10 - rank) * 0.008)
                        + min(0.08, main_pct / 100.0)
                        + min(0.05, max(0.0, vol_ratio - 0.6) * 0.025)
                        + price_stage * 0.05 + min(0.04, breadth * 0.04))
            pool.append((score, main_net, super_net, -pct, code, {
                "code": code, "name": live.get("name") or member.get("name") or base.get("name"),
                "industry": base.get("industry") or live.get("industry"),
                "price": price, "pct": pct, "score": round(score, 3),
                "entry_path": "concept_expansion", "candidate_status": "concept_expansion_lane",
                "sector_heat": {"rank": rank, "name": concept.get("name"), "pct": concept_pct,
                                "main_net": concept_flow, "positive_ratio": breadth,
                                "source": concept.get("source")},
                "concept_context": {"version": CONCEPT_EXPANSION_LANE_VERSION,
                                    "concept_code": concept.get("code"),
                                    "concept_name": concept.get("name"), "concept_rank": rank,
                                    "discovery_path": "leader_reverse_map" if leader_driven else "concept_flow_topn",
                                    "leader_context": leader_context,
                                    "active_peer_count": int(_num(concept.get("active_peer_count"), 0)),
                                    "member_main_pct": main_pct, "member_main_net": main_net,
                                    "quote_at": quote_at, "constituents_complete": True},
            }))
    pool.sort(reverse=True, key=lambda row: row[:5])
    lane, per_counts, emitted = [], {}, set()
    for _, _, _, _, code, item in pool:
        concept_name = item["concept_context"]["concept_name"]
        if code in emitted or per_counts.get(concept_name, 0) >= per_concept:
            continue
        emitted.add(code)
        per_counts[concept_name] = per_counts.get(concept_name, 0) + 1
        lane.append(item)
        if len(lane) >= max_total:
            break
    return lane, {"version": CONCEPT_EXPANSION_LANE_VERSION, "injected": len(lane),
                  "concepts": sorted(per_counts), "skipped_stale_live": skipped_stale,
                  "note": "完整概念成分仅作发现；领涨股仅用于反查概念且被排除，正式候选必须命中当日全市场实时行情"}


def _ths_hot_lane_candidates(universe, live_map, seen_codes=None,
                             asof_date=None, max_total=3):
    """同花顺热点题材车道（P0，sector_rotation 候选层）。

    同花顺编辑部人工运营的"当日强势股+题材归因"是市场上最快的题材
    信号（73ms 零鉴权，~125 只/日）。命中榜单且仍可合规买入的成员
    直接注入候选，题材标签随 payload 入审计。与板块异动车道的差异：
    本车道以"个股已确认强势+题材明确"为准，不依赖板块聚合排名。
    注入候选仍需通过追高上限、Q级、入场模型、资金等全部执行闸门；
    评分与 surge lane 同规：证据合成、硬顶 0.74，不驱动强制换仓。
    """
    if AD is None:
        return [], None
    seen_codes = set(seen_codes or ())
    try:
        hot_rows = AD.ths_hot_reason() or []
    except Exception:
        hot_rows = []
    if not hot_rows:
        return [], None
    hot_by_code = {str(row.get("code") or ""): row for row in hot_rows}
    universe_by_code = {
        str(row.get("code") or ""): row for row in (universe or [])
        if row.get("code")
    }
    lane = []
    valid_pct = sorted(
        float(row.get("pct")) for row in hot_rows
        if isinstance(row.get("pct"), (int, float))
    )
    valid_dde = sorted(
        float(row.get("dde_net")) for row in hot_rows
        if isinstance(row.get("dde_net"), (int, float))
    )

    def _percentile(value, population):
        if not population or not isinstance(value, (int, float)):
            return 0.0
        return sum(1 for item in population if item <= float(value)) / len(population)

    for code, hot in hot_by_code.items():
        if code in seen_codes or code not in universe_by_code:
            continue
        row = universe_by_code[code]
        if row.get("risk_flag") or "ST" in str(row.get("name") or "").upper():
            continue
        live = live_map.get(code) or {}
        price = _num(live.get("price"), _num(row.get("price")))
        pct = _num(live.get("pct"), None)
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # 已涨停附近/下跌的不占车道；与 surge lane 同一可交易区间。
        if not isinstance(pct, (int, float)) or pct <= 0.5 or pct >= 9.0:
            continue
        dde = _num(hot.get("dde_net"), 0.0)
        theme = str(hot.get("reason") or "")[:60]
        # DDE absolute units are not a stable public contract.  Rank values
        # inside the same snapshot and penalise late/high-runup names instead
        # of treating the raw number as a permanent scoring scale.
        pct_rank = _percentile(pct, valid_pct)
        dde_rank = _percentile(dde, valid_dde)
        maturity_penalty = max(0.0, pct - 5.5) * 0.015
        lane_score = min(0.70, max(
            0.0, 0.54 + pct_rank * 0.08 + dde_rank * 0.06 - maturity_penalty
        ))
        lane.append({
            "code": code, "name": row.get("name"), "industry": row.get("industry"),
            "price": price, "pct": pct,
            "score": round(lane_score, 3),
            "entry_path": "ths_hot",
            "candidate_status": "ths_hot_lane",
            "ths_hot_context": {
                "theme": theme,
                "turnover": hot.get("turnover"),
                "dde_net": dde,
                "pct_percentile": round(pct_rank, 4),
                "dde_percentile": round(dde_rank, 4),
                "maturity_penalty": round(maturity_penalty, 4),
                "source_at": hot.get("source_at"),
                "reason": "同花顺当日强势股人工题材归因命中；全部执行闸门照常生效",
            },
        })
    lane.sort(key=lambda item: (
        _num(item.get("score")),
        _num((item.get("ths_hot_context") or {}).get("dde_percentile")),
    ), reverse=True)
    lane = lane[:max_total]
    meta_rows = [
        {"code": item.get("code"), "name": item.get("name"),
         "theme": (item.get("ths_hot_context") or {}).get("theme"),
         "pct": item.get("pct"), "score": item.get("score")}
        for item in lane
    ]
    meta = None
    if lane:
        meta = {
            "version": "ths-hot-lane-v1",
            "source": "同花顺当日强势股题材归因",
            "hot_count": len(hot_rows),
            "injected": meta_rows,
            "note": "注入候选仍走全部执行闸门；追高上限照常生效",
        }
    return lane, meta


def _sector_heat_map(universe, sector_rows=None):
    """Build a live breadth-aware sector map.

    The old map was based only on the leading pages of the gainers list.  That
    identifies a sector after several names have already accelerated, but it
    misses the earlier, more useful rotation phase.  We retain that source as
    corroboration, while calculating breadth and median strength from the
    full live universe.  This is a ranking context only: it cannot bypass any
    security, quote, Q-level, capital or risk gate.
    """
    rows = list(sector_rows or [])
    source = "盘中热点板块"

    # First build same-snapshot industry statistics from the full universe.
    # A valid onset needs breadth and positive median strength, not one
    # limit-up stock.  Missing main-flow data is neutral rather than positive.
    grouped = {}
    for item in universe or []:
        key = _sector_key(item.get("industry"))
        pct = item.get("pct")
        if not key or not isinstance(pct, (int, float)) or abs(float(pct)) > 20:
            continue
        bucket = grouped.setdefault(key, {"pct": [], "main_pct": [], "codes": set()})
        bucket["pct"].append(float(pct))
        main_pct = item.get("main_pct")
        if isinstance(main_pct, (int, float)) and abs(float(main_pct)) <= 100:
            bucket["main_pct"].append(float(main_pct))
        code = str(item.get("code") or "")
        if code:
            bucket["codes"].add(code)

    def _median(values):
        values = sorted(values)
        if not values:
            return 0.0
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0

    live_metrics = {}
    for key, bucket in grouped.items():
        values = bucket["pct"]
        count = len(bucket["codes"]) or len(values)
        if count < 5:
            continue
        median_pct = _median(values)
        positive_ratio = sum(value > 0 for value in values) / len(values)
        median_main_pct = _median(bucket["main_pct"]) if bucket["main_pct"] else None
        # Early rotation: broad participation, modest positive median and no
        # single-name inference.  The flow clause is only required when flow
        # coverage exists for the group; unknown flow never grants a bonus.
        flow_confirmed = median_main_pct is not None and median_main_pct >= 0.0
        onset = (
            median_pct >= 0.35
            and positive_ratio >= 0.58
            and flow_confirmed
        )
        onset_score = 0.0
        if onset:
            onset_score = min(
                1.0,
                0.34
                + min(0.26, median_pct * 0.18)
                + min(0.24, max(0.0, positive_ratio - 0.50) * 1.2)
                + (0.10 if flow_confirmed else 0.0),
            )
        live_metrics[key] = {
            "member_count": count,
            "median_pct": round(median_pct, 3),
            "positive_ratio": round(positive_ratio, 3),
            "median_main_pct": round(median_main_pct, 3) if median_main_pct is not None else None,
            "early_rotation": bool(onset),
            "early_rotation_score": round(onset_score, 3),
        }

    if not rows:
        rows = [
            {
                "name": key,
                "pct": metrics["median_pct"],
                "leader_count": metrics["member_count"],
                "positive_ratio": metrics["positive_ratio"],
                "source": "全市场实时行业聚合",
            }
            for key, metrics in live_metrics.items()
        ]
        rows.sort(key=lambda row: (_num(row.get("pct"), -999), _num(row.get("positive_ratio"), 0)), reverse=True)
        source = "全市场实时行业聚合"

    # Keep the external top-sector rows, then add broad early-rotation groups
    # that are not yet in the top-gainer pages.
    by_key = {_sector_key(row.get("name")): dict(row) for row in rows if _sector_key(row.get("name"))}
    for key, metrics in live_metrics.items():
        if metrics["early_rotation"] and key not in by_key:
            by_key[key] = {
                "name": key, "pct": metrics["median_pct"],
                "leader_count": metrics["member_count"],
                "positive_ratio": metrics["positive_ratio"],
                "source": "全市场实时行业聚合",
            }
    ordered = sorted(
        by_key.values(),
        key=lambda row: (
            bool(live_metrics.get(_sector_key(row.get("name")), {}).get("early_rotation")),
            _num(row.get("pct"), -999), _num(row.get("positive_ratio"), 0),
        ),
        reverse=True,
    )
    output = {}
    for rank, row in enumerate(ordered[:35], 1):
        key = _sector_key(row.get("name"))
        if not key:
            continue
        raw_pct = _num(row.get("pct"), 0)
        if abs(raw_pct) > 30:
            continue
        pct = max(-20.0, min(20.0, raw_pct))
        metrics = live_metrics.get(key, {})
        onset_score = _num(metrics.get("early_rotation_score"), 0.0)
        strength = max(0.0, 2.5 - (rank - 1) * 0.10) + max(0.0, pct) * 0.18 + onset_score * 0.35
        output[key] = {
            "rank": rank,
            "name": row.get("name") or key,
            "pct": round(pct, 2),
            "score": round(strength, 3),
            "top_stock": row.get("top_stock"),
            "top_stock_code": row.get("top_stock_code"),
            "source": row.get("source") or source,
            **metrics,
        }
    return output


def _sector_entry_threshold_context(sector_heat, market, candidate_heat=None):
    """Derive a same-day sector-entry threshold from market and heat dispersion.

    A static 0.68 threshold makes a broad momentum day and a weak rotation day
    indistinguishable.  The context is persisted on each candidate so an order
    audit can show the exact daily threshold and why it moved.
    """
    heats = list((sector_heat or {}).values())
    top = heats[:10]
    positive_top = sum(1 for item in top if _num(item.get("pct"), 0) > 0)
    light = str((market or {}).get("light") or "unknown")
    breadth = _num((market or {}).get("breadth_up_pct"), None)
    threshold = {"green": 0.59, "yellow": 0.63, "red": 0.68}.get(light, 0.67)
    reasons = [f"市场{ {'green':'绿灯','yellow':'黄灯','red':'红灯'}.get(light, '数据未知') }"]
    if breadth is not None:
        if breadth >= 65:
            threshold -= 0.03
            reasons.append(f"上涨家数占比 {breadth:.1f}% 较高")
        elif breadth >= 55:
            threshold -= 0.01
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏强")
        elif breadth < 45:
            threshold += 0.03
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏弱")
        elif breadth < 50:
            threshold += 0.015
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏谨慎")
    else:
        threshold += 0.015
        reasons.append("市场宽度缺失，额外保守")
    if top:
        if positive_top >= 7:
            threshold -= 0.02
            reasons.append(f"前{len(top)}热点中 {positive_top} 个上涨，热点扩散充分")
        elif positive_top <= 2:
            threshold += 0.025
            reasons.append(f"前{len(top)}热点中仅 {positive_top} 个上涨，热点集中")
    else:
        threshold += 0.02
        reasons.append("热点板块样本不足")
    candidate_heat = candidate_heat or {}
    rank = int(_num(candidate_heat.get("rank"), 999))
    pct = _num(candidate_heat.get("pct"), 0)
    if rank <= 3 and pct >= 1.5:
        threshold -= 0.015
        reasons.append(f"所属板块第{rank}名且涨幅 {pct:+.2f}%")
    elif rank > 8 or pct < 0.5:
        threshold += 0.02
        reasons.append(f"所属板块第{rank}名、涨幅 {pct:+.2f}%")
    threshold = round(max(0.54, min(0.72, threshold)), 3)
    return {
        "threshold": threshold,
        "market_light": light,
        "breadth_up_pct": breadth,
        "positive_top_count": positive_top,
        "top_sample_count": len(top),
        "sector_rank": rank,
        "sector_pct": round(pct, 2),
        "reason": "；".join(reasons),
    }


def _trend_entry_threshold_context(market, trend_structure, distance_ma20,
                                   main_pct, pct):
    """按当日环境动态计算趋势回踩确认门槛。

    趋势回踩不是固定分数闸门：强趋势、宽度健康且回踩位置合理时，
    可以降低确认线；弱市、波动放大或均线结构不完整时提高确认线。
    结构性否决仍由 ``_strategy_entry_assessment`` 的 blockers 负责，
    本函数只负责可解释的分数门槛，并把所有输入写入审计快照。
    """
    market = market or {}
    light = str(market.get("light") or "unknown").lower()
    # v3: 只放松趋势回踩的确认分软门槛；红灯/未知市场、双源行情、
    # 权限、流动性和均线破坏等硬门禁保持不变。
    threshold = {"green": 0.53, "yellow": 0.57, "red": 0.68}.get(light, 0.63)
    reasons = [f"市场{ {'green':'绿灯','yellow':'黄灯','red':'红灯'}.get(light, '数据未知') }"]

    breadth = _num(market.get("breadth_up_pct"), None)
    if breadth is None:
        threshold += 0.015
        reasons.append("市场宽度缺失，额外保守")
    elif breadth >= 65:
        threshold -= 0.03
        reasons.append(f"上涨家数占比 {breadth:.1f}% 较强")
    elif breadth >= 55:
        threshold -= 0.015
        reasons.append(f"上涨家数占比 {breadth:.1f}% 偏强")
    elif breadth < 45:
        threshold += 0.035
        reasons.append(f"上涨家数占比 {breadth:.1f}% 偏弱")
    else:
        threshold += 0.015
        reasons.append(f"上涨家数占比 {breadth:.1f}% 偏谨慎")

    # 不同调用方字段名不完全一致，优先使用明确的百分比字段。
    volatility = None
    for key in ("index_volatility_pct", "market_volatility_pct", "volatility_pct"):
        if market.get(key) is not None:
            volatility = _num(market.get(key), None)
            break
    if volatility is not None:
        if volatility >= 2.5:
            threshold += 0.025
            reasons.append(f"指数波动 {volatility:.2f}% 偏高")
        elif volatility <= 1.0:
            threshold -= 0.01
            reasons.append(f"指数波动 {volatility:.2f}% 温和")

    if trend_structure >= 0.90 and distance_ma20 is not None and -0.03 <= distance_ma20 <= 0.05:
        threshold -= 0.025
        reasons.append("MA20/MA60多头且回踩位置合理")
    elif trend_structure >= 0.70:
        threshold -= 0.01
        reasons.append("均线结构基本修复")
    elif trend_structure < 0.50:
        threshold += 0.045
        reasons.append("均线结构偏弱")

    if distance_ma20 is None:
        threshold += 0.025
        reasons.append("MA20偏离度缺失")
    elif distance_ma20 < -0.06 or distance_ma20 > 0.09:
        threshold += 0.03
        reasons.append(f"偏离MA20 {distance_ma20 * 100:+.2f}%，位置不理想")
    elif -0.03 <= distance_ma20 <= 0.05:
        threshold -= 0.015
        reasons.append(f"偏离MA20 {distance_ma20 * 100:+.2f}%，处于回踩区")

    if main_pct <= -8:
        threshold += 0.025
        reasons.append(f"主力净流出 {main_pct:+.2f}%")
    if pct <= -2.5:
        threshold += 0.015
        reasons.append(f"实时涨跌幅 {pct:+.2f}% 偏弱")

    threshold = round(max(0.50, min(0.74, threshold)), 3)
    return {
        "version": "trend-pullback-threshold-v3",
        "threshold": threshold,
        "market_light": light,
        "breadth_up_pct": breadth,
        "volatility_pct": volatility,
        "trend_structure": round(_num(trend_structure), 3),
        "distance_ma20_pct": round(distance_ma20 * 100, 3) if distance_ma20 is not None else None,
        "main_pct": round(_num(main_pct), 3),
        "intraday_pct": round(_num(pct), 3),
        "relaxation": {
            "scope": "trend_pullback_soft_score_only",
            "baseline_delta": -0.02 if light == "green" else -0.03 if light == "yellow" else 0.0,
            "floor": 0.50,
            "hard_gates_unchanged": True,
        },
        "execution_zone": {"ma20_distance_min_pct": -10.0, "ma20_distance_max_pct": 14.0},
        "reason": "；".join(reasons),
    }


def _sector_overheat_guard(kline, quote=None, candidate_heat=None):
    """识别板块轮动中的个股追高/末端加速风险。

    板块强势只说明资金在流入，不代表当前价位仍有安全边际。这里使用
    同一只股票的已完成日线与实时价，动态组合 5/20 日涨幅、均线乖离、
    布林上轨位置和连续上涨天数。缺少足够历史时返回 ``unknown``，由上层
    保守降权而不是编造结论。
    """
    quote = quote or {}
    heat = candidate_heat or {}
    result = {
        "level": "unknown", "score": None, "ret5_pct": None, "ret20_pct": None,
        "ma20_distance_pct": None, "boll_position": None, "up_streak": None,
        "sector_pct": _num(heat.get("pct"), None), "sector_rank": int(_num(heat.get("rank"), 999)),
        "pullback_confirmed": False, "reason": "历史日线不足，暂不判定过热",
    }
    if kline is None or len(kline) < 25:
        return result
    try:
        closes = pd.to_numeric(kline["close"], errors="coerce").dropna()
    except (KeyError, TypeError, ValueError):
        return result
    if len(closes) < 25:
        return result
    close = _num(quote.get("price"), _num(closes.iloc[-1], 0))
    if close <= 0:
        return result
    ma20 = _num(closes.tail(20).mean(), None)
    std20 = _num(closes.tail(20).std(ddof=0), 0.0)
    if not ma20 or ma20 <= 0:
        return result
    ret5 = (float(closes.iloc[-1]) / float(closes.iloc[-6]) - 1.0) * 100
    ret20 = (float(closes.iloc[-1]) / float(closes.iloc[-21]) - 1.0) * 100
    distance = (close / ma20 - 1.0) * 100
    upper = ma20 + 2.0 * std20
    boll_position = (close - (ma20 - 2.0 * std20)) / max(upper - (ma20 - 2.0 * std20), 1e-9)
    streak = 0
    for idx in range(len(closes) - 1, 0, -1):
        if float(closes.iloc[idx]) > float(closes.iloc[idx - 1]):
            streak += 1
        else:
            break
    current_pct = _num(quote.get("pct"), 0.0)
    intraday_high = _num(quote.get("high"), 0.0)
    open_price = _num(quote.get("open_price"), 0.0)
    intraday_pullback_pct = (
        (close / intraday_high - 1.0) * 100
        if intraday_high > 0 and close > 0 else None
    )
    # "距离 MA20 不超过 6%" 是位置描述，不是日内回踩。此前它让不少
    # 正在加速的热点被误标为已经回踩。只有先从当日高点回撤至少 0.8%，
    # 再守住开盘价附近，才算可执行的回踩承接；缺少高/开盘价则不伪造确认。
    pullback = bool(
        intraday_pullback_pct is not None
        and intraday_pullback_pct <= -0.8
        and (open_price <= 0 or close >= open_price * 0.995)
    )
    score = 0.0
    score += 0.30 if ret5 >= 12 else 0.18 if ret5 >= 8 else 0.0
    score += 0.25 if ret20 >= 28 else 0.15 if ret20 >= 20 else 0.0
    score += 0.25 if distance >= 18 else 0.15 if distance >= 12 else 0.0
    score += 0.20 if boll_position >= 1.02 else 0.12 if boll_position >= 0.92 else 0.0
    score += 0.10 if streak >= 6 else 0.05 if streak >= 4 else 0.0
    if _num(heat.get("pct"), 0) >= 3.5 and int(_num(heat.get("rank"), 999)) <= 5:
        score += 0.08
    score = min(1.0, score)
    level = "extreme" if score >= 0.72 else "hot" if score >= 0.50 else "caution" if score >= 0.28 else "normal"
    reason = {
        "extreme": "个股处于连续加速、均线乖离或布林上轨末端，禁止板块轮动追入",
        "hot": "个股短线过热，等待回踩均线/布林中轨并重新放量确认",
        "caution": "个股已有明显短线加速，降低仓位并要求回踩确认",
        "normal": "个股位置未显示明显过热",
    }[level]
    result.update({
        "level": level, "score": round(score, 3), "ret5_pct": round(ret5, 2),
        "ret20_pct": round(ret20, 2), "ma20_distance_pct": round(distance, 2),
        "boll_position": round(boll_position, 3), "up_streak": streak,
        "intraday_high": round(intraday_high, 3) if intraday_high > 0 else None,
        "intraday_pullback_pct": round(intraday_pullback_pct, 2) if intraday_pullback_pct is not None else None,
        "pullback_confirmed": pullback, "reason": reason,
    })
    return result


def _star_sector_impulse(universe, asof_date=None):
    """Map broad STAR-board strength into a small same-industry bonus.

    STAR stocks remain untradable.  Their aggregate move is context only: at
    least five same-day valid peers, a positive majority and a positive median
    are required.  The bounded bonus cannot override any entry or risk gate.
    """
    grouped = {}
    latest = None
    dated_rows = []
    for item in universe or []:
        stamp = str(item.get("quote_at") or item.get("time") or "")
        source_day = stamp[:10]
        if len(source_day) == 10 and source_day[4:5] == "-" and source_day[7:8] == "-":
            dated_rows.append(source_day)
    target_day = _date(asof_date or dt.date.today()).isoformat()
    eligible_days = [value for value in dated_rows if value <= target_day]
    day = max(eligible_days, default=target_day)
    for row in universe or []:
        scope = _security_scope(row.get("code"), row.get("name"), row.get("risk_flag"))
        if scope["board"] != "科创板":
            continue
        quote_at = str(row.get("quote_at") or row.get("time") or "")
        if not quote_at or quote_at[:10] != day:
            continue
        pct = _num(row.get("pct"), None)
        sector = _sector_key(row.get("industry"))
        if not sector or pct is None or abs(pct) > 20:
            continue
        grouped.setdefault(sector, []).append(float(pct))
        latest = max(latest or quote_at, quote_at) if quote_at else latest
    output = {}
    for sector, values in grouped.items():
        if len(values) < 5:
            continue
        series = pd.Series(values, dtype="float64")
        median_pct = float(series.median())
        positive_ratio = float((series > 0).mean())
        if median_pct < 0.8 or positive_ratio < 0.60:
            continue
        bonus = min(0.12, max(0.0, median_pct - 0.5) * 0.018 + max(0.0, positive_ratio - 0.5) * 0.08)
        output[sector] = {
            "sample_count": len(values),
            "median_pct": round(median_pct, 2),
            "positive_ratio_pct": round(positive_ratio * 100, 1),
            "bonus": round(bonus, 3),
            "source": "科创板同业实时表现（仅作映射加分）",
            "asof": latest,
        }
    return output


def _new_entry_price_gate(account, pick, quote=None):
    """Keep same-day new entries away from late acceleration and limit-up queues.

    This gate applies only to a *new* strategy-position.  Existing positions
    still use their dedicated scale-in / intraday-T models; another strategy
    may independently hold the same code.

    2026-08-24 新增盘中追高闸门：现价较今日开盘的溢价超过
    ``max_open_runup_pct`` 时拒绝开仓。此前只校验涨幅 vs 昨收，
    候选在早盘被 deferred 后，等席位释放时按已拉升 2%+ 的现价成交
    （"便宜的不买贵的买"），追高买入极易买在日内顶部。
    """
    account_id = account["id"] if isinstance(account, dict) else str(account)
    quote = quote or {}
    pct = _num(quote.get("pct"), _num(pick.get("pct"), None))
    # This is only a *routing* exception.  The later chase gate still requires
    # green market, top-sector confirmation, Q1, strong money flow/volume and
    # a passing cross-source quote before it may execute.  Do not apply this
    # to ordinary sector candidates or any other strategy.
    aggressive_sector_candidate = (
        account_id == "sector_rotation"
        and str(pick.get("candidate_status") or "") in {
            "ths_hot_lane", "concept_expansion_lane", "sector_surge_lane", "hot_leader_watch",
        }
        and pct is not None and pct >= 7.0
    )
    # 盘中追高上限：现价 / 今日开盘 - 1。开盘价缺失时跳过（无法度量盘中拉升）。
    price = _num(quote.get("price"), 0.0)
    open_price = _num(quote.get("open_price"), 0.0)
    max_runup = (ACCOUNT_SPECS.get(account_id) or {}).get("max_open_runup_pct")
    # The fast strategy's acceleration zone is handled by the strict chase
    # gate below.  Let it reach that gate instead of being rejected first by
    # the generic open-price runup rule.
    tq_momentum_candidate = account_id == "tq_breakout" and pct is not None and pct >= 3.5
    # 主力点火候选（2026-08-31 复核 P1）：+3.5%~+7.5% 不再被开盘追高上限
    # 直接否决——通用追高帽与主力 +8.8% 执行区间互相矛盾，当日大量强股
    # 因此反复被拒。改走 ignition_entry 八项确认（入场评估层强制校验）。
    mf_ignition_candidate = (
        account_id == MAIN_FORCE_STRATEGY_ID
        and pct is not None and 3.5 <= pct <= 7.5
    )
    if (
        price > 0 and open_price > 0 and max_runup is not None
        and not aggressive_sector_candidate
        and not tq_momentum_candidate and not mf_ignition_candidate
    ):
        effective_cap = max_runup + _open_runup_bonus()
        runup = price / open_price - 1
        if runup > effective_cap:
            return False, (
                f"现价较开盘 {runup*100:+.2f}% 已超盘中追高上限 "
                f"{effective_cap*100:.1f}%（含时段余量），转观察等待回踩，不追日内高点"
            )
    if pct is None:
        return True, None
    limit_pct = _limit_pct(pick.get("code"))
    # 短线日内做T拥有唯一的追高通道，后续仍必须通过 _chase_entry_gate 的
    # 双源行情、Q1、资金、量能和策略确认分校验；本函数不能提前把它筛掉。
    if account_id == "tq_breakout":
        if pct > limit_pct + 0.60:
            return False, f"涨幅 {pct:+.2f}% 已超出涨停附近有效报价区间，停止追高"
        if pct >= limit_pct - 0.15:
            return False, f"涨幅 {pct:+.2f}% 已触及涨停价位，不模拟封板排队成交"
        return True, None

    # 财报质量突破不追高：涨停附近或突破末端一律观察；其余高位按独立
    # 入场上限过滤。它不能借用短线接力的追高通道。
    if account_id == MAIN_FORCE_STRATEGY_ID:
        limit_buffer = 1.0 if limit_pct <= 10.0 else 2.0
        if pct >= limit_pct - limit_buffer:
            return False, f"涨幅 {pct:+.2f}% 已接近涨停，{ACCOUNT_SPECS[account_id]['entry_model_name']}不模拟封板排队"
        ignition_high = 7.5
        if pct > ignition_high:
            return False, (
                f"涨幅 {pct:+.2f}% 超出主力承接/点火区间（≤+{ignition_high}%），转入观察"
            )
        if pct > 3.5:
            # 点火分支路由：此处不做实质判定，只打标；八项点火条件
            # （分钟量能、VWAP、低点抬高、资金增量/持续率、微观盘口）
            # 在入场评估层强制校验，30 秒快速通道同样携带该证据。
            if isinstance(pick, dict):
                pick["ignition_lane"] = True
        return True, None

    if account_id == NEW_STRATEGY_ID:
        limit_buffer = 1.0 if limit_pct <= 10.0 else 2.0
        if pct >= limit_pct - limit_buffer:
            return False, f"涨幅 {pct:+.2f}% 已接近涨停，{ACCOUNT_SPECS[account_id]['entry_model_name']}不追高，转入回踩观察"
        ceiling = _num(ACCOUNT_SPECS[account_id].get("entry_pct_high"), 6.5)
        if pct > ceiling:
            return False, f"涨幅 {pct:+.2f}% 超出{ACCOUNT_SPECS[account_id]['entry_model_name']}突破执行上限，转入观察"
        return True, None

    # 板块轮动的热点加速候选由专属追高门禁接管；普通板块候选仍不追高。
    if aggressive_sector_candidate:
        if pct >= limit_pct - 0.15:
            return False, f"涨幅 {pct:+.2f}% 已触及涨停价位，不模拟封板排队成交"
        return True, None

    # 趋势与板块策略不追高：涨停附近一律观察；其余高位也按策略上限过滤。
    limit_buffer = 1.0 if limit_pct <= 10.0 else 2.0
    if pct >= limit_pct - limit_buffer:
        return False, f"涨幅 {pct:+.2f}% 已接近涨停，{ACCOUNT_SPECS[account_id]['entry_model_name']}不追高，转入次日观察"
    ceiling = {"sector_rotation": 7.0, "trend_pullback": 6.0}.get(account_id, 7.0)
    if pct > ceiling:
        return False, f"涨幅 {pct:+.2f}% 超出{ACCOUNT_SPECS[account_id]['entry_model_name']}当日新开仓区间，转入观察"
    return True, None


def _attach_candidate_financial_disclosure(picks, asof_date):
    """Attach point-in-time disclosure evidence to the *actual* candidates.

    The finance endpoint provides report periods but not publication times.  We
    therefore never invent one.  Instead, only the small candidate set is
    enriched from the disclosure timeline; a missing/failed notice source is
    visible as ``shadow`` rather than silently becoming an approved report.
    This is evidence and tie-break metadata for now, not a new hard gate that
    could make the three established paper strategies suddenly empty.
    """
    picks = list(picks or [])
    codes = list(dict.fromkeys(str(item.get("code") or "") for item in picks if item.get("code")))[:120]
    summary = {
        "status": "unavailable", "requested": len(codes), "reported": 0,
        "shadow": 0, "unknown": 0, "latest_published_at": None,
        "source": "disclosure_timeline",
    }
    if not codes:
        summary["status"] = "empty"
        return picks, summary
    if DT is None:
        summary["reason"] = "披露时间模块不可用；保留财报数值但不作为点时已证实证据"
        return picks, summary
    try:
        records = DT.fetch_disclosure_timeline(codes, asof=asof_date)
    except Exception as exc:
        summary["reason"] = f"披露时间源本轮不可用：{type(exc).__name__}"
        return picks, summary
    evidence = {str(row.get("code")): dict(row) for row in (records or []) if row.get("code")}
    published = []
    bonus_values = []
    for pick in picks:
        code = str(pick.get("code") or "")
        row = evidence.get(code) or {}
        published_at = row.get("published_at")
        status = str(row.get("status") or "unknown")
        source = str(row.get("source") or "unknown")
        proven = bool(published_at) and source != "unknown" and status not in {"unknown", "error"}
        financial = {
            "report_period": row.get("report_period") or pick.get("report_period"),
            "report_published_at": published_at,
            "asof_date": str(_date(asof_date)),
            "profit_source": "reported" if proven else "shadow",
            "source": source,
            "status": status,
            "reason": row.get("reason"),
        }
        pick["financial_evidence"] = financial
        # These flat aliases are deliberately kept for decision snapshots and
        # existing UI consumers that do not yet render the nested object.
        pick["report_period"] = financial["report_period"]
        pick["report_published_at"] = financial["report_published_at"]
        pick["financial_source"] = financial["source"]
        pick["profit_source"] = financial["profit_source"]
        # Phase 1 of the rollout: a verifiably published report can only
        # break close ranking ties.  The bounded bonus is deliberately far
        # smaller than an entry/risk signal and is recorded separately so it
        # can be shadow-evaluated before any future hard profit gate.
        bonus = 0.0
        age_days = None
        if proven:
            bonus = 0.035
            try:
                published_day = dt.datetime.fromisoformat(str(published_at).replace("Z", "+00:00")).date()
                age_days = max(0, (_date(asof_date) - published_day).days)
                if age_days <= 120:
                    bonus += 0.015
            except (TypeError, ValueError, OverflowError):
                pass
        pick["financial_evidence"]["report_age_days"] = age_days
        pick["financial_evidence"]["ranking_bonus"] = round(bonus, 4)
        pick["financial_ranking_bonus"] = round(bonus, 4)
        if bonus:
            old_score = _num(pick.get("score"), 0.0)
            pick["score"] = round(old_score + bonus, 6)
            components = dict(pick.get("score_components") or {})
            components["financial_disclosure_bonus"] = round(bonus, 6)
            components["final_score_before_disclosure"] = round(old_score, 6)
            components["final_score"] = round(old_score + bonus, 6)
            pick["score_components"] = components
            pick.setdefault("reasons", []).append(
                f"定期报告已披露（{financial['report_period'] or '报告期未知'}），排序加分 {bonus:.3f}"
            )
            bonus_values.append(bonus)
        if proven:
            summary["reported"] += 1
            published.append(str(published_at))
        else:
            summary["shadow"] += 1
            summary["unknown"] += 1
    summary["status"] = "ready" if summary["reported"] == len(codes) else "partial"
    summary["latest_published_at"] = max(published) if published else None
    summary["ranking_bonus_applied"] = len(bonus_values)
    summary["ranking_bonus_max"] = round(max(bonus_values), 4) if bonus_values else 0.0
    summary["hard_gate_readiness"] = {
        "ready": False,
        "requirement": "连续5个交易日候选披露覆盖率≥85%，并完成样本外验证后，才可人工决定是否升级为利润硬门",
    }
    # Current score is the execution candidate score.  A reported disclosure
    # may resolve close ties, while all price/risk gates remain unchanged.
    picks.sort(key=lambda item: (_num(item.get("score")), _num(item.get("super_net"))), reverse=True)
    return picks, summary


MAIN_FORCE_WATCH_POOL_PATH = os.path.join(BASE, "data_cache", "main_force_watch_pool.json")
_MAIN_FORCE_POOL_LOCK = threading.Lock()
_MAIN_FORCE_POOL_SIZE = 10
_MAIN_FORCE_POOL_BUFFER = 3   # 额外请求的替补位（top10 掉榜时用 11-13 名补）
_MAIN_FORCE_POOL_MAX_REPLACEMENTS = 2


def _main_force_watch_pool(picks, *, asof_day=None, now=None, live_map=None):
    """主力策略每日10只观察池冻结（2026-08-31 复核 P1）。

    旧实现每三分钟全量重算 top10，当日候选并集漂移到 17 只，无法稳定
    跟踪资金持续性。现在：早盘/首次有效扫描冻结 10 只；此后每轮重算
    top(10+buffer) 作为比照，掉榜成员用榜单新面孔替换，全天最多替换
    2 只；替换预算耗尽后掉榜成员保留（只要还在 buffer 内）。

    fail-open：任何异常返回原始 picks，绝不因观察池状态冻结选股。
    """
    now = now if isinstance(now, dt.datetime) else dt.datetime.now()
    day = _date(asof_day or now.date()).isoformat()
    try:
        with _MAIN_FORCE_POOL_LOCK:
            state = {}
            try:
                with open(MAIN_FORCE_WATCH_POOL_PATH, encoding="utf-8") as handle:
                    state = json.load(handle) or {}
            except (OSError, ValueError):
                state = {}
            if state.get("date") != day:
                state = {
                    "date": day, "codes": [], "seed_picks": [],
                    "replaced": [], "frozen_at": None,
                }
            ranked = [str(item.get("code") or "") for item in picks if item.get("code")]
            ranked = list(dict.fromkeys(ranked))
            window = ranked[: _MAIN_FORCE_POOL_SIZE + _MAIN_FORCE_POOL_BUFFER]
            current_by_code = {str(item.get("code") or ""): item for item in picks if item.get("code")}
            seed_by_code = {
                str(item.get("code") or ""): item
                for item in (state.get("seed_picks") or []) if item.get("code")
            }
            if not state.get("codes"):
                # 首次有效扫描：冻结当前 top10
                state["codes"] = window[:_MAIN_FORCE_POOL_SIZE]
                state["seed_picks"] = [
                    json.loads(_json(current_by_code[code]))
                    for code in state["codes"] if code in current_by_code
                ]
                state["frozen_at"] = now.isoformat(timespec="seconds")
                _save_watch_pool(state)
                pool = state["codes"]
            else:
                frozen = [str(c) for c in state["codes"] if str(c)]
                replaced = list(state.get("replaced") or [])
                budget = _MAIN_FORCE_POOL_MAX_REPLACEMENTS - len(replaced)
                kept, missing = [], []
                for code in frozen:
                    if code in window:
                        kept.append(code)
                    else:
                        missing.append(code)
                newcomers = [c for c in window if c not in frozen]
                for code in missing:
                    if budget <= 0 or not newcomers:
                        # The watch pool is a daily commitment, not a view of
                        # the latest top-N.  Preserve the original selection
                        # evidence when a name drops out of the transient
                        # model output; live price/risk validation still runs
                        # for it below.
                        kept.append(code)
                        continue
                    incoming = newcomers.pop(0)
                    replaced.append({
                        "out": code, "in": incoming,
                        "at": now.isoformat(timespec="seconds"),
                    })
                    kept.append(incoming)
                    budget -= 1
                # Preserve the full frozen pool even when a later model pass
                # has fewer output rows.  Only the bounded replacement path
                # is allowed to change membership during the same trade day.
                pool = list(dict.fromkeys(kept))[:_MAIN_FORCE_POOL_SIZE]
                if len(pool) < _MAIN_FORCE_POOL_SIZE:
                    pool.extend(c for c in window if c not in pool)
                    pool = pool[:_MAIN_FORCE_POOL_SIZE]
                state["codes"] = pool
                state["replaced"] = replaced
                for code in pool:
                    if code in current_by_code:
                        seed_by_code[code] = json.loads(_json(current_by_code[code]))
                state["seed_picks"] = [seed_by_code[code] for code in pool if code in seed_by_code]
                _save_watch_pool(state)
            picked = []
            live_fields = (
                "name", "industry", "price", "pct", "main_pct", "main_net", "super_net",
                "amount", "turnover", "vol_ratio", "quote_at", "source",
            )
            for code in pool:
                # Fresh model evidence wins; otherwise retain the frozen
                # selection evidence and overlay the current full-market quote.
                item = dict(current_by_code.get(code) or seed_by_code.get(code) or {})
                if not item:
                    continue
                live = (live_map or {}).get(code) or {}
                for field in live_fields:
                    if live.get(field) is not None:
                        item[field] = live[field]
                if live:
                    item["quote_source"] = live.get("source") or "live_snapshot"
                item["watch_pool_frozen"] = code not in current_by_code
                picked.append(item)
            meta = {
                "version": "watch-pool-v2",
                "frozen_at": state.get("frozen_at"),
                "pool": pool,
                "replaced_today": state.get("replaced") or [],
                "max_replacements": _MAIN_FORCE_POOL_MAX_REPLACEMENTS,
                "raw_candidate_count": len(ranked),
                "returned_count": len(picked),
                "frozen_carry_count": sum(1 for item in picked if item.get("watch_pool_frozen")),
            }
            return picked, meta
    except Exception:
        return picks, None


def _save_watch_pool(state):
    try:
        tmp = MAIN_FORCE_WATCH_POOL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(tmp, MAIN_FORCE_WATCH_POOL_PATH)
    except OSError:
        pass


def _ignition_shadow_store(rows):
    """影子对比记录（ignition-v1 shadow）：原规则 vs 点火规则同刻判定。"""
    rows = [row for row in (rows or []) if row]
    if not rows:
        return
    try:
        with _db(immediate=True) as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO paper_ignition_shadow(
                       day,bucket,code,recorded_at,price,pct,runup,
                       old_rule_passed,old_rule_reason,
                       ignition_passed,ignition_reasons)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        row["day"], row["bucket"], row["code"], row["recorded_at"],
                        row.get("price"), row.get("pct"), row.get("runup"),
                        int(bool(row.get("old_rule_passed"))), row.get("old_rule_reason"),
                        int(bool(row.get("ignition_passed"))),
                        json.dumps(row.get("ignition_reasons") or [], ensure_ascii=False),
                    )
                    for row in rows
                ],
            )
    except Exception:
        pass  # 影子记录绝不影响主链路


def _ignition_shadow_backfill(asof_day=None, quote_fetch=None):
    """回填影子记录的 +30/+60 分钟表现，供两规则命中后对比。

    回溯用回填时刻的最新报价，精度受 3 分钟扫描间隔限制；记录中同时
    保存实际回填时间（at_30m/at_60m）以便评估精度。
    """
    now = dt.datetime.now()
    try:
        with _db(immediate=True) as conn:
            pending = _rows(
                conn,
                """SELECT * FROM paper_ignition_shadow
                   WHERE resolved=0 AND recorded_at <= ?
                   ORDER BY id LIMIT 80""",
                ((now - dt.timedelta(minutes=30)).isoformat(timespec="seconds"),),
            )
            if not pending:
                return {"updated": 0}
            codes = sorted({str(row["code"]) for row in pending})
            if quote_fetch is None:
                quote_map = _quotes(codes, asof_date=asof_day or now.date())
            else:
                quote_map = quote_fetch(codes)
            updated = 0
            for row in pending:
                recorded = dt.datetime.fromisoformat(str(row["recorded_at"])[:19])
                quote = dict(quote_map.get(str(row["code"])) or {})
                price_now = _num(quote.get("price"), 0.0)
                if price_now <= 0:
                    continue
                age_min = (now - recorded).total_seconds() / 60
                sets, params = [], []
                if price_now and _num(row["price_30m"], 0.0) <= 0 and age_min >= 30:
                    sets.append("price_30m=?"); params.append(price_now)
                    sets.append("at_30m=?"); params.append(now.isoformat(timespec="seconds"))
                if price_now and _num(row["price_60m"], 0.0) <= 0 and age_min >= 60:
                    sets.append("price_60m=?"); params.append(price_now)
                    sets.append("at_60m=?"); params.append(now.isoformat(timespec="seconds"))
                    sets.append("resolved=1")
                if sets:
                    params.append(row["id"])
                    conn.execute(
                        f"UPDATE paper_ignition_shadow SET {', '.join(sets)} WHERE id=?",
                        tuple(params),
                    )
                    updated += 1
            return {"updated": updated, "pending": len(pending)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def ignition_shadow_report(asof_day=None, limit=200):
    """影子对比汇总：两规则命中后 30/60 分钟的平均表现。"""
    day = _date(asof_day or dt.date.today()).isoformat()
    try:
        with _db() as conn:
            rows = _rows(
                conn,
                """SELECT * FROM paper_ignition_shadow WHERE day=?
                   ORDER BY id DESC LIMIT ?""",
                (day, max(10, int(limit))),
            )
    except Exception as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    def cohort(passed_key):
        sample = [row for row in rows if row[passed_key] and _num(row["price_30m"], 0.0) > 0]
        rets30, rets60 = [], []
        for row in sample:
            base = _num(row["price"], 0.0)
            if base <= 0:
                continue
            rets30.append((_num(row["price_30m"]) / base - 1) * 100)
            if _num(row["price_60m"], 0.0) > 0:
                rets60.append((_num(row["price_60m"]) / base - 1) * 100)
        return {
            "hits": len(sample),
            "avg_30m_pct": round(sum(rets30) / len(rets30), 3) if rets30 else None,
            "avg_60m_pct": round(sum(rets60) / len(rets60), 3) if rets60 else None,
        }

    overlap = [
        row for row in rows
        if row["old_rule_passed"] and row["ignition_passed"]
    ]
    return {
        "status": "ok", "day": day, "records": len(rows),
        "old_rule": cohort("old_rule_passed"),
        "ignition_rule": cohort("ignition_passed"),
        "overlap": len(overlap),
        "note": "影子对比：点火规则正式放开前，用命中后30/60分钟表现对比原规则",
        "recent": [
            {
                "code": row["code"], "recorded_at": row["recorded_at"],
                "price": row["price"], "pct": row["pct"],
                "old_rule_passed": bool(row["old_rule_passed"]),
                "ignition_passed": bool(row["ignition_passed"]),
                "ret_30m_pct": (
                    round((_num(row["price_30m"]) / _num(row["price"]) - 1) * 100, 2)
                    if _num(row["price_30m"], 0.0) > 0 and _num(row["price"], 0.0) > 0 else None
                ),
                "ret_60m_pct": (
                    round((_num(row["price_60m"]) / _num(row["price"]) - 1) * 100, 2)
                    if _num(row["price_60m"], 0.0) > 0 and _num(row["price"], 0.0) > 0 else None
                ),
            }
            for row in rows[:40]
        ],
    }


def _candidate_rows(account, asof_date, market, sector_rows=None, live_universe=None, live_asof_date=None):
    """用完整历史因子排序，并用当日实时快照覆盖可变字段。

    ``asof_date`` 是历史因子截止日；``live_asof_date`` 允许盘中用当天
    实时行情而不误要求当天尚未收盘的日线因子。
    """
    price_f, first_board_codes = _selection_inputs()
    account_id = account["id"] if isinstance(account, dict) else str(account)
    spec = ACCOUNT_SPECS[account_id]
    base_universe = {str(row.get("code")): row for row in (U.load_universe() or [])}
    factor_freshness = _selection_factor_freshness(price_f, list(base_universe.values()), asof_date)
    if not factor_freshness.get("passed"):
        return [], {
            "blocked": True,
            "reason": factor_freshness.get("reason") or "历史因子新鲜度门禁未通过",
            "factor_freshness": factor_freshness,
        }
    eligible_factor_universe = {
        code for code, row in base_universe.items()
        if code and _security_scope(code, row.get("name"), row.get("risk_flag"))["allowed"]
    }
    if "last_date" not in price_f:
        return [], {"blocked": True, "reason": "选股因子缺少数据日期"}
    factor_dates = price_f["last_date"].astype(str).str[:10]
    # 盘中/周一不应把自然日直接当因子滞后日；以最近完整收盘日
    # 计算，避免周末后把周四因子误判为超过策略时效。
    try:
        lag_reference_day = U.latest_complete_trade_date(_date(asof_date))
    except Exception:
        lag_reference_day = _date(asof_date)
    lags = factor_dates.map(lambda value: _trading_weekday_lag(value, lag_reference_day))
    usable = lags.notna() & (lags <= spec["max_factor_lag"])
    total_factor_rows = len(price_f)
    dropped_stale = int((~usable).sum())
    price_f = price_f[usable].copy()
    if price_f.empty:
        latest = factor_dates.max() if len(factor_dates) else None
        return [], {
            "blocked": True,
            "reason": f"{spec['entry_model_name']}没有满足 {spec['max_factor_lag']} 个工作日时效要求的因子；最新 {latest or '未知'}",
        }
    usable_coverage = len(price_f) / max(total_factor_rows, 1)
    eligible_usable_codes = set(price_f.index.astype(str)) & eligible_factor_universe
    eligible_factor_coverage = len(eligible_usable_codes) / max(len(eligible_factor_universe), 1)
    if (
        len(price_f) < CANDIDATE_FACTOR_MIN_ROWS
        or usable_coverage < CANDIDATE_FACTOR_MIN_COVERAGE
        or eligible_factor_coverage < CANDIDATE_FACTOR_MIN_COVERAGE
    ):
        return [], {
            "blocked": True,
            "reason": (
                f"{spec['entry_model_name']}全市场因子覆盖不足：有效 {len(price_f)} / {total_factor_rows} "
                f"（{usable_coverage * 100:.1f}%），可交易范围 {len(eligible_usable_codes)} / "
                f"{len(eligible_factor_universe)}（{eligible_factor_coverage * 100:.1f}%），需至少 "
                f"{CANDIDATE_FACTOR_MIN_COVERAGE * 100:.0f}%；本轮不使用残缺样本选股"
            ),
            "usable_factor_rows": len(price_f), "total_factor_rows": total_factor_rows,
            "factor_coverage_pct": round(usable_coverage * 100, 2),
            "eligible_factor_rows": len(eligible_usable_codes),
            "eligible_universe_rows": len(eligible_factor_universe),
            "eligible_factor_coverage_pct": round(eligible_factor_coverage * 100, 2),
            "dropped_stale_rows": dropped_stale,
        }
    factor_date = str(price_f["last_date"].dropna().max())
    factor_oldest_date = str(price_f["last_date"].dropna().min())
    style = (account.get("style") if isinstance(account, dict) else None) or spec["default_style"]
    profile = STYLE_PROFILES.get(style, STYLE_PROFILES[spec["default_style"]])
    # These metadata objects are populated only by the sector strategy, but
    # the common return payload is shared by all four strategies.  Always
    # initialise them so a non-sector scan cannot abort after completing the
    # expensive full-market factor build.
    surge_meta = None
    ths_meta = None
    concept_meta = None
    expected_live_day = _date(live_asof_date if live_asof_date is not None else asof_date).isoformat()
    live_map = {}
    live_scan = {
        "status": "historical_only" if live_universe is None else "blocked",
        "expected_quote_day": expected_live_day,
        "eligible_rows": len(eligible_factor_universe),
        "covered_rows": 0,
        "coverage_pct": 0.0,
        "required_rows": None,
        "scope": "eligible_a_share_universe",
    }
    for row in (live_universe or []):
        code = str(row.get("code") or "")
        if not code or str(row.get("quote_at") or "")[:10] != expected_live_day:
            continue
        try:
            if pd.isna(row.get("price")) or pd.isna(row.get("pct")) or float(row.get("price")) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        live_map[code] = row
    if live_universe is not None:
        # A live cross-sectional scan must never rank a code using persisted
        # price/pct/flow fields.  Metadata may come from the universe file,
        # but missing live quotes are excluded from this run entirely.
        covered_live_codes = set(live_map) & eligible_factor_universe
        required_live_rows = max(CANDIDATE_FACTOR_MIN_ROWS,
                                 int(len(eligible_factor_universe) * CANDIDATE_FACTOR_MIN_COVERAGE + 0.999999))
        live_scan.update({
            "required_rows": required_live_rows,
            "covered_rows": len(covered_live_codes),
            "coverage_pct": round(len(covered_live_codes) / max(len(eligible_factor_universe), 1) * 100, 2),
            "status": "fresh_day_coverage" if len(covered_live_codes) >= required_live_rows else "blocked",
        })
        if len(covered_live_codes) < required_live_rows:
            return [], {
                "blocked": True,
                "reason": f"当日实时行情覆盖不足：{len(covered_live_codes)} / {len(eligible_factor_universe)}，需至少 {required_live_rows} 只",
                "live_quote_day": expected_live_day,
                "live_quote_rows": len(covered_live_codes),
                "live_eligible_rows": len(eligible_factor_universe),
                "live_coverage_pct": round(len(covered_live_codes) / max(len(eligible_factor_universe), 1) * 100, 2),
                "full_market_scan": live_scan,
            }
        live_codes = set(live_map)
        price_f = price_f.loc[price_f.index.astype(str).isin(live_codes)].copy()
        if price_f.empty:
            return [], {"blocked": True, "reason": "当日实时行情未覆盖任何有效因子股票"}
    universe = []
    for code in price_f.index:
        base = dict(base_universe.get(str(code)) or {"code": str(code)})
        if str(code) in live_map:
            base.update(live_map[str(code)])
        universe.append(base)
    if live_map:
        # 技术因子仍来自已收盘日线；实时价格/涨跌幅/资金字段用于当日排序和容量。
        for code, row in live_map.items():
            if code in price_f.index and isinstance(row.get("price"), (int, float)):
                price_f.loc[code, "price"] = row["price"]
                # amount/turnover 是实时-only 字段：CSV 因子缓存从不持久化它们，
                # build_factor_table 直接从 price 读取。若不在此处注入，函数内的
                # factor_columns_missing 检查会在每次扫描时误报（策略实际消费
                # 的是注入后的 table，候选并不为空）。与公开选股路径
                # (main.py live_field_map) 保持同一注入时机。
                for _fld in ("amount", "turnover"):
                    _v = row.get(_fld)
                    if isinstance(_v, (int, float)):
                        price_f.loc[code, _fld] = _v
    # Use the same financial feed as public selection.  The report-period
    # values remain compatible with the running strategies; publication-time
    # proof is attached below to the concrete candidate records instead of
    # pretending a report period is a disclosure timestamp.
    try:
        finance = dfc.fetch_finance_latest()
    except Exception:
        finance = {"data": {}}
    # Keep the paper selector point-in-time consistent with the public
    # selector: a report/period that was not visible at the completed
    # signal date must not satisfy a profit gate.  The factor layer retains
    # unknown disclosures as shadow evidence, while strategies require the
    # explicit ``reported`` source for executable profit thresholds.
    # The new three-day strategy needs the raw reported profit values in order
    # to build its shadow lane.  An unknown publication timestamp must never be
    # treated as reported, but hiding the value before the strategy runs makes
    # it impossible to ask the disclosure source for confirmation and leaves
    # the account permanently at zero candidates.  Keep those values as
    # ``shadow`` for this one model; the strategy still requires an explicit
    # publication record before a formal pick is returned.
    finance_asof = None if account_id == NEW_STRATEGY_ID else asof_date
    fund = F.compute_fundamental_factors(universe, finance, asof=finance_asof)
    live_flow = {
        str(code): row.get("main_pct")
        for code, row in live_map.items()
        if isinstance(row.get("main_pct"), (int, float))
    }
    live_super_flow = {
        str(code): row.get("super_net")
        for code, row in live_map.items()
        if isinstance(row.get("super_net"), (int, float))
    }
    # 补齐缺失的超大单/主力资金数据。底层是 60 秒全市场共享快照，
    # 必须覆盖所有有效候选而非旧版的前 100 只，否则后排股票会长期
    # 以“无超大单资金”反复被审计拒绝。
    _flow_supplemented_codes = set()
    _missing_super_codes = {
        str(code) for code in eligible_factor_universe
        if code in live_map and (
            not isinstance(live_map[code].get("super_net"), (int, float))
            or not isinstance(live_map[code].get("main_pct"), (int, float))
        )
    }
    if _missing_super_codes:
        try:
            _supplemented = dfc.enrich_live_flow_details(live_universe, _missing_super_codes)
            for _code, _detail in _supplemented.items():
                if _code in live_map:
                    for _flow_field in ("super_net", "main_net", "big_net", "mid_net", "small_net", "main_pct"):
                        if not isinstance(live_map[_code].get(_flow_field), (int, float)) and isinstance(_detail.get(_flow_field), (int, float)):
                            live_map[_code][_flow_field] = _detail[_flow_field]
                    live_map[_code]["flow_source"] = _detail.get("flow_source")
                    live_map[_code]["flow_quote_at"] = _detail.get("quote_at") or _detail.get("flow_fetched_at")
                    _flow_supplemented_codes.add(_code)
                if isinstance(_detail.get("super_net"), (int, float)):
                    live_super_flow[_code] = _detail["super_net"]
                if isinstance(_detail.get("main_pct"), (int, float)):
                    live_flow[_code] = _detail["main_pct"]
        except Exception:
            pass
    table = S.build_factor_table(
        price_f, fund, sentiment=None,
        realtime_flow=live_flow,
        realtime_super_flow=live_super_flow,
    )
    if live_universe is not None:
        # 日线技术指标/财务仍来自已下载的完整历史；当天可变字段必须由
        # 同一轮实时快照覆盖，不能继续沿用上一交易日的收盘价或涨跌幅。
        live_field_map = {
            "price": "price", "pct": "pct", "amount": "amount",
            "turnover": "turnover", "main_pct": "main_pct",
            "super_net_raw": "super_net", "main_net": "main_net",
        }
        for code, row in live_map.items():
            if code not in table.index:
                continue
            for target, source in live_field_map.items():
                value = row.get(source)
                if value is not None and str(value).strip() not in {"", "--", "-"}:
                    table.loc[code, target] = value
        table["quote_at"] = pd.Series(
            {code: (live_map.get(str(code)) or {}).get("quote_at") for code in table.index}
        )
        table["quote_source"] = pd.Series(
            {code: (live_map.get(str(code)) or {}).get("source") or "live_snapshot" for code in table.index}
        )
        table["flow_source"] = pd.Series(
            {code: (live_map.get(str(code)) or {}).get("flow_source") or "eastmoney_snapshot" for code in table.index}
        )
        table["flow_quote_at"] = pd.Series(
            {code: (live_map.get(str(code)) or {}).get("flow_quote_at") or (live_map.get(str(code)) or {}).get("quote_at") for code in table.index}
        )
        table["historical_factor_date"] = factor_date
    star_sector_impulse = _star_sector_impulse(universe, asof_date)
    eligible_codes = [
        code for code in table.index
        if _security_scope(
            code,
            table.loc[code, "name"] if "name" in table else (base_universe.get(str(code)) or {}).get("name"),
            (base_universe.get(str(code)) or {}).get("risk_flag"),
        )["allowed"]
    ]
    table = table.loc[eligible_codes].copy()
    # The paper execution path currently has no authoritative disclosure
    # timestamp for financial reports.  Keep profit values visible in the
    # candidate snapshot, but never pretend they are point-in-time proven.
    # This makes the limitation explicit in audit/research without silently
    # changing the three strategy rule set to an empty universe.
    if "profit_source" in table:
        table["profit_source"] = table["profit_source"].fillna("unknown")
    table["star_sector_bonus"] = pd.Series({
        code: _num((star_sector_impulse.get(_sector_key(table.loc[code, "industry"])) or {}).get("bonus"), 0.0)
        for code in table.index
    }).reindex(table.index).fillna(0.0)
    # 外部快照中的停牌/缺失值可能以 ``--`` 等字符串进入表格。策略比较前
    # 统一清洗所有数值列，防止一次脏值中断整轮 5 分钟全市场扫描。
    numeric_columns = (
        "price", "pct", "amount", "turnover", "main_pct", "super_net",
        "flow", "mom5_raw", "mom20_raw", "mom60_raw", "vol_surge_raw",
        "rsi14_raw", "pe", "pb", "roe", "profit_yoy",
    )
    for column in numeric_columns:
        if column in table:
            table[column] = pd.to_numeric(table[column], errors="coerce")
    # 板块热度不仅服务于板块轮动策略，也作为三个模拟策略的同日
    # “热门启动段”上下文。它只进入排序/审计，不改变证券权限和下单硬门禁。
    sector_heat = _sector_heat_map(universe, sector_rows)
    if sector_heat:
        table["sector_heat_score"] = pd.Series(
            {
                code: _num(
                    (sector_heat.get(_sector_key(table.loc[code, "industry"])) or {}).get("score"),
                    0.0,
                )
                for code in table.index
            }
        ).reindex(table.index).fillna(0.0)
        table["sector_early_rotation"] = pd.Series(
            {
                code: bool(
                    (sector_heat.get(_sector_key(table.loc[code, "industry"])) or {}).get("early_rotation")
                )
                for code in table.index
            }
        ).reindex(table.index).fillna(False)
        table["sector_early_rotation_score"] = pd.Series(
            {
                code: _num(
                    (sector_heat.get(_sector_key(table.loc[code, "industry"])) or {}).get("early_rotation_score"),
                    0.0,
                )
                for code in table.index
            }
        ).reindex(table.index).fillna(0.0)
    else:
        table["sector_heat_score"] = 0.0
        table["sector_early_rotation"] = False
        table["sector_early_rotation_score"] = 0.0
    if style == "sector":
        heat_values = pd.Series(
            {
                code: _num((sector_heat.get(_sector_key(table.loc[code, "industry"])) or {}).get("score"), -1.5)
                for code in table.index
            }
        )
        table["sentiment"] = F.zscore(heat_values).reindex(table.index).fillna(-1.5)
    selection_overlay = _adaptive_selection(account)
    selection_model = selection_overlay.get("model_family") or profile["source_strategy"]
    available_models = set(getattr(S, "PAPER_WEIGHTS", {}) or {}) | set(getattr(S, "STRATEGIES", {}) or {})
    if selection_model not in available_models:
        # The strategy package may be deployed independently from the paper
        # executor.  Fail closed and leave an auditable blocked scan instead of
        # silently falling back to one of the three legacy models.
        return [], {
            "blocked": True,
            "reason": f"纸盘策略模型 {selection_model} 尚未在 strategies 模块注册；本轮不生成候选或成交",
            "model_family": selection_model,
            "strategy_id": account_id,
        }
    # 主力观察池：多请求 3 只替补位（top10 掉榜时用 11-13 名补位/保留），
    # 实际池大小仍为 10，由 _main_force_watch_pool 冻结与替换。
    candidate_topn = (
        _MAIN_FORCE_POOL_SIZE + _MAIN_FORCE_POOL_BUFFER
        if account_id == MAIN_FORCE_STRATEGY_ID
        else (120 if style == "pullback" else 48)
    )
    raw = S.run_strategy(selection_model, table, topn=candidate_topn, news_hits=[], auto_news=False,
                         gate={"light": market["light"]}, first_board_codes=first_board_codes,
                         weight_overrides=selection_overlay.get("weights"),
                         condition_overrides=selection_overlay.get("conditions"))
    disclosure_prime = {"status": "not_required", "requested": 0, "reported": 0}
    if account_id == NEW_STRATEGY_ID and not (raw.get("picks") or []):
        # First pass deliberately produces a bounded shadow set.  Resolve its
        # publication timestamps in batches, then rerun the deterministic
        # strategy so only proven reports enter the executable lane.
        shadow_codes = [str(item.get("code") or "") for item in (raw.get("shadow_picks") or [])]
        shadow_codes = list(dict.fromkeys(code for code in shadow_codes if code))[:120]
        disclosure_prime = {
            "status": "unavailable" if DT is None else "pending",
            "requested": len(shadow_codes), "reported": 0,
        }
        if DT is not None and shadow_codes:
            try:
                records = DT.fetch_disclosure_timeline(shadow_codes, asof=asof_date)
                evidence = {str(item.get("code")): item for item in (records or []) if item.get("code")}
                for code, evidence_row in evidence.items():
                    published_at = evidence_row.get("published_at")
                    status = str(evidence_row.get("status") or "unknown")
                    source = str(evidence_row.get("source") or "unknown")
                    if not published_at or status in {"unknown", "error"} or source == "unknown" or code not in table.index:
                        continue
                    table.loc[code, "profit_source"] = "reported"
                    table.loc[code, "report_published_at"] = published_at
                    period = str(evidence_row.get("report_period") or "")
                    if period.endswith("12-31"):
                        table.loc[code, "annual_report_published_at"] = published_at
                    disclosure_prime["reported"] += 1
                disclosure_prime["status"] = "ready" if disclosure_prime["reported"] else "partial"
                if disclosure_prime["reported"]:
                    raw = S.run_strategy(selection_model, table, topn=candidate_topn, news_hits=[], auto_news=False,
                                         gate={"light": market["light"]}, first_board_codes=first_board_codes,
                                         weight_overrides=selection_overlay.get("weights"),
                                         condition_overrides=selection_overlay.get("conditions"))
            except Exception as exc:
                disclosure_prime.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    raw.setdefault("metadata", {})["disclosure_prime"] = disclosure_prime
    picks = list(raw.get("picks", []) or [])
    # The strategy layer exposes a bounded early-hot lane in addition to its
    # normal top-N.  Merge it here so it receives the exact same execution
    # approval and risk checks; it is not a bypass or an immediate order.
    seen_pick_codes = {str(item.get("code") or "") for item in picks}
    for watch_pick in (raw.get("hot_leader_watch") or []):
        watch_code = str(watch_pick.get("code") or "")
        if watch_code and watch_code not in seen_pick_codes:
            picks.append(watch_pick)
            seen_pick_codes.add(watch_code)
    for pick in picks:
        if account_id == NEW_STRATEGY_ID:
            # strategies.run_strategy intentionally keeps pick payloads small;
            # copy the canonical hard-gate evidence into the execution pick so
            # paper approval can re-check it rather than trusting a label.
            pick_code = str(pick.get("code") or "")
            row = next((table.loc[index] for index in table.index if str(index) == pick_code), None)
            if row is not None:
                for field in (
                    "three_up", "boll_mid_breakout", "above_ma5_5d",
                    "above_ma5", "above_ma10", "above_ma20", "above_ma60",
                    "above_all_ma", "profit_source", "report_date",
                    "report_published_at", "annual_report_date",
                    "annual_report_published_at", "net_profit", "annual_net_profit",
                    "super_net_raw",
                ):
                    if field in table.columns and field not in pick:
                        value = row.get(field)
                        if pd.notna(value):
                            pick[field] = value.item() if hasattr(value, "item") else value
                pick.setdefault("strategy_id", NEW_STRATEGY_ID)
        live_row = live_map.get(str(pick.get("code"))) if live_universe is not None else None
        if live_row is not None:
            pick["quote_at"] = live_row.get("quote_at")
            pick["quote_source"] = live_row.get("source") or "live_snapshot"
            pick["quote_data_scope"] = "当日实时行情；技术指标使用前一完整交易日下载K线"
            pick["historical_factor_date"] = factor_date
        impulse = star_sector_impulse.get(_sector_key(pick.get("industry")))
        if impulse:
            pick["star_sector_signal"] = impulse
            pick.setdefault("reasons", []).append(
                f"科创板同业中位涨幅 {impulse['median_pct']:+.2f}%，映射加分 {impulse['bonus']:.3f}"
            )
    watch_pool_meta = None
    if account_id == MAIN_FORCE_STRATEGY_ID:
        # 每日10只观察池冻结：早盘/首次有效扫描定池，盘中最多替换2只，
        # 避免三分钟重算导致候选并集漂移（2026-08-31 复核 P1）。
        picks, watch_pool_meta = _main_force_watch_pool(
            picks, asof_day=live_asof_date or asof_date, live_map=live_map,
        )
    raw_candidate_count = len(picks)
    entry_screened = []
    entry_screened_reasons = []
    shadow_rows = []
    for pick in picks:
        allowed, reason = _new_entry_price_gate(account, pick)
        if allowed:
            entry_screened.append(pick)
        else:
            entry_screened_reasons.append(reason)
        if account_id == MAIN_FORCE_STRATEGY_ID and IGN is not None:
            # 影子对比（同刻双规则判定，不产生交易）：old_rule 还原改造前的
            # 追高上限行为；ignition 为八项点火条件。命中后 30/60 分钟表现
            # 由 _ignition_shadow_backfill 回填，作为放开点火买入前的依据。
            try:
                code = str(pick.get("code") or "")
                pct_s = _num(pick.get("pct"), None)
                price_s = _num(pick.get("price"), 0.0)
                open_s = _num(pick.get("open"), 0.0)
                runup_s = (price_s / open_s - 1) if price_s > 0 and open_s > 0 else None
                spec_mf = ACCOUNT_SPECS[account_id]
                limit_s = _limit_pct(code)
                buffer_s = 1.0 if limit_s <= 10.0 else 2.0
                old_rejected = False
                if pct_s is not None and pct_s >= limit_s - buffer_s:
                    old_rejected, old_reason = True, "接近涨停"
                elif pct_s is not None and pct_s > _num(spec_mf.get("entry_pct_high"), 8.8):
                    old_rejected, old_reason = True, f"涨幅超旧执行上限 +{_num(spec_mf.get('entry_pct_high'), 8.8)}%"
                elif runup_s is not None and runup_s > spec_mf.get("max_open_runup_pct", 0.035) + _open_runup_bonus(now=None):
                    old_rejected, old_reason = True, f"开盘涨幅超旧追高上限 {runup_s*100:+.2f}%"
                elif pct_s is not None and pct_s > 3.5:
                    old_rejected, old_reason = True, "旧规则：开盘涨幅>3.5%进入追高拒绝区"
                else:
                    old_reason = "旧规则放行"
                if pct_s is not None and 3.5 <= pct_s <= 7.5:
                    ignition = IGN.evaluate_ignition(code, pct=pct_s, limit_pct=limit_s)
                    ignition_passed = bool(ignition.get("passed"))
                    ignition_reasons = list(ignition.get("reasons") or [])
                else:
                    ignition_passed = False
                    ignition_reasons = ["非点火区间"]
                shadow_rows.append({
                    # day 用扫描/交易当日而非因子日：_candidate_rows 的
                    # asof_date 在盘中引导路径是"上一完整交易日因子快照"，
                    # 若打因子日，当日影子汇总/回填口径会错日（2026-08-31
                    # 线上发现记录全落在 08-28）。
                    "day": dt.date.today().isoformat(),
                    "bucket": _intraday_business_key(now=None),
                    "code": code, "recorded_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "price": price_s, "pct": pct_s, "runup": runup_s,
                    "old_rule_passed": not old_rejected, "old_rule_reason": old_reason,
                    "ignition_passed": ignition_passed, "ignition_reasons": ignition_reasons,
                })
            except Exception:
                pass  # 影子记录 fail-open
    if shadow_rows:
        _ignition_shadow_store(shadow_rows)
    picks = entry_screened
    picks, financial_evidence = _attach_candidate_financial_disclosure(picks, asof_date)
    if style == "pullback":
        # 趋势波段不是只买“已经完全多头排列”的股票，否则市场处于
        # 轮动/修复阶段时候选会被压到个位数。保留温和过渡期的回踩股，
        # 最终仍由趋势结构、回踩位置、资金稳定和 Q1-Q3 再次确认。
        picks = [
            p for p in picks
            if -5 <= _num(p.get("mom20"), -999) <= 30
            and -10 <= _num(p.get("mom5"), -999) <= 15
            and 35 <= _num(price_f.loc[p["code"], "rsi14"], 0) <= 75
        ]
        # ``bottom_reversal`` is intentionally retained as the conservative
        # base model, but it does not know today's MA20 distance.  Rank the
        # resulting pool by the actual completed-K-line trend structure before
        # spending the small live approval budget.  This prevents a momentum
        # leader already 10% above MA20 from repeatedly occupying the first
        # slots of a *pullback* strategy.
        trend_ranked = []
        for pick in picks:
            kline = _completed_kline(pick.get("code"), factor_date)
            try:
                closes = pd.to_numeric(kline["close"], errors="coerce").dropna()
            except (KeyError, TypeError, ValueError):
                closes = pd.Series(dtype=float)
            if len(closes) < 60:
                continue
            ma20 = _num(closes.tail(20).mean(), None)
            ma60 = _num(closes.tail(60).mean(), None)
            price = _num(pick.get("price"), None)
            if not ma20 or not ma60 or not price:
                continue
            distance = price / ma20 - 1.0
            # These are the same outer bounds as the execution model.  They
            # are a pre-ranking filter, not a new buy rule, so a real-time
            # quote is still independently checked below.
            if ma20 < ma60 * 0.93 or distance < -0.10 or distance > 0.14:
                continue
            structure = 1.0 if ma20 >= ma60 else 0.72
            pullback_fit = (
                1.0 if -0.03 <= distance <= 0.05
                else 0.55 if -0.06 <= distance <= 0.09
                else 0.25
            )
            pool_score = 0.62 * pullback_fit + 0.28 * structure + 0.10 * max(
                0.0, min(1.0, 1.0 - abs(_num(pick.get("mom20")) - 12.0) / 18.0)
            )
            item = dict(pick)
            # P3 双路径拆分：mom20<0 是底部启动（结构尚未修复，小仓试错
            # 确认后加仓）；mom20≥0 是多头回踩（结构已立，回踩承接直接
            # 建仓）。两条路径时机不同，不再让同一评分混排两种时机。
            item["trend_path"] = "bottom_start" if _num(pick.get("mom20"), 0) < 0 else "pullback"
            item["trend_candidate_context"] = {
                "version": "trend-pullback-pool-v2",
                "trend_path": item["trend_path"],
                "ma20": round(ma20, 3), "ma60": round(ma60, 3),
                "distance_ma20_pct": round(distance * 100, 3),
                "structure_score": round(structure, 3),
                "pullback_fit": round(pullback_fit, 3),
                "pool_score": round(pool_score, 3),
            }
            # The selection score remains evidence; the pool score only
            # determines which candidates deserve the limited live checks.
            trend_ranked.append((pool_score, _num(item.get("score")), item))
        trend_ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        picks = [item for _, _, item in trend_ranked]
    style_filtered_count = len(picks)
    if style == "sector":
        enriched = []
        for pick in picks:
            heat = sector_heat.get(_sector_key(pick.get("industry")))
            individual_path = pick.get("entry_path") == "individual_strong"
            if heat and heat["rank"] <= 15 and heat["pct"] > 0:
                enriched.append({
                    **pick, "sector_heat": heat, "entry_path": "sector_heat",
                    "sector_threshold_context": _sector_entry_threshold_context(sector_heat, market, heat),
                })
            elif individual_path:
                # 板块热度不足只降低优先级，不否决个股极强；最终仍需
                # 独立行情、Q级、资金、量能和单票风险全部通过。
                enriched.append({
                    **pick,
                    "sector_heat": heat or {"rank": 999, "pct": 0.0, "score": -1.0, "source": "无板块热度"},
                    "entry_path": "individual_strong",
                    "sector_threshold_context": _sector_entry_threshold_context(sector_heat, market, heat),
                })
        # 真实概念资金榜 -> 完整成分 -> 当日全市场实时行情。概念接口
        # 只负责发现，不能替代全市场实时价格与资金门禁。
        try:
            concepts = dfc.fetch_hot_concept_snapshot(topn=6)
        except Exception as exc:
            concepts = []
            concept_meta = {"version": CONCEPT_EXPANSION_LANE_VERSION,
                            "injected": 0, "source_error": f"{type(exc).__name__}: {exc}"}
        # 反向题材扩散：先从少数接近涨停且资金、量能确认的领涨股反查其
        # 概念，再只观察其中尚未涨停的同概念成员。这样不会遗漏尚未进入
        # 资金榜前六的液冷等早期题材；领涨股本身明确排除，仍不追板。
        reverse_leaders = _concept_reverse_map_leaders(universe, live_map)
        reverse_concepts = []
        reverse_error = None
        if reverse_leaders:
            try:
                reverse_concepts = dfc.fetch_leader_concept_snapshot(reverse_leaders)
            except Exception as exc:
                reverse_error = f"{type(exc).__name__}: {exc}"
        # Preserve reverse-discovery evidence even when the board is already
        # in the flow top-N.  Otherwise a hot board would silently lose the
        # leader code that must be excluded from peer selection/audit.
        reverse_by_code = {str(item.get("code") or ""): item for item in reverse_concepts}
        merged_concepts = []
        for concept in concepts:
            reverse = reverse_by_code.pop(str(concept.get("code") or ""), None)
            if reverse:
                merged_concepts.append({
                    **concept,
                    "leader_context": reverse.get("leader_context") or [],
                    "active_peer_count": reverse.get("active_peer_count", 0),
                    "member_count": reverse.get("member_count", concept.get("member_count", 0)),
                })
            else:
                merged_concepts.append(concept)
        concepts = merged_concepts + list(reverse_by_code.values())
        concept_lane, lane_meta = _concept_expansion_lane_candidates(
            concepts, universe, live_map,
            seen_codes={str(p.get("code") or "") for p in enriched},
            market=market, asof_date=asof_date,
        )
        if concept_meta:
            concept_meta = {**lane_meta, **concept_meta}
        else:
            concept_meta = lane_meta
        concept_meta["reverse_leader_discovery"] = {
            "leaders": reverse_leaders,
            "concepts_found": [
                {"code": item.get("code"), "name": item.get("name"),
                 "member_count": item.get("member_count"),
                 "positive_ratio": item.get("positive_ratio"),
                 "active_peer_count": item.get("active_peer_count")}
                for item in reverse_concepts
            ],
            "source_error": reverse_error,
            "note": "强势领涨股反查概念，仅用于同概念未涨停股发现；领涨股不进入候选",
        }
        if concept_lane:
            enriched.extend(concept_lane)
        # 盘中板块异动直通车道：主题日内引爆时不再等隔夜因子排名追认。
        # 注入候选与常规候选一样经过入场筛选/追高闸门/Q级/资金全部校验。
        surge_lane, surge_meta = _sector_surge_lane_candidates(
            sector_heat, universe, live_map,
            seen_codes={str(p.get("code") or "") for p in enriched},
            market=market,
        )
        if surge_lane:
            enriched.extend(surge_lane)
        # 同花顺热点题材车道（P0）：人工运营的当日强势股+题材归因标签，
        # 比板块涨幅排名更早更快。命中当日热点榜的成员直接注入候选，
        # 题材标签随 payload 入审计；追高上限与全部执行闸门照常生效。
        ths_lane, ths_meta = _ths_hot_lane_candidates(
            universe, live_map,
            seen_codes={str(p.get("code") or "") for p in enriched},
            asof_date=asof_date,
        )
        if ths_lane:
            enriched.extend(ths_lane)
        picks = enriched
    if spec["mode"] == "intraday_t":
        picks = [p for p in picks if _asset_type(p.get("code"), p.get("name")) == "stock_t1"]
    risk_codes = {
        str(row.get("code")) for row in universe
        if row.get("risk_flag") or "ST" in str(row.get("name") or "").upper() or "退" in str(row.get("name") or "")
    }
    picks = [pick for pick in picks if pick["code"] not in risk_codes]
    # 容量以策略/共享池席位为主。一手金额过高不得在候选阶段静默删票；
    # _price_aware_qty 会在执行时按真实现金、风险预算和整手规则决定数量，
    # 不足一手则进入可重排等待池。
    picks = [pick for pick in picks if _num(pick.get("price")) > 0]
    # Reserve a few places for the early-hot lane so a normal top-N block
    # cannot erase the very candidates this lane was introduced to surface.
    reserved_statuses = {"hot_leader_watch", "concept_expansion_lane",
                         "sector_surge_lane", "ths_hot_lane"}
    hot_watch = [pick for pick in picks if pick.get("candidate_status") in reserved_statuses]
    regular = [pick for pick in picks if pick.get("candidate_status") not in reserved_statuses]
    hot_watch = hot_watch[:6]
    picks = regular[:max(0, 30 - len(hot_watch))] + hot_watch
    return picks, {
        "blocked": False, "factor_date": factor_date, "factor_oldest_date": factor_oldest_date,
        "usable_factor_rows": len(price_f), "total_factor_rows": total_factor_rows,
        "factor_coverage_pct": round(usable_coverage * 100, 2), "dropped_stale_rows": dropped_stale,
        "factor_freshness": factor_freshness,
        "eligible_factor_rows": len(eligible_usable_codes),
        "eligible_universe_rows": len(eligible_factor_universe),
        "eligible_factor_coverage_pct": round(eligible_factor_coverage * 100, 2),
        "full_market_scan": live_scan,
        "flow_coverage": {
            "eligible_rows": len(eligible_factor_universe),
            "super_net_rows": sum(1 for code in eligible_factor_universe if isinstance((live_map.get(str(code)) or {}).get("super_net"), (int, float))),
            "main_pct_rows": sum(1 for code in eligible_factor_universe if isinstance((live_map.get(str(code)) or {}).get("main_pct"), (int, float))),
            "supplemented_rows": len(_flow_supplemented_codes),
            "source": "eastmoney_snapshot+eastmoney_full_market_flow",
        },
        "max_factor_lag": spec["max_factor_lag"],
        "flow_source": raw.get("flow_source"), "style": style,
        "style_name": profile["name"],
        "model_family": selection_model,
        "sector_source": next(iter(sector_heat.values()), {}).get("source") if sector_heat else None,
        "hot_sectors": list(sector_heat.values())[:8],
        "sector_surge_lane": surge_meta,
        "concept_expansion_lane": concept_meta,
        "ths_hot_lane": ths_meta,
        "hot_leader_context": raw.get("hot_leader_context"),
        "bottom_reversal_context": raw.get("bottom_reversal_context"),
        "star_sector_impulse": list(star_sector_impulse.values())[:12],
        "security_scope": "仅沪深主板和创业板；科创板仅作同业映射信号",
        "selection_evolution": selection_overlay,
        "raw_candidate_count": raw_candidate_count,
        "watch_pool": watch_pool_meta,
        "ignition_shadow_recorded": sum(
            1 for row in shadow_rows if row.get("ignition_passed")
        ) if account_id == MAIN_FORCE_STRATEGY_ID else None,
        "entry_screened_count": len(entry_screened_reasons),
        "entry_screened_reasons": list(dict.fromkeys(entry_screened_reasons))[:6],
        "style_filtered_count": style_filtered_count,
        "trend_pool_eligible_count": len(picks) if style == "pullback" else None,
        "trend_pool_relaxation": ({
            "candidate_review_topn": candidate_topn,
            "ma20_ma60_floor": 0.93,
            "ma20_distance_range_pct": [-10.0, 14.0],
            "max_intraday_pct": 6.0,
            "hard_risk_gates_unchanged": True,
        } if style == "pullback" else None),
        "weights_used": raw.get("weights_used"),
        "financial_point_in_time": {
            **financial_evidence,
            "reason": "候选已逐条查询披露时间；未知披露只标记影子证据，不伪造发布时间",
            "profit_source_counts": {
                str(key): int(value)
                for key, value in (table.get("profit_source", pd.Series(dtype=str))
                                  .fillna("unknown").astype(str).value_counts().to_dict()).items()
            },
        },
    }


def _parse_quote_timestamp(value):
    """Parse public quote timestamps from ISO or compact YYYYMMDDHHMMSS forms.

    Tencent emits compact timestamps while Sina emits ISO timestamps.  Both are
    valid independent-source evidence; treating the Tencent form as invalid
    made a whole quote batch look stale even when it had just been refreshed.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        pass
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 14:
        try:
            return dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


def _quotes(codes, asof_date=None):
    """小批量实时查询失败时回退至本地快照，调用方仍会标记数据时效限制。"""
    codes = [str(code) for code in (codes or []) if str(code)]
    if not codes:
        return {}
    source_errors = {}
    try:
        live_rows = dfc.fetch_realtime_for_codes(list(codes)) or []
    except Exception as exc:
        # One provider must never abort a risk pass for every holding.  Keep
        # the exception in the quote envelope so entry remains fail-closed and
        # exits can surface a structured degraded/unavailable reason.
        live_rows = []
        source_errors["live"] = f"{type(exc).__name__}: {exc}"
    live = {str(r.get("code")): r for r in live_rows if r.get("code")}
    # 单票行情核验以价格为核心，但东财 ulist 偶尔返回价格而遗漏资金字段。
    # 补齐动作独立于双源价格校验：资金流失败不能把可执行的风险卖出误写成
    # “实时行情失败”，也不能令整批持仓都失去主力意图解释。
    flow_details = {}
    missing_flow_codes = {
        code for code, row in live.items()
        if not isinstance(row.get("super_net"), (int, float))
        or not isinstance(row.get("main_pct"), (int, float))
    }
    if missing_flow_codes:
        try:
            flow_details = dfc.enrich_live_flow_details(live_rows, missing_flow_codes) or {}
        except Exception as exc:
            source_errors["flow"] = f"{type(exc).__name__}: {exc}"
    # 独立核验源采用腾讯优先、缺失代码自动切换新浪；不能因为腾讯一次空响应
    # 把整轮策略都误判成“独立行情源未返回”。来源仍写入审计 payload，便于追溯。
    try:
        cross_rows = dfc.fetch_independent_realtime_for_codes(list(codes)) or []
    except Exception as exc:
        cross_rows = []
        source_errors["cross"] = f"{type(exc).__name__}: {exc}"
    cross = {str(r.get("code")): r for r in cross_rows if r.get("code")}
    local = _latest_price_map(codes)
    local_snapshot_at = _universe_snapshot_time()
    out = {}
    expected_day = _date(asof_date or dt.date.today())
    for code in codes:
        live_row = dict(live.get(code) or {})
        flow_row = dict(flow_details.get(code) or {})
        for field in ("super_net", "main_net", "big_net", "mid_net", "small_net", "main_pct"):
            if not isinstance(live_row.get(field), (int, float)) and isinstance(flow_row.get(field), (int, float)):
                live_row[field] = flow_row[field]
        is_live = bool(live_row.get("quote_at"))
        if is_live:
            # 保留本地快照中的名称、行业等静态元数据，但绝不把旧的价格/涨跌幅
            # 拼到“实时”时间戳上。主源缺字段时必须明确标记 incomplete，不能
            # 用昨天或上一轮缓存价格制造一笔看似通过的成交。
            row = {
                key: value for key, value in dict(local.get(code) or {}).items()
                if key not in {"price", "pct", "open", "high", "low", "pre_close", "quote_at"}
            }
            row.update({key: value for key, value in live_row.items() if value is not None})
            row["price"] = live_row.get("price")
            row["pct"] = live_row.get("pct")
        else:
            row = dict(local.get(code) or {})
        row["quote_at"] = live_row.get("quote_at") if is_live else local_snapshot_at
        row["quote_source"] = "live" if is_live else "local_cache"
        flow_at = flow_row.get("quote_at") or flow_row.get("flow_fetched_at") or live_row.get("quote_at")
        flow_time = _parse_quote_timestamp(flow_at)
        flow_day_ok = bool(flow_time and flow_time.date() == expected_day)
        flow_age_seconds = None
        if flow_time is not None and expected_day == dt.date.today():
            now_for_flow = dt.datetime.now(flow_time.tzinfo) if flow_time.tzinfo else dt.datetime.now()
            flow_age_seconds = round((now_for_flow - flow_time).total_seconds(), 1)
        has_flow = isinstance(live_row.get("super_net"), (int, float)) and isinstance(live_row.get("main_pct"), (int, float))
        row["flow_source"] = flow_row.get("flow_source") or ("eastmoney_ulist" if has_flow else None)
        row["flow_quote_at"] = flow_at
        row["flow_age_seconds"] = flow_age_seconds
        row["flow_validation"] = (
            "flow_fresh" if has_flow and flow_day_ok and (flow_age_seconds is None or -120 <= flow_age_seconds <= 120)
            else "flow_unavailable"
        )
        if source_errors.get("flow"):
            row["flow_source_error"] = source_errors["flow"]
        if source_errors:
            row["quote_source_errors"] = dict(source_errors)
        valid_price = isinstance(row.get("price"), (int, float)) and row.get("price") > 0
        valid_pct = isinstance(row.get("pct"), (int, float)) and abs(row.get("pct")) <= 30
        cross_row = cross.get(str(code)) or {}
        cross_price = _num(cross_row.get("price"), 0)
        cross_pct = _num(cross_row.get("pct"), 999)
        price_gap = abs(_num(row.get("price")) - cross_price) / max(cross_price, 0.01) if cross_price > 0 else None
        pct_gap = abs(_num(row.get("pct")) - cross_pct) if cross_price > 0 else None
        cross_at = str(cross_row.get("quote_at") or "")
        cross_day = cross_at[:10].replace("-", "") if "-" in cross_at[:10] else cross_at[:8]
        cross_day_ok = cross_day == expected_day.strftime("%Y%m%d")
        cross_time_ok = False
        if cross_day_ok:
            cross_time = _parse_quote_timestamp(cross_at)
            if cross_time is not None:
                if expected_day != dt.date.today():
                    # Historical replay validates source-date consistency, not
                    # the wall-clock freshness of an already archived quote.
                    cross_time_ok = True
                else:
                    now = dt.datetime.now(cross_time.tzinfo) if cross_time.tzinfo else dt.datetime.now()
                    cross_time_ok = -120 <= (now - cross_time).total_seconds() <= 20 * 60
        cross_available = cross_price > 0 and bool(cross_at)
        # 2026-08-28：急涨急跌时两个行情源的固有滞后差会被放大，|pct|>=3%
        # 时容差放宽一倍，避免 V 型反转时刻系统性拒绝回补/开仓
        # （601212 回补因 0.27pp 被拒）。
        gap_tolerance_scale = 2.0 if abs(_num(row.get("pct"), 0.0)) >= 3.0 else 1.0
        cross_ok = bool(is_live and valid_price and valid_pct and cross_available and cross_day_ok and cross_time_ok and price_gap <= 0.005 * gap_tolerance_scale and pct_gap <= 0.20 * gap_tolerance_scale)
        if source_errors.get("live") and not is_live:
            validation = "source_error"
            failure_reason = f"主行情源异常：{source_errors['live']}"
        elif not (is_live and valid_price and valid_pct):
            validation = "incomplete"
            failure_reason = "主行情价格或涨跌幅无效"
        elif source_errors.get("cross"):
            validation = "cross_source_unavailable"
            failure_reason = f"独立行情源异常：{source_errors['cross']}"
        elif not cross_available:
            validation = "cross_source_unavailable"
            failure_reason = "独立行情源未返回有效价格或时间"
        elif not cross_day_ok:
            validation = "cross_source_failed"
            failure_reason = f"独立行情源时间不是交易日（{cross_at or '未知'}）"
        elif not cross_time_ok:
            validation = "cross_source_unavailable"
            failure_reason = "独立行情源更新时间过期，已等待其重连/切换"
        elif price_gap > 0.005 * gap_tolerance_scale or pct_gap > 0.20 * gap_tolerance_scale:
            validation = "cross_source_failed"
            failure_reason = f"双源差异超阈值：价格差 {price_gap * 100:.3f}%、涨跌幅差 {pct_gap:.3f}%"
        else:
            validation = "cross_source_checked"
            failure_reason = None
        row["quote_validation"] = validation
        row["quote_cross_check"] = {
            "source": cross_row.get("source") or "tencent_public_quote+sina_public_quote",
            "quote_at": cross_row.get("quote_at"),
            "price_gap_pct": round(price_gap * 100, 3) if price_gap is not None else None,
            "pct_gap": round(pct_gap, 3) if pct_gap is not None else None,
            "available": cross_available, "date_ok": cross_day_ok, "time_ok": cross_time_ok, "passed": cross_ok,
            "failure_reason": failure_reason,
        }
        out[code] = row
    return out


def _news_for(names):
    global _NEWS_SCAN_META
    try:
        # 风控中心与自进化共用同一份事件/公告集合，避免一处看到公告、
        # 另一处仍把它当成普通快讯。公告必须保留 verified/source 字段。
        result = F.news_keyword_scan(names, include_announcements=True)
        result = result if isinstance(result, list) else []
        observed = []
        for row in result:
            value = str(row.get("time") or "").strip()
            try:
                if value.isdigit() and len(value) >= 14:
                    observed.append(dt.datetime.strptime(value[:14], "%Y%m%d%H%M%S"))
                elif value:
                    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    observed.append(parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed)
            except (TypeError, ValueError, OverflowError):
                continue
        latest = max(observed) if observed else dt.datetime.now()
        _NEWS_SCAN_META = {
            "observed_at": latest.strftime("%Y-%m-%d %H:%M:%S"),
            "stale": (dt.datetime.now() - latest).total_seconds() > 15 * 60,
            "error": None,
        }
        return result
    except Exception as exc:
        _NEWS_SCAN_META = {"observed_at": None, "stale": True, "error": str(exc)}
        return []


def _negative_hits(news, code):
    return [row for row in news if row.get("code") == code and row.get("tone", 0) < 0]


def _dynamic_news_risk(news, code, market=None):
    """统一新闻/公告动态门禁，供信号和执行两阶段重复核验。"""
    rows = [row for row in (news or []) if str(row.get("code") or "") == str(code)]
    now = dt.datetime.now()
    active_rows = []
    for row in rows:
        if _num(row.get("tone"), 0) >= 0:
            active_rows.append(row)
            continue
        observed = None
        try:
            value = str(row.get("time") or "").strip()
            if value.isdigit() and len(value) >= 14:
                observed = dt.datetime.strptime(value[:14], "%Y%m%d%H%M%S")
            elif value:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                observed = parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
        except (TypeError, ValueError, OverflowError):
            observed = None
        is_intraday = dt.time(9, 30) <= now.time() <= dt.time(15, 0)
        if bool(row.get("verified")):
            ttl = NEWS_VERIFIED_TTL_SECONDS
        else:
            ttl = (2 * 60 * 60) if is_intraday else NEWS_UNVERIFIED_TTL_SECONDS
        if observed is None or (now - observed).total_seconds() <= ttl:
            active_rows.append(row)
    rows = active_rows
    negative = [row for row in rows if _num(row.get("tone"), 0) < 0]
    verified_negative = [row for row in negative if bool(row.get("verified"))]
    market_light = str((market or {}).get("light") or "unknown")
    if verified_negative:
        mode, scale, allowed = "公告否决", 0.0, False
        reason = f"已核验负面公告/事件 {len(verified_negative)} 条，禁止该股新增仓位"
    elif negative:
        mode, scale, allowed = "舆情收紧", 0.50, True
        reason = f"负面快讯 {len(negative)} 条尚未完成公告核验，新增仓位缩减50%"
    else:
        mode, scale, allowed = "正常", 1.0, True
        reason = "未命中负面公告或快讯"
    if _NEWS_SCAN_META.get("stale") and not verified_negative:
        scale *= 0.75
        mode = "舆情源降级"
        reason = "新闻/公告扫描已过期，新增仓位按75%额度执行"
    if market_light in {"red", "unknown"}:
        scale = 0.0
        allowed = False
        mode = "市场暂停"
        reason = "市场红灯或数据未知，暂停该股新增仓位"
    return {
        "mode": mode,
        "risk_scale": scale,
        "new_entry_allowed": allowed,
        "negative_count": len(negative),
        "verified_negative_count": len(verified_negative),
        "events": rows[:8],
        "reason": reason,
    }


def _chase_entry_gate(account, pick, quote, market, entry_model, q, execution_quote):
    """Controlled chase gate for TQ and the sector-hot acceleration lane."""
    account_id = account["id"]
    pct = _num(quote.get("pct"), -999)
    limit_pct = _limit_pct(pick.get("code"))
    result = {"allowed": False, "risk_scale": 1.0, "reason": None, "mode": "normal"}
    if account_id == "trend_pullback":
        result["reason"] = "趋势回踩策略不追高"
        return result
    if account_id == NEW_STRATEGY_ID:
        result["reason"] = "三日策略不使用追高通道"
        return result
    sector_hot = (
        account_id == "sector_rotation"
        and str(pick.get("candidate_status") or "") in {
            "ths_hot_lane", "concept_expansion_lane", "sector_surge_lane", "hot_leader_watch",
        }
        and pct >= 7.0
    )
    if account_id != "tq_breakout" and not sector_hot:
        result["reason"] = "板块轮动策略不追高"
        return result
    result["required"] = bool(sector_hot)
    if sector_hot:
        heat = pick.get("sector_heat") if isinstance(pick.get("sector_heat"), dict) else {}
        rank = int(_num(heat.get("rank"), 999))
        heat_pct = _num(heat.get("pct"), -999.0)
        overheat = (entry_model.get("overheat_guard") or {}) if isinstance(entry_model, dict) else {}
        if market.get("light") != "green":
            result["reason"] = "热点加速试仓仅在市场绿灯开放"
            return result
        if execution_quote.get("status") != "cross_source_checked":
            result["reason"] = "热点加速试仓必须通过双源实时行情核验"
            return result
        if pct > limit_pct - 0.15:
            result["reason"] = "热点加速已接近涨停，不模拟封板排队成交"
            return result
        if q.get("tier") != "Q1":
            result["reason"] = f"热点加速后市质量 {q.get('tier')}，未达到 Q1"
            return result
        score = _num(entry_model.get("score"))
        if not entry_model.get("passed") or score < 0.80:
            result["reason"] = f"热点加速确认分 {score:.2f} 不足 0.80"
            return result
        if rank > 5 or heat_pct < 2.5:
            result["reason"] = f"热点加速要求板块前5且涨幅≥2.5%，当前第 {rank}、{heat_pct:+.2f}%"
            return result
        main_pct = _num(quote.get("main_pct"), -999)
        vol_ratio = _num(quote.get("vol_ratio"))
        if main_pct < 3.0 or vol_ratio < 2.0:
            result["reason"] = "热点加速所需主力净流入≥3%且量比≥2.0未同时满足"
            return result
        if str(overheat.get("level") or "unknown") not in {"normal", "caution"}:
            result["reason"] = "个股已处于连续加速末端，热点策略不追末板"
            return result
        result.update({
            # This is deliberately smaller than a normal sector position, but
            # not a dust order.  The former 25% scale was multiplied again by
            # the entry-model scale (and, before the sizing fix below, by the
            # market-yellow scale), which regularly rounded a valid hot name
            # down to a single lot.  Half of the independently available
            # sector budget keeps the first hot-leader expression meaningful
            # while preserving headroom for confirmation or rotation.
            "allowed": True, "risk_scale": 0.50, "mode": "板块热点加速试仓",
            "reason": "绿灯、前五热点、双源、Q1、主力与量能同步，允许未触板的50%试仓",
        })
        return result
    if market.get("light") not in ("green", "yellow"):
        result["reason"] = "大盘非绿/黄灯，禁止追买"
        return result
    if execution_quote.get("status") != "cross_source_checked":
        result["reason"] = "追买必须通过双源实时行情核验"
        return result
    # limit_up_threshold 是交易所涨停前的保守触发线（9.5/19.5/29.5），
    # 实时行情通常会显示到 10/20/30%，允许小幅上浮用于行情取整。
    # P3：动量确认模式从 +3.5% 起（与普通入场带上沿一致），消除
    # 3.5%~7% 之间"既不是低位启动、又不按追高管理"的规则模糊区。
    if pct < 3.5 or pct > limit_pct + 0.60:
        result["reason"] = f"涨幅 {pct:+.2f}% 未处于短线动量确认区间（+3.5% 至涨停附近）"
        return result
    # 已触及涨停价位的标的无法区分“封板”与“未封板”：本系统不模拟涨停板
    # 排队成交（ACCOUNT_SPECS 明确声明），因此贴近涨停幅度的报价一律不放行。
    if pct >= limit_pct - 0.15:
        result["reason"] = f"涨幅 {pct:+.2f}% 已触及涨停价位，不模拟封板排队成交"
        return result
    if q.get("tier") != "Q1":
        result["reason"] = f"后市质量 {q.get('tier')}，未达到 Q1"
        return result
    score = _num(entry_model.get("score"))
    if not entry_model.get("passed") or score < 0.78:
        result["reason"] = f"策略确认分 {score:.2f} 不足"
        return result
    main_pct = _num(quote.get("main_pct"), -999)
    vol_ratio = _num(quote.get("vol_ratio"))
    # 强势接力只在强资金、强量能和高质量信号同时成立时追买。
    if main_pct < 2.0 or vol_ratio < 1.5 or _num(pick.get("score")) < 0.20:
        result["reason"] = "接力追买所需资金、量能或候选质量不足"
        return result
    result.update({"allowed": True, "risk_scale": 0.35, "mode": "短线确认追高",
                   "reason": "短线日内做T：主力、量能、Q1 后市和候选质量确认通过"})
    return result


def _strategy_market_policy(account, pick, quote, market):
    """不同策略使用不同的黄灯缩放；红灯统一停止新开仓。"""
    light = market.get("light") or "unknown"
    account_id = account["id"]
    if light == "green":
        return {"allowed": True, "risk_scale": 1.0, "state": "正常", "reason": "市场绿灯"}
    if light == "yellow":
        scale = market_light_scale(light, account["id"])
        return {
            "allowed": True, "risk_scale": scale, "state": "谨慎",
            "reason": f"市场黄灯，{ACCOUNT_SPECS[account['id']]['entry_model_name']}按{scale*100:.0f}%仓位执行",
        }
    if light == "unknown":
        return {"allowed": False, "risk_scale": 0.0, "state": "禁止", "reason": "市场数据未知"}

    if account_id == "sector_rotation":
        heat = pick.get("sector_heat") or {}
        hot_enough = _num(heat.get("rank"), 999) <= 8 and _num(heat.get("pct")) >= 1.0
        quote_pct = _num(quote.get("pct"), -999)
        return {
            "allowed": False, "risk_scale": 0.0, "state": "观察",
            "reason": "市场红灯，板块轮动策略暂停新开仓",
            "shadow_exception": bool(hot_enough and quote_pct > 0),
            "shadow_reason": (
                f"热点板块排名第{heat.get('rank')}、涨幅{_num(heat.get('pct')):.2f}%且个股上涨，"
                "仅记录为影子例外，不产生模拟成交"
            ) if hot_enough and quote_pct > 0 else None,
        }
    if account_id == NEW_STRATEGY_ID:
        return {
            "allowed": False, "risk_scale": 0.0, "state": "观察",
            "reason": "市场红灯，三日策略暂停新开仓",
        }
    if account_id == "tq_breakout":
        return {
            "allowed": False, "risk_scale": 0.0, "state": "观察",
            "reason": "市场红灯，首板接力暂停新开仓",
        }
    return {
        "allowed": False, "risk_scale": 0.0, "state": "观察",
        "reason": "市场红灯，趋势回踩策略暂停新开仓",
    }


def _clip01(value):
    return max(0.0, min(1.0, _num(value)))


def _microstructure_soft_factor(account_id, evidence):
    """Build a small, fail-open score from public microstructure evidence."""
    evidence = dict(evidence or {})
    values = {
        "五档失衡": evidence.get("depth_imbalance"),
        "逐笔主动买卖": evidence.get("active_buy_sell_imbalance"),
        "分钟VWAP": evidence.get("vwap_deviation_pct"),
    }
    available = [name for name, value in values.items() if value is not None]
    weights = {
        "tq_breakout": 0.08,
        "sector_rotation": 0.07,
        "trend_pullback": 0.04,
        NEW_STRATEGY_ID: 0.05,
        MAIN_FORCE_STRATEGY_ID: 0.15,
    }
    weight = weights.get(account_id, 0.0)
    # One isolated public-quote field is too brittle to affect ranking. Keep it
    # as shadow evidence until at least two independent micro views agree.
    if evidence.get("status") not in {"ok", "partial"} or len(available) < 2 or weight <= 0:
        evidence.update({"score_applied": False, "available_components": available})
        return evidence, None, 0.0, "微观结构证据不足，仅影子记录"
    components = []
    if values["五档失衡"] is not None:
        components.append(_clip01((float(values["五档失衡"]) + 1.0) / 2.0))
    if values["逐笔主动买卖"] is not None:
        components.append(_clip01((float(values["逐笔主动买卖"]) + 1.0) / 2.0))
    if values["分钟VWAP"] is not None:
        # +/-1.5% around session VWAP spans the useful soft-score range.
        components.append(_clip01(0.5 + float(values["分钟VWAP"]) / 3.0))
    score = sum(components) / len(components)
    evidence.update({
        "score_applied": True, "soft_score": round(score, 4),
        "soft_weight": weight, "available_components": available,
        "policy": "soft_score_only_no_gate_override",
    })
    detail = (
        f"五档 {float(values['五档失衡'] or 0):+.2f}；"
        f"主动买卖 {float(values['逐笔主动买卖'] or 0):+.2f}；"
        f"现价/VWAP {float(values['分钟VWAP'] or 0):+.2f}%"
    )
    return evidence, score, weight, detail


def _strategy_entry_assessment(account, pick, quote, kline, decision, threshold_delta=0.0, market=None):
    """按各模拟盘账户各自的独立模型复核入场。"""
    account_id = account["id"]
    spec = ACCOUNT_SPECS[account_id]
    pct = _num(quote.get("pct"), -999)
    vol_ratio = _num(quote.get("vol_ratio"))
    main_pct = _num(quote.get("main_pct"))
    rank_score = _num(pick.get("score"))
    checks = []
    overheat = {"level": "normal", "score": 0.0, "pullback_confirmed": False}
    threshold_context = {}
    breakout_probe = False
    timing_mode = "常规确认"
    blockers = [
        reason for reason in (decision.get("hard_vetoes") or [])
        if reason != "海外风险红灯"
    ]

    def add(name, value, weight, detail):
        checks.append({
            "name": name,
            "score": round(_clip01(value), 3),
            "weight": weight,
            "detail": detail,
        })

    microstructure, micro_score, micro_weight, micro_detail = _microstructure_soft_factor(
        account_id, pick.get("microstructure")
    )

    if account_id == "tq_breakout":
        limit_pct = _limit_pct(pick.get("code"))
        # Leaving the preferred band is already a momentum entry.  Treat the
        # entire +3.5%~limit interval consistently instead of leaving 5%~7%
        # in a weaker Q2-capable gap between ordinary and chase logic.
        breakout_probe = 3.5 < pct <= 7.0
        # 追求可成交的盘中强度，而非把高开/加速本身当作买点。
        entry_zone = (
            1.0 if 0.2 <= pct <= 3.5 else
            0.55 if -1.0 <= pct <= 5.0 else 0.0
        )
        add("可成交强度", entry_zone, 0.28, f"涨跌幅 {pct:+.2f}%（优选 +0.2% 至 +3.5%）")
        add("量能确认", vol_ratio / 1.5, 0.22, f"量比 {vol_ratio:.2f}")
        add("主力方向", (main_pct + 5) / 10, 0.20, f"主力净流入占比 {main_pct:+.2f}%")
        add("强势候选质量", (rank_score + 0.8) / 1.6, 0.18, f"候选分 {rank_score:.3f}；昨日首板仅作加分")
        add("六维护栏", _num(decision.get("avg_score")) / 0.75, 0.12,
            f"通用六维 {_num(decision.get('avg_score')):.2f}")
        if pct <= -2:
            blockers.append(f"短线候选实时涨幅 {pct:+.2f}% 低于 -2%")
        # 近期短线策略的亏损尾部主要来自量价动量尚在、但实时资金已经
        # 转弱的标的。做T可以追随强度，不能在主力已转为净流出时逆势开仓。
        if main_pct < 0:
            blockers.append(f"短线候选主力净流入 {main_pct:+.2f}% 未转正")
        chase_probe = (
            pct >= limit_pct - 0.30
            and pct <= limit_pct + 0.60
            and main_pct >= 2.0
            and vol_ratio >= 1.5
            and rank_score >= 0.20
            and _num(decision.get("avg_score")) >= 0.65
        )
        if breakout_probe:
            add("加速突破确认", min(vol_ratio / 2.0, (main_pct + 2) / 6.0), 0.10,
                f"涨幅 {pct:+.2f}%：只允许热点加速的小仓试错")
            if vol_ratio < 1.5:
                blockers.append(f"加速突破量比 {vol_ratio:.2f} 未达 1.50")
            if main_pct < 2.0:
                blockers.append(f"加速突破主力净流入 {main_pct:+.2f}% 未达 +2.00%")
            if rank_score < 0.20 or _num(decision.get("avg_score")) < 0.65:
                blockers.append("加速突破缺少候选质量或六维确认")
        else:
            if pct > 7.0 and not chase_probe:
                blockers.append(f"盘中已上涨 {pct:+.2f}%：只记录观察，不模拟追入")
        if pct >= limit_pct - 0.3 and not chase_probe:
            blockers.append("接近涨停，无法验证真实排队成交")
        if vol_ratio < 0.9:
            blockers.append(f"量比 {vol_ratio:.2f} 低于接力最低要求")
        threshold = 0.64
    elif account_id == MAIN_FORCE_STRATEGY_ID:
        amount = _num(quote.get("amount"), _num(pick.get("amount"), 0.0))
        super_net = _num(quote.get("super_net"), _num(pick.get("super_net"), 0.0))
        flow_intensity = _num(quote.get("main_net"), _num(pick.get("main_net"), 0.0)) / max(amount, 1.0)
        depth = _num((microstructure or {}).get("depth_imbalance"), None)
        active = _num((microstructure or {}).get("active_buy_sell_imbalance"), None)
        vwap = _num((microstructure or {}).get("vwap_deviation_pct"), None)
        add("每日主力观察排名", rank_score, 0.22, f"每日10只复合排名分 {rank_score:.3f}")
        add("主力净流入占比", main_pct / 6.0, 0.23, f"主力净流入 {main_pct:+.2f}%")
        add("资金成交强度", flow_intensity / 0.04, 0.18, f"主力净额/成交额 {flow_intensity*100:+.2f}%")
        add("实时量能", vol_ratio / 1.8, 0.12, f"量比 {vol_ratio:.2f}")
        add("安全涨幅区间", 1.0 if 0.3 <= pct <= 5.5 else 0.55 if -0.5 <= pct <= 7.5 else 0.0,
            0.10, f"实时涨幅 {pct:+.2f}%")
        if main_pct < 3.0:
            blockers.append(f"主力净流入 {main_pct:+.2f}% 未达 +3.00%")
        if super_net <= 0:
            blockers.append("超大单未保持净流入")
        if flow_intensity < 0.02:
            blockers.append(f"主力净额/成交额 {flow_intensity*100:+.2f}% 未达 +2.00%")
        if vol_ratio < 1.0:
            blockers.append(f"量比 {vol_ratio:.2f} 未达 1.00")
        if pct < -1.0 or pct > 7.5:
            blockers.append(f"涨幅 {pct:+.2f}% 不在主力确认执行区间 -1.00% 至 +7.50%")
        ignition_result = None
        if -1.0 < pct <= 3.5:
            ignition_result = {"passed": None, "lane": "常规承接（≤+3.5%）"}
        elif pct <= 7.5:
            # 点火分支（2026-08-31 P1）：+3.5%~+7.5% 不再被通用追高帽直接
            # 拒绝，改为强制八项点火条件（分钟量能结构、VWAP 承接、低点
            # 抬高、5分钟主力增量、10分钟持续率、逐笔/五档）。任何一项
            # 数据缺失或不满足都记为 blocker——点火是放宽追高的交换条件。
            if IGN is not None:
                try:
                    ignition_code = str(pick.get("code") or "")
                    ignition_result = IGN.evaluate_ignition(
                        ignition_code, pct=pct, limit_pct=_limit_pct(ignition_code),
                    )
                except Exception as exc:
                    ignition_result = {
                        "passed": False,
                        "reasons": [f"点火判定异常: {type(exc).__name__}"],
                        "evidence": {},
                    }
            else:
                ignition_result = {"passed": False, "reasons": ["ignition_entry 模块不可用"], "evidence": {}}
            ignition_ok = bool(ignition_result.get("passed"))
            if not IGNITION_BUY_ENABLED:
                # 影子对比期：点火判定照常运行并记录，但买入不放开。
                blockers.append(
                    f"点火通道影子运行中，买入未放开（点火判定："
                    f"{'成立' if ignition_ok else '未成立'}）"
                )
            elif not ignition_ok:
                for ignition_reason in (ignition_result.get("reasons") or [])[:4]:
                    blockers.append(f"点火确认未通过：{ignition_reason}")
            else:
                add("点火八项确认", 1.0, 0.0, "分钟量能/VWAP/低点抬高/资金增量与持续率/微观盘口全部成立")
        else:
            ignition_result = {"passed": False, "lane": "超出点火区间上限 +7.5%"}
        if active is not None and active < -0.15:
            blockers.append(f"逐笔主动成交偏卖 {active:+.2f}，疑似诱多/派发")
        if depth is not None and depth < -0.35:
            blockers.append(f"五档盘口卖压 {depth:+.2f} 过强")
        if vwap is not None and not (-0.5 <= vwap <= 2.0):
            blockers.append(f"现价偏离分钟VWAP {vwap:+.2f}% 超出承接区间")
        threshold_context = {
            "version": MAIN_FORCE_STRATEGY_VERSION, "daily_candidate_limit": 10,
            "position_limit": 3, "main_pct": main_pct,
            "flow_intensity": round(flow_intensity, 6),
            "ignition": (
                {
                    "passed": ignition_result.get("passed"),
                    "reasons": list(ignition_result.get("reasons") or [])[:6],
                    "version": ignition_result.get("version"),
                }
                if ignition_result is not None else None
            ),
            "reason": "主力强度、成交占比、量能与微观结构确认；>3.5% 走点火八项确认",
        }
        threshold = 0.72
    elif account_id == NEW_STRATEGY_ID:
        # The reported-profit model supplies point-in-time financial and
        # technical path metadata.  Missing metadata is a hard blocker: a
        # paper fill must never be fabricated from a partial candidate row.
        metadata = {}
        for key in ("metadata", "reported_profit_breakout", "strategy_context", "entry_context"):
            if isinstance(pick.get(key), dict):
                metadata.update(pick.get(key) or {})
        values = {**metadata, **pick}

        def flag(value):
            return value is True or str(value).strip().lower() in {"1", "true", "yes", "y", "通过", "reported"}

        profit_source = str(
            values.get("profit_source") or values.get("financial_source")
            or values.get("fundamental_source") or ""
        ).strip().lower()
        reported = bool(
            flag(values.get("reported_profit"))
            or flag(values.get("profit_reported"))
            or "reported" in profit_source
            or "披露" in profit_source
            or "point_in_time" in profit_source
        )
        entry_path = str(values.get("entry_path") or "").strip().lower()
        three_up = flag(values.get("three_up")) or entry_path == "three_day_profit"
        ma5_path = flag(values.get("above_ma5_5d")) or flag(values.get("ma5_path")) or entry_path == "five_day_profit"
        above_all_ma = flag(values.get("above_all_ma"))
        above_boll = (
            flag(values.get("above_boll_mid")) or flag(values.get("above_boll"))
            or flag(values.get("boll_mid_breakout"))
            or (entry_path == "five_day_profit" and above_all_ma)
        )
        above_ma60 = flag(values.get("above_ma60")) or flag(values.get("above_main_ma")) or above_all_ma
        breakout_path = bool((three_up or ma5_path) and reported)
        breakout_probe = bool(breakout_path and pct >= 3.5)
        flow_raw = _num(values.get("super_net_raw"), _num(values.get("super_net"), None))
        quality_score = _num(values.get("quality"), _num(values.get("roe"), 0.0))
        add("财报披露证据", 1.0 if reported else 0.0, 0.25,
            f"利润来源 {profit_source or '未知'}；点时披露 {'通过' if reported else '缺失'}")
        add("突破路径", 1.0 if breakout_path else 0.0, 0.28,
            f"三连阳={'是' if three_up else '否'}，MA5路径={'是' if ma5_path else '否'}")
        add("主要均线位置", (int(above_boll) + int(above_ma60)) / 2.0, 0.20,
            f"BOLL中轨={'上方' if above_boll else '未知/下方'}；MA60={'上方' if above_ma60 else '未知/下方'}")
        add("超大单资金", (flow_raw + 1.0) / 3.0 if flow_raw is not None else 0.0, 0.17,
            f"超大单净流入 {flow_raw:+.2f}" if flow_raw is not None else "超大单资金缺失")
        add("实时量价", min((vol_ratio or 0.0) / 1.2, 1.0), 0.10,
            f"涨跌幅 {pct:+.2f}%；量比 {vol_ratio:.2f}")
        if not reported:
            blockers.append("缺少已披露财报利润证据，禁止质量突破建仓")
        if not breakout_path:
            blockers.append("未满足三连阳或 MA5+财报突破路径")
        if not above_boll or not above_ma60:
            blockers.append("现价未同时站上主要均线/BOLL中轨")
        if flow_raw is None or flow_raw <= 0:
            blockers.append("超大单资金缺失或未净流入")
        if main_pct < 0:
            blockers.append(f"实时主力净流入 {main_pct:+.2f}% 不足")
        if vol_ratio < 0.9:
            blockers.append(f"实时量比 {vol_ratio:.2f} 低于质量突破最低要求")
        if pct <= -4.0:
            blockers.append(f"实时跌幅 {pct:+.2f}% 触发质量突破入场否决")
        threshold_context = {
            "version": "reported-profit-breakout-entry-v1",
            "profit_source": profit_source or None,
            "reported_profit": reported,
            "breakout_path": breakout_path,
            "three_up": three_up,
            "ma5_path": ma5_path,
            "above_boll_mid": above_boll,
            "above_ma60": above_ma60,
            "super_net_raw": flow_raw,
            "quality_score": quality_score,
            "reason": "已披露利润与突破结构双确认",
        }
        threshold = 0.70
    elif account_id == "trend_pullback":
        close = ma20 = ma60 = None
        if kline is not None and len(kline) >= 60:
            closes = pd.to_numeric(kline["close"], errors="coerce").dropna()
            if len(closes) >= 60:
                close = _num(closes.iloc[-1], None)
                ma20 = _num(closes.tail(20).mean(), None)
                ma60 = _num(closes.tail(60).mean(), None)
        price = _num(quote.get("price"), 0)
        distance_ma20 = price / ma20 - 1 if price > 0 and ma20 else None
        open_price = _num(quote.get("open_price"), 0)
        bottom_context = pick.get("bottom_reversal_context") if isinstance(pick.get("bottom_reversal_context"), dict) else {}
        bottom_structure = str(bottom_context.get("structure") or "")
        bottom_start = bottom_structure in {"base_rebound", "reversal_watch"}
        trend_structure = (
            1.0 if ma20 and ma60 and ma20 >= ma60
            else 0.72 if ma20 and ma60 and ma20 >= ma60 * 0.95 and close and close >= ma20 * 0.98
            else 0.50 if close and ma20 and close >= ma20
            else 0.0
        )
        pullback_fit = (
            1.0 if distance_ma20 is not None and -0.03 <= distance_ma20 <= 0.05
            else 0.55 if distance_ma20 is not None and -0.06 <= distance_ma20 <= 0.09
            else 0.0
        )
        mom20 = _num(pick.get("mom20"))
        add("趋势结构", trend_structure, 0.30,
            f"MA20 {ma20:.2f} / MA60 {ma60:.2f}" if ma20 and ma60 else "均线不足")
        add("回踩位置", pullback_fit, 0.28,
            f"现价偏离 MA20 {(distance_ma20 or 0)*100:+.2f}%")
        add("中期动量", 1 - abs(mom20 - 12) / 18, 0.18, f"20日动量 {mom20:+.2f}%")
        add("资金稳定", (main_pct + 8) / 12, 0.14, f"主力净流入占比 {main_pct:+.2f}%")
        add("实时波动", (pct + 4) / 8, 0.10, f"涨跌幅 {pct:+.2f}%")
        if close is None or ma20 is None or ma60 is None:
            blockers.append("趋势均线数据不足")
        elif ma20 < ma60 * 0.98:
            blockers.append("MA20 未接近 MA60，趋势结构仍未修复")
        if distance_ma20 is None or distance_ma20 < -0.10 or distance_ma20 > 0.14:
            blockers.append("现价偏离 MA20 超出趋势回踩执行区间")
        if pct <= -4:
            blockers.append(f"实时跌幅 {pct:+.2f}% 触发趋势入场否决")
        if main_pct < -2.0:
            blockers.append(f"实时主力净流入 {main_pct:+.2f}% 偏弱，不做趋势回踩")
        if bottom_start:
            # 底部启动不能只因日线形态入场；要看到当天承接，避免把仍在
            # 下跌的反转候选当成趋势回踩。真正的多头回踩路径不受此限制。
            timing_mode = "底部启动承接确认"
            reclaim_ok = bool(
                pct >= -0.5
                and (open_price <= 0 or price >= open_price * 0.995)
                and main_pct >= 0
                and vol_ratio >= 0.9
            )
            if not reclaim_ok:
                blockers.append("底部启动尚未出现日内承接：需守住开盘附近、主力不净流出且量比≥0.9")
        else:
            timing_mode = "多头趋势回踩"
        threshold_context = _trend_entry_threshold_context(
            market or {}, trend_structure, distance_ma20, main_pct, pct
        )
        threshold = _num(threshold_context.get("threshold"), 0.60)
    else:
        heat = pick.get("sector_heat") or {}
        overheat = _sector_overheat_guard(kline, quote, heat)
        threshold_context = dict(
            pick.get("sector_threshold_context")
            or _sector_entry_threshold_context({}, market or {}, heat)
        )
        heat_rank = int(_num(heat.get("rank"), 999))
        heat_pct = _num(heat.get("pct"), -99)
        member_count = int(_num(heat.get("member_count"), 0))
        positive_ratio = _num(heat.get("positive_ratio"), None)
        sector_main_pct = _num(heat.get("median_main_pct"), None)
        early_rotation = bool(heat.get("early_rotation"))
        individual_path = pick.get("entry_path") == "individual_strong"
        hot_lane = pick.get("entry_path") in {"sector_surge", "concept_expansion", "ths_hot"}
        add("板块排名", (16 - heat_rank) / 15, 0.30, f"热点板块第 {heat_rank} 名")
        add("板块强度", heat_pct / 3.0, 0.22, f"板块涨幅 {heat_pct:+.2f}%")
        add("个股相对强度", (pct + 1) / 5, 0.20, f"个股涨幅 {pct:+.2f}%")
        add("资金共振", (main_pct + 6) / 12, 0.18, f"主力净流入占比 {main_pct:+.2f}%")
        add("成交活跃", vol_ratio / 1.5, 0.10, f"量比 {vol_ratio:.2f}")
        add("个股位置安全边际", 1.0 - _num(overheat.get("score"), 0.5), 0.10,
            overheat.get("reason") or "位置数据不足")
        if overheat.get("level") == "extreme":
            blockers.append(overheat.get("reason") or "个股位置过热")
        elif overheat.get("level") == "hot" and not overheat.get("pullback_confirmed"):
            blockers.append(overheat.get("reason") or "个股过热，等待回踩确认")
        elif overheat.get("level") == "hot":
            threshold_context.setdefault("reasons", []).append("过热后出现回踩，仅允许提高门槛后的确认入场")
        elif overheat.get("level") == "caution":
            threshold_context.setdefault("reasons", []).append("个股短线过热，动态提高入场门槛")
        if individual_path:
            # 个股强势例外：板块热度降为排序项，不作为硬否决；
            # 但个股门槛更高，避免把“孤立拉升”误当成机会。
            add("个股妖股路径", min((pct - 3.5) / 5.0, (main_pct - 2.5) / 4.0, (vol_ratio - 1.5) / 2.0), 0.20,
                f"板块第 {heat_rank} 名、个股 {pct:+.2f}%、主力 {main_pct:+.2f}%、量比 {vol_ratio:.2f}")
            if pct < 3.5 or pct > 8.5:
                blockers.append(f"个股强势路径涨幅 {pct:+.2f}% 不在 +3.5% 至 +8.5% 区间")
            if main_pct < 2.5:
                blockers.append(f"个股强势路径主力净流入 {main_pct:+.2f}% 未达 +2.50%")
            if vol_ratio < 1.5:
                blockers.append(f"个股强势路径量比 {vol_ratio:.2f} 未达 1.50")
            if heat_rank > 15 or heat_pct <= 0:
                checks[-5]["detail"] += "；板块热度不足，仅降权不否决"
        else:
            # "热点"不能只由一只涨停股定义。板块轮动历史亏损集中在
            # 广度、板块资金或个股资金未共振时仍被放行的候选；因此普通
            # 路径要求至少有同快照的成员广度和资金确认。个股强势路径
            # 保留其更高的独立门槛，避免错过真正的龙头。
            if hot_lane:
                # Live hot-lane discovery is intentionally earlier than the
                # lagging breadth model.  Require current board strength and
                # member-level flow, but do not wait for a full 60% breadth
                # confirmation that arrives after the first impulse.
                if heat_rank > 10 or heat_pct < 1.2:
                    blockers.append("实时热点车道板块未达到前10且涨幅至少 +1.2%")
                if main_pct < 0.3:
                    blockers.append(f"热点车道个股主力净流入 {main_pct:+.2f}% 未达 +0.30%")
                if vol_ratio < 0.9:
                    blockers.append(f"热点车道个股量比 {vol_ratio:.2f} 未达 0.90")
            elif heat_rank > 8 or heat_pct < 0.8:
                blockers.append("所属板块未进入前8强且涨幅至少 +0.8% 的热点区")
            if not hot_lane and (member_count < 5 or positive_ratio is None or positive_ratio < 0.60):
                blockers.append("热点板块广度不足：至少5只成分、60%上涨才可轮动")
            if not hot_lane and (sector_main_pct is None or sector_main_pct < 0):
                blockers.append("热点板块资金未确认净流入")
            if not hot_lane and not early_rotation and not (heat_rank <= 3 and heat_pct >= 1.5):
                blockers.append("热点尚未形成早期扩散或前三强加速共振")
            if not hot_lane and main_pct < 0.5:
                blockers.append(f"个股主力净流入 {main_pct:+.2f}% 未达 +0.50%")
            if not hot_lane and vol_ratio < 1.1:
                blockers.append(f"个股量比 {vol_ratio:.2f} 未达 1.10")
        if pct <= -1.0:
            blockers.append(f"个股实时涨幅 {pct:+.2f}% 与板块热点背离")
        # 当日门槛由市场灯、全市场宽度、热点扩散和所属板块强度共同决定，
        # 不再锁死 0.68。个股强势例外仍额外提高要求，不能借弱板块放松。
        dynamic_base = _num(threshold_context.get("threshold"), 0.61)
        threshold = max(0.60 if hot_lane else 0.65, dynamic_base + (0.04 if individual_path else 0.0))
        if overheat.get("level") == "caution":
            threshold += 0.04
        if overheat.get("level") == "hot" and overheat.get("pullback_confirmed"):
            threshold += 0.06
        threshold_context.update({
            "base_threshold": round(dynamic_base, 3),
            "individual_path_extra": 0.04 if individual_path else 0.0,
            "hot_lane": hot_lane,
            "overheat_threshold_extra": 0.04 if overheat.get("level") == "caution" else 0.0,
            "overheat_pullback_extra": 0.06 if overheat.get("level") == "hot" and overheat.get("pullback_confirmed") else 0.0,
            "selection_news_delta": round(_num(threshold_delta), 3),
            "overheat_guard": overheat,
        })

    if micro_score is not None:
        add("微观结构影子确认", micro_score, micro_weight, micro_detail)

    weight_total = sum(item["weight"] for item in checks) or 1.0
    score = sum(item["score"] * item["weight"] for item in checks) / weight_total
    threshold = max(0.45, min(0.85, threshold + threshold_delta))
    if threshold_context:
        threshold_context["base_threshold"] = round(
            _num(threshold_context.get("threshold"), threshold), 3
        )
        threshold_context["threshold_delta"] = round(_num(threshold_delta), 3)
        threshold_context["effective_threshold"] = round(threshold, 3)
    passed = not blockers and score >= threshold
    reasons = list(blockers)
    if score < threshold:
        if account_id in {"sector_rotation", "trend_pullback", MAIN_FORCE_STRATEGY_ID}:
            reasons.append(
                f"{spec['entry_model_name']}评分 {score:.2f} 未达当日动态门槛 {threshold:.2f}"
                f"（{threshold_context.get('reason') or '市场条件'}）"
            )
        else:
            reasons.append(f"{spec['entry_model_name']}评分 {score:.2f} 未达 {threshold:.2f}")
    return {
        "name": spec["entry_model_name"],
        "risk_profile": spec["risk_profile"],
        "risk_profile_name": RISK_PROFILES[spec["risk_profile"]]["name"],
        "score": round(score, 3),
        "threshold": round(threshold, 3),
        "threshold_context": threshold_context if account_id in {"sector_rotation", "trend_pullback", NEW_STRATEGY_ID, MAIN_FORCE_STRATEGY_ID} else None,
        "passed": passed,
        "checks": checks,
        "blockers": blockers,
        "reasons": reasons,
        "overheat_guard": overheat,
        "microstructure": microstructure,
        "timing_mode": timing_mode,
        # 热点加速段的赔率尚可但回撤显著增加；通过也只使用常规单笔规模的三分之一。
        "position_scale": (
            0.20 if account_id == "sector_rotation" and overheat.get("level") == "hot" and overheat.get("pullback_confirmed")
            else 0.25 if account_id == "sector_rotation" and overheat.get("level") == "caution"
            else 0.35 if account_id == "tq_breakout" and breakout_probe
            else 0.50 if account_id == "sector_rotation" and (individual_path or hot_lane)
            else 0.60 if account_id == NEW_STRATEGY_ID and breakout_probe
            else 0.70 if account_id == MAIN_FORCE_STRATEGY_ID
            else 1.0
        ),
        # 趋势回踩的首笔只建立半仓观察仓；确认加仓由 _swing_scale_in
        # 单独执行，避免一次把“回踩”误当作趋势已经恢复。
        "entry_tranche_scale": 0.50 if account_id == "trend_pullback" else 0.60 if account_id == NEW_STRATEGY_ID else 1.0,
        "execution_mode": (
            "趋势回踩试仓，确认后补齐" if account_id == "trend_pullback"
            else "三日策略试仓，确认后管理" if account_id == NEW_STRATEGY_ID
            else "超强主力前三确认建仓" if account_id == MAIN_FORCE_STRATEGY_ID
            else "热点实时车道试仓" if account_id == "sector_rotation" and hot_lane
            else "个股强势例外试仓" if account_id == "sector_rotation" and individual_path
            else "热点加速试仓" if account_id == "tq_breakout" and breakout_probe
            else "常规入场"
        ),
    }


def _signal_approval(
    account,
    pick,
    quote,
    kline,
    sector_flow,
    market,
    news,
    history_meta=None,
    asof_date=None,
    factor_asof_date=None,
):
    code = pick["code"]
    flags = []
    security_scope = _security_scope(code, quote.get("name") or pick.get("name"), quote.get("risk_flag"))
    if not security_scope["allowed"]:
        flags.append(security_scope["reason"])
    entry_price_allowed, entry_price_reason = _new_entry_price_gate(account, pick, quote)
    if not entry_price_allowed:
        flags.append(entry_price_reason)
    market_policy = _strategy_market_policy(account, pick, quote, market)
    if not market_policy["allowed"]:
        flags.append(market_policy["reason"])
    if quote.get("risk_flag") or "ST" in str(quote.get("name") or "").upper() or "退" in str(quote.get("name") or ""):
        flags.append("ST/退市风险标的")
    if not isinstance(quote.get("price"), (int, float)) or quote.get("price") <= 0:
        flags.append("缺少有效报价")
    if not _quote_is_fresh(quote, asof_date or dt.date.today()):
        flags.append("未取得带当日源时间戳的实时行情")
    elif quote.get("quote_validation") != "cross_source_checked":
        flags.append("\u5b9e\u65f6\u884c\u60c5\u672a\u901a\u8fc7\u72ec\u7acb\u4ea4\u53c9\u6838\u9a8c")
    listing = _new_listing_profile(account["id"], code, kline, history_meta, asof_date or dt.date.today())
    is_new_listing = bool(listing.get("eligible"))
    if kline is None or len(kline) < 120:
        if is_new_listing:
            # 近期上市的主板/创业板股票没有 120 根日线是客观事实。它们不进入
            # 长周期模型，改由独立的流动性/资金/波动观察模型决定是否小仓试错。
            pass
        else:
            flags.append("历史 K 线不足 120 根")
    shadow_news = _negative_hits(news, code)
    dynamic_news = _dynamic_news_risk(news, code, market)
    if not dynamic_news["new_entry_allowed"]:
        flags.append(dynamic_news["reason"])
    meta = history_meta or {}
    unadjusted_fallback = str(meta.get("adjustment") or "").lower() == "none"
    # 历史K线时效应相对最近完整收盘日判断，不能拿行情日线与
    # 因子快照日直接比较；后者可能天然早一个交易日。
    try:
        history_reference_day = U.latest_complete_trade_date(_date(asof_date or dt.date.today()))
    except Exception:
        history_reference_day = _date(asof_date or dt.date.today())
    usable_history, history_lag = _strategy_reference_is_usable(
        account["id"], meta.get("last_date"), history_reference_day
    )
    if not usable_history:
        flags.append(
            f"历史数据滞后 {history_lag if history_lag is not None else '未知'} 个工作日，"
            f"超过本策略上限 {ACCOUNT_SPECS[account['id']]['max_factor_lag']}"
        )
    decision = DE.buy_decision(
        code, name=pick.get("name"), kline=kline, snap=quote,
        sector_flow=sector_flow, overseas_gate=market["overseas"], news_hits=[],
    )
    if unadjusted_fallback:
        decision.setdefault("warnings", []).append("历史K线为不复权兜底，指标仅作降级参考")
        decision["history_quality"] = {
            "adjustment": "none",
            "mode": "degraded_tradable",
            "formal_trade_allowed": True,
            "repair_pending": True,
        }
    decision["shadow_news_warning_count"] = len(shadow_news)
    decision["dynamic_news_risk"] = dynamic_news
    decision["shadow_news_notice"] = (
        "新闻/公告已纳入统一动态风控：核验负面阻止新增仓位，未核验负面仅降级额度"
        if shadow_news else None
    )
    news_overlay = NL.code_overlay(code, asof=asof_date or dt.date.today())
    delta = _entry_score_delta(account, asof_date or dt.date.today()) + _num(news_overlay.get("threshold_delta"))
    if dynamic_news["new_entry_allowed"] and dynamic_news["risk_scale"] < 1:
        delta += 0.05
    entry_model = (
        _new_listing_entry_assessment(account, pick, quote, kline, listing, market=market)
        if is_new_listing else
        _strategy_entry_assessment(account, pick, quote, kline, decision, delta, market=market)
    )
    # 新股路径只是替代长期均线/120日样本要求，绝不绕过价格、跌停、资金或
    # 经验证的硬风险否决；海外红灯仍沿用各策略已有的市场状态门控。
    if is_new_listing:
        flags.extend(
            reason for reason in (decision.get("hard_vetoes") or [])
            if reason != "海外风险红灯"
        )
    flags = [flag for flag in flags if flag]
    entry_model["news_learning"] = news_overlay
    entry_model["dynamic_news_risk"] = dynamic_news
    entry_model["new_listing"] = {
        key: value for key, value in listing.items() if key != "policy"
    }
    flags.extend(entry_model["reasons"])
    decision["entry_model"] = entry_model
    return (not flags), "；".join(flags), decision, market_policy


def generate_signals(asof_date=None):
    """15:05 盘后任务：生成次交易日有效的待审批信号。"""
    init_db()
    day = _date(asof_date)
    if not _is_trade_weekday(day):
        return {"status": "skipped", "reason": "非交易工作日"}
    # 收盘任务先补齐当天完整日线，再重建轻量因子缓存。任何一步失败都要
    # 明确写入任务结果，不能把旧因子继续伪装成当天已更新。
    close_target = U.latest_complete_trade_date(day)
    try:
        history_refresh = U.refresh_history(asof_day=close_target, workers=3, max_seconds=420)
    except Exception as exc:
        history_refresh = {
            "status": "failed",
            "target_date": close_target.isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        factor_refresh = _rebuild_selection_factor_cache(close_target)
    except Exception as exc:
        factor_refresh = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    # 收盘任务必须绕过 240 秒缓存强制刷新一次全市场快照，确保收盘口径不滞留在 14:55 的盘中缓存。
    try:
        close_raw = dfc.fetch_market_snapshot_full(max_age=0, force=True)
        # A historical/research invocation must not compare an old as-of day
        # with today's live timestamp.  Scheduled today's close is the only
        # path that needs the real-time cross-sectional gate.
        close_universe = (
            _validated_live_universe(close_raw, day, max_quote_age_minutes=30)
            if day == dt.date.today() else list(close_raw or [])
        )
        close_snapshot_at = max(
            (str(row.get("quote_at")) for row in close_universe if row.get("quote_at")),
            default=None,
        )
        close_snapshot_refresh = {"status": "ok", "rows": len(close_universe), "source_at": close_snapshot_at}
    except Exception as exc:
        close_universe = []
        close_snapshot_refresh = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    close_gate = (
        _live_scan_gate(close_universe, day)
        if day == dt.date.today() else
        {"ready": True, "mode": "historical", "expected_day": day.isoformat(),
         "covered_codes": len(close_universe), "eligible_codes": None}
    )
    market = _market_state(day, live_universe=close_universe or None)
    summary = {
        "slot": "close",
        "date": day.isoformat(),
        "market": market,
        "market_snapshot_refresh": close_snapshot_refresh,
        "live_scan_gate": close_gate,
        "history_refresh": history_refresh,
        "factor_refresh": factor_refresh,
        "accounts": [],
    }
    # Do not create fresh next-day signals from a partial daily-bar universe.
    # Existing orders and holdings are intentionally untouched; only this close
    # signal generation is deferred until the retry pass has a valid factor set.
    if factor_refresh.get("status") != "ok":
        reason = factor_refresh.get("reason") or factor_refresh.get("error") or "完整日线因子未就绪"
        summary.update({"status": "blocked", "reason": reason})
        with _db() as conn:
            accounts = _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'")
            for account in accounts:
                detail = f"{reason}；{_json(factor_refresh.get('refresh_gate') or {})}"
                _audit(conn, account["id"], "close_signal_blocked_history", detail)
                summary["accounts"].append({
                    "id": account["id"], "created": 0, "blocked": True, "reason": reason,
                })
        return summary
    if day == dt.date.today() and not close_gate["ready"]:
        reason = (
            f"收盘实时行情覆盖 {close_gate['covered_codes']}/{close_gate['eligible_codes']}"
            f"（{close_gate['coverage_pct']:.1f}%），需至少 {close_gate['required_codes']} 只；"
            "本轮不生成新信号，等待下一次数据源重连"
        )
        summary.update({"status": "blocked", "reason": reason})
        with _db() as conn:
            for account in _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'"):
                _audit(conn, account["id"], "close_signal_blocked_market", reason)
                summary["accounts"].append({
                    "id": account["id"], "created": 0, "blocked": True,
                    "reason": reason, "live_scan_gate": close_gate,
                })
        return summary
    # Research observations are intentionally separated from the order ledger.
    # They use only the close snapshot available in this run and cannot affect
    # the pending signals created below.
    try:
        summary["research_observations"] = PR.update_observations(day, close_universe)
    except Exception as exc:
        summary["research_observations"] = {
            "status": "failed", "error": f"{type(exc).__name__}: {exc}"
        }
    try:
        sector_rows = dfc.fetch_hot_sector_snapshot()
        if not sector_rows:
            sector_rows = dfc.fetch_sector_flow("industry")
    except Exception:
        sector_rows = []
    # Build candidates and collect all provider-backed evidence before opening
    # the ledger write transaction.  The previous loop held SQLite's write
    # connection while _candidate_rows fetched finance/flow data and while
    # _quotes/_news_for made network calls; a slow source could block fills,
    # risk exits and the API for minutes.
    with _db_readonly() as read_conn:
        accounts = _rows(read_conn, "SELECT * FROM paper_accounts WHERE status='running'")
    candidate_batches = []
    all_names = {}
    all_codes = set()
    for account in accounts:
        candidates, meta = _candidate_rows(
            account, day, market, sector_rows=sector_rows,
            live_universe=close_universe or None,
        )
        candidate_batches.append((account, candidates, meta))
        for pick in candidates:
            code = str(pick.get("code") or "")
            if code:
                all_codes.add(code)
                all_names[code] = pick.get("name") or code
    try:
        evidence_news = _news_for(all_names) if all_names else []
    except Exception:
        evidence_news = []
    try:
        evidence_sector_flow = dfc.fetch_sector_flow("industry")
    except Exception:
        evidence_sector_flow = []
    try:
        evidence_quotes = _quotes(sorted(all_codes), asof_date=day) if all_codes else {}
    except Exception:
        evidence_quotes = {}
    evidence_history = _history_manifest()
    # Research ledgers use their own SQLite connections.  Keep those writes
    # outside the trading-ledger transaction: opening a second connection
    # while ``conn`` holds the signal write lock can block the scan (and, on a
    # busy WAL, risk exits) even though the research tables are optional.
    research_results = {}
    for account, candidates, meta in candidate_batches:
        account_id = account["id"]
        research = {}
        try:
            research["candidate_snapshot"] = NL.capture_candidate_snapshot(
                account_id, candidates, day, slot="close",
            )
        except Exception as exc:
            research["candidate_snapshot_error"] = f"{type(exc).__name__}: {exc}"
        if not meta.get("blocked"):
            try:
                research["shadow"] = PR.record_shadow_run(
                    account_id, candidates, meta=meta, market=market, signal_date=day,
                )
            except Exception as exc:
                research["shadow_error"] = f"{type(exc).__name__}: {exc}"
        research_results[account_id] = research
    with _db() as conn:
        for account, candidates, meta in candidate_batches:
            # A pause/reset may occur while provider calls are in flight.  Do
            # not write signals for an account that is no longer active.
            current = conn.execute(
                "SELECT status,cycle_id FROM paper_accounts WHERE id=?", (account["id"],)
            ).fetchone()
            if not current or current["status"] != "running":
                continue
            research = research_results.get(account["id"], {})
            if research.get("candidate_snapshot_error"):
                _audit(conn, account["id"], "candidate_snapshot_failed", research["candidate_snapshot_error"])
            if meta.get("blocked"):
                _audit(conn, account["id"], "signal_blocked", meta["reason"])
                summary["accounts"].append({"id": account["id"], "created": 0, **meta})
                continue
            if research.get("shadow") is not None:
                meta["research_shadow"] = research["shadow"]
            if research.get("shadow_error"):
                # A research-ledger failure must be visible and retryable, but a
                # close signal that has already passed the existing data gates is
                # not allowed to disappear because an optional shadow write failed.
                meta["research_shadow"] = {"status": "failed", "error": research["shadow_error"]}
                _audit(conn, account["id"], "research_shadow_failed", research["shadow_error"])
            created = 0
            for pick in candidates:
                code = pick["code"]
                quote = evidence_quotes.get(code, {})
                kline = _completed_kline(code, day)
                passed, reason, decision, market_policy = _signal_approval(
                    account, pick, quote, kline, evidence_sector_flow, market,
                    evidence_news, evidence_history.get(code), day
                )
                payload = {"pick": pick, "decision": decision, "market": market, "factor": meta,
                           "market_policy": market_policy, "quote": quote,
                           "news": [n for n in evidence_news if n.get("code") == code]}
                payload = _with_decision_snapshot(
                    payload, account_id=account["id"], code=code, side="buy",
                    decision=("approved_signal" if passed else "rejected_signal"),
                    reason=reason, asof_date=day, quote=quote, kline=kline,
                    news=payload.get("news"),
                    final_score=(decision.get("entry_model") or {}).get("score"),
                )
                status = "pending" if passed else "blocked"
                conn.execute(
                    """INSERT OR IGNORE INTO paper_signals(account_id,signal_date,intended_date,code,name,industry,close_price,rank_score,t_tier,t_score,payload,status,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (account["id"], day.isoformat(), _next_weekday(day).isoformat(), code, pick.get("name"),
                     pick.get("industry"), _num(quote.get("price"), _num(pick.get("price"))), _num(pick.get("score")),
                     decision.get("tier"), _num((decision.get("entry_model") or {}).get("score")),
                     _json(payload), status, reason, _now()),
                )
                _risk_log(conn, account["id"], code, "buy", "approved_signal" if passed else "rejected_signal", reason, payload)
                created += int(passed)
            _audit(conn, account["id"], "close_signal_scan", f"候选 {len(candidates)}，通过 {created}")
            summary["accounts"].append({"id": account["id"], "created": created, "candidates": len(candidates), **meta})
    return summary


def backfill_research_shadow(asof_date=None):
    """Create a missing close research snapshot without touching the trading ledger.

    This is deliberately *not* a second close run: it does not insert signals,
    write orders, alter risk decisions or add an audit decision.  It exists only
    to repair a newly deployed research ledger after the regular close job has
    already completed.  The immutable research table still accepts just one
    snapshot per strategy/date/version.
    """
    init_db()
    day = _date(asof_date)
    if not _is_trade_weekday(day):
        return {"status": "skipped", "reason": "非交易工作日", "date": day.isoformat()}
    try:
        close_universe = dfc.fetch_market_snapshot_full(max_age=0, force=True)
        close_snapshot_at = max(
            (str(row.get("quote_at")) for row in close_universe if row.get("quote_at")),
            default=None,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "date": day.isoformat(),
            "reason": f"全市场收盘快照不可用：{type(exc).__name__}: {exc}",
        }
    if not close_universe:
        return {"status": "skipped", "date": day.isoformat(), "reason": "全市场收盘快照为空"}
    market = _market_state(day, live_universe=close_universe)
    try:
        PR.update_observations(day, close_universe)
    except Exception:
        # Observation backfill is best effort.  Candidate evidence remains safe
        # to write and will receive its next close observation on a later run.
        pass
    try:
        sector_rows = dfc.fetch_hot_sector_snapshot() or dfc.fetch_sector_flow("industry")
    except Exception:
        sector_rows = []
    summary = {
        "status": "completed",
        "mode": "research_only",
        "date": day.isoformat(),
        "snapshot_at": close_snapshot_at,
        "accounts": [],
    }
    with _db() as conn:
        accounts = _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'")
        for account in accounts:
            candidates, meta = _candidate_rows(
                account, day, market, sector_rows=sector_rows, live_universe=close_universe,
            )
            meta = dict(meta or {})
            meta["research_backfill"] = True
            meta["research_backfill_at"] = _now()
            if meta.get("blocked"):
                summary["accounts"].append({
                    "id": account["id"], "status": "skipped", "reason": meta.get("reason"),
                })
                continue
            try:
                saved = PR.record_shadow_run(
                    account["id"], candidates, meta=meta, market=market, signal_date=day,
                )
            except Exception as exc:
                saved = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            summary["accounts"].append({
                "id": account["id"], "candidates": len(candidates), "research": saved,
            })
    return summary


def manual_research_backfill():
    """Safely repair today's shadow-research close snapshot on demand.

    The button is intentionally *not* a historical re-run.  Reconstructing an
    older day from a later quote would introduce look-ahead bias, while a full
    close run would create live paper signals.  It is therefore available only
    after the current trading day's 15:05 close window and delegates to the
    research-only backfill path above.
    """
    now = dt.datetime.now()
    day = now.date()
    if not _is_trade_weekday(day):
        return {
            "status": "skipped",
            "reason": "非交易日不补录；下一交易日收盘后会自动补齐观察。",
            "date": day.isoformat(),
            "manual_allowed": False,
        }
    if now.time() < dt.time(15, 5):
        return {
            "status": "skipped",
            "reason": "仅在当日 15:05 收盘任务之后允许补录，避免使用未完整的日线与收盘快照。",
            "date": day.isoformat(),
            "manual_allowed": False,
        }
    result = backfill_research_shadow(day)
    result["manual_allowed"] = True
    result["policy"] = "仅补录当日完整收盘快照与既有样本当日观察；不补写历史候选，不生成信号、委托或风控决策。"
    return result


def _account_exposure(conn, account_id, quotes):
    positions = _position_rows(conn, account_id)
    value = 0.0
    industries = {}
    for pos in positions:
        price = _num((quotes.get(pos["code"]) or {}).get("price"), _num(pos["cost"]))
        item_value = _num(pos["qty"]) * price
        value += item_value
        industries[pos.get("industry") or "未知"] = industries.get(pos.get("industry") or "未知", 0.0) + item_value
    account = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
    nav = _num(account["cash"]) + value
    return positions, value, nav, industries


def _runtime_parameter_active(effective_date=None, asof_day=None, status=None):
    """Whether a live parameter may affect a decision made now.

    A parameter that is already marked ``active`` is a runtime control, not a
    next-day promise.  This makes intraday risk tightening/relaxing take
    effect on the very next scan.  Historical replay remains point-in-time:
    when evaluating a past date, the recorded effective date is still
    honoured and a later live overlay cannot leak backwards.
    """
    if status is not None and str(status) != "active":
        return False
    target_day = _date(asof_day or dt.date.today())
    today = _date()
    if target_day >= today:
        return True
    effective = str(effective_date or "")[:10]
    return bool(effective and effective <= target_day.isoformat())


def _risk_profile(account, asof_day=None):
    account_id = account.get("id")
    default_key = (ACCOUNT_SPECS.get(account_id) or {}).get("risk_profile", "trend")
    profile = dict(RISK_PROFILES.get(account.get("risk_profile"), RISK_PROFILES[default_key]))
    # Expose the shared staged downside guard through the same active risk
    # profile used by sizing/entry.  Without an overlay the three strategy
    # defaults remain exactly those defined by the execution policy.
    account_id = str(account.get("id") or "")
    for key, value in (INTRADAY_DOWNSIDE_POLICIES.get(account_id) or {}).items():
        profile[f"downside_{key}"] = value
    params = _loads(account.get("params"), {})
    overlay = params.get("adaptive_risk") or {}
    meta = params.get("adaptive_risk_meta") or {}
    if _runtime_parameter_active(
        meta.get("effective_date"), asof_day=asof_day, status=meta.get("status")
    ):
        for key, bounds in ADAPTIVE_RISK_BOUNDS.items():
            if key not in overlay:
                continue
            value = max(bounds[0], min(bounds[1], _num(overlay[key], profile.get(key, bounds[0]))))
            profile[key] = int(round(value)) if key == "cooldown_days" else round(value, 6)
        profile["adaptive_version"] = meta.get("version")
        profile["adaptive_candidate_id"] = meta.get("candidate_id")
    return profile


# The pool cap is hard, but it is not a per-strategy cap.  Each strategy gets
# a risk-profile-weighted target inside the pool and a protected floor.  This
# keeps a single signal stream from consuming all 82% before the other two
# strategies have a chance to act; unused capacity can be lent only after the
# other strategies have reached their floors.
STRATEGY_POOL_FLOOR_RATIO = 0.60
# Four strategy accounts share the pool.  2026-08-24 集中化改造：
# 12 是硬上限（每策略 3 席），单仓风险预算同步放大，使资金利用率
# 从 ~35% 提升到 ~75%；"集中力量办大事"而不是 17 席 × 零散小仓。
SHARED_POOL_MAX_POSITIONS = 15
STRATEGY_MIN_POSITIONS = 2
STRATEGY_MAX_POSITIONS = 6
# 四套模型共用资金但代表不同的市场假设。12 席总池下每策略保底 2 席，
# 该底座只保护"席位数"，不会放宽总暴露、单票/行业上限、行情校验或
# 单笔风险预算。
STRATEGY_PROTECTED_SLOT_FLOOR = 2
SLOT_UPGRADE_MIN_CANDIDATE_SCORE = 75.0
SLOT_UPGRADE_MIN_EDGE = 25.0
# A seat transfer is less aggressive than a forced quality rotation: it is
# allowed only for a genuinely strong candidate, but does not require the
# candidate to beat an existing holding by the full urgent-replacement margin.
SLOT_BORROW_MIN_CANDIDATE_SCORE = 65.0
SLOT_BORROW_MIN_EDGE = 12.0
# 总席位是全天风险容量，不是 09:30 后可一次性买满的目标。候选仍由
# 全市场实时重排，但新开仓按确认窗口逐步释放；做T回补、既有持仓加仓和
# 风险卖出不占用这条新票释位规则。
ENTRY_DEPLOYMENT_WINDOWS = (
    ("09:30", "09:45", 4, 1, "开盘确认期：最多 4 席位，每策略最多 1 席位"),
    ("09:45", "10:30", 8, 2, "早盘确认期：最多 8 席位，每策略最多 2 席位"),
    ("10:30", "13:05", 10, 3, "上午/午间复核期：最多 10 席位，每策略最多 3 席位"),
    ("13:05", "14:30", SHARED_POOL_MAX_POSITIONS, STRATEGY_MAX_POSITIONS, "午后扩展期：按动态上限释位"),
)


def _cycle_first_trade_day(cycle):
    """Return the first market day on which a newly started cycle can trade.

    A cycle may be created on a weekend or after the cash session.  In those
    cases the staged opening allocation belongs to the next trading day, not
    to the calendar date stored in ``started_at``.  Legacy rows without a
    usable timestamp fail safely to today's trading day.
    """
    raw = (cycle or {}).get("started_at") or (cycle or {}).get("created_at")
    try:
        started = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        started_day = started.date()
    except (TypeError, ValueError):
        started = None
        started_day = dt.date.today()
    if not U.is_trade_day(started_day):
        return U.next_trade_day(started_day)
    # A cycle enabled after the close has its first executable session on the
    # next trading day.  Date-only legacy timestamps keep same-day semantics.
    has_explicit_time = started is not None and ("T" in str(raw) or " " in str(raw))
    if has_explicit_time:
        if started.time().replace(tzinfo=None) >= dt.time(15, 0):
            return U.next_trade_day(started_day)
    return started_day


def _position_limit_window(now=None):
    """Return the audit window for a position-allocation version.

    The window is deliberately not the activation gate.  A fingerprint of the
    active risk inputs is appended by ``_dynamic_position_limits`` so a live
    risk change creates a new version immediately, while repeated reads under
    identical inputs reuse one stable version.
    """
    now = now or dt.datetime.now()
    clock = now.strftime("%H:%M")
    if clock < "12:00":
        phase = "morning"
    elif clock < "15:15":
        phase = "afternoon"
    else:
        phase = "after_close"
    return f"{now.date().isoformat()}:{phase}"


def _entry_deployment_gate(conn, account_id, code, positions, pending_slots, count_budget, asof_day, now=None):
    """Gate *new* automatic positions by intraday deployment phase.

    The dynamic limits stay the hard, all-day ceiling.  This helper merely
    prevents an opening scan from consuming every available slot before the
    market has had time to confirm breadth, sector rotation and money flow.
    Historical replay remains deterministic and is not paced by today's wall
    clock.
    """
    day = _date(asof_day)
    if day != dt.date.today():
        return {"allowed": True, "phase": "historical_replay", "reason": "历史回放不使用实时释位时钟"}
    cycle = _active_cycle(conn)
    first_trade_day = _cycle_first_trade_day(cycle)
    if day != first_trade_day:
        return {
            "allowed": True,
            "phase": "cycle_followup_day",
            "first_trade_day": first_trade_day.isoformat(),
            "reason": "非新周期首个交易日，取消分时释位限制，按动态总席位与策略上限审批",
        }
    now = now or dt.datetime.now()
    clock = now.strftime("%H:%M")
    stage = next((item for item in ENTRY_DEPLOYMENT_WINDOWS if item[0] <= clock < item[1]), None)
    if stage is None:
        return {
            "allowed": False, "phase": "closed", "clock": clock,
            "reason": "14:30 后不新增普通自动仓，保留风控、做T回补和等待池次日重排",
        }
    _, _, stage_pool_cap, stage_strategy_cap, label = stage
    dynamic_pool = int(count_budget.get("pool_limit") or SHARED_POOL_MAX_POSITIONS)
    dynamic_strategy = int((count_budget.get("limits") or {}).get(account_id, STRATEGY_MAX_POSITIONS))
    pool_cap = min(dynamic_pool, int(stage_pool_cap))
    strategy_cap = min(dynamic_strategy, int(stage_strategy_cap))
    open_pairs = {
        (str(item.get("account_id")), str(item.get("code")))
        for item in positions
        if int(_num(item.get("qty"))) >= LOT_SIZE
    } | set(pending_slots or set())
    own_codes = {pair_code for pair_account, pair_code in open_pairs if pair_account == account_id}
    existing = str(code) in own_codes
    allowed = existing or (len(open_pairs) < pool_cap and len(own_codes) < strategy_cap)
    return {
        "allowed": allowed,
        "phase": label,
        "clock": clock,
        "pool_current": len(open_pairs), "pool_cap": pool_cap,
        "strategy_current": len(own_codes), "strategy_cap": strategy_cap,
        "dynamic_pool_cap": dynamic_pool, "dynamic_strategy_cap": dynamic_strategy,
        "reason": (
            "已有持仓不受新票释位限制" if existing else
            ("分时释位通过" if allowed else
             f"{label}：当前总席位 {len(open_pairs)}/{pool_cap}，策略席位 {len(own_codes)}/{strategy_cap}；保留在等待池按下一窗口实时重排")
        ),
    }


def _dynamic_position_limits(conn):
    """Return a versioned allocation inside the 15-slot hard cap.

    ``pool_limit`` is an effective deployable limit, not a target that must
    always be filled.  When the aggregate risk budget is reduced, the system
    can leave seats empty.  Any active risk-profile change is immediately
    reflected in a new allocation version; contraction is still executed only
    through the staged, T+1/quote/limit-aware capacity-exit path.
    """
    cycle = _active_cycle(conn)
    all_rows = _shared_account_rows(conn, cycle["id"])
    running_rows = [row for row in all_rows if row.get("status") == "running"]
    rows = running_rows or all_rows
    account_ids = [key for key in ACCOUNT_SPECS if any(row.get("id") == key for row in rows)]
    if not account_ids:
        account_ids = list(ACCOUNT_SPECS)
    row_map = {row.get("id"): row for row in rows}
    profiles = {
        account_id: _risk_profile(row_map.get(account_id) or {"id": account_id})
        for account_id in account_ids
    }
    weights = {
        account_id: max(_num(profiles[account_id].get("max_exposure")), 0.01)
        for account_id in account_ids
    }
    runtime_inputs = {
        "window": _position_limit_window(),
        "hard_pool_cap": SHARED_POOL_MAX_POSITIONS,
        "protected_slot_floor": STRATEGY_PROTECTED_SLOT_FLOOR,
        # P3 审计修复（R7）：分配公式/常量参与指纹——改公式发版后当天
        # 同 phase 不再复用旧版本行的过时席位分配。
        "allocation_formula": "slots-v3",
        "accounts": {
            account_id: {
                "max_exposure": round(weights[account_id], 6),
                "adaptive_version": profiles[account_id].get("adaptive_version"),
                "adaptive_candidate_id": profiles[account_id].get("adaptive_candidate_id"),
            }
            for account_id in account_ids
        },
        "running_accounts": [row.get("id") for row in running_rows],
    }
    fingerprint = hashlib.sha1(_json(runtime_inputs).encode("utf-8")).hexdigest()[:12]
    allocation_key = f"{runtime_inputs['window']}:risk-{fingerprint}"
    existing = conn.execute(
        """SELECT * FROM paper_position_limit_versions
           WHERE cycle_id=? AND allocation_key=?""",
        (cycle["id"], allocation_key),
    ).fetchone()
    if existing:
        limits = _loads(existing["limits"], {})
        weights = _loads(existing["weights"], {})
        inputs = _loads(existing["inputs"], {})
        return {
            "pool_limit": int(_num(existing["pool_limit"], SHARED_POOL_MAX_POSITIONS)),
            "limits": {key: int(_num(value)) for key, value in limits.items()},
            "weights": weights,
            "source": existing["source"],
            "allocation_version": f"slots-v{existing['id']}",
            "effective_at": existing["effective_at"],
            "allocation_key": allocation_key,
            "slot_borrow": inputs.get("last_slot_borrow"),
        }
    count = len(account_ids)
    baseline = sum(_num(ACCOUNT_SPECS[key].get("max_exposure"), 0.0) for key in account_ids) / max(count, 1)
    current = sum(weights.values()) / max(count, 1)
    # The hard cap is 15.  A material aggregate risk reduction leaves
    # one or more seats empty instead of pretending every strategy should be
    # fully deployed.  With four models, each retains a protected expression
    # floor when the risk budget permits; the risk profile still
    # constrains capital, single-name size and entry permission.
    hard_cap = min(SHARED_POOL_MAX_POSITIONS, STRATEGY_MAX_POSITIONS * count)
    risk_scale = max(0.60, min(1.0, current / max(baseline, 0.01)))
    base_floor_total = min(hard_cap, STRATEGY_MIN_POSITIONS * count)
    total_cap = max(base_floor_total, min(hard_cap, int(round(hard_cap * risk_scale))))
    protected_floor = (
        min(STRATEGY_PROTECTED_SLOT_FLOOR, STRATEGY_MAX_POSITIONS)
        if total_cap >= STRATEGY_PROTECTED_SLOT_FLOOR * count else
        STRATEGY_MIN_POSITIONS
    )
    minimum = min(protected_floor, total_cap // max(count, 1))
    weight_total = sum(weights.values()) or 1.0
    raw = {key: total_cap * weights[key] / weight_total for key in account_ids}
    strategy_caps = {key: (3 if key == MAIN_FORCE_STRATEGY_ID else STRATEGY_MAX_POSITIONS)
                     for key in account_ids}
    limits = {
        key: max(minimum, min(strategy_caps[key], int(raw[key])))
        for key in account_ids
    }
    order = {key: idx for idx, key in enumerate(ACCOUNT_SPECS)}
    while sum(limits.values()) < total_cap:
        candidates = [key for key in account_ids if limits[key] < strategy_caps[key]]
        if not candidates:
            break
        key = max(candidates, key=lambda item: (raw[item] - limits[item], weights[item], -order.get(item, 99)))
        limits[key] += 1
    while sum(limits.values()) > total_cap:
        candidates = [key for key in account_ids if limits[key] > minimum]
        if not candidates:
            break
        key = max(candidates, key=lambda item: (limits[item] - raw[item], -weights[item], order.get(item, 99)))
        limits[key] -= 1
    now = _now()
    cursor = conn.execute(
        """INSERT INTO paper_position_limit_versions(
               cycle_id,allocation_key,pool_limit,limits,weights,inputs,source,effective_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            cycle["id"], allocation_key, total_cap, _json(limits), _json(weights),
            _json({"baseline_exposure": baseline, "current_exposure": current,
                   "risk_scale": risk_scale, "runtime_inputs": runtime_inputs,
                   "protected_slot_floor": protected_floor,
                   "activation": "active-risk-change-immediate"}),
            "versioned_runtime_active_risk_budget", now, now,
        ),
    )
    return {
        "pool_limit": total_cap,
        "limits": limits,
        "weights": {key: round(weights[key], 4) for key in account_ids},
        "source": "versioned_runtime_active_risk_budget",
        "allocation_version": f"slots-v{cursor.lastrowid}",
        "effective_at": now,
        "allocation_key": allocation_key,
        "slot_borrow": None,
    }


def _strategy_pool_budget(conn, account, nav, positions, quotes, market=None, exclude_reservation_key=None):
    """Return the fair shared-pool budget for one strategy.

    ``target_amount`` is a soft target, ``floor_amount`` is the amount kept
    available for the other strategies, and ``allowance_amount`` is the
    actual additional amount this strategy may open right now.  All values
    are derived from the same live quote snapshot used by the order gate.
    """
    rows = _shared_account_rows(conn)
    if not rows:
        rows = [account]
    values = {}
    profiles = {}
    weights = {}
    for row in rows:
        row_id = row.get("id")
        if not row_id:
            continue
        profiles[row_id] = _risk_profile(row)
        weights[row_id] = max(_num(profiles[row_id].get("max_exposure"), 0.0), 0.01)
        values[row_id] = 0.0
    if account.get("id") not in values:
        account_id = account.get("id")
        profiles[account_id] = _risk_profile(account)
        weights[account_id] = max(_num(profiles[account_id].get("max_exposure"), 0.0), 0.01)
        values[account_id] = 0.0
    for position in positions or []:
        account_id = position.get("account_id")
        if account_id not in values:
            continue
        quote = (quotes or {}).get(position.get("code")) or {}
        price = _num(quote.get("price"), _num(position.get("cost")))
        values[account_id] += max(0.0, _num(position.get("qty"))) * max(0.0, price)

    pending_by_account, pending_total = _pending_buy_reservations(
        conn, exclude_order_key=exclude_reservation_key,
    )
    nav = max(_num(nav), 0.0)
    pool_cap_amount = nav * SHARED_POOL_MAX_EXPOSURE
    pool_value = sum(values.values())
    # Pending buys consume both cash and pool capacity before they fill.
    # They are not added to market value, so keep the two figures explicit.
    global_remaining = max(0.0, pool_cap_amount - pool_value - pending_total)
    weight_total = sum(weights.values()) or 1.0
    base_target_pct = {
        key: SHARED_POOL_MAX_EXPOSURE * weight / weight_total
        for key, weight in weights.items()
    }
    market_light = str((market or {}).get("light") or "").lower()
    # A missing market argument is used by read-only dashboard aggregation;
    # the execution path always supplies the current market gate.
    scales = market_light_scales(market_light) if market_light else None
    target_pct = {
        key: value * (scales.get(key, 0.0) if scales is not None else 1.0)
        for key, value in base_target_pct.items()
    }
    floor_pct = {key: value * STRATEGY_POOL_FLOOR_RATIO for key, value in target_pct.items()}
    account_id = account.get("id")
    # 超强主力资金优先：目标与地板取“灯色缩水目标”与“优先预留份额”的
    # 较大者。预留写入 floor_pct 后，其他策略计算 other_floor_reserve 时
    # 自动把它当受保护额度——这就是资金分配优先级的落点。
    priority_floor_amount = (
        nav * MAIN_FORCE_PRIORITY_FLOOR_PCT
        if account_id == MAIN_FORCE_STRATEGY_ID else 0.0
    )
    if priority_floor_amount > 0.0:
        target_pct[account_id] = max(
            target_pct.get(account_id, 0.0), MAIN_FORCE_PRIORITY_FLOOR_PCT)
        floor_pct[account_id] = max(
            floor_pct.get(account_id, 0.0), MAIN_FORCE_PRIORITY_FLOOR_PCT)
    current_amount = values.get(account_id, 0.0)
    pending_strategy_amount = pending_by_account.get(account_id, 0.0)
    current_total_amount = current_amount + pending_strategy_amount
    target_amount = nav * target_pct.get(account_id, 0.0)
    floor_amount = nav * floor_pct.get(account_id, 0.0)
    own_headroom = max(0.0, target_amount - current_total_amount)
    other_floor_reserve = sum(
        max(0.0, nav * floor_pct.get(key, 0.0)
            - values.get(key, 0.0) - pending_by_account.get(key, 0.0))
        for key in values if key != account_id
    )
    after_floor = max(0.0, global_remaining - other_floor_reserve)
    other_floors_met = all(
        values.get(key, 0.0) + pending_by_account.get(key, 0.0) + 1e-6
        >= nav * floor_pct.get(key, 0.0)
        for key in values if key != account_id
    )
    # Only capacity left after the other floors are protected may be
    # redistributed above this strategy's target.
    redistribution = max(0.0, after_floor - own_headroom) if other_floors_met else 0.0
    allowance = min(global_remaining, after_floor, own_headroom + redistribution)
    # Sector rotation is the shortest-horizon and most concentrated sleeve.
    # Its shared-pool budget must not turn into implicit leverage when account
    # NAV is reduced by inter-sleeve transfers.  Cap gross committed value at
    # 100% of its own NAV; exits and risk actions are unaffected.
    if account_id == "sector_rotation" and nav > 0:
        sector_cap = nav
        allowance = min(allowance, max(0.0, sector_cap - current_total_amount))
    # Gross committed cap: current holding + already-reserved pending orders
    # + the net new allowance.  The sizing layer subtracts current and pending
    # once, so pending capital is no longer double-counted.
    absolute_cap = current_total_amount + max(0.0, allowance)
    return {
        "account_id": account_id,
        "target_pct": round(target_pct.get(account_id, 0.0) * 100, 2),
        "base_target_pct": round(base_target_pct.get(account_id, 0.0) * 100, 2),
        "market_scale_pct": round((scales.get(account_id, 0.0) if scales is not None else 1.0) * 100, 1),
        # target_amount / absolute_cap_amount above already include the
        # market-light multiplier.  Callers must not multiply the same market
        # coefficient a second time when turning remaining budget into shares.
        "market_scale_applied": bool(scales is not None),
        "floor_pct": round(floor_pct.get(account_id, 0.0) * 100, 2),
        "priority_floor_pct": (
            round(MAIN_FORCE_PRIORITY_FLOOR_PCT * 100, 2)
            if priority_floor_amount > 0.0 else None),
        "priority_floor_amount": round(priority_floor_amount, 2),
        "current_pct": round(current_amount / nav * 100, 2) if nav else 0.0,
        "target_amount": round(target_amount, 2),
        "floor_amount": round(floor_amount, 2),
        "current_amount": round(current_amount, 2),
        "pending_reserve_amount": round(pending_strategy_amount, 2),
        "current_total_amount": round(current_total_amount, 2),
        "allowance_amount": round(max(0.0, allowance), 2),
        "absolute_cap_amount": round(max(0.0, absolute_cap), 2),
        "global_remaining_amount": round(global_remaining, 2),
        "other_floor_reserve": round(other_floor_reserve, 2),
        "redistribution_amount": round(redistribution, 2),
        "redistribution_allowed": bool(redistribution > 0.0),
        "pool_value": round(pool_value, 2),
        "pool_cap_amount": round(pool_cap_amount, 2),
        "pool_exposure_pct": round(pool_value / nav * 100, 2) if nav else 0.0,
        "pending_pool_reserve_amount": round(pending_total, 2),
        "pool_committed_amount": round(pool_value + pending_total, 2),
        "pool_committed_pct": round((pool_value + pending_total) / nav * 100, 2) if nav else 0.0,
        "pool_available_amount": round(global_remaining, 2),
        "pool_limit_pct": round(SHARED_POOL_MAX_EXPOSURE * 100, 2),
        "other_floors_met": bool(other_floors_met),
    }


def _entry_execution_scale(market_policy, entry_model, chase_entry, dynamic_news, strategy_budget):
    """Return the one-time sizing multiplier for a new entry.

    ``_strategy_pool_budget`` bakes the green/yellow/red market multiplier
    into the strategy target and absolute cap.  Multiplying it again here
    reduces the *remaining* amount twice, which is especially visible after
    a first fill: e.g. a 65% yellow budget became 65% * 65% before lot
    rounding.

    P3 档位制：不再让 chase/模型/新闻多个缩放连乘把仓位打到无表达力
    （旧热点试仓 0.5 × caution 0.25 = 12.5%）。最终档位——
    - 追高/热点加速试仓通过：固定 0.50（独立档，不再乘模型 caution）；
    - 普通入场：模型×新闻缩放，下限 0.35（保留谨慎表达但不打成尘埃）；
    - 市场系数仅在策略预算未含时叠加一次。
    """
    chase = chase_entry or {}
    if chase.get("allowed") or "试仓" in str(chase.get("mode") or ""):
        scale = 0.50
    else:
        scale = (
            _num((entry_model or {}).get("position_scale"), 1.0)
            * _num((dynamic_news or {}).get("risk_scale"), 1.0)
        )
        scale = max(0.35, min(scale, 1.0))
    market_scale = _num(market_policy.get("risk_scale"), 1.0)
    if not bool((strategy_budget or {}).get("market_scale_applied")):
        scale *= market_scale
    return max(0.0, min(scale, 1.0))


def _adaptive_selection(account, asof_day=None):
    """Return a bounded paper-only ranking overlay once it is active."""
    params = _loads(account.get("params"), {})
    overlay = params.get("adaptive_selection") or {}
    meta = params.get("adaptive_selection_meta") or {}
    if not _runtime_parameter_active(
        meta.get("effective_date"), asof_day=asof_day, status=meta.get("status")
    ):
        return {}
    weights = overlay.get("weights") or {}
    source = ACCOUNT_SPECS.get(account.get("id"), {}).get("source_strategy")
    model_family = str(overlay.get("model_family") or source)
    allowed = {
        "tq_breakout": {"one_to_two"},
        "trend_pullback": {"bottom_reversal", "trend_continuation"},
        "sector_rotation": {"sentiment_pioneer"},
        NEW_STRATEGY_ID: {NEW_STRATEGY_ID},
    }.get(account.get("id"), {source})
    if model_family not in allowed:
        model_family = source
    expected = set(S.PAPER_WEIGHTS.get(model_family, {}))
    if set(weights) != expected:
        weights = {}
    conditions = overlay.get("conditions") or {}
    return {
        "weights": weights,
        "conditions": conditions if isinstance(conditions, dict) else {},
        "model_family": model_family,
        "entry_paths": overlay.get("entry_paths") or {"normal": True},
        "mutation_type": overlay.get("mutation_type") or "none",
        "objectives": overlay.get("objectives") or [],
        "entry_score_delta": max(-0.02, min(0.03, _num(overlay.get("entry_score_delta"), 0.0))),
        "version": meta.get("version"), "candidate_id": meta.get("candidate_id"), "tier": meta.get("tier"),
    }


def _entry_score_delta(account, asof_day=None):
    adaptive = _adaptive_selection(account, asof_day)
    if adaptive:
        return adaptive["entry_score_delta"]
    params = _loads(account.get("params"), {})
    effective = params.get("effective_date")
    if effective and not _runtime_parameter_active(effective, asof_day=asof_day):
        return 0.0
    return _num(params.get("entry_score_delta"), _num(params.get("min_t_score_delta")))


def _account_risk_state(conn, account, nav, asof_day):
    """账户级熔断：达到阈值只禁止新开仓，卖出风控仍会继续。"""
    profile = _risk_profile(account)
    day = _date(asof_day).isoformat()
    start_nav = _num(account.get("daily_start_nav"), 0)
    if account.get("daily_nav_date") != day or start_nav <= 0:
        start_nav = nav
        conn.execute("UPDATE paper_accounts SET daily_start_nav=?,daily_nav_date=? WHERE id=?", (nav, day, account["id"]))
        account["daily_start_nav"], account["daily_nav_date"] = nav, day
    navs = [r[0] for r in conn.execute("SELECT nav FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account["id"],)).fetchall()]
    peak = max(navs + [nav, _account_reference_capital(account) or nav])
    drawdown = 1 - nav / peak if peak else 0.0
    daily_loss = 1 - nav / start_nav if start_nav else 0.0
    reasons = []
    cooldown = account.get("cooldown_until")
    if cooldown and str(cooldown) >= day:
        reasons.append(f"冷静期至 {cooldown}")
    if daily_loss >= profile["daily_loss"]:
        reasons.append(f"单日亏损 {daily_loss*100:.2f}% 触发熔断")
    if drawdown >= profile["drawdown"]:
        until = _next_weekday(_date(asof_day) + dt.timedelta(days=profile["cooldown_days"] - 1)).isoformat()
        conn.execute("UPDATE paper_accounts SET cooldown_until=? WHERE id=?", (until, account["id"]))
        account["cooldown_until"] = until
        reasons.append(f"滚动回撤 {drawdown*100:.2f}% 触发冷静期")
    return {"blocked": bool(reasons), "reasons": reasons, "daily_loss_pct": round(daily_loss * 100, 2), "drawdown_pct": round(drawdown * 100, 2)}


def _price_aware_qty(
    nav, cash, position_value, industry_value, code_value,
    fill_price, hard_stop, profile, exposure_cap=None, max_exposure_cap=None,
    exposure_scale=1.0, strategy_position_value=None,
    strategy_cap_amount=None, pool_cap_amount=None,
    pending_strategy_amount=0.0, pending_pool_amount=0.0,
):
    """按股数计算下单规模；数量席位是容量硬门，金额约束只做软 sizing。

    现金、行情、T+1、跌停和硬风险仍由调用方的安全门禁负责；这里的
    strategy/pool/industry amount limits 只决定本次最多下多少股。若不足一手，
    调用方应把候选放入等待池，等待额度释放后按最新行情复核。
    """
    if fill_price <= 0:
        return 0, {"target_amount": 0.0, "reason": "无有效价格"}
    loss_per_share = fill_price * max(abs(hard_stop), 0.01)
    risk_budget = nav * profile["single_risk"]
    by_risk = risk_budget / loss_per_share
    by_weight = max(0.0, nav * profile["max_weight"] - code_value) / fill_price
    profile_exposure = _num(max_exposure_cap, profile["max_exposure"])
    effective_exposure = min(profile_exposure, _num(exposure_cap, profile_exposure))
    # 市场黄灯的仓位系数只作用于“剩余可用额度”。
    # 旧逻辑把 max_exposure 直接乘 risk_scale，导致已有持仓超过缩小后的新上限时，
    # 可用额度被算成 0；例如 65% 上限、当前 49.7% 持仓、黄灯 65% 时会错误得到 42.25% 上限。
    scale = max(0.0, min(_num(exposure_scale, 1.0), 1.0))
    pool_limit = nav * effective_exposure if pool_cap_amount is None else _num(pool_cap_amount)
    pool_remaining = max(
        0.0,
        pool_limit - _num(position_value) - max(0.0, _num(pending_pool_amount)),
    )
    if strategy_cap_amount is None:
        strategy_remaining = pool_remaining
    else:
        strategy_remaining = max(
            0.0,
            _num(strategy_cap_amount) - _num(strategy_position_value)
            - max(0.0, _num(pending_strategy_amount)),
        )
    remaining_exposure = min(pool_remaining, strategy_remaining)
    by_exposure = remaining_exposure * scale / fill_price
    by_industry = max(0.0, nav * profile["max_industry"] - industry_value) / fill_price
    by_cash = max(0.0, cash) / fill_price
    limits = {
        "risk": by_risk, "weight": by_weight, "exposure": by_exposure,
        "industry": by_industry, "cash": by_cash,
    }
    shares = min(limits.values())
    qty = int(shares / LOT_SIZE) * LOT_SIZE
    binding = [name for name, value in limits.items() if value <= shares + 1e-6]
    return max(qty, 0), {
        "risk_budget": round(risk_budget, 2), "loss_per_share": round(loss_per_share, 4),
        "target_amount": round(qty * fill_price, 2), "price": round(fill_price, 4),
        "effective_max_exposure_pct": round(effective_exposure * 100, 1),
        "pool_remaining_amount": round(pool_remaining, 2),
        "strategy_remaining_amount": round(strategy_remaining, 2),
        "pending_pool_amount": round(max(0.0, _num(pending_pool_amount)), 2),
        "pending_strategy_amount": round(max(0.0, _num(pending_strategy_amount)), 2),
        "new_exposure_scale_pct": round(scale * 100, 1),
        "constraint_shares": {name: round(value, 2) for name, value in limits.items()},
        "binding_constraints": binding,
    }


def _exceptional_opportunity(account, pick, quote, market, entry_model, q, risk_state, asof_day, conn):
    """共享资金池硬上限不允许任何例外。

    保留该函数仅为了让旧版审计字段和前端状态可读；所谓“特级机会”
    只能通过卖出低质量持仓释放额度，不能把总池上限从82%抬高。
    """
    return False, "总资金池82%硬上限不可突破；需先卖出低质量持仓释放额度"

def _buy_order(conn, account, signal, quote, market, news, asof_day, *, all_quotes=None):
    _assert_active_lease(conn, "strategy buy")
    # Pause/reset may happen while a scheduled scan is already in progress.
    # Re-read the account and cycle immediately before any execution decision.
    current_account = conn.execute("SELECT status,cycle_id FROM paper_accounts WHERE id=?", (account["id"],)).fetchone()
    current_cycle = _active_cycle(conn)
    if not current_account or current_account["status"] != "running" or current_account["cycle_id"] != current_cycle["id"]:
        return {
            "code": signal["code"], "status": "risk_rejected",
            "reason": "周期已暂停、重置或切换，本次在途订单已取消",
        }
    code = signal["code"]
    if _entry_freeze_enabled():
        freeze_reason = _entry_frozen_reason("自动候选")
        payload = _loads(signal.get("payload"), {})
        order_id, _created, _reason, freeze_payload = _record_entry_frozen_waitlist(
            conn, account["id"], code,
            name=signal.get("name"),
            qty=signal.get("qty") or 0,
            planned_price=_num(quote.get("price"), signal.get("close_price")),
            risk_payload={
                "signal": payload.get("decision"),
                "market": market,
                "quote": quote,
                "source_signal": payload,
            },
            signal_id=signal.get("id"),
            asof_day=asof_day,
            source="自动候选",
        )
        if str(signal.get("status") or "") in ENTRY_RETRY_SIGNAL_STATUSES:
            conn.execute(
                "UPDATE paper_signals SET status=?,reason=?,payload=? WHERE id=?",
                (
                    ENTRY_FROZEN_WAITLIST_STATUS, freeze_reason,
                    _json({**payload, "entry_freeze": freeze_payload.get("entry_freeze")}),
                    signal["id"],
                ),
            )
        if not _created:
            _risk_log(
                conn, account["id"], code, "buy", ENTRY_FROZEN_WAITLIST_STATUS,
                freeze_reason, freeze_payload,
            )
        return {
            "filled": False, "deferred": True, "waitlisted": True,
            "status": ENTRY_FROZEN_WAITLIST_STATUS, "order_id": order_id,
            "signal_id": signal.get("id"), "code": code, "qty": 0,
            "reason": freeze_reason,
        }
    payload = _loads(signal["payload"])
    pick = payload.get("pick") or {}
    market_policy = _strategy_market_policy(account, pick, quote, market)
    execution_quote = _execution_quote_status(quote, asof_day)
    signal_quote = payload.get("quote") or {}
    risk = {
        "signal": payload.get("decision"), "market": market,
        "market_policy": market_policy, "quote": quote,
        "signal_quote": {
            "quote_at": signal_quote.get("quote_at"),
            "quote_source": signal_quote.get("quote_source"),
            "price": signal_quote.get("price"),
            "pct": signal_quote.get("pct"),
        },
        "execution_quote": execution_quote,
        "execution_day": _date(asof_day).isoformat(),
        "q": None,
    }
    reasons = []
    security_scope = _security_scope(code, quote.get("name") or signal.get("name"), quote.get("risk_flag"))
    risk["security_scope"] = security_scope
    if not security_scope["allowed"]:
        reasons.append(security_scope["reason"])
    if not market_policy["allowed"]:
        reasons.append(market_policy["reason"])
    price = _num(quote.get("price"), 0)
    pct = _num(quote.get("pct"))
    lim = _limit_pct(code)
    if price <= 0:
        reasons.append("无有效实时价格")
    if not execution_quote["fresh"]:
        reasons.append(execution_quote["reason"])
    if pct <= -lim + 0.05:
        reasons.append("接近跌停，禁止买入")
    shadow_news = _negative_hits(news, code)
    dynamic_news = _dynamic_news_risk(news, code, market)
    if not dynamic_news["new_entry_allowed"]:
        reasons.append(dynamic_news["reason"])
    risk["shadow_news"] = {
        "warning_count": len(shadow_news),
        "notice": (
            "新闻/公告已纳入统一动态风控：核验负面阻止新增仓位，未核验负面仅降级额度"
            if shadow_news else None
        ),
    }
    risk["dynamic_news_risk"] = dynamic_news
    signal_close = _num(signal.get("close_price"), 0)
    gap = price / signal_close - 1 if signal_close > 0 and price > 0 else None
    spec = ACCOUNT_SPECS[account["id"]]
    if gap is None:
        reasons.append("缺少有效收盘价，无法计算跳空")
    elif not (spec["gap_q2"][0] <= gap <= spec["gap_q2"][1]):
        reasons.append(f"跳空 {gap*100:+.2f}% 超出 Q2 区间")
    kline = _completed_kline(code, asof_day, inclusive=False)
    latest_decision = DE.buy_decision(
        code, name=signal.get("name"), kline=kline, snap=quote,
        sector_flow=[], overseas_gate=market["overseas"], news_hits=[],
    )
    news_overlay = NL.code_overlay(code, asof=asof_day)
    delta = _entry_score_delta(account, asof_day) + _num(news_overlay.get("threshold_delta"))
    if dynamic_news["new_entry_allowed"] and dynamic_news["risk_scale"] < 1:
        delta += 0.05
    entry_model = _strategy_entry_assessment(account, pick, quote, kline, latest_decision, delta, market=market)
    entry_model["news_learning"] = news_overlay
    entry_model["dynamic_news_risk"] = dynamic_news
    risk["entry_model"] = entry_model
    # P3 双路径仓位档位：底部启动（mom20<0）结构未修复，首仓上限 60%
    # 档，确认后由加仓模型表达；多头回踩维持原满额档。
    if account["id"] == "trend_pullback":
        trend_path = str((pick or {}).get("trend_path") or "")
        if trend_path == "bottom_start":
            entry_model["position_scale"] = min(
                _num(entry_model.get("position_scale"), 1.0), 0.6)
            entry_model["trend_path"] = "bottom_start(底部启动小仓试仓)"
    if not entry_model["passed"]:
        reasons.extend(entry_model["reasons"])
    q = DE.next_day_auction_matrix(
        code, name=signal.get("name"), kline=kline, snap=quote,
        overseas_gate=market["overseas"], news_hits=[],
    )
    risk["q"] = q
    # 一致预期 EPS（P2）：仅三日策略，周级缓存的信息上下文。
    # 机构覆盖数<3 或解析失败为 None；不参与评分，供人工复核与后续建模。
    if account["id"] == NEW_STRATEGY_ID and AD is not None:
        try:
            risk["eps_consensus"] = AD.ths_eps_forecast(code, asof_day=asof_day)
        except Exception:
            risk["eps_consensus"] = None
    # Q3 仍不是可成交等级。短线日内做T仅把“个股强度、实时资金、
    # 板块确认”三者都很强的 Q3 记录成影子样本，以验证是否漏掉真正
    # 强势股；它不占现金、席位，也不会变成待成交订单。
    allowed_q = set(spec["allowed_q"])
    q3_shadow = False
    q3_shadow_detail = None
    if account["id"] == "tq_breakout" and q.get("tier") == "Q3":
        heat = pick.get("sector_heat") or {}
        q3_strength = bool(
            entry_model.get("passed")
            and 0.5 <= _num(quote.get("pct"), -99.0) <= 5.0
            and _num(quote.get("main_pct"), _num(quote.get("main_net_pct"))) >= 2.0
            and _num(quote.get("vol_ratio"), 0.0) >= 1.5
            and int(_num(heat.get("rank"), 999)) <= 10
            and _num(heat.get("pct"), 0.0) > 0
        )
        q3_shadow_detail = {
            "eligible": q3_strength,
            "tier": "Q3",
            "entry_score": _num(entry_model.get("score")),
            "threshold": _num(entry_model.get("threshold")),
            "individual_pct": _num(quote.get("pct")),
            "main_pct": _num(quote.get("main_pct"), _num(quote.get("main_net_pct"))),
            "vol_ratio": _num(quote.get("vol_ratio")),
            "sector_rank": int(_num(heat.get("rank"), 999)),
            "sector_pct": _num(heat.get("pct")),
            "policy": "仅影子验证，不生成模拟成交",
        }
        risk["q3_shadow"] = q3_shadow_detail
        if q3_strength:
            q3_shadow = True
        else:
            reasons.append("Q3 未同时满足个股强度、实时资金与板块确认，仅观察")
    elif q.get("tier") not in allowed_q:
        reasons.append(
            f"{spec['entry_model_name']}只允许 {'/'.join(sorted(allowed_q))}，当前为 {q.get('tier')}"
        )
    chase_entry = _chase_entry_gate(account, pick, quote, market, entry_model, q, execution_quote)
    risk["chase_entry"] = chase_entry
    # 三日策略的财报/均线形态由盘前完成日线预筛；盘中上涨过快时不能等
    # 到末段才把它当作新的突破。+3.5% 以上只接受 Q1、双源、资金和量能
    # 同步的早盘确认，否则保留为次轮观察，不模拟追入。
    timing_gate = {"allowed": True, "mode": "常规入场", "reason": "处于策略常规执行区间"}
    if account["id"] == NEW_STRATEGY_ID and _num(quote.get("pct"), -999.0) >= 3.5:
        timing_gate = {"allowed": False, "mode": "突破加速确认", "reason": None}
        if execution_quote.get("status") != "cross_source_checked":
            timing_gate["reason"] = "三日策略加速段必须通过双源实时行情核验"
        elif q.get("tier") != "Q1":
            timing_gate["reason"] = f"三日策略加速段仅允许 Q1，当前为 {q.get('tier')}"
        elif _num(quote.get("main_pct"), -999.0) < 1.0 or _num(quote.get("vol_ratio"), 0.0) < 1.2:
            timing_gate["reason"] = "三日策略加速段需主力净流入≥1%且量比≥1.2"
        else:
            timing_gate.update({"allowed": True, "reason": "财报突破在加速段获得双源、Q1、资金与量能确认"})
    risk["three_day_timing_gate"] = timing_gate
    if not timing_gate["allowed"]:
        reasons.append(f"{timing_gate['mode']}未通过：{timing_gate['reason']}")
    entry_price_allowed, entry_price_reason = _new_entry_price_gate(account, pick, quote)
    risk["entry_price_gate"] = {"allowed": entry_price_allowed, "reason": entry_price_reason}
    if not entry_price_allowed:
        reasons.append(entry_price_reason)
    # 短线日内做T允许追高，但不是无条件放行：达到追高区间后，必须明确写入
    # 专属门禁失败原因，避免与普通开仓或容量拒绝混在一起。
    if (
        (account["id"] == "tq_breakout" and _num(quote.get("pct"), -999.0) >= 3.5)
        or chase_entry.get("required")
    ) and not chase_entry["allowed"]:
        prefix = "热点加速试仓" if chase_entry.get("required") else "短线追高风控"
        reasons.append(f"{prefix}未通过：{chase_entry.get('reason') or '确认条件不足'}")
    all_codes = [p["code"] for p in _position_rows(conn, asof_day=asof_day)] + [code]
    if all_quotes is None:
        all_quotes = {} if conn.in_transaction else _quotes(all_codes, asof_date=asof_day)
    else:
        all_quotes = dict(all_quotes)
    positions, position_value, nav, industries, code_values = _shared_account_exposure(conn, all_quotes, asof_day)
    shared_cash = _shared_cash(conn)
    open_codes = {
        item["code"] for item in positions
        if item.get("account_id") == account["id"] and int(_num(item.get("qty"))) >= LOT_SIZE
    }
    pending_slots = _pending_position_slots(conn, positions)
    committed_open_codes = open_codes | {
        pending_code for pending_account, pending_code in pending_slots
        if pending_account == account["id"]
    }
    timing_block_reasons = []
    if code in open_codes:
        reasons.append(
            "本策略已持有该股票，不重复执行普通开仓；其他策略仍可按各自模型独立建仓，"
            "本策略仅由专属加仓/做T模型复核"
        )
    elif ET is not None:
        # 入场时机状态机（P3）：观察→首次触发→连续确认→入场/失效。
        # 只管新开仓；已持有（加仓/做T）走原有专属模型链路。状态机只
        # 决定"本轮是否允许进入执行闸门"，追高上限/Q级/资金等门禁照常。
        # 时机未确认属"未到时机"而非"永不合适"：归入软延迟进替补队列
        # 复试。硬拒绝会让轮换回补（卖了买不回）和确认期候选被永久
        # 终结——2026-08 审计确认的 P1 缺陷。
        micro = entry_model.get("microstructure") or {}
        timing_allowed, timing_info = ET.evaluate(
            account["id"], code, price, _num(quote.get("pct")),
            fast=bool(quote.get("_fast_entry_monitor")),
            evidence={
                "cross_source_checked": execution_quote.get("status") == "cross_source_checked",
                "main_pct": _num(quote.get("main_pct"), _num(quote.get("main_net_pct"))),
                "vol_ratio": _num(quote.get("vol_ratio")),
                "active_buy_sell_imbalance": micro.get("active_buy_sell_imbalance"),
                "depth_imbalance": micro.get("depth_imbalance"),
            },
        )
        risk["entry_timing"] = timing_info
        if not timing_allowed:
            timing_block_reasons.append(
                f"入场时机：{timing_info.get('reason') or timing_info.get('state')}")
            reasons.extend(timing_block_reasons)
    count_budget = _dynamic_position_limits(conn)
    position_limit = max(1, int(count_budget["limits"].get(account["id"], spec["max_positions"])))
    pool_open_positions = {
        (str(item.get("account_id")), str(item.get("code"))) for item in positions
        if int(_num(item.get("qty"))) >= LOT_SIZE
    } | pending_slots
    # 主力独立席位保障（2026-08-31 复核 P2）：主力空仓时，其他策略不得
    # 占用共享池最后一个空席——否则满席后已通过的主力候选会饿死。
    # P1 死锁修复（2026-09-03）：预留仅在 ①主力当日确有在途候选（排队/
    # 等待复核的非终态信号）且 ②未到 14:30 放行时限时生效；主力全天无
    # 候选或临近收盘仍未建仓时，最后一席交还其他策略，避免"主力等确认、
    # 其他策略等席位"的双向空等。查询异常时维持原预留行为（fail-closed）。
    mf_seat_interest = 0
    mf_seat_reserved = (
        account["id"] != MAIN_FORCE_STRATEGY_ID
        and not any(key[0] == MAIN_FORCE_STRATEGY_ID for key in pool_open_positions)
        and len(pool_open_positions) >= count_budget["pool_limit"] - 1
    )
    if mf_seat_reserved:
        try:
            mf_seat_interest = int(conn.execute(
                "SELECT COUNT(*) FROM paper_signals "
                "WHERE account_id=? AND intended_date=? AND status IN (?,?,?)",
                (MAIN_FORCE_STRATEGY_ID, str(asof_day)[:10],
                 *ENTRY_RETRY_SIGNAL_STATUSES),
            ).fetchone()[0] or 0)
        except Exception:
            mf_seat_interest = 1
        _mf_now = _now()
        _mf_day = str(asof_day)[:10]
        _mf_deadline = f"{_mf_day} 14:30:00" if _mf_day == _mf_now[:10] else None
        mf_seat_reserved = mf_seat_interest > 0 and (
            _mf_deadline is None or _mf_now < _mf_deadline)
    risk["position_count_gate"] = {
        "current": len(open_codes), "committed": len(committed_open_codes), "limit": position_limit,
        "pool_current": len(pool_open_positions), "pool_limit": count_budget["pool_limit"],
        "dynamic": True, "source": count_budget["source"],
        "allocation_version": count_budget["allocation_version"],
        "main_force_seat_reserved": mf_seat_reserved,
        "main_force_seat_interest": mf_seat_interest,
        "is_existing_position": code in open_codes,
        "scope": "按策略账户计数；同一股票可由其他策略独立持有和交易",
    }
    risk["capacity_policy"] = {
        "version": ENTRY_CAPACITY_POLICY,
        "mode": "count_primary_amount_soft",
        "amount_constraints": "sizing_only",
        "candidate_veto": "position_count_or_hard_safety_gate_only",
        "whole_lot_shortfall": "retryable_waitlist",
        "risk_exits_affected": False,
    }
    entry_deployment = _entry_deployment_gate(
        conn, account["id"], code, positions, pending_slots, count_budget, asof_day,
    )
    risk["entry_deployment_gate"] = entry_deployment
    pacing_blocked = code not in committed_open_codes and not entry_deployment["allowed"]
    if pacing_blocked:
        reasons.append(entry_deployment["reason"])
    strategy_count_blocked = code not in committed_open_codes and len(committed_open_codes) >= position_limit
    pool_count_blocked = (
        (account["id"], code) not in pool_open_positions
        and len(pool_open_positions) >= count_budget["pool_limit"]
    )
    # A borrowed seat is a late-session quality allocation tool.  It must not
    # let a strong opening candidate bypass the staged deployment budget.
    if strategy_count_blocked and not pacing_blocked:
        upgrade = _slot_upgrade_context(conn, account["id"], signal, positions, asof_day)
        risk["slot_upgrade"] = upgrade
        if upgrade.get("eligible"):
            borrowed = _apply_slot_borrow(conn, account["id"], upgrade, asof_day)
            risk["slot_borrow"] = borrowed
            if borrowed.get("allowed"):
                # Re-read the same allocation version after the atomic transfer
                # so sizing and the audit gate use the borrowed slot immediately.
                count_budget = _dynamic_position_limits(conn)
                position_limit = max(1, int(count_budget["limits"].get(account["id"], position_limit)))
                strategy_count_blocked = len(committed_open_codes) >= position_limit
                risk["position_count_gate"]["limit"] = position_limit
                risk["position_count_gate"]["allocation_version"] = count_budget["allocation_version"]
                if strategy_count_blocked:
                    reasons.append(
                        f"策略持仓及待成交席位已达动态上限 {len(committed_open_codes)}/{position_limit}；"
                        "借位后仍无可用席位"
                    )
            else:
                reasons.append(
                    f"策略持仓及待成交席位已达动态上限 {len(committed_open_codes)}/{position_limit}；"
                    f"{upgrade['reason']}；{borrowed.get('reason')}"
                )
        else:
            reasons.append(
                f"策略持仓及待成交席位已达动态上限 {len(committed_open_codes)}/{position_limit}；"
                f"{upgrade['reason']}"
            )
    if pool_count_blocked:
        reasons.append(
            f"总持仓及待成交席位已达共享硬上限 {len(pool_open_positions)}/{count_budget['pool_limit']}"
        )
    elif mf_seat_reserved:
        reasons.append(
            "共享池仅剩最后 1 席：为主力策略独立席位预留，"
            "待主力建仓或池内席位释放后恢复其他策略买入"
        )
    strategy_budget = _strategy_pool_budget(conn, account, nav, positions, all_quotes, market=market)
    risk["strategy_budget"] = strategy_budget
    risk_state = _shared_risk_state(conn, account, nav, asof_day)
    risk["account_risk"] = risk_state
    if risk_state["blocked"]:
        reasons.extend(risk_state["reasons"])
    profile = _risk_profile(account)
    code_value = code_values.get(code, 0.0)
    industry_value = industries.get(signal.get("industry") or "未知", 0.0)
    fill_price = price * (1 + SLIPPAGE)
    if signal_close > 0 and lim > 0:
        # P3 审计修复：涨停价封顶对所有买入生效，不只追买分支。
        # 旧逻辑非 chase 候选在封板价位会模拟出 price×1.001 高于交易所
        # 涨停价的成交——现实中该价位根本买不到。
        upper_limit = signal_close * (1 + lim / 100)
        fill_price = min(fill_price, round(upper_limit, 2))
    # The strategy budget has already applied the current market light to its
    # remaining amount.  Only independent model/chase/news adjustments belong
    # here; _entry_execution_scale restores a market multiplier only for
    # callers that did not receive a market-scaled strategy budget.
    risk_scale = _entry_execution_scale(
        market_policy, entry_model, chase_entry, dynamic_news, strategy_budget,
    )
    # 黄灯系数已经写入策略预算，不能在剩余仓位上重复乘一次；否则当前持仓
    # 会被 65% x 65% 这种双重缩放错误压成一手。
    # Position exposure is a shared-pool hard stop plus a fair strategy budget;
    # it is no longer calculated as 82% of a separate 300,000-yuan bucket for
    # every strategy.  Per-strategy models still control single-name risk,
    # industry concentration, stop distance and entry quality below.
    exposure_cap = SHARED_POOL_MAX_EXPOSURE
    qty, sizing = _price_aware_qty(
        nav, shared_cash, position_value, industry_value, code_value,
        fill_price, ACCOUNT_SPECS[account["id"]]["hard_stop"], profile,
        exposure_cap=exposure_cap, max_exposure_cap=exposure_cap, exposure_scale=risk_scale,
        strategy_position_value=strategy_budget["current_amount"],
        strategy_cap_amount=strategy_budget["absolute_cap_amount"],
        pool_cap_amount=strategy_budget["pool_cap_amount"],
        pending_strategy_amount=strategy_budget.get("pending_reserve_amount", 0.0),
        pending_pool_amount=strategy_budget.get("pending_pool_reserve_amount", 0.0),
    )
    # Entry-model scaling must reduce the actual order as well as the residual
    # exposure allowance.  Otherwise a risk/weight constraint could silently
    # turn a trend "trial" into a full first position.
    entry_tranche_scale = max(0.10, min(_num(entry_model.get("entry_tranche_scale"), 1.0), 1.0))
    recovery_watch = payload.get("recovery_watch") or {}
    if recovery_watch.get("passed"):
        # A protective exit may re-enter only as a small probe.  It still
        # traverses every ordinary quote, security, Q-tier and cash gate.
        entry_tranche_scale = min(entry_tranche_scale, _num(recovery_watch.get("probe_ratio"), 0.25))
        risk["recovery_probe"] = {"enabled": True, "ratio": entry_tranche_scale,
                                   "exit_class": recovery_watch.get("exit_class")}
    if entry_tranche_scale < 1.0 and qty >= LOT_SIZE:
        qty = int((qty * entry_tranche_scale) / LOT_SIZE) * LOT_SIZE
        sizing["entry_tranche_scale_pct"] = round(entry_tranche_scale * 100, 1)
        sizing["entry_tranche_qty"] = int(qty)
        # P3 审计修复（P2）：同步缩放后的目标金额——dust_order 地板
        # （MIN_ORDER_AMOUNT）读取本字段，不同步会让 ¥30000 预算缩到
        # ¥7500 的探针仓绕过地板直接成交。
        sizing["target_amount"] = round(qty * fill_price, 2)
    else:
        sizing["entry_tranche_scale_pct"] = 100.0
    if (
        account["id"] == MAIN_FORCE_STRATEGY_ID
        and (account["id"], code) not in committed_open_codes
        and qty >= LOT_SIZE
    ):
        # 主力首仓纪律（2026-08-31 复核 P1）：首笔不超过共享净值 12%，
        # 资金持续、价格站稳后再由既有加仓路径表达；避免一笔 6.7 万
        # 吃掉"精挑三只"的大部分预算，也不退回两三千元的无效小仓。
        first_cap = nav * 0.12
        capped_qty = int(min(qty * fill_price, first_cap) / fill_price / LOT_SIZE) * LOT_SIZE
        if capped_qty < qty:
            sizing["main_force_first_tranche"] = {
                "cap_pct": 12.0, "cap_amount": round(first_cap, 2),
                "qty_before": int(qty), "qty_after": int(capped_qty),
                "note": "资金持续且价格站稳后允许后续加仓",
            }
            qty = max(capped_qty, 0)
            sizing["target_amount"] = round(qty * fill_price, 2)
            sizing["entry_tranche_qty"] = int(qty)
    sizing["strategy_target_pct"] = strategy_budget["target_pct"]
    sizing["strategy_base_target_pct"] = strategy_budget.get("base_target_pct")
    sizing["strategy_floor_pct"] = strategy_budget["floor_pct"]
    sizing["strategy_current_pct"] = strategy_budget["current_pct"]
    sizing["strategy_market_scale_pct"] = strategy_budget.get("market_scale_pct")
    sizing["strategy_redistribution_pct"] = round(strategy_budget["redistribution_amount"] / max(nav, 1) * 100, 2)
    sizing["capacity_policy"] = ENTRY_CAPACITY_POLICY
    sizing["amount_constraints_soft"] = True
    exceptional = {"approved": False, "reason": None}
    soft_amount_reasons = []
    # 入场时机未确认 = 未到时机，进替补队列复试而非终态拒绝。
    soft_amount_reasons.extend(timing_block_reasons)
    capacity_constraints = set(sizing.get("binding_constraints") or [])
    # 特级机会仅保留审计字段；总池硬上限、现金、单股、行业和止损预算
    # 均不可被所谓“机会”放宽，必须先卖出释放额度。
    if qty < LOT_SIZE and capacity_constraints == {"exposure"} and not reasons:
        approved_exception, exception_reason = _exceptional_opportunity(
            account, pick, quote, market, entry_model, q, risk_state, asof_day, conn
        )
        exceptional.update({"approved": approved_exception, "reason": exception_reason})
        # The opportunity path is intentionally audit-only.  It can never
        # re-size an order beyond the single shared 82% hard ceiling.
    sizing["market_risk_scale"] = _num(market_policy.get("risk_scale"), 1.0)
    sizing["execution_adjustment_scale"] = risk_scale
    sizing["market_scale_applied_in_budget"] = bool(strategy_budget.get("market_scale_applied"))
    sizing["entry_execution_mode"] = entry_model.get("execution_mode", "常规入场")
    risk["exceptional_opportunity"] = exceptional
    # 市场黄灯只收紧账户总仓位上限，不再把每笔数量二次打折。
    # 每笔委托仍受单票权重和单笔止损预算限制，可避免小账户因100股取整而长期空仓。
    risk["sizing"] = sizing
    amount = qty * fill_price
    fees = _commission(amount) if amount else 0.0
    sizing["one_lot_amount"] = round(LOT_SIZE * fill_price, 2)
    sizing["one_lot_fee"] = round(_commission(LOT_SIZE * fill_price), 2)
    sizing["cash_available"] = round(shared_cash, 2)
    sizing["qty_final"] = int(qty)
    if qty < LOT_SIZE and not reasons:
        limits = sizing.get("constraint_shares") or {}
        binding = [name for name, value in limits.items() if _num(value) < LOT_SIZE]
        if not binding:
            binding = list(sizing.get("binding_constraints") or [])
        labels = {
            "risk": "单笔止损预算", "weight": "单票仓位上限",
            "exposure": "策略总仓位上限", "industry": "行业仓位上限",
            "cash": "可用现金",
        }
        root = labels.get(binding[0], "风险约束") if binding else "风险约束"
        root_limit = _num(limits.get(binding[0]), 0) if binding else 0
        # P3 审计修复（P2）：binding 为 risk 时是结构性不可满足——高价股
        # ×紧止损使 by_risk 永远不足一手，转等待池只会每天空转占位到
        # 24h 归档。预算侧约束（cash/weight/exposure/industry）才可等待。
        if set(binding) == {"risk"}:
            reasons.append(
                f"单笔止损预算结构性不足：一手约 ¥{LOT_SIZE * fill_price:,.2f}，"
                f"风险预算最多 {root_limit:.0f} 股，转终态拒绝"
            )
        else:
            reasons.append(
                f"一手约 ¥{LOT_SIZE * fill_price:,.2f}，模型可买 {int(qty)} 股；金额约束仅影响下单规模，"
                f"当前转入等待池（参考限制：{root}，最多 {root_limit:.0f} 股）"
            )
            soft_amount_reasons.append(reasons[-1])
    # 2026-09-03 修复：尘埃单此前仅用于"已拦截订单"的分类，金额低于
    # MIN_ORDER_AMOUNT 且其余闸门全过时 allowed=True 照样成交（当日
    # 7 笔 <8000 成交实证）。现作为软性拦截：候选转入 deferred 队列，
    # 预算释放后按全尺寸复核，不再用小仓占用稀缺席位。
    if qty >= LOT_SIZE and amount < MIN_ORDER_AMOUNT and not reasons:
        dust_reason = (
            f"低于最小建仓金额 \u00a5{MIN_ORDER_AMOUNT:,.0f}（当前 \u00a5{amount:,.0f}）；"
            "席位稀缺时不建尘埃仓，候选保留在等待池，预算释放后按全尺寸复核"
        )
        reasons.append(dust_reason)
        soft_amount_reasons.append(dust_reason)
    _, pending_cash = _pending_buy_reservations(conn)
    cash_after_pending = shared_cash - pending_cash
    # Zero cash is a real safety veto.  A positive balance that is merely
    # smaller than the order stays a soft sizing shortfall and remains
    # retryable: the audit (2026-08) found this reason was hard-classified,
    # permanently rejecting candidates that only needed the queue to drain.
    if cash_after_pending <= 1e-6:
        reasons.append(
            f"共享资金池暂无可用现金（已有待成交买单预占 ¥{pending_cash:,.2f}）"
            if pending_cash > 0 else "共享资金池暂无可用现金"
        )
    elif amount + fees > cash_after_pending + 1e-6:
        cash_reason = (
            f"共享资金池可用现金不足（已有待成交买单预占 ¥{pending_cash:,.2f}）"
            if pending_cash > 0 else "共享资金池可用现金不足"
        )
        reasons.append(cash_reason)
        soft_amount_reasons.append(cash_reason)
    if amount and fees / amount > 0.003:
        cost_reason = "单笔最低佣金占比过高，暂不下单，候选保留在等待池复核"
        reasons.append(cost_reason)
        soft_amount_reasons.append(cost_reason)
    if account["id"] == "tq_breakout" and amount > 0:
        expected_edge = max(TQ_MIN_EXPECTED_EDGE_PCT, _num(profile.get("min_cost_edge"), 0.0))
        # 用实际买入佣金 + 预估卖出佣金/印花税 + 双向滑点做保守成本门槛；
        # 不把未实现收益当现金，只判断该委托是否值得发生。
        expected_profit = amount * expected_edge
        estimated_round_trip_cost = (
            fees + _commission(amount) + amount * STAMP_SELL + amount * SLIPPAGE * 2
        )
        sizing["minimum_effective_order_amount"] = TQ_MIN_EFFECTIVE_ENTRY_AMOUNT
        sizing["expected_edge_pct"] = round(expected_edge * 100, 2)
        sizing["expected_profit_amount"] = round(expected_profit, 2)
        sizing["estimated_round_trip_cost"] = round(estimated_round_trip_cost, 2)
        if amount < TQ_MIN_EFFECTIVE_ENTRY_AMOUNT:
            min_amount_reason = (
                f"短线有效委托至少 ¥{TQ_MIN_EFFECTIVE_ENTRY_AMOUNT:,.0f}，当前 ¥{amount:,.0f}；"
                "金额约束仅用于委托 sizing，候选保留在等待池复核"
            )
            reasons.append(min_amount_reason)
            soft_amount_reasons.append(min_amount_reason)
        elif expected_profit < estimated_round_trip_cost * 1.25:
            edge_reason = (
                f"短线预期毛收益 ¥{expected_profit:,.2f} 未覆盖估算往返成本 "
                f"¥{estimated_round_trip_cost:,.2f}，候选保留在等待池复核"
            )
            reasons.append(edge_reason)
            soft_amount_reasons.append(edge_reason)
    # Amount/cost constraints are intentionally soft.  A real cash shortage,
    # stale/invalid quote, T+1, limit-down, hard risk, or policy rejection is
    # still a hard veto and must remain risk_rejected rather than waitlisted.
    hard_reasons = [reason for reason in reasons if reason not in soft_amount_reasons]
    count_only_blocked = bool(hard_reasons) and all(
        str(item).startswith((
            "策略持仓及待成交席位已达动态上限",
            "总持仓及待成交席位已达共享硬上限",
            "开盘确认期：", "早盘确认期：", "上午/午间复核期：", "午后扩展期：", "14:30 后不新增普通自动仓",
        ))
        for item in hard_reasons
    )
    # 新开自动仓只要占用一席，就必须有足够的资金意义。止损后的回补
    # 也不是例外：一手探针若低于地板，保留观察即可，不能靠“探针”
    # 名义绕过尘埃仓保护并挤占策略席位。
    dust_order = (
        qty >= LOT_SIZE
        and amount < MIN_ORDER_AMOUNT
        and not hard_reasons
    )
    # 2026-09-03 修复：入场时机确认中的候选不允许被打成终态拒绝。
    # 时机软阻断常与"主力席位预留"等硬拒绝写在同一单，旧逻辑会把确认中
    # 的候选直接 risk_rejected，而两条复试通道（30 秒快速通道、3 分钟
    # recheck）都不捞 rejected，确认计数永远停在 1/2（线上实证
    # trend_pullback 21/21、tq_breakout 12/12 卡死）。确认中的候选反正
    # 买不进，归入 deferred_capacity 留在复试队列；每次复试都会重跑全部
    # 闸门（含硬拒绝），不会产生任何越权成交。
    capacity_deferred = bool(timing_block_reasons) or count_only_blocked or (
        bool(soft_amount_reasons) and not hard_reasons
    ) or dust_order or (
        qty < LOT_SIZE and not hard_reasons
        and set(sizing.get("binding_constraints") or []).issubset({"cash", "weight", "exposure", "industry"})
    )
    # A Q3 sample is worth recording only if every ordinary execution/risk
    # gate also passed.  It must never turn stale quotes, a hard veto or an
    # undersized order into a seemingly valid research observation.
    q3_shadow_ready = q3_shadow and not reasons
    allowed = not reasons and not q3_shadow_ready
    order_status = "filled" if allowed else (
        "shadow_q3" if q3_shadow_ready else ("deferred_capacity" if capacity_deferred else "risk_rejected")
    )
    reason = (
        "Q3 三重强度影子候选：个股强度、实时资金和板块确认均通过；仅记录验证，不模拟买入"
        if q3_shadow_ready else "；".join(reasons) if reasons else (
        f"{q.get('tier')} 通过；按价格与风险预算计算 {qty} 股"
        + (f"；{chase_entry['reason']}" if chase_entry["allowed"] else "")
    ))
    decision_name = "approved" if allowed else (
        "q3_shadow_candidate" if q3_shadow_ready else ("deferred_capacity" if capacity_deferred else "rejected")
    )
    risk = _with_decision_snapshot(
        risk, account_id=account["id"], code=code, side="buy",
        decision=decision_name, reason=reason, asof_date=asof_day,
        quote=quote, kline=kline, news=news,
        final_score=entry_model.get("score"),
    )
    # An execution retry is an attempt for this signal, not a new candidate.
    # Retire the old retry before inserting the next canonical attempt so it
    # cannot reserve a second slot or cash on the following scan.
    _supersede_signal_execution_retries(conn, signal.get("id"))
    _assert_active_lease(conn, "strategy buy order write")
    cursor = conn.execute(
        """INSERT INTO paper_orders(account_id,signal_id,side,code,name,qty,planned_price,filled_price,amount,fees,status,reason,risk_payload,created_at,executed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account["id"], signal["id"], "buy", code, signal.get("name"), qty, price, fill_price if allowed else None,
         amount if allowed else None, fees if allowed else None, order_status, reason, _json(risk), _now(), _now() if allowed else None),
    )
    # A frozen order is a waitlist marker, not a second live order.  Once the
    # data gate reopens and this candidate receives a fresh decision, retire
    # its old marker so the terminal cannot keep showing "待执行" after the
    # newer fill/defer/reject has already been recorded.
    # Keep the signal lifecycle in lockstep with its order lifecycle.  The
    # previous statement retired old deferred/waitlist orders but left their
    # signal rows pending, so the UI and the recheck worker could resurrect a
    # signal whose only order had already been superseded.
    conn.execute(
        """UPDATE paper_signals
           SET status='superseded',
               reason=COALESCE(reason,'') || '；已由最新实时复核委托替代'
           WHERE id IN (
               SELECT signal_id FROM paper_orders
               WHERE account_id=? AND code=? AND side='buy'
                 AND status IN (?,?) AND id<>? AND signal_id IS NOT NULL
           )
             AND status IN (?,?,?)""",
        (
            account["id"], code, ENTRY_FROZEN_WAITLIST_STATUS, "deferred_capacity",
            int(cursor.lastrowid), *ENTRY_RETRY_SIGNAL_STATUSES,
        ),
    )
    conn.execute(
        """UPDATE paper_orders
           SET status='superseded',
               reason=COALESCE(reason,'') || '；已由最新实时复核委托替代，见 #' || ?
           WHERE account_id=? AND code=? AND side='buy'
             AND status IN (?,?) AND id<>?""",
        (
            int(cursor.lastrowid), account["id"], code,
            ENTRY_FROZEN_WAITLIST_STATUS, "deferred_capacity", int(cursor.lastrowid),
        ),
    )
    _risk_log(conn, account["id"], code, "buy", decision_name, reason, risk)
    if not allowed:
        if (risk.get("slot_borrow") or {}).get("allowed"):
            risk["slot_borrow_rollback"] = _rollback_slot_borrow(conn, risk["slot_borrow"])
        if q3_shadow_ready:
            conn.execute(
                "UPDATE paper_signals SET status='shadow_q3', reason=?, payload=? WHERE id=?",
                (reason, _json({**payload, "q3_shadow": q3_shadow_detail}), signal["id"]),
            )
            _audit(conn, account["id"], "q3_shadow_candidate", f"{code}：{reason}")
            return {
                "filled": False, "shadow": True, "status": "shadow_q3",
                "code": code, "qty": 0, "reason": reason,
            }
        if capacity_deferred:
            if pacing_blocked:
                deferred_reason = entry_deployment["reason"]
            elif dust_order:
                deferred_reason = (
                    f"策略预算剩余仅支持 {_num(sizing.get('target_amount')):.0f} 元新仓，"
                    f"低于最小建仓金额 {MIN_ORDER_AMOUNT:.0f} 元；席位稀缺时不建尘埃仓，"
                    "保留为替补候选，待轮换释放预算后按全尺寸复核"
                )
            elif timing_block_reasons:
                deferred_reason = (
                    f"入场时机确认中：{timing_info.get('reason') or timing_info.get('state')}；"
                    "候选保留在等待池，连续确认完成后自动复核"
                )
            elif count_only_blocked:
                deferred_reason = (
                    (risk.get("slot_upgrade") or {}).get("reason")
                    or "持仓数量配额已满，保留为高分替补候选；释放动态席位后重新复核"
                )
            else:
                deferred_reason = "金额/成本约束仅影响本次下单规模，候选保留在等待池；资金、席位或预算释放后按最新行情重新复核"
            conn.execute("UPDATE paper_signals SET status='deferred_capacity', reason=? WHERE id=?", (deferred_reason, signal["id"]))
            return {"filled": False, "deferred": True, "reason": deferred_reason}
        conn.execute("UPDATE paper_signals SET status='rejected', reason=? WHERE id=?", (reason, signal["id"]))
        return {"filled": False, "reason": reason}
    order_id = int(cursor.lastrowid)
    savepoint = f"strategy_fill_{order_id}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _assert_active_lease(conn, "strategy fill reservation")
        reserved, reserve_reason = _reserve_shared_capital(
            conn, order_id, account["id"], code, amount, fees,
        )
        if not reserved:
            raise RuntimeError(reserve_reason or "共享资金池预占失败")
        _assert_active_lease(conn, "strategy fill cash debit")
        _debit_shared_cash(conn, amount + fees, preferred_account_id=account["id"])
        _finish_capital_reservation(conn, order_id, "consumed")
        _assert_active_lease(conn, "strategy fill lot")
        _record_lot(conn, account, signal, qty, fill_price, asof_day, order_id, is_t_base=True, fees=fees)
        if ET is not None:
            ET.mark_entered(account["id"], code, fill_price)
        conn.execute("INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (order_id, account["id"], "buy", code, qty, fill_price, amount, fees, asof_day.isoformat(), quote.get("quote_at"), "实时价 + 0.10% 滑点"))
        _assert_active_lease(conn, "strategy fill finalization")
        conn.execute("UPDATE paper_signals SET status='filled', reason=? WHERE id=?", (reason, signal["id"]))
        _sync_positions(conn, account["id"], asof_day)
        conn.execute(
            "UPDATE paper_orders SET status='filled',filled_price=?,amount=?,fees=?,executed_at=? WHERE id=?",
            (fill_price, amount, fees, _now(), order_id),
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if _lease_lost(exc):
            raise
        _finish_capital_reservation(conn, order_id, "released")
        if (risk.get("slot_borrow") or {}).get("allowed"):
            risk["slot_borrow_rollback"] = _rollback_slot_borrow(conn, risk["slot_borrow"])
        failure = f"策略买入执行失败，可重试：{type(exc).__name__}: {exc}"
        conn.execute(
            "UPDATE paper_orders SET status=?,reason=?,filled_price=NULL,amount=NULL,fees=NULL,executed_at=NULL WHERE id=?",
            (STRATEGY_EXECUTION_RETRY_STATUS, failure, order_id),
        )
        conn.execute("UPDATE paper_signals SET status='pending',reason=? WHERE id=?", (failure, signal["id"]))
        _risk_log(conn, account["id"], code, "buy", STRATEGY_EXECUTION_RETRY_STATUS, failure, risk)
        return {"filled": False, "retryable": True, "status": STRATEGY_EXECUTION_RETRY_STATUS, "reason": failure}
    if exceptional.get("approved"):
        cycle = _active_cycle(conn)
        _observe_intraday(
            conn, cycle["id"], account["id"], code, fill_price, "exceptional_entry",
            exceptional["reason"], {"signal_id": signal["id"], "sizing": sizing, "risk": risk},
        )
    _audit(conn, account["id"], "buy_filled", f"{code} {qty}股 @ {fill_price:.2f}")
    return {"filled": True, "code": code, "qty": qty, "price": round(fill_price, 2)}


def _manual_risk_state(conn, account, nav, asof_day):
    profile = _risk_profile(account)
    day = _date(asof_day).isoformat()
    start_nav = _num(account.get("daily_start_nav"), nav)
    navs = [r[0] for r in conn.execute(
        "SELECT nav FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account["id"],)
    ).fetchall()]
    peak = max(navs + [nav, _account_reference_capital(account) or nav])
    daily_loss = 1 - nav / start_nav if start_nav else 0.0
    drawdown = 1 - nav / peak if peak else 0.0
    reasons = []
    cooldown = account.get("cooldown_until")
    if cooldown and str(cooldown) >= day:
        reasons.append(f"冷静期至 {cooldown}")
    if daily_loss >= profile["daily_loss"]:
        reasons.append(f"单日亏损 {daily_loss*100:.2f}% 已触发熔断")
    if drawdown >= profile["drawdown"]:
        reasons.append(f"滚动回撤 {drawdown*100:.2f}% 已触发熔断")
    return {
        "blocked": bool(reasons), "reasons": reasons,
        "daily_loss_pct": round(daily_loss * 100, 2),
        "drawdown_pct": round(drawdown * 100, 2),
        "cooldown_until": cooldown,
    }


def _manual_order_plan(
    conn, account_id, code, side, qty=0, order_type="market",
    limit_price=None, asof_day=None, quote=None, exclude_reservation_key=None,
    all_quotes=None, live_universe=None, market_context=None,
):
    day = _date(asof_day)
    code = str(code or "").strip()
    side = str(side or "").lower()
    order_type = str(order_type or "market").lower()
    requested_qty = int(_num(qty))
    reasons = []
    entry_frozen = side == "buy" and _entry_freeze_enabled()
    if entry_frozen:
        reasons.append(_entry_frozen_reason("手动委托"))
    if side not in {"buy", "sell"}:
        reasons.append("方向必须为买入或卖出")
    if order_type not in {"market", "limit"}:
        reasons.append("委托类型必须为市价或限价")
    if len(code) != 6 or not code.isdigit():
        reasons.append("请输入六位证券代码")
    account_row = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
    account = dict(account_row) if account_row else None
    if not account:
        reasons.append("未找到策略账户")
    elif account["status"] != "running":
        reasons.append("策略账户未运行，请先启动或恢复当前周期")

    if quote is None:
        # Callers that already prefetched market evidence pass ``quote``.
        # When a plan is evaluated inside a write transaction, use only the
        # persisted local snapshot; network I/O here can otherwise hold the
        # ledger lock while a provider retries.
        local_quote = dict(
            ({} if conn.in_transaction else _quotes([code], asof_date=day)).get(code)
            or {}
        )
    else:
        local_quote = dict(quote)
    snapshot_at = local_quote.get("quote_at") or _universe_snapshot_time()
    local_quote["quote_at"] = snapshot_at
    local_quote["quote_source"] = local_quote.get("quote_source") or "local_cache"
    price = _num(local_quote.get("price"), 0)
    if price <= 0:
        reasons.append("缺少有效行情快照")
    if not snapshot_at:
        reasons.append("缺少行情快照时间")
    elif str(snapshot_at)[:10] != day.isoformat():
        reasons.append(f"行情快照停留在 {str(snapshot_at)[:10]}，禁止模拟成交")
    if order_type == "limit" and _num(limit_price) <= 0:
        reasons.append("限价委托必须填写有效价格")

    plan = {
        "account_id": account_id, "account_name": account.get("name") if account else None,
        "code": code, "name": local_quote.get("name") or code,
        "industry": local_quote.get("industry") or "未知", "side": side,
        "order_type": order_type, "limit_price": _num(limit_price) if limit_price is not None else None,
        "quote": local_quote, "quote_price": price, "quote_at": snapshot_at,
        "requested_qty": requested_qty, "recommended_qty": 0, "qty": requested_qty,
        "allowed": False, "triggered": order_type == "market",
        "risk": {"entry_freeze": {
            "enabled": True, "env": ENTRY_FREEZE_ENV,
            "status": ENTRY_FROZEN_WAITLIST_STATUS,
        }} if entry_frozen else {},
        "reasons": reasons, "entry_frozen": entry_frozen,
    }
    if not account or price <= 0 or side not in {"buy", "sell"}:
        if entry_frozen:
            plan["status"] = ENTRY_FROZEN_WAITLIST_STATUS
            plan["waitlisted"] = True
        return plan

    strategy_positions = _position_rows(conn, account_id, day)
    position = next((p for p in strategy_positions if p["code"] == code), None)
    positions = _position_rows(conn, asof_day=day)
    open_codes = {
        item["code"] for item in positions
        if item.get("account_id") == account_id and int(_num(item.get("qty"))) >= LOT_SIZE
    }
    pending_slots = _pending_position_slots(
        conn, positions, exclude_order_key=exclude_reservation_key,
    )
    committed_open_codes = open_codes | {
        pending_code for pending_account, pending_code in pending_slots
        if pending_account == account_id
    }
    count_budget = _dynamic_position_limits(conn)
    position_limit = max(
        1,
        int(count_budget["limits"].get(account_id, (ACCOUNT_SPECS.get(account_id) or {}).get("max_positions", 5))),
    )
    pool_open_positions = {
        (str(item.get("account_id")), str(item.get("code"))) for item in positions
        if int(_num(item.get("qty"))) >= LOT_SIZE
    } | pending_slots
    all_codes = sorted({p["code"] for p in positions} | {code})
    # 仓位总额也必须使用同一轮实时快照；仅读历史 universe 会把已持仓
    # 按成本或前收估值，造成共享池净值与风控单日亏损被夸大。
    if all_quotes is None:
        all_quotes = {} if conn.in_transaction else _quotes(all_codes, asof_date=day)
    else:
        all_quotes = dict(all_quotes)
    _, position_value, nav, industries, code_values = _shared_account_exposure(conn, all_quotes, day)
    shared_cash = _shared_cash(conn)
    strategy_budget = _strategy_pool_budget(
        conn, account, nav, positions, all_quotes,
        exclude_reservation_key=exclude_reservation_key,
    )
    risk_state = _shared_risk_state(conn, account, nav, day)
    plan["risk"]["account"] = risk_state
    plan["risk"]["strategy_budget"] = strategy_budget
    plan["nav"] = round(nav, 2)
    plan["cash"] = round(shared_cash, 2)
    plan["position"] = position
    asset_type = _asset_type(code, local_quote.get("name"))
    plan["asset_type"] = asset_type

    if side == "buy":
        security_scope = _security_scope(code, local_quote.get("name"), local_quote.get("risk_flag"))
        plan["risk"]["security_scope"] = security_scope
        if not security_scope["allowed"]:
            reasons.append(security_scope["reason"])
        plan["risk"]["position_count_gate"] = {
            "current": len(open_codes), "committed": len(committed_open_codes), "limit": position_limit,
            "pool_current": len(pool_open_positions), "pool_limit": count_budget["pool_limit"],
            "dynamic": True, "source": count_budget["source"],
            "allocation_version": count_budget["allocation_version"],
            "is_existing_position": code in open_codes,
            "scope": "按策略账户计数；同一股票可由其他策略独立持有和交易",
        }
        if code not in committed_open_codes and len(committed_open_codes) >= position_limit:
            reasons.append(
                f"策略持仓及待成交席位已达动态上限 {len(committed_open_codes)}/{position_limit}"
            )
        # P1 死锁修复（2026-09-03）：同主判定处口径——预留仅当主力当日有
        # 在途候选且未到 14:30 放行时限时生效；查询异常维持原预留行为。
        _mf_seat_reserve = (
            account_id != MAIN_FORCE_STRATEGY_ID
            and not any(key[0] == MAIN_FORCE_STRATEGY_ID for key in pool_open_positions)
            and len(pool_open_positions) >= count_budget["pool_limit"] - 1
        )
        if _mf_seat_reserve:
            try:
                _mf_interest = int(conn.execute(
                    "SELECT COUNT(*) FROM paper_signals "
                    "WHERE account_id=? AND intended_date=? AND status IN (?,?,?)",
                    (MAIN_FORCE_STRATEGY_ID, str(day)[:10],
                     *ENTRY_RETRY_SIGNAL_STATUSES),
                ).fetchone()[0] or 0)
            except Exception:
                _mf_interest = 1
            _mf_now = _now()
            _mf_day = str(day)[:10]
            _mf_deadline = f"{_mf_day} 14:30:00" if _mf_day == _mf_now[:10] else None
            _mf_seat_reserve = _mf_interest > 0 and (
                _mf_deadline is None or _mf_now < _mf_deadline)
        if (account_id, code) not in pool_open_positions and len(pool_open_positions) >= count_budget["pool_limit"]:
            reasons.append(
                f"总持仓及待成交席位已达共享硬上限 {len(pool_open_positions)}/{count_budget['pool_limit']}"
            )
        elif _mf_seat_reserve:
            reasons.append(
                "共享池仅剩最后 1 席：为主力策略独立席位预留，"
                "待主力建仓或池内席位释放后恢复其他策略买入"
            )
        if code in open_codes:
            addition_allowed, addition_reason = _existing_position_addition_gate(
                conn, account, code, day,
            )
            plan["risk"]["existing_addition_gate"] = {
                "allowed": addition_allowed, "reason": addition_reason,
            }
            if not addition_allowed:
                reasons.append(addition_reason)
        # 候选、回补和加仓必须共享本轮实时市场快照，不能在同一轮又退回
        # 上一交易日的收盘门控。
        if live_universe is None:
            # A transaction-held plan must not fetch a full-market snapshot.
            # Missing live evidence is intentionally fail-closed by the
            # market gate below.
            if conn.in_transaction:
                live_universe = []
            else:
                try:
                    live_universe = dfc.fetch_market_snapshot_full(max_age=240) or []
                except Exception:
                    live_universe = []
        market = dict(market_context or _market_state(
            day, live_universe=live_universe,
            allow_network=not conn.in_transaction,
        ))
        plan["risk"]["market"] = market
        strategy_budget = _strategy_pool_budget(
            conn, account, nav, positions, all_quotes, market=market,
            exclude_reservation_key=exclude_reservation_key,
        )
        plan["risk"]["strategy_budget"] = strategy_budget
        if market.get("light") in ("red", "unknown"):
            reasons.append("市场门控为红灯或未知，禁止新开仓")
        if risk_state["blocked"]:
            reasons.extend(risk_state["reasons"])
        if account.get("mode") == "intraday_t" and asset_type != "stock_t1":
            reasons.append("短线日内做T账户只接受普通股票")
        if local_quote.get("risk_flag") or "ST" in str(local_quote.get("name") or "").upper():
            reasons.append("ST/退市风险标的禁止开仓")
        kline = _completed_kline(code, day, inclusive=False)
        decision = DE.buy_decision(
            code, name=local_quote.get("name"), kline=kline, snap=local_quote,
            sector_flow=[], overseas_gate=market.get("overseas") or {"light": "unknown"},
            news_hits=[],
        )
        plan["risk"]["model"] = decision
        if decision.get("tier") not in ("T1", "T2"):
            reasons.append(f"买入模型为 {decision.get('tier')}，未通过开仓门禁")
        if account_id == NEW_STRATEGY_ID:
            # Manual orders use the same independent quality/breakout entry
            # review; without a persisted candidate's disclosure/technical
            # evidence they fail closed rather than bypassing the strategy
            # model through the operator UI.
            manual_pick = dict(local_quote)
            manual_pick.update({"code": code, "name": local_quote.get("name") or code})
            manual_entry = _strategy_entry_assessment(
                account, manual_pick, local_quote, kline, decision, market=market,
            )
            plan["risk"]["entry_model"] = manual_entry
            if not manual_entry.get("passed"):
                reasons.extend(manual_entry.get("reasons") or ["未通过三日策略专属入场复核"])
        profile = _risk_profile(account)
        fill_reference = _num(limit_price) if order_type == "limit" else price
        fill_reference = max(fill_reference, 0.01)
        code_value = code_values.get(code, 0.0)
        industry_value = industries.get(plan["industry"], 0.0)
        safe_qty, sizing = _price_aware_qty(
            nav, shared_cash, position_value, industry_value, code_value,
            fill_reference * (1 + SLIPPAGE), ACCOUNT_SPECS[account_id]["hard_stop"], profile,
            exposure_cap=SHARED_POOL_MAX_EXPOSURE,
            max_exposure_cap=SHARED_POOL_MAX_EXPOSURE,
            strategy_position_value=strategy_budget["current_amount"],
            strategy_cap_amount=strategy_budget["absolute_cap_amount"],
            pool_cap_amount=strategy_budget["pool_cap_amount"],
            pending_strategy_amount=strategy_budget.get("pending_reserve_amount", 0.0),
            pending_pool_amount=strategy_budget.get("pending_pool_reserve_amount", 0.0),
        )
        plan["risk"]["sizing"] = sizing
        plan["recommended_qty"] = safe_qty
        if requested_qty <= 0:
            requested_qty = safe_qty
        if requested_qty > safe_qty:
            reasons.append(f"委托数量超过模型上限 {safe_qty} 股")
        plan["qty"] = requested_qty
    else:
        decision = DE.sell_decision(
            {
                "code": code, "name": local_quote.get("name"),
                "cost": position.get("cost") if position else None,
                "peak_price": position.get("peak_price") if position else None,
                "hold_days": _hold_days(position, day) if position else 0,
            },
            kline=_completed_kline(code, day, inclusive=False), snap=local_quote,
            overseas_gate={"light": "unknown"}, news_hits=[],
        )
        plan["risk"]["model"] = decision
        available_qty = int((position or {}).get("available_qty") or 0)
        plan["available_qty"] = available_qty
        if not position:
            reasons.append("该策略账户没有此标的持仓")
        if requested_qty <= 0:
            requested_qty = available_qty
        if requested_qty > available_qty:
            reasons.append(f"可卖份额仅 {available_qty} 股；当日买入份额仍受 T+1 锁定")
        plan["recommended_qty"] = available_qty
        plan["qty"] = requested_qty

    if plan["qty"] < LOT_SIZE or plan["qty"] % LOT_SIZE:
        reasons.append("委托数量必须为 100 股的正整数倍")
    if order_type == "limit":
        limit_value = _num(limit_price)
        plan["triggered"] = price <= limit_value if side == "buy" else price >= limit_value

    # 手动委托只是人工发起，不得绕过自动交易使用的行情真实性门禁。限价单在
    # 尚未触发时可以保留（触发瞬间仍会复核）；一旦需要模拟成交，买入必须双源
    # 通过，卖出至少要有当日新鲜主行情，且跌停时绝不虚构成交。
    execution_gate = _execution_quote_status(
        local_quote,
        day,
        purpose="entry" if side == "buy" else "exit",
    )
    plan["risk"]["execution_quote"] = execution_gate
    requires_fill_gate = order_type == "market" or plan["triggered"]
    if requires_fill_gate and not execution_gate.get("fresh"):
        reasons.append(f"成交行情未通过校验：{execution_gate.get('reason') or '未知行情状态'}")
        plan["risk"]["fill_deferred"] = {
            "kind": "quote",
            "reason": execution_gate.get("reason") or "实时行情校验未通过",
        }
    if side == "sell" and requires_fill_gate:
        limit_pct = _limit_pct(code, local_quote.get("name"), local_quote.get("risk_flag"))
        limit_down = price <= 0 or _num(local_quote.get("pct")) <= -limit_pct + 0.05
        plan["risk"]["limit_down_gate"] = {
            "blocked": limit_down,
            "limit_pct": limit_pct,
            "pct": _num(local_quote.get("pct")),
        }
        if limit_down:
            reasons.append(f"当前触及 {limit_pct:.1f}% 跌停保护，不能虚构卖出成交")
            plan["risk"]["fill_deferred"] = {
                "kind": "limit_down",
                "reason": f"当前触及 {limit_pct:.1f}% 跌停保护",
            }
    fill_price = (
        min(price * (1 + SLIPPAGE), _num(limit_price))
        if side == "buy" and order_type == "limit" and plan["triggered"]
        else max(price * (1 - SLIPPAGE), _num(limit_price))
        if side == "sell" and order_type == "limit" and plan["triggered"]
        else price * (1 + SLIPPAGE if side == "buy" else 1 - SLIPPAGE)
    )
    amount = max(plan["qty"], 0) * max(fill_price, 0)
    fees = _commission(amount) + (amount * STAMP_SELL if side == "sell" else 0.0)
    if side == "buy":
        _, pending_cash = _pending_buy_reservations(
            conn, exclude_order_key=exclude_reservation_key,
        )
        if amount + fees > shared_cash - pending_cash + 1e-6:
            reasons.append(
                f"共享资金池可用现金不足（已有待成交买单预占 ¥{pending_cash:,.2f}）"
                if pending_cash > 0 else "共享资金池可用现金不足"
            )
    plan.update({
        "fill_price": round(fill_price, 4), "amount": round(amount, 2),
        "fees": round(fees, 2), "reasons": list(dict.fromkeys(reasons)),
    })
    plan["allowed"] = not plan["reasons"]
    if entry_frozen:
        plan["status"] = ENTRY_FROZEN_WAITLIST_STATUS
        plan["waitlisted"] = True
    else:
        plan["status"] = (
            "risk_rejected" if not plan["allowed"]
            else "ready_to_fill" if plan["triggered"]
            else "pending_limit"
        )
    plan["risk"] = _with_decision_snapshot(
        plan.get("risk") or {}, account_id=account_id, code=code, side=side,
        decision=(
            ENTRY_FROZEN_WAITLIST_STATUS
            if entry_frozen
            else "approved_manual" if plan["allowed"] else "rejected_manual"
        ),
        reason="；".join(plan.get("reasons") or []) or None,
        asof_date=day, quote=local_quote,
        kline=_completed_kline(code, day, inclusive=False),
        final_score=((plan.get("risk") or {}).get("model") or {}).get("avg_score")
        if isinstance((plan.get("risk") or {}).get("model"), dict) else None,
    )
    return plan


def preview_manual_order(
    account_id, code, side, qty=0, order_type="market", limit_price=None, asof_date=None,
):
    init_db()
    with _db() as conn:
        return _manual_order_plan(
            conn, account_id, code, side, qty, order_type, limit_price, asof_date,
        )


def _execute_manual_plan(conn, account, plan, order_id, asof_day):
    _assert_active_lease(conn, "manual fill")
    if plan.get("side") == "buy" and _entry_freeze_enabled():
        # Callers normally gate this earlier; keep the fill primitive itself
        # fail-closed so a future path cannot debit cash or write a lot while
        # the operator freeze is active.
        raise RuntimeError(_entry_frozen_reason("成交执行"))
    qty = int(plan["qty"])
    amount = _num(plan["amount"])
    fees = _num(plan["fees"])
    fill_price = _num(plan["fill_price"])
    realized_pnl = None
    if plan["side"] == "buy":
        _assert_active_lease(conn, "manual fill cash debit")
        _debit_shared_cash(conn, amount + fees, preferred_account_id=account["id"])
        _finish_capital_reservation(conn, order_id, "consumed")
        _record_lot(conn, account, plan, qty, fill_price, asof_day, order_id, is_t_base=True, fees=fees)
    else:
        _assert_active_lease(conn, "manual fill lot consumption")
        consumed, cost_amount = _consume_available_lots(
            conn, account["id"], plan["code"], qty, asof_day
        )
        if consumed != qty:
            raise RuntimeError("可卖份额在成交前发生变化，委托已停止")
        realized_pnl = amount - cost_amount - fees
        _credit_shared_cash(conn, amount - fees, account["id"])
    _assert_active_lease(conn, "manual fill finalization")
    conn.execute(
        """UPDATE paper_orders SET filled_price=?,amount=?,fees=?,status='filled',
           reason=?,risk_payload=?,realized_pnl=?,executed_at=? WHERE id=?""",
        (
            fill_price, amount, fees, "手动模拟委托经模型复核后成交",
            _json(plan["risk"]), realized_pnl, _now(), order_id,
        ),
    )
    conn.execute(
        """INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            order_id, account["id"], plan["side"], plan["code"], qty, fill_price,
            amount, fees, _date(asof_day).isoformat(), plan.get("quote_at"),
            "本地行情快照按 0.10% 滑点模拟；不代表真实可成交价格",
        ),
    )
    _risk_log(
        conn, account["id"], plan["code"], plan["side"], "manual_filled",
        "手动模拟委托通过模型门禁并成交", plan,
    )
    _audit(
        conn, account["id"], "manual_order_filled",
        f"{plan['side']} {plan['code']} {qty}股 @ {fill_price:.2f}",
    )
    _sync_positions(conn, account["id"], asof_day)
    return realized_pnl


def _commit_strategy_buy(
    conn, account, plan, asof_day, *, reason, detail, action,
    is_t_base=True, assumption="实时行情 + 滑点模拟；不代表真实可成交价格",
):
    """Commit a strategy buy through one reservation/debit/fill transaction.

    Rebuy and scale-in used to debit the shared cash balance directly.  This
    primitive makes them obey the same reservation invariant as normal and
    manual buys, while a savepoint prevents a malformed lot/fill write from
    leaving a cash debit behind.
    """
    _assert_active_lease(conn, "strategy auxiliary buy")
    account_id = account["id"]
    code = str(plan["code"])
    qty = int(plan["qty"])
    fill_price = _num(plan["fill_price"])
    amount = _num(plan["amount"])
    fees = _num(plan["fees"])
    cursor = conn.execute(
        """INSERT INTO paper_orders(
           account_id,side,code,name,qty,planned_price,status,reason,
           risk_payload,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (account_id, "buy", code, plan.get("name"), qty,
         _num(plan.get("planned_price"), fill_price), "pending_execution",
         reason, _json(detail), _now()),
    )
    order_id = int(cursor.lastrowid)
    savepoint = f"strategy_buy_{order_id}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        _assert_active_lease(conn, "strategy auxiliary reservation")
        reserved, reserve_reason = _reserve_shared_capital(
            conn, order_id, account_id, code, amount, fees,
        )
        if not reserved:
            raise RuntimeError(reserve_reason or "共享资金池预占失败")
        _assert_active_lease(conn, "strategy auxiliary cash debit")
        _debit_shared_cash(conn, amount + fees, preferred_account_id=account_id)
        _finish_capital_reservation(conn, order_id, "consumed")
        conn.execute(
            """UPDATE paper_orders SET filled_price=?,amount=?,fees=?,status='filled',
               executed_at=? WHERE id=?""",
            (fill_price, amount, fees, _now(), order_id),
        )
        _assert_active_lease(conn, "strategy auxiliary lot")
        _record_lot(
            conn, account, plan, qty, fill_price, asof_day, order_id,
            is_t_base=is_t_base, fees=fees,
        )
        conn.execute(
            """INSERT INTO paper_fills(
               order_id,account_id,side,code,qty,price,amount,fees,fill_date,
               quote_at,assumption)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, account_id, "buy", code, qty, fill_price, amount, fees,
             _date(asof_day).isoformat(), plan.get("quote_at"), assumption),
        )
        _assert_active_lease(conn, "strategy auxiliary fill finalization")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if _lease_lost(exc):
            raise
        _finish_capital_reservation(conn, order_id, "released")
        failure = f"{action}未成交：{exc}"
        conn.execute(
            "UPDATE paper_orders SET status=?,reason=?,filled_price=NULL,amount=NULL,fees=NULL,executed_at=NULL WHERE id=?",
            (STRATEGY_EXECUTION_RETRY_STATUS, failure, order_id),
        )
        _risk_log(conn, account_id, code, "buy", STRATEGY_EXECUTION_RETRY_STATUS, failure, detail)
        return None, failure
    _assert_active_lease(conn, "strategy auxiliary audit")
    _risk_log(conn, account_id, code, "buy", action, reason, detail)
    _audit(conn, account_id, action, f"{code} {qty}股 @ {fill_price:.2f}")
    return {"order_id": order_id, "side": "buy", "code": code, "qty": qty}, None


def submit_manual_order(
    account_id, code, side, qty=0, order_type="market", limit_price=None, asof_date=None,
):
    init_db()
    day = _date(asof_date)
    with _db() as snapshot_conn:
        existing_codes = [
            row["code"] for row in _rows(
                snapshot_conn,
                "SELECT DISTINCT code FROM paper_position_lots WHERE remaining_qty>0",
            )
        ]
    quote_map = _quotes(sorted(set(existing_codes) | {str(code)}), asof_date=day)
    live_universe = None
    if str(side).lower() == "buy":
        try:
            live_universe = dfc.fetch_market_snapshot_full(max_age=240) or []
        except Exception:
            live_universe = []
    market_context = _market_state(day, live_universe=live_universe, allow_network=True) if str(side).lower() == "buy" else None
    with _db(immediate=True) as conn:
        plan = _manual_order_plan(
            conn, account_id, code, side, qty, order_type, limit_price, day,
            quote=quote_map.get(str(code)) or {}, all_quotes=quote_map,
            live_universe=live_universe, market_context=market_context,
        )
        account_row = conn.execute("SELECT * FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        account = dict(account_row) if account_row else None
        status = plan.get("status") or "risk_rejected"
        reason = "；".join(plan.get("reasons") or [])
        if status == "pending_limit":
            reason = "限价尚未触发；委托当日有效，触发时重新执行风控"
        elif status == ENTRY_FROZEN_WAITLIST_STATUS:
            reason = reason or _entry_frozen_reason("手动委托")
        cursor = conn.execute(
            """INSERT INTO paper_orders(
               account_id,side,code,name,qty,planned_price,status,reason,risk_payload,
               order_type,origin,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_id, side, code, plan.get("name"), int(plan.get("qty") or 0),
                _num(limit_price) if order_type == "limit" else _num(plan.get("quote_price")),
                status, reason, _json(plan.get("risk") or {}), order_type, "manual",
                day.isoformat() if status in {"pending_limit", ENTRY_FROZEN_WAITLIST_STATUS}
                and order_type == "limit" else None, _now(),
            ),
        )
        order_id = cursor.lastrowid
        if status in {"ready_to_fill", "pending_limit"} and account \
                and plan.get("allowed") and side == "buy":
            # Limit orders reserve at their limit price (plus commission),
            # not at the last snapshot price.  A pending order therefore
            # cannot silently consume capacity earmarked for another model.
            reserve_price = (
                _num(limit_price) if status == "pending_limit"
                else _num(plan.get("fill_price"))
            )
            reserve_amount = max(0, int(plan.get("qty") or 0)) * max(reserve_price, 0.0)
            reserve_fees = _commission(reserve_amount)
            reserved, reserve_reason = _reserve_shared_capital(
                conn, order_id, account_id, code, reserve_amount, reserve_fees,
            )
            if not reserved:
                status = "risk_rejected"
                reason = reserve_reason or "共享资金池预占失败"
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=? WHERE id=?",
                    (status, reason, order_id),
                )
                _risk_log(conn, account_id, code, side, "manual_rejected", reason, plan)
        if status == "ready_to_fill" and account:
            savepoint = f"manual_fill_{int(order_id)}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                _execute_manual_plan(conn, account, plan, order_id, day)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                status = "filled"
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                _finish_capital_reservation(conn, order_id, "released")
                status = MANUAL_EXECUTION_RETRY_STATUS
                reason = f"手动模拟委托执行失败，可重试：{type(exc).__name__}: {exc}"
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=?,risk_payload=?,filled_price=NULL,amount=NULL,fees=NULL,executed_at=NULL WHERE id=?",
                    (status, reason, _json({**(plan.get("risk") or {}), "execution_error": str(exc), "retryable": True}), order_id),
                )
                _risk_log(conn, account_id, code, side, status, reason, {"order_id": order_id, "error": str(exc)})
        elif status == "pending_limit" and account and plan.get("allowed"):
            # Reservation is held until trigger, cancellation or expiry.
            pass
        elif status == ENTRY_FROZEN_WAITLIST_STATUS:
            _risk_log(
                conn, account_id, code, "buy", ENTRY_FROZEN_WAITLIST_STATUS,
                reason, plan,
            )
            _audit(conn, account_id, ENTRY_FROZEN_WAITLIST_STATUS, f"manual: {code} order={order_id}")
        elif status == "risk_rejected":
            _risk_log(conn, account_id, code, side, "manual_rejected", reason, plan)
        _record_nav(conn, day, quotes=quote_map)
        return {"order_id": order_id, "status": status, "plan": plan}


def cancel_manual_order(order_id):
    init_db()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM paper_orders WHERE id=? AND origin='manual'", (int(order_id),)
        ).fetchone()
        if not row:
            raise ValueError("未找到手动模拟委托")
        if row["status"] not in set(ENTRY_RETRY_ORDER_STATUSES):
            raise ValueError("只有待触发的限价委托可以撤销")
        conn.execute(
            "UPDATE paper_orders SET status='cancelled',reason='用户撤销模拟委托',cancelled_at=? WHERE id=?",
            (_now(), int(order_id)),
        )
        _finish_capital_reservation(conn, order_id, "released")
        _audit(conn, row["account_id"], "manual_order_cancelled", f"order={order_id}")
    return {"order_id": int(order_id), "status": "cancelled"}


def process_pending_manual_orders(asof_date=None):
    init_db()
    day = _date(asof_date)
    output = []
    retry_placeholders = ",".join("?" for _ in ENTRY_RETRY_ORDER_STATUSES)
    retry_params = tuple(ENTRY_RETRY_ORDER_STATUSES)
    with _db() as snapshot_conn:
        pending = _rows(
            snapshot_conn,
            f"""SELECT * FROM paper_orders
               WHERE origin='manual' AND status IN ({retry_placeholders})
               ORDER BY id""",
            retry_params,
        )
        position_codes = [
            row["code"] for row in _rows(
                snapshot_conn,
                "SELECT DISTINCT code FROM paper_position_lots WHERE remaining_qty>0",
            )
        ]
    if not pending:
        return output
    # All provider calls happen before taking the write lock.  A quote failure
    # is isolated per source by _quotes and leaves the order retryable.
    quote_map = _quotes(sorted({row["code"] for row in pending} | set(position_codes)), asof_date=day)
    live_universe = None
    if any(row.get("side") == "buy" for row in pending):
        try:
            live_universe = dfc.fetch_market_snapshot_full(max_age=240) or []
        except Exception:
            live_universe = []
    market_context = (
        _market_state(day, live_universe=live_universe, allow_network=True)
        if any(row.get("side") == "buy" for row in pending) else None
    )
    with _db(immediate=True) as conn:
        pending = _rows(
            conn,
            f"""SELECT * FROM paper_orders
               WHERE origin='manual' AND status IN ({retry_placeholders})
               ORDER BY id""",
            retry_params,
        )
        for order in pending:
            _assert_active_lease(conn, "pending manual order")
            if order.get("side") == "buy" and _entry_freeze_enabled():
                reason = _entry_frozen_reason("待触发限价委托")
                payload = _loads(order.get("risk_payload"), {})
                payload["entry_freeze"] = {
                    "enabled": True, "env": ENTRY_FREEZE_ENV,
                    "status": ENTRY_FROZEN_WAITLIST_STATUS,
                    "source": "待触发限价委托", "asof_date": day.isoformat(),
                    "previous_status": order.get("status"),
                    "reason": reason,
                }
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=?,risk_payload=? WHERE id=?",
                    (ENTRY_FROZEN_WAITLIST_STATUS, reason, _json(payload), order["id"]),
                )
                _risk_log(
                    conn, order["account_id"], order["code"], "buy",
                    ENTRY_FROZEN_WAITLIST_STATUS, reason, payload,
                )
                _audit(conn, order["account_id"], ENTRY_FROZEN_WAITLIST_STATUS,
                       f"pending_limit: {order['code']} order={order['id']}")
                output.append({
                    "order_id": order["id"], "status": ENTRY_FROZEN_WAITLIST_STATUS,
                    "waitlisted": True, "reason": reason,
                })
                continue
            if order.get("expires_at") and order["expires_at"] < day.isoformat():
                conn.execute(
                    "UPDATE paper_orders SET status='expired',reason='限价委托已过有效期' WHERE id=?",
                    (order["id"],),
                )
                _finish_capital_reservation(conn, order["id"], "released")
                output.append({"order_id": order["id"], "status": "expired"})
                continue
            quote = dict(quote_map.get(order["code"]) or {})
            order_type = str(order.get("order_type") or "limit").lower()
            try:
                plan = _manual_order_plan(
                    conn, order["account_id"], order["code"], order["side"], order["qty"],
                    order_type, order["planned_price"] if order_type == "limit" else None, day, quote=quote,
                    exclude_reservation_key=str(order["id"]),
                    all_quotes=quote_map, live_universe=live_universe,
                    market_context=market_context,
                )
            except Exception as exc:
                reason = f"待成交订单复核失败，可重试：{type(exc).__name__}: {exc}"
                conn.execute(
                    "UPDATE paper_orders SET status=?,reason=?,risk_payload=? WHERE id=?",
                    (MANUAL_EXECUTION_RETRY_STATUS, reason, _json({"execution_error": str(exc), "retryable": True}), order["id"]),
                )
                _finish_capital_reservation(conn, order["id"], "released")
                _risk_log(conn, order["account_id"], order["code"], order["side"], MANUAL_EXECUTION_RETRY_STATUS, reason, {"order_id": order["id"], "error": str(exc)})
                output.append({"order_id": order["id"], "status": MANUAL_EXECUTION_RETRY_STATUS, "reason": reason})
                continue
            if not plan["allowed"]:
                reason = "；".join(plan["reasons"])
                deferred = dict((plan.get("risk") or {}).get("fill_deferred") or {})
                # 触发时只是行情缺失/跌停的限价单继续保留，等待下一轮真实行情
                # 或价格解锁；不能释放资金后改用旧报价成交，也不把它伪装成风控
                # 拒绝。策略、资金和 T+1 等其它门禁失败才会终止委托。
                if plan.get("triggered") and deferred:
                    conn.execute(
                        "UPDATE paper_orders SET reason=?,risk_payload=? WHERE id=?",
                        (reason, _json(plan["risk"]), order["id"]),
                    )
                    output.append({
                        "order_id": order["id"],
                        "status": "pending_execution_guard",
                        "reason": deferred.get("reason") or reason,
                    })
                    continue
                conn.execute(
                    "UPDATE paper_orders SET status='risk_rejected',reason=?,risk_payload=? WHERE id=?",
                    (reason, _json(plan["risk"]), order["id"]),
                )
                _finish_capital_reservation(conn, order["id"], "released")
                output.append({"order_id": order["id"], "status": "risk_rejected", "reason": reason})
                continue
            if not plan["triggered"]:
                reserve_price = _num(order.get("planned_price"), _num(plan.get("limit_price")))
                reserve_amount = max(0, int(plan.get("qty") or 0)) * max(reserve_price, 0.0)
                reserve_fees = _commission(reserve_amount)
                reserved, reserve_reason = _reserve_shared_capital(
                    conn, order["id"], order["account_id"], order["code"],
                    reserve_amount, reserve_fees,
                )
                if not reserved:
                    reason = reserve_reason or "共享资金池预占失败"
                    conn.execute(
                        "UPDATE paper_orders SET status='risk_rejected',reason=?,risk_payload=? WHERE id=?",
                        (reason, _json(plan["risk"]), order["id"]),
                    )
                    _finish_capital_reservation(conn, order["id"], "released")
                    output.append({"order_id": order["id"], "status": "risk_rejected", "reason": reason})
                    continue
                conn.execute(
                    "UPDATE paper_orders SET status='pending_limit',reason=?,risk_payload=? WHERE id=?",
                    ("限价尚未触发；委托当日有效，触发时重新执行风控", _json(plan["risk"]), order["id"]),
                )
                output.append({"order_id": order["id"], "status": "pending_limit"})
                continue
            account = dict(conn.execute(
                "SELECT * FROM paper_accounts WHERE id=?", (order["account_id"],)
            ).fetchone())
            reserve_price = _num(plan.get("fill_price"), _num(order.get("planned_price")))
            reserve_amount = max(0, int(plan.get("qty") or 0)) * max(reserve_price, 0.0)
            reserve_fees = _commission(reserve_amount)
            reserved, reserve_reason = _reserve_shared_capital(
                conn, order["id"], order["account_id"], order["code"],
                reserve_amount, reserve_fees,
            )
            if not reserved:
                reason = reserve_reason or "共享资金池预占失败"
                # A triggered order can become temporarily unfunded because
                # another reservation is still active.  Keep it retryable;
                # turning this into a terminal rejection loses the order even
                # though a later sell/cancel may release the cash.
                conn.execute(
                    "UPDATE paper_orders SET status='pending_limit',reason=?,risk_payload=? WHERE id=?",
                    (f"触发后等待资金重算：{reason}", _json({**(plan["risk"] or {}), "fill_deferred": {
                        "reason": reason, "retryable": True,
                    }}), order["id"]),
                )
                output.append({"order_id": order["id"], "status": "pending_execution_guard", "reason": reason})
                continue
            # Isolate each pending fill.  A single stale reservation, cash
            # mismatch, or ledger error must not roll back the whole batch and
            #—more importantly—must not abort the caller's subsequent risk
            # sells for this scan.
            savepoint = f"pending_exec_{int(order['id'])}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                _execute_manual_plan(conn, account, plan, order["id"], day)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                output.append({"order_id": order["id"], "status": "filled"})
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if _lease_lost(exc):
                    raise
                reason = f"待成交执行异常，下一轮重试：{type(exc).__name__}: {exc}"
                # Release the old reservation before retrying.  The next
                # cycle will resize it against the then-current cash pool.
                _finish_capital_reservation(conn, order["id"], "released")
                conn.execute(
                    "UPDATE paper_orders SET status='pending_limit',reason=?,risk_payload=? WHERE id=?",
                    (reason, _json({**(plan.get("risk") or {}),
                                    "execution_error": str(exc),
                                    "execution_retry": True}), order["id"]),
                )
                _risk_log(
                    conn, order["account_id"], order["code"], order["side"],
                    "pending_execution_retry", reason,
                    {"order_id": order["id"], "error": str(exc)},
                )
                output.append({
                    "order_id": order["id"], "status": "pending_execution_retry",
                    "reason": reason,
                })
        _record_nav(conn, day, quotes=quote_map)
    return output


def execute_open(asof_date=None):
    """09:31 开盘审批：先复核共享开盘事件，再查询待执行候选。"""
    init_db()
    day = _date(asof_date)
    # Risk exits must run before the opening data gate and before any waitlist
    # order is reconsidered.  A quote gate may stop buys, but never stops a
    # protective sell pass.
    opening_risk = monitor_risk(day)
    # P3 审计修复（2026-09-03）：开盘 slot 与 monitor_intraday 一样先做
    # 轻量数据源探活。此前 09:31 只拉快照而不探活，入场门禁
    # _entry_freeze_status 读到的仍是上一交易日收盘的 data_source_health
    # （age 远大于 15 分钟），把开盘全部候选判为“行情源未通过最近15分钟
    # 健康检查”而冻结，再由 09:33 首轮 intraday 扫描重建——每天约 20 条
    # superseded 噪音并延迟建仓约 5 分钟。探活同时会在失败时关闭连接池、
    # 轮换东财/腾讯重试，让随后的全市场快照拉取更可靠。
    # 探活失败不放宽任何门禁：门禁仍按旧快照冻结（fail-closed 语义不变）。
    try:
        open_source_health = dfc.check_data_source_health(force=True)
    except Exception as exc:
        open_source_health = {
            "healthy": False, "reconnected": False, "attempts": 0,
            "action": f"健康检查异常：{type(exc).__name__}: {exc}",
        }
    # 门禁状态有 15s 缓存，且上面的 monitor_risk 可能已用旧快照算过一次；
    # 探活后强制重算，让本轮买入判定基于最新健康快照。
    try:
        _entry_freeze_status(force=True)
    except Exception:
        pass
    # A pending signal is not permission to trade against yesterday's or a
    # partial market snapshot.  Re-check the same full-market gate here so the
    # standalone 09:31 scheduler cannot bypass a blocked 3-minute scan.
    try:
        open_rows = _validated_live_universe(
            dfc.fetch_market_snapshot_full(max_age=120), day, max_quote_age_minutes=20,
        )
    except Exception:
        open_rows = []
    open_gate = _live_scan_gate(open_rows, day)
    if not open_gate["ready"]:
        reason = (
            f"开盘实时行情覆盖 {open_gate['covered_codes']}/{open_gate['eligible_codes']}"
            f"（{open_gate['coverage_pct']:.1f}%），需至少 {open_gate['required_codes']} 只；"
            "阻止待执行新买入，保留独立风控/卖出任务"
        )
        return {
            "slot": "open", "date": day.isoformat(), "status": "blocked",
            "orders": list(opening_risk.get("orders", [])),
            "manual_orders": list(opening_risk.get("manual_orders", [])), "expired": 0,
            "reason": reason, "live_scan_gate": open_gate,
            "data_source_health": open_source_health,
        }
    current_clock = dt.datetime.now().strftime("%H:%M")
    opening_event = (
        monitor_opening_events(day, event_clock="09:31")
        if current_clock in {"09:30", "09:31", "09:32"} else
        {"status": "skipped", "slot": "opening_event", "reason": "非开盘审批时间"}
    )
    try:
        # Opening-event risk exits must commit before any pending buy is
        # reconsidered; a sell can release both T+1 capacity and shared cash.
        manual_orders = process_pending_manual_orders(day)
    except Exception as exc:
        if _lease_lost(exc):
            raise
        # A malformed pending buy is retryable work; it must never prevent
        # the independent opening scan from evaluating fresh candidates.
        manual_orders = [{"status": "pending_batch_retry", "reason": str(exc)}]
    # Snapshot the pending queue and fetch all external data before acquiring
    # the write lock below.  A slow quote/news provider must not block risk,
    # manual cancellation, or another scheduler process on SQLite.
    with _db() as prefetch_conn:
        # 冻结等待池只预取 5 天内的信号；更老的会在下方统一按过期处理，
        # 不值得为它们消耗行情/新闻配额。
        _waitlist_cutoff = (day - dt.timedelta(days=5)).isoformat()
        prefetch_pending = _rows(
            prefetch_conn,
            """SELECT code,name FROM paper_signals
               WHERE (status=? AND intended_date>?) OR (status='pending' AND intended_date<=?)
               ORDER BY COALESCE(t_score,0) DESC,
                        COALESCE(rank_score,0) DESC,id""",
            (ENTRY_FROZEN_WAITLIST_STATUS, _waitlist_cutoff, day.isoformat()),
        )
    prefetch_codes = [str(row.get("code") or "") for row in prefetch_pending if row.get("code")]
    prefetch_names = {
        str(row.get("code")): row.get("name") or str(row.get("code"))
        for row in prefetch_pending if row.get("code")
    }
    prefetch_market = _market_state(day) if prefetch_codes else None
    prefetch_news = _news_for(prefetch_names) if prefetch_codes else []
    prefetch_quotes = _quotes(prefetch_codes, asof_date=day) if prefetch_codes else {}
    with _db(immediate=True) as conn:
        accounts = _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'")
        # Limited slots must be won by the strongest live candidate, not by
        # whichever signal happened to be inserted first.
        pending = _rows(
            conn,
            """SELECT * FROM paper_signals
               WHERE status IN ('pending',?)
               ORDER BY COALESCE(t_score,0) DESC,
                        COALESCE(rank_score,0) DESC,account_id,id""",
            (ENTRY_FROZEN_WAITLIST_STATUS,),
        )
        expired = [
            row for row in pending
            if row.get("status") == "pending" and row["intended_date"] < day.isoformat()
        ]
        # 冻结等待池同样要有保鲜期：无限期 due 会让旧信号带着陈旧的
        # close_price 与因子证据长期占据排名席位。超过 5 个自然日仍未解冻
        # 的等待池信号按过期处理；若标的仍有效，当日选股会重新生成新信号。
        waitlist_cutoff = (day - dt.timedelta(days=5)).isoformat()
        expired += [
            row for row in pending
            if row.get("status") == ENTRY_FROZEN_WAITLIST_STATUS
            and str(row["intended_date"] or "")[:10] <= waitlist_cutoff
        ]
        for row in expired:
            conn.execute("UPDATE paper_signals SET status='expired', reason='错过有效交易日，不追价补单' WHERE id=?", (row["id"],))
        due = [
            row for row in pending
            if row.get("status") != "pending" or row["intended_date"] == day.isoformat()
        ]
        if not due:
            return {
                "slot": "open", "date": day.isoformat(),
                "orders": list(opening_risk.get("orders", [])) + list(opening_event.get("orders", [])),
                "opening_event": opening_event,
                "manual_orders": list(opening_risk.get("manual_orders", [])) + manual_orders,
                "expired": len(expired),
            }
        market = prefetch_market
        names = {s["code"]: s.get("name") or s["code"] for s in due}
        news = prefetch_news
        quote_map = prefetch_quotes
        account_map = {a["id"]: a for a in accounts}
        ranked_due = []
        for signal in due:
            account = account_map.get(signal["account_id"])
            if not account:
                continue
            assessment = _waitlist_realtime_assessment(
                signal, account, quote_map.get(signal["code"], {}), day,
            )
            payload = _loads(signal.get("payload"), {})
            payload["waitlist_realtime"] = assessment
            # Persist the evidence before attempting an order.  A rejected or
            # capacity-blocked candidate can therefore be compared fairly on
            # the next 3-minute round instead of forever retaining its
            # yesterday/close rank.
            conn.execute(
                "UPDATE paper_signals SET payload=? WHERE id=?",
                (_json(payload), signal["id"]),
            )
            signal = dict(signal)
            signal["payload"] = _json(payload)
            ranked_due.append((assessment, signal, account))
        # Shared capital/slots are scarce: resolve them from one live queue,
        # not by account insertion order.  Strategy budgets, borrowing and all
        # normal approval gates remain inside _buy_order.
        ranked_due.sort(
            key=lambda item: (
                bool(item[0].get("entry_envelope_ok")),
                _num(item[0].get("live_score")),
                _num(item[1].get("t_score")),
                _num(item[1].get("rank_score")),
                -int(_num(item[1].get("id"))),
            ),
            reverse=True,
        )
        result = []
        for _, signal, account in ranked_due:
            result.append(_buy_order(
                conn, account, signal, quote_map.get(signal["code"], {}), market, news, day,
                all_quotes=quote_map,
            ))
        _record_nav(conn, day, quotes=quote_map)
        return {
            "slot": "open", "date": day.isoformat(), "market": market,
            "orders": list(opening_risk.get("orders", [])) + list(opening_event.get("orders", [])) + result,
            "opening_event": opening_event,
            "manual_orders": list(opening_risk.get("manual_orders", [])) + manual_orders,
            "expired": len(expired),
        }


def _hold_days(position, asof_day):
    frame = _completed_kline(position["code"], asof_day, inclusive=False)
    if frame is not None and not frame.empty:
        try:
            return int((frame.index.date > _date(position["entry_date"])).sum())
        except Exception:
            pass
    return max((_date(asof_day) - _date(position["entry_date"])).days, 0)


def _replacement_min_hold_days(account_id):
    """Return the active-rotation observation window for one strategy.

    This is separate from ACCOUNT_SPECS.hold_min: that field is the normal
    holding horizon, while this value only decides when a *sellable* weak
    position may yield a full slot to a materially stronger candidate.
    """
    return int(POSITION_REVIEW_MIN_HOLD_DAYS_BY_STRATEGY.get(
        str(account_id or ""), POSITION_REVIEW_MIN_HOLD_DAYS,
    ))


def _score100(value, default=50.0):
    """Normalize model/rank scores that may be expressed as 0..1 or 0..100."""
    if value is None:
        return default
    value = _num(value, default / 100.0)
    if 0.0 <= value <= 1.5:
        value *= 100.0
    return max(0.0, min(100.0, value))


def _replacement_score_from_signal(signal):
    """Return the comparable 0..100 score used for a slot replacement.

    The entry assessment, intraday score and selection rank have different
    scales.  Keeping this composition in one helper prevents the order gate,
    holding review and UI audit from comparing different numbers.
    """
    signal = signal or {}
    payload = _loads(signal.get("payload"), {}) if isinstance(signal, dict) else {}
    entry = (payload.get("decision") or {}).get("entry_model") or {}
    return round(
        _score100(entry.get("score"), 0.0) * 0.45
        + _score100(signal.get("t_score"), 0.0) * 0.35
        + _score100(signal.get("rank_score"), 0.0) * 0.20,
        2,
    )


def _waitlist_realtime_assessment(signal, account, quote, asof_day):
    """Re-score a waiting candidate with the quote used for this execution.

    A pending signal is a reservation of research effort, not a permanent
    execution priority.  The original cross-sectional score remains the
    anchor; quote freshness, intraday drift and the strategy's current entry
    envelope decide the live ordering.  This function does not approve an
    order -- ``_buy_order`` still performs the full dual-source/risk check.
    """
    payload = _loads(signal.get("payload"), {})
    pick = dict(payload.get("pick") or {})
    pick.update({key: value for key, value in (quote or {}).items() if value is not None})
    base = _replacement_score_from_signal(signal)
    source_price = _num((payload.get("pick") or {}).get("price"), None)
    live_price = _num((quote or {}).get("price"), None)
    drift_pct = ((live_price / source_price - 1.0) * 100.0) if source_price and live_price else None
    fresh = bool((quote or {}).get("quote_at"))
    allowed, reason = _new_entry_price_gate(account, pick)
    # Penalise stale or materially changed candidates without fabricating a
    # prediction.  A positive score alone never overrides the entry envelope.
    score = base + (4.0 if fresh else -25.0)
    if drift_pct is not None:
        score -= min(18.0, abs(drift_pct) * 1.6)
    if not allowed:
        score -= 35.0
    return {
        "at": _now(), "asof_date": str(_date(asof_day)),
        "base_score": round(base, 2), "live_score": round(max(0.0, score), 2),
        "source_price": source_price, "live_price": live_price,
        "drift_pct": round(drift_pct, 3) if drift_pct is not None else None,
        "quote_at": (quote or {}).get("quote_at"),
        "quote_validation": (quote or {}).get("validation"),
        "entry_envelope_ok": bool(allowed), "entry_envelope_reason": reason,
    }


def _best_replacement_candidate(conn, account_id, day, held_codes):
    """Find the strongest pending candidate that is not already held."""
    next_day = _next_weekday(day).isoformat()
    rows = _rows(
        conn,
        """SELECT id,code,name,rank_score,t_score,payload,intended_date,status
           FROM paper_signals
           WHERE account_id=? AND status IN ('pending','deferred_capacity',?)
             AND intended_date>=? AND intended_date<=?
           ORDER BY COALESCE(t_score,0) DESC,COALESCE(rank_score,0) DESC,id DESC""",
        (account_id, ENTRY_FROZEN_WAITLIST_STATUS, _date(day).isoformat(), next_day),
    )
    held_codes = {str(code) for code in (held_codes or set())}
    best = None
    for row in rows:
        code = str(row.get("code") or "")
        if not code or code in held_codes:
            continue
        payload = _loads(row.get("payload"), {})
        pick = payload.get("pick") or {}
        if not _security_scope(
            code, row.get("name") or pick.get("name"), pick.get("risk_flag"),
        )["allowed"]:
            continue
        # Replacement quality must be a stable composite.  Taking the maximum
        # of three differently-scaled fields let one noisy value hijack the
        # last slot in a concentrated portfolio.
        score = _replacement_score_from_signal(row)
        item = {
            "signal_id": int(row["id"]), "status": row.get("status"),
            "code": code, "name": row.get("name") or code,
            "score": round(score, 2), "intended_date": row.get("intended_date"),
        }
        if best is None or item["score"] > best["score"]:
            best = item
    return best


def _slot_upgrade_context(conn, account_id, signal, positions, asof_day):
    """Explain whether a full strategy slot can be upgraded by a candidate.

    This is deliberately an explanation/queue helper, not a way around T+1
    or the shared hard cap.  The actual sell still happens only in
    ``monitor_risk`` after a fresh exit quote, lot availability and the same
    score comparison have been checked.
    """
    cycle = _active_cycle(conn)
    count_budget = _dynamic_position_limits(conn)
    counts = {
        key: sum(
            1 for item in positions
            if item.get("account_id") == key and int(_num(item.get("qty"))) >= LOT_SIZE
        )
        for key in count_budget.get("limits", {})
    }
    target_limit = int(_num(count_budget.get("limits", {}).get(account_id), 0))
    donors = []
    for donor_id, donor_limit_raw in count_budget.get("limits", {}).items():
        if donor_id == account_id:
            continue
        donor_limit = int(_num(donor_limit_raw))
        donor_count = int(counts.get(donor_id, 0))
        # A donor may give only an unused slot and must retain the normal
        # three-position minimum.  This is an actual allocation transfer,
        # not an exception that raises the shared 15-slot hard cap.
        donor_floor = max(STRATEGY_MIN_POSITIONS, donor_count)
        if donor_limit > donor_floor and donor_count < donor_limit:
            donors.append({
                "account_id": donor_id,
                "limit": donor_limit,
                "count": donor_count,
                "remaining_after": donor_limit - 1,
            })
    pool_limit = int(_num(count_budget.get("pool_limit"), SHARED_POOL_MAX_POSITIONS))
    pending_slots = _pending_position_slots(conn, positions)
    occupied_pool = {
        (str(item.get("account_id")), str(item.get("code")))
        for item in positions if int(_num(item.get("qty"))) >= LOT_SIZE
    } | pending_slots
    # When every strategy has reached its local allocation but the effective
    # pool still has free seats (for example 8 occupied out of a 12-seat
    # risk-reduced pool), lend one of those unallocated seats directly.
    if len(occupied_pool) < pool_limit:
        donors.append({
            "account_id": "shared_pool",
            "limit": pool_limit,
            "count": len(occupied_pool),
            "remaining_after": len(occupied_pool),
            "unused_pool_slots": pool_limit - len(occupied_pool),
        })
    borrow_candidate = _replacement_score_from_signal(signal)
    borrow_ready = bool(
        target_limit < STRATEGY_MAX_POSITIONS
        and donors
        and borrow_candidate >= SLOT_BORROW_MIN_CANDIDATE_SCORE
    )
    candidate_score = _replacement_score_from_signal(signal)
    held = [
        item for item in positions
        if item.get("account_id") == account_id and int(_num(item.get("qty"))) >= LOT_SIZE
    ]
    weakest = None
    for position in held:
        review = conn.execute(
            """SELECT score,action FROM paper_position_reviews
               WHERE cycle_id=? AND account_id=? AND code=?
               ORDER BY id DESC LIMIT 1""",
            (cycle["id"], account_id, str(position.get("code") or "")),
        ).fetchone()
        score = _num(review["score"], 100.0) if review else 100.0
        item = {
            "code": str(position.get("code") or ""),
            "name": position.get("name") or position.get("code"),
            "score": score,
            "hold_days": _hold_days(position, asof_day),
            "available_qty": int(_num(position.get("available_qty"))),
            "review_action": review["action"] if review else None,
        }
        if weakest is None or item["score"] < weakest["score"]:
            weakest = item
    if weakest is None:
        return {
            "candidate_score": candidate_score,
            "borrow_candidate_score": borrow_candidate,
            "borrow_ready": borrow_ready,
            "donors": donors,
            "eligible": borrow_ready,
            "reason": (
                f"候选 {borrow_candidate:.1f} 分可从 {donors[0]['account_id']} 借用一个未使用席位"
                if borrow_ready else "暂无可比较的存量持仓"
            ),
        }
    edge = candidate_score - weakest["score"]
    pool_donor_available = any(item.get("account_id") == "shared_pool" for item in donors)
    borrow_ready = bool(
        target_limit < STRATEGY_MAX_POSITIONS
        and donors
        and borrow_candidate >= SLOT_BORROW_MIN_CANDIDATE_SCORE
        and (pool_donor_available or edge >= SLOT_BORROW_MIN_EDGE)
    )
    urgent = bool(
        candidate_score >= SLOT_UPGRADE_MIN_CANDIDATE_SCORE
        and edge >= SLOT_UPGRADE_MIN_EDGE
        and weakest["score"] <= POSITION_REVIEW_EXIT_SCORE
    )
    net_edge = edge - POSITION_REPLACEMENT_EXECUTION_BUFFER
    at_dynamic_limit = len(held) >= target_limit
    full_slot_ready = bool(
        at_dynamic_limit
        and net_edge >= POSITION_FULL_CAP_REPLACEMENT_EDGE
        and weakest["score"] < POSITION_FULL_CAP_MAX_SCORE
    )
    regular_upgrade_ready = bool(
        net_edge >= POSITION_REVIEW_REPLACEMENT_EDGE
        and (
            weakest["score"] <= POSITION_REVIEW_ANY_REPLACE_SCORE
            or (
                weakest.get("review_action") in {"watch", "reduce", "exit"}
                and weakest["score"] < POSITION_REVIEW_REPLACE_SCORE
            )
        )
    )
    upgrade_ready = bool(urgent or full_slot_ready or regular_upgrade_ready)
    min_hold_days = _replacement_min_hold_days(account_id)
    if weakest["available_qty"] < LOT_SIZE:
        state = "t1_locked"
        reason = (
            f"高分替补 {candidate_score:.1f} 分，现有最弱仓 {weakest['name']} "
            f"{weakest['score']:.1f} 分，分差 {edge:.1f}；最弱仓受 T+1 锁定，"
            "保留为优先替补，最早可卖后自动复核"
        )
    elif upgrade_ready and weakest["hold_days"] < min_hold_days and not urgent:
        state = "observe"
        reason = (
            f"候选 {candidate_score:.1f} 分高于最弱仓 {weakest['score']:.1f} 分（+{edge:.1f}），"
            f"但最弱仓观察期仅 {weakest['hold_days']}/{min_hold_days} 日，"
            "继续观察以避免高频换手"
        )
    elif urgent:
        state = "urgent_upgrade"
        reason = (
            f"候选 {candidate_score:.1f} 分显著高于最弱仓 {weakest['name']} "
            f"{weakest['score']:.1f} 分（+{edge:.1f}），达到紧急择强换仓条件；"
            "等待下一次风控扫描按 T+1 和行情核验执行"
        )
    elif full_slot_ready or regular_upgrade_ready:
        state = "upgrade_ready"
        reason = (
            f"候选 {candidate_score:.1f} 分高于最弱仓 {weakest['name']} "
            f"{weakest['score']:.1f} 分（原始 +{edge:.1f}、成本缓冲后 +{net_edge:.1f}），"
            "进入择强换仓队列"
        )
    else:
        state = "edge_insufficient"
        reason = (
            f"候选 {candidate_score:.1f} 分较最弱仓 {weakest['score']:.1f} 分高 {edge:.1f}，"
            f"扣除执行缓冲后 {net_edge:.1f}，未达到满席净优势 "
            f"{POSITION_FULL_CAP_REPLACEMENT_EDGE:.1f} 分；继续候选重排，不占换仓队列"
        )
    return {
        "candidate_score": candidate_score, "weakest": weakest,
        "edge": round(edge, 2), "urgent": urgent,
        "borrow_candidate_score": borrow_candidate,
        "borrow_ready": borrow_ready,
        "donors": donors,
        "state": "slot_borrow_ready" if borrow_ready else state,
        "eligible": bool(borrow_ready or state in {"urgent_upgrade", "upgrade_ready"}),
        "reason": (
            f"候选 {borrow_candidate:.1f} 分达到借位条件，可从 {donors[0]['account_id']} "
            f"转入一个未使用席位；不突破总上限{SHARED_POOL_MAX_POSITIONS}"
            if borrow_ready else reason
        ),
    }


def _apply_slot_borrow(conn, account_id, upgrade, asof_day):
    """Atomically transfer one unused strategy slot to a strong candidate."""
    if not upgrade or not upgrade.get("borrow_ready") or not upgrade.get("donors"):
        return {"allowed": False, "reason": "未达到动态借位条件"}
    cycle = _active_cycle(conn)
    budget = _dynamic_position_limits(conn)
    limits = {key: int(_num(value)) for key, value in (budget.get("limits") or {}).items()}
    donor = next(
        (item for item in upgrade["donors"]
         if item.get("account_id") == "shared_pool"
         or limits.get(item.get("account_id"), 0) == int(_num(item.get("limit")))),
        None,
    )
    account_slot_cap = 3 if account_id == MAIN_FORCE_STRATEGY_ID else STRATEGY_MAX_POSITIONS
    if donor is None or limits.get(account_id, 0) >= account_slot_cap:
        return {"allowed": False, "reason": "借位名额已被其他并发下单占用"}
    donor_id = donor["account_id"]
    if donor_id == "shared_pool":
        before = dict(limits)
        limits[account_id] += 1
        version_text = str(budget.get("allocation_version", "slots-v0"))
        try:
            version_id = int(version_text.rsplit("v", 1)[-1])
        except (TypeError, ValueError):
            version_id = 0
        row = conn.execute(
            "SELECT id,inputs FROM paper_position_limit_versions WHERE id=? AND cycle_id=?",
            (version_id, cycle["id"]),
        ).fetchone()
        if row is None:
            return {"allowed": False, "reason": "未找到当前席位版本，暂不借位"}
        inputs = _loads(row["inputs"], {})
        event = {
            "at": _now(), "account_id": account_id, "from": donor_id,
            "candidate_score": round(_num(upgrade.get("borrow_candidate_score")), 2),
            "limits_before": before, "limits_after": limits,
            "pool_free_before": int(_num(donor.get("unused_pool_slots"))),
        }
        history = list(inputs.get("slot_borrow_events") or [])[-9:]
        history.append(event)
        inputs["slot_borrow_events"] = history
        inputs["last_slot_borrow"] = event
        conn.execute(
            "UPDATE paper_position_limit_versions SET limits=?,inputs=?,source=?,effective_at=? WHERE id=?",
            (_json(limits), _json(inputs), "versioned_runtime_active_risk_budget+pool_slot_borrow", _now(), row["id"]),
        )
        return {
            "allowed": True, "from_account": donor_id, "to_account": account_id,
            "limits_before": before, "limits_after": limits,
            "allocation_version": budget.get("allocation_version"),
            "reason": "从共享池未使用动态席位借用1个席位",
        }
    donor_count = sum(
        1 for item in _position_rows(conn)
        if item.get("account_id") == donor_id and int(_num(item.get("qty"))) >= LOT_SIZE
    )
    if limits[donor_id] - 1 < max(STRATEGY_MIN_POSITIONS, donor_count):
        return {"allowed": False, "reason": "出让策略已达到最小保留席位"}
    before = dict(limits)
    limits[donor_id] -= 1
    limits[account_id] += 1
    # The schema stores the numeric row id as the allocation version; locate
    # the active row by the version returned from _dynamic_position_limits.
    version_text = str(budget.get("allocation_version", "slots-v0"))
    try:
        version_id = int(version_text.rsplit("v", 1)[-1])
    except (TypeError, ValueError):
        version_id = 0
    row = conn.execute(
        "SELECT id,inputs FROM paper_position_limit_versions WHERE id=? AND cycle_id=?",
        (version_id, cycle["id"]),
    ).fetchone()
    if row is None:
        return {"allowed": False, "reason": "未找到当前席位版本，暂不借位"}
    inputs = _loads(row["inputs"], {})
    event = {
        "at": _now(), "account_id": account_id, "from": donor_id,
        "candidate_score": round(_num(upgrade.get("borrow_candidate_score")), 2),
        "limits_before": before, "limits_after": limits,
    }
    history = list(inputs.get("slot_borrow_events") or [])[-9:]
    history.append(event)
    inputs["slot_borrow_events"] = history
    inputs["last_slot_borrow"] = event
    conn.execute(
        "UPDATE paper_position_limit_versions SET limits=?,inputs=?,source=?,effective_at=? WHERE id=?",
        (_json(limits), _json(inputs), "versioned_runtime_active_risk_budget+slot_borrow", _now(), row["id"]),
    )
    return {
        "allowed": True, "from_account": donor_id, "to_account": account_id,
        "limits_before": before, "limits_after": limits,
        "allocation_version": budget.get("allocation_version"),
        "reason": f"高分候选从 {donor_id} 借用1个未使用席位",
    }


def _rollback_slot_borrow(conn, borrow):
    """Undo a provisional seat transfer when the order does not fill."""
    if not borrow or not borrow.get("allowed") or borrow.get("rolled_back"):
        return {"allowed": False, "reason": "没有可回滚的借位"}
    cycle = _active_cycle(conn)
    version_text = str(borrow.get("allocation_version") or "slots-v0")
    try:
        version_id = int(version_text.rsplit("v", 1)[-1])
    except (TypeError, ValueError):
        version_id = 0
    row = conn.execute(
        "SELECT id,limits,inputs FROM paper_position_limit_versions WHERE id=? AND cycle_id=?",
        (version_id, cycle["id"]),
    ).fetchone()
    if row is None:
        return {"allowed": False, "reason": "当前借位版本不存在，无法自动回滚"}
    current = _loads(row["limits"], {})
    after = {key: int(_num(value)) for key, value in (borrow.get("limits_after") or {}).items()}
    before = {key: int(_num(value)) for key, value in (borrow.get("limits_before") or {}).items()}
    if after and current != after:
        return {"allowed": False, "reason": "借位版本已发生变化，保留当前并发调整"}
    inputs = _loads(row["inputs"], {})
    events = list(inputs.get("slot_borrow_events") or [])
    if events:
        events.pop()
    inputs["slot_borrow_events"] = events
    inputs["last_slot_borrow"] = events[-1] if events else None
    conn.execute(
        "UPDATE paper_position_limit_versions SET limits=?,inputs=?,source=?,effective_at=? WHERE id=?",
        (_json(before), _json(inputs), "versioned_runtime_active_risk_budget", _now(), row["id"]),
    )
    return {"allowed": True, "limits": before, "reason": "订单未成交，借用席位已回滚"}


def _main_force_intent(position, quote, market=None, news=None):
    """Estimate whether a sharp move is more consistent with washout or distribution.

    This is an explainable *risk signal*, not an observable fact.  It requires
    price/flow/volume evidence to agree before assigning either label; missing
    fields deliberately fall back to ``uncertain``.  The result is written to
    risk reviews and sell-plan payloads, but is not a standalone order trigger.
    """
    quote = quote or {}
    price = _num(quote.get("price"), None)
    pct = _num(quote.get("pct"), None)
    main_pct = _num(quote.get("main_pct"), _num(quote.get("main_net_pct"), None))
    super_net = _num(quote.get("super_net"), None)
    vol_ratio = _num(quote.get("vol_ratio"), None)
    high = _num(quote.get("high"), None)
    low = _num(quote.get("low"), None)
    open_price = _num(quote.get("open_price"), None)
    market_pct = _num((market or {}).get("live_index_pct"), None)
    relative = pct - market_pct if pct is not None and market_pct is not None else None
    range_pos = None
    if price is not None and high and low is not None and high > low:
        range_pos = max(0.0, min(1.0, (price - low) / (high - low)))
    negative_news = bool(_negative_hits(news or [], str(position.get("code") or "")))

    distribution = 0.0
    washout = 0.0
    evidence = []
    missing = []
    if pct is None:
        missing.append("当日涨跌幅")
    elif pct <= -4:
        distribution += 28; washout += 14; evidence.append(f"当日跌幅 {pct:+.2f}%")
    elif pct <= -2:
        distribution += 20; washout += 12; evidence.append(f"当日跌幅 {pct:+.2f}%")
    elif pct < 0:
        distribution += 8; washout += 8; evidence.append(f"当日小幅回落 {pct:+.2f}%")
    if main_pct is None:
        missing.append("主力净流入占比")
    elif main_pct <= -4:
        distribution += 32; evidence.append(f"主力净流入占比 {main_pct:+.2f}%")
    elif main_pct <= -2:
        distribution += 22; evidence.append(f"主力净流入占比 {main_pct:+.2f}%")
    elif main_pct >= 1:
        washout += 28; evidence.append(f"主力仍为净流入 {main_pct:+.2f}%")
    elif main_pct >= -1:
        washout += 18; evidence.append(f"主力流出有限 {main_pct:+.2f}%")
    else:
        washout += 8
    if super_net is not None:
        if super_net < 0:
            distribution += 6
        else:
            washout += 5
    else:
        missing.append("超大单资金")
    if vol_ratio is None:
        missing.append("量比")
    elif vol_ratio >= 1.5:
        distribution += 12; washout += 10; evidence.append(f"量比 {vol_ratio:.2f}")
    elif vol_ratio >= 1.1:
        distribution += 5; washout += 6
    if range_pos is not None:
        if range_pos <= 0.35:
            distribution += 14; evidence.append("收盘靠近日内低位")
        elif range_pos >= 0.60:
            washout += 18; evidence.append("下探后收复日内低位")
    else:
        missing.append("日内高低价")
    if open_price is not None and price is not None and price < open_price:
        distribution += 4
    if relative is not None:
        if relative <= -3:
            distribution += 12; evidence.append(f"相对沪深300弱 {relative:+.2f}%")
        elif relative >= -1:
            washout += 9
    if negative_news:
        distribution += 16; evidence.append("发现负面公告/舆情")
    elif news is not None:
        washout += 5

    distribution = round(min(100.0, distribution), 1)
    washout = round(min(100.0, washout), 1)
    usable = 1.0 - min(len(set(missing)) / 5.0, 0.8)
    gap = abs(distribution - washout)
    confidence = round(min(0.98, (0.50 + gap / 100.0) * usable), 2)
    if distribution >= 60 and distribution - washout >= 12 and confidence >= 0.58:
        classification, label, hint = "distribution", "疑似出货", "冻结加仓，优先复核可卖仓位"
    elif washout >= 58 and washout - distribution >= 10 and confidence >= 0.58:
        classification, label, hint = "washout", "疑似洗盘", "不因单日下跌单独清仓，等待承接确认"
    else:
        classification, label, hint = "uncertain", "意图不确定", "不得据此单独下单"
    return {
        "classification": classification,
        "label": label,
        "confidence": confidence,
        "distribution_score": distribution,
        "washout_score": washout,
        "action_hint": hint,
        "evidence": evidence[:8],
        "missing": sorted(set(missing)),
        "relative_to_market_pct": round(relative, 2) if relative is not None else None,
        "range_position": round(range_pos, 3) if range_pos is not None else None,
        "asof": quote.get("quote_at"),
        "model": "main_force_intent_v1_shadow",
    }


def _bought_today(position, asof_day=None):
    """整仓都是当日买入（同日新仓）才适用"买入后峰值"口径。

    2026-08-31 复核 P1：同日新仓在买入后不到一分钟就被按买入前的日内
    最高价算回撤（如开盘 3.62 冲高、3.40 才买入，立刻得到 6%+ 假回撤
    预警）。同日新仓的 peak 只从买入后的采样价起记录；次日恢复完整
    日内最高价口径。部分加仓的老仓仍用完整口径（老仓峰值合法）。
    """
    today = _date(asof_day or dt.date.today()).isoformat()
    qty = int(_num(position.get("qty")))
    today_qty = int(_num(position.get("today_acquired_qty")))
    return (
        qty > 0 and today_qty >= qty
        and str(position.get("entry_date") or "") == today
    )


def _position_peak(position, quote, price, asof_day=None):
    """统一峰值口径：同日新仓不吸收买入前的日内 high。"""
    if _bought_today(position, asof_day):
        return max(_num(position.get("peak_price"), 0.0), price or 0.0)
    return max(
        _num(position.get("peak_price"), 0.0),
        _num(quote.get("high"), 0.0), price or 0.0,
    )


def _intraday_downside_guard(position, quote, market=None, news=None, policy_override=None,
                             flow_trajectory=None):
    """Combine intraday weakness and main-force intent into a staged guard.

    ``warning`` is informational/freeze-add territory.  ``partial`` and
    ``full`` are candidates for a sell only after the caller confirms the
    signal on a subsequent scan.  Washout evidence suppresses a sell unless
    a severe loss/negative-event condition is also present.
    """
    account_id = str(position.get("account_id") or "")
    policy = dict(INTRADAY_DOWNSIDE_POLICIES.get(account_id) or {})
    if policy_override:
        for key in policy:
            candidate_key = f"downside_{key}"
            if candidate_key in policy_override and policy_override[candidate_key] is not None:
                policy[key] = policy_override[candidate_key]
    if not policy:
        return {"level": "none", "sell_ratio": 0.0, "reason": "无对应策略下跌防线"}
    quote = quote or {}
    pct = _num(quote.get("pct"), None)
    price = _num(quote.get("price"), None)
    cost = _num(position.get("cost"), None)
    ret_pct = (price / cost - 1) * 100 if price and cost else None
    market_pct = _num((market or {}).get("live_index_pct"), None)
    relative = pct - market_pct if pct is not None and market_pct is not None else None
    peak = _position_peak(position, quote, price)
    peak_retrace = (1 - price / peak) * 100 if price and peak else None
    peak_return = (peak / cost - 1) * 100 if peak and cost else None
    giveback = peak_return - ret_pct if peak_return is not None and ret_pct is not None else None
    intent = _main_force_intent(position, quote, market=market, news=news)
    distribution = intent.get("classification") == "distribution" and intent.get("confidence", 0) >= 0.58
    washout = intent.get("classification") == "washout" and intent.get("confidence", 0) >= 0.58
    negative_news = bool(_negative_hits(news or [], str(position.get("code") or "")))
    warning = bool(
        (pct is not None and pct <= policy["warning_pct"])
        or (relative is not None and relative <= policy["relative_pct"])
        or (peak_retrace is not None and peak_retrace >= policy["peak_retrace_pct"])
    )
    # Intraday-T must not dump inventory during a low-open/high-go recovery.
    # A quote that has reclaimed the morning low by >=1.5% with a non-negative
    # tape is treated as a washout/recovery unless independent distribution
    # evidence is present.  Hard stops remain handled by _sell_plan.
    day_low = _num(quote.get("low"), None)
    low_rebound = ((price / day_low - 1.0) * 100) if price and day_low and day_low > 0 else None
    recovery_hold = bool(
        account_id == "tq_breakout"
        and low_rebound is not None and low_rebound >= 1.5
        and pct is not None and pct >= -1.0
        and not distribution and not negative_news
    )
    if recovery_hold:
        warning = False
    flow_trajectory = dict(flow_trajectory or {})
    main_force_distribution = bool(
        position.get("account_id") == MAIN_FORCE_STRATEGY_ID
        and distribution and flow_trajectory.get("status") == "ok"
        and _num(flow_trajectory.get("main_delta_5m"), 0.0) < 0
        and _num(flow_trajectory.get("positive_persistence_10m"), 1.0) < 0.50
    )
    # Distribution needs both price weakness and an independent confirmation;
    # a large fall with positive/neutral flow is treated as possible washout.
    partial = bool(
        distribution
        and pct is not None and pct <= policy["partial_pct"]
        and (ret_pct is None or ret_pct <= 0 or (relative is not None and relative <= policy["relative_pct"]))
    )
    if main_force_distribution:
        partial = True
    severe = bool(
        distribution
        and pct is not None and pct <= policy["full_pct"]
        and (ret_pct is None or ret_pct <= 0)
        and peak_retrace is not None
        and peak_retrace >= policy["peak_retrace_pct"]
    )
    if main_force_distribution:
        severe = True
    # A position can lose its entire accumulated edge before the daily loss
    # threshold is reached.  Treat that as a separate, auditable risk path:
    # washout evidence may suppress the ordinary intent-based sell, but it
    # must not suppress protection of a meaningful peak-to-current giveback.
    giveback_partial = bool(
        giveback is not None
        and peak_return is not None
        and peak_return >= policy.get("giveback_min_peak_return_pct", 4.0)
        and giveback >= policy.get("giveback_partial_pct", 6.0)
        and (ret_pct is None or ret_pct <= 1.0)
        and (pct is None or pct <= 0.0)
    )
    giveback_severe = bool(
        giveback_partial
        and giveback >= policy.get("giveback_full_pct", 10.0)
        and (ret_pct is None or ret_pct <= -3.0)
    )
    if washout and not negative_news:
        # 洗盘只是在风险尚可承受时延迟减仓，不能覆盖各策略自己的
        # 亏损保护线。短线T容忍区最窄，趋势波段最宽，板块轮动居中。
        washout_override = _num(
            (STRATEGY_RISK_BEHAVIORS.get(account_id) or {}).get(
                "washout_loss_override_pct"
            ),
            -4.0,
        )
        loss_beyond_override = (
            ret_pct is not None and ret_pct <= washout_override
        ) or (
            relative is not None and relative <= policy["relative_pct"] - 1.0
        )
        if not loss_beyond_override:
            partial = False
            severe = False
    warning_trim_ratio = _num(policy.get("warning_trim_ratio"), 0.0)
    if giveback_severe or severe:
        level, sell_ratio = "full", 1.0
    elif giveback_partial or partial:
        level, sell_ratio = "partial", policy["partial_ratio"]
    elif warning:
        level, sell_ratio = "warning", warning_trim_ratio
    else:
        level, sell_ratio = "none", 0.0
    reasons = []
    if main_force_distribution:
        reasons.append("超强主力出货共振：意图分类为疑似出货，5分钟资金转负且10分钟持续率低于50%")
    if pct is not None and pct <= policy["warning_pct"]:
        reasons.append(f"当日跌幅 {pct:+.2f}%")
    if relative is not None and relative <= policy["relative_pct"]:
        reasons.append(f"相对沪深300弱 {relative:+.2f}%")
    if peak_retrace is not None and peak_retrace >= policy["peak_retrace_pct"]:
        reasons.append(f"盘中高点回撤 {peak_retrace:.2f}%")
    if giveback_partial:
        reasons.append(
            f"收益回吐保护：峰值收益 {peak_return:.2f}%、已回吐 {giveback:.2f}%"
        )
    reasons.append(intent.get("label") or "主力意图不确定")
    return {
        "level": level,
        "sell_ratio": sell_ratio,
        "reason": "；".join(reasons) if reasons else "未达到下跌预警条件",
        "policy": policy,
        "main_force_intent": intent,
        "intraday_pct": round(pct, 2) if pct is not None else None,
        "cost_return_pct": round(ret_pct, 2) if ret_pct is not None else None,
        "relative_to_market_pct": round(relative, 2) if relative is not None else None,
        "peak_retrace_pct": round(peak_retrace, 2) if peak_retrace is not None else None,
        "peak_return_pct": round(peak_return, 2) if peak_return is not None else None,
        "giveback_pct": round(giveback, 2) if giveback is not None else None,
        "giveback_protection": bool(giveback_partial or giveback_severe),
        "strategy_risk_behavior": STRATEGY_RISK_BEHAVIORS.get(account_id, {}),
        "negative_news": negative_news,
        "flow_trajectory": flow_trajectory,
        "main_force_distribution": main_force_distribution,
        "recovery_hold": recovery_hold,
        "recovery_hold_reason": (
            f"低开高走：较日内低点反弹 {low_rebound:.2f}%，暂缓日内预警减仓"
            if recovery_hold else None
        ),
        # 日内T的预警首段减仓是一次性的轻仓保护；趋势/板块仍维持
        # 两次确认后才执行的 partial/full 机制。
        "requires_confirmation": level in {"partial", "full"},
        "model": "intraday_downside_guard_v1",
        "asof": quote.get("quote_at"),
    }


def _downside_confirmed(conn, account_id, code, asof_day, guard):
    """Require a distinct prior five-minute scan with the same adverse intent."""
    # 2026-09-03 二次确认减仓：warning 级也允许走两次确认。首段减仓
    # 一天只有一次，之后价格长期停在 warning 区间（跌不深但持续阴跌、
    # 主力意图反复读出疑似出货）时，两次确认是仅存的保护通道，
    # 不能再被级别门槛直接挡掉。
    if not guard or guard.get("level") not in {"partial", "full", "warning"}:
        return False
    rows = conn.execute(
        """SELECT decision,payload,created_at FROM paper_risk_decisions
           WHERE account_id=? AND code=? AND side='sell'
             AND substr(created_at,1,10)=?
             AND decision IN ('downside_warning','downside_partial_pending','downside_full_pending')
           ORDER BY id DESC LIMIT 2""",
        (account_id, code, _date(asof_day).isoformat()),
    ).fetchall()
    if not rows:
        return False
    latest_row = rows[0]
    latest = _loads(latest_row["payload"], {})
    prior_guard = latest.get("downside_guard") or {}
    prior_class = ((prior_guard.get("main_force_intent") or {}).get("classification"))
    current_class = ((guard.get("main_force_intent") or {}).get("classification"))
    prior_level = prior_guard.get("level")
    current_time = str(guard.get("asof") or "")
    prior_time = str(prior_guard.get("asof") or latest_row["created_at"] or "")
    try:
        current_at = dt.datetime.fromisoformat(current_time.replace("Z", "+00:00"))
        prior_at = dt.datetime.fromisoformat(prior_time.replace("Z", "+00:00"))
        if bool(current_at.tzinfo) != bool(prior_at.tzinfo):
            return False
        if (current_at - prior_at).total_seconds() < max(60, INTRADAY_INTERVAL_MINUTES * 60 - 30):
            return False
    except (TypeError, ValueError, OverflowError):
        # A missing timestamp must fail closed; two concurrent scheduler calls
        # must never be mistaken for two independent confirmations.
        return False
    # Giveback protection is intent-independent by design (see the audit note
    # above): a position that gave back a large accumulated edge while the
    # main-force classifier returned "uncertain" (e.g. missing flow fields)
    # used to make this confirmation unreachable, so the protective exit could
    # never fire until the hard stop.  Two consecutive scans both carrying the
    # giveback flag are still required — the spacing check above already
    # guarantees they are distinct observations.
    giveback_today = bool(
        conn.execute(
            ("SELECT 1 FROM paper_risk_decisions "
             "WHERE account_id=? AND code=? AND side='sell' "
             "AND substr(created_at,1,10)=? "
             "AND decision IN ('downside_warning','downside_partial_pending','downside_full_pending') "
             "AND json_extract(payload,'$.downside_guard.giveback_protection') "
             "    IN (1,'1','true','True') "
             "LIMIT 1"),
            (account_id, code, _date(asof_day).isoformat()),
        ).fetchone()
    )
    # 2026-09-03 sticky fix: giveback protection is a persistent intraday
    # condition.  Once it has fired in ANY earlier scan today it stays valid
    # for the rest of the day, so a minor bounce flipping the live flag back
    # to 0 can no longer make the protective confirmation unreachable (the
    # position used to drift all the way to the hard stop instead).
    giveback_confirmed = (
        bool(guard.get("giveback_protection")) or giveback_today
    ) and (
        bool(prior_guard.get("giveback_protection")) or giveback_today
    )
    confirmed_intent = (
        prior_class == current_class == "distribution" or giveback_confirmed
    )
    return prior_level in {"warning", "partial", "full"} and confirmed_intent


def _position_quality_score(conn, position, quote, asof_day, news=None, replacement=None, nav=None, market=None,
                            flow_trajectory=None):
    """Score an existing holding for concentration decisions (0..100).

    The score is intentionally independent from the entry gate: it combines
    the position's actual return, live momentum/flow, completed-kline trend,
    the original model score and verified negative-event pressure.  A high
    score may remain as a one-lot core/observation holding; a low score is
    eligible for rotation only after T+1 and quote gates pass.
    """
    code = str(position.get("code") or "")
    account_id = position.get("account_id")
    price = _num(quote.get("price"), _num(position.get("cost")))
    cost = _num(position.get("cost"))
    ret_pct = (price / cost - 1) * 100 if price > 0 and cost > 0 else 0.0
    pct = _num(quote.get("pct"))
    main_pct = _num(quote.get("main_pct"), _num(quote.get("main_net_pct")))
    vol_ratio = _num(quote.get("vol_ratio"), 1.0)
    turnover = _num(quote.get("turnover"), 0.0)
    momentum = max(0.0, min(100.0, 50.0 + pct * 4.0 + (vol_ratio - 1.0) * 12.0))
    flow = max(0.0, min(100.0, 50.0 + main_pct * 4.0))
    return_score = max(0.0, min(100.0, 50.0 + ret_pct * 3.0))

    trend = 50.0
    trend_detail = "趋势数据不足"
    frame = _completed_kline(code, asof_day, inclusive=False)
    if frame is not None and not frame.empty and "close" in frame.columns:
        close_series = pd.to_numeric(frame["close"], errors="coerce").dropna()
        if not close_series.empty:
            last_close = float(close_series.iloc[-1])
            ma20 = float(close_series.tail(20).mean()) if len(close_series) >= 20 else None
            ma60 = float(close_series.tail(60).mean()) if len(close_series) >= 60 else None
            checks = []
            if ma20:
                checks.append(last_close >= ma20)
            if ma60:
                checks.append(last_close >= ma60)
            if ma20 and ma60:
                checks.append(ma20 >= ma60)
            trend = 50.0 + sum(20.0 if item else -20.0 for item in checks)
            trend = max(0.0, min(100.0, trend))
            trend_detail = f"收盘/MA20/MA60结构 {sum(checks)}/{len(checks)}"

    model_score = 50.0
    signal = conn.execute(
        """SELECT rank_score,t_score,payload FROM paper_signals
           WHERE account_id=? AND code=? ORDER BY signal_date DESC,id DESC LIMIT 1""",
        (account_id, code),
    ).fetchone()
    if signal:
        payload = _loads(signal["payload"], {})
        entry = (payload.get("decision") or {}).get("entry_model") or {}
        model_score = max(
            _score100(signal["t_score"], 0.0),
            _score100(entry.get("score"), 0.0),
            _score100(signal["rank_score"], 0.0),
        )
    negative = _negative_hits(news or [], code)
    main_force_intent = _main_force_intent(position, quote, market=market, news=news)
    news_penalty = min(24.0, len(negative) * 12.0)
    # 限售解禁预警（P0）：未来30天大额解禁（≥3%流通）重扣，60天≥5%中扣。
    # 解禁是确定性的供给冲击，等价格反应再退出就晚了；数据源失败时扣0。
    lockup_penalty, lockup_desc = 0.0, None
    if AD is not None:
        try:
            lockup_penalty, lockup_desc = AD.lockup_penalty(code, asof_day=asof_day)
        except Exception:
            lockup_penalty, lockup_desc = 0.0, None
    # 龙虎榜信号（P1）：近7天上榜净买 +6（抢筹确认），净卖 -6（出货警示）。
    lhb_bonus, lhb_desc = 0.0, None
    if AD is not None:
        try:
            lhb_bonus, lhb_desc = AD.lhb_position_signal(code, asof_day=asof_day)
        except Exception:
            lhb_bonus, lhb_desc = 0.0, None
    # 筹码/杠杆信号（P2）：两融 ±4、大宗 -5/+3、股东户数 ±5。
    # 各自独立小幅度，合计最坏 -14，与 lockup/lhb 共同构成替代数据
    # 评分层；任一数据源失败该项为 0，不影响其余。
    margin_bonus, margin_desc, block_bonus, block_desc, holder_bonus, holder_desc = 0.0, None, 0.0, None, 0.0, None
    if AD is not None:
        try:
            margin_bonus, margin_desc = AD.margin_signal(code, asof_day=asof_day)
        except Exception:
            margin_bonus, margin_desc = 0.0, None
        try:
            block_bonus, block_desc = AD.block_trade_signal(code, asof_day=asof_day)
        except Exception:
            block_bonus, block_desc = 0.0, None
        try:
            holder_bonus, holder_desc = AD.holder_signal(code, asof_day=asof_day)
        except Exception:
            holder_bonus, holder_desc = 0.0, None
    alt_bonus = lhb_bonus + margin_bonus + block_bonus + holder_bonus
    alt_shadow_delta = alt_bonus - lockup_penalty
    weights = HOLDING_QUALITY_WEIGHTS.get(account_id, HOLDING_QUALITY_WEIGHTS["trend_pullback"])
    hold_days = _hold_days(position, asof_day)
    # A just-opened position has no completed holding-period evidence.  Keep
    # its entry model visible, but neutralise the slow daily-K component for
    # the first calendar trading day.  Hard stops, verified announcements and
    # T+1/limit gates remain fully active elsewhere in monitor_risk.
    review_phase = "建仓复核" if hold_days < 1 else "持仓复核"
    trend_for_score = 50.0 if hold_days < 1 else trend
    # Public-web alternative data remains shadow evidence until its
    # point-in-time coverage and out-of-sample attribution are validated.
    # It must not move automatic concentration/rotation thresholds yet.
    score = (
        model_score * weights["model"] + trend_for_score * weights["trend"]
        + flow * weights["flow"] + momentum * weights["momentum"]
        + return_score * weights["return"] - news_penalty
    )
    score = round(max(0.0, min(100.0, score)), 2)
    grade = "建仓复核" if hold_days < 1 else ("核心" if score >= 65 else "观察" if score >= 50 else "减仓" if score >= 40 else "淘汰")
    market_value = max(0.0, _num(position.get("qty")) * max(price, 0.0))
    nav = max(_num(nav), 0.0)
    position_pct = market_value / nav * 100 if nav else 0.0
    small = position_pct < POSITION_REVIEW_SMALL_PCT * 100
    one_lot = int(_num(position.get("qty"))) <= LOT_SIZE
    replacement_score = _num((replacement or {}).get("score"), None)
    replacement_edge = replacement_score - score if replacement_score is not None else None
    reasons = [
        f"模型 {model_score:.1f}", f"趋势 {trend_for_score:.1f}" + ("（新仓中性）" if hold_days < 1 else ""),
        f"资金 {flow:.1f}", f"动量 {momentum:.1f}",
        f"收益 {return_score:.1f}",
    ]
    if negative:
        reasons.append(f"负面事件 {len(negative)} 条")
    if lockup_penalty:
        reasons.append(f"影子·限售解禁 -{lockup_penalty:.0f}（{lockup_desc}）")
    if lhb_desc:
        reasons.append(f"影子·龙虎榜 {'+' if lhb_bonus >= 0 else ''}{lhb_bonus:.0f}（{lhb_desc}）")
    for _bonus, _desc, _label in (
        (margin_bonus, margin_desc, "两融"),
        (block_bonus, block_desc, "大宗"),
        (holder_bonus, holder_desc, "股东户数"),
    ):
        if _desc:
            reasons.append(f"影子·{_label} {'+' if _bonus >= 0 else ''}{_bonus:.0f}（{_desc}）")
    if one_lot:
        reasons.append("当前仅一手")
    if small:
        reasons.append(f"仓位仅 {position_pct:.2f}%")
    reasons.append(
        f"主力意图 {main_force_intent['label']}"
        f"（置信度 {main_force_intent['confidence']*100:.0f}%）"
    )
    flow_trajectory = dict(flow_trajectory or {})
    if flow_trajectory.get("status") == "ok":
        flow_direction = flow_trajectory.get("direction")
        divergence = "none"
        if pct > 0 and flow_direction == "outflow":
            divergence = "price_up_flow_out"
        elif pct < 0 and flow_direction == "inflow":
            divergence = "price_down_flow_in"
        flow_trajectory["price_flow_divergence"] = divergence
        labels = {"inflow": "流入", "outflow": "流出", "flat": "平稳"}
        reasons.append(
            f"影子·分钟资金 {labels.get(flow_direction, '未知')}，"
            f"5分钟主力变化 {float(flow_trajectory.get('main_delta_5m') or 0)/10000:.1f}万，"
            f"持续性 {float(flow_trajectory.get('positive_persistence_10m') or 0)*100:.0f}%"
        )
    return {
        "code": code, "account_id": account_id, "score": score, "grade": grade,
        "market_value": round(market_value, 2), "position_pct": round(position_pct, 2),
        "ret_pct": round(ret_pct, 2), "hold_days": hold_days,
        "small_position": bool(small), "one_lot": bool(one_lot),
        "review_phase": review_phase, "weights": weights,
        "model_score": round(model_score, 2), "trend_score": round(trend_for_score, 2),
        "trend_raw_score": round(trend, 2),
        "flow_score": round(flow, 2), "momentum_score": round(momentum, 2),
        "return_score": round(return_score, 2), "turnover": round(turnover, 2),
        "news_penalty": round(news_penalty, 2),
        "lockup_penalty": round(lockup_penalty, 2),
        "lockup_detail": lockup_desc,
        "lhb_bonus": round(lhb_bonus, 2),
        "lhb_detail": lhb_desc,
        "margin_bonus": round(margin_bonus, 2), "margin_detail": margin_desc,
        "block_bonus": round(block_bonus, 2), "block_detail": block_desc,
        "holder_bonus": round(holder_bonus, 2), "holder_detail": holder_desc,
        "alt_shadow_delta": round(alt_shadow_delta, 2),
        "alt_score_applied": False,
        "fund_flow_trajectory": flow_trajectory,
        "fund_flow_trajectory_applied": False,
        "trend_detail": trend_detail,
        "replacement": replacement, "replacement_score": replacement_score,
        "replacement_edge": round(replacement_edge, 2) if replacement_edge is not None else None,
        "main_force_intent": main_force_intent,
        "reasons": reasons,
    }


def _over_capacity_exit_candidates(conn, positions, reviews, account_map, asof_day):
    """Pick the weakest *sellable* positions when a strategy exceeds its cap.

    The cap controls the number of distinct stocks, not the number of lots.
    New purchases are blocked immediately; existing excess holdings are then
    reduced over ordinary risk passes, preserving T+1 and live-quote gates.
    """
    selected = {}
    count_budget = _dynamic_position_limits(conn)
    by_account = {}
    for position in positions:
        if int(_num(position.get("qty"))) >= LOT_SIZE:
            by_account.setdefault(position["account_id"], []).append(position)
    for account_id, items in by_account.items():
        account = account_map.get(account_id) or {}
        fallback = (ACCOUNT_SPECS.get(account_id) or {}).get("max_positions", 5)
        limit = max(1, int(count_budget["limits"].get(account_id, fallback)))
        excess = max(0, len(items) - limit)
        if not excess:
            continue
        eligible = [
            item for item in items
            if int(_num(item.get("available_qty"))) >= LOT_SIZE
        ]
        eligible.sort(key=lambda item: (
            _num((reviews.get((account_id, item["code"])) or {}).get("score"), 100.0),
            _num((reviews.get((account_id, item["code"])) or {}).get("market_value"), 0.0),
        ))
        for rank, item in enumerate(eligible[:excess], start=1):
            review = reviews.get((account_id, item["code"])) or {}
            selected[(account_id, item["code"])] = (
                f"策略持仓数 {len(items)}/{limit}（总上限 {count_budget['pool_limit']}），压缩超额持仓；"
                f"按质量评分排序第 {rank} 个（{_num(review.get('score')):.1f} 分）"
            )
    return selected


def _permission_scope_exit_candidates(conn, positions, reviews, quote_map, asof_day):
    """Choose at most one restricted holding per strategy and trading day.

    These are legacy positions which the user cannot trade on the configured
    account (STAR/BSE/ST).  They must never be reinforced.  Exits are gradual,
    auditable and still obey T+1, fresh-quote and limit-down execution gates.
    """
    selected = {}
    by_account = {}
    for position in positions:
        quote = (quote_map or {}).get(position.get("code"), {})
        scope = _security_scope(
            position.get("code"), quote.get("name") or position.get("name"),
            quote.get("risk_flag"),
        )
        if scope["allowed"] or int(_num(position.get("qty"))) < LOT_SIZE:
            continue
        item = dict(position)
        item["security_scope"] = scope
        by_account.setdefault(position["account_id"], []).append(item)
    priority = {"风险警示": 0, "北交所": 1, "科创板": 2}
    for account_id, items in by_account.items():
        completed_today = conn.execute(
            """SELECT COUNT(*) FROM paper_audit
               WHERE account_id=? AND event='permission_scope_exit'
                 AND substr(created_at,1,10)=?""",
            (account_id, _date(asof_day).isoformat()),
        ).fetchone()[0]
        remaining = max(0, PERMISSION_SCOPE_EXIT_MAX_PER_STRATEGY_DAY - int(completed_today or 0))
        if not remaining:
            continue
        items.sort(key=lambda item: (
            0 if int(_num(item.get("available_qty"))) >= LOT_SIZE else 1,
            priority.get(item["security_scope"].get("board"), 9),
            _num((reviews.get((account_id, item["code"])) or {}).get("score"), 100.0),
        ))
        for item in items[:remaining]:
            scope = item["security_scope"]
            selected[(account_id, item["code"])] = (
                f"{scope['reason']}；不在可交易权限范围，按每策略每日最多 "
                f"{PERMISSION_SCOPE_EXIT_MAX_PER_STRATEGY_DAY} 只逐步退出并释放席位"
            )
    return selected


def _concentration_action(review, position, quote_status, sells_used):
    """Decide whether a quality review should trigger a full rotation sell."""
    if not quote_status.get("fresh"):
        return "quote_pending", "行情未通过核验，暂不做集中换仓"
    if not review or review.get("score") is None:
        return "review_pending", "持仓评分尚未完成，暂不做集中换仓"
    if int(position.get("available_qty") or 0) < LOT_SIZE:
        return "t1_locked", "T+1 可卖份额不足，等待可卖后再评估"
    if sells_used >= POSITION_REVIEW_MAX_SELLS_PER_RUN:
        return "queued", "本轮集中换仓已达到最多三笔，顺延下一轮"
    score = _num(review.get("score"))
    raw_edge = _num(review.get("replacement_edge"), None)
    edge = (
        raw_edge - POSITION_REPLACEMENT_EXECUTION_BUFFER
        if raw_edge is not None else None
    )
    review["replacement_execution_buffer"] = POSITION_REPLACEMENT_EXECUTION_BUFFER
    review["replacement_net_edge"] = round(edge, 2) if edge is not None else None
    replacement_score = _num(review.get("replacement_score"), None)
    at_dynamic_limit = bool(review.get("at_dynamic_limit"))
    rotations_today = int(_num(review.get("rotations_today")))
    # The normal two-day observation window prevents churn.  It must not trap
    # a very weak holding when a materially stronger, freshly revalidated
    # replacement is waiting for its only slot.  This is still constrained by
    # T+1, fresh exit quote, total pool hard cap and the replacement buy gate.
    urgent_slot_upgrade = bool(
        replacement_score is not None
        and replacement_score >= SLOT_UPGRADE_MIN_CANDIDATE_SCORE
        and edge is not None and edge >= SLOT_UPGRADE_MIN_EDGE
        and score <= POSITION_REVIEW_EXIT_SCORE
    )
    if urgent_slot_upgrade:
        return "consolidation_exit", (
            f"紧急择强换仓：现仓 {score:.1f} 分，替补 {replacement_score:.1f} 分，"
            f"原始分差 {raw_edge:.1f}、扣除执行缓冲后 {edge:.1f}；"
            "豁免最短观察期但不豁免 T+1/行情/总池门禁"
        )
    min_hold_days = _replacement_min_hold_days(
        review.get("account_id") or position.get("account_id")
    )
    if review.get("hold_days", 0) < min_hold_days:
        return "new_position", (
            f"持仓观察期 {review.get('hold_days', 0)}/{min_hold_days} 日，"
            "暂不因评分换仓"
        )
    # Small residual lots can be consolidated at a moderate score.  A larger
    # holding is only rotated when its absolute quality is clearly weak; this
    # prevents broad churn while still removing a genuinely poor position.
    can_replace = (
        edge is not None and edge >= POSITION_REVIEW_REPLACEMENT_EDGE
        and (
            (review.get("small_position") and score < POSITION_REVIEW_REPLACE_SCORE)
            or score <= POSITION_REVIEW_ANY_REPLACE_SCORE
        )
    )
    full_slot_upgrade = (
        at_dynamic_limit
        and edge is not None and edge >= POSITION_FULL_CAP_REPLACEMENT_EDGE
        and score < POSITION_FULL_CAP_MAX_SCORE
    )
    # Swing positions need an independent observation before a quality-score
    # exit.  A single intraday/market-wide score can be noisy and was observed
    # to liquidate trend holdings one session before their rebound.  Hard
    # stops, downside guards and urgent slot upgrades are evaluated elsewhere
    # and still take precedence; this gate only affects concentration exits.
    if position.get("account_id") == "trend_pullback" and score <= POSITION_REVIEW_EXIT_SCORE:
        if not bool(review.get("quality_exit_confirmed")):
            return "watch", (
                f"趋势持仓评分 {score:.1f} 低于淘汰线，但尚无连续观察确认；"
                "保留观察，等待下一完整窗口或结构破坏"
            )
    if score <= POSITION_REVIEW_EXIT_SCORE:
        return "consolidation_exit", f"持仓质量评分 {score:.1f} 低于淘汰线 {POSITION_REVIEW_EXIT_SCORE:.0f}"
    if (can_replace or full_slot_upgrade) and rotations_today >= POSITION_ROTATION_MAX_PER_STRATEGY_DAY:
        return "queued", (
            f"本策略今日主动换仓已达 {rotations_today}/{POSITION_ROTATION_MAX_PER_STRATEGY_DAY} 上限，"
            "候选保留至下一轮/下一交易日"
        )
    if can_replace or full_slot_upgrade:
        if full_slot_upgrade:
            return "consolidation_exit", (
                f"动态席位已满，择强换股：现仓 {score:.1f} 分，"
                f"替补原始高 {raw_edge:.1f} 分，扣执行缓冲后仍高 {edge:.1f} 分"
            )
        if can_replace:
            return "consolidation_exit", (
                f"低质量小仓换仓：评分 {score:.1f}，后备候选原始高 {raw_edge:.1f} 分，"
                f"扣执行缓冲后高 {edge:.1f} 分"
            )
    if score < POSITION_REVIEW_REPLACE_SCORE:
        return "watch", f"评分 {score:.1f} 偏弱，尚无足够优势候选替换"
    return "hold", f"评分 {score:.1f}，保留并等待策略加仓确认"


def _save_position_review(conn, cycle_id, review, action, reason):
    replacement = review.get("replacement") or {}
    conn.execute(
        """INSERT INTO paper_position_reviews(
           cycle_id,account_id,code,review_date,score,grade,action,market_value,
           position_pct,replacement_code,replacement_score,reasons,detail,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cycle_id,account_id,code,review_date) DO UPDATE SET
             score=excluded.score,grade=excluded.grade,action=excluded.action,
             market_value=excluded.market_value,position_pct=excluded.position_pct,
             replacement_code=excluded.replacement_code,replacement_score=excluded.replacement_score,
             reasons=excluded.reasons,detail=excluded.detail,created_at=excluded.created_at""",
        (
            cycle_id, review["account_id"], review["code"], _date(review.get("review_date") or dt.date.today()).isoformat(),
            review["score"], review["grade"], action, review["market_value"], review["position_pct"],
            replacement.get("code"), review.get("replacement_score"),
            "；".join(review.get("reasons") or []),
            _json({**review, "action": action, "action_reason": reason}), _now(),
        ),
    )


def _rotation_buy_candidate(conn, account, replacement, quote, market, news, asof_day, *, all_quotes=None):
    """Re-enter one released slot with a previously deferred high-score signal.

    The normal buy path is deliberately reused so the replacement still goes
    through live quote validation, T+1/limit checks, the shared 82% cap and the
    strategy's own risk budget.  A failed replacement is recorded as an audit
    result; it never bypasses a gate merely because a sell just freed cash.
    """
    signal_id = replacement.get("signal_id") if replacement else None
    if not signal_id:
        return {"status": "no_candidate", "reason": "没有可复用的候选信号"}
    row = conn.execute(
        "SELECT * FROM paper_signals WHERE id=? AND status IN ('pending','deferred_capacity',?)",
        (signal_id, ENTRY_FROZEN_WAITLIST_STATUS),
    ).fetchone()
    if not row:
        return {"status": "candidate_expired", "reason": "候选已被其他流程处理"}
    result = _buy_order(
        conn, account, dict(row), quote or {}, market or {}, news or {}, _date(asof_day),
        all_quotes=dict(all_quotes or {}),
    )
    result = dict(result or {})
    result.update({
        "signal_id": int(signal_id),
        "rotation": True,
        "replacement_code": replacement.get("code"),
        "replacement_score": replacement.get("score"),
    })
    return result


def _sell_plan(position, quote, asof_day, news, hard_stop_touched_today=False):
    spec = ACCOUNT_SPECS[position["account_id"]]
    price = _num(quote.get("price"), 0)
    cost = _num(position["cost"], 0)
    # 与盘中守护同口径：峰值吸收当日 high，回撤不被 3 分钟采样间隙低估；
    # 同日新仓例外——只用买入后的采样价，不吸收买入前的日内高点。
    peak = _position_peak(position, quote, price)
    ret = price / cost - 1 if cost and price else None
    drawdown = 1 - price / peak if peak and price else None
    days = _hold_days(position, asof_day)
    reasons, sell_ratio, next_stage = [], 0.0, int(position.get("take_stage") or 0)
    exit_class, exit_reason_code = "none", None
    # P1 审计修复（2026-09-02）：卖出状态机的"当日已发生某类退出"判定
    # 改为读取订单 payload 里的稳定 ASCII 标记，不再对中文 reason 做
    # LIKE 匹配——文案措辞调整曾经会静默破坏跌破确认与级别去重。
    exit_marker = None
    if ret is None:
        return 0.0, "缺少有效报价", next_stage, {
            "strategy_id": position["account_id"],
            "risk_profile": spec.get("risk_profile"),
            "strategy_version": spec.get("strategy_version") or RISK_VERSION,
            "hold_days": days,
        }
    # P3 审计修复（S4）：退出类别按严重度固定优先级——旧实现顺序执行
    # 且后者覆盖前者，同时满足"硬止损+达到最长持有"的持仓被记为较弱的
    # max_hold，污染恢复观察与自进化样本的退出归因。
    _exit_severity = {"none": 0, "tactical_take_profit": 1, "max_hold": 2,
                      "trailing_stop": 3, "hard_stop": 4}
    def _set_exit(new_class, new_code):
        nonlocal exit_class, exit_reason_code
        if _exit_severity[new_class] > _exit_severity.get(exit_class, 0):
            exit_class, exit_reason_code = new_class, new_code
    if ret <= spec["hard_stop"]:
        # 2026-08-28 修复：盘中首次触碰硬止损且非崩盘形态时，不再立即
        # 全仓清掉——先按守卫 partial 比例减仓；后续扫描仍在线下、或当日
        # 已做过首段减仓、或已逼近跌停（崩盘形态）时才全清。避免把单针
        # 探底卖在最低点（2026-08-28 601212 案例：7.013 清仓后反弹 +7.6%）。
        limit_pct_now = _limit_pct(position["code"])
        pct_now = _num(quote.get("pct"), 0.0)
        crash_tape = pct_now <= -(limit_pct_now * 0.8)
        if crash_tape or hard_stop_touched_today:
            suffix = "崩盘形态" if crash_tape else "跌破确认"
            reasons.append(f"硬止损 {ret*100:.1f}%（{suffix}，全清）")
            sell_ratio = 1.0
            _set_exit("hard_stop", "hard_stop")
        else:
            guard_ratio = _num(
                (INTRADAY_DOWNSIDE_POLICIES.get(position["account_id"]) or {}).get("partial_ratio"), 0.35,
            )
            reasons.append(
                f"硬止损首段减仓：现距成本 {ret*100:.1f}% 触及止损线且非崩盘形态，"
                f"先减 {guard_ratio*100:.0f}%；后续扫描仍在线下将清仓"
            )
            sell_ratio = max(sell_ratio, guard_ratio)
            _set_exit("hard_stop", "hard_stop")
            exit_marker = "hard_stop_first_trim"
    if ret >= spec["trail_after"] and drawdown is not None and drawdown >= spec["trail_stop"]:
        reasons.append(f"移动止损，峰值回撤 {drawdown*100:.1f}%")
        sell_ratio = 1.0
        _set_exit("trailing_stop", "trailing_stop")
    if days >= spec["hold_max"]:
        reasons.append(f"达到最长持有 {days} 日")
        sell_ratio = 1.0
        _set_exit("max_hold", "max_hold")
    stages = spec["take_profit"]
    # P2 审计修复（2026-09-02）：跳空越档时单轮内连续消费所有已满足的
    # 止盈档位（累计比例，封顶 1.0）。旧实现每轮只消费一档，价格从 +5%
    # 直接跳到 +11% 时第二档要等下一个 3 分钟轮次，期间暴露于回撤。
    # 仍只在无更强退出（硬止损/移动止损/最长持有）时执行。
    if sell_ratio == 0:
        while next_stage < len(stages) and ret >= stages[next_stage][0]:
            sell_ratio = min(1.0, sell_ratio + stages[next_stage][1])
            next_stage += 1
            reasons.append(f"阶梯止盈 {ret*100:.1f}%")
            _set_exit("tactical_take_profit", "take_profit")
    shadow_news = _negative_hits(news, position["code"])
    main_force_intent = _main_force_intent(position, quote, news=news)
    exit_profile = {
        "strategy_id": position["account_id"],
        "risk_profile": spec.get("risk_profile"),
        "strategy_version": spec.get("strategy_version") or RISK_VERSION,
        "hold_min": spec.get("hold_min"),
        "hold_max": spec.get("hold_max"),
        "hard_stop": spec.get("hard_stop"),
        "trail_after": spec.get("trail_after"),
        "trail_stop": spec.get("trail_stop"),
        "take_profit": spec.get("take_profit"),
        "hard_stop_unchanged": True,
    }
    return sell_ratio, "；".join(reasons), next_stage, {
        "strategy_id": position["account_id"],
        "risk_profile": spec.get("risk_profile"),
        "strategy_version": spec.get("strategy_version") or RISK_VERSION,
        "exit_profile": exit_profile,
        "ret_pct": round(ret*100, 2),
        "drawdown_pct": round((drawdown or 0)*100, 2),
        "hold_days": days,
        "main_force_intent": main_force_intent,
        "shadow_news_warning_count": len(shadow_news),
        "shadow_news_notice": (
            "快讯关键词仅作影子提示，不自动卖出"
            if shadow_news else None
        ),
        "exit_class": exit_class,
        "exit_reason_code": exit_reason_code,
        "exit_marker": exit_marker,
        "protective_exit": exit_class in PROTECTIVE_EXIT_CLASSES,
        "volatility_shadow": _volatility_shadow(position["code"], asof_day, price),
    }


def _monitor_risk_impl(asof_date=None):
    """14:50 风控任务：仅监控当前持仓，卖出不受市场新开仓门禁影响。"""
    init_db()
    day = _date(asof_date)
    # Claim the minute before touching positions.  Pending manual buys are
    # deliberately processed only after this risk-exit pass has committed.
    # This covers overlap between the periodic intraday run and the dedicated
    # 14:50 risk slot without holding a database transaction across network
    # quote requests.
    scan_minute = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with _db(immediate=True) as conn:
        _assert_active_lease(conn, "risk scan marker")
        scan_marker = conn.execute(
            "SELECT event,detail FROM paper_audit WHERE event='risk_scan_state' "
            "AND detail LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%"scan_minute": "{scan_minute}"%',),
        ).fetchone()
        marker_detail = _loads(scan_marker["detail"], {}) if scan_marker else {}
        marker_started = marker_detail.get("started_at") if marker_detail else None
        marker_stale = False
        if marker_started:
            try:
                marker_stale = (
                    dt.datetime.now() - dt.datetime.fromisoformat(str(marker_started)[:19])
                ).total_seconds() > 15 * 60
            except (TypeError, ValueError):
                marker_stale = False
        marker_running = scan_marker and marker_detail.get("status") == "running"
        if scan_marker and (
            marker_detail.get("status") == "completed"
            or (marker_running and not marker_stale)
        ):
            return {
                "slot": "risk", "date": day.isoformat(), "orders": [],
                "manual_orders": [], "status": "already_scanned",
                "reason": f"{scan_minute} 风控扫描已由同一调度周期完成",
            }
        _audit(conn, None, "risk_scan_state", _json({
            "scan_minute": scan_minute, "status": "running", "attempt": 1,
            "started_at": _now(),
        }))
    manual_orders = []
    with _db() as snapshot_conn:
        running_ids = {row["id"] for row in _rows(snapshot_conn, "SELECT id FROM paper_accounts WHERE status='running'")}
        positions = [p for p in _position_rows(snapshot_conn, asof_day=day) if p["account_id"] in running_ids]
        market_context = _cached_close_market(snapshot_conn, day, allow_network=False)
        retry_placeholders = ",".join("?" for _ in ENTRY_RETRY_SIGNAL_STATUSES)
        candidate_rows = snapshot_conn.execute(
            f"SELECT DISTINCT code FROM paper_signals WHERE status IN ({retry_placeholders})",
            tuple(ENTRY_RETRY_SIGNAL_STATUSES),
        ).fetchall()
        candidate_codes = {str(row[0]) for row in candidate_rows if row[0]}
    if positions:
        codes = sorted({str(p["code"]) for p in positions} | candidate_codes)
        quote_map = _quotes(codes, asof_date=day)
        news = _news_for({p["code"]: p.get("name") or p["code"] for p in positions})
        # Current-only minute flow is fetched concurrently before opening the
        # SQLite write transaction.  It is shadow evidence and can never block
        # deterministic risk exits when the source is unavailable.
        try:
            flow_trajectory_map = AD.fund_flow_trajectories(codes, asof_day=day) if AD is not None else {}
        except Exception:
            flow_trajectory_map = {}
    else:
        quote_map, news, flow_trajectory_map = {}, [], {}
    if not positions:
        with _db(immediate=True) as conn:
            _sync_positions(conn, asof_day=day)
            _record_nav(conn, day, quotes=quote_map)
        try:
            manual_orders = process_pending_manual_orders(day)
        except Exception as exc:
            manual_orders = [{"status": "pending_batch_retry", "reason": str(exc)}]
        result = {"slot": "risk", "date": day.isoformat(), "orders": [], "manual_orders": manual_orders}
        with _db(immediate=True) as conn:
            _audit(conn, None, "risk_scan_state", _json({"scan_minute": scan_minute, "status": "completed", "finished_at": _now()}))
        return result
    with _db(immediate=True) as conn:
        running_ids = {row["id"] for row in _rows(conn, "SELECT id FROM paper_accounts WHERE status='running'")}
        positions = [p for p in _position_rows(conn, asof_day=day) if p["account_id"] in running_ids]
        cycle = _active_cycle(conn)
        account_map = {
            row["id"]: row for row in _rows(
                conn, "SELECT * FROM paper_accounts WHERE status='running'"
            )
        }
        _, pool_market_value, pool_nav, _, _ = _shared_account_exposure(conn, quote_map, day)
        held_by_account = {}
        for item in positions:
            held_by_account.setdefault(item["account_id"], set()).add(item["code"])
        count_budget = _dynamic_position_limits(conn)
        rotations_today = {
            account_id: conn.execute(
                """SELECT COUNT(*) FROM paper_audit
                   WHERE account_id=? AND event='quality_rotation'
                     AND substr(created_at,1,10)=?""",
                (account_id, day.isoformat()),
            ).fetchone()[0]
            for account_id in account_map
        }
        quality_reviews = {}
        for position in positions:
            replacement = _best_replacement_candidate(
                conn, position["account_id"], day,
                held_by_account.get(position["account_id"], set()),
            )
            review = _position_quality_score(
                conn, position, quote_map.get(position["code"], {}), day,
                news=news, replacement=replacement, nav=pool_nav,
                market=market_context,
                flow_trajectory=flow_trajectory_map.get(position["code"]),
            )
            review["review_date"] = day
            review["dynamic_position_limit"] = count_budget["limits"].get(position["account_id"], 5)
            review["strategy_position_count"] = len(held_by_account.get(position["account_id"], set()))
            review["at_dynamic_limit"] = (
                review["strategy_position_count"] >= review["dynamic_position_limit"]
            )
            review["pool_position_limit"] = count_budget["pool_limit"]
            review["rotations_today"] = int(rotations_today.get(position["account_id"], 0))
            quality_reviews[(position["account_id"], position["code"])] = review
        capacity_exit_reasons = _over_capacity_exit_candidates(
            conn, positions, quality_reviews, account_map, day,
        )
        permission_exit_reasons = _permission_scope_exit_candidates(
            conn, positions, quality_reviews, quote_map, day,
        )
        # Capacity compression and full-slot rotation must evaluate the weakest
        # holdings first; database/lot insertion order must never decide which
        # stock is sacrificed for a stronger candidate.
        positions.sort(key=lambda item: (
            0 if (item["account_id"], item["code"]) in permission_exit_reasons else 1,
            0 if (item["account_id"], item["code"]) in capacity_exit_reasons else 1,
            _num((quality_reviews.get((item["account_id"], item["code"])) or {}).get("score"), 100.0),
        ))
        concentration_sells_used = 0
        permission_sells_used = 0
        rotation_swaps_used_by_account = dict(rotations_today)
        rotation_bought_codes = set()
        rotation_results = []
        orders = []
        for position in positions:
            _assert_active_lease(conn, "risk position")
            quote = quote_map.get(position["code"], {})
            quote_status = _execution_quote_status(quote, day, purpose="exit")
            quality_review = quality_reviews.get((position["account_id"], position["code"])) or {}
            if position["account_id"] == "trend_pullback":
                previous_review = conn.execute(
                    """SELECT action FROM paper_position_reviews
                       WHERE cycle_id=? AND account_id=? AND code=? AND review_date < ?
                       ORDER BY review_date DESC LIMIT 1""",
                    (cycle["id"], position["account_id"], position["code"], day.isoformat()),
                ).fetchone()
                quality_review["quality_exit_confirmed"] = bool(
                    previous_review and previous_review["action"] in {
                        "watch", "consolidation_exit", "capacity_exit"
                    }
                )
            downside_guard = _intraday_downside_guard(
                position, quote, market=market_context, news=news,
                policy_override=_risk_profile(
                    account_map.get(position["account_id"]) or {"id": position["account_id"]}
                ),
                flow_trajectory=flow_trajectory_map.get(position["code"]),
            )
            permission_reason = permission_exit_reasons.get((position["account_id"], position["code"]))
            capacity_reason = capacity_exit_reasons.get((position["account_id"], position["code"]))
            quality_review["rotations_today"] = int(
                rotation_swaps_used_by_account.get(position["account_id"], 0)
            )
            if not quote_status["fresh"]:
                pending_ratio, pending_reason, _, pending_detail = _sell_plan(
                    position, quote, day, news
                )
                quality_action, quality_reason = _concentration_action(
                    quality_review, position, quote_status, concentration_sells_used,
                )
                if permission_reason:
                    quality_action = "permission_scope_exit_pending_quote"
                    quality_reason = f"{permission_reason}；{quote_status['reason']}，等待可执行行情"
                elif capacity_reason:
                    quality_action = "capacity_exit_pending_quote"
                    quality_reason = f"{capacity_reason}；{quote_status['reason']}，等待可执行行情"
                if downside_guard.get("level") != "none" and not permission_reason and not capacity_reason:
                    quality_action = "downside_pending_quote"
                    quality_reason = f"下跌{downside_guard['level']}：{downside_guard['reason']}；{quote_status['reason']}"
                _save_position_review(conn, cycle["id"], quality_review, quality_action, quality_reason)
                if pending_ratio > 0 or capacity_reason or permission_reason or downside_guard.get("level") != "none":
                    detail = {
                        **pending_detail,
                        "quote_status": quote_status,
                        "position_quality": quality_review,
                        "downside_guard": downside_guard,
                        "intended_sell_ratio": pending_ratio,
                    }
                    reason = quality_reason if (capacity_reason or permission_reason or downside_guard.get("level") != "none") else (
                        f"{pending_reason}；{quote_status['reason']}，风险未解除"
                    )
                    _risk_log(
                        conn,
                        position["account_id"],
                        position["code"],
                        "sell",
                        "exit_pending_data",
                        reason,
                        detail,
                    )
                    orders.append({
                        "code": position["code"],
                        "status": "exit_pending_data",
                        "reason": reason,
                    })
                continue
            price = _num(quote.get("price"), 0)
            # 峰值同时吸收当日 high：两轮 3 分钟扫描之间的冲高若不记入
            # peak，移动止损会系统性延迟触发（与日内下行守卫的口径一致）。
            # 同日新仓例外：只用买入后的采样价（2026-08-31 P1），
            # 避免买入前的高点立即制造虚假回撤预警。
            scan_peak = (
                price if _bought_today(position, day)
                else max(price, _num(quote.get("high"), 0.0))
            )
            if scan_peak > _num(position.get("peak_price"), 0):
                conn.execute("UPDATE paper_positions SET peak_price=? WHERE account_id=? AND code=?", (scan_peak, position["account_id"], position["code"]))
                position["peak_price"] = scan_peak
            downside_confirmed = _downside_confirmed(
                conn, position["account_id"], position["code"], day, downside_guard,
            )
            downside_guard["confirmed"] = downside_confirmed
            # P1 审计修复（2026-09-02）：主判定改用订单 payload 的稳定标记
            # exit_marker='hard_stop_first_trim'（由 _sell_plan 写入）；中文
            # reason LIKE 仅保留为当日旧订单（标记上线前写入）的同日兜底。
            hard_stop_touched_today = bool(conn.execute(
                """SELECT 1 FROM paper_orders
                   WHERE account_id=? AND code=? AND side='sell' AND status='filled'
                     AND substr(created_at,1,10)=?
                       AND (
                           json_extract(risk_payload,'$.exit_marker')='hard_stop_first_trim'
                           OR reason LIKE '%硬止损首段减仓%'
                       )
                   LIMIT 1""",
                (position["account_id"], position["code"], day.isoformat()),
            ).fetchone())
            ratio, reason, next_stage, detail = _sell_plan(
                position, quote, day, news, hard_stop_touched_today=hard_stop_touched_today,
            )
            quality_action, quality_reason = _concentration_action(
                quality_review, position, quote_status, concentration_sells_used,
            )
            if permission_reason:
                # Permissions exits have their own per-strategy daily quota.
                # Do not let three ordinary capacity/quality rotations defer a
                # position the account is no longer allowed to reinforce.
                quality_action, quality_reason = "permission_scope_exit", permission_reason
            elif capacity_reason and concentration_sells_used < POSITION_REVIEW_MAX_SELLS_PER_RUN:
                quality_action, quality_reason = "capacity_exit", capacity_reason
            concentration_triggered = quality_action in {
                "consolidation_exit", "capacity_exit", "permission_scope_exit",
            }
            if concentration_triggered:
                # A quality rotation is a full exit, even when the ordinary
                # ladder would only take a partial profit.  Otherwise a
                # low-quality one-lot can remain indefinitely after the first
                # partial sell and defeat the purpose of concentration.
                if quality_action == "permission_scope_exit":
                    ratio = max(ratio, PERMISSION_SCOPE_EXIT_RATIO)
                    detail["permission_scope_exit"] = {
                        "tranche_ratio": PERMISSION_SCOPE_EXIT_RATIO,
                        "daily_limit": PERMISSION_SCOPE_EXIT_MAX_PER_STRATEGY_DAY,
                    }
                    reason = (
                        quality_reason if not reason else
                        f"{quality_reason}；与常规风险卖出取较高比例"
                    )
                else:
                    ratio = 1.0
                    reason = quality_reason if ratio <= 0 or not reason else f"{quality_reason}；覆盖常规卖出比例"
                    # 集中轮换是全退，覆盖 _sell_plan 可能带出的首段减仓标记，
                    # 避免"硬止损首段减仓已发生"的状态被全退订单误报。
                    detail["exit_marker"] = "concentration_exit"
                detail["position_quality"] = quality_review
                detail["concentration_review"] = True
            elif ratio > 0:
                quality_action = "risk_exit"
                detail["position_quality"] = quality_review
            detail["downside_guard"] = downside_guard
            # P1 审计修复（2026-09-02）：预警/守卫减仓的当日去重同样改用
            # 结构化标记（见下方 guard_actionable 分支写入 detail["exit_marker"]），
            # 中文 LIKE 仅作标记上线前旧订单的同日兜底。
            warning_trimmed = bool(conn.execute(
                """SELECT 1 FROM paper_orders
                   WHERE account_id=? AND code=? AND side='sell' AND status='filled'
                     AND substr(created_at,1,10)=?
                       AND (
                           json_extract(risk_payload,'$.exit_marker')='downside_warning_trim'
                           OR reason LIKE '%下跌预警首段减仓%'
                       )
                   LIMIT 1""",
                (position["account_id"], position["code"], day.isoformat()),
            ).fetchone())
            # P3 审计修复（P1）：partial/full 缺少一次性消费标记——确认后
            # 每个扫描周期都重复减仓，弱势日 ~10 分钟内复利式清仓。按级别
            # 去重：partial 已卖不重复 partial，但条件恶化仍可升级到 full。
            guard_level = downside_guard.get("level")
            guard_level_trimmed = bool(conn.execute(
                """SELECT 1 FROM paper_orders
                   WHERE account_id=? AND code=? AND side='sell' AND status='filled'
                     AND substr(created_at,1,10)=?
                       AND (
                           json_extract(risk_payload,'$.exit_marker')=?
                           OR reason LIKE ?
                       )
                   LIMIT 1""",
                (position["account_id"], position["code"], day.isoformat(),
                 f"downside_{guard_level}", f"%下跌{guard_level}已连续两次确认%"),
            ).fetchone())
            # 2026-09-03 二次确认减仓：warning 级首段减仓（一天一次）用完
            # 之后，若连续两轮扫描（满足最小扫描间隔）均确认"疑似出货"，
            # 允许追加一次 partial 比例的减仓——兑现"连续两次扫描确认后
            # 才允许部分减仓"的审计口径；当日一次，条件恶化仍可升级 full。
            warning_confirmed_trimmed = bool(conn.execute(
                """SELECT 1 FROM paper_orders
                   WHERE account_id=? AND code=? AND side='sell' AND status='filled'
                     AND substr(created_at,1,10)=?
                       AND (
                           json_extract(risk_payload,'$.exit_marker')='downside_warning_confirmed'
                           OR reason LIKE '%下跌预警连续两次确认减仓%'
                       )
                   LIMIT 1""",
                (position["account_id"], position["code"], day.isoformat()),
            ).fetchone())
            warning_actionable = bool(
                position["account_id"] in {"tq_breakout", NEW_STRATEGY_ID}
                and downside_guard.get("level") == "warning"
                and _num(downside_guard.get("sell_ratio")) > 0
                and not warning_trimmed
            )
            warning_confirmed_trim = bool(
                position["account_id"] in {"tq_breakout", NEW_STRATEGY_ID}
                and downside_guard.get("level") == "warning"
                and downside_confirmed
                and ((downside_guard.get("main_force_intent") or {}).get("classification")
                     == "distribution")
                and warning_trimmed
                and not warning_confirmed_trimmed
            )
            guard_actionable = (
                (downside_guard.get("level") in {"partial", "full"} and downside_confirmed
                 and not guard_level_trimmed)
                or warning_actionable
                or warning_confirmed_trim
            )
            guard_pending = downside_guard.get("level") in {"partial", "full"} and not downside_confirmed
            if guard_actionable and not concentration_triggered:
                if warning_confirmed_trim:
                    # 追加减仓按 partial 比例执行（sell_ratio 在 warning 级
                    # 只带首段比例，不能代表确认后的处置力度）。
                    ratio = max(
                        ratio,
                        _num((downside_guard.get("policy") or {}).get("partial_ratio"),
                             _num(downside_guard.get("sell_ratio"), 0.0)),
                    )
                else:
                    ratio = max(ratio, _num(downside_guard.get("sell_ratio"), 0.0))
                guard_level = downside_guard.get("level")
                if warning_confirmed_trim:
                    quality_action = "downside_warning_confirmed"
                elif warning_actionable:
                    quality_action = "downside_warning_trim"
                else:
                    quality_action = f"downside_{guard_level}"
                # 当日去重的结构化消费标记（P1 审计修复 2026-09-02）：
                # 卖出订单 payload 携带 exit_marker，次日/下一级别仍可升级。
                detail["exit_marker"] = quality_action
                mfi_label = ((downside_guard.get('main_force_intent') or {}).get('label') or '不确定')
                if warning_confirmed_trim:
                    quality_reason = (
                        f"下跌预警连续两次确认减仓：本次处理可卖仓位的 {ratio*100:.0f}%；"
                        f"{downside_guard.get('reason')}；主力意图 {mfi_label}"
                    )
                elif warning_actionable:
                    quality_reason = (
                        f"下跌预警首段减仓：本次处理可卖仓位的 {ratio*100:.0f}%；"
                        f"{downside_guard.get('reason')}；主力意图 {mfi_label}"
                    )
                else:
                    quality_reason = (
                        f"下跌{guard_level}已连续两次确认：{downside_guard.get('reason')}；"
                        f"主力意图 {mfi_label}"
                    )
                reason = f"{quality_reason}；覆盖常规卖出比例" if reason else quality_reason
            elif guard_pending or downside_guard.get("level") == "warning":
                if not concentration_triggered:
                    quality_action = "downside_warning"
                    quality_reason = (
                        f"下跌预警待确认：{downside_guard.get('reason')}；"
                        "连续两次扫描确认后才允许部分/全部减仓"
                    )
                if not concentration_triggered:
                    _risk_log(
                        conn,
                        position["account_id"],
                        position["code"],
                        "sell",
                        (
                            f"downside_{downside_guard.get('level')}_pending"
                            if guard_pending else "downside_warning"
                        ),
                        quality_reason,
                        {"downside_guard": downside_guard, "quote_status": quote_status},
                    )
            _save_position_review(conn, cycle["id"], quality_review, quality_action, quality_reason)
            if ratio <= 0:
                continue
            detail["quote_status"] = quote_status
            if quote_status.get("degraded"):
                reason += "；主行情新鲜有效，备用行情未核验，按风控退出降级执行"
            if int(position.get("available_qty") or 0) < LOT_SIZE:
                pending_action = (
                    "permission_scope_exit_t1_locked"
                    if quality_action == "permission_scope_exit" else "held_t1"
                )
                _risk_log(conn, position["account_id"], position["code"], "sell", pending_action, "A股 T+1，暂不可卖", detail)
                continue
            sellable = int(position.get("available_qty") or 0)
            if ratio >= 0.999:
                planned_qty = sellable
            else:
                partial_qty = int(sellable * ratio / LOT_SIZE) * LOT_SIZE
                # P3 审计修复（P2）：一手仓的部分比例取整后为 0，旧逻辑
                # max(LOT_SIZE,…) 会把"预警轻减 25%"放大成整仓清仓。部分
                # 退出一手仓时跳过本次分批（保留观察），不违背分级语义。
                if partial_qty < LOT_SIZE:
                    _risk_log(
                        conn, position["account_id"], position["code"], "sell",
                        "partial_skipped_min_lot",
                        f"可卖 {sellable} 股不足按 {ratio*100:.0f}% 部分减仓的最低一手，"
                        "保留观察不做整仓清仓",
                        detail,
                    )
                    continue
                planned_qty = partial_qty
            planned_qty = min(planned_qty, sellable)
            pct = _num(quote.get("pct"))
            if price <= 0 or pct <= -_limit_pct(
                position["code"], position.get("name"), position.get("risk_flag")
            ) + 0.05:
                # A quote that remains locked at the same limit price cannot
                # produce a new paper fill every five-minute pass.  Keep one
                # auditable attempt, then retry after a short cooldown or as
                # soon as the quoted price changes (the lock may have opened).
                retry_after = (dt.datetime.now() - dt.timedelta(
                    minutes=POSITION_REVIEW_BLOCKED_RETRY_MINUTES
                )).strftime("%Y-%m-%d %H:%M:%S")
                recent_block = conn.execute(
                    """SELECT id,planned_price FROM paper_orders
                       WHERE account_id=? AND side='sell' AND code=?
                         AND status='unfilled_limit_down' AND created_at>=?
                       ORDER BY id DESC LIMIT 1""",
                    (position["account_id"], position["code"], retry_after),
                ).fetchone()
                if recent_block and abs(_num(recent_block["planned_price"]) - price) < 0.001:
                    orders.append({
                        "code": position["code"],
                        "status": "unfilled_limit_down_wait",
                        "reason": f"同价跌停委托 {POSITION_REVIEW_BLOCKED_RETRY_MINUTES} 分钟冷却中，行情解锁或冷却结束后重试",
                    })
                    continue
                status, order_reason = "unfilled_limit_down", (reason + "；跌停/无报价，不能虚构成交")
                _assert_active_lease(conn, "risk unfilled-order write")
                detail = _with_decision_snapshot(
                    detail, account_id=position["account_id"], code=position["code"],
                    side="sell", decision="unfilled", reason=order_reason,
                    asof_date=day, quote=quote, news=news,
                    kline=_completed_kline(position["code"], day, inclusive=False),
                )
                cursor = conn.execute("INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,status,reason,risk_payload,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                      (position["account_id"], "sell", position["code"], position.get("name"), planned_qty, price or None, status, order_reason, _json(detail), _now()))
                _risk_log(conn, position["account_id"], position["code"], "sell", "unfilled", order_reason, detail)
                orders.append({"code": position["code"], "status": status, "reason": order_reason})
                continue
            qty = planned_qty
            fill_price = price * (1 - SLIPPAGE)
            amount = qty * fill_price
            fees = _commission(amount) + amount * STAMP_SELL
            savepoint = f"risk_pos_{position['account_id']}_{position['code']}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                _assert_active_lease(conn, "risk sell lot")
                consumed, cost_amount = _consume_available_lots(conn, position["account_id"], position["code"], qty, day)
                if consumed < LOT_SIZE:
                    _risk_log(conn, position["account_id"], position["code"], "sell", "held_t1", "可卖底仓不足", detail)
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    continue
                qty = consumed
                remaining_qty = max(0, int(_num(position.get("qty"))) - qty)
                position_closed = remaining_qty < LOT_SIZE
                detail["remaining_qty"] = remaining_qty
                detail["position_closed"] = position_closed
                if concentration_triggered and not position_closed:
                    detail["capacity_state"] = "partial_due_t1"
                    reason += f"；仅卖出可卖底仓，仍有 {remaining_qty} 股受 T+1 约束，后续继续处理"
                amount = qty * fill_price
                fees = _commission(amount) + amount * STAMP_SELL
                realized_pnl = amount - cost_amount - fees
                detail = _with_decision_snapshot(
                    detail, account_id=position["account_id"], code=position["code"],
                    side="sell", decision="filled", reason=reason, asof_date=day,
                    quote=quote, news=news,
                    kline=_completed_kline(position["code"], day, inclusive=False),
                    final_score=quality_review.get("score"),
                )
                _assert_active_lease(conn, "risk sell finalization")
                cursor = conn.execute(
                    """INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,filled_price,amount,fees,status,reason,risk_payload,realized_pnl,created_at,executed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (position["account_id"], "sell", position["code"], position.get("name"), qty, price, fill_price,
                     amount, fees, "filled", reason, _json(detail), realized_pnl, _now(), _now()),
                )
                _credit_shared_cash(conn, amount - fees, position["account_id"])
                conn.execute("UPDATE paper_positions SET take_stage=? WHERE account_id=? AND code=?",
                             (next_stage, position["account_id"], position["code"]))
                conn.execute("INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (cursor.lastrowid, position["account_id"], "sell", position["code"], qty, fill_price, amount, fees,
                              day.isoformat(), quote.get("quote_at") or _now(), "实时价 - 0.10% 滑点，含佣金及印花税"))
                _risk_log(conn, position["account_id"], position["code"], "sell", "filled", reason, detail)
                _audit(conn, position["account_id"], "sell_filled", f"{position['code']} {qty}股 @ {fill_price:.2f}")
                # 2026-08-28：partial 减仓时仓位仍在，不产生回补观察；
                # 只有完全退出才记录 recovery watch。
                if detail.get("protective_exit") and ratio >= 0.999:
                    policy = _recovery_policy(position["account_id"])
                    _audit(conn, position["account_id"], "protective_exit_recovery_watch", _json({
                        "status": "watching", "code": position["code"],
                        "account_id": position["account_id"], "exit_class": detail.get("exit_class"),
                        "exit_reason_code": detail.get("exit_reason_code"),
                        "exit_price": fill_price, "exit_at": _now(),
                        "min_reclaim_pct": policy["reclaim_pct"],
                        "required_scans": policy["min_scans"],
                        "cooldown_minutes": policy["cooldown_minutes"],
                        "expires_on": (_date(day) + dt.timedelta(days=policy["max_days"])).isoformat(),
                        "probe_ratio": 0.25,
                        "volatility_shadow": detail.get("volatility_shadow"),
                    }))
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if _lease_lost(exc):
                    raise
                retry_reason = f"持仓卖出执行失败，可重试：{type(exc).__name__}: {exc}"
                _risk_log(conn, position["account_id"], position["code"], "sell", "execution_retry", retry_reason, {"error": str(exc), "retryable": True})
                orders.append({"code": position["code"], "status": "execution_retry", "reason": retry_reason})
                continue
            if concentration_triggered:
                # Rebuild the position snapshot before sizing the replacement
                # so the released value is visible to the shared-pool budget
                # in this same risk pass.
                _sync_positions(conn, asof_day=day)
                if quality_action == "permission_scope_exit":
                    permission_sells_used += 1
                else:
                    concentration_sells_used += 1
                if quality_action == "consolidation_exit":
                    rotation_swaps_used_by_account[position["account_id"]] = (
                        int(rotation_swaps_used_by_account.get(position["account_id"], 0)) + 1
                    )
                # P3 审计修复（S2）：质量轮换独立事件——旧计数把 capacity/
                # permission 退出也算进每日轮换额度，挤占真正的择强换仓。
                if quality_action == "consolidation_exit":
                    _audit(
                        conn, position["account_id"], "quality_rotation",
                        f"{position['code']} 择强换仓，质量评分 {quality_review.get('score', 0):.1f}",
                    )
                _audit(
                    conn, position["account_id"], "concentration_rotation",
                    f"{position['code']} 质量评分 {quality_review.get('score', 0):.1f}，释放额度等待高分候选 {((quality_review.get('replacement') or {}).get('code') or '下一轮选股')}",
                )
                if quality_action == "permission_scope_exit":
                    _audit(
                        conn, position["account_id"], "permission_scope_exit",
                        f"{position['code']} {permission_reason}；本次卖出 {qty} 股，剩余 {remaining_qty} 股",
                    )
                # Reducing an over-cap strategy must lower its stock count.
                # A score-based rotation may enter a stronger replacement;
                # capacity compression deliberately releases cash instead.
                replacement = (
                    {} if quality_action in {"capacity_exit", "permission_scope_exit"} or not position_closed
                    else (quality_review.get("replacement") or {})
                )
                replacement_code = replacement.get("code")
                if replacement_code and replacement_code not in rotation_bought_codes:
                    # Replacement quotes were prefetched with the candidate
                    # snapshot before this write transaction.  Never start
                    # network or disk I/O after the risk ledger is locked.
                    replacement_quote = quote_map.get(replacement_code) or {}
                    replacement_name = replacement.get("name") or replacement_code
                    replacement_news = [
                        row for row in news if str(row.get("code") or "") == str(replacement_code)
                    ]
                    replacement_result = _rotation_buy_candidate(
                        conn,
                        account_map.get(position["account_id"], {"id": position["account_id"]}),
                        replacement,
                        replacement_quote,
                        market_context,
                        replacement_news,
                        day,
                        all_quotes=quote_map,
                    )
                    rotation_results.append(replacement_result)
                    if replacement_result.get("filled"):
                        rotation_bought_codes.add(replacement_code)
                    detail["replacement_buy"] = replacement_result
            orders.append({
                "code": position["code"], "status": "filled", "qty": qty,
                "reason": reason, "concentration_rotation": concentration_triggered,
                "quality_score": quality_review.get("score"),
                "position_closed": position_closed,
                "remaining_qty": remaining_qty,
                "replacement_buy": detail.get("replacement_buy"),
            })
        _sync_positions(conn, asof_day=day)
        _record_nav(conn, day, quotes=quote_map)
        risk_result = {
            "slot": "risk", "date": day.isoformat(),
            "orders": orders, "manual_orders": manual_orders,
            "concentration": {
                "reviewed": len(quality_reviews),
                "rotated": concentration_sells_used,
                "permission_scope_exits": permission_sells_used,
                "replacements": rotation_results,
                "max_per_run": POSITION_REVIEW_MAX_SELLS_PER_RUN,
                "pool_market_value": round(pool_market_value, 2),
                "pool_nav": round(pool_nav, 2),
            },
        }
    # Pending/manual buys run only after all risk exits have committed.  A
    # failed pending batch remains retryable and never hides a completed sell.
    try:
        manual_orders = process_pending_manual_orders(day)
    except Exception as exc:
        if _lease_lost(exc):
            raise
        manual_orders = [{"status": "pending_batch_retry", "reason": str(exc)}]
        with _db(immediate=True) as audit_conn:
            _audit(audit_conn, None, "pending_manual_batch_retry", str(exc))
    risk_result["manual_orders"] = manual_orders
    with _db(immediate=True) as conn:
        _assert_active_lease(conn, "risk scan completion")
        _audit(conn, None, "risk_scan_state", _json({
            "scan_minute": scan_minute, "status": "completed", "finished_at": _now(),
        }))
    return risk_result


def monitor_risk(asof_date=None):
    """Run risk monitoring with a retryable scan ledger.

    The implementation deliberately remains exception-transparent to the
    scheduler, but converts the minute marker to ``failed`` first.  A direct
    retry in the same minute therefore is not swallowed as ``already_scanned``
    after a provider/ledger exception.
    """
    scan_minute = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        return _monitor_risk_impl(asof_date)
    except Exception as exc:
        if _lease_lost(exc):
            raise
        try:
            with _db(immediate=True) as conn:
                _audit(conn, None, "risk_scan_state", _json({
                    "scan_minute": scan_minute,
                    "status": "failed",
                    "finished_at": _now(),
                    "error": f"{type(exc).__name__}: {exc}",
                }))
        except Exception:
            pass
        raise


def _cached_close_market(conn, day, *, allow_network=True):
    row = conn.execute(
        "SELECT detail FROM paper_jobs WHERE slot='close' AND market_date<=? AND status='completed' ORDER BY market_date DESC LIMIT 1",
        (_date(day).isoformat(),),
    ).fetchone()
    detail = _loads(row["detail"]) if row else {}
    return detail.get("market") or _market_state(day, allow_network=allow_network)


def _observe_intraday(conn, cycle_id, account_id, code, price, action, reason, payload):
    conn.execute(
        """INSERT INTO paper_intraday_observations(cycle_id,account_id,code,observed_at,price,action,reason,payload)
           VALUES(?,?,?,?,?,?,?,?)""",
        (cycle_id, account_id, code, _now(), price, action, reason, _json(payload)),
    )


def _intraday_action_today(conn, cycle_id, account_id, code, action, day):
    row = conn.execute(
        """SELECT * FROM paper_intraday_observations WHERE cycle_id=? AND account_id=? AND code=? AND action=?
           AND substr(observed_at,1,10)=? ORDER BY id DESC LIMIT 1""",
        (cycle_id, account_id, code, action, _date(day).isoformat()),
    ).fetchone()
    return dict(row) if row else None


def _prioritize_live_candidate_budget(candidates, account_id, recheck_codes=None, limit=12):
    """Rank the live approval budget while reserving sector-hot evidence.

    Hot lanes only earn review capacity, never approval.  Without this small
    reservation the common score sort can discard every freshly injected
    concept candidate before the execution/risk models ever inspect it.
    """
    recheck_codes = {str(code or "") for code in (recheck_codes or ())}

    def fast_priority_active(item):
        marker = item.get("fast_entry_priority") or {}
        if not marker.get("confirmed"):
            return False
        try:
            valid_until = dt.datetime.fromisoformat(str(marker.get("valid_until") or "")[:19])
        except ValueError:
            return False
        return valid_until >= dt.datetime.now()

    def rank_key(item):
        return (
            1 if fast_priority_active(item) else 0,
            _num(item.get("score"), _num(item.get("t_score"), _num(item.get("rank_score"), -999.0))),
            _num(item.get("t_score"), -999.0),
            _num(item.get("super_net_raw"), _num(item.get("super_net"), -999.0)),
        )

    ranked = sorted(candidates or [], key=rank_key, reverse=True)
    selected = ranked[:limit]
    if account_id == "sector_rotation":
        hot_statuses = {"ths_hot_lane", "concept_expansion_lane", "sector_surge_lane", "hot_leader_watch"}
        hot_paths = {"ths_hot", "concept_expansion", "sector_surge", "hot_leader"}
        all_hot = [
            item for item in ranked
            if item.get("candidate_status") in hot_statuses
            or item.get("entry_path") in hot_paths
        ]
        # Reserve distinct evidence lanes, not merely the four highest scores
        # across all lanes.  Otherwise the broad hot-leader list can consume
        # every reserved slot and the freshly installed THS concept lane is
        # still never evaluated.
        ths_hot = [
            item for item in all_hot
            if item.get("candidate_status") == "ths_hot_lane"
            or item.get("entry_path") == "ths_hot"
        ][:2]
        sector_surge = [
            item for item in all_hot
            if item.get("candidate_status") == "sector_surge_lane"
            or item.get("entry_path") == "sector_surge"
        ][:1]
        concept_expansion = [
            item for item in all_hot
            if item.get("candidate_status") == "concept_expansion_lane"
            or item.get("entry_path") == "concept_expansion"
        ][:2]
        hot = ths_hot + [item for item in concept_expansion if item not in ths_hot]
        hot.extend(item for item in sector_surge if item not in hot)
        hot.extend(item for item in all_hot if item not in hot)
        hot = hot[:6]
        hot_codes = {str(item.get("code") or "") for item in hot}
        regular = [item for item in ranked if str(item.get("code") or "") not in hot_codes]
        selected = hot + regular[:max(0, limit - len(hot))]
    selected_codes = {str(item.get("code") or "") for item in selected}
    selected.extend(
        item for item in ranked
        if str(item.get("code") or "") in recheck_codes
        and str(item.get("code") or "") not in selected_codes
    )
    # Sector-hot reservations may reorder the score list.  A candidate already
    # confirmed by the 30-second observer must nevertheless be inspected first
    # in the next formal three-minute pass.  This changes review order only;
    # it never bypasses approval, quote, risk, capacity or cash gates.
    fast_confirmed = [item for item in ranked if fast_priority_active(item)]
    if fast_confirmed:
        ordered = []
        seen = set()
        for item in fast_confirmed + selected:
            code = str(item.get("code") or "")
            if code and code not in seen:
                ordered.append(item)
                seen.add(code)
        selected = ordered
    return selected


def _bootstrap_signals_for_today(asof_day, live_universe=None, source_slot="intraday"):
    """用上一交易日的完整因子扫描当日候选；仓位数量不作为扫描门槛。

    ``source_slot`` 用于区分盘中重扫和 09:25 集合竞价预选，便于审计
    与后续验证两条路径的候选质量。
    """
    day = _date(asof_day)
    market = _market_state(day, live_universe=live_universe)
    result = {
        "status": "ready",
        "factor_date": None,
        "market": market,
        "accounts": [],
    }
    try:
        factor_table, _ = _selection_inputs()
        factor_date = str(factor_table["last_date"].dropna().max()) if "last_date" in factor_table else None
    except Exception as exc:
        result.update({"status": "blocked", "reason": f"候选因子不可用：{exc}"})
        return result
    result["factor_date"] = factor_date
    factor_is_fresh = _reference_date_is_fresh(factor_date, day)
    # 因子重建通常晚于日线下载一个短窗口。只要缓存完整、最近且
    # 明确落后恰好一个交易日，允许降级扫描；绝不接受未来因子或
    # 多日陈旧快照。硬行情/成交门禁仍在后续流程执行。
    factor_degraded = False
    if not factor_is_fresh and factor_date:
        try:
            # 比较“最近完整收盘日”，而不是盘中自然日；周末/午后会
            # 让 8/13→8/17 看起来落后两天，实际只落后一个完整交易日。
            complete_day = U.latest_complete_trade_date(day)
            lag = _trading_weekday_lag(factor_date, complete_day)
            meta = _selection_factor_cache_meta()
            freshness = _selection_factor_freshness(
                factor_table, list(U.load_universe() or []), complete_day, meta=meta
            )
            factor_degraded = bool(lag == 1 and freshness.get("passed") and
                                   freshness.get("degraded_fallback"))
        except Exception:
            factor_degraded = False
    if not factor_is_fresh and not factor_degraded:
        result.update({"status": "blocked", "reason": f"候选因子停留在 {factor_date or '未知'}"})
        return result
    if factor_degraded:
        result["factor_fallback"] = True
        result["factor_fallback_reason"] = "上一完整交易日因子快照，等待当日重建"

    factor_day = _date(factor_date)
    history = _history_manifest()
    try:
        sector_rows = dfc.fetch_hot_sector_snapshot()
        if not sector_rows:
            sector_rows = dfc.fetch_sector_flow("industry")
    except Exception:
        sector_rows = []
    # Build the broad candidate sets and fetch bounded microstructure evidence
    # before opening the write transaction. A slow public tick endpoint must
    # never hold the SQLite writer or delay risk exits/API reads.
    with _db() as account_conn:
        accounts = _rows(account_conn, "SELECT * FROM paper_accounts WHERE status='running'")
    precomputed_candidates = {}
    micro_codes = []
    for account in accounts:
        try:
            rows, meta = _candidate_rows(
                account, factor_day, market, sector_rows=sector_rows,
                live_universe=live_universe, live_asof_date=day,
            )
        except Exception as exc:
            rows, meta = [], {"blocked": True, "reason": f"候选生成失败：{type(exc).__name__}: {exc}"}
        precomputed_candidates[account["id"]] = (rows, meta)
        preliminary = _prioritize_live_candidate_budget(rows, account["id"], limit=6)
        micro_codes.extend(str(item.get("code") or "") for item in preliminary if item.get("code"))
    try:
        global_microstructure_map = (
            AD.market_microstructures(micro_codes, asof_day=day)
            if AD is not None else {}
        )
    except Exception:
        global_microstructure_map = {}
    with _db() as conn:
        cycle = _active_cycle(conn)
        for account in accounts:
            try:
                # 已成交或待执行的同一标的当天不可重复委托；但已有持仓、已有其他
                # 候选均不能停止账户继续扫描。数量由资金/风险预算而非持仓个数约束。
                actionable_codes = {
                    str(row["code"])
                    for row in _rows(
                        conn,
                        """SELECT code FROM paper_signals
                           WHERE account_id=? AND intended_date=? AND status='pending'""",
                        (account["id"], day.isoformat()),
                    )
                }
                filled_codes = {
                    str(row["code"])
                    for row in _rows(
                        conn,
                        """SELECT code FROM paper_signals
                           WHERE account_id=? AND intended_date=? AND status='filled'""",
                        (account["id"], day.isoformat()),
                    )
                }
                deferred_codes = {
                    str(row["code"])
                    for row in _rows(
                        conn,
                        """SELECT code FROM paper_signals
                           WHERE account_id=? AND intended_date=?
                             AND status IN ('deferred_capacity',?)""",
                        (account["id"], day.isoformat(), ENTRY_FROZEN_WAITLIST_STATUS),
                    )
                }
                # 追高专属风控未通过时，当日不应每五分钟重复生成同一笔拒绝单；
                # 保留审计，下一交易日再用新的行情、量能和Q级重新评估。
                chase_rejected_codes = {
                    str(row["code"])
                    for row in _rows(
                        conn,
                        """SELECT code FROM paper_signals
                           WHERE account_id=? AND intended_date=? AND status='rejected'
                             AND reason LIKE '%短线追高风控未通过%'""",
                        (account["id"], day.isoformat()),
                    )
                }
                position_codes = {
                    str(row["code"]) for row in _position_rows(conn, account["id"], day)
                }
                # 波段策略仅在当天已经完整卖出、重新入选且此前没有再入场尝试时，
                # 才允许同标的当日再次开仓；日内做T由专用的高抛/回补路径处理。
                recovery_watches = _recovery_watches(conn, account["id"], day)
                recovery_codes = set(recovery_watches)
                reentry_codes = set()
                if account.get("mode") != "intraday_t":
                    sold_today = {
                        str(row["code"])
                        for row in _rows(
                            conn,
                            """SELECT DISTINCT code FROM paper_orders
                               WHERE account_id=? AND side='sell' AND status='filled'
                                 AND substr(executed_at,1,10)=?""",
                            (account["id"], day.isoformat()),
                        )
                    }
                    reentry_codes = sold_today - position_codes - recovery_codes
                # 普通 blocked/rejected 信号可在日内新行情下复评；短线追高专属门禁
                # 失败则固定到下一交易日，避免每5分钟反复刷相同拒绝记录。
                conn.execute(
                    """UPDATE paper_signals SET status='superseded',reason=?
                       WHERE account_id=? AND intended_date=?
                         AND (status='blocked' OR (status='rejected' AND reason NOT LIKE '%短线追高风控未通过%'))""",
                    (f"{RISK_VERSION} 重新评估后未再次入选", account["id"], day.isoformat()),
                )
                # H2: 当日待成交 pending 若长时间无法成交（资金/价格未满足），必须
                # 有时间兜底转出，否则其 code 一直留在 actionable_codes 阻断同标的
                # 再入场 → “永久无法再入场”死端。90 分钟未成交视为当日放弃，
                # 释放该标的的再入场封锁；次日重新扫描时按新行情重新评估。
                pending_cutoff = (dt.datetime.now() - dt.timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """UPDATE paper_signals SET status='superseded',reason=?
                       WHERE account_id=? AND intended_date=?
                         AND status='pending' AND created_at < ?""",
                    (f"{RISK_VERSION} 当日90分钟未成交，释放同标的再入场", account["id"], day.isoformat(), pending_cutoff),
                )

                candidates, meta = precomputed_candidates.get(account["id"], ([], {}))
                candidates = [dict(item) for item in candidates]
                # 仓位单位逻辑修复后，旧的容量误判信号必须重新进入本轮候选，
                # 不能因为它们不在当前前 12 名就继续沉淀为“次日重筛”。
                candidate_codes = {str(item.get("code") or "") for item in candidates}
                recheck_rows = _rows(
                    conn,
                    """SELECT code,payload FROM paper_signals
                       WHERE account_id=? AND intended_date=?
                             AND status IN ('recheck_capacity','deferred_capacity',?,?)""",
                    (account["id"], day.isoformat(), ENTRY_FROZEN_WAITLIST_STATUS, RECOVERY_WATCH_STATUS),
                )
                recheck_codes = {str(row.get("code") or "") for row in recheck_rows}
                for recheck_row in recheck_rows:
                    recheck_payload = _loads(recheck_row.get("payload"), {})
                    recheck_pick = recheck_payload.get("pick") or {}
                    if recheck_payload.get("fast_entry_priority"):
                        recheck_pick["fast_entry_priority"] = recheck_payload["fast_entry_priority"]
                    recheck_code = str(recheck_pick.get("code") or recheck_row.get("code") or "")
                    recheck_scope = _security_scope(
                        recheck_code, recheck_pick.get("name"), recheck_pick.get("risk_flag"),
                    )
                    if recheck_code and recheck_scope["allowed"]:
                        # It may already be in the live top candidates.  Either
                        # way, this pass must be allowed to refresh its deferred
                        # status after cash or a slot has been released.
                        deferred_codes.discard(recheck_code)
                    if recheck_code and recheck_scope["allowed"] and recheck_code not in candidate_codes:
                        recheck_pick["code"] = recheck_code
                        candidates.append(recheck_pick)
                        candidate_codes.add(recheck_code)
                        # The following approval pass will either restore this
                        # candidate to pending or keep it deferred.  Do not let a
                        # stale snapshot of deferred_codes suppress that recheck.
                    elif (
                        recheck_code and recheck_scope["allowed"]
                        and recheck_pick.get("fast_entry_priority")
                    ):
                        # The live strategy scan may already contain the same
                        # code.  Preserve the 30-second confirmation on that
                        # fresher candidate instead of losing its priority
                        # during code deduplication.
                        for live_pick in candidates:
                            if str(live_pick.get("code") or "") == recheck_code:
                                live_pick["fast_entry_priority"] = recheck_pick["fast_entry_priority"]
                                break
                # Do not repeatedly send the same structurally invalid trend names
                # through the 12-slot live approval budget every three minutes.
                # Capacity/freeze rechecks remain exempt: they were valid entries
                # and must be allowed to wake as soon as a slot/cash is released.
                structural_cooldowns = []
                risk_cooldowns = []
                cooled_candidates = []
                for candidate in candidates:
                    candidate_code = str(candidate.get("code") or "")
                    risk_cooldown = _bootstrap_risk_rejection_cooldown(
                        conn, account["id"], candidate_code, day,
                    )
                    if risk_cooldown:
                        risk_cooldowns.append({"code": candidate_code, **risk_cooldown})
                        continue
                    cooldown = (
                        None if candidate_code in recheck_codes
                        else _bootstrap_structural_recheck_cooldown(
                            conn, account["id"], candidate_code, day,
                        )
                    )
                    if cooldown:
                        structural_cooldowns.append({"code": candidate_code, **cooldown})
                        continue
                    cooled_candidates.append(candidate)
                candidates = cooled_candidates
                try:
                    NL.capture_candidate_snapshot(account["id"], candidates, factor_day, slot=source_slot)
                except Exception as exc:
                    _audit(conn, account["id"], "candidate_snapshot_failed", f"{type(exc).__name__}: {exc}")
                # Waiting-pool priority is recalculated from the newest score on
                # every scan.  Database insertion order must not pin an old name.
                deduped = {}
                for item in candidates:
                    code = str(item.get("code") or "")
                    if code:
                        deduped[code] = item
                candidates = _prioritize_live_candidate_budget(
                    list(deduped.values()), account["id"], recheck_codes, limit=12,
                )
                if meta.get("blocked"):
                    item = {
                        "id": account["id"], "status": "blocked",
                        "reason": meta.get("reason"), "candidates": 0, "approved": 0,
                        "full_market_scan": meta.get("full_market_scan"),
                    }
                    result["accounts"].append(item)
                    _observe_intraday(
                        conn, cycle["id"], account["id"], None, None, "scan",
                        item["reason"], {"factor_date": factor_date, "market": market,
                                         "full_market_scan": meta.get("full_market_scan")},
                    )
                    continue

                names = {pick["code"]: pick.get("name") or pick["code"] for pick in candidates}
                quotes = _quotes(list(names))
                news = _news_for(names)
                for candidate in candidates:
                    candidate["microstructure"] = global_microstructure_map.get(
                        str(candidate.get("code") or ""),
                        {"status": "source_unavailable", "score_applied": False,
                         "grade": "public_quote_shadow"},
                    )
                try:
                    sector_flow = dfc.fetch_sector_flow("industry")
                except Exception:
                    sector_flow = []
                approved = 0
                waitlisted = 0
                skipped_existing = 0
                for pick in candidates:
                    code = pick["code"]
                    is_reentry = code in reentry_codes
                    is_recovery = code in recovery_codes
                    reentry_already_armed = bool(
                        _intraday_action_today(conn, cycle["id"], account["id"], code, "swing_reentry_armed", day)
                    )
                    if (
                        code in actionable_codes
                        or code in position_codes or (code in filled_codes and not (is_reentry or is_recovery))
                        or reentry_already_armed or code in chase_rejected_codes
                        or any(item.get("code") == code for item in risk_cooldowns)
                    ):
                        skipped_existing += 1
                        continue
                    quote = quotes.get(code, {})
                    # 同一只候选在同一轮循环里会被 _signal_approval 和
                    # _with_decision_snapshot 各用一次 K 线；只读一次文件。
                    kline = _completed_kline(code, factor_day)
                    passed, reason, decision, market_policy = _signal_approval(
                        account, pick, quote, kline,
                        sector_flow, market, news, history.get(code), day,
                        factor_asof_date=factor_day,
                    )
                    recovery_observation = None
                    if is_recovery:
                        recovery_ok, recovery_reason, recovery_observation = _recovery_observation(
                            conn, account["id"], code, recovery_watches[code], quote, day,
                        )
                        if not recovery_ok:
                            passed = False
                            reason = f"止损后恢复观察：{recovery_reason}"
                    payload = {
                        "pick": pick, "decision": decision, "market": market,
                        "market_policy": market_policy,
                        "factor": {**meta, "bootstrap": True, "bootstrap_slot": source_slot},
                        "risk_version": RISK_VERSION,
                        "quote": quote,
                        "news": [item for item in news if item.get("code") == code],
                    }
                    if is_reentry:
                        payload["reentry"] = {"kind": "swing_reentry", "same_day": True}
                    if is_recovery:
                        payload["recovery_watch"] = {
                            **recovery_watches[code], "observation": recovery_observation,
                            "passed": bool(passed), "probe_ratio": 0.25,
                            "policy": _recovery_policy(account["id"]),
                        }
                    payload = _with_decision_snapshot(
                        payload, account_id=account["id"], code=code, side="buy",
                        decision=("approved_bootstrap" if passed else "rejected_bootstrap"),
                        reason=reason, asof_date=day, quote=quote,
                        kline=kline,
                        news=payload.get("news"),
                        final_score=(decision.get("entry_model") or {}).get("score"),
                    )
                    status = (
                        ENTRY_FROZEN_WAITLIST_STATUS
                        if passed and _entry_freeze_enabled()
                        else "pending" if passed else RECOVERY_WATCH_STATUS if is_recovery else "blocked"
                    )
                    if status == ENTRY_FROZEN_WAITLIST_STATUS:
                        waitlisted += 1
                        reason = _entry_frozen_reason("盘中候选")
                        payload["entry_freeze"] = {
                            "enabled": True, "env": ENTRY_FREEZE_ENV,
                            "status": ENTRY_FROZEN_WAITLIST_STATUS,
                            "source": "盘中候选", "asof_date": day.isoformat(),
                            "reason": reason,
                        }
                    # Waiting-pool priority is still subordinate to the normal
                    # approval/risk result, but an early hot leader should not be
                    # hidden behind an older high-Q signal forever.  Keep the
                    # adjustment tiny, bounded and auditable; it cannot override
                    # a rejected signal or consume a slot without the usual gates.
                    hot_leader = pick.get("hot_leader") or {}
                    hot_score = max(0.0, min(1.0, _num(hot_leader.get("score"), 0.0)))
                    hot_stage = str(hot_leader.get("stage") or "normal")
                    waitlist_priority = round(
                        max(0.0, min(0.08, hot_score * 0.08))
                        if hot_stage == "early_acceleration" else 0.0,
                        6,
                    )
                    payload["waitlist_priority"] = {
                        "version": "hot-leader-waitlist-v1",
                        "score": waitlist_priority,
                        "stage": hot_stage,
                        "reason": "早期强势、资金和流动性共振；仅用于同批等待池排序",
                        "execution_override": False,
                    }
                    conn.execute(
                        """INSERT INTO paper_signals(
                               account_id,signal_date,intended_date,code,name,industry,close_price,
                               rank_score,t_tier,t_score,payload,status,reason,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(account_id,signal_date,code) DO UPDATE SET
                               intended_date=excluded.intended_date,
                               name=excluded.name,
                               industry=excluded.industry,
                               close_price=excluded.close_price,
                               rank_score=excluded.rank_score,
                               t_tier=excluded.t_tier,
                               t_score=excluded.t_score,
                               payload=excluded.payload,
                               status=excluded.status,
                               reason=excluded.reason,
                               created_at=excluded.created_at""",
                        (
                            account["id"], factor_day.isoformat(), day.isoformat(), code,
                            pick.get("name"), pick.get("industry"),
                            _num(quote.get("price"), _num(pick.get("price"))),
                            _num(pick.get("score")), decision.get("tier"),
                            _num((decision.get("entry_model") or {}).get("score"), 0.0) + waitlist_priority,
                            _json(payload), status, reason, _now(),
                        ),
                    )
                    _risk_log(
                        conn, account["id"], code, "buy",
                        ENTRY_FROZEN_WAITLIST_STATUS
                        if status == ENTRY_FROZEN_WAITLIST_STATUS
                        else "approved_bootstrap" if passed else "rejected_bootstrap",
                        reason or "盘中候选通过", payload,
                    )
                    if is_reentry and passed:
                        _observe_intraday(
                            conn, cycle["id"], account["id"], code, _num(quote.get("price")),
                            "swing_reentry_armed", "波段卖出后重新满足入场条件，允许单次再入场", payload,
                        )
                    if is_recovery:
                        _observe_intraday(
                            conn, cycle["id"], account["id"], code, _num(quote.get("price")),
                            "protective_recovery", reason, payload,
                        )
                    approved += int(passed)
                item = {
                    "id": account["id"], "status": "ready",
                    "reason": (
                        f"扫描 {len(candidates)} 只，新增通过 {approved} 只"
                        + (f"，保留当日信号 {skipped_existing} 只" if skipped_existing else "")
                        + (f"，结构冷却 {len(structural_cooldowns)} 只" if structural_cooldowns else "")
                        + (f"，重复风控冷却 {len(risk_cooldowns)} 只" if risk_cooldowns else "")
                    ),
                    "candidates": len(candidates) - skipped_existing, "approved": approved,
                    "waitlisted": waitlisted, "existing": skipped_existing,
                    "structural_cooldowns": structural_cooldowns[:12],
                    "risk_cooldowns": risk_cooldowns[:12],
                    "microstructure": {
                        "requested": len(names),
                        "available": sum(
                            1 for code, value in global_microstructure_map.items()
                            if code in names
                            if value.get("status") in {"ok", "partial"}
                        ),
                        "policy": "bounded_soft_score_shadow",
                    },
                    "full_market_scan": meta.get("full_market_scan"),
                }
                result["accounts"].append(item)
                _observe_intraday(
                    conn, cycle["id"], account["id"], None, None, "scan",
                    item["reason"],
                    {"factor_date": factor_date, "market": market, "meta": meta,
                     "structural_cooldowns": structural_cooldowns[:12],
                     "risk_cooldowns": risk_cooldowns[:12]},
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _audit(conn, account["id"], "account_scan_error", error)
                item = {"id": account["id"], "status": "error", "reason": error,
                        "candidates": 0, "approved": 0, "full_market_scan": None}
                result["accounts"].append(item)
                try:
                    _observe_intraday(conn, cycle["id"], account["id"], None, None,
                                      "scan_error", error, {"error": error})
                except Exception:
                    pass
                continue
    result["approved"] = sum(item.get("approved", 0) for item in result["accounts"])
    result["waitlisted"] = sum(item.get("waitlisted", 0) for item in result["accounts"])
    return result


def _auction_quote_time(value):
    """解析行情源时间，兼容 ISO 时间与 20260804092500 格式。"""
    text = str(value or "").strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 14:
        try:
            return dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


def run_auction_preselection(asof_date=None, force=False):
    """只在 09:25 读取一次集合竞价快照，生成待 09:31 开盘确认的预选信号。

    09:15–09:20 的委托可撤，波动和虚假撮合较大，因此不采集也不下单；
    09:25 的快照只用于筛选与排序，真正成交仍须在 09:31 用最新双源行情复核。
    """
    init_db()
    now = dt.datetime.now()
    day = _date(asof_date)
    if not force and (now.weekday() >= 5 or now.time() < dt.time(9, 24) or now.time() > dt.time(9, 27)):
        return {"slot": "auction", "status": "skipped", "date": day.isoformat(), "reason": "仅在 09:25 采集集合竞价快照"}
    try:
        live_universe = dfc.fetch_market_snapshot_full(max_age=90, force=True)
    except Exception as exc:
        return {"slot": "auction", "status": "blocked", "date": day.isoformat(), "reason": f"竞价行情读取失败：{exc}"}
    current_rows = []
    for row in live_universe or []:
        stamp = _auction_quote_time(row.get("quote_at"))
        if stamp and stamp.date() == day and dt.time(9, 24) <= stamp.time() <= dt.time(9, 27):
            current_rows.append(row)
    auction_gate = _live_scan_gate(current_rows, day)
    if not auction_gate["ready"]:
        return {
            "slot": "auction", "status": "blocked", "date": day.isoformat(),
            "reason": (
                f"竞价行情覆盖 {auction_gate['covered_codes']}/{auction_gate['eligible_codes']}"
                f"（{auction_gate['coverage_pct']:.1f}%），需至少 {auction_gate['required_codes']} 只"
            ),
            "snapshot_count": len(live_universe or []), "auction_count": len(current_rows),
            "live_scan_gate": auction_gate,
        }
    auction_at = max((_auction_quote_time(row.get("quote_at")) for row in current_rows), default=None)
    # The full snapshot may contain rows captured before/after the auction
    # window.  Ranking must consume only the validated 09:24–09:27 rows.
    result = _bootstrap_signals_for_today(day, live_universe=current_rows, source_slot="auction_0925")
    result.update({
        "slot": "auction", "status": result.get("status", "ready"),
        "auction_at": auction_at.isoformat() if auction_at else None,
        "snapshot_count": len(live_universe or []), "auction_count": len(current_rows),
        "live_scan_gate": auction_gate,
        "execution_note": "仅预选；09:31 开盘审批仍需最新双源实时行情通过后才可成交",
    })
    with _db() as conn:
        cycle = _active_cycle(conn)
        for account in _rows(conn, "SELECT id FROM paper_accounts WHERE status='running'"):
            _observe_intraday(
                conn, cycle["id"], account["id"], None, None, "auction_preselect",
                f"09:25 集合竞价预选完成：有效 {len(current_rows)} 只",
                {"auction_at": result.get("auction_at"), "snapshot_count": len(live_universe or []),
                 "auction_count": len(current_rows), "approved": result.get("approved", 0)},
            )
    return result


def _existing_position_addition_gate(conn, account, code, asof_day, quote=None):
    """Only reinforce high-quality holdings while the count budget is healthy."""
    positions = _position_rows(conn, asof_day=asof_day)
    position = next(
        (item for item in positions if item.get("account_id") == account["id"] and str(item.get("code")) == str(code)),
        None,
    )
    quote = quote or {}
    scope = _security_scope(
        code, quote.get("name") or (position or {}).get("name"), quote.get("risk_flag"),
    )
    if not scope["allowed"]:
        return False, f"{scope['reason']}，存量持仓只允许卖出，不允许加仓或做T回补"
    count_budget = _dynamic_position_limits(conn)
    strategy_count = sum(
        1 for item in positions
        if item.get("account_id") == account["id"] and int(_num(item.get("qty"))) >= LOT_SIZE
    )
    pool_count = sum(1 for item in positions if int(_num(item.get("qty"))) >= LOT_SIZE)
    strategy_limit = int(count_budget["limits"].get(account["id"], 5))
    if strategy_count > strategy_limit:
        return False, f"策略持仓 {strategy_count}/{strategy_limit} 超出动态上限，先完成压缩或换仓"
    if pool_count > count_budget["pool_limit"]:
        return False, f"总持仓 {pool_count}/{count_budget['pool_limit']} 超限，暂停追加仓位"
    cycle = _active_cycle(conn)
    review = conn.execute(
        """SELECT score,action FROM paper_position_reviews
           WHERE cycle_id=? AND account_id=? AND code=?
           ORDER BY id DESC LIMIT 1""",
        (cycle["id"], account["id"], str(code)),
    ).fetchone()
    if review and _num(review["score"], 0.0) < POSITION_ADD_MIN_SCORE:
        return False, f"持仓质量评分 {_num(review['score']):.1f} 低于加仓线 {POSITION_ADD_MIN_SCORE:.0f}"
    if review and str(review["action"] or "") in {
        "capacity_exit", "capacity_exit_pending_quote", "consolidation_exit", "risk_exit",
        "permission_scope_exit", "permission_scope_exit_pending_quote",
        # The shared downside guard freezes same-strategy additions as soon
        # as a warning is observed.  A confirmed partial/full action keeps
        # that freeze in place until the holding leaves the review queue.
        "downside_warning", "downside_pending_quote", "downside_partial", "downside_full",
    }:
        return False, "该持仓已进入下跌预警/压缩/退出队列，不允许反向加仓"
    return True, "动态席位正常，持仓质量允许进入策略专属加仓复核"


def _opening_event_assessment(conn, account, position, quote, asof_day, cycle):
    """共享的开盘事件识别器；策略只提供阈值，不复制行情判断逻辑。"""
    policy = OPENING_EVENT_POLICIES.get(account.get("id")) or {}
    price = _num(quote.get("price"))
    cost = _num(position.get("cost"))
    pct = _num(quote.get("pct"), 0.0)
    prev_close = _num(quote.get("prev_close"))
    if prev_close <= 0 and price > 0 and pct > -99:
        prev_close = price / max(1.0 + pct / 100.0, 0.01)
    peak = max(price, _num(quote.get("high")))
    observations = conn.execute(
        """SELECT price,payload FROM paper_intraday_observations
           WHERE cycle_id=? AND account_id=? AND code=?
           AND substr(observed_at,1,10)=? ORDER BY id DESC LIMIT 30""",
        (cycle["id"], account["id"], str(position["code"]), _date(asof_day).isoformat()),
    ).fetchall()
    for row in observations:
        peak = max(peak, _num(row["price"]))
        detail = _loads(row["payload"], {}) or {}
        peak = max(peak, _num(detail.get("quote_high")))
    peak_pct = (peak / prev_close - 1.0) * 100 if peak > 0 and prev_close > 0 else 0.0
    retrace_pct = (1.0 - price / peak) * 100 if price > 0 and peak > 0 else 0.0
    peak_edge_pct = (peak / cost - 1.0) * 100 if peak > 0 and cost > 0 else -999.0
    passed = bool(
        policy.get("enabled")
        and price > 0
        and peak_pct >= _num(policy.get("min_peak_pct"))
        and retrace_pct >= _num(policy.get("min_retrace_pct"))
        and pct >= _num(policy.get("min_current_pct"), -99.0)
        and (
            policy.get("allow_loss_trim")
            or peak_edge_pct >= _num(policy.get("min_peak_edge_pct"), 0.0)
        )
        and peak_edge_pct >= _num(policy.get("min_peak_edge_pct"), -999.0)
    )
    return {
        "engine": "shared_opening_event_v1",
        "strategy": account.get("id"),
        "strategy_name": policy.get("name"),
        "passed": passed,
        "price": round(price, 4),
        "peak_price": round(peak, 4),
        "prev_close": round(prev_close, 4) if prev_close > 0 else None,
        "current_pct": round(pct, 3),
        "peak_pct": round(peak_pct, 3),
        "retrace_pct": round(retrace_pct, 3),
        "peak_edge_pct": round(peak_edge_pct, 3),
        "min_peak_pct": _num(policy.get("min_peak_pct")),
        "min_retrace_pct": _num(policy.get("min_retrace_pct")),
        "quote_at": quote.get("quote_at"),
        "quote_validation": quote.get("quote_validation"),
        "reason": (
            "开盘冲高后回落，满足策略专属减仓阈值"
            if passed else "开盘冲高回落尚未同时满足峰值、回撤、成本和策略条件"
        ),
    }


def _sell_rebuy_confirmation(conn, sold, quote, asof_day, account):
    """独立的高抛后回补确认，不把止损恢复误作抄底。

    此通道只接回同日已高抛的库存；保护性退出仍由 ``protective_recovery``
    经过冷却、重新入选与完整入场门禁处理。回补必须同时确认卖后折价、低点、
    反弹持续、日内未失控以及实时资金不偏空。
    """
    policy = OPENING_EVENT_POLICIES.get(account.get("id")) or {}
    sold_payload = _loads(sold.get("payload"), {}) or {}
    sold_price = _num(sold_payload.get("sell_price"))
    price = _num(quote.get("price"))
    rows = conn.execute(
        """SELECT price FROM paper_intraday_observations
           WHERE id>? AND account_id=? AND code=?
           AND substr(observed_at,1,10)=? AND price IS NOT NULL ORDER BY id""",
        (int(sold.get("id") or 0), account["id"], str(sold.get("code") or ""), _date(asof_day).isoformat()),
    ).fetchall()
    # Current price is a candidate rebound observation, but cannot by itself
    # prove a rebound: two earlier post-sell observations are required.
    observed_prices = [_num(row["price"]) for row in rows if _num(row["price"]) > 0]
    prices = observed_prices + ([price] if price > 0 else [])
    low_since_sell = min(prices) if prices else price
    rebound_pct = (price / low_since_sell - 1.0) * 100 if low_since_sell > 0 else 0.0
    max_sold_ratio = _num(policy.get("rebuy_max_sold_ratio"), 0.995)
    min_rebound = _num(policy.get("rebuy_rebound_pct"), 1.5)
    min_observations = max(2, int(_num(policy.get("rebuy_min_observations"), 2)))
    min_main_pct = _num(policy.get("rebuy_min_main_pct"), 0.0)
    min_current_pct = _num(policy.get("rebuy_min_current_pct"), -1.0)
    if not sold_payload.get("opening_event"):
        # 普通做T可以比开盘事件更快，但也必须先观察低点再确认反弹。
        min_rebound = min(min_rebound, 0.60)
    main_pct = _num(quote.get("main_pct"), _num(quote.get("main_net_pct"), None))
    current_pct = _num(quote.get("pct"), None)
    low_index = prices.index(low_since_sell) if prices else -1
    rebound_confirmations = sum(
        1 for item in prices[low_index + 1:]
        if item >= low_since_sell * (1 + min_rebound / 100.0)
    ) if low_index >= 0 else 0
    passed = bool(
        price > 0 and sold_price > 0 and len(observed_prices) >= min_observations
        and price <= sold_price * max_sold_ratio
        and rebound_pct >= min_rebound
        and rebound_confirmations >= 2
        and current_pct is not None and current_pct >= min_current_pct
        and main_pct is not None and main_pct >= min_main_pct
    )
    blockers = []
    if len(observed_prices) < min_observations:
        blockers.append(f"卖后观察 {len(observed_prices)}/{min_observations} 次不足")
    if price > sold_price * max_sold_ratio:
        blockers.append("回补价未形成策略要求的卖后折价")
    if rebound_pct < min_rebound or rebound_confirmations < 2:
        blockers.append(f"低点反弹未持续确认（{rebound_pct:.2f}% / {rebound_confirmations} 次）")
    if current_pct is None or current_pct < min_current_pct:
        blockers.append(f"日内涨跌幅未守住 {min_current_pct:+.2f}%")
    if main_pct is None or main_pct < min_main_pct:
        blockers.append(f"实时主力净流入未达 {min_main_pct:+.2f}%")
    return {
        "passed": passed,
        "low_since_sell": round(low_since_sell, 4) if low_since_sell > 0 else None,
        "rebound_pct": round(rebound_pct, 3),
        "min_rebound_pct": min_rebound,
        "max_sold_ratio": max_sold_ratio,
        "observations": len(observed_prices),
        "required_observations": min_observations,
        "rebound_confirmations": rebound_confirmations,
        "main_pct": round(main_pct, 3) if main_pct is not None else None,
        "min_main_pct": min_main_pct,
        "current_pct": round(current_pct, 3) if current_pct is not None else None,
        "min_current_pct": min_current_pct,
        "model": "sell_rebuy_confirmation_v2",
        "blockers": blockers,
        "reason": (
            "高抛后低点、反弹持续和资金确认通过，允许策略专属回补"
            if passed else "；".join(blockers) or "等待卖后低点与资金确认，暂不接下落飞刀"
        ),
    }


def _intraday_sell(conn, account, position, quote, asof_day, profile, cycle, opening_event=False):
    """高抛腿：普通做T与共享开盘事件共用成交/账本逻辑。"""
    if not opening_event and account.get("mode") != "intraday_t":
        return None, "非日内做T策略"
    quote_status = _execution_quote_status(quote, asof_day, purpose="exit")
    if not quote_status["fresh"]:
        _risk_log(
            conn, account["id"], position["code"], "sell",
            "exit_pending_data", quote_status["reason"], quote_status,
        )
        return None, quote_status["reason"]
    price = _num(quote.get("price"))
    available = int(position.get("available_qty") or 0)
    if available < LOT_SIZE:
        return None, "无可卖底仓"
    if _intraday_action_today(conn, cycle["id"], account["id"], position["code"], "t_sell", asof_day):
        return None, "本标的本日已完成高抛观察"
    pct = _num(quote.get("pct"))
    edge = price / max(_num(position["cost"]), 0.01) - 1
    event = _opening_event_assessment(conn, account, position, quote, asof_day, cycle) if opening_event else None
    if opening_event:
        if not event.get("passed"):
            return None, event.get("reason") or "未达到开盘事件阈值"
        qty_ratio = _num((OPENING_EVENT_POLICIES.get(account.get("id")) or {}).get("trim_ratio"), 0.2)
        trigger = "开盘冲高回落，策略专属事件减仓"
    else:
        # 做T高抛不是“涨 1.2% 就卖”。旧规则把仍在加速的主升股当成
        # 可卖对象，容易在大行情里过早丢掉底仓。普通日内腿改为先冲高、
        # 再确认从日内高点回撤，仍保留成本收益后才减一部分可卖库存。
        prev_close = _num(quote.get("prev_close"))
        if prev_close <= 0 and price > 0 and pct > -99:
            prev_close = price / max(1.0 + pct / 100.0, 0.01)
        peak = max(price, _num(quote.get("high")))
        peak_pct = (peak / prev_close - 1.0) * 100 if peak > 0 and prev_close > 0 else 0.0
        retrace_pct = (1.0 - price / peak) * 100 if peak > 0 and price > 0 else 0.0
        min_peak_pct = 3.5
        min_retrace_pct = 1.2
        min_profit_edge = max(_num(profile.get("min_cost_edge")), 0.012)
        if not (
            price > 0
            and peak_pct >= min_peak_pct
            and retrace_pct >= min_retrace_pct
            and edge >= min_profit_edge
            and pct >= -0.5
        ):
            return None, (
                f"未形成冲高回落做T卖点（峰值 {peak_pct:.2f}%/{min_peak_pct:.2f}%，"
                f"回撤 {retrace_pct:.2f}%/{min_retrace_pct:.2f}%，收益 {edge*100:.2f}%）"
            )
        qty_ratio = 0.30
        trigger = "冲高后回撤确认且收益覆盖成本"
    qty = max(LOT_SIZE, int(available * qty_ratio / LOT_SIZE) * LOT_SIZE)
    qty = min(qty, available)
    fill = price * (1 - SLIPPAGE)
    amount = qty * fill
    fees = _commission(amount) + amount * STAMP_SELL
    consumed, cost_amount = _consume_available_lots(conn, account["id"], position["code"], qty, asof_day)
    if consumed < LOT_SIZE:
        return None, "可卖底仓不足"
    qty, amount = consumed, consumed * fill
    fees = _commission(amount) + amount * STAMP_SELL
    pnl = amount - cost_amount - fees
    payload = {"kind": "stock_inventory_t", "sell_price": round(fill, 4), "qty": qty,
               "cost_amount": round(cost_amount, 2), "quote_at": quote.get("quote_at"),
               "edge_pct": round(edge * 100, 2), "trigger": trigger,
               "opening_event": bool(opening_event), "opening_assessment": event,
               "quote_high": _num(quote.get("high")), "quote_low": _num(quote.get("low")),
               "take_profit_peak_pct": round(peak_pct, 3) if not opening_event else None,
               "take_profit_retrace_pct": round(retrace_pct, 3) if not opening_event else None,
               "quote_status": quote_status}
    payload = _with_decision_snapshot(
        payload, account_id=account["id"], code=position["code"], side="sell",
        decision=("opening_event_t_sell" if opening_event else "intraday_t_sell"),
        reason=trigger, asof_date=asof_day, quote=quote,
        kline=_completed_kline(position["code"], asof_day, inclusive=False),
    )
    order_reason = "开盘冲高回落减仓：共享事件引擎" if opening_event else "日内做T高抛：仅卖出已结算底仓"
    audit_action = "opening_event_t_sell" if opening_event else "intraday_t_sell"
    cursor = conn.execute(
        """INSERT INTO paper_orders(account_id,side,code,name,qty,planned_price,filled_price,amount,fees,status,reason,risk_payload,realized_pnl,created_at,executed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account["id"], "sell", position["code"], position.get("name"), qty, price, fill, amount, fees,
         "filled", order_reason, _json(payload), pnl, _now(), _now()),
    )
    _credit_shared_cash(conn, amount - fees, account["id"])
    conn.execute("INSERT INTO paper_fills(order_id,account_id,side,code,qty,price,amount,fees,fill_date,quote_at,assumption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (cursor.lastrowid, account["id"], "sell", position["code"], qty, fill, amount, fees,
                 _date(asof_day).isoformat(), quote.get("quote_at"), "开盘/5分钟实时快照高抛，含滑点、佣金、印花税"))
    _risk_log(conn, account["id"], position["code"], "sell", audit_action, "开盘事件高抛通过" if opening_event else "日内高抛通过", payload)
    _audit(conn, account["id"], audit_action, f"{position['code']} {qty}股 @ {fill:.2f}")
    return {
        "order_id": cursor.lastrowid, "side": "sell", "code": position["code"],
        "qty": qty, "pnl": round(pnl, 2), "sell_price": round(fill, 4),
        "opening_event": bool(opening_event), "trigger": trigger,
    }, "高抛成交"


def _intraday_buyback(conn, account, position, quote, market, asof_day, profile, cycle, *, all_quotes=None):
    """回补腿只能对应同日已高抛的库存；回补后仍按 T+1 锁定。"""
    quote_status = _execution_quote_status(quote, asof_day)
    if not quote_status["fresh"]:
        _risk_log(
            conn, account["id"], position["code"], "buy",
            "rejected_stale_quote", quote_status["reason"], quote_status,
        )
        return None, quote_status["reason"]
    sold = _intraday_action_today(conn, cycle["id"], account["id"], position["code"], "t_sell", asof_day)
    if not sold or _intraday_action_today(conn, cycle["id"], account["id"], position["code"], "t_rebuy", asof_day):
        return None, "无待回补的日内卖出"
    addition_allowed, addition_reason = _existing_position_addition_gate(
        conn, account, position["code"], asof_day, quote=quote,
    )
    if not addition_allowed:
        return None, addition_reason
    payload = _loads(sold.get("payload"))
    sold_price, sold_qty = _num(payload.get("sell_price")), int(_num(payload.get("qty")))
    price = _num(quote.get("price"))
    if market.get("light") in ("red", "unknown"):
        return None, "市场门控禁止日内回补"
    confirmation = _sell_rebuy_confirmation(conn, sold, quote, asof_day, account)
    if not confirmation.get("passed"):
        return None, confirmation.get("reason") or "等待回补确认"
    rebuy_ratio = _num(
        (OPENING_EVENT_POLICIES.get(account.get("id")) or {}).get("rebuy_max_sold_ratio"),
        0.992,
    ) if payload.get("opening_event") else 0.992
    if price <= 0 or sold_qty < LOT_SIZE or price > sold_price * rebuy_ratio:
        return None, "回落幅度未达到回补阈值"
    shared_positions = _position_rows(conn, asof_day=asof_day)
    # The caller has already fetched the complete same-round quote snapshot
    # before taking the ledger write lock.  Never call a provider from here.
    quotes = dict(all_quotes or {})
    _, value, nav, industries, code_values = _shared_account_exposure(conn, quotes, asof_day)
    shared_cash = _shared_cash(conn)
    strategy_budget = _strategy_pool_budget(conn, account, nav, shared_positions, quotes, market=market)
    shared_risk = _shared_risk_state(conn, account, nav, asof_day)
    if shared_risk["blocked"]:
        return None, "；".join(shared_risk["reasons"])
    code_value = code_values.get(position["code"], 0.0)
    qty, sizing = _price_aware_qty(
        nav, shared_cash, value, industries.get(position.get("industry") or "未知", 0.0),
        code_value, price * (1 + SLIPPAGE), ACCOUNT_SPECS[account["id"]]["hard_stop"], profile,
        exposure_cap=SHARED_POOL_MAX_EXPOSURE,
        max_exposure_cap=SHARED_POOL_MAX_EXPOSURE,
        strategy_position_value=strategy_budget["current_amount"],
        strategy_cap_amount=strategy_budget["absolute_cap_amount"],
        pool_cap_amount=strategy_budget["pool_cap_amount"],
        pending_strategy_amount=strategy_budget.get("pending_reserve_amount", 0.0),
        pending_pool_amount=strategy_budget.get("pending_pool_reserve_amount", 0.0),
    )
    sizing["strategy_budget"] = strategy_budget
    qty = min(qty, sold_qty)
    if qty < LOT_SIZE:
        return None, "价格或风控预算不足以回补一手"
    fill = price * (1 + SLIPPAGE)
    amount = qty * fill
    fees = _commission(amount)
    if amount + fees > shared_cash:
        return None, "共享资金池可用现金不足"
    if _entry_freeze_enabled():
        reason = _entry_frozen_reason("日内回补")
        order_id, created, _reason, payload = _record_entry_frozen_waitlist(
            conn, account["id"], position["code"],
            name=position.get("name"),
            qty=qty,
            planned_price=_num(quote.get("price")),
            risk_payload={
                "kind": "intraday_t_rebuy",
                "paired_sell_observation": sold.get("id"),
                "quote": quote,
            },
            asof_day=asof_day,
            source="日内回补",
        )
        if not created:
            _risk_log(
                conn, account["id"], position["code"], "buy",
                ENTRY_FROZEN_WAITLIST_STATUS, reason, payload,
            )
        return {
            "filled": False, "deferred": True, "waitlisted": True,
            "status": ENTRY_FROZEN_WAITLIST_STATUS, "order_id": order_id,
            "side": "buy", "code": position["code"], "qty": qty,
        }, reason
    detail = {"kind": "stock_inventory_t", "paired_sell_observation": sold["id"], "qty": qty,
              "sell_price": sold_price, "buy_price": round(fill, 4), "quote_at": quote.get("quote_at"),
              "opening_event": bool(payload.get("opening_event")), "confirmation": confirmation,
              "sizing": sizing}
    detail = _with_decision_snapshot(
        detail, account_id=account["id"], code=position["code"], side="buy",
        decision=("opening_event_rebuy" if payload.get("opening_event") else "intraday_t_rebuy"),
        reason=confirmation.get("reason"), asof_date=asof_day, quote=quote,
        kline=_completed_kline(position["code"], asof_day, inclusive=False),
    )
    action_name = "opening_event_rebuy" if payload.get("opening_event") else "intraday_t_rebuy"
    commit_result, failure = _commit_strategy_buy(
        conn, account,
        {
            "code": position["code"], "name": position.get("name"),
            "industry": position.get("industry"), "qty": qty,
            "planned_price": price, "fill_price": fill, "amount": amount,
            "fees": fees, "quote_at": quote.get("quote_at"),
        },
        asof_day,
        reason=("确认后回补：新买份额次日可卖"
                if payload.get("opening_event") else "日内做T回补：新买份额次日可卖"),
        detail=detail, action=action_name, is_t_base=False,
        assumption="实时双源快照回补 + 滑点模拟；股票份额次日可卖",
    )
    if failure:
        return None, failure
    return commit_result, "回补成交"


def _swing_scale_in(conn, account, position, quote, market, asof_day, profile, cycle, *, all_quotes=None):
    """趋势/板块策略的单日一次确认加仓；始终受原有仓位和风险预算约束。"""
    if account.get("mode") == "intraday_t":
        return None, "日内做T使用专用高抛回补规则"
    addition_allowed, addition_reason = _existing_position_addition_gate(
        conn, account, position["code"], asof_day, quote=quote,
    )
    if not addition_allowed:
        return None, addition_reason
    if _intraday_action_today(conn, cycle["id"], account["id"], position["code"], "swing_scale_in", asof_day):
        return None, "本标的今日已完成确认加仓"
    if market.get("light") in ("red", "unknown"):
        return None, "市场门控不允许加仓"
    quote_status = _execution_quote_status(quote, asof_day)
    if not quote_status["fresh"]:
        return None, quote_status["reason"]
    price, pct, cost = _num(quote.get("price")), _num(quote.get("pct")), _num(position.get("cost"))
    if price <= 0 or cost <= 0:
        return None, "缺少有效价格或成本"
    account_id = account["id"]
    if account_id == "trend_pullback":
        # 首笔是回踩试仓。只有成本上方重新站稳、且不是末端拉升时，才
        # 补齐同等规模；这不是把下跌中的仓位机械摊平。
        passed = price >= cost * 1.015 and 0.0 <= pct <= 2.2
        scale, label = 1.0, "趋势回踩确认加仓（补齐试仓）"
    elif account_id == "sector_rotation":
        # P1 审计修复（2026-09-02）：此前只读 main_net_pct 单字段，行情源
        # 仅提供 main_pct 时该字段缺失、_num 默认 0 使 `>= -2.0` 恒真，
        # 资金确认门失效。改为与全代码一致的双字段回退，且两者都缺失时
        # fail-closed：缺少资金证据不放行热点延续加仓。
        main_pct = _num(quote.get("main_pct"), _num(quote.get("main_net_pct"), None))
        passed = (
            price >= cost * 1.008
            and 0.2 <= pct <= 4.5
            and main_pct is not None
            and main_pct >= -2.0
        )
        scale, label = 0.30, "热点延续加仓"
    else:
        return None, "当前策略未配置波段加仓"
    if not passed:
        return None, "未达到策略专属的确认加仓条件"
    positions = _position_rows(conn, asof_day=asof_day)
    # Reuse the pre-fetched quote map; provider/disk I/O is forbidden while
    # the strategy/stock ledger transaction is open.
    quotes = dict(all_quotes or {})
    _, position_value, nav, industries, code_values = _shared_account_exposure(conn, quotes, asof_day)
    shared_cash = _shared_cash(conn)
    strategy_budget = _strategy_pool_budget(conn, account, nav, positions, quotes, market=market)
    risk_state = _shared_risk_state(conn, account, nav, asof_day)
    if risk_state["blocked"]:
        return None, "；".join(risk_state["reasons"])
    code_value = code_values.get(position["code"], 0.0)
    fill = price * (1 + SLIPPAGE)
    qty, sizing = _price_aware_qty(
        nav, shared_cash, position_value,
        industries.get(position.get("industry") or "未知", 0.0), code_value, fill,
        ACCOUNT_SPECS[account_id]["hard_stop"], profile,
        exposure_cap=SHARED_POOL_MAX_EXPOSURE,
        max_exposure_cap=SHARED_POOL_MAX_EXPOSURE,
        strategy_position_value=strategy_budget["current_amount"],
        strategy_cap_amount=strategy_budget["absolute_cap_amount"],
        pool_cap_amount=strategy_budget["pool_cap_amount"],
        pending_strategy_amount=strategy_budget.get("pending_reserve_amount", 0.0),
        pending_pool_amount=strategy_budget.get("pending_pool_reserve_amount", 0.0),
    )
    sizing["strategy_budget"] = strategy_budget
    # 趋势策略只补齐首笔观察仓：上限为现有可识别持仓规模，不能因一次
    # 确认把单票直接推到整个账户的最大额度。板块策略仍按其独立小仓确认。
    if account_id == "trend_pullback":
        tranche = int((_num(position.get("qty")) * scale) / LOT_SIZE) * LOT_SIZE
    else:
        tranche = int((nav * scale / fill) / LOT_SIZE) * LOT_SIZE
    qty = min(qty, tranche)
    if qty < LOT_SIZE:
        return None, "剩余风险预算或仓位空间不足一手"
    amount, fees = qty * fill, _commission(qty * fill)
    if amount + fees > shared_cash:
        return None, "共享资金池可用现金不足"
    if _entry_freeze_enabled():
        reason = _entry_frozen_reason("策略确认加仓")
        order_id, created, _reason, payload = _record_entry_frozen_waitlist(
            conn, account["id"], position["code"],
            name=position.get("name"), qty=qty,
            planned_price=_num(quote.get("price")),
            risk_payload={
                "kind": "swing_scale_in",
                "quote": quote,
            },
            asof_day=asof_day,
            source="策略确认加仓",
        )
        if not created:
            _risk_log(
                conn, account["id"], position["code"], "buy",
                ENTRY_FROZEN_WAITLIST_STATUS, reason, payload,
            )
        return {
            "filled": False, "deferred": True, "waitlisted": True,
            "status": ENTRY_FROZEN_WAITLIST_STATUS, "order_id": order_id,
            "side": "buy", "code": position["code"], "qty": qty,
        }, reason
    detail = {
        "kind": "swing_scale_in", "label": label, "qty": qty, "price": round(fill, 4),
        "pct": pct, "cost": cost, "quote_at": quote.get("quote_at"), "sizing": sizing,
    }
    detail = _with_decision_snapshot(
        detail, account_id=account_id, code=position["code"], side="buy",
        decision="swing_scale_in", reason=label, asof_date=asof_day,
        quote=quote, kline=_completed_kline(position["code"], asof_day, inclusive=False),
        final_score=sizing.get("qty_final"),
    )
    commit_result, failure = _commit_strategy_buy(
        conn, account,
        {
            "code": position["code"], "name": position.get("name"),
            "industry": position.get("industry"), "qty": qty,
            "planned_price": price, "fill_price": fill, "amount": amount,
            "fees": fees, "quote_at": quote.get("quote_at"),
        },
        asof_day, reason=label, detail=detail, action="swing_scale_in",
        is_t_base=True,
        assumption=f"{label}；实时双源行情含滑点和佣金",
    )
    if failure:
        return None, failure
    return commit_result, label


def monitor_opening_events(asof_date=None, event_clock=None):
    """扫描开盘冲高/回落事件；四策略共用识别器，各自使用独立策略阈值。"""
    init_db()
    day = _date(asof_date)
    clock = str(event_clock or dt.datetime.now().strftime("%H:%M"))
    if clock not in OPENING_EVENT_CLOCKS:
        return {"status": "skipped", "slot": "opening_event", "date": day.isoformat(), "clock": clock,
                "reason": "不在开盘事件窗口"}
    with _db() as snapshot_conn:
        cycle = _active_cycle(snapshot_conn)
        accounts = _rows(snapshot_conn, "SELECT * FROM paper_accounts WHERE status='running'")
        positions = [p for account in accounts for p in _position_rows(snapshot_conn, account["id"], day)]
    if not positions:
        return {"status": "completed", "slot": "opening_event", "date": day.isoformat(),
                "clock": clock, "observed": 0, "orders": []}
    quotes = _quotes(sorted({str(p["code"]) for p in positions}), asof_date=day)
    # P2-1 审计修复（2026-09-02）：开盘事件 NAV 同样用本地快照兜底。
    nav_quotes = _nav_quotes_with_snapshot_fallback(quotes)
    with _db(immediate=True) as conn:
        # Re-read mutable account/position state after network evidence is
        # ready; a concurrent pause/reset can only reduce the work performed.
        cycle = _active_cycle(conn)
        accounts = _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'")
        positions = [p for account in accounts for p in _position_rows(conn, account["id"], day)]
        if not positions:
            return {"status": "completed", "slot": "opening_event", "date": day.isoformat(),
                    "clock": clock, "observed": 0, "orders": []}
        account_map = {a["id"]: a for a in accounts}
        orders = []
        observed = 0
        for position in positions:
            _assert_active_lease(conn, "opening-event position")
            account = account_map.get(position["account_id"])
            if not account:
                continue
            quote = quotes.get(str(position["code"]), {})
            action, reason = _intraday_sell(
                conn, account, position, quote, day, _risk_profile(account), cycle,
                opening_event=True,
            )
            price = _num(quote.get("price"))
            observed += 1
            if action:
                _observe_intraday(
                    conn, cycle["id"], account["id"], position["code"], price,
                    "t_sell", reason, {**action, "trigger": "opening_event", "quote_at": quote.get("quote_at"),
                                       "opening_event": True,
                                       "quote_high": _num(quote.get("high")), "quote_low": _num(quote.get("low"))},
                )
                orders.append({**action, "strategy": account["id"], "trigger": "opening_event"})
            else:
                _observe_intraday(
                    conn, cycle["id"], account["id"], position["code"], price,
                    "opening_event_observe", reason,
                    {"trigger": "opening_event", "quote_at": quote.get("quote_at"),
                     "quote_validation": quote.get("quote_validation"),
                     "quote_high": _num(quote.get("high")), "quote_low": _num(quote.get("low"))},
                )
        _sync_positions(conn, asof_day=day)
        _record_nav(conn, day, quotes=nav_quotes)
        return {"status": "completed", "slot": "opening_event", "date": day.isoformat(),
                "clock": clock, "observed": observed, "orders": orders,
                "engine": "shared_opening_event_v1"}


def _validated_live_universe(rows, asof_day, max_quote_age_minutes=None):
    """Return only same-day, usable full-market rows for candidate scanning.

    Individual holding exits can safely use their dedicated two-source quote
    checks.  A cross-sectional scan is different: a partial or yesterday's
    snapshot must never be allowed to rank the whole market as if it were
    complete.
    """
    expected_day = _date(asof_day).isoformat()
    now_utc = dt.datetime.now(dt.timezone.utc)
    valid = []
    for row in rows or []:
        code = str(row.get("code") or "")
        quote_day = str(row.get("quote_at") or "")[:10]
        quote_age_ok = True
        if max_quote_age_minutes is not None:
            try:
                parsed = dt.datetime.fromisoformat(str(row.get("quote_at") or "").replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    # Source timestamps without an offset are interpreted as
                    # Asia/Shanghai by the production container.  Do not
                    # silently compare a naive local value with UTC.
                    parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                age_seconds = (now_utc - parsed.astimezone(dt.timezone.utc)).total_seconds()
                # A small future skew is tolerated for provider clock drift,
                # while an old quote is never allowed into a cross-sectional
                # ranking scan.
                quote_age_ok = -120 <= age_seconds <= max_quote_age_minutes * 60
            except (TypeError, ValueError):
                quote_age_ok = False
        if (
            len(code) == 6 and code.isdigit()
            and quote_day == expected_day
            and quote_age_ok
            and _num(row.get("price"), 0) > 0
            and _num(row.get("pct"), None) is not None
        ):
            valid.append(row)
    return valid


def _live_scan_gate(live_universe, expected_day):
    """Return a set-based live coverage decision for cross-sectional scans.

    A fixed row count is unsafe because the eligible universe changes with
    listings and permission filters.  Historical rows may provide metadata,
    but every eligible code must have a validated same-day live quote before
    it can enter ranking.
    """
    eligible = set()
    for row in (U.load_universe() or []):
        code = str(row.get("code") or "")
        if code and _security_scope(code, row.get("name"), row.get("risk_flag"))["allowed"]:
            eligible.add(code)
    live_codes = {str(row.get("code") or "") for row in (live_universe or [])}
    covered = eligible & live_codes
    coverage = len(covered) / max(len(eligible), 1)
    required = max(4000, int(len(eligible) * 0.90 + 0.9999))
    return {
        "ready": len(covered) >= required and coverage >= 0.90,
        "eligible_codes": len(eligible),
        "covered_codes": len(covered),
        "missing_codes": len(eligible - live_codes),
        "coverage_pct": round(coverage * 100, 2),
        "required_codes": required,
        "expected_day": str(expected_day),
    }


def monitor_fast_entries(asof_datetime=None, force=False):
    """每 30 秒复核少量候选；只确认并排队，不直接创建委托。"""
    init_db()
    now = asof_datetime if isinstance(asof_datetime, dt.datetime) else dt.datetime.now()
    day = now.date()
    clock = now.strftime("%H:%M")
    if not force and (
        not _is_trade_weekday(day)
        or not any(start <= clock <= end for start, end in INTRADAY_WINDOWS)
    ):
        return {"status": "skipped", "slot": "fast-entry", "reason": "非交易时段"}

    lock_key = "paper-runner-global"
    owner_seed = f"fast-entry:{now.isoformat()}:{os.getpid()}"
    owner_key = hashlib.sha1(owner_seed.encode("utf-8")).hexdigest()[:20]
    with _db(immediate=True) as lock_conn:
        acquired, owner, expires_at = _claim_runtime_lease(
            lock_conn, lock_key, owner_key, "fast-entry", ttl_seconds=90,
        )
        if not acquired:
            return {
                "status": "in_progress", "slot": "fast-entry",
                "reason": "全市场/风控任务运行中，本次快速复核让路",
                "lease_owner": owner, "lease_expires_at": expires_at,
            }
        lease = lock_conn.execute(
            "SELECT fencing_token FROM paper_runtime_locks WHERE lock_key=? AND owner_key=?",
            (lock_key, owner),
        ).fetchone()
        fencing_token = int(lease[0] or 0)
    _set_lease_context(lock_key, owner, fencing_token)

    try:
        retry_placeholders = ",".join("?" for _ in ENTRY_RETRY_SIGNAL_STATUSES)
        with _db() as snapshot_conn:
            accounts = {
                row["id"]: row for row in _rows(
                    snapshot_conn, "SELECT * FROM paper_accounts WHERE status='running'"
                )
            }
            # 快速通道覆盖两类候选：
            # 1) 所有策略被"入场时机"拦下的信号（原有逻辑）；
            # 2) 主力策略当日观察池信号——主力候选多数被容量/资金拦下而非
            #    入场时机，若只扫第一类，主力股票永远进不了 30 秒通道
            #    （2026-08-31 复核确认的 P0）。
            candidates = _rows(
                snapshot_conn,
                f"""SELECT s.* FROM paper_signals s
                    JOIN (
                        SELECT account_id,code,MAX(id) AS max_id
                        FROM paper_signals
                        WHERE intended_date=? AND status IN ({retry_placeholders})
                          AND reason LIKE '%入场时机%'
                        GROUP BY account_id,code
                    ) latest ON latest.max_id=s.id
                    ORDER BY s.t_score DESC,s.rank_score DESC,s.id DESC LIMIT 20""",
                (day.isoformat(), *ENTRY_RETRY_SIGNAL_STATUSES),
            )
            main_force_candidates = _rows(
                snapshot_conn,
                f"""SELECT s.* FROM paper_signals s
                    JOIN (
                        SELECT account_id,code,MAX(id) AS max_id
                        FROM paper_signals
                        WHERE intended_date=? AND status IN ({retry_placeholders})
                          AND account_id=?
                        GROUP BY account_id,code
                    ) latest ON latest.max_id=s.id
                    ORDER BY s.t_score DESC,s.rank_score DESC,s.id DESC LIMIT 20""",
                (day.isoformat(), *ENTRY_RETRY_SIGNAL_STATUSES, MAIN_FORCE_STRATEGY_ID),
            )
            seen_fast = {(str(row.get("account_id")), str(row.get("code"))) for row in candidates}
            for row in main_force_candidates:
                key = (str(row.get("account_id")), str(row.get("code")))
                if key not in seen_fast:
                    candidates.append(row)
                    seen_fast.add(key)
            positions = _position_rows(snapshot_conn, asof_day=day, readonly=True)
            market = _cached_close_market(snapshot_conn, day, allow_network=False)
        if not candidates:
            return {"status": "completed", "slot": "fast-entry", "checked": 0, "confirmed": []}

        candidate_codes = {str(row.get("code") or "") for row in candidates}
        all_codes = sorted(candidate_codes | {str(row.get("code") or "") for row in positions})
        quote_map = _quotes(all_codes, asof_date=day)
        news = _news_for({
            str(row.get("code")): row.get("name") or row.get("code") for row in candidates
        })
        confirmed = []
        observations = []
        checked = 0
        with _db(immediate=True) as conn:
            for signal in candidates:
                _assert_active_lease(conn, "fast entry candidate")
                account = accounts.get(signal.get("account_id"))
                code = str(signal.get("code") or "")
                quote = dict(quote_map.get(code) or {})
                if not account or _num(quote.get("price"), 0.0) <= 0:
                    continue
                payload = _loads(signal.get("payload"), {})
                decision = payload.get("decision") or {}
                entry_model = decision.get("entry_model") or {}
                micro = entry_model.get("microstructure") or {}
                execution_quote = _execution_quote_status(quote, day, purpose="entry")
                evidence = {
                    "cross_source_checked": execution_quote.get("status") == "cross_source_checked",
                    "main_pct": _num(quote.get("main_pct"), _num(quote.get("main_net_pct"))),
                    "vol_ratio": _num(quote.get("vol_ratio")),
                    "active_buy_sell_imbalance": micro.get("active_buy_sell_imbalance"),
                    "depth_imbalance": micro.get("depth_imbalance"),
                }
                if account["id"] == MAIN_FORCE_STRATEGY_ID:
                    # 主力点火候选：30 秒通道负责预判八项点火条件。数据缺失
                    # 时 evaluate_ignition 返回失败——点火通道 fail-closed。
                    ignition = {"passed": False, "reasons": ["ignition_entry 模块不可用"]}
                    if IGN is not None:
                        try:
                            ignition = IGN.evaluate_ignition(
                                code, pct=_num(quote.get("pct")),
                                limit_pct=_limit_pct(code),
                            )
                        except Exception as exc:
                            ignition = {"passed": False, "reasons": [f"点火判定异常: {type(exc).__name__}"]}
                    evidence["ignition_ok"] = bool(ignition.get("passed"))
                    evidence["ignition_detail"] = (
                        "；".join(ignition.get("reasons") or []) or "八项点火条件成立"
                    )
                allowed, timing_info = ET.evaluate(
                    account["id"], code, _num(quote.get("price")), _num(quote.get("pct")),
                    fast=True, evidence=evidence,
                )
                checked += 1
                observation = {
                    "code": code, "account_id": account["id"],
                    "allowed": bool(allowed), "timing": timing_info,
                    "quote_status": execution_quote.get("status"),
                }
                observations.append(observation)
                if allowed:
                    marker = {
                        "version": "fast-entry-priority-v1",
                        "confirmed": True,
                        "confirmed_at": now.isoformat(timespec="seconds"),
                        "valid_until": (now + dt.timedelta(minutes=7)).isoformat(timespec="seconds"),
                        "priority": "highest",
                        "execution_override": False,
                        "reason": "30秒快速通道确认；下一轮三分钟正式任务最高优先级完整复核",
                    }
                    payload["fast_entry_priority"] = marker
                    pick = dict(payload.get("pick") or {})
                    pick["fast_entry_priority"] = marker
                    payload["pick"] = pick
                    payload["fast_entry_observation"] = observation
                    conn.execute(
                        "UPDATE paper_signals SET status='deferred_capacity',reason=?,payload=? WHERE id=?",
                        ("快速通道已确认；等待下一轮三分钟任务最高优先级复核", _json(payload), signal["id"]),
                    )
                    confirmed.append({"code": code, "account_id": account["id"], "valid_until": marker["valid_until"]})
                else:
                    payload["fast_entry_observation"] = observation
                    conn.execute(
                        "UPDATE paper_signals SET payload=? WHERE id=?",
                        (_json(payload), signal["id"]),
                    )
            _audit(conn, None, "fast_entry_monitor", _json({
                "checked": checked, "confirmed": len(confirmed), "orders_created": 0,
                "candidate_codes": sorted(candidate_codes),
                "interval_seconds": 30,
            }))
        return {
            "status": "completed", "slot": "fast-entry", "date": day.isoformat(),
            "checked": checked, "confirmed": confirmed, "observations": observations,
            "orders": [],
            "note": "只确认并排队；下一轮三分钟任务最高优先级完整复核后才可下单",
        }
    finally:
        try:
            with _db(immediate=True) as release_conn:
                _release_runtime_lease(
                    release_conn, lock_key, owner, fencing_token,
                )
        finally:
            _clear_lease_context()


def monitor_intraday(asof_datetime=None, force=False):
    """每三分钟观察库存；09:30/09:31/13:00 先执行共享开盘事件扫描。"""
    init_db()
    now = asof_datetime if isinstance(asof_datetime, dt.datetime) else dt.datetime.now()
    day = now.date()
    clock = now.strftime("%H:%M")
    in_window = any(start <= clock <= end for start, end in INTRADAY_WINDOWS)
    if not force and (not _is_trade_weekday(day) or not in_window):
        return {"slot": "intraday", "status": "skipped", "reason": "非交易时段"}
    # 每日一次过期数据归档清理（供自进化学习）；函数内部按自然日幂等，
    # 仅在交易时段内执行，避免非交易夜间的重复空跑。
    try:
        _cleanup_stale_data()
    except Exception as exc:
        try:
            with _db() as clean_conn:
                _audit(clean_conn, None, "cleanup_stale_data_error", _json({"error": f"{type(exc).__name__}: {exc}"}))
        except Exception:
            pass
    # 每轮 3 分钟扫描先做轻量数据源探活；失败时 data_fetcher 会关闭连接池、
    # 清空熔断状态并轮换东方财富/腾讯重试，随后本轮仍使用可用源继续执行。
    try:
        data_source_health = dfc.check_data_source_health(force=True)
    except Exception as exc:
        data_source_health = {
            "healthy": False, "reconnected": False, "attempts": 0,
            "action": f"健康检查异常：{type(exc).__name__}: {exc}",
        }
    try:
        live_universe = _validated_live_universe(
            dfc.fetch_market_snapshot_full(max_age=240), day, max_quote_age_minutes=20,
        )
    except Exception:
        live_universe = []
    # A non-empty but partial response is a dangerous failure mode: it looks
    # normal in a UI but silently narrows the market scan.  Force one source
    # recovery/refresh before refusing only the candidate scan for this round.
    if len(live_universe) < 1000:
        try:
            data_source_health = dfc.check_data_source_health(force=True)
        except Exception as exc:
            data_source_health = {
                "healthy": False, "reconnected": False, "attempts": 0,
                "action": f"二次重连异常：{type(exc).__name__}: {exc}",
            }
        try:
            live_universe = _validated_live_universe(
                dfc.fetch_market_snapshot_full(max_age=0, force=True), day, max_quote_age_minutes=20,
            )
        except Exception:
            live_universe = []
    live_gate = _live_scan_gate(live_universe, day)
    scan_ready = bool(live_gate["ready"])
    scan_block = (
        None if scan_ready else
        f"全市场实时行情覆盖 {live_gate['covered_codes']}/{live_gate['eligible_codes']}（{live_gate['coverage_pct']:.1f}%），需至少 {live_gate['required_codes']} 只；本轮停止候选扫描，不使用旧快照"
    )
    # 门禁决策每轮落审计：杜绝“静默全停”，正是候选冻结能潜伏 27 小时未被发现的根因。
    try:
        with _db() as gate_conn:
            _audit(gate_conn, None, "live_scan_gate", _json({
                "ready": scan_ready,
                "covered_codes": live_gate["covered_codes"],
                "eligible_codes": live_gate["eligible_codes"],
                "coverage_pct": live_gate["coverage_pct"],
                "required_codes": live_gate["required_codes"],
                "reason": scan_block,
            }))
    except Exception:
        pass
    # 影子对比回填（点火规则 vs 原规则）：每轮扫描检查 +30/+60 分钟到期
    # 的影子记录并回填表现；fail-open，绝不阻塞主扫描。
    try:
        _ignition_shadow_backfill(asof_day=day)
    except Exception:
        pass
    # 覆盖率不足时可以记录最近一次达标快照的诊断，但不能把最多 180
    # 分钟旧数据重新宣布为本轮正式实时候选。风险卖出已在上面独立执行；
    # 本轮只保留等待池，直到同日新鲜全市场快照恢复。
    if not scan_ready:
        try:
            fb_rows = dfc.fetch_market_snapshot(pages=None, allow_disk_fallback=True)
            fb_universe = _validated_live_universe(fb_rows, day, max_quote_age_minutes=180)
            fb_gate = _live_scan_gate(fb_universe, day)
            if fb_gate["ready"]:
                with _db() as fb_conn:
                    _audit(fb_conn, None, "live_scan_gate_fallback", _json({
                        "fallback_covered": fb_gate["covered_codes"],
                        "fallback_eligible": fb_gate["eligible_codes"],
                        "fallback_coverage_pct": fb_gate["coverage_pct"],
                        "formal_candidate_scan": False,
                        "reason": "实时覆盖率不足；旧快照仅用于诊断，不进入正式候选/买入",
                    }))
        except Exception as exc:
            try:
                with _db() as fb_err_conn:
                    _audit(fb_err_conn, None, "live_scan_gate_fallback_error", _json({
                        "error": f"{type(exc).__name__}: {exc}",
                    }))
            except Exception:
                pass
    # 09:30/13:00 既刷新候选，也执行一次共享开盘事件引擎；普通新开仓仍由
    # 09:31 开盘审批负责，避免把“事件减仓”误变成无风控追涨。
    if clock in {"09:30", "13:00"}:
        # 午后窗口同样先跑完整持仓风控；本分支随后只生成候选/事件，
        # 不得让待买或事件处理抢在风险退出之前。
        opening_risk = monitor_risk(day)
        opening_event = monitor_opening_events(day, event_clock=clock)
        bootstrap = (
            _bootstrap_signals_for_today(day, live_universe=live_universe, source_slot="open_scan")
            if scan_ready else {"status": "blocked", "reason": scan_block, "accounts": []}
        )
        with _db() as conn:
            accounts = _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'")
            market = _cached_close_market(conn, day)
        return {
            "slot": "intraday", "date": day.isoformat(),
            "observed": opening_event.get("observed", 0),
            "orders": list(opening_risk.get("orders", [])) + list(opening_event.get("orders", [])),
            "scan_only": not bool(opening_event.get("orders")),
            "reason": "开市即时扫描和冲高回落事件已完成；普通开仓仍等待开盘审批",
            "market": market, "bootstrap": bootstrap,
            "opening_event": opening_event,
            "risk": opening_risk,
            "running_accounts": len(accounts), "data_source_health": data_source_health,
            "live_universe_coverage": len(live_universe),
        }
    # Always execute the risk pass.  The monitor itself uses lots as the
    # settlement source of truth and returns an empty result when there are no
    # active lots.  Skipping the pass based on a stale aggregate projection
    # previously suppressed scan markers, recovery bookkeeping and pending
    # manual-order handling after a restart.
    risk_result = monitor_risk(day)
    bootstrap = (
        _bootstrap_signals_for_today(day, live_universe=live_universe)
        if scan_ready else {"status": "blocked", "reason": scan_block, "accounts": []}
    )
    open_result = (
        execute_open(day)
        if scan_ready else
        {"status": "blocked", "reason": scan_block, "orders": [], "manual_orders": [],
         "opening_event": {"orders": []}, "live_scan_gate": live_gate}
    )
    opening_orders = list(open_result.get("orders", []))
    # Reuse the already validated, same-round full-market snapshot.  Reading
    # the previous close snapshot here made live market state ``unknown`` and
    # silently disabled intraday buyback/scale-in even when the scan itself
    # had fresh coverage.
    # This is intentionally before the ledger write transaction.  It may
    # refresh the index/overseas context, while the breadth calculation stays
    # pinned to ``live_universe`` from this scan.
    live_market = _market_state(day, live_universe=live_universe, allow_network=True)
    with _db() as snapshot_conn:
        cycle = _active_cycle(snapshot_conn)
        accounts = _rows(snapshot_conn, "SELECT * FROM paper_accounts WHERE status='running'")
        positions = [p for account in accounts for p in _position_rows(snapshot_conn, account["id"], day)]
    if not positions:
        return {
            "slot": "intraday", "date": day.isoformat(), "observed": 0,
            "orders": list(risk_result.get("orders", [])) + opening_orders,
            "reason": "首次建仓候选已检查，暂无可做T底仓",
            "bootstrap": bootstrap,
            "opening_event": open_result.get("opening_event"),
            "data_source_health": data_source_health,
            "live_universe_coverage": len(live_universe),
        }
    quotes = _quotes(sorted({p["code"] for p in positions}), asof_date=day)
    # P2-1 审计修复（2026-09-02）：NAV 估值副本用本地快照最近价补齐
    # _quotes 因防陈旧而置 None 的持仓价格（估值非成交，动作循环仍用
    # 未污染的 quotes）。必须在写事务外生成——内部读取本地快照文件。
    nav_quotes = _nav_quotes_with_snapshot_fallback(quotes)
    with _db(immediate=True) as conn:
        cycle = _active_cycle(conn)
        accounts = _rows(conn, "SELECT * FROM paper_accounts WHERE status='running'")
        positions = [p for account in accounts for p in _position_rows(conn, account["id"], day)]
        if not positions:
            return {
                "slot": "intraday", "date": day.isoformat(), "observed": 0,
                "orders": list(risk_result.get("orders", [])) + opening_orders,
                "reason": "持仓在行情抓取期间已被平仓或暂停",
                "bootstrap": bootstrap,
                "opening_event": open_result.get("opening_event"),
                "data_source_health": data_source_health,
                "live_universe_coverage": len(live_universe),
            }
        account_map = {a["id"]: a for a in accounts}
        _, shared_position_value, shared_nav, _, _ = _shared_account_exposure(conn, quotes, day)
        actions = list(risk_result.get("orders", [])) + opening_orders

        def _run_intraday_action(position, action_fn, side):
            """Isolate one strategy/stock action from the rest of the pass."""
            savepoint = f"intraday_pos_{position['account_id']}_{position['code']}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                result = action_fn()
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                return result
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if _lease_lost(exc):
                    raise
                reason = f"盘中单票动作失败，可重试：{type(exc).__name__}: {exc}"
                _risk_log(
                    conn, position["account_id"], position["code"],
                    side,
                    "execution_retry", reason, {"error": str(exc), "retryable": True},
                )
                return None, reason

        for position in positions:
            _assert_active_lease(conn, "intraday position")
            account = account_map[position["account_id"]]
            quote = quotes.get(position["code"], {})
            price = _num(quote.get("price"))
            profile = _risk_profile(account)
            risk_state = _shared_risk_state(conn, account, shared_nav, day)
            reason = "共享风控状态正常，等待策略专属事件或加仓条件"
            if account.get("mode") == "intraday_t":
                action, reason = _run_intraday_action(
                    position,
                    lambda: _intraday_sell(conn, account, position, quote, day, profile, cycle),
                    "sell",
                )
                if action:
                    _observe_intraday(conn, cycle["id"], account["id"], position["code"], price, "t_sell", reason, {**action, "quote_at": quote.get("quote_at")})
                    actions.append(action)
                    continue
                if not risk_state["blocked"]:
                    action, reason = _run_intraday_action(
                        position,
                        lambda: _intraday_buyback(
                            conn, account, position, quote, live_market, day, profile, cycle,
                            all_quotes=quotes,
                        ),
                        "buy",
                    )
                    if action:
                        _observe_intraday(conn, cycle["id"], account["id"], position["code"], price, "t_rebuy", reason, {**action, "quote_at": quote.get("quote_at")})
                        actions.append(action)
                        continue
            elif not risk_state["blocked"]:
                # 趋势/轮动也可使用共享事件引擎产生的同日卖出，但回补仍需
                # 各自策略质量门和“卖出后低点反弹”确认，不复用普通开仓。
                action, reason = _run_intraday_action(
                    position,
                    lambda: _intraday_buyback(
                        conn, account, position, quote, live_market, day, profile, cycle,
                        all_quotes=quotes,
                    ),
                    "buy",
                )
                if action:
                    _observe_intraday(conn, cycle["id"], account["id"], position["code"], price, "t_rebuy", reason, {**action, "quote_at": quote.get("quote_at")})
                    actions.append(action)
                    continue
                action, reason = _run_intraday_action(
                    position,
                    lambda: _swing_scale_in(
                        conn, account, position, quote, live_market, day, profile, cycle,
                        all_quotes=quotes,
                    ),
                    "buy",
                )
                if action:
                    _observe_intraday(conn, cycle["id"], account["id"], position["code"], price, "swing_scale_in", reason, {**action, "quote_at": quote.get("quote_at")})
                    actions.append(action)
                    continue
            _observe_intraday(conn, cycle["id"], account["id"], position["code"], price, "observe", reason, {
                "quote_at": quote.get("quote_at"), "quote_source": quote.get("quote_source"), "risk": risk_state,
                "available_qty": position.get("available_qty"), "locked_qty": position.get("locked_qty"),
            })
        _sync_positions(conn, asof_day=day)
        _record_nav(conn, day, quotes=nav_quotes)
        return {"slot": "intraday", "date": day.isoformat(), "observed": len(positions), "orders": actions,
                "market": live_market, "bootstrap": bootstrap,
                "opening_event": open_result.get("opening_event"),
                "data_source_health": data_source_health,
                "live_universe_coverage": len(live_universe),
                "note": "3分钟监控仅观察；未满足阈值时不生成订单"}


def _nav_quotes_with_snapshot_fallback(quotes):
    """P2-1 审计修复（2026-09-02）：NAV 估值专用本地兜底副本。

    东方财富 ulist 实时接口会间歇性对部分代码漏返回 price；_quotes 为
    防“把旧价拼到实时时间戳上虚构成交”会把这类行的 price 置 None。
    该策略对成交/风控正确，但 _record_nav 据此把整仓降级为“按成本估值”，
    导致盘中 NAV 长期失真（2026-09-02 巡检：37/41 轮 cost_fallback）。

    NAV 不是成交：用本地全市场快照缓存的最近价估值是安全且更准确的
    （停牌股按最后成交价计也是标准做法）。本函数只补估值副本，不改动
    原始 quotes——风控/做T路径仍走严格的实时价格门禁。
    内部读取本地快照文件（无网络），必须由调用方在写事务外调用。
    """
    if not quotes:
        return dict(quotes or {})
    merged = dict(quotes)
    missing = [code for code, quote in merged.items()
               if not isinstance((quote or {}).get("price"), (int, float))]
    if not missing:
        return merged
    try:
        local = _latest_price_map(missing)
    except Exception:
        local = {}
    for code in missing:
        lq = local.get(code) or {}
        lprice = lq.get("price")
        if not isinstance(lprice, (int, float)):
            continue  # 连快照都无价（长期停牌等）→ 保持缺失，_record_nav 按成本计
        base = dict(merged.get(code) or {})
        base["price"] = lprice
        if not isinstance(base.get("pct"), (int, float)) and isinstance(lq.get("pct"), (int, float)):
            base["pct"] = lq["pct"]
        base["quote_source"] = "local_snapshot_fallback"
        if not base.get("quote_at") and lq.get("quote_at"):
            base["quote_at"] = lq["quote_at"]
        merged[code] = base
    return merged


def _record_nav(conn, asof_day, quotes=None):
    _assert_active_lease(conn, "NAV write")
    accounts = _rows(conn, "SELECT * FROM paper_accounts")
    all_positions = _position_rows(conn, asof_day=asof_day)
    # NAV persistence is commonly called while the ledger write transaction
    # is open.  Never perform provider or disk I/O while holding that
    # transaction; callers must prefetch and pass the quote snapshot.
    if quotes is None:
        # Keep the write primitive deterministic if an older caller omitted
        # the snapshot.  The resulting NAV is explicitly marked degraded
        # below; it must not silently fetch data under the SQLite lock.
        quotes = {}
    # 缺行情的持仓绝不能静默按成本计价。 由于此函数可能持有 SQLite
    # 写事务，缺失快照只标记为 degraded；补拉必须由调用方在事务外完成。
    missing_codes = sorted({
        p["code"] for p in all_positions
        if not isinstance((quotes.get(p["code"]) or {}).get("price"), (int, float))
    })
    benchmark = _benchmark_close()
    fallback_accounts = []
    for account in accounts:
        account_positions = [p for p in all_positions if p["account_id"] == account["id"]]
        has_missing_quote = bool(account_positions and any(
            not isinstance((quotes.get(p["code"]) or {}).get("price"), (int, float))
            for p in account_positions
        ))
        if has_missing_quote:
            fallback_accounts.append(account["id"])
        value = sum(
            _num((quotes.get(p["code"]) or {}).get("price"), _num(p.get("cost"))) * _num(p.get("qty"))
            for p in account_positions
        )
        unrealized = sum(
            (_num((quotes.get(p["code"]) or {}).get("price"), _num(p.get("cost"))) - _num(p.get("cost")))
            * _num(p.get("qty"))
            for p in account_positions
        )
        realized = _num(conn.execute(
            """SELECT COALESCE(SUM(realized_pnl),0) FROM paper_orders
               WHERE account_id=? AND side='sell' AND status='filled'""",
            (account["id"],),
        ).fetchone()[0])
        # In shared-pool mode account.cash is only an attribution bucket.  A
        # transfer between strategies must never appear as investment return.
        # Persist a synthetic strategy NAV that exactly matches the dashboard:
        # baseline capital + realized P&L + marked open-position P&L.
        nav = _account_reference_capital(account) + realized + unrealized
        conn.execute(
            """INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(account_id,nav_date) DO UPDATE SET cash=excluded.cash,market_value=excluded.market_value,nav=excluded.nav,benchmark=excluded.benchmark,created_at=excluded.created_at""",
            (account["id"], _date(asof_day).isoformat(), _num(account["cash"]), value, nav, benchmark, _now()),
        )
        conn.execute(
            "UPDATE paper_nav SET quote_status=? WHERE account_id=? AND nav_date=?",
            ("cost_fallback" if has_missing_quote else "verified", account["id"], _date(asof_day).isoformat()),
        )
    if fallback_accounts:
        try:
            _audit(
                conn, None, "nav_record_skipped",
                f"{'、'.join(fallback_accounts)} 的持仓行情缺失（{','.join(missing_codes)[:200]}），"
                "本次按成本价一致估值并标记 cost_fallback；行情恢复后下一轮自动重估",
            )
        except Exception:
            pass


def _weekly_review(conn, day):
    week = f"{day.isocalendar().year}-W{day.isocalendar().week:02d}"
    accounts = _rows(conn, "SELECT * FROM paper_accounts")
    output = []
    for account in accounts:
        nav_rows = _rows(conn, "SELECT * FROM paper_nav WHERE account_id=? ORDER BY nav_date", (account["id"],))
        navs = [r["nav"] for r in nav_rows]
        reference_capital = _account_reference_capital(account)
        peak = navs[0] if navs else reference_capital
        max_dd = 0.0
        for nav in navs:
            peak = max(peak, nav)
            max_dd = min(max_dd, nav / peak - 1 if peak else 0)
        fills = _rows(conn, "SELECT * FROM paper_fills WHERE account_id=?", (account["id"],))
        closed = _rows(conn, "SELECT * FROM paper_orders WHERE account_id=? AND side='sell' AND status='filled'", (account["id"],))
        signal_count = conn.execute("SELECT COUNT(*) FROM paper_signals WHERE account_id=?", (account["id"],)).fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status!='filled'", (account["id"],)).fetchone()[0]
        latest_nav = navs[-1] if navs else reference_capital
        ret = latest_nav / max(reference_capital, 1) - 1
        advice = []
        change = None
        if max_dd <= -0.10:
            advice.append("滚动回撤达到进取档位熔断线：保持冷静期，禁止放宽任何参数")
        if len(closed) < 6 or signal_count < 10:
            advice.append("样本不足：本周仅生成报告，不自动调参")
        else:
            params = _loads(account.get("params"), {})
            current = _num(params.get("entry_score_delta"), _num(params.get("min_t_score_delta")))
            next_delta = min(0.03, current + 0.01) if ret < 0 else max(-0.03, current - 0.01)
            if next_delta != current:
                # 模拟盘参数调整当天立即生效，历史成交仍按原版本保留。
                effective = _date(day).isoformat()
                params.pop("min_t_score_delta", None)
                params.update({"entry_score_delta": round(next_delta, 3), "effective_date": effective})
                version = f"v3.{week.split('W')[-1]}"
                reason = "本周负收益，提高策略专属入场门槛" if ret < 0 else "本周正收益，小幅放宽策略专属入场门槛"
                conn.execute("UPDATE paper_accounts SET params=?,version=?,updated_at=? WHERE id=?", (_json(params), version, _now(), account["id"]))
                cycle = _active_cycle(conn)
                conn.execute(
                    "INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cycle["id"], account["id"], version, account.get("style") or "pullback", _json(params), reason, effective, _now()),
                )
                change = f"自动微调：专属入场评分偏移 {current:+.3f} → {next_delta:+.3f}，{effective} 生效"
                advice.append(change)
            else:
                advice.append("参数已达到本周期受限边界，本周不再调整")
        text = (f"# {account['name']} 周度复盘（{week}）\n\n"
                f"- 策略绩效参考资金：{reference_capital:.0f} 元\n"
                f"- 最新净值：{latest_nav:.2f} 元，累计收益：{ret*100:+.2f}%\n"
                f"- 最大回撤：{max_dd*100:.2f}%\n"
                f"- 模拟成交：{len(fills)} 笔；已平仓：{len(closed)} 笔；风控拦截/未成交：{rejected} 笔\n"
                f"- 风格：{STYLE_PROFILES.get(account.get('style'), {}).get('name', account.get('style'))}\n"
                f"- 参数版本：{account['version']}（自动微调当日生效，安全边界不可放宽）\n\n"
                f"## 下周期建议\n" + "\n".join(f"- {x}" for x in advice) + "\n\n"
                "模拟成交采用快照与滑点假设，不代表实际可成交价格；本报告不构成投资建议。\n")
        conn.execute("INSERT INTO paper_reviews(week_key,account_id,report,recommendation,created_at) VALUES(?,?,?,?,?) ON CONFLICT(week_key,account_id) DO UPDATE SET report=excluded.report,recommendation=excluded.recommendation,created_at=excluded.created_at",
                     (week, account["id"], text, "；".join(advice), _now()))
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"paper_{account['id']}_{week}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        output.append({"account_id": account["id"], "week": week, "recommendation": advice})
    return output


def _claim_runtime_lease(conn, lock_key, owner_key, slot, ttl_seconds=720):
    """Atomically claim the one process-wide paper runner lease."""
    now = dt.datetime.now()
    # Keep the same sortable format as _now()/paper_jobs.  Mixing ``T`` and a
    # space makes SQLite's textual ``expires_at < now`` comparison wrong for
    # timestamps on the same day (an expired lease can look live forever).
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_s = (now + dt.timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    owner_value = f"{owner_key}:{now_s}"

    for _retry in range(5):
        try:
            # 1. 尝试更新过期租约
            cursor = conn.execute(
                "UPDATE paper_runtime_locks SET owner_key=?, acquired_at=?, heartbeat_at=?, expires_at=?, slot=?, "
                "fencing_token=COALESCE(fencing_token,0)+1 "
                "WHERE lock_key=? AND (expires_at IS NULL OR expires_at < ?)",
                (owner_value, now_s, now_s, expires_s, slot, lock_key, now_s),
            )
            if cursor.rowcount > 0:
                conn.commit()
                return True, owner_value, expires_s

            # 2. 尝试插入新租约
            try:
                conn.execute(
                    "INSERT INTO paper_runtime_locks(lock_key, owner_key, acquired_at, heartbeat_at, expires_at, slot, fencing_token) VALUES(?,?,?,?,?,?,?)",
                    (lock_key, owner_value, now_s, now_s, expires_s, slot, 1),
                )
                conn.commit()
                return True, owner_value, expires_s
            except sqlite3.IntegrityError:
                # 3. 租约存在且未过期 - 仍由当前 owner 持有；不能按
                # acquired_at 强制抢占，因为心跳可能已经续期。
                row = conn.execute(
                    "SELECT owner_key, acquired_at, heartbeat_at, expires_at, fencing_token FROM paper_runtime_locks WHERE lock_key=?",
                    (lock_key,),
                ).fetchone()
                if row:
                    return False, row[0], row[3]
                return False, "unknown", None
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and _retry < 4:
                time.sleep(2)
                continue
            raise
    return False, "database_locked", None


def _release_runtime_lease(conn, lock_key, owner_key, fencing_token=None):
    """Release only the lease generation that this worker actually owns.

    ``fencing_token`` is optional for compatibility with old maintenance
    scripts; the live runner always supplies it.  A stale worker may therefore
    never delete a newer owner's lease just because it retained the old owner
    string.
    """
    if fencing_token is None:
        conn.execute(
            "DELETE FROM paper_runtime_locks WHERE lock_key=? AND owner_key=?",
            (lock_key, owner_key),
        )
    else:
        conn.execute(
            "DELETE FROM paper_runtime_locks WHERE lock_key=? AND owner_key=? AND fencing_token=?",
            (lock_key, owner_key, int(fencing_token)),
        )




def _cleanup_stale_data():
    """归档过期数据（供自进化学习），并清理活跃表。

    策略：
    - deferred_capacity: 归档到 archive 表，然后从活跃表删除
    - pending: 超过24小时的归档并标记为 expired
    - entry_frozen_waitlist: 归档并标记为 superseded
    - blocked: 超过48小时的归档并标记为 expired

    归档数据保留供自进化系统学习：
    - 哪些信号被延迟执行
    - 容量瓶颈的时间分布
    - 信号质量与执行率的关系
    """
    now = _now()
    today = _date().isoformat()

    # 检查是否今天已经清理过
    with _db() as conn:
        last_cleanup = conn.execute(
            "SELECT created_at FROM paper_audit WHERE event='cleanup_stale_data' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_cleanup:
            last_date = str(last_cleanup[0])[:10]
            if last_date == today:
                return  # 今天已经清理过

    with _db(immediate=True) as conn:
        cleaned = {}

        # 归档 deferred_capacity 订单（超过24小时）
        conn.execute("""
            INSERT OR IGNORE INTO paper_orders_archive
            SELECT * FROM paper_orders
            WHERE status='deferred_capacity' AND created_at < datetime('now', '-1 day')
        """)
        cursor = conn.execute("""
            DELETE FROM paper_orders
            WHERE status='deferred_capacity' AND created_at < datetime('now', '-1 day')
        """)
        cleaned["deferred_capacity_orders"] = cursor.rowcount

        # 归档 deferred_capacity 信号（超过24小时）
        conn.execute("""
            INSERT OR IGNORE INTO paper_signals_archive
            SELECT * FROM paper_signals
            WHERE status='deferred_capacity' AND created_at < datetime('now', '-1 day')
        """)
        cursor = conn.execute("""
            DELETE FROM paper_signals
            WHERE status='deferred_capacity' AND created_at < datetime('now', '-1 day')
        """)
        cleaned["deferred_capacity_signals"] = cursor.rowcount

        # 归档 pending 信号（超过24小时）
        conn.execute("""
            INSERT OR IGNORE INTO paper_signals_archive
            SELECT * FROM paper_signals
            WHERE status='pending' AND created_at < datetime('now', '-1 day')
        """)
        cursor = conn.execute("""
            UPDATE paper_signals SET status='expired', reason='auto-archive: pending超过24小时'
            WHERE status='pending' AND created_at < datetime('now', '-1 day')
        """)
        cleaned["pending_signals"] = cursor.rowcount

        # 归档 entry_frozen_waitlist（超过24小时）
        conn.execute("""
            INSERT OR IGNORE INTO paper_orders_archive
            SELECT * FROM paper_orders
            WHERE status='entry_frozen_waitlist' AND created_at < datetime('now', '-1 day')
        """)
        cursor = conn.execute("""
            UPDATE paper_orders SET status='superseded', reason='auto-archive: entry_frozen_waitlist超过24小时'
            WHERE status='entry_frozen_waitlist' AND created_at < datetime('now', '-1 day')
        """)
        cleaned["entry_frozen_orders"] = cursor.rowcount

        # H3: 冻结长期开启时 waitlist 信号也必须有时间兜底转出，否则活跃表
        # 只进不出——复评环每轮都会把 waitlist 信号重新加入候选，形成永不
        # 解冻的死循环。超过 24 小时仍未解冻视为失效，归档后标记 superseded，
        # 释放对同标的再入场的持续阻断。
        conn.execute("""
            INSERT OR IGNORE INTO paper_signals_archive
            SELECT * FROM paper_signals
            WHERE status='entry_frozen_waitlist' AND created_at < datetime('now', '-1 day')
        """)
        cursor = conn.execute("""
            UPDATE paper_signals SET status='superseded', reason='auto-archive: entry_frozen_waitlist信号超过24小时'
            WHERE status='entry_frozen_waitlist' AND created_at < datetime('now', '-1 day')
        """)
        cleaned["entry_frozen_signals"] = cursor.rowcount

        # 归档 blocked 信号（超过48小时）
        conn.execute("""
            INSERT OR IGNORE INTO paper_signals_archive
            SELECT * FROM paper_signals
            WHERE status='blocked' AND created_at < datetime('now', '-2 days')
        """)
        cursor = conn.execute("""
            UPDATE paper_signals SET status='expired', reason='auto-archive: blocked超过48小时'
            WHERE status='blocked' AND created_at < datetime('now', '-2 days')
        """)
        cleaned["blocked_signals"] = cursor.rowcount

        # 执行失败的策略/手动买单停留在 execution_retry 中间态：信号已回到
        # pending 并会生成新订单，旧行若不收敛会无界累积、污染订单计数。
        # 超 24 小时归档到 archive 表后从活跃表删除。
        for _retry_status in ("execution_retry", "manual_execution_retry"):
            conn.execute(
                """INSERT OR IGNORE INTO paper_orders_archive
                   SELECT * FROM paper_orders
                   WHERE status=? AND created_at < datetime('now', '-1 day')""",
                (_retry_status,),
            )
            cursor = conn.execute(
                """DELETE FROM paper_orders
                   WHERE status=? AND created_at < datetime('now', '-1 day')""",
                (_retry_status,),
            )
            cleaned[f"{_retry_status}_archived"] = cursor.rowcount

        # intraday 分钟级运行记录无界增长：每个交易日约 220 个 key，单行
        # detail JSON 数十 KB。保留 30 天供审计/复盘，超期直接删除（这些
        # 记录只服务于“当日幂等”，历史价值有限；周期归档另有全量清理）。
        cursor = conn.execute("""
            DELETE FROM paper_job_runs
            WHERE market_date < date('now', '-30 days')
        """)
        cleaned["job_runs_purged"] = cursor.rowcount

        # 同步收紧 paper_jobs 的历史深度：保留 180 天，防止多年累积后
        # dashboard 的 ORDER BY 查询变慢。
        cursor = conn.execute("""
            DELETE FROM paper_jobs
            WHERE market_date < date('now', '-180 days')
        """)
        cleaned["jobs_purged"] = cursor.rowcount

        # 记录清理日志
        _audit(conn, None, "cleanup_stale_data", _json(cleaned))
        conn.commit()

    # H9: 归档删除是批量写，显式收缩 WAL，避免 -wal 无界增长。
    _wal_checkpoint()

    return cleaned

def run_slot(slot, asof_date=None, force=False):
    """统一幂等入口；计划任务和页面的“立即检查”都使用同一事务键。"""
    if slot not in {"auction", "open", "risk", "close", "weekly-review", "intraday"}:
        raise ValueError("slot 必须是 auction、open、risk、close、weekly-review 或 intraday")
    init_db()
    day = _date(asof_date)
    if slot == "weekly-review" and not force:
        # Anchor the weekly review to the last *trading* day of the ISO week.
        # Previously a statutory holiday on Friday left the entire week without
        # a review: the cron trigger was rejected by the trading-day guard and
        # the branch itself only accepted weekday()==4.
        try:
            probe = U.next_trade_day(day)
            if probe.isocalendar()[:2] == day.isocalendar()[:2]:
                return {
                    "status": "skipped", "slot": slot,
                    "reason": f"本周仍有交易日 {probe.isoformat()}，复盘延后",
                }
            anchor = day if _is_trade_weekday(day) else U.previous_trade_day(day)
            if anchor.isocalendar()[:2] != day.isocalendar()[:2]:
                return {"status": "skipped", "slot": slot, "reason": "本周无交易日，无需复盘"}
        except RuntimeError:
            anchor = day  # calendar unavailable: keep legacy behavior
        day = anchor
    if not force and not _is_trade_weekday(day):
        return {"status": "skipped", "slot": slot, "reason": "非交易工作日"}
    run_asof = dt.datetime.now()
    intraday_key = _intraday_business_key(run_asof) if slot == "intraday" else None
    max_auto_retries = 2
    retry_count = 0
    # One lease spans every scheduled slot: a 15:05 full-market factor rebuild
    # must not race a manual retry, and an intraday scan that takes longer than
    # three minutes must not start another risk/open loop in parallel.
    runtime_lock_key = "paper-runner-global"
    # H6: 租约 TTL 必须 ≥ 2× 最大单轮耗时，否则扫描尚未结束租约即过期，
    # 下一轮 cron 会抢到过期租约与未结束的 run 并发写候选/订单。
    # - auction/open 为秒级~分钟级任务：300s/600s 足够，且即使释放失败，
    #   幽灵租约也会在几分钟内自动过期，不会挡住后续开盘/盘中任务；
    # - intraday 每 3 分钟一轮（INTRADAY_INTERVAL_MINUTES=3）：正常单轮
    #   155~204s。2026-09-02 巡检发现 4 轮挂起 10~12 分钟才被回收——旧
    #   TTL 600s 把挂死扫描的盲区放大到 ~10 分钟。进程存活期间心跳线程
    #   每 TTL/4 续期（300s→75s），活进程不会被误抢；TTL 只决定挂死/
    #   SIGKILL 僵尸的回收时延。收紧到 300s（仍 >2× 正常耗时）把盘中
    #   风控盲区从 ~10 分钟压缩到 ~5 分钟。fencing token + 完成 CAS 保证
    #   即使慢扫描被回收，原进程后续写也会因代际不匹配而中止而非双写。
    # - close/risk 等长任务允许因子重建与多账户全量扫描，给到 40 分钟，
    #   与下方 job stale_minutes=45 对齐，且 2×TTL 僵尸回收（80 分钟）
    #   仍远小于一天，不会滞留僵尸租约。
    runtime_lock_ttl = {
        "auction": 300, "open": 600, "risk": 2400, "close": 2400,
        "weekly-review": 2400, "intraday": 300,
    }.get(slot, 600)
    owner_seed = f"{slot}:{day.isoformat()}:{intraday_key or 'daily'}:{os.getpid()}:{_now()}"
    runtime_owner = hashlib.sha1(owner_seed.encode("utf-8")).hexdigest()[:20]
    runtime_lock_acquired = False
    with _db(immediate=True) as conn:
        new_run = True
        prior_detail = {}
        if intraday_key:
            exists = conn.execute("SELECT * FROM paper_job_runs WHERE run_key=?", (intraday_key,)).fetchone()
        else:
            exists = conn.execute("SELECT * FROM paper_jobs WHERE slot=? AND market_date=?", (slot, day.isoformat())).fetchone()
        if exists:
            prior_detail = _loads(exists["detail"], {}) or {}
            if exists["status"] == "running":
                # H6: 与租约 TTL 对齐——intraday 租约 300s（TTL 主要决定
                # 挂死扫描的回收时延，活进程由心跳每 TTL/4 续期），job
                # stale 兜底判定用 5 分钟；其它 slot 租约 40 分钟，
                # stale 判定 45 分钟。
                stale_minutes = 5 if intraday_key else 45
                try:
                    expires_raw = str(exists["expires_at"] or "")[:19] if "expires_at" in exists.keys() else ""
                    if expires_raw:
                        # A live heartbeat can legitimately keep a long close
                        # task running beyond its nominal wall-clock budget.
                        # Only an expired lease is stale; never steal a live
                        # worker based on started_at alone.
                        stale_running = dt.datetime.fromisoformat(expires_raw) <= dt.datetime.now()
                    else:
                        started = dt.datetime.fromisoformat(str(exists["started_at"] or "")[:19])
                        stale_running = (dt.datetime.now() - started).total_seconds() > stale_minutes * 60
                except (TypeError, ValueError):
                    stale_running = True
                if stale_running:
                    prior_detail = {
                        **prior_detail,
                        "error": "任务记录过期，按租约超时自动回收",
                        "recovered_from_running": True,
                        "recovered_at": _now(),
                        "retry_count": int(prior_detail.get("retry_count") or 0),
                    }
                    if intraday_key:
                        conn.execute(
                            "UPDATE paper_job_runs SET status='failed',detail=?,finished_at=? WHERE run_key=?",
                            (_json(prior_detail), _now(), intraday_key),
                        )
                    else:
                        conn.execute(
                            "UPDATE paper_jobs SET status='failed',detail=?,finished_at=? WHERE slot=? AND market_date=?",
                            (_json(prior_detail), _now(), slot, day.isoformat()),
                        )
                    exists = conn.execute(
                        "SELECT * FROM paper_job_runs WHERE run_key=?" if intraday_key
                        else "SELECT * FROM paper_jobs WHERE slot=? AND market_date=?",
                        (intraday_key,) if intraday_key else (slot, day.isoformat()),
                    ).fetchone()
            retry_count = int(prior_detail.get("retry_count") or 0)
            retryable = exists["status"] in {"failed", "interrupted"}
            # 失败任务由同一时段的兜底计划自动重试两次；成功任务仍严格幂等。
            # force 保留给人工诊断，但不会无限自动重跑失败任务。
            if not (retryable and (force or retry_count < max_auto_retries)):
                return {"status": "already_done", "slot": slot, "date": day.isoformat(), "detail": _loads(exists["detail"], {})}
            retry_count += 1
            retry_detail = {
                "retry_count": retry_count,
                "previous_error": prior_detail.get("error"),
                "retry_started_at": _now(),
            }
            new_run = False
        acquired, owner, expires_at = _claim_runtime_lease(
            conn, runtime_lock_key, runtime_owner, slot, ttl_seconds=runtime_lock_ttl,
        )
        if not acquired:
            return {
                "status": "in_progress", "slot": slot, "date": day.isoformat(),
                "reason": "上一轮全市场任务仍在执行，本轮不并发重入",
                "lease_owner": owner, "lease_expires_at": expires_at,
            }
        lease_row = conn.execute(
            "SELECT owner_key, fencing_token, expires_at FROM paper_runtime_locks "
            "WHERE lock_key=? AND owner_key=?",
            (runtime_lock_key, owner),
        ).fetchone()
        if not lease_row:
            # The claim was lost between the CAS and the read-back.  Never
            # create a run without a fencing generation.
            return {
                "status": "in_progress", "slot": slot, "date": day.isoformat(),
                "reason": "租约已被其他运行实例接管，本轮不写入任务状态",
                "lease_owner": owner, "lease_expires_at": expires_at,
            }
        fencing_token = int(lease_row["fencing_token"] or 0)
        runtime_lock_acquired = True
        try:
            if intraday_key and new_run:
                conn.execute(
                    """INSERT INTO paper_job_runs(
                           run_key,slot,market_date,status,started_at,owner_key,heartbeat_at,expires_at,fencing_token
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (intraday_key, slot, day.isoformat(), "running", _now(), owner, _now(), expires_at, fencing_token),
                )
            elif intraday_key:
                cursor = conn.execute(
                    """UPDATE paper_job_runs
                       SET status='running',detail=?,started_at=?,finished_at=NULL,
                           owner_key=?,heartbeat_at=?,expires_at=?,fencing_token=?
                       WHERE run_key=? AND status IN ('failed','interrupted')""",
                    (_json(retry_detail), _now(), owner, _now(), expires_at, fencing_token, intraday_key),
                )
                if cursor.rowcount != 1:
                    _release_runtime_lease(conn, runtime_lock_key, owner, fencing_token)
                    runtime_lock_acquired = False
                    return {"status": "already_done", "slot": slot, "date": day.isoformat(),
                            "detail": "重试竞争中任务已被其他实例领取"}
            elif not intraday_key and new_run:
                conn.execute(
                    """INSERT INTO paper_jobs(
                           slot,market_date,status,started_at,owner_key,heartbeat_at,expires_at,fencing_token
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (slot, day.isoformat(), "running", _now(), owner, _now(), expires_at, fencing_token),
                )
            elif not intraday_key:
                cursor = conn.execute(
                    """UPDATE paper_jobs
                       SET status='running',detail=?,started_at=?,finished_at=NULL,
                           owner_key=?,heartbeat_at=?,expires_at=?,fencing_token=?
                       WHERE slot=? AND market_date=? AND status IN ('failed','interrupted')""",
                    (_json(retry_detail), _now(), owner, _now(), expires_at, fencing_token,
                     slot, day.isoformat()),
                )
                if cursor.rowcount != 1:
                    _release_runtime_lease(conn, runtime_lock_key, owner, fencing_token)
                    runtime_lock_acquired = False
                    return {"status": "already_done", "slot": slot, "date": day.isoformat(),
                            "detail": "重试竞争中任务已被其他实例领取"}
        except Exception:
            _release_runtime_lease(conn, runtime_lock_key, owner, fencing_token)
            runtime_lock_acquired = False
            raise

    # 租约心跳：只要本进程还活着就周期性续期，杜绝“close/风险扫描合法地
    # 跑超过 TTL”后被下一轮 cron 抢走租约、与仍在运行的原进程并发执行的
    # 窗口。若进程死亡（SIGKILL/OOM），心跳随之消失，租约按原 TTL 过期，
    # 僵尸回收语义不变。
    lease_renewal_stop = threading.Event()
    lease_lost_event = threading.Event()
    _set_lease_context(runtime_lock_key, owner, fencing_token, lease_lost_event)

    def _renew_runtime_lease():
        renew_interval = max(60.0, runtime_lock_ttl / 4.0)
        while not lease_renewal_stop.wait(renew_interval):
            try:
                with _db(immediate=True) as renew_conn:
                    heartbeat_at = _now()
                    expires = (dt.datetime.now() + dt.timedelta(seconds=runtime_lock_ttl)).strftime("%Y-%m-%d %H:%M:%S")
                    lock_cursor = renew_conn.execute(
                        "UPDATE paper_runtime_locks SET heartbeat_at=?,expires_at=? "
                        "WHERE lock_key=? AND owner_key=? AND fencing_token=?",
                        (heartbeat_at, expires, runtime_lock_key, owner, fencing_token),
                    )
                    if lock_cursor.rowcount != 1:
                        lease_lost_event.set()
                        return
                    if intraday_key:
                        job_cursor = renew_conn.execute(
                            "UPDATE paper_job_runs SET heartbeat_at=?,expires_at=?,fencing_token=? "
                            "WHERE run_key=? AND owner_key=? AND fencing_token=? AND status='running'",
                            (heartbeat_at, expires, fencing_token, intraday_key, owner, fencing_token),
                        )
                    else:
                        job_cursor = renew_conn.execute(
                            "UPDATE paper_jobs SET heartbeat_at=?,expires_at=?,fencing_token=? "
                            "WHERE slot=? AND market_date=? AND owner_key=? AND fencing_token=? AND status='running'",
                            (heartbeat_at, expires, slot, day.isoformat(), owner, fencing_token),
                        )
                    if job_cursor.rowcount != 1:
                        lease_lost_event.set()
                        return
            except Exception:
                # 续期失败不致命：TTL 过期兜底仍然成立，且失败通常意味着
                # 进程本身已经处于异常状态。
                continue

    lease_renewal_thread = None
    if runtime_lock_acquired:
        lease_renewal_thread = threading.Thread(
            target=_renew_runtime_lease, name=f"lease-renew-{slot}", daemon=True
        )
        lease_renewal_thread.start()
    try:
        if slot == "open":
            detail = execute_open(day)
        elif slot == "risk":
            detail = monitor_risk(day)
        elif slot == "close":
            detail = generate_signals(day)
            # Fetch the closing mark before opening the NAV write transaction;
            # _record_nav is intentionally a pure ledger projection primitive.
            with _db() as snapshot_conn:
                nav_positions = _position_rows(snapshot_conn, asof_day=day, readonly=True)
            nav_quotes = _quotes(
                sorted({str(row["code"]) for row in nav_positions}),
                asof_date=day,
            ) if nav_positions else {}
            # P2-1 审计修复（2026-09-02）：收盘 NAV 同样用本地快照补齐
            # _quotes 间歇性漏 price 的持仓，避免收盘点按成本估值失真。
            nav_quotes = _nav_quotes_with_snapshot_fallback(nav_quotes)
            with _db(immediate=True) as conn:
                _record_nav(conn, day, quotes=nav_quotes)
        elif slot == "auction":
            detail = run_auction_preselection(day, force=force)
        elif slot == "intraday":
            detail = monitor_intraday(asof_datetime=run_asof, force=force)
        else:
            # The weekly-review anchor logic at the top of run_slot already
            # restricted `day` to the last trading day of the ISO week; a
            # weekday()==4 gate here would re-break the holiday-Friday case.
            with _db() as conn:
                detail = {"slot": slot, "date": day.isoformat(), "reviews": _weekly_review(conn, day)}
        if slot in {"intraday", "risk"}:
            # 风控快照由调度任务维护，页面只读取。后台异步刷新避免在持有
            # 租约时同步等待多轮重试；但 cron runner 是一次性进程，退出前
            # 必须有界等待刷新落地（daemon 线程会随进程死亡）。
            try:
                kick = request_risk_snapshot_refresh(trigger=f"slot:{slot}")
                detail["risk_snapshot_refresh"] = kick.get("status")
                if kick.get("status") == "scheduled":
                    detail["risk_snapshot_refresh"] = wait_risk_snapshot_refresh(
                        timeout=20.0
                    ).get("status")
            except Exception as exc:
                detail["risk_snapshot_refresh"] = f"error:{type(exc).__name__}"
            try:
                prior = RC.load_snapshot()
                detail["risk_snapshot_at"] = (prior or {}).get("asof")
            except Exception:
                pass
        with _db(immediate=True) as conn:
            _assert_active_lease(conn, "job completion")
            if intraday_key:
                cursor = conn.execute(
                    "UPDATE paper_job_runs SET status='completed',detail=?,finished_at=? "
                    "WHERE run_key=? AND owner_key=? AND fencing_token=? AND status='running'",
                    (_json(detail), _now(), intraday_key, owner, fencing_token),
                )
            else:
                cursor = conn.execute(
                    "UPDATE paper_jobs SET status='completed',detail=?,finished_at=? "
                    "WHERE slot=? AND market_date=? AND owner_key=? AND fencing_token=? AND status='running'",
                    (_json(detail), _now(), slot, day.isoformat(), owner, fencing_token),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("paper lease lost: completion CAS")
        return {"status": "completed", **detail}
    except Exception as exc:
        with _db(immediate=True) as conn:
            if _lease_lost(exc) or lease_lost_event.is_set():
                raise
            if intraday_key:
                conn.execute(
                    "UPDATE paper_job_runs SET status='failed',detail=?,finished_at=? "
                    "WHERE run_key=? AND owner_key=? AND fencing_token=? AND status='running'",
                    (_json({"error": str(exc), "retry_count": retry_count}), _now(), intraday_key, owner, fencing_token),
                )
            else:
                conn.execute(
                    "UPDATE paper_jobs SET status='failed',detail=?,finished_at=? "
                    "WHERE slot=? AND market_date=? AND owner_key=? AND fencing_token=? AND status='running'",
                    (_json({"error": str(exc), "retry_count": retry_count}), _now(), slot, day.isoformat(), owner, fencing_token),
                )
        raise
    finally:
        if runtime_lock_acquired:
            # 先停租约心跳再释放：释放之后心跳线程若还在运行，会把已删除
            # 的租约重新 INSERT 回去（UPDATE 0 行后由 claim 的 INSERT 分支
            # 复活），制造新的幽灵租约。
            lease_renewal_stop.set()
            if lease_renewal_thread is not None:
                lease_renewal_thread.join(timeout=5)
            try:
                with _db(immediate=True) as conn:
                    # 修复：必须用 _claim_runtime_lease 返回的完整 owner_value
                    # （"sha1:时间戳"），裸 runtime_owner（sha1 前缀）与表中
                    # owner_key 永不匹配，DELETE 0 行 → 幽灵租约残留至 TTL 过期，
                    # 导致后续 open/intraday 全部被"不并发重入"拦截。
                    _release_runtime_lease(conn, runtime_lock_key, owner, fencing_token)
            except Exception as exc:
                # 释放失败不再静默：输出 ALARM 供 health-alert/日志排查，
                # 同时由 TTL 过期兜底（auction 300s / open 600s / intraday 300s）。
                print(json.dumps({
                    "alarm": "runtime_lease_release_failed",
                    "slot": slot, "lease_owner": owner, "lease_expires_at": expires_at,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": "幽灵租约将在 TTL 过期后由 _claim_runtime_lease 自动回收",
                }, ensure_ascii=False), flush=True)
        _clear_lease_context()


def _account_metric_inputs(conn, account_ids, today):
    """Batch the small ledger projections shared by dashboard account cards.

    The previous implementation loaded the same sell rows, buy count,
    rejected count, and NAV history once per account.  Keep the raw rows
    narrow (never fetch ``risk_payload``) and group them in memory so the
    account metric formula remains unchanged.
    """
    ids = [str(account_id) for account_id in account_ids if account_id]
    empty = {
        "latest_nav": {}, "navs": {}, "previous_nav": {}, "sells": {},
        "buy_count": {}, "rejected": {},
    }
    if not ids:
        return empty
    placeholders = ",".join("?" for _ in ids)
    order_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()}
    realized_field = "realized_pnl" if "realized_pnl" in order_columns else "NULL AS realized_pnl"
    executed_field = "executed_at" if "executed_at" in order_columns else "NULL AS executed_at"
    sell_fields = (
        f"id,account_id,code,qty,filled_price,amount,fees,status,{realized_field},"
        f"created_at,{executed_field}"
    )
    sell_rows = _rows(
        conn,
        f"SELECT {sell_fields} FROM paper_orders "
        f"WHERE account_id IN ({placeholders}) AND side='sell' AND status='filled'",
        tuple(ids),
    )
    sells = {account_id: [] for account_id in ids}
    for row in sell_rows:
        sells.setdefault(str(row.get("account_id") or ""), []).append(row)
    buy_rows = _rows(
        conn,
        f"SELECT account_id,COUNT(*) AS count FROM paper_fills "
        f"WHERE account_id IN ({placeholders}) AND side='buy' GROUP BY account_id",
        tuple(ids),
    )
    buy_count = {str(row["account_id"]): int(row["count"] or 0) for row in buy_rows}
    rejected_rows = _rows(
        conn,
        f"SELECT account_id,COUNT(*) AS count FROM paper_orders "
        f"WHERE account_id IN ({placeholders}) AND status!='filled' GROUP BY account_id",
        tuple(ids),
    )
    rejected = {str(row["account_id"]): int(row["count"] or 0) for row in rejected_rows}
    nav_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(paper_nav)").fetchall()}
    quote_status_field = ",quote_status" if "quote_status" in nav_columns else ""
    nav_rows = _rows(
        conn,
        f"SELECT account_id,nav_date,nav,benchmark{quote_status_field},created_at "
        f"FROM paper_nav WHERE account_id IN ({placeholders}) ORDER BY account_id,nav_date",
        tuple(ids),
    )
    navs = {account_id: [] for account_id in ids}
    latest_nav = {}
    previous_nav = {}
    today_key = _date(today).isoformat()
    for row in nav_rows:
        account_id = str(row.get("account_id") or "")
        navs.setdefault(account_id, []).append(row.get("nav"))
        latest_nav[account_id] = row
        if str(row.get("nav_date") or "") < today_key:
            previous_nav[account_id] = row
    return {
        "latest_nav": latest_nav, "navs": navs, "previous_nav": previous_nav,
        "sells": sells, "buy_count": buy_count, "rejected": rejected,
    }


def _account_metrics(conn, account, quotes=None, positions=None, metric_cache=None, allow_network=True):
    # Orphan accounts (removed from ACCOUNT_SPECS but still present in the
    # ledger) must not crash the whole dashboard.
    spec = ACCOUNT_SPECS.get(account["id"]) or {"entry_model_name": str(account.get("id") or "unknown")}
    profile = _risk_profile(account)
    if metric_cache is None:
        nav_row = conn.execute(
            "SELECT * FROM paper_nav WHERE account_id=? ORDER BY nav_date DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
    else:
        nav_row = (metric_cache.get("latest_nav") or {}).get(account["id"])
    positions = positions if positions is not None else _position_rows(conn, account["id"])
    if quotes is None:
        quotes = _quotes([p["code"] for p in positions]) if positions else {}
    missing_valuation_codes = [
        str(p.get("code")) for p in positions
        if not isinstance((quotes.get(p["code"]) or {}).get("price"), (int, float))
    ]
    valuation_status = (
        "cost_fallback" if missing_valuation_codes
        else str((nav_row["quote_status"] if nav_row and "quote_status" in nav_row.keys() else None) or "verified")
    )
    position_value = sum(
        _num((quotes.get(p["code"]) or {}).get("price"), _num(p["cost"])) * _num(p["qty"])
        for p in positions
    )
    today = dt.date.today().isoformat()
    if metric_cache is None:
        sells = _rows(
            conn,
            """SELECT id,account_id,code,qty,filled_price,amount,fees,status,
                      realized_pnl,created_at,executed_at
                 FROM paper_orders
                 WHERE account_id=? AND side='sell' AND status='filled'""",
            (account["id"],),
        )
    else:
        sells = list((metric_cache.get("sells") or {}).get(account["id"], []))
    # A strategy may have sold its last position today.  In that case the
    # current-position quote list is empty, but today's realized attribution
    # still needs the sold symbols' live quotes (for the previous-close
    # baseline).  Fetch them explicitly instead of treating an empty holding
    # list as "no daily P&L".
    sell_codes = {
        str(order.get("code") or "") for order in sells
        if order.get("code") and str(order.get("executed_at") or order.get("created_at") or "")[:10] == today
    }
    missing_sell_codes = sorted(code for code in sell_codes if code not in quotes)
    if missing_sell_codes and allow_network:
        quotes = {**quotes, **_quotes(missing_sell_codes)}
    if metric_cache is None:
        buy_count = int(conn.execute(
            "SELECT COUNT(*) FROM paper_fills WHERE account_id=? AND side='buy'",
            (account["id"],),
        ).fetchone()[0])
        rejected = int(conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status!='filled'",
            (account["id"],),
        ).fetchone()[0])
    else:
        buy_count = int((metric_cache.get("buy_count") or {}).get(account["id"], 0))
        rejected = int((metric_cache.get("rejected") or {}).get(account["id"], 0))
    unrealized = sum((_num((quotes.get(p["code"]) or {}).get("price"), _num(p["cost"])) - _num(p["cost"])) * _num(p["qty"]) for p in positions)
    position_cost_value = sum(_num(p.get("cost")) * _num(p.get("qty")) for p in positions)
    holding_return_pct = (
        unrealized / position_cost_value * 100
        if position_cost_value > 0 else None
    )
    # In shared-pool mode an account's cash balance is merely the ledger
    # bucket used to attribute orders; it is not an independent 100,000 yuan
    # account.  Therefore NAV - initial_cash is not strategy P&L and can show
    # artificial losses of tens of thousands when another strategy consumed
    # the shared cash.  Attribute only actual completed sell P&L plus the
    # current mark-to-market P&L of this strategy's open lots.
    realized = sum(_num(order.get("realized_pnl")) for order in sells)
    strategy_pnl = realized + unrealized
    ledger_initial_cash = max(0.0, _num(account.get("initial_cash"), 0.0))
    # A sleeve introduced after a shared-pool cycle begins can have no private
    # ledger allocation without minting cash into the pool.  Its optional
    # reference capital is presentation/risk context only; orders still use
    # the real pooled cash through _shared_account_exposure.
    initial_cash = _account_reference_capital(account)
    # In a shared pool, ``cash`` is only a settlement allocation bucket.  It
    # must never be used as a strategy NAV: another model spending shared cash
    # would otherwise manufacture a large loss here.  Persisted account NAV,
    # drawdown, excess return and the headline card therefore all share this
    # synthetic but fully attributable definition.
    nav = initial_cash + strategy_pnl
    if metric_cache is None:
        navs = [
            _num(row[0]) for row in conn.execute(
                "SELECT nav FROM paper_nav WHERE account_id=? ORDER BY nav_date",
                (account["id"],),
            ).fetchall()
        ]
    else:
        navs = [_num(value) for value in (metric_cache.get("navs") or {}).get(account["id"], [])]
    if ledger_initial_cash <= 0 < initial_cash:
        # Existing zero NAV snapshots belong to the late-join bookkeeping
        # state, not a real loss.  Rebase only this sleeve's synthetic series;
        # the persisted shared-pool NAV is deliberately untouched.
        navs = [initial_cash if abs(value) < 0.01 else value for value in navs]
    if not navs or abs(navs[-1] - nav) > 0.01:
        navs.append(nav)
    else:
        navs[-1] = nav
    peak = navs[0] if navs else nav
    dd = 0.0
    for value in navs:
        peak = max(peak, value)
        dd = min(dd, value / peak - 1 if peak else 0)
    wins = [order for order in sells if _num(order.get("realized_pnl")) > 0]
    losses = [order for order in sells if _num(order.get("realized_pnl")) < 0]
    win_rate = len(wins) / len(sells) * 100 if sells else None
    gain = sum(_num(order.get("realized_pnl")) for order in wins)
    loss = abs(sum(_num(order.get("realized_pnl")) for order in losses))
    benchmark = None
    if nav_row and nav_row["benchmark"] and account.get("benchmark_start"):
        benchmark = nav_row["benchmark"] / account["benchmark_start"] - 1
    market_session = _market_session()
    if metric_cache is None:
        previous_nav_row = conn.execute(
            "SELECT nav,nav_date FROM paper_nav WHERE account_id=? AND nav_date<? ORDER BY nav_date DESC LIMIT 1",
            (account["id"], today),
        ).fetchone()
    else:
        previous_nav_row = (metric_cache.get("previous_nav") or {}).get(account["id"])
    # Today's strategy attribution is also independent of the shared cash
    # bucket: open-position mark-to-market change plus today's completed sell
    # P&L.  This prevents a cash transfer from appearing as +/- 10,000 yuan.
    today_position_pnl = 0.0
    today_position_base = 0.0
    today_has_quote = False
    for position in positions:
        quote = quotes.get(position["code"]) or {}
        price = _num(quote.get("price"), _num(position.get("cost")))
        pnl, _pct, baseline = _today_position_performance(position, price, quote, today)
        if pnl is not None:
            today_has_quote = True
            today_position_pnl += _num(pnl)
            today_position_base += _num(baseline)
    today_sells = [
        order for order in sells
        if str(order.get("executed_at") or order.get("created_at") or "")[:10] == today
    ]
    today_sell = _today_sell_performance(today_sells, quotes, today)
    today_sell_pnl = today_sell["pnl"]
    today_sell_available = today_sell["covered"] == today_sell["total"]
    # Daily P&L is available when either an open holding was marked or there
    # was a completed sell today.  Previously ``today_has_quote`` was required
    # unconditionally, so a strategy that fully exited showed an em dash even
    # though its realized loss/profit was known.
    today_activity = today_has_quote or today_sell["total"] > 0
    today_pnl = (
        (today_position_pnl if today_has_quote else 0.0) + today_sell_pnl
        if market_session["today_pnl_available"] and today_activity and today_sell_available
        else None
    )
    today_baseline_nav = (
        (today_position_base if today_has_quote else 0.0) + today_sell["baseline"]
        if today_pnl is not None else None
    )
    today_return_pct = today_pnl / today_baseline_nav * 100 if today_pnl is not None and today_baseline_nav > 0 else None
    daily_loss_pct = max(0.0, -(today_return_pct or 0.0))
    return {
        "id": account["id"], "name": account["name"], "status": account["status"], "cycle_days": account["cycle_days"],
        "mode": account.get("mode"), "style": account.get("style"),
        "style_name": STYLE_PROFILES.get(account.get("style"), {}).get("name", account.get("style")),
        "risk_profile": account.get("risk_profile"),
        "risk_profile_name": profile.get("name", account.get("risk_profile")),
        "entry_model_name": spec["entry_model_name"],
        "risk_limits": {
            "max_weight_pct": round(profile["max_weight"] * 100, 1),
            "max_exposure_pct": round(profile["max_exposure"] * 100, 1),
            "max_industry_pct": round(profile["max_industry"] * 100, 1),
            "single_risk_pct": round(profile["single_risk"] * 100, 2),
            "daily_loss_pct": round(profile["daily_loss"] * 100, 1),
            "drawdown_pct": round(profile["drawdown"] * 100, 1),
        },
        "version": account["version"], "params": _loads(account.get("params"), {}),
        "initial_cash": round(initial_cash, 2), "cash": account["cash"],
        "ledger_initial_cash": round(ledger_initial_cash, 2),
        "capital_allocation_mode": "shared_pool_reference" if ledger_initial_cash <= 0 < initial_cash else "shared_pool_ledger",
        "shared_pool_reference_capital": round(initial_cash, 2) if ledger_initial_cash <= 0 < initial_cash else None,
        "cash_allocation_bucket": round(_num(account["cash"]), 2),
        "strategy_synthetic_nav": round(nav, 2),
        "nav": round(nav, 2), "return_pct": round(strategy_pnl / initial_cash * 100, 2) if initial_cash > 0 else None,
        "valuation_status": valuation_status,
        "valuation_missing_codes": missing_valuation_codes,
        "benchmark_return_pct": round(benchmark * 100, 2) if benchmark is not None else None,
        "excess_return_pct": round(((nav / initial_cash - 1) - benchmark) * 100, 2) if benchmark is not None and initial_cash > 0 else None,
        "max_drawdown_pct": round(dd * 100, 2), "trade_count": buy_count + len(sells),
        "risk_blocks": rejected,
        "position_value": round(position_value, 2),
        "position_cost_value": round(position_cost_value, 2),
        "holding_return_pct": round(holding_return_pct, 2) if holding_return_pct is not None else None,
        "fund_utilization_pct": round(position_value / max(nav, 1) * 100, 1),
        "deployment_limit_pct": round(profile["max_exposure"] * 100, 1),
        "deployment_remaining": round(max(nav * profile["max_exposure"] - position_value, 0.0), 2),
        "realized_pnl": round(realized, 2), "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(strategy_pnl, 2), "today_pnl": round(today_pnl, 2) if today_pnl is not None else None,
        "today_return_pct": round(today_return_pct, 2) if today_return_pct is not None else None,
        "today_pnl_available": today_pnl is not None,
        "today_pnl_status": (
            "已清仓，含当日已实现盈亏"
            if today_pnl is not None and not positions and today_sell["total"] > 0
            else market_session["label"] if today_pnl is None else ""
        ),
        "market_session": market_session["code"],
        "today_baseline_nav": round(today_baseline_nav, 2) if today_baseline_nav is not None else None,
        "today_baseline_date": today if today_pnl is not None else (previous_nav_row["nav_date"] if previous_nav_row else None),
        "today_open_position_pnl": round(today_position_pnl, 2) if today_has_quote else None,
        "today_sell_pnl": today_sell_pnl if today_sell["covered"] == today_sell["total"] else None,
        "today_sell_quote_coverage": f"{today_sell['covered']}/{today_sell['total']}",
        "today_sell_missing_codes": today_sell["missing_codes"],
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "profit_loss_ratio": round(gain / loss, 2) if loss else (None if not gain else None),
        "daily_loss_pct": round(daily_loss_pct, 2),
    }


def _shared_metrics(conn, cycle, positions, quotes):
    """Portfolio-level view: strategies remain separate decisions, capital is one pool."""
    initial = _shared_initial_cash(conn, cycle)
    cash = _shared_cash(conn)
    missing_valuation_codes = [
        str(pos.get("code")) for pos in positions
        if not isinstance((quotes.get(pos["code"]) or {}).get("price"), (int, float))
    ]
    market_value = sum(
        _num((quotes.get(pos["code"]) or {}).get("price"), _num(pos["cost"])) * _num(pos["qty"])
        for pos in positions
    )
    nav = cash + market_value
    _, pending_reserve = _pending_buy_reservations(conn, cycle["id"])
    pool_cap = nav * SHARED_POOL_MAX_EXPOSURE
    economic_history = _economic_pool_nav_history(conn, cycle)
    previous = next(
        (row for row in reversed(economic_history) if row["nav_date"] < dt.date.today().isoformat()),
        None,
    )
    today_pnl = nav - _num(previous.get("nav"), None) if previous and _num(previous.get("nav"), None) else None
    today_base = _num(previous.get("nav"), None) if previous else None
    pool_nav_rows = [_num(row.get("nav"), 0.0) for row in economic_history]
    pool_nav_rows.append(nav)
    pool_peak = 0.0
    pool_drawdown = 0.0
    for value in pool_nav_rows:
        pool_peak = max(pool_peak, value)
        if pool_peak > 0:
            pool_drawdown = max(pool_drawdown, 1 - value / pool_peak)
    fills = conn.execute("SELECT COUNT(*) FROM paper_fills WHERE account_id IN (SELECT id FROM paper_accounts WHERE cycle_id=?)", (cycle["id"],)).fetchone()[0]
    return {
        "mode": "shared_pool",
        "label": f"总资金池 · {len(ACCOUNT_SPECS)}套策略独立决策",
        "strategy_count": len(_shared_account_rows(conn, cycle["id"])),
        "initial_cash": round(initial, 2),
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "nav": round(nav, 2),
        "valuation_status": "cost_fallback" if missing_valuation_codes else "verified",
        "valuation_missing_codes": missing_valuation_codes,
        "return_pct": round((nav / initial - 1) * 100, 2) if initial else None,
        "fund_utilization_pct": round(market_value / max(nav, 1) * 100, 1),
        "pool_limit_pct": round(SHARED_POOL_MAX_EXPOSURE * 100, 2),
        "pool_limit_amount": round(pool_cap, 2),
        "pending_buy_reserve": round(pending_reserve, 2),
        "committed_exposure": round(market_value + pending_reserve, 2),
        "committed_exposure_pct": round((market_value + pending_reserve) / max(nav, 1) * 100, 2),
        "available_capacity": round(max(0.0, pool_cap - market_value - pending_reserve), 2),
        "position_count": len(positions),
        "filled_count": int(fills),
        "today_pnl": round(today_pnl, 2) if today_pnl is not None else None,
        "today_return_pct": round(today_pnl / today_base * 100, 2) if today_pnl is not None and today_base else None,
        "daily_loss_pct": round(max(0.0, -(today_pnl / today_base * 100)) if today_pnl is not None and today_base else 0.0, 2),
        "max_drawdown_pct": round(pool_drawdown * 100, 2),
        "today_baseline_nav": round(today_base, 2) if today_base else None,
    }


_schedule_cache = {"data": None, "ts": 0}
# Archived cycles are immutable.  Decoding their JSON snapshots on every
# dashboard refresh was the dominant cost of the read path (and repeatedly
# allocated several MB of short-lived dictionaries).  Keep a process-local
# projection keyed by lightweight archive metadata; a new archive naturally
# invalidates it without putting cache state into the trading ledger.
_archive_orders_cache = {"signature": None, "rows": ()}


def _trim_process_memory():
    """Ask glibc to return temporary archive-decoding pages after a rebuild."""
    try:
        # Linux containers use glibc; keep this optional so local Windows
        # development stays dependency-free.
        import ctypes
        gc.collect()
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (AttributeError, OSError):
        pass


def _load_archive_order_projection(signature):
    """Load the immutable archive projection without decoding source snapshots."""
    try:
        with open(ARCHIVE_ORDERS_CACHE_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("signature") != signature:
            return None
        rows = payload.get("rows")
        return tuple(dict(row) for row in rows) if isinstance(rows, list) else None
    except (OSError, TypeError, ValueError):
        return None


def _store_archive_order_projection(signature, rows):
    """Atomically persist a small, UI-only archive projection for restarts."""
    temp_path = f"{ARCHIVE_ORDERS_CACHE_PATH}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(ARCHIVE_ORDERS_CACHE_PATH), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump({"signature": signature, "rows": rows}, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, ARCHIVE_ORDERS_CACHE_PATH)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _archived_order_rows(conn):
    """Return a cached, UI-safe projection of immutable archived orders."""
    try:
        archive_meta = _rows(
            conn,
            "SELECT id,cycle_key,created_at,length(snapshot) AS snapshot_bytes "
            "FROM paper_archives ORDER BY id DESC",
        )
    except sqlite3.Error:
        return []
    signature_rows = [
        (row.get("id"), row.get("cycle_key"), row.get("created_at"), row.get("snapshot_bytes"))
        for row in archive_meta
    ]
    # A JSON string gives the in-process and on-disk cache the exact same
    # stable comparison key across Python tuple/list representations.
    signature = json.dumps(signature_rows, ensure_ascii=False, separators=(",", ":"))
    if signature == _archive_orders_cache["signature"]:
        # Callers add presentation-only fields, so never expose the cached
        # objects themselves for mutation.
        return [dict(row) for row in _archive_orders_cache["rows"]]

    projected_rows = _load_archive_order_projection(signature)
    if projected_rows is not None:
        _archive_orders_cache["signature"] = signature
        _archive_orders_cache["rows"] = projected_rows
        return [dict(row) for row in projected_rows]

    archived_rows = []
    archived_seen = set()
    try:
        archives = _rows(conn, "SELECT id,cycle_key,snapshot,created_at FROM paper_archives ORDER BY id DESC")
    except sqlite3.Error:
        archives = []
    for archive in archives:
        try:
            snapshot = _loads(archive.get("snapshot"), {}) or {}
            archived_names = {
                row.get("id"): row.get("name")
                for row in (snapshot.get("paper_accounts") or [])
            }
            for archived in snapshot.get("paper_orders") or []:
                item = dict(archived)
                key = (
                    str(archive.get("cycle_key") or ""),
                    str(item.get("id") or ""),
                    str(item.get("created_at") or ""),
                )
                if key in archived_seen:
                    continue
                archived_seen.add(key)
                item.pop("risk_payload", None)
                item["account_name"] = archived_names.get(item.get("account_id"), item.get("account_id"))
                item["archived_cycle"] = archive.get("cycle_key")
                item["read_only"] = True
                archived_rows.append(item)
        except (TypeError, ValueError, sqlite3.Error):
            # One damaged legacy archive must not blank the current ledger.
            continue
    _archive_orders_cache["signature"] = signature
    _archive_orders_cache["rows"] = tuple(archived_rows)
    _store_archive_order_projection(signature, archived_rows)
    # ``snapshot`` may be several megabytes of JSON.  The projection above is
    # deliberately small; release the short-lived decode buffers instead of
    # letting the web process retain them until a later allocation spike.
    _trim_process_memory()
    return [dict(row) for row in archived_rows]

def _recent_orders_with_archives(conn, account_names, limit=500):
    """Return the latest ledger rows across the live and archived cycles.

    A cycle reset deliberately clears ``paper_orders`` after putting an
    immutable snapshot into ``paper_archives``.  The activity page is a
    cross-cycle audit view, so showing only the live table made a successful
    reset look like historical orders had disappeared.
    """
    limit = max(20, min(int(limit or 500), 1000))
    # The current trading day is an audit record, not merely a sample.  Pull
    # a larger live window so the later per-date presentation budget cannot
    # silently erase 09:35/10:05 orders after many waitlist retries arrive.
    live_window = max(limit, 2000)
    # ``risk_payload`` contains the complete decision snapshot and can be
    # hundreds of KB per order.  This list view never renders it; selecting
    # then ``pop``-ing it created a 500MB+ transient allocation on a busy
    # ledger and was the direct cause of slow/502 dashboard refreshes.
    order_fields = (
        "id,account_id,signal_id,side,code,name,qty,planned_price,filled_price,"
        "amount,fees,status,reason,realized_pnl,created_at,executed_at,"
        "order_type,origin,expires_at,cancelled_at"
    )
    orders = _rows(
        conn,
        f"SELECT {order_fields} FROM paper_orders ORDER BY id DESC LIMIT ?",
        (live_window,),
    )
    for order in orders:
        order["account_name"] = account_names.get(order["account_id"], order["account_id"])
        order["archived_cycle"] = None

    orders.extend(_archived_order_rows(conn))
    orders.sort(
        key=lambda item: (item.get("executed_at") or item.get("created_at") or "", item.get("id") or 0),
        reverse=True,
    )
    # Keep the *entire current-date slice* before allocating the remaining
    # response budget across older dates.  The former equal per-day cap let a
    # burst of waitlist/superseded rows crowd out real early-session fills;
    # filtering the page to "已成交" then falsely looked like history loss.
    current_day = _date().isoformat()
    # Split in one pass.  ``item not in current_rows`` compares full ledger
    # dictionaries (including nested decision details) and becomes quadratic
    # once archives are present; the overview endpoint then appears to hang.
    current_rows = []
    historical_rows = []
    for item in orders:
        item_day = str(item.get("executed_at") or item.get("created_at") or "")[:10]
        (current_rows if item_day == current_day else historical_rows).append(item)
    selected = list(current_rows[:limit])
    history_budget = max(0, limit - len(selected))
    dates = {
        str(item.get("executed_at") or item.get("created_at") or "")[:10]
        for item in historical_rows
        if str(item.get("executed_at") or item.get("created_at") or "")[:10]
    }
    per_day = max(25, min(100, history_budget // max(1, len(dates)))) if history_budget else 0
    date_counts = {}
    for item in historical_rows:
        day = str(item.get("executed_at") or item.get("created_at") or "")[:10] or "unknown"
        if not history_budget or date_counts.get(day, 0) >= per_day:
            continue
        selected.append(item)
        date_counts[day] = date_counts.get(day, 0) + 1
        if len(selected) >= limit:
            break
    selected.sort(
        key=lambda item: (item.get("executed_at") or item.get("created_at") or "", item.get("id") or 0),
        reverse=True,
    )
    return selected


def dashboard(include_activity=False, include_history_symbols=False):
    """Return the paper workspace read model without hidden-page ledger scans.

    The browser requests cross-cycle orders and the history selector only in
    their own workspaces.  Keeping those lifetime-growing queries out of the
    portfolio view is the difference between a fast refresh and a full ledger
    report on every click.
    """
    # Cache schedule_status to avoid repeated init_db() calls
    now_ts = time.time()
    if _schedule_cache["data"] is not None and now_ts - _schedule_cache["ts"] < 60:
        schedule = _schedule_cache["data"]
    else:
        schedule = schedule_status()
        _schedule_cache["data"] = schedule
        _schedule_cache["ts"] = now_ts
    market_session = _market_session()
    with _db() as conn:
        cycle = _active_cycle(conn)
        account_rows = _rows(
            conn,
            """SELECT * FROM paper_accounts
               ORDER BY CASE id WHEN 'tq_breakout' THEN 1 WHEN 'trend_pullback' THEN 2
                                WHEN 'sector_rotation' THEN 3 WHEN 'reported_profit_breakout' THEN 4
                                WHEN 'main_force_top10' THEN 5 ELSE 9 END""",
        )
        positions = _position_rows(conn, asof_day=dt.date.today())
        today = dt.date.today().isoformat()
        today_sell_codes = {
            str(row["code"]) for row in conn.execute(
                """SELECT DISTINCT code FROM paper_orders
                   WHERE side='sell' AND status='filled'
                     AND substr(COALESCE(executed_at,created_at),1,10)=?""",
                (today,),
            ).fetchall()
        }
        codes = sorted({p["code"] for p in positions} | today_sell_codes)
        # A browser refresh must never perform two network quote requests plus
        # a flow enrichment while the database read transaction is open.  That
        # made the overview and health endpoints block behind a slow provider.
        # Trading/risk paths still call ``_quotes`` and retain their strict
        # dual-source validation; the dashboard is deliberately a fast,
        # cache-only read model.
        # 估值价格源：优先最近一次全市场快照缓存（15:15 收盘快照与风控刷新
        # 维护）。universe.json 是低频重建的股票池清单——其 price 字段曾被
        # 当作实时估值，7/28 版本的陈旧价格把全池 NAV 压低约 1.2 万并伪造
        # 出“今日亏损”，因此只作缺省兜底。
        quotes = {}
        _snapshot_rows = dfc.load_market_snapshot_full_cached()
        _snapshot_by_code = {
            str(row.get("code")): row for row in _snapshot_rows
            if row.get("code") and isinstance(row.get("price"), (int, float))
        } if _snapshot_rows else {}
        for _code in codes or []:
            row = _snapshot_by_code.get(str(_code))
            if row is not None:
                quotes[str(_code)] = dict(row)
        for _code, row in (_latest_price_map(codes) if codes else {}).items():
            existing = quotes.get(_code)
            if not isinstance(existing, dict) or not isinstance(existing.get("price"), (int, float)):
                quotes[_code] = row
        for quote in quotes.values():
            quote.setdefault("quote_source", "dashboard_cache")
            quote.setdefault("quote_validation", "dashboard_cached")
        # Keep the latest concentration review alongside each holding.  The
        # review is written by the risk cycle (not by the browser), so the
        # dashboard remains a read-only view and never creates a sell order.
        review_rows = _rows(
            conn,
            """SELECT * FROM paper_position_reviews
               WHERE cycle_id=? AND id IN (
                   SELECT MAX(id) FROM paper_position_reviews
                   WHERE cycle_id=? GROUP BY account_id,code
               )
               ORDER BY score ASC,id DESC""",
            (cycle["id"], cycle["id"]),
        )
        review_map = {(row["account_id"], row["code"]): row for row in review_rows}
        metric_cache = _account_metric_inputs(
            conn, [row["id"] for row in account_rows], today
        )
        sold_codes = sorted({
            str(order.get("code"))
            for rows in (metric_cache.get("sells") or {}).values()
            for order in rows
            if order.get("code")
        })
        # ``_account_metrics`` normally backfills today's sold symbols with a
        # live quote.  The compact risk read model is intentionally
        # network-free, so provide its cache-only marks up front instead.
        for code, row in (_latest_price_map(sold_codes) if sold_codes else {}).items():
            quotes.setdefault(code, row)
        accounts = [
            _account_metrics(
                conn,
                row,
                quotes=quotes,
                positions=[p for p in positions if p["account_id"] == row["id"]],
                metric_cache=metric_cache,
            )
            for row in account_rows
        ]
        shared = _shared_metrics(conn, cycle, positions, quotes)
        # Publish the same fair-budget calculation used by order sizing so
        # the dashboard, risk center and audit detail never show three copies
        # of the global 82% ceiling.
        account_rows_by_id = {row["id"]: row for row in account_rows}
        count_budget = _dynamic_position_limits(conn)
        pending_slots = _pending_position_slots(conn, positions)
        occupied_pool_slots = len({
            (str(item.get("account_id")), str(item.get("code")))
            for item in positions if int(_num(item.get("qty"))) >= LOT_SIZE
        } | pending_slots)
        free_pool_slots = max(0, int(count_budget["pool_limit"]) - occupied_pool_slots)
        shared["dynamic_position_slots_used"] = occupied_pool_slots
        shared["dynamic_position_slots_available"] = free_pool_slots
        # Make the auto entry circuit-breaker explain itself in the same
        # read-only dashboard response.  A frozen waitlist without these
        # checks looks identical to a strategy producing no candidates.
        shared["entry_freeze"] = _entry_freeze_status()
        shared["slot_borrow_policy"] = (
            "高分候选可从共享池未使用席位或其他策略空闲席位借用1席；"
            f"不突破硬上限{SHARED_POOL_MAX_POSITIONS}，仍须通过行情、资金、T+1与风控门禁"
        )
        # Allocation and borrowing are execution controls, so expose the same
        # version/history that _buy_order used.  The UI can now distinguish
        # "策略本身有6席" from "本轮从空闲席位借来1席", rather than making a
        # changed upper limit look like an unexplained manual override.
        active_version_id = int(str(count_budget.get("allocation_version") or "slots-v0").rsplit("v", 1)[-1] or 0)
        version_row = conn.execute(
            "SELECT id,pool_limit,limits,weights,inputs,source,effective_at FROM paper_position_limit_versions WHERE id=?",
            (active_version_id,),
        ).fetchone()
        version_inputs = _loads(version_row["inputs"], {}) if version_row else {}
        borrow_events = list(version_inputs.get("slot_borrow_events") or [])[-9:]
        shared["slot_allocation"] = {
            "hard_cap": SHARED_POOL_MAX_POSITIONS,
            "deployable_cap": int(count_budget.get("pool_limit") or 0),
            "limits": count_budget.get("limits") or {},
            "weights": count_budget.get("weights") or {},
            "version": count_budget.get("allocation_version"),
            "effective_at": count_budget.get("effective_at"),
            "source": count_budget.get("source"),
        }
        shared["slot_borrow_audit"] = borrow_events
        shared["slot_borrow_last"] = borrow_events[-1] if borrow_events else None
        for account in accounts:
            account_source = account_rows_by_id.get(account["id"], account)
            account["max_positions"] = max(
                1,
                int(count_budget["limits"].get(account["id"], (ACCOUNT_SPECS.get(account["id"]) or {}).get("max_positions", 5))),
            )
            account["position_limit_dynamic"] = True
            account["pool_position_limit"] = count_budget["pool_limit"]
            account["position_limit_source"] = count_budget["source"]
            account["position_limit_version"] = count_budget["allocation_version"]
            account["dynamic_position_slots_used"] = occupied_pool_slots
            account["dynamic_position_slots_available"] = free_pool_slots
            account["slot_borrow_available"] = bool(
                free_pool_slots > 0 or int(account.get("max_positions", 0)) < STRATEGY_MAX_POSITIONS
            )
            account["slot_borrow_policy"] = "高分候选自动借用1个空闲席位；借位不放宽资金/行情/风控"
            account["position_limit_excess"] = max(
                0, int(_num(account.get("position_count"))) - account["max_positions"],
            )
            # P3 审计修复（R6）：传入 market 使黄灯系数进入展示预算——
            # 旧口径黄/红灯下 deployment_remaining 系统性虚高。市场灯用
            # 已加载的缓存快照计算（allow_network=False，无新增网络开销）。
            try:
                _dash_market = _market_state(
                    _date(None), live_universe=_snapshot_rows,
                    allow_network=False,
                )
            except Exception:
                _dash_market = None
            budget = _strategy_pool_budget(
                conn, account_source,
                shared.get("nav"), positions, quotes,
                market=_dash_market,
            )
            account["strategy_budget_pct"] = budget["target_pct"]
            account["strategy_floor_pct"] = budget["floor_pct"]
            account["strategy_position_pct_pool"] = budget["current_pct"]
            account["strategy_position_value"] = budget["current_amount"]
            account["strategy_pending_reserve_amount"] = budget.get("pending_reserve_amount", 0.0)
            account["strategy_committed_amount"] = budget.get("current_total_amount", budget["current_amount"])
            account["strategy_budget_amount"] = budget["target_amount"]
            account["strategy_floor_amount"] = budget["floor_amount"]
            account["strategy_allowance_amount"] = budget["allowance_amount"]
            account["strategy_redistribution_amount"] = budget["redistribution_amount"]
            account["pool_exposure_pct"] = budget["pool_exposure_pct"]
            account["pool_limit_pct"] = budget["pool_limit_pct"]
            account["strategy_budget"] = budget
            # Existing consumers use these names for the visible budget.
            account["fund_utilization_pct"] = budget["current_pct"]
            account["deployment_limit_pct"] = budget["target_pct"]
            account["deployment_remaining"] = budget["allowance_amount"]
        # Never scale strategy P&L to hide a ledger discrepancy.  Publish the
        # independently calculated totals and an explicit reconciliation gap.
        raw_total = sum(_num(account.get("total_pnl")) for account in accounts)
        pool_total = _num(shared.get("nav")) - _num(shared.get("initial_cash"))
        raw_today = sum(_num(account.get("today_pnl")) for account in accounts)
        pool_today = shared.get("today_pnl")
        pool_today_base = shared.get("today_baseline_nav")
        today_complete = all(account.get("today_pnl") is not None for account in accounts)
        # ``paper_nav`` stores strategy-synthetic NAV: it intentionally
        # excludes cash transferred between strategy attribution buckets.
        # Comparing today's real shared cash+market value with yesterday's
        # sum of those synthetic rows turns an internal transfer into a large
        # fake daily profit/loss.  The four independently calculated daily
        # contributions are exhaustive and transfer-neutral, so use their sum
        # for the visible shared-pool daily P&L.  Preserve the legacy ledger
        # comparison explicitly for reconciliation diagnostics.
        shared["ledger_today_pnl"] = pool_today
        shared["ledger_today_baseline_nav"] = pool_today_base
        shared["ledger_today_reconciliation_delta"] = round(_num(pool_today) - raw_today, 2) if (
            pool_today is not None and today_complete
        ) else None
        if today_complete:
            economic_today = round(raw_today, 2)
            economic_base = _num(shared.get("nav")) - economic_today
            shared["today_pnl"] = economic_today
            shared["today_baseline_nav"] = round(economic_base, 2) if economic_base > 0 else None
            shared["today_return_pct"] = round(economic_today / economic_base * 100, 2) if economic_base > 0 else None
            shared["daily_loss_pct"] = round(max(0.0, -economic_today / economic_base * 100), 2) if economic_base > 0 else 0.0
            shared["today_pnl_source"] = "strategy_contribution_sum"
        for account in accounts:
            account["strategy_attributed_pnl"] = round(_num(account.get("total_pnl")), 2)
            account["strategy_attributed_today_pnl"] = account.get("today_pnl")
            account["strategy_pnl_pct_pool"] = round(
                account["strategy_attributed_pnl"] / max(_num(shared.get("initial_cash")), 1) * 100, 2
            )
            account["strategy_today_pct_pool"] = round(
                account["strategy_attributed_today_pnl"] / max(_num(shared.get("today_baseline_nav")), 1) * 100, 2
            ) if account.get("strategy_attributed_today_pnl") is not None and shared.get("today_baseline_nav") else None
        shared["strategy_pnl_sum"] = round(raw_total, 2)
        shared["pnl_reconciliation_delta"] = round(pool_total - raw_total, 2)
        shared["strategy_today_pnl_sum"] = round(raw_today, 2) if today_complete else None
        # The visible pool number and the four strategy cards now use the same
        # transfer-neutral definition and must reconcile exactly.  The former
        # synthetic-ledger discrepancy remains in ledger_* fields above.
        shared["today_pnl_reconciliation_delta"] = round(
            _num(shared.get("today_pnl")) - raw_today, 2
        ) if shared.get("today_pnl") is not None and today_complete else None
        for pos in positions:
            quality = review_map.get((pos["account_id"], pos["code"])) or {}
            pos["quality_score"] = _num(quality.get("score"), None)
            pos["quality_grade"] = quality.get("grade")
            pos["quality_action"] = quality.get("action")
            pos["quality_replacement_code"] = quality.get("replacement_code")
            pos["quality_replacement_score"] = _num(quality.get("replacement_score"), None)
            pos["quality_reasons"] = quality.get("reasons")
            pos["quality_review_date"] = quality.get("review_date")
            quality_detail = _loads(quality.get("detail"), {}) if quality else {}
            pos["quality_review_phase"] = quality_detail.get("review_phase")
            pos["quality_weights"] = quality_detail.get("weights")
            pos["quality_model_score"] = _num(quality_detail.get("model_score"), None)
            pos["quality_trend_score"] = _num(quality_detail.get("trend_score"), None)
            pos["quality_flow_score"] = _num(quality_detail.get("flow_score"), None)
            pos["quality_momentum_score"] = _num(quality_detail.get("momentum_score"), None)
            pos["quality_return_score"] = _num(quality_detail.get("return_score"), None)
            pos["quality_news_penalty"] = _num(quality_detail.get("news_penalty"), 0.0)
            pos["main_force_intent"] = quality_detail.get("main_force_intent")
            price = _num((quotes.get(pos["code"]) or {}).get("price"), _num(pos["cost"]))
            spec = ACCOUNT_SPECS.get(pos["account_id"]) or {"hard_stop": -0.05}
            pos["price"] = round(price, 2)
            pos["market_value"] = round(price * _num(pos["qty"]), 2)
            pos["cost_value"] = round(_num(pos["cost"]) * _num(pos["qty"]), 2)
            pos["account_weight_pct"] = round(pos["market_value"] / max(shared["nav"], 1) * 100, 2)
            pos["pool_weight_pct"] = pos["account_weight_pct"]
            pos["unrealized_pnl"] = round(pos["market_value"] - pos["cost_value"], 2)
            pos["ret_pct"] = round((price / _num(pos["cost"]) - 1) * 100, 2) if pos.get("cost") else None
            quote = quotes.get(pos["code"]) or {}
            pos["today_pnl"], pos["today_return_pct"], pos["today_baseline"] = _today_position_performance(
                pos, price, quote, dt.date.today()
            )
            pos["today_pnl_status"] = market_session["label"] if pos["today_pnl"] is None else ""
            pos["hold_days"] = _hold_days(pos, dt.date.today())
            pos["quote_at"] = (quotes.get(pos["code"]) or {}).get("quote_at")
            pos["quote_source"] = (quotes.get(pos["code"]) or {}).get("quote_source")
            pos["quote_validation"] = (quotes.get(pos["code"]) or {}).get("quote_validation")
            pos["quote_cross_check"] = (quotes.get(pos["code"]) or {}).get("quote_cross_check")
            pos["main_net"] = (quotes.get(pos["code"]) or {}).get("main_net")
            pos["super_net"] = (quotes.get(pos["code"]) or {}).get("super_net")
            pos["big_net"] = (quotes.get(pos["code"]) or {}).get("big_net")
            pos["mid_net"] = (quotes.get(pos["code"]) or {}).get("mid_net")
            pos["small_net"] = (quotes.get(pos["code"]) or {}).get("small_net")
            pos["main_pct"] = (quotes.get(pos["code"]) or {}).get("main_pct")
            pos["turnover"] = (quotes.get(pos["code"]) or {}).get("turnover")
            pos["vol_ratio"] = (quotes.get(pos["code"]) or {}).get("vol_ratio")
            pos["risk_price"] = round(_num(pos["cost"]) * (1 + spec["hard_stop"]), 3)
            pos["t1_status"] = "可卖" if int(pos.get("available_qty") or 0) > 0 else "今日不可卖"
            pos["t1_reason"] = (
                "已达到最早可卖日期"
                if int(pos.get("available_qty") or 0) > 0
                else f"买入份额锁定至 {pos.get('available_date')}"
            )
            ret = _num(pos.get("ret_pct"))
            pos["price_state"] = (
                "触及风控线" if price <= pos["risk_price"]
                else "盈利运行" if ret > 1
                else "弱势观察" if ret < -1
                else "成本附近"
            )
        account_names = {item["id"]: item["name"] for item in accounts}
        for account in accounts:
            account["position_count"] = sum(1 for p in positions if p["account_id"] == account["id"])
            account["pending_position_slots"] = sum(
                1 for pending_account, _ in pending_slots if pending_account == account["id"]
            )
            account["committed_position_count"] = (
                account["position_count"] + account["pending_position_slots"]
            )
            account["position_limit_excess"] = max(
                0, account["position_count"] - int(_num(account.get("max_positions"), 999)),
            )
            account["pending_order_count"] = conn.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE account_id=? AND status IN ('pending_limit',?)",
                (account["id"], ENTRY_FROZEN_WAITLIST_STATUS),
            ).fetchone()[0]
            account["cooldown_until"] = conn.execute(
                "SELECT cooldown_until FROM paper_accounts WHERE id=?", (account["id"],)
            ).fetchone()[0]
        shared["position_limit"] = count_budget["pool_limit"]
        shared["pending_position_slots"] = len(pending_slots)
        shared["committed_position_count"] = shared.get("position_count", 0) + len(pending_slots)
        shared["position_limit_excess"] = max(0, shared.get("position_count", 0) - count_budget["pool_limit"])
        shared["position_limits"] = count_budget["limits"]
        shared["position_limit_dynamic"] = True
        shared["position_limit_version"] = count_budget["allocation_version"]
        # Recent activity is a cross-cycle audit view: merge immutable reset
        # snapshots only for the visible activity workspace.  Its archive scan
        # is intentionally never run for a portfolio refresh.
        orders = _recent_orders_with_archives(conn, account_names, limit=500) if include_activity else []
        fills = []
        risk_decisions = []
        # Do not load the full signal payload here.  A signal's immutable
        # decision snapshot can be hundreds of KB, and loading 120 of them
        # just to render a candidate card was the main cold-page memory spike.
        # SQLite extracts the two small presentation fragments in-process;
        # the complete evidence remains in the ledger for the dedicated audit
        # endpoints and never needs to live in the web response cache.
        signal_fields = (
            "id,account_id,signal_date,intended_date,code,name,industry,close_price,"
            "rank_score,t_tier,t_score,status,reason,created_at"
        )
        try:
            signals = _rows(
                conn,
                f"""SELECT {signal_fields},
                           json_extract(payload,'$.pick.sector_heat') AS sector_heat_json,
                           json_extract(payload,'$.decision.entry_model') AS entry_model_json
                    FROM paper_signals WHERE intended_date=?
                    ORDER BY account_id,rank_score DESC,id DESC LIMIT 120""",
                (dt.date.today().isoformat(),),
            )
        except sqlite3.OperationalError:
            # Older SQLite builds may omit JSON1.  Preserve a fast, useful
            # dashboard instead of falling back to fetching the large payload.
            signals = _rows(
                conn,
                f"""SELECT {signal_fields} FROM paper_signals WHERE intended_date=?
                    ORDER BY account_id,rank_score DESC,id DESC LIMIT 120""",
                (dt.date.today().isoformat(),),
            )
        signal_ids = [int(signal["id"]) for signal in signals]
        execution_by_signal = {}
        if signal_ids:
            placeholders = ",".join("?" for _ in signal_ids)
            execution_rows = _rows(
                conn,
                f"""SELECT o.signal_id,o.status AS order_status,o.executed_at,o.filled_price,
                           f.quote_at AS execution_quote_at
                    FROM paper_orders o
                    LEFT JOIN paper_fills f ON f.order_id=o.id
                    WHERE o.signal_id IN ({placeholders})
                    ORDER BY o.id DESC""",
                tuple(signal_ids),
            )
            for row in execution_rows:
                execution_by_signal.setdefault(int(row["signal_id"]), row)
        for signal in signals:
            sector_heat = _loads(signal.pop("sector_heat_json", None), {}) or {}
            entry_model = _loads(signal.pop("entry_model_json", None), {}) or {}
            execution = execution_by_signal.get(int(signal["id"])) or {}
            signal["audit"] = {
                "factor_date": signal.get("signal_date"),
                "signal_quote_at": None,
                "signal_quote_pct": None,
                "signal_quote_source": None,
                "decision_at": signal.get("created_at"),
                "planned_review_date": signal.get("intended_date"),
                "signal_mode": "overview_compact",
                "execution_status": execution.get("order_status") or "not_executed",
                "executed_at": execution.get("executed_at"),
                "execution_quote_at": execution.get("execution_quote_at"),
                "execution_price": execution.get("filled_price"),
            }
            signal["payload"] = {
                "pick": {"sector_heat": sector_heat},
                "decision": {"entry_model": entry_model},
            }
            signal["account_name"] = account_names.get(signal["account_id"], signal["account_id"])
        signal_sets = {}
        for signal in signals:
            signal_sets.setdefault(signal["account_id"], set()).add(signal["code"])
        candidate_overlap = []
        account_ids = [account["id"] for account in accounts]
        for index, left in enumerate(account_ids):
            for right in account_ids[index + 1:]:
                intersection = sorted(signal_sets.get(left, set()) & signal_sets.get(right, set()))
                union = signal_sets.get(left, set()) | signal_sets.get(right, set())
                candidate_overlap.append({
                    "left": left, "left_name": account_names.get(left, left),
                    "right": right, "right_name": account_names.get(right, right),
                    "count": len(intersection), "codes": intersection,
                    "jaccard_pct": round(len(intersection) / len(union) * 100, 1) if union else 0.0,
                })
        history_symbols = _rows(
            conn,
            """SELECT code,MAX(name) AS name,COUNT(*) AS order_count,
                      MAX(COALESCE(executed_at,created_at)) AS last_activity_at
                 FROM paper_orders GROUP BY code
                 ORDER BY last_activity_at DESC,code""",
        ) if include_history_symbols else []
        reviews = _rows(conn, "SELECT * FROM paper_reviews ORDER BY week_key DESC, account_id LIMIT 4")
        last_jobs = _rows(conn, "SELECT * FROM paper_jobs ORDER BY market_date DESC, started_at DESC LIMIT 8")
        monitor_runs = _rows(conn, "SELECT * FROM paper_job_runs ORDER BY started_at DESC LIMIT 24")
        for run in monitor_runs:
            detail = _loads(run.get("detail"), {})
            bootstrap = detail.get("bootstrap") or {}
            run["detail"] = {
                "reason": detail.get("reason"),
                "error": detail.get("error"),
                "observed": detail.get("observed"),
                "bootstrap": {"reason": bootstrap.get("reason")} if bootstrap else None,
            }
        observations = _rows(conn, "SELECT * FROM paper_intraday_observations WHERE cycle_id=? ORDER BY id DESC LIMIT 30", (cycle["id"],))
        for item in observations:
            item.pop("payload", None)
        versions = _rows(conn, "SELECT * FROM paper_parameter_versions WHERE cycle_id=? ORDER BY id DESC LIMIT 12", (cycle["id"],))
        for item in versions:
            item["params"] = _loads(item.get("params"))
        archives = _rows(conn, "SELECT id,cycle_key,reason,created_at FROM paper_archives ORDER BY id DESC LIMIT 12")
        exposure = {}
        for pos in positions:
            value = _num(pos["price"]) * _num(pos["qty"])
            exposure[pos.get("industry") or "未知"] = round(exposure.get(pos.get("industry") or "未知", 0.0) + value, 2)
        # Today's account cards are strategy attribution views.  The only
        # authoritative portfolio-level daily P&L is the shared-pool value;
        # summing strategy attribution can double-count marks around fills.
        today_pnl = shared.get("today_pnl")
        today_baseline = shared.get("today_baseline_nav")
        today_available = today_pnl is not None and today_baseline
        today_summary = {
            "asof_date": dt.date.today().isoformat(),
            "available_accounts": 1 if today_available else 0, "account_count": len(accounts),
            "pnl": round(today_pnl, 2) if today_pnl is not None else None,
            "return_pct": round(today_pnl / today_baseline * 100, 2) if today_pnl is not None and today_baseline else None,
            "baseline_nav": round(today_baseline, 2) if today_available else None,
            "pnl_available": today_pnl is not None,
            "market_session": market_session["code"],
            "market_session_label": market_session["label"],
        }
        nav_rows = _rows(
            conn,
            """SELECT account_id,nav_date,nav,benchmark,created_at
               FROM paper_nav ORDER BY nav_date,account_id""",
        )
        nav_dates = sorted({str(row.get("nav_date")) for row in nav_rows if row.get("nav_date")})
        nav_by_account = {}
        benchmark_by_date = {}
        for row in nav_rows:
            nav_by_account.setdefault(row["account_id"], {})[row["nav_date"]] = _num(row.get("nav"), None)
            if _num(row.get("benchmark"), None) is not None:
                benchmark_by_date[row["nav_date"]] = _num(row.get("benchmark"), None)
        curve_series = []
        shared_initial = _shared_initial_cash(conn, cycle)
        shared_values = [
            {
                "date": row["nav_date"],
                "nav": round(_num(row.get("nav")), 2),
                "return_pct": round((_num(row.get("nav")) / shared_initial - 1) * 100, 3)
                if shared_initial else None,
            }
            for row in _economic_pool_nav_history(conn, cycle)
        ]
        curve_series.append({"id": "shared_pool", "name": "总资金池", "values": shared_values})
        for account in accounts:
            initial = _account_reference_capital(account)
            values = []
            for nav_date in nav_dates:
                nav = nav_by_account.get(account["id"], {}).get(nav_date)
                values.append({
                    "date": nav_date,
                    "nav": round(nav, 2) if nav is not None else None,
                    "return_pct": round((nav / initial - 1) * 100, 3) if nav is not None and initial else None,
                })
            curve_series.append({"id": account["id"], "name": account["name"], "values": values})
        benchmark_base = next((value for value in (benchmark_by_date.get(day) for day in nav_dates) if value), None)
        benchmark_series = [
            {
                "date": nav_date,
                "value": round(benchmark_by_date.get(nav_date), 2) if benchmark_by_date.get(nav_date) is not None else None,
                "return_pct": (
                    round((benchmark_by_date[nav_date] / benchmark_base - 1) * 100, 3)
                    if benchmark_base and benchmark_by_date.get(nav_date) is not None else None
                ),
            }
            for nav_date in nav_dates
        ]
        equity_curve = {
            "dates": nav_dates,
            "series": curve_series,
            "benchmark": {"name": "沪深300（收盘快照）", "values": benchmark_series},
            "asof": max((row.get("created_at") for row in nav_rows if row.get("created_at")), default=None),
        }
        return {
            "accounts": accounts, "shared": shared, "capital_model": "shared_pool",
            "positions": positions, "position_reviews": review_rows,
            "orders": orders, "signals": signals,
            "today_summary": today_summary,
            "history_symbols": history_symbols,
            "candidate_overlap": candidate_overlap,
            "fills": fills, "risk_decisions": risk_decisions,
            "reviews": reviews, "jobs": last_jobs, "monitor_runs": monitor_runs,
            "observations": observations, "parameter_versions": versions,
            "archives": archives, "cycle": cycle, "industry_exposure": exposure, "schedule": schedule,
            "equity_curve": equity_curve,
            "disclaimer": "模拟交易，不连接券商；成交为快照价叠加费用和滑点的规则化假设，不代表真实可成交价格。",
        }


def research_validation_dashboard(limit=40):
    """Return only the shadow-research ledger for the three paper strategies."""
    result = PR.dashboard(limit=limit)
    now = dt.datetime.now()
    today = now.date()
    after_close = bool(_is_trade_weekday(today) and now.time() >= dt.time(15, 5))
    result["backfill_policy"] = {
        "scheduled_at": "每个交易日 15:05 盘后评分任务",
        "manual_allowed": after_close,
        "manual_label": "补录当日收盘快照" if after_close else "收盘后可补录",
        "manual_scope": "仅补录当日完整收盘快照与既有样本当日观察；不补写历史候选，不生成信号、委托或风控决策。",
        "next_observation": "每个后续有效交易日的 15:05 收盘任务会依次补齐 1 / 3 / 5 / 10 / 20 日观察。",
        "current_date": today.isoformat(),
    }
    return result


def stock_trade_history(code, account_id=None):
    """Return an on-demand, per-stock audit trail without bloating dashboard()."""
    code = str(code or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("code must be a six-digit stock code")
    account_id = str(account_id or "").strip()
    # Fetch the optional live mark before opening the read transaction.  The
    # history endpoint must never hold even a SQLite read snapshot while a
    # slow provider request is in flight; this was a major source of delayed
    # stock-file responses during an active scanner.
    try:
        prefetched_quotes = _quotes([code])
    except Exception:
        prefetched_quotes = {}
    # This endpoint is deliberately read-only.  Calling init_db() here used to
    # run reconciliation writes and intermittently returned HTTP 500 whenever
    # the intraday scanner held the SQLite writer.
    with _db_readonly() as conn:
        accounts = _rows(conn, "SELECT id,name FROM paper_accounts")
        account_names = {item["id"]: item["name"] for item in accounts}
        if account_id and account_id not in account_names:
            raise ValueError("unknown paper account")
        where = "WHERE o.code=?" + (" AND o.account_id=?" if account_id else "")
        args = (code, account_id) if account_id else (code,)
        orders = _rows(
            conn,
            f"""SELECT o.*,f.fill_date AS fill_date,f.quote_at AS fill_quote_at,f.assumption AS fill_assumption
                  FROM paper_orders o
                  LEFT JOIN paper_fills f ON f.order_id=o.id
                  {where}
                  ORDER BY COALESCE(o.executed_at,o.created_at) DESC,o.id DESC""",
            args,
        )
        for order in orders:
            order.pop("risk_payload", None)
            order["account_name"] = account_names.get(order["account_id"], order["account_id"])
        fills = _rows(
            conn,
            f"SELECT * FROM paper_fills WHERE code=?" + (" AND account_id=?" if account_id else "") + " ORDER BY fill_date DESC, id DESC",
            args,
        )
        for fill in fills:
            fill["account_name"] = account_names.get(fill["account_id"], fill["account_id"])
        # 重置周期会将账本归档成只读快照；个股档案必须同时查询这些归档，
        # 否则“全部历史”会在重置后错误地只剩当前周期。
        archives = _rows(conn, "SELECT cycle_key,snapshot,created_at FROM paper_archives ORDER BY id DESC")
        for archive in archives:
            snapshot = _loads(archive.get("snapshot"), {}) or {}
            archived_accounts = {
                row.get("id"): row.get("name")
                for row in (snapshot.get("paper_accounts") or [])
            }
            archived_fills = {
                item.get("order_id"): item
                for item in (snapshot.get("paper_fills") or [])
                if str(item.get("code") or "") == code
            }
            for order in snapshot.get("paper_orders") or []:
                if str(order.get("code") or "") != code or (account_id and order.get("account_id") != account_id):
                    continue
                item = dict(order)
                item.pop("risk_payload", None)
                fill = archived_fills.get(item.get("id")) or {}
                item["fill_date"] = fill.get("fill_date")
                item["fill_quote_at"] = fill.get("quote_at")
                item["fill_assumption"] = fill.get("assumption")
                item["account_name"] = archived_accounts.get(item.get("account_id"), item.get("account_id"))
                item["archived_cycle"] = archive.get("cycle_key")
                orders.append(item)
            for fill in archived_fills.values():
                if account_id and fill.get("account_id") != account_id:
                    continue
                item = dict(fill)
                item["account_name"] = archived_accounts.get(item.get("account_id"), item.get("account_id"))
                item["archived_cycle"] = archive.get("cycle_key")
                fills.append(item)
        orders.sort(key=lambda item: (item.get("executed_at") or item.get("created_at") or "", item.get("id") or 0), reverse=True)
        fills.sort(key=lambda item: (item.get("fill_date") or "", item.get("id") or 0), reverse=True)
        positions = [
            item for item in _position_rows(
                conn, account_id=account_id or None, asof_day=dt.date.today(), readonly=True
            )
            if item.get("code") == code
        ]
        quotes = prefetched_quotes if positions else {}
        quote = quotes.get(code) or {}
        latest_price = _num(quote.get("price"))
        for position in positions:
            price = latest_price or _num(position.get("cost"))
            position["price"] = round(price, 2)
            position["market_value"] = round(price * _num(position.get("qty")), 2)
            position["unrealized_pnl"] = round((price - _num(position.get("cost"))) * _num(position.get("qty")), 2)
            position["ret_pct"] = round((price / _num(position.get("cost"), 1) - 1) * 100, 2)
            position["today_pnl"], position["today_return_pct"], position["today_baseline"] = _today_position_performance(
                position, price, quote, dt.date.today()
            )
            position["account_name"] = account_names.get(position["account_id"], position["account_id"])
            position["hold_days"] = _hold_days(position, dt.date.today())
        filled = [item for item in orders if item.get("status") == "filled"]
        sell_orders = [item for item in filled if item.get("side") == "sell"]
        buy_fills = [item for item in fills if item.get("side") == "buy"]
        sell_fills = [item for item in fills if item.get("side") == "sell"]
        realized = sum(_num(item.get("realized_pnl")) for item in sell_orders)
        fees = sum(_num(item.get("fees")) for item in fills)
        names = [item.get("name") for item in orders if item.get("name")]
        if not names:
            names = [item.get("name") for item in positions if item.get("name")]
        return {
            "code": code,
            "name": names[0] if names else code,
            "account_filter": account_id or None,
            "positions": positions,
            "orders": orders,
            "fills": fills,
            "summary": {
                "order_count": len(orders),
                "filled_orders": len(filled),
                "rejected_orders": sum(1 for item in orders if item.get("status") == "risk_rejected"),
                "buy_qty": sum(int(item.get("qty") or 0) for item in buy_fills),
                "sell_qty": sum(int(item.get("qty") or 0) for item in sell_fills),
                "buy_amount": round(sum(_num(item.get("amount")) for item in buy_fills), 2),
                "sell_amount": round(sum(_num(item.get("amount")) for item in sell_fills), 2),
                "realized_pnl": round(realized, 2),
                "fees": round(fees, 2),
                "active_position_count": len(positions),
                "today_pnl": round(sum(_num(item.get("today_pnl")) for item in positions if item.get("today_pnl") is not None), 2) if any(item.get("today_pnl") is not None for item in positions) else None,
                "today_pnl_status": _market_session()["label"] if not any(item.get("today_pnl") is not None for item in positions) else "",
            },
            "quote_at": quote.get("quote_at"),
            "note": "This audit trail is generated from the paper ledger only; no broker order is involved.",
        }


_RISK_SNAPSHOT_REFRESH_LOCK = threading.Lock()
_RISK_SNAPSHOT_REFRESH = {"running": False, "started_at": None, "last_error": None,
                          "trigger": None, "thread": None}


def request_risk_snapshot_refresh(trigger="background", on_complete=None):
    """后台异步刷新风控快照；去重，立即返回，不阻塞调用方。

    全市场抓取 + 逐持仓新闻扫描需要数分钟。此前 intraday/risk slot 在
    持有全局租约时同步等待它（最多3次重试），页面 /risk-refresh 也各自
    维护一套线程状态。统一收敛到这里。
    """
    with _RISK_SNAPSHOT_REFRESH_LOCK:
        if _RISK_SNAPSHOT_REFRESH["running"]:
            return {"status": "already_running",
                    "started_at": _RISK_SNAPSHOT_REFRESH["started_at"],
                    "trigger": _RISK_SNAPSHOT_REFRESH["trigger"]}
        _RISK_SNAPSHOT_REFRESH.update(running=True, started_at=_now(), last_error=None,
                                      trigger=str(trigger)[:60])

    def _worker():
        error = None
        try:
            risk_dashboard(refresh=True)
        except Exception as exc:
            error = exc
            with _RISK_SNAPSHOT_REFRESH_LOCK:
                _RISK_SNAPSHOT_REFRESH.update(running=False,
                                              last_error=f"{type(exc).__name__}: {exc}")
        finally:
            if callable(on_complete):
                try:
                    on_complete()
                except Exception as callback_exc:
                    # Cache invalidation is best effort and must never turn a
                    # completed risk snapshot into a failed refresh state.
                    with _RISK_SNAPSHOT_REFRESH_LOCK:
                        _RISK_SNAPSHOT_REFRESH["last_error"] = (
                            f"cache_invalidation:{type(callback_exc).__name__}: {callback_exc}"
                        )
            with _RISK_SNAPSHOT_REFRESH_LOCK:
                _RISK_SNAPSHOT_REFRESH["running"] = False
                if error is None and str(_RISK_SNAPSHOT_REFRESH.get("last_error") or "").startswith("cache_invalidation:"):
                    # Preserve a cache warning for the status endpoint, but
                    # do not report a successful snapshot as a hard failure.
                    pass

    worker_thread = threading.Thread(target=_worker, name="risk-snapshot-refresh", daemon=True)
    with _RISK_SNAPSHOT_REFRESH_LOCK:
        _RISK_SNAPSHOT_REFRESH["thread"] = worker_thread
    worker_thread.start()
    return {"status": "scheduled"}


def wait_risk_snapshot_refresh(timeout=240.0):
    """供一次性 runner 进程在退出前调用：等待后台快照刷新落地。

    cron 通过 ``docker exec`` 触发的 slot 进程是短生命周期的；daemon 线程
    会随主进程一起被杀死。若主流程不等它，快照将永远无法由 slot 路径
    更新。常驻 API 进程不需要调用本函数。
    """
    with _RISK_SNAPSHOT_REFRESH_LOCK:
        thread = _RISK_SNAPSHOT_REFRESH.get("thread")
        running = _RISK_SNAPSHOT_REFRESH.get("running")
    if not running or thread is None:
        return {"status": "idle"}
    if not thread.is_alive():
        return {"status": "finished"}
    thread.join(timeout=timeout)
    with _RISK_SNAPSHOT_REFRESH_LOCK:
        err = _RISK_SNAPSHOT_REFRESH.get("last_error")
    if thread.is_alive():
        return {"status": f"timeout_after_{int(timeout)}s"}
    return {"status": "failed" if err else "completed", "error": err}


def risk_snapshot_refresh_status():
    with _RISK_SNAPSHOT_REFRESH_LOCK:
        status = dict(_RISK_SNAPSHOT_REFRESH)
    status.pop("thread", None)  # 不可 JSON 序列化，且不应暴露给 API
    return status


def _risk_base_dashboard():
    """Build only the ledger projection consumed by ``risk_center``.

    ``dashboard()`` also loads archives, candidate payloads, equity curves,
    research validation and history symbols for the main overview page.  The
    risk page needs accounts, current lots, latest reviews and recent risk
    decisions only; keeping those read models separate materially lowers
    first-load memory and SQLite work without changing any risk rule.
    """
    today = dt.date.today()
    with _db_readonly() as conn:
        cycle = conn.execute(
            "SELECT * FROM paper_cycles WHERE status IN ('draft','running','paused') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if cycle is None:
            return {
                "accounts": [], "shared": {}, "positions": [],
                "position_reviews": [], "risk_decisions": [],
                "capital_model": "shared_pool",
            }
        cycle = dict(cycle)
        account_rows = _rows(
            conn,
            """SELECT * FROM paper_accounts
               ORDER BY CASE id WHEN 'tq_breakout' THEN 1 WHEN 'trend_pullback' THEN 2
                                WHEN 'sector_rotation' THEN 3 WHEN 'reported_profit_breakout' THEN 4
                                WHEN 'main_force_top10' THEN 5 ELSE 9 END""",
        )
        positions = _position_rows(conn, asof_day=today, readonly=True)
        codes = sorted({str(position.get("code")) for position in positions if position.get("code")})
        quotes = {}
        try:
            snapshot_rows = dfc.load_market_snapshot_full_cached()
        except Exception:
            snapshot_rows = []
        for row in snapshot_rows or []:
            code = str(row.get("code") or "")
            if code in codes and isinstance(row.get("price"), (int, float)):
                quotes[code] = dict(row)
        for code, row in (_latest_price_map(codes) if codes else {}).items():
            if code not in quotes or not isinstance(quotes[code].get("price"), (int, float)):
                quotes[code] = row
        for quote in quotes.values():
            quote.setdefault("quote_source", "dashboard_cache")
            quote.setdefault("quote_validation", "dashboard_cached")
        review_rows = _rows(
            conn,
            """SELECT * FROM paper_position_reviews
               WHERE cycle_id=? AND id IN (
                   SELECT MAX(id) FROM paper_position_reviews
                   WHERE cycle_id=? GROUP BY account_id,code
               ) ORDER BY score ASC,id DESC""",
            (cycle["id"], cycle["id"]),
        )
        metric_cache = _account_metric_inputs(
            conn, [row["id"] for row in account_rows], today
        )
        accounts = [
            _account_metrics(
                conn, row, quotes=quotes,
                positions=[p for p in positions if p["account_id"] == row["id"]],
                metric_cache=metric_cache,
                allow_network=False,
            )
            for row in account_rows
        ]
        shared = _shared_metrics(conn, cycle, positions, quotes)
        account_names = {account["id"]: account["name"] for account in accounts}
        for position in positions:
            quote = quotes.get(position["code"]) or {}
            price = _num(quote.get("price"), _num(position.get("cost")))
            spec = ACCOUNT_SPECS.get(position["account_id"]) or {"hard_stop": -0.05}
            position["price"] = round(price, 2)
            position["market_value"] = round(price * _num(position.get("qty")), 2)
            position["cost_value"] = round(_num(position.get("cost")) * _num(position.get("qty")), 2)
            position["account_weight_pct"] = round(
                position["market_value"] / max(_num(shared.get("nav")), 1) * 100, 2
            )
            position["unrealized_pnl"] = round(position["market_value"] - position["cost_value"], 2)
            position["ret_pct"] = round(
                (price / _num(position.get("cost")) - 1) * 100, 2
            ) if position.get("cost") else None
            position["quote_at"] = quote.get("quote_at")
            position["quote_source"] = quote.get("quote_source")
            position["quote_validation"] = quote.get("quote_validation")
            position["quote_cross_check"] = quote.get("quote_cross_check")
            position["main_pct"] = quote.get("main_pct")
            position["main_net"] = quote.get("main_net")
            position["super_net"] = quote.get("super_net")
            position["risk_price"] = round(_num(position.get("cost")) * (1 + spec["hard_stop"]), 3)
            position["t1_status"] = "可卖" if int(position.get("available_qty") or 0) > 0 else "今日不可卖"
        risk_decisions = _rows(
            conn,
            """SELECT id,account_id,code,side,decision,reason,created_at
               FROM paper_risk_decisions ORDER BY id DESC LIMIT 60""",
        )
        for decision in risk_decisions:
            decision["account_name"] = account_names.get(
                decision.get("account_id"), decision.get("account_id")
            )
        return {
            "accounts": accounts,
            "shared": shared,
            "capital_model": "shared_pool",
            "positions": positions,
            "position_reviews": review_rows,
            "risk_decisions": risk_decisions,
            "cycle": cycle,
        }


def risk_dashboard(refresh=False, allow_stale=False, allow_network=True):
    """返回独立风控中心；新增代理因子只读，不改变账户或订单。"""
    base = _risk_base_dashboard()
    snapshot = None if refresh else RC.load_snapshot()
    now = dt.datetime.now()
    in_market_session = now.weekday() < 5 and dt.time(9, 20) <= now.time() <= dt.time(15, 15)
    # Risk pages read the persisted snapshot immediately.  A five-minute
    # freshness window avoids making every page load wait on public APIs;
    # source age remains visible in data-quality rows and the explicit refresh
    # action can still force a new snapshot.
    snapshot_ttl = 300 if in_market_session else 1800
    snapshot_age = RC.snapshot_age_seconds(snapshot) if snapshot is not None else None
    snapshot_stale = snapshot is not None and (snapshot_age is None or snapshot_age > snapshot_ttl)
    if snapshot_stale and not allow_stale:
        snapshot = None
    if snapshot is None and not allow_network:
        # 请求线程禁止联网（页面读取路径）：返回占位快照保持响应速度，
        # 同时触发后台刷新；下一次读取即可拿到真实数据。
        request_risk_snapshot_refresh(trigger="risk-dashboard-no-network")
        snapshot = {
            "asof": None, "market": {}, "positions": [], "universe": [],
            "news": {"events": [], "error": "snapshot_refreshing"},
            "data_quality": [{"name": "风控快照", "status": "refreshing",
                              "detail": "后台正在刷新全市场快照，稍后自动恢复"}],
            "dynamic_risk": {}, "sector_rows": [],
        }
    if snapshot is None:
        # 复用 5 分钟任务已经建立的全市场缓存。旧实现单独抓取市值前 2,000
        # 只股票，既慢又把 36% 覆盖误标成“全市场”。
        try:
            live_universe = dfc.fetch_market_snapshot_full(max_age=240)
        except Exception:
            live_universe = []
        market = _market_state(dt.date.today(), live_universe=live_universe)
        universe = U.load_universe() or []
        # The persisted universe is a historical fallback and can lag the
        # current session.  Refresh the market-wide snapshot from the live
        # source, then let the risk snapshot persist it for instant reads.
        if live_universe:
            universe = live_universe
        positions = list(base.get("positions") or [])
        names = {
            str(position.get("code")): position.get("name") or str(position.get("code"))
            for position in positions
        }
        news_events = []
        news_error = None
        if names:
            try:
                news_events = F.news_keyword_scan(names, include_announcements=True)
            except Exception as exc:
                news_error = str(exc)
        try:
            sector_rows = dfc.fetch_hot_sector_snapshot()
            if not sector_rows:
                sector_rows = dfc.fetch_sector_flow("industry")
        except Exception:
            sector_rows = []
        snapshot = RC.refresh_snapshot(
            market=market,
            positions=positions,
            universe=universe,
            snapshot_at=max((str(row.get("quote_at")) for row in universe if row.get("quote_at")), default=_universe_snapshot_time()),
            news_events=news_events,
            news_error=news_error,
            sector_rows=sector_rows,
        )
    result = RC.build_dashboard(base, snapshot)
    # 风控页面与交易页使用同一总资金池口径；策略仍保留各自的风险模型，
    # 但不再把三套账户误展示成三个互不相关的资金账户。
    result["capital_model"] = base.get("capital_model", "shared_pool")
    result["shared"] = base.get("shared", {})
    # 页面读取时同时暴露最近一轮 5 分钟探活结果，便于区分“没有行情”与
    # “行情源正在重连/已经降级”。不在页面请求里再次发起重型全市场抓取。
    result["data_source_health"] = dfc.load_source_health()
    result["snapshot_age_seconds"] = round(RC.snapshot_age_seconds(snapshot) or 0, 1) if snapshot else None
    result["snapshot_ttl_seconds"] = snapshot_ttl
    result["snapshot_stale"] = bool(snapshot and (RC.snapshot_age_seconds(snapshot) or 0) > snapshot_ttl)
    return result


def _audit_symbol_name(payload, account_id, code, position_names=None, order_names=None):
    """Resolve a stable, display-only symbol label for a risk decision.

    Newer decisions carry the immutable decision snapshot, while older rows
    may contain only a code.  The latter must not make the frontend appear to
    randomly lose names: use the same account/code's ledger names as bounded
    fallback data.  This never affects an execution decision.
    """
    symbol_name = ""
    if isinstance(payload, dict):
        for key in ("signal", "pick", "quote", "execution_quote"):
            item = payload.get(key)
            if isinstance(item, dict) and item.get("name"):
                symbol_name = str(item["name"]).strip()
                if symbol_name:
                    return symbol_name
        if payload.get("name"):
            symbol_name = str(payload["name"]).strip()
            if symbol_name:
                return symbol_name
    ledger_key = (str(account_id or ""), str(code or ""))
    for names in (position_names or {}, order_names or {}):
        symbol_name = str(names.get(ledger_key) or "").strip()
        if symbol_name:
            return symbol_name
    return ""


def risk_audit(limit=160):
    """轻量风控审计流：只读取账本，不请求行情或重建风控快照。"""
    limit = max(20, min(int(limit), 300))
    # This endpoint is a pure read model.  Calling init_db() here used to run
    # migrations, stale-task recovery, lock deletion and shared-cash repair in
    # the middle of a browser request.  It could therefore mutate the ledger
    # while the risk page was only being viewed.  Use a query-only connection;
    # schema maintenance belongs to the scheduler/API write paths.
    try:
        with _db_readonly() as conn:
            accounts = _rows(conn, "SELECT id,name FROM paper_accounts ORDER BY id")
            names = {row["id"]: row["name"] for row in accounts}
            decisions = _rows(
                conn,
                """SELECT id,account_id,code,side,decision,reason,payload,created_at
                   FROM paper_risk_decisions ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
            decision_keys = {
                (str(row.get("account_id") or ""), str(row.get("code") or ""))
                for row in decisions if row.get("code")
            }
            position_names = {}
            order_names = {}
            signal_rows = []
            if decision_keys:
                key_clause = " OR ".join("(account_id=? AND code=?)" for _ in decision_keys)
                key_params = tuple(value for key in decision_keys for value in key)
                # A risk page only needs the current pending signal linked to
                # one of the visible decisions.  Reading every historical
                # pending/waitlist signal grew without bound and made a page
                # refresh contend with the scanner's SQLite work.
                signal_rows = _rows(
                    conn,
                    f"""SELECT id,account_id,code,status,intended_date,created_at
                        FROM paper_signals
                        WHERE ({key_clause})
                          AND status IN ('pending','entry_frozen_waitlist','deferred_capacity','recheck_capacity')
                        ORDER BY id DESC""",
                    key_params,
                )
                for row in _rows(
                    conn,
                    f"SELECT account_id,code,name FROM paper_positions WHERE {key_clause}",
                    key_params,
                ):
                    name = str(row.get("name") or "").strip()
                    if name:
                        position_names[(str(row["account_id"]), str(row["code"]))] = name
                # The display fallback only needs one immutable order label
                # per account/code.  Loading every matching historical order
                # makes a risk refresh grow with the lifetime of the ledger.
                for row in _rows(
                    conn,
                    f"""SELECT o.account_id,o.code,o.name
                        FROM paper_orders o
                        JOIN (
                          SELECT account_id,code,MAX(id) AS id
                          FROM paper_orders WHERE {key_clause}
                          GROUP BY account_id,code
                        ) latest ON latest.id=o.id""",
                    key_params,
                ):
                    ledger_key = (str(row["account_id"]), str(row["code"]))
                    name = str(row.get("name") or "").strip()
                    if name:
                        order_names[ledger_key] = name
    except sqlite3.OperationalError as exc:
        # A first boot can legitimately happen before the schema writer has
        # created the ledger.  Keep the read endpoint useful and side-effect
        # free instead of initializing it from a GET request.
        if "no such table" in str(exc).lower():
            return {"accounts": [], "alerts": [], "asof": _now(), "lightweight": True}
        raise
    linked_signals = {}
    for signal in signal_rows:
        key = (str(signal.get("account_id") or ""), str(signal.get("code") or ""))
        linked_signals.setdefault(key, signal)
    alerts = []
    for decision in decisions:
        outcome = str(decision.get("decision") or "")
        payload = _loads(decision.get("payload"), {}) or {}
        # Snapshot has first priority; position/order ledger data is only the
        # historical compatibility fallback for rows created before snapshots
        # included a name field.
        symbol_name = _audit_symbol_name(
            payload, decision.get("account_id"), decision.get("code"),
            position_names=position_names, order_names=order_names,
        )
        # 风控载荷的行情字段历史上分别放在 quote / execution_quote；
        # 审计接口必须兼容两种结构，否则页面会把已通过的双源核验显示成“未核验”。
        quote_status = payload.get("quote_status") if isinstance(payload, dict) else None
        if not isinstance(quote_status, dict) and isinstance(payload, dict):
            quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}
            execution_quote = payload.get("execution_quote") if isinstance(payload.get("execution_quote"), dict) else {}
            quote_status = dict(quote)
            quote_status.update(execution_quote)
            if quote.get("quote_cross_check") and not execution_quote.get("quote_cross_check"):
                quote_status["quote_cross_check"] = quote["quote_cross_check"]
        if not isinstance(quote_status, dict) and isinstance(payload, dict) and payload.get("status") in {
            "unverified", "cross_source_failed", "cross_source_unavailable", "invalid", "stale"
        }:
            quote_status = payload
        quote_status = quote_status if isinstance(quote_status, dict) else {}
        cross = quote_status.get("quote_cross_check") if isinstance(quote_status.get("quote_cross_check"), dict) else {}
        validation = quote_status.get("quote_validation") or quote_status.get("status")
        # 兼容早期未保存 quote 载荷的记录：已明确写入“核验未通过”的按失败处理，
        # 其余个股记录按“独立源未返回”展示，避免笼统地落到“未核验”。
        if not validation and decision.get("code"):
            validation = (
                "cross_source_failed"
                if decision.get("reason") == "real-time quote lacks passing independent cross-source check"
                else "cross_source_unavailable"
            )
        if not decision.get("code") and not validation:
            validation = "not_applicable"
            validation_detail = "账户级风险状态，不涉及个股行情"
        if validation == "not_applicable":
            validation_detail = "账户级风险状态，不涉及个股行情"
        elif validation == "cross_source_checked":
            validation_detail = "双源核验通过"
        elif validation == "cross_source_failed":
            validation_detail = cross.get("failure_reason") or quote_status.get("reason") or "双源核验未通过"
        elif validation == "cross_source_unavailable":
            validation_detail = cross.get("failure_reason") or quote_status.get("reason") or "独立行情源未返回"
        elif validation in {"stale", "invalid", "missing"}:
            validation_detail = quote_status.get("reason") or "主行情不可用"
        elif validation == "range_timestamp_checked":
            validation_detail = "主行情时间与数值范围校验通过，未完成独立源核对"
        elif validation == "degraded_cross_source":
            validation_detail = quote_status.get("reason") or "主行情有效，独立源不可用，已降级核验"
        else:
            validation_detail = quote_status.get("reason") or "未提供独立行情源校验结果"
            if decision.get("reason") == "real-time quote lacks passing independent cross-source check":
                validation_detail = "历史记录未保存备用源细节；已进入独立行情校验门但未通过"
        display_reason = decision.get("reason") or outcome
        # 旧记录曾把“无法买一手”无条件拼在所有拒绝原因后面，
        # 这里在展示层去掉重复尾缀，保留真正的首要风控原因。
        generic_capacity = "价格、损失预算或剩余仓位不足以买入一手"
        if generic_capacity in display_reason:
            display_reason = display_reason.replace("；" + generic_capacity, "").replace(generic_capacity, "")
            display_reason = display_reason.strip("； ") or "剩余风险预算不足以买入一手"
        linked = linked_signals.get((str(decision.get("account_id") or ""), str(decision.get("code") or "")))
        alerts.append({
            "time": decision.get("created_at"),
            "account_id": decision.get("account_id"),
            "account_name": names.get(decision.get("account_id"), decision.get("account_id")),
            "code": decision.get("code"),
            "name": symbol_name,
            "level": "tightened" if outcome in {"filled", "exit_pending_data"} else "watch",
            "reason": display_reason,
            "action": outcome,
            "execution_mode": "active",
            "rule_version": RISK_VERSION,
            "quote_validation": validation,
            "quote_validation_detail": validation_detail,
            "quote_at": quote_status.get("quote_at"),
            "cross_quote_at": cross.get("quote_at"),
            "cross_price_gap_pct": cross.get("price_gap_pct"),
            "cross_pct_gap": cross.get("pct_gap"),
            "linked_signal": ({
                "id": linked.get("id"), "status": linked.get("status"),
                "intended_date": linked.get("intended_date"),
                "created_at": linked.get("created_at"),
                "label": "待执行委托（等待开盘审批/执行批次）",
            } if linked else None),
        })
    return {"accounts": accounts, "alerts": alerts, "asof": _now(), "lightweight": True}


def _validate_capital(capital):
    capital = float(capital)
    if capital < 1000 or capital > 10_000_000:
        raise ValueError("总模拟资金池须在 1,000 至 10,000,000 元之间")
    return capital


def _cycle_snapshot(conn):
    cycle = _active_cycle(conn)
    # Archives are consumed by the account/stock history views.  Loading every
    # risk decision and its full decision_snapshot into one Python object made
    # cycle rollover grow without bound and could restart the container before
    # the transaction committed.  Preserve the durable trading ledger and
    # version history, while recording counts for high-volume operational
    # tables instead of duplicating their bulky payloads.
    ledger_tables = [
        "paper_accounts", "paper_orders", "paper_positions",
        "paper_position_lots", "paper_fills", "paper_nav",
        "paper_parameter_versions", "paper_position_limit_versions",
    ]
    counted_tables = [
        "paper_signals", "paper_risk_decisions", "paper_jobs",
        "paper_job_runs", "paper_reviews", "paper_intraday_observations",
        "paper_position_reviews", "paper_capital_reservations",
    ]
    snapshot = {}
    for table in ledger_tables:
        if table == "paper_orders":
            # Exclude the large evidence JSON at SQL level.  Fetching it and
            # deleting it afterwards still causes a large temporary allocation
            # and was enough to make rollover exceed the gateway timeout.
            columns = [
                row["name"] for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
                if row["name"] != "risk_payload"
            ]
            select_list = ",".join(f'"{name}"' for name in columns)
            snapshot[table] = _rows(conn, f"SELECT {select_list} FROM paper_orders")
        else:
            snapshot[table] = _rows(conn, f"SELECT * FROM {table}")
    snapshot["_archive_format"] = "compact-ledger-v2"
    snapshot["_table_counts"] = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ledger_tables + counted_tables
    }
    return cycle, snapshot


def _archive_current_cycle(conn, reason):
    cycle, snapshot = _cycle_snapshot(conn)
    now = _now()
    conn.execute("INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at) VALUES(?,?,?,?,?)",
                 (cycle["id"], cycle["cycle_key"], reason, _json(snapshot), now))
    conn.execute("UPDATE paper_cycles SET status='archived',ended_at=?,updated_at=? WHERE id=?", (now, now, cycle["id"]))
    for table in ["paper_signals", "paper_orders", "paper_positions", "paper_position_lots", "paper_fills",
                  "paper_risk_decisions", "paper_nav", "paper_jobs", "paper_job_runs", "paper_reviews",
                  "paper_intraday_observations", "paper_parameter_versions", "paper_position_reviews",
                  "paper_capital_reservations", "paper_position_limit_versions"]:
        conn.execute(f"DELETE FROM {table}")
    _audit(conn, None, "cycle_archived", f"周期 {cycle['cycle_key']} 已归档：{reason}")
    return cycle


def _create_cycle(conn, capital, status="paused", reason="新建模拟周期"):
    capital = _validate_capital(capital)
    now = _now()
    key = "cycle-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    cursor = conn.execute(
        "INSERT INTO paper_cycles(cycle_key,status,capital,risk_profile,started_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (key, status, capital, "shared_pool", now if status == "running" else None, now, now),
    )
    cycle_id = cursor.lastrowid
    benchmark = _benchmark_close()
    account_share = capital / max(len(ACCOUNT_SPECS), 1)
    for account_id, spec in ACCOUNT_SPECS.items():
        conn.execute(
            """UPDATE paper_accounts SET name=?,source_strategy=?,status=?,initial_cash=?,cash=?,cycle_days=?,
               max_positions=?,max_weight=?,max_exposure=?,version=?,benchmark_start=?,cycle_id=?,mode=?,style=?,
               risk_profile=?,params='{}',daily_start_nav=?,daily_nav_date=?,cooldown_until=NULL,updated_at=? WHERE id=?""",
            (spec["name"], spec["source_strategy"], status, account_share, account_share, spec["cycle_days"], spec["max_positions"],
             spec["max_weight"], spec["max_exposure"], spec.get("strategy_version") or "v3.0", benchmark, cycle_id, spec["mode"], spec["default_style"],
             spec["risk_profile"], account_share, _date().isoformat(), now, account_id),
        )
        conn.execute("INSERT INTO paper_nav(account_id,nav_date,cash,market_value,nav,benchmark,created_at) VALUES(?,?,?,?,?,?,?)",
                     (account_id, _date().isoformat(), account_share, 0.0, account_share, benchmark, now))
        conn.execute("INSERT INTO paper_parameter_versions(cycle_id,account_id,version,style,params,reason,effective_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (cycle_id, account_id, spec.get("strategy_version") or "v3.0", spec["default_style"], "{}", reason, _date().isoformat(), now))
    _audit(conn, None, "cycle_created", f"{key}：共享模拟资金池 {capital:.2f} 元，多策略独立决策共用资金，{reason}")
    return {"id": cycle_id, "cycle_key": key, "status": status, "capital": capital}


def configure_capital(capital):
    """保存草稿资金；有成交或持仓的周期不可改写。"""
    capital = _validate_capital(capital)
    init_db()
    with _db() as conn:
        cycle = _active_cycle(conn)
        if cycle["status"] == "running":
            raise ValueError("当前周期运行中，请先暂停后再修改资金")
        activity = conn.execute("SELECT COUNT(*) FROM paper_orders WHERE status='filled'").fetchone()[0]
        positions = conn.execute("SELECT COUNT(*) FROM paper_position_lots WHERE remaining_qty>0").fetchone()[0]
        if activity or positions:
            raise ValueError("本周期已有模拟成交或持仓，资金已锁定；请使用完全重置新建周期")
        conn.execute("UPDATE paper_cycles SET capital=?,updated_at=? WHERE id=?", (capital, _now(), cycle["id"]))
        share = capital / max(len(ACCOUNT_SPECS), 1)
        conn.execute("UPDATE paper_accounts SET initial_cash=?,cash=?,daily_start_nav=?,updated_at=?", (share, share, share, _now()))
        conn.execute("UPDATE paper_nav SET cash=?,market_value=0,nav=?", (share, share))
        _audit(conn, None, "capital_configured", f"共享模拟资金池草稿设为 {capital:.2f} 元；策略初始展示份额 {share:.2f} 元")
    return dashboard()


def start_new_cycle(capital=300000.0, include_dashboard=True):
    """保存资金并启动新周期。旧周期先完整归档，避免覆盖历史模拟结果。

    HTTP 写接口使用 ``include_dashboard=False``，避免在已经提交账本变更后
    同步抓行情并构建超大 overview，导致反向代理超时而让页面误报启动失败。
    保留默认值是为了兼容现有脚本调用方。
    """
    init_db()
    with _db() as conn:
        current = _active_cycle(conn)
        if current["status"] == "running":
            raise ValueError("当前周期仍在运行，请先暂停再启动新周期")
        _archive_current_cycle(conn, "用户保存资金并启动新周期")
        cycle = _create_cycle(conn, capital, status="running", reason="保存资金并启动")
    summary = dashboard() if include_dashboard else {
        "cycle": cycle,
        "strategy_count": len(ACCOUNT_SPECS),
        "refresh_required": True,
    }
    return summary, cycle


def reset_cycle(capital=300000.0, include_dashboard=True):
    """完全重置只归档，不删除历史；新周期保持暂停，等待用户明确启动。

    HTTP 写接口使用 ``include_dashboard=False``（与 start_new_cycle 一致），
    避免重置后在事务已提交时同步抓行情构建超大 overview 导致超时。
    """
    init_db()
    with _db() as conn:
        current = _active_cycle(conn)
        if current["status"] == "running":
            raise ValueError("当前周期仍在运行，请先暂停后再完全重置")
        _archive_current_cycle(conn, "用户完全重置")
        cycle = _create_cycle(conn, capital, status="paused", reason="完全重置后等待启动")
    if include_dashboard:
        return dashboard(), cycle
    return {
        "cycle": cycle,
        "strategy_count": len(ACCOUNT_SPECS),
        "refresh_required": True,
    }, cycle


def set_account_style(account_id, style):
    if account_id not in ACCOUNT_SPECS:
        raise ValueError("未知策略账户")
    if style not in STYLE_PROFILES:
        raise ValueError("风格必须是 strong、pullback、sector 或 quality")
    init_db()
    with _db() as conn:
        cycle = _active_cycle(conn)
        if cycle["status"] == "running":
            raise ValueError("当前周期运行中，暂停后才能调整策略风格")
        conn.execute("UPDATE paper_accounts SET style=?,updated_at=? WHERE id=?", (style, _now(), account_id))
        _audit(conn, account_id, "style_changed", f"后续候选风格切换为 {STYLE_PROFILES[style]['name']}；已有持仓不强平")
    return dashboard()


def set_accounts_status(status, capital=None):
    if status not in {"running", "paused"}:
        raise ValueError("状态必须为 running 或 paused")
    init_db()
    if capital is not None:
        configure_capital(capital)
    with _db() as conn:
        cycle = _active_cycle(conn)
        # 幂等：已是目标状态时直接返回当前状态，避免前端重复点击报 500
        # （此前 paused 再 pause / running 再 resume 都抛 ValueError）。
        if cycle["status"] == status:
            return dashboard()
        if status == "running" and cycle["status"] not in {"paused", "draft"}:
            raise ValueError("当前周期不是暂停状态，不能恢复运行")
        if status == "paused" and cycle["status"] != "running":
            raise ValueError("当前周期并未运行，无需再次暂停")
        conn.execute("UPDATE paper_accounts SET status=?, updated_at=?", (status, _now()))
        conn.execute("UPDATE paper_cycles SET status=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                     (status, _now() if status == "running" else None, _now(), cycle["id"]))
        _audit(conn, None, "accounts_" + status, "三策略模拟盘状态变更")
    return dashboard()


def latest_reviews():
    init_db()
    with _db() as conn:
        return _rows(conn, "SELECT * FROM paper_reviews ORDER BY week_key DESC, account_id")


def schedule_status():
    names = [
            "A股模拟盘-竞价预选", "A股模拟盘-日内监控-上午", "A股模拟盘-日内监控-下午", "A股模拟盘-开盘审批",
        "A股模拟盘-盘后评分", "A股模拟盘-周度复盘",
    ]
    found = []
    if os.name == "nt":
        for name in names:
            try:
                result = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, text=True, errors="replace", timeout=4)
                if result.returncode == 0:
                    found.append(name)
            except Exception:
                break
    else:
        schedule_file = os.environ.get(
            "ASTOCK_SCHEDULE_FILE", "/etc/cron.d/astock-quant"
        )
        if os.path.isfile(schedule_file):
            found.extend(names)
    today = dt.date.today().isoformat()
    with _db() as conn:
        latest = _rows(conn, """
            SELECT slot,status,started_at,finished_at,detail
            FROM paper_jobs
            WHERE market_date=?
            ORDER BY COALESCE(finished_at,started_at) DESC
            LIMIT 12
        """, (today,))
        intraday = _rows(conn, """
            SELECT run_key,status,started_at,finished_at,detail
            FROM paper_job_runs
            WHERE run_key LIKE ?
            ORDER BY COALESCE(finished_at,started_at) DESC
            LIMIT 12
        """, (f"intraday:{today.replace('-', '')}%",))
    failed = [row for row in latest + intraday if row.get("status") == "failed"]
    return {
        "installed": len(found) == len(names),
        "tasks": found,
        "runtime_date": today,
        "latest_runs": latest,
        "latest_intraday_runs": intraday,
        "runtime_status": "异常" if failed else ("已执行" if latest or intraday else "等待首个任务"),
        "runtime_failed_count": len(failed),
        "note": "09:25采集集合竞价快照做预选；09:30、09:31、13:00先执行共享开盘事件扫描（冲高回落可减仓，回补需后续反弹确认），09:31再用最新双源行情审批新开仓；其后每3分钟执行至11:25和14:55；15:05盘后评分和周五15:20复盘。失败任务会由同一时段的兜底计划自动重试，页面显示数据库中的实际运行记录。"
    }


def install_windows_schedule():
    """由用户在本地页面点击“启用模拟盘”后调用；不在服务启动时注册系统任务。"""
    if os.name != "nt":
        return {"ok": False, "message": "当前系统不是 Windows，未安装计划任务"}
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    # pythonw has no console subsystem, so every three-minute scheduled check
    # remains invisible.  Keep python.exe as a safe fallback for non-standard
    # Python installations.
    python = pythonw if os.path.exists(pythonw) else sys.executable
    runner = os.path.join(os.path.dirname(__file__), "paper_runner.py")
    definitions = [
        ("A股模拟盘-竞价预选", "09:25", "auction", "MON,TUE,WED,THU,FRI", None),
        ("A股模拟盘-日内监控-上午", "09:30", "intraday", "MON,TUE,WED,THU,FRI", "01:55"),
        ("A股模拟盘-日内监控-下午", "13:00", "intraday", "MON,TUE,WED,THU,FRI", "01:55"),
        ("A股模拟盘-开盘审批", "09:31", "open", "MON,TUE,WED,THU,FRI", None),
        ("A股模拟盘-盘后评分", "15:05", "close", "MON,TUE,WED,THU,FRI", None),
        ("A股模拟盘-周度复盘", "15:20", "weekly-review", "FRI", None),
    ]
    errors = []
    for name, clock, slot, days, duration in definitions:
        command = f'"{python}" "{runner}" --slot {slot}'
        try:
            args = ["schtasks", "/Create", "/TN", name, "/SC", "WEEKLY", "/D", days, "/ST", clock, "/TR", command]
            if duration:
                args.extend(["/RI", str(INTRADAY_INTERVAL_MINUTES), "/DU", duration])
            args.append("/F")
            result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=12)
            if result.returncode != 0:
                errors.append((result.stderr or result.stdout or name).strip())
        except Exception as exc:
            errors.append(str(exc))
    return {"ok": not errors, "errors": errors, "schedule": schedule_status()}


def install_platform_schedule():
    """Install on Windows; report the administrator-managed cron on Linux."""
    if os.name == "nt":
        return install_windows_schedule()
    status = schedule_status()
    return {
        "ok": status["installed"],
        "message": (
            "Linux 定时任务由 /etc/cron.d/astock-quant 管理"
            if status["installed"]
            else "尚未检测到 Linux 定时任务，请执行 deploy/install-centos9.sh"
        ),
        "schedule": status,
    }
