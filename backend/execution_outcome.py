# -*- coding: utf-8 -*-
"""选股可执行性 → 执行可验证性的连接层（selection executable vs execution verified）。

本模块是 PR149（selection tradability contract）**之上**的一层扩展。它不改动
``selection_tradability`` 的任何判定口径，只把两个此前被混为一谈的概念显式拆开::

    selection_executable    按历史点时可成交性契约，理论上可以买（PR149 结论）
    execution_verified      按成交流水证据，实际上真的成交了（本层结论）

**必须允许** ``selection_executable=True`` 而 ``execution_verified=False``：
这正是"选出来 ≠ 能成交"的核心现场。反过来，
``execution_verified=True`` 而 ``selection_executable=False`` 也允许存在
（例如选股用日线证据判 blocked，而盘中真实盘口允许成交），但它会被单独计数，
**不**静默通过。

──────────────────────── 收益三层，不得互相替代 ────────────────────────

::

    market_return       市场层：按标签定义观察未来价格得到的收益（反事实，不需要成交）
    selection_return    选股层：**可执行选股子集**上的同一条市场标签收益
                        （仍然是反事实，来源标注为 executable_selection_market_label）
    execution_return    执行层：**真实成交往返**净收益（来源标注为 realized_fill_round_trip）

硬性规则：

* 没有成交证据 → ``execution_return`` **不成立**（``not_applicable``），
  其 ``maybe()`` 为 ``None``；
* 有成交但缺离场成交 → ``execution_return`` 为 ``unknown``（**不是** None 冒充零）；
* ``execution_return`` **一旦**是 ``known``，来源必须是 ``realized_fill_round_trip``，
  且 ``execution_verified`` 必须为真 —— 用 ``market_label_value`` 顶替执行收益
  会被 :func:`assert_no_market_label_substitution` 直接拒绝。

``market_label_value`` 是反事实标签，``execution_return`` 是成交结果。把前者写进
后者是"用市场标签冒充已实现收益"，本模块从来源与不变式两处同时禁止。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

try:  # ``backend`` on sys.path (production and ``cd backend`` test runs)
    import execution_evidence as EE
    import selection_tradability as ST
except ImportError:  # pragma: no cover - package-style import
    from . import execution_evidence as EE
    from . import selection_tradability as ST

__all__ = [
    "EXECUTION_BUCKETS",
    "EXECUTION_BUCKET_NEITHER",
    "EXECUTION_BUCKET_UNVERIFIED",
    "EXECUTION_BUCKET_VERIFIED",
    "EXECUTION_BUCKET_WITHOUT_SELECTION_PROOF",
    "EXECUTION_OUTCOME_VERSION",
    "RETURN_SOURCE_EXECUTABLE_SELECTION",
    "RETURN_SOURCE_MARKET_LABEL",
    "RETURN_SOURCE_REALIZED_FILLS",
    "ExecutionOutcomeError",
    "MarketLabelSubstitution",
    "SelectionExecutionOutcome",
    "assert_no_market_label_substitution",
    "build_execution_reality_report",
    "execution_bucket",
    "execution_eligibility",
    "link_execution_outcome",
    "realized_execution_return",
]

EXECUTION_OUTCOME_VERSION = "execution-outcome-v1"

RETURN_SOURCE_MARKET_LABEL = "market_label_value"
RETURN_SOURCE_EXECUTABLE_SELECTION = "executable_selection_market_label"
RETURN_SOURCE_REALIZED_FILLS = "realized_fill_round_trip"

#: execution 侧桶。``selection_bucket`` 仍然由 PR149 的 ``outcome_bucket`` 决定，
#: 两者是**正交**的，不合并成一套优先级。
EXECUTION_BUCKET_VERIFIED = "selection_executable_execution_verified"
EXECUTION_BUCKET_UNVERIFIED = "selection_executable_execution_unverified"
EXECUTION_BUCKET_WITHOUT_SELECTION_PROOF = "execution_verified_without_selection_proof"
EXECUTION_BUCKET_NEITHER = "neither_selection_nor_execution"
EXECUTION_BUCKETS = (
    EXECUTION_BUCKET_VERIFIED,
    EXECUTION_BUCKET_UNVERIFIED,
    EXECUTION_BUCKET_WITHOUT_SELECTION_PROOF,
    EXECUTION_BUCKET_NEITHER,
)


class ExecutionOutcomeError(ValueError):
    """执行结果契约被违反。"""


class MarketLabelSubstitution(ExecutionOutcomeError):
    """试图用市场反事实标签顶替真实执行收益。"""


@dataclass(frozen=True, slots=True)
class SelectionExecutionOutcome:
    """一条"选股可执行性 × 执行可验证性"的连接记录。"""

    sample_key: str
    security_code: str

    #: PR149 的结论原样透传：不改口径，不因执行失败而回写。
    selection_executable: bool = False
    #: 本层结论：入场与离场都拿到**真实成交证据**才算验证通过。
    execution_verified: bool = False

    selection_status: Optional[str] = None
    selection_bucket: Optional[str] = None
    fill_verdict: str = EE.FILL_VERDICT_UNKNOWN

    market_return: Any = None
    selection_return: Any = None
    execution_return: Any = None

    entry_evidence: Any = None
    exit_evidence: Any = None
    version: str = EXECUTION_OUTCOME_VERSION

    def as_dict(self) -> dict:
        payload = {
            "version": self.version,
            "sample_key": self.sample_key,
            "security_code": self.security_code,
            "selection_executable": self.selection_executable,
            "execution_verified": self.execution_verified,
            "selection_status": self.selection_status,
            "selection_bucket": self.selection_bucket,
            "fill_verdict": self.fill_verdict,
            "execution_bucket": execution_bucket(self),
        }
        for name in EE.RETURN_FIELDS:
            holder = getattr(self, name)
            payload[name] = holder.as_dict() if holder is not None else None
        return payload


def _finite(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _market_field(market_label_value: Any) -> Any:
    value = _finite(market_label_value)
    if value is None:
        return EE.EvidenceField.unknown(
            "market_return",
            detail="the market label value is missing or not finite",
        )
    return EE.EvidenceField.known(
        "market_return", value, source=RETURN_SOURCE_MARKET_LABEL,
        detail="market counterfactual label; independent of whether the trade was executable",
    )


def _selection_field(
    selection_executable: bool, market_label_value: Any, executable_return: Any = None
) -> Any:
    """选股层收益：可执行子集上的市场标签收益。**仍然不是成交收益**。"""
    if not selection_executable:
        return EE.EvidenceField.not_applicable(
            "selection_return",
            detail=(
                "the selection contract did not prove this sample executable, so no selection "
                "return is defined (the market label is still reported separately)"
            ),
        )
    value = _finite(executable_return if executable_return is not None else market_label_value)
    if value is None:
        return EE.EvidenceField.unknown(
            "selection_return",
            detail="the sample is executable but its market label value is missing",
        )
    return EE.EvidenceField.known(
        "selection_return", value, source=RETURN_SOURCE_EXECUTABLE_SELECTION,
        detail=(
            "market label restricted to the executable selection subset; this is a "
            "counterfactual, not a fill"
        ),
    )


def realized_execution_return(entry: Any, exit_evidence: Any = None) -> Any:
    """**真实成交往返**收益。这是 ``execution_return`` 唯一合法的来源。

    * 入场没有正成交 → ``not_applicable``：没有成交就没有执行收益，
      ``market_label_value`` **不得**用来顶替；
    * 有入场成交但缺离场成交 → ``unknown``：往返还没闭合，不猜；
    * 两侧数量不一致（部分平仓/多次建仓）→ ``unknown``：无法定义为一次干净往返。
    """
    if entry is None or not entry.has_positive_fill():
        return EE.EvidenceField.not_applicable(
            "execution_return",
            detail=(
                "no verified fill exists, so an execution return is not defined; a market "
                "label must never be substituted for it"
            ),
        )
    if exit_evidence is None or not exit_evidence.has_positive_fill():
        return EE.EvidenceField.unknown(
            "execution_return",
            detail=(
                "the entry filled but no exit fill evidence exists, so the round trip is not "
                "closed"
            ),
        )
    entry_qty = entry.filled_qty.require()
    exit_qty = exit_evidence.filled_qty.require()
    if entry_qty != exit_qty:
        return EE.EvidenceField.unknown(
            "execution_return",
            detail=(
                "entry and exit filled quantities differ, so the round trip is not a clean "
                f"single position ({entry_qty!r} vs {exit_qty!r})"
            ),
        )
    entry_price = entry.fill_price.require()
    exit_price = exit_evidence.fill_price.require()
    entry_fees = entry.fees.maybe()
    exit_fees = exit_evidence.fees.maybe()
    if entry_fees is None or exit_fees is None:
        return EE.EvidenceField.unknown(
            "execution_return",
            detail="fill fee evidence is missing on one leg of the round trip",
        )
    cost = entry_qty * entry_price + float(entry_fees)
    proceeds = exit_qty * exit_price - float(exit_fees)
    if cost <= 0:
        return EE.EvidenceField.unknown(
            "execution_return", detail="the entry cost basis is non-positive"
        )
    return EE.EvidenceField.known(
        "execution_return", (proceeds - cost) / cost, source=RETURN_SOURCE_REALIZED_FILLS,
        detail=(
            "net of fees on both legs, computed from the confirmed fill quantities and prices"
        ),
    )


def link_execution_outcome(
    selection_outcome: Any,
    entry_evidence: Any = None,
    exit_evidence: Any = None,
    *,
    sample_key: Optional[str] = None,
    security_code: Optional[str] = None,
) -> SelectionExecutionOutcome:
    """连接 PR149 的 selection outcome 与真实成交证据。

    ``selection_outcome`` 是 :class:`selection_tradability.ExecutableSelectionOutcome`
    （或任何带 ``selected`` / ``executable`` / ``entry_status`` 的对象）。
    它的结论**原样透传**，本函数不回写、不重判 selection。
    """
    selection_status = getattr(selection_outcome, "entry_status", None)
    selection_executable = bool(getattr(selection_outcome, "executable", False))
    market_label_value = getattr(selection_outcome, "market_label_value", None)
    executable_return = getattr(selection_outcome, "executable_return", None)

    market_field = _market_field(market_label_value)
    selection_field = _selection_field(
        selection_executable, market_label_value, executable_return
    )

    entry = entry_evidence
    exit_side = exit_evidence
    entry_verified = bool(entry is not None and entry.proves_fill())
    exit_verified = bool(exit_side is not None and exit_side.proves_fill())
    execution_verified = bool(entry_verified and exit_verified)

    #: ``fill_verdict`` 描述**入场腿**的成交事实。它与 ``execution_verified``
    #: 不是同一个问题：入场全成而离场未平仓时是 ``fill_verified`` +
    #: ``execution_verified=False``，这是"买进了但往返还没闭合"，不是矛盾。
    verdict = entry.fill_verdict_value() if entry is not None else EE.FILL_VERDICT_UNKNOWN

    execution_field = realized_execution_return(entry, exit_side)

    outcome = SelectionExecutionOutcome(
        sample_key=str(
            sample_key if sample_key is not None else getattr(selection_outcome, "sample_key", "")
        ),
        security_code=str(
            security_code
            if security_code is not None
            else getattr(selection_outcome, "security_code", "")
        ),
        selection_executable=selection_executable,
        execution_verified=execution_verified,
        selection_status=selection_status,
        selection_bucket=(
            ST.outcome_bucket(selection_outcome)
            if _is_tradability_outcome(selection_outcome) else None
        ),
        fill_verdict=verdict,
        market_return=market_field,
        selection_return=selection_field,
        execution_return=execution_field,
        entry_evidence=entry,
        exit_evidence=exit_side,
    )
    assert_no_market_label_substitution(outcome)
    return outcome


def _is_tradability_outcome(value: Any) -> bool:
    return all(hasattr(value, name) for name in ("selected", "entry_status", "executable"))


def execution_bucket(outcome: SelectionExecutionOutcome) -> str:
    """execution 侧桶的**唯一**分类。两个布尔维度，四种组合，互斥且穷尽。"""
    if outcome.selection_executable and outcome.execution_verified:
        return EXECUTION_BUCKET_VERIFIED
    if outcome.selection_executable:
        return EXECUTION_BUCKET_UNVERIFIED
    if outcome.execution_verified:
        return EXECUTION_BUCKET_WITHOUT_SELECTION_PROOF
    return EXECUTION_BUCKET_NEITHER


def assert_no_market_label_substitution(outcome: SelectionExecutionOutcome) -> None:
    """拒绝"用市场标签冒充执行收益"。

    两条不变式（**单向**，因为"往返已成交"不保证"往返可算收益"）：

    1. ``execution_return`` 为 ``known`` ⟹ ``execution_verified`` 为真：
       没有验证过的往返，不允许携带已实现收益；
    2. ``execution_return`` 为 ``known`` ⟹ 来源是
       :data:`RETURN_SOURCE_REALIZED_FILLS`。

    反向不成立，也**不应**成立：两腿都成交但数量不等（部分平仓）或费用证据缺失时，
    ``execution_verified`` 为真而 ``execution_return`` 为 ``unknown`` —— 这是
    "成交确认了、收益暂时算不出来"，必须在字段里如实说明，而不是编一个数。
    """
    holder = outcome.execution_return
    if holder is None:
        return
    if holder.is_known:
        if holder.source != RETURN_SOURCE_REALIZED_FILLS:
            raise MarketLabelSubstitution(
                "execution_return is known but its source is "
                f"{holder.source!r}; a market label must never be substituted for a fill"
            )
        if not outcome.execution_verified:
            raise MarketLabelSubstitution(
                "execution_return is known while execution_verified is false; "
                "an unverified fill must never carry an execution return"
            )


def build_execution_reality_report(outcomes: Sequence[Any]) -> dict:
    """执行真实性的审计报告。每条记录都落在某个 execution 桶里，不静默丢行。"""
    buckets = {key: 0 for key in EXECUTION_BUCKETS}
    verdicts: dict = {}
    inconsistencies: dict = {}
    selection_executable = 0
    execution_verified = 0
    for outcome in outcomes or ():
        buckets[execution_bucket(outcome)] += 1
        verdicts[outcome.fill_verdict] = verdicts.get(outcome.fill_verdict, 0) + 1
        if outcome.selection_executable:
            selection_executable += 1
        if outcome.execution_verified:
            execution_verified += 1
        for evidence in (outcome.entry_evidence, outcome.exit_evidence):
            for code in getattr(evidence, "inconsistencies", lambda: ())():
                inconsistencies[code] = inconsistencies.get(code, 0) + 1
    total = len(outcomes or ())
    return {
        "version": EXECUTION_OUTCOME_VERSION,
        "outcomes": total,
        "selection_executable": selection_executable,
        "execution_verified": execution_verified,
        **buckets,
        "fill_verdict_counts": verdicts,
        "inconsistency_counts": inconsistencies,
        "selection_fact_preserved": True,
        "execution_return_requires_fill": True,
        "accounted": sum(buckets.values()) == total,
    }


def execution_eligibility(
    outcomes: Sequence[Any],
    *,
    require_selection: bool = True,
) -> dict:
    """挑出"真的有成交"的样本。

    ``require_selection=True``（默认）只保留 selection 与 execution **都**成立的
    样本：生产评估用这一档。``require_selection=False`` 额外纳入
    "执行验证过但选股没证明"的样本，供复盘"选股漏了什么"。
    """
    selected = [item for item in (outcomes or ()) if item.execution_verified]
    if require_selection:
        selected = [item for item in selected if item.selection_executable]
    return {
        "version": EXECUTION_OUTCOME_VERSION,
        "require_selection": bool(require_selection),
        "verified_executions": len([o for o in (outcomes or ()) if o.execution_verified]),
        "eligible": len(selected),
        "sample_keys": [item.sample_key for item in selected],
        "returns": [item.execution_return.maybe() for item in selected],
    }
