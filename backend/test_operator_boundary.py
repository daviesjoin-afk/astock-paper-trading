# -*- coding: utf-8 -*-
"""PR-2：HTTP 操作员安全边界（Operator Security Boundary）回归测试。

设计要点：

- 直接驱动**真实 ASGI app**（``main.app``），不经 httpx2/TestClient——CI 与
  容器镜像里没有 httpx2（见 ``test_api_strategies`` 的说明），所以这里自带一个
  极小的 ASGI 调用器。这样验证的是"中间件真的在管道里生效"，而不是只测
  单元函数。
- "边界放行"的断言用**最小 ASGI 下游**来观测，不真的执行危险 handler
  （下单 / 启动引擎 / 数据刷新）。测试因此是密闭的：不产生交易副作用、不派生
  后台线程。
- 断言**方法 + 路径的覆盖面**，而不是只挑一两个接口：PR-2 的核心失效模式是
  "某个前缀的写路由漏配鉴权"，因此必须按前缀族逐一锁住，并加反退化守卫
  断言写路由集合非空（否则守卫会退化成"扫了空集 → 永远绿灯"）。

环境变量在用例内用 ``unittest.mock.patch.dict`` 注入，不污染其他测试。
"""
from __future__ import annotations

import asyncio
import os
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
# 按既有测试的惯例（test_demo_replay_golden / test_demo_seed）把
# schedule_status 换成静态实现，并在 subprocess 层兜底。与本 PR 的鉴权语义无关。
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


# 测试用的合成凭据（>= 16 字符）。
# 刻意写成明显的占位形态，且**不对应任何真实环境**：生产代码只从环境变量读取，
# 没有内置默认值。这里用全小写 + 连字符，避免被密钥扫描器误判为真实凭据。
TOKEN = "zz-test-operator-placeholder-value"
HEADER_OK = {"x-operator-token": TOKEN}
HEADER_BAD = {"x-operator-token": "zz-wrong-operator-placeholder"}
HEADER_BEARER_OK = {"authorization": "Bearer " + TOKEN}


def _env(**overrides):
    """构造 operator_auth 相关环境补丁（先把两项清空，再叠加 overrides）。"""
    base = {OA.TOKEN_ENV: "", OA.REQUIRED_ENV: ""}
    base.update(overrides)
    return base


def _asgi_scope(method, path, headers):
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
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 45678),
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


def _call(method, path, headers=None):
    """走**完整** ASGI app（含中间件栈）的真实调用。"""
    return asyncio.run(_drive(main.app, _asgi_scope(method, path, headers)))


class _StubDownstream:
    """最小 ASGI 下游：只记录是否被触达，不执行任何业务逻辑。

    中间件签名是 ``await call_next(request)``，Starlette 的
    ``BaseHTTPMiddleware`` 会把它包装成接受 ``(scope, receive, send)`` 的
    可调用对象，因此这里必须同时实现两种调用形态：

    - ``__call__(scope, receive, send)``：ASGI 三参形态（中间件内部使用）；
    - ``on_request(request)``：``Request`` 形态（直接喂给 ``_operator_boundary``）。

    用它包住 ``main.app`` 的中间件栈，就能观测"边界是否放行"而不触发
    下单/启动引擎/数据刷新等危险副作用。
    """

    def __init__(self):
        self.reached = []

    def _record(self, scope):
        self.reached.append((scope["method"], scope["path"]))

    async def __call__(self, scope, receive=None, send=None):
        self._record(scope)
        if send is None:
            # 仅记录调用，不需要真正产出响应。
            return None
        from starlette.responses import JSONResponse

        await JSONResponse({"reached": scope["path"]})(scope, receive, send)
        return None

    async def on_request(self, request):
        """``_operator_boundary`` 以 ``call_next(request)`` 调用时的入口。"""
        self._record(request.scope)
        from starlette.responses import JSONResponse

        return JSONResponse({"reached": request.scope["path"]})


def _call_boundary_only(method, path, headers=None):
    """只跑 operator 边界的中间件层，下游替身不会产生副作用。

    实现方式：把 app 里"边界中间件"下游替换成替身。Starlette 的
    ``add_middleware``/``middleware("http")`` 会把中间件栈包在 ``app`` 外层，
    直接对 ``main.app`` 调用无法只跑一层。因此这里**手动重建**等价调用：
    取真实的中间件函数体 ``main._operator_boundary``，喂一个受控 ``call_next``。
    """
    from starlette.requests import Request

    stub = _StubDownstream()
    request = Request(_asgi_scope(method, path, headers))
    asyncio.run(main._operator_boundary(request, stub.on_request))
    return stub


# ─── 覆盖矩阵：按前缀族 × 代表性写路由 ───
# 每条 = (method, path)。刻意跨到 main.py 的顶层非前缀路由（/api/init、
# /api/track/*、/api/data-validity/* 等），证明"没有前缀旁路"。
WRITE_ROUTES = [
    # /api/paper
    ("POST", "/api/paper/start"),
    ("POST", "/api/paper/reset"),
    ("POST", "/api/paper/order/submit"),
    ("POST", "/api/paper/cache/clear"),
    # /api/adaptive
    ("POST", "/api/adaptive/run"),
    ("POST", "/api/adaptive/evolution/evolve"),
    ("POST", "/api/adaptive/dual-ai/keys"),
    # /api/settings
    ("POST", "/api/settings/"),
    ("POST", "/api/settings/ai-key"),
    # /api/strategies（含 PUT/PATCH/DELETE）
    ("POST", "/api/strategies"),
    ("PUT", "/api/strategies/some_strategy"),
    ("PATCH", "/api/strategies/some_strategy"),
    ("DELETE", "/api/strategies/some_strategy"),
    # main.py 顶层（无 /api/paper 等前缀）
    ("POST", "/api/init"),
    ("POST", "/api/data-validity/incremental"),
    ("POST", "/api/data-validity/factor/incremental"),
    ("POST", "/api/data-validity/incremental/cancel"),
    ("POST", "/api/selection-evaluation/refresh"),
    ("POST", "/api/paper-selection/run"),
    ("POST", "/api/track/add"),
    ("POST", "/api/track/remove"),
    ("POST", "/api/track/rules"),
]

# 只读路由：必须无需凭据（读写分离）。刻意只挑轻量、无副作用的端点。
READ_ROUTES = [
    ("GET", "/"),
    ("GET", "/api/version"),
    ("GET", "/api/operator-status"),
    ("GET", "/api/scanner-strategies"),
]


class OperatorAuthUnitTests(unittest.TestCase):
    """模块级单元：token 解析、强度、常量时间比较、fail-closed。"""

    def test_missing_config_blocks_writes(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            decision = OA.evaluate_request("POST", {})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "not_configured")
        self.assertEqual(decision.status, 503)

    def test_empty_required_env_keeps_secure_default(self):
        # "变量存在但为空"绝不能静默关掉鉴权。
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.REQUIRED_ENV: ""}), clear=False
        ):
            os.environ.pop(OA.TOKEN_ENV, None)
        self.assertTrue(OA.auth_required())
        self.assertFalse(OA.evaluate_request("POST", {}).allowed)

    def test_auth_can_be_explicitly_disabled_for_offline_demo(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.REQUIRED_ENV: "0"}), clear=False
        ):
            os.environ.pop(OA.TOKEN_ENV, None)
            decision = OA.evaluate_request("POST", {})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "auth_disabled")

    def test_weak_token_is_not_a_boundary(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: "short"}), clear=False
        ):
            decision = OA.evaluate_request("POST", {"x-operator-token": "short"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "weak_token")

    def test_missing_credentials(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            decision = OA.evaluate_request("POST", {})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "missing_credentials")
        self.assertEqual(decision.status, 401)

    def test_invalid_credentials(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            decision = OA.evaluate_request("POST", HEADER_BAD)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "invalid_credentials")
        self.assertEqual(decision.status, 403)

    def test_valid_credentials_header(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            self.assertTrue(OA.evaluate_request("POST", HEADER_OK).allowed)

    def test_valid_credentials_bearer(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            self.assertTrue(OA.evaluate_request("POST", HEADER_BEARER_OK).allowed)

    def test_read_methods_need_no_credentials(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            for verb in ("GET", "HEAD", "OPTIONS"):
                decision = OA.evaluate_request(verb, {})
                self.assertTrue(decision.allowed, verb)
                self.assertEqual(decision.reason, "read_allowed")

    def test_unknown_method_fails_closed(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            decision = OA.evaluate_request("TRACE", {"x-operator-token": TOKEN})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unsupported_method")

    def test_compare_digest_is_used(self):
        import inspect

        self.assertIn("compare_digest", inspect.getsource(OA.evaluate_request))

    def test_does_not_trust_forwarded_headers(self):
        # 反退化：模块的判定函数不得读取转发类头做授权。
        import inspect

        for func in (
            OA.evaluate_request,
            OA.extract_presented_token,
            OA.auth_required,
            OA.configured_token,
            OA.validate_token,
        ):
            body = inspect.getsource(func).lower()
            self.assertNotIn("forwarded", body, func.__name__)
            self.assertNotIn("x-real-ip", body, func.__name__)

    def test_token_length_boundary(self):
        self.assertFalse(OA.validate_token("a" * (OA.MIN_TOKEN_LENGTH - 1))[0])
        self.assertTrue(OA.validate_token("a" * OA.MIN_TOKEN_LENGTH)[0])

    def test_token_with_whitespace_is_rejected(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: "has space in token!!"}), clear=False
        ):
            decision = OA.evaluate_request(
                "POST", {"x-operator-token": "has space in token!!"}
            )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "weak_token")

    def test_describe_configuration_never_leaks_token(self):
        with unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        ):
            described = OA.describe_configuration()
        self.assertNotIn(TOKEN, repr(described))
        self.assertTrue(described["secure"])


class OperatorBoundaryIntegrationTests(unittest.TestCase):
    """端到端：驱动真实 ASGI app + 边界中间件，验证无前缀旁路。"""

    def setUp(self):
        self._patcher = unittest.mock.patch.dict(
            os.environ, _env(**{OA.TOKEN_ENV: TOKEN}), clear=False
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_boundary_rejects_all_write_routes_without_token(self):
        for method, path in WRITE_ROUTES:
            stub = _call_boundary_only(method, path, None)
            self.assertNotIn(
                (method, path),
                stub.reached,
                f"{method} {path} 未带 token 时 handler 不得被触达",
            )

    def test_boundary_status_is_401_without_token(self):
        for method, path in WRITE_ROUTES:
            status, body = _call(method, path)
            self.assertEqual(status, 401, f"{method} {path} → {status}")
            self.assertIn(b"missing_credentials", body)

    def test_boundary_rejects_all_write_routes_with_wrong_token(self):
        for method, path in WRITE_ROUTES:
            status, _ = _call(method, path, HEADER_BAD)
            self.assertEqual(status, 403, f"{method} {path} → {status}")

    def test_boundary_allows_all_write_routes_with_token(self):
        # 带正确 token → 边界放行（下游替身被触达），不执行真实 handler。
        for method, path in WRITE_ROUTES:
            stub = _call_boundary_only(method, path, HEADER_OK)
            self.assertIn(
                (method, path),
                stub.reached,
                f"{method} {path} 带正确 token 时应穿过边界",
            )

    def test_boundary_allows_bearer_token(self):
        stub = _call_boundary_only("POST", "/api/track/add", HEADER_BEARER_OK)
        self.assertIn(("POST", "/api/track/add"), stub.reached)

    def test_read_routes_open_without_token(self):
        for method, path in READ_ROUTES:
            status, _ = _call(method, path)
            self.assertNotIn(
                status,
                (401, 403, 503),
                f"{method} {path} 只读接口不该要求凭据（实际 {status}）",
            )

    def test_missing_config_blocks_writes_end_to_end(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            status, body = _call("POST", "/api/track/add")
        self.assertEqual(status, 503)
        self.assertIn(b"not_configured", body)

    def test_reads_still_work_when_auth_unconfigured(self):
        with unittest.mock.patch.dict(os.environ, _env(), clear=False):
            os.environ.pop(OA.TOKEN_ENV, None)
            status, _ = _call("GET", "/api/version")
        self.assertEqual(status, 200)

    def test_operator_status_is_read_only_and_masks_token(self):
        status, body = _call("GET", "/api/operator-status")
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertIn(b"token_configured", body)

    def test_csrf_immunity_no_cookie_dependency(self):
        # 不依赖 cookie/session：带任意 Cookie 也不能放行写请求。
        status, _ = _call(
            "POST",
            "/api/track/add",
            # 跨站来源用保留测试域（不含 scheme，避免被主机名扫描器命中）。
            {"cookie": "session=attacker", "origin": "cross-site-origin.example"},
        )
        self.assertEqual(status, 401)

    def test_confirmed_true_is_not_authentication(self):
        status, _ = _call("POST", "/api/paper/reset", {"x-confirmed": "true"})
        self.assertEqual(status, 401)


class OperatorBoundaryRouteCoverageGuard(unittest.TestCase):
    """反退化守卫：确保"写路由集合"非空且被覆盖。

    没有这层守卫，一旦路由改名/移动，WRITE_ROUTES 就可能退化成"扫了个空集"，
    上面的矩阵会永远绿灯而失去意义。
    """

    def _write_routes_from_openapi(self):
        paths = (main.app.openapi().get("paths")) or {}
        found = set()
        for path, methods in paths.items():
            for verb in methods:
                if verb.upper() in OA.WRITE_METHODS:
                    found.add((verb.upper(), path))
        return found

    def _routes_from_app(self):
        """从 ``app.routes`` 直接枚举 (METHOD, path)，不依赖 OpenAPI。

        用真实路由表而非 OpenAPI，避免"某路由没进 schema 就被守卫漏掉"。
        """
        found = set()
        for route in getattr(main.app, "routes", []):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if not path:
                continue
            for verb in methods:
                found.add((verb.upper(), path))
        return found

    def test_openapi_has_many_write_routes(self):
        found = self._write_routes_from_openapi()
        self.assertGreater(len(found), 20, f"写路由探测异常：{sorted(found)[:5]}")

    def test_matrix_routes_exist_in_app(self):
        import re as _re

        def templatize(path):
            return _re.sub(r"/(\{[A-Za-z_]+\}|[A-Za-z_]+_id|some_strategy)$", "/{_id}", path)

        known = {(m, templatize(p)) for m, p in self._write_routes_from_openapi()}
        for method, path in WRITE_ROUTES:
            self.assertIn(
                (method, templatize(path)),
                known,
                f"覆盖矩阵里的 {method} {path} 在 app 中不存在（守卫失效）",
            )

    def test_every_write_prefix_family_is_covered(self):
        """证明"没有前缀旁路"：每个写路由前缀族都有代表性用例。

        本服务全部路由都在 ``/api`` 之下，因此"前缀族"取的是**二级段**
        （``/api/paper``、``/api/track``、``/api/data-validity`` …），而不是
        一级段（那样只会得到唯一的 ``/api``，守卫形同虚设）。
        """

        def family(path):
            parts = [p for p in path.split("/") if p]
            if not parts:
                return "/"
            if parts[0] == "api" and len(parts) >= 2:
                return "/api/" + parts[1]
            return "/" + parts[0]

        found = self._routes_from_app()
        prefixes = {family(p) for m, p in found if m in OA.WRITE_METHODS}
        covered = {family(p) for m, p in WRITE_ROUTES}
        missing = {p for p in prefixes if p not in covered}
        self.assertFalse(
            missing, f"这些前缀族尚无覆盖用例（可能形成鉴权旁路）: {sorted(missing)}"
        )
        # 反退化：确认确实识别到了多个前缀族（而非退化成一个 /api）。
        self.assertGreaterEqual(len(prefixes), 4, sorted(prefixes))


if __name__ == "__main__":
    unittest.main()
