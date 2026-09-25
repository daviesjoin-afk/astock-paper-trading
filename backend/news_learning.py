# -*- coding: utf-8 -*-
"""Auditable news-event learning for the paper-trading system.

The module records when the system first observed an event, then waits for
completed 1/3/5-trading-day outcomes.  It never trusts a publication timestamp
as proof that the paper engine knew the event at that time.  Source credibility
and market impact are deliberately modelled as separate questions.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import data_fetcher as dfc


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import data_paths
import paper_position_read_model as PPRM
DB_PATH = data_paths.data_path("adaptive_learning.sqlite3")
PAPER_DB_PATH = data_paths.data_path("paper_trading.sqlite3")
BENCHMARK_CACHE_KEY = "BENCH_000300"
TZ = ZoneInfo("Asia/Shanghai")
HORIZONS = (1, 3, 5)
ENGINE_VERSION = "news-learning-v2"
MIN_MATURE_EVENTS = 30
MIN_EVENT_DATES = 10
MIN_SOURCE_GRADES = 2
POOL_LIMIT = 180
MAJOR_EVENT_MIN_SCORE = 0.62

MAJOR_EVENT_RULES = (
    ("ai_infrastructure", 0.88, ("数据中心", "资本开支", "算力基础设施", "AI基础设施", "人工智能投资", "服务器集群")),
    ("monetary_policy", 0.86, ("降准", "降息", "加息", "美联储", "央行", "流动性投放")),
    ("industry_policy", 0.82, ("产业政策", "专项规划", "监管新规", "出口管制", "关税", "反补贴")),
    ("geopolitics", 0.82, ("制裁", "冲突升级", "停火", "地缘政治", "贸易摩擦")),
    ("commodity_energy", 0.76, ("原油", "黄金", "铜价", "稀土", "电价", "天然气")),
    ("systemic_risk", 0.92, ("金融危机", "银行挤兑", "主权违约", "重大事故", "大规模停产")),
    ("supply_chain", 0.74, ("供应中断", "停产", "缺货", "扩产", "产能投资", "重大订单")),
)

THEME_MAP = {
    "ai_infrastructure": {
        "keywords": ("meta", "微软", "谷歌", "亚马逊", "openai", "数据中心", "资本开支", "算力", "ai基础设施", "服务器"),
        "industries": ("通信设备", "光学光电子", "元件", "计算机设备", "电源设备", "软件开发", "专用设备"),
        "label": "AI与数据中心资本开支",
    },
    "semiconductor": {
        "keywords": ("半导体", "芯片", "先进制程", "存储器", "晶圆", "光刻"),
        "industries": ("半导体", "电子化学品", "元件", "专用设备"),
        "label": "半导体供应链",
    },
    "energy": {
        "keywords": ("原油", "天然气", "电力", "储能", "电网", "能源"),
        "industries": ("油气开采", "电力", "电网设备", "电池", "光伏设备", "风电设备"),
        "label": "能源与电力",
    },
    "liquidity": {
        "keywords": ("降准", "降息", "加息", "央行", "流动性", "美联储"),
        "industries": ("银行", "证券", "保险", "房地产"),
        "label": "宏观流动性",
    },
    "trade_policy": {
        "keywords": ("关税", "出口管制", "制裁", "反补贴", "贸易摩擦"),
        "industries": ("半导体", "通信设备", "汽车零部件", "光伏设备", "电池"),
        "label": "贸易与出口政策",
    },
}


EVENT_RULES = (
    ("regulatory_penalty", -1, 1.00, ("立案", "处罚", "行政监管", "纪律处分", "调查通知")),
    ("earnings_warning", -1, 0.85, ("预亏", "亏损", "业绩下降", "业绩预减", "减值")),
    ("shareholder_reduction", -1, 0.70, ("减持", "拟减持")),
    ("litigation", -1, 0.75, ("诉讼", "仲裁", "冻结", "被执行")),
    # “解除质押”已移入 EVENT_OVERRIDE_RULES 的利好分支；此处只保留基础
    # 质押关键词，避免同一标题被顺序首匹配判成利空。
    ("pledge", -1, 0.50, ("质押",)),
    ("abnormal_volatility", 0, 0.55, ("异常波动", "风险提示", "股票交易异常")),
    ("earnings_positive", 1, 0.75, ("预增", "扭亏", "业绩增长", "创历史新高")),
    ("buyback", 1, 0.65, ("回购", "注销股份")),
    ("shareholder_increase", 1, 0.60, ("增持", "承诺不减持")),
    ("contract_order", 1, 0.60, ("中标", "签订合同", "重大合同", "订单")),
    ("dividend", 1, 0.45, ("分红", "派息", "利润分配")),
    ("restructuring", 0, 0.70, ("重组", "资产收购", "发行股份", "重大资产")),
    ("governance", 0, 0.45, ("董事", "监事", "高级管理人员", "控制权", "股东大会")),
    ("industry_policy", 0, 0.50, ("政策", "产业规划", "行业规范", "监管要求")),
)

# 否定/反转语义短语必须先于 EVENT_RULES 匹配：classify 是顺序首匹配，
# “承诺不减持”此前会先命中 shareholder_reduction 的“减持”，把正面公告
# 系统性误判为负面并传导到入场阈值 overlay。
EVENT_OVERRIDE_RULES = (
    ("shareholder_increase", 1, 0.60, ("承诺不减持", "终止减持", "暂不减持", "取消减持", "延期减持")),
    ("pledge_release", 1, 0.40, ("解除质押", "解除部分质押")),
)


def _now():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value, fallback=None):
    try:
        return json.loads(value) if value else (fallback if fallback is not None else {})
    except (TypeError, ValueError):
        return fallback if fallback is not None else {}


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS news_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            canonical_hash TEXT NOT NULL,
            article_id TEXT,
            code TEXT NOT NULL,
            name TEXT,
            title TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_url TEXT,
            evidence_grade TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            expected_direction INTEGER NOT NULL,
            severity REAL NOT NULL,
            parse_rule TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_news_events_code_seen ON news_events(code,first_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_news_events_type ON news_events(event_type,evidence_grade);
        CREATE TABLE IF NOT EXISTS news_event_outcomes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
            horizon INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            start_close REAL NOT NULL,
            end_close REAL NOT NULL,
            benchmark_start REAL,
            benchmark_end REAL,
            stock_return_pct REAL NOT NULL,
            benchmark_return_pct REAL,
            excess_return_pct REAL,
            max_adverse_pct REAL,
            price_source TEXT NOT NULL,
            matured_at TEXT NOT NULL,
            UNIQUE(event_id,horizon)
        );
        CREATE INDEX IF NOT EXISTS idx_news_outcomes_event ON news_event_outcomes(event_id,horizon);
        CREATE TABLE IF NOT EXISTS news_source_reputation(
            source_name TEXT PRIMARY KEY,
            evidence_grade TEXT NOT NULL,
            observed_events INTEGER NOT NULL,
            linked_pct REAL NOT NULL,
            unique_pct REAL NOT NULL,
            outcome_coverage_pct REAL NOT NULL,
            credibility_score REAL NOT NULL,
            detail TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS news_effectiveness(
            event_type TEXT NOT NULL,
            evidence_grade TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            samples INTEGER NOT NULL,
            directional_win_rate REAL,
            mean_excess_pct REAL,
            median_excess_pct REAL,
            downside_mean_pct REAL,
            confidence REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(event_type,evidence_grade,horizon)
        );
        CREATE TABLE IF NOT EXISTS news_factor_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            gates TEXT NOT NULL,
            weights TEXT NOT NULL,
            max_score_delta REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS news_learning_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            captured INTEGER NOT NULL DEFAULT 0,
            matured INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS news_candidate_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            slot TEXT NOT NULL,
            account_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            industry TEXT,
            pool_tier TEXT NOT NULL,
            rank_no INTEGER,
            score REAL,
            captured_at TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(snapshot_date,slot,account_id,code)
        );
        CREATE INDEX IF NOT EXISTS idx_news_candidate_active ON news_candidate_snapshots(valid_until,code,pool_tier);
        CREATE TABLE IF NOT EXISTS market_major_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            canonical_hash TEXT NOT NULL,
            article_id TEXT,
            title TEXT NOT NULL,
            summary TEXT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_url TEXT,
            evidence_grade TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            significance_score REAL NOT NULL,
            themes TEXT NOT NULL,
            affected_industries TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_market_major_seen ON market_major_events(first_seen_at DESC);
        CREATE TABLE IF NOT EXISTS market_event_candidate_links(
            event_id INTEGER NOT NULL REFERENCES market_major_events(id) ON DELETE CASCADE,
            code TEXT NOT NULL,
            name TEXT,
            industry TEXT,
            pool_tier TEXT NOT NULL,
            mapping_reason TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(event_id,code)
        );
        """
    )


def _clean_title(value):
    text = re.sub(r"\s+", "", str(value or "")).lower()
    return re.sub(r"[，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]<>《》]", "", text)


def _parse_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", raw)
    if not match:
        return None
    text = match.group(0).replace(" ", "T")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        return None


def classify(title):
    title = str(title or "")
    for event_type, direction, severity, words in EVENT_OVERRIDE_RULES:
        hit = next((word for word in words if word in title), None)
        if hit:
            return event_type, direction, severity, f"keyword:{hit}"
    for event_type, direction, severity, words in EVENT_RULES:
        hit = next((word for word in words if word in title), None)
        if hit:
            return event_type, direction, severity, f"keyword:{hit}"
    return "other", 0, 0.25, "keyword:fallback"


def _second_trade_weekday(day):
    cursor = day
    remaining = 2
    while remaining:
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def capture_candidate_snapshot(account_id, candidates, snapshot_date=None, slot="close", captured_at=None):
    """Persist the actual paper ranking before approval filters remove candidates."""
    day = dt.date.fromisoformat(str(snapshot_date or dt.datetime.now(TZ).date())[:10])
    captured = captured_at or _now()
    valid_until = _second_trade_weekday(day).isoformat()
    rows = list(candidates or [])[:30]
    with _connect() as conn:
        ensure_schema(conn)
        for rank_no, pick in enumerate(rows, 1):
            tier = "active_candidate" if rank_no <= 15 else "near_candidate"
            conn.execute(
                """INSERT INTO news_candidate_snapshots(
                       snapshot_date,slot,account_id,code,name,industry,pool_tier,rank_no,score,captured_at,valid_until,payload
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_date,slot,account_id,code) DO UPDATE SET
                       name=excluded.name,industry=excluded.industry,pool_tier=excluded.pool_tier,
                       rank_no=excluded.rank_no,score=excluded.score,captured_at=excluded.captured_at,
                       valid_until=excluded.valid_until,payload=excluded.payload""",
                (day.isoformat(), str(slot), str(account_id), str(pick.get("code") or ""),
                 pick.get("name"), pick.get("industry"), tier, rank_no,
                 float(pick.get("score")) if isinstance(pick.get("score"), (int, float)) else None,
                 captured, valid_until, _json(pick)),
            )
    return {"account_id": str(account_id), "captured": len(rows), "valid_until": valid_until}


def candidate_pool(conn=None, asof=None, limit=POOL_LIMIT):
    """Return a deduplicated, priority-ordered pool at code grain."""
    day = str(asof or dt.datetime.now(TZ).date())[:10]
    items = []
    if os.path.exists(PAPER_DB_PATH):
        paper = sqlite3.connect(PAPER_DB_PATH, timeout=20)
        paper.row_factory = sqlite3.Row
        try:
            # 「现在持有什么」必须来自当前 active cycle 的权威 lot，而不是
            # paper_positions 投影。投影没有 cycle_id / source_order_id，一个
            # 旧周期的残留镜像行会被当成当前 holding 并进入候选池、事件捕获与
            # 学习输入。此处按 cycle 过滤，且无 active cycle 时不给出任何 holding。
            for row in PPRM.current_holding_rows(paper):
                items.append({**row, "pool_tier": "holding",
                              "rank_no": 0, "source": "paper_position_lots"})
            for row in paper.execute(
                """SELECT code,name,industry,account_id,rank_score FROM paper_signals
                   WHERE status IN ('pending','deferred_capacity') AND intended_date>=?""", (day,)
            ):
                items.append({**dict(row), "pool_tier": "pending_signal", "rank_no": 0, "source": "paper_signals"})
            # Seed the pool from the latest completed ranking as well. This keeps
            # a newly upgraded deployment useful before the next close snapshot.
            cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=4)).isoformat()
            recent = paper.execute(
                """SELECT code,name,industry,account_id,rank_score,signal_date
                     FROM paper_signals
                    WHERE signal_date>=? AND status NOT IN ('expired','superseded')
                    ORDER BY account_id,signal_date DESC,rank_score DESC""", (cutoff,)
            ).fetchall()
            account_rank = defaultdict(int)
            for row in recent:
                account_id = str(row["account_id"])
                account_rank[account_id] += 1
                rank_no = account_rank[account_id]
                if rank_no > 30:
                    continue
                items.append({**dict(row), "pool_tier": "active_candidate" if rank_no <= 15 else "near_candidate",
                              "rank_no": rank_no, "source": "recent_paper_ranking"})
        finally:
            paper.close()
    owns = conn is None
    conn = conn or _connect()
    try:
        ensure_schema(conn)
        for row in conn.execute(
            """SELECT code,name,industry,account_id,pool_tier,rank_no,score,slot,captured_at
                 FROM news_candidate_snapshots WHERE valid_until>=?
                ORDER BY snapshot_date DESC,CASE pool_tier WHEN 'active_candidate' THEN 1 ELSE 2 END,rank_no""", (day,)
        ):
            items.append({**dict(row), "source": "candidate_snapshot"})
    finally:
        if owns:
            conn.close()
    priority = {"holding": 0, "pending_signal": 1, "active_candidate": 2, "near_candidate": 3}
    deduped = {}
    for item in items:
        code = str(item.get("code") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        current = deduped.get(code)
        if current is None or priority.get(item.get("pool_tier"), 9) < priority.get(current.get("pool_tier"), 9):
            deduped[code] = item
    rows = sorted(deduped.values(), key=lambda x: (priority.get(x.get("pool_tier"), 9), int(x.get("rank_no") or 999), x["code"]))
    return rows[: int(limit)]


def _paper_codes(limit=POOL_LIMIT):
    """最近出现过的代码（用于事件扫描范围），**不是**当前持仓声明。

    语义区分（Round-10 §6）：本函数只做 **historical/recent symbol discovery** ——
    给公告扫描一个有界的、可复现的代码集合。因此它允许使用 ``paper_positions``
    投影的 ``entry_date`` 作为"曾经出现过"的时间戳。

    但这**绝不**意味着那些代码是当前持仓：

    - 不能标成 ``pool_tier = "holding"``（holding 只在 :func:`candidate_pool`
      里由当前 active cycle 的权威 lot 产生）；
    - 不能提高 holding 优先级；
    - 不能用于候选排除或持仓存在性判断。

    真正「现在持有什么」请走 ``paper_position_read_model.current_holding_*``。
    """
    pool = candidate_pool(limit=limit)
    if pool:
        return [str(row["code"]) for row in pool]
    if not os.path.exists(PAPER_DB_PATH):
        return []
    conn = sqlite3.connect(PAPER_DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT code,MAX(observed_at) seen FROM(
                   SELECT code,COALESCE(entry_date,'') observed_at FROM paper_positions
                   UNION ALL SELECT code,COALESCE(signal_date,'') FROM paper_signals
                   UNION ALL SELECT code,COALESCE(created_at,'') FROM paper_orders
               ) WHERE length(code)=6 GROUP BY code ORDER BY seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [str(row["code"]) for row in rows]
    finally:
        conn.close()


def _event_rows(codes):
    rows = []
    for start in range(0, len(codes), 50):
        for item in dfc.fetch_company_announcements(codes[start:start + 50], page_size=100):
            rows.append({
                **item, "code": str(item.get("code") or ""),
                "title": item.get("summary") or "", "stock_codes": [str(item.get("code") or "")],
            })
    for item in dfc.fetch_fast_news(100):
        stock_codes = [str(code) for code in (item.get("stock_codes") or []) if str(code) in set(codes)]
        for code in stock_codes:
            rows.append({**item, "code": code, "title": item.get("title") or item.get("summary") or ""})
    return rows


def capture_events(conn, first_seen_at=None, codes=None):
    ensure_schema(conn)
    seen = first_seen_at or _now()
    codes = list(codes or _paper_codes())[:POOL_LIMIT]
    inserted = 0
    observed = 0
    for item in _event_rows(codes):
        code = str(item.get("code") or "")
        title = str(item.get("title") or item.get("summary") or "").strip()
        if len(code) != 6 or not title:
            continue
        observed += 1
        source_name = str(item.get("source") or "未知来源")
        source_type = str(item.get("source_type") or "unknown")
        source_url = str(item.get("source_url") or "").strip() or None
        grade = str(item.get("evidence_grade") or "D").upper()
        normalized = _clean_title(title)
        canonical_hash = hashlib.sha256(f"{code}|{normalized}".encode("utf-8")).hexdigest()
        identity = source_url or str(item.get("article_id") or "") or normalized
        event_key = hashlib.sha256(f"{source_name}|{code}|{identity}".encode("utf-8")).hexdigest()
        event_type, direction, severity, rule = classify(title)
        now = _now()
        cursor = conn.execute(
            """INSERT OR IGNORE INTO news_events(
                   event_key,canonical_hash,article_id,code,name,title,source_name,source_type,source_url,
                   evidence_grade,published_at,first_seen_at,event_type,expected_direction,severity,
                   parse_rule,raw_payload,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_key, canonical_hash, item.get("article_id"), code, item.get("name"), title,
             source_name, source_type, source_url, grade, _parse_time(item.get("time")), seen,
             event_type, direction, severity, rule, _json(item), now, now),
        )
        inserted += int(cursor.rowcount > 0)
    return {"codes": len(codes), "observed": observed, "captured": inserted, "first_seen_at": seen}


def _major_event_profile(item):
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or title)
    text = (title + " " + summary).lower()
    matches = []
    for event_type, base_score, words in MAJOR_EVENT_RULES:
        hit = next((word for word in words if str(word).lower() in text), None)
        if hit:
            matches.append((base_score, event_type, hit))
    # Global hyperscaler capex is material even when a short headline omits “data center”.
    hyperscaler = next((name for name in ("meta", "微软", "谷歌", "亚马逊", "openai") if name in text), None)
    investment = next((word for word in ("投入", "投资", "资本支出", "扩建", "采购") if word in text), None)
    if hyperscaler and investment:
        matches.append((0.88, "ai_infrastructure", f"{hyperscaler}+{investment}"))
    if not matches:
        return None
    score, event_type, trigger = max(matches)
    grade = str(item.get("evidence_grade") or "D").upper()
    score += {"A": 0.08, "B": 0.05, "C": 0.0, "D": -0.25}.get(grade, -0.1)
    if item.get("source_url"):
        score += 0.02
    themes = []
    industries = set()
    for theme_id, rule in THEME_MAP.items():
        if any(str(word).lower() in text for word in rule["keywords"]):
            themes.append({"id": theme_id, "label": rule["label"]})
            industries.update(rule["industries"])
    if event_type == "ai_infrastructure" and not any(row["id"] == "ai_infrastructure" for row in themes):
        rule = THEME_MAP["ai_infrastructure"]
        themes.append({"id": "ai_infrastructure", "label": rule["label"]})
        industries.update(rule["industries"])
    return {"event_type": event_type, "score": round(min(1.0, score), 3), "trigger": trigger,
            "themes": themes, "industries": sorted(industries)}


def _industry_match(candidate_industry, mapped):
    current = re.sub(r"[ⅡⅢI]+$", "", str(candidate_industry or "")).lower()
    target = re.sub(r"[ⅡⅢI]+$", "", str(mapped or "")).lower()
    return bool(current and target and (current in target or target in current))


def capture_major_events(conn, first_seen_at=None, pool=None):
    """Capture broad market events independently of stock-code tagging."""
    ensure_schema(conn)
    seen = first_seen_at or _now()
    pool = list(pool or candidate_pool(conn=conn))
    captured = linked = observed = 0
    for item in dfc.fetch_fast_news(120):
        profile = _major_event_profile(item)
        if not profile or profile["score"] < MAJOR_EVENT_MIN_SCORE:
            continue
        observed += 1
        title = str(item.get("title") or item.get("summary") or "").strip()
        if not title:
            continue
        source_name = str(item.get("source") or "未知来源")
        source_url = str(item.get("source_url") or "").strip() or None
        normalized = _clean_title(title)
        canonical_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        identity = source_url or str(item.get("article_id") or "") or normalized
        event_key = hashlib.sha256(f"{source_name}|{identity}".encode("utf-8")).hexdigest()
        now = _now()
        cursor = conn.execute(
            """INSERT OR IGNORE INTO market_major_events(
                   event_key,canonical_hash,article_id,title,summary,source_name,source_type,source_url,
                   evidence_grade,published_at,first_seen_at,event_type,significance_score,themes,
                   affected_industries,verification_status,raw_payload,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_key, canonical_hash, item.get("article_id"), title, item.get("summary"), source_name,
             item.get("source_type") or "news_aggregator", source_url, item.get("evidence_grade") or "C",
             _parse_time(item.get("time")), seen, profile["event_type"], profile["score"],
             _json(profile["themes"]), _json(profile["industries"]),
             "single_source_linked" if source_url else "unverified", _json({**item, "major_trigger": profile["trigger"]}), now, now),
        )
        captured += int(cursor.rowcount > 0)
        event = conn.execute("SELECT id FROM market_major_events WHERE event_key=?", (event_key,)).fetchone()
        if not event:
            continue
        stock_codes = {str(code) for code in (item.get("stock_codes") or [])}
        for candidate in pool:
            direct = str(candidate.get("code")) in stock_codes
            industry_hit = next((industry for industry in profile["industries"] if _industry_match(candidate.get("industry"), industry)), None)
            if not direct and not industry_hit:
                continue
            reason = "新闻原始股票标签" if direct else f"主题行业映射：{industry_hit}"
            confidence = 0.95 if direct else 0.72
            link_cursor = conn.execute(
                """INSERT OR IGNORE INTO market_event_candidate_links(
                       event_id,code,name,industry,pool_tier,mapping_reason,confidence,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (event["id"], candidate.get("code"), candidate.get("name"), candidate.get("industry"),
                 candidate.get("pool_tier") or "unknown", reason, confidence, now),
            )
            linked += int(link_cursor.rowcount > 0)
    return {"observed": observed, "captured": captured, "candidate_links": linked, "pool_size": len(pool),
            "first_seen_at": seen, "authority": "context_only_single_source_never_trades"}


def _usable_frame(code, begin):
    frame = dfc.load_cached_kline(code)
    wanted_end = dt.datetime.now(TZ).date().strftime("%Y%m%d")
    if frame is None or frame.empty or frame.index[-1].date() < dt.datetime.now(TZ).date() - dt.timedelta(days=4):
        fetched = dfc.fetch_kline(code, beg=begin.replace("-", ""), end=wanted_end)
        if fetched is not None and not fetched.empty:
            frame = fetched
    if frame is None or frame.empty:
        return None
    frame = frame.copy().sort_index()
    frame.index = frame.index.date
    return frame


def _benchmark_frame():
    frame = dfc.load_cached_kline(BENCHMARK_CACHE_KEY)
    if frame is None or frame.empty:
        frame = dfc.fetch_kline(None, beg="20230101", end=dt.datetime.now(TZ).strftime("%Y%m%d"), secid="1.000300")
    if frame is None or frame.empty:
        return None
    frame = frame.copy().sort_index()
    frame.index = frame.index.date
    return frame


def _eligible_dates(frame, first_seen):
    available = dt.datetime.fromisoformat(first_seen)
    current = dt.datetime.now(TZ)
    rows = []
    for value in frame.index:
        day = value if isinstance(value, dt.date) else value.date()
        # A close is usable only if it could have been known after first_seen.
        if day < available.date() or (day == available.date() and available.time() >= dt.time(15, 0)):
            continue
        if day == current.date() and current.time() < dt.time(15, 10):
            continue
        rows.append(day)
    return rows


def mature_outcomes(conn, max_codes=24):
    ensure_schema(conn)
    pending = conn.execute(
        """SELECT e.* FROM news_events e
           WHERE e.evidence_grade!='D' AND e.first_seen_at<=?
             AND (SELECT COUNT(*) FROM news_event_outcomes o WHERE o.event_id=e.id)<3
           ORDER BY e.first_seen_at,e.id LIMIT 240""",
        ((dt.datetime.now(TZ) - dt.timedelta(days=1)).isoformat(timespec="seconds"),),
    ).fetchall()
    by_code = defaultdict(list)
    for row in pending:
        by_code[row["code"]].append(row)
    benchmark = _benchmark_frame()
    inserted = 0
    processed_codes = 0
    for code, events in list(by_code.items())[:max_codes]:
        begin = min(str(row["first_seen_at"])[:10] for row in events)
        frame = _usable_frame(code, begin)
        if frame is None or frame.empty:
            continue
        processed_codes += 1
        for event in events:
            dates = _eligible_dates(frame, event["first_seen_at"])
            if not dates:
                continue
            start_date = dates[0]
            start_close = float(frame.loc[start_date, "close"])
            for horizon in HORIZONS:
                # horizon=1 means the first full trading-day move after the entry close.
                if len(dates) <= horizon:
                    continue
                end_date = dates[horizon]
                end_close = float(frame.loc[end_date, "close"])
                stock_return = (end_close / start_close - 1) * 100
                path = [float(frame.loc[day, "close"]) for day in dates[: horizon + 1]]
                adverse = min((price / start_close - 1) * 100 for price in path)
                bench_start = bench_end = bench_return = excess = None
                if benchmark is not None and start_date in benchmark.index and end_date in benchmark.index:
                    bench_start = float(benchmark.loc[start_date, "close"])
                    bench_end = float(benchmark.loc[end_date, "close"])
                    bench_return = (bench_end / bench_start - 1) * 100 if bench_start else None
                    excess = stock_return - bench_return if bench_return is not None else None
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO news_event_outcomes(
                           event_id,horizon,start_date,end_date,start_close,end_close,benchmark_start,
                           benchmark_end,stock_return_pct,benchmark_return_pct,excess_return_pct,
                           max_adverse_pct,price_source,matured_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event["id"], horizon, start_date.isoformat(), end_date.isoformat(), start_close,
                     end_close, bench_start, bench_end, stock_return, bench_return, excess, adverse,
                     str(getattr(frame, "attrs", {}).get("source") or "cached_or_public_kline"), _now()),
                )
                inserted += int(cursor.rowcount > 0)
    return {"matured": inserted, "processed_codes": processed_codes, "pending_events": len(pending)}


def recalibrate(conn):
    ensure_schema(conn)
    now = _now()
    conn.execute("DELETE FROM news_source_reputation")
    sources = conn.execute(
        """SELECT source_name,MIN(evidence_grade) grade,COUNT(*) events,
                  AVG(CASE WHEN source_url IS NOT NULL THEN 1.0 ELSE 0.0 END) linked,
                  COUNT(DISTINCT canonical_hash)*1.0/COUNT(*) uniqueness
           FROM news_events GROUP BY source_name"""
    ).fetchall()
    for row in sources:
        covered = conn.execute(
            """SELECT COUNT(DISTINCT e.id) FROM news_events e
               JOIN news_event_outcomes o ON o.event_id=e.id WHERE e.source_name=?""",
            (row["source_name"],),
        ).fetchone()[0]
        coverage = covered / max(int(row["events"]), 1)
        grade_base = {"A": 0.95, "B": 0.82, "C": 0.65, "D": 0.20}.get(row["grade"], 0.35)
        # Credibility is provenance quality, not whether prices later moved as expected.
        score = 100 * (0.55 * grade_base + 0.25 * float(row["linked"]) + 0.12 * float(row["uniqueness"]) + 0.08 * coverage)
        detail = {"grade_component": grade_base, "return_performance_excluded": True,
                  "outcome_coverage_is_auditability_not_directional_accuracy": True}
        conn.execute(
            "INSERT INTO news_source_reputation VALUES(?,?,?,?,?,?,?,?,?)",
            (row["source_name"], row["grade"], row["events"], row["linked"] * 100,
             row["uniqueness"] * 100, coverage * 100, round(score, 2), _json(detail), now),
        )

    conn.execute("DELETE FROM news_effectiveness")
    groups = conn.execute(
        """SELECT e.event_type,e.evidence_grade,o.horizon,e.expected_direction,o.excess_return_pct
           FROM news_event_outcomes o JOIN news_events e ON e.id=o.event_id
           WHERE o.excess_return_pct IS NOT NULL"""
    ).fetchall()
    grouped = defaultdict(list)
    for row in groups:
        grouped[(row["event_type"], row["evidence_grade"], row["horizon"])].append(row)
    for (event_type, grade, horizon), rows in grouped.items():
        values = [float(row["excess_return_pct"]) for row in rows]
        directed = [1 if int(row["expected_direction"]) * float(row["excess_return_pct"]) > 0 else 0
                    for row in rows if int(row["expected_direction"]) != 0]
        downside = [value for value in values if value < 0]
        confidence = min(100.0, math.sqrt(len(rows) / MIN_MATURE_EVENTS) * 100)
        conn.execute(
            "INSERT INTO news_effectiveness VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_type, grade, horizon, len(rows),
             (sum(directed) / len(directed) * 100 if directed else None), statistics.mean(values),
             statistics.median(values), (statistics.mean(downside) if downside else 0.0), confidence, now),
        )

    mature_events = conn.execute("SELECT COUNT(DISTINCT event_id) FROM news_event_outcomes WHERE horizon=5").fetchone()[0]
    event_dates = conn.execute("SELECT COUNT(DISTINCT substr(first_seen_at,1,10)) FROM news_events").fetchone()[0]
    grades = conn.execute(
        "SELECT COUNT(DISTINCT e.evidence_grade) FROM news_events e JOIN news_event_outcomes o ON o.event_id=e.id WHERE o.horizon=5"
    ).fetchone()[0]
    gates = {
        "mature_5d_events": {"current": mature_events, "required": MIN_MATURE_EVENTS, "passed": mature_events >= MIN_MATURE_EVENTS},
        "event_dates": {"current": event_dates, "required": MIN_EVENT_DATES, "passed": event_dates >= MIN_EVENT_DATES},
        "source_grades": {"current": grades, "required": MIN_SOURCE_GRADES, "passed": grades >= MIN_SOURCE_GRADES},
    }
    eligible = all(item["passed"] for item in gates.values())
    status = "micro_eligible" if eligible else "shadow"
    max_delta = 0.005 if eligible else 0.0
    version = f"news-{dt.datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')}"
    weights = {}
    for row in conn.execute("SELECT * FROM news_effectiveness WHERE horizon=5"):
        if int(row["samples"]) >= MIN_MATURE_EVENTS and float(row["confidence"]) >= 80:
            weights[f"{row['event_type']}:{row['evidence_grade']}"] = max(-1.0, min(1.0, float(row["mean_excess_pct"]) / 5.0))
    conn.execute(
        "INSERT INTO news_factor_versions(version,status,gates,weights,max_score_delta,reason,created_at) VALUES(?,?,?,?,?,?,?)",
        (version, status, _json(gates), _json(weights), max_delta,
         "达到样本门禁后仅允许±0.005入场阈值微调；当前继续影子记录" if not eligible else "样本门禁通过，进入有界微调资格", now),
    )
    return {"version": version, "status": status, "gates": gates, "weights": weights, "max_score_delta": max_delta}


def code_overlay(code, asof=None):
    """Read-only bounded paper-entry overlay; returns zero while gates are locked.

    code="__market__" returns an aggregate market-level signal from major events.
    """
    if not os.path.exists(DB_PATH):
        return {"status": "not_started", "threshold_delta": 0.0, "events": 0}
    with _connect() as conn:
        version = conn.execute("SELECT * FROM news_factor_versions ORDER BY id DESC LIMIT 1").fetchone()
        if not version or version["status"] != "micro_eligible" or float(version["max_score_delta"]) <= 0:
            return {"status": (version["status"] if version else "not_started"), "threshold_delta": 0.0, "events": 0}
        weights = _loads(version["weights"], {})
        cutoff = str(asof or _now())
        if len(cutoff) == 10:
            cutoff = (_now() if cutoff == dt.datetime.now(TZ).date().isoformat() else cutoff + "T23:59:59+08:00")
        if str(code) == "__market__":
            # 市场级：使用 major events 的主题权重汇总
            major_rows = conn.execute(
                """SELECT themes,significance_score,first_seen_at FROM market_major_events
                   WHERE first_seen_at<=? ORDER BY first_seen_at DESC LIMIT 20""",
                (cutoff,),
            ).fetchall()
            score = 0.0
            used = 0
            now = dt.datetime.now(TZ)
            for row in major_rows:
                themes = _loads(row["themes"], [])
                for theme in themes:
                    # themes 元素是 {"id","label"} 字典；权重键格式是
                    # "{event_type}:{evidence_grade}"。旧实现拼
                    # f"major:{dict}" 永远查不到，市场级 overlay 实际恒为 0。
                    theme_id = theme.get("id") if isinstance(theme, dict) else str(theme)
                    if not theme_id:
                        continue
                    matched = [w for key, w in weights.items()
                               if str(key).startswith(f"{theme_id}:")]
                    if not matched:
                        continue
                    learned = sum(matched) / len(matched)
                    age = max(0, (now - dt.datetime.fromisoformat(row["first_seen_at"])).days)
                    score += float(learned) * float(row["significance_score"] or 1.0) * math.exp(-age / 7.0)
                    used += 1
            bound = float(version["max_score_delta"])
            delta = max(-bound, min(bound, -score * bound * 0.5))
            return {"status": version["status"], "version": version["version"],
                    "threshold_delta": round(delta, 4), "events": used}
        rows = conn.execute(
            """SELECT event_type,evidence_grade,expected_direction,severity,first_seen_at FROM news_events
               WHERE code=? AND first_seen_at<=? ORDER BY first_seen_at DESC LIMIT 12""",
            (str(code), cutoff),
        ).fetchall()
        score = 0.0
        used = 0
        now = dt.datetime.now(TZ)
        for row in rows:
            learned = weights.get(f"{row['event_type']}:{row['evidence_grade']}")
            if learned is None:
                continue
            age = max(0, (now - dt.datetime.fromisoformat(row["first_seen_at"])).days)
            score += float(learned) * float(row["severity"]) * math.exp(-age / 5.0)
            used += 1
        bound = float(version["max_score_delta"])
        # Positive evidence lowers the required entry score; negative evidence raises it.
        delta = max(-bound, min(bound, -score * bound))
        return {"status": version["status"], "version": version["version"],
                "threshold_delta": round(delta, 4), "events": used}


def run_cycle(trigger="manual-ui", first_seen_at=None, codes=None):
    started = _now()
    with _connect() as conn:
        try:
            pool = candidate_pool(conn=conn)
            scoped_codes = list(codes or [row["code"] for row in pool])[:POOL_LIMIT]
            captured = capture_events(conn, first_seen_at=first_seen_at, codes=scoped_codes)
            major = capture_major_events(conn, first_seen_at=first_seen_at, pool=pool)
            matured = mature_outcomes(conn)
            factor = recalibrate(conn)
            detail = {"candidate_pool": {"size": len(pool), "codes": scoped_codes},
                      "capture": captured, "major_radar": major, "maturity": matured, "factor": factor,
                      "authority": "paper_only_bounded_no_broker_orders"}
            conn.execute(
                "INSERT INTO news_learning_runs(trigger,status,captured,matured,detail,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                (trigger, "completed", captured["captured"], matured["matured"], _json(detail), started, _now()),
            )
            return overview(conn)
        except Exception as exc:
            conn.execute(
                "INSERT INTO news_learning_runs(trigger,status,captured,matured,detail,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
                (trigger, "failed", 0, 0, _json({"error": f"{type(exc).__name__}: {exc}"}), started, _now()),
            )
            raise


def overview(conn=None):
    owns = conn is None
    conn = conn or _connect()
    try:
        ensure_schema(conn)
        totals = {
            "events": conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0],
            "linked_events": conn.execute("SELECT COUNT(*) FROM news_events WHERE source_url IS NOT NULL").fetchone()[0],
            "mature_outcomes": conn.execute("SELECT COUNT(*) FROM news_event_outcomes").fetchone()[0],
            "mature_5d_events": conn.execute("SELECT COUNT(DISTINCT event_id) FROM news_event_outcomes WHERE horizon=5").fetchone()[0],
            "event_types": conn.execute("SELECT COUNT(DISTINCT event_type) FROM news_events").fetchone()[0],
            "major_events": conn.execute("SELECT COUNT(*) FROM market_major_events").fetchone()[0],
            "major_candidate_links": conn.execute("SELECT COUNT(*) FROM market_event_candidate_links").fetchone()[0],
        }
        totals["linked_pct"] = round(totals["linked_events"] / max(totals["events"], 1) * 100, 1)
        events = [dict(row) for row in conn.execute(
            """SELECT e.*, (SELECT COUNT(*) FROM news_event_outcomes o WHERE o.event_id=e.id) outcome_count
               FROM news_events e ORDER BY first_seen_at DESC,id DESC LIMIT 20"""
        )]
        pool = candidate_pool(conn=conn)
        pool_counts = dict(Counter(row.get("pool_tier") or "unknown" for row in pool))
        major_events = []
        for row in conn.execute(
            """SELECT e.*,(SELECT COUNT(*) FROM market_event_candidate_links l WHERE l.event_id=e.id) candidate_links
                 FROM market_major_events e ORDER BY first_seen_at DESC,id DESC LIMIT 16"""
        ):
            item = dict(row)
            item["themes"] = _loads(item.get("themes"), [])
            item["affected_industries"] = _loads(item.get("affected_industries"), [])
            major_events.append(item)
        major_links = [dict(row) for row in conn.execute(
            """SELECT l.*,e.title,e.event_type,e.significance_score,e.first_seen_at
                 FROM market_event_candidate_links l JOIN market_major_events e ON e.id=l.event_id
                ORDER BY e.first_seen_at DESC,l.confidence DESC LIMIT 30"""
        )]
        sources = [dict(row) for row in conn.execute("SELECT * FROM news_source_reputation ORDER BY credibility_score DESC")]
        effectiveness = [dict(row) for row in conn.execute(
            "SELECT * FROM news_effectiveness ORDER BY samples DESC,event_type,evidence_grade,horizon LIMIT 30"
        )]
        latest = conn.execute("SELECT * FROM news_factor_versions ORDER BY id DESC LIMIT 1").fetchone()
        factor = dict(latest) if latest else None
        if factor:
            factor["gates"] = _loads(factor["gates"], {})
            factor["weights"] = _loads(factor["weights"], {})
        runs = []
        for row in conn.execute("SELECT * FROM news_learning_runs ORDER BY id DESC LIMIT 8"):
            item = dict(row); item["detail"] = _loads(item["detail"], {}); runs.append(item)
        return {
            "engine_version": ENGINE_VERSION,
            "mode": "shadow" if not factor or factor["status"] == "shadow" else "paper_micro_eligible",
            "policy": "先记录首次可见时间，再等待1/3/5个交易日结果；来源可信度与涨跌效果分开校准。",
            "authority": "仅模拟盘入场阈值，达到门禁后最大±0.005；不接券商、不直接下单、不自动放宽风控。",
            "collection_policy": {
                "times": ["08:15", "12:15", "18:45"],
                "candidate_scope": "持仓 + 待执行信号 + 策略前15 + 观察区16–30，跨账户去重",
                "snapshot_ttl": "两个交易日",
                "major_scope": "全市场重大政策、流动性、产业、地缘与系统性风险事件",
                "ordinary_full_market_news": False,
            },
            "pool": {"size": len(pool), "counts": pool_counts, "items": pool[:40]},
            "major_radar": {
                "events": major_events, "links": major_links,
                "authority": "单一来源重大事件只作上下文；必须经独立数据验证与门禁后才能影响模拟盘",
            },
            "totals": totals, "factor": factor, "sources": sources,
            "effectiveness": effectiveness, "events": events, "runs": runs,
        }
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# R27-B2C-5 —— news owner 的 typed fact contract（owner → research readiness）
# ---------------------------------------------------------------------------
#
# 本节只做一件事：把 owner **已经**存在的 durable 事实边界整理成一个 typed projection，
# 并发布一个**只包含 owner 真能产生的状态**的核验闭集。它刻意**不**新增核验列
# （DB migration = 0），也刻意**不**产生 verified。
#
# 为什么没有 verified —— 这是 R27-B2C-5 审计出来的硬事实，不是设计偏好：
# ``verification_status`` 在整个仓库里只有**一个** writer（``capture_major_events``），
# 它只写得出 ``"single_source_linked"`` / ``"unverified"`` 两个值，而这两个值都只回答
# "有没有 source_url"；没有任何 UPDATE、第二 writer 或多源复算路径能把一行升级成
# "已核验"。``news_events`` 连核验列都没有。于是：
#
#     CURRENT NEWS OWNER HAS NO VERIFIED STATE
#
# 发明一个 ``NEWS_OWNER_VERIFIED`` 就是伪造 provenance，所以本节拒绝这么做；等 owner
# 真的发布核验 writer 时，人工把它登记进下面的闭集即可。
#
# 三条不变量，本节全部代码都只为它们服务：
#
# 1. ``first_seen_at`` 是**唯一**的 historical availability authority。
#    ``published_at`` 只说明"来源声称事件何时发布"，**永远不能**把可用性提前 ——
#    否则 9/20 发布、9/21 才被摄取的事件会倒填进 as_of=9/20 的历史研究。
# 2. ``evidence_grade`` 是**来源可追溯性**分级（A 官方原文 / B 可定位披露聚合 /
#    C 带链接媒体 / D 无链接），不是核验结论，不能升级任何核验状态。
# 3. durable normalized 列是 ledger authority。``raw_payload`` 只是原始观察材料，
#    不得覆盖任何列值 —— 因此下面每个 SELECT 都显式列出列名，**从不**读 raw_payload。
#
# 本节**不**联网、不读时钟：typed read 只读 durable ledger。live fetch 只属于
# ``capture_events`` / ``capture_major_events`` 这两条 ingestion writer 路径。

NEWS_FACT_CONTRACT_VERSION = "news-fact-v1"

#: 两个 durable ledger 各自的 record kind。identity 的 namespace 就是它。
NEWS_RECORD_KIND_EVENT = "news_event"
NEWS_RECORD_KIND_MAJOR_EVENT = "market_major_event"

#: 固定顺序闭集（= 读取顺序），调用方不需要猜，deterministic fingerprint 也因此稳定。
NEWS_RECORD_KINDS = (
    NEWS_RECORD_KIND_EVENT,
    NEWS_RECORD_KIND_MAJOR_EVENT,
)

#: 本 owner 的核验结论闭集。**刻意只有三态，且没有 verified。**
#:
#: * :data:`NEWS_OWNER_SINGLE_SOURCE` —— 可追溯到**一条**来源（有 source_url 或
#:   article_id）。这是"单源可追溯"，**不是**核验通过：没有任何第二来源复算过它。
#: * :data:`NEWS_OWNER_UNVERIFIED` —— owner 记录的 ledger 状态**明确是未核验**
#:   （``market_major_events.verification_status == "unverified"`）。
#: * :data:`NEWS_OWNER_SOURCE_UNUSABLE` —— 来源**不可追溯**（既没有 source_url 也没有
#:   article_id），核验过程无从下手 → fail closed。
NEWS_OWNER_SINGLE_SOURCE = "single_source"
NEWS_OWNER_UNVERIFIED = "unverified"
NEWS_OWNER_SOURCE_UNUSABLE = "source_unusable"

NEWS_OWNER_VERIFICATION_STATUSES = (
    NEWS_OWNER_SINGLE_SOURCE,
    NEWS_OWNER_UNVERIFIED,
    NEWS_OWNER_SOURCE_UNUSABLE,
)

#: ``market_major_events.verification_status`` 的 **writer 闭集**（R27-B2C-5 审计所得）。
#:
#: 闭集的唯一来源是 ``capture_major_events`` 里那个内联表达式
#: ``"single_source_linked" if source_url else "unverified"``。这个元组是它的显式登记：
#: 未知值一律 hard error，绝不静默落进 ``unverified``。``test_news_fact_contract`` 会
#: 直接扫描 writer 源码，要求它与本元组逐字一致 —— 未来新增状态却没有更新映射时，
#: 那条测试必须变 RED。
NEWS_MAJOR_EVENT_WRITER_STATUSES = ("single_source_linked", "unverified")

#: ledger 状态词 → news owner 核验闭集。**完整表，不是 ``if/else`` 链。**
#: catch-all ``else`` 会把 owner 未来新增的状态静默归成一态 —— 那正是"非 owner 替
#: owner 决定它自己的词是什么意思"。
_NEWS_MAJOR_LEDGER_STATUS_TO_OWNER = {
    "single_source_linked": NEWS_OWNER_SINGLE_SOURCE,
    "unverified": NEWS_OWNER_UNVERIFIED,
}

#: 一次 typed read 的默认上限（在 PIT 过滤**之后**应用）。
NEWS_FACT_READ_LIMIT = 200


class NewsFactContractError(ValueError):
    """owner fact contract 的构造 / 读取被拒绝 —— fail closed。

    ``reason`` 是稳定 machine code，供调用方与测试依赖；文案本身不承载判定：

    * ``version_mismatch`` —— fact contract 版本不符；
    * ``unknown_record_kind`` —— record kind 不在闭集里；
    * ``unknown_owner_status`` —— 核验结论不在本 owner 的闭集里；
    * ``unknown_verification_status`` —— ledger 核验状态不在审计过的 writer 闭集里；
    * ``missing_first_seen_at`` / ``malformed_first_seen_at`` / ``naive_first_seen_at``
      —— PIT availability 不可证明（**不**回退 published_at / created_at / now）；
    * ``bad_as_of`` —— 历史读的 as_of 不是 ``YYYY-MM-DD``；
    * ``bad_record_kinds`` / ``bad_limit`` —— 读取参数非法；
    * ``ledger_unavailable`` —— durable ledger 不存在（**不**创建、**不**抓取）；
    * ``empty_fact_field`` —— 身份 / 指纹 / 标题 / 来源 / 事件类型 / grade 为空；
    * ``alien_code`` / ``field_not_a_finite_number`` / ``non_finite_significance``
      —— 字段形状与 record kind 不符；
    * ``malformed_themes`` / ``malformed_affected_industries`` —— major event 的
      normalized 列不是合法 JSON；
    * ``ledger_status_mismatch`` / ``traceability_status_mismatch`` —— 发布的核验结论
      与 durable 列不自洽。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = str(reason)
        text = self.reason if not detail else "%s: %s" % (self.reason, detail)
        super().__init__(text)


def news_owner_status_mapping_problems(mapping: Mapping, writer_statuses: Any) -> list[str]:
    """显式映射与 writer 闭集的**双向**一致性检查（纯函数）。

    单独抽成纯函数，是为了让"缺一个合法状态"与"多一个已不存在的状态"两个方向都能被
    **直接测到**，而不是只能靠改 owner 源码来验 —— 与
    ``ai_research_portfolio_adapter._portfolio_outcome_mapping_problems`` 同一手法。

    任何一处漂移都意味着"某个 ledger 状态的含义"已经不再由人工决定，而是由映射表的
    默认分支决定。
    """
    problems: list[str] = []
    missing = sorted(set(writer_statuses) - set(mapping))
    if missing:
        problems.append(
            f"writer 写得出但没有登记 owner 核验结论的 ledger 状态：{missing}"
            "（owner 不得替自己猜一个新状态的含义）"
        )
    unknown = sorted(set(mapping) - set(writer_statuses))
    if unknown:
        problems.append(
            f"映射里有 writer 已不再产生的 ledger 状态：{unknown}"
            "（说明这张表与审计过的 writer 闭集漂移了）"
        )
    return problems


def _major_event_owner_status(ledger_status: str) -> str:
    """``market_major_events.verification_status`` → news owner 核验闭集。

    **fail closed 是这里的关键性质。** 映射表必须与审计过的 writer 闭集精确相等：

    * writer 新增一个状态 → 表里查不到 → 抛 ``unknown_verification_status``；
    * 表里出现 writer 已不产生的状态 → 同样抛（说明这张表已经漂移）。

    刻意**不**缓存：漂移必须在**下一次调用**就 fail closed，而不是等进程重启。
    这也直接落实了两条禁止事项：``single_source_linked`` 不得映射成 verified；
    未知状态不得默认成 unverified。
    """
    problems = news_owner_status_mapping_problems(
        _NEWS_MAJOR_LEDGER_STATUS_TO_OWNER, NEWS_MAJOR_EVENT_WRITER_STATUSES,
    )
    if problems:
        raise NewsFactContractError(
            "unknown_verification_status",
            "ledger 状态词表与显式映射漂移：" + "; ".join(problems),
        )
    mapped = _NEWS_MAJOR_LEDGER_STATUS_TO_OWNER.get(str(ledger_status))
    if mapped is None:
        raise NewsFactContractError(
            "unknown_verification_status",
            f"{ledger_status!r} 不在审计过的 writer 闭集 "
            f"{NEWS_MAJOR_EVENT_WRITER_STATUSES} 里",
        )
    return mapped


def _traceable(source_url: Any, article_id: Any) -> bool:
    """这一行能不能被追溯回它声称的来源（有链接或有文章 id）。"""
    return bool(str(source_url or "").strip() or str(article_id or "").strip())


def _news_event_owner_status(source_url: Any, article_id: Any) -> str:
    """``news_events`` 行的 owner 核验结论 —— 由 durable 列确定性派生。

    ``news_events`` **没有**核验列，因此本 owner 只发布它能从 durable 列证明的结论：
    这一行能不能被追溯到它声称的那条来源。刻意**不**读 ``evidence_grade`` —— grade 是
    可追溯性分级，把它当核验就是把"抓取方式"冒充"事实核验"。
    """
    if _traceable(source_url, article_id):
        return NEWS_OWNER_SINGLE_SOURCE
    return NEWS_OWNER_SOURCE_UNUSABLE


def _parse_owner_instant(value: Any, *, what: str) -> dt.datetime:
    """解析一个 owner 记录的 instant；**必须**带可证明的时区。

    缺失 / 无法解析 / naive 一律 fail closed。这里**没有**任何 fallback：
    不回退 ``published_at``、不回退 ``created_at``、不回退 ``now()`` —— PIT 不可证明的
    事实不得进入研究链路。

    返回的 instant **归一到 owner 时区**（``TZ``）。row 里的文本允许带任意 offset，而本
    owner 的日历语义只有一个：read 的日边界是 ``as_of + 23:59:59+08:00``。若这里原样返回
    原始 offset，``availability_day`` 就会改用另一套日期口径 —— 一个跨过 UTC 午夜的
    instant（``2026-09-20T16:30+00:00`` 在上海已经是 9/21 00:30）会让 projection 声明一个
    **比真实可用日更早**的业务日，于是 ``InformationEvent`` 的 look-ahead guard 被绕过。
    绝对时刻的比较不受此影响（aware 比较与时区无关），受影响的是**由它派生的日历日**。
    """
    raw = str(value or "").strip()
    if not raw:
        raise NewsFactContractError("missing_first_seen_at", f"{what} is empty")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise NewsFactContractError(
            "malformed_first_seen_at", f"{what}={raw!r}",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NewsFactContractError(
            "naive_first_seen_at",
            f"{what}={raw!r} 没有可证明的时区语义 —— 不得假设本机时区",
        )
    return parsed.astimezone(TZ)


def _as_of_boundary(as_of: Any) -> dt.datetime:
    """``'YYYY-MM-DD'`` → 该日历日在 owner 时区的**日末**边界。

    与既有的 :func:`code_overlay` 约定一致（``asof + "T23:59:59+08:00"``）：``as_of``
    是一个**日历日**，因此"当日系统已知"= ``first_seen_at <= 当日 23:59:59``。

    刻意**只**接受 ``YYYY-MM-DD``：一个带时刻的 as_of 会让人以为边界是那个时刻，而
    research contract 的 ``as_of`` 本身就是业务日（``YYYY-MM-DD``）。
    """
    text = str(as_of or "").strip()
    try:
        day = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise NewsFactContractError(
            "bad_as_of", f"{as_of!r} 不是 YYYY-MM-DD",
        ) from exc
    if day.isoformat() != text:
        raise NewsFactContractError("bad_as_of", f"{as_of!r} 不是规范日期")
    return dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=TZ)


def _validated_record_kinds(record_kinds: Any) -> tuple[str, ...]:
    """校验 record kind 选择器：非空子集，并按 owner 的固定顺序返回。"""
    try:
        requested = set(record_kinds)
    except TypeError as exc:
        raise NewsFactContractError("bad_record_kinds", repr(record_kinds)) from exc
    if not requested:
        raise NewsFactContractError("bad_record_kinds", "至少需要一个 record kind")
    unknown = sorted(requested - set(NEWS_RECORD_KINDS))
    if unknown:
        raise NewsFactContractError("bad_record_kinds", f"未知 record kind：{unknown}")
    return tuple(kind for kind in NEWS_RECORD_KINDS if kind in requested)


def _validated_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise NewsFactContractError("bad_limit", repr(limit))
    return int(limit)


def _text_or_none(value: Any) -> str | None:
    """列值 → 非空文本或 ``None``；刻意**不**把空白列升级成占位符。"""
    text = str(value if value is not None else "").strip()
    return text or None


def _loads_list(value: Any, *, what: str) -> list:
    """严格解析一个 normalized JSON 列（``themes`` / ``affected_industries``）。

    不清空、不吞异常：owner 自己用 ``_json()`` 写这两列，畸形值意味着 ledger 已被
    非 owner 写入 —— 那必须 fail closed，而不是当成空列表静默降级。
    """
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError) as exc:
        raise NewsFactContractError(
            what, repr(value)[:120],
        ) from exc
    if not isinstance(parsed, list):
        raise NewsFactContractError(what, f"expected a JSON list, got {type(parsed).__name__}")
    return parsed


def _theme_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    """``themes`` JSON → 不可变 ``((id, label), ...)``，**保留 ledger 里的顺序**。

    不做排序、不做去重：durable 列是 authority，本层只把它变成不可变形状，好让
    fingerprint 与比较不依赖可变对象。
    """
    pairs: list[tuple[str, str]] = []
    for item in _loads_list(value, what="malformed_themes"):
        if isinstance(item, Mapping):
            pairs.append((str(item.get("id") or ""), str(item.get("label") or "")))
        else:
            pairs.append((str(item), ""))
    return tuple(pairs)


def _industry_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in _loads_list(value, what="malformed_affected_industries"))


@dataclass(frozen=True, slots=True, init=False)
class NewsFactProjection:
    """news owner 对**一条 durable 事件事实**的正式投影。

    research 层引用一条 news 事实时读到的就是它：fact contract 版本、record kind、
    owner 自己的身份（``event_key``）、PIT 可用日、来源身份、以及 owner 自己的核验结论。

    它**不**包含 research 语义，**不**携带 ``evidence_grade`` 之外的任何"可信度"表面，
    也**不**包含 source reputation / candidate-link confidence —— 那两个问题不属于
    "这条事件是否被核验"。

    **没有公开 raw 构造器。** ``NewsFactProjection(...)`` 一律抛 ``TypeError``；唯一签发
    路径是 :func:`news_fact_projections`（它再走私有
    :func:`_issue_news_fact_projection`）。否则任何调用方都能自述
    ``identity`` / ``first_seen_at`` / ``owner_verification_status`` 然后声称"这是一条
    news owner 事实" —— "owner 派生身份"就只是一句声明。

    四条结构性规则（构造期强制，因此无法表达自相矛盾的投影）：

    * ``first_seen_at`` 必须可解析且带时区；``availability_day`` 由它派生，**不**单独传入；
    * ``availability_day`` 与 ``first_seen_at`` 是**两个**字段：前者是业务日，后者是
      exact timestamp，二者不得互换；
    * ``record_kind`` 决定哪些字段适用 —— 不适用的一律是 ``None`` / 空元组，
      **不补假值**；
    * 发布的核验结论必须与 durable 列自洽：major event 必须等于 ledger 状态词的映射，
      news event 必须等于可追溯性判定。
    """

    version: str
    record_kind: str
    identity: str
    canonical_hash: str
    code: str | None
    title: str
    source_name: str
    source_type: str
    source_url: str | None
    article_id: str | None
    published_at: str | None
    first_seen_at: str
    availability_day: str
    event_type: str
    evidence_grade: str
    owner_verification_status: str
    ledger_verification_status: str | None
    significance_score: float | None
    themes: tuple[tuple[str, str], ...]
    affected_industries: tuple[str, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            "NewsFactProjection has no public constructor: 调用方不能自述一条 news 事实的"
            "身份 / PIT / 核验结论。请使用 "
            "news_learning.news_fact_projections(conn, as_of=...) —— 全部字段由 news owner "
            "从它自己的 durable ledger 行派生"
        )

    def __post_init__(self) -> None:
        if self.version != NEWS_FACT_CONTRACT_VERSION:
            raise NewsFactContractError(
                "version_mismatch", f"{self.version!r} != {NEWS_FACT_CONTRACT_VERSION!r}",
            )
        if self.record_kind not in NEWS_RECORD_KINDS:
            raise NewsFactContractError("unknown_record_kind", repr(self.record_kind))
        for name in ("identity", "canonical_hash", "title", "source_name",
                     "source_type", "event_type", "evidence_grade"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise NewsFactContractError("empty_fact_field", f"{name} 为空")
            object.__setattr__(self, name, value)
        if self.owner_verification_status not in NEWS_OWNER_VERIFICATION_STATUSES:
            raise NewsFactContractError(
                "unknown_owner_status", repr(self.owner_verification_status),
            )
        object.__setattr__(self, "source_url", _text_or_none(self.source_url))
        object.__setattr__(self, "article_id", _text_or_none(self.article_id))
        object.__setattr__(self, "published_at", _text_or_none(self.published_at))

        # PIT：availability 只由 first_seen_at 派生。published_at 在这里只被当作描述性
        # 文本保存，**从不**参与边界计算。业务日按 owner 时区算（见
        # :func:`_parse_owner_instant`）—— 与 read 的 ``as_of + 23:59:59+08:00`` 日边界
        # 是**同一套**日历口径。
        instant = _parse_owner_instant(self.first_seen_at, what="first_seen_at")
        object.__setattr__(self, "availability_day", instant.date().isoformat())

        if self.record_kind == NEWS_RECORD_KIND_EVENT:
            if self.significance_score is not None:
                raise NewsFactContractError(
                    "field_not_a_finite_number",
                    "news_event 没有 significance_score —— 不补假值",
                )
            if self.themes or self.affected_industries:
                raise NewsFactContractError(
                    "malformed_themes", "news_event 没有 themes / affected_industries",
                )
            if self.ledger_verification_status is not None:
                raise NewsFactContractError(
                    "ledger_status_mismatch",
                    "news_events 没有 verification_status 列 —— 不得伪造一个",
                )
            code = str(self.code or "").strip()
            if len(code) != 6 or not code.isdigit():
                raise NewsFactContractError("alien_code", repr(self.code))
            object.__setattr__(self, "code", code)
        else:
            if self.code is not None:
                raise NewsFactContractError(
                    "alien_code", "market_major_event 是市场级的，没有 code —— 不补假值",
                )
            score = self.significance_score
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise NewsFactContractError(
                    "field_not_a_finite_number", repr(score),
                )
            number = float(score)
            if not math.isfinite(number):
                raise NewsFactContractError("non_finite_significance", repr(score))
            object.__setattr__(self, "significance_score", number)
            # 未知的 ledger 状态词在这里**没有**第二个检查点：闭集合法性只有
            # :func:`_major_event_owner_status` 一处判定（它自己就会抛
            # ``unknown_verification_status``）。刻意不重复一遍 —— 两处等价的 fail-closed
            # 会互相掩盖，使"某个状态被静默默认"这类退化逃过一次单点变异。
            status = str(self.ledger_verification_status or "")
            object.__setattr__(self, "ledger_verification_status", status)
            expected = _major_event_owner_status(status)
            if self.owner_verification_status != expected:
                raise NewsFactContractError(
                    "ledger_status_mismatch",
                    f"ledger 状态 {status!r} 只能对应 {expected!r}，"
                    f"但投影发布的是 {self.owner_verification_status!r}",
                )
        if self.record_kind == NEWS_RECORD_KIND_EVENT:
            expected = _news_event_owner_status(self.source_url, self.article_id)
            if self.owner_verification_status != expected:
                raise NewsFactContractError(
                    "traceability_status_mismatch",
                    f"可追溯性只能对应 {expected!r}，"
                    f"但投影发布的是 {self.owner_verification_status!r}",
                )
        object.__setattr__(self, "themes", tuple(self.themes))
        object.__setattr__(self, "affected_industries", tuple(self.affected_industries))

    @property
    def is_owner_verified(self) -> bool:
        """owner 是否判定这条事实**通过**了核验。

        今天是**恒 False**：本 owner 没有 verified 状态（见本节开头）。保留这个只读属性
        是为了让"news 事实从不自称已核验"成为可断言的形状，而不是靠调用方记得。
        """
        return False

    def as_dict(self) -> Mapping:
        """本投影的 canonical 形状（fingerprint / 审计共用）。"""
        return {
            "version": self.version,
            "record_kind": self.record_kind,
            "identity": self.identity,
            "canonical_hash": self.canonical_hash,
            "code": self.code,
            "title": self.title,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "article_id": self.article_id,
            "published_at": self.published_at,
            "first_seen_at": self.first_seen_at,
            "availability_day": self.availability_day,
            "event_type": self.event_type,
            "evidence_grade": self.evidence_grade,
            "owner_verification_status": self.owner_verification_status,
            "ledger_verification_status": self.ledger_verification_status,
            "significance_score": self.significance_score,
            "themes": [list(item) for item in self.themes],
            "affected_industries": list(self.affected_industries),
        }


def _issue_news_fact_projection(**fields: Any) -> NewsFactProjection:
    """签发一条 owner fact 投影。只有本模块的 :func:`_news_fact_row` 调用它。

    绕过恒抛错的 ``__init__`` 并在设置完全部字段后跑 ``__post_init__``，使校验逻辑仍然
    只有一份、且紧挨字段定义（与 ``paper_portfolio_read_model._issue_portfolio_fact_projection``
    及 ``ai_research_contract._issue_evidence_ref`` 同一手法）。
    """
    projection = object.__new__(NewsFactProjection)
    for name, value in fields.items():
        object.__setattr__(projection, name, value)
    projection.__post_init__()
    return projection


#: 显式列清单 —— 刻意列出而不是 ``SELECT *``：``raw_payload`` **不得**成为 authority，
#: 因此它根本不出现在读路径里。
#:
#: ``created_at`` 被列出，但它**不是**事实成分、也不进入投影：列出它只为让"created_at
#: 永远不能成为 availability authority"这条不变量**可被 mutation 证明** —— 一条关于某列的
#: 禁令只有在那一列可见的地方才可证伪，否则"created_at fallback"这种写法在源码里根本
#: 无法表达（于是它测出来的只会是一次接线错误，是假杀而不是证据）。
_NEWS_FACT_EVENT_COLUMNS = (
    "event_key,canonical_hash,article_id,code,title,source_name,source_type,source_url,"
    "evidence_grade,published_at,first_seen_at,event_type,created_at"
)
_NEWS_FACT_MAJOR_COLUMNS = (
    "event_key,canonical_hash,article_id,title,summary,source_name,source_type,source_url,"
    "evidence_grade,published_at,first_seen_at,event_type,significance_score,themes,"
    "affected_industries,verification_status,created_at"
)

_NEWS_FACT_READ_SQL = {
    NEWS_RECORD_KIND_EVENT: f"SELECT {_NEWS_FACT_EVENT_COLUMNS} FROM news_events",
    NEWS_RECORD_KIND_MAJOR_EVENT: (
        f"SELECT {_NEWS_FACT_MAJOR_COLUMNS} FROM market_major_events"
    ),
}


def _news_fact_row(kind: str, row: Any) -> NewsFactProjection:
    """一条 durable 行 → 一条 typed 投影。字段全部来自 normalized 列。"""
    if kind == NEWS_RECORD_KIND_EVENT:
        return _issue_news_fact_projection(
            version=NEWS_FACT_CONTRACT_VERSION,
            record_kind=kind,
            identity=row["event_key"],
            canonical_hash=row["canonical_hash"],
            code=row["code"],
            title=row["title"],
            source_name=row["source_name"],
            source_type=row["source_type"],
            source_url=row["source_url"],
            article_id=row["article_id"],
            published_at=row["published_at"],
            first_seen_at=row["first_seen_at"],
            availability_day="",
            event_type=row["event_type"],
            evidence_grade=row["evidence_grade"],
            owner_verification_status=_news_event_owner_status(
                row["source_url"], row["article_id"],
            ),
            ledger_verification_status=None,
            significance_score=None,
            themes=(),
            affected_industries=(),
        )
    return _issue_news_fact_projection(
        version=NEWS_FACT_CONTRACT_VERSION,
        record_kind=kind,
        identity=row["event_key"],
        canonical_hash=row["canonical_hash"],
        code=None,
        title=row["title"],
        source_name=row["source_name"],
        source_type=row["source_type"],
        source_url=row["source_url"],
        article_id=row["article_id"],
        published_at=row["published_at"],
        first_seen_at=row["first_seen_at"],
        availability_day="",
        event_type=row["event_type"],
        evidence_grade=row["evidence_grade"],
        owner_verification_status=_major_event_owner_status(row["verification_status"]),
        ledger_verification_status=row["verification_status"],
        significance_score=row["significance_score"],
        themes=_theme_pairs(row["themes"]),
        affected_industries=_industry_tuple(row["affected_industries"]),
    )


def news_fact_projections(
    conn, *, as_of: Any, record_kinds: Any = NEWS_RECORD_KINDS, limit: Any = NEWS_FACT_READ_LIMIT,
) -> tuple[NewsFactProjection, ...]:
    """news owner 的**唯一** public typed 历史读入口（R27-B2C-5）。

    ``as_of`` 是**必填的关键字参数**，而且刻意**没有默认值**：一个能同时表示"当前"与
    "历史某日"的双义接口后面很难审。本函数只回答"到 ``as_of`` 这个日历日为止，系统
    **当时**已经观测到哪些事件"：

    ```text
    保留条件：  first_seen_at <= as_of 当日 23:59:59（owner 时区）
    排除条件：  first_seen_at >  as_of        ⇒ 那时系统还不知道它
    ```

    **``published_at`` 不参与边界计算。** 一条 ``published_at=9/20`` 但
    ``first_seen_at=9/21`` 的事件，在 ``as_of=9/20`` 必须不可见 —— 否则就是把 9/21 才
    摄取到的数据倒填进历史。

    **不联网、不写库。** 本函数只发 SELECT，而且刻意**不**调用 :func:`ensure_schema`
    （那会写 DDL）：ledger 不存在时 fail closed 成 ``ledger_unavailable``，**不**创建表、
    **不**回退去抓 live news、**不**做 backfill。历史缺数据就是"当时没有证据"，
    不是"现在补一条再假装当时知道"。

    刻意**没有** ``latest`` / ``recent`` / ``today`` / ``now`` 这类隐式回退入口。
    需要当前视图请读 :func:`overview`（它是展示层，不是 evidence）。

    边界比较在 Python 侧逐行完成，而不是把 ISO 文本交给 SQL 的字典序：owner 不能假设
    每一行的 offset 都和 owner 一样，那等于把 PIT 交给一个没被证明的假设。畸形 / naive
    的 ``first_seen_at`` 使整次读取 fail closed —— 因为"跳过这一行"就是静默降级。
    """
    kinds = _validated_record_kinds(record_kinds)
    boundary = _as_of_boundary(as_of)
    bound = _validated_limit(limit)

    rows: list[tuple[str, Any]] = []
    for kind in kinds:
        try:
            rows.extend((kind, row) for row in conn.execute(_NEWS_FACT_READ_SQL[kind]))
        except sqlite3.OperationalError as exc:
            raise NewsFactContractError(
                "ledger_unavailable",
                f"{kind} 的 durable ledger 不可读：{exc}",
            ) from exc

    available: list[tuple[dt.datetime, str, NewsFactProjection]] = []
    for kind, row in rows:
        instant = _parse_owner_instant(row["first_seen_at"], what="first_seen_at")
        if instant > boundary:
            continue
        projection = _news_fact_row(kind, row)
        available.append((instant, kind, projection))
    # 固定顺序：先按 owner 记录的时刻，再按 record kind，最后按 owner identity。
    available.sort(key=lambda item: (item[0], item[1], item[2].identity))
    return tuple(item[2] for item in available[:bound])
