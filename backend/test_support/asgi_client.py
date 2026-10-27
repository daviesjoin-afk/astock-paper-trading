# -*- coding: utf-8 -*-
"""Minimal ASGI driver for tests that need the real app but no HTTP client.

`fastapi.testclient` requires ``httpx``/``httpx2``, which is deliberately **not**
in the locked runtime dependencies (the image has no HTTP client for tests). CI
and the docker smoke job therefore cannot import a module that uses it: the
whole module fails to *load*, which reads like a real regression.

This driver calls the ASGI app directly — including whatever middleware the app
installs — and returns ``(status, body)``. It is the same approach
``test_operator_boundary`` already used inline; keeping it here stops each new
test from re-inventing it or accidentally re-introducing the dependency.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


def scope(method: str, path: str, *, query: str = "", headers: dict[str, str] | None = None,
          client: tuple[str, int] = ("127.0.0.1", 45678),
          scheme: str = "http", server: tuple[str, int] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "headers": [
            (str(key).lower().encode("ascii"), str(value).encode("utf-8"))
            for key, value in (headers or {}).items()
        ],
        "scheme": scheme,
        "server": server or ("localhost", 80 if scheme == "http" else 443),
        "client": client,
        "root_path": "",
        "http_version": "1.1",
    }


async def drive(app, asgi_scope: dict[str, Any], *, body: bytes = b"") -> tuple[int | None, bytes]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(asgi_scope, receive, send)
    start = next((item for item in messages if item["type"] == "http.response.start"), None)
    payload = b"".join(item.get("body", b"") for item in messages
                       if item["type"] == "http.response.body")
    return (start["status"] if start else None), payload


def call(app, method: str, path: str, *, query: str = "",
         headers: dict[str, str] | None = None, body: bytes = b"",
         **scope_kwargs: Any) -> tuple[int | None, bytes]:
    """Call the ASGI app and return ``(status, body_bytes)``."""
    return asyncio.run(drive(app, scope(method, path, query=query, headers=headers,
                                       **scope_kwargs), body=body))


def json_call(app, method: str, path: str, **kwargs: Any) -> tuple[int | None, Any]:
    status, payload = call(app, method, path, **kwargs)
    if not payload:
        return status, None
    return status, json.loads(payload.decode("utf-8"))
