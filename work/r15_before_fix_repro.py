# -*- coding: utf-8 -*-
"""R15 before-fix **differential** reproduction：sell 风险决策偷偷依赖机器当前日期。

缺陷链路（base ``06197d76``）::

    _sell_plan(asof_day=D)
            ↓  漏传 asof_day
    _position_peak(position, quote, price)      # 内部 asof_day=None
            ↓
    _bought_today(position, asof_day=None)
            ↓
    _date(asof_day or dt.date.today())          # 回退到机器 wall-clock

后果：历史回放（``asof_day == 2026-01-05``）时机器今天是别的日期，同日新仓
**不再**被识别为同日新仓，于是把**买入前**出现的日内 high 吸进 peak，凭空造出
回撤并真实触发 trailing_stop。

修复后该链路被两处切断：``_sell_plan`` 显式把 as-of 传进纯 engine，而
``paper_risk_decision`` 的 ``asof_day`` 是 keyword-only 必填——传 ``None`` 直接
``ValueError``，**没有** wall-clock 回退可用。

本脚本是 **differential reproduction**：

* BASE（未修复）：打印 ``R15-C1 REPRODUCED`` / ``R15-C2 REPRODUCED``；
* FIX 后（``asof_day`` 显式贯穿）：同样的构造会打印
  ``R15-C1 NOT REPRODUCED`` / ``R15-C2 NOT REPRODUCED``。

在已修复的代码上看到 ``NOT REPRODUCED`` **不是脚本失败**，正是修复生效的证据。

全部纯 fixture：

* 不打开数据库（position 是普通 dict，``_spec_for`` 走内置声明分支）；
* 不访问网络 / 生产 cache / K 线（``_completed_kline`` 打桩为 None）；
* 不 sleep；
* 机器“今天”用 ``PT.dt`` 的假模块显式模拟，绝不动真实系统时钟；
* 确定性阈值只走 ``spec_override``，不改任何生产风险参数。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from unittest import mock

BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "backend"))
sys.path.insert(0, BACKEND)

import paper_trading as PT  # noqa: E402
import paper_risk_decision as PRD  # noqa: E402

ACCOUNT = "tq_breakout"
CODE = "600519"
ASOF = dt.date(2026, 1, 5)
#: 与 asof 脱节的“机器今天”。
MACHINE_OTHER = dt.date(2026, 9, 20)
#: 确定性阈值（仅测试用；hard_stop/trail 与账户默认同量级，take_profit 清空、
#: hold_max 抬到不可达，使断言只反映 peak 口径的差异）。
SPEC = {"hard_stop": -0.50, "trail_after": 0.01, "trail_stop": 0.05,
        "take_profit": [], "hold_max": 100000}
COST = 10.00
PRICE = 10.50
PRE_ENTRY_HIGH = 12.00


def _fake_dt(machine_day):
    """一个“机器今天是 machine_day”的 ``datetime`` 替代模块。"""
    class _Date(dt.date):
        @classmethod
        def today(cls):
            return cls(machine_day.year, machine_day.month, machine_day.day)

    class _Datetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            base = dt.datetime(machine_day.year, machine_day.month, machine_day.day, 10, 0, 0)
            return base if tz is None else base.replace(tzinfo=tz)

    class _DT:
        date = _Date
        datetime = _Datetime
        timedelta = dt.timedelta

    return _DT


def _fresh_same_day():
    """asof 当天整仓买入的新仓：100 股全部在 asof_day 取得。"""
    return {"account_id": ACCOUNT, "code": CODE, "qty": 100, "today_acquired_qty": 100,
            "entry_date": ASOF.isoformat(), "cost": COST, "peak_price": COST,
            "take_stage": None}


def _overnight():
    """隔夜老仓：昨天建仓、当日无新增。"""
    return {"account_id": ACCOUNT, "code": CODE, "qty": 100, "today_acquired_qty": 0,
            "entry_date": "2026-01-04", "cost": COST, "peak_price": COST,
            "take_stage": 0}


def _partial_add_on():
    """老仓 + 当日部分加仓：200 股里只有 100 股是当日取得。"""
    return {"account_id": ACCOUNT, "code": CODE, "qty": 200, "today_acquired_qty": 100,
            "entry_date": ASOF.isoformat(), "cost": COST, "peak_price": COST,
            "take_stage": 0}


QUOTE = {"price": PRICE, "high": PRE_ENTRY_HIGH, "pct": 5.0}


def _sell(position, *, asof_day=ASOF, machine_day=MACHINE_OTHER, spec=SPEC):
    """驱动真实生产 ``_sell_plan``；K 线打桩、机器日期显式模拟。"""
    with mock.patch.object(PT, "dt", _fake_dt(machine_day)), \
            mock.patch.object(PT, "_completed_kline", return_value=None):
        return PT._sell_plan(position, QUOTE, asof_day, [], spec_override=spec)


def _peak(position, asof_day):
    """峰值口径只能由显式 as-of 驱动。

    base 上 ``asof_day`` 可省且会回退到机器今天；修复后 ``paper_risk_decision``
    要求 keyword-only 必填，``None`` 直接 ``ValueError``——这里把它收敛成
    ``None`` 返回值，好让 C1 的对照打印在两种代码状态下都能跑完。
    """
    try:
        return PRD.position_peak(position, QUOTE, PRICE, asof_day=asof_day)
    except ValueError:
        return None


def case_c1():
    """R15-C1：历史同日新仓把买入前的日内 high 吸进 peak。"""
    position = _fresh_same_day()
    leaked = _peak(position, None)            # base：漏传 asof → wall-clock 回退
    correct = _peak(position, ASOF)           # 本应使用的口径（显式 asof）
    _ratio, _reason, _stage, detail = _sell(position)
    print("R15-C1  asof_day=2026-01-05  machine_today=%s" % MACHINE_OTHER.isoformat())
    print("R15-C1  position: qty=100 today_acquired_qty=100 entry_date=2026-01-05 "
          "cost=10.00 peak_price=10.00 take_stage=None")
    print("R15-C1  quote: price=%.2f high=%.2f (high 出现在买入之前)" % (PRICE, PRE_ENTRY_HIGH))
    print("R15-C1  peak(asof_day=None) = %s   <- base 的 _sell_plan 实际使用" % leaked)
    print("R15-C1  peak(asof_day=asof) = %.2f   <- 正确口径" % correct)
    print("R15-C1  _sell_plan(... asof_day=2026-01-05) drawdown=%.2f%% exit=%s"
          % (detail.get("drawdown_pct"), detail.get("exit_class")))
    reproduced = (
        leaked == PRE_ENTRY_HIGH
        and abs(correct - PRICE) < 1e-9
        and abs(float(detail.get("drawdown_pct") or 0.0) - 12.5) < 1e-9
    )
    if reproduced:
        print("R15-C1 REPRODUCED: _sell_plan(asof_day=2026-01-05) absorbed the "
              "pre-entry high 12.00 into the risk calculation")
    else:
        print("R15-C1 NOT REPRODUCED: the explicit asof_day now reaches the peak "
              "calculation (pre-entry high is ignored, no wall-clock fallback exists)")
    return reproduced


def case_c2():
    """R15-C2：仅因机器日期不同，同一输入由 none 变成 trailing_stop。"""
    position = _fresh_same_day()
    # 世界 A：机器今天 == asof（正确的“同日新仓”识别）。
    a_ratio, _a_reason, _a_stage, a_detail = _sell(position, machine_day=ASOF)
    # 世界 B：机器今天 != asof（wall-clock 泄漏）。
    b_ratio, _b_reason, _b_stage, b_detail = _sell(position, machine_day=MACHINE_OTHER)
    print("R15-C2  machine_today=2026-01-05 (= asof): ratio=%s exit=%s drawdown=%.2f%%"
          % (a_ratio, a_detail.get("exit_class"), a_detail.get("drawdown_pct")))
    print("R15-C2  machine_today=2026-09-20 (!= asof): ratio=%s exit=%s drawdown=%.2f%%"
          % (b_ratio, b_detail.get("exit_class"), b_detail.get("drawdown_pct")))
    print("R15-C2  identical ledger / identical asof_day / identical quote；"
          "只有机器当前日期不同")
    reproduced = (
        a_ratio == 0.0 and a_detail.get("exit_class") == "none"
        and b_ratio == 1.0 and b_detail.get("exit_class") == "trailing_stop"
    )
    if reproduced:
        print("R15-C2 REPRODUCED: the wall-clock leak alone flips exit none -> "
              "trailing_stop (sell_ratio 0.0 -> 1.0)")
    else:
        print("R15-C2 NOT REPRODUCED: both machine-today worlds agree "
          "(exit=%s / %s)" % (a_detail.get("exit_class"), b_detail.get("exit_class")))
    return reproduced


def case_c3():
    """反向敏感性：隔夜老仓**应当**吸收当日 high（修复后也不得改变）。"""
    position = _overnight()
    a_ratio, _r, _s, a_detail = _sell(position, machine_day=ASOF)
    b_ratio, _r, _s, b_detail = _sell(position, machine_day=MACHINE_OTHER)
    print("R15-C3  overnight: ratio=%s/%s exit=%s/%s peak absorbs high=%.2f"
          % (a_ratio, b_ratio, a_detail.get("exit_class"), b_detail.get("exit_class"),
             _peak(position, ASOF)))
    stable = (
        a_ratio == 1.0 and b_ratio == 1.0
        and a_detail.get("exit_class") == "trailing_stop"
        and b_detail.get("exit_class") == "trailing_stop"
    )
    print("R15-C3  overnight position absorbs the daily high: %s"
          % ("PASS (semantics preserved)" if stable else "UNEXPECTED"))
    return stable


def case_c4():
    """反向敏感性：部分加仓的老仓仍用完整口径（不得误判成新仓）。"""
    position = _partial_add_on()
    a_ratio, _r, _s, a_detail = _sell(position, machine_day=ASOF)
    b_ratio, _r, _s, b_detail = _sell(position, machine_day=MACHINE_OTHER)
    print("R15-C4  partial add-on: ratio=%s/%s exit=%s/%s peak absorbs high=%.2f"
          % (a_ratio, b_ratio, a_detail.get("exit_class"), b_detail.get("exit_class"),
             _peak(position, ASOF)))
    stable = (
        a_ratio == 1.0 and b_ratio == 1.0
        and a_detail.get("exit_class") == "trailing_stop"
        and b_detail.get("exit_class") == "trailing_stop"
    )
    print("R15-C4  partial same-day add-on keeps old-position peak semantics: %s"
          % ("PASS (semantics preserved)" if stable else "UNEXPECTED"))
    return stable


def main():
    print("R15 before-fix differential reproduction "
          "(unmodified production code, pure fixture, no DB / no network / no sleep)")
    c1 = case_c1()
    c2 = case_c2()
    case_c3()
    case_c4()
    print("\nSUMMARY")
    print("  R15-C1 historical same-day position absorbs pre-entry high:  %s"
          % ("REPRODUCED" if c1 else "NOT REPRODUCED"))
    print("  R15-C2 false trailing stop caused by pre-entry high:        %s"
          % ("REPRODUCED" if c2 else "NOT REPRODUCED"))
    print("  NOTE: this probe is a differential reproduction — "
          "'NOT REPRODUCED' on fixed code is the expected after-fix outcome.")


if __name__ == "__main__":
    main()
