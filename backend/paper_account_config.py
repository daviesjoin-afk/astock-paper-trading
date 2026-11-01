"""账户配置字段的持久化 owner（PR-10 工作包 C）。

只做一件事：把调用方判定好的**配置类字段**写进 `paper_accounts` —— 参数快照、
版本号、风格、冷却窗口。现金、身份挂接、账户创建分别归其它 owner：

* 现金：``paper_shared_cash``
* cycle 身份/启停：``paper_user_cycle_attachment``
* 创建与字段补齐：``paper_user_account_provisioning``

边界：不读时钟（``now`` 一律由调用方传入）、不提交、不吞异常、不做版本比较
（比较与冲突判定仍由调用方完成，本模块只落库它给出的结果）。
"""
from __future__ import annotations

__all__ = [
    "ACCOUNT_CONFIG_VERSION",
    "set_cooldown",
    "set_style",
    "write_params",
]

ACCOUNT_CONFIG_VERSION = "paper-account-config-v1"


def write_params(conn, account_id, *, params, now=None, version=None):
    """写参数快照；``version`` 给出时一并写版本号，``now`` 给出时刷新时间戳。"""
    sets = ["params=?"]
    values: list = [params]
    if version is not None:
        sets.append("version=?")
        values.append(version)
    if now is not None:
        sets.append("updated_at=?")
        values.append(now)
    values.append(account_id)
    conn.execute(
        f"UPDATE paper_accounts SET {','.join(sets)} WHERE id=?",
        tuple(values),
    )


def set_style(conn, account_id, *, style, now):
    """写账户风格字段。"""
    conn.execute(
        "UPDATE paper_accounts SET style=?,updated_at=? WHERE id=?",
        (style, now, account_id),
    )


def set_cooldown(conn, account_id, *, cooldown_until):
    """写共享风控冷却窗口。"""
    conn.execute(
        "UPDATE paper_accounts SET cooldown_until=? WHERE id=?",
        (cooldown_until, account_id),
    )
