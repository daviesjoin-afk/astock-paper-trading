# -*- coding: utf-8 -*-
"""共享行情 HTTP 传输层。

只负责连接复用、重试和腾讯源熔断状态；缓存、解析和具体 provider 仍由
``data_fetcher`` 维护。通过从旧模块重新导出名称，保留现有调用方兼容性。
"""

import threading
import time

import requests


HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_session_local = threading.local()
_tencent_circuit_lock = threading.Lock()
_tencent_circuit = {
    "failures": 0,
    "open_until": 0.0,
    "backoff_seconds": 45,
    "last_probe_at": 0.0,
}


def _session():
    """每个下载线程复用独立的 requests.Session 连接池。"""
    session = getattr(_session_local, "value", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _session_local.value = session
    return session


def reset_data_source(reason=None, reset_circuit=False):
    """关闭当前线程连接池；按需重置腾讯源熔断器。"""
    del reason  # 保留旧签名，调用方可继续传递诊断原因。
    session = getattr(_session_local, "value", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
        try:
            delattr(_session_local, "value")
        except AttributeError:
            pass
    if reset_circuit:
        with _tencent_circuit_lock:
            _tencent_circuit.update(
                {
                    "failures": 0,
                    "open_until": 0.0,
                    "backoff_seconds": 45,
                    "last_probe_at": 0.0,
                }
            )


def http_get(url, params=None, timeout=15, encoding="utf-8", retries=2):
    """执行带连接复用和空响应重试的 GET 请求。"""
    for index in range(retries + 1):
        try:
            response = _session().get(url, params=params, timeout=timeout)
            response.raise_for_status()
            response.encoding = encoding
            if response.text:
                return response.text
            if index < retries:
                time.sleep(0.4 * (index + 1))
                continue
        except requests.RequestException:
            if index == retries:
                raise
        time.sleep(0.5 * (index + 1))
    return ""


def http_post_json(url, body, timeout=15):
    """执行 JSON POST；保持旧实现的失败返回空对象行为。"""
    try:
        response = _session().post(url, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json() if response.text else {}
    except Exception:
        return {}
