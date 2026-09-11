# -*- coding: utf-8 -*-
"""周期归档服务（PR-58：从 paper_trading.py 抽出的第一条受控垂直）。

**归属**：周期**耐久归档**这条垂直——快照格式、归档落库、活动账本清理、资金校验。

**为什么是这条垂直**（决策记录，PR-58 的要求）：

1. 任务书候选优先级是 Cycle → Signal → Risk Exit。周期所有权**尚未**抽取，
   但周期创建/参与者解析与账户常量（``ACCOUNT_SPECS``/``ACTIVE_ACCOUNT_IDS``/
   ``STYLE_PROFILES``）以及账户开设辅助（``_spec_for``/``_ensure_user_strategy_accounts``/
   ``_benchmark_close``）深度耦合；把它们一起搬会变成"搬常量 + 搬账户域"，爆炸半径
   远超"一次一条垂直"。
2. 因此本 PR 只搬**与账户域无关**的那部分周期垂直：资金校验、账本快照
   （``compact-ledger-v2``）、归档写入、活动表清理、审计。它们只依赖
   ``paper_repository``（已在更底层）与标准库。
3. 所有权/参与者（PR-48 不变式）与周期创建留待后续 PR——那时应先抽取
   ``paper_account_specs``（常量层）再搬，避免为了搬函数而制造循环依赖。

**依赖方向**（新服务不得 import paper_trading）::

    paper_trading (façade / orchestration)
            ↓
    paper_cycle_service
            ↓
    paper_repository / 标准库

行为与抽取前逐字等价：只搬函数、不改一行语义。
"""
from __future__ import annotations

import datetime as dt
import json

import paper_repository as PRP

__all__ = [
    "CYCLE_SERVICE_VERSION",
    "ARCHIVE_FORMAT",
    "LEDGER_TABLES",
    "COUNTED_TABLES",
    "PURGED_TABLES",
    "CAPITAL_MIN",
    "CAPITAL_MAX",
    "now",
    "validate_capital",
    "cycle_snapshot",
    "archive_cycle",
]

CYCLE_SERVICE_VERSION = "cycle-service-v1"
ARCHIVE_FORMAT = "compact-ledger-v2"

# 归档时**整体搬运**的账本表（durable trading ledger）。
LEDGER_TABLES = (
    "paper_accounts", "paper_orders", "paper_positions",
    "paper_position_lots", "paper_fills", "paper_nav",
    "paper_parameter_versions", "paper_position_limit_versions",
)

# 高量运维表只记行数，搬运整包 payload 会让轮转体积无界增长。
COUNTED_TABLES = (
    "paper_signals", "paper_risk_decisions", "paper_jobs",
    "paper_job_runs", "paper_reviews", "paper_intraday_observations",
    "paper_position_reviews", "paper_capital_reservations",
)

# 归档后清空的活动账本表（历史保留在 paper_archives.snapshot 里）。
PURGED_TABLES = (
    "paper_signals", "paper_orders", "paper_positions", "paper_position_lots", "paper_fills",
    "paper_risk_decisions", "paper_nav", "paper_jobs", "paper_job_runs", "paper_reviews",
    "paper_intraday_observations", "paper_parameter_versions", "paper_position_reviews",
    "paper_capital_reservations", "paper_position_limit_versions",
)

CAPITAL_MIN = 1000.0
CAPITAL_MAX = 10_000_000.0


def now() -> str:
    """与 paper_trading._now 完全一致的本地时间戳格式。"""
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def validate_capital(capital) -> float:
    """资金池边界校验（与抽取前逐字一致）。"""
    capital = float(capital)
    if capital < CAPITAL_MIN or capital > CAPITAL_MAX:
        raise ValueError("总模拟资金池须在 1,000 至 10,000,000 元之间")
    return capital


def cycle_snapshot(conn, cycle):
    """构造周期归档快照（``compact-ledger-v2``）。

    Archives are consumed by the account/stock history views.  Loading every
    risk decision and its full decision_snapshot into one Python object made
    cycle rollover grow without bound and could restart the container before
    the transaction committed.  Preserve the durable trading ledger and
    version history, while recording counts for high-volume operational
    tables instead of duplicating their bulky payloads.
    """
    snapshot = {}
    for table in LEDGER_TABLES:
        if table == "paper_orders":
            # Exclude the large evidence JSON at SQL level.  Fetching it and
            # deleting it afterwards still causes a large temporary allocation
            # and was enough to make rollover exceed the gateway timeout.
            columns = [
                row["name"] for row in conn.execute("PRAGMA table_info(paper_orders)").fetchall()
                if row["name"] != "risk_payload"
            ]
            select_list = ",".join(f'"{name}"' for name in columns)
            snapshot[table] = PRP.rows(conn, f"SELECT {select_list} FROM paper_orders")
        else:
            snapshot[table] = PRP.rows(conn, f"SELECT * FROM {table}")
    snapshot["_archive_format"] = ARCHIVE_FORMAT
    snapshot["_table_counts"] = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in LEDGER_TABLES + COUNTED_TABLES
    }
    return cycle, snapshot


def archive_cycle(conn, cycle, reason):
    """把当前周期整体归档：写 paper_archives、翻状态、清空活动表、记审计。

    历史只归档不删除；返回被归档的 cycle 行（dict）。
    """
    _cycle, snapshot = cycle_snapshot(conn, cycle)
    stamp = now()
    conn.execute("INSERT INTO paper_archives(cycle_id,cycle_key,reason,snapshot,created_at) VALUES(?,?,?,?,?)",
                 (cycle["id"], cycle["cycle_key"], reason, _json(snapshot), stamp))
    conn.execute("UPDATE paper_cycles SET status='archived',ended_at=?,updated_at=? WHERE id=?",
                 (stamp, stamp, cycle["id"]))
    for table in PURGED_TABLES:
        conn.execute(f"DELETE FROM {table}")
    # 与抽取前逐字等价：原 _audit() 在**调用时**取 _now()，不复用归档时间戳。
    PRP.audit(conn, None, "cycle_archived", f"周期 {cycle['cycle_key']} 已归档：{reason}", now())
    return cycle
