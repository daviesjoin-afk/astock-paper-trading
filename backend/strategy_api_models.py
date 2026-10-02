# -*- coding: utf-8 -*-
"""PR-51：Strategy Admin API 的类型化请求契约（Pydantic v2）。

只描述**输入形状**（字段名 / 类型 / 可选性 / 默认值），不承载业务语义：
id 模式与 DSL 合法性由 ``strategy_registry`` / ``strategy_dsl_schema`` 裁决；
生命周期合法边由 ``strategy_lifecycle`` 裁决，晋级证据由
``strategy_promotion`` 裁决，非对称风险闸门由 ``asymmetric_risk`` 裁决，
再由 ``strategy_service`` 翻译成 domain exception。这里**不复制第二套规则**。

两条硬约束：

1. ``extra="ignore"``——调用方多传的字段一律丢弃。这是"风险放大唯一入口
   不被 Web 侧绕过"的前提：``risk_evidence`` / ``challenger_win`` /
   evolution evidence / promotion approval 即使出现在 body 里也不会被采纳，
   ``strategy_service`` 仍然按 fail-closed 走闸门（Web 只能收紧风险）。
2. 不做语义校验——例如 ``id`` 只要求是字符串，"必须是 ``^[a-z][a-z0-9_]{2,63}$``"
   由 Registry 判定，避免同一个规则出现两种实现与两种错误文案。

兼容面：``clone`` 的遗留 ``id`` 字段、编辑时顶层直接给可版本化字段，都是
旧前端（策略注册表）在用的写法，保留到 PR-52 统一清理。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Request(BaseModel):
    """所有请求模型的公共配置：忽略多余字段（见模块 docstring 约束 1）。"""

    model_config = ConfigDict(extra="ignore")


class StrategyCreateRequest(_Request):
    """``POST /api/strategies``。"""

    id: str
    name: str
    description: str = ""
    metadata: dict[str, Any] | None = None
    dsl_ast: Any = None
    actor: str = "web-ui"


class StrategyUpdateRequest(_Request):
    """``PUT`` / ``PATCH /api/strategies/{id}``（生成不可变新版本）。

    ``changes`` 为空时回落到顶层的可版本化字段（PATCH 便捷形态），与旧行为
    一致；因此这四个字段在这里是**可选**的，是否是"本次要改的字段"由调用方
    是否真的传了该键决定（``model_fields_set``），路由不再自己拼装。
    """

    changes: dict[str, Any] | None = None
    # PATCH 便捷形态：顶层直接给可版本化字段。
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    dsl_ast: Any = None
    expected_version: int | None = None
    change_note: str = ""
    actor: str = "web-ui"

    def versioned_fields(self) -> dict[str, Any]:
        """返回本次要写入的可版本化字段（changes 优先，否则取顶层显式字段）。"""
        if self.changes is not None:
            return dict(self.changes)
        fields = ("name", "description", "metadata", "dsl_ast")
        return {key: getattr(self, key) for key in fields if key in self.model_fields_set}

    def has_changes(self) -> bool:
        return bool(self.versioned_fields())


class StrategyValidateRequest(_Request):
    """``POST /api/strategies/validate``。"""

    dsl_ast: Any = None
    metadata: Any = None


class StrategyPreviewRequest(_Request):
    """``POST /api/strategies/preview``。

    预览引擎读取的草稿字段是开放集合（``style`` / ``max_positions`` /
    ``max_weight_pct`` / ``max_exposure_pct`` / ``hold_min`` / ``hold_max`` …
    见 ``strategy_creation_preview``）。这里只声明常用字段，其余原样透传，
    因此额外字段是 **allow** 而不是 ignore：预览是只读计算，不存在"多传字段
    被采纳"的风险放大语义。
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    dsl_ast: Any = None

    def draft(self) -> dict[str, Any]:
        """还原成预览引擎消费的普通字典（含 extra 字段，不含未传的字段）。"""
        return self.model_dump(exclude_unset=True)


class StrategyTransitionRequest(_Request):
    """Exact, compare-and-swap lifecycle intent."""

    strategy_version: int
    strategy_checksum: str
    expected_state: str
    target_state: str
    actor_type: Literal["human", "system", "ai"]
    actor_id: str
    proposal_fingerprint: str | None = None
    reason_code: str = ""
    reason: str = ""


class PromotionProposalRequest(_Request):
    """AI/human/system proposal bound to explicit immutable identities."""

    strategy_version: int
    strategy_checksum: str
    expected_state: str
    target_state: str
    evidence_bundle: dict[str, Any] = Field(default_factory=dict)
    proposer_type: Literal["human", "system", "ai"]
    proposer_id: str
    rationale: str = ""


class StrategyHealthCaptureRequest(_Request):
    """``POST /api/strategies/{id}/health/snapshots``。

    R33-A 只接受**显式**的 exact version 与显式 observation window：没有
    「latest」、没有「当前日」、没有隐式窗口，也没有任何由证据到结论的判断。
    """

    strategy_version: int
    strategy_checksum: str
    observation_start: str
    observation_end: str
    #: 可选：只接受**显式给出**的 exact R32 比对报告 id，绝不搜索最新报告。
    comparison_report_id: str | None = None


class StrategyRetirementEvaluateRequest(_Request):
    """``POST /api/strategies/{id}/retirement/evaluate``。

    R33-B 只接受**显式**的 health snapshot id：没有 latest health、没有 current
    health，也没有任何隐式的「当前状态」输入。
    """

    snapshot_id: str


class StrategyCloneRequest(_Request):
    """``POST /api/strategies/{id}/clone``。"""
    new_strategy_id: str | None = None
    # 旧前端（策略注册表）传 ``id``；新契约用 ``new_strategy_id``。
    id: str | None = None
    name: str | None = None
    source_version: int | None = None
    actor: str = "web-ui"

    def target_id(self) -> str:
        return str(self.new_strategy_id or self.id or "").strip()


# ---------------------------------------------------------------------------
# 响应模型：只覆盖有明确外部契约的两处，其余保持现有 dict 形状不动
# （避免为了 typed 而一次性重写全部 response schema）。
# ---------------------------------------------------------------------------

class StrategyValidateResponse(BaseModel):
    valid: bool
    normalized_ast: Any = None
    checksum: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StrategyDeleteResponse(BaseModel):
    strategy_id: str
    deleted: bool
    archived_instead_hint: str | None = None
