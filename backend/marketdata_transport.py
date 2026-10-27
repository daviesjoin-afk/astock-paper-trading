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


# ---------------------------------------------------------------------------
# 按主机的传输层短路。
#
# 为什么需要：盘中扫描 95% 的时间是网络等待，而其中很大一部分是在等**已经
# 挂掉的主机**。生产实测单轮 `http_get_attempt` 58 次调用里 34 次失败、累计
# 44 秒墙钟但只有 1.1 秒 CPU；这些等待串在 `risk_pass`（67s）、`position_quotes`
# （25s）等串行阶段里，直接把整轮扫描拖到 190 秒以上。
#
# 语义边界（刻意保持保守）：
# * 只在**传输层**失败（超时/连接错误）时计数；HTTP 状态错误说明对端活着，
#   反而清零。
# * 只有连续失败到阈值才短路，单次抖动不会触发。
# * 短路时抛 ``requests.ConnectionError``——与真实连接失败的异常类型一致，
#   因此所有调用方原有的 fail-closed 分支（缺数据即不发布/不入场）原样生效，
#   不会因为短路而放宽任何门禁。
_TRANSPORT_BREAKER_THRESHOLD = 6      # 连续多少次传输失败后短路
_TRANSPORT_BREAKER_COOLDOWN_SECONDS = 90.0

_transport_breaker = {}
_transport_breaker_lock = threading.Lock()


def _transport_host(url):
    """取主机名作为短路键；解析不出来就用固定 key，避免每个 URL 各占一格。"""
    text = str(url or "")
    marker = "://"
    if marker not in text:
        return "unknown"
    rest = text.split(marker, 1)[1]
    host = rest.split("/", 1)[0].split("?", 1)[0]
    return host.lower() or "unknown"


def _transport_breaker_remaining(host):
    """返回剩余短路秒数；未短路返回 None。"""
    with _transport_breaker_lock:
        state = _transport_breaker.get(host)
        if not state:
            return None
        remaining = (state.get("until") or 0.0) - time.monotonic()
        return remaining if remaining > 0 else None


def _transport_breaker_note(host, *, ok):
    """记录一次请求的健康结果；成功清零，连续失败到阈值则短路。"""
    with _transport_breaker_lock:
        state = _transport_breaker.setdefault(host, {"fails": 0, "until": 0.0})
        if ok:
            state["fails"] = 0
            state["until"] = 0.0
            return
        state["fails"] = int(state.get("fails") or 0) + 1
        if state["fails"] >= _TRANSPORT_BREAKER_THRESHOLD:
            state["until"] = time.monotonic() + _TRANSPORT_BREAKER_COOLDOWN_SECONDS


def reset_transport_breakers():
    """清空短路状态。给测试与故障恢复用；生产不需要定期调用。"""
    with _transport_breaker_lock:
        _transport_breaker.clear()


def transport_breaker_snapshot():
    """只读快照，供诊断与自检读取。"""
    with _transport_breaker_lock:
        now = time.monotonic()
        return {
            host: {
                "fails": int(state.get("fails") or 0),
                "open": bool((state.get("until") or 0.0) > now),
                "remaining_seconds": round(max(0.0, (state.get("until") or 0.0) - now), 1),
            }
            for host, state in _transport_breaker.items()
        }


def http_get(url, params=None, timeout=15, encoding="utf-8", retries=2):
    """执行带连接复用和空响应重试的 GET 请求。

    对端立刻断开连接时不再插入退避等待：等待既改变不了对端状态，又会在
    一个扫描轮次里累计十几秒空转（实测每轮 18~19 秒纯 sleep）。重试次数、
    超时、请求参数与返回值都不变；超时、HTTP 状态错误和空响应仍按原退避
    等待，外层仍拥有主机轮换与分批补查。

    外层再加一道**按主机短路**（见 ``_TRANSPORT_BREAKER_*``）：同一主机连续
    多次传输失败后，短时间内直接快速失败，不再为每个调用方重复付超时。
    """
    host = _transport_host(url)
    open_for = _transport_breaker_remaining(host)
    if open_for is not None:
        with PTM.phase("http_breaker_skip"):
            raise requests.ConnectionError(
                f"transport breaker open for {host} ({open_for:.0f}s left)")
    try:
        text = _http_get_impl(url, params=params, timeout=timeout,
                              encoding=encoding, retries=retries)
    except requests.RequestException as exc:
        if isinstance(exc, requests.HTTPError):
            # 4xx = 我们的请求有问题，主机本身可达 → 清零连续计数。
            # 5xx = 主机侧异常 → 计入失败（生产实测 push2 的 trends2 会快速返回
            # 502，而同主机的 details/ulist 在超时；若把 5xx 也算"可达"，两者
            # 交替出现会让短路永远不触发）。
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= int(status) < 500:
                _transport_breaker_note(host, ok=True)
            else:
                _transport_breaker_note(host, ok=False)
        else:
            _transport_breaker_note(host, ok=False)
        raise
    _transport_breaker_note(host, ok=True)
    return text


def _http_get_impl(url, params=None, timeout=15, encoding="utf-8", retries=2):
    """``http_get`` 的原实现（不含主机短路）。"""
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
