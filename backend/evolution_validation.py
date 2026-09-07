# -*- coding: utf-8 -*-
"""自进化验证层（B 批）：A/B 对照表 + 观察期门禁。

两个职责：

1. ``record_ab_snapshots``（每日收盘学习调用）：对所有生效中的进化部署
   （风控版本 / 选股覆盖 / 资金分摊 / AI 调参覆盖）计算部署后观察窗口内的
   账户净值表现 vs 部署前等长基线窗口，写入 ``adaptive_ab_tests`` 对照表。
   观察不满 ``MIN_OBSERVATION_DAYS`` 个净值日只记录 verdict='observing'，
   不下结论——不使用未来数据，也不对短窗口装作有统计意义。

2. ``pre_apply_gate``（A 批新通道 apply_allocation / apply_tuner_proposals
   的前置门禁）：同一账户同一通道的上一次覆盖仍处于观察期（< 5 个净值日）
   时拒绝新覆盖，防止连续覆盖导致效果无法归因。既有风控/选股进化通道已有
   自己的分层影子门禁（1日观测/3日小步/5日标准/10日成熟），此处不重复拦截。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from adaptive_common import _json, _loads, _now

TZ = ZoneInfo("Asia/Shanghai")
MIN_OBSERVATION_DAYS = 5
_BASELINE_WINDOW_DAYS = 5


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adaptive_ab_tests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            kind TEXT NOT NULL,             -- risk|selection|allocation|tuner
            version TEXT,
            candidate_id INTEGER,
            deployed_at TEXT,
            observation_days INTEGER,
            baseline_return_pct REAL,       -- 部署前等长窗口账户收益
            deployment_return_pct REAL,     -- 部署后窗口账户收益
            excess_pct REAL,                -- 部署后 - 部署前
            verdict TEXT NOT NULL,          -- observing|pass|fail
            detail TEXT,
            created_at TEXT NOT NULL)
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_adaptive_ab_recent "
        "ON adaptive_ab_tests(account_id,kind,id DESC)")


def _paper_connect(paper_db_path):
    conn = sqlite3.connect(paper_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _nav_series(paper, account_id):
    rows = paper.execute(
        "SELECT nav_date,nav FROM paper_nav WHERE account_id=? ORDER BY nav_date",
        (account_id,)).fetchall()
    return [(str(r["nav_date"]), float(r["nav"] or 0.0)) for r in rows]


def _window_return(series, end_date: str, days: int):
    """截至 end_date（含）往前 days 个净值日的窗口收益；样本不足返回 None。"""
    upto = [(d, v) for d, v in series if d <= end_date]
    if len(upto) < days + 1:
        return None
    end_val = upto[-1][1]
    start_val = upto[-1 - days][1]
    if start_val <= 0:
        return None
    return (end_val / start_val - 1.0) * 100.0


def _active_deployments(paper, adaptive_conn):
    """汇总各通道当前生效的部署（version/candidate/deployed_at）。"""
    deployments = []
    today = dt.datetime.now(TZ).date().isoformat()
    # 通道1：A 批两种 params 覆盖（allocation / tuner 共用 adaptive_selection 键）
    for row in paper.execute("SELECT id,params FROM paper_accounts").fetchall():
        params = _loads(row["params"], {}) or {}
        alloc = params.get("adaptive_allocation") or {}
        if alloc.get("status") == "active" and str(alloc.get("effective_date") or "") <= today:
            deployments.append({
                "account_id": row["id"], "kind": "allocation",
                "version": f"decision-{alloc.get('decision_id')}",
                "candidate_id": alloc.get("decision_id"),
                "deployed_at": alloc.get("applied_at"),
            })
        meta = params.get("adaptive_selection_meta") or {}
        overlay = params.get("adaptive_selection") or {}
        if (overlay.get("weights") and str(meta.get("status") or "") == "active"
                and str(meta.get("effective_date") or "") <= today):
            deployments.append({
                "account_id": row["id"],
                "kind": "tuner" if str(meta.get("tier") or "") == "llm_consensus" else "selection",
                "version": str(meta.get("version") or ""),
                "candidate_id": meta.get("candidate_id") or meta.get("run_id"),
                "deployed_at": meta.get("applied_at"),
            })
    # 通道2：风控版本部署表（由 risk_evolution 维护，缺表/未提供时静默跳过）
    if adaptive_conn is None:
        return deployments
    try:
        rows = adaptive_conn.execute(
            "SELECT account_id,version,candidate_id,deployed_at FROM adaptive_risk_deployments "
            "WHERE rolled_back_at IS NULL"
        ).fetchall()
        for row in rows:
            deployments.append({
                "account_id": row["account_id"], "kind": "risk",
                "version": row["version"], "candidate_id": row["candidate_id"],
                "deployed_at": row["deployed_at"],
            })
    except sqlite3.Error:
        pass
    return deployments


def record_ab_snapshots(adaptive_connect, paper_db_path) -> dict:
    """对每个生效部署记录一条当日 A/B 快照（幂等：同部署同日只记一条）。"""
    now = _now()
    today = dt.datetime.now(TZ).date().isoformat()
    recorded = 0
    with adaptive_connect() as conn:
        ensure_schema(conn)
        paper = _paper_connect(paper_db_path)
        try:
            deployments = _active_deployments(paper, conn)
            navs = {}
            for account_id in {d["account_id"] for d in deployments}:
                navs[account_id] = _nav_series(paper, account_id)
        finally:
            paper.close()
        for dep in deployments:
            exists = conn.execute(
                "SELECT 1 FROM adaptive_ab_tests WHERE account_id=? AND kind=? "
                "AND COALESCE(version,'')=COALESCE(?,'') AND deployed_at IS ? "
                "AND substr(created_at,1,10)=?",
                (dep["account_id"], dep["kind"], dep.get("version"),
                 dep.get("deployed_at"), today),
            ).fetchone()
            if exists:
                continue
            series = navs.get(dep["account_id"]) or []
            deployed_date = str(dep.get("deployed_at") or today)[:10]
            obs_days = len([d for d, _ in series if d > deployed_date])
            deployment_return = _window_return(series, today, MIN_OBSERVATION_DAYS) \
                if obs_days >= MIN_OBSERVATION_DAYS else None
            baseline_return = None
            if deployment_return is not None:
                baseline_return = _window_return(
                    series, deployed_date, _BASELINE_WINDOW_DAYS)
            if deployment_return is None or baseline_return is None:
                verdict = "observing"
                excess = None
            else:
                excess = round(deployment_return - baseline_return, 4)
                verdict = "pass" if excess >= 0 else "fail"
            conn.execute(
                """INSERT INTO adaptive_ab_tests(account_id,kind,version,candidate_id,
                   deployed_at,observation_days,baseline_return_pct,deployment_return_pct,
                   excess_pct,verdict,detail,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dep["account_id"], dep["kind"], dep.get("version"),
                 dep.get("candidate_id"), dep.get("deployed_at"), obs_days,
                 baseline_return, deployment_return, excess, verdict,
                 _json({"min_observation_days": MIN_OBSERVATION_DAYS,
                        "baseline_window_days": _BASELINE_WINDOW_DAYS}), now),
            )
            recorded += 1
    return {"deployments": len(deployments), "recorded": recorded}


def pre_apply_gate(adaptive_connect, paper_db_path, account_id: str, kind: str) -> None:
    """A 批新通道 apply 前置门禁：上一覆盖仍在观察期则拒绝。"""
    paper = _paper_connect(paper_db_path)
    try:
        try:
            series = _nav_series(paper, account_id)
        except sqlite3.OperationalError:
            # NAV 表不可用（极早期账本）：无法计算观察期，放行但学习周期的
            # A/B 快照仍会在数据可用后自动接管验证。
            return
    finally:
        paper.close()
    with adaptive_connect() as conn:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT deployed_at,version FROM adaptive_ab_tests "
            "WHERE account_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (account_id, kind),
        ).fetchone()
    if not row:
        return
    deployed_date = str(row["deployed_at"] or "")[:10]
    if not deployed_date:
        return
    obs_days = len([d for d, _ in series if d > deployed_date])
    if obs_days < MIN_OBSERVATION_DAYS:
        raise ValueError(
            f"账户 {account_id} 的上一 {kind} 覆盖（{row['version']}）部署于 "
            f"{deployed_date}，仅观察 {obs_days}/{MIN_OBSERVATION_DAYS} 个净值日，"
            "观察期内拒绝新覆盖（否则效果无法归因）")


def ab_summary(adaptive_connect) -> dict:
    """供 overview 展示：每个 (账户, 通道) 的最新验证结论。"""
    with adaptive_connect() as conn:
        ensure_schema(conn)
        rows = conn.execute(
            """SELECT t.* FROM adaptive_ab_tests t
               JOIN (SELECT account_id,kind,MAX(id) AS mid FROM adaptive_ab_tests
                     GROUP BY account_id,kind) latest
                 ON t.account_id=latest.account_id AND t.kind=latest.kind
                AND t.id=latest.mid
               ORDER BY t.id DESC LIMIT 40"""
        ).fetchall()
    return {"rows": [dict(r) for r in rows],
            "min_observation_days": MIN_OBSERVATION_DAYS}
