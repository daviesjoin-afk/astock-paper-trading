# -*- coding: utf-8 -*-
"""「策略选股」：模拟盘 5 套策略的盘后自动选股与持久化。

设计要点（与需求一一对应）
--------------------------
1. **策略来源**：完全取自模拟盘账户注册表 ``strategy_registry``（5 套），
   与「策略模拟」页同源；每个策略带编号（策略1…策略5）与名称。
2. **评分选股**：复用既有选股因子链 ``main._select_uncached``（同一套
   覆盖率/行情新鲜度/买卖范围门禁），每个策略按其模型族评分取 Top N
   （默认 5，候选不足则少于 5 只并给出空状态）。
3. **分组归属**：结果按策略分组存储，唯一键为 ``(trade_date, strategy_id)``。
   同一只股票被多个策略同时选中时**分别归属各策略、不去重、不合并**。
4. **调度**：交易日盘后由 ``paper_selection_runner.py`` 触发（cron），
   重跑按 ``(交易日, 策略编号)`` 覆盖当天结果（DELETE 后 INSERT，同一事务）。

本模块只做研究选股，不生成订单、不改动模拟盘账本。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

import data_fetcher as dfc
import strategy_registry as SR

DB_PATH = os.path.join(dfc.CACHE_DIR, "selection_tracking.db")
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_TOPN = 5
MAX_TOPN = 20

# 模拟盘账户 → 选股模型族（strategies.run_strategy 的策略 id）。
# reported_profit_breakout / main_force_top10 使用各自复合评分实现，
# 因此直接映射到同名策略 id。
STRATEGY_MODEL = {
    "tq_breakout": "one_to_two",
    "trend_pullback": "bottom_reversal",
    "sector_rotation": "sentiment_pioneer",
    "reported_profit_breakout": "reported_profit_breakout",
    "main_force_top10": "main_force_top10",
}


def _today():
    return dt.datetime.now(CHINA_TZ).date()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def catalog():
    """返回 5 套策略的编号/名称/模型族（顺序与注册表一致）。"""
    labels = SR.labels()
    items = []
    for index, spec_id in enumerate(SR.active_ids(), start=1):
        items.append({
            "no": index,
            "label": f"策略{index}",
            "strategy_id": spec_id,
            "strategy_name": labels.get(spec_id, spec_id),
            "model_id": STRATEGY_MODEL.get(spec_id, spec_id),
        })
    return items


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_selection_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_no INTEGER NOT NULL,
            strategy_name TEXT NOT NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL,               -- ok|empty|blocked|error
            message TEXT,
            factor_date TEXT,
            topn INTEGER NOT NULL,
            source TEXT NOT NULL,               -- scheduled|manual
            created_at TEXT NOT NULL,
            UNIQUE(trade_date, strategy_id)
        );
        CREATE TABLE IF NOT EXISTS paper_selection_picks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            rank_no INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            industry TEXT,
            price REAL,
            pct REAL,
            score REAL,
            super_net REAL,
            reasons TEXT,
            news_status TEXT,
            UNIQUE(trade_date, strategy_id, rank_no)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_selection_picks_day
            ON paper_selection_picks(trade_date, strategy_id);
        """
    )


def _run_one(model_id: str, topn: int):
    """调用既有选股流水线（含覆盖率与行情新鲜度门禁）。"""
    import main as M
    return M._select_uncached(strategy=model_id, topn=topn)


_DATE_KEYS = ("historical_factor_date", "trade_date", "reference_date", "complete_cutoff")


def _trade_date_of(result):
    """从选股返回里解析因子基准日（即本次选股对应的交易日）。

    真实的 ``main._select_uncached`` 并不会在顶层放日期：它把
    ``historical_factor_date`` 写到每只 pick 上，把 ``reference_date`` /
    ``complete_cutoff`` 放在 ``data_quality`` 里。因此按
    顶层 → data_quality → pick 逐级回退，保证落库时不会丢数据来源。
    """
    if not isinstance(result, dict):
        return ""
    sources = [result]
    quality = result.get("data_quality")
    if isinstance(quality, dict):
        sources.append(quality)
    sources.extend(p for p in (result.get("picks") or [])[:1] if isinstance(p, dict))
    for source in sources:
        for key in _DATE_KEYS:
            value = str(source.get(key) or "").strip()
            if value:
                return value[:10]
    return ""


def run_daily(strategies=None, topn: int = DEFAULT_TOPN, run_date: str | None = None,
              source: str = "scheduled") -> dict:
    """为每套策略跑一次评分选股，并覆盖写入当天结果。

    返回 ``{trade_date, topn, strategies: [...]}``，每项含 status/picks；
    ``status`` ∈ ok / empty（候选不足）/ blocked（数据门禁未通过）/ error。
    """
    topn = max(1, min(int(topn), MAX_TOPN))
    trade_date = str(run_date or _today().isoformat())[:10]
    items = catalog()
    if strategies:
        wanted = {str(item) for item in strategies}
        items = [item for item in items
                 if item["strategy_id"] in wanted or str(item["no"]) in wanted]
    conn = _connect()
    try:
        ensure_schema(conn)
        summary = []
        for item in items:
            now = dt.datetime.now(CHINA_TZ).isoformat(timespec="seconds")
            status, message, picks, factor_date = "error", "", [], ""
            try:
                result = _run_one(item["model_id"], topn)
                if isinstance(result, dict) and result.get("need_init"):
                    status = "blocked"
                    message = str(result.get("message") or "选股数据尚未就绪")
                    factor_date = str(result.get("factor_date") or "")
                else:
                    raw = (result or {}).get("picks") or []
                    factor_date = _trade_date_of(result)
                    for index, pick in enumerate(raw[:topn], start=1):
                        news_check = pick.get("news_check") or {}
                        picks.append({
                            "rank_no": index,
                            "code": str(pick.get("code") or ""),
                            "name": pick.get("name"),
                            "industry": pick.get("industry"),
                            "price": pick.get("price"),
                            "pct": pick.get("pct"),
                            "score": pick.get("score"),
                            "super_net": pick.get("super_net"),
                            "reasons": pick.get("reasons") or [],
                            "news_status": news_check.get("status"),
                        })
                    status = "ok" if picks else "empty"
                    if not picks:
                        message = "当日无通过门禁的候选"
            except Exception as exc:
                status = "error"
                message = f"{type(exc).__name__}: {exc}"[:300]

            # 覆盖语义：同一 (交易日, 策略编号) 先删后写，重跑不留历史副本。
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM paper_selection_runs WHERE trade_date=? AND strategy_id=?",
                (trade_date, item["strategy_id"]),
            )
            conn.execute(
                "DELETE FROM paper_selection_picks WHERE trade_date=? AND strategy_id=?",
                (trade_date, item["strategy_id"]),
            )
            conn.execute(
                """INSERT INTO paper_selection_runs(trade_date,strategy_id,strategy_no,
                   strategy_name,model_id,status,message,factor_date,topn,source,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (trade_date, item["strategy_id"], item["no"], item["strategy_name"],
                 item["model_id"], status, message, factor_date, topn, source, now),
            )
            for pick in picks:
                conn.execute(
                    """INSERT INTO paper_selection_picks(trade_date,strategy_id,rank_no,code,
                       name,industry,price,pct,score,super_net,reasons,news_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (trade_date, item["strategy_id"], pick["rank_no"], pick["code"],
                     pick["name"], pick["industry"], pick["price"], pick["pct"],
                     pick["score"], pick["super_net"], json.dumps(pick["reasons"],
                     ensure_ascii=False), pick["news_status"]),
                )
            conn.commit()
            summary.append({**item, "status": status, "message": message,
                            "factor_date": factor_date, "picks": picks})
        return {"trade_date": trade_date, "topn": topn, "source": source,
                "strategies": summary}
    finally:
        conn.close()


def _latest_trade_date(conn):
    row = conn.execute(
        "SELECT trade_date FROM paper_selection_runs ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    return row["trade_date"] if row else None


def latest(trade_date: str | None = None, strategy_id: str = "") -> dict:
    """读取某交易日（默认最近一次运行日）的分组选股结果。"""
    conn = _connect()
    try:
        ensure_schema(conn)
        day = str(trade_date or "").strip() or _latest_trade_date(conn)
        if not day:
            return {"found": False, "trade_date": None, "strategies": []}
        runs = {
            row["strategy_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM paper_selection_runs WHERE trade_date=? ORDER BY strategy_no",
                (day,)).fetchall()
        }
        picks_rows = conn.execute(
            "SELECT * FROM paper_selection_picks WHERE trade_date=? "
            "ORDER BY strategy_id, rank_no", (day,)).fetchall()
    finally:
        conn.close()
    grouped = {}
    for row in picks_rows:
        grouped.setdefault(row["strategy_id"], []).append({
            "rank_no": row["rank_no"],
            "code": row["code"],
            "name": row["name"],
            "industry": row["industry"],
            "price": row["price"],
            "pct": row["pct"],
            "score": row["score"],
            "super_net": row["super_net"],
            "reasons": json.loads(row["reasons"] or "[]"),
            "news_status": row["news_status"],
        })
    strategies = []
    for item in catalog():
        if strategy_id and item["strategy_id"] != strategy_id:
            continue
        run = runs.get(item["strategy_id"]) or {}
        strategies.append({
            **item,
            "status": run.get("status") or "not_run",
            "message": run.get("message") or "",
            "factor_date": run.get("factor_date") or "",
            "updated_at": run.get("created_at") or None,
            "picks": grouped.get(item["strategy_id"], []),
        })
    return {"found": True, "trade_date": day, "strategies": strategies}
