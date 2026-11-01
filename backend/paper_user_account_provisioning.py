# -*- coding: utf-8 -*-
"""账户创建与字段补齐的持久化 owner（PR-10 工作包 C）。

负责 `paper_accounts` 的**创建**职责：内置账户与用户策略账户的 INSERT、既有账户
的档案字段补齐、以及新策略版本的首次写入。现金、身份挂接与配置字段分别归
`paper_shared_cash` / `paper_user_cycle_attachment` / `paper_account_config`。

不读时钟（``now`` 由调用方传入）、不提交、不决定"该不该建这个账户"。
"""

ACCOUNT_INSERT_COLUMNS = (
    "id", "name", "source_strategy", "status", "initial_cash", "cash", "cycle_days",
    "max_positions", "max_weight", "max_exposure", "risk_profile", "version",
    "benchmark_start", "created_at", "updated_at",
)


def insert_account(conn, *, fields):
    """创建一条账户行；列集合受 :data:`ACCOUNT_INSERT_COLUMNS` 约束。"""
    unknown = set(fields) - set(ACCOUNT_INSERT_COLUMNS)
    if unknown:
        raise ValueError(f"未知账户列：{sorted(unknown)}")
    columns = [name for name in ACCOUNT_INSERT_COLUMNS if name in fields]
    marks = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO paper_accounts({','.join(columns)}) VALUES({marks})",
        tuple(fields[name] for name in columns),
    )


def ensure_profile(conn, account_id, *, name, source_strategy, cycle_days,
                   max_weight, max_exposure, mode, risk_profile):
    """既有账户的档案字段补齐（模式为空时才写入调用方给出的默认模式）。"""
    conn.execute(
        """UPDATE paper_accounts SET name=?,source_strategy=?,cycle_days=?,
           max_weight=?,max_exposure=?,mode=COALESCE(NULLIF(mode,''),?),
           risk_profile=? WHERE id=?""",
        (name, source_strategy, cycle_days, max_weight, max_exposure, mode,
         risk_profile, account_id),
    )


def set_version(conn, account_id, *, version):
    """只给新引入的策略写首次版本号；已有版本不被配置刷新改写。"""
    conn.execute(
        "UPDATE paper_accounts SET version=? WHERE id=?",
        (version, account_id),
    )


def provision_user_accounts(conn, participant_ids, *, spec_for, now_fn, audit_fn):
    """Create missing paused, zero-funded user strategy ledger rows."""
    for account_id in participant_ids:
        exists = conn.execute("SELECT 1 FROM paper_accounts WHERE id=?", (account_id,)).fetchone()
        if exists:
            continue
        spec = spec_for(account_id)
        now = now_fn()
        insert_account(conn, fields={
            "id": account_id, "name": spec["name"],
            "source_strategy": spec["source_strategy"], "status": "paused",
            "initial_cash": 0.0, "cash": 0.0, "cycle_days": spec["cycle_days"],
            "max_positions": spec["max_positions"], "max_weight": spec["max_weight"],
            "max_exposure": spec["max_exposure"], "risk_profile": spec["risk_profile"],
            "version": spec["strategy_version"], "benchmark_start": None,
            "created_at": now, "updated_at": now,
        })
        audit_fn(conn, account_id, "user_strategy_account_provisioned",
                 f"用户策略 {spec['name']} 已开户（paused，等待周期分配资金；DSL v{spec['dsl_version']}）")
