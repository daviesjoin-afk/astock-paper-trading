"""Capability registry and offline catalog for all upstream dataset groups."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .errors import CapabilityError, RegistryError
from .registry_data import (CAPABILITIES, KNOWN_BLOCKERS, LOCAL_HELPERS,
                            SOURCE_CATALOG, Capability, capability_count,
                            upstream_metadata)
from .source_snapshot import StaticSource, load_static_source, source_manifest

_MISSING = object()
TDX_QUERY_PARAMETERS = {
    "bars.mootdx_legacy": ("code", "operation", "frequency", "offset"),
    "fundamentals.tdx_finance": ("code",),
    "fundamentals.tdx_f10": ("code", "category"),
}


def _dependency_state(name: str) -> dict[str, Any]:
    if name.startswith("IWENCAI_"):
        return dict(name=name, kind="credential", available=bool(os.environ.get(name)),
                    detail="environment variable" if os.environ.get(name) else "not configured")
    if name == "anonymous_cookie":
        return dict(name=name, kind="browser_session", available=None,
                    detail="anonymous BSE session is created at call time")
    if name in {"requests", "urllib"}:
        return dict(name=name, kind="runtime", available=True,
                    detail="provided by the injectable transport shim")
    try:
        found = importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        found = False
    return dict(name=name, kind="python_package", available=found,
                detail="installed" if found else "optional dependency absent")


class CapabilityRegistry:
    """Immutable capability declarations plus lazy execution of pinned code."""

    def __init__(self, *, http: Any | None = None, fetch: Any | None = None,
                 as_of: str | None = None, max_age: float | None = None,
                 auto_load: bool = False) -> None:
        self._http = http
        self._fetch = fetch
        self._as_of = as_of
        self._max_age = max_age
        self._lock = threading.RLock()
        self._source: StaticSource | None = None
        if auto_load:
            self.load_source()

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return CAPABILITIES

    def dependency_report(self) -> dict[str, Any]:
        names = sorted({dep for cap in CAPABILITIES for dep in cap.optional_dependencies}
                       | {cred for cap in CAPABILITIES for cred in cap.credentials}
                       | {"requests", "pandas", "numpy", "baostock", "mootdx",
                          "xlrd", "openpyxl"})
        items = [_dependency_state(name) for name in names]
        return dict(items=items,
                    available=sum(1 for item in items if item["available"] is True),
                    absent=sum(1 for item in items if item["available"] is False),
                    unknown=sum(1 for item in items if item["available"] is None),
                    optional_dependencies_do_not_disable_package=True)

    def blockers(self) -> dict[str, dict[str, Any]]:
        return {name: dict(id=name, **value) for name, value in KNOWN_BLOCKERS.items()}

    def sources(self) -> tuple[str, ...]:
        return SOURCE_CATALOG

    def catalog(self, *, layer: str | None = None, source: str | None = None,
                temporal: str | None = None, market: str | None = None,
                available_only: bool = False, search: str | None = None,
                include_uncounted: bool = True) -> dict[str, Any]:
        """Pure, offline catalog; filters never load upstream code or network."""
        rows = list(CAPABILITIES)
        if not include_uncounted:
            rows = [row for row in rows if row.counted]
        if layer is not None:
            rows = [row for row in rows if layer in row.layer or layer in row.id]
        if source is not None:
            needle = source.casefold()
            rows = [row for row in rows if needle in row.source.casefold()]
        if temporal is not None:
            rows = [row for row in rows if row.temporal == temporal]
        if market is not None:
            rows = [row for row in rows if market.upper() in row.markets]
        if search is not None:
            needle = search.casefold()
            rows = [row for row in rows
                    if any(needle in str(value).casefold()
                           for value in (row.id, row.name, row.description,
                                         row.source, " ".join(row.functions)))]
        result = [row.as_dict() for row in rows]
        for row in result:
            if row["id"] in TDX_QUERY_PARAMETERS:
                row["parameters"] = list(TDX_QUERY_PARAMETERS[row["id"]])
        if available_only:
            dependency = {item["name"]: item for item in self.dependency_report()["items"]}

            def available(row: dict[str, Any]) -> bool:
                deps = tuple(row["credentials"]) + tuple(
                    dep for dep in row["optional_dependencies"]
                    if dep not in {"requests", "urllib"})
                states = [dependency.get(dep, {"available": True})["available"] for dep in deps]
                return (all(state is True for state in states)
                        and not any(state["permanent"] is True
                                    and state.get("status") in {"endpoint_removed", "permanently_unavailable"}
                                    for state in row.get("blockers", [])))

            result = [row for row in result if available(row)]
        return dict(status="ok", upstream=upstream_metadata(),
                    snapshot=source_manifest(), counts=capability_count(),
                    dependency_summary=self.dependency_report(),
                    returned=len(result), capabilities=result,
                    blockers=self.blockers(), sources=list(SOURCE_CATALOG),
                    local_helpers=[dict(id=id_, function=function, description=description)
                                   for id_, function, description in LOCAL_HELPERS],
                    network_accessed=False)

    def describe(self, capability_id: str) -> dict[str, Any]:
        capability = self.get(capability_id)
        return dict(status="ok", capability=capability.as_dict(),
                    dependency_state=[_dependency_state(dep)
                                      for dep in capability.credentials + capability.optional_dependencies],
                    functions={name: self.function_signature(name)
                               for name in capability.functions},
                    source_loaded=self._source is not None,
                    network_accessed=False)

    def get(self, capability_id: str) -> Capability:
        for capability in CAPABILITIES:
            if capability.id == capability_id or capability.name == capability_id:
                return capability
        raise RegistryError("capability_not_found", capability_id)

    def function_signature(self, name: str) -> str | None:
        """Return a signature without loading executable source."""
        from .source_snapshot import python_blocks
        import ast
        for block in python_blocks():
            for node in ast.parse(block).body:
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    return name + ast.unparse(node.args)
        for _id, function, _desc in LOCAL_HELPERS:
            if function == name:
                return "signature available after source load"
        return None

    def load_source(self) -> StaticSource:
        with self._lock:
            if self._source is None:
                self._source = load_static_source(
                    http=self._http, fetch=self._fetch,
                    as_of=self._as_of, max_age=self._max_age)
            return self._source

    def function(self, name: str) -> Any:
        source = self.load_source()
        if name not in source:
            raise RegistryError("function_not_found", name)
        return source.callable(name)

    def _validate(self, capability: Capability, args: Sequence[Any],
                  kwargs: Mapping[str, Any]) -> None:
        if capability.credentials:
            missing = [name for name in capability.credentials
                       if name.startswith("IWENCAI_") and not os.environ.get(name)]
            if missing:
                raise CapabilityError("credential_required",
                                      f"{capability.id}: {', '.join(missing)}")
        for name in capability.optional_dependencies:
            state = _dependency_state(name)
            if state["available"] is False:
                raise CapabilityError("optional_dependency_missing",
                                      f"{capability.id}: {name}")
        # Transport is required only for capabilities that are not pure local
        # computations.  Local functions are allowed with no injector.
        if capability.temporal != "local_computation" and self._http is None and self._fetch is None:
            raise CapabilityError("transport_required", capability.id)
        if self._as_of is not None and capability.temporal in {
                "current_snapshot", "same_trading_day", "unknown"}:
            raise CapabilityError("historical_snapshot_unavailable",
                                  f"{capability.id}: {capability.temporal} cannot honestly answer --as-of")
        # Upstream qfq/hfq arguments are passed through unchanged.  This layer
        # does not promote them into the V2 reviewed-close path; consumers must
        # keep adjustment in provenance and reject qfq/hfq there.

    def _tdx_query(self, capability_id: str, args: Sequence[Any], options: Mapping[str, Any]) -> Any:
        """Materialize the dataset shown in upstream client-method examples."""
        options = dict(options)
        if len(args) > 1:
            raise CapabilityError("invalid_parameters", "one positional symbol at most")
        if sum(("code" in options, "symbol" in options, bool(args))) != 1:
            raise CapabilityError("symbol_required", "provide exactly one code or symbol")
        symbol = options.pop("code") if "code" in options else options.pop("symbol") if "symbol" in options else args[0]
        normalize = self.function("norm_ticker")
        symbol = [normalize(item) for item in symbol] if isinstance(symbol, (list, tuple)) else normalize(symbol)
        reference = capability_id != "bars.mootdx_legacy"
        operation = options.pop("operation", "bars")
        category = options.pop("category", None)
        if not reference and operation not in ("bars", "quotes", "transaction"):
            raise CapabilityError("unsupported_operation", str(operation))
        if reference and options:
            raise CapabilityError("invalid_parameters", ", ".join(sorted(options)))
        client = self.function("tdx_client")(check="finance" if reference else "bars")
        try:
            if capability_id == "fundamentals.tdx_finance":
                result = client.finance(symbol=symbol)
            elif capability_id == "fundamentals.tdx_f10":
                categories = client.F10C(symbol=symbol)
                if not isinstance(categories, list) or not all(isinstance(row, dict) and row.get("name") for row in categories):
                    raise CapabilityError("source_schema_changed", "F10C categories")
                if category is not None:
                    categories = [row for row in categories if row["name"] == category]
                    if not categories:
                        raise CapabilityError("category_not_found", str(category))
                result = [dict(symbol=symbol, category=row["name"],
                               text=client.F10(symbol=symbol, name=row["name"])) for row in categories]
                if any(not isinstance(row["text"], str) or not row["text"].strip() for row in result):
                    raise CapabilityError("source_empty_unverified", "F10 text missing or invalid")
            else:
                result = getattr(client, operation)(symbol=symbol, **options)
            if result is None or (hasattr(result, "__len__") and len(result) == 0):
                raise CapabilityError("source_empty_unverified", capability_id)
            return result
        finally:
            # mootdx wraps a pytdx client; injected test clients may close directly.
            connection = getattr(client, "client", client)
            close = getattr(connection, "disconnect", None) or getattr(connection, "close", None)
            if callable(close):
                close()

    def call(self, capability_id: str, /, *args: Any, **kwargs: Any) -> Any:
        """Call one capability by id/name through the pinned upstream function."""
        capability = self.get(capability_id)
        function_name = kwargs.pop("function", capability.functions[0])
        if function_name not in capability.functions:
            raise RegistryError("function_not_in_capability",
                                f"{capability.id}: {function_name}")
        self._validate(capability, args, kwargs)
        if capability.id in ("bars.mootdx_legacy", "fundamentals.tdx_finance", "fundamentals.tdx_f10"):
            return self._tdx_query(capability.id, args, kwargs)
        function = self.function(function_name)
        return function(*args, **kwargs)

    # Explicit lower-level escape hatch; keeps all shipped helpers reachable.
    def call_function(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        if self._http is None and self._fetch is None:
            # Local-only helpers may still be called; network functions fail
            # closed from the inert transport instead of reaching the network.
            # All definitions are safe to load with the inert transport.  A
            # network function then fails closed at its first egress attempt.
            pass
        return self.function(name)(*args, **kwargs)


DEFAULT_REGISTRY = CapabilityRegistry()
