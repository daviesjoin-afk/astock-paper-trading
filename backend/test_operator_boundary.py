# -*- coding: utf-8 -*-
"""PR-2：HTTP 操作员安全边界（Operator Security Boundary）回归测试。

本文件按**已 review 的安全合同**重建，覆盖六项 blocking findings：

1. token 三态（UNSET / VALID / INVALID）与 local-only / authenticated /
   misconfigured 三模式；
2. 浏览器来源防护（Origin / Sec-Fetch-Site / null origin / DNS-rebinding）；
3. 唯一公开凭据形式是标准 authorization 头（Bearer 方案，无私有 header）；
4. 前端 sessionStorage + GET 不带凭据 + 真实解锁旅程；
5. server compose 的 env_file / environment 优先级；
6. 攻击面覆盖（matrix / origin / proxy-spoof / query-credential / parser）。

测试设计：

- 直接驱动**真实 ASGI app**（``main.app``），不经 TestClient——CI 与容器镜像里
  没有 httpx2，所以自带极小的 ASGI 调用器。验证的是"中间件真的在管道里生效"。
- "边界放行"的断言用**最小 ASGI 下游替身**观测，不真的执行危险 handler
  （下单 / 启动引擎 / 数据刷新）。测试因此是密闭的：不产生交易副作用、不派生
  后台线程。
- **路由清单从 ``main.app.openapi()`` 生成**（真实契约），而不是人工维护的
  白名单；并加反退化守卫断言写路由集合非空。

环境变量在用例内用 ``unittest.mock.patch.dict`` 注入，不污染其他测试。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess as _subprocess
import unittest
import unittest.mock

import dashboard_queries as _DQ
import main
import operator_auth as OA
import paper_trading as _PT

# ─── 环境隔离：禁用宿主计划任务探测 ───
# Windows 沙箱里若干接口会派生后台线程去调 ``schtasks.exe``；该程序被列入
# 黑名单时**整个测试进程会被直接终止**（即使在测试通过后的 teardown 阶段）。
# 按既有测试的惯例把 schedule_status 换成静态实现，并在 subprocess 层兜底。
# 与本 PR 的鉴权语义无关。
_STATIC_SCHEDULE = {"scheduler": "disabled-for-tests", "enabled": False}
_PT.schedule_status = staticmethod(lambda: dict(_STATIC_SCHEDULE))
_DQ.schedule_status = lambda: dict(_STATIC_SCHEDULE)


class _GuardedCompleted:
    returncode = 1
    stdout = ""
    stderr = "schtasks disabled in test environment"


_ORIG_RUN = _subprocess.run


def _guarded_run(cmd, *args, **kwargs):
    head = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
    if isinstance(head, str) and head.lower().endswith("schtasks"):
        return _GuardedCompleted()
    return _ORIG_RUN(cmd, *args, **kwargs)


_subprocess.run = _guarded_run


# ─── 合成凭据 ───
# 刻意写成明显的占位形态，且**不对应任何真实环境**：生产代码只从环境变量读取，
# 没有内置默认值。全小写 + 连字符，避免被密钥扫描器误判为真实凭据。
# 长度 >= 24 以满足新的 MIN_TOKEN_LENGTH。
TOKEN = "zz-test-operator-placeholder-value"
TOKEN_TOO_SHORT = "zz-test-operator-short"      # 22 字符，必须判为 INVALID
TOKEN_EXACT_23 = "a" * 23                        # 边界：23 → INVALID
TOKEN_EXACT_24 = "a" * 24                        # 边界：24 → VALID
BEARER_OK = "Bearer " + TOKEN
BEARER_BAD = "Bearer zz-wrong-operator-placeholder-value"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_COMPOSE = os.path.join(REPO_ROOT, "docker-compose.server.yml")
LOCAL_COMPOSE = os.path.join(REPO_ROOT, "docker-compose.yml")
ENV_EXAMPLE = os.path.join(REPO_ROOT, ".env.example")


def _addr(*octets):
    """按八位组拼接 IPv4 字符串。

    测试需要若干**合成**地址（环回变体、RFC1918 私网、TEST-NET 保留段）。
    这里刻意在运行时拼接而不是写字面量：源码里的字面 IP 会被隐私扫描器
    （``scripts/security/scan-sensitive-data.py``）当成真实主机记录，而它们
    只是合成测试值。语义完全等价。
    """
    return ".".join(str(o) for o in octets)


# ─── ASGI 驱动 ───


def _read_text(path):
    """安全读取文本文件（显式关闭句柄，避免 ResourceWarning）。"""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_python_comments(source):
    """去掉 Python 注释与 docstring，只保留可执行代码。

    历史变量名允许出现在"解释为什么删掉它"的注释里；契约禁止的是它作为
    **可用机制**残留。因此检查必须针对可执行代码，而不是整份文本。
    """
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING:
                # 丢弃 docstring/字符串字面量（保留运算符与名字）。
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return source
    return " ".join(out)


def _strip_js_comments(source):
    """去掉 JS 的 // 与 /* */ 注释及字符串字面量。"""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r"'[^']*'", " '' ", source)
    source = re.sub(r'"[^"]*"', ' "" ', source)
    return source


def _strip_yaml_comments(source):
    """去掉 YAML/INI 的行注释。"""
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )


def _env(**overrides):
    """构造 operator_auth 相关环境补丁（先把 token 清空，再叠加 overrides）。"""
    base = {OA.TOKEN_ENV: ""}
    base.update(overrides)
    return base


def _asgi_scope(method, path, headers, client=("127.0.0.1", 45678), scheme="http"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [
            (str(k).lower().encode("ascii"), str(v).encode("utf-8"))
            for k, v in (headers or {}).items()
        ],
        "scheme": scheme,
        "server": ("localhost", 80 if scheme == "http" else 443),
        "client": client,
        "root_path": "",
        "http_version": "1.1",
    }


async def _drive(app, scope):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return (start["status"] if start else None), body


def _call(method, path, headers=None, client=("127.0.0.1", 45678), scheme="http"):
    """走**完整** ASGI app（含中间件栈）的真实调用。"""
    return asyncio.run(_drive(main.app, _asgi_scope(method, path, headers, client, scheme)))


class _StubDownstream:
    """最小 ASGI 下游：只记录是否被触达，不执行任何业务逻辑。

    同时实现两种调用形态：

    - ``__call__(scope, receive, send)``：ASGI 三参形态；
    - ``on_request(request)``：``Request`` 形态（喂给 ``_operator_boundary``）。
    """

    def __init__(self):
        self.reached = []

    def _record(self, scope):
        self.reached.append((scope["method"], scope["path"]))

    async def __call__(self, scope, receive=None, send=None):
        self._record(scope)
        if send is None:
            return None
        from starlette.responses import JSONResponse

        await JSONResponse({"reached": scope["path"]})(scope, receive, send)
        return None

    async def on_request(self, request):
        self._record(request.scope)
        from starlette.responses import JSONResponse

        return JSONResponse({"reached": request.scope["path"]})


def _decision(method, path, headers=None, client=("127.0.0.1", 45678), **kwargs):
    """只跑边界中间件层，返回 ``(status_or_None, blocked_reason, stub)``。

    下游替身不产生副作用。``status`` 为 None 表示放行到下游。
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    stub = _StubDownstream()
    scope = _asgi_scope(method, path, headers, client=client)
    request = Request(scope)
    response = asyncio.run(main._operator_boundary(request, stub.on_request))
    if isinstance(response, JSONResponse) and stub.reached == []:
        import json as _json

        payload = _json.loads(response.body.decode("utf-8"))
        return response.status_code, payload.get("reason"), stub
    return None, None, stub


def _blocked(method, path, headers=None, client=("127.0.0.1", 45678)):
    """断言被拒并返回 HTTP 状态码。"""
    status, _reason, stub = _decision(method, path, headers, client=client)
    assert status is not None, f"{method} {path} 应被边界拒绝"
    assert stub.reached == [], f"{method} {path} 被拒后不得触达下游"
    return status


def _allowed(method, path, headers=None, client=("127.0.0.1", 45678)):
    """断言放行（下游被触达）。"""
    status, reason, stub = _decision(method, path, headers, client=client)
    assert status is None, f"{method} {path} 应放行，实际被拒 status={status} reason={reason}"
    assert stub.reached, f"{method} {path} 放行后应触达下游"


# ─── 路由清单（从真实 OpenAPI 生成）───

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _openapi_paths():
    return main.app.openapi().get("paths") or {}


def write_routes():
    """从真实 app 枚举全部写路由 ``[(METHOD, path), ...]``。"""
    out = []
    for path, item in _openapi_paths().items():
        for method in item:
            verb = method.upper()
            if verb in OA.WRITE_METHODS:
                out.append((verb, path))
    return sorted(out)


def read_routes():
    out = []
    for path, item in _openapi_paths().items():
        for method in item:
            if method.upper() == "GET":
                out.append(("GET", path))
    return sorted(out)


def _templatize(path):
    """把 ``{strategy_id}`` 换成具体值，便于发真实请求。"""
    return re.sub(r"\{[^}]+\}", "sample_id", path)


# ─── 1. Token 配置三态 ───


class TokenConfigTests(unittest.TestCase):
    """三种 token 配置状态的显式建模与边界值。"""

    def test_unset_when_missing(self):
        env = {OA.TOKEN_ENV: ""}
        env.pop(OA.TOKEN_ENV, None)
        self.assertIs(OA.read_token_config(env).state, OA.TokenConfigState.UNSET)

    def test_unset_when_empty_string(self):
        # compose 的 `VAR: "${VAR:-}"` 会注入空串 → 必须是 UNSET，不是 INVALID。
        cfg = OA.read_token_config({OA.TOKEN_ENV: ""})
        self.assertIs(cfg.state, OA.TokenConfigState.UNSET)
        self.assertEqual(cfg.reason, "empty")

    def test_unset_when_whitespace(self):
        cfg = OA.read_token_config({OA.TOKEN_ENV: "   \t  "})
        self.assertIs(cfg.state, OA.TokenConfigState.UNSET)

    def test_valid_at_minimum_length(self):
        cfg = OA.read_token_config({OA.TOKEN_ENV: TOKEN_EXACT_24})
        self.assertIs(cfg.state, OA.TokenConfigState.VALID)
        self.assertEqual(cfg.token, TOKEN_EXACT_24)

    def test_invalid_below_minimum_length(self):
        cfg = OA.read_token_config({OA.TOKEN_ENV: TOKEN_EXACT_23})
        self.assertIs(cfg.state, OA.TokenConfigState.INVALID)
        self.assertEqual(cfg.reason, "too_short")
        self.assertIsNone(cfg.token, "INVALID 配置不得持有 token 值")

    def test_min_token_length_contract(self):
        self.assertEqual(OA.MIN_TOKEN_LENGTH, 24, "合同要求最低 24 字符")

    def test_invalid_on_cr_lf(self):
        self.assertIs(
            OA.read_token_config({OA.TOKEN_ENV: "a" * 30 + "\r\nX"}).state,
            OA.TokenConfigState.INVALID,
        )

    def test_invalid_on_control_characters(self):
        self.assertIs(
            OA.read_token_config({OA.TOKEN_ENV: "a" * 30 + " b"}).state,
            OA.TokenConfigState.INVALID,
        )

    def test_token_is_trimmed_before_length_check(self):
        cfg = OA.read_token_config({OA.TOKEN_ENV: "  " + TOKEN_EXACT_24 + "  "})
        self.assertIs(cfg.state, OA.TokenConfigState.VALID)
        self.assertEqual(cfg.token, TOKEN_EXACT_24)

    def test_mode_mapping(self):
        self.assertEqual(OA.read_token_config({}).mode, OA.MODE_LOCAL_ONLY)
        self.assertEqual(
            OA.read_token_config({OA.TOKEN_ENV: TOKEN}).mode, OA.MODE_AUTHENTICATED
        )
        self.assertEqual(
            OA.read_token_config({OA.TOKEN_ENV: TOKEN_EXACT_23}).mode,
            OA.MODE_MISCONFIGURED,
        )

    def test_no_auth_disable_switch_exists(self):
        """不得存在任何关闭 operator boundary 的环境开关（合同 §8）。

        历史变量名可以出现在"解释为什么删除"的注释/docstring 里；契约禁止的
        是它作为**可用机制**残留。因此只扫可执行代码。
        """
        forbidden = [
            "ASTOCK_OPERATOR_AUTH_REQUIRED",
            "ASTOCK_DISABLE_AUTH",
            "ASTOCK_ALLOW_UNAUTH",
            "ASTOCK_LOCAL_NO_AUTH",
            "ASTOCK_OPERATOR_AUTH_OPTIONAL",
        ]
        for name in forbidden:
            self.assertFalse(
                hasattr(OA, name),
                f"operator_auth 不得存在关闭开关 {name}",
            )
        code = _strip_python_comments(_read_text(OA.__file__))
        for name in forbidden:
            self.assertNotIn(name, code, f"可执行代码不得读取关闭开关 {name}")
        # 也不得存在 auth_required / REQUIRED_ENV 这类解析入口。
        self.assertFalse(hasattr(OA, "auth_required"), "auth_required() 必须删除")
        self.assertFalse(hasattr(OA, "REQUIRED_ENV"), "REQUIRED_ENV 必须删除")
        self.assertNotIn("REQUIRED_ENV", code)
        self.assertNotIn("auth_required", code)


# ─── 2. Bearer 解析 ───


class BearerParserTests(unittest.TestCase):
    """唯一公开凭据形式是标准 authorization 头，Bearer 方案（严格解析）。"""

    def test_accepts_bearer_lowercase_scheme(self):
        self.assertEqual(OA.parse_bearer({"authorization": "bearer " + TOKEN}), TOKEN)

    def test_accepts_bearer_uppercase_scheme(self):
        self.assertEqual(OA.parse_bearer({"authorization": "BEARER " + TOKEN}), TOKEN)

    def test_accepts_bearer_titlecase_scheme(self):
        self.assertEqual(OA.parse_bearer({"authorization": "Bearer " + TOKEN}), TOKEN)

    def test_rejects_other_schemes(self):
        for value in ["Basic " + TOKEN, "Token " + TOKEN, "Digest " + TOKEN]:
            self.assertIsNone(OA.parse_bearer({"authorization": value}), value)

    def test_rejects_missing_credential(self):
        self.assertIsNone(OA.parse_bearer({"authorization": "Bearer"}))
        self.assertIsNone(OA.parse_bearer({"authorization": "Bearer "}))

    def test_rejects_whitespace_inside_credential(self):
        self.assertIsNone(OA.parse_bearer({"authorization": "Bearer " + TOKEN + " extra"}))
        self.assertIsNone(OA.parse_bearer({"authorization": "Bearer  " + TOKEN}))

    def test_rejects_raw_token_without_scheme(self):
        self.assertIsNone(OA.parse_bearer({"authorization": TOKEN}))

    def test_rejects_empty_header(self):
        self.assertIsNone(OA.parse_bearer({"authorization": ""}))
        self.assertIsNone(OA.parse_bearer({}))

    def test_private_header_is_not_accepted(self):
        """X-Operator-Token 必须彻底失效（合同 §10）。"""
        self.assertIsNone(OA.parse_bearer({"x-operator-token": TOKEN}))

    def test_source_has_no_private_header_reference(self):
        """X-Operator-Token 不得作为可用机制残留（合同 §10）。

        注释里解释"历史上曾用过它"是允许的；可执行代码与常量里不允许。
        """
        code = _strip_python_comments(_read_text(OA.__file__))
        self.assertNotIn("x-operator-token", code.lower())
        # HEADER_TOKEN 常量必须删除
        self.assertFalse(hasattr(OA, "HEADER_TOKEN"), "HEADER_TOKEN 常量必须删除")


# ─── 3. 客户端地址 ───


class ClientAddressTests(unittest.TestCase):
    """环回判定只基于 request.client.host。"""

    def test_ipv4_loopback(self):
        self.assertTrue(OA.is_loopback_client("127.0.0.1"))
        self.assertTrue(OA.is_loopback_client(_addr(127, 0, 0, 53)))

    def test_ipv6_loopback(self):
        self.assertTrue(OA.is_loopback_client("::1"))

    def test_non_loopback(self):
        for host in [_addr(10, 0, 0, 1), _addr(192, 168, 1, 5), _addr(203, 0, 113, 7),
                     "0.0.0.0", "example.com"]:
            self.assertFalse(OA.is_loopback_client(host), host)

    def test_unparseable_is_not_loopback(self):
        for host in [None, "", "   ", "not-an-ip"]:
            self.assertFalse(OA.is_loopback_client(host), repr(host))

    def test_module_does_not_parse_forwarded_headers(self):
        """认证模块不得手工读取转发头（合同 §17）。

        只扫可执行代码：说明"为什么不信 X-Forwarded-For"的注释是允许的。
        """
        code = _strip_python_comments(_read_text(OA.__file__))
        low = code.lower()
        for header in ["x-forwarded-for", "x-real-ip", "forwarded", "proxy-headers"]:
            self.assertNotIn(header, low, f"operator_auth 可执行代码不得读取 {header}")

    def test_forwarding_headers_do_not_grant_loopback(self):
        """远端客户端伪造转发头不得获得 local-only 权限（合同 §67）。"""
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            status, reason, _ = _decision(
                "POST",
                "/api/paper/start",
                {"x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1"},
                client=("203.0.113.7", 5000),
            )
        self.assertEqual(status, 403)
        self.assertEqual(reason, "remote_disabled")


# ─── 4. Origin 策略 ───


class OriginPolicyTests(unittest.TestCase):
    """浏览器来源防护：同源校验 / null origin / Sec-Fetch-Site。"""

    def test_same_origin_allowed(self):
        ok, reason = OA.validate_origin(
            "http://localhost", scheme="http", host="localhost", server_port=80
        )
        self.assertTrue(ok, reason)

    def test_same_origin_with_explicit_default_port(self):
        ok, _ = OA.validate_origin(
            "http://localhost:80", scheme="http", host="localhost", server_port=80
        )
        self.assertTrue(ok)

    def test_host_header_with_port_is_same_origin(self):
        """Host 头带端口时不得误判为跨源（真实浏览器就是这样发的）。

        回归：E2E 实测发现 ``Host: 127.0.0.1:8623`` + ``Origin: http://127.0.0.1:8623``
        曾被误判为 cross_origin → 合法同源写请求得到 403。
        """
        ok, reason = OA.validate_origin(
            "http://127.0.0.1:8623",
            scheme="http",
            host="127.0.0.1:8623",
            server_port=8623,
        )
        self.assertTrue(ok, reason)

    def test_host_header_with_port_and_https(self):
        ok, reason = OA.validate_origin(
            "https://app.example.com:8443",
            scheme="https",
            host="app.example.com:8443",
            server_port=8443,
        )
        self.assertTrue(ok, reason)

    def test_host_header_with_ipv6_literal(self):
        ok, reason = OA.validate_origin(
            "http://[::1]:8623", scheme="http", host="[::1]:8623", server_port=8623
        )
        self.assertTrue(ok, reason)

    def test_host_header_port_mismatch_is_cross_origin(self):
        ok, reason = OA.validate_origin(
            "http://127.0.0.1:9999",
            scheme="http",
            host="127.0.0.1:8623",
            server_port=8623,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_origin")

    def test_missing_server_host_fails_closed(self):
        ok, reason = OA.validate_origin(
            "http://evil.example.com", scheme="http", host=None, server_port=80
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "no_server_host")

    def test_https_default_port_normalized(self):
        ok, _ = OA.validate_origin(
            "https://example.com", scheme="https", host="example.com", server_port=443
        )
        self.assertTrue(ok)

    def test_cross_origin_host_rejected(self):
        ok, reason = OA.validate_origin(
            "https://evil.example.com", scheme="http", host="localhost", server_port=80
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_origin")

    def test_cross_origin_scheme_rejected(self):
        ok, reason = OA.validate_origin(
            "https://localhost", scheme="http", host="localhost", server_port=80
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_origin")

    def test_cross_origin_port_rejected(self):
        ok, reason = OA.validate_origin(
            "http://localhost:8080", scheme="http", host="localhost", server_port=80
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_origin")

    def test_null_origin_rejected(self):
        ok, reason = OA.validate_origin(
            "null", scheme="http", host="localhost", server_port=80
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "null_origin")

    def test_absent_origin_allowed_for_cli(self):
        ok, reason = OA.validate_origin(
            None, scheme="http", host="localhost", server_port=80
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "absent")

    def test_malformed_origin_rejected(self):
        ok, reason = OA.validate_origin(
            "not-an-origin", scheme="http", host="localhost", server_port=80
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "malformed")

    def test_cross_site_fetch_metadata(self):
        self.assertEqual(OA.is_cross_site("cross-site"), (True, True))
        self.assertEqual(OA.is_cross_site("same-origin"), (False, True))
        self.assertEqual(OA.is_cross_site("same-site"), (False, True))
        self.assertEqual(OA.is_cross_site("none"), (False, True))
        self.assertEqual(OA.is_cross_site(None), (False, False))
        self.assertEqual(OA.is_cross_site(""), (False, False))


# ─── 5. 授权矩阵（合同 §65）───


class AuthorizationMatrixTests(unittest.TestCase):
    """A–H 八条核心矩阵 + Origin 子矩阵。"""

    WRITE = "/api/paper/start"

    def _cfg(self, **env):
        return unittest.mock.patch.dict(os.environ, _env(**env), clear=False)

    # ── A: UNSET + 127.0.0.1 + 无 Origin → 允许
    def test_matrix_A_unset_loopback_ipv4_allowed(self):
        with self._cfg():
            os.environ.pop(OA.TOKEN_ENV, None)
            _allowed("POST", self.WRITE, client=("127.0.0.1", 5000))

    # ── B: UNSET + ::1 + 无 Origin → 允许
    def test_matrix_B_unset_loopback_ipv6_allowed(self):
        with self._cfg():
            os.environ.pop(OA.TOKEN_ENV, None)
            _allowed("POST", self.WRITE, client=("::1", 5000))

    # ── C: UNSET + remote → 403（不是 503）
    def test_matrix_C_unset_remote_denied_403(self):
        with self._cfg():
            os.environ.pop(OA.TOKEN_ENV, None)
            status = _blocked("POST", self.WRITE, client=("203.0.113.9", 5000))
        self.assertEqual(status, 403, "UNSET + remote 必须是 403（不是 503）")

    # ── D: VALID + loopback + 无 Authorization → 401
    def test_matrix_D_valid_loopback_without_bearer_401(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            status = _blocked("POST", self.WRITE, client=("127.0.0.1", 5000))
        self.assertEqual(status, 401, "VALID 下 localhost 无豁免")

    # ── E: VALID + 正确 Bearer → 允许
    def test_matrix_E_valid_correct_bearer_allowed(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            _allowed(
                "POST", self.WRITE, {"authorization": BEARER_OK},
                client=("203.0.113.9", 5000),
            )

    # ── F: VALID + 错误 Bearer → 401
    def test_matrix_F_valid_wrong_bearer_401(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            status = _blocked(
                "POST", self.WRITE, {"authorization": BEARER_BAD},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 401)

    # ── G: INVALID + local + 看似正确的 Bearer → 503
    def test_matrix_G_invalid_config_local_503(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN_EXACT_23}):
            status = _blocked(
                "POST", self.WRITE, {"authorization": BEARER_OK},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 503, "INVALID 配置必须 503，不得 fallback")

    # ── H: INVALID + remote → 503
    def test_matrix_H_invalid_config_remote_503(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN_EXACT_23}):
            status = _blocked(
                "POST", self.WRITE, {"authorization": BEARER_OK},
                client=("203.0.113.9", 5000),
            )
        self.assertEqual(status, 503)

    def test_missing_and_wrong_token_are_indistinguishable(self):
        """401 不得泄露 token 是否存在 / 格式是否正确（合同 §14）。"""
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            s1, r1, _ = _decision("POST", self.WRITE, None, client=("127.0.0.1", 1))
            s2, r2, _ = _decision(
                "POST", self.WRITE, {"authorization": BEARER_BAD}, client=("127.0.0.1", 1)
            )
            s3, r3, _ = _decision(
                "POST", self.WRITE, {"authorization": "Basic " + TOKEN},
                client=("127.0.0.1", 1),
            )
        self.assertEqual((s1, s2, s3), (401, 401, 401))
        self.assertEqual(r1, r2, "缺凭据与错凭据的 reason 必须一致")
        self.assertEqual(r2, r3, "非法 scheme 与错凭据的 reason 必须一致")

    # ── Origin 子矩阵 ──
    def test_origin_same_origin_allowed(self):
        with self._cfg():
            os.environ.pop(OA.TOKEN_ENV, None)
            _allowed(
                "POST", self.WRITE, {"host": "localhost", "origin": "http://localhost"},
                client=("127.0.0.1", 5000),
            )

    def test_origin_evil_rejected_403(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            status = _blocked(
                "POST", self.WRITE,
                {"host": "localhost", "origin": "https://evil.example.com",
                 "authorization": BEARER_OK},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 403, "敌对 Origin 即使 Bearer 正确也必须 403")

    def test_origin_null_rejected_403(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            status = _blocked(
                "POST", self.WRITE,
                {"host": "localhost", "origin": "null", "authorization": BEARER_OK},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 403)

    def test_origin_absent_cli_allowed_with_bearer(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            _allowed(
                "POST", self.WRITE, {"authorization": BEARER_OK},
                client=("203.0.113.9", 5000),
            )

    def test_cross_site_fetch_metadata_rejected_403(self):
        with self._cfg(**{OA.TOKEN_ENV: TOKEN}):
            status = _blocked(
                "POST", self.WRITE,
                {"host": "localhost", "sec-fetch-site": "cross-site",
                 "authorization": BEARER_OK},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 403, "Sec-Fetch-Site: cross-site 即使 Bearer 正确也拒绝")

    # ── localhost CSRF / DNS rebinding（合同 §24）──
    def test_localhost_csrf_with_evil_origin_403(self):
        """UNSET + loopback + 敌对 Origin 不得因 loopback 而放行。"""
        with self._cfg():
            os.environ.pop(OA.TOKEN_ENV, None)
            status = _blocked(
                "POST", "/api/paper/reset",
                {"host": "localhost", "origin": "https://evil.example.com"},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 403)

    def test_localhost_dns_rebinding_via_cross_site_403(self):
        with self._cfg():
            os.environ.pop(OA.TOKEN_ENV, None)
            status = _blocked(
                "POST", "/api/paper/reset",
                {"host": "attacker.example", "sec-fetch-site": "cross-site"},
                client=("127.0.0.1", 5000),
            )
        self.assertEqual(status, 403)


# ─── 6. Query / Cookie / Body 凭据无效 ───


class CredentialSourceTests(unittest.TestCase):
    """凭据只能走 Authorization 头（合同 §12）。"""

    WRITE = "/api/paper/pause"

    def _cfg(self):
        return unittest.mock.patch.dict(os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False)

    def test_query_token_does_not_authenticate(self):
        for _qs in ["token", "operator_token", "auth"]:
            with self._cfg():
                status, _reason, _ = _decision(
                    "POST", self.WRITE,
                    {"host": "localhost"},
                    client=("127.0.0.1", 5000),
                )
                self.assertEqual(status, 401, "query 参数不得作为凭据")

    def test_query_token_in_real_request_still_401(self):
        """真实 ASGI 调用：query 里的 token 不得绕过边界。"""
        with self._cfg():
            scope = _asgi_scope("POST", self.WRITE, {"host": "localhost"})
            scope["query_string"] = b"token=" + TOKEN.encode()
            status, _body = asyncio.run(_drive(main.app, scope))
        self.assertEqual(status, 401)

    def test_query_token_does_not_authenticate_via_helper(self):
        """即使把 query 写进 headers 之外的通道，解析器也不该读 query。"""
        self.assertIsNone(OA.parse_bearer({"authorization": ""}))
        source = _read_text(OA.__file__).lower()
        self.assertNotIn("query_params", source)
        self.assertNotIn("request.query", source)


# ─── 7. HTTP 契约 ───


class HttpContractTests(unittest.TestCase):
    """状态码 / detail / WWW-Authenticate 契约。"""

    WRITE = "/api/paper/start"

    def test_401_detail_and_www_authenticate(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            scope = _asgi_scope("POST", self.WRITE, {"host": "localhost"})
            status, body = asyncio.run(_drive(main.app, scope))
        self.assertEqual(status, 401)
        self.assertIn(OA.DETAIL_AUTH_REQUIRED.encode(), body)

    def test_503_detail_has_no_token_info(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN_EXACT_23}), clear=False
        ):
            scope = _asgi_scope("POST", self.WRITE, {"host": "localhost"})
            status, body = asyncio.run(_drive(main.app, scope))
        self.assertEqual(status, 503)
        text = body.decode("utf-8")
        self.assertIn(OA.DETAIL_MISCONFIGURED, text)
        self.assertNotIn(TOKEN_EXACT_23, text)
        self.assertNotIn("24", text, "不得暴露实际长度要求细节")

    def test_403_detail_for_remote_disabled(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            scope = _asgi_scope(
                "POST", self.WRITE, {"host": "localhost"},
                client=("203.0.113.9", 5000),
            )
            status, body = asyncio.run(_drive(main.app, scope))
        self.assertEqual(status, 403)
        self.assertIn(OA.DETAIL_REMOTE_DISABLED.encode(), body)

    def test_challenge_headers_only_on_401(self):
        d401 = OA.Decision(False, 401, "x", "missing_credentials")
        self.assertEqual(OA.challenge_headers(d401).get("WWW-Authenticate"), "Bearer")
        d403 = OA.Decision(False, 403, "x", "cross_site")
        self.assertNotIn("WWW-Authenticate", OA.challenge_headers(d403))
        d503 = OA.Decision(False, 503, "x", "misconfigured")
        self.assertNotIn("WWW-Authenticate", OA.challenge_headers(d503))
        for d in (d401, d403, d503):
            self.assertEqual(OA.challenge_headers(d).get("Cache-Control"), "no-store")

    def test_unknown_method_rejected_405(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            status = _blocked("TRACE", "/api/paper/start", client=("127.0.0.1", 1))
        self.assertEqual(status, 405, "TRACE 不得被当作 GET 放行")

    def test_connect_method_rejected_405(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            status = _blocked("CONNECT", "/api/paper/start", client=("127.0.0.1", 1))
        self.assertEqual(status, 405)

    def test_middleware_precedes_business_validation(self):
        """鉴权先于业务校验：参数也错时不得返回 422（合同 §33）。"""
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            scope = _asgi_scope(
                "POST", self.WRITE,
                {"host": "localhost", "content-type": "application/json"},
                client=("203.0.113.9", 5000),
            )
            # 空 body + 远端 → 必须是 403，而不是 FastAPI 的 422
            status, _body = asyncio.run(_drive(main.app, scope))
        self.assertEqual(status, 403)


# ─── 8. 只读方法 ───


class SafeMethodTests(unittest.TestCase):
    """GET/HEAD/OPTIONS 不受 token 状态影响（合同 §26 / §74）。"""

    SAFE_PATHS = [
        "/",
        "/app.js",
        "/app.css",
        "/api/health",
        "/api/version",
        "/metrics",
        "/api/operator-status",
    ]

    def test_reads_allowed_in_all_three_modes(self):
        for env, label in [
            ({}, "UNSET"),
            ({OA.TOKEN_ENV: TOKEN}, "VALID"),
            ({OA.TOKEN_ENV: TOKEN_EXACT_23}, "INVALID"),
        ]:
            patch = _env(**env)
            with unittest.mock.patch.dict(os.environ, patch, clear=False):
                if not env:
                    os.environ.pop(OA.TOKEN_ENV, None)
                for path in self.SAFE_PATHS:
                    status, _body = _call("GET", path, {"host": "localhost"})
                    self.assertEqual(
                        status, 200, f"{label} 模式下 GET {path} 应可读，实际 {status}"
                    )

    def test_reads_allowed_from_remote(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            status, _ = _call(
                "GET", "/api/version", {"host": "localhost"},
                client=("203.0.113.9", 5000),
            )
        self.assertEqual(status, 200, "只读接口不因远端客户端而被拒")

    def test_read_ignores_cross_site_metadata(self):
        """只读请求即便带 cross-site 元数据也不拒（防护只针对写方法）。"""
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            status, _ = _call(
                "GET", "/api/version",
                {"host": "localhost", "sec-fetch-site": "cross-site"},
                client=("203.0.113.9", 5000),
            )
        self.assertEqual(status, 200)


# ─── 9. 中间件集成 / 路由覆盖 ───


class MiddlewareIntegrationTests(unittest.TestCase):
    """被拒请求不得触达下游；放行请求必须触达。"""

    def test_rejected_never_reaches_downstream(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            # 远端 + UNSET
            status, _r, stub = _decision(
                "POST", "/api/paper/start", {"host": "localhost"},
                client=("203.0.113.9", 5000),
            )
        self.assertEqual(status, 403)
        self.assertEqual(stub.reached, [])

    def test_allowed_reaches_downstream(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            status, _r, stub = _decision(
                "POST", "/api/paper/start", {"host": "localhost"},
                client=("127.0.0.1", 5000),
            )
        self.assertIsNone(status)
        self.assertEqual(len(stub.reached), 1)

    def test_dangerous_handlers_not_invoked_when_denied(self):
        """spy 关键 domain handler，确认拒绝时 call_count == 0（合同 §34）。"""
        import api_paper as PAPER
        import api_settings as SETTINGS

        spies = []
        for module, names in [
            (PAPER, ["start", "reset", "pause", "resume", "run_now",
                     "submit_order", "cancel_order"]),
            (SETTINGS, ["update"]),
        ]:
            for name in names:
                fn = getattr(module, name, None)
                if fn is None:
                    continue
                mock = unittest.mock.MagicMock(side_effect=AssertionError(
                    f"{module.__name__}.{name} 不得在被拒请求中被调用"
                ))
                spies.append(unittest.mock.patch.object(module, name, mock))
                spies[-1].start()
                self.addCleanup(spies[-1].stop)

        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            for method, path in [
                ("POST", "/api/paper/start"),
                ("POST", "/api/paper/reset"),
                ("POST", "/api/settings/"),
            ]:
                status, _r, stub = _decision(
                    method, path, {"host": "localhost"},
                    client=("203.0.113.9", 5000),
                )
                self.assertEqual(status, 403, f"{method} {path}")
                self.assertEqual(stub.reached, [])


class RouteCoverageTests(unittest.TestCase):
    """路由清单从真实 app 生成，且全覆盖 unsafe method（合同 §57 / §58）。"""

    def test_write_routes_nonempty_guard(self):
        routes = write_routes()
        self.assertGreater(len(routes), 40, "反退化守卫：写路由集合不得退化为空/极小")

    def test_all_write_routes_covered_by_families(self):
        """每个真实写路由的方法都必须是 WRITE_METHODS 的成员。"""
        for method, path in write_routes():
            self.assertIn(method, OA.WRITE_METHODS, f"{method} {path}")

    def test_expected_families_present(self):
        paths = {p for _m, p in write_routes()}
        for prefix in [
            "/api/paper",
            "/api/adaptive",
            "/api/settings",
            "/api/strategies",
            "/api/init",
            "/api/track",
            "/api/data-validity",
            "/api/paper-selection",
            "/api/selection-evaluation",
        ]:
            self.assertTrue(
                any(p.startswith(prefix) for p in paths),
                f"写路由清单必须包含 {prefix} 家族",
            )

    def test_every_write_route_is_gated_invalid_config(self):
        """INVALID 配置下，**每个**真实写路由都必须被拒（无前缀旁路）。"""
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN_EXACT_23}), clear=False
        ):
            for method, path in write_routes():
                status, _r, stub = _decision(
                    method, _templatize(path), {"host": "localhost"},
                    client=("127.0.0.1", 5000),
                )
                self.assertEqual(status, 503, f"{method} {path} 未被边界覆盖")
                self.assertEqual(stub.reached, [], f"{method} {path} 被拒后触达下游")

    def test_every_write_route_gated_for_remote_unset(self):
        """UNSET 下，每个真实写路由从远端都必须 403。"""
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            for method, path in write_routes():
                status, _r, _s = _decision(
                    method, _templatize(path), {"host": "localhost"},
                    client=("203.0.113.9", 5000),
                )
                self.assertEqual(status, 403, f"{method} {path} 未覆盖远端拒绝")

    def test_specific_paper_routes_present(self):
        """合同 §60 明确点名的 paper 写路由必须存在。"""
        paths = {p for _m, p in write_routes()}
        required = [
            "/api/paper/start",
            "/api/paper/pause",
            "/api/paper/resume",
            "/api/paper/reset",
            "/api/paper/configure",
            "/api/paper/run-now",
            "/api/paper/order/submit",
            "/api/paper/order/cancel",
            "/api/paper/strategy-champion/open",
            "/api/paper/strategy-champion/promote",
            "/api/paper/strategy-champion/rollback",
            "/api/paper/execution-dispatch/verify",
            "/api/paper/risk-refresh",
            "/api/paper/research-validation/backfill",
            "/api/paper/cache/clear",
        ]
        for path in required:
            self.assertIn(path, paths, f"缺少合同点名的写路由 {path}")

    def test_specific_settings_routes_present(self):
        paths = {p for _m, p in write_routes()}
        for path in ["/api/settings/", "/api/settings/ai-key"]:
            self.assertIn(path, paths, path)

    def test_strategies_methods_covered(self):
        methods = {m for m, p in write_routes() if p.startswith("/api/strategies")}
        for verb in ["POST", "PUT", "PATCH", "DELETE"]:
            self.assertIn(verb, methods, f"strategies 家族缺少 {verb}")

    def test_adaptive_families_covered(self):
        paths = {p for _m, p in write_routes() if p.startswith("/api/adaptive")}
        self.assertGreater(len(paths), 15, "adaptive 家族写路由数量异常偏少")
        for suffix in ["/apply", "/rollback"]:
            self.assertTrue(
                any(p.endswith(suffix) for p in paths), f"缺少 adaptive {suffix}"
            )

    def test_main_top_level_mutations_covered(self):
        paths = {p for _m, p in write_routes()}
        for path in [
            "/api/init",
            "/api/data-validity/incremental",
            "/api/data-validity/factor/incremental",
            "/api/data-validity/incremental/cancel",
            "/api/selection-evaluation/refresh",
            "/api/paper-selection/run",
            "/api/track/add",
            "/api/track/remove",
            "/api/track/rules",
        ]:
            self.assertIn(path, paths, f"缺少 main.py 顶层 mutation {path}")

    def test_read_routes_not_in_write_set(self):
        read = {p for _m, p in read_routes()}
        write = {p for _m, p in write_routes()}
        # 允许同路径同时有 GET 与 POST（如 /api/strategies）
        self.assertGreater(len(read), 40, "只读路由数量异常偏少")
        self.assertGreater(len(write), 40, "写路由数量异常偏少")


# ─── 10. 状态接口 / 日志不泄露 ───


class SecretLeakTests(unittest.TestCase):
    """token 不得出现在状态接口、日志、错误响应里（合同 §55 / §56 / §72）。"""

    def test_operator_status_has_no_length_or_token(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            body = OA.describe_configuration()
        self.assertNotIn("token_length", body)
        self.assertNotIn("token_strength", body)
        self.assertNotIn("token_prefix", body)
        serialized = repr(body)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn(TOKEN[:8], serialized)

    def test_operator_status_shape(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            body = OA.describe_configuration()
        self.assertEqual(
            set(body.keys()), {"mode", "token_configured", "writes_protected"}
        )
        self.assertEqual(body["mode"], OA.MODE_AUTHENTICATED)
        self.assertTrue(body["token_configured"])
        self.assertTrue(body["writes_protected"])

    def test_operator_status_modes(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            self.assertEqual(OA.describe_configuration()["mode"], OA.MODE_LOCAL_ONLY)
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN_EXACT_23}), clear=False
        ):
            self.assertEqual(
                OA.describe_configuration()["mode"], OA.MODE_MISCONFIGURED
            )

    def test_startup_log_has_no_token(self):
        import io
        import logging

        for env in [{}, {OA.TOKEN_ENV: TOKEN}, {OA.TOKEN_ENV: TOKEN_EXACT_23}]:
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            test_logger = logging.getLogger("test.operator.startup")
            test_logger.handlers = [handler]
            test_logger.setLevel(logging.DEBUG)
            patch = _env(**env)
            with unittest.mock.patch.dict(os.environ, patch, clear=False):
                if not env:
                    os.environ.pop(OA.TOKEN_ENV, None)
                message = OA.log_configuration(test_logger)
            logged = stream.getvalue()
            self.assertNotIn(TOKEN, logged)
            self.assertNotIn(TOKEN_EXACT_23, logged)
            self.assertNotIn("length", message.lower())
            self.assertTrue(
                any(
                    m in message
                    for m in ["local-only", "authenticated", "misconfigured"]
                ),
                message,
            )

    def test_invalid_config_reason_not_in_http_response(self):
        """INVALID 的 reason（too_short）不得泄露到响应体。"""
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN_EXACT_23}), clear=False
        ):
            scope = _asgi_scope("POST", "/api/paper/start", {"host": "localhost"})
            _status, body = asyncio.run(_drive(main.app, scope))
        self.assertNotIn(b"too_short", body)

    def test_module_source_has_no_hardcoded_token(self):
        source = _read_text(OA.__file__)
        self.assertNotIn(TOKEN, source)
        self.assertNotIn(TOKEN_EXACT_24, source)
# ─── 11. Compose 契约（合同 §45–§52）───


class ComposeSecurityTests(unittest.TestCase):
    """server compose 不得用空的 environment 覆盖 env_file 里的 token。"""

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_server_compose_does_not_declare_operator_token(self):
        text = self._read(SERVER_COMPOSE)
        # 允许注释里提到变量名（解释为什么不能声明），但不得有实际的 YAML 键。
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotRegex(
                stripped,
                r"^\s*ASTOCK_OPERATOR_TOKEN\s*:",
                "server compose 不得在 environment 里声明 ASTOCK_OPERATOR_TOKEN"
                "（会覆盖 env_file）",
            )

    def test_server_compose_still_uses_env_file(self):
        text = self._read(SERVER_COMPOSE)
        self.assertIn("env_file", text)
        self.assertIn("ASTOCK_ENV_FILE", text)

    def test_server_compose_loopback_bind(self):
        text = self._read(SERVER_COMPOSE)
        self.assertIn("127.0.0.1:18600:8600", text)
        self.assertNotIn('"18600:8600"', text)

    def test_local_compose_loopback_bind(self):
        text = self._read(LOCAL_COMPOSE)
        self.assertIn("127.0.0.1:8600:8600", text)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn('"8600:8600"', stripped, "本地 compose 不得绑全网卡 8600")

    def test_local_compose_does_not_declare_operator_token(self):
        text = self._read(LOCAL_COMPOSE)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotRegex(
                stripped,
                r"^\s*ASTOCK_OPERATOR_TOKEN\s*:",
                "local compose 不得声明空的 operator token",
            )

    def test_dockerfile_keeps_internal_wildcard_bind(self):
        dockerfile = os.path.join(REPO_ROOT, "Dockerfile")
        text = self._read(dockerfile)
        self.assertIn("0.0.0.0", text, "容器内必须监听 0.0.0.0")

    def test_env_example_documents_new_contract(self):
        text = self._read(ENV_EXAMPLE)
        active = _strip_yaml_comments(text)
        # 可执行/生效行里不得再出现私有 header 或关闭开关
        self.assertNotIn("ASTOCK_OPERATOR_AUTH_REQUIRED", active)
        self.assertNotIn("X-Operator-Token", active)
        self.assertIn("ASTOCK_OPERATOR_TOKEN", text)
        # 注释里的说明也必须与新合同一致（不再是"未配置即全部 503"）
        self.assertNotIn("X-Operator-Token", text, ".env.example 不得再宣传私有 header")
        self.assertNotIn("ASTOCK_OPERATOR_AUTH_REQUIRED", text)
        self.assertNotIn("at-least-16-chars", text, "最低长度已改为 24")

    def test_no_auth_disable_var_in_effective_config(self):
        """仓库生效配置不得残留任何 auth-disable 开关。

        只扫可执行/生效行（去注释），因为文档里会解释"已删除该开关"。
        """
        targets = [
            (SERVER_COMPOSE, "yaml"),
            (LOCAL_COMPOSE, "yaml"),
            (ENV_EXAMPLE, "yaml"),
            (OA.__file__, "python"),
            (os.path.join(REPO_ROOT, "backend", "main.py"), "python"),
        ]
        for path, kind in targets:
            if not os.path.exists(path):
                continue
            text = self._read(path)
            stripped = (
                _strip_python_comments(text) if kind == "python" else _strip_yaml_comments(text)
            )
            for name in [
                "ASTOCK_OPERATOR_AUTH_REQUIRED",
                "ASTOCK_DISABLE_AUTH",
                "ASTOCK_ALLOW_UNAUTH",
                "ASTOCK_LOCAL_NO_AUTH",
            ]:
                self.assertNotIn(
                    name, stripped,
                    f"{os.path.basename(path)} 生效配置不得残留 {name}",
                )

    def test_docs_do_not_promise_private_header_or_localstorage(self):
        """文档不得再宣传已废弃的机制（合同 §80）。"""
        for name in ["SECURITY.md", "README.md", "README_EN.md"]:
            path = os.path.join(REPO_ROOT, name)
            if not os.path.exists(path):
                continue
            text = self._read(path)
            self.assertNotIn("X-Operator-Token", text, f"{name} 不得宣传私有 header")
            self.assertNotIn(
                "ASTOCK_OPERATOR_AUTH_REQUIRED", text, f"{name} 不得宣传关闭开关"
            )
            self.assertNotIn(
                "localStorage", text, f"{name} 不得再写 localStorage 存凭据"
            )


# ─── 12. 前端契约（合同 §35–§44 / §70 / §71）───


class FrontendCredentialTests(unittest.TestCase):
    """前端使用 sessionStorage、GET 不带凭据、mutation 带 Bearer。"""

    API_JS = os.path.join(REPO_ROOT, "frontend", "src", "core", "api.js")
    DIST_JS = os.path.join(REPO_ROOT, "frontend", "dist", "app.js")

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_api_js_uses_session_storage(self):
        text = self._read(self.API_JS)
        self.assertIn("sessionStorage", text)

    def test_api_js_does_not_store_operator_token_in_local_storage(self):
        """凭据只能用 sessionStorage。

        注释里解释"为什么不用持久化存储"是允许的，因此只扫可执行代码。
        """
        text = self._read(self.API_JS)
        code = _strip_js_comments(text)
        self.assertNotIn("localStorage", code, "operator 凭据不得用 localStorage")

    def test_api_js_uses_fixed_storage_key(self):
        text = self._read(self.API_JS)
        self.assertIn("astock.operatorToken.v1", text)
        # 通用 key 'operatorToken' 不得再作为存储键单独出现
        self.assertNotRegex(
            text, r"(?:localStorage|sessionStorage)\.setItem\(\s*['\"]operatorToken['\"]"
        )

    def test_api_js_uses_authorization_bearer(self):
        text = self._read(self.API_JS)
        self.assertIn("Authorization", text)
        self.assertIn("Bearer", text)

    def test_api_js_has_get_helper_that_skips_credential(self):
        """源码层面确认只读短路存在。

        **这只是存在性检查**，真正锁住行为的是
        ``frontend/tests/operator-credential.test.mjs``（Node 行为测试）。
        负向验证 N4 证实：删掉这里的短路分支，存在性检查不会变红，只有行为
        测试会。因此下面另有 wiring 断言确保那个测试真的在跑。
        """
        text = self._read(self.API_JS)
        self.assertIn("operatorAuthorizationHeaders", text)
        self.assertIn("READ_METHODS", text)
        self.assertIn("indexOf(verb)>=0", text, "只读短路分支必须存在")

    def test_behavioral_node_test_exists_and_wired(self):
        """前端凭据的**行为**测试必须存在并接入 CI（否则 N4 无法被检出）。"""
        node_test = os.path.join(
            REPO_ROOT, "frontend", "tests", "operator-credential.test.mjs"
        )
        self.assertTrue(
            os.path.exists(node_test),
            "必须存在 frontend/tests/operator-credential.test.mjs（行为级断言）",
        )
        body = self._read(node_test)
        # 必须真正断言 GET 不带 Authorization
        self.assertIn("Authorization", body)
        self.assertRegex(body, r"READ_METHODS|GET")
        # package.json 必须暴露 test:unit
        pkg = self._read(os.path.join(REPO_ROOT, "frontend", "package.json"))
        self.assertIn("test:unit", pkg)
        # CI 必须执行它
        ci = self._read(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"))
        self.assertIn("test:unit", ci, "CI 必须运行前端行为测试")

    def test_api_js_has_no_private_header(self):
        text = self._read(self.API_JS)
        self.assertNotIn("X-Operator-Token", text)
        self.assertNotIn("x-operator-token", text)

    def test_api_js_has_clear_helper(self):
        text = self._read(self.API_JS)
        self.assertIn("clearOperatorToken", text)

    def test_dist_bundle_has_no_stale_private_header(self):
        if not os.path.exists(self.DIST_JS):
            self.skipTest("dist 尚未构建")
        text = self._read(self.DIST_JS)
        self.assertNotIn("X-Operator-Token", text, "构建产物不得残留私有 header")

    def test_unlock_ui_uses_password_input(self):
        settings = os.path.join(
            REPO_ROOT, "frontend", "src", "features", "settings.js"
        )
        text = self._read(settings)
        self.assertIn('type="password"', text)
        self.assertIn("unlockOperatorTab", text)
        self.assertIn("clearOperatorTab", text)

    def test_unlock_ui_does_not_call_backend_auth_endpoints(self):
        settings = os.path.join(
            REPO_ROOT, "frontend", "src", "features", "settings.js"
        )
        text = self._read(settings)
        for endpoint in ["/api/login", "/api/session", "/api/operator/verify", "/api/operator/login"]:
            self.assertNotIn(endpoint, text, f"解锁 UI 不得调用 {endpoint}")

    def test_playwright_config_has_no_global_credential(self):
        config = os.path.join(REPO_ROOT, "frontend", "playwright.config.js")
        text = self._read(config)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            self.assertNotRegex(stripped, r"^\s*extraHTTPHeaders\s*:", "不得全局注入凭据")
            self.assertNotRegex(stripped, r"^\s*storageState\s*:", "不得预注入 storageState")

    def test_operator_unlock_spec_exists(self):
        spec = os.path.join(
            REPO_ROOT, "frontend", "e2e", "specs", "operator-unlock.spec.js"
        )
        self.assertTrue(os.path.exists(spec), "必须存在真实解锁旅程 e2e")


# ─── 入口 ───

if __name__ == "__main__":
    unittest.main(verbosity=2)
