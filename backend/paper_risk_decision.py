# -*- coding: utf-8 -*-
"""确定性风险退出决策引擎（pure deterministic risk-exit domain engine）。

定位
----
``paper_trading`` 负责编排与证据收集（读 DB、读行情、解析策略配置）；本模块只管
一件事——"给定一个已进入风险判定的持仓，应该做出什么确定性的退出决定"。

依赖方向是单向的::

    paper_trading  →  paper_risk_decision

硬边界（由 ``test_paper_trading_architecture_guard.py`` 静态强制）：

* zero DB / network / filesystem / scheduler / global cache
* 不 import ``paper_trading`` / ``strategy_policies`` / ``paper_account_specs``
* **zero wall-clock**：不出现 ``date.today`` / ``datetime.now`` / ``time.time``
* 决策日期只能由 ``asof_day`` 显式传入；缺失即 fail fast

相同输入必须得到结构等价的输出：结果不随机器日期、系统时间、数据库内容或网络
状态变化。

为什么存在（R15 correctness defect）
-----------------------------------
``_sell_plan(asof_day=D)`` 曾把 asof 漏传给峰值口径，峰值 helper 于是回退到
``dt.date.today()``。回放/补算历史日期 ``D`` 时，同日新仓会吸收**买入前**的日内
high，仅凭机器当前日期就凭空制造出移动止损（exit ``none`` → ``trailing_stop``，
``sell_ratio`` 0.0 → 1.0）。本模块把该口径改成"必须显式给出 as-of 日期"，从源头
消除这条隐式依赖。
"""
import datetime as dt

__all__ = [
    "bought_today",
    "position_peak",
    "main_force_intent",
    "evaluate_sell",
]

#: 退出类别按严重度固定排序：更严重者胜，绝不被后续判定覆盖。
#: （P3 审计修复 S4：旧实现顺序执行且后者覆盖前者，"硬止损 + 达到最长持有"的
#: 持仓会被记成较弱的 max_hold，污染恢复观察与自进化样本的退出归因。）
EXIT_SEVERITY = {
    "none": 0,
    "tactical_take_profit": 1,
    "max_hold": 2,
    "trailing_stop": 3,
    "hard_stop": 4,
}

#: 首段减仓之后，全清还需要**独立确认**：要么继续跌破，要么等足时间。
#: 3 分钟一轮的扫描下，仅凭"仍在成本线下"就清仓，会把仓位反复砍在震荡低点。
#: 15 分钟 ≈ 5 轮扫描；0.5% 是相对首段成交价的恶化幅度。
HARD_STOP_FULL_CLEAR_CONFIRM_MINUTES = 15.0
HARD_STOP_FULL_CLEAR_DEEPEN_PCT = 0.005


#: 严格数值契约：只接受 int/float，NaN 视为缺失。
#:
#: 本模块**必须是零项目依赖**的叶子（``test_paper_trading_architecture_guard``
#: 的 guard6a 钉着这条：它是纯决策引擎，不能因为 import 而卷进任何环）。
#: 所以这里保留一份与 ``paper_numeric.num`` 逐字相同的实现——**刻意不去 import**。
#: 改动一处必须同步另一处，``test_paper_numeric_contract`` 会对两者的行为做比对。
def _num(value, default=0.0):
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    return default


def _negative_hits(news, code):
    return [
        row for row in (news or [])
        if row.get("code") == code and row.get("tone", 0) < 0
    ]


def _asof_iso(asof_day):
    """把显式 as-of 日期规整成 ``YYYY-MM-DD``；缺失即 fail fast。

    这里**刻意**没有 ``or date.today()`` 回退：风险决策不允许自行猜"今天"。
    """
    if asof_day is None:
        raise ValueError(
            "risk decision 需要显式 asof_day：不允许回退到机器当前日期"
        )
    if isinstance(asof_day, dt.datetime):
        return asof_day.date().isoformat()
    if isinstance(asof_day, dt.date):
        return asof_day.isoformat()
    text = str(asof_day).strip()
    if not text:
        raise ValueError("risk decision 的 asof_day 不能为空")
    return dt.date.fromisoformat(text[:10]).isoformat()


def bought_today(position, *, asof_day):
    """整仓都是**当日**买入（同日新仓）才适用"买入后峰值"口径。

    2026-08-31 复核 P1：同日新仓在买入后不到一分钟就被按买入前的日内最高价算
    回撤（如开盘 3.62 冲高、3.40 才买入，立刻得到 6%+ 假回撤预警）。同日新仓的
    peak 只从买入后的采样价起记录；次日恢复完整日内最高价口径。部分加仓的老仓
    仍用完整口径（老仓峰值合法）。

    ``asof_day`` 是 keyword-only 且必填：传入 ``None`` 直接 ``ValueError``。
    """
    asof = _asof_iso(asof_day)
    qty = int(_num(position.get("qty")))
    today_qty = int(_num(position.get("today_acquired_qty")))
    return (
        qty > 0 and today_qty >= qty
        and str(position.get("entry_date") or "") == asof
    )


def position_peak(position, quote, price, *, asof_day):
    """统一峰值口径：同日新仓不吸收买入前的日内 high。

    语义（不得改变）：

    * 整仓同日新仓 —— ``max(authoritative peak, current price)``
    * 其它（隔夜仓、部分加仓）—— ``max(authoritative peak, quote high, current price)``
    """
    authoritative = _num(position.get("peak_price"), 0.0)
    if bought_today(position, asof_day=asof_day):
        return max(authoritative, price or 0.0)
    return max(authoritative, _num(quote.get("high"), 0.0), price or 0.0)


#: 证据分两类，缺失代价不同（2026-10-30 P0「下跌确认不可达」）：
#:
#: * 价格类（当日涨跌幅、日内高低价）：直接描述"跌了多少、收在什么位置"。
#:   东财 ulist 的资金字段偶发缺失（``super_net`` / ``main_pct``）时，价格类仍然
#:   齐全；把它们混装成 ``len(missing)/5`` 会让 -8% 的当日跌幅因为**资金**字段
#:   缺失而把 usable 打到 0.6，confidence 永远够不到 0.58 门槛，于是
#:   ``paper_risk_evidence.intraday_downside_guard`` 只能停在上报级，
#:   全天不减仓。
#: * 资金类（主力净流入占比、超大单资金、量比）：只影响"意图有多可信"，
#:   不决定"价格证据是否成立"。
#: 权重按证据强度分开：缺价格证据的代价 0.20，缺资金证据 0.08。
PRICE_EVIDENCE_FIELDS = ("当日涨跌幅", "日内高低价")
FLOW_EVIDENCE_FIELDS = ("主力净流入占比", "超大单资金", "量比")
PRICE_EVIDENCE_MISSING_WEIGHT = 0.20
FLOW_EVIDENCE_MISSING_WEIGHT = 0.08
#: usable 的下限：缺证据可以降低置信度，但不能把结论抹成"零可信"。
CONFIDENCE_USABLE_FLOOR = 0.35

#: ``downturn``（下跌确认）分支的阈值：只吃**价格**证据。
#: 它不参与 0.58 置信度门槛（confidence 照实输出），但要求价格证据齐全
#: （无价格类 missing）、跌幅足够深、且出货分数不低于 50、洗盘不占优。
DOWNTURN_PCT_THRESHOLD = -6.0
DOWNTURN_DISTRIBUTION_FLOOR = 50.0


def main_force_intent(position, quote, market=None, news=None):
    """判断急跌更像"洗盘"还是"出货"——可解释的**风险信号**，不是可观测事实。

    需要价格/资金/量能证据互相印证才会给出标签；缺失字段一律退回 ``uncertain``。
    结果只写入风险复核与卖出计划 payload，本身不是独立的下单触发器。

    ``downturn``（下跌确认）是 2026-10-30 新增的**第四类**结论，专门覆盖
    "价格证据齐全、资金证据缺失"这一种现场：它不等于 ``distribution``
    （没有资金证据就不能断言主力在出货），因此下游只允许**部分**减仓。
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
    missing_fields = sorted(set(missing))
    # 缺失代价分证据类：价格类缺失才真正削弱"跌了多少"这条事实，
    # 资金类缺失只削弱意图判断——两者混装会让资金字段的偶发缺失
    # 把 -8% 的当日跌幅判成"意图不确定"（P0 现场）。
    price_missing = [item for item in missing_fields if item in PRICE_EVIDENCE_FIELDS]
    flow_missing = [item for item in missing_fields if item in FLOW_EVIDENCE_FIELDS]
    usable = max(
        CONFIDENCE_USABLE_FLOOR,
        1.0 - (PRICE_EVIDENCE_MISSING_WEIGHT * len(price_missing)
               + FLOW_EVIDENCE_MISSING_WEIGHT * len(flow_missing)),
    )
    gap = abs(distribution - washout)
    confidence = round(min(0.98, (0.50 + gap / 100.0) * usable), 2)
    if distribution >= 60 and distribution - washout >= 12 and confidence >= 0.58:
        classification, label, hint = "distribution", "疑似出货", "冻结加仓，优先复核可卖仓位"
    elif washout >= 58 and washout - distribution >= 10 and confidence >= 0.58:
        classification, label, hint = "washout", "疑似洗盘", "不因单日下跌单独清仓，等待承接确认"
    elif (
        not price_missing
        and pct is not None and pct <= DOWNTURN_PCT_THRESHOLD
        and distribution >= DOWNTURN_DISTRIBUTION_FLOOR
        and washout < distribution
    ):
        # 价格证据自洽的深跌：不假装是"出货"（没有资金证据），但也不能
        # 退回"意图不确定"从而让整条减仓通道不可达。
        classification, label, hint = (
            "downturn",
            "下跌确认（资金证据缺失）",
            "价格证据充分，允许部分减仓，禁止据此加仓",
        )
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
        "missing": missing_fields,
        "price_evidence_missing": price_missing,
        "flow_evidence_missing": flow_missing,
        "confidence_usable": round(usable, 3),
        "relative_to_market_pct": round(relative, 2) if relative is not None else None,
        "range_position": round(range_pos, 3) if range_pos is not None else None,
        "asof": quote.get("quote_at"),
        "model": "main_force_intent_v1_shadow",
    }


def evaluate_sell(position, quote, *, asof_day, spec, hold_days, news=(),
                  hard_stop_touched_today=False, limit_pct=None,
                  hard_stop_first_trim_ratio=0.35, risk_version="",
                  hard_stop_trim_age_minutes=None, hard_stop_trim_price=None):
    """纯退出决策：硬止损 / 移动止损 / 最长持有 / 阶梯止盈 / 严重度仲裁。

    所有执行判断只依赖显式输入——策略 spec、hold_days、limit_pct、首段减仓比例
    均由调用方（``paper_trading._sell_plan``）解析后传入；本函数不解析策略归属、
    不读行情 provider、不碰数据库、不读 wall clock。

    返回结构（adapter 负责补 orchestration 诊断，如 volatility shadow）::

        status                     "decided" | "no_quote"
        sell_ratio                 卖出比例
        reason                     中文原因串
        next_stage                 推进后的阶梯止盈档位
        exit_class                 退出类别（none/hard_stop/...）
        exit_reason_code           稳定 ASCII 归因码
        exit_marker                当日去重标记（首段减仓）
        ret                        相对成本的收益率（小数）
        drawdown                   相对峰值的回撤（小数）
        strategy_version           生效风险版本
        main_force_intent          主力意图影子信号
        shadow_news_warning_count  负面快讯条数（影子提示，不触发卖出）
    """
    if limit_pct is None:
        raise ValueError("evaluate_sell 需要显式 limit_pct（由调用方解析）")
    price = _num(quote.get("price"), 0)
    cost = _num(position["cost"], 0)
    # 与盘中守护同口径：峰值吸收当日 high，回撤不被 3 分钟采样间隙低估；
    # 同日新仓例外——只用买入后的采样价，不吸收买入前的日内高点。
    peak = position_peak(position, quote, price, asof_day=asof_day)
    ret = price / cost - 1 if cost and price else None
    drawdown = 1 - price / peak if peak and price else None
    # R14：take_stage 的权威在 cycle-owned 风险状态。本周期无状态行（遗留持仓）时
    # 读模型给 ``None`` —— "未知"保持未知：跳过阶梯止盈（绝不猜档位多卖），
    # hard stop / max hold / trailing（peak 缺失锚定成本）照常工作。
    raw_stage = position.get("take_stage")
    stage_known = raw_stage is not None
    next_stage = int(raw_stage) if stage_known else 0
    strategy_version = spec.get("strategy_version") or risk_version
    reasons, sell_ratio = [], 0.0
    exit_class, exit_reason_code = "none", None
    # P1 审计修复（2026-09-02）：卖出状态机的"当日已发生某类退出"判定改为读取订单
    # payload 里的稳定 ASCII 标记，不再对中文 reason 做 LIKE 匹配——文案措辞调整
    # 曾经会静默破坏跌破确认与级别去重。
    exit_marker = None

    def _set_exit(new_class, new_code):
        nonlocal exit_class, exit_reason_code
        if EXIT_SEVERITY[new_class] > EXIT_SEVERITY.get(exit_class, 0):
            exit_class, exit_reason_code = new_class, new_code

    if ret is None:
        return {
            "status": "no_quote",
            "sell_ratio": 0.0,
            "reason": "缺少有效报价",
            "next_stage": next_stage,
            "exit_class": "none",
            "exit_reason_code": None,
            "exit_marker": None,
            "ret": None,
            "drawdown": None,
            "strategy_version": strategy_version,
            "main_force_intent": None,
            "shadow_news_warning_count": 0,
        }
    if ret <= spec["hard_stop"]:
        # 2026-08-28 修复：盘中首次触碰硬止损且非崩盘形态时，不再立即全仓清掉——
        # 先按守卫 partial 比例减仓；后续扫描仍在线下、或当日已做过首段减仓、或已
        # 逼近跌停（崩盘形态）时才全清。避免把单针探底卖在最低点
        # （2026-08-28 601212 案例：7.013 清仓后反弹 +7.6%）。
        pct_now = _num(quote.get("pct"), 0.0)
        crash_tape = pct_now <= -(_num(limit_pct, 0.0) * 0.8)
        if crash_tape:
            reasons.append(f"硬止损 {ret*100:.1f}%（崩盘形态，全清）")
            sell_ratio = 1.0
            _set_exit("hard_stop", "hard_stop")
        elif hard_stop_touched_today:
            # 首段减仓之后不得仅凭"还在线下"就全清：3 分钟一轮的扫描会把仓位
            # 反复砍在震荡低点（601212 式单针探底的翻版）。只有**继续恶化**
            # （较首段成交价再跌 0.5%）或**等足确认时间**（15 分钟 ≈ 5 轮扫描）
            # 才清仓。trim 的时间/成交价拿不到时沿用旧行为，绝不静默不卖。
            trim_price = _num(hard_stop_trim_price, 0.0)
            deepened = (
                trim_price > 0 and price > 0
                and price <= trim_price * (1 - HARD_STOP_FULL_CLEAR_DEEPEN_PCT)
            )
            waited = (
                hard_stop_trim_age_minutes is not None
                and _num(hard_stop_trim_age_minutes, 0.0) >= HARD_STOP_FULL_CLEAR_CONFIRM_MINUTES
            )
            evidence_missing = (
                hard_stop_trim_age_minutes is None and hard_stop_trim_price is None
            )
            if deepened or waited or evidence_missing:
                reasons.append(f"硬止损 {ret*100:.1f}%（跌破确认，全清）")
                sell_ratio = 1.0
                _set_exit("hard_stop", "hard_stop")
            else:
                reasons.append(
                    f"硬止损首段减仓已执行（现距成本 {ret*100:.1f}%）：等待继续跌破 "
                    f"{HARD_STOP_FULL_CLEAR_DEEPEN_PCT*100:.1f}% 或 "
                    f"{HARD_STOP_FULL_CLEAR_CONFIRM_MINUTES:.0f} 分钟确认，本轮不再追加卖出"
                )
                _set_exit("hard_stop", "hard_stop")
        else:
            guard_ratio = _num(hard_stop_first_trim_ratio, 0.35)
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
    if hold_days >= spec["hold_max"]:
        reasons.append(f"达到最长持有 {hold_days} 日")
        sell_ratio = 1.0
        _set_exit("max_hold", "max_hold")
    stages = spec["take_profit"]
    # P2 审计修复（2026-09-02）：跳空越档时单轮内连续消费所有已满足的止盈档位
    # （累计比例，封顶 1.0）。旧实现每轮只消费一档，价格从 +5% 直接跳到 +11% 时
    # 第二档要等下一个 3 分钟轮次，期间暴露于回撤。仍只在无更强退出（硬止损/
    # 移动止损/最长持有）时执行；stage 未知（无同周期风险状态行）时整段跳过 ——
    # 档位历史不可证明就不得据此卖出。
    if sell_ratio == 0 and stage_known:
        while next_stage < len(stages) and ret >= stages[next_stage][0]:
            sell_ratio = min(1.0, sell_ratio + stages[next_stage][1])
            next_stage += 1
            reasons.append(f"阶梯止盈 {ret*100:.1f}%")
            _set_exit("tactical_take_profit", "take_profit")
    # 影子证据（负面快讯）只作提示，永远不单独构成卖出触发。
    shadow_news = _negative_hits(news, position["code"])
    return {
        "status": "decided",
        "sell_ratio": sell_ratio,
        "reason": "；".join(reasons),
        "next_stage": next_stage,
        "exit_class": exit_class,
        "exit_reason_code": exit_reason_code,
        "exit_marker": exit_marker,
        "ret": ret,
        "drawdown": drawdown,
        "strategy_version": strategy_version,
        "main_force_intent": main_force_intent(position, quote, news=news),
        "shadow_news_warning_count": len(shadow_news),
    }
