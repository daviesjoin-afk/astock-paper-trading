"""R23 —— Strategy / Selection Provenance 的**薄 adapter**（唯一版本解析入口）。

依赖方向（规格 §16）：

    paper_selection / selection_tracking / paper_trading
        ↓
    strategy_selection_resolver  →  strategy_selection_provenance（纯契约）
        ↓
    strategy_registry（immutable version / cycle pin 的唯一 authority）
    strategy_runtime（cycle runtime context）

本模块**不**重新实现任何版本解析：

* cycle-owned 路径只调用 :func:`strategy_registry.cycle_version_for_account`
  （= ``cycle_stamp_for_account`` 严格 pin + ``get_version`` 校验 checksum），
  没有 legacy binding 回退，也没有 current-head 回退；
* research 路径只在**创建新 run 的那一刻**读一次 current immutable head，读完即
 持久化；
* 历史读取只读 persisted stamp，**绝不**再调用 ``SR.get_version(strategy_id)``
  去解释一个已经存在的 run；
* signal / order 的写入戳（:func:`signal_cycle_provenance`）同样只认 cycle pin，
  缺 pin 即 ``SignalCycleUnprovable``，不走 legacy / current-head 兜底。

这里没有 SQL、没有 ``MAX(version)``、没有 ``ORDER BY version DESC``、没有
``date.today()``：那三种写法都被架构门禁静态拒绝。
"""
from __future__ import annotations

from typing import Any

import strategy_registry as SR
import strategy_selection_provenance as SP

__all__ = [
    "SignalCycleUnprovable", "cycle_provenance", "research_provenance",
    "reading_from_run", "signal_cycle_provenance", "verify_immutable",
    "model_family_reading", "resolve_asof_day",
]


class SignalCycleUnprovable(Exception):
    """A signal's cycle ownership cannot be proven, so nothing may be written.

    Carries the ids so callers can record a diagnosis instead of guessing. This
    is deliberately distinct from a *temporary* condition: a missing cycle pin is
    permanent for that cycle, so the caller must not retry it into existence.
    """

    def __init__(self, account_id, cycle_id, detail):
        self.account_id = str(account_id or "")
        self.cycle_id = cycle_id
        self.detail = str(detail or "")
        super().__init__(
            f"signal cycle provenance unprovable for account={self.account_id} "
            f"cycle={self.cycle_id}: {self.detail}"
        )


def signal_cycle_provenance(conn, account_id):
    """Resolve one signal's immutable ``(cycle_id, strategy stamp)`` — or refuse.

    A signal's provenance is a write-time fact and must answer **both** questions
    at once: which cycle did this decision belong to, and which immutable strategy
    version produced it. Resolving them together is what makes "cycle 8's signal
    stamped with cycle 9's strategy version" unrepresentable.

    The cycle is read from the account row (an account is *economically* bound to
    exactly one cycle while it is in the participant set), and the version must
    come from that cycle's **pin**. When the pin is missing this **fails closed**:
    falling back to a legacy binding and finally to a user strategy's *current
    head* would silently reinterpret a historical decision with today's strategy,
    which is exactly what R23 forbids.
    """
    row = conn.execute(
        "SELECT cycle_id FROM paper_accounts WHERE id=?", (str(account_id),)
    ).fetchone()
    cycle_id = None
    if row is not None:
        try:
            cycle_id = int(row[0]) if row[0] is not None else None
        except (TypeError, ValueError, IndexError):
            cycle_id = None
    if cycle_id is None:
        raise SignalCycleUnprovable(account_id, None, "account has no durable cycle")
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=cycle_id)
    if stamp is None:
        raise SignalCycleUnprovable(
            account_id, cycle_id, "cycle has no pinned immutable strategy version"
        )
    return cycle_id, tuple(stamp)


def _unproven(detail: str, subject: str = "") -> SP.ProvenanceReading:
    return SP.ProvenanceReading(None, SP.STATUS_UNKNOWN, detail, subject)


def cycle_provenance(conn, strategy_id: str, *, cycle_id: Any, asof_day: Any,
                     scope: str = SP.SCOPE_CYCLE) -> SP.ProvenanceReading:
    """Resolve the immutable version pinned to **one explicit cycle** (strict).

    ``conn`` must be a connection to the **registry/ledger** database (the one
    that holds ``paper_cycle_strategy_versions``). A missing pin, an unknown
    account or an absent cycle id all fail closed: they return ``unknown``
    rather than adopting a legacy binding or today's head. Callers must not
    substitute a default.
    """
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return _unproven("no strategy id supplied")
    if SP.canonical_cycle_id(cycle_id) is None:
        return _unproven("cycle scope requires an explicit cycle id", strategy_id)
    if SP.canonical_day(asof_day) is None:
        return _unproven("missing or invalid as-of day", strategy_id)
    # 唯一权威：cycle pin。``cycle_version_for_account`` 同时校验 checksum，
    # 因此「pin 指向一个不存在的 checksum」也在这里被拒绝 —— 它以
    # ``ValueError`` 报错（这是 Registry 的既有契约），必须转成 fail-closed
    # 的 reading，绝不让异常穿透到调用方，也绝不静默纠正成 Registry 里的值。
    try:
        version = SR.cycle_version_for_account(conn, strategy_id, cycle_id=int(cycle_id))
    except ValueError as exc:
        return _unproven(f"cycle {int(cycle_id)} pin is not resolvable: {exc}", strategy_id)
    if version is None:
        return _unproven(
            f"cycle {int(cycle_id)} has no pinned immutable version for {strategy_id}",
            strategy_id,
        )
    try:
        provenance = SP.StrategySelectionProvenance(
            strategy_id=version.strategy_id, strategy_version=version.version,
            strategy_checksum=version.checksum, asof_day=asof_day, scope=scope,
            cycle_id=int(cycle_id),
        )
    except ValueError as exc:  # pragma: no cover - 契约校验兜底
        return _unproven(str(exc), strategy_id)
    return SP.ProvenanceReading(provenance, SP.STATUS_VERIFIED, "", strategy_id)


def research_provenance(conn, strategy_id: str, *, asof_day: Any,
                        scope: str = SP.SCOPE_RESEARCH) -> SP.ProvenanceReading:
    """Pin the **current immutable head once**, at run-creation time only.

    ``conn`` must be a connection to the **registry** database (the one that
    holds ``paper_strategy_versions`` and the head table) — for family A that is
    the ledger registry, not the research DB.

    This is the single place allowed to read "now" for a research run. The
    result must be persisted immediately; every later read goes through
    :func:`reading_from_run`.
    """
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return _unproven("no strategy id supplied")
    if SP.canonical_day(asof_day) is None:
        return _unproven("missing or invalid as-of day", strategy_id)
    head = SR.get_version(strategy_id, conn=conn)
    if head is None:
        return _unproven(f"strategy {strategy_id} has no immutable version", strategy_id)
    try:
        provenance = SP.StrategySelectionProvenance(
            strategy_id=head.strategy_id, strategy_version=head.version,
            strategy_checksum=head.checksum, asof_day=asof_day, scope=scope,
        )
    except ValueError as exc:  # pragma: no cover - 契约校验兜底
        return _unproven(str(exc), strategy_id)
    return SP.ProvenanceReading(provenance, SP.STATUS_VERIFIED, "", strategy_id)


def model_family_reading(model_id: str, *, detail: str = "") -> SP.ProvenanceReading:
    """A run whose "strategy" is a model family, not a registered strategy.

    ``strategy_id`` / ``version`` / ``checksum`` are honestly **not applicable**
    here — the family ids (``three_day`` / ``five_day`` / ``ten_day`` / …) do not
    exist in ``strategy_definitions``, and inventing a mapping would be exactly
    the "no invented provenance" failure R23 forbids.
    """
    model_id = str(model_id or "").strip()
    return SP.not_applicable_reading(
        model_id, detail=detail or "model family is not a registered strategy id",
    )


def reading_from_run(row: Any, *, default_scope: str | None = None) -> SP.ProvenanceReading:
    """Read a **persisted** run row back. Never resolves the Registry."""
    return SP.reading_from_row(row, default_scope=default_scope)


def verify_immutable(conn, provenance: SP.StrategySelectionProvenance | None) -> bool:
    """Re-check a persisted stamp against the immutable version table.

    Used to prove that a stored ``(strategy_id, version, checksum)`` triple is
    still resolvable to that *exact* version — a checksum that does not match is
    rejected, never silently corrected. The Registry reports a mismatch by
    raising ``ValueError``; that is a negative answer here, not a crash.
    """
    if provenance is None:
        return False
    try:
        found = SR.get_version(
            provenance.strategy_id, provenance.strategy_version,
            checksum=provenance.strategy_checksum, conn=conn,
        )
    except ValueError:
        return False
    return found is not None


def resolve_asof_day(explicit: Any, declared=None) -> str:
    """Re-exported so callers depend on one module for the whole contract."""
    return SP.resolve_asof_day(explicit, declared)
