# -*- coding: utf-8 -*-
"""模拟盘通用规则与证券权限边界。

这里不打开数据库、不调用行情接口，只处理交易日、费用、证券类型和
板块权限等可重复规则。``paper_trading`` 继续重新导出这些名称，保证
旧的内部调用和回放脚本不需要一次性迁移。
"""

from decimal import Decimal, ROUND_HALF_UP

COMMISSION = 0.0001
MIN_COMMISSION = 0.0
STAMP_SELL = 0.0005
SLIPPAGE = 0.001
SIMULATION_RULESET_VERSION = "a-share-simulation-v1"
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


def simulated_execution_terms(reference_price, side, quantity, *, limit_price=None):
    """统一计算模拟成交价、金额、费用和滑点，供执行决策与下单预算共用。"""
    normalized_side = str(side or "").strip().lower()
    qty = max(0, int(quantity or 0))
    reference = float(reference_price or 0)
    if normalized_side not in {"buy", "sell"}:
        raise ValueError(f"unsupported execution side: {side!r}")
    if reference <= 0 or qty <= 0:
        return {
            "reference_price": reference, "fill_price": 0.0, "amount": 0.0,
            "commission": 0.0, "stamp_duty": 0.0, "fees": 0.0,
            "slippage_amount": 0.0, "pricing_basis": "invalid_reference",
        }
    direction = 1.0 if normalized_side == "buy" else -1.0
    fill_price = reference * (1.0 + direction * SLIPPAGE)
    basis = "reference_plus_slippage" if normalized_side == "buy" else "reference_minus_slippage"
    if limit_price is not None:
        limit_value = float(limit_price or 0)
        if limit_value <= 0:
            raise ValueError("limit_price must be positive")
        bounded_price = min(fill_price, limit_value) if normalized_side == "buy" else max(fill_price, limit_value)
        if bounded_price != fill_price:
            basis += "_limit_capped"
        fill_price = bounded_price
    fill_price = round(fill_price, 4)
    amount = qty * fill_price
    commission_amount = commission(amount)
    stamp = amount * STAMP_SELL if normalized_side == "sell" else 0.0
    return {
        "reference_price": reference,
        "fill_price": fill_price,
        "amount": round(amount, 2),
        "commission": round(commission_amount, 2),
        "stamp_duty": round(stamp, 2),
        "fees": round(commission_amount + stamp, 2),
        "slippage_amount": round((fill_price - reference) * qty, 2),
        "pricing_basis": basis,
    }


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


def price_limit_band(code, name=None, risk_flag=False, prev_close=None):
    """计算普通 A 股当日涨跌停价，供后端成交规则统一限价。

    ``limit_pct`` 是用于提前拦截的保守触发线（例如主板 9.5%），不是交易所
    涨跌停幅度。成交边界按证券板块规则计算并四舍五入到分；行情源明确提供
    涨跌停价时应优先使用行情证据中的价格。
    """
    reference = Decimal(str(prev_close or 0))
    if reference <= 0:
        return None
    code = str(code or "")
    if is_st_or_delisting(name, risk_flag):
        pct = Decimal("0.05")
    elif code.startswith(("300", "301", "302", "688", "689")):
        pct = Decimal("0.20")
    elif code.startswith(("4", "8", "92")):
        pct = Decimal("0.30")
    else:
        pct = Decimal("0.10")
    tick = Decimal("0.01")
    upper = (reference * (Decimal("1") + pct)).quantize(tick, rounding=ROUND_HALF_UP)
    lower = (reference * (Decimal("1") - pct)).quantize(tick, rounding=ROUND_HALF_UP)
    return {"limit_up_price": float(upper), "limit_down_price": float(lower),
            "limit_pct": float(pct * 100), "source": "a_share_board_rule"}


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
