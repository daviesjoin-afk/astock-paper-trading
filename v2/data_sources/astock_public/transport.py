"""Dependency-injected transport shims for statically reused upstream code.

The upstream functions call ``requests`` and ``urllib.request.urlopen`` directly.
This module exposes a ``requests``-compatible module object whose egress goes
through an injected callable.  When a project ``SourceHttp`` is supplied, every
HTTP request becomes a validated ``SourceHttp.request`` call with a stable
``RequestIdentity``.  No transport object is constructed and no network call is
made at import time.
"""
from __future__ import annotations

import json as _json
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .errors import TransportInjectionError

BytesFetch = Callable[..., bytes]
RequestFunc = Callable[..., Any]


class ShimResponse:
    """Subset of :class:`requests.Response` used by the upstream code."""

    def __init__(self, url: str, body: bytes, status: int = 200,
                 headers: Mapping[str, str] | None = None, encoding: str = "utf-8") -> None:
        self.url = url
        self.status_code = status
        self.status = status
        self.content = body
        self.headers = dict(headers or {})
        self.encoding = encoding
        self.apparent_encoding = encoding
        self.reason = "OK" if status < 400 else "Upstream error"
        self._body = body
        self._offset = 0

    @property
    def text(self) -> str:
        return self._body.decode(self.encoding or "utf-8", "replace")

    def json(self) -> Any:
        return _json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"HTTP {self.status_code} for url: {self.url}", response=self)

    def iter_content(self, chunk_size: int = 8192) -> Iterable[bytes]:
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self._offset = len(self._body)

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers.items())

    def info(self) -> dict[str, str]:
        return dict(self.headers)

    def getcode(self) -> int:
        return self.status_code

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> "ShimResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class RequestException(Exception):
    pass


class HTTPError(RequestException):
    def __init__(self, message: str, response: ShimResponse | None = None) -> None:
        self.response = response
        super().__init__(message)


class ConnectionError(RequestException):
    pass


class Timeout(RequestException):
    pass


@dataclass
class _RequestSpec:
    method: str
    url: str
    params: Mapping[str, Any] | None = None
    data: Any = None
    json: Any = None
    headers: Mapping[str, str] | None = None
    timeout: Any = None
    allow_status: tuple[int, ...] = ()
    allow_redirects: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def full_url(self) -> str:
        if not self.params:
            return self.url
        separator = "&" if "?" in self.url else "?"
        return self.url + separator + urllib.parse.urlencode(self.params, doseq=True)

    def payload(self) -> bytes:
        if self.data is None and self.json is None:
            return b""
        if self.json is not None:
            return _json.dumps(self.json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if isinstance(self.data, (bytes, bytearray, memoryview)):
            return bytes(self.data)
        if isinstance(self.data, str):
            return self.data.encode("utf-8")
        return urllib.parse.urlencode(self.data, doseq=True).encode("utf-8")


class SourceHttpAdapter:
    """Route a request-shaped call through ``SourceHttp`` or a bytes fetcher.

    ``SourceHttp`` is detected structurally: it must expose ``request(identity,
    *, headers=None, validate=None, as_of=None, max_age=None)`` returning an
    object with ``raw``.  This avoids a hard dependency on the owning project's
    package layout while preserving its cache/identity semantics.
    """

    def __init__(self, transport: Any, *, as_of: str | None = None,
                 max_age: float | None = None, source: str = "astock-public") -> None:
        self.transport = transport
        self.as_of = as_of
        self.max_age = max_age
        self.source = source

    def __call__(self, spec: _RequestSpec) -> bytes:
        url = spec.full_url()
        payload = spec.payload()
        headers = dict(spec.headers or {})
        if spec.json is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        if payload and spec.method.upper() == "POST" and "Content-Type" not in headers:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if callable(self.transport) and not hasattr(self.transport, "request"):
            return bytes(self.transport(url, method=spec.method.upper(), data=payload,
                                        headers=headers, timeout=spec.timeout))
        request = getattr(self.transport, "request", None)
        if not callable(request):
            raise TransportInjectionError("unsupported_transport", type(self.transport).__name__)
        identity = self._identity(url, spec.method.upper(), payload)
        try:
            snapshot = request(identity, headers=headers, as_of=self.as_of,
                               max_age=self.max_age)
        except TypeError:
            # Also accept narrowly-scoped injected requesters that do not expose
            # the full SourceHttp keyword contract.
            snapshot = request(identity, headers=headers)
        raw = getattr(snapshot, "raw", snapshot)
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise TransportInjectionError("transport_result_not_bytes", type(raw).__name__)
        return bytes(raw)

    def _identity(self, url: str, method: str, payload: bytes) -> Any:
        identity_cls = getattr(sys.modules.get(type(self.transport).__module__), "RequestIdentity", None)
        if identity_cls is None:
            try:
                from v2.data_sources.source_http import RequestIdentity as identity_cls  # type: ignore
            except Exception:
                identity_cls = None
        if identity_cls is None:
            @dataclass(frozen=True)
            class LocalIdentity:
                source: str
                url: str
                method: str
                payload: bytes
                adjust: str
                symbol: str
                date: str

                def document(self) -> dict:
                    import base64
                    return dict(source=self.source, url=self.url, method=self.method,
                                payload=base64.b64encode(self.payload).decode("ascii"),
                                adjust=self.adjust, symbol=self.symbol, date=self.date)

            identity_cls = LocalIdentity
        try:
            return identity_cls(self.source, url, method=method, payload=payload,
                                adjust="none", symbol="*", date="")
        except TypeError:
            return identity_cls(self.source, url, method, payload)


def _dispatch_response(spec, dispatch) -> ShimResponse:
    """Preserve HTTP status so upstream allow_status/raise_for_status work."""
    try:
        return ShimResponse(spec.full_url(), dispatch(spec))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except (OSError, ValueError):
            body = b""
        finally:
            exc.close()
        return ShimResponse(spec.full_url(), body, status=exc.code, headers=exc.headers)
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        error = Timeout if isinstance(exc, TimeoutError) else ConnectionError
        raise error(str(exc)) from exc


class _ShimSession:
    """Session used by the upstream official-exchange backup endpoints."""

    def __init__(self, dispatch: Callable[[_RequestSpec], bytes], allow_status: tuple[int, ...] = ()) -> None:
        self.headers: dict[str, str] = {}
        self._dispatch = dispatch
        self.allow_status = allow_status

    def _headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        merged = dict(self.headers)
        merged.update(headers or {})
        return merged

    def request(self, method: str, url: str, **kwargs: Any) -> ShimResponse:
        spec = _RequestSpec(method=method.upper(), url=url,
                            params=kwargs.get("params"), data=kwargs.get("data"),
                            json=kwargs.get("json"), headers=self._headers(kwargs.get("headers")),
                            timeout=kwargs.get("timeout"),
                            allow_status=self.allow_status,
                            allow_redirects=kwargs.get("allow_redirects", True),
                            extra={k: v for k, v in kwargs.items()
                                   if k not in {"params", "data", "json", "headers", "timeout", "allow_redirects"}})
        return _dispatch_response(spec, self._dispatch)

    def get(self, url: str, **kwargs: Any) -> ShimResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> ShimResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> ShimResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> ShimResponse:
        return self.request("DELETE", url, **kwargs)

    def mount(self, prefix: str, adapter: Any) -> None:
        return None

    def __enter__(self) -> "_ShimSession":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class RequestsShim(types.ModuleType):
    """A deliberately narrow ``requests`` replacement bound to one transport."""

    RequestException = RequestException
    HTTPError = HTTPError
    ConnectionError = ConnectionError
    Timeout = Timeout

    def __init__(self, dispatch: Callable[[_RequestSpec], bytes], *,
                 allow_status: tuple[int, ...] = ()) -> None:
        super().__init__("requests")
        self._dispatch = dispatch
        self._allow_status = allow_status
        self.adapters = types.ModuleType("requests.adapters")
        self.adapters.HTTPAdapter = lambda *args, **kwargs: object()
        self.exceptions = types.SimpleNamespace(RequestException=RequestException,
                                                HTTPError=HTTPError,
                                                ConnectionError=ConnectionError,
                                                Timeout=Timeout)

    def _request(self, method: str, url: str, **kwargs: Any) -> ShimResponse:
        spec = _RequestSpec(method=method.upper(), url=url,
                            params=kwargs.get("params"), data=kwargs.get("data"),
                            json=kwargs.get("json"), headers=kwargs.get("headers"),
                            timeout=kwargs.get("timeout"), allow_status=self._allow_status,
                            allow_redirects=kwargs.get("allow_redirects", True),
                            extra={k: v for k, v in kwargs.items()
                                   if k not in {"params", "data", "json", "headers", "timeout", "allow_redirects"}})
        return _dispatch_response(spec, self._dispatch)

    def request(self, method: str, url: str, **kwargs: Any) -> ShimResponse:
        return self._request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> ShimResponse:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> ShimResponse:
        return self._request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> ShimResponse:
        return self._request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> ShimResponse:
        return self._request("DELETE", url, **kwargs)

    def Session(self) -> _ShimSession:
        return _ShimSession(self._dispatch, self._allow_status)

    def Response(self) -> ShimResponse:
        return ShimResponse("", b"")


class UrllibRequestShim(types.ModuleType):
    """A narrow ``urllib.request`` replacement bound to one transport."""

    def __init__(self, dispatch: Callable[[_RequestSpec], bytes]) -> None:
        super().__init__("urllib.request")
        self._dispatch = dispatch
        self.Request = urllib.request.Request
        self.urlopen = self._urlopen
        # Preserve real error types so upstream exception clauses remain valid.
        self.HTTPError = urllib.error.HTTPError
        self.URLError = urllib.error.URLError

    def _urlopen(self, request: Any, timeout: Any = None, **kwargs: Any) -> ShimResponse:
        if isinstance(request, str):
            url, method, data, headers = request, "GET", None, {}
        else:
            url = request.full_url
            method = getattr(request, "get_method", lambda: "GET")()
            data = getattr(request, "data", None)
            try:
                headers = dict(request.header_items())
            except (AttributeError, TypeError, ValueError):
                raw_headers = getattr(request, "headers", {}) or {}
                headers = dict(raw_headers.items()) if hasattr(raw_headers, "items") else dict(raw_headers)
        spec = _RequestSpec(method=method, url=url, data=data, headers=headers,
                            timeout=timeout, extra=dict(kwargs))
        return ShimResponse(url, self._dispatch(spec))


def _urllib_module(dispatch: Callable[[_RequestSpec], bytes]) -> types.ModuleType:
    module = types.ModuleType("urllib")
    module.request = UrllibRequestShim(dispatch)
    module.error = urllib.error
    module.parse = urllib.parse
    return module


def build_source_http_transport(http: Any | None = None, *, as_of: str | None = None,
                                max_age: float | None = None,
                                allow_status: tuple[int, ...] = (404,)) -> dict[str, types.ModuleType] | None:
    """Return module overrides for a supplied ``SourceHttp`` (or ``None``)."""
    if http is None:
        return None
    adapter = SourceHttpAdapter(http, as_of=as_of, max_age=max_age)
    requests_shim = RequestsShim(adapter, allow_status=allow_status)
    urllib_shim = _urllib_module(adapter)
    return {"requests": requests_shim, "urllib": urllib_shim,
            "urllib.request": urllib_shim.request, "urllib.error": urllib.error,
            "urllib.parse": urllib.parse}


def build_bytes_transport(fetch: BytesFetch, *,
                          allow_status: tuple[int, ...] = (404,)) -> dict[str, types.ModuleType]:
    """Return module overrides for a simple ``fetch(url, ...) -> bytes`` callable."""
    def dispatch(spec: _RequestSpec) -> bytes:
        return bytes(fetch(spec.full_url(), method=spec.method.upper(), data=spec.payload(),
                           headers=dict(spec.headers or {}), timeout=spec.timeout))
    requests_shim = RequestsShim(dispatch, allow_status=allow_status)
    urllib_shim = _urllib_module(dispatch)
    return {"requests": requests_shim, "urllib": urllib_shim,
            "urllib.request": urllib_shim.request, "urllib.error": urllib.error,
            "urllib.parse": urllib.parse}


def build_denied_transport(*, allow_status: tuple[int, ...] = (404,)) -> dict[str, types.ModuleType]:
    """Install inert ``requests``/``urllib`` modules that fail closed on egress.

    This is the default for local-only calls.  Session construction and parsing
    remain available; any actual request raises ``TransportInjectionError``.
    """
    def deny(spec: _RequestSpec) -> bytes:
        raise TransportInjectionError("transport_required", f"{spec.method} {spec.full_url()}")

    requests_shim = RequestsShim(deny, allow_status=allow_status)
    urllib_shim = _urllib_module(deny)
    return {"requests": requests_shim, "urllib": urllib_shim,
            "urllib.request": urllib_shim.request, "urllib.error": urllib.error,
            "urllib.parse": urllib.parse}


def import_overrides(overrides: Mapping[str, types.ModuleType] | None):
    """Temporarily install module overrides; restore the exact previous state."""
    if not overrides:
        return nullcontext()
    from contextlib import contextmanager

    @contextmanager
    def _install():
        # Module bindings are process-wide; serialize cold namespace loading
        # so two clients cannot capture each other's injected transport.
        with _IMPORT_LOCK:
            saved = {name: sys.modules.get(name, _ABSENT) for name in overrides}
            sys.modules.update(overrides)
            try:
                yield
            finally:
                for name, value in saved.items():
                    if value is _ABSENT:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = value

    return _install()


_ABSENT = object()
_IMPORT_LOCK = threading.RLock()
