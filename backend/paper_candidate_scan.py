"""Prepare one trading candidate scan before opening the ledger writer.

This module owns collection order and per-round reuse. It cannot create signals,
approve orders, select a cycle or commit trades. The facade retains those checks.
"""
from copy import deepcopy
from dataclasses import dataclass
import datetime as dt
from typing import Callable, ContextManager

from paper_candidate_inputs import CandidateScanInputs
import paper_timing as PTM

#: 候选阶段的新闻/公告事实可以在同一轮里被后续执行阶段复用多久。
#: 3 分钟与最小调度周期同量级：超过它就必须重新抓取，不能拿上一轮的新闻
#: 去为这一轮的成交做门禁。
NEWS_SNAPSHOT_MAX_AGE_SECONDS = 180.0


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
class NewsSnapshot:
    """一轮扫描的新闻/公告事实：**代码并集只抓一次**。

    owner / key / 生命周期 / TTL / 失效语义（架构规范要求每个缓存写清）：

    * owner：``paper_candidate_scan``（本模块），不是各策略。
    * key：本轮全部账户冷却后候选代码的并集（外加各自的名字映射）。
    * 生命周期：一次 ``prepare`` 到同轮 ``execute_open`` 结束；不跨 slot 复用。
    * source timestamp：``observed_at`` 是**抓取时刻**，不是新闻发布时间——
      每条记录自己的 ``time`` 仍然逐行保留，时效判断继续按记录时间做。
    * TTL：``NEWS_SNAPSHOT_MAX_AGE_SECONDS``；超时、未覆盖请求的代码、或抓取失败
      （``ok=False``）都必须重新抓取。
    * 失败语义：``ok=False`` 表示"没拿到"，绝不等于"没有负面新闻"。
    """

    rows: tuple = ()
    codes: frozenset = frozenset()
    observed_at: dt.datetime | None = None
    ok: bool = False
    source: str = "unknown"

    def age_seconds(self, *, now=None):
        if self.observed_at is None:
            return float("inf")
        current = now or dt.datetime.now()
        return max(0.0, (current - self.observed_at).total_seconds())

    def covers(self, codes):
        requested = {str(code) for code in codes if code}
        return bool(requested) and requested <= set(self.codes)

    def reusable_for(self, codes, *, now=None):
        """同一轮内可安全复用的三个条件：抓取成功、覆盖所需代码、未过期。"""
        if not self.ok:
            return False
        if not self.covers(codes):
            return False
        return self.age_seconds(now=now) <= NEWS_SNAPSHOT_MAX_AGE_SECONDS

    def rows_for(self, codes):
        """按代码切片给某一个消费者；每条记录仍是独立副本。

        数据源契约（``factors.news_keyword_scan``）：快讯命中与公告命中都**必然**
        写入 ``code``，公告还会先按本次请求的 ``universe_names`` 过滤，因此生产源
        不会产出无代码记录 —— 无代码分支在这里只是**防御**：万一将来接入一个会
        返回全市场条目的源，宁可把该行分发给所有消费者（多给的证据由按代码取值的
        门禁自行过滤），也不要让它悄悄消失。
        """
        wanted = {str(code) for code in codes if code}
        return [deepcopy(row) for row in self.rows
                if not row.get("code") or str(row.get("code")) in wanted]


@dataclass(frozen=True)
class EvidenceCollection:
    evidence_by_account: dict
    news_snapshot: NewsSnapshot


@dataclass(frozen=True)
class PreparedCandidates:
    rows_by_account: dict
    microstructures: dict
    evidence_by_account: dict
    news_snapshot: NewsSnapshot | None = None


def _collect_news_snapshot(ports, names, *, now=None):
    """代码并集只请求一次；失败如实标记 ``ok=False``，不伪造空结果。"""
    observed_at = now or dt.datetime.now()
    codes = frozenset(str(code) for code in (names or {}))
    if not codes:
        return NewsSnapshot(observed_at=observed_at, ok=True, source="empty")
    try:
        rows = ports.news(names)
    except Exception:
        return NewsSnapshot(codes=codes, observed_at=observed_at, ok=False, source="failed")
    rows = rows if isinstance(rows, list) else []
    return NewsSnapshot(rows=tuple(rows), codes=codes, observed_at=observed_at,
                        ok=True, source="provider")


def collect_evidence(accounts, precomputed_candidates, day, *, ports: EvidencePorts):
    """Read cooldowns, then one union news batch, then one quote batch.

    News used to be fetched once **per account**: every strategy paid the same
    provider latency for a largely identical code set. The union is fetched
    once per round and sliced per account, so one strategy's rows can never
    leak into another's (each slice is a deep copy).
    """
    evidence_by_account = {}
    for account in accounts:
        rows, meta = precomputed_candidates.get(account["id"], ([], {}))
        if meta.get("blocked"):
            continue
        with ports.open_readonly() as conn, PTM.phase("candidate.cooldown_read"):
            cooled = ports.cooled_candidates(conn, account["id"], rows, day)
        names = {pick["code"]: pick.get("name") or pick["code"] for pick in cooled["candidates"]}
        evidence_by_account[account["id"]] = {"cooled": cooled, "names": names}
    union_names = {}
    for evidence in evidence_by_account.values():
        for code, name in evidence["names"].items():
            union_names.setdefault(code, name)
    with PTM.phase("candidate.news_source"):
        snapshot = _collect_news_snapshot(ports, union_names)
    for evidence in evidence_by_account.values():
        evidence["news"] = snapshot.rows_for(evidence["names"])
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
    return EvidenceCollection(evidence_by_account, snapshot)


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
        collected = ports.collect_evidence(accounts, candidates, day)
    # Ports written before this round (tests, older callers) may still return the
    # plain evidence mapping; accept both without changing how they are used.
    evidence = getattr(collected, "evidence_by_account", collected)
    snapshot = getattr(collected, "news_snapshot", None)
    return PreparedCandidates(candidates, microstructures, evidence, snapshot)
