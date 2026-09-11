# -*- coding: utf-8 -*-
"""模拟盘选股 overlay 的历史单位兼容迁移（PR-1.1）。

背景
----
PR #107 把 ``PAPER_CONDITION_DEFAULTS["sentiment_pioneer"]["individual_mom5_min"]``
从错误的 ``2.0``（percentage points）改成 canonical ``0.02``（fraction）。
但**历史自进化 overlay 已经把旧值持久化**进 ``paper_accounts.params``：

.. code-block:: json

    {"adaptive_selection": {"model_family": "sentiment_pioneer",
                            "conditions": {"individual_mom5_min": 2.0}}}

运行时 ``_paper_conditions()`` 会用 overlay 覆盖代码默认值，于是
``mom5_raw >= 2.0``（等价 +200%）继续生效，``sentiment_pioneer`` 的
``individual_strong`` 路径仍是死分支。**代码默认值修好了，持久化状态没有** ——
所以需要一次数据兼容迁移，而不是再加一个代码补丁。

三层防护
--------
- **Layer A** ``migrate_legacy_selection_units()``：启动期经 ``db_migrate`` 修订 live overlay。
- **Layer B** ``normalize_legacy_selection_units()`` / ``normalize_legacy_account_params()``：
  activation / rollback **写 live 之前**归一化，让历史 candidate 无法重新污染 live。
- **Layer C**：读路径调用同一个纯函数做**内存级**归一化（绝不写库）。

只修**已证实的 legacy sentinel**。
---------------------
本模块**只**修 ``sentiment_pioneer`` 的 ``conditions.individual_mom5_min`` 且值恰为
``2.0`` 的情形。绝不写 ``if value > 1: value /= 100`` 这类猜测式归一化：
``2.1`` / ``0.025`` / ``20`` / ``"2.0"`` / ``True`` 一律保持原样。
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math

import paper_repository as PRP

__all__ = [
    "LEGACY_MOM5_MIN_PCT_POINTS",
    "MOM5_MIN_FIELD",
    "SENTIMENT_PIONEER_MODEL",
    "MIGRATION_ID",
    "AUDIT_EVENT",
    "AUDIT_REASON",
    "canonical_mom5_min_fraction",
    "is_exact_legacy_mom5_min",
    "normalize_legacy_conditions",
    "normalize_legacy_selection_units",
    "normalize_legacy_account_params",
    "migrate_legacy_selection_units",
]

#: 历史 bug signature：错误地以 percentage points 写入的 2.0（意图 +2%）。
LEGACY_MOM5_MIN_PCT_POINTS = 2.0
MOM5_MIN_FIELD = "individual_mom5_min"
SENTIMENT_PIONEER_MODEL = "sentiment_pioneer"
MIGRATION_ID = "legacy-sentiment-pioneer-mom5-unit-v1"
AUDIT_EVENT = "adaptive_selection_unit_migrated"
NORMALIZED_EVENT = "adaptive_selection_unit_normalized"
AUDIT_REASON = (
    "legacy percentage-point value corrected to canonical momentum fraction "
    "after factor unit contract migration"
)

_TOLERANCE = 1e-12


def canonical_mom5_min_fraction():
    """canonical 目标值的**唯一来源**：PR #107 的代码默认值。

    刻意从 ``strategies.PAPER_CONDITION_DEFAULTS`` 读取而不是再硬编码一个
    ``0.02``，避免出现第二个事实源。``strategies`` 依赖 pandas，故延迟导入。
    """
    import strategies as S

    return float(S.PAPER_CONDITION_DEFAULTS[SENTIMENT_PIONEER_MODEL][MOM5_MIN_FIELD])


def is_exact_legacy_mom5_min(value):
    """仅当 ``value`` 是**数值**且恰为 ``2.0`` 时返回 True。

    显式排除 ``bool``（``True`` 是 ``int`` 子类）、字符串（``"2.0"`` 不是已证实的
    历史序列化格式）、``NaN``/``inf``，以及 ``2.1``/``20``/``0.025`` 等一切其它值。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    return math.isclose(number, LEGACY_MOM5_MIN_PCT_POINTS, rel_tol=0.0, abs_tol=_TOLERANCE)


def _detail(old_value, new_value):
    return {
        "migration_id": MIGRATION_ID,
        "field": MOM5_MIN_FIELD,
        "old_value": float(old_value),
        "new_value": float(new_value),
        "reason": AUDIT_REASON,
    }


def normalize_legacy_conditions(conditions, *, model_family=None, canonical=None):
    """纯函数：归一化一份 conditions 字典。

    返回 ``(normalized_copy, details)``：

    - ``normalized_copy`` 永远是**新对象**，绝不原地修改调用方入参；
    - ``details`` 为空列表表示什么都没改（含"值不是已证实 sentinel"的情形）。

    只有 ``model_family == "sentiment_pioneer"`` 且 ``individual_mom5_min`` 恰为
    ``2.0``（JSON number）时才会替换为 canonical fraction。
    """
    if not isinstance(conditions, dict):
        return conditions, []
    result = copy.deepcopy(conditions)
    if str(model_family or "") != SENTIMENT_PIONEER_MODEL:
        return result, []
    if MOM5_MIN_FIELD not in result:
        return result, []
    value = result[MOM5_MIN_FIELD]
    if not is_exact_legacy_mom5_min(value):
        return result, []
    target = canonical_mom5_min_fraction() if canonical is None else float(canonical)
    result[MOM5_MIN_FIELD] = target
    return result, [_detail(value, target)]


def normalize_legacy_selection_units(overlay, *, effective_model=None):
    """纯函数：归一化一份 ``adaptive_selection`` overlay。

    model 判定优先用 overlay 自带的 ``model_family``；缺失时才回退到调用方给出的
    ``effective_model``（例如账户当前模型）。返回 ``(normalized_copy, details)``。
    """
    if not isinstance(overlay, dict):
        return overlay, []
    result = copy.deepcopy(overlay)
    model = str(result.get("model_family") or effective_model or "")
    if model != SENTIMENT_PIONEER_MODEL:
        return result, []
    conditions = result.get("conditions")
    if not isinstance(conditions, dict):
        return result, []
    normalized, details = normalize_legacy_conditions(
        conditions, model_family=SENTIMENT_PIONEER_MODEL,
    )
    if details:
        result["conditions"] = normalized
    return result, details


def normalize_legacy_account_params(params, *, effective_model=None):
    """纯函数：归一化 ``paper_accounts.params`` 整份 JSON。

    只触碰 ``params["adaptive_selection"]``，其余键（``adaptive_selection_meta``、
    自定义键等）语义与取值完全不变。返回 ``(normalized_copy, details)``。
    """
    if not isinstance(params, dict):
        return params, []
    overlay = params.get("adaptive_selection")
    if not isinstance(overlay, dict):
        return copy.deepcopy(params), []
    normalized_overlay, details = normalize_legacy_selection_units(
        overlay, effective_model=effective_model,
    )
    if not details:
        return copy.deepcopy(params), []
    result = copy.deepcopy(params)
    result["adaptive_selection"] = normalized_overlay
    return result, details


def _default_now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _is_plausible_fraction(value):
    """把非 sentinel 的取值分类为"正常 fraction"或"可疑值"。

    **只用于统计与告警**，绝不据此做任何转换。判定为"分数域内"的条件是：
    有限数值且 ``|v| <= 1``（``0.02`` / ``0.025`` / ``0.5`` / ``1.0``）。
    字符串、``bool``、``NaN``/``inf``，以及 ``1.5`` / ``2.1`` / ``20`` 这类
    超出分数域的值一律归为可疑（记录但不修改）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and abs(number) <= 1.0


def _load_params(raw):
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def migrate_legacy_selection_units(conn, *, now_fn=None):
    """Layer A：把 live ``paper_accounts.params`` 里已证实的 legacy 单位修订为 canonical。

    设计约束（PR-1.1 §6–§9）：

    - **精确匹配**：只处理 ``model_family == "sentiment_pioneer"`` 且
      ``conditions.individual_mom5_min`` 恰为数值 ``2.0`` 的行；
    - **不改其它任何 JSON 字段**，也不"顺便刷新成新 defaults"；
    - **同一事务**内完成 写回 + audit —— audit 失败则 params 不落盘
      （调用方 ``db_migrate`` 外层还有一层 ``BEGIN``/``commit``/``rollback``）；
    - **幂等**：第二次运行 ``migrated == 0``，且不产生重复 audit。

    返回统计 ``{"scanned", "migrated", "skipped", "unexpected"}``。
    绝不输出完整 params 内容。
    """
    now_fn = now_fn or _default_now
    stats = {"scanned": 0, "migrated": 0, "skipped": 0, "unexpected": 0}
    if not _table_exists(conn, "paper_accounts"):
        return stats

    rows = conn.execute("SELECT id, params FROM paper_accounts").fetchall()
    for row in rows:
        stats["scanned"] += 1
        account_id = str(row[0])
        params = _load_params(row[1])
        if params is None:
            stats["unexpected"] += 1
            print(json.dumps({
                "alarm": "legacy_momentum_overlay_unparsable",
                "account_id": account_id,
                "note": "params 不是合法 JSON object，保持原样",
            }, ensure_ascii=False), flush=True)
            continue

        overlay = params.get("adaptive_selection")
        if not isinstance(overlay, dict):
            stats["skipped"] += 1
            continue
        conditions = overlay.get("conditions")
        if not isinstance(conditions, dict) or MOM5_MIN_FIELD not in conditions:
            stats["skipped"] += 1
            continue

        model = str(overlay.get("model_family") or "")
        if model != SENTIMENT_PIONEER_MODEL:
            stats["skipped"] += 1
            continue

        normalized_overlay, details = normalize_legacy_selection_units(
            overlay, effective_model=model,
        )
        if not details:
            # 字段存在但不是已证实的 legacy sentinel。有两种情况：
            #   1) 已是合法 fraction（0.02 / 0.025 / 0.5…）→ 用户或迁移后的正常值，跳过；
            #   2) 超出分数域或类型异常（1.5 / 2.1 / 20 / "2.0" / NaN…）→ 记录但不猜。
            value = conditions.get(MOM5_MIN_FIELD)
            if _is_plausible_fraction(value):
                stats["skipped"] += 1
            else:
                stats["unexpected"] += 1
                print(json.dumps({
                    "alarm": "legacy_momentum_threshold_unexpected",
                    "account_id": account_id,
                    "field": MOM5_MIN_FIELD,
                    "value_type": type(value).__name__,
                    "note": "非已证实的 legacy sentinel，保持原样",
                }, ensure_ascii=False), flush=True)
            continue

        updated = dict(params)
        updated["adaptive_selection"] = normalized_overlay
        conn.execute(
            "UPDATE paper_accounts SET params=? WHERE id=?",
            (json.dumps(updated, ensure_ascii=False), account_id),
        )
        for detail in details:
            PRP.audit(
                conn, account_id, AUDIT_EVENT,
                json.dumps(detail, ensure_ascii=False), now_fn(),
            )
        stats["migrated"] += 1

    if stats["migrated"] or stats["unexpected"]:
        print("legacy factor-unit migration: scanned=%d migrated=%d skipped=%d unexpected=%d"
              % (stats["scanned"], stats["migrated"], stats["skipped"], stats["unexpected"]),
              flush=True)
    return stats
