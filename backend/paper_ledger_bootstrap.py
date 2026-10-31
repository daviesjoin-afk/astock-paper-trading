# -*- coding: utf-8 -*-
"""账本结构 bootstrap owner：只回答"结构是否已按当前代码建好 / 升好"。

**它回答什么**：核心表、增量列、索引、触发器这些**结构**要不要再建一遍；
建完之后结构是否真的完整（必需表/列校验）；已升级的库能否走低成本路径。

**它不回答什么**（各有 owner，本模块绝不复制其结论）：

- 账户配置、周期挂接、策略 seed → ``paper_trading`` / ``strategy_registry`` /
  ``runtime_settings`` 的运行期引导，每次 ``init_db`` 都跑，不因"结构 ready"跳过；
- 过期租约回收、signal 与订单对账 → ``paper_trading`` 的运行期恢复；
- 迁移版本号 → ``db_migrate`` 的 ``schema_version`` 体系。本模块只写"本次事务
  里结构已校验通过"的收据（``ledger_schema_receipt``），不复述、不改写版本号。

**生命周期**：一次 ``init_db`` 调用、一条连接、一个事务。不缓存任何进程级
"已 ready"结论，收据跟着数据库文件走；换 ``DB_PATH`` 天然隔离。
"""
from __future__ import annotations

import paper_schema_migrations as PSM
import runtime_settings as RSET
import strategy_champion as SCM


def ensure_ledger_structure(conn):
    """建表 + 加列 + 索引 + 触发器。只动结构，不写任何业务行。

    调用顺序沿用原 ``paper_trading._upgrade_ledger`` 的 DDL 顺序，只是把账户 /
    周期 / 租约回收 / 对账这些**业务**步骤留在 ``paper_trading``。这里每个
    函数都是幂等的；只有结构摘要失配或结构 owner 源码变化时才会被调到。
    """
    PSM.ensure_core_tables(conn)
    PSM.ensure_paper_columns(conn)
    # 收据表本身也是结构的一部分：先建它，后面算出的结构摘要才包含它，否则
    # "写入时的摘要"永远比"下一次读到的摘要"少一张表，冷启动永不收敛。
    PSM.ensure_bootstrap_receipt_table(conn)
    # 设置表 + 策略定义/生命周期结构（含 registry 生命周期 pin 的取值口径）。
    RSET.ensure_settings_structure(conn)
    PSM.ensure_execution_verification_columns(conn)
    PSM.ensure_rebalance_state_cycle_ownership(conn)
    PSM.ensure_feature_tables(conn)
    PSM.ensure_runtime_lease_columns(conn)
    PSM.ensure_ignition_shadow_table(conn)
    PSM.ensure_performance_indexes(conn)
    SCM.ensure_schema(conn)


def bootstrap_schema(conn, *, fresh):
    """确认结构已按当前代码建好/升好；不满足就重建并重新校验。

    ``fresh`` 为真表示账本主表都不存在（全新库），此时无条件跑完整链。
    返回 ``{"path": "verified"|"cold", "owner_fingerprint": ...}``，只用于
    编排与观测，不参与任何业务判断。

    **不在这里写收据**：收据代表"整个初始化事务成功"，由 ``init_db`` 在业务
    恢复也走完之后、同一个事务里写入 —— 迁移/恢复中途失败就整体回滚，下一次
    启动重跑完整链，绝不会把半成品标成"已完成升级"。

    源码指纹取不到时 ``schema_owner_fingerprint`` 直接抛错（fail closed）：没有
    指纹就无法证明收据仍对应当前代码，此时既不能走 verified，也不能写新收据。
    """
    fingerprint = PSM.structure_fingerprint(conn)
    owner_fingerprint = PSM.schema_owner_fingerprint()
    receipt = PSM.read_bootstrap_receipt(conn)
    if (
        not fresh
        and receipt is not None
        and receipt["contract_version"] == PSM.BOOTSTRAP_CONTRACT_VERSION
        # 历史哨兵（unavailable:<module>）不是指纹，一律不算命中。
        and PSM.is_valid_owner_fingerprint(receipt["owner_fingerprint"])
        and receipt["owner_fingerprint"] == owner_fingerprint
        and receipt["structure_fingerprint"] == fingerprint
    ):
        # 低成本路径：摘要与收据一致，不再重复 DDL；但仍做必需表/列校验，
        # 校验失败会抛错，而不是带着损坏结构进入交易。
        PSM.verify_required_structure(conn)
        return {"path": "verified", "owner_fingerprint": owner_fingerprint}
    ensure_ledger_structure(conn)
    PSM.verify_required_structure(conn)
    return {"path": "cold", "owner_fingerprint": owner_fingerprint}
