"""Mature broad public A-share data layer built on a pinned upstream snapshot.

Public API:
    PublicClient, CapabilityRegistry, Capability, QueryResult,
    catalog, query, source_manifest, dependency_report,
    AStockPublicError, RegistryError, CapabilityError,
    SourceLoadError, TransportInjectionError.

The package is import-safe: catalog and dependency queries perform no network
I/O and do not execute the upstream source.  Optional packages are reported per
capability; their absence disables only that capability.  A network capability
requires dependency-injected ``http`` (project ``SourceHttp``) or ``fetch``.
"""
from __future__ import annotations

from .client import PublicClient, QueryResult, catalog, query
from .errors import (AStockPublicError, CapabilityError, RegistryError,
                     SourceLoadError, TransportInjectionError)
from .registry import CapabilityRegistry, DEFAULT_REGISTRY
from .registry_data import (CAPABILITIES, Capability, KNOWN_BLOCKERS,
                            SOURCE_CATALOG, capability_count, upstream_metadata)
from .source_snapshot import (EXPECTED_FUNCTIONS, EXPECTED_PYTHON_BLOCKS,
                              SKILL_SHA256, UPSTREAM_COMMIT, UPSTREAM_LICENSE,
                              UPSTREAM_NAME, UPSTREAM_SKILL_SHA256, UPSTREAM_URL,
                              UPSTREAM_VERSION, StaticSource, load_static_source,
                              source_manifest)
from .transport import (RequestsShim, ShimResponse, SourceHttpAdapter,
                        build_bytes_transport, build_denied_transport,
                        build_source_http_transport)


def dependency_report() -> dict:
    """Offline dependency availability report."""
    return DEFAULT_REGISTRY.dependency_report()


def dependency_state(name: str) -> dict:
    for item in dependency_report()["items"]:
        if item["name"] == name:
            return item
    from .registry import _dependency_state
    return _dependency_state(name)


__version__ = "1.0.0"
__all__ = [
    "PublicClient", "QueryResult", "catalog", "query",
    "CapabilityRegistry", "DEFAULT_REGISTRY", "Capability", "CAPABILITIES",
    "AStockPublicError", "RegistryError", "CapabilityError",
    "SourceLoadError", "TransportInjectionError",
    "source_manifest", "dependency_report", "dependency_state",
    "capability_count", "upstream_metadata", "KNOWN_BLOCKERS", "SOURCE_CATALOG",
    "StaticSource", "load_static_source",
    "RequestsShim", "ShimResponse", "SourceHttpAdapter",
    "build_bytes_transport", "build_denied_transport",
    "build_source_http_transport",
    "UPSTREAM_NAME", "UPSTREAM_VERSION", "UPSTREAM_COMMIT", "UPSTREAM_LICENSE",
    "UPSTREAM_URL", "UPSTREAM_SKILL_SHA256", "SKILL_SHA256",
    "EXPECTED_PYTHON_BLOCKS", "EXPECTED_FUNCTIONS", "__version__",
]
