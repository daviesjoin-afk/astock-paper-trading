# -*- coding: utf-8 -*-
"""PR-2：HTTP 操作员安全边界（Operator Security Boundary）。

本模块把"谁可以改变系统状态"这一个问题收敛到唯一一处，供 HTTP 控制面统一
调用。它**只做操作员边界**：不引入账户系统、OAuth、JWT、RBAC、session
框架，也不涉及任何交易/选股/风控/自进化算法。

## 威胁模型（本模块负责拦截）

1. 未认证的远程状态变更（unauthenticated remote mutation）。
2. 浏览器跨站请求触发的状态变更 / CSRF。
3. DNS-rebinding 式的 localhost 状态变更。
4. 未来新增的 POST/PUT/PATCH/DELETE 路由忘记单独补鉴权。
5. operator token 出现在 URL / query / 日志里。
6. 弱 token 配置静默降级成"无鉴权模式"。
7. 被伪造的 ``X-Forwarded-For`` 被应用层手工信任。

## 设计原则

- **fail-closed**：写请求默认拒绝。只有"配置了足够强的 token 且请求携带
  正确凭据"才放行；任何配置异常都宁可整体拒绝，绝不静默降级为无鉴权。
  注意 fail-closed 落在**请求层**（每个写请求独立判定），而不是让进程无法
  启动——只读看板在未配置 token 时仍须可用（见 ``SECURITY.md`` 的读写分离）。
- **读/写分离**：``GET`` / ``HEAD`` / ``OPTIONS`` 不要求凭据（只读控制面），
  避免把只读看板也变成需要密钥的负担，也避免引入任何 cookie/session 状态
  从而天然免疫 CSRF（无凭据即无可被跨站冒用的身份）。
- **凭据只走请求头**：``X-Operator-Token``，或标准 ``Authorization`` 头（Bearer 方案）。
  不接受 query/body/URL 传递，避免 token 落入访问日志与浏览器历史。
- **常量时间比较**：用 :func:`hmac.compare_digest`，避免时序侧信道。
- **不信任 ``X-Forwarded-For``**：本模块不解析、不据此做任何授权判断。
  来源 IP 信任属于反向代理/网络 ACL 层（见 ``SECURITY.md``）。

## 公开 API

- :func:`configured_token` —— 读取当前进程配置的 operator token。
- :func:`is_configured` —— token 是否已配置（用于启动期 fail-closed 自检）。
- :func:`evaluate_request` —— 对单个请求做鉴权判定，返回 :class:`Decision`。
- :func:`assert_secure_configuration` —— 启动期校验，配置不安全时抛异常。

环境变量（唯一来源）：

- ``ASTOCK_OPERATOR_TOKEN`` —— operator 共享密钥。未配置时写请求一律 503。
- ``ASTOCK_OPERATOR_AUTH_REQUIRED`` —— 默认 ``1``（写请求必须鉴权）。显式设
  为 ``0`` 才允许在**未配置 token** 时放行写请求；这是给纯本机离线演示留的
  逃生阀，且必须由操作者显式开启，不会因为"忘记配置"而自动发生。
"""
from __future__ import annotations

import hmac
import os
import re

# ─── 常量 ───
TOKEN_ENV = "ASTOCK_OPERATOR_TOKEN"
REQUIRED_ENV = "ASTOCK_OPERATOR_AUTH_REQUIRED"
HEADER_TOKEN = "x-operator-token"
HEADER_AUTHORIZATION = "authorization"
BEARER_PREFIX = "bearer "

# 写方法：需要操作员边界。其余（GET/HEAD/OPTIONS）视为只读。
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# token 最短长度。低于此长度视为弱 token，直接拒绝配置（fail-closed），
# 而不是"降低标准放行"。
MIN_TOKEN_LENGTH = 16

# 允许出现在 token 里的字符；拒绝控制字符/空白，避免 header 注入与
# 复制粘贴引入首尾不可见字符导致的"配了但一直 401"。
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=\-]+$")


class OperatorAuthConfigError(RuntimeError):
    """operator 边界配置不安全（弱 token / 非法字符）。"""


class Decision:
    """一次鉴权判定的结果。

    ``allowed`` 为 True 表示放行；否则 ``status`` 与 ``detail`` 用于构造
    HTTP 响应。``reason`` 是稳定的机器可读标识，便于测试与审计。
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


def configured_token():
    """返回当前进程配置的 operator token，未配置时返回 ``None``。

    只读环境变量；空字符串等同未配置。
    """
    raw = os.getenv(TOKEN_ENV)
    if raw is None:
        return None
    token = raw.strip()
    return token or None


def auth_required():
    """写请求是否必须携带凭据。

    默认 True（安全默认）。只有操作者**显式**把
    ``ASTOCK_OPERATOR_AUTH_REQUIRED`` 设为 ``0``/``false``/``no``/``off``
    时才为 False。

    **空字符串/空白视为"未设置"→ 保持默认 True**。这一点是刻意的安全语义：
    一个"变量存在但为空"的配置（例如 ``ASTOCK_OPERATOR_AUTH_REQUIRED=``）
    绝不能静默关掉鉴权——那正是威胁模型里"弱配置静默降级成无鉴权模式"的
    一种形态。要关闭必须写下明确的否定值。
    """
    raw = os.getenv(REQUIRED_ENV)
    if raw is None:
        return True
    value = str(raw).strip().lower()
    if value == "":
        # 空值 = 未设置 → 维持安全默认。
        return True
    return value not in {"0", "false", "no", "off"}


def is_configured():
    """是否已配置 operator token。"""
    return configured_token() is not None


def validate_token(token):
    """校验 token 强度，返回 ``(ok, reason)``。

    不做任何"自动修正"。弱 token 是配置错误，必须让调用方显式处理。
    """
    if token is None:
        return False, "missing"
    if len(token) < MIN_TOKEN_LENGTH:
        return False, "too_short"
    if not _TOKEN_PATTERN.match(token):
        return False, "invalid_characters"
    return True, "ok"


def assert_secure_configuration(check_required=None):
    """配置自检，返回 ``(ok, message)``；**不抛异常、不阻断启动**。

    这里刻意不 fail 掉进程：本仓库明确支持"零配置基础模式"（见
    ``.env.example``），只读看板必须在未配置 token 时依然可用。安全保证由
    **请求层** 的 :func:`evaluate_request` 提供：未配置 token 时所有写请求
    一律 503。本函数只负责把"当前处于无边界状态"这件事**显著地**暴露给运维
    （启动日志 + ``/api/operator-status``），避免静默的安全假象。

    规则：

    - 默认要求 token；若未配置 → ``(False, 原因)``，写接口实际已被请求层挡住。
    - token 已配置但强度不足 → ``(False, 原因)``，同样不放行（弱 token 不是边界）。
    - 操作者显式 ``ASTOCK_OPERATOR_AUTH_REQUIRED=0`` 且未配置 token →
      ``(True, ...)``，表示"有意关闭"，但仍会在状态里标注 insecure。
    """
    required = auth_required() if check_required is None else bool(check_required)
    token = configured_token()
    if token is None:
        if required:
            return False, (
                f"{TOKEN_ENV} 未配置：写接口已按 fail-closed 拒绝（503）。"
                f"只读接口不受影响。请设置 >= {MIN_TOKEN_LENGTH} 字符的随机 token，"
                f"或显式设置 {REQUIRED_ENV}=0 以在纯本机离线模式下有意关闭鉴权。"
            )
        return True, (
            f"{REQUIRED_ENV}=0 且未配置 {TOKEN_ENV}：写接口处于"
            f"**有意关闭鉴权**状态，仅应在纯本机离线环境使用。"
        )
    ok, reason = validate_token(token)
    if not ok:
        return False, (
            f"{TOKEN_ENV} 强度不足（{reason}）：至少 {MIN_TOKEN_LENGTH} 字符，"
            f"仅允许 [A-Za-z0-9._~+/=-]。弱 token 不会被当作有效鉴权。"
        )
    return True, "operator token 已配置且强度合规。"


def describe_configuration():
    """返回可安全对外展示的配置状态摘要（**绝不包含 token 本身**）。"""
    token = configured_token()
    ok_strength, strength_reason = validate_token(token) if token is not None else (False, "missing")
    required = auth_required()
    ok, message = assert_secure_configuration()
    return {
        "auth_required": required,
        "token_configured": token is not None,
        "token_length": len(token) if token else 0,
        "token_strength": strength_reason,
        "writes_protected": bool(token is not None and ok_strength),
        "secure": bool(ok and ok_strength),
        "message": message,
    }


def extract_presented_token(headers):
    """从请求头取出调用方出示的 token。

    ``headers`` 需支持大小写不敏感的 ``.get(name)``（Starlette/FastAPI 的
    ``Headers`` 与普通 dict 均可——dict 走小写键）。命中其一即可：

    - ``X-Operator-Token: <token>``
    - 标准 ``Authorization`` 头，Bearer 方案（``Bearer <token>``）

    绝不从 query / URL / body 读取。
    """
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get(HEADER_TOKEN)
    except Exception:
        raw = None
    if not raw:
        try:
            auth = headers.get(HEADER_AUTHORIZATION)
        except Exception:
            auth = None
        if auth and str(auth).strip().lower().startswith(BEARER_PREFIX):
            raw = str(auth).strip()[len(BEARER_PREFIX):]
    if not raw:
        return None
    token = str(raw).strip()
    return token or None


def evaluate_request(method, headers, *, token=None, required=None):
    """判定一次请求是否通过操作员边界。

    参数：

    - ``method``：HTTP 方法（大小写不敏感）。
    - ``headers``：请求头。
    - ``token`` / ``required``：测试注入用；默认从环境读取。

    返回 :class:`Decision`。
    """
    verb = str(method or "").upper()

    # 只读方法：直接放行（读写分离）。
    if verb in READ_METHODS:
        return Decision(True, 200, "", "read_allowed")
    # 未知方法按写处理（fail-closed）。
    if verb not in WRITE_METHODS:
        return Decision(True, 200, "", "read_allowed") if verb == "" else Decision(
            False, 405, "不支持的 HTTP 方法", "unsupported_method"
        )

    need = auth_required() if required is None else bool(required)
    effective = configured_token() if token is None else (token or None)

    if effective is None:
        if need:
            # 未配置 token 且鉴权开启 → 拒绝（不可静默降级为无鉴权）。
            return Decision(
                False,
                503,
                "操作员边界未配置：服务端未设置 operator token，"
                "写接口已按 fail-closed 拒绝。",
                "not_configured",
            )
        # 操作者显式关掉了鉴权（纯本机离线模式）。
        return Decision(True, 200, "", "auth_disabled")

    ok, reason = validate_token(effective)
    if not ok:
        # 弱 token 不构成边界。
        return Decision(
            False,
            503,
            "操作员边界配置无效（token 强度不足），写接口已拒绝。",
            "weak_token",
        )

    presented = extract_presented_token(headers)
    if presented is None:
        return Decision(False, 401, "缺少操作员凭据", "missing_credentials")
    if not hmac.compare_digest(presented.encode("utf-8"), effective.encode("utf-8")):
        return Decision(False, 403, "操作员凭据无效", "invalid_credentials")
    return Decision(True, 200, "", "authorized")


def challenge_headers(decision):
    """为未通过鉴权的响应生成建议响应头（便于前端/操作者定位）。"""
    headers = {"Cache-Control": "no-store"}
    if decision is not None and decision.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return headers
