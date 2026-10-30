# -*- coding: utf-8 -*-
"""调度任务及独立展示刷新队列的状态迁移和唯一写入 owner。

**它回答什么**：一条任务记录从"被认领"到"完成/失败/被回收"的状态怎么写，以及
租约（``owner_key`` + ``fencing_token``）如何参与每次 CAS。认领探测、首次写入、
重试复用、心跳续期、完成、失败、启动恢复、历史清理——全部在这里，语句只有一份。

**它不回答什么**（各有 owner，本模块绝不复制其结论）：

- 这次运行**该不该**重试 → ``paper_trading`` 的槽位编排（``retry_count`` / ``force``
  语义在那里）；
- 租约锁本身的取得/释放/过期判定 → 运行时租约锁表与 ``_lease_abandoned``；
- 重试后的业务分派、成交与资金 → 各自的业务 owner。

**为什么值得单独一层**：这套 CAS 以前在 ``paper_trading`` 里被复制了 9 份（daily 与
intraday 两张表 × 认领/复用/续期/完成/失败/恢复/清理），任何一次谓词改动都要求人手
改齐 9 处；漏一处就等于给"重复成交 / 活任务被抢 / 死亡任务不回收"开一道口子。
搬出来之后谓词只有一份，"谁能写这两张表"可以机械检查。

**边界**：本模块接受**调用方已打开**的连接，自己从不 commit/rollback，也不开新连接；
表名是模块常量（不用 f-string 拼表名），写入语句只出现在本文件。
"""
from __future__ import annotations

DAILY_TABLE = "paper_jobs"
INTRADAY_TABLE = "paper_job_runs"

RUNNING = "running"
RETRYABLE_STATUSES = ("failed", "interrupted")

# 保留窗口：intraday 分钟级记录只服务"当日幂等"，30 天够复盘；daily 记录保留
# 180 天，防止 dashboard 的 ORDER BY 在多年累积后变慢。
INTRADAY_RETENTION = "date('now', '-30 days')"
DAILY_RETENTION = "date('now', '-180 days')"

_ABANDONED_DETAIL = (
    "json_patch(CASE WHEN json_valid(detail) THEN detail ELSE '{}' END,"
    " json_object('error','任务进程中断或超时，系统已自动回收',"
    " 'recovered_at',?,"
    " 'recovered_from_running',1,"
    " 'failure_kind','interrupted',"
    " 'elapsed_seconds',CAST(ROUND((julianday(?) - julianday(started_at)) * 86400) AS INTEGER)))"
)

_LEASE_CAS = "owner_key=? AND fencing_token=? AND status='running'"


def _table(run_key):
    return INTRADAY_TABLE if run_key is not None else DAILY_TABLE


def read_job(conn, *, slot, market_date, run_key=None):
    """Read the existing record for this run identity (``None`` when absent)."""
    if run_key is not None:
        return conn.execute(
            f"SELECT * FROM {INTRADAY_TABLE} WHERE run_key=?", (run_key,),
        ).fetchone()
    return conn.execute(
        f"SELECT * FROM {DAILY_TABLE} WHERE slot=? AND market_date=?",
        (slot, market_date),
    ).fetchone()


def mark_interrupted(conn, *, slot, market_date, run_key=None, detail, now):
    """Force a stale ``running`` record to ``failed`` with the caller's detail."""
    if run_key is not None:
        return conn.execute(
            f"UPDATE {INTRADAY_TABLE} SET status='failed',detail=?,finished_at=?"
            " WHERE run_key=?",
            (detail, now, run_key),
        )
    return conn.execute(
        f"UPDATE {DAILY_TABLE} SET status='failed',detail=?,finished_at=?"
        " WHERE slot=? AND market_date=?",
        (detail, now, slot, market_date),
    )


def claim_job(conn, *, slot, market_date, run_key=None, owner, expires_at,
          fencing_token, now):
    """Write the first ``running`` record for this identity."""
    if run_key is not None:
        return conn.execute(
            f"""INSERT INTO {INTRADAY_TABLE}(
                    run_key,slot,market_date,status,started_at,owner_key,
                    heartbeat_at,expires_at,fencing_token
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (run_key, slot, market_date, RUNNING, now, owner, now, expires_at,
             fencing_token),
        )
    return conn.execute(
        f"""INSERT INTO {DAILY_TABLE}(
                slot,market_date,status,started_at,owner_key,heartbeat_at,
                expires_at,fencing_token
            ) VALUES(?,?,?,?,?,?,?,?)""",
        (slot, market_date, RUNNING, now, owner, now, expires_at, fencing_token),
    )


def reclaim_job(conn, *, slot, market_date, run_key=None, owner, expires_at,
            fencing_token, detail, now):
    """Take over a retryable record; ``rowcount`` tells whether this caller won.

    ``rowcount != 1`` is **not** an error here: another instance won the race.
    Callers release their runtime lease and report ``already_done``.
    """
    if run_key is not None:
        return conn.execute(
            f"""UPDATE {INTRADAY_TABLE}
                SET status='running',detail=?,started_at=?,finished_at=NULL,
                    owner_key=?,heartbeat_at=?,expires_at=?,fencing_token=?
                WHERE run_key=? AND status IN ('failed','interrupted')""",
            (detail, now, owner, now, expires_at, fencing_token, run_key),
        )
    return conn.execute(
        f"""UPDATE {DAILY_TABLE}
            SET status='running',detail=?,started_at=?,finished_at=NULL,
                owner_key=?,heartbeat_at=?,expires_at=?,fencing_token=?
            WHERE slot=? AND market_date=? AND status IN ('failed','interrupted')""",
        (detail, now, owner, now, expires_at, fencing_token, slot, market_date),
    )


def renew_lease(conn, *, slot, market_date, run_key=None, owner, fencing_token,
          heartbeat_at, expires_at):
    """Extend the heartbeat; ``rowcount != 1`` means the lease is no longer ours."""
    if run_key is not None:
        return conn.execute(
            f"UPDATE {INTRADAY_TABLE} SET heartbeat_at=?,expires_at=?,fencing_token=?"
            f" WHERE run_key=? AND {_LEASE_CAS}",
            (heartbeat_at, expires_at, fencing_token, run_key, owner,
             fencing_token),
        )
    return conn.execute(
        f"UPDATE {DAILY_TABLE} SET heartbeat_at=?,expires_at=?,fencing_token=?"
        f" WHERE slot=? AND market_date=? AND {_LEASE_CAS}",
        (heartbeat_at, expires_at, fencing_token, slot, market_date, owner,
         fencing_token),
    )


def complete_job(conn, *, slot, market_date, run_key=None, owner, fencing_token,
             status, detail, now):
    """Close the record with its final status; callers must check ``rowcount``."""
    if run_key is not None:
        return conn.execute(
            f"UPDATE {INTRADAY_TABLE} SET status=?,detail=?,finished_at=?"
            f" WHERE run_key=? AND {_LEASE_CAS}",
            (status, detail, now, run_key, owner, fencing_token),
        )
    return conn.execute(
        f"UPDATE {DAILY_TABLE} SET status=?,detail=?,finished_at=?"
        f" WHERE slot=? AND market_date=? AND {_LEASE_CAS}",
        (status, detail, now, slot, market_date, owner, fencing_token),
    )


def fail_job(conn, *, slot, market_date, run_key=None, owner, fencing_token,
         detail, now):
    """Close the record as ``failed`` while this caller still holds the fence."""
    if run_key is not None:
        return conn.execute(
            f"UPDATE {INTRADAY_TABLE} SET status='failed',detail=?,finished_at=?"
            f" WHERE run_key=? AND {_LEASE_CAS}",
            (detail, now, run_key, owner, fencing_token),
        )
    return conn.execute(
        f"UPDATE {DAILY_TABLE} SET status='failed',detail=?,finished_at=?"
        f" WHERE slot=? AND market_date=? AND {_LEASE_CAS}",
        (detail, now, slot, market_date, owner, fencing_token),
    )


def recover_abandoned(conn, *, now, intraday_cutoff, job_cutoff):
    """Fail every ``running`` record whose lease expired (or trusted timestamp aged out).

    Recovery **merges** the detail instead of overwriting it: the old
    implementation wrote ``{error, recovered_at}`` and erased ``retry_count`` /
    ``history_refresh`` / ``factor_refresh`` / ``timing``, so an interrupted run
    became indistinguishable from a data-coverage failure afterwards.
    ``elapsed_seconds`` records how long the run survived before being reclaimed.

    A freshly started process is **not** evidence that another worker died: only
    the explicit lease expiry (or the legacy ``started_at`` fallback) reclaims.
    """
    intraday = conn.execute(
        f"""UPDATE {INTRADAY_TABLE}
            SET status='failed',detail={_ABANDONED_DETAIL},finished_at=?
            WHERE status='running'
              AND ((expires_at IS NOT NULL AND expires_at<?)
                   OR (expires_at IS NULL AND started_at<?))""",
        (now, now, now, now, intraday_cutoff),
    ).rowcount
    daily = conn.execute(
        f"""UPDATE {DAILY_TABLE}
            SET status='failed',detail={_ABANDONED_DETAIL},finished_at=?
            WHERE status='running'
              AND ((expires_at IS NOT NULL AND expires_at<?)
                   OR (expires_at IS NULL AND started_at<?))""",
        (now, now, now, now, job_cutoff),
    ).rowcount
    return {"job_runs": intraday, "jobs": daily}


def prune_jobs(conn):
    """Delete records past their retention window (``rowcount`` only, no commit)."""
    intraday = conn.execute(
        f"DELETE FROM {INTRADAY_TABLE} WHERE market_date < {INTRADAY_RETENTION}",
    ).rowcount
    daily = conn.execute(
        f"DELETE FROM {DAILY_TABLE} WHERE market_date < {DAILY_RETENTION}",
    ).rowcount
    return {"job_runs_purged": intraday, "jobs_purged": daily}


class SnapshotLeaseLost(RuntimeError):
    """A display worker no longer owns the right to publish."""


def read_snapshot_refresh(conn):
    row = conn.execute("SELECT * FROM paper_dashboard_refresh_jobs WHERE id=1").fetchone()
    return dict(row) if row is not None else None


def enqueue_snapshot_refresh(conn, *, trigger, now):
    """Coalesce requests, retaining a follow-up request made during a running pass."""
    conn.execute("""INSERT INTO paper_dashboard_refresh_jobs
        (id,status,trigger,requested_at) VALUES(1,'queued',?,?)
        ON CONFLICT(id) DO UPDATE SET
            trigger=excluded.trigger, requested_at=excluded.requested_at,
            request_version=request_version+1,
            status=CASE WHEN status='running' THEN 'running' ELSE 'queued' END
        """, (str(trigger)[:60], now))
    return read_snapshot_refresh(conn)


def claim_snapshot_refresh(conn, *, owner, now, expires_at):
    """Claim pending/due work or recover an expired process with a new fence."""
    changed = conn.execute("""UPDATE paper_dashboard_refresh_jobs SET
        status='running', owner_key=?, fencing_token=fencing_token+1,
        claimed_version=request_version, attempt=attempt+1,
        started_at=?, heartbeat_at=?, expires_at=?, finished_at=NULL, next_retry_at=NULL
        WHERE id=1 AND (
            (status IN ('queued','failed') AND (next_retry_at IS NULL OR next_retry_at<=?))
            OR (status='running' AND expires_at<=?)
        )""", (owner, now, now, expires_at, now, now))
    return read_snapshot_refresh(conn) if changed.rowcount == 1 else None


def renew_snapshot_refresh(conn, *, owner, fencing_token, now, expires_at):
    return conn.execute("""UPDATE paper_dashboard_refresh_jobs
        SET heartbeat_at=?, expires_at=?
        WHERE id=1 AND status='running' AND owner_key=? AND fencing_token=? AND expires_at>?
        """, (now, expires_at, owner, fencing_token, now)).rowcount == 1


def assert_snapshot_refresh_lease(conn, *, owner, fencing_token, now):
    row = conn.execute("""SELECT 1 FROM paper_dashboard_refresh_jobs
        WHERE id=1 AND status='running' AND owner_key=? AND fencing_token=? AND expires_at>?
        """, (owner, fencing_token, now)).fetchone()
    if row is None:
        raise SnapshotLeaseLost("dashboard snapshot lease lost")


def complete_snapshot_refresh(conn, *, owner, fencing_token, now):
    changed = conn.execute("""UPDATE paper_dashboard_refresh_jobs SET
        status=CASE WHEN request_version>claimed_version THEN 'queued' ELSE 'completed' END,
        finished_at=?, last_completed_at=?, last_error=NULL,
        owner_key=NULL, expires_at=NULL, next_retry_at=NULL
        WHERE id=1 AND status='running' AND owner_key=? AND fencing_token=? AND expires_at>?
        """, (now, now, owner, fencing_token, now))
    if changed.rowcount != 1:
        raise SnapshotLeaseLost("dashboard snapshot completion lost its lease")


def fail_snapshot_refresh(conn, *, owner, fencing_token, now, retry_at, error):
    return conn.execute("""UPDATE paper_dashboard_refresh_jobs SET
        status='failed', finished_at=?, next_retry_at=?, last_error=?,
        owner_key=NULL, expires_at=NULL
        WHERE id=1 AND status='running' AND owner_key=? AND fencing_token=? AND expires_at>?
        """, (now, retry_at, str(error)[:500], owner, fencing_token, now)).rowcount == 1
