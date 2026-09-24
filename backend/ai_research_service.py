# -*- coding: utf-8 -*-
"""R27-B2B —— 研究运行时唯一的 **orchestration boundary**。

R27-A 定义了 typed research contract，R27-B1 打通了 provider 链路，R27-B2A 给了
typed hypothesis 一个 append-only 的 canonical 存放点。三者都是**零件**：没有任何
一层负责"一次研究运行的生命周期"。本模块就是那一层，且**只有**那一层。

它负责的业务非常具体：

    1. 接收 caller intent（``purpose`` / ``trigger`` / 研究身份 / 研究问题）
    2. 接收**已经由 authority 签发**的 typed ``InformationEvent``
    3. 调用 :func:`ai_research_provider.run_research` —— **恰好一次**
    4. 拿到 typed ``ResearchHypothesis``
    5. 显式生成 operational metadata：``created_at``、provider 槽位/模型、token/latency
    6. 调用 :func:`ai_research_repository.append_run` —— **恰好一次**，在一个短事务里
    7. 返回 research result + canonical run id

依赖方向是单向的，且**不可反转**：

    runtime caller
        ↓
    ai_research_service
        ├── ai_research_provider     （typed 输入 → typed ResearchHypothesis）
        └── ai_research_repository   （append-only canonical ledger）

``repository → service``、``provider → service``、``contract → service`` 都**不存在**：
service 在最上层，不是被下层回调的钩子。本模块也**不** import
``ai_research_contract`` —— 它不需要重新校验或重建 typed 对象，provider 交出来的已经是
契约对象，repository 会再独立校验一次。多一个消费者就多一份"两套规则必然漂移"的风险。

──────────────────── 这一层不拥有什么 ────────────────────

**不拥有 research 裁决。** ``status`` / ``reason`` / ``confidence`` / ``authority`` /
``is_authoritative`` 全部由 R27-A 从 evidence 派生，本层只是把它们原样交给持久化层。
本层没有任何参数能让调用方自述结论。

**不拥有 provider 配置。** ``provider_config`` 由**调用方**解析好后传入（与 R27-B1 的
``run_research``、与 ``ai_review_service`` 的 ``ai1`` / ``ai2`` 槽位同构）。本层不读环境
变量、不建第三套 API Key 词表、不认识任何厂商身份。

**不拥有业务幂等。** 本层**不**生成 ``research_run_key`` / job id / cycle id，也**不**
把 ``hypothesis_id`` 当唯一键。两次明确执行就是两条 run —— 这与 canonical ledger 的
append-only 语义一致，也与被迁移的 legacy writer 的既有行为一致（legacy 表同样是每次
执行插一行）。真正的 exactly-once 需求若在某条 runtime 上出现，属于 **application 层**
的 business key，不属于这里，更不能污染 append-only 台账的含义。

**不拥有任何 execution authority。** 本层产出的是研究结论与审计元数据，**绝不**产出
signal / order / risk / promotion / portfolio 写入。它不 import 任何那些模块。

──────────────────── 事务与网络边界 ────────────────────

顺序是硬约束，不是风格问题：

    provider / network call          ← **不在任何事务内**
            ↓
    typed ResearchHypothesis
            ↓
    短 DB transaction                ← 只有 append_run 一次本地写
            ↓
    append_run

把网络请求放进事务会让一次 LLM 等待持有 SQLite 写锁（并发下整库阻塞），也会让
"provider 失败但事务已开始"变成一个需要靠 rollback 兜底的状态。这里从结构上避免它：
``connect_factory()`` 只在 provider 返回**之后**才被打开一次。

──────────────────── 失败语义 ────────────────────

三类失败都必须让调用方看到**明确失败**，而不是一条"看起来成功"的降级结果：

* provider / contract 失败 → :data:`REASON_PROVIDER_FAILED`，**canonical row = 0**
* 持久化失败 → :data:`REASON_PERSISTENCE_FAILED`
* 任何一类失败都**不**回落到 legacy provider / legacy 表写入

第三条尤其重要：``new path failed → 静默改走 legacy LLM → 写 legacy 表 → 调用方以为
成功`` 会同时制造双 authority、双付费路径和不可审计的状态。宁可让一次运行明确失败。

``ResearchServiceError`` 因此**不**把失败翻译成空结论：把"调用失败"写成"模型没有提出
异议"会让失败不可观测，并让下游把一次零产出当成一次成功的复核。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import ai_research_provider as provider
import ai_research_repository as repository

__all__ = [
    "TZ",
    "DEFAULT_MAX_TOKENS",
    "REASON_PROVIDER_FAILED",
    "REASON_PERSISTENCE_FAILED",
    "ResearchServiceError",
    "ResearchRunResult",
    "run_research_run",
]

#: 运维时间语义与仓库其余部分一致（上海时区）。
TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_MAX_TOKENS = 1800

#: provider 或 contract 阶段失败。**不**降级、**不**回落 legacy。
REASON_PROVIDER_FAILED = "provider_failed"
#: canonical 写入失败。**不**假装成功、**不**改用 legacy 表兜底。
REASON_PERSISTENCE_FAILED = "persistence_failed"


class ResearchServiceError(RuntimeError):
    """一次研究运行无法完成 —— fail closed。

    ``reason`` 是唯一可被程序依赖的字段；``stage`` 说明失败发生在哪一段
    （``provider`` / ``persistence``）。``cause`` 保留原始异常对象供诊断，但**不**参与
    文案拼接：provider 的原始响应体、API Key、请求 URL 都不得因此进入日志或审计。

    刻意继承 ``RuntimeError`` 而不是 ``ValueError``：这不是"参数不合法"，而是"这次运行
    没有产出"。
    """

    def __init__(
        self,
        reason: str,
        detail: str = "",
        *,
        stage: str = "",
        cause: BaseException | None = None,
    ) -> None:
        self.reason = str(reason)
        self.stage = str(stage)
        self.cause = cause
        text = self.reason if not detail else "%s: %s" % (self.reason, detail)
        super().__init__(text)


@dataclass(frozen=True)
class ResearchRunResult:
    """一次**已经落库**的研究运行。

    ``hypothesis`` 是 typed 结论：它的 ``status`` / ``reason`` / ``authority`` /
    ``is_authoritative`` 全部由 R27-A 派生。本层不复制、不重算、也不改写成摘要 ——
    复制一份裁决就多一个会和契约漂移的第二真值。

    ``created_at`` 是**本次运行的持久化时间**，``hypothesis.as_of`` 是**业务/证据时间**。
    两者刻意分开：历史证据不得因为"今天跑了一次研究"而被重新解释成今天的结论。
    """

    run_id: int
    purpose: str
    trigger: str
    created_at: str
    provider_slot: str | None
    provider_model: str
    hypothesis: Any
    narrative: str
    counter_arguments: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    latency_ms: int


# ─────────────────────────────────────────────────────────────────────────────
# 运维时间
# ─────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    """本层**唯一**读墙上时钟的地方，且只用于 operational metadata。

    它产出的值只进 ``created_at`` 一列。``as_of`` 永远由调用方/evidence 提供 ——
    研究层不接受"用当前时间重新解释历史证据"。
    """
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# 付费前的输入校验 —— 坏输入不该先花钱再失败
# ─────────────────────────────────────────────────────────────────────────────


def _required_label(value: Any, *, what: str, limit: int) -> str:
    """``purpose`` / ``trigger`` 是**审计标签**：只做非空与长度检查。

    刻意**不**在这里解释它们的内容（禁止 ``if purpose == "signal"``）：它们不控制
    status、不选择 writer、不参与任何判定。长度上界复用
    :mod:`ai_research_repository` 的常量，避免"service 收得比 ledger 宽"造出一个
    永远写不进去的值。

    这里真的会跑吗？会 —— 它在 provider **之前**执行。``purpose`` 写成空串时，若等到
    ``append_run`` 才发现，那条永远写不进去的运行已经为一次 LLM 调用付过费了。
    """
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError(f"{what} requires a non-empty value")
    if len(text) > limit:
        raise ValueError(f"{what} exceeds {limit} chars")
    return text


def _provider_slot(provider_config: Any) -> str | None:
    """从**已解析好的**槽位配置里取审计标签。

    本层不校验它是不是 ``ai1`` / ``ai2`` —— 那需要 import ``ai_review_service`` 的槽位
    定义，等于让 orchestration 层依赖整套 provider 配置模型，并造出第二份槽位词表。
    repository 会做长度上界，且这一列从不参与任何判定。
    """
    if not isinstance(provider_config, Mapping):
        return None
    value = provider_config.get("slot")
    return value if isinstance(value, str) and value.strip() else None


# ─────────────────────────────────────────────────────────────────────────────
# entry point
# ─────────────────────────────────────────────────────────────────────────────


def run_research_run(
    connect_factory,
    *,
    purpose: Any,
    trigger: Any,
    hypothesis_id: Any,
    as_of: Any,
    subject: Any,
    question: Any,
    events: Any,
    provider_config: Any,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    clock=None,
) -> ResearchRunResult:
    """跑一次研究运行并把它**恰好一次**追加进 canonical ledger。

    ``provider_config`` 是调用方解析好的槽位配置（``slot`` / ``api_key`` / ``base_url`` /
    ``model`` / ``timeout_seconds``）。``events`` 必须是 typed ``InformationEvent``；
    provider 会在**任何网络请求之前**完成类型校验与 PIT 预检，坏输入不会先付费。

    ``clock`` 只为测试注入；生产走 :func:`_now`。它**只**影响 ``created_at``。

    返回 :class:`ResearchRunResult`。任何失败都抛 :class:`ResearchServiceError` ——
    包括"provider 成功但落库失败"，那种情况下 canonical row 不存在，调用方必须看到失败，
    而不是拿到一个假装成功的返回值。
    """
    # ① 审计标签先校验：坏 purpose 不该先付费再失败。
    purpose_text = _required_label(
        purpose, what="purpose", limit=repository.MAX_PURPOSE_CHARS,
    )
    trigger_text = _required_label(
        trigger, what="trigger", limit=repository.MAX_TRIGGER_CHARS,
    )

    # ② provider / network：**不在任何 DB transaction 内**。
    #    provider 自己负责"先校验、后付费"：规范 caller 身份 → typed 事件校验 →
    #    PIT 预检 → 去重/冲突检测 → 才发起请求。
    try:
        result = provider.run_research(
            provider_config=provider_config,
            hypothesis_id=hypothesis_id,
            as_of=as_of,
            subject=subject,
            question=question,
            events=events,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 —— 一律 fail closed，不回落 legacy
        raise ResearchServiceError(
            REASON_PROVIDER_FAILED, type(exc).__name__, stage="provider", cause=exc,
        ) from exc

    # ③ 短 DB transaction：只有一次 append。`created_at` 在这一刻取，
    #    它表示"这条运行何时被持久化"，而不是 provider 何时开始思考。
    created_at = (clock or _now)()
    slot = _provider_slot(provider_config)
    try:
        with connect_factory() as conn:
            repository.ensure_schema(conn)
            run_id = repository.append_run(
                conn,
                hypothesis=result.hypothesis,
                purpose=purpose_text,
                trigger=trigger_text,
                created_at=created_at,
                provider_slot=slot,
                provider_model=result.model,
                narrative=result.narrative,
                counter_arguments=result.counter_arguments,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=result.latency_ms,
            )
    except Exception as exc:  # noqa: BLE001 —— 持久化失败必须显式上报
        raise ResearchServiceError(
            REASON_PERSISTENCE_FAILED, type(exc).__name__, stage="persistence", cause=exc,
        ) from exc

    return ResearchRunResult(
        run_id=int(run_id),
        purpose=purpose_text,
        trigger=trigger_text,
        created_at=created_at,
        provider_slot=slot,
        provider_model=str(result.model or ""),
        hypothesis=result.hypothesis,
        narrative=result.narrative,
        counter_arguments=tuple(result.counter_arguments),
        input_tokens=int(result.input_tokens),
        output_tokens=int(result.output_tokens),
        latency_ms=int(result.latency_ms),
    )
