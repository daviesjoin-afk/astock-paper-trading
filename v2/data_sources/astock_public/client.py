"""Main-thread friendly catalog/query façade.

Importing this module, constructing a client, calling ``catalog`` or running the
CLI's catalog mode never loads executable upstream code, imports optional data
packages, or accesses the network.  Network calls happen only in ``query`` and
``call`` with an explicit ``http``/``fetch`` transport.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):  # direct `python astock_public/client.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from astock_public.errors import AStockPublicError
    from astock_public.registry import CapabilityRegistry
    from astock_public.registry_data import capability_count, upstream_metadata
    from astock_public.source_snapshot import source_manifest
else:
    from .errors import AStockPublicError
    from .registry import CapabilityRegistry
    from .registry_data import capability_count, upstream_metadata
    from .source_snapshot import source_manifest


@dataclass(frozen=True)
class QueryResult:
    status: str
    capability: str
    function: str
    result: Any
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(status=self.status, capability=self.capability,
                    function=self.function, result=self.result,
                    provenance=self.provenance)


def _public_value(value: Any) -> Any:
    """JSON-safe projection that does not mutate pandas frames or other objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return dict(__bytes__=True, length=len(value))
    if isinstance(value, Mapping):
        return {str(key): _public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_public_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(frame=True, rows=_public_value(to_dict(orient="records")))
        except TypeError:
            return dict(frame=True, rows=_public_value(to_dict()))
    attrs = getattr(value, "attrs", None)
    columns = getattr(value, "columns", None)
    if columns is not None:
        return dict(frame=True, columns=[str(c) for c in columns],
                    rows=len(value), attrs=_public_value(dict(attrs or {})))
    return dict(type=type(value).__name__, repr=repr(value)[:500])


class PublicClient:
    """Catalog and query entry point for callers including the V2 main thread."""

    def __init__(self, *, http: Any | None = None, fetch: Any | None = None,
                 as_of: str | None = None, max_age: float | None = None,
                 registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry or CapabilityRegistry(
            http=http, fetch=fetch, as_of=as_of, max_age=max_age)

    def catalog(self, **filters: Any) -> dict[str, Any]:
        return self.registry.catalog(**filters)

    def capabilities(self) -> list[dict[str, Any]]:
        return self.catalog()["capabilities"]

    def describe(self, capability_id: str) -> dict[str, Any]:
        return self.registry.describe(capability_id)

    def dependencies(self) -> dict[str, Any]:
        return self.registry.dependency_report()

    def blockers(self) -> dict[str, Any]:
        return dict(status="ok", blockers=self.registry.blockers(),
                    note="Transient 501/timeouts and unexplained runtime anomalies are not permanent; candidate-endpoint failures are scoped separately; TDX TCP failure does not affect the TDX HTTP package.")

    def query(self, capability_id: str, /, *args: Any,
              as_json: bool = False, **kwargs: Any) -> QueryResult | dict[str, Any]:
        """Execute one declared capability with an injected transport."""
        capability = self.registry.get(capability_id)
        function_name = kwargs.get("function", capability.functions[0])
        raw = self.registry.call(capability_id, *args, **kwargs)
        result = _public_value(raw) if as_json else raw
        payload = QueryResult(
            status="ok", capability=capability.id, function=function_name,
            result=result,
            provenance=dict(upstream=upstream_metadata(), source=capability.source,
                            temporal=capability.temporal,
                            temporal_note=capability.temporal_note,
                            markets=list(capability.markets),
                            credentials=list(capability.credentials),
                            optional_dependencies=list(capability.optional_dependencies),
                            blockers=[dict(self.registry.blockers()[name])
                                      for name in capability.blockers],
                            transport_injected=True,
                            adjustment="caller_supplied_upstream_semantics"))
        return payload.as_dict() if as_json else payload

    def call(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        return self.registry.call_function(name, *args, **kwargs)

    def function(self, name: str) -> Any:
        return self.registry.function(name)


def catalog(**filters: Any) -> dict[str, Any]:
    """Module-level convenience wrapper; pure and offline-safe."""
    return PublicClient().catalog(**filters)


def query(capability_id: str, /, *args: Any, http: Any | None = None,
          fetch: Any | None = None, as_of: str | None = None,
          max_age: float | None = None, as_json: bool = False,
          **kwargs: Any) -> QueryResult | dict[str, Any]:
    """Module-level convenience wrapper; requires explicit transport."""
    return PublicClient(http=http, fetch=fetch, as_of=as_of,
                        max_age=max_age).query(capability_id, *args,
                                               as_json=as_json, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline-safe A-stock public data catalog/query")
    parser.add_argument("action", choices=("catalog", "dependencies", "blockers",
                                           "describe", "manifest"))
    parser.add_argument("--capability")
    parser.add_argument("--layer")
    parser.add_argument("--source")
    parser.add_argument("--temporal")
    parser.add_argument("--market")
    parser.add_argument("--search")
    parser.add_argument("--available-only", action="store_true")
    parser.add_argument("--include-uncounted", action="store_true", default=True)
    parser.add_argument("--counted-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "manifest":
            out = dict(status="ok", manifest=source_manifest(),
                       counts=capability_count(), upstream=upstream_metadata())
        else:
            client = PublicClient()
            if args.action == "catalog":
                out = client.catalog(layer=args.layer, source=args.source,
                                     temporal=args.temporal, market=args.market,
                                     search=args.search,
                                     available_only=args.available_only,
                                     include_uncounted=not args.counted_only)
            elif args.action == "dependencies":
                out = dict(status="ok", dependencies=client.dependencies())
            elif args.action == "blockers":
                out = client.blockers()
            else:
                if not args.capability:
                    raise AStockPublicError("capability_required", "describe")
                out = client.describe(args.capability)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    except AStockPublicError as exc:
        print(json.dumps(dict(status="error", code=exc.code, detail=exc.detail),
                         ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
