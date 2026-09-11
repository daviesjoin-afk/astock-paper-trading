# -*- coding: utf-8 -*-
"""PR-2：HTTP 操作员安全边界（Operator Security Boundary）。

本模块把"谁可以改变系统状态"这一个问题收敛到唯一一处，供 HTTP 控制面统一调用。
它**只做操作员边界**：不引入账户系统、OAuth、JWT、RBAC、session 框架，也不涉及
任何交易 / 选股 / 风控 / 自进化算法。

## 认证模型（已人工 review，不得自行更改）

唯一公开的凭据形式：标准 ``Authorization`` 头，**Bearer 方案**——即把
``Bearer <token>``（scheme 与凭据之间恰好一个空格）作为该请求头的值。

凭据只能走这一个请求头。**不接受** query / URL / body / cookie 传递，也不接受
任何私有 header（例如历史上曾用过的私有 token 头，已删除）。这样可以避免
token 落入访问日志、浏览器历史与 Referer。

## 三种 token 配置状态（显式建模）

    UNSET   —— 环境变量不存在，或 trim 后为空
    VALID   —— 存在且 trim 后满足长度/字符要求
    INVALID —— 存在但强度不足（过短 / 含非法字符）

对应三种运行模式：

    local-only mode     UNSET，写操作仅允许环回客户端（仍受浏览器来源防护约束）
    authenticated mode  VALID，任何客户端的写操作都必须带 Bearer（localhost 无豁免）
    misconfigured mode  INVALID，所有写操作 503

## fail-closed

写操作默认拒绝。任何配置异常都宁可整体拒绝，也**绝不**静默降级为无鉴权、
降级为 UNSET、或降级为 local-only。本模块**不存在**任何关闭操作员边界的
环境开关（历史变量 ``ASTOCK_OPERATOR_AUTH_REQUIRED`` 已彻底删除）。

## 不信任转发头

来源 IP 判定**只**使用 ``request.client.host``，再用
:func:`ipaddress.ip_address(...).is_loopback` 判定环回。本模块不解析、不读取
``X-Forwarded-For`` / ``X-Real-IP`` / ``Forwarded``。代理信任边界由
Uvicorn 的 ``--proxy-headers --forwarded-allow-ips=127.0.0.1``（已在部署中启用）
负责，应用层不建立第二套 parser。
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
from enum import Enum

logger = logging.getLogger(__name__)

# ─── 常量 ───

TOKEN_ENV = "ASTOCK_OPERATOR_TOKEN"
HEADER_AUTHORIZATION = "authorization"

# 唯一被接受的 scheme 前缀（大小写不敏感比较）。
_BEARER_SCHEME = "bearer"

# 写方法：需要操作员边界。其余视为只读。
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# token 最短长度。低于此长度视为弱 token → INVALID（fail-closed），
# 而不是"降低标准放行"。
MIN_TOKEN_LENGTH = 24

# token 允许的字符；拒绝控制字符/空白，避免 header 注入与首尾不可见字符
# 导致的"配了但一直 401"。
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=\-]+$")

# 允许默认端口省略的规范化映射。
_DEFAULT_PORTS = {"http": 80, "https": 443}

# 浏览器来源防护：这些 Origin 值一律拒绝，不得解释成"没有 Origin"。
_NULL_ORIGINS = frozenset({"null"})

# 允许继续后续判断的 Sec-Fetch-Site 取值。
# cross-site 直接拒绝；其余（含缺失）交给 Origin / Auth / IP 策略决定。
_FETCH_SITE_ALLOWED = frozenset({"same-origin", "same-site", "none"})
_FETCH_SITE_BLOCKED = frozenset({"cross-site"})

# 对外契约文案（§14 / §15 固定，不得夹带 token 信息）。
DETAIL_AUTH_REQUIRED = "operator authentication required"
DETAIL_MISCONFIGURED = "operator authentication is misconfigured"
DETAIL_REMOTE_DISABLED = "remote operator mutations are disabled"
DETAIL_CROSS_SITE = "cross-site operator mutation is not allowed"
DETAIL_BAD_ORIGIN = "request origin is not allowed"
DETAIL_UNSUPPORTED_METHOD = "unsupported HTTP method"

# 运行模式（用于 /api/operator-status，不泄露 token 长度）。
MODE_LOCAL_ONLY = "local-only"
MODE_AUTHENTICATED = "authenticated"
MODE_MISCONFIGURED = "misconfigured"


class TokenConfigState(Enum):
    """token 配置的三种状态（显式建模，不允许散落猜测）。"""

    UNSET = "unset"
    VALID = "valid"
    INVALID = "invalid"


class TokenConfig:
    """一次进程内 token 配置的不可变快照。

    ``state`` 是权威判定；``token`` 仅在 ``state is VALID`` 时有值。
    ``reason`` 仅用于**内部日志与测试**，绝不进入 HTTP 响应。
    """

    __slots__ = ("state", "token", "reason")

    def __init__(self, state, token=None, reason="ok"):
        self.state = state
        self.token = token
        self.reason = reason

    def __repr__(self):  # pragma: no cover - 调试辅助（绝不回显 token）
        return f"TokenConfig(state={self.state.value!r}, reason={self.reason!r})"

    def __eq__(self, other):
        if not isinstance(other, TokenConfig):
            return NotImplemented
        return self.state == other.state and self.token == other.token

    @property
    def mode(self):
        """对应的运行模式名（供 /api/operator-status 与启动日志使用）。"""
        if self.state is TokenConfigState.VALID:
            return MODE_AUTHENTICATED
        if self.state is TokenConfigState.INVALID:
            return MODE_MISCONFIGURED
        return MODE_LOCAL_ONLY


class Decision:
    """一次鉴权判定的结果。

    ``allowed`` 为 True 表示放行；否则 ``status`` / ``detail`` 用于构造 HTTP 响应。
    ``reason`` 是稳定的机器可读标识，便于测试与审计。
    """

    __slots__ = ("allowed", "status", "detail", "reason")

    def __init__(self, allowed, status=200, detail="", reason="ok"):
        self.allowed = allowed
        self.status = status
        self.detail = detail
        self.reason = reason

    def __repr__(self):  # pragma: no cover - 调试辅助
        return (
            f"Decision(allowed={self.allowed!r}, status={self.status!r}, "
            f"reason={self.reason!r})"
        )

    def __eq__(self, other):
        if not isinstance(other, Decision):
            return NotImplemented
        return (
            self.allowed == other.allowed
            and self.status == other.status
            and self.reason == other.reason
        )


# ─── token 配置读取与校验 ───


def read_token_config(env=None):
    """读取并分类当前 token 配置，返回 :class:`TokenConfig`。

    - 环境变量不存在 / trim 后为空  → UNSET
    - trim 后长度 >= 24 且字符合法  → VALID
    - 存在但强度不足                → INVALID

    **空值（``""`` 或纯空白）属于 UNSET，不是 INVALID。** 这一点是刻意的：
    部署工具常把未设置的变量以空串形式注入（例如 compose 的
    ``VAR: "${VAR:-}"``），那表示"操作者没有配置"，而不是"配置了一个坏值"。
    两者虽然都拒绝远程写操作，但 UNSET 保留本机可用性，INVALID 则整体 503。
    """
    source = os.environ if env is None else env
    raw = source.get(TOKEN_ENV)
    if raw is None:
        return TokenConfig(TokenConfigState.UNSET, None, "missing")
    token = str(raw).strip()
    if not token:
        return TokenConfig(TokenConfigState.UNSET, None, "empty")
    if len(token) < MIN_TOKEN_LENGTH:
        return TokenConfig(TokenConfigState.INVALID, None, "too_short")
    if not _TOKEN_PATTERN.match(token):
        return TokenConfig(TokenConfigState.INVALID, None, "invalid_characters")
    return TokenConfig(TokenConfigState.VALID, token, "ok")


def validate_token(token):
    """校验 token 强度，返回 ``(ok, reason)``。

    保留此函数作为细粒度校验入口；不做任何"自动修正"。
    """
    if token is None:
        return False, "missing"
    value = str(token).strip()
    if not value:
        return False, "empty"
    if len(value) < MIN_TOKEN_LENGTH:
        return False, "too_short"
    if not _TOKEN_PATTERN.match(value):
        return False, "invalid_characters"
    return True, "ok"


# ─── Bearer 解析 ───


def parse_bearer(headers):
    """从 **``Authorization`` 头**解析 Bearer 凭据，返回 token 或 ``None``。

    严格语义（§11）。合法形态是 ``Bearer`` 加一个空格再加凭据；
    ``Bearer`` scheme 大小写不敏感（``bearer`` / ``BEARER`` / ``Bearer``）。

    非法（一律返回 ``None``）：

    - 其它 scheme（Basic / Token / Digest …）；
    - 只有 scheme 没有凭据，或凭据为空；
    - 凭据内部含空白（例如两个 token 连写）；
    - 完全没有 scheme，直接把原始 token 当头的值。

    绝不从 query / URL / body / cookie 读取凭据。
    """
    if headers is None:
        return None
    try:
        raw = headers.get(HEADER_AUTHORIZATION)
    except Exception:
        return None
    if not raw:
        return None
    value = str(raw)
    # 只按**第一个空格**切分 scheme 与 credential；多余空白视为非法。
    if " " not in value:
        return None
    scheme, _, credential = value.partition(" ")
    if scheme.strip().lower() != _BEARER_SCHEME:
        return None
    if not credential or credential != credential.strip():
        # 空 credential，或首尾带空白（含 "Bearer  a"、尾随空格）。
        return None
    if any(ch.isspace() for ch in credential):
        # token 内部含空白（"Bearer a b"）→ 非法。
        return None
    return credential


# ─── 客户端地址 ───


def is_loopback_client(host):
    """``host`` 是否为环回地址。无法解析一律返回 ``False``（fail-closed）。

    只接受传入的**已确定**主机串（调用方必须来自 ``request.client.host``）。
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(str(host).strip()).is_loopback
    except (ValueError, TypeError):
        return False


# ─── 浏览器来源防护 ───


def _split_origin(origin):
    """把 origin 字符串拆成 ``(scheme, host, port)``；非法返回 ``None``。"""
    if not origin:
        return None
    text = str(origin).strip()
    if not text:
        return None
    if text.lower() in _NULL_ORIGINS:
        # 调用方应单独处理 null；这里表示"不可作为合法 origin 比较"。
        return None
    if "://" not in text:
        return None
    scheme, _, rest = text.partition("://")
    scheme = scheme.strip().lower()
    if not scheme or not rest:
        return None
    rest = rest.split("/", 1)[0]  # 丢掉 path
    if not rest:
        return None
    # IPv6 字面量形如 [::1]:8000
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            return None
        host = rest[1:end]
        tail = rest[end + 1:]
        if tail.startswith(":"):
            port_text = tail[1:]
        elif tail == "":
            port_text = ""
        else:
            return None
    elif ":" in rest:
        host, _, port_text = rest.rpartition(":")
        if not host:
            return None
    else:
        host, port_text = rest, ""
    host = host.strip().lower()
    if not host:
        return None
    if port_text == "":
        port = _DEFAULT_PORTS.get(scheme)
    else:
        if not port_text.isdigit():
            return None
        port = int(port_text)
    if port is None:
        return None
    return scheme, host, port


def _split_host_header(value):
    """把 ``Host`` 头拆成 ``(host, port_or_None)``。

    ``Host`` 头常带端口（``127.0.0.1:8623`` / ``[::1]:8000``），而 ``Origin``
    的 host 部分**不含**端口（端口单独在 origin 的 port 位）。若直接把带端口的
    Host 与 origin 的 host 比较，合法的同源请求会被误判为跨源（403）。
    这里统一拆开，交给调用方按位比较。
    """
    if not value:
        return "", None
    text = str(value).strip()
    if not text:
        return "", None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return text.lower(), None
        host = text[1:end]
        tail = text[end + 1:]
        if tail.startswith(":") and tail[1:].isdigit():
            return host.lower(), int(tail[1:])
        return host.lower(), None
    if text.count(":") == 1:
        host, _, port_text = text.rpartition(":")
        if port_text.isdigit():
            return host.lower(), int(port_text)
        return text.lower(), None
    return text.lower(), None


def validate_origin(origin, *, scheme="http", host=None, server_port=None):
    """校验 ``Origin`` 是否与**服务器自身 origin** 同源。

    返回 ``(ok, reason)``。

    - Origin 缺失（``None``/空）→ ``(True, "absent")``：交给后续
      Auth / IP 策略判断（CLI / curl 常无 Origin）。
    - ``Origin: null`` → ``(False, "null_origin")``：不得解释成"没有 Origin"。
    - 解析失败 → ``(False, "malformed")``。
    - ``scheme`` / ``host`` / ``port`` 任一不一致 → ``(False, "cross_origin")``；
      默认端口做规范化（http→80，https→443）。

    ``host`` 允许传入带端口的 ``Host`` 头值（例如 ``127.0.0.1:8623``）——端口会被
    拆出来参与比较，避免合法的同源请求被误判。
    """
    if origin is None or not str(origin).strip():
        return True, "absent"
    text = str(origin).strip()
    if text.lower() in _NULL_ORIGINS:
        return False, "null_origin"
    parsed = _split_origin(text)
    if parsed is None:
        return False, "malformed"
    o_scheme, o_host, o_port = parsed

    server_scheme = str(scheme or "http").strip().lower()
    server_host, host_header_port = _split_host_header(host)
    if host_header_port is not None:
        # Host 头自带端口时以它为准（这正是客户端实际连的端口）。
        s_port = host_header_port
    elif server_port is None:
        s_port = _DEFAULT_PORTS.get(server_scheme)
    else:
        try:
            s_port = int(server_port)
        except (TypeError, ValueError):
            return False, "malformed_server"

    if not server_host:
        # 没有可信的服务器 host 就无法做同源比较 → fail-closed。
        return False, "no_server_host"
    if o_scheme != server_scheme:
        return False, "cross_origin"
    if o_host != server_host:
        return False, "cross_origin"
    if o_port != s_port:
        return False, "cross_origin"
    return True, "same_origin"


def is_cross_site(sec_fetch_site):
    """``Sec-Fetch-Site: cross-site`` 是否命中。

    返回 ``(cross_site, known)``。缺失/无法识别时 ``known=False``，
    调用方继续按 Origin / Auth / IP 策略判断（不因缺失而放行或拒绝）。
    """
    if sec_fetch_site is None:
        return False, False
    value = str(sec_fetch_site).strip().lower()
    if not value:
        return False, False
    if value in _FETCH_SITE_BLOCKED:
        return True, True
    if value in _FETCH_SITE_ALLOWED:
        return False, True
    # 未知取值：不据此放行，交由后续策略；但记录为"已识别"以便测试区分。
    return False, False


# ─── HTTP 请求头读取（大小写不敏感，兼容 dict 与 Starlette Headers）───


def _header(headers, name, default=None):
    if headers is None:
        return default
    try:
        value = headers.get(name)
    except Exception:
        return default
    if value is None:
        return default
    return value


# ─── 授权主入口 ───


def authorize_mutation(
    method,
    headers,
    client_host,
    *,
    scheme="http",
    host_header=None,
    server_port=None,
    token_config=None,
):
    """对一次 **写请求** 做完整判定，返回 :class:`Decision`。

    判定顺序（§25）：method → Sec-Fetch-Site → Origin → token 配置状态 →
    IP/Bearer 授权。跨站浏览器请求尽早挡住。

    只读方法（GET/HEAD/OPTIONS）不在本函数职责内——由 :func:`evaluate_request`
    直接放行。未知方法（TRACE/CONNECT…）返回 405。
    """
    verb = str(method or "").upper()

    if verb in READ_METHODS:
        return Decision(True, 200, "", "read_allowed")
    if verb not in WRITE_METHODS:
        return Decision(False, 405, DETAIL_UNSUPPORTED_METHOD, "unsupported_method")

    cfg = read_token_config() if token_config is None else token_config

    # ── 1. Sec-Fetch-Site：跨站浏览器请求直接拒绝，即使 Bearer 正确 ──
    cross_site, _known = is_cross_site(_header(headers, "sec-fetch-site"))
    if cross_site:
        return Decision(False, 403, DETAIL_CROSS_SITE, "cross_site")

    # ── 2. Origin：同源校验（null / 跨源 / 畸形一律拒绝）──
    origin = _header(headers, "origin")
    ok_origin, origin_reason = validate_origin(
        origin,
        scheme=scheme,
        host=host_header,
        server_port=server_port,
    )
    if not ok_origin:
        return Decision(False, 403, DETAIL_BAD_ORIGIN, f"origin_{origin_reason}")

    # ── 3. token 配置状态 ──
    if cfg.state is TokenConfigState.INVALID:
        # 配置了坏值：绝不 fallback 到 UNSET / local / disabled。
        return Decision(False, 503, DETAIL_MISCONFIGURED, "misconfigured")

    if cfg.state is TokenConfigState.UNSET:
        # local-only mode：仅环回客户端可写。
        if is_loopback_client(client_host):
            return Decision(True, 200, "", "local_only_allowed")
        return Decision(False, 403, DETAIL_REMOTE_DISABLED, "remote_disabled")

    # ── 4. VALID：任何客户端（含 localhost）都必须带正确 Bearer ──
    # 缺凭据与凭据错误必须**完全不可区分**（同一 status、同一 detail、同一
    # reason）——否则外部调用方能据此探测 token 是否存在 / 格式是否正确。
    presented = parse_bearer(headers)
    if presented is None or not hmac.compare_digest(
        presented.encode("utf-8"), cfg.token.encode("utf-8")
    ):
        return Decision(False, 401, DETAIL_AUTH_REQUIRED, "auth_failed")
    return Decision(True, 200, "", "authorized")


def evaluate_request(
    method,
    headers,
    client_host=None,
    *,
    scheme="http",
    host_header=None,
    server_port=None,
    token_config=None,
):
    """兼容入口：对单个请求做判定（含只读方法放行）。

    只读方法直接放行；其余转交 :func:`authorize_mutation`。
    ``headers`` 需支持大小写不敏感的 ``.get(name)``。
    """
    verb = str(method or "").upper()
    if verb in READ_METHODS:
        return Decision(True, 200, "", "read_allowed")
    if verb not in WRITE_METHODS:
        return Decision(False, 405, DETAIL_UNSUPPORTED_METHOD, "unsupported_method")
    return authorize_mutation(
        verb,
        headers,
        client_host,
        scheme=scheme,
        host_header=host_header,
        server_port=server_port,
        token_config=token_config,
    )


# ─── 响应头 ───


def challenge_headers(decision):
    """为未通过鉴权的响应生成建议响应头。

    401 附 ``WWW-Authenticate``（值为 ``Bearer``）；一律 ``Cache-Control: no-store``。
    """
    headers = {"Cache-Control": "no-store"}
    if decision is not None and decision.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return headers


# ─── 状态展示与启动日志（绝不泄露 token）───


def describe_configuration(token_config=None):
    """返回可安全对外展示的配置摘要。

    只暴露模式与布尔量；**不返回 token 长度、前缀或任何强度细节**。
    """
    cfg = read_token_config() if token_config is None else token_config
    return {
        "mode": cfg.mode,
        "token_configured": cfg.state is TokenConfigState.VALID,
        "writes_protected": cfg.state is not TokenConfigState.UNSET,
    }


def log_configuration(logger_=None):
    """把当前模式打进启动日志。

    只输出模式名；**禁止**输出 token 长度 / 值 / 前缀。
    """
    cfg = read_token_config()
    target = logger_ or logger
    if cfg.state is TokenConfigState.VALID:
        message = "operator auth mode: authenticated"
    elif cfg.state is TokenConfigState.INVALID:
        message = "operator auth misconfigured"
    else:
        message = "operator auth mode: local-only"
    target.warning(message) if cfg.state is TokenConfigState.INVALID else target.info(message)
    return message
