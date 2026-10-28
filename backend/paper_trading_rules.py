# -*- coding: utf-8 -*-
"""模拟盘通用规则与证券权限边界。

这里不打开数据库、不调用行情接口，只处理交易日、费用、证券类型和
板块权限等可重复规则。``paper_trading`` 继续重新导出这些名称，保证
旧的内部调用和回放脚本不需要一次性迁移。
"""

# A1 phase 2：整手数量的唯一 owner（引擎此前只在 paper_trading 里定义）。
LOT_SIZE = 100
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
    import universe as U
    return U.next_trade_day(value)


def is_trade_weekday(value):
    import universe as U
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
    import factors as F
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


def selection_buy_scope(code, name=None, risk_flag=None, instrument_type=None):
    """Single read-side eligibility rule for all buy suggestions.

    Execution has an equivalent hard gate in ``paper_trading``.  The public
    scanner/detail APIs must not describe a restricted board, ST stock or a
    non-equity instrument as a buyable idea simply because it has six digits.
    """
    code = str(code or "").strip()
    label = str(name or "").strip()
    upper = label.upper()
    kind = str(instrument_type or "").strip().lower()
    risk = str(risk_flag or "").strip().lower() in {"1", "true", "yes", "y", "risk", "st"}
    if len(code) != 6 or not code.isdigit():
        return {"allowed": False, "reason": "证券代码格式无效"}
    if risk or "ST" in upper or "退" in label:
        return {"allowed": False, "reason": "ST/退市风险证券不提供买入建议"}
    if kind in {"etf", "fund", "index", "bond", "convertible_bond", "unknown"}:
        return {"allowed": False, "reason": "当前系统仅研究可交易的沪深主板与创业板普通股"}
    if any(token in upper for token in ("ETF", "LOF", "基金", "指数")):
        return {"allowed": False, "reason": "基金或指数不属于当前股票买入范围"}
    if code.startswith(("688", "689")):
        return {"allowed": False, "reason": "科创板仅作产业共振参考，不生成买入建议"}
    if code.startswith(("4", "8", "92")):
        return {"allowed": False, "reason": "北交所股票不在当前交易权限范围"}
    if not code.startswith(("000", "001", "002", "003", "600", "601", "603", "605", "300", "301", "302")):
        return {"allowed": False, "reason": "不属于当前允许的沪深主板或创业板普通股范围"}
    return {"allowed": True, "reason": "沪深主板/创业板普通股"}
