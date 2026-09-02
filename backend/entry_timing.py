# -*- coding: utf-8 -*-
"""入场时机状态机（P3 重构核心）。

解决"候选、确认、下单三层各自重新判断"导致的追晚/错过/仓位无表达力：
每个 (策略, 代码) 维护一条状态轨迹——

    观察 → 首次触发 → 连续确认 → 入场 / 失效

状态落盘 data_cache/entry_timing_state.json，跨日自动清空、进程重启不丢。
各策略差异只体现在 PROFILES 参数（确认轮数、触发区间、失效条件、
触发价追涨上限、时段窗口）；追高上限、Q级、资金、双源等既有门禁仍在
paper_trading 原链路，状态机只回答"本轮允许不允许进入执行闸门"。

铁律：内部任何异常一律 fail-open 放行——状态机故障不能冻结交易。
"""
import datetime as dt
import json
import os
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE, "data_cache", "entry_timing_state.json")

_lock = threading.Lock()
_state = None

PROFILES = {
    # 短线日内做T：正常带 +0.2%~+3.5%；>3.5% 进入动量确认模式
    #（叠加既有追高门禁的 Q1/主力/量比/双源要求，共 3 轮确认）。
    "tq_breakout": {
        "confirm_scans": 2,
        "confirm_min_seconds": 180,
        "expiry_scans": 8,
        "max_age_minutes": 45,
        "max_chase_from_trigger": 0.02,
        "momentum_mode_from": 0.035,
        "momentum_confirm_scans": 3,
        "afternoon_first_trigger": True,
    },
    # 趋势波段优选：只在开盘价附近/回踩位入场；>5% 视为动量禁区。
    "trend_pullback": {
        "confirm_scans": 2,
        "confirm_min_seconds": 300,
        "expiry_scans": 10,
        "max_age_minutes": 60,
        "max_chase_from_trigger": 0.015,
        "momentum_mode_from": 0.05,
        "momentum_confirm_scans": 99,   # 实际禁止动量追
        "afternoon_first_trigger": False,
    },
    # 板块轮动先锋：板块启动两次确认 + 触发后真实回踩结构
    #（冲高≥1% → 回踩不破触发价 → 才允许入场），不再把"离MA20不远"当回踩。
    "sector_rotation": {
        "confirm_scans": 2,
        "confirm_min_seconds": 240,
        "expiry_scans": 10,
        "max_age_minutes": 60,
        "max_chase_from_trigger": 0.02,
        "momentum_mode_from": 0.045,
        "momentum_confirm_scans": 3,
        "afternoon_first_trigger": True,
        "pullback_structure_required": True,
    },
    # 三日策略：收盘级筛选 + 早盘执行确认；10:30 后不接受首次触发，
    # 禁止盘中后段把已加速股票再当首发突破。
    "reported_profit_breakout": {
        "confirm_scans": 2,
        "confirm_min_seconds": 300,
        "expiry_scans": 12,
        "max_age_minutes": 90,
        "max_chase_from_trigger": 0.015,
        "momentum_mode_from": 0.05,
        "momentum_confirm_scans": 3,
        "afternoon_first_trigger": False,
        "morning_window_only": "10:30",
    },
    # 超强主力股：常规承接 2 次确认（比普通波段快）；涨幅进入点火区间
    # (+3.5%~+7.5%) 后走点火分支——30 秒快速通道 2 次严格确认，且必须
    # 携带 ignition_ok=True（八项点火条件已由 ignition_entry 预先判定），
    # 缺证据一律不放行。确认后仍由下一轮三分钟任务完成资金/席位/风控。
    "main_force_top10": {
        "confirm_scans": 2,
        "confirm_min_seconds": 60,
        "expiry_scans": 10,
        "max_age_minutes": 60,
        "max_chase_from_trigger": 0.02,
        "momentum_mode_from": None,
        "afternoon_first_trigger": True,
        "ignition_zone": (3.5, 7.5),
        "ignition_confirm_scans": 2,
        "ignition_confirm_min_seconds": 30,
    },
}


def _load():
    global _state
    if _state is None:
        try:
            with open(STATE_PATH, encoding="utf-8") as handle:
                _state = json.load(handle) or {}
        except (OSError, ValueError):
            _state = {}
    today = dt.date.today().isoformat()
    if _state.get("_day") != today:
        _state = {"_day": today}
    return _state


def _save():
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_state, handle, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def evaluate(strategy_id, code, price, pct, now=None, *, fast=False, evidence=None):
    """状态机主入口：返回 (allowed, info)。

    allowed=True 仅表示"本轮可进入执行闸门"；追高上限/Q级/资金等门禁
    照常在调用方生效。info["state"] ∈ triggered/confirming/confirmed/
    expired/observing/entered。内部异常 fail-open 放行。
    """
    try:
        return _evaluate(
            strategy_id, code, price, pct, now=now,
            fast=fast, evidence=evidence,
        )
    except Exception as exc:  # 状态机故障绝不冻结交易
        return True, {"state": "error", "reason": f"{type(exc).__name__}: {exc}"}


def _evaluate(strategy_id, code, price, pct, now=None, *, fast=False, evidence=None):
    now = now or dt.datetime.now()
    profile = PROFILES.get(strategy_id)
    if profile is None:
        return True, {"state": "no_profile"}
    try:
        price = float(price)
        pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        return True, {"state": "bad_input"}
    if price <= 0:
        return True, {"state": "bad_input"}

    with _lock:
        state = _load()
        bucket = state.setdefault(strategy_id, {})
        record = bucket.get(code)
        t = now.time()

        # ---- 时段窗口 ----
        cutoff = profile.get("morning_window_only")
        if record is None and cutoff and t >= dt.time.fromisoformat(cutoff + ":00"):
            return False, {"state": "observing",
                           "reason": f"已过首发窗口 {cutoff}，当日不把加速股当首发突破"}
        if (record is None and not profile.get("afternoon_first_trigger", True)
                and t >= dt.time(13, 0)):
            return False, {"state": "observing",
                           "reason": "午后不接受首次触发，等待次日早盘结构"}

        # ---- 失效/已入场记录的守门 ----
        if record:
            # 已入场标记：同日不再对同一标的重复触发（卖出后自然过期重置）
            if record.get("state") == "entered":
                return False, {"state": "entered",
                               "reason": "状态机已入场，等待卖出后重置"}
            # 30 秒快速通道只负责确认，不负责下单。确认结果保留到下一轮
            # 三分钟正式任务，由正式任务重新执行行情、风控、容量和资金门禁。
            if record.get("state") == "confirmed":
                trigger_price = float(record.get("trigger_price") or price)
                max_chase = float(profile.get("max_chase_from_trigger", 0.02))
                if price > trigger_price * (1 + max_chase):
                    record["state"] = "expired"
                    record["high_since_trigger"] = max(
                        float(record.get("high_since_trigger") or 0), price)
                    _save()
                    return False, {
                        "state": "expired",
                        "reason": f"快速确认后较触发价已涨超 {max_chase*100:.1f}%，等待回踩",
                    }
                if price < trigger_price * 0.995:
                    bucket.pop(code, None)
                    _save()
                    return False, {"state": "expired", "reason": "快速确认后跌破触发价，重新观察"}
                return True, {
                    "state": "confirmed", "record": record,
                    "reason": "快速观察已确认，交由三分钟正式任务完整复核",
                    "fast_path": record.get("confirmation_path") == "fast_watch",
                }
            # 过期失效记录：必须回踩到失效高点的 -2% 以下才重新触发。
            # 旧实现直接 pop——持续上涨的股票每轮以更高锚点重触，
            # 追涨帽自我棘轮形同虚设（2026-08 精读审计确认）。
            if record.get("state") == "expired":
                expired_high = float(record.get("high_since_trigger") or 0)
                reclaim = expired_high * 0.98 if expired_high > 0 else 0
                record["scans_since_trigger"] = int(record.get("scans_since_trigger") or 0) + 1
                record["high_since_trigger"] = max(expired_high, price)
                if price > reclaim:
                    _save()
                    return False, {"state": "expired",
                                   "reason": f"等待回踩：需回落至 {reclaim:.2f}（失效高点-2%）以下重新触发"}
                bucket.pop(code, None)  # 已回踩，清记录走全新触发
                record = None
            else:
                age_min = (now - dt.datetime.fromisoformat(
                    str(record.get("first_trigger_at"))[:19])).total_seconds() / 60
                # 超时必须只按真实经过时间判断。快速通道每 30 秒观察一次，
                # 若把它混入三分钟扫描计数，原本 45~60 分钟的观察窗会在
                # 数分钟内被耗尽；进程重启/调度抖动也会让同一策略出现不同
                # 的失效时长。``scans_since_trigger`` 仅保留作诊断。
                if age_min > float(profile.get("max_age_minutes", 45)):
                    trigger_price = float(record.get("trigger_price") or price)
                    max_chase = float(profile.get("max_chase_from_trigger", 0.02))
                    # 超时本身不等于走势失效。只要价格仍在原始追涨帽内且
                    # 没有跌破触发结构，重置观察窗口并重新确认；真正加速的
                    # 标的仍需等回踩，不能借超时绕开追高约束。
                    if trigger_price * 0.995 <= price <= trigger_price * (1 + max_chase):
                        bucket[code] = record = {
                            "state": "triggered",
                            "first_trigger_at": now.isoformat(timespec="seconds"),
                            "trigger_price": price,
                            "trigger_pct": pct,
                            "confirm_count": 1,
                            "scans_since_trigger": 1,
                            "high_since_trigger": price,
                            "last_price": price,
                            "mode": record.get("mode") or "normal",
                            "retriggered_after_timeout": True,
                        }
                        _save()
                        return False, {
                            "state": "triggered", "record": record,
                            "reason": "观察窗口到期但价格未失控，已重新触发并开始确认",
                        }
                    record["state"] = "expired"
                    record["high_since_trigger"] = max(
                        float(record.get("high_since_trigger") or 0), price)
                    _save()
                    return False, {"state": "expired",
                                   "reason": "观察超时失效，等待回踩后重新触发"}

        # ---- 动量模式：涨幅越大确认要求越高 ----
        momentum_from = profile.get("momentum_mode_from")
        required = int(profile["confirm_scans"])
        mode = "normal"
        if momentum_from is not None and pct is not None and pct > momentum_from * 100:
            required = max(required, int(profile.get("momentum_confirm_scans", required)))
            mode = "momentum"
        # ---- 点火模式：涨幅进入点火区间的主力候选走独立确认分支 ----
        ignition_zone = profile.get("ignition_zone")
        if ignition_zone and pct is not None and ignition_zone[0] <= pct <= ignition_zone[1]:
            required = max(required, int(profile.get("ignition_confirm_scans", 2)))
            mode = "ignition"

        if record is None:
            # ---- 首次触发 ----
            bucket[code] = record = {
                "state": "triggered",
                "first_trigger_at": now.isoformat(timespec="seconds"),
                "trigger_price": price,
                "trigger_pct": pct,
                "confirm_count": 1,
                "scans_since_trigger": 1,
                "high_since_trigger": price,
                "last_price": price,
                "mode": mode,
            }
            _save()
            if required <= 1:
                return True, {"state": "confirmed", "record": record}
            return False, {"state": "triggered",
                           "reason": f"首次触发，连续确认中 1/{required}（{mode}模式）",
                           "record": record}

        # ---- 后续扫描：更新轨迹 ----
        # 快速通道有独立的 fast_confirm_count；不可消耗常规三分钟观察
        # 计数，否则一分钟两次检查会错误制造“观察超时”。
        if not fast:
            record["scans_since_trigger"] = int(record.get("scans_since_trigger") or 0) + 1
        high = max(float(record.get("high_since_trigger") or price), price)
        record["high_since_trigger"] = high
        record["last_price"] = price
        if mode in ("momentum", "ignition"):
            record["mode"] = mode

        trigger_price = float(record["trigger_price"])

        # ---- 触发价追涨上限：看到之后涨太多，标记失效等回踩 ----
        # P3 精读修复：旧实现直接 pop——持续上涨的股票下一轮以更高价
        # 重触，追涨帽自我棘轮。现在保留 expired 记录+高点水位，必须
        # 回踩到高点-2% 以下才重新触发。
        max_chase = float(profile.get("max_chase_from_trigger", 0.02))
        if price > trigger_price * (1 + max_chase):
            record["state"] = "expired"
            record["high_since_trigger"] = high
            _save()
            reclaim = high * 0.98
            return False, {"state": "expired",
                           "reason": f"较触发价已涨超 {max_chase*100:.1f}%，"
                                     f"失效等待回踩至 {reclaim:.2f} 以下"}

        # ---- 跌破触发价：结构失效 ----
        if price < trigger_price * 0.995:
            bucket.pop(code, None)
            _save()
            return False, {"state": "expired", "reason": "跌破触发价，结构失效重新观察"}

        # ---- 板块轮动：真实回踩结构 ----
        if profile.get("pullback_structure_required") and record.get("pullback_done") is not True:
            ran_up = high / trigger_price - 1
            retraced = price <= high * 0.99
            held = price >= trigger_price
            if ran_up >= 0.01 and retraced and held:
                record["pullback_done"] = True
            elif ran_up >= 0.01:
                _save()
                return False, {"state": "awaiting_pullback",
                               "reason": "等待真实回踩：冲高≥1%后需回踩且守住触发价"}

        # ---- 连续确认 ----
        # 独立快速观察通道只处理已经被常规扫描首次触发的候选。它不会降低
        # 触发价追涨帽、回踩结构或调用方的双源/Q级/仓位/资金门禁；这里只
        # 在两次相隔至少 15 秒的严格量价确认后，缩短等待下一轮全市场扫描
        # 的时间。证据转弱会清零快速计数，不能靠历史强势累计放行。
        if fast:
            evidence = dict(evidence or {})
            main_pct = float(evidence.get("main_pct") or 0.0)
            vol_ratio = float(evidence.get("vol_ratio") or 0.0)
            active = evidence.get("active_buy_sell_imbalance")
            depth = evidence.get("depth_imbalance")
            cross_source = bool(evidence.get("cross_source_checked"))
            mode_now = str(record.get("mode") or mode)
            # 点火模式下快速确认间隔放宽到 30 秒（ignition_confirm_min_seconds），
            # 且必须携带 ignition_ok=True——八项点火条件已在调用方预先判定，
            # 缺证据一律视为不成立，绝不由状态机单方面放行。
            min_fast_gap = 15
            ignition_required = mode_now == "ignition"
            ignition_ok = bool(evidence.get("ignition_ok"))
            ignition_detail = str(evidence.get("ignition_detail") or "")
            if ignition_required:
                min_fast_gap = int(profile.get("ignition_confirm_min_seconds", 30))
            active_value = float(active) if active is not None else None
            depth_value = float(depth) if depth is not None else None
            # 逐笔最近样本容易被一段主动卖单短暂扭曲。轻度偏卖只有在
            # 五档买盘明确承接且大尺度主力占比足够高时才允许；逐笔和
            # 盘口同时偏空仍直接清零，避免把派发误判成洗盘。
            micro_ok = (
                active_value is None
                or active_value >= -0.10
                or (
                    active_value >= -0.30
                    and depth_value is not None and depth_value >= 0.15
                    and main_pct >= 6.0
                )
            ) and (depth_value is None or depth_value >= -0.20)
            strong = (
                (not ignition_required or ignition_ok)
                and cross_source
                and main_pct >= (3.0 if mode_now in ("momentum", "ignition") else 2.0)
                and vol_ratio >= (1.5 if mode_now in ("momentum", "ignition") else 1.2)
                and price >= trigger_price
                and micro_ok
            )
            last_fast_raw = record.get("last_fast_confirm_at")
            try:
                last_fast = dt.datetime.fromisoformat(str(last_fast_raw)[:19]) if last_fast_raw else None
            except ValueError:
                last_fast = None
            separated = last_fast is None or (now - last_fast).total_seconds() >= min_fast_gap
            if strong and separated:
                record["fast_confirm_count"] = int(record.get("fast_confirm_count") or 0) + 1
                record["last_fast_confirm_at"] = now.isoformat(timespec="seconds")
                record["fast_evidence"] = {
                    "main_pct": round(main_pct, 3), "vol_ratio": round(vol_ratio, 3),
                    "cross_source_checked": cross_source,
                    "ignition_ok": ignition_ok if ignition_required else None,
                }
            elif not strong:
                record["fast_confirm_count"] = 0
                record["fast_evidence"] = {
                    "main_pct": round(main_pct, 3), "vol_ratio": round(vol_ratio, 3),
                    "cross_source_checked": cross_source, "reset": True,
                }
            fast_count = int(record.get("fast_confirm_count") or 0)
            first_ts = dt.datetime.fromisoformat(str(record["first_trigger_at"])[:19])
            elapsed = (now - first_ts).total_seconds()
            _save()
            if strong and fast_count >= 2 and elapsed >= 20:
                record["state"] = "confirmed"
                record["confirmation_path"] = "fast_watch"
                _save()
                return True, {
                    "state": "confirmed", "record": record,
                    "reason": "快速观察通道连续两次严格量价确认通过",
                    "fast_path": True,
                }
            return False, {
                "state": "fast_confirming", "record": record,
                "reason": (
                    f"快速观察确认 {fast_count}/2"
                    + ("（点火模式）" if ignition_required else "")
                    + f"：主力 {main_pct:+.2f}%，"
                    f"量比 {vol_ratio:.2f}，双源={'通过' if cross_source else '未通过'}，"
                    f"逐笔={active_value if active_value is not None else '缺失'}，"
                    f"五档={depth_value if depth_value is not None else '缺失'}"
                    + ("" if not ignition_required else (
                        f"，点火={'成立' if ignition_ok else '未成立' + (('：' + ignition_detail) if ignition_detail else '')}"
                    ))
                ),
                "fast_path": True,
            }

        confirm_count = int(record.get("confirm_count") or 0) + 1
        record["confirm_count"] = confirm_count
        first_ts = dt.datetime.fromisoformat(str(record["first_trigger_at"])[:19])
        elapsed = (now - first_ts).total_seconds()
        _save()
        if confirm_count >= required and elapsed >= float(profile["confirm_min_seconds"]):
            record["state"] = "confirmed"
            _save()
            return True, {"state": "confirmed", "record": record}
        return False, {"state": "confirming",
                       "reason": f"连续确认中 {confirm_count}/{required}（{mode}模式，已等 {int(elapsed)}秒）"}


def mark_entered(strategy_id, code, price=None):
    """成交后标记 entered：同日不再对同一标的重复触发。"""
    try:
        with _lock:
            state = _load()
            record = state.get(strategy_id, {}).get(code)
            if record is not None:
                record["state"] = "entered"
                if price:
                    record["entry_price"] = float(price)
                _save()
    except Exception:
        pass


def reset(strategy_id=None, code=None):
    global _state
    try:
        with _lock:
            state = _load()
            if strategy_id is None:
                # 必须原地清空：state 是模块级缓存，重绑局部变量会让
                # 后续 _load() 命中旧内存数据（测试间状态污染的根源）。
                state.clear()
                state["_day"] = dt.date.today().isoformat()
            elif code is None:
                state.pop(strategy_id, None)
            else:
                state.get(strategy_id, {}).pop(code, None)
            _save()
    except Exception:
        pass
