# -*- coding: utf-8 -*-
"""双AI共识调参器 —— 历史入口，已收敛到通用槽位编排器。

真正的 schema / 配置 / 调用 / 共识 / 编排逻辑全部在 ``ai_review_service``；
本模块只保留一层**兼容门面**，让既有调用方（``adaptive_engine`` /
``api_adaptive`` / 下游 ``self_evolution``·``evolution_apply`` / 历史测试）
无需一次性改完：

- ``mimo`` / ``deepseek`` 被当作 ``ai1`` / ``ai2`` 的历史别名，映射表只在
  ``ai_review_service.LEGACY_PROVIDER_SLOTS`` 出现一次，运行期不存在任何
  "按厂商身份分支"的逻辑；
- 返回结构保留 ``mimo`` / ``deepseek`` 键，但内容来自通用槽位结果；
- 审计表名 ``dual_ai_tuning_runs`` 与列语义保持不变，下游无感。

新代码请直接用 ``ai_review_service``（``run_ai_review`` / ``get_slots`` /
``update_slot`` / ``get_review_settings`` · ``update_review_settings``）。
"""
from __future__ import annotations

import ai_review_service as _S

DUAL_AI_VERSION = _S.AI_REVIEW_VERSION

# 共识阈值：从通用层再导出，保持历史读法可用。
CONSENSUS_WEIGHT_DIRECTION_THRESHOLD = _S.CONSENSUS_WEIGHT_DIRECTION_THRESHOLD
CONSENSUS_WEIGHT_MAGNITUDE_RATIO = _S.CONSENSUS_WEIGHT_MAGNITUDE_RATIO
CONSENSUS_DELTA_DIRECTION_THRESHOLD = _S.CONSENSUS_DELTA_DIRECTION_THRESHOLD
CONSENSUS_CONDITION_MAGNITUDE_RATIO = _S.CONSENSUS_CONDITION_MAGNITUDE_RATIO
CONSENSUS_MIN_CONFIDENCE = _S.CONSENSUS_MIN_CONFIDENCE
CONSENSUS_MAX_WEIGHT_STEP = _S.CONSENSUS_MAX_WEIGHT_STEP
CONSENSUS_MAX_DELTA_STEP = _S.CONSENSUS_MAX_DELTA_STEP


def ensure_schema(conn):
    """创建/升级 AI 审核相关表（幂等，只做加法）。"""
    _S.ensure_schema(conn)


def get_api_keys(conn):
    """历史视图：厂商别名 → 掩码状态（绝不含明文 Key）。"""
    slots = _S.get_slots(conn)
    result = {}
    for legacy, slot in _S.LEGACY_PROVIDER_SLOTS:
        view = dict(slots[slot])
        view["provider"] = legacy
        result[legacy] = view
    return result


def update_api_key(conn, provider, api_key=None, base_url=None, model=None, enabled=None):
    """历史入口：``provider`` 作为槽位别名映射后写入对应槽位。"""
    return _S.update_slot(
        conn, _S.resolve_slot(provider),
        api_key=api_key, base_url=base_url, model=model, enabled=enabled,
    )


def _get_provider_config(conn, provider):
    """历史入口：单槽位完整配置（含明文 Key），仅服务端内部使用。"""
    cfg = _S.get_slot_config(conn, _S.resolve_slot(provider))
    return {
        "provider": provider,
        "slot": cfg["slot"],
        "api_key": cfg["api_key"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "enabled": cfg["enabled"],
        "timeout": cfg["timeout_seconds"],
    }


def _check_consensus(mimo_proposals, deepseek_proposals, accounts_map, evolution=None,
                     evolution_by_account=None, labels=None):
    """历史入口：把两个提案列表适配成 ``{"ai1": …, "ai2": …}`` 后交给通用共识门禁。"""
    res = _S._check_consensus(
        {"ai1": mimo_proposals, "ai2": deepseek_proposals}, accounts_map,
        evolution=evolution, evolution_by_account=evolution_by_account, labels=labels,
    )
    return res[0], res[1], res[2]


def _build_tuning_system_prompt():
    return _S._build_tuning_system_prompt()


def _build_tuning_user_prompt(evidence, accounts, mode):
    return _S._build_tuning_user_prompt(evidence, accounts, mode)


def _legacy_side(side):
    side = side or {}
    return {
        "status": side.get("status"),
        "model": side.get("model"),
        "decision": side.get("decision"),
        "confidence": side.get("confidence"),
        "market_regime": side.get("market_regime"),
        "summary": side.get("summary"),
        "proposals_count": side.get("proposals_count", 0),
        "latency_ms": side.get("latency_ms"),
        "error": side.get("error"),
    }


def _legacy_view(result):
    """把统一结果模型翻译回历史的 ``mimo`` / ``deepseek`` 结构。"""
    reviewers = result.get("reviewers") or {}
    left = _legacy_side(reviewers.get("ai1"))
    right = _legacy_side(reviewers.get("ai2"))
    proposals = result.get("proposals") or []
    return {
        "id": result.get("id"),
        "version": DUAL_AI_VERSION,
        "mode": result.get("mode"),
        "review_mode": result.get("review_mode"),
        "status": result.get("status"),
        "consensus": bool(result.get("consensus")),
        "consensus_reason": result.get("reason"),
        "reason": result.get("reason"),
        "merged_proposals": proposals,
        "proposals": proposals,
        "mimo": left,
        "deepseek": right,
        "ai1": left,
        "ai2": right,
        "reviewers": reviewers,
        "config_snapshot": result.get("config_snapshot"),
        "single_reviewer_slot": result.get("single_reviewer_slot"),
        "total_latency_ms": result.get("total_latency_ms"),
        "evidence_hash": result.get("evidence_hash"),
    }


def run_dual_ai_tuning(connect_factory, paper_db_path, snapshot_paths, evidence_collector,
                       tuning_accounts_fn, config=None, profile=None, trigger="scheduled",
                       mode="intraday"):
    """历史入口：执行一次 AI 审核并翻译回旧结构。

    实际执行的是 ``ai_review_service.run_ai_review``：模式由 ``ai_review_settings``
    的 ``review_mode`` 决定（single / dual），绝不在这里按厂商身份选路。
    """
    result = _S.run_ai_review(
        connect_factory, paper_db_path, snapshot_paths, evidence_collector,
        tuning_accounts_fn, config=config, profile=profile, trigger=trigger, mode=mode,
    )
    return _legacy_view(result)


def recent_runs(conn, limit=20):
    """读取最近的审核记录。"""
    return _S.recent_runs(conn, limit)


def dual_ai_status(conn):
    """整体状态（保留 ``providers`` / ``mimo_ready`` / ``deepseek_ready`` 等旧字段）。"""
    return _S.review_status(conn)
