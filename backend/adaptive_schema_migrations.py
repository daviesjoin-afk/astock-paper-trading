"""``adaptive_learning.sqlite3`` 的 schema owner（2026-10-29 收口）.

为什么需要它
------------
``db_migrate.MIGRATIONS`` 里 ``paper_trading`` 有 40 个版本步骤，而
``adaptive_learning`` 只有 **1 个**——部署日志那句"adaptive_learning 已是最新 v1"
的真实含义是：**版本号与真实 schema 无关**。该库的 40 余张表实际由 8 个 owner 在
建立连接时各自 ``CREATE TABLE IF NOT EXISTS`` 建出来，编排点却挂在业务引擎
``adaptive_engine._init_schema`` 里。这与 PR #290 修掉的"两条升级链漂移 → 升级后
缺表"是同一个根因，只是发生在另一个库。

本模块只做一件事：把"谁的表由谁建、按什么顺序建、失败怎么算"变成**一个有名字的
owner**。DDL 文本仍归各模块（它们最了解自己的表），**编排权与版本权**归这里：

* ``ensure_adaptive_feature_tables(conn)`` —— 按固定顺序调用 8 个 owner 的 ``ensure_schema``；
* ``ensure_owner(conn, label)`` —— 单个 owner，供 ``db_migrate`` 逐步登记版本；

顺序不是随意的：``adaptive_risk`` / ``adaptive_selection`` 的表最先（引擎早期路径就读
它们），两个 learning owner 最后且**允许失败**（契约层自己 fail closed，建表失败不得
阻断服务启动）。这两条语义从前靠 ``_init_schema`` 里的位置和 try/except 表达，
现在变成显式契约。

刻意不用 ``getattr(module, name)`` / ``__import__`` 做分发：那会把"谁参与建表"
藏进字符串，任何静态守卫都看不见（2026-10-29 审查 F-03 的教训）。这里的 8 个
分支是**显式**的，同时在调用时读模块属性，所以 ``patch.object(news_learning,
"ensure_schema")`` 这类测试缝仍然有效。
"""
from __future__ import annotations

import adaptive_risk
import adaptive_selection
import ai_analysis
import deepseek_advisor
import learning_dataset
import learning_evaluation
import news_learning
import self_evolution
import trade_attribution

#: 建表顺序 + 失败是否可容忍。``db_migrate`` 的自适应库步骤按同一顺序登记版本。
OWNER_LABELS: tuple[str, ...] = (
    "risk_evolution",
    "selection_evolution",
    "deepseek_advisor",
    "news_learning",
    "trade_attribution",
    "ai_analysis",
    "self_evolution",
    "learning_dataset",
    "learning_evaluation",
)

#: 允许建表失败（由契约层 fail closed）的 owner。
TOLERANT_OWNERS: frozenset[str] = frozenset({"learning_dataset", "learning_evaluation"})


def ensure_owner(conn, label: str) -> None:
    """建一个 owner 的表（幂等）。未登记的标签直接报错，不静默跳过。"""
    if label == "risk_evolution":
        adaptive_risk.ensure_schema(conn)
    elif label == "selection_evolution":
        adaptive_selection.ensure_schema(conn)
    elif label == "deepseek_advisor":
        deepseek_advisor.ensure_schema(conn)
    elif label == "news_learning":
        news_learning.ensure_schema(conn)
    elif label == "trade_attribution":
        trade_attribution.ensure_schema(conn)
    elif label == "ai_analysis":
        ai_analysis.ensure_schema(conn)
    elif label == "self_evolution":
        # 2026-10-29：进化库的表原先只在"用到进化功能时"懒建（引擎里有 8 处
        # 懒调用），于是同一份代码在不同部署上能得到不同 schema——"用过才建"就是
        # 版本号失真的另一个来源。它的 DDL 全是 CREATE TABLE IF NOT EXISTS，
        # 放进连接期编排后懒调用退化成幂等空转，不改变任何数据。
        self_evolution.ensure_schema(conn)
    elif label == "learning_dataset":
        learning_dataset.ensure_schema(conn)
    elif label == "learning_evaluation":
        learning_evaluation.ensure_schema(conn)
    else:
        raise KeyError(f"未登记的 adaptive schema owner: {label}")


def ensure_adaptive_feature_tables(conn) -> None:
    """按契约顺序建立 8 个 owner 的表；两个 learning owner 允许失败。

    这就是原先 ``adaptive_engine._init_schema`` 两处（快速路径与完整路径）里手写的
    那 8 次调用，逐行等价搬过来——包括"最后的 try/except pass"语义：学习契约的
    表升级是纯 additive + 幂等的，失败由契约层自己 fail closed，绝不能因此让
    服务起不来。
    """
    for label in OWNER_LABELS:
        if label not in TOLERANT_OWNERS:
            ensure_owner(conn, label)
            continue
        try:
            ensure_owner(conn, label)
        except Exception:  # noqa: BLE001 - 与既有语义一致：契约层 fail closed
            pass
