"""Round-local source snapshots and identical fundamental calculations."""
from copy import deepcopy
import hashlib
import json

import factors as F
import paper_timing as PTM


class CandidateScanInputs:
    """One scan's explicit snapshot cutoff; never shared across decisions.

    The first usable universe/finance read passes the provider's normal checks.
    Later strategies see independent copies of that round's facts. Empty reads
    and failures are not retained, so another strategy can recover the source.
    Full input content and financial cutoff identify reusable calculations;
    strict financial history and the report strategy's live lane cannot collide.
    """

    def __init__(self, snapshot_asof):
        self.snapshot_asof = snapshot_asof
        self._fundamentals = {}
        self._universe = None
        self._finance = None
        self._disclosures = {}

    def universe(self, loader):
        if self._universe is None:
            with PTM.phase("candidate.universe_snapshot_load"):
                value = loader()
            if not value:
                return value
            self._universe = deepcopy(value)
        return deepcopy(self._universe)

    def finance(self, loader):
        if self._finance is None:
            with PTM.phase("candidate.finance_snapshot_load"):
                value = loader()
            if not isinstance(value, dict) or not value.get("data"):
                return value
            self._finance = deepcopy(value)
        return deepcopy(self._finance)

    def disclosures(self, codes, *, asof, loader):
        """Memoize disclosure rows by code and as-of inside one scan round.

        Candidate sets differ per strategy, so only the missing codes are
        fetched. Only rows actually returned are cached; a missing row or a
        failed source is retried by another account instead of becoming a
        round-wide negative result.
        """
        cutoff = asof.isoformat() if hasattr(asof, "isoformat") else str(asof)
        requested = list(dict.fromkeys(str(code) for code in codes if code))
        missing = [code for code in requested
                   if (code, cutoff) not in self._disclosures]
        if missing:
            with PTM.phase("candidate.disclosure_round_source"):
                rows = loader(missing, asof=asof) or []
            found = {
                str(row.get("code")): row
                for row in rows
                if isinstance(row, dict) and row.get("code")
            }
            for code in missing:
                if code in found:
                    self._disclosures[(code, cutoff)] = deepcopy(found[code])
        return [deepcopy(self._disclosures[(code, cutoff)])
                for code in requested
                if self._disclosures.get((code, cutoff)) is not None]

    def fundamentals(self, universe, finance, *, asof):
        def cutoff(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        try:
            raw = json.dumps((universe, finance, cutoff(asof), cutoff(self.snapshot_asof)),
                             sort_keys=True, ensure_ascii=False).encode("utf-8")
            key = hashlib.sha256(raw).digest()
        except (TypeError, ValueError, OverflowError):
            # Non-JSON source values keep the original computation path.
            return F.compute_fundamental_factors(
                universe, finance, asof=asof, snapshot_asof=self.snapshot_asof)
        if key not in self._fundamentals:
            value = F.compute_fundamental_factors(
                universe, finance, asof=asof, snapshot_asof=self.snapshot_asof)
            if len(self._fundamentals) >= 4:
                del self._fundamentals[next(iter(self._fundamentals))]
            self._fundamentals[key] = value
        # Each strategy may alter its own table. Never hand out the cached frame.
        return self._fundamentals[key].copy(deep=True)
