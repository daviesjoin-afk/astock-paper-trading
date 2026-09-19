# -*- coding: utf-8 -*-
"""定时调仓扫描模块。

流程：
1. 每晚22:00扫描所有持仓，评估质量变化 + 当日公告/新闻
2. 生成调仓计划（卖出/减仓/持有）
3. 次日开盘(09:25-09:40)用开盘数据验证计划
4. 验证通过后执行换仓

设计原则：
- 晚间扫描可结合当日公告、新闻做综合判断
- 收盘扫描只生成计划，不执行
- 次日开盘验证是最终决策点（看走势、资金量）
- 卖出后自动从等待池选替补
- 无冷却期限制
- 所有决策完整审计
"""
from __future__ import annotations

import datetime as dt
import json

import execution_verification as EV
import paper_position_read_model as PPRM
import paper_schema_migrations as PSM

# ─── 调仓参数 ───
REBALANCE_VERSION = "daily-rebalance-v2"

# 晚间扫描触发时间（22:00，可结合当日公告分析）
CLOSE_SCAN_HOUR = 22
CLOSE_SCAN_MINUTE = 0

# 次日开盘验证窗口
OPEN_VERIFY_START = (9, 25)  # 集合竞价结束
OPEN_VERIFY_END = (9, 40)    # 开盘后10分钟

# 调仓阈值（按策略类型差异化）
REBALANCE_THRESHOLDS = {
    # 质量分下降超过此值触发调仓评估
    # 短线策略更敏感，中线策略稍宽松
    "quality_score_decline": 12.0,
    # 持仓收益率低于此值（亏损）且质量分下降，触发止损换仓
    "loss_with_decline_pct": -3.0,
    # 持仓天数超过此值且收益低于2%，触发换仓
    # 短线策略（三日）4天，波段策略8天，轮动策略6天
    "stale_hold_days_by_strategy": {
        "tq_breakout": 4,
        "trend_pullback": 8,
        "sector_rotation": 6,
        "reported_profit_breakout": 6,
        "main_force_top10": 4,
        "_default": 6,
    },
    # 连续N日资金净流出，触发换仓评估
    "consecutive_outflow_days": 3,
    # 当日跌幅超过此值（收盘），触发次日开盘换仓
    "close_drop_pct": -3.0,
    # 当日涨幅超过此值（收盘），考虑次日止盈
    "close_rise_pct": 7.0,
    # 开盘跌幅超过此值，确认卖出
    "open_gap_down_pct": -3.0,
    # 开盘涨幅超过此值，确认止盈
    "open_gap_up_pct": 5.0,
    # 开盘量比低于此值，流动性不足
    "open_low_volume_ratio": 0.5,
    # 公告负面关键词（命中则加强卖出信号）
    "negative_announcement_keywords": [
        "退市", "ST", "亏损", "减持", "违规", "处罚", "立案",
        "业绩预减", "业绩预亏", "净利润下降", "重大风险",
        "停牌", "终止上市", "暂停上市",
    ],
}

# 替补候选选择参数
REPLACEMENT_PARAMS = {
    # 替补候选最低分数
    "min_replacement_score": 55.0,
    # 最多同时换仓数（每日）
    "max_rotations_per_day": 3,
    # 无冷却期限制
}


def _now():
    import datetime as _dt
    from zoneinfo import ZoneInfo
    return _dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def _date(value=None):
    # ``_dt`` 曾只在 _now() 的局部作用域导入，字符串/datetime 分支会
    # NameError。模块顶部已有 ``import datetime as dt``，直接复用。
    if value is None:
        return _now().date()
    if isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return _now().date()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)


def _num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _load_json(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return default if default is not None else {}


# ─── 数据库 Schema ───

def ensure_schema(conn):
    """创建调仓相关的数据库表（周期归属由迁移模块持有，幂等）。

    本函数**不再**自己写 DDL：三张状态表的规范形状（含 ``cycle_id`` 与把周期
    算进唯一契约的 UNIQUE / PRIMARY KEY）由
    :func:`paper_schema_migrations.ensure_rebalance_state_cycle_ownership` 单一持有。
    在 scanner 里再写一份 ``CREATE TABLE`` 会造出第二个真相来源 —— 而且这里
    写的 DDL 只在表**不存在**时生效，一个已经存在的旧表会**静默保留**
    ``UNIQUE(scan_date, account_id, code)`` 这个跨周期错误约束。
    """
    PSM.ensure_rebalance_state_cycle_ownership(conn)


# ─── 周期归属 ───

class NoActiveCycle(RuntimeError):
    """没有可归属的 paper cycle。

    ``daily_close_scan`` / ``verify_all_plans`` 是**写**路径：它们落下的每一条
    scan / plan 都必须是"某个周期的事实"。没有 active cycle 时既不能猜一个
    （``MAX(paper_cycles.id)`` / 账户绑定 / 日期都与"当时 active"不是一回事），
    也不能落一条 ``cycle_id=NULL`` 的新行 —— 那会把一条**今天的决策**伪装成
    "legacy 归属不可证明"，而 legacy 语义是给升级前的历史行保留的。
    因此 fail closed：不扫描、不写状态。
    """


def resolve_cycle_id(conn):
    """**只读**解析当前 active cycle id；不存在返回 ``None``（绝不创建周期）。

    复用 :func:`paper_position_read_model.active_cycle_id` —— 它已经是全仓库
    「现在属于哪个周期」的唯一只读实现，且**不会**像 ``_active_cycle`` 那样在
    没有周期时顺手 ``_ensure_cycle`` 插一个（那等于用一次写操作回答查询）。
    """
    return PPRM.active_cycle_id(conn)


def _require_cycle_id(cycle_id, *, operation):
    """把 ``cycle_id=None`` 挡在写路径之外（fail closed，不是 permissive fallback）。"""
    if cycle_id is None:
        raise NoActiveCycle(
            f"{operation}: 没有 active paper cycle，拒绝写入无归属的调仓状态"
        )
    return int(cycle_id)


def _is_risk_handled(conn, account_id, code, today, cycle_id):
    """检查该持仓是否已被实时风控系统处理（或待处理）。

    实时风控覆盖的场景（调仓系统不应重复）：
    - 硬止损、移动止损
    - 阶梯止盈
    - 跌停卖出
    - 已有待执行的卖出委托

    ``cycle_id`` 是**必须**的：``paper_orders.cycle_id`` 自 v18 起就是不可变的
    写入期事实，因此"这条卖出委托属于哪个周期"是可判定的。只按
    ``created_at >= today`` 判断则不然 —— 同一天从 cycle 8 翻到 cycle 9 时，
    cycle 8 在当天早些时候落下的卖出委托仍然满足"今天"，会把 cycle 9 里一个
    **全新的**持仓判成 ``risk_handled=True``，从而让调仓系统整轮跳过它。

    这里刻意**不**用"日期窗口"代替周期身份：日期窗口只是周期的近似，
    近似会在同日翻周期这个真实场景上失效。

    Returns:
        dict: {
            handled: bool,
            reason: str,
            order_id: int or None,
        }
    """
    cycle_id = _require_cycle_id(cycle_id, operation="_is_risk_handled")
    # 检查是否有当日待执行的卖出委托
    pending_sell = conn.execute(
        """SELECT id, status, reason FROM paper_orders
           WHERE account_id=? AND code=? AND side='sell' AND cycle_id=?
             AND status IN ('pending_limit', 'unfilled_limit_down', 'entry_frozen_waitlist')
             AND created_at >= ?
           ORDER BY id DESC LIMIT 1""",
        (account_id, code, cycle_id, today.isoformat())
    ).fetchone()
    if pending_sell:
        return {
            "handled": True,
            "reason": f"已有待执行卖出委托 #{pending_sell[0]}（{pending_sell[1]}）",
            "order_id": pending_sell[0],
        }

    # 检查当日是否已成功卖出。"今日已卖出"是**成交声称**：只有被证据证明卖出的
    # 委托才能抑制今日的换仓动作，否则一个"没发生过的卖出"会挡住真实换仓。
    filled_sell = conn.execute(
        """SELECT id, status FROM paper_orders
           WHERE account_id=? AND code=? AND side='sell' AND cycle_id=?
             AND status='filled'
             AND """ + EV.VERIFIED_PREDICATE + """
             AND created_at >= ?
           ORDER BY id DESC LIMIT 1""",
        (account_id, code, cycle_id, today.isoformat())
    ).fetchone()
    if filled_sell:
        return {
            "handled": True,
            "reason": f"今日已卖出（委托 #{filled_sell[0]}）",
            "order_id": filled_sell[0],
        }

    # 检查最近一次风险审计是否已触发该股票的卖出。
    #
    # 这里原本查询 ``risk_log`` 表 —— 但**该表在生产 schema 中不存在**
    # （``paper_trading.init_db()`` 建出的 40 张表里没有它，全仓库也没有任何
    # 写入点；``risk_log`` 是 ``_risk_log()`` 这个**函数名**，不是表名）。
    # 结果是这一句必然抛 ``no such table: risk_log``，让整个调仓扫描 100% 失败。
    #
    # 权威替代来源是 ``paper_orders.risk_payload``：风控退出在落库时由
    # ``_sell_plan`` 写入 ``exit_class`` / ``exit_reason_code``，而生产代码自己判定
    # 「当日是否已发生某类退出」用的就是同一个来源（见 ``paper_trading`` 里的
    # ``hard_stop_touched_today``）。这里沿用同一口径，不再引入第二套读法。
    #
    # 与上面的 ``filled_sell`` 一致，只有**被证据证明成交**的卖出才抑制换仓：
    # 一个"没发生过的卖出"不该挡住真实换仓。窗口比 ``filled_sell`` 宽一天，
    # 以覆盖昨日盘尾触发的风控退出。
    recent_risk = conn.execute(
        """SELECT id, reason FROM paper_orders
           WHERE account_id=? AND code=? AND side='sell' AND cycle_id=?
             AND status='filled'
             AND """ + EV.VERIFIED_PREDICATE + """
             AND created_at >= ?
             AND (
                 json_extract(risk_payload,'$.exit_class') IN (
                     'hard_stop', 'trailing_stop', 'max_hold', 'tactical_take_profit')
                 OR json_extract(risk_payload,'$.exit_reason_code') IN (
                     'hard_stop', 'trailing_stop', 'max_hold', 'take_profit')
             )
           ORDER BY id DESC LIMIT 1""",
        (account_id, code, cycle_id, (today - dt.timedelta(days=1)).isoformat())
    ).fetchone()
    if recent_risk:
        return {
            "handled": True,
            "reason": f"实时风控已触发卖出（委托 #{recent_risk[0]}）：{recent_risk[1][:50] if recent_risk[1] else ''}",
            "order_id": recent_risk[0],
        }

    return {"handled": False, "reason": "", "order_id": None}


def scan_positions_quality(conn, account_id, positions, quotes, factor_table=None,
                           news=None, cycle_id=None):
    """扫描单个账户的所有持仓，评估质量变化。

    与实时风控协调：
    - 已有卖出委托/已卖出/已触发止损的持仓 → 跳过
    - 只对"实时风控未覆盖"的持仓生成调仓计划
    - 调仓关注的是：质量衰退、资金趋势、公告风险、持仓过久

    ``cycle_id`` 必须由调用方**一次**解析后显式传入，scanner 内部绝不各自重新
    解析 active cycle —— 否则同一个 scan 事务里的不同 helper 可能看到不同的周期。

    Args:
        conn: 数据库连接
        account_id: 账户ID
        positions: 持仓列表
        quotes: 实时报价 {code: quote}
        factor_table: 因子表（可选）
        news: 新闻/公告列表 [{code, title, content, type, ...}]
        cycle_id: 本次扫描的 paper cycle（必须；无周期时 fail closed）

    Returns:
        list: 每个持仓的评估结果
    """
    cycle_id = _require_cycle_id(cycle_id, operation="scan_positions_quality")
    results = []
    today = _date()
    thresholds = REBALANCE_THRESHOLDS
    negative_keywords = thresholds.get("negative_announcement_keywords", [])

    for pos in positions:
        code = str(pos.get("code", ""))
        if not code:
            continue

        quote = quotes.get(code, {})
        price = _num(quote.get("price"), _num(pos.get("cost")))
        cost = _num(pos.get("cost"))
        qty = int(pos.get("qty", 0))

        # 计算收益率
        unrealized_pnl = (price / cost - 1) if cost > 0 and price > 0 else 0.0

        # 计算持仓天数
        entry_date = _date(pos.get("entry_date")) if pos.get("entry_date") else today
        hold_days = max(0, (today - entry_date).days)

        # 获取当前质量分
        quality_score = _num(
            (pos.get("latest_quality_review") or {}).get("score"),
            _num(pos.get("quality_score"), 50.0)
        )

        # 获取历史质量分（**同一个 cycle** 的上次扫描）
        #
        # 原实现是 ``WHERE account_id=? AND code=? ORDER BY id DESC LIMIT 1``，
        # 即"这只票上一次扫描"——**跨周期**。cycle 9 的第一次扫描会读到 cycle 8
        # 的分数当基线，于是 quality_change 是"相对上一个周期"的差值：一个在
        # cycle 9 完全没变过的持仓会被判成质量大幅衰退并生成卖出计划。
        # 新周期的第一次扫描没有同周期历史 ⇒ 沿用"无上次扫描"语义
        # （``prev_quality_score = quality_score``，即 change=0），绝不借旧周期基线。
        prev_scan = conn.execute(
            """SELECT quality_score FROM rebalance_scans
               WHERE cycle_id=? AND account_id=? AND code=?
               ORDER BY id DESC LIMIT 1""",
            (cycle_id, account_id, code)
        ).fetchone()
        prev_quality_score = _num(prev_scan[0], quality_score) if prev_scan else quality_score
        quality_change = quality_score - prev_quality_score

        # 分析资金流趋势
        super_net = _num(quote.get("super_net"))
        fund_flow_trend = "inflow" if super_net > 0 else ("outflow" if super_net < 0 else "neutral")

        # 检查连续流出天数（同周期）
        consecutive_outflow = _check_consecutive_outflow(conn, account_id, code, today, cycle_id)

        # ── 检查实时风控是否已处理（同周期） ──
        risk_status = _is_risk_handled(conn, account_id, code, today, cycle_id)

        # ── 分析当日公告/新闻 ──
        news_risk = _analyze_news_for_code(code, news, negative_keywords)
        if news_risk["has_negative"]:
            quality_change = quality_change - news_risk["penalty"]
            news_reason = f"；负面公告：{news_risk['summary']}"
        else:
            news_reason = ""

        # ── 决策（协调模式）──
        if risk_status["handled"]:
            # 实时风控已处理，调仓系统只记录不干预
            action = "risk_handled"
            reason = risk_status["reason"]
            sell_ratio = 0.0
        else:
            # 实时风控未处理，调仓系统评估
            action, reason, sell_ratio = _decide_rebalance_action(
                quality_score=quality_score,
                quality_change=quality_change,
                unrealized_pnl=unrealized_pnl,
                hold_days=hold_days,
                fund_flow_trend=fund_flow_trend,
                consecutive_outflow=consecutive_outflow,
                super_net=super_net,
                account_id=account_id,
            )

            # 如果有负面新闻，升级动作
            if news_risk["has_negative"] and action == "hold":
                action = "reduce"
                reason = "持仓质量稳定，但存在负面公告"
                sell_ratio = 0.5
            elif news_risk["has_critical"] and action in ("hold", "reduce"):
                action = "sell"
                reason = "重大负面公告，建议清仓"
                sell_ratio = 1.0

            reason = reason + news_reason

        result = {
            "account_id": account_id,
            "code": code,
            "name": pos.get("name", ""),
            "current_qty": qty,
            "cost": cost,
            "current_price": price,
            "unrealized_pnl_pct": round(unrealized_pnl * 100, 2),
            "hold_days": hold_days,
            "quality_score": round(quality_score, 2),
            "prev_quality_score": round(prev_quality_score, 2),
            "quality_change": round(quality_change, 2),
            "fund_flow_trend": fund_flow_trend,
            "consecutive_outflow_days": consecutive_outflow,
            "news_risk": news_risk,
            "risk_handled": risk_status["handled"],
            "risk_reason": risk_status["reason"],
            "action": action,
            "action_reason": reason,
            "planned_sell_ratio": sell_ratio,
        }
        results.append(result)

        # 保存扫描结果（包括 risk_handled 的，用于追踪）
        #
        # ``INSERT OR REPLACE`` 的冲突目标现在含 ``cycle_id``：同一天在两个
        # 周期里扫描同一 account/code 会留下**两行**，而不是把旧周期那一行
        # 覆盖掉。这正是 §18 的 same-day rollover 承重场景。
        conn.execute(
            """INSERT OR REPLACE INTO rebalance_scans(
                cycle_id, scan_date, account_id, code, name, current_qty, cost,
                current_price, unrealized_pnl_pct, hold_days, quality_score,
                prev_quality_score, quality_change, fund_flow_trend,
                consecutive_outflow_days, action, action_reason, planned_sell_ratio,
                scan_version, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle_id, today.isoformat(), account_id, code, pos.get("name"), qty, cost, price,
             round(unrealized_pnl * 100, 2), hold_days, quality_score, prev_quality_score,
             quality_change, fund_flow_trend, consecutive_outflow,
             action, reason, sell_ratio, REBALANCE_VERSION, _now().isoformat())
        )

    return results


def _analyze_news_for_code(code, news, negative_keywords):
    """分析某只股票的公告/新闻风险。

    Returns:
        dict: {
            has_negative: bool,
            has_critical: bool,
            penalty: float,
            summary: str,
            items: list,
        }
    """
    if not news:
        return {"has_negative": False, "has_critical": False, "penalty": 0, "summary": "", "items": []}

    code_news = [n for n in news if str(n.get("code", "")) == str(code)]
    if not code_news:
        return {"has_negative": False, "has_critical": False, "penalty": 0, "summary": "", "items": []}

    negative_items = []
    critical_items = []

    critical_keywords = ["退市", "ST", "暂停上市", "终止上市", "立案", "重大违法"]

    for item in code_news:
        title = str(item.get("title", ""))
        content = str(item.get("content", ""))
        text = title + " " + content

        is_critical = any(kw in text for kw in critical_keywords)
        is_negative = any(kw in text for kw in negative_keywords)

        if is_critical:
            critical_items.append(item)
        elif is_negative:
            negative_items.append(item)

    penalty = 0.0
    summary_parts = []

    if critical_items:
        penalty = 20.0
        summary_parts.append(f"{len(critical_items)}条重大负面")
    if negative_items:
        penalty = max(penalty, 10.0)
        summary_parts.append(f"{len(negative_items)}条负面公告")

    return {
        "has_negative": bool(negative_items or critical_items),
        "has_critical": bool(critical_items),
        "penalty": penalty,
        "summary": "、".join(summary_parts) if summary_parts else "",
        "items": critical_items + negative_items,
    }


def _check_consecutive_outflow(conn, account_id, code, today, cycle_id):
    """检查**同一周期内**的连续资金流出天数。

    原实现只按 ``account_id`` / ``code`` 取最近 5 行，于是 cycle 8 攒下的
    outflow 连续天数会直接算进 cycle 9 的第一天：cycle 9 一个全新的持仓可能
    在"连续 5 日净流出"的名义下被判成卖出，而这 5 天一天都不属于 cycle 9。
    连续流出是一个**周期内的**观测序列，必须与周期一起分区。
    """
    cycle_id = _require_cycle_id(cycle_id, operation="_check_consecutive_outflow")
    rows = conn.execute(
        """SELECT fund_flow_trend FROM rebalance_scans
           WHERE cycle_id=? AND account_id=? AND code=?
           ORDER BY scan_date DESC LIMIT 5""",
        (cycle_id, account_id, code)
    ).fetchall()
    count = 0
    for row in rows:
        if row[0] == "outflow":
            count += 1
        else:
            break
    return count


def _decide_rebalance_action(quality_score, quality_change, unrealized_pnl,
                              hold_days, fund_flow_trend, consecutive_outflow,
                              super_net, account_id=""):
    """决定调仓动作（仅处理实时风控未覆盖的场景）。

    实时风控已覆盖（调仓系统不重复）：
    - 硬止损（hard_stop）
    - 移动止损（trailing_stop）
    - 阶梯止盈（take_profit）
    - 涨跌停卖出

    调仓系统覆盖（实时风控未处理）：
    - 质量分持续衰退
    - 资金连续流出
    - 持仓过久但未触发止损/止盈
    - 公告/新闻风险
    - 替补候选更优时的主动换仓

    Returns:
        (action, reason, sell_ratio)
        action: "hold" | "reduce" | "sell" | "urgent_sell"
        sell_ratio: 0~1
    """
    thresholds = REBALANCE_THRESHOLDS
    reasons = []

    # 获取策略对应的持仓天数上限
    stale_days_map = thresholds.get("stale_hold_days_by_strategy", {})
    max_hold = stale_days_map.get(account_id, stale_days_map.get("_default", 6))

    # ── 场景1：亏损 + 质量恶化 → 建议卖出 ──
    # 注意：这里不触发硬止损（那是实时风控的事），而是"质量止损"
    if unrealized_pnl < thresholds["loss_with_decline_pct"] / 100 and quality_change < -thresholds["quality_score_decline"]:
        reasons.append(f"亏损{unrealized_pnl*100:.1f}%且质量分下降{abs(quality_change):.1f}（质量止损）")
        return "sell", "；".join(reasons), 1.0

    # ── 场景2：持仓过久 + 收益不足 → 建议换仓 ──
    # 实时风控的 max_hold 是硬上限，这里是"收益效率"评估
    if hold_days >= max_hold and unrealized_pnl < 0.02:
        reasons.append(f"持仓{hold_days}天（上限{max_hold}天），收益率仅{unrealized_pnl*100:.1f}%，效率低")
        return "sell", "；".join(reasons), 1.0

    # ── 场景3：资金持续流出 → 评估减仓 ──
    if consecutive_outflow >= thresholds["consecutive_outflow_days"]:
        reasons.append(f"连续{consecutive_outflow}日资金净流出")
        if unrealized_pnl < 0:
            return "sell", "；".join(reasons), 1.0
        elif unrealized_pnl < 0.05:
            return "reduce", "；".join(reasons), 0.5

    # ── 场景4：质量分大幅下降 → 评估减仓 ──
    if quality_change < -thresholds["quality_score_decline"]:
        reasons.append(f"质量分下降{abs(quality_change):.1f}")
        if unrealized_pnl < 0:
            return "sell", "；".join(reasons), 1.0
        elif unrealized_pnl < 0.05:
            return "reduce", "；".join(reasons), 0.5

    # ── 场景5：质量分小幅下降 + 资金流出 → 轻度减仓 ──
    if quality_change < -5 and fund_flow_trend == "outflow":
        reasons.append(f"质量分下降{abs(quality_change):.1f}且资金流出")
        if unrealized_pnl < 0:
            return "reduce", "；".join(reasons), 0.5
        elif unrealized_pnl < 0.03:
            return "reduce", "；".join(reasons), 0.3

    # ── 场景6：收益尚可但质量恶化 → 观察/轻度减仓 ──
    if quality_change < -8 and unrealized_pnl > 0.05:
        reasons.append(f"质量分下降{abs(quality_change):.1f}，建议部分止盈")
        return "reduce", "；".join(reasons), 0.3

    # 默认持有
    return "hold", "持仓质量稳定", 0.0


def daily_close_scan(conn, accounts, quotes, factor_table=None, news=None, cycle_id=None):
    """每日晚间调仓扫描。

    与实时风控协调：
    - 先扫描所有持仓的质量状态
    - 只为"实时风控未覆盖"的持仓生成调仓计划
    - 调仓计划在次日开盘验证后才执行

    不变量::

        One scan = one transaction = one paper snapshot = one cycle identity.

    ``cycle_id`` 由**调用方**在打开连接后只读解析一次并显式传入；scanner 内部
    的任何 helper 都不再各自重新解析 active cycle。缺省 ``None`` **不是**
    permissive fallback —— 没有 active cycle 时本函数 fail closed（抛
    :class:`NoActiveCycle`），绝不落下无归属的 scan / plan。

    Args:
        conn: 数据库连接
        accounts: 账户列表 [{id, positions: [...]}]
        quotes: 全市场报价
        factor_table: 因子表
        news: 新闻列表
        cycle_id: 本次扫描的 paper cycle（必须；无周期则 fail closed）

    Returns:
        dict: 扫描结果
    """
    cycle_id = _require_cycle_id(cycle_id, operation="daily_close_scan")
    ensure_schema(conn)
    today = _date()
    all_results = []
    plans = []
    risk_handled_count = 0

    for account in accounts:
        account_id = str(account.get("id", ""))
        positions = account.get("positions", [])
        if not positions:
            continue

        # 扫描持仓质量
        results = scan_positions_quality(
            conn, account_id, positions, quotes,
            factor_table=factor_table, news=news, cycle_id=cycle_id,
        )
        all_results.extend(results)

        # 统计 risk_handled 的持仓
        for result in results:
            if result["action"] == "risk_handled":
                risk_handled_count += 1
                continue  # 跳过已由实时风控处理的持仓

        # 只为"实时风控未覆盖"且需要调仓的持仓生成计划
        for result in results:
            if result["action"] in ("sell", "reduce", "urgent_sell"):
                sell_qty = int(result["current_qty"] * result["planned_sell_ratio"])
                # 确保卖出数量是100的整数倍
                sell_qty = max(100, (sell_qty // 100) * 100)
                sell_qty = min(sell_qty, result["current_qty"])

                plan = {
                    "cycle_id": cycle_id,
                    "plan_date": today.isoformat(),
                    "account_id": account_id,
                    "code": result["code"],
                    "name": result["name"],
                    "action": result["action"],
                    "sell_qty": sell_qty,
                    "sell_ratio": result["planned_sell_ratio"],
                    "sell_reason": result["action_reason"],
                    "status": "planned",
                }
                plans.append(plan)

                # 保存计划。``cycle_id`` 是 **creation-time fact** —— 写入即定，
                # 之后 verify 绝不再猜归属（见 verify_all_plans 的 defense in depth）。
                # 计划周期与扫描周期必然相同：它们来自同一次调用、同一个 cycle_id。
                conn.execute(
                    """INSERT INTO rebalance_plans(
                        cycle_id, plan_date, account_id, code, name, action,
                        sell_qty, sell_ratio, sell_reason, status,
                        plan_version, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cycle_id, today.isoformat(), account_id, result["code"], result["name"],
                     result["action"], sell_qty, result["planned_sell_ratio"],
                     result["action_reason"], "planned", REBALANCE_VERSION,
                     _now().isoformat(), _now().isoformat())
                )

    # 统计
    summary = {
        "cycle_id": cycle_id,
        "scan_date": today.isoformat(),
        "version": REBALANCE_VERSION,
        "total_positions": len(all_results),
        "risk_handled": risk_handled_count,
        "rebalance_evaluated": len(all_results) - risk_handled_count,
        "hold": sum(1 for r in all_results if r["action"] == "hold"),
        "reduce": sum(1 for r in all_results if r["action"] == "reduce"),
        "sell": sum(1 for r in all_results if r["action"] == "sell"),
        "urgent_sell": sum(1 for r in all_results if r["action"] == "urgent_sell"),
        "plans_created": len(plans),
        "details": all_results,
        "plans": plans,
        "coordination": {
            "rule": "实时风控覆盖止损/止盈/跌停，调仓系统覆盖质量衰退/资金趋势/公告风险",
            "risk_handled_positions": risk_handled_count,
            "rebalance_positions": len(all_results) - risk_handled_count,
        },
    }

    return summary


# ─── 次日开盘验证 ───

def verify_opening_data(conn, plan, open_quote, market_data=None):
    """用开盘数据验证调仓计划。

    Args:
        plan: 调仓计划
        open_quote: 开盘报价 {price, pct, volume_ratio, super_net, ...}
        market_data: 市场整体数据（可选）

    Returns:
        dict: {verified: bool, reason: str, adjusted_action: str}
    """
    thresholds = REBALANCE_THRESHOLDS
    reasons = []
    verified = True
    adjusted_action = plan.get("action", "sell")

    open_price = _num(open_quote.get("price"))
    open_pct = _num(open_quote.get("pct"))
    open_volume_ratio = _num(open_quote.get("vol_ratio"), 1.0)
    open_super_net = _num(open_quote.get("super_net"))

    # 检查1：开盘跌幅过大 → 确认卖出
    if open_pct < thresholds["open_gap_down_pct"]:
        reasons.append(f"开盘跌幅{open_pct:.1f}%，确认卖出止损")
        adjusted_action = "sell"
        verified = True

    # 检查2：开盘涨幅过大 → 考虑止盈
    elif open_pct > thresholds["open_gap_up_pct"]:
        reasons.append(f"开盘涨幅{open_pct:.1f}%，考虑止盈")
        adjusted_action = "sell"
        verified = True

    # 检查3：开盘量比过低 → 流动性风险
    elif open_volume_ratio < thresholds["open_low_volume_ratio"]:
        reasons.append(f"开盘量比{open_volume_ratio:.2f}，流动性不足")
        # 流动性不足时，如果是 reduce 计划，改为 hold
        if plan.get("action") == "reduce":
            adjusted_action = "hold"
            verified = False
            reasons.append("流动性不足，暂缓减仓")

    # 检查4：开盘资金大幅流出 → 确认卖出
    if open_super_net < -10000000:  # 超大单净流出超过1000万
        reasons.append(f"开盘超大单净流出{open_super_net/10000:.0f}万")
        if plan.get("action") in ("sell", "urgent_sell"):
            verified = True

    # 检查5：开盘资金大幅流入 → 可能取消卖出
    elif open_super_net > 5000000:  # 超大单净流入超过500万
        if plan.get("action") == "reduce":
            adjusted_action = "hold"
            verified = False
            reasons.append("开盘资金流入，暂缓减仓")

    # 如果没有特别情况，维持原计划
    if not reasons:
        reasons.append("开盘数据正常，维持原计划")
        verified = plan.get("action") in ("sell", "urgent_sell")

    return {
        "verified": verified,
        "reason": "；".join(reasons),
        "adjusted_action": adjusted_action,
        "open_price": open_price,
        "open_pct": open_pct,
        "open_volume_ratio": open_volume_ratio,
        "open_fund_flow": open_super_net,
    }


class StalePlanCycle(RuntimeError):
    """调用方试图在周期 X 验证一个属于周期 Y 的计划。

    这不是"计划过期"，而是**归属冲突**：一个 cycle 8 的计划在 cycle 9 被验证
    就等于让旧周期的决策在新周期生效。fail closed —— 一条都不改。
    """

    def __init__(self, plan_id, plan_cycle_id, cycle_id):
        self.plan_id = plan_id
        self.plan_cycle_id = plan_cycle_id
        self.cycle_id = cycle_id
        super().__init__(
            f"plan #{plan_id} 属于 cycle {plan_cycle_id}，"
            f"不得在 cycle {cycle_id} 验证"
        )


def verify_all_plans(conn, plans, quotes, cycle_id=None):
    """批量验证所有调仓计划（**必须**指定周期，defense in depth）。

    即使调用方已经用 :func:`get_pending_plans` 筛过 current-cycle 计划，本函数
    自身也必须要求 ``cycle_id`` 并逐条校验 ``plan.cycle_id == cycle_id``：
    否则任何 stale plan 都能被 caller 直接注入，绕过筛选。

    UPDATE 的 WHERE 至少是 ``id=? AND cycle_id=?``，且 ``rowcount`` 必须恰为 1
    —— 0 行说明这条计划不属于本周期（或已被并发改动），此时 fail closed，
    而不是"什么都没更新但报成功"。

    Args:
        conn: 数据库连接
        plans: 计划列表
        quotes: 开盘报价 {code: quote}
        cycle_id: 请求验证的 paper cycle（必须）

    Returns:
        list: 验证结果
    """
    cycle_id = _require_cycle_id(cycle_id, operation="verify_all_plans")
    results = []
    for plan in plans:
        plan_id = plan.get("id")
        plan_cycle_id = plan.get("cycle_id")
        # 归属冲突：不接受"计划自己说自己属于谁"以外的任何推断。
        if plan_cycle_id is None or int(plan_cycle_id) != cycle_id:
            raise StalePlanCycle(plan_id, plan_cycle_id, cycle_id)

        code = plan.get("code", "")
        quote = quotes.get(code, {})

        verification = verify_opening_data(conn, plan, quote)

        # 更新计划状态
        new_status = "verified" if verification["verified"] else "cancelled"
        cursor = conn.execute(
            """UPDATE rebalance_plans SET
                status=?, open_price=?, open_pct=?, open_volume_ratio=?,
                open_fund_flow=?, open_verified=?, open_verify_reason=?,
                updated_at=?
               WHERE id=? AND cycle_id=?""",
            (new_status, verification["open_price"], verification["open_pct"],
             verification["open_volume_ratio"], verification["open_fund_flow"],
             int(verification["verified"]), verification["reason"],
             _now().isoformat(), plan_id, cycle_id)
        )
        if cursor.rowcount != 1:
            raise StalePlanCycle(plan_id, plan_cycle_id, cycle_id)

        results.append({**plan, **verification, "new_status": new_status})

    return results


# ─── 替补候选选择 ───

def find_replacement_candidates(conn, account_id, sold_code, quotes, factor_table=None):
    """为卖出的持仓找替补候选。

    优先级：
    1. deferred_capacity 高分候选
    2. pending 信号中分数最高的
    3. 当日新生成的候选

    无冷却期限制。

    Args:
        conn: 数据库连接
        account_id: 账户ID
        sold_code: 被卖出的股票代码
        quotes: 报价
        factor_table: 因子表

    Returns:
        list: 替补候选列表
    """
    params = REPLACEMENT_PARAMS
    today = _date()
    candidates = []

    # 1. 从 deferred_capacity 和 entry_frozen_waitlist 中找
    rows = conn.execute(
        """SELECT id, code, name, rank_score, t_score, payload
           FROM paper_signals
           WHERE account_id=?
             AND status IN ('deferred_capacity', 'entry_frozen_waitlist', 'pending')
             AND intended_date=?
           ORDER BY t_score DESC, rank_score DESC
           LIMIT 10""",
        (account_id, today.isoformat())
    ).fetchall()

    for row in rows:
        code = str(row[1])
        score = _num(row[4], _num(row[3], 0))
        if score >= params["min_replacement_score"]:
            payload = _load_json(row[5], {})
            candidates.append({
                "signal_id": row[0],
                "code": code,
                "name": row[2],
                "score": score,
                "source": "waiting_pool",
                "quality_score": _num(payload.get("quality_score")),
            })

    # 2. 如果等待池没有候选，从因子表中找
    if not candidates and factor_table is not None:
        try:
            # 获取已持仓和已拒绝的代码。「已持仓」必须是**当前 active cycle**
            # 的权威 lot：paper_positions 是投影，旧周期残留镜像行会让一个本已
            # 不持有的代码被错误排除在替补候选之外。
            held_codes = PPRM.current_held_codes(conn, account_id=account_id)

            rejected_rows = conn.execute(
                """SELECT code FROM paper_signals
                   WHERE account_id=? AND intended_date=? AND status IN ('rejected', 'risk_rejected')""",
                (account_id, today.isoformat())
            ).fetchall()
            rejected_codes = {str(r[0]) for r in rejected_rows}

            exclude_codes = held_codes | rejected_codes | {sold_code}

            # 从因子表中找高分候选
            if "score" in factor_table.columns:
                top_candidates = (
                    factor_table[~factor_table.index.isin(exclude_codes)]
                    .nlargest(5, "score")
                )
                for idx, row in top_candidates.iterrows():
                    candidates.append({
                        "code": str(idx),
                        "name": str(row.get("name", "")),
                        "score": _num(row.get("score"), 0),
                        "source": "factor_table",
                    })
        except Exception:
            pass

    return candidates[:params["max_rotations_per_day"]]


# ─── 状态查询 ───

def get_rebalance_status(conn, limit=10, cycle_id=None):
    """获取调仓状态（**current-cycle operational state**）。

    ``cycle_id`` 是必须的：默认接口只应展示当前 active cycle 的运营状态。
    历史跨周期的 recent history 若将来需要，应由**独立接口**提供 —— 让历史行
    混进 operational status 会让"现在待执行什么"这个问题的答案包含旧周期计划。

    ``cycle_id IS NULL`` 的 legacy 行天然被 ``cycle_id=?`` 排除（SQL 里
    ``NULL = 8`` 不为真），因此 legacy 状态在运营视图里不可见 —— 这正是
    "归属不可证明 ⇒ 不可操作"的落地方式。
    """
    cycle_id = _require_cycle_id(cycle_id, operation="get_rebalance_status")
    ensure_schema(conn)

    # 最近的扫描（本周期）
    scans = conn.execute(
        """SELECT scan_date, account_id, code, name, action, action_reason,
                  quality_score, quality_change, unrealized_pnl_pct, hold_days
           FROM rebalance_scans WHERE cycle_id=?
           ORDER BY id DESC LIMIT ?""",
        (cycle_id, limit)
    ).fetchall()

    # 最近的计划（本周期）
    plans = conn.execute(
        """SELECT id, plan_date, account_id, code, name, action, status,
                  sell_qty, sell_reason, open_verified, open_verify_reason,
                  executed_at, realized_pnl
           FROM rebalance_plans WHERE cycle_id=?
           ORDER BY id DESC LIMIT ?""",
        (cycle_id, limit)
    ).fetchall()

    # 待执行的计划（本周期）
    pending_plans = conn.execute(
        """SELECT id, plan_date, account_id, code, name, action, sell_qty, sell_reason
           FROM rebalance_plans WHERE cycle_id=? AND status IN ('planned', 'verified')
           ORDER BY plan_date DESC""",
        (cycle_id,)
    ).fetchall()

    return {
        "version": REBALANCE_VERSION,
        "cycle_id": cycle_id,
        "recent_scans": [
            {
                "date": r[0], "account": r[1], "code": r[2], "name": r[3],
                "action": r[4], "reason": r[5],
                "quality_score": r[6], "quality_change": r[7],
                "pnl_pct": r[8], "hold_days": r[9],
            }
            for r in scans
        ],
        "recent_plans": [
            {
                "id": r[0], "date": r[1], "account": r[2], "code": r[3],
                "name": r[4], "action": r[5], "status": r[6],
                "sell_qty": r[7], "reason": r[8],
                "open_verified": bool(r[9]), "open_reason": r[10],
                "executed_at": r[11], "pnl": r[12],
            }
            for r in plans
        ],
        "pending_plans": [
            {
                "id": r[0], "date": r[1], "account": r[2], "code": r[3],
                "name": r[4], "action": r[5], "sell_qty": r[6], "reason": r[7],
            }
            for r in pending_plans
        ],
        "thresholds": REBALANCE_THRESHOLDS,
        "replacement_params": REPLACEMENT_PARAMS,
    }


def get_pending_plans(conn, cycle_id=None):
    """获取**指定周期**待执行的调仓计划（``cycle_id`` 必须）。

    原实现是 ``WHERE status IN ('planned','verified')``，即**全部历史周期**的
    待执行计划。在 cycle 9 调用它会拿到 cycle 8 的计划，而 verify 会就地把它
    更新成 verified —— 一个旧周期的决策就这样在新周期"通过验证"了。

    legacy（``cycle_id IS NULL``）计划不可验证、不可执行：它们不满足
    ``cycle_id=?``，因此这里天然不返回。

    Args:
        conn: 数据库连接
        cycle_id: 请求的 paper cycle（必须）
    """
    cycle_id = _require_cycle_id(cycle_id, operation="get_pending_plans")
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT * FROM rebalance_plans
           WHERE cycle_id=? AND status IN ('planned', 'verified')
           ORDER BY plan_date DESC""",
        (cycle_id,)
    ).fetchall()
    return [dict(r) for r in rows]
