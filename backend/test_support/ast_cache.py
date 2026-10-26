"""Process-local, mutation-safe AST/source cache for architecture guards.

Cached trees contain real ``ast.AST`` nodes, so ``ast.walk``, ``ast.unparse``,
``ast.dump``, and ``isinstance`` keep their normal semantics.  Every node and
child list is read-only: a mutating ``NodeTransformer`` fails before changing
the shared tree.  ``copy.deepcopy(tree)`` deliberately returns a mutable tree,
allowing transformations that build and return a new tree without polluting
later guards.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Final

_READONLY_MESSAGE: Final = "cached AST is read-only; copy.deepcopy(tree) before transforming it"
_CACHE: dict[Path, tuple[tuple[int, int], str, ast.Module]] = {}


class ReadOnlyASTError(TypeError):
    """Raised when a test tries to mutate a shared cached tree or child list."""


def _read_only(self, *args, **kwargs):
    raise ReadOnlyASTError(_READONLY_MESSAGE)


class _ReadOnlyASTList(list):
    __setitem__ = _read_only
    __delitem__ = _read_only
    __iadd__ = _read_only
    __imul__ = _read_only
    append = _read_only
    clear = _read_only
    extend = _read_only
    insert = _read_only
    pop = _read_only
    remove = _read_only
    reverse = _read_only
    sort = _read_only

    def __deepcopy__(self, memo):
        return [copy.deepcopy(item, memo) for item in self]


class _ReadOnlyNode(ast.AST):
    __setattr__ = _read_only
    __delattr__ = _read_only

    def __deepcopy__(self, memo):
        clone = self.__class__.__bases__[0].__new__(self.__class__.__bases__[0])
        memo[id(self)] = clone
        for name, value in vars(self).items():
            object.__setattr__(clone, name, copy.deepcopy(value, memo))
        return clone


def _readonly_copy(value):
    if isinstance(value, ast.AST):
        # AST's C getset descriptors bypass a subclass __setattr__.  Shadow
        # each descriptor with a property so writes are rejected as well.
        namespace = {
            "__module__": value.__class__.__module__,
            "__qualname__": value.__class__.__qualname__,
        }
        for field in (*value._fields, *value._attributes):
            descriptor = getattr(value.__class__, field, None)
            if descriptor is None:
                continue
            namespace[field] = property(
                lambda self, get=descriptor.__get__: get(self, type(self)),
                _read_only,
                _read_only,
            )
        cls = type(value.__class__.__name__, (value.__class__, _ReadOnlyNode), namespace)
        clone = cls.__new__(cls)
        for name, item in vars(value).items():
            object.__setattr__(clone, name, _readonly_copy(item))
        return clone
    if isinstance(value, list):
        return _ReadOnlyASTList(_readonly_copy(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_readonly_copy(item) for item in value)
    return value


def _load(path: str | Path):
    resolved = Path(path).resolve()
    stat = resolved.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(resolved)
    if cached is None or cached[0] != signature:
        text = resolved.read_text(encoding="utf-8")
        parsed = _readonly_copy(ast.parse(text, filename=str(resolved)))
        cached = (signature, text, parsed)
        _CACHE[resolved] = cached
    return cached[1:]


def source(path: str | Path) -> str:
    """Return current UTF-8 source, sharing reads with :func:`tree`.

    Canonical path, ``st_mtime_ns``, and ``st_size`` identify a snapshot.
    Only the newest snapshot per path is retained.
    """
    return _load(path)[0]


def tree(path: str | Path) -> ast.Module:
    """Return a read-only AST for ``path``, reparsing after file changes."""
    return _load(path)[1]


def clear() -> None:
    """Drop cached snapshots (primarily for isolated cache tests)."""
    _CACHE.clear()
