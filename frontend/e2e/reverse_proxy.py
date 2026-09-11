#!/usr/bin/env python3
"""PR-2 复审 Blocker 1：nginx 反代形态的**真实**回归夹具。

为什么需要它
------------
Playwright 默认直连 Uvicorn（``baseURL=http://127.0.0.1:8611``）。直连时浏览器
发出的 ``Host`` 天然带着端口，所以「``proxy_set_header Host $host`` 会丢掉非
默认端口」这个缺陷在直连 E2E 里**永远测不出来**——这正是复审指出的盲区。

CI 里没有 nginx，因此这里用一个最小的 HTTP 反向代理**复刻 nginx 的 Host 转发
行为**；浏览器是真的，``Origin`` / ``Sec-Fetch-Site`` 由真实 Chromium 生成，
而这恰恰是直连测试覆盖不到的部分。

两种形态（对应 ``deploy/astock-codex.nginx.conf`` 的两个版本）：

``preserve``
    等价 ``proxy_set_header Host $http_host``（**修复后**）。原样转发
    ``host:port``，后端 ``request.url.port`` 与浏览器 ``Origin`` 里的端口一致，
    同源判断成立 → 合法写请求放行。

``strip_port``
    等价 ``proxy_set_header Host $host``（**修复前**）。只转发主机名，后端把
    端口当成协议默认端口（http→80），与 ``Origin`` 里的真实端口不一致 →
    合法的同源写请求被判 ``cross_origin`` → 403。保留这个形态是为了让回归测试
    **自证有效**：它能复现缺陷，才说明它真能抓住回归。
"""
from __future__ import annotations

import http.client
import http.server
import threading

# 逐跳头（hop-by-hop，RFC 7230 §6.1）：不应向上游转发，也不应回传给客户端。
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def forward_host_value(raw_host: str, mode: str) -> str:
    """按 nginx 变量语义决定转发给上游的 ``Host`` 值。"""
    if mode == "preserve":
        return raw_host
    if mode == "strip_port":
        # 等价 ``$host``：丢掉端口；IPv6 字面量 ``[::1]:8612`` 一并剥离。
        if raw_host.startswith("["):
            end = raw_host.find("]")
            return raw_host[: end + 1] if end >= 0 else raw_host
        return raw_host.split(":")[0]
    raise ValueError(f"unknown host mode: {mode!r}")


class ReverseProxy:
    """最小 HTTP 反向代理（真实 TCP），复刻 nginx 的 ``Host`` 转发行为。"""

    def __init__(self, upstream_host, upstream_port, *, host_mode="preserve",
                 listen_port=0, name="proxy"):
        if host_mode not in ("preserve", "strip_port"):
            raise ValueError(f"unknown host_mode: {host_mode!r}")
        self.upstream = (upstream_host, upstream_port)
        self.host_mode = host_mode
        self.name = name
        # 记录每次请求收到的原始 Host，便于排障/断言（进程内可读）。
        self.seen_hosts = []
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静默：不污染 E2E 日志
                return

            def _relay(self):
                raw_host = self.headers.get("Host", "")
                proxy.seen_hosts.append(raw_host)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None

                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in _HOP_BY_HOP and key.lower() != "host"
                }
                headers["Host"] = forward_host_value(raw_host, proxy.host_mode)

                conn = http.client.HTTPConnection(*proxy.upstream, timeout=30)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    resp = conn.getresponse()
                    status = resp.status
                    resp_headers = resp.getheaders()
                    # http.client 会自动解出 chunked 正文，因此这里总是拿到完整
                    # body，可以统一用 Content-Length 回传（保持 HTTP/1.1 keep-alive）。
                    payload = resp.read()
                finally:
                    conn.close()

                self.send_response(status)
                for key, value in resp_headers:
                    if key.lower() in _HOP_BY_HOP or key.lower() == "content-length":
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD" and payload:
                    self.wfile.write(payload)

        for verb in _METHODS:
            setattr(Handler, f"do_{verb}", Handler._relay)

        class _QuietServer(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # 客户端提前断开（ConnectionReset/Aborted）是正常现象，静默处理。
                return

        self._httpd = _QuietServer(("127.0.0.1", listen_port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"astock-e2e-{name}",
            daemon=True,
        )

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
