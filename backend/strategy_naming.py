# -*- coding: utf-8 -*-
"""策略与选股模型族的名号（唯一命名入口）。

**为什么有这个模块**：仓库里同时存在三代名号 —— 五策略时代的策略 id、远古的模型族
别名（``one_to_two`` 这种）、以及现在的三策略正式集合。散落各处的旧名号会让人分不清
"这个名字是历史数据里的值，还是现在还在跑的东西"。

本模块给出的答案是：**旧名号只允许出现在这个文件里**，其它任何生产代码都必须使用
canonical 名称，或调用这里的解析函数。

三类名号的区别（不要混）：

1. **正式策略 id**（``tq_breakout`` / ``trend_pullback`` / ``reported_profit_breakout``）
   —— 当前周期的参与者。权威定义在 :mod:`strategy_roster`，本模块只做转发。
2. **退役策略 id**（``sector_rotation`` / ``main_force_top10``）
   —— 不再参与新周期，但归档账本与历史持仓仍以它们为身份。**不可删除**：
   删掉就读不出历史，属于伪造历史。
3. **模型族名**（选股打分模型）—— 曾用远古别名，现统一为 canonical 名称。
   持久化字段 ``paper_accounts.source_strategy`` 和历史 ``params`` 里可能仍是旧值，
   读取时必须经过 :func:`canonical_model_family` 归一化。
"""
from __future__ import annotations

CANONICAL_FIRST_BOARD_RELAY = "first_board_relay"
CANONICAL_PULLBACK_REVERSAL = "pullback_reversal"
CANONICAL_SECTOR_SENTIMENT = "sector_sentiment"
CANONICAL_TREND_CONTINUATION = "trend_continuation"

MODEL_FAMILIES = (
    CANONICAL_FIRST_BOARD_RELAY,
    CANONICAL_PULLBACK_REVERSAL,
    CANONICAL_SECTOR_SENTIMENT,
    CANONICAL_TREND_CONTINUATION,
)

NAMING_VERSION = "strategy-naming-v1"

#: 旧模型族名 → canonical 名。**这是全仓库唯一允许出现旧名号的地方。**
_LEGACY_MODEL_FAMILY_ALIASES = {
    "one_to_two": CANONICAL_FIRST_BOARD_RELAY,
    "bottom_reversal": CANONICAL_PULLBACK_REVERSAL,
    "sentiment_pioneer": CANONICAL_SECTOR_SENTIMENT,
}

LEGACY_MODEL_FAMILIES = tuple(sorted(_LEGACY_MODEL_FAMILY_ALIASES))

#: 数据兼容迁移专用：历史 overlay 里 ``model_family`` 可能是旧值，
#: 匹配必须用旧值，否则那条历史状态永远修不到。
LEGACY_SECTOR_SENTIMENT_MODEL = "sentiment_pioneer"

#: 派生 payload key 也跟着改名，避免 API 回报里继续出现旧名号。
_LEGACY_CONTEXT_KEYS = {"bottom_reversal_context": f"{CANONICAL_PULLBACK_REVERSAL}_context"}


def canonical_model_family(value) -> str:
    """把任意来源的模型族名归一化成 canonical 名称。

    未知值原样返回（调用方自己决定是拒绝还是记录），**不做猜测式映射**。
    """
    name = str(value or "").strip()
    if not name:
        return ""
    return _LEGACY_MODEL_FAMILY_ALIASES.get(name, name)


def is_legacy_model_family(value) -> bool:
    return str(value or "").strip() in _LEGACY_MODEL_FAMILY_ALIASES


def is_canonical_model_family(value) -> bool:
    return str(value or "").strip() in MODEL_FAMILIES


def legacy_context_key(key: str) -> str:
    """历史 payload 里的派生 key → canonical key。"""
    return _LEGACY_CONTEXT_KEYS.get(str(key or ""), str(key or ""))


def migration_pairs() -> tuple[tuple[str, str], ...]:
    """``(旧值, 新值)`` 列表，供持久化迁移使用。"""
    return tuple(sorted(_LEGACY_MODEL_FAMILY_ALIASES.items()))


__all__ = [
    "CANONICAL_FIRST_BOARD_RELAY", "CANONICAL_PULLBACK_REVERSAL",
    "CANONICAL_SECTOR_SENTIMENT", "CANONICAL_TREND_CONTINUATION",
    "MODEL_FAMILIES", "NAMING_VERSION", "LEGACY_MODEL_FAMILIES",
    "LEGACY_SECTOR_SENTIMENT_MODEL", "canonical_model_family",
    "is_legacy_model_family", "is_canonical_model_family", "legacy_context_key",
    "migration_pairs",
]
