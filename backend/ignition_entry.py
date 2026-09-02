# -*- coding: utf-8 -*-
"""主力策略"点火入场通道"判定（ignition-v1）。

背景（2026-08-31 复核）：主力策略的通用追高上限在开盘涨幅 +3.5% 后直接
否决，与主力模型 +8.8% 的执行区间互相矛盾，大量强股反复被拒。本模块不是
简单放宽追涨，而是一条独立确认通道：涨幅进入 +3.5%~+7.5% 点火区间时，
必须同时满足分钟量能、VWAP 承接、低点结构、主力资金增量与微观盘口八项
条件；任何一项数据缺失都按失败处理（fail-closed），绝不凭单一证据放行。

确认通过后仍由既有链路（入场时机状态机、双源、Q级、资金、席位）完成审批，
本模块只回答"点火结构是否成立"。
"""
import datetime as dt

try:
    import alt_data as AD
except ImportError:  # pragma: no cover
    AD = None

VERSION = "ignition-v1"

# 点火区间（对昨收的实时涨幅）。接近涨停的排除由调用方按涨停价缓冲处理。
IGNITION_LOW_PCT = 3.5
IGNITION_HIGH_PCT = 7.5

# 量能结构参数
VOLUME_BASE_BARS = 5        # 前 5 根分钟量作为基准
VOLUME_SURGE_BARS = 2       # 最近 1-2 根作为点火量
VOLUME_SURGE_RATIO = 2.0    # 点火量 ≥ 基准中位数 × 2
VOLUME_STABILITY_MAX_RATIO = 4.0  # 基准最大/最小 ≤ 4 视为"相对平稳"

# 低点结构参数
LOW_LOOKBACK_BARS = 30      # 在最近 30 根分钟线里找局部低点
MIN_MINUTE_BARS = 12        # 至少要有这么多根分钟线才谈得上结构

# 资金/盘口阈值
MIN_MAIN_DELTA_5M = 0.0             # 5 分钟主力资金增量 > 0
MIN_POSITIVE_PERSISTENCE_10M = 0.67  # 10 分钟正流入持续率 ≥ 67%
MIN_ACTIVE_IMBALANCE = -0.10         # 逐笔主动买卖不偏空
MIN_DEPTH_IMBALANCE = -0.20          # 五档卖压不过强


def _local_minima(prices):
    """返回 (index, value) 列表：严格低于相邻两根的局部低点。"""
    lows = []
    for i in range(1, len(prices) - 1):
        if prices[i] < prices[i - 1] and prices[i] <= prices[i + 1]:
            lows.append((i, prices[i]))
    return lows


def _check_volume_structure(series):
    """前 5 根分钟量平稳 + 最近 1-2 根点火放量。返回 (ok, detail)。"""
    if series is None or len(series) < VOLUME_BASE_BARS + VOLUME_SURGE_BARS:
        return False, "分钟量数据不足"
    volumes = [float(row.get("volume") or 0) for row in series]
    base = volumes[-(VOLUME_BASE_BARS + VOLUME_SURGE_BARS):-VOLUME_SURGE_BARS]
    surge = volumes[-VOLUME_SURGE_BARS:]
    positive_base = [v for v in base if v > 0]
    if not positive_base:
        return False, "基准窗口无量能"
    base_median = sorted(positive_base)[len(positive_base) // 2]
    if base_median <= 0:
        return False, "基准中位量为 0"
    vmax, vmin = max(base), min(base)
    if vmin <= 0 or vmax / vmin > VOLUME_STABILITY_MAX_RATIO:
        return False, f"前{VOLUME_BASE_BARS}根分钟量不平稳（max/min={vmax/max(vmin,1e-9):.1f}）"
    peak = max(surge)
    if peak < base_median * VOLUME_SURGE_RATIO:
        return False, (
            f"点火量不足：最近{VOLUME_SURGE_BARS}根峰值 {peak:.0f} "
            f"< 基准中位 {base_median:.0f}×{VOLUME_SURGE_RATIO:.0f}"
        )
    return True, f"基准中位 {base_median:.0f}，点火峰值 {peak:.0f}（×{peak/base_median:.1f}）"


def _check_lows_rising(series):
    """最近两个局部低点抬高。分钟线只有单点价格，用它做低点代理。"""
    if series is None or len(series) < MIN_MINUTE_BARS:
        return False, "分钟结构数据不足"
    prices = [float(row.get("price") or 0) for row in series][-LOW_LOOKBACK_BARS:]
    lows = _local_minima(prices)
    if len(lows) < 2:
        return False, f"回看{len(prices)}根只有{len(lows)}个局部低点，结构不足"
    (_, first_low), (_, second_low) = lows[-2], lows[-1]
    # 允许 0.1% 以内的噪音
    if second_low < first_low * 0.999:
        return False, f"低点未抬高：{first_low:.2f} → {second_low:.2f}"
    return True, f"低点抬高 {first_low:.2f} → {second_low:.2f}"


def evaluate_ignition(code, *, pct=None, limit_pct=None,
                      micro=None, flow=None, minute_series=None, now=None):
    """点火八项条件判定。返回 dict(passed, reasons, evidence, version)。

    fail-closed：minute_series/micro/flow 任一缺失，对应条件记为失败，
    整体不通过——点火是放宽追高的交换条件，证据不齐绝不开闸。
    """
    now = now or dt.datetime.now()
    pct = pct if pct is not None else None
    reasons = []
    evidence = {"version": VERSION, "checked_at": now.isoformat(timespec="seconds")}

    # 区间门槛
    if pct is None:
        return {"passed": False, "reasons": ["缺少实时涨幅"], "evidence": evidence, "version": VERSION}
    if not (IGNITION_LOW_PCT <= pct <= IGNITION_HIGH_PCT):
        return {
            "passed": False,
            "reasons": [f"涨幅 {pct:+.2f}% 不在点火区间 +{IGNITION_LOW_PCT}%~+{IGNITION_HIGH_PCT}%"],
            "evidence": evidence, "version": VERSION,
        }
    if limit_pct is not None and pct >= limit_pct - 1.0:
        return {
            "passed": False,
            "reasons": [f"涨幅 {pct:+.2f}% 已接近涨停（≥{limit_pct - 1.0:.1f}%），不模拟排队"],
            "evidence": evidence, "version": VERSION,
        }
    evidence["pct"] = round(float(pct), 2)

    if AD is None:
        return {"passed": False, "reasons": ["alt_data 模块不可用"], "evidence": evidence, "version": VERSION}

    # 1-2. 分钟量能结构
    if minute_series is None:
        minute_series = AD.minute_trend_series(code)
    vol_ok, vol_detail = _check_volume_structure(minute_series)
    evidence["volume_structure"] = {"ok": vol_ok, "detail": vol_detail}
    if not vol_ok:
        reasons.append(f"分钟量能结构不满足：{vol_detail}")

    # 3. 价格站上分钟 VWAP
    vwap_dev = (micro or {}).get("vwap_deviation_pct") if micro else None
    if vwap_dev is None and micro is None:
        micro = AD.market_microstructure(code)
        vwap_dev = micro.get("vwap_deviation_pct")
    if vwap_dev is None:
        vwap_ok = False
        vwap_detail = "VWAP 数据缺失"
    else:
        vwap_ok = float(vwap_dev) >= 0.0
        vwap_detail = f"现价/分钟VWAP {float(vwap_dev):+.2f}%"
    evidence["above_vwap"] = {"ok": vwap_ok, "detail": vwap_detail}
    if not vwap_ok:
        reasons.append(f"未站上分钟VWAP：{vwap_detail}")

    # 4. 低点抬高
    low_ok, low_detail = _check_lows_rising(minute_series)
    evidence["lows_rising"] = {"ok": low_ok, "detail": low_detail}
    if not low_ok:
        reasons.append(f"低点结构不满足：{low_detail}")

    # 5-6. 主力资金增量与持续率
    if flow is None:
        try:
            flow = AD.fund_flow_trajectory(code)
        except Exception:
            flow = None
    flow = dict(flow or {})
    delta5 = flow.get("main_delta_5m")
    if delta5 is None:
        flow_ok = False
        flow_detail = "资金轨迹数据缺失"
    else:
        flow_ok = float(delta5) > MIN_MAIN_DELTA_5M
        flow_detail = f"5分钟主力增量 {float(delta5)/10000:+.1f}万"
    if not flow_ok:
        reasons.append(f"主力资金增量不满足：{flow_detail}")
    persistence = flow.get("positive_persistence_10m")
    if persistence is None:
        persist_ok = False
        persist_detail = "10分钟持续率数据缺失"
    else:
        persist_ok = float(persistence) >= MIN_POSITIVE_PERSISTENCE_10M
        persist_detail = f"10分钟正流入持续率 {float(persistence)*100:.0f}%"
    if not persist_ok:
        reasons.append(f"资金持续率不满足：{persist_detail}")
    evidence["flow"] = {"ok": flow_ok and persist_ok, "detail": f"{flow_detail}；{persist_detail}"}

    # 7-8. 微观盘口
    if micro is None:
        micro = AD.market_microstructure(code)
    active = micro.get("active_buy_sell_imbalance")
    if active is None:
        active_ok = False
        active_detail = "逐笔数据缺失"
    else:
        active_ok = float(active) >= MIN_ACTIVE_IMBALANCE
        active_detail = f"逐笔主动买卖 {float(active):+.2f}"
    if not active_ok:
        reasons.append(f"逐笔偏空：{active_detail}")
    depth = micro.get("depth_imbalance")
    if depth is None:
        depth_ok = False
        depth_detail = "五档数据缺失"
    else:
        depth_ok = float(depth) >= MIN_DEPTH_IMBALANCE
        depth_detail = f"五档失衡 {float(depth):+.2f}"
    if not depth_ok:
        reasons.append(f"五档卖压过强：{depth_detail}")
    evidence["micro"] = {"ok": active_ok and depth_ok, "detail": f"{active_detail}；{depth_detail}"}

    evidence["passed"] = not reasons
    return {
        "passed": not reasons, "reasons": reasons,
        "evidence": evidence, "version": VERSION,
    }
