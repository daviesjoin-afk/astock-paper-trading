# -*- coding: utf-8 -*-
"""候选层的板块/题材车道计算（owner）。

候选池按业务簇归位：这里拥有"板块热度 → 候选注入"的全部计算，包括

- 板块热度图（``_sector_heat_map``）与入场阈值上下文；
- 板块异动直通车道（``_sector_surge_lane_candidates``）；
- 概念反查领涨股与概念扩张车道；
- 同花顺热门车道；
- 科创板板块脉冲（只作上下文加分，不作候选）。

这些计算**不写账本、不下单**，只把候选算出来交给上层编排；注入的候选
仍要通过全部执行闸门（Q 级、追高上限、入场模型、单票风险、双源校验），
所以这里不是绕过风控的直通车。

依赖方向：本模块只依赖基础 owner（数值合同、证券权限）与行情读取，
不 import 交易引擎。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

try:
    import alt_data as AD
except ImportError:  # 替代数据层缺失只降级信号丰富度，不中断交易。
    AD = None

from paper_common import as_date as _date
from paper_numeric import num as _num
from paper_trading_rules import security_scope as _security_scope

def _sector_key(value):
    text = str(value or "").strip()
    for suffix in ("行业", "Ⅱ", "Ⅲ", "II", "III", "概念"):
        text = text.replace(suffix, "")
    return text

SECTOR_SURGE_LANE_VERSION = "sector-surge-lane-v1"

def _sector_surge_lane_candidates(sector_heat, universe, live_map,
                                  seen_codes=None, market=None,
                                  max_total=4, per_sector=2):
    """盘中板块异动直通车道（仅 sector_rotation 候选层）。

    背景：候选池锚定昨夜因子排名，主题在盘中引爆时（如 2026-08-20 白银
    首日 +6%~7%）成员股因 mom5/mom20 还停留在昨日平淡数据而排不进
    top-N，直到第 3 日才被看到——此时只剩追高或踏空。本车道在板块
    热度榜前 5 且板块涨幅 ≥2% 时，把该板块实时主力资金最强的成员直接
    注入候选。注入候选仍需通过全部执行闸门（Q 级、追高上限、入场
    模型、单票风险、双源校验），不是绕过风控的直通车。
    """
    seen_codes = set(seen_codes or ())
    surge = [
        heat for heat in (sector_heat or {}).values()
        if int(_num(heat.get("rank"), 999)) <= 5 and _num(heat.get("pct")) >= 2.0
    ]
    if not surge:
        return [], None
    # 双确认：板块异动需同花顺题材命中才注入（2026-08-27）
    # 单独板块异动易被大盘普涨稀释，单独题材命中可能在弱板块中；两者叠加才注入。
    # AD 缺失时退化为单确认（fail-open）；AD 可用但题材为空时不注入（fail-closed）。
    ad_available = AD is not None
    ths_hot_codes = set()
    if ad_available:
        try:
            ths_rows = AD.ths_hot_reason() or []
            for _r in ths_rows:
                _c = str(_r.get("code") or "")
                if _c:
                    ths_hot_codes.add(_c)
        except Exception:
            ths_hot_codes = set()
    if ad_available:
        code_to_sector = {str(r.get("code") or ""): _sector_key(str(r.get("industry") or "")) for r in (universe or [])}
        hot_sector_keys = {code_to_sector.get(c) for c in ths_hot_codes if code_to_sector.get(c)}
        hot_sector_keys.discard("")
        confirmed = [h for h in surge if _sector_key(h.get("name") or "") in hot_sector_keys]
        if confirmed:
            surge = confirmed
        else:
            # 无题材确认：返回空并带审计说明，不注入
            return [], {"version": SECTOR_SURGE_LANE_VERSION, "trigger": "板块热度 rank≤5 且 pct≥2.0% (题材未命中)",
                        "sectors": [h.get("name") for h in surge], "injected": [], "theme_hit": False,
                        "note": "板块异动但同花顺题材未命中，双确认不注入（等待题材确认）"}
    heat_by_key = {}
    for heat in surge:
        heat_by_key[_sector_key(heat.get("name") or "")] = heat
    members = []
    for row in universe or []:
        code = str(row.get("code") or "")
        if not code or code in seen_codes:
            continue
        if row.get("risk_flag") or "ST" in str(row.get("name") or "").upper() or "退" in str(row.get("name") or ""):
            continue
        key = _sector_key(str(row.get("industry") or ""))
        if key not in heat_by_key:
            continue
        live = live_map.get(code) or {}
        price = _num(live.get("price"), _num(row.get("price")))
        pct = _num(live.get("pct"), None)
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # 已涨停附近/下跌的成员不注入；0.5% 以下尚未启动也不占车道。
        if not isinstance(pct, (int, float)) or pct <= 0.5 or pct >= 9.0:
            continue
        main_pct = _num(live.get("main_pct"), 0.0)
        vol_ratio = _num(live.get("vol_ratio"), 0.0)
        members.append((main_pct, vol_ratio, code, row, live, price, pct, key))
    if not members:
        return [], None
    members.sort(key=lambda item: (item[0], item[1]), reverse=True)
    lane, per_count, meta_rows = [], {}, []
    for main_pct, vol_ratio, code, row, live, price, pct, key in members:
        if per_count.get(key, 0) >= per_sector or len(lane) >= max_total:
            continue
        heat = heat_by_key[key]
        # 合成评分只用真实主力资金证据，且硬顶 0.74：替补评分合成中
        # rank_score 占 20% 权重，若车道评分达到 0.75+ 会凭空武装紧急
        # 择强换仓（门槛 75 分）。车道候选未经完整模型排名，不允许驱动
        # 强制换仓——只能凭真实入场评估通过后正常入场。
        lane_score = min(0.74, 0.55 + max(main_pct, 0.0) / 25.0)
        lane.append({
            "code": code, "name": row.get("name"), "industry": row.get("industry"),
            "price": price, "pct": pct,
            "score": round(lane_score, 3),
            "entry_path": "sector_surge",
            "sector_heat": heat,
            "sector_threshold_context": _sector_entry_threshold_context(
                sector_heat, market, heat),
            "candidate_status": "sector_surge_lane",
            "surge_lane_context": {
                "version": SECTOR_SURGE_LANE_VERSION,
                "main_pct": main_pct, "vol_ratio": vol_ratio,
                "reason": "盘中板块异动直通：热度前5且板块涨幅≥2%，按实时主力资金排名注入",
            },
        })
        per_count[key] = per_count.get(key, 0) + 1
        meta_rows.append({"code": code, "name": row.get("name"),
                          "sector": heat.get("name"), "main_pct": main_pct, "pct": pct})
    meta = None
    if lane:
        meta = {
            "version": SECTOR_SURGE_LANE_VERSION,
            "trigger": "板块热度 rank≤5 且板块涨幅≥2.0% + 同花顺题材双确认",
            "sectors": [h.get("name") for h in surge],
            "injected": meta_rows,
            "theme_hit": True,
            "note": "双确认注入：板块异动且题材命中；候选仍走全部执行闸门",
        }
    return lane, meta

CONCEPT_EXPANSION_LANE_VERSION = "concept-expansion-lane-v3"

def _concept_reverse_map_leaders(universe, live_map, max_leaders=3):
    """Return a bounded set of live leaders for reverse concept discovery.

    This is discovery-only.  It deliberately excludes the leader from the
    later peer lane, so a limit-up stock cannot turn its own tag into a chase
    order.  The high threshold also prevents a broad market of merely green
    names from fanning out into dozens of concept requests.
    """
    base_by_code = {str(row.get("code") or ""): row for row in (universe or [])}
    leaders = []
    for code, live in (live_map or {}).items():
        base = base_by_code.get(str(code)) or {}
        scope = _security_scope(code, live.get("name") or base.get("name"), base.get("risk_flag"))
        if not scope["allowed"]:
            continue
        pct = _num(live.get("pct"), -999.0)
        if (pct < 8.5 or pct > 19.5 or _num(live.get("price"), 0.0) <= 0
                or _num(live.get("main_net"), 0.0) <= 0
                or _num(live.get("vol_ratio"), 0.0) < 0.8):
            continue
        leaders.append({
            "code": str(code), "name": live.get("name") or base.get("name"),
            "pct": pct, "price": _num(live.get("price"), 0.0),
            "main_net": _num(live.get("main_net"), 0.0),
            "main_pct": _num(live.get("main_pct"), 0.0),
            "vol_ratio": _num(live.get("vol_ratio"), 0.0),
        })
    leaders.sort(key=lambda row: (row["pct"], row["main_net"], row["vol_ratio"]), reverse=True)
    return leaders[:max(1, min(int(max_leaders), 3))]

def _concept_expansion_lane_candidates(concepts, universe, live_map,
                                       seen_codes=None, market=None,
                                       asof_date=None, max_total=6, per_concept=2):
    """Expand strong concepts into fresh, funded, not-yet-limit-up peers."""
    seen_codes = set(seen_codes or ())
    universe_by_code = {str(row.get("code") or ""): row for row in (universe or []) if row.get("code")}
    expected_day = _date(asof_date or dt.date.today()).isoformat()
    pool, skipped_stale = [], 0
    for concept in concepts or []:
        rank = int(_num(concept.get("rank"), 999))
        concept_pct = _num(concept.get("pct"), 0.0)
        concept_flow = _num(concept.get("main_net"), 0.0)
        breadth = _num(concept.get("positive_ratio"), 0.0)
        leader_context = list(concept.get("leader_context") or [])
        leader_driven = bool(leader_context)
        if not concept.get("complete") or breadth < 0.45:
            continue
        if leader_driven:
            # Reverse mapping is allowed to discover a board which has not
            # reached the top of the aggregate flow ranking yet, but only when
            # its strong leader is independently corroborated by breadth and
            # at least two tradeable peers.  It never includes that leader.
            if int(_num(concept.get("member_count"), 0)) < 5 or int(_num(concept.get("active_peer_count"), 0)) < 2:
                continue
        elif rank > 10 or concept_pct <= 0.4 or concept_flow <= 0:
            continue
        leader_codes = {str(row.get("code") or "") for row in leader_context}
        for member in concept.get("members") or []:
            code = str(member.get("code") or "")
            base = universe_by_code.get(code)
            live = live_map.get(code)
            # Formal candidates must be present in the full-market live map;
            # concept-member quotes are discovery evidence only.
            if not code or code in seen_codes or code in leader_codes or base is None or live is None:
                continue
            quote_at = str(live.get("quote_at") or "")
            if not quote_at.startswith(expected_day):
                skipped_stale += 1
                continue
            scope = _security_scope(code, live.get("name") or base.get("name"), base.get("risk_flag"))
            if not scope["allowed"]:
                continue
            price, pct = _num(live.get("price"), 0.0), _num(live.get("pct"), 999.0)
            main_pct, main_net = _num(live.get("main_pct"), 0.0), _num(live.get("main_net"), 0.0)
            super_net, vol_ratio = _num(live.get("super_net"), 0.0), _num(live.get("vol_ratio"), 0.0)
            if price <= 0 or not (0.2 < pct < 9.0) or main_net <= 0 or main_pct <= 0 or vol_ratio < 0.65:
                continue
            price_stage = max(0.0, 1.0 - abs(pct - 3.0) / 6.0)
            score = min(0.74, 0.46 + (0.035 if leader_driven else max(0, 10 - rank) * 0.008)
                        + min(0.08, main_pct / 100.0)
                        + min(0.05, max(0.0, vol_ratio - 0.6) * 0.025)
                        + price_stage * 0.05 + min(0.04, breadth * 0.04))
            pool.append((score, main_net, super_net, -pct, code, {
                "code": code, "name": live.get("name") or member.get("name") or base.get("name"),
                "industry": base.get("industry") or live.get("industry"),
                "price": price, "pct": pct, "score": round(score, 3),
                "entry_path": "concept_expansion", "candidate_status": "concept_expansion_lane",
                "sector_heat": {"rank": rank, "name": concept.get("name"), "pct": concept_pct,
                                "main_net": concept_flow, "positive_ratio": breadth,
                                "source": concept.get("source")},
                "concept_context": {"version": CONCEPT_EXPANSION_LANE_VERSION,
                                    "concept_code": concept.get("code"),
                                    "concept_name": concept.get("name"), "concept_rank": rank,
                                    "discovery_path": "leader_reverse_map" if leader_driven else "concept_flow_topn",
                                    "leader_context": leader_context,
                                    "active_peer_count": int(_num(concept.get("active_peer_count"), 0)),
                                    "member_main_pct": main_pct, "member_main_net": main_net,
                                    "quote_at": quote_at, "constituents_complete": True},
            }))
    pool.sort(reverse=True, key=lambda row: row[:5])
    lane, per_counts, emitted = [], {}, set()
    for _, _, _, _, code, item in pool:
        concept_name = item["concept_context"]["concept_name"]
        if code in emitted or per_counts.get(concept_name, 0) >= per_concept:
            continue
        emitted.add(code)
        per_counts[concept_name] = per_counts.get(concept_name, 0) + 1
        lane.append(item)
        if len(lane) >= max_total:
            break
    return lane, {"version": CONCEPT_EXPANSION_LANE_VERSION, "injected": len(lane),
                  "concepts": sorted(per_counts), "skipped_stale_live": skipped_stale,
                  "note": "完整概念成分仅作发现；领涨股仅用于反查概念且被排除，正式候选必须命中当日全市场实时行情"}

def _ths_hot_lane_candidates(universe, live_map, seen_codes=None,
                             asof_date=None, max_total=3):
    """同花顺热点题材车道（P0，sector_rotation 候选层）。

    同花顺编辑部人工运营的"当日强势股+题材归因"是市场上最快的题材
    信号（73ms 零鉴权，~125 只/日）。命中榜单且仍可合规买入的成员
    直接注入候选，题材标签随 payload 入审计。与板块异动车道的差异：
    本车道以"个股已确认强势+题材明确"为准，不依赖板块聚合排名。
    注入候选仍需通过追高上限、Q级、入场模型、资金等全部执行闸门；
    评分与 surge lane 同规：证据合成、硬顶 0.74，不驱动强制换仓。
    """
    if AD is None:
        return [], None
    seen_codes = set(seen_codes or ())
    try:
        hot_rows = AD.ths_hot_reason() or []
    except Exception:
        hot_rows = []
    if not hot_rows:
        return [], None
    hot_by_code = {str(row.get("code") or ""): row for row in hot_rows}
    universe_by_code = {
        str(row.get("code") or ""): row for row in (universe or [])
        if row.get("code")
    }
    lane = []
    valid_pct = sorted(
        float(row.get("pct")) for row in hot_rows
        if isinstance(row.get("pct"), (int, float))
    )
    valid_dde = sorted(
        float(row.get("dde_net")) for row in hot_rows
        if isinstance(row.get("dde_net"), (int, float))
    )

    def _percentile(value, population):
        if not population or not isinstance(value, (int, float)):
            return 0.0
        return sum(1 for item in population if item <= float(value)) / len(population)

    for code, hot in hot_by_code.items():
        if code in seen_codes or code not in universe_by_code:
            continue
        row = universe_by_code[code]
        if row.get("risk_flag") or "ST" in str(row.get("name") or "").upper():
            continue
        live = live_map.get(code) or {}
        price = _num(live.get("price"), _num(row.get("price")))
        pct = _num(live.get("pct"), None)
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        # 已涨停附近/下跌的不占车道；与 surge lane 同一可交易区间。
        if not isinstance(pct, (int, float)) or pct <= 0.5 or pct >= 9.0:
            continue
        dde = _num(hot.get("dde_net"), 0.0)
        theme = str(hot.get("reason") or "")[:60]
        # DDE absolute units are not a stable public contract.  Rank values
        # inside the same snapshot and penalise late/high-runup names instead
        # of treating the raw number as a permanent scoring scale.
        pct_rank = _percentile(pct, valid_pct)
        dde_rank = _percentile(dde, valid_dde)
        maturity_penalty = max(0.0, pct - 5.5) * 0.015
        lane_score = min(0.70, max(
            0.0, 0.54 + pct_rank * 0.08 + dde_rank * 0.06 - maturity_penalty
        ))
        lane.append({
            "code": code, "name": row.get("name"), "industry": row.get("industry"),
            "price": price, "pct": pct,
            "score": round(lane_score, 3),
            "entry_path": "ths_hot",
            "candidate_status": "ths_hot_lane",
            "ths_hot_context": {
                "theme": theme,
                "turnover": hot.get("turnover"),
                "dde_net": dde,
                "pct_percentile": round(pct_rank, 4),
                "dde_percentile": round(dde_rank, 4),
                "maturity_penalty": round(maturity_penalty, 4),
                "source_at": hot.get("source_at"),
                "reason": "同花顺当日强势股人工题材归因命中；全部执行闸门照常生效",
            },
        })
    lane.sort(key=lambda item: (
        _num(item.get("score")),
        _num((item.get("ths_hot_context") or {}).get("dde_percentile")),
    ), reverse=True)
    lane = lane[:max_total]
    meta_rows = [
        {"code": item.get("code"), "name": item.get("name"),
         "theme": (item.get("ths_hot_context") or {}).get("theme"),
         "pct": item.get("pct"), "score": item.get("score")}
        for item in lane
    ]
    meta = None
    if lane:
        meta = {
            "version": "ths-hot-lane-v1",
            "source": "同花顺当日强势股题材归因",
            "hot_count": len(hot_rows),
            "injected": meta_rows,
            "note": "注入候选仍走全部执行闸门；追高上限照常生效",
        }
    return lane, meta

def _sector_heat_map(universe, sector_rows=None):
    """Build a live breadth-aware sector map.

    The old map was based only on the leading pages of the gainers list.  That
    identifies a sector after several names have already accelerated, but it
    misses the earlier, more useful rotation phase.  We retain that source as
    corroboration, while calculating breadth and median strength from the
    full live universe.  This is a ranking context only: it cannot bypass any
    security, quote, Q-level, capital or risk gate.
    """
    rows = list(sector_rows or [])
    source = "盘中热点板块"

    # First build same-snapshot industry statistics from the full universe.
    # A valid onset needs breadth and positive median strength, not one
    # limit-up stock.  Missing main-flow data is neutral rather than positive.
    grouped = {}
    for item in universe or []:
        key = _sector_key(item.get("industry"))
        pct = item.get("pct")
        if not key or not isinstance(pct, (int, float)) or abs(float(pct)) > 20:
            continue
        bucket = grouped.setdefault(key, {"pct": [], "main_pct": [], "codes": set()})
        bucket["pct"].append(float(pct))
        main_pct = item.get("main_pct")
        if isinstance(main_pct, (int, float)) and abs(float(main_pct)) <= 100:
            bucket["main_pct"].append(float(main_pct))
        code = str(item.get("code") or "")
        if code:
            bucket["codes"].add(code)

    def _median(values):
        values = sorted(values)
        if not values:
            return 0.0
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0

    live_metrics = {}
    for key, bucket in grouped.items():
        values = bucket["pct"]
        count = len(bucket["codes"]) or len(values)
        if count < 5:
            continue
        median_pct = _median(values)
        positive_ratio = sum(value > 0 for value in values) / len(values)
        median_main_pct = _median(bucket["main_pct"]) if bucket["main_pct"] else None
        # Early rotation: broad participation, modest positive median and no
        # single-name inference.  The flow clause is only required when flow
        # coverage exists for the group; unknown flow never grants a bonus.
        flow_confirmed = median_main_pct is not None and median_main_pct >= 0.0
        onset = (
            median_pct >= 0.35
            and positive_ratio >= 0.58
            and flow_confirmed
        )
        onset_score = 0.0
        if onset:
            onset_score = min(
                1.0,
                0.34
                + min(0.26, median_pct * 0.18)
                + min(0.24, max(0.0, positive_ratio - 0.50) * 1.2)
                + (0.10 if flow_confirmed else 0.0),
            )
        live_metrics[key] = {
            "member_count": count,
            "median_pct": round(median_pct, 3),
            "positive_ratio": round(positive_ratio, 3),
            "median_main_pct": round(median_main_pct, 3) if median_main_pct is not None else None,
            "early_rotation": bool(onset),
            "early_rotation_score": round(onset_score, 3),
        }

    if not rows:
        rows = [
            {
                "name": key,
                "pct": metrics["median_pct"],
                "leader_count": metrics["member_count"],
                "positive_ratio": metrics["positive_ratio"],
                "source": "全市场实时行业聚合",
            }
            for key, metrics in live_metrics.items()
        ]
        rows.sort(key=lambda row: (_num(row.get("pct"), -999), _num(row.get("positive_ratio"), 0)), reverse=True)
        source = "全市场实时行业聚合"

    # Keep the external top-sector rows, then add broad early-rotation groups
    # that are not yet in the top-gainer pages.
    by_key = {_sector_key(row.get("name")): dict(row) for row in rows if _sector_key(row.get("name"))}
    for key, metrics in live_metrics.items():
        if metrics["early_rotation"] and key not in by_key:
            by_key[key] = {
                "name": key, "pct": metrics["median_pct"],
                "leader_count": metrics["member_count"],
                "positive_ratio": metrics["positive_ratio"],
                "source": "全市场实时行业聚合",
            }
    ordered = sorted(
        by_key.values(),
        key=lambda row: (
            bool(live_metrics.get(_sector_key(row.get("name")), {}).get("early_rotation")),
            _num(row.get("pct"), -999), _num(row.get("positive_ratio"), 0),
        ),
        reverse=True,
    )
    output = {}
    for rank, row in enumerate(ordered[:35], 1):
        key = _sector_key(row.get("name"))
        if not key:
            continue
        raw_pct = _num(row.get("pct"), 0)
        if abs(raw_pct) > 30:
            continue
        pct = max(-20.0, min(20.0, raw_pct))
        metrics = live_metrics.get(key, {})
        onset_score = _num(metrics.get("early_rotation_score"), 0.0)
        strength = max(0.0, 2.5 - (rank - 1) * 0.10) + max(0.0, pct) * 0.18 + onset_score * 0.35
        output[key] = {
            "rank": rank,
            "name": row.get("name") or key,
            "pct": round(pct, 2),
            "score": round(strength, 3),
            "top_stock": row.get("top_stock"),
            "top_stock_code": row.get("top_stock_code"),
            "source": row.get("source") or source,
            **metrics,
        }
    return output

def _sector_entry_threshold_context(sector_heat, market, candidate_heat=None):
    """Derive a same-day sector-entry threshold from market and heat dispersion.

    A static 0.68 threshold makes a broad momentum day and a weak rotation day
    indistinguishable.  The context is persisted on each candidate so an order
    audit can show the exact daily threshold and why it moved.
    """
    heats = list((sector_heat or {}).values())
    top = heats[:10]
    positive_top = sum(1 for item in top if _num(item.get("pct"), 0) > 0)
    light = str((market or {}).get("light") or "unknown")
    breadth = _num((market or {}).get("breadth_up_pct"), None)
    threshold = {"green": 0.59, "yellow": 0.63, "red": 0.68}.get(light, 0.67)
    reasons = [f"市场{ {'green':'绿灯','yellow':'黄灯','red':'红灯'}.get(light, '数据未知') }"]
    if breadth is not None:
        if breadth >= 65:
            threshold -= 0.03
            reasons.append(f"上涨家数占比 {breadth:.1f}% 较高")
        elif breadth >= 55:
            threshold -= 0.01
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏强")
        elif breadth < 45:
            threshold += 0.03
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏弱")
        elif breadth < 50:
            threshold += 0.015
            reasons.append(f"上涨家数占比 {breadth:.1f}% 偏谨慎")
    else:
        threshold += 0.015
        reasons.append("市场宽度缺失，额外保守")
    if top:
        if positive_top >= 7:
            threshold -= 0.02
            reasons.append(f"前{len(top)}热点中 {positive_top} 个上涨，热点扩散充分")
        elif positive_top <= 2:
            threshold += 0.025
            reasons.append(f"前{len(top)}热点中仅 {positive_top} 个上涨，热点集中")
    else:
        threshold += 0.02
        reasons.append("热点板块样本不足")
    candidate_heat = candidate_heat or {}
    rank = int(_num(candidate_heat.get("rank"), 999))
    pct = _num(candidate_heat.get("pct"), 0)
    if rank <= 3 and pct >= 1.5:
        threshold -= 0.015
        reasons.append(f"所属板块第{rank}名且涨幅 {pct:+.2f}%")
    elif rank > 8 or pct < 0.5:
        threshold += 0.02
        reasons.append(f"所属板块第{rank}名、涨幅 {pct:+.2f}%")
    threshold = round(max(0.54, min(0.72, threshold)), 3)
    return {
        "threshold": threshold,
        "market_light": light,
        "breadth_up_pct": breadth,
        "positive_top_count": positive_top,
        "top_sample_count": len(top),
        "sector_rank": rank,
        "sector_pct": round(pct, 2),
        "reason": "；".join(reasons),
    }

def _star_sector_impulse(universe, asof_date=None):
    """Map broad STAR-board strength into a small same-industry bonus.

    STAR stocks remain untradable.  Their aggregate move is context only: at
    least five same-day valid peers, a positive majority and a positive median
    are required.  The bounded bonus cannot override any entry or risk gate.
    """
    grouped = {}
    latest = None
    dated_rows = []
    for item in universe or []:
        stamp = str(item.get("quote_at") or item.get("time") or "")
        source_day = stamp[:10]
        if len(source_day) == 10 and source_day[4:5] == "-" and source_day[7:8] == "-":
            dated_rows.append(source_day)
    target_day = _date(asof_date or dt.date.today()).isoformat()
    eligible_days = [value for value in dated_rows if value <= target_day]
    day = max(eligible_days, default=target_day)
    for row in universe or []:
        scope = _security_scope(row.get("code"), row.get("name"), row.get("risk_flag"))
        if scope["board"] != "科创板":
            continue
        quote_at = str(row.get("quote_at") or row.get("time") or "")
        if not quote_at or quote_at[:10] != day:
            continue
        pct = _num(row.get("pct"), None)
        sector = _sector_key(row.get("industry"))
        if not sector or pct is None or abs(pct) > 20:
            continue
        grouped.setdefault(sector, []).append(float(pct))
        latest = max(latest or quote_at, quote_at) if quote_at else latest
    output = {}
    for sector, values in grouped.items():
        if len(values) < 5:
            continue
        series = pd.Series(values, dtype="float64")
        median_pct = float(series.median())
        positive_ratio = float((series > 0).mean())
        if median_pct < 0.8 or positive_ratio < 0.60:
            continue
        bonus = min(0.12, max(0.0, median_pct - 0.5) * 0.018 + max(0.0, positive_ratio - 0.5) * 0.08)
        output[sector] = {
            "sample_count": len(values),
            "median_pct": round(median_pct, 2),
            "positive_ratio_pct": round(positive_ratio * 100, 1),
            "bonus": round(bonus, 3),
            "source": "科创板同业实时表现（仅作映射加分）",
            "asof": latest,
        }
    return output
