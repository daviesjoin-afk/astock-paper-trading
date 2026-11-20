"""Pinned, offline-safe loading of the upstream ``a-stock-data`` static source.

The upstream project is a Markdown Skill with embedded, runnable Python.  We
retain a documented publication derivative and load only imports, constants and definitions
under an AST allow-list.  Top-level examples, control flow, network calls and
filesystem writes are never executed.  Network-capable modules are replaced only
while the static code is being compiled, through explicit dependency injection.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import SourceLoadError
from .transport import (build_bytes_transport, build_denied_transport,
                        build_source_http_transport, import_overrides)

UPSTREAM_NAME = "a-stock-data"
UPSTREAM_VERSION = "3.10.4"
UPSTREAM_COMMIT = "f21534946c42be3ad048ac938eecb1bad84cb8ab"
UPSTREAM_LICENSE = "Apache-2.0"
UPSTREAM_URL = "https://github.com/simonlin1212/a-stock-data"
UPSTREAM_SKILL_SHA256 = "304e7cc270b595c3031b3c9bd5431d6617ae684b66396092bebbc9f8622f60a6"
SKILL_SHA256 = "072ca0857c48212d7220a97744ab2f7dcc86b7d622e1139de44df954ba49c799"
SKILL_LINES = 7711
EXPECTED_PYTHON_BLOCKS = 91
EXPECTED_FUNCTIONS = 182

_BLOCK = re.compile(r"```python\r?\n(.*?)\r?\n```", re.S)
# Pure constructors used by upstream module-level constants.  Anything else in
# an assignment (notably network, Excel and endpoint calls) is a demo statement.
_SAFE_ASSIGN_CALLS = frozenset({
    "compile", "timezone", "timedelta", "get", "list", "len", "sum", "set",
    "Counter", "Session", "create_default_context", "dropna", "sort_values",
    "match", "fullmatch", "search", "sub", "now", "date", "datetime",
})
_SAFE_STATEMENTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                    ast.Import, ast.ImportFrom)


def snapshot_dir() -> Path:
    return Path(__file__).resolve().parent / "_upstream"


def skill_path() -> Path:
    return snapshot_dir() / "SKILL.md"


def source_manifest() -> dict[str, Any]:
    path = skill_path()
    if not path.is_file():
        raise SourceLoadError("upstream_snapshot_missing", str(path))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SKILL_SHA256:
        raise SourceLoadError("upstream_checksum_mismatch", digest)
    text = raw.decode("utf-8")
    blocks = _BLOCK.findall(text)
    line_count = len(text.splitlines())
    if line_count != SKILL_LINES or len(blocks) != EXPECTED_PYTHON_BLOCKS:
        raise SourceLoadError("upstream_shape_mismatch",
                              f"lines={line_count}, blocks={len(blocks)}")
    return dict(name=UPSTREAM_NAME, version=UPSTREAM_VERSION, commit=UPSTREAM_COMMIT,
                license=UPSTREAM_LICENSE, url=UPSTREAM_URL, skill_sha256=digest,
                upstream_skill_sha256=UPSTREAM_SKILL_SHA256,
                mirror_kind="publication_derivative",
                skill_lines=SKILL_LINES, python_blocks=len(blocks),
                tests=5, fixtures=12, docs=2,
                notice=str(snapshot_dir() / "LICENSE"),
                modifications="Definitions/constants are AST-filtered; demos never run; HTTP is injected; publication contacts removed; TCP peers use mootdx or ASTOCK_TDX_SERVERS; old stock-flow '-' buckets are corrected to None, not zero.",
                upstream_snapshot_path=str(path))


def _static_module(src: str, known: set[str], filename: str) -> ast.Module:
    """Strip every top-level statement with observable side effects."""
    tree = ast.parse(src, filename=filename)
    keep: list[ast.stmt] = []
    local = set(known)
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            keep.append(node)
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
            continue
        if isinstance(node, ast.ImportFrom):
            keep.append(node)
            for alias in node.names:
                imported.add(alias.asname or alias.name)
            continue
        if isinstance(node, _SAFE_STATEMENTS):
            # Preserve upstream definitions, correcting the old flow parser's
            # missing-bucket sentinel: '-' is unknown, never a measured zero.
            if isinstance(node, ast.FunctionDef) and node.name == "stock_fund_flow_120d":
                for value in ast.walk(node):
                    if (isinstance(value, ast.IfExp) and isinstance(value.orelse, ast.Constant)
                            and value.orelse.value == 0 and isinstance(value.test, ast.Compare)
                            and any(isinstance(part, ast.Constant) and part.value == "-"
                                    for part in value.test.comparators)):
                        value.orelse = ast.copy_location(ast.Constant(value=None), value.orelse)
            keep.append(node)
            local.add(getattr(node, "name", ""))
            continue
        # Control flow, expressions, try/except and demos are never needed to
        # recreate upstream callable semantics.
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        calls = []
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                func = call.func
                calls.append(func.id if isinstance(func, ast.Name)
                             else (func.attr if isinstance(func, ast.Attribute) else ""))
        if any(call not in _SAFE_ASSIGN_CALLS for call in calls):
            continue
        loads = [name.id for name in ast.walk(node)
                 if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)]
        if any(name not in local and name not in imported for name in loads):
            continue
        keep.append(node)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    local.add(name.id)
    tree.body = keep
    return tree


def _optional_stubs() -> dict[str, types.ModuleType]:
    """Only stub modules that are truly absent, preserving real optional deps."""
    stubs: dict[str, types.ModuleType] = {}
    if importlib.util.find_spec("mootdx") is None:
        mootdx = types.ModuleType("mootdx")
        quotes = types.ModuleType("mootdx.quotes")

        class _MissingQuotes:
            def factory(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("mootdx is not installed")

        quotes.Quotes = _MissingQuotes()
        mootdx.quotes = quotes
        stubs["mootdx"] = mootdx
        stubs["mootdx.quotes"] = quotes
    for package in ("pandas", "numpy", "baostock"):
        if importlib.util.find_spec(package) is None:
            module = types.ModuleType(package)
            def missing_attribute(name, dependency=package):
                if name.startswith("__"):
                    raise AttributeError(name)
                raise SourceLoadError("optional_dependency_missing", dependency)
            module.__getattr__ = missing_attribute
            stubs[package] = module
    return stubs


class StaticSource:
    """One immutable namespace containing all shipped upstream definitions."""

    def __init__(self, *, http: Any | None = None, fetch: Any | None = None,
                 as_of: str | None = None, max_age: float | None = None,
                 module_overrides: Mapping[str, types.ModuleType] | None = None,
                 allow_status: tuple[int, ...] = (404,)) -> None:
        manifest = source_manifest()
        text = skill_path().read_text(encoding="utf-8")
        blocks = _BLOCK.findall(text)
        if http is not None and fetch is not None:
            raise SourceLoadError("ambiguous_transport", "supply either http or fetch")
        overrides = dict(module_overrides or {})
        transport_overrides: dict[str, types.ModuleType] = {}
        if http is not None:
            transport_overrides.update(build_source_http_transport(
                http, as_of=as_of, max_age=max_age, allow_status=allow_status) or {})
        elif fetch is not None:
            transport_overrides.update(build_bytes_transport(fetch, allow_status=allow_status))
        else:
            transport_overrides.update(build_denied_transport(allow_status=allow_status))
        for name, module in transport_overrides.items():
            overrides.setdefault(name, module)
        stubs = _optional_stubs()
        # The static loader itself must never import real network modules, even
        # when callers supply custom module overrides for a subset.
        for name, module in build_denied_transport(allow_status=allow_status).items():
            overrides.setdefault(name, module)
        # Dynamic imports inside definitions are also resolved from these stubs.
        for name, module in stubs.items():
            overrides.setdefault(name, module)
        namespace: dict[str, Any] = {"__name__": "astock_public._upstream.static",
                                    "__file__": str(skill_path())}
        with import_overrides(overrides):
            known: set[str] = set()
            for index, block in enumerate(blocks, 1):
                filename = f"SKILL.md:block{index}"
                tree = _static_module(block, known, filename)
                exec(compile(tree, filename, "exec"), namespace)
                known.update(name for name, value in namespace.items()
                             if not name.startswith("__"))
        # Upstream functions resolve globals dynamically.  Keep transport shims
        # bound after import_overrides restores the process-level module table,
        # and never leave real requests/urllib modules as an accidental egress.
        for name, module in transport_overrides.items():
            namespace.setdefault(name, module)
        self.namespace = namespace
        self.functions = {name: value for name, value in namespace.items()
                          if callable(value) and hasattr(value, "__code__")}
        required = static_definition_names(blocks)
        missing = required - self.functions.keys()
        if missing:
            raise SourceLoadError("incomplete_definitions", ", ".join(sorted(missing)))
        if len(self.functions) < EXPECTED_FUNCTIONS:
            raise SourceLoadError("incomplete_definitions", str(len(self.functions)))
        self.manifest = dict(manifest, loaded_functions=len(self.functions))

    def callable(self, name: str) -> Any:
        try:
            function = self.functions[name]
        except KeyError as exc:
            raise SourceLoadError("function_not_found", name) from exc
        return function

    def __contains__(self, name: str) -> bool:
        return name in self.functions


def load_static_source(*, http: Any | None = None, fetch: Any | None = None,
                       as_of: str | None = None, max_age: float | None = None,
                       module_overrides: Mapping[str, types.ModuleType] | None = None) -> StaticSource:
    return StaticSource(http=http, fetch=fetch, as_of=as_of, max_age=max_age,
                        module_overrides=module_overrides)


def python_blocks() -> list[str]:
    return list(_BLOCK.findall(skill_path().read_text(encoding="utf-8")))


def static_definition_names(blocks: Iterable[str] | None = None) -> set[str]:
    """Names of all definitions preserved by the safe loader."""
    names: set[str] = set()
    for block in blocks if blocks is not None else python_blocks():
        for node in ast.parse(block).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names
