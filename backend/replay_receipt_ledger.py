# -*- coding: utf-8 -*-
"""成对回执的持久台账：原子成对发布、幂等重放、状态可续。

方案 §4.1 的落地：

- 主键就是 ``(experiment_id, target_identity, environment_hash,
  transition_sequence, evaluator_version)``；重复请求读到同一份回执，
  绝不重新发布结果。
- **成对发布**：一个会话的冠军回执与挑战者回执要么都写进去，要么都不写。
  B 侧失败不能让 A 侧单独"完成"。
- 每个回执同时保存该侧的 **后继状态**，因此下一次会话可以从台账续跑，
  不需要进程内内存状态。

表放在研究库里（与 ``agent_task_queue`` 同一个文件），所以"发回执"与
"推进任务检查点"可以在同一个事务里提交。
"""
from __future__ import annotations

import datetime as dt
import json

QUEUE_DB_HINT = "agent_tasks.sqlite3"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS experiment_runs (
        experiment_id TEXT PRIMARY KEY,
        target_identity TEXT NOT NULL,
        environment_hash TEXT NOT NULL,
        champion TEXT NOT NULL,
        challenger TEXT NOT NULL,
        rules TEXT NOT NULL,
        sessions TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paired_replay_receipts (
        experiment_id TEXT NOT NULL,
        target_identity TEXT NOT NULL,
        environment_hash TEXT NOT NULL,
        transition_sequence INTEGER NOT NULL,
        evaluator_version TEXT NOT NULL,
        side TEXT NOT NULL,
        session_date TEXT NOT NULL,
        receipt_fingerprint TEXT NOT NULL,
        step TEXT NOT NULL,
        next_state TEXT NOT NULL,
        published_at TEXT NOT NULL,
        PRIMARY KEY (experiment_id, target_identity, environment_hash,
                     transition_sequence, evaluator_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS paired_replay_receipts_experiment
        ON paired_replay_receipts(experiment_id, transition_sequence)
    """,
)


class ReceiptLedgerError(ValueError):
    """台账合同违约。"""


class ReceiptConflict(ReceiptLedgerError):
    """同一个回执键上出现了不同的内容：这是实现漂移，不能静默覆盖。"""


class ExperimentRunMismatch(ReceiptLedgerError):
    """同一个 experiment_id 被登记成了不同的实验。"""


def _now(now=None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(moment, str):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone() is not None


def ensure_schema(conn) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def record_run(conn, *, experiment_id: str, target_identity: str, environment_hash: str,
               champion: dict, challenger: dict, rules: dict, sessions: list,
               now=None) -> bool:
    """登记一次实验的冻结规格。已存在且一致 → False；不一致 → 抛错。"""
    ensure_schema(conn)
    experiment_id = str(experiment_id or "").strip()
    if not experiment_id:
        raise ReceiptLedgerError("experiment_id_required")
    existing = get_run(conn, experiment_id)
    payload = {"target_identity": str(target_identity), "environment_hash": str(environment_hash),
               "champion": champion, "challenger": challenger, "rules": rules,
               "sessions": sessions}
    if existing is not None:
        for key, value in payload.items():
            if existing[key] != value:
                raise ExperimentRunMismatch(f"experiment_run_spec_changed:{key}")
        return False
    conn.execute(
        "INSERT INTO experiment_runs (experiment_id, target_identity, environment_hash,"
        " champion, challenger, rules, sessions, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (experiment_id, payload["target_identity"], payload["environment_hash"],
         _dump(champion), _dump(challenger), _dump(rules), _dump(sessions), _now(now)),
    )
    return True


def get_run(conn, experiment_id: str):
    if not _has_table(conn, "experiment_runs"):
        return None
    row = conn.execute("SELECT * FROM experiment_runs WHERE experiment_id=?",
                       (str(experiment_id),)).fetchone()
    if row is None:
        return None
    record = dict(row)
    for key in ("champion", "challenger", "rules", "sessions"):
        record[key] = _load(record[key], None)
    return record


def _row_key(row) -> tuple:
    return (row["experiment_id"], row["target_identity"], row["environment_hash"],
            int(row["transition_sequence"]), row["evaluator_version"])


def publish_pair(conn, *, champion, challenger, champion_step: dict, challenger_step: dict,
                 champion_state: dict, challenger_state: dict, now=None) -> tuple[list, bool]:
    """原子发布一个会话的两侧回执。

    返回 ``(stored_rows, created)``。``created=False`` 表示两侧都已在台账里且内容一致
    —— 这是幂等重放，不是失败。任一侧内容漂移则抛 ``ReceiptConflict``，
    且事务内不会留下任何新行。
    """
    ensure_schema(conn)
    if champion.side != "champion" or challenger.side != "challenger":
        raise ReceiptLedgerError("receipt_sides_must_be_champion_and_challenger")
    for receipt in (champion, challenger):
        if (receipt.experiment_id, receipt.target_identity, receipt.environment_hash,
                receipt.evaluator_version) != (
                champion.experiment_id, champion.target_identity,
                champion.environment_hash, champion.evaluator_version):
            raise ReceiptLedgerError("receipts_must_belong_to_the_same_run")
    if champion.session_date != challenger.session_date:
        raise ReceiptLedgerError("paired_receipts_must_share_a_session")
    if champion.transition_sequence == challenger.transition_sequence:
        raise ReceiptLedgerError("paired_receipts_must_have_distinct_sequences")

    stamp = _now(now)
    rows = (("champion", champion, champion_step, champion_state),
            ("challenger", challenger, challenger_step, challenger_state))
    stored = []
    created = False
    # 先**两侧全部预检**，再开始写。中途才发现冲突会把冠军行留在事务里，
    # 而"要不要回滚"是调用方的事 —— 这条路径不能依赖调用方。
    pending = []
    for side, receipt, step, next_state in rows:
        existing = conn.execute(
            "SELECT * FROM paired_replay_receipts WHERE experiment_id=? AND"
            " target_identity=? AND environment_hash=? AND transition_sequence=?"
            " AND evaluator_version=?",
            (receipt.experiment_id, receipt.target_identity, receipt.environment_hash,
             int(receipt.transition_sequence), receipt.evaluator_version),
        ).fetchone()
        if existing is not None:
            if str(existing["receipt_fingerprint"]) != str(receipt.receipt_fingerprint) \
                    or str(existing["side"]) != side:
                raise ReceiptConflict(
                    f"receipt_content_drift:{side}:{receipt.transition_sequence}")
            stored.append(dict(existing))
            continue
        pending.append((side, receipt, step, next_state))
    # 写入本身也在保存点里：第二侧 INSERT 抛错时第一侧必须一起消失。预检只能防
    # 内容冲突，防不住写入失败。
    conn.execute("SAVEPOINT publish_pair")
    try:
        for side, receipt, step, next_state in pending:
            conn.execute(
                "INSERT INTO paired_replay_receipts (experiment_id, target_identity,"
                " environment_hash, transition_sequence, evaluator_version, side, session_date,"
                " receipt_fingerprint, step, next_state, published_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (receipt.experiment_id, receipt.target_identity, receipt.environment_hash,
                 int(receipt.transition_sequence), receipt.evaluator_version, side,
                 receipt.session_date, receipt.receipt_fingerprint, _dump(step),
                 _dump(next_state), stamp),
            )
            created = True
            stored.append({
                "experiment_id": receipt.experiment_id,
                "target_identity": receipt.target_identity,
                "environment_hash": receipt.environment_hash,
                "transition_sequence": int(receipt.transition_sequence),
                "evaluator_version": receipt.evaluator_version, "side": side,
                "session_date": receipt.session_date,
                "receipt_fingerprint": receipt.receipt_fingerprint,
                "step": step, "next_state": next_state, "published_at": stamp,
            })
    except BaseException:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT publish_pair")
            conn.execute("RELEASE SAVEPOINT publish_pair")
        except Exception:  # noqa: BLE001 - 保存点可能未建立
            pass
        raise
    conn.execute("RELEASE SAVEPOINT publish_pair")
    if created and len({_row_key({"experiment_id": item["experiment_id"],
                                 "target_identity": item["target_identity"],
                                 "environment_hash": item["environment_hash"],
                                 "transition_sequence": item["transition_sequence"],
                                 "evaluator_version": item["evaluator_version"]})
                        for item in stored}) != 2:
        raise ReceiptLedgerError("paired_receipts_must_use_two_distinct_keys")
    return stored, created


def _session_rows(conn, experiment_id: str) -> dict[str, dict[str, dict]]:
    if not _has_table(conn, "paired_replay_receipts"):
        return {}
    grouped: dict[str, dict[str, dict]] = {}
    for row in conn.execute(
            "SELECT * FROM paired_replay_receipts WHERE experiment_id=?",
            (str(experiment_id),)).fetchall():
        record = dict(row)
        grouped.setdefault(str(record["session_date"]), {})[str(record["side"])] = record
    return grouped


def _is_complete_pair(rows: dict) -> bool:
    """完整的一对：两侧都在、会话/身份/版本一致、序号相邻且冠军在前。"""
    champion = rows.get("champion")
    challenger = rows.get("challenger")
    if champion is None or challenger is None:
        return False
    if set(rows) != {"champion", "challenger"}:
        return False
    for field in ("target_identity", "environment_hash", "evaluator_version",
                  "session_date"):
        if str(champion.get(field)) != str(challenger.get(field)):
            return False
    if not (isinstance(champion.get("receipt_fingerprint"), str)
            and len(champion["receipt_fingerprint"]) == 64
            and isinstance(challenger.get("receipt_fingerprint"), str)
            and len(challenger["receipt_fingerprint"]) == 64):
        return False
    return int(challenger["transition_sequence"]) - int(champion["transition_sequence"]) == 1


def published_sessions(conn, experiment_id: str) -> set[str]:
    """**两侧完整配对**才算这个会话已发布。

    只看任意一侧就当成"已发布"，会让"只有冠军回执"的残留直接把实验标成完成。
    """
    return {day for day, rows in _session_rows(conn, experiment_id).items()
            if _is_complete_pair(rows)}


def incomplete_sessions(conn, experiment_id: str) -> dict[str, list[str]]:
    """半侧或不成对的残留：返回 ``{session_date: [已有的一侧]}``。

    这种状态说明过去某次发布没有原子完成。它**不能被自动跳过**，必须显式报出来
    等修复，否则实验会带着不完整的证据继续往前跑。
    """
    result = {}
    for day, rows in _session_rows(conn, experiment_id).items():
        if not _is_complete_pair(rows):
            result[day] = sorted(rows)
    return result


def receipts(conn, experiment_id: str) -> list[dict]:
    if not _has_table(conn, "paired_replay_receipts"):
        return []
    rows = conn.execute(
        "SELECT * FROM paired_replay_receipts WHERE experiment_id=?"
        " ORDER BY transition_sequence", (str(experiment_id),)).fetchall()
    result = []
    for row in rows:
        record = dict(row)
        record["step"] = _load(record["step"], {})
        record["next_state"] = _load(record["next_state"], {})
        result.append(record)
    return result


def latest_state(conn, experiment_id: str, side: str):
    """某一侧最后发布的后继状态。没有就是 None（= 从期初资本开始）。"""
    if not _has_table(conn, "paired_replay_receipts"):
        return None
    row = conn.execute(
        "SELECT next_state, transition_sequence, session_date FROM paired_replay_receipts"
        " WHERE experiment_id=? AND side=? ORDER BY transition_sequence DESC LIMIT 1",
        (str(experiment_id), str(side))).fetchone()
    if row is None:
        return None
    return {"state": _load(row["next_state"], {}),
            "transition_sequence": int(row["transition_sequence"]),
            "session_date": str(row["session_date"])}
