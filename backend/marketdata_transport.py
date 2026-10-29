# -*- coding: utf-8 -*-
"""共享行情 HTTP 传输层。

只负责连接复用、重试和腾讯源熔断状态；缓存、解析和具体 provider 仍由
``data_fetcher`` 维护。通过从旧模块重新导出名称，保留现有调用方兼容性。
"""

import json
import os
import threading
import time
import uuid

import requests
import paper_timing as PTM
from env_utils import env_float


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
# * 只在**传输层**失败（超时/连接错误/5xx）时计数；4xx 说明我们的请求有问题、
#   主机可达，反而清零。
# * 短路时抛 ``requests.ConnectionError``——与真实连接失败的异常类型一致，
#   因此所有调用方原有的 fail-closed 分支（缺数据即不发布/不入场）原样生效，
#   不会因为短路而放宽任何门禁。
#
# 为什么还要有"浪费预算"这一条：`http_get` 的默认参数是 ``timeout=15,
# retries=2``，**单个失败 URL 就要烧 45 秒**。若只按"连续 6 次"触发，最坏要
# 先浪费 270 秒才把主机短路——生产 close 时段实测出现过 480 秒里约 408 秒
# 没有任何子阶段覆盖的黑洞，正是这类"还没到阈值就先把超时预算烧光"的表现。
# 因此再加一条按**累计浪费时长**触发的闸门：同一主机累计浪费到 45 秒（约等于一次 timeout=15, retries=2 的最坏尝试）就短路，
# 与次数阈值取"或"关系。
_TRANSPORT_BREAKER_THRESHOLD = 6      # 连续多少次传输失败后短路
_TRANSPORT_BREAKER_BURN_BUDGET = 45.0  # 或：累计浪费多少秒后短路（=一次最坏尝试）
_TRANSPORT_BREAKER_COOLDOWN_SECONDS = 90.0

_transport_breaker = {}
_transport_breaker_lock = threading.Lock()
_PROCESS_STARTED = time.monotonic()


# ---------------------------------------------------------------------------
# 扫描尾部的预算收敛。
#
# 为什么需要：``http_get`` 的默认参数是 ``timeout=15, retries=2``，单个失败
# URL 最坏要烧 45 秒。一次 intraday 扫描有上百次 HTTP 调用，尾部几次"还在等
# 已经挂掉的源"就把整轮从 120 秒推到 300~500 秒，直接吃掉下一个三分钟调度窗。
# 主机短路能止住后续调用，但它只在**本轮进程内**生效，且要等到阈值才会触发。
#
# 这里的策略只作用于**扫描进程的后半程**：``ASTOCK_SCAN_BUDGET_SECONDS`` 由
# 调度入口（每轮一个全新 ``paper_runner`` 进程）注入，超过
# ``ASTOCK_SCAN_TAIL_AFTER_SECONDS`` 后，把后续请求的超时压到 4 秒、重试压到
# 1 次。语义不变——失败仍然抛给调用方，原有的 fail-closed/fail-open 分支照旧；
# 变的只是"愿意为一个已经不可用的源等多久"。
#
# 未设置预算的进程（API、测试、离线脚本）完全不受影响；扫描也只有在**已经
# 跑过尾部阈值**后才收敛，正常轮次的请求参数与现在完全一致。
# ---------------------------------------------------------------------------
_SCAN_BUDGET_SECONDS = env_float("ASTOCK_SCAN_BUDGET_SECONDS", 0.0, minimum=0.0)
_SCAN_TAIL_AFTER_SECONDS = env_float("ASTOCK_SCAN_TAIL_AFTER_SECONDS", 90.0, minimum=1.0)
_SCAN_TAIL_TIMEOUT_CAP = 8.0
_SCAN_TAIL_RETRIES_CAP = 1


def scan_tail_active():
    """当前进程是否已进入扫描尾部（未配置预算时永远为 False）。"""
    if _SCAN_BUDGET_SECONDS <= 0:
        return False
    return (time.monotonic() - _PROCESS_STARTED) >= _SCAN_TAIL_AFTER_SECONDS


def scan_budget_limits(timeout, retries):
    """按扫描尾部预算收敛单次请求的 ``(timeout, retries)``。

    ``timeout`` 可能是 ``(connect, read)`` 元组；无法比较时原样返回，绝不因为
    诊断优化改动调用方的请求契约。
    """
    if not scan_tail_active():
        return timeout, retries
    try:
        capped_timeout = min(float(timeout), _SCAN_TAIL_TIMEOUT_CAP)
        capped_retries = min(int(retries), _SCAN_TAIL_RETRIES_CAP)
    except (TypeError, ValueError):
        return timeout, retries
    return capped_timeout, capped_retries


# ---------------------------------------------------------------------------
# 跨进程熔断记忆。
#
# 每个 cron 轮次都是一次全新的 ``docker exec python paper_runner.py``，进程内
# 熔断表随进程销毁。于是同一个已经挂掉的主机，每一轮都要重新"学习"一次：
# 先烧 6 次失败 / 45 秒累计浪费，才开始短路。生产日志里每轮 20~159 次
# ``http_get_attempt`` 失败正是这个重复学习成本。
#
# 只持久化**已经短路**的主机与到期时刻，冷却很短（90s），文件原子替换；
# 读写失败一律忽略，熔断是优化而不是正确性依赖。默认不启用，由调度入口显式
# 注入 ``ASTOCK_TRANSPORT_BREAKER_FILE``，避免测试与 API 进程互相污染。
# ---------------------------------------------------------------------------
_BREAKER_STATE_FILE = (os.environ.get("ASTOCK_TRANSPORT_BREAKER_FILE") or "").strip()
_BREAKER_STATE_MAX_HOSTS = 32
_breaker_state_loaded = False


def _load_breaker_state():
    """把上一次进程留下的、仍未到期的短路状态并入当前熔断表。"""
    global _breaker_state_loaded
    if _breaker_state_loaded:
        return
    _breaker_state_loaded = True
    if not _BREAKER_STATE_FILE:
        return
    try:
        with open(_BREAKER_STATE_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    now_mono, now_wall = time.monotonic(), time.time()
    with _transport_breaker_lock:
        for host, raw in list(payload.items())[:_BREAKER_STATE_MAX_HOSTS]:
            if not isinstance(raw, dict):
                continue
            try:
                remaining = float(raw.get("until_wall") or 0.0) - now_wall
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                continue
            _transport_breaker[str(host)] = {
                "fails": int(raw.get("fails") or 0),
                "burned": float(raw.get("burned") or 0.0),
                "until": now_mono + remaining,
            }


def _persist_breaker_state():
    """原子写出当前仍短路的主机；没有配置文件或写失败时静默跳过。

    **快照与发布必须在同一把锁内**：否则两次并发写可能乱序完成 ——
    一次失败拍到"已打开的短路"，一次成功拍到"已清空"，但后者的文件替换
    先落地，前者的旧快照随后覆盖它。内存里主机是健康的，重新加载文件却
    又恢复出整段冷却期（实测约 90 秒），后续扫描会因此拒绝一个已经恢复
    的源；反过来的顺序则会丢掉刚打开的短路。

    所以这里一直持有 ``_transport_breaker_lock`` 到 ``os.replace`` 完成：
    状态更新与快照发布成为同一个临界区，旧的快照不可能替换新的。
    """
    if not _BREAKER_STATE_FILE:
        return
    now_mono, now_wall = time.monotonic(), time.time()
    with _transport_breaker_lock:
        payload = {
            host: {
                "fails": int(state.get("fails") or 0),
                "burned": round(float(state.get("burned") or 0.0), 2),
                "until_wall": round(now_wall + (state["until"] - now_mono), 3),
            }
            for host, state in _transport_breaker.items()
            if (state.get("until") or 0.0) > now_mono
        }
        if len(payload) > _BREAKER_STATE_MAX_HOSTS:
            payload = dict(list(payload.items())[:_BREAKER_STATE_MAX_HOSTS])
        temp_path = f"{_BREAKER_STATE_FILE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            os.replace(temp_path, _BREAKER_STATE_FILE)
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def breaker_state_path():
    """当前进程使用的持久化路径（诊断用；未启用时为空串）。"""
    return _BREAKER_STATE_FILE


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
    _load_breaker_state()
    with _transport_breaker_lock:
        state = _transport_breaker.get(host)
        if not state:
            return None
        remaining = (state.get("until") or 0.0) - time.monotonic()
        return remaining if remaining > 0 else None


def _transport_breaker_note(host, *, ok, burned=0.0):
    """记录一次请求的健康结果。

    成功清零；**连续失败到次数阈值**或**累计浪费到时长预算**任一满足即短路。
    后者是必要的：``timeout=15, retries=2`` 下单次失败就烧 45 秒，只按次数
    触发会让扫描先白等几百秒。
    """
    _load_breaker_state()
    state_changed = False
    with _transport_breaker_lock:
        state = _transport_breaker.setdefault(
            host, {"fails": 0, "burned": 0.0, "until": 0.0})
        if ok:
            state_changed = bool(state.get("until"))
            state["fails"] = 0
            state["burned"] = 0.0
            state["until"] = 0.0
        else:
            state["fails"] = int(state.get("fails") or 0) + 1
            state["burned"] = float(state.get("burned") or 0.0) + max(0.0, float(burned or 0.0))
            if (state["fails"] >= _TRANSPORT_BREAKER_THRESHOLD
                    or state["burned"] >= _TRANSPORT_BREAKER_BURN_BUDGET):
                if not (state.get("until") or 0.0) > time.monotonic():
                    state_changed = True
                state["until"] = time.monotonic() + _TRANSPORT_BREAKER_COOLDOWN_SECONDS
    if state_changed:
        _persist_breaker_state()


# ---------------------------------------------------------------------------
# 公开的短路 API。
#
# 别的模块（如 ``alt_data`` 的自有 GET 出口）需要共享同一份"这个主机是否已经
# 不可用"的判断。跨模块直接调用下划线函数属于隐性耦合，也会触发耦合预算里的
# ``private_cross_access`` 门禁，因此这里把三个操作显式公开；内部实现与
# ``http_get`` 用的是同一份状态。
# ---------------------------------------------------------------------------
def transport_host(url):
    """``url`` 对应的短路键（主机名）。"""
    return _transport_host(url)


def transport_breaker_remaining(host):
    """剩余短路秒数；未短路返回 None。"""
    return _transport_breaker_remaining(host)


def note_transport_result(host, *, ok, burned=0.0):
    """上报一次请求的健康结果（成功清零；失败按次数/累计浪费判定短路）。"""
    return _transport_breaker_note(host, ok=ok, burned=burned)


def reset_transport_breakers():
    """清空短路状态（含跨进程文件）。给测试与故障恢复用。"""
    global _breaker_state_loaded
    with _transport_breaker_lock:
        _transport_breaker.clear()
        _breaker_state_loaded = True
        # 删除也必须在同一把锁内：否则并发写可能刚落盘就被这里删掉，
        # 或者这里的删除晚于一次发布，把已经清空的状态又写回去。
        if _BREAKER_STATE_FILE:
            try:
                os.unlink(_BREAKER_STATE_FILE)
            except OSError:
                pass


def transport_breaker_snapshot():
    """只读快照，供诊断与自检读取。"""
    with _transport_breaker_lock:
        now = time.monotonic()
        return {
            host: {
                "fails": int(state.get("fails") or 0),
                "burned_seconds": round(float(state.get("burned") or 0.0), 2),
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
    timeout, retries = scan_budget_limits(timeout, retries)
    try:
        started = time.perf_counter()
        text = _http_get_impl(url, params=params, timeout=timeout,
                              encoding=encoding, retries=retries)
    except requests.RequestException as exc:
        burned = max(0.0, time.perf_counter() - started)
        if isinstance(exc, requests.HTTPError):
            # 4xx = 我们的请求有问题，主机本身可达 → 清零连续计数。
            # 5xx = 主机侧异常 → 计入失败（生产实测 push2 的 trends2 会快速返回
            # 502，而同主机的 details/ulist 在超时；若把 5xx 也算"可达"，两者
            # 交替出现会让短路永远不触发）。
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= int(status) < 500:
                _transport_breaker_note(host, ok=True)
            else:
                _transport_breaker_note(host, ok=False, burned=burned)
        else:
            # 超时/连接错误：这次失败实际烧掉多少墙钟就记多少。
            _transport_breaker_note(host, ok=False, burned=burned)
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
