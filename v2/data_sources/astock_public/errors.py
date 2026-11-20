"""Typed errors for the public A-share data layer."""
from __future__ import annotations


class AStockPublicError(Exception):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class RegistryError(AStockPublicError):
    """Catalog, registry or query-shape error."""


class CapabilityError(AStockPublicError):
    """A capability cannot honestly be used in the requested configuration."""


class SourceLoadError(AStockPublicError):
    """The pinned upstream static source is missing, modified or unsafe to execute."""


class TransportInjectionError(AStockPublicError):
    """No usable transport was supplied for a network capability."""
