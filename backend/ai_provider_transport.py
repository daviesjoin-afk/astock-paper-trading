# -*- coding: utf-8 -*-
"""provider-neutral 的 OpenAI-compatible JSON chat **传输层**。

本模块只有一个职责：

    给定一份**已经解析好的** provider config，执行一次 OpenAI-compatible 的
    JSON chat request，并把响应解析成一个 JSON object。

它**不知道**任何业务概念：没有 market / stock / signal / research / risk /
execution / strategy / account / consensus / tuning 分支。因此这里没有
``if provider == "deepseek"`` 也没有 ``if slot == "ai1"`` —— 厂商身份不是本层的
输入维度。``provider_config["slot"]`` 只出现在错误文案里，不参与任何判定。

配置解析（数据库、环境变量、槽位默认值）**不属于**本层：调用方交进来的
必须是已解析好的 dict，本层不读 ``os.getenv``、不开 ``sqlite3`` 连接。

网络 ownership：整个仓库里 AI provider 的 HTTP 调用**只**允许出现在本模块。
``ai_research_provider`` 等上层只调用 :func:`call_json`。

失败一律 fail closed（:class:`ProviderTransportError` + 稳定 machine reason）：
HTTP 失败、外层响应不可解析、``choices`` 缺失、``content`` 不是字符串、
``content`` 不是合法 JSON、解析结果不是 JSON **object** —— 全部拒绝。
``[]`` / ``"abc"`` / ``123`` / ``null`` 都不是成功的响应。

**Secret 安全**：API Key 不出现在异常文案、``repr``、日志或返回值里。HTTP 错误
只保留 status code 与稳定 reason，绝不把 request headers / ``Authorization`` /
prompt / response body 拼进异常。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "ProviderTransportError",
    "REASON_API_KEY_MISSING",
    "REASON_MODEL_MISSING",
    "REASON_BASE_URL_INVALID",
    "REASON_HTTP_ERROR",
    "REASON_NETWORK_ERROR",
    "REASON_RESPONSE_NOT_JSON",
    "REASON_RESPONSE_NOT_OBJECT",
    "REASON_CHOICES_MISSING",
    "REASON_CONTENT_NOT_STRING",
    "REASON_CONTENT_NOT_OBJECT",
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_MAX_TOKENS",
    "MAX_MAX_TOKENS",
    "normalize_base_url",
    "chat_completions_url",
    "is_usable_base_url",
    "validate_base_url",
    "build_request_body",
    "call_json",
]

# ─── 资源边界 ───
DEFAULT_TIMEOUT_SECONDS = 40
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 300
MIN_MAX_TOKENS = 400
MAX_MAX_TOKENS = 4000

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

# ─── 稳定 machine reasons（审计只依赖这些，不依赖自由文案）───
REASON_API_KEY_MISSING = "api_key_missing"
REASON_MODEL_MISSING = "model_missing"
REASON_BASE_URL_INVALID = "base_url_invalid"
REASON_HTTP_ERROR = "http_error"
REASON_NETWORK_ERROR = "network_error"
REASON_RESPONSE_NOT_JSON = "response_not_json"
REASON_RESPONSE_NOT_OBJECT = "response_not_object"
REASON_CHOICES_MISSING = "choices_missing"
REASON_CONTENT_NOT_STRING = "content_not_string"
REASON_CONTENT_NOT_OBJECT = "content_not_object"


class ProviderTransportError(RuntimeError, ValueError):
    """一次 provider 调用失败 —— 携带**稳定 machine reason**，绝不携带凭据。

    ``reason`` 是唯一可被程序依赖的字段；``status`` 只在 HTTP 层失败时存在。
    刻意**不**把 request headers / ``Authorization`` / prompt / response body
    放进文案：错误诊断的价值远低于凭据泄漏的代价。

    同时继承 ``ValueError``：配置层拒绝（缺 model / 非法 base_url）在收敛到本层之
    前就是 ``ValueError``，调用方按旧类型捕获；网络与协议失败则一直是
    ``RuntimeError``。两种历史预期都必须继续成立，否则"抽取传输层"就变成了一次
    静默的行为变更。
    """

    def __init__(self, reason: str, *, status: int | None = None, detail: str = "") -> None:
        self.reason = str(reason)
        self.status = status
        parts = [self.reason]
        if status is not None:
            parts.append("status=%d" % status)
        if detail:
            parts.append(str(detail))
        super().__init__(": ".join(parts))


# ─────────────────────────────────────────────────────────────────────────────
# base_url —— 只做协议层归一化，不做配置来源解析
# ─────────────────────────────────────────────────────────────────────────────


def normalize_base_url(value) -> str:
    """规范化 base_url：去空白、去尾斜杠；完整接口地址也接受。"""
    text = str(value or "").strip().rstrip("/")
    if text.lower().endswith(_CHAT_COMPLETIONS_SUFFIX):
        text = text[: -len(_CHAT_COMPLETIONS_SUFFIX)].rstrip("/")
    return text


def chat_completions_url(base_url) -> str:
    """由 base_url 拼出 Chat Completions 地址。``/v1`` 与 ``/v1/`` 等价。"""
    base = normalize_base_url(base_url)
    if not base:
        raise ProviderTransportError(REASON_BASE_URL_INVALID)
    return base + _CHAT_COMPLETIONS_SUFFIX


def is_usable_base_url(value) -> bool:
    """base_url 是否是**真能发请求**的地址：scheme ∈ {http, https} 且 hostname 非空。

    宽松判定（不抛异常），既兜住保存校验，也兜住历史脏数据。
    """
    text = normalize_base_url(value)
    if not text or any(ch.isspace() for ch in text):
        return False
    try:
        parts = urllib.parse.urlsplit(text)
        hostname = parts.hostname
    except ValueError:  # 例如非法 IPv6 字面量
        return False
    return parts.scheme.lower() in ("http", "https") and bool(hostname)


def validate_base_url(value) -> str:
    """保存时的严格校验：不合法即抛错，把清晰原因回给调用方。

    空字符串表示"未配置 / 清除该字段"，允许通过（由 readiness 拦下）。
    """
    text = normalize_base_url(value)
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        raise ValueError("base_url 不能包含空白字符")
    try:
        parts = urllib.parse.urlsplit(text)
        hostname = parts.hostname
    except ValueError as exc:
        raise ValueError("base_url 无法解析：%s" % exc) from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not hostname:
        raise ValueError("base_url 缺少主机名")
    return text


def _timeout_seconds(provider_config) -> float:
    try:
        value = float(provider_config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT_SECONDS)
    return max(float(MIN_TIMEOUT_SECONDS), min(float(MAX_TIMEOUT_SECONDS), value))


# ─────────────────────────────────────────────────────────────────────────────
# request body
# ─────────────────────────────────────────────────────────────────────────────


def build_request_body(provider_config, system_prompt, user_prompt, max_tokens=1800):
    """构造最小公共 OpenAI 兼容请求体。

    刻意**只**包含所有兼容端点都认识的字段；没有任何按厂商/槽位身份增删字段的逻辑
    （历史上 DeepSeek 会被额外塞 ``thinking``，那正是要消灭的厂商耦合）。
    """
    model = str(provider_config.get("model") or "").strip()
    if not model:
        raise ProviderTransportError(
            REASON_MODEL_MISSING, detail="%s_model_missing" % provider_config.get("slot"),
        )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max(MIN_MAX_TOKENS, min(int(max_tokens), MAX_MAX_TOKENS)),
        "stream": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 一次调用
# ─────────────────────────────────────────────────────────────────────────────


def _parse_assistant_content(payload) -> dict:
    """从外层响应里取出助手内容并解析成 JSON **object**；任何偏差都 fail closed。

    ``[]`` / ``"abc"`` / ``123`` / ``null`` / 缺 ``choices`` / ``content`` 非字符串
    —— 全部不是成功的响应。协议不合法必须明确 RED，不能被静默当成可用结果。
    """
    if not isinstance(payload, dict):
        raise ProviderTransportError(REASON_RESPONSE_NOT_JSON)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderTransportError(REASON_CHOICES_MISSING)
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderTransportError(REASON_CHOICES_MISSING)
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderTransportError(REASON_CHOICES_MISSING)
    content = message.get("content")
    if not isinstance(content, str):
        raise ProviderTransportError(REASON_CONTENT_NOT_STRING)
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        raise ProviderTransportError(REASON_CONTENT_NOT_OBJECT) from None
    if not isinstance(parsed, dict):
        raise ProviderTransportError(REASON_CONTENT_NOT_OBJECT)
    return parsed


def call_json(provider_config, system_prompt, user_prompt, max_tokens=1800):
    """执行一次 provider 调用，返回 ``(parsed_json_object, in_tokens, out_tokens, latency_ms)``。

    校验顺序刻意把**便宜的本地检查放在网络之前**：缺 Key / 缺 model / 非法 base_url
    在任何请求发出前就 fail closed，绝不为了发现"配置不全"而先付费调用一次。
    """
    api_key = str(provider_config.get("api_key") or "")
    if not api_key.strip():
        raise ProviderTransportError(
            REASON_API_KEY_MISSING, detail="%s_api_key_missing" % provider_config.get("slot"),
        )
    base_url = provider_config.get("base_url")
    if not is_usable_base_url(base_url):
        raise ProviderTransportError(REASON_BASE_URL_INVALID)
    url = chat_completions_url(base_url)
    body = build_request_body(provider_config, system_prompt, user_prompt, max_tokens=max_tokens)

    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds(provider_config)) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 只保留 status code：response body 可能回显 prompt，headers 可能带凭据。
        raise ProviderTransportError(REASON_HTTP_ERROR, status=exc.code) from None
    except urllib.error.URLError:
        raise ProviderTransportError(REASON_NETWORK_ERROR) from None
    latency_ms = round((time.monotonic() - started) * 1000)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProviderTransportError(REASON_RESPONSE_NOT_JSON) from None
    parsed = _parse_assistant_content(payload)

    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return (
        parsed,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        latency_ms,
    )
