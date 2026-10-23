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
import data_paths
STATE_PATH = data_paths.data_path("entry_timing_state.json")

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
    # 2026-10-23 操作员决定：午后首次触发放开。理由是"恐慌后确认恢复、择机
    # 买入"被"午后不接受首次触发"整条挡住（晋西车轴 13:07 模型 0.756 高于
    # 门槛 0.580 仍被拒）。放开的是时段，不是约束：追高帽（1.5%）、连续确认
    # （2 次 / ≥300 秒）、Q级、双源、13:05-14:30 释位窗口与 14:30 截止全部
    # 照旧；"突破加速后不许追"仍由动量禁区（>5%）表达。
    "trend_pullback": {
        "confirm_scans": 2,
        "confirm_min_seconds": 300,
        "expiry_scans": 10,
        "max_age_minutes": 60,
        "max_chase_from_trigger": 0.015,
        "momentum_mode_from": 0.05,
        "momentum_confirm_scans": 99,   # 实际禁止动量追
        "afternoon_first_trigger": True,
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
    # 三日策略：收盘级筛选 + 早盘执行确认。``morning_window_only`` 现在只
    # 约束**上午**的首发窗口（10:30 后不再把加速股当首发突破）；2026-10-23
    # 操作员决定午后首次触发放开，因此 13:00 之后不再受这条限制。
    "reported_profit_breakout": {
        "confirm_scans": 2,
        "confirm_min_seconds": 300,
        "expiry_scans": 12,
        "max_age_minutes": 90,
        "max_chase_from_trigger": 0.015,
        "momentum_mode_from": 0.05,
        "momentum_confirm_scans": 3,
        "afternoon_first_trigger": True,
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
        # ``morning_window_only`` 只约束**上午**：窗口关闭后到 13:00 之间不再
        # 接受首发突破（"别把上午后段已加速的股票当首发"），午后是否允许由
        # ``afternoon_first_trigger`` 单独表达。两条规则不能互相覆盖——否则
        # 一个上午窗口就会顺带把整个下午挡死。
        afternoon_start = dt.time(13, 0)
        cutoff = profile.get("morning_window_only")
        if record is None and cutoff:
            cutoff_at = dt.time.fromisoformat(cutoff + ":00")
            if cutoff_at <= t < afternoon_start:
                return False, {"state": "observing",
                               "reason": f"已过首发窗口 {cutoff}，当日不把加速股当首发突破"}
        if (record is None and not profile.get("afternoon_first_trigger", True)
                and t >= afternoon_start):
            return False, {"state": "observing",
                           "reason": "午后不接受首次触发，等待次日早盘结构"}

        # ---- 失效/已入场记录的守门 ----
        _carry_retrig = 0
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
                    if profile.get("recovery_entry"):
                        _enter_recovery_watch(bucket, code, record, price, now,
                                              "确认后跌破触发价 0.5%")
                        return False, {
                            "state": "recovery_watch",
                            "reason": "确认后跌破触发价；上午证据已保留，等待恢复确认",
                        }
                    bucket.pop(code, None)
                    _save()
                    return False, {"state": "expired",
                                   "reason": "快速确认后跌破触发价，重新观察"}
                return True, {
                    "state": "confirmed", "record": record,
                    "reason": "快速观察已确认，交由三分钟正式任务完整复核",
                    "fast_path": record.get("confirmation_path") == "fast_watch",
                }
            # 跌后恢复观察：记录的是"上午有过依据、随后被跌破/普跌打掉"的候选。
            # 不能把它当全新首次触发（那会让午后恢复永远进不来），也不能无条件
            # 放行（那会变成午后追涨）——恢复资格由调用方按当日指数、相对强弱、
            # 个股事件与资金证据判定后，通过 evidence["recovery_ok"] 传入。
            if record.get("state") == "recovery_watch":
                return _evaluate_recovery_watch(
                    profile, code, record, price, pct, now,
                    evidence=evidence, fast=fast,
                )
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
                # P2（2026-09-03）：保留重触发 lineage——失效回踩后的
                # 重触发不再伪装成"首次触发"，便于日志/前端区分反复进出。
                _carry_retrig = int(record.get("retrigger_seq") or 0) + 1
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
                        _retrig_seq = int(record.get("retrigger_seq") or 0) + 1
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
                            "retrigger_seq": _retrig_seq,
                        }
                        _save()
                        return False, {
                            "state": "triggered", "record": record,
                            "reason": (f"第{_retrig_seq}次重触发：观察窗口到期但价格未失控，"
                                       "已重新开始确认"),
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
                "retrigger_seq": _carry_retrig,
            }
            _save()
            if required <= 1:
                record["confirmed_at"] = now.isoformat(timespec="seconds")
                _save()
                return True, {"state": "confirmed", "record": record}
            _retrig_label = "第%d次重触发" % _carry_retrig if _carry_retrig else "首次触发"
            return False, {"state": "triggered",
                           "reason": f"{_retrig_label}，连续确认中 1/{required}（{mode}模式）",
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
            if profile.get("recovery_entry"):
                # 启用恢复通道的策略保留上午证据，等恢复证据重新确认
                _enter_recovery_watch(bucket, code, record, price, now,
                                      "跌破触发价 0.5%，结构失效")
                return False, {
                    "state": "recovery_watch",
                    "reason": "跌破触发价，结构失效；上午证据已保留，等待恢复确认",
                }
            # 未启用恢复通道的策略保持原行为：删记录，按全新触发重新观察
            bucket.pop(code, None)
            _save()
            return False, {"state": "expired",
                           "reason": "跌破触发价，结构失效重新观察"}

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
            quote_usable = bool(evidence.get("quote_usable", cross_source))
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
                and quote_usable
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
                    "quote_usable": quote_usable,
                    "ignition_ok": ignition_ok if ignition_required else None,
                }
            elif not strong:
                record["fast_confirm_count"] = 0
                record["fast_evidence"] = {
                    "main_pct": round(main_pct, 3), "vol_ratio": round(vol_ratio, 3),
                    "cross_source_checked": cross_source, "reset": True,
                    "quote_usable": quote_usable,
                }
            fast_count = int(record.get("fast_confirm_count") or 0)
            first_ts = dt.datetime.fromisoformat(str(record["first_trigger_at"])[:19])
            elapsed = (now - first_ts).total_seconds()
            _save()
            if strong and fast_count >= 2 and elapsed >= 20:
                record["state"] = "confirmed"
                record["confirmation_path"] = "fast_watch"
                record["confirmed_at"] = now.isoformat(timespec="seconds")
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
                    f"量比 {vol_ratio:.2f}，行情={'双源核验通过' if cross_source else '单源有效' if quote_usable else '不可用'}，"
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
            record["confirmed_at"] = now.isoformat(timespec="seconds")
            _save()
            return True, {"state": "confirmed", "record": record}
        _retrig_seq = int(record.get("retrigger_seq") or 0)
        return False, {"state": "confirming",
                       "reason": (("第%d次重触发后" % _retrig_seq) if _retrig_seq else "")
                                 + f"连续确认中 {confirm_count}/{required}（{mode}模式，已等 {int(elapsed)}秒）"}


def _morning_evidence(record):
    """把一条记录里的"上午依据"压成可追溯的小包，供恢复入场判定使用。"""
    existing = record.get("morning_evidence")
    if isinstance(existing, dict) and existing:
        return dict(existing)
    return {
        "first_trigger_at": record.get("first_trigger_at"),
        "trigger_price": record.get("trigger_price"),
        "trigger_pct": record.get("trigger_pct"),
        "confirmed_at": record.get("confirmed_at"),
        "confirmation_path": record.get("confirmation_path"),
        "mode": record.get("mode"),
        "evidence": record.get("evidence") or record.get("fast_evidence"),
    }


def _enter_recovery_watch(bucket, code, record, price, now, reason):
    """失效记录转成恢复观察，**保留**上午证据而不是删掉。

    旧实现在跌破触发价时 ``bucket.pop``：午后恢复再来时 record 为 None，
    被"午后不接受首次触发"判成全新追涨候选，永远进不来。保留证据后，
    恢复资格改由调用方按当日指数/相对强弱/个股事件/资金证据判定。
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0
    low = record.get("low_since_breakdown")
    low = price if low is None else min(float(low), price)
    bucket[code] = {
        "state": "recovery_watch",
        "morning_evidence": _morning_evidence(record),
        "low_since_breakdown": low,
        "breakdown_at": now.isoformat(timespec="seconds"),
        "breakdown_reason": reason,
        "recovery_scans": int(record.get("recovery_scans") or 0),
        "recovery_last_at": record.get("recovery_last_at"),
        "retrigger_seq": int(record.get("retrigger_seq") or 0),
        "high_since_trigger": record.get("high_since_trigger"),
        "last_price": price,
        "scans_since_trigger": int(record.get("scans_since_trigger") or 0),
    }
    _save()


def _evaluate_recovery_watch(profile, code, record, price, pct, now, *, evidence, fast):
    """恢复观察：调用方证明"这次恢复成立"若干次后，才重新给出入场资格。

    只做计数与时序，不自行判断市场/个股证据——那些判断需要当日指数、新闻和
    资金，属于 ``recovery_entry_decision``。缺失证据一律按"不成立"处理。
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False, {"state": "recovery_watch", "reason": "价格无效"}
    low = record.get("low_since_breakdown")
    record["low_since_breakdown"] = price if low is None else min(float(low), price)
    record["last_price"] = price
    if not profile.get("recovery_entry"):
        _save()
        return False, {"state": "recovery_watch",
                       "reason": "该策略不启用跌后恢复入场", "record": record}
    evidence = dict(evidence or {})
    if not evidence.get("recovery_ok"):
        if record.get("recovery_scans"):
            record["recovery_scans"] = 0
        record.pop("recovery_last_at", None)
        _save()
        return False, {
            "state": "recovery_watch",
            "reason": (evidence.get("recovery_reason")
                       or "恢复条件未满足（需当日指数、相对强弱、资金与收复证据）"),
            "record": record,
        }
    # 30 秒快速通道只观察、不累计恢复确认次数：否则同一分钟内两次采样
    # 就能凑满"多次确认"。
    if not fast:
        record["recovery_scans"] = int(record.get("recovery_scans") or 0) + 1
        record["recovery_last_at"] = now.isoformat(timespec="seconds")
    count = int(record.get("recovery_scans") or 0)
    required = max(1, int(profile.get("recovery_confirm_scans", 2)))
    if count < required:
        _save()
        return False, {"state": "recovery_watch",
                       "reason": f"跌后恢复确认中 {count}/{required}", "record": record}
    seq = int(record.get("retrigger_seq") or 0) + 1
    record.update({
        "state": "confirmed",
        "confirmed_at": now.isoformat(timespec="seconds"),
        "confirmation_path": "selloff_recovery",
        "recovery_confirmed": True,
        "trigger_price": price,
        "trigger_pct": pct,
        "retrigger_seq": seq,
    })
    _save()
    return True, {
        "state": "confirmed", "record": record, "recovery": True,
        "reason": f"第{seq}次重触发：普跌后恢复确认（{count} 次观察）",
    }


def recovery_watch_evidence(strategy_id, code):
    """读取某标的的跌后恢复观察记录（只读、不读时钟）；没有则 ``None``。"""
    try:
        with _lock:
            state = _load()
            record = state.get(strategy_id, {}).get(code)
            if not record or record.get("state") != "recovery_watch":
                return None
            return {
                "morning_evidence": _morning_evidence(record),
                "low_since_breakdown": record.get("low_since_breakdown"),
                "breakdown_at": record.get("breakdown_at"),
                "breakdown_reason": record.get("breakdown_reason"),
                "recovery_scans": int(record.get("recovery_scans") or 0),
                "retrigger_seq": int(record.get("retrigger_seq") or 0),
            }
    except Exception:
        return None


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
