# -*- coding: utf-8 -*-
"""模拟盘通用规则与证券权限边界。

这里不打开数据库、不调用行情接口，只处理交易日、费用、证券类型和
板块权限等可重复规则。``paper_trading`` 继续重新导出这些名称，保证
旧的内部调用和回放脚本不需要一次性迁移。
"""

import factors as F
import universe as U


COMMISSION = 0.0001
MIN_COMMISSION = 0.0
STAMP_SELL = 0.0005
SLIPPAGE = 0.001
MAIN_BOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
CHINEXT_PREFIXES = ("300", "301", "302")
STAR_PREFIXES = ("688", "689")
T0_ETF_PREFIXES = ("51", "52", "56", "58", "15", "16", "18")


def next_weekday(value):
    """T+1 在下一个上海交易日解锁，而不是简单的下一个工作日。"""
    return U.next_trade_day(value)


def is_trade_weekday(value):
    return U.is_trade_day(value)


def commission(amount):
    return max(MIN_COMMISSION, amount * COMMISSION)


def is_st_or_delisting(name=None, risk_flag=False):
    """判断证券是否使用 ST/退市风险规则。"""
    label = str(name or "").upper()
    return bool(risk_flag) or "ST" in label or "退" in str(name or "")


def limit_pct(code, name=None, risk_flag=False):
    """返回可交易持仓适用的跌停百分比。"""
    if is_st_or_delisting(name, risk_flag):
        return 5.0
    return F.limit_up_threshold(str(code)) * 100


def asset_type(code, name=None):
    """A 股普通股票 T+1；常见场内 ETF 代码段按 T+0 处理。"""
    code = str(code or "")
    label = str(name or "")
    if code.startswith(T0_ETF_PREFIXES) and (
        "ETF" in label.upper() or code.startswith(("51", "52", "56", "58", "15"))
    ):
        return "etf_t0"
    return "stock_t1"


def security_scope(code, name=None, risk_flag=False):
    """证券权限唯一入口：只允许沪深主板和创业板普通股票。"""
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
