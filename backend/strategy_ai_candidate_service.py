# -*- coding: utf-8 -*-
"""R35-C orchestration：exact research hypothesis → R35-B candidate batch。

一句话 authority：本模块拥有 **"一次 AI 候选生成的完整编排"** —— 读取 exact
canonical R27 research run、pin exact parent、调用 provider、把 proposal 交给 R35-B
确定性展开并追加台账。

它不是无意义 wrapper：R27 research ledger、R35-B generation、provider 网络这三者的
**顺序与失败语义**本身就是独立 authority，正确顺序是本层的责任：

```text
1. 短读事务    exact research run + exact parent pin facts
2. 关闭写事务/连接
3. AI provider 网络调用            ← 绝不在 SQLite 写事务里等 LLM
4. 严格解析 + R35-B search space 校验
5. 短写事务    重新 pin exact parent → R35-B 生成 + 追加 batch/proposals/candidates
6. commit
```

─────────────── 几条不可让步的顺序约束 ───────────────

* **research 资格 gate 在网络调用之前**：``insufficient_evidence`` / ``unsupported``
  / ``corrupt record`` / ``run not found`` 必须在**第二次 provider 调用之前**拒绝 ——
  否则坏输入先产生一次付费调用。
* **confidence 不参与资格**：R27 已明确它是 AI 自评而非证据。这里不设任何
  ``confidence >= x`` 阈值，资格只看 ``status == supported``。
* **as-of 必须与 research run 一致**：不允许拿 2026-09-01 的 hypothesis 自动生成
  2026-10-06 的候选。要在新业务日用同一思想，必须**重新产生** canonical research run。
* **AI 不选 parent，也不控制 universe / regime / constraints**：这些由 caller 显式
  输入或 exact pinned parent 继承。constraints 走 R35-B 的 inherit-or-only-tighten，
  R35-C **不开放** AI risk tuning。
* **写事务 all-or-nothing**：第 N 个 proposal 写失败 → 整次 generation batch 回滚。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

import ai_research_contract as ARC
import ai_research_repository as ARR
import strategy_ai_proposal as SAIP
import strategy_ai_provider as SAIPR
import strategy_candidate as SC
import strategy_candidate_service as SCV
import strategy_candidate_search_space as SS
import strategy_generator as SG

__all__ = [
    "AICandidateGenerationError",
    "REASON_RESEARCH_NOT_FOUND",
    "REASON_RESEARCH_UNSUPPORTED",
    "REASON_RESEARCH_AUTHORITY",
    "REASON_ASOF_MISMATCH",
    "REASON_AI_CANDIDATE_CAP",
    "generate_candidates_from_research",
    "plan_research_candidate_generation",
]


class AICandidateGenerationError(RuntimeError, ValueError):
    """Stable rejection from the AI candidate generation boundary."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)


REASON_RESEARCH_NOT_FOUND = "research_run_not_found"
REASON_RESEARCH_UNSUPPORTED = "research_not_supported"
REASON_RESEARCH_AUTHORITY = "research_is_not_authoritative"
REASON_ASOF_MISMATCH = "research_as_of_mismatch"
REASON_AI_CANDIDATE_CAP = "ai_candidate_cap_exceeded"


def _research_provenance(run: Mapping[str, Any]) -> dict:
    """Map the exact R27 run onto R35-B's existing research provenance vocabulary.

    ``source_identity`` 必须是 **exact** run identity，绝不是 ``"latest-ai-research"``；
    ``source_fingerprint`` 直接使用 R27 已有的 ``record_hash`` —— R27 已经是
    integrity authority，这里不重新 hash 另一份。
    """
    return {
        "source_kind": "ai_research",
        "source_identity": f"ai_research_run:{run.get('id')}",
        "source_fingerprint": str(run.get("record_hash") or ""),
        "hypothesis_id": str(run.get("hypothesis_id") or ""),
    }


def _require_supported(run: Mapping[str, Any]) -> None:
    """Research 资格 gate —— **只**消费 R27 repository 的权威判定。

    ``get_run`` 返回前已经做过 stored/embedded 一致性与 ``record_hash`` 重算，因此
    ``status`` 是 canonical 的、不可被外部声明。R35-C 不重算 status、不设
    ``confidence >= x`` 阈值：confidence 是 AI 自评而非证据，参与资格判定就等于把
    "AI 越自信结论越强"这条环路请回来。
    """
    if str(run.get("status")) != ARC.HYPOTHESIS_SUPPORTED:
        raise AICandidateGenerationError(REASON_RESEARCH_UNSUPPORTED,
                                         str(run.get("status")))


def _research_run_identity(research_run_id: Any) -> int:
    """Normalise the caller-supplied run identity to the ledger's integer key.

    只接受规范十进制整数（``int`` 或纯数字字符串）—— 这是 HTTP 与 CLI 的形态差异，
    不是模糊匹配。``"7"`` → ``7``；``" 7"`` / ``"7.0"`` / ``"latest"`` / ``"*"``
    一律拒绝：任何"帮调用方猜一个 run"的行为都会重新引入隐式 latest。
    """
    if isinstance(research_run_id, bool):
        raise AICandidateGenerationError(REASON_RESEARCH_NOT_FOUND, "invalid run id")
    if isinstance(research_run_id, int) and research_run_id > 0:
        return int(research_run_id)
    if isinstance(research_run_id, str) and research_run_id.strip().isdigit():
        value = int(research_run_id.strip())
        if value > 0:
            return value
    raise AICandidateGenerationError(REASON_RESEARCH_NOT_FOUND, "invalid run id")


def plan_research_candidate_generation(
    conn: sqlite3.Connection,
    *,
    research_run_id: str,
    strategy_id: str,
    strategy_version: int,
    strategy_checksum: str,
    asof: str | None = None,
    universe_spec=None,
    intended_market_regime: str | None = None,
) -> dict:
    """The **read** half: every gate that must fail closed before a paid call.

    存在的唯一理由是把"拒绝"排在网络调用之前。没有这一层，一次坏输入会先花掉一次
    provider 调用；有了它，provider 只可能看到已通过资格的业务日与已 pin 的父策略事实。

    ``asof`` 可以省略：省略即**采纳 research run 自己的业务日**（那是唯一诚实的取值），
    显式给值则必须**完全一致**，不一致 fail closed。两种形态都不允许"拿旧研究生成新
    日期候选"——省略不是"用今天"，而是"用这条研究本身的日期"。
    """
    if not isinstance(research_run_id, str) or not research_run_id.strip():
        raise AICandidateGenerationError(REASON_RESEARCH_NOT_FOUND, "research_run_id is required")
    run_key = _research_run_identity(research_run_id)
    # R27 的 research ledger 不由 ``init_db`` / ``db_migrate`` 预建，而是由写路径惰性
    # 建表。读函数自己保证 schema 已建是本仓库既有约定（见
    # ``ai_review_service.get_slot_config``）；否则全新库上"查一条 run"会直接
    # ``no such table`` 崩成 5xx —— 那既不是"查无此行"，也不是 fail closed。
    ARR.ensure_schema(conn)
    run = ARR.get_run(conn, run_key)
    if run is None:
        # 绝不回退到 recent_runs(...)[0]：那是把"最新一次研究"变成隐式输入。
        raise AICandidateGenerationError(REASON_RESEARCH_NOT_FOUND, str(run_key))

    # AI 研究永远不是交易 authority。读取到的任何一行都必须是 research。
    if run.get("authority") != "research" or bool(run.get("is_authoritative")):
        raise AICandidateGenerationError(REASON_RESEARCH_AUTHORITY, research_run_id)

    # 资格只看 R27 canonical status。confidence 是 AI 自评，**不**是证据，因此不参与。
    #
    # 刻意**不**在这里重建 typed hypothesis：``ResearchEvidenceRef`` 没有公开构造器
    # （只有 owner adapter 的 factory 能签发），而 ``get_run`` 返回前已经做过
    # stored/embedded 一致性与 ``record_hash`` 重算 —— status 是 canonical 的，
    # 由本层重算一套反而会制造第二份 status authority。
    _require_supported(run)

    # 业务日必须与 research run 钉死：不允许历史思想自动流到新日期。
    #
    # 省略 asof ⇒ 采纳 run 自己的业务日（唯一诚实的取值），而**不是**"用今天"；
    # 显式给值 ⇒ 必须完全一致，否则 fail closed。
    run_asof = str(run.get("as_of"))
    if asof is None:
        asof = run_asof
    elif str(asof) != run_asof:
        raise AICandidateGenerationError(REASON_ASOF_MISMATCH, f"{run_asof} != {asof}")

    pin = SCV.pin_parent_strategy(
        conn, strategy_id=strategy_id, strategy_version=int(strategy_version),
        strategy_checksum=str(strategy_checksum), asof=str(asof),
        universe_spec=universe_spec,
        intended_market_regime=intended_market_regime or None,
        # constraints=None → 继承 exact pinned parent 那一版；AI 无权覆盖。
        constraints=None,
    )
    hypothesis_projection = run.get("hypothesis") or {}
    return {
        "research_run": dict(run),
        "hypothesis_projection": dict(hypothesis_projection),
        "parent_pin": pin,
        "research_provenance": _research_provenance(run),
        "hypothesis_id": str(run.get("hypothesis_id") or ""),
        "thesis": str(hypothesis_projection.get("thesis") or ""),
        # evidence_count 取自 R27 canonical 投影（它本身就是 len(hypothesis.evidence)），
        # 绝不采信 provider 自述的 evidence_count（AI 无权自述证据量）。
        "evidence_count": int(hypothesis_projection.get("evidence_count")
                              or len(hypothesis_projection.get("evidence") or ())),
        "asof": str(asof),
    }


def _adjustable_parameters(pin: SS.ParentStrategyPin) -> tuple[str, ...]:
    """The parent-declared, editable parameter ids (parent 自己的契约)。"""
    import strategy_parameter_schema as SPS
    schema = SPS.StrategyParameterSchema.from_dsl(pin.dsl_ast)
    return schema.editable


def generate_candidates_from_research(
    *,
    research_reader,
    writer,
    provider_config: Any,
    research_run_id: str,
    strategy_id: str,
    strategy_version: int,
    strategy_checksum: str,
    asof: str | None = None,
    universe_spec=None,
    intended_market_regime: str | None = None,
    max_candidates: int = SAIP.MAX_AI_CANDIDATES_PER_REQUEST,
    created_at: str | None = None,
) -> dict:
    """Full AI candidate generation: research → provider → R35-B batch.

    ``research_reader`` / ``writer`` 各自拥有自己的短连接（默认实现用同一张 paper DB），
    这样**网络调用发生在任何写事务之外**：LLM 可能要几十秒，绝不能拿着 SQLite 写锁
    等它返回。
    """
    if max_candidates > SAIP.MAX_AI_CANDIDATES_PER_REQUEST:
        # 只能收紧，不能放宽。
        raise AICandidateGenerationError(REASON_AI_CANDIDATE_CAP, str(max_candidates))

    # ── 1. 短读：所有 fail-closed gate 都在这里，网络之前 ──
    with research_reader() as conn:
        plan = plan_research_candidate_generation(
            conn, research_run_id=research_run_id, strategy_id=strategy_id,
            strategy_version=strategy_version, strategy_checksum=strategy_checksum,
            asof=asof, universe_spec=universe_spec,
            intended_market_regime=intended_market_regime,
        )
    pin: SS.ParentStrategyPin = plan["parent_pin"]
    run: dict = plan["research_run"]
    # 业务日的唯一真相在 plan 里（它是 exact research run 的 as_of）。写事务必须复用它，
    # 而不是复用调用方那个可能为 None 的入参。
    pinned_asof: str = plan["asof"]

    # ── 2/3. 网络调用在写事务之外 ──
    result = SAIPR.propose_candidate_space(
        provider_config=provider_config,
        hypothesis=plan["hypothesis_projection"], run=run,
        available_parameters=_adjustable_parameters(pin),
        max_candidates=int(max_candidates),
    )

    # ── 4. proposal → R35-B 原生 search space 的 material ──
    #
    # 这里**不**预构造 search space：DSL / parameter 合法性的唯一裁决点是 R35-B
    # （写事务内那次真实生成）。在这里再预校验一遍只会制造第二个裁决点。
    declared = SAIP.search_space_payload(result.proposal)
    parameters = declared.pop("parameter_variants", None)
    extra_material = {
        "ai_proposal": result.proposal.projection(),
        "research_run_id": run.get("id"),
        "proposal_contract_version": result.proposal.proposal_contract_version,
    }

    # ── 5/6. 短写事务：重新 pin exact parent → R35-B 生成 + 追加 ──
    #
    # R35-B 的拒绝（非法 AST / 越权参数 / 超限空间 / no-op）在这里被包装成**稳定
    # reason**：调用方与前端按 reason 分支，而不是去解析 R35-B 的文案。绝不"自动
    # 修复" —— 整体拒绝，且写事务回滚，所以不产生任何半批次。
    with writer() as conn:
        try:
            return SCV.generate_and_record_candidates(
                conn,
                strategy_id=strategy_id, strategy_version=int(strategy_version),
                strategy_checksum=strategy_checksum, asof=pinned_asof,
                generator_type=SG.BOUNDED_COMBINATION_GENERATOR,
                generator_version=SG.BOUNDED_COMBINATION_VERSION,
                parameter_variants=parameters or {},
                factor_slot=declared.get("factor_slot"),
                entry_slot=declared.get("entry_slot"),
                exit_slot=declared.get("exit_slot"),
                universe_spec=universe_spec,
                intended_market_regime=intended_market_regime or "",
                max_candidates=int(max_candidates),
                # evidence_count 来自 exact research run 的 canonical 投影；AI 无权自述。
                evidence_count=int(plan["evidence_count"]),
                hypothesis_id=plan["hypothesis_id"],
                research_provenance=plan["research_provenance"],
                # model_identity 是"把 hypothesis 变成 candidate proposal 的 provider"，
                # 与 research run 最初由哪个 model 产生是两件事。provider 没给可靠
                # version 就留空，不编造。
                model_identity=_provider_model_identity(provider_config),
                # constraints=None → 继承 exact pinned parent（只能收紧）。
                # R35-C **不开放** AI risk tuning。
                constraints=None,
                created_at=created_at,
                search_space_material=extra_material,
            )
        except (SS.SearchSpaceError, SC.CandidateValidationError,
                SG.StrategyGeneratorError, SCV.StrategyCandidateUnavailable) as exc:
            # 稳定 reason + 异常类型名，不回显可能很长的内部文案。
            raise AICandidateGenerationError(
                SAIP.Reason.SEARCH_SPACE_REJECTED, type(exc).__name__) from None


def _provider_model_identity(provider_config: Any) -> dict:
    """The **candidate-proposal** provider identity, never the research run's model."""
    if not isinstance(provider_config, Mapping):
        return {}
    identity = {}
    model = str(provider_config.get("model") or "").strip()
    if model:
        identity["model"] = model
    return identity
