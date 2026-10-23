# -*- coding: utf-8 -*-
"""入场评分模型：均线结构、资金量能、微结构、DSL 声明式评估与 Q 分级确认。

本模块只做纯计算：输入是已冻结的候选、实时报价、已收盘日线、盘口微结构和
市场灯色，输出是入场评估字典。不打开数据库、不联网、不写账本、不读时钟。

``_spec_for``（账户声明解析）属于交易主编排：注册表型用户策略需要运行时上下文，
因此这里只保留同名解析口，由 ``paper_trading`` 在导入后绑定自己的实现。这样
评分模型既能独立导入，也不会形成第二份账户声明真相。
"""
from __future__ import annotations

import os

import pandas as pd

import paper_account_specs as ACS
from paper_candidate_assembly import (
    _sector_entry_threshold_context,
    _sector_overheat_guard,
    _trend_entry_threshold_context,
)
from paper_numeric import num as _num
from paper_trading_rules import limit_pct as _limit_pct
from strategy_policies import MAIN_FORCE_STRATEGY_ID, NEW_STRATEGY_ID

try:
    import ignition_entry as IGN
except ImportError:  # 点火通道缺失时主力候选退回常规承接，不中断评估。
    IGN = None

MAIN_FORCE_STRATEGY_VERSION = ACS.MAIN_FORCE_STRATEGY_VERSION
RISK_PROFILES = ACS.RISK_PROFILES
# 点火买入总开关：影子对比期默认关闭，置 PAPER_IGNITION_BUY=1 正式放开。
IGNITION_BUY_ENABLED = os.environ.get("PAPER_IGNITION_BUY", "").strip().lower() in {
    "1", "true", "yes",
}

_SPEC_RESOLVER = None


def bind_spec_resolver(resolver):
    """由交易主编排注入账户声明解析口（含注册表运行时账户）。"""
    global _SPEC_RESOLVER
    _SPEC_RESOLVER = resolver


def _spec_for(account_id):
    """解析账户 spec：优先使用主编排绑定的唯一解析口。"""
    if _SPEC_RESOLVER is None:
        # 独立导入（例如离线评分、测试）时退回声明层与保守回退，不猜测注册表。
        return ACS.builtin_spec(account_id) or ACS.fallback_spec()
    return _SPEC_RESOLVER(account_id)


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

def _dsl_entry_assessment(account_id, spec, pick, quote, decision, threshold_delta=0.0, market=None):
    """声明式（DSL）策略的入场复核（PR-35）。

    选股层已经用编译后的 DSL 在完整历史因子上做了布尔判定；这里不复述
    规则，只复核执行域事实：通用六维护栏的硬否决、实时量价与资金方向。
    全部字段来自注入的实时快照，缺失按中性/保守处理，绝不虚构证据。
    """
    pct = _num(quote.get("pct"), -999)
    vol_ratio = _num(quote.get("vol_ratio"))
    main_pct = _num(quote.get("main_pct"))
    checks = []
    blockers = [
        reason for reason in (decision.get("hard_vetoes") or [])
        if reason != "海外风险红灯"
    ]
    overheat = {"level": "normal", "score": 0.0, "pullback_confirmed": False}

    def add(name, value, weight, detail):
        checks.append({
            "name": name, "score": round(_clip01(value), 3),
            "weight": weight, "detail": detail,
        })

    add("声明式规则通过", 1.0, 0.55, "编译 DSL 规则在最新完整收盘因子上成立")
    add("实时涨幅区间", 1.0 if -2.0 <= pct <= _num(spec.get("entry_pct_high"), 7.0) else 0.3,
        0.20, f"实时涨幅 {pct:+.2f}%")
    if vol_ratio is None:
        add("实时量能", 1.0, 0.15, "量比缺失，按中性处理")
    else:
        add("实时量能", min(max(vol_ratio, 0.0) / 1.2, 1.5), 0.15, f"量比 {vol_ratio:.2f}")
    if main_pct is None:
        add("资金方向", 1.0, 0.10, "主力净流入缺失，按中性处理")
    else:
        add("资金方向", 1.0 if main_pct >= 0 else 0.2, 0.10, f"主力净流入占比 {main_pct:+.2f}%")
    if pct <= -4.0:
        blockers.append(f"实时跌幅 {pct:+.2f}% 触发声明式入场否决")
    weight_total = sum(item["weight"] for item in checks) or 1.0
    score = sum(item["score"] * item["weight"] for item in checks) / weight_total
    threshold = max(0.45, min(0.85, 0.55 + threshold_delta))
    passed = not blockers and score >= threshold
    reasons = list(blockers)
    if score < threshold:
        reasons.append(f"{spec['entry_model_name']}评分 {score:.2f} 未达 {threshold:.2f}")
    return {
        "name": spec["entry_model_name"],
        "risk_profile": spec["risk_profile"],
        "risk_profile_name": RISK_PROFILES[spec["risk_profile"]]["name"],
        "score": round(score, 3),
        "threshold": round(threshold, 3),
        "threshold_context": {"version": "dsl-entry-v1",
                              "reason": "声明式规则门在选股层判定；执行层只复核量价/资金/硬否决"},
        "passed": passed,
        "checks": checks,
        "blockers": blockers,
        "reasons": reasons,
        "overheat_guard": overheat,
        "microstructure": None,
        "timing_mode": "声明式规则确认",
        "position_scale": 1.0,
        "entry_tranche_scale": 1.0,
        "execution_mode": "声明式 DSL 入场",
    }

def _strategy_entry_assessment(account, pick, quote, kline, decision, threshold_delta=0.0, market=None):
    """按各模拟盘账户各自的独立模型复核入场。"""
    account_id = account["id"]
    spec = _spec_for(account_id)
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

    if spec.get("selection_mode") == "dsl":
        # PR-35：声明式策略走专属入场复核，不再落入板块热度通用分支。
        return _dsl_entry_assessment(
            account_id, spec, pick, quote, decision,
            threshold_delta=threshold_delta, market=market,
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
