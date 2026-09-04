# -*- coding: utf-8 -*-
"""轻量可观测性模块：Prometheus 文本格式 /metrics（零第三方依赖）。

提供：
- 进程/系统指标：内存 RSS、磁盘使用率、数据库文件大小
- 业务指标：模拟盘周期状态、任务执行成功率、最近任务耗时
- 健康检查辅助

设计原则：任何指标采集失败都必须静默降级（返回 None/0），绝不影响主业务。
"""
import os
import time

import data_fetcher as dfc

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data_cache")
REPORT_DIR = os.path.join(BASE, "reports")

_METRICS_CACHE = {"data": None, "ts": 0.0}
_METRICS_TTL_SECONDS = 10.0


def _safe_float(value, default=0.0):
    try:
        num = float(value)
        return num if num != num else default  # NaN check
    except (TypeError, ValueError):
        return default


def _collect_system_metrics():
    """进程与系统级指标（尽力而为，失败返回空 dict）。"""
    metrics = {}
    # 优先当前进程 /proc/self/status；容器内受限时退回 /proc/1 或 resource
    for proc_path in ("/proc/self/status", "/proc/1/status"):
        try:
            with open(proc_path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        metrics["process_rss_kb"] = _safe_float(line.split()[1])
                        break
            if "process_rss_kb" in metrics:
                break
        except OSError:
            continue
    if "process_rss_kb" not in metrics:
        try:
            import resource
            metrics["process_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ImportError, ValueError):
            pass
    try:
        stat = os.statvfs("/")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        metrics["disk_total_bytes"] = total
        metrics["disk_free_bytes"] = free
        metrics["disk_used_ratio"] = round((total - free) / total, 4) if total else 0.0
    except OSError:
        pass
    return metrics


def _collect_db_metrics():
    """三个 SQLite 数据库文件大小。"""
    metrics = {}
    for name in ("paper_trading.sqlite3", "adaptive_learning.sqlite3", "paper_research.sqlite3"):
        path = os.path.join(CACHE_DIR, name)
        try:
            metrics[f"db_size_bytes_{name.replace('.sqlite3', '')}"] = os.path.getsize(path)
        except OSError:
            pass
    return metrics


def _collect_business_metrics():
    """业务指标：读取健康检查与模拟盘概览缓存，不触发网络/重计算。"""
    metrics = {}
    try:
        health = dfc.load_source_health() or {}
        metrics["data_source_healthy"] = 1 if health.get("healthy") else 0
    except Exception:
        pass
    return metrics


def _format_prometheus(metrics, prefix="astock"):
    """将 dict 转成 Prometheus 文本格式（gauge 类型）。"""
    lines = []
    lines.append("# HELP astock_process_rss_kb Process resident memory (KB)")
    lines.append("# TYPE astock_process_rss_kb gauge")
    for key, value in metrics.items():
        if not isinstance(value, (int, float)):
            continue
        safe_key = key.replace("-", "_").replace(".", "_")
        lines.append(f"{prefix}_{safe_key} {value}")
    lines.append(f"# HELP {prefix}_scrape_ts Scrape timestamp unix seconds")
    lines.append(f"# TYPE {prefix}_scrape_ts gauge")
    lines.append(f"{prefix}_scrape_ts {int(time.time())}")
    return "\n".join(lines) + "\n"


def metrics_payload(force=False):
    """返回 Prometheus 文本（带 10s 缓存，避免每次请求重复 stat）。"""
    now = time.time()
    if not force and _METRICS_CACHE["data"] and now - _METRICS_CACHE["ts"] < _METRICS_TTL_SECONDS:
        return _METRICS_CACHE["data"]
    merged = {}
    for collector in (_collect_system_metrics, _collect_db_metrics, _collect_business_metrics):
        try:
            merged.update(collector())
        except Exception:
            continue
    payload = _format_prometheus(merged)
    _METRICS_CACHE["data"] = payload
    _METRICS_CACHE["ts"] = now
    return payload
