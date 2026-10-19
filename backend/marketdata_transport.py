# -*- coding: utf-8 -*-
"""共享行情 HTTP 传输层。

只负责连接复用、重试和腾讯源熔断状态；缓存、解析和具体 provider 仍由
``data_fetcher`` 维护。通过从旧模块重新导出名称，保留现有调用方兼容性。
"""

import threading
import time

import requests
import paper_timing as PTM


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


# 对端在链路层直接断开（RemoteDisconnected、拒绝连接）时，一次尝试通常
# 200~400 毫秒就返回。这个阈值用来把「立刻被断开」与「真的等到超时」区分开。
_CONNECTION_DROP_GRACE_SECONDS = 0.5


def _connection_dropped(exc, attempt_seconds):
    """判断失败是否属于对端立刻断开连接（而不是超时或 HTTP 状态错误）。

    ``requests.ConnectTimeout`` 同时继承 ``ConnectionError`` 与 ``Timeout``，
    必须先排除 ``Timeout``，否则连接超时会被误当成「立刻断开」而丢掉退避。
    """
    return (
        isinstance(exc, requests.ConnectionError)
        and not isinstance(exc, requests.Timeout)
        and attempt_seconds < _CONNECTION_DROP_GRACE_SECONDS
    )


def http_get(url, params=None, timeout=15, encoding="utf-8", retries=2):
    """执行带连接复用和空响应重试的 GET 请求。

    对端立刻断开连接时不再插入退避等待：等待既改变不了对端状态，又会在
    一个扫描轮次里累计十几秒空转（实测每轮 18~19 秒纯 sleep）。重试次数、
    超时、请求参数与返回值都不变；超时、HTTP 状态错误和空响应仍按原退避
    等待，外层仍拥有主机轮换与分批补查。
    """
    for index in range(retries + 1):
        attempt_started = time.perf_counter()
        try:
            with PTM.phase("http_get_attempt"):
                response = _session().get(url, params=params, timeout=timeout)
                response.raise_for_status()
            response.encoding = encoding
            if response.text:
                return response.text
            if index == retries:
                return ""
            if index < retries:
                with PTM.phase("http_retry_backoff"):
                    time.sleep(0.4 * (index + 1))
                continue
        except requests.RequestException as exc:
            if index == retries:
                raise
            if _connection_dropped(exc, time.perf_counter() - attempt_started):
                continue
        with PTM.phase("http_retry_backoff"):
            time.sleep(0.5 * (index + 1))
    return ""


def http_post_json(url, body, timeout=15):
    """执行 JSON POST；保持旧实现的失败返回空对象行为。"""
    try:
        with PTM.phase("http_post_attempt"):
            response = _session().post(url, json=body, timeout=timeout)
            response.raise_for_status()
        return response.json() if response.text else {}
    except Exception:
        return {}
