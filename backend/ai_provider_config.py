# -*- coding: utf-8 -*-
"""Provider 配置的唯一 owner（B5）。

配置解析（数据库、环境变量、默认值）与 HTTP 传输是**两件事**：

- ``ai_provider_transport`` 只负责"给定一份已解析好的 provider config，
  发一次 OpenAI-compatible 请求"；它不读环境变量、不连数据库。
- 本模块负责"把配置解析出来"：厂商目录、环境变量兜底、默认 base_url/model、
  开关与超时，并把结果整理成 transport 认识的形状。

legacy 的 tuner 以前自己抄了一份目录、模型解析与 urllib 调用，于是同一个
厂商有两套 base_url 默认值、两套超时上限、两套错误文案。B5 把它们收敛到
这里：配置只解析一次，网络只由 transport 发出。

**能力边界**：本模块不决定"这次调用该不该发生"（那是 slot readiness 的职责），
也不读写账本。
"""
from __future__ import annotations

import os

import ai_provider_transport as transport

__all__ = [
    "PROVIDER_CATALOG",
    "provider_catalog",
    "provider_name",
    "model_name",
    "enabled",
    "configured",
    "base_url",
    "timeout_seconds",
    "legacy_provider_config",
    "call_json",
    "REASON_API_KEY_MISSING",
]

#: 厂商目录。``status`` 只表示"这是不是一个真实存在的厂商"，不表示它被启用。
PROVIDER_CATALOG = {
    "deepseek": {
        "label": "DeepSeek", "status": "active",
        "default_model": "deepseek-v4-flash",
        "default_base_url": "https://api.deepseek.com",
        "base_url_env": "DEEPSEEK_BASE_URL", "api_key_env": "DEEPSEEK_API_KEY",
    },
    "kimi": {
        "label": "Kimi", "status": "reserved",
        "default_model": "moonshot-v1-8k",
        "default_base_url": "https://api.moonshot.cn/v1",
        "base_url_env": "KIMI_BASE_URL", "api_key_env": "KIMI_API_KEY",
    },
    "mimo": {
        "label": "MIMO", "status": "active",
        "default_model": "mimo-v1",
        "default_base_url": "https://api.mimo.ai/v1",
        "base_url_env": "MIMO_BASE_URL", "api_key_env": "MIMO_API_KEY",
    },
}

#: legacy 路径的槽位名（transport 只用它写错误文案，不参与任何判定）。
LEGACY_SLOT = "legacy_tuner"

REASON_API_KEY_MISSING = transport.REASON_API_KEY_MISSING


def provider_catalog():
    """返回厂商目录的独立副本。"""
    return {key: dict(value) for key, value in PROVIDER_CATALOG.items()}


def provider_name():
    """当前厂商 id；未知值退回 deepseek。"""
    value = str(os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()
    return value if value in PROVIDER_CATALOG else "deepseek"


def model_name():
    """当前厂商的模型名。"""
    provider = provider_name()
    spec = PROVIDER_CATALOG[provider]
    default = str(spec.get("default_model") or "")
    return str(os.getenv(f"{provider.upper()}_MODEL") or default).strip() or default


def base_url():
    """当前厂商的 base_url（已去掉尾部斜杠）。"""
    spec = PROVIDER_CATALOG[provider_name()]
    default = str(spec.get("default_base_url") or "").rstrip("/")
    return str(os.getenv(spec["base_url_env"]) or default).strip().rstrip("/")


def api_key():
    spec = PROVIDER_CATALOG[provider_name()]
    return str(os.getenv(spec["api_key_env"]) or "").strip()


def timeout_seconds():
    """legacy 超时：默认 35 秒，受 transport 的上下界约束。"""
    raw = os.getenv("DEEPSEEK_TIMEOUT_SECONDS") or 35
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 35.0
    return max(float(transport.MIN_TIMEOUT_SECONDS),
               min(float(transport.MAX_TIMEOUT_SECONDS), value))


def enabled(config=None):
    """legacy 调参开关：环境变量或配置项任一为真即视为开启。"""
    truthy = str(os.getenv("LLM_ADVISOR_ENABLED") or "").strip().lower() in {
        "1", "true", "yes", "on", "enabled",
    }
    return truthy or bool((config or {}).get("llm_advisor_enabled", False))


def configured():
    """legacy 凭据门禁：厂商 API Key 是否存在。"""
    return bool(api_key())


def legacy_provider_config():
    """把 legacy 环境变量解析成 transport 认识的 ``provider_config``。"""
    return {
        "slot": LEGACY_SLOT,
        "provider": provider_name(),
        "api_key": api_key(),
        "base_url": base_url(),
        "model": model_name(),
        "timeout_seconds": timeout_seconds(),
        "enabled": bool(str(os.getenv("LLM_ADVISOR_ENABLED") or "").strip()),
    }


def call_json(system, user, max_tokens=1800):
    """一次 legacy provider 调用，返回 ``(parsed_json, in_tokens, out_tokens)``。

    网络与协议解析全部交给 ``ai_provider_transport``：它已经把 HTTP 失败、
    响应不可解析、choices 缺失、content 非对象等收敛成稳定 machine reason，
    并且保证 API Key 不出现在任何异常文案里。legacy 这里只保留旧的三元组
    形状与 ``api_key_missing`` 这条既有 reason。
    """
    config = legacy_provider_config()
    if not str(config.get("api_key") or "").strip():
        raise RuntimeError(REASON_API_KEY_MISSING)
    try:
        parsed, in_tokens, out_tokens, _latency_ms = transport.call_json(
            config, system, user, max_tokens=max_tokens)
    except transport.ProviderTransportError as exc:
        reason = getattr(exc, "reason", "") or ""
        if reason == transport.REASON_API_KEY_MISSING:
            raise RuntimeError(REASON_API_KEY_MISSING) from None
        raise RuntimeError(reason or "provider_transport_error") from None
    return parsed, in_tokens, out_tokens
