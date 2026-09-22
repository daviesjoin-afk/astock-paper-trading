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
* research 路径把 current immutable head 的读取**挪到计算之前**
  (`research_version_pin`，每个 run 恰好读一次)，再由
  `research_provenance` 用「计算前的 pin + 计算后的 as-of」组装 provenance——
  计算过程中发生的策略升级不会改变该 run 的戳；
* signal 的周期归属由**调用方显式传入** (`signal_cycle_provenance`)：本模块不查
  `paper_accounts`、不解析 active/current cycle，只认 cycle pin，缺 pin 即
  `SignalCycleUnprovable`；
* signal-linked order 的血缘由 `signal_order_provenance` 独家解析——
  exact signal + 身份校验 + 完整 provenance 校验，任何一项不成立即
  `SignalOrderUnprovable`，绝不 current-fill；
* 历史读取只读 persisted stamp，**绝不**再调用 `SR.get_version(strategy_id)`
  去解释一个已经存在的 run。

这里没有 SQL、没有 ``MAX(version)``、没有 ``ORDER BY version DESC``、没有
``date.today()``：那三种写法都被架构门禁静态拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import strategy_registry as SR
import strategy_selection_provenance as SP

__all__ = [
    "SignalCycleUnprovable", "SignalOrderProvenance", "SignalOrderUnprovable",
    "SignalStaleContext", "SignalWriteContext",
    "cycle_provenance", "research_provenance", "research_version_pin",
    "reading_from_run", "signal_cycle_provenance", "signal_order_provenance",
    "signal_write_context", "verify_immutable", "model_family_reading",
    "resolve_asof_day",
]


class SignalCycleUnprovable(Exception):
    """A signal's cycle ownership cannot be proven, so nothing may be written.

    Carries the ids so callers can record a diagnosis instead of guessing. This
    is deliberately distinct from a *temporary* condition: a missing cycle pin is
    permanent for that cycle, so the caller must not retry it into existence.
    """

    event = "signal_provenance_unprovable"

    def __init__(self, account_id, cycle_id, detail):
        self.account_id = str(account_id or "")
        self.cycle_id = cycle_id
        self.detail = str(detail or "")
        super().__init__(
            f"signal cycle provenance unprovable for account={self.account_id} "
            f"cycle={self.cycle_id}: {self.detail}"
        )


def signal_cycle_provenance(conn, account_id, *, cycle_id):
    """Resolve one signal's immutable ``(cycle_id, strategy stamp)`` — or refuse.

    A signal's provenance is a write-time fact and must answer **both** questions
    at once: which cycle did this decision belong to, and which immutable strategy
    version produced it. Resolving them together is what makes "cycle 8's signal
    stamped with cycle 9's strategy version" unrepresentable.

    The cycle is **supplied by the caller**, and that is deliberate. The caller
    has already read and checked it inside the very transaction that is about to
    write the signal (``generate_signals`` re-reads ``paper_accounts`` before
    deciding; ``_bootstrap_signals_for_today`` already holds the resolved cycle),
    so it is a fact of that transaction. Letting this function re-derive it from
    ``paper_accounts`` would make the writer's cycle binding both hidden and
    second-guessed: the same transaction could resolve "the cycle" twice and get
    two different answers.

    Above all this function must never *search* for a cycle — no active/latest/
    current-cycle lookup, and no ``paper_accounts`` read. The only authority here
    is the cycle **pin**: when the pin is missing this **fails closed**, because
    falling back to a legacy binding and finally to a user strategy's *current
    head* would silently reinterpret a historical decision with today's strategy,
    which is exactly what R23 forbids.
    """
    requested = SP.canonical_cycle_id(cycle_id)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)
    if stamp is None:
        raise SignalCycleUnprovable(
            account_id, requested, "cycle has no pinned immutable strategy version"
        )
    return requested, tuple(stamp)


@dataclass(frozen=True)
class SignalWriteContext:
    """The frozen ``(cycle, strategy stamp)`` a signal batch commits under.

    Frozen because it is a statement about *which world* the candidates in this
    batch were built in — not a live handle the writer may refresh. ``cycle_id``
    is the caller's already-resolved cycle; the stamp is that cycle's immutable
    pin. It carries no other authority, so it cannot be used to re-derive a cycle.
    """

    account_id: str
    cycle_id: int
    strategy_id: str
    strategy_version: int
    strategy_checksum: str
    asof_day: str

    def __post_init__(self):
        object.__setattr__(self, "account_id", str(self.account_id or ""))
        object.__setattr__(self, "cycle_id", int(self.cycle_id))
        object.__setattr__(self, "strategy_id", str(self.strategy_id))
        object.__setattr__(self, "strategy_version", int(self.strategy_version))
        object.__setattr__(self, "strategy_checksum", str(self.strategy_checksum))
        object.__setattr__(self, "asof_day", str(self.asof_day or ""))

    @property
    def stamp(self) -> tuple[str, int, str]:
        return (self.strategy_id, self.strategy_version, self.strategy_checksum)


def signal_write_context(conn, account_id, *, cycle_id, account_cycle_id,
                         asof_day="") -> SignalWriteContext:
    """Freeze one signal batch's write context — or refuse because it went stale.

    ``cycle_id`` is the cycle the caller captured **before** the candidates were
    built; ``account_cycle_id`` is what the committing transaction observes now.
    Two questions are answered against that captured cycle:

    * is the account still bound to it? If the two differ, a rollover landed in
      the window between candidate build and commit, so the batch describes a
      world that no longer exists and is dropped as :class:`SignalStaleContext` —
      never migrated onto the new cycle;
    * does it have an immutable pin? A missing pin fails closed via
      :class:`SignalCycleUnprovable`, exactly as in
      :func:`signal_cycle_provenance`.

    Both cycle values come from the caller, so this module performs **no**
    ``paper_accounts`` read and never *derives* a cycle: it only compares the two
    facts the caller already holds. That keeps the writer's cycle binding a
    single visible decision while making a mid-batch rollover unrepresentable.
    """
    requested = SP.canonical_cycle_id(cycle_id)
    if requested is None:
        raise SignalCycleUnprovable(
            account_id, None, "signal requires an explicit canonical cycle id"
        )
    observed = SP.canonical_cycle_id(account_cycle_id)
    if observed != requested:
        raise SignalStaleContext(account_id, requested,
                                 observed if observed is not None else "none")
    stamp = SR.cycle_stamp_for_account(conn, account_id, cycle_id=requested)
    if stamp is None:
        raise SignalCycleUnprovable(
            account_id, requested, "cycle has no pinned immutable strategy version"
        )
    return SignalWriteContext(
        account_id=account_id, cycle_id=requested,
        strategy_id=stamp[0], strategy_version=stamp[1], strategy_checksum=stamp[2],
        asof_day=asof_day,
    )


def signal_write_context_or_error(conn, account_id, *, cycle_id, account_cycle_id,
                                  asof_day=""):
    """Non-raising form of :func:`signal_write_context` for facade callers.

    Returns ``(context, None)`` on success and ``(None, exc)`` when the batch must
    be dropped — the caller records ``exc.event`` / ``exc.detail`` in its audit and
    skips the batch. Both failure modes stay distinguishable (the exception *type*
    is the diagnosis), so the facade never has to re-derive why.
    """
    try:
        return signal_write_context(
            conn, account_id, cycle_id=cycle_id,
            account_cycle_id=account_cycle_id, asof_day=asof_day,
        ), None
    except (SignalCycleUnprovable, SignalStaleContext) as exc:
        return None, exc


class SignalStaleContext(Exception):
    """A signal batch's cycle context changed between candidate build and commit.

    ``generate_signals`` / ``_bootstrap_signals_for_today`` fetch candidates and
    provider evidence while holding no write transaction, so a cycle rollover can
    land in that window. The account row the candidates were built from then
    belongs to a *different* cycle than the one being committed into. Committing
    anyway would stamp old-world candidates with the new cycle's immutable
    version — a current-state reinterpretation of a past decision. The batch is
    dropped instead, with the ids recorded for audit.
    """

    event = "signal_stale_cycle_context"

    def __init__(self, account_id, captured_cycle_id, declared_cycle_id):
        self.account_id = str(account_id or "")
        self.captured_cycle_id = captured_cycle_id
        self.declared_cycle_id = declared_cycle_id
        self.detail = (
            f"stale_context: candidates belong to cycle {captured_cycle_id}, "
            f"account is now bound to cycle {declared_cycle_id}"
        )
        super().__init__(
            f"stale signal cycle context for account={self.account_id} "
            f"captured={self.captured_cycle_id} declared={self.declared_cycle_id}"
        )


class SignalOrderUnprovable(Exception):
    """A signal-linked order cannot inherit a provable provenance — refuse it.

    A signal whose persisted provenance is incomplete (a legacy row with no
    version/checksum) or absent (a dangling ``signal_id``) does **not** carry
    demonstrable execution provenance. Such a signal must never be *completed*
    from current state: doing so would stamp today's strategy version onto a
    decision that was made without one, which is the exact re-interpretation R23
    forbids. Carries the ids so the caller can record a diagnosis.
    """

    def __init__(self, account_id, signal_id, detail):
        self.account_id = str(account_id or "")
        self.signal_id = signal_id
        self.detail = str(detail or "")
        super().__init__(
            f"signal order provenance unprovable for account={self.account_id} "
            f"signal={self.signal_id}: {self.detail}"
        )


@dataclass(frozen=True)
class SignalOrderProvenance:
    """One signal-linked order's proven ``(cycle_id, strategy stamp)`` lineage.

    Frozen on purpose: this is a resolved *fact* handed to an order writer, not
    a mutable carrier that a writer could partially overwrite.
    """

    signal_id: int
    account_id: str
    cycle_id: int
    strategy_id: str
    strategy_version: int
    strategy_checksum: str

    def __post_init__(self):
        object.__setattr__(self, "signal_id", int(self.signal_id))
        object.__setattr__(self, "account_id", str(self.account_id or ""))
        object.__setattr__(self, "cycle_id", int(self.cycle_id))
        object.__setattr__(self, "strategy_id", str(self.strategy_id))
        object.__setattr__(self, "strategy_version", int(self.strategy_version))
        object.__setattr__(self, "strategy_checksum", str(self.strategy_checksum))

    @property
    def stamp(self) -> tuple[str, int, str]:
        return (self.strategy_id, self.strategy_version, self.strategy_checksum)


def signal_order_provenance(conn, *, signal_id, account_id,
                            expected_cycle_id=None) -> SignalOrderProvenance:
    """Resolve the **exact** provenance a signal-linked order must inherit.

    This is the single owner of that resolution: every order writer calls it
    instead of copying the "look up the signal, validate it, validate the stamp"
    sequence. Responsibilities are deliberately narrow — exact signal lookup,
    identity validation, complete-provenance validation. It writes no order, no
    fill, no risk decision and no cash movement.

    The rules are strict by design:

    * the signal row must exist;
    * it must belong to the requested account;
    * its persisted stamp must be **complete** (``strategy_id``,
      ``strategy_version`` >= 1, non-empty ``strategy_checksum``);
    * its ``cycle_id`` must be a canonical cycle id.

    Any failure raises :class:`SignalOrderUnprovable`. There is deliberately **no**
    fallback to ``stamp_for_account``, no current-head read, no current/active
    cycle: a signal that cannot prove its own lineage yields no order.

    When ``expected_cycle_id`` is supplied, the signal's cycle must **equal** it.
    A mismatch is a refusal, never a re-binding — the caller's execution cycle and
    the signal's cycle disagree, so neither "order cycle B + signal v1" nor "order
    cycle B + current v2" is representable.
    """
    try:
        requested_signal = int(signal_id)
    except (TypeError, ValueError):
        raise SignalOrderUnprovable(
            account_id, signal_id, "signal id is missing or not an integer"
        ) from None
    row = conn.execute(
        """SELECT account_id,strategy_id,strategy_version,strategy_checksum,cycle_id
           FROM paper_signals WHERE id=?""",
        (requested_signal,),
    ).fetchone()
    if row is None:
        raise SignalOrderUnprovable(
            account_id, requested_signal, "signal row does not exist"
        )
    row_account = row["account_id"] if hasattr(row, "keys") else row[0]
    strategy_id = row["strategy_id"] if hasattr(row, "keys") else row[1]
    strategy_version = row["strategy_version"] if hasattr(row, "keys") else row[2]
    strategy_checksum = row["strategy_checksum"] if hasattr(row, "keys") else row[3]
    signal_cycle = row["cycle_id"] if hasattr(row, "keys") else row[4]
    if str(row_account or "") != str(account_id or ""):
        raise SignalOrderUnprovable(
            account_id, requested_signal,
            f"signal belongs to account {row_account!r}",
        )
    strategy_id = str(strategy_id or "").strip()
    strategy_checksum = str(strategy_checksum or "").strip()
    try:
        version = int(strategy_version) if strategy_version is not None else None
    except (TypeError, ValueError):
        version = None
    if not strategy_id or version is None or version < 1 or not strategy_checksum:
        raise SignalOrderUnprovable(
            account_id, requested_signal,
            "signal carries no complete immutable provenance "
            "(legacy or partially stamped)",
        )
    signal_cycle = SP.canonical_cycle_id(signal_cycle)
    if signal_cycle is None:
        raise SignalOrderUnprovable(
            account_id, requested_signal,
            "signal has no canonical cycle ownership",
        )
    if expected_cycle_id is not None:
        expected = SP.canonical_cycle_id(expected_cycle_id)
        if expected is None:
            raise SignalOrderUnprovable(
                account_id, requested_signal,
                "caller supplied a non-canonical expected cycle id",
            )
        if signal_cycle != expected:
            raise SignalOrderUnprovable(
                account_id, requested_signal,
                f"signal cycle {signal_cycle} does not match execution cycle {expected}",
            )
    return SignalOrderProvenance(
        signal_id=requested_signal, account_id=account_id, cycle_id=signal_cycle,
        strategy_id=strategy_id, strategy_version=version,
        strategy_checksum=strategy_checksum,
    )


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


@dataclass(frozen=True)
class ResearchVersionPin:
    """The immutable version of a strategy, pinned **before** a run computes.

    ``version`` / ``checksum`` are what the computation actually ran under, so a
    pin is only meaningful if it is taken before the work starts — see
    :func:`research_version_pin`.

    **What "ran under" means here (honest scope).** For family A the selection
    semantics are realised by ``model_id`` (``paper_selection.STRATEGY_MODEL`` →
    ``strategies.PAPER_WEIGHTS`` / ``_paper_conditions``), i.e. by code, not by
    the immutable definition row. The pin therefore records **which published
    strategy identity the run is attributed to** — an identity/release fact, not
    an input the scoring consumed. It is still the correct authority for the
    provenance columns, and pinning it early is still required: otherwise a
    mid-run publication would be credited with a result it did not produce. What
    this pin must *not* be read as is a claim that editing the definition changes
    selection output; that binding does not exist in this path.
    """

    strategy_id: str
    version: int
    checksum: str

    def __post_init__(self):
        object.__setattr__(self, "strategy_id", str(self.strategy_id or ""))
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "checksum", str(self.checksum or ""))


def research_version_pin(conn, strategy_id: str) -> ResearchVersionPin | None:
    """Read the strategy's immutable head **once**, for a run about to compute.

    This is a **separate, earlier** entry point on purpose. ``research_provenance``
    is called after the computation with the as-of it resolved, and if *it* were
    the only reader then a strategy edit landing mid-run would be recorded as the
    version that produced a result it never produced (T0 v1 → T2 upgrade → T4 the
    provenance resolver reads v2). Pinning here, before ``_run_one``, makes the
    recorded version the one the work actually started under.

    Returns ``None`` when the strategy has no immutable version at all; the caller
    records an honest ``unknown`` rather than inventing one.
    """
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return None
    head = SR.get_version(strategy_id, conn=conn)
    if head is None:
        return None
    return ResearchVersionPin(
        strategy_id=head.strategy_id, version=head.version, checksum=head.checksum,
    )


def research_provenance(conn, strategy_id: str, *, asof_day: Any,
                        scope: str = SP.SCOPE_RESEARCH,
                        pin: ResearchVersionPin | None = None) -> SP.ProvenanceReading:
    """Assemble one research run's provenance from a **pre-computation** pin.

    ``conn`` must be a connection to the **registry** database (the one that
    holds ``paper_strategy_versions`` and the head table) — for family A that is
    the ledger registry, not the research DB.

    A ``pin`` is required: the immutable version must have been read *before* the
    computation ran (see :func:`research_version_pin`). This function therefore
    never reads the current head itself — that read happens exactly once per run,
    in the caller, in the right order. Without a pin the result is an honest
    ``unknown``: a run whose version cannot be attributed is not silently stamped
    with whatever the head happens to be now.
    """
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return _unproven("no strategy id supplied")
    if SP.canonical_day(asof_day) is None:
        return _unproven("missing or invalid as-of day", strategy_id)
    if pin is None:
        return _unproven(
            "no immutable version was pinned before the run computed", strategy_id,
        )
    try:
        provenance = SP.StrategySelectionProvenance(
            strategy_id=pin.strategy_id, strategy_version=pin.version,
            strategy_checksum=pin.checksum, asof_day=asof_day, scope=scope,
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
