"""Prepare one trading candidate scan before opening the ledger writer.

This module owns collection order and per-round reuse. It cannot create signals,
approve orders, select a cycle or commit trades. The facade retains those checks.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, ContextManager

from paper_candidate_inputs import CandidateScanInputs
import paper_timing as PTM


@dataclass(frozen=True)
class EvidencePorts:
    open_readonly: Callable[[], ContextManager]
    cooled_candidates: Callable
    news: Callable
    sector_flow: Callable
    quotes: Callable


@dataclass(frozen=True)
class PreparationPorts:
    candidate_rows: Callable
    prioritize: Callable
    microstructures: Callable
    collect_evidence: Callable


@dataclass(frozen=True)
class PreparedCandidates:
    rows_by_account: dict
    microstructures: dict
    evidence_by_account: dict


def collect_evidence(accounts, precomputed_candidates, day, *, ports: EvidencePorts):
    """Read cooldowns, then news, then one quote batch; isolate account outputs."""
    evidence_by_account = {}
    for account in accounts:
        rows, meta = precomputed_candidates.get(account["id"], ([], {}))
        if meta.get("blocked"):
            continue
        with ports.open_readonly() as conn, PTM.phase("candidate.cooldown_read"):
            cooled = ports.cooled_candidates(conn, account["id"], rows, day)
        names = {pick["code"]: pick.get("name") or pick["code"] for pick in cooled["candidates"]}
        try:
            with PTM.phase("candidate.news_source"):
                news = ports.news(names) if names else []
        except Exception:
            news = []
        evidence_by_account[account["id"]] = {"cooled": cooled, "names": names, "news": news}
    quote_codes = list(dict.fromkeys(
        code for evidence in evidence_by_account.values() for code in evidence["names"]
    ))
    try:
        with PTM.phase("candidate.sector_source"):
            sector_flow = ports.sector_flow("industry") if evidence_by_account else []
    except Exception:
        sector_flow = []
    try:
        # Quotes stay last: slow news must not age execution evidence first.
        with PTM.phase("candidate.quote_source"):
            quotes = (ports.quotes(quote_codes) or {}) if quote_codes else {}
    except Exception:
        quotes = {}
    if not isinstance(quotes, dict):
        quotes = {}
    for evidence in evidence_by_account.values():
        evidence["quotes"] = {code: deepcopy(quotes[code]) for code in evidence["names"] if code in quotes}
        evidence["sector_flow"] = deepcopy(sector_flow)
    return evidence_by_account


def prepare(accounts, *, factor_day, day, market, sector_rows, live_universe,
            snapshot_asof, ports: PreparationPorts):
    """Share calculations only in this invocation; keep strategy failures separate."""
    shared_inputs = CandidateScanInputs(snapshot_asof)
    candidates, micro_codes = {}, []
    for account in accounts:
        try:
            with PTM.phase("candidate.rows"):
                rows, meta = ports.candidate_rows(
                    account, factor_day, market, sector_rows=sector_rows,
                    live_universe=live_universe, live_asof_date=day, shared_inputs=shared_inputs,
                )
        except Exception as exc:
            rows, meta = [], {"blocked": True, "reason": f"候选生成失败：{type(exc).__name__}: {exc}"}
        candidates[account["id"]] = (rows, meta)
        preliminary = ports.prioritize(rows, account["id"], limit=6)
        micro_codes.extend(str(row.get("code") or "") for row in preliminary if row.get("code"))
    try:
        with PTM.phase("candidate.microstructure_source"):
            microstructures = ports.microstructures(list(dict.fromkeys(micro_codes)), asof_day=day)
    except Exception:
        microstructures = {}
    with PTM.phase("candidate.evidence"):
        evidence = ports.collect_evidence(accounts, candidates, day)
    return PreparedCandidates(candidates, microstructures, evidence)
