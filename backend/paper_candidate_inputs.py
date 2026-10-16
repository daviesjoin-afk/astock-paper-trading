"""Bounded reuse of identical fundamental inputs in one candidate scan."""
import hashlib
import json

import factors as F


class CandidateScanInputs:
    """One scan's explicit snapshot cutoff; never shared across decisions.

    Providers still run through their existing freshness/expiry checks on each
    read. Full input content and financial cutoff identify reusable calculations;
    strict financial history and the report strategy's live lane cannot collide.
    """

    def __init__(self, snapshot_asof):
        self.snapshot_asof = snapshot_asof
        self._fundamentals = {}

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
